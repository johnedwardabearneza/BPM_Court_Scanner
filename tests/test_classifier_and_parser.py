from pathlib import Path
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from classifier import (
    STATUS_EMPTY_NO_CASE,
    STATUS_HIT_VALID,
    STATUS_NEEDS_MANUAL_CHECK,
    STATUS_NO_BRIEF_FOUND,
    STATUS_SEALED_OR_UNCLICKABLE,
    classify_docket,
)
from main import group_options_by_month
from monthly_report import (
    inspect_option_progress,
    parse_selection_indices,
    write_monthly_reports,
)
from nyscef_browser import MotionPdfOption, parse_handdown_event_items
from pdf_parser import extract_pdf_case_titles, normalize_case_number, parse_motion_pdf_file
from report_builder import (
    CaseScanRecord,
    load_checkpoint,
    save_checkpoint,
    write_all_reports,
)
from ui_theme import format_date_words, format_month_words, parse_row_meta, short_case_type


class TestPdfParser(unittest.TestCase):
    def test_normalize_strips_plus_suffix(self):
        self.assertEqual(normalize_case_number("2024-10878+1"), "2024-10878")
        self.assertEqual(normalize_case_number(" 2025-05679 "), "2025-05679")

    def test_extract_pdf_case_titles_handles_own_line_and_inline(self):
        sample = (
            "Title\nCase Number\nDocument\n"
            "Peo v Antonucci, Ashley\n2026-06036\nM315124.pdf\n"
            "Mtr of McNair v Logan\n2026-02939 +1\nM315115.pdf\n"
            "Mtr of A. (Anonymous), Maryam; Admin for Children\ufffds Services 2026-10848\nM317561.pdf\n"
        )
        titles = extract_pdf_case_titles(sample)
        self.assertEqual(titles["2026-06036"], "Peo v Antonucci, Ashley")
        self.assertEqual(titles["2026-02939"], "Mtr of McNair v Logan")
        self.assertEqual(
            titles["2026-10848"],
            "Mtr of A. (Anonymous), Maryam; Admin for Children's Services",
        )

    def test_parses_real_downloaded_motion_list_if_present(self):
        sample_pdf = Path("test_data") / "motion_list.pdf"
        if not sample_pdf.exists():
            self.skipTest("Optional test motion_list.pdf not present.")
        parsed = parse_motion_pdf_file(sample_pdf)
        self.assertEqual(parsed.page_count, 2)
        self.assertGreaterEqual(len(parsed.cases), 50)
        self.assertTrue(all("+" not in c for c in parsed.cases))


