import argparse
from datetime import datetime
from pathlib import Path
import re
import sys
import webbrowser

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from classifier import (
    STATUS_EMPTY_NO_CASE,
    STATUS_HIT_VALID,
    STATUS_NEEDS_MANUAL_CHECK,
    STATUS_NO_BRIEF_FOUND,
    STATUS_SEALED_OR_UNCLICKABLE,
)
from config import MAX_DELAY_SEC, MIN_DELAY_SEC, OUTPUT_DIR, STOP_EVENT
from monthly_report import (
    inspect_option_progress,
    parse_selection_indices,
    write_monthly_reports,
)
from nyscef_browser import MotionPdfOption, NyscefEdgeSession
from pdf_parser import ParsedMotionList, parse_motion_pdf_bytes, parse_motion_pdf_file
from report_builder import (
    load_checkpoint,
    save_checkpoint,
    write_all_reports,
)
from ui_theme import format_date_words


def _slugify(name: str) -> str:
    stem = Path(name).stem
    return re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_") or datetime.now().strftime("%Y%m%d_%H%M%S")


def _run_dir_for_option(selected: MotionPdfOption) -> Path:
    slug = _slugify(selected.filename)
    if selected.date_iso:
        yyyy, mm, dd = selected.date_iso.split("-")
        expected_prefix = f"{mm}-{dd}-{yyyy}"
        if not selected.filename.startswith(expected_prefix):
            slug = f"{selected.date_iso}__{slug}"
    return OUTPUT_DIR / slug


def _print_banner() -> None:
    print("=" * 72)
    print("  NY Appellate Division (2nd Dept) — Briefs Hit-List Scanner")
    print("  Runs cleanly on Personal Laptop via Native Microsoft Edge (CDP)")
    print("=" * 72)


def group_options_by_month(options: list[MotionPdfOption]) -> dict[str, list[MotionPdfOption]]:
    grouped: dict[str, list[MotionPdfOption]] = {}
    for opt in options:
        ym = opt.year_month or "unknown"
        grouped.setdefault(ym, []).append(opt)
    return grouped


def _prompt_month_selection(months: list[str], grouped: dict[str, list[MotionPdfOption]]) -> str:
    print("\nAvailable Months (type number, or YYYY-MM like 2026-10):")
    recent = months[:24]
    for i, ym in enumerate(recent, start=1):
        done_count = sum(1 for o in grouped[ym] if inspect_option_progress(_run_dir_for_option(o)).is_complete)
        done_tag = f" | {done_count} done" if done_count else ""
        print(f"  [{i:2d}] {ym} ({len(grouped[ym])} Motion Lists{done_tag})")
    if len(months) > len(recent):
        print(f"  ... ({len(months) - len(recent)} older months back to {months[-1]} also available via YYYY-MM)")

    while True:
        raw = input("\nSelect month [1]: ").strip() or "1"
        if raw in grouped:
            return raw
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(recent):
                return recent[idx]
        print("Invalid month. Enter a list number or YYYY-MM (e.g. 2026-10).")


def _format_case_log_line(
    idx: int,
    total: int,
    case_no: str,
    rec,
    parsed_titles: dict[str, str],
) -> str:
    tag = {
        STATUS_HIT_VALID: "HIT ",
        STATUS_SEALED_OR_UNCLICKABLE: "SEAL",
        STATUS_NO_BRIEF_FOUND: "NOBR",
        STATUS_EMPTY_NO_CASE: "EMPT",
        STATUS_NEEDS_MANUAL_CHECK: "WARN",
    }.get(rec.status, "INFO")

    extra = rec.note
    if rec.status == STATUS_HIT_VALID and rec.briefs:
        n = len(rec.briefs)
        numbered = ", ".join(f"[{i}] {b.title}" for i, b in enumerate(rec.briefs, 1))
        extra = f"{n} PDF{'s' if n != 1 else ''}: {numbered}"
    title_col = rec.caption or parsed_titles.get(case_no, "")
    return f"[{idx:02d}/{total:02d}] [{tag}] {case_no} | {title_col[:40]:<40} | {extra}"


