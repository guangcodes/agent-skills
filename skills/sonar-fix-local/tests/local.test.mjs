import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import http from "node:http";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import {
  onlineClient,
  localOrigin,
  localClient,
  configureGate,
  bootstrap,
  pages,
  save,
  fingerprint,
  checkSnapshot,
  normalizeLcov,
  generateCoverage,
  collectLocal,
} from "../scripts/local.mjs";
import { stop } from "../scripts/runtime.mjs";

function fixture(t) {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), "sonar-local-test-"));
  t.after(() => fs.rmSync(base, { recursive: true, force: true }));
  const root = path.join(base, "project");
  const run = path.join(base, "run");
  fs.mkdirSync(root);
  fs.mkdirSync(path.join(run, "source/coverage"), { recursive: true });
  execFileSync("git", ["init", "-q", "--template="], { cwd: root });
  fs.writeFileSync(path.join(root, "business.ts"), "export const total = 1;\n");
  save(path.join(run, "state.json"), { root, project: "local-test" });
  save(path.join(run, "source-manifest.json"), fingerprint(root));
  return { base, root, run };
}
test("DOCKER_HOST 远端覆盖项在接触 Docker 前被拒绝", (t) => {
  const before = process.env.DOCKER_HOST;
  t.after(() => {
    if (before === undefined) delete process.env.DOCKER_HOST;
    else process.env.DOCKER_HOST = before;
  });
  process.env.DOCKER_HOST = "tcp://remote.example:2376";
  assert.throws(() => stop("/nonexistent-run"), /DOCKER_HOST/);
});
test("线上客户端始终 GET，禁止重定向，拒绝其他主机", async () => {
  let request;
  const get = onlineClient(
    { host: "https://sonar.rydev.work", token: "private-test-token" },
    async (u, options) => {
      request = { u, options };
      return { ok: true, status: 200, json: async () => ({ profiles: [] }) };
    },
  );
  await get("/api/qualityprofiles/search", { project: "admin" });
  assert.equal(request.u.origin, "https://sonar.rydev.work");
  assert.equal(request.options.method, "GET");
  assert.equal(request.options.redirect, "error");
  assert.equal(request.options.body, undefined);
  assert.throws(
    () => onlineClient({ host: "https://other.example", token: "secret" }),
    /其他线上/,
  );
  await assert.rejects(get("//other.example/api/test"), /无效 API/);
});
test("本机写 API 拒绝线上、主机别名与重定向；错误不泄露正文", async () => {
  for (const url of [
    "https://sonar.rydev.work",
    "http://localhost:19090",
    "http://127.0.0.1:19090/path",
    "http://127.0.0.1:19090/?target=online",
    "http://secret@127.0.0.1:19090",
  ])
    assert.throws(() => localOrigin(url));
  let request;
  const api = localClient(
    "http://127.0.0.1:19090",
    "Bearer local-token",
    async (url, options) => {
      request = options;
      return {
        ok: false,
        status: 403,
        text: async () => "secret response token",
      };
    },
  );
  await assert.rejects(
    api("/api/projects/create", { project: "local" }, true),
    (error) =>
      !error.message.includes("secret") && error.message.includes("403"),
  );
  assert.equal(request.method, "POST");
  assert.equal(request.redirect, "error");
});
test("本机分页读取所有记录，并拒绝提前结束或变化的总数", async () => {
  const result = await pages(
    async (_, p) => ({
      paging: { total: 2 },
      issues: [{ rule: "ts:S" + p.p }],
    }),
    "/api/issues/search",
    {},
    "issues",
  );
  assert.equal(result.length, 2);
  await assert.rejects(
    pages(
      async () => ({ paging: { total: 2 }, issues: [] }),
      "/api/issues/search",
      {},
      "issues",
    ),
    /提前结束/,
  );
  let i = 0;
  await assert.rejects(
    pages(
      async () => ({ paging: { total: ++i + 1 }, issues: [{}] }),
      "/api/issues/search",
      {},
      "issues",
    ),
    /正在变化/,
  );
});
test("新建本机 Gate 的默认条件不会重复添加，阈值不同的条件会替换", async () => {
  let current = [
    { id: "1", metric: "new_coverage", op: "LT", error: "80" },
    { id: "2", metric: "new_violations", op: "GT", error: "1" },
    { id: "3", metric: "unwanted", op: "GT", error: "0" },
  ];
  const desired = [
    { metric: "new_coverage", op: "LT", error: "80" },
    { metric: "new_violations", op: "GT", error: "0" },
  ];
  await configureGate(
    async (endpoint, params) => {
      if (endpoint.endsWith("/show")) return { conditions: [...current] };
      if (endpoint.endsWith("/delete_condition"))
        current = current.filter((x) => x.id !== params.id);
      if (endpoint.endsWith("/create_condition")) {
        assert.ok(!current.some((x) => x.metric === params.metric));
        current.push(params);
      }
      return {};
    },
    "Local",
    desired,
    "local",
  );
  assert.deepEqual(
    current.map(({ metric, op, error }) => ({ metric, op, error })),
    desired,
  );
});
test("源码编辑、删除和新增文件均使快照失效", (t) => {
  const { root, run } = fixture(t);
  assert.equal(checkSnapshot(run), true);
  fs.writeFileSync(path.join(root, "business.ts"), "export const total = 2;");
  assert.equal(checkSnapshot(run), false);
  fs.writeFileSync(path.join(root, "business.ts"), "export const total = 1;\n");
  fs.writeFileSync(path.join(root, "new.ts"), "export {};");
  assert.equal(checkSnapshot(run), false);
  fs.unlinkSync(path.join(root, "new.ts"));
  fs.unlinkSync(path.join(root, "business.ts"));
  assert.equal(checkSnapshot(run), false);
});
test("LCOV 将宿主绝对路径改为快照相对路径，并拒绝外部路径", (t) => {
  const { root } = fixture(t);
  const result = normalizeLcov(
    Buffer.from(`SF:${root}/business.ts\nDA:1,1\nend_of_record\n`),
    root,
    fingerprint(root).files,
  );
  assert.match(result.toString(), /^SF:business\.ts/m);
  assert.throws(
    () =>
      normalizeLcov(
        Buffer.from("SF:../secret.ts\nend_of_record"),
        root,
        fingerprint(root).files,
      ),
    /不在源码快照/,
  );
});
test("覆盖率命令失败时不会给旧报告签发有效凭证", async (t) => {
  const { base, run } = fixture(t);
  const report = path.join(base, "old.info");
  fs.writeFileSync(report, "SF:business.ts\nend_of_record\n");
  await assert.rejects(
    generateCoverage(run, report, [process.execPath, "-e", "process.exit(1)"]),
    /命令失败/,
  );
  assert.equal(fs.existsSync(path.join(run, "coverage-receipt.json")), false);
});
for (const [name, data] of [
  ["尾部截断", "SF:business.ts\nDA:1,1\nend_of_record\nSF:business.ts\nDA:1,1\n"],
  ["中间记录未终止", "SF:business.ts\nDA:1,1\nSF:business.ts\nDA:1,1\nend_of_record\n"],
  ["孤立终止标记", "end_of_record\nSF:business.ts\nDA:1,1\nend_of_record\n"],
])
  test(`拒绝不完整 LCOV：${name}`, async (t) => {
    const { base, run } = fixture(t);
    const report = path.join(base, "incomplete.info");
    await assert.rejects(
      generateCoverage(run, report, [
        process.execPath, "-e",
        'require("node:fs").writeFileSync(process.argv[1],process.argv[2])',
        report, data,
      ]),
      /LCOV 不完整/,
    );
    assert.equal(fs.existsSync(path.join(run, "coverage-receipt.json")), false);
  });

