import calendar
import csv
import datetime as dt
import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from config import DOWNLOADS_DIR, DOWNLOADS_KY_DIR, OUTPUT_DIR, STOP_EVENT, verify_app_integrity
from ky_xlsx import write_ky_tracker_xlsx
from ui_theme import MONTH_NAMES, format_date_words, format_month_words

KY_BASE_URL = "https://appellatepublic.kycourts.net"
KY_API_V1 = f"{KY_BASE_URL}/api/api/v1"

KY_COA_NAME = "Kentucky Court of Appeals"
KY_SUP_NAME = "Kentucky Supreme Court"

# Bump whenever collection/naming rules change so stale saved scan results are never reused.
KY_LOGIC_VERSION = 6

KY_INTERN_NAMES: list[str] = [
    "Bearneza, John Edward",
    "Castro, Kim Clarence",
    "Castro, Windlyn",
    "Crisostomo, Kyle David",
    "Deloterio, Arjun",
    "Gunce, Blaze Arianne",
    "Laurente, Christille",
    "Oñang, Carl Nathaniel",
    "Sangacena, Krystal Laura",
    "Santiago, Rashmil",
]

KY_COURTS = (
    ("KYCOA", KY_COA_NAME),
    ("KYSUP", KY_SUP_NAME),
    ("BOTH", "Both Courts (KYCOA + KYSUP)"),
)

KY_DOWNLOAD_LOG_FIELDS = [
    "Downloaded At",
    "Download Date",
    "Court",
    "Docket Number",
    "Document Name",
    "Document Type",
    "Filed Date",
    "Saved Filename",
    "Saved Path",
]
_KY_DOWNLOAD_HISTORY_LOCK = threading.Lock()

KY_COLLECTIBLE_PLEADINGS_MOTIONS = (
    ("PETITION FOR DISCRETIONARY REVIEW", "PetitionForDiscretionaryReview"),
    ("PETITION FOR REVIEW", "PetitionForReview"),
    ("PETITION FOR REHEARING", "PetitionForRehearing"),
    ("WRIT OF MANDAMUS", "PetitionForWritOfMandamus"),
    ("WRIT OF CERTIORARI", "PetitionForWritOfCertiorari"),
    ("WRIT OF HABEAS CORPUS", "PetitionForWritOfHabeasCorpus"),
    ("MOTION FOR DISCRETIONARY REVIEW", "MotionForDiscretionaryReview"),
    ("MOTION FOR REHEARING", "MotionForRehearing"),
    ("MOTION FOR REVIEW", "MotionForReview"),
    ("MOTION FOR RECONSIDERATION", "MotionForReconsideration"),
    ("MOTION TO RECONSIDER", "MotionForReconsideration"),
)

KY_EXCLUDED_DOC_TERMS = (
    "RECEIPT NOTICE",
    "NOTICE",
    "COVER PAGE",
    "ENVELOPE",
    "APPENDIX",
    "APPENDEX",
    "RAP 5(E)",
    "CONFIRMATION",
    "MOTION FOR EXTENSION",
    "MOTION FOR ADDITIONAL TIME",
    "MOTION FOR ENLARGEMENT",
    "MOTION TO DISMISS",
    "MOTION TO STRIKE",
    "MOTION TO SUPPLEMENT",
    "MOTION FOR LEAVE",
)


@dataclass
class KyCollectibleDoc:
    document_id: str
    document_name: str
    docket_entry_id: str
    filed_date: str
    sub_type: str
    submitter: str = ""
    save_as_name: str = ""
    download_url: str = ""


@dataclass
class KySearchRow:
    row_id: str = ""
    case_id: str = ""
    case_number: str = ""
    court: str = ""
    doc_type: str = ""
    sub_type: str = ""
    filed_date_iso: str = ""
    filed_date_display: str = ""
    document_name: str = ""
    matching_text: str = ""


@dataclass
class KyCaseRecord:
    case_number: str
    case_id: str
    court: str
    title: str
    first_filed_date: str
    search_sub_types: list[str] = field(default_factory=list)
    status: str = "PENDING"
    collectible_docs: list[KyCollectibleDoc] = field(default_factory=list)
    receipt_notice_count: int = 0
    note: str = ""
    checked_at: str = ""
    review_note: str = ""

    @property
    def case_url(self) -> str:
        return f"{KY_BASE_URL}/case/{self.case_id}"


def format_ky_submitter_surname_first(raw: str) -> str:
    """
    Format submitter names as 'Surname, First Middle' to match C-Track's 'Submitted By' column
    (e.g. 'Mark Francis Sommer' -> 'Sommer, Mark Francis';
    'Richard William Bertelson III' -> 'Bertelson III, Richard William').
    """
    if not raw:
        return ""
    suffixes = {"JR", "JR.", "SR", "SR.", "II", "III", "IV", "V"}
    parts_out: list[str] = []
    for chunk in raw.split(";"):
        name = " ".join(chunk.strip().split())
        if not name or "," in name:
            if name:
                parts_out.append(name)
            continue
        tokens = name.split(" ")
        if len(tokens) < 2:
            parts_out.append(name)
            continue
        if len(tokens) >= 3 and tokens[-1].upper().rstrip(",") in suffixes:
            surname = f"{tokens[-2]} {tokens[-1]}"
            given = " ".join(tokens[:-2])
        else:
            surname = tokens[-1]
            given = " ".join(tokens[:-1])
        parts_out.append(f"{surname}, {given}" if given else surname)
    return "; ".join(parts_out)


def court_code_for(court: str, case_number: str = "") -> str:
    c_up = (court or "").upper()
    num_up = (case_number or "").upper()
    if "SUPREME" in c_up or "-SC-" in num_up or "KYSUP" in c_up:
        return "KYSUP"
    return "KYCOA"


def is_ky_clerk_receipt_text(text: str) -> bool:
    """
    True when a document's extracted text is the Clerk's one-page 'RECEIPT NOTICE' letter.
    Some of these letters are named exactly like the brief (e.g. 'APPELLANT BRIEF FILED (DPA)'),
    and scanned OCR may misread 'NOTICE' as 'NOTlCE'. Scanned PDFs with no text return False.
    """
    head = " ".join((text or "").split()).upper()
    if not head:
        return False
    has_clerk = "OFFICE OF" in head[:80] and "CLERK" in head[:120]
    has_receipt = (
        "RECEIPT NOT" in head[:900]
        or "THE DOCUMENT LISTED BELOW HAS BEEN FILED" in head[:900]
    )
    return has_clerk and has_receipt


