# Bounded Review-Fix Contracts

## requirements.json

```json
{
  "original_request": "用户原始需求原文",
  "accepted_amendments": [],
  "required_outcomes": [],
  "allowed_scope": [],
  "non_goals": [],
  "acceptance_criteria": [],
  "authorization_boundary": [],
  "authorized_scope_extensions": [],
  "unresolved_decisions": []
}
```

只有用户明确选择退出验证模式时，才可额外包含：

```json
{
  "test_mode": "deep",
  "test_mode_reason": "user:deep"
}
```

`test_mode` 只能是 `none`、`light`、`deep`，reason 必须精确等于 `user:<mode>`，两个字段必须同时存在。用户未指定时同时省略，由 `code-review-fix-loop` 使用 `light/default`。原始用户文字与明确接受的补充是模式授权的唯一来源。

## drift-check.json

初始 review 和每个修复窗口前都提交：

```json
{
  "result": "aligned",
  "requirement_digest": "sha256",
  "original_request": "用户原始需求原文",
  "accepted_amendments": [],
  "current_diff_files": ["src/example.ts"],
  "planned_actions": ["repair the current actionable findings"],
  "alignment_summary": "当前动作仍符合原始目标",
  "untracked_review_screening": [
    {
      "path": "src/new-file.ts",
      "classification": "safe_to_review",
      "reason": "普通源码文件名，不命中凭据路径规则"
    }
  ]
}
```

仅 `aligned`、`aligned_with_amendment` 可继续。原始要求、补充、digest、当前路径、untracked 筛查或批准范围不一致时熔断。

## 初始 reviewer 结果

Supervisor 只直接执行一次成功的初始完整 review；首次不完整可以重试一次。主协议：

```json
{
  "coverage_complete": true,
  "findings": [
    {
      "fingerprint": "src/example.ts|parseInput|missing empty guard|empty input",
      "status": "actionable",
      "severity": "P1",
      "location": "src/example.ts:42",
      "summary": "空输入进入解析器时会抛出异常"
    }
  ]
}
```

JSON fingerprint 使用稳定的 `path|symbol|root-cause|trigger`。`status` 使用 `actionable`、`not_actionable`、`not_introduced`、`out_of_scope`、`low_signal`、`needs_decision`、`needs_clarification`、`blocked`。`not_actionable` 用于保留 finding 身份并记录经代码事实核对后不成立；需要决策、澄清或阻塞时不得进入自动修复。

## skill-result.json

每个 `code-review-fix-loop` 窗口必须返回结构化结果。达到自身第五个修复批次但仍有问题时：

```json
{
  "status": "incomplete",
  "exit_reason": "max_fix_rounds_reached",
  "requirements_snapshot": {
    "original_request": "用户原始需求原文",
    "accepted_amendments": [],
    "required_outcomes": [],
    "allowed_scope": [],
    "non_goals": [],
    "acceptance_criteria": [],
    "authorization_boundary": [],
    "authorized_scope_extensions": [],
    "unresolved_decisions": []
  },
  "requirement_digest": "<lowercase-sha256>",
  "alignment_directive": null,
  "alignment_directive_status": "not_applicable",
  "alignment_resolution_evidence": null,
  "fix_rounds": 5,
  "unresolved_count": 1,
  "coverage_status": "covered",
  "review_checkpoint": {
    "source": "code-review-fix-loop",
    "snapshot_contract": "git-cumulative-diff-sha256-v1",
    "review_complete": true,
    "coverage_status": "covered",
    "review_baseline": "<frozen-baseline-tree>",
    "reviewed_diff_hash": "<lowercase-sha256>",
    "changed_paths": ["src/example.ts"],
    "findings": [
      {
        "fingerprint": "stable-finding-id",
        "status": "actionable",
        "severity": "P1",
        "location": "src/example.ts:42",
        "summary": "仍需继续修复的问题"
      }
    ]
  },
  "validation_status": "focused",
  "test_mode": "light",
  "test_mode_reason": "default",
  "exit_test_count": 1,
  "exit_test_status": "passed",
  "exit_test_skip_reason": null
}
```

