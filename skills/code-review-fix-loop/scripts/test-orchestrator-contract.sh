#!/usr/bin/env bash
set -u

skill_dir=$(cd "$(dirname "$0")/.." && pwd -P)
wrapper="$skill_dir/scripts/run-review.sh"
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
assert_contains 'validation_status = unverified' "$skill_dir/SKILL.md" 'review coverage and validation status are tracked separately'
assert_contains 'test_mode = none' "$skill_dir/SKILL.md" 'automated tests are disabled by default'
assert_contains 'test_mode_reason = default' "$skill_dir/SKILL.md" 'test mode selection records its source'
assert_contains '默认 review 只允许只读分析' "$skill_dir/SKILL.md" 'default review is read-only analysis'
assert_contains '显式要求“自动化测试”但未指定级别时，使用 `light`' "$skill_dir/SKILL.md" 'unspecified automated testing defaults to light'
assert_contains '显式要求“深度测试”' "$skill_dir/SKILL.md" 'explicit deep testing request selects deep mode'
assert_contains '高风险代码可以把 `none/light` 自动升级为 `deep`' "$skill_dir/SKILL.md" 'high-risk code can auto-upgrade testing to deep'
assert_contains '`test_mode_reason=risk:<evidence>`' "$skill_dir/SKILL.md" 'risk-based upgrade records concrete evidence'
assert_not_contains '高风险只能提示建议 `deep`，不得自动升级' "$skill_dir/SKILL.md" 'obsolete no-auto-upgrade rule is removed'
assert_contains '`none`：禁止运行 test、lint、typecheck、build、E2E、smoke' "$skill_dir/SKILL.md" 'none mode forbids automated verification commands'
assert_contains '`light`：只运行与改动直接对应的最小自动化测试' "$skill_dir/SKILL.md" 'light mode stays narrowly scoped'
assert_contains '`deep`：每个修复批次先运行直接相关验证；focused review 收敛后再运行一次' "$skill_dir/SKILL.md" 'deep mode defers the full matrix until review convergence'
assert_contains '完整本地自动化验证矩阵的唯一入口是 `test_mode=deep`' "$skill_dir/SKILL.md" 'only deep mode can run the full local matrix'
assert_contains '`light` 不得运行完整本地自动化验证矩阵' "$skill_dir/SKILL.md" 'light mode cannot escalate to the full local matrix'
assert_contains '`test_mode=none` 时，代码修复只能标记为 `已修复-未验证`' "$skill_dir/SKILL.md" 'untested fixes remain explicitly unverified'
assert_contains '是否采用 TDD 由仓库规则、现有测试策略、风险和用户要求决定' "$skill_dir/SKILL.md" 'TDD follows repository policy and risk'
assert_contains 'reviewer 结论不是事实本身' "$skill_dir/SKILL.md" 'findings require independent validation'
assert_contains 'Perform read-only code analysis' "$wrapper" 'focused reviewer prompt requires read-only analysis'
assert_contains 'Do not inspect Codex memory, prior task history' "$wrapper" 'reviewer prompt excludes irrelevant history'
assert_contains '默认不得因为进入 review 而运行全局 typecheck/test/build/E2E' "$skill_dir/SKILL.md" 'review does not trigger global validation by default'
assert_contains '`test_mode=deep` 且 focused review 已收敛时，运行一次' "$skill_dir/SKILL.md" 'deep validation runs once after review convergence'
assert_not_contains 'coverage_status=covered` 时结束' "$skill_dir/SKILL.md" 'completion does not require a removed final full review'
assert_contains 'validation_status：' "$skill_dir/SKILL.md" 'final report exposes validation status'
assert_contains 'test_mode：' "$skill_dir/SKILL.md" 'final report exposes the selected test mode'
assert_contains 'test_mode_reason：' "$skill_dir/SKILL.md" 'final report exposes the test mode reason'

cd "$repo" || exit 1
assert_rejects 'quiet events require an output path' --quiet-events uncommitted
assert_dry_run_contains 'uncommitted full review carries read-only instructions' 'Perform\ read-only\ code\ analysis' uncommitted
assert_dry_run_contains 'quiet uncommitted review remains read-only' 'Perform\ read-only\ code\ analysis' --quiet-events --output "$tmp_dir/result.txt" uncommitted
assert_dry_run_contains 'base full review carries read-only instructions' 'Perform\ read-only\ code\ analysis' base "$base_branch"
assert_dry_run_contains 'commit full review carries read-only instructions' 'Perform\ read-only\ code\ analysis' commit HEAD
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
quiet_result="$tmp_dir/quiet-result.txt"
quiet_stdout="$tmp_dir/quiet-stdout.txt"
reviewer_pwd="$tmp_dir/reviewer-pwd.txt"
if PATH="$mock_bin:$PATH" MOCK_PWD_FILE="$reviewer_pwd" "$wrapper" --quiet-events --output "$quiet_result" uncommitted >"$quiet_stdout" &&
  grep -Fq 'review completed; result=' "$quiet_stdout" &&
  grep -Fq 'final reviewer result' "$quiet_result" &&
  [[ $(wc -l <"${quiet_result}.events.jsonl") -eq 2 ]] &&
  grep -Fq 'mock codex warning' "${quiet_result}.stderr.log" &&
  ! grep -Fq 'mock codex warning' "$quiet_stdout" &&
  grep -Fq 'events_lines=2' "${quiet_result}.metrics" &&
  grep -Fq 'stderr_lines=1' "${quiet_result}.metrics" &&
  [[ $(<"$reviewer_pwd") == "$repo_physical" ]] &&
  grep -Fq 'exit_status=0' "${quiet_result}.metrics"; then
  pass 'quiet mode isolates reviewer streams and runs from the repository root'
else
  fail 'quiet mode isolates reviewer streams and runs from the repository root'
fi

relative_stdout="$tmp_dir/relative-stdout.txt"
if (cd "$repo/docs" && PATH="$mock_bin:$PATH" "$wrapper" --quiet-events --output relative-result.txt uncommitted >"$relative_stdout") &&
  grep -Fq 'final reviewer result' "$repo/docs/relative-result.txt" &&
  [[ -f "$repo/docs/relative-result.txt.events.jsonl" ]] &&
  [[ -f "$repo/docs/relative-result.txt.stderr.log" ]] &&
  [[ -f "$repo/docs/relative-result.txt.metrics" ]] &&
  [[ ! -e "$repo/relative-result.txt" ]]; then
  pass 'relative output paths remain anchored to the caller directory'
else
  fail 'relative output paths remain anchored to the caller directory'
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
