#!/usr/bin/env bash
set -u

skill_dir=$(cd "$(dirname "$0")/.." && pwd -P)
wrapper="$skill_dir/scripts/run-review.sh"
snapshot_helper="$skill_dir/scripts/workspace-snapshot.py"
handoff_contract="$skill_dir/references/handoff-contract.md"
agent_file="$skill_dir/agents/openai.yaml"
failures=0

pass() {
  printf 'PASS: %s\n' "$1"
}

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  failures=$((failures + 1))
}

assert_contains() {
  local pattern=$1
  local file=$2
  local label=$3
  if grep -Fq -- "$pattern" "$file"; then
    pass "$label"
  else
    fail "$label"
  fi
}

assert_not_contains() {
  local pattern=$1
  local file=$2
  local label=$3
  if grep -Fq -- "$pattern" "$file"; then
    fail "$label"
  else
    pass "$label"
  fi
}

assert_before() {
  local first=$1
  local second=$2
  local file=$3
  local label=$4
  local first_line
  local second_line
  first_line=$(grep -nF -- "$first" "$file" | head -n 1 | cut -d: -f1)
  second_line=$(grep -nF -- "$second" "$file" | head -n 1 | cut -d: -f1)
  if [[ -n "$first_line" && -n "$second_line" ]] && (( first_line < second_line )); then
    pass "$label"
  else
    fail "$label"
  fi
}

assert_accepts() {
  local label=$1
  shift
  if "$wrapper" --dry-run "$@" >/dev/null 2>&1; then
    pass "$label"
  else
    fail "$label"
  fi
}

assert_rejects() {
  local label=$1
  shift
  if "$wrapper" --dry-run "$@" >/dev/null 2>&1; then
    fail "$label"
  else
    pass "$label"
  fi
}

assert_dry_run_contains() {
  local label=$1
  local pattern=$2
  shift 2
  local output
  if output=$("$wrapper" --dry-run "$@" 2>&1) && grep -Fq -- "$pattern" <<<"$output"; then
    pass "$label"
  else
    fail "$label"
  fi
}

tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/code-review-fix-loop-test.XXXXXX")
trap 'rm -rf "$tmp_dir"' EXIT
repo="$tmp_dir/repo"
mkdir -p "$repo/docs"
repo_physical=$(cd "$repo" && pwd -P)
git -C "$repo" init -q
git -C "$repo" config user.email test@example.com
git -C "$repo" config user.name Test
printf 'before\n' >"$repo/allowed.txt"
printf 'before\n' >"$repo/unrelated.txt"
printf 'before\n' >"$repo/docs/nested.txt"
git -C "$repo" add allowed.txt unrelated.txt docs/nested.txt
git -C "$repo" commit -qm initial
base_branch=$(git -C "$repo" branch --show-current)
printf 'after\n' >"$repo/allowed.txt"
printf 'after\n' >"$repo/unrelated.txt"
printf 'after\n' >"$repo/docs/nested.txt"

scope_file="$tmp_dir/scope.txt"
printf 'allowed.txt\n' >"$scope_file"
bad_scope_file="$tmp_dir/bad-scope.txt"
printf ':(top)**\n' >"$bad_scope_file"
directory_scope_file="$tmp_dir/directory-scope.txt"
printf 'docs\n' >"$directory_scope_file"

