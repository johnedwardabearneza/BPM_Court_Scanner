__author__ = "John Edward Bearneza"
__version__ = "2.1.2"

import os
from pathlib import Path
import queue
import re
import shutil
import sys
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from tkinter import filedialog, messagebox, ttk

if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("qwardz.bpm.court.scanner.release.v2")
    except Exception:
        pass
    try:
        import ctypes
        # Enable Per-Monitor V2 DPI awareness (Windows 10 1703+) to prevent blurry text and oversized bitmap scaling on high-DPI laptops
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

from config import APP_AUTHOR, BASE_DIR, OUTPUT_DIR, STOP_EVENT, find_asset, verify_app_integrity
from gui_hits import HitListView
from gui_ky import KyScannerFrame
from ui_theme import format_date_words, format_month_words

BG, CARD, LINE = "#111111", "#1a1a1a", "#2a2a2a"
TEXT, MUTED, ACCENT = "#ededed", "#8a8a8a", "#4ade80"

CASE_LINE = re.compile(r"^\[(\d+)/(\d+)\]\s+\[(\w+)\s*\]\s+(\d{4}-\d{5})\s+\|\s*(.*?)\s*\|\s*(.*)$")


def format_progress(prog) -> tuple[str, str, str]:
    """Return (status_text, hits_text, tree_tag) for an OptionProgress."""
    if getattr(prog, "brief_pdfs", 0) > 0:
        hits = f"{prog.hits} cases ({prog.brief_pdfs} PDFs)"
    elif prog.completed or prog.hits:
        hits = str(prog.hits)
    else:
        hits = ""
    if prog.is_complete:
        return f"Done  {prog.completed}/{prog.total}", hits, "done"
    if prog.completed or prog.manual:
        warn = f"  ({prog.manual} to recheck)" if prog.manual else ""
        return f"Partial  {prog.completed}/{prog.total or '?'}{warn}", hits, "partial"
    return "Not scanned", "", "pending"


def apply_windows_taskbar_icon(root: tk.Tk, ico_path: str) -> None:
    """Ensure Windows Taskbar and Titlebar strictly display the application icon instead of the Tk feather."""
    if sys.platform != "win32":
        return
    try:
        if not root or not root.winfo_exists():
            return
        import ctypes
        from ctypes import wintypes
        root.update_idletasks()
        cid = root.winfo_id()
        GA_ROOT = 2
        root_hwnd = ctypes.windll.user32.GetAncestor(cid, GA_ROOT)
        if not root_hwnd:
            root_hwnd = cid

        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x00000010
        LR_DEFAULTSIZE = 0x00000040

        hicon_big = ctypes.windll.user32.LoadImageW(None, str(ico_path), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
        hicon_small = ctypes.windll.user32.LoadImageW(None, str(ico_path), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)

        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1

        if hicon_big:
            ctypes.windll.user32.SendMessageW(root_hwnd, WM_SETICON, ICON_BIG, hicon_big)
        if hicon_small:
            ctypes.windll.user32.SendMessageW(root_hwnd, WM_SETICON, ICON_SMALL, hicon_small)

        GCLP_HICON = -14
        GCLP_HICONSM = -34
        try:
            set_class_long = ctypes.windll.user32.SetClassLongPtrW
        except AttributeError:
            set_class_long = ctypes.windll.user32.SetClassLongW

        if hicon_big:
            set_class_long(root_hwnd, GCLP_HICON, hicon_big)
        if hicon_small:
            set_class_long(root_hwnd, GCLP_HICONSM, hicon_small)

        # Set AppID and RelaunchIcon directly on window PropertyStore for Windows 11 taskbar
        try:
            class PROPERTYKEY(ctypes.Structure):
                _fields_ = [
                    ('fmtid_data1', wintypes.DWORD),
                    ('fmtid_data2', wintypes.WORD),
                    ('fmtid_data3', wintypes.WORD),
                    ('fmtid_data4', wintypes.BYTE * 8),
                    ('pid', wintypes.DWORD)
                ]

            class GUID(ctypes.Structure):
                _fields_ = [
                    ('Data1', wintypes.DWORD),
                    ('Data2', wintypes.WORD),
                    ('Data3', wintypes.WORD),
                    ('Data4', wintypes.BYTE * 8)
                ]

            class PROPVARIANT(ctypes.Structure):
                _fields_ = [
                    ('vt', wintypes.USHORT),
                    ('wReserved1', wintypes.WORD),
                    ('wReserved2', wintypes.WORD),
                    ('wReserved3', wintypes.WORD),
                    ('pwszVal', wintypes.LPWSTR)
                ]

            class IPropertyStoreVtbl(ctypes.Structure):
                pass

            class IPropertyStore(ctypes.Structure):
                _fields_ = [('lpVtbl', ctypes.POINTER(IPropertyStoreVtbl))]

            IPropertyStoreVtbl._fields_ = [
                ('QueryInterface', ctypes.c_void_p),
                ('AddRef', ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.POINTER(IPropertyStore))),
                ('Release', ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.POINTER(IPropertyStore))),
                ('GetCount', ctypes.c_void_p),
                ('GetAt', ctypes.c_void_p),
                ('GetValue', ctypes.c_void_p),
                ('SetValue', ctypes.WINFUNCTYPE(wintypes.LONG, ctypes.POINTER(IPropertyStore), ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))),
                ('Commit', ctypes.WINFUNCTYPE(wintypes.LONG, ctypes.POINTER(IPropertyStore)))
            ]

            IID_IPropertyStore = GUID(
                0x886d8eeb, 0x8cf2, 0x4446,
                (wintypes.BYTE * 8)(0x8d, 0x02, 0xcd, 0xba, 0x1d, 0xbd, 0xcf, 0x99)
            )
            PKEY_AppUserModel_ID = PROPERTYKEY(
                0x9F4C2855, 0x9F79, 0x4B39,
                (wintypes.BYTE * 8)(0xA8, 0xD0, 0xE1, 0xD4, 0x2D, 0xE1, 0xD5, 0xF3),
                5
            )
            PKEY_RelaunchIcon = PROPERTYKEY(
                0x9F4C2855, 0x9F79, 0x4B39,
                (wintypes.BYTE * 8)(0xA8, 0xD0, 0xE1, 0xD4, 0x2D, 0xE1, 0xD5, 0xF3),
                3
            )

            pStore = ctypes.POINTER(IPropertyStore)()
            hr = ctypes.windll.shell32.SHGetPropertyStoreForWindow(
                root_hwnd, ctypes.byref(IID_IPropertyStore), ctypes.byref(pStore)
            )
            if hr == 0 and pStore:
                pv_id = PROPVARIANT(31, 0, 0, 0, "qwardz.bpm.court.scanner.release.v2")
                pStore.contents.lpVtbl.contents.SetValue(pStore, ctypes.byref(PKEY_AppUserModel_ID), ctypes.byref(pv_id))
                pv_icon = PROPVARIANT(31, 0, 0, 0, f"{ico_path},0")
                pStore.contents.lpVtbl.contents.SetValue(pStore, ctypes.byref(PKEY_RelaunchIcon), ctypes.byref(pv_icon))
                pStore.contents.lpVtbl.contents.Commit(pStore)
                pStore.contents.lpVtbl.contents.Release(pStore)
        except Exception:
            pass
    except Exception:
        pass


