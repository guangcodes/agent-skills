# Reusable Review-Fix Handoff Contract

本文件定义 `supervisor_handoff=available|required` 共用的自包含协议。默认 `available` 也必须严格输出这一 schema，不依赖调用方或 sibling Skill 补充字段。

## 需求快照与摘要

进入循环时从用户原文和明确接受的补充建立：

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

只有用户明确指定退出验证模式时，才增加配对的 `test_mode` 与 `test_mode_reason=user:<mode>`；默认 light 不写入这两个字段。对该 object 使用 UTF-8、`ensure_ascii=true`、key 排序且无多余空白的 JSON 编码，再计算 lowercase SHA-256，得到 `requirement_digest`。后续用户要求变化时旧结果不得复用，必须产生新的需求快照。

## skill-result.json

```json
{
  "status": "complete",
  "exit_reason": "converged",
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
  "fix_rounds": 1,
  "unresolved_count": 0,
  "coverage_status": "covered",
  "review_checkpoint": {
    "source": "code-review-fix-loop",
    "snapshot_contract": "git-cumulative-diff-sha256-v1",
    "review_complete": true,
    "coverage_status": "covered",
    "review_baseline": "<tree-sha>",
    "reviewed_diff_hash": "<lowercase-sha256>",
    "changed_paths": ["src/example.ts"],
    "findings": []
  },
  "validation_status": "focused",
  "test_mode": "light",
  "test_mode_reason": "default",
  "exit_test_count": 1,
  "exit_test_status": "passed",
  "exit_test_skip_reason": null
}
```

字段规则：

- `status` 只能是 `complete`、`incomplete`、`blocked`；`exit_reason` 必须非空。
- `requirements_snapshot` 必须逐字携带本次循环冻结并用于 review 的完整需求 object；`requirement_digest` 必须由该 object 按上一节规则重新计算得到。后续启动 Supervisor 时直接把这个 object 写为 `requirements.json`，不得从摘要或自然语言重新合成。
- 未收到上层纠偏指令时，`alignment_directive` 和 `alignment_resolution_evidence` 必须为 `null`，`alignment_directive_status` 必须为 `not_applicable`。收到指令时必须逐字回显，状态只能是 `resolved` 或 `unresolved`，并给出非空解决证据；`status=complete` 只允许与 `resolved` 配对。
- `fix_rounds` 为 `0`–`5`；`unresolved_count` 等于 checkpoint 内 `actionable`、`needs_decision`、`needs_clarification`、`blocked` 的数量。
- `coverage_status` 使用 `covered`、`partial`、`uncovered`。可复用 checkpoint 必须是 `covered`；无法形成时使用 `review_checkpoint=null`，上层会 fail closed。
- `complete` 只和 `converged + unresolved_count=0` 配对；可续接的轮次耗尽只和 `incomplete + max_fix_rounds_reached + fix_rounds=5` 配对。
- snapshot 必须由本 Skill 的 `workspace-snapshot.py --baseline <review_baseline>` 在最后 review 前后稳定采集。
- 退出验证字段遵守主 Skill 的一次性阶段规则；执行时 skip reason 为 null，跳过时使用标准原因。

## Finding 状态映射

`review_checkpoint.findings` 使用英文机器状态；中文台账到协议的唯一映射为：

| 中文台账状态 | 机器状态 |
| --- | --- |
| `待修复` | `actionable` |
| `不成立` | `not_actionable` |
| `非本次引入` | `not_introduced` |
| `范围外` | `out_of_scope` |
| `低信号不处理` | `low_signal` |
| `需要决策` | `needs_decision` |
| `需要澄清` | `needs_clarification` |
| `阻塞` | `blocked` |

`已修复`、`已修复-未验证` 保留在人类台账，不作为当前 checkpoint finding 输出。每个机器 finding 必须包含稳定 `fingerprint`、`severity=P0|P1|P2|P3`、非空 `location` 和 `summary`。零修改重分类时例外：必须保留输入 checkpoint 的完整 fingerprint 集合、severity/location，并使用上表中的适当终态，不能清空 findings。
