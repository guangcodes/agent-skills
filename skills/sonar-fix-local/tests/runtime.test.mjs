import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import crypto from "node:crypto";
import { execFileSync, spawnSync, spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { fingerprint, save } from "../scripts/local.mjs";
import { acquireProjectLock, projectIdentity, projectKey } from "../scripts/lock.mjs";
import http from "node:http";

const runtime = fileURLToPath(
  new URL("../scripts/runtime.mjs", import.meta.url),
);
function fixture(t, properties = "sonar.projectKey=online-project\n") {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), "sonar-runtime-fix-"));
  t.after(() => fs.rmSync(base, { recursive: true, force: true }));
  const root = path.join(base, "project");
  const run = path.join(base, "run");
  const bin = path.join(base, "bin");
  fs.mkdirSync(root);
  fs.mkdirSync(bin);
  fs.mkdirSync(path.join(run, "source/coverage"), { recursive: true });
  execFileSync("git", ["init", "-q", "--template="], { cwd: root });
  fs.writeFileSync(path.join(root, ".gitignore"), "node_modules/\n");
  fs.mkdirSync(path.join(root, "node_modules"));
  fs.writeFileSync(path.join(root, "business.ts"), "export const total = 1;\n");
  fs.writeFileSync(path.join(root, "sonar-project.properties"), properties);
  fs.writeFileSync(
    path.join(run, "source/sonar-project.properties"),
    properties,
  );
  const id = "0123456789abcdef",
    network = `sfl-${id}`;
  save(path.join(run, "state.json"), {
    id,
    root,
    cache: path.join(base, "cache"),
    lockRoot: path.join(base, "locks"),
    projectIdentity: projectIdentity(root),
    projectKey: projectKey(projectIdentity(root)),
    network,
    server: `${network}-server`,
    proxy: `${network}-access`,
    scanner: `${network}-scanner`,
    project: `sonar-fix-local-${id}`,
    version: "26.9.0.129388",
    image: "sonarqube:26.9.0.129388-community",
    docker: {
      context: "default",
      host: "unix:///tmp/local-docker.sock",
      daemonId: "fixture-daemon",
    },
  });
  const manifest = fingerprint(root);
  save(path.join(run, "source-manifest.json"), manifest);
  save(path.join(run, "config-parity.json"), {});
  save(path.join(run, "config/settings.json"), {
    settings: [
      { key: "sonar.exclusions", values: ["vendor/**", "generated/**"] },
    ],
  });
  const lcov = "SF:business.ts\nDA:1,1\nend_of_record\n";
  fs.writeFileSync(path.join(run, "source/coverage/lcov.info"), lcov);
  save(path.join(run, "coverage-receipt.json"), {
    sourceFingerprint: manifest.sourceFingerprint,
    reportHash: crypto.createHash("sha256").update(lcov).digest("hex"),
  });
  for (const name of [
    "scanner.env",
    "credentials.json",
    "pending-password.json",
  ])
    fs.writeFileSync(path.join(run, name), "dummy-local-credential");
  const fake = path.join(bin, "docker");
  fs.writeFileSync(
    fake,
    `#!${process.execPath}
const fs = require('node:fs'); const path = require('node:path');
const raw = process.argv.slice(2), args = raw[0] === '--host' ? raw.slice(2) : raw, run = process.env.FAKE_RUN, mode = process.env.FAKE_MODE, removedFile = path.join(run, 'removed-resources.json');
const removed = fs.existsSync(removedFile) ? JSON.parse(fs.readFileSync(removedFile)) : {};
fs.appendFileSync(path.join(run, 'docker-calls.jsonl'), JSON.stringify(raw)+'\\n');
const emit = value => process.stdout.write(JSON.stringify(value));
const startedFile = path.join(run, 'created-resources.json');
const created = fs.existsSync(startedFile) ? JSON.parse(fs.readFileSync(startedFile)) : {};
const preflight = args[0] === 'info' ? 'info' : args[0] === 'context' ? args[1] : null;
if (mode === 'preflight-' + preflight) {
  fs.writeFileSync(path.join(run, 'preflight-ready'), String(process.pid));
  process.on('SIGTERM',()=>{});
  const timer = setInterval(() => {
    if (!fs.existsSync(path.join(run, 'continue-preflight'))) return;
    clearInterval(timer);
    if (preflight === 'inspect') emit([{Endpoints:{docker:{Host:'unix:///tmp/local-docker.sock'}}}]);
    else console.log(preflight === 'info' ? 'fixture-daemon' : 'default');
  }, 10);
  return;
}
if (mode === 'cleanup-pause' && args[0] === 'inspect' && !fs.existsSync(path.join(run, 'continue-cleanup'))) {
  fs.writeFileSync(path.join(run, 'cleanup-ready'), 'ready');
  const deadline = Date.now() + 6000;
  while (!fs.existsSync(path.join(run, 'continue-cleanup')) && Date.now() < deadline)
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 10);
}
if (mode === 'scan-interrupt' && args[0] === 'inspect' && !fs.existsSync(path.join(run, 'interrupt-ready'))) {
  fs.writeFileSync(path.join(run, 'interrupt-ready'), 'ready');
  process.on('SIGTERM',()=>{}); setInterval(()=>{},1000); return;
}
if (mode === 'start-interrupt' || mode === 'start-body' || mode === 'start-pull') {
  if (mode === 'start-pull' && args[0] === 'image' && args[1] === 'inspect') process.exit(1);
  if (mode === 'start-pull' && args[0] === 'pull') {
    fs.writeFileSync(path.join(run, 'pull-ready'), String(process.pid));
    process.on('SIGTERM',()=>{});
    const timer = setInterval(() => {
      if (fs.existsSync(path.join(run, 'continue-pull'))) { clearInterval(timer); process.exit(0); }
    }, 10);
    return;
  }
  if (args[0] === 'network' && args[1] === 'create') {
    created[args.at(-1)] = true; fs.writeFileSync(startedFile, JSON.stringify(created)); console.log('network'); return;
  }
  if (args[0] === 'run') {
    created[args[args.indexOf('--name')+1]] = true; fs.writeFileSync(startedFile, JSON.stringify(created));
    if (mode === 'start-interrupt') {
      fs.writeFileSync(path.join(run, 'interrupt-ready'), 'ready');
      process.on('SIGTERM',()=>{}); setInterval(()=>{},1000); return;
    }
    if (mode === 'start-pull') { console.error('unexpected creation after stop'); process.exit(7); }
    console.log('container'); return;
  }
  if (args[0] === 'inspect' || (args[0] === 'network' && args[1] === 'inspect')) {
    const name = args.at(-1);
    if (!created[name]) { console.error('Error: No such object: '+name); process.exit(1); }
  }
}
if (raw[0] === '--host' && (process.env.DOCKER_HOST || process.env.DOCKER_CONTEXT)) {
  console.error('Pinned Docker command inherited an endpoint override'); process.exit(1);
}
if (args[0] === 'context') {
  if (args[1] === 'show') console.log(mode === 'context-changed' ? 'other-context' : 'default');
  else emit([{Endpoints:{docker:{Host:mode === 'endpoint-changed' ? 'unix:///tmp/other-docker.sock' : 'unix:///tmp/local-docker.sock'}}}]);
} else if (args[0] === 'info') {
  console.log(mode === 'daemon-changed' ? 'other-daemon' : 'fixture-daemon');
} else if (mode === 'rm-fails' && (args[0] === 'rm' || (args[0] === 'network' && args[1] === 'rm'))) {
  console.error('Removal failed'); process.exit(1);
} else if (mode === 'daemon-down') {
  console.error('Cannot connect to the Docker daemon'); process.exit(1);
} else if (mode === 'timeout') {
  process.kill(process.pid, 'SIGTERM');
} else if (mode === 'malformed') {
  console.log('invalid inspect response');
} else if (mode === 'missing-lowercase') {
  console.error(args[0] === 'network' ? 'error: no such network: '+args[2] : 'error: no such object: '+args[1]); process.exit(1);
} else if (mode === 'missing-daemon-lowercase') {
  console.error(args[0] === 'network' ? 'error response from daemon: network '+args[2]+' not found' : 'error response from daemon: no such container: '+args[1]); process.exit(1);
} else if (mode === 'wrong-resource-missing') {
  console.error('error: no such object: unrelated-container'); process.exit(1);
} else if (mode === 'denied') {
  console.error('permission denied: error: no such object: '+args[1]); process.exit(1);
} else if (mode === 'missing') {
  console.error(args[0] === 'network' ? 'Error response from daemon: network '+args[2]+' not found' : 'Error: No such object: '+args[1]); process.exit(1);
} else if (args[0] === 'rm' || (args[0] === 'network' && args[1] === 'rm')) {
  removed[args.at(-1)] = true; fs.writeFileSync(removedFile, JSON.stringify(removed));
} else if (args[0] === 'inspect') {
  if (mode === 'scan-pending' && args[1].endsWith('-scanner') && !created[args[1]]) {
    console.error('Error: No such object: '+args[1]); process.exit(1);
  }
  if (removed[args[1]]) { console.error('Error: No such object: '+args[1]); process.exit(1); }
  emit([{Config:{Labels:{'sonar-fix-local.run':mode === 'foreign' ? 'fedcba9876543210' : '0123456789abcdef'}},NetworkSettings:{Networks:{'sfl-0123456789abcdef':{IPAddress:'172.18.0.2'}}}}]);
} else if (args[0] === 'network' && args[1] === 'inspect') {
  if (removed[args[2]]) { console.error('Error response from daemon: network '+args[2]+' not found'); process.exit(1); }
  emit([{Internal:true,Labels:{'sonar-fix-local.run':mode === 'foreign' ? 'fedcba9876543210' : '0123456789abcdef'}}]);
} else if (args[0] === 'run') {
  fs.writeFileSync(path.join(run, 'scanner-args.json'), JSON.stringify(raw));
  if (mode === 'scan-pending') {
    fs.writeFileSync(path.join(run, 'scanner-ready'), String(process.pid));
    process.on('SIGTERM',()=>{});
    const timer = setInterval(() => {
      if (!fs.existsSync(path.join(run, 'continue-scanner'))) return;
      clearInterval(timer);
      created[args[args.indexOf('--name')+1]] = true;
      fs.writeFileSync(startedFile, JSON.stringify(created));
      process.exit(0);
    }, 10);
    return;
  }
  if (mode === 'scan-success') { fs.writeFileSync(path.join(run, 'work/report-task.txt'), 'ceTaskId=task-current\\n'); process.exit(0); }
  process.exit(7);
}
`,
    { mode: 0o700 },
  );
  function execute(command, mode, envOverrides = {}) {
    const env = {
      ...process.env,
      PATH: `${bin}${path.delimiter}${process.env.PATH}`,
      FAKE_MODE: mode,
      FAKE_RUN: run,
    };
    delete env.DOCKER_HOST;
    delete env.DOCKER_CONTEXT;
    Object.assign(env, envOverrides);
    return spawnSync(process.execPath, [runtime, command, run], {
      env,
      encoding: "utf8",
    });
  }
  return {
    run, root, base, bin,
    execute,
    credentials: () =>
      ["scanner.env", "credentials.json", "pending-password.json"].filter(
        (name) => fs.existsSync(path.join(run, name)),
      ),
  };
}

