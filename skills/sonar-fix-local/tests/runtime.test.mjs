import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import crypto from "node:crypto";
import { execFileSync, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { fingerprint, save } from "../scripts/local.mjs";

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
const raw = process.argv.slice(2), args = raw[0] === '--host' ? raw.slice(2) : raw, run = process.env.FAKE_RUN, mode = process.env.FAKE_MODE;
fs.appendFileSync(path.join(run, 'docker-calls.jsonl'), JSON.stringify(raw)+'\\n');
const emit = value => process.stdout.write(JSON.stringify(value));
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
} else if (args[0] === 'inspect') {
  emit([{Config:{Labels:{'sonar-fix-local.run':'0123456789abcdef'}},NetworkSettings:{Networks:{'sfl-0123456789abcdef':{IPAddress:'172.18.0.2'}}}}]);
} else if (args[0] === 'network' && args[1] === 'inspect') {
  emit([{Internal:true,Labels:{'sonar-fix-local.run':'0123456789abcdef'}}]);
} else if (args[0] === 'run') {
  fs.writeFileSync(path.join(run, 'scanner-args.json'), JSON.stringify(raw)); process.exit(7);
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
    run,
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