test("完整的多记录 LCOV 支持 CRLF 和末尾无换行", (t) => {
  const { root } = fixture(t);
  const record = `SF:${root}/business.ts\r\nDA:1,1\r\nend_of_record`;
  const normalized = normalizeLcov(Buffer.from(record + "\r\n" + record), root, fingerprint(root).files);
  assert.equal(normalized.toString().match(/^SF:business\.ts$/gm)?.length, 2);
});
test("生成覆盖率后只有对应源码快照能获得凭证", async (t) => {
  const { base, run } = fixture(t);
  const report = path.join(base, "fresh.info");
  await generateCoverage(run, report, [
    process.execPath,
    "-e",
    'require("node:fs").writeFileSync(process.argv[1],"SF:business.ts\\nDA:1,1\\nend_of_record\\n")',
    report,
  ]);
  assert.equal(
    JSON.parse(fs.readFileSync(path.join(run, "coverage-receipt.json")))
      .sourceFingerprint,
    JSON.parse(fs.readFileSync(path.join(run, "source-manifest.json")))
      .sourceFingerprint,
  );
});
for (const failure of ["测试失败", "无法启动", "报告无效"]) {
  test(`覆盖率成功后重跑${failure}会使旧凭证失效`, async (t) => {
    const { base, run } = fixture(t);
    const report = path.join(base, "retry.info");
    const successfulCommand = [
      process.execPath,
      "-e",
      'require("node:fs").writeFileSync(process.argv[1],"SF:business.ts\\nDA:1,1\\nend_of_record\\n")',
      report,
    ];
    const receipt = path.join(run, "coverage-receipt.json");
    await generateCoverage(run, report, successfulCommand);
    assert.equal(fs.existsSync(receipt), true);
    const failedCommand =
      failure === "测试失败"
        ? [process.execPath, "-e", "process.exit(1)"]
        : failure === "无法启动"
          ? [path.join(base, "missing-test-runner")]
          : [
              process.execPath,
              "-e",
              'require("node:fs").writeFileSync(process.argv[1],"invalid coverage report")',
              report,
            ];
    await assert.rejects(generateCoverage(run, report, failedCommand));
    // 旧 LCOV 可保留用于诊断，但没有凭证时 Scanner 必须拒绝使用。
    assert.equal(
      fs.existsSync(path.join(run, "source/coverage/lcov.info")),
      true,
    );
    assert.equal(fs.existsSync(receipt), false);
    await generateCoverage(run, report, successfulCommand);
    assert.equal(fs.existsSync(receipt), true);
  });
}
test("完整本机结果保留非空热点 ruleKey；空门禁不能报告新代码通过", async (t) => {
  const { run } = fixture(t);
  save(path.join(run, "credentials.json"), {
    origin: "http://127.0.0.1:19090",
    token: "local-only",
  });
  save(path.join(run, "analysis-success.json"), { analysisId: "analysis-1" });
  const result = await collectLocal(run, {
    fetcher: async (u) => {
      if (u.pathname === "/api/hotspots/search") {
        assert.equal(u.searchParams.get("project"), "local-test");
        assert.equal(u.searchParams.has("projectKey"), false);
      }
      const data =
        u.pathname === "/api/ce/component"
          ? {
              current: { status: "SUCCESS", analysisId: "analysis-1" },
              queue: [],
            }
          : u.pathname === "/api/issues/search"
            ? {
                paging: { total: 1 },
                issues: [
                  {
                    rule: "docker:S7020",
                    component: "local:Dockerfile",
                    line: 35,
                  },
                ],
              }
            : u.pathname === "/api/hotspots/search"
              ? {
                  paging: { total: 1 },
                  hotspots: [
                    {
                      key: "hotspot-1",
                      ruleKey: "typescript:S4790",
                      component: "local:business.ts",
                      line: 1,
                    },
                  ],
                }
              : u.pathname === "/api/qualitygates/project_status"
                ? { projectStatus: { status: "OK", conditions: [] } }
                : { component: { measures: [] } };
      return { ok: true, text: async () => JSON.stringify(data) };
    },
  });
  assert.equal(result.hotspots[0].ruleKey, "typescript:S4790");
  assert.equal(result.newCodeGateEvaluated, false);
  assert.equal(result.testExecutionReportsImported, false);
  assert.match(result.testExecutionNote, /不导入测试执行 XML/);
  assert.equal(result.snapshotMatchesCurrent, true);
  assert.equal(result.issues.length, 1);
});
for (const language of ["ts", "cloudformation", "kubernetes", "azureresourcemanager", "ansible", "githubactions", "azurepipelines", "json"])
test(`${language} 规则比对按语义处理导出顺序，参数或分析器变化时失败`, (t) => {
  const { run } = fixture(t);
  const config = path.join(run, "config");
  fs.mkdirSync(config);
  const xml = (n) =>
    `<profile><name>${n}</name><rules><rule><repositoryKey>typescript</repositoryKey><key>S107</key><priority>MAJOR</priority><type>CODE_SMELL</type><parameters><parameter><key>max</key><value>7</value></parameter></parameters></rule></rules></profile>`;
  fs.writeFileSync(path.join(config, `${language}.xml`), xml("Online"));
  fs.writeFileSync(path.join(config, `local-${language}.xml`), xml("Local"));
  save(path.join(config, "profiles.json"), [
    { language, activeRules: 1 },
  ]);
  const plugins = {
    plugins: ["javascript", "web", "iac", "text"].map((key) => ({
      key,
      version: "1.0",
    })),
  };
  for (const name of ["plugins", "local-plugins"])
    save(path.join(config, name + ".json"), plugins);
  for (const name of ["gate", "local-gate"])
    save(path.join(config, name + ".json"), {
      conditions: [{ metric: "new_coverage", op: "LT", error: "80" }],
    });
  const helper = fileURLToPath(
    new URL("../scripts/profile-parity.py", import.meta.url),
  );
  execFileSync("python3", [helper, run]);
  fs.writeFileSync(
    path.join(config, `local-${language}.xml`),
    xml("Local").replace("<value>7</value>", "<value>8</value>"),
  );
  assert.throws(() =>
    execFileSync("python3", [helper, run], { stdio: "pipe" }),
  );
  fs.writeFileSync(path.join(config, `local-${language}.xml`), xml("Local"));
  plugins.plugins[0].version = "2.0";
  save(path.join(config, "local-plugins.json"), plugins);
  assert.throws(() =>
    execFileSync("python3", [helper, run], { stdio: "pipe" }),
  );
});