def _scan_single_parsed(
    session: NyscefEdgeSession,
    parsed: ParsedMotionList,
    run_dir: Path,
    args: argparse.Namespace,
    batch_tag: str = "",
) -> tuple[dict[str, Path], bool]:
    cases = parsed.cases[: args.limit] if args.limit and args.limit > 0 else parsed.cases
    total = len(cases)
    prefix = f"{batch_tag} " if batch_tag else ""
    date_words = format_date_words(parsed.motion_date_hint or parsed.source_name)
    print(
        f"\n{prefix}[2/3] PDF Parsed: {parsed.source_name} ({parsed.page_count} pages, "
        f"{parsed.raw_match_count} matches -> {len(parsed.cases)} unique case numbers)."
    )
    if args.limit and args.limit > 0:
        print(f"      Test Limit active: checking first {total} case(s).")

    records = load_checkpoint(run_dir)
    resumed_done = sum(
        1 for c in cases
        if c in records and records[c].status != STATUS_NEEDS_MANUAL_CHECK
    )

    meta = {
        "source_name": parsed.source_name,
        "motion_date_hint": parsed.motion_date_hint,
        "total_unique_cases": len(parsed.cases),
    }

    if resumed_done == total and not args.fresh:
        print(
            f"{prefix}[3/3] Already scanned ({resumed_done}/{total} saved in checkpoint). "
            "Showing saved case-by-case breakdown below (tick 'Re-scan from scratch (verify)' to re-check live on NYSCEF):\n"
        )
    elif resumed_done > 0 and not args.fresh:
        print(
            f"{prefix}[3/3] Resuming from checkpoint ({resumed_done}/{total} already saved). "
            f"Checking remaining {total - resumed_done} case(s) live on NYSCEF..."
        )
        print("      Tip: If NYSCEF shows 'I am not a robot' once in Edge, click it and the scan continues.\n")
    elif args.fresh and resumed_done > 0:
        print(f"{prefix}[3/3] Re-scanning all {total} case(s) live on NYSCEF from scratch (verification mode)...")
        print("      Tip: If NYSCEF shows 'I am not a robot' once in Edge, click it and the scan continues.\n")
    else:
        print(f"{prefix}[3/3] Checking {total} case(s) live on NYSCEF (Appellate Division - 2nd Dept)...")
        print("      Tip: If NYSCEF shows 'I am not a robot' once in Edge, click it and the scan continues.\n")

    interrupted = False
    backfilled_captions = False
    try:
        for idx, case_no in enumerate(cases, start=1):
            if STOP_EVENT.is_set():
                interrupted = True
                print("\nStopped by user — saving checkpoint and partial Hit-List reports...")
                break
            existing = records.get(case_no)
            if existing and existing.status != STATUS_NEEDS_MANUAL_CHECK and not args.fresh:
                if not existing.caption and case_no in parsed.case_titles:
                    existing.caption = parsed.case_titles[case_no]
                    backfilled_captions = True
                print(_format_case_log_line(idx, total, case_no, existing, parsed.case_titles), flush=True)
                continue

            rec = session.check_case(case_no, min_delay=args.min_delay, max_delay=args.max_delay)
            if not rec.caption and case_no in parsed.case_titles:
                rec.caption = parsed.case_titles[case_no]
            records[case_no] = rec
            save_checkpoint(run_dir, meta, records)
            print(_format_case_log_line(idx, total, case_no, rec, parsed.case_titles), flush=True)
    except KeyboardInterrupt:
        interrupted = True
        print("\nInterrupted by user — saving checkpoint and partial Hit-List reports...")

    if backfilled_captions:
        save_checkpoint(run_dir, meta, records)

    reports = write_all_reports(run_dir, meta, cases, records)
    hits = [records[c] for c in cases if c in records and records[c].status == STATUS_HIT_VALID]
    all_brief_pdfs = [(rec.case_number, rec.caption, b.title) for rec in hits for b in (rec.briefs or [])]
    total_pdfs = len(all_brief_pdfs)
    sealed_cnt = sum(1 for c in cases if c in records and records[c].status == STATUS_SEALED_OR_UNCLICKABLE)
    nobr_cnt = sum(1 for c in cases if c in records and records[c].status == STATUS_NO_BRIEF_FOUND)
    empty_cnt = sum(1 for c in cases if c in records and records[c].status == STATUS_EMPTY_NO_CASE)
    manual_cnt = sum(1 for c in cases if c in records and records[c].status == STATUS_NEEDS_MANUAL_CHECK)

    print("\n" + "-" * 72)
    print(f"  {date_words} ({parsed.source_name}) — SCAN SUMMARY:")
    print(f"  • Total Unique Cases in PDF    : {total}")
    print(f"  • Valid Brief Cases (HIT)      : {len(hits):<3} case(s)")
    print(f"  • Total Brief PDFs to Download : {total_pdfs:<3} Brief PDF(s)")
    print(f"  • Skipped — Sealed Brief       : {sealed_cnt:<3} (plain-text unclickable brief on NYSCEF)")
    print(f"  • Skipped — No Brief Filed     : {nobr_cnt:<3} (case exists in NYSCEF, 0 briefs filed)")
    print(f"  • Skipped — Not in NYSCEF      : {empty_cnt:<3} (no e-filed record, e.g. criminal/paper appeal)")
    if manual_cnt:
        print(f"  • Needs Manual Recheck         : {manual_cnt:<3} (temporary timeout or unverified response)")
    if all_brief_pdfs:
        print(f"  • All Brief PDFs to Download ({total_pdfs} PDF(s) across {len(hits)} case(s)):")
        for pdf_idx, (c_no, cap, b_title) in enumerate(all_brief_pdfs, 1):
            print(f"    [PDF {pdf_idx:02d}/{total_pdfs:02d}] {c_no} | {cap} -> {b_title}")
    print(f"  Date Report: {reports['html']}")
    print("-" * 72)
    return reports, interrupted


