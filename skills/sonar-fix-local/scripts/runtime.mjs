import fs from "node:fs";
import path from "node:path";
import { runChild } from "./process.mjs";
import { pathToFileURL } from "node:url";
import { parseProperties, serializeCsvValues, scannerSettings } from "./properties.mjs";
import { read, save } from "./state.mjs";
import { interruptionError, throwIfAborted, waitWithSignal, timeoutSignal } from "./process.mjs";
import {
  projectIdentity,
  projectKey,
  acquireProjectLock,
  watchStartup,
  acquireScanLock,
  assertProjectLock,
  readProjectLock,
  claimProjectCleanup,
  claimRunStartup,
  claimRunScan,
  claimRunCleanup,
  lockRootFor,
  captureRunLease,
} from "./lock.mjs";

const SCANNER = "sonarsource/sonar-scanner-cli:12.2.0.4256_8.1.0";
function dockerEnvironment(host) {
  const env = { ...process.env };
  delete env.SONAR_TOKEN;
  delete env.SONAR_HOST_URL;
  if (host) {
    delete env.DOCKER_HOST;
    delete env.DOCKER_CONTEXT;
  }
  return env;
}
async function docker(args, host, signal) {
  const result = await runChild("docker", host ? ["--host", host, ...args] : args, {
    env: dockerEnvironment(host), signal, timeoutMs: 30000,
  });
  if (result.code !== 0) {
    const error = Error("Docker 操作失败");
    error.status = result.code;
    error.stderr = result.stderr;
    throw error;
  }
  return result.stdout;
}
async function logged(args, log, host, signal) {
  throwIfAborted(signal);
  const fd = fs.openSync(log, "w", 0o600);
  try {
    const { code } = await runChild("docker", host ? ["--host", host, ...args] : args, {
      env: dockerEnvironment(host), stdio: ["ignore", fd, fd], signal,
    });
    if (code !== 0) throw Error("Docker 操作失败；检查个人目录日志：" + path.basename(log));
  } finally {
    fs.closeSync(fd);
  }
}
async function stateFor(run, bind = false, signal, cleanup = false) {
  if (process.env.DOCKER_HOST && !process.env.DOCKER_HOST.startsWith("unix://"))
    throw Error("拒绝 DOCKER_HOST 指向远端 Docker");
  const s = read(path.join(run, "state.json"));
  const identity = cleanup && s.projectIdentity ? s.projectIdentity : projectIdentity(s.root);
  const key = projectKey(identity);
  if (s.projectIdentity && s.projectIdentity !== identity)
    throw Error("运行目录项目身份与当前工作区不一致");
  if (s.projectKey && s.projectKey !== key)
    throw Error("运行目录项目 key 与当前工作区不一致");
  // Older runs did not persist the project lock identity.  Deriving it is
  // safe because the run root is already stored in state.json; no resource is
  // claimed until the Docker ownership checks below pass.
  if (!s.projectIdentity || !s.projectKey) {
    s.projectIdentity = identity;
    s.projectKey = key;
    save(path.join(run, "state.json"), s);
  }
  if (
    !/^[a-f0-9]{16}$/.test(s.id) ||
    s.network !== `sfl-${s.id}` ||
    s.server !== `sfl-${s.id}-server` ||
    s.proxy !== `sfl-${s.id}-access` ||
    s.scanner !== `sfl-${s.id}-scanner` ||
    s.project !== `sonar-fix-local-${s.id}` ||
    s.image !== `sonarqube:${s.version}-community` ||
    !/^\d+\.\d+\.\d+\.\d+$/.test(s.version)
  )
    throw Error("无效运行标识");
  if (!s.docker && (!bind || !Object.hasOwn(s, "docker")))
    throw Error(
      "运行目录未记录 Docker 归属；停止操作并保留本机凭据，请使用新 run",
    );
  const { context, host } = await readDockerContext(signal);
  if (!host?.startsWith("unix://"))
    throw Error("必须使用本机 Unix socket Docker context，拒绝远端 Docker");
  const daemonId = (await docker(["info", "--format", "{{.ID}}"], host, signal)).trim();
  if (!daemonId || /\s/.test(daemonId))
    throw Error("无法确认 Docker daemon 身份；停止操作并保留本机凭据");
  if (s.docker) {
    if (
      s.docker.context !== context ||
      s.docker.host !== host ||
      s.docker.daemonId !== daemonId
    )
      throw Error(
        "Docker context、endpoint 或 daemon 已变化；停止操作并保留本机凭据",
      );
  } else {
    // 在创建任何容器或网络之前落盘；后续操作不能随宿主 context 漂移。
    s.docker = { context, host, daemonId };
    save(path.join(run, "state.json"), s);
  }
  return s;
}
async function readDockerContext(signal) {
  const context = (await docker(["context", "show"], undefined, signal)).trim();
  const configuredHost = JSON.parse(await docker(["context", "inspect", context], undefined, signal))[0]
    .Endpoints.docker.Host;
  // DOCKER_CONTEXT 优先于 DOCKER_HOST，与 Docker CLI 的环境变量优先级一致。
  const host = process.env.DOCKER_CONTEXT
    ? configuredHost
    : process.env.DOCKER_HOST || configuredHost;
  return { context, host };
}
async function runtimeFor(run, bind = false, signal, cleanup = false) {
  const s = await stateFor(run, bind, signal, cleanup);
  return boundRuntime(s, signal);
}
function boundRuntime(s, signal) {
  return {
    s,
    docker: (args) => docker(args, s.docker.host, signal),
    logged: (args, log, signal) => logged(args, log, s.docker.host, signal),
    owned: (name, network = false) => owned(name, s.id, network, s.docker.host, signal),
  };
}
async function owned(name, id, network, host, signal) {
  try {
    const data = JSON.parse(
      await docker(network ? ["network", "inspect", name] : ["inspect", name], host, signal),
    )[0];
    return (
      (network ? data.Labels : data.Config.Labels)?.["sonar-fix-local.run"] ===
      id
    );
  } catch (error) {
    // inspect 的连接失败、超时和异常响应不能视为资源已消失。
    // Docker CLI 的错误前缀存在大小写差异；仍要求完整消息与当前资源名匹配。
    if (missingDockerError(error, name))
      return false;
    throw Error(`无法检查本轮 Docker 资源 ${name}；停止操作并保留本机凭据`);
  }
}

