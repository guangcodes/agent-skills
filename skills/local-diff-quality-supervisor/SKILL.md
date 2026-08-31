---
name: local-diff-quality-supervisor
description: Review, repair, and quality-gate an existing staged, unstaged, or untracked local Git diff until it is a functionally complete READY_LOCAL_DIFF with zero known in-scope bugs. Use when implementation work already exists and Codex must reconstruct acceptance, run one systematic initial review, repair frozen root-cause batches through code-review-fix-loop when available, control scope extensions, perform focused impact reviews and validation, and stop deterministically without committing, pushing, creating a PR, merging, deploying, or entering an unbounded review-fix loop. Do not use for implementation from a clean worktree, read-only review, tiny localized edits, CI-only failures, production operations, or GitHub comment triage.
---

# Local Diff Quality Supervisor

## Objective

Treat the existing local Git diff as both the input and the work product:

> Convert the current local diff into a functionally complete, review-clean, verified
> `READY_LOCAL_DIFF` with zero known in-scope bugs.

Keep the defect standard invariant. A review budget, missing evidence, or convergence threshold may
pause work but never convert incomplete work into success. Do not claim that unknown bugs are
impossible; bind the READY claim to the recorded scope, review surfaces, validations, and diff hash.

This Skill owns requirements, scope, the finding ledger, review evidence, convergence, validation,
and READY. It never commits, pushes, opens a PR, merges, deploys, or changes production state.

## Required resources

At initialization, read:

- [references/contracts.md](references/contracts.md) for JSON command contracts.
- [references/lifecycle-matrix.md](references/lifecycle-matrix.md) before changing restore,
  migration, evidence, or convergence behavior.
- [references/review-surfaces.md](references/review-surfaces.md) for review and invalidation rules.
- [references/readiness.md](references/readiness.md) before claiming `READY_LOCAL_DIFF`.

Use `scripts/delivery-state.py` for every transition. Never edit state JSON directly. Resume an
existing state instead of recreating it; schema migration preserves lifetime history, normalizes
current requirement defaults, and backfills all current required fields before promoting the
stored version.

## Ownership boundary

The supervisor controls the complete workflow. For one frozen remediation batch:

1. Prefer `code-review-fix-loop` in `frozen_batch` mode when installed and applicable.
2. Otherwise execute the same batch contract directly.
3. Never let the batch executor run a new full initial review, expand scope, reset outer state, or
   claim overall READY.

The supervisor itself performs the initial review campaign, impact-review routing, design gate,
final validation, and READY decision.

## Workflow

### 1. Freeze the local input

Resolve the Git root, HEAD, and complete staged, unstaged, and untracked path set. Refuse an empty
diff. Hash staged index state and unstaged worktree state separately, including untracked content.
Include the raw, unfiltered worktree manifest for every currently staged or unstaged tracked path in
the diff identity so clean filters, EOL normalization, or ignored file-mode changes cannot preserve
stale evidence.
Before creating state, reject an initial filename that the frozen batch literal-path contract cannot
represent, including Git pathspec or glob syntax, so initialization cannot authorize an
unremediable path.
Reject changed submodule/gitlink paths as unsupported. Freeze the initial diff hash and exact path
list before review or modification.

Reconstruct:

- Objective, original request, scope, and non-goals.
- Acceptance criteria and falsifiable invariants.
- Required validations.
- Authorization boundary and unresolved correctness decisions.
- Optional `allowed_paths` as the maximum path boundary. Exact files do not authorize descendants;
  mark a not-yet-created directory with a trailing `/`. Existing directories are frozen as
  directory boundaries during initialization.

The initial diff paths become the exact authorized write set. `allowed_paths` does not itself
authorize edits; it only limits later `extend-scope` requests.

Initialize state outside the target repository:

```bash
python3 <skill-dir>/scripts/delivery-state.py init \
  --state <state.json> \
  --project-root <git-root> \
  --requirements-file <requirements.json>
```

