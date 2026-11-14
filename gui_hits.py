import json
import os
import re
import tkinter as tk
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

from classifier import STATUS_HIT_VALID
from config import DOWNLOADS_DIR, DOWNLOADS_NY_DIR, OUTPUT_DIR
from report_builder import load_checkpoint
from ui_theme import format_date_words

BG, CARD, LINE = "#111111", "#1a1a1a", "#2a2a2a"
TEXT, MUTED, ACCENT = "#ededed", "#8a8a8a", "#4ade80"
WARN_COLOR = "#f5b942"
ERR_COLOR = "#f87171"
ERR_BG = "#2e1212"
WARN_BG = "#2a2415"
WHOLE_MONTH = "Whole month"

NY_BRIEF_TYPE_MAP = [
    ("PETITION FOR REVIEW", "PetitionForReview"),
    ("PETITION FOR REHEARING", "PetitionForRehearing"),
    ("PETITION FOR WRIT OF MANDAMUS", "PetitionForWritOfMandamus"),
    ("WRIT OF MANDAMUS", "WritOfMandamus"),
    ("WRIT OF CERTIORARI", "WritOfCertiorari"),
    ("WRIT OF HABEAS CORPUS", "WritOfHabeasCorpus"),
    ("APPELLANT'S BRIEF", "AppellantBrief"),
    ("APPELLANTS BRIEF", "AppellantBrief"),
    ("RESPONDENT'S BRIEF", "RespondentBrief"),
    ("RESPONDENTS BRIEF", "RespondentBrief"),
    ("REPLY BRIEF", "ReplyBrief"),
    ("AFFIRMATION", "AffidavitOfService"),
    ("AFFIDAVIT", "AffidavitOfService"),
    ("BRIEF", "Brief"),
]


def clean_original_filename(title: str) -> str:
    """Sanitize original document title for use as a valid filesystem filename."""
    cleaned = re.sub(r'[<>:"/\\|?*]+', " - ", (title or "Brief").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-_")
    if cleaned.lower().endswith(".pdf"):
        cleaned = cleaned[:-4].strip()
    return cleaned or "Brief"


def assign_ny_original_names(doc_titles: list[str]) -> list[str]:
    """
    Keep original NY document names intact without renaming or CamelCasing.
    Disambiguates duplicates within the same case with (2), (3)...
    """
    clean_titles = [clean_original_filename(t) for t in doc_titles]
    counts: dict[str, int] = {}
    for ct in clean_titles:
        counts[ct] = counts.get(ct, 0) + 1

    seen: dict[str, int] = {}
    names: list[str] = []
    for ct in clean_titles:
        if counts[ct] > 1:
            idx = seen.get(ct, 0) + 1
            seen[ct] = idx
            name = f"{ct} ({idx})" if idx > 1 else ct
        else:
            name = ct
        names.append(name)
    return names


def assign_ny_save_as_names(case_number: str, doc_titles: list[str]) -> list[str]:
    """For NY, keep the original document names (no SOP renaming required)."""
    return assign_ny_original_names(doc_titles)


def ny_brief_camel_type(title: str) -> str:
    up = (title or "").upper()
    for phrase, camel in NY_BRIEF_TYPE_MAP:
        if phrase in up:
            return camel
    cleaned = re.sub(r"[^A-Za-z0-9]+", " ", title).title().replace(" ", "")
    return cleaned or "Brief"


def ny_downloads_dir(
    month_str: str | None = None,
    case_number: str | None = None,
    date_str: str | None = None,
) -> Path:
    base = DOWNLOADS_NY_DIR
    if month_str:
        safe_m = re.sub(r"[^A-Za-z0-9_-]+", "_", month_str).strip("_")
        if safe_m:
            base = base / safe_m
    if date_str:
        safe_d = re.sub(r'[<>:"/\\|?*]+', "_", date_str).strip()
        if safe_d:
            base = base / safe_d
    if case_number:
        safe_c = re.sub(r'[<>:"/\\|?*]+', "_", case_number).strip()
        if safe_c:
            base = base / safe_c
    base.mkdir(parents=True, exist_ok=True)
    return base


def ny_motion_lists_dir(month_str: str | None = None) -> Path:
    """Return the dedicated 'Motion Lists' folder inside the month's downloads directory."""
    base = ny_downloads_dir(month_str) / "Motion Lists"
    base.mkdir(parents=True, exist_ok=True)
    return base


@dataclass(frozen=True)
class BriefDoc:
    title: str
    url: str
    save_as_name: str
    row_summary: str = ""


@dataclass(frozen=True)
class HitRow:
    date_tag: str
    case_number: str
    caption: str
    briefs: str
    key: str
    pdf_count: int = 0
    brief_titles: tuple[str, ...] = ()
    docs: tuple[BriefDoc, ...] = ()


class DoneStore:
    """Persists which hit cases were already downloaded, so progress survives restarts."""

    def __init__(self, path: Path | None = None):
        self.path = path or (OUTPUT_DIR / "downloaded.json")
        self._done: dict[str, bool] = {}
        if self.path.exists():
            try:
                self._done = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._done = {}  # corrupt file: start clean instead of crashing the UI

    def is_done(self, key: str) -> bool:
        return bool(self._done.get(key))

    def toggle(self, key: str) -> bool:
        if self._done.pop(key, None) is None:
            self._done[key] = True
        self._save()
        return self.is_done(key)

    def set_many(self, keys: list[str], done: bool) -> None:
        for k in keys:
            if done:
                self._done[k] = True
            else:
                self._done.pop(k, None)
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._done, indent=2), encoding="utf-8")
        tmp.replace(self.path)


