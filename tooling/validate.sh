#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/.." && pwd -P)
python3 "$repo_root/tooling/validate_catalog.py"

validator=${SKILL_VALIDATOR:-$HOME/.codex/skills/.system/skill-creator/scripts/quick_validate.py}
if [[ -f "$validator" ]]; then
  while IFS= read -r skill_dir; do
    python3 "$validator" "$skill_dir"
  done < <(find "$repo_root/skills" -mindepth 1 -maxdepth 1 -type d | sort)
else
  printf 'official Skill validator not found; catalog validation remains active\n' >&2
fi

"$repo_root/skills/code-review-fix-loop/scripts/test-orchestrator-contract.sh"
python3 -m unittest discover -s "$repo_root/skills/local-diff-quality-supervisor/tests" -p 'test_*.py'
python3 -m unittest discover -s "$repo_root/skills/bounded-review-supervisor/tests" -p 'test_*.py'
bash "$repo_root/skills/bounded-review-supervisor/tests/test_contract.sh" \
  "$repo_root/skills/bounded-review-supervisor"

python3 "$repo_root/tooling/package_codex_plugin.py" review-workflows >/dev/null

plugin_validator=${PLUGIN_VALIDATOR:-$HOME/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py}
if [[ -f "$plugin_validator" ]]; then
  python3 "$plugin_validator" "$repo_root/dist/codex/review-workflows"
else
  printf 'official Plugin validator not found; package was still materialized\n' >&2
fi

printf 'agent-skills validation passed\n'