Stop at `decision_required` when product, permission, schema, billing, lifecycle, or ownership
semantics materially affect correctness.

### 2. Run one systematic initial review

Review the exact frozen diff once across every required review surface. Keep the reviewer read-only
and finish the entire campaign before remediation.

```bash
python3 <skill-dir>/scripts/delivery-state.py record-review \
  --state <state.json> \
  --result-file <initial-review.json>
```

Classify every finding:

- `requirement_gap`
- `introduced_regression`
- `blocking_dependency`
- `test_infrastructure`
- `decision_required`
- `independent_follow_up`
- `enhancement`
- `false_positive`

The first four create current-delivery derived requirements. An introduced regression can never
become a follow-up. Only causally independent problems and enhancements may remain non-blocking.

### 3. Approve necessary scope extensions

Do not silently edit a path outside the current exact authorization. Before changing a direct
dependency or missing implementation path, record an extension linked to exactly one open finding
representing the requirement gap, regression, blocking dependency, or evidence defect:

```bash
python3 <skill-dir>/scripts/delivery-state.py extend-scope \
  --state <state.json> \
  --result-file <scope-extension.json>
```

Require an exact literal path list, reason, parent ID, and invalidated review surfaces. Reject
directories, globs, pathspecs, unrelated cleanup, and paths outside the user's maximum boundary.

### 4. Execute one frozen root-cause batch

Group only open findings with one canonical root-cause key. Normalize case and whitespace before
comparing identity; retain the readable normalized cause in the batch. Freeze exact paths and mode:

```bash
python3 <skill-dir>/scripts/delivery-state.py begin-fix \
  --state <state.json> \
  --finding-ids-file <finding-ids.json> \
  --batch-paths-file <batch-paths.json> \
  --test-mode <none|light|deep>
```

Require the live diff to match the latest accepted evidence. Persist both the Git index entries and
the raw, unfiltered worktree content-and-mode manifest for every authorized path before handing the
batch to an executor. Retain a separate Git-filtered object ID only for HEAD-equivalence checks.
Reject a frozen batch whose final path component is a symlink, and recheck every batch endpoint
before `record-fix` or `cancel-fix`, so an executor cannot write through an authorized link.

Pass the returned `executor_contract` to `code-review-fix-loop`. The batch executor must:

1. Verify each finding against current code and reproducible evidence.
2. Diagnose the shared root cause.
3. Modify only the frozen batch paths.
4. Run the returned surface-aware `preflight_checklist` once before impact review.
5. Return actual changed paths, invalidated surfaces, diff hashes, and executor identity.

The consolidated preflight always checks changed-path integrity and adds only relevant feature-flag,
expiry, retry/CAS, failure-path, or fixture checks for the affected surfaces. Record every check by
ID with actual evidence. At `record-fix`, expand the required checklist from every reported
invalidated surface, not only the finding's original surfaces. Do not call a reviewer until the
complete checklist passes.

If verification proves that no code change is appropriate, do not fabricate a diff. While the
frozen Git state is still exact, use `cancel-fix` to return every batch finding either to `retry` or
to evidence-backed `false_positive`; this clears the active batch without resetting delivery state.
Cancellation applies the same ignored-path, out-of-batch-write, and Git-control manifest checks as
`record-fix`.

Record the result:

```bash
python3 <skill-dir>/scripts/delivery-state.py record-fix \
  --state <state.json> \
  --result-file <fix-result.json>
```

