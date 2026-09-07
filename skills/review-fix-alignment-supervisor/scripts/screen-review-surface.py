#!/usr/bin/env python3
"""Fail closed before an alignment review reads changed-file contents."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any


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
CREDENTIAL_DOMAIN_SUFFIXES = {"bucket", "parser", "policy"}
PRIVATE_KEY_NAMES = {"id_dsa", "id_ecdsa", "id_ed25519", "id_rsa"}
SNAPSHOT_CONTRACT = "git-cumulative-diff-sha256-v1"


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


def resolve_git_root(candidate: Path) -> Path:
    root = candidate.resolve()
    if not root.is_dir():
        raise ValueError(f"project root is not a directory: {root}")
    discovered = Path(
        os.fsdecode(run_git(root, "rev-parse", "--show-toplevel")).strip()
    ).resolve()
    if discovered != root:
        raise ValueError(f"project root must be the Git repository root: {discovered}")
    return root


def resolve_tree(root: Path, revision: str) -> str:
    try:
        return os.fsdecode(
            run_git(root, "rev-parse", "--verify", f"{revision}^{{tree}}")
        ).strip()
    except ValueError as exc:
        raise ValueError(f"cannot resolve review baseline tree: {revision}") from exc


def current_head(root: Path) -> str:
    return os.fsdecode(run_git(root, "rev-parse", "HEAD")).strip()


def collect_workspace_paths(root: Path, baseline_tree: str) -> dict[str, Any]:
    resolved_baseline = resolve_tree(root, baseline_tree)
    committed = run_git(
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
    staged = run_git(
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
    unstaged = run_git(
        root,
        "diff",
        "--ignore-submodules=none",
        "--no-renames",
        "--name-only",
        "-z",
        "--",
    )
    untracked = run_git(root, "ls-files", "--others", "--exclude-standard", "-z")

    def decode(raw: bytes) -> list[str]:
        return sorted(
            {
                normalize_repo_path(os.fsdecode(item))
                for item in raw.split(b"\0")
                if item
            }
        )

    committed_paths = decode(committed)
    staged_paths = decode(staged)
    unstaged_paths = decode(unstaged)
    untracked_paths = decode(untracked)
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


def parse_gitlink_paths(raw: bytes) -> list[str]:
    gitlinks: list[str] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        header, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise ValueError("cannot parse Git entry while checking gitlinks")
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


def reject_changed_gitlinks(changed: list[str], gitlinks: list[str]) -> None:
    changed_gitlinks = sorted(set(changed) & set(gitlinks))
    if changed_gitlinks:
        raise ValueError(
            "changed gitlinks are not supported in alignment snapshots: "
            + ", ".join(changed_gitlinks)
        )


def read_screened_diff(
    root: Path,
    diff_args: tuple[str, ...],
    screened_paths: list[str],
) -> bytes:
    if not screened_paths:
        return b""
    literal_pathspecs = [f":(literal){item}" for item in screened_paths]
    return run_git(root, *diff_args, "--", *literal_pathspecs)


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


def tracked_worktree_file_reason(root: Path, relative: str) -> str | None:
    try:
        mode = (root / relative).lstat().st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(mode):
        return "changed tracked worktree path must be a regular file"
    return None


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
            str(paths["baseline_tree"]),
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
    digest.update(str(paths["baseline_tree"]).encode("ascii"))
    digest.update(b"head\0")
    digest.update(str(paths["head"]).encode("ascii"))
    digest.update(b"committed\0")
    digest.update(committed)
    digest.update(b"staged\0")
    digest.update(staged)
    digest.update(b"unstaged\0")
    digest.update(unstaged)
    for relative in paths["untracked_paths"]:
        metadata = (root / relative).lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"unsafe untracked file type: {relative}")
        digest.update(b"untracked-metadata\0")
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
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


def screen_review_surface(root: Path, baseline_tree: str) -> dict[str, Any]:
    paths = collect_workspace_paths(root, baseline_tree)
    changed_paths = paths["changed_paths"]
    untracked_paths = paths["untracked_paths"]
    assert isinstance(changed_paths, list)
    assert isinstance(untracked_paths, list)
    untracked_set = set(untracked_paths)
    for relative in changed_paths:
        reason = sensitive_path_reason(relative)
        if reason is not None:
            raise ValueError(f"sensitive changed path {relative}: {reason}")
        if relative not in untracked_set:
            reason = tracked_worktree_file_reason(root, relative)
            if reason is not None:
                raise ValueError(
                    f"unsafe tracked worktree file type: {relative}: {reason}"
                )
    for relative in untracked_paths:
        if not stat.S_ISREG((root / relative).lstat().st_mode):
            raise ValueError(f"unsafe untracked file type: {relative}")

    first_digest = compute_workspace_digest(root, paths)
    confirmed_paths = collect_workspace_paths(root, str(paths["baseline_tree"]))
    if confirmed_paths != paths:
        raise ValueError("workspace paths changed while screening review surface")
    second_digest = compute_workspace_digest(root, confirmed_paths)
    final_paths = collect_workspace_paths(root, str(paths["baseline_tree"]))
    if final_paths != confirmed_paths or second_digest != first_digest:
        raise ValueError("workspace contents changed while screening review surface")
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
        "screening_status": "safe",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--baseline", required=True)
    args = parser.parse_args()
    try:
        result = screen_review_surface(
            resolve_git_root(args.project_root),
            args.baseline,
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"screening_status": "blocked", "error": str(exc)}))
        return 3
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
