from pathlib import Path
import queue
import sys
import tempfile
import tkinter as tk
import unittest
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gui import App, format_progress
from gui_worker import QueueWriter
from monthly_report import OptionProgress
from nyscef_browser import MotionPdfOption


class TestFormatProgress(unittest.TestCase):
    def test_done_partial_pending(self):
        self.assertEqual(format_progress(OptionProgress(55, 55, 2, 0, True, "", 4)), ("Done  55/55", "2 cases (4 PDFs)", "done"))
        self.assertEqual(format_progress(OptionProgress(55, 55, 2, 0, True, "")), ("Done  55/55", "2", "done"))
        text, hits, tag = format_progress(OptionProgress(49, 10, 1, 2, False, ""))
        self.assertEqual((tag, hits), ("partial", "1"))
        self.assertIn("2 to recheck", text)
        self.assertEqual(format_progress(OptionProgress(0, 0, 0, 0, False, "[PENDING]")), ("Not scanned", "", "pending"))


class TestQueueWriter(unittest.TestCase):
    def test_splits_lines_and_strips_bell(self):
        q: queue.Queue = queue.Queue()
        w = QueueWriter(q)
        w.write("\a\n[USER ACTION REQUIRED] x\npartial")
        w.write(" line\n")
        got = []
        while not q.empty():
            got.append(q.get()[1])
        self.assertEqual(got, ["", "[USER ACTION REQUIRED] x", "partial line"])


