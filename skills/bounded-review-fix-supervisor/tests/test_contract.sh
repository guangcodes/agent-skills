#!/usr/bin/env bash
set -euo pipefail

skill_dir=${1:?usage: test-bounded-review-fix-supervisor-contract.sh <skill-dir>}
skill_file="$skill_dir/SKILL.md"
agent_file="$skill_dir/agents/openai.yaml"
state_script="$skill_dir/scripts/supervisor-state.py"
contracts_file="$skill_dir/references/contracts.md"
invariants_file="$skill_dir/references/invariants.md"

[[ -f "$skill_file" ]]
[[ -f "$agent_file" ]]
[[ -f "$state_script" ]]
[[ -f "$contracts_file" ]]
[[ -f "$invariants_file" ]]

grep -Fq 'name: bounded-review-fix-supervisor' "$skill_file"
grep -Fq '用户明确调用 `$bounded-review-fix-supervisor`' "$skill_file"
grep -Fq '当前存在 active Goal' "$skill_file"
grep -Fq '默认值和硬上限均为 `4`' "$skill_file"
grep -Fq '4 × 5 = 20' "$skill_file"
grep -Fq 'max_fix_rounds_reached' "$skill_file"
grep -Fq '不在相同工作区状态上重复 bug review' "$skill_file"
grep -Fq '直接复用其检查点' "$skill_file"
grep -Fq 'adopt-fix-result' "$skill_file"
grep -Fq 'begin-fix-window' "$skill_file"
grep -Fq 'review_checkpoint.source=code-review-fix-loop' "$skill_file"
grep -Fq '设置 `supervisor_handoff=required`' "$skill_file"
grep -Fq '`review_checkpoint.reviewed_diff_hash`' "$skill_file"
grep -Fq '`review_checkpoint.changed_paths`' "$skill_file"
grep -Fq 'workspace-snapshot.py' "$skill_file"
grep -Fq 'snapshot_contract=git-cumulative-diff-sha256-v1' "$skill_file"
grep -Fq '不存在时完全省略' "$skill_file"
grep -Fq '用户未指定时只能是 `light/default`' "$skill_file"
grep -Fq '最多可能出现 `max_fix_windows` 次' "$skill_file"
grep -Fq 'REQUIRED SUB-SKILL:** `review-fix-alignment-supervisor`' "$skill_file"
grep -Fq '状态进入 `alignment_required`' "$skill_file"
grep -Fq 'record-alignment' "$skill_file"
grep -Fq '`ask_developer`' "$skill_file"
grep -Fq '不自动 commit、push、创建 PR、merge、部署' "$skill_file"

grep -Fq 'STATE_VERSION = 8' "$state_script"
grep -Fq 'MAX_FIX_WINDOWS = 4' "$state_script"
grep -Fq 'CHILD_FIX_ROUND_LIMIT = 5' "$state_script"
grep -Fq 'def command_begin_fix_window' "$state_script"
grep -Fq 'INITIAL_REVIEW_PROMPT' "$state_script"
grep -Fq 'Do not run test, lint, typecheck, build' "$state_script"
grep -Fq 'child_review_checkpoint_stale' "$state_script"
grep -Fq 'invalid_child_review_checkpoint' "$state_script"
grep -Fq 'def command_adopt_fix_result' "$state_script"
grep -Fq 'exit_test_skip_reason' "$state_script"
grep -Fq 'child_requirement_digest_mismatch' "$state_script"
grep -Fq 'child_requirement_snapshot_mismatch' "$state_script"
grep -Fq 'alignment_directive_contract_mismatch' "$state_script"
grep -Fq 'alignment_directive_unresolved' "$state_script"
grep -Fq 'review_checkpoint_digest' "$state_script"
grep -Fq 'unsafe_tracked_worktree_file' "$state_script"
grep -Fq '"requirements_snapshot"' "$contracts_file"
grep -Fq '"alignment_directive_status"' "$contracts_file"
grep -Fq 'SNAPSHOT_CONTRACT = "git-cumulative-diff-sha256-v1"' "$state_script"
grep -Fq 'max_fix_windows_reached' "$state_script"
grep -Fq 'def command_record_alignment' "$state_script"
grep -Fq 'alignment_requires_developer_decision' "$state_script"

grep -Fq 'status=incomplete' "$contracts_file"
grep -Fq 'exit_reason=max_fix_rounds_reached' "$contracts_file"
grep -Fq 'review_checkpoint' "$contracts_file"
grep -Fq 'not_actionable' "$contracts_file"
grep -Fq '状态 schema 为 v8' "$contracts_file"
grep -Fq 'alignment-result.json' "$contracts_file"
grep -Fq '"review_checkpoint_digest"' "$contracts_file"
grep -Fq '跨窗口 Review 检查点' "$invariants_file"
grep -Fq 'fail closed' "$invariants_file"
grep -Fq '禁用 lazy fetch' "$invariants_file"
grep -Fq 'changed tracked path' "$invariants_file"

grep -Fq '$bounded-review-fix-supervisor' "$agent_file"
grep -Fq 'allow_implicit_invocation: false' "$agent_file"

if grep -R -Fq 'bounded-review-supervisor' "$skill_file" "$agent_file" "$state_script"; then
  printf '%s\n' 'renamed skill still contains the obsolete invocation name' >&2
  exit 1
fi

printf '%s\n' 'bounded review-fix supervisor contract passed'
