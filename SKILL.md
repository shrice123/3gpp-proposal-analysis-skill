---
name: analyze-3gpp-meeting-proposals
description: Resolve, scope, retrieve, trace, and analyze 3GPP meeting proposals across SA1-SA6, RAN1-RAN6, and CT1-CT6 with auditable evidence and automatic private-mirror fallback. Use when a user identifies a meeting by number, month, official URL, file URI, or local directory, or asks about an Agenda Item, Key Issue, Solution or Solution Variant, TDoc chain, company position, consensus, disagreement, adoption, or proposal evolution, including vague whole-meeting or AI-related requests. Also use when producing a sourced comparison or handing evidence to generic DOCX/PPTX capabilities. Do not require or invoke a proposal-analysis MCP service.
---

# Analyze 3GPP Meeting Proposals

Keep reasoning with the host Agent. Use the bundled script only to accelerate mechanical discovery, OOXML extraction, and relationship-candidate collection.

## Work progressively

1. Interpret the requested working group, meeting number or month, topic, company, time range, and desired decision.
2. Resolve a named or date-based meeting before retrieving meeting content:

   ```text
   python scripts/collect_3gpp_evidence.py resolve --meeting "SA5 May 2026"
   ```

   Accept `SA1`-`SA6`, `RAN1`-`RAN6`, and `CT1`-`CT6`. Prefer an exact meeting number or official URL when available. Treat `ambiguous` and `unresolved` as a hard stop for collection. Present the returned official candidates and ask only which exact meeting the user means.
3. Run a cheap scope preview before committing to a large analysis:

   ```text
   python scripts/collect_3gpp_evidence.py preview --meeting "<meeting-or-url>" --query "<topic>" --output "<workdir>"
   ```

4. Decide from the returned candidates, concentration, relationship coverage, and failures:
   - Clear and concentrated: continue without asking.
   - Clear but broad: collect the core decision chain first, give a landscape, then complete the evidence in batches.
   - Ambiguous but concentrated: ask only the highest-impact question.
   - Ambiguous and dispersed: offer 3-5 choices derived from the preview, never a generic fixed menu.
   - User does not know what to choose: summarize the observed landscape and recommend useful deep dives.
5. When the user supplies one or more TDocs, seed the scope explicitly instead of downloading every topical match:

   ```text
   python scripts/collect_3gpp_evidence.py collect --meeting "<meeting-or-url>" --query "<topic>" --include-tdoc "R1-2601001" --output "<workdir>" --stage core
   ```

   Repeat `--include-tdoc` when needed. Collect those TDocs and their explicit valid relationship chain; record absent TDocs as missing.
6. For a broad or relationship-heavy request without explicit TDocs, collect baseline and approved documents first:

   ```text
   python scripts/collect_3gpp_evidence.py collect --meeting "<meeting-or-url>" --query "<topic>" --output "<workdir>" --stage core
   ```

7. Complete the same output directory when the input proposals are needed:

   ```text
   python scripts/collect_3gpp_evidence.py collect --meeting "<meeting-or-url>" --query "<topic>" --output "<workdir>" --stage complete
   ```

   For a small, concentrated scope, run `--stage complete` directly.
8. Read `scope_preview.json`, `manifest.json`, `relationships.json`, `evidence.jsonl`, `document_index.jsonl`, `diffs.json`, and `coverage.json`. Inspect original proposals for every material conclusion.
9. Analyze the baseline-to-approved differences first, then use input proposals to attribute positions and disagreements. Use the host's generic document or presentation skill when a DOCX/PPTX is requested; otherwise deliver sourced Markdown.

Treat candidate count only as a cost signal. Never use a fixed document threshold as the decision rule. `--batch-size` controls execution only and never removes documents from the selected scope.

## Preserve evidence boundaries

- Establish scope from agenda metadata, titles, bodies, identifiers, and relation chains together. A keyword hit alone is insufficient.
- Treat every script-produced relationship as a candidate until its cited source directly establishes it.
- Keep `invalidated` relationships as anomaly evidence and never use them to schedule another download.
- Keep public facts, explicit company statements, meeting dispositions, and Agent inference separate.
- Label company stance as explicit support, explicit opposition, concern, neutral clarification, or unclear. Require direct evidence for strong labels.
- Add confidence and reasoning to inferences. Report coverage, failures, unavailable formats, and likely omissions.
- Never infer that `Not Handled` means rejected, `Merge into` means every idea was adopted, or co-signing means agreement with every later revision.

Read [references/evidence-rules.md](references/evidence-rules.md) before judging proposal relationships or company positions. Read [references/analysis-patterns.md](references/analysis-patterns.md) when selecting an analysis structure or handling a vague/broad request.

## Handle failures and unsupported formats

- Let the script try the public 3GPP source first and automatically fall back to its configured private file mirror. After a host-level public failure opens the run-local circuit, do not force repeated public retries.
- Use `--mirror-root "<private-mirror-uri>"` or `THREEGPP_MIRROR_ROOT` only to override the built-in environment default. Use `--no-mirror` when private-mirror access is inappropriate. Never print mirror credentials or copy private source paths into the final answer.
- Retry through the script's normal User-Agent, Referer, source-routing, and coverage behavior; do not claim completeness when both public and mirror access remain partial.
- Let the script use its bounded download pool and public-document cache. Do not increase concurrency above 8 or launch competing manual download loops.
- Use `--no-cache` when persistence is inappropriate and `--refresh` when the remote body must be reacquired. Never clear the shared cache unless the user explicitly requests it.
- Use generic PDF/document/presentation/spreadsheet capabilities for PDF, legacy `.doc`, images, malformed OOXML, or layout-sensitive evidence.
- If Python is unavailable, follow the same workflow manually: inspect the meeting index and agenda, build a TDoc manifest, download only candidate proposals, trace explicit cross-references, and maintain a coverage ledger.
- On Windows, use `scripts/run-collector.cmd` when Python is installed but not available through the current shell's `PATH` or PowerShell policy blocks direct `.ps1` execution. Add `--no-cache` when the host forbids writes to the user cache.
- If both public and private-mirror discovery fail, reuse a validated cache or ask for an explicit accessible local meeting directory. Use a browser only to locate an exact public 3GPP meeting URL, then rerun `resolve`, `preview`, or `collect`; do not turn the fallback into a serial manual download loop.
- Never bypass the collector's bounded concurrency by launching parallel collectors or downloading proposals one by one in the browser.
- If the meeting, KI, Solution, company, or premise appears inconsistent, show the conflicting evidence and resolve the smallest consequential ambiguity before deep analysis.

The script must not decide company viewpoints, consensus, technical merit, or final adoption.

Read [references/performance-workflow.md](references/performance-workflow.md) when tuning concurrency, inspecting cache/recovery behavior, or continuing an interrupted collection.
