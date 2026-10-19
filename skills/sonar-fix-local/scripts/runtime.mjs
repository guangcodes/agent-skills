import fs from "node:fs";
import path from "node:path";
import { execFileSync, spawn } from "node:child_process";
import { pathToFileURL } from "node:url";
import { parseProperties, serializeCsvValues } from "./properties.mjs";
import {
  read,
  save,
  localClient,
  bootstrap,
  checkSnapshot,
  collectLocal,
} from "./local.mjs";

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
function docker(args, host) {
  return execFileSync("docker", host ? ["--host", host, ...args] : args, {
    encoding: "utf8",
    env: dockerEnvironment(host),
    timeout: 30000,
    stdio: ["ignore", "pipe", "pipe"],
  });
}
async function logged(args, log, host) {
  const fd = fs.openSync(log, "w", 0o600);
  try {
    const code = await new Promise((resolve, reject) => {
      const child = spawn("docker", host ? ["--host", host, ...args] : args, {
        env: dockerEnvironment(host),
        stdio: ["ignore", fd, fd],
      });
      child.on("error", () => reject(Error("Docker 无法启动")));
      child.on("exit", resolve);
    });
    if (code !== 0)
      throw Error("Docker 操作失败；检查个人目录日志：" + path.basename(log));
  } finally {
    fs.closeSync(fd);
  }
}
function stateFor(run, bind = false) {
  if (process.env.DOCKER_HOST && !process.env.DOCKER_HOST.startsWith("unix://"))
    throw Error("拒绝 DOCKER_HOST 指向远端 Docker");
  const s = read(path.join(run, "state.json"));
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
  const { context, host } = readDockerContext();
  if (!host?.startsWith("unix://"))
    throw Error("必须使用本机 Unix socket Docker context，拒绝远端 Docker");
  const daemonId = docker(["info", "--format", "{{.ID}}"], host).trim();
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
function readDockerContext() {
  const context = docker(["context", "show"]).trim();
  const configuredHost = JSON.parse(docker(["context", "inspect", context]))[0]
    .Endpoints.docker.Host;
  // DOCKER_CONTEXT 优先于 DOCKER_HOST，与 Docker CLI 的环境变量优先级一致。
  const host = process.env.DOCKER_CONTEXT
    ? configuredHost
    : process.env.DOCKER_HOST || configuredHost;
  return { context, host };
}
function runtimeFor(run, bind = false) {
  const s = stateFor(run, bind);
  return {
    s,
    docker: (args) => docker(args, s.docker.host),
    logged: (args, log) => logged(args, log, s.docker.host),
    owned: (name, network = false) => owned(name, s.id, network, s.docker.host),
  };
}
function owned(name, id, network, host) {
  try {
    const data = JSON.parse(
      docker(network ? ["network", "inspect", name] : ["inspect", name], host),
    )[0];
    return (
      (network ? data.Labels : data.Config.Labels)?.["sonar-fix-local.run"] ===
      id
    );
  } catch (error) {
    // inspect 的连接失败、超时和异常响应不能视为资源已消失。
    // Docker CLI 的错误前缀存在大小写差异；仍要求完整消息与当前资源名匹配。
    const missingMessages = new Set([
      `error: no such object: ${name}`,
      `error: no such container: ${name}`,
      `error: no such network: ${name}`,
      `error response from daemon: no such container: ${name}`,
      `error response from daemon: network ${name} not found`,
    ]);
    if (
      error.status === 1 &&
      missingMessages.has(error.stderr?.toString().trim().toLowerCase())
    )
      return false;
    throw Error(`无法检查本轮 Docker 资源 ${name}；停止操作并保留本机凭据`);
  }
}
export async function start(run, port = "19090") {
  const { s, docker, logged } = runtimeFor(run, true);
  if (!/^\d+$/.test(port) || +port < 1024 || +port > 65535)
    throw Error("无效本机端口");
  for (const image of [s.image, SCANNER, "nginx:1.28-alpine"]) {
    try {
      docker(["image", "inspect", image]);
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
      );
    }
  }
  const label = ["--label", `sonar-fix-local.run=${s.id}`];
  docker(["network", "create", "--internal", ...label, s.network]);
  const network = JSON.parse(docker(["network", "inspect", s.network]))[0];
  if (!network.Internal) throw Error("必须使用内部网络");
  const plugin = fs.existsSync(path.join(run, "yaml.jar"))
    ? [
        "-v",
        `${run}/yaml.jar:/opt/sonarqube/extensions/plugins/sonar-yaml-plugin.jar:ro`,
      ]
    : [];
  docker([
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
  const ip = JSON.parse(docker(["inspect", s.server]))[0].NetworkSettings
    .Networks[s.network].IPAddress;
  if (!/^\d+\.\d+\.\d+\.\d+$/.test(ip)) throw Error("无效本机容器 IP");
  // 内部网络不发布宿主端口；代理只转发到固定的本机容器，不接受目标 URL。
  fs.writeFileSync(
    path.join(run, "nginx.conf"),
    `events {}\nhttp { access_log off; server { listen 9000; client_max_body_size 100m; location / { proxy_pass http://${ip}:9000; proxy_read_timeout 300s; } } }\n`,
    { mode: 0o600 },
  );
  docker([
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
  docker(["network", "connect", s.network, s.proxy]);
  docker(["restart", s.proxy]);
  const origin = `http://127.0.0.1:${port}`;
  const deadline = Date.now() + 120000;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(origin + "/api/system/status", {
        redirect: "error",
        signal: AbortSignal.timeout(3000),
      });
      const status = await r.json();
      if (status.status === "UP") {
        await bootstrap(run, origin);
        return;
      }
    } catch (e) {
      if (fs.existsSync(path.join(run, "pending-password.json"))) throw e;
    }
    await new Promise((resolve) => setTimeout(resolve, 1500));
  }
  throw Error("本机服务启动超过两分钟，停止本轮；检查本次容器日志");
}
export async function scan(run) {
  const { s, docker, logged, owned } = runtimeFor(run);
  if (!owned(s.server) || !owned(s.network, true))
    throw Error("本机服务不属于本轮");
  const server = JSON.parse(docker(["inspect", s.server]))[0];
  const networks = Object.keys(server.NetworkSettings.Networks);
  if (
    networks.length !== 1 ||
    networks[0] !== s.network ||
    !JSON.parse(docker(["network", "inspect", s.network]))[0].Internal
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
  const onlineExclusions = read(
    path.join(run, "config/settings.json"),
  ).settings.find((setting) => setting.key === "sonar.exclusions");
  // 本地显式配置（包括空值）优先，否则继承已导出的线上配置。
  const exclusions = properties.has("sonar.exclusions")
    ? properties.get("sonar.exclusions")
    : onlineExclusions?.values
      ? serializeCsvValues(onlineExclusions.values)
      : (onlineExclusions?.value ?? "");
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
      "-Dsonar.qualitygate.wait=false",
    ],
    path.join(run, "scanner.log"),
  );
  const task = fs
    .readFileSync(path.join(run, "work/report-task.txt"), "utf8")
    .match(/^ceTaskId=(\S+)$/m)?.[1];
  if (!task || !/^[A-Za-z0-9_-]+$/.test(task))
    throw Error("缺少本次分析任务编号");
  const c = read(path.join(run, "credentials.json"));
  const api = localClient(c.origin, "Bearer " + c.token);
  const deadline = Date.now() + 120000;
  while (Date.now() < deadline) {
    const data = await api("/api/ce/task", { id: task });
    if (data.task?.status === "SUCCESS") {
      save(path.join(run, "analysis-success.json"), {
        task,
        analysisId: data.task.analysisId,
      });
      return collectLocal(run);
    }
    if (!["PENDING", "IN_PROGRESS"].includes(data.task?.status))
      throw Error("本次分析任务失败；不读取旧结果");
    await new Promise((resolve) => setTimeout(resolve, 1500));
  }
  throw Error("本机分析处理超过两分钟；本轮无有效结论");
}
export function stop(run) {
  const { s, docker, owned } = runtimeFor(run);
  for (const name of [s.scanner, s.proxy, s.server])
    if (owned(name)) docker(["rm", "-f", name]);
  if (owned(s.network, true)) docker(["network", "rm", s.network]);
  // 报告与源码快照留在私有目录；临时本机账号随容器销毁，不保留明文凭据。
  for (const name of [
    "scanner.env",
    "credentials.json",
    "pending-password.json",
  ])
    fs.rmSync(path.join(run, name), { force: true });
}
if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(fs.realpathSync(process.argv[1])).href
) {
  try {
    const [mode, run, port] = process.argv.slice(2);
    if (mode === "start") await start(path.resolve(run), port);
    else if (mode === "scan") {
      const r = await scan(path.resolve(run));
      console.log(
        JSON.stringify({
          report: path.join(path.resolve(run), "result.json"),
          issues: r.issues.length,
          hotspots: r.hotspots.length,
          snapshotMatchesCurrent: r.snapshotMatchesCurrent,
          newCodeGateEvaluated: false,
        }),
      );
      if (!r.snapshotMatchesCurrent) process.exitCode = 2;
    } else if (mode === "stop") stop(path.resolve(run));
    else throw Error("命令：start | scan | stop");
  } catch (e) {
    console.error(e.message);
    process.exitCode = 1;
  }
}
