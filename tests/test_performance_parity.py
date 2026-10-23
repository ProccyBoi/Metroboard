import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import app as metro


class FirmwareMetadataParityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "metroboard_test.bin"
        self.path.write_bytes(b"firmware version one")
        with metro._FIRMWARE_HASH_LOCK:
            metro._FIRMWARE_HASH_CACHE.clear()
        enabled = mock.patch.object(metro, "FIRMWARE_OTA_ENABLED", True)
        enabled.start()
        self.addCleanup(enabled.stop)

    def expected_metadata(self, version="1.0.0"):
        data = self.path.read_bytes()
        return {
            "available": True,
            "version": version,
            "path": str(self.path),
            "filename": self.path.name,
            "size": len(data),
            "md5": hashlib.md5(data).hexdigest(),
            "sha256": hashlib.sha256(data).hexdigest(),
            "mtime": self.path.stat().st_mtime,
        }

    def metadata(self, version="1.0.0"):
        return metro._firmware_metadata_for_path(version, str(self.path))

    def test_unchanged_file_reuses_hashes_but_preserves_all_response_fields(self):
        expected = self.expected_metadata()
        with mock.patch("builtins.open", wraps=open) as opened:
            self.assertEqual(self.metadata(), expected)
            self.assertEqual(self.metadata(), expected)
        self.assertEqual(opened.call_count, 1)

    def test_version_and_returned_metadata_are_not_shared_between_callers(self):
        first = self.metadata()
        first["md5"] = "caller mutation"
        first["version"] = "caller version"
        self.assertEqual(self.metadata("2.0.0"), self.expected_metadata("2.0.0"))

    def test_same_size_in_place_write_with_preserved_mtime_refreshes_hashes(self):
        previous = self.metadata()
        stat = self.path.stat()
        self.path.write_bytes(b"firmware version two")
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        current = self.metadata()
        self.assertEqual(current, self.expected_metadata())
        self.assertEqual(current["size"], previous["size"])
        self.assertEqual(current["mtime"], previous["mtime"])
        self.assertNotEqual(current["sha256"], previous["sha256"])

    def test_atomic_replacement_with_same_size_and_mtime_refreshes_hashes(self):
        previous = self.metadata()
        stat = self.path.stat()
        replacement = self.path.with_suffix(".new")
        replacement.write_bytes(b"firmware version two")
        os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        replacement.replace(self.path)
        current = self.metadata()
        self.assertEqual(current, self.expected_metadata())
        self.assertEqual(current["size"], previous["size"])
        self.assertEqual(current["mtime"], previous["mtime"])
        self.assertNotEqual(current["sha256"], previous["sha256"])

    def test_missing_empty_and_recreated_files_preserve_availability(self):
        self.metadata()
        self.path.unlink()
        self.assertEqual(self.metadata(), {
            "available": False, "reason": "missing_binary", "version": "1.0.0",
            "path": str(self.path),
        })
        self.path.write_bytes(b"")
        self.assertEqual(self.metadata(), {
            "available": False, "reason": "empty_binary", "version": "1.0.0",
            "path": str(self.path),
        })
        self.path.write_bytes(b"replacement firmware")
        self.assertEqual(self.metadata(), self.expected_metadata())

    def test_disabled_ota_and_missing_version_do_not_reuse_available_metadata(self):
        self.metadata()
        with mock.patch.object(metro, "FIRMWARE_OTA_ENABLED", False):
            self.assertEqual(self.metadata(), {
                "available": False, "reason": "ota_disabled", "version": "1.0.0",
            })
        self.assertEqual(self.metadata(""), {
            "available": False, "reason": "missing_version", "version": "",
        })

    def test_file_modified_during_read_does_not_poison_later_metadata(self):
        original_open = open
        path = self.path

        class ChangingReader:
            def __enter__(self):
                self.file = original_open(path, "rb")
                self.changed = False
                return self

            def __exit__(self, *args):
                self.file.close()

            def fileno(self):
                return self.file.fileno()

            def read(self, size):
                data = self.file.read(size)
                if data and not self.changed:
                    self.changed = True
                    path.write_bytes(b"firmware version two")
                return data

        with mock.patch("builtins.open", return_value=ChangingReader()):
            self.metadata()
        self.assertEqual(self.metadata(), self.expected_metadata())


class LayoutKeyParityTests(unittest.TestCase):
    @staticmethod
    def keys_from_rendered_layout():
        return {
            station["key"]
            for line in metro._board_layout_model()["lines"]
            for station in line["stations"]
        }

    def test_repository_sequences_match_rendered_layout(self):
        expected = self.keys_from_rendered_layout()
        self.assertTrue(expected)
        with mock.patch.object(metro, "_board_layout_model", side_effect=AssertionError("geometry not needed")):
            self.assertEqual(metro._board_layout_keys(), expected)

    def test_segment_slicing_duplicates_and_unrendered_segments_match_layout(self):
        sequences = {"FIRST": ["Alpha", "Beta"], "SECOND": ["Gamma", "Delta"]}
        specs = [{
            "line": "M1", "suffix": "M", "color": "#26C6DA",
            "segments": [
                {"array_names": ["FIRST", "SECOND"], "start": "Beta", "end": "Gamma", "path": [[0, 0], [1, 1]]},
                {"array_names": ["FIRST"], "path": [[0, 0], [1, 1]]},
                {"array_names": ["SECOND"], "path": []},
                {"array_names": ["MISSING"], "path": [[0, 0], [1, 1]]},
                {"array_names": ["SECOND"], "start": "Delta", "end": "Gamma", "path": [[0, 0], [1, 1]]},
            ],
        }]
        with (
            mock.patch.object(metro, "_load_firmware_sequences", return_value=sequences),
            mock.patch.object(metro, "BOARD_LINE_SPECS", specs),
        ):
            self.assertEqual(metro._board_layout_keys(), {"AlphaM", "BetaM", "GammaM"})
            self.assertEqual(metro._board_layout_keys(), self.keys_from_rendered_layout())
            sequences["FIRST"].append("New Station")
            self.assertEqual(metro._board_layout_keys(), self.keys_from_rendered_layout())
            self.assertIn("New StationM", metro._board_layout_keys())


if __name__ == "__main__":
    unittest.main()
