from pathlib import Path
import sys
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from classifier import STATUS_EMPTY_NO_CASE, STATUS_HIT_VALID, STATUS_SEALED_OR_UNCLICKABLE, BriefLink
from gui_hits import (
    DoneStore,
    HitListView,
    WHOLE_MONTH,
    assign_ny_save_as_names,
    collect_hits,
    ny_brief_camel_type,
    ny_downloads_dir,
)
from nyscef_browser import MotionPdfOption
from report_builder import CaseScanRecord, save_checkpoint


def _brief(title="APPELLANT'S BRIEF"):
    return [BriefLink(title, "https://iapps.courts.state.ny.us/nyscef/ViewDocument?docIndex=A", "4 X")]


class TestHits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.opt1 = MotionPdfOption("07-31 (a)", "u", "07-31-2026_MOTION_LIST.pdf", "2026-07", "2026-07-31")
        self.opt2 = MotionPdfOption("07-30 (b)", "u", "07-30-2026_MOTION_LIST.pdf", "2026-07", "2026-07-30")
        self.resolver = lambda o: root / o.filename.replace(".pdf", "")
        save_checkpoint(self.resolver(self.opt1), {"total_unique_cases": 3}, {
            "2025-09410": CaseScanRecord("2025-09410", STATUS_HIT_VALID, "DITECH v. ALBRECHT", briefs=_brief()),
            "2022-04406": CaseScanRecord("2022-04406", STATUS_SEALED_OR_UNCLICKABLE, "SEALED"),
            "2026-05684": CaseScanRecord("2026-05684", STATUS_EMPTY_NO_CASE),
        })
        save_checkpoint(self.resolver(self.opt2), {"total_unique_cases": 1}, {
            "2025-09410": CaseScanRecord("2025-09410", STATUS_HIT_VALID, "DITECH v. ALBRECHT", briefs=_brief("REPLY BRIEF")),
        })
        self.store = DoneStore(root / "downloaded.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_collect_only_hits_and_scope_filter(self):
        opts = [self.opt1, self.opt2]
        rows = collect_hits(opts, self.resolver)
        self.assertEqual([(r.date_tag, r.case_number) for r in rows],
                         [("2026-07-31", "2025-09410"), ("2026-07-30", "2025-09410")])
        only = collect_hits(opts, self.resolver, "2026-07-30")
        self.assertEqual([r.briefs for r in only], ["[1] REPLY BRIEF"])
        self.assertEqual([r.pdf_count for r in only], [1])

    def test_done_store_persists_and_survives_corrupt_file(self):
        self.assertTrue(self.store.toggle("k"))
        self.assertTrue(DoneStore(self.store.path).is_done("k"))
        self.assertFalse(self.store.toggle("k"))
        self.store.path.write_text("{not json", encoding="utf-8")
        self.assertFalse(DoneStore(self.store.path).is_done("k"))

    def test_view_filter_hide_done_and_copy(self):
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("No display available for Tk")
        root.withdraw()
        try:
            view = HitListView(root, lambda: ("2026-07", [self.opt1, self.opt2]), lambda: None, self.store,
                               run_dir_resolver=self.resolver)
            view.refresh()
            self.assertEqual(len(view.tree.get_children()), 2)
            view.tree.selection_set("2026-07-31::2025-09410")
            view.toggle_selected()
            self.assertTrue(self.store.is_done("2026-07-31::2025-09410"))
            view.hide_var.set(True)
            view._fill()
            self.assertEqual(len(view.tree.get_children()), 1)
            view.hide_var.set(False)
            view.filter_var.set("nomatch")
            self.assertEqual(len(view.tree.get_children()), 0)
            view.filter_var.set("")
            view.copy_all()
            # same case on two dates must be copied once
            self.assertEqual(root.clipboard_get(), "2025-09410")
            self.assertEqual(view.scope_var.get(), WHOLE_MONTH)
            items = view.all_brief_pdf_items()
            self.assertEqual(len(items), 2)
            view.select_all()
            self.assertEqual(len(view.tree.selection()), 2)
            view.toggle_all()
            self.assertTrue(all(self.store.is_done(r.key) for r in view.rows))
            view.toggle_all()
            self.assertFalse(any(self.store.is_done(r.key) for r in view.rows))

            # Checkbox selection and Select All tests
            view.toggle_check_all()
            self.assertEqual(len(view.checked_keys), 2)
            self.assertEqual(str(view.tree.heading("sel")["text"]), "☑")
            self.assertEqual(str(view.dl_all_btn["text"]), "⬇ Download Selected (2)")
            view.copy_selected()
            self.assertEqual(root.clipboard_get(), "2025-09410")
            view.toggle_check_all()
            self.assertEqual(len(view.checked_keys), 0)
            self.assertEqual(str(view.tree.heading("sel")["text"]), "☐")
            self.assertEqual(str(view.dl_all_btn["text"]), "⬇ Download All PDFs (2)")
        finally:
            root.destroy()

    def test_ny_save_as_naming_and_camel_types(self):
        titles = ["APPELLANT'S BRIEF", "RESPONDENT'S BRIEF", "APPELLANT'S BRIEF", "REPLY BRIEF"]
        names = assign_ny_save_as_names("2025-12405", titles)
        self.assertEqual(
            names,
            [
                "APPELLANT'S BRIEF",
                "RESPONDENT'S BRIEF",
                "APPELLANT'S BRIEF (2)",
                "REPLY BRIEF",
            ],
        )
        self.assertEqual(ny_brief_camel_type("PETITION FOR REVIEW"), "PetitionForReview")
        self.assertEqual(ny_brief_camel_type("WRIT OF MANDAMUS"), "WritOfMandamus")
        self.assertEqual(ny_brief_camel_type("AFFIRMATION OF SERVICE"), "AffidavitOfService")

    def test_ny_downloads_dir_and_handler_callbacks(self):
        d = ny_downloads_dir("NOVEMBER 2026", "2025-09410")
        self.assertTrue(d.name == "2025-09410")
        self.assertTrue(d.parent.name == "NOVEMBER_2026")
        self.assertTrue(d.exists())

        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("No display available for Tk")
        root.withdraw()
        try:
            download_calls = []
            folder_calls = []
            view = HitListView(
                root,
                lambda: ("NOVEMBER 2026", [self.opt1]),
                lambda: None,
                self.store,
                run_dir_resolver=self.resolver,
                download_handler=lambda items: download_calls.append(items),
                open_folder_handler=lambda ym: folder_calls.append(ym),
            )
            view.refresh()
            self.assertEqual(len(view.rows), 1)
            self.assertIn("Download All PDFs", str(view.dl_all_btn["text"]))

            # Click download all
            view.download_checked_or_all()
            self.assertEqual(len(download_calls), 1)
            self.assertEqual(len(download_calls[0]), 1)
            self.assertEqual(download_calls[0][0]["case_number"], "2025-09410")
            self.assertEqual(download_calls[0][0]["save_as_name"], "APPELLANT'S BRIEF")

            # Open folder
            view.open_downloads_folder()
            self.assertEqual(folder_calls, ["NOVEMBER 2026"])

            # Detail view selection and individual brief download
            view.tree.selection_set(view.rows[0].key)
            view._on_select()
            children = view.detail_items_frame.winfo_children()
            self.assertEqual(len(children), 1)
            # Row has compact icon download button with confirmation
            btns = [w for w in children[0].winfo_children() if isinstance(w, tk.Button)]
            save_btn = next(b for b in btns if "⬇" in str(b["text"]))
            self.assertEqual(str(save_btn["text"]), "⬇")
            # If user declines confirmation, no download
            with patch("tkinter.messagebox.askyesno", return_value=False):
                save_btn.invoke()
            self.assertEqual(len(download_calls), 1)

            # If user accepts confirmation, triggers download
            with patch("tkinter.messagebox.askyesno", return_value=True):
                save_btn.invoke()
            self.assertEqual(len(download_calls), 2)
            self.assertEqual(root.clipboard_get(), "APPELLANT'S BRIEF")

            fake_pdf = ny_downloads_dir("NOVEMBER 2026") / "2025_09410_cal_APPELLANT_S_BRIEF_1.pdf"
            if fake_pdf.exists():
                fake_pdf.unlink()

            # Test Open PDF button when file does not exist yet (informs user, does NOT auto-download)
            cur_btns = [w for w in view.detail_items_frame.winfo_children()[0].winfo_children() if isinstance(w, tk.Button)]
            open_btn = next(b for b in cur_btns if "Open" in str(b["text"]))
            open_btn.invoke()
            self.assertEqual(len(download_calls), 2)
            self.assertIn("Not downloaded yet", str(view.note["text"]))

            # Test Open PDF button when local file exists
            fake_pdf.write_bytes(b"%PDF-1.4 test")
            found = view._find_downloaded_pdf("2025-09410", "APPELLANT'S BRIEF")
            self.assertIsNotNone(found)
            self.assertEqual(found.name, fake_pdf.name)
            fake_pdf.unlink()
        finally:
            root.destroy()

    def test_ny_audit_downloads_folder(self):
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("No display available for Tk")
        root.withdraw()
        try:
            view = HitListView(root, lambda: ("2026-07", [self.opt1, self.opt2]), lambda: None, self.store,
                               run_dir_resolver=self.resolver)
            view.refresh()
            # Trigger audit with no files in download directory
            with patch.object(tk.Toplevel, "deiconify", return_value=None):
                view.audit_downloads_folder()
                # Verify audit ran without exception
                self.assertIsNotNone(view.audit_btn)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