for (const [name, properties, expected] of [
  [
    "继承线上数组配置",
    "sonar.projectKey=online\n",
    "vendor/**,generated/**,**/.git/**",
  ],
  [
    "本地配置优先",
    "sonar.exclusions=local-build/**\n",
    "local-build/**,**/.git/**",
  ],
  ["本地显式清空", "sonar.exclusions=\n", "**/.git/**"],
  ["冒号分隔符", "sonar.exclusions:colon-build/**\n", "colon-build/**,**/.git/**"],
  ["空白分隔符", "sonar.exclusions whitespace-build/**\n", "whitespace-build/**,**/.git/**"],
  ["重复键取最后值", "sonar.exclusions=old/**\nsonar.exclusions=actual/**\n", "actual/**,**/.git/**"],
  ["重复键最后清空", "sonar.exclusions=old/**\nsonar.exclusions:\n", "**/.git/**"],
  ["解码转义", String.raw`sonar\.exclusions=build\u002f**` + "\n", "build/**,**/.git/**"],
  ["本地 CSV 引号保留", 'sonar.exclusions="**/comma,name/**"\n', '"**/comma,name/**",**/.git/**'],
])
  test(`Scanner 排除范围：${name}`, (t) => {
    const f = fixture(t, properties);
    f.execute("scan", "capture");
    const args = JSON.parse(
      fs.readFileSync(path.join(f.run, "scanner-args.json")),
    );
    assert.deepEqual(args.slice(0, 2), [
      "--host",
      "unix:///tmp/local-docker.sock",
    ]);
    assert.equal(
      args.find((value) => value.startsWith("-Dsonar.exclusions=")),
      "-Dsonar.exclusions=" + expected,
    );
  });
