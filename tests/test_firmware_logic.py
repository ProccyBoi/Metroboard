"""Compile the sketch's pure functions and compare them with firmware 1.5.1.

This exercises actual firmware logic with host String/strip adapters. It does
not replace a PlatformIO build or validation on an ESP32 and physical LEDs.
"""

import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def extract_function(source, name):
    match = re.search(r"^.*?\b" + name + r"\([^;]*?\)\s*\{", source, re.M)
    if not match:
        raise AssertionError(f"Firmware function missing: {name}")
    end = match.end()
    depth = 1
    # Selected pure functions contain no brace characters inside string literals.
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[match.start():end]


def sketch_bindings(source):
    sequences = {
        name: re.findall(r'"([^\"]+)"', body)
        for name, body in re.findall(
            r"const char\* const (\w+)\[\] = \{(.*?)\};", source, re.S
        )
    }
    lengths = [int(x) for x in re.search(
        r"STRIP_LEN\[LINE_COUNT\] = \{([^}]+)\}", source
    )[1].split(",")]
    reverse = [int(x) for x in re.search(
        r"REVERSE\[LINE_COUNT\] = \{([^}]+)\}", source
    )[1].split(",")]
    lines = re.findall(r"L_(\w+)", re.search(
        r"enum Line : uint8_t \{(.*?)LINE_COUNT", source, re.S
    )[1])
    bindings = []
    for line, name, start in re.findall(
        r"bindSeq\(L_(\w+), (\w+), sizeof\(\w+\) / sizeof\(\*\w+\)(?:, (\d+))?\);",
        source,
    ):
        line_index = lines.index(line)
        for index, station in enumerate(sequences[name], int(start or 0)):
            led = lengths[line_index] - 1 - index if reverse[line_index] else index
            if not 0 <= led < lengths[line_index]:
                raise AssertionError(f"Station outside physical strip: {station}")
            bindings.append((station + ("M" if line == "M1" else line[1:]), line, led))
    return bindings


class FirmwareLogicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "metroboard_public.ino").read_text()
        cls.bindings = sketch_bindings(cls.source)
        compiler = shutil.which("g++") or shutil.which("clang++")
        if not compiler:
            raise RuntimeError("Firmware host regression tests require g++ or clang++")
        cls.temp = tempfile.TemporaryDirectory(prefix="metroboard-firmware-")
        cls.addClassCleanup(cls.temp.cleanup)
        work = Path(cls.temp.name)
        generated = [HOST_ADAPTERS]
        for pattern in (
            r"enum Line : uint8_t \{.*?\};",
            r"const uint16_t STRIP_LEN\[LINE_COUNT\] = \{.*?\};",
            r"const uint32_t LINE_COLOR_HEX\[LINE_COUNT\] = \{.*?\};",
            r"static const char\* kPayloadBase =.*?;",
            r"static const char\* kFirmwareVersion =.*?;",
            r"static const char\* kBuildLabel =.*?;",
            r"static const size_t URL_CHAR_BUDGET =.*?;",
        ):
            generated.append(re.search(pattern, cls.source, re.S)[0])
        generated.extend([
            GLOBALS,
            "LedBinding B[] = {" + ",\n".join(
                "{" + json.dumps(key) + f", L_{line}, {led}" + "}"
                for key, line, led in cls.bindings
            ) + "};\nsize_t NB = sizeof(B) / sizeof(*B);",
        ])
        for name in (
            "unres", "appendEncoded", "enc", "lineColor", "lineColorScaled",
            "distanceLevelForState", "lineColorForState", "clearDirty",
            "computeBatchEnd", "appendDeviceQuery", "makeBatchUrl",
            "repaintDirtyLinesAndDecayOnce",
        ):
            generated.append(extract_function(cls.source, name))
        generated.append((ROOT / "tests/firmware_1_5_1_reference.inc").read_text())
        generated.append(HARNESS)
        cpp = work / "firmware.cpp"
        cpp.write_text("\n\n".join(generated))
        cls.binary = work / "firmware-tests"
        result = subprocess.run(
            [compiler, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
             str(cpp), "-o", str(cls.binary)], capture_output=True, text=True,
        )
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def run_native(self, check):
        result = subprocess.run([str(self.binary), check], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_percent_encoding_matches_legacy_for_all_nonzero_bytes(self):
        self.run_native("encoding")

    def test_all_station_batches_preserve_urls_and_boundaries(self):
        self.run_native("batching")

    def test_dirty_render_and_ttl_match_legacy_for_every_dirty_mask(self):
        self.run_native("render")

    def test_m1_has_all_31_physical_bindings_through_bankstown(self):
        expected = [
            "Tallawong", "Rouse Hill", "Kellyville", "Bella Vista", "Norwest",
            "Hills Showground", "Castle Hill", "Cherrybrook", "Epping",
            "Macquarie University", "Macquarie Park", "North Ryde", "Chatswood",
            "Crows Nest", "Victoria Cross", "Barangaroo", "Martin Place",
            "Gadigal", "Central", "Waterloo", "Sydenham", "Marrickville",
            "Dulwich Hill", "Hurlstone Park", "Canterbury", "Campsie", "Belmore",
            "Lakemba", "Wiley Park", "Punchbowl", "Bankstown",
        ]
        actual = [(key, led) for key, line, led in self.bindings if line == "M1"]
        self.assertEqual(actual, [(name + "M", index) for index, name in enumerate(expected)])


HOST_ADAPTERS = r'''
#include <algorithm>
#include <array>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <random>
#include <string>
#include <vector>
class String : public std::string {
public:
    using std::string::string;
};
struct Strip {
    std::vector<uint32_t> pixels, displayed;
    uint8_t brightness = 0, shownBrightness = 0;
    unsigned shows = 0;
    void setBrightness(uint8_t b) { brightness = b; }
    void clear() { std::fill(pixels.begin(), pixels.end(), 0); }
    void setPixelColor(size_t i, uint32_t c) { pixels.at(i) = c; }
    void show() { displayed = pixels; shownBrightness = brightness; ++shows; }
    uint32_t Color(uint8_t r, uint8_t g, uint8_t b) {
        return (uint32_t(r) << 16) | (uint32_t(g) << 8) | b;
    }
    bool operator==(const Strip& other) const {
        return pixels == other.pixels && displayed == other.displayed &&
            brightness == other.brightness && shownBrightness == other.shownBrightness &&
            shows == other.shows;
    }
};
'''

GLOBALS = r'''
std::array<Strip, LINE_COUNT> strips;
struct LedBinding { const char* station; Line line; uint16_t led; };
struct DeviceConfig { String boardId; } gConfig;
String gLastOtaResult = "none", gLastOtaTargetVersion;
uint8_t gEffectiveBrightness = 16;
bool gDistanceBrightnessEnabled = false;
uint8_t gDistanceLevelFar = 55, gDistanceLevelApproaching = 150;
uint8_t stateBuf[500], ttlBuf[500];
bool dirtyLine[LINE_COUNT];
'''

HARNESS = r'''
String legacyBatchUrl(size_t first, size_t end) {
    String url(kPayloadBase);
    legacyAppendDeviceQuery(url);
    url += "&only=";
    for (size_t j = first; j < end; ++j) {
        if (j > first) url += ',';
        url += legacyEnc(B[j].station);
    }
    return url;
}
int main(int argc, char** argv) {
    assert(argc == 2);
    std::string check = argv[1];
    if (check == "encoding") {
        std::string bytes;
        for (int i = 1; i <= 255; ++i) bytes += char(i);
        for (size_t first = 0; first <= bytes.size(); ++first) {
            const char* text = bytes.c_str() + first;
            assert(enc(text) == legacyEnc(text));
            String combined("prefix?");
            appendEncoded(combined, text);
            assert(combined == std::string("prefix?") + legacyEnc(text));
        }
        assert(enc("a b+c/%") == "a%20b%2Bc%2F%25");
    } else if (check == "batching") {
        for (const char* board : {"TEST1", "AB12C"}) {
            gConfig.boardId = board;
            for (const char* status : {"none", "pending", "fail_http_-1"}) {
                gLastOtaResult = status;
                for (const char* target : {"", "1.5.2", "1.4.6-bridge"}) {
                    gLastOtaTargetVersion = target;
                    size_t visited = 0, batches = 0;
                    for (size_t first = 0; first < NB;) {
                        size_t end = computeBatchEnd(first);
                        assert(end == legacyComputeBatchEnd(first));
                        assert(end > first && end <= NB);
                        auto url = makeBatchUrl(first, end);
                        assert(url == legacyBatchUrl(first, end));
                        assert(url.length() <= URL_CHAR_BUDGET);
                        if (end < NB) assert(makeBatchUrl(first, end + 1).length() > URL_CHAR_BUDGET);
                        visited += end - first;
                        first = end;
                        ++batches;
                    }
                    assert(visited == NB && batches == 3);
                }
            }
        }
    } else if (check == "render") {
        std::mt19937 random(20261024);
        for (unsigned mask = 0; mask < (1U << LINE_COUNT); ++mask) {
            for (unsigned distance = 0; distance < 2; ++distance) {
                gDistanceBrightnessEnabled = distance;
                gEffectiveBrightness = uint8_t(random());
                gDistanceLevelFar = uint8_t(random());
                gDistanceLevelApproaching = uint8_t(random());
                for (size_t ln = 0; ln < LINE_COUNT; ++ln) {
                    strips[ln] = Strip();
                    strips[ln].pixels.resize(STRIP_LEN[ln]);
                    for (auto& pixel : strips[ln].pixels) pixel = random();
                    strips[ln].show();
                    dirtyLine[ln] = (mask >> ln) & 1;
                }
                for (size_t i = 0; i < NB; ++i) {
                    stateBuf[i] = random() % 4;
                    ttlBuf[i] = random() % 4;
                }
                const auto initialStrips = strips;
                const std::vector<uint8_t> initialTtl(ttlBuf, ttlBuf + NB);
                legacyRepaintDirtyLinesAndDecayOnce();
                const auto expectedStrips = strips;
                const std::vector<uint8_t> expectedTtl(ttlBuf, ttlBuf + NB);
                strips = initialStrips;
                std::copy(initialTtl.begin(), initialTtl.end(), ttlBuf);
                for (size_t ln = 0; ln < LINE_COUNT; ++ln) dirtyLine[ln] = (mask >> ln) & 1;
                repaintDirtyLinesAndDecayOnce();
                assert(strips == expectedStrips);
                assert(std::vector<uint8_t>(ttlBuf, ttlBuf + NB) == expectedTtl);
                for (bool dirty : dirtyLine) assert(!dirty);
                for (size_t i = 0; i < NB; ++i) {
                    uint8_t expected = initialTtl[i];
                    if (stateBuf[i] != 3 && expected > 0) --expected;
                    assert(ttlBuf[i] == expected);
                }
            }
        }
    } else {
        return 2;
    }
    std::puts("PASS");
}
'''
