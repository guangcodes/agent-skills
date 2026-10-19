# 本机分析操作

以下 `<skill-directory>` 指实际安装目录，`<project-root>` 指目标 Git 工作区。脚本不替用户选择测试命令，也不修改项目配置。

## 采集与启动

1. 核对项目 AGENTS.md、Git 状态、测试命令、依赖和隔离环境。对普通修复保留已有改动；只读诊断不修复。使用项目指定的 Node 版本。
2. 执行 `node <skill-directory>/scripts/local.mjs prepare <project-root>`。标准输出为唯一的个人运行目录，后续简称 `<run>`；源码快照包含 Git 已跟踪文件和非忽略的未跟踪文件，跳过已删除文件，拒绝环境凭据、符号链接及未支持语言。忽略的环境文件、node_modules 和 .git 不复制。
3. 执行 `node <skill-directory>/scripts/runtime.mjs start <run> [port]`。端口默认 19090，冲突时选择其他空闲高位端口，不停止已有服务。

`prepare` 只 GET 线上配置，导出相关 Profile、门禁和分析范围设置，并下载线上 YAML 插件。`start` 使用导出的精确 SonarQube 版本、官方 Scanner 8.1.0 镜像及 nginx 本机固定代理，创建全新私有项目和独立本机凭据，导入配置并比对规则编号、数量、参数、类型、严重级别/impacts、分析器版本及门禁条件。规则配置是快照，不维护手工映射。`sonar.global.exclusions` 和 `sonar.global.test.exclusions` 按全局作用域导入本轮隔离实例，其余设置写入本机项目；线上继承标记不改变设置本身的作用域。多值设置按重复参数导入，随后读取本机有效值逐项比对；作用域或值不一致时拒绝继续。导出包含 `sonar.test.inclusions`；多条件忽略、文件内容忽略、代码块忽略和规则适用范围（`sonar.issue.ignore.multicriteria`、`sonar.issue.ignore.allfile`、`sonar.issue.ignore.block`、`sonar.issue.enforce.multicriteria`）无论通过 `value`、`values` 还是结构化 `fieldValues` 返回，都在未支持完整同步时停止。

镜像下载发生在挂载源码之前。Sonar 和 Scanner 仅连接 Docker internal 网络，没有外网默认路由；宿主 API 经 `127.0.0.1` 固定代理转发到本轮 Sonar 容器。代理不接受任意转发目标。线上 token 不传给 Docker。本机账号随本轮服务清理销毁。

Docker 当前 context 必须为本机 Unix socket，拒绝 SSH/TCP 远端。首次启动在创建资源前记录 context、有效 Unix endpoint 和 daemon ID；资源命令显式固定到该 endpoint。扫描和清理前重新核对三项身份，任一变化时停止并保留凭据；切回原 context/endpoint 且确认原 daemon 后才可重试。未记录归属的旧运行目录不自动绑定或清理，须人工核验残留资源；新分析使用新 run。找不到精确镜像、插件无权限、配置不支持或比对失败时停止，报告原因；不换成其他版本，不降低校验，不调用线上 Scanner。初次下载可能较慢；观察本轮 `pull-*.log`，不要以切换线上扫描或更改全局 Docker 配置解决下载问题。

## 本地覆盖率

从项目既有配置确定测试命令与 LCOV 路径，先验证测试服务隔离与依赖，确认命令没有生产访问或发布副作用。Skill 允许执行既有测试来生成覆盖率，无需把“生成报告”当成“补测试”另行确认。

推荐让报告写入个人目录；例如支持 Vitest 的 npm 项目：

```sh
node <skill-directory>/scripts/local.mjs coverage <run> <run>/coverage/lcov.info -- npm run test:coverage -- --coverage.reportsDirectory=<run>/coverage
```

上述只是示例：按实际包管理器及覆盖率工具替换命令，不给不支持 Vitest 参数的工具追加选项。若工具仅支持项目内报告，使用项目已有的被忽略输出路径，例如：

```sh
node <skill-directory>/scripts/local.mjs coverage <run> coverage/lcov.info -- npm run test:coverage
```

覆盖率命令在原项目根目录执行，参数数组直接传给进程，不经过 shell。输出存入 `<run>/coverage.log`。工具检查命令退出成功、源码执行前后不变、报告新生成且有效；把宿主绝对路径归一为快照相对路径，拒绝不在源码清单中的 LCOV 文件。命令所需环境由当前进程提供，但 Sonar 凭据不传给测试进程。

