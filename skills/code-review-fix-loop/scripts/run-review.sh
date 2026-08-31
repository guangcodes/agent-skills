#!/usr/bin/env bash
set -euo pipefail

dry_run=false
quiet_events=false
output_path=""
scope_file=""
caller_dir=$(pwd -P)
original_review_argv=("$@")

while (( $# > 0 )); do
  case "$1" in
    --dry-run)
      dry_run=true
      shift
      ;;
    --output)
      output_path=${2:-}
      [[ -n "$output_path" ]] || { printf '%s\n' '--output requires a path' >&2; exit 2; }
      shift 2
      ;;
    --quiet-events)
      quiet_events=true
      shift
      ;;
    --scope-file)
      scope_file=${2:-}
      [[ -n "$scope_file" ]] || { printf '%s\n' '--scope-file requires a path' >&2; exit 2; }
      shift 2
      ;;
    *)
      break
      ;;
  esac
done

mode=${1:-}
[[ -n "$mode" ]] || {
  printf 'usage: run-review.sh [--dry-run] [--quiet-events] [--output <path>] uncommitted\n' >&2
  printf '       run-review.sh [--dry-run] [--quiet-events] [--output <path>] base <branch>\n' >&2
  printf '       run-review.sh [--dry-run] [--quiet-events] [--output <path>] commit <sha>\n' >&2
  printf '       run-review.sh [--dry-run] [--quiet-events] [--output <path>] --scope-file <path> paths <path> [<path> ...]\n' >&2
  exit 2
}
shift

if $quiet_events && [[ -z "$output_path" ]]; then
  printf '%s\n' '--quiet-events requires --output' >&2
  exit 2
fi

