import re

MONTH_NAMES = (
    "",
    "JANUARY",
    "FEBRUARY",
    "MARCH",
    "APRIL",
    "MAY",
    "JUNE",
    "JULY",
    "AUGUST",
    "SEPTEMBER",
    "OCTOBER",
    "NOVEMBER",
    "DECEMBER",
)


def format_date_words(value: str) -> str:
    """
    Format a date string like '2026-07-31' or '07-31-2026' as 'JULY 31, 2026'.
    Returns the input unchanged if no valid date pattern is found.
    """
    raw = (value or "").strip()
    m_iso = re.search(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)", raw)
    if m_iso:
        yyyy, mm, dd = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        if 1 <= mm <= 12 and 1 <= dd <= 31:
            return f"{MONTH_NAMES[mm]} {dd}, {yyyy}"
    m_us = re.search(r"(?<!\d)(\d{2})[-/](\d{2})[-/](\d{4})(?!\d)", raw)
    if m_us:
        mm, dd, yyyy = int(m_us.group(1)), int(m_us.group(2)), int(m_us.group(3))
        if 1 <= mm <= 12 and 1 <= dd <= 31:
            return f"{MONTH_NAMES[mm]} {dd}, {yyyy}"
    return raw


def format_month_words(ym: str) -> str:
    """Format '2026-07' as 'JULY 2026'."""
    raw = (ym or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})$", raw)
    if m:
        yyyy, mm = int(m.group(1)), int(m.group(2))
        if 1 <= mm <= 12:
            return f"{MONTH_NAMES[mm]} {yyyy}"
    return raw


