---
name: bounded-review-fix-supervisor
description: Use when the user explicitly invokes `$bounded-review-fix-supervisor` inside an active Goal to continue a high-assurance local review-fix campaign beyond one code-review-fix-loop's five repair rounds, with an alignment checkpoint before every additional repair window and a bounded global limit; never use for implicit, read-only, CI, deployment, or production requests.
---

# Bounded Review-Fix Supervisor

## 触发门禁

仅在以下条件同时成立时继续：

1. 用户明确调用 `$bounded-review-fix-supervisor`，而不是只提到名称。
2. 通过原生 Goal 状态确认当前存在 active Goal；不得根据提示词自行推断。

任一条件不成立时，在读取 diff、创建状态文件或启动 reviewer 前停止。门禁通过后先显示：“已显式触发 `$bounded-review-fix-supervisor`；当前 active Goal 已确认。”

## 定位

这是高保障的外层流程控制器。它优先接管同一工作区上已有的可复用 `code-review-fix-loop` 结果；没有可用结果时才执行一次独立的初始完整 diff review，然后把多个 **REQUIRED SUB-SKILL:** `code-review-fix-loop` 调用串成有界 campaign。每个子调用仍最多执行 `5` 个修复批次；只有子调用精确因为 `max_fix_rounds_reached` 退出、仍有可自动修复 finding、最后完整累计 review 检查点有效，并且 **REQUIRED SUB-SKILL:** `review-fix-alignment-supervisor` 完成一次只读纠偏检查后，Supervisor 才能开启下一修复窗口。

Supervisor 不在相同工作区状态上重复 bug review。子 Skill 已对当前完整累计 diff 完成只读 review 时，直接复用其检查点；缺失、覆盖不完整、基线不符、范围不符或 diff hash 过期都必须熔断，不能用一次新的 review 静默补救。Alignment Supervisor 也复用该检查点，只检查用户需求对齐、功能缺口、设计发散和最小充分性，不重新发现同一批 bug。这样增加的是可用修复轮次，并在扩容前纠偏，而不是增加同状态 bug review 次数。

## 初始化

1. 固定 Git 根目录、`baseline_head`、`baseline_tree` 和当前完整 staged、unstaged、untracked 范围；每个状态转换前确认 HEAD 未变化。
2. 从对话生成 `<requirements.json>`，保存用户原始要求、明确接受的补充、目标、范围、非目标、验收、授权边界和已批准路径。只有用户明确指定 `none`、`light` 或 `deep` 时才保存配对的 `test_mode`、`test_mode_reason=user:<mode>`；用户未指定时完全省略，由子 Skill 使用 `light/default`。不得默认、推断、建议、升级或降级模式。
3. 完整阅读 [references/contracts.md](references/contracts.md) 和 [references/invariants.md](references/invariants.md)，然后在系统临时目录、项目根目录外初始化：

```bash
python3 <skill-dir>/scripts/supervisor-state.py init \
  --state <temp-state.json> --project-root <project-root> \
  --baseline-head <sha> --requirements-file <requirements.json> \
  --max-windows <1-4>
```

`--max-windows` 可省略，默认值和硬上限均为 `4`。它统计 `code-review-fix-loop` 调用窗口，不统计初始 review；因此默认理论上最多容纳 `4 × 5 = 20` 个子 Skill 修复批次。始终使用状态助手计数，旧名称或 v7 及更早 schema 的临时状态不得续跑，必须重新初始化。

如果当前 diff 已有同一次 `code-review-fix-loop` 刚返回的结构化结果，初始化后应优先执行 `adopt-fix-result`。接管命令会验证并采用子结果的 `review_baseline` tree，再按该 baseline 重新计算 baseline-to-HEAD committed paths 和全部工作区路径，因此支持原子 Skill 的 `uncommitted`、`base` 和 `commit` 比较基线。只有结果缺失、baseline tree 无法解析、不是同一 HEAD/diff、检查点覆盖不完整或字段无法验证时才不能接管；无效结果会 fail closed，不能退回同状态初审来掩盖协议失败。没有现成结果时走正常初审。

## 外层命令边界

- Supervisor 自己不得运行 test、lint、typecheck、build、integration、E2E、smoke、benchmark、Docker、网络或远程验证。
- Supervisor 不得执行全仓扫描或无边界探索；只读取 Git 状态与 diff、明确路径、精确搜索、状态文件和子 Skill 已返回的证据。
- `execute-review` 启动的初始 reviewer 服从专用的只读静态审查指令，明确禁止测试及其他自动化验证；`code-review-fix-loop` 服从自身契约。
- `review-fix-alignment-supervisor` 是只读静态纠偏，不运行测试、修改代码或启动另一轮 bug review。
- 仓库开发流程、用户或其他流程要求的测试仍由要求方管理；不得混入子 Skill 的 `exit_test_count`。

## Campaign 流程

### 1. 接管已有结果，或执行初始完整 Review

若已有可复用的 `<skill-result.json>`，先执行：

