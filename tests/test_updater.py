import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from updater import (
    cleanup_old_version,
    download_update_binary,
    is_valid_windows_executable,
)


class TestUpdater(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_is_valid_windows_executable_checks_pe_and_size(self):
        small_file = self.dir_path / "small.exe"
        small_file.write_bytes(b"MZ" + b"\x00" * 100)
        self.assertFalse(is_valid_windows_executable(small_file))

        non_pe_file = self.dir_path / "fake.exe"
        non_pe_file.write_bytes(b"PK" + b"\x00" * (16 * 1024 * 1024))
        self.assertFalse(is_valid_windows_executable(non_pe_file))

        valid_file = self.dir_path / "valid.exe"
        valid_file.write_bytes(b"MZ" + b"\x00" * (16 * 1024 * 1024))
        self.assertTrue(is_valid_windows_executable(valid_file))

    def test_cleanup_old_version_removes_old_binary_when_frozen(self):
        fake_exe = self.dir_path / "BPM_Court_Scanner.exe"
        fake_exe.write_bytes(b"MZtest")
        old_exe = self.dir_path / "BPM_Court_Scanner.old.exe"
        old_exe.write_bytes(b"MZold")

        with patch("sys.frozen", True, create=True), patch("sys.executable", str(fake_exe)):
            self.assertTrue(old_exe.exists())
            cleanup_old_version()
            self.assertFalse(old_exe.exists())

    @patch("urllib.request.urlopen")
    def test_download_update_binary_validates_and_renames(self, mock_urlopen):
        fake_data = b"MZ" + b"\x00" * (16 * 1024 * 1024)
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Length": str(len(fake_data))}
        mock_resp.read.side_effect = [fake_data[:1024 * 1024], fake_data[1024 * 1024:], b""]
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        target_file = self.dir_path / "BPM_Court_Scanner.new.exe"
        progress = []

        success = download_update_binary(
            "http://example.com/test.exe",
            target_file,
            progress_callback=lambda cur, tot: progress.append((cur, tot)),
        )

        self.assertTrue(success)
        self.assertTrue(target_file.exists())
        self.assertGreater(len(progress), 0)


if __name__ == "__main__":
    unittest.main()

