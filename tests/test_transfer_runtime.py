import io
import json
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import collect_3gpp_evidence as collector
import transfer_runtime as runtime


def proposal_zip(text: str = "proposal") -> bytes:
    docx = io.BytesIO()
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>",
        )
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as archive:
        archive.writestr("proposal.docx", docx.getvalue())
    return outer.getvalue()


class FixtureHandler(BaseHTTPRequestHandler):
    payloads = {}
    etags = {}
    delays = {}
    failures = {}
    incomplete_once = set()
    active = 0
    maximum_active = 0
    body_responses = 0
    requests = []
    guard = threading.Lock()

    @classmethod
    def reset(cls):
        cls.payloads = {}
        cls.etags = {}
        cls.delays = {}
        cls.failures = {}
        cls.incomplete_once = set()
        cls.active = 0
        cls.maximum_active = 0
        cls.body_responses = 0
        cls.requests = []

    def log_message(self, *args):
        return

    def do_GET(self):
        cls = type(self)
        with cls.guard:
            cls.active += 1
            cls.maximum_active = max(cls.maximum_active, cls.active)
            cls.requests.append({key: value for key, value in self.headers.items()})
        try:
            delay = cls.delays.get(self.path, 0)
            if delay:
                time.sleep(delay)
            failures = cls.failures.get(self.path, [])
            if failures:
                status = failures.pop(0)
                self.send_response(status)
                if status == 429:
                    self.send_header("Retry-After", "0")
                self.end_headers()
                return
            payload = cls.payloads.get(self.path)
            if payload is None:
                self.send_response(404)
                self.end_headers()
                return
            etag = cls.etags.get(self.path, '"fixture-v1"')
            if self.headers.get("If-None-Match") == etag and not self.headers.get("Range"):
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            start = 0
            range_header = self.headers.get("Range")
            if range_header:
                start = int(range_header.split("=", 1)[1].split("-", 1)[0])
                if start >= len(payload):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(payload)}")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(payload)-1}/{len(payload)}")
            else:
                self.send_response(200)
            body = payload[start:]
            if self.path in cls.incomplete_once:
                cls.incomplete_once.remove(self.path)
                self.send_header("Content-Length", str(len(body) + 20))
                self.send_header("ETag", etag)
                self.end_headers()
                self.wfile.write(body[: max(1, len(body) // 2)])
                self.close_connection = True
                return
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", "Wed, 17 Jun 2026 10:29:35 GMT")
            self.end_headers()
            self.wfile.write(body)
            with cls.guard:
                cls.body_responses += 1
        finally:
            with cls.guard:
                cls.active -= 1


class LocalServer:
    def __enter__(self):
        FixtureHandler.reset()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class TransferRuntimeTests(unittest.TestCase):
    def test_parallel_download_is_bounded_and_faster(self):
        with LocalServer() as server, tempfile.TemporaryDirectory() as temp:
            tasks = []
            for index in range(20):
                path = f"/S2-260{index:04d}.zip"
                FixtureHandler.payloads[path] = proposal_zip(str(index))
                FixtureHandler.delays[path] = 0.06
                tasks.append(runtime.DownloadTask(1, f"S2-260{index:04d}", server.base + path))
            serial_downloader = runtime.StreamingDownloader(
                runtime.CacheManager(Path(temp) / "serial-cache", enabled=False),
                Path(temp) / "serial",
                retries=0,
                refresh=True,
            )
            started = time.monotonic()
            serial, serial_metrics = runtime.adaptive_pipeline(
                tasks, serial_downloader, lambda result: result.sha256, max_concurrency=1, parse_workers=1
            )
            serial_seconds = time.monotonic() - started
            FixtureHandler.maximum_active = 0
            parallel_downloader = runtime.StreamingDownloader(
                runtime.CacheManager(Path(temp) / "parallel-cache", enabled=False),
                Path(temp) / "parallel",
                retries=0,
                refresh=True,
            )
            started = time.monotonic()
            parallel, parallel_metrics = runtime.adaptive_pipeline(
                tasks, parallel_downloader, lambda result: result.sha256, max_concurrency=4, parse_workers=2
            )
            parallel_seconds = time.monotonic() - started
            self.assertEqual(20, len(serial))
            self.assertEqual(20, len(parallel))
            self.assertLessEqual(parallel_seconds, serial_seconds * 0.40)
            self.assertEqual(1, serial_metrics.maximum_active)
            self.assertLessEqual(parallel_metrics.maximum_active, 4)
            self.assertGreaterEqual(parallel_metrics.maximum_active, 2)

    def test_conditional_cache_etag_refresh_and_parsed_cache(self):
        with LocalServer() as server, tempfile.TemporaryDirectory() as temp:
            path = "/S2-2608000.zip"
            FixtureHandler.payloads[path] = proposal_zip("first")
            FixtureHandler.etags[path] = '"v1"'
            cache = runtime.CacheManager(Path(temp) / "cache")
            task = runtime.DownloadTask(1, "S2-2608000", server.base + path)
            downloader = runtime.StreamingDownloader(cache, Path(temp) / "work", retries=0)
            first = downloader.fetch(task)
            self.assertEqual("downloaded", first.state)
            cache.save_parsed(task.source, first.sha256, collector.PARSER_VERSION, {"ok": True})
            second = downloader.fetch(task)
            self.assertEqual("cached", second.state)
            self.assertEqual(0, second.body_bytes)
            self.assertEqual({"ok": True}, cache.load_parsed(task.source, first.sha256, collector.PARSER_VERSION))
            FixtureHandler.payloads[path] = proposal_zip("second")
            FixtureHandler.etags[path] = '"v2"'
            third = downloader.fetch(task)
            self.assertEqual("downloaded", third.state)
            self.assertNotEqual(first.sha256, third.sha256)
            refreshed = runtime.StreamingDownloader(cache, Path(temp) / "work", retries=0, refresh=True).fetch(task)
            self.assertEqual("downloaded", refreshed.state)

    def test_metadata_cache_uses_conditional_get_and_zero_body_bytes(self):
        with LocalServer() as server, tempfile.TemporaryDirectory() as temp:
            path = "/TdocsByAgenda.htm"
            FixtureHandler.payloads[path] = b"<html><body>agenda</body></html>"
            cache = runtime.CacheManager(Path(temp) / "cache")
            first_fetcher = collector.Fetcher()
            first = collector.cached_metadata_bytes(
                first_fetcher, cache, server.base + path, server.base + "/", refresh=False
            )
            second_fetcher = collector.Fetcher()
            second = collector.cached_metadata_bytes(
                second_fetcher, cache, server.base + path, server.base + "/", refresh=False
            )
            self.assertEqual(first, second)
            self.assertGreater(first_fetcher.body_bytes, 0)
            self.assertEqual(second_fetcher.body_bytes, 0)
            self.assertEqual(second_fetcher.cache_hits, {server.base + path})

    def test_range_resume_206_and_416(self):
        with LocalServer() as server, tempfile.TemporaryDirectory() as temp:
            path = "/S2-2608001.zip"
            payload = proposal_zip("resume")
            FixtureHandler.payloads[path] = payload
            FixtureHandler.etags[path] = '"resume"'
            cache = runtime.CacheManager(Path(temp) / "cache")
            task = runtime.DownloadTask(1, "S2-2608001", server.base + path)
            payload_path = cache.payload_path(task.source)
            payload_path.parent.mkdir(parents=True, exist_ok=True)
            partial = payload_path.with_name(payload_path.name + ".part")
            partial.write_bytes(payload[: len(payload) // 2])
            runtime.atomic_json(cache.partial_meta_path(task.source), {"etag": '"resume"'})
            downloader = runtime.StreamingDownloader(cache, Path(temp) / "work", retries=0)
            result = downloader.fetch(task)
            self.assertEqual("downloaded", result.state)
            self.assertGreater(result.resumed_bytes, 0)
            self.assertTrue(any(request.get("Range") for request in FixtureHandler.requests))

            payload_path.unlink()
            partial.write_bytes(payload)
            runtime.atomic_json(cache.partial_meta_path(task.source), {"etag": '"resume"'})
            complete = downloader.fetch(task)
            self.assertEqual("resumed_complete", complete.state)
            self.assertEqual(len(payload), complete.resumed_bytes)

    def test_retry_after_incomplete_response_and_adaptive_pressure(self):
        with LocalServer() as server, tempfile.TemporaryDirectory() as temp:
            first_path = "/S2-2608002.zip"
            second_path = "/S2-2608003.zip"
            third_path = "/S2-2608006.zip"
            FixtureHandler.payloads[first_path] = proposal_zip("retry")
            FixtureHandler.payloads[second_path] = proposal_zip("incomplete")
            FixtureHandler.payloads[third_path] = proposal_zip("forbidden")
            FixtureHandler.failures[first_path] = [429]
            FixtureHandler.failures[third_path] = [403]
            FixtureHandler.incomplete_once.add(second_path)
            downloader = runtime.StreamingDownloader(
                runtime.CacheManager(Path(temp) / "cache"),
                Path(temp) / "work",
                retries=2,
                sleep=lambda _: None,
            )
            tasks = [
                runtime.DownloadTask(1, "S2-2608002", server.base + first_path),
                runtime.DownloadTask(1, "S2-2608003", server.base + second_path),
                runtime.DownloadTask(1, "S2-2608006", server.base + third_path),
            ]
            pairs, metrics = runtime.adaptive_pipeline(
                tasks, downloader, lambda result: result.sha256, max_concurrency=4, parse_workers=1
            )
            self.assertTrue(all(result.state != "fetch_error" for result, _ in pairs))
            self.assertGreaterEqual(metrics.pressure_events, 3)
            self.assertLess(metrics.final_window, 4)
            self.assertGreaterEqual(sum(result.retries for result, _ in pairs), 3)

    def test_cache_lock_prevents_duplicate_body_download(self):
        with LocalServer() as server, tempfile.TemporaryDirectory() as temp:
            path = "/S2-2608004.zip"
            FixtureHandler.payloads[path] = proposal_zip("lock")
            FixtureHandler.delays[path] = 0.1
            cache = runtime.CacheManager(Path(temp) / "cache")
            downloader = runtime.StreamingDownloader(cache, Path(temp) / "work", retries=0)
            task = runtime.DownloadTask(1, "S2-2608004", server.base + path)
            results = []
            threads = [threading.Thread(target=lambda: results.append(downloader.fetch(task))) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertEqual(2, len(results))
            self.assertEqual(1, FixtureHandler.body_responses)
            self.assertEqual({"cached", "downloaded"}, {result.state for result in results})

    def test_cache_commands_require_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / "cache"
            manager = runtime.CacheManager(cache)
            (cache / "item").mkdir()
            (cache / "item" / "payload.zip").write_bytes(proposal_zip())
            self.assertEqual(2, collector.main(["cache", "clear", "--cache-dir", str(cache)]))
            self.assertTrue((cache / "item" / "payload.zip").exists())
            self.assertEqual(0, collector.main(["cache", "clear", "--cache-dir", str(cache), "--yes"]))
            self.assertFalse((cache / "item").exists())

    def test_streaming_downloader_never_requests_the_whole_body(self):
        payload = proposal_zip("streamed")

        class ChunkOnlyResponse:
            status = 200
            headers = {"Content-Length": str(len(payload)), "ETag": '"stream"'}

            def __init__(self):
                self.offset = 0
                self.read_sizes = []

            def getcode(self):
                return self.status

            def read(self, size):
                if size < 0:
                    raise AssertionError("whole-body read is forbidden")
                self.read_sizes.append(size)
                chunk = payload[self.offset:self.offset + size]
                self.offset += len(chunk)
                return chunk

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with tempfile.TemporaryDirectory() as temp:
            response = ChunkOnlyResponse()
            cache = runtime.CacheManager(Path(temp) / "cache", enabled=False)
            downloader = runtime.StreamingDownloader(cache, Path(temp) / "work", retries=0)
            task = runtime.DownloadTask(1, "S2-2608005", "https://example.invalid/S2-2608005.zip")
            with mock.patch.object(runtime.urllib.request, "urlopen", return_value=response):
                result = downloader.fetch(task)
            self.assertEqual("downloaded", result.state)
            self.assertTrue(response.read_sizes)
            self.assertTrue(all(size == runtime.CHUNK_SIZE for size in response.read_sizes))


if __name__ == "__main__":
    unittest.main()
