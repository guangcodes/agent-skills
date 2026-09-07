#!/usr/bin/env python3
"""Deterministic state gate for bounded-review-fix-supervisor."""

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
STATE_VERSION = 8
MAX_FIX_WINDOWS = 4
CHILD_FIX_ROUND_LIMIT = 5
SNAPSHOT_CONTRACT = "git-cumulative-diff-sha256-v1"
INITIAL_REVIEW_PROMPT = (
    "Perform a read-only static review of all current staged, unstaged, and untracked "
    "changes in this repository. Use git status and the cached and working-tree diffs "
    "to establish the complete scope. Report only discrete actionable findings "
    "introduced by these changes. Do not modify files. Do not inspect unrelated history "
    "or Git-ignored and credential files. Do not run test, lint, typecheck, build, "
    "integration, E2E, smoke, benchmark, Docker, network, or remote commands."
)
OUTER_EXECUTION_POLICY = {
    "scope": "bounded-review-fix-supervisor-command-layer",
    "disallowed_actions": [
        "automated_test_execution",
        "whole_repository_scan_analysis",
    ],
    "propagate_to_child_processes_or_subskills": False,
}
ALLOWED_DRIFT_RESULTS = {"aligned", "aligned_with_amendment"}
FINDING_STATUSES = {
    "actionable",
    "not_actionable",
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
    "required_outcomes": list,
    "allowed_scope": list,
    "non_goals": list,
    "acceptance_criteria": list,
    "authorization_boundary": list,
    "authorized_scope_extensions": list,
    "unresolved_decisions": list,
}
TEST_MODES = {"none", "light", "deep"}
EXIT_TEST_STATUSES = {"not_run", "passed", "failed", "blocked", "skipped"}
EXIT_TEST_SKIP_REASONS = {
    "user_disabled",
    "no_persistent_change",
    "no_applicable_automated_test",
    "blocked_before_exit_validation",
}
ALIGNMENT_OUTCOMES = {"continue", "redirect", "ask_developer", "blocked"}
REQUIREMENT_ALIGNMENT_VALUES = {"aligned", "misaligned", "uncertain"}
IMPLEMENTATION_COMPLETENESS_VALUES = {
    "complete",
    "fixable_gap",
    "decision_gap",
    "blocked",
}
DESIGN_CONVERGENCE_VALUES = {
    "convergent",
    "needs_redirect",
    "decision_required",
    "blocked",
}
MINIMALITY_VALUES = {"minimum_sufficient", "excess_or_redundant", "uncertain"}
ALIGNMENT_FINDING_KINDS = {
    "requirement_gap",
    "requirement_drift",
    "redundancy",
    "design_divergence",
    "missing_information",
    "authorization_boundary",
}
ALIGNMENT_DIRECTIVE_STATUSES = {"not_applicable", "resolved", "unresolved"}
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
    if not isinstance(value, dict):
        emit({"error": "unsupported or invalid state schema"}, 2)
    if value.get("version") != STATE_VERSION:
        emit(
            {
                "error": (
                    "legacy supervisor state cannot be resumed after the alignment-checkpoint "
                    "contract change; reinitialize the renamed Skill"
                )
            },
            2,
        )
    configured_windows = value.get("max_fix_windows")
    if (
        type(configured_windows) is not int
        or not 1 <= configured_windows <= MAX_FIX_WINDOWS
    ):
        emit(
            {
                "error": (
                    "state max_fix_windows must be between "
                    f"1 and {MAX_FIX_WINDOWS}"
                )
            },
            2,
        )
    if value.get("outer_execution_policy") != OUTER_EXECUTION_POLICY:
        emit({"error": "state outer execution policy is missing or invalid"}, 2)
    requirements = validate_requirements(value.get("requirements"))
    if value.get("requirement_digest") != canonical_digest(requirements):
        emit({"error": "state requirements digest is missing or invalid"}, 2)
    if not isinstance(value.get("baseline_tree"), str) or not value["baseline_tree"].strip():
        emit({"error": "state baseline_tree is missing or invalid"}, 2)
    initial_review_count = value.get("initial_review_count")
    active_review_attempt = value.get("active_review_attempt")
    fix_window_count = value.get("fix_window_count")
    active_fix_window = value.get("active_fix_window")
    alignment_check_count = value.get("alignment_check_count")
    if (
        type(initial_review_count) is not int
        or initial_review_count not in {0, 1}
        or (
            active_review_attempt is not None
            and (
                type(active_review_attempt) is not int
                or active_review_attempt < 1
            )
        )
        or type(fix_window_count) is not int
        or not 0 <= fix_window_count <= configured_windows
        or type(alignment_check_count) is not int
        or not 0 <= alignment_check_count <= max(0, configured_windows - 1)
        or alignment_check_count > fix_window_count
        or alignment_check_count < max(0, fix_window_count - 1)
        or (
            active_fix_window is not None
            and (
                type(active_fix_window) is not int
                or active_fix_window != fix_window_count + 1
                or active_fix_window > configured_windows
            )
        )
    ):
        emit({"error": "state review, fix-window, or alignment counters are invalid"}, 2)
    if value.get("phase") not in {
        "ready_for_review",
        "reviewing",
        "ready_for_fix",
        "fixing",
        "alignment_required",
        "stopped",
    }:
        emit({"error": "state phase is invalid"}, 2)
    for field in ("active_fix_directive", "pending_alignment_directive"):
        directive = value.get(field)
        if field not in value or (
            directive is not None
            and (not isinstance(directive, str) or not directive.strip())
        ):
            emit({"error": f"state {field} is missing or invalid"}, 2)
    latest_alignment = value.get("latest_alignment_result")
    if "latest_alignment_result" not in value or (
        latest_alignment is not None and not isinstance(latest_alignment, dict)
    ):
        emit({"error": "state latest_alignment_result is missing or invalid"}, 2)
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


def git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["GIT_NO_LAZY_FETCH"] = "1"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment


def run_git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            env=git_environment(),
            stdin=subprocess.DEVNULL,
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


def current_tree(root: Path) -> str:
    try:
        return os.fsdecode(run_git(root, "rev-parse", "HEAD^{tree}")).strip()
    except ValueError as exc:
        raise ValueError("Git repository must have a valid HEAD tree") from exc


def resolve_tree(root: Path, revision: str) -> str:
    try:
        return os.fsdecode(
            run_git(root, "rev-parse", "--verify", f"{revision}^{{tree}}")
        ).strip()
    except ValueError as exc:
        raise ValueError(f"cannot resolve review baseline tree: {revision}") from exc


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