同一 run 再次生成覆盖率时，工具会先使旧凭证失效，并记录旧 LCOV 的文件身份、大小及纳秒 mtime，仅权限变化造成的 ctime 更新不算重新生成。即使旧文件刚刚生成，只要本次命令没有重写或替换它就拒绝导入；相同内容重新写入或原子替换可以通过。粗粒度文件系统无法证明变化时保守拒绝。命令失败、无法启动或报告校验失败后，Scanner 都不能继续使用旧 LCOV。只有本次生成及全部校验成功后才重建凭证。

首次必须生成覆盖率。只有同一次调用中 `<run>/coverage-receipt.json` 对应快照且 LCOV 哈希仍一致时，才复用本轮报告。其他旧报告没有可验证凭证，不以文件存在或时间较新代替源码匹配。源码改变时重建 run，不能手改指纹或伪造 receipt。

## 扫描与读取

```sh
node <skill-directory>/scripts/runtime.mjs scan <run>
```

Scanner 只读挂载源码与原项目 node_modules，写入个人缓存/工作目录。扫描前检查本轮容器归属、内部网络、配置比对、源码指纹和覆盖率哈希；命令行强制本机地址、独立 project key 和本机 LCOV 路径。保留项目原有 exclusions，仅额外排除个人快照 Git 元数据。

文本/密钥检测同步 `sonar.text.activate`、`sonar.text.inclusions.activate`、`sonar.text.inclusions` 和 `sonar.text.excluded.file.suffixes`，导入后按原始 scalar/数组形态读回校验。Kotlin 的 `.kt`、`.kts` 均不在支持范围，采集阶段直接拒绝。

分析器设置按已支持 Profile 对应的 `sonar.<analyzer>.` 前缀导出，包括 JS 文件大小上限、文件后缀、启用开关和文本检测范围；公共范围设置另包含 `sonar.filesize.limit`、`sonar.sourceEncoding`。不保存服务器、认证和其他语言的配置。JSON/YAML 输入还可能触发 CloudFormation、Kubernetes、Azure Resource Manager、Ansible、GitHub Actions、Azure Pipelines 或 JSON Profile；线上存在时一并备份、恢复并核验规则，仍要求 IaC 插件版本一致，不使用本机默认 Profile 代替。源码接收类型不扩大，Bicep 与 Terraform 等未支持输入仍拒绝。

本机仅导入有凭证的 LCOV 覆盖率，命令行显式清空 `sonar.coverageReportPaths`、`sonar.testExecutionReportPaths` 和旧的 `sonar.typescript.lcov.reportPaths`，避免继承指向未复制、陈旧或未验证报告的路径。测试执行 XML 不导入，因此不提供测试数量、时长或失败数；`result.json` 中 `testExecutionReportsImported=false` 明确记录此边界。

容器上限 4 GB、Node heap 2560 MB、Java heap 512 MB 是本机有限验证通过的起点，Sonar 容器上限 3 GB。先核对实际 Docker 可用内存；不足时报告环境限制，不停止其他服务或改全局资源配置。amd64 Scanner 在 ARM 宿主上使用模拟，可能较慢。

等待本次 CE task 最多两分钟。成功后只读取该 analysisId 的结果，完整分页读取全量未解决 Issues 和待审 Hotspots，保留规则号/文件/行号。结果位于 `<run>/result.json`，包含原始 Gate，但 `newCodeGateEvaluated` 固定为 false。本版本不建立新代码基线，原始 `OK` 不表示新代码门禁通过。

退出码：0 为本机分析成功且源码仍匹配；2 为分析完成但工作区已经变化，只能说明快照结果；1 为环境、同步、覆盖率或分析失败。失败不能当成零问题，也不回退到旧线上结果来证明当前代码。

普通修复前检查 `node <skill-directory>/scripts/local.mjs check <run>` 和目标代码位置。检查与修复间仍可能发生并行编辑，应按实际文件核验，不把旧行号机械套入新代码。修复后执行相关验证；需复扫时用新 run，最多一次，不改动旧快照。

## 清理与交付

无论成功、失败或用户中断，都执行：

```sh
node <skill-directory>/scripts/runtime.mjs stop <run>
```

只删除本轮带匹配 label 的 Scanner、代理、Sonar 容器及网络，不使用 prune，不停止其他服务；删除本轮本机明文凭据，保留私有结果/日志/快照，镜像和通用分析器缓存可供后续复用。服务初始化或中断不完整时同样清理，禁止重置未知实例密码。Docker 明确返回当前资源不存在时，清理可重复执行，兼容错误前缀的大小写差异；其他资源名、权限错误、连接失败或异常响应均停止清理并保留本机凭据。若 Docker 不可用导致清理失败，报告残留资源的本轮名称；不能声称已清理。

报告全量问题、待确认项、覆盖率、验证证据和快照漂移。没有覆盖率/热点问题时也如实说明，不要求用户确认不存在的事项。源码报告留在个人目录，不写业务项目的临时进度文档，不提交或推送。