function missingDockerError(error, name) {
  return (
    error.status === 1 &&
    new Set([
      `error: no such object: ${name}`,
      `error: no such container: ${name}`,
      `error: no such network: ${name}`,
      `error response from daemon: no such container: ${name}`,
      `error response from daemon: network ${name} not found`,
    ]).has(error.stderr?.toString().trim().toLowerCase())
  );
}

function resourceNames(id) {
  if (!/^[a-f0-9]{16}$/.test(id)) throw Error("无效运行标识");
  return {
    scanner: `sfl-${id}-scanner`,
    proxy: `sfl-${id}-access`,
    server: `sfl-${id}-server`,
    network: `sfl-${id}`,
  };
}

async function inspectResource(name, network, docker) {
  try {
    const data = JSON.parse(
      await docker(network ? ["network", "inspect", name] : ["inspect", name]),
    )[0];
    if (!data || typeof data !== "object") throw Error("invalid inspect");
    const labels = network ? data.Labels : data.Config?.Labels;
    return { state: "present", owned: labels?.["sonar-fix-local.run"] };
  } catch (error) {
    if (missingDockerError(error, name))
      return { state: "absent" };
    throw Error(`无法检查资源 ${name}；保留当前项目锁和凭据`);
  }
}

async function resourceStatusForRun(id, docker) {
  const names = resourceNames(id);
  const details = {
    server: await inspectResource(names.server, false, docker),
    proxy: await inspectResource(names.proxy, false, docker),
    scanner: await inspectResource(names.scanner, false, docker),
    network: await inspectResource(names.network, true, docker),
  };
  return {
    hasResources: Object.values(details).some((item) => item.state !== "absent"),
    details,
  };
}