for (const mismatch of [false, true])
  test(
    mismatch
      ? "本机配置读回不一致时拒绝完成"
      : "本机配置保留全局作用域与多值项目设置",
    async (t) => {
      const { run, root } = fixture(t);
      const project = "local-test";
      const plugins = {
        plugins: ["javascript", "web", "iac", "text"].map((key) => ({
          key,
          version: "1.0",
        })),
      };
      save(path.join(run, "state.json"), {
        root,
        project,
        id: "0123456789abcdef",
        version: "26.10.0.133078",
      });
      const profiles = ["json", "cloudformation", "kubernetes", "azureresourcemanager", "ansible", "githubactions", "azurepipelines"]
        .map((language) => ({ language, name: language, activeRules: 0 }));
      const profileXml = (name) => `<profile><name>${name}</name><rules/></profile>`;
      save(path.join(run, "config/profiles.json"), profiles);
      for (const profile of profiles)
        fs.writeFileSync(path.join(run, "config", `${profile.language}.xml`), profileXml(profile.name));
      save(path.join(run, "config/plugins.json"), plugins);
      save(path.join(run, "config/gate.json"), { conditions: [] });
      const settings = [
        {
          key: "sonar.global.exclusions",
          values: ["**/dist/**", "**/node_modules/**"],
          inherited: true,
        },
        {
          key: "sonar.global.test.exclusions",
          values: ["**/test-utils/**"],
          inherited: true,
        },
        {
          key: "sonar.exclusions",
          values: ["project-generated/**", "**/comma,name/**"],
        },
        {
          key: "sonar.coverage.exclusions",
          value: "legacy/**",
          inherited: true,
        },
        {
          key: "sonar.test.inclusions",
          values: ["tests/**/*.ts"],
          inherited: true,
        },
        {
          key: "sonar.javascript.exclusions",
          values: ["**/frontend-generated/**"],
          inherited: true,
        },
        ...["sonar.javascript.file.suffixes", "sonar.typescript.file.suffixes", "sonar.css.file.suffixes", "sonar.html.file.suffixes", "sonar.yaml.file.suffixes", "sonar.docker.file.patterns"]
          .map((key) => ({ key, values: ["fixture-scope"], inherited: true })),
        { key: "sonar.text.activate", value: "true", inherited: true },
        { key: "sonar.text.inclusions.activate", value: "false", inherited: true },
        { key: "sonar.text.inclusions", values: ["**/*.custom-config"], inherited: true },
        { key: "sonar.text.excluded.file.suffixes", values: [".binary"], inherited: true },
        { key: "sonar.javascript.maxFileSize", value: "2000", inherited: true },
        { key: "sonar.filesize.limit", value: "30", inherited: true },
        { key: "sonar.cloudformation.file.identifier", value: "Resources", inherited: true },
        { key: "sonar.kubernetes.activate", value: "false", inherited: true },
      ];
      save(path.join(run, "config/settings.json"), {
        settings: [
          ...settings,
          ...["sonar.issue.ignore.multicriteria", "sonar.issue.ignore.allfile", "sonar.issue.ignore.block", "sonar.issue.enforce.multicriteria"]
            .map((key) => ({ key, fieldValues: [] })),
        ],
      });
      const applied = new Map();
      const restored = new Set();
      const bindings = new Map();
      const server = http.createServer(async (req, res) => {
        let data = "";
        for await (const chunk of req) data += chunk;
        const params = new URLSearchParams(data);
        const endpoint = new URL(req.url, "http://127.0.0.1").pathname;
        let body = {};
        if (endpoint === "/api/system/status")
          body = { status: "UP", version: "26.10.0.133078" };
        if (endpoint === "/api/user_tokens/generate")
          body = { token: "local-test-token" };
        if (endpoint === "/api/qualitygates/show") body = { conditions: [] };
        if (endpoint === "/api/plugins/installed") body = plugins;
        if (endpoint === "/api/qualityprofiles/restore") {
          for (const profile of profiles)
            if (data.includes(`<name>Local ${profile.language} 0123456789abcdef</name>`))
              restored.add(profile.language);
        }
        if (endpoint === "/api/qualityprofiles/add_project")
          bindings.set(params.get("language"), { project: params.get("project"), name: params.get("qualityProfile") });
        if (endpoint === "/api/qualityprofiles/backup") {
          const language = new URL(req.url, "http://127.0.0.1").searchParams.get("language");
          res.setHeader("Content-Type", "application/xml");
          res.end(profileXml(`Local ${language} 0123456789abcdef`));
          return;
        }
        if (endpoint === "/api/settings/set") {
          const key = params.get("key");
          const isGlobal = key.startsWith("sonar.global.");
          if (
            isGlobal
              ? params.has("component")
              : params.get("component") !== project
          ) {
            res.statusCode = 400;
            body = { errors: [{ msg: "Setting scope mismatch" }] };
          } else
            applied.set(
              key,
              params.has("values")
                ? {
                    values: params.getAll("values"),
                    component: params.get("component") ?? undefined,
                  }
                : {
                    value: params.get("value"),
                    component: params.get("component") ?? undefined,
                  },
            );
        }
        if (endpoint === "/api/settings/values") {
          body = {
            settings: [...applied].map(([key, setting]) => ({
              key,
              ...setting,
            })),
          };
          if (mismatch) body.settings[0].values = ["different/**"];
        }
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify(body));
      });
      t.after(() => new Promise((resolve) => server.close(resolve)));
      await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
      const start = () =>
        bootstrap(run, `http://127.0.0.1:${server.address().port}`);
      if (mismatch) {
        await assert.rejects(start(), /设置不一致/);
        assert.equal(
          fs.existsSync(path.join(run, "config-parity.json")),
          false,
        );
        assert.equal(fs.existsSync(path.join(run, "scanner.env")), false);
        return;
      }
      await start();
      assert.deepEqual([...restored].sort(), profiles.map((profile) => profile.language).sort());
      for (const profile of profiles)
        assert.deepEqual(bindings.get(profile.language), {
          project,
          name: `Local ${profile.language} 0123456789abcdef`,
        });
      assert.equal(applied.size, settings.length);
      for (const setting of settings) {
        const actual = applied.get(setting.key);
        assert.equal(
          actual.component,
          setting.key.startsWith("sonar.global.") ? undefined : project,
        );
        assert.deepEqual(
          actual.values ?? actual.value,
          setting.values ?? setting.value,
        );
      }
      assert.equal(
        fs.existsSync(path.join(run, "config/local-settings.json")),
        true,
      );
      assert.equal(fs.existsSync(path.join(run, "config-parity.json")), true);
      assert.equal(fs.existsSync(path.join(run, "scanner.env")), true);
    },
  );

