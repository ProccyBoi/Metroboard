#include <Arduino.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <Update.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <esp_wifi.h>

static const char* kPayloadBase =
    "https://damp-catlin-metroboard-7be2a3b3.koyeb.app/board_payload";
static const char* kFirmwareVersion = "1.4.6-bridge";
static const char* kBuildLabel = "bridge";
static const uint32_t OTA_STARTUP_DELAY_MS = 10000;
static const uint32_t OTA_CHECK_MS = 30000;

struct DeviceConfig {
    String ssid;
    String pass;
    String boardId;

    bool isValid() const { return ssid.length() > 0 && boardId.length() > 0; }
};

Preferences gPrefs;
DeviceConfig gConfig;
String gLastOtaResult = "none";
String gLastOtaTargetVersion;
uint32_t gLastOtaCheck = 0;
bool gOtaInProgress = false;

static String enc(const char* s) {
    const char* hex = "0123456789ABCDEF";
    String out;
    while (*s) {
        char c = *s++;
        if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
            (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' ||
            c == '~') {
            out += c;
        } else {
            out += '%';
            out += hex[(c >> 4) & 0xF];
            out += hex[c & 0xF];
        }
    }
    return out;
}

static String serviceBaseUrl() {
    String url = String(kPayloadBase);
    int slash = url.lastIndexOf('/');
    if (slash > 0) return url.substring(0, slash);
    return url;
}

static bool readVersionPart(const char*& p, int& out) {
    while (*p && (*p < '0' || *p > '9')) p++;
    if (!*p) return false;
    long value = 0;
    while (*p >= '0' && *p <= '9') {
        value = value * 10 + (*p - '0');
        if (value > 32767) value = 32767;
        p++;
    }
    out = (int)value;
    return true;
}

static bool versionIsNewer(const char* candidate, const char* current) {
    if (!candidate || !*candidate) return false;
    const char* a = candidate;
    const char* b = current && *current ? current : "0";
    for (uint8_t i = 0; i < 6; i++) {
        int av = 0, bv = 0;
        bool hasA = readVersionPart(a, av);
        bool hasB = readVersionPart(b, bv);
        if (!hasA && !hasB) return false;
        if (av != bv) return av > bv;
    }
    return false;
}

static bool isHexMd5(const char* md5) {
    if (!md5 || strlen(md5) != 32) return false;
    for (uint8_t i = 0; i < 32; i++) {
        char c = md5[i];
        bool hex = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') ||
                   (c >= 'A' && c <= 'F');
        if (!hex) return false;
    }
    return true;
}

static bool firmwareUrlIsTrusted(const char* firmwareUrl) {
    if (!firmwareUrl || !*firmwareUrl) return false;
    String prefix = serviceBaseUrl();
    prefix += "/firmware.bin?";
    return String(firmwareUrl).startsWith(prefix);
}

static void reportStatus() {
    if (WiFi.status() != WL_CONNECTED || !gConfig.isValid()) return;

    String url = String(kPayloadBase);
    url += "?board_id=";
    url += enc(gConfig.boardId.c_str());
    url += "&fw=";
    url += enc(kFirmwareVersion);
    url += "&build=";
    url += enc(kBuildLabel);
    url += "&ota=";
    url += enc(gLastOtaResult.c_str());
    url += "&ota_target=";
    url += enc(gLastOtaTargetVersion.c_str());
    url += "&only=__settings__";

    WiFiClientSecure client;
    client.setInsecure();
    client.setTimeout(8000);
    client.setHandshakeTimeout(8000);

    HTTPClient http;
    http.setTimeout(8000);
    http.setReuse(false);
    if (!http.begin(client, url)) {
        http.end();
        return;
    }
    http.addHeader("Accept", "application/json");
    http.addHeader("Accept-Encoding", "identity");
    http.addHeader("Cache-Control", "no-cache");
    http.addHeader("Connection", "close");
    http.addHeader("User-Agent", "ESP32-Metroboard/bridge");
    int code = http.GET();
    Serial.printf("[BRIDGE] status HTTP %d\n", code);
    http.end();
}

static bool loadConfigFromPrefs() {
    gPrefs.begin("metroboard", true);
    gConfig.ssid = gPrefs.getString("ssid", "");
    gConfig.pass = gPrefs.getString("pass", "");
    gConfig.boardId = gPrefs.getString("board", "");
    gPrefs.end();
    return gConfig.isValid();
}

static void applyWifiRadioTuning() {
    WiFi.setSleep(false);
    WiFi.setTxPower(WIFI_POWER_19_5dBm);
    esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20);
}

