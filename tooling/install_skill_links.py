#!/usr/bin/env python3
"""Link canonical Skill directories into a user-level discovery directory."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def link_current_skills(install_root: Path, entries: list[dict[str, object]]) -> None:
    for entry in entries:
        source = (ROOT / str(entry["path"])).resolve()
        target = install_root / str(entry["name"])
        if target.is_symlink() and target.resolve() == source:
            print(f"unchanged {target}")
            continue
        if os.path.lexists(target):
            raise SystemExit(f"refusing to replace existing path: {target}")
        target.symlink_to(source, target_is_directory=True)
        print(f"linked {target} -> {source}")


def remove_managed_legacy_links(
    install_root: Path,
    entries: list[dict[str, object]],
) -> None:
    for entry in entries:
        for legacy_name in entry.get("renamed_from", []):
            legacy_target = install_root / str(legacy_name)
            if not legacy_target.is_symlink():
                continue
            legacy_source = (ROOT / "skills" / str(legacy_name)).resolve(strict=False)
            try:
                installed_source = legacy_target.resolve(strict=False)
            except RuntimeError:
                continue
            if installed_source != legacy_source:
                continue
            legacy_target.unlink()
            print(f"removed legacy link {legacy_target} -> {legacy_source}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--install-root",
        type=Path,
        default=Path.home() / ".agents/skills",
        help="user-level Skill discovery directory",
    )
    args = parser.parse_args()
    install_root = args.install_root.expanduser().resolve()
    install_root.mkdir(parents=True, exist_ok=True)

    catalog = json.loads((ROOT / "catalog/skills.json").read_text(encoding="utf-8"))
    entries = catalog.get("skills", [])
    link_current_skills(install_root, entries)
    remove_managed_legacy_links(install_root, entries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