收敛时使用 `status=complete`、`exit_reason=converged`、`unresolved_count=0`，且检查点 findings 不得包含未终结项。没有收到纠偏指令时必须返回 `alignment_directive=null`、`alignment_directive_status=not_applicable`、`alignment_resolution_evidence=null`。收到指令时必须逐字回显为 `alignment_directive`，把状态设为 `resolved` 或 `unresolved`，并给出非空 `alignment_resolution_evidence`；只有 `resolved` 才能与 `status=complete` 配对，不能用 clean review 丢弃未解决的纠偏约束。阻塞时使用 `status=blocked` 和非空退出原因；无法形成完整检查点时 `review_checkpoint` 可以是 `null`，Supervisor 必须停止。

只有以下组合允许进入跨窗口 alignment checkpoint：

- `status=incomplete`；
- `exit_reason=max_fix_rounds_reached`；
- `fix_rounds=5`；
- 检查点完整且 `coverage_status=covered`；
- 未终结项全部为 `actionable`；
- 当前窗口验证不是 `failed` 或 `blocked`；
- 当前窗口数尚未达到 `max_fix_windows`。

这些条件成立后，状态只能进入 `alignment_required`，不能直接开始下一窗口。

`requirements_snapshot` 必须逐字等于 Supervisor 冻结的完整需求 object，`requirement_digest` 必须是该 object 的规范摘要；用户要求或已接受补充变化后，旧结果即使 Git hash 相同也不得接管。独立结果被后续接管时，调用方直接从这里提取 `requirements.json`，不得重新概括。`unresolved_count` 必须等于检查点中 `actionable`、`needs_decision`、`needs_clarification`、`blocked` 的总数。检查点的 snapshot contract、baseline、diff hash 和 changed paths 必须与 Supervisor 使用同版算法重新采集的当前快照完全相同。子 Skill 使用自身的 `workspace-snapshot.py` 生成可交接快照，不依赖 sibling Skill；父级还必须核对纠偏指令、状态和证据，不得把缺失字段补写成成功，也不得在相同 diff 上追加外层 review 来替代无效检查点。

没有持久化修改时，子 Skill 可以复用父级提供的完整检查点并重分类 finding，但必须原样保留全部 fingerprint、severity 和 location。允许的终态包括 `not_actionable`、`not_introduced`、`out_of_scope`、`low_signal`；不得删除原 finding 或返回伪造的空检查点。状态助手会对 fingerprint 集合与身份字段做精确核对。

## 接管已有 skill-result.json

若本 campaign 是在一次 `code-review-fix-loop` 已结束后启动，初始化状态后可执行 `adopt-fix-result`。被接管结果适用与 `record-fix` 相同的 schema、模式、coverage、baseline、changed paths、diff hash、finding 和验证证据核验。成功接管后：

- 不执行初始 reviewer，`initial_review_count=0`；
- 先验证并采用子检查点的 `review_baseline` tree，再以该 baseline 重算 committed、staged、unstaged 和 untracked 完整路径；因此 `base`/`commit` 结果不会被错误绑定到初始化时的 HEAD tree；
- 该结果计为 `fix_window_count=1`、`review_checkpoint_count=1`，并累计其 `fix_rounds`；
- `complete + converged` 直接完成；
- `incomplete + max_fix_rounds_reached + fix_rounds=5` 进入 `alignment_required`；
- 缺失、畸形、过期或不匹配的检查点持久化为 `invalid_child_review_checkpoint` 或对应绑定错误并 fail closed，不得退回同状态初审。

## alignment-result.json

每个额外修复窗口开始前，`review-fix-alignment-supervisor` 必须复用最后的子 Review 检查点，对当前完整 diff 做一次需求与设计纠偏，并返回：

```json
{
  "source": "review-fix-alignment-supervisor",
  "outcome": "redirect",
  "snapshot_contract": "git-cumulative-diff-sha256-v1",
  "review_baseline": "<tree-sha>",
  "assessed_head": "<commit-sha>",
  "assessed_diff_hash": "<lowercase-sha256>",
  "changed_paths": ["src/example.ts", "src/legacy.ts"],
  "requirement_digest": "<lowercase-sha256>",
  "review_checkpoint_digest": "<lowercase-sha256>",
  "requirement_alignment": "aligned",
  "implementation_completeness": "fixable_gap",
  "design_convergence": "needs_redirect",
  "minimality": "excess_or_redundant",
  "alignment_findings": [
    {
      "kind": "redundancy",
      "summary": "存在两套等价状态转换",
      "evidence": "用户只要求一个入口；src/example.ts 与 src/legacy.ts 并行实现",
      "related_paths": ["src/example.ts", "src/legacy.ts"]
    }
  ],
  "correction_directive": "保留现有公开入口，移除重复状态转换，并据此修复剩余 finding。",
  "developer_question": null,
  "blocker": null
}
```

