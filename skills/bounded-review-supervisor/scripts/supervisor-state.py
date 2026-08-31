#!/usr/bin/env python3
"""Deterministic state gate for bounded-review-supervisor."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn


BLOCKED_EXIT = 3
RETRY_EXIT = 4
STATE_VERSION = 4
MAX_FULL_REVIEW_ROUNDS = 4
OUTER_EXECUTION_POLICY = {
    "scope": "bounded-review-supervisor-command-layer",
    "disallowed_actions": [
        "automated_test_execution",
        "whole_repository_scan_analysis",
    ],
    "propagate_to_child_processes_or_subskills": False,
}
ALLOWED_DRIFT_RESULTS = {"aligned", "aligned_with_amendment"}
FINDING_STATUSES = {
    "actionable",
    "not_introduced",
    "out_of_scope",
    "low_signal",
    "needs_decision",
    "needs_clarification",
    "blocked",
}
BLOCKING_FINDING_STATUSES = {"needs_decision", "needs_clarification", "blocked"}
REQUIREMENT_FIELDS = {
    "original_request": str,
    "accepted_amendments": list,
    "test_mode": str,
    "test_mode_reason": str,
    "required_outcomes": list,
    "allowed_scope": list,
    "non_goals": list,
    "acceptance_criteria": list,
    "authorization_boundary": list,
    "authorized_scope_extensions": list,
    "unresolved_decisions": list,
}
TEST_MODE_ORDER = {"none": 0, "light": 1, "deep": 2}
SENSITIVE_FILE_EXTENSIONS = {".jks", ".kdbx", ".key", ".p12", ".pem", ".pfx"}
SENSITIVE_EXACT_NAMES = {
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".yarnrc",
    ".yarnrc.yml",
    "auth.json",
    "cookies.json",
    "cookies.txt",
    "kubeconfig",
}
SENSITIVE_DIRECTORY_COMPONENTS = {
    ".aws",
    ".azure",
    ".gnupg",
    ".ssh",
    "credentials",
}
SENSITIVE_PATH_SEQUENCES = {
    (".config", "gcloud"),
    (".config", "gh"),
    (".docker", "config.json"),
    (".kube", "config"),
}
CREDENTIAL_NOUNS = {
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "passwd",
    "password",
    "passwords",
    "secret",
    "secrets",
    "token",
    "tokens",
}
CREDENTIAL_QUALIFIERS = {
    "access",
    "api",
    "auth",
    "bearer",
    "client",
    "github",
    "private",
    "refresh",
    "session",
}
CREDENTIAL_DOMAIN_SUFFIXES = {"bucket", "parser", "policy"}
PRIVATE_KEY_NAMES = {"id_dsa", "id_ecdsa", "id_ed25519", "id_rsa"}


def emit(payload: dict[str, Any], exit_code: int = 0) -> NoReturn:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    raise SystemExit(exit_code)


def read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        emit({"error": f"cannot read {label}: {exc}"}, 2)


def load_state(path: Path) -> dict[str, Any]:
    value = read_json(path, "state JSON")
    if not isinstance(value, dict) or value.get("version") not in {2, 3, STATE_VERSION}:
        emit({"error": "unsupported or invalid state schema"}, 2)
    if value["version"] == 2:
        legacy_round = value.get("full_review_round", 0)
        if "active_review_round" not in value:
            if value.get("phase") == "reviewing":
                value["active_review_round"] = legacy_round
                value["full_review_round"] = max(0, legacy_round - 1)
            else:
                value["active_review_round"] = None
                if value.get("phase") == "ready_for_review" and value.get("review_failure_count", 0) > 0:
                    value["full_review_round"] = max(0, legacy_round - 1)
        value.setdefault(
            "review_attempt_count",
            legacy_round,
        )
        value.setdefault("latest_review_result", None)
        requirements = value.get("requirements")
        if isinstance(requirements, dict):
            requirements.setdefault("authorized_scope_extensions", [])
        value["version"] = 3
    if value["version"] == 3:
        configured_rounds = value.get("max_full_review_rounds")
        if type(configured_rounds) is not int or configured_rounds < 1:
            emit({"error": "invalid max_full_review_rounds in state"}, 2)
        value["max_full_review_rounds"] = min(configured_rounds, MAX_FULL_REVIEW_ROUNDS)
        value["outer_execution_policy"] = OUTER_EXECUTION_POLICY.copy()
        value["version"] = STATE_VERSION
    requirements = value.get("requirements")
    if isinstance(requirements, dict):
        requirements.setdefault("test_mode", "none")
        requirements.setdefault("test_mode_reason", "legacy_default")
    configured_rounds = value.get("max_full_review_rounds")
    if (
        type(configured_rounds) is not int
        or not 1 <= configured_rounds <= MAX_FULL_REVIEW_ROUNDS
    ):
        emit(
            {
                "error": (
                    "state max_full_review_rounds must be between "
                    f"1 and {MAX_FULL_REVIEW_ROUNDS}"
                )
            },
            2,
        )
    if value.get("outer_execution_policy") != OUTER_EXECUTION_POLICY:
        emit({"error": "state outer execution policy is missing or invalid"}, 2)
    full_review_round = value.get("full_review_round")
    active_review_round = value.get("active_review_round")
    if (
        type(full_review_round) is not int
        or not 0 <= full_review_round <= configured_rounds
        or (
            active_review_round is not None
            and (
                type(active_review_round) is not int
                or not 1 <= active_review_round <= configured_rounds
            )
        )
    ):
        emit({"error": "state review round counters are invalid"}, 2)
    return value


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def append_event(state: dict[str, Any], event: str, **details: Any) -> None:
    state["history"].append({"event": event, **details})


def block(path: Path, state: dict[str, Any], reason: str, **details: Any) -> NoReturn:
    state["status"] = "blocked"
    state["phase"] = "stopped"
    state["exit_reason"] = reason
    state.update(details)
    append_event(state, "blocked", reason=reason, **details)
    save_state(path, state)
    emit(state, BLOCKED_EXIT)


def ensure_active(state: dict[str, Any]) -> None:
    if state["status"] != "active":
        emit(state, BLOCKED_EXIT)


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


def resolve_git_root(candidate: Path) -> Path:
    root = candidate.resolve()
    if not root.is_dir():
        raise ValueError(f"project root is not a directory: {root}")
    try:
        discovered = Path(os.fsdecode(run_git(root, "rev-parse", "--show-toplevel")).strip()).resolve()
    except ValueError as exc:
        raise ValueError(f"project root is not a Git repository: {root}") from exc
    if discovered != root:
        raise ValueError(f"project root must be the Git repository root: {discovered}")
    return root


def current_head(root: Path) -> str:
    try:
        return os.fsdecode(run_git(root, "rev-parse", "HEAD")).strip()
    except ValueError as exc:
        raise ValueError("Git repository must have a valid HEAD") from exc


def verify_repository(path: Path, state: dict[str, Any]) -> Path:
    try:
        root = resolve_git_root(Path(state["project_root"]))
        head = current_head(root)
    except ValueError as exc:
        block(path, state, "repository_unavailable", repository_error=str(exc))
    if head != state["baseline_head"]:
        block(
            path,
            state,
            "baseline_head_changed",
            expected_head=state["baseline_head"],
            actual_head=head,
        )
    return root


def normalize_repo_path(value: str) -> str:
    candidate = PurePosixPath(value)
    if (
        not value
        or candidate.is_absolute()
        or value.startswith("./")
        or value.endswith("/")
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ValueError(f"invalid repository-relative path: {value}")
    return candidate.as_posix()


def collect_workspace_paths(root: Path) -> dict[str, Any]:
    staged_raw = run_git(
        root,
        "diff",
        "--cached",
        "--no-renames",
        "--name-only",
        "-z",
        "HEAD",
        "--",
    )
    unstaged_raw = run_git(root, "diff", "--no-renames", "--name-only", "-z", "--")
    untracked_raw = run_git(root, "ls-files", "--others", "--exclude-standard", "-z")

    def decode_paths(raw: bytes) -> list[str]:
        return sorted(
            {
                normalize_repo_path(os.fsdecode(item))
                for item in raw.split(b"\0")
                if item
            }
        )

    staged_paths = decode_paths(staged_raw)
    unstaged_paths = decode_paths(unstaged_raw)
    untracked_paths = decode_paths(untracked_raw)
    return {
        "head": current_head(root),
        "staged_paths": staged_paths,
        "unstaged_paths": unstaged_paths,
        "untracked_paths": untracked_paths,
        "changed_paths": sorted(set(staged_paths + unstaged_paths + untracked_paths)),
    }


def changed_paths(root: Path) -> list[str]:
    return collect_workspace_paths(root)["changed_paths"]


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def read_screened_diff(
    root: Path,
    diff_args: tuple[str, ...],
    screened_paths: list[str],
) -> bytes:
    if not screened_paths:
        return b""
    literal_pathspecs = [f":(literal){item}" for item in screened_paths]
    return run_git(root, *diff_args, "--", *literal_pathspecs)


def validate_requirements(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        emit({"error": "requirements file must contain a JSON object"}, 2)
    for field, expected_type in REQUIREMENT_FIELDS.items():
        if not isinstance(value.get(field), expected_type):
            emit({"error": f"requirements field {field} must be {expected_type.__name__}"}, 2)
    if not value["original_request"].strip():
        emit({"error": "original_request must not be empty"}, 2)
    if value["test_mode"] not in TEST_MODE_ORDER:
        emit({"error": "requirements test_mode must be none, light, or deep"}, 2)
    if not value["test_mode_reason"].strip():
        emit({"error": "requirements test_mode_reason must not be empty"}, 2)
    for field, expected_type in REQUIREMENT_FIELDS.items():
        if expected_type is list and any(not isinstance(item, str) for item in value[field]):
            emit({"error": f"requirements field {field} must contain strings"}, 2)
    try:
        value["authorized_scope_extensions"] = sorted(
            {normalize_repo_path(item) for item in value["authorized_scope_extensions"]}
        )
    except ValueError as exc:
        emit({"error": str(exc)}, 2)
    if value["unresolved_decisions"]:
        emit({"error": "requirements contain unresolved decisions"}, 2)
    return value


def state_path_is_temporary(path: Path) -> bool:
    resolved = path.resolve()
    candidates = {Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve(), Path("/private/tmp").resolve()}
    return any(resolved == root or root in resolved.parents for root in candidates)


def compute_workspace_hash(
    root: Path,
    path_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    paths = path_snapshot or collect_workspace_paths(root)
    staged = read_screened_diff(
        root,
        ("diff", "--cached", "--no-renames", "--binary", "HEAD"),
        paths["staged_paths"],
    )
    unstaged = read_screened_diff(
        root,
        ("diff", "--no-renames", "--binary"),
        paths["unstaged_paths"],
    )
    digest = hashlib.sha256()
    digest.update(b"head\0")
    digest.update(paths["head"].encode("ascii"))
    digest.update(b"staged\0")
    digest.update(staged)
    digest.update(b"unstaged\0")
    digest.update(unstaged)
    for normalized in paths["untracked_paths"]:
        candidate = root / normalized
        metadata = candidate.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"untracked path is not a regular file: {normalized}")
        digest.update(b"untracked-metadata\0")
        digest.update(normalized.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(
            json.dumps(
                {
                    "mode": stat.S_IFMT(metadata.st_mode) | stat.S_IMODE(metadata.st_mode),
                    "size": metadata.st_size,
                    "mtime_ns": metadata.st_mtime_ns,
                    "ctime_ns": metadata.st_ctime_ns,
                },
                sort_keys=True,
            ).encode()
        )
    if collect_workspace_paths(root) != paths:
        raise ValueError("workspace paths changed while computing snapshot")
    return {
        "project_root": str(root),
        "head": paths["head"],
        "diff_hash": digest.hexdigest(),
        "untracked_hash_mode": "metadata-only",
        "staged_paths": paths["staged_paths"],
        "unstaged_paths": paths["unstaged_paths"],
        "untracked_paths": paths["untracked_paths"],
        "changed_paths": paths["changed_paths"],
    }


def require_phase(state: dict[str, Any], expected: str) -> None:
    if state["phase"] != expected:
        emit({"error": f"cannot perform operation during phase {state['phase']}; expected {expected}"}, 2)


def command_init(args: argparse.Namespace) -> None:
    state_path = Path(args.state)
    if state_path.exists():
        emit({"error": f"state file already exists: {state_path}"}, 2)
    if not state_path_is_temporary(state_path):
        emit({"error": "state file must be inside a system temporary directory"}, 2)
    if not 1 <= args.max_rounds <= MAX_FULL_REVIEW_ROUNDS:
        emit(
            {
                "error": (
                    f"max rounds must be between 1 and {MAX_FULL_REVIEW_ROUNDS}"
                )
            },
            2,
        )
    try:
        project_root = resolve_git_root(Path(args.project_root))
        head = current_head(project_root)
    except ValueError as exc:
        emit({"error": str(exc)}, 2)
    resolved_state_path = state_path.resolve()
    if resolved_state_path == project_root or project_root in resolved_state_path.parents:
        emit({"error": "state file must be outside project root"}, 2)
    if args.baseline_head != head:
        emit({"error": f"baseline HEAD must equal current HEAD {head}"}, 2)
    requirements = validate_requirements(read_json(Path(args.requirements_file), "requirements JSON"))
    scope = changed_paths(project_root)
    state = {
        "version": STATE_VERSION,
        "project_root": str(project_root),
        "baseline_head": head,
        "requirements": requirements,
        "requirement_digest": canonical_digest(requirements),
        "initial_scope_files": scope,
        "approved_scope_files": scope.copy(),
        "scope_extensions": [],
        "max_full_review_rounds": args.max_rounds,
        "outer_execution_policy": OUTER_EXECUTION_POLICY.copy(),
        "full_review_round": 0,
        "active_review_round": None,
        "review_attempt_count": 0,
        "total_fix_rounds": 0,
        "status": "active",
        "phase": "ready_for_review",
        "exit_reason": None,
        "review_failure_count": 0,
        "last_review_diff_hash": None,
        "last_post_fix_diff_hash": None,
        "previous_actionable_fingerprints": [],
        "finding_streaks": {},
        "pending_actionable_fingerprints": [],
        "latest_findings": [],
        "latest_fix_result": None,
        "history": [],
    }
    append_event(state, "initialized", initial_scope_files=scope)
    save_state(state_path, state)
    emit(state)


def command_status(args: argparse.Namespace) -> None:
    emit(load_state(Path(args.state)))


def command_hash_diff(args: argparse.Namespace) -> None:
    path = Path(args.state)
    state = load_state(path)
    ensure_active(state)
    root = verify_repository(path, state)
    paths = collect_workspace_paths(root)
    reject_unsafe_review_surface(path, state, paths)
    emit(capture_workspace_hash(path, state, root, paths, "workspace_snapshot_failed"))


def command_extend_scope(args: argparse.Namespace) -> None:
    path = Path(args.state)
    state = load_state(path)
    ensure_active(state)
    allowed_phases = (
        {"ready_for_fix"}
        if args.reason == "direct_dependency"
        else {"ready_for_review", "ready_for_fix"}
    )
    if state["phase"] not in allowed_phases:
        emit(
            {
                "error": (
                    f"cannot extend scope for {args.reason} during phase {state['phase']}; "
                    f"expected {' or '.join(sorted(allowed_phases))}"
                )
            },
            2,
        )
    root = verify_repository(path, state)
    values = read_json(Path(args.paths_file), "scope paths JSON")
    if not isinstance(values, list) or not values or any(not isinstance(item, str) for item in values):
        emit({"error": "scope paths file must contain a non-empty JSON array of strings"}, 2)
    normalized = sorted({normalize_repo_path(item) for item in values})
    if args.reason == "user_authorized":
        frozen_authorized = set(state["requirements"]["authorized_scope_extensions"])
        if not set(normalized) <= frozen_authorized:
            emit({"error": "user_authorized paths are not present in the frozen requirements baseline"}, 2)
    already_changed = set(changed_paths(root))
    for item in normalized:
        if args.reason == "direct_dependency":
            try:
                run_git(root, "ls-files", "--error-unmatch", "--", item)
            except ValueError:
                emit({"error": f"direct dependency must be an existing tracked file: {item}"}, 2)
            if item in already_changed:
                emit({"error": f"direct dependency must be authorized before it is modified: {item}"}, 2)
    approved = sorted(set(state["approved_scope_files"]) | set(normalized))
    state["approved_scope_files"] = approved
    extension = {"paths": normalized, "reason": args.reason}
    if args.reason == "user_authorized":
        extension["authorization"] = {
            "source": "requirements_baseline",
            "requirement_digest": state["requirement_digest"],
            "authorized_paths": normalized,
        }
    state["scope_extensions"].append(extension)
    append_event(state, "scope_extended", **extension)
    save_state(path, state)
    emit(state)


def validate_drift_check(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        emit({"error": "drift check file must contain a JSON object"}, 2)
    for field in (
        "result",
        "requirement_digest",
        "original_request",
        "accepted_amendments",
        "current_diff_files",
        "planned_actions",
        "alignment_summary",
        "untracked_review_screening",
    ):
        if field not in value:
            emit({"error": f"drift check is missing {field}"}, 2)
    if not isinstance(value["result"], str):
        emit({"error": "drift check result must be a string"}, 2)
    if not isinstance(value["current_diff_files"], list) or any(
        not isinstance(item, str) for item in value["current_diff_files"]
    ):
        emit({"error": "current_diff_files must be an array of strings"}, 2)
    if not isinstance(value["planned_actions"], list) or any(
        not isinstance(item, str) for item in value["planned_actions"]
    ):
        emit({"error": "planned_actions must be an array of strings"}, 2)
    if not isinstance(value["original_request"], str) or not value["original_request"].strip():
        emit({"error": "drift check original_request must not be empty"}, 2)
    if not isinstance(value["accepted_amendments"], list) or any(
        not isinstance(item, str) for item in value["accepted_amendments"]
    ):
        emit({"error": "accepted_amendments must be an array of strings"}, 2)
    if not isinstance(value["alignment_summary"], str) or not value["alignment_summary"].strip():
        emit({"error": "drift check alignment_summary must not be empty"}, 2)
    screening = value["untracked_review_screening"]
    if not isinstance(screening, list):
        emit({"error": "untracked_review_screening must be an array"}, 2)
    for item in screening:
        if not isinstance(item, dict):
            emit({"error": "each untracked screening entry must be an object"}, 2)
        if not isinstance(item.get("path"), str):
            emit({"error": "untracked screening path must be a string"}, 2)
        if item.get("classification") != "safe_to_review":
            emit({"error": "untracked screening classification must be safe_to_review"}, 2)
        if not isinstance(item.get("reason"), str) or not item["reason"].strip():
            emit({"error": "untracked screening reason must not be empty"}, 2)
    return value


def sensitive_path_reason(relative: str) -> str | None:
    path = PurePosixPath(relative)
    name = path.name.lower()
    ordered_parts = tuple(part.lower() for part in path.parts)
    lower_parts = set(ordered_parts)
    if any(part.startswith(".env") for part in ordered_parts):
        return "environment file pattern"
    if any(
        name == exact_name or name.startswith(f"{exact_name}.")
        for exact_name in SENSITIVE_EXACT_NAMES
    ):
        return "credential or cookie filename"
    if lower_parts & SENSITIVE_DIRECTORY_COMPONENTS:
        return "credential configuration directory"
    if any(
        ordered_parts[index : index + len(sequence) - 1] == sequence[:-1]
        and (
            ordered_parts[index + len(sequence) - 1] == sequence[-1]
            or ordered_parts[index + len(sequence) - 1].startswith(
                f"{sequence[-1]}."
            )
        )
        for sequence in SENSITIVE_PATH_SEQUENCES
        for index in range(len(ordered_parts) - len(sequence) + 1)
    ):
        return "authentication configuration path"
    if path.suffix.lower() in SENSITIVE_FILE_EXTENSIONS:
        return "credential file extension"
    stem = path.stem
    camel_split_stem = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", stem)
    camel_split_stem = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", camel_split_stem)
    semantic_parts = tuple(
        item
        for item in re.split(r"[^a-z0-9]+", camel_split_stem.lower())
        if item
    )
    semantic_set = set(semantic_parts)
    if name in PRIVATE_KEY_NAMES:
        return "private key filename"
    credential_nouns = semantic_set & CREDENTIAL_NOUNS
    safe_domain_name = (
        bool(semantic_parts)
        and semantic_parts[0] in CREDENTIAL_NOUNS
        and len(semantic_parts) > 1
        and set(semantic_parts[1:]) <= CREDENTIAL_DOMAIN_SUFFIXES
    )
    if semantic_parts and (
        (credential_nouns and not safe_domain_name)
        or "apikey" in semantic_set
        or {"api", "key"} <= semantic_set
        or {"private", "key"} <= semantic_set
    ):
        return "credential-semantic filename"
    return None


def untracked_mode_reason(mode: int) -> str | None:
    if not stat.S_ISREG(mode):
        return "untracked path must be a regular file"
    return None


def untracked_file_reason(root: Path, relative: str) -> str | None:
    return untracked_mode_reason((root / relative).lstat().st_mode)


def reject_unsafe_review_surface(
    path: Path,
    state: dict[str, Any],
    path_snapshot: dict[str, Any],
    *,
    sensitive_reason: str = "sensitive_changed_path",
    unsafe_file_reason: str = "unsafe_untracked_file",
) -> None:
    for relative in path_snapshot["changed_paths"]:
        reason = sensitive_path_reason(relative)
        if reason is not None:
            block(
                path,
                state,
                sensitive_reason,
                sensitive_path=relative,
                screening_reason=reason,
            )
    root = Path(state["project_root"])
    for relative in path_snapshot["untracked_paths"]:
        try:
            reason = untracked_file_reason(root, relative)
        except OSError:
            block(path, state, "review_workspace_changed_during_screening")
        if reason is not None:
            block(
                path,
                state,
                unsafe_file_reason,
                sensitive_path=relative,
                screening_reason=reason,
            )


def capture_workspace_hash(
    path: Path,
    state: dict[str, Any],
    root: Path,
    path_snapshot: dict[str, Any],
    failure_reason: str,
) -> dict[str, Any]:
    try:
        return compute_workspace_hash(root, path_snapshot)
    except (OSError, ValueError) as exc:
        block(path, state, failure_reason, workspace_error=str(exc))


def command_begin_review(args: argparse.Namespace) -> None:
    path = Path(args.state)
    state = load_state(path)
    ensure_active(state)
    require_phase(state, "ready_for_review")
    root = verify_repository(path, state)
    drift = validate_drift_check(read_json(Path(args.drift_check_file), "drift check JSON"))
    result = drift["result"]
    if result not in ALLOWED_DRIFT_RESULTS:
        block(path, state, f"requirement_{result}")
    if drift["requirement_digest"] != state["requirement_digest"]:
        block(path, state, "requirement_baseline_mismatch")
    if (
        drift["original_request"] != state["requirements"]["original_request"]
        or drift["accepted_amendments"] != state["requirements"]["accepted_amendments"]
    ):
        block(path, state, "requirement_source_mismatch")
    paths = collect_workspace_paths(root)
    actual_paths = paths["changed_paths"]
    try:
        reported_paths = sorted(
            {normalize_repo_path(item) for item in drift["current_diff_files"]}
        )
    except ValueError as exc:
        block(path, state, "invalid_drift_path", drift_path_error=str(exc))
    if reported_paths != actual_paths:
        block(
            path,
            state,
            "drift_check_scope_mismatch",
            actual_diff_files=actual_paths,
            reported_diff_files=reported_paths,
        )
    actual_untracked = paths["untracked_paths"]
    screening_by_path: dict[str, dict[str, Any]] = {}
    for item in drift["untracked_review_screening"]:
        try:
            normalized = normalize_repo_path(item["path"])
        except ValueError as exc:
            block(path, state, "invalid_drift_path", drift_path_error=str(exc))
        if normalized in screening_by_path:
            block(path, state, "untracked_screening_mismatch", duplicate_screening_path=normalized)
        screening_by_path[normalized] = item
    if sorted(screening_by_path) != actual_untracked:
        block(
            path,
            state,
            "untracked_screening_mismatch",
            actual_untracked_files=actual_untracked,
            screened_untracked_files=sorted(screening_by_path),
        )
    reject_unsafe_review_surface(path, state, paths)
    unexpected = sorted(set(actual_paths) - set(state["approved_scope_files"]))
    if unexpected:
        block(path, state, "scope_expanded_without_authorization", unexpected_scope_files=unexpected)
    if state["full_review_round"] >= state["max_full_review_rounds"]:
        block(path, state, "max_full_review_rounds_reached")
    workspace = capture_workspace_hash(
        path,
        state,
        root,
        paths,
        "workspace_snapshot_failed",
    )
    state["active_review_round"] = state["full_review_round"] + 1
    state["review_attempt_count"] += 1
    state["phase"] = "reviewing"
    state["last_review_diff_hash"] = workspace["diff_hash"]
    is_final = state["active_review_round"] == state["max_full_review_rounds"]
    append_event(
        state,
        "review_started",
        round=state["active_review_round"],
        diff_hash=workspace["diff_hash"],
        requirement_check=result,
        requirement_digest=state["requirement_digest"],
        diff_files=actual_paths,
        planned_actions=drift["planned_actions"],
        original_request=drift["original_request"],
        accepted_amendments=drift["accepted_amendments"],
        alignment_summary=drift["alignment_summary"],
        untracked_review_screening=drift["untracked_review_screening"],
        is_final_gate=is_final,
    )
    save_state(path, state)
    state["is_final_gate"] = is_final
    emit(state)


def validate_findings(
    value: Any,
    *,
    require_structured_fingerprint: bool = False,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("findings must contain a JSON array")
    fingerprints: set[str] = set()
    for finding in value:
        if not isinstance(finding, dict):
            raise ValueError("each finding must be a JSON object")
        for field in ("fingerprint", "status", "severity", "location", "summary"):
            if not isinstance(finding.get(field), str) or not finding[field].strip():
                raise ValueError(f"finding field {field} must be a non-empty string")
        if finding["status"] not in FINDING_STATUSES:
            raise ValueError(f"unsupported finding status: {finding['status']}")
        if finding["severity"] not in {"P0", "P1", "P2", "P3"}:
            raise ValueError(f"unsupported finding severity: {finding['severity']}")
        if require_structured_fingerprint:
            components = [" ".join(item.casefold().split()) for item in finding["fingerprint"].split("|")]
            if len(components) != 4 or any(not item for item in components):
                raise ValueError("finding fingerprint must use path|symbol|root-cause|trigger")
            location_path = re.sub(r":\d+(?:-\d+)?$", "", finding["location"]).casefold()
            if components[0] != location_path:
                raise ValueError("finding fingerprint path must match the line-free location path")
            finding["fingerprint"] = hashlib.sha256("\0".join(components).encode("utf-8")).hexdigest()
        if finding["fingerprint"] in fingerprints:
            raise ValueError(f"duplicate finding fingerprint: {finding['fingerprint']}")
        fingerprints.add(finding["fingerprint"])
    return value


NATIVE_FINDING_PATTERN = re.compile(
    r"^- \[(?P<severity>P[0-3])\] (?P<title>.+?) \u2014 (?P<location>.+?:\d+(?:-\d+)?)\s*$",
    re.MULTILINE,
)
NATIVE_CLEAN_PATTERNS = (
    re.compile(
        r"(?:After reviewing (?:the )?(?:complete|entire|full) "
        r"(?:current )?(?:diff|changes?|review scope),\s+)?"
        r"(?:I |We )(?:did not|didn't) find any "
        r"(?:actionable )?(?:issues|findings|problems) "
        r"(?:in|across) (?:the )?(?:complete|entire|full) "
        r"(?:current )?(?:diff|changes?|review scope)[.!]?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:I |We )?found no "
        r"(?:actionable )?(?:issues|findings|problems) "
        r"(?:in|across) (?:the )?(?:complete|entire|full) "
        r"(?:current )?(?:diff|changes?|review scope)[.!]?",
        re.IGNORECASE,
    ),
    re.compile(
        r"No (?:evident |actionable |material )?"
        r"(?:correctness or compatibility )?(?:issues|findings|problems) "
        r"(?:were found )?(?:in|across) (?:the )?"
        r"(?:complete|entire|full) (?:current )?"
        r"(?:diff|changes?|review scope)[.!]?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:The )?(?:only change|complete (?:current )?(?:diff|changes?)|"
        r"entire (?:current )?(?:diff|changes?)|"
        r"full (?:current )?(?:diff|changes?)).+,\s+"
        r"with no (?:evident |actionable |material )?"
        r"(?:correctness or compatibility )?(?:issues|findings|problems)[.!]?",
        re.IGNORECASE,
    ),
    re.compile(
        r"未发现(?:由|在|针对)[^。\n]{0,40}"
        r"(?:全部|完整|当前完整|allowlisted|已列入允许范围)[^。\n]{0,40}"
        r"(?:可操作|需要修复)[^。\n]{0,20}(?:缺陷|问题|发现)"
        r"[。.]?(?:按要求仅进行了只读静态检查。[。]?)?"
    ),
    re.compile(
        r"没有发现(?:由|在|针对)[^。\n]{0,40}"
        r"(?:全部|完整|当前完整|allowlisted|已列入允许范围)[^。\n]{0,40}"
        r"(?:可操作|需要修复)[^。\n]{0,20}(?:缺陷|问题|发现)"
        r"[。.]?(?:按要求仅进行了只读静态检查。[。]?)?"
    ),
)
NATIVE_COVERAGE_SENTENCE_PATTERNS = (
    re.compile(
        r"^(?:I|we|(?:the )?reviewer)\s+(?:"
        r"(?:could not|couldn't|cannot|can't|failed to|"
        r"(?:was|were|am|are|is) unable to)"
        r".*\b(?:inspect|review|read|access|cover)|"
        r"(?:did not|didn't|skipped|omitted|only)"
        r".*\b(?:inspect|inspected|review|reviewed|reviewing|"
        r"check|checked|checking|read|access|cover|covered))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:unable to|failed to)\s+"
        r"(?:inspect|review|read|access|cover)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:the )?(?:review|inspection|coverage|diff)\s+.*(?:"
        r"could not|couldn't|cannot|can't|failed|unable|"
        r"incomplete|partial|limited|unavailable|not complete|"
        r"(?:did|does|do) not include|"
        r"exclud(?:e|es|ed|ing)|omit(?:s|ted|ting)?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:the )?(?:rest|remainder|part|portion)\s+of\s+"
        r"(?:the\s+)?(?:diff|changes?|files?).*"
        r"(?:not reviewed|not inspected|unreviewed|unchecked)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:(?:some|certain|the)\s+)?"
        r"(?:staged|unstaged|untracked|generated|changed|modified)\s+"
        r"(?:files?|changes?)\s+(?:were|was|are|is)\s+"
        r"(?:not reviewed|not inspected|not checked|"
        r"unreviewed|uninspected|unchecked)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:only (?:reviewed|inspected|checked)|"
        r"(?:reviewed|inspected|checked) only|"
        r"(?:review|inspection|coverage) (?:was |is )?limited to|"
        r"(?:I|we) could (?:inspect|review|access) only|"
        r"(?:I|we) (?:was|were|am|are) only able to "
        r"(?:inspect|review|check|read|access|cover))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:(?:我|我们|审查者|reviewer)(?:无法|不能|未能).*"
        r"(?:审查|检查|读取|访问|覆盖|完成)|"
        r"(?:本次|此次|该)(?:审查|检查).*(?:无法|不能|未能|未完成)|"
        r"(?:无法|不能|未能).*(?:读取|访问).*(?:diff|变更|文件))",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:(?:(?:部分|其余|剩余)?(?:文件|变更|差异|范围)|"
        r"(?:已暂存|未暂存|未跟踪|生成|已修改)(?:文件|变更))"
        r".*(?:未审查|未检查)|"
        r"(?:覆盖不完整|仅审查))"
    ),
)


def native_coverage_caveat(text: str) -> bool:
    sentences = (
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?。！？])\s+|\n+", text)
    )
    return any(
        pattern.search(sentence)
        for sentence in sentences
        if sentence
        for pattern in NATIVE_COVERAGE_SENTENCE_PATTERNS
    )


def parse_review_output(final_output: bytes) -> list[dict[str, Any]]:
    text = final_output.decode("utf-8").strip()
    if not text:
        raise ValueError("review output is empty")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if parsed is not None:
        if not isinstance(parsed, dict) or set(parsed) != {"coverage_complete", "findings"}:
            raise ValueError("review output must contain only coverage_complete and findings")
        if parsed["coverage_complete"] is not True:
            raise ValueError("structured review output must assert complete coverage")
        return validate_findings(
            parsed["findings"],
            require_structured_fingerprint=True,
        )

    if native_coverage_caveat(text):
        raise ValueError("native review output reports incomplete coverage")

    matches = list(NATIVE_FINDING_PATTERN.finditer(text))
    if matches:
        findings: list[dict[str, Any]] = []
        for index, match in enumerate(matches):
            body_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            body = " ".join(
                line.strip()
                for line in text[match.end() : body_end].splitlines()
                if line.strip()
            )
            severity = match.group("severity")
            title = match.group("title").strip()
            location = match.group("location").strip()
            stable_location = re.sub(r":\d+(?:-\d+)?$", "", location)
            stable_title = " ".join(title.casefold().split())
            fingerprint_source = f"{stable_location}\0{stable_title}".encode("utf-8")
            findings.append(
                {
                    "fingerprint": hashlib.sha256(fingerprint_source).hexdigest(),
                    "status": "actionable",
                    "severity": severity,
                    "location": location,
                    "summary": f"{title}: {body}" if body else title,
                }
            )
        return validate_findings(findings)

    if any(pattern.fullmatch(text) for pattern in NATIVE_CLEAN_PATTERNS):
        return []
    raise ValueError("unrecognized native review output")


def review_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "coverage_complete": {
                "type": "boolean",
                "const": True,
                "description": (
                    "Assert true only after reviewing the complete current staged, "
                    "unstaged, and untracked diff without coverage limitations"
                ),
            },
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "fingerprint": {
                            "type": "string",
                            "pattern": r"^[^|\n]+\|[^|\n]+\|[^|\n]+\|[^|\n]+$",
                            "description": (
                                "Stable path|symbol|root-cause|trigger identity; "
                                "exclude severity and line numbers"
                            ),
                        },
                        "status": {"type": "string", "enum": sorted(FINDING_STATUSES)},
                        "severity": {
                            "type": "string",
                            "enum": ["P0", "P1", "P2", "P3"],
                        },
                        "location": {"type": "string", "minLength": 1},
                        "summary": {"type": "string", "minLength": 1},
                    },
                    "required": ["fingerprint", "status", "severity", "location", "summary"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["coverage_complete", "findings"],
        "additionalProperties": False,
    }


def build_review_command(
    reviewer: str,
    schema_path: Path,
    final_path: Path,
) -> list[str]:
    return [
        reviewer,
        "exec",
        "--sandbox",
        "read-only",
        "review",
        "--uncommitted",
        "--ephemeral",
        "--json",
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(final_path),
    ]


def verify_frozen_review_workspace(path: Path, state: dict[str, Any], root: Path) -> None:
    paths = collect_workspace_paths(root)
    actual_paths = paths["changed_paths"]
    unexpected = sorted(set(actual_paths) - set(state["approved_scope_files"]))
    if unexpected:
        block(
            path,
            state,
            "scope_expanded_before_reviewer_launch",
            unexpected_scope_files=unexpected,
        )
    reject_unsafe_review_surface(
        path,
        state,
        paths,
        sensitive_reason="sensitive_path_before_reviewer_launch",
        unsafe_file_reason="unsafe_untracked_file_before_reviewer_launch",
    )
    workspace = capture_workspace_hash(
        path,
        state,
        root,
        paths,
        "review_workspace_changed_before_reviewer_launch",
    )
    if workspace["diff_hash"] != state["last_review_diff_hash"]:
        block(
            path,
            state,
            "review_workspace_changed_before_reviewer_launch",
            expected_diff_hash=state["last_review_diff_hash"],
            actual_diff_hash=workspace["diff_hash"],
        )


def command_execute_review(args: argparse.Namespace) -> None:
    path = Path(args.state)
    state = load_state(path)
    ensure_active(state)
    require_phase(state, "reviewing")
    root = verify_repository(path, state)
    verify_frozen_review_workspace(path, state, root)
    reviewer = shutil.which("codex")
    execution_error = None
    with tempfile.TemporaryDirectory(prefix="bounded-review-exec-") as temp_dir:
        schema_path = Path(temp_dir) / "review-schema.json"
        final_path = Path(temp_dir) / "review-final.json"
        schema_path.write_text(json.dumps(review_output_schema()), encoding="utf-8")
        if reviewer is None:
            return_code, stdout, stderr = 127, b"", b"codex executable not found"
        else:
            try:
                completed = subprocess.run(
                    build_review_command(reviewer, schema_path, final_path),
                    cwd=root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                return_code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
            except OSError as exc:
                return_code, stdout, stderr = 127, b"", str(exc).encode()
        try:
            final_output = final_path.read_bytes()
        except OSError:
            final_output = b""

    findings: list[dict[str, Any]] = []
    review_complete = False
    if return_code == 0 and final_output:
        try:
            findings = parse_review_output(final_output)
            review_complete = True
        except (UnicodeDecodeError, ValueError) as exc:
            execution_error = str(exc)

    raw_output = b"stdout\0" + stdout + b"\0stderr\0" + stderr + b"\0final\0" + final_output
    result = {
        "schema_version": 1,
        "reviewer": "codex-exec-review",
        "reviewer_executable": reviewer,
        "review_complete": review_complete,
        "reviewer_exit_code": return_code,
        "reviewed_diff_hash": state["last_review_diff_hash"],
        "coverage_status": "covered" if review_complete else "uncovered",
        "raw_output_sha256": hashlib.sha256(raw_output).hexdigest(),
        "raw_output_bytes": len(raw_output),
        "findings": findings,
    }
    if execution_error is not None:
        result["execution_error"] = execution_error
    state["latest_review_result"] = result
    root = verify_repository(path, state)
    paths = collect_workspace_paths(root)
    actual_paths = paths["changed_paths"]
    unexpected = sorted(set(actual_paths) - set(state["approved_scope_files"]))
    if unexpected:
        block(
            path,
            state,
            "scope_expanded_during_review",
            unexpected_scope_files=unexpected,
        )
    reject_unsafe_review_surface(
        path,
        state,
        paths,
        sensitive_reason="sensitive_path_during_review",
        unsafe_file_reason="unsafe_untracked_file_during_review",
    )
    workspace = capture_workspace_hash(
        path,
        state,
        root,
        paths,
        "review_workspace_changed",
    )
    if workspace["diff_hash"] != state["last_review_diff_hash"]:
        block(
            path,
            state,
            "review_workspace_changed",
            expected_diff_hash=state["last_review_diff_hash"],
            actual_diff_hash=workspace["diff_hash"],
        )
    if not result["review_complete"]:
        state["review_failure_count"] += 1
        append_event(
            state,
            "review_incomplete",
            round=state["active_review_round"],
            count=state["review_failure_count"],
            reviewer=result["reviewer"],
            reviewer_exit_code=result["reviewer_exit_code"],
            coverage_status=result["coverage_status"],
            raw_output_sha256=result["raw_output_sha256"],
            raw_output_bytes=result["raw_output_bytes"],
        )
        if state["review_failure_count"] >= 2:
            block(path, state, "reviewer_failed_twice")
        state["active_review_round"] = None
        state["phase"] = "ready_for_review"
        save_state(path, state)
        emit(state, RETRY_EXIT)

    state["review_failure_count"] = 0
    state["full_review_round"] = state["active_review_round"]
    state["active_review_round"] = None
    findings = result["findings"]
    state["latest_findings"] = findings
    blocking = [finding for finding in findings if finding["status"] in BLOCKING_FINDING_STATUSES]
    actionable = sorted(finding["fingerprint"] for finding in findings if finding["status"] == "actionable")
    append_event(
        state,
        "review_recorded",
        round=state["full_review_round"],
        actionable_fingerprints=actionable,
        findings=findings,
        reviewer=result["reviewer"],
        reviewed_diff_hash=result["reviewed_diff_hash"],
        coverage_status=result["coverage_status"],
        raw_output_sha256=result["raw_output_sha256"],
        raw_output_bytes=result["raw_output_bytes"],
    )
    if blocking:
        block(path, state, "unresolved_review_items", unresolved_review_items=blocking)
    if not actionable:
        state["status"] = "complete"
        state["phase"] = "stopped"
        state["exit_reason"] = "full_diff_review_converged"
        save_state(path, state)
        emit(state)
    if state["full_review_round"] >= state["max_full_review_rounds"]:
        state["pending_actionable_fingerprints"] = actionable
        block(path, state, "max_full_review_rounds_reached")

    previous = set(state["previous_actionable_fingerprints"])
    streaks: dict[str, int] = {}
    for fingerprint in actionable:
        streaks[fingerprint] = state["finding_streaks"].get(fingerprint, 0) + 1 if fingerprint in previous else 1
    repeated = sorted(fingerprint for fingerprint, count in streaks.items() if count >= 3)
    state["finding_streaks"] = streaks
    state["pending_actionable_fingerprints"] = actionable
    if repeated:
        block(
            path,
            state,
            "repeated_finding_without_progress",
            repeated_finding_fingerprints=repeated,
        )
    state["phase"] = "ready_for_fix"
    save_state(path, state)
    emit(state)


def validate_fix_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        emit({"error": "fix result file must contain a JSON object"}, 2)
    required = {
        "status": str,
        "fix_rounds": int,
        "unresolved_count": int,
        "coverage_status": str,
        "validation_status": str,
        "requested_test_mode": str,
        "effective_test_mode": str,
        "test_mode_reason": str,
    }
    for field, expected in required.items():
        if type(value.get(field)) is not expected:
            emit({"error": f"fix result field {field} must be {expected.__name__}"}, 2)
    if value["fix_rounds"] < 0 or value["unresolved_count"] < 0:
        emit({"error": "fix_rounds and unresolved_count cannot be negative"}, 2)
    if value["coverage_status"] not in {"covered", "partial", "uncovered"}:
        emit({"error": "invalid coverage_status"}, 2)
    if value["validation_status"] not in {"unverified", "focused", "complete", "blocked"}:
        emit({"error": "invalid validation_status"}, 2)
    if (
        value["requested_test_mode"] not in TEST_MODE_ORDER
        or value["effective_test_mode"] not in TEST_MODE_ORDER
    ):
        emit({"error": "invalid test mode in fix result"}, 2)
    if not value["test_mode_reason"].strip():
        emit({"error": "fix result test_mode_reason must not be empty"}, 2)
    if TEST_MODE_ORDER[value["effective_test_mode"]] < TEST_MODE_ORDER[value["requested_test_mode"]]:
        emit({"error": "effective_test_mode cannot downgrade requested_test_mode"}, 2)
    return value


def command_record_fix(args: argparse.Namespace) -> None:
    path = Path(args.state)
    state = load_state(path)
    ensure_active(state)
    require_phase(state, "ready_for_fix")
    if state["full_review_round"] >= state["max_full_review_rounds"]:
        block(path, state, "max_full_review_rounds_reached")
    root = verify_repository(path, state)
    result = validate_fix_result(read_json(Path(args.skill_result_file), "fix result JSON"))
    state["latest_fix_result"] = result
    if result["requested_test_mode"] != state["requirements"]["test_mode"]:
        block(
            path,
            state,
            "code_review_fix_loop_test_mode_mismatch",
            expected_test_mode=state["requirements"]["test_mode"],
            reported_test_mode=result["requested_test_mode"],
        )
    if result["status"] != "complete":
        block(path, state, f"code_review_fix_loop_{result['status']}")
    if result["unresolved_count"] != 0:
        block(path, state, "code_review_fix_loop_unresolved")
    if result["validation_status"] == "blocked":
        block(path, state, "code_review_fix_loop_validation_blocked")
    paths = collect_workspace_paths(root)
    actual_paths = paths["changed_paths"]
    unexpected = sorted(set(actual_paths) - set(state["approved_scope_files"]))
    if unexpected:
        block(path, state, "scope_expanded_without_authorization", unexpected_scope_files=unexpected)
    reject_unsafe_review_surface(path, state, paths)
    workspace = capture_workspace_hash(
        path,
        state,
        root,
        paths,
        "workspace_snapshot_failed_after_fix",
    )
    if workspace["diff_hash"] == state["last_review_diff_hash"]:
        block(path, state, "diff_unchanged_after_fix")
    state["total_fix_rounds"] += result["fix_rounds"]
    state["previous_actionable_fingerprints"] = state["pending_actionable_fingerprints"]
    state["pending_actionable_fingerprints"] = []
    state["last_post_fix_diff_hash"] = workspace["diff_hash"]
    state["phase"] = "ready_for_review"
    append_event(
        state,
        "fix_recorded",
        diff_hash=workspace["diff_hash"],
        fix_result=result,
        total_fix_rounds=state["total_fix_rounds"],
    )
    save_state(path, state)
    emit(state)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init")
    init.add_argument("--state", required=True)
    init.add_argument("--project-root", required=True)
    init.add_argument("--baseline-head", required=True)
    init.add_argument("--requirements-file", required=True)
    init.add_argument("--max-rounds", type=int, default=MAX_FULL_REVIEW_ROUNDS)
    init.set_defaults(handler=command_init)

    status = commands.add_parser("status")
    status.add_argument("--state", required=True)
    status.set_defaults(handler=command_status)

    hash_diff = commands.add_parser("hash-diff")
    hash_diff.add_argument("--state", required=True)
    hash_diff.set_defaults(handler=command_hash_diff)

    extend = commands.add_parser("extend-scope")
    extend.add_argument("--state", required=True)
    extend.add_argument("--paths-file", required=True)
    extend.add_argument("--reason", required=True, choices=["direct_dependency", "user_authorized"])
    extend.set_defaults(handler=command_extend_scope)

    begin = commands.add_parser("begin-review")
    begin.add_argument("--state", required=True)
    begin.add_argument("--drift-check-file", required=True)
    begin.set_defaults(handler=command_begin_review)

    review = commands.add_parser("execute-review")
    review.add_argument("--state", required=True)
    review.set_defaults(handler=command_execute_review)

    fix = commands.add_parser("record-fix")
    fix.add_argument("--state", required=True)
    fix.add_argument("--skill-result-file", required=True)
    fix.set_defaults(handler=command_record_fix)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