static bool wifiConnect(uint32_t timeoutMs) {
    if (!gConfig.isValid()) return false;
    WiFi.mode(WIFI_STA);
    applyWifiRadioTuning();
    if (gConfig.pass.length() > 0) {
        WiFi.begin(gConfig.ssid.c_str(), gConfig.pass.c_str());
    } else {
        WiFi.begin(gConfig.ssid.c_str());
    }
    Serial.printf("[BRIDGE] WiFi \"%s\"", gConfig.ssid.c_str());
    uint32_t start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < timeoutMs) {
        Serial.print('.');
        delay(250);
        yield();
    }
    if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("\n[BRIDGE] IP=%s heap=%u\n",
                      WiFi.localIP().toString().c_str(), ESP.getFreeHeap());
        return true;
    }
    Serial.println("\n[BRIDGE] WiFi connect failed");
    return false;
}

static void ensureWifi() {
    if (WiFi.status() == WL_CONNECTED) return;
    WiFi.disconnect(true, true);
    delay(100);
    wifiConnect(15000);
}

static bool performOtaUpdate(const char* firmwareUrl, const char* expectedMd5,
                             int expectedSize, const char* targetVersion) {
    if (!firmwareUrl || !*firmwareUrl || WiFi.status() != WL_CONNECTED)
        return false;

    gOtaInProgress = true;
    gLastOtaTargetVersion = String(targetVersion ? targetVersion : "");
    gLastOtaResult = "in_progress";
    reportStatus();
    Serial.printf("[BRIDGE] OTA start target=%s\n",
                  gLastOtaTargetVersion.c_str());

    WiFiClientSecure client;
    client.setInsecure();
    client.setTimeout(30000);
    client.setHandshakeTimeout(30000);

    HTTPClient http;
    http.setTimeout(30000);
    http.setReuse(false);
    if (!http.begin(client, firmwareUrl)) {
        gLastOtaResult = "fail_http_begin";
        http.end();
        gOtaInProgress = false;
        reportStatus();
        return false;
    }
    http.addHeader("Accept", "application/octet-stream");
    http.addHeader("Accept-Encoding", "identity");
    http.addHeader("Cache-Control", "no-cache");
    http.addHeader("Connection", "close");
    http.addHeader("User-Agent", "ESP32-Metroboard/bridge-ota");

    int code = http.GET();
    if (code != HTTP_CODE_OK) {
        gLastOtaResult = String("fail_http_") + code;
        http.end();
        gOtaInProgress = false;
        reportStatus();
        return false;
    }

    int contentLength = http.getSize();
    int updateSize = contentLength > 0 ? contentLength : expectedSize;
    if (expectedSize > 0 && contentLength > 0 && expectedSize != contentLength) {
        gLastOtaResult = "fail_size_mismatch";
        http.end();
        gOtaInProgress = false;
        reportStatus();
        return false;
    }
    if (updateSize <= 0) {
        gLastOtaResult = "fail_no_size";
        http.end();
        gOtaInProgress = false;
        reportStatus();
        return false;
    }
    if ((uint32_t)updateSize > ESP.getFreeSketchSpace()) {
        gLastOtaResult = "fail_too_large";
        http.end();
        gOtaInProgress = false;
        reportStatus();
        return false;
    }
    if (!Update.begin((size_t)updateSize)) {
        gLastOtaResult = String("fail_update_begin_") + Update.getError();
        http.end();
        gOtaInProgress = false;
        reportStatus();
        return false;
    }
    if (expectedMd5 && strlen(expectedMd5) == 32 && !Update.setMD5(expectedMd5)) {
        gLastOtaResult = "fail_md5_invalid";
        Update.abort();
        http.end();
        gOtaInProgress = false;
        reportStatus();
        return false;
    }

    WiFiClient* stream = http.getStreamPtr();
    uint8_t buffer[1024];
    size_t written = 0;
    uint32_t lastDataAt = millis();
    while (written < (size_t)updateSize) {
        int available = stream->available();
        if (available <= 0) {
            if (millis() - lastDataAt > 30000) {
                gLastOtaResult = "fail_download_stalled";
                Update.abort();
                http.end();
                gOtaInProgress = false;
                reportStatus();
                return false;
            }
            delay(1);
            yield();
            continue;
        }

        size_t want = min((size_t)available, sizeof(buffer));
        want = min(want, (size_t)updateSize - written);
        int got = stream->readBytes(buffer, want);
        if (got <= 0) {
            delay(1);
            yield();
            continue;
        }
        lastDataAt = millis();
        size_t chunkWritten = Update.write(buffer, (size_t)got);
        if (chunkWritten != (size_t)got) {
            gLastOtaResult = String("fail_write_") + Update.getError();
            Update.abort();
            http.end();
            gOtaInProgress = false;
            reportStatus();
            return false;
        }
        written += chunkWritten;
        delay(1);
        yield();
    }

    bool ok = Update.end(true);
    http.end();
    if (!ok) {
        gLastOtaResult = String("fail_update_end_") + Update.getError();
        gOtaInProgress = false;
        reportStatus();
        return false;
    }
    if (!Update.isFinished()) {
        gLastOtaResult = "fail_update_incomplete";
        gOtaInProgress = false;
        reportStatus();
        return false;
    }

    gLastOtaResult = "done_reboot";
    reportStatus();
    delay(500);
    ESP.restart();
    return true;
}

