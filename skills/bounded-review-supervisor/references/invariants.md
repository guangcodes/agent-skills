# Supervisor Safety Invariants

本文件是 review surface 的统一语义定义；不得用新增关键词特例绕过这些不变量。

## 统一 Git 快照

- 路径阶段只读取 Git 元数据，固定 `HEAD`、staged、unstaged、untracked 四部分。
- staged/unstaged 路径与 binary diff 一律使用 `--no-renames`，让 rename 两端同时进入范围与敏感筛查。
- 内容哈希包含 HEAD、staged binary diff、unstaged binary diff 和 untracked metadata；两类 diff 只能使用已筛查快照生成的 literal pathspec 读取，不得再次读取无路径边界的实时全量 diff。
- 哈希后重新采集完整路径快照；HEAD、index、worktree 或 untracked 集合变化均失败。

## Review surface 分类

- 任一路径组件命中 `.env*`、认证配置目录/结构、私钥扩展名或精确凭据文件名即拒绝。
- 文件名先拆成规范化语义 token，再按 credential noun、credential qualifier 和组合关系判断；不得使用无边界子串匹配。
- `tokenizer`、`token_bucket`、`secret_parser` 等业务源码必须可审查；`tokens`、`access_tokens`、`session_tokens`、`client_secret` 等凭据语义必须拒绝。
- untracked 默认拒绝，只允许 `S_ISREG` 普通文件；symlink、FIFO、socket、block/character device 和目录全部拒绝。
- untracked hash 永不读取文件内容。

## Reviewer 输出

- JSON schema 是主协议，必须有 `coverage_complete: true` 和合法 findings。
- Markdown 只作为兼容 fallback：coverage caveat 必须是独立句子，并以 reviewer、review、coverage、diff 范围或明确中文审查主体开头。
- 产品缺陷文本中的 `cannot review`、`cannot read`、`用户无法审查` 不得被当作 coverage caveat。
- clean 必须断言当前完整范围没有 actionable finding；`no new issues` 不证明旧 finding 已解决。

## 参数化验证矩阵

| 维度 | 必须覆盖 |
|---|---|
| 路径 | 根目录/嵌套、rename 两端、环境与认证结构、凭据语义、业务近似词 |
| Git 状态 | staged、unstaged、staged+unstaged、untracked、HEAD 变化 |
| 文件类型 | regular、symlink、directory、FIFO、socket、block/character device |
| 输出 | JSON complete/incomplete、Markdown finding/clean/caveat、英文/中文产品文本 |