if [[ -n "$output_path" && "$output_path" != /* ]]; then
  output_path="$caller_dir/$output_path"
fi

args=(codex exec review --ephemeral --json)
if [[ -n "$output_path" ]]; then
  args+=(--output-last-message "$output_path")
fi
readonly_rules='Perform read-only code analysis. You may run only read-only inspection commands such as git status, git diff, git show, git log, rg, and sed. Report only discrete actionable findings introduced by the specified changes, using concise file/line, scenario, impact, and evidence fields. Do not modify files. Do not inspect Codex memory, prior task history, unrelated changes, or broad repository history. Do not read Git-ignored files or credential files. Do not run test, lint, typecheck, build, E2E, smoke, Docker, network, or remote commands.'
repo_root=""

case "$mode" in
  uncommitted)
    [[ -z "$scope_file" ]] || { printf '%s\n' '--scope-file is only valid with paths mode' >&2; exit 2; }
    command -v git >/dev/null 2>&1 || { printf 'git is not installed or not on PATH\n' >&2; exit 127; }
    repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || { printf 'uncommitted mode requires a Git repository\n' >&2; exit 2; }
    prompt="$readonly_rules Review all current staged, unstaged, and untracked changes in this repository. Use git status and the appropriate cached and working-tree diffs to establish the complete scope."
    args+=("$prompt")
    ;;
  base)
    [[ -z "$scope_file" ]] || { printf '%s\n' '--scope-file is only valid with paths mode' >&2; exit 2; }
    target=${1:-}
    [[ -n "$target" ]] || { printf 'base mode requires a branch\n' >&2; exit 2; }
    shift
    case "$target" in
      -*|*$'\n'*|*$'\r'*|*$'\t'*)
        printf 'base mode requires a safe branch name: %s\n' "$target" >&2
        exit 2
        ;;
    esac
    command -v git >/dev/null 2>&1 || { printf 'git is not installed or not on PATH\n' >&2; exit 127; }
    repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || { printf 'base mode requires a Git repository\n' >&2; exit 2; }
    base_commit=$(git -C "$repo_root" rev-parse --verify "${target}^{commit}" 2>/dev/null) || {
      printf 'base mode cannot resolve branch: %s\n' "$target" >&2
      exit 2
    }
    head_commit=$(git -C "$repo_root" rev-parse --verify 'HEAD^{commit}' 2>/dev/null) || {
      printf 'base mode cannot resolve HEAD\n' >&2
      exit 2
    }
    merge_base=$(git -C "$repo_root" merge-base "$base_commit" "$head_commit" 2>/dev/null) || {
      printf 'base mode cannot find a merge base for: %s\n' "$target" >&2
      exit 2
    }
    prompt="$readonly_rules Review the branch changes from merge-base commit $merge_base through HEAD commit $head_commit. Restrict findings to that exact committed diff."
    args+=("$prompt")
    ;;
  commit)
    [[ -z "$scope_file" ]] || { printf '%s\n' '--scope-file is only valid with paths mode' >&2; exit 2; }
    target=${1:-}
    [[ -n "$target" ]] || { printf 'commit mode requires a sha\n' >&2; exit 2; }
    shift
    case "$target" in
      -*|*$'\n'*|*$'\r'*|*$'\t'*)
        printf 'commit mode requires a safe revision: %s\n' "$target" >&2
        exit 2
        ;;
    esac
    command -v git >/dev/null 2>&1 || { printf 'git is not installed or not on PATH\n' >&2; exit 127; }
    repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || { printf 'commit mode requires a Git repository\n' >&2; exit 2; }
    commit_sha=$(git -C "$repo_root" rev-parse --verify "${target}^{commit}" 2>/dev/null) || {
      printf 'commit mode cannot resolve revision: %s\n' "$target" >&2
      exit 2
    }
    prompt="$readonly_rules Review only the changes introduced by commit $commit_sha. Use git show for that exact commit and do not include working-tree or unrelated history changes."
    args+=("$prompt")
    ;;
  paths)
    (( $# > 0 )) || { printf 'paths mode requires at least one changed path\n' >&2; exit 2; }
    [[ -n "$scope_file" ]] || { printf 'paths mode requires --scope-file\n' >&2; exit 2; }
    [[ -f "$scope_file" ]] || { printf 'scope file does not exist: %s\n' "$scope_file" >&2; exit 2; }
    command -v git >/dev/null 2>&1 || { printf 'git is not installed or not on PATH\n' >&2; exit 127; }
    repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || { printf 'paths mode requires a Git repository\n' >&2; exit 2; }

    allowed_paths=()
    while IFS= read -r allowed_path || [[ -n "$allowed_path" ]]; do
      case "$allowed_path" in
        ''|/*|.|./*|..|../*|*/../*|*/..|*/./*|*/.|*//*|*/|:*|*$'\r'*|*$'\t'*)
          printf 'scope file contains an invalid repository-relative file path: %s\n' "$allowed_path" >&2
          exit 2
          ;;
      esac
      [[ ! -d "$repo_root/$allowed_path" ]] || {
        printf 'scope file must list files, not directories: %s\n' "$allowed_path" >&2
        exit 2
      }
      if git -C "$repo_root" check-ignore -q -- "$allowed_path"; then
        printf 'scope file refuses Git-ignored paths: %s\n' "$allowed_path" >&2
        exit 2
      fi
      allowed_paths+=("$allowed_path")
    done <"$scope_file"
    (( ${#allowed_paths[@]} > 0 )) || { printf 'scope file must list at least one path\n' >&2; exit 2; }

    prompt="$readonly_rules Review only the current staged, unstaged, and untracked changes in the allowlisted literal file paths below. You may read tracked direct dependencies when required for context, but do not review unrelated changes or history. The orchestrator owns any explicitly authorized testing."
    for target in "$@"; do
      case "$target" in
        ''|/*|.|./*|..|../*|*/../*|*/..|*/./*|*/.|*//*|*/|:*|*$'\r'*|*$'\t'*)
          printf 'paths mode requires normalized repository-relative file paths: %s\n' "$target" >&2
          exit 2
          ;;
      esac
      [[ ! -d "$repo_root/$target" ]] || {
        printf 'paths mode requires files, not directories: %s\n' "$target" >&2
        exit 2
      }
      if git -C "$repo_root" check-ignore -q -- "$target"; then
        printf 'paths mode refuses Git-ignored paths: %s\n' "$target" >&2
        exit 2
      fi
      in_scope=false
      for allowed_path in "${allowed_paths[@]}"; do
        if [[ "$target" == "$allowed_path" ]]; then
          in_scope=true
          break
        fi
      done
      $in_scope || {
        printf 'paths mode refuses paths outside the scope file: %s\n' "$target" >&2
        exit 2
      }
      [[ -n "$(git -C "$repo_root" status --porcelain=v1 --untracked-files=all -- ":(literal)$target")" ]] || {
        printf 'paths mode requires currently changed paths: %s\n' "$target" >&2
        exit 2
      }
      prompt+=$'\n- '
      prompt+="$target"
    done
    shift "$#"
    args+=("$prompt")
    ;;
  *)
    printf 'unsupported review mode: %s\n' "$mode" >&2
    exit 2
    ;;
esac

(( $# == 0 )) || {
  printf 'review modes do not accept additional arguments\n' >&2
  exit 2
}

if $dry_run; then
  cd "$repo_root"
  printf '%q ' "${args[@]}"
  printf '\n'
  exit 0
fi

# A caller may enable xtrace; never let a one-time proof enter trace output.
case "$-" in
  *x*) set +x ;;
esac
origin_ref=""
if command -v codex-notify >/dev/null 2>&1; then
  if claimed_origin_ref=$(codex-notify internal-review claim --launcher "$0" -- "${original_review_argv[@]}" 2>/dev/null); then
    case "$claimed_origin_ref" in
      *[!0-9a-f]*|'')
        ;;
      *)
        if (( ${#claimed_origin_ref} == 64 )); then
          origin_ref=$claimed_origin_ref
        fi
        ;;
    esac
  fi
fi
unset claimed_origin_ref

if [[ -n "$origin_ref" ]]; then
  args=(
    codex exec review
    -c "shell_environment_policy.set.CODEX_NOTIFY_ORIGIN_REF=\"$origin_ref\""
    "${args[@]:3}"
  )
fi
unset origin_ref

cd "$repo_root"

command -v codex >/dev/null 2>&1 || {
  printf 'codex CLI is not installed or not on PATH\n' >&2
  exit 127
}

if [[ -n "$output_path" ]]; then
  if [[ -d "$output_path" ]]; then
    printf 'output path must be a file, not a directory: %s\n' "$output_path" >&2
    exit 2
  fi
  rm -f -- "$output_path"
fi

if ! $quiet_events && [[ -z "$output_path" ]]; then
  exec "${args[@]}"
fi

if ! $quiet_events; then
  status=0
  "${args[@]}" || status=$?
  if (( status == 0 )) && [[ ! -s "$output_path" ]]; then
    printf 'review completed without a fresh nonempty result: %s\n' "$output_path" >&2
    exit 1
  fi
  exit "$status"
fi

events_path="${output_path}.events.jsonl"
stderr_path="${output_path}.stderr.log"
metrics_path="${output_path}.metrics"
started_at_epoch=$(date +%s)
status=0
"${args[@]}" >"$events_path" 2>"$stderr_path" || status=$?
if (( status == 0 )) && [[ ! -s "$output_path" ]]; then
  status=1
fi
finished_at_epoch=$(date +%s)
events_bytes=$(wc -c <"$events_path" | tr -d ' ')
events_lines=$(wc -l <"$events_path" | tr -d ' ')
stderr_bytes=$(wc -c <"$stderr_path" | tr -d ' ')
stderr_lines=$(wc -l <"$stderr_path" | tr -d ' ')
result_bytes=0
if [[ -f "$output_path" ]]; then
  result_bytes=$(wc -c <"$output_path" | tr -d ' ')
fi
{
  printf 'mode=%s\n' "$mode"
  printf 'started_at_epoch=%s\n' "$started_at_epoch"
  printf 'finished_at_epoch=%s\n' "$finished_at_epoch"
  printf 'duration_seconds=%s\n' "$((finished_at_epoch - started_at_epoch))"
  printf 'events_lines=%s\n' "$events_lines"
  printf 'events_bytes=%s\n' "$events_bytes"
  printf 'stderr_lines=%s\n' "$stderr_lines"
  printf 'stderr_bytes=%s\n' "$stderr_bytes"
  printf 'result_bytes=%s\n' "$result_bytes"
  printf 'exit_status=%s\n' "$status"
} >"$metrics_path"
if (( status == 0 )); then
  printf 'review completed; result=%s events=%s stderr=%s metrics=%s\n' "$output_path" "$events_path" "$stderr_path" "$metrics_path"
  exit 0
fi

if [[ ! -s "$output_path" ]]; then
  printf 'review failed with status %d and no fresh nonempty result; events=%s stderr=%s metrics=%s\n' "$status" "$events_path" "$stderr_path" "$metrics_path" >&2
else
  printf 'review failed with status %d; events=%s stderr=%s metrics=%s\n' "$status" "$events_path" "$stderr_path" "$metrics_path" >&2
fi
exit "$status"
