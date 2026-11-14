from contextlib import closing
import datetime as dt
import gzip
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from classifier import STATUS_HIT_VALID
from config import OUTPUT_DIR, STOP_EVENT
from ky_scanner import (
    KY_API_V1,
    KyCaseRecord,
    build_ky_tracker_rows,
    fetch_ky_month_search_rows,
    load_ky_checkpoint,
    save_ky_checkpoint,
    scan_ky_case,
)
from report_builder import load_checkpoint
from ui_theme import format_month_words

DB_PATH = OUTPUT_DIR / "team_dashboard.db"
_DB_LOCK = threading.Lock()
_SERVER_INSTANCE: ThreadingHTTPServer | None = None
_SERVER_PORT: int = 8080
_PUBLIC_URL: str = ""
_TUNNEL_PROC: subprocess.Popen | None = None
_TUNNEL_THREAD: threading.Thread | None = None
_TUNNEL_STOP = threading.Event()
_SCAN_STATUS: dict[str, str] = {"running": "", "progress": ""}
_SCAN_PROGRESS: dict[str, any] = {"val": 0, "max": 100, "lbl": "Ready", "active_case": "", "hits": 0}
_SCAN_LOGS: list[dict[str, str]] = []


def build_ky_month_keys(months_back: int = 18) -> tuple[str, ...]:
    """Generate recent YYYY-MM keys dynamically from today's date so new months appear automatically."""
    today = dt.date.today()
    y, m = today.year, today.month
    keys = []
    for _ in range(months_back):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return tuple(keys)


KY_MONTH_KEYS = build_ky_month_keys()


def _fmt_us_date(iso_or_us: str) -> str:
    s = (iso_or_us or "").strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        y, m, d = s.split("-")
        return f"{m}/{d}/{y}"
    return s


def _init_db() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with _DB_LOCK, closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS done_status (
                scope TEXT NOT NULL,
                case_number TEXT NOT NULL,
                done INTEGER NOT NULL DEFAULT 1,
                updated_by TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (scope, case_number)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS published_batches (
                scope TEXT PRIMARY KEY,
                jurisdiction TEXT NOT NULL,
                label TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                published_by TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.commit()


def get_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def _parse_ny_folder_date(folder_name: str) -> tuple[str, str] | None:
    """Extract (YYYY-MM, MM/DD/YYYY) from NY folder names like '11-02-2026_MOTION_LIST' or '2026-07-01__...'."""
    m_iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})_", folder_name)
    if m_iso:
        y, mo, d = m_iso.group(1), m_iso.group(2), m_iso.group(3)
        return f"{y}-{mo}", f"{mo}/{d}/{y}"
    m_us = re.match(r"^(\d{2})-(\d{2})-(\d{4})_", folder_name)
    if m_us:
        mo, d, y = m_us.group(1), m_us.group(2), m_us.group(3)
        return f"{y}-{mo}", f"{mo}/{d}/{y}"
    return None


def sync_ky_checkpoint_to_batch(court_code: str, ym: str, records: dict, rows: list = None) -> None:
    """Synchronize an active or completed Kentucky checkpoint into SQLite published_batches."""
    scope = f"KY_{court_code}_{ym}"
    label = f"Kentucky {court_code} — {format_month_words(ym)}"
    hits = []
    overview = []
    for rec in records.values():
        if rec.status == "BRIEF_FOUND":
            docs = [
                {
                    "document_id": doc.document_id,
                    "document_name": doc.document_name,
                    "filed_date": _fmt_us_date(doc.filed_date),
                    "submitter": doc.submitter,
                    "save_as_name": doc.save_as_name,
                    "download_url": f"{KY_API_V1}/publicaccessdocuments/{doc.document_id}/download" if doc.document_id else "",
                }
                for doc in rec.collectible_docs
            ]
            hits.append({
                "case_number": rec.case_number,
                "case_id": rec.case_id,
                "court": rec.court,
                "title": rec.title,
                "date": _fmt_us_date(rec.first_filed_date),
                "case_url": f"https://appellatepublic.kycourts.net/case/{rec.case_id}",
                "review_note": rec.review_note or "",
                "docs": docs,
            })
        cnt = len(rec.collectible_docs)
        if rec.status == "BRIEF_FOUND":
            if rec.review_note:
                status_lbl = f"HIT ({cnt} PDF{'s' if cnt != 1 else ''}) ⚠"
                tag = "review"
            else:
                status_lbl = f"HIT ({cnt} PDF{'s' if cnt != 1 else ''})"
                tag = "hit"
        elif rec.review_note:
            only_receipt = all("files: receipt notice only" in seg for seg in (rec.review_note or "").split("; "))
            status_lbl = "⚠ Receipt only" if only_receipt else "⚠ Check manually"
            tag = "review"
        elif rec.status == "RECEIPT_ONLY":
            status_lbl = "Receipt Notice only"
            tag = "skip"
        elif rec.status in ("NOT_SCANNED", "PENDING"):
            status_lbl = "Not scanned"
            tag = "pending"
        else:
            status_lbl = "No collectible brief"
            tag = "skip"

        overview.append({
            "date": _fmt_us_date(rec.first_filed_date),
            "case_number": rec.case_number,
            "subtype": "; ".join(rec.search_sub_types or []) or "Briefing",
            "status": status_lbl,
            "tag": tag,
            "is_hit": rec.status == "BRIEF_FOUND",
        })

    payload = {
        "hits": hits,
        "overview": overview,
        "month_key": ym,
        "court_code": court_code,
    }
    _save_batch(scope, "KY", label, payload, published_by="Live Sync")


def sync_local_checkpoints_to_db() -> None:
    """Auto-load local KY and NY checkpoints from output/ into the dashboard database with 100% parity."""
    _init_db()
    if not OUTPUT_DIR.exists():
        return
    ny_by_month: dict[str, list[dict]] = {}
    seen_ny_cases: dict[str, set[str]] = {}

    for d in sorted(OUTPUT_DIR.iterdir()):
        if not d.is_dir():
            continue
        cp = d / "checkpoint.json"
        if not cp.exists():
            continue

        # Kentucky Folders: KY_KYCOA_YYYY-MM or KY_KYSUP_YYYY-MM
        if d.name.startswith("KY_"):
            parts = d.name.split("_")
            if len(parts) >= 3:
                court_code = parts[1]
                ym = parts[2]
                saved = load_ky_checkpoint(court_code, ym)
                if not saved:
                    continue
                sync_ky_checkpoint_to_batch(court_code, ym, saved)
            continue

        # New York Folders: MM-DD-YYYY_MOTION_LIST or YYYY-MM-DD__...
        date_info = _parse_ny_folder_date(d.name)
        if not date_info:
            continue
        month_key, us_date_str = date_info
        try:
            entries = load_checkpoint(cp)
        except Exception:
            continue
        valid = [e for e in entries if getattr(e, "classifier_status", "") == STATUS_HIT_VALID]
        if not valid:
            continue

        seen_ny_cases.setdefault(month_key, set())
        ny_by_month.setdefault(month_key, [])

        for e in valid:
            cn = getattr(e, "case_number", "")
            if not cn or cn in seen_ny_cases[month_key]:
                continue
            seen_ny_cases[month_key].add(cn)

            docs = []
            for b in getattr(e, "brief_rows", []):
                doc_name = getattr(b, "doc_name", "") or getattr(b, "text", "") or "Brief"
                save_as = getattr(b, "sop_save_as_name", "") or getattr(b, "save_as_name", "") or f"NY_{cn}_Brief"
                docs.append({
                    "document_id": "",
                    "document_name": doc_name,
                    "filed_date": us_date_str,
                    "submitter": "",
                    "save_as_name": save_as,
                    "download_url": getattr(b, "doc_url", "") or "",
                })

            ny_by_month[month_key].append({
                "case_number": cn,
                "case_id": "",
                "court": "New York AD2",
                "title": getattr(e, "case_title", "") or "New York Case",
                "date": us_date_str,
                "case_url": getattr(e, "case_url", "") or "https://iapps.courts.state.ny.us/nyscef/CaseSearch",
                "review_note": "",
                "docs": docs,
            })

    for ym, hits in ny_by_month.items():
        scope = f"NY_AD2_{ym}"
        label = f"New York AD2 — {format_month_words(ym)}"
        _save_batch(scope, "NY", label, {"hits": hits, "month_key": ym, "court_code": "AD2"}, published_by="Local Sync")


_STATE_VERSION: int = 1
_CACHED_BASE_STATE: dict | None = None


def _invalidate_state_cache() -> None:
    global _STATE_VERSION, _CACHED_BASE_STATE
    _STATE_VERSION += 1
    _CACHED_BASE_STATE = None