assert_contains 'max_fix_rounds = 5' "$skill_dir/SKILL.md" 'repair-batch limit remains five'
assert_not_contains 'superpowers:' "$skill_dir/SKILL.md" 'removed Superpowers references do not remain'
assert_not_contains 'REQUIRED SUB-SKILL:' "$skill_dir/SKILL.md" 'orchestrator has no dangling mandatory sub-skill references'
assert_not_contains 'max_final_review_attempts' "$skill_dir/SKILL.md" 'final full-review attempt state is removed'
assert_not_contains '最终全量 review' "$skill_dir/SKILL.md" 'final full-review phase is removed'
assert_contains '没有修复项时写 `无已修复项`' "$skill_dir/SKILL.md" 'zero-fix report has an explicit value'
assert_contains 'cumulative_review_count = 0' "$skill_dir/SKILL.md" 'cumulative review count starts at zero'
assert_contains 'validation_status = unverified' "$skill_dir/SKILL.md" 'review coverage and validation status are tracked separately'
assert_contains 'round_test_mode = none' "$skill_dir/SKILL.md" 'repair rounds never schedule loop-owned automated tests'
assert_contains 'test_mode = light' "$skill_dir/SKILL.md" 'exit validation defaults to light'
assert_contains 'test_mode_reason = default' "$skill_dir/SKILL.md" 'exit test mode selection records its source'
assert_contains 'exit_test_count = 0' "$skill_dir/SKILL.md" 'exit validation starts with a zero count'
assert_contains 'exit_test_skip_reason = unset' "$skill_dir/SKILL.md" 'exit validation skip reason starts unset'
assert_contains 'review_baseline = unset' "$skill_dir/SKILL.md" 'cumulative review freezes an explicit baseline'
assert_contains 'current_diff_paths = []' "$skill_dir/SKILL.md" 'cumulative review tracks the complete current path set'
assert_contains 'last_cumulative_review_diff_hash = unset' "$skill_dir/SKILL.md" 'cumulative review records its accepted diff snapshot'
assert_contains 'snapshot_contract = git-cumulative-diff-sha256-v1' "$skill_dir/SKILL.md" 'handoff uses a versioned canonical snapshot contract'
[[ -f "$snapshot_helper" ]] || fail 'canonical workspace snapshot helper exists'
[[ -f "$handoff_contract" ]] || fail 'self-contained handoff contract exists'
assert_contains 'supervisor_handoff = available' "$skill_dir/SKILL.md" 'reusable supervisor handoff is available by default'
assert_contains 'requirement_digest = unset' "$skill_dir/SKILL.md" 'handoff binds the original requirement snapshot'
assert_contains 'requirements_snapshot = unset' "$skill_dir/SKILL.md" 'handoff retains the digest preimage'
assert_contains '"requirements_snapshot"' "$handoff_contract" 'standalone handoff contains the frozen requirement snapshot'
assert_contains '"alignment_directive_status"' "$handoff_contract" 'handoff reports correction-directive resolution status'
assert_contains 'cumulative_changed_paths = []' "$skill_dir/SKILL.md" 'cumulative changed paths drive exit validation'
assert_contains '`failed`、`blocked`' "$skill_dir/SKILL.md" 'failed validation is distinct from blocked validation'
assert_contains '所有 review 只允许只读分析' "$skill_dir/SKILL.md" 'default review is read-only analysis'
assert_contains '用户未提及自动化测试：使用 `light`' "$skill_dir/SKILL.md" 'unspecified automated testing defaults to exit light'
assert_contains '只有用户明确要求“深度测试”' "$skill_dir/SKILL.md" 'only an explicit user request selects deep'
assert_contains '用户明确禁止自动化测试时使用 `none`' "$skill_dir/SKILL.md" 'an explicit no-test request disables exit validation'
assert_contains '调用方只能原样透传' "$skill_dir/SKILL.md" 'caller may relay only the explicit user-selected mode'
assert_contains '用户未指定时必须省略' "$skill_dir/SKILL.md" 'caller omits mode fields when the user is silent'
assert_contains '不得默认、推断、建议、升级或降级' "$skill_dir/SKILL.md" 'caller cannot synthesize or alter the user-selected mode'
assert_contains '本 Skill 必须对照用户原文核验透传值' "$skill_dir/SKILL.md" 'child verifies passthrough provenance against user text'
assert_contains '都不得把 `none/light` 自动升级为 `deep`' "$skill_dir/SKILL.md" 'risk never auto-upgrades exit validation'
assert_not_contains '高风险代码可以把 `none/light` 自动升级为 `deep`' "$skill_dir/SKILL.md" 'obsolete risk-based auto-upgrade is removed'
assert_contains '`deep_not_requested`' "$skill_dir/SKILL.md" 'high-risk light validation is recorded in the ledger'
assert_contains '由提出要求的流程负责调度和管理' "$skill_dir/SKILL.md" 'externally required TDD and preflight retain their owner'
assert_contains '不计入 `exit_test_count`' "$skill_dir/SKILL.md" 'external tests do not consume loop exit validation count'
assert_contains '`light`：退出时只运行覆盖全部 `cumulative_changed_paths`' "$skill_dir/SKILL.md" 'light validation covers cumulative changes once at exit'
assert_contains '`deep`：退出时运行一次' "$skill_dir/SKILL.md" 'deep validation runs only in the exit phase'
assert_contains '每条命令只执行一次' "$skill_dir/SKILL.md" 'the frozen exit validation phase does not repeat commands'
assert_contains '不得返回修复循环' "$skill_dir/SKILL.md" 'exit validation failures do not restart remediation'
assert_contains '测试发现的问题必须进入同一台账' "$skill_dir/SKILL.md" 'exit test findings are reported in the main ledger'
assert_contains 'exit_reason=max_fix_rounds_reached' "$skill_dir/SKILL.md" 'five-round exhaustion has a continuable supervisor exit reason'
assert_contains 'review_checkpoint=null' "$skill_dir/SKILL.md" 'invalid review evidence cannot be synthesized for a supervisor'
assert_contains '`reviewed_diff_hash`' "$skill_dir/SKILL.md" 'supervisor checkpoint uses the formal reviewed diff hash field'
assert_contains '`changed_paths`' "$skill_dir/SKILL.md" 'supervisor checkpoint uses the formal changed paths field'
assert_contains '由 `workspace-snapshot.py` 生成的当前快照字段' "$skill_dir/SKILL.md" 'supervisor checkpoint fields come from the self-contained snapshot helper'
assert_contains '是否开启下一个五轮窗口由上层决定' "$skill_dir/SKILL.md" 'parent owns cross-window continuation'
assert_contains '存在未解决指令时不得返回 `status=complete`' "$skill_dir/SKILL.md" 'an unresolved redirect cannot be discarded by a clean review'
assert_contains '没有产生持久化修改' "$skill_dir/SKILL.md" 'zero-change exits skip automated validation'
assert_contains '### 修复后累计全量 review' "$skill_dir/SKILL.md" 'post-fix review is explicitly cumulative and complete'
assert_contains '不得只审 `round_paths`' "$skill_dir/SKILL.md" 'round paths do not constrain the reviewer scope'
assert_contains '不再要求必须由本轮 `round_paths` 引入' "$skill_dir/SKILL.md" 'cumulative findings are not limited to the latest batch'
assert_contains '把 `coverage_status` 设为 `covered`' "$skill_dir/SKILL.md" 'a stable complete cumulative review establishes coverage'
assert_not_contains 'focused_review_count' "$skill_dir/SKILL.md" 'obsolete focused-review counter is removed'
assert_not_contains '中间轮次只审查 `round_paths`' "$skill_dir/SKILL.md" 'obsolete latest-batch-only scope is removed'
assert_not_contains 'Focused review 只能把' "$skill_dir/SKILL.md" 'obsolete focused finding restriction is removed'
assert_before '**累计全量 review**' '**退出验证与报告**' "$skill_dir/SKILL.md" 'cumulative review precedes the one exit validation phase'
assert_contains '$code-review-fix-loop' "$agent_file" 'default prompt explicitly invokes the skill'
assert_contains '完整累计 diff 做一次只读静态 review' "$agent_file" 'default prompt reflects post-fix cumulative static review'
assert_contains '默认 light 的退出验证' "$agent_file" 'default prompt reflects exit-only light validation'
assert_contains '只有我明确要求时才使用 deep' "$agent_file" 'default prompt reflects explicit-only deep validation'
assert_contains 'reviewer 结论不是事实本身' "$skill_dir/SKILL.md" 'findings require independent validation'
assert_contains 'Perform read-only code analysis' "$wrapper" 'reviewer prompt requires read-only analysis'
assert_contains 'codex exec --sandbox read-only review' "$wrapper" 'reviewer command enforces a read-only sandbox'
assert_contains '初始 commit' "$skill_dir/SKILL.md" 'working-tree review explicitly requires an initial commit'
assert_contains 'Do not inspect Codex memory, prior task history' "$wrapper" 'reviewer prompt excludes irrelevant history'
assert_contains 'GIT_NO_LAZY_FETCH=1' "$wrapper" 'reviewer and Git commands cannot trigger lazy object fetches'
assert_contains 'GIT_TERMINAL_PROMPT=0' "$wrapper" 'reviewer and Git commands cannot prompt for credentials'
assert_contains 'must be outside the Git worktree' "$wrapper" 'review artifacts cannot enter the frozen workspace'
assert_contains '所有 review 只允许只读分析' "$skill_dir/SKILL.md" 'review never runs automated validation'
assert_not_contains 'coverage_status=covered` 时结束' "$skill_dir/SKILL.md" 'completion does not require a removed final full review'
assert_contains 'validation_status：' "$skill_dir/SKILL.md" 'final report exposes validation status'
assert_contains 'round_test_mode：' "$skill_dir/SKILL.md" 'final report exposes the immutable round mode'
assert_contains 'test_mode：' "$skill_dir/SKILL.md" 'final report exposes the selected test mode'
assert_contains 'test_mode_reason：' "$skill_dir/SKILL.md" 'final report exposes the test mode reason'
assert_contains 'exit_test_count：' "$skill_dir/SKILL.md" 'final report exposes exit validation count'
assert_contains 'exit_test_status：' "$skill_dir/SKILL.md" 'final report exposes exit validation status'
assert_contains 'exit_test_skip_reason：' "$skill_dir/SKILL.md" 'final report exposes exit validation skip reason'
assert_contains '| `待修复` | `actionable` |' "$handoff_contract" 'handoff maps actionable ledger findings'
assert_contains '| `不成立` | `not_actionable` |' "$handoff_contract" 'handoff maps rejected ledger findings'

