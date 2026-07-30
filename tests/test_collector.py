import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "collect_3gpp_evidence.py"
SPEC = importlib.util.spec_from_file_location("collect_3gpp_evidence", SCRIPT)
COLLECTOR = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(COLLECTOR)


def make_xlsx(path: Path, rows: list[list[str]]) -> None:
    row_xml = []
    for row_no, row in enumerate(rows, 1):
        cells = "".join(
            f'<c r="{chr(65 + index)}{row_no}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
            for index, value in enumerate(row)
        )
        row_xml.append(f'<row r="{row_no}">{cells}</row>')
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            + "".join(row_xml)
            + "</sheetData></worksheet>",
        )


def make_docx_bytes(paragraphs: list[str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
            + "".join(f"<w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>" for text in paragraphs)
            + "</w:body></w:document>",
        )
    return buffer.getvalue()


def make_tdoc_zip(path: Path, tdoc: str, paragraphs: list[str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{tdoc}.docx", make_docx_bytes(paragraphs))


class LightweightProposalSkillTests(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *args], check=False, capture_output=True, text=True)

    def build_golden_fixture(self, root: Path) -> None:
        make_xlsx(
            root / "agenda.xlsx",
            [
                ["TDoc", "Title", "Source", "Status", "Agenda Item", "Comments"],
                ["S2-2605845", "KI#18 Solution Variant#18.7 intent structure baseline", "Nokia", "Not Handled", "10.18", "Baseline incl. S2-2606500, S2-2606432. Revised to S2-2606605."],
                ["S2-2606432", "KI#11 unrelated security comment", "Company B", "Not Handled", "10.18", "Merge into S2-2605845. Incorrect AI and merge assignment."],
                ["S2-2606500", "KI#18 input for intent structure", "Company C", "Merge into S2-2606605", "10.18", ""],
                ["S2-2606605", "KI#18 Solution Variant#18.7 approved intent structure revision", "Nokia, Company C", "Approved", "10.18", "Revision of S2-2605845."],
            ],
        )
        make_tdoc_zip(root / "S2-2605845.zip", "S2-2605845", ["KI#18 Solution Variant#18.7. Baseline proposal for intent structure."])
        make_tdoc_zip(root / "S2-2606432.zip", "S2-2606432", ["KI#18. This document was Not Handled. It does not reference the intent baseline."])
        make_tdoc_zip(root / "S2-2606500.zip", "S2-2606500", ["KI#18 Solution Variant#18.7. Merge this proposal into S2-2606605."])
        make_tdoc_zip(root / "S2-2606605.zip", "S2-2606605", ["Approved revision of S2-2605845 for KI#18 Solution Variant#18.7."])

    def test_golden_chain_and_row_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            meeting = root / "meeting"
            output = root / "output"
            meeting.mkdir()
            self.build_golden_fixture(meeting)
            result = self.run_cli("collect", "--meeting", str(meeting), "--query", "KI#18 Solution Variant#18.7 intent structure", "--output", str(output))
            self.assertEqual(result.returncode, 0, result.stderr)
            preview = json.loads((output / "scope_preview.json").read_text(encoding="utf-8"))
            self.assertIn("S2-2605845", {item["tdoc"] for item in preview["candidates"]})
            self.assertIn("S2-2606605", {item["tdoc"] for item in preview["candidates"]})
            self.assertNotIn("S2-2606432", {item["tdoc"] for item in preview["candidates"]})
            relations = json.loads((output / "relationships.json").read_text(encoding="utf-8"))["relationships"]
            edges = {(item["from"], item["to"], item["type"]) for item in relations}
            self.assertIn(("S2-2606605", "S2-2605845", "revision_of"), edges)
            self.assertIn(("S2-2606500", "S2-2606605", "merged_into"), edges)
            self.assertFalse(any(item["from"] == "S2-2606432" and item["classification"] == "candidate" for item in relations))
            self.assertTrue(any(item["from"] == "S2-2606432" and item["classification"] == "invalidated" for item in relations))

    def test_vague_preview_uses_actual_agenda_clusters(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            meeting = root / "meeting"
            output = root / "output"
            meeting.mkdir()
            self.build_golden_fixture(meeting)
            result = self.run_cli("preview", "--meeting", str(meeting), "--query", "AI intent", "--output", str(output))
            self.assertEqual(result.returncode, 0, result.stderr)
            preview = json.loads((output / "scope_preview.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(preview["agenda_record_count"], 4)
            self.assertTrue(all(item["tdoc"].startswith("S2-") for item in preview["candidates"]))
            for name in ("manifest.json", "relationships.json", "evidence.jsonl", "coverage.json"):
                self.assertTrue((output / name).exists())

    def test_bad_zip_and_company_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            meeting = root / "meeting"
            output = root / "output"
            meeting.mkdir()
            make_xlsx(meeting / "agenda.xlsx", [["TDoc", "Title", "Source", "Status"], ["S2-2607000", "AI proposal", "Example Labs", "Approved"]])
            (meeting / "S2-2607000.zip").write_bytes(b"not-a-zip")
            aliases = root / "aliases.json"
            aliases.write_text(json.dumps({"Example": ["Example Labs"]}), encoding="utf-8")
            result = self.run_cli("collect", "--meeting", str(meeting), "--query", "AI", "--company", "Example", "--aliases", str(aliases), "--output", str(output))
            self.assertEqual(result.returncode, 0, result.stderr)
            coverage = json.loads((output / "coverage.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(coverage["unsupported_files"], 1)

    def test_multiple_local_meetings_are_combined(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "meeting-a"
            second = root / "meeting-b"
            output = root / "output"
            first.mkdir()
            second.mkdir()
            make_xlsx(first / "agenda.xlsx", [["TDoc", "Title", "Source", "Status"], ["S2-2607001", "KI#18 AI intent", "Company A", "Approved"]])
            make_xlsx(second / "agenda.xlsx", [["TDoc", "Title", "Source", "Status"], ["S2-2607002", "KI#18 AI intent follow-up", "Company B", "Revised"]])
            result = self.run_cli(
                "preview",
                "--meeting", str(first),
                "--meeting", str(second),
                "--query", "KI#18 AI intent",
                "--output", str(output),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            preview = json.loads((output / "scope_preview.json").read_text(encoding="utf-8"))
            self.assertEqual(len(preview["meetings"]), 2)
            self.assertEqual({"S2-2607001", "S2-2607002"}, {item["tdoc"] for item in preview["candidates"]})

    def test_unresolved_meeting_still_writes_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "output"
            result = self.run_cli("preview", "--meeting", "not-a-real-meeting", "--query", "AI", "--output", str(output))
            self.assertEqual(result.returncode, 2)
            preview = json.loads((output / "scope_preview.json").read_text(encoding="utf-8"))
            self.assertEqual(preview["meetings"][0]["confidence"], "unresolved")
            self.assertEqual(json.loads((output / "coverage.json").read_text(encoding="utf-8"))["completeness"], "partial")

    def test_fetch_failure_and_required_headers_are_auditable(self) -> None:
        fetcher = COLLECTOR.Fetcher()
        with mock.patch.object(COLLECTOR.urllib.request, "urlopen", side_effect=urllib.error.URLError("403 forbidden")) as opened:
            with self.assertRaises(COLLECTOR.CollectorError):
                fetcher.bytes("https://example.invalid/meeting/", "https://example.invalid/")
        request = opened.call_args.args[0]
        self.assertIn("3GPP-evidence-collector", request.get_header("User-agent"))
        self.assertEqual(request.get_header("Referer"), "https://example.invalid/")
        self.assertEqual(len(fetcher.failures), 1)

    def test_zip_slip_member_is_ignored(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("../escape.docx", make_docx_bytes(["must not escape"]))
            archive.writestr("safe.docx", make_docx_bytes(["safe"]))
        members = COLLECTOR.extract_archive("S2-2607003.zip", buffer.getvalue())
        self.assertEqual(["safe.docx"], [name for name, _ in members])

    def test_core_then_complete_resumes_and_produces_schema_v2_diffs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            meeting = root / "meeting"
            output = root / "output"
            meeting.mkdir()
            self.build_golden_fixture(meeting)
            common = (
                "--meeting", str(meeting),
                "--query", "KI#18 Solution Variant#18.7 intent structure",
                "--output", str(output),
                "--no-cache",
            )
            core = self.run_cli("collect", *common, "--stage", "core", "--max-concurrency", "2")
            self.assertEqual(core.returncode, 0, core.stderr)
            core_manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            core_ids = {item["tdoc"] for item in core_manifest["documents"] if item["state"].endswith("processed")}
            self.assertEqual({"S2-2605845", "S2-2606605"}, core_ids)
            diffs = json.loads((output / "diffs.json").read_text(encoding="utf-8"))["diffs"]
            self.assertTrue(any(item["from"] == "S2-2606605" and item["to"] == "S2-2605845" for item in diffs))
            self.assertTrue((output / "document_index.jsonl").stat().st_size > 0)

            complete = self.run_cli("collect", *common, "--stage", "complete", "--max-concurrency", "4")
            self.assertEqual(complete.returncode, 0, complete.stderr)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            coverage = json.loads((output / "coverage.json").read_text(encoding="utf-8"))
            preview = json.loads((output / "scope_preview.json").read_text(encoding="utf-8"))
            self.assertEqual(2, manifest["schema_version"])
            self.assertEqual(2, coverage["schema_version"])
            self.assertGreaterEqual(coverage["resumed_documents"], 2)
            processed_ids = {item["tdoc"] for item in manifest["documents"] if item["state"].endswith("processed")}
            self.assertIn("S2-2606500", processed_ids)
            self.assertNotIn("S2-2606432", {item["tdoc"] for item in preview["candidates"]})

    def test_body_relationship_adds_a_new_download_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            meeting = root / "meeting"
            output = root / "output"
            meeting.mkdir()
            make_xlsx(
                meeting / "agenda.xlsx",
                [
                    ["TDoc", "Title", "Source", "Status"],
                    ["S2-2608100", "KI#18 AI intent baseline", "Company A", "Approved"],
                    ["S2-2608101", "Unrelated follow-up", "Company B", "Not Handled"],
                ],
            )
            make_tdoc_zip(meeting / "S2-2608100.zip", "S2-2608100", ["KI#18 AI intent. Revision of S2-2608101."])
            make_tdoc_zip(meeting / "S2-2608101.zip", "S2-2608101", ["Historical evidence."])
            result = self.run_cli(
                "collect",
                "--meeting", str(meeting),
                "--query", "KI#18 AI intent",
                "--output", str(output),
                "--no-cache",
                "--batch-size", "1",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            processed = {item["tdoc"] for item in manifest["documents"] if item["state"].endswith("processed")}
            self.assertEqual({"S2-2608100", "S2-2608101"}, processed)

    def test_all_sa_ran_ct_working_groups_are_discovered_from_parent_indexes(self) -> None:
        fetcher = COLLECTOR.Fetcher()
        for family in ("SA", "RAN", "CT"):
            for number in range(1, 7):
                group = f"{family}{number}"
                parent = COLLECTOR.TSG_ROOTS[family]
                expected = f"{parent}WG{number}_Representative/"
                with self.subTest(group=group), mock.patch.object(
                    COLLECTOR,
                    "cached_links",
                    return_value=[expected],
                ):
                    resolved, candidates = COLLECTOR.discover_group_root(
                        fetcher,
                        None,
                        group,
                        refresh=False,
                    )
                    self.assertEqual(expected, resolved)
                    self.assertEqual([expected], candidates)

    def test_meeting_descriptor_accepts_number_suffix_and_date_formats(self) -> None:
        cases = {
            "SA5#167": ("SA5", "167", "", None, None),
            "RAN1#125-AH-e": ("RAN1", "125", "ah-e", None, None),
            "CT3 2026-05": ("CT3", None, "", 2026, 5),
            "SA5 May 2026": ("SA5", None, "", 2026, 5),
            "RAN1 2026年5月": ("RAN1", None, "", 2026, 5),
        }
        for hint, expected in cases.items():
            with self.subTest(hint=hint):
                parsed = COLLECTOR.parse_meeting_descriptor(hint)
                self.assertEqual(expected, (parsed["group"], parsed["number"], parsed["suffix"], parsed["year"], parsed["month"]))

        explicit = COLLECTOR.resolve_meeting(
            COLLECTOR.Fetcher(),
            "https://www.3gpp.org/ftp/tsg_sa/WG5_TM/TSGS5_167",
        )
        self.assertEqual("resolved", explicit["status"])
        self.assertEqual("explicit", explicit["confidence"])
        self.assertTrue(explicit["selected_url"].endswith("/"))

        invalid_group = COLLECTOR.resolve_meeting(COLLECTOR.Fetcher(), "SA7#100")
        self.assertEqual("unresolved", invalid_group["status"])

    def test_representative_group_numbers_resolve_without_static_group_map(self) -> None:
        fetcher = COLLECTOR.Fetcher()
        fixtures = {
            "SA2#175-AH-e": (
                COLLECTOR.TSG_ROOTS["SA"] + "WG2_Arch/",
                "TSGS2_175-AH-e",
            ),
            "SA5#167": (
                COLLECTOR.TSG_ROOTS["SA"] + "WG5_TM/",
                "TSGS5_167",
            ),
            "RAN1#125": (
                COLLECTOR.TSG_ROOTS["RAN"] + "WG1_RL1/",
                "TSGR1_125",
            ),
            "CT3#147": (
                COLLECTOR.TSG_ROOTS["CT"] + "WG3_interworking_ex-CN3/",
                "TSGC3_147",
            ),
        }

        def links(_fetcher, _cache, url, *, refresh):
            del _fetcher, _cache, refresh
            for root, meeting in fixtures.values():
                if url == root:
                    return [root + meeting]
            family = next(key for key, root in COLLECTOR.TSG_ROOTS.items() if root == url)
            roots = [root for root, _meeting in fixtures.values() if root.startswith(COLLECTOR.TSG_ROOTS[family])]
            return sorted(set(roots))

        with mock.patch.object(COLLECTOR, "cached_links", side_effect=links):
            for hint, (root, meeting) in fixtures.items():
                with self.subTest(hint=hint):
                    resolved = COLLECTOR.resolve_meeting(fetcher, hint)
                    self.assertEqual("resolved", resolved["status"])
                    self.assertEqual(root + meeting + "/", resolved["selected_url"])

    def test_date_resolution_uses_official_calendar_and_detects_ambiguity(self) -> None:
        fetcher = COLLECTOR.Fetcher()
        parent = COLLECTOR.TSG_ROOTS["CT"]
        root = parent + "WG3_interworking_ex-CN3/"
        calendar_html = """
        <table>
          <tr><td>C3-147</td><td>3GPPCT3#147</td><td>China</td><td>2026-05-18</td><td>2026-05-22</td></tr>
        </table>
        """

        def links(_fetcher, _cache, url, *, refresh):
            del _fetcher, _cache, refresh
            if url == parent:
                return [root]
            if url == root:
                return [root + "TSGC3_147", root + "TSGC3_147-bis"]
            return []

        with (
            mock.patch.object(COLLECTOR, "cached_text", return_value=calendar_html),
            mock.patch.object(COLLECTOR, "cached_links", side_effect=links),
        ):
            resolved = COLLECTOR.resolve_meeting(fetcher, "CT3 May 2026")
        self.assertEqual("ambiguous", resolved["status"])
        self.assertIsNone(resolved["resolved"])
        self.assertEqual("2026-05-18", resolved["calendar_matches"][0]["start_date"])
        self.assertEqual(2, len(resolved["candidates"]))

        multiple_calendar_html = """
        <table>
          <tr><td>S5-167</td><td>3GPPSA5#167</td><td>2026-05-18</td><td>2026-05-22</td></tr>
          <tr><td>S5-167-CH</td><td>3GPPSA5#167-CH SWG</td><td>2026-05-18</td><td>2026-05-22</td></tr>
        </table>
        """
        sa_parent = COLLECTOR.TSG_ROOTS["SA"]
        sa_root = sa_parent + "WG5_TM/"

        def sa_links(_fetcher, _cache, url, *, refresh):
            del _fetcher, _cache, refresh
            return [sa_root] if url == sa_parent else [sa_root + "TSGS5_167"]

        with (
            mock.patch.object(COLLECTOR, "cached_text", return_value=multiple_calendar_html),
            mock.patch.object(COLLECTOR, "cached_links", side_effect=sa_links),
        ):
            main_meeting = COLLECTOR.resolve_meeting(fetcher, "SA5 May 2026")
        self.assertEqual("resolved", main_meeting["status"])
        self.assertEqual("SA5#167-CH", main_meeting["excluded_calendar_matches"][0]["meeting"])

        two_working_group_html = """
        <table>
          <tr><td>S5-167</td><td>3GPPSA5#167</td><td>2026-05-18</td><td>2026-05-22</td></tr>
          <tr><td>S5-168</td><td>3GPPSA5#168</td><td>2026-05-25</td><td>2026-05-29</td></tr>
        </table>
        """
        with mock.patch.object(COLLECTOR, "cached_text", return_value=two_working_group_html):
            multiple = COLLECTOR.resolve_meeting(fetcher, "SA5 May 2026")
        self.assertEqual("ambiguous", multiple["status"])
        self.assertEqual({"SA5#167", "SA5#168"}, {item["name"] for item in multiple["candidates"]})

    def test_explicit_tdoc_restricts_direct_scope_and_missing_tdoc_is_auditable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            meeting = root / "meeting"
            output = root / "output"
            meeting.mkdir()
            make_xlsx(
                meeting / "agenda.xlsx",
                [
                    ["TDoc", "Title", "Source", "Status"],
                    ["R1-2601000", "AI baseline", "Company A", "Approved"],
                    ["R1-2601001", "AI alternative", "Company B", "Not Handled"],
                ],
            )
            make_tdoc_zip(meeting / "R1-2601000.zip", "R1-2601000", ["AI baseline."])
            make_tdoc_zip(meeting / "R1-2601001.zip", "R1-2601001", ["AI alternative."])
            result = self.run_cli(
                "collect",
                "--meeting", str(meeting),
                "--query", "AI",
                "--include-tdoc", "R1-2601001",
                "--include-tdoc", "R1-2601999",
                "--output", str(output),
                "--no-cache",
                "--stage", "core",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            preview = json.loads((output / "scope_preview.json").read_text(encoding="utf-8"))
            self.assertEqual(["R1-2601001", "R1-2601999"], preview["included_tdocs"])
            self.assertEqual(
                {"R1-2601001", "R1-2601999"},
                {item["tdoc"] for item in preview["candidates"]},
            )
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            states = {item["tdoc"]: item["state"] for item in manifest["documents"]}
            self.assertTrue(states["R1-2601001"].endswith("processed"))
            self.assertEqual("missing", states["R1-2601999"])
            self.assertNotIn("R1-2601000", states)

            invalid = self.run_cli(
                "preview",
                "--meeting", str(meeting),
                "--query", "AI",
                "--include-tdoc", "not-a-tdoc",
                "--output", str(root / "invalid-output"),
                "--no-cache",
            )
            self.assertEqual(2, invalid.returncode)
            self.assertIn("--include-tdoc requires identifiers", invalid.stderr)

    def test_ambiguous_resolution_blocks_collection_before_downloader(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "output"
            args = COLLECTOR.argparse.Namespace(
                output=str(output),
                aliases=None,
                no_cache=True,
                cache_dir=None,
                meeting=["SA5 May 2026"],
                refresh=False,
                include_tdoc=[],
                query="AI",
                company=[],
                command="collect",
                retries=0,
                download="matched",
                stage="complete",
                max_concurrency=4,
                parse_workers=2,
                batch_size=8,
            )
            ambiguous = {
                "input": "SA5 May 2026",
                "status": "ambiguous",
                "confidence": "candidate",
                "kind": "name",
                "resolved": None,
                "selected_url": None,
                "candidates": [{"name": "SA5#167"}, {"name": "SA5#167-CH"}],
            }
            with (
                mock.patch.object(COLLECTOR, "resolve_meeting", return_value=ambiguous),
                mock.patch.object(COLLECTOR, "StreamingDownloader") as downloader,
            ):
                result = COLLECTOR.collect(args)
            self.assertEqual(2, result)
            downloader.assert_not_called()
            coverage = json.loads((output / "coverage.json").read_text(encoding="utf-8"))
            self.assertEqual("partial", coverage["completeness"])
            self.assertEqual(0, coverage["downloaded_archives"])


if __name__ == "__main__":
    unittest.main()