test("不支持的续行配置在启动 Scanner 前明确拒绝", (t) => {
  const f = fixture(t, "sonar.exclusions=first/**,\\\n  second/**\n");
  const result = f.execute("scan", "capture");
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /不支持续行/);
  assert.equal(fs.existsSync(path.join(f.run, "scanner-args.json")), false);
});
test("Scanner 继承线上 scalar 排除配置", (t) => {
  const f = fixture(t);
  save(path.join(f.run, "config/settings.json"), {
    settings: [{ key: "sonar.exclusions", value: "legacy/**" }],
  });
  f.execute("scan", "capture");
  const args = JSON.parse(
    fs.readFileSync(path.join(f.run, "scanner-args.json")),
  );
  assert.equal(
    args.find((value) => value.startsWith("-Dsonar.exclusions=")),
    "-Dsonar.exclusions=legacy/**,**/.git/**",
  );
});
test("Scanner 继承线上数组时保留逗号、引号和空白边界", (t) => {
  const f = fixture(t);
  save(path.join(f.run, "config/settings.json"), {
    settings: [{ key: "sonar.exclusions", values: ['**/comma,name/**', '**/quote"name/**', ' leading/**'] }],
  });
  f.execute("scan", "capture");
  const args = JSON.parse(fs.readFileSync(path.join(f.run, "scanner-args.json")));
  assert.equal(
    args.find((value) => value.startsWith("-Dsonar.exclusions=")),
    '-Dsonar.exclusions="**/comma,name/**","**/quote""name/**"," leading/**",**/.git/**',
  );
});
for (const genericCoverageCopied of [false, true])
test(`仅覆盖率扫描清空继承的 XML 路径：通用覆盖率 XML ${genericCoverageCopied ? "已复制" : "缺失"}`, (t) => {
  const f = fixture(t, "sonar.projectKey=online\nsonar.testExecutionReportPaths=coverage/sonar-report.xml\nsonar.coverageReportPaths=coverage/sonar.xml\nsonar.typescript.lcov.reportPaths=coverage/old.info\n");
  if (genericCoverageCopied)
    fs.writeFileSync(path.join(f.run, "source/coverage/sonar.xml"), '<coverage version="1"/>');
  f.execute("scan", "capture");
  assert.equal(fs.existsSync(path.join(f.run, "source/coverage/sonar-report.xml")), false);
  const args = JSON.parse(fs.readFileSync(path.join(f.run, "scanner-args.json")));
  assert.equal(args.filter((value) => value.startsWith("-Dsonar.testExecutionReportPaths=")).at(-1),
    "-Dsonar.testExecutionReportPaths=");
  assert.equal(args.filter((value) => value.startsWith("-Dsonar.coverageReportPaths=")).at(-1),
    "-Dsonar.coverageReportPaths=");
  assert.ok(args.includes("-Dsonar.typescript.lcov.reportPaths="));
  assert.ok(args.includes("-Dsonar.javascript.lcov.reportPaths=coverage/lcov.info"));
});
test("覆盖率凭证失效后 Scanner 不得启动扫描", (t) => {
  const f = fixture(t);
  fs.rmSync(path.join(f.run, "coverage-receipt.json"));
  const result = f.execute("scan", "capture");
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /coverage-receipt\.json/);
  assert.equal(fs.existsSync(path.join(f.run, "scanner-args.json")), false);
});
for (const mode of [
  "daemon-down",
  "timeout",
  "malformed",
  "wrong-resource-missing",
  "denied",
])
  test(`清理检查失败时保留凭据：${mode}`, (t) => {
    const f = fixture(t);
    const result = f.execute("stop", mode);
    assert.notEqual(result.status, 0);
    assert.equal(f.credentials().length, 3);
    assert.doesNotMatch(result.stderr, /dummy-local-credential/);
  });
test("清理不删除其他 run 的 Docker 资源", (t) => {
  const f = fixture(t);
  const result = f.execute("stop", "foreign");
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /未确认属于当前 run/);
  assert.equal(f.credentials().length, 3);
  const calls = fs.readFileSync(path.join(f.run, "docker-calls.jsonl"), "utf8");
  assert.doesNotMatch(calls, /"rm"/);
});
test("资源明确不存在时清理可重复执行", (t) => {
  const f = fixture(t);
  assert.equal(f.execute("stop", "missing").status, 0);
  assert.equal(f.credentials().length, 0);
  assert.equal(f.execute("stop", "missing").status, 0);
});
test("成功清理后才删除凭据，且只清理本轮资源", (t) => {
  const f = fixture(t);
  assert.equal(f.execute("stop", "owned").status, 0);
  assert.equal(f.credentials().length, 0);
  const calls = fs
    .readFileSync(path.join(f.run, "docker-calls.jsonl"), "utf8")
    .trim()
    .split("\n")
    .map((line) => {
      const args = JSON.parse(line);
      return args[0] === "--host" ? args.slice(2) : args;
    });
  assert.deepEqual(
    calls.filter((args) => args[0] === "rm").map((args) => args.at(-1)),
    [
      "sfl-0123456789abcdef-scanner",
      "sfl-0123456789abcdef-access",
      "sfl-0123456789abcdef-server",
    ],
  );
  assert.deepEqual(
    calls.filter((args) => args[0] === "network" && args[1] === "rm"),
    [["network", "rm", "sfl-0123456789abcdef"]],
  );
});

test("清理后保留结果、覆盖率、日志、配置和源码快照", (t) => {
  const f = fixture(t);
  for (const name of [
    "result.json",
    "scanner.log",
    "coverage.log",
    "source-manifest.json",
    "config/settings.json",
    "source/business.ts",
    "source/coverage/lcov.info",
  ]) {
    const file = path.join(f.run, name);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, "retain");
  }
  assert.equal(f.execute("stop", "owned").status, 0);
  for (const name of [
    "result.json",
    "scanner.log",
    "coverage.log",
    "source-manifest.json",
    "config/settings.json",
    "source/business.ts",
    "source/coverage/lcov.info",
  ]) assert.equal(fs.existsSync(path.join(f.run, name)), true, name);
});