def is_ky_collectible_doc(doc_name: str, parent_type: str = "", parent_subtype: str = "") -> bool:
    """
    Return True if a Kentucky C-Track popup document is a collectible Brief, Pleading, or Motion.
    Excludes non-collectible items such as 'RECEIPT NOTICE', 'RAP 5(E) ENVELOPE', and Orders.
    """
    up = (doc_name or "").upper().strip()
    if not up:
        return False
    if any(term in up for term in KY_EXCLUDED_DOC_TERMS):
        return False
    if "ORDER" in up or any(w in up for w in ("DENIED", "GRANTED", "DISMISSED", "DISMISSING")):
        return False

    for phrase, _camel in KY_COLLECTIBLE_PLEADINGS_MOTIONS:
        if phrase in up:
            return True

    if "BRIEF" in up:
        return True

    sub_up = f"{parent_type} {parent_subtype}".upper()
    for phrase, _camel in KY_COLLECTIBLE_PLEADINGS_MOTIONS:
        if phrase in sub_up and ("PETITION" in up or "WRIT" in up or "MOTION" in up):
            return True
    return False


def is_ky_briefing_search_row(doc_type: str, sub_type: str = "", doc_name: str = "") -> bool:
    """
    Return True when a C-Track Document Search row belongs to Briefing or any collectible
    Pleading/Motion from the SOP List of Documents to Collect, excluding Court Orders,
    Opinions, Dispositions, Minute Sheets, Calendars, and Receipt Notices.
    """
    sub_doc = f"{(sub_type or '').upper()} {(doc_name or '').upper()}".strip()
    if any(term in sub_doc for term in KY_EXCLUDED_DOC_TERMS):
        return False
    if any(
        bad in sub_doc
        for bad in (
            "ORDER",
            "OPINION",
            "DISPOSITION",
            "MINUTE SHEET",
            "CALENDAR",
            "SHOW CAUSE",
            "OVERDUE",
            "DENIED",
            "GRANTED",
            "DISMISSED",
            "DISMISSING",
            "AFFIRMING",
            "REVERSING",
            "VACATING",
        )
    ):
        return False
    dt_up = (doc_type or "").upper().strip()
    if "BRIEF" in dt_up:
        return True
    return is_ky_collectible_doc(sub_type or doc_name, doc_type, sub_type)


KY_VOL_RE = re.compile(r"\bVOL(?:UME)?\.?\s*([0-9]+|[IVXLCDM]+)\b", re.IGNORECASE)


def ky_doc_volume_number(name: str) -> str | None:
    """Return volume identifier string (e.g. '1', '2', 'II') if doc name contains a volume indicator."""
    if not name:
        return None
    m = KY_VOL_RE.search(name)
    return m.group(1).upper() if m else None


def _vol_to_int(val: str) -> int:
    if val.isdigit():
        return int(val)
    roman_map = {
        "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5,
        "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10,
    }
    return roman_map.get(val.upper(), 0)


def has_ky_multi_volume(docs: list[KyCollectibleDoc]) -> bool:
    """
    Return True when collected documents represent an active multi-volume brief situation:
    - Multiple documents with volume numbers (e.g. Vol 1 and Vol 2..7), OR
    - Any document labeled Volume 2 or higher.
    """
    if not docs:
        return False
    vols: list[str] = []
    for d in docs:
        v = ky_doc_volume_number(d.document_name) or ky_doc_volume_number(d.sub_type)
        if v:
            vols.append(v)
    if len(vols) > 1:
        return True
    return any(_vol_to_int(v) >= 2 for v in vols)


def ky_brief_camel_type(doc_name: str, sub_type: str = "") -> str:
    """Map a Kentucky collectible document description to the SOP CamelCase suffix."""
    combined = f"{doc_name or ''} {sub_type or ''}".upper()
    doc_up = (doc_name or "").upper()

    for phrase, camel in KY_COLLECTIBLE_PLEADINGS_MOTIONS:
        if phrase in doc_up or phrase in combined:
            return camel

    if "COMBINED" in combined and "APPELLEE" in combined and "CROSS-APPELLANT" in combined:
        return "CombinedAppelleeCrossAppellantBrief"
    if "COMBINED" in combined and "REPLY" in combined and "CROSS-APPELLEE" in combined:
        return "CombinedReplyCrossAppelleeBrief"
    if "COMBINED" in combined and "APPELLANT" in combined:
        return "CombinedAppellantBrief"
    if "CROSS-APPELLANT" in combined and "REPLY" in combined:
        return "CrossAppellantReplyBrief"
    if "CROSS-REPLY" in combined or "CROSS REPLY" in combined:
        return "CrossAppellantReplyBrief"
    if "CROSS-APPELLANT" in combined:
        return "CrossAppellantBrief"
    if "CROSS-APPELLEE" in combined:
        return "CrossAppelleeBrief"
    if "APPELLANT" in combined and "REPLY" in combined:
        return "AppellantReplyBrief"
    if "APPELLEE" in combined and "REPLY" in combined:
        return "AppelleeReplyBrief"
    if "REPLY" in combined and "BRIEF" in combined:
        # Plain 'REPLY BRIEF FILED' is the appellant's reply; the team's SOP example
        # saves exactly this docket entry as KYSUP_2023-SC-0228_AppellantReplyBrief.
        return "AppellantReplyBrief"
    if "APPELLANT" in combined and "BRIEF" in combined:
        return "AppellantBrief"
    if "APPELLEE" in combined and "BRIEF" in combined:
        return "AppelleeBrief"
    if "AMICUS" in combined:
        return "AmicusBrief"
    if "SUPPLEMENTAL" in combined:
        return "SupplementalBrief"
    if "ANDERS" in combined:
        return "AndersBrief"
    if "PETITIONER" in combined and "BRIEF" in combined:
        return "PetitionerBrief"
    if "RESPONDENT" in combined and "BRIEF" in combined:
        return "RespondentBrief"

    cleaned = re.sub(r"\bFILED\b", "", doc_up)
    words = re.findall(r"[A-Z0-9]+", cleaned)
    return "".join(w.capitalize() for w in words) or "Brief"