def collect_hits(month_opts: list, run_dir_for_option: Callable, scope: str = WHOLE_MONTH) -> list[HitRow]:
    rows: list[HitRow] = []
    for opt in month_opts:
        date_tag = opt.date_iso or opt.filename
        if scope not in (WHOLE_MONTH, date_tag, format_date_words(date_tag)):
            continue
        for rec in load_checkpoint(run_dir_for_option(opt)).values():
            if rec.status != STATUS_HIT_VALID:
                continue
            b_list = [b.title for b in (rec.briefs or [])]
            save_names = assign_ny_original_names(b_list)
            doc_objs = tuple(
                BriefDoc(
                    title=b.title,
                    url=b.url,
                    save_as_name=save_names[i] if i < len(save_names) else clean_original_filename(b.title),
                    row_summary=getattr(b, "row_summary", ""),
                )
                for i, b in enumerate(rec.briefs or [])
            )
            titles = ", ".join(f"[{i}] {t}" for i, t in enumerate(b_list, 1))
            rows.append(
                HitRow(
                    date_tag,
                    rec.case_number,
                    rec.caption,
                    titles,
                    f"{date_tag}::{rec.case_number}",
                    pdf_count=len(b_list),
                    brief_titles=tuple(b_list),
                    docs=doc_objs,
                )
            )
    return rows