class TestClassifier(unittest.TestCase):
    def test_ignores_confirmation_notice_and_captures_real_brief_only(self):
        rows = [
            {
                "text": "4 APPELLANT'S BRIEF *Corrected* Appellant's Brief with Affirmation of Service WALLSHEIN, CHARLES Processed Confirmation Notice",
                "links": [
                    {
                        "text": "APPELLANT'S BRIEF",
                        "href": "https://iapps.courts.state.ny.us/nyscef/ViewDocument?docIndex=CxwjNC0Eqd123",
                        "onclick": "",
                    },
                    {
                        "text": "Confirmation Notice",
                        "href": "https://iapps.courts.state.ny.us/nyscef/ConfirmationNotice?docIndex=CxwjNC0Eqd123",
                        "onclick": "",
                    },
                ],
            }
        ]
        status, briefs, _ = classify_docket(rows, case_type="Appellate Division - 2nd Dept | Civil Action - General")
        self.assertEqual(status, STATUS_HIT_VALID)
        self.assertEqual(len(briefs), 1)
        self.assertEqual(briefs[0].title, "APPELLANT'S BRIEF")
        self.assertIn("ViewDocument", briefs[0].url)

    def test_collectible_pleadings_and_motions_captured_while_generic_motion_ignored(self):
        rows = [
            {
                "text": "2 PETITION FOR WRIT OF HABEAS CORPUS Processed Confirmation Notice",
                "links": [
                    {
                        "text": "PETITION FOR WRIT OF HABEAS CORPUS",
                        "href": "https://iapps.courts.state.ny.us/nyscef/ViewDocument?docIndex=Hab123",
                        "onclick": "",
                    }
                ],
            },
            {
                "text": "5 MOTION FOR REHEARING Processed Confirmation Notice",
                "links": [
                    {
                        "text": "MOTION FOR REHEARING",
                        "href": "https://iapps.courts.state.ny.us/nyscef/ViewDocument?docIndex=Reh456",
                        "onclick": "",
                    }
                ],
            },
            {
                "text": "6 ORDER ON MOTION FOR REHEARING Processed",
                "links": [
                    {
                        "text": "ORDER ON MOTION FOR REHEARING",
                        "href": "https://iapps.courts.state.ny.us/nyscef/ViewDocument?docIndex=Ord789",
                        "onclick": "",
                    }
                ],
            },
        ]
        status, briefs, _ = classify_docket(rows)
        self.assertEqual(status, STATUS_HIT_VALID)
        self.assertEqual([b.title for b in briefs], ["PETITION FOR WRIT OF HABEAS CORPUS", "MOTION FOR REHEARING"])

    def test_sealed_matrimonial_plain_text_brief_returns_unclickable(self):
        rows = [
            {
                "text": "13 APPELLANT'S BRIEF SANDERS, PETER S. Filed: 02/11/2026",
                "links": [],
            }
        ]
        status, briefs, note = classify_docket(rows, case_type="Appellate Division - 2nd Dept | Civil Action - Matrimonial")
        self.assertEqual(status, STATUS_SEALED_OR_UNCLICKABLE)
        self.assertEqual(briefs, [])
        self.assertIn("Matrimonial", note)

    def test_order_mentioning_brief_in_description_does_not_false_positive(self):
        rows = [
            {
                "text": "5 DECISION & ORDER ON MOTION Directing respondent to serve brief by May 1",
                "links": [
                    {
                        "text": "DECISION & ORDER ON MOTION",
                        "href": "https://iapps.courts.state.ny.us/nyscef/ViewDocument?docIndex=Order999",
                        "onclick": "",
                    }
                ],
            }
        ]
        status, briefs, _ = classify_docket(rows, case_type="Civil Action - General")
        # Row contained BRIEF in text, but link was clickable ORDER (not a brief) -> NO_BRIEF_FOUND
        self.assertEqual(status, STATUS_NO_BRIEF_FOUND)
        self.assertEqual(briefs, [])

    def test_empty_brief_rows_returns_no_brief_found(self):
        status, briefs, _ = classify_docket([], case_type="Civil Action - General")
        self.assertEqual(status, STATUS_NO_BRIEF_FOUND)
        self.assertEqual(briefs, [])


