---
name: review-fix-alignment-supervisor
description: Use when a local code-review-fix-loop has stopped converging, exhausted its repair rounds, repeatedly reopens the same root cause, or reaches a decision or information gap; inspect the current full local diff against the conversation and latest review checkpoint, then return continue, redirect, ask_developer, or blocked. Do not use for normally converging repair rounds, initial implementation, routine code review, test execution, delivery-readiness certification, or extra repair capacity.
---

# Review-Fix Alignment Supervisor

## 定位

这是 `code-review-fix-loop` 失去收敛时的只读纠偏器。它回答三个问题：

1. 当前完整 code diff 是否仍在实现用户真正要求的结果；
2. 当前方案是否存在功能缺口、设计发散或不必要的冗余；
3. 下一步应继续原方向、按明确指令纠偏，还是必须停下来请开发人员决策。

本 Skill 不执行修复、不增加修复轮次、不重新做一遍 bug review、不运行任何自动化测试，也不声明 `READY_LOCAL_DIFF`。finding 的真实性和剩余 bug 以最近一次有效的 `code-review-fix-loop` 完整累计 review 检查点为准；本 Skill 只补充需求对齐、方案完整性和最小充分性判断。

仓库开发流程、用户或其他流程要求的 TDD、preflight 和验证继续由提出要求的一方管理；这些工作不属于本 Skill，也不因本 Skill 介入而被取消或重复执行。

## 何时介入

仅在出现至少一个非收敛或决策信号时介入：

- `code-review-fix-loop` 达到修复轮次上限后仍有 actionable finding；
- 同一根因或等价症状在修复后重复出现；
- 多轮修改没有减少问题，或 diff 持续扩大却没有对应的新需求、finding 或必要依赖；
- 当前方案疑似偏离用户要求、遗漏必要用户路径，或引入了并行实现、重复抽象、过量配置、无关重构；
- 修复依赖产品语义、验收口径、权限、外部事实或授权边界的决定；
- 调用方明确要求进行一次 alignment checkpoint。

正常收敛中的每一轮不要调用本 Skill。单纯需要突破五轮上限时使用 `bounded-review-fix-supervisor`；本 Skill 本身不提供额外修复容量。

## 输入与事实优先级

介入时读取并相互核对：

- 当前对话中的原始用户需求、后续明确修订、已接受决策和非目标；
- 仓库 `AGENTS.md`、强制规范、公开契约和与改动直接相关的现有设计；
- 当前全部 staged、unstaged、untracked code diff，以及理解这些改动所必需的直接代码上下文；
- 最近一次 `code-review-fix-loop` 的累计 review 检查点、finding 台账、退出原因和实际验证证据；
- 调用方提供的当前 snapshot contract、diff hash、changed paths、baseline、HEAD、`requirement_digest`，以及最近一次有效累计 review 检查点的 `review_checkpoint_digest`。

冲突时按以下顺序裁决：

1. 用户最新的明确要求或明确授权；
2. 用户此前仍有效的要求与已接受决策；
3. 仓库强制规则、外部兼容契约和安全边界；
4. 当前代码中与目标直接相关且仍有效的设计约定；
5. Agent 推断。

结合完整对话理解需求，不把一次 Agent 摘要当成用户原文。范围和审查面根据当前 diff 及直接依赖实时重算，不跨 checkpoint 冻结；但用户的写入授权、非目标和高风险操作边界不会因此自动扩大。

## 单次纠偏流程

### 1. 绑定当前快照

在读取任何 diff 内容前先完成 review surface 安全筛查。若调用方已提供同一 HEAD、diff hash 和 changed paths 上成功的 `bounded-review-fix-supervisor hash-diff` 结果，直接复用该安全证据；否则在项目根目录执行本 Skill 自带的筛查器：

```bash
python3 <skill-dir>/scripts/screen-review-surface.py \
  --project-root <project-root> \
  --baseline <latest-review-baseline-tree>
```

`--baseline` 必须逐字取自最近一次有效 `code-review-fix-loop` 累计 review 检查点的 `review_baseline`，不能默认成当前 HEAD。筛查器只读取 Git 路径元数据、diff 字节和文件类型，不读取 untracked 文件内容；输出 `snapshot_contract=git-cumulative-diff-sha256-v1`、解析后的 baseline tree、当前 HEAD、committed/staged/unstaged/untracked 路径、完整 changed paths 和 `diff_hash`，并显式覆盖本地 submodule ignore 配置。任一 changed path 命中 `.env*`、认证配置、credential/token/cookie/private-key 语义、私钥扩展名或这些私钥名称的备份变体，untracked 不是普通文件，或尚存在于工作树中的 changed tracked path 不是普通文件（例如 symlink）时，立即返回 `blocked`，在报告中写明敏感路径和解除条件；不得继续读取 diff、凭据内容或用人工判断绕过。筛查后 baseline、HEAD、changed paths 或 hash 变化时必须重新筛查。