```bash
python3 <skill-dir>/scripts/supervisor-state.py adopt-fix-result \
  --state <temp-state.json> --skill-result-file <skill-result.json>
```

接管成功的结果计为第一个修复窗口和第一个复用 Review 检查点，`initial_review_count` 保持 `0`。已收敛结果直接完成；`incomplete + max_fix_rounds_reached + fix_rounds=5` 直接进入 `alignment_required`，不在相同 diff 上补做初审。

没有现成结果时，生成 `<drift-check.json>` 并执行初始 review：

原样核对需求、补充、当前完整 diff、计划动作和全部 untracked 路径筛查，然后执行：

```bash
python3 <skill-dir>/scripts/supervisor-state.py begin-review \
  --state <temp-state.json> --drift-check-file <drift-check.json>
python3 <skill-dir>/scripts/supervisor-state.py execute-review \
  --state <temp-state.json>
```

初始 reviewer 必须在 CLI 强制的 read-only sandbox 中对全部 staged、unstaged、untracked diff 做纯静态审查，指令中明确禁止 test、lint、typecheck、build、integration、E2E、smoke、benchmark、Docker、网络和远程命令。结构化 JSON 必须显式声明 `coverage_complete=true`。工作区、HEAD、范围或安全筛查变化时拒绝结果；连续两次 reviewer 不完整时熔断。没有 actionable finding 时直接完成；否则进入第一个修复窗口。

### 2. 开始一个修复窗口

每个窗口前重新执行需求漂移、范围和快照核对：

```bash
python3 <skill-dir>/scripts/supervisor-state.py begin-fix-window \
  --state <temp-state.json> --drift-check-file <drift-check.json>
```

然后把当前 findings、用户原始要求、明确接受的补充，以及状态中完整的 `requirements` object 和 `requirement_digest` 交给 `code-review-fix-loop`。若上一窗口产生 `redirect`，还必须逐字传入状态中的 `alignment_directive`，作为下一窗口的纠偏约束；它不能扩大用户需求或写入授权。子 Skill 必须在结果中逐字回显该指令，以 `alignment_directive_status=resolved|unresolved` 声明处理状态，并给出非空 `alignment_resolution_evidence`；没有指令时只能返回 `null + not_applicable + null`。requirements 中存在用户明确模式配对时原样透传；不存在时完全省略。还要设置 `supervisor_handoff=required`，把 [references/contracts.md](references/contracts.md) 中的完整 `skill-result.json` schema 和冻结的 `baseline_tree` 交给子 Skill。子 Skill 必须原样返回完整 `requirements_snapshot` 和匹配的 digest，使用自身的 `workspace-snapshot.py` 在最后累计 review 前后采集 `git-cumulative-diff-sha256-v1` 快照，并把最终 `diff_hash`、`changed_paths` 分别逐字复制为 `review_checkpoint.reviewed_diff_hash`、`review_checkpoint.changed_paths`；父级以同版本算法独立复算，不得事后补造旧快照。

### 3. 复用子 Skill Review 检查点

子 Skill 返回后，不再启动相同 diff 的外层 review。立即把结果写入 `<skill-result.json>` 并执行：

```bash
python3 <skill-dir>/scripts/supervisor-state.py record-fix \
  --state <temp-state.json> --skill-result-file <skill-result.json>
```

状态助手重新计算当前快照并核验：

- `review_checkpoint.source=code-review-fix-loop`；
- `review_checkpoint.snapshot_contract=git-cumulative-diff-sha256-v1`；
- `review_complete=true`、`coverage_status=covered`；
- `review_baseline` 等于冻结的 `baseline_tree`；
- `reviewed_diff_hash` 和 `changed_paths` 精确等于当前工作区；
- finding 明细与 `unresolved_count` 一致；
- 子 Skill 的 `requirement_digest` 等于当前冻结需求摘要；
- 子 Skill 的 `requirements_snapshot` 逐字等于当前冻结的完整 requirements object；
- 子 Skill 报告的测试模式与用户冻结选择一致，用户未指定时只能是 `light/default`。
- 没有纠偏指令时，`alignment_directive=null`、`alignment_directive_status=not_applicable`、`alignment_resolution_evidence=null`；存在纠偏指令时必须逐字回显并给出 `resolved|unresolved` 状态和非空证据，`complete` 只能与 `resolved` 配对。
- `exit_test_count=0` 时必须是 `skipped + unverified`，并携带与最终状态相符的标准 `exit_test_skip_reason`；有持久化修改时只能以 `no_applicable_automated_test` 明确声明没有适用测试，不能用空原因绕过默认 light。

### 4. 在额外窗口前纠偏

当 `record-fix` 接纳了 `incomplete + max_fix_rounds_reached + fix_rounds=5` 且尚未达到全局窗口上限时，状态进入 `alignment_required`，不得直接开始下一窗口。