def _run_option_batch(
    session: NyscefEdgeSession,
    selected_opts: list[MotionPdfOption],
    month_opts: list[MotionPdfOption],
    year_month: str,
    args: argparse.Namespace,
) -> None:
    if not selected_opts:
        print("\nAll Motion Lists in this selection are already DONE! (Use --fresh to rescan.)")
        return

    last_date_report: dict[str, Path] | None = None
    total_batch = len(selected_opts)

    for pos, selected in enumerate(selected_opts, start=1):
        run_dir = _run_dir_for_option(selected)
        saved_pdf = run_dir / selected.filename
        batch_tag = f"[Date {pos}/{total_batch}]"
        date_words = format_date_words(selected.date_iso or selected.filename)
        print(f"\n{batch_tag} [1/3] Downloading Motion List PDF for {date_words} ({selected.filename}) via Edge browser...")
        pdf_bytes = session.download_motion_pdf(selected, saved_pdf)
        try:
            from gui_hits import ny_motion_lists_dir
            ml_dest = ny_motion_lists_dir(year_month) / selected.filename
            if pdf_bytes and (not ml_dest.exists() or ml_dest.stat().st_size != len(pdf_bytes)):
                ml_dest.write_bytes(pdf_bytes)
        except Exception:
            pass
        parsed = parse_motion_pdf_bytes(pdf_bytes, selected.filename)

        last_date_report, interrupted = _scan_single_parsed(session, parsed, run_dir, args, batch_tag=batch_tag)
        monthly_reports = write_monthly_reports(OUTPUT_DIR, year_month, month_opts, _run_dir_for_option)
        if interrupted:
            print(f"Monthly Master Hit-List updated with partial progress: {monthly_reports['html']}")
            break
    else:
        monthly_reports = write_monthly_reports(OUTPUT_DIR, year_month, month_opts, _run_dir_for_option)

    print(f"\nMonthly Combined Hit-List ({year_month}): {monthly_reports['html']}")
    if not args.no_open:
        target_open = monthly_reports["html"] if total_batch > 1 else (last_date_report["html"] if last_date_report else monthly_reports["html"])
        webbrowser.open(target_open.as_uri())


