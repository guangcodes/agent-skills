#!/usr/bin/env python3
"""Create the canonical, content-stable workspace snapshot used for handoff."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any


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


def current_head(root: Path) -> str:
    try:
        return os.fsdecode(run_git(root, "rev-parse", "HEAD")).strip()
    except ValueError as exc:
        raise ValueError(
            "workspace snapshots require a Git repository with an initial commit"
        ) from exc


def resolve_tree(root: Path, revision: str) -> str:
    try:
        return os.fsdecode(
            run_git(root, "rev-parse", "--verify", f"{revision}^{{tree}}")
        ).strip()
    except ValueError as exc:
        raise ValueError(f"cannot resolve review baseline tree: {revision}") from exc


def collect_workspace_paths(root: Path, baseline_tree: str) -> dict[str, Any]:
    head = current_head(root)
    resolved_baseline = resolve_tree(root, baseline_tree)
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
        "head": head,
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


def reject_changed_gitlinks(changed: list[str], gitlinks: list[str]) -> None:
    changed_gitlinks = sorted(set(changed) & set(gitlinks))
    if changed_gitlinks:
        raise ValueError(
            "changed gitlinks are not supported in reusable snapshots: "
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
    for relative in paths["untracked_paths"]:
        metadata = (root / relative).lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"untracked path is not a regular file: {relative}")
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


def compute_workspace_snapshot(root: Path, baseline_tree: str) -> dict[str, Any]:
    paths = collect_workspace_paths(root, baseline_tree)
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--baseline", required=True)
    args = parser.parse_args()
    try:
        root = resolve_git_root(args.project_root)
        snapshot = compute_workspace_snapshot(root, args.baseline)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=True, sort_keys=True))
        return 2
    print(json.dumps(snapshot, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
