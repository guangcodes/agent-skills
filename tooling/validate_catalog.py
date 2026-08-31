#!/usr/bin/env python3
"""Validate canonical skills, catalog references, and bundle metadata."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path.relative_to(ROOT)}: {exc}") from exc


def frontmatter_value(text: str, key: str) -> str | None:
    match = re.search(rf"(?m)^{re.escape(key)}:\s*[\"']?([^\n\"']+)", text)
    return match.group(1).strip() if match else None


def main() -> int:
    errors: list[str] = []
    skills_doc = load_json(ROOT / "catalog/skills.json")
    bundles_doc = load_json(ROOT / "catalog/bundles.json")
    skill_entries = skills_doc.get("skills", [])
    bundle_entries = bundles_doc.get("bundles", [])

    names = [entry.get("name") for entry in skill_entries]
    if len(names) != len(set(names)):
        errors.append("catalog/skills.json contains duplicate names")

    for entry in skill_entries:
        name = entry.get("name")
        rel_path = entry.get("path")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            errors.append(f"invalid skill name: {name!r}")
            continue
        if rel_path != f"skills/{name}":
            errors.append(f"{name}: path must be skills/{name}")
            continue
        skill_dir = ROOT / rel_path
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            errors.append(f"{name}: missing SKILL.md")
            continue
        text = skill_file.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            errors.append(f"{name}: missing YAML frontmatter")
        if frontmatter_value(text, "name") != name:
            errors.append(f"{name}: frontmatter name does not match catalog")
        if not frontmatter_value(text, "description"):
            errors.append(f"{name}: missing frontmatter description")
        for dependency in entry.get("depends_on", []):
            if dependency not in names:
                errors.append(f"{name}: unknown dependency {dependency}")

    bundle_names: set[str] = set()
    for bundle in bundle_entries:
        bundle_name = bundle.get("name")
        if not isinstance(bundle_name, str) or not NAME_RE.fullmatch(bundle_name):
            errors.append(f"invalid bundle name: {bundle_name!r}")
            continue
        if bundle_name in bundle_names:
            errors.append(f"duplicate bundle: {bundle_name}")
        bundle_names.add(bundle_name)
        for skill_name in bundle.get("skills", []):
            if skill_name not in names:
                errors.append(f"{bundle_name}: unknown skill {skill_name}")
        for harness, manifest_path in bundle.get("adapters", {}).items():
            path = ROOT / manifest_path
            if not path.is_file():
                errors.append(f"{bundle_name}: missing {harness} adapter {manifest_path}")

    actual_dirs = sorted(
        path.name for path in (ROOT / "skills").iterdir() if path.is_dir()
    )
    if actual_dirs != sorted(names):
        errors.append(
            "skills/ directories and catalog differ: "
            f"directories={actual_dirs}, catalog={sorted(names)}"
        )

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"catalog valid: {len(names)} skills, {len(bundle_names)} bundle(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
