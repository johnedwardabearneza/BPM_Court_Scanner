import csv
import datetime as dt
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from pathlib import Path
from typing import Any
from tkinter import filedialog, messagebox, ttk

from config import DATA_DIR, DOWNLOADS_DIR, DOWNLOADS_KY_DIR, OUTPUT_DIR, STOP_EVENT
from ky_scanner import (
    DEFAULT_KY_INTERN_NAME,
    KY_COURTS,
    KY_INTERN_NAMES,
    KyCaseRecord,
    KyCollectibleDoc,
    KySearchRow,
    _normalize_iso_date,
    assign_ky_save_as_names,
    build_ky_tracker_rows,
    download_ky_pdf,
    fetch_ky_month_search_rows,
    has_ky_multi_volume,
    ky_doc_volume_number,
    ky_download_history_path,
    ky_downloads_dir,
    ky_output_dir,
    load_ky_download_tracker_rows,
    load_ky_checkpoint,
    load_ky_search_rows,
    record_ky_download_history,
    save_ky_checkpoint,
    scan_ky_case,
)
from ky_xlsx import write_ky_tracker_xlsx
from ui_theme import format_date_words, format_month_words

BG, CARD, LINE = "#111111", "#1a1a1a", "#2a2a2a"
TEXT, MUTED, ACCENT = "#ededed", "#8a8a8a", "#4ade80"
WARN_COLOR = "#f5b942"
ERR_COLOR = "#f87171"
ERR_BG = "#2e1212"
WARN_BG = "#2a2415"
WHOLE_MONTH = "Whole month"


def build_ky_month_keys(months_back: int = 120) -> tuple[str, ...]:
    """Generate recent YYYY-MM keys dynamically from today's date so all available historical years appear automatically (default 10 years / 120 months back to 2017)."""
    today = dt.date.today()
    y, m = today.year, today.month
    keys: list[str] = []
    for _ in range(months_back):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return tuple(keys)


KY_MONTH_KEYS = build_ky_month_keys(120)


