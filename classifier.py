from dataclasses import dataclass
from typing import Any


STATUS_HIT_VALID = "HIT_VALID"
STATUS_SEALED_OR_UNCLICKABLE = "SEALED_OR_UNCLICKABLE"
STATUS_NO_BRIEF_FOUND = "NO_BRIEF_FOUND"
STATUS_EMPTY_NO_CASE = "EMPTY_NO_CASE"
STATUS_NEEDS_MANUAL_CHECK = "NEEDS_MANUAL_CHECK"


@dataclass(frozen=True)
class BriefLink:
    title: str
    url: str
    row_summary: str


COLLECTIBLE_PLEADING_MOTION_TERMS = (
    "PETITION FOR REVIEW",
    "PETITION FOR REHEARING",
    "PETITION FOR WRIT OF MANDAMUS",
    "WRIT OF MANDAMUS",
    "PETITION FOR WRIT OF CERTIORARI",
    "WRIT OF CERTIORARI",
    "PETITION FOR WRIT OF HABEAS CORPUS",
    "WRIT OF HABEAS CORPUS",
    "MOTION FOR REHEARING",
    "MOTION FOR REVIEW",
    "MOTION FOR RECONSIDERATION",
)


def is_collectible_doc_text(text_up: str) -> bool:
    """Return True if text matches Briefs or the specific collectible Pleadings/Motions."""
    if "CONFIRMATION NOTICE" in text_up:
        return False
    if "BRIEF" in text_up:
        return True
    if "ORDER" in text_up:
        return False
    return any(term in text_up for term in COLLECTIBLE_PLEADING_MOTION_TERMS)


def is_valid_brief_anchor(anchor: dict[str, Any]) -> bool:
    """
    Determine whether an anchor inside a DocumentList table row represents a
    clickable collectible document (Brief, qualifying Petition, or qualifying Motion)
    rather than ancillary links like 'Confirmation Notice'.
    """
    text = (anchor.get("text") or "").strip()
    href = (anchor.get("href") or "").strip()
    onclick = (anchor.get("onclick") or "").strip()

    text_up = text.upper()
    href_up = href.upper()

    if not is_collectible_doc_text(text_up):
        return False
    if "VIEWDOCUMENT" in href_up:
        return True
    if onclick and href_up not in ("", "#"):
        return True
    return False


def extract_clickable_briefs(brief_rows: list[dict[str, Any]]) -> tuple[list[BriefLink], int]:
    """
    Inspect rows extracted from NYSCEF DocumentList containing collectible documents.
    Returns (valid_clickable_briefs, unclickable_brief_row_count).
    """
    valid_briefs: list[BriefLink] = []
    unclickable_rows = 0
    seen_urls: set[str] = set()

    for row in brief_rows:
        row_text = (row.get("text") or "").strip()
        anchors = row.get("links") or []

        row_valid_links: list[BriefLink] = []
        for anchor in anchors:
            if not is_valid_brief_anchor(anchor):
                continue
            url = (anchor.get("href") or "").strip()
            title = (anchor.get("text") or "").strip()
            dedup_key = f"{title.upper()}::{url}"
            if dedup_key in seen_urls:
                continue
            seen_urls.add(dedup_key)
            row_valid_links.append(BriefLink(title=title, url=url, row_summary=row_text))

        if row_valid_links:
            valid_briefs.extend(row_valid_links)
        elif is_collectible_doc_text(row_text.upper()):
            has_any_viewdoc = any("VIEWDOCUMENT" in (a.get("href") or "").upper() for a in anchors)
            if not has_any_viewdoc:
                # Plain-text collectible row with zero clickable ViewDocument links (e.g. Matrimonial)
                unclickable_rows += 1

    return valid_briefs, unclickable_rows


def classify_docket(
    brief_rows: list[dict[str, Any]],
    case_type: str = "",
) -> tuple[str, list[BriefLink], str]:
    """
    Classify a loaded NYSCEF DocumentList docket into one of the NY workflow states.
    Returns (status, clickable_briefs, note).
    """
    valid_briefs, unclickable_rows = extract_clickable_briefs(brief_rows)

    if valid_briefs:
        note = f"{len(valid_briefs)} clickable brief document(s)"
        if unclickable_rows > 0:
            note += f" (+{unclickable_rows} non-clickable brief row(s))"
        return STATUS_HIT_VALID, valid_briefs, note

    if unclickable_rows > 0:
        is_matrimonial = "MATRIMONIAL" in case_type.upper()
        reason = "Sealed / Matrimonial plain-text brief (0 links)" if is_matrimonial else "Brief listed without clickable document link"
        return STATUS_SEALED_OR_UNCLICKABLE, [], reason

    return STATUS_NO_BRIEF_FOUND, [], "Docket found, no BRIEF documents filed"