async function cleanupResourcesForRun(id, docker) {
  const names = resourceNames(id);
  const status = await resourceStatusForRun(id, docker);
  for (const [kind, name] of Object.entries(names)) {
    const item = status.details[kind];
    if (item.state === "absent") continue;
    if (item.owned !== id)
      throw Error(`资源 ${name} 未确认属于当前 run，拒绝删除`);
    try {
      if (kind === "network") await docker(["network", "rm", name]);
      else await docker(["rm", "-f", name]);
    } catch (error) {
      if (!missingDockerError(error, name)) throw error;
    }
  }
  const after = await resourceStatusForRun(id, docker);
  if (after.hasResources)
    throw Error(`当前 run 资源清理后仍有残留：${JSON.stringify(after.details)}`);
}

export async function start(run, port = "19090", signal) {
  const operation = claimRunStartup(run, signal);
  signal = operation.signal;
  let lockAcquired = false;
  let startup;
  let holderToken;
  try {
    const { bootstrap } = await import("./local.mjs");
    throwIfAborted(signal);
    let { s, docker, logged } = await runtimeFor(run, true, signal);
    throwIfAborted(signal);
    const lockRoot = lockRootFor(s);
    if (!/^\d+$/.test(port) || +port < 1024 || +port > 65535)
      throw Error("无效本机端口");
    const lease = await acquireProjectLock(
      {
        projectIdentity: s.projectIdentity,
        projectKey: s.projectKey,
        runId: s.id,
        runDir: run,
        createdAt: s.capturedAt,
        docker: s.docker,
      },
      {
        currentDocker: s.docker,
        resourceStatus: (existing) =>
          resourceStatusForRun(existing.runId, docker),
        cleanupResources: async (existing) => {
          await cleanupResourcesForRun(existing.runId, docker);
          cleanupStaleRunFiles(existing);
        },
        cleanupStale: cleanupStaleRunFiles,
        lockRoot,
        starting: true,
        claimPublication: () => operation.claimPublication(),
      },
    );
    lockAcquired = true;
    holderToken = lease.metadata.holderToken;
    startup = watchStartup(lease, signal);
    signal = startup.signal;
    ({ docker, logged } = boundRuntime(s, signal));
    throwIfAborted(signal);
    s.lockRoot = lockRoot;
    save(path.join(run, "state.json"), s);
    const existingResources = await resourceStatusForRun(s.id, docker);
    if (existingResources.hasResources)
      throw Error(
        `当前 run 已存在资源，拒绝覆盖：${JSON.stringify(existingResources.details)}`,
      );
    for (const image of [s.image, SCANNER, "nginx:1.28-alpine"]) {
      try {
        await docker(["image", "inspect", image]);
      } catch {
        await logged(
          [
            "pull",
            ...(image === SCANNER ? ["--platform", "linux/amd64"] : []),
            image,
          ],
          path.join(
            run,
            "pull-" +
              (image === SCANNER
                ? "scanner"
                : image.startsWith("nginx")
                  ? "proxy"
                  : "server") +
              ".log",
          ),
          signal,
        );
      }
    }
    throwIfAborted(signal);
    const label = ["--label", `sonar-fix-local.run=${s.id}`];
    await docker(["network", "create", "--internal", ...label, s.network]);
    const network = JSON.parse(await docker(["network", "inspect", s.network]))[0];
    if (!network.Internal) throw Error("必须使用内部网络");
    const plugin = fs.existsSync(path.join(run, "yaml.jar"))
      ? [
          "-v",
          `${run}/yaml.jar:/opt/sonarqube/extensions/plugins/sonar-yaml-plugin.jar:ro`,
        ]
      : [];
    await docker([
      "run",
      "-d",
      "--rm",
      "--name",
      s.server,
      ...label,
      "--network",
      s.network,
      "--memory",
      "3g",
      "--cpus",
      "4",
      "-e",
      "SONAR_ES_BOOTSTRAP_CHECKS_DISABLE=true",
      "-e",
      "SONAR_WEB_JAVAOPTS=-Xms128m -Xmx512m",
      "-e",
      "SONAR_CE_JAVAOPTS=-Xms128m -Xmx512m",
      "-e",
      "SONAR_SEARCH_JAVAOPTS=-Xms512m -Xmx512m",
      ...plugin,
      s.image,
    ]);
    // 使用本轮容器 IP，避免代理接入内部网络前发生 DNS 初始化竞争。
    const ip = JSON.parse(await docker(["inspect", s.server]))[0].NetworkSettings
      .Networks[s.network].IPAddress;
    if (!/^\d+\.\d+\.\d+\.\d+$/.test(ip)) throw Error("无效本机容器 IP");
    // 内部网络不发布宿主端口；代理只转发到固定的本机容器，不接受目标 URL。
    fs.writeFileSync(
      path.join(run, "nginx.conf"),
      `events {}\nhttp { access_log off; server { listen 9000; client_max_body_size 100m; location / { proxy_pass http://${ip}:9000; proxy_read_timeout 300s; } } }\n`,
      { mode: 0o600 },
    );
    await docker([
      "run",
      "-d",
      "--rm",
      "--name",
      s.proxy,
      ...label,
      "--network",
      "bridge",
      "--memory",
      "64m",
      "-p",
      `127.0.0.1:${port}:9000`,
      "-v",
      `${run}/nginx.conf:/etc/nginx/nginx.conf:ro`,
      "nginx:1.28-alpine",
    ]);
    await docker(["network", "connect", s.network, s.proxy]);
    await docker(["restart", s.proxy]);
    const origin = `http://127.0.0.1:${port}`;
    const deadline = Date.now() + 120000;
    while (Date.now() < deadline) {
      throwIfAborted(signal);
      try {
        const request = timeoutSignal(signal, 3000);
        let status;
        try {
          const r = await fetch(origin + "/api/system/status", {
            redirect: "error",
            signal: request.signal,
          });
          status = await r.json();
        } finally {
          request.dispose();
        }
        if (status.status === "UP") {
          await bootstrap(run, origin, signal);
          throwIfAborted(signal);
          return;
        }
      } catch (e) {
        if (signal?.aborted) throw interruptionError(signal);
        if (fs.existsSync(path.join(run, "pending-password.json"))) throw e;
      }
      await waitWithSignal(1500, signal);
    }
    throw Error("本机服务启动超过两分钟，停止本轮；检查本次容器日志");
  } catch (error) {
    // Acknowledge that every startup command has settled before cleanup can
    // release the lease. A competing stop owns its own cleanup transaction.
    const leaseStopped = startup?.finish();
    const stoppedElsewhere = operation.finish() || leaseStopped;
    if (lockAcquired && !stoppedElsewhere) {
      try {
        await cleanupRun(run, { holderToken });
      } catch (cleanupError) {
        throw Error(`${error.message}；当前 run 清理失败：${cleanupError.message}`);
      }
    }
    throw error;
  } finally {
    startup?.finish();
    operation.finish();
  }
}