def assign_ky_save_as_names(court: str, case_number: str, docs: list[KyCollectibleDoc]) -> list[str]:
    """
    Assign SOP Save-As filenames: [KYCOA|KYSUP]_[CaseNumber]_[BriefType].
    When multiple documents in the same case share the same BriefType, number them 1, 2, 3...
    """
    code = court_code_for(court, case_number)
    base_types = [ky_brief_camel_type(d.document_name, d.sub_type) for d in docs]
    counts: dict[str, int] = {}
    for bt in base_types:
        counts[bt] = counts.get(bt, 0) + 1

    seen: dict[str, int] = {}
    names: list[str] = []
    for doc, bt in zip(docs, base_types):
        if counts[bt] > 1:
            idx = seen.get(bt, 0) + 1
            seen[bt] = idx
            name = f"{code}_{case_number}_{bt}{idx}"
        else:
            name = f"{code}_{case_number}_{bt}"
        doc.save_as_name = name
        names.append(name)
    return names


def _utc_to_us_eastern(utc_dt: dt.datetime) -> dt.datetime:
    """Convert a timezone-aware UTC datetime to US Eastern Time (America/New_York) without tzdata."""
    u = utc_dt.astimezone(dt.timezone.utc).replace(tzinfo=None)
    y = u.year
    mar1_w = dt.date(y, 3, 1).weekday()
    second_sun_mar = 8 + ((6 - mar1_w) % 7)
    nov1_w = dt.date(y, 11, 1).weekday()
    first_sun_nov = 1 + ((6 - nov1_w) % 7)
    dst_start_utc = dt.datetime(y, 3, second_sun_mar, 7, 0, 0)
    dst_end_utc = dt.datetime(y, 11, first_sun_nov, 6, 0, 0)
    offset_hours = -4 if dst_start_utc <= u < dst_end_utc else -5
    return u + dt.timedelta(hours=offset_hours)


def _parse_ctrack_date(raw_iso: str) -> tuple[str, str]:
    """Convert C-Track UTC ISO string to America/New_York ('YYYY-MM-DD', 'MM/DD/YYYY')."""
    if not raw_iso:
        return "", ""
    try:
        norm = raw_iso.replace("+0000", "+00:00").replace("Z", "+00:00")
        parsed = _utc_to_us_eastern(dt.datetime.fromisoformat(norm))
        return parsed.strftime("%Y-%m-%d"), parsed.strftime("%m/%d/%Y")
    except Exception:
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw_iso)
        if m:
            y, mo, d = m.group(1), m.group(2), m.group(3)
            return f"{y}-{mo}-{d}", f"{mo}/{d}/{y}"
        return raw_iso[:10], raw_iso[:10]


def _api_get_json(url: str, headers: dict[str, str] | None = None, timeout: int = 15, retries: int = 2):
    """GET JSON with exponential backoff on transient failures (resets, timeouts, 429/5xx)."""
    req_headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
    if headers:
        req_headers.update(headers)
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=req_headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            if exc.code < 500 and exc.code != 429:
                raise
            last_exc: Exception = exc
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            last_exc = exc
        if attempt == retries or STOP_EVENT.is_set():
            raise last_exc
        STOP_EVENT.wait(1.5 * (2 ** attempt))
    raise last_exc  # pragma: no cover


def fetch_ky_month_search_rows(court_code: str, year_month: str) -> list[KySearchRow]:
    """Fetch Document Search rows for a court ('KYCOA', 'KYSUP', or 'BOTH') and 'YYYY-MM'."""
    verify_app_integrity()
    if court_code == "BOTH":
        rows = fetch_ky_month_search_rows("KYCOA", year_month)
        rows.extend(fetch_ky_month_search_rows("KYSUP", year_month))
        rows.sort(key=lambda r: r.filed_date_iso, reverse=True)
        return rows

    court_name = KY_SUP_NAME if court_code == "KYSUP" else KY_COA_NAME
    year, month = [int(x) for x in year_month.split("-")]
    last_day = calendar.monthrange(year, month)[1]
    start_dt = dt.date(year, month, 1)
    end_dt = dt.date(year, month, last_day) + dt.timedelta(days=1)

    params = {
        "queryString": "true",
        "searchFilters[0].indexFieldName": "caseHeader.court",
        "searchFilters[0].operation": "=",
        "searchFilters[0].values[0]": court_name,
        "searchFields[0].indexFieldName": "parentDate",
        "searchFields[0].operation": ">=",
        "searchFields[0].values[0]": start_dt.strftime("%m/%d/%Y"),
        "searchFields[1].indexFieldName": "parentDate",
        "searchFields[1].operation": "<=",
        "searchFields[1].values[0]": end_dt.strftime("%m/%d/%Y"),
    }
    qs = urllib.parse.urlencode(params)
    url = f"{KY_API_V1}/publicaccessdocuments/search?{qs}"

    matched: list[KySearchRow] = []
    start_idx = 1
    page_size = 500
    while not STOP_EVENT.is_set():
        data = _api_get_json(
            url,
            headers={
                "X-CTrack-Paging-StartIndex": str(start_idx),
                "X-CTrack-Paging-MaxResults": str(page_size),
                "X-CTrack-Paging-CalculateTotalCount": "true",
                "X-CTrack-Paging-OrderBy": "parentDate desc",
            },
        )
        results = data.get("resultItems") or data.get("results") or []
        if not results:
            break
        for item in results:
            rm = item.get("rowMap") or {}
            iso_date, us_date = _parse_ctrack_date(str(rm.get("parentDate") or ""))
            if not iso_date.startswith(year_month):
                continue
            doc_type = str(rm.get("parentType") or "").strip()
            sub_type = str(rm.get("parentSubtype") or "").strip()
            doc_name = str(rm.get("documentName") or "").strip()
            if not is_ky_briefing_search_row(doc_type, sub_type, doc_name):
                continue
            matched.append(
                KySearchRow(
                    row_id=str(item.get("id") or ""),
                    case_id=str(rm.get("caseHeader.caseID") or ""),
                    case_number=str(rm.get("caseHeader.caseNumber") or "").strip(),
                    court=str(rm.get("caseHeader.court") or court_name).strip(),
                    doc_type=doc_type,
                    sub_type=sub_type,
                    filed_date_iso=iso_date,
                    filed_date_display=us_date,
                    document_name=doc_name,
                )
            )
        if len(results) < page_size:
            break
        start_idx += page_size
    return matched


