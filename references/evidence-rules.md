# Evidence and relationship rules

## Evidence hierarchy

Prefer, in order:

1. Meeting agenda/status records and approved meeting output.
2. Proposal text with a stable TDoc identifier and locator.
3. Explicit revision, merge, or response wording in another TDoc.
4. Title, source/company metadata, and document history.
5. Agent inference from technical overlap.

Levels 1-3 may establish facts when unambiguous. Levels 4-5 normally establish candidates only.

## Scope membership

Include a TDoc when at least one strong signal or two independent weaker signals connect it to the requested subject.

Strong signals include an explicit Agenda Item, KI, Solution/Solution Variant, direct merge/revision reference, or a meeting record assigning the TDoc to that subject. Weaker signals include title/body terminology, company metadata, and semantic similarity.

Expand from seed documents in both directions through explicit revision, merge, response, and approval references. Record why every expanded document entered the scope.

## Relationship semantics

Store each edge with `from`, `to`, `type`, source TDoc/file, locator, quoted excerpt or normalized evidence, and confidence.

- `revision_of`: a document explicitly revises or replaces another.
- `merged_into`: meeting metadata or proposal text explicitly directs content into another TDoc.
- `input_to`: a document is explicitly listed as input to a baseline or revision.
- `approved_version_of`: an approved output is explicitly connected to an earlier baseline/revision.
- `responds_to`: a document explicitly comments on or answers another proposal.

Do not transfer a relation from an adjacent agenda row. Do not infer a relation merely because identifiers are close or topics overlap.

## Meeting dispositions

- `Approved` establishes the meeting disposition of that version, not automatic acceptance of every source contribution.
- `Agreed` and `Endorsed` retain their exact meeting wording.
- `Not Handled` means the meeting did not handle it in the recorded context; it is not evidence of rejection.
- `Merge into X` establishes a workflow relation to X, not full substantive adoption.
- `Postponed`, `Withdrawn`, and `Revised` must remain distinct.

## Company viewpoints

Use the following labels:

- **Explicit support**: direct supportive statement or proposal advocating the position.
- **Explicit opposition**: direct rejection or incompatible alternative accompanied by clear wording.
- **Concern**: stated risk, objection, missing requirement, or requested safeguard without categorical opposition.
- **Neutral clarification**: question, editorial correction, or factual clarification without a directional position.
- **Unclear**: evidence does not justify another label.

Separate document authorship from meeting comments by other companies. Co-signing supports attribution to the submitted document only; it does not prove agreement with later revisions or every merged detail.

For each material position, provide the company, label, proposition, evidence locator, evidence strength, and any Agent inference. Prefer “the proposal states” over “the company believes” when attribution is uncertain.

## Completeness statement

Every final analysis must state:

- Meetings and directories inspected.
- Agenda/KI/Solution filters applied.
- Number of candidate, retrieved, parsed, unsupported, and failed documents.
- Whether relationship expansion reached a stable closure.
- Known gaps and how they could change the conclusions.
