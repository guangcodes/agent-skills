# JSON contracts

Use UTF-8 JSON outside the target repository. Never include secrets, environment values, tokens,
cookies, private keys, or credential material.

## Requirements

Required:

```json
{
  "objective": "Complete and verify the existing local feature diff.",
  "scope": ["Gateway safety behavior"],
  "acceptance_criteria": [
    {"id": "A1", "description": "A disabled alias is rejected before routing."}
  ]
}
```

Optional:

```json
{
  "original_request": "...",
  "non_goals": ["Production rollout"],
  "allowed_paths": ["apps/gateway/", "packages/db/", "tests/"],
  "authorization_boundary": ["No commit, push, PR, merge, deploy, or provider call"],
  "delivery_target": "local_diff",
  "unresolved_decisions": [],
  "invariants": [
    {"id": "I1", "description": "Unacknowledged generations are never serviceable."}
  ],
  "review_surfaces": [
    {"id": "functional-completeness", "description": "..."}
  ],
  "required_validations": [
    {"id": "direct-tests", "description": "..."}
  ],
  "design_review_thresholds": {
    "high_priority_after_fix_batches": 2,
    "surface_failures": 2,
    "surface_invalidations": 3,
    "root_cause_recurrences": 2
  }
}
```

`delivery_target` may only be `local_diff`. `allowed_paths` is an optional maximum authorization
boundary; initial diff paths are the exact authorized set. Every later path requires
`extend-scope`, even when it lies inside that maximum boundary. An exact file boundary authorizes
only that file. Use a trailing `/` for a directory that does not yet exist; an existing directory
is frozen as a directory boundary during initialization. Migration normalizes legacy requirements
and fills every current design-review threshold before promoting the schema version.

## Review result

```json
{
  "kind": "initial",
  "diff_hash": "<live hash>",
  "coverage_complete": true,
  "surfaces": [
    {
      "id": "functional-completeness",
      "status": "failed",
      "evidence": "The failure path is missing."
    }
  ],
  "findings": [
    {
      "id": "F1",
      "classification": "introduced_regression",
      "severity": "P1",
      "summary": "Malformed freshness data is accepted.",
      "trigger": "Submit a payload whose timestamp is not bound by its digest.",
      "root_cause": "The digest omits serviceability time fields.",
      "location": "apps/gateway/src/runtime-safety.ts",
      "affected_surfaces": ["domain-invariants", "fail-closed", "time-semantics"],
      "evidence": "The parser accepts the mutated payload.",
      "source": "initial-review"
    }
  ]
}
```

Use `kind=initial` once for the exact frozen input and cover every configured surface. Use
`kind=impact` after a fix and cover every failed, blocked, or invalidated surface.
An impact result is accepted only while the live diff hash still equals the hash recorded by the
fix; restore or separately remediate drift before review.

Classifications:

- `requirement_gap`
- `introduced_regression`
- `blocking_dependency`
- `test_infrastructure`
- `independent_follow_up`
- `enhancement`
- `false_positive`
- `decision_required`

Severities are `P0` through `P3`.

## Scope extension

Link every extension to exactly one open current-delivery finding:

```json
{
  "reason": "The regression requires updating its direct serializer dependency.",
  "finding_id": "F1",
  "paths": [
    "apps/gateway/src/runtime-serializer.ts",
    "tests/unit/runtime-serializer.test.ts"
  ],
  "invalidated_surfaces": [
    "functional-completeness",
    "fail-closed",
    "test-reliability"
  ]
}
```

