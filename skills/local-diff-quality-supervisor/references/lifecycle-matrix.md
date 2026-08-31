# Lifecycle matrix

Use this matrix before changing migration, recovery, finding, evidence, or convergence behavior.

## State invariants

| Concern | Required invariant |
|---|---|
| Schema migration | Normalize current requirement defaults and backfill every current required field before version promotion, preserve history, revoke legacy READY, reopen unsafe in-flight work, and require the current READY contract. |
| Maximum path boundary | Exact file entries authorize only themselves; trailing-slash or initialization-time existing directory entries authorize descendants, while every new file still requires an explicit scope extension. |
| Fixable finding | `open -> fixing -> fixed_unverified -> verified`; its derived requirement follows the same delivery state and evidence hash. |
| False positive | Store evidence on the current diff. A later code fix moves it to `decision_required`; reclassification updates or removes its derived requirement. |
| Follow-up | Remains outside current-delivery readiness and has no blocking derived requirement. |
| Review evidence | Initial and impact review evidence binds to one exact diff hash; neither review may adopt drift. |
| Validation evidence | Acceptance, validation, finding, and derived-requirement evidence all bind to the current diff hash. |
| Failed fix validation | A failure bound to `fixed_unverified` reopens the finding, invalidates its surfaces, and returns to remediation. |
| Frozen endpoint | Begin, record, and cancel reject a batch path whose final component is a symlink; schema migration reopens older active batches. |
| Complete campaign | Any later validation submission or review-surface invalidation clears it, even when the diff hash is unchanged; only another complete submission replaces it. |
| READY | Only the current schema may issue READY after a stable manifest and post-manifest snapshot. Migration never preserves an older READY claim. |

## Counter invariants

| Counter | Increment | Reset |
|---|---|---|
| Lifetime failure | Every failed or blocked review result, including initial review | Never |
| Epoch failure | Every failed or blocked review result in the current design epoch | Successful design-review resolution |
| Lifetime invalidation | Every new evidence-invalidating cycle, including design-review invalidation | Never |
| Epoch invalidation | Every new invalidation in the current design epoch | Successful design-review resolution |
| Root occurrence | A newly discovered fixable root cause or a verified recurrence | Epoch occurrence resets at design review; lifetime never resets |

Root-cause identity is `sha256(normalize(casefold(collapse-whitespace(root_cause))))`. Finding
fingerprints, fix batches, lifetime counters, and epoch counters use this same identity. Correcting
a finding's root cause rebuilds aggregate ownership without creating another occurrence.
Reconstruction caps one finding's epoch history at its initial discovery plus recorded epoch
recurrences; an event that only merges another affected surface cannot add an occurrence.

If a convergence threshold is reached while `decision_required` or `validation_failed` is already
active, preserve the current hard gate and persist a deferred design review. Clearing the first gate
must enter `design_review_required` before remediation resumes.

Every command that is about to enter a hard gate computes root-cause and surface-invalidation
convergence first. This ordering applies to validation decisions, invalid validation bindings, and
false-positive evidence invalidated by a later fix.

| Existing gate | Immediate correctness gate | Convergence | Result |
|---|---|---|---|
| decision or validation failure | any | reached | Preserve existing gate; persist deferred design review. |
| none | decision required | reached | Enter decision gate; persist deferred design review. |
| none | validation failure | reached | Enter validation-failure gate; persist deferred design review. |
| none | none | reached | Enter design review immediately. |
| none | correctness gate | not reached | Enter the correctness gate. |
| none | none | not reached | Route by remediation, impact review, validation, or acceptance state. |

Decision resolution and migration reconstruction do not create a new discovery. A finding records
whether its root cause is already represented in the ledger.

## Review routing

```text
record-fix
  -> consolidated local preflight
  -> evidence-only fast path when every strict eligibility assertion holds
  -> otherwise focused impact review
  -> validation
  -> stable diff hash
  -> one final complete validation campaign containing every required item
  -> READY evaluation
```

Never modify code while a reviewer is running. Never reuse validation evidence from another diff
hash. Never combine different root causes merely to reduce reviewer calls.
Do not accept external `add-findings` while a frozen fix batch is active. Finish or cancel the batch
before finding ingestion can change hard gates, counters, or the design epoch.
