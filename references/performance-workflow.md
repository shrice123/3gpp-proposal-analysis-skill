# Collection performance and recovery

## Defaults

Use four download workers, two parse workers, batches of eight, and three retries. These are conservative per-host limits for the public 3GPP server.

```text
collect ... --max-concurrency 4 --parse-workers 2 --batch-size 8 --retries 3
```

The scheduler lowers its active window after throttling, server errors, timeouts, or incomplete responses. Do not compensate by starting another collector against the same meeting.

The source router tries the public 3GPP host before its configured private file mirror. A DNS, connection, timeout, access-denied, throttling, or server failure opens a circuit for the remainder of the run so later 3GPP resources go directly to the mirror. A resource-specific 404 still tries the mirror but does not mark the whole public host unavailable.

Use `--mirror-root "<private-mirror-uri>"` or `THREEGPP_MIRROR_ROOT` to override the configured mirror and `--no-mirror` to disable it. A mirror can preserve the public `ftp/` and `dynareport/` hierarchy or expose the TSG FTP directories at its root. Inspect `source_routing` and `source_fallbacks` in `coverage.json` when diagnosing access.

## Stages

- `resolve`: read only the official TSG parent index, working-group directory index, and date calendar needed to identify a meeting. It never downloads proposal bodies.
- `--stage core`: collect baseline and approved documents identified in meeting metadata. If neither role is identifiable, collect the direct query matches.
- `--stage complete`: reuse a matching schema-v2 manifest and add the remaining direct and explicit-relation documents.
- Reuse the same output directory and exact meeting/query/company inputs when continuing from core to complete.

The stage changes execution order, not the interpreted scope.

Use repeatable `--include-tdoc` arguments to restrict the direct scope to known TDocs. Explicit valid relationship endpoints can still expand the scope. Missing requested TDocs remain visible in the manifest and coverage ledger.

An explicit seed cannot reveal a reverse relationship that exists only in an unselected document and is absent from meeting metadata. Report that as a possible omission instead of claiming that the body-derived relationship chain is exhaustive.

## Cache

The default user cache stores source files obtained from public or configured private mirrors and deterministic parsed data. It never stores the user question, Agent inference, or viewpoint conclusions.

```text
cache info
cache clear --yes
collect ... --cache-dir "<path>"
collect ... --no-cache
collect ... --refresh
```

Use `--cache-dir` to share a deliberate cache location across hosts. Use `--no-cache` when persistent public files are not acceptable. Use `--refresh` to bypass cached bodies; do not use it routinely.

The collector validates cache entries with ETag or Last-Modified, resumes partial files with Range/If-Range, validates ZIPs, and atomically replaces completed payloads. A parser-version change reuses the raw file but regenerates deterministic parsed data.
`resolve`, `preview`, and `collect` conditionally cache public working-group indexes, official meeting calendars, and meeting metadata. Repeated operations can therefore receive a 304 with zero metadata body bytes; inspect `metadata_cache_hits` and `metadata_body_bytes`.

Only execute `cache clear --yes` after an explicit user request. Report the removed location and size.

## Progress and recovery

Inspect these schema-v2 metrics:

- `manifest.json`: priority, state, cache state, validators, hashes, retries, bytes, and timings per TDoc.
- `coverage.json`: body bytes, cache and parsed-cache hits, adaptive concurrency, retries, first-evidence time, failures, and completeness.
- `document_index.jsonl`: deterministic paragraph index and change state.
- `diffs.json`: baseline/revision/input differences when both documents are available.

An interrupted collection can be restarted with the same command and output directory. Completed documents remain in the manifest; valid `.part` files and cache entries resume instead of restarting.

Do not treat a fast or cache-heavy run as complete unless `coverage.json` reports the expected scope and explicitly accounts for missing, unsupported, or failed documents.
