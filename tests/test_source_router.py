import io
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import collect_3gpp_evidence as collector
import source_router
import transfer_runtime


def valid_zip_bytes() -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("proposal.txt", "mirror")
    return payload.getvalue()


class SourceRouterTests(unittest.TestCase):
    def test_windows_file_uri_maps_to_unc(self):
        path = source_router.file_uri_to_path(
            "file://mirror.example.test/share/ftp/tsg_sa/",
            windows=True,
        )
        self.assertEqual(
            r"\\mirror.example.test\share\ftp\tsg_sa",
            str(path).rstrip("\\"),
        )

    def test_public_url_has_exact_and_ftp_root_mirror_layouts(self):
        relatives = source_router.mirror_relatives(
            "https://www.3gpp.org/ftp/tsg_ran/WG1_RL1/"
        )
        self.assertEqual(
            ["ftp/tsg_ran/WG1_RL1", "tsg_ran/WG1_RL1"],
            relatives,
        )
        with self.assertRaises(source_router.SourceRoutingError):
            source_router.join_mirror_source("C:/mirror", "../outside")

    def test_metadata_falls_back_to_directory_mirror_and_opens_circuit(self):
        with tempfile.TemporaryDirectory() as temp:
            mirror = Path(temp)
            first = mirror / "ftp" / "tsg_sa"
            second = mirror / "ftp" / "tsg_ran"
            (first / "WG2_Arch").mkdir(parents=True)
            (second / "WG1_RL1").mkdir(parents=True)
            router = source_router.SourceRouter(str(mirror))
            fetcher = collector.Fetcher(router)
            failure = urllib.error.URLError("public network unavailable")
            with mock.patch.object(
                collector.urllib.request,
                "urlopen",
                side_effect=failure,
            ) as opened:
                sa_links = collector.list_links(
                    fetcher,
                    "https://www.3gpp.org/ftp/tsg_sa/",
                )
                ran_links = collector.list_links(
                    fetcher,
                    "https://www.3gpp.org/ftp/tsg_ran/",
                )
            self.assertTrue(any("WG2_Arch" in link for link in sa_links))
            self.assertTrue(any("WG1_RL1" in link for link in ran_links))
            self.assertEqual(1, opened.call_count)
            self.assertTrue(router.public_unavailable)
            self.assertEqual(2, router.mirror_hits)
            self.assertEqual([], fetcher.failures)

    def test_named_meeting_resolves_entirely_from_mirror_after_outage(self):
        with tempfile.TemporaryDirectory() as temp:
            mirror = Path(temp)
            group = mirror / "ftp" / "tsg_sa" / "WG5_TM"
            (group / "S5-155").mkdir(parents=True)
            router = source_router.SourceRouter(str(mirror))
            fetcher = collector.Fetcher(router)
            with mock.patch.object(
                collector.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("offline"),
            ) as opened:
                result = collector.resolve_meeting(
                    fetcher,
                    "SA5#155",
                )
            self.assertEqual("resolved", result["status"])
            self.assertEqual(
                "https://www.3gpp.org/ftp/tsg_sa/WG5_TM/S5-155/",
                result["resolved"],
            )
            self.assertEqual(1, opened.call_count)
            self.assertGreaterEqual(router.mirror_hits, 2)

    def test_public_404_uses_mirror_without_opening_host_circuit(self):
        with tempfile.TemporaryDirectory() as temp:
            mirror = Path(temp)
            target = mirror / "ftp" / "example.txt"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"from mirror")
            router = source_router.SourceRouter(str(mirror))
            fetcher = collector.Fetcher(router)
            error = urllib.error.HTTPError(
                "https://www.3gpp.org/ftp/example.txt",
                404,
                "Not Found",
                None,
                None,
            )
            with mock.patch.object(
                collector.urllib.request,
                "urlopen",
                side_effect=error,
            ):
                body = fetcher.bytes(
                    "https://www.3gpp.org/ftp/example.txt"
                )
            self.assertEqual(b"from mirror", body)
            self.assertFalse(router.public_unavailable)
            self.assertEqual(1, router.mirror_hits)

    def test_downloader_falls_back_to_mirror_tdoc(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mirror_file = (
                root
                / "mirror"
                / "ftp"
                / "tsg_ran"
                / "WG1_RL1"
                / "RAN1_125"
                / "Docs"
                / "R1-2601001.zip"
            )
            mirror_file.parent.mkdir(parents=True)
            mirror_file.write_bytes(valid_zip_bytes())
            router = source_router.SourceRouter(str(root / "mirror"))
            downloader = transfer_runtime.StreamingDownloader(
                transfer_runtime.CacheManager(root / "cache"),
                root / "work",
                retries=3,
                router=router,
                sleep=lambda _: None,
            )
            task = transfer_runtime.DownloadTask(
                1,
                "R1-2601001",
                "https://www.3gpp.org/ftp/tsg_ran/WG1_RL1/RAN1_125/Docs/R1-2601001.zip",
            )
            with mock.patch.object(
                transfer_runtime.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("offline"),
            ) as opened:
                result = downloader.fetch(task)
            self.assertEqual("local", result.state)
            self.assertEqual("private_mirror", result.source_kind)
            self.assertEqual(str(mirror_file.resolve()), result.path)
            self.assertEqual(1, opened.call_count)
            self.assertTrue(router.public_unavailable)

    def test_downloader_404_uses_mirror_without_host_circuit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mirror_file = root / "mirror" / "ftp" / "missing.zip"
            mirror_file.parent.mkdir(parents=True)
            mirror_file.write_bytes(valid_zip_bytes())
            router = source_router.SourceRouter(str(root / "mirror"))
            downloader = transfer_runtime.StreamingDownloader(
                transfer_runtime.CacheManager(root / "cache"),
                root / "work",
                retries=3,
                router=router,
            )
            source = "https://www.3gpp.org/ftp/missing.zip"
            failure = urllib.error.HTTPError(
                source,
                404,
                "Not Found",
                None,
                None,
            )
            with mock.patch.object(
                transfer_runtime.urllib.request,
                "urlopen",
                side_effect=failure,
            ) as opened:
                result = downloader.fetch(
                    transfer_runtime.DownloadTask(
                        1,
                        "S5-2601001",
                        source,
                    )
                )
            self.assertEqual("local", result.state)
            self.assertEqual(1, opened.call_count)
            self.assertFalse(router.public_unavailable)

    def test_file_uri_meeting_is_resolved(self):
        with tempfile.TemporaryDirectory() as temp:
            meeting = Path(temp) / "meeting"
            meeting.mkdir()
            result = collector.resolve_meeting(
                collector.Fetcher(
                    source_router.SourceRouter(mirror_enabled=False)
                ),
                meeting.as_uri(),
            )
            self.assertEqual("resolved", result["status"])
            self.assertEqual("file_uri", result["kind"])

    def test_valid_cache_is_used_after_public_and_mirror_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache = transfer_runtime.CacheManager(root / "cache")
            source = "https://www.3gpp.org/ftp/tsg_ct/example/C3-2601001.zip"
            payload = cache.payload_path(source)
            payload.parent.mkdir(parents=True)
            payload.write_bytes(valid_zip_bytes())
            digest = transfer_runtime.file_sha256(payload)
            transfer_runtime.atomic_json(
                cache.meta_path(source),
                {"sha256": digest},
            )
            router = source_router.SourceRouter(str(root / "missing-mirror"))
            downloader = transfer_runtime.StreamingDownloader(
                cache,
                root / "work",
                retries=0,
                router=router,
            )
            with mock.patch.object(
                transfer_runtime.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("offline"),
            ):
                result = downloader.fetch(
                    transfer_runtime.DownloadTask(
                        1,
                        "C3-2601001",
                        source,
                    )
                )
            self.assertEqual("cached", result.state)
            self.assertEqual("stale_cache", result.source_kind)
            self.assertEqual(1, router.stale_cache_hits)

    def test_valid_metadata_cache_is_used_after_both_sources_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache = transfer_runtime.CacheManager(root / "cache")
            source = "https://www.3gpp.org/ftp/tsg_sa/TdocsByAgenda.htm"
            payload = cache.payload_path(source)
            payload.parent.mkdir(parents=True)
            payload.write_bytes(b"<html>cached agenda</html>")
            transfer_runtime.atomic_json(
                cache.meta_path(source),
                {
                    "sha256": transfer_runtime.file_sha256(payload),
                    "partial": False,
                },
            )
            router = source_router.SourceRouter(str(root / "missing-mirror"))
            fetcher = collector.Fetcher(router)
            with mock.patch.object(
                collector.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("offline"),
            ):
                body = collector.cached_metadata_bytes(
                    fetcher,
                    cache,
                    source,
                    None,
                    refresh=False,
                )
            self.assertEqual(b"<html>cached agenda</html>", body)
            self.assertEqual(1, router.stale_cache_hits)
            self.assertEqual("stale_cache", fetcher.effective_sources[source]["source_kind"])

    def test_private_mirror_path_is_redacted_from_failures(self):
        with tempfile.TemporaryDirectory() as temp:
            mirror = str(Path(temp) / "private-host-share")
            router = source_router.SourceRouter(mirror)
            fetcher = collector.Fetcher(router)
            with mock.patch.object(
                collector.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("offline"),
            ):
                with self.assertRaises(collector.CollectorError) as raised:
                    fetcher.bytes(
                        "https://www.3gpp.org/ftp/missing.txt"
                    )
            self.assertNotIn(mirror, str(raised.exception))
            self.assertIn("<private-mirror>", str(raised.exception))
            self.assertNotIn(mirror, str(router.fallbacks))

    def test_environment_override_and_no_mirror(self):
        with mock.patch.dict(
            source_router.os.environ,
            {source_router.MIRROR_ENV: "C:/configured-mirror"},
        ):
            configured = source_router.SourceRouter()
        self.assertEqual("C:/configured-mirror", configured.mirror_root)
        disabled = source_router.SourceRouter(mirror_enabled=False)
        candidates = disabled.candidates(
            "https://www.3gpp.org/ftp/tsg_sa/"
        )
        self.assertEqual(["public"], [item.kind for item in candidates])

    def test_readme_does_not_disclose_default_private_mirror(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn(source_router.DEFAULT_MIRROR_ROOT, readme)
        self.assertNotIn(
            source_router.file_uri_to_path(
                source_router.DEFAULT_MIRROR_ROOT,
                windows=True,
            ).parts[0],
            readme,
        )


if __name__ == "__main__":
    unittest.main()