def short_case_type(case_type: str) -> str:
    cleaned = (case_type or "").strip()
    cleaned = re.sub(r"^Appellate Division\s*-\s*2nd Dept\s*\|\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def parse_row_meta(title: str, row_summary: str) -> dict[str, str]:
    """
    Extract clean Doc #, Filed Date, and special flags (like *Corrected* or Replacement Brief)
    while stripping messy NYSCEF row noise ('Processed Confirmation Notice', 'Received: ...').
    """
    text = (row_summary or "").strip()
    doc_num = ""
    m_doc = re.match(r"^\s*(\d+)\b", text)
    if m_doc:
        doc_num = f"Doc #{m_doc.group(1)}"

    filed_date = ""
    m_filed = re.search(r"Filed:\s*(\d{2}/\d{2}/\d{4})", text, re.IGNORECASE)
    if m_filed:
        filed_date = f"Filed {m_filed.group(1)}"

    tags: list[str] = []
    upper = text.upper()
    if "*CORRECTED*" in upper or ("CORRECTED" in upper and "CORRECTED" not in title.upper()):
        tags.append("Corrected")
    if "REPLACEMENT BRIEF" in upper:
        tags.append("Replacement")
    if "AMENDED" in upper and "AMENDED" not in title.upper():
        tags.append("Amended")
    if "*** PENDING ***" in upper:
        tags.append("Pending")

    return {
        "doc_num": doc_num,
        "filed_date": filed_date,
        "flag": " • ".join(tags),
    }


SHARED_CSS = """
:root {
  --bg: #111111;
  --card: #1a1a1a;
  --line: #2a2a2a;
  --text: #ededed;
  --muted: #8a8a8a;
  --accent: #4ade80;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: "Segoe UI", system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  padding: 32px 20px 56px;
  line-height: 1.45;
}
.wrap { max-width: 860px; margin: 0 auto; }

.topbar { display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; margin-bottom: 20px; }
.title { margin: 0; font-size: 1.2rem; font-weight: 600; }
.subtitle { color: var(--muted); font-size: 0.85rem; margin-top: 3px; }
.actions-bar { display: flex; gap: 10px; align-items: center; }
.progress-pill { color: var(--muted); font-size: 0.85rem; }
.progress-pill strong { color: var(--accent); }

.kpi-strip { display: flex; gap: 28px; flex-wrap: wrap; padding: 14px 0; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); margin-bottom: 20px; }
.kpi-val { font-size: 1.3rem; font-weight: 600; }
.kpi-lbl { font-size: 0.78rem; color: var(--muted); }
.kpi-val.green { color: var(--accent); }
.kpi-val.warn { color: var(--text); }

.tabs { display: flex; gap: 20px; margin-bottom: 16px; flex-wrap: wrap; }
.tab-btn { background: none; border: none; border-bottom: 2px solid transparent; color: var(--muted); padding: 6px 0; font-size: 0.92rem; cursor: pointer; }
.tab-btn:hover { color: var(--text); }
.tab-btn.active { color: var(--text); border-bottom-color: var(--accent); }
.tab-panel { display: none; }
.tab-panel.active { display: block; }

.toolbar { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; margin-bottom: 14px; }
.search-input { flex: 1 1 240px; background: var(--card); border: 1px solid var(--line); color: var(--text); border-radius: 6px; padding: 8px 12px; font-size: 0.88rem; outline: none; }
.search-input:focus { border-color: var(--accent); }
.toggle-lbl { display: inline-flex; align-items: center; gap: 6px; font-size: 0.85rem; color: var(--muted); cursor: pointer; }

.btn { background: var(--card); color: var(--text); border: 1px solid var(--line); border-radius: 6px; padding: 6px 12px; font-size: 0.83rem; cursor: pointer; }
.btn:hover { border-color: var(--accent); }
.btn:disabled { opacity: 0.4; cursor: not-allowed; pointer-events: none; }
.btn-primary { border-color: var(--accent); color: var(--accent); }
.btn-copied { background: var(--accent) !important; border-color: var(--accent) !important; color: #111 !important; }

.hit-card { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px; margin-bottom: 10px; }
.hit-card.is-done { opacity: 0.4; }
.hit-card.is-done .case-num { text-decoration: line-through; }
.hit-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
.hit-left { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.done-chk { width: 17px; height: 17px; accent-color: var(--accent); cursor: pointer; }
.case-num { font-family: Consolas, monospace; font-size: 1.1rem; font-weight: 600; color: var(--accent); }
.type-pill, .dup-pill { font-size: 0.78rem; color: var(--muted); }
.caption { margin: 6px 0 10px 27px; font-size: 0.92rem; }
.brief-rows { margin-left: 27px; display: flex; flex-direction: column; gap: 4px; }
.brief-row { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; padding: 6px 0; border-top: 1px solid var(--line); }
.brief-title-wrap { display: flex; align-items: center; gap: 8px; }
.brief-badge { font-size: 0.82rem; font-weight: 600; }
.flag-badge { font-size: 0.75rem; color: var(--muted); border: 1px solid var(--line); padding: 0 6px; border-radius: 4px; }
.brief-meta { font-size: 0.8rem; color: var(--muted); }

.sub-filters { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 12px; }
.chip-btn { background: none; color: var(--muted); border: 1px solid var(--line); border-radius: 999px; padding: 4px 11px; font-size: 0.8rem; cursor: pointer; }
.chip-btn.active { color: var(--text); border-color: var(--accent); }
.table-wrap { border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 500; font-size: 0.78rem; }
tr:last-child td { border-bottom: none; }
.mono { font-family: Consolas, monospace; white-space: nowrap; }
.link { color: var(--accent); text-decoration: none; }
.link:hover { text-decoration: underline; }
.pill { font-size: 0.78rem; color: var(--muted); }
.pill-done { color: var(--accent); }
.pill-part { color: var(--text); }
.pill-pend { color: var(--muted); }
.date-section { margin-top: 22px; }
.date-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
.date-header h3 { margin: 0; font-size: 0.95rem; font-weight: 600; color: var(--muted); }
.empty-box { border: 1px dashed var(--line); border-radius: 8px; padding: 24px; text-align: center; color: var(--muted); }
"""


SHARED_JS = """
const _memStore = {};
function _safeReadStore(key) {
  try {
    return JSON.parse(localStorage.getItem(key) || "{}");
  } catch (e) {
    return _memStore[key] || {};
  }
}
function _safeWriteStore(key, obj) {
  _memStore[key] = obj;
  try {
    localStorage.setItem(key, JSON.stringify(obj));
  } catch (e) {}
}

function _markCopied(btn) {
  const orig = btn.innerText;
  btn.innerText = "Copied ✓";
  btn.classList.add("btn-copied");
  setTimeout(() => {
    btn.innerText = orig;
    btn.classList.remove("btn-copied");
  }, 1300);
}

function _fallbackCopy(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try {
    document.execCommand("copy");
  } finally {
    document.body.removeChild(ta);
  }
}

function copyFromAttr(btn) {
  const text = btn.getAttribute("data-copy") || "";
  if (!text) return;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => _markCopied(btn)).catch(() => {
      _fallbackCopy(text);
      _markCopied(btn);
    });
  } else {
    _fallbackCopy(text);
    _markCopied(btn);
  }
}

function switchTab(tabId, btn) {
  document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
  document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
  const panel = document.getElementById(tabId);
  if (panel) panel.classList.add("active");
  if (btn) btn.classList.add("active");
}

function initChecklist(storageKey) {
  const saved = _safeReadStore(storageKey);
  const cards = Array.from(document.querySelectorAll(".hit-card"));
  const counterEl = document.getElementById("done-counter");
  const emptyFilterEl = document.getElementById("empty-filter-msg");

  function refresh() {
    let doneCount = 0;
    let visibleCount = 0;
    const hideDone = document.getElementById("hide-done")?.checked;
    const q = (document.getElementById("case-search")?.value || "").trim().toLowerCase();

    cards.forEach(card => {
      const id = card.getAttribute("data-key");
      const chk = card.querySelector(".done-chk");
      const isDone = Boolean(saved[id]);
      if (chk) chk.checked = isDone;
      card.classList.toggle("is-done", isDone);
      if (isDone) doneCount++;

      const hay = (card.getAttribute("data-search") || "").toLowerCase();
      const matchSearch = !q || hay.includes(q);
      const matchHide = !hideDone || !isDone;
      const show = matchSearch && matchHide;
      card.style.display = show ? "" : "none";
      if (show) visibleCount++;
    });

    document.querySelectorAll(".date-section").forEach(sec => {
      const visibleCards = Array.from(sec.querySelectorAll(".hit-card")).some(c => c.style.display !== "none");
      sec.style.display = visibleCards ? "" : "none";
    });

    if (emptyFilterEl) {
      emptyFilterEl.style.display = (cards.length > 0 && visibleCount === 0) ? "block" : "none";
    }
    if (counterEl) counterEl.innerHTML = `<strong>${doneCount}</strong> / ${cards.length} downloaded`;
  }

  cards.forEach(card => {
    const id = card.getAttribute("data-key");
    const chk = card.querySelector(".done-chk");
    if (chk) {
      chk.addEventListener("change", () => {
        if (chk.checked) saved[id] = true;
        else delete saved[id];
        _safeWriteStore(storageKey, saved);
        refresh();
      });
    }
  });

  document.getElementById("case-search")?.addEventListener("input", refresh);
  document.getElementById("hide-done")?.addEventListener("change", refresh);
  refresh();
}

function filterSkipped(kind, btn) {
  document.querySelectorAll(".chip-btn").forEach(b => b.classList.remove("active"));
  if (btn) btn.classList.add("active");
  document.querySelectorAll("#skipped-tbody tr").forEach(tr => {
    const rowKind = tr.getAttribute("data-kind") || "";
    tr.style.display = (kind === "all" || rowKind === kind) ? "" : "none";
  });
}
"""
