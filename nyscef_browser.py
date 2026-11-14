import base64
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import random
import re
import shutil
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from classifier import (
    STATUS_EMPTY_NO_CASE,
    STATUS_NEEDS_MANUAL_CHECK,
    classify_docket,
)
from config import (
    CAPTCHA_WAIT_TIMEOUT_SEC,
    COUNTY_AD2_VALUE,
    DEFAULT_CDP_PORT,
    HANDDOWNS_URL,
    MAX_DELAY_SEC,
    MIN_DELAY_SEC,
    MSEDGE_CANDIDATES,
    NYSCEF_SEARCH_URL,
    OUTPUT_DIR,
    PROFILE_DIR,
    STOP_EVENT,
)
from report_builder import CaseScanRecord

# Runs against every <tr> of a NYSCEF DocumentList page; keeps leaf rows mentioning Briefs or collectible Pleadings/Motions.
BRIEF_ROWS_JS = r"""trs => {
    const terms = [
        'BRIEF',
        'PETITION FOR REVIEW',
        'PETITION FOR REHEARING',
        'WRIT OF MANDAMUS',
        'WRIT OF CERTIORARI',
        'WRIT OF HABEAS CORPUS',
        'MOTION FOR REHEARING',
        'MOTION FOR REVIEW',
        'MOTION FOR RECONSIDERATION'
    ];
    return trs
        .filter(tr => {
            if (tr.querySelector('tr') || !tr.querySelector('td')) return false;
            const up = tr.innerText.toUpperCase();
            return terms.some(t => up.includes(t));
        })
        .map(tr => ({
            text: tr.innerText.replace(/\s+/g, ' ').trim().slice(0, 260),
            links: Array.from(tr.querySelectorAll('a')).map(a => ({
                text: (a.innerText || '').trim(),
                href: a.href || '',
                onclick: a.getAttribute('onclick') || ''
            }))
        }));
}"""


@dataclass(frozen=True)
class MotionPdfOption:
    label: str
    url: str
    filename: str
    year_month: str = ""
    date_iso: str = ""


def parse_handdown_event_items(raw_items: list[dict]) -> list[MotionPdfOption]:
    """
    Parse and sort FullCalendar or anchor Motion List items newest-first.
    Calendar startIso is preferred over filename dates so clerk filename typos
    (e.g. '06-07-2026_MOTION_LIST.pdf' on calendar date '2026-07-01') stay under the true month.
    """
    seen_urls: set[str] = set()
    options: list[tuple[tuple[int, int, int], MotionPdfOption]] = []

    for item in raw_items:
        url = (item.get("href") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        filename = urllib.parse.unquote(url.rsplit("/", 1)[-1])
        start_iso = (item.get("startIso") or "").strip()

        if re.match(r"^\d{4}-\d{2}-\d{2}$", start_iso):
            yyyy, mm, dd = map(int, start_iso.split("-"))
            sort_key = (yyyy, mm, dd)
            year_month = f"{yyyy:04d}-{mm:02d}"
            date_iso = start_iso
            label = f"{mm:02d}-{dd:02d}-{yyyy:04d} ({filename})"
        else:
            date_m = re.search(r"(\d{1,2})-(\d{1,2})-(\d{4})", filename)
            if date_m:
                mm, dd, yyyy = int(date_m.group(1)), int(date_m.group(2)), int(date_m.group(3))
                sort_key = (yyyy, mm, dd)
                year_month = f"{yyyy:04d}-{mm:02d}"
                date_iso = f"{yyyy:04d}-{mm:02d}-{dd:02d}"
                label = f"{mm:02d}-{dd:02d}-{yyyy:04d} ({filename})"
            else:
                sort_key = (0, 0, 0)
                year_month = "unknown"
                date_iso = ""
                label = filename

        options.append((
            sort_key,
            MotionPdfOption(
                label=label,
                url=url,
                filename=filename,
                year_month=year_month,
                date_iso=date_iso,
            ),
        ))

    options.sort(key=lambda pair: pair[0], reverse=True)
    return [opt for _, opt in options]


def find_msedge_executable() -> Path:
    for candidate in MSEDGE_CANDIDATES:
        if candidate.exists():
            return candidate
    which = shutil.which("msedge") or shutil.which("msedge.exe")
    if which:
        return Path(which)
    raise FileNotFoundError("Microsoft Edge (msedge.exe) was not found on this PC.")


def find_free_port(preferred: int = DEFAULT_CDP_PORT) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("No free localhost port found for Edge CDP.")


def _wait_cdp_ready(port: int, timeout_s: int = 20) -> None:
    deadline = time.time() + timeout_s
    url = f"http://127.0.0.1:{port}/json/version"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.4)
    raise TimeoutError(f"Edge remote debugging port {port} did not become ready within {timeout_s}s.")


