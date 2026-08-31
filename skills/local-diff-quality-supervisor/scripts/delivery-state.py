#!/usr/bin/env python3
"""Deterministic delivery-quality state machine.

The script intentionally uses only the Python standard library and Git. It
stores no source contents, credentials, or environment values in its state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn


STATE_VERSION = 9
POLICY_EXIT = 3

DEFAULT_REVIEW_SURFACES = [
    ("functional-completeness", "Requirements and user-visible paths are complete."),
    ("domain-invariants", "State, ownership, revision, generation, and digest invariants hold."),
    ("data-consistency", "Persistence, transactions, migrations, and compatibility are safe."),
    ("concurrency-idempotency", "Concurrent, duplicate, retry, and ordering behavior is safe."),
    ("cross-system-consistency", "Facts remain consistent across services, caches, and providers."),
    ("fail-closed", "Missing, stale, malformed, partial, or contradictory facts fail safely."),
    ("time-semantics", "Clock source, expiry, boundary, and freshness behavior is explicit."),
    ("recovery", "Restart, cache miss, retry, partial commit, and rollback behavior is safe."),
    ("api-permissions", "Authorization, validation, serialization, and compatibility are correct."),
    ("security-secrecy", "Secrets, credentials, logs, errors, and audit boundaries are safe."),
    ("test-reliability", "Tests are isolated, deterministic, and clean up durable facts."),
    ("operations", "Flags, observability, rollout, stop, and rollback boundaries are explicit."),
]

DEFAULT_VALIDATIONS = [
    ("direct-tests", "Direct tests for changed behavior pass."),
    ("static-checks", "Applicable lint, typecheck, or static checks pass."),
    ("integration", "Applicable integration or equivalent runtime evidence passes."),
    ("diff-integrity", "Diff, generated files, whitespace, branch, and status checks pass."),
]

PREFLIGHT_CHECKS = {
    "changed-path-integrity": "Changed paths, generated artifacts, and frozen scope are consistent.",
    "feature-flag-defaults": "Feature flags, defaults, disabled paths, and rollout behavior are coherent.",
    "expiry-time-boundaries": "Expiry, clock source, freshness, and boundary comparisons are explicit.",
    "retry-idempotency-cas": "Retry, idempotency, CAS, ordering, and race behavior preserve invariants.",
    "failure-paths": "Malformed, missing, stale, unauthorized, and partial facts fail safely.",
    "test-fixture-isolation": "Fixtures, shared helpers, cleanup, and repeat execution remain isolated.",
}

PREFLIGHT_SURFACE_MAP = {
    "feature-flag-defaults": {"functional-completeness", "operations"},
    "expiry-time-boundaries": {"time-semantics"},
    "retry-idempotency-cas": {
        "domain-invariants",
        "data-consistency",
        "concurrency-idempotency",
        "recovery",
    },
    "failure-paths": {"fail-closed", "api-permissions", "security-secrecy"},
    "test-fixture-isolation": {"test-reliability"},
}

FIXABLE_CLASSIFICATIONS = {
    "requirement_gap",
    "introduced_regression",
    "blocking_dependency",
    "test_infrastructure",
}
FOLLOW_UP_CLASSIFICATIONS = {"independent_follow_up", "enhancement"}
ALL_CLASSIFICATIONS = (
    FIXABLE_CLASSIFICATIONS
    | FOLLOW_UP_CLASSIFICATIONS
    | {"false_positive", "decision_required"}
)
FINDING_STATUSES = {
    "open",
    "fixing",
    "fixed_unverified",
    "verified",
    "false_positive",
    "follow_up",
    "decision_required",
    "blocked",
}
SURFACE_STATUSES = {"unreviewed", "clean", "failed", "invalidated", "blocked"}
RESULT_STATUSES = {"pending", "pass", "fail", "blocked"}
SEVERITIES = {"P0", "P1", "P2", "P3"}
BATCH_EXECUTORS = {"code-review-fix-loop", "direct"}
BATCH_EXECUTOR_MODES = {"frozen_batch"}

SENSITIVE_EXTENSIONS = {".jks", ".key", ".p12", ".pem", ".pfx"}
SENSITIVE_EXACT_NAMES = {
    ".env",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "auth.json",
    "cookies.json",
    "cookies.txt",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "kubeconfig",
}
SENSITIVE_COMPONENTS = {".aws", ".azure", ".gnupg", ".ssh", "credentials", "secrets"}
SENSITIVE_WORDS = {
    "credential",
    "credentials",
    "password",
    "passwords",
    "passwd",
    "secret",
    "secrets",
    "token",
    "tokens",
}


def emit(payload: Any, exit_code: int = 0) -> NoReturn:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    raise SystemExit(exit_code)


def read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        emit({"error": f"cannot read {label}: {exc}"}, 2)


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def require(condition: bool, message: str) -> None:
    if not condition:
        emit({"error": message}, 2)


def policy_stop(
    state_path: Path, state: dict[str, Any], phase: str, reason: str, **details: Any
) -> NoReturn:
    state["status"] = "paused"
    state["phase"] = phase
    state["exit_reason"] = reason
    state["history"].append({"event": "policy_stop", "phase": phase, "reason": reason, **details})
    atomic_write(state_path, state)
    emit(
        {
            "delivery_id": state["delivery_id"],
            "status": state["status"],
            "phase": state["phase"],
            "reason": reason,
            **details,
        },
        POLICY_EXIT,
    )


def run_git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"Git command failed: {' '.join(args)}: {exc}") from exc


def run_git_input(root: Path, data: bytes, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            input=data,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"Git command failed: {' '.join(args)}: {exc}") from exc


def git_predicate(root: Path, *args: str) -> bool:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise ValueError(f"Git command failed: {' '.join(args)}: {exc}") from exc
    if completed.returncode not in {0, 1}:
        raise ValueError(
            f"Git command failed: {' '.join(args)}: exit {completed.returncode}"
        )
    return completed.returncode == 0


def resolve_git_root(candidate: Path) -> Path:
    root = candidate.resolve()
    if not root.is_dir():
        raise ValueError(f"project root is not a directory: {root}")
    discovered = Path(
        os.fsdecode(run_git(root, "rev-parse", "--show-toplevel")).strip()
    ).resolve()
    if discovered != root:
        raise ValueError(f"project root must be the Git root: expected {discovered}, got {root}")
    return root


def current_head(root: Path) -> str:
    return os.fsdecode(run_git(root, "rev-parse", "HEAD")).strip()


def decode_z_paths(raw: bytes) -> list[str]:
    return [
        os.fsdecode(item)
        for item in raw.split(b"\0")
        if item
    ]


def literal_pathspec(relative: str) -> str:
    return f":(literal){relative}"


def changed_gitlink_paths(root: Path, paths: list[str]) -> list[str]:
    if not paths:
        return []
    gitlinks: set[str] = set()
    index_raw = run_git(
        root,
        "ls-files",
        "--stage",
        "-z",
        "--",
        *(literal_pathspec(path) for path in paths),
    )
    for record in index_raw.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        fields = os.fsdecode(metadata).split()
        if separator and len(fields) == 3 and fields[0] == "160000":
            gitlinks.add(os.fsdecode(raw_path))

    head_raw = run_git(
        root,
        "ls-tree",
        "-z",
        "HEAD",
        "--",
        *(literal_pathspec(path) for path in paths),
    )
    for record in head_raw.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        fields = os.fsdecode(metadata).split()
        if separator and len(fields) == 3 and fields[0] == "160000":
            gitlinks.add(os.fsdecode(raw_path))
    return sorted(gitlinks)


def is_sensitive_path(relative: str) -> bool:
    path = PurePosixPath(relative)
    lower_parts = [part.lower() for part in path.parts]
    name = path.name.lower()
    stem_words = set(re.split(r"[^a-z0-9]+", path.stem.lower()))
    if name in SENSITIVE_EXACT_NAMES or name.startswith(".env."):
        return True
    if path.suffix.lower() in SENSITIVE_EXTENSIONS:
        return True
    if any(part in SENSITIVE_COMPONENTS for part in lower_parts):
        return True
    if stem_words & SENSITIVE_WORDS:
        return True
    return bool(
        stem_words & {"key", "keys"}
        and stem_words & {"access", "api", "private", "secret", "secrets"}
    )


def diff_snapshot(root: Path) -> dict[str, Any]:
    staged = decode_z_paths(
        run_git(
            root,
            "diff",
            "--cached",
            "--no-renames",
            "--name-only",
            "-z",
            "HEAD",
            "--",
        )
    )
    unstaged = decode_z_paths(
        run_git(root, "diff", "--no-renames", "--name-only", "-z", "--")
    )
    untracked = decode_z_paths(
        run_git(root, "ls-files", "--others", "--exclude-standard", "-z", "--")
    )
    paths = sorted(set(staged + unstaged + untracked))
    sensitive = [path for path in paths if is_sensitive_path(path)]
    if sensitive:
        raise ValueError(
            "changed scope contains sensitive-looking paths; refuse to hash or review: "
            + ", ".join(sensitive)
        )
    gitlinks = changed_gitlink_paths(root, paths)
    if gitlinks:
        raise ValueError(
            "changed submodule paths are unsupported by this local-diff supervisor: "
            + ", ".join(gitlinks)
        )

    tracked_paths = sorted(set(staged + unstaged))
    tracked_worktree = {
        relative: item["worktree"]
        for relative, item in build_ready_manifest(root, tracked_paths).items()
    }

    digest = hashlib.sha256()
    digest.update(current_head(root).encode("ascii"))
    digest.update(b"\0tracked-worktree-manifest\0")
    digest.update(
        json.dumps(
            tracked_worktree,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    digest.update(b"\0staged-diff\0")
    digest.update(
        run_git(root, "diff", "--cached", "--no-renames", "--binary", "HEAD", "--")
    )
    digest.update(b"\0unstaged-diff\0")
    digest.update(run_git(root, "diff", "--no-renames", "--binary", "--"))
    digest.update(b"\0untracked\0")

    for relative in untracked:
        absolute = root / relative
        try:
            info = absolute.lstat()
        except OSError as exc:
            raise ValueError(f"cannot inspect untracked path {relative}: {exc}") from exc
        digest.update(relative.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        if stat.S_ISLNK(info.st_mode):
            digest.update(b"120000")
            digest.update(b"\0")
            digest.update(os.fsencode(os.readlink(absolute)))
            digest.update(b"\0")
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"untracked path must be a regular file or symlink: {relative}")
        digest.update(
            ("100755" if info.st_mode & 0o111 else "100644").encode("ascii")
        )
        digest.update(b"\0")
        with absolute.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")

    return {"hash": digest.hexdigest(), "paths": paths}


def index_manifest(root: Path, paths: list[str]) -> dict[str, list[dict[str, str]]]:
    result = {path: [] for path in paths}
    if not paths:
        return result
    raw = run_git(
        root,
        "ls-files",
        "--stage",
        "-z",
        "--",
        *(literal_pathspec(path) for path in paths),
    )
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise ValueError("cannot parse Git index manifest")
        fields = os.fsdecode(metadata).split()
        if len(fields) != 3:
            raise ValueError("cannot parse Git index entry")
        relative = os.fsdecode(raw_path)
        if relative in result:
            result[relative].append(
                {"mode": fields[0], "oid": fields[1], "stage": fields[2]}
            )
    for entries in result.values():
        entries.sort(key=lambda item: (item["stage"], item["mode"], item["oid"]))
    return result


def build_ready_manifest(root: Path, paths: list[str]) -> dict[str, dict[str, Any]]:
    manifest: dict[str, dict[str, Any]] = {}
    indexed = index_manifest(root, paths)
    for relative in paths:
        require_no_symlink_parents(root, relative)
        absolute = root / relative
        if absolute.is_symlink():
            content = os.readlink(absolute).encode("utf-8", "surrogateescape")
            oid = os.fsdecode(
                run_git_input(root, content, "hash-object", "--stdin")
            ).strip()
            manifest[relative] = {
                "index": indexed[relative],
                "worktree": {
                    "deleted": False,
                    "mode": "120000",
                    "oid": oid,
                    "git_oid": oid,
                },
            }
            continue
        if not absolute.exists():
            manifest[relative] = {
                "index": indexed[relative],
                "worktree": {
                    "deleted": True,
                    "mode": None,
                    "oid": None,
                    "git_oid": None,
                },
            }
            continue
        info = absolute.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"READY path must be a regular file or symlink: {relative}")
        content = absolute.read_bytes()
        raw_oid = os.fsdecode(
            run_git_input(root, content, "hash-object", "--stdin")
        ).strip()
        git_oid = os.fsdecode(
            run_git_input(
                root,
                content,
                "hash-object",
                "--path",
                relative,
                "--stdin",
            )
        ).strip()
        manifest[relative] = {
            "index": indexed[relative],
            "worktree": {
                "deleted": False,
                "mode": "100755" if info.st_mode & 0o111 else "100644",
                "oid": raw_oid,
                "git_oid": git_oid,
            },
        }
    return manifest


def changed_manifest_paths(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> list[str]:
    return sorted(
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    )


def normalize_items(
    raw: Any,
    *,
    label: str,
    prefix: str,
    default_items: list[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    if raw is None:
        raw = [
            {"id": item_id, "description": description}
            for item_id, description in (default_items or [])
        ]
    require(isinstance(raw, list) and raw, f"{label} must be a non-empty array")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, 1):
        if isinstance(item, str):
            item = {"id": f"{prefix}{index}", "description": item}
        require(isinstance(item, dict), f"{label}[{index}] must be an object or string")
        item_id = item.get("id")
        description = item.get("description")
        require(
            isinstance(item_id, str) and item_id.strip(),
            f"{label}[{index}].id must be non-empty",
        )
        require(
            isinstance(description, str) and description.strip(),
            f"{label}[{index}].description must be non-empty",
        )
        require(item_id not in seen, f"duplicate {label} id: {item_id}")
        seen.add(item_id)
        result.append({"id": item_id, "description": description})
    return result


def normalize_requirements(raw: Any) -> dict[str, Any]:
    require(isinstance(raw, dict), "requirements must be an object")
    objective = raw.get("objective")
    scope = raw.get("scope")
    require(isinstance(objective, str) and objective.strip(), "objective must be non-empty")
    require(
        isinstance(scope, list)
        and scope
        and all(isinstance(item, str) and item.strip() for item in scope),
        "scope must be a non-empty string array",
    )
    delivery_target = raw.get("delivery_target", "local_diff")
    require(
        delivery_target == "local_diff",
        "local-diff-quality-supervisor only produces a READY local diff; "
        "commit, push, PR, and merge belong to a separate handoff workflow",
    )
    raw_allowed_paths = raw.get("allowed_paths", [])
    require(
        isinstance(raw_allowed_paths, list)
        and all(isinstance(item, str) and item.strip() for item in raw_allowed_paths),
        "allowed_paths must be a string array",
    )
    allowed_paths = [
        normalize_allowed_boundary(item, "allowed_paths entry")
        for item in raw_allowed_paths
    ]
    require(len(allowed_paths) == len(set(allowed_paths)), "allowed_paths must be unique")
    unresolved = raw.get("unresolved_decisions", [])
    require(
        isinstance(unresolved, list)
        and all(isinstance(item, str) and item.strip() for item in unresolved),
        "unresolved_decisions must be a string array",
    )
    non_goals = raw.get("non_goals", [])
    authorization_boundary = raw.get("authorization_boundary", [])
    require(
        isinstance(non_goals, list)
        and all(isinstance(item, str) and item.strip() for item in non_goals),
        "non_goals must be a string array",
    )
    require(
        isinstance(authorization_boundary, list)
        and all(
            isinstance(item, str) and item.strip()
            for item in authorization_boundary
        ),
        "authorization_boundary must be a string array",
    )
    raw_thresholds = raw.get("design_review_thresholds", {})
    require(
        isinstance(raw_thresholds, dict),
        "design_review_thresholds must be an object",
    )
    threshold_defaults = {
        "high_priority_after_fix_batches": 2,
        "surface_failures": 2,
        "surface_invalidations": 3,
        "root_cause_recurrences": 2,
    }
    thresholds: dict[str, int] = {}
    for name, default in threshold_defaults.items():
        value = raw_thresholds.get(name, default)
        require(
            type(value) is int and value >= 1,
            f"design_review_thresholds.{name} must be a positive integer",
        )
        thresholds[name] = value
    normalized = {
        "original_request": raw.get("original_request", objective),
        "objective": objective,
        "scope": scope,
        "non_goals": non_goals,
        "allowed_paths": allowed_paths,
        "authorization_boundary": authorization_boundary,
        "delivery_target": delivery_target,
        "unresolved_decisions": unresolved,
        "acceptance_criteria": normalize_items(
            raw.get("acceptance_criteria"), label="acceptance_criteria", prefix="A"
        ),
        "invariants": normalize_items(
            raw.get("invariants", [{"id": "I1", "description": "No known in-scope bug remains."}]),
            label="invariants",
            prefix="I",
        ),
        "review_surfaces": normalize_items(
            raw.get("review_surfaces"),
            label="review_surfaces",
            prefix="R",
            default_items=DEFAULT_REVIEW_SURFACES,
        ),
        "required_validations": normalize_items(
            raw.get("required_validations"),
            label="required_validations",
            prefix="V",
            default_items=DEFAULT_VALIDATIONS,
        ),
        "design_review_thresholds": thresholds,
    }
    return normalized


def requirements_digest(requirements: dict[str, Any]) -> str:
    encoded = json.dumps(
        requirements, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def migrate_state(path: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Upgrade persisted state without erasing delivery history."""
    version = state.get("version")
    if version == STATE_VERSION:
        return state
    require(version in {2, 3, 4, 5, 6, 7, 8}, "unsupported or invalid state schema")
    from_version = version
    history = state.setdefault("history", [])
    historical_paths = {
        changed_path
        for event in history
        if event.get("event") == "fix_recorded"
        for changed_path in event.get("changed_paths", [])
        if isinstance(changed_path, str)
    }
    state.setdefault(
        "authorized_paths",
        sorted(set(state.get("initial_paths", [])) | historical_paths),
    )
    state["requirements"] = freeze_allowed_boundaries(
        Path(state["project_root"]),
        normalize_requirements(state.get("requirements")),
        exact_file_paths=set(state["authorized_paths"]),
    )
    state.setdefault("scope_extensions", [])
    state.setdefault(
        "derived_requirements",
        {
            f"REQ-{finding_id}": {
                "id": f"REQ-{finding_id}",
                "parent_finding_id": finding_id,
                "classification": finding["classification"],
                "description": finding["summary"],
                "status": (
                    "verified"
                    if finding["status"] == "verified"
                    else finding["status"]
                    if finding["status"] in {"fixed_unverified", "false_positive"}
                    else "open"
                ),
            }
            for finding_id, finding in state.get("findings", {}).items()
            if finding.get("classification") in FIXABLE_CLASSIFICATIONS
        },
    )
    state.setdefault("design_epoch", 1)
    state.setdefault("epoch_fix_batch_count", 0)
    state.setdefault(
        "design_review_count",
        sum(event.get("event") == "design_review_resolved" for event in history),
    )
    state.setdefault(
        "convergence_events",
        sum(
            event.get("event") == "policy_stop"
            and event.get("phase") == "design_review_required"
            for event in history
        ),
    )
    state.setdefault("deferred_design_review", None)
    state.setdefault("complete_validation_campaign", None)
    state.setdefault(
        "follow_up_requirements",
        [
            finding_id
            for finding_id, finding in state.get("findings", {}).items()
            if finding.get("status") == "follow_up"
        ],
    )
    state.setdefault("active_fix", None)
    state.setdefault("ready", False)
    state.setdefault("ready_local_diff", False)
    state.setdefault("ready_diff_hash", None)
    state.setdefault("ready_manifest", None)
    state.setdefault("exit_reason", None)
    state.setdefault("current_diff_hash", state.get("initial_diff_hash"))
    state["requirements_digest"] = requirements_digest(state["requirements"])
    state.setdefault(
        "invariants",
        {
            item["id"]: item
            for item in state.get("requirements", {}).get("invariants", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        },
    )
    state.setdefault(
        "initial_review_completed",
        any(
            event.get("event") == "review_recorded" and event.get("kind") == "initial"
            for event in history
        ),
    )
    state.setdefault(
        "review_campaign_count",
        sum(event.get("event") == "review_recorded" for event in history),
    )
    state.setdefault(
        "fix_batch_count",
        sum(event.get("event") == "fix_recorded" for event in history),
    )
    for surface in state.get("review_surfaces", {}).values():
        surface.setdefault("reviewed_diff_hash", None)
        surface.setdefault("failure_count", 0)
        surface.setdefault("invalidation_count", 0)
        surface.setdefault("epoch_failure_count", 0)
        surface.setdefault("epoch_invalidation_count", 0)
    for finding in state.get("findings", {}).values():
        finding.setdefault("recurrence_count", 0)
        finding.setdefault("epoch_recurrence_count", 0)

    if version == 2:
        state.pop("delivered", None)
        state.pop("delivery", None)

    ready_revoked = bool(state.get("ready") or state.get("ready_local_diff"))
    state["ready"] = False
    state["ready_local_diff"] = False
    state["ready_diff_hash"] = None
    state["ready_manifest"] = None
    if state.get("status") == "ready":
        state["status"] = "active"
        state["phase"] = "validation"
        state["exit_reason"] = None

    current_diff_hash = state.get("current_diff_hash")
    for finding_id, finding in state.get("findings", {}).items():
        assign_root_cause_identity(finding, finding.get("root_cause", ""))
        if finding.get("status") != "false_positive":
            continue
        evidence_hash = finding.get("evidence_diff_hash") or current_diff_hash
        finding["evidence_diff_hash"] = evidence_hash
        requirement = state.get("derived_requirements", {}).get(f"REQ-{finding_id}")
        if requirement:
            requirement["status"] = "false_positive"
            requirement["evidence"] = finding.get("evidence", "")
            requirement["evidence_diff_hash"] = evidence_hash

    reopened_active_fix = False
    active_fix = state.get("active_fix")
    if isinstance(active_fix, dict):
        for finding_id in active_fix.get("finding_ids", []):
            finding = state.get("findings", {}).get(finding_id)
            if isinstance(finding, dict) and finding.get("status") == "fixing":
                finding["status"] = "open"
        state["active_fix"] = None
        state["status"] = "active"
        state["phase"] = "remediation"
        state["exit_reason"] = None
        reopened_active_fix = True

    state["root_cause_stats"] = rebuild_root_cause_stats(state)
    state["version"] = STATE_VERSION
    state.setdefault("history", []).append(
        {
            "event": "state_migrated",
            "from_version": from_version,
            "to_version": STATE_VERSION,
            "reopened_active_fix": reopened_active_fix,
            "ready_revoked": ready_revoked,
        }
    )
    atomic_write(path, state)
    return state


def load_state(path: Path) -> dict[str, Any]:
    state = read_json(path, "state")
    require(isinstance(state, dict), "unsupported or invalid state schema")
    state = migrate_state(path, state)
    required = {
        "delivery_id",
        "project_root",
        "baseline_head",
        "initial_diff_hash",
        "initial_paths",
        "requirements",
        "status",
        "phase",
        "acceptance",
        "review_surfaces",
        "validations",
        "findings",
        "root_cause_stats",
        "history",
    }
    require(required <= state.keys(), "state is missing required fields")
    for finding_id, finding in state["findings"].items():
        assign_root_cause_identity(finding, finding.get("root_cause", ""))
        finding.setdefault(
            "root_cause_registered",
            finding.get("classification") in FIXABLE_CLASSIFICATIONS
            or f"REQ-{finding_id}" in state.get("derived_requirements", {}),
        )
    state.setdefault("deferred_design_review", None)
    state.setdefault("complete_validation_campaign", None)
    return state


def save_event(
    path: Path, state: dict[str, Any], event: str, **details: Any
) -> None:
    state["history"].append({"event": event, **details})
    atomic_write(path, state)


def state_root(state: dict[str, Any]) -> Path:
    return Path(state["project_root"])


def verify_context(
    state: dict[str, Any],
    *,
    allow_head_drift: bool = False,
) -> dict[str, Any]:
    root = resolve_git_root(state_root(state))
    head = current_head(root)
    if not allow_head_drift and head != state["baseline_head"]:
        emit(
            {
                "error": "HEAD drifted from the delivery baseline",
                "expected": state["baseline_head"],
                "actual": head,
            },
            POLICY_EXIT,
        )
    try:
        snapshot = diff_snapshot(root)
    except ValueError as exc:
        emit({"error": str(exc)}, POLICY_EXIT)
    return {"head": head, **snapshot}


def capture_stable_manifest(
    state: dict[str, Any],
    paths: list[str],
    *,
    label: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    before = verify_context(state)
    try:
        manifest = build_ready_manifest(state_root(state), paths)
    except (OSError, ValueError) as exc:
        emit({"error": f"cannot capture {label} manifest: {exc}"}, POLICY_EXIT)
    after = verify_context(state)
    require(
        before["head"] == after["head"]
        and before["hash"] == after["hash"]
        and before["paths"] == after["paths"],
        f"local diff changed while capturing {label} manifest; retry from current evidence",
    )
    return after, manifest


def normalize_allowed_boundary(relative: Any, label: str) -> str:
    require(isinstance(relative, str) and relative.strip(), f"{label} must be non-empty")
    is_directory = relative.endswith("/")
    literal = relative[:-1] if is_directory else relative
    normalized = normalize_literal_path(literal, label)
    return normalized + "/" if is_directory else normalized


def freeze_allowed_boundaries(
    root: Path,
    requirements: dict[str, Any],
    *,
    exact_file_paths: set[str],
) -> dict[str, Any]:
    frozen = []
    for allowed in requirements["allowed_paths"]:
        explicit_directory = allowed.endswith("/")
        literal = allowed.rstrip("/")
        existing_directory = (
            literal not in exact_file_paths and (root / literal).is_dir()
        )
        frozen.append(literal + "/" if explicit_directory or existing_directory else literal)
    requirements["allowed_paths"] = frozen
    return requirements


def allowed_path(relative: str, allowed_paths: list[str]) -> bool:
    if not allowed_paths:
        return True
    path = PurePosixPath(relative)
    for allowed in allowed_paths:
        is_directory = allowed.endswith("/")
        candidate = PurePosixPath(allowed.rstrip("/"))
        if path == candidate or (is_directory and candidate in path.parents):
            return True
    return False


def normalize_literal_path(relative: Any, label: str) -> str:
    require(isinstance(relative, str) and relative.strip(), f"{label} must be non-empty")
    require(relative == relative.strip(), f"{label} must not contain surrounding whitespace")
    path = PurePosixPath(relative)
    require(not path.is_absolute(), f"{label} must be repository-relative")
    require(
        relative == path.as_posix()
        and ".." not in path.parts
        and "." not in path.parts,
        f"{label} must be a normalized literal file path",
    )
    require(
        not any(character in relative for character in "*?[]"),
        f"{label} must not be a Git pathspec or glob",
    )
    require(not relative.startswith(":"), f"{label} must not use Git pathspec magic")
    require(not is_sensitive_path(relative), f"{label} must not be sensitive-looking")
    return relative


def require_no_symlink_parents(root: Path, relative: str) -> None:
    current = root
    for component in PurePosixPath(relative).parts[:-1]:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(
                f"path traverses a symlinked parent outside the literal Git tree: {relative}"
            )


def require_no_symlink_endpoints(root: Path, paths: list[str], label: str) -> None:
    symlinks = sorted(relative for relative in paths if (root / relative).is_symlink())
    require(
        not symlinks,
        f"{label} must not contain symlink endpoints: " + ", ".join(symlinks),
    )


def require_git_visible_scope_path(root: Path, relative: str) -> None:
    if ".git" in PurePosixPath(relative).parts:
        raise ValueError(f"scope path must not target Git metadata: {relative}")
    tracked = git_predicate(
        root,
        "ls-files",
        "--error-unmatch",
        "--",
        literal_pathspec(relative),
    )
    ignored = git_predicate(root, "check-ignore", "--quiet", "--no-index", "--", relative)
    if not tracked and ignored:
        raise ValueError(
            f"scope path is ignored and absent from the supervised Git diff: {relative}"
        )


def manifest_matches_head(
    root: Path,
    relative: str,
    manifest: dict[str, Any],
) -> bool:
    raw = run_git(root, "ls-tree", "-z", "HEAD", "--", literal_pathspec(relative))
    if not raw:
        worktree = manifest.get("worktree", {})
        return manifest.get("index") == [] and worktree.get("deleted") is True
    record = raw.rstrip(b"\0")
    metadata, separator, raw_path = record.partition(b"\t")
    fields = os.fsdecode(metadata).split()
    if not separator or os.fsdecode(raw_path) != relative or len(fields) != 3:
        raise ValueError(f"cannot parse HEAD manifest for path: {relative}")
    mode, object_type, oid = fields
    if object_type != "blob":
        return False
    worktree = manifest.get("worktree", {})
    return (
        manifest.get("index") == [{"mode": mode, "oid": oid, "stage": "0"}]
        and worktree.get("deleted") is False
        and worktree.get("mode") == mode
        and worktree.get("git_oid") == oid
    )


def worktree_entry_metadata_manifest(root: Path) -> dict[str, dict[str, Any]]:
    manifest: dict[str, dict[str, Any]] = {}

    def raise_walk_error(error: OSError) -> None:
        raise error

    for directory, dirnames, filenames in os.walk(
        root, followlinks=False, onerror=raise_walk_error
    ):
        directory_path = Path(directory)
        if directory_path == root and ".git" in dirnames:
            dirnames.remove(".git")
        entries = list(filenames)
        for name in list(dirnames):
            candidate = directory_path / name
            if candidate.is_symlink():
                dirnames.remove(name)
                entries.append(name)
        for name in entries:
            candidate = directory_path / name
            relative = candidate.relative_to(root).as_posix()
            if relative == ".git":
                continue
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            item: dict[str, Any] = {
                "file_type": stat.S_IFMT(info.st_mode),
                "permissions": stat.S_IMODE(info.st_mode),
                "uid": info.st_uid,
                "gid": info.st_gid,
                "device": info.st_dev,
                "inode": info.st_ino,
                "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
                "ctime_ns": info.st_ctime_ns,
                "flags": getattr(info, "st_flags", 0),
                "generation": getattr(info, "st_gen", 0),
            }
            if stat.S_ISLNK(info.st_mode):
                item["link_target_sha256"] = hashlib.sha256(
                    os.fsencode(os.readlink(candidate))
                ).hexdigest()
            manifest[relative] = item
    return manifest


def changed_worktree_entries(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    allowed_paths: list[str],
) -> list[str]:
    allowed = set(allowed_paths)
    return sorted(
        relative
        for relative in set(before) | set(after)
        if relative not in allowed and before.get(relative) != after.get(relative)
    )


def worktree_directory_manifest(root: Path) -> dict[str, dict[str, int]]:
    manifest: dict[str, dict[str, int]] = {}
    for directory, dirnames, _ in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        if directory_path == root and ".git" in dirnames:
            dirnames.remove(".git")
        dirnames[:] = [
            name for name in dirnames if not (directory_path / name).is_symlink()
        ]
        try:
            info = directory_path.lstat()
        except FileNotFoundError:
            continue
        relative = (
            "." if directory_path == root else directory_path.relative_to(root).as_posix()
        )
        manifest[relative] = {
            "permissions": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid,
            "gid": info.st_gid,
            "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns,
        }
    return manifest


def authorized_directory_ancestors(allowed_paths: list[str]) -> set[str]:
    ancestors = {"."}
    for relative in allowed_paths:
        parent = PurePosixPath(relative).parent
        while parent != PurePosixPath("."):
            ancestors.add(parent.as_posix())
            parent = parent.parent
    return ancestors


def changed_worktree_directories(
    before: dict[str, dict[str, int]],
    after: dict[str, dict[str, int]],
    allowed_paths: list[str],
) -> list[str]:
    authorized_ancestors = authorized_directory_ancestors(allowed_paths)
    changed: set[str] = set()
    structural_fields = {"permissions", "uid", "gid"}
    timestamp_fields = {"mtime_ns", "ctime_ns"}
    for relative in set(before) | set(after):
        old = before.get(relative)
        new = after.get(relative)
        if old is None or new is None:
            if relative not in authorized_ancestors:
                changed.add(relative)
            continue
        if any(old.get(field) != new.get(field) for field in structural_fields):
            changed.add(relative)
            continue
        if relative not in authorized_ancestors and any(
            old.get(field) != new.get(field) for field in timestamp_fields
        ):
            changed.add(relative)
    return sorted(changed)


def git_control_roots(root: Path) -> list[tuple[str, Path, Path | None]]:
    git_dir = Path(os.fsdecode(run_git(root, "rev-parse", "--absolute-git-dir")).strip())
    raw_common_dir = Path(
        os.fsdecode(run_git(root, "rev-parse", "--git-common-dir")).strip()
    )
    common_dir = (
        raw_common_dir
        if raw_common_dir.is_absolute()
        else (root / raw_common_dir).resolve()
    )
    if common_dir == git_dir:
        return [("<git-dir>", git_dir, None)]
    return [
        ("<git-dir>", git_dir, None),
        ("<git-common-dir>", common_dir, git_dir),
    ]


def git_control_manifest(root: Path) -> dict[str, dict[str, Any]]:
    manifest: dict[str, dict[str, Any]] = {}
    for label, control_root, excluded_subtree in git_control_roots(root):
        if not control_root.is_dir():
            continue
        for directory, dirnames, filenames in os.walk(control_root, followlinks=False):
            directory_path = Path(directory)
            if directory_path == control_root and "objects" in dirnames:
                dirnames.remove("objects")
            if excluded_subtree is not None:
                dirnames[:] = [
                    name
                    for name in dirnames
                    if (directory_path / name) != excluded_subtree
                ]
            entries = list(filenames)
            for name in list(dirnames):
                candidate = directory_path / name
                if candidate.is_symlink():
                    dirnames.remove(name)
                    entries.append(name)
            for name in entries:
                candidate = directory_path / name
                if excluded_subtree is not None and (
                    candidate == excluded_subtree
                    or excluded_subtree in candidate.parents
                ):
                    continue
                relative = candidate.relative_to(control_root).as_posix()
                if relative == "index" or relative.startswith("objects/"):
                    continue
                try:
                    info = candidate.lstat()
                    digest = hashlib.sha256()
                    if stat.S_ISLNK(info.st_mode):
                        digest.update(os.fsencode(os.readlink(candidate)))
                    elif stat.S_ISREG(info.st_mode):
                        with candidate.open("rb") as handle:
                            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                                digest.update(chunk)
                    else:
                        digest.update(b"non-regular")
                except FileNotFoundError:
                    continue
                manifest[f"{label}/{relative}"] = {
                    "file_type": stat.S_IFMT(info.st_mode),
                    "permissions": stat.S_IMODE(info.st_mode),
                    "size": info.st_size,
                    "ctime_ns": info.st_ctime_ns,
                    "sha256": digest.hexdigest(),
                }
    return manifest


def frozen_batch_boundary_changes(
    state: dict[str, Any], active: dict[str, Any]
) -> list[str]:
    entry_manifest_before = active.get("worktree_entry_manifest_before")
    require(
        isinstance(entry_manifest_before, dict),
        "active fix predates the worktree-entry manifest; restart the frozen batch",
    )
    outside_batch_writes = changed_worktree_entries(
        entry_manifest_before,
        worktree_entry_metadata_manifest(state_root(state)),
        active["authorized_paths"],
    )
    ignored_path_changes = (
        set(active["ignored_paths_before"])
        ^ set(ignored_worktree_paths(state_root(state)))
    ) - set(active["authorized_paths"])
    directory_manifest_before = active.get("worktree_directory_manifest_before")
    require(
        isinstance(directory_manifest_before, dict),
        "active fix predates the worktree-directory manifest; restart the frozen batch",
    )
    directory_changes = changed_worktree_directories(
        directory_manifest_before,
        worktree_directory_manifest(state_root(state)),
        active["authorized_paths"],
    )
    git_control_before = active.get("git_control_manifest_before")
    require(
        isinstance(git_control_before, dict),
        "active fix predates the Git-control manifest; restart the frozen batch",
    )
    git_control_after = git_control_manifest(state_root(state))
    git_control_changes = {
        path
        for path in set(git_control_before) | set(git_control_after)
        if git_control_before.get(path) != git_control_after.get(path)
    }
    return sorted(
        set(outside_batch_writes)
        | ignored_path_changes
        | set(directory_changes)
        | git_control_changes
    )


def ignored_worktree_paths(root: Path) -> list[str]:
    return sorted(
        decode_z_paths(
            run_git(
                root,
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "-z",
                "--",
            )
        )
    )


def check_scope(state: dict[str, Any], paths: list[str]) -> None:
    authorized = set(state["authorized_paths"])
    outside = [
        path
        for path in paths
        if path not in authorized
    ]
    require(
        not outside,
        "changed paths require an approved extend-scope transition: " + ", ".join(outside),
    )


def remediable_findings(state: dict[str, Any]) -> list[str]:
    return [
        finding_id
        for finding_id, finding in state["findings"].items()
        if finding["status"] == "open"
    ]


def verification_pending_findings(state: dict[str, Any]) -> list[str]:
    return [
        finding_id
        for finding_id, finding in state["findings"].items()
        if finding["status"] == "fixed_unverified"
        or (
            finding["status"] == "verified"
            and finding.get("verified_diff_hash") != state["current_diff_hash"]
        )
    ]


def preflight_checklist(surface_ids: set[str]) -> list[dict[str, str]]:
    check_ids = {"changed-path-integrity"}
    for check_id, mapped_surfaces in PREFLIGHT_SURFACE_MAP.items():
        if surface_ids & mapped_surfaces:
            check_ids.add(check_id)
    return [
        {"id": check_id, "description": PREFLIGHT_CHECKS[check_id]}
        for check_id in sorted(check_ids)
    ]


def is_test_evidence_path(relative: str) -> bool:
    path = PurePosixPath(relative)
    parent_parts = {part.casefold() for part in path.parts[:-1]}
    name = path.name.casefold()
    return bool(
        parent_parts & {"test", "tests", "__tests__"}
        or ".test." in name
        or ".spec." in name
        or (name.endswith(".snap") and "__snapshots__" in parent_parts)
    )


def normalized_root_cause(root_cause: str) -> str:
    return " ".join(root_cause.casefold().split())


def root_cause_key(root_cause: str) -> str:
    return hashlib.sha256(normalized_root_cause(root_cause).encode("utf-8")).hexdigest()


def assign_root_cause_identity(
    finding: dict[str, Any], root_cause: str
) -> None:
    require(
        isinstance(root_cause, str) and root_cause.strip(),
        "finding root_cause must be non-empty",
    )
    finding["root_cause"] = root_cause.strip()
    finding["normalized_root_cause"] = normalized_root_cause(root_cause)
    finding["root_cause_key"] = root_cause_key(root_cause)


def finding_root_cause_key(finding: dict[str, Any]) -> str:
    key = finding.get("root_cause_key")
    if isinstance(key, str) and key:
        return key
    root_cause = finding.get("root_cause", "")
    assign_root_cause_identity(finding, root_cause)
    return finding["root_cause_key"]


def current_epoch_finding_occurrences(state: dict[str, Any]) -> dict[str, int]:
    history = state.get("history", [])
    start = 0
    current_epoch = state.get("design_epoch")
    for index, event in enumerate(history):
        if (
            event.get("event") == "design_review_resolved"
            and event.get("design_epoch") == current_epoch
        ):
            start = index + 1

    occurrences: dict[str, int] = {}
    for event in history[start:]:
        event_name = event.get("event")
        if event_name in {"review_recorded", "findings_added"}:
            finding_ids = event.get("added", [])
        elif event_name == "validation_recorded":
            finding_ids = event.get("added_findings", [])
        else:
            continue
        if not isinstance(finding_ids, list):
            continue
        for finding_id in finding_ids:
            if isinstance(finding_id, str):
                occurrences[finding_id] = occurrences.get(finding_id, 0) + 1
    for finding_id, count in list(occurrences.items()):
        finding = state.get("findings", {}).get(finding_id)
        if not isinstance(finding, dict):
            continue
        maximum = 1 + max(0, int(finding.get("epoch_recurrence_count", 0)))
        occurrences[finding_id] = min(count, maximum)
    return occurrences


def rebuild_root_cause_stats(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for finding in state.get("findings", {}).values():
        if finding.get("classification") not in FIXABLE_CLASSIFICATIONS:
            continue
        root_cause = finding.get("root_cause")
        if not isinstance(root_cause, str) or not root_cause.strip():
            continue
        key = finding_root_cause_key(finding)
        item = stats.setdefault(
            key,
            {
                "root_cause": normalized_root_cause(root_cause),
                "occurrence_count": 0,
                "recurrence_count": 0,
                "epoch_occurrence_count": 0,
                "epoch_recurrence_count": 0,
            },
        )
        occurrences = 1 + max(0, int(finding.get("recurrence_count", 0)))
        item["occurrence_count"] += occurrences
        item["recurrence_count"] = max(0, item["occurrence_count"] - 1)
    history_occurrences: dict[str, int] = {}
    for finding_id, count in current_epoch_finding_occurrences(state).items():
        finding = state.get("findings", {}).get(finding_id)
        if (
            not isinstance(finding, dict)
            or finding.get("classification") not in FIXABLE_CLASSIFICATIONS
        ):
            continue
        root_cause = finding.get("root_cause")
        if isinstance(root_cause, str) and root_cause.strip():
            key = finding_root_cause_key(finding)
            history_occurrences[key] = history_occurrences.get(key, 0) + count

    legacy_epoch_recurrences: dict[str, int] = {}
    for finding in state.get("findings", {}).values():
        if finding.get("classification") not in FIXABLE_CLASSIFICATIONS:
            continue
        root_cause = finding.get("root_cause")
        if not isinstance(root_cause, str) or not root_cause.strip():
            continue
        key = finding_root_cause_key(finding)
        legacy_epoch_recurrences[key] = (
            legacy_epoch_recurrences.get(key, 0)
            + max(0, int(finding.get("epoch_recurrence_count", 0)))
        )

    for key, item in stats.items():
        legacy_recurrences = legacy_epoch_recurrences.get(key, 0)
        legacy_occurrences = legacy_recurrences + 1 if legacy_recurrences else 0
        item["epoch_occurrence_count"] = max(
            history_occurrences.get(key, 0),
            legacy_occurrences,
        )
        item["epoch_recurrence_count"] = max(
            0,
            item["epoch_occurrence_count"] - 1,
        )
    return stats


def register_root_cause_occurrence(state: dict[str, Any], root_cause: str) -> None:
    key = root_cause_key(root_cause)
    item = state["root_cause_stats"].setdefault(
        key,
        {
            "root_cause": normalized_root_cause(root_cause),
            "occurrence_count": 0,
            "recurrence_count": 0,
            "epoch_occurrence_count": 0,
            "epoch_recurrence_count": 0,
        },
    )
    item["occurrence_count"] += 1
    item["recurrence_count"] = max(0, item["occurrence_count"] - 1)
    item["epoch_occurrence_count"] += 1
    item["epoch_recurrence_count"] = max(0, item["epoch_occurrence_count"] - 1)


def finding_fingerprint(finding: dict[str, Any]) -> str:
    material = "|".join(
        [
            finding.get("classification", ""),
            finding.get("location", ""),
            finding_root_cause_key(finding),
            finding.get("trigger", ""),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def normalize_finding(raw: Any, surface_ids: set[str]) -> dict[str, Any]:
    require(isinstance(raw, dict), "finding must be an object")
    required_strings = ["id", "classification", "severity", "summary", "trigger", "root_cause"]
    for key in required_strings:
        require(
            isinstance(raw.get(key), str) and raw[key].strip(),
            f"finding.{key} must be non-empty",
        )
    finding_id = raw["id"]
    classification = raw["classification"]
    require(classification in ALL_CLASSIFICATIONS, f"invalid classification: {classification}")
    require(raw["severity"] in SEVERITIES, f"invalid severity: {raw['severity']}")
    affected = raw.get("affected_surfaces", [])
    require(
        isinstance(affected, list)
        and affected
        and all(isinstance(item, str) and item in surface_ids for item in affected),
        "finding.affected_surfaces must reference known review surfaces",
    )
    location = raw.get("location", "")
    require(isinstance(location, str), "finding.location must be a string")
    evidence = raw.get("evidence", "")
    require(isinstance(evidence, str), "finding.evidence must be a string")

    if classification in FOLLOW_UP_CLASSIFICATIONS:
        status = "follow_up"
    elif classification == "false_positive":
        require(evidence.strip(), "false_positive finding requires evidence")
        status = "false_positive"
    elif classification == "decision_required":
        status = "decision_required"
    else:
        status = "open"

    finding = {
        "id": finding_id,
        "classification": classification,
        "severity": raw["severity"],
        "summary": raw["summary"],
        "trigger": raw["trigger"],
        "location": location,
        "affected_surfaces": sorted(set(affected)),
        "evidence": evidence,
        "status": status,
        "source": raw.get("source", "review"),
        "recurrence_count": 0,
        "epoch_recurrence_count": 0,
        "root_cause_registered": False,
    }
    assign_root_cause_identity(finding, raw["root_cause"])
    finding["fingerprint"] = finding_fingerprint(finding)
    return finding


def add_findings(
    state: dict[str, Any], raw_findings: Any
) -> tuple[list[str], list[str]]:
    require(isinstance(raw_findings, list), "findings must be an array")
    surface_ids = set(state["review_surfaces"])
    existing_ids = set(state["findings"])
    existing_fingerprints = {
        item["fingerprint"]: item_id for item_id, item in state["findings"].items()
    }
    added: list[str] = []
    duplicates: list[str] = []
    seen_report_ids: set[str] = set()
    for raw in raw_findings:
        finding = normalize_finding(raw, surface_ids)
        require(
            finding["id"] not in seen_report_ids,
            f"duplicate finding id in one report: {finding['id']}",
        )
        seen_report_ids.add(finding["id"])
        duplicate_id = existing_fingerprints.get(finding["fingerprint"])
        if finding["id"] in existing_ids:
            require(
                duplicate_id == finding["id"],
                f"existing finding id has different fingerprint: {finding['id']}",
            )
        if duplicate_id:
            duplicates.append(duplicate_id)
            existing = state["findings"][duplicate_id]
            previous_surfaces = set(existing["affected_surfaces"])
            merged_surfaces = previous_surfaces | set(finding["affected_surfaces"])
            existing["affected_surfaces"] = sorted(merged_surfaces)
            if (
                finding["classification"] in FIXABLE_CLASSIFICATIONS
                and existing["status"]
                in {"verified", "fixed_unverified", "false_positive"}
            ):
                register_root_cause_occurrence(state, finding["root_cause"])
                existing["status"] = "open"
                existing["recurrence_count"] = existing.get("recurrence_count", 0) + 1
                existing["epoch_recurrence_count"] = (
                    existing.get("epoch_recurrence_count", 0) + 1
                )
                existing["evidence"] = finding["evidence"]
                existing["evidence_diff_hash"] = None
                existing["source"] = finding["source"]
                requirement = state["derived_requirements"].get(
                    f"REQ-{duplicate_id}"
                )
                if requirement:
                    requirement["status"] = "open"
                added.append(duplicate_id)
            elif (
                merged_surfaces != previous_surfaces
                and existing["status"]
                in {"open", "decision_required", "blocked"}
            ):
                added.append(duplicate_id)
            continue
        state["findings"][finding["id"]] = finding
        if finding["status"] == "false_positive":
            finding["evidence_diff_hash"] = state["current_diff_hash"]
        existing_ids.add(finding["id"])
        existing_fingerprints[finding["fingerprint"]] = finding["id"]
        added.append(finding["id"])
        if finding["status"] == "follow_up":
            state["follow_up_requirements"].append(finding["id"])
        elif finding["classification"] in FIXABLE_CLASSIFICATIONS:
            register_root_cause_occurrence(state, finding["root_cause"])
            finding["root_cause_registered"] = True
            state["derived_requirements"][f"REQ-{finding['id']}"] = {
                "id": f"REQ-{finding['id']}",
                "parent_finding_id": finding["id"],
                "classification": finding["classification"],
                "description": finding["summary"],
                "status": "open",
            }
    return added, duplicates


def open_blocking_findings(state: dict[str, Any]) -> list[str]:
    """All finding states that prevent READY, regardless of next action."""
    return [
        finding_id
        for finding_id, finding in state["findings"].items()
        if finding["status"]
        in {"open", "fixing", "fixed_unverified", "decision_required", "blocked"}
    ]


def decision_findings(state: dict[str, Any]) -> list[str]:
    return [
        finding_id
        for finding_id, finding in state["findings"].items()
        if finding["status"] == "decision_required"
    ]


def root_cause_threshold_reached(state: dict[str, Any]) -> bool:
    threshold = state["requirements"]["design_review_thresholds"][
        "root_cause_recurrences"
    ]
    return any(
        item.get("epoch_recurrence_count", 0) >= threshold
        for item in state["root_cause_stats"].values()
    )


def invalidate_finding_surfaces(
    state: dict[str, Any], finding_ids: list[str]
) -> list[str]:
    invalidated: set[str] = set()
    for finding_id in finding_ids:
        finding = state["findings"][finding_id]
        if finding["status"] not in {"open", "decision_required", "blocked"}:
            continue
        for surface_id in finding["affected_surfaces"]:
            surface = state["review_surfaces"][surface_id]
            if surface["status"] != "invalidated":
                surface["invalidation_count"] += 1
                surface["epoch_invalidation_count"] += 1
            surface["status"] = "invalidated"
            surface["evidence"] = ""
            surface["reviewed_diff_hash"] = None
            invalidated.add(surface_id)
    if invalidated:
        state["complete_validation_campaign"] = None
    return sorted(invalidated)


def hard_gate(state: dict[str, Any]) -> str | None:
    if state["status"] == "paused" and state["phase"] in {
        "decision_required",
        "design_review_required",
        "validation_failed",
        "blocked",
    }:
        return state["phase"]
    return None


def set_active_phase(state: dict[str, Any], fallback: str) -> None:
    """Route active work without bypassing a persisted hard gate."""
    if hard_gate(state):
        return
    state["status"] = "active"
    if decision_findings(state) or state["requirements"]["unresolved_decisions"]:
        state["status"] = "paused"
        state["phase"] = "decision_required"
    elif state["active_fix"] is not None:
        state["phase"] = "fixing"
    elif remediable_findings(state):
        state["phase"] = "remediation"
    elif any(
        item["status"] in {"invalidated", "failed", "blocked"}
        for item in state["review_surfaces"].values()
    ):
        state["phase"] = "impact_review"
    elif verification_pending_findings(state):
        state["phase"] = "validation"
    else:
        state["phase"] = fallback


def defer_design_review(
    state: dict[str, Any], reason: str, *, added_findings: list[str]
) -> None:
    pending = state.get("deferred_design_review")
    if not isinstance(pending, dict):
        pending = {"reasons": [], "added_findings": []}
    legacy_reason = pending.pop("reason", None)
    reasons = set(pending.get("reasons", []))
    if isinstance(legacy_reason, str) and legacy_reason:
        reasons.add(legacy_reason)
    reasons.add(reason)
    pending["reasons"] = sorted(reasons)
    pending["added_findings"] = sorted(
        set(pending.get("added_findings", [])) | set(added_findings)
    )
    state["deferred_design_review"] = pending


def route_hard_gate_and_convergence(
    state_path: Path,
    state: dict[str, Any],
    *,
    existing_gate: str | None = None,
    immediate_phase: str | None = None,
    immediate_reason: str | None = None,
    immediate_details: dict[str, Any] | None = None,
    convergence_reason: str | None = None,
    convergence_details: dict[str, Any] | None = None,
    added_findings: list[str] | None = None,
) -> None:
    """Preserve correctness gates and defer design convergence behind them."""
    immediate_details = immediate_details or {}
    convergence_details = convergence_details or {}
    added_findings = added_findings or []
    if existing_gate in {"design_review_required", "blocked"}:
        return
    if convergence_reason:
        state["convergence_events"] += 1
        if existing_gate or immediate_phase:
            defer_design_review(
                state,
                convergence_reason,
                added_findings=added_findings,
            )
    if existing_gate:
        return
    if immediate_phase:
        require(
            isinstance(immediate_reason, str) and immediate_reason,
            "immediate hard gate requires a reason",
        )
        policy_stop(
            state_path,
            state,
            immediate_phase,
            immediate_reason,
            **immediate_details,
        )
    if convergence_reason:
        policy_stop(
            state_path,
            state,
            "design_review_required",
            convergence_reason,
            **convergence_details,
        )


def enforce_deferred_design_review(
    state_path: Path, state: dict[str, Any], *, cleared_gate: str
) -> None:
    pending = state.get("deferred_design_review")
    if not isinstance(pending, dict):
        return
    deferred_reasons = pending.get("reasons", [])
    if not deferred_reasons and isinstance(pending.get("reason"), str):
        deferred_reasons = [pending["reason"]]
    state["deferred_design_review"] = None
    policy_stop(
        state_path,
        state,
        "design_review_required",
        "deferred_design_review_after_hard_gate",
        cleared_gate=cleared_gate,
        deferred_reasons=deferred_reasons,
        added_findings=pending.get("added_findings", []),
    )


def summarize(state: dict[str, Any], snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    result = {
        "delivery_id": state["delivery_id"],
        "status": state["status"],
        "phase": state["phase"],
        "baseline_head": state["baseline_head"],
        "current_diff_hash": state["current_diff_hash"],
        "fix_batch_count": state["fix_batch_count"],
        "epoch_fix_batch_count": state["epoch_fix_batch_count"],
        "design_review_count": state["design_review_count"],
        "design_epoch": state["design_epoch"],
        "initial_review_completed": state["initial_review_completed"],
        "open_findings": open_blocking_findings(state),
        "remediable_findings": remediable_findings(state),
        "verification_pending_findings": verification_pending_findings(state),
        "follow_up_requirements": state["follow_up_requirements"],
        "derived_requirements": state["derived_requirements"],
        "root_cause_stats": state["root_cause_stats"],
        "authorized_paths": state["authorized_paths"],
        "scope_extensions": state["scope_extensions"],
        "acceptance": {
            item_id: item["status"] for item_id, item in state["acceptance"].items()
        },
        "review_surfaces": {
            item_id: item["status"] for item_id, item in state["review_surfaces"].items()
        },
        "validations": {
            item_id: item["status"] for item_id, item in state["validations"].items()
        },
        "ready": state["ready"],
        "ready_local_diff": state["ready_local_diff"],
        "exit_reason": state.get("exit_reason"),
    }
    if snapshot:
        result["live_head"] = snapshot["head"]
        result["live_diff_hash"] = snapshot["hash"]
        result["live_paths"] = snapshot["paths"]
        matches = snapshot["hash"] == state["current_diff_hash"]
        result["state_matches_live_diff"] = matches
        if not matches:
            result["ready"] = False
            result["ready_local_diff"] = False
            result["ready_stale"] = bool(state["ready"])
    return result


def command_init(args: argparse.Namespace) -> None:
    state_path = Path(args.state).resolve()
    if state_path.exists():
        emit(
            {
                "error": "delivery state already exists; resume it instead of resetting",
                "state": str(state_path),
            },
            POLICY_EXIT,
        )
    try:
        root = resolve_git_root(Path(args.project_root))
        head = current_head(root)
        snapshot = diff_snapshot(root)
    except ValueError as exc:
        emit({"error": str(exc)}, 2)
    try:
        state_path.relative_to(root)
    except ValueError:
        pass
    else:
        emit(
            {
                "error": "delivery state must be outside the supervised Git repository",
                "state": str(state_path),
            },
            2,
        )
    requirements = normalize_requirements(read_json(Path(args.requirements_file), "requirements"))
    require(
        bool(snapshot["paths"]),
        "quality delivery requires an existing local diff; no staged, unstaged, or untracked work was found",
    )
    for path in snapshot["paths"]:
        normalize_literal_path(path, "initial diff path")
    requirements = freeze_allowed_boundaries(
        root,
        requirements,
        exact_file_paths=set(snapshot["paths"]),
    )
    outside_scope = [
        path
        for path in snapshot["paths"]
        if not allowed_path(path, requirements["allowed_paths"])
    ]
    require(
        not outside_scope,
        "initial local diff contains paths outside allowed_paths: "
        + ", ".join(outside_scope),
    )
    digest = requirements_digest(requirements)
    delivery_id = hashlib.sha256(
        f"{root}\0{head}\0{snapshot['hash']}\0{digest}".encode("utf-8")
    ).hexdigest()[:20]

    state = {
        "version": STATE_VERSION,
        "delivery_id": delivery_id,
        "project_root": str(root),
        "baseline_head": head,
        "requirements_digest": digest,
        "requirements": requirements,
        "status": "active",
        "phase": "initial_review",
        "exit_reason": None,
        "initial_diff_hash": snapshot["hash"],
        "initial_paths": snapshot["paths"],
        "authorized_paths": snapshot["paths"],
        "scope_extensions": [],
        "current_diff_hash": snapshot["hash"],
        "initial_review_completed": False,
        "review_campaign_count": 0,
        "fix_batch_count": 0,
        "epoch_fix_batch_count": 0,
        "design_review_count": 0,
        "design_epoch": 1,
        "convergence_events": 0,
        "deferred_design_review": None,
        "complete_validation_campaign": None,
        "acceptance": {
            item["id"]: {
                **item,
                "status": "pending",
                "evidence": "",
                "diff_hash": None,
            }
            for item in requirements["acceptance_criteria"]
        },
        "invariants": {item["id"]: item for item in requirements["invariants"]},
        "review_surfaces": {
            item["id"]: {
                **item,
                "status": "unreviewed",
                "evidence": "",
                "reviewed_diff_hash": None,
                "failure_count": 0,
                "invalidation_count": 0,
                "epoch_failure_count": 0,
                "epoch_invalidation_count": 0,
            }
            for item in requirements["review_surfaces"]
        },
        "validations": {
            item["id"]: {
                **item,
                "status": "pending",
                "evidence": "",
                "diff_hash": None,
            }
            for item in requirements["required_validations"]
        },
        "findings": {},
        "derived_requirements": {},
        "root_cause_stats": {},
        "follow_up_requirements": [],
        "active_fix": None,
        "ready": False,
        "ready_local_diff": False,
        "ready_diff_hash": None,
        "ready_manifest": None,
        "history": [
            {
                "event": "initialized",
                "head": head,
                "diff_hash": snapshot["hash"],
                "paths": snapshot["paths"],
            }
        ],
    }
    atomic_write(state_path, state)
    if requirements["unresolved_decisions"]:
        policy_stop(
            state_path,
            state,
            "decision_required",
            "requirements_contain_unresolved_decisions",
            decisions=requirements["unresolved_decisions"],
        )
    emit({"state": str(state_path), **summarize(state, {"head": head, **snapshot})})


def command_status(args: argparse.Namespace) -> None:
    state = load_state(Path(args.state))
    snapshot = verify_context(state)
    emit(summarize(state, snapshot))


def command_record_review(args: argparse.Namespace) -> None:
    state_path = Path(args.state)
    state = load_state(state_path)
    require(state["status"] == "active", "review requires active delivery state")
    require(not state["ready"], "READY delivery cannot start another review")
    require(state["active_fix"] is None, "cannot review while a fix batch is active")
    snapshot = verify_context(state)
    check_scope(state, snapshot["paths"])
    result = read_json(Path(args.result_file), "review result")
    require(isinstance(result, dict), "review result must be an object")
    kind = result.get("kind")
    require(kind in {"initial", "impact"}, "review kind must be initial or impact")
    require(result.get("diff_hash") == snapshot["hash"], "review diff_hash is stale")
    require(result.get("coverage_complete") is True, "review coverage must be complete")
    raw_surfaces = result.get("surfaces")
    require(isinstance(raw_surfaces, list) and raw_surfaces, "surfaces must be non-empty")
    surface_ids = set(state["review_surfaces"])
    reviewed_ids: set[str] = set()
    normalized_surfaces: list[dict[str, str]] = []
    for item in raw_surfaces:
        require(isinstance(item, dict), "surface review must be an object")
        surface_id = item.get("id")
        status = item.get("status")
        evidence = item.get("evidence")
        require(surface_id in surface_ids, f"unknown review surface: {surface_id}")
        require(surface_id not in reviewed_ids, f"duplicate reviewed surface: {surface_id}")
        require(status in {"clean", "failed", "blocked"}, "invalid reviewed surface status")
        require(isinstance(evidence, str) and evidence.strip(), "surface evidence is required")
        reviewed_ids.add(surface_id)
        normalized_surfaces.append(
            {"id": surface_id, "status": status, "evidence": evidence}
        )

    if kind == "initial":
        require(not state["initial_review_completed"], "initial review already completed")
        require(
            state["phase"] == "initial_review",
            "initial local diff is not ready for its frozen review",
        )
        require(
            snapshot["hash"] == state["initial_diff_hash"],
            "local diff changed after supervisor initialization; restore the frozen input or restart with explicit user approval",
        )
        require(reviewed_ids == surface_ids, "initial review must cover every review surface")
    else:
        require(
            state["phase"] == "impact_review",
            "impact review is allowed only after a recorded fix invalidates evidence",
        )
        require(
            snapshot["hash"] == state["current_diff_hash"],
            "delivery diff changed after the recorded fix; reject drift before impact review",
        )
        invalidated = {
            surface_id
            for surface_id, item in state["review_surfaces"].items()
            if item["status"] in {"invalidated", "failed", "blocked"}
        }
        require(invalidated, "impact review requires invalidated or failed surfaces")
        require(
            invalidated <= reviewed_ids,
            "impact review must cover every invalidated, failed, or blocked surface",
        )

    added, duplicates = add_findings(state, result.get("findings", []))
    for item in normalized_surfaces:
        target = state["review_surfaces"][item["id"]]
        target["status"] = item["status"]
        target["evidence"] = item["evidence"]
        target["reviewed_diff_hash"] = snapshot["hash"]
        if item["status"] in {"failed", "blocked"}:
            target["failure_count"] += 1
            target["epoch_failure_count"] += 1

    if kind == "initial":
        state["initial_review_completed"] = True
    state["review_campaign_count"] += 1
    state["current_diff_hash"] = snapshot["hash"]

    added_findings = [state["findings"][finding_id] for finding_id in added]
    blocking_added = [
        finding
        for finding in added_findings
        if finding["status"] in {"open", "decision_required", "blocked"}
    ]
    surface_results = {item["id"]: item["status"] for item in normalized_surfaces}
    reviewed_blocking = [
        finding
        for finding in state["findings"].values()
        if finding["status"] in {"open", "decision_required", "blocked"}
        and set(finding["affected_surfaces"]) & reviewed_ids
    ]
    for finding in reviewed_blocking:
        require(
            set(finding["affected_surfaces"]) <= reviewed_ids,
            f"finding {finding['id']} affects a surface omitted from this review",
        )
        require(
            all(
                surface_results[surface_id] in {"failed", "blocked"}
                for surface_id in finding["affected_surfaces"]
            ),
            f"finding {finding['id']} requires affected surfaces to be failed or blocked",
        )
    blocking_surface_coverage = {
        surface_id
        for finding in state["findings"].values()
        if finding["status"] in {"open", "decision_required", "blocked"}
        for surface_id in finding["affected_surfaces"]
    }
    failed_or_blocked_surfaces = {
        item["id"]
        for item in normalized_surfaces
        if item["status"] in {"failed", "blocked"}
    }
    require(
        failed_or_blocked_surfaces <= blocking_surface_coverage,
        "every failed or blocked review surface requires a blocking finding: "
        + ", ".join(sorted(failed_or_blocked_surfaces - blocking_surface_coverage)),
    )
    high_priority = [
        finding
        for finding in added_findings
        if finding["status"] == "open" and finding["severity"] in {"P0", "P1", "P2"}
    ]
    thresholds = state["requirements"]["design_review_thresholds"]
    repeated_surface = kind == "impact" and any(
        item["epoch_failure_count"] >= thresholds["surface_failures"]
        for item in state["review_surfaces"].values()
    )
    difficult_after_fixes = (
        kind == "impact"
        and state["epoch_fix_batch_count"]
        >= thresholds["high_priority_after_fix_batches"]
        and bool(high_priority)
    )
    repeated_root_cause = any(
        item.get("epoch_recurrence_count", 0)
        >= thresholds["root_cause_recurrences"]
        for item in state["root_cause_stats"].values()
    )
    convergence_reached = (
        repeated_surface or difficult_after_fixes or repeated_root_cause
    )
    decision_ids = [
        finding["id"]
        for finding in added_findings
        if finding["status"] == "decision_required"
    ]
    if convergence_reached or decision_ids:
        state["current_diff_hash"] = snapshot["hash"]
        save_event(
            state_path,
            state,
            "review_recorded",
            kind=kind,
            added=added,
            duplicates=duplicates,
        )
        route_hard_gate_and_convergence(
            state_path,
            state,
            immediate_phase="decision_required" if decision_ids else None,
            immediate_reason=(
                "review_found_decision_required" if decision_ids else None
            ),
            immediate_details={"added_findings": added},
            convergence_reason=(
                "review_not_converging" if convergence_reached else None
            ),
            convergence_details={
                "repeated_surface": repeated_surface,
                "difficult_after_fixes": difficult_after_fixes,
                "repeated_root_cause": repeated_root_cause,
                "added_findings": added,
            },
            added_findings=added,
        )

    set_active_phase(state, "validation")
    save_event(
        state_path,
        state,
        "review_recorded",
        kind=kind,
        added=added,
        duplicates=duplicates,
        surfaces=sorted(reviewed_ids),
    )
    emit({"added_findings": added, "duplicates": duplicates, **summarize(state, snapshot)})


def command_extend_scope(args: argparse.Namespace) -> None:
    state_path = Path(args.state)
    state = load_state(state_path)
    require(state["status"] == "active", "scope extension requires active delivery state")
    require(not state["ready"], "READY local diff cannot expand scope")
    require(
        state["phase"] == "remediation" and state["active_fix"] is None,
        "scope must expand before a remediation batch is frozen",
    )
    snapshot = verify_context(state)
    require(
        snapshot["hash"] == state["current_diff_hash"],
        "extend-scope must be approved before changing any new path",
    )
    result = read_json(Path(args.result_file), "scope extension")
    require(isinstance(result, dict), "scope extension must be an object")
    reason = result.get("reason")
    require(isinstance(reason, str) and reason.strip(), "scope extension reason is required")
    finding_id = result.get("finding_id")
    require(
        isinstance(finding_id, str) and finding_id in state["findings"],
        "scope extension must name one known finding_id",
    )
    require(
        state["findings"][finding_id]["status"] == "open",
        "scope extension finding must be open",
    )
    parent = {"kind": "finding", "id": finding_id}
    raw_paths = result.get("paths")
    require(isinstance(raw_paths, list) and raw_paths, "scope extension paths must be non-empty")
    paths = [normalize_literal_path(item, "scope extension path") for item in raw_paths]
    require(len(paths) == len(set(paths)), "scope extension paths must be unique")
    new_paths = sorted(set(paths) - set(state["authorized_paths"]))
    require(new_paths, "scope extension must add at least one new exact path")
    try:
        for path in new_paths:
            require_no_symlink_parents(state_root(state), path)
            require_git_visible_scope_path(state_root(state), path)
    except ValueError as exc:
        emit({"error": str(exc)}, 2)
    directory_paths = [
        path for path in new_paths if (state_root(state) / path).is_dir()
    ]
    require(
        not directory_paths,
        "scope extension paths must be files, not directories: "
        + ", ".join(directory_paths),
    )
    boundary = state["requirements"]["allowed_paths"]
    outside_boundary = [path for path in new_paths if not allowed_path(path, boundary)]
    require(
        not outside_boundary,
        "scope extension exceeds the user authorization boundary: "
        + ", ".join(outside_boundary),
    )
    invalidated = result.get("invalidated_surfaces")
    require(
        isinstance(invalidated, list)
        and invalidated
        and all(item in state["review_surfaces"] for item in invalidated),
        "scope extension must invalidate known review surfaces",
    )
    state["complete_validation_campaign"] = None
    for surface_id in set(invalidated):
        surface = state["review_surfaces"][surface_id]
        if surface["status"] != "invalidated":
            surface["invalidation_count"] += 1
            surface["epoch_invalidation_count"] += 1
        surface["status"] = "invalidated"
        surface["evidence"] = ""
        surface["reviewed_diff_hash"] = None
    state["authorized_paths"] = sorted(set(state["authorized_paths"]) | set(new_paths))
    extension = {
        "reason": reason,
        "parent": parent,
        "paths": new_paths,
        "invalidated_surfaces": sorted(set(invalidated)),
        "design_epoch": state["design_epoch"],
        "diff_hash": snapshot["hash"],
    }
    state["scope_extensions"].append(extension)
    save_event(state_path, state, "scope_extended", **extension)
    emit({"scope_extension": extension, **summarize(state, snapshot)})


def command_begin_fix(args: argparse.Namespace) -> None:
    state_path = Path(args.state)
    state = load_state(state_path)
    require(state["status"] == "active", "fix requires active delivery state")
    require(state["phase"] == "remediation", "state is not ready for remediation")
    require(state["active_fix"] is None, "another fix batch is already active")
    snapshot, manifest_before = capture_stable_manifest(
        state,
        state["authorized_paths"],
        label="fix baseline",
    )
    require(
        snapshot["hash"] == state["current_diff_hash"],
        "delivery diff changed after the latest recorded evidence; review the drift before fixing",
    )
    ids = read_json(Path(args.finding_ids_file), "finding ids")
    require(
        isinstance(ids, list)
        and ids
        and all(isinstance(item, str) for item in ids),
        "finding ids must be a non-empty string array",
    )
    require(len(set(ids)) == len(ids), "finding ids must be unique")
    raw_batch_paths = read_json(Path(args.batch_paths_file), "batch paths")
    require(
        isinstance(raw_batch_paths, list) and raw_batch_paths,
        "batch paths must be a non-empty string array",
    )
    batch_paths = [normalize_literal_path(item, "batch path") for item in raw_batch_paths]
    require(len(batch_paths) == len(set(batch_paths)), "batch paths must be unique")
    require(
        set(batch_paths) <= set(state["authorized_paths"]),
        "batch paths must be authorized by the initial scope or extend-scope",
    )
    require_no_symlink_endpoints(state_root(state), batch_paths, "batch paths")
    selected: list[dict[str, Any]] = []
    for finding_id in ids:
        require(finding_id in state["findings"], f"unknown finding: {finding_id}")
        finding = state["findings"][finding_id]
        require(finding["status"] == "open", f"finding is not open: {finding_id}")
        require(
            finding["classification"] in FIXABLE_CLASSIFICATIONS,
            f"finding cannot be fixed in the current delivery: {finding_id}",
        )
        selected.append(finding)
    root_cause_keys = {finding_root_cause_key(finding) for finding in selected}
    require(
        len(root_cause_keys) == 1,
        "one fix batch must contain findings with one shared root cause",
    )
    shared_root_cause_key = next(iter(root_cause_keys))
    shared_root_cause = next(
        finding["normalized_root_cause"]
        for finding in selected
        if finding_root_cause_key(finding) == shared_root_cause_key
    )
    batch = state["fix_batch_count"] + 1
    affected_surfaces = {
        surface_id
        for finding in selected
        for surface_id in finding["affected_surfaces"]
    }
    required_preflight = preflight_checklist(affected_surfaces)
    for finding in selected:
        finding["status"] = "fixing"
    state["active_fix"] = {
        "batch": batch,
        "finding_ids": ids,
        "root_cause": shared_root_cause,
        "root_cause_key": shared_root_cause_key,
        "diff_hash_before": snapshot["hash"],
        "paths_before": snapshot["paths"],
        "manifest_before": manifest_before,
        "manifest_paths": sorted(state["authorized_paths"]),
        "authorized_paths": sorted(batch_paths),
        "ignored_paths_before": ignored_worktree_paths(state_root(state)),
        "worktree_entry_manifest_before": worktree_entry_metadata_manifest(
            state_root(state)
        ),
        "worktree_directory_manifest_before": worktree_directory_manifest(
            state_root(state)
        ),
        "git_control_manifest_before": git_control_manifest(state_root(state)),
        "preflight_checklist": required_preflight,
        "test_mode": args.test_mode,
        "executor_contract": {
            "preferred": "code-review-fix-loop",
            "fallback": "direct",
            "mode": "frozen_batch",
            "may_initial_review": False,
            "may_expand_scope": False,
            "may_claim_ready": False,
            "preflight_checklist": required_preflight,
        },
    }
    state["phase"] = "fixing"
    save_event(
        state_path,
        state,
        "fix_started",
        batch=batch,
        finding_ids=ids,
        root_cause=shared_root_cause,
        root_cause_key=shared_root_cause_key,
        authorized_paths=sorted(batch_paths),
        test_mode=args.test_mode,
    )
    emit({"active_fix": state["active_fix"], **summarize(state, snapshot)})


def command_cancel_fix(args: argparse.Namespace) -> None:
    state_path = Path(args.state)
    state = load_state(state_path)
    require(state["status"] == "active", "fix cancellation requires active state")
    active = state.get("active_fix")
    require(isinstance(active, dict), "no active fix batch")
    require_no_symlink_endpoints(
        state_root(state), active["authorized_paths"], "fix cancellation paths"
    )
    snapshot, manifest_after = capture_stable_manifest(
        state,
        active["manifest_paths"],
        label="fix cancellation",
    )
    require(
        snapshot["hash"] == active["diff_hash_before"]
        and manifest_after == active["manifest_before"],
        "fix cancellation requires the exact frozen pre-fix Git state",
    )
    boundary_changes = frozen_batch_boundary_changes(state, active)
    require(
        not boundary_changes,
        "fix cancellation left persistent writes outside the frozen batch: "
        + ", ".join(boundary_changes),
    )
    result = read_json(Path(args.result_file), "fix cancellation")
    require(isinstance(result, dict), "fix cancellation must be an object")
    resolutions = result.get("finding_resolutions")
    require(
        isinstance(resolutions, list)
        and len(resolutions) == len(active["finding_ids"]),
        "fix cancellation must resolve every frozen finding exactly once",
    )
    resolution_ids: list[str] = []
    invalidated: set[str] = set()
    dispositions: dict[str, str] = {}
    for item in resolutions:
        require(isinstance(item, dict), "fix cancellation resolution must be an object")
        finding_id = item.get("id")
        require(
            isinstance(finding_id, str) and finding_id in active["finding_ids"],
            f"unknown frozen finding: {finding_id}",
        )
        require(
            finding_id not in resolution_ids,
            f"duplicate fix cancellation finding: {finding_id}",
        )
        disposition = item.get("disposition")
        require(
            disposition in {"retry", "false_positive"},
            "fix cancellation disposition must be retry or false_positive",
        )
        evidence = item.get("evidence")
        require(
            isinstance(evidence, str) and evidence.strip(),
            "fix cancellation evidence is required",
        )
        finding = state["findings"][finding_id]
        finding["status"] = "open" if disposition == "retry" else "false_positive"
        finding["evidence"] = evidence
        requirement = state["derived_requirements"].get(f"REQ-{finding_id}")
        if disposition == "retry":
            if requirement:
                requirement["status"] = "open"
        else:
            finding["evidence_diff_hash"] = snapshot["hash"]
            if requirement:
                requirement["status"] = "false_positive"
                requirement["evidence"] = evidence
                requirement["evidence_diff_hash"] = snapshot["hash"]
            for surface_id in finding["affected_surfaces"]:
                surface = state["review_surfaces"][surface_id]
                if surface["status"] != "invalidated":
                    surface["invalidation_count"] += 1
                    surface["epoch_invalidation_count"] += 1
                surface["status"] = "invalidated"
                surface["evidence"] = ""
                surface["reviewed_diff_hash"] = None
                invalidated.add(surface_id)
        resolution_ids.append(finding_id)
        dispositions[finding_id] = disposition
    require(
        set(resolution_ids) == set(active["finding_ids"]),
        "fix cancellation must resolve the exact frozen finding set",
    )
    state["active_fix"] = None
    state["current_diff_hash"] = snapshot["hash"]
    state["ready"] = False
    state["ready_local_diff"] = False
    state["ready_diff_hash"] = None
    if invalidated:
        state["complete_validation_campaign"] = None
    threshold = state["requirements"]["design_review_thresholds"][
        "surface_invalidations"
    ]
    over_invalidated = [
        surface_id
        for surface_id, item in state["review_surfaces"].items()
        if item["epoch_invalidation_count"] >= threshold
    ]
    set_active_phase(state, "validation")
    save_event(
        state_path,
        state,
        "fix_cancelled",
        batch=active["batch"],
        dispositions=dispositions,
        invalidated_surfaces=sorted(invalidated),
    )
    route_hard_gate_and_convergence(
        state_path,
        state,
        convergence_reason=(
            "review_surfaces_repeatedly_invalidated"
            if over_invalidated
            else None
        ),
        convergence_details={"surfaces": over_invalidated},
    )
    emit(summarize(state, snapshot))


def command_record_fix(args: argparse.Namespace) -> None:
    state_path = Path(args.state)
    state = load_state(state_path)
    require(state["status"] == "active", "fix result requires active state")
    active = state.get("active_fix")
    require(isinstance(active, dict), "no active fix batch")
    require_no_symlink_endpoints(
        state_root(state), active["authorized_paths"], "fix result paths"
    )
    snapshot, manifest_after = capture_stable_manifest(
        state,
        active["manifest_paths"],
        label="fix result",
    )
    check_scope(state, snapshot["paths"])
    result = read_json(Path(args.result_file), "fix result")
    require(isinstance(result, dict), "fix result must be an object")
    require(
        result.get("finding_ids") == active["finding_ids"],
        "fix result finding_ids must exactly match the active batch",
    )
    require(
        result.get("diff_hash_before") == active["diff_hash_before"],
        "fix result diff_hash_before does not match",
    )
    require(result.get("diff_hash_after") == snapshot["hash"], "fix result diff_hash_after is stale")
    require(
        snapshot["hash"] != active["diff_hash_before"],
        "fix batch did not change the delivery diff",
    )
    raw_changed_paths = result.get("changed_paths")
    require(
        isinstance(raw_changed_paths, list) and raw_changed_paths,
        "changed_paths must be a non-empty string array",
    )
    changed_paths = [
        normalize_literal_path(item, "changed path") for item in raw_changed_paths
    ]
    require(len(changed_paths) == len(set(changed_paths)), "changed_paths must be unique")
    actual_changed_paths = changed_manifest_paths(
        active["manifest_before"],
        manifest_after,
    )
    require(
        actual_changed_paths,
        "fix batch did not change any authorized file",
    )
    require(
        set(changed_paths) == set(actual_changed_paths),
        "reported changed_paths do not match the actual fix delta: "
        f"reported={sorted(changed_paths)}, actual={actual_changed_paths}",
    )
    require(
        set(actual_changed_paths) <= set(active["authorized_paths"]),
        "fix changed paths outside the frozen batch authorization",
    )
    invisible_writes = [
        path
        for path in actual_changed_paths
        if path not in snapshot["paths"]
        and not manifest_matches_head(state_root(state), path, manifest_after[path])
    ]
    require(
        not invisible_writes,
        "fix left persistent writes outside the supervised Git diff: "
        + ", ".join(invisible_writes),
    )
    boundary_changes = frozen_batch_boundary_changes(state, active)
    require(
        not boundary_changes,
        "fix left persistent writes outside the frozen batch: "
        + ", ".join(boundary_changes),
    )
    check_scope(state, actual_changed_paths)
    executor = result.get("executor")
    require(isinstance(executor, dict), "fix result must identify its batch executor")
    require(executor.get("name") in BATCH_EXECUTORS, "invalid fix batch executor")
    require(
        executor.get("mode") in BATCH_EXECUTOR_MODES,
        "fix executor must run in frozen_batch mode",
    )
    require(
        result.get("test_mode") == active["test_mode"],
        "fix result test_mode must match the frozen batch",
    )
    invalidated = result.get("invalidated_surfaces")
    require(
        isinstance(invalidated, list)
        and invalidated
        and all(item in state["review_surfaces"] for item in invalidated),
        "invalidated_surfaces must reference known review surfaces",
    )
    required_invalidations = {
        surface_id
        for finding_id in active["finding_ids"]
        for surface_id in state["findings"][finding_id]["affected_surfaces"]
    }
    require(
        required_invalidations <= set(invalidated),
        "fix result must invalidate every surface affected by its findings: "
        + ", ".join(sorted(required_invalidations - set(invalidated))),
    )
    direct_validation = result.get("direct_validation", [])
    require(isinstance(direct_validation, list), "direct_validation must be an array")
    required_preflight_ids = {
        item["id"]
        for item in active["preflight_checklist"]
        + preflight_checklist(set(invalidated))
    }
    seen_preflight_ids: set[str] = set()
    for index, item in enumerate(direct_validation):
        require(isinstance(item, dict), "direct validation entry must be an object")
        check_id = item.get("id")
        if (
            check_id is None
            and required_preflight_ids == {"changed-path-integrity"}
            and len(direct_validation) == 1
        ):
            check_id = "changed-path-integrity"
            item["id"] = check_id
        require(
            isinstance(check_id, str) and check_id.strip(),
            f"direct validation entry {index} requires an id",
        )
        require(check_id not in seen_preflight_ids, f"duplicate preflight id: {check_id}")
        seen_preflight_ids.add(check_id)
        require(item.get("status") in {"pass", "fail", "blocked"}, "invalid direct validation status")
        require(
            isinstance(item.get("evidence"), str) and item["evidence"].strip(),
            "direct validation evidence is required",
        )
    require(
        direct_validation
        and all(item["status"] == "pass" for item in direct_validation),
        "every direct validation must pass before a fix can be recorded",
    )
    require(
        required_preflight_ids <= seen_preflight_ids,
        "direct validation omitted required preflight checks: "
        + ", ".join(sorted(required_preflight_ids - seen_preflight_ids)),
    )
    fast_path = result.get("evidence_only_fast_path")
    evidence_only = fast_path is not None
    if evidence_only:
        require(isinstance(fast_path, dict), "evidence_only_fast_path must be an object")
        require(
            active["test_mode"] in {"light", "deep"},
            "evidence-only fast path requires light or deep targeted tests",
        )
        require(
            all(
                state["findings"][finding_id]["classification"] == "test_infrastructure"
                for finding_id in active["finding_ids"]
            ),
            "evidence-only fast path only accepts test_infrastructure findings",
        )
        require(
            set(invalidated) == {"test-reliability"},
            "evidence-only fast path may close only test-reliability",
        )
        require(
            all(is_test_evidence_path(path) for path in actual_changed_paths),
            "evidence-only fast path changed a non-test-evidence path",
        )
        for assertion in [
            "changes_product_behavior",
            "changes_fixture_semantics",
            "changes_shared_test_helpers",
        ]:
            require(
                fast_path.get(assertion) is False,
                f"evidence-only fast path requires {assertion}=false",
            )
        require(
            "targeted-tests" in seen_preflight_ids,
            "evidence-only fast path requires targeted-tests evidence",
        )
        require(
            isinstance(fast_path.get("evidence"), str)
            and fast_path["evidence"].strip(),
            "evidence-only fast path requires evidence",
        )
    for finding_id in active["finding_ids"]:
        state["findings"][finding_id]["status"] = "fixed_unverified"
        state["findings"][finding_id]["fix_batch"] = active["batch"]
        requirement = state["derived_requirements"].get(f"REQ-{finding_id}")
        if requirement:
            requirement["status"] = "fixed_unverified"
    invalidated_surface_ids = set(invalidated)
    for finding_id, finding in state["findings"].items():
        if finding["status"] != "verified":
            continue
        finding["status"] = "fixed_unverified"
        finding["verification_evidence"] = ""
        finding["verified_diff_hash"] = None
        requirement = state["derived_requirements"].get(f"REQ-{finding_id}")
        if requirement:
            requirement["status"] = "fixed_unverified"
            requirement["verification_evidence"] = ""
            requirement["verified_diff_hash"] = None
    for finding_id, finding in state["findings"].items():
        if finding["status"] != "false_positive":
            continue
        finding["status"] = "decision_required"
        finding["evidence"] = ""
        finding["evidence_diff_hash"] = None
        requirement = state["derived_requirements"].get(f"REQ-{finding_id}")
        if requirement:
            requirement["status"] = "decision_required"
            requirement["evidence"] = ""
            requirement["evidence_diff_hash"] = None
        invalidated_surface_ids.update(finding["affected_surfaces"])
    for surface_id in invalidated_surface_ids:
        surface = state["review_surfaces"][surface_id]
        if surface["status"] != "invalidated":
            surface["invalidation_count"] += 1
            surface["epoch_invalidation_count"] += 1
        surface["status"] = "invalidated"
        surface["evidence"] = ""
        surface["reviewed_diff_hash"] = None
    state["complete_validation_campaign"] = None
    if evidence_only:
        surface = state["review_surfaces"]["test-reliability"]
        surface["status"] = "clean"
        surface["evidence"] = fast_path["evidence"]
        surface["reviewed_diff_hash"] = snapshot["hash"]
    state["fix_batch_count"] = active["batch"]
    state["epoch_fix_batch_count"] += 1
    state["active_fix"] = None
    state["current_diff_hash"] = snapshot["hash"]
    state["ready"] = False
    state["ready_local_diff"] = False
    state["ready_diff_hash"] = None
    state["phase"] = "validation" if evidence_only else "impact_review"
    save_event(
        state_path,
        state,
        "fix_recorded",
        batch=state["fix_batch_count"],
        finding_ids=result["finding_ids"],
        changed_paths=changed_paths,
        invalidated_surfaces=sorted(invalidated_surface_ids),
        direct_validation=direct_validation,
        executor=executor,
        test_mode=result["test_mode"],
        evidence_only_fast_path=evidence_only,
    )
    threshold = state["requirements"]["design_review_thresholds"][
        "surface_invalidations"
    ]
    over_invalidated = [
        surface_id
        for surface_id, item in state["review_surfaces"].items()
        if item["epoch_invalidation_count"] >= threshold
    ]
    pending_decisions = decision_findings(state)
    route_hard_gate_and_convergence(
        state_path,
        state,
        immediate_phase="decision_required" if pending_decisions else None,
        immediate_reason=(
            "later_fix_invalidated_false_positive_evidence"
            if pending_decisions
            else None
        ),
        immediate_details={"findings": pending_decisions},
        convergence_reason=(
            "review_surfaces_repeatedly_invalidated"
            if over_invalidated
            else None
        ),
        convergence_details={"surfaces": over_invalidated},
    )
    emit(summarize(state, snapshot))


def command_add_findings(args: argparse.Namespace) -> None:
    state_path = Path(args.state)
    state = load_state(state_path)
    require(
        state.get("active_fix") is None,
        "cannot add findings while an active fix batch is frozen",
    )
    gate_before = hard_gate(state)
    snapshot = verify_context(state)
    require(
        snapshot["hash"] == state["current_diff_hash"],
        "delivery diff changed after the latest recorded evidence; review the drift before adding findings",
    )
    result = read_json(Path(args.result_file), "finding result")
    require(isinstance(result, dict), "finding result must be an object")
    require(result.get("diff_hash") == snapshot["hash"], "finding result diff_hash is stale")
    added, duplicates = add_findings(state, result.get("findings", []))
    require(added or duplicates, "finding result did not add or match any finding")
    blocking_added = [
        finding_id
        for finding_id in added
        if state["findings"][finding_id]["status"]
        in {"open", "decision_required", "blocked"}
    ]
    state["current_diff_hash"] = snapshot["hash"]
    invalidated = invalidate_finding_surfaces(state, blocking_added)
    if blocking_added:
        state["ready"] = False
        state["ready_local_diff"] = False
        state["ready_diff_hash"] = None
        state["ready_manifest"] = None
    decision_added = any(
        state["findings"][finding_id]["status"] == "decision_required"
        for finding_id in blocking_added
    )
    root_threshold_reached = bool(
        blocking_added and root_cause_threshold_reached(state)
    )
    if gate_before is None and (decision_added or root_threshold_reached):
        save_event(
            state_path,
            state,
            "findings_added",
            added=added,
            duplicates=duplicates,
            invalidated_surfaces=invalidated,
        )
    route_hard_gate_and_convergence(
        state_path,
        state,
        existing_gate=gate_before,
        immediate_phase="decision_required" if decision_added else None,
        immediate_reason="new_finding_requires_decision" if decision_added else None,
        immediate_details={"added_findings": added},
        convergence_reason=(
            "added_findings_reached_root_cause_threshold"
            if root_threshold_reached
            else None
        ),
        convergence_details={"added_findings": added},
        added_findings=added,
    )
    if blocking_added and gate_before is None:
        set_active_phase(state, "validation")
    save_event(
        state_path,
        state,
        "findings_added",
        added=added,
        duplicates=duplicates,
        invalidated_surfaces=invalidated,
        preserved_gate=gate_before,
    )
    emit({"added_findings": added, "duplicates": duplicates, **summarize(state, snapshot)})


def command_record_validation(args: argparse.Namespace) -> None:
    state_path = Path(args.state)
    state = load_state(state_path)
    recovering_validation_gate = (
        state["status"] == "paused" and state["phase"] == "validation_failed"
    )
    require(
        state["status"] == "active" or recovering_validation_gate,
        "validation requires active state or the validation_failed recovery gate",
    )
    require(state["active_fix"] is None, "cannot validate while a fix batch is active")
    require(
        recovering_validation_gate
        or state["phase"] in {"validation", "acceptance_audit"},
        "state is not ready for validation",
    )
    if recovering_validation_gate:
        enforce_deferred_design_review(
            state_path,
            state,
            cleared_gate="validation_failed",
        )
        state["status"] = "active"
        state["phase"] = "validation"
        state["exit_reason"] = None
    snapshot = verify_context(state)
    require(
        all(
            surface["status"] == "clean"
            for surface in state["review_surfaces"].values()
        ),
        "validation requires every review surface to be clean",
    )
    require(
        snapshot["hash"] == state["current_diff_hash"],
        "delivery diff changed after review; invalidate and review affected surfaces before validation",
    )
    result = read_json(Path(args.result_file), "validation result")
    require(isinstance(result, dict), "validation result must be an object")
    require(result.get("diff_hash") == snapshot["hash"], "validation diff_hash is stale")
    # Every accepted submission starts a new campaign attempt. A later partial
    # submission must not compose with an older complete campaign.
    state["complete_validation_campaign"] = None

    failed_bindings: list[tuple[str, str, Any]] = []
    submitted_ids: dict[str, set[str]] = {}
    for group_name, state_key in [
        ("validations", "validations"),
        ("acceptance", "acceptance"),
    ]:
        entries = result.get(group_name, [])
        require(isinstance(entries, list), f"{group_name} must be an array")
        seen_ids: set[str] = set()
        for item in entries:
            require(isinstance(item, dict), f"{group_name} entry must be an object")
            item_id = item.get("id")
            require(item_id in state[state_key], f"unknown {group_name} id: {item_id}")
            require(item_id not in seen_ids, f"duplicate {group_name} id: {item_id}")
            seen_ids.add(item_id)
            status = item.get("status")
            require(status in {"pass", "fail", "blocked"}, f"invalid {group_name} status")
            evidence = item.get("evidence")
            require(
                isinstance(evidence, str) and evidence.strip(),
                f"{group_name} evidence is required",
            )
            target = state[state_key][item_id]
            target["status"] = status
            target["evidence"] = evidence
            target["diff_hash"] = snapshot["hash"]
            if status in {"fail", "blocked"}:
                failed_bindings.append((group_name, item_id, item.get("finding_id")))
        submitted_ids[state_key] = seen_ids

    verified = result.get("verified_findings", [])
    require(
        isinstance(verified, list) and all(isinstance(item, str) for item in verified),
        "verified_findings must be a string array",
    )
    require(len(verified) == len(set(verified)), "verified_findings must be unique")
    failed_finding_ids = {
        finding_id
        for _, _, finding_id in failed_bindings
        if isinstance(finding_id, str)
    }
    contradictory_findings = sorted(set(verified) & failed_finding_ids)
    require(
        not contradictory_findings,
        "a finding cannot be verified and bound to failed evidence in one validation result: "
        + ", ".join(contradictory_findings),
    )
    finding_evidence = result.get("finding_evidence", {})
    require(isinstance(finding_evidence, dict), "finding_evidence must be an object")
    for finding_id in verified:
        require(finding_id in state["findings"], f"unknown finding: {finding_id}")
        finding = state["findings"][finding_id]
        require(
            finding["status"] == "fixed_unverified",
            f"finding is not fixed_unverified: {finding_id}",
        )
        evidence = finding_evidence.get(finding_id)
        require(
            isinstance(evidence, str) and evidence.strip(),
            f"verification evidence is required for finding: {finding_id}",
        )
        finding["status"] = "verified"
        finding["verification_evidence"] = evidence
        finding["verified_diff_hash"] = snapshot["hash"]
        requirement = state["derived_requirements"].get(f"REQ-{finding_id}")
        if requirement:
            requirement["status"] = "verified"
            requirement["verification_evidence"] = evidence
            requirement["verified_diff_hash"] = snapshot["hash"]

    new_findings = result.get("new_findings", [])
    added: list[str] = []
    duplicates: list[str] = []
    if new_findings:
        added, duplicates = add_findings(state, new_findings)
    reopened_failed_findings: list[str] = []
    for _, _, finding_id in failed_bindings:
        if not isinstance(finding_id, str) or finding_id in reopened_failed_findings:
            continue
        finding = state["findings"].get(finding_id)
        if not isinstance(finding, dict) or finding["status"] != "fixed_unverified":
            continue
        finding["status"] = "open"
        finding["verification_evidence"] = ""
        finding["verified_diff_hash"] = None
        finding["recurrence_count"] = finding.get("recurrence_count", 0) + 1
        finding["epoch_recurrence_count"] = (
            finding.get("epoch_recurrence_count", 0) + 1
        )
        register_root_cause_occurrence(state, finding["root_cause"])
        requirement = state["derived_requirements"].get(f"REQ-{finding_id}")
        if requirement:
            requirement["status"] = "open"
            requirement["verification_evidence"] = ""
            requirement["verified_diff_hash"] = None
        reopened_failed_findings.append(finding_id)
    blocking_added = sorted(set(reopened_failed_findings) | {
        finding_id
        for finding_id in added
        if state["findings"][finding_id]["status"]
        in {"open", "decision_required", "blocked"}
    })
    invalidated = invalidate_finding_surfaces(state, blocking_added)
    blocking_ids = set(open_blocking_findings(state))
    invalid_failed_bindings = [
        f"{group_name}:{item_id}"
        for group_name, item_id, finding_id in failed_bindings
        if not isinstance(finding_id, str) or finding_id not in blocking_ids
    ]
    binding_owners: dict[str, str] = {}
    for group_name, item_id, finding_id in failed_bindings:
        if not isinstance(finding_id, str):
            continue
        item_key = f"{group_name}:{item_id}"
        if finding_id in binding_owners:
            invalid_failed_bindings.extend(
                [binding_owners[finding_id], item_key]
            )
        else:
            binding_owners[finding_id] = item_key
    invalid_failed_bindings = sorted(set(invalid_failed_bindings))
    root_threshold_reached = bool(
        blocking_added and root_cause_threshold_reached(state)
    )
    pending_decisions = decision_findings(state)
    if invalid_failed_bindings or pending_decisions or root_threshold_reached:
        state["current_diff_hash"] = snapshot["hash"]
        save_event(
            state_path,
            state,
            "validation_recorded",
            verified_findings=verified,
            added_findings=added,
            duplicates=duplicates,
            invalidated_surfaces=invalidated,
            failed=[
                f"{group_name}:{item_id}"
                for group_name, item_id, _ in failed_bindings
            ],
            invalid_failed_bindings=invalid_failed_bindings,
        )
        immediate_phase = (
            "decision_required"
            if pending_decisions
            else "validation_failed"
            if invalid_failed_bindings
            else None
        )
        immediate_reason = (
            "validation_found_decision_required"
            if pending_decisions
            else "each_failed_item_requires_its_own_blocking_finding"
            if invalid_failed_bindings
            else None
        )
        immediate_details = (
            {"findings": pending_decisions}
            if pending_decisions
            else {"failed": invalid_failed_bindings}
            if invalid_failed_bindings
            else {}
        )
        route_hard_gate_and_convergence(
            state_path,
            state,
            immediate_phase=immediate_phase,
            immediate_reason=immediate_reason,
            immediate_details=immediate_details,
            convergence_reason=(
                "validation_findings_reached_root_cause_threshold"
                if root_threshold_reached
                else None
            ),
            convergence_details={"added_findings": added},
            added_findings=added,
        )
    state["current_diff_hash"] = snapshot["hash"]
    complete_campaign = (
        submitted_ids.get("validations") == set(state["validations"])
        and submitted_ids.get("acceptance") == set(state["acceptance"])
        and not failed_bindings
        and not blocking_added
        and not decision_findings(state)
    )
    if complete_campaign:
        state["complete_validation_campaign"] = {
            "diff_hash": snapshot["hash"],
            "validation_ids": sorted(submitted_ids["validations"]),
            "acceptance_ids": sorted(submitted_ids["acceptance"]),
        }
    set_active_phase(state, "acceptance_audit")
    save_event(
        state_path,
        state,
        "validation_recorded",
        verified_findings=verified,
        added_findings=added,
        duplicates=duplicates,
        invalidated_surfaces=invalidated,
        failed=[f"{group_name}:{item_id}" for group_name, item_id, _ in failed_bindings],
        complete_campaign=complete_campaign,
    )
    emit(
        {
            "verified_findings": verified,
            "added_findings": added,
            "duplicates": duplicates,
            **summarize(state, snapshot),
        }
    )


def command_resolve_design_review(args: argparse.Namespace) -> None:
    state_path = Path(args.state)
    state = load_state(state_path)
    require(
        state["status"] == "paused"
        and state["phase"] in {"design_review_required", "blocked"},
        "state is not waiting for a design review",
    )
    snapshot = verify_context(state)
    check_scope(state, snapshot["paths"])
    require(
        snapshot["hash"] == state["current_diff_hash"],
        "delivery diff changed while design review was paused; review the drift before resolving",
    )
    result = read_json(Path(args.result_file), "design review result")
    require(isinstance(result, dict), "design review result must be an object")
    decision = result.get("decision")
    require(decision in {"resume", "rollback", "blocked"}, "invalid design review decision")
    require(
        isinstance(result.get("diagnosis"), str) and result["diagnosis"].strip(),
        "design review diagnosis is required",
    )
    require(
        isinstance(result.get("strategy"), str) and result["strategy"].strip(),
        "design review strategy is required",
    )
    state["design_review_count"] += 1
    state["exit_reason"] = None
    if decision == "blocked":
        state["status"] = "paused"
        state["phase"] = "blocked"
        state["exit_reason"] = "design_review_blocked"
        save_event(
            state_path,
            state,
            "design_review_resolved",
            decision=decision,
            diagnosis=result["diagnosis"],
            strategy=result["strategy"],
        )
        emit(summarize(state, snapshot), POLICY_EXIT)

    state["deferred_design_review"] = None

    invalidated = result.get("invalidated_surfaces", [])
    require(
        isinstance(invalidated, list)
        and invalidated
        and all(item in state["review_surfaces"] for item in invalidated),
        "design review must invalidate known review surfaces",
    )
    state["complete_validation_campaign"] = None
    for surface_id in set(invalidated):
        surface = state["review_surfaces"][surface_id]
        surface["invalidation_count"] += 1
        surface["epoch_invalidation_count"] += 1
        surface["status"] = "invalidated"
        surface["evidence"] = ""
        surface["reviewed_diff_hash"] = None
    for item in normalize_items(
        result.get("added_acceptance", []),
        label="added_acceptance",
        prefix="DA",
        default_items=[],
    ) if result.get("added_acceptance") else []:
        require(item["id"] not in state["acceptance"], f"duplicate acceptance id: {item['id']}")
        state["acceptance"][item["id"]] = {
            **item,
            "status": "pending",
            "evidence": "",
            "diff_hash": None,
        }
    for item in normalize_items(
        result.get("added_invariants", []),
        label="added_invariants",
        prefix="DI",
        default_items=[],
    ) if result.get("added_invariants") else []:
        require(item["id"] not in state["invariants"], f"duplicate invariant id: {item['id']}")
        state["invariants"][item["id"]] = item
    previous_epoch = state["design_epoch"]
    state["design_epoch"] += 1
    state["epoch_fix_batch_count"] = 0
    for surface in state["review_surfaces"].values():
        surface["epoch_failure_count"] = 0
        surface["epoch_invalidation_count"] = 0
    for finding in state["findings"].values():
        finding["epoch_recurrence_count"] = 0
    for item in state["root_cause_stats"].values():
        item["epoch_occurrence_count"] = 0
        item["epoch_recurrence_count"] = 0
    state["status"] = "active"
    state["current_diff_hash"] = snapshot["hash"]
    if decision_findings(state) or state["requirements"]["unresolved_decisions"]:
        save_event(
            state_path,
            state,
            "design_review_resolved",
            decision=decision,
            diagnosis=result["diagnosis"],
            strategy=result["strategy"],
            invalidated_surfaces=sorted(set(invalidated)),
            previous_design_epoch=previous_epoch,
            design_epoch=state["design_epoch"],
        )
        policy_stop(
            state_path,
            state,
            "decision_required",
            "design_review_left_decisions_unresolved",
            findings=decision_findings(state),
            decisions=state["requirements"]["unresolved_decisions"],
        )
    set_active_phase(state, "validation")
    save_event(
        state_path,
        state,
        "design_review_resolved",
        decision=decision,
        diagnosis=result["diagnosis"],
        strategy=result["strategy"],
        invalidated_surfaces=sorted(set(invalidated)),
        previous_design_epoch=previous_epoch,
        design_epoch=state["design_epoch"],
    )
    emit(summarize(state, snapshot))


def command_resolve_decision(args: argparse.Namespace) -> None:
    state_path = Path(args.state)
    state = load_state(state_path)
    require(
        state["phase"] == "decision_required",
        "state is not waiting for a decision",
    )
    snapshot = verify_context(state)
    check_scope(state, snapshot["paths"])
    require(
        snapshot["hash"] == state["current_diff_hash"],
        "delivery diff changed while a correctness decision was paused; review the drift before resolving",
    )
    result = read_json(Path(args.result_file), "decision result")
    require(isinstance(result, dict), "decision result must be an object")
    resolutions = result.get("resolved_decisions", [])
    require(isinstance(resolutions, list), "resolved_decisions must be an array")
    unresolved = list(state["requirements"]["unresolved_decisions"])
    for item in resolutions:
        require(isinstance(item, dict), "decision resolution must be an object")
        decision = item.get("decision")
        resolution = item.get("resolution")
        evidence = item.get("evidence")
        require(decision in unresolved, f"unknown unresolved decision: {decision}")
        require(
            isinstance(resolution, str) and resolution.strip(),
            "decision resolution is required",
        )
        require(
            isinstance(evidence, str) and evidence.strip(),
            "decision resolution evidence is required",
        )
        unresolved.remove(decision)

    finding_resolutions = result.get("finding_resolutions", [])
    require(isinstance(finding_resolutions, list), "finding_resolutions must be an array")
    for item in finding_resolutions:
        require(isinstance(item, dict), "finding resolution must be an object")
        finding_id = item.get("id")
        require(finding_id in state["findings"], f"unknown finding: {finding_id}")
        finding = state["findings"][finding_id]
        require(
            finding["status"] == "decision_required",
            f"finding is not decision_required: {finding_id}",
        )
        classification = item.get("classification")
        require(
            classification in (FIXABLE_CLASSIFICATIONS | FOLLOW_UP_CLASSIFICATIONS | {"false_positive"}),
            f"invalid resolved classification: {classification}",
        )
        evidence = item.get("evidence")
        require(
            isinstance(evidence, str) and evidence.strip(),
            "finding decision evidence is required",
        )
        finding["classification"] = classification
        finding["evidence"] = evidence
        replacement_root_cause = item.get("root_cause")
        if replacement_root_cause is not None:
            require(
                isinstance(replacement_root_cause, str) and replacement_root_cause.strip(),
                "resolved root_cause must be non-empty",
            )
            assign_root_cause_identity(finding, replacement_root_cause)
        else:
            assign_root_cause_identity(finding, finding["root_cause"])
        if classification in FOLLOW_UP_CLASSIFICATIONS:
            finding["status"] = "follow_up"
            finding["root_cause_registered"] = False
            state["derived_requirements"].pop(f"REQ-{finding_id}", None)
            if finding_id not in state["follow_up_requirements"]:
                state["follow_up_requirements"].append(finding_id)
        elif classification == "false_positive":
            finding["status"] = "false_positive"
            finding["root_cause_registered"] = False
            finding["evidence_diff_hash"] = snapshot["hash"]
            requirement = state["derived_requirements"].get(f"REQ-{finding_id}")
            if requirement:
                requirement["status"] = "false_positive"
                requirement["evidence"] = evidence
                requirement["evidence_diff_hash"] = snapshot["hash"]
        else:
            finding["status"] = "open"
            finding["root_cause_registered"] = True
            state["derived_requirements"][f"REQ-{finding_id}"] = {
                "id": f"REQ-{finding_id}",
                "parent_finding_id": finding_id,
                "classification": classification,
                "description": finding["summary"],
                "status": "open",
            }
        finding["fingerprint"] = finding_fingerprint(finding)

    state["root_cause_stats"] = rebuild_root_cause_stats(state)

    state["requirements"]["unresolved_decisions"] = unresolved
    remaining_decision_findings = [
        finding_id
        for finding_id, finding in state["findings"].items()
        if finding["status"] == "decision_required"
    ]
    state["requirements_digest"] = requirements_digest(state["requirements"])
    state["current_diff_hash"] = snapshot["hash"]
    save_event(
        state_path,
        state,
        "decision_resolved",
        resolved_decisions=[item["decision"] for item in resolutions],
        finding_resolutions=[item["id"] for item in finding_resolutions],
    )
    if unresolved or remaining_decision_findings:
        policy_stop(
            state_path,
            state,
            "decision_required",
            "decisions_remain_unresolved",
            decisions=unresolved,
            findings=remaining_decision_findings,
        )
    enforce_deferred_design_review(
        state_path,
        state,
        cleared_gate="decision_required",
    )
    state["status"] = "active"
    state["phase"] = "initial_review"
    state["exit_reason"] = None
    set_active_phase(
        state,
        "validation" if state["initial_review_completed"] else "initial_review",
    )
    save_event(state_path, state, "decision_gate_cleared")
    emit(summarize(state, snapshot))


def readiness_failures(state: dict[str, Any], snapshot: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    unauthorized = sorted(set(snapshot["paths"]) - set(state["authorized_paths"]))
    if unauthorized:
        failures.append(
            "live diff contains paths without an extend-scope approval: "
            + ", ".join(unauthorized)
        )
    if not state["initial_review_completed"]:
        failures.append("initial review campaign is incomplete")
    if state["active_fix"] is not None:
        failures.append("a fix batch is still active")
    if snapshot["hash"] != state["current_diff_hash"]:
        failures.append("live diff does not match the latest recorded evidence")
    complete_campaign = state.get("complete_validation_campaign")
    if not (
        isinstance(complete_campaign, dict)
        and complete_campaign.get("diff_hash") == snapshot["hash"]
        and complete_campaign.get("validation_ids") == sorted(state["validations"])
        and complete_campaign.get("acceptance_ids") == sorted(state["acceptance"])
    ):
        failures.append(
            "no complete final validation campaign is recorded for the current diff hash"
        )
    failures.extend(
        f"acceptance {item_id} is {item['status']}"
        for item_id, item in state["acceptance"].items()
        if item["status"] != "pass" or item["diff_hash"] != snapshot["hash"]
    )
    failures.extend(
        f"review surface {item_id} is {item['status']}"
        for item_id, item in state["review_surfaces"].items()
        if item["status"] != "clean"
    )
    failures.extend(
        f"validation {item_id} is {item['status']}"
        for item_id, item in state["validations"].items()
        if item["status"] != "pass" or item["diff_hash"] != snapshot["hash"]
    )
    failures.extend(f"finding {finding_id} is {state['findings'][finding_id]['status']}" for finding_id in open_blocking_findings(state))
    failures.extend(
        f"finding {finding_id} verification is stale"
        for finding_id, finding in state["findings"].items()
        if (
            finding["status"] == "verified"
            and finding.get("verified_diff_hash") != snapshot["hash"]
        )
        or (
            finding["status"] == "false_positive"
            and finding.get("evidence_diff_hash") != snapshot["hash"]
        )
    )
    failures.extend(
        f"derived requirement {requirement_id} is {item['status']}"
        for requirement_id, item in state["derived_requirements"].items()
        if (
            item["status"] == "verified"
            and item.get("verified_diff_hash") != snapshot["hash"]
        )
        or (
            item["status"] == "false_positive"
            and item.get("evidence_diff_hash") != snapshot["hash"]
        )
        or item["status"] not in {"verified", "false_positive"}
    )
    if state["requirements"]["unresolved_decisions"]:
        failures.append("requirements contain unresolved decisions")
    if state["phase"] in {
        "decision_required",
        "design_review_required",
        "validation_failed",
        "blocked",
    }:
        failures.append(f"state phase is {state['phase']}")
    return failures


def command_evaluate_ready(args: argparse.Namespace) -> None:
    state_path = Path(args.state)
    state = load_state(state_path)
    require(state["status"] == "active", "READY evaluation requires active state")
    snapshot = verify_context(state)
    failures = readiness_failures(state, snapshot)
    if failures:
        save_event(state_path, state, "ready_rejected", failures=failures)
        emit(
            {
                "delivery_id": state["delivery_id"],
                "ready": False,
                "failures": failures,
            },
            POLICY_EXIT,
        )
    final_snapshot, ready_manifest = capture_stable_manifest(
        state,
        snapshot["paths"],
        label="READY",
    )
    if (
        final_snapshot["head"] != snapshot["head"]
        or final_snapshot["hash"] != snapshot["hash"]
        or final_snapshot["paths"] != snapshot["paths"]
    ):
        emit(
            {
                "error": "local diff changed during READY evaluation; rerun review and validation",
                "expected_diff_hash": snapshot["hash"],
                "actual_diff_hash": final_snapshot["hash"],
            },
            POLICY_EXIT,
        )
    state["status"] = "ready"
    state["phase"] = "ready_local_diff"
    state["ready"] = True
    state["ready_local_diff"] = True
    state["ready_diff_hash"] = final_snapshot["hash"]
    state["ready_manifest"] = ready_manifest
    state["current_diff_hash"] = final_snapshot["hash"]
    state["exit_reason"] = None
    save_event(state_path, state, "ready_accepted", diff_hash=final_snapshot["hash"])
    emit(summarize(state, final_snapshot))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init")
    init.add_argument("--state", required=True)
    init.add_argument("--project-root", required=True)
    init.add_argument("--requirements-file", required=True)
    init.set_defaults(func=command_init)

    status = subparsers.add_parser("status")
    status.add_argument("--state", required=True)
    status.set_defaults(func=command_status)

    review = subparsers.add_parser("record-review")
    review.add_argument("--state", required=True)
    review.add_argument("--result-file", required=True)
    review.set_defaults(func=command_record_review)

    extend_scope = subparsers.add_parser("extend-scope")
    extend_scope.add_argument("--state", required=True)
    extend_scope.add_argument("--result-file", required=True)
    extend_scope.set_defaults(func=command_extend_scope)

    begin_fix = subparsers.add_parser("begin-fix")
    begin_fix.add_argument("--state", required=True)
    begin_fix.add_argument("--finding-ids-file", required=True)
    begin_fix.add_argument("--batch-paths-file", required=True)
    begin_fix.add_argument(
        "--test-mode",
        choices=("none", "light", "deep"),
        default="none",
    )
    begin_fix.set_defaults(func=command_begin_fix)

    record_fix = subparsers.add_parser("record-fix")
    record_fix.add_argument("--state", required=True)
    record_fix.add_argument("--result-file", required=True)
    record_fix.set_defaults(func=command_record_fix)

    cancel_fix = subparsers.add_parser("cancel-fix")
    cancel_fix.add_argument("--state", required=True)
    cancel_fix.add_argument("--result-file", required=True)
    cancel_fix.set_defaults(func=command_cancel_fix)

    add_findings_parser = subparsers.add_parser("add-findings")
    add_findings_parser.add_argument("--state", required=True)
    add_findings_parser.add_argument("--result-file", required=True)
    add_findings_parser.set_defaults(func=command_add_findings)

    validation = subparsers.add_parser("record-validation")
    validation.add_argument("--state", required=True)
    validation.add_argument("--result-file", required=True)
    validation.set_defaults(func=command_record_validation)

    design = subparsers.add_parser("resolve-design-review")
    design.add_argument("--state", required=True)
    design.add_argument("--result-file", required=True)
    design.set_defaults(func=command_resolve_design_review)

    decision = subparsers.add_parser("resolve-decision")
    decision.add_argument("--state", required=True)
    decision.add_argument("--result-file", required=True)
    decision.set_defaults(func=command_resolve_decision)

    ready = subparsers.add_parser("evaluate-ready")
    ready.add_argument("--state", required=True)
    ready.set_defaults(func=command_evaluate_ready)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
