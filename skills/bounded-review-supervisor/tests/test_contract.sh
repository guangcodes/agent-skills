#!/usr/bin/env bash
set -euo pipefail

skill_dir=${1:?usage: test-bounded-review-supervisor-contract.sh <skill-dir>}
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

grep -Fq '默认值和硬上限均为 `4`' "$skill_file"
grep -Fq 'requirement_drift_check' "$skill_file"
grep -Fq '完整累计 diff' "$skill_file"
grep -Fq 'code-review-fix-loop' "$skill_file"
grep -Fq '项目外持久化写入' "$skill_file"
grep -Fq '最多为第 `4` 次' "$skill_file"
grep -Fq '不得开始下一次' "$skill_file"
grep -Fq '不得直接运行 test、lint、typecheck、build' "$skill_file"
grep -Fq '不得直接运行面向整个仓库的代码扫描' "$skill_file"
grep -Fq '不继承本限制' "$skill_file"
grep -Fq 'propagate_to_child_processes_or_subskills=false' "$skill_file"
grep -Fq '显式传递 requirements 中冻结的 `test_mode`' "$skill_file"
grep -Fq '`requested_test_mode`' "$skill_file"
grep -Fq 'effective_test_mode' "$contracts_file"
grep -Fq 'MAX_FULL_REVIEW_ROUNDS = 4' "$state_script"
grep -Fq 'STATE_VERSION = 4' "$state_script"
grep -Fq '不得修改 `code-review-fix-loop`' "$skill_file"
grep -Fq -- '--requirements-file' "$skill_file"
grep -Fq -- '--drift-check-file' "$skill_file"
grep -Fq 'execute-review' "$skill_file"
grep -Fq 'codex exec --sandbox read-only review --uncommitted' "$skill_file"
grep -Fq '不包含 severity 或行号' "$skill_file"
grep -Fq '统一路径快照' "$skill_file"
grep -Fq 'untracked 只允许普通文件' "$skill_file"
grep -Fq '结构化 JSON 是主协议' "$skill_file"
grep -Fq 'Markdown 仅作为兼容 fallback' "$skill_file"
grep -Fq '`no new issues` 不算 clean' "$skill_file"
grep -Fq 'JSON fingerprint 使用 `path|symbol|root-cause|trigger`' "$skill_file"
grep -Fq '只能在 `ready_for_fix` 阶段' "$skill_file"
grep -Fq '防止在 finding 出现前预先扩大范围' "$skill_file"
grep -Fq -- '--skill-result-file' "$skill_file"
grep -Fq 'Review 期间保持工作区不变' "$skill_file"
grep -Fq -- '--reason user_authorized' "$skill_file"
grep -Fq 'authorized_scope_extensions' "$skill_file"
grep -Fq '只在完整结果与冻结快照均被接受后计数' "$skill_file"
grep -Fq '$bounded-review-supervisor' "$agent_file"
grep -Fq 'allow_implicit_invocation: false' "$agent_file"
grep -Fq '用户明确调用 `$bounded-review-supervisor`' "$skill_file"
grep -Fq '当前存在 active Goal' "$skill_file"
grep -Fq '已显式触发 `$bounded-review-supervisor`' "$skill_file"
grep -Fq '不得把普通自然语言请求' "$skill_file"
grep -Fq '统一安全不变量' "$skill_file"
grep -Fq '统一 Git 快照' "$invariants_file"
grep -Fq -- '--no-renames' "$invariants_file"
grep -Fq 'untracked 默认拒绝' "$invariants_file"
grep -Fq 'JSON schema 是主协议' "$invariants_file"
grep -Fq '参数化验证矩阵' "$invariants_file"

if grep -Fq '修改 code-review-fix-loop' "$skill_file" && \
   ! grep -Fq '不得修改 `code-review-fix-loop`' "$skill_file"; then
  printf '%s\n' 'skill must preserve code-review-fix-loop' >&2
  exit 1
fi

printf '%s\n' 'bounded review supervisor contract passed'