for (const mode of ["missing-lowercase", "missing-daemon-lowercase"])
  test(`Docker 小写 not-found 清理可重复执行：${mode}`, (t) => {
    const f = fixture(t);
    const first = f.execute("stop", mode);
    assert.equal(first.status, 0, first.stderr);
    assert.equal(f.credentials().length, 0);
    assert.equal(f.execute("stop", mode).status, 0);
    const calls = fs
      .readFileSync(path.join(f.run, "docker-calls.jsonl"), "utf8")
      .trim()
      .split("\n")
      .map((line) => {
        const args = JSON.parse(line);
        return args[0] === "--host" ? args.slice(2) : args;
      });
    assert.equal(
      calls.some(
        (args) =>
          args[0] === "rm" || (args[0] === "network" && args[1] === "rm"),
      ),
      false,
    );
  });

for (const mode of ["context-changed", "endpoint-changed", "daemon-changed"])
  for (const command of ["scan", "stop"])
    test(`Docker 归属变化时拒绝 ${command} 并保留凭据：${mode}`, (t) => {
      const f = fixture(t);
      const result = f.execute(command, mode);
      assert.notEqual(result.status, 0);
      assert.match(result.stderr, /Docker.*变化/);
      assert.equal(f.credentials().length, 3);
      const calls = fs
        .readFileSync(path.join(f.run, "docker-calls.jsonl"), "utf8")
        .trim()
        .split("\n")
        .map((line) => JSON.parse(line));
      assert.equal(
        calls.some((args) => args.includes("rm") || args.includes("run")),
        false,
      );
    });

test("清理拒绝未绑定 Docker 的旧运行目录，保留凭据", (t) => {
  const f = fixture(t);
  const state = JSON.parse(
    fs.readFileSync(path.join(f.run, "state.json"), "utf8"),
  );
  delete state.docker;
  save(path.join(f.run, "state.json"), state);
  const result = f.execute("stop", "missing-lowercase");
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /Docker.*归属/);
  assert.equal(f.credentials().length, 3);
});

test("首次启动记录 Docker 身份，并显式固定所有资源命令的 endpoint", (t) => {
  const f = fixture(t);
  const state = JSON.parse(
    fs.readFileSync(path.join(f.run, "state.json"), "utf8"),
  );
  state.docker = null;
  save(path.join(f.run, "state.json"), state);
  // fake Docker 在创建 server 时退出；身份必须在任何资源操作之前落盘。
  assert.notEqual(f.execute("start", "capture").status, 0);
  const after = JSON.parse(
    fs.readFileSync(path.join(f.run, "state.json"), "utf8"),
  );
  assert.deepEqual(after.docker, {
    context: "default",
    host: "unix:///tmp/local-docker.sock",
    daemonId: "fixture-daemon",
  });
  const calls = fs
    .readFileSync(path.join(f.run, "docker-calls.jsonl"), "utf8")
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line));
  const resources = calls.filter((args) => !args.includes("context"));
  assert.ok(resources.length > 1);
  assert.ok(
    resources.every(
      (args) => args[0] === "--host" && args[1] === after.docker.host,
    ),
  );
});

test("显式本机 DOCKER_HOST 切换不能绕过归属检查", (t) => {
  const f = fixture(t);
  const result = f.execute("stop", "missing-lowercase", {
    DOCKER_HOST: "unix:///tmp/other-docker.sock",
  });
  assert.notEqual(result.status, 0);
  assert.equal(f.credentials().length, 3);
});

test("容器删除失败时保留凭据并停止清理", (t) => {
  const f = fixture(t);
  const result = f.execute("stop", "rm-fails");
  assert.notEqual(result.status, 0);
  assert.equal(f.credentials().length, 3);
});

test("DOCKER_CONTEXT 优先于本机 DOCKER_HOST，资源操作仍固定记录的 endpoint", (t) => {
  const f = fixture(t);
  const result = f.execute("stop", "owned", {
    DOCKER_CONTEXT: "default",
    DOCKER_HOST: "unix:///tmp/ignored-docker.sock",
  });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(f.credentials().length, 0);
  const calls = fs
    .readFileSync(path.join(f.run, "docker-calls.jsonl"), "utf8")
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line));
  assert.ok(
    calls
      .filter((args) => !args.includes("context"))
      .every(
        (args) =>
          args[0] === "--host" && args[1] === "unix:///tmp/local-docker.sock",
      ),
  );
});

for (const [label, properties, expected] of [
  ["继承线上", "sonar.projectKey=fixture\n", "30"],
  ["项目覆盖", "sonar.filesize.limit=12\n", "12"],
]) test(`Scanner 隐藏文件大小参数：${label}`, (t) => {
  const f = fixture(t, properties);
  save(path.join(f.run, "config/settings.json"), {
    settings: [{ key: "sonar.filesize.limit", value: "30", inherited: true }],
  });
  f.execute("scan", "capture");
  const args = JSON.parse(fs.readFileSync(path.join(f.run, "scanner-args.json")));
  assert.equal(args.find((value) => value.startsWith("-Dsonar.filesize.limit=")),
    `-Dsonar.filesize.limit=${expected}`);
});

async function lockFixture(t, f) {
  const s = JSON.parse(fs.readFileSync(path.join(f.run, "state.json")));
  const lock = await acquireProjectLock({ projectIdentity: s.projectIdentity, projectKey: s.projectKey,
    runId: s.id, runDir: f.run, docker: s.docker }, {
    lockRoot: s.lockRoot, currentDocker: s.docker, resourceStatus: () => ({ hasResources: false }), cleanupResources: () => {},
  });
  t.after(() => { try { process.kill(lock.metadata.pid, "SIGTERM"); } catch {} });
  return lock;
}

test("原 worktree 删除后 stop 仍按持久化身份清理资源、凭据和锁", async (t) => {
  const f = fixture(t);
  const lock = await lockFixture(t, f);
  fs.rmSync(f.root, { recursive: true });
  const result = f.execute("stop", "owned");
  assert.equal(result.status, 0, result.stderr);
  assert.equal(f.credentials().length, 0);
  assert.equal(fs.existsSync(lock.lockDir), false);
});

