---
name: bounded-review-supervisor
description: Use when the user explicitly invokes `$bounded-review-supervisor` and the current task has an active Goal; never use for implicit or ordinary natural-language review requests.
---

# Bounded Review Supervisor

## 触发门禁

仅在以下条件同时成立时继续：

1. 用户明确调用 `$bounded-review-supervisor`，而不是只提到名称。
2. 通过原生 Goal 状态确认当前存在 active Goal；不得根据提示词里出现 “Goal” 自行推断。

任一条件不成立时，在读取契约、检查 diff、创建状态文件或启动 reviewer 前停止。不得把普通自然语言请求、一般“review 本地 diff”或相似工作流自动升级为本 Skill。

门禁通过后，先向用户显示“已显式触发 `$bounded-review-supervisor`；当前 active Goal 已确认。”，再开始初始化。

## 核心职责

作为外层有界状态机，循环执行“完整累计 diff review → 交给现有修复流程 → 再次完整 review”。不得修改 `code-review-fix-loop`；有 finding 时把它们作为现成 finding 交给该 Skill。

## 初始化

1. 用 Git 根目录固定 `project_root` 和 `baseline_head`；脚本拒绝子目录、非仓库、无效 SHA，并在每个状态转换前确认 HEAD 未变化。
2. 从对话生成 `<requirements.json>`：保存原始需求、用户明确接受的补充、目标、范围、非目标、验收、授权边界、用户已明确批准的额外 repo-relative 路径、`test_mode`、`test_mode_reason` 和未决事项。`test_mode` 按用户要求固定为 `none`、`light` 或 `deep`；代理总结不得覆盖用户原文，存在未决事项时初始化即停止。执行前完整阅读 [references/contracts.md](references/contracts.md) 的 JSON 契约和 [references/invariants.md](references/invariants.md) 的统一安全不变量。
3. 在系统临时目录、且在 `project_root` 外创建本 Goal 专属状态文件，执行：

```bash
python3 <skill-dir>/scripts/supervisor-state.py init \
  --state <temp-state.json> --project-root <project-root> \
  --baseline-head <sha> --requirements-file <requirements.json> \
  --max-rounds <1-4>
```

`--max-rounds` 可省略，默认值和硬上限均为 `4`，允许指定 `1`–`4`。大于 `4` 或小于 `1` 必须拒绝。始终用状态助手推进轮次；不得靠心算、重建状态或重命名轮次绕过上限。旧状态中的更大上限必须迁移到 `4`；轮次计数已经越界时停止，不得继续。

## Supervisor 命令层限制

本节只约束 `bounded-review-supervisor` 外层代理在整个 Skill 执行期间直接发起的命令：

- 不得直接运行 test、lint、typecheck、build、integration、E2E、smoke、benchmark 或其他自动化测试/验证命令。
- 不得直接运行面向整个仓库的代码扫描、全库索引、全库静态分析或无边界探索；只允许为编排所需读取 Git 状态与 diff、检查明确路径、执行精确搜索及调用本 Skill 的状态助手。
- `execute-review` 启动的原生 reviewer 和 `code-review-fix-loop` 等子流程或子 Skill 不继承本限制，分别服从其自身契约。不得把本节改写进子流程提示，也不得据此降低子流程原有验证要求。
- 外层最终证据核对只汇总既有证据，不新增自动化测试或全库扫描。

状态文件必须记录 `outer_execution_policy`，明确限制作用域、禁止项以及 `propagate_to_child_processes_or_subskills=false`。

## 每轮流程