class TestAppEvents(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError:
            self.skipTest("No display available for Tk")
        self.root.withdraw()
        self.events: queue.Queue = queue.Queue()
        self.worker = SimpleNamespace(commands=queue.Queue())
        self.app = App(self.root, self.events, self.worker)

    def tearDown(self):
        if getattr(self.app, "_after_id", None):
            try:
                self.root.after_cancel(self.app._after_id)
            except Exception:
                pass
        self.root.destroy()

    def _calendar(self):
        opts = [
            MotionPdfOption("07-31-2026 (07-31-2026_MOTION_LIST.pdf)", "https://ex/1.pdf", "07-31-2026_MOTION_LIST.pdf", "2026-07", "2026-07-31"),
            MotionPdfOption("07-30-2026 (07-30-2026_MOTION_LIST.pdf)", "https://ex/2.pdf", "07-30-2026_MOTION_LIST.pdf", "2026-07", "2026-07-30"),
        ]
        self.events.put(("calendar", {"2026-07": opts, "2026-06": []}))
        self.app._poll()

    def test_calendar_populates_table_and_enables_buttons(self):
        self.assertIn("Start", str(self.app.scan_btn["text"]))
        self.assertEqual(str(self.app.all_btn["text"]), "Start All Remaining")
        self.assertEqual(str(self.app.scan_btn["state"]), "disabled")
        self._calendar()
        self.assertEqual(len(self.app.tree.get_children()), 2)
        self.assertEqual(self.app.tree.item("0", "values")[0], "JULY 31, 2026")
        self.assertEqual(str(self.app.scan_btn["state"]), "normal")
        self.assertEqual(self.app.month_var.get(), "JULY 2026")
        self.assertEqual(self.app.active_month_key(), "2026-07")
        self.assertTrue(len(self.app.tree.selection()) >= 1)

    def test_scan_selected_sends_command_and_busy_locks_buttons(self):
        self._calendar()
        self.app.tree.selection_set("1")
        self.app.scan_selected()
        cmd = self.worker.commands.get_nowait()
        self.assertEqual(cmd[0], "scan")
        self.assertEqual([o.date_iso for o in cmd[1]], ["2026-07-30"])
        self.events.put(("busy", None))
        self.app._poll()
        self.assertEqual(str(self.app.scan_btn["state"]), "disabled")
        self.assertEqual(str(self.app.stop_btn["state"]), "normal")

    def test_case_lines_update_progress_and_captcha_banner(self):
        self._calendar()
        self.events.put(("log", "[USER ACTION REQUIRED] hCaptcha/Verification detected at 'x'."))
        self.app._poll()
        self.assertTrue(self.app.banner.winfo_manager())
        self.events.put(("log", "[01/04] [HIT ] 2025-05679 | THE BANK OF NY | APPELLANT'S BRIEF, REPLY BRIEF"))
        self.events.put(("log", "[02/04] [EMPT] 2026-06036 | Peo v Antonucci, Ashley | No matching docket"))
        self.events.put(("log", "[03/04] [SEAL] 2022-04406 | VICTORIA NACLERIO v. VINCENT NACLERIO | Sealed"))
        self.app._poll()
        self.assertFalse(self.app.banner.winfo_manager())
        self.assertEqual(self.app.run_hits, 1)
        self.assertAlmostEqual(float(self.app.bar["value"]), 75.0)
        log_txt = self.app.log.get("1.0", "end")
        self.assertIn("[01/04] HIT (HAS BRIEF)", log_txt)
        self.assertIn("2025-05679", log_txt)
        self.assertIn("[02/04] SKIP (NO RECORD)", log_txt)
        self.assertIn("Peo v Antonucci, Ashley", log_txt)
        self.assertIn("[03/04] SKIP (SEALED)", log_txt)
        self.assertIn("VICTORIA NACLERIO v. VINCENT NACLERIO", log_txt)

    def test_ny_download_briefs_command_and_events(self):
        self._calendar()
        test_items = [
            {"key": "2026-07-31::2025-09410", "case_number": "2025-09410", "title": "BRIEF", "url": "https://x", "save_path": "x.pdf"}
        ]
        self.app.download_ny_briefs(test_items)
        cmd = self.worker.commands.get_nowait()
        self.assertEqual(cmd[0], "download_briefs")
        self.assertEqual(cmd[1], test_items)
        self.assertEqual(self.app.active_action, "download")

        # Busy state during download
        self.events.put(("busy", None))
        self.app._poll()
        self.assertEqual(str(self.app.hits.dl_all_btn["text"]), "⏳ Downloading...")
        self.assertEqual(str(self.app.hits.dl_all_btn["state"]), "disabled")

        # Progress event
        self.events.put(("dl_prog", (1, 2, "Downloading 1/2: doc.pdf...")))
        self.app._poll()
        self.assertEqual(float(self.app.bar["value"]), 50.0)
        self.assertIn("Downloading 1/2", self.app.prog_lbl["text"])
        self.assertIn("Downloading 1/2", self.app.hits.note["text"])
        self.assertIn("Downloading (1/2)", self.app.hits.dl_all_btn["text"])

        # Case done event
        self.events.put(("ny_case_done", "2026-07-31::2025-09410"))
        self.app._poll()
        self.assertTrue(self.app.hits.store.is_done("2026-07-31::2025-09410"))

        # Download completed & idle
        self.events.put(("dl_done", (1, 1, "output/downloads_ny")))
        self.app._poll()
        self.assertIn("Downloaded 1/1", self.app.hits.note["text"])
        self.assertIn("⬇ Download All PDFs", str(self.app.hits.dl_all_btn["text"]))

        self.events.put(("idle", None))
        self.app._poll()
        self.assertFalse(self.app.busy)
        self.assertIsNone(self.app.active_action)
        self.assertIn("Download complete", self.app.prog_lbl["text"])


class TestCachedScanOutput(unittest.TestCase):
    def test_already_scanned_date_still_prints_all_cases_and_summary(self):
        import io
        from contextlib import redirect_stdout
        from classifier import STATUS_EMPTY_NO_CASE, STATUS_HIT_VALID
        from main import _scan_single_parsed
        from pdf_parser import ParsedMotionList
        from report_builder import CaseScanRecord, save_checkpoint

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            parsed = ParsedMotionList(
                source_name="07-31-2026_MOTION_LIST.pdf",
                motion_date_hint="July 31, 2026",
                page_count=1,
                raw_match_count=2,
                cases=["2025-09410", "2026-06036"],
                case_titles={"2025-09410": "Ditech Financial LLC v Albrecht", "2026-06036": "Peo v Antonucci, Ashley"},
            )
            save_checkpoint(
                run_dir,
                {"source_name": parsed.source_name},
                {
                    "2025-09410": CaseScanRecord("2025-09410", STATUS_HIT_VALID, caption="DITECH FINANCIAL LLC v. ALBRECHT"),
                    "2026-06036": CaseScanRecord("2026-06036", STATUS_EMPTY_NO_CASE),
                },
            )
            args = SimpleNamespace(limit=0, fresh=False, min_delay=0.0, max_delay=0.0)
            buf = io.StringIO()
            with redirect_stdout(buf):
                _scan_single_parsed(SimpleNamespace(), parsed, run_dir, args, batch_tag="[Date 1/1]")
            out = buf.getvalue()
            self.assertIn("Already scanned (2/2 saved in checkpoint)", out)
            self.assertIn("[01/02] [HIT ] 2025-09410", out)
            self.assertIn("[02/02] [EMPT] 2026-06036 | Peo v Antonucci, Ashley", out)
            self.assertIn("SCAN SUMMARY:", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
