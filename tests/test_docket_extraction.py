from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from classifier import STATUS_HIT_VALID, STATUS_NO_BRIEF_FOUND, STATUS_SEALED_OR_UNCLICKABLE, classify_docket
from nyscef_browser import BRIEF_ROWS_JS

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

DOCKET_WITH_BRIEFS = """
<table><tr><td>
  <table>
    <tr><th>Doc</th><th>Type</th></tr>
    <tr>
      <td>4</td>
      <td><a href="/nyscef/ViewDocument?docIndex=AAA">APPELLANT'S BRIEF</a>
          *Corrected*
          WALLSHEIN, CHARLES<br>Filed: 10/15/2025</td>
      <td><a href="/nyscef/ConfirmationNotice?docIndex=AAA">Confirmation Notice</a></td>
    </tr>
    <tr>
      <td>5</td>
      <td><a href="/nyscef/ViewDocument?docIndex=BBB">DECISION &amp; ORDER ON MOTION</a> directing brief by May 1</td>
    </tr>
    <tr><td>6</td><td>NOTICE OF APPEAL</td></tr>
  </table>
</td></tr></table>
"""

DOCKET_SEALED = """
<table><tr><td>13</td><td>APPELLANT'S BRIEF SANDERS, PETER S. Filed: 02/11/2026</td></tr></table>
"""

DOCKET_NO_BRIEF = """
<table><tr><td>1</td><td>NOTICE OF APPEAL</td></tr><tr><td>2</td><td>PRE-ARGUMENT STATEMENT</td></tr></table>
"""


@unittest.skipIf(sync_playwright is None, "Playwright not installed")
class TestBriefRowsJsOnRealDom(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(channel="msedge", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._pw.stop()

    def _extract(self, html: str):
        page = self._browser.new_page()
        try:
            page.set_content(html)
            return page.eval_on_selector_all("tr", BRIEF_ROWS_JS)
        finally:
            page.close()

    def test_real_brief_is_hit_and_notice_and_order_are_ignored(self):
        rows = self._extract(DOCKET_WITH_BRIEFS)
        # Outer wrapper <tr> must be skipped (it contains nested <tr>), only leaf rows stay.
        self.assertTrue(all("\n" not in r["text"] and "  " not in r["text"] for r in rows))
        status, briefs, _ = classify_docket(rows, case_type="Civil Action - General")
        self.assertEqual(status, STATUS_HIT_VALID)
        self.assertEqual([b.title for b in briefs], ["APPELLANT'S BRIEF"])
        self.assertIn("ViewDocument?docIndex=AAA", briefs[0].url)
        self.assertTrue(briefs[0].url.startswith("http") or briefs[0].url.startswith("about:") or "ViewDocument" in briefs[0].url)

    def test_plain_text_brief_is_sealed(self):
        status, briefs, _ = classify_docket(self._extract(DOCKET_SEALED), case_type="Civil Action - Matrimonial")
        self.assertEqual(status, STATUS_SEALED_OR_UNCLICKABLE)
        self.assertEqual(briefs, [])

    def test_docket_without_brief_text(self):
        status, briefs, _ = classify_docket(self._extract(DOCKET_NO_BRIEF))
        self.assertEqual(status, STATUS_NO_BRIEF_FOUND)
        self.assertEqual(briefs, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
