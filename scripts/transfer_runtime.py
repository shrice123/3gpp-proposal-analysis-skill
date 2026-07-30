#!/usr/bin/env python3
"""Portable streaming transfer, cache, and bounded scheduling primitives."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import hashlib
import heapq
import http.client
import json
import os
import random
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable, Iterable

USER_AGENT = "Mozilla/5.0 (compatible; 3GPP-evidence-collector/2.0)"
CHUNK_SIZE = 256 * 1024
LOCK_STALE_SECONDS = 15 * 60


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}-{threading.get_ident()}")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def default_cache_root() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        return Path(base) / "3GPP Proposal Cache" if base else Path.home() / "AppData" / "Local" / "3GPP Proposal Cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "3gpp-proposal-analysis"
    base = os.environ.get("XDG_CACHE_HOME")
    return Path(base) / "3gpp-proposal-analysis" if base else Path.home() / ".cache" / "3gpp-proposal-analysis"


def normalized_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    path = urllib.parse.quote(urllib.parse.unquote(parsed.path), safe="/:@")
    return urllib.parse.urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), path, parsed.query, ""))


def cache_key(value: str) -> str:
    return hashlib.sha256(normalized_url(value).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def valid_zip(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return archive.testzip() is None
    except (OSError, zipfile.BadZipFile):
        return False


class TransferError(RuntimeError):
    def __init__(self, message: str, *, pressure: bool = False, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.pressure = pressure
        self.retry_after = retry_after


@dataclasses.dataclass
class DownloadTask:
    priority: int
    tdoc: str
    source: str
    referer: str | None = None


@dataclasses.dataclass
class DownloadResult:
    task: DownloadTask
    state: str
    path: str | None
    sha256: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    content_length: int | None = None
    body_bytes: int = 0
    resumed_bytes: int = 0
    retries: int = 0
    elapsed_seconds: float = 0.0
    cache_hit: bool = False
    parsed_cache_hit: bool = False
    pressure_events: int = 0
    error: str | None = None


class CacheManager:
    def __init__(self, root: Path | None, enabled: bool = True) -> None:
        self.enabled = enabled
        self.root = (root or default_cache_root()).expanduser().resolve()
        if self.enabled:
            self.root.mkdir(parents=True, exist_ok=True)

    def entry_dir(self, source: str) -> Path:
        return self.root / cache_key(source)

    def payload_path(self, source: str) -> Path:
        suffix = Path(urllib.parse.urlparse(source).path).suffix or ".bin"
        return self.entry_dir(source) / f"payload{suffix}"

    def meta_path(self, source: str) -> Path:
        return self.entry_dir(source) / "meta.json"

    def partial_meta_path(self, source: str) -> Path:
        return self.entry_dir(source) / "partial-meta.json"

    def read_meta(self, source: str) -> dict[str, Any]:
        if not self.enabled:
            return {}
        try:
            value = json.loads(self.meta_path(source).read_text(encoding="utf-8-sig"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def parsed_path(self, source: str, sha256: str, parser_version: str) -> Path:
        version = "".join(character if character.isalnum() or character in "._-" else "_" for character in parser_version)
        return self.entry_dir(source) / f"parsed-{version}-{sha256}.json"

    def load_parsed(self, source: str, sha256: str, parser_version: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        path = self.parsed_path(source, sha256, parser_version)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    def save_parsed(self, source: str, sha256: str, parser_version: str, value: dict[str, Any]) -> None:
        if self.enabled:
            atomic_json(self.parsed_path(source, sha256, parser_version), value)

    @contextlib.contextmanager
    def lock(self, source: str) -> Iterable[None]:
        if not self.enabled:
            yield
            return
        entry = self.entry_dir(source)
        entry.mkdir(parents=True, exist_ok=True)
        lock_path = entry / ".lock"
        deadline = time.monotonic() + 60
        while True:
            try:
                descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                    stream.write(f"{os.getpid()} {time.time()}\n")
                break
            except FileExistsError:
                try:
                    age = time.time() - lock_path.stat().st_mtime
                    if age > LOCK_STALE_SECONDS:
                        lock_path.unlink()
                        continue
                except OSError:
                    continue
                if time.monotonic() >= deadline:
                    raise TransferError(f"Timed out waiting for cache lock: {lock_path}")
                time.sleep(0.1)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                lock_path.unlink()

    def info(self) -> dict[str, Any]:
        if not self.root.exists():
            return {"path": str(self.root), "entries": 0, "files": 0, "bytes": 0}
        files = [path for path in self.root.rglob("*") if path.is_file() and path.name != ".lock"]
        entries = sum(1 for path in self.root.iterdir() if path.is_dir())
        return {"path": str(self.root), "entries": entries, "files": len(files), "bytes": sum(path.stat().st_size for path in files)}

    def clear(self) -> dict[str, Any]:
        before = self.info()
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        return before


class StreamingDownloader:
    def __init__(
        self,
        cache: CacheManager,
        work_dir: Path,
        *,
        retries: int = 3,
        refresh: bool = False,
        timeout: int = 45,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cache = cache
        self.work_dir = work_dir.resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.retries = retries
        self.refresh = refresh
        self.timeout = timeout
        self.sleep = sleep

    def _paths(self, source: str) -> tuple[Path, Path, Path]:
        if self.cache.enabled:
            payload = self.cache.payload_path(source)
            meta = self.cache.meta_path(source)
            partial_meta = self.cache.partial_meta_path(source)
        else:
            entry = self.work_dir / cache_key(source)
            entry.mkdir(parents=True, exist_ok=True)
            suffix = Path(urllib.parse.urlparse(source).path).suffix or ".bin"
            payload = entry / f"payload{suffix}"
            meta = entry / "meta.json"
            partial_meta = entry / "partial-meta.json"
        payload.parent.mkdir(parents=True, exist_ok=True)
        return payload, meta, partial_meta

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _validate_payload(self, source: str, path: Path) -> None:
        if not path.is_file() or path.stat().st_size == 0:
            raise TransferError(f"Downloaded file is empty: {source}", pressure=True)
        if Path(urllib.parse.urlparse(source).path).suffix.casefold() == ".zip" and not valid_zip(path):
            raise TransferError(f"Downloaded ZIP is invalid: {source}", pressure=True)

    def _local(self, task: DownloadTask) -> DownloadResult:
        started = time.monotonic()
        path = Path(task.source).resolve()
        if not path.is_file():
            return DownloadResult(task, "fetch_error", None, error=f"Local source missing: {path}")
        return DownloadResult(
            task,
            "local",
            str(path),
            sha256=file_sha256(path),
            content_length=path.stat().st_size,
            elapsed_seconds=time.monotonic() - started,
            cache_hit=True,
        )

    def fetch(self, task: DownloadTask) -> DownloadResult:
        parsed = urllib.parse.urlparse(task.source)
        if parsed.scheme not in ("http", "https"):
            return self._local(task)
        started = time.monotonic()
        pressure_events = 0
        total_body_bytes = 0
        total_resumed = 0
        last_error: str | None = None
        with self.cache.lock(task.source):
            payload, meta_path, partial_meta_path = self._paths(task.source)
            partial = payload.with_name(payload.name + ".part")
            meta = self._read_json(meta_path)
            for attempt in range(self.retries + 1):
                headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
                if task.referer:
                    headers["Referer"] = task.referer
                if payload.exists() and not self.refresh:
                    if meta.get("etag"):
                        headers["If-None-Match"] = str(meta["etag"])
                    elif meta.get("last_modified"):
                        headers["If-Modified-Since"] = str(meta["last_modified"])
                partial_meta = self._read_json(partial_meta_path)
                existing = partial.stat().st_size if partial.exists() else 0
                if existing and not self.refresh:
                    validator = partial_meta.get("etag") or partial_meta.get("last_modified")
                    if validator:
                        headers["Range"] = f"bytes={existing}-"
                        headers["If-Range"] = str(validator)
                request = urllib.request.Request(task.source, headers=headers)
                try:
                    response = urllib.request.urlopen(request, timeout=self.timeout)
                    status = getattr(response, "status", response.getcode())
                    response_headers = response.headers
                    if status == 206 and existing:
                        mode = "ab"
                        total_resumed = existing
                    else:
                        mode = "wb"
                        existing = 0
                    response_meta = {
                        "url": normalized_url(task.source),
                        "etag": response_headers.get("ETag"),
                        "last_modified": response_headers.get("Last-Modified"),
                        "content_length": int(response_headers["Content-Length"]) if response_headers.get("Content-Length", "").isdigit() else None,
                        "fetched_at": utc_now(),
                    }
                    atomic_json(partial_meta_path, response_meta)
                    received = 0
                    with response, partial.open(mode) as stream:
                        try:
                            while True:
                                chunk = response.read(CHUNK_SIZE)
                                if not chunk:
                                    break
                                stream.write(chunk)
                                received += len(chunk)
                        except http.client.IncompleteRead as exc:
                            if exc.partial:
                                stream.write(exc.partial)
                                received += len(exc.partial)
                            total_body_bytes += received
                            raise TransferError(
                                f"Incomplete response for {task.source}: received {received}",
                                pressure=True,
                            ) from exc
                    total_body_bytes += received
                    expected = response_meta["content_length"]
                    if expected is not None and received != expected:
                        raise TransferError(
                            f"Incomplete response for {task.source}: expected {expected}, received {received}",
                            pressure=True,
                        )
                    self._validate_payload(task.source, partial)
                    os.replace(partial, payload)
                    digest = file_sha256(payload)
                    final_meta = {
                        **response_meta,
                        "sha256": digest,
                        "content_length": payload.stat().st_size,
                        "completed_at": utc_now(),
                    }
                    atomic_json(meta_path, final_meta)
                    with contextlib.suppress(OSError):
                        partial_meta_path.unlink()
                    return DownloadResult(
                        task,
                        "downloaded",
                        str(payload),
                        sha256=digest,
                        etag=final_meta.get("etag"),
                        last_modified=final_meta.get("last_modified"),
                        content_length=payload.stat().st_size,
                        body_bytes=total_body_bytes,
                        resumed_bytes=total_resumed,
                        retries=attempt,
                        elapsed_seconds=time.monotonic() - started,
                        pressure_events=pressure_events,
                    )
                except urllib.error.HTTPError as exc:
                    if exc.code == 304 and payload.exists():
                        try:
                            self._validate_payload(task.source, payload)
                        except TransferError:
                            with contextlib.suppress(OSError):
                                payload.unlink()
                            meta = {}
                            last_error = "Cached payload failed validation"
                            continue
                        return DownloadResult(
                            task,
                            "cached",
                            str(payload),
                            sha256=meta.get("sha256") or file_sha256(payload),
                            etag=meta.get("etag"),
                            last_modified=meta.get("last_modified"),
                            content_length=payload.stat().st_size,
                            retries=attempt,
                            elapsed_seconds=time.monotonic() - started,
                            cache_hit=True,
                            pressure_events=pressure_events,
                        )
                    if exc.code == 416 and partial.exists():
                        try:
                            self._validate_payload(task.source, partial)
                            os.replace(partial, payload)
                            digest = file_sha256(payload)
                            final_meta = {**partial_meta, "sha256": digest, "content_length": payload.stat().st_size, "completed_at": utc_now()}
                            atomic_json(meta_path, final_meta)
                            return DownloadResult(
                                task,
                                "resumed_complete",
                                str(payload),
                                sha256=digest,
                                etag=final_meta.get("etag"),
                                last_modified=final_meta.get("last_modified"),
                                content_length=payload.stat().st_size,
                                resumed_bytes=payload.stat().st_size,
                                retries=attempt,
                                elapsed_seconds=time.monotonic() - started,
                                pressure_events=pressure_events,
                            )
                        except TransferError:
                            with contextlib.suppress(OSError):
                                partial.unlink()
                    pressure = exc.code in (403, 429) or 500 <= exc.code < 600
                    pressure_events += int(pressure)
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        delay = float(retry_after) if retry_after else None
                    except ValueError:
                        delay = None
                    last_error = f"HTTP {exc.code}: {exc.reason}"
                    if attempt >= self.retries:
                        break
                    self.sleep(delay if delay is not None else (2**attempt + random.random() * 0.25))
                except (urllib.error.URLError, TimeoutError, OSError, TransferError) as exc:
                    pressure = isinstance(exc, TransferError) and exc.pressure
                    pressure_events += int(pressure)
                    last_error = str(exc)
                    if attempt >= self.retries:
                        break
                    self.sleep(2**attempt + random.random() * 0.25)
            return DownloadResult(
                task,
                "fetch_error",
                None,
                body_bytes=total_body_bytes,
                resumed_bytes=total_resumed,
                retries=self.retries,
                elapsed_seconds=time.monotonic() - started,
                pressure_events=pressure_events,
                error=last_error or f"Unable to download {task.source}",
            )


@dataclasses.dataclass
class SchedulerMetrics:
    configured_concurrency: int
    maximum_active: int = 0
    final_window: int = 1
    pressure_events: int = 0
    successes: int = 0


def adaptive_download(
    tasks: Iterable[DownloadTask],
    downloader: StreamingDownloader,
    *,
    max_concurrency: int,
) -> tuple[list[DownloadResult], SchedulerMetrics]:
    queue: list[tuple[int, str, int, DownloadTask]] = []
    serial = 0
    seen: set[tuple[str, str]] = set()
    for task in tasks:
        key = (task.tdoc, normalized_url(task.source) if urllib.parse.urlparse(task.source).scheme else str(Path(task.source).resolve()))
        if key in seen:
            continue
        seen.add(key)
        heapq.heappush(queue, (task.priority, task.tdoc, serial, task))
        serial += 1
    window = max(1, max_concurrency)
    metrics = SchedulerMetrics(configured_concurrency=max_concurrency, final_window=window)
    results: list[DownloadResult] = []
    success_streak = 0
    with ThreadPoolExecutor(max_workers=max_concurrency, thread_name_prefix="3gpp-download") as executor:
        active: dict[Future[DownloadResult], DownloadTask] = {}
        while queue or active:
            while queue and len(active) < window:
                _, _, _, task = heapq.heappop(queue)
                active[executor.submit(downloader.fetch, task)] = task
                metrics.maximum_active = max(metrics.maximum_active, len(active))
            if not active:
                break
            completed, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in completed:
                active.pop(future)
                result = future.result()
                results.append(result)
                metrics.pressure_events += result.pressure_events
                if result.pressure_events or result.state == "fetch_error":
                    window = max(1, window // 2)
                    success_streak = 0
                else:
                    metrics.successes += 1
                    success_streak += 1
                    if success_streak >= 8 and window < max_concurrency:
                        window += 1
                        success_streak = 0
                metrics.final_window = window
    results.sort(key=lambda result: (result.task.priority, result.task.tdoc, result.task.source))
    return results, metrics


def adaptive_pipeline(
    tasks: Iterable[DownloadTask],
    downloader: StreamingDownloader,
    parser: Callable[[DownloadResult], Any],
    *,
    max_concurrency: int,
    parse_workers: int,
    on_complete: Callable[[DownloadResult, Any], None] | None = None,
) -> tuple[list[tuple[DownloadResult, Any]], SchedulerMetrics]:
    queue: list[tuple[int, str, int, DownloadTask]] = []
    serial = 0
    seen: set[tuple[str, str]] = set()
    for task in tasks:
        key = (task.tdoc, normalized_url(task.source) if urllib.parse.urlparse(task.source).scheme else str(Path(task.source).resolve()))
        if key in seen:
            continue
        seen.add(key)
        heapq.heappush(queue, (task.priority, task.tdoc, serial, task))
        serial += 1
    window = max(1, max_concurrency)
    metrics = SchedulerMetrics(configured_concurrency=max_concurrency, final_window=window)
    completed_pairs: list[tuple[DownloadResult, Any]] = []
    success_streak = 0
    with (
        ThreadPoolExecutor(max_workers=max_concurrency, thread_name_prefix="3gpp-download") as download_executor,
        ThreadPoolExecutor(max_workers=parse_workers, thread_name_prefix="3gpp-parse") as parse_executor,
    ):
        active_downloads: dict[Future[DownloadResult], DownloadTask] = {}
        active_parses: dict[Future[Any], DownloadResult] = {}
        while queue or active_downloads or active_parses:
            while queue and len(active_downloads) < window:
                _, _, _, task = heapq.heappop(queue)
                active_downloads[download_executor.submit(downloader.fetch, task)] = task
                metrics.maximum_active = max(metrics.maximum_active, len(active_downloads))
            futures = list(active_downloads) + list(active_parses)
            if not futures:
                break
            finished, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in finished:
                if future in active_downloads:
                    active_downloads.pop(future)
                    result = future.result()
                    metrics.pressure_events += result.pressure_events
                    if result.pressure_events or result.state == "fetch_error":
                        window = max(1, window // 2)
                        success_streak = 0
                    else:
                        metrics.successes += 1
                        success_streak += 1
                        if success_streak >= 8 and window < max_concurrency:
                            window += 1
                            success_streak = 0
                    metrics.final_window = window
                    if result.path and result.state != "fetch_error":
                        active_parses[parse_executor.submit(parser, result)] = result
                    else:
                        completed_pairs.append((result, None))
                        if on_complete:
                            on_complete(result, None)
                else:
                    result = active_parses.pop(future)
                    parsed = future.result()
                    completed_pairs.append((result, parsed))
                    if on_complete:
                        on_complete(result, parsed)
    completed_pairs.sort(key=lambda pair: (pair[0].task.priority, pair[0].task.tdoc, pair[0].task.source))
    return completed_pairs, metrics
