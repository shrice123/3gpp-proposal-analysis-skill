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


if __name__ == "__main__":
    unittest.main()
