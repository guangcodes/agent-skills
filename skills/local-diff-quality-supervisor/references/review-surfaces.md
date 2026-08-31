# Review surfaces

## Purpose

Use a fixed review campaign to avoid repeated whole-diff exploration. Freeze the existing local
diff at initialization, then review it once across all configured surfaces before changing it. A
later fix or approved scope extension invalidates only surfaces whose assumptions or evidence
changed.

## Default surfaces

| ID | Required review question |
|---|---|
| `functional-completeness` | Does every acceptance criterion have a complete normal and failure path? |
| `domain-invariants` | Are ownership, state, revision, generation, identity, and digest invariants enforced? |
| `data-consistency` | Are persistence, transactions, migrations, compatibility, and delete behavior safe? |
| `concurrency-idempotency` | Are races, duplicate requests, retries, locks, and ordering safe? |
| `cross-system-consistency` | Can DB, cache, services, or providers observe contradictory facts? |
| `fail-closed` | Do missing, stale, future, malformed, partial, or conflicting facts fail safely? |
| `time-semantics` | Are clock source, expiry, boundary, and read-to-use timing explicit? |
| `recovery` | Are restart, cache miss, unknown commit, retry, rollback, and LKG behavior safe? |
| `api-permissions` | Are authentication, authorization, input validation, errors, and compatibility correct? |
| `security-secrecy` | Can secrets, credentials, digests, or private state leak through output or logs? |
| `test-reliability` | Are tests deterministic, isolated, order-independent, and fully cleaned up? |
| `operations` | Are flags, defaults, observability, rollout, stop, and rollback boundaries explicit? |

## Evidence status

- `unreviewed`: no accepted review evidence.
- `clean`: accepted evidence exists and has not been invalidated.
- `failed`: an actionable finding affects the surface.
- `invalidated`: a later change altered the reviewed assumptions or code.
- `blocked`: evidence cannot be completed without a decision or external condition.

Review coverage and runtime validation are independent. A clean review surface does not prove
behavior; a passing test does not prove that the review surface was examined.

## Invalidation examples

| Change | Commonly invalidated surfaces |
|---|---|
| Input validation | functional completeness, API/permissions, fail-closed, test reliability |
| State transition | domain invariants, data consistency, concurrency, recovery |
| Digest or identity binding | domain invariants, cross-system consistency, fail-closed, security |
| TTL or time comparison | time semantics, fail-closed, recovery |
| Transaction or migration | data consistency, concurrency, recovery, operations |
| Cache projection | cross-system consistency, fail-closed, recovery |
| Test fixture cleanup | test reliability |
| Feature flag/default | functional completeness, operations, recovery |

Before impact review, consolidate relevant feature-flag, expiry, retry/CAS, failure-path, and test
fixture checks into one local preflight. Select checks by invalidated surface; do not mechanically
scan unrelated categories. A passing preflight narrows reviewer reconstruction but never replaces a
required impact review.

Do not invalidate unrelated surfaces merely because a file changed. Do not retain clean evidence
when its assumption, dependency, or behavior changed.

Count one invalidation per evidence-invalidating cycle. A scope extension or newly tracked blocking
finding starts the cycle; recording the corresponding fix must not count the same cycle again.

## Full campaign reset

Do not repeat the initial campaign for ordinary fixes. A new complete campaign is justified only
when the requirement scope materially changes, the core state model or architecture is rewritten,
the changed module set expands materially, or the initial review assumptions are proven broadly
invalid. Record this through design review rather than silently resetting state. Preserve lifetime
history while evaluating subsequent convergence inside the new `design_epoch`.
