---
name: code-review-fix-loop
description: Use when Codex needs to resolve actionable findings from a local code review, repeat review after fixes, or coordinate a review-fix-review cycle for staged, unstaged, untracked, branch, or commit changes; not for read-only review requests, GitHub review threads, CI-only failures, deployment, or production operations.
---

# Code Review Fix Loop

## 核心职责

只负责调度：固定审查范围、维护 finding 台账、批量修复同根因问题、调用独立 reviewer、判断收敛并报告结果。核心原则是：**没有现成 finding 时先做一次全量 review；每个产生持久化修改的修复批次后，对当前完整累计 diff 做一次全量静态 review；所有 review 始终只读；本 Skill 不在修复轮次内自行运行自动化测试；收敛完成或达到修复轮次上限后，对累计修改执行至多一次退出验证。退出验证默认使用 `light`，只有用户明确要求时才使用 `deep`。**

## 入口边界

使用本流程：

- 用户要求修复已有本地 review findings、修完复审或持续处理直到收敛。
- 当前目标是 staged、unstaged、untracked、branch 或 commit diff。
- 本地 working-tree review 所在的 Git 仓库已有至少一个 commit 和有效 `HEAD`；尚无初始 commit 时必须先停止并提示建立基线，本流程不把 unborn `HEAD` 隐式解释为空树。

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
cumulative_review_count = 0
coverage_status = uncovered
validation_status = unverified
round_test_mode = none
test_mode = light
test_mode_reason = default
exit_test_count = 0
exit_test_status = not_run
exit_test_skip_reason = unset
initial_scope_files = []
review_baseline = unset
current_diff_paths = []
last_cumulative_review_diff_hash = unset
snapshot_contract = git-cumulative-diff-sha256-v1
round_paths = []
cumulative_changed_paths = []
external_validation_evidence = []
supervisor_handoff = available
requirements_snapshot = unset
requirement_digest = unset
alignment_directive = unset
alignment_directive_status = not_applicable
alignment_resolution_evidence = unset
ledger = []
```

台账字段：

```text
| ID | 来源轮次 | 严重度 | 文件位置 | 场景/影响 | 状态 | 执行 Skill | 证据/下一步 |
```

状态只使用：`待判断`、`待修复`、`已修复`、`已修复-未验证`、`不成立`、`非本次引入`、`范围外`、`低信号不处理`、`需要决策`、`需要澄清`、`阻塞`。

`coverage_status` 只表示 reviewer 覆盖，使用 `covered`、`partial`、`uncovered`。`validation_status` 单独表示本 Skill 的退出验证证据，使用 `unverified`、`focused`、`complete`、`failed`、`blocked`。`exit_test_status` 使用 `not_run`、`passed`、`failed`、`blocked`、`skipped`。不得用测试结果替代 review 覆盖，也不得用 review 通过替代行为验证。结束循环不要求 `coverage_status=covered`，但必须如实报告覆盖边界。

`round_test_mode` 固定为 `none`，不得改变。`test_mode` 只控制退出验证，使用 `none`、`light`、`deep`；`test_mode_reason` 使用 `default`、`user:none`、`user:light` 或 `user:deep`。`exit_test_count` 统计退出验证阶段，不统计阶段内的命令数，且不得超过 `1`。执行退出验证时 `exit_test_skip_reason=null`；跳过时必须记录标准原因 `user_disabled`、`no_persistent_change`、`no_applicable_automated_test` 或 `blocked_before_exit_validation`，不能只写 `not_run` 或笼统的 `skipped`。

`supervisor_handoff` 默认是 `available`：最终除人类可读报告外，还按 [references/handoff-contract.md](references/handoff-contract.md) 输出可复用的结构化结果，使本次调用达到五轮上限后可以被 `bounded-review-fix-supervisor` 直接接管。开始循环时按该文件固定完整 `requirements_snapshot` 并计算 `requirement_digest`；被上层 Supervisor 调用时改为 `required`，逐字采用父级提供的完整 snapshot 和 digest，并核对二者匹配。若父级还提供 `alignment_directive`，必须把它作为本窗口约束，并在最终结果逐字回显，同时返回 `alignment_directive_status=resolved|unresolved` 和非空 `alignment_resolution_evidence`；没有指令时保持 `null + not_applicable + null`。带指令的结果只有证明 `resolved` 才能声明 `complete`。两种模式使用同一 schema，不改变本 Skill 的五轮上限、review 范围或测试归属，也不依赖 sibling Skill；不得由调用方补造缺失的需求、纠偏状态或 review 证据。

开始修复前记录完整 `initial_scope_files`，并按原始目标冻结 `review_baseline`。每个修复批次只用 `round_paths` 记录本轮实际修改，但修复后的 reviewer 范围必须重新从 `review_baseline` 计算为全部 `current_diff_paths`，不得只审 `round_paths`。所有当前 diff 路径必须属于 `initial_scope_files` 或已明确纳入的直接依赖，范围外漂移必须熔断，不能静默吸收。把持久化修改并入 `cumulative_changed_paths`，退出测试必须覆盖全部累计修改，不能只看最后一轮路径。

需要冻结或交接工作区快照时，始终使用本 Skill 自带的规范助手，不依赖任何 sibling Skill：

```bash
python3 <skill-dir>/scripts/workspace-snapshot.py \
  --project-root <git-root> --baseline <review-baseline-tree>