class KyScannerFrame(tk.Frame):
    """Unified Kentucky C-Track (KYCOA / KYSUP) scanner view inside the main GUI window."""

    def __init__(self, parent, app):
        super().__init__(parent, bg=BG)
        self.app = app
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.active_action: str | None = None
        self.search_rows: list[KySearchRow] = []
        self.case_order: list[str] = []
        self.case_meta: dict[str, dict] = {}
        self.records: dict[str, KyCaseRecord] = {}
        self._all_scanned_records: dict[str, KyCaseRecord] = {}
        self._date_label_to_raw: dict[str, str] = {}
        self.done_cases: set[str] = set()
        self.checked_cases: set[str] = set()
        self.pinned_errors: list[dict[str, str]] = []
        self._last_dl_report: dict[str, Any] | None = None
        self._court_map = {label: code for code, label in KY_COURTS}
        self._month_map = {format_month_words(ym): ym for ym in KY_MONTH_KEYS}
        self._after_id: str | None = None
        self._build()
        self._on_filter_change()
        self._after_id = self.after(120, self._poll)
        self.bind("<Destroy>", self._on_destroy)

    def _on_destroy(self, _e=None) -> None:
        if self._after_id:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _btn(self, parent, text, cmd, primary=False):
        return tk.Button(
            parent, text=text, command=cmd, relief="flat", bd=0, padx=12, pady=6, cursor="hand2",
            bg=ACCENT if primary else CARD, fg="#111111" if primary else TEXT,
            activebackground=LINE, activeforeground=TEXT, disabledforeground="#555555",
            font=("Segoe UI", 9, "bold" if primary else "normal"),
        )

    def _build(self) -> None:
        paned = tk.PanedWindow(self, orient="horizontal", bg=LINE, sashwidth=6, bd=0, opaqueresize=True)
        paned.pack(fill="both", expand=True, padx=20, pady=(4, 16))

        left_col = tk.Frame(paned, bg=BG)
        right_col = tk.Frame(paned, bg=BG)
        sw = self.winfo_screenwidth()
        init_left_w = min(540, max(380, int(sw * 0.38)))
        paned.add(left_col, minsize=360, width=init_left_w, stretch="never")
        paned.add(right_col, minsize=380, stretch="always")

        bar1 = tk.Frame(left_col, bg=BG)
        bar1.pack(fill="x", padx=(4, 10), pady=(4, 2))

        tk.Label(bar1, text="Court", bg=BG, fg=MUTED).pack(side="left")
        self.court_var = tk.StringVar(value=KY_COURTS[0][1])
        self.court_box = ttk.Combobox(
            bar1, textvariable=self.court_var, state="readonly", width=22,
            values=[label for _c, label in KY_COURTS],
        )
        self.court_box.pack(side="left", padx=(4, 8))
        self.court_box.bind("<<ComboboxSelected>>", lambda _e: self._on_filter_change())

        tk.Label(bar1, text="Month", bg=BG, fg=MUTED).pack(side="left")
        month_labels = [format_month_words(ym) for ym in KY_MONTH_KEYS]
        self.month_var = tk.StringVar(value=month_labels[0])  # Default to current month
        self.month_box = ttk.Combobox(
            bar1, textvariable=self.month_var, state="readonly", width=18, height=18, values=month_labels,
        )
        self.month_box.pack(side="left", padx=(4, 8))
        self.month_box.bind("<<ComboboxSelected>>", lambda _e: self._on_filter_change())

        self.load_btn = self._btn(bar1, "Load", self.load_month)
        self.load_btn.pack(side="left", padx=(0, 4))

        bar2 = tk.Frame(left_col, bg=BG)
        bar2.pack(fill="x", padx=(4, 10), pady=(2, 6))

        tk.Label(bar2, text="Intern Name", bg=BG, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 6))
        saved_intern = self._load_saved_intern_name()
        self.intern_var = tk.StringVar(value=saved_intern)
        self.intern_box = ttk.Combobox(
            bar2, textvariable=self.intern_var, state="normal", values=KY_INTERN_NAMES,
        )
        self.intern_box.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.intern_box.bind("<<ComboboxSelected>>", lambda _e: self._on_intern_change())
        self.intern_box.bind("<FocusOut>", lambda _e: self._on_intern_change())
        self.intern_box.bind("<KeyRelease>", lambda _e: self._on_intern_change())

        wrap = tk.Frame(left_col, bg=BG)
        wrap.pack(fill="both", expand=True, padx=(4, 10))
        cols = ("num", "date", "case", "subtype", "status")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="extended", height=11)
        for col, label, w, anchor, stretch in (
            ("num", "#", 36, "center", False),
            ("date", "Date", 140, "w", False),
            ("case", "Case #", 98, "w", False),
            ("subtype", "Sub-Type", 210, "w", True),
            ("status", "Status", 160, "w", False),
        ):
            self.tree.heading(col, text=label, anchor=anchor)
            self.tree.column(col, width=w, minwidth=w, anchor=anchor, stretch=stretch)
        self.tree.tag_configure("hit", foreground=ACCENT)
        self.tree.tag_configure("review", foreground="#f5b942")
        self.tree.tag_configure("skip", foreground=MUTED)
        self.tree.tag_configure("pending", foreground=TEXT)
        sc = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        sc_x = ttk.Scrollbar(wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=sc.set, xscrollcommand=sc_x.set)
        sc.pack(side="right", fill="y")
        sc_x.pack(side="bottom", fill="x")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda _e: self.open_selected_case_from_top())
        self.tree.bind("<Control-a>", lambda _e: (self.tree.selection_set(self.tree.get_children()), "break")[1])
        self.tree.bind("<Control-A>", lambda _e: (self.tree.selection_set(self.tree.get_children()), "break")[1])

        actions = tk.Frame(left_col, bg=BG)
        actions.pack(fill="x", padx=(4, 10), pady=(8, 4))
        self.scan_btn = self._btn(actions, "▶  Start Scan", self.start_scan, primary=True)
        self.stop_btn = self._btn(actions, "Stop", self.stop_scan)
        self.scan_btn.pack(side="left", padx=(0, 8))
        self.stop_btn.pack(side="left", padx=(0, 10))
        self.fresh_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            actions, text="Re-scan from scratch (verify)", variable=self.fresh_var,
            bg=BG, fg=MUTED, selectcolor=CARD, activebackground=BG, activeforeground=TEXT,
            highlightthickness=0,
        ).pack(side="left")

        # Dedicated info line so summary is never clipped or overlapped
        sub_info = tk.Frame(left_col, bg=BG)
        sub_info.pack(fill="x", padx=(4, 10), pady=(0, 4))
        self.summary_lbl = tk.Label(sub_info, text="", bg=BG, fg=MUTED, font=("Segoe UI", 8), anchor="w")
        self.summary_lbl.pack(fill="x")

        prog = tk.Frame(left_col, bg=BG)
        prog.pack(fill="x", padx=(4, 10))
        self.bar = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.bar.pack(fill="x")
        self.prog_lbl = tk.Label(prog, text="", bg=BG, fg=MUTED, anchor="w")
        self.prog_lbl.pack(fill="x", pady=(4, 6))

        log_hdr = tk.Frame(left_col, bg=BG)
        log_hdr.pack(fill="x", padx=(4, 10), pady=(4, 2))

        log_lbl = tk.Label(
            log_hdr, text="Scan Log", bg=BG, fg=TEXT, font=("Segoe UI", 10, "bold"), anchor="w",
            cursor="hand2",
        )
        log_lbl.pack(side="left")
        log_lbl.bind("<Button-1>", lambda _e: self.toggle_log_expand())

        self.log_err_pill = tk.Label(
            log_hdr, text="", bg=BG, fg=ERR_COLOR, font=("Segoe UI", 8, "bold"), cursor="hand2"
        )
        self.log_err_pill.pack(side="left", padx=(8, 0))
        self.log_err_pill.bind("<Button-1>", lambda _e: self.show_manual_check_modal())

        self.log_expand_btn = tk.Button(
            log_hdr, text="⤢ Expand", command=self.toggle_log_expand, relief="flat", bd=0,
            padx=8, pady=1, cursor="hand2", bg=CARD, fg=TEXT, activebackground=LINE,
            activeforeground=TEXT, font=("Segoe UI", 8, "bold"),
        )
        self.log_expand_btn.pack(side="right", padx=(4, 0))

        clear_btn = tk.Button(
            log_hdr, text="Clear", command=self._clear_log, relief="flat", bd=0,
            padx=8, pady=1, cursor="hand2", bg=CARD, fg=MUTED, activebackground=LINE,
            activeforeground=TEXT, font=("Segoe UI", 8),
        )
        clear_btn.pack(side="right")

        self.logwrap = tk.Frame(left_col, bg=CARD, highlightbackground=LINE, highlightthickness=1)

        self.pinned_alert_frame = tk.Frame(
            left_col, bg=ERR_BG, highlightbackground=ERR_COLOR, highlightthickness=1
        )
        self.pinned_alert_lbl = tk.Label(
            self.pinned_alert_frame,
            text="",
            bg=ERR_BG,
            fg=ERR_COLOR,
            font=("Segoe UI", 9, "bold"),
            anchor="w",
        )
        self.pinned_alert_lbl.pack(side="left", padx=8, pady=3, fill="x", expand=True)

        self.pinned_alert_view_btn = tk.Button(
            self.pinned_alert_frame,
            text="🔍 Review Issues",
            command=self.show_manual_check_modal,
            relief="flat",
            bd=0,
            padx=8,
            pady=1,
            cursor="hand2",
            bg="#451a1a",
            fg="#fee2e2",
            activebackground="#5c2424",
            activeforeground="#ffffff",
            font=("Segoe UI", 8, "bold"),
        )
        self.pinned_alert_view_btn.pack(side="right", padx=(2, 6), pady=3)

        self.pinned_alert_dismiss_btn = tk.Button(
            self.pinned_alert_frame,
            text="✕",
            command=self.clear_pinned_errors,
            relief="flat",
            bd=0,
            padx=6,
            pady=1,
            cursor="hand2",
            bg=ERR_BG,
            fg="#9ca3af",
            activebackground="#3f1a1a",
            activeforeground="#d1d5db",
            font=("Segoe UI", 8, "bold"),
        )
        self.pinned_alert_dismiss_btn.pack(side="right", padx=(2, 4), pady=3)

        self.logwrap.pack(fill="x", padx=(4, 10), pady=(0, 4))
        self.log = tk.Text(
            self.logwrap, bg=CARD, fg=MUTED, bd=0, padx=12, pady=6, wrap="word",
            font=("Consolas", 10), state="disabled", highlightthickness=0, height=4,
        )
        self.log.bind("<Double-1>", lambda _e: self.toggle_log_expand())
        self.log.tag_configure("hit", foreground=ACCENT, font=("Consolas", 10, "bold"))
        self.log.tag_configure("head", foreground=TEXT, font=("Consolas", 10, "bold"))
        self.log.tag_configure("dim", foreground=MUTED)
        self.log.tag_configure("err", foreground=ERR_COLOR, font=("Consolas", 10, "bold"))
        self.log.tag_configure("warn", foreground=WARN_COLOR, font=("Consolas", 10, "bold"))
        lsb = ttk.Scrollbar(self.logwrap, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        lsb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

        self.hit_frame = tk.Frame(right_col, bg=BG)
        self.hit_frame.pack(fill="both", expand=True, padx=(10, 4))
        self._build_hit_tab(self.hit_frame)
        self._set_buttons()

    def _build_hit_tab(self, parent: tk.Frame) -> None:
        hdr = tk.Frame(parent, bg=BG)
        hdr.pack(fill="x", pady=(4, 4))
        self.hit_title_lbl = tk.Label(
            hdr, text="Hit-List & Save-As Names (0 cases • 0 PDFs)",
            bg=BG, fg=ACCENT, font=("Segoe UI", 11, "bold"), anchor="w",
        )
        self.hit_title_lbl.pack(side="left")

        self.manual_check_btn = tk.Button(
            hdr,
            text="✓ All Clean (0 checks)",
            command=self.show_manual_check_modal,
            relief="flat",
            bd=0,
            padx=10,
            pady=2,
            cursor="hand2",
            bg=CARD,
            fg=MUTED,
            activebackground=LINE,
            activeforeground=TEXT,
            font=("Segoe UI", 9, "bold"),
        )
        self.manual_check_btn.pack(side="right")

        top = tk.Frame(parent, bg=BG)
        top.pack(fill="x", pady=(2, 6))

        self.scope_var = tk.StringVar(value=WHOLE_MONTH)
        self.scope_box = ttk.Combobox(
            top, textvariable=self.scope_var, state="readonly", width=22, height=18, values=[WHOLE_MONTH],
        )
        self.scope_box.pack(side="left", padx=(0, 10))
        self.scope_box.bind("<<ComboboxSelected>>", lambda _e: self._on_scope_change())

        tk.Label(top, text="Search:", bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.refresh_hits_table())
        tk.Entry(
            top, textvariable=self.search_var, bg=CARD, fg=TEXT, insertbackground=TEXT,
            relief="flat", width=22, font=("Segoe UI", 9),
        ).pack(side="left", padx=(6, 10), ipady=4)

        self.hide_done_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            top, text="Hide downloaded", variable=self.hide_done_var, command=self.refresh_hits_table,
            bg=BG, fg=MUTED, selectcolor=CARD, activebackground=BG, activeforeground=TEXT,
            highlightthickness=0, font=("Segoe UI", 9),
        ).pack(side="left")
        self.hit_stats_lbl = tk.Label(top, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9))
        self.hit_stats_lbl.pack(side="right")

        wrap = tk.Frame(parent, bg=BG)
        wrap.pack(fill="both", expand=True)
        cols = ("sel", "num", "done", "date", "case", "pdfs", "savenames")
        self.hit_tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="extended")
        for col, label, w, anchor, stretch in (
            ("sel", "☐", 36, "center", False),
            ("num", "#", 38, "center", False),
            ("done", "Done", 64, "center", False),
            ("date", "Doc Date", 140, "w", False),
            ("case", "Case #", 104, "w", False),
            ("pdfs", "PDFs", 84, "w", False),
            ("savenames", "Standard Save-As Filename(s)", 440, "w", True),
        ):
            if col == "sel":
                self.hit_tree.heading(col, text=label, anchor=anchor, command=self.toggle_check_all)
            else:
                self.hit_tree.heading(col, text=label, anchor=anchor)
            self.hit_tree.column(col, width=w, minwidth=300 if stretch else w, anchor=anchor, stretch=stretch)
        self.hit_tree.tag_configure("done", foreground=MUTED)
        self.hit_tree.tag_configure("todo", foreground=TEXT)
        self.hit_tree.tag_configure("checked", foreground=ACCENT)
        self.hit_tree.tag_configure("review", foreground="#f5b942")
        hsb = ttk.Scrollbar(wrap, orient="vertical", command=self.hit_tree.yview)
        hsb_x = ttk.Scrollbar(wrap, orient="horizontal", command=self.hit_tree.xview)
        self.hit_tree.configure(yscrollcommand=hsb.set, xscrollcommand=hsb_x.set)
        hsb.pack(side="right", fill="y")
        hsb_x.pack(side="bottom", fill="x")
        self.hit_tree.pack(side="left", fill="both", expand=True)
        self.hit_tree.bind("<Button-1>", self._on_hit_tree_click, add="+")
        self.hit_tree.bind("<<TreeviewSelect>>", lambda _e: self._on_hit_select())
        self.hit_tree.bind("<Double-1>", lambda _e: self.open_selected_hit_case())
        self.hit_tree.bind("<space>", lambda _e: self.toggle_selected_done())
        self.hit_tree.bind("<Control-a>", lambda _e: (self.hit_tree.selection_set(self.hit_tree.get_children()), "break")[1])
        self.hit_tree.bind("<Control-A>", lambda _e: (self.hit_tree.selection_set(self.hit_tree.get_children()), "break")[1])

        self.detail_box = tk.Frame(parent, bg=CARD, highlightbackground=LINE, highlightthickness=1, padx=12, pady=8)
        self.detail_box.pack(fill="x", pady=(6, 0))
        self.detail_title_lbl = tk.Label(
            self.detail_box, text="Click any Hit case above to view full details & one-click Save-As filename buttons",
            bg=CARD, fg=TEXT, font=("Segoe UI", 9, "bold"), anchor="w", justify="left", wraplength=740,
        )
        self.detail_title_lbl.pack(fill="x")
        def _on_main_detail_resize(event):
            w = max(260, event.width - 24)
            self.detail_title_lbl.configure(wraplength=w)
        self.detail_box.bind("<Configure>", _on_main_detail_resize)
        self.detail_pills = tk.Frame(self.detail_box, bg=CARD)
        self.detail_pills.pack(fill="x", pady=(6, 0))

        bar = tk.Frame(parent, bg=BG)
        bar.pack(fill="x", pady=(6, 0))
        self.dl_all_btn = self._btn(bar, "Download PDFs", self.download_checked_or_all_pdfs, primary=True)
        self.dl_all_btn.pack(side="left", padx=(0, 4))

        self._btn(bar, "Open Excel", self.open_excel_tracker).pack(side="left", padx=(0, 4))
        self._btn(bar, "Open Folder", self.open_downloads_folder).pack(side="left", padx=(0, 4))
        self._btn(bar, "Scan Downloads", self.audit_downloads_folder).pack(side="left", padx=(0, 4))
        self._btn(bar, "✓ Mark", self.toggle_selected_done).pack(side="left", padx=(0, 4))
        self._btn(bar, "Open Case", self.open_selected_hit_case).pack(side="right")
        self.toast_lbl = tk.Label(bar, text="", bg=BG, fg=ACCENT, font=("Segoe UI", 9, "bold"), anchor="w")
        self.toast_lbl.pack(side="left", fill="x", expand=True, padx=(6, 4))

    # ---------- helpers ----------
    def active_court_code(self) -> str:
        return self._court_map.get(self.court_var.get(), "KYCOA")

    def active_year_month(self) -> str:
        return self._month_map.get(self.month_var.get(), KY_MONTH_KEYS[0] if KY_MONTH_KEYS else "2026-11")

    def _intern_config_path(self) -> Path:
        return DATA_DIR / "intern_config.json"

    def _load_saved_intern_name(self) -> str:
        try:
            p = self._intern_config_path()
            if p.is_file():
                data = json.loads(p.read_text(encoding="utf-8"))
                val = str(data.get("intern_name") or "").strip()
                if val:
                    return val
        except Exception:
            pass
        return DEFAULT_KY_INTERN_NAME.strip()

    def _active_intern_name(self) -> str:
        if hasattr(self, "intern_var"):
            val = self.intern_var.get().strip()
            if val:
                return val
        if hasattr(self, "intern_box"):
            val = self.intern_box.get().strip()
            if val:
                return val
        return self._load_saved_intern_name()

    def _on_intern_change(self) -> None:
        name = self._active_intern_name()
        try:
            p = self._intern_config_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"intern_name": name}, indent=2), encoding="utf-8")
        except Exception:
            pass
        try:
            self._ensure_tracker_xlsx()
        except Exception:
            pass

    def _done_file(self):
        return ky_output_dir(self.active_court_code(), self.active_year_month()) / "downloaded_cases.json"

    def _load_done(self) -> None:
        p = self._done_file()
        try:
            self.done_cases = set(json.loads(p.read_text(encoding="utf-8"))) if p.exists() else set()
        except Exception:
            self.done_cases = set()

    def _save_done(self) -> None:
        try:
            self._done_file().write_text(json.dumps(sorted(self.done_cases)), encoding="utf-8")
        except Exception:
            pass

    def _set_buttons(self) -> None:
        if self.busy:
            action = getattr(self, "active_action", None)
            if action == "load":
                self.load_btn.configure(
                    text="⏳ Loading...", bg="#f59e0b", fg="#111111", disabledforeground="#111111",
                    font=("Segoe UI", 9, "bold"), state="disabled",
                )
                self.scan_btn.configure(
                    text="▶  Start Scan", bg=CARD, fg="#555555", disabledforeground="#555555",
                    font=("Segoe UI", 9, "normal"), state="disabled",
                )
                self.dl_all_btn.configure(state="disabled")
            elif action == "scan":
                self.scan_btn.configure(
                    text="⏳ Scanning...", bg="#f59e0b", fg="#111111", disabledforeground="#111111",
                    font=("Segoe UI", 9, "bold"), state="disabled",
                )
                self.load_btn.configure(
                    text="Load", bg=CARD, fg="#555555", disabledforeground="#555555",
                    font=("Segoe UI", 9, "normal"), state="disabled",
                )
                self.dl_all_btn.configure(state="disabled")
            elif action == "download":
                self.dl_all_btn.configure(
                    text="Downloading...", bg="#f59e0b", fg="#111111", disabledforeground="#111111",
                    font=("Segoe UI", 9, "bold"), state="disabled",
                )
                self.load_btn.configure(
                    text="Load", bg=CARD, fg="#555555", disabledforeground="#555555",
                    font=("Segoe UI", 9, "normal"), state="disabled",
                )
                self.scan_btn.configure(
                    text="▶  Start Scan", bg=CARD, fg="#555555", disabledforeground="#555555",
                    font=("Segoe UI", 9, "normal"), state="disabled",
                )
            else:
                self.load_btn.configure(state="disabled")
                self.scan_btn.configure(state="disabled")
                self.dl_all_btn.configure(state="disabled")

            self.stop_btn.configure(
                text="⏹  Stop", bg="#ef4444", fg="#ffffff", font=("Segoe UI", 9, "bold"), state="normal"
            )
            self.court_box.configure(state="disabled")
            self.month_box.configure(state="disabled")
            if hasattr(self, "scope_box"):
                self.scope_box.configure(state="disabled")
        else:
            self.active_action = None
            self.load_btn.configure(
                text="Load", bg=CARD, fg=TEXT, disabledforeground="#555555",
                font=("Segoe UI", 9, "normal"), state="normal",
            )
            self.scan_btn.configure(
                text="▶  Start Scan", bg=ACCENT, fg="#111111", disabledforeground="#555555",
                font=("Segoe UI", 9, "bold"), state="normal",
            )
            self.dl_all_btn.configure(
                text="Download All PDFs", bg=ACCENT, fg="#111111", disabledforeground="#555555",
                font=("Segoe UI", 9, "bold"), state="normal",
            )
            self.stop_btn.configure(
                text="Stop", bg=CARD, fg=TEXT, font=("Segoe UI", 9, "normal"), state="disabled",
            )
            self.court_box.configure(state="readonly")
            self.month_box.configure(state="readonly")
            if hasattr(self, "scope_box"):
                self.scope_box.configure(state="readonly")

    def _get_record(self, cn: str) -> KyCaseRecord | None:
        rec = self.records.get(cn)
        if rec is not None:
            return rec
        return self._all_scanned_records.get(cn)

    def _update_scope_box_values(self) -> None:
        if not hasattr(self, "scope_box"):
            return
        dates = set()
        for r in self.records.values():
            if r.status == "BRIEF_FOUND" and r.first_filed_date:
                dates.add(r.first_filed_date)

        def _date_sort_key(d: str) -> tuple[int, int, int]:
            try:
                parts = d.strip().replace("-", "/").split("/")
                if len(parts) == 3:
                    if len(parts[0]) == 4:
                        return int(parts[0]), int(parts[1]), int(parts[2])
                    return int(parts[2]), int(parts[0]), int(parts[1])
            except Exception:
                pass
            return (0, 0, 0)

        sorted_dates = sorted(dates, key=_date_sort_key, reverse=True)
        date_labels = [format_date_words(d) for d in sorted_dates]
        self._date_label_to_raw = dict(zip(date_labels, sorted_dates))

        # Matches NY exactly: Whole month + individual dates of this month
        values = [WHOLE_MONTH] + date_labels

        self.scope_box.configure(values=values)
        if self.scope_var.get() not in values:
            self.scope_var.set(WHOLE_MONTH)

    def _on_scope_change(self) -> None:
        self.refresh_hits_table()

    def _populate_top_tree_from_records(self, records: dict[str, KyCaseRecord]) -> None:
        self.tree.delete(*self.tree.get_children())
        self.case_order = list(records.keys())
        self.case_meta.clear()
        for idx, (cn, rec) in enumerate(records.items(), 1):
            self.case_meta[cn] = {
                "case_id": rec.case_id,
                "date": rec.first_filed_date,
                "subtypes": list(rec.search_sub_types),
                "num": idx,
            }
            status, _names, tag = self._format_case_row(rec)
            sub_str = "; ".join(rec.search_sub_types) if rec.search_sub_types else (rec.note or "")
            date_display = format_date_words(rec.first_filed_date)
            self.tree.insert("", "end", iid=cn, values=(str(idx), date_display, cn, sub_str, status), tags=(tag,))
        hits_cnt = sum(1 for r in records.values() if r.status == "BRIEF_FOUND")
        self.summary_lbl.configure(text=f"{len(self.case_order)} cases (cached)")
        self.prog_lbl.configure(text=f"Loaded {len(self.case_order)} cached cases • {hits_cnt} hit cases")

    def toggle_log_expand(self) -> None:
        self.log_expanded = not getattr(self, "log_expanded", False)
        if self.log_expanded:
            self.log.configure(height=14)
            self.log_expand_btn.configure(text="⤡ Collapse", fg=ACCENT)
        else:
            self.log.configure(height=4)
            self.log_expand_btn.configure(text="⤢ Expand", fg=TEXT)

    def _append_log(self, msg: str, tag: str = "dim") -> None:
        if "[ERROR]" in msg or tag == "err":
            tag = "err"
            self._record_error_auto(msg)
        elif "[WARN]" in msg or "⚠" in msg or tag == "warn":
            tag = "warn"
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.clear_pinned_errors()

    def _record_error(self, err_type: str, case_no: str, msg: str, details: str = "") -> None:
        for existing in self.pinned_errors:
            if existing.get("case") == case_no and existing.get("msg") == msg:
                return
        self.pinned_errors.append({
            "type": err_type,
            "case": case_no,
            "msg": msg,
            "details": details,
            "time": dt.datetime.now().strftime("%H:%M:%S"),
        })
        self._update_pinned_alert_bar()
        try:
            self.bell()
        except Exception:
            pass

    def _record_error_auto(self, log_msg: str) -> None:
        clean = log_msg.replace("[ERROR]", "").strip()
        case_m = re.search(r"\b\d{4}-CA-\d{4}\b|\b\d{4}-SC-\d{4}\b", clean)
        case_no = case_m.group(0) if case_m else ""
        self._record_error("Error", case_no, clean, clean)

    def _update_pinned_alert_bar(self) -> None:
        if not hasattr(self, "pinned_alert_frame"):
            return
        err_cnt = len(self.pinned_errors)
        if err_cnt > 0:
            last = self.pinned_errors[-1]
            c = last.get("case", "")
            m = last.get("msg", "")
            summary = f"{c}: {m}" if c and c not in m else m
            if len(summary) > 52:
                summary = summary[:49] + "..."
            self.pinned_alert_lbl.configure(text=f"❌ {err_cnt} Error(s) Logged • {summary}")
            if hasattr(self, "log_err_pill"):
                self.log_err_pill.configure(text=f"⚠ {err_cnt} Error(s)")
            if not self.pinned_alert_frame.winfo_ismapped() and hasattr(self, "logwrap"):
                self.pinned_alert_frame.pack(fill="x", padx=(4, 10), pady=(0, 4), before=self.logwrap)
        else:
            if hasattr(self, "log_err_pill"):
                self.log_err_pill.configure(text="")
            if self.pinned_alert_frame.winfo_ismapped():
                self.pinned_alert_frame.pack_forget()

    def clear_pinned_errors(self) -> None:
        self.pinned_errors.clear()
        self._update_pinned_alert_bar()
        self.refresh_hits_table()

    def get_manual_check_cases(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for cn, r in self.records.items():
            flags: list[str] = []
            guidance: list[str] = []
            severity = "warn"

            if has_ky_multi_volume(r.collectible_docs):
                vols = [ky_doc_volume_number(d.document_name) or ky_doc_volume_number(d.sub_type) for d in r.collectible_docs]
                vols_clean = [v for v in vols if v]
                vols_str = ", ".join(f"Vol {v}" for v in vols_clean) or f"{len(r.collectible_docs)} Volumes"
                flags.append(f"Multi-Volume ({vols_str})")
                guidance.append("Check if Vol 2+ are brief text continuations or attached depositions/exhibits. Per BPM rule, exclude if not continuation.")

            if r.review_note:
                flags.append("Review Note")
                guidance.append(f"Docket entry note: {r.review_note}. Check website.")
            elif r.status in ("NO_BRIEF", "RECEIPT_ONLY") and any("BRIEF" in s.upper() for s in r.search_sub_types):
                flags.append("No Collectible PDF")
                guidance.append("Docket indicates brief was filed, but no PDF was accessible via C-Track API. Check website (sealed/paper).")

            if any("PRO SE" in s.upper() for s in r.search_sub_types):
                flags.append("Pro Se")
                guidance.append("Filed by Pro Se party. Verify formatting and signatures on website.")

            matching_errors = [e for e in getattr(self, "pinned_errors", []) if e.get("case") == cn]
            if matching_errors:
                flags.append(f"{len(matching_errors)} Error(s)")
                guidance.append(f"Download/Scan error: {matching_errors[0].get('msg', '')}")
                severity = "err"

            if flags:
                results.append({
                    "case_number": cn,
                    "title": r.title,
                    "flags": " • ".join(flags),
                    "guidance": " | ".join(guidance),
                    "severity": severity,
                    "record": r,
                    "doc_count": len(r.collectible_docs),
                })
        return results

    def _toast(self, text: str) -> None:
        self.toast_lbl.configure(text=text)
        self.after(2200, lambda: self.toast_lbl.configure(text=""))

    def _on_filter_change(self) -> None:
        if not self.busy:
            court_code = self.active_court_code()
            ym = self.active_year_month()
            self.search_rows = []
            self.case_order.clear()
            self.case_meta.clear()
            self.records = {}
            self.checked_cases.clear()
            self.tree.delete(*self.tree.get_children())
            self._load_done()
            saved = load_ky_checkpoint(court_code, ym)
            if saved:
                self.records = saved
                self.search_rows = load_ky_search_rows(court_code, ym, saved)
                if self.search_rows:
                    self._populate_top_tree(self.search_rows)
                else:
                    self._populate_top_tree_from_records(saved)
                hits = [r for r in self.records.values() if r.status == "BRIEF_FOUND"]
                pdfs = sum(len(r.collectible_docs) for r in hits)
                dupes_cnt = max(0, len(self.search_rows) - len(self.case_order))
                dupe_txt = f" • {dupes_cnt} duplicates" if dupes_cnt > 0 else ""
                self.prog_lbl.configure(
                    text=f"Ready • {len(self.case_order)} Cases ({len(self.search_rows)} rows{dupe_txt}) • "
                    f"{len(hits)} Hits ({pdfs} PDFs)"
                )
                self._clear_log()
                self._append_log(
                    f"=== KENTUCKY C-TRACK ({court_code}) — {format_month_words(ym)} (Loaded: {len(hits)} Hits • {pdfs} PDFs) ===",
                    "head",
                )
            else:
                self.summary_lbl.configure(text="")
                self.prog_lbl.configure(text="Ready to scan or load")
                self._clear_log()
                if "unittest" not in sys.modules and not getattr(self.app, "remote_blocked", False):
                    self.after(150, self.load_month)
            self._update_scope_box_values()
            self.refresh_hits_table()

    def on_ky_data_cleared(self) -> None:
        self.search_rows = []
        self.case_order.clear()
        self.case_meta.clear()
        self.records.clear()
        self.done_cases.clear()
        self.checked_cases.clear()
        self._save_done()
        self.tree.delete(*self.tree.get_children())
        self.hit_tree.delete(*self.hit_tree.get_children())
        self.detail_title_lbl.configure(
            text="Click any Hit case above to view full details & one-click Save-As filename buttons"
        )
        for child in self.detail_pills.winfo_children():
            child.destroy()
        self.hit_stats_lbl.configure(text="")
        self.summary_lbl.configure(text="")
        self.prog_lbl.configure(text="Reset complete: clean slate")
        self.bar["value"] = 0
        self._clear_log()
        self._set_buttons()
        self._toast("Cleared successfully! Reset to fresh state.")

    # ---------- actions ----------
    def load_month(self) -> None:
        if self.busy or getattr(self.app, "remote_blocked", False):
            return
        self._start_worker(scan_cases=False)

    def start_scan(self) -> None:
        if self.busy:
            return
        if getattr(self.app, "remote_blocked", False):
            messagebox.showerror("Access Blocked", getattr(self.app, "remote_blocked_msg", "This application is currently suspended."))
            return
        self._start_worker(scan_cases=True)

    def stop_scan(self) -> None:
        STOP_EVENT.set()
        self.prog_lbl.configure(text="Stopping after current case...")
        action = getattr(self, "active_action", None)
        if action == "scan":
            self.scan_btn.configure(text="⏹  Stopping...", bg="#eab308", fg="#111111")
        elif action == "load":
            self.load_btn.configure(text="⏹  Stopping...", bg="#eab308", fg="#111111")
        elif action == "download":
            self.dl_all_btn.configure(text="⏹  Stopping...", bg="#eab308", fg="#111111")
        self.stop_btn.configure(text="Stopping...", state="disabled")

    def _start_worker(self, scan_cases: bool) -> None:
        STOP_EVENT.clear()
        self.busy = True
        self.active_action = "scan" if scan_cases else "load"
        self._set_buttons()
        self._clear_log()
        court_code = self.active_court_code()
        ym = self.active_year_month()
        fresh = bool(self.fresh_var.get()) and scan_cases
        threading.Thread(
            target=self._worker_run, args=(court_code, ym, scan_cases, fresh), daemon=True
        ).start()

    def _worker_run(self, court_code: str, ym: str, scan_cases: bool, fresh: bool) -> None:
        try:
            self.q.put(("log", f"=== KENTUCKY C-TRACK ({court_code}) — {format_month_words(ym)} ===", "head"))
            if not fresh and getattr(self, "search_rows", None):
                rows = self.search_rows
            else:
                self.q.put(("prog", 0, 1, "Fetching Document Search rows from C-Track..."))
                rows = fetch_ky_month_search_rows(court_code, ym)
                self.q.put(("loaded_rows", rows, court_code, ym, fresh))
            if not scan_cases or STOP_EVENT.is_set():
                self.q.put(("done", False))
                return

            unique_cases: list[tuple[str, str, str, str, list[str]]] = []
            seen: dict[str, list[str]] = {}
            first_meta: dict[str, tuple[str, str, str]] = {}
            for r in rows:
                if not r.case_number or not r.case_id:
                    continue
                if r.case_number not in seen:
                    seen[r.case_number] = []
                    first_meta[r.case_number] = (r.case_id, r.court, r.filed_date_display)
                label = r.sub_type or r.document_name or r.doc_type
                if label and label not in seen[r.case_number]:
                    seen[r.case_number].append(label)

            for cn, subs in seen.items():
                cid, crt, fdate = first_meta[cn]
                unique_cases.append((cn, cid, crt, fdate, subs))

            saved = {} if fresh else load_ky_checkpoint(court_code, ym)
            records: dict[str, KyCaseRecord] = dict(saved)
            total = len(unique_cases)
            self.q.put(("log", f"Found {len(rows)} Document Search row(s) across {total} unique case(s).", "head"))
            self.q.put(("prog", 0, total, f"Starting scan of {total} case(s)..."))

            skipped_errors: list[str] = []
            processed_count = 0

            # Phase 1: Fast cache evaluation for already scanned cases
            to_scan: list[tuple[int, str, str, str, str, list[str]]] = []
            for idx, (cn, cid, crt, fdate, subs) in enumerate(unique_cases, start=1):
                cached = records.get(cn)
                if (
                    cached
                    and cached.first_filed_date == fdate
                    and cached.search_sub_types == subs
                    and (
                        cached.status == "BRIEF_FOUND"
                        or (cached.status in ("NO_BRIEF", "RECEIPT_ONLY") and cached.receipt_notice_count > 0)
                    )
                ):
                    processed_count += 1
                    self.q.put(("prog", processed_count, total, f"[{processed_count}/{total}] Already checked: {cn} (cached)"))
                    self.q.put(("case_done", processed_count, total, cached))
                else:
                    to_scan.append((idx, cn, cid, crt, fdate, subs))

            # Phase 2: Parallel high-speed scanning for uncached cases (3 concurrent workers)
            if to_scan and not STOP_EVENT.is_set():
                scan_q: queue.Queue = queue.Queue()
                for item in to_scan:
                    scan_q.put(item)

                scan_lock = threading.Lock()
                num_workers = min(3, len(to_scan))

                def _scan_thread() -> None:
                    nonlocal processed_count
                    while not STOP_EVENT.is_set():
                        try:
                            item = scan_q.get_nowait()
                        except queue.Empty:
                            break

                        idx, cn, cid, crt, fdate, subs = item
                        self.q.put(("prog", processed_count, total, f"[{processed_count}/{total}] Scanning C-Track: {cn}..."))

                        try:
                            rec = scan_ky_case(cid, cn, crt, fdate, subs, year_month=ym)
                            with scan_lock:
                                records[cn] = rec
                                processed_count += 1
                                curr_done = processed_count
                                if curr_done % 5 == 0:
                                    try:
                                        save_ky_checkpoint(court_code, ym, records, rows)
                                    except Exception:
                                        pass
                            self.q.put(("case_done", curr_done, total, rec))
                        except InterruptedError:
                            self.q.put(("log", f"Scan stopped by user at case {cn}.", "warn"))
                            scan_q.task_done()
                            break
                        except Exception as case_exc:
                            with scan_lock:
                                skipped_errors.append(cn)
                                processed_count += 1
                                curr_done = processed_count
                            self.q.put((
                                "log",
                                f"[ERROR] {cn}: could not be read ({case_exc}) — SKIPPED. Check it manually or retry.",
                                "err",
                            ))
                            self.q.put(("err_record", {
                                "type": "Scan Error",
                                "case": cn,
                                "msg": f"Case could not be read: {case_exc}",
                                "details": str(case_exc),
                            }))
                        finally:
                            scan_q.task_done()

                threads = [threading.Thread(target=_scan_thread, daemon=True) for _ in range(num_workers)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

            save_ky_checkpoint(court_code, ym, records, rows)
            if skipped_errors:
                self.q.put((
                    "log",
                    f"⚠ {len(skipped_errors)} case(s) could NOT be scanned: {', '.join(skipped_errors)}",
                    "warn",
                ))
            self.q.put(("summary", len(rows), total, records))
        except Exception as exc:
            self.q.put(("log", f"[ERROR] Kentucky C-Track scan failed: {exc}", "err"))
            self.q.put(("err_record", {
                "type": "Scan Error",
                "case": "",
                "msg": f"C-Track scan failed: {exc}",
                "details": str(exc),
            }))
        finally:
            self.q.put(("done", scan_cases))

    def _poll(self) -> None:
        if not self.winfo_exists():
            return
        try:
            while True:
                ev = self.q.get_nowait()
                kind = ev[0]
                if kind == "log":
                    self._append_log(ev[1], ev[2])
                elif kind == "prog":
                    done, total, text = ev[1], ev[2], ev[3]
                    self.bar["value"] = round((done / max(total, 1)) * 100)
                    self.prog_lbl.configure(text=text)
                    if getattr(self, "active_action", None) == "download":
                        self.dl_all_btn.configure(
                            text=f"Downloading PDFs ({done}/{total})...",
                            bg="#f59e0b",
                            fg="#111111",
                            disabledforeground="#111111",
                            state="disabled",
                        )
                elif kind == "loaded_rows":
                    rows, court_code, ym, fresh = ev[1], ev[2], ev[3], ev[4]
                    self.search_rows = rows
                    self.records = {} if fresh else load_ky_checkpoint(court_code, ym)
                    self._load_done()
                    self._populate_top_tree(rows)
                    self._update_scope_box_values()
                    self.refresh_hits_table()
                elif kind == "case_done":
                    idx, total, rec = ev[1], ev[2], ev[3]
                    self.records[rec.case_number] = rec
                    self._update_top_row(rec)
                    self.bar["value"] = round((idx / max(total, 1)) * 100)
                    self.prog_lbl.configure(text=f"Checked {idx}/{total} cases • {rec.case_number}")
                    tag = "hit" if rec.status == "BRIEF_FOUND" else "dim"
                    badge = "HIT  " if rec.status == "BRIEF_FOUND" else "SKIP "
                    if rec.review_note:
                        tag, badge = "head", "CHECK"
                    title_col = " ".join((rec.title or "Case").split())[:38]
                    self._append_log(
                        f"[{idx:02d}/{total:02d}] [{badge}] {rec.case_number:<12} | {title_col:<38} | {rec.note}",
                        tag,
                    )
                    now = time.monotonic()
                    if rec.status == "BRIEF_FOUND" and (now - getattr(self, "_last_hit_refresh_time", 0.0)) >= 1.0:
                        self._last_hit_refresh_time = now
                        self.refresh_hits_table()
                elif kind == "summary":
                    row_cnt, case_cnt, records = ev[1], ev[2], ev[3]
                    hits = [r for r in records.values() if r.status == "BRIEF_FOUND"]
                    if hasattr(self, "case_order") and self.case_order:
                        order_map = {cn: i for i, cn in enumerate(self.case_order)}
                        hits.sort(key=lambda r: order_map.get(r.case_number, 999999))
                    pdfs = sum(len(r.collectible_docs) for r in hits)
                    self._append_log("-" * 92, "dim")
                    self._append_log(
                        f"KENTUCKY SCAN SUMMARY: {row_cnt} search rows • {case_cnt} unique cases • "
                        f"{len(hits)} Hit Cases • {pdfs} Collectible Brief PDFs",
                        "head",
                    )
                    p_idx = 0
                    for r in hits:
                        for d in r.collectible_docs:
                            p_idx += 1
                            self._append_log(
                                f"  [PDF {p_idx:02d}/{pdfs:02d}] {r.case_number} -> {d.save_as_name}  ({d.document_name})",
                                "hit",
                            )
                    review_cases = [r for r in records.values() if r.review_note]
                    if review_cases:
                        self._append_log(
                            f"⚠ {len(review_cases)} case(s) need a MANUAL CHECK (Brief entry with no collectible PDF):",
                            "warn",
                        )
                        for r in review_cases:
                            self._append_log(f"  {r.case_number}: {r.review_note}", "warn")
                    self._append_log("-" * 92, "dim")
                    self._update_scope_box_values()
                    self.refresh_hits_table()
                elif kind == "dl_case_done":
                    cn = ev[1]
                    self.done_cases.add(cn)
                    self.checked_cases.discard(cn)
                    self._save_done()
                    self.refresh_hits_table()
                elif kind == "download_tracker_changed":
                    tracker_path = self._ensure_tracker_xlsx()
                    if tracker_path:
                        self._toast(f"Daily downloads saved in {tracker_path.name}")
                elif kind == "err_record":
                    err_info = ev[1]
                    self._record_error(
                        err_info.get("type", "Error"),
                        err_info.get("case", ""),
                        err_info.get("msg", ""),
                        err_info.get("details", "")
                    )
                elif kind == "dl_complete_report":
                    report = ev[1]
                    self._last_dl_report = report
                    manual_cases = self.get_manual_check_cases()
                    failed_cnt = len(report.get("failed_details", []))
                    if failed_cnt > 0 or len(manual_cases) > 0:
                        self.show_manual_check_modal(report)
                elif kind == "dl_toast":
                    self._toast(ev[1])
                elif kind == "done":
                    self.busy = False
                    self.active_action = None
                    self._set_buttons()
                    self._on_hit_select()
                    hits = [r for r in self.records.values() if r.status == "BRIEF_FOUND"]
                    pdfs = sum(len(r.collectible_docs) for r in hits)
                    dupes_cnt = max(0, len(self.search_rows) - len(self.case_order))
                    dupe_txt = f" • {dupes_cnt} duplicates" if dupes_cnt > 0 else ""
                    self.prog_lbl.configure(
                        text=f"Ready • {len(self.case_order)} Cases ({len(self.search_rows)} rows{dupe_txt}) • "
                        f"{len(hits)} Hits ({pdfs} PDFs)"
                    )
        except queue.Empty:
            pass
        except Exception:
            traceback.print_exc()
        finally:
            if self.winfo_exists():
                self._after_id = self.after(120, self._poll)

    def _populate_top_tree(self, rows: list[KySearchRow]) -> None:
        self.tree.delete(*self.tree.get_children())
        self.case_order.clear()
        self.case_meta.clear()
        for r in rows:
            if not r.case_number:
                continue
            if r.case_number not in self.case_meta:
                self.case_order.append(r.case_number)
                self.case_meta[r.case_number] = {
                    "case_id": r.case_id,
                    "date": r.filed_date_display or r.filed_date_iso,
                    "subtypes": [],
                }
            st = r.sub_type or r.document_name or r.doc_type
            if st and st not in self.case_meta[r.case_number]["subtypes"]:
                self.case_meta[r.case_number]["subtypes"].append(st)

        for idx, cn in enumerate(self.case_order, 1):
            meta = self.case_meta[cn]
            meta["num"] = idx
            sub_str = "; ".join(meta["subtypes"])
            rec = self.records.get(cn)
            status, _pdf_str, tag = self._format_case_row(rec)
            date_display = format_date_words(meta["date"])
            self.tree.insert("", "end", iid=cn, values=(str(idx), date_display, cn, sub_str, status), tags=(tag,))

        dupes_cnt = max(0, len(rows) - len(self.case_order))
        dupe_txt = f" • {dupes_cnt} duplicates" if dupes_cnt > 0 else ""
        self.summary_lbl.configure(
            text=f"{len(self.case_order)} Cases ({len(rows)} rows{dupe_txt})"
        )

    def _format_case_row(self, rec: KyCaseRecord | None) -> tuple[str, str, str]:
        if not rec:
            return "Not scanned", "", "pending"
        if rec.status == "BRIEF_FOUND":
            cnt = len(rec.collectible_docs)
            names = ", ".join(d.save_as_name for d in rec.collectible_docs)
            if has_ky_multi_volume(rec.collectible_docs):
                return f"HIT ({cnt} PDFs) ⚠ Multi-Vol", names, "review"
            if rec.review_note:
                return f"HIT ({cnt} PDF{'s' if cnt != 1 else ''}) ⚠", names, "review"
            return f"HIT ({cnt} PDF{'s' if cnt != 1 else ''})", names, "hit"
        if rec.review_note:
            segs = [s.strip().lower() for s in rec.review_note.split("; ") if s.strip()]
            only_receipt = segs and all("files: receipt notice only" in seg for seg in segs)
            only_cover = segs and all("cover page" in seg for seg in segs)
            if only_cover:
                return "Cover Page only", rec.review_note, "skip"
            if only_receipt:
                return "Receipt Notice only", rec.review_note, "skip"
            return "⚠ Check manually", rec.review_note, "review"
        if rec.status == "RECEIPT_ONLY":
            return "Receipt Notice only", "0 (Receipt Notice only)", "skip"
        return "No collectible brief", "0", "skip"

    def _update_top_row(self, rec: KyCaseRecord) -> None:
        if not self.tree.exists(rec.case_number):
            return
        meta = self.case_meta.get(rec.case_number, {})
        status, _pdf_str, tag = self._format_case_row(rec)
        num_str = str(meta.get("num", ""))
        date_display = format_date_words(meta.get("date", rec.first_filed_date))
        self.tree.item(
            rec.case_number,
            values=(num_str, date_display, rec.case_number, "; ".join(meta.get("subtypes", [])), status),
            tags=(tag,),
        )

    def refresh_hits_table(self) -> None:
        self.hit_tree.delete(*self.hit_tree.get_children())
        scope = self.scope_var.get().strip() if hasattr(self, "scope_var") else WHOLE_MONTH

        raw_hits = [r for r in self.records.values() if r.status == "BRIEF_FOUND"]
        if hasattr(self, "case_order") and self.case_order:
            order_map = {cn: i for i, cn in enumerate(self.case_order)}
            raw_hits.sort(key=lambda r: order_map.get(r.case_number, 999999))

        # Scope filter (by specific date in current month, same as NY)
        if scope and scope != WHOLE_MONTH:
            raw_target_date = getattr(self, "_date_label_to_raw", {}).get(scope, scope)
            hits = [
                r for r in raw_hits
                if r.first_filed_date in (scope, raw_target_date)
                or format_date_words(r.first_filed_date) == scope
            ]
        else:
            hits = raw_hits

        total_pdfs = sum(len(r.collectible_docs) for r in hits)
        scope_suffix = f" • {scope}" if scope and scope != WHOLE_MONTH else ""
        self.hit_title_lbl.configure(
            text=f"Hit-List & Save-As Names ({len(hits)} cases • {total_pdfs} PDFs{scope_suffix})"
        )

        q = self.search_var.get().strip().lower()
        hide_done = bool(self.hide_done_var.get())
        done_cnt = 0
        done_pdfs = 0
        shown_Ids: list[str] = []

        for r in hits:
            is_done = r.case_number in self.done_cases
            if not is_done and r.collectible_docs and all(
                self._find_ky_downloaded_pdf(r.case_number, d) is not None for d in r.collectible_docs
            ):
                is_done = True
                self.done_cases.add(r.case_number)
            if is_done:
                done_cnt += 1
                done_pdfs += len(r.collectible_docs)
            if hide_done and is_done:
                continue
            save_str = "  |  ".join(d.save_as_name for d in r.collectible_docs)
            date_words = format_date_words(r.first_filed_date)
            hay = f"{r.case_number} {r.title} {save_str} {date_words}".lower()
            if q and q not in hay:
                continue
            cnt = len(r.collectible_docs)
            is_checked = r.case_number in self.checked_cases
            is_multi = has_ky_multi_volume(r.collectible_docs)
            row_tag = "checked" if is_checked else ("done" if is_done else ("review" if is_multi else "todo"))
            pdf_label = f"{cnt} PDFs ⚠ Vol" if is_multi else f"{cnt} PDF{'s' if cnt != 1 else ''}"
            row_num = len(shown_Ids) + 1
            self.hit_tree.insert(
                "", "end", iid=r.case_number,
                values=(
                    "☑" if is_checked else "☐",
                    str(row_num),
                    "✓ Done" if is_done else "",
                    date_words,
                    r.case_number,
                    pdf_label,
                    save_str,
                ),
                tags=(row_tag,),
            )
            shown_Ids.append(r.case_number)

        all_checked = bool(shown_Ids) and all(cid in self.checked_cases for cid in shown_Ids)
        self.hit_tree.heading("sel", text="☑" if all_checked else "☐")
        if hasattr(self, "select_all_btn"):
            self.select_all_btn.configure(text="☐ Unselect All" if all_checked else "☑ Select All")
        checked_cnt = len(self.checked_cases)
        if checked_cnt > 0:
            self.dl_all_btn.configure(text=f"Download Selected ({checked_cnt})")
        else:
            self.dl_all_btn.configure(text="Download All PDFs")
        self.hit_stats_lbl.configure(
            text=f"Selected: {checked_cnt}  •  Done: {done_cnt}/{len(hits)} cases ({done_pdfs}/{total_pdfs} PDFs)"
        )
        if hasattr(self, "manual_check_btn"):
            manual_items = self.get_manual_check_cases()
            if manual_items:
                self.manual_check_btn.configure(
                    text=f"⚠ Manual Check Needed ({len(manual_items)})",
                    bg="#3b2d10",
                    fg=WARN_COLOR,
                    activebackground="#4d3b14",
                    activeforeground="#fcd34d",
                )
            else:
                self.manual_check_btn.configure(
                    text="✓ All Clean (0 checks)",
                    bg=CARD,
                    fg=MUTED,
                    activebackground=LINE,
                    activeforeground=TEXT,
                )
        if not self.hit_tree.selection() and shown_Ids:
            self.hit_tree.selection_set(shown_Ids[0])
        self._on_hit_select()

    @staticmethod
    def _fmt_us_date(iso_or_us: str) -> str:
        return format_date_words(iso_or_us)

    def _copy_one_name(self, name: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(name)
        self._toast(f"Copied: {name}")

    def _copy_and_open_pdf(self, name: str, url: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(name)
        if url:
            webbrowser.open(url)
        self._toast(f"Copied '{name}' & opened PDF in browser!")

    def _on_hit_select(self) -> None:
        for w in self.detail_pills.winfo_children():
            w.destroy()
        sel = self.hit_tree.selection()
        if not sel:
            self.detail_title_lbl.configure(
                text="Click any Hit case above to view Website Row Guide & one-click Save-As buttons"
            )
            return
        cn = sel[0]
        rec = self._get_record(cn)
        if not rec:
            return
        clean_title = " ".join((rec.title or "").split())
        is_multi = has_ky_multi_volume(rec.collectible_docs)
        multi_badge = "  —  ⚠ MULTI-VOLUME BRIEF" if is_multi else ""
        self.detail_title_lbl.configure(
            text=f"{cn}  •  {clean_title}   ({len(rec.collectible_docs)} Collectible PDF{'s' if len(rec.collectible_docs) != 1 else ''}{multi_badge} — Website Row Guide & Save-As Names)"
        )
        if is_multi:
            warn_banner = tk.Frame(self.detail_pills, bg="#2a2415", highlightbackground="#f5b942", highlightthickness=1, padx=10, pady=5)
            warn_banner.pack(fill="x", pady=(2, 6))
            tk.Label(
                warn_banner,
                text="⚠ MULTI-VOLUME BRIEF: Check if Vol 2+ are brief continuations or attachments (depositions/records/exhibits).\n"
                     "   Ma'am Stephanie Directive: If Vol 2+ is NOT a brief continuation, do not collect. Click [✖ Exclude] on that volume.",
                bg="#2a2415", fg="#f5b942", font=("Segoe UI", 9, "bold"),
                anchor="w", justify="left", wraplength=720,
            ).pack(fill="x")

        # Show newest-first to match C-Track's Docket Entries table top-to-bottom order
        docs_top_down = sorted(rec.collectible_docs, key=lambda x: x.filed_date or "", reverse=True)
        for idx, d in enumerate(docs_top_down, 1):
            row_f = tk.Frame(self.detail_pills, bg=CARD)
            row_f.pack(fill="x", pady=2)
            is_dl = bool(self._find_ky_downloaded_pdf(cn, d))
            tk.Button(
                row_f,
                text="✔" if is_dl else "⬇",
                command=lambda doc_obj=d, case_no=cn: (
                    self._confirm_auto_save_one_pdf(case_no, doc_obj)
                ),
                relief="flat", bd=0, width=3, padx=4, pady=1, cursor="hand2",
                bg=LINE if is_dl else ACCENT, fg=ACCENT if is_dl else "#111111",
                activebackground=LINE, activeforeground=TEXT,
                font=("Segoe UI", 9, "bold"),
            ).pack(side="left", padx=(0, 8))
            us_date = self._fmt_us_date(d.filed_date)
            sub_txt = f"  •  Submitted By: {d.submitter}" if d.submitter else ""
            vol_num = ky_doc_volume_number(d.document_name) or ky_doc_volume_number(d.sub_type)
            vol_badge = f" [Vol {vol_num}]" if vol_num else ""
            web_info = f"◄  [{idx}] Website: {us_date}  •  {d.document_name}{vol_badge}{sub_txt}"
            is_vol_warn = bool(vol_num and vol_num != "1" and vol_num != "I")
            tk.Label(
                row_f, text=web_info, bg=CARD,
                fg="#f5b942" if is_vol_warn else TEXT,
                font=("Segoe UI", 9, "bold" if is_vol_warn else "normal"),
                anchor="w", justify="left", wraplength=560,
            ).pack(side="left", fill="x", expand=True)

            tk.Button(
                row_f, text="👁 Open",
                command=lambda doc_obj=d, case_no=cn: self._on_open_ky_pdf(case_no, doc_obj),
                relief="flat", bd=0, padx=6, pady=1, bg=LINE, fg=TEXT,
                font=("Segoe UI", 8), cursor="hand2",
            ).pack(side="right", padx=(4, 2))

            tk.Button(
                row_f, text="📋 Copy",
                command=lambda name=d.save_as_name: self._copy_one_name(name),
                relief="flat", bd=0, padx=6, pady=1, bg=LINE, fg=MUTED,
                activebackground=BG, activeforeground=TEXT,
                font=("Segoe UI", 8), cursor="hand2",
            ).pack(side="right", padx=(4, 2))

            if len(rec.collectible_docs) > 1 or is_multi:
                tk.Button(
                    row_f, text="✖ Exclude",
                    command=lambda doc_obj=d, case_no=cn: self._exclude_ky_doc(case_no, doc_obj),
                    relief="flat", bd=0, padx=6, pady=1, bg=LINE, fg="#ff6b6b",
                    activebackground=BG, activeforeground="#ff4444",
                    font=("Segoe UI", 8, "bold"), cursor="hand2",
                ).pack(side="right", padx=(4, 2))

    def _exclude_ky_doc(self, case_number: str, doc_obj: KyCollectibleDoc) -> None:
        rec = self._get_record(case_number)
        if not rec:
            return
        orig_count = len(rec.collectible_docs)
        rec.collectible_docs = [
            d for d in rec.collectible_docs
            if d.document_id != doc_obj.document_id and d.document_name != doc_obj.document_name
        ]
        if len(rec.collectible_docs) == orig_count:
            rec.collectible_docs = [d for d in rec.collectible_docs if d is not doc_obj]

        if rec.collectible_docs:
            assign_ky_save_as_names(rec.court, rec.case_number, rec.collectible_docs)
            names_preview = ", ".join(d.save_as_name for d in rec.collectible_docs)
            rec.note = f"{len(rec.collectible_docs)} collectible PDF(s): {names_preview}"
            if not has_ky_multi_volume(rec.collectible_docs):
                notes = [s for s in rec.review_note.split("; ") if "multi-volume" not in s.lower()]
                rec.review_note = "; ".join(notes)
        else:
            rec.status = "NO_BRIEF"
            rec.note = "Excluded attached document(s) manually — no collectible brief remaining"

        save_ky_checkpoint(self.active_court_code(), self.active_year_month(), self.records)
        self._update_top_row(rec)
        self.refresh_hits_table()
        if self.hit_tree.exists(case_number):
            self.hit_tree.selection_set(case_number)
            self._on_hit_select()
        self._toast(f"Excluded '{doc_obj.document_name}' from {case_number}")

    # ---------- hit-list actions ----------
    def _on_hit_tree_click(self, event) -> None:
        region = self.hit_tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self.hit_tree.identify_column(event.x)
        row_id = self.hit_tree.identify_row(event.y)
        if col == "#1" and row_id:
            if row_id in self.checked_cases:
                self.checked_cases.discard(row_id)
            else:
                self.checked_cases.add(row_id)
            self.refresh_hits_table()
            if self.hit_tree.exists(row_id):
                self.hit_tree.selection_set(row_id)

    def toggle_check_all(self) -> None:
        visible = list(self.hit_tree.get_children())
        if not visible:
            return
        all_checked = all(cn in self.checked_cases for cn in visible)
        for cn in visible:
            if all_checked:
                self.checked_cases.discard(cn)
            else:
                self.checked_cases.add(cn)
        self.refresh_hits_table()

    def toggle_selected_done(self) -> None:
        targets = list(self.checked_cases) if self.checked_cases else list(self.hit_tree.selection())
        if not targets:
            return
        all_done = all(cn in self.done_cases for cn in targets)
        for cn in targets:
            if all_done:
                self.done_cases.discard(cn)
            else:
                self.done_cases.add(cn)
        self.checked_cases.clear()
        self._save_done()
        self.refresh_hits_table()

    def toggle_all_done(self) -> None:
        visible = list(self.hit_tree.get_children())
        if not visible:
            return
        all_done = all(cn in self.done_cases for cn in visible)
        for cn in visible:
            if all_done:
                self.done_cases.discard(cn)
            else:
                self.done_cases.add(cn)
        self._save_done()
        self.refresh_hits_table()

    def copy_selected_case_num(self) -> None:
        sel = self.hit_tree.selection() or self.tree.selection()
        if not sel:
            return
        self.clipboard_clear()
        self.clipboard_append("\n".join(sel))
        self._toast(f"Copied {len(sel)} Case #")

    def copy_selected_save_names(self) -> None:
        sel = self.hit_tree.selection() or self.tree.selection()
        if not sel:
            self._toast("Select a hit case first")
            return
        names: list[str] = []
        for cn in sel:
            rec = self._get_record(cn)
            if rec and rec.collectible_docs:
                names.extend(d.save_as_name for d in rec.collectible_docs)
        if not names:
            self._toast("No Save-As names on selected row")
            return
        self.clipboard_clear()
        self.clipboard_append("\n".join(names))
        self._toast(f"Copied {len(names)} Save-As filename(s)")

    def _tracker_sheet_name(self) -> str:
        ym = self.active_year_month()
        month_word = format_month_words(ym).split()[0] if ym else "OCTOBER"
        return f"KY - COA TRACKER {month_word}"

    def _tracker_xlsx_file(self) -> Path:
        ym = self.active_year_month()
        month_word = format_month_words(ym).split()[0] if ym else "OCTOBER"
        # Save directly in the month downloads folder (e.g. KYCOA_JANUARY_2025)
        dl_dir = ky_downloads_dir(self.active_court_code(), ym)
        return dl_dir / f"KY - COA TRACKER {month_word}.xlsx"

    def _current_tracker_rows(self) -> list[dict[str, str]]:
        court_code = self.active_court_code()
        ym = self.active_year_month()
        records = self.records or load_ky_checkpoint(court_code, ym)
        history_file = ky_download_history_path(court_code, ym)
        seen_filenames = set()
        if history_file.exists():
            with history_file.open("r", encoding="utf-8") as hf:
                for h_line in hf:
                    try:
                        ev = json.loads(h_line)
                        fn = str(ev.get("saved_filename") or "").strip().lower()
                        if fn:
                            seen_filenames.add(fn)
                    except Exception:
                        pass
        month_dir = ky_downloads_dir(court_code, ym).resolve()
        disk_index = self._get_ky_pdf_disk_index(force_refresh=True)

        # Import PDFs saved before download events were recorded. Their file
        # modification timestamps preserve the date for this existing batch.
        for case_number, rec in records.items():
            for doc in rec.collectible_docs:
                filename = f"{doc.save_as_name}.pdf".lower()
                if filename in seen_filenames:
                    continue
                saved_path = disk_index.get(filename)
                if not KyScannerFrame._is_valid_pdf_path(saved_path):
                    continue
                try:
                    saved_path.resolve().relative_to(month_dir)
                    downloaded_at = dt.datetime.fromtimestamp(saved_path.stat().st_mtime)
                except (OSError, ValueError):
                    continue
                record_ky_download_history(
                    court_code,
                    ym,
                    doc,
                    saved_path,
                    case_number,
                    rec.case_id,
                    downloaded_at=downloaded_at,
                )
                seen_filenames.add(filename)

        downloaded_rows = load_ky_download_tracker_rows(
            court_code, ym, intern_name=self._active_intern_name(),
            search_rows=self.search_rows, records=records,
        )
        # Strictly return only actual downloaded documents. If 10 cases were downloaded, only those 10 appear.
        # When user downloads 30 more tomorrow, they append cleanly with tomorrow's collection date.
        return downloaded_rows

    def _ensure_tracker_xlsx(self) -> Path | None:
        t_rows = self._current_tracker_rows()
        if not t_rows:
            return None
        xlsx_path = self._tracker_xlsx_file()
        sheet_tab = self._tracker_sheet_name()
        ym = self.active_year_month()
        month_word = format_month_words(ym).split()[0] if ym else "OCTOBER"

        # Strictly clean up any lingering duplicate/alt files from prior runs so folder always has only ONE master Excel
        try:
            if xlsx_path.parent.exists():
                for old_dup in xlsx_path.parent.glob(f"{xlsx_path.stem}*.xlsx"):
                    if old_dup.name != xlsx_path.name:
                        try:
                            old_dup.unlink()
                        except Exception:
                            pass
        except Exception:
            pass

        try:
            xlsx_path.parent.mkdir(parents=True, exist_ok=True)
            write_ky_tracker_xlsx(xlsx_path, t_rows, sheet_tab_name=sheet_tab)

            # Also keep a silent mirror copy in output directory for internal backup
            try:
                out_backup = ky_output_dir(self.active_court_code(), ym) / f"KY - COA TRACKER {month_word}.xlsx"
                write_ky_tracker_xlsx(out_backup, t_rows, sheet_tab_name=sheet_tab)
            except Exception:
                pass
            return xlsx_path
        except PermissionError:
            # File is open in Microsoft Excel. Never create duplicate files (_latest, _1, etc.)!
            self._toast("⚠️ Close Excel to update!")
            return xlsx_path
        except Exception:
            if xlsx_path.exists():
                return xlsx_path
            return None

    def open_excel_tracker(self) -> None:
        xlsx_file = self._tracker_xlsx_file()
        if xlsx_file.exists():
            try:
                self._ensure_tracker_xlsx()
            except Exception:
                pass
            try:
                os.startfile(str(xlsx_file))  # type: ignore[attr-defined]
                self._toast(f"Opened {xlsx_file.name}")
                return
            except Exception as exc:
                self._toast(f"Open error: {exc}")
                return

        p = None
        try:
            p = self._ensure_tracker_xlsx()
        except Exception:
            pass

        if p and p.exists():
            try:
                os.startfile(str(p))  # type: ignore[attr-defined]
                self._toast(f"Opened {p.name}")
                return
            except Exception as exc:
                self._toast(f"Open error: {exc}")
                return

        self._toast("⚠️ Download PDFs first!")

    def save_excel_tracker(self) -> None:
        t_rows = self._current_tracker_rows()
        if not t_rows:
            self._toast("⚠️ No PDFs to save!")
            return
        ym = self.active_year_month()
        month_word = format_month_words(ym).split()[0] if ym else "OCTOBER"
        default_name = f"KY - COA TRACKER {month_word}.xlsx"
        init_dir = ky_downloads_dir(self.active_court_code(), ym)
        init_dir.mkdir(parents=True, exist_ok=True)
        dest = filedialog.asksaveasfilename(
            parent=self,
            title="Save Kentucky Excel Tracker (.xlsx)",
            defaultextension=".xlsx",
            initialdir=str(init_dir),
            initialfile=default_name,
            filetypes=[("Excel Workbook (.xlsx)", "*.xlsx"), ("All Files", "*.*")],
        )
        if not dest:
            return
        try:
            out_path = write_ky_tracker_xlsx(Path(dest), t_rows, sheet_tab_name=self._tracker_sheet_name())
            self._toast(f"Saved {out_path.name} ({len(t_rows)} rows)!")
        except Exception as exc:
            self._toast(f"Save error: {exc}")

    def open_selected_case_from_top(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        cn = sel[0]
        cid = self.case_meta.get(cn, {}).get("case_id") or (self._get_record(cn).case_id if self._get_record(cn) else "")
        if cid:
            webbrowser.open(f"https://appellatepublic.kycourts.net/case/{cid}")

    def open_selected_hit_case(self) -> None:
        sel = self.hit_tree.selection()
        if not sel:
            return
        rec = self._get_record(sel[0])
        if rec and rec.case_id:
            webbrowser.open(rec.case_url)

    @staticmethod
    def _is_valid_pdf_path(p: Path | None) -> bool:
        if not p or not isinstance(p, Path):
            return False
        try:
            if not p.exists():
                return False
            st = p.stat()
            sz = getattr(st, "st_size", 0)
            if isinstance(sz, (int, float)):
                return sz >= 16
            return True
        except Exception:
            return False

    def _get_ky_pdf_disk_index(self, force_refresh: bool = False) -> dict[str, Path]:
        now = time.monotonic()
        cache = getattr(self, "_pdf_disk_cache", None)
        if not isinstance(cache, dict):
            cache = None
        cache_time = getattr(self, "_pdf_disk_cache_time", 0.0)
        if not isinstance(cache_time, (int, float)):
            cache_time = 0.0
        # Re-use in-memory disk index for 3.0 seconds during table rebuilds so we never re-scan disk 400+ times
        if not force_refresh and cache is not None and (now - cache_time) < 3.0:
            return cache

        idx: dict[str, Path] = {}
        court_code = self.active_court_code() if hasattr(self, "active_court_code") else "KYCOA"
        ym = self.active_year_month() if hasattr(self, "active_year_month") else "2026-10"

        # 1. Active month downloads folder (recursively indexes all day subfolders in one fast pass)
        try:
            m_dir = ky_downloads_dir(court_code, ym)
            if isinstance(m_dir, Path) and m_dir.exists():
                for p in m_dir.rglob("*.pdf"):
                    if KyScannerFrame._is_valid_pdf_path(p):
                        idx[p.name.lower()] = p
        except Exception:
            pass

        # 2. General downloads folder root
        try:
            if DOWNLOADS_KY_DIR.exists():
                for p in DOWNLOADS_KY_DIR.glob("*.pdf"):
                    if KyScannerFrame._is_valid_pdf_path(p):
                        idx[p.name.lower()] = p
        except Exception:
            pass

        # 3. Legacy OUTPUT_DIR fallback
        try:
            if OUTPUT_DIR.exists():
                for p in OUTPUT_DIR.rglob("*.pdf"):
                    if KyScannerFrame._is_valid_pdf_path(p):
                        idx[p.name.lower()] = p
        except Exception:
            pass

        self._pdf_disk_cache = idx
        self._pdf_disk_cache_time = now
        return idx

    def _find_ky_downloaded_pdf(self, case_no: str, doc_obj: KyCollectibleDoc) -> Path | None:
        target_name = f"{doc_obj.save_as_name}.pdf"
        target_lower = target_name.lower()

        # Step 1: O(1) direct check in primary case docket date folder
        court_code = self.active_court_code() if hasattr(self, "active_court_code") else "KYCOA"
        ym = self.active_year_month() if hasattr(self, "active_year_month") else "2026-10"
        case_docket_date = getattr(self, "case_meta", {}).get(case_no, {}).get("date")
        if not case_docket_date:
            r = self._get_record(case_no) if hasattr(self, "_get_record") else None
            if r and r.first_filed_date:
                case_docket_date = r.first_filed_date
        if isinstance(case_docket_date, str) and case_docket_date:
            try:
                case_day_words = format_date_words(case_docket_date)
                c_dir = ky_downloads_dir(court_code, ym, date_str=case_day_words)
                if isinstance(c_dir, Path) and c_dir.exists():
                    primary = c_dir / target_name
                    if KyScannerFrame._is_valid_pdf_path(primary):
                        return primary
            except Exception:
                pass

        # Step 2: O(1) direct check in month root folder
        try:
            m_dir = ky_downloads_dir(court_code, ym)
            if isinstance(m_dir, Path) and m_dir.exists():
                m_target = m_dir / target_name
                if KyScannerFrame._is_valid_pdf_path(m_target):
                    return m_target
        except Exception:
            pass

        # Step 3: O(1) in-memory lookup from disk index
        idx = KyScannerFrame._get_ky_pdf_disk_index(self)
        if isinstance(idx, dict):
            found = idx.get(target_lower)
            if KyScannerFrame._is_valid_pdf_path(found):
                return found
        return None

    def _confirm_auto_save_one_pdf(self, case_no: str, doc_obj: KyCollectibleDoc) -> None:
        local = self._find_ky_downloaded_pdf(case_no, doc_obj)
        if local and local.exists() and local.stat().st_size >= 100:
            re_dl = messagebox.askyesno(
                "Already Downloaded",
                f"This brief PDF is already downloaded:\n\n"
                f"• {local.name}\n\n"
                f"Do you want to re-download and replace it?\n"
                f"(Click 'No' to open the existing file)",
                parent=self,
            )
            if not re_dl:
                try:
                    os.startfile(str(local))
                    self._toast(f"Opened existing: {local.name[:30]}...")
                except Exception as exc:
                    self._toast(f"Error opening PDF: {exc}")
                return

        rec = self._get_record(case_no)
        case_docket_date = getattr(self, "case_meta", {}).get(case_no, {}).get("date") or (rec.first_filed_date if rec else "") or ""
        day_words = format_date_words(case_docket_date) if case_docket_date else None
        dest_dir = ky_downloads_dir(self.active_court_code(), self.active_year_month(), date_str=day_words)
        msg = (
            f"Do you want to download this brief PDF?\n\n"
            f"• Case: {case_no}\n"
            f"• Document: {doc_obj.document_name}\n"
            f"• Save as: {doc_obj.save_as_name}.pdf\n\n"
            f"The file will be saved directly to:\n{dest_dir}"
        )
        if not messagebox.askyesno("Confirm Download", msg, parent=self):
            return
        self._copy_one_name(doc_obj.save_as_name)
        self._auto_save_one_pdf(case_no, doc_obj)

    def _on_open_ky_pdf(self, case_no: str, doc_obj: KyCollectibleDoc) -> None:
        local = self._find_ky_downloaded_pdf(case_no, doc_obj)
        if local and local.exists():
            try:
                os.startfile(str(local))
                self._toast(f"Opened: {local.name[:35]}...")
                return
            except Exception as exc:
                self._toast(f"Error opening PDF: {exc}")
                return
        self._toast("Not downloaded yet. Click the ⬇ icon to download.")

    def _auto_save_one_pdf(self, case_no: str, doc: KyCollectibleDoc) -> None:
        if self.busy:
            self._toast("Wait for current operation to finish")
            return
        court_code = self.active_court_code()
        ym = self.active_year_month()
        rec = self._get_record(case_no)
        case_docket_date = getattr(self, "case_meta", {}).get(case_no, {}).get("date") or (rec.first_filed_date if rec else "") or ""
        day_words = format_date_words(case_docket_date) if case_docket_date else None
        dest_dir = ky_downloads_dir(court_code, ym, date_str=day_words)
        self._toast(f"Downloading {doc.save_as_name}.pdf...")

        def _run_one() -> None:
            try:
                saved_path = download_ky_pdf(
                    doc, dest_dir, court_code=court_code, case_number=case_no, year_month=ym,
                    case_id=rec.case_id if rec else "",
                )
                self.q.put(("download_tracker_changed",))
                self.q.put(("log", f"[AUTO-SAVED] {case_no} -> {saved_path.name}", "hit"))
                r = self._get_record(case_no)
                if r and all(self._find_ky_downloaded_pdf(case_no, d) is not None for d in r.collectible_docs):
                    self.q.put(("dl_case_done", case_no))
                self.q.put(("dl_toast", f"Saved: {saved_path.name}"))
            except Exception as exc:
                self.q.put(("log", f"[ERROR] Failed to download {doc.save_as_name}: {exc}", "err"))
                self.q.put(("err_record", {
                    "type": "Download Error",
                    "case": case_no,
                    "msg": f"{doc.save_as_name}: {exc}",
                    "details": str(exc),
                }))
                self.q.put(("dl_toast", f"Download failed: {doc.save_as_name}"))

        threading.Thread(target=_run_one, daemon=True).start()

    def download_checked_or_all_pdfs(self) -> None:
        if self.busy:
            return
        if not self._active_intern_name():
            self._toast("⚠️ Select Intern Name first!")
            try:
                self.intern_box.focus_set()
            except Exception:
                pass
            return
        if self.checked_cases:
            ordered = [cn for cn in self.hit_tree.get_children() if cn in self.checked_cases]
            if not ordered:
                ordered = sorted(self.checked_cases)
            self._start_pdf_download_worker(ordered)
            return
        self.download_all_month_pdfs()

    def download_selected_case_pdfs(self) -> None:
        if self.busy:
            return
        if not self._active_intern_name():
            self._toast("⚠️ Select Intern Name first!")
            try:
                self.intern_box.focus_set()
            except Exception:
                pass
            return
        sel = list(self.hit_tree.selection())
        if not sel:
            self._toast("⚠️ Select Hit case first!")
            return
        self._start_pdf_download_worker(sel)

    def download_all_month_pdfs(self) -> None:
        if self.busy:
            return
        if not self._active_intern_name():
            self._toast("⚠️ Select Intern Name first!")
            try:
                self.intern_box.focus_set()
            except Exception:
                pass
            return
        hits = [
            cn for cn, r in self.records.items()
            if r.status == "BRIEF_FOUND" and r.collectible_docs
        ]
        if hasattr(self, "case_order") and self.case_order:
            order_map = {cn: i for i, cn in enumerate(self.case_order)}
            hits.sort(key=lambda cn: order_map.get(cn, 999999))
        if not hits:
            self._toast("⚠️ Scan for Hits first!")
            return
        todo = [cn for cn in hits if cn not in self.done_cases] or hits
        self._start_pdf_download_worker(todo)

    def _start_pdf_download_worker(self, case_numbers: list[str]) -> None:
        STOP_EVENT.clear()
        self.busy = True
        self.active_action = "download"
        self._set_buttons()
        court_code = self.active_court_code()
        ym = self.active_year_month()
        default_dest = ky_downloads_dir(court_code, ym)
        tasks: list[tuple[str, KyCollectibleDoc, Path]] = []
        for cn in case_numbers:
            rec = self._get_record(cn)
            if rec and rec.collectible_docs:
                case_docket_date = getattr(self, "case_meta", {}).get(cn, {}).get("date") or rec.first_filed_date or ""
                day_words = format_date_words(case_docket_date) if case_docket_date else None
                c_dest = ky_downloads_dir(court_code, ym, date_str=day_words)
                for d in rec.collectible_docs:
                    tasks.append((cn, d, c_dest))
        if not tasks:
            self.busy = False
            self._set_buttons()
            return

        def _worker() -> None:
            total = len(tasks)
            saved_cnt = 0
            existing_cnt = 0
            newly_saved_cnt = 0
            failed_cases: set[str] = set()
            failed_details: list[dict[str, Any]] = []
            self.q.put(("log", f"=== AUTO-DOWNLOADING ALL {total} PDF(s) ===", "head"))

            # Phase 1: Fast instant scan of already-downloaded PDFs on disk
            needed_tasks: list[tuple[int, str, KyCollectibleDoc, Path]] = []
            for idx, (cn, doc, task_dest) in enumerate(tasks, start=1):
                if STOP_EVENT.is_set():
                    break
                target = task_dest / f"{doc.save_as_name}.pdf"
                existing_pdf = target if (target.exists() and target.stat().st_size >= 100) else self._find_ky_downloaded_pdf(cn, doc)
                if existing_pdf:
                    saved_cnt += 1
                    existing_cnt += 1
                    self.q.put(("log", f"[{idx:02d}/{total:02d}] [EXISTS] {existing_pdf.name} (Skipping re-download)", "dim"))
                    self.q.put(("prog", idx, total, f"[{idx}/{total}] Already downloaded: {existing_pdf.name[:35]}..."))
                    rec = self._get_record(cn)
                    if (
                        cn not in failed_cases
                        and rec
                        and all(self._find_ky_downloaded_pdf(cn, d) is not None for d in rec.collectible_docs)
                    ):
                        self.q.put(("dl_case_done", cn))
                else:
                    needed_tasks.append((idx, cn, doc, task_dest))

            # Phase 2: Parallel high-speed downloads for remaining files (3 concurrent workers)
            if needed_tasks and not STOP_EVENT.is_set():
                task_q: queue.Queue[tuple[int, str, KyCollectibleDoc, Path]] = queue.Queue()
                for item in needed_tasks:
                    task_q.put(item)

                worker_lock = threading.Lock()
                num_workers = min(3, len(needed_tasks))

                def _download_thread() -> None:
                    nonlocal saved_cnt, newly_saved_cnt
                    while not STOP_EVENT.is_set():
                        try:
                            item = task_q.get_nowait()
                        except queue.Empty:
                            break

                        idx, cn, doc, task_dest = item
                        rec = self._get_record(cn)
                        self.q.put(("prog", idx - 1, total, f"[{idx}/{total}] Downloading: {doc.save_as_name[:35]}.pdf..."))

                        try:
                            out_file = download_ky_pdf(
                                doc, task_dest, court_code=court_code, case_number=cn, year_month=ym,
                                case_id=rec.case_id if rec else "",
                            )
                            with worker_lock:
                                saved_cnt += 1
                                newly_saved_cnt += 1
                                curr_saved = saved_cnt

                            self.q.put(("log", f"[{idx:02d}/{total:02d}] [SAVED] {out_file.name}", "hit"))
                            self.q.put(("prog", curr_saved, total, f"[{idx}/{total}] Saved: {out_file.name[:35]}..."))
                        except Exception as exc:
                            with worker_lock:
                                failed_cases.add(cn)
                                err_msg = str(exc)
                                failed_details.append({
                                    "case": cn,
                                    "doc_name": doc.save_as_name,
                                    "error": err_msg,
                                    "doc_obj": doc,
                                    "dest": str(task_dest),
                                })
                            self.q.put(("log", f"[{idx:02d}/{total:02d}] [ERROR] {doc.save_as_name}: {exc}", "err"))
                            self.q.put(("err_record", {
                                "type": "Download Error",
                                "case": cn,
                                "msg": f"{doc.save_as_name}: {exc}",
                                "details": err_msg,
                            }))
                            self.q.put(("prog", idx, total, f"[{idx}/{total}] Error: {doc.save_as_name[:30]}"))

                        rec = self._get_record(cn)
                        if (
                            cn not in failed_cases
                            and rec
                            and all(self._find_ky_downloaded_pdf(cn, d) is not None for d in rec.collectible_docs)
                        ):
                            self.q.put(("dl_case_done", cn))

                        task_q.task_done()

                threads = [threading.Thread(target=_download_thread, daemon=True) for _ in range(num_workers)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

            report = {
                "total": total,
                "saved_cnt": saved_cnt,
                "existing_cnt": existing_cnt,
                "newly_saved_cnt": newly_saved_cnt,
                "failed_cases": list(failed_cases),
                "failed_details": failed_details,
                "default_dest": str(default_dest),
                "case_numbers": list(case_numbers),
            }
            if newly_saved_cnt:
                self.q.put(("download_tracker_changed",))
            self.q.put(("dl_complete_report", report))
            self.q.put(("dl_toast", f"Auto-saved {saved_cnt}/{total} PDF(s) to {default_dest.name}!"))
            self.q.put(("done", False))

        threading.Thread(target=_worker, daemon=True).start()

    def open_downloads_folder(self) -> None:
        d = ky_downloads_dir(self.active_court_code(), self.active_year_month())
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(d))  # type: ignore[attr-defined]
        except Exception:
            webbrowser.open(d.as_uri())

    def audit_downloads_folder(self) -> None:
        court_code = self.active_court_code()
        ym = self.active_year_month()
        m_dir = ky_downloads_dir(court_code, ym)
        month_name = format_month_words(ym)

        if not m_dir.exists():
            messagebox.showinfo(
                "Scan Downloads",
                f"No downloads directory found on disk for {court_code} {month_name}.\n\nPath:\n{m_dir}",
                parent=self,
            )
            return

        # 1. Collect all expected collectible docs
        hits = [r for r in self.records.values() if r.status == "BRIEF_FOUND"]
        expected_docs: dict[str, tuple[str, KyCollectibleDoc]] = {}
        for r in hits:
            for d in r.collectible_docs:
                expected_docs[f"{d.save_as_name}.pdf".lower()] = (r.case_number, d)

        # 2. Scan actual files on disk
        files_by_name: dict[str, list[Path]] = {}
        corrupted_files: list[Path] = []
        for p in m_dir.rglob("*"):
            if p.is_file():
                if p.name.lower().endswith(".part") or p.stat().st_size < 100:
                    corrupted_files.append(p)
                if p.name.lower().endswith(".pdf"):
                    files_by_name.setdefault(p.name.lower(), []).append(p)

        # 3. Find duplicates (same filename present in more than 1 path)
        duplicates: list[tuple[str, list[Path]]] = []
        for fname, plist in sorted(files_by_name.items()):
            if len(plist) > 1:
                duplicates.append((fname, plist))

        # 4. Find missing (expected docs not on disk)
        missing_docs: list[tuple[str, str, KyCollectibleDoc]] = []
        for exp_name, (cn, doc) in sorted(expected_docs.items()):
            if exp_name not in files_by_name:
                missing_docs.append((cn, exp_name, doc))

        # 5. Find extra / unrecognized PDFs
        extra_files: list[Path] = []
        for fname, plist in sorted(files_by_name.items()):
            if fname not in expected_docs:
                extra_files.extend(plist)

        total_expected = len(expected_docs)
        total_found = len(files_by_name)
        has_issues = bool(missing_docs or duplicates or corrupted_files)

        win = tk.Toplevel(self)
        win.title(f"Downloads Audit & Verification — {court_code} {month_name}")
        win.configure(bg=BG)

        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        win_w = min(1100, max(760, int(sw * 0.86)))
        win_h = min(700, max(460, int(sh * 0.82)))
        win.geometry(f"{win_w}x{win_h}")
        win.minsize(740, 440)

        win.update_idletasks()
        try:
            x = self.winfo_rootx() + max(0, (self.winfo_width() - win_w) // 2)
            y = self.winfo_rooty() + max(0, (self.winfo_height() - win_h) // 2)
            win.geometry(f"{win_w}x{win_h}+{x}+{y}")
        except Exception:
            win.geometry(f"{win_w}x{win_h}")

        if has_issues:
            b_bg, b_border, b_color = (
                ERR_BG if (missing_docs or corrupted_files) else WARN_BG,
                ERR_COLOR if (missing_docs or corrupted_files) else WARN_COLOR,
                ERR_COLOR if (missing_docs or corrupted_files) else WARN_COLOR,
            )
            title = f"⚠️ Download Audit: {len(missing_docs)} Missing • {len(duplicates)} Duplicate(s) • {len(corrupted_files)} Broken"
        else:
            b_bg, b_border, b_color = "#122a18", ACCENT, ACCENT
            title = f"✅ Downloads 100% Verified! All {total_expected} Briefs Clean on Disk"

        hdr_f = tk.Frame(win, bg=b_bg, highlightbackground=b_border, highlightthickness=1, padx=14, pady=10)
        hdr_f.pack(fill="x", padx=14, pady=(12, 8))

        tk.Label(hdr_f, text=title, bg=b_bg, fg=b_color, font=("Segoe UI", 12, "bold"), anchor="w").pack(fill="x")
        stats_line = (
            f"Folder: {m_dir.name} • Expected: {total_expected} PDFs • On Disk: {total_found} unique PDFs • "
            f"Missing: {len(missing_docs)} • Duplicates: {len(duplicates)} • Broken: {len(corrupted_files)}"
        )
        tk.Label(hdr_f, text=stats_line, bg=b_bg, fg=TEXT, font=("Segoe UI", 9), anchor="w").pack(fill="x", pady=(2, 0))

        content_f = tk.Frame(win, bg=BG)
        content_f.pack(fill="both", expand=True, padx=14, pady=4)

        if not has_issues:
            ok_box = tk.Frame(content_f, bg="#122416", highlightbackground=ACCENT, highlightthickness=1, padx=20, pady=30)
            ok_box.pack(fill="both", expand=True, pady=10)
            tk.Label(ok_box, text="🎉 Zero Download Discrepancies!", bg="#122416", fg=ACCENT, font=("Segoe UI", 14, "bold")).pack(pady=(0, 8))
            tk.Label(
                ok_box,
                text=f"Every single collectible brief ({total_expected} PDFs across {len(hits)} cases) is properly saved on your hard drive.\n"
                     f"• No missing brief files\n"
                     f"• No duplicate files across folders\n"
                     f"• No zero-byte or corrupted .part files",
                bg="#122416", fg=TEXT, font=("Segoe UI", 10), justify="center",
            ).pack()
        else:
            if missing_docs:
                mis_box = tk.LabelFrame(
                    content_f, text=f" ❌ Missing / Not Downloaded ({len(missing_docs)} PDFs) ",
                    bg=CARD, fg=ERR_COLOR, font=("Segoe UI", 9, "bold"), bd=1, highlightbackground=ERR_COLOR,
                )
                mis_box.pack(fill="both", expand=True, pady=(0, 6))

                mis_frame = tk.Frame(mis_box, bg=CARD)
                mis_frame.pack(fill="both", expand=True, padx=8, pady=(4, 6))

                cols = ("num", "case", "file", "docname")
                tv_mis = ttk.Treeview(mis_frame, columns=cols, show="headings", height=min(6, len(missing_docs)))
                tv_mis.heading("num", text="#", anchor="center")
                tv_mis.heading("case", text="Case #", anchor="w")
                tv_mis.heading("file", text="Expected Save-As File", anchor="w")
                tv_mis.heading("docname", text="Original Document Name", anchor="w")
                tv_mis.column("num", width=36, minwidth=32, anchor="center", stretch=False)
                tv_mis.column("case", width=120, minwidth=100, anchor="w", stretch=False)
                tv_mis.column("file", width=340, minwidth=200, anchor="w", stretch=True)
                tv_mis.column("docname", width=320, minwidth=200, anchor="w", stretch=True)

                sb_mis = ttk.Scrollbar(mis_frame, orient="vertical", command=tv_mis.yview)
                hsb_mis = ttk.Scrollbar(mis_frame, orient="horizontal", command=tv_mis.xview)
                tv_mis.configure(yscrollcommand=sb_mis.set, xscrollcommand=hsb_mis.set)
                sb_mis.pack(side="right", fill="y")
                hsb_mis.pack(side="bottom", fill="x")
                tv_mis.pack(side="left", fill="both", expand=True)

                for idx, (cn, fname, doc) in enumerate(missing_docs, start=1):
                    tv_mis.insert("", "end", values=(str(idx), cn, f"{doc.save_as_name}.pdf", doc.document_name))

                def _dl_missing():
                    missing_cns = list(dict.fromkeys(cn for cn, _, _ in missing_docs))
                    win.destroy()
                    self._start_pdf_download_worker(missing_cns)

                mis_act = tk.Frame(mis_box, bg=CARD)
                mis_act.pack(fill="x", padx=8, pady=(0, 6))
                tk.Button(
                    mis_act, text="⬇ Download Missing PDFs Only", command=_dl_missing,
                    bg=ACCENT, fg="#111111", relief="flat", bd=0, padx=10, pady=3,
                    font=("Segoe UI", 9, "bold"), cursor="hand2",
                ).pack(side="left")

            if duplicates:
                dup_box = tk.LabelFrame(
                    content_f, text=f" ⚠️ Duplicate Files on Disk ({len(duplicates)} names with multiple copies) ",
                    bg=CARD, fg=WARN_COLOR, font=("Segoe UI", 9, "bold"), bd=1, highlightbackground=WARN_COLOR,
                )
                dup_box.pack(fill="both", expand=True, pady=(0, 6))

                dup_frame = tk.Frame(dup_box, bg=CARD)
                dup_frame.pack(fill="both", expand=True, padx=8, pady=(4, 6))

                cols_dup = ("num", "file", "copies", "locations")
                tv_dup = ttk.Treeview(dup_frame, columns=cols_dup, show="headings", height=min(5, len(duplicates)))
                tv_dup.heading("num", text="#", anchor="center")
                tv_dup.heading("file", text="Filename", anchor="w")
                tv_dup.heading("copies", text="Copies", anchor="center")
                tv_dup.heading("locations", text="Disk Locations", anchor="w")
                tv_dup.column("num", width=36, minwidth=32, anchor="center", stretch=False)
                tv_dup.column("file", width=280, minwidth=180, anchor="w", stretch=True)
                tv_dup.column("copies", width=60, minwidth=50, anchor="center", stretch=False)
                tv_dup.column("locations", width=480, minwidth=240, anchor="w", stretch=True)

                sb_dup = ttk.Scrollbar(dup_frame, orient="vertical", command=tv_dup.yview)
                hsb_dup = ttk.Scrollbar(dup_frame, orient="horizontal", command=tv_dup.xview)
                tv_dup.configure(yscrollcommand=sb_dup.set, xscrollcommand=hsb_dup.set)
                sb_dup.pack(side="right", fill="y")
                hsb_dup.pack(side="bottom", fill="x")
                tv_dup.pack(side="left", fill="both", expand=True)

                for idx, (fname, plist) in enumerate(duplicates, start=1):
                    locs = "  |  ".join(p.parent.name for p in plist)
                    tv_dup.insert("", "end", values=(str(idx), fname, str(len(plist)), locs))
                sb_dup.pack(side="right", fill="y", padx=(0, 8), pady=6)

                for idx, (fname, plist) in enumerate(duplicates, start=1):
                    locs = "  |  ".join(p.parent.name for p in plist)
                    tv_dup.insert("", "end", values=(str(idx), fname, str(len(plist)), locs))

            if corrupted_files:
                cor_box = tk.LabelFrame(
                    content_f, text=f" ⚠️ Corrupted / Zero-byte / .part Files ({len(corrupted_files)} files) ",
                    bg=CARD, fg=ERR_COLOR, font=("Segoe UI", 9, "bold"), bd=1, highlightbackground=ERR_COLOR,
                )
                cor_box.pack(fill="x", pady=(0, 6))
                for cp in corrupted_files:
                    tk.Label(
                        cor_box, text=f"  • {cp.name} ({cp.stat().st_size} bytes) in {cp.parent.name}",
                        bg=CARD, fg=ERR_COLOR, font=("Consolas", 9), anchor="w",
                    ).pack(fill="x", padx=8, pady=1)

        btm_bar = tk.Frame(win, bg=BG)
        btm_bar.pack(fill="x", padx=14, pady=(4, 12))

        tk.Button(
            btm_bar, text="📁 Open Downloads Folder", command=self.open_downloads_folder,
            bg=CARD, fg=TEXT, activebackground=LINE, activeforeground=TEXT,
            relief="flat", bd=0, padx=12, pady=4, font=("Segoe UI", 9, "bold"), cursor="hand2",
        ).pack(side="left")

        def _copy_audit_report():
            lines = [f"=== DOWNLOADS AUDIT REPORT: {court_code} {month_name} ==="]
            lines.append(f"Folder: {m_dir}")
            lines.append(f"Expected: {total_expected} | On Disk: {total_found} | Missing: {len(missing_docs)} | Duplicates: {len(duplicates)}")
            if missing_docs:
                lines.append("\n[MISSING BRIEFS]")
                for cn, fn, doc in missing_docs:
                    lines.append(f"  • {cn}: {fn}")
            if duplicates:
                lines.append("\n[DUPLICATE FILES ON DISK]")
                for fn, plist in duplicates:
                    lines.append(f"  • {fn} ({len(plist)} copies): {', '.join(str(p) for p in plist)}")
            if corrupted_files:
                lines.append("\n[CORRUPTED FILES]")
                for cp in corrupted_files:
                    lines.append(f"  • {cp.name}")
            self.clipboard_clear()
            self.clipboard_append("\n".join(lines))
            self._toast("Copied audit report to clipboard!")

        tk.Button(
            btm_bar, text="📋 Copy Audit Report", command=_copy_audit_report,
            bg=CARD, fg=MUTED, activebackground=LINE, activeforeground=TEXT,
            relief="flat", bd=0, padx=12, pady=4, font=("Segoe UI", 9), cursor="hand2",
        ).pack(side="left", padx=(8, 0))

        tk.Button(
            btm_bar, text="Close", command=win.destroy,
            bg=LINE, fg=TEXT, activebackground=BG, activeforeground=TEXT,
            relief="flat", bd=0, padx=16, pady=4, font=("Segoe UI", 9, "bold"), cursor="hand2",
        ).pack(side="right")

    def show_all_pdfs_modal(self) -> None:
        hits = [r for r in self.records.values() if r.status == "BRIEF_FOUND"]
        total_pdfs = sum(len(r.collectible_docs) for r in hits)
        if not total_pdfs:
            messagebox.showinfo("Brief PDFs", "No collectible Kentucky PDFs found yet.")
            return
        win = tk.Toplevel(self)
        win.title(f"All Kentucky Collectible PDFs ({total_pdfs} PDFs across {len(hits)} cases)")
        win.configure(bg=BG)

        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        win_w = min(1080, max(740, int(sw * 0.84)))
        win_h = min(620, max(440, int(sh * 0.78)))
        win.geometry(f"{win_w}x{win_h}")
        win.minsize(720, 400)

        win.update_idletasks()
        try:
            x = self.winfo_rootx() + max(0, (self.winfo_width() - win_w) // 2)
            y = self.winfo_rooty() + max(0, (self.winfo_height() - win_h) // 2)
            win.geometry(f"{win_w}x{win_h}+{x}+{y}")
        except Exception:
            win.geometry(f"{win_w}x{win_h}")

        tbl_f = tk.Frame(win, bg=BG)
        tbl_f.pack(fill="both", expand=True, padx=14, pady=14)

        cols = ("num", "case", "savename", "docname", "filed", "submitter")
        tv = ttk.Treeview(tbl_f, columns=cols, show="headings", selectmode="browse")
        for c, lbl, w, strt in (
            ("num", "#", 48, False),
            ("case", "Case #", 115, False),
            ("savename", "Standard Save-As Filename", 300, True),
            ("docname", "Popup Document Name", 240, True),
            ("filed", "Filed Date", 140, False),
            ("submitter", "Submitted By", 140, False),
        ):
            tv.heading(c, text=lbl, anchor="w")
            tv.column(c, width=w, minwidth=w - 20, anchor="w", stretch=strt)

        sb = ttk.Scrollbar(tbl_f, orient="vertical", command=tv.yview)
        hsb = ttk.Scrollbar(tbl_f, orient="horizontal", command=tv.xview)
        tv.configure(yscrollcommand=sb.set, xscrollcommand=hsb.set)
        sb.pack(side="right", fill="y")
        hsb.pack(side="bottom", fill="x")
        tv.pack(side="left", fill="both", expand=True)

        idx = 0
        for r in hits:
            for d in r.collectible_docs:
                idx += 1
                tv.insert(
                    "", "end", iid=f"{idx}",
                    values=(f"[{idx:02d}]", r.case_number, d.save_as_name, d.document_name, format_date_words(d.filed_date), d.submitter),
                )

        def _copy_sel(_e=None):
            s = tv.selection()
            if not s:
                return
            vals = tv.item(s[0], "values")
            if len(vals) >= 3:
                self.clipboard_clear()
                self.clipboard_append(vals[2])
                self._toast(f"Copied {vals[2]}")

        tv.bind("<Double-1>", _copy_sel)

    def show_manual_check_modal(self, report: dict[str, Any] | None = None) -> None:
        court_code = self.active_court_code()
        ym = self.active_year_month()
        month_display = format_month_words(ym)
        manual_items = self.get_manual_check_cases()

        failed_details: list[dict[str, Any]] = []
        if report and report.get("failed_details"):
            failed_details = list(report["failed_details"])
        else:
            for err in self.pinned_errors:
                if err.get("type") == "Download Error":
                    failed_details.append({
                        "case": err.get("case", ""),
                        "doc_name": err.get("msg", ""),
                        "error": err.get("details", "") or err.get("msg", ""),
                    })

        win = tk.Toplevel(self)
        win.title(f"Kentucky C-Track ({court_code}) • Download & Manual Verification Guide")
        win.configure(bg=BG)

        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        win_w = min(1140, max(780, int(sw * 0.86)))
        win_h = min(720, max(460, int(sh * 0.82)))
        win.geometry(f"{win_w}x{win_h}")
        win.minsize(740, 440)

        win.update_idletasks()
        try:
            x = self.winfo_rootx() + max(0, (self.winfo_width() - win_w) // 2)
            y = self.winfo_rooty() + max(0, (self.winfo_height() - win_h) // 2)
            win.geometry(f"{win_w}x{win_h}+{x}+{y}")
        except Exception:
            win.geometry(f"{win_w}x{win_h}")

        has_errors = bool(failed_details)
        has_manual = bool(manual_items)
        if has_errors:
            banner_bg = ERR_BG
            banner_border = ERR_COLOR
            title_color = ERR_COLOR
            title_text = f"❌ Download Completed with {len(failed_details)} Error(s) • {len(manual_items)} Manual Check(s) Needed"
        elif has_manual:
            banner_bg = WARN_BG
            banner_border = WARN_COLOR
            title_color = WARN_COLOR
            title_text = f"⚠️ Download Finished — {len(manual_items)} Case(s) Require Manual Check on Court Website"
        else:
            banner_bg = "#122a18"
            banner_border = ACCENT
            title_color = ACCENT
            title_text = "✅ 100% Complete & Verified! All Briefs Saved with No Discrepancies"

        hdr_frame = tk.Frame(win, bg=banner_bg, highlightbackground=banner_border, highlightthickness=1, padx=14, pady=10)
        hdr_frame.pack(fill="x", padx=14, pady=(12, 8))

        tk.Label(
            hdr_frame, text=title_text, bg=banner_bg, fg=title_color,
            font=("Segoe UI", 12, "bold"), anchor="w",
        ).pack(fill="x")

        if report:
            stats_text = (
                f"Batch Progress: {report.get('saved_cnt', 0)}/{report.get('total', 0)} PDFs accounted for "
                f"({report.get('newly_saved_cnt', 0)} newly downloaded, {report.get('existing_cnt', 0)} already on disk, {len(failed_details)} failed) • "
                f"Folder: {Path(report.get('default_dest', '')).name}"
            )
        else:
            stats_text = (
                f"Jurisdiction: {court_code} • Month: {month_display} • "
                f"{len(self.records)} cases loaded • {len(manual_items)} item(s) flagged for human verification"
            )

        tk.Label(
            hdr_frame, text=stats_text, bg=banner_bg, fg=TEXT,
            font=("Segoe UI", 9), anchor="w",
        ).pack(fill="x", pady=(2, 0))

        content_f = tk.Frame(win, bg=BG)
        content_f.pack(fill="both", expand=True, padx=14, pady=4)

        if failed_details:
            err_box = tk.LabelFrame(
                content_f, text=f" ❌ Download Errors ({len(failed_details)} file(s)) ",
                bg=CARD, fg=ERR_COLOR, font=("Segoe UI", 9, "bold"), bd=1, highlightbackground=ERR_COLOR,
            )
            err_box.pack(fill="x", pady=(0, 8), ipady=4)

            err_table_frame = tk.Frame(err_box, bg=CARD)
            err_table_frame.pack(fill="both", expand=True, padx=8, pady=(4, 6))

            err_cols = ("case", "doc", "reason")
            err_tv = ttk.Treeview(err_table_frame, columns=err_cols, show="headings", height=min(4, len(failed_details)))
            err_tv.heading("case", text="Case #", anchor="w")
            err_tv.heading("doc", text="Document Name", anchor="w")
            err_tv.heading("reason", text="Error Reason", anchor="w")
            err_tv.column("case", width=120, minwidth=100, anchor="w", stretch=False)
            err_tv.column("doc", width=340, minwidth=200, anchor="w", stretch=True)
            err_tv.column("reason", width=420, minwidth=240, anchor="w", stretch=True)

            err_sb = ttk.Scrollbar(err_table_frame, orient="vertical", command=err_tv.yview)
            err_hsb = ttk.Scrollbar(err_table_frame, orient="horizontal", command=err_tv.xview)
            err_tv.configure(yscrollcommand=err_sb.set, xscrollcommand=err_hsb.set)
            err_sb.pack(side="right", fill="y")
            err_hsb.pack(side="bottom", fill="x")
            err_tv.pack(side="left", fill="both", expand=True)

            for item in failed_details:
                err_tv.insert("", "end", values=(item.get("case", ""), item.get("doc_name", ""), item.get("error", "")))

            err_bar = tk.Frame(err_box, bg=CARD)
            err_bar.pack(fill="x", padx=8, pady=(0, 6))

            def _retry_failed():
                failed_cns = list(dict.fromkeys(item["case"] for item in failed_details if item.get("case")))
                win.destroy()
                if failed_cns:
                    self._start_pdf_download_worker(failed_cns)

            tk.Button(
                err_bar, text="🔁 Retry Failed Downloads", command=_retry_failed,
                bg="#451a1a", fg="#fee2e2", activebackground="#5c2424", activeforeground="#ffffff",
                relief="flat", bd=0, padx=10, pady=3, font=("Segoe UI", 9, "bold"), cursor="hand2",
            ).pack(side="left")

        if manual_items:
            man_box = tk.LabelFrame(
                content_f, text=f" ⚠️ Cases Requiring Manual Check / Website Double-Check ({len(manual_items)} cases) ",
                bg=CARD, fg=WARN_COLOR, font=("Segoe UI", 9, "bold"), bd=1, highlightbackground=WARN_COLOR,
            )
            man_box.pack(fill="both", expand=True, pady=(0, 8))

            rule_f = tk.Frame(man_box, bg="#201a0e", padx=10, pady=6)
            rule_f.pack(fill="x", padx=8, pady=(6, 4))
            tk.Label(
                rule_f,
                text="📌 SOP Rules: (1) Multi-Vol: Verify if Vol 2+ are brief text continuations or attached depositions/exhibits. Per BPM Stephanie rule, exclude if not continuation.\n"
                     "              (2) Missing/Review Note: Docket shows brief filed, but PDF link was not available via C-Track API (check website).",
                bg="#201a0e", fg="#fbbf24", font=("Segoe UI", 8), justify="left", anchor="w",
            ).pack(fill="x")

            man_table_frame = tk.Frame(man_box, bg=CARD)
            man_table_frame.pack(fill="both", expand=True, padx=8, pady=(4, 4))

            man_cols = ("num", "case", "flags", "title", "guidance")
            man_tv = ttk.Treeview(man_table_frame, columns=man_cols, show="headings", selectmode="browse")
            man_tv.heading("num", text="#", anchor="center")
            man_tv.heading("case", text="Case #", anchor="w")
            man_tv.heading("flags", text="Flag / Reason", anchor="w")
            man_tv.heading("title", text="Case Title", anchor="w")
            man_tv.heading("guidance", text="Action Required / Guidance", anchor="w")

            man_tv.column("num", width=38, minwidth=34, anchor="center", stretch=False)
            man_tv.column("case", width=120, minwidth=100, anchor="w", stretch=False)
            man_tv.column("flags", width=160, minwidth=130, anchor="w", stretch=False)
            man_tv.column("title", width=320, minwidth=200, anchor="w", stretch=True)
            man_tv.column("guidance", width=460, minwidth=260, anchor="w", stretch=True)

            man_sb = ttk.Scrollbar(man_table_frame, orient="vertical", command=man_tv.yview)
            man_hsb = ttk.Scrollbar(man_table_frame, orient="horizontal", command=man_tv.xview)
            man_tv.configure(yscrollcommand=man_sb.set, xscrollcommand=man_hsb.set)
            man_sb.pack(side="right", fill="y")
            man_hsb.pack(side="bottom", fill="x")
            man_tv.pack(side="left", fill="both", expand=True)

            for idx, item in enumerate(manual_items, start=1):
                clean_title = " ".join((item["title"] or "").split())
                man_tv.insert(
                    "", "end", iid=str(idx),
                    values=(str(idx), item["case_number"], item["flags"], clean_title, item["guidance"]),
                )

            # Responsive Selection Detail Card: Displays complete, unclipped text with dynamic word-wrapping
            detail_card = tk.Frame(man_box, bg="#161c18", highlightbackground=LINE, highlightthickness=1, padx=10, pady=6)
            detail_card.pack(fill="x", padx=8, pady=(2, 6))

            sel_hdr_lbl = tk.Label(
                detail_card, text="💡 Click any row above to view full title & complete guidance:",
                bg="#161c18", fg=MUTED, font=("Segoe UI", 8, "bold"), anchor="w",
            )
            sel_hdr_lbl.pack(fill="x")

            sel_title_lbl = tk.Label(
                detail_card, text="",
                bg="#161c18", fg=TEXT, font=("Segoe UI", 9, "bold"), anchor="w", justify="left",
            )
            sel_title_lbl.pack(fill="x", pady=(2, 2))

            sel_guide_lbl = tk.Label(
                detail_card, text="",
                bg="#161c18", fg=WARN_COLOR, font=("Segoe UI", 9), anchor="w", justify="left",
            )
            sel_guide_lbl.pack(fill="x")

            def _on_detail_resize(event):
                w = max(280, event.width - 24)
                sel_title_lbl.configure(wraplength=w)
                sel_guide_lbl.configure(wraplength=w)
            detail_card.bind("<Configure>", _on_detail_resize)

            def _on_manual_sel(_e=None):
                sel = man_tv.selection()
                if not sel:
                    return
                vals = man_tv.item(sel[0], "values")
                if len(vals) >= 5:
                    sel_hdr_lbl.configure(text=f"📌 Selected Case: {vals[1]}  •  Flag: {vals[2]}", fg=ACCENT)
                    sel_title_lbl.configure(text=f"Title: {vals[3]}")
                    sel_guide_lbl.configure(text=f"Action Required: {vals[4]}")

            man_tv.bind("<<TreeviewSelect>>", _on_manual_sel)

            if manual_items:
                man_tv.selection_set("1")
                man_tv.focus("1")
                _on_manual_sel()

            act_bar = tk.Frame(man_box, bg=CARD)
            act_bar.pack(fill="x", padx=8, pady=(0, 8))

            def _get_selected_case_no() -> str | None:
                sel = man_tv.selection()
                if not sel:
                    return None
                vals = man_tv.item(sel[0], "values")
                return vals[1] if len(vals) >= 2 else None

            def _open_case_site():
                c_no = _get_selected_case_no()
                if not c_no:
                    self._toast("Select a case in the table first")
                    return
                rec = self._get_record(c_no)
                url = rec.case_url if rec and rec.case_id else "https://appellatepublic.kycourts.net"
                webbrowser.open(url)
                self._toast(f"Opened {c_no} in court website")

            def _select_in_app():
                c_no = _get_selected_case_no()
                if not c_no:
                    self._toast("Select a case in the table first")
                    return
                if self.hit_tree.exists(c_no):
                    self.hit_tree.selection_set(c_no)
                    self.hit_tree.see(c_no)
                    self._on_hit_select()
                    self._toast(f"Selected {c_no} in main hit-list!")
                elif self.tree.exists(c_no):
                    self.tree.selection_set(c_no)
                    self.tree.see(c_no)
                    self._toast(f"Selected {c_no} in top cases list!")

            def _copy_case_no():
                c_no = _get_selected_case_no()
                if not c_no:
                    return
                self.clipboard_clear()
                self.clipboard_append(c_no)
                self._toast(f"Copied {c_no}")

            tk.Button(
                act_bar, text="🌐 Open Case on Website ↗", command=_open_case_site,
                bg="#2a2415", fg=WARN_COLOR, activebackground="#3d3319", activeforeground="#fcd34d",
                relief="flat", bd=0, padx=10, pady=3, font=("Segoe UI", 9, "bold"), cursor="hand2",
            ).pack(side="left", padx=(0, 6))

            tk.Button(
                act_bar, text="🔍 View & Edit in App", command=_select_in_app,
                bg=LINE, fg=TEXT, activebackground=BG, activeforeground=TEXT,
                relief="flat", bd=0, padx=10, pady=3, font=("Segoe UI", 9), cursor="hand2",
            ).pack(side="left", padx=(0, 6))

            tk.Button(
                act_bar, text="📋 Copy Case #", command=_copy_case_no,
                bg=LINE, fg=MUTED, activebackground=BG, activeforeground=TEXT,
                relief="flat", bd=0, padx=8, pady=3, font=("Segoe UI", 9), cursor="hand2",
            ).pack(side="left")

            man_tv.bind("<Double-1>", lambda _e: _open_case_site())

        elif not failed_details:
            clean_box = tk.Frame(content_f, bg="#122416", highlightbackground=ACCENT, highlightthickness=1, padx=20, pady=30)
            clean_box.pack(fill="both", expand=True, pady=10)
            tk.Label(
                clean_box, text="🎉 100% Complete & Verified!",
                bg="#122416", fg=ACCENT, font=("Segoe UI", 14, "bold"),
            ).pack(pady=(0, 8))
            tk.Label(
                clean_box,
                text="All brief files in this batch were downloaded cleanly according to standard BPM naming rules.\n"
                     "No multi-volume ambiguity, missing docket PDFs, or network errors were detected.",
                bg="#122416", fg=TEXT, font=("Segoe UI", 10), justify="center",
            ).pack()

        btm_bar = tk.Frame(win, bg=BG)
        btm_bar.pack(fill="x", padx=14, pady=(4, 12))

        tk.Button(
            btm_bar, text="📁 Open Downloads Folder", command=self.open_downloads_folder,
            bg=CARD, fg=TEXT, activebackground=LINE, activeforeground=TEXT,
            relief="flat", bd=0, padx=12, pady=4, font=("Segoe UI", 9, "bold"), cursor="hand2",
        ).pack(side="left")

        def _copy_all_checklist():
            lines = [f"=== KENTUCKY C-TRACK VERIFICATION REPORT ({court_code} {month_display}) ==="]
            if failed_details:
                lines.append(f"\n[DOWNLOAD ERRORS - {len(failed_details)} FILES]")
                for f in failed_details:
                    lines.append(f"  • {f.get('case')}: {f.get('doc_name')} -> {f.get('error')}")
            if manual_items:
                lines.append(f"\n[CASES NEEDING MANUAL CHECK - {len(manual_items)} CASES]")
                for item in manual_items:
                    lines.append(f"  • {item['case_number']} | {item['flags']} | {item['title']}")
                    lines.append(f"    Action: {item['guidance']}")
            self.clipboard_clear()
            self.clipboard_append("\n".join(lines))
            self._toast("Copied full report to clipboard!")

        tk.Button(
            btm_bar, text="📋 Copy Full Report", command=_copy_all_checklist,
            bg=CARD, fg=MUTED, activebackground=LINE, activeforeground=TEXT,
            relief="flat", bd=0, padx=12, pady=4, font=("Segoe UI", 9), cursor="hand2",
        ).pack(side="left", padx=(8, 0))

        tk.Button(
            btm_bar, text="Close", command=win.destroy,
            bg=LINE, fg=TEXT, activebackground=BG, activeforeground=TEXT,
            relief="flat", bd=0, padx=16, pady=4, font=("Segoe UI", 9, "bold"), cursor="hand2",
        ).pack(side="right")
