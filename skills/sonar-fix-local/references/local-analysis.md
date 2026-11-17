# 本机分析操作

以下 `<skill-directory>` 指实际安装目录，`<project-root>` 指目标 Git 工作区。脚本不替用户选择测试命令，也不修改项目配置。

## 采集与启动

1. 核对项目 AGENTS.md、Git 状态、测试命令、依赖和隔离环境。对普通修复保留已有改动；只读诊断不修复。使用项目指定的 Node 版本。
2. 执行 `node <skill-directory>/scripts/local.mjs prepare <project-root>`。标准输出为唯一的个人运行目录，后续简称 `<run>`；源码快照包含 Git 已跟踪文件和非忽略的未跟踪文件，跳过已删除文件，拒绝环境凭据、符号链接及未支持语言。忽略的环境文件、node_modules 和 .git 不复制。
3. 执行 `node <skill-directory>/scripts/runtime.mjs start <run> [port]`。端口默认 19090，冲突时选择其他空闲高位端口，不停止已有服务。

`prepare` 只建立快照和配置，不获取 Sonar 运行锁；覆盖率阶段同样不获取锁。`start` 在首次异步操作前登记当前 run 的启动状态，核对本机 Docker context、Unix socket 和 daemon ID 后，按项目级 project key 原子创建锁目录。锁元数据写入原始项目身份、project key、run ID、运行目录、持有进程和创建时间。Git worktree 通过 canonical `git-common-dir` 共享锁，非 Git 项目使用 canonical project root，最终 key 为稳定 SHA-256 哈希。锁由本 Skill 的内部 holder 跨越独立的 start/scan/stop 进程持有。

同一项目已有活动锁时不会终止已有实例；当前 start 立即停止并报告已有 run ID、运行目录和按 run ID 核对的容器/网络状态。过期锁只有在持有进程退出、启动和扫描已确认结束、Docker 三项身份未变且对应资源已经不存在时才删除。进程退出但资源仍在时，只清理标签 `sonar-fix-local.run=<run-id>` 精确确认的资源；清理失败或 Docker context、socket、daemon 变化时保留锁和凭据。不同 project key 可以并发运行。

`prepare` 只 GET 线上配置，导出相关 Profile、门禁和分析范围设置，并下载线上 YAML 插件。`start` 使用导出的精确 SonarQube 版本、官方 Scanner 8.1.0 镜像及 nginx 本机固定代理，创建全新私有项目和独立本机凭据，导入配置并比对规则编号、数量、参数、类型、严重级别/impacts、分析器版本及门禁条件。规则配置是快照，不维护手工映射。`sonar.global.exclusions` 和 `sonar.global.test.exclusions` 按全局作用域导入本轮隔离实例，隐藏扫描参数 `sonar.filesize.limit` 通过 Scanner 传递，项目 properties 中的显式值优先，导出值及实际传入值分别记录于 `config/scanner-settings.json` 和 `scanner-settings-effective.json`；其余设置写入本机项目；线上继承标记不改变设置本身的作用域。多值设置按重复参数导入，随后读取本机有效值逐项比对；作用域或值不一致时拒绝继续。导出包含 `sonar.test.inclusions`；多条件忽略、文件内容忽略、代码块忽略和规则适用范围（`sonar.issue.ignore.multicriteria`、`sonar.issue.ignore.allfile`、`sonar.issue.ignore.block`、`sonar.issue.enforce.multicriteria`）无论通过 `value`、`values` 还是结构化 `fieldValues` 返回，都在未支持完整同步时停止。

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

覆盖率命令在原项目根目录执行，参数数组直接传给进程，不经过 shell。输出存入 `<run>/coverage.log`。工具检查命令退出成功、源码执行前后不变、报告新生成且有效；把宿主绝对路径归一为快照相对路径，拒绝不在源码清单中的 LCOV 文件。命令所需环境由当前进程提供，但 Sonar 凭据不传给测试进程。收到中断时先向本次覆盖率命令的独立进程组发送对应终止信号，1 秒宽限期后对该进程组发送 `SIGKILL`；即使 npm 等启动器先退出，也保留强杀计时器清理后代进程。Docker 命令同样使用异步、可取消的子进程，不阻塞中断处理；日志句柄在 `finally` 中关闭，随后统一进入当前 run 清理。

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

所有路径都调用同一个当前 run 清理入口：只删除本轮带匹配 label 的 Scanner、代理、Sonar 容器及网络，不使用 prune，不停止其他服务；确认容器、网络、临时凭据和项目锁都不存在后才报告清理成功。正常扫描先保存并校验 `result.json`，再由 `stop` 释放锁。覆盖率失败、扫描失败、Bridge 无响应、脚本抛错、进程中断和用户取消也进入同一清理流程。报告、覆盖率、日志、配置快照和源码快照保留，镜像和通用分析器缓存可供后续复用。服务初始化或中断不完整时同样清理，禁止重置未知实例密码。Docker 明确返回当前资源不存在时，清理可重复执行，兼容错误前缀的大小写差异；其他资源名、权限错误、连接失败或异常响应均停止清理并保留本机凭据。若 Docker 不可用导致清理失败，报告残留资源的本轮名称；不能声称已清理。

