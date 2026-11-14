from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nyscef_browser import NyscefEdgeSession

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

# Mirrors the real NYSCEF CaseSearch page: normal form + hidden captcha iframes/widgets.
CASE_SEARCH_WITH_HIDDEN_WIDGETS = """
<html><head><title>Case Search</title></head><body>
<h1>Case Search</h1><input name="txtCaseIdentifierNumber"/>
<iframe src="https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html" style="display:none"></iframe>
<iframe src="https://www.google.com/recaptcha/api2/anchor" style="display:none"></iframe>
<div class="h-captcha" style="width:0;height:0"></div>
</body></html>
"""

CHALLENGE_PAGE = """
<html><head><title>Captcha</title></head><body>
<p>To continue, please check the box below:</p><p>Having CAPTCHA trouble?</p>
<iframe src="https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html"></iframe>
</body></html>
"""


@unittest.skipIf(sync_playwright is None, "Playwright not installed")
class TestCaptchaDetection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(channel="msedge", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._pw.stop()

    def _detect(self, html: str) -> bool:
        session = NyscefEdgeSession.__new__(NyscefEdgeSession)
        session.page = self._browser.new_page()
        try:
            session.page.set_content(html)
            return session._is_captcha_present()
        finally:
            session.page.close()

    def test_hidden_captcha_widgets_on_normal_page_are_not_a_challenge(self):
        self.assertFalse(self._detect(CASE_SEARCH_WITH_HIDDEN_WIDGETS))

    def test_real_challenge_page_is_detected(self):
        self.assertTrue(self._detect(CHALLENGE_PAGE))


if __name__ == "__main__":
    unittest.main(verbosity=2)