```

助手输出 `snapshot_contract=git-cumulative-diff-sha256-v1`、baseline、HEAD、完整 changed paths 和 `diff_hash`；changed paths 与 hash 都覆盖 `baseline → HEAD` committed diff、staged、unstaged 和 untracked，并显式覆盖仓库或 submodule 配置中的 ignore 选项。每次 reviewer 前后各采集一次；只有 contract、baseline、HEAD、paths 和 hash 全部相同才接受该 review。上层可以用同版本算法独立复算，但不得替子 Skill 补造旧快照。changed gitlink 或 dirty submodule 无法形成该规范快照，必须阻塞。

## 调度矩阵

| 任务 | 执行规则 |
|---|---|
| 独立只读审查 | `scripts/run-review.sh`，底层为 `codex exec review` |
| 判断 finding 是否成立 | 对照当前代码、触发场景和可复现证据独立核对；reviewer 结论不是事实本身 |
| 根因未知、已有测试失败或行为异常 | 先检查调用方提供的证据并定位根因；本 Skill 不为修复轮次自行启动测试，其他流程要求的诊断由其要求方管理 |
| 修复 bug 或改变行为 | 只做符合现有模式的最小修复，记录为 `已修复-未验证`，等待当前完整累计 diff review 和退出验证 |
| 仓库开发流程、用户或上层 Skill 要求 TDD、preflight 或其他测试 | 由提出要求的流程调度、记录和解释；保留证据来源，不计入 `exit_test_count`，不得冒充为本 Skill 的退出验证 |
| 本 Skill 的自动化验证 | 只在符合条件的退出阶段执行一次；默认 `light`，用户明确要求时才使用 `deep` |
| 完成声明与证据核对 | 逐项记录实际命令、结果与未验证边界；不得把静态推理、review 通过、外部证据或未执行的测试写成本 Skill 的验证成功 |
| 权限、数据口径、schema 或架构决策 | 标记 `需要决策` 并停止自动修复；列出必须由用户确认的决策、约束与验收标准 |
| UI 交互或视觉链路 | 若由其他流程要求验证，则由该流程管理；本 Skill 只有在用户明确选择适用的 `deep` 退出验证时才自行运行 Playwright |
| PR review threads | `gh-address-comments` |
| GitHub Actions 失败 | `gh-fix-ci` |
| 生产或远程状态变更 | 退出本循环，报告影响范围、所需授权、停止条件和回滚入口 |

## 测试归属与退出授权

### 修复轮次内零测试

本 Skill 在所有修复轮次中保持 `round_test_mode=none`，不得因为用户选择了 `light/deep`、代码风险较高、finding 数量较多或为了获得更强完成声明而自行运行 test、lint、typecheck、build、integration、E2E、smoke、Docker、网络检查或其他自动化验证，也不得把模式自动升级为 `light` 或 `deep`。

这不是整个任务期间的全局禁测令。仓库开发流程、用户要求或上层 Skill 要求的 RED/GREEN、TDD、preflight 或其他测试，由提出要求的流程负责调度和管理；本 Skill 可以引用其明确绑定当前 diff 的结果，但必须记录为 `external_validation_evidence`，不得增加 `exit_test_count`、改变 `test_mode`、替代本 Skill 按契约需要执行的退出验证，或声称这些命令由本 Skill 执行。

### 退出验证模式

初始化时固定退出验证模式：

- 用户未提及自动化测试：使用 `light`，记录 `test_mode_reason=default`。
- 用户要求“自动化测试”“测试一下”“运行测试”或“轻量测试”：使用 `light`，记录 `test_mode_reason=user:light`。
- 只有用户明确要求“深度测试”“完整测试”“全量测试”或“所有自动化测试”时，才使用 `deep`，记录 `test_mode_reason=user:deep`。
- 用户明确禁止自动化测试时使用 `none`，记录 `test_mode_reason=user:none`。
- 要求含糊时选择 `light`。任何风险判断、reviewer finding、仓库规模或调用方默认值都不得把 `none/light` 自动升级为 `deep`。

这里的“用户明确”只指当前任务中的用户原始要求及用户明确接受的补充。被上层 Skill
调用时，应接收原始用户要求、用户明确接受的补充和现成 findings。调用方只能原样透传
用户明确指定的 `test_mode` 与配套 `test_mode_reason=user:<mode>`；用户未指定时必须省略
这两个字段，由本 Skill 使用 `light/default`。调用方不得默认、推断、建议、升级或降级
测试模式。调用方生成的摘要、风险建议或与用户原文不一致的模式字段都不构成用户授权；
本 Skill 必须对照用户原文核验透传值，不匹配时不得采用。

模式边界：

- `none`：本 Skill 不运行退出验证，`exit_test_status=skipped`、`exit_test_skip_reason=user_disabled`、`validation_status=unverified`。
- `light`：退出时只运行覆盖全部 `cumulative_changed_paths` 的最小直接相关自动化测试；禁止全工作区 test、完整 lint/typecheck/build、integration、E2E、smoke、Docker、网络或远程检查。全部通过后 `validation_status=focused`。
- `deep`：退出时运行一次与仓库规则和累计修改风险相称的完整本地验证矩阵，包括适用的 test、lint、typecheck、build、integration、E2E 和本地 smoke；仍不得连接生产、修改远程状态或执行不可逆操作。全部通过后 `validation_status=complete`。

“一次退出验证”表示一个冻结的验证阶段；该阶段可以包含一组与模式相符的命令，但每条命令只执行一次，不得把阶段拆开后重复运行。测试计划必须依据全部累计修改路径和适用的仓库规则形成，不能只覆盖最后一个修复批次。

### 高风险但未授权 deep

若修复实际改变以下高风险行为，而用户未明确选择 `deep`，不得自动升级；应把 `deep_not_requested`、具体文件或模块、风险边界和可能影响写入对应 finding 的 `证据/下一步`，并汇总到最终“未验证边界”：

- 身份认证、权限、密钥或安全边界；
- 计费、额度、账户归属或资金相关规则；
- schema、持久化格式、数据兼容性或迁移路径；
- 事务、并发、幂等、重试或任务状态机；
- 公共 API、跨服务契约或广泛复用的核心基础设施。

该警告不是新的 bug、`待决策` 或自动修复项。即使 `light` 退出验证通过，涉及此类警告时也只能报告 `validation_status=focused`，不得宣称完整验证。

## 退出验证阶段

只在以下条件全部满足时运行本 Skill 的退出验证：

- 至少一个修复批次产生了持久化修改，`cumulative_changed_paths` 非空；
- 主循环因 findings 收敛完成或 `fix_round >= max_fix_rounds` 而准备退出；
- `test_mode` 不是 `none`。

没有产生持久化修改、用户明确禁测或因其他熔断条件停止时，不启动退出验证；记录 `exit_test_status=skipped`、标准化的 `exit_test_skip_reason` 和未验证边界。无持久化修改使用 `no_persistent_change`，用户禁测使用 `user_disabled`，退出验证前已熔断使用 `blocked_before_exit_validation`。纯文档等确实没有适用自动化测试的修改可使用 `no_applicable_automated_test`，但必须在台账中写明判断依据，不得伪造验证成功。若存在适用测试但因本地环境无法启动，则启动一次退出验证阶段并记录 `exit_test_count=1`、`exit_test_status=blocked`、`exit_test_skip_reason=null`、`validation_status=blocked` 和最小阻塞证据。

符合条件时：

1. 按 `test_mode` 和全部 `cumulative_changed_paths` 冻结测试计划。
2. 执行该验证阶段一次并令 `exit_test_count=1`；不得在本次调用中启动第二次退出验证。
3. 全部通过时，设置 `exit_test_status=passed`，按模式把 `validation_status` 设为 `focused` 或 `complete`，并把被该证据覆盖的 finding 从 `已修复-未验证` 更新为 `已修复`。
4. 任一命令失败时，保存失败命令和最小错误证据，区分本次回归、基线失败、环境失败与原因未知，把问题随台账输出：本次引入且范围内的问题记为 `待修复`，基线问题记为 `非本次引入`，环境问题记为 `阻塞`，无法判断时记为 `需要澄清`。
5. 失败后设置 `exit_test_status=failed`、`validation_status=failed`，本次调用不得修改代码，也不得返回修复循环、重新启动 reviewer 或再次运行测试；最终结果不得声明完成。环境导致验证无法执行时使用 `blocked` 而不是 `failed`。

达到修复轮次上限时，即使台账仍有未终结 finding，也按上述条件执行至多一次退出验证，并把既有未终结项与测试新增项一起输出；验证通过不能把轮次耗尽或未解决问题转换为成功。

## Reviewer 分层

### 首次全量 review

全量 review 覆盖用户指定的完整范围：

```bash
<skill-dir>/scripts/run-review.sh --quiet-events --output <system-temp-result-path> uncommitted
<skill-dir>/scripts/run-review.sh --quiet-events --output <system-temp-result-path> base <base-branch>
<skill-dir>/scripts/run-review.sh --quiet-events --output <system-temp-result-path> commit <sha>
```

仅在用户没有提供可处理 finding 时调用一次；已有明确 finding 时跳过。reviewer 正常结束后执行 `initial_review_count += 1`。初始化时同时冻结后续累计 review 使用的比较基线：`uncommitted` 使用启动时的 HEAD tree，`base` 使用 merge-base tree，`commit` 使用该提交的比较基线 tree。若 commit 目标无法在当前工作树中准确表示“原提交加当前修复”的净 diff，应先使用隔离工作树或标记 `阻塞`，不得退回只审本轮路径。

### 修复后累计全量 review

每个产生持久化修改的修复批次完成后，都重新审查从冻结 `review_baseline` 到当前工作树的完整累计 diff，包括当前范围内全部 committed、staged、unstaged 和 untracked 变更：

```bash
<skill-dir>/scripts/run-review.sh --quiet-events --output <system-temp-result-path> cumulative <review-baseline>
```

这仍是静态、只读 review，不运行任何自动化测试。开始 reviewer 前后都用 `workspace-snapshot.py` 采集完整 `current_diff_paths` 和 diff hash；路径、内容、HEAD 或 snapshot contract 发生漂移时拒绝结果。只有 reviewer 明确覆盖完整累计 diff 且快照稳定时，才执行 `cumulative_review_count += 1`、记录 `last_cumulative_review_diff_hash` 并把 `coverage_status` 设为 `covered`。reviewer 失败或覆盖不完整时标记 `阻塞`，不得把部分结果当作累计审查完成。

“完整累计 diff”不是全仓扫描。reviewer 只审查冻结基线后的全部当前 diff，并可读取理解这些改动所需的 tracked 直接依赖；不得审查无关历史、基线外代码或范围外工作树改动。`round_paths` 继续用于记录本轮修复和归因，但不再限制 reviewer 输入。

## Reviewer 与验证职责

- 所有 review 只允许只读分析。reviewer 和主流程在 review 阶段都不得运行 test、lint、typecheck、build、integration、E2E、smoke、Docker、网络或远程检查。
- reviewer 只做代码审查；本 Skill 的自动化验证只发生在符合条件的退出阶段。外部流程要求的测试保持其原始归属并单独记录。
- reviewer 没有修改代码时，已有且仍绑定当前 diff 的外部证据不会因 review 本身失效，不得为 reviewer 重复执行。
- reviewer 不得读取 `.env.local`、`.env.*.local`、credential、token、cookie、私钥或其他被 Git 忽略的凭据文件。
- 若全量 reviewer 无法通过仓库规则遵守上述边界，先使用可自动恢复的隔离方式让本地凭据对 reviewer 不可见；无法安全隔离时标记 `阻塞`，不得直接暴露凭据。
- wrapper 必须保持只读、使用 `--ephemeral --json`，并在 Git 根目录解析路径和启动 reviewer。由于当前 CLI 的 `--uncommitted`、`--base`、`--commit` 与自定义 review 指令互斥，wrapper 应把 scope 解析为明确的工作树范围或 commit SHA，再通过自定义只读指令调用 reviewer；不得退回无法携带禁测规则的 scope flag。仅当本轮生成新的非空结果且 reviewer 成功时才报告成功；reviewer 失败、结果缺失或范围不明确均视为 `阻塞`。
- 调用 reviewer 时默认使用 `--quiet-events --output`：详细 JSONL 写入结果旁的 `.events.jsonl`，CLI stderr 写入 `.stderr.log`，耗时、事件/错误/结果字节数和退出状态写入 `.metrics`，主流程只读取最终结果与必要指标。等待 reviewer 时每次等待 30–60 秒，不要高频轮询、重复读取事件流或把完整事件历史送回模型。
- `--output` 必须位于 Git worktree 外的系统临时目录；wrapper 对结果及三个 sidecar 路径执行 fail-closed 检查并拒绝 symlink。不得让 reviewer 产物进入被冻结的 diff，也不得用仓库内相对路径作为输出。
- 累计 reviewer 接收冻结基线后的完整当前 diff 和必要的 tracked 直接依赖；不得读取无关 memory、历史任务、基线外改动或执行全仓探索。

## 主循环

1. **固定范围与退出验证模式**：记录用户目标、原始 findings、允许修改边界、`initial_scope_files`、固定的 `round_test_mode=none`、`test_mode` 和 `test_mode_reason`；用户未提及测试时使用 `light/default`。
2. **获取首次审查**：已有 finding 则跳过；否则执行一次全量 review。
3. **判断并合并**：对照当前代码、触发场景和可复现证据核对 finding，合并同根因 findings，按阻塞级别组织为一个修复批次。
4. **记录风险边界**：判断当前批次是否实际改变高风险行为；未明确选择 `deep` 时写入 `deep_not_requested`，不得改变测试模式。
5. **批量修复**：按调度矩阵处理当前批次，记录 `round_paths` 并更新 `cumulative_changed_paths`；本 Skill 不运行轮内自动化测试，修改项保持 `已修复-未验证`。不要每修一项就启动 reviewer。
6. **累计全量 review**：每个产生持久化修改的批次后，重新计算并只读审查从 `review_baseline` 到当前工作树的完整累计 diff；此阶段不运行任何本 Skill 管理的自动化测试。只有完整覆盖且快照稳定的结果才被接受。
7. **限制新增 finding**：累计 reviewer 的新 finding 只要由当前完整 diff 引入、位于 `initial_scope_files` 或已明确纳入的直接依赖、并且可复现，就进入 `待修复`，不再要求必须由本轮 `round_paths` 引入。基线既有、范围外或低信号项进入对应终态或待办，不得扩大本循环。
8. **推进或停止**：每完成一个修复批次执行 `fix_round += 1`。仍有待修复项且未触发熔断时进入下一批；否则冻结退出原因。
9. **退出验证与报告**：仅在收敛完成或达到修复轮次上限时按退出验证阶段执行至多一次测试；测试发现的问题只入账并输出，不在本次调用中继续修复。最后分别报告 review 覆盖、退出验证状态、外部验证证据和未验证边界。

`fix_round` 按修复批次计数，不按 finding、reviewer 调用、外部测试或退出验证命令计数。

## 自动修复条件

仅在以下条件全部满足时自动修复：

- finding 有具体触发场景且经代码事实验证成立；
- 修法符合现有模式，不改变未确认的业务、权限、schema 或数据语义；
- 修改局限于 `initial_scope_files` 及直接依赖；
- 修法足够明确，可以通过当前代码事实和修复后的累计全量 review 检查，并接受退出验证前始终处于 `已修复-未验证`。

否则标记 `需要决策`、`需要澄清` 或 `阻塞`。若仓库开发流程、用户或上层 Skill 另外要求 TDD 或测试，仍由该要求方管理，不得把其工作转化为本 Skill 的轮内测试模式。

## 熔断与安全

以下任一条件成立即停止循环：

- `fix_round >= max_fix_rounds`；达到轮次上限时仍按退出验证规则处理累计修改；
- 同类失败重复出现且范围没有缩小；
- 连续两次 reviewer 的 finding 指纹与相关 diff hash 均未变化；
- 修复持续扩大影响面；
- reviewer 连续失败或覆盖不完整；
- 需要生产、远程、凭据或不可逆操作；
- 需要用户决定产品或数据语义。

除达到修复轮次上限外，其他熔断条件默认不启动退出验证；只报告已有证据和未验证边界。不得回滚用户无关改动，不得执行破坏性 Git 操作，不得把 secret、生产连接串或未脱敏日志发送给 reviewer 或测试进程。

## 最终输出

始终输出：

```text
修复总结：
结果状态：complete | incomplete | blocked
修复轮次：fix_round/max_fix_rounds
首次全量 review：initial_review_count
修复后累计全量 review：cumulative_review_count
修复项摘要：
已修改：
累计修改范围：
现存问题：
待决策项：
待澄清项：
已验证：
外部验证证据：
未验证边界：
原生 reviewer：
coverage_status：
validation_status：
round_test_mode：
test_mode：
test_mode_reason：
requirement_digest：
exit_test_count：
exit_test_status：
exit_test_skip_reason：
退出原因：
```

`修复项摘要` 每项包含 ID、严重度、位置、最终状态、执行 Skill、修复摘要和验证证据；没有修复项时写 `无已修复项`。高风险但未明确选择 `deep` 的对应项必须包含 `deep_not_requested`。退出测试发现的问题必须进入同一台账并保留来源和分类。`coverage_status=partial/uncovered` 时只能声明台账内没有未终结 finding，不得声明完整审查范围没有新的 actionable finding；`validation_status=failed/blocked` 或台账仍有未终结项时，`结果状态` 不得为 `complete`。

### 可复用结构化结果

无论 `supervisor_handoff=available` 还是 `required`，都在上述人类可读报告之外严格按 [references/handoff-contract.md](references/handoff-contract.md) 生成机器可读结果。`available` 不等待 sibling Skill 提供 schema；`required` 必须与调用方给定的同版 schema、完整 `requirements_snapshot` 和 `requirement_digest` 一致。结果遵守：

- 收敛时使用 `status=complete`、`exit_reason=converged`、`unresolved_count=0`。
- 第五个修复批次后的完整累计 review 仍有可修复 finding 时，使用 `status=incomplete`、`exit_reason=max_fix_rounds_reached`、`fix_rounds=5`；不得把本 Skill 的轮次耗尽伪装成 blocked。
- 返回最后一次已接受的完整累计 review 检查点，包括 `source=code-review-fix-loop`、`snapshot_contract=git-cumulative-diff-sha256-v1`、`review_complete`、`coverage_status`、冻结的 `review_baseline`、`reviewed_diff_hash`、完整 `changed_paths` 和当前 findings。`reviewed_diff_hash` 与 `changed_paths` 必须逐字复制本 Skill 在最终 review 后由 `workspace-snapshot.py` 生成的当前快照字段；内部的 `last_cumulative_review_diff_hash` 与 `current_diff_paths` 只能作为来源状态，不能以不同字段名输出。
- 只有检查点确实绑定当前工作区、`coverage_status=covered` 且 review 后没有持久化写入时，才可将其作为可复用检查点返回。无法满足时返回 `review_checkpoint=null` 和真实阻塞原因。
- finding 必须保留稳定 fingerprint、状态、严重度、位置和摘要；`unresolved_count` 必须等于所有待修复、需要决策、需要澄清和阻塞项的数量。
- 顶层必须携带本次 review 实际使用的完整 `requirements_snapshot` 及其 `requirement_digest`，使后续 Supervisor 能原样恢复需求基线；不得只输出 digest 或重新概括需求。
- 顶层必须携带 `alignment_directive`、`alignment_directive_status` 和 `alignment_resolution_evidence`。未收到指令时只能是 `null + not_applicable + null`；收到指令时逐字回显，状态只能是 `resolved|unresolved` 且证据非空。存在未解决指令时不得返回 `status=complete`，即使最后累计 review 没有 actionable finding。
- 中文台账状态到机器 finding 状态只能使用 handoff contract 的固定映射；`已修复` 与 `已修复-未验证` 只留在人类台账，不得伪装成当前未终结 finding。
- 已有上游完整 review 检查点、但所有 finding 经独立核对均不成立且没有持久化修改时，可以复用该检查点：必须保留其完整 fingerprint 集合、severity 和 location，只把对应状态改为 `not_actionable`、`not_introduced`、`out_of_scope` 或 `low_signal`；不得用空 findings 冒充重新 review 后的 clean 检查点。
- `test_mode`、`test_mode_reason`、`exit_test_count`、`exit_test_status`、`exit_test_skip_reason` 和 `validation_status` 仍只描述本次调用自己的退出验证，不得替上层汇总或改写。

这份交接允许上层在完全相同的 diff 状态下直接复用本 Skill 的累计 review；它不授权本 Skill 突破五轮上限。是否开启下一个五轮窗口由上层决定。