报告全量问题、待确认项、覆盖率、验证证据和快照漂移。没有覆盖率/热点问题时也如实说明，不要求用户确认不存在的事项。源码报告留在个人目录，不写业务项目的临时进度文档，不提交或推送。

过期锁回收与锁释放通过锁目录内的原子排他标记串行化；回收时在同一目录中发布新运行元数据，不先删除再重建项目锁。同一项目租约内的 `scan` 在异步操作前原子获取 `.scan-<holderToken>` 标记，持有到结果保存或异常清理结束；重复调用直接拒绝，不能清理已有扫描。正常返回在 `finally` 中释放扫描标记，`stop` 清理项目锁时也移除该标记；标记名包含随机租约标识，因此旧扫描的退出不会移除新租约的扫描标记，也不受其他 `start` 检查资源时持有 `.mutation` 的影响。HTTP 请求的超时和取消一直覆盖到正文读取结束；提前拒绝 HTTP 错误时也主动终止未消费的正文。`stop` 使用运行目录保存的项目身份，不要求原 worktree 仍然存在；Docker 三项身份和资源标签仍必须核验。

无法捕获的 `SIGKILL` 或断电可能在锁回收中留下 `.mutation` 排他标记。此时保留锁并明确报错，不自动抢占；须人工确认回收进程和 Docker 资源状态后恢复。扫描被强杀后遗留的 `.scan-<holderToken>` 标记在项目租约仍活动时不会自动抢占；若同时遗留 `.lifecycle` 操作状态，`stop` 会因无法确认子进程结束而保留现场；须人工核实后恢复，再重新准备运行。即使 holder 已退出，只要启动或扫描标记仍存在，也不能自动回收、移除标记或发布新租约。先显式执行 `stop`，通过统一入口请求取消并等待扫描及其子进程确认结束；无法确认时保留锁、资源和凭据。仅无待确认操作的过期锁可以继续按 Docker 身份和精确 run ID 核验回收。覆盖率工具若主动创建新的 session 逃离本次进程组，也不属于自动进程组终止能保证覆盖的边界。

内部 holder 使用随机租约标识自行检查锁是否仍属于自己，锁删除或换代后自行退出；清理不会向磁盘中记录的 PID 发送终止信号，避免 PID 复用误伤其他进程。公共数据与进程工具不依赖 CLI 入口，coverage/collect 的失败清理不会形成模块初始化循环。

统一清理入口在删除任何资源之前获取项目锁的修改权，持有到资源、凭据和项目锁清理结束；获取失败时不先删除资源。竞争 `start` 发现仍存活的 holder 时只读取资源状态并拒绝启动，不跨越 Docker 状态请求持有修改锁，避免阻断 `stop` 或异常清理。

`start` 和 `scan` 在首次异步操作前原子建立当前 run 的 `.lifecycle` 目录；启动时也覆盖尚未绑定 Docker 身份、尚未获取项目锁的预检阶段。统一清理先在该目录登记停止请求，等待启动或扫描及其子进程、HTTP 操作结束，再读取最新运行状态；整个清理期间保留该目录，拒绝新的同 run 启动和扫描。扫描也每 50 ms 检查停止请求，取消并等待尚未完成的 Scanner 创建命令，避免 `stop` 看到容器尚不存在就提前释放项目锁。操作状态未写完整、进程死亡但未确认子进程退出或等待超时时保留现场并报错；不能直接删除残留目录后重试。正常操作或清理结束后移除目录。

启动租约发布前另建立 `.startup-<holderToken>` 标记并记录启动进程号。清理遇到该标记时写入同租约的停止请求；启动流程每 50 ms 检查请求，取消自己的 Docker/HTTP 操作，并在这些操作结束后移除启动标记。清理持锁等待确认，随后统一删除资源、凭据和项目锁，避免预检或镜像拉取期间 `stop` 已返回成功而旧启动进程又创建容器。正常启动结束同样移除启动标记，保留项目锁供后续扫描使用；启动完成前拒绝 `scan`。

等待启动或扫描确认最多 10 秒；超时或操作进程已退出但未留下完成确认时，保留项目锁、资源和凭据并明确报错。不可捕获的强杀可能留下仍在执行的 Docker 子进程或请求，此时须人工核实运行现场，不能仅凭旧操作 PID 消失就自动抢占该租约。

启动、扫描、覆盖率和单独读取结果的自动异常清理均记录原操作的随机租约标识；原本没有租约也明确记录为空，不能据此清理后来新建的租约。异常清理在发送启动或扫描取消请求之前核对该标识，并在取得项目锁修改权后再次核对。租约发布与“核验并请求停止”共用 `.lifecycle/transition` 原子互斥；停止请求先到则禁止继续发布租约，租约先发布则清理重新核验归属。同一 run 停止并重新启动后，旧操作的失败不得取消新启动或删除替代租约的资源、凭据和项目锁。显式 `stop` 则清理调用时的当前 run。
