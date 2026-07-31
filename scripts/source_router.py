#!/usr/bin/env python3
"""Route canonical 3GPP sources through an optional private file mirror."""

from __future__ import annotations

import dataclasses
import os
import re
import threading
import urllib.error
import urllib.parse
from pathlib import Path, PurePosixPath

DEFAULT_MIRROR_ROOT = "file://3gpp.db.huawei.com/3GPP-Mirror/"
MIRROR_ENV = "THREEGPP_MIRROR_ROOT"
PUBLIC_3GPP_HOSTS = {"3gpp.org", "www.3gpp.org"}


class SourceRoutingError(ValueError):
    pass


def is_public_3gpp_url(value: str) -> bool:
    parsed = urllib.parse.urlsplit(value)
    return parsed.scheme.casefold() in ("http", "https") and (parsed.hostname or "").casefold() in PUBLIC_3GPP_HOSTS


def is_file_uri(value: str) -> bool:
    return urllib.parse.urlsplit(value).scheme.casefold() == "file"


def file_uri_to_path(value: str, *, windows: bool | None = None) -> Path:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.casefold() != "file":
        raise SourceRoutingError(f"Not a file URI: {value}")
    if parsed.query or parsed.fragment:
        raise SourceRoutingError("File URI queries and fragments are not supported")
    decoded = urllib.parse.unquote(parsed.path)
    use_windows = os.name == "nt" if windows is None else windows
    host = parsed.netloc
    if use_windows:
        if host and host.casefold() != "localhost":
            return Path("\\\\" + host + decoded.replace("/", "\\"))
        if decoded.startswith("/") and len(decoded) >= 3 and decoded[2] == ":":
            decoded = decoded[1:]
        return Path(decoded.replace("/", "\\"))
    if host and host.casefold() != "localhost":
        return Path("//" + host + decoded)
    return Path(decoded)


def local_source_path(value: str) -> Path:
    return file_uri_to_path(value) if is_file_uri(value) else Path(value)


def _safe_relative_path(value: str) -> str:
    decoded = urllib.parse.unquote(value).replace("\\", "/").lstrip("/")
    parts = PurePosixPath(decoded).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise SourceRoutingError(f"Unsafe mirror-relative path: {value}")
    return "/".join(parts)


def join_mirror_source(root: str, relative: str) -> str:
    safe_relative = _safe_relative_path(relative)
    if re.match(r"^[A-Za-z]:[\\/]", root) or root.startswith("\\\\"):
        return str(Path(root).expanduser().joinpath(*PurePosixPath(safe_relative).parts))
    parsed = urllib.parse.urlsplit(root)
    if parsed.scheme.casefold() == "file":
        base_path = parsed.path.rstrip("/") + "/"
        quoted = urllib.parse.quote(safe_relative, safe="/:@")
        return urllib.parse.urlunsplit(("file", parsed.netloc, base_path + quoted, "", ""))
    if parsed.scheme:
        raise SourceRoutingError("Mirror root must be a file URI or local directory")
    return str(Path(root).expanduser().joinpath(*PurePosixPath(safe_relative).parts))


def mirror_relatives(public_url: str) -> list[str]:
    if not is_public_3gpp_url(public_url):
        return []
    relative = _safe_relative_path(urllib.parse.urlsplit(public_url).path)
    result = [relative]
    if relative.casefold().startswith("ftp/"):
        result.append(relative[4:])
    return list(dict.fromkeys(result))


def source_kind(value: str, mirror_root: str | None = None) -> str:
    if is_public_3gpp_url(value):
        return "public"
    if is_file_uri(value):
        if mirror_root:
            candidates = [join_mirror_source(mirror_root, relative) for relative in ("ftp", "dynareport")]
            normalized = value.rstrip("/").casefold()
            if any(normalized.startswith(candidate.rstrip("/").casefold()) for candidate in candidates):
                return "private_mirror"
        return "file_uri"
    if urllib.parse.urlsplit(value).scheme.casefold() in ("http", "https"):
        return "remote"
    return "local"