def scan_ky_case(case_id: str, case_number: str, court: str = "", first_filed_date: str = "",
                 search_sub_types: list[str] | None = None, year_month: str | None = None) -> KyCaseRecord:
    """Inspect a Kentucky case's full Docket Entries and popup Documents via the C-Track API."""
    if STOP_EVENT.is_set():
        raise InterruptedError(f"Scan interrupted by user before inspecting case {case_number}")
    case_info = _api_get_json(f"{KY_API_V1}/cases/{case_id}")
    title = " ".join(str(case_info.get("shortTitle") or "").split())
    resolved_court = str(case_info.get("court") or court or KY_COA_NAME).strip()

    entries: list[dict] = []
    seen_entry_ids: set[str] = set()
    page_size, start_idx = 200, 1
    for _ in range(25):
        if STOP_EVENT.is_set():
            raise InterruptedError(f"Scan interrupted by user while inspecting case {case_number}")
        page = _api_get_json(
            f"{KY_API_V1}/cases/{case_id}/docketentries",
            headers={"X-CTrack-Paging-StartIndex": str(start_idx), "X-CTrack-Paging-MaxResults": str(page_size)},
        ) or []
        fresh = [e for e in page if str(e.get("docketEntryID")) not in seen_entry_ids]
        entries.extend(fresh)
        seen_entry_ids.update(str(e.get("docketEntryID")) for e in fresh)
        if len(page) < page_size or not fresh:
            break
        start_idx += page_size

    collectible_docs: list[KyCollectibleDoc] = []
    seen_doc_ids: set[str] = set()
    seen_doc_signatures: set[tuple[str, str, str]] = set()
    receipt_count = 0
    review_entries: list[str] = []

    for entry in entries:
        if STOP_EVENT.is_set():
            raise InterruptedError(f"Scan interrupted by user while inspecting case {case_number}")
        if not entry.get("hasDocuments"):
            continue
        de_id = str(entry.get("docketEntryID") or "")
        de_type = str(entry.get("docketEntryType") or "").strip()
        de_sub = str(entry.get("docketEntrySubtype") or "").strip()
        de_date, de_date_us = _parse_ctrack_date(str(entry.get("filedDate") or ""))

        submitters = entry.get("submittedBy") or []
        submitter_str = "; ".join(
            format_ky_submitter_surname_first(str(s.get("sortName") or s.get("displayName") or "").strip())
            for s in submitters if isinstance(s, dict)
        )

        docs = _api_get_json(
            f"{KY_API_V1}/publicaccessdocuments?filter=parentCategory=docketentries,parentID={de_id}"
        )
        collected_from_entry = 0
        skipped_names: list[str] = []
        for doc in docs or []:
            if not doc.get("documentAvailable", True):
                continue
            doc_id = str(doc.get("documentID") or "")
            if doc_id and doc_id in seen_doc_ids:
                continue
            doc_name = str(doc.get("documentName") or doc.get("documentDescription") or "").strip()
            doc_text = " ".join(str(t) for t in (doc.get("documentText") or []))
            if "RECEIPT NOTICE" in doc_name.upper() or is_ky_clerk_receipt_text(doc_text):
                receipt_count += 1
                continue
            if is_ky_collectible_doc(doc_name, de_type, de_sub):
                norm_txt = " ".join(doc_text.split())
                if len(norm_txt) >= 200:
                    sig = (de_date, doc_name.upper(), norm_txt)
                    if sig in seen_doc_signatures:
                        collected_from_entry += 1
                        continue
                    seen_doc_signatures.add(sig)
                if doc_id:
                    seen_doc_ids.add(doc_id)
                collected_from_entry += 1
                collectible_docs.append(
                    KyCollectibleDoc(
                        document_id=doc_id,
                        document_name=doc_name,
                        docket_entry_id=de_id,
                        filed_date=de_date,
                        sub_type=de_sub or de_type,
                        submitter=submitter_str,
                        download_url=f"{KY_API_V1}/publicaccessdocuments/{doc_id}/download",
                    )
                )
            else:
                skipped_names.append(doc_name)

        # A Brief docket entry whose attached files were all rejected must be checked by a human.
        sub_up = de_sub.upper()
        is_brief_entry = "BRIEF" in sub_up and not any(
            t in sub_up for t in ("ORDER", "MOTION", "EXTENSION", "ADDITIONAL TIME", "DEFICIEN")
        )
        if is_brief_entry and collected_from_entry == 0:
            files = ", ".join(skipped_names) if skipped_names else "receipt notice only"
            review_entries.append(f"{de_date_us} {de_sub} (files: {files})")

    # Number duplicates oldest-first regardless of the order the API returned entries in.
    collectible_docs.reverse()
    collectible_docs.sort(key=lambda d: d.filed_date)
    assign_ky_save_as_names(resolved_court, case_number, collectible_docs)

    if collectible_docs:
        status = "BRIEF_FOUND"
        names_preview = ", ".join(d.save_as_name for d in collectible_docs)
        note = f"{len(collectible_docs)} collectible PDF(s): {names_preview}"
        if has_ky_multi_volume(collectible_docs):
            review_entries.append("Multi-Volume brief (check if Vol 2+ are attachments/depositions)")
    elif receipt_count > 0:
        status = "RECEIPT_ONLY"
        note = f"Popup had {receipt_count} RECEIPT NOTICE file(s) only — do not collect"
    else:
        status = "NO_BRIEF"
        note = "No collectible brief/pleading/motion PDF in docket entries"
    review_note = "; ".join(review_entries)
    if review_note:
        note += f" | CHECK MANUALLY: {review_note}"

    return KyCaseRecord(
        case_number=case_number,
        case_id=case_id,
        court=resolved_court,
        title=title,
        first_filed_date=first_filed_date,
        search_sub_types=search_sub_types or [],
        status=status,
        collectible_docs=collectible_docs,
        receipt_notice_count=receipt_count,
        note=note,
        checked_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        review_note=review_note,
    )


