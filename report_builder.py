import csv
import html
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from classifier import (
    BriefLink,
    STATUS_EMPTY_NO_CASE,
    STATUS_HIT_VALID,
    STATUS_NEEDS_MANUAL_CHECK,
    STATUS_NO_BRIEF_FOUND,
    STATUS_SEALED_OR_UNCLICKABLE,
)
from ui_theme import SHARED_CSS, SHARED_JS, format_date_words, parse_row_meta, short_case_type


@dataclass
class CaseScanRecord:
    case_number: str
    status: str
    caption: str = ""
    case_type: str = ""
    docket_url: str = ""
    note: str = ""
    briefs: list[BriefLink] | None = None
    checked_at: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["briefs"] = [asdict(b) for b in (self.briefs or [])]
        return data

    @classmethod
    def from_dict(cls, raw: dict) -> "CaseScanRecord":
        briefs = [BriefLink(**b) for b in raw.get("briefs", [])]
        return cls(
            case_number=raw["case_number"],
            status=raw["status"],
            caption=raw.get("caption", ""),
            case_type=raw.get("case_type", ""),
            docket_url=raw.get("docket_url", ""),
            note=raw.get("note", ""),
            briefs=briefs,
            checked_at=raw.get("checked_at", ""),
        )


def save_checkpoint(run_dir: Path, meta: dict, records: dict[str, CaseScanRecord]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": meta,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "records": {k: v.to_dict() for k, v in records.items()},
    }
    checkpoint_path = run_dir / "checkpoint.json"
    tmp_path = run_dir / "checkpoint.json.tmp"
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(checkpoint_path)


def load_checkpoint(run_dir: Path) -> dict[str, CaseScanRecord]:
    checkpoint_path = run_dir / "checkpoint.json"
    if not checkpoint_path.exists():
        return {}
    raw = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    return {case_no: CaseScanRecord.from_dict(rec_dict) for case_no, rec_dict in raw.get("records", {}).items()}


def write_all_reports(run_dir: Path, meta: dict, ordered_cases: list[str], records: dict[str, CaseScanRecord]) -> dict[str, Path]:
    run_dir.mkdir(parents=True, exist_ok=True)
    ordered_records = [records[c] for c in ordered_cases if c in records]

    # 1. CSV report
    csv_path = run_dir / "scan_results.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Case Number", "Status", "Clickable Brief Count", "Brief Titles",
            "Caption", "Case Type", "Docket URL", "Brief URLs", "Note", "Checked At",
        ])
        for rec in ordered_records:
            briefs = rec.briefs or []
            writer.writerow([
                rec.case_number, rec.status, len(briefs),
                " | ".join(b.title for b in briefs), rec.caption, rec.case_type,
                rec.docket_url, " | ".join(b.url for b in briefs), rec.note, rec.checked_at,
            ])

    # 2. Plain TXT Hit-List
    hits = [r for r in ordered_records if r.status == STATUS_HIT_VALID]
    manual_checks = [r for r in ordered_records if r.status == STATUS_NEEDS_MANUAL_CHECK]
    sealed = [r for r in ordered_records if r.status == STATUS_SEALED_OR_UNCLICKABLE]
    no_brief = [r for r in ordered_records if r.status == STATUS_NO_BRIEF_FOUND]
    empty = [r for r in ordered_records if r.status == STATUS_EMPTY_NO_CASE]

    txt_lines = [
        f"NY APPELLATE 2ND DEPT — BRIEF HIT-LIST ({meta.get('source_name', '')})",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Checked: {len(ordered_records)}/{len(ordered_cases)} | HITS: {len(hits)} | Sealed: {len(sealed)} | No Brief: {len(no_brief)} | Empty: {len(empty)} | Manual: {len(manual_checks)}",
        "=" * 72,
        "",
        f"VALID CASES TO OPEN ON COMPANY LAPTOP ({len(hits)}):",
    ]
    for r in hits:
        titles = ", ".join(b.title for b in (r.briefs or []))
        txt_lines.append(f"  [HIT] {r.case_number}  |  {r.caption}  |  Briefs ({len(r.briefs or [])}): {titles}")
    if manual_checks:
        txt_lines.extend(["", f"NEEDS MANUAL CHECK ({len(manual_checks)}):"])
        for r in manual_checks:
            txt_lines.append(f"  [!] {r.case_number}  |  {r.note}")

    txt_path = run_dir / "hit_list.txt"
    txt_path.write_text("\n".join(txt_lines) + "\n", encoding="utf-8")

    # 3. Clean Checklist Dark-Mode HTML Dashboard
    html_path = run_dir / "hit_list.html"
    html_path.write_text(
        _render_html_dashboard(meta, ordered_cases, ordered_records, hits, manual_checks, sealed, no_brief, empty),
        encoding="utf-8",
    )

    return {
        "html": html_path,
        "txt": txt_path,
        "csv": csv_path,
        "checkpoint": run_dir / "checkpoint.json",
    }