test("两秒内未重写的旧 LCOV 不得获得新凭证", async (t) => {
  const { base, run } = fixture(t);
  const report = path.join(base, "unchanged.info");
  const data = "SF:business.ts\nDA:1,1\nend_of_record\n";
  fs.writeFileSync(report, data);
  save(path.join(run, "coverage-receipt.json"), { previousAttempt: true });
  await assert.rejects(
    generateCoverage(run, report, [process.execPath, "-e", "process.exit(0)"]),
    /重新生成|未生成新的 LCOV/,
  );
  assert.equal(fs.readFileSync(report, "utf8"), data);
  assert.equal(fs.existsSync(path.join(run, "coverage-receipt.json")), false);
});

for (const replacement of [false, true])
  test(`相同内容的 LCOV ${replacement ? "原子替换" : "重新写入"}仍可签发凭证`, async (t) => {
    const { base, run } = fixture(t);
    const report = path.join(base, "regenerated.info");
    const data = "SF:business.ts\nDA:1,1\nend_of_record\n";
    fs.writeFileSync(report, data);
    const regenerate = replacement
      ? 'const fs=require("node:fs");const p=process.argv[1];const old=fs.statSync(p);fs.copyFileSync(p,p+".new");fs.utimesSync(p+".new",old.atime,old.mtime);fs.renameSync(p+".new",p);'
      : 'const fs=require("node:fs");const p=process.argv[1];fs.writeFileSync(p,fs.readFileSync(p));';
    await generateCoverage(run, report, [
      process.execPath,
      "-e",
      regenerate,
      report,
    ]);
    assert.equal(fs.existsSync(path.join(run, "coverage-receipt.json")), true);
    assert.equal(fs.readFileSync(report, "utf8"), data);
  });

test("只改变旧 LCOV 权限不能作为重新生成的证据", async (t) => {
  const { base, run } = fixture(t);
  const report = path.join(base, "permissions-only.info");
  fs.writeFileSync(report, "SF:business.ts\nDA:1,1\nend_of_record\n", {
    mode: 0o644,
  });
  await assert.rejects(
    generateCoverage(run, report, [
      process.execPath,
      "-e",
      'require("node:fs").chmodSync(process.argv[1], 0o600)',
      report,
    ]),
    /重新生成|未生成新的 LCOV/,
  );
  assert.equal(fs.existsSync(path.join(run, "coverage-receipt.json")), false);
});