def ky_output_dir(court_code: str, year_month: str) -> Path:
    d = OUTPUT_DIR / f"KY_{court_code}_{year_month}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ky_downloads_dir(court_code: str, year_month: str, date_str: str | None = None) -> Path:
    # Use human-friendly month words (e.g. KYCOA_NOVEMBER_2026) so users can clearly see the month name
    m_words = format_month_words(year_month).replace(" ", "_")
    d = DOWNLOADS_KY_DIR / f"{court_code}_{m_words}"
    if date_str:
        clean_date = format_date_words(date_str) if any(c.isdigit() for c in date_str) else date_str
        safe_d = re.sub(r'[<>:"/\\|?*]+', "_", clean_date).strip()
        expected_prefix = m_words.split("_")[0].upper()
        if safe_d and safe_d.upper().startswith(expected_prefix):
            d = d / safe_d
    d.mkdir(parents=True, exist_ok=True)
    return d


def ky_download_history_path(court_code: str, year_month: str) -> Path:
    """Return the internal append-only history used to rebuild the Excel tracker."""
    return ky_output_dir(court_code, year_month) / "download_history.jsonl"


def _append_ky_download_history(
    history_path: Path,
    doc: KyCollectibleDoc,
    saved_path: Path,
    court_code: str,
    case_number: str,
    case_id: str,
    downloaded_at: dt.datetime | None = None,
) -> None:
    """Persist one successful download event so later days never replace it."""
    history_path.parent.mkdir(parents=True, exist_ok=True)
    downloaded_at = (downloaded_at or dt.datetime.now()).astimezone()
    event = {
        "downloaded_at": downloaded_at.isoformat(timespec="seconds"),
        "download_date": downloaded_at.strftime("%Y-%m-%d"),
        "court_code": court_code,
        "case_number": case_number,
        "case_id": case_id,
        "document_name": doc.document_name,
        "sub_type": doc.sub_type,
        "filed_date": _normalize_iso_date(doc.filed_date),
        "document_id": doc.document_id,
        "download_url": doc.download_url or f"{KY_API_V1}/publicaccessdocuments/{doc.document_id}/download",
        "saved_filename": saved_path.name,
        "saved_path": str(saved_path),
    }
    with _KY_DOWNLOAD_HISTORY_LOCK:
        with history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")


def record_ky_download_history(
    court_code: str,
    year_month: str,
    doc: KyCollectibleDoc,
    saved_path: Path,
    case_number: str,
    case_id: str = "",
    downloaded_at: dt.datetime | None = None,
) -> None:
    """Record a new download or import an existing PDF using its file timestamp."""
    _append_ky_download_history(
        ky_download_history_path(court_code, year_month),
        doc,
        saved_path,
        court_code,
        case_number,
        case_id,
        downloaded_at,
    )


def load_ky_download_tracker_rows(
    court_code: str,
    year_month: str,
    intern_name: str | None = None,
    search_rows: list[KySearchRow] | None = None,
    records: dict[str, KyCaseRecord] | None = None,
) -> list[dict[str, str]]:
    """
    Build Excel tracker rows from actual successful downloads.
    Strictly aligns with the assigned month's C-Track Document Search rows
    (e.g. July 2024), so all Filed Dates in Column F match the assigned month (puro 7),
    while attributing the full count of all collected in-case PDFs to Column I on the first row.
    """
    history_path = ky_download_history_path(court_code, year_month)
    if not history_path.exists():
        return []

    target_intern = intern_name if intern_name is not None else DEFAULT_KY_INTERN_NAME
    downloaded_cases: dict[str, dict[str, str]] = {}
    downloaded_counts: dict[str, int] = {}
    with _KY_DOWNLOAD_HISTORY_LOCK:
        with history_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    event = json.loads(line)
                    cn = str(event.get("case_number") or "").strip()
                    dt_val = str(event.get("download_date") or "").strip()
                    fd_val = str(event.get("filed_date") or "").strip()
                    st_val = str(event.get("sub_type") or "").strip()
                    dn_val = str(event.get("document_name") or "").strip()
                    if cn:
                        downloaded_counts[cn] = downloaded_counts.get(cn, 0) + 1
                        if cn not in downloaded_cases:
                            downloaded_cases[cn] = {"download_date": dt_val, "filed_date": fd_val, "sub_type": st_val, "document_name": dn_val}
                        else:
                            if dt_val:
                                current_dt = downloaded_cases[cn].get("download_date", "")
                                if not current_dt or dt_val < current_dt:
                                    downloaded_cases[cn]["download_date"] = dt_val
                            if fd_val and fd_val > downloaded_cases[cn].get("filed_date", ""):
                                downloaded_cases[cn]["filed_date"] = fd_val
                                if st_val:
                                    downloaded_cases[cn]["sub_type"] = st_val
                                if dn_val:
                                    downloaded_cases[cn]["document_name"] = dn_val
                except (json.JSONDecodeError, TypeError):
                    continue

    if not downloaded_cases:
        return []

    if records is None:
        records = load_ky_checkpoint(court_code, year_month)
    if search_rows is None:
        search_rows = load_ky_search_rows(court_code, year_month, records)

    seen_keys: set[tuple[str, str, str, str]] = set()
    dl_search_rows: list[KySearchRow] = []
    case_found_in_search: set[str] = set()

    for sr in search_rows:
        cn = sr.case_number
        if cn not in downloaded_cases:
            continue
        sub_doc = f"{(sr.sub_type or '')} {(sr.document_name or '')}".upper()
        if any(term in sub_doc for term in KY_EXCLUDED_DOC_TERMS) or "RECEIPT" in sub_doc or "NOTICE" in sub_doc:
            continue
        iso_d = _normalize_iso_date(sr.filed_date_iso) or _normalize_iso_date(sr.filed_date_display) or ""
        if year_month and not iso_d.startswith(year_month):
            continue
        key = (cn, iso_d, (sr.sub_type or "").strip(), (sr.row_id or sr.document_name or "").strip())
        if key in seen_keys:
            continue
        seen_keys.add(key)
        dl_search_rows.append(sr)
        case_found_in_search.add(cn)

    for cn, info in downloaded_cases.items():
        if cn not in case_found_in_search:
            rec = records.get(cn)
            case_date = getattr(rec, "first_filed_date", "") if rec else ""
            iso_d = _normalize_iso_date(info.get("filed_date")) or _normalize_iso_date(case_date) or ""
            if year_month and not (iso_d and iso_d.startswith(year_month)):
                iso_d = _normalize_iso_date(case_date) if (case_date and (_normalize_iso_date(case_date) or "").startswith(year_month)) else f"{year_month}-01"
            st = info.get("sub_type") or "APPELLANT BRIEF FILED"
            dn = info.get("document_name") or st
            dl_search_rows.append(KySearchRow(
                case_number=cn,
                court=court_code,
                doc_type="CA - BRIEFING",
                sub_type=st,
                filed_date_iso=iso_d,
                filed_date_display=iso_d,
                document_name=dn,
            ))

    if dl_search_rows:
        rows = build_ky_tracker_rows(
            dl_search_rows,
            records,
            intern_name=target_intern,
            collection_date="",
            year_month=year_month,
        )
        for r in rows:
            docket = r["DOCKET NUMBER"].removeprefix("Go to case")
            if docket in downloaded_cases and downloaded_cases[docket]["download_date"]:
                r["DATE OF COLLECTION"] = downloaded_cases[docket]["download_date"]
            if (not records or docket not in records) and r.get("No. of collected documents") != "0":
                if docket in downloaded_counts:
                    r["No. of collected documents"] = str(downloaded_counts[docket])
            if not r.get("Document Name"):
                r["Document Name"] = r.get("SUB TYPE") or ""
        return rows

    # Fallback when search rows are not available (e.g. isolated unit tests)
    rows: list[dict[str, str]] = []
    seen_documents: set[tuple[str, str]] = set()
    with _KY_DOWNLOAD_HISTORY_LOCK:
        with history_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                case_number = str(event.get("case_number") or "").strip()
                if not case_number:
                    continue
                document_id = str(event.get("document_id") or "").strip()
                saved_filename = str(event.get("saved_filename") or "").lower()
                document_key = (case_number, document_id or saved_filename)
                if document_key in seen_documents:
                    continue

                filed_date_val = str(event.get("filed_date") or "").strip()
                iso_filed = _normalize_iso_date(filed_date_val)

                seen_documents.add(document_key)
                case_id = str(event.get("case_id") or "").strip()
                case_url = f"{KY_BASE_URL}/case/{case_id}" if case_id else ""
                doc_url = str(event.get("download_url") or "").strip()
                rows.append({
                    "INTERN NAME": target_intern,
                    "DATE OF COLLECTION": str(event.get("download_date") or ""),
                    "DOCKET NUMBER": f"Go to case{case_number}",
                    "DOCUMENT TYPE": "CA - BRIEFING",
                    "SUB TYPE": str(event.get("sub_type") or ""),
                    "FILED DATE": iso_filed or filed_date_val,
                    "Document Name": str(event.get("document_name") or ""),
                    "Matching Document Text": "Viewthe document" if doc_url or document_id else "",
                    "No. of collected documents": "0",
                    "_case_url": case_url,
                    "_doc_url": doc_url,
                    "_document_id": document_id,
                    "_saved_filename": saved_filename,
                })

    rows.sort(
        key=lambda r: (
            _normalize_iso_date(r.get("FILED DATE") or "") or "",
            r.get("DOCKET NUMBER") or "",
        ),
        reverse=True,
    )
    case_counts: dict[str, int] = {}
    for r in rows:
        docket = r["DOCKET NUMBER"].removeprefix("Go to case")
        case_counts[docket] = case_counts.get(docket, 0) + 1

    seen_cases: set[str] = set()
    for r in rows:
        docket = r["DOCKET NUMBER"].removeprefix("Go to case")
        count = case_counts.get(docket, 1)
        if docket not in seen_cases:
            seen_cases.add(docket)
            r["No. of collected documents"] = str(count)
        else:
            r["No. of collected documents"] = "0"
        if not r.get("Document Name"):
            r["Document Name"] = r.get("SUB TYPE") or ""

    return rows


