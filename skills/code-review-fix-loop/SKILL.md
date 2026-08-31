---
name: code-review-fix-loop
description: Use when Codex needs to resolve actionable findings from a local code review, repeat review after fixes, or coordinate a review-fix-review cycle for staged, unstaged, untracked, branch, or commit changes; not for read-only review requests, GitHub review threads, CI-only failures, deployment, or production operations.
---

# Code Review Fix Loop

## 核心职责

只负责调度：固定审查范围、维护 finding 台账、批量修复同根因问题、调用独立 reviewer、判断收敛并报告结果。核心原则是：**没有现成 finding 时先做一次全量 review；修复后只做 focused review；所有 review 始终只读；自动化验证服从仓库规则、用户要求、风险与 `test_mode`。**

## 入口边界

使用本流程：

- 用户要求修复已有本地 review findings、修完复审或持续处理直到收敛。
- 当前目标是 staged、unstaged、untracked、branch 或 commit diff。

改用专门流程：

- 只读审查：使用原生 `/review` 或 `codex exec review`。
- GitHub review threads：使用 `gh-address-comments`。
- GitHub Actions：使用 `gh-fix-ci`。
- 部署、迁移、生产数据、队列、Redis：退出本循环，明确影响范围、授权边界、停止条件、验证和回滚方案后等待用户确认。

## 状态与台账

初始化：

```text
fix_round = 0
max_fix_rounds = 5
initial_review_count = 0
focused_review_count = 0
coverage_status = uncovered
validation_status = unverified
test_mode = none
test_mode_reason = default
initial_scope_files = []
round_paths = []
ledger = []
```

台账字段：

```text
| ID | 来源轮次 | 严重度 | 文件位置 | 场景/影响 | 状态 | 执行 Skill | 证据/下一步 |
```

状态只使用：`待判断`、`待修复`、`已修复`、`已修复-未验证`、`不成立`、`非本次引入`、`低信号不处理`、`需要决策`、`需要澄清`、`阻塞`。

`coverage_status` 只表示 reviewer 覆盖，使用 `covered`、`partial`、`uncovered`。`validation_status` 单独表示验证证据，使用 `unverified`、`focused`、`complete`、`blocked`。`test_mode` 使用 `none`、`light`、`deep`；`test_mode_reason` 记录 `default`、`user:light`、`user:deep` 或 `risk:<evidence>`。不得用测试结果替代 review 覆盖，也不得用 review 通过替代行为验证。结束循环不要求 `coverage_status=covered`，但必须如实报告覆盖边界。

开始修复前记录完整 `initial_scope_files`。每个修复批次只记录本轮实际修改的 `round_paths`，不得用扩大后的工作树替换原始范围。

## 调度矩阵

| 任务 | 执行规则 |
|---|---|
| 独立只读审查 | `scripts/run-review.sh`，底层为 `codex exec review` |
| 判断 finding 是否成立 | 对照当前代码、触发场景和可复现证据独立核对；reviewer 结论不是事实本身 |
| 根因未知、测试失败或行为异常 | 先收集证据并尽量复现，定位根因后再修改；日志、smoke 等场景按入口边界路由专门 SKILL |
| 修复 bug 或改变行为，且 `test_mode=light/deep` | 遵循仓库 `AGENTS.md`、开发规范和现有测试策略；仓库未规定时按风险选择直接相关验证，不强制采用 TDD |
| 修复 bug 或改变行为，且 `test_mode=none` | 只做最小修复并标记 `已修复-未验证`，不得暗中运行测试 |
| 完成声明与证据核对 | 逐项记录实际命令、结果与未验证边界；不得把静态推理、review 通过或未执行的测试写成验证成功 |
| 权限、数据口径、schema 或架构决策 | 标记 `需要决策` 并停止自动修复；列出必须由用户确认的决策、约束与验收标准 |
| UI 交互或视觉链路 | `playwright` |
| PR review threads | `gh-address-comments` |
| GitHub Actions 失败 | `gh-fix-ci` |
| 生产或远程状态变更 | 退出本循环，报告影响范围、所需授权、停止条件和回滚入口 |

## 测试授权

默认 `test_mode=none`。先按用户要求选择基础模式，再根据当前修复批次涉及的代码风险决定是否升级。

- 未显式要求测试：使用 `none`，记录 `test_mode_reason=default`。
- 显式要求“自动化测试”但未指定级别时，使用 `light`，记录 `test_mode_reason=user:light`；“测试一下”“运行测试”“轻量测试”同样按 `light` 处理。
- 显式要求“深度测试”、“完整测试”、“全量测试”或“所有自动化测试”：使用 `deep`，记录 `test_mode_reason=user:deep`。
- 要求含糊时先选择较低级别；形成具体修复批次后再执行风险判断。

高风险代码可以把 `none/light` 自动升级为 `deep`。升级后本次循环保持 `deep`，不得在后续批次自动降级；记录 `test_mode_reason=risk:<evidence>`，其中 evidence 必须包含具体文件或模块、风险边界和可能影响。用户明确禁止自动化测试时保持 `none`，不得用风险规则覆盖明确禁令。