function removeTemporaryCredentials(run) {
  for (const name of [
    "scanner.env",
    "credentials.json",
    "pending-password.json",
    "nginx.conf",
  ])
    fs.rmSync(path.join(run, name), { force: true });
  const residual = [
    "scanner.env",
    "credentials.json",
    "pending-password.json",
    "nginx.conf",
  ].filter((name) => fs.existsSync(path.join(run, name)));
  if (residual.length)
    throw Error(`临时凭据或代理配置清理后仍有残留：${residual.join(",")}`);
}

function cleanupStaleRunFiles(lockMetadata) {
  if (!fs.existsSync(lockMetadata.runDir)) return;
  const stateFile = path.join(lockMetadata.runDir, "state.json");
  if (!fs.existsSync(stateFile))
    throw Error(`过期锁运行目录缺少 state.json，保留锁：${lockMetadata.runDir}`);
  const state = read(stateFile);
  if (
    state.id !== lockMetadata.runId ||
    (state.projectKey && state.projectKey !== lockMetadata.projectKey)
  )
    throw Error(`过期锁运行目录身份不匹配，保留锁：${lockMetadata.runDir}`);
  removeTemporaryCredentials(lockMetadata.runDir);
}

/**
 * The one cleanup entry point used by start failures, scan failures,
 * coverage failures, interruption handlers and `runtime.mjs stop`.
 */