def download_ky_pdf(
    doc: KyCollectibleDoc,
    dest_dir: Path,
    overwrite: bool = False,
    timeout: int = 60,
    retries: int = 4,
    court_code: str = "",
    case_number: str = "",
    year_month: str = "",
    case_id: str = "",
) -> Path:
    """
    Download a collectible Kentucky brief PDF directly from C-Track and save it
    using its exact SOP Save-As filename (e.g. 'KYCOA_2026-CA-0236_AppellantBrief.pdf').
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_stem = re.sub(r'[<>:"/\\|?*]+', "_", (doc.save_as_name or doc.document_id or "Brief").strip())
    if safe_stem.lower().endswith(".pdf"):
        safe_stem = safe_stem[:-4]
    target = dest_dir / f"{safe_stem}.pdf"
    if not overwrite and target.exists() and target.stat().st_size >= 16:
        return target

    url = doc.download_url or f"{KY_API_V1}/publicaccessdocuments/{doc.document_id}/download"
    req_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/pdf,application/octet-stream,*/*",
        "Referer": "https://appellatepublic.kycourts.net/",
    }
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=req_headers)
        part = target.with_suffix(".pdf.part")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            if not data.startswith(b"%PDF"):
                raise ValueError(f"C-Track response for {doc.save_as_name} is not a valid PDF")
            part.write_bytes(data)
            part.replace(target)
            if court_code and case_number and year_month:
                try:
                    record_ky_download_history(
                        court_code, year_month, doc, target, case_number, case_id,
                    )
                except OSError:
                    # Keep the successful PDF download successful if history storage is unavailable.
                    pass
            return target
        except urllib.error.HTTPError as exc:
            if exc.code < 500 and exc.code != 429:
                raise
            last_exc = exc
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
            last_exc = exc
        finally:
            if part.exists():
                try:
                    part.unlink()
                except OSError:
                    pass
        if attempt == retries or STOP_EVENT.is_set():
            assert last_exc is not None
            raise last_exc
        STOP_EVENT.wait(1.5 * (2 ** attempt))
    assert last_exc is not None
    raise last_exc  # pragma: no cover


def load_ky_checkpoint(court_code: str, year_month: str) -> dict[str, KyCaseRecord]:
    p = ky_output_dir(court_code, year_month) / "checkpoint.json"
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if raw.get("logic_version") != KY_LOGIC_VERSION:
            return {}  # saved under older rules: force a fresh scan instead of trusting stale results
        saved_sr = raw.get("search_rows")
        allowed_cases: set[str] | None = None
        sr_dates: dict[str, str] = {}
        sr_subtypes: dict[str, list[str]] = {}
        if isinstance(saved_sr, list) and saved_sr:
            allowed_cases = set()
            for sr_item in saved_sr:
                dt_val = str(sr_item.get("doc_type") or "")
                st_val = str(sr_item.get("sub_type") or "")
                dn_val = str(sr_item.get("document_name") or "")
                if not is_ky_briefing_search_row(dt_val, st_val, dn_val):
                    continue
                cn_val = str(sr_item.get("case_number") or "").strip()
                if not cn_val:
                    continue
                allowed_cases.add(cn_val)
                if cn_val not in sr_dates:
                    sr_dates[cn_val] = str(sr_item.get("filed_date_display") or sr_item.get("filed_date_iso") or "")
                subs = sr_subtypes.setdefault(cn_val, [])
                if st_val and st_val not in subs:
                    subs.append(st_val)

        out: dict[str, KyCaseRecord] = {}
        for cn, item in (raw.get("records") or {}).items():
            if allowed_cases is not None and cn not in allowed_cases:
                continue
            raw_docs = [KyCollectibleDoc(**d) for d in (item.get("collectible_docs") or [])]
            for d in raw_docs:
                d.submitter = format_ky_submitter_surname_first(d.submitter)
            docs = [d for d in raw_docs if is_ky_collectible_doc(d.document_name, d.sub_type)]
            crt = item.get("court", "")
            assign_ky_save_as_names(crt, item.get("case_number", cn), docs)
            status = item.get("status", "PENDING")
            note = item.get("note", "")
            if status == "BRIEF_FOUND" and not docs:
                status = "NO_BRIEF"
                note = "No collectible brief/pleading/motion PDF in docket entries"
            elif docs:
                status = "BRIEF_FOUND"
                note = f"{len(docs)} collectible PDF(s): {', '.join(d.save_as_name for d in docs)}"
            if item.get("review_note"):
                note += f" | CHECK MANUALLY: {item['review_note']}"
            raw_subs = sr_subtypes.get(cn) or [
                st for st in (item.get("search_sub_types") or []) if is_ky_briefing_search_row("", str(st))
            ]
            out[cn] = KyCaseRecord(
                case_number=item.get("case_number", cn),
                case_id=item.get("case_id", ""),
                court=crt,
                title=" ".join(str(item.get("title", "")).split()),
                first_filed_date=sr_dates.get(cn) or item.get("first_filed_date", ""),
                search_sub_types=list(raw_subs),
                status=status,
                collectible_docs=docs,
                receipt_notice_count=int(item.get("receipt_notice_count") or 0),
                note=note,
                checked_at=item.get("checked_at", ""),
                review_note=str(item.get("review_note") or ""),
            )
        return out
    except Exception:
        return {}


DEFAULT_KY_INTERN_NAME = ""

KY_TRACKER_FIELDNAMES: list[str] = [
    "INTERN NAME",
    "DATE OF COLLECTION",
    "DOCKET NUMBER",
    "DOCUMENT TYPE",
    "SUB TYPE",
    "FILED DATE",
    "Document Name",
    "Matching Document Text",
    "No. of collected documents",
]


def _normalize_iso_date(raw_date: str) -> str:
    s = (raw_date or "").strip()
    if not s:
        return ""
    if "/" in s:
        parts = s.split("/")
        if len(parts) == 3:
            try:
                return f"{parts[2]}-{int(parts[0]):02d}-{int(parts[1]):02d}"
            except Exception:
                return s
    return s


def build_ky_tracker_rows(search_rows: list[KySearchRow],
                          records: dict[str, KyCaseRecord],
                          intern_name: str = DEFAULT_KY_INTERN_NAME,
                          collection_date: str | None = None,
                          year_month: str | None = None) -> list[dict[str, str]]:
    """
    Build rows matching the team's Kentucky CSV/Excel tracker (KY - COA TRACKER).
    Format has exactly 9 columns:
      1. INTERN NAME (e.g. Bearneza, John Edward )
      2. DATE OF COLLECTION (ISO YYYY-MM-DD on first row only, blank on subsequent rows)
      3. DOCKET NUMBER (starts with 'Go to case', e.g. Go to case2026-CA-1093)
      4. DOCUMENT TYPE (e.g. CA - BRIEFING)
      5. SUB TYPE (e.g. APPELLANT BRIEF FILED)
      6. FILED DATE (ISO YYYY-MM-DD)
      7. Document Name (e.g. APPELLANT BRIEF FILED)
      8. Matching Document Text (e.g. Viewthe document)
      9. No. of collected documents (count of collectible PDFs on first occurrence, 0 on duplicates)
    """
    today_iso = collection_date or dt.datetime.now().strftime("%Y-%m-%d")
    target_intern = intern_name if intern_name is not None else DEFAULT_KY_INTERN_NAME
    filtered_rows = [
        sr for sr in search_rows if is_ky_briefing_search_row(sr.doc_type, sr.sub_type, sr.document_name)
    ]
    if year_month:
        filtered_rows = [
            sr for sr in filtered_rows
            if (sr.filed_date_iso and sr.filed_date_iso.startswith(year_month))
            or (sr.filed_date_display and sr.filed_date_display.startswith(year_month))
        ]
    # Sort descending by Filed Date (e.g. Day 31/30 down to Day 1) per team tracking SOP
    filtered_rows.sort(
        key=lambda sr: (
            _normalize_iso_date(sr.filed_date_iso) or _normalize_iso_date(sr.filed_date_display) or "",
            sr.case_number or "",
        ),
        reverse=True,
    )
    seen_cases: set[str] = set()
    rows_out: list[dict[str, str]] = []
    for idx, sr in enumerate(filtered_rows):
        rec = records.get(sr.case_number)
        matching_docs = list(rec.collectible_docs) if rec else []
        if rec and sr.case_number not in seen_cases:
            seen_cases.add(sr.case_number)
            count = len(matching_docs)
        else:
            count = 0

        raw_cn = (sr.case_number or "").strip()
        if raw_cn.startswith("Go to case"):
            docket_no = raw_cn
        else:
            docket_no = f"Go to case{raw_cn}" if raw_cn else ""

        filed_date = _normalize_iso_date(sr.filed_date_iso) or _normalize_iso_date(sr.filed_date_display)

        col_date = today_iso

        doc_type = (sr.doc_type or "").strip()
        if not doc_type or doc_type in ("BRIEF", "BRIEFING") or "BRIEF" in (sr.sub_type or "").upper():
            if not doc_type or not doc_type.startswith("CA - "):
                doc_type = "CA - BRIEFING"

        # Case URL for Column C
        case_id = (sr.case_id or (rec.case_id if rec else "")).strip()
        case_url = f"{KY_BASE_URL}/case/{case_id}" if case_id else ""

        # Document name for Column G
        doc_name = (sr.document_name or sr.sub_type or "").strip()

        # Document download / view URL for Column H
        doc_id = (sr.row_id or "").strip()
        if not doc_id and rec and matching_docs:
            for cdoc in matching_docs:
                if cdoc.sub_type == sr.sub_type or cdoc.document_name == doc_name:
                    doc_id = cdoc.document_id
                    break
            if not doc_id:
                doc_id = matching_docs[0].document_id

        doc_url = f"{KY_API_V1}/publicaccessdocuments/{doc_id}/download" if doc_id else ""
        match_text = "Viewthe document" if doc_url else ""

        rows_out.append({
            "INTERN NAME": target_intern,
            "DATE OF COLLECTION": col_date,
            "DOCKET NUMBER": docket_no,
            "DOCUMENT TYPE": doc_type,
            "SUB TYPE": (sr.sub_type or "").strip(),
            "FILED DATE": filed_date,
            "Document Name": doc_name,
            "Matching Document Text": match_text,
            "No. of collected documents": "0",
            "_case_url": case_url,
            "_doc_url": doc_url,
        })

    # Strictly preserve the natural C-Track Document Search order (Day 31 down to Day 1),
    # matching the human intern workflow of copying the web page directly without re-grouping cases.
    seen_cases: set[str] = set()
    for r in rows_out:
        docket = r["DOCKET NUMBER"].removeprefix("Go to case")
        rec = records.get(docket) if records else None
        matching_docs = list(rec.collectible_docs) if rec else []
        count = len(matching_docs) if rec else 1
        if docket not in seen_cases:
            seen_cases.add(docket)
            r["No. of collected documents"] = str(count)
        else:
            r["No. of collected documents"] = "0"

    return rows_out


def load_ky_search_rows(court_code: str, year_month: str,
                        records: dict[str, KyCaseRecord] | None = None) -> list[KySearchRow]:
    """Load cached Document Search rows from checkpoint.json or fallback to kentucky_tracker.csv."""
    out_dir = ky_output_dir(court_code, year_month)
    cp_path = out_dir / "checkpoint.json"
    if cp_path.exists():
        try:
            raw = json.loads(cp_path.read_text(encoding="utf-8"))
            saved_rows = raw.get("search_rows")
            if isinstance(saved_rows, list) and saved_rows:
                return [
                    KySearchRow(**sr)
                    for sr in saved_rows
                    if is_ky_briefing_search_row(
                        str(sr.get("doc_type") or ""),
                        str(sr.get("sub_type") or ""),
                        str(sr.get("document_name") or ""),
                    )
                ]
        except Exception:
            pass

    csv_path = out_dir / "kentucky_tracker.csv"
    if csv_path.exists():
        try:
            with csv_path.open("r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                rows_out: list[KySearchRow] = []
                for idx, row in enumerate(reader):
                    raw_docket = str(row.get("DOCKET NUMBER") or "").strip()
                    cn = raw_docket[len("Go to case"):].strip() if raw_docket.startswith("Go to case") else raw_docket
                    if not cn:
                        continue
                    doc_t = str(row.get("DOCUMENT TYPE") or "CA - BRIEFING").strip()
                    sub_t = str(row.get("SUB TYPE") or "").strip()
                    if not is_ky_briefing_search_row(doc_t, sub_t):
                        continue
                    rec = (records or {}).get(cn)
                    f_raw = str(row.get("FILED DATE") or "").strip()
                    f_iso = _normalize_iso_date(f_raw)
                    if "/" in f_raw:
                        f_disp = f_raw
                    elif "-" in f_iso:
                        parts = f_iso.split("-")
                        f_disp = f"{parts[1]}/{parts[2]}/{parts[0]}" if len(parts) == 3 else f_iso
                    else:
                        f_disp = f_raw
                    rows_out.append(
                        KySearchRow(
                            row_id=str(idx + 1),
                            case_id=rec.case_id if rec else "",
                            case_number=cn,
                            court=rec.court if rec else KY_COA_NAME,
                            doc_type=doc_t,
                            sub_type=sub_t,
                            filed_date_iso=f_iso,
                            filed_date_display=f_disp,
                            document_name="",
                            matching_text="",
                        )
                    )
                if rows_out:
                    return rows_out
        except Exception:
            pass
    return []


def save_ky_checkpoint(court_code: str, year_month: str,
                       records: dict[str, KyCaseRecord],
                       search_rows: list[KySearchRow] | None = None) -> Path:
    out_dir = ky_output_dir(court_code, year_month)
    cp_path = out_dir / "checkpoint.json"
    if search_rows is None:
        search_rows = load_ky_search_rows(court_code, year_month, records) or None
    else:
        search_rows = [
            sr for sr in search_rows if is_ky_briefing_search_row(sr.doc_type, sr.sub_type, sr.document_name)
        ]
    payload: dict = {
        "logic_version": KY_LOGIC_VERSION,
        "court_code": court_code,
        "year_month": year_month,
        "updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "records": {cn: asdict(r) for cn, r in records.items()},
    }
    if search_rows is not None:
        payload["search_rows"] = [asdict(sr) for sr in search_rows]
    cp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    if search_rows is not None:
        tracker_rows = build_ky_tracker_rows(search_rows, records)
        csv_path = out_dir / "kentucky_tracker.csv"
        try:
            with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=KY_TRACKER_FIELDNAMES, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(tracker_rows)
        except PermissionError:
            pass
        month_word = format_month_words(year_month).split()[0] if year_month else "OCTOBER"
        sheet_tab = f"KY - COA TRACKER {month_word}"
        xlsx_name = f"KY - COA TRACKER {month_word}.xlsx"

        # Save internal backup copy inside output directory (never in downloads until user downloads)
        xlsx_path = out_dir / xlsx_name
        try:
            write_ky_tracker_xlsx(xlsx_path, tracker_rows, sheet_tab_name=sheet_tab)
            # Auto-clean any old _latest duplicates in output folder so strictly 1 file exists
            for old_dup in out_dir.glob(f"KY - COA TRACKER {month_word}_latest*.xlsx"):
                try:
                    old_dup.unlink()
                except Exception:
                    pass
        except PermissionError:
            pass
    return cp_path

