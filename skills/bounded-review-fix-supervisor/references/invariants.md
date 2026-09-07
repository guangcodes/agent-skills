# Bounded Review-Fix Safety Invariants

本文件是 review surface 的统一语义定义；不得用新增关键词特例绕过这些不变量。

## 统一 Git 快照

- 路径阶段只读取 Git 元数据，固定 `HEAD`、staged、unstaged、untracked 四部分。
- staged/unstaged 路径与 binary diff 一律使用 `--no-renames`，让 rename 两端同时进入范围与敏感筛查。
- 内容哈希包含 HEAD、staged binary diff、unstaged binary diff 和 untracked metadata；两类 diff 只能使用已筛查快照生成的 literal pathspec 读取，不得再次读取无路径边界的实时全量 diff。
- 同一路径快照必须连续计算两次完整内容摘要，并在两次摘要间及第二次摘要后重新采集完整路径；任一摘要、HEAD、index、worktree 或 untracked 集合变化均失败，不能接受同路径内容变化形成的混合快照。
- 所有 Git 子进程以及可能调用 Git 的 reviewer 子进程都必须禁用 lazy fetch 和终端凭据提示，缺失对象或认证问题应快速失败而不是隐式联网或挂起。

## Review surface 分类

- 任一路径组件命中 `.env*`、认证配置目录/结构、私钥扩展名或精确凭据文件名即拒绝。
- 文件名先拆成规范化语义 token，再按 credential noun、credential qualifier 和组合关系判断；不得使用无边界子串匹配。
- `tokenizer`、`token_bucket`、`secret_parser` 等业务源码必须可审查；`tokens`、`access_tokens`、`session_tokens`、`client_secret` 等凭据语义必须拒绝。
- untracked 默认拒绝，只允许 `S_ISREG` 普通文件；symlink、FIFO、socket、block/character device 和目录全部拒绝。
- 尚存在于工作树中的 changed tracked path 也必须是 `S_ISREG` 普通文件；删除的 tracked path 可以缺失，symlink 和其他特殊类型必须在读取内容前拒绝。
- untracked hash 永不读取文件内容。

## Reviewer 输出

- 初始 reviewer 使用自定义完整 diff 指令，而不是把 `--uncommitted` 与自定义指令同时传给 CLI；指令必须明确只做静态审查并禁止所有自动化测试和验证命令。
- JSON schema 是主协议，必须有 `coverage_complete: true` 和合法 findings。
- Markdown 只作为兼容 fallback：coverage caveat 必须是独立句子，并以 reviewer、review、coverage、diff 范围或明确中文审查主体开头。
- 产品缺陷文本中的 `cannot review`、`cannot read`、`用户无法审查` 不得被当作 coverage caveat。
- clean 必须断言当前完整范围没有 actionable finding；`no new issues` 不证明旧 finding 已解决。

## 跨窗口 Review 检查点

- 初始完整 review 之后，Supervisor 不得对相同 diff 重复启动 reviewer；子 Skill 的最后累计 review 是下一状态转换的唯一正常检查点。
- campaign 启动前刚完成的 `code-review-fix-loop` 结果若与当前 baseline、完整路径和 diff hash 精确匹配，应通过 `adopt-fix-result` 直接接管并计为第一个窗口；不得在相同状态重复初审。
- 只有 `source`、`snapshot_contract=git-cumulative-diff-sha256-v1`、完整覆盖、冻结 baseline、changed paths 和 diff hash 全部匹配当前状态时才能接纳检查点。子 Skill 用自带助手生成，父级用同版算法独立复算；两者都覆盖 baseline 到 HEAD 的 committed paths，显式用 `--ignore-submodules=none` 覆盖本地忽略配置，并拒绝 changed gitlink 或 dirty submodule。
- 缺失或畸形的子检查点必须持久化为 stopped/blocked；协议错误不得只返回瞬时退出码后把 campaign 留在 fixing。
- 零持久化修改只能复用输入检查点并重分类：finding fingerprint 集合、severity 和 location 必须保持不变，不能用空 findings 冒充 clean review。
- 子结果跳过退出验证时必须给出与测试模式、修复轮次和停止状态一致的标准 `exit_test_skip_reason`；有持久化修改不能用空原因绕过默认 light。
- 子 Skill 达到五轮上限不等于整个 campaign 失败；仅当退出原因为 `max_fix_rounds_reached` 且剩余项全部可自动修复时，才可进入下一窗口。
- 五轮上限后的有效子检查点只允许进入 `alignment_required`；在 `review-fix-alignment-supervisor` 返回 `continue` 或 `redirect` 前不得开始额外窗口。
- Alignment 必须复用同一子检查点、该检查点完整 object 的规范摘要和当前 diff hash，只检查需求对齐、功能完整性、设计收敛与最小充分性；finding 台账变化后旧结论不得复用，也不得用它重做 bug review 或运行测试。
- `redirect` 指令必须进入下一窗口输入，并由子结果逐字回显处理状态与证据；带指令的 clean 结果只有明确证明 `resolved` 才能完成。`ask_developer` 与 `blocked` 必须停止整个 campaign。
- 检查点缺失、过期或不完整时 fail closed，不得由父级补写成功字段或用隐式重复 review 掩盖协议失败。
- `max_fix_windows` 约束实际子 Skill 调用窗口；达到上限后，即使仍有 finding 也不得继续。

## 参数化验证矩阵

| 维度 | 必须覆盖 |
|---|---|
| 路径 | 根目录/嵌套、rename 两端、环境与认证结构、凭据语义、业务近似词 |
| Git 状态 | staged、unstaged、staged+unstaged、untracked、HEAD 变化 |
| 文件类型 | regular、symlink、directory、FIFO、socket、block/character device |
| 输出 | JSON complete/incomplete、Markdown finding/clean/caveat、英文/中文产品文本 |