class HitListView(tk.Frame):
    def __init__(self, parent, get_context: Callable[[], tuple[str, list]], open_browser: Callable,
                 store: DoneStore | None = None, run_dir_resolver: Callable | None = None,
                 download_handler: Callable | None = None, open_folder_handler: Callable | None = None):
        super().__init__(parent, bg=BG)
        self.get_context = get_context
        self.store = store or DoneStore()
        self.run_dir_resolver = run_dir_resolver
        self.download_handler = download_handler
        self.open_folder_handler = open_folder_handler
        self.rows: list[HitRow] = []
        self.checked_keys: set[str] = set()
        self._build(open_browser)

    def _btn(self, parent, text, cmd, primary: bool = False):
        bg = ACCENT if primary else CARD
        fg = "#111111" if primary else TEXT
        fnt = ("Segoe UI", 9, "bold") if primary else ("Segoe UI", 9)
        return tk.Button(parent, text=text, command=cmd, relief="flat", bd=0, padx=12, pady=6,
                         cursor="hand2", bg=bg, fg=fg, activebackground=LINE, activeforeground=TEXT,
                         font=fnt)

    def _build(self, open_browser: Callable) -> None:
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", pady=(10, 8))
        self.scope_var = tk.StringVar(value=WHOLE_MONTH)
        self.scope_box = ttk.Combobox(bar, textvariable=self.scope_var, state="readonly", width=18,
                                      values=[WHOLE_MONTH])
        self.scope_box.pack(side="left")
        self.scope_box.bind("<<ComboboxSelected>>", lambda _e: self.refresh())
        self.filter_var = tk.StringVar()
        entry = tk.Entry(bar, textvariable=self.filter_var, bg=CARD, fg=TEXT, insertbackground=TEXT,
                         relief="flat", highlightthickness=1, highlightbackground=LINE,
                         highlightcolor=ACCENT, width=24)
        entry.pack(side="left", padx=10, ipady=4)
        self.filter_var.trace_add("write", lambda *_: self._fill())
        self.hide_var = tk.BooleanVar(value=False)
        tk.Checkbutton(bar, text="Hide downloaded", variable=self.hide_var, command=self._fill, bg=BG,
                       fg=MUTED, selectcolor=CARD, activebackground=BG, activeforeground=TEXT,
                       highlightthickness=0).pack(side="left")
        self.counter = tk.Label(bar, text="", bg=BG, fg=MUTED)
        self.counter.pack(side="right")

        wrap = tk.Frame(self, bg=BG)
        wrap.pack(fill="both", expand=True)
        cols = ("sel", "num", "done", "date", "case", "pdfs", "briefs")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="extended")
        for col, label, width, anchor, stretch in (
            ("sel", "☐", 36, "center", False),
            ("num", "#", 38, "center", False),
            ("done", "Done", 50, "center", False),
            ("date", "Date", 125, "w", False),
            ("case", "Case #", 100, "w", False),
            ("pdfs", "Brief PDFs", 85, "center", False),
            ("briefs", "Brief Documents", 460, "w", True),
        ):
            if col == "sel":
                self.tree.heading(col, text=label, anchor=anchor, command=self.toggle_check_all)
            else:
                self.tree.heading(col, text=label, anchor=anchor)
            self.tree.column(col, width=width, anchor=anchor, stretch=stretch)
        self.tree.tag_configure("checked", foreground=ACCENT)
        self.tree.tag_configure("done", foreground=MUTED)
        self.tree.tag_configure("todo", foreground=TEXT)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        sb_x = ttk.Scrollbar(wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=sb.set, xscrollcommand=sb_x.set)
        sb.pack(side="right", fill="y")
        sb_x.pack(side="bottom", fill="x")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Button-1>", self._on_tree_click, add="+")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_select())
        self.tree.bind("<Double-1>", lambda _e: self.toggle_selected())
        self.tree.bind("<space>", lambda _e: self.toggle_selected())
        self.tree.bind("<Control-a>", lambda _e: self.select_all())
        self.tree.bind("<Control-A>", lambda _e: self.select_all())

        self.detail_box = tk.Frame(self, bg=CARD, highlightbackground=LINE, highlightthickness=1, padx=12, pady=8)
        self.detail_box.pack(fill="x", pady=(6, 0))
        self.detail_title_lbl = tk.Label(
            self.detail_box, text="Click a row above to view full Case Caption & Brief Document titles",
            bg=CARD, fg=TEXT, font=("Segoe UI", 9, "bold"), anchor="w", justify="left", wraplength=950,
        )
        self.detail_title_lbl.pack(fill="x")
        self.detail_items_frame = tk.Frame(self.detail_box, bg=CARD)
        self.detail_items_frame.pack(fill="x", pady=(4, 0))
        self.detail_lbl = self.detail_title_lbl

        foot = tk.Frame(self, bg=BG)
        foot.pack(fill="x", pady=(8, 0))
        self.dl_all_btn = self._btn(foot, "⬇ Download All PDFs", self.download_checked_or_all, primary=True)
        self.dl_all_btn.pack(side="left", padx=(0, 6))
        self.open_folder_btn = self._btn(foot, "Open PDFs Folder", self.open_downloads_folder)
        self.open_folder_btn.pack(side="left", padx=(0, 6))
        self.audit_btn = self._btn(foot, "Scan Downloads", self.audit_downloads_folder)
        self.audit_btn.pack(side="left", padx=(0, 6))
        self.toggle_done_btn = self._btn(foot, "✓ Mark / Unmark", self.toggle_selected)
        self.toggle_done_btn.pack(side="left", padx=(0, 6))
        self.copy_btn = self._btn(foot, "Copy Case #", self.copy_selected)
        self.copy_btn.pack(side="left", padx=(0, 6))
        self.note = tk.Label(foot, text="", bg=BG, fg=ACCENT)
        self.note.pack(side="left", padx=10)

    # ---------- data ----------
    def refresh(self) -> None:
        resolver = self.run_dir_resolver
        if resolver is None:
            from main import _run_dir_for_option as resolver

        _month, month_opts = self.get_context()
        tags = [WHOLE_MONTH] + [format_date_words(o.date_iso or o.filename) for o in month_opts]
        self.scope_box.configure(values=tags)
        if self.scope_var.get() not in tags:
            self.scope_var.set(WHOLE_MONTH)
        self.rows = collect_hits(month_opts, resolver, self.scope_var.get())
        self._fill()

    def visible_rows(self) -> list[HitRow]:
        needle = self.filter_var.get().strip().lower()
        out = []
        for r in self.rows:
            if self.hide_var.get() and self.store.is_done(r.key):
                continue
            date_words = format_date_words(r.date_tag)
            if needle and needle not in f"{r.case_number} {r.caption} {r.date_tag} {date_words}".lower():
                continue
            out.append(r)
        return out

    def _fill(self) -> None:
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        self.row_by_key = {r.key: r for r in self.rows}
        for idx, r in enumerate(self.visible_rows(), start=1):
            done = self.store.is_done(r.key)
            is_checked = r.key in self.checked_keys
            pdf_lbl = f"{r.pdf_count} PDF{'s' if r.pdf_count != 1 else ''}"
            row_tag = "checked" if is_checked else ("done" if done else "todo")
            self.tree.insert(
                "",
                "end",
                iid=r.key,
                tags=(row_tag,),
                values=(
                    "☑" if is_checked else "☐",
                    idx,
                    "✔" if done else "",
                    format_date_words(r.date_tag),
                    r.case_number,
                    pdf_lbl,
                    r.briefs,
                ),
            )
        for iid in selected:
            if self.tree.exists(iid):
                self.tree.selection_add(iid)
        downloaded = sum(1 for r in self.rows if self.store.is_done(r.key))
        total_pdfs = sum(r.pdf_count for r in self.rows)
        done_pdfs = sum(r.pdf_count for r in self.rows if self.store.is_done(r.key))
        self.counter.configure(
            text=f"{len(self.rows)} cases • {total_pdfs} Brief PDFs • Done: {downloaded}/{len(self.rows)} cases ({done_pdfs}/{total_pdfs} PDFs)"
        )
        if hasattr(self, "show_pdfs_btn"):
            self.show_pdfs_btn.configure(text=f"Show All Brief PDFs ({total_pdfs})")
        vis = self.visible_rows()
        all_vis_done = bool(vis) and all(self.store.is_done(r.key) for r in vis)
        if hasattr(self, "mark_all_btn"):
            self.mark_all_btn.configure(text="Unmark All" if all_vis_done else "Mark All Downloaded")
        vis_keys = [r.key for r in vis]
        all_checked = bool(vis_keys) and all(k in self.checked_keys for k in vis_keys)
        self.tree.heading("sel", text="☑" if all_checked else "☐")
        if hasattr(self, "select_all_btn"):
            self.select_all_btn.configure(text="☐ Unselect All" if all_checked else "☑ Select All")
        if hasattr(self, "dl_all_btn"):
            checked_cnt = len(self.checked_keys)
            if checked_cnt > 0:
                self.dl_all_btn.configure(text=f"⬇ Download Selected ({checked_cnt})")
            else:
                self.dl_all_btn.configure(text=f"⬇ Download All PDFs ({total_pdfs})")
        self._on_select()

    def _copy_text(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()
        self.note.configure(text=f"Copied: {text[:35]}...")

    def _render_brief_lines(self, docs_or_titles: list, case_number: str = "", key: str = "") -> None:
        for w in self.detail_items_frame.winfo_children():
            w.destroy()
        if not docs_or_titles:
            return
        for idx, item in enumerate(docs_or_titles, 1):
            if isinstance(item, BriefDoc):
                doc_title = item.title
                doc_url = item.url
                save_as = item.save_as_name
            else:
                m = re.match(r"^\[\d+\]\s*(.*)$", str(item))
                doc_title = m.group(1).strip() if m else str(item).strip()
                doc_url = ""
                save_as = clean_original_filename(doc_title)

            row = tk.Frame(self.detail_items_frame, bg=CARD)
            row.pack(fill="x", pady=2)

            local_exists = bool(self._find_downloaded_pdf(case_number, doc_title))
            tk.Button(
                row,
                text="✔" if local_exists else "⬇",
                command=lambda s=save_as, u=doc_url, t=doc_title, c=case_number, k=key: (
                    self._confirm_download_single(s, u, t, c, k)
                ),
                relief="flat", bd=0, width=3, padx=4, pady=1, cursor="hand2",
                bg=LINE if local_exists else ACCENT, fg=ACCENT if local_exists else "#111111",
                activebackground=LINE, activeforeground=TEXT,
                font=("Segoe UI", 9, "bold"),
            ).pack(side="left", padx=(0, 8))

            tk.Label(
                row, text=f"◄ [{idx}] Website: {doc_title}", bg=CARD, fg=TEXT, font=("Segoe UI", 9),
                anchor="w", justify="left",
            ).pack(side="left", fill="x", expand=True)

            if doc_url or save_as:
                tk.Button(
                    row, text="👁 Open",
                    command=lambda c=case_number, t=doc_title, u=doc_url, s=save_as, k=key: self._on_view_doc(c, t, u, s, k),
                    relief="flat", bd=0, padx=6, pady=1, bg=LINE, fg=TEXT,
                    font=("Segoe UI", 8), cursor="hand2",
                ).pack(side="right", padx=(4, 2))

            tk.Button(
                row, text="📋 Copy", command=lambda s=save_as: self._copy_text(s),
                relief="flat", bd=0, padx=6, pady=1, bg=LINE, fg=MUTED,
                activebackground=BG, activeforeground=TEXT,
                font=("Segoe UI", 8), cursor="hand2",
            ).pack(side="right", padx=(4, 2))

    def _on_select(self) -> None:
        sel = self.tree.selection()
        if not sel:
            self.detail_title_lbl.configure(text="Click a row above to view full Case Caption & Brief Document titles")
            self._render_brief_lines([])
            return
        key = sel[0]
        r = getattr(self, "row_by_key", {}).get(key)
        if r:
            pdf_lbl = f"{r.pdf_count} Brief PDF{'s' if r.pdf_count != 1 else ''}"
            clean_caption = " ".join((r.caption or "").split())
            caption_txt = f"  •  {clean_caption}" if clean_caption else ""
            self.detail_title_lbl.configure(
                text=f"{r.case_number}{caption_txt}   ({pdf_lbl})"
            )
            docs = list(r.docs) if r.docs else []
            if not docs:
                titles = list(r.brief_titles) if r.brief_titles else []
                if not titles and r.briefs:
                    parsed = re.split(r",\s*(?=\[\d+\])", r.briefs)
                    titles = [p.strip() for p in parsed if p.strip()]
                save_names = assign_ny_original_names(titles)
                docs = [
                    BriefDoc(
                        title=t,
                        url="",
                        save_as_name=save_names[i] if i < len(save_names) else clean_original_filename(t),
                    )
                    for i, t in enumerate(titles)
                ]
            self._render_brief_lines(docs, r.case_number, r.key)

    def download_checked_or_all(self) -> None:
        vis = self.visible_rows()
        if not vis:
            return
        if self.checked_keys:
            targets = [r for r in vis if r.key in self.checked_keys]
        else:
            targets = [r for r in vis if not self.store.is_done(r.key)]
            if not targets:
                targets = vis

        items = []
        month_label = ""
        try:
            month_label = self.get_context()[0]
        except Exception:
            pass

        dest_dir = ny_downloads_dir(month_label)
        for r in targets:
            day_words = format_date_words(r.date_tag)
            day_dest_dir = ny_downloads_dir(month_label, date_str=day_words)
            docs = r.docs
            if not docs and r.brief_titles:
                save_names = assign_ny_original_names(list(r.brief_titles))
                docs = tuple(
                    BriefDoc(t, "", save_names[i] if i < len(save_names) else clean_original_filename(t))
                    for i, t in enumerate(r.brief_titles)
                )
            for d in docs:
                items.append({
                    "key": r.key,
                    "case_number": r.case_number,
                    "title": d.title,
                    "save_as_name": d.save_as_name,
                    "url": d.url,
                    "dest_dir": str(day_dest_dir),
                    "save_path": str(day_dest_dir / f"{d.save_as_name}.pdf"),
                })

        if not items:
            self.note.configure(text="No brief PDFs to download")
            return

        if self.download_handler:
            self.download_handler(items)
        else:
            self.note.configure(text=f"Ready to download {len(items)} PDF(s)")

    def _confirm_download_single(self, save_as: str, url: str, title: str, case_number: str, key: str) -> None:
        from tkinter import messagebox
        date_tag = key.split("::")[0] if "::" in key else ""
        local = self._find_downloaded_pdf(case_number, title, date_tag=date_tag)
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
                    self.note.configure(text=f"Opened existing: {local.name[:35]}...")
                except Exception as exc:
                    self.note.configure(text=f"Error opening PDF: {exc}")
                return

        month_label = ""
        try:
            month_label = self.get_context()[0]
        except Exception:
            pass
        day_words = format_date_words(date_tag) if date_tag else None
        target_dir = ny_downloads_dir(month_label, date_str=day_words)
        msg = (
            f"Do you want to download this brief PDF?\n\n"
            f"• Case: {case_number}\n"
            f"• Document: {title}\n\n"
            f"The file will be saved directly to:\n{target_dir}"
        )
        if not messagebox.askyesno("Confirm Download", msg, parent=self):
            return
        self._copy_text(title)
        self._on_download_single_doc(save_as, url, title, case_number, key)

    def _on_download_single_doc(self, save_as: str, url: str, title: str, case_number: str, key: str) -> None:
        month_label = ""
        try:
            month_label = self.get_context()[0]
        except Exception:
            pass
        date_tag = key.split("::")[0] if "::" in key else ""
        day_words = format_date_words(date_tag) if date_tag else None
        dest_dir = ny_downloads_dir(month_label, date_str=day_words)
        fallback_name = clean_original_filename(title) if not save_as else save_as
        item = {
            "key": key,
            "case_number": case_number,
            "title": title,
            "save_as_name": fallback_name,
            "url": url,
            "dest_dir": str(dest_dir),
            "save_path": str(dest_dir / f"{fallback_name}.pdf"),
        }
        if self.download_handler:
            self.download_handler([item])
            self.note.configure(text=f"Downloading {save_as}.pdf...")
        else:
            self.note.configure(text=f"Copied {save_as}")

    def _find_downloaded_pdf(self, case_number: str, doc_title: str, date_tag: str | None = None) -> Path | None:
        month_label = ""
        try:
            month_label = self.get_context()[0]
        except Exception:
            pass
        dirs_to_check: list[Path] = []
        if date_tag:
            dirs_to_check.append(ny_downloads_dir(month_label, date_str=format_date_words(date_tag)))
        month_root = ny_downloads_dir(month_label)
        dirs_to_check.append(month_root)

        # Check all day subdirectories in month folder
        if month_root.exists():
            for sub in month_root.iterdir():
                if sub.is_dir() and sub.name != "Motion Lists" and sub not in dirs_to_check:
                    dirs_to_check.append(sub)

        # Check all other subdirectories in DOWNLOADS_NY_DIR
        if DOWNLOADS_NY_DIR.exists():
            for sub in DOWNLOADS_NY_DIR.iterdir():
                if sub.is_dir() and sub not in dirs_to_check:
                    dirs_to_check.append(sub)

        # Backwards compatibility: also check legacy OUTPUT_DIR / "downloads_ny"
        base_legacy = OUTPUT_DIR / "downloads_ny"
        if base_legacy.exists():
            if base_legacy not in dirs_to_check:
                dirs_to_check.append(base_legacy)
            for sub in base_legacy.iterdir():
                if sub.is_dir() and sub not in dirs_to_check:
                    dirs_to_check.append(sub)

        safe_case = case_number.replace("-", "_").upper()
        raw_case = case_number.upper()
        clean_words = [w for w in re.sub(r"[^A-Za-z0-9]+", " ", doc_title).upper().split() if len(w) > 3]

        for d in dirs_to_check:
            if not d.exists():
                continue
            candidates = list(d.glob("*.pdf"))
            sub = d / case_number
            if sub.exists():
                candidates.extend(sub.glob("*.pdf"))
            for p in candidates:
                p_up = p.name.upper()
                if safe_case in p_up or raw_case in p_up:
                    if not clean_words or any(w in p_up for w in clean_words):
                        return p
        return None

    def _on_view_doc(self, case_number: str, doc_title: str, doc_url: str, save_as: str, key: str) -> None:
        date_tag = key.split("::")[0] if "::" in key else None
        local = self._find_downloaded_pdf(case_number, doc_title, date_tag=date_tag)
        if local and local.exists():
            try:
                os.startfile(str(local))
                self.note.configure(text=f"Opened: {local.name[:35]}...")
                return
            except Exception as exc:
                self.note.configure(text=f"Error opening PDF: {exc}")
                return

        # If not yet downloaded locally, guide the user to download first
        self.note.configure(text="Not downloaded yet. Click the ⬇ icon to download.")

    def open_downloads_folder(self) -> None:
        month_label = ""
        try:
            month_label = self.get_context()[0]
        except Exception:
            pass
        if self.open_folder_handler:
            self.open_folder_handler(month_label)
            return
        scope = self.scope_var.get().strip() if hasattr(self, "scope_var") else WHOLE_MONTH
        if scope and scope != WHOLE_MONTH:
            d = ny_downloads_dir(month_label, date_str=scope)
        else:
            d = ny_downloads_dir(month_label)
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(d))
        except Exception:
            webbrowser.open(d.as_uri())

    def audit_downloads_folder(self) -> None:
        month_label = ""
        try:
            month_label = self.get_context()[0]
        except Exception:
            pass
        m_dir = ny_downloads_dir(month_label)
        month_display = month_label or "NY"

        if not m_dir.exists():
            messagebox.showinfo(
                "Scan Downloads",
                f"No downloads directory found on disk for {month_display}.\n\nPath:\n{m_dir}",
                parent=self,
            )
            return

        # 1. Collect all expected collectible docs
        expected_docs: dict[str, tuple[str, str, dict]] = {}
        for r in self.rows:
            day_words = format_date_words(r.date_tag) if r.date_tag else None
            dest_dir = ny_downloads_dir(month_label, date_str=day_words)
            for b in (r.docs or []):
                fn = f"{b.save_as_name}.pdf".lower()
                item = {
                    "key": r.key,
                    "case_number": r.case_number,
                    "title": b.title,
                    "save_as_name": b.save_as_name,
                    "url": b.url,
                    "dest_dir": str(dest_dir),
                    "save_path": str(dest_dir / f"{b.save_as_name}.pdf"),
                }
                expected_docs[fn] = (r.case_number, b.title, item)

        # 2. Scan actual files on disk
        files_by_name: dict[str, list[Path]] = {}
        corrupted_files: list[Path] = []
        for p in m_dir.rglob("*"):
            if p.is_file():
                if p.name.lower().endswith(".part") or p.stat().st_size < 100:
                    corrupted_files.append(p)
                if p.name.lower().endswith(".pdf"):
                    files_by_name.setdefault(p.name.lower(), []).append(p)

        # 3. Disambiguate with _find_downloaded_pdf fallback
        missing_docs: list[tuple[str, str, str, dict]] = []
        for exp_name, (cn, title, item) in sorted(expected_docs.items()):
            if exp_name not in files_by_name:
                date_tag = item.get("key", "").split("::")[0] if "::" in item.get("key", "") else None
                found_alt = self._find_downloaded_pdf(cn, title, date_tag=date_tag)
                if found_alt and found_alt.exists() and found_alt.stat().st_size >= 100:
                    files_by_name.setdefault(found_alt.name.lower(), []).append(found_alt)
                else:
                    missing_docs.append((cn, exp_name, title, item))

        # 4. Find duplicates
        duplicates: list[tuple[str, list[Path]]] = []
        for fname, plist in sorted(files_by_name.items()):
            if len(plist) > 1:
                duplicates.append((fname, plist))

        total_expected = len(expected_docs)
        total_found = len(files_by_name)
        has_issues = bool(missing_docs or duplicates or corrupted_files)

        win = tk.Toplevel(self)
        win.title(f"Downloads Audit & Verification — NYSCEF {month_display}")
        win.configure(bg=BG)
        win.geometry("960x620")
        win.minsize(820, 500)

        win.update_idletasks()
        try:
            x = self.winfo_rootx() + max(0, (self.winfo_width() - 960) // 2)
            y = self.winfo_rooty() + max(0, (self.winfo_height() - 620) // 2)
            win.geometry(f"960x620+{x}+{y}")
        except Exception:
            win.geometry("960x620")

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
                text=f"Every single collectible brief ({total_expected} PDFs) is properly saved on your hard drive.\n"
                     f"• No missing brief files\n"
                     f"• No duplicate files across folders\n"
                     f"• No zero-byte or corrupted .part files",
                bg="#122416", fg=TEXT, font=("Segoe UI", 10), justify="center",
            ).pack()
        else:
            if missing_docs:
                mis_box = tk.LabelFrame(
                    content_f, text=f" ❌ Missing / Hindi Na-Download ({len(missing_docs)} PDFs) ",
                    bg=CARD, fg=ERR_COLOR, font=("Segoe UI", 9, "bold"), bd=1, highlightbackground=ERR_COLOR,
                )
                mis_box.pack(fill="both", expand=True, pady=(0, 6))

                cols = ("num", "case", "file", "title")
                tv_mis = ttk.Treeview(mis_box, columns=cols, show="headings", height=min(6, len(missing_docs)))
                tv_mis.heading("num", text="#", anchor="w")
                tv_mis.heading("case", text="Case #", anchor="w")
                tv_mis.heading("file", text="Expected Save-As File", anchor="w")
                tv_mis.heading("title", text="Document Title", anchor="w")
                tv_mis.column("num", width=45, stretch=False)
                tv_mis.column("case", width=120, stretch=False)
                tv_mis.column("file", width=340, stretch=True)
                tv_mis.column("title", width=340, stretch=True)

                for idx, (cn, fn, title_str, _) in enumerate(missing_docs, 1):
                    tv_mis.insert("", "end", values=(str(idx), cn, fn, title_str))

                sb_m = ttk.Scrollbar(mis_box, orient="vertical", command=tv_mis.yview)
                tv_mis.configure(yscrollcommand=sb_m.set)
                tv_mis.pack(side="left", fill="both", expand=True, padx=4, pady=4)
                sb_m.pack(side="right", fill="y", pady=4)

            if duplicates:
                dup_box = tk.LabelFrame(
                    content_f, text=f" ⚠️ Duplicates sa Disk ({len(duplicates)} files naulit sa magkaibang folders) ",
                    bg=CARD, fg=WARN_COLOR, font=("Segoe UI", 9, "bold"), bd=1, highlightbackground=WARN_COLOR,
                )
                dup_box.pack(fill="both", expand=True, pady=(0, 6))

                cols_d = ("num", "filename", "paths")
                tv_dup = ttk.Treeview(dup_box, columns=cols_d, show="headings", height=min(4, len(duplicates)))
                tv_dup.heading("num", text="#", anchor="w")
                tv_dup.heading("filename", text="Duplicate File", anchor="w")
                tv_dup.heading("paths", text="Locations", anchor="w")
                tv_dup.column("num", width=45, stretch=False)
                tv_dup.column("filename", width=320, stretch=False)
                tv_dup.column("paths", width=480, stretch=True)

                for idx, (fn, plist) in enumerate(duplicates, 1):
                    locs = " | ".join(p.parent.name for p in plist)
                    tv_dup.insert("", "end", values=(str(idx), fn, locs))

                sb_d = ttk.Scrollbar(dup_box, orient="vertical", command=tv_dup.yview)
                tv_dup.configure(yscrollcommand=sb_d.set)
                tv_dup.pack(side="left", fill="both", expand=True, padx=4, pady=4)
                sb_d.pack(side="right", fill="y", pady=4)

            if corrupted_files:
                cor_box = tk.LabelFrame(
                    content_f, text=f" ⚠️ Corrupted / 0-Byte / Incomplete Files ({len(corrupted_files)}) ",
                    bg=CARD, fg=ERR_COLOR, font=("Segoe UI", 9, "bold"), bd=1, highlightbackground=ERR_COLOR,
                )
                cor_box.pack(fill="x", pady=(0, 6))
                for cp in corrupted_files[:5]:
                    tk.Label(cor_box, text=f"  • {cp.name} ({cp.stat().st_size} bytes) in {cp.parent.name}", bg=CARD, fg=ERR_COLOR, font=("Segoe UI", 8), anchor="w").pack(fill="x")
                if len(corrupted_files) > 5:
                    tk.Label(cor_box, text=f"  ... and {len(corrupted_files) - 5} more broken files", bg=CARD, fg=MUTED, font=("Segoe UI", 8), anchor="w").pack(fill="x")

        # Bottom Bar
        btm_bar = tk.Frame(win, bg=CARD, padx=14, pady=10)
        btm_bar.pack(fill="x", side="bottom")

        tk.Button(
            btm_bar, text="📁 Open Downloads Folder", command=self.open_downloads_folder,
            bg=CARD, fg=TEXT, activebackground=LINE, activeforeground=TEXT,
            relief="flat", bd=0, padx=12, pady=4, font=("Segoe UI", 9, "bold"), cursor="hand2",
        ).pack(side="left")

        def _copy_audit_report():
            lines = [f"=== NYSCEF DOWNLOADS AUDIT REPORT: {month_display} ==="]
            lines.append(f"Folder: {m_dir}")
            lines.append(f"Expected: {total_expected} | On Disk: {total_found} | Missing: {len(missing_docs)} | Duplicates: {len(duplicates)}")
            if missing_docs:
                lines.append("\n[MISSING BRIEFS]")
                for cn, fn, t, _ in missing_docs:
                    lines.append(f"  • {cn}: {fn} ({t})")
            if duplicates:
                lines.append("\n[DUPLICATE FILES ON DISK]")
                for fn, plist in duplicates:
                    locs = ", ".join(p.parent.name for p in plist)
                    lines.append(f"  • {fn} in: {locs}")
            self.clipboard_clear()
            self.clipboard_append("\n".join(lines))
            self.note.configure(text="Audit report copied to clipboard!")

        tk.Button(
            btm_bar, text="📋 Copy Full Report", command=_copy_audit_report,
            bg=CARD, fg=TEXT, activebackground=LINE, activeforeground=TEXT,
            relief="flat", bd=0, padx=12, pady=4, font=("Segoe UI", 9), cursor="hand2",
        ).pack(side="left", padx=8)

        if missing_docs and self.download_handler:
            def _dl_missing_only():
                missing_items = [it[3] for it in missing_docs if it[3]]
                if missing_items:
                    win.destroy()
                    self.download_handler(missing_items)

            tk.Button(
                btm_bar,
                text=f"⬇ Download Missing PDFs Only ({len(missing_docs)})",
                command=_dl_missing_only,
                bg=ACCENT, fg="#111111", activebackground="#38bdf8", activeforeground="#111111",
                relief="flat", bd=0, padx=14, pady=4, font=("Segoe UI", 9, "bold"), cursor="hand2",
            ).pack(side="left", padx=8)

        tk.Button(
            btm_bar, text="Close", command=win.destroy,
            bg=LINE, fg=TEXT, activebackground=LINE, activeforeground=TEXT,
            relief="flat", bd=0, padx=16, pady=4, font=("Segoe UI", 9, "bold"), cursor="hand2",
        ).pack(side="right")

    def all_brief_pdf_items(self) -> list[tuple[int, int, str, str, str, str, bool]]:
        """Return flattened list of (idx, total, date_words, case_no, caption, brief_title, is_done)."""
        vis = self.visible_rows()
        flat_raw = [
            (format_date_words(r.date_tag), r.case_number, r.caption, b_title, self.store.is_done(r.key))
            for r in vis
            for b_title in (r.brief_titles or ([r.briefs] if r.briefs else []))
        ]
        total = len(flat_raw)
        return [(i, total, d, c, cap, b, done) for i, (d, c, cap, b, done) in enumerate(flat_raw, 1)]

    def show_all_pdfs(self) -> None:
        items = self.all_brief_pdf_items()
        win = tk.Toplevel(self)
        win.title("All Brief PDFs to Download")
        win.configure(bg=BG)
        win.geometry("880x460")
        total_pdfs = len(items)
        vis_cases = len(self.visible_rows())
        rem_pdfs = sum(1 for *_, done in items if not done)
        tk.Label(
            win,
            text=f"Total Brief PDFs to Download: {total_pdfs} PDF(s) across {vis_cases} Hit Case(s)   •   Remaining: {rem_pdfs} PDF(s)",
            bg=BG,
            fg=ACCENT,
            font=("Segoe UI", 11, "bold"),
            anchor="w",
        ).pack(fill="x", padx=20, pady=(16, 10))
        frame = tk.Frame(win, bg=BG)
        frame.pack(fill="both", expand=True, padx=20)
        cols = ("num", "done", "date", "case", "caption", "brief")
        tv = ttk.Treeview(frame, columns=cols, show="headings")
        for col, lbl, w, st in (
            ("num", "PDF #", 65, False),
            ("done", "Done", 50, False),
            ("date", "Date", 135, False),
            ("case", "Case #", 95, False),
            ("caption", "Case Title", 260, True),
            ("brief", "Brief PDF Document", 220, True),
        ):
            tv.heading(col, text=lbl, anchor="w")
            tv.column(col, width=w, anchor="w", stretch=st)
        for i, total, d, c, cap, b, done in items:
            tv.insert("", "end", values=(f"{i:02d}/{total:02d}", "✔" if done else "", d, c, cap, b))
        sb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        bot = tk.Frame(win, bg=BG)
        bot.pack(fill="x", padx=20, pady=12)
        def _copy_list():
            lines = [f"[{i:02d}/{total:02d}] {d} | {c} | {cap} -> {b}" for i, total, d, c, cap, b, _ in items]
            self.clipboard_clear()
            self.clipboard_append("\n".join(lines))
            self.update()
            self.note.configure(text=f"Copied {total_pdfs} Brief PDF lines")
        self._btn(bot, f"Copy All {total_pdfs} Brief PDF Lines", _copy_list).pack(side="left")
        self._btn(bot, "Close", win.destroy).pack(side="right")

    # ---------- actions ----------
    def _on_tree_click(self, event) -> None:
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self.tree.identify_column(event.x)
        row_id = self.tree.identify_row(event.y)
        if col == "#1" and row_id:
            if row_id in self.checked_keys:
                self.checked_keys.discard(row_id)
            else:
                self.checked_keys.add(row_id)
            self._fill()
            if self.tree.exists(row_id):
                self.tree.selection_set(row_id)

    def toggle_check_all(self) -> None:
        vis_keys = [r.key for r in self.visible_rows()]
        if not vis_keys:
            return
        all_checked = all(k in self.checked_keys for k in vis_keys)
        for k in vis_keys:
            if all_checked:
                self.checked_keys.discard(k)
            else:
                self.checked_keys.add(k)
        self._fill()

    def select_all(self) -> str:
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def toggle_selected(self) -> None:
        targets = list(self.checked_keys) if self.checked_keys else list(self.tree.selection())
        for iid in targets:
            self.store.toggle(iid)
        self.checked_keys.clear()
        self._fill()

    def toggle_all(self) -> None:
        vis = self.visible_rows()
        if not vis:
            return
        mark_done = not all(self.store.is_done(r.key) for r in vis)
        self.store.set_many([r.key for r in vis], mark_done)
        self._fill()
        action = "Marked" if mark_done else "Unmarked"
        self.note.configure(text=f"{action} all {len(vis)} case(s)")

    def _copy(self, numbers: list[str]) -> None:
        unique = list(dict.fromkeys(numbers))
        if not unique:
            self.note.configure(text="Nothing to copy")
            return
        self.clipboard_clear()
        self.clipboard_append("\n".join(unique))
        self.update()
        self.note.configure(text=f"Copied {len(unique)} case #")

    def copy_selected(self) -> None:
        targets = list(self.checked_keys) if self.checked_keys else list(self.tree.selection())
        by_key = {r.key: r.case_number for r in self.rows}
        self._copy([by_key[i] for i in targets if i in by_key])

    def copy_all(self) -> None:
        self._copy([r.case_number for r in self.visible_rows()])