export async function cleanupRun(run, { holderToken } = {}) {
  if (process.env.DOCKER_HOST && !process.env.DOCKER_HOST.startsWith("unix://"))
    throw Error("拒绝 DOCKER_HOST 指向远端 Docker");
  const operation = claimRunCleanup(run, { beforeStop() {
    if (holderToken === undefined) return;
    if (captureRunLease(run).holderToken !== holderToken)
      throw Error("项目锁租约已变化；保留新运行的资源和凭据");
  } });
  try {
    await operation.waitForStartup();
    await cleanupSettledRun(run, holderToken);
  } finally {
    operation.dispose();
  }
}

async function cleanupSettledRun(run, holderToken) {
  const raw = read(path.join(run, "state.json"));
  const key = raw.projectKey || projectKey(projectIdentity(raw.root));
  const lockRoot = lockRootFor(raw);
  const lock = readProjectLock(key, lockRoot);
  const credentials = [
    "scanner.env",
    "credentials.json",
    "pending-password.json",
  ].some((name) => fs.existsSync(path.join(run, name)));
  if (!raw.docker) {
    if (lock)
      throw Error("运行目录未记录 Docker 归属；无法安全清理项目锁和资源");
    if (credentials)
      throw Error("运行目录未记录 Docker 归属；保留本机凭据");
    return;
  }
  const { s, docker } = await runtimeFor(run, false, undefined, true);
  const claim = claimProjectCleanup(
    {
      projectIdentity: s.projectIdentity,
      projectKey: s.projectKey,
      runId: s.id,
      runDir: run,
      docker: s.docker,
      holderToken,
    },
    { lockRoot, currentDocker: s.docker },
  );
  try {
    await claim.waitForStartup();
    await cleanupResourcesForRun(s.id, docker);
    removeTemporaryCredentials(run);
    claim.release();
  } finally {
    claim.dispose();
  }
}

