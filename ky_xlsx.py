"""
Exact OpenXML (.xlsx) generator for the Kentucky Court of Appeals Tracker.
Matches the team's 'COURT ASSIGNMENT - INTERN' Excel template:
  - Single active sheet: KY - COA TRACKER [MONTH]
  - Row 1: Pink/lavender header row (Calibri 10 Bold, centered, thin borders)
  - Rows 2-3: Two blank bordered separator rows
  - Rows 4+: Data rows with real Excel serial dates (m/d/yyyy), centered cells,
    thin grid borders, and conditional formatting on Column C (DOCKET NUMBER)
    that highlights duplicate docket numbers in light red/pink (#FFC7CE / #9C0006).
"""

import datetime as dt
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

# Initial shared strings (indices 0..38) preserved from the official template
# so Sheet 1 (COURT ASSIGNMENT) and Sheet 2 headers map to their exact indices.
_BASE_SHARED_STRINGS: tuple[str, ...] = (
    "JURISDICTION (STATE)",       # 0
    "COVERAGE",                   # 1
    "Sangacena, Krystal Laura",   # 2
    "Oñang, Carl Nathaniel\xa0",  # 3
    "Crisostomo, Kyle David\xa0", # 4
    "Santiago, Rashmil\xa0",      # 5
    "Deloterio, Arjun\xa0",       # 6
    "Castro, Kim Clarence\xa0",   # 7
    "Gunce, Blaze Arianne\xa0",   # 8
    "Laurente, Christille\xa0",   # 9
    "Bearneza, John Edward\xa0",  # 10
    "Castro, Windlyn",            # 11
    "INTERN",                     # 12
    "DATE OF COLLECTION",         # 13
    "NOTES",                      # 14
    "DATE OF ASSIGNMENT",         # 15
    "CA - BRIEFING",              # 16
    "REPLY BRIEF FILED",          # 17
    "APPELLANT BRIEF FILED",      # 18
    "DOCKET NUMBER",              # 19
    "Go to case2026-CA-1093",     # 20
    "Go to case2026-CA-0559",     # 21
    "Go to case2026-CA-0625",     # 22
    "No. of collected documents", # 23
    "NY 2ND",                     # 24
    "KY COA",                     # 25
    "2026 - MARCH",               # 26
    "2026 - FEB",                 # 27
    "2026 -  APRIL",              # 28
    "2026 -  MAY",                # 29
    "2026 -  JUNE",               # 30
    "INTERN NAME",                # 31
    "DOCUMENT TYPE",              # 32
    "SUB TYPE",                   # 33
    "FILED DATE",                 # 34
    "SAMPLE",                     # 35
    "2026 -  JULY",               # 36
    "Document Name",              # 37
    "Matching Document Text",     # 38
)

_CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/><Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/><Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/><Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/></Types>"""

_ROOT_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/><Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/></Relationships>"""

_WORKBOOK_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" mc:Ignorable="x15 xr xr6 xr10 xr2" xmlns:x15="http://schemas.microsoft.com/office/spreadsheetml/2010/11/main" xmlns:xr="http://schemas.microsoft.com/office/spreadsheetml/2014/revision" xmlns:xr6="http://schemas.microsoft.com/office/spreadsheetml/2016/revision6" xmlns:xr10="http://schemas.microsoft.com/office/spreadsheetml/2016/revision10" xmlns:xr2="http://schemas.microsoft.com/office/spreadsheetml/2015/revision2"><fileVersion appName="xl" lastEdited="7" lowestEdited="7" rupBuild="30430"/><workbookPr defaultThemeVersion="202300"/><bookViews><workbookView activeTab="0" xWindow="-110" yWindow="-110" windowWidth="19420" windowHeight="10300"/></bookViews><sheets><sheet name="KY - COA TRACKER" sheetId="1" r:id="rId1"/></sheets><calcPr calcId="191029"/></workbook>"""

_WORKBOOK_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme1.xml"/><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/><Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>"""

_STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" mc:Ignorable="x14ac x16r2 xr" xmlns:x14ac="http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac" xmlns:x16r2="http://schemas.microsoft.com/office/spreadsheetml/2015/02/main" xmlns:xr="http://schemas.microsoft.com/office/spreadsheetml/2014/revision"><fonts count="5" x14ac:knownFonts="1"><font><sz val="11"/><color theme="1"/><name val="Aptos Narrow"/><family val="2"/><scheme val="minor"/></font><font><b/><sz val="11"/><color theme="1"/><name val="Aptos Narrow"/><family val="2"/><scheme val="minor"/></font><font><b/><sz val="10"/><color theme="1"/><name val="Calibri"/><family val="2"/></font><font><sz val="10"/><color theme="1"/><name val="Calibri"/><family val="2"/></font><font><sz val="10"/><color rgb="0563C1"/><u/><name val="Calibri"/><family val="2"/></font></fonts><fills count="9"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FFFFFF00"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor theme="8" tint="0.79998168889431442"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor theme="7" tint="0.79998168889431442"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor theme="5" tint="0.59999389629810485"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor theme="2" tint="-9.9978637043366805E-2"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor theme="9" tint="0.59999389629810485"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFFFFFFF"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border><border><left style="thin"><color indexed="64"/></left><right style="thin"><color indexed="64"/></right><top style="thin"><color indexed="64"/></top><bottom style="thin"><color indexed="64"/></bottom><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="19"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="0" fontId="1" fillId="3" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="0" fontId="1" fillId="4" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="0" fontId="1" fillId="5" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="0" fontId="1" fillId="6" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="0" fontId="1" fillId="7" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="14" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="0" fontId="2" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="0" fontId="3" fillId="8" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="0" fontId="3" fillId="8" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf><xf numFmtId="14" fontId="3" fillId="8" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf><xf numFmtId="0" fontId="2" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf><xf numFmtId="0" fontId="3" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="14" fontId="3" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="0" fontId="3" fillId="0" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="0" fontId="4" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles><dxfs count="1"><dxf><font><color rgb="FF9C0006"/></font><fill><patternFill><bgColor rgb="FFFFC7CE"/></patternFill></fill></dxf></dxfs><tableStyles count="0" defaultTableStyle="TableStyleMedium2" defaultPivotStyle="PivotStyleLight16"/></styleSheet>"""

