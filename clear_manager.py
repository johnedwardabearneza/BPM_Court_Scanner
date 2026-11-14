"""
Clear and Reset Manager for BPM Court Scanner.

Handles safe resetting and clearing of:
- Scanned records, checkpoints, and reports
- "Done" tracking status
- Downloaded PDF briefs
Across:
- Active Month (NY or KY)
- Jurisdiction (All NY or All KY)
- Factory Reset (Complete clean slate)
"""

from __future__ import annotations

import calendar
import json
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tkinter as tk
from tkinter import messagebox
from typing import Any

from config import DOWNLOADS_DIR, DOWNLOADS_KY_DIR, DOWNLOADS_NY_DIR, OUTPUT_DIR
from ui_theme import format_month_words

BG, CARD, LINE = "#111111", "#1a1a1a", "#2a2a2a"
TEXT, MUTED, ACCENT = "#ededed", "#8a8a8a", "#4ade80"
DANGER = "#dc2626"
DANGER_BG = "#7f1d1d"


def _on_rm_error(func: Any, path: str, _exc_info: Any) -> None:
    """Error handler for shutil.rmtree on Windows (handles read-only files)."""
    try:
        os_path = Path(path)
        if os_path.exists():
            os_path.chmod(stat.S_IWRITE | stat.S_IREAD)
            func(path)
    except Exception:
        pass


def remove_path_safely(path: Path) -> tuple[int, int]:
    """
    Safely delete a file or directory tree.
    Returns (dirs_deleted, files_deleted).
    """
    dirs_cnt = 0
    files_cnt = 0
    if not path.exists():
        return dirs_cnt, files_cnt

    if path.is_file():
        try:
            path.chmod(stat.S_IWRITE | stat.S_IREAD)
            path.unlink()
            files_cnt += 1
        except Exception:
            pass
        return dirs_cnt, files_cnt

    # Directory
    try:
        for item in path.glob("**/*"):
            if item.is_file():
                files_cnt += 1
            elif item.is_dir():
                dirs_cnt += 1
        shutil.rmtree(path, onerror=_on_rm_error)
        dirs_cnt += 1
    except Exception:
        pass
    return dirs_cnt, files_cnt


def _clean_team_dashboard_db(scopes: list[str] | None = None, jurisdictions: list[str] | None = None) -> None:
    """Clean up team_dashboard.db for reset scopes without crashing if DB is locked."""
    db_file = OUTPUT_DIR / "team_dashboard.db"
    if not db_file.exists():
        return
    try:
        conn = sqlite3.connect(str(db_file), timeout=5.0)
        cur = conn.cursor()
        if scopes:
            placeholders = ",".join("?" for _ in scopes)
            cur.execute(f"DELETE FROM done_status WHERE scope IN ({placeholders})", scopes)
            cur.execute(f"DELETE FROM published_batches WHERE scope IN ({placeholders})", scopes)
        if jurisdictions:
            placeholders = ",".join("?" for _ in jurisdictions)
            cur.execute(f"DELETE FROM published_batches WHERE jurisdiction IN ({placeholders})", jurisdictions)
            for j in jurisdictions:
                cur.execute("DELETE FROM done_status WHERE scope LIKE ?", (f"{j}_%",))
        conn.commit()
        conn.close()
    except Exception:
        pass


