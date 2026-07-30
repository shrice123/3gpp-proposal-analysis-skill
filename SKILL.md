---
name: analyze-3gpp-meeting-proposals
description: Scope, retrieve, trace, and analyze 3GPP meeting proposals with auditable evidence. Use when a user asks about a 3GPP meeting, Agenda Item, Key Issue, Solution or Solution Variant, TDoc chain, company position, consensus, disagreement, adoption, or proposal evolution, including vague requests such as analyzing a whole meeting or an AI-related topic. Also use when producing a sourced comparison or handing evidence to generic DOCX/PPTX capabilities. Do not require or invoke a proposal-analysis MCP service.
---

# Analyze 3GPP Meeting Proposals

Keep reasoning with the host Agent. Use the bundled script only to accelerate mechanical discovery, OOXML extraction, and relationship-candidate collection.

## Work progressively

1. Interpret the requested meeting, topic, company, time range, and desired decision.
2. Run a cheap scope preview before committing to a large analysis:

   ```text
   python scripts/collect_3gpp_evidence.py preview --meeting "<meeting-or-url>" --query "<topic>" --output "<workdir>"
   ```

3. Decide from the returned candidates, concentration, relationship coverage, and failures:
   - Clear and concentrated: continue without asking.
   - Clear but broad: collect the core decision chain first, give a landscape, then complete the evidence in batches.
   - Ambiguous but concentrated: ask only the highest-impact question.
   - Ambiguous and dispersed: offer 3-5 choices derived from the preview, never a generic fixed menu.
   - User does not know what to choose: summarize the observed landscape and recommend useful deep dives.
4. For a broad or relationship-heavy request, collect baseline and approved documents first:

   ```text
   python scripts/collect_3gpp_evidence.py collect --meeting "<meeting-or-url>" --query "<topic>" --output "<workdir>" --stage core
   ```

5. Complete the same output directory when the input proposals are needed:

   ```text
   python scripts/collect_3gpp_evidence.py collect --meeting "<meeting-or-url>" --query "<topic>" --output "<workdir>" --stage complete
   ```

   For a small, concentrated scope, run `--stage complete` directly.
6. Read `scope_preview.json`, `manifest.json`, `relationships.json`, `evidence.jsonl`, `document_index.jsonl`, `diffs.json`, and `coverage.json`. Inspect original proposals for every material conclusion.
7. Analyze the baseline-to-approved differences first, then use input proposals to attribute positions and disagreements. Use the host's generic document or presentation skill when a DOCX/PPTX is requested; otherwise deliver sourced Markdown.

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

- Retry a 3GPP request with the script's normal User-Agent and Referer behavior; do not claim completeness when access remains partial.
- Let the script use its bounded download pool and public-document cache. Do not increase concurrency above 8 or launch competing manual download loops.
- Use `--no-cache` when persistence is inappropriate and `--refresh` when the remote body must be reacquired. Never clear the shared cache unless the user explicitly requests it.
- Use generic PDF/document/presentation/spreadsheet capabilities for PDF, legacy `.doc`, images, malformed OOXML, or layout-sensitive evidence.
- If Python is unavailable, follow the same workflow manually: inspect the meeting index and agenda, build a TDoc manifest, download only candidate proposals, trace explicit cross-references, and maintain a coverage ledger.
- If the meeting, KI, Solution, company, or premise appears inconsistent, show the conflicting evidence and resolve the smallest consequential ambiguity before deep analysis.

The script must not decide company viewpoints, consensus, technical merit, or final adoption.

Read [references/performance-workflow.md](references/performance-workflow.md) when tuning concurrency, inspecting cache/recovery behavior, or continuing an interrupted collection.