_THEME_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Office Theme"><a:themeElements><a:clrScheme name="Office"><a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1><a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="0E2841"/></a:dk2><a:lt2><a:srgbClr val="E8E8E8"/></a:lt2><a:accent1><a:srgbClr val="156082"/></a:accent1><a:accent2><a:srgbClr val="E97132"/></a:accent2><a:accent3><a:srgbClr val="196B24"/></a:accent3><a:accent4><a:srgbClr val="0F9ED5"/></a:accent4><a:accent5><a:srgbClr val="A02B93"/></a:accent5><a:accent6><a:srgbClr val="4EA72E"/></a:accent6><a:hlink><a:srgbClr val="467886"/></a:hlink><a:folHlink><a:srgbClr val="96607D"/></a:folHlink></a:clrScheme><a:fontScheme name="Office"><a:majorFont><a:latin typeface="Aptos Display" panose="02110004020202020204"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont><a:minorFont><a:latin typeface="Aptos Narrow" panose="02110004020202020204"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme><a:fmtScheme name="Office"><a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:gradFill rotWithShape="1"><a:gsLst><a:gs pos="0"><a:schemeClr val="phClr"><a:lumMod val="110000"/><a:satMod val="105000"/><a:tint val="67000"/></a:schemeClr></a:gs><a:gs pos="50000"><a:schemeClr val="phClr"><a:lumMod val="105000"/><a:satMod val="103000"/><a:tint val="73000"/></a:schemeClr></a:gs><a:gs pos="100000"><a:schemeClr val="phClr"><a:lumMod val="105000"/><a:satMod val="109000"/><a:tint val="81000"/></a:schemeClr></a:gs></a:gsLst><a:lin ang="5400000" scaled="0"/></a:gradFill><a:gradFill rotWithShape="1"><a:gsLst><a:gs pos="0"><a:schemeClr val="phClr"><a:satMod val="103000"/><a:lumMod val="102000"/><a:tint val="94000"/></a:schemeClr></a:gs><a:gs pos="50000"><a:schemeClr val="phClr"><a:satMod val="110000"/><a:lumMod val="100000"/><a:shade val="100000"/></a:schemeClr></a:gs><a:gs pos="100000"><a:schemeClr val="phClr"><a:lumMod val="99000"/><a:satMod val="120000"/><a:shade val="78000"/></a:schemeClr></a:gs></a:gsLst><a:lin ang="5400000" scaled="0"/></a:gradFill></a:fillStyleLst><a:lnStyleLst><a:ln w="12700" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:prstDash val="solid"/><a:miter lim="800000"/></a:ln><a:ln w="19050" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:prstDash val="solid"/><a:miter lim="800000"/></a:ln><a:ln w="25400" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:prstDash val="solid"/><a:miter lim="800000"/></a:ln></a:lnStyleLst><a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst><a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"><a:tint val="95000"/><a:satMod val="170000"/></a:schemeClr></a:solidFill><a:gradFill rotWithShape="1"><a:gsLst><a:gs pos="0"><a:schemeClr val="phClr"><a:tint val="93000"/><a:satMod val="150000"/><a:shade val="98000"/><a:lumMod val="102000"/></a:schemeClr></a:gs><a:gs pos="50000"><a:schemeClr val="phClr"><a:tint val="98000"/><a:satMod val="130000"/><a:shade val="90000"/><a:lumMod val="103000"/></a:schemeClr></a:gs><a:gs pos="100000"><a:schemeClr val="phClr"><a:shade val="63000"/><a:satMod val="120000"/></a:schemeClr></a:gs></a:gsLst><a:lin ang="5400000" scaled="0"/></a:gradFill></a:bgFillStyleLst></a:fmtScheme></a:themeElements><a:objectDefaults/><a:extraClrSchemeLst/></a:theme>"""

_APP_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><Application>Microsoft Excel</Application><DocSecurity>0</DocSecurity><ScaleCrop>false</ScaleCrop><HeadingPairs><vt:vector size="2" baseType="variant"><vt:variant><vt:lpstr>Worksheets</vt:lpstr></vt:variant><vt:variant><vt:i4>1</vt:i4></vt:variant></vt:vector></HeadingPairs><TitlesOfParts><vt:vector size="1" baseType="lpstr"><vt:lpstr>KY - COA TRACKER</vt:lpstr></vt:vector></TitlesOfParts><Company></Company><LinksUpToDate>false</LinksUpToDate><SharedDoc>false</SharedDoc><HyperlinksChanged>false</HyperlinksChanged><AppVersion>16.0300</AppVersion></Properties>"""


def date_to_excel_serial(raw_date: str) -> int | None:
    """Convert YYYY-MM-DD or MM/DD/YYYY string into an Excel 1900-system serial integer."""
    s = (raw_date or "").strip()
    if not s:
        return None
    try:
        if "-" in s:
            parts = s.split("-")
            d = dt.date(int(parts[0]), int(parts[1]), int(parts[2]))
        elif "/" in s:
            parts = s.split("/")
            d = dt.date(int(parts[2]), int(parts[0]), int(parts[1]))
        else:
            return None
        return (d - dt.date(1899, 12, 30)).days
    except Exception:
        return None