def clear_ny_month(year_month: str, delete_pdfs: bool = True) -> dict[str, int]:
    """
    Reset New York scan data for a specific year_month (e.g. '2026-11').
    Clears:
    - Monthly combined reports (_MONTH_{year_month})
    - All date run directories belonging to this month
    - Done records in downloaded.json for this month
    - Downloaded PDFs in downloads_ny matching this month (if delete_pdfs is True)
    """
    stats = {"dirs": 0, "files": 0, "pdfs": 0}
    if not OUTPUT_DIR.exists():
        return stats

    # Parse yyyy and mm
    parts = year_month.strip().split("-")
    if len(parts) != 2:
        return stats
    yyyy, mm = parts[0], parts[1]
    month_name = ""
    try:
        month_name = calendar.month_name[int(mm)].upper()
    except Exception:
        pass

    # 1. Remove _MONTH_{year_month}
    m_dir = OUTPUT_DIR / f"_MONTH_{year_month}"
    d, f = remove_path_safely(m_dir)
    stats["dirs"] += d
    stats["files"] += f

    # 2. Remove date motion list folders matching mm-dd-yyyy or yyyy-mm-dd
    date_pat = re.compile(rf"^({mm}-\d{{2}}-{yyyy}|{yyyy}-{mm}-\d{{2}}).*_MOTION_LIST$", re.IGNORECASE)
    for p in OUTPUT_DIR.iterdir():
        if p.is_dir() and date_pat.match(p.name):
            d, f = remove_path_safely(p)
            stats["dirs"] += d
            stats["files"] += f

    # 3. Clean downloaded.json for this month
    dl_json = OUTPUT_DIR / "downloaded.json"
    if dl_json.exists():
        try:
            data = json.loads(dl_json.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cleaned = {
                    k: v for k, v in data.items()
                    if not (k.startswith(f"{year_month}-") or k.startswith(f"{mm}-") or f"-{yyyy}::" in k)
                }
                dl_json.write_text(json.dumps(cleaned, indent=2), encoding="utf-8")
        except Exception:
            pass

    # 4. Remove downloaded PDFs if requested
    if delete_pdfs:
        # Check both centralized DOWNLOADS_NY_DIR and legacy OUTPUT_DIR / "downloads_ny"
        dl_roots_to_check = []
        if DOWNLOADS_NY_DIR.exists():
            dl_roots_to_check.append(DOWNLOADS_NY_DIR)
        legacy_dl_root = OUTPUT_DIR / "downloads_ny"
        if legacy_dl_root.exists() and legacy_dl_root not in dl_roots_to_check:
            dl_roots_to_check.append(legacy_dl_root)

        for dl_root in dl_roots_to_check:
            for sub in dl_root.iterdir():
                sub_name = sub.name.upper()
                is_match = (
                    sub.name == year_month
                    or sub.name == f"{yyyy}_{mm}"
                    or (month_name and month_name in sub_name and yyyy in sub_name)
                    or (month_name and sub_name == month_name)
                )
                if is_match:
                    pdf_cnt = len(list(sub.glob("**/*.pdf")))
                    d, f = remove_path_safely(sub)
                    stats["dirs"] += d
                    stats["pdfs"] += pdf_cnt
                    stats["files"] += f

    # 5. Clean team dashboard DB
    _clean_team_dashboard_db(scopes=[f"NY_{year_month}"])
    return stats


def clear_ky_month(court_code: str, year_month: str, delete_pdfs: bool = True) -> dict[str, int]:
    """
    Reset Kentucky scan data for court_code + year_month (e.g. 'KYCOA', '2026-11').
    Target: OUTPUT_DIR / f"KY_{court_code}_{year_month}" and DOWNLOADS_KY_DIR / f"{court_code}_{year_month}"
    """
    stats = {"dirs": 0, "files": 0, "pdfs": 0}

    # Remove downloaded PDFs from organized downloads if requested
    if delete_pdfs and DOWNLOADS_KY_DIR.exists():
        m_words = format_month_words(year_month).replace(" ", "_")
        targets_to_check = [
            DOWNLOADS_KY_DIR / f"{court_code}_{m_words}",
            DOWNLOADS_KY_DIR / f"{court_code}_{year_month}",
        ]
        for ky_dl_dir in targets_to_check:
            if ky_dl_dir.exists():
                pdf_cnt = len(list(ky_dl_dir.glob("**/*.pdf")))
                d, f = remove_path_safely(ky_dl_dir)
                stats["dirs"] += d
                stats["files"] += f
                stats["pdfs"] += pdf_cnt

    if not OUTPUT_DIR.exists():
        return stats

    target_dir = OUTPUT_DIR / f"KY_{court_code}_{year_month}"
    if not target_dir.exists():
        return stats

    if delete_pdfs:
        pdf_cnt = len(list((target_dir / "downloads").glob("**/*.pdf"))) if (target_dir / "downloads").exists() else 0
        d, f = remove_path_safely(target_dir)
        stats["dirs"] += d
        stats["files"] += f
        stats["pdfs"] += pdf_cnt
    else:
        # Keep downloads folder, remove checkpoint and tracker files
        for item in target_dir.iterdir():
            if item.name == "downloads" and item.is_dir():
                continue
            d, f = remove_path_safely(item)
            stats["dirs"] += d
            stats["files"] += f

    # Clean team dashboard DB
    _clean_team_dashboard_db(scopes=[f"KY_{court_code}_{year_month}"])
    return stats


def clear_ny_all(delete_pdfs: bool = True) -> dict[str, int]:
    """Reset ALL New York scan data across all months."""
    stats = {"dirs": 0, "files": 0, "pdfs": 0}

    # Delete from centralized downloads
    if delete_pdfs and DOWNLOADS_NY_DIR.exists():
        pdf_cnt = len(list(DOWNLOADS_NY_DIR.glob("**/*.pdf")))
        d, f = remove_path_safely(DOWNLOADS_NY_DIR)
        stats["dirs"] += d
        stats["pdfs"] += pdf_cnt
        stats["files"] += f

    if not OUTPUT_DIR.exists():
        return stats

    for p in OUTPUT_DIR.iterdir():
        if p.is_dir():
            if p.name.endswith("_MOTION_LIST") or p.name.startswith("_MONTH_"):
                d, f = remove_path_safely(p)
                stats["dirs"] += d
                stats["files"] += f

    dl_json = OUTPUT_DIR / "downloaded.json"
    if dl_json.exists():
        remove_path_safely(dl_json)
        stats["files"] += 1

    if delete_pdfs:
        dl_root = OUTPUT_DIR / "downloads_ny"
        if dl_root.exists():
            pdf_cnt = len(list(dl_root.glob("**/*.pdf")))
            d, f = remove_path_safely(dl_root)
            stats["dirs"] += d
            stats["pdfs"] += pdf_cnt
            stats["files"] += f

    _clean_team_dashboard_db(jurisdictions=["NY"])
    return stats


def clear_ky_all(delete_pdfs: bool = True) -> dict[str, int]:
    """Reset ALL Kentucky scan data across all courts and months."""
    stats = {"dirs": 0, "files": 0, "pdfs": 0}

    # Delete from centralized downloads
    if delete_pdfs and DOWNLOADS_KY_DIR.exists():
        pdf_cnt = len(list(DOWNLOADS_KY_DIR.glob("**/*.pdf")))
        d, f = remove_path_safely(DOWNLOADS_KY_DIR)
        stats["dirs"] += d
        stats["pdfs"] += pdf_cnt
        stats["files"] += f

    if not OUTPUT_DIR.exists():
        return stats

    for p in OUTPUT_DIR.iterdir():
        if p.is_dir() and p.name.startswith("KY_"):
            if delete_pdfs:
                pdf_cnt = len(list((p / "downloads").glob("*.pdf"))) if (p / "downloads").exists() else 0
                d, f = remove_path_safely(p)
                stats["dirs"] += d
                stats["files"] += f
                stats["pdfs"] += pdf_cnt
            else:
                for sub in p.iterdir():
                    if sub.name == "downloads" and sub.is_dir():
                        continue
                    d, f = remove_path_safely(sub)
                    stats["dirs"] += d
                    stats["files"] += f

    _clean_team_dashboard_db(jurisdictions=["KY"])
    return stats


def clear_factory_reset(delete_pdfs: bool = True) -> dict[str, int]:
    """
    Factory reset everything: returns the system to a clean, brand new state.
    Clears all NY and KY scans, checkpoints, and downloaded briefs.
    """
    s_ny = clear_ny_all(delete_pdfs=delete_pdfs)
    s_ky = clear_ky_all(delete_pdfs=delete_pdfs)
    _clean_team_dashboard_db(jurisdictions=["NY", "KY"])

    if delete_pdfs and DOWNLOADS_DIR.exists():
        d, f = remove_path_safely(DOWNLOADS_DIR)

    if OUTPUT_DIR.exists():
        for p in OUTPUT_DIR.iterdir():
            if p.name in ("downloaded.json", "last_jurisdiction.json"):
                remove_path_safely(p)

    return {
        "dirs": s_ny["dirs"] + s_ky["dirs"],
        "files": s_ny["files"] + s_ky["files"],
        "pdfs": s_ny["pdfs"] + s_ky["pdfs"],
    }


class ClearResetModal(tk.Toplevel):
    """
    Modal dialog allowing the user to select the reset scope:
    - Active Month only (NY or KY)
    - Active Jurisdiction only (All NY or All KY)
    - Factory Reset (Brand new clean slate)
    With an option to delete or preserve downloaded PDFs.
    """

    def __init__(self, parent: tk.Tk, app: Any, default_scope: str = "month"):
        super().__init__(parent)
        self.app = app
        self.title("Reset / Clear Scanner Data")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(parent)

        # Context detection
        self.jurisdiction = getattr(app, "jurisdiction", "NY")
        self.ny_month = app.active_month_key() if hasattr(app, "active_month_key") else "2026-11"
        self.ny_label = app.month_var.get() if hasattr(app, "month_var") else self.ny_month

        ky_frame = getattr(app, "ky_frame", None)
        self.ky_court = ky_frame.active_court_code() if ky_frame and hasattr(ky_frame, "active_court_code") else "KYCOA"
        self.ky_month = ky_frame.active_year_month() if ky_frame and hasattr(ky_frame, "active_year_month") else "2026-11"
        self.ky_label = ky_frame.month_var.get() if ky_frame and hasattr(ky_frame, "month_var") else self.ky_month

        self._build_ui(default_scope)

        # Center over parent
        self.update_idletasks()
        w, h = 530, 470
        try:
            x = parent.winfo_rootx() + (parent.winfo_width() // 2) - (w // 2)
            y = parent.winfo_rooty() + (parent.winfo_height() // 2) - (h // 2)
            self.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        except Exception:
            self.geometry(f"{w}x{h}")

        self.grab_set()
        self.focus_set()

    def _build_ui(self, default_scope: str) -> None:
        pad = tk.Frame(self, bg=BG, padx=22, pady=18)
        pad.pack(fill="both", expand=True)

        # Header
        top_bar = tk.Frame(pad, bg=BG)
        top_bar.pack(fill="x", pady=(0, 10))
        tk.Label(
            top_bar, text="🧹 Reset / Clear Scanner Data", bg=BG, fg=TEXT,
            font=("Segoe UI", 13, "bold"), anchor="w"
        ).pack(fill="x")
        tk.Label(
            top_bar, text="Return to a clean slate. Scanned records, checkpoints, and downloads will be removed.",
            bg=BG, fg=MUTED, font=("Segoe UI", 9), anchor="w"
        ).pack(fill="x", pady=(2, 0))

        # Scope options container
        scope_box = tk.Frame(pad, bg=CARD, highlightbackground=LINE, highlightthickness=1, padx=14, pady=12)
        scope_box.pack(fill="x", pady=(0, 12))

        tk.Label(scope_box, text="SELECT SCOPE TO RESET:", bg=CARD, fg=MUTED, font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(0, 8))

        self.scope_var = tk.StringVar(value=default_scope)

        # Option 1: Month
        if self.jurisdiction == "NY":
            opt1_title = f"Active Month: New York — {self.ny_label}"
            opt1_desc = f"Clears all scan records, reports, and downloaded PDFs for {self.ny_label} only."
        else:
            opt1_title = f"Active Month: Kentucky — {self.ky_court} ({self.ky_label})"
            opt1_desc = f"Clears all search rows, checkpoints, tracker, and PDFs for {self.ky_court} {self.ky_label}."

        r1 = tk.Radiobutton(
            scope_box, text=opt1_title, variable=self.scope_var, value="month",
            bg=CARD, fg=TEXT, selectcolor=BG, activebackground=CARD, activeforeground=TEXT,
            font=("Segoe UI", 10, "bold"), highlightthickness=0, anchor="w",
        )
        r1.pack(fill="x")
        tk.Label(scope_box, text=f"    {opt1_desc}", bg=CARD, fg=MUTED, font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 8))

        # Option 2: Jurisdiction
        if self.jurisdiction == "NY":
            opt2_title = "Current Jurisdiction: All New York (All Months)"
            opt2_desc = "Clears all New York motion lists, checkpoints, and downloaded briefs."
        else:
            opt2_title = "Current Jurisdiction: All Kentucky (All Courts & Months)"
            opt2_desc = "Clears all KYCOA and KYSUP search records, trackers, and downloaded briefs."

        r2 = tk.Radiobutton(
            scope_box, text=opt2_title, variable=self.scope_var, value="jurisdiction",
            bg=CARD, fg=TEXT, selectcolor=BG, activebackground=CARD, activeforeground=TEXT,
            font=("Segoe UI", 10, "bold"), highlightthickness=0, anchor="w",
        )
        r2.pack(fill="x")
        tk.Label(scope_box, text=f"    {opt2_desc}", bg=CARD, fg=MUTED, font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 8))

        # Option 3: Factory Reset
        r3 = tk.Radiobutton(
            scope_box, text="⚡ Factory Reset: EVERYTHING (Complete Clean Slate)", variable=self.scope_var, value="all",
            bg=CARD, fg="#f87171", selectcolor=BG, activebackground=CARD, activeforeground="#f87171",
            font=("Segoe UI", 10, "bold"), highlightthickness=0, anchor="w",
        )
        r3.pack(fill="x")
        tk.Label(scope_box, text="    Clears ALL scan data, checkpoints, and PDFs across both NY & KY. Brand new!", bg=CARD, fg=MUTED, font=("Segoe UI", 8)).pack(anchor="w")

        # Delete PDFs Checkbox
        pdf_box = tk.Frame(pad, bg=CARD, highlightbackground=LINE, highlightthickness=1, padx=14, pady=10)
        pdf_box.pack(fill="x", pady=(0, 14))

        self.delete_pdfs_var = tk.BooleanVar(value=True)
        cb = tk.Checkbutton(
            pdf_box, text="Also permanently delete downloaded PDF files from disk",
            variable=self.delete_pdfs_var, bg=CARD, fg=TEXT, selectcolor=BG,
            activebackground=CARD, activeforeground=TEXT, font=("Segoe UI", 9, "bold"),
            highlightthickness=0, anchor="w",
        )
        cb.pack(fill="x")
        tk.Label(
            pdf_box, text="Uncheck if you only want to re-scan cases from scratch but keep your existing PDF briefs on disk.",
            bg=CARD, fg=MUTED, font=("Segoe UI", 8), anchor="w",
        ).pack(fill="x", padx=(20, 0), pady=(2, 0))

        # Bottom buttons
        btn_bar = tk.Frame(pad, bg=BG)
        btn_bar.pack(fill="x", side="bottom")

        cancel_btn = tk.Button(
            btn_bar, text="Cancel", command=self.destroy, relief="flat", bd=0,
            padx=14, pady=6, cursor="hand2", bg=CARD, fg=TEXT, activebackground=LINE,
            activeforeground=TEXT, font=("Segoe UI", 9),
        )
        cancel_btn.pack(side="left")

        self.apply_btn = tk.Button(
            btn_bar, text="🗑 Clear / Reset Now", command=self._execute_clear, relief="flat", bd=0,
            padx=16, pady=6, cursor="hand2", bg=DANGER, fg="#ffffff", activebackground="#b91c1c",
            activeforeground="#ffffff", font=("Segoe UI", 9, "bold"),
        )
        self.apply_btn.pack(side="right")

    def _execute_clear(self) -> None:
        scope = self.scope_var.get()
        delete_pdfs = self.delete_pdfs_var.get()

        if scope == "month":
            scope_desc = f"{self.ny_label} (New York)" if self.jurisdiction == "NY" else f"{self.ky_court} {self.ky_label} (Kentucky)"
        elif scope == "jurisdiction":
            scope_desc = f"ALL data for {('New York' if self.jurisdiction == 'NY' else 'Kentucky')}"
        else:
            scope_desc = "EVERYTHING across both New York and Kentucky (Complete Factory Reset)"

        pdf_warning = "AND will PERMANENTLY DELETE downloaded PDF files from disk" if delete_pdfs else "while keeping your downloaded PDF files intact"

        confirm_msg = (
            f"Are you sure you want to proceed with the reset?\n\n"
            f"Target: {scope_desc}\n"
            f"PDF Files: {pdf_warning}.\n\n"
            f"This action cannot be undone."
        )

        if not messagebox.askyesno("Confirm Permanent Reset", confirm_msg, parent=self):
            return

        # Execute
        stats: dict[str, int] = {"dirs": 0, "files": 0, "pdfs": 0}
        try:
            if scope == "month":
                if self.jurisdiction == "NY":
                    stats = clear_ny_month(self.ny_month, delete_pdfs=delete_pdfs)
                    if hasattr(self.app, "on_ny_data_cleared"):
                        self.app.on_ny_data_cleared(self.ny_month)
                else:
                    stats = clear_ky_month(self.ky_court, self.ky_month, delete_pdfs=delete_pdfs)
                    ky_f = getattr(self.app, "ky_frame", None)
                    if ky_f and hasattr(ky_f, "on_ky_data_cleared"):
                        ky_f.on_ky_data_cleared()

            elif scope == "jurisdiction":
                if self.jurisdiction == "NY":
                    stats = clear_ny_all(delete_pdfs=delete_pdfs)
                    if hasattr(self.app, "on_ny_data_cleared"):
                        self.app.on_ny_data_cleared(None)
                else:
                    stats = clear_ky_all(delete_pdfs=delete_pdfs)
                    ky_f = getattr(self.app, "ky_frame", None)
                    if ky_f and hasattr(ky_f, "on_ky_data_cleared"):
                        ky_f.on_ky_data_cleared()

            elif scope == "all":
                stats = clear_factory_reset(delete_pdfs=delete_pdfs)
                if hasattr(self.app, "on_ny_data_cleared"):
                    self.app.on_ny_data_cleared(None)
                ky_f = getattr(self.app, "ky_frame", None)
                if ky_f and hasattr(ky_f, "on_ky_data_cleared"):
                    ky_f.on_ky_data_cleared()

            self.destroy()

            msg = (
                f"Reset complete! The scanner has returned to a clean slate.\n\n"
                f"• Folders removed: {stats['dirs']}\n"
                f"• Scan / report files cleared: {stats['files']}\n"
                f"• PDF briefs deleted: {stats['pdfs']}"
            )
            messagebox.showinfo("Reset Successful", msg, parent=self.app.root)

        except Exception as exc:
            messagebox.showerror("Reset Error", f"An error occurred while clearing data:\n{exc}", parent=self)