cd "$repo" || exit 1
assert_rejects 'quiet events require an output path' --quiet-events uncommitted
assert_rejects 'review output outside system temp is rejected' --output /review-result.txt uncommitted
assert_dry_run_contains 'uncommitted full review carries read-only instructions' 'Perform\ read-only\ code\ analysis' uncommitted
assert_dry_run_contains 'quiet uncommitted review remains read-only' 'Perform\ read-only\ code\ analysis' --quiet-events --output "$tmp_dir/result.txt" uncommitted
assert_dry_run_contains 'base full review carries read-only instructions' 'Perform\ read-only\ code\ analysis' base "$base_branch"
assert_dry_run_contains 'commit full review carries read-only instructions' 'Perform\ read-only\ code\ analysis' commit HEAD
assert_rejects 'cumulative mode requires a baseline revision' cumulative
assert_dry_run_contains 'cumulative review carries complete-current-diff instructions' 'Review\ the\ complete\ current\ code\ diff' cumulative HEAD
assert_dry_run_contains 'cumulative review includes every working-tree change class' 'all\ current\ staged\,\ unstaged\,\ and\ untracked\ changes' cumulative HEAD
assert_dry_run_contains 'cumulative review enforces a read-only sandbox' '--sandbox read-only' cumulative HEAD
if (cd "$repo/docs" && "$wrapper" --dry-run cumulative HEAD >/dev/null 2>&1); then
  pass 'cumulative mode resolves the frozen baseline from a subdirectory'