def write_ky_tracker_xlsx(
    xlsx_path: Path,
    tracker_rows: list[dict[str, str]],
    sheet_tab_name: str = "KY - COA TRACKER",
) -> Path:
    """
    Write a formatted .xlsx file identical to the team's 'COURT ASSIGNMENT - INTERN'
    Excel workbook (with 'KY - COA TRACKER' active, pink headers, 2 blank rows,
    m/d/yyyy date cells, and duplicate Docket Number highlighting).
    """
    strings: list[str] = list(_BASE_SHARED_STRINGS)
    str_to_idx: dict[str, int] = {val: i for i, val in enumerate(strings)}

    def s_idx(text: str) -> int:
        if text in str_to_idx:
            return str_to_idx[text]
        idx = len(strings)
        strings.append(text)
        str_to_idx[text] = idx
        return idx

    sheet_rows_xml: list[str] = [
        # Row 1: Pink/lavender header row
        '<row r="1" spans="1:9" x14ac:dyDescent="0.35">'
        '<c r="A1" s="9" t="s"><v>31</v></c>'
        '<c r="B1" s="9" t="s"><v>13</v></c>'
        '<c r="C1" s="9" t="s"><v>19</v></c>'
        '<c r="D1" s="9" t="s"><v>32</v></c>'
        '<c r="E1" s="9" t="s"><v>33</v></c>'
        '<c r="F1" s="9" t="s"><v>34</v></c>'
        '<c r="G1" s="13" t="s"><v>37</v></c>'
        '<c r="H1" s="9" t="s"><v>38</v></c>'
        '<c r="I1" s="9" t="s"><v>23</v></c>'
        '</row>',
    ]

    hyperlinks_xml: list[str] = []
    rels_xml: list[str] = []
    h_counter = 1

    for idx, row in enumerate(tracker_rows):
        r_num = idx + 2
        intern_str = str(row.get("INTERN NAME") or "")
        col_date_str = str(row.get("DATE OF COLLECTION") or "").strip()
        docket_str = str(row.get("DOCKET NUMBER") or "")
        case_url = str(row.get("_case_url") or "").strip()
        doc_type_str = str(row.get("DOCUMENT TYPE") or "")
        sub_type_str = str(row.get("SUB TYPE") or "")
        filed_date_str = str(row.get("FILED DATE") or "").strip()
        doc_name_str = str(row.get("Document Name") or "").strip()
        match_txt_str = str(row.get("Matching Document Text") or "").strip()
        doc_url = str(row.get("_doc_url") or "").strip()
        try:
            count_val = int(str(row.get("No. of collected documents") or "0").strip())
        except ValueError:
            count_val = 0

        # Column A: INTERN NAME
        c_a = f'<c r="A{r_num}" s="14" t="s"><v>{s_idx(intern_str)}</v></c>'

        # Column B: DATE OF COLLECTION (date serial on first row, blank bordered on rest)
        col_serial = date_to_excel_serial(col_date_str)
        if col_serial is not None:
            c_b = f'<c r="B{r_num}" s="15"><v>{col_serial}</v></c>'
        elif col_date_str:
            c_b = f'<c r="B{r_num}" s="14" t="s"><v>{s_idx(col_date_str)}</v></c>'
        else:
            c_b = f'<c r="B{r_num}" s="14"/>'

        # Column C: DOCKET NUMBER (clickable link to case URL if present)
        if case_url and docket_str:
            rel_id = f"rIdH{h_counter}"
            h_counter += 1
            hyperlinks_xml.append(
                f'<hyperlink xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" ref="C{r_num}" r:id="{rel_id}"/>'
            )
            rels_xml.append(
                f'<Relationship Id="{rel_id}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="{escape(case_url)}" TargetMode="External"/>'
            )
            c_c = f'<c r="C{r_num}" s="18" t="s"><v>{s_idx(docket_str)}</v></c>'
        else:
            c_c = f'<c r="C{r_num}" s="16" t="s"><v>{s_idx(docket_str)}</v></c>'

        # Column D: DOCUMENT TYPE
        c_d = f'<c r="D{r_num}" s="11" t="s"><v>{s_idx(doc_type_str)}</v></c>'

        # Column E: SUB TYPE
        c_e = f'<c r="E{r_num}" s="11" t="s"><v>{s_idx(sub_type_str)}</v></c>'

        # Column F: FILED DATE
        filed_serial = date_to_excel_serial(filed_date_str)
        if filed_serial is not None:
            c_f = f'<c r="F{r_num}" s="12"><v>{filed_serial}</v></c>'
        elif filed_date_str:
            c_f = f'<c r="F{r_num}" s="11" t="s"><v>{s_idx(filed_date_str)}</v></c>'
        else:
            c_f = f'<c r="F{r_num}" s="12"/>'

        # Column G: Document Name
        c_g = f'<c r="G{r_num}" s="11" t="s"><v>{s_idx(doc_name_str)}</v></c>' if doc_name_str else f'<c r="G{r_num}" s="11"/>'

        # Column H: Matching Document Text (clickable link to document download/view if present)
        if doc_url and match_txt_str:
            rel_id = f"rIdH{h_counter}"
            h_counter += 1
            hyperlinks_xml.append(
                f'<hyperlink xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" ref="H{r_num}" r:id="{rel_id}"/>'
            )
            rels_xml.append(
                f'<Relationship Id="{rel_id}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="{escape(doc_url)}" TargetMode="External"/>'
            )
            c_h = f'<c r="H{r_num}" s="18" t="s"><v>{s_idx(match_txt_str)}</v></c>'
        elif match_txt_str:
            c_h = f'<c r="H{r_num}" s="11" t="s"><v>{s_idx(match_txt_str)}</v></c>'
        else:
            c_h = f'<c r="H{r_num}" s="11"/>'

        # Column I: No. of collected documents
        c_i = f'<c r="I{r_num}" s="11"><v>{count_val}</v></c>'

        sheet_rows_xml.append(
            f'<row r="{r_num}" ht="18.6" customHeight="1" spans="1:9" x14ac:dyDescent="0.35">'
            f"{c_a}{c_b}{c_c}{c_d}{c_e}{c_f}{c_g}{c_h}{c_i}"
            f"</row>"
        )

    last_row = max(1, len(tracker_rows) + 1)
    hl_block = f'<hyperlinks>{"".join(hyperlinks_xml)}</hyperlinks>' if hyperlinks_xml else ""
    sheet1_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
        'mc:Ignorable="x14ac" '
        'xmlns:x14ac="http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac">'
        f'<dimension ref="A1:I{last_row}"/>'
        '<sheetViews><sheetView tabSelected="1" workbookViewId="0">'
        '<selection activeCell="A2" sqref="A2"/>'
        '</sheetView></sheetViews>'
        '<sheetFormatPr baseColWidth="8" defaultRowHeight="15"/>'
        '<cols>'
        '<col min="1" max="1" width="23" style="17" customWidth="1"/>'
        '<col min="2" max="2" width="19.140625" style="17" customWidth="1"/>'
        '<col min="3" max="4" width="23.140625" style="17" customWidth="1"/>'
        '<col min="5" max="5" width="34" style="17" customWidth="1"/>'
        '<col min="6" max="6" width="16" style="17" customWidth="1"/>'
        '<col min="7" max="7" width="28" style="17" customWidth="1"/>'
        '<col min="8" max="8" width="22" style="17" customWidth="1"/>'
        '<col min="9" max="9" width="23.140625" style="17" customWidth="1"/>'
        '</cols>'
        f'<sheetData>{"".join(sheet_rows_xml)}</sheetData>'
        '<conditionalFormatting sqref="C1:C1048576 D1:H1">'
        '<cfRule type="duplicateValues" dxfId="0" priority="1"/>'
        '</conditionalFormatting>'
        f'{hl_block}'
        '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>'
        '</worksheet>'
    )

    sst_items = []
    for s in strings:
        preserve = ' xml:space="preserve"' if (s.startswith(" ") or s.endswith(" ") or "\xa0" in s or "  " in s) else ""
        sst_items.append(f"<si><t{preserve}>{escape(s)}</t></si>")
    shared_strings_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'count="{len(strings)}" uniqueCount="{len(strings)}">'
        f'{"".join(sst_items)}</sst>'
    )

    now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    core_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<dc:creator>BPM Court Scanner</dc:creator>'
        '<cp:lastModifiedBy>BPM Court Scanner</cp:lastModifiedBy>'
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{now_iso}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now_iso}</dcterms:modified>'
        '</cp:coreProperties>'
    )

    safe_tab_name = escape(sheet_tab_name.strip()[:31] if sheet_tab_name else "KY - COA TRACKER")
    workbook_xml = _WORKBOOK_XML.replace('name="KY - COA TRACKER"', f'name="{safe_tab_name}"')
    app_xml = _APP_XML.replace('<vt:lpstr>KY - COA TRACKER</vt:lpstr>', f'<vt:lpstr>{safe_tab_name}</vt:lpstr>')

    xlsx_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(xlsx_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _ROOT_RELS_XML)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS_XML)
        zf.writestr("xl/theme/theme1.xml", _THEME_XML)
        zf.writestr("xl/styles.xml", _STYLES_XML)
        zf.writestr("xl/sharedStrings.xml", shared_strings_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet1_xml)
        if rels_xml:
            sheet1_rels_xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                + "".join(rels_xml)
                + "</Relationships>"
            )
            zf.writestr("xl/worksheets/_rels/sheet1.xml.rels", sheet1_rels_xml)
        zf.writestr("docProps/core.xml", core_xml)
        zf.writestr("docProps/app.xml", app_xml)

    return xlsx_path