def _save_batch(scope: str, jurisdiction: str, label: str, payload: dict, published_by: str = "Edward") -> None:
    _init_db()
    now_iso = dt.datetime.now().isoformat(timespec="seconds")
    pjson = json.dumps(payload)
    with _DB_LOCK, closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            INSERT INTO published_batches (scope, jurisdiction, label, payload_json, published_by, published_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope) DO UPDATE SET
                jurisdiction=excluded.jurisdiction,
                label=excluded.label,
                payload_json=excluded.payload_json,
                published_by=excluded.published_by,
                published_at=excluded.published_at
            """,
            (scope, jurisdiction, label, pjson, published_by, now_iso),
        )
        conn.commit()
    _invalidate_state_cache()


def set_case_done(scope: str, case_number: str, done: bool, user: str) -> None:
    _init_db()
    now_iso = dt.datetime.now().strftime("%m/%d %H:%M")
    with _DB_LOCK, closing(sqlite3.connect(DB_PATH)) as conn:
        if done:
            conn.execute(
                """
                INSERT INTO done_status (scope, case_number, done, updated_by, updated_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(scope, case_number) DO UPDATE SET
                    done=1, updated_by=excluded.updated_by, updated_at=excluded.updated_at
                """,
                (scope, case_number, user, now_iso),
            )
        else:
            conn.execute(
                "DELETE FROM done_status WHERE scope=? AND case_number=?",
                (scope, case_number),
            )
        conn.commit()
    _invalidate_state_cache()


def _lite_batch(b: dict) -> dict:
    """Summary-only view of a batch (no hits/overview rows) for months not on screen."""
    return {
        "scope": b["scope"],
        "jurisdiction": b["jurisdiction"],
        "label": b["label"],
        "month_key": b["month_key"],
        "court_code": b["court_code"],
        "hits": [],
        "overview": [],
        "hit_count": len(b["hits"]),
        "pdf_count": sum(len(h.get("docs", [])) for h in b["hits"]),
        "published_by": b["published_by"],
        "published_at": b["published_at"],
        "done": b["done"],
        "lite": True,
    }


def get_dashboard_state(sync_first: bool = False, full_scopes: set[str] | None = None) -> dict:
    """Return dashboard state. full_scopes=None returns every batch in full; otherwise only the
    listed scopes are full and the rest are lite summaries (see _lite_batch)."""
    global _CACHED_BASE_STATE
    if sync_first:
        sync_local_checkpoints_to_db()
    if _CACHED_BASE_STATE is None:
        with _DB_LOCK, closing(sqlite3.connect(DB_PATH)) as conn:
            batches = conn.execute(
                """
                SELECT scope, jurisdiction, label, payload_json, published_by, published_at
                FROM published_batches
                ORDER BY CASE WHEN jurisdiction='KY' THEN 0 ELSE 1 END ASC, scope DESC
                """
            ).fetchall()
            done_rows = conn.execute(
                "SELECT scope, case_number, updated_by, updated_at FROM done_status WHERE done=1"
            ).fetchall()
        done_map: dict[str, dict[str, dict[str, str]]] = {}
        for scope, cn, by, at in done_rows:
            done_map.setdefault(scope, {})[cn] = {"by": by, "at": at}
        out_batches = []
        for scope, jur, label, pjson, by, at in batches:
            data = json.loads(pjson)
            out_batches.append({
                "scope": scope,
                "jurisdiction": jur,
                "label": label,
                "month_key": data.get("month_key") or (scope.split("_")[-1] if "_" in scope else ""),
                "court_code": data.get("court_code") or (scope.split("_")[1] if len(scope.split("_")) > 1 else ""),
                "hits": data.get("hits", []),
                "overview": data.get("overview", []),
                "published_by": by,
                "published_at": at,
                "done": done_map.get(scope, {}),
            })
        _CACHED_BASE_STATE = {
            "batches": out_batches,
            "ky_months": [{"key": ym, "label": format_month_words(ym)} for ym in KY_MONTH_KEYS],
        }

    batches_out = _CACHED_BASE_STATE["batches"]
    if full_scopes is not None:
        batches_out = [b if b["scope"] in full_scopes else _lite_batch(b) for b in batches_out]

    return {
        "version": _STATE_VERSION,
        "batches": batches_out,
        "ky_months": _CACHED_BASE_STATE["ky_months"],
        "public_url": _PUBLIC_URL,
        "lan_url": f"http://{get_lan_ip()}:{_SERVER_PORT}",
        "scan_status": _SCAN_STATUS,
        "scan_progress": _SCAN_PROGRESS,
        "scan_logs": _SCAN_LOGS[-120:],
    }


def _trigger_live_ky_scan(court_code: str, ym: str, fresh: bool = False) -> None:
    global _SCAN_STATUS, _SCAN_PROGRESS
    if _SCAN_STATUS["running"]:
        return

    STOP_EVENT.clear()
    _SCAN_STATUS["running"] = f"{court_code}_{ym}"
    _SCAN_STATUS["progress"] = f"Fetching C-Track Document Search for {court_code} {ym}..."
    _SCAN_PROGRESS = {"val": 0, "max": 100, "lbl": "Connecting to C-Track...", "active_case": "", "hits": 0}
    _SCAN_LOGS.append({"text": f"=== KENTUCKY C-TRACK ({court_code}) — {format_month_words(ym)} ===", "tag": "head"})

    def _job() -> None:
        global _SCAN_STATUS, _SCAN_PROGRESS
        try:
            rows = fetch_ky_month_search_rows(court_code, ym)
            if STOP_EVENT.is_set():
                _SCAN_LOGS.append({"text": "Scan stopped by user.", "tag": "head"})
                return

            saved = {} if fresh else load_ky_checkpoint(court_code, ym)
            records = dict(saved)
            seen: dict[str, list[str]] = {}
            meta: dict[str, tuple[str, str, str]] = {}
            for r in rows:
                if not r.case_number or not r.case_id:
                    continue
                if r.case_number not in seen:
                    seen[r.case_number] = []
                    meta[r.case_number] = (r.case_id, r.court, r.filed_date_display)
                lbl = r.sub_type or r.document_name or r.doc_type
                if lbl and lbl not in seen[r.case_number]:
                    seen[r.case_number].append(lbl)

            total = len(seen)
            _SCAN_LOGS.append({"text": f"Found {len(rows)} Document Search row(s) across {total} unique case(s).", "tag": "head"})
            _SCAN_PROGRESS = {"val": 1, "max": 100, "lbl": f"Found {total} cases. Inspecting docket entries...", "active_case": "", "hits": 0}

            hit_count = sum(1 for r in records.values() if r.status == "BRIEF_FOUND")
            sync_ky_checkpoint_to_batch(court_code, ym, records, rows)

            for idx, (cn, subs) in enumerate(seen.items(), 1):
                if STOP_EVENT.is_set():
                    _SCAN_LOGS.append({"text": f"Scan stopped by user at case {idx}/{total}.", "tag": "head"})
                    break

                cid, crt, fdate = meta[cn]
                cached = records.get(cn)
                if (
                    not fresh
                    and cached
                    and cached.status in ("BRIEF_FOUND", "NO_BRIEF", "RECEIPT_ONLY")
                    and cached.first_filed_date == fdate
                    and cached.search_sub_types == subs
                ):
                    rec = cached
                else:
                    _SCAN_STATUS["progress"] = f"Scanning {idx}/{total}: {cn}..."
                    pct = round((idx / max(total, 1)) * 100)
                    _SCAN_PROGRESS = {
                        "val": pct,
                        "max": 100,
                        "lbl": f"Checked {idx}/{total} cases ({pct}%) • {cn} ({hit_count} Hits)",
                        "active_case": cn,
                        "hits": hit_count,
                    }
                    try:
                        rec = scan_ky_case(cid, cn, crt, fdate, subs)
                        records[cn] = rec
                        save_ky_checkpoint(court_code, ym, records, rows)
                        if rec.status == "BRIEF_FOUND":
                            hit_count = sum(1 for r in records.values() if r.status == "BRIEF_FOUND")
                            sync_ky_checkpoint_to_batch(court_code, ym, records, rows)
                    except Exception as err:
                        _SCAN_LOGS.append({"text": f"[ERROR] {cn}: could not be read ({err}) — SKIPPED.", "tag": "head"})
                        continue

                tag = "hit" if rec.status == "BRIEF_FOUND" else "dim"
                badge = "HIT  " if rec.status == "BRIEF_FOUND" else "SKIP "
                if rec.review_note:
                    tag, badge = "head", "CHECK"
                title_col = " ".join((rec.title or "Case").split())[:38]
                _SCAN_LOGS.append({
                    "text": f"[{idx:02d}/{total:02d}] [{badge}] {cn:<12} | {title_col:<38} | {rec.note}",
                    "tag": tag,
                })

            save_ky_checkpoint(court_code, ym, records, rows)
            sync_ky_checkpoint_to_batch(court_code, ym, records, rows)

            hits = [r for r in records.values() if r.status == "BRIEF_FOUND"]
            pdfs = sum(len(r.collectible_docs) for r in hits)
            _SCAN_LOGS.append({"text": "-" * 88, "tag": "dim"})
            _SCAN_LOGS.append({
                "text": f"KENTUCKY SCAN SUMMARY: {len(rows)} search rows • {total} unique cases • {len(hits)} Hit Cases • {pdfs} Collectible Brief PDFs",
                "tag": "head",
            })
            _SCAN_PROGRESS = {
                "val": 100,
                "max": 100,
                "lbl": f"Scan Complete • {len(hits)} Hit Cases ({pdfs} PDFs)",
                "active_case": "",
                "hits": len(hits),
            }
        except Exception as exc:
            _SCAN_LOGS.append({"text": f"[ERROR] Kentucky C-Track scan failed: {exc}", "tag": "head"})
            _SCAN_PROGRESS = {"val": 0, "max": 100, "lbl": f"Error: {exc}", "active_case": "", "hits": 0}
        finally:
            _SCAN_STATUS["running"] = ""
            _SCAN_STATUS["progress"] = ""

    threading.Thread(target=_job, daemon=True).start()


def generate_dashboard_html() -> str:
    """Generate dynamic HTML with embedded initial state and pre-populated month options for zero load lag."""
    state = get_dashboard_state(sync_first=False)
    ky_batches = {b["scope"]: b for b in state["batches"] if b["jurisdiction"] == "KY"}
    ny_batches = {b["scope"]: b for b in state["batches"] if b["jurisdiction"] == "NY"}

    # Build Kentucky Month Options
    ky_opts = []
    for ym in KY_MONTH_KEYS:
        scope = f"KY_KYCOA_{ym}"
        b = ky_batches.get(scope)
        month_word = format_month_words(ym)
        if b:
            pdf_cnt = sum(len(h["docs"]) for h in b["hits"])
            label = f"{month_word} ({len(b['hits'])} cases • {pdf_cnt} PDFs)"
        else:
            label = f"{month_word}"
        selected = ' selected="selected"' if ym == "2026-11" else ""
        ky_opts.append(f'<option value="{ym}"{selected}>{label}</option>')
    ky_opts_html = "\n".join(ky_opts)

    # Build New York Month Options
    ny_opts = []
    for scope, b in ny_batches.items():
        ym = b["month_key"]
        month_word = format_month_words(ym)
        pdf_cnt = sum(len(h["docs"]) for h in b["hits"])
        label = f"{month_word} ({len(b['hits'])} cases • {pdf_cnt} PDFs)"
        selected = ' selected="selected"' if ym == "2026-11" else ""
        ny_opts.append(f'<option value="{scope}"{selected}>{label}</option>')
    ny_opts_html = "\n".join(ny_opts)

    # Embed only the batches visible on first paint in full; every other month ships as a
    # tiny "lite" summary and is fetched on demand when selected (keeps first load fast
    # over slow WFH tunnels).
    ny_default = next((s for s, b in ny_batches.items() if b["month_key"] == "2026-11"), next(iter(ny_batches), ""))
    initial_state = get_dashboard_state(full_scopes={"KY_KYCOA_2026-11", ny_default})
    state_json = json.dumps(initial_state).replace("</script>", "<\\/script>")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BPM Court Scanner — WFH Team Web App</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>⚖️</text></svg>">
<style>
  :root {{
    --bg: #111111;
    --card: #1a1a1a;
    --card-active: #222222;
    --line: #2a2a2a;
    --text: #ededed;
    --muted: #8a8a8a;
    --accent: #4ade80;
    --accent-hover: #38c168;
    --selected: #2f3b33;
    --badge-bg: #1e3a29;
    --danger: #ef4444;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    font-size: 13px;
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }}

  /* Top Navigation Bar */
  .top-bar {{
    background: var(--bg);
    border-bottom: 1px solid var(--line);
    padding: 10px 20px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-shrink: 0;
    gap: 16px;
  }}
  .app-brand {{
    font-size: 16px;
    font-weight: 700;
    color: var(--text);
    display: flex;
    align-items: center;
    gap: 8px;
    white-space: nowrap;
  }}
  .switcher {{
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 3px;
    display: inline-flex;
    gap: 2px;
  }}
  .tab-btn {{
    background: transparent;
    border: none;
    color: var(--muted);
    font-size: 12px;
    font-weight: 600;
    padding: 6px 14px;
    border-radius: 4px;
    cursor: pointer;
    transition: all 0.15s ease;
  }}
  .tab-btn:hover {{ color: var(--text); }}
  .tab-btn.active {{
    background: var(--accent);
    color: #111111;
    font-weight: 700;
  }}

  .nav-right {{
    display: flex;
    align-items: center;
    gap: 12px;
    margin-left: auto;
  }}
  .user-box {{
    display: flex;
    align-items: center;
    gap: 6px;
    color: var(--muted);
    font-size: 12px;
  }}
  .user-box input {{
    background: var(--card);
    border: 1px solid var(--line);
    color: var(--text);
    padding: 5px 8px;
    border-radius: 4px;
    width: 110px;
    font-size: 12px;
  }}
  .status-pill {{
    color: var(--accent);
    font-weight: 600;
    font-size: 12px;
    display: flex;
    align-items: center;
    gap: 6px;
  }}

  /* WFH Public Link Share Banner */
  .share-banner {{
    background: #14281a;
    border-bottom: 1px solid #234d31;
    padding: 6px 20px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 12px;
    flex-shrink: 0;
  }}
  .share-url {{
    font-family: Consolas, monospace;
    color: var(--accent);
    font-weight: 700;
    margin: 0 4px;
  }}

  /* Content Views */
  .view-container {{
    flex: 1;
    overflow: hidden;
    position: relative;
  }}
  .view-pane {{
    position: absolute;
    inset: 0;
    display: none;
    overflow: hidden;
  }}
  .view-pane.active {{ display: flex; }}

  /* HOME VIEW */
  #homeView {{
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 40px 20px;
    text-align: center;
    overflow-y: auto;
  }}
  .home-title {{ font-size: 20px; font-weight: 700; margin-bottom: 6px; }}
  .home-subtitle {{ color: var(--muted); margin-bottom: 30px; font-size: 14px; }}
  .home-cards {{
    display: flex;
    gap: 24px;
    max-width: 960px;
    width: 100%;
    justify-content: center;
    flex-wrap: wrap;
  }}
  .jurisdiction-card {{
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 28px 24px;
    width: 440px;
    text-align: left;
    display: flex;
    flex-direction: column;
    box-shadow: 0 4px 16px rgba(0,0,0,0.4);
  }}
  .jurisdiction-card:hover {{ border-color: #3b5042; }}
  .card-title {{ font-size: 15px; font-weight: 700; margin-bottom: 8px; }}
  .card-sub1 {{ color: var(--accent); font-weight: 600; font-size: 12px; margin-bottom: 4px; }}
  .card-sub2 {{ color: var(--muted); font-size: 12px; margin-bottom: 24px; min-height: 36px; }}

  /* SPLIT 2-COLUMN LAYOUT (Kentucky / New York) */
  .split-layout {{
    display: flex;
    width: 100%;
    height: 100%;
    overflow: hidden;
  }}
  .left-col {{
    width: 580px;
    border-right: 1px solid var(--line);
    display: flex;
    flex-direction: column;
    padding: 14px 16px;
    background: var(--bg);
    flex-shrink: 0;
    overflow-y: auto;
  }}
  .right-col {{
    flex: 1;
    display: flex;
    flex-direction: column;
    padding: 14px 18px;
    background: var(--bg);
    overflow: hidden;
  }}

  /* Form controls & Buttons */
  .control-row {{
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 10px;
    flex-wrap: wrap;
  }}
  label.ctrl-lbl {{ color: var(--muted); font-size: 12px; }}
  select, input.text-input {{
    background: var(--card);
    border: 1px solid var(--line);
    color: var(--text);
    padding: 6px 10px;
    border-radius: 4px;
    font-size: 12px;
  }}
  select:focus, input.text-input:focus {{ outline: none; border-color: var(--accent); }}

  .btn {{
    background: var(--card);
    border: 1px solid var(--line);
    color: var(--text);
    padding: 6px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
    font-weight: 600;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    transition: background 0.15s, border-color 0.15s;
    user-select: none;
    text-decoration: none;
  }}
  .btn:hover {{ border-color: var(--accent); background: var(--card-active); }}
  .btn-primary {{
    background: var(--accent);
    color: #111111;
    font-weight: 700;
    border-color: var(--accent);
  }}
  .btn-primary:hover {{ background: var(--accent-hover); border-color: var(--accent-hover); }}

  /* Scanned cases / batches table in left column */
  .batch-table-wrap {{
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 4px;
    height: 350px;
    min-height: 300px;
    max-height: 460px;
    overflow-y: auto;
    overflow-x: hidden;
    margin-bottom: 10px;
    box-sizing: border-box;
  }}
  .mini-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
    table-layout: fixed;
  }}
  .mini-table th, .mini-table td {{
    padding: 7px 10px;
    border-bottom: 1px solid var(--line);
    text-align: left;
    box-sizing: border-box;
  }}
  .mini-table th {{ background: #151515; color: var(--muted); position: sticky; top: 0; z-index: 1; }}
  .mini-table tr.selected-row {{ background: #1a3322 !important; border-left: 3px solid var(--accent); }}
  .mini-table tr.active-row {{ background: #233527; font-weight: 600; }}
  .mini-table tr:hover:not(.active-row):not(.selected-row) {{ background: #1f1f1f; cursor: pointer; }}

  /* Progress bar */
  .prog-wrap {{
    margin-bottom: 10px;
  }}
  .prog-track {{
    background: #151515;
    border: 1px solid var(--line);
    border-radius: 4px;
    height: 14px;
    overflow: hidden;
    margin-bottom: 6px;
  }}
  .prog-fill {{
    background: var(--accent);
    height: 100%;
    width: 0%;
    transition: width 0.2s ease;
  }}
  .prog-lbl {{
    color: var(--muted);
    font-size: 11px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}

  /* Scan Log console box */
  .log-title {{
    font-weight: 700;
    font-size: 12px;
    margin-bottom: 6px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }}
  .log-box {{
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 4px;
    padding: 10px 12px;
    font-family: Consolas, monospace;
    font-size: 11px;
    line-height: 1.5;
    flex: 1;
    min-height: 180px;
    overflow-y: auto;
    color: var(--muted);
    white-space: pre-wrap;
    word-break: break-all;
  }}
  .log-line-head {{ color: var(--text); font-weight: 700; }}
  .log-line-hit {{ color: var(--accent); font-weight: 700; }}
  .log-line-dim {{ color: #666666; }}

  /* Right column: Hit-List Table & Selected Case Drawer */
  .hit-header {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 8px;
    flex-shrink: 0;
  }}
  .hit-title {{
    font-size: 14px;
    font-weight: 700;
    color: var(--accent);
  }}
  .hit-stats {{ color: var(--muted); font-size: 12px; }}

  .filter-bar {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 10px;
    flex-shrink: 0;
  }}

  .table-wrap {{
    flex: 1;
    overflow: auto;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 4px;
  }}
  table.main-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }}
  table.main-table th, table.main-table td {{
    padding: 7px 10px;
    border-bottom: 1px solid var(--line);
    text-align: left;
    white-space: nowrap;
  }}
  table.main-table th {{
    background: #151515;
    color: var(--muted);
    position: sticky;
    top: 0;
    z-index: 2;
  }}
  table.main-table tr {{ cursor: pointer; }}
  table.main-table tr:hover {{ background: #1e1e1e; }}
  table.main-table tr.selected-row {{ background: var(--selected) !important; color: #ffffff; }}
  table.main-table tr.done-row {{ opacity: 0.55; }}

  /* Selected Case Briefs Drawer (Bottom) */
  .case-drawer {{
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 10px 14px;
    margin-top: 8px;
    flex-shrink: 0;
  }}
  .case-drawer-title {{
    font-weight: 700;
    font-size: 12px;
    color: var(--text);
    margin-bottom: 8px;
    word-break: break-word;
  }}
  .brief-row {{
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 5px;
    background: transparent;
    padding: 2px 0;
  }}
  .brief-save-btn {{
    background: var(--accent);
    color: #111111;
    font-family: Consolas, monospace;
    font-weight: 700;
    font-size: 12px;
    padding: 4px 12px;
    border-radius: 4px;
    border: none;
    cursor: pointer;
    white-space: nowrap;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    transition: filter 0.15s ease, transform 0.05s ease;
  }}
  .brief-save-btn:hover {{
    filter: brightness(1.1);
  }}
  .brief-save-btn:active {{
    transform: scale(0.98);
  }}
  .brief-meta {{
    font-size: 12px;
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}

  /* Bottom Actions Bar */
  .actions-bar {{
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 10px;
    flex-shrink: 0;
    flex-wrap: wrap;
  }}

  /* Toast Notification */
  #toast {{
    position: fixed;
    bottom: 24px;
    right: 24px;
    background: var(--accent);
    color: #111111;
    font-weight: 700;
    font-size: 12px;
    padding: 10px 18px;
    border-radius: 6px;
    display: none;
    z-index: 999;
    box-shadow: 0 4px 14px rgba(0,0,0,0.5);
  }}

  /* Modal Overlay */
  .modal-backdrop {{
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0, 0, 0, 0.75);
    z-index: 1000;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
  }}
  .modal-box {{
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 8px;
    width: 960px;
    max-width: 96vw;
    max-height: 85vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 10px 30px rgba(0,0,0,0.8);
    overflow: hidden;
  }}
  .modal-head {{
    padding: 12px 18px;
    border-bottom: 1px solid var(--line);
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: #151515;
  }}
  .modal-body {{
    padding: 14px 18px;
    overflow-y: auto;
    flex: 1;
  }}
</style>
</head>
<body>

<!-- TOP NAVIGATION BAR -->
<div class="top-bar">
  <div class="app-brand">
    BPM Court Scanner
  </div>
  <div class="switcher">
    <button class="tab-btn" id="btnTabHome" onclick="switchTab('home')">Home</button>
    <button class="tab-btn" id="btnTabNy" onclick="switchTab('ny')">New York — AD2 (NYSCEF)</button>
    <button class="tab-btn active" id="btnTabKy" onclick="switchTab('ky')">Kentucky — C-Track (KYCOA / KYSUP)</button>
  </div>
  <div class="nav-right">
    <div class="user-box">
      <label>Your Name:</label>
      <input type="text" id="userNameInput" placeholder="Edward">
    </div>
    <div class="status-pill" id="globalStatusPill">● Kentucky Ready</div>
    <button class="btn btn-primary" onclick="copyWfhLink()">📋 Copy WFH Link</button>
  </div>
</div>

<!-- MAIN VIEW CONTAINER -->
<div class="view-container">

  <!-- HOME VIEW -->
  <div class="view-pane" id="homeView">
    <div class="home-title">Select a Court Jurisdiction</div>
    <div class="home-subtitle">Choose which state court workflow you want to open:</div>
    <div class="home-cards">
      <div class="jurisdiction-card">
        <div class="card-title">New York — Appellate Division, 2nd Dept</div>
        <div class="card-sub1">NYSCEF Motion List Calendar & Case Search</div>
        <div class="card-sub2">Collects Briefs, Petitions/Writs, and Qualifying Motions from Motion List calendars.</div>
        <button class="btn btn-primary" onclick="switchTab('ny')">Open New York Scanner →</button>
      </div>
      <div class="jurisdiction-card">
        <div class="card-title">Kentucky — C-Track Public Access</div>
        <div class="card-sub1">Court of Appeals (KYCOA) & Supreme Court (KYSUP)</div>
        <div class="card-sub2">Auto-filters Receipt Notices, generates Save-As Filenames & downloads briefs with 1-click.</div>
        <button class="btn btn-primary" onclick="switchTab('ky')">Open Kentucky Scanner →</button>
      </div>
    </div>
  </div>

  <!-- KENTUCKY VIEW -->
  <div class="view-pane active" id="kyView">
    <div class="split-layout">
      <!-- Left Column: Controls & Live Scan Log -->
      <div class="left-col">
        <div class="control-row">
          <label class="ctrl-lbl">Court:</label>
          <select id="kyCourtSelect" onchange="onKyMonthChange()">
            <option value="KYCOA">Kentucky Court of Appeals (KYCOA)</option>
            <option value="KYSUP">Kentucky Supreme Court (KYSUP)</option>
          </select>
        </div>
        <div class="control-row">
          <label class="ctrl-lbl">Month:</label>
          <select id="kyMonthSelect" style="flex:1;" onchange="onKyMonthChange()">
            {ky_opts_html}
          </select>
          <button class="btn" id="btnKyLoad" onclick="loadKyMonthCases()">Load</button>
        </div>

        <div class="batch-table-wrap">
          <table class="mini-table">
            <colgroup>
              <col style="width:84px;">
              <col style="width:105px;">
              <col>
              <col style="width:120px;">
            </colgroup>
            <thead>
              <tr>
                <th>Date</th>
                <th>Case #</th>
                <th>Sub-Type</th>
                <th style="text-align:right;">Status</th>
              </tr>
            </thead>
            <tbody id="kyBatchesTbody"></tbody>
          </table>
        </div>

        <div class="control-row" style="margin-top:4px;">
          <button class="btn btn-primary" id="btnStartScan" onclick="triggerKyLiveScan()">▶ Start Scan</button>
          <button class="btn" id="btnStopScan" onclick="stopKyScan()">Stop</button>
          <label style="color:var(--muted);font-size:11px;cursor:pointer;margin-left:4px;">
            <input type="checkbox" id="chkKyFresh"> Re-scan from scratch (verify)
          </label>
          <div style="flex:1;"></div>
          <span id="kySummaryLbl" style="color:var(--muted);font-size:11px;"></span>
        </div>

        <div class="prog-wrap">
          <div class="prog-track">
            <div class="prog-fill" id="kyProgFill"></div>
          </div>
          <div class="prog-lbl" id="kyProgLbl">Ready</div>
        </div>

        <div class="log-title">
          <span>Scan Log</span>
          <button class="btn" style="padding:2px 8px;font-size:11px;" onclick="clearScanLog()">Clear</button>
        </div>
        <div class="log-box" id="kyLogBox"></div>
      </div>

      <!-- Right Column: Hit-List & Save-As Table + Briefs Drawer -->
      <div class="right-col">
        <div class="hit-header">
          <div class="hit-title" id="kyHitTitle">Hit-List & Save-As Names (0 cases • 0 PDFs)</div>
        </div>

        <div class="filter-bar">
          <div style="display:flex;align-items:center;gap:8px;">
            <label class="ctrl-lbl">Search:</label>
            <input type="text" class="text-input" id="kySearchBox" placeholder="Case #, title, or filename..." style="width:220px;" oninput="onKySearchInput()">
            <label style="color:var(--muted);font-size:12px;cursor:pointer;">
              <input type="checkbox" id="kyHideDone" onchange="onKyHideDoneChange()"> Hide downloaded
            </label>
          </div>
          <div class="hit-stats" id="kyStatsLbl"></div>
        </div>

        <div class="table-wrap">
          <table class="main-table">
            <thead>
              <tr>
                <th style="width:36px;text-align:center;"><input type="checkbox" id="kyHeadCheck" onchange="toggleSelectAll(this.checked)"></th>
                <th style="width:68px;">Done</th>
                <th style="width:90px;">Doc Date</th>
                <th style="width:110px;">Case #</th>
                <th style="width:55px;">PDFs</th>
                <th>Standard Save-As Filename(s)</th>
              </tr>
            </thead>
            <tbody id="kyHitsTbody"></tbody>
          </table>
        </div>

        <!-- Selected Case Briefs Drawer (Bottom) -->
        <div class="case-drawer" id="kyCaseDrawer">
          <div class="case-drawer-title" id="kyDrawerTitle">Select a case above to view collectible brief PDFs</div>
          <div id="kyDrawerBriefsList"></div>
        </div>

        <!-- Bottom Actions Bar -->
        <div class="actions-bar">
          <button class="btn" id="btnSelectAll" onclick="toggleSelectAll()">☑ Select All</button>
          <button class="btn btn-primary" id="btnDownloadZip" onclick="downloadCheckedOrAllZip()">⬇ Download All PDFs</button>
          <button class="btn" onclick="downloadSelectedCasePdfs()">⬇ Auto-Save Case PDFs</button>
          <button class="btn" onclick="toggleSelectedDone()">✓ Mark / Unmark</button>
          <button class="btn" onclick="copyTrackerCsv()">Copy Tracker CSV</button>
          <button class="btn" id="btnShowAllPdfs" onclick="showAllBriefPdfs()">Show All Brief PDFs</button>
          <div style="flex:1;"></div>
          <button class="btn" onclick="openSelectedInCtrack()">Open Case in Browser ↗</button>
        </div>
      </div>
    </div>
  </div>

  <!-- NEW YORK VIEW -->
  <div class="view-pane" id="nyView">
    <div class="split-layout">
      <!-- Left Column: NY Month selector & Motion Lists -->
      <div class="left-col">
        <div class="control-row">
          <label class="ctrl-lbl">Month:</label>
          <select id="nyMonthSelect" style="flex:1;" onchange="onNyMonthChange()">
            {ny_opts_html}
          </select>
          <button class="btn" onclick="onNyMonthChange()">Load</button>
        </div>
        <div class="batch-table-wrap" style="height:350px;min-height:300px;max-height:460px;">
          <table class="mini-table">
            <colgroup>
              <col>
              <col style="width:75px;text-align:center;">
              <col style="width:85px;text-align:right;">
            </colgroup>
            <thead>
              <tr>
                <th>New York Batches</th>
                <th style="text-align:center;">Cases</th>
                <th style="text-align:right;">PDFs</th>
              </tr>
            </thead>
            <tbody id="nyBatchesTbody"></tbody>
          </table>
        </div>
        <div style="color:var(--muted);font-size:11px;margin-top:8px;line-height:1.5;">
          * Note: New York motion lists with captcha require Edge browser on Edward's PC for initial scraping. Once scanned, all hit briefs can be opened and downloaded here by any teammate!
        </div>
      </div>

      <!-- Right Column: NY Hit-List -->
      <div class="right-col">
        <div class="hit-header">
          <div class="hit-title" id="nyHitTitle">New York Hit-List (0 cases)</div>
        </div>
        <div class="filter-bar">
          <input type="text" class="text-input" id="nySearchBox" placeholder="Search NY case # or title..." style="width:240px;" oninput="onNySearchInput()">
          <label style="color:var(--muted);font-size:12px;cursor:pointer;">
            <input type="checkbox" id="nyHideDone" onchange="onNyHideDoneChange()"> Hide downloaded
          </label>
        </div>
        <div class="table-wrap">
          <table class="main-table">
            <thead>
              <tr>
                <th style="width:68px;">Done</th>
                <th style="width:90px;">Date</th>
                <th style="width:110px;">Case #</th>
                <th>Case Title & Briefs</th>
              </tr>
            </thead>
            <tbody id="nyHitsTbody"></tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

</div>

<!-- TOAST -->
<div id="toast"></div>

<!-- ALL PDFS MODAL -->
<div id="allPdfsModal" class="modal-backdrop" style="display:none;" onclick="if(event.target===this)closeAllPdfsModal()">
  <div class="modal-box">
    <div class="modal-head">
      <div id="modalTitle" style="font-weight:700;font-size:13px;color:var(--text);">All Kentucky Collectible PDFs</div>
      <button class="btn" style="padding:2px 8px;" onclick="closeAllPdfsModal()">✕</button>
    </div>
    <div style="padding:8px 18px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:12px;">
      <input type="text" class="text-input" id="modalSearch" placeholder="Filter PDFs by case or name..." style="width:260px;" oninput="filterModalPdfs()">
      <div style="display:flex;gap:8px;">
        <button class="btn" onclick="copyAllModalSaveNames()">📋 Copy All Filenames</button>
        <button class="btn btn-primary" onclick="closeAllPdfsModal()">Done</button>
      </div>
    </div>
    <div class="modal-body" style="padding:0;">
      <table class="main-table" style="width:100%;">
        <thead>
          <tr>
            <th style="width:40px;">#</th>
            <th style="width:110px;">Case #</th>
            <th>Save-As Name</th>
            <th>Doc Name</th>
            <th style="width:90px;">Filed</th>
            <th>Submitter</th>
          </tr>
        </thead>
        <tbody id="modalPdfsTbody"></tbody>
      </table>
    </div>
  </div>
</div>

<script>
let STATE = {state_json};
let CHECKED = new Set();
let SELECTED_CASE = null;
let CURRENT_TAB = localStorage.getItem('bpm_active_tab') || 'ky';

const userNameEl = document.getElementById('userNameInput');
userNameEl.value = localStorage.getItem('bpm_team_user') || 'Edward';
userNameEl.addEventListener('input', () => localStorage.setItem('bpm_team_user', userNameEl.value.trim() || 'Edward'));

function toast(msg) {{
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.style.display = 'block';
  setTimeout(() => {{ el.style.display = 'none'; }}, 2400);
}}

function copyWfhLink() {{
  const url = STATE.public_url || STATE.lan_url || window.location.origin;
  navigator.clipboard.writeText(url);
  toast('Copied WFH Public Link: ' + url);
}}

function switchTab(tab) {{
  CURRENT_TAB = tab;
  localStorage.setItem('bpm_active_tab', tab);
  ['home', 'ny', 'ky'].forEach(t => {{
    document.getElementById(t + 'View').classList.toggle('active', t === tab);
    const b = document.getElementById('btnTab' + t.charAt(0).toUpperCase() + t.slice(1));
    if (b) b.classList.toggle('active', t === tab);
  }});
  if (tab === 'ky') renderKyTable();
  if (tab === 'ny') renderNyTable();
}}

function currentKyScope() {{
  const court = document.getElementById('kyCourtSelect').value || 'KYCOA';
  const ym = document.getElementById('kyMonthSelect').value || '2026-11';
  return `KY_${{court}}_${{ym}}`;
}}

function currentKyBatch() {{
  const scope = currentKyScope();
  return STATE.batches.find(b => b.scope === scope) || null;
}}

function currentNyBatch() {{
  const sel = document.getElementById('nyMonthSelect');
  const scope = sel ? sel.value : '';
  const ny = STATE.batches.filter(b => b.jurisdiction === 'NY');
  return ny.find(b => b.scope === scope) || ny[0] || null;
}}

let LAST_STATE_VERSION = STATE.version || 0;
let LAST_KY_SIG = '';
let LAST_DRAWER_CASE = '';
let LAST_LOG_COUNT = 0;
let FETCH_SEQ = 0;

function visibleScopes() {{
  const scopes = [currentKyScope()];
  const nySel = document.getElementById('nyMonthSelect');
  if (nySel && nySel.value) scopes.push(nySel.value);
  return scopes;
}}

// Only the months on screen are downloaded in full; other months arrive as small "lite"
// summaries and are fetched on demand. Stale responses (older request) are ignored, and an
// identical request already in flight is reused instead of downloading the same month twice.
let FETCH_INFLIGHT = null;
function fetchState(reuse = false) {{
  const key = visibleScopes().join(',');
  if (reuse && FETCH_INFLIGHT && FETCH_INFLIGHT.key === key) return FETCH_INFLIGHT.promise;
  const promise = doFetchState(key).finally(() => {{
    if (FETCH_INFLIGHT && FETCH_INFLIGHT.promise === promise) FETCH_INFLIGHT = null;
  }});
  FETCH_INFLIGHT = {{ key, promise }};
  return promise;
}}

async function doFetchState(key) {{
  const seq = ++FETCH_SEQ;
  try {{
    const scopes = key.split(',').filter(Boolean).map(encodeURIComponent).join(',');
    const res = await fetch(`/api/state?lite=1&scopes=${{scopes}}`);
    if (!res.ok || seq !== FETCH_SEQ) return;
    const next = await res.json();
    if (seq !== FETCH_SEQ) return;
    // Keep already-downloaded full batches when nothing changed on the server.
    if (next.version === STATE.version && Array.isArray(STATE.batches)) {{
      next.batches = next.batches.map(b => {{
        if (!b.lite) return b;
        const prev = STATE.batches.find(p => p.scope === b.scope && !p.lite);
        return prev || b;
      }});
    }}
    STATE = next;
    LAST_STATE_VERSION = STATE.version || 0;
    applyStateToUi();
  }} catch (err) {{}}
}}

function batchHitCount(b) {{ return b.lite ? (b.hit_count || 0) : (b.hits || []).length; }}
function batchPdfCount(b) {{
  return b.lite ? (b.pdf_count || 0) : (b.hits || []).reduce((acc, h) => acc + (h.docs ? h.docs.length : 0), 0);
}}

function applyScanProgressToUi() {{
  const isScanning = !!(STATE.scan_status && STATE.scan_status.running);
  const statusPill = document.getElementById('globalStatusPill');
  const startBtn = document.getElementById('btnStartScan');
  const stopBtn = document.getElementById('btnStopScan');
  const progFill = document.getElementById('kyProgFill');
  const progLbl = document.getElementById('kyProgLbl');
  const monthSel = document.getElementById('kyMonthSelect');
  const courtSel = document.getElementById('kyCourtSelect');

  if (isScanning) {{
    statusPill.textContent = '● Scanning ' + STATE.scan_status.running;
    statusPill.style.color = '#f59e0b';
    startBtn.disabled = true;
    startBtn.style.opacity = '0.6';
    startBtn.innerHTML = `⏳ Scanning... (${{STATE.scan_progress.val || 0}}%)`;
    stopBtn.disabled = false;
    stopBtn.style.background = '#3b1c1c';
    stopBtn.style.color = '#ef4444';
    stopBtn.style.borderColor = '#ef4444';
    monthSel.disabled = true;
    courtSel.disabled = true;
    progFill.style.width = (STATE.scan_progress.val || 0) + '%';
    progLbl.innerHTML = `<strong>${{STATE.scan_progress.lbl || 'Scanning...'}}</strong>`;
    progLbl.style.color = 'var(--accent)';
  }} else {{
    statusPill.textContent = '● Kentucky Ready';
    statusPill.style.color = 'var(--accent)';
    startBtn.disabled = false;
    startBtn.style.opacity = '1';
    startBtn.innerHTML = '▶ Start Scan';
    stopBtn.disabled = true;
    stopBtn.style.background = 'var(--card)';
    stopBtn.style.color = 'var(--text)';
    stopBtn.style.borderColor = 'var(--line)';
    monthSel.disabled = false;
    courtSel.disabled = false;
    if (STATE.scan_progress && STATE.scan_progress.val === 100) {{
      progFill.style.width = '100%';
      progLbl.textContent = STATE.scan_progress.lbl || 'Scan Complete';
      progLbl.style.color = 'var(--accent)';
    }}
  }}

  updateScanLogs();
}}

function applyStateToUi() {{
  applyScanProgressToUi();

  if (CURRENT_TAB === 'ky') renderKyTable();
  if (CURRENT_TAB === 'ny') renderNyTable();
}}

function updateScanLogs() {{
  const logBox = document.getElementById('kyLogBox');
  if (!logBox) return;
  const logs = STATE.scan_logs || [];
  if (logs.length === LAST_LOG_COUNT) return;
  LAST_LOG_COUNT = logs.length;
  const isAtBottom = logBox.scrollHeight - logBox.scrollTop <= logBox.clientHeight + 60;
  logBox.innerHTML = logs.map(l => {{
    const cls = l.tag === 'hit' ? 'log-line-hit' : (l.tag === 'head' ? 'log-line-head' : 'log-line-dim');
    return `<div class="${{cls}}">${{l.text}}</div>`;
  }}).join('');
  if (isAtBottom) logBox.scrollTop = logBox.scrollHeight;
}}

async function loadKyMonthCases() {{
  const court = document.getElementById('kyCourtSelect').value || 'KYCOA';
  const ym = document.getElementById('kyMonthSelect').value || '2026-11';
  const loadBtn = document.getElementById('btnKyLoad');
  const progLbl = document.getElementById('kyProgLbl');
  const progFill = document.getElementById('kyProgFill');
  const statusPill = document.getElementById('globalStatusPill');
  const leftTbody = document.getElementById('kyBatchesTbody');
  const rightTbody = document.getElementById('kyHitsTbody');

  if (loadBtn) {{ loadBtn.disabled = true; loadBtn.innerHTML = '⏳ Loading...'; }}
  if (statusPill) {{ statusPill.textContent = `● Loading ${{court}} ${{ym}}...`; statusPill.style.color = '#f59e0b'; }}
  if (progLbl) {{ progLbl.innerHTML = `<strong>⏳ Fetching Document Search cases from C-Track (${{court}} ${{ym}})... Please wait...</strong>`; progLbl.style.color = 'var(--accent)'; }}
  if (progFill) {{ progFill.style.width = '35%'; }}

  if (leftTbody) {{
    leftTbody.innerHTML = `
      <tr>
        <td colspan="4" style="text-align:center;padding:50px 10px;color:var(--accent);font-size:13px;">
          <div style="font-size:24px;margin-bottom:8px;">⏳</div>
          <strong>Fetching C-Track Document Search Cases...</strong>
          <div style="font-size:11px;color:var(--muted);margin-top:6px;">Connecting to public court database for ${{court}} ${{ym}}. Please wait...</div>
        </td>
      </tr>
    `;
  }}
  if (rightTbody) {{
    rightTbody.innerHTML = `
      <tr>
        <td colspan="6" style="text-align:center;padding:110px 10px;color:var(--accent);font-size:13px;">
          <div style="font-size:28px;margin-bottom:10px;">⏳</div>
          <strong style="color:var(--accent);font-size:14px;">Loading C-Track Cases & Briefs (${{court}} ${{ym}})...</strong>
          <div style="font-size:12px;color:var(--muted);margin-top:6px;">Retrieving court docket records. Please wait...</div>
        </td>
      </tr>
    `;
  }}

  toast(`⏳ Loading cases from C-Track (${{court}} ${{ym}})...`);
  try {{
    const res = await fetch('/api/ky/load', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ court_code: court, ym }})
    }});
    const data = await res.json();
    LAST_KY_SIG = '';
    await fetchState();
    toast(`✅ Successfully loaded cases for ${{court}} ${{ym}}!`);
  }} catch (err) {{
    toast('⚠ Failed to fetch cases: ' + err);
  }} finally {{
    if (loadBtn) {{ loadBtn.disabled = false; loadBtn.textContent = 'Load'; }}
    if (statusPill && (!STATE.scan_status || !STATE.scan_status.running)) {{
      statusPill.textContent = '● Kentucky Ready';
      statusPill.style.color = 'var(--accent)';
    }}
  }}
}}

let SEARCH_TIMER = null;
function onKySearchInput() {{
  clearTimeout(SEARCH_TIMER);
  SEARCH_TIMER = setTimeout(() => {{
    LAST_KY_SIG = '';
    renderKyTable();
  }}, 100);
}}

function onKyHideDoneChange() {{
  LAST_KY_SIG = '';
  renderKyTable();
}}

async function onKyMonthChange() {{
  CHECKED.clear();
  SELECTED_CASE = null;
  LAST_KY_SIG = '';
  LAST_DRAWER_CASE = '';
  renderKyTable();
  const batch = currentKyBatch();
  if (batch && batch.lite) await fetchState(true);
}}

let NY_SEARCH_TIMER = null;
function onNySearchInput() {{
  clearTimeout(NY_SEARCH_TIMER);
  NY_SEARCH_TIMER = setTimeout(() => {{
    LAST_NY_SIG = '';
    renderNyTable();
  }}, 100);
}}

function onNyHideDoneChange() {{
  LAST_NY_SIG = '';
  renderNyTable();
}}

async function onNyMonthChange() {{
  LAST_NY_SIG = '';
  renderNyTable();
  const batch = currentNyBatch();
  if (!batch || batch.lite) await fetchState(true);
}}

function selectKyCase(cn) {{
  if (SELECTED_CASE === cn) return;
  SELECTED_CASE = cn;
  document.querySelectorAll('#kyBatchesTbody tr').forEach(r => {{
    r.classList.toggle('selected-row', r.getAttribute('data-case') === cn);
  }});
  document.querySelectorAll('#kyHitsTbody tr').forEach(r => {{
    r.classList.toggle('selected-row', r.getAttribute('data-case') === cn);
  }});
  renderKyDrawer();
}}

function getKyFilteredHits(batch) {{
  if (!batch) return [];
  const q = (document.getElementById('kySearchBox').value || '').trim().toLowerCase();
  const hideDone = document.getElementById('kyHideDone').checked;
  return batch.hits.filter(h => {{
    if (hideDone && batch.done && batch.done[h.case_number]) return false;
    if (!q) return true;
    const hay = `${{h.case_number}} ${{h.title}} ${{h.docs.map(d => d.save_as_name).join(' ')}}`.toLowerCase();
    return hay.includes(q);
  }});
}}

function updateKyHeaderStats(batch) {{
  if (!batch) return;
  const overviewList = batch.overview && batch.overview.length ? batch.overview : batch.hits;
  const totalPdfs = batch.hits.reduce((acc, h) => acc + h.docs.length, 0);
  const doneCases = batch.hits.filter(h => batch.done && batch.done[h.case_number]).length;
  const donePdfs = batch.hits.filter(h => batch.done && batch.done[h.case_number]).reduce((acc, h) => acc + h.docs.length, 0);

  document.getElementById('kyHitTitle').textContent = `Hit-List & Save-As Names (${{batch.hits.length}} cases • ${{totalPdfs}} PDFs)`;
  document.getElementById('kyStatsLbl').textContent = `Selected: ${{CHECKED.size}}  •  Done: ${{doneCases}}/${{batch.hits.length}} cases (${{donePdfs}}/${{totalPdfs}} PDFs)`;
  document.getElementById('btnShowAllPdfs').textContent = `Show All Brief PDFs (${{totalPdfs}})`;

  if (document.getElementById('kySummaryLbl')) {{
    document.getElementById('kySummaryLbl').textContent = `${{overviewList.length}} cases (${{overviewList.length}} rows)`;
  }}
  if (document.getElementById('kyProgLbl') && (!STATE.scan_status || !STATE.scan_status.running)) {{
    document.getElementById('kyProgLbl').textContent = `Ready • ${{overviewList.length}} unique cases (${{overviewList.length}} search rows) • ${{batch.hits.length}} hit cases (${{totalPdfs}} collectible PDFs)`;
  }}
  document.getElementById('btnDownloadZip').textContent = CHECKED.size > 0
    ? `⬇ Download Selected (${{CHECKED.size}})`
    : `⬇ Download All PDFs (ZIP)`;
}}

function renderKyTable() {{
  const batch = currentKyBatch();
  const q = (document.getElementById('kySearchBox').value || '').trim().toLowerCase();
  const hideDone = document.getElementById('kyHideDone').checked;
  const doneKeys = batch && batch.done ? Object.keys(batch.done).sort().join(',') : '';
  const currentSig = batch
    ? `${{batch.scope}}|${{batch.lite ? 'lite' : 'full'}}|${{batch.hits.length}}|${{(batch.overview||[]).length}}|${{doneKeys}}|${{q}}|${{hideDone}}`
    : 'EMPTY';

  if (batch && batch.lite) {{
    if (currentSig === LAST_KY_SIG) return;
    LAST_KY_SIG = currentSig;
    const n = batch.hit_count || 0, p = batch.pdf_count || 0;
    document.getElementById('kyHitTitle').textContent = `Hit-List & Save-As Names (${{n}} cases • ${{p}} PDFs)`;
    document.getElementById('kyBatchesTbody').innerHTML = `
      <tr><td colspan="4" style="text-align:center;padding:45px 10px;color:var(--accent);font-size:12px;">
        <div style="font-size:22px;margin-bottom:6px;">⏳</div><strong>Loading saved cases for this month...</strong>
      </td></tr>`;
    document.getElementById('kyHitsTbody').innerHTML = `
      <tr><td colspan="6" style="text-align:center;padding:110px 10px;color:var(--accent);font-size:13px;">
        <div style="font-size:28px;margin-bottom:10px;">⏳</div>
        <strong style="font-size:14px;">Loading ${{n}} cases • ${{p}} PDFs...</strong>
        <div style="font-size:12px;color:var(--muted);margin-top:6px;">Downloading this month's saved hit-list from the team server.</div>
      </td></tr>`;
    return;
  }}

  if (currentSig === LAST_KY_SIG && batch) {{
    // Fast path: Data did not change. Only sync selection highlight and stats in 0ms!
    document.querySelectorAll('#kyBatchesTbody tr').forEach(r => {{
      r.classList.toggle('selected-row', r.getAttribute('data-case') === SELECTED_CASE);
    }});
    document.querySelectorAll('#kyHitsTbody tr').forEach(r => {{
      r.classList.toggle('selected-row', r.getAttribute('data-case') === SELECTED_CASE);
    }});
    updateKyHeaderStats(batch);
    renderKyDrawer();
    return;
  }}
  LAST_KY_SIG = currentSig;

  const tbody = document.getElementById('kyHitsTbody');
  tbody.innerHTML = '';

  const leftTbody = document.getElementById('kyBatchesTbody');
  leftTbody.innerHTML = '';

  if (!batch) {{
    document.getElementById('kyHitTitle').textContent = `Hit-List & Save-As Names (0 cases • 0 PDFs)`;
    document.getElementById('kyStatsLbl').textContent = 'No scan checkpoint yet for this month. Click Load or ▶ Start Scan to check live!';
    document.getElementById('btnShowAllPdfs').textContent = 'Show All Brief PDFs (0)';
    if (document.getElementById('kySummaryLbl')) {{
      document.getElementById('kySummaryLbl').textContent = '0 cases (0 rows)';
    }}
    if (document.getElementById('kyProgLbl') && (!STATE.scan_status || !STATE.scan_status.running)) {{
      document.getElementById('kyProgLbl').innerHTML = 'Ready • Click <strong style="color:var(--accent);">[ Load ]</strong> or <strong style="color:var(--accent);">[ ▶ Start Scan ]</strong> to inspect this month';
    }}
    const emptyTr = document.createElement('tr');
    emptyTr.innerHTML = `
      <td colspan="4" style="text-align:center;color:var(--muted);padding:45px 10px;font-size:12px;">
        <div style="font-size:20px;margin-bottom:6px;color:var(--accent);">📂 Month Not Scanned Yet</div>
        No cases loaded yet for this month.<br>
        <span style="font-size:11px;color:var(--accent);display:inline-block;margin-top:6px;">👉 Click <strong>[ Load ]</strong> to fetch Document Search cases or <strong>[ ▶ Start Scan ]</strong> to inspect briefs!</span>
      </td>
    `;
    leftTbody.appendChild(emptyTr);

    const emptyRightTr = document.createElement('tr');
    emptyRightTr.innerHTML = `
      <td colspan="6" style="text-align:center;padding:110px 10px;color:var(--muted);font-size:13px;">
        <div style="font-size:28px;margin-bottom:10px;">📋</div>
        <strong style="color:var(--text);font-size:14px;">No Scanned Data for this Month Yet</strong>
        <div style="color:var(--muted);font-size:12px;margin-top:6px;">Cases and collectible brief PDFs will appear here once loaded or scanned.</div>
        <div style="margin-top:14px;">
          <span style="background:#1f2922;color:var(--accent);padding:5px 12px;border-radius:4px;border:1px solid #284430;font-size:11px;">👉 Click <strong>[ Load ]</strong> or <strong>[ ▶ Start Scan ]</strong> on the left to start checking!</span>
        </div>
      </td>
    `;
    tbody.appendChild(emptyRightTr);

    renderKyDrawer();
    return;
  }}

  // Populate left table with case overview rows
  const overviewList = batch.overview && batch.overview.length ? batch.overview : batch.hits.map(h => ({{
    date: h.date,
    case_number: h.case_number,
    subtype: h.docs.map(d => d.document_name).join(', ') || 'Briefing',
    status: `HIT (${{h.docs.length}} PDF${{h.docs.length !== 1 ? 's' : ''}})`,
    tag: 'hit',
    is_hit: true,
  }}));

  const leftFrag = document.createDocumentFragment();
  overviewList.forEach(item => {{
    const tr = document.createElement('tr');
    tr.setAttribute('data-case', item.case_number);
    if (SELECTED_CASE === item.case_number) {{
      tr.className = 'selected-row';
    }}
    let statusColor = 'var(--muted)';
    let statusWeight = 'normal';
    if (item.tag === 'hit' || item.is_hit) {{
      statusColor = 'var(--accent)';
      statusWeight = '700';
    }} else if (item.tag === 'review' || (item.status && item.status.includes('⚠'))) {{
      statusColor = '#f5b942';
      statusWeight = '600';
    }}
    tr.innerHTML = `
      <td>${{item.date || ''}}</td>
      <td style="font-weight:700;letter-spacing:0.3px;">${{item.case_number}}</td>
      <td style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${{item.subtype || ''}}">${{item.subtype || ''}}</td>
      <td style="text-align:right;"><span style="color:${{statusColor}};font-weight:${{statusWeight}};">${{item.status}}</span></td>
    `;
    tr.onclick = () => selectKyCase(item.case_number);
    leftFrag.appendChild(tr);
  }});
  leftTbody.appendChild(leftFrag);

  // Populate right table with hit list
  const filtered = getKyFilteredHits(batch);
  updateKyHeaderStats(batch);

  if (!SELECTED_CASE && filtered.length > 0) {{
    SELECTED_CASE = filtered[0].case_number;
  }}

  const rightFrag = document.createDocumentFragment();
  if (filtered.length === 0) {{
    const noHitsTr = document.createElement('tr');
    const isSearching = !!q;
    noHitsTr.innerHTML = `
      <td colspan="6" style="text-align:center;padding:110px 10px;color:var(--muted);font-size:13px;">
        <div style="font-size:28px;margin-bottom:10px;">${{isSearching ? '🔍' : '📋'}}</div>
        <strong style="color:var(--text);font-size:14px;">${{isSearching ? 'No Matching Cases Found' : 'No Collectible Brief Hits Found Yet'}}</strong>
        <div style="color:var(--muted);font-size:12px;margin-top:6px;">
          ${{isSearching ? `No cases match search query "<strong>${{q}}</strong>". Try clearing search.` : `Found ${{overviewList.length}} case(s) in Document Search, but none have matched collectible brief PDFs yet.`}}
        </div>
        ${{!isSearching ? '<div style="margin-top:14px;"><span style="background:#1f2922;color:var(--accent);padding:5px 12px;border-radius:4px;border:1px solid #284430;font-size:11px;">👉 Click <strong>[ ▶ Start Scan ]</strong> on the left to inspect all dockets live!</span></div>' : ''}}
      </td>
    `;
    rightFrag.appendChild(noHitsTr);
  }} else {{
    filtered.forEach(h => {{
      const isChecked = CHECKED.has(h.case_number);
      const isSelected = SELECTED_CASE === h.case_number;
      const doneInfo = batch.done ? batch.done[h.case_number] : null;

      const tr = document.createElement('tr');
      tr.setAttribute('data-case', h.case_number);
      let cls = [];
      if (isSelected) cls.push('selected-row');
      if (doneInfo) cls.push('done-row');
      tr.className = cls.join(' ');

      const savePreview = h.docs.map(d => d.save_as_name).join(', ');
      const statusBadge = doneInfo
        ? `<span style="color:var(--accent);font-weight:700;" onclick="event.stopPropagation();markCaseDone('${{h.case_number}}',false)">✓ ${{doneInfo.by}}</span>`
        : `<span style="color:var(--muted);" onclick="event.stopPropagation();markCaseDone('${{h.case_number}}',true)">Todo</span>`;

      tr.innerHTML = `
        <td style="text-align:center;" onclick="event.stopPropagation();">
          <input type="checkbox" ${{isChecked ? 'checked' : ''}} onchange="toggleCaseCheck('${{h.case_number}}', this.checked)">
        </td>
        <td>${{statusBadge}}</td>
        <td>${{h.date || ''}}</td>
        <td><a href="${{h.case_url}}" target="_blank" style="color:var(--accent);font-weight:700;text-decoration:none;" onclick="event.stopPropagation();">${{h.case_number}} ↗</a></td>
        <td><span style="background:#222;padding:2px 6px;border-radius:3px;">${{h.docs.length}}</span></td>
        <td style="font-family:Consolas,monospace;max-width:400px;overflow:hidden;text-overflow:ellipsis;">${{savePreview}}</td>
      `;
      tr.onclick = () => selectKyCase(h.case_number);
      rightFrag.appendChild(tr);
    }});
  }}
  tbody.appendChild(rightFrag);

  const allVisibleChecked = filtered.length > 0 && filtered.every(h => CHECKED.has(h.case_number));
  document.getElementById('kyHeadCheck').checked = allVisibleChecked;
  const selectAllBtn = document.getElementById('btnSelectAll');
  if (selectAllBtn) {{
    selectAllBtn.textContent = allVisibleChecked ? '☐ Unselect All' : '☑ Select All';
  }}

  renderKyDrawer();
}}

function renderKyDrawer() {{
  const batch = currentKyBatch();
  const drawerTitle = document.getElementById('kyDrawerTitle');
  const briefsList = document.getElementById('kyDrawerBriefsList');

  if (!batch || !SELECTED_CASE) {{
    drawerTitle.textContent = 'Click any Hit case above to view full details & one-click Save-As filename buttons';
    briefsList.innerHTML = '';
    LAST_DRAWER_CASE = '';
    return;
  }}
  if (LAST_DRAWER_CASE === SELECTED_CASE && briefsList.children.length > 0) {{
    return;
  }}
  LAST_DRAWER_CASE = SELECTED_CASE;
  briefsList.innerHTML = '';

  const caseObj = batch.hits.find(h => h.case_number === SELECTED_CASE);
  if (!caseObj) {{
    drawerTitle.textContent = 'Click any Hit case above to view full details & one-click Save-As filename buttons';
    return;
  }}

  const count = caseObj.docs.length;
  drawerTitle.textContent = `${{caseObj.case_number}}  •  ${{caseObj.title || 'Case'}}   (${{count}} Collectible PDF${{count !== 1 ? 's' : ''}} — Website Row Guide & Save-As Names)`;

  const frag = document.createDocumentFragment();
  caseObj.docs.forEach((doc, idx) => {{
    const div = document.createElement('div');
    div.className = 'brief-row';
    const sub = doc.submitter ? ` • Submitted By: ${{doc.submitter}}` : '';
    div.innerHTML = `
      <button class="brief-save-btn" onclick="saveAndDownloadSingle('${{doc.document_id}}','${{doc.save_as_name}}','${{caseObj.case_number}}')">⬇ ${{doc.save_as_name}}</button>
      <span class="brief-meta">◄ [${{idx+1}}] Website: ${{doc.filed_date || ''}} • ${{doc.document_name}}${{sub}}</span>
    `;
    frag.appendChild(div);
  }});
  briefsList.appendChild(frag);
}}

function toggleCaseCheck(cn, checked) {{
  if (checked) CHECKED.add(cn); else CHECKED.delete(cn);
  const batch = currentKyBatch();
  if (batch) updateKyHeaderStats(batch);
  const filtered = getKyFilteredHits(batch);
  const allChecked = filtered.length > 0 && filtered.every(h => CHECKED.has(h.case_number));
  document.getElementById('kyHeadCheck').checked = allChecked;
  const selectAllBtn = document.getElementById('btnSelectAll');
  if (selectAllBtn) {{
    selectAllBtn.textContent = allChecked ? '☐ Unselect All' : '☑ Select All';
  }}
}}

function toggleSelectAll(forceVal) {{
  const batch = currentKyBatch();
  if (!batch) return;
  const filtered = getKyFilteredHits(batch);
  const allChecked = filtered.length > 0 && filtered.every(h => CHECKED.has(h.case_number));
  const target = (typeof forceVal === 'boolean') ? forceVal : !allChecked;
  filtered.forEach(h => {{ if (target) CHECKED.add(h.case_number); else CHECKED.delete(h.case_number); }});
  document.querySelectorAll('#kyHitsTbody input[type="checkbox"]').forEach(cb => {{
    cb.checked = target;
  }});
  document.getElementById('kyHeadCheck').checked = target;
  const selectAllBtn = document.getElementById('btnSelectAll');
  if (selectAllBtn) {{
    selectAllBtn.textContent = target ? '☐ Unselect All' : '☑ Select All';
  }}
  updateKyHeaderStats(batch);
}}

async function markCaseDone(cn, done) {{
  const batch = CURRENT_TAB === 'ny' ? currentNyBatch() : currentKyBatch();
  if (!batch) return;
  const user = userNameEl.value.trim() || 'Edward';
  await fetch('/api/done', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ scope: batch.scope, case_number: cn, done, user }})
  }});
  await fetchState();
}}

async function toggleSelectedDone() {{
  const batch = currentKyBatch();
  if (!batch) return;
  const targets = CHECKED.size > 0 ? Array.from(CHECKED) : (SELECTED_CASE ? [SELECTED_CASE] : []);
  if (!targets.length) {{ toast('Select or check a case first'); return; }}
  const allDone = targets.every(cn => !!batch.done[cn]);
  const newDoneState = !allDone;
  const user = userNameEl.value.trim() || 'Edward';
  await fetch('/api/done_many', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ scope: batch.scope, case_numbers: targets, done: newDoneState, user }})
  }});
  CHECKED.clear();
  await fetchState();
  toast(`Marked ${{targets.length}} case(s) as ${{newDoneState ? 'Done' : 'Todo'}}`);
}}

async function markAllDownloaded(done) {{
  const batch = currentKyBatch();
  if (!batch) return;
  const user = userNameEl.value.trim() || 'Edward';
  const cns = batch.hits.map(h => h.case_number);
  await fetch('/api/done_many', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ scope: batch.scope, case_numbers: cns, done, user }})
  }});
  await fetchState();
  toast(done ? 'Marked all cases as downloaded!' : 'Unmarked all cases');
}}

function saveAndDownloadSingle(docId, saveAsName, caseNumber) {{
  navigator.clipboard.writeText(saveAsName);
  toast('Copied & Auto-Saving: ' + saveAsName);
  if (docId) {{
    window.location.href = `/api/ky/pdf/${{encodeURIComponent(docId)}}/${{encodeURIComponent(saveAsName)}}.pdf`;
  }}
  if (caseNumber) {{
    markCaseDone(caseNumber, true);
  }}
}}

function copyAndOpenSingle(docId, saveAsName) {{
  navigator.clipboard.writeText(saveAsName);
  toast('Copied Save-As: ' + saveAsName);
  if (docId) {{
    window.open(`https://appellatepublic.kycourts.net/api/api/v1/publicaccessdocuments/${{docId}}/download`, '_blank');
  }}
}}

function downloadSelectedCasePdfs() {{
  if (!SELECTED_CASE) {{ toast('Select a case first'); return; }}
  const batch = currentKyBatch();
  const caseObj = batch.hits.find(h => h.case_number === SELECTED_CASE);
  if (!caseObj || !caseObj.docs.length) return;
  caseObj.docs.forEach((d, i) => {{
    setTimeout(() => {{
      saveAndDownloadSingle(d.document_id, d.save_as_name, caseObj.case_number);
    }}, i * 350);
  }});
}}

function downloadCheckedOrAllZip() {{
  const batch = currentKyBatch();
  if (!batch) return;
  const cases = CHECKED.size > 0 ? Array.from(CHECKED) : batch.hits.map(h => h.case_number);
  if (!cases.length) return;
  const user = userNameEl.value.trim() || 'Edward';
  toast(`Preparing ZIP of ${{cases.length}} case(s)...`);
  const qs = new URLSearchParams({{ scope: batch.scope, cases: cases.join(','), user }});
  window.location.href = `/api/ky/zip?` + qs.toString();
  setTimeout(() => {{ CHECKED.clear(); fetchState(); }}, 3000);
}}

function copySelectedSaveNames() {{
  const batch = currentKyBatch();
  if (!batch) return;
  const cases = CHECKED.size > 0 ? batch.hits.filter(h => CHECKED.has(h.case_number)) : (SELECTED_CASE ? batch.hits.filter(h => h.case_number === SELECTED_CASE) : batch.hits);
  const names = [];
  cases.forEach(c => c.docs.forEach(d => names.push(d.save_as_name)));
  if (!names.length) {{ toast('No Save-As names found'); return; }}
  navigator.clipboard.writeText(names.join('\\n'));
  toast(`Copied ${{names.length}} Save-As filename(s)`);
}}

function copyTrackerCsv() {{
  const batch = currentKyBatch();
  if (!batch) return;
  const user = userNameEl.value.trim() || 'Bearneza, John Edward ';
  const today = new Date().toISOString().slice(0, 10);
  const targets = CHECKED.size > 0 ? batch.hits.filter(h => CHECKED.has(h.case_number)) : (SELECTED_CASE ? batch.hits.filter(h => h.case_number === SELECTED_CASE) : batch.hits);
  if (!targets.length) {{ toast('No cases to copy'); return; }}
  const lines = targets.map((h, idx) => {{
    const colDate = idx === 0 ? today : '';
    const docket = h.case_number.startsWith('Go to case') ? h.case_number : ('Go to case' + h.case_number);
    return [user, colDate, docket, 'CA - BRIEFING', 'APPELLANT BRIEF FILED', h.date || '', '', '', h.docs.length].join('\\t');
  }});
  navigator.clipboard.writeText(lines.join('\\n'));
  toast(`Copied ${{lines.length}} tracker rows for Excel/Sheets (Paste in A4 under Ma'am's header)!`);
}}

let MODAL_PDFS = [];

function showAllBriefPdfs() {{
  const batch = currentKyBatch();
  if (!batch) return;
  MODAL_PDFS = [];
  let idx = 1;
  batch.hits.forEach(h => {{
    h.docs.forEach(d => {{
      MODAL_PDFS.push({{
        num: idx++,
        case_number: h.case_number,
        save_as_name: d.save_as_name,
        document_name: d.document_name,
        filed_date: d.filed_date || h.date,
        submitter: d.submitter || '',
        doc_id: d.document_id,
      }});
    }});
  }});
  document.getElementById('modalTitle').textContent = `All Kentucky Collectible PDFs (${{MODAL_PDFS.length}} PDFs across ${{batch.hits.length}} cases)`;
  document.getElementById('modalSearch').value = '';
  renderModalPdfs();
  document.getElementById('allPdfsModal').style.display = 'flex';
}}

function closeAllPdfsModal() {{
  document.getElementById('allPdfsModal').style.display = 'none';
}}

function filterModalPdfs() {{
  renderModalPdfs();
}}

function renderModalPdfs() {{
  const tbody = document.getElementById('modalPdfsTbody');
  tbody.innerHTML = '';
  const q = (document.getElementById('modalSearch').value || '').trim().toLowerCase();
  const filtered = MODAL_PDFS.filter(p => !q || `${{p.case_number}} ${{p.save_as_name}} ${{p.document_name}} ${{p.submitter}}`.toLowerCase().includes(q));
  filtered.forEach(p => {{
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td style="color:var(--muted);text-align:center;">${{p.num}}</td>
      <td><strong>${{p.case_number}}</strong></td>
      <td style="font-family:Consolas,monospace;color:var(--accent);cursor:pointer;" onclick="saveAndDownloadSingle('${{p.doc_id}}','${{p.save_as_name}}','${{p.case_number}}')" title="Click to Auto-Save">⬇ ${{p.save_as_name}}</td>
      <td>${{p.document_name}}</td>
      <td>${{p.filed_date}}</td>
      <td style="color:var(--muted);">${{p.submitter}}</td>
    `;
    tbody.appendChild(tr);
  }});
}}

function copyAllModalSaveNames() {{
  if (!MODAL_PDFS.length) return;
  const names = MODAL_PDFS.map(p => p.save_as_name);
  navigator.clipboard.writeText(names.join('\\n'));
  toast(`Copied ${{names.length}} Save-As filename(s)`);
}}

function openSelectedInCtrack() {{
  const batch = currentKyBatch();
  if (!batch || !SELECTED_CASE) return;
  const caseObj = batch.hits.find(h => h.case_number === SELECTED_CASE);
  if (caseObj && caseObj.case_url) {{
    window.open(caseObj.case_url, '_blank');
  }}
}}

async function triggerKyLiveScan() {{
  if (STATE.scan_status && STATE.scan_status.running) {{
    toast(`⚠ Scan is already running for ${{STATE.scan_status.running}}! Click Stop to abort it first.`);
    return;
  }}
  const court = document.getElementById('kyCourtSelect').value;
  const ym = document.getElementById('kyMonthSelect').value || '2026-11';
  const fresh = document.getElementById('chkKyFresh').checked;

  // 1. Instant 0ms Optimistic UI feedback
  const startBtn = document.getElementById('btnStartScan');
  const stopBtn = document.getElementById('btnStopScan');
  const progFill = document.getElementById('kyProgFill');
  const progLbl = document.getElementById('kyProgLbl');
  const logBox = document.getElementById('kyLogBox');
  const statusPill = document.getElementById('globalStatusPill');
  const leftTbody = document.getElementById('kyBatchesTbody');
  const rightTbody = document.getElementById('kyHitsTbody');

  startBtn.disabled = true;
  startBtn.style.opacity = '0.6';
  startBtn.innerHTML = '⏳ Scanning... (1%)';
  stopBtn.disabled = false;
  stopBtn.style.background = '#3b1c1c';
  stopBtn.style.color = '#ef4444';
  stopBtn.style.borderColor = '#ef4444';
  progFill.style.width = '12%';
  progLbl.innerHTML = `<strong>⏳ Connecting to Kentucky C-Track (${{court}} ${{ym}})... Starting docket scan...</strong>`;
  progLbl.style.color = 'var(--accent)';
  if (statusPill) {{ statusPill.textContent = `● Scanning ${{court}} ${{ym}}...`; statusPill.style.color = '#f59e0b'; }}

  if (leftTbody && (!leftTbody.children.length || leftTbody.innerText.includes('Not Scanned') || leftTbody.innerText.includes('No cases'))) {{
    leftTbody.innerHTML = `
      <tr>
        <td colspan="4" style="text-align:center;padding:50px 10px;color:var(--accent);font-size:13px;">
          <div style="font-size:24px;margin-bottom:8px;">⏳</div>
          <strong>Connecting to Kentucky C-Track...</strong>
          <div style="font-size:11px;color:var(--muted);margin-top:6px;">Starting live inspection for ${{court}} ${{ym}}...</div>
        </td>
      </tr>
    `;
  }}
  if (rightTbody && (!rightTbody.children.length || rightTbody.innerText.includes('No Scanned') || rightTbody.innerText.includes('No cases') || rightTbody.innerText.includes('No Collectible'))) {{
    rightTbody.innerHTML = `
      <tr>
        <td colspan="6" style="text-align:center;padding:110px 10px;color:var(--accent);font-size:13px;">
          <div style="font-size:28px;margin-bottom:10px;">⏳</div>
          <strong style="color:var(--accent);font-size:14px;">Scanning Dockets Live (${{court}} ${{ym}})...</strong>
          <div style="font-size:12px;color:var(--muted);margin-top:6px;">Collectible brief PDFs and Save-As names will appear here as hits are found.</div>
        </td>
      </tr>
    `;
  }}

  logBox.innerHTML += `<div class="log-line-head">=== CONNECTING TO C-TRACK (${{court}} — ${{ym}}) ===</div>`;
  logBox.innerHTML += `<div class="log-line-dim">Querying Kentucky Document Search database for ${{ym}}...</div>`;
  logBox.scrollTop = logBox.scrollHeight;

  toast(`Starting live C-Track scan for ${{court}} ${{ym}}...`);

  try {{
    const res = await fetch('/api/ky/scan', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ court_code: court, ym, fresh }})
    }});
    const data = await res.json();
    if (data.error) {{
      toast('⚠ ' + data.error);
      applyStateToUi();
    }} else {{
      setTimeout(fetchState, 400);
    }}
  }} catch (e) {{
    toast('⚠ Network connection error');
    applyStateToUi();
  }}
}}

async function stopKyScan() {{
  await fetch('/api/ky/stop', {{ method: 'POST' }});
  toast('Stopping scan...');
}}

function clearScanLog() {{
  document.getElementById('kyLogBox').innerHTML = '';
}}

function selectNyMonth(scope) {{
  const sel = document.getElementById('nyMonthSelect');
  if (sel) {{
    sel.value = scope;
    onNyMonthChange();
  }}
}}

let LAST_NY_SIG = '';

function renderNyTable() {{
  const batch = currentNyBatch();
  const q = (document.getElementById('nySearchBox').value || '').trim().toLowerCase();
  const hideDone = document.getElementById('nyHideDone').checked;
  const doneKeys = batch && batch.done ? Object.keys(batch.done).sort().join(',') : '';
  const currentSig = batch
    ? `${{batch.scope}}|${{batch.lite ? 'lite' : 'full'}}|${{batch.hits.length}}|${{doneKeys}}|${{q}}|${{hideDone}}`
    : 'EMPTY';

  const nyBatchesTbody = document.getElementById('nyBatchesTbody');
  if (nyBatchesTbody && STATE.batches) {{
    const nyBatches = STATE.batches.filter(b => b.jurisdiction === 'NY');
    nyBatchesTbody.innerHTML = nyBatches.map(b => {{
      const isSel = batch && b.scope === batch.scope;
      const pdfCount = batchPdfCount(b);
      const name = (b.label || '').replace('New York AD2 — ', '') || b.month_key;
      return `
        <tr class="${{isSel ? 'selected-row' : ''}}" style="cursor:pointer;" onclick="selectNyMonth('${{b.scope}}')">
          <td style="font-weight:${{isSel ? '700' : 'normal'}};color:${{isSel ? 'var(--accent)' : 'var(--text)'}};">${{name}}</td>
          <td>${{batchHitCount(b)}}</td>
          <td style="color:var(--accent);font-weight:600;">${{pdfCount}}</td>
        </tr>
      `;
    }}).join('');
  }}

  if (currentSig === LAST_NY_SIG && batch) return;
  LAST_NY_SIG = currentSig;

  const tbody = document.getElementById('nyHitsTbody');
  tbody.innerHTML = '';
  if (!batch) return;
  if (batch.lite) {{
    document.getElementById('nyHitTitle').textContent = `New York Hit-List (${{batch.hit_count || 0}} cases)`;
    tbody.innerHTML = `
      <tr><td colspan="4" style="text-align:center;padding:90px 10px;color:var(--accent);font-size:13px;">
        <div style="font-size:26px;margin-bottom:8px;">⏳</div>
        <strong>Loading ${{batch.hit_count || 0}} cases • ${{batch.pdf_count || 0}} PDFs...</strong>
      </td></tr>`;
    return;
  }}

  const filtered = batch.hits.filter(h => {{
    if (hideDone && batch.done && batch.done[h.case_number]) return false;
    if (!q) return true;
    return `${{h.case_number}} ${{h.title}}`.toLowerCase().includes(q);
  }});

  document.getElementById('nyHitTitle').textContent = `New York Hit-List (${{filtered.length}} cases)`;

  const frag = document.createDocumentFragment();
  filtered.forEach(h => {{
    const doneInfo = batch.done ? batch.done[h.case_number] : null;
    const tr = document.createElement('tr');
    if (doneInfo) tr.className = 'done-row';

    const docsHtml = h.docs.map(d => `
      <div style="margin:4px 0;">
        <a href="${{d.download_url || h.case_url}}" target="_blank" class="brief-save-btn" style="text-decoration:none;display:inline-block;">📄 ${{d.save_as_name}}</a>
        <span style="color:var(--muted);font-size:11px;margin-left:6px;">${{d.document_name}}</span>
      </div>
    `).join('');

    const statusBadge = doneInfo
      ? `<span style="color:var(--accent);font-weight:700;" onclick="event.stopPropagation();markCaseDone('${{h.case_number}}',false)">✓ ${{doneInfo.by}}</span>`
      : `<span style="color:var(--muted);" onclick="event.stopPropagation();markCaseDone('${{h.case_number}}',true)">Todo</span>`;

    tr.innerHTML = `
      <td>${{statusBadge}}</td>
      <td>${{h.date || ''}}</td>
      <td><a href="${{h.case_url}}" target="_blank" style="color:var(--accent);font-weight:700;text-decoration:none;">${{h.case_number}} ↗</a></td>
      <td>
        <div style="font-weight:600;margin-bottom:4px;">${{h.title}}</div>
        ${{docsHtml}}
      </td>
    `;
    frag.appendChild(tr);
  }});
  tbody.appendChild(frag);
}}

// Initialize from embedded state immediately
applyStateToUi();
switchTab(CURRENT_TAB);

let IS_POLLING = false;
async function pollLoop() {{
  if (IS_POLLING) return;
  IS_POLLING = true;
  try {{
    const res = await fetch('/api/scan/poll');
    if (res.ok) {{
      const pollData = await res.json();
      const wasScanning = !!(STATE.scan_status && STATE.scan_status.running);
      STATE.scan_status = pollData.scan_status;
      STATE.scan_progress = pollData.scan_progress;
      STATE.scan_logs = pollData.scan_logs;
      applyScanProgressToUi();

      const kyB = currentKyBatch();
      const nyB = currentNyBatch();
      const needsHydration = !!((kyB && kyB.lite) || (nyB && nyB.lite));
      const dataChanged = pollData.version !== LAST_STATE_VERSION || (wasScanning && !pollData.scan_status.running);
      if (dataChanged || needsHydration) {{
        LAST_STATE_VERSION = pollData.version;
        // Reuse an in-flight month download when only hydrating; force a fresh one on changes.
        await fetchState(!dataChanged);
      }}
    }}
  }} catch (err) {{
  }} finally {{
    IS_POLLING = false;
  }}
  const running = !!(STATE.scan_status && STATE.scan_status.running);
  setTimeout(pollLoop, running ? 800 : 2500);
}}

window.addEventListener('load', () => {{
  setTimeout(pollLoop, 1500);
}});
</script>
</body>
</html>
"""


class DashboardRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def _send_bytes(self, raw: bytes, content_type: str, status: int = 200, extra_headers: dict | None = None) -> None:
        accept = self.headers.get("Accept-Encoding", "")
        if "gzip" in accept and len(raw) > 512:
            raw = gzip.compress(raw, compresslevel=5)
            enc = "gzip"
        else:
            enc = None

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if enc:
            self.send_header("Content-Encoding", enc)
        self.send_header("Content-Length", str(len(raw)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _send_json(self, data: dict, status: int = 200) -> None:
        raw = json.dumps(data).encode("utf-8")
        self._send_bytes(raw, "application/json; charset=utf-8", status=status)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        if path == "/api/tunnel":
            self._send_json({"url": _PUBLIC_URL})
            return

        if path in ("/", "/index.html"):
            raw = generate_dashboard_html().encode("utf-8")
            self._send_bytes(raw, "text/html; charset=utf-8", status=200)
            return

        if path == "/api/state":
            qs = urllib.parse.parse_qs(parsed.query)
            full_scopes = None
            if (qs.get("lite") or ["0"])[0] == "1":
                full_scopes = {s for s in (qs.get("scopes") or [""])[0].split(",") if s}
            self._send_json(get_dashboard_state(sync_first=False, full_scopes=full_scopes))
            return

        if path == "/api/scan/poll":
            self._send_json({
                "version": _STATE_VERSION,
                "scan_status": _SCAN_STATUS,
                "scan_progress": _SCAN_PROGRESS,
                "scan_logs": _SCAN_LOGS[-60:],
            })
            return

        if path.startswith("/api/ky/pdf/"):
            parts = path[len("/api/ky/pdf/"):].split("/", 1)
            doc_id = urllib.parse.unquote(parts[0]).strip()
            raw_fname = urllib.parse.unquote(parts[1] if len(parts) > 1 else f"{doc_id}.pdf")
            safe_fname = re.sub(r'[<>:"/\\|?*]+', "_", raw_fname)
            if not safe_fname.lower().endswith(".pdf"):
                safe_fname += ".pdf"
            url = f"{KY_API_V1}/publicaccessdocuments/{doc_id}/download"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=35) as resp:
                    pdf_bytes = resp.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", f'attachment; filename="{safe_fname}"')
                self.send_header("Content-Length", str(len(pdf_bytes)))
                self.end_headers()
                self.wfile.write(pdf_bytes)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=502)
            return

        if path == "/api/ky/zip":
            qs = urllib.parse.parse_qs(parsed.query)
            scope = (qs.get("scope") or [""])[0]
            user = (qs.get("user") or ["Teammate"])[0]
            wanted = {c.strip() for c in (qs.get("cases") or [""])[0].split(",") if c.strip()}
            state = get_dashboard_state(sync_first=False)
            batch = next((b for b in state["batches"] if b["scope"] == scope), None)
            if not batch:
                self._send_json({"error": "Batch not found"}, status=404)
                return
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for h in batch["hits"]:
                    cn = h["case_number"]
                    if wanted and cn not in wanted:
                        continue
                    for d in h["docs"]:
                        doc_id = d.get("document_id")
                        if not doc_id:
                            continue
                        fname = re.sub(r'[<>:"/\\|?*]+', "_", d["save_as_name"]) + ".pdf"
                        url = f"{KY_API_V1}/publicaccessdocuments/{doc_id}/download"
                        try:
                            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                            with urllib.request.urlopen(req, timeout=30) as resp:
                                data = resp.read()
                            if data.startswith(b"%PDF"):
                                zf.writestr(fname, data)
                        except Exception:
                            continue
                    set_case_done(scope, cn, True, user)
            raw_zip = buf.getvalue()
            zip_name = f"{scope}_Brief_PDFs.zip"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{zip_name}"')
            self.send_header("Content-Length", str(len(raw_zip)))
            self.end_headers()
            self.wfile.write(raw_zip)
            return

        self._send_json({"error": "Not found"}, status=404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        if self.path == "/api/done":
            set_case_done(
                str(body.get("scope") or ""),
                str(body.get("case_number") or ""),
                bool(body.get("done")),
                str(body.get("user") or "Teammate"),
            )
            self._send_json({"ok": True})
            return
        if self.path == "/api/done_many":
            scope = str(body.get("scope") or "")
            done = bool(body.get("done"))
            user = str(body.get("user") or "Teammate")
            for cn in body.get("case_numbers") or []:
                set_case_done(scope, str(cn), done, user)
            self._send_json({"ok": True})
            return
        if self.path == "/api/ky/load":
            court = str(body.get("court_code") or "KYCOA")
            ym = str(body.get("ym") or "2026-11")
            try:
                saved = load_ky_checkpoint(court, ym)
                if not saved:
                    try:
                        rows = fetch_ky_month_search_rows(court, ym)
                    except Exception:
                        rows = []
                    records = {}
                    seen = {}
                    for r in rows:
                        if not r.case_number or not r.case_id:
                            continue
                        if r.case_number not in seen:
                            seen[r.case_number] = []
                        lbl = r.sub_type or r.document_name or r.doc_type
                        if lbl and lbl not in seen[r.case_number]:
                            seen[r.case_number].append(lbl)
                    for r in rows:
                        if r.case_number and r.case_number not in records:
                            records[r.case_number] = KyCaseRecord(
                                case_id=r.case_id,
                                case_number=r.case_number,
                                court=r.court,
                                title=getattr(r, "case_title", "") or "Case",
                                first_filed_date=r.filed_date_display,
                                search_sub_types=seen.get(r.case_number, []),
                                status="NOT_SCANNED",
                                collectible_docs=[],
                                note="Not scanned",
                            )
                    save_ky_checkpoint(court, ym, records, rows)
                    sync_ky_checkpoint_to_batch(court, ym, records, rows)
                else:
                    sync_ky_checkpoint_to_batch(court, ym, saved)
                self._send_json({"ok": True})
            except Exception as err:
                self._send_json({"error": str(err)}, status=500)
            return
        if self.path == "/api/ky/scan":
            court = str(body.get("court_code") or "KYCOA")
            ym = str(body.get("ym") or "2026-11")
            if _SCAN_STATUS["running"]:
                self._send_json({"error": f"A scan is already in progress for {_SCAN_STATUS['running']}. Click Stop first."}, status=409)
                return
            _trigger_live_ky_scan(court, ym, bool(body.get("fresh")))
            self._send_json({"ok": True})
            return
        if self.path == "/api/ky/stop":
            STOP_EVENT.set()
            _SCAN_STATUS["running"] = ""
            _SCAN_PROGRESS["lbl"] = "Scan stopped by user"
            self._send_json({"ok": True})
            return
        if self.path == "/api/publish":
            _save_batch(
                str(body.get("scope") or ""),
                str(body.get("jurisdiction") or "KY"),
                str(body.get("label") or ""),
                body.get("payload") or {},
                str(body.get("user") or "Edward"),
            )
            self._send_json({"ok": True})
            return
        self._send_json({"error": "Not found"}, status=404)


def start_public_wfh_tunnel(local_port: int) -> None:
    """Start a free public HTTPS tunnel via cloudflared (Philippine CDN edge) or localhost.run fallback."""
    global _PUBLIC_URL, _TUNNEL_PROC, _TUNNEL_THREAD
    if _TUNNEL_THREAD is not None and _TUNNEL_THREAD.is_alive():
        return

    _TUNNEL_STOP.clear()

    def _candidates() -> list[tuple[str, list[str], str]]:
        out = []
        cf_bin = shutil.which("cloudflared")
        if cf_bin:
            out.append((
                "cloudflared",
                [cf_bin, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{local_port}"],
                r"https://[a-zA-Z0-9-]+\.trycloudflare\.com",
            ))
        if shutil.which("ssh"):
            out.append((
                "localhost.run",
                [
                    "ssh",
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "ServerAliveInterval=30",
                    "-o", "ExitOnForwardFailure=yes",
                    "-R", f"80:127.0.0.1:{local_port}",
                    "nokey@localhost.run",
                ],
                r"https://[a-zA-Z0-9.-]+\.lhr\.life",
            ))
        return out

    def _run_one(cmd: list[str], pattern: str) -> bool:
        """Run one tunnel provider until it exits. Returns True if it ever published a URL."""
        global _PUBLIC_URL, _TUNNEL_PROC
        got_url = False
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            return False
        _TUNNEL_PROC = proc
        try:
            assert proc.stdout is not None
            # Keep draining output for the whole tunnel lifetime so the pipe never fills up.
            for line in proc.stdout:
                if not got_url:
                    m = re.search(pattern, line)
                    if m:
                        _PUBLIC_URL = m.group(0)
                        got_url = True
        finally:
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
        proc.wait()
        if _PUBLIC_URL and got_url:
            _PUBLIC_URL = ""
        return got_url

    def _tunnel_worker() -> None:
        # Prefer cloudflared (Manila edge); fall back to localhost.run when cloudflared is
        # missing, rate-limited (HTTP 429 / error 1015), or drops. Reconnect with backoff.
        backoff = 3
        while not _TUNNEL_STOP.is_set():
            any_ok = False
            for _name, cmd, pattern in _candidates():
                if _TUNNEL_STOP.is_set():
                    return
                if _run_one(cmd, pattern):
                    any_ok = True
                    backoff = 3
                    break
            if _TUNNEL_STOP.wait(backoff if any_ok else min(backoff, 60)):
                return
            if not any_ok:
                backoff = min(backoff * 2, 60)

    _TUNNEL_THREAD = threading.Thread(target=_tunnel_worker, daemon=True)
    _TUNNEL_THREAD.start()


class DashboardServer(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second app instance silently share port 8080 with a stale
    # one (requests then land on either process). Keep the bind exclusive there.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def stop_dashboard_server() -> None:
    """Cleanly shut down the web server and kill the background SSH tunnel process."""
    global _SERVER_INSTANCE, _TUNNEL_PROC, _PUBLIC_URL
    _TUNNEL_STOP.set()
    if _TUNNEL_PROC is not None:
        try:
            _TUNNEL_PROC.terminate()
            _TUNNEL_PROC.kill()
        except Exception:
            pass
        _TUNNEL_PROC = None
    _PUBLIC_URL = ""
    if _SERVER_INSTANCE is not None:
        try:
            _SERVER_INSTANCE.shutdown()
            _SERVER_INSTANCE.server_close()
        except Exception:
            pass
        _SERVER_INSTANCE = None


def start_dashboard_server_background(port: int = 8080) -> str:
    """Start the team web dashboard server + WFH public HTTPS tunnel in daemon threads."""
    global _SERVER_INSTANCE, _SERVER_PORT
    if _SERVER_INSTANCE is None:
        _init_db()
        sync_local_checkpoints_to_db()
        for p in (port, 8081, 8088, 8765):
            try:
                srv = DashboardServer(("0.0.0.0", p), DashboardRequestHandler)
                _SERVER_INSTANCE = srv
                _SERVER_PORT = p
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                break
            except OSError:
                continue
        start_public_wfh_tunnel(_SERVER_PORT)
    for _ in range(50):
        if _PUBLIC_URL:
            break
        time.sleep(0.1)
    return _PUBLIC_URL or f"http://127.0.0.1:{_SERVER_PORT}"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    _init_db()
    sync_local_checkpoints_to_db()
    if not os.environ.get("RENDER"):
        start_public_wfh_tunnel(port)
    print(f"=== BPM WFH Team Web App Running ===")
    print(f"Local Browser : http://127.0.0.1:{port}")
    DashboardServer(("0.0.0.0", port), DashboardRequestHandler).serve_forever()