else
  fail 'cumulative mode resolves the frozen baseline from a subdirectory'
fi
assert_rejects 'cumulative mode rejects an unknown baseline revision' cumulative does-not-exist
assert_rejects 'paths mode requires an explicit scope file' paths allowed.txt
assert_accepts 'paths mode accepts an in-scope changed file' --scope-file "$scope_file" paths allowed.txt
if (cd "$repo/docs" && "$wrapper" --dry-run --scope-file "$scope_file" paths allowed.txt >/dev/null 2>&1); then
  pass 'paths mode resolves repository-relative files from a subdirectory'
else
  fail 'paths mode resolves repository-relative files from a subdirectory'
fi
assert_rejects 'paths mode rejects a changed file outside the scope' --scope-file "$scope_file" paths unrelated.txt
assert_rejects 'paths mode rejects Git pathspec magic' --scope-file "$bad_scope_file" paths ':(top)**'
assert_rejects 'paths mode rejects directory scopes' --scope-file "$directory_scope_file" paths docs

mock_bin="$tmp_dir/bin"
mkdir -p "$mock_bin"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'if [[ -n "${MOCK_ARGS_FILE:-}" ]]; then printf "%s\n" "$@" >"$MOCK_ARGS_FILE"; fi' \
  'output_path=""' \
  'while (( $# > 0 )); do' \
  '  if [[ "$1" == "--output-last-message" ]]; then output_path=$2; shift 2; else shift; fi' \
  'done' \
  'if [[ -n "${MOCK_PWD_FILE:-}" ]]; then pwd >"$MOCK_PWD_FILE"; fi' \
  'if [[ "${MOCK_SKIP_RESULT:-false}" != "true" ]]; then printf "final reviewer result\n" >"$output_path"; fi' \
  'printf "{\"type\":\"turn.started\"}\n{\"type\":\"turn.completed\"}\n"' \
  'printf "mock codex warning\n" >&2' \
  'exit "${MOCK_CODEX_STATUS:-0}"' \
  >"$mock_bin/codex"
chmod +x "$mock_bin/codex"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "%064d\n" 0' \
  >"$mock_bin/codex-notify"
