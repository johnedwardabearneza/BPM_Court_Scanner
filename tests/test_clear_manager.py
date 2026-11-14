import json
from pathlib import Path
import queue
import shutil
import tempfile
import tkinter as tk
import unittest
from unittest.mock import MagicMock, patch

import clear_manager


class TestClearManager(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="bpm_test_clear_"))
        self.output_patch = patch.object(clear_manager, "OUTPUT_DIR", self.test_dir)
        self.output_patch.start()

        self.dl_dir = self.test_dir / "downloads"
        self.dl_patch = patch.object(clear_manager, "DOWNLOADS_DIR", self.dl_dir)
        self.dl_patch.start()
        self.ny_dl_patch = patch.object(clear_manager, "DOWNLOADS_NY_DIR", self.dl_dir / "New York")
        self.ny_dl_patch.start()
        self.ky_dl_patch = patch.object(clear_manager, "DOWNLOADS_KY_DIR", self.dl_dir / "Kentucky")
        self.ky_dl_patch.start()

    def tearDown(self):
        self.ky_dl_patch.stop()
        self.ny_dl_patch.stop()
        self.dl_patch.stop()
        self.output_patch.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_clear_ny_month(self):
        # Create dummy month folder and date folders
        m_dir = self.test_dir / "_MONTH_2026-11"
        m_dir.mkdir(parents=True)
        (m_dir / "report.html").write_text("dummy", encoding="utf-8")

        d1 = self.test_dir / "11-06-2026_MOTION_LIST"
        d1.mkdir()
        (d1 / "checkpoint.json").write_text("{}", encoding="utf-8")

        d_other = self.test_dir / "07-06-2026_MOTION_LIST"
        d_other.mkdir()
        (d_other / "checkpoint.json").write_text("{}", encoding="utf-8")

        # Downloads (both legacy and new organized structure)
        dl_dir = self.test_dir / "downloads_ny" / "2026-11"
        dl_dir.mkdir(parents=True)
        (dl_dir / "case_brief.pdf").write_bytes(b"dummy pdf content")

        new_ny_dl = self.dl_dir / "New York" / "NOVEMBER_2026"
        new_ny_dl.mkdir(parents=True)
        (new_ny_dl / "case_brief_new.pdf").write_bytes(b"dummy new pdf content")

        # downloaded.json
        dl_json = self.test_dir / "downloaded.json"
        dl_json.write_text(json.dumps({
            "2026-11-06::2025-001": True,
            "2026-07-06::2025-002": True
        }), encoding="utf-8")

        stats = clear_manager.clear_ny_month("2026-11", delete_pdfs=True)
        self.assertFalse(m_dir.exists())
        self.assertFalse(d1.exists())
        self.assertTrue(d_other.exists())  # July should remain untouched
        self.assertFalse(dl_dir.exists())
        self.assertFalse(new_ny_dl.exists())

        # Verify downloaded.json retained July
        data = json.loads(dl_json.read_text(encoding="utf-8"))
        self.assertNotIn("2026-11-06::2025-001", data)
        self.assertIn("2026-07-06::2025-002", data)
        self.assertGreater(stats["files"], 0)

    def test_clear_ky_month_with_pdfs(self):
        ky_dir = self.test_dir / "KY_KYCOA_2026-11"
        ky_dir.mkdir(parents=True)
        (ky_dir / "checkpoint.json").write_text("{}", encoding="utf-8")
        dl = ky_dir / "downloads"
        dl.mkdir()
        (dl / "brief.pdf").write_bytes(b"pdf data")

        new_ky_dl = self.dl_dir / "Kentucky" / "KYCOA_NOVEMBER_2026"
        new_ky_dl.mkdir(parents=True)
        (new_ky_dl / "brief_new.pdf").write_bytes(b"new pdf data")

        stats = clear_manager.clear_ky_month("KYCOA", "2026-11", delete_pdfs=True)
        self.assertFalse(ky_dir.exists())
        self.assertFalse(new_ky_dl.exists())
        self.assertGreater(stats["files"], 0)

    def test_clear_ky_month_keep_pdfs(self):
        ky_dir = self.test_dir / "KY_KYCOA_2026-11"
        ky_dir.mkdir(parents=True)
        (ky_dir / "checkpoint.json").write_text("{}", encoding="utf-8")
        dl = ky_dir / "downloads"
        dl.mkdir()
        (dl / "brief.pdf").write_bytes(b"pdf data")

        new_ky_dl = self.dl_dir / "Kentucky" / "KYCOA_NOVEMBER_2026"
        new_ky_dl.mkdir(parents=True)
        (new_ky_dl / "brief_new.pdf").write_bytes(b"new pdf data")

        stats = clear_manager.clear_ky_month("KYCOA", "2026-11", delete_pdfs=False)
        self.assertTrue(ky_dir.exists())
        self.assertFalse((ky_dir / "checkpoint.json").exists())
        self.assertTrue((dl / "brief.pdf").exists())
        self.assertTrue((new_ky_dl / "brief_new.pdf").exists())

    def test_clear_factory_reset(self):
        (self.test_dir / "KY_KYCOA_2026-11").mkdir(parents=True)
        (self.test_dir / "11-06-2026_MOTION_LIST").mkdir(parents=True)
        (self.test_dir / "_MONTH_2026-11").mkdir(parents=True)
        (self.test_dir / "downloads_ny").mkdir(parents=True)
        (self.test_dir / "downloaded.json").write_text("{}", encoding="utf-8")
        (self.dl_dir / "New York").mkdir(parents=True)
        (self.dl_dir / "Kentucky").mkdir(parents=True)

        clear_manager.clear_factory_reset(delete_pdfs=True)
        self.assertFalse((self.test_dir / "KY_KYCOA_2026-11").exists())
        self.assertFalse((self.test_dir / "11-06-2026_MOTION_LIST").exists())
        self.assertFalse((self.test_dir / "_MONTH_2026-11").exists())
        self.assertFalse((self.test_dir / "downloads_ny").exists())
        self.assertFalse(self.dl_dir.exists())

    def test_clear_reset_modal_ui(self):
        root = tk.Tk()
        try:
            mock_app = MagicMock()
            mock_app.root = root
            mock_app.jurisdiction = "NY"
            mock_app.active_month_key.return_value = "2026-11"
            mock_app.month_var.get.return_value = "NOVEMBER 2026"
            mock_app.ky_frame = None

            dlg = clear_manager.ClearResetModal(root, mock_app, default_scope="month")
            self.assertEqual(dlg.scope_var.get(), "month")
            self.assertTrue(dlg.delete_pdfs_var.get())
            dlg.destroy()
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