def host_level_failure(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (401, 403, 407, 429) or 500 <= exc.code < 600
    return isinstance(exc, (urllib.error.URLError, TimeoutError, OSError))


def safe_error(candidate: "SourceCandidate", exc: BaseException | str) -> str:
    if candidate.kind != "private_mirror":
        return str(exc)
    if isinstance(exc, PermissionError):
        return "<private-mirror>: access denied"
    if isinstance(exc, FileNotFoundError):
        return "<private-mirror>: source not found"
    return "<private-mirror>: source unavailable"


@dataclasses.dataclass(frozen=True)
class SourceCandidate:
    requested: str
    effective: str
    kind: str


class SourceRouter:
    def __init__(self, mirror_root: str | None = None, *, mirror_enabled: bool = True) -> None:
        configured = mirror_root if mirror_root is not None else os.environ.get(MIRROR_ENV, DEFAULT_MIRROR_ROOT)
        self.mirror_root = configured.rstrip("/") + "/" if configured and is_file_uri(configured) else configured
        self.mirror_enabled = bool(mirror_enabled and self.mirror_root)
        self.public_unavailable = False
        self._lock = threading.Lock()
        self.public_attempts = 0
        self.mirror_attempts = 0
        self.mirror_hits = 0
        self.stale_cache_hits = 0
        self.fallbacks: list[dict[str, str]] = []

    def candidates(self, source: str) -> list[SourceCandidate]:
        if not is_public_3gpp_url(source) or not self.mirror_enabled:
            return [SourceCandidate(source, source, source_kind(source, self.mirror_root))]
        public = SourceCandidate(source, source, "public")
        mirrors = [
            SourceCandidate(source, join_mirror_source(str(self.mirror_root), relative), "private_mirror")
            for relative in mirror_relatives(source)
        ]
        return mirrors if self.public_unavailable else [public, *mirrors]

    def note_attempt(self, candidate: SourceCandidate) -> None:
        with self._lock:
            if candidate.kind == "public":
                self.public_attempts += 1
            elif candidate.kind == "private_mirror":
                self.mirror_attempts += 1

    def note_failure(self, candidate: SourceCandidate, exc: BaseException) -> None:
        with self._lock:
            if candidate.kind == "public" and host_level_failure(exc):
                self.public_unavailable = True
            self.fallbacks.append(
                {
                    "requested_source": candidate.requested,
                    "effective_source": (
                        "<private-mirror>"
                        if candidate.kind == "private_mirror"
                        else candidate.effective
                    ),
                    "source_kind": candidate.kind,
                    "error": safe_error(candidate, exc),
                }
            )

    def note_success(self, candidate: SourceCandidate) -> None:
        if candidate.kind == "private_mirror":
            with self._lock:
                self.mirror_hits += 1

    def note_stale_cache(self) -> None:
        with self._lock:
            self.stale_cache_hits += 1

    def coverage(self) -> dict[str, object]:
        return {
            "public_attempts": self.public_attempts,
            "mirror_attempts": self.mirror_attempts,
            "mirror_hits": self.mirror_hits,
            "stale_cache_hits": self.stale_cache_hits,
            "public_circuit_open": self.public_unavailable,
            "mirror_enabled": self.mirror_enabled,
        }


def directory_index(path: Path) -> bytes:
    rows: list[str] = []
    for child in sorted(path.iterdir(), key=lambda item: item.name.casefold()):
        suffix = "/" if child.is_dir() else ""
        href = urllib.parse.quote(child.name) + suffix
        rows.append(f'<a href="{href}">{child.name}{suffix}</a>')
    return ("<html><body>" + "\n".join(rows) + "</body></html>").encode("utf-8")


def candidate_local_bytes(candidate: SourceCandidate) -> bytes:
    path = local_source_path(candidate.effective)
    if path.is_dir():
        return directory_index(path)
    return path.read_bytes()