class TestReportsAndCheckpoint(unittest.TestCase):
    def test_checkpoint_roundtrip_and_reports_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run_1"
            meta = {"source_name": "11-06-2026_MOTION_LIST.pdf", "total_unique_cases": 3}
            _, briefs, note = classify_docket([
                {
                    "text": "4 APPELLANT'S BRIEF WALLSHEIN",
                    "links": [
                        {
                            "text": "APPELLANT'S BRIEF",
                            "href": "https://iapps.courts.state.ny.us/nyscef/ViewDocument?docIndex=ABC",
                            "onclick": "",
                        }
                    ],
                }
            ])
            records = {
                "2025-05679": CaseScanRecord(
                    case_number="2025-05679",
                    status=STATUS_HIT_VALID,
                    caption='BANK OF NY "TEST" v. UDDIN',
                    case_type="Civil Action - General",
                    briefs=briefs,
                    note=note,
                ),
                "2025-00300": CaseScanRecord(case_number="2025-00300", status=STATUS_SEALED_OR_UNCLICKABLE),
                "2017-06910": CaseScanRecord(case_number="2017-06910", status=STATUS_EMPTY_NO_CASE),
            }
            save_checkpoint(run_dir, meta, records)
            reloaded = load_checkpoint(run_dir)
            self.assertEqual(set(reloaded.keys()), {"2025-05679", "2025-00300", "2017-06910"})
            self.assertEqual(len(reloaded["2025-05679"].briefs), 1)

            paths = write_all_reports(run_dir, meta, ["2025-05679", "2025-00300", "2017-06910"], reloaded)
            self.assertTrue(paths["html"].exists())
            self.assertTrue(paths["txt"].exists())
            self.assertTrue(paths["csv"].exists())
            html_content = paths["html"].read_text(encoding="utf-8")
            self.assertIn("2025-05679", html_content)
            self.assertIn("APPELLANT&#x27;S BRIEF", html_content)
            self.assertIn("BANK OF NY &quot;TEST&quot; v. UDDIN", html_content)

    def test_group_options_by_month_preserves_order_and_handles_legacy_names(self):
        opts = [
            MotionPdfOption("11-06-2026 (11-06-2026_MOTION_LIST.pdf)", "https://ex/1.pdf", "11-06-2026_MOTION_LIST.pdf", "2026-11", "2026-11-06"),
            MotionPdfOption("10-30-2026 (10-30-2026_MOTION_LIST.pdf)", "https://ex/2.pdf", "10-30-2026_MOTION_LIST.pdf", "2026-10", "2026-10-30"),
            MotionPdfOption("01-03-2017 (01-03-2017 MOTION LIST.pdf)", "https://ex/3.pdf", "01-03-2017 MOTION LIST.pdf", "2017-01", "2017-01-03"),
        ]
        grouped = group_options_by_month(opts)
        self.assertEqual(list(grouped.keys()), ["2026-11", "2026-10", "2017-01"])
        self.assertEqual(len(grouped["2026-10"]), 1)

    def test_parse_handdown_event_items_prefers_calendar_date_over_filename_typo(self):
        raw_items = [
            {
                "text": "Motions",
                "href": "https://www.nycourts.gov/courts/ad2/Handdowns/2026/07-July/06-07-2026_MOTION_LIST.pdf",
                "startIso": "2026-07-01",
            },
            {
                "text": "Motions",
                "href": "https://www.nycourts.gov/courts/ad2/Handdowns/2024/12-December/12-30-2024.pdf",
                "startIso": "2024-12-30",
            },
            {
                "text": "Motions",
                "href": "https://www.nycourts.gov/courts/ad2/Handdowns/2024/01-January/01-24-2024%20MOTON%20LIST.pdf",
                "startIso": "2024-01-24",
            },
        ]
        parsed = parse_handdown_event_items(raw_items)
        self.assertEqual(len(parsed), 3)
        # Clerk typo filename '06-07-2026_MOTION_LIST.pdf' placed on 2026-07-01 MUST stay in 2026-07
        self.assertEqual(parsed[0].year_month, "2026-07")
        self.assertEqual(parsed[0].date_iso, "2026-07-01")
        self.assertIn("07-01-2026 (06-07-2026_MOTION_LIST.pdf)", parsed[0].label)
        self.assertEqual(parsed[1].year_month, "2024-12")
        self.assertEqual(parsed[2].filename, "01-24-2024 MOTON LIST.pdf")

    def test_parse_selection_indices_supports_single_ranges_and_lists(self):
        self.assertEqual(parse_selection_indices("3", 14), [2])
        self.assertEqual(parse_selection_indices("1-4", 14), [0, 1, 2, 3])
        self.assertEqual(parse_selection_indices("1..3, 6, 2", 14), [0, 1, 2, 5])
        with self.assertRaises(ValueError):
            parse_selection_indices("15", 14)
        with self.assertRaises(ValueError):
            parse_selection_indices("abc", 14)

    def test_monthly_reports_and_progress_badges(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            opt1 = MotionPdfOption("07-31-2026 (07-31-2026_MOTION_LIST.pdf)", "https://ex/1.pdf", "07-31-2026_MOTION_LIST.pdf", "2026-07", "2026-07-31")
            opt2 = MotionPdfOption("07-30-2026 (07-30-2026_MOTION_LIST.pdf)", "https://ex/2.pdf", "07-30-2026_MOTION_LIST.pdf", "2026-07", "2026-07-30")
            opt3 = MotionPdfOption("07-29-2026 (07-29-2026_MOTION_LIST.pdf)", "https://ex/3.pdf", "07-29-2026_MOTION_LIST.pdf", "2026-07", "2026-07-29")

            resolver = lambda o: root / o.filename.replace(".pdf", "")
            _, briefs, note = classify_docket([
                {
                    "text": "4 APPELLANT'S BRIEF",
                    "links": [{"text": "APPELLANT'S BRIEF", "href": "https://iapps.courts.state.ny.us/nyscef/ViewDocument?docIndex=X1"}],
                }
            ])

            # Date 1 completed with shared case 2025-05679
            save_checkpoint(
                resolver(opt1),
                {"source_name": opt1.filename, "total_unique_cases": 1},
                {"2025-05679": CaseScanRecord("2025-05679", STATUS_HIT_VALID, "CAP A", "General", briefs=briefs, note=note)},
            )
            # Date 2 partial (1 of 3 checked) also containing 2025-05679
            save_checkpoint(
                resolver(opt2),
                {"source_name": opt2.filename, "total_unique_cases": 3},
                {"2025-05679": CaseScanRecord("2025-05679", STATUS_HIT_VALID, "CAP A", "General", briefs=briefs, note=note)},
            )

            p1 = inspect_option_progress(resolver(opt1))
            p2 = inspect_option_progress(resolver(opt2))
            p3 = inspect_option_progress(resolver(opt3))
            self.assertTrue(p1.is_complete)
            self.assertIn("DONE 1/1 | 1 HITS", p1.badge)
            self.assertFalse(p2.is_complete)
            self.assertIn("PARTIAL 1/3 | 1 HITS", p2.badge)
            self.assertEqual(p3.badge, "[PENDING]")

            out = write_monthly_reports(root, "2026-07", [opt1, opt2, opt3], resolver)
            html_txt = out["html"].read_text(encoding="utf-8")
            self.assertIn("NY Briefs Monthly Master — JULY 2026 (2026-07)", html_txt)
            self.assertIn("Also on JULY 30, 2026", html_txt)
            self.assertIn("JULY 31, 2026", html_txt)
            self.assertIn("Copy All Unique (1)", html_txt)
            self.assertIn("Open on Company Laptop (2)", html_txt)
            self.assertTrue(out["txt"].exists())
            self.assertTrue(out["csv"].exists())

    def test_ui_theme_strips_noise_and_hides_empty_manual_tab(self):
        self.assertEqual(format_date_words("2026-07-31"), "JULY 31, 2026")
        self.assertEqual(format_date_words("07-31-2026_MOTION_LIST.pdf"), "JULY 31, 2026")
        self.assertEqual(format_month_words("2026-07"), "JULY 2026")
        self.assertEqual(
            short_case_type("Appellate Division - 2nd Dept | Civil Action - General"),
            "Civil Action - General",
        )
        meta_corr = parse_row_meta(
            "APPELLANT'S BRIEF",
            "13 APPELLANT'S BRIEF *Corrected* THOMPSON, CHRISTOPHER H. Filed: 08/13/2026 Received: 09/23/2026 Processed Confirmation Notice",
        )
        self.assertEqual(meta_corr["doc_num"], "Doc #13")
        self.assertEqual(meta_corr["filed_date"], "Filed 08/13/2026")
        self.assertEqual(meta_corr["flag"], "Corrected")

        meta_pend = parse_row_meta(
            "APPELLANT'S BRIEF",
            "14 APPELLANT'S BRIEF Rothstein, Ronen Filed: 11/05/2026 Received: 11/05/2026 *** Pending *** Confirmation Notice",
        )
        self.assertEqual(meta_pend["flag"], "Pending")

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run_clean"
            meta = {"source_name": "07-31-2026_MOTION_LIST.pdf", "total_unique_cases": 2}
            _, briefs, note = classify_docket([
                {
                    "text": "13 APPELLANT'S BRIEF *Corrected* THOMPSON, CHRISTOPHER H. Filed: 08/13/2026 Received: 09/23/2026 Processed Confirmation Notice",
                    "links": [{"text": "APPELLANT'S BRIEF", "href": "https://iapps.courts.state.ny.us/nyscef/ViewDocument?docIndex=Z1"}],
                }
            ])
            records = {
                "2025-09410": CaseScanRecord(
                    case_number="2025-09410",
                    status=STATUS_HIT_VALID,
                    caption="DITECH v. ALBRECHT",
                    case_type="Appellate Division - 2nd Dept | Civil Action - General",
                    briefs=briefs,
                    note=note,
                ),
                "2022-04406": CaseScanRecord(
                    case_number="2022-04406",
                    status=STATUS_SEALED_OR_UNCLICKABLE,
                    caption="NACLERIO v. NACLERIO",
                    case_type="Appellate Division - 2nd Dept | Civil Action - Matrimonial",
                ),
            }
            paths = write_all_reports(run_dir, meta, ["2025-09410", "2022-04406"], records)
            clean_html = paths["html"].read_text(encoding="utf-8")
            self.assertNotIn("Processed Confirmation Notice", clean_html)
            self.assertNotIn("Appellate Division - 2nd Dept |", clean_html)
            self.assertNotIn("Needs Check (", clean_html)
            self.assertIn("Doc #13", clean_html)
            self.assertIn("Filed 08/13/2026", clean_html)
            self.assertIn("Open on Company Laptop (1)", clean_html)
            self.assertIn("Skipped Audit (1)", clean_html)

            # Adding a NEEDS_MANUAL_CHECK record MUST reveal Needs Check tab & KPI
            records["2026-00001"] = CaseScanRecord(
                case_number="2026-00001",
                status=STATUS_NEEDS_MANUAL_CHECK,
                note="Timeout waiting for docket",
            )
            paths2 = write_all_reports(run_dir, meta, ["2025-09410", "2022-04406", "2026-00001"], records)
            warn_html = paths2["html"].read_text(encoding="utf-8")
            self.assertIn("Needs Check (1)", warn_html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