枚举与组合规则：

- `outcome`：`continue`、`redirect`、`ask_developer`、`blocked`；
- `requirement_alignment`：`aligned`、`misaligned`、`uncertain`；
- `implementation_completeness`：`complete`、`fixable_gap`、`decision_gap`、`blocked`；
- `design_convergence`：`convergent`、`needs_redirect`、`decision_required`、`blocked`；
- `minimality`：`minimum_sufficient`、`excess_or_redundant`、`uncertain`；
- finding kind：`requirement_gap`、`requirement_drift`、`redundancy`、`design_divergence`、`missing_information`、`authorization_boundary`。

`continue` 必须是 aligned、convergent、minimum_sufficient、无 alignment finding，且三个控制字段均为 null。`redirect` 不得包含 uncertain/decision/blocked 状态，必须有 finding 和非空 `correction_directive`。`ask_developer` 必须有 decision/uncertain 信号、finding 和唯一非空 `developer_question`。`blocked` 必须把完整性或设计标为 blocked，并有非空 `blocker`。结果的 snapshot contract、baseline、HEAD、hash、完整 changed paths 和 `requirement_digest` 必须逐字复制调用方证据，并与状态助手重新采集的当前完整 diff 和冻结需求精确相等；`review_checkpoint_digest` 必须匹配当前 `latest_review_result` 完整 object 的规范 SHA-256 摘要，从而把结论绑定到确切 finding 台账而不只是 diff hash。

`continue` 或 `redirect` 才把状态推进到 `ready_for_fix`；`redirect` 指令由下一次 `begin-fix-window` 返回并逐字传给子 Skill。`ask_developer` 和 `blocked` 都立即停止 campaign。Alignment 不运行测试、不修改 diff，也不重新执行 bug review。

## 测试模式证据

用户明确模式配对存在时，子结果的 `test_mode` 和 reason 必须与其完全一致；不存在时只能返回 `light/default`。`exit_test_count` 只能是 `0` 或 `1`：

- `0` 只能对应 `skipped + unverified`，且必须携带标准 `exit_test_skip_reason`；
- `1 + passed`：light 对应 `focused`，deep 对应 `complete`，`exit_test_skip_reason=null`；
- `1 + failed` 对应 `validation_status=failed`，`exit_test_skip_reason=null`；
- `1 + blocked` 对应 `validation_status=blocked`，`exit_test_skip_reason=null`；
- none 必须是 `0 + skipped + unverified + user_disabled`。

标准跳过原因只有：`user_disabled`、`no_persistent_change`、`no_applicable_automated_test`、`blocked_before_exit_validation`。非 none 模式下，零修复轮次只能使用 `no_persistent_change`；有持久化修复但确实没有适用自动化测试时只能使用 `no_applicable_automated_test`，并在台账中保留判断依据；子流程在退出验证前已经阻塞时使用 `blocked_before_exit_validation`。`not_run` 只是运行前的内部初值，不是合法的最终结构化结果。

这些字段记录每个子窗口自己的退出验证。Supervisor 不运行或伪造额外测试。

## 状态 schema

状态 schema 为 v8。v7 及更早状态不含强制 alignment checkpoint，不能安全迁移，必须重新初始化。

核心计数：

- `initial_review_count`：成功的外层初始完整 review，只能是 `0` 或 `1`；接管已有子结果时保持 `0`；
- `review_attempt_count`：包括首次 incomplete 重试；
- `fix_window_count`：已接纳或接管的 `code-review-fix-loop` 窗口，也就是已复用的子 Review 检查点数；
- `alignment_check_count`：已接纳的跨窗口纠偏检查点数；最大为 `max_fix_windows - 1`；
- `max_fix_windows`：`1`–`4`，默认 `4`；
- `total_fix_rounds`：所有窗口实际修复批次总数；
- `review_checkpoint_count`：初始 review 与已接纳子检查点总数。

## 范围与快照

统一快照固定 HEAD、staged、unstaged、untracked 路径及 digest。`extend-scope --reason direct_dependency` 只允许在 `ready_for_fix`、子窗口开始前登记尚未修改的 tracked 直接依赖；`user_authorized` 只接受冻结在 requirements 中的路径。每个窗口开始与结果接纳时均重新核对完整 changed paths 和安全筛查。
