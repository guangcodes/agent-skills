# Supervisor JSON Contracts

执行 Supervisor 前完整读取本文件。所有路径均指向本 Goal 在系统临时目录、且在项目根目录外创建的 UTF-8 JSON 文件。

## requirements.json

```json
{
  "original_request": "用户原始需求原文",
  "accepted_amendments": [],
  "test_mode": "deep",
  "test_mode_reason": "user:deep",
  "required_outcomes": [],
  "allowed_scope": [],
  "non_goals": [],
  "acceptance_criteria": [],
  "authorization_boundary": [],
  "authorized_scope_extensions": [],
  "unresolved_decisions": []
}
```

`test_mode` 是 `none`、`light` 或 `deep`，`test_mode_reason` 是非空字符串；其余除 `original_request` 外的业务字段均为字符串数组。原始需求不得由代理摘要替换。`authorized_scope_extensions` 只放用户已明确批准的规范化 repo-relative 路径；新授权属于需求 amendment，必须重建基线并重新初始化 Supervisor。

## drift-check.json

```json
{
  "result": "aligned",
  "requirement_digest": "从 state 读取",
  "original_request": "从 state 原样读取",
  "accepted_amendments": [],
  "current_diff_files": ["repo/relative/path"],
  "planned_actions": ["review complete diff"],
  "alignment_summary": "本轮工作与需求基线的对应关系",
  "untracked_review_screening": [
    {
      "path": "repo/relative/untracked.txt",
      "classification": "safe_to_review",
      "reason": "不含凭据或敏感数据"
    }
  ]
}
```

`result` 使用 `aligned`、`aligned_with_amendment`、`drift_suspected`、`drift_confirmed` 或 `baseline_unavailable`。当前版本中 amendment 必须先由用户确认并重新初始化 Supervisor；不得靠修改 digest 静默更新需求。筛查清单必须与统一路径快照中的全部 untracked 路径完全一致。路径、安全分类、文件类型与哈希规则以 [invariants.md](invariants.md) 为唯一语义定义；任何 untracked 非普通文件都会硬熔断。不得通过全库文件系统遍历扩张到 Git 审查范围之外。

## 原生 reviewer 结果

```json
{
  "coverage_complete": true,
  "findings": [
    {
      "fingerprint": "path|symbol|root-cause|trigger",
      "status": "actionable",
      "severity": "P1",
      "location": "path:line",
      "summary": "触发场景和影响"
    }
  ]
}
```

`execute-review` 由 Supervisor 直接启动 `codex exec --sandbox read-only review --uncommitted --ephemeral --json --output-schema ...`，不再同时传入 positional prompt。调用方不提交结果文件；Supervisor 从实际进程退出码、最终输出和 review 前冻结的统一快照派生完整证据。JSON 输出按 schema 验证，且必须显式返回 `coverage_complete: true`；缺失或为 false 均按 incomplete fail closed。其 fingerprint 必须是 `path|symbol|root-cause|trigger` 四段式稳定身份。Markdown fallback 只识别明确的 reviewer/coverage 语法、标准 finding 或严格 clean 结论；普通产品文本不得触发 coverage 判定，`no new issues` 不得触发 clean。只有退出码为 `0`、输出可验证且 reviewer 结束后统一快照未变化时才增加 `full_review_round`。`status` 使用：`actionable`、`not_introduced`、`out_of_scope`、`low_signal`、`needs_decision`、`needs_clarification`、`blocked`；同一结果内 fingerprint 必须唯一。

Supervisor 当前状态 schema 为 v4；读取旧 v2/v3 状态时会迁移轮次尝试字段、把旧的更大 review 上限收紧到 `4`，并写入外层执行策略。状态中的 `max_full_review_rounds` 只能是 `1`–`4`。

状态必须包含：

```json
{
  "outer_execution_policy": {
    "scope": "bounded-review-supervisor-command-layer",
    "disallowed_actions": [
      "automated_test_execution",
      "whole_repository_scan_analysis"
    ],
    "propagate_to_child_processes_or_subskills": false
  }
}
```

这是外层命令策略记录，不传递给原生 reviewer、`code-review-fix-loop` 或其他子流程。

## skill-result.json

```json
{
  "status": "complete",
  "fix_rounds": 1,
  "unresolved_count": 0,
  "coverage_status": "partial",
  "validation_status": "focused",
  "requested_test_mode": "light",
  "effective_test_mode": "deep",
  "test_mode_reason": "risk:parser controls supervisor state transitions"
}
```

`coverage_status` 使用 `covered`、`partial`、`uncovered`；`validation_status` 使用 `unverified`、`focused`、`complete`、`blocked`。`requested_test_mode` 必须等于 requirements 中由 Supervisor 传入的模式；`effective_test_mode` 可以按子 Skill 风险规则升级但不得降级，`test_mode_reason` 记录最终理由。外层完整 review 负责恢复完整覆盖，不能把内层 focused coverage 伪装成 covered。

## 范围与 review 快照

`extend-scope --reason direct_dependency` 只能在已有 finding 的 `ready_for_fix` 阶段调用，并且只接受修改前已存在的 tracked 文件；不得在 `ready_for_review` 预先授权依赖。`--reason user_authorized` 可在 `ready_for_review` 或 `ready_for_fix` 调用，但只接受已冻结在 `authorized_scope_extensions` 的路径。`begin-review` 分别固定 staged、unstaged 与 untracked metadata 的快照及合并 changed paths；原生 review 期间不得修改工作区，`execute-review` 会在接受结果前重新核对二者。
