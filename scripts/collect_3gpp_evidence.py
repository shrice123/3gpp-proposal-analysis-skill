#!/usr/bin/env python3
"""Collect mechanical, auditable evidence for Agent-led 3GPP proposal analysis.

Uses only the Python standard library. It intentionally does not infer company
positions, consensus, technical merit, or substantive adoption.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import difflib
import html
import hashlib
import http.client
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
import xml.etree.ElementTree as ET

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from transfer_runtime import (  # noqa: E402
    CacheManager,
    DownloadResult,
    DownloadTask,
    StreamingDownloader,
    adaptive_pipeline,
    atomic_json,
    default_cache_root,
)
from source_router import (  # noqa: E402
    SourceCandidate,
    SourceRouter,
    candidate_local_bytes,
    file_uri_to_path,
    is_file_uri,
    safe_error,
)

VERSION = "2.2.0"
PARSER_VERSION = "2.2"
USER_AGENT = "Mozilla/5.0 (compatible; 3GPP-evidence-collector/2.2)"
TDoc_RE = re.compile(r"\b([A-Z]\d[-–]?\d{7})\b", re.I)
KI_RE = re.compile(r"\bKI\s*#?\s*(\d+(?:\.\d+)*)\b", re.I)
SV_RE = re.compile(r"\b(?:Solution\s+Variant|SV)\s*#?\s*(\d+(?:\.\d+)*)\b", re.I)
SOLUTION_RE = re.compile(r"\bSolution\s*#?\s*(\d+(?:\.\d+)*)\b", re.I)
RELATION_PATTERNS = [
    ("merged_into", re.compile(r"\b(?:merge(?:d)?|merging)\s+(?:this\s+)?(?:proposal\s+)?into\s+(?P<target>[A-Z]\d[-–]?\d{7})", re.I)),
    ("revision_of", re.compile(r"\b(?:revision|revised\s+version)\s+of\s+(?P<target>[A-Z]\d[-–]?\d{7})", re.I)),
    ("revision_of", re.compile(r"\b(?P<target>[A-Z]\d[-–]?\d{7})\s+(?:was\s+)?revised\s+(?:as|to)\b", re.I)),
    ("input_to", re.compile(r"\b(?:input|contribution)\s+to\s+(?P<target>[A-Z]\d[-–]?\d{7})", re.I)),
    ("responds_to", re.compile(r"\b(?:response|reply|comment(?:s|ing)?)\s+(?:to|on)\s+(?P<target>[A-Z]\d[-–]?\d{7})", re.I)),
]
TOPIC_ALIASES = {
    "ai": ["artificial intelligence", "machine learning", "inference", "training", "agent"],
    "ml": ["machine learning", "artificial intelligence"],
}
TSG_ROOTS = {
    "SA": "https://www.3gpp.org/ftp/tsg_sa/",
    "RAN": "https://www.3gpp.org/ftp/tsg_ran/",
    "CT": "https://www.3gpp.org/ftp/tsg_ct/",
}
GROUP_RE = re.compile(r"\b(SA|RAN|CT)\s*[-_ ]?\s*([1-6])\b", re.I)
MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(html.unescape(href))


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "tr":
            self._row = []
        elif tag.lower() in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag.lower() == "tr" and self._row is not None:
            if any(self._row):
                self.rows.append(self._row)
            self._row = None


class CollectorError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def normalize_tdoc(value: str) -> str:
    compact = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    if len(compact) >= 9:
        return compact[:2] + "-" + compact[2:]
    return value.strip().upper().replace("–", "-")


def normalized_words(value: str) -> list[str]:
    value = value.casefold()
    return [part for part in re.findall(r"[a-z0-9]+(?:\.[0-9]+)?|[\u4e00-\u9fff]+", value) if len(part) > 1 or part.isdigit()]


def query_terms(query: str) -> list[str]:
    terms = normalized_words(query)
    expanded = list(terms)
    for term in terms:
        expanded.extend(normalized_words(" ".join(TOPIC_ALIASES.get(term, []))))
    return list(dict.fromkeys(expanded))


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "item"


def source_path(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    return urllib.parse.unquote(parsed.path) if parsed.scheme in ("http", "https", "file") else value


class Fetcher:
    def __init__(self, router: SourceRouter | None = None) -> None:
        self.router = router or SourceRouter()
        self.checked = 0
        self.failures: list[dict[str, str]] = []
        self.body_bytes = 0
        self.cache_hits: set[str] = set()
        self.last_headers: dict[str, dict[str, str]] = {}
        self.effective_sources: dict[str, dict[str, str]] = {}

    def bytes(
        self,
        source: str,
        referer: str | None = None,
        *,
        extra_headers: dict[str, str] | None = None,
        cached_data: bytes | None = None,
    ) -> bytes:
        self.checked += 1
        errors: list[str] = []
        for candidate in self.router.candidates(source):
            self.router.note_attempt(candidate)
            try:
                body = self._candidate_bytes(
                    candidate,
                    source,
                    referer,
                    extra_headers=extra_headers,
                    cached_data=cached_data,
                )
                self.router.note_success(candidate)
                self.effective_sources[source] = {
                    "requested_source": source,
                    "effective_source": candidate.effective,
                    "source_kind": candidate.kind,
                    "fallback_reason": "; ".join(errors),
                }
                return body
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, CollectorError) as exc:
                self.router.note_failure(candidate, exc)
                errors.append(f"{candidate.kind}: {safe_error(candidate, exc)}")
        error = "; ".join(errors) or "no source candidates"
        self.failures.append({"source": source, "error": error})
        raise CollectorError(f"Unable to access {source}: {error}")

    def _candidate_bytes(
        self,
        candidate: SourceCandidate,
        logical_source: str,
        referer: str | None,
        *,
        extra_headers: dict[str, str] | None,
        cached_data: bytes | None,
    ) -> bytes:
        parsed = urllib.parse.urlparse(candidate.effective)
        if parsed.scheme not in ("http", "https"):
            return candidate_local_bytes(candidate)
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if referer:
            headers["Referer"] = referer
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(candidate.effective, headers=headers)
        partial = b""
        for attempt in range(2):
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    self.last_headers[logical_source] = {
                        key.casefold(): value for key, value in response.headers.items()
                    }
                    body = response.read()
                    self.body_bytes += len(body)
                    return body
            except urllib.error.HTTPError as exc:
                if exc.code == 304 and cached_data is not None:
                    self.cache_hits.add(logical_source)
                    return cached_data
                raise
            except http.client.IncompleteRead as exc:
                partial = exc.partial or partial
                if attempt == 0:
                    continue
            except (urllib.error.URLError, TimeoutError, OSError):
                raise
        if partial:
            raise CollectorError(
                f"Incomplete response from {candidate.effective}: received {len(partial)} bytes"
            )
        raise CollectorError(f"Incomplete response from {candidate.effective}")

    def text(self, source: str, referer: str | None = None) -> str:
        raw = self.bytes(source, referer)
        for encoding in ("utf-8-sig", "utf-8", "cp1252"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")


def cached_metadata_bytes(
    fetcher: Fetcher,
    cache: CacheManager,
    source: str,
    referer: str | None,
    *,
    refresh: bool,
) -> bytes:
    if not cache.enabled:
        return fetcher.bytes(source, referer)
    with cache.lock(source):
        payload_path = cache.payload_path(source)
        metadata = cache.read_meta(source)
        cached_data: bytes | None = None
        headers: dict[str, str] = {}
        if payload_path.exists():
            try:
                cached_data = payload_path.read_bytes()
            except OSError:
                cached_data = None
        if cached_data is not None and not refresh:
            if metadata.get("etag"):
                headers["If-None-Match"] = str(metadata["etag"])
            if metadata.get("last_modified"):
                headers["If-Modified-Since"] = str(metadata["last_modified"])
        try:
            raw = fetcher.bytes(
                source,
                referer,
                extra_headers=headers,
                cached_data=cached_data if headers else None,
            )
        except CollectorError:
            cached_hash = hashlib.sha256(cached_data).hexdigest() if cached_data is not None else None
            cache_valid = (
                cached_data is not None
                and not refresh
                and not metadata.get("partial")
                and (
                    not metadata.get("sha256")
                    or metadata.get("sha256") == cached_hash
                )
            )
            if not cache_valid:
                raise
            fetcher.cache_hits.add(source)
            fetcher.router.note_stale_cache()
            fetcher.effective_sources[source] = {
                "requested_source": source,
                "effective_source": str(payload_path),
                "source_kind": "stale_cache",
                "fallback_reason": "Public and private-mirror sources were unavailable.",
            }
            return cached_data
        if source in fetcher.cache_hits:
            if metadata.get("partial"):
                fetcher.failures.append({"source": source, "error": "cached metadata is from an incomplete response"})
            return raw
        response_headers = fetcher.last_headers.get(source, {})
        payload_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = payload_path.with_name(payload_path.name + f".tmp-{os.getpid()}")
        temporary.write_bytes(raw)
        os.replace(temporary, payload_path)
        partial = any(
            failure.get("source") == source and "incomplete response" in failure.get("error", "")
            for failure in fetcher.failures
        )
        atomic_json(
            cache.meta_path(source),
            {
                "url": source,
                **fetcher.effective_sources.get(source, {}),
                "etag": response_headers.get("etag"),
                "last_modified": response_headers.get("last-modified"),
                "content_length": response_headers.get("content-length"),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "fetched_at": utc_now(),
                "parser_version": PARSER_VERSION,
                "partial": partial,
            },
        )
        return raw


def decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def links_from_text(text: str, url: str) -> list[str]:
    parser = LinkParser()
    parser.feed(text)
    result: list[str] = []
    for href in parser.links:
        if href.startswith(("#", "?", "mailto:")):
            continue
        absolute = urllib.parse.urljoin(url, href)
        if urllib.parse.urlparse(absolute).netloc == urllib.parse.urlparse(url).netloc:
            result.append(absolute)
    return list(dict.fromkeys(result))


def list_links(fetcher: Fetcher, url: str) -> list[str]:
    return links_from_text(fetcher.text(url, urllib.parse.urljoin(url, "./")), url)


def cached_text(
    fetcher: Fetcher,
    cache: CacheManager | None,
    url: str,
    *,
    refresh: bool,
) -> str:
    if cache is None:
        return fetcher.text(url, urllib.parse.urljoin(url, "./"))
    return decode_text(
        cached_metadata_bytes(
            fetcher,
            cache,
            url,
            urllib.parse.urljoin(url, "./"),
            refresh=refresh,
        )
    )


def cached_links(
    fetcher: Fetcher,
    cache: CacheManager | None,
    url: str,
    *,
    refresh: bool,
) -> list[str]:
    return links_from_text(cached_text(fetcher, cache, url, refresh=refresh), url)


def group_short_code(group: str) -> str:
    if group.startswith("SA"):
        return f"S{group[2:]}"
    if group.startswith("RAN"):
        return f"R{group[3:]}"
    return f"C{group[2:]}"


def parse_month_hint(hint: str) -> tuple[int | None, int | None]:
    chinese = re.search(r"\b(20\d{2})\s*年\s*(1[0-2]|0?[1-9])\s*月", hint)
    if chinese:
        return int(chinese.group(1)), int(chinese.group(2))
    numeric = re.search(r"\b(20\d{2})[-/.](1[0-2]|0?[1-9])\b", hint)
    if numeric:
        return int(numeric.group(1)), int(numeric.group(2))
    english = re.search(
        r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?\s+(20\d{2})\b",
        hint,
        re.I,
    )
    if english:
        return int(english.group(2)), MONTHS[english.group(1).casefold()]
    english_reverse = re.search(
        r"\b(20\d{2})\s+(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?\b",
        hint,
        re.I,
    )
    if english_reverse:
        return int(english_reverse.group(1)), MONTHS[english_reverse.group(2).casefold()]
    return None, None


def parse_meeting_descriptor(hint: str) -> dict[str, Any]:
    normalized = " ".join(hint.replace("_", "-").split())
    group_match = GROUP_RE.search(normalized)
    group = f"{group_match.group(1).upper()}{group_match.group(2)}" if group_match else None
    number = None
    suffix = ""
    if group_match:
        remainder = normalized[group_match.end() :]
        number_match = re.match(
            r"\s*(?:#|meeting\s*#?|会议\s*#?)?\s*(\d{2,3})(?!\d)(?:[-_ ]?([A-Za-z][A-Za-z0-9-]*))?",
            remainder,
            re.I,
        )
        if number_match:
            number = number_match.group(1)
            suffix = (number_match.group(2) or "").strip("-").casefold()
    year, month = parse_month_hint(normalized)
    return {
        "group": group,
        "tsg": re.match(r"SA|RAN|CT", group).group(0) if group else None,
        "number": number,
        "suffix": suffix,
        "year": year,
        "month": month,
        "normalized": normalized,
    }


def parse_meeting_hint(hint: str) -> tuple[str | None, str | None, str]:
    descriptor = parse_meeting_descriptor(hint)
    return descriptor["group"], descriptor["number"], descriptor["suffix"]


def discover_group_root(
    fetcher: Fetcher,
    cache: CacheManager | None,
    group: str,
    *,
    refresh: bool,
) -> tuple[str | None, list[str]]:
    match = re.fullmatch(r"(SA|RAN|CT)([1-6])", group)
    if not match:
        return None, []
    parent = TSG_ROOTS[match.group(1)]
    directory_pattern = re.compile(rf"^WG{match.group(2)}(?:_|$)", re.I)
    roots = []
    for link in cached_links(fetcher, cache, parent, refresh=refresh):
        leaf = urllib.parse.unquote(urllib.parse.urlparse(link).path.rstrip("/").split("/")[-1])
        if directory_pattern.match(leaf):
            roots.append(link.rstrip("/") + "/")
    roots = sorted(set(roots))
    return (roots[0] if len(roots) == 1 else None), roots


def calendar_matches(
    fetcher: Fetcher,
    cache: CacheManager | None,
    group: str,
    year: int,
    month: int,
    *,
    refresh: bool,
) -> tuple[str, list[dict[str, Any]]]:
    short = group_short_code(group)
    calendar_url = f"https://www.3gpp.org/dynareport/Meetings-{short}.htm"
    parser = TableParser()
    parser.feed(cached_text(fetcher, cache, calendar_url, refresh=refresh))
    result: list[dict[str, Any]] = []
    code_re = re.compile(rf"\b{re.escape(short)}[-\s]?(\d+)(?:[-\s]?([A-Za-z][A-Za-z0-9-]*))?\b", re.I)
    date_re = re.compile(r"\b(20\d{2})[-\u2010-\u2015](\d{2})[-\u2010-\u2015](\d{2})\b")
    for row in parser.rows:
        text = " ".join(row)
        code = code_re.search(text)
        dates = date_re.findall(text)
        if not code or not dates:
            continue
        start_year, start_month, start_day = (int(value) for value in dates[0])
        if (start_year, start_month) != (year, month):
            continue
        result.append(
            {
                "meeting": f"{group}#{code.group(1)}" + (f"-{code.group(2)}" if code.group(2) else ""),
                "number": code.group(1),
                "suffix": (code.group(2) or "").casefold(),
                "start_date": f"{start_year:04d}-{start_month:02d}-{start_day:02d}",
                "end_date": "-".join(dates[1]) if len(dates) > 1 else None,
                "meeting_kind": "subgroup" if re.search(r"\b(?:AHG|SWG)\b", text, re.I) else "working_group",
                "source": calendar_url,
            }
        )
    unique = {(item["number"], item["suffix"], item["start_date"]): item for item in result}
    return calendar_url, sorted(unique.values(), key=lambda item: (int(item["number"]), item["suffix"]))


def meeting_directory_candidates(
    links: list[str],
    group: str,
    numbers: set[str],
    suffix: str,
) -> list[dict[str, Any]]:
    short = group_short_code(group).casefold()
    group_token = group.casefold()
    candidates = []
    for link in links:
        name = urllib.parse.unquote(urllib.parse.urlparse(link).path.rstrip("/").split("/")[-1])
        folded = name.casefold()
        compact = re.sub(r"[^a-z0-9]", "", folded)
        matched_number = next(
            (number for number in numbers if re.search(rf"(?<!\d){re.escape(number)}(?!\d)", folded)),
            None,
        )
        if not matched_number:
            continue
        if short not in compact and group_token not in compact:
            continue
        score = 5
        matched_fields = ["group", "meeting_number"]
        if suffix:
            suffix_tokens = [token for token in re.split(r"[-_\s]+", suffix) if token]
            if not all(token.casefold() in folded for token in suffix_tokens):
                continue
            score += 3
            matched_fields.append("suffix")
        candidates.append(
            {
                "name": name,
                "url": link.rstrip("/") + "/",
                "score": score,
                "matched_fields": matched_fields,
                "number": matched_number,
            }
        )
    return sorted(candidates, key=lambda item: (-item["score"], item["name"].casefold()))


def resolve_meeting(
    fetcher: Fetcher,
    hint: str,
    cache: CacheManager | None = None,
    *,
    refresh: bool = False,
) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(hint)
    if parsed.scheme in ("http", "https"):
        resolved = hint.rstrip("/") + "/"
        return {
            "input": hint,
            "normalized_group": None,
            "tsg": None,
            "meeting_hint": None,
            "date_hint": None,
            "status": "resolved",
            "selected_url": resolved,
            "resolved": resolved,
            "kind": "url",
            "confidence": "explicit",
            "candidates": [],
        }
    if is_file_uri(hint):
        local = file_uri_to_path(hint)
        if local.exists():
            resolved = hint.rstrip("/") + ("/" if local.is_dir() else "")
            return {
                "input": hint,
                "normalized_group": None,
                "tsg": None,
                "meeting_hint": None,
                "date_hint": None,
                "status": "resolved",
                "selected_url": resolved,
                "resolved": resolved,
                "kind": "file_uri",
                "confidence": "explicit",
                "candidates": [],
            }
        return {
            "input": hint,
            "normalized_group": None,
            "tsg": None,
            "meeting_hint": None,
            "date_hint": None,
            "status": "unresolved",
            "selected_url": None,
            "resolved": None,
            "kind": "file_uri",
            "confidence": "unresolved",
            "candidates": [],
            "warning": "The file URI does not resolve to an accessible file or directory.",
        }
    local = Path(hint)
    if local.exists():
        resolved = str(local.resolve())
        return {
            "input": hint,
            "normalized_group": None,
            "tsg": None,
            "meeting_hint": None,
            "date_hint": None,
            "status": "resolved",
            "selected_url": resolved,
            "resolved": resolved,
            "kind": "local",
            "confidence": "explicit",
            "candidates": [],
        }
    descriptor = parse_meeting_descriptor(hint)
    group = descriptor["group"]
    number = descriptor["number"]
    suffix = descriptor["suffix"]
    year = descriptor["year"]
    month = descriptor["month"]
    base = {
        "input": hint,
        "normalized_group": group,
        "tsg": descriptor["tsg"],
        "meeting_hint": f"{group}#{number}" + (f"-{suffix}" if suffix else "") if group and number else None,
        "date_hint": f"{year:04d}-{month:02d}" if year and month else None,
        "selected_url": None,
        "resolved": None,
        "kind": "name",
        "candidates": [],
    }
    if not group:
        return {
            **base,
            "status": "unresolved",
            "confidence": "unresolved",
            "warning": "Include an SA1-SA6, RAN1-RAN6, or CT1-CT6 working-group code, or supply an official URL or local directory.",
        }
    calendar_rows: list[dict[str, Any]] = []
    if year and month:
        try:
            calendar_url, all_calendar_rows = calendar_matches(fetcher, cache, group, year, month, refresh=refresh)
        except CollectorError as exc:
            return {**base, "status": "unresolved", "confidence": "unresolved", "warning": str(exc)}
        calendar_rows = [item for item in all_calendar_rows if item["meeting_kind"] == "working_group"]
        base["excluded_calendar_matches"] = [
            item for item in all_calendar_rows if item["meeting_kind"] != "working_group"
        ]
        if number and not any(item["number"] == number for item in calendar_rows):
            return {
                **base,
                "status": "ambiguous",
                "confidence": "candidate",
                "calendar_source": calendar_url,
                "calendar_matches": calendar_rows,
                "warning": f"{group}#{number} is not listed in the official {year:04d}-{month:02d} meeting calendar.",
            }
        if not number:
            if not calendar_rows:
                return {
                    **base,
                    "status": "unresolved",
                    "confidence": "unresolved",
                    "calendar_source": calendar_url,
                    "warning": f"No {group} meeting is listed in the official calendar for {year:04d}-{month:02d}.",
                }
            if len(calendar_rows) > 1:
                return {
                    **base,
                    "status": "ambiguous",
                    "confidence": "candidate",
                    "calendar_source": calendar_url,
                    "calendar_matches": calendar_rows,
                    "candidates": [
                        {
                            "name": item["meeting"],
                            "url": None,
                            "score": 5,
                            "matched_fields": ["group", "official_calendar_date"],
                            "number": item["number"],
                            "start_date": item["start_date"],
                            "end_date": item["end_date"],
                        }
                        for item in calendar_rows[:10]
                    ],
                    "warning": "Multiple official meetings match this working group and month; choose an exact meeting number or URL.",
                }
            numbers = {item["number"] for item in calendar_rows}
        else:
            numbers = {number}
    elif number:
        numbers = {number}
    else:
        return {
            **base,
            "status": "unresolved",
            "confidence": "unresolved",
            "warning": "Include a meeting number or a year and month, or supply an official URL or local directory.",
        }
    try:
        root, roots = discover_group_root(fetcher, cache, group, refresh=refresh)
    except CollectorError as exc:
        return {**base, "status": "unresolved", "confidence": "unresolved", "warning": str(exc)}
    if not root:
        return {
            **base,
            "status": "ambiguous" if roots else "unresolved",
            "confidence": "candidate" if roots else "unresolved",
            "candidates": [{"name": source_path(item).rstrip("/").split("/")[-1], "url": item, "score": 0, "matched_fields": ["group"]} for item in roots],
            "warning": f"Expected one official directory for {group}; found {len(roots)}.",
        }
    try:
        candidates = meeting_directory_candidates(
            cached_links(fetcher, cache, root, refresh=refresh),
            group,
            numbers,
            suffix,
        )
    except CollectorError as exc:
        return {**base, "status": "unresolved", "confidence": "unresolved", "group_root": root, "warning": str(exc)}
    if not candidates:
        return {
            **base,
            "status": "unresolved",
            "confidence": "unresolved",
            "group_root": root,
            "calendar_matches": calendar_rows,
            "warning": f"No meeting directory matched under {root}",
        }
    best_score = candidates[0]["score"]
    tied = [item for item in candidates if item["score"] == best_score]
    if len(tied) != 1:
        return {
            **base,
            "status": "ambiguous",
            "confidence": "candidate",
            "group_root": root,
            "calendar_matches": calendar_rows,
            "candidates": tied[:10],
            "warning": "Multiple official meeting directories match; choose one exact meeting or URL.",
        }
    selected = tied[0]["url"]
    return {
        **base,
        "status": "resolved",
        "selected_url": selected,
        "resolved": selected,
        "kind": "url",
        "confidence": "high",
        "group_root": root,
        "calendar_matches": calendar_rows,
        "candidates": tied,
    }


def crawl_source(
    fetcher: Fetcher,
    resolved: dict[str, Any],
    max_depth: int = 2,
    include_document_directories: bool = True,
) -> list[str]:
    source = resolved.get("resolved")
    if not source:
        return []
    if resolved["kind"] in ("local", "file_uri"):
        root = file_uri_to_path(source) if resolved["kind"] == "file_uri" else Path(source)
        return [str(path) for path in root.rglob("*") if path.is_file()]
    files: list[str] = []
    seen: set[str] = set()

    def walk(url: str, depth: int) -> None:
        if url in seen or depth > max_depth or len(seen) > 250:
            return
        seen.add(url)
        try:
            links = list_links(fetcher, url)
        except CollectorError:
            return
        base_path = urllib.parse.urlparse(source).path.rstrip("/") + "/"
        for link in links:
            path = urllib.parse.urlparse(link).path
            if not path.startswith(base_path):
                continue
            if link.rstrip("/") == url.rstrip("/") or "/../" in link:
                continue
            leaf = urllib.parse.unquote(path.rstrip("/").split("/")[-1])
            if path.endswith("/") or not Path(leaf).suffix:
                if leaf.casefold() in ("inbox", "_older"):
                    continue
                if not include_document_directories and leaf.casefold() == "docs":
                    continue
                walk(link.rstrip("/") + "/", depth + 1)
            else:
                files.append(link)

    walk(source, 0)
    return list(dict.fromkeys(files))


def xml_text(element: ET.Element) -> str:
    return "".join(text for text in element.itertext() if text)


def text_hash(value: str) -> str:
    normalized = " ".join(value.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def evidence_item(locator: str, text: str, *, change_state: str = "current", section: str = "") -> dict[str, Any]:
    return {
        "locator": locator,
        "text": text,
        "change_state": change_state,
        "section": section,
        "paragraph_hash": text_hash(text),
    }


def _docx_text_by_state(element: ET.Element, state: str = "current") -> tuple[list[str], list[str], bool]:
    current: list[str] = []
    deleted: list[str] = []
    inserted = False

    def walk(node: ET.Element, inherited: str) -> None:
        nonlocal inserted
        tag = node.tag.rsplit("}", 1)[-1]
        active = inherited
        if tag == "del":
            active = "deleted"
        elif tag == "ins":
            active = "inserted"
            inserted = True
        if tag in ("t", "delText") and node.text:
            (deleted if active == "deleted" else current).append(node.text)
        for child in node:
            walk(child, active)

    walk(element, state)
    return current, deleted, inserted


def extract_docx(data: bytes) -> list[dict[str, Any]]:
    evidence = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
        table_rows = [node for node in root.iter() if node.tag.endswith("}tr")]
        for row_index, row in enumerate(table_rows, 1):
            cells = []
            for cell in (node for node in row if node.tag.endswith("}tc")):
                cells.append(" ".join(xml_text(cell).split()))
            if any(cells):
                evidence.append(evidence_item(f"table-row:{row_index}", " | ".join(cells)))
        paragraphs = [node for node in root.iter() if node.tag.endswith("}p")]
        section = ""
        for index, paragraph in enumerate(paragraphs, 1):
            current, deleted, inserted = _docx_text_by_state(paragraph)
            text = " ".join("".join(current).split())
            if text:
                style = next(
                    (
                        value
                        for node in paragraph.iter()
                        if node.tag.endswith("}pStyle")
                        for key, value in node.attrib.items()
                        if key.endswith("}val")
                    ),
                    "",
                )
                if style.casefold().startswith("heading") or (len(text) < 160 and re.match(r"^\d+(?:\.\d+)*\s+\S", text)):
                    section = text
                evidence.append(
                    evidence_item(
                        f"paragraph:{index}",
                        text,
                        change_state="inserted" if inserted else "current",
                        section=section,
                    )
                )
            deleted_text = " ".join("".join(deleted).split())
            if deleted_text:
                evidence.append(evidence_item(f"paragraph:{index}/deleted", deleted_text, change_state="deleted", section=section))
    return evidence


def extract_pptx(data: bytes) -> list[dict[str, Any]]:
    evidence = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        slides = sorted(name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name))
        for slide_index, name in enumerate(slides, 1):
            root = ET.fromstring(archive.read(name))
            text = " ".join(xml_text(root).split())
            if text:
                evidence.append(evidence_item(f"slide:{slide_index}", text))
    return evidence


def xlsx_rows(data: bytes) -> list[tuple[str, int, list[str]]]:
    rows: list[tuple[str, int, list[str]]] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(item.itertext()) for item in root if item.tag.endswith("}si")]
        sheets = sorted(name for name in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name))
        for sheet_no, name in enumerate(sheets, 1):
            root = ET.fromstring(archive.read(name))
            for row_no, row in enumerate((node for node in root.iter() if node.tag.endswith("}row")), 1):
                values: list[str] = []
                for cell in (node for node in row if node.tag.endswith("}c")):
                    cell_type = cell.attrib.get("t")
                    raw = next((node.text or "" for node in cell if node.tag.endswith("}v")), "")
                    if cell_type == "s" and raw.isdigit() and int(raw) < len(shared):
                        value = shared[int(raw)]
                    elif cell_type == "inlineStr":
                        value = "".join(cell.itertext())
                    else:
                        value = raw
                    values.append(" ".join(value.split()))
                if any(values):
                    rows.append((f"sheet:{sheet_no}", row_no, values))
    return rows


def extract_xlsx(data: bytes) -> list[dict[str, Any]]:
    return [evidence_item(f"{sheet}/row:{row}", " | ".join(values)) for sheet, row, values in xlsx_rows(data)]


def extract_csv(data: bytes) -> list[dict[str, Any]]:
    text = data.decode("utf-8-sig", errors="replace")
    return [evidence_item(f"row:{index}", " | ".join(row)) for index, row in enumerate(csv.reader(io.StringIO(text)), 1) if row]


def extract_html(data: bytes) -> list[dict[str, Any]]:
    text = ""
    encodings = ("utf-16", "utf-8-sig", "cp1252") if data.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "cp1252", "utf-16")
    for encoding in encodings:
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        text = data.decode("utf-8", errors="replace")
    parser = TableParser()
    parser.feed(text)
    return [evidence_item(f"html-row:{index}", " | ".join(row)) for index, row in enumerate(parser.rows, 1)]


def extract_supported(name: str, data: bytes) -> tuple[list[dict[str, Any]], str]:
    suffix = Path(source_path(name)).suffix.casefold()
    try:
        if suffix == ".docx":
            return extract_docx(data), "parsed"
        if suffix == ".pptx":
            return extract_pptx(data), "parsed"
        if suffix == ".xlsx":
            return extract_xlsx(data), "parsed"
        if suffix in (".htm", ".html"):
            return extract_html(data), "parsed"
        if suffix in (".csv", ".txt", ".md"):
            return extract_csv(data) if suffix == ".csv" else [evidence_item("text:1", data.decode("utf-8-sig", errors="replace"))], "parsed"
    except (zipfile.BadZipFile, KeyError, ET.ParseError, UnicodeError) as exc:
        return [], f"parse_error:{exc}"
    return [], "unsupported"


def extract_archive(name: str, data: bytes) -> list[tuple[str, bytes]]:
    if Path(source_path(name)).suffix.casefold() != ".zip":
        return [(Path(source_path(name)).name, data)]
    result = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                normalized = Path(member.filename.replace("\\", "/"))
                if normalized.is_absolute() or ".." in normalized.parts:
                    continue
                result.append((str(normalized), archive.read(member)))
    except zipfile.BadZipFile:
        return [(Path(source_path(name)).name, data)]
    return result


def safe_archive_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    result = []
    for member in archive.infolist():
        if member.is_dir():
            continue
        normalized = Path(member.filename.replace("\\", "/"))
        if normalized.is_absolute() or ".." in normalized.parts:
            continue
        result.append(member)
    return result


def extract_docx_incremental(data: bytes) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive, archive.open("word/document.xml") as document:
        paragraph_index = 0
        section = ""
        for _, element in ET.iterparse(document, events=("end",)):
            if not element.tag.endswith("}p"):
                continue
            paragraph_index += 1
            current, deleted, inserted = _docx_text_by_state(element)
            text = " ".join("".join(current).split())
            if text:
                style = next(
                    (
                        value
                        for node in element.iter()
                        if node.tag.endswith("}pStyle")
                        for key, value in node.attrib.items()
                        if key.endswith("}val")
                    ),
                    "",
                )
                if style.casefold().startswith("heading") or (len(text) < 160 and re.match(r"^\d+(?:\.\d+)*\s+\S", text)):
                    section = text
                evidence.append(
                    evidence_item(
                        f"paragraph:{paragraph_index}",
                        text,
                        change_state="inserted" if inserted else "current",
                        section=section,
                    )
                )
            deleted_text = " ".join("".join(deleted).split())
            if deleted_text:
                evidence.append(
                    evidence_item(
                        f"paragraph:{paragraph_index}/deleted",
                        deleted_text,
                        change_state="deleted",
                        section=section,
                    )
                )
            element.clear()
    return evidence


def extract_supported_incremental(name: str, data: bytes) -> tuple[list[dict[str, Any]], str]:
    if Path(source_path(name)).suffix.casefold() == ".docx":
        try:
            return extract_docx_incremental(data), "parsed"
        except (zipfile.BadZipFile, KeyError, ET.ParseError, UnicodeError) as exc:
            return [], f"parse_error:{exc}"
    return extract_supported(name, data)


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(temporary, path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    try:
        with path.open(encoding="utf-8-sig") as stream:
            for line in stream:
                if line.strip():
                    value = json.loads(line)
                    if isinstance(value, dict):
                        rows.append(value)
    except (OSError, ValueError):
        return []
    return rows


def parse_downloaded_document(
    result: DownloadResult,
    *,
    output: Path,
    terms: list[str],
    cache: CacheManager,
    save_matched: bool,
) -> dict[str, Any]:
    if not result.path or not result.sha256:
        return {"files": [], "evidence": [], "document_index": [], "relationships": [], "unsupported": 0, "parsed": 0}
    cached = cache.load_parsed(result.task.source, result.sha256, PARSER_VERSION)
    path = Path(result.path)
    target_dir = output / "downloads" / safe_name(result.task.tdoc)
    if cached is not None:
        result.parsed_cache_hit = True
        cached = dict(cached)
        cached["files"] = [dict(item) for item in cached.get("files", []) if isinstance(item, dict)]
        if save_matched and path.suffix.casefold() == ".zip":
            target_dir.mkdir(parents=True, exist_ok=True)
            try:
                with zipfile.ZipFile(path) as archive:
                    for member in safe_archive_members(archive):
                        target = target_dir / safe_name(Path(member.filename).name)
                        with archive.open(member) as source_stream, target.open("wb") as target_stream:
                            while True:
                                chunk = source_stream.read(256 * 1024)
                                if not chunk:
                                    break
                                target_stream.write(chunk)
            except zipfile.BadZipFile:
                pass
            for item in cached["files"]:
                name = item.get("name")
                if name:
                    target = target_dir / safe_name(Path(str(name)).name)
                    item["saved_path"] = str(target.relative_to(output)) if target.exists() else None
        elif save_matched:
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / safe_name(path.name)
            with path.open("rb") as source_stream, target.open("wb") as target_stream:
                while True:
                    chunk = source_stream.read(256 * 1024)
                    if not chunk:
                        break
                    target_stream.write(chunk)
            for item in cached["files"]:
                item["saved_path"] = str(target.relative_to(output))
        else:
            for item in cached["files"]:
                item["saved_path"] = None
        return cached

    files: list[dict[str, Any]] = []
    all_evidence: list[dict[str, Any]] = []
    document_index: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    parsed_count = 0
    unsupported = 0

    def process(inner_name: str, inner_data: bytes, saved_path: str | None) -> None:
        nonlocal parsed_count, unsupported
        evidence, state = extract_supported_incremental(inner_name, inner_data)
        parsed_count += int(state == "parsed")
        unsupported += int(state == "unsupported")
        files.append({"name": inner_name, "state": state, "saved_path": saved_path})
        indexed = []
        for item in evidence:
            indexed_item = {
                "tdoc": result.task.tdoc,
                "source": result.task.source,
                "inner_file": inner_name,
                "locator": item["locator"],
                "text": item["text"],
                "paragraph_hash": item.get("paragraph_hash") or text_hash(item["text"]),
                "change_state": item.get("change_state", "current"),
                "section": item.get("section", ""),
                "identifiers": identifiers(item["text"]),
            }
            document_index.append(indexed_item)
            indexed.append(indexed_item)
        selected: set[int] = set()
        key_sections = ("proposal", "discussion", "conclusion", "observation", "summary")
        for index, item in enumerate(indexed):
            text = item["text"].casefold()
            relevant = (
                any(term in text for term in terms)
                or any(pattern.search(item["text"]) for _, pattern in RELATION_PATTERNS)
                or any(key in item.get("section", "").casefold() for key in key_sections)
            )
            if relevant:
                selected.update(range(max(0, index - 1), min(len(indexed), index + 2)))
        all_evidence.extend(indexed[index] for index in sorted(selected))
        relationships.extend(relation_candidates(result.task.tdoc, f"{result.task.source}#{inner_name}", evidence))

    try:
        if path.suffix.casefold() == ".zip":
            with zipfile.ZipFile(path) as archive:
                members = safe_archive_members(archive)
                if save_matched:
                    target_dir.mkdir(parents=True, exist_ok=True)
                for member in members:
                    inner_name = member.filename.replace("\\", "/")
                    inner_data = archive.read(member)
                    saved_path = None
                    if save_matched:
                        target = target_dir / safe_name(Path(inner_name).name)
                        target.write_bytes(inner_data)
                        saved_path = str(target.relative_to(output))
                    process(inner_name, inner_data, saved_path)
        else:
            data = path.read_bytes()
            saved_path = None
            if save_matched:
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / safe_name(path.name)
                target.write_bytes(data)
                saved_path = str(target.relative_to(output))
            process(path.name, data, saved_path)
    except zipfile.BadZipFile:
        files.append({"name": path.name, "state": "unsupported", "saved_path": None})
        unsupported += 1

    value = {
        "files": files,
        "evidence": all_evidence,
        "document_index": document_index,
        "relationships": relationships,
        "unsupported": unsupported,
        "parsed": parsed_count,
    }
    cache.save_parsed(result.task.source, result.sha256, PARSER_VERSION, value)
    return value


def deduplicate_evidence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    first_by_hash: dict[str, str] = {}
    result = []
    for row in rows:
        digest = row.get("paragraph_hash") or text_hash(row.get("text", ""))
        key = f"{row.get('tdoc')}:{row.get('inner_file')}:{row.get('locator')}"
        duplicate_of = first_by_hash.get(digest)
        updated = dict(row)
        updated["paragraph_hash"] = digest
        updated["duplicate_of"] = duplicate_of
        if duplicate_of is None:
            first_by_hash[digest] = key
        result.append(updated)
    return result


def build_diffs(relationships: list[dict[str, Any]], document_index: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_tdoc: dict[str, list[dict[str, Any]]] = {}
    for item in document_index:
        if item.get("change_state") != "deleted":
            by_tdoc.setdefault(item["tdoc"], []).append(item)
    result = []
    seen: set[tuple[str, str, str]] = set()
    for relationship in relationships:
        if relationship.get("classification") == "invalidated" or relationship.get("type") not in ("revision_of", "input_to", "merged_into"):
            continue
        newer = relationship["from"]
        baseline = relationship["to"]
        key = (newer, baseline, relationship["type"])
        if key in seen or newer not in by_tdoc or baseline not in by_tdoc:
            continue
        seen.add(key)
        left = by_tdoc[baseline]
        right = by_tdoc[newer]
        matcher = difflib.SequenceMatcher(
            a=[item["paragraph_hash"] for item in left],
            b=[item["paragraph_hash"] for item in right],
            autojunk=False,
        )
        changes = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            changes.append({
                "operation": tag,
                "baseline": [{"locator": item["locator"], "text": item["text"]} for item in left[i1:i2]],
                "candidate": [{"locator": item["locator"], "text": item["text"]} for item in right[j1:j2]],
            })
        result.append({
            "from": newer,
            "to": baseline,
            "relation_type": relationship["type"],
            "source": relationship.get("source"),
            "status": "available",
            "changes": changes,
        })
    return result


HEADER_MAP = {
    "tdoc": ("td", "tdoc", "tdocno", "tdocnumber", "document", "documentno"),
    "title": ("title", "subject"),
    "source": ("source", "company", "companies"),
    "status": ("status", "result", "conclusion", "treatment"),
    "agenda": ("agenda", "agendaitem", "ai"),
    "comments": ("comments", "comment"),
    "discussion": ("emaildiscussion", "discussion"),
}


def canonical_header(value: str) -> str | None:
    key = re.sub(r"[^a-z0-9]", "", value.casefold())
    for canonical, aliases in HEADER_MAP.items():
        if key in aliases:
            return canonical
    return None


def agenda_records(source: str, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records = []
    current_headers: dict[int, str] = {}
    for item in evidence:
        values = [part.strip() for part in item["text"].split("|")]
        detected = {index: canonical_header(value) for index, value in enumerate(values)}
        detected = {index: value for index, value in detected.items() if value}
        if "tdoc" in detected.values() and len(detected) >= 2:
            current_headers = detected
            continue
        if current_headers:
            mapped = {header: values[index] if index < len(values) else "" for index, header in current_headers.items()}
            match = TDoc_RE.search(mapped.get("tdoc", ""))
            if match:
                status = mapped.get("status", "")
                comments = mapped.get("comments", "")
                discussion = mapped.get("discussion", "")
                title = mapped.get("title", "")
                role_hints = []
                if "baseline" in f"{title} {comments}".casefold():
                    role_hints.append("baseline")
                if re.search(r"\bapproved\b", status, re.I):
                    role_hints.append("approved")
                records.append({
                    "tdoc": normalize_tdoc(match.group(1)),
                    "title": title,
                    "source_company": mapped.get("source", ""),
                    "status": status,
                    "agenda": mapped.get("agenda", ""),
                    "comments": comments,
                    "discussion": discussion,
                    "role_hints": role_hints,
                    "metadata_source": source,
                    "metadata_locator": item["locator"],
                })
    return records


def agenda_relationships(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    relations = []
    record_list = list(records)
    invalid_merge_sources = {
        record["tdoc"]
        for record in record_list
        if re.search(r"\b(?:incorrect|wrong)\b.{0,80}\b(?:merge|assignment|agenda)", f"{record.get('comments', '')} {record.get('discussion', '')}", re.I)
    }

    def add(record: dict[str, Any], source_id: str, target_id: str, relation_type: str, evidence: str) -> None:
        source_id = normalize_tdoc(source_id)
        target_id = normalize_tdoc(target_id)
        if source_id == target_id:
            return
        invalidated = relation_type in ("merged_into", "input_to") and source_id in invalid_merge_sources
        relations.append({
            "from": source_id,
            "to": target_id,
            "type": relation_type,
            "classification": "invalidated" if invalidated else "candidate",
            "confidence": "high",
            "source": record["metadata_source"],
            "locator": record["metadata_locator"],
            "evidence": evidence,
            **({"warning": "Meeting discussion identifies an incorrect agenda/merge assignment."} if invalidated else {}),
        })

    for record in record_list:
        combined = " ".join((record.get("comments", ""), record.get("discussion", ""), record.get("status", ""))).strip()
        merge_match = re.search(r"\bmerge(?:d)?\s+into\s+(?P<clause>.*?)(?:Not\s+Handled|$)", combined, re.I)
        if merge_match:
            for target in TDoc_RE.findall(merge_match.group("clause")):
                add(record, record["tdoc"], target, "merged_into", combined)
        for match in re.finditer(r"\brevision\s+of\s+(?P<target>[A-Z]\d[-–]?\d{7})", combined, re.I):
            add(record, record["tdoc"], match.group("target"), "revision_of", combined)
        for match in re.finditer(r"\brevised.{0,40}\bto\s+(?P<target>[A-Z]\d[-–]?\d{7})", combined, re.I):
            add(record, match.group("target"), record["tdoc"], "revision_of", combined)
        baseline_match = re.search(r"\bbaseline\b.*?\bincl\.?\s+(?P<inputs>.*?)(?:\brevised\b|$)", combined, re.I)
        if baseline_match:
            for source_id in TDoc_RE.findall(baseline_match.group("inputs")):
                add(record, source_id, record["tdoc"], "input_to", combined)
    return relations


def identifiers(text: str) -> dict[str, list[str]]:
    return {
        "tdocs": list(dict.fromkeys(normalize_tdoc(value) for value in TDoc_RE.findall(text))),
        "key_issues": list(dict.fromkeys(KI_RE.findall(text))),
        "solution_variants": list(dict.fromkeys(SV_RE.findall(text))),
        "solutions": list(dict.fromkeys(SOLUTION_RE.findall(text))),
    }


def match_score(
    record: dict[str, Any],
    terms: list[str],
    companies: list[str],
    aliases: dict[str, list[str]],
    required_ids: dict[str, list[str]],
) -> int:
    haystack = " ".join(
        str(record.get(key, ""))
        for key in ("tdoc", "title", "source_company", "status", "agenda", "comments", "discussion", "text")
    ).casefold()
    observed_ids = identifiers(haystack)
    for field in ("tdocs", "key_issues", "solution_variants", "solutions"):
        if required_ids[field] and not set(required_ids[field]).intersection(observed_ids[field]):
            return -1
    structural_terms = {"ki", "key", "issue", "solution", "variant", "sv"} | {
        value.casefold() for field in required_ids.values() for value in field
    }
    semantic_terms = [term for term in terms if term not in structural_terms and not re.fullmatch(r"\d+(?:\.\d+)*", term)]
    if semantic_terms and not any(term in haystack for term in semantic_terms):
        return -1
    score = sum(2 if term in haystack else 0 for term in semantic_terms)
    score += sum(5 for field in required_ids.values() if field)
    for company in companies:
        variants = [company] + aliases.get(company, [])
        if any(variant.casefold() in haystack for variant in variants):
            score += 4
        else:
            return -1
    return score


def relation_candidates(source_id: str, source_name: str, evidence: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for item in evidence:
        text = item["text"]
        for relation_type, pattern in RELATION_PATTERNS:
            for match in pattern.finditer(text):
                target = normalize_tdoc(match.group("target"))
                if target == source_id:
                    continue
                result.append({
                    "from": source_id,
                    "to": target,
                    "type": relation_type,
                    "classification": "candidate",
                    "confidence": "high",
                    "source": source_name,
                    "locator": item["locator"],
                    "evidence": " ".join(text[max(0, match.start() - 100):match.end() + 100].split()),
                })
    return result


def write_json(path: Path, value: Any) -> None:
    atomic_json(path, value)


def load_aliases(path: str | None) -> dict[str, list[str]]:
    if not path:
        return {}
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or any(not isinstance(key, str) or not isinstance(items, list) for key, items in value.items()):
        raise CollectorError("Aliases must be a JSON object mapping company names to arrays.")
    return {key: [str(item) for item in items] for key, items in value.items()}


def likely_metadata_file(source: str) -> bool:
    name = urllib.parse.unquote(urllib.parse.urlparse(source).path).casefold()
    return Path(name).suffix in (".xlsx", ".csv", ".docx", ".htm", ".html", ".zip") and any(
        term in name for term in ("agenda", "status", "report", "list", "tdoc", "index")
    )


def metadata_priority(source: str) -> tuple[int, str]:
    name = source_path(source).casefold().replace("\\", "/")
    if name.endswith("/tdocsbyagenda.htm") or name.endswith("/tdocsbyagenda.html"):
        return (0, name)
    if "/report/" in name and "/_older/" not in name:
        return (1, name)
    if "index" in Path(name).name:
        return (2, name)
    if "/_older/" in name:
        return (4, name)
    return (3, name)


def tdoc_from_name(source: str) -> str | None:
    match = TDoc_RE.search(urllib.parse.unquote(urllib.parse.urlparse(source).path))
    return normalize_tdoc(match.group(1)) if match else None


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def unique_relationships(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = {}
    for relationship in rows:
        key = (
            relationship.get("from"),
            relationship.get("to"),
            relationship.get("type"),
            relationship.get("source"),
            relationship.get("locator"),
        )
        unique[key] = relationship
    return sorted(
        unique.values(),
        key=lambda item: (
            item.get("from", ""),
            item.get("to", ""),
            item.get("type", ""),
            item.get("source", ""),
            item.get("locator", ""),
        ),
    )


def task_priority(tdoc: str, record_by_id: dict[str, dict[str, Any]], matched_ids: set[str]) -> int:
    record = record_by_id.get(tdoc)
    if record:
        hints = set(record.get("role_hints", []))
        if "approved" in hints or "baseline" in hints:
            return 0
    if tdoc in matched_ids:
        return 1
    return 2 if record else 3


def router_from_args(args: argparse.Namespace) -> SourceRouter:
    return SourceRouter(
        getattr(args, "mirror_root", None),
        mirror_enabled=not getattr(args, "no_mirror", False),
    )


def collect(args: argparse.Namespace) -> int:
    run_started = time.monotonic()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    router = router_from_args(args)
    fetcher = Fetcher(router)
    aliases = load_aliases(args.aliases)
    cache_enabled = not args.no_cache and (
        bool(args.cache_dir)
        or any(
            urllib.parse.urlparse(hint).scheme in ("http", "https") or not Path(hint).exists()
            for hint in args.meeting
        )
    )
    cache = CacheManager(Path(args.cache_dir) if args.cache_dir else None, enabled=cache_enabled)
    meetings = [resolve_meeting(fetcher, hint, cache, refresh=args.refresh) for hint in args.meeting]
    resolution_blocked = any(meeting.get("status") != "resolved" for meeting in meetings)
    all_files: list[str] = []
    for meeting in meetings:
        if meeting.get("kind") == "url" and meeting.get("resolved"):
            try:
                root_links = list_links(fetcher, meeting["resolved"])
            except CollectorError:
                root_links = []
            root_files = [
                link
                for link in root_links
                if Path(urllib.parse.urlparse(link).path).suffix
            ]
            if any("tdocsbyagenda.htm" in source_path(link).casefold() for link in root_files):
                all_files.extend(root_files)
                continue
        all_files.extend(crawl_source(fetcher, meeting, include_document_directories=False))
    all_files = list(dict.fromkeys(all_files))
    metadata_sources = [source for source in all_files if likely_metadata_file(source)]
    if not metadata_sources:
        metadata_sources = [source for source in all_files if Path(urllib.parse.urlparse(source).path).suffix.casefold() in (".xlsx", ".csv")]
    metadata_sources.sort(key=metadata_priority)

    records: list[dict[str, Any]] = []
    metadata_manifest: list[dict[str, Any]] = []
    for source in metadata_sources[:30]:
        try:
            raw = cached_metadata_bytes(
                fetcher,
                cache,
                source,
                meetings[0].get("resolved") if meetings else None,
                refresh=args.refresh,
            )
        except CollectorError:
            continue
        for inner_name, inner_data in extract_archive(source, raw):
            evidence, state = extract_supported(inner_name, inner_data)
            metadata_manifest.append(
                {
                    "source": source,
                    **fetcher.effective_sources.get(source, {}),
                    "inner_file": inner_name,
                    "state": state,
                }
            )
            records.extend(agenda_records(source, evidence))
        if "tdocsbyagenda.htm" in source_path(source).casefold() and records:
            break

    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        by_id.setdefault(record["tdoc"], record)
    records = list(by_id.values())
    record_by_id = {record["tdoc"]: record for record in records}
    terms = query_terms(args.query)
    explicit_ids = {normalize_tdoc(value) for value in args.include_tdoc}
    invalid_explicit_ids = sorted(value for value in explicit_ids if not re.fullmatch(r"[A-Z]\d-\d{7}", value))
    if invalid_explicit_ids:
        raise CollectorError(
            "--include-tdoc requires identifiers such as S5-2600123, R1-2600123, or C3-2600123: "
            + ", ".join(invalid_explicit_ids)
        )
    required_ids = identifiers(args.query)
    if explicit_ids:
        required_ids["tdocs"] = sorted(set(required_ids["tdocs"]) | explicit_ids)
    scored = [(match_score(record, terms, args.company, aliases, required_ids), record) for record in records]
    matched = (
        [record for record in records if record["tdoc"] in explicit_ids]
        if explicit_ids
        else [record for score, record in scored if score > 0]
    )
    if not matched and not terms and records:
        matched = records
    matched_ids = {record["tdoc"] for record in matched} | explicit_ids

    file_by_tdoc: dict[str, list[str]] = {}
    for source in all_files:
        tdoc = tdoc_from_name(source)
        if tdoc:
            file_by_tdoc.setdefault(tdoc, []).append(source)

    all_agenda_relationships = agenda_relationships(records)
    related_ids = set(matched_ids)
    relationships = [
        relationship
        for relationship in all_agenda_relationships
        if relationship["from"] in matched_ids or relationship["to"] in matched_ids
    ]
    for relationship in relationships:
        if relationship["classification"] != "invalidated":
            related_ids.update((relationship["from"], relationship["to"]))
    for meeting in meetings:
        if meeting.get("kind") == "url" and meeting.get("resolved"):
            for tdoc in related_ids:
                if tdoc in record_by_id or tdoc in matched_ids:
                    file_by_tdoc.setdefault(
                        tdoc,
                        [urllib.parse.urljoin(meeting["resolved"], f"Docs/{tdoc}.zip")],
                    )

    scoped_records = []
    scoped_record_ids = set()
    for record in records:
        if record["tdoc"] in related_ids:
            scoped = dict(record)
            if record["tdoc"] in explicit_ids:
                scoped["scope_basis"] = "explicit_tdoc"
            else:
                scoped["scope_basis"] = "direct_query" if record["tdoc"] in matched_ids else "explicit_relationship"
            scoped_records.append(scoped)
            scoped_record_ids.add(record["tdoc"])
    for tdoc in sorted(related_ids - scoped_record_ids):
        scoped_records.append({
            "tdoc": tdoc,
            "scope_basis": "explicit_tdoc" if tdoc in explicit_ids else "explicit_relationship",
            "metadata_missing": True,
            "title": "",
            "source_company": "",
            "status": "",
            "agenda": "",
        })
    distributions = {
        "agenda": dict(Counter(record.get("agenda") or "(blank)" for record in scoped_records)),
        "company": dict(Counter(record.get("source_company") or "(blank)" for record in scoped_records)),
        "status": dict(Counter(record.get("status") or "(blank)" for record in scoped_records)),
    }
    warnings = []
    if resolution_blocked:
        warnings.append("Meeting resolution is ambiguous or unresolved; proposal-body collection is blocked.")
    if records and len(matched) == len(records) and args.query:
        warnings.append("The query did not concentrate the agenda records; inspect topic aliases and identifiers.")
    if not records:
        warnings.append("No structured agenda records were parsed; scope may be incomplete.")
    missing_explicit_ids = explicit_ids - set(record_by_id)
    if missing_explicit_ids:
        warnings.append("Some explicitly included TDocs were not present in parsed meeting metadata.")
    if fetcher.failures:
        warnings.append("Some sources could not be accessed; do not claim complete coverage.")

    preview = {
        "schema_version": 2,
        "collector_version": VERSION,
        "generated_at": utc_now(),
        "query": args.query,
        "companies": args.company,
        "included_tdocs": sorted(explicit_ids),
        "meetings": meetings,
        "discovered_file_count": len(all_files),
        "agenda_record_count": len(records),
        "direct_candidate_count": len(matched),
        "candidate_count": len(scoped_records),
        "current_meeting_candidate_count": sum(not record.get("metadata_missing", False) for record in scoped_records),
        "historical_relationship_candidate_count": sum(bool(record.get("metadata_missing", False)) for record in scoped_records),
        "candidates": scoped_records,
        "distributions": distributions,
        "warnings": warnings,
    }
    base_coverage = {
        "schema_version": 2,
        "checked_requests": fetcher.checked,
        "metadata_body_bytes": fetcher.body_bytes,
        "metadata_cache_hits": len(fetcher.cache_hits),
        "discovered_files": len(all_files),
        "metadata_sources_checked": len(metadata_manifest),
        "agenda_records": len(records),
        "candidate_documents": len(scoped_records),
        "current_meeting_candidate_documents": sum(not record.get("metadata_missing", False) for record in scoped_records),
        "historical_relationship_candidate_documents": sum(bool(record.get("metadata_missing", False)) for record in scoped_records),
        "downloaded_archives": 0,
        "parsed_files": 0,
        "unsupported_files": 0,
        "failures": list(fetcher.failures),
        "relationship_expansion_stable": args.command == "collect",
        "completeness": "partial" if warnings or fetcher.failures else "no_known_gaps",
        "total_body_bytes": 0,
        "resumed_bytes": 0,
        "cache_hits": 0,
        "parsed_cache_hits": 0,
        "cache_hit_rate": 0.0,
        "retry_count": 0,
        "maximum_active_downloads": 0,
        "configured_concurrency": getattr(args, "max_concurrency", 0),
        "first_evidence_seconds": None,
        "stage_timings": {"total_seconds": time.monotonic() - run_started},
        "source_routing": router.coverage(),
        "source_fallbacks": list(router.fallbacks),
    }
    write_json(output / "scope_preview.json", preview)
    if args.command == "preview" or resolution_blocked:
        write_json(output / "manifest.json", {"schema_version": 2, "collector_version": VERSION, "metadata": metadata_manifest, "documents": []})
        write_json(output / "relationships.json", {"schema_version": 2, "relationships": unique_relationships(relationships)})
        write_jsonl(output / "evidence.jsonl", [])
        write_jsonl(output / "document_index.jsonl", [])
        write_json(output / "diffs.json", {"schema_version": 2, "diffs": []})
        write_json(output / "coverage.json", base_coverage)
        print(json.dumps({"output": str(output), "candidates": len(scoped_records), "failures": len(fetcher.failures)}, ensure_ascii=False))
        return 0 if not resolution_blocked else 2

    signature_value = {
        "meetings": [meeting.get("resolved") for meeting in meetings],
        "query": args.query,
        "companies": args.company,
        "included_tdocs": sorted(explicit_ids),
    }
    run_signature = hashlib.sha256(json.dumps(signature_value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    prior_manifest = read_json(output / "manifest.json")
    can_resume = prior_manifest.get("schema_version") == 2 and prior_manifest.get("run_signature") == run_signature
    document_by_key: dict[tuple[str, str | None], dict[str, Any]] = {}
    evidence_rows = read_jsonl(output / "evidence.jsonl") if can_resume else []
    document_index = read_jsonl(output / "document_index.jsonl") if can_resume else []
    if can_resume:
        for item in prior_manifest.get("documents", []):
            if isinstance(item, dict):
                document_by_key[(item.get("tdoc", ""), item.get("source"))] = item
        prior_relationships = read_json(output / "relationships.json").get("relationships", [])
        relationships.extend(item for item in prior_relationships if isinstance(item, dict))
    relationships = unique_relationships(relationships)
    completed_ids = {
        item.get("tdoc")
        for item in document_by_key.values()
        if item.get("state") in ("processed", "cached_processed", "local_processed")
    }
    resumed_documents = len(completed_ids)

    downloader = StreamingDownloader(
        cache,
        output / ".transfer",
        retries=args.retries,
        refresh=args.refresh,
        router=router,
    )
    related_ids.update(
        endpoint
        for relationship in relationships
        if relationship.get("classification") != "invalidated"
        for endpoint in (relationship.get("from"), relationship.get("to"))
        if endpoint
    )
    priority_by_id = {
        tdoc: 0 if tdoc in explicit_ids else task_priority(tdoc, record_by_id, matched_ids)
        for tdoc in related_ids
    }
    core_ids = {tdoc for tdoc, priority in priority_by_id.items() if priority == 0}
    if not core_ids:
        core_ids = set(matched_ids)
    selected_ids = core_ids if args.stage == "core" else set(related_ids)

    manifest_template = {
        "schema_version": 2,
        "collector_version": VERSION,
        "parser_version": PARSER_VERSION,
        "run_signature": run_signature,
        "query": args.query,
        "companies": args.company,
        "included_tdocs": sorted(explicit_ids),
        "meetings": meetings,
        "stage": args.stage,
        "metadata": metadata_manifest,
    }
    transfer_results: list[DownloadResult] = []
    parsed_count = 0
    unsupported = 0
    first_evidence_seconds: float | None = None
    max_active = 0
    final_window = args.max_concurrency
    pressure_events = 0
    visited = set(completed_ids)
    pending = set(selected_ids) - visited

    def flush_state() -> None:
        current_relationships = unique_relationships(relationships)
        current_evidence = deduplicate_evidence(evidence_rows)
        documents = sorted(document_by_key.values(), key=lambda item: (item.get("priority", 9), item.get("tdoc", ""), item.get("source") or ""))
        current_failures = list(fetcher.failures)
        current_failures.extend(
            {"source": result.task.source, "tdoc": result.task.tdoc, "error": result.error or result.state}
            for result in transfer_results
            if result.state == "fetch_error"
        )
        current_requested = len(transfer_results)
        current_cache_hits = sum(result.cache_hit for result in transfer_results)
        write_json(output / "manifest.json", {**manifest_template, "documents": documents})
        write_json(output / "relationships.json", {"schema_version": 2, "relationships": current_relationships})
        write_jsonl(output / "evidence.jsonl", current_evidence)
        write_jsonl(output / "document_index.jsonl", document_index)
        write_json(output / "diffs.json", {"schema_version": 2, "diffs": build_diffs(current_relationships, document_index)})
        write_json(
            output / "coverage.json",
            {
                **base_coverage,
                "downloaded_archives": sum(result.state in ("downloaded", "resumed_complete") for result in transfer_results),
                "parsed_files": parsed_count,
                "unsupported_files": unsupported,
                "failures": current_failures,
                "completeness": (
                    "partial"
                    if warnings or current_failures or unsupported or router.stale_cache_hits
                    else "no_known_gaps"
                ),
                "total_body_bytes": sum(result.body_bytes for result in transfer_results),
                "resumed_bytes": sum(result.resumed_bytes for result in transfer_results),
                "cache_hits": current_cache_hits,
                "parsed_cache_hits": sum(result.parsed_cache_hit for result in transfer_results),
                "cache_hit_rate": current_cache_hits / current_requested if current_requested else 0.0,
                "retry_count": sum(result.retries for result in transfer_results),
                "first_evidence_seconds": first_evidence_seconds,
                "stage_timings": {"total_seconds": time.monotonic() - run_started},
                "resumed_documents": resumed_documents,
                "stage": args.stage,
                "run_state": "in_progress",
                "source_routing": router.coverage(),
                "source_fallbacks": list(router.fallbacks),
            },
        )

    if args.download == "metadata":
        for tdoc in sorted(selected_ids, key=lambda item: (priority_by_id.get(item, 3), item)):
            sources = file_by_tdoc.get(tdoc, [])
            source = sources[0] if sources else None
            document_by_key[(tdoc, source)] = {
                "tdoc": tdoc,
                "state": "metadata_only" if source else "missing",
                "source": source,
                "priority": priority_by_id.get(tdoc, 3),
                "cache_state": "not_requested",
            }
        flush_state()
    else:
        while pending:
            ordered = sorted(pending, key=lambda item: (priority_by_id.get(item, 3), item))
            batch_ids = ordered[: args.batch_size]
            tasks: list[DownloadTask] = []
            for tdoc in batch_ids:
                pending.discard(tdoc)
                sources = file_by_tdoc.get(tdoc, [])
                if not sources:
                    document_by_key[(tdoc, None)] = {
                        "tdoc": tdoc,
                        "state": "missing",
                        "source": None,
                        "priority": priority_by_id.get(tdoc, 3),
                        "cache_state": "miss",
                    }
                    visited.add(tdoc)
                    continue
                source = sorted(sources)[0]
                referer = next((meeting.get("resolved") for meeting in meetings if meeting.get("kind") == "url"), None)
                tasks.append(DownloadTask(priority_by_id.get(tdoc, 3), tdoc, source, referer))
            if not tasks:
                flush_state()
                continue

            def parse_result(result: DownloadResult) -> dict[str, Any]:
                return parse_downloaded_document(
                    result,
                    output=output,
                    terms=terms,
                    cache=cache,
                    save_matched=args.download == "matched",
                )

            def handle_pair(result: DownloadResult, parsed: dict[str, Any] | None) -> None:
                nonlocal parsed_count, unsupported, first_evidence_seconds, relationships
                transfer_results.append(result)
                visited.add(result.task.tdoc)
                if result.state == "fetch_error" or parsed is None:
                    document_by_key[(result.task.tdoc, result.task.source)] = {
                        "tdoc": result.task.tdoc,
                        "state": "fetch_error",
                        "source": result.task.source,
                        "priority": result.task.priority,
                        "cache_state": "miss",
                        "download_seconds": result.elapsed_seconds,
                        "retry_count": result.retries,
                        "error": result.error,
                        "effective_source": result.effective_source,
                        "source_kind": result.source_kind,
                        "fallback_reason": result.fallback_reason,
                    }
                    flush_state()
                    return
                parsed_count += parsed.get("parsed", 0)
                unsupported += parsed.get("unsupported", 0)
                if parsed.get("evidence") and first_evidence_seconds is None:
                    first_evidence_seconds = time.monotonic() - run_started
                evidence_rows.extend(parsed.get("evidence", []))
                document_index.extend(parsed.get("document_index", []))
                new_relationships = [item for item in parsed.get("relationships", []) if isinstance(item, dict)]
                relationships.extend(new_relationships)
                state = (
                    "local_processed"
                    if result.state == "local"
                    else ("cached_processed" if result.cache_hit else "processed")
                )
                document_by_key[(result.task.tdoc, result.task.source)] = {
                    "tdoc": result.task.tdoc,
                    "state": state,
                    "source": result.task.source,
                    "effective_source": result.effective_source,
                    "source_kind": result.source_kind,
                    "fallback_reason": result.fallback_reason,
                    "priority": result.task.priority,
                    "cache_state": "parsed_hit" if result.parsed_cache_hit else ("hit" if result.cache_hit else "miss"),
                    "etag": result.etag,
                    "last_modified": result.last_modified,
                    "content_length": result.content_length,
                    "sha256": result.sha256,
                    "body_bytes": result.body_bytes,
                    "resumed_bytes": result.resumed_bytes,
                    "retry_count": result.retries,
                    "download_seconds": result.elapsed_seconds,
                    "files": parsed.get("files", []),
                }
                if args.stage == "complete":
                    for relationship in new_relationships:
                        if relationship.get("classification") == "invalidated":
                            continue
                        target_id = relationship.get("to")
                        if not target_id or target_id in visited or target_id in pending:
                            continue
                        if target_id in record_by_id:
                            for meeting in meetings:
                                if meeting.get("kind") == "url" and meeting.get("resolved"):
                                    file_by_tdoc.setdefault(target_id, [urllib.parse.urljoin(meeting["resolved"], f"Docs/{target_id}.zip")])
                        if target_id in file_by_tdoc:
                            priority_by_id.setdefault(target_id, task_priority(target_id, record_by_id, matched_ids))
                            pending.add(target_id)
                relationships = unique_relationships(relationships)
                flush_state()

            pairs, scheduler = adaptive_pipeline(
                tasks,
                downloader,
                parse_result,
                max_concurrency=args.max_concurrency,
                parse_workers=args.parse_workers,
                on_complete=handle_pair,
            )
            max_active = max(max_active, scheduler.maximum_active)
            final_window = scheduler.final_window
            pressure_events += scheduler.pressure_events
            del pairs

    all_failures = list(fetcher.failures)
    all_failures.extend(
        {"source": result.task.source, "tdoc": result.task.tdoc, "error": result.error or result.state}
        for result in transfer_results
        if result.state == "fetch_error"
    )
    downloaded_count = sum(result.state in ("downloaded", "resumed_complete") for result in transfer_results)
    cache_hits = sum(result.cache_hit for result in transfer_results)
    parsed_cache_hits = sum(result.parsed_cache_hit for result in transfer_results)
    requested = len(transfer_results)
    has_missing = any(item.get("state") in ("missing", "fetch_error") for item in document_by_key.values())
    total_seconds = time.monotonic() - run_started
    coverage = {
        **base_coverage,
        "downloaded_archives": downloaded_count,
        "parsed_files": parsed_count,
        "unsupported_files": unsupported,
        "failures": all_failures,
        "relationship_expansion_stable": not pending,
        "completeness": (
            "partial"
            if warnings or all_failures or unsupported or has_missing or router.stale_cache_hits
            else "no_known_gaps"
        ),
        "total_body_bytes": sum(result.body_bytes for result in transfer_results),
        "resumed_bytes": sum(result.resumed_bytes for result in transfer_results),
        "cache_hits": cache_hits,
        "parsed_cache_hits": parsed_cache_hits,
        "cache_hit_rate": cache_hits / requested if requested else 0.0,
        "retry_count": sum(result.retries for result in transfer_results),
        "maximum_active_downloads": max_active,
        "configured_concurrency": args.max_concurrency,
        "final_adaptive_window": final_window,
        "pressure_events": pressure_events,
        "resumed_documents": resumed_documents,
        "first_evidence_seconds": first_evidence_seconds,
        "stage": args.stage,
        "run_state": "complete",
        "stage_timings": {"total_seconds": total_seconds},
        "source_routing": router.coverage(),
        "source_fallbacks": list(router.fallbacks),
    }
    flush_state()
    write_json(output / "coverage.json", coverage)
    print(
        json.dumps(
            {
                "output": str(output),
                "candidates": len(scoped_records),
                "stage": args.stage,
                "downloaded": downloaded_count,
                "cache_hits": cache_hits,
                "failures": len(all_failures),
            },
            ensure_ascii=False,
        )
    )
    return 0 if meetings and any(meeting.get("resolved") for meeting in meetings) else 2


def resolve_command(args: argparse.Namespace) -> int:
    router = router_from_args(args)
    fetcher = Fetcher(router)
    cache = CacheManager(Path(args.cache_dir) if args.cache_dir else None, enabled=not args.no_cache)
    meetings = [
        resolve_meeting(fetcher, hint, cache, refresh=args.refresh)
        for hint in args.meeting
    ]
    payload = {
        "schema_version": 1,
        "collector_version": VERSION,
        "generated_at": utc_now(),
        "meetings": meetings,
        "checked_requests": fetcher.checked,
        "metadata_body_bytes": fetcher.body_bytes,
        "metadata_cache_hits": len(fetcher.cache_hits),
        "failures": fetcher.failures,
        "source_routing": router.coverage(),
        "source_fallbacks": list(router.fallbacks),
    }
    if args.output:
        write_json(Path(args.output).resolve(), payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if all(meeting.get("status") == "resolved" for meeting in meetings) else 2


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--version", action="version", version=VERSION)
    subparsers = result.add_subparsers(dest="command", required=True)
    resolver = subparsers.add_parser("resolve")
    resolver.add_argument("--meeting", action="append", required=True, help="Meeting number/date hint, official URL, or local directory; repeatable.")
    resolver.add_argument("--output", help="Optional JSON output file.")
    resolver.add_argument("--cache-dir")
    resolver.add_argument("--no-cache", action="store_true")
    resolver.add_argument("--refresh", action="store_true")
    resolver.add_argument("--mirror-root", help="Override the configured private file-mirror root.")
    resolver.add_argument("--no-mirror", action="store_true", help="Disable private-mirror fallback.")
    for command in ("preview", "collect"):
        child = subparsers.add_parser(command)
        child.add_argument("--meeting", action="append", required=True, help="Meeting number/date hint, official URL, or local fixture directory; repeatable.")
        child.add_argument("--query", required=True, help="Natural-language topic, KI, Solution, Solution Variant, or TDoc.")
        child.add_argument("--include-tdoc", action="append", default=[], help="Explicit TDoc seed; repeatable.")
        child.add_argument("--company", action="append", default=[], help="Company filter; repeatable.")
        child.add_argument("--aliases", help="UTF-8 JSON mapping canonical company names to aliases.")
        child.add_argument("--output", required=True)
        child.add_argument("--retries", type=int, default=3)
        child.add_argument("--cache-dir")
        child.add_argument("--no-cache", action="store_true")
        child.add_argument("--refresh", action="store_true")
        child.add_argument("--mirror-root", help="Override the configured private file-mirror root.")
        child.add_argument("--no-mirror", action="store_true", help="Disable private-mirror fallback.")
        if command == "collect":
            child.add_argument("--download", choices=("matched", "metadata"), default="matched")
            child.add_argument("--stage", choices=("core", "complete"), default="complete")
            child.add_argument("--max-concurrency", type=int, choices=range(1, 9), default=4, metavar="1..8")
            child.add_argument("--parse-workers", type=int, choices=range(1, 5), default=2, metavar="1..4")
            child.add_argument("--batch-size", type=int, default=8)
    cache = subparsers.add_parser("cache")
    cache_actions = cache.add_subparsers(dest="cache_action", required=True)
    info = cache_actions.add_parser("info")
    info.add_argument("--cache-dir")
    clear = cache_actions.add_parser("clear")
    clear.add_argument("--cache-dir")
    clear.add_argument("--yes", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        if args.command == "cache":
            cache = CacheManager(Path(args.cache_dir) if args.cache_dir else default_cache_root(), enabled=True)
            if args.cache_action == "info":
                print(json.dumps(cache.info(), ensure_ascii=False))
                return 0
            if not args.yes:
                print("error: cache clear requires --yes", file=sys.stderr)
                return 2
            print(json.dumps({"cleared": cache.clear()}, ensure_ascii=False))
            return 0
        if args.command == "resolve":
            return resolve_command(args)
        if args.command in ("preview", "collect") and args.retries < 0:
            raise CollectorError("--retries cannot be negative")
        if args.command == "collect":
            if args.batch_size < 1:
                raise CollectorError("--batch-size must be at least 1")
        return collect(args)
    except CollectorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