class App:
    def __init__(self, root: tk.Tk, events: queue.Queue, worker):
        self.author = verify_app_integrity()
        self.root, self.events, self.worker = root, events, worker
        self.grouped: dict[str, list] = {}
        self._label_to_month: dict[str, str] = {}
        self.month_opts: list = []
        self.busy = False
        self.run_hits = 0
        self.run_pdfs = 0
        self._ny_initialized = False
        self._after_id: str | None = None
        self.remote_blocked = False
        self.remote_blocked_msg = ""
        self._update_pill_btn: tk.Button | None = None
        self._build()
        self._after_id = self.root.after(100, self._poll)
        self._after_icon_ids: list[str] = []
        ico_file = find_asset("app_icon.ico")
        if ico_file.is_file() and "unittest" not in sys.modules:
            self._after_icon_ids.append(self.root.after(80, self._apply_icon))
            self._after_icon_ids.append(self.root.after(300, self._apply_icon))
        threading.Thread(target=self._check_remote_auth, daemon=True).start()

    def _apply_icon(self) -> None:
        try:
            ico_file = find_asset("app_icon.ico")
            if hasattr(self, "root") and self.root.winfo_exists() and ico_file.is_file():
                apply_windows_taskbar_icon(self.root, str(ico_file))
        except Exception:
            pass

    # ---------- layout ----------
    def _btn(self, parent, text, cmd, primary=False):
        return tk.Button(
            parent, text=text, command=cmd, relief="flat", bd=0, padx=14, pady=7, cursor="hand2",
            bg=ACCENT if primary else CARD, fg="#111111" if primary else TEXT,
            activebackground=LINE, activeforeground=TEXT, disabledforeground="#555555",
            font=("Segoe UI", 10, "bold" if primary else "normal"),
        )

    def _build(self) -> None:
        r = self.root
        r.title("BPM Court Scanner")
        r.configure(bg=BG)

        # Apply official scale icon to window & taskbar
        ico_file = find_asset("app_icon.ico")
        png_file = find_asset("app_icon.png")
        if ico_file.is_file():
            try:
                r.iconbitmap(str(ico_file))
                r.iconbitmap(default=str(ico_file))
            except Exception:
                pass
        if png_file.is_file():
            try:
                self._app_icon_img = tk.PhotoImage(file=str(png_file))
                r.iconphoto(False, self._app_icon_img)
                r.iconphoto(True, self._app_icon_img)
            except Exception:
                pass
        if ico_file.is_file():
            apply_windows_taskbar_icon(r, str(ico_file))
        sw = r.winfo_screenwidth()
        sh = r.winfo_screenheight()
        target_w = min(1380, max(940, sw - 40))
        target_h = min(780, max(500, sh - 80))
        x = max(10, (sw - target_w) // 2)
        y = max(10, (sh - target_h - 40) // 2)
        r.geometry(f"{target_w}x{target_h}+{x}+{y}")
        r.minsize(940, 480)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT,
                        rowheight=28, borderwidth=0, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", background=BG, foreground=MUTED, borderwidth=0,
                        font=("Segoe UI", 9))
        style.map("Treeview", background=[("selected", "#2f3b33")], foreground=[("selected", TEXT)])
        style.configure("TCombobox", fieldbackground=CARD, background=CARD, foreground=TEXT,
                        arrowcolor=TEXT, borderwidth=0)
        style.map("TCombobox", fieldbackground=[("readonly", CARD)], foreground=[("readonly", TEXT)])
        style.configure("Horizontal.TProgressbar", troughcolor=CARD, background=ACCENT, borderwidth=0)
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=BG, foreground=MUTED, padding=(10, 6), borderwidth=0)
        style.map("TNotebook.Tab", foreground=[("selected", TEXT)], background=[("selected", CARD)])

        head = tk.Frame(r, bg=BG)
        head.pack(fill="x", padx=24, pady=(18, 6))
        self.header_frame = head

        self.title_lbl = tk.Label(head, text="BPM Court Scanner", bg=BG, fg=TEXT, font=("Segoe UI", 16, "bold"))
        self.title_lbl.pack(side="left")

        sw = tk.Frame(head, bg=CARD, padx=3, pady=3)
        sw.pack(side="left", padx=(20, 0))
        self.jurisdiction: str = "KY"
        self.ny_mode_btn = tk.Button(
            sw, text="New York — AD2 (NYSCEF)", command=lambda: self.switch_jurisdiction("NY"),
            relief="flat", bd=0, padx=12, pady=4, cursor="hand2", font=("Segoe UI", 9, "bold"),
        )
        self.ky_mode_btn = tk.Button(
            sw, text="Kentucky — C-Track (KYCOA / KYSUP)", command=lambda: self.switch_jurisdiction("KY"),
            relief="flat", bd=0, padx=12, pady=4, cursor="hand2", font=("Segoe UI", 9, "bold"),
        )
        self.ny_mode_btn.pack(side="left", padx=(0, 2))
        self.ky_mode_btn.pack(side="left")

        self.about_btn = tk.Button(
            head, text="ℹ  About", command=self.show_about,
            relief="flat", bd=0, padx=10, pady=3, cursor="hand2",
            bg=CARD, fg=MUTED, activebackground=LINE, activeforeground=TEXT,
            font=("Segoe UI", 9),
        )
        self.about_btn.pack(side="right", padx=(8, 0))

        self.clear_all_btn = tk.Button(
            head, text="🧹 Clear / Reset All", command=self.prompt_clear_dialog,
            relief="flat", bd=0, padx=10, pady=3, cursor="hand2",
            bg="#7f1d1d", fg="#fca5a5", activebackground="#991b1b", activeforeground="#ffffff",
            font=("Segoe UI", 9, "bold"),
        )
        self.clear_all_btn.pack(side="right", padx=(10, 0))

        self.status = tk.Label(head, text="Ready", bg=BG, fg=MUTED, font=("Segoe UI", 10))
        self.status.pack(side="right")

        self.banner = tk.Label(
            r, bg=ACCENT, fg="#111111", font=("Segoe UI", 11, "bold"), pady=9,
            text="Captcha in Edge — click 'I am human'. Scan will auto-resume.",
        )

        self.ny_frame = tk.Frame(r, bg=BG)
        self.ky_frame = KyScannerFrame(r, self)

        ny_paned = tk.PanedWindow(self.ny_frame, orient="horizontal", bg=LINE, sashwidth=6, bd=0, opaqueresize=True)
        ny_paned.pack(fill="both", expand=True, padx=20, pady=(4, 16))
        ny_left = tk.Frame(ny_paned, bg=BG)
        ny_right = tk.Frame(ny_paned, bg=BG)
        ny_paned.add(ny_left, minsize=380, width=540, stretch="never")
        ny_paned.add(ny_right, minsize=420, stretch="always")

        bar = tk.Frame(ny_left, bg=BG)
        bar.pack(fill="x", padx=(4, 10), pady=(4, 8))
        tk.Label(bar, text="Month", bg=BG, fg=MUTED).pack(side="left")
        self.month_var = tk.StringVar()
        self.month_box = ttk.Combobox(bar, textvariable=self.month_var, state="readonly", width=20, height=18)
        self.month_box.pack(side="left", padx=(8, 0))
        self.month_box.bind("<<ComboboxSelected>>", lambda _e: self._select_month())
        self.ny_load_btn = tk.Button(
            bar, text="Load", command=self.load_ny_month, relief="flat", bd=0, padx=12, pady=5, cursor="hand2",
            bg=CARD, fg=TEXT, activebackground=LINE, activeforeground=TEXT, disabledforeground="#555555",
            font=("Segoe UI", 9, "normal"),
        )
        self.ny_load_btn.pack(side="left", padx=(8, 0))
        self.count_lbl = tk.Label(bar, text="", bg=BG, fg=MUTED)
        self.count_lbl.pack(side="left", padx=12)

        wrap = tk.Frame(ny_left, bg=BG)
        wrap.pack(fill="both", expand=True, padx=(4, 10))
        cols = ("date", "file", "status", "hits")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="extended", height=8)
        for col, label, width in (
            ("date", "Date", 145),
            ("file", "Motion List", 205),
            ("status", "Status", 230),
            ("hits", "Hit Cases / Brief PDFs", 170),
        ):
            self.tree.heading(col, text=label, anchor="w")
            self.tree.column(col, width=width, minwidth=width, anchor="w")
        self.tree.tag_configure("done", foreground=ACCENT)
        self.tree.tag_configure("partial", foreground=TEXT)
        self.tree.tag_configure("pending", foreground=MUTED)
        self.tree.tag_configure("scanning", foreground="#f5b942")
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        scroll_x = ttk.Scrollbar(wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=scroll_x.set)
        scroll.pack(side="right", fill="y")
        scroll_x.pack(side="bottom", fill="x")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Control-a>", lambda _e: (self.tree.selection_set(self.tree.get_children()), "break")[1])
        self.tree.bind("<Control-A>", lambda _e: (self.tree.selection_set(self.tree.get_children()), "break")[1])
        self.tree.bind("<Double-1>", lambda _e: self.open_selected_motion_list_pdf())

        tk.Label(ny_left, text="1. Select date(s) in the table (optional)    2. Click Start Scan",
                 bg=BG, fg=MUTED, anchor="w", font=("Segoe UI", 9)).pack(fill="x", padx=(4, 10), pady=(6, 0))
        actions = tk.Frame(ny_left, bg=BG)
        actions.pack(fill="x", padx=(4, 10), pady=(6, 8))
        self.scan_btn = self._btn(actions, "▶  Start Scan", self.scan_start, primary=True)
        self.all_btn = self._btn(actions, "Start All Remaining", self.scan_remaining)
        self.stop_btn = self._btn(actions, "Stop", self.stop)
        self.pdf_btn = self._btn(actions, "Load PDF...", self.load_pdf)
        for b in (self.scan_btn, self.stop_btn):
            b.pack(side="left", padx=(0, 8))
        self.fresh_var = tk.BooleanVar(value=False)
        tk.Checkbutton(actions, text="Re-scan from scratch (verify)", variable=self.fresh_var, bg=BG, fg=MUTED,
                       selectcolor=CARD, activebackground=BG, activeforeground=TEXT,
                       highlightthickness=0).pack(side="left", padx=(4, 0))
        self.pdf_btn.pack(side="right")

        prog = tk.Frame(ny_left, bg=BG)
        prog.pack(fill="x", padx=(4, 10))
        self.bar = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.bar.pack(fill="x")
        self.prog_lbl = tk.Label(prog, text="", bg=BG, fg=MUTED, anchor="w")
        self.prog_lbl.pack(fill="x", pady=(4, 6))

        log_hdr = tk.Frame(ny_left, bg=BG)
        log_hdr.pack(fill="x", padx=(4, 10), pady=(4, 2))

        log_lbl = tk.Label(
            log_hdr, text="Scan Log", bg=BG, fg=TEXT, font=("Segoe UI", 10, "bold"), anchor="w",
            cursor="hand2",
        )
        log_lbl.pack(side="left")
        log_lbl.bind("<Button-1>", lambda _e: self.toggle_log_expand())

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

        logwrap = tk.Frame(ny_left, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        logwrap.pack(fill="x", padx=(4, 10), pady=(0, 4))
        self.log = tk.Text(logwrap, bg=CARD, fg=MUTED, bd=0, padx=12, pady=6, wrap="word",
                           font=("Consolas", 10), state="disabled", highlightthickness=0, height=4)
        self.log.bind("<Double-1>", lambda _e: self.toggle_log_expand())
        self.log.tag_configure("hit", foreground=ACCENT, font=("Consolas", 10, "bold"))
        self.log.tag_configure("warn", foreground=TEXT, font=("Consolas", 10, "bold"))
        self.log.tag_configure("head", foreground=TEXT)
        self.log.tag_configure("dim", foreground=MUTED)
        sb = ttk.Scrollbar(logwrap, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

        ny_hdr = tk.Frame(ny_right, bg=BG)
        ny_hdr.pack(fill="x", padx=(10, 4), pady=(4, 4))
        self.ny_hit_title_lbl = tk.Label(
            ny_hdr, text="Hit-List", bg=BG, fg=ACCENT, font=("Segoe UI", 11, "bold"), anchor="w",
        )
        self.ny_hit_title_lbl.pack(side="left")
        self.hits = HitListView(
            ny_right,
            lambda: (self.active_month_key(), self.month_opts),
            self.open_month_report,
            download_handler=self.download_ny_briefs,
            open_folder_handler=self.open_ny_downloads_folder,
        )
        self.hits.pack(fill="both", expand=True, padx=(10, 4))
        self._set_buttons()
        self.switch_jurisdiction("KY")

    def switch_jurisdiction(self, mode: str | None = None) -> None:
        target = mode if mode in ("NY", "KY") else getattr(self, "jurisdiction", "KY") or "KY"
        self.jurisdiction = target
        self.ny_frame.pack_forget()
        self.ky_frame.pack_forget()

        if self.jurisdiction == "KY":
            self.ky_frame.pack(fill="both", expand=True)
            self.ny_mode_btn.configure(bg=CARD, fg=MUTED)
            self.ky_mode_btn.configure(bg=ACCENT, fg="#111111")
            self.status.configure(text="Kentucky Ready")
            if not getattr(self.ky_frame, "search_rows", None) and not getattr(self.ky_frame, "records", None) and not self.ky_frame.busy:
                if "unittest" not in sys.modules and not getattr(self, "remote_blocked", False):
                    self.root.after(150, self.ky_frame.load_month)
        else:
            self.ny_frame.pack(fill="both", expand=True)
            self.ny_mode_btn.configure(bg=ACCENT, fg="#111111")
            self.ky_mode_btn.configure(bg=CARD, fg=MUTED)
            if not self._ny_initialized and not self.grouped:
                STOP_EVENT.clear()
                self._ny_initialized = True
                self.status.configure(text="Starting Edge...")
                self.worker.commands.put(("init_ny",))
            else:
                self.status.configure(text="Ready")

    # ---------- state ----------
    def _set_buttons(self) -> None:
        if getattr(self, "remote_blocked", False):
            self.scan_btn.configure(
                text="❌ Suspended", bg=CARD, fg="#ef4444", disabledforeground="#ef4444", state="disabled"
            )
            self.all_btn.configure(state="disabled")
            self.pdf_btn.configure(state="disabled")
            self.stop_btn.configure(state="disabled")
            if hasattr(self, "ny_load_btn"):
                self.ny_load_btn.configure(state="disabled")
            if hasattr(self, "hits") and hasattr(self.hits, "dl_all_btn"):
                self.hits.dl_all_btn.configure(state="disabled")
            if hasattr(self, "ky_frame"):
                if hasattr(self.ky_frame, "scan_btn"):
                    self.ky_frame.scan_btn.configure(
                        text="❌ Suspended", bg=CARD, fg="#ef4444", disabledforeground="#ef4444", state="disabled"
                    )
                if hasattr(self.ky_frame, "load_btn"):
                    self.ky_frame.load_btn.configure(state="disabled")
                if hasattr(self.ky_frame, "dl_all_btn"):
                    self.ky_frame.dl_all_btn.configure(state="disabled")
            return

        ready = bool(self.month_opts)
        if self.busy:
            action = getattr(self, "active_action", None)
            if action == "load":
                if hasattr(self, "ny_load_btn"):
                    self.ny_load_btn.configure(
                        text="⏳ Loading...", bg="#f59e0b", fg="#111111", disabledforeground="#111111",
                        font=("Segoe UI", 9, "bold"), state="disabled",
                    )
            elif action == "download":
                self.scan_btn.configure(
                    text="▶  Start Scan", bg=CARD, fg=TEXT, disabledforeground="#555555",
                    font=("Segoe UI", 9, "bold"), state="disabled"
                )
                if hasattr(self, "hits") and hasattr(self.hits, "dl_all_btn"):
                    self.hits.dl_all_btn.configure(
                        text="⏳ Downloading...", bg="#f59e0b", fg="#111111", disabledforeground="#111111",
                        state="disabled"
                    )
            else:
                self.scan_btn.configure(
                    text="⏳ Scanning...", bg="#f59e0b", fg="#111111", disabledforeground="#111111",
                    font=("Segoe UI", 9, "bold"), state="disabled"
                )
            self.all_btn.configure(state="disabled")
            self.pdf_btn.configure(state="disabled")
            if hasattr(self, "ny_load_btn") and action != "load":
                self.ny_load_btn.configure(
                    text="Load", bg=CARD, fg="#555555", disabledforeground="#555555",
                    font=("Segoe UI", 9, "normal"), state="disabled",
                )
            if hasattr(self, "clear_all_btn"):
                self.clear_all_btn.configure(state="disabled")
            self.stop_btn.configure(
                text="⏹  Stop", bg="#ef4444", fg="#ffffff", font=("Segoe UI", 9, "bold"), state="normal"
            )
            self.month_box.configure(state="disabled")
        else:
            scan_state = "normal" if ready else "disabled"
            self.scan_btn.configure(
                text="▶  Start Scan", bg=ACCENT, fg="#111111", disabledforeground="#555555",
                font=("Segoe UI", 9, "bold"), state=scan_state
            )
            self.all_btn.configure(state=scan_state)
            self.pdf_btn.configure(state=scan_state)
            if hasattr(self, "ny_load_btn"):
                self.ny_load_btn.configure(
                    text="Load", bg=CARD, fg=TEXT, disabledforeground="#555555",
                    font=("Segoe UI", 9, "normal"), state="normal",
                )
            if hasattr(self, "clear_all_btn"):
                self.clear_all_btn.configure(state="normal")
            self.stop_btn.configure(
                text="Stop", bg=CARD, fg=TEXT, font=("Segoe UI", 9, "normal"), state="disabled"
            )
            self.month_box.configure(state="readonly")
            if hasattr(self, "hits"):
                self.hits._fill()

    def download_ny_briefs(self, items: list[dict]) -> None:
        if self.busy:
            messagebox.showinfo("Scanner Busy", "Please wait for current operation to finish.")
            return
        if not items:
            return
        self.active_action = "download"
        self.busy = True
        self._set_buttons()
        self.status.configure(text=f"Downloading {len(items)} PDF(s)...")
        self.prog_lbl.configure(text=f"Starting download of {len(items)} PDF(s)...")
        month_label = self.active_month_key()
        self.worker.commands.put(("download_briefs", items, month_label))

    def open_ny_downloads_folder(self, month_key: str | None = None) -> None:
        from gui_hits import ny_downloads_dir
        dest = ny_downloads_dir(month_key or self.active_month_key())
        try:
            os.startfile(str(dest))
        except Exception:
            webbrowser.open(dest.as_uri())

    def open_selected_motion_list_pdf(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        try:
            idx = int(sel[0])
            opt = self.month_opts[idx]
        except Exception:
            return
        from gui_hits import ny_motion_lists_dir
        from main import _run_dir_for_option

        ml_dir = ny_motion_lists_dir(self.active_month_key())
        target = ml_dir / opt.filename
        if not target.exists():
            leg_target = _run_dir_for_option(opt) / opt.filename
            if leg_target.exists():
                try:
                    shutil.copy2(leg_target, target)
                except Exception:
                    pass
                target = leg_target

        if target.exists():
            try:
                os.startfile(str(target))
                self.status.configure(text=f"Opened Motion List: {opt.filename}")
            except Exception as exc:
                self.status.configure(text=f"Error opening Motion List: {exc}")
        else:
            self.status.configure(text=f"Motion List {opt.filename} not downloaded yet. Click Start to scan.")

    def active_month_key(self) -> str:
        val = self.month_var.get()
        return self._label_to_month.get(val, val)

    def _select_month(self) -> None:
        self.month_opts = self.grouped.get(self.active_month_key(), [])
        self.count_lbl.configure(text=f"{len(self.month_opts)} Motion Lists")
        self.refresh_tree()
        self._set_buttons()

    def load_ny_month(self) -> None:
        if self.busy:
            return
        if getattr(self, "remote_blocked", False):
            messagebox.showerror(
                "Access Blocked",
                getattr(self, "remote_blocked_msg", "This application is currently suspended."),
            )
            return

        if hasattr(self, "ny_load_btn"):
            self.ny_load_btn.configure(
                text="⏳ Loading...", bg="#f59e0b", fg="#111111", disabledforeground="#111111",
                font=("Segoe UI", 9, "bold"), state="disabled",
            )
            self.root.update_idletasks()

        if self.grouped:
            def _finish():
                self._select_month()
                m = self.month_var.get() or "Selected Month"
                self.status.configure(text=f"Loaded {m}")
                if hasattr(self, "ny_load_btn"):
                    self.ny_load_btn.configure(
                        text="Load", bg=CARD, fg=TEXT, disabledforeground="#555555",
                        font=("Segoe UI", 9, "normal"), state="normal",
                    )
            self.root.after(350, _finish)
            return

        self.busy = True
        self.active_action = "load"
        STOP_EVENT.clear()
        self._ny_initialized = True
        self.status.configure(text="Loading NY Handdowns Calendar...")
        self.worker.commands.put(("init_ny",))

    def refresh_tree(self) -> None:
        from gui_hits import ny_motion_lists_dir
        from main import _run_dir_for_option
        from monthly_report import inspect_option_progress

        # Ensure downloaded motion list PDFs are mirrored into the organized Motion Lists folder
        try:
            ml_dir = ny_motion_lists_dir(self.active_month_key())
            for opt in self.month_opts:
                src_pdf = _run_dir_for_option(opt) / opt.filename
                if src_pdf.exists():
                    dest_pdf = ml_dir / opt.filename
                    if not dest_pdf.exists() or dest_pdf.stat().st_size != src_pdf.stat().st_size:
                        shutil.copy2(src_pdf, dest_pdf)
        except Exception:
            pass

        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        month_hits = 0
        month_pdfs = 0
        active_iid = getattr(self, "active_scan_iid", None)
        for i, opt in enumerate(self.month_opts):
            iid_str = str(i)
            if getattr(self, "busy", False) and active_iid is not None and iid_str == active_iid:
                hits_str = f"{self.run_hits} cases ({self.run_pdfs} PDFs)" if getattr(self, "run_hits", 0) else ""
                scan_lbl = getattr(self, "_active_scan_status", "⏳ Scanning...")
                date_words = format_date_words(opt.date_iso or opt.label)
                self.tree.insert("", "end", iid=iid_str, tags=("scanning",),
                                 values=(date_words, opt.filename, scan_lbl, hits_str))
                continue
            prog = inspect_option_progress(_run_dir_for_option(opt))
            month_hits += prog.hits
            month_pdfs += prog.brief_pdfs
            status, hits, tag = format_progress(prog)
            date_words = format_date_words(opt.date_iso or opt.label)
            self.tree.insert("", "end", iid=iid_str, tags=(tag,),
                             values=(date_words, opt.filename, status, hits))
        self.count_lbl.configure(
            text=f"{len(self.month_opts)} Motion Lists   •   {month_hits} hit case(s)   •   {month_pdfs} Brief PDF(s) to download"
        )
        for iid in selected:
            if self.tree.exists(iid):
                self.tree.selection_add(iid)
        if active_iid and self.tree.exists(active_iid):
            self.tree.selection_set(active_iid)
            self.tree.see(active_iid)
        elif not self.tree.selection() and self.tree.get_children():
            first_pending = next(
                (iid for iid in self.tree.get_children() if "done" not in self.tree.item(iid, "tags")),
                self.tree.get_children()[0],
            )
            self.tree.selection_set(first_pending)
        self.hits.refresh()
        total_hit_rows = len(self.hits.rows)
        total_hit_pdfs = sum(r.pdf_count for r in self.hits.rows)
        tab_label = f"Hit-List ({total_hit_rows} cases • {total_hit_pdfs} Brief PDFs)" if total_hit_rows else "Hit-List"
        self.ny_hit_title_lbl.configure(text=tab_label)

    # ---------- commands ----------
    def _start(self, opts: list, fresh: bool = False) -> None:
        if getattr(self, "remote_blocked", False):
            messagebox.showerror("Access Blocked", getattr(self, "remote_blocked_msg", "This application is currently suspended."))
            return
        if not opts:
            messagebox.showinfo("Nothing to scan", "All selected Motion Lists are already Done.")
            return
        self.active_scan_opts = list(opts)
        self.worker.commands.put(("scan", opts, self.month_opts, self.active_month_key(), fresh))

    def scan_start(self) -> None:
        sel = self.tree.selection()
        if sel:
            picked = [self.month_opts[int(i)] for i in sel]
        else:
            from main import _run_dir_for_option
            from monthly_report import inspect_option_progress
            picked = [o for o in self.month_opts
                      if not inspect_option_progress(_run_dir_for_option(o)).is_complete]
            if not picked:
                picked = list(self.month_opts)
        self._start(picked, fresh=self.fresh_var.get())

    def scan_selected(self) -> None:
        self.scan_start()

    def scan_remaining(self) -> None:
        self.scan_start()

    def stop(self) -> None:
        STOP_EVENT.set()
        self.status.configure(text="Stopping after current case...")
        self.scan_btn.configure(text="⏹  Stopping...", bg="#eab308", fg="#111111")
        self.stop_btn.configure(text="Stopping...", state="disabled")

    def load_pdf(self) -> None:
        path = filedialog.askopenfilename(title="Motion List PDF", filetypes=[("PDF", "*.pdf")])
        if path:
            self.worker.commands.put(("local_pdf", path))

    def open_month_report(self) -> None:
        from main import _run_dir_for_option
        from monthly_report import write_monthly_reports

        out = write_monthly_reports(OUTPUT_DIR, self.active_month_key(), self.month_opts, _run_dir_for_option)
        webbrowser.open(out["html"].resolve().as_uri())

    def show_about(self) -> None:
        dlg = tk.Toplevel(self.root)
        dlg.title("About BPM Court Scanner")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        ico_file = find_asset("app_icon.ico")
        png_file = find_asset("app_icon.png")
        if ico_file.is_file():
            try:
                dlg.iconbitmap(str(ico_file))
                dlg.iconbitmap(default=str(ico_file))
            except Exception:
                pass
            apply_windows_taskbar_icon(dlg, str(ico_file))
        if png_file.is_file():
            try:
                if not hasattr(self, "_app_icon_img"):
                    self._app_icon_img = tk.PhotoImage(file=str(png_file))
                dlg.iconphoto(False, self._app_icon_img)
            except Exception:
                pass
        dlg.transient(self.root)
        dlg.grab_set()

        w, h = 480, 270
        px = max(10, self.root.winfo_x() + (self.root.winfo_width() - w) // 2)
        py = max(10, self.root.winfo_y() + (self.root.winfo_height() - h) // 2)
        dlg.geometry(f"{w}x{h}+{px}+{py}")

        card = tk.Frame(dlg, bg=CARD, padx=24, pady=20, highlightbackground=LINE, highlightthickness=1)
        card.pack(fill="both", expand=True, padx=14, pady=14)

        tk.Label(
            card, text="BPM Court Scanner", bg=CARD, fg=TEXT, font=("Segoe UI", 16, "bold")
        ).pack(anchor="w")

        tk.Label(
            card, text="Automated Brief & Docket Extraction Platform", bg=CARD, fg=ACCENT, font=("Segoe UI", 9, "bold")
        ).pack(anchor="w", pady=(2, 12))

        author = verify_app_integrity()
        details = (
            "Jurisdictions:\n"
            "  • New York — AD2 (NYSCEF)\n"
            "  • Kentucky — C-Track (KYCOA / KYSUP)\n\n"
            f"Developer:   {author}\n"
            f"Version:     {__version__} (Dual-Jurisdiction)\n"
            f"Copyright:   © 2026 {author}. All rights reserved."
        )
        tk.Label(
            card, text=details, bg=CARD, fg=MUTED, font=("Consolas", 9), justify="left", anchor="w"
        ).pack(fill="x", pady=(0, 14))

        close_btn = tk.Button(
            card, text="Close", command=dlg.destroy, relief="flat", bd=0, padx=20, pady=5,
            cursor="hand2", bg=LINE, fg=TEXT, activebackground=ACCENT, activeforeground="#111111",
            font=("Segoe UI", 9, "bold"),
        )
        close_btn.pack(side="right")
        close_btn.focus_set()
        dlg.bind("<Escape>", lambda _e: dlg.destroy())

    def prompt_clear_dialog(self, default_scope: str = "month") -> None:
        if self.busy or getattr(self.ky_frame, "busy", False):
            messagebox.showwarning(
                "Scanner Busy",
                "A scan or download task is currently running.\nPlease click Stop first before clearing or resetting data.",
                parent=self.root,
            )
            return
        from clear_manager import ClearResetModal
        ClearResetModal(self.root, self, default_scope=default_scope)

    def on_ny_data_cleared(self, month_key: str | None = None) -> None:
        from gui_hits import DoneStore
        self.hits.store = DoneStore()
        self.hits.refresh()
        self.refresh_tree()
        self._clear_log()
        self.bar.configure(value=0)
        self.prog_lbl.configure(text="")
        lbl = f"Month {month_key}" if month_key else "All New York"
        self.status.configure(text=f"Reset complete: {lbl} cleared")

    # ---------- events ----------
    def toggle_log_expand(self) -> None:
        self.log_expanded = not getattr(self, "log_expanded", False)
        if self.log_expanded:
            self.log.configure(height=14)
            self.log_expand_btn.configure(text="⤡ Collapse", fg=ACCENT)
        else:
            self.log.configure(height=4)
            self.log_expand_btn.configure(text="⤢ Expand", fg=TEXT)

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _log(self, text: str, tag: str = "dim") -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _handle_line(self, line: str) -> None:
        if "USER ACTION REQUIRED" in line:
            self.banner.pack(fill="x", padx=24, pady=(0, 6), after=self.root.winfo_children()[0])
            self.root.bell()
            self._log("Captcha detected — solve it in the Edge window.", "warn")
            return
        if "[RESUMED]" in line:
            self.banner.pack_forget()
            self._log("Captcha cleared, resuming.", "dim")
            return
        m = CASE_LINE.match(line)
        if m:
            idx, total, code, case_no, caption, extra = m.groups()
            self.banner.pack_forget()
            self.bar.configure(value=100 * int(idx) / max(int(total), 1))
            num = f"[{int(idx):02d}/{int(total):02d}]"
            name_col = f"{caption[:40]:<40}  " if caption else ""
            if code == "HIT":
                self.run_hits += 1
                m_pdf = re.match(r"^(\d+)\s+PDF", extra)
                n_pdfs = int(m_pdf.group(1)) if m_pdf else (extra.count(",") + 1 if extra else 1)
                self.run_pdfs += n_pdfs
                self._log(f"{num} HIT (HAS BRIEF)   {case_no}  {name_col}—  Open & download ({extra})", "hit")
            elif code == "SEAL":
                self._log(f"{num} SKIP (SEALED)     {case_no}  {name_col}—  {extra}", "warn")
            elif code == "WARN":
                self._log(f"{num} NEEDS RECHECK     {case_no}  {name_col}—  {extra}", "warn")
            elif code == "NOBR":
                self._log(
                    f"{num} SKIP (NO BRIEF)   {case_no}  {name_col}—  Case exists in NYSCEF, but 0 briefs filed",
                    "dim",
                )
            else:
                self._log(
                    f"{num} SKIP (NO RECORD)  {case_no}  {name_col}—  Not in NYSCEF 2nd Dept (criminal/paper appeal)",
                    "dim",
                )
            clean_cap = " ".join((caption or "").split())[:35]
            cap_snippet = f" • {clean_cap}" if clean_cap else ""
            date_prefix = f"{self.active_scan_date} • " if getattr(self, "active_scan_date", None) else ""
            self.prog_lbl.configure(
                text=f"{date_prefix}Case {int(idx):02d}/{int(total):02d} • {case_no}{cap_snippet}   •   {self.run_hits} hit case(s) ({self.run_pdfs} Brief PDF(s))"
            )
            self.status.configure(
                text=f"Scanning {getattr(self, 'active_scan_date', '') or 'Case'}: {case_no} ({int(idx):02d}/{int(total):02d})..."
            )

            active_iid = getattr(self, "active_scan_iid", None)
            if active_iid is not None and self.tree.exists(active_iid):
                self._active_scan_status = f"⏳ Scanning {int(idx):02d}/{int(total):02d}"
                hits_str = f"{self.run_hits} cases ({self.run_pdfs} PDFs)" if self.run_hits else ""
                date_txt = getattr(self, "active_scan_date", "")
                fn_txt = getattr(self, "active_scan_filename", "")
                self.tree.item(
                    active_iid,
                    values=(date_txt, fn_txt, self._active_scan_status, hits_str),
                    tags=("scanning",),
                )

            if int(idx) % 5 == 0:
                self.refresh_tree()
            return
        if line.startswith("[VERIFY]"):
            self._log(line, "warn" if ("CHANGED" in line or "->" in line) else "hit")
            return
        if line.startswith("  -> ") or line.startswith("    [PDF "):
            self._log(line, "hit")
            return
        if line.startswith("[Date"):
            self.status.configure(text=line[:75])
            if "[1/3]" in line:
                self.bar.configure(value=0)
            self._log(line, "head")

            # Extract active motion list option from date line
            m_batch = re.search(r"\[Date\s+(\d+)/(\d+)\]", line)
            if m_batch:
                date_pos = int(m_batch.group(1)) - 1
                opts = getattr(self, "active_scan_opts", [])
                if 0 <= date_pos < len(opts):
                    opt = opts[date_pos]
                    self.active_scan_filename = opt.filename
                    self.active_scan_date = format_date_words(opt.date_iso or opt.label)
                    for i, o in enumerate(self.month_opts):
                        if o.filename == opt.filename:
                            self.active_scan_iid = str(i)
                            break
            m_for = re.search(r"for\s+(.*?)\s+\(([^)]+\.pdf)\)", line, re.IGNORECASE)
            if m_for:
                self.active_scan_date = m_for.group(1).strip()
                self.active_scan_filename = m_for.group(2).strip()
                for i, o in enumerate(self.month_opts):
                    if o.filename == self.active_scan_filename:
                        self.active_scan_iid = str(i)
                        break

            active_iid = getattr(self, "active_scan_iid", None)
            if active_iid is not None and self.tree.exists(active_iid):
                self._active_scan_status = "⏳ Starting..."
                self.tree.item(
                    active_iid,
                    values=(
                        getattr(self, "active_scan_date", ""),
                        getattr(self, "active_scan_filename", ""),
                        self._active_scan_status,
                        "",
                    ),
                    tags=("scanning",),
                )
                self.tree.selection_set(active_iid)
                self.tree.see(active_iid)
            return
        if line.strip():
            self._log(line, "dim")

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._handle_line(payload)
                elif kind == "status":
                    self.status.configure(text=payload)
                elif kind == "calendar":
                    self.busy = False
                    self.active_action = None
                    self.grouped = payload
                    months = list(payload.keys())
                    self._label_to_month = {format_month_words(m): m for m in months}
                    labels = [format_month_words(m) for m in months]
                    self.month_box.configure(values=labels)
                    self.month_var.set(labels[0])
                    self.status.configure(text="Ready")
                    self._select_month()
                    self._set_buttons()
                elif kind == "busy":
                    self.busy, self.run_hits, self.run_pdfs = True, 0, 0
                    if getattr(self, "active_action", None) == "download":
                        self.status.configure(text="Downloading...")
                    else:
                        self.status.configure(text="Scanning...")
                    self._set_buttons()
                elif kind == "dl_prog":
                    cur, tot, msg = payload
                    self.bar.configure(value=100 * cur / max(tot, 1))
                    self.prog_lbl.configure(text=msg)
                    self.status.configure(text=msg)
                    if hasattr(self, "hits") and hasattr(self.hits, "note"):
                        self.hits.note.configure(text=msg)
                    if hasattr(self, "hits") and hasattr(self.hits, "dl_all_btn"):
                        self.hits.dl_all_btn.configure(
                            text=f"⏳ Downloading ({cur}/{tot})...",
                            bg="#f59e0b",
                            fg="#111111",
                            disabledforeground="#111111",
                            state="disabled",
                        )
                elif kind == "ny_case_done":
                    self.hits.store.set_many([payload], True)
                    self.hits._fill()
                elif kind == "dl_done":
                    downloaded, total, folder = payload
                    self.status.configure(text=f"Downloaded {downloaded}/{total} PDF(s)")
                    self.prog_lbl.configure(text=f"Download complete: {downloaded}/{total} Brief PDF(s) saved to {folder}")
                    if hasattr(self, "hits") and hasattr(self.hits, "note"):
                        self.hits.note.configure(text=f"✔ Downloaded {downloaded}/{total} PDF(s)")
                    if hasattr(self, "hits") and hasattr(self.hits, "dl_all_btn"):
                        self.hits.dl_all_btn.configure(
                            text="⬇ Download All PDFs",
                            bg=ACCENT,
                            fg="#111111",
                            state="normal",
                        )
                    self.hits.refresh()
                elif kind == "idle":
                    self.busy = False
                    self.active_action = None
                    self.active_scan_iid = None
                    self.active_scan_date = None
                    self.active_scan_filename = None
                    self._active_scan_status = None
                    self.active_scan_opts = []
                    self.banner.pack_forget()
                    self.status.configure(text="Ready")
                    self.refresh_tree()
                    self._set_buttons()
                    if hasattr(self, "hits"):
                        self.hits.refresh()
                elif kind == "remote_guard":
                    allowed, code, msg, status = payload
                    if not allowed:
                        self.remote_blocked = True
                        self.remote_blocked_msg = msg
                        self._set_buttons()
                        if code == "KILL_SWITCH":
                            self.status.configure(text="❌ Access Suspended", fg="#ef4444")
                            messagebox.showerror("Access Suspended", msg, parent=self.root)
                        elif code == "MANDATORY_UPDATE":
                            self.status.configure(text="⚠️ Update Required", fg="#f59e0b")
                            if status.download_url:
                                prompt_txt = (
                                    f"A mandatory update (v{status.latest_version}) is required to continue using BPM Court Scanner.\n"
                                    f"Your current version: v{__version__}\n\n"
                                    f"Would you like to automatically download and apply this update now?"
                                )
                                if messagebox.askyesno("Update Required", prompt_txt, parent=self.root):
                                    self._start_inapp_update(status, mandatory=True)
                                else:
                                    self.root.destroy()
                                    return
                            else:
                                messagebox.showerror("Update Required", f"A mandatory update (v{status.latest_version}) is required to continue using BPM Court Scanner.", parent=self.root)
                                self.root.destroy()
                                return
                    else:
                        was_blocked = getattr(self, "remote_blocked", False)
                        self.remote_blocked = False
                        self.remote_blocked_msg = ""
                        if was_blocked:
                            target_text = "Kentucky Ready" if getattr(self, "jurisdiction", "KY") == "KY" else "Ready"
                            self.status.configure(text=target_text, fg=TEXT)
                            self._set_buttons()
                        if code == "OPTIONAL_UPDATE":
                            self._show_update_pill(status)
                        announcement_text = getattr(status, "announcement", "").strip()
                        if announcement_text:
                            self._log(f"Notice: {announcement_text}", "hit")
                    if "unittest" not in sys.modules and self.root.winfo_exists():
                        interval_ms = 15000 if not allowed else 300000
                        self.root.after(
                            interval_ms,
                            lambda: threading.Thread(target=self._check_remote_auth, daemon=True).start(),
                        )
                elif kind == "error":
                    self._log(payload, "warn")
                    messagebox.showerror("Scan error", payload.strip().splitlines()[-1])
                elif kind == "fatal":
                    self._ny_initialized = False
                    self.status.configure(text="Failed to start (click NY to retry)")
                    self._log(payload, "warn")
                    messagebox.showerror("Cannot start", payload.strip().splitlines()[-1])
                elif kind == "closed":
                    try:
                        self.root.destroy()
                    except Exception:
                        pass
                    os._exit(0)
        except queue.Empty:
            pass
        except Exception:
            traceback.print_exc()
        finally:
            if self._after_id:
                try:
                    self.root.after_cancel(self._after_id)
                except Exception:
                    pass
            if self.root.winfo_exists():
                self._after_id = self.root.after(100, self._poll)

    def _check_remote_auth(self) -> None:
        try:
            from remote_guard import check_app_authorization, fetch_remote_status
            status = fetch_remote_status()
            allowed, code, msg = check_app_authorization(status, __version__)
            self.events.put(("remote_guard", (allowed, code, msg, status)))
        except Exception:
            pass

    def _show_update_pill(self, status) -> None:
        if getattr(self, "_update_pill_btn", None) is not None:
            return
        if not getattr(status, "download_url", ""):
            return
        hdr = getattr(self, "header_frame", None)
        if not hdr:
            return
        self._update_pill_btn = tk.Button(
            hdr,
            text=f"🚀 Update v{status.latest_version} Available",
            command=lambda: self._prompt_auto_update(status),
            relief="flat",
            bd=0,
            padx=10,
            pady=3,
            cursor="hand2",
            bg="#1e3a8a",
            fg="#93c5fd",
            activebackground="#2563eb",
            activeforeground="#ffffff",
            font=("Segoe UI", 9, "bold"),
        )
        self._update_pill_btn.pack(side="left", padx=(15, 0))

    def _prompt_auto_update(self, status) -> None:
        if not getattr(status, "download_url", ""):
            return
        ans = messagebox.askyesno(
            "Update Available",
            f"BPM Court Scanner v{status.latest_version} is available!\n\n"
            f"Would you like to automatically download and install this update now?\n"
            f"(The app will download in the background and restart seamlessly).",
            parent=self.root,
        )
        if ans:
            self._start_inapp_update(status)

    def _start_inapp_update(self, status, mandatory: bool = False) -> None:
        from updater import apply_update_and_restart, download_update_binary

        up_win = tk.Toplevel(self.root)
        up_win.title("Updating BPM Court Scanner")
        up_win.configure(bg="#111111")
        up_win.geometry("460x220")
        up_win.resizable(False, False)
        up_win.transient(self.root)
        up_win.grab_set()

        try:
            x = self.root.winfo_x() + (self.root.winfo_width() // 2) - 230
            y = self.root.winfo_y() + (self.root.winfo_height() // 2) - 110
            up_win.geometry(f"+{max(0, x)}+{max(0, y)}")
        except Exception:
            pass

        tk.Label(
            up_win,
            text=f"Downloading Update v{status.latest_version}",
            bg="#111111",
            fg="#4ade80",
            font=("Segoe UI", 12, "bold"),
        ).pack(pady=(20, 4))

        status_lbl = tk.Label(
            up_win,
            text="Connecting to update server...",
            bg="#111111",
            fg="#8a8a8a",
            font=("Segoe UI", 9),
        )
        status_lbl.pack(pady=(0, 15))

        prog_bar = ttk.Progressbar(up_win, orient="horizontal", length=380, mode="determinate")
        prog_bar.pack(pady=(0, 10))

        detail_lbl = tk.Label(
            up_win,
            text="Please wait while the update packages...",
            bg="#111111",
            fg="#ededed",
            font=("Segoe UI", 9),
        )
        detail_lbl.pack(pady=(0, 15))

        stop_event = threading.Event()

        def _on_win_close():
            stop_event.set()
            up_win.destroy()
            if mandatory:
                self.root.destroy()

        up_win.protocol("WM_DELETE_WINDOW", _on_win_close)

        def _update_worker():
            target_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else BASE_DIR / "dist"
            new_exe = target_dir / "BPM_Court_Scanner.new.exe"

            def _on_progress(cur, tot):
                if not up_win.winfo_exists():
                    return
                pct = round(100 * cur / max(tot, 1)) if tot > 0 else 0
                cur_mb = cur / (1024 * 1024)
                tot_mb = tot / (1024 * 1024) if tot > 0 else 0
                up_win.after(0, lambda: (
                    prog_bar.configure(value=pct),
                    status_lbl.configure(text=f"Downloading: {pct}% complete"),
                    detail_lbl.configure(text=f"{cur_mb:.1f} MB of {tot_mb:.1f} MB downloaded"),
                ))

            ok = download_update_binary(
                status.download_url,
                new_exe,
                progress_callback=_on_progress,
                stop_event=stop_event,
            )

            if not up_win.winfo_exists():
                return

            if ok:
                up_win.after(0, lambda: (
                    status_lbl.configure(text="✔ Download Complete!", fg="#4ade80"),
                    detail_lbl.configure(text="Restarting into new version in 2 seconds..."),
                    prog_bar.configure(value=100),
                ))
                time.sleep(1.8)
                try:
                    apply_update_and_restart(new_exe)
                except Exception as exc:
                    up_win.after(0, lambda: messagebox.showerror("Update Error", f"Failed to restart app: {exc}", parent=self.root))
            else:
                if not stop_event.is_set():
                    def _show_fail():
                        status_lbl.configure(text="❌ Download Failed", fg="#ef4444")
                        detail_lbl.configure(text="Could not complete download. Check connection.")
                        messagebox.showwarning("Update Notice", "In-app update could not be completed. The application will close.", parent=self.root)
                        if mandatory:
                            self.root.destroy()
                    up_win.after(0, _show_fail)

        threading.Thread(target=_update_worker, daemon=True).start()

    def on_close(self) -> None:
        STOP_EVENT.set()
        for aid in getattr(self, "_after_icon_ids", []):
            try:
                self.root.after_cancel(aid)
            except Exception:
                pass
        try:
            self.worker.commands.put(None)
        except Exception:
            pass
        self.root.withdraw()  # hidden until the worker has shut Edge down ("closed" event)
        # Fallback: force exit after 2.5s if worker shutdown is stalled
        self.root.after(2500, self._force_exit)

    def _force_exit(self) -> None:
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)


def main() -> None:
    from updater import cleanup_old_version
    cleanup_old_version()

    from gui_worker import QueueWriter, ScannerWorker

    events: queue.Queue = queue.Queue()
    sys.stdout = sys.stderr = QueueWriter(events)
    root = tk.Tk()
    worker = ScannerWorker(events)
    app = App(root, events, worker)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    worker.start()
    root.mainloop()


if __name__ == "__main__":
    main()
