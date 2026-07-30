# 3GPP Proposal Analysis Skill

A lightweight, evidence-grounded Agent Skill for scoping, retrieving, tracing,
and analyzing 3GPP meeting proposals.

It combines:

- Agent reasoning for intent clarification, company-position analysis, and conclusions.
- A thin, portable workflow in `SKILL.md`.
- Optional Python standard-library scripts for deterministic retrieval, parsing,
  relationship candidates, evidence indexing, and revision diffs.

It does **not** require an MCP server, database, background service, dedicated UI,
or third-party Python package.

## What it supports

- Clear requests such as a meeting + KI + Solution Variant + comparison dimension.
- Vague requests such as "analyze this meeting" or "analyze AI proposals", using a
  real-data preview before the Agent asks a high-impact clarification question.
- Baseline, revision, merge, input, approval, and invalidated relationship evidence.
- Company comparisons with direct-evidence discipline.
- `core` collection for baseline/approved evidence, followed by resumable
  `complete` collection.
- Bounded parallel downloads, adaptive throttling, retries, conditional requests,
  streaming writes, Range resume, deterministic parse caching, and multi-process locks.
- Incremental schema-v2 evidence packages suitable for audit and Agent follow-up.

## Repository layout

```text
.
├── SKILL.md
├── agents/openai.yaml
├── references/
├── scripts/
│   ├── collect_3gpp_evidence.py
│   └── transfer_runtime.py
└── tests/
```

The repository root is the Skill folder, so it can be copied directly into a
host's Skill directory.

## Install

### Codex

Clone or copy this repository to:

```text
~/.codex/skills/analyze-3gpp-meeting-proposals
```

Restart Codex, then invoke it explicitly with:

```text
$analyze-3gpp-meeting-proposals
```

Natural-language 3GPP meeting, KI, Solution, TDoc, or company-position requests
can also trigger it implicitly.

### Other Skill-compatible Agents

Copy the repository to the Agent's Skill directory. The workflow remains usable
without Python: follow the manual fallback procedure in `SKILL.md`.

## CLI quick start

Python 3.10 or newer is recommended. The collector uses only the standard library.

Preview the real meeting scope:

```bash
python scripts/collect_3gpp_evidence.py preview \
  --meeting "SA2#175-AH-e" \
  --query "KI#18 Solution Variant#18.7 intent structure" \
  --output output/preview
```

Collect the highest-priority evidence first:

```bash
python scripts/collect_3gpp_evidence.py collect \
  --meeting "SA2#175-AH-e" \
  --query "KI#18 Solution Variant#18.7 intent structure" \
  --stage core \
  --output output/analysis
```

Resume the same scope and complete it:

```bash
python scripts/collect_3gpp_evidence.py collect \
  --meeting "SA2#175-AH-e" \
  --query "KI#18 Solution Variant#18.7 intent structure" \
  --stage complete \
  --output output/analysis
```

Relevant performance controls:

```text
--max-concurrency 1..8
--parse-workers 1..4
--batch-size N
--retries N
--cache-dir PATH
--no-cache
--refresh
```

Inspect or explicitly clear the public-file cache:

```bash
python scripts/collect_3gpp_evidence.py cache info
python scripts/collect_3gpp_evidence.py cache clear --yes
```

## Outputs

- `scope_preview.json`: resolved meeting and candidate scope.
- `manifest.json`: per-document transfer, cache, parser, and recovery state.
- `relationships.json`: evidence-located relationship candidates.
- `evidence.jsonl`: selected evidence with paragraph hashes and change state.
- `coverage.json`: completeness, failures, bytes, cache hits, concurrency, and timings.
- `document_index.jsonl`: deterministic paragraph/identifier index.
- `diffs.json`: located baseline/revision/approved paragraph differences.

The script produces facts and relationship candidates. It intentionally does not
generate company viewpoints, consensus, or technical conclusions; those remain
the Agent's responsibility.

## Evidence discipline

- Do not define scope from keyword hits alone.
- Keep source location for every baseline, revision, merge, and approval claim.
- Treat unproven relationships as candidates.
- `Not Handled` does not mean rejected.
- `Merge into` does not mean every contribution was adopted.
- Separate direct evidence from Agent inference and state coverage gaps.

See `references/evidence-rules.md` and `references/analysis-patterns.md`.

## Tests

```bash
python -m unittest discover -s tests -v
```

The suite covers the golden SA2#175-AH-e fixture, relationship isolation,
schema-v2 recovery, bounded parallel performance, 200/206/304/403/416/429
responses, ETag changes, partial transfers, cache locking, and ZIP safety.

## Privacy and responsible use

- The cache contains only public source files and deterministic parsed results.
- User questions, company-viewpoint conclusions, and Agent inference are not cached.
- Do not commit downloaded proposals, cache content, evidence outputs, or `.part` files.
- Use conservative concurrency against public 3GPP infrastructure.
- Respect 3GPP terms, availability, and source attribution.

This project is independent and is not affiliated with or endorsed by 3GPP.
3GPP and related marks belong to their respective owners.

## 中文说明

这是一个轻量化、证据可复核的 3GPP 会议提案分析 Skill。Agent 负责理解用户意图、
需求澄清、公司观点判断和最终结论；标准库脚本只负责机械化范围预览、下载、解析、
关系候选、证据索引和版本差异。

项目不依赖 MCP、数据库、常驻服务、专用 UI 或第三方 Python 包。对于模糊问题，
应先基于真实会议数据生成预览，再由 Agent 提供动态分析方向；对于大范围问题，
可先执行 `--stage core` 获取会议结果、baseline 和 approved 版本，再以相同输出目录
执行 `--stage complete` 补齐范围。

## License

MIT

