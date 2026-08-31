#!/usr/bin/env python3
"""Materialize a Codex plugin from canonical Skill sources."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIST_ROOT = ROOT / "dist/codex"
IGNORES = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def find_bundle(name: str) -> dict:
    bundles = load_json(ROOT / "catalog/bundles.json").get("bundles", [])
    for bundle in bundles:
        if bundle.get("name") == name:
            return bundle
    available = ", ".join(sorted(str(item.get("name")) for item in bundles))
    raise SystemExit(f"unknown bundle {name!r}; available: {available}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", help="bundle name from catalog/bundles.json")
    args = parser.parse_args()

    bundle = find_bundle(args.bundle)
    manifest_rel = bundle.get("adapters", {}).get("codex")
    if not manifest_rel:
        raise SystemExit(f"bundle {args.bundle!r} has no Codex adapter")
    manifest = load_json(ROOT / manifest_rel)
    if manifest.get("name") != args.bundle:
        raise SystemExit("Codex manifest name must match bundle name")
    if manifest.get("version") != bundle.get("version"):
        raise SystemExit("Codex manifest version must match bundle version")

    target = (DIST_ROOT / args.bundle).resolve()
    dist_root = DIST_ROOT.resolve()
    if target.parent != dist_root:
        raise SystemExit("refusing to package outside dist/codex")
    if target.exists():
        shutil.rmtree(target)
    (target / ".codex-plugin").mkdir(parents=True)
    (target / "skills").mkdir()
    (target / ".codex-plugin/plugin.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    for skill_name in bundle.get("skills", []):
        source = ROOT / "skills" / skill_name
        if not (source / "SKILL.md").is_file():
            raise SystemExit(f"missing canonical skill: {skill_name}")
        shutil.copytree(source, target / "skills" / skill_name, ignore=IGNORES)

    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