def collect_workspace_paths(
    root: Path,
    baseline_tree: str | None = None,
) -> dict[str, Any]:
    resolved_baseline = resolve_tree(root, baseline_tree or current_tree(root))
    committed_raw = run_git(
        root,
        "diff",
        "--ignore-submodules=none",
        "--no-renames",
        "--name-only",
        "-z",
        resolved_baseline,
        "HEAD",
        "--",
    )
    staged_raw = run_git(
        root,
        "diff",
        "--ignore-submodules=none",
        "--cached",
        "--no-renames",
        "--name-only",
        "-z",
        "HEAD",
        "--",
    )
    unstaged_raw = run_git(
        root,
        "diff",
        "--ignore-submodules=none",
        "--no-renames",
        "--name-only",
        "-z",
        "--",
    )
    untracked_raw = run_git(root, "ls-files", "--others", "--exclude-standard", "-z")

    def decode_paths(raw: bytes) -> list[str]:
        return sorted(
            {
                normalize_repo_path(os.fsdecode(item))
                for item in raw.split(b"\0")
                if item
            }
        )

    committed_paths = decode_paths(committed_raw)
    staged_paths = decode_paths(staged_raw)
    unstaged_paths = decode_paths(unstaged_raw)
    untracked_paths = decode_paths(untracked_raw)
    return {
        "baseline_tree": resolved_baseline,
        "head": current_head(root),
        "committed_paths": committed_paths,
        "staged_paths": staged_paths,
        "unstaged_paths": unstaged_paths,
        "untracked_paths": untracked_paths,
        "changed_paths": sorted(
            set(committed_paths + staged_paths + unstaged_paths + untracked_paths)
        ),
    }


def changed_paths(root: Path, baseline_tree: str | None = None) -> list[str]:
    return collect_workspace_paths(root, baseline_tree)["changed_paths"]


def parse_gitlink_paths(raw: bytes) -> list[str]:
    gitlinks: list[str] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        header, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise ValueError("cannot parse Git index entry while checking gitlinks")
        if header.split(maxsplit=1)[0] == b"160000":
            gitlinks.append(normalize_repo_path(os.fsdecode(raw_path)))
    return sorted(set(gitlinks))


def tracked_gitlink_paths(root: Path, baseline_tree: str) -> list[str]:
    sources = (
        run_git(root, "ls-files", "--stage", "-z"),
        run_git(root, "ls-tree", "-r", "-z", baseline_tree),
        run_git(root, "ls-tree", "-r", "-z", "HEAD"),
    )
    return sorted(
        {
            path
            for raw in sources
            for path in parse_gitlink_paths(raw)
        }
    )


def reject_changed_gitlinks(
    changed: list[str],
    gitlinks: list[str],
) -> None:
    changed_gitlinks = sorted(set(changed) & set(gitlinks))
    if changed_gitlinks:
        raise ValueError(
            "changed gitlinks are not supported in reusable snapshots: "
            + ", ".join(changed_gitlinks)
        )


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
    allowed_fields = set(REQUIREMENT_FIELDS) | {"test_mode", "test_mode_reason"}
    unknown_fields = sorted(set(value) - allowed_fields)
    if unknown_fields:
        emit(
            {"error": f"requirements contain unknown fields: {', '.join(unknown_fields)}"},
            2,
        )
    mode_present = "test_mode" in value
    reason_present = "test_mode_reason" in value
    if mode_present != reason_present:
        emit({"error": "requirements test_mode and test_mode_reason must be provided together"}, 2)
    if mode_present:
        mode = value["test_mode"]
        reason = value["test_mode_reason"]
        if not isinstance(mode, str) or mode not in TEST_MODES:
            emit({"error": "requirements test_mode must be none, light, or deep"}, 2)
        if not isinstance(reason, str) or reason != f"user:{mode}":
            emit({"error": "requirements test_mode_reason must equal user:<test_mode>"}, 2)
    for field, expected_type in REQUIREMENT_FIELDS.items():
        if not isinstance(value.get(field), expected_type):
            emit({"error": f"requirements field {field} must be {expected_type.__name__}"}, 2)
    if not value["original_request"].strip():
        emit({"error": "original_request must not be empty"}, 2)
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


def compute_workspace_digest(root: Path, paths: dict[str, Any]) -> str:
    reject_changed_gitlinks(
        paths["changed_paths"],
        tracked_gitlink_paths(root, paths["baseline_tree"]),
    )
    committed = read_screened_diff(
        root,
        (
            "diff",
            "--ignore-submodules=none",
            "--no-renames",
            "--binary",
            paths["baseline_tree"],
            "HEAD",
        ),
        paths["committed_paths"],
    )
    staged = read_screened_diff(
        root,
        (
            "diff",
            "--ignore-submodules=none",
            "--cached",
            "--no-renames",
            "--binary",
            "HEAD",
        ),
        paths["staged_paths"],
    )
    unstaged = read_screened_diff(
        root,
        ("diff", "--ignore-submodules=none", "--no-renames", "--binary"),
        paths["unstaged_paths"],
    )
    digest = hashlib.sha256()
    digest.update(b"baseline\0")
    digest.update(paths["baseline_tree"].encode("ascii"))
    digest.update(b"head\0")
    digest.update(paths["head"].encode("ascii"))
    digest.update(b"committed\0")
    digest.update(committed)
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
    return digest.hexdigest()