def _render_brief_row(title: str, row_summary: str) -> str:
    meta = parse_row_meta(title, row_summary)
    flag_html = f'<span class="flag-badge">{html.escape(meta["flag"])}</span>' if meta["flag"] else ""
    meta_parts = [p for p in (meta["doc_num"], meta["filed_date"]) if p]
    meta_str = " • ".join(meta_parts)
    return (
        f'<div class="brief-row">'
        f'  <div class="brief-title-wrap"><span class="brief-badge">{html.escape(title)}</span>{flag_html}</div>'
        f'  <span class="brief-meta">{html.escape(meta_str)}</span>'
        f'</div>'
    )


def _render_html_dashboard(
    meta: dict,
    ordered_cases: list[str],
    ordered_records: list[CaseScanRecord],
    hits: list[CaseScanRecord],
    manual_checks: list[CaseScanRecord],
    sealed: list[CaseScanRecord],
    no_brief: list[CaseScanRecord],
    empty: list[CaseScanRecord],
) -> str:
    source_raw = str(meta.get("source_name", "Motion List"))
    source_name = html.escape(source_raw)
    date_display = html.escape(format_date_words(str(meta.get("motion_date_hint") or source_raw)))
    storage_key = f"ny_briefs_done::{source_raw}"
    total_pdfs = sum(len(r.briefs or []) for r in hits)

    hit_cards_html: list[str] = []
    for r in hits:
        brief_rows_html = "".join(_render_brief_row(b.title, b.row_summary) for b in (r.briefs or []))
        ctype = short_case_type(r.case_type)
        type_pill = f'<span class="type-pill">{html.escape(ctype)}</span>' if ctype else ""
        pdf_cnt = len(r.briefs or [])
        pdf_pill = f'<span class="type-pill">{pdf_cnt} Brief PDF{"s" if pdf_cnt != 1 else ""}</span>'
        search_str = f"{r.case_number} {r.caption} {ctype}"
        hit_cards_html.append(f"""
        <div class="hit-card" data-key="{html.escape(r.case_number, quote=True)}" data-search="{html.escape(search_str, quote=True)}">
          <div class="hit-head">
            <div class="hit-left">
              <input type="checkbox" class="done-chk" title="Mark case downloaded" />
              <span class="case-num">{html.escape(r.case_number)}</span>
              {pdf_pill}
              {type_pill}
            </div>
            <button class="btn" data-copy="{html.escape(r.case_number, quote=True)}" onclick="copyFromAttr(this)">Copy</button>
          </div>
          <div class="caption">{html.escape(r.caption)}</div>
          <div class="brief-rows">{brief_rows_html}</div>
        </div>
        """)

    if not hit_cards_html:
        hit_cards_html.append('<div class="empty-box">No clickable BRIEF cases found in this scan.</div>')

    skipped_rows: list[str] = []
    for kind, label, group in (
        ("sealed", "Sealed / Unclickable", sealed),
        ("nobrief", "No Brief", no_brief),
        ("empty", "Not Found", empty),
    ):
        for r in group:
            ctype = short_case_type(r.case_type) or "-"
            skipped_rows.append(
                f"<tr data-kind='{kind}'>"
                f"<td class='mono'>{html.escape(r.case_number)}</td>"
                f"<td><span class='pill pill-pend'>{label}</span></td>"
                f"<td>{html.escape(r.caption or '-')}</td>"
                f"<td>{html.escape(ctype)}</td>"
                f"</tr>"
            )
    if not skipped_rows:
        skipped_rows.append('<tr><td colspan="4" class="mono">None</td></tr>')

    manual_rows = "".join(
        f"<tr><td class='mono'>{html.escape(r.case_number)}</td>"
        f"<td>{html.escape(r.caption or '-')}</td>"
        f"<td>{html.escape(r.note or r.status)}</td></tr>"
        for r in manual_checks
    ) or '<tr><td colspan="3">All cases verified cleanly — zero errors.</td></tr>'

    copy_all_str = "\n".join(r.case_number for r in hits)
    copy_disabled = "" if hits else " disabled"
    total_skipped = len(sealed) + len(no_brief) + len(empty)
    manual_kpi_html = (
        f'<div class="kpi"><div class="kpi-val warn">{len(manual_checks)}</div><div class="kpi-lbl">Needs Check</div></div>'
        if manual_checks else ""
    )
    manual_tab_html = (
        f'<button class="tab-btn" onclick="switchTab(\'tab-manual\', this)">Needs Check ({len(manual_checks)})</button>'
        if manual_checks else ""
    )
    manual_panel_html = (
        f"""
  <div id="tab-manual" class="tab-panel">
    <div class="table-wrap">
      <table>
        <thead><tr><th>Case #</th><th>Caption</th><th>Error / Reason</th></tr></thead>
        <tbody>{manual_rows}</tbody>
      </table>
    </div>
  </div>"""
        if manual_checks else ""
    )
    sub_filters_html = (
        f"""<div class="sub-filters">
      <button class="chip-btn active" onclick="filterSkipped('all', this)">All ({total_skipped})</button>
      <button class="chip-btn" onclick="filterSkipped('sealed', this)">Sealed ({len(sealed)})</button>
      <button class="chip-btn" onclick="filterSkipped('nobrief', this)">No Brief ({len(no_brief)})</button>
      <button class="chip-btn" onclick="filterSkipped('empty', this)">Not Found ({len(empty)})</button>
    </div>"""
        if total_skipped else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Briefs Hit-List — {source_name}</title>
<style>{SHARED_CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div>
      <h1 class="title">NY Briefs Hit-List — {date_display}</h1>
      <div class="subtitle">{source_name} • Scanned {len(ordered_records)}/{len(ordered_cases)} cases • {total_pdfs} brief PDF(s) ready to download</div>
    </div>
    <div class="actions-bar">
      <div class="progress-pill" id="done-counter"><strong>0</strong> / {len(hits)} downloaded</div>
      <button class="btn btn-primary" data-copy="{html.escape(copy_all_str, quote=True)}" onclick="copyFromAttr(this)"{copy_disabled}>Copy All ({len(hits)})</button>
    </div>
  </div>

  <div class="kpi-strip">
    <div class="kpi"><div class="kpi-val green">{len(hits)}</div><div class="kpi-lbl">Cases to Open</div></div>
    <div class="kpi"><div class="kpi-val green">{total_pdfs}</div><div class="kpi-lbl">Brief PDFs Total</div></div>
    <div class="kpi"><div class="kpi-val">{len(sealed)}</div><div class="kpi-lbl">Sealed / Unclickable</div></div>
    <div class="kpi"><div class="kpi-val">{len(no_brief) + len(empty)}</div><div class="kpi-lbl">No Brief / Empty</div></div>
    {manual_kpi_html}
  </div>

  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab('tab-action', this)">Open on Company Laptop ({len(hits)})</button>
    <button class="tab-btn" onclick="switchTab('tab-skipped', this)">Skipped Audit ({total_skipped})</button>
    {manual_tab_html}
  </div>

  <div id="tab-action" class="tab-panel active">
    <div class="toolbar">
      <input id="case-search" class="search-input" type="search" placeholder="Filter by Case # or caption..." />
      <label class="toggle-lbl"><input type="checkbox" id="hide-done" /> Hide downloaded</label>
    </div>
    {"".join(hit_cards_html)}
    <div id="empty-filter-msg" class="empty-box" style="display:none">All cases checked off or filtered out — clear search or uncheck "Hide downloaded".</div>
  </div>

  <div id="tab-skipped" class="tab-panel">
    {sub_filters_html}
    <div class="table-wrap">
      <table>
        <thead><tr><th>Case #</th><th>Reason</th><th>Caption</th><th>Case Type</th></tr></thead>
        <tbody id="skipped-tbody">{"".join(skipped_rows)}</tbody>
      </table>
    </div>
  </div>{manual_panel_html}
</div>
<script>
{SHARED_JS}
initChecklist({json.dumps(storage_key)});
</script>
</body>
</html>
"""