满足以下任一项且本批次实际改变对应行为时，可以判为高风险：

- 身份认证、权限、密钥或安全边界；
- 计费、额度、账户归属或资金相关规则；
- schema、持久化格式、数据兼容性或迁移路径；
- 事务、并发、幂等、重试或任务状态机；
- 公共 API、跨服务契约或广泛复用的核心基础设施。

不得只因文件较多、代码复杂或“保险起见”升级。若高风险同时涉及未确认的产品、数据或架构语义，先进入决策门，不得用 deep 测试替代决策。

各模式的执行边界：

- `none`：禁止运行 test、lint、typecheck、build、E2E、smoke 或其他自动化验证；只允许 Git diff/status、代码搜索、文件读取和静态推理等只读分析。
- `light`：只运行与改动直接对应的最小自动化测试，例如单个单元、组件或 API 测试文件；禁止全工作区测试、完整 lint/typecheck/build、E2E、smoke、Docker、网络或远程检查。
- `deep`：每个修复批次先运行直接相关验证；focused review 收敛后再运行一次仓库规定且与风险相称的完整本地验证矩阵，包括适用的 test、lint、typecheck、build、integration、E2E 和本地 smoke；仍不得连接生产、修改远程状态或执行不可逆操作。

完整本地自动化验证矩阵的唯一入口是 `test_mode=deep`。`none` 不得运行任何自动化测试；`light` 不得运行完整本地自动化验证矩阵，也不得升级到全工作区 test、完整 lint/typecheck/build、integration、E2E 或 smoke。无论 `test_mode` 为何，reviewer 都不得运行测试。是否采用 TDD 由仓库规则、现有测试策略、风险和用户要求决定；不得仅因进入本流程强制 TDD。

每个修复批次按以下规则产生验证证据：

- `none`：不运行测试，修改项标记 `已修复-未验证`，`validation_status=unverified`。
- `light`：运行最小直接相关测试，通过后 `validation_status=focused`。
- `deep`：每批运行直接相关测试；focused review 收敛后运行一次完整本地自动化验证矩阵，全部通过后 `validation_status=complete`。

测试失败时先保存失败命令和最小错误证据，区分基线、环境与本次回归，定位根因后再修改。需要继续改代码则进入下一修复批次并重新执行对应验证；无法执行或无法继续时设为 `blocked` 并熔断。没有产生修复批次时，不因选择了 `light/deep` 而单独启动测试。

## Reviewer 分层

### 首次全量 review

全量 review 覆盖用户指定的完整范围：

```bash
<skill-dir>/scripts/run-review.sh --quiet-events --output <scratch-result-path> uncommitted
<skill-dir>/scripts/run-review.sh --quiet-events --output <scratch-result-path> base <base-branch>
<skill-dir>/scripts/run-review.sh --quiet-events --output <scratch-result-path> commit <sha>
```

仅在用户没有提供可处理 finding 时调用一次；已有明确 finding 时跳过。reviewer 正常结束后执行 `initial_review_count += 1`。如果此次审查没有产生需要修复的 finding，可把 `coverage_status` 设为 `covered`；一旦后续修改代码，覆盖状态按 focused review 的实际范围降为 `partial`。

### Focused review

行为、契约或安全边界发生变化时，中间轮次只审查 `round_paths`：

```bash
<skill-dir>/scripts/run-review.sh --quiet-events --output <scratch-result-path> --scope-file <scratch-scope-file> paths <path> [<path> ...]
```

纯文档错字、删除被取代文档等低风险修复可以跳过 focused reviewer。Focused review 只能把 `coverage_status` 设为 `partial`，表示只覆盖本轮修改路径。

在目标仓库外创建 `<scratch-scope-file>`，每行记录一个 `initial_scope_files` 文件或已明确纳入本轮的直接依赖。`paths` 模式只接受该清单中的规范化、字面量、仓库相对文件路径；必须拒绝目录、Git pathspec、Git ignored 文件、范围外路径和当前没有改动的路径。它只允许 reviewer 查看列出的当前改动及必要的 tracked 直接依赖，不允许扩大审查范围。

## Reviewer 与验证职责

