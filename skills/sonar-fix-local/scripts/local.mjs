import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import crypto from "node:crypto";
import { execFileSync, spawn } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";
import { loadConfig } from "./diagnose.mjs";

export const CACHE = path.join(os.homedir(), ".local/share/sonar-fix-local");
// 相同 JSON/YAML 输入也会触发 IaC 规则；Profile 与分析器设置使用同一映射。
const PROFILE_SETTINGS = new Map([
  ["js", "javascript"],
  ["ts", "typescript"],
  ["css", "css"],
  ["web", "html"],
  ["docker", "docker"],
  ["yaml", "yaml"],
  ["json", "json"],
  ["cloudformation", "cloudformation"],
  ["kubernetes", "kubernetes"],
  ["azureresourcemanager", "azureresourcemanager"],
  ["ansible", "ansible"],
  ["githubactions", "githubactions"],
  ["azurepipelines", "azurepipelines"],
  ["secrets", "text"],
  ["text", "text"],
]);
const ANALYZER_SETTING_PREFIXES = [...new Set(PROFILE_SETTINGS.values())]
  .map((name) => `sonar.${name}.`);
const UNSUPPORTED_ISSUE_PATTERNS = new Map([
  ["sonar.issue.ignore.multicriteria", "多条件忽略"],
  ["sonar.issue.ignore.allfile", "文件内容忽略"],
  ["sonar.issue.ignore.block", "代码块忽略"],
  ["sonar.issue.enforce.multicriteria", "规则适用范围"],
]);
const SETTINGS = [
  "sonar.exclusions",
  "sonar.inclusions",
  "sonar.test.exclusions",
  "sonar.test.inclusions",
  "sonar.coverage.exclusions",
  "sonar.cpd.exclusions",
  "sonar.sourceEncoding",
  "sonar.filesize.limit",
  "sonar.global.exclusions",
  "sonar.global.test.exclusions",
  ...UNSUPPORTED_ISSUE_PATTERNS.keys(),
];
const hash = (data) => crypto.createHash("sha256").update(data).digest("hex");
export const read = (filename) => JSON.parse(fs.readFileSync(filename, "utf8"));
export function save(filename, value) {
  fs.mkdirSync(path.dirname(filename), { recursive: true, mode: 0o700 });
  fs.writeFileSync(filename, JSON.stringify(value, null, 2) + "\n", {
    mode: 0o600,
  });
}
export function localOrigin(value) {
  const u = new URL(value);
  if (
    u.protocol !== "http:" ||
    u.hostname !== "127.0.0.1" ||
    !u.port ||
    u.username ||
    u.password ||
    u.pathname !== "/" ||
    u.search ||
    u.hash
  )
    throw Error("本机 API 必须为 http://127.0.0.1:<port>");
  return u.origin;
}
export function onlineClient(config, fetcher = fetch, audit = []) {
  if (config.host !== "https://sonar.rydev.work")
    throw Error("拒绝向其他线上主机发送凭据");
  // 线上客户端没有写方法；本机客户端和线上凭据分开创建。
  return async (endpoint, params = {}, binary = false) => {
    if (
      !endpoint.startsWith("/api/") ||
      endpoint.startsWith("//") ||
      endpoint.includes("://")
    )
      throw Error("无效 API 路径");
    const u = new URL(endpoint, config.host);
    u.search = new URLSearchParams(params);
    let r;
    try {
      r = await fetcher(u, {
        method: "GET",
        headers: { Authorization: `Bearer ${config.token}` },
        redirect: "error",
        signal: AbortSignal.timeout(30000),
      });
    } catch {
      throw Error(`线上 GET ${u.pathname} 失败，凭据与响应正文未输出`);
    }
    audit.push({ method: "GET", path: u.pathname, status: r.status });
    if (!r.ok)
      throw Error(`线上 GET ${u.pathname}: HTTP ${r.status}，停止同步`);
    if (binary) return Buffer.from(await r.arrayBuffer());
    return r.json();
  };
}
export function snapshotFiles(root) {
  const names = execFileSync(
    "git",
    ["ls-files", "-co", "--exclude-standard", "-z"],
    { cwd: root },
  )
    .toString()
    .split("\0")
    .filter(Boolean);
  return [...new Set(names)]
    .filter((n) => fs.existsSync(path.join(root, n)))
    .sort();
}
export function fingerprint(root, names = snapshotFiles(root)) {
  const files = names.map((name) => {
    const src = path.join(root, name);
    if (!fs.lstatSync(src).isFile())
      throw Error(`不支持目录或符号链接源码：${name}`);
    return [name, hash(fs.readFileSync(src))];
  });
  return { files, sourceFingerprint: hash(JSON.stringify(files)) };
}
export function checkSnapshot(run) {
  const state = read(path.join(run, "state.json"));
  const manifest = read(path.join(run, "source-manifest.json"));
  return (
    fingerprint(state.root).sourceFingerprint === manifest.sourceFingerprint
  );
}
export async function prepare(root, { cache = CACHE, fetcher = fetch } = {}) {
  root = fs.realpathSync(root);
  const config = loadConfig(root);
  const names = snapshotFiles(root);
  // 凭据文件即使意外成为非忽略文件，也不进入快照。
  if (
    names.some(
      (n) =>
        /(^|\/)\.env(?:$|\.)/.test(n) &&
        !/\.env\.(example|sample|template)$/.test(n),
    )
  )
    throw Error("源码清单含环境凭据文件，停止复制");
  if (
    names.some((n) =>
      /\.(py|java|go|cs|rb|php|kts?|scala|swift|cpp|c|h|xml|xsd|xsl|xslt|tf(?:vars)?(?:\.json)?|hcl|bicep)$/i.test(n),
    )
  )
    throw Error(
      "当前流程只核验 JS/TS 前端相关分析器；其他语言需先补充配置同步方案",
    );
  const id = crypto.randomBytes(8).toString("hex");
  const run = path.join(cache, "runs", id);
  if (run === root || run.startsWith(root + path.sep))
    throw Error("工具与缓存必须放项目之外");
  fs.mkdirSync(run, { recursive: true, mode: 0o700 });
  fs.chmodSync(run, 0o700);
  const state = {
    id,
    root,
    project: `sonar-fix-local-${id}`,
    server: `sfl-${id}-server`,
    proxy: `sfl-${id}-access`,
    scanner: `sfl-${id}-scanner`,
    network: `sfl-${id}`,
    cache,
    // null 表示新运行尚未绑定 Docker；缺少字段的旧运行不能自动认领资源。
    docker: null,
    capturedAt: new Date().toISOString(),
  };
  save(path.join(run, "state.json"), state);
  const audit = [];
  const get = onlineClient(config, fetcher, audit);
  try {
    const version = (await get("/api/server/version", {}, true))
      .toString()
      .trim();
    if (!/^\d+\.\d+\.\d+\.\d+$/.test(version))
      throw Error("无法确定精确 Sonar 镜像版本");
    state.version = version;
    state.image = `sonarqube:${version}-community`;
    const plugins = await get("/api/plugins/installed");
    save(path.join(run, "config/plugins.json"), plugins);
    const result = await get("/api/qualityprofiles/search", {
      project: config.project,
    });
    const profiles = result.profiles.filter((p) => PROFILE_SETTINGS.has(p.language));
    if (
      !profiles.some((p) => p.language === "ts") ||
      !profiles.some((p) => p.language === "js")
    )
      throw Error("缺少 JS/TS 项目规则配置");
    const exports = [];
    for (const p of profiles) {
      if (exports.some((x) => x.language === p.language))
        throw Error("同语言有多个 Profile，无法确定项目绑定");
      const xml = await get(
        "/api/qualityprofiles/backup",
        { language: p.language, qualityProfile: p.name },
        true,
      );
      fs.writeFileSync(path.join(run, "config", `${p.language}.xml`), xml, {
        mode: 0o600,
      });
      exports.push({
        language: p.language,
        name: p.name,
        activeRules: p.activeRuleCount,
        sha256: hash(xml),
      });
    }
    save(path.join(run, "config/profiles.json"), exports);
    const binding = await get("/api/qualitygates/get_by_project", {
      project: config.project,
    });
    save(
      path.join(run, "config/gate.json"),
      await get("/api/qualitygates/show", { name: binding.qualityGate.name }),
    );
    const onlineSettings = await get("/api/settings/values", {
      component: config.project,
    });
    // 只保存分析范围和已支持分析器的配置；不复制服务器、认证或其他语言设置。
    const settings = {
      settings: onlineSettings.settings.filter((setting) =>
        SETTINGS.includes(setting.key) ||
        ANALYZER_SETTING_PREFIXES.some((prefix) => setting.key.startsWith(prefix)),
      ),
    };
    const unsupported = settings.settings.find(
        (x) =>
          UNSUPPORTED_ISSUE_PATTERNS.has(x.key) &&
          (x.values?.length ||
            x.value ||
            Object.keys(x.fieldValues ?? {}).length),
    );
    if (unsupported)
      throw Error(
        `线上配置了${UNSUPPORTED_ISSUE_PATTERNS.get(unsupported.key)}（${unsupported.key}）；需同步完整子配置后才能分析，不能静默省略`,
      );
    save(path.join(run, "config/settings.json"), settings);
    if (profiles.some((p) => p.language === "yaml")) {
      const jar = await get("/api/plugins/download", { plugin: "yaml" }, true);
      if (jar.subarray(0, 2).toString() !== "PK")
        throw Error("YAML 插件不是 JAR");
      fs.writeFileSync(path.join(run, "yaml.jar"), jar, { mode: 0o600 });
      // JAR 不含凭据；私有 run 目录仍为 0700，挂载后须允许容器 UID 读取。
      // 显式 chmod 避免宿主 umask 077 把读取权限再次屏蔽。
      fs.chmodSync(path.join(run, "yaml.jar"), 0o644);
    }
    const manifest = {
      head: execFileSync("git", ["rev-parse", "HEAD"], {
        cwd: root,
        encoding: "utf8",
      }).trim(),
      ...fingerprint(root, names),
    };
    for (const [name] of manifest.files) {
      const dst = path.join(run, "source", name);
      fs.mkdirSync(path.dirname(dst), { recursive: true });
      fs.copyFileSync(path.join(root, name), dst);
    }
    if (
      fingerprint(
        path.join(run, "source"),
        manifest.files.map(([n]) => n),
      ).sourceFingerprint !== manifest.sourceFingerprint ||
      fingerprint(root).sourceFingerprint !== manifest.sourceFingerprint
    )
      throw Error("复制期间源码变化，丢弃本轮并重新采集");
    save(path.join(run, "source-manifest.json"), manifest);
    save(path.join(run, "state.json"), state);
    for (const d of [".scannerwork", "node_modules", "coverage"])
      fs.mkdirSync(path.join(run, "source", d), { recursive: true });
    execFileSync("git", ["init", "-q", "--template="], {
      cwd: path.join(run, "source"),
    });
    execFileSync(
      "git",
      [
        "--literal-pathspecs",
        "add",
        "-f",
        "--pathspec-from-file=-",
        "--pathspec-file-nul",
      ],
      {
        cwd: path.join(run, "source"),
        input: manifest.files.map(([n]) => n).join("\0") + "\0",
      },
    );
    return run;
  } finally {
    save(path.join(run, "online-read-audit.json"), audit);
  }
}
export function localClient(origin, token, fetcher = fetch) {
  origin = localOrigin(origin);
  return async (endpoint, params = {}, post = false, form = null) => {
    if (
      !endpoint.startsWith("/api/") ||
      endpoint.startsWith("//") ||
      endpoint.includes("://")
    )
      throw Error("无效本机 API 路径");
    const u = new URL(endpoint, origin);
    // 多值设置通过重复参数传递，逗号可能是单个值的一部分。
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params))
      for (const item of Array.isArray(value) ? value : [value])
        query.append(key, item);
    if (!post) u.search = query;
    let r;
    try {
      r = await fetcher(u, {
        method: post ? "POST" : "GET",
        headers: {
          Authorization: token,
          ...(post && !form
            ? { "Content-Type": "application/x-www-form-urlencoded" }
            : {}),
        },
        body: post ? form || query : undefined,
        redirect: "error",
        signal: AbortSignal.timeout(30000),
      });
    } catch {
      throw Error(`本机 ${u.pathname} 请求失败，凭据与正文未输出`);
    }
    if (!r.ok) throw Error(`本机 ${u.pathname}: HTTP ${r.status}`);
    const text = await r.text();
    try {
      return JSON.parse(text);
    } catch {
      return text;
    }
  };
}
export async function configureGate(api, gateName, conditions, project) {
  await api("/api/qualitygates/create", { name: gateName }, true);
  const existing = (await api("/api/qualitygates/show", { name: gateName }))
    .conditions;
  const equal = (a, b) =>
    a.metric === b.metric && a.op === b.op && a.error === b.error;
  // 新建 Gate 可能自带条件：保留完全相同项，替换不同项，不重复添加。
  for (const c of existing)
    if (!conditions.some((wanted) => equal(c, wanted)))
      await api("/api/qualitygates/delete_condition", { id: c.id }, true);
  for (const c of conditions)
    if (!existing.some((item) => equal(item, c)))
      await api(
        "/api/qualitygates/create_condition",
        { gateName, metric: c.metric, op: c.op, error: c.error },
        true,
      );
  await api(
    "/api/qualitygates/select",
    { projectKey: project, gateName },
    true,
  );
}
export async function bootstrap(run, origin) {
  const state = read(path.join(run, "state.json"));
  origin = localOrigin(origin);
  if (fs.existsSync(path.join(run, "credentials.json")))
    throw Error("已配置过本机实例；不重置现有账号，使用新 run");
  const password = crypto.randomBytes(30).toString("hex") + "aA1!";
  save(path.join(run, "pending-password.json"), { origin, password });
  let api = localClient(
    origin,
    "Basic " + Buffer.from("admin:admin").toString("base64"),
  );
  const status = await api("/api/system/status");
  if (status.status !== "UP" || status.version !== state.version)
    throw Error("本机服务未就绪或版本不一致");
  await api(
    "/api/users/change_password",
    { login: "admin", previousPassword: "admin", password },
    true,
  );
  api = localClient(
    origin,
    "Basic " + Buffer.from("admin:" + password).toString("base64"),
  );
  const { token } = await api(
    "/api/user_tokens/generate",
    { name: state.project, type: "USER_TOKEN" },
    true,
  );
  if (!token) throw Error("本机 token 生成失败");
  save(path.join(run, "credentials.json"), { origin, token });
  api = localClient(origin, "Bearer " + token);
  await api(
    "/api/projects/create",
    {
      project: state.project,
      name: "Local Sonar analysis",
      visibility: "private",
    },
    true,
  );
  for (const p of read(path.join(run, "config/profiles.json"))) {
    const name = `Local ${p.language} ${state.id}`;
    const xml = fs
      .readFileSync(path.join(run, "config", `${p.language}.xml`), "utf8")
      .replace(/<name>[^<]*<\/name>/, `<name>${name}</name>`);
    const form = new FormData();
    form.set(
      "backup",
      new Blob([xml], { type: "application/xml" }),
      "profile.xml",
    );
    await api("/api/qualityprofiles/restore", {}, true, form);
    await api(
      "/api/qualityprofiles/add_project",
      { project: state.project, language: p.language, qualityProfile: name },
      true,
    );
    fs.writeFileSync(
      path.join(run, "config", `local-${p.language}.xml`),
      await api("/api/qualityprofiles/backup", {
        language: p.language,
        qualityProfile: name,
      }),
      { mode: 0o600 },
    );
  }
  const gateName = `Local ${state.id}`;
  await configureGate(
    api,
    gateName,
    read(path.join(run, "config/gate.json")).conditions,
    state.project,
  );
  for (const s of read(path.join(run, "config/settings.json")).settings) {
    if (UNSUPPORTED_ISSUE_PATTERNS.has(s.key)) continue;
    await api(
      "/api/settings/set",
      {
        // 继承标记不改变设置作用域；全局排除只能写入本轮隔离实例的全局配置。
        ...(s.key.startsWith("sonar.global.")
          ? {}
          : { component: state.project }),
        key: s.key,
        ...(s.values ? { values: s.values } : { value: s.value }),
      },
      true,
    );
  }
  const settings = read(path.join(run, "config/settings.json")).settings.filter(
    (setting) => !UNSUPPORTED_ISSUE_PATTERNS.has(setting.key),
  );
  const localSettings = settings.length
    ? await api("/api/settings/values", {
        component: state.project,
        keys: settings.map((setting) => setting.key).join(","),
      })
    : { settings: [] };
  for (const setting of settings) {
    const actual = localSettings.settings.find(
      (item) => item.key === setting.key,
    );
    if (
      !actual ||
      JSON.stringify(actual.values ?? actual.value) !==
        JSON.stringify(setting.values ?? setting.value)
    )
      throw Error(`本机分析范围设置不一致：${setting.key}`);
  }
  save(path.join(run, "config/local-settings.json"), localSettings);
  save(
    path.join(run, "config/local-plugins.json"),
    await api("/api/plugins/installed"),
  );
  save(
    path.join(run, "config/local-gate.json"),
    await api("/api/qualitygates/show", { name: gateName }),
  );
  execFileSync(
    "python3",
    [fileURLToPath(new URL("./profile-parity.py", import.meta.url)), run],
    { stdio: "pipe" },
  );
  fs.writeFileSync(path.join(run, "scanner.env"), `SONAR_TOKEN=${token}\n`, {
    mode: 0o600,
  });
}
export async function pages(get, endpoint, params, field) {
  const rows = [];
  let total;
  for (let p = 1; p <= 100; p++) {
    const data = await get(endpoint, { ...params, p: String(p), ps: "100" });
    if (
      !Array.isArray(data[field]) ||
      !Number.isInteger(data.paging?.total) ||
      (total !== undefined && total !== data.paging.total)
    )
      throw Error("本机分页响应不完整或正在变化");
    total = data.paging.total;
    rows.push(...data[field]);
    if (rows.length === total) return rows;
    if (!data[field].length || rows.length > total)
      throw Error("本机分页提前结束或总数不符");
  }
  throw Error("本机分页超过限制");
}
export async function collectLocal(run, { fetcher = fetch } = {}) {
  const state = read(path.join(run, "state.json"));
  const c = read(path.join(run, "credentials.json"));
  const api = localClient(c.origin, "Bearer " + c.token, fetcher);
  const before = await api("/api/ce/component", { component: state.project });
  if (
    before.current?.status !== "SUCCESS" ||
    !before.current.analysisId ||
    before.queue?.length
  )
    throw Error("本机分析未完成");
  const analysisId = before.current.analysisId;
  if (read(path.join(run, "analysis-success.json")).analysisId !== analysisId)
    throw Error("本机结果不属于本次扫描任务");
  const issues = await pages(
    api,
    "/api/issues/search",
    { componentKeys: state.project, resolved: "false" },
    "issues",
  );
  const hotspots = await pages(
    api,
    "/api/hotspots/search",
    { project: state.project, status: "TO_REVIEW" },
    "hotspots",
  );
  const metrics = await api("/api/measures/component", {
    component: state.project,
    metricKeys:
      "coverage,line_coverage,branch_coverage,ncloc,violations,bugs,vulnerabilities,code_smells,security_hotspots,duplicated_lines_density",
  });
  const gate = await api("/api/qualitygates/project_status", { analysisId });
  const after = await api("/api/ce/component", { component: state.project });
  if (
    after.current?.analysisId !== analysisId ||
    after.current?.status !== "SUCCESS" ||
    after.queue?.length
  )
    throw Error("采集期间本机分析变化");
  const result = {
    analysisId,
    project: state.project,
    sourceFingerprint: read(path.join(run, "source-manifest.json"))
      .sourceFingerprint,
    snapshotMatchesCurrent: checkSnapshot(run),
    newCodeGateEvaluated: false,
    newCodeGateNote:
      "新代码门禁未评估：本轮未建立比较基线；原始 OK 不能作为通过证据",
    testExecutionReportsImported: false,
    testExecutionNote:
      "本机仅导入 LCOV 覆盖率，不导入测试执行 XML；不提供测试数量、时长或失败数",
    issues,
    hotspots,
    metrics,
    rawGate: gate.projectStatus,
  };
  save(path.join(run, "result.json"), result);
  return result;
}
export function normalizeLcov(data, root, files) {
  const names = new Set(files.map(([name]) => name));
  let count = 0;
  let inRecord = false;
  const result = data
    .toString()
    .split("\n")
    .map((line) => {
      if (!line.startsWith("SF:")) {
        if (line.trim() === "end_of_record") {
          if (!inRecord) throw Error("LCOV 不完整：终止标记缺少 SF 记录");
          inRecord = false;
        }
        return line;
      }
      if (inRecord) throw Error("LCOV 不完整：前一 SF 记录尚未终止");
      const value = line.slice(3).trim();
      const relative = path.relative(root, path.resolve(root, value));
      if (!names.has(relative))
        throw Error("LCOV 路径不在源码快照中，停止导入");
      count++;
      inRecord = true;
      return "SF:" + relative;
    })
    .join("\n");
  if (!count || inRecord) throw Error("LCOV 不完整：缺少记录或终止标记");
  return Buffer.from(result);
}
export async function generateCoverage(run, report, command) {
  const state = read(path.join(run, "state.json"));
  const receipt = path.join(run, "coverage-receipt.json");
  // 新尝试先使旧凭证失效；任一阶段失败都不能让 Scanner 继续复用旧报告。
  fs.rmSync(receipt, { force: true });
  if (!command.length || !checkSnapshot(run))
    throw Error("缺少覆盖率命令或源码已经变化");
  const src = path.resolve(state.root, report);
  const previousReport = fs.existsSync(src)
    ? fs.statSync(src, { bigint: true })
    : null;
  const started = Date.now();
  const log = fs.openSync(path.join(run, "coverage.log"), "w", 0o600);
  const env = { ...process.env };
  delete env.SONAR_TOKEN;
  delete env.SONAR_HOST_URL;
  let code;
  try {
    code = await new Promise((resolve, reject) => {
      const child = spawn(command[0], command.slice(1), {
        cwd: state.root,
        env,
        stdio: ["ignore", log, log],
      });
      child.on("error", () => reject(Error("覆盖率命令无法启动")));
      child.on("exit", resolve);
    });
  } finally {
    fs.closeSync(log);
  }
  if (code !== 0)
    throw Error("覆盖率命令失败；查看个人目录 coverage.log，不导入旧报告");
  if (!checkSnapshot(run))
    throw Error("覆盖率执行期间源码变化；不导入报告，重新采集或报告范围缺口");
  if (!fs.existsSync(src)) throw Error("未生成新的 LCOV，拒绝复用陈旧报告");
  const currentReport = fs.statSync(src, { bigint: true });
  // 内容可以完全相同，但文件必须在本次尝试中重写或被原子替换。
  // mtime 使用纳秒精度；权限等属性变化导致的 ctime 更新不算重新生成。
  if (
    Number(currentReport.mtimeMs) < started - 2000 ||
    (previousReport &&
      ["dev", "ino", "size", "mtimeNs"].every(
        (key) => previousReport[key] === currentReport[key],
      ))
  )
    throw Error("LCOV 未重新生成，拒绝复用陈旧报告");
  const data = normalizeLcov(
    fs.readFileSync(src),
    state.root,
    read(path.join(run, "source-manifest.json")).files,
  );
  fs.writeFileSync(path.join(run, "source/coverage/lcov.info"), data, {
    mode: 0o600,
  });
  save(receipt, {
    sourceFingerprint: read(path.join(run, "source-manifest.json"))
      .sourceFingerprint,
    reportHash: hash(data),
    completedAt: new Date().toISOString(),
    command,
  });
}
if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(fs.realpathSync(process.argv[1])).href
) {
  try {
    const [mode, arg, ...rest] = process.argv.slice(2);
    if (mode === "prepare") console.log(await prepare(arg || process.cwd()));
    else if (mode === "bootstrap") await bootstrap(arg, rest[0]);
    else if (mode === "check") {
      if (!checkSnapshot(arg)) throw Error("当前源码与快照不一致");
    } else if (mode === "coverage") {
      if (rest[1] !== "--")
        throw Error("格式：coverage <run> <lcov-path> -- <command...>");
      await generateCoverage(arg, rest[0], rest.slice(2));
    } else if (mode === "collect") {
      const r = await collectLocal(arg);
      console.log(
        JSON.stringify({
          report: path.join(arg, "result.json"),
          issues: r.issues.length,
          hotspots: r.hotspots.length,
          snapshotMatchesCurrent: r.snapshotMatchesCurrent,
          newCodeGateEvaluated: false,
        }),
      );
      if (!r.snapshotMatchesCurrent) process.exitCode = 2;
    } else
      throw Error("命令：prepare | bootstrap | check | coverage | collect");
  } catch (e) {
    console.error(e.message);
    process.exitCode = 1;
  }
}