static bool checkForOtaUpdate() {
    if (gOtaInProgress || WiFi.status() != WL_CONNECTED || !gConfig.isValid())
        return false;

    String url = serviceBaseUrl();
    url += "/firmware_manifest?board_id=";
    url += enc(gConfig.boardId.c_str());
    url += "&current=";
    url += enc(kFirmwareVersion);
    url += "&build=";
    url += enc(kBuildLabel);

    WiFiClientSecure client;
    client.setInsecure();
    client.setTimeout(20000);
    client.setHandshakeTimeout(20000);

    HTTPClient http;
    http.setTimeout(20000);
    http.setReuse(false);
    Serial.printf("[BRIDGE] manifest %s\n", url.c_str());
    if (!http.begin(client, url)) {
        gLastOtaResult = "fail_manifest_begin";
        http.end();
        reportStatus();
        return false;
    }
    http.addHeader("Accept", "application/json");
    http.addHeader("Accept-Encoding", "identity");
    http.addHeader("Cache-Control", "no-cache");
    http.addHeader("Connection", "close");
    http.addHeader("User-Agent", "ESP32-Metroboard/bridge");

    int code = http.GET();
    if (code != HTTP_CODE_OK) {
        gLastOtaResult = String("fail_manifest_http_") + code;
        http.end();
        reportStatus();
        return false;
    }

    String body = http.getString();
    http.end();
    body.trim();

    StaticJsonDocument<1536> doc;
    DeserializationError err = deserializeJson(doc, body);
    if (err) {
        gLastOtaResult = "fail_manifest_json";
        reportStatus();
        return false;
    }

    bool otaEnabled = doc["ota_enabled"] | true;
    bool updateAvailable = doc["update_available"] | false;
    const char* version = doc["version"] | "";
    const char* firmwareUrl = doc["url"] | "";
    const char* md5 = doc["md5"] | "";
    int size = doc["size"] | 0;
    gLastOtaTargetVersion = String(version ? version : "");

    if (!otaEnabled || !updateAvailable) {
        gLastOtaResult = otaEnabled ? "no_update" : "disabled";
        reportStatus();
        return false;
    }
    if (!versionIsNewer(version, kFirmwareVersion)) {
        gLastOtaResult = "refuse_not_newer";
        reportStatus();
        return false;
    }
    if (!firmwareUrlIsTrusted(firmwareUrl)) {
        gLastOtaResult = "refuse_untrusted_url";
        reportStatus();
        return false;
    }
    if (!isHexMd5(md5)) {
        gLastOtaResult = "refuse_bad_md5";
        reportStatus();
        return false;
    }
    if (size <= 0) {
        gLastOtaResult = "refuse_bad_size";
        reportStatus();
        return false;
    }

    return performOtaUpdate(firmwareUrl, md5, size, version);
}

void setup() {
    Serial.begin(115200);
    delay(150);
    Serial.println("\n[BRIDGE] Metroboard OTA bridge boot");
    if (!loadConfigFromPrefs()) {
        Serial.println("[BRIDGE] Missing stored WiFi or board ID.");
        return;
    }
    Serial.printf("[BRIDGE] board=%s version=%s\n", gConfig.boardId.c_str(),
                  kFirmwareVersion);
    wifiConnect(60000);
    reportStatus();
}

void loop() {
    ensureWifi();
    uint32_t now = millis();
    if (WiFi.status() == WL_CONNECTED && now > OTA_STARTUP_DELAY_MS &&
        (gLastOtaCheck == 0 || now - gLastOtaCheck >= OTA_CHECK_MS)) {
        gLastOtaCheck = now;
        checkForOtaUpdate();
    }
    delay(50);
}
