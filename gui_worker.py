import argparse
import queue
import re
import threading
import traceback

from config import MAX_DELAY_SEC, MIN_DELAY_SEC, OUTPUT_DIR, STOP_EVENT, verify_app_integrity
from main import _run_dir_for_option, _run_option_batch, _scan_single_parsed, _slugify, group_options_by_month
from nyscef_browser import NyscefEdgeSession
from pdf_parser import parse_motion_pdf_file
from report_builder import load_checkpoint
from ui_theme import format_date_words


class QueueWriter:
    """File-like stdout replacement that forwards complete lines to the GUI event queue."""

    def __init__(self, events: queue.Queue):
        self._events = events
        self._buffer = ""

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._events.put(("log", line.replace("\a", "").rstrip()))
        return len(text)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False


def make_args() -> argparse.Namespace:
    return argparse.Namespace(
        pdf=None,
        month=None,
        auto_latest=False,
        all_month=False,
        limit=0,
        fresh=False,
        min_delay=MIN_DELAY_SEC,
        max_delay=MAX_DELAY_SEC,
        no_open=True,  # GUI has its own "Open" buttons
    )


def diff_rechecked(old: dict, new: dict) -> list[str]:
    """
    Compare two checkpoint record maps and describe what a re-scan changed.
    Only cases re-checked in this run (different `checked_at`) are compared, so a stopped
    re-scan never reports untouched cases as verified.
    """
    rechecked = 0
    changes: list[str] = []
    for case_no, rec in new.items():
        before = old.get(case_no)
        if before is None or before.checked_at == rec.checked_at:
            continue
        rechecked += 1
        old_briefs, new_briefs = len(before.briefs or []), len(rec.briefs or [])
        if before.status != rec.status or old_briefs != new_briefs:
            changes.append(
                f"{case_no}: {before.status} ({old_briefs} brief) -> {rec.status} ({new_briefs} brief)"
            )
    return [str(rechecked)] + changes


