from dataclasses import dataclass, field
from pathlib import Path
import re
import pymupdf

from config import CASE_NUMBER_REGEX


@dataclass(frozen=True)
class ParsedMotionList:
    source_name: str
    page_count: int
    raw_match_count: int
    cases: list[str]
    motion_date_hint: str
    case_titles: dict[str, str] = field(default_factory=dict)


def normalize_case_number(raw: str) -> str:
    """Strip optional +N motion suffixes (e.g. '2024-10878+1' -> '2024-10878')."""
    cleaned = raw.strip()
    match = re.match(r"^(\d{4}-\d{5})", cleaned)
    return match.group(1) if match else cleaned


def extract_pdf_case_titles(full_text: str) -> dict[str, str]:
    """
    Extract the PDF 'Title' column (e.g. 'Peo v Antonucci, Ashley') paired with each
    Case Number in an Appellate Division 2nd Dept Motion List PDF.
    """
    titles: dict[str, str] = {}
    lines = [ln.replace("\ufffd", "'").strip() for ln in full_text.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        m_inline = re.match(r"^(.*\S)\s+(\d{4}-\d{5})(?:\s*\+\d+)?$", ln)
        if m_inline:
            title_cand = m_inline.group(1).strip()
            if title_cand not in ("Title", "Case Number", "Document"):
                titles.setdefault(m_inline.group(2), title_cand)
            continue
        m_own = re.match(r"^(\d{4}-\d{5})(?:\s*\+\d+)?$", ln)
        if m_own and i > 0:
            prev = lines[i - 1]
            if not re.match(r"^M\d+\.pdf$", prev, re.IGNORECASE) and prev not in ("Title", "Case Number", "Document"):
                titles.setdefault(m_own.group(1), prev)
    return titles


def _extract_date_hint(source_name: str, full_text: str) -> str:
    file_match = re.search(r"(\d{2}-\d{2}-\d{4})", source_name)
    if file_match:
        return file_match.group(1)
    header_match = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}\b",
        full_text[:1500],
        re.IGNORECASE,
    )
    return header_match.group(0) if header_match else "unknown_date"


def parse_motion_pdf_bytes(pdf_bytes: bytes, source_name: str) -> ParsedMotionList:
    if not pdf_bytes or not pdf_bytes.lstrip().startswith(b"%PDF"):
        raise ValueError(f"Invalid PDF content received for '{source_name}'.")

    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        full_text = "\n".join(page.get_text("text") for page in doc)
        page_count = len(doc)
    finally:
        doc.close()

    raw_matches = re.findall(CASE_NUMBER_REGEX, full_text)
    seen: set[str] = set()
    unique_cases: list[str] = []
    for raw in raw_matches:
        normalized = normalize_case_number(raw)
        if normalized not in seen:
            seen.add(normalized)
            unique_cases.append(normalized)

    return ParsedMotionList(
        source_name=source_name,
        page_count=page_count,
        raw_match_count=len(raw_matches),
        cases=unique_cases,
        motion_date_hint=_extract_date_hint(source_name, full_text),
        case_titles=extract_pdf_case_titles(full_text),
    )


def parse_motion_pdf_file(pdf_path: Path | str) -> ParsedMotionList:
    path = Path(pdf_path)
    return parse_motion_pdf_bytes(path.read_bytes(), path.name)