def parse_content_disposition_filename(cd_header: str) -> str:
    """Extract server-suggested filename from Content-Disposition header."""
    if not cd_header:
        return ""
    m_utf8 = re.search(r"filename\*\s*=\s*UTF-8''([^;\r\n]+)", cd_header, re.IGNORECASE)
    if m_utf8:
        return urllib.parse.unquote(m_utf8.group(1).strip().strip('"\''))
    m = re.search(r'filename\s*=\s*"([^"]+)"', cd_header, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m_bare = re.search(r'filename\s*=\s*([^;\s]+)', cd_header, re.IGNORECASE)
    if m_bare:
        return m_bare.group(1).strip().strip('"\'')
    return ""


class NyscefEdgeSession:
    """
    Launches a genuine visible Microsoft Edge window (no Playwright launch flags)
    and attaches over Chrome DevTools Protocol (CDP). Any Captcha/Turnstile screen
    pauses safely for the human user to click once in the open Edge window.
    """

    def __init__(self, profile_dir: Path = PROFILE_DIR, cdp_port: int | None = None):
        self.profile_dir = profile_dir
        self.cdp_port = cdp_port or find_free_port()
        self._proc: subprocess.Popen | None = None
        self._pw_cm = None
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    def __enter__(self) -> "NyscefEdgeSession":
        msedge = find_msedge_executable()
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 1  # SW_SHOWNORMAL so Edge is always visible on desktop

        candidate_dirs = [
            self.profile_dir,
            self.profile_dir.parent / f".edge_profile_session_{self.cdp_port}",
        ]
        last_err: Exception | None = None
        for prof_dir in candidate_dirs:
            prof_dir.mkdir(parents=True, exist_ok=True)
            self._proc = subprocess.Popen(
                [
                    str(msedge),
                    f"--remote-debugging-port={self.cdp_port}",
                    f"--user-data-dir={prof_dir}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "about:blank",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                startupinfo=startupinfo,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            try:
                _wait_cdp_ready(self.cdp_port, timeout_s=12)
                last_err = None
                break
            except Exception as exc:
                last_err = exc
                if self._proc and self._proc.poll() is None:
                    self._proc.terminate()
                # Primary profile may already be locked by another open Edge window; retry with isolated dir
                continue

        if last_err is not None:
            raise last_err

        self._pw_cm = sync_playwright()
        self._pw = self._pw_cm.__enter__()
        self._browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{self.cdp_port}")
        if self._browser.contexts:
            self.context = self._browser.contexts[0]
        else:
            self.context = self._browser.new_context(accept_downloads=True)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw_cm:
                self._pw_cm.__exit__(exc_type, exc, tb)
        except Exception:
            pass
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()

    def _is_captcha_present(self) -> bool:
        assert self.page is not None
        title_up = (self.page.title() or "").strip().upper()
        url_up = (self.page.url or "").upper()
        if any(k in title_up for k in ("CAPTCHA", "JUST A MOMENT", "ATTENTION REQUIRED")):
            return True
        if "CAPTCHA" in url_up:
            return True
        try:
            # Real NYSCEF pages embed hidden hCaptcha/reCAPTCHA iframes, so iframe presence is NOT
            # a signal. A genuine challenge replaces the page and shows this text instead.
            body_head = self.page.inner_text("body")[:400].upper()
            if "CHECK THE BOX BELOW" in body_head or "VERIFY YOU ARE HUMAN" in body_head or "HAVING CAPTCHA TROUBLE" in body_head:
                return True
        except Exception:
            pass
        return False

    def wait_if_captcha(self, step_label: str, timeout_s: int = CAPTCHA_WAIT_TIMEOUT_SEC) -> bool:
        assert self.page is not None
        start = time.time()
        notified = False
        last_reminder = 0.0
        while time.time() - start < timeout_s:
            if STOP_EVENT.is_set():
                return False
            if self._is_captcha_present():
                elapsed = time.time() - start
                if not notified:
                    print(
                        f"\a\n[USER ACTION REQUIRED] hCaptcha/Verification detected at '{step_label}'.\n"
                        "Paused — please click the 'I am human' check inside Edge. The scan will auto-resume as soon as it clears (or click Stop anytime)...",
                        flush=True,
                    )
                    try:
                        self.page.bring_to_front()
                    except Exception:
                        pass
                    notified = True
                    last_reminder = elapsed
                elif elapsed - last_reminder >= 45.0:
                    print(f"[WAITING FOR CAPTCHA] Still paused at '{step_label}' ({int(elapsed)}s elapsed)...", flush=True)
                    last_reminder = elapsed
                time.sleep(1.0)
                continue

            if notified:
                # Allow post-Captcha redirect to settle before returning
                try:
                    self.page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                time.sleep(1.0)
                print(f"[RESUMED] Captcha cleared in {time.time() - start:.1f}s.", flush=True)
            return True
        return False

    def list_handdown_motion_pdfs(self) -> list[MotionPdfOption]:
        assert self.page is not None
        cache_file = OUTPUT_DIR / "ny_calendar_cache.json"
        STOP_EVENT.clear()
        load_err: Exception | None = None
        for attempt in (1, 2, 3):
            if STOP_EVENT.is_set():
                return []
            try:
                self.page.goto(HANDDOWNS_URL, wait_until="domcontentloaded", timeout=60000)
                if STOP_EVENT.is_set():
                    return []
                if self.wait_if_captcha("AD2 Handdowns Calendar"):
                    load_err = None
                    break
            except Exception as exc:
                load_err = exc
                if STOP_EVENT.is_set():
                    return []
                if attempt == 3:
                    break
                time.sleep(2.0)
        else:
            if STOP_EVENT.is_set():
                return []
            load_err = TimeoutError("Timed out waiting to load NY Courts Handdowns page.")

        if STOP_EVENT.is_set():
            return []

        # If loading failed or timed out, attempt to fallback to cached calendar
        if load_err is not None:
            if cache_file.exists():
                try:
                    cached_raw = json.loads(cache_file.read_text(encoding="utf-8"))
                    if cached_raw and isinstance(cached_raw, list):
                        print(f"[CACHE] NY Courts site unreachable ({load_err}) — loaded {len(cached_raw)} Motion Lists from cache.", flush=True)
                        return parse_handdown_event_items(cached_raw)
                except Exception:
                    pass
            raise load_err

        time.sleep(1.0)

        # Primary: extract all months/years directly from FullCalendar's in-memory clientEvents
        raw_items = self.page.evaluate(
            """() => {
                if (window.$ && $('#Handdown').fullCalendar) {
                    try {
                        const events = $('#Handdown').fullCalendar('clientEvents') || [];
                        const mapped = events
                            .map(e => {
                                const rawUrl = e.url ? new URL(e.url, window.location.href).href : '';
                                const startIso = e.start ? e.start.format('YYYY-MM-DD') : '';
                                return {
                                    text: (e.title || '').trim(),
                                    href: rawUrl,
                                    startIso
                                };
                            })
                            .filter(x =>
                                (x.text.toUpperCase().includes('MOTION') || x.href.toUpperCase().includes('MOTION'))
                                && x.href.toUpperCase().endsWith('.PDF')
                            );
                        if (mapped.length > 0) return mapped;
                    } catch (err) {}
                }
                return Array.from(document.querySelectorAll('a[href]'))
                    .map(a => ({
                        text: (a.innerText || '').trim(),
                        href: a.href,
                        startIso: ''
                    }))
                    .filter(x =>
                        (x.text.toUpperCase().includes('MOTION') || x.href.toUpperCase().includes('MOTION'))
                        && x.href.toUpperCase().endsWith('.PDF')
                    );
            }"""
        )

        # Cache successfully fetched items for offline/resilient fallback
        if raw_items and isinstance(raw_items, list):
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps(raw_items, indent=2), encoding="utf-8")
            except Exception:
                pass

        return parse_handdown_event_items(raw_items)

    def download_motion_pdf(self, pdf_option: MotionPdfOption, save_to: Path) -> bytes:
        assert self.page is not None
        assert self.context is not None
        save_to.parent.mkdir(parents=True, exist_ok=True)

        # Method 1 (Primary): Capture genuine Edge browser navigation response via CDP Fetch
        # before Edge's built-in PDF viewer wraps the stream in HTML.
        cdp = self.context.new_cdp_session(self.page)
        captured: dict[str, bytes] = {}
        last_err: str = ""

        def _on_paused(ev: dict) -> None:
            nonlocal last_err
            req_id = ev["requestId"]
            url = ev.get("request", {}).get("url", "")
            status = ev.get("responseStatusCode")
            try:
                if status == 200 and ".PDF" in url.upper():
                    body_res = cdp.send("Fetch.getResponseBody", {"requestId": req_id})
                    raw = (
                        base64.b64decode(body_res["body"])
                        if body_res.get("base64Encoded")
                        else body_res["body"].encode("latin1")
                    )
                    if raw.lstrip().startswith(b"%PDF"):
                        captured["bytes"] = raw
                elif status and status >= 400:
                    last_err = f"HTTP {status} from {url}"
            except Exception as exc:
                last_err = f"CDP getResponseBody error: {exc}"
            finally:
                try:
                    cdp.send("Fetch.continueResponse", {"requestId": req_id})
                except Exception:
                    try:
                        cdp.send("Fetch.continueRequest", {"requestId": req_id})
                    except Exception:
                        pass

        cdp.on("Fetch.requestPaused", _on_paused)
        try:
            cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*.pdf*", "requestStage": "Response"}]})
            try:
                self.page.goto(pdf_option.url, wait_until="load", timeout=35000)
            except Exception as nav_exc:
                if "bytes" not in captured:
                    last_err = f"Navigation note: {nav_exc}"
            self.wait_if_captcha("Motion List PDF Download")
            time.sleep(0.3)
        finally:
            try:
                cdp.send("Fetch.disable")
            except Exception:
                pass
            try:
                cdp.detach()
            except Exception:
                pass

        if "bytes" in captured:
            save_to.write_bytes(captured["bytes"])
            return captured["bytes"]

        # Method 2 (Fallback): Same-origin <a download> click on Handdowns page
        if "nycourts.gov" not in (self.page.url or ""):
            self.page.goto(HANDDOWNS_URL, wait_until="domcontentloaded", timeout=35000)
            self.wait_if_captcha("Handdowns Re-entry")

        try:
            with self.page.expect_download(timeout=20000) as dl_info:
                self.page.evaluate(
                    """({url, fname}) => {
                        const a = document.createElement('a');
                        a.href = url;
                        a.download = fname;
                        document.body.appendChild(a);
                        a.click();
                        a.remove();
                    }""",
                    {"url": pdf_option.url, "fname": pdf_option.filename},
                )
            dl = dl_info.value
            dl.save_as(str(save_to))
            data = save_to.read_bytes()
            if data.lstrip().startswith(b"%PDF"):
                return data
        except Exception as dl_exc:
            last_err = f"{last_err}; Fallback download error: {dl_exc}".strip("; ")

        raise RuntimeError(f"Failed to download Motion List PDF ({pdf_option.url}): {last_err}")

    def download_brief_pdf(self, doc_url: str, save_to: Path, timeout_sec: int = 35) -> Path:
        """
        Download a NYSCEF brief PDF directly and preserve exact native server filename.
        Method 1: Authenticated context.request (Fastest, real-time, non-blocking)
        Method 2: expect_download via <a download> click inside Edge page (Fallback)
        """
        assert self.page is not None
        assert self.context is not None

        if save_to.is_dir() or not save_to.suffix:
            dest_dir = save_to
            dest_dir.mkdir(parents=True, exist_ok=True)
            default_target = None
        else:
            dest_dir = save_to.parent
            dest_dir.mkdir(parents=True, exist_ok=True)
            default_target = save_to
            if default_target.exists() and default_target.stat().st_size >= 100:
                return default_target

        last_err: str = ""

        # Method 1 (Primary): Direct streaming via Playwright APIRequestContext
        # Shares the active browser session, cookies, and Cloudflare tokens without page navigation
        try:
            resp = self.context.request.get(doc_url, timeout=timeout_sec * 1000)
            if resp.status == 200:
                body = resp.body()
                if body.lstrip().startswith(b"%PDF"):
                    cd = resp.headers.get("content-disposition", "")
                    server_name = parse_content_disposition_filename(cd)
                    fname = server_name or (default_target.name if default_target else "Brief.pdf")
                    final_path = dest_dir / fname
                    final_path.write_bytes(body)
                    return final_path
                else:
                    last_err = f"Response is not a valid PDF (length={len(body)})"
            elif resp.status in (401, 403):
                last_err = f"HTTP {resp.status} - Cloudflare check or auth needed"
            else:
                last_err = f"HTTP {resp.status} from {doc_url}"
        except Exception as req_exc:
            last_err = f"APIRequest error: {req_exc}"

        # Method 2 (Fallback): In-page <a download> click via browser session
        try:
            with self.page.expect_download(timeout=15000) as dl_info:
                self.page.evaluate(
                    """({url}) => {
                        const a = document.createElement('a');
                        a.href = url;
                        a.setAttribute('download', '');
                        document.body.appendChild(a);
                        a.click();
                        setTimeout(() => a.remove(), 1000);
                    }""",
                    {"url": doc_url},
                )
            dl = dl_info.value
            fname = dl.suggested_filename or (default_target.name if default_target else "Brief.pdf")
            final_path = dest_dir / fname
            dl.save_as(str(final_path))
            if final_path.exists() and final_path.stat().st_size >= 100:
                return final_path
        except Exception as dl_exc:
            last_err = f"{last_err}; download error: {dl_exc}".strip("; ")

        raise RuntimeError(f"Failed to download NYSCEF brief PDF ({doc_url}): {last_err}")

    def check_case(self, case_no: str, min_delay: float = MIN_DELAY_SEC, max_delay: float = MAX_DELAY_SEC) -> CaseScanRecord:
        assert self.page is not None
        now_str = datetime.now().isoformat(timespec="seconds")

        # Polite human-like pause between cases
        time.sleep(random.uniform(min_delay, max_delay))

        try:
            on_results_page = False
            page_title = ""
            for attempt in (1, 2):
                self.page.goto(NYSCEF_SEARCH_URL, wait_until="domcontentloaded", timeout=35000)
                if not self.wait_if_captcha(f"CaseSearch ({case_no})"):
                    return CaseScanRecord(
                        case_number=case_no,
                        status=STATUS_NEEDS_MANUAL_CHECK,
                        note="Timed out waiting for Captcha on CaseSearch page",
                        checked_at=now_str,
                    )

                time.sleep(random.uniform(0.4, 0.8))
                self.page.select_option('select[name="txtCounty"]', COUNTY_AD2_VALUE)
                rb = self.page.locator('input[name="rbCaseIdentifierNumberType"][value="indexNumber"]')
                if rb.count() > 0:
                    rb.check()
                self.page.fill('input[name="txtCaseIdentifierNumber"]', case_no)
                time.sleep(random.uniform(0.3, 0.6))

                with self.page.expect_navigation(wait_until="domcontentloaded", timeout=35000):
                    self.page.locator('button[name="btnSubmit"]').first.click()

                if not self.wait_if_captcha(f"CaseSearchResults ({case_no})"):
                    return CaseScanRecord(
                        case_number=case_no,
                        status=STATUS_NEEDS_MANUAL_CHECK,
                        note="Timed out waiting for Captcha after search submit",
                        checked_at=now_str,
                    )

                time.sleep(0.4)
                page_title = (self.page.title() or "").strip()
                body_text = self.page.inner_text("body")
                on_results_page = (
                    "CASE SEARCH RESULTS" in page_title.upper()
                    or "CASE SEARCH RESULTS" in body_text[:600].upper()
                    or "CASESEARCHRESULTS" in self.page.url.upper()
                )
                if on_results_page:
                    break
                # If solving hCaptcha returned to blank CaseSearch, auto-retry once immediately
                if attempt == 1 and self.page.locator('input[name="txtCaseIdentifierNumber"]').count() > 0:
                    print(f"[RETRY] Re-submitting {case_no} after Captcha redirect...", flush=True)
                    continue
                break

            if not on_results_page:
                return CaseScanRecord(
                    case_number=case_no,
                    status=STATUS_NEEDS_MANUAL_CHECK,
                    note=f"Unexpected page after submit (title='{page_title}', url='{self.page.url}')",
                    checked_at=now_str,
                )

            # Restrict search to results table docket links matching exact case number
            case_link = self.page.locator('a[href*="DocumentList"]').filter(
                has_text=re.compile(rf"^\s*{re.escape(case_no)}\s*$")
            )
            if case_link.count() == 0:
                case_link = self.page.locator('a[href*="DocumentList"]').filter(has_text=case_no)

            if case_link.count() == 0:
                # A genuine empty result echoes the searched number; otherwise the page is
                # an error/throttle page and must not be reported as "case not found".
                if case_no not in body_text:
                    return CaseScanRecord(
                        case_number=case_no,
                        status=STATUS_NEEDS_MANUAL_CHECK,
                        note="Results page did not echo the searched case number; re-scan needed",
                        checked_at=now_str,
                    )
                return CaseScanRecord(
                    case_number=case_no,
                    status=STATUS_EMPTY_NO_CASE,
                    note="No matching docket in NYSCEF Appellate Division 2nd Dept",
                    checked_at=now_str,
                )

            # Extract Caption and Court/Case Type from the matching row
            cells = case_link.first.locator("xpath=ancestor::tr[1]/td")
            caption = ""
            case_type = ""
            if cells.count() >= 4:
                caption = cells.nth(2).inner_text().replace("\n", " ").strip()
                case_type = cells.nth(3).inner_text().replace("\n", " | ").strip()

            on_doc_list = False
            doc_body = ""
            for doc_attempt in (1, 2):
                with self.page.expect_navigation(wait_until="domcontentloaded", timeout=35000):
                    case_link.first.click()

                if not self.wait_if_captcha(f"DocumentList ({case_no})"):
                    return CaseScanRecord(
                        case_number=case_no,
                        status=STATUS_NEEDS_MANUAL_CHECK,
                        caption=caption,
                        case_type=case_type,
                        docket_url=self.page.url,
                        note="Timed out waiting for Captcha on DocumentList page",
                        checked_at=now_str,
                    )

                time.sleep(0.5)
                doc_title = (self.page.title() or "").strip().upper()
                doc_body = self.page.inner_text("body")
                on_doc_list = (
                    ("DOCUMENT LIST" in doc_title or "DOCUMENTLIST" in self.page.url.upper())
                    and case_no in doc_body
                )
                if on_doc_list:
                    break
                if doc_attempt == 1 and case_link.count() > 0:
                    print(f"[RETRY] Re-opening DocumentList for {case_no} after Captcha redirect...", flush=True)
                    continue
                break

            if not on_doc_list:
                return CaseScanRecord(
                    case_number=case_no,
                    status=STATUS_NEEDS_MANUAL_CHECK,
                    caption=caption,
                    case_type=case_type,
                    docket_url=self.page.url,
                    note="DocumentList page did not load or echo case number; re-scan needed",
                    checked_at=now_str,
                )

            docket_url = self.page.url
            brief_rows = self.page.eval_on_selector_all("tr", BRIEF_ROWS_JS)

            status, clickable_briefs, note = classify_docket(brief_rows, case_type=case_type)
            doc_body_up = doc_body[:800].upper()
            if "PAGE 1 OF" in doc_body_up and "PAGE 1 OF 1" not in doc_body_up:
                note = f"{note} | [MULTI-PAGE DOCKET DETECTED]"
                if status == "NO_BRIEF_FOUND":
                    status = STATUS_NEEDS_MANUAL_CHECK
            return CaseScanRecord(
                case_number=case_no,
                status=status,
                caption=caption,
                case_type=case_type,
                docket_url=docket_url,
                note=note,
                briefs=clickable_briefs,
                checked_at=now_str,
            )

        except Exception as exc:
            return CaseScanRecord(
                case_number=case_no,
                status=STATUS_NEEDS_MANUAL_CHECK,
                note=f"Error during scan: {exc}",
                checked_at=now_str,
            )
