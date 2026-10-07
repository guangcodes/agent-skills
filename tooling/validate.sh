#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/.." && pwd -P)
python_bin=${AGENT_SKILLS_PYTHON:-python3}
"$python_bin" "$repo_root/tooling/validate_catalog.py"
"$python_bin" -m unittest discover -s "$repo_root/tooling/tests" -p 'test_*.py'

validator=${SKILL_VALIDATOR:-$HOME/.codex/skills/.system/skill-creator/scripts/quick_validate.py}
if [[ -f "$validator" ]]; then
  while IFS= read -r skill_dir; do
    "$python_bin" "$validator" "$skill_dir"
  done < <(find "$repo_root/skills" -mindepth 1 -maxdepth 1 -type d | sort)
else
  printf 'official Skill validator not found; catalog validation remains active\n' >&2
fi

"$repo_root/skills/code-review-fix-loop/scripts/test-orchestrator-contract.sh"
"$python_bin" -m unittest discover -s "$repo_root/skills/bounded-review-fix-supervisor/tests" -p 'test_*.py'
bash "$repo_root/skills/bounded-review-fix-supervisor/tests/test_contract.sh" \
  "$repo_root/skills/bounded-review-fix-supervisor"
"$python_bin" -m unittest discover -s "$repo_root/skills/review-fix-alignment-supervisor/tests" -p 'test_*.py'

plugin_validator=${PLUGIN_VALIDATOR:-$HOME/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py}
while IFS= read -r bundle_name; do
  "$python_bin" "$repo_root/tooling/package_codex_plugin.py" "$bundle_name" >/dev/null
  if [[ -f "$plugin_validator" ]]; then
    "$python_bin" "$plugin_validator" "$repo_root/dist/codex/$bundle_name"
  fi
done < <(
  "$python_bin" -c \
    'import json, pathlib, sys; doc=json.loads((pathlib.Path(sys.argv[1]) / "catalog" / "bundles.json").read_text()); print("\n".join(bundle["name"] for bundle in doc["bundles"] if "codex" in bundle.get("adapters", {})))' \
    "$repo_root"
)

if [[ ! -f "$plugin_validator" ]]; then
  printf 'official Plugin validator not found; packages were still materialized\n' >&2
fi

printf 'agent-skills validation passed\n'