The result must identify either `code-review-fix-loop` or `direct`, both in `frozen_batch` mode,
and echo the frozen `test_mode`. Independently compute the actual changed paths from the before and
after manifests; require them to exactly equal the reported paths and remain inside the frozen batch.
Reject any persistent batch write that is absent from the supervised Git diff; a path that cleanly
returns to HEAD is not a persistent write. Scope extensions cannot authorize ignored files or Git
metadata. Reject recently created or modified worktree/Git-control paths outside the frozen batch,
including ignored files that Git does not report. Compare begin/end metadata manifests for every
non-directory worktree entry instead of inferring writes from a wall-clock timestamp. Snapshot
ignored path names as an independent creation/deletion check without reading ignored contents.
Snapshot a content-hashed Git-control manifest so deleted or replaced repository-control files are
rejected too. Exclude the supervised Git index content, but include `index.lock` so a persistent
lock creation or deletion is rejected.
Snapshot the worktree directory set and metadata as well. Reject unauthorized directory creation,
deletion, permission, ownership, or timestamp changes; allow timestamp-only churn solely for
ancestors required by authorized file writes.
Every affected review surface must be invalidated. Fixed findings become `fixed_unverified`, not
verified. Any previously verified current-delivery finding also returns to `fixed_unverified`
because the new diff makes its verification evidence stale. Previously accepted `false_positive`
findings return to `decision_required` and require fresh evidence on the new diff.
If that decision reclassifies a formerly fixable finding as an independent follow-up or enhancement,
remove its current-delivery derived requirement so it cannot strand READY.
If later evidence rediscovers a fixable finding previously marked `false_positive`, reopen its
existing ledger entry, invalidate its surfaces, and revoke READY. A reviewer may reuse the stable
ledger ID when its fingerprint still matches; conflicting reuse of an ID is rejected.

Use the evidence-only fast path without an impact reviewer only when every finding is
`test_infrastructure`, every changed path is test evidence, only `test-reliability` is invalidated,
targeted tests pass under `light` or `deep`, and the change affects no product behavior, fixture
semantics, or shared test helper. A test-evidence path must live below an explicit `test`, `tests`,
or `__tests__` root, use a `.test.` or `.spec.` filename, or be a `.snap` file below
`__snapshots__`; a generic `fixtures` or `snapshots` directory is insufficient. Otherwise retain
the focused impact review.

### 5. Review only invalidated evidence

After a fix, review the actual changed paths, necessary tracked dependencies, and every failed,
blocked, or invalidated surface. Do not rerun the complete initial campaign.
Reject any live diff drift after `record-fix`; an impact review cannot adopt unmanifested edits as
its new evidence baseline.

Record an `impact` review using `record-review`. Clean evidence for unaffected surfaces remains
valid.

Give the focused reviewer only the current batch files, necessary tracked dependencies, invalidated
surfaces, the preflight results, and invariants already proved. Never modify code while it runs.
Do not accept `add-findings` while a frozen fix batch is active; finish or cancel the batch before
external findings can change gates, convergence counters, or the design epoch.

Route by actionable state:

- Open findings → `remediation`.
- No open findings but invalidated surfaces → `impact_review`.
- Clean affected surfaces plus `fixed_unverified` findings → `validation`.
- No findings or invalidated evidence → `validation`.

Never route `fixed_unverified` back to remediation.

### 6. Enter the design-convergence gate when needed

Track both lifetime history and the current `design_epoch`. Trigger
`design_review_required` when the current epoch repeatedly produces the same root cause,
high-priority findings after fixes, repeated surface failure, or repeated invalidation.
Aggregate root-cause recurrence independently of finding location and trigger so different symptoms
of one normalized root cause share the same convergence counter.
Apply that threshold after findings enter through review, `add-findings`, or validation.
This includes the initial review; a clearly repeated root cause freezes patching immediately.
Register each finding's initial root-cause discovery once. Decision reclassification does not add a
new occurrence for a finding already represented in the root-cause ledger. If a decision corrects
the root cause, recompute its canonical key and rebuild aggregate ownership so old counters cannot
remain attached to the obsolete cause. During reconstruction, cap each finding's epoch history at
one discovery plus its recorded epoch recurrences so duplicate surface merges cannot inflate it.
Compute convergence before entering any competing hard gate. Preserve the immediate decision or
validation-failure gate, persist a deferred design review, and require it before remediation resumes.

