# Alignment Result Contract

调用方要求结构化结果时，输出一个 JSON object：

```json
{
  "source": "review-fix-alignment-supervisor",
  "outcome": "continue | redirect | ask_developer | blocked",
  "snapshot_contract": "git-cumulative-diff-sha256-v1",
  "review_baseline": "<tree-sha>",
  "assessed_head": "<commit-sha>",
  "assessed_diff_hash": "<lowercase-sha256>",
  "changed_paths": ["src/example.ts"],
  "requirement_digest": "<lowercase-sha256>",
  "review_checkpoint_digest": "<lowercase-sha256>",
  "requirement_alignment": "aligned | misaligned | uncertain",
  "implementation_completeness": "complete | fixable_gap | decision_gap | blocked",
  "design_convergence": "convergent | needs_redirect | decision_required | blocked",
  "minimality": "minimum_sufficient | excess_or_redundant | uncertain",
  "alignment_findings": [
    {
      "kind": "requirement_gap | requirement_drift | redundancy | design_divergence | missing_information | authorization_boundary",
      "summary": "离散结论",
      "evidence": "关联需求、finding 与代码事实",
      "related_paths": ["src/example.ts"]
    }
  ],
  "correction_directive": null,
  "developer_question": null,
  "blocker": null
}
```

## 通用约束

- `source` 必须精确为 `review-fix-alignment-supervisor`。
- 在读取 diff 内容前，必须已有绑定同一快照的安全筛查证据；独立调用使用本 Skill 的 `screen-review-surface.py --baseline <latest-review-baseline-tree>`，敏感路径、特殊 untracked 文件或尚存在于工作树中的 changed tracked 非普通文件令结果只能为 `blocked`。
- `snapshot_contract`、`review_baseline`、`assessed_head`、`assessed_diff_hash` 和 `changed_paths` 必须逐字复制安全筛查结果对应的 `snapshot_contract`、`baseline_tree`、`head`、`diff_hash` 和 `changed_paths`。它们共同覆盖 baseline 到 HEAD 的 committed diff、staged、unstaged 与 untracked；不得默认 baseline 为当前 HEAD 或由调用方补造旧证据。
- `requirement_digest` 必须逐字复制最近一次有效 `code-review-fix-loop` 检查点或上层 Supervisor 交付的当前需求摘要；需求变化后不得复用旧结果。
- `review_checkpoint_digest` 必须等于最近一次有效完整 review checkpoint object 的规范 SHA-256 摘要：UTF-8 编码、`ensure_ascii=true`、键排序、紧凑分隔符。即使工作区 diff hash 再次相同，只要 finding 台账或其他检查点证据变化，旧 alignment 结果也不得复用。
- `alignment_findings` 中的路径必须是规范的仓库相对路径；每项必须有非空 `summary` 和 `evidence`。
- `correction_directive`、`developer_question`、`blocker` 只能是 `null` 或非空字符串。
- 一次结果只能选择一个 `outcome`，不得把“继续”和“等待开发人员”混合表达。

## 结果组合

| outcome | 必须满足 | 禁止 |
| --- | --- | --- |
| `continue` | `requirement_alignment=aligned`；`design_convergence=convergent`；`minimality=minimum_sufficient`；完整性为 `complete` 或 `fixable_gap`；`alignment_findings=[]` | 三个控制字段都必须为 `null` |
| `redirect` | 不含 uncertain/decision/blocked 状态；`implementation_completeness=fixable_gap`；非空 `alignment_findings`；非空 `correction_directive` | `developer_question`、`blocker` 必须为 `null` |
| `ask_developer` | `implementation_completeness=decision_gap` 或存在无法自行裁决的用户可见语义；非空 `alignment_findings`；非空 `developer_question` | `correction_directive`、`blocker` 必须为 `null` |
| `blocked` | 完整性或设计状态为 `blocked`；非空 `blocker` | `correction_directive`、`developer_question` 必须为 `null` |

`continue` 不表示没有剩余 bug；它只表示现有方向可以继续收敛。`redirect` 不授权新的产品行为或路径。`ask_developer` 要求调用方在收到回答前停止所有后续写入、review 和测试。