def _interactive_loop(session: NyscefEdgeSession, args: argparse.Namespace) -> int:
    print("\n[1/3] Fetching Motion List calendar from NY Courts AD2 Handdowns...")
    options = session.list_handdown_motion_pdfs()
    if not options:
        raise RuntimeError("No Motion List PDFs found on the Handdowns page.")

    grouped = group_options_by_month(options)
    months = list(grouped.keys())
    active_month = args.month if args.month in grouped else months[0]
    if args.month and args.month not in grouped:
        print(f"Warning: Month '{args.month}' not found. Showing {active_month} instead.")

    while True:
        month_opts = grouped[active_month]
        done_indices: set[int] = set()
        print(f"\nMotion List PDFs for {active_month} ({len(month_opts)} total, newest first):")
        for idx, opt in enumerate(month_opts, start=1):
            prog = inspect_option_progress(_run_dir_for_option(opt))
            if prog.is_complete:
                done_indices.add(idx - 1)
            print(f"  [{idx:2d}] {opt.label:<42} {prog.badge}")

        remaining_count = len(month_opts) - len(done_indices)
        print("\nCommands:")
        print(f"  1 or 1-3 or 1,2,5 -> Scan specific date(s)")
        print(f"  [A] Scan ALL remaining PENDING/PARTIAL dates in {active_month} ({remaining_count} left)")
        print(f"  [R] Open Combined Monthly Hit-List for {active_month}")
        print(f"  [M] Switch Month/Year (all {len(months)} months from {months[-1]} to {months[0]})")
        print("  [P] Load a local PDF path")
        print("  [Q] Quit")

        raw_choice = input("\nSelect action [A]: ").strip() or "A"
        upper = raw_choice.upper()

        if upper in ("Q", "QUIT", "EXIT"):
            print("Closing Edge session. Goodbye!")
            return 0

        if upper == "M":
            active_month = _prompt_month_selection(months, grouped)
            continue

        if raw_choice in grouped:
            active_month = raw_choice
            continue

        if upper == "R":
            monthly = write_monthly_reports(OUTPUT_DIR, active_month, month_opts, _run_dir_for_option)
            print(f"\nMonthly Combined Hit-List ready: {monthly['html']}")
            if not args.no_open:
                webbrowser.open(monthly["html"].as_uri())
            continue

        if upper == "P":
            raw_path = input("Enter full path to Motion List PDF: ").strip().strip('"')
            parsed = parse_motion_pdf_file(raw_path)
            run_dir = OUTPUT_DIR / _slugify(parsed.source_name)
            reports, _ = _scan_single_parsed(session, parsed, run_dir, args)
            if not args.no_open:
                webbrowser.open(reports["html"].as_uri())
            continue

        if upper == "A":
            pending_opts = [
                opt for idx, opt in enumerate(month_opts)
                if args.fresh or idx not in done_indices
            ]
            _run_option_batch(session, pending_opts, month_opts, active_month, args)
            continue

        try:
            picked_indices = parse_selection_indices(raw_choice, len(month_opts))
        except ValueError as exc:
            print(f"Invalid input: {exc}")
            continue

        selected_opts = [month_opts[i] for i in picked_indices]
        _run_option_batch(session, selected_opts, month_opts, active_month, args)


def run_scanner(args: argparse.Namespace) -> int:
    _print_banner()

    with NyscefEdgeSession() as session:
        if args.pdf:
            parsed = parse_motion_pdf_file(args.pdf)
            run_dir = OUTPUT_DIR / _slugify(parsed.source_name)
            reports, _ = _scan_single_parsed(session, parsed, run_dir, args)
            if not args.no_open:
                webbrowser.open(reports["html"].as_uri())
            return 0

        if args.auto_latest or args.all_month:
            options = session.list_handdown_motion_pdfs()
            if not options:
                raise RuntimeError("No Motion List PDFs found on Handdowns.")
            grouped = group_options_by_month(options)
            months = list(grouped.keys())
            active_month = args.month if args.month in grouped else months[0]
            month_opts = grouped[active_month]

            if args.all_month:
                pending_opts = [
                    opt for opt in month_opts
                    if args.fresh or not inspect_option_progress(_run_dir_for_option(opt)).is_complete
                ]
                _run_option_batch(session, pending_opts, month_opts, active_month, args)
            else:
                _run_option_batch(session, [month_opts[0]], month_opts, active_month, args)
            return 0

        return _interactive_loop(session, args)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NY Appellate Division 2nd Dept NYSCEF Brief Hit-List Scanner")
    parser.add_argument("--pdf", help="Path to a local MOTION_LIST.pdf (skips Handdowns download)")
    parser.add_argument("--month", help="Preselect target month YYYY-MM (e.g. 2026-07)")
    parser.add_argument("--auto-latest", action="store_true", help="Scan newest Motion List PDF in selected month and exit")
    parser.add_argument("--all-month", action="store_true", help="Scan all remaining dates in selected month and exit")
    parser.add_argument("--limit", type=int, default=0, help="Optional cap on number of cases per PDF (for quick tests)")
    parser.add_argument("--fresh", action="store_true", help="Ignore existing checkpoint and re-scan all cases")
    parser.add_argument("--min-delay", type=float, default=MIN_DELAY_SEC, help="Minimum polite delay between cases (seconds)")
    parser.add_argument("--max-delay", type=float, default=MAX_DELAY_SEC, help="Maximum polite delay between cases (seconds)")
    parser.add_argument("--no-open", action="store_true", help="Do not automatically open HTML reports in browser")
    return parser


if __name__ == "__main__":
    args = build_arg_parser().parse_args()
    raise SystemExit(run_scanner(args))