- 默认 review 只允许只读分析。reviewer 和主流程在 review 阶段都不得运行 test、lint、typecheck、build、E2E、smoke、Docker、网络或远程检查。
- 默认不得因为进入 review 而运行全局 typecheck/test/build/E2E。reviewer 没有修改代码时，已有验证证据不会因 review 本身失效，不得重复执行。
- 自动化验证由主流程执行：`none` 不运行；`light` 每批只运行最小直接相关测试；`deep` 每批运行直接相关测试，并在 focused review 收敛后运行一次完整本地自动化验证矩阵。
- `test_mode=none` 时，代码修复只能标记为 `已修复-未验证`，最终报告必须列出未验证边界。不得为了获得更强完成声明而自行运行测试。
- reviewer 只做只读代码审查，不得重复运行完整 test、build、E2E、smoke、网络请求或远程检查。
- reviewer 不得读取 `.env.local`、`.env.*.local`、credential、token、cookie、私钥或其他被 Git 忽略的凭据文件。
- 若全量 reviewer 无法通过仓库规则遵守上述边界，先使用可自动恢复的隔离方式让本地凭据对 reviewer 不可见；无法安全隔离时标记 `阻塞`，不得直接暴露凭据。
- wrapper 必须保持只读、使用 `--ephemeral --json`，并在 Git 根目录解析路径和启动 reviewer。由于当前 CLI 的 `--uncommitted`、`--base`、`--commit` 与自定义 review 指令互斥，wrapper 应把 scope 解析为明确的工作树范围或 commit SHA，再通过自定义只读指令调用 reviewer；不得退回无法携带禁测规则的 scope flag。仅当本轮生成新的非空结果且 reviewer 成功时才报告成功；reviewer 失败、结果缺失或范围不明确均视为 `阻塞`。
- 调用 reviewer 时默认使用 `--quiet-events --output`：详细 JSONL 写入结果旁的 `.events.jsonl`，CLI stderr 写入 `.stderr.log`，耗时、事件/错误/结果字节数和退出状态写入 `.metrics`，主流程只读取最终结果与必要指标。等待 reviewer 时每次等待 30–60 秒，不要高频轮询、重复读取事件流或把完整事件历史送回模型。
- focused reviewer 只接收 `round_paths`、对应改动和必要的 tracked 直接依赖；不得读取无关 memory、历史任务、范围外 diff 或执行全仓探索。

## 主循环

1. **固定范围与基础测试模式**：记录用户目标、原始 findings、允许修改边界、`initial_scope_files`、基础 `test_mode` 和 `test_mode_reason`；没有测试要求时使用 `none/default`。
2. **获取首次审查**：已有 finding 则跳过；否则执行一次全量 review。
3. **判断并合并**：对照当前代码、触发场景和可复现证据核对 finding，合并同根因 findings，按阻塞级别组织为一个修复批次。
4. **评估代码风险**：根据当前批次实际触及的行为判断风险；符合高风险条件时把 `none/light` 升级为 `deep` 并记录具体 `test_mode_reason`。不得仅按改动规模升级。
5. **批量修复与直接验证**：按调度矩阵处理当前批次并记录 `round_paths`。`none` 只做最小修复且不运行测试；`light/deep` 按仓库规则运行最小直接相关测试。不要每修一项就启动 reviewer。
6. **Focused review**：行为、契约或安全边界变化时只读审查 `round_paths`；纯文档低风险修复可跳过。此阶段不运行任何自动化测试。
7. **限制新增 finding**：中间 reviewer 的新 finding 只有在由 `round_paths` 引入、属于 `initial_scope_files` 或其直接依赖、并且可复现时才进入 `待修复`。历史遗留、范围外或低信号项进入对应终态或待办，不得扩大本循环。
8. **深度验证**：`test_mode=deep` 且 focused review 已收敛时，运行一次与仓库规则和风险相称的完整本地验证矩阵。失败则保存证据并回到根因分析；修复后必须重新 focused review，再重新执行完整矩阵。
9. **结束**：无未终结 finding 且授权验证已完成时，核对并汇总实际证据；按实际结果报告 `coverage_status` 与 `validation_status`。覆盖或验证不完整时不得宣称完整范围已审净或修复已被完整验证。

每完成一个修复批次再执行 `fix_round += 1`，不是每个 finding 或每次测试都加一轮。

## 自动修复条件

仅在以下条件全部满足时自动修复：

- finding 有具体触发场景且经代码事实验证成立；
- 修法符合现有模式，不改变未确认的业务、权限、schema 或数据语义；
- 修改局限于 `initial_scope_files` 及直接依赖；
- `test_mode=none` 时修法足够明确且可以通过代码事实与 focused review 检查，并接受 `已修复-未验证` 状态；`light/deep` 时可以通过对应级别的自动化测试验证。

否则标记 `需要决策`、`需要澄清` 或 `阻塞`。

## 熔断与安全

以下任一条件成立即停止循环：

- `fix_round >= max_fix_rounds`；
- 同类失败重复出现且范围没有缩小；
- 连续两次 reviewer 的 finding 指纹与相关 diff hash 均未变化；
- 修复持续扩大影响面；
- reviewer 连续失败或覆盖不完整；
- 需要生产、远程、凭据或不可逆操作；
- 需要用户决定产品或数据语义。

不得回滚用户无关改动，不得执行破坏性 Git 操作，不得把 secret、生产连接串或未脱敏日志发送给 reviewer。

## 最终输出

始终输出：

```text
修复总结：
修复轮次：fix_round/max_fix_rounds
首次全量 review：initial_review_count
focused review：focused_review_count
修复项摘要：
已修改：
现存问题：
待决策项：
待澄清项：
已验证：
未验证边界：
原生 reviewer：
coverage_status：
validation_status：
test_mode：
test_mode_reason：
退出原因：
```

`修复项摘要` 每项包含 ID、严重度、位置、最终状态、执行 Skill、修复摘要和验证证据；没有修复项时写 `无已修复项`。`coverage_status=partial/uncovered` 时只能声明台账内没有未终结 finding，不得声明完整审查范围没有新的 actionable finding。
