import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import config
from gui_hits import HitListView, ny_downloads_dir, ny_motion_lists_dir
from gui_ky import KyScannerFrame
from ky_scanner import KyCollectibleDoc, ky_downloads_dir


class TestDownloadPaths(unittest.TestCase):
    def test_default_config_paths(self):
        self.assertEqual(config.DOWNLOADS_NY_DIR, config.DOWNLOADS_DIR / "New York")
        self.assertEqual(config.DOWNLOADS_KY_DIR, config.DOWNLOADS_DIR / "Kentucky")

    def test_ny_downloads_dir_structure(self):
        ny_dir = ny_downloads_dir("NOVEMBER 2026")
        self.assertEqual(ny_dir.name, "NOVEMBER_2026")
        self.assertEqual(ny_dir.parent.name, "New York")
        self.assertEqual(ny_dir.parent.parent.name, "downloads")

    def test_ny_downloads_day_dir_structure(self):
        day_dir = ny_downloads_dir("NOVEMBER 2026", date_str="NOVEMBER 9, 2026")
        self.assertEqual(day_dir.name, "NOVEMBER 9, 2026")
        self.assertEqual(day_dir.parent.name, "NOVEMBER_2026")
        self.assertEqual(day_dir.parent.parent.name, "New York")
        self.assertTrue(day_dir.exists())

    def test_ny_motion_lists_dir_structure(self):
        ml_dir = ny_motion_lists_dir("NOVEMBER 2026")
        self.assertEqual(ml_dir.name, "Motion Lists")
        self.assertEqual(ml_dir.parent.name, "NOVEMBER_2026")
        self.assertEqual(ml_dir.parent.parent.name, "New York")
        self.assertEqual(ml_dir.parent.parent.parent.name, "downloads")
        self.assertTrue(ml_dir.exists())

    def test_ky_downloads_dir_structure(self):
        ky_dir = ky_downloads_dir("KYCOA", "2026-11")
        self.assertEqual(ky_dir.name, "KYCOA_NOVEMBER_2026")
        self.assertEqual(ky_dir.parent.name, "Kentucky")
        self.assertEqual(ky_dir.parent.parent.name, "downloads")

    def test_ky_downloads_day_dir_structure(self):
        day_dir = ky_downloads_dir("KYCOA", "2026-11", date_str="NOVEMBER 10, 2026")
        self.assertEqual(day_dir.name, "NOVEMBER 10, 2026")
        self.assertEqual(day_dir.parent.name, "KYCOA_NOVEMBER_2026")
        self.assertEqual(day_dir.parent.parent.name, "Kentucky")
        self.assertTrue(day_dir.exists())

    def test_legacy_ny_download_fallback_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            legacy_dir = tmp_path / "downloads_ny" / "NOVEMBER_2026"
            legacy_dir.mkdir(parents=True)
            fake_pdf = legacy_dir / "2025_09410_cal_APPELLANT_S_BRIEF_1.pdf"
            fake_pdf.write_bytes(b"%PDF-1.4 dummy")

            with mock.patch("gui_hits.OUTPUT_DIR", tmp_path):
                mock_view = mock.MagicMock()
                mock_view.get_context = mock.MagicMock(return_value=("NOVEMBER 2026", []))
                found = HitListView._find_downloaded_pdf(mock_view, "2025-09410", "APPELLANT'S BRIEF")
                self.assertIsNotNone(found)
                self.assertEqual(found.name, fake_pdf.name)

    def test_legacy_ky_download_fallback_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            legacy_dir = tmp_path / "KY_KYCOA_2026-11" / "downloads"
            legacy_dir.mkdir(parents=True)
            fake_pdf = legacy_dir / "KYCOA_2026-CA-0236_AppellantBrief.pdf"
            fake_pdf.write_bytes(b"%PDF-1.4 dummy brief content that exceeds one hundred bytes limit for valid pdf files")

            frame = mock.MagicMock(spec=KyScannerFrame)
            frame.active_court_code.return_value = "KYCOA"
            frame.active_year_month.return_value = "2026-11"

            doc = KyCollectibleDoc(
                document_id="doc1",
                document_name="APPELLANT BRIEF FILED",
                docket_entry_id="de1",
                filed_date="11/04/2026",
                sub_type="APPELLANT BRIEF FILED",
                submitter="Test Submitter",
                save_as_name="KYCOA_2026-CA-0236_AppellantBrief",
            )

            with mock.patch("gui_ky.OUTPUT_DIR", tmp_path):
                found = KyScannerFrame._find_ky_downloaded_pdf(frame, "2026-CA-0236", doc)
                self.assertIsNotNone(found)
                self.assertEqual(found.name, fake_pdf.name)


if __name__ == "__main__":
    unittest.main()