class ScannerWorker(threading.Thread):
    """
    Owns the Edge/Playwright session (Playwright sync objects are thread-bound), loads the
    calendar once, then executes commands pushed by the GUI:
    ('scan', opts, month_opts, month, fresh), ('local_pdf', path) or None to quit.
    If the user closes the Edge window between scans, a new session is opened automatically.
    """

    def __init__(self, events: queue.Queue):
        super().__init__(name="scanner-worker", daemon=False)
        verify_app_integrity()
        self.events = events
        self.commands: queue.Queue = queue.Queue()
        self.args = make_args()
        self._cm: NyscefEdgeSession | None = None
        self.session: NyscefEdgeSession | None = None

    def _open_session(self) -> NyscefEdgeSession:
        self._close_session()
        self._cm = NyscefEdgeSession()
        self.session = self._cm.__enter__()
        return self.session

    def _close_session(self) -> None:
        if self._cm is not None:
            try:
                self._cm.__exit__(None, None, None)
            except Exception:
                pass
        self._cm = self.session = None

    def _ensure_session_alive(self) -> None:
        """Reopen a tab, or the whole Edge session, if the user closed the window."""
        if self.session is None:
            self._open_session()
            return
        try:
            if self.session.page is None or self.session.page.is_closed():
                self.session.page = self.session.context.new_page()
            self.session.page.title()  # raises if the browser process is gone
            return
        except Exception:
            self.events.put(("log", "Edge window was closed — reopening Edge..."))
        self._close_session()
        self._open_session()

    def _init_ny_calendar(self) -> None:
        STOP_EVENT.clear()
        self.events.put(("status", "Starting Edge for NY NYSCEF..."))
        if self.session is None:
            session = self._open_session()
        else:
            self._ensure_session_alive()
            session = self.session
        self.events.put(("status", "Loading Motion List calendar from NY Courts..."))
        options = session.list_handdown_motion_pdfs()
        if not options:
            if STOP_EVENT.is_set():
                return
            raise RuntimeError("No Motion List PDFs found on the Handdowns page.")
        self.events.put(("calendar", group_options_by_month(options)))

    def run(self) -> None:
        try:
            self._command_loop()
        except Exception:
            self.events.put(("fatal", traceback.format_exc()))
        finally:
            self._close_session()
            self.events.put(("closed", None))

    def _scan_batch(self, selected, month_opts, month, fresh: bool) -> None:
        before = {}
        if fresh:
            before = {id(o): load_checkpoint(_run_dir_for_option(o)) for o in selected}
        self.args.fresh = fresh
        try:
            _run_option_batch(self.session, selected, month_opts, month, self.args)
        finally:
            self.args.fresh = False
        if fresh:
            self._report_verification(selected, before)

    def _report_verification(self, selected, before: dict) -> None:
        for opt in selected:
            result = diff_rechecked(before[id(opt)], load_checkpoint(_run_dir_for_option(opt)))
            rechecked, changes = result[0], result[1:]
            name = format_date_words(opt.date_iso or opt.filename)
            if not changes:
                print(f"[VERIFY] {name}: {rechecked} case(s) re-checked, all match the previous scan.")
                continue
            print(f"[VERIFY] {name}: {rechecked} case(s) re-checked, {len(changes)} CHANGED:")
            for line in changes:
                print(f"[VERIFY]   {line}")

    def _command_loop(self) -> None:
        while True:
            cmd = self.commands.get()
            if cmd is None:
                return
            STOP_EVENT.clear()
            if cmd[0] == "init_ny":
                try:
                    self._init_ny_calendar()
                except Exception:
                    if not STOP_EVENT.is_set():
                        self.events.put(("fatal", traceback.format_exc()))
                continue
            self.events.put(("busy", None))
            try:
                self._ensure_session_alive()
                if cmd[0] == "scan":
                    _, selected, month_opts, month, fresh = cmd
                    self._scan_batch(selected, month_opts, month, fresh)
                elif cmd[0] == "local_pdf":
                    parsed = parse_motion_pdf_file(cmd[1])
                    _scan_single_parsed(self.session, parsed, OUTPUT_DIR / _slugify(parsed.source_name), self.args)
                elif cmd[0] == "download_briefs":
                    _, items, month_key = cmd
                    self._download_briefs_batch(items, month_key)
            except Exception:
                self.events.put(("error", traceback.format_exc()))
            finally:
                self.events.put(("idle", None))

    def _download_briefs_batch(self, items: list[dict], month_key: str) -> None:
        import time
        from pathlib import Path
        from gui_hits import ny_downloads_dir

        total = len(items)
        if total == 0:
            return
        self.events.put(("log", f"=== AUTO-DOWNLOADING {total} NY BRIEF PDF(s) ===", "head"))
        downloaded = 0
        by_case: dict[str, list[dict]] = {}
        for it in items:
            by_case.setdefault(it["key"], []).append(it)

        case_downloaded: dict[str, int] = {}
        for idx, it in enumerate(items, 1):
            if STOP_EVENT.is_set():
                self.events.put(("log", "Download canceled by user.", "warn"))
                break
            case_no = it["case_number"]
            doc_url = it["url"]
            save_path = Path(it["save_path"])
            dest_dir = Path(it.get("dest_dir") or save_path.parent)
            dest_dir.mkdir(parents=True, exist_ok=True)
            doc_title = it.get("title") or save_path.stem

            # Check if this document already exists on disk (>= 100 bytes)
            existing_file: Path | None = None
            if save_path.exists() and save_path.stat().st_size >= 100:
                existing_file = save_path
            else:
                safe_case = case_no.replace("-", "_").upper()
                raw_case = case_no.upper()
                clean_words = [w for w in re.sub(r"[^A-Za-z0-9]+", " ", doc_title).upper().split() if len(w) > 3]
                for p in dest_dir.glob("*.pdf"):
                    p_up = p.name.upper()
                    if (safe_case in p_up or raw_case in p_up) and (not clean_words or any(w in p_up for w in clean_words)):
                        if p.stat().st_size >= 100:
                            existing_file = p
                            break

            if existing_file:
                downloaded += 1
                self.events.put(("log", f"[EXISTS] {case_no} -> {existing_file.name} (Skipping re-download)", "dim"))
                self.events.put(("dl_prog", (idx, total, f"[{idx}/{total}] Already downloaded: {existing_file.name[:45]}")))
                case_downloaded[it["key"]] = case_downloaded.get(it["key"], 0) + 1
                if case_downloaded[it["key"]] >= len(by_case.get(it["key"], [])):
                    self.events.put(("ny_case_done", it["key"]))
                continue

            self.events.put(("dl_prog", (idx - 1, total, f"[{idx}/{total}] Downloading: {case_no} - {doc_title[:35]}...")))
            try:
                if not doc_url:
                    self.events.put(("log", f"[SKIP] No URL for {case_no} ({doc_title})", "dim"))
                    self.events.put(("dl_prog", (idx, total, f"[{idx}/{total}] Skipped (No URL): {doc_title[:35]}")))
                    continue
                # session.download_brief_pdf preserves exact website filename via Content-Disposition header
                saved_path = self.session.download_brief_pdf(doc_url, save_path)
                downloaded += 1
                self.events.put(("log", f"[DOWNLOADED] {case_no} -> {saved_path.name}", "hit"))
                self.events.put(("dl_prog", (idx, total, f"[{idx}/{total}] Saved: {saved_path.name[:45]}")))
                case_downloaded[it["key"]] = case_downloaded.get(it["key"], 0) + 1
                if case_downloaded[it["key"]] >= len(by_case.get(it["key"], [])):
                    self.events.put(("ny_case_done", it["key"]))
            except Exception as exc:
                self.events.put(("log", f"[ERROR] Failed to download {case_no} ({doc_title}): {exc}", "warn"))
                self.events.put(("dl_prog", (idx, total, f"[{idx}/{total}] Error: {doc_title[:30]}")))
            time.sleep(0.1)

        dest = ny_downloads_dir(month_key)
        self.events.put(("dl_prog", (total, total, f"Downloaded {downloaded}/{total} PDF(s)")))
        self.events.put(("dl_done", (downloaded, total, str(dest))))
