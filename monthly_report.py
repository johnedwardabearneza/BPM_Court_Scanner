import csv
from dataclasses import dataclass
from datetime import datetime
import html
import json
from pathlib import Path
import re

from classifier import (
    STATUS_HIT_VALID,
    STATUS_NEEDS_MANUAL_CHECK,
)
from report_builder import load_checkpoint
from ui_theme import SHARED_CSS, SHARED_JS, format_date_words, format_month_words, parse_row_meta, short_case_type


@dataclass(frozen=True)
class OptionProgress:
    total: int
    completed: int
    hits: int
    manual: int
    is_complete: bool
    badge: str
    brief_pdfs: int = 0


def parse_selection_indices(raw: str, max_count: int) -> list[int]:
    """
    Parse 1-based user choices like '2', '1-4', '1..4', or '1, 3, 5-6'
    into ordered unique 0-based indices.
    """
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError("Empty selection.")

    indices: list[int] = []
    seen: set[int] = set()
    for part in cleaned.split(","):
        token = part.strip()
        if not token:
            raise ValueError(f"Invalid empty token in '{raw}'.")

        range_m = re.match(r"^(\d+)\s*(?:-|\.\.)\s*(\d+)$", token)
        if range_m:
            start_1 = int(range_m.group(1))
            end_1 = int(range_m.group(2))
            if start_1 < 1 or end_1 < 1 or start_1 > max_count or end_1 > max_count:
                raise ValueError(f"Range '{token}' out of bounds (1..{max_count}).")
            step = 1 if start_1 <= end_1 else -1
            for num in range(start_1, end_1 + step, step):
                idx = num - 1
                if idx not in seen:
                    seen.add(idx)
                    indices.append(idx)
            continue

        if token.isdigit():
            num = int(token)
            if num < 1 or num > max_count:
                raise ValueError(f"Selection '{num}' out of bounds (1..{max_count}).")
            idx = num - 1
            if idx not in seen:
                seen.add(idx)
                indices.append(idx)
            continue

        raise ValueError(f"Unrecognized selection token '{token}'.")

    if not indices:
        raise ValueError("No valid indices selected.")
    return indices


def inspect_option_progress(run_dir: Path) -> OptionProgress:
    cp_path = run_dir / "checkpoint.json"
    if not cp_path.exists():
        return OptionProgress(0, 0, 0, 0, False, "[PENDING]")

    try:
        payload = json.loads(cp_path.read_text(encoding="utf-8"))
    except Exception:
        return OptionProgress(0, 0, 0, 0, False, "[PENDING]")

    meta = payload.get("meta") or {}
    raw_recs = payload.get("records") or {}
    total = int(meta.get("total_unique_cases") or len(raw_recs) or 0)
    completed = 0
    hits = 0
    manual = 0
    brief_pdfs = 0

    for rec in raw_recs.values():
        status = rec.get("status", "")
        if status == STATUS_NEEDS_MANUAL_CHECK:
            manual += 1
        else:
            completed += 1
        if status == STATUS_HIT_VALID:
            hits += 1
            brief_pdfs += len(rec.get("briefs") or [])

    if completed == 0 and manual == 0:
        if total == 0 and meta.get("source_name"):
            return OptionProgress(0, 0, 0, 0, True, "[DONE 0/0 | 0 HITS]", 0)
        return OptionProgress(total, 0, 0, 0, False, "[PENDING]", 0)

    is_complete = bool(total > 0 and completed >= total and manual == 0)
    pdf_part = f" ({brief_pdfs} PDFs)" if brief_pdfs else ""
    if is_complete:
        badge = f"[DONE {completed}/{total} | {hits} HITS{pdf_part}]"
    else:
        warn_part = f" | {manual} WARN" if manual else ""
        badge = f"[PARTIAL {completed}/{total or '?'} | {hits} HITS{pdf_part}{warn_part}]"

    return OptionProgress(total, completed, hits, manual, is_complete, badge, brief_pdfs)


