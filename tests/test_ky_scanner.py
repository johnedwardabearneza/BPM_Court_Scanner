import io
import json
import queue
import tempfile
import tkinter as tk
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from config import STOP_EVENT
import ky_scanner
from gui import App
from ky_scanner import (
    KyCaseRecord,
    KyCollectibleDoc,
    KySearchRow,
    assign_ky_save_as_names,
    build_ky_tracker_rows,
    is_ky_clerk_receipt_text,
    is_ky_collectible_doc,
    scan_ky_case,
)


class _DummyWorker:
    def __init__(self):
        self.commands = queue.Queue()


class TestKentuckyScanner(unittest.TestCase):
    def test_collectible_doc_filter_excludes_receipt_and_envelope(self):
        self.assertFalse(is_ky_collectible_doc("RECEIPT NOTICE", "CA - BRIEFING", "APPELLANT BRIEF FILED"))
        self.assertFalse(
            is_ky_collectible_doc("APPELLANT BRIEF FILED RAP 5(E) ENVELOPE", "CA - BRIEFING", "APPELLANT BRIEF FILED")
        )
        self.assertFalse(
            is_ky_collectible_doc("APPELLANT BRIEF FILED (DPA) NOTICE", "CA - BRIEFING", "APPELLANT BRIEF FILED")
        )
        self.assertFalse(is_ky_collectible_doc("APPELLANT BRIEF COVER PAGE", "CA - BRIEFING", "APPELLANT BRIEF FILED"))
        self.assertFalse(is_ky_collectible_doc("ORDER DENYING MOTION", "CA - ORDERS", "ORDER"))
        self.assertFalse(is_ky_collectible_doc("MOTION FOR ADDITIONAL TIME", "CA - MOTIONS", "MOTION"))

        self.assertTrue(is_ky_collectible_doc("APPELLANT BRIEF FILED", "CA - BRIEFING", "APPELLANT BRIEF FILED"))
        self.assertTrue(is_ky_collectible_doc("APPELLEE BRIEF FILED", "CA - BRIEFING", "APPELLEE BRIEF FILED"))
        self.assertTrue(
            is_ky_collectible_doc(
                "COMBINED APPELLEE/CROSS-APPELLANT BRIEF FILED",
                "CA - BRIEFING",
                "COMBINED APPELLEE/CROSS-APPELLANT BRIEF FILED",
            )
        )
        self.assertTrue(is_ky_collectible_doc("PETITION FOR WRIT OF MANDAMUS", "ORIGINAL ACTION", "PETITION"))
        self.assertTrue(is_ky_collectible_doc("MOTION FOR REHEARING", "CA - MOTIONS", "MOTION FOR REHEARING"))
        self.assertTrue(is_ky_collectible_doc("MOTION FOR RECONSIDERATION", "CA - MOTIONS", "MOTION"))

    def test_sop_save_as_naming_and_duplicate_numbering(self):
        docs_single = [
            KyCollectibleDoc("1", "APPELLEE BRIEF FILED", "10", "2026-10-13", "APPELLEE BRIEF FILED"),
        ]
        names = assign_ky_save_as_names("Kentucky Court of Appeals", "2024-CA-0123", docs_single)
        self.assertEqual(names, ["KYCOA_2024-CA-0123_AppelleeBrief"])

        docs_sup = [
            KyCollectibleDoc("2", "REPLY BRIEF FILED", "11", "2026-10-20", "REPLY BRIEF FILED"),
        ]
        names_sup = assign_ky_save_as_names("Kentucky Supreme Court", "2023-SC-0228", docs_sup)
        self.assertEqual(names_sup, ["KYSUP_2023-SC-0228_AppellantReplyBrief"])

        docs_dup = [
            KyCollectibleDoc("3", "APPELLEE BRIEF FILED", "12", "2026-06-22", "APPELLEE BRIEF FILED"),
            KyCollectibleDoc("4", "APPELLEE BRIEF FILED", "13", "2026-06-22", "APPELLEE BRIEF FILED"),
            KyCollectibleDoc(
                "5",
                "COMBINED APPELLEE/CROSS-APPELLANT BRIEF FILED",
                "14",
                "2026-04-30",
                "COMBINED APPELLEE/CROSS-APPELLANT BRIEF FILED",
            ),
        ]
        names_dup = assign_ky_save_as_names("Kentucky Court of Appeals", "2026-CA-0500", docs_dup)
        self.assertEqual(
            names_dup,
            [
                "KYCOA_2026-CA-0500_AppelleeBrief1",
                "KYCOA_2026-CA-0500_AppelleeBrief2",
                "KYCOA_2026-CA-0500_CombinedAppelleeCrossAppellantBrief",
            ],
        )

    def test_tracker_rows_deduplicate_same_case_count(self):
        search_rows = [
            KySearchRow("r1", "100", "2026-CA-0625", "Kentucky Court of Appeals",
                        "CA - BRIEFING", "APPELLANT BRIEF FILED", "2026-10-21", "10/21/2026", "APPELLANT BRIEF FILED"),
            KySearchRow("r2", "100", "2026-CA-0625", "Kentucky Court of Appeals",
                        "CA - BRIEFING", "APPELLEE BRIEF FILED", "2026-10-20", "10/20/2026", "APPELLEE BRIEF FILED"),
        ]
        doc = KyCollectibleDoc(
            "d1", "APPELLANT BRIEF FILED", "de1", "2026-10-21", "APPELLANT BRIEF FILED",
            save_as_name="KYCOA_2026-CA-0625_AppellantBrief",
        )
        records = {
            "2026-CA-0625": KyCaseRecord(
                case_number="2026-CA-0625",
                case_id="100",
                court="Kentucky Court of Appeals",
                title="TEST V. TEST",
                first_filed_date="10/21/2026",
                status="BRIEF_FOUND",
                collectible_docs=[doc],
            )
        }
        tracker = build_ky_tracker_rows(
            search_rows, records, intern_name="Bearneza, John Edward ", collection_date="2026-11-05"
        )
        self.assertEqual(len(tracker), 2)
        self.assertGreaterEqual(len(tracker[0]), 9)
        self.assertEqual(tracker[0]["INTERN NAME"], "Bearneza, John Edward ")
        self.assertEqual(tracker[0]["DATE OF COLLECTION"], "2026-11-05")
        self.assertEqual(tracker[0]["DOCKET NUMBER"], "Go to case2026-CA-0625")
        self.assertEqual(tracker[0]["FILED DATE"], "2026-10-21")
        self.assertEqual(tracker[0]["Document Name"], "APPELLANT BRIEF FILED")
        self.assertEqual(tracker[0]["Matching Document Text"], "Viewthe document")
        self.assertEqual(tracker[0]["No. of collected documents"], "1")
        self.assertEqual(tracker[0]["_case_url"], "https://appellatepublic.kycourts.net/case/100")
        self.assertEqual(tracker[0]["_doc_url"], "https://appellatepublic.kycourts.net/api/api/v1/publicaccessdocuments/r1/download")
        self.assertNotIn("Save-As Filenames", tracker[0])
        # Duplicate row for same case records 0 count and keeps collection date
        self.assertEqual(tracker[1]["DOCKET NUMBER"], "Go to case2026-CA-0625")
        self.assertEqual(tracker[1]["DATE OF COLLECTION"], "2026-11-05")
        self.assertEqual(tracker[1]["No. of collected documents"], "0")

        # Verify formatted .xlsx generation matches template structure (single sheet, no cover tab)
        from ky_xlsx import write_ky_tracker_xlsx
        import zipfile
        with tempfile.TemporaryDirectory() as tmp:
            xlsx_path = Path(tmp) / "test_tracker.xlsx"
            write_ky_tracker_xlsx(xlsx_path, tracker, sheet_tab_name="KY - COA TRACKER OCTOBER")
            self.assertTrue(xlsx_path.exists())
            with zipfile.ZipFile(xlsx_path, "r") as zf:
                self.assertIn("xl/worksheets/sheet1.xml", zf.namelist())
                self.assertNotIn("xl/worksheets/sheet2.xml", zf.namelist())
                sheet1 = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
                wb_xml = zf.read("xl/workbook.xml").decode("utf-8")
                app_xml = zf.read("docProps/app.xml").decode("utf-8")
                self.assertIn('name="KY - COA TRACKER OCTOBER"', wb_xml)
                self.assertNotIn("COURT ASSIGNMENT", wb_xml)
                self.assertIn('<vt:lpstr>KY - COA TRACKER OCTOBER</vt:lpstr>', app_xml)
                self.assertNotIn("COURT ASSIGNMENT", app_xml)
                self.assertIn('<row r="1"', sheet1)
                self.assertIn('<row r="2"', sheet1)
                self.assertIn('<row r="3"', sheet1)
                self.assertIn('<c r="B2" s="15"><v>46331</v></c>', sheet1)
                self.assertIn('<c r="B3" s="15"><v>46331</v></c>', sheet1)
                self.assertIn('<c r="F2" s="12"><v>46316</v></c>', sheet1)
                self.assertIn('<c r="C2" s="18"', sheet1)  # Hyperlinked style 18 on Docket Number
                self.assertIn('<c r="H2" s="18"', sheet1)  # Hyperlinked style 18 on Matching Document Text
                self.assertIn('<c r="I2" s="11"><v>1</v></c>', sheet1)
                self.assertIn('<c r="I3" s="11"><v>0</v></c>', sheet1)
                self.assertIn('<hyperlinks>', sheet1)
                self.assertIn('ref="C2"', sheet1)
                self.assertIn('ref="H2"', sheet1)
                self.assertIn("xl/worksheets/_rels/sheet1.xml.rels", zf.namelist())
                self.assertNotIn("xl/worksheets/_rels/sheet2.xml.rels", zf.namelist())
                sheet1_rels = zf.read("xl/worksheets/_rels/sheet1.xml.rels").decode("utf-8")
                self.assertIn("https://appellatepublic.kycourts.net/case/100", sheet1_rels)
                self.assertIn("https://appellatepublic.kycourts.net/api/api/v1/publicaccessdocuments/r1/download", sheet1_rels)
                self.assertIn('type="duplicateValues"', sheet1)

    def test_briefing_search_row_filter_excludes_orders_and_opinions(self):
        from ky_scanner import is_ky_briefing_search_row

        self.assertTrue(is_ky_briefing_search_row("CA - BRIEFING", "APPELLANT BRIEF FILED"))
        self.assertTrue(is_ky_briefing_search_row("CA - BRIEFING", "REPLY BRIEF FILED"))
        self.assertTrue(is_ky_briefing_search_row("BRIEFING", "APPELLANT(S) BRIEF FILED - APPEAL PERFECTED"))
        self.assertTrue(is_ky_briefing_search_row("ORIGINAL ACTION", "PETITION FOR WRIT OF MANDAMUS"))
        self.assertTrue(is_ky_briefing_search_row("CA - POST OPINION", "PETITION FOR REHEARING"))
        self.assertTrue(is_ky_briefing_search_row("CA - MOTIONS", "MOTION FOR RECONSIDERATION"))

        self.assertFalse(is_ky_briefing_search_row("CA - NON-DISPOSITIONAL ORDERS", "ORDER TO HOLD APPEAL IN ABEYANCE"))
        self.assertFalse(is_ky_briefing_search_row("CA - NON-DISPOSITIONAL ORDERS", "ORDER ON MOTION(S)"))
        self.assertFalse(is_ky_briefing_search_row("CA - START TIME ORDERS", "PREHEARING DENIED/START TIME ORDER"))
        self.assertFalse(is_ky_briefing_search_row("CA - OPINION", "NOT TO BE PUBLISHED - OPINION AFFIRMING"))
        self.assertFalse(is_ky_briefing_search_row("CA - POST OPINION", "PETITION FOR REHEARING DENIED"))

        mixed_rows = [
            KySearchRow("1", "c1", "2026-CA-0277", "Kentucky Court of Appeals",
                        "CA - NON-DISPOSITIONAL ORDERS", "ORDER TO HOLD APPEAL IN ABEYANCE",
                        "2026-10-30", "10/30/2026"),
            KySearchRow("2", "c2", "2026-CA-0406", "Kentucky Court of Appeals",
                        "CA - BRIEFING", "APPELLEE BRIEF FILED",
                        "2026-10-30", "10/30/2026"),
        ]
        tracker = build_ky_tracker_rows(mixed_rows, {})
        self.assertEqual(len(tracker), 1)
        self.assertEqual(tracker[0]["DOCKET NUMBER"], "Go to case2026-CA-0406")
        self.assertEqual(tracker[0]["DOCUMENT TYPE"], "CA - BRIEFING")
        self.assertEqual(tracker[0]["SUB TYPE"], "APPELLEE BRIEF FILED")

    def test_unified_gui_jurisdiction_switcher(self):
        root = tk.Tk()
        root.withdraw()
        try:
            worker = _DummyWorker()
            app = App(root, queue.Queue(), worker)
            self.assertIn(app.jurisdiction, ("KY", "NY"))
            app.switch_jurisdiction("KY")
            self.assertEqual(app.jurisdiction, "KY")
            self.assertEqual(app.ky_frame.active_court_code(), "KYCOA")
            self.assertTrue(worker.commands.empty())
            app.switch_jurisdiction("NY")
            self.assertEqual(app.jurisdiction, "NY")
            self.assertEqual(worker.commands.get_nowait(), ("init_ny",))
            app.switch_jurisdiction("KY")
            self.assertEqual(app.jurisdiction, "KY")
        finally:
            root.destroy()