Create a `requirement_gap` finding before extending scope for missing acceptance behavior. Paths
must be normalized, repository-relative, exact literal file paths. Directories, globs, pathspecs,
duplicates, sensitive-looking paths (including standard private-key filenames), and paths outside
`allowed_paths` are rejected before their content is hashed. A scope path whose parent component is
a symlink is also rejected so an authorized repository-relative path cannot traverse outside the
literal Git tree. An untracked symlink itself is valid local work and is hashed by link target and
Git symlink mode without following the target, but it cannot be a frozen remediation batch endpoint.
Recheck endpoints before recording or cancelling a fix so replacement by a symlink is rejected.
Reject `.git` metadata and untracked ignored paths;
reject Git pathspec magic in every caller-supplied literal path. After a fix, reject any persistent
changed-path manifest entry absent from the supervised Git diff. Reject a changed submodule/gitlink
during initialization because this supervisor does not model nested repository state; inspect both
HEAD and the current index so deletion or replacement cannot hide a baseline gitlink.
During a frozen batch, also reject recently created or modified worktree and Git-control paths
outside the exact batch authorization. Compare begin/end metadata manifests for all non-directory
worktree entries; do not infer a write by comparing filesystem ctime to the wall clock. Compare the
ignored path-name set independently without reading ignored contents, and compare a content-hashed
Git-control manifest before and after execution. In a linked worktree, monitor both the worktree
administrative Git directory and the shared Git common directory, excluding only the supervised
index content and object store. Do not exclude `index.lock`; its persistent creation, deletion, or
replacement must fail the batch boundary.
Compare the complete worktree directory set and metadata before and after execution too. Permit
timestamp-only churn for ancestors of authorized file paths because normal file creation or atomic
replacement can update those directories; reject creation, deletion, ownership, permission, or
unrelated-directory timestamp changes outside that narrow exception.
Apply the same literal filename restrictions to the initial Git path set before state creation;
otherwise a path could be authorized at initialization but rejected when remediation begins.

## Finding IDs and batch paths

`finding-ids.json`:

```json
["F1", "F2"]
```

Every finding in one batch must share one canonical root-cause key. Case and repeated whitespace do
not create different root causes. Persist both the normalized readable value and its key in the
finding and frozen batch.

`batch-paths.json`:

```json
[
  "apps/gateway/src/runtime-safety.ts",
  "tests/unit/runtime-safety.test.ts"
]
```

Every batch path must already be in the exact authorized set.

## Fix result

```json
{
  "finding_ids": ["F1"],
  "diff_hash_before": "<hash returned by begin-fix>",
  "diff_hash_after": "<current live hash>",
  "changed_paths": [
    "apps/gateway/src/runtime-safety.ts",
    "tests/unit/runtime-safety.test.ts"
  ],
  "executor": {
    "name": "code-review-fix-loop",
    "mode": "frozen_batch"
  },
  "test_mode": "light",
  "invalidated_surfaces": [
    "domain-invariants",
    "fail-closed",
    "time-semantics",
    "test-reliability"
  ],
  "direct_validation": [
    {
      "id": "changed-path-integrity",
      "status": "pass",
      "evidence": "The focused regression test passed."
    }
  ]
}
```

Executor `name` is `code-review-fix-loop` or `direct`; `mode` is always `frozen_batch`.
`test_mode` must equal the mode frozen by `begin-fix`. Changed paths must be inside the batch
authorization and must exactly match the state machine's manifest-derived actual delta. That
manifest compares Git index entries separately from worktree content and mode, so staging a file is
an actual batch change even when its worktree bytes do not change.
Invalidations must include every affected surface from every finding. All direct validation entries
must pass before the fix is recorded. Recording any later code fix makes every previously verified
current-delivery finding and derived requirement `fixed_unverified` again. It also moves every
`false_positive` finding to `decision_required`; fresh evidence must bind it to the new diff hash.
If a later report matches the original fixable fingerprint, reopen the same finding instead of
discarding it as a duplicate. The report may reuse that stable finding ID; the same ID with a
different fingerprint is rejected.

`begin-fix` returns a surface-aware `preflight_checklist`. `direct_validation` must contain unique
IDs and cover that checklist before impact review. If `record-fix` reports additional invalidated
surfaces, it must also cover every preflight check implied by those surfaces. Additional
targeted-test evidence is allowed.

For a pure test-evidence change, `fix-result.json` may include:

```json
{
  "evidence_only_fast_path": {
    "changes_product_behavior": false,
    "changes_fixture_semantics": false,
    "changes_shared_test_helpers": false,
    "evidence": "Static checks and the targeted test prove test reliability."
  }
}
```

This skips impact review only for `test_infrastructure` findings, test-evidence-only paths,
`test-reliability` as the sole invalidated surface, `light` or `deep` mode, and passing
`targeted-tests`. Any broader code or fixture semantic change requires focused impact review.
A test-evidence path must be below an explicit `test`, `tests`, or `__tests__` root, use a
`.test.` or `.spec.` filename, or be a `.snap` below `__snapshots__`. Generic `fixtures` and
`snapshots` directory names do not qualify by themselves.

## Cancel a no-op fix

Use this only while the Git state and authorized-path manifest still exactly match `begin-fix`:
the same worktree-entry, directory, ignored-path, and Git-control manifests used by `record-fix`
must also match.

