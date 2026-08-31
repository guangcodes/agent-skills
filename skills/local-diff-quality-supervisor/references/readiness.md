# READY_LOCAL_DIFF contract

## Invariant goal

The existing local work must satisfy the agreed scope with zero known in-scope bugs. A convergence
threshold or missing evidence may require design review or a pause, but cannot turn incomplete work
into success.

## Finding routes

Keep routing separate from readiness:

- `remediable_findings`: `open`; next action is a frozen fix batch.
- `verification_pending_findings`: `fixed_unverified`; next action is validation.
- `readiness_blocking_findings`: `open`, `fixing`, `fixed_unverified`,
  `decision_required`, or `blocked`; all prevent READY but require different next actions.

Never route a `fixed_unverified` finding back to remediation merely because it blocks READY.

## READY_LOCAL_DIFF

Accept READY only when all conditions hold against the current Git state:

1. Initialization captured a non-empty staged, unstaged, or untracked local diff.
2. The initial review covered that exact frozen diff before remediation.
3. Every acceptance criterion is `pass` on the current diff hash.
4. Every required review surface is `clean`.
5. Every required validation is `pass` on the current diff hash.
6. Every current-delivery finding is `verified` or evidence-backed `false_positive` on the current
    diff hash.
7. Every derived requirement is `verified` or evidence-backed `false_positive` on the current diff
    hash.
8. No finding is `open`, `fixing`, `fixed_unverified`, `decision_required`, or `blocked`.
9. No fix batch is active.
10. No unresolved correctness decision remains.
11. Current HEAD equals the frozen baseline.
12. The current diff hash, including separately hashed staged-index and unstaged-worktree state,
    matches all accepted evidence.
    For every staged or unstaged tracked path, that identity includes the raw, unfiltered worktree
    manifest in addition to Git's filtered diff representation.
13. Every live diff path belongs to the exact authorized set established by the initial diff and
    recorded scope extensions.
14. The READY manifest records index entries plus raw, unfiltered worktree content/mode for every
    live path; a separate Git-filtered object ID is used only for HEAD-equivalence checks.
15. A snapshot taken after READY manifest capture still matches the snapshot used for the READY
    checks.
16. Final complete validation evidence was produced only after the accepted diff hash stabilized;
    no evidence from an older hash is reused as final proof.
17. One `record-validation` campaign contains every required validation and acceptance item passing
    on that stable hash; accumulated piecemeal evidence is insufficient.
18. No review surface was invalidated after that campaign; any invalidation clears the campaign and
    requires focused review followed by a new complete campaign.
19. No later validation submission occurred after that campaign. Every accepted submission starts
    a new campaign attempt, so partial evidence or a failure clears the older complete campaign.

Independent follow-ups and enhancements are non-blocking delivery byproducts. Requirement gaps,
introduced regressions, blocking dependencies, and evidence-breaking test infrastructure defects
remain part of the current delivery.

## Convergence

Preserve lifetime review, fix, recurrence, and invalidation history. Evaluate non-convergence using
only the current `design_epoch`. A completed design review starts a new epoch without erasing
history, giving the revised design a fresh convergence window.

Count normalized root causes independently from finding fingerprints. Different locations, triggers,
or symptoms with the same normalized root cause contribute to one recurrence counter.
Count discovery events only. Resolving or reclassifying an existing decision finding is not a new
root-cause occurrence.
Track whether each finding already registered its discovery so repeated decision resolution cannot
inflate recurrence counters.

Exceeding a threshold enters `design_review_required`; it never produces READY.
Evaluate the same root-cause threshold when findings are added by review, external `add-findings`,
or validation, including the initial review campaign.

## Incomplete states

- `decision_required`: correctness depends on an unresolved decision.
- `design_review_required`: the current implementation approach is not converging.
- `validation_failed`: failed evidence is missing its own tracked finding.
- `blocked`: an explicit decision stops work before completion.

Resume the same state through its matching resolver. Never recreate state to clear findings or
counters.

`resolve-design-review` is also the recovery route after that gate records `blocked`; it may resume
only while the frozen HEAD, path set, and diff hash still match.

## Output and claims

The only successful terminal output is the verified local code diff:

> The agreed delivery scope is complete with zero known bugs under the recorded review and
> verification evidence.

Do not claim:

> The whole project has no possible unknown bugs.

Do not infer authorization for commit, push, PR creation, merge, deployment, or production changes
from `READY_LOCAL_DIFF`. Route those actions through a separately authorized workflow.