1. **需求漂移门禁 `requirement_drift_check`**：重读并原样写入原始需求和已接受补充，生成 `<drift-check.json>`，比较需求原文、补充、digest、当前完整累计 diff、未终结 findings 和拟执行修复；同时逐项列出全部 untracked 文件的 `safe_to_review` 分类和理由。只有 `aligned`、`aligned_with_amendment` 可继续；任一需求基线、文件清单或筛查清单不一致即熔断。
2. `hash-diff --state <state>` 先生成统一路径快照：baseline HEAD、禁用 rename 检测的 staged/unstaged 两侧路径、untracked 路径。只按路径组件、认证配置结构、扩展名和规范化文件名语义执行 fail-closed 分类；敏感 tracked 路径必须在读取 diff 内容前停止。untracked 只允许普通文件，symlink、FIFO、socket、device 和目录全部拒绝。筛查通过后，哈希 HEAD、staged/unstaged binary diff 和 untracked metadata；结束时重新采集路径快照，任何变化均熔断。不得读取 untracked 内容，也不得遍历 Git 审查范围之外的仓库文件。
3. 用 `begin-review --drift-check-file` 提交漂移证据。脚本同时检查 Git root、baseline HEAD、实际 changed paths 和已批准范围。
4. 筛查完整且无 `.env`、凭据样式文件或 untracked symlink 后，用 `execute-review` 由 Supervisor 自己启动原生 `codex exec --sandbox read-only review --uncommitted`，对当前全部 staged、unstaged、untracked 和前序修复改动做独立、只读、完整 review；不得同时传入与 `--uncommitted` 互斥的 positional prompt，不得由调用方代写 review 结果，也不得降级为只审上一轮文件。必须由 CLI sandbox 强制 reviewer 只读，不能只依赖事后 diff 检查。
5. Review 期间保持工作区不变。Supervisor 在启动前后都重新核对统一快照、安全分类和完整 diff hash，并从实际进程退出码和最终输出派生覆盖状态、reviewed diff hash、原始输出摘要及 findings。结构化 JSON 是主协议：必须通过 schema 并显式声明 `coverage_complete: true`。Markdown 仅作为兼容 fallback：逐句匹配带 reviewer、review、coverage 或明确范围主体的 coverage 语法，不得对普通缺陷描述做全篇关键词扫描；只接受标准 P0–P3 finding 或整段严格 clean 结论，`no new issues` 不算 clean。Markdown 指纹不包含 severity 或行号；JSON fingerprint 使用 `path|symbol|root-cause|trigger` 并由 Supervisor 规范化哈希。只在完整结果与冻结快照均被接受后计数；不完整、工作区或 HEAD 变化、待决策/待澄清/阻塞或同一 fingerprint 连续三次出现均按契约熔断。
6. 状态为 `ready_for_fix` 时，把 findings 交给 **REQUIRED SUB-SKILL:** `code-review-fix-loop`，并显式传递 requirements 中冻结的 `test_mode` 与 `test_mode_reason`。外层命令层禁测限制不得随之传递；子 Skill 可按其高风险规则升级模式，但不得降级请求模式，其测试、focused review、验证和内部熔断保持原样。
7. 将其最终状态、修复轮次、未终结数量、coverage、validation、`requested_test_mode`、`effective_test_mode` 和最终 `test_mode_reason` 写入 `<skill-result.json>`，再用 `record-fix` 记录。`record-fix` 会核对 requested 模式与 requirements 完全一致，并拒绝任何模式降级。只有 `complete + unresolved_count=0 + validation_status!=blocked` 且 diff 已变化才能进入下一轮。

若 review finding 的修复必须触及初始范围外的 tracked 直接依赖，只能在 `ready_for_fix` 阶段、修改前用 `extend-scope --reason direct_dependency` 显式记录；`ready_for_review` 或其他阶段必须拒绝该 reason，防止在 finding 出现前预先扩大范围。其他扩张必须先停下取得用户明确授权，将路径写入新需求基线的 `authorized_scope_extensions` 并重新初始化，再用 `--reason user_authorized` 登记；未登记的新 changed path 会熔断。

配置的最后一次完整 review 是最终门禁，最多为第 `4` 次：无 finding 才完成；仍有 finding 时记录并停止，不修复，也不得开始下一次。

## 状态助手

```bash
python3 <skill-dir>/scripts/supervisor-state.py hash-diff --state <state>
python3 <skill-dir>/scripts/supervisor-state.py extend-scope --state <state> \
  --paths-file <json-array-file> --reason direct_dependency
python3 <skill-dir>/scripts/supervisor-state.py extend-scope --state <state> \
  --paths-file <json-array-file> --reason user_authorized
python3 <skill-dir>/scripts/supervisor-state.py begin-review --state <state> \
  --drift-check-file <drift-check.json>
python3 <skill-dir>/scripts/supervisor-state.py execute-review --state <state>
python3 <skill-dir>/scripts/supervisor-state.py record-fix --state <state> \
  --skill-result-file <skill-result.json>
python3 <skill-dir>/scripts/supervisor-state.py status --state <state>
```

退出码 `3` 表示硬熔断；不得继续。退出码 `4` 表示首次 reviewer 不完整，可重新开始一次完整 review；再次失败将熔断。

## 授权边界

- 按任务最小必要原则自动读取项目外依赖、文档、只读兄弟实现和脱敏日志；不得读取凭据、token、cookie、私钥、Keychain、`.env*.local` 或 Git ignored secrets。untracked hash 只使用 metadata；读取内容前必须先排除敏感路径。平台要求时主动申请范围明确的只读权限；拒绝后停止。
- 自动写入 `project_root` 内与 finding 和验证直接相关的文件；保留无关用户改动。
- 允许在系统/Codex 临时目录写本 Goal 的状态、scope、review 结果和 finding 文件，禁止秘密材料，仅清理本 Goal 创建的临时文件。
- 除上述临时文件外，任何项目外持久化写入必须在操作前报告准确路径、原因、变更和替代方案并等待确认。不得用软链接或路径跳转绕过。
- 不自动 commit、push、创建 PR、merge、部署、访问生产或修改远程状态。

## 停止与输出

需求漂移、基线不可用、重复 finding、修复后 diff 不变、reviewer 连续失败、范围持续扩大、需要产品/权限/schema/架构决策、项目外持久写入或远程操作时停止。结束前直接核对状态台账、diff hash、review 结果和子流程已经提供的验证证据；外层不得为完成声明追加测试或全库扫描。

最终报告：完整 review 次数、修复轮次、逐项 finding、最终文件与 diff hash、漂移检查、验证证据、覆盖和未验证边界、外部读取、临时文件、待决策/阻塞项及退出原因。只有最新完整 review 无 actionable finding 且台账无未终结项时才能声明完成。