async function waitForReady(file) {
  const deadline = Date.now() + 5000;
  while (!fs.existsSync(file) && Date.now() < deadline)
    await new Promise((resolve) => setTimeout(resolve, 10));
  assert.ok(fs.existsSync(file), `missing readiness marker: ${file}`);
}

function interruptibleCli(t, f, command, mode, port) {
  const env = { ...process.env, PATH: `${f.bin}${path.delimiter}${process.env.PATH}`, FAKE_MODE: mode, FAKE_RUN: f.run };
  delete env.DOCKER_HOST; delete env.DOCKER_CONTEXT;
  const child = spawn(process.execPath, [runtime, command, f.run, ...(port ? [String(port)] : [])], { env, stdio: ["ignore", "pipe", "pipe"] });
  t.after(() => { if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL"); });
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const done = new Promise((resolve) => child.once("close", (code) => resolve({ code, stderr })));
  return { child, done };
}

for (const signal of ["SIGINT", "SIGTERM"]) {
  for (const command of ["start", "scan"]) {
    test(`${command} 在 Docker 子进程阻塞时收到 ${signal} 后停止并清理`, { timeout: 15000 }, async (t) => {
      const f = fixture(t);
      if (command === "scan") await lockFixture(t, f);
      const { child, done } = interruptibleCli(t, f, command, `${command}-interrupt`);
      await waitForReady(path.join(f.run, "interrupt-ready"));
      const started = Date.now();
      child.kill(signal);
      const result = await done;
      assert.equal(result.code, signal === "SIGINT" ? 130 : 143, result.stderr);
      assert.ok(Date.now() - started < 6000);
      assert.equal(f.credentials().length, 0, result.stderr);
      const state = JSON.parse(fs.readFileSync(path.join(f.run, "state.json")));
      assert.equal(fs.existsSync(path.join(state.lockRoot, `${state.projectKey}.lock`)), false);
      const calls = fs.readFileSync(path.join(f.run, "docker-calls.jsonl"), "utf8").trim().split("\n").map(JSON.parse);
      if (command === "start") assert.equal(calls.filter((args) => args.includes("run")).length, 1);
      assert.ok(calls.some((args) => args.includes("rm")));
    });
  }
}

test("并发 scan 被拒绝时不清理正在运行的扫描", { timeout: 15000 }, async (t) => {
  const f = fixture(t);
  const lock = await lockFixture(t, f);
  const { child, done } = interruptibleCli(t, f, "scan", "scan-interrupt");
  await waitForReady(path.join(f.run, "interrupt-ready"));
  const callsBefore = fs.readFileSync(path.join(f.run, "docker-calls.jsonl"), "utf8");
  const rejected = f.execute("scan", "owned");
  assert.equal(rejected.status, 1, rejected.stderr);
  assert.match(rejected.stderr, /拒绝并发扫描/);
  assert.match(rejected.stderr, /0123456789abcdef/);
  assert.ok(rejected.stderr.includes(f.run));
  assert.equal(fs.readFileSync(path.join(f.run, "docker-calls.jsonl"), "utf8"), callsBefore);
  assert.equal(child.exitCode, null);
  assert.equal(f.credentials().length, 3);
  assert.ok(fs.existsSync(path.join(lock.lockDir, `.scan-${lock.metadata.holderToken}`)));
  child.kill("SIGTERM");
  const result = await done;
  assert.equal(result.code, 143, result.stderr);
  assert.equal(f.credentials().length, 0);
  assert.equal(fs.existsSync(lock.lockDir), false);
});

test("竞争 start 检查活动资源时 stop 仍能完整清理", { timeout: 15000 }, async (t) => {
  const f = fixture(t);
  const lock = await lockFixture(t, f);
  const { done } = interruptibleCli(t, f, "start", "scan-interrupt");
  await waitForReady(path.join(f.run, "interrupt-ready"));
  const stopped = f.execute("stop", "owned");
  assert.equal(stopped.status, 0, stopped.stderr);
  assert.equal(f.credentials().length, 0);
  assert.equal(fs.existsSync(lock.lockDir), false);
  const result = await done;
  assert.equal(result.code, 1, result.stderr);
});

for (const phase of ["show", "inspect", "info"]) {
  for (const bound of [false, true]) {
    test(`stop 取消 ${phase} 预检并等待退出（Docker ${bound ? "已绑定" : "未绑定"}）`, { timeout: 15000 }, async (t) => {
      const f = fixture(t);
      const stateFile = path.join(f.run, "state.json");
      const state = JSON.parse(fs.readFileSync(stateFile));
      if (!bound) {
        state.docker = null;
        save(stateFile, state);
        for (const name of f.credentials()) fs.unlinkSync(path.join(f.run, name));
      }
      const { done } = interruptibleCli(t, f, "start", `preflight-${phase}`);
      await waitForReady(path.join(f.run, "preflight-ready"));
      const pid = Number(fs.readFileSync(path.join(f.run, "preflight-ready")));
      t.after(() => { try { process.kill(pid, "SIGKILL"); } catch {} });
      const stopped = f.execute("stop", "missing");
      assert.equal(stopped.status, 0, stopped.stderr);
      assert.throws(() => process.kill(pid, 0), { code: "ESRCH" });
      fs.writeFileSync(path.join(f.run, "continue-preflight"), "ready");
      const result = await done;
      assert.equal(result.code, 1, result.stderr);
      assert.match(result.stderr, /已请求停止/);
      assert.equal(fs.existsSync(path.join(f.run, ".lifecycle")), false);
      assert.equal(fs.existsSync(path.join(state.lockRoot, `${state.projectKey}.lock`)), false);
      assert.equal(f.credentials().length, 0);
      const calls = fs.readFileSync(path.join(f.run, "docker-calls.jsonl"), "utf8").trim().split("\n").map(JSON.parse);
      assert.equal(calls.filter((args) => args.includes("run") || args.includes("create")).length, 0);
      for (const artifact of ["source/coverage/lcov.info", "source-manifest.json", "source/sonar-project.properties", "docker-calls.jsonl"])
        assert.ok(fs.existsSync(path.join(f.run, artifact)));
      assert.equal(f.execute("stop", "missing").status, 0);
    });
  }
}

for (const mode of ["start-pull", "start-interrupt"]) {
  test(`stop 取消已获锁的 ${mode} 并等待启动结束才释放租约`, { timeout: 15000 }, async (t) => {
    const f = fixture(t);
    const { child, done } = interruptibleCli(t, f, "start", mode);
    await waitForReady(path.join(f.run, mode === "start-pull" ? "pull-ready" : "interrupt-ready"));
    const state = JSON.parse(fs.readFileSync(path.join(f.run, "state.json")));
    const lockDir = path.join(state.lockRoot, `${state.projectKey}.lock`);
    assert.ok(fs.existsSync(lockDir));
    const stopped = f.execute("stop", mode);
    assert.equal(stopped.status, 0, stopped.stderr);
    fs.writeFileSync(path.join(f.run, "continue-pull"), "ready");
    let timeout;
    const result = await Promise.race([
      done,
      new Promise((resolve) => { timeout = setTimeout(() => { child.kill("SIGKILL"); resolve(null); }, 3000); }),
    ]);
    clearTimeout(timeout);
    assert.ok(result, "startup must finish after stop");
    assert.equal(result.code, 1, result.stderr);
    assert.match(result.stderr, /已请求停止/);
    assert.equal(fs.existsSync(lockDir), false);
    assert.equal(f.credentials().length, 0);
    const calls = fs.readFileSync(path.join(f.run, "docker-calls.jsonl"), "utf8").trim().split("\n").map(JSON.parse);
    if (mode === "start-pull") {
      assert.equal(calls.filter((args) => args.includes("run") || args.includes("create")).length, 0);
      const pullPid = Number(fs.readFileSync(path.join(f.run, "pull-ready")));
      assert.throws(() => process.kill(pullPid, 0), { code: "ESRCH" });
    } else {
      assert.equal(calls.filter((args) => args.includes("run")).length, 1);
      const removed = JSON.parse(fs.readFileSync(path.join(f.run, "removed-resources.json")));
      assert.equal(removed[state.server], true);
      assert.equal(removed[state.network], true);
    }
    // A new lease can now start; the old startup has no remaining commands.
    const next = await lockFixture(t, f);
    assert.ok(fs.existsSync(next.lockDir));
    assert.equal(f.execute("stop", mode).status, 0);
  });
}

test("stop 在删除资源前持有修改锁，竞争 start 不能阻断清理", { timeout: 15000 }, async (t) => {
  const f = fixture(t);
  const lock = await lockFixture(t, f);
  const { done } = interruptibleCli(t, f, "stop", "cleanup-pause");
  await waitForReady(path.join(f.run, "cleanup-ready"));
  try {
    assert.ok(fs.existsSync(path.join(lock.lockDir, ".mutation")));
    const rejected = f.execute("start", "owned");
    assert.equal(rejected.status, 1, rejected.stderr);
    assert.match(rejected.stderr, /已有启动、扫描或清理操作/);
    assert.equal(f.credentials().length, 3);
  } finally {
    fs.writeFileSync(path.join(f.run, "continue-cleanup"), "ready");
  }
  const result = await done;
  assert.equal(result.code, 0, result.stderr);
  assert.equal(f.credentials().length, 0);
  assert.equal(fs.existsSync(lock.lockDir), false);
});

test("启动探测收到响应头后正文停滞仍可中断并清理", { timeout: 15000 }, async (t) => {
  const f = fixture(t);
  let started;
  const ready = new Promise((resolve) => { started = resolve; });
  const server = http.createServer((_, response) => {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.write('{"status":'); started();
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => { server.closeAllConnections(); server.close(); });
  const { child, done } = interruptibleCli(t, f, "start", "start-body", server.address().port);
  await ready;
  await new Promise((resolve) => setTimeout(resolve, 30));
  child.kill("SIGTERM");
  const result = await done;
  assert.equal(result.code, 143, result.stderr);
  assert.equal(f.credentials().length, 0, result.stderr);
  const state = JSON.parse(fs.readFileSync(path.join(f.run, "state.json")));
  assert.equal(fs.existsSync(path.join(state.lockRoot, `${state.projectKey}.lock`)), false);
});

for (const holderExited of [false, true]) {
  test(`stop 等待尚未创建 Scanner 的扫描进程被强杀，再释放项目锁（holder 退出 ${holderExited}）`, { timeout: 15000 }, async (t) => {
    const f = fixture(t);
    const lock = await lockFixture(t, f);
    const { done } = interruptibleCli(t, f, "scan", "scan-pending");
    await waitForReady(path.join(f.run, "scanner-ready"));
    const scannerPid = Number(fs.readFileSync(path.join(f.run, "scanner-ready")));
    t.after(() => { try { process.kill(scannerPid, "SIGKILL"); } catch {} });
    if (holderExited) {
      process.kill(lock.metadata.pid, "SIGTERM");
      const deadline = Date.now() + 3000;
      let alive = true;
      while (alive && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 10));
        try { process.kill(lock.metadata.pid, 0); } catch { alive = false; }
      }
      assert.equal(alive, false, "holder must have exited before reclamation");
      const nextRun = path.join(f.base, "next-run");
      const state = JSON.parse(fs.readFileSync(path.join(f.run, "state.json")));
      const id = "fedcba9876543210", network = `sfl-${id}`;
      save(path.join(nextRun, "state.json"), {
        ...state, id, network, server: `${network}-server`, proxy: `${network}-access`,
        scanner: `${network}-scanner`, project: `sonar-fix-local-${id}`,
      });
      const env = { ...process.env, PATH: `${f.bin}${path.delimiter}${process.env.PATH}`, FAKE_RUN: f.run, FAKE_MODE: "scan-pending" };
      delete env.DOCKER_HOST; delete env.DOCKER_CONTEXT;
      const rejected = spawnSync(process.execPath, [runtime, "start", nextRun], { env, encoding: "utf8", timeout: 6000 });
      assert.equal(rejected.status, 1, rejected.stderr);
      assert.match(rejected.stderr, /扫描尚未确认结束/);
      assert.ok(rejected.stderr.includes(f.run));
      assert.match(rejected.stderr, /0123456789abcdef/);
      assert.equal(fs.existsSync(path.join(f.run, "removed-resources.json")), false);
      assert.equal(f.credentials().length, 3);
      assert.equal(JSON.parse(fs.readFileSync(path.join(lock.lockDir, "lock.json"))).holderToken, lock.metadata.holderToken);
      assert.doesNotThrow(() => process.kill(scannerPid, 0));
    }
    const stopped = f.execute("stop", "scan-pending");
    assert.equal(stopped.status, 0, stopped.stderr);
    // Stop must settle the creator, not merely observe an absent container.
    assert.throws(() => process.kill(scannerPid, 0), { code: "ESRCH" });
    fs.writeFileSync(path.join(f.run, "continue-scanner"), "ready");
    const result = await done;
    assert.equal(result.code, 1, result.stderr);
    assert.match(result.stderr, /已请求停止/);
    assert.equal(fs.existsSync(path.join(f.run, "created-resources.json")), false);
    assert.equal(fs.existsSync(lock.lockDir), false);
    assert.equal(fs.existsSync(path.join(f.run, ".lifecycle")), false);
    assert.equal(f.credentials().length, 0);
    for (const artifact of ["source/coverage/lcov.info", "coverage-receipt.json", "source-manifest.json", "config/settings.json", "scanner.log"])
      assert.ok(fs.existsSync(path.join(f.run, artifact)), artifact);
    const replacement = await lockFixture(t, f);
    assert.notEqual(replacement.metadata.holderToken, lock.metadata.holderToken);
    assert.equal(f.execute("stop", "scan-pending").status, 0);
  });
}

test("stop 取消等待 HTTP 的 scan 后才允许替代租约", { timeout: 15000 }, async (t) => {
  const f = fixture(t);
  const original = await lockFixture(t, f);
  let response, reached;
  const ready = new Promise((resolve) => { reached = resolve; });
  const server = http.createServer((_, reply) => { response = reply; reached(); });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => { server.closeAllConnections(); server.close(); });
  save(path.join(f.run, "credentials.json"), { origin: `http://127.0.0.1:${server.address().port}`, token: "old-token" });
  const { done } = interruptibleCli(t, f, "scan", "scan-success");
  await ready;
  assert.equal(f.execute("stop", "owned").status, 0);
  const result = await done;
  assert.equal(result.code, 1, result.stderr);
  assert.match(result.stderr, /已请求停止/);
  assert.equal(fs.existsSync(original.lockDir), false);
  assert.equal(f.credentials().length, 0);
  const replacement = await lockFixture(t, f);
  assert.notEqual(replacement.metadata.holderToken, original.metadata.holderToken);
  fs.unlinkSync(path.join(f.run, "removed-resources.json"));
  for (const name of ["credentials.json", "scanner.env", "pending-password.json"])
    fs.writeFileSync(path.join(f.run, name), "replacement-credential");
  const callsBefore = fs.readFileSync(path.join(f.run, "docker-calls.jsonl"), "utf8");
  response.writeHead(503); response.end();
  assert.equal(fs.readFileSync(path.join(f.run, "docker-calls.jsonl"), "utf8"), callsBefore);
  assert.equal(f.credentials().length, 3);
  assert.equal(fs.readFileSync(path.join(f.run, "credentials.json"), "utf8"), "replacement-credential");
  assert.equal(JSON.parse(fs.readFileSync(path.join(replacement.lockDir, "lock.json"))).holderToken, replacement.metadata.holderToken);
  assert.equal(fs.existsSync(path.join(f.run, ".lifecycle")), false);
  assert.equal(f.execute("stop", "owned").status, 0);
});