export async function scan(run, signal) {
  const raw = read(path.join(run, "state.json"));
  const identity = raw.projectIdentity || projectIdentity(raw.root);
  // Rejection is outside the cleanup catch: this invocation has not claimed
  // the active scan and must never tear down its resources, even on abort.
  const releaseScan = acquireScanLock({
    projectIdentity: identity,
    projectKey: raw.projectKey || projectKey(identity),
    runId: raw.id,
    runDir: run,
    docker: raw.docker,
  }, { lockRoot: lockRootFor(raw), currentDocker: raw.docker });
  let operation;
  try { operation = claimRunScan(run, signal); }
  catch (error) { releaseScan?.(); throw error; }
  signal = operation.signal;
  let cleanupOnError = false;
  try {
    const { checkSnapshot, collectLocal, localClient } = await import("./local.mjs");
    throwIfAborted(signal);
    const { s, docker, logged, owned } = await runtimeFor(run, false, signal);
    throwIfAborted(signal);
    const lockRoot = lockRootFor(s);
    const hasLock = Boolean(readProjectLock(s.projectKey, lockRoot));
    if (hasLock) {
      assertProjectLock(
        {
          projectIdentity: s.projectIdentity,
          projectKey: s.projectKey,
          runId: s.id,
          runDir: run,
          docker: s.docker,
          holderToken: releaseScan?.holderToken ?? null,
        },
        { lockRoot, currentDocker: s.docker },
      );
      cleanupOnError = true;
    } else if (s.capturedAt) {
      // A missing lock cannot authorize a scan, but exact run labels still
      // make it safe to tear down resources created by this run.
      cleanupOnError = true;
      throw Error("缺少当前 run 的项目锁；停止扫描");
    }
    if (!(await owned(s.server)) || !(await owned(s.network, true)))
      throw Error("本机服务不属于本轮");
    const server = JSON.parse(await docker(["inspect", s.server]))[0];
    const networks = Object.keys(server.NetworkSettings.Networks);
    if (
      networks.length !== 1 ||
      networks[0] !== s.network ||
      !JSON.parse(await docker(["network", "inspect", s.network]))[0].Internal
    )
      throw Error("Sonar 网络隔离被改变");
    if (!checkSnapshot(run)) throw Error("当前源码与快照不一致，请重新采集");
    if (!fs.existsSync(path.join(s.root, "node_modules")))
      throw Error("项目依赖未安装；停止扫描，避免 Docker 创建原项目目录");
    read(path.join(run, "config-parity.json"));
    const receipt = read(path.join(run, "coverage-receipt.json"));
    const manifest = read(path.join(run, "source-manifest.json"));
    const { createHash } = await import("node:crypto");
    const lcov = fs.readFileSync(path.join(run, "source/coverage/lcov.info"));
    if (
      receipt.sourceFingerprint !== manifest.sourceFingerprint ||
      receipt.reportHash !== createHash("sha256").update(lcov).digest("hex")
    )
      throw Error("覆盖率与源码快照不匹配");
    fs.mkdirSync(path.join(run, "work"), { recursive: true });
    fs.mkdirSync(path.join(s.cache, "scanner-cache"), {
      recursive: true,
      mode: 0o700,
    });
    const props = fs.readFileSync(
      path.join(run, "source/sonar-project.properties"),
      "utf8",
    );
    const properties = parseProperties(props);
    const scanSettings = scannerSettings(
      read(path.join(run, "config/settings.json")).settings, properties,
    );
    save(path.join(run, "scanner-settings-effective.json"), scanSettings);
    const onlineExclusions = read(
      path.join(run, "config/settings.json"),
    ).settings.find((setting) => setting.key === "sonar.exclusions");
    // 本地显式配置（包括空值）优先，否则继承已导出的线上配置。
    const exclusions = properties.has("sonar.exclusions")
      ? properties.get("sonar.exclusions")
      : onlineExclusions?.values
        ? serializeCsvValues(onlineExclusions.values)
        : (onlineExclusions?.value ?? "");
    throwIfAborted(signal);
    await logged(
    [
      "run",
      "--rm",
      "--name",
      s.scanner,
      "--label",
      `sonar-fix-local.run=${s.id}`,
      "--platform",
      "linux/amd64",
      "--network",
      s.network,
      "--user",
      "0",
      "--memory",
      "4g",
      "--cpus",
      "4",
      "-e",
      "SONAR_SCANNER_JAVA_OPTS=-Xmx512m",
      "-e",
      "SONAR_SCANNER_OPTS=-Xmx512m",
      "-e",
      "SCANNER_WORKDIR_PATH=/usr/src/.scannerwork",
      "--env-file",
      `${run}/scanner.env`,
      "-v",
      `${run}/source:/usr/src:ro`,
      "-v",
      `${run}/work:/usr/src/.scannerwork`,
      "-v",
      `${s.cache}/scanner-cache:/opt/sonar-scanner/.sonar/cache`,
      "-v",
      `${s.root}/node_modules:/usr/src/node_modules:ro`,
      SCANNER,
      `-Dsonar.host.url=http://${s.server}:9000`,
      `-Dsonar.projectKey=${s.project}`,
      "-Dsonar.projectName=Local Sonar analysis",
      "-Dsonar.scm.disabled=true",
      "-Dsonar.scanner.skipJreProvisioning=true",
      "-Dsonar.javascript.node.maxspace=2560",
      "-Dsonar.javascript.lcov.reportPaths=coverage/lcov.info",
      // 只导入有凭证的 LCOV，不读取项目中未验证的覆盖率或测试执行 XML。
      "-Dsonar.typescript.lcov.reportPaths=",
      "-Dsonar.coverageReportPaths=",
      "-Dsonar.testExecutionReportPaths=",
      `-Dsonar.exclusions=${exclusions}${exclusions ? "," : ""}**/.git/**`,
      ...Object.entries(scanSettings).map(([key, value]) => `-D${key}=${value}`),
      "-Dsonar.qualitygate.wait=false",
    ],
    path.join(run, "scanner.log"),
    signal,
    );
    throwIfAborted(signal);
    const task = fs
      .readFileSync(path.join(run, "work/report-task.txt"), "utf8")
      .match(/^ceTaskId=(\S+)$/m)?.[1];
    if (!task || !/^[A-Za-z0-9_-]+$/.test(task))
      throw Error("缺少本次分析任务编号");
    const c = read(path.join(run, "credentials.json"));
    const api = localClient(c.origin, "Bearer " + c.token, fetch, signal);
    const deadline = Date.now() + 120000;
    while (Date.now() < deadline) {
      throwIfAborted(signal);
      const data = await api("/api/ce/task", { id: task });
      if (data.task?.status === "SUCCESS") {
        save(path.join(run, "analysis-success.json"), {
          task,
          analysisId: data.task.analysisId,
        });
        const result = await collectLocal(run, { signal });
        throwIfAborted(signal);
        return result;
      }
      if (!["PENDING", "IN_PROGRESS"].includes(data.task?.status))
        throw Error("本次分析任务失败；不读取旧结果");
      await waitWithSignal(1500, signal);
    }
    throw Error("本机分析处理超过两分钟；本轮无有效结论");
  } catch (error) {
    // All awaited scanner/HTTP operations have settled. A concurrent stop
    // owns cleanup; acknowledge it before it inspects resources or releases
    // the project lease. Self-cleanup must also release this gate first.
    const stoppedElsewhere = operation.finish();
    if (!stoppedElsewhere && (cleanupOnError || signal?.aborted)) {
      try {
        await cleanupRun(run, { holderToken: releaseScan?.holderToken ?? null });
      } catch (cleanupError) {
        throw Error(`${error.message}；当前 run 清理失败：${cleanupError.message}`);
      }
    }
    throw error;
  } finally {
    operation.finish();
    releaseScan?.();
  }
}
export async function stop(run) {
  await cleanupRun(run);
}
if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(fs.realpathSync(process.argv[1])).href
) {
  const [mode, runArg, port] = process.argv.slice(2);
  const runPath = runArg ? path.resolve(runArg) : null;
  const abortController = new AbortController();
  let interrupted = false;
  let sigintListener;
  let sigtermListener;
  const onInterrupt = (signal) => {
    if (interrupted) return;
    interrupted = true;
    const error = Error(`收到 ${signal}，停止当前操作`);
    error.signal = signal;
    abortController.abort(error);
    process.exitCode = signal === "SIGINT" ? 130 : 143;
  };
  if (["start", "scan", "stop"].includes(mode)) {
    sigintListener = () => onInterrupt("SIGINT");
    sigtermListener = () => onInterrupt("SIGTERM");
    process.on("SIGINT", sigintListener);
    process.on("SIGTERM", sigtermListener);
  }
  try {
    if (mode === "start") await start(runPath, port, abortController.signal);
    else if (mode === "scan") {
      const r = await scan(runPath, abortController.signal);
      console.log(
        JSON.stringify({
          report: path.join(runPath, "result.json"),
          issues: r.issues.length,
          hotspots: r.hotspots.length,
          snapshotMatchesCurrent: r.snapshotMatchesCurrent,
          newCodeGateEvaluated: false,
        }),
      );
      if (!r.snapshotMatchesCurrent) process.exitCode = 2;
    } else if (mode === "stop") await stop(runPath);
    else throw Error("命令：start | scan | stop");
  } catch (e) {
    console.error(e.message);
    if (!interrupted) process.exitCode = 1;
  } finally {
    if (sigintListener) process.removeListener("SIGINT", sigintListener);
    if (sigtermListener) process.removeListener("SIGTERM", sigtermListener);
  }
}
