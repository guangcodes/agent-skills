#!/usr/bin/env python3
"""Link canonical Skill directories into a user-level discovery directory."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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
    for entry in catalog.get("skills", []):
        source = (ROOT / entry["path"]).resolve()
        target = install_root / entry["name"]
        if target.is_symlink() and target.resolve() == source:
            print(f"unchanged {target}")
            continue
        if os.path.lexists(target):
            raise SystemExit(f"refusing to replace existing path: {target}")
        target.symlink_to(source, target_is_directory=True)
        print(f"linked {target} -> {source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