把完整对话中的当前用户需求、仓库强制规则、当前全部 diff、最后子 Review 检查点、finding 台账、退出原因、验证证据，以及当前 `requirement_digest`、最后 Review 检查点的规范 `review_checkpoint_digest` 和 `hash-diff` 返回的 snapshot contract、baseline、HEAD、hash、完整 changed paths 交给 `review-fix-alignment-supervisor`。它必须对当前完整 diff 做一次静态对齐与设计收敛检查，并把这些绑定字段逐字写入 [references/contracts.md](references/contracts.md) 中的 `alignment-result.json`；不得重跑 bug review 或任何测试。

```bash
python3 <skill-dir>/scripts/supervisor-state.py record-alignment \
  --state <temp-state.json> --alignment-result-file <alignment-result.json>
```

- `continue`：允许按原方向开始下一窗口；
- `redirect`：允许开始下一窗口，但必须把 `correction_directive` 交给子 Skill；
- `ask_developer`：立即停止所有后续写入、review 和测试，向开发人员提出结果中的唯一问题；
- `blocked`：立即停止，并报告解除条件。

Alignment 结论必须绑定当前 requirement digest、snapshot contract、baseline、HEAD、`diff_hash` 和 changed paths。评估期间需求、diff 或 HEAD 变化、任一绑定字段过期、契约不完整都不得继续。

### 5. 完成、续接或停止

| 子 Skill 结果 | Supervisor 行为 |
|---|---|
| `complete + converged + unresolved_count=0`，检查点有效 | 直接完成，不追加 review |
| `incomplete + max_fix_rounds_reached + fix_rounds=5`，只有可自动修复 findings，检查点有效 | 未达 `max_fix_windows` 时先进入 alignment checkpoint；只有 `continue` 或 `redirect` 才开启下一窗口 |
| 已到最后窗口仍有 finding | 记录 `max_fix_windows_reached` 并停止 |
| `blocked`、验证 `failed/blocked`、需要决策/澄清、alignment 要求询问/阻塞、检查点无效或范围漂移 | 立即熔断 |

同一窗口没有持久化修改时，只有 `fix_rounds=0`、子 Skill 返回 `complete/converged`，并且复用输入检查点对全部原 finding 做了可审计重分类时才可完成：fingerprint 集合必须完全相同，severity/location 不变，不能把 findings 直接清空。只要报告了修复轮次，diff 就必须变化。

## 测试归属

每个 `code-review-fix-loop` 窗口仍是一次独立调用，按其契约在退出时最多运行一次自动化验证。Supervisor 不通过伪造 `test_mode=none` 压制中间窗口测试，也不接管其验证所有权。因此整个 campaign 最多可能出现 `max_fix_windows` 次子流程退出验证；若以后需要整个 campaign 只验证一次，应另行设计 validation ownership，不能在本 Skill 中暗改。

## 范围扩展

已有 finding 的修复需要触及未修改的 tracked 直接依赖时，只能在 `ready_for_fix`、调用 `begin-fix-window` 前用 `extend-scope --reason direct_dependency` 登记。其他路径必须先取得用户明确授权，写入新的 requirements 基线并重新初始化；未登记的新 changed path 一律熔断。

## 状态助手

```bash
python3 <skill-dir>/scripts/supervisor-state.py hash-diff --state <state>
python3 <skill-dir>/scripts/supervisor-state.py extend-scope --state <state> \
  --paths-file <json-array-file> --reason direct_dependency
python3 <skill-dir>/scripts/supervisor-state.py begin-review --state <state> \
  --drift-check-file <drift-check.json>
python3 <skill-dir>/scripts/supervisor-state.py execute-review --state <state>
python3 <skill-dir>/scripts/supervisor-state.py adopt-fix-result --state <state> \
  --skill-result-file <skill-result.json>
python3 <skill-dir>/scripts/supervisor-state.py begin-fix-window --state <state> \
  --drift-check-file <drift-check.json>
python3 <skill-dir>/scripts/supervisor-state.py record-fix --state <state> \
  --skill-result-file <skill-result.json>
python3 <skill-dir>/scripts/supervisor-state.py record-alignment --state <state> \
  --alignment-result-file <alignment-result.json>
python3 <skill-dir>/scripts/supervisor-state.py status --state <state>
```

退出码 `3` 表示硬熔断，不得继续；退出码 `4` 只表示初始 reviewer 首次不完整，可以重试一次。

## 授权与输出

- 只自动写入项目内与 finding 直接相关的文件以及本 campaign 的系统临时状态；保留无关用户改动。
- 不自动 commit、push、创建 PR、merge、部署、访问生产或修改远程状态。
- 最终报告必须包含：初始完整 review 次数、是否接管已有结果、修复窗口数、累计修复批次、Review 检查点总数、其中复用的子检查点数（等于已接纳的 `fix_window_count`）、alignment checkpoint 次数和最后结论、逐项 finding、最终 changed paths 与 diff hash、测试模式、每窗口 `exit_test_skip_reason` 与验证证据、未验证边界、阻塞项和退出原因。
- 只有最后接纳的完整检查点无 actionable finding、台账无未终结项且验证未失败或阻塞时，才能声明 campaign 完成。