def write_monthly_reports(
    output_root: Path,
    year_month: str,
    month_options: list,
    run_dir_resolver,
) -> dict[str, Path]:
    month_dir = output_root / f"_MONTH_{year_month}"
    month_dir.mkdir(parents=True, exist_ok=True)

    date_entries: list[dict] = []
    case_dates_map: dict[str, list[str]] = {}
    unique_hits_order: list[str] = []
    seen_unique_hits: set[str] = set()

    for opt in month_options:
        run_dir = run_dir_resolver(opt)
        records = load_checkpoint(run_dir)
        prog = inspect_option_progress(run_dir)
        ordered_recs = list(records.values())
        hits = [r for r in ordered_recs if r.status == STATUS_HIT_VALID]
        date_tag = opt.date_iso or opt.filename

        for r in hits:
            case_dates_map.setdefault(r.case_number, [])
            if date_tag not in case_dates_map[r.case_number]:
                case_dates_map[r.case_number].append(date_tag)
            if r.case_number not in seen_unique_hits:
                seen_unique_hits.add(r.case_number)
                unique_hits_order.append(r.case_number)

        date_entries.append({
            "option": opt,
            "date_tag": date_tag,
            "run_dir": run_dir,
            "progress": prog,
            "records": ordered_recs,
            "hits": hits,
        })

    # 1. Monthly combined CSV
    csv_path = month_dir / "monthly_scan_results.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Calendar Date", "Motion PDF", "Case Number", "Status",
            "Clickable Brief Count", "Brief Titles", "Caption", "Case Type",
            "Docket URL", "Brief URLs", "Note",
        ])
        for entry in date_entries:
            opt = entry["option"]
            for rec in entry["records"]:
                briefs = rec.briefs or []
                writer.writerow([
                    entry["date_tag"], opt.filename, rec.case_number, rec.status,
                    len(briefs), " | ".join(b.title for b in briefs),
                    rec.caption, rec.case_type, rec.docket_url,
                    " | ".join(b.url for b in briefs), rec.note,
                ])

    # 2. Monthly TXT hit-list
    scanned_dates = sum(1 for e in date_entries if e["records"])
    completed_dates = sum(1 for e in date_entries if e["progress"].is_complete)
    total_hit_entries = sum(len(e["hits"]) for e in date_entries)
    total_brief_pdfs = sum(len(r.briefs or []) for e in date_entries for r in e["hits"])

    txt_lines = [
        f"NY APPELLATE 2ND DEPT — MONTHLY MASTER HIT-LIST ({year_month})",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Dates Completed: {completed_dates}/{len(date_entries)} (Started: {scanned_dates}) | Unique Valid Case #s: {len(unique_hits_order)} | Brief PDFs: {total_brief_pdfs}",
        "=" * 78,
        "",
        f"UNIQUE VALID CASE NUMBERS TO OPEN ({len(unique_hits_order)}):",
    ]
    for c in unique_hits_order:
        txt_lines.append(f"  - {c}  (dates: {', '.join(case_dates_map[c])})")

    txt_lines.append("")
    txt_lines.append("BREAKDOWN BY MOTION LIST DATE:")
    for entry in date_entries:
        txt_lines.append(f"\n[{entry['date_tag']}] {entry['option'].filename} — {entry['progress'].badge}")
        for r in entry["hits"]:
            titles = ", ".join(b.title for b in (r.briefs or []))
            txt_lines.append(f"    [HIT] {r.case_number} | {r.caption} | {titles}")

    txt_path = month_dir / "monthly_hit_list.txt"
    txt_path.write_text("\n".join(txt_lines) + "\n", encoding="utf-8")

    # 3. Clean Monthly Dark-Mode HTML Dashboard
    html_path = month_dir / "monthly_hit_list.html"
    html_path.write_text(
        _render_monthly_html(
            year_month=year_month,
            date_entries=date_entries,
            unique_hits_order=unique_hits_order,
            case_dates_map=case_dates_map,
            completed_dates=completed_dates,
            scanned_dates=scanned_dates,
            total_hit_entries=total_hit_entries,
            total_brief_pdfs=total_brief_pdfs,
        ),
        encoding="utf-8",
    )

    return {"html": html_path, "txt": txt_path, "csv": csv_path}


def _render_brief_row(title: str, row_summary: str) -> str:
    meta = parse_row_meta(title, row_summary)
    flag_html = f'<span class="flag-badge">{html.escape(meta["flag"])}</span>' if meta["flag"] else ""
    meta_str = " • ".join(p for p in (meta["doc_num"], meta["filed_date"]) if p)
    return (
        f'<div class="brief-row">'
        f'  <div class="brief-title-wrap"><span class="brief-badge">{html.escape(title)}</span>{flag_html}</div>'
        f'  <span class="brief-meta">{html.escape(meta_str)}</span>'
        f'</div>'
    )