chmod +x "$mock_bin/codex-notify"
quiet_result="$tmp_dir/quiet-result.txt"
quiet_stdout="$tmp_dir/quiet-stdout.txt"
reviewer_pwd="$tmp_dir/reviewer-pwd.txt"
reviewer_args="$tmp_dir/reviewer-args.txt"
if PATH="$mock_bin:$PATH" MOCK_PWD_FILE="$reviewer_pwd" MOCK_ARGS_FILE="$reviewer_args" "$wrapper" --quiet-events --output "$quiet_result" uncommitted >"$quiet_stdout" &&
  grep -Fq 'review completed; result=' "$quiet_stdout" &&
  grep -Fq 'final reviewer result' "$quiet_result" &&
  [[ $(wc -l <"${quiet_result}.events.jsonl") -eq 2 ]] &&
  grep -Fq 'mock codex warning' "${quiet_result}.stderr.log" &&
  ! grep -Fq 'mock codex warning' "$quiet_stdout" &&
  grep -Fq 'events_lines=2' "${quiet_result}.metrics" &&
  grep -Fq 'stderr_lines=1' "${quiet_result}.metrics" &&
  [[ $(<"$reviewer_pwd") == "$repo_physical" ]] &&
  [[ $(sed -n '1p' "$reviewer_args") == exec ]] &&
  [[ $(sed -n '2p' "$reviewer_args") == --sandbox ]] &&
  [[ $(sed -n '3p' "$reviewer_args") == read-only ]] &&
  [[ $(sed -n '4p' "$reviewer_args") == review ]] &&
  grep -Fq 'exit_status=0' "${quiet_result}.metrics"; then
  pass 'quiet mode isolates reviewer streams and runs from the repository root'
else
  fail 'quiet mode isolates reviewer streams and runs from the repository root'
fi

unborn_repo="$tmp_dir/unborn-repo"
mkdir -p "$unborn_repo"
git -C "$unborn_repo" init -q
unborn_wrapper_stderr="$tmp_dir/unborn-wrapper.stderr"
if (cd "$unborn_repo" && "$wrapper" --dry-run uncommitted >/dev/null 2>"$unborn_wrapper_stderr"); then
  fail 'uncommitted review rejects an unborn HEAD'
elif grep -Fq 'initial commit' "$unborn_wrapper_stderr"; then
  pass 'uncommitted review rejects an unborn HEAD'
else
  fail 'uncommitted review rejects an unborn HEAD'
fi
if (cd "$unborn_repo" && "$wrapper" --dry-run cumulative HEAD >/dev/null 2>"$unborn_wrapper_stderr"); then
  fail 'cumulative review rejects an unborn HEAD'
elif grep -Fq 'initial commit' "$unborn_wrapper_stderr"; then
  pass 'cumulative review rejects an unborn HEAD'
else
  fail 'cumulative review rejects an unborn HEAD'
fi
unborn_snapshot_output="$tmp_dir/unborn-snapshot.json"
if python3 "$snapshot_helper" --project-root "$unborn_repo" --baseline HEAD >"$unborn_snapshot_output"; then
  fail 'workspace snapshot rejects an unborn HEAD'
elif grep -Fq 'initial commit' "$unborn_snapshot_output"; then
  pass 'workspace snapshot rejects an unborn HEAD'
else
  fail 'workspace snapshot rejects an unborn HEAD'
fi

if (cd "$repo/docs" && PATH="$mock_bin:$PATH" "$wrapper" --dry-run --output relative-result.txt uncommitted >/dev/null 2>&1); then
  fail 'review output inside the worktree is rejected'
else
  pass 'review output inside the worktree is rejected'
fi

failed_result="$tmp_dir/failed-result.txt"
failed_stderr="$tmp_dir/failed-stderr.txt"
PATH="$mock_bin:$PATH" MOCK_CODEX_STATUS=7 "$wrapper" --quiet-events --output "$failed_result" uncommitted >/dev/null 2>"$failed_stderr"
failed_status=$?
if [[ $failed_status -eq 7 ]] &&
  grep -Fq 'review failed with status 7' "$failed_stderr" &&
  grep -Fq 'exit_status=7' "${failed_result}.metrics"; then
  pass 'quiet events preserve reviewer failure status and metrics'
else
  fail 'quiet events preserve reviewer failure status and metrics'
fi

stale_result="$tmp_dir/stale-result.txt"
printf 'stale reviewer result\n' >"$stale_result"
stale_stderr="$tmp_dir/stale-stderr.txt"
PATH="$mock_bin:$PATH" MOCK_SKIP_RESULT=true "$wrapper" --quiet-events --output "$stale_result" uncommitted >/dev/null 2>"$stale_stderr"
stale_status=$?
if [[ $stale_status -ne 0 ]] &&
  [[ ! -s "$stale_result" ]] &&
  grep -Fq 'no fresh nonempty result' "$stale_stderr" &&
  grep -Fq 'result_bytes=0' "${stale_result}.metrics" &&
  grep -Fq 'exit_status=1' "${stale_result}.metrics"; then
  pass 'wrapper rejects successful reviewer exits without a fresh result'
else
  fail 'wrapper rejects successful reviewer exits without a fresh result'
fi

if (( failures > 0 )); then
  printf '%d contract test(s) failed\n' "$failures" >&2
  exit 1
fi

printf 'All orchestrator contract tests passed\n'
