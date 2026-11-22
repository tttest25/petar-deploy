"""Offline contracts for the pre-provisioning Chromium archive."""

import hashlib
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("stage_chromium", Path(__file__).with_name("stage-smoke-chromium.py"))
chromium = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chromium)
ARCHIVE = b"offline chromium fixture"
PINS = {"chromium_version": "1.2.3.4", "chromium_bytes": len(ARCHIVE),
        "chromium_sha256": hashlib.sha256(ARCHIVE).hexdigest()}


class ChromiumStagingTest(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.source = Path(work.name) / "supplied.zip"
        self.output = Path(work.name) / "staged.zip"

    def test_stages_supplied_archive_without_network(self):
        self.source.write_bytes(ARCHIVE)
        with patch.object(chromium, "fetch") as fetch:
            chromium.stage(PINS, self.output, self.source)
        fetch.assert_not_called()
        self.assertEqual(self.output.read_bytes(), ARCHIVE)
        self.assertEqual(self.source.read_bytes(), ARCHIVE)

    def test_invalid_archive_leaves_no_staged_file(self):
        for data in (b"", ARCHIVE.upper()):
            with self.subTest(data=data):
                self.source.write_bytes(data)
                with self.assertRaises(ValueError):
                    chromium.stage(PINS, self.output, self.source)
                self.assertFalse(self.output.exists())

    def test_failed_download_discards_partial_archive(self):
        def interrupted(_version, _size, target):
            target.write(ARCHIVE[:4])
            raise subprocess.TimeoutExpired("curl", 300)

        with patch.object(chromium, "fetch", side_effect=interrupted):
            with self.assertRaises(subprocess.TimeoutExpired):
                chromium.stage(PINS, self.output)
        self.assertFalse(self.output.exists())

    def test_verifies_download_and_refuses_to_replace_existing_file(self):
        with patch.object(chromium, "fetch", side_effect=lambda _version, _size, target: target.write(ARCHIVE)):
            chromium.stage(PINS, self.output)
        self.assertEqual(self.output.read_bytes(), ARCHIVE)
        with self.assertRaises(FileExistsError):
            chromium.stage(PINS, self.output)
        self.assertEqual(self.output.read_bytes(), ARCHIVE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