def _render_monthly_html(
    year_month: str,
    date_entries: list[dict],
    unique_hits_order: list[str],
    case_dates_map: dict[str, list[str]],
    completed_dates: int,
    scanned_dates: int,
    total_hit_entries: int,
    total_brief_pdfs: int,
) -> str:
    status_rows = []
    grouped_sections = []
    storage_key = f"ny_monthly_done::{year_month}"

    for entry in date_entries:
        opt = entry["option"]
        prog: OptionProgress = entry["progress"]
        date_report = entry["run_dir"] / "hit_list.html"
        link_cell = (
            f'<a class="link" href="{html.escape(date_report.resolve().as_uri(), quote=True)}">Open Date Report</a>'
            if date_report.exists()
            else '<span class="brief-meta">Not scanned yet</span>'
        )
        pill_cls = "pill-done" if prog.is_complete else ("pill-part" if prog.completed > 0 else "pill-pend")
        date_words = format_date_words(entry["date_tag"])
        status_rows.append(
            f"<tr>"
            f"<td class='mono'>{html.escape(date_words)}</td>"
            f"<td>{html.escape(opt.filename)}</td>"
            f"<td><span class='pill {pill_cls}'>{html.escape(prog.badge)}</span></td>"
            f"<td>{link_cell}</td>"
            f"</tr>"
        )

        if not entry["hits"]:
            continue

        cards = []
        for r in entry["hits"]:
            other_dates = [format_date_words(d) for d in case_dates_map.get(r.case_number, []) if d != entry["date_tag"]]
            dup_badge = (
                f'<span class="dup-pill">Also on {", ".join(other_dates)}</span>'
                if other_dates else ""
            )
            ctype = short_case_type(r.case_type)
            type_pill = f'<span class="type-pill">{html.escape(ctype)}</span>' if ctype else ""
            pdf_cnt = len(r.briefs or [])
            pdf_pill = f'<span class="type-pill">{pdf_cnt} Brief PDF{"s" if pdf_cnt != 1 else ""}</span>'
            brief_rows_html = "".join(_render_brief_row(b.title, b.row_summary) for b in (r.briefs or []))
            card_key = f"{entry['date_tag']}::{r.case_number}"
            search_str = f"{r.case_number} {r.caption} {ctype} {entry['date_tag']} {date_words}"
            cards.append(f"""
            <div class="hit-card" data-key="{html.escape(card_key, quote=True)}" data-search="{html.escape(search_str, quote=True)}">
              <div class="hit-head">
                <div class="hit-left">
                  <input type="checkbox" class="done-chk" title="Mark case downloaded" />
                  <span class="case-num">{html.escape(r.case_number)}</span>
                  {pdf_pill}
                  {type_pill}
                  {dup_badge}
                </div>
                <button class="btn" data-copy="{html.escape(r.case_number, quote=True)}" onclick="copyFromAttr(this)">Copy</button>
              </div>
              <div class="caption">{html.escape(r.caption)}</div>
              <div class="brief-rows">{brief_rows_html}</div>
            </div>
            """)

        date_pdf_cnt = sum(len(r.briefs or []) for r in entry["hits"])
        grouped_sections.append(f"""
        <div class="date-section">
          <div class="date-header">
            <h3>{html.escape(date_words)} — {len(entry['hits'])} valid case(s) • {date_pdf_cnt} Brief PDF(s) to download</h3>
            {link_cell}
          </div>
          {"".join(cards)}
        </div>
        """)

    if not grouped_sections:
        grouped_sections.append('<div class="empty-box">No clickable BRIEF cases found in scanned dates so far.</div>')

    copy_all_unique = "\n".join(unique_hits_order)
    copy_disabled = "" if unique_hits_order else " disabled"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Monthly Master Hit-List ({html.escape(year_month)})</title>
<style>{SHARED_CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div>
      <h1 class="title">NY Briefs Monthly Master — {html.escape(format_month_words(year_month))} ({html.escape(year_month)})</h1>
      <div class="subtitle">{completed_dates}/{len(date_entries)} lists completed ({scanned_dates} started) • {total_brief_pdfs} brief PDF(s)</div>
    </div>
    <div class="actions-bar">
      <div class="progress-pill" id="done-counter"><strong>0</strong> / {total_hit_entries} downloaded</div>
      <button class="btn btn-primary" data-copy="{html.escape(copy_all_unique, quote=True)}" onclick="copyFromAttr(this)"{copy_disabled}>Copy All Unique ({len(unique_hits_order)})</button>
    </div>
  </div>

  <div class="kpi-strip">
    <div class="kpi"><div class="kpi-val green">{len(unique_hits_order)}</div><div class="kpi-lbl">Unique Case #s</div></div>
    <div class="kpi"><div class="kpi-val green">{total_brief_pdfs}</div><div class="kpi-lbl">Brief PDFs Total</div></div>
    <div class="kpi"><div class="kpi-val">{completed_dates}/{len(date_entries)}</div><div class="kpi-lbl">Month Progress</div></div>
    <div class="kpi"><div class="kpi-val">{total_hit_entries}</div><div class="kpi-lbl">Date-Case Entries</div></div>
  </div>

  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab('tab-cases', this)">Open on Company Laptop ({total_hit_entries})</button>
    <button class="tab-btn" onclick="switchTab('tab-dates', this)">Date Checklist ({completed_dates}/{len(date_entries)})</button>
  </div>

  <div id="tab-cases" class="tab-panel active">
    <div class="toolbar">
      <input id="case-search" class="search-input" type="search" placeholder="Filter by Case #, caption, or date (e.g. 2026-07-31)..." />
      <label class="toggle-lbl"><input type="checkbox" id="hide-done" /> Hide downloaded</label>
    </div>
    {"".join(grouped_sections)}
    <div id="empty-filter-msg" class="empty-box" style="display:none">All cases checked off or filtered out — clear search or uncheck "Hide downloaded".</div>
  </div>

  <div id="tab-dates" class="tab-panel">
    <div class="table-wrap">
      <table>
        <thead><tr><th>Calendar Date</th><th>Motion List PDF</th><th>Status</th><th>Report</th></tr></thead>
        <tbody>{"".join(status_rows)}</tbody>
      </table>
    </div>
  </div>
</div>
<script>
{SHARED_JS}
initChecklist({json.dumps(storage_key)});
</script>
</body>
</html>
"""