确认累计 review 检查点覆盖当前完整 diff，且其 baseline、diff hash 与 changed paths 仍和工作区一致。调用方提供了统一快照助手时复用它，不自行创造另一套 hash。

若评估期间 code diff、HEAD 或 changed paths 变化，丢弃本次分析并对新快照重做；不得把旧结论套到新 diff。检查点缺失或过期且无法安全重建时返回 `blocked`，不要把未知状态解释为无问题。

### 2. 重建实时需求

从整体对话提取当前仍有效的：期望结果、用户可见行为、验收标准、非目标、兼容要求、写入授权和未决问题。明确标出后续要求覆盖了哪些早期要求。

逐项建立 `需求 -> 当前实现证据 -> 缺口` 映射。需求范围可以随对话更新，审查面可以随 diff 调整，但不得用代码已经写成什么反向改写用户需求。

### 3. 检查完整 diff 的方向与充分性

对当前全部 code diff 做静态对齐检查，而不是只看最新一轮修改：

- 每项有效需求是否都有必要的实现路径；
- 每个 diff 文件和关键 hunk 是否能追溯到需求、有效 finding 或不可缺少的直接依赖；
- 剩余 finding 是局部实现错误，还是暴露了错误假设、错误状态模型、错误职责边界或根本不可行的方案；
- 是否存在两套事实源、并行流程、重复分支、投机性抽象、过量配置、无关清理或为绕过症状而叠加的补丁；
- 是否有更小但仍完整、兼容、可维护、能处理失败路径的实现。

“最小改动”指 **最小充分改动**：完成全部有效需求并收敛已知问题所必需的最小方案，不等于最少文件或最少行数。

### 4. 选择唯一结果

- `continue`：方向正确；剩余问题可在现有设计内直接修复；没有需要先移除的明显发散或待决语义。
- `redirect`：存在明确的需求偏离、可修复缺口、冗余或设计发散，而且技术纠偏方向唯一、可逆、未超出用户授权。给出下一轮应保留、删除、替换和验证的具体指令。
- `ask_developer`：正确性取决于开发人员选择或补充事实。立即停止后续写入、review 和测试，只问一个能解除阻塞的聚焦问题。
- `blocked`：检查点过期、权限不足、外部状态不可用或其他非决策条件使判断不可靠。停止并给出解除条件。

产品语义、验收口径、公共 API/Schema、数据生命周期、认证、安全、计费、写入授权、不可获得事实，或多个同样合理但用户可见结果不同的方案，需要 `ask_developer`。普通的局部实现方式、命名、可逆重构和已有契约能唯一决定的工程问题使用 `redirect`，不要把常规工程判断上抛。

## 输出契约

返回一份人类可读结论，并在调用方需要结构化交接时同时返回：

```json
{
  "source": "review-fix-alignment-supervisor",
  "outcome": "continue",
  "snapshot_contract": "git-cumulative-diff-sha256-v1",
  "review_baseline": "<tree-sha>",
  "assessed_head": "<commit-sha>",
  "assessed_diff_hash": "<lowercase-sha256>",
  "changed_paths": ["src/example.ts"],
  "requirement_digest": "<lowercase-sha256>",
  "review_checkpoint_digest": "<lowercase-sha256>",
  "requirement_alignment": "aligned",
  "implementation_completeness": "fixable_gap",
  "design_convergence": "convergent",
  "minimality": "minimum_sufficient",
  "alignment_findings": [],
  "correction_directive": null,
  "developer_question": null,
  "blocker": null
}
```

字段枚举和组合规则见 [references/result-contract.md](references/result-contract.md)。快照字段必须逐字复制本次安全筛查结果；`requirement_digest` 必须逐字复制最近一次有效 `code-review-fix-loop` 检查点或上层 Supervisor 提供的当前需求摘要。`review_checkpoint_digest` 必须是该最近检查点完整 object 的规范 SHA-256 摘要，使用 UTF-8、`ensure_ascii=true`、键排序和紧凑分隔符；它把纠偏结论绑定到确切 finding 台账，不能只凭复现的 diff hash 复用旧结论。证据必须绑定本次完整 diff，指出关联需求、finding 和具体路径，不得用泛化措辞替代可核验依据。

`redirect` 的 `correction_directive` 必须足够具体，能直接成为下一次 `code-review-fix-loop` 的附加输入，但它不能扩大用户需求或写入授权。`ask_developer` 只提出一个决策问题，并说明为什么当前证据无法自行裁决；在收到回答前不得继续 Agent 工作。