Freeze patching and diagnose the invalid assumption, state model, ownership boundary, or overall
implementation structure. Resolve with `resume`, `rollback`, or `blocked`:

```bash
python3 <skill-dir>/scripts/delivery-state.py resolve-design-review \
  --state <state.json> \
  --result-file <design-review.json>
```

`resume` and `rollback` start a new design epoch while preserving lifetime counts and history. A
new epoch receives a fresh convergence window; deleting state or history is forbidden.

### 7. Validate and close requirements

Run required validation only after affected review surfaces are clean:

```bash
python3 <skill-dir>/scripts/delivery-state.py record-validation \
  --state <state.json> \
  --result-file <validation.json>
```

Record actual commands or evidence, never planned checks. A finding becomes `verified` only when
validation closes it against the current diff hash. Its derived requirement closes at the same
time.

A new validation submission replaces the prior campaign attempt; a partial submission therefore
clears any older complete campaign even when its evidence passes. A failed or blocked validation
or acceptance item must bind to its own distinct tracked blocking
finding; two failed items cannot share one finding ID.
If it binds to a `fixed_unverified` finding, reopen that finding, invalidate its surfaces, and route
back to remediation. Any review-surface invalidation clears the previously complete validation
campaign, even when the diff hash did not change. Any accepted failed or blocked validation result
also clears it. Reject one payload that both verifies a finding and binds failure to the same ID.
Validation-created requirement gaps or regressions return to remediation; independent findings
become structured follow-ups; decisions enter `decision_required`.

Run independent target tests and typecheck in parallel only when they share no mutable Docker,
database, generated-output, or cache state. Prefer the narrowest applicable command, such as one
Vitest file. After focused review converges, freeze the diff hash and run the complete applicable
lint, test, integration, and coverage matrix once. Submit every required validation and acceptance
item in that single final campaign; piecemeal passes cannot issue READY. Evidence from an older hash
is never final proof.

### 8. Compute and stop at READY_LOCAL_DIFF

Run:

```bash
python3 <skill-dir>/scripts/delivery-state.py evaluate-ready \
  --state <state.json>
```

READY requires:

- The initial frozen campaign completed.
- Every acceptance criterion and required validation passed on the current diff.
- Every required review surface is clean.
- Every current-delivery finding and derived requirement is verified on the current diff or proven
  false.
- No open, fixing, fixed-unverified, decision-required, or blocked finding remains.
- No fix batch or unresolved decision remains.
- Every live diff path is explicitly authorized.
- Current HEAD and diff hash match the recorded evidence.
- The READY manifest records index entries plus raw, unfiltered worktree content/mode for every live
  path; Git-filtered IDs are used only for HEAD equivalence.
- A second snapshot taken after READY manifest capture still matches the accepted snapshot.

The only successful terminal state is `ready_local_diff`. Stop with the verified local code diff.
Use a separate, explicitly authorized workflow for commit, push, PR, merge, deployment, or
production operations.

## Hard gates

Treat these as incomplete:

- `decision_required`
- `design_review_required`
- `validation_failed`
- `blocked`

Only the matching resolver may leave a hard gate. Do not lower acceptance or reclassify a current
regression as follow-up to escape one.

Freeze the diff while a hard gate is active. A resolver must reject HEAD, path, or diff-hash drift;
resolve the decision or design direction before changing code. Use `resolve-design-review` for both
`design_review_required` and a design review that previously returned `blocked`.

## Reporting

Always report:

- State path, delivery ID, phase, baseline, and current diff hash.
- Initial, authorized, and extended paths with extension reasons.
- Acceptance, review-surface, and validation results.
- Remediable, verification-pending, blocking, and follow-up findings.
- Derived requirements.
- Lifetime fix count, current design epoch, and epoch fix count.
- Lifetime and current-epoch root-cause occurrence and recurrence counts.
- Batch executor and direct validation evidence.
- `READY_LOCAL_DIFF` decision and exact missing evidence when rejected.
- Git or external actions actually performed; normally none.