class TestKentuckyScanLogic(unittest.TestCase):
    CASE_ID = "case-1"
    RECEIPT_TEXT = (
        "OFFICE OF CLERK OF COURT OF APPEALS 669 Chamberlin Ave., Ste. B Frankfort, Kentucky 40601-9229 "
        "Kate R. Morgan Clerk NO. 2025-CA-0766-MR RECEIPT NOTICE The document listed below has been filed"
    )

    @staticmethod
    def _entry(entry_id, sub, filed_utc):
        return {
            "docketEntryID": entry_id, "hasDocuments": True, "docketEntryType": "CA - BRIEFING",
            "docketEntrySubtype": sub, "filedDate": filed_utc, "submittedBy": [],
        }

    @staticmethod
    def _doc(doc_id, name, text=""):
        return {"documentID": doc_id, "documentName": name, "documentAvailable": True,
                "documentText": [text] if text else None}

    def _fake_api(self, entries, docs_by_entry):
        def fake(url, headers=None, timeout=25, retries=3):
            if url.endswith(f"/cases/{self.CASE_ID}"):
                return {"shortTitle": "A\nV.\nB", "court": "Kentucky Court of Appeals"}
            if "/docketentries" in url:
                return entries
            entry_id = url.split("parentID=")[1]
            return docs_by_entry[entry_id]
        return fake

    def test_scan_uses_real_ctrack_names_and_rejects_receipt_letters(self):
        # API order is newest-first, exactly like C-Track's Docket Entries table.
        entries = [
            self._entry("e4", "REPLY BRIEF FILED", "2026-03-18T15:00:00.000+0000"),
            self._entry("e3", "APPELLEE BRIEF FILED (COM)", "2026-03-03T15:00:00.000+0000"),
            self._entry("e2", "APPELLANT BRIEF FILED (DPA)", "2025-09-26T15:00:00.000+0000"),
            self._entry("e1", "APPELLANT BRIEF FILED", "2025-09-02T15:00:00.000+0000"),
        ]
        docs = {
            "e4": [self._doc("d4r", "RECEIPT NOTICE"), self._doc("d4", "REPLY BRIEF FILED", "R COMMONWEALTH")],
            "e3": [self._doc("d3", "APPELLEE BRIEF FILED (COM)", "A Commonwealth"), self._doc("d3r", "RECEIPT NOTICE")],
            # Clerk letter that carries the SAME name as the brief; only its text gives it away.
            "e2": [self._doc("d2n", "APPELLANT BRIEF FILED (DPA)", self.RECEIPT_TEXT),
                   self._doc("d2", "APPELLANT BRIEF FILED (DPA)", "A COMMONWEALTH OF KENTUCKY appellant brief")],
            "e1": [self._doc("d1", "APPELLANT BRIEF FILED", "A COMMONWEALTH"), self._doc("d1r", "RECEIPT NOTICE")],
        }
        with mock.patch.object(ky_scanner, "_api_get_json", self._fake_api(entries, docs)):
            rec = scan_ky_case(self.CASE_ID, "2025-CA-0766", "Kentucky Court of Appeals", "09/26/2025")
        self.assertEqual(rec.status, "BRIEF_FOUND")
        self.assertEqual(
            [d.document_id for d in rec.collectible_docs], ["d1", "d2", "d3", "d4"],
        )  # chronological, clerk letter d2n rejected
        self.assertEqual(
            [d.save_as_name for d in rec.collectible_docs],
            [
                "KYCOA_2025-CA-0766_AppellantBrief1",
                "KYCOA_2025-CA-0766_AppellantBrief2",
                "KYCOA_2025-CA-0766_AppelleeBrief",
                "KYCOA_2025-CA-0766_AppellantReplyBrief",
            ],
        )
        self.assertEqual(rec.review_note, "")

    def test_brief_entry_with_only_cover_page_is_flagged_for_manual_check(self):
        entries = [self._entry("e1", "APPELLANT BRIEF FILED", "2026-10-29T15:00:00.000+0000")]
        docs = {"e1": [self._doc("c1", "APPELLANT BRIEF COVER PAGE", "BRIEF FOR APPELLANT")]}
        with mock.patch.object(ky_scanner, "_api_get_json", self._fake_api(entries, docs)):
            rec = scan_ky_case(self.CASE_ID, "2026-CA-0928", "Kentucky Court of Appeals", "10/29/2026")
        self.assertEqual(rec.status, "NO_BRIEF")
        self.assertIn("APPELLANT BRIEF COVER PAGE", rec.review_note)
        self.assertIn("CHECK MANUALLY", rec.note)

    def test_orders_alone_do_not_raise_manual_check(self):
        entries = [self._entry("e1", "ORDER ON MOTION(S)", "2026-10-29T15:00:00.000+0000")]
        docs = {"e1": [self._doc("o1", "ORDER ON MOTION(S)", "ORDER")]}
        with mock.patch.object(ky_scanner, "_api_get_json", self._fake_api(entries, docs)):
            rec = scan_ky_case(self.CASE_ID, "2026-CA-0001", "Kentucky Court of Appeals", "10/29/2026")
        self.assertEqual(rec.status, "NO_BRIEF")
        self.assertEqual(rec.review_note, "")

    def test_naming_for_real_ctrack_reply_variants(self):
        court = "Kentucky Court of Appeals"
        mk = lambda name: [KyCollectibleDoc("x", name, "e", "2026-01-01", name)]  # noqa: E731
        self.assertEqual(assign_ky_save_as_names(court, "2026-CA-0001", mk("REPLY BRIEF FILED")),
                         ["KYCOA_2026-CA-0001_AppellantReplyBrief"])
        self.assertEqual(assign_ky_save_as_names(court, "2026-CA-0001", mk("REPLY BRIEF FILED (EXPEDITED)")),
                         ["KYCOA_2026-CA-0001_AppellantReplyBrief"])
        self.assertEqual(assign_ky_save_as_names(court, "2026-CA-0001", mk("CROSS-REPLY BRIEF FILED")),
                         ["KYCOA_2026-CA-0001_CrossAppellantReplyBrief"])
        self.assertEqual(assign_ky_save_as_names(court, "2026-CA-0001", mk("COMBINED REPLY/CROSS-APPELLEE BRIEF FILED")),
                         ["KYCOA_2026-CA-0001_CombinedReplyCrossAppelleeBrief"])
        self.assertEqual(assign_ky_save_as_names(court, "2026-CA-0001", mk("APPELLANT BRIEF FILED (PRO SE)")),
                         ["KYCOA_2026-CA-0001_AppellantBrief"])

    def test_clerk_receipt_text_detection(self):
        self.assertTrue(is_ky_clerk_receipt_text(self.RECEIPT_TEXT))
        self.assertFalse(is_ky_clerk_receipt_text(""))
        self.assertFalse(is_ky_clerk_receipt_text("A COMMONWEALTH OF KENTUCKY COURT OF APPEALS BRIEF FOR APPELLANT"))

    def test_checkpoint_from_older_logic_version_is_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ky_scanner, "OUTPUT_DIR", Path(tmp)):
            rec = KyCaseRecord("2026-CA-0001", "c", "Kentucky Court of Appeals", "T", "10/01/2026", status="NO_BRIEF")
            ky_scanner.save_ky_checkpoint("KYCOA", "2026-10", {"2026-CA-0001": rec})
            self.assertIn("2026-CA-0001", ky_scanner.load_ky_checkpoint("KYCOA", "2026-10"))
            cp = Path(tmp) / "KY_KYCOA_2026-10" / "checkpoint.json"
            payload = json.loads(cp.read_text(encoding="utf-8"))
            payload["logic_version"] = ky_scanner.KY_LOGIC_VERSION - 1
            cp.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(ky_scanner.load_ky_checkpoint("KYCOA", "2026-10"), {})

    def test_api_retries_transient_errors_but_not_client_errors(self):
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b'{"ok": true}'
        calls = [ConnectionResetError(10054, "reset"), urllib.error.HTTPError("u", 503, "x", {}, io.BytesIO()), ok]
        with mock.patch("urllib.request.urlopen", side_effect=calls), \
                mock.patch.object(ky_scanner.STOP_EVENT, "wait", return_value=False):
            self.assertEqual(ky_scanner._api_get_json("http://x"), {"ok": True})
        bad = urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO())
        with mock.patch("urllib.request.urlopen", side_effect=[bad]) as m:
            with self.assertRaises(urllib.error.HTTPError):
                ky_scanner._api_get_json("http://x")
            self.assertEqual(m.call_count, 1)

    def test_download_ky_pdf_saves_with_exact_sop_filename(self):
        import tempfile
        from pathlib import Path

        doc = KyCollectibleDoc(
            document_id="doc123",
            document_name="APPELLANT BRIEF FILED",
            docket_entry_id="de1",
            filed_date="2026-10-20",
            sub_type="APPELLANT BRIEF FILED",
            submitter="Sommer, Mark Francis",
            save_as_name="KYCOA_2026-CA-0236_AppellantBrief",
            download_url="https://example.test/doc123/download",
        )
        ok_resp = mock.MagicMock()
        ok_resp.__enter__.return_value.read.return_value = b"%PDF-1.7\nfake pdf content"
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            with mock.patch("urllib.request.urlopen", return_value=ok_resp) as m:
                out_path = ky_scanner.download_ky_pdf(doc, dest)
                self.assertEqual(out_path.name, "KYCOA_2026-CA-0236_AppellantBrief.pdf")
                self.assertTrue(out_path.exists())
                self.assertEqual(m.call_count, 1)
                # Second call without overwrite skips network download
                out_path2 = ky_scanner.download_ky_pdf(doc, dest, overwrite=False)
                self.assertEqual(out_path2, out_path)
                self.assertEqual(m.call_count, 1)

    def test_download_ky_pdf_retries_on_incompleteread(self):
        import http.client
        import tempfile
        from pathlib import Path

        doc = KyCollectibleDoc(
            document_id="doc123",
            document_name="APPELLANT BRIEF FILED",
            docket_entry_id="de1",
            filed_date="2026-10-20",
            sub_type="APPELLANT BRIEF FILED",
            submitter="Sommer, Mark Francis",
            save_as_name="KYCOA_2026-CA-0236_AppellantBrief",
            download_url="https://example.test/doc123/download",
        )
        fail_resp = mock.MagicMock()
        fail_resp.__enter__.return_value.read.side_effect = http.client.IncompleteRead(b"partial", 100)
        ok_resp = mock.MagicMock()
        ok_resp.__enter__.return_value.read.return_value = b"%PDF-1.7\nfull content"
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            with mock.patch("urllib.request.urlopen", side_effect=[fail_resp, ok_resp]) as m:
                out_path = ky_scanner.download_ky_pdf(doc, dest, retries=1)
                self.assertEqual(out_path.name, "KYCOA_2026-CA-0236_AppellantBrief.pdf")
                self.assertTrue(out_path.exists())
                self.assertEqual(m.call_count, 2)

    def test_find_ky_downloaded_pdf(self):
        import tempfile
        from pathlib import Path
        from gui_ky import KyScannerFrame
        from ky_scanner import KyCollectibleDoc

        doc = KyCollectibleDoc(
            document_id="doc1",
            document_name="APPELLANT BRIEF FILED",
            docket_entry_id="de1",
            filed_date="10/20/2026",
            sub_type="APPELLANT BRIEF FILED",
            submitter="Test Submitter",
            save_as_name="KYCOA_2026-CA-0001_AppellantBrief",
            download_url="https://example.test",
        )
        frame = mock.MagicMock(spec=KyScannerFrame)
        frame.active_court_code.return_value = "KYCOA"
        frame.active_year_month.return_value = "2026-10"

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("gui_ky.ky_downloads_dir", return_value=Path(tmp)):
                res = KyScannerFrame._find_ky_downloaded_pdf(frame, "2026-CA-0001", doc)
                self.assertIsNone(res)

                fake_pdf = Path(tmp) / "KYCOA_2026-CA-0001_AppellantBrief.pdf"
                fake_pdf.write_bytes(b"%PDF-1.4 sample content " * 10)
                res = KyScannerFrame._find_ky_downloaded_pdf(frame, "2026-CA-0001", doc)
                self.assertEqual(res, fake_pdf)

    def test_team_dashboard_state_and_done_sync(self):
        import dashboard_server

        state = dashboard_server.get_dashboard_state()
        self.assertIn("batches", state)
        dashboard_server.set_case_done("KY_KYCOA_2026-11", "2026-CA-0236", True, "Edward")
        state2 = dashboard_server.get_dashboard_state()
        b = next((x for x in state2["batches"] if x["scope"] == "KY_KYCOA_2026-11"), None)
        if b is not None:
            self.assertEqual(b["done"].get("2026-CA-0236", {}).get("by"), "Edward")
        dashboard_server.set_case_done("KY_KYCOA_2026-11", "2026-CA-0236", False, "Edward")

    def test_ky_button_processing_states_and_colors(self):
        import tkinter as tk
        from gui_ky import KyScannerFrame

        r = tk.Tk()
        try:
            app = type("App", (), {"root": r})()
            f = KyScannerFrame(r, app)
            # Idle defaults
            self.assertEqual(f.load_btn["text"], "Load")
            self.assertEqual(f.scan_btn["text"], "▶  Start Scan")
            self.assertEqual(f.scan_btn["bg"], "#4ade80")
            self.assertEqual(str(f.stop_btn["state"]), "disabled")

            # Loading active
            f.busy = True
            f.active_action = "load"
            f._set_buttons()
            self.assertIn("Loading", f.load_btn["text"])
            self.assertEqual(f.load_btn["bg"], "#f59e0b")
            self.assertEqual(str(f.scan_btn["state"]), "disabled")
            self.assertEqual(str(f.stop_btn["state"]), "normal")

            # Scanning active
            f.active_action = "scan"
            f._set_buttons()
            self.assertIn("Scanning", f.scan_btn["text"])
            self.assertEqual(f.scan_btn["bg"], "#f59e0b")
            self.assertEqual(str(f.load_btn["state"]), "disabled")
            self.assertEqual(f.stop_btn["bg"], "#ef4444")

            # Stop triggered
            f.stop_scan()
            self.assertIn("Stopping", f.scan_btn["text"])

            # Back to idle
            f.busy = False
            f._set_buttons()
            self.assertEqual(f.scan_btn["text"], "▶  Start Scan")
            self.assertEqual(f.scan_btn["bg"], "#4ade80")
            self.assertEqual(f.load_btn["text"], "Load")
            self.assertEqual(str(f.stop_btn["state"]), "disabled")
        finally:
            STOP_EVENT.clear()
            r.destroy()


    def test_multi_volume_detection_and_exclusion(self):
        from ky_scanner import has_ky_multi_volume, ky_doc_volume_number

        self.assertEqual(ky_doc_volume_number("APPELLANT BRIEF FILED (VOL. 1)"), "1")
        self.assertEqual(ky_doc_volume_number("APPELLANT BRIEF FILED (VOL. 7)"), "7")
        self.assertEqual(ky_doc_volume_number("BRIEF (VOLUME 2)"), "2")
        self.assertEqual(ky_doc_volume_number("BRIEF VOL 3"), "3")
        self.assertEqual(ky_doc_volume_number("BRIEF VOL. II"), "II")
        self.assertIsNone(ky_doc_volume_number("APPELLANT BRIEF FILED"))

        doc1 = KyCollectibleDoc("1", "APPELLANT BRIEF FILED (VOL. 1)", "e1", "2026-10-10", "APPELLANT BRIEF")
        doc2 = KyCollectibleDoc("2", "APPELLANT BRIEF FILED (VOL. 2)", "e1", "2026-10-10", "APPELLANT BRIEF")
        doc7 = KyCollectibleDoc("7", "APPELLANT BRIEF FILED (VOL. 7)", "e1", "2026-10-10", "APPELLANT BRIEF")

        # Multi-volume case with 7 volumes
        docs_7 = [doc1, doc2, doc7]
        self.assertTrue(has_ky_multi_volume(docs_7))

        # 2 volumes
        self.assertTrue(has_ky_multi_volume([doc1, doc2]))

        # After excluding other volumes, only Vol 1 remains -> Warning clears
        self.assertFalse(has_ky_multi_volume([doc1]))

        # Normal single brief
        normal_doc = KyCollectibleDoc("1", "APPELLANT BRIEF FILED", "e1", "2026-10-10", "APPELLANT BRIEF")
        self.assertFalse(has_ky_multi_volume([normal_doc]))

    def test_gui_ky_multi_volume_formatting_and_exclusion(self):
        import tkinter as tk
        from gui_ky import KyScannerFrame

        r = tk.Tk()
        try:
            app = type("App", (), {"root": r})()
            f = KyScannerFrame(r, app)

            doc1 = KyCollectibleDoc("d1", "APPELLANT BRIEF FILED (VOL. 1)", "e1", "2026-10-10", "APPELLANT BRIEF", save_as_name="KYCOA_2024-CA-0001_AppellantBrief1")
            doc2 = KyCollectibleDoc("d2", "APPELLANT BRIEF FILED (VOL. 2)", "e1", "2026-10-10", "APPELLANT BRIEF", save_as_name="KYCOA_2024-CA-0001_AppellantBrief2")

            rec = KyCaseRecord(
                case_number="2024-CA-0001",
                case_id="cid-1",
                court="Kentucky Court of Appeals",
                title="Jennings v. Commonwealth",
                first_filed_date="2026-10-10",
                status="BRIEF_FOUND",
                collectible_docs=[doc1, doc2],
                note="2 collectible PDF(s)",
            )
            f.records["2024-CA-0001"] = rec

            status_text, _names, tag = f._format_case_row(rec)
            self.assertEqual(status_text, "HIT (2 PDFs) ⚠ Multi-Vol")
            self.assertEqual(tag, "review")

            # Exclude Vol 2 (deposition attachment)
            with mock.patch("gui_ky.save_ky_checkpoint"):
                f._exclude_ky_doc("2024-CA-0001", doc2)

            self.assertEqual(len(rec.collectible_docs), 1)
            self.assertEqual(rec.collectible_docs[0].document_name, "APPELLANT BRIEF FILED (VOL. 1)")
            self.assertEqual(rec.collectible_docs[0].save_as_name, "KYCOA_2024-CA-0001_AppellantBrief")

            # Status transitions back to clean HIT (1 PDF)
            new_status, _names, new_tag = f._format_case_row(rec)
            self.assertEqual(new_status, "HIT (1 PDF)")
            self.assertEqual(new_tag, "hit")
        finally:
            STOP_EVENT.clear()
            r.destroy()

    def test_manual_check_detection_and_pinned_errors(self):
        import tkinter as tk
        from gui_ky import KyScannerFrame
        from ky_scanner import KyCaseRecord, KyCollectibleDoc

        r = tk.Tk()
        try:
            app = type("App", (), {"root": r})()
            f = KyScannerFrame(r, app)
            f.records.clear()
            f.pinned_errors.clear()

            # Initially 0 errors and 0 checks
            self.assertEqual(len(f.pinned_errors), 0)
            self.assertEqual(len(f.get_manual_check_cases()), 0)

            # 1. Multi-volume case
            doc1 = KyCollectibleDoc("d1", "APPELLANT BRIEF VOL 1", "e1", "2026-10-10", "APPELLANT BRIEF", save_as_name="KYCOA_2024-CA-0010_AppellantBrief1")
            doc2 = KyCollectibleDoc("d2", "APPELLANT BRIEF VOL 2", "e1", "2026-10-10", "APPELLANT BRIEF", save_as_name="KYCOA_2024-CA-0010_AppellantBrief2")
            rec_multi = KyCaseRecord(
                case_number="2024-CA-0010", case_id="c10", court="Kentucky Court of Appeals",
                title="Alpha v. Beta", first_filed_date="2026-10-10", status="BRIEF_FOUND",
                collectible_docs=[doc1, doc2],
            )

            # 2. Review note case
            rec_review = KyCaseRecord(
                case_number="2024-CA-0020", case_id="c20", court="Kentucky Court of Appeals",
                title="Gamma v. Delta", first_filed_date="2026-10-10", status="NO_BRIEF",
                review_note="Brief entry found on docket, but no collectible PDF",
                search_sub_types=["APPELLANT BRIEF FILED"],
            )

            # 3. Pro Se case
            rec_prose = KyCaseRecord(
                case_number="2024-CA-0030", case_id="c30", court="Kentucky Court of Appeals",
                title="Epsilon v. Zeta", first_filed_date="2026-10-10", status="BRIEF_FOUND",
                search_sub_types=["APPELLANT BRIEF FILED (PRO SE)"],
            )

            f.records["2024-CA-0010"] = rec_multi
            f.records["2024-CA-0020"] = rec_review
            f.records["2024-CA-0030"] = rec_prose

            manual_cases = f.get_manual_check_cases()
            self.assertEqual(len(manual_cases), 3)
            case_nums = [m["case_number"] for m in manual_cases]
            self.assertIn("2024-CA-0010", case_nums)
            self.assertIn("2024-CA-0020", case_nums)
            self.assertIn("2024-CA-0030", case_nums)

            # Error recording and pinned bar
            f._record_error("Download Error", "2024-CA-0010", "Connection timed out after 60s")
            self.assertEqual(len(f.pinned_errors), 1)
            self.assertIn("1 Error(s) Logged", f.pinned_alert_lbl["text"])

            # Log with [ERROR] should auto-tag and pin
            f._append_log("[ERROR] 2024-CA-0099: Failed to stream socket (IncompleteRead)", "head")
            self.assertEqual(len(f.pinned_errors), 2)
            self.assertIn("2 Error(s) Logged", f.pinned_alert_lbl["text"])

            # Test modal generation
            f.show_manual_check_modal({
                "total": 3, "saved_cnt": 2, "existing_cnt": 1, "newly_saved_cnt": 1,
                "failed_cases": ["2024-CA-0010"],
                "failed_details": [{"case": "2024-CA-0010", "doc_name": "TestDoc", "error": "Timeout"}],
                "default_dest": "C:/fake/path",
            })

            # Clear errors
            f.clear_pinned_errors()
            self.assertEqual(len(f.pinned_errors), 0)

            # Test audit downloads folder with missing, duplicate, and corrupted files
            import tempfile
            with tempfile.TemporaryDirectory() as tmp_dl:
                tmp_dir = Path(tmp_dl)
                (tmp_dir / "doc1.pdf").write_bytes(b"%PDF-1.4 sample content " * 10)
                sub = tmp_dir / "date_subfolder"
                sub.mkdir()
                (sub / "doc1.pdf").write_bytes(b"%PDF-1.4 sample content " * 10)
                (tmp_dir / "broken.part").write_bytes(b"broken")

                with mock.patch("gui_ky.ky_downloads_dir", return_value=tmp_dir):
                    f.audit_downloads_folder()
        finally:
            STOP_EVENT.clear()
            r.destroy()

    def test_strict_month_isolation_and_intern_name(self):
        """Verify strict month filtering excludes out-of-month briefs and respects custom intern names."""
        with mock.patch("ky_scanner._api_get_json") as m_api:
            # Case info
            m_api.side_effect = [
                {"shortTitle": "Test v Test", "court": "Kentucky Court of Appeals"},
                # 3 docket entries: April 2024, August 2024, January 2025
                [
                    {
                        "docketEntryID": 1,
                        "docketEntryType": "BRIEF",
                        "docketEntrySubtype": "APPELLANT BRIEF FILED",
                        "filedDate": "2024-04-02T12:00:00Z",
                        "hasDocuments": True,
                    },
                    {
                        "docketEntryID": 2,
                        "docketEntryType": "BRIEF",
                        "docketEntrySubtype": "REPLY BRIEF FILED",
                        "filedDate": "2024-08-01T14:00:00Z",
                        "hasDocuments": True,
                    },
                    {
                        "docketEntryID": 3,
                        "docketEntryType": "BRIEF",
                        "docketEntrySubtype": "APPELLEE BRIEF FILED",
                        "filedDate": "2025-01-27T09:00:00Z",
                        "hasDocuments": True,
                    },
                ],
                # Documents for Entry 1 (April 2024)
                [{"documentID": "doc_apr", "documentName": "APPELLANT BRIEF FILED", "documentAvailable": True}],
                # Documents for Entry 2 (August 2024)
                [{"documentID": "doc_aug", "documentName": "REPLY BRIEF FILED", "documentAvailable": True}],
                # Documents for Entry 3 (January 2025)
                [{"documentID": "doc_jan", "documentName": "APPELLEE BRIEF FILED", "documentAvailable": True}],
            ]

            rec = ky_scanner.scan_ky_case(
                "case123", "2024-CA-1113", year_month="2024-08"
            )
            # All collectible briefs inside the matched case are collected (Appellant, Reply, Appellee)
            self.assertEqual(len(rec.collectible_docs), 3)
            self.assertEqual(rec.collectible_docs[0].filed_date, "2024-04-02")
            self.assertEqual(rec.collectible_docs[0].sub_type, "APPELLANT BRIEF FILED")
            self.assertEqual(rec.collectible_docs[1].filed_date, "2024-08-01")
            self.assertEqual(rec.collectible_docs[1].sub_type, "REPLY BRIEF FILED")
            self.assertEqual(rec.collectible_docs[2].filed_date, "2025-01-27")
            self.assertEqual(rec.collectible_docs[2].sub_type, "APPELLEE BRIEF FILED")

            # Check tracker rows with custom intern name and descending date sorting (Day 30/31 down to 1)
            sr1 = ky_scanner.KySearchRow(
                row_id="doc_aug1",
                case_id="case1",
                case_number="2024-CA-0001",
                court="Kentucky Court of Appeals",
                doc_type="CA - BRIEFING",
                sub_type="APPELLANT BRIEF FILED",
                document_name="APPELLANT BRIEF FILED",
                filed_date_iso="2024-08-05",
                filed_date_display="08/05/2024",
            )
            sr2 = ky_scanner.KySearchRow(
                row_id="doc_aug2",
                case_id="case2",
                case_number="2024-CA-0002",
                court="Kentucky Court of Appeals",
                doc_type="CA - BRIEFING",
                sub_type="REPLY BRIEF FILED",
                document_name="REPLY BRIEF FILED",
                filed_date_iso="2024-08-28",
                filed_date_display="08/28/2024",
            )
            rows = ky_scanner.build_ky_tracker_rows(
                [sr1, sr2], {}, intern_name="Oñang, Carl Nathaniel", year_month="2024-08"
            )
            self.assertEqual(len(rows), 2)
            # Descending order: August 28 must come before August 5
            self.assertEqual(rows[0]["FILED DATE"], "2024-08-28")
            self.assertEqual(rows[0]["INTERN NAME"], "Oñang, Carl Nathaniel")
            self.assertEqual(rows[1]["FILED DATE"], "2024-08-05")

    def test_load_ky_download_tracker_rows_grouping_and_month_isolation(self):
        with tempfile.TemporaryDirectory() as td:
            temp_out = Path(td)
            with mock.patch("ky_scanner.OUTPUT_DIR", temp_out):
                # Simulate download history with events from multiple months and cases
                doc_aug28 = ky_scanner.KyCollectibleDoc(
                    document_id="d1", document_name="REPLY BRIEF", docket_entry_id="de1",
                    filed_date="2024-08-28", sub_type="REPLY BRIEF FILED",
                )
                doc_aug15_a = ky_scanner.KyCollectibleDoc(
                    document_id="d2", document_name="APPELLANT BRIEF", docket_entry_id="de2",
                    filed_date="2024-08-15", sub_type="APPELLANT BRIEF FILED",
                )
                doc_aug15_b = ky_scanner.KyCollectibleDoc(
                    document_id="d3", document_name="APPELLEE BRIEF", docket_entry_id="de3",
                    filed_date="2024-08-15", sub_type="APPELLEE BRIEF FILED",
                )
                doc_july = ky_scanner.KyCollectibleDoc(
                    document_id="d4", document_name="OLD BRIEF", docket_entry_id="de4",
                    filed_date="2024-07-20", sub_type="APPELLANT BRIEF FILED",
                )

                ky_scanner.record_ky_download_history("KYCOA", "2024-08", doc_aug15_a, Path("p1.pdf"), "2024-CA-0010")
                ky_scanner.record_ky_download_history("KYCOA", "2024-08", doc_aug15_b, Path("p2.pdf"), "2024-CA-0010")
                ky_scanner.record_ky_download_history("KYCOA", "2024-08", doc_aug28, Path("p3.pdf"), "2024-CA-0020")
                ky_scanner.record_ky_download_history("KYCOA", "2024-08", doc_july, Path("p4.pdf"), "2024-CA-0030")

                rows = ky_scanner.load_ky_download_tracker_rows("KYCOA", "2024-08", intern_name="Oñang, Carl Nathaniel")

                # Exactly 1 clean primary row per downloaded case (no duplicates, no pink highlights)
                self.assertEqual(len(rows), 3)
                # All rows must carry Carl's name
                for r in rows:
                    self.assertEqual(r["INTERN NAME"], "Oñang, Carl Nathaniel")

                # Top case must be 2024-CA-0020 with August 28 (Day 28 before Day 15)
                self.assertEqual(rows[0]["DOCKET NUMBER"], "Go to case2024-CA-0020")
                self.assertEqual(rows[0]["FILED DATE"], "2024-08-28")
                self.assertEqual(rows[0]["No. of collected documents"], "1")

                # Next case is 2024-CA-0010 with 2 collected documents credited on its single row
                self.assertEqual(rows[1]["DOCKET NUMBER"], "Go to case2024-CA-0010")
                self.assertEqual(rows[1]["No. of collected documents"], "2")

                # Third case is 2024-CA-0030
                self.assertEqual(rows[2]["DOCKET NUMBER"], "Go to case2024-CA-0030")
                self.assertEqual(rows[2]["No. of collected documents"], "1")

    def test_scan_ky_case_raises_interrupted_error_on_stop(self):
        ky_scanner.STOP_EVENT.set()
        try:
            with self.assertRaises(InterruptedError):
                ky_scanner.scan_ky_case("case123", "2024-CA-0089", year_month="2024-07")
        finally:
            ky_scanner.STOP_EVENT.clear()

        # Also verify interruption during entry processing raises InterruptedError and does NOT return NO_BRIEF
        with mock.patch("ky_scanner._api_get_json") as mock_api:
            mock_api.side_effect = [
                {"shortTitle": "Test Case", "court": "Kentucky Court of Appeals"},  # case info
                [{"docketEntryID": "de1", "hasDocuments": True, "filedDate": "2024-07-10T10:00:00Z", "docketEntrySubtype": "BRIEF"}],  # entries
            ]
            # Set stop event after entries are loaded
            def trigger_stop(*args, **kwargs):
                ky_scanner.STOP_EVENT.set()
                return []
            
            ky_scanner.STOP_EVENT.clear()
            # When entries loop runs with STOP_EVENT set:
            with mock.patch.object(ky_scanner.STOP_EVENT, "is_set", side_effect=[False, False, True]):
                with self.assertRaises(InterruptedError):
                    ky_scanner.scan_ky_case("case123", "2024-CA-0089", year_month="2024-07")


if __name__ == "__main__":
    unittest.main()