```json
{
  "finding_resolutions": [
    {
      "id": "F1",
      "disposition": "false_positive",
      "evidence": "The reproducer proves the behavior is correct."
    }
  ]
}
```

Run `cancel-fix --state <state.json> --result-file <cancellation.json>`. Resolve every frozen
finding exactly once. `retry` restores it to `open`; `false_positive` requires evidence, marks its
derived requirement the same way, and invalidates affected review surfaces for focused re-review.
Cancellation uses the same surface-invalidation convergence gate as a recorded fix.

## Validation result

```json
{
  "diff_hash": "<current live hash>",
  "validations": [
    {"id": "direct-tests", "status": "pass", "evidence": "42/42 passed"},
    {"id": "diff-integrity", "status": "pass", "evidence": "git diff --check passed"}
  ],
  "acceptance": [
    {"id": "A1", "status": "pass", "evidence": "Integration test proves rejection."}
  ],
  "verified_findings": ["F1"],
  "finding_evidence": {
    "F1": "Regression test plus clean impact review closes the finding."
  },
  "new_findings": []
}
```

Every failed or blocked validation or acceptance item must bind to its own tracked blocking finding:

```json
{
  "id": "integration",
  "status": "fail",
  "evidence": "The disabled route remains visible.",
  "finding_id": "F7"
}
```

IDs may appear once per array. Each failed or blocked item must use a distinct finding ID. Blocking
new findings invalidate all their affected surfaces. A finding cannot appear in `verified_findings`
and as a failed binding in the same payload. Every accepted validation submission replaces the
prior campaign attempt, so partial evidence or a failure clears an older complete campaign before
the next hard gate.

## Add findings

```json
{
  "diff_hash": "<current live hash>",
  "findings": [
    {
      "id": "F5",
      "classification": "test_infrastructure",
      "severity": "P2",
      "summary": "The fixture leaks durable state.",
      "trigger": "Run the integration file twice.",
      "root_cause": "Cleanup omits the new table.",
      "location": "tests/integration/runtime-safety.test.ts",
      "affected_surfaces": ["test-reliability"],
      "evidence": "The second run fails.",
      "source": "validation"
    }
  ]
}
```

The live diff must match the latest recorded evidence. This command cannot adopt a changed HEAD or
bypass an active hard gate. If findings reach a convergence threshold while another hard gate is
active, preserve that gate but persist a deferred design review; its resolver must enter
`design_review_required` before remediation can resume.

All event handlers use one routing order: preserve an existing hard gate; otherwise enter an
immediate `decision_required` or `validation_failed` correctness gate; defer any simultaneous
design-convergence gate; enter `design_review_required` directly only when no correctness gate is
active.

## Resolve decision

```json
{
  "resolved_decisions": [
    {
      "decision": "Choose the authoritative serviceability state.",
      "resolution": "The acknowledged database generation is authoritative.",
      "evidence": "The user explicitly confirmed database authority."
    }
  ],
  "finding_resolutions": [
    {
      "id": "F6",
      "classification": "requirement_gap",
      "root_cause": "The prior contract did not identify one authority.",
      "evidence": "The decision creates a current-delivery requirement."
    }
  ]
}
```

A finding resolution may choose a fixable classification, `independent_follow_up`, `enhancement`,
or `false_positive`. A fixable resolution creates a derived requirement.
Reclassifying a finding that already has a derived requirement as `independent_follow_up` or
`enhancement` removes that current-delivery requirement and records the finding as a non-blocking
delivery byproduct.

The live HEAD, paths, and diff hash must still match the evidence recorded when the decision gate
was entered. Resolve the decision before changing code.

## Design review

```json
{
  "decision": "resume",
  "diagnosis": "Patch-level changes cannot converge because serviceability has two authorities.",
  "strategy": "Make the acknowledged DB generation the single authority.",
  "invalidated_surfaces": [
    "domain-invariants",
    "cross-system-consistency",
    "fail-closed",
    "recovery"
  ],
  "added_acceptance": [
    {"id": "A5", "description": "Redis-only generations are never serviceable."}
  ],
  "added_invariants": [
    {"id": "I5", "description": "DB acknowledgement precedes serviceability."}
  ]
}
```

`decision` is `resume`, `rollback`, or `blocked`. Resume and rollback preserve lifetime history but
start a new design epoch with fresh convergence counters.

The live HEAD, paths, and diff hash must still match the evidence recorded when design review was
required. The same command resumes either `design_review_required` or a design review that
previously returned `blocked`. Apply the revised strategy only after the resolver starts the new
design epoch.