for (const [mode, hasOriginalLease] of [["coverage", true], ["coverage", false], ["collect", true]]) {
  test(`旧 ${mode} 失败不清理后来建立的租约（原租约 ${hasOriginalLease}）`, { timeout: 15000 }, async (t) => {
    const f = fixture(t);
    if (hasOriginalLease) await lockFixture(t, f);
    let response, reached;
    const ready = new Promise((resolve) => { reached = resolve; });
    const server = http.createServer((_, reply) => { response = reply; reached(); });
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    t.after(() => { server.closeAllConnections(); server.close(); });
    save(path.join(f.run, "credentials.json"), { origin: `http://127.0.0.1:${server.address().port}`, token: "old-token" });
    const local = fileURLToPath(new URL("../scripts/local.mjs", import.meta.url));
    const worker = `const fs=require('node:fs');fs.writeFileSync(${JSON.stringify(path.join(f.run, "coverage-ready"))},'ready');setInterval(()=>{if(fs.existsSync(${JSON.stringify(path.join(f.run, "continue-coverage"))}))process.exit(7);},10);`;
    const args = mode === "coverage" ? [mode, f.run, "coverage/lcov.info", "--", process.execPath, "-e", worker] : [mode, f.run];
    const env = { ...process.env, PATH: `${f.bin}${path.delimiter}${process.env.PATH}`, FAKE_MODE: "owned", FAKE_RUN: f.run };
    delete env.DOCKER_HOST; delete env.DOCKER_CONTEXT;
    const child = spawn(process.execPath, [local, ...args], { env, stdio: ["ignore", "ignore", "pipe"], timeout: 12000, killSignal: "SIGKILL" });
    t.after(() => { if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL"); });
    let stderr = "";
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    const done = new Promise((resolve, reject) => {
      child.once("error", reject);
      child.once("close", (code) => resolve(code));
    });
    if (mode === "coverage") await waitForReady(path.join(f.run, "coverage-ready"));
    else await ready;
    assert.equal(f.execute("stop", "owned").status, 0);
    const replacement = await lockFixture(t, f);
    fs.unlinkSync(path.join(f.run, "removed-resources.json"));
    for (const name of ["credentials.json", "scanner.env", "pending-password.json"])
      fs.writeFileSync(path.join(f.run, name), "replacement-credential");
    const callsBefore = fs.readFileSync(path.join(f.run, "docker-calls.jsonl"), "utf8");
    if (mode === "coverage") fs.writeFileSync(path.join(f.run, "continue-coverage"), "ready");
    else { response.writeHead(503); response.end(); }
    assert.equal(await done, 1, stderr);
    assert.match(stderr, /租约已变化/);
    assert.equal(fs.readFileSync(path.join(f.run, "docker-calls.jsonl"), "utf8"), callsBefore);
    assert.equal(f.credentials().length, 3);
    assert.equal(JSON.parse(fs.readFileSync(path.join(replacement.lockDir, "lock.json"))).holderToken, replacement.metadata.holderToken);
    assert.equal(fs.existsSync(path.join(f.run, ".lifecycle")), false);
    assert.equal(f.execute("stop", "owned").status, 0);
  });
}

for (const mode of ["coverage", "collect"]) {
  test(`local CLI ${mode} 失败后完成清理而非陷入模块循环等待`, { timeout: 10000 }, async (t) => {
    const f = fixture(t);
    const lock = await lockFixture(t, f);
    const local = fileURLToPath(new URL("../scripts/local.mjs", import.meta.url));
    const env = { ...process.env, PATH: `${f.bin}${path.delimiter}${process.env.PATH}`, FAKE_MODE: "owned", FAKE_RUN: f.run };
    delete env.DOCKER_HOST; delete env.DOCKER_CONTEXT;
    const args = mode === "coverage" ? ["coverage", f.run, "coverage/lcov.info", "--", process.execPath, "-e", "process.exit(7)"] : ["collect", f.run];
    const result = spawnSync(process.execPath, [local, ...args], { env, encoding: "utf8", timeout: 6000 });
    assert.equal(result.status, 1, result.stderr);
    assert.equal(f.credentials().length, 0, result.stderr);
    assert.equal(fs.existsSync(lock.lockDir), false);
  });
}

for (const signal of ["SIGINT", "SIGTERM"]) {
  for (const cleanupFails of [false, true]) {
    test(`coverage CLI 收到 ${signal} 后保留中断退出码，清理${cleanupFails ? "失败保留恢复信息" : "成功释放资源"}`, { timeout: 15000 }, async (t) => {
      const f = fixture(t);
      const lock = await lockFixture(t, f);
      const local = fileURLToPath(new URL("../scripts/local.mjs", import.meta.url));
      const ready = path.join(f.run, "coverage-worker.pid");
      const worker = `process.on('SIGINT',()=>{});process.on('SIGTERM',()=>{});require('node:fs').writeFileSync(${JSON.stringify(ready)},String(process.pid));setInterval(()=>{},1000);`;
      const env = { ...process.env, PATH: `${f.bin}${path.delimiter}${process.env.PATH}`, FAKE_MODE: cleanupFails ? "rm-fails" : "owned", FAKE_RUN: f.run };
      delete env.DOCKER_HOST; delete env.DOCKER_CONTEXT;
      const child = spawn(process.execPath, [local, "coverage", f.run, "coverage/lcov.info", "--", process.execPath, "-e", worker], {
        env, stdio: ["ignore", "ignore", "pipe"], timeout: 12000, killSignal: "SIGKILL",
      });
      t.after(() => { if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL"); });
      let stderr = "";
      child.stderr.on("data", (chunk) => { stderr += chunk; });
      const done = new Promise((resolve, reject) => {
        child.once("error", reject);
        child.once("close", (code, exitSignal) => resolve({ code, exitSignal }));
      });
      await waitForReady(ready);
      const workerPid = Number(fs.readFileSync(ready));
      t.after(() => { try { process.kill(workerPid, "SIGKILL"); } catch {} });
      child.kill(signal);
      const result = await done;
      assert.equal(result.exitSignal, null, stderr);
      assert.equal(result.code, signal === "SIGINT" ? 130 : 143, stderr);
      assert.throws(() => process.kill(workerPid, 0), { code: "ESRCH" });
      assert.equal(fs.existsSync(path.join(f.run, "coverage-receipt.json")), false);
      assert.equal(fs.existsSync(lock.lockDir), cleanupFails, stderr);
      assert.equal(f.credentials().length, cleanupFails ? 3 : 0, stderr);
      if (cleanupFails) assert.match(stderr, /当前 run 清理失败/);
      else {
        const removed = JSON.parse(fs.readFileSync(path.join(f.run, "removed-resources.json")));
        for (const name of ["sfl-0123456789abcdef-server", "sfl-0123456789abcdef-access", "sfl-0123456789abcdef-scanner", "sfl-0123456789abcdef"])
          assert.equal(removed[name], true, `resource not removed: ${name}`);
      }
      for (const name of ["coverage.log", "source/coverage/lcov.info", "source-manifest.json", "source/sonar-project.properties"])
        assert.ok(fs.existsSync(path.join(f.run, name)), `retained artifact missing: ${name}`);
    });
  }
}

test("Scanner 成功后轮询并保存当前结果，stop 才释放资源和锁", { timeout: 15000 }, async (t) => {
  const f = fixture(t);
  const lock = await lockFixture(t, f);
  const requested = [];
  const server = http.createServer((request, response) => {
    const endpoint = new URL(request.url, "http://127.0.0.1").pathname;
    requested.push(endpoint);
    const data = {
      "/api/ce/task": { task: { status: "SUCCESS", analysisId: "analysis-current" } },
      "/api/ce/component": { current: { status: "SUCCESS", analysisId: "analysis-current" }, queue: [] },
      "/api/issues/search": { paging: { total: 0 }, issues: [] },
      "/api/hotspots/search": { paging: { total: 0 }, hotspots: [] },
      "/api/measures/component": { component: { measures: [] } },
      "/api/qualitygates/project_status": { projectStatus: { status: "OK", conditions: [] } },
    }[endpoint];
    response.writeHead(data ? 200 : 404, { "Content-Type": "application/json" });
    response.end(JSON.stringify(data || {}));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => { server.closeAllConnections(); server.close(); });
  save(path.join(f.run, "credentials.json"), { origin: `http://127.0.0.1:${server.address().port}`, token: "test-token" });
  const { done } = interruptibleCli(t, f, "scan", "scan-success");
  const result = await done;
  assert.equal(result.code, 0, result.stderr);
  const saved = JSON.parse(fs.readFileSync(path.join(f.run, "result.json")));
  assert.equal(saved.analysisId, "analysis-current");
  assert.equal(saved.snapshotMatchesCurrent, true);
  assert.ok(requested.includes("/api/ce/task"));
  assert.ok(fs.existsSync(lock.lockDir));
  assert.equal(fs.existsSync(path.join(lock.lockDir, `.scan-${lock.metadata.holderToken}`)), false);
  const stopped = f.execute("stop", "owned");
  assert.equal(stopped.status, 0, stopped.stderr);
  assert.equal(fs.existsSync(lock.lockDir), false);
  assert.equal(f.credentials().length, 0);
  assert.ok(fs.existsSync(path.join(f.run, "result.json")));
});