def compute_workspace_hash(
    root: Path,
    path_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    paths = path_snapshot or collect_workspace_paths(root)
    first_digest = compute_workspace_digest(root, paths)
    confirmed_paths = collect_workspace_paths(root, paths["baseline_tree"])
    if confirmed_paths != paths:
        raise ValueError("workspace paths changed while computing snapshot")
    second_digest = compute_workspace_digest(root, confirmed_paths)
    final_paths = collect_workspace_paths(root, paths["baseline_tree"])
    if final_paths != confirmed_paths or second_digest != first_digest:
        raise ValueError("workspace contents changed while computing snapshot")
    return {
        "snapshot_contract": SNAPSHOT_CONTRACT,
        "project_root": str(root),
        "baseline_tree": paths["baseline_tree"],
        "head": paths["head"],
        "diff_hash": second_digest,
        "untracked_hash_mode": "metadata-only",
        "committed_paths": paths["committed_paths"],
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
    if not 1 <= args.max_windows <= MAX_FIX_WINDOWS:
        emit(
            {
                "error": (
                    f"max fix windows must be between 1 and {MAX_FIX_WINDOWS}"
                )
            },
            2,
        )
    try:
        project_root = resolve_git_root(Path(args.project_root))
        head = current_head(project_root)
        baseline_tree = current_tree(project_root)
    except ValueError as exc:
        emit({"error": str(exc)}, 2)
    resolved_state_path = state_path.resolve()
    if resolved_state_path == project_root or project_root in resolved_state_path.parents:
        emit({"error": "state file must be outside project root"}, 2)
    if args.baseline_head != head:
        emit({"error": f"baseline HEAD must equal current HEAD {head}"}, 2)
    requirements = validate_requirements(read_json(Path(args.requirements_file), "requirements JSON"))
    scope = changed_paths(project_root, baseline_tree)
    state = {
        "version": STATE_VERSION,
        "project_root": str(project_root),
        "baseline_head": head,
        "baseline_tree": baseline_tree,
        "requirements": requirements,
        "requirement_digest": canonical_digest(requirements),
        "initial_scope_files": scope,
        "approved_scope_files": scope.copy(),
        "scope_extensions": [],
        "max_fix_windows": args.max_windows,
        "outer_execution_policy": OUTER_EXECUTION_POLICY.copy(),
        "initial_review_count": 0,
        "active_review_attempt": None,
        "review_attempt_count": 0,
        "review_checkpoint_count": 0,
        "fix_window_count": 0,
        "alignment_check_count": 0,
        "active_fix_window": None,
        "active_fix_input_diff_hash": None,
        "active_fix_directive": None,
        "total_fix_rounds": 0,
        "status": "active",
        "phase": "ready_for_review",
        "exit_reason": None,
        "review_failure_count": 0,
        "last_review_diff_hash": None,
        "last_post_fix_diff_hash": None,
        "pending_actionable_fingerprints": [],
        "latest_findings": [],
        "latest_fix_result": None,
        "latest_alignment_result": None,
        "pending_alignment_directive": None,
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
    paths = collect_workspace_paths(root, state["baseline_tree"])
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
    already_changed = set(changed_paths(root, state["baseline_tree"]))
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
    if any(
        suffix.lower() in SENSITIVE_FILE_EXTENSIONS for suffix in path.suffixes
    ):
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
    has_key_token = bool(semantic_set & {"key", "keys"})
    if any(
        name == private_name or name.startswith(f"{private_name}.")
        for private_name in PRIVATE_KEY_NAMES
    ):
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
        or bool(semantic_set & {"apikey", "apikeys"})
        or ("api" in semantic_set and has_key_token)
        or ("access" in semantic_set and has_key_token)
        or ("private" in semantic_set and has_key_token)
    ):
        return "credential-semantic filename"
    return None


def untracked_mode_reason(mode: int) -> str | None:
    if not stat.S_ISREG(mode):
        return "untracked path must be a regular file"
    return None


def untracked_file_reason(root: Path, relative: str) -> str | None:
    return untracked_mode_reason((root / relative).lstat().st_mode)


def tracked_worktree_file_reason(root: Path, relative: str) -> str | None:
    try:
        mode = (root / relative).lstat().st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(mode):
        return "changed tracked worktree path must be a regular file"
    return None


def reject_unsafe_review_surface(
    path: Path,
    state: dict[str, Any],
    path_snapshot: dict[str, Any],
    *,
    sensitive_reason: str = "sensitive_changed_path",
    unsafe_file_reason: str = "unsafe_untracked_file",
    unsafe_tracked_file_reason: str = "unsafe_tracked_worktree_file",
) -> None:
    root = Path(state["project_root"])
    untracked_paths = set(path_snapshot["untracked_paths"])
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
        if relative not in untracked_paths:
            try:
                reason = tracked_worktree_file_reason(root, relative)
            except OSError:
                block(path, state, "review_workspace_changed_during_screening")
            if reason is not None:
                block(
                    path,
                    state,
                    unsafe_tracked_file_reason,
                    sensitive_path=relative,
                    screening_reason=reason,
                )
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


def prepare_round(
    path: Path,
    state: dict[str, Any],
    drift_check_file: Path,
) -> tuple[Path, dict[str, Any], list[str], dict[str, Any]]:
    root = verify_repository(path, state)
    drift = validate_drift_check(read_json(drift_check_file, "drift check JSON"))
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
    paths = collect_workspace_paths(root, state["baseline_tree"])
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
    workspace = capture_workspace_hash(
        path,
        state,
        root,
        paths,
        "workspace_snapshot_failed",
    )
    return root, drift, actual_paths, workspace


def command_begin_review(args: argparse.Namespace) -> None:
    path = Path(args.state)
    state = load_state(path)
    ensure_active(state)
    require_phase(state, "ready_for_review")
    if state["initial_review_count"] != 0:
        block(path, state, "initial_review_already_completed")
    _, drift, actual_paths, workspace = prepare_round(
        path,
        state,
        Path(args.drift_check_file),
    )
    state["review_attempt_count"] += 1
    state["active_review_attempt"] = state["review_attempt_count"]
    state["phase"] = "reviewing"
    state["last_review_diff_hash"] = workspace["diff_hash"]
    append_event(
        state,
        "initial_review_started",
        attempt=state["active_review_attempt"],
        diff_hash=workspace["diff_hash"],
        requirement_check=drift["result"],
        requirement_digest=state["requirement_digest"],
        diff_files=actual_paths,
        planned_actions=drift["planned_actions"],
        original_request=drift["original_request"],
        accepted_amendments=drift["accepted_amendments"],
        alignment_summary=drift["alignment_summary"],
        untracked_review_screening=drift["untracked_review_screening"],
    )
    save_state(path, state)
    emit(state)


def command_begin_fix_window(args: argparse.Namespace) -> None:
    path = Path(args.state)
    state = load_state(path)
    ensure_active(state)
    require_phase(state, "ready_for_fix")
    if state["fix_window_count"] >= state["max_fix_windows"]:
        block(path, state, "max_fix_windows_reached")
    _, drift, actual_paths, workspace = prepare_round(
        path,
        state,
        Path(args.drift_check_file),
    )
    if workspace["diff_hash"] != state["last_review_diff_hash"]:
        block(
            path,
            state,
            "workspace_changed_since_last_review_checkpoint",
            expected_diff_hash=state["last_review_diff_hash"],
            actual_diff_hash=workspace["diff_hash"],
        )
    state["active_fix_window"] = state["fix_window_count"] + 1
    state["active_fix_input_diff_hash"] = workspace["diff_hash"]
    state["active_fix_directive"] = state["pending_alignment_directive"]
    state["pending_alignment_directive"] = None
    state["phase"] = "fixing"
    append_event(
        state,
        "fix_window_started",
        window=state["active_fix_window"],
        diff_hash=workspace["diff_hash"],
        requirement_check=drift["result"],
        requirement_digest=state["requirement_digest"],
        diff_files=actual_paths,
        planned_actions=drift["planned_actions"],
        pending_actionable_fingerprints=state["pending_actionable_fingerprints"],
        alignment_directive=state["active_fix_directive"],
    )
    save_state(path, state)
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
        "--ephemeral",
        "--json",
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(final_path),
        INITIAL_REVIEW_PROMPT,
    ]


def verify_frozen_review_workspace(path: Path, state: dict[str, Any], root: Path) -> None:
    paths = collect_workspace_paths(root, state["baseline_tree"])
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
                    env=git_environment(),
                    stdin=subprocess.DEVNULL,
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
    paths = collect_workspace_paths(root, state["baseline_tree"])
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
            "initial_review_incomplete",
            attempt=state["active_review_attempt"],
            count=state["review_failure_count"],
            reviewer=result["reviewer"],
            reviewer_exit_code=result["reviewer_exit_code"],
            coverage_status=result["coverage_status"],
            raw_output_sha256=result["raw_output_sha256"],
            raw_output_bytes=result["raw_output_bytes"],
        )
        if state["review_failure_count"] >= 2:
            block(path, state, "reviewer_failed_twice")
        state["active_review_attempt"] = None
        state["phase"] = "ready_for_review"
        save_state(path, state)
        emit(state, RETRY_EXIT)

    state["review_failure_count"] = 0
    state["initial_review_count"] = 1
    state["active_review_attempt"] = None
    state["review_checkpoint_count"] = 1
    findings = result["findings"]
    state["latest_findings"] = findings
    blocking = [finding for finding in findings if finding["status"] in BLOCKING_FINDING_STATUSES]
    actionable = sorted(finding["fingerprint"] for finding in findings if finding["status"] == "actionable")
    append_event(
        state,
        "initial_review_recorded",
        checkpoint=state["review_checkpoint_count"],
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
        state["exit_reason"] = "initial_review_converged"
        save_state(path, state)
        emit(state)
    state["pending_actionable_fingerprints"] = actionable
    state["phase"] = "ready_for_fix"
    save_state(path, state)
    emit(state)


def validate_fix_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        emit({"error": "fix result file must contain a JSON object"}, 2)
    required = {
        "status": str,
        "exit_reason": str,
        "requirements_snapshot": dict,
        "requirement_digest": str,
        "fix_rounds": int,
        "unresolved_count": int,
        "coverage_status": str,
        "validation_status": str,
        "test_mode": str,
        "test_mode_reason": str,
        "exit_test_count": int,
        "exit_test_status": str,
        "alignment_directive_status": str,
    }
    for field, expected in required.items():
        if type(value.get(field)) is not expected:
            emit({"error": f"fix result field {field} must be {expected.__name__}"}, 2)
    if "exit_test_skip_reason" not in value:
        emit({"error": "fix result field exit_test_skip_reason is required"}, 2)
    skip_reason = value["exit_test_skip_reason"]
    if skip_reason is not None and (
        not isinstance(skip_reason, str) or not skip_reason.strip()
    ):
        emit(
            {
                "error": (
                    "exit_test_skip_reason must be null or a non-empty string"
                )
            },
            2,
        )
    if value["status"] not in {"complete", "incomplete", "blocked"}:
        emit({"error": "invalid fix result status"}, 2)
    for field in ("alignment_directive", "alignment_resolution_evidence"):
        if field not in value:
            emit({"error": f"fix result field {field} is required"}, 2)
        field_value = value[field]
        if field_value is not None and (
            not isinstance(field_value, str) or not field_value.strip()
        ):
            emit(
                {"error": f"fix result field {field} must be null or a non-empty string"},
                2,
            )
    if value["alignment_directive_status"] not in ALIGNMENT_DIRECTIVE_STATUSES:
        emit({"error": "invalid alignment_directive_status in fix result"}, 2)
    if value["alignment_directive_status"] == "not_applicable":
        if (
            value["alignment_directive"] is not None
            or value["alignment_resolution_evidence"] is not None
        ):
            emit(
                {
                    "error": (
                        "not_applicable alignment directive status requires null "
                        "directive and resolution evidence"
                    )
                },
                2,
            )
    elif (
        value["alignment_directive"] is None
        or value["alignment_resolution_evidence"] is None
    ):
        emit(
            {
                "error": (
                    "resolved or unresolved alignment directive status requires "
                    "a directive and resolution evidence"
                )
            },
            2,
        )
    if not value["exit_reason"].strip():
        emit({"error": "fix result exit_reason must not be empty"}, 2)
    if re.fullmatch(r"[0-9a-f]{64}", value["requirement_digest"]) is None:
        emit({"error": "fix result requirement_digest must be lowercase sha256"}, 2)
    value["requirements_snapshot"] = validate_requirements(
        value["requirements_snapshot"]
    )
    if canonical_digest(value["requirements_snapshot"]) != value["requirement_digest"]:
        emit({"error": "fix result requirement snapshot digest mismatch"}, 2)
    if not 0 <= value["fix_rounds"] <= CHILD_FIX_ROUND_LIMIT:
        emit(
            {
                "error": (
                    "fix_rounds must be between zero and "
                    f"{CHILD_FIX_ROUND_LIMIT}"
                )
            },
            2,
        )
    if value["unresolved_count"] < 0:
        emit({"error": "unresolved_count cannot be negative"}, 2)
    if value["coverage_status"] not in {"covered", "partial", "uncovered"}:
        emit({"error": "invalid coverage_status"}, 2)
    if value["validation_status"] not in {
        "unverified",
        "focused",
        "complete",
        "failed",
        "blocked",
    }:
        emit({"error": "invalid validation_status"}, 2)
    if value["test_mode"] not in TEST_MODES:
        emit({"error": "invalid test mode in fix result"}, 2)
    if value["test_mode_reason"] not in {
        "default",
        "user:none",
        "user:light",
        "user:deep",
    }:
        emit({"error": "invalid test_mode_reason in fix result"}, 2)
    if value["test_mode_reason"] == "default" and value["test_mode"] != "light":
        emit({"error": "default test_mode_reason requires light mode"}, 2)
    if value["test_mode_reason"].startswith("user:") and value["test_mode_reason"] != f"user:{value['test_mode']}":
        emit({"error": "user test_mode_reason must match test_mode"}, 2)
    if value["exit_test_count"] not in {0, 1}:
        emit({"error": "exit_test_count must be zero or one"}, 2)
    if value["exit_test_status"] not in EXIT_TEST_STATUSES:
        emit({"error": "invalid exit_test_status"}, 2)
    if value["exit_test_count"] == 0 and value["exit_test_status"] != "skipped":
        emit({"error": "zero exit_test_count requires skipped"}, 2)
    if value["exit_test_count"] == 1 and value["exit_test_status"] not in {"passed", "failed", "blocked"}:
        emit({"error": "one exit_test_count requires passed, failed, or blocked"}, 2)
    if value["fix_rounds"] == 0 and value["exit_test_count"] != 0:
        emit({"error": "zero fix_rounds cannot execute exit validation"}, 2)
    if value["exit_test_count"] == 1 and skip_reason is not None:
        emit({"error": "executed exit validation requires a null skip reason"}, 2)
    if value["exit_test_count"] == 0 and skip_reason not in EXIT_TEST_SKIP_REASONS:
        emit({"error": "skipped exit validation requires a recognized skip reason"}, 2)
    if value["test_mode"] == "none" and (
        value["exit_test_count"] != 0
        or value["exit_test_status"] != "skipped"
        or value["validation_status"] != "unverified"
        or skip_reason != "user_disabled"
    ):
        emit(
            {
                "error": (
                    "none mode requires skipped, unverified exit evidence with "
                    "user_disabled reason"
                )
            },
            2,
        )
    if value["exit_test_count"] == 0 and value["test_mode"] != "none":
        if value["status"] == "blocked":
            expected_skip_reason = "blocked_before_exit_validation"
        elif value["fix_rounds"] == 0:
            expected_skip_reason = "no_persistent_change"
        else:
            expected_skip_reason = "no_applicable_automated_test"
        if skip_reason != expected_skip_reason:
            emit(
                {
                    "error": (
                        "exit_test_skip_reason does not match the final result and "
                        "persistent-change state"
                    )
                },
                2,
            )
    expected_validation = {
        "passed": "focused" if value["test_mode"] == "light" else "complete",
        "failed": "failed",
        "blocked": "blocked",
        "not_run": "unverified",
        "skipped": "unverified",
    }[value["exit_test_status"]]
    if value["validation_status"] != expected_validation:
        emit({"error": "validation_status does not match exit_test_status and test_mode"}, 2)
    return value


def validate_review_checkpoint(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("continuable fix result requires review_checkpoint")
    required = {
        "source": str,
        "snapshot_contract": str,
        "review_complete": bool,
        "coverage_status": str,
        "review_baseline": str,
        "reviewed_diff_hash": str,
        "changed_paths": list,
        "findings": list,
    }
    for field, expected in required.items():
        if type(value.get(field)) is not expected:
            raise ValueError(
                f"review checkpoint field {field} must be {expected.__name__}"
            )
    if value["source"] != "code-review-fix-loop":
        raise ValueError("review checkpoint source must be code-review-fix-loop")
    if value["snapshot_contract"] != SNAPSHOT_CONTRACT:
        raise ValueError(
            f"review checkpoint snapshot_contract must be {SNAPSHOT_CONTRACT}"
        )
    if value["review_complete"] is not True or value["coverage_status"] != "covered":
        raise ValueError("review checkpoint must prove complete covered review")
    if not value["review_baseline"].strip():
        raise ValueError("review checkpoint baseline must not be empty")
    if re.fullmatch(r"[0-9a-f]{64}", value["reviewed_diff_hash"]) is None:
        raise ValueError("review checkpoint diff hash must be lowercase sha256")
    try:
        value["changed_paths"] = sorted(
            {normalize_repo_path(item) for item in value["changed_paths"]}
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid review checkpoint changed path: {exc}") from exc
    try:
        value["findings"] = validate_findings(value["findings"])
    except ValueError as exc:
        raise ValueError(f"invalid review checkpoint findings: {exc}") from exc
    return value


def validate_bound_child_result(
    path: Path,
    state: dict[str, Any],
    result: dict[str, Any],
    workspace: dict[str, Any],
    actual_paths: list[str],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    if result["requirement_digest"] != state["requirement_digest"]:
        block(
            path,
            state,
            "child_requirement_digest_mismatch",
            expected_requirement_digest=state["requirement_digest"],
            reported_requirement_digest=result["requirement_digest"],
        )
    if result["requirements_snapshot"] != state["requirements"]:
        block(path, state, "child_requirement_snapshot_mismatch")
    active_directive = state["active_fix_directive"]
    if active_directive is None:
        if (
            result["alignment_directive"] is not None
            or result["alignment_directive_status"] != "not_applicable"
            or result["alignment_resolution_evidence"] is not None
        ):
            block(path, state, "alignment_directive_contract_mismatch")
    else:
        if (
            result["alignment_directive"] != active_directive
            or result["alignment_directive_status"] == "not_applicable"
            or result["alignment_resolution_evidence"] is None
        ):
            block(
                path,
                state,
                "alignment_directive_contract_mismatch",
                expected_alignment_directive=active_directive,
                reported_alignment_directive=result["alignment_directive"],
                reported_alignment_directive_status=result[
                    "alignment_directive_status"
                ],
            )
        if (
            result["status"] == "complete"
            and result["alignment_directive_status"] != "resolved"
        ):
            block(
                path,
                state,
                "alignment_directive_unresolved",
                alignment_directive=active_directive,
                alignment_resolution_evidence=result[
                    "alignment_resolution_evidence"
                ],
            )
    expected_mode = state["requirements"].get("test_mode", "light")
    expected_mode_reason = state["requirements"].get("test_mode_reason", "default")
    if (
        result["test_mode"] != expected_mode
        or result["test_mode_reason"] != expected_mode_reason
    ):
        block(
            path,
            state,
            "code_review_fix_loop_test_mode_mismatch",
            expected_test_mode=expected_mode,
            expected_test_mode_reason=expected_mode_reason,
            reported_test_mode=result["test_mode"],
            reported_test_mode_reason=result["test_mode_reason"],
        )
    if result["status"] == "blocked":
        block(path, state, "code_review_fix_loop_blocked")
    if result["validation_status"] in {"failed", "blocked"}:
        block(
            path,
            state,
            f"code_review_fix_loop_validation_{result['validation_status']}",
        )

    try:
        checkpoint = validate_review_checkpoint(result.get("review_checkpoint"))
    except ValueError as exc:
        block(
            path,
            state,
            "invalid_child_review_checkpoint",
            checkpoint_error=str(exc),
        )
    if checkpoint["review_baseline"] != state["baseline_tree"]:
        block(
            path,
            state,
            "child_review_baseline_mismatch",
            expected_review_baseline=state["baseline_tree"],
            reported_review_baseline=checkpoint["review_baseline"],
        )
    if checkpoint["reviewed_diff_hash"] != workspace["diff_hash"]:
        block(
            path,
            state,
            "child_review_checkpoint_stale",
            expected_diff_hash=workspace["diff_hash"],
            reported_diff_hash=checkpoint["reviewed_diff_hash"],
        )
    if checkpoint["changed_paths"] != actual_paths:
        block(
            path,
            state,
            "child_review_scope_mismatch",
            expected_changed_paths=actual_paths,
            reported_changed_paths=checkpoint["changed_paths"],
        )
    if result["coverage_status"] != checkpoint["coverage_status"]:
        block(path, state, "child_review_coverage_mismatch")

    findings = checkpoint["findings"]
    unresolved = [
        finding
        for finding in findings
        if finding["status"] == "actionable"
        or finding["status"] in BLOCKING_FINDING_STATUSES
    ]
    if result["unresolved_count"] != len(unresolved):
        block(
            path,
            state,
            "child_unresolved_count_mismatch",
            reported_unresolved_count=result["unresolved_count"],
            checkpoint_unresolved_count=len(unresolved),
        )
    actionable = sorted(
        finding["fingerprint"]
        for finding in findings
        if finding["status"] == "actionable"
    )
    return checkpoint, findings, actionable


def validate_zero_change_reclassification(
    path: Path,
    state: dict[str, Any],
    findings: list[dict[str, Any]],
) -> None:
    previous_by_fingerprint = {
        finding["fingerprint"]: finding for finding in state["latest_findings"]
    }
    current_by_fingerprint = {
        finding["fingerprint"]: finding for finding in findings
    }
    if set(previous_by_fingerprint) != set(current_by_fingerprint):
        block(
            path,
            state,
            "zero_change_checkpoint_finding_set_mismatch",
            expected_fingerprints=sorted(previous_by_fingerprint),
            reported_fingerprints=sorted(current_by_fingerprint),
        )
    for fingerprint, previous in previous_by_fingerprint.items():
        current = current_by_fingerprint[fingerprint]
        if (
            current["severity"] != previous["severity"]
            or current["location"] != previous["location"]
        ):
            block(
                path,
                state,
                "zero_change_checkpoint_finding_identity_mismatch",
                finding_fingerprint=fingerprint,
            )


def finish_or_continue_child_result(
    path: Path,
    state: dict[str, Any],
    result: dict[str, Any],
    actionable: list[str],
) -> NoReturn:
    blocking = [
        finding
        for finding in state["latest_findings"]
        if finding["status"] in BLOCKING_FINDING_STATUSES
    ]
    if blocking:
        block(path, state, "unresolved_review_items", unresolved_review_items=blocking)
    if not actionable:
        if (
            result["status"] != "complete"
            or result["exit_reason"] != "converged"
            or result["unresolved_count"] != 0
        ):
            block(path, state, "child_completion_contract_mismatch")
        state["status"] = "complete"
        state["phase"] = "stopped"
        state["exit_reason"] = "review_fix_converged"
        save_state(path, state)
        emit(state)

    if (
        result["status"] != "incomplete"
        or result["exit_reason"] != "max_fix_rounds_reached"
        or result["fix_rounds"] != CHILD_FIX_ROUND_LIMIT
    ):
        block(path, state, "child_continuation_contract_mismatch")
    if state["fix_window_count"] >= state["max_fix_windows"]:
        block(path, state, "max_fix_windows_reached")
    state["phase"] = "alignment_required"
    save_state(path, state)
    emit(state)


def validate_alignment_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        emit({"error": "alignment result file must contain a JSON object"}, 2)
    required = {
        "source": str,
        "outcome": str,
        "snapshot_contract": str,
        "review_baseline": str,
        "assessed_head": str,
        "assessed_diff_hash": str,
        "changed_paths": list,
        "requirement_digest": str,
        "review_checkpoint_digest": str,
        "requirement_alignment": str,
        "implementation_completeness": str,
        "design_convergence": str,
        "minimality": str,
        "alignment_findings": list,
    }
    for field, expected in required.items():
        if type(value.get(field)) is not expected:
            emit({"error": f"alignment result field {field} must be {expected.__name__}"}, 2)
    if value["source"] != "review-fix-alignment-supervisor":
        emit({"error": "alignment result source must be review-fix-alignment-supervisor"}, 2)
    if value["outcome"] not in ALIGNMENT_OUTCOMES:
        emit({"error": "invalid alignment outcome"}, 2)
    if value["snapshot_contract"] != SNAPSHOT_CONTRACT:
        emit({"error": "alignment snapshot_contract is unsupported"}, 2)
    for field in ("review_baseline", "assessed_head"):
        if re.fullmatch(r"[0-9a-f]{40,64}", value[field]) is None:
            emit({"error": f"alignment {field} must be a lowercase Git object id"}, 2)
    if re.fullmatch(r"[0-9a-f]{64}", value["assessed_diff_hash"]) is None:
        emit({"error": "alignment assessed_diff_hash must be lowercase sha256"}, 2)
    if re.fullmatch(r"[0-9a-f]{64}", value["requirement_digest"]) is None:
        emit({"error": "alignment requirement_digest must be lowercase sha256"}, 2)
    if re.fullmatch(r"[0-9a-f]{64}", value["review_checkpoint_digest"]) is None:
        emit({"error": "alignment review_checkpoint_digest must be lowercase sha256"}, 2)
    if any(not isinstance(item, str) for item in value["changed_paths"]):
        emit({"error": "alignment changed_paths must contain strings"}, 2)
    try:
        normalized_changed_paths = sorted(
            {normalize_repo_path(item) for item in value["changed_paths"]}
        )
    except ValueError as exc:
        emit({"error": f"invalid alignment changed path: {exc}"}, 2)
    if normalized_changed_paths != value["changed_paths"]:
        emit({"error": "alignment changed_paths must be sorted and unique"}, 2)
    enum_fields = {
        "requirement_alignment": REQUIREMENT_ALIGNMENT_VALUES,
        "implementation_completeness": IMPLEMENTATION_COMPLETENESS_VALUES,
        "design_convergence": DESIGN_CONVERGENCE_VALUES,
        "minimality": MINIMALITY_VALUES,
    }
    for field, allowed in enum_fields.items():
        if value[field] not in allowed:
            emit({"error": f"invalid alignment result {field}"}, 2)

    findings: list[dict[str, Any]] = []
    for finding in value["alignment_findings"]:
        if not isinstance(finding, dict):
            emit({"error": "each alignment finding must be a JSON object"}, 2)
        if finding.get("kind") not in ALIGNMENT_FINDING_KINDS:
            emit({"error": "invalid alignment finding kind"}, 2)
        for field in ("summary", "evidence"):
            if not isinstance(finding.get(field), str) or not finding[field].strip():
                emit({"error": f"alignment finding field {field} must be a non-empty string"}, 2)
        related_paths = finding.get("related_paths")
        if not isinstance(related_paths, list) or any(
            not isinstance(item, str) for item in related_paths
        ):
            emit({"error": "alignment finding related_paths must be a string array"}, 2)
        try:
            finding["related_paths"] = sorted(
                {normalize_repo_path(item) for item in related_paths}
            )
        except ValueError as exc:
            emit({"error": f"invalid alignment finding path: {exc}"}, 2)
        findings.append(finding)
    value["alignment_findings"] = findings

    for field in ("correction_directive", "developer_question", "blocker"):
        if field not in value:
            emit({"error": f"alignment result field {field} is required"}, 2)
        control = value.get(field)
        if control is not None and (not isinstance(control, str) or not control.strip()):
            emit({"error": f"alignment result {field} must be null or a non-empty string"}, 2)

    outcome = value["outcome"]
    if outcome == "continue":
        if (
            value["requirement_alignment"] != "aligned"
            or value["implementation_completeness"] not in {"complete", "fixable_gap"}
            or value["design_convergence"] != "convergent"
            or value["minimality"] != "minimum_sufficient"
            or findings
            or any(value.get(field) is not None for field in (
                "correction_directive",
                "developer_question",
                "blocker",
            ))
        ):
            emit({"error": "continue alignment result has an invalid field combination"}, 2)
    elif outcome == "redirect":
        if (
            value["implementation_completeness"] != "fixable_gap"
            or value["requirement_alignment"] == "uncertain"
            or value["design_convergence"] in {"decision_required", "blocked"}
            or value["minimality"] == "uncertain"
            or not findings
            or not isinstance(value.get("correction_directive"), str)
            or value.get("developer_question") is not None
            or value.get("blocker") is not None
            or (
                value["requirement_alignment"] == "aligned"
                and value["design_convergence"] == "convergent"
                and value["minimality"] == "minimum_sufficient"
            )
        ):
            emit({"error": "redirect alignment result has an invalid field combination"}, 2)
    elif outcome == "ask_developer":
        decision_signal = (
            value["requirement_alignment"] == "uncertain"
            or value["implementation_completeness"] == "decision_gap"
            or value["design_convergence"] == "decision_required"
            or value["minimality"] == "uncertain"
        )
        if (
            not decision_signal
            or value["implementation_completeness"] == "blocked"
            or value["design_convergence"] == "blocked"
            or not findings
            or value.get("correction_directive") is not None
            or not isinstance(value.get("developer_question"), str)
            or value.get("blocker") is not None
        ):
            emit({"error": "ask_developer alignment result has an invalid field combination"}, 2)
    else:
        blocked_signal = (
            value["implementation_completeness"] == "blocked"
            or value["design_convergence"] == "blocked"
        )
        if (
            not blocked_signal
            or not isinstance(value.get("blocker"), str)
            or value.get("correction_directive") is not None
            or value.get("developer_question") is not None
        ):
            emit({"error": "blocked alignment result has an invalid field combination"}, 2)
    return value


def command_record_fix(args: argparse.Namespace) -> None:
    path = Path(args.state)
    state = load_state(path)
    ensure_active(state)
    require_phase(state, "fixing")
    root = verify_repository(path, state)
    result = validate_fix_result(read_json(Path(args.skill_result_file), "fix result JSON"))
    state["latest_fix_result"] = result
    paths = collect_workspace_paths(root, state["baseline_tree"])
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
    checkpoint, findings, actionable = validate_bound_child_result(
        path,
        state,
        result,
        workspace,
        actual_paths,
    )

    input_diff_hash = state["active_fix_input_diff_hash"]
    if result["fix_rounds"] > 0 and workspace["diff_hash"] == input_diff_hash:
        block(path, state, "diff_unchanged_after_fix_window")
    if result["fix_rounds"] == 0 and workspace["diff_hash"] != input_diff_hash:
        block(path, state, "diff_changed_without_reported_fix_round")
    if result["fix_rounds"] == 0:
        validate_zero_change_reclassification(path, state, findings)

    active_fix_directive = state["active_fix_directive"]
    state["fix_window_count"] = state["active_fix_window"]
    state["active_fix_window"] = None
    state["active_fix_input_diff_hash"] = None
    state["active_fix_directive"] = None
    state["total_fix_rounds"] += result["fix_rounds"]
    state["review_checkpoint_count"] += 1
    state["latest_review_result"] = checkpoint
    state["latest_findings"] = findings
    state["pending_actionable_fingerprints"] = actionable
    state["last_post_fix_diff_hash"] = workspace["diff_hash"]
    state["last_review_diff_hash"] = workspace["diff_hash"]
    append_event(
        state,
        "fix_window_checkpoint_accepted",
        window=state["fix_window_count"],
        diff_hash=workspace["diff_hash"],
        fix_result=result,
        alignment_directive=active_fix_directive,
        total_fix_rounds=state["total_fix_rounds"],
        review_checkpoint_count=state["review_checkpoint_count"],
    )

    finish_or_continue_child_result(path, state, result, actionable)


def command_adopt_fix_result(args: argparse.Namespace) -> None:
    path = Path(args.state)
    state = load_state(path)
    ensure_active(state)
    require_phase(state, "ready_for_review")
    if state["initial_review_count"] != 0 or state["fix_window_count"] != 0:
        block(path, state, "fix_result_adoption_no_longer_available")

    root = verify_repository(path, state)
    result = validate_fix_result(
        read_json(Path(args.skill_result_file), "fix result JSON")
    )
    state["latest_fix_result"] = result
    try:
        candidate_checkpoint = validate_review_checkpoint(
            result.get("review_checkpoint")
        )
    except ValueError as exc:
        block(
            path,
            state,
            "invalid_child_review_checkpoint",
            checkpoint_error=str(exc),
        )
    try:
        adopted_baseline = resolve_tree(
            root,
            candidate_checkpoint["review_baseline"],
        )
    except ValueError as exc:
        block(
            path,
            state,
            "child_review_baseline_invalid",
            checkpoint_error=str(exc),
        )
    if adopted_baseline != candidate_checkpoint["review_baseline"]:
        block(
            path,
            state,
            "child_review_baseline_must_be_tree",
            reported_review_baseline=candidate_checkpoint["review_baseline"],
            resolved_review_baseline=adopted_baseline,
        )
    state["baseline_tree"] = adopted_baseline
    paths = collect_workspace_paths(root, adopted_baseline)
    actual_paths = paths["changed_paths"]
    reject_unsafe_review_surface(path, state, paths)
    workspace = capture_workspace_hash(
        path,
        state,
        root,
        paths,
        "workspace_snapshot_failed_during_fix_result_adoption",
    )
    checkpoint, findings, actionable = validate_bound_child_result(
        path,
        state,
        result,
        workspace,
        actual_paths,
    )

    state["initial_scope_files"] = actual_paths.copy()
    state["approved_scope_files"] = actual_paths.copy()
    state["fix_window_count"] = 1
    state["total_fix_rounds"] = result["fix_rounds"]
    state["review_checkpoint_count"] = 1
    state["latest_review_result"] = checkpoint
    state["latest_findings"] = findings
    state["pending_actionable_fingerprints"] = actionable
    state["last_post_fix_diff_hash"] = workspace["diff_hash"]
    state["last_review_diff_hash"] = workspace["diff_hash"]
    append_event(
        state,
        "fix_result_adopted",
        window=1,
        diff_hash=workspace["diff_hash"],
        fix_result=result,
        total_fix_rounds=state["total_fix_rounds"],
        review_checkpoint_count=state["review_checkpoint_count"],
        adopted_review_baseline=adopted_baseline,
    )
    finish_or_continue_child_result(path, state, result, actionable)


def command_record_alignment(args: argparse.Namespace) -> None:
    path = Path(args.state)
    state = load_state(path)
    ensure_active(state)
    require_phase(state, "alignment_required")
    root = verify_repository(path, state)
    paths = collect_workspace_paths(root, state["baseline_tree"])
    actual_paths = paths["changed_paths"]
    unexpected = sorted(set(actual_paths) - set(state["approved_scope_files"]))
    if unexpected:
        block(path, state, "scope_expanded_before_alignment", unexpected_scope_files=unexpected)
    reject_unsafe_review_surface(path, state, paths)
    workspace = capture_workspace_hash(
        path,
        state,
        root,
        paths,
        "workspace_snapshot_failed_before_alignment",
    )
    if workspace["diff_hash"] != state["last_review_diff_hash"]:
        block(
            path,
            state,
            "workspace_changed_before_alignment_result",
            expected_diff_hash=state["last_review_diff_hash"],
            actual_diff_hash=workspace["diff_hash"],
        )
    result = validate_alignment_result(
        read_json(Path(args.alignment_result_file), "alignment result JSON")
    )
    expected_checkpoint = {
        "snapshot_contract": workspace["snapshot_contract"],
        "review_baseline": workspace["baseline_tree"],
        "assessed_head": workspace["head"],
        "assessed_diff_hash": workspace["diff_hash"],
        "changed_paths": workspace["changed_paths"],
        "requirement_digest": state["requirement_digest"],
        "review_checkpoint_digest": canonical_digest(state["latest_review_result"]),
    }
    mismatched_fields = [
        field
        for field, expected in expected_checkpoint.items()
        if result[field] != expected
    ]
    if mismatched_fields:
        block(
            path,
            state,
            "alignment_checkpoint_stale",
            expected_diff_hash=workspace["diff_hash"],
            reported_diff_hash=result["assessed_diff_hash"],
            mismatched_fields=mismatched_fields,
        )

    state["alignment_check_count"] += 1
    state["latest_alignment_result"] = result
    append_event(
        state,
        "alignment_checkpoint_accepted",
        after_fix_window=state["fix_window_count"],
        diff_hash=workspace["diff_hash"],
        review_checkpoint_digest=result["review_checkpoint_digest"],
        outcome=result["outcome"],
        alignment_findings=result["alignment_findings"],
    )
    if result["outcome"] == "ask_developer":
        block(
            path,
            state,
            "alignment_requires_developer_decision",
            developer_question=result["developer_question"],
        )
    if result["outcome"] == "blocked":
        block(
            path,
            state,
            "alignment_supervisor_blocked",
            alignment_blocker=result["blocker"],
        )

    state["pending_alignment_directive"] = (
        result["correction_directive"]
        if result["outcome"] == "redirect"
        else None
    )
    state["phase"] = "ready_for_fix"
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
    init.add_argument("--max-windows", type=int, default=MAX_FIX_WINDOWS)
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

    begin_fix = commands.add_parser("begin-fix-window")
    begin_fix.add_argument("--state", required=True)
    begin_fix.add_argument("--drift-check-file", required=True)
    begin_fix.set_defaults(handler=command_begin_fix_window)

    fix = commands.add_parser("record-fix")
    fix.add_argument("--state", required=True)
    fix.add_argument("--skill-result-file", required=True)
    fix.set_defaults(handler=command_record_fix)

    adopt = commands.add_parser("adopt-fix-result")
    adopt.add_argument("--state", required=True)
    adopt.add_argument("--skill-result-file", required=True)
    adopt.set_defaults(handler=command_adopt_fix_result)

    alignment = commands.add_parser("record-alignment")
    alignment.add_argument("--state", required=True)
    alignment.add_argument("--alignment-result-file", required=True)
    alignment.set_defaults(handler=command_record_alignment)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
