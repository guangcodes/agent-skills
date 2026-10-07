# agent-skills

个人 Agent Skill 的统一源码仓库。仓库采用轻量 monorepo：Skill 本体保持自包含，bundle 只描述组合关系，harness adapter 负责生成各平台可安装的产物。

## 当前 Skill

| Skill | 用途 | 可移植性 |
| --- | --- | --- |
| `code-review-fix-loop` | 对本地 review finding 执行有界的核验、修复、完整累计 diff review 与退出验证循环 | Codex-specific |
| `review-fix-alignment-supervisor` | 在修复不收敛时静态检查完整 diff 的需求对齐、功能缺口、设计发散与最小充分性 | Codex-specific |
| `bounded-review-fix-supervisor` | 在显式调用且存在 active Goal 时，经纠偏检查复用 Review 检查点并串联最多四个五轮修复窗口 | Codex-specific |
| `submit-pr-mr` | 显式触发后按项目规则验证、提交并创建 PR/MR，再做只读审查 | Codex-specific |

这些 Skill 依赖 Codex 的 review、Goal、Git 和平台 CLI 能力，所以暂不宣称跨 harness 可直接运行。未来的可移植 Skill 仍放在 `skills/`；平台差异放在 `adapters/<harness>/`，不要把平台分支复制回 Skill 本体。

## 目录

```text
skills/                         唯一源码，每个目录都是自包含 Skill
catalog/skills.json             Skill 来源、分类、依赖与可移植性
catalog/bundles.json            可独立发布的组合与版本
adapters/codex/*                Codex Plugin manifest 源文件
tooling/                        校验、打包与本机链接安装工具
dist/                           生成产物，不提交 Git
```

## 日常操作

```bash
./tooling/validate.sh
python3 tooling/package_codex_plugin.py review-workflows
python3 tooling/package_codex_plugin.py delivery-workflows
python3 tooling/install_skill_links.py
```

`install_skill_links.py` 默认把 `skills/*` 链接到官方用户级目录 `~/.agents/skills`，遇到非本仓库管理的同名文件会停止，不会覆盖。当前名称全部链接成功后，它会按 catalog 的 `renamed_from` 元数据移除仍指向本仓库旧目录的废弃链接，并保留任何用户自有的同名路径。Codex Plugin 产物按 bundle 生成到 `dist/codex/<bundle>`，其中包含真实文件而不是逃逸出 Plugin 根目录的链接。

## 发布边界

- `skills/` 是唯一可编辑源码；不要直接修改 `dist/`。
- bundle 各自版本化，不要求整个仓库中的所有 Skill 同步发布。
- 本项目使用 [MIT License](LICENSE)；创建公开 GitHub 仓库后再补充仓库地址。
- 提交或发布前运行 `./tooling/validate.sh`。
