from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from classifier import STATUS_EMPTY_NO_CASE, STATUS_HIT_VALID, BriefLink
from gui_worker import ScannerWorker, diff_rechecked
from report_builder import CaseScanRecord

B = [BriefLink("APPELLANT'S BRIEF", "https://x/ViewDocument?docIndex=A", "4 X")]


class TestDiffRechecked(unittest.TestCase):
    def test_unchanged_changed_and_untouched_cases(self):
        old = {
            "A": CaseScanRecord("A", STATUS_HIT_VALID, briefs=B, checked_at="t1"),
            "B": CaseScanRecord("B", STATUS_EMPTY_NO_CASE, checked_at="t1"),
            "C": CaseScanRecord("C", STATUS_EMPTY_NO_CASE, checked_at="t1"),
        }
        new = {
            "A": CaseScanRecord("A", STATUS_HIT_VALID, briefs=B, checked_at="t2"),      # same result
            "B": CaseScanRecord("B", STATUS_HIT_VALID, briefs=B, checked_at="t2"),      # changed
            "C": CaseScanRecord("C", STATUS_EMPTY_NO_CASE, checked_at="t1"),            # not re-checked (stopped)
            "D": CaseScanRecord("D", STATUS_EMPTY_NO_CASE, checked_at="t2"),            # brand new, no baseline
        }
        out = diff_rechecked(old, new)
        self.assertEqual(out[0], "2")  # only A and B were actually re-checked
        self.assertEqual(len(out), 2)
        self.assertIn("B: EMPTY_NO_CASE (0 brief) -> HIT_VALID (1 brief)", out[1])

    def test_identical_rescan_reports_no_changes(self):
        old = {"A": CaseScanRecord("A", STATUS_HIT_VALID, briefs=B, checked_at="t1")}
        new = {"A": CaseScanRecord("A", STATUS_HIT_VALID, briefs=B, checked_at="t2")}
        self.assertEqual(diff_rechecked(old, new), ["1"])


class _DeadPage:
    def is_closed(self):
        return True


class TestEnsureSessionAlive(unittest.TestCase):
    def test_closed_tab_gets_a_new_page_without_restarting_edge(self):
        events = __import__("queue").Queue()
        worker = ScannerWorker(events)

        class Ctx:
            def new_page(self_inner):
                return type("P", (), {"is_closed": lambda s: False, "title": lambda s: "ok"})()

        worker.session = type("S", (), {"page": _DeadPage(), "context": Ctx()})()
        worker._ensure_session_alive()
        self.assertEqual(worker.session.page.title(), "ok")
        self.assertTrue(events.empty())

    def test_dead_browser_triggers_reopen(self):
        events = __import__("queue").Queue()
        worker = ScannerWorker(events)
        reopened = []

        class Ctx:
            def new_page(self_inner):
                raise RuntimeError("browser has been closed")

        worker.session = type("S", (), {"page": _DeadPage(), "context": Ctx()})()
        worker._close_session = lambda: reopened.append("close")
        worker._open_session = lambda: reopened.append("open")
        worker._ensure_session_alive()
        self.assertEqual(reopened, ["close", "open"])
        self.assertIn("reopening Edge", events.get_nowait()[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
