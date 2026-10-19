import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { prepare, read } from "../scripts/local.mjs";

function fixture(t, settings, { yaml = false, languages = [] } = {}) {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), "sonar-prepare-test-"));
  t.after(() => fs.rmSync(base, { recursive: true, force: true }));
  const root = path.join(base, "project");
  fs.mkdirSync(root);
  execFileSync("git", ["init", "-q", "--template="], { cwd: root });
  // 配置导出测试只需要固定的 HEAD 标识，不创建提交。
  fs.writeFileSync(
    path.join(root, ".git/HEAD"),
    "0123456789abcdef0123456789abcdef01234567\n",
  );
  fs.writeFileSync(path.join(root, ".gitignore"), ".env.local\n");
  fs.writeFileSync(path.join(root, ".env.local"), "SONAR_TOKEN=squ_fixture\n");
  fs.writeFileSync(
    path.join(root, "sonar-project.properties"),
    "sonar.projectKey=fixture\n",
  );
  fs.writeFileSync(path.join(root, "business.ts"), "export const total = 1;\n");
  const previous = {
    SONAR_TOKEN: process.env.SONAR_TOKEN,
    SONAR_HOST_URL: process.env.SONAR_HOST_URL,
  };
  Object.assign(process.env, {
    SONAR_TOKEN: "squ_fixture",
    SONAR_HOST_URL: "https://sonar.rydev.work",
  });
  t.after(() => {
    for (const [key, value] of Object.entries(previous))
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
  });
  const calls = [];
  const fetcher = async (url, options) => {
    calls.push(url);
    assert.equal(options.method, "GET");
    assert.equal(options.redirect, "error");
    assert.equal(url.origin, "https://sonar.rydev.work");
    if (url.pathname === "/api/server/version")
      return {
        ok: true,
        status: 200,
        arrayBuffer: async () => Buffer.from("26.10.0.133078"),
      };
    if (url.pathname === "/api/qualityprofiles/backup")
      return {
        ok: true,
        status: 200,
        arrayBuffer: async () => Buffer.from("<profile><rules/></profile>"),
      };
    if (url.pathname === "/api/plugins/download") {
      assert.equal(url.searchParams.get("plugin"), "yaml");
      return {
        ok: true,
        status: 200,
        arrayBuffer: async () => Buffer.from("PKfixture"),
      };
    }
    const responses = {
      "/api/plugins/installed": { plugins: [] },
      "/api/qualityprofiles/search": {
        profiles: ["js", "ts", ...(yaml ? ["yaml"] : []), ...languages].map((language) => ({
          language,
          name: language,
          activeRuleCount: 0,
        })),
      },
      "/api/qualitygates/get_by_project": { qualityGate: { name: "fixture" } },
      "/api/qualitygates/show": { conditions: [] },
      "/api/settings/values": {
        settings: settings.filter((setting) =>
          !url.searchParams.has("keys") || url.searchParams.get("keys").split(",").includes(setting.key),
        ),
      },
    };
    assert.ok(responses[url.pathname], `Unexpected GET ${url.pathname}`);
    return { ok: true, status: 200, json: async () => responses[url.pathname] };
  };
  return {
    root,
    calls,
    prepare: () => prepare(root, { cache: path.join(base, "cache"), fetcher }),
  };
}

test("严格 umask 下 YAML 插件仍可供容器读取，运行目录保持私有", async (t) => {
  const previous = process.umask(0o077);
  t.after(() => process.umask(previous));
  const f = fixture(t, [], { yaml: true });
  const run = await f.prepare();
  assert.equal(fs.statSync(path.join(run, "yaml.jar")).mode & 0o777, 0o644);
  assert.equal(fs.statSync(run).mode & 0o777, 0o700);
});

for (const [key, fields] of [
  ["sonar.issue.ignore.multicriteria", { ruleKey: "typescript:S3776", resourceKey: "**/generated/**" }],
  ["sonar.issue.ignore.allfile", { fileRegexp: "@Generated" }],
  ["sonar.issue.ignore.block", { beginBlockRegexp: "START-NOSCAN", endBlockRegexp: "END-NOSCAN" }],
  ["sonar.issue.enforce.multicriteria", { ruleKey: "typescript:S3776", resourceKey: "business.ts" }],
])
for (const [name, value] of [
  [
    "结构化数组",
    {
      fieldValues: [
        fields,
      ],
    },
  ],
  [
    "结构化对象",
    {
      fieldValues: {
        fieldValues: [
          {
            value: fields,
          },
        ],
      },
    },
  ],
  ["旧 scalar", { value: "criteria-1" }],
  ["旧数组", { values: ["criteria-1"] }],
])
  test(`配置导出拒绝未支持的 ${key}：${name}`, async (t) => {
    const f = fixture(t, [
      { key, ...value },
    ]);
    await assert.rejects(f.prepare(), /需同步完整子配置/);
  });

for (const [key, values] of [
  ["sonar.test.inclusions", ["tests/**/*.ts"]],
  ["sonar.javascript.exclusions", ["**/generated/**"]],
  ["sonar.javascript.file.suffixes", [".js", ".cjs"]],
  ["sonar.typescript.file.suffixes", [".ts"]],
  ["sonar.css.file.suffixes", [".css", ".scss"]],
  ["sonar.html.file.suffixes", [".html"]],
  ["sonar.yaml.file.suffixes", [".yaml"]],
  ["sonar.docker.file.patterns", ["Dockerfile", "*.dockerfile"]],
  ["sonar.text.inclusions", ["**/*.custom-config"]],
  ["sonar.text.excluded.file.suffixes", [".binary"]],
])
test(`配置导出包含仅在线上设置的 ${key}`, async (t) => {
  const setting = {
    key,
    values,
    inherited: true,
  };
  const f = fixture(t, [setting]);
  const run = await f.prepare();
  assert.deepEqual(read(path.join(run, "config/settings.json")).settings, [
    setting,
  ]);
  assert.ok(f.calls.some((url) => url.pathname === "/api/settings/values"));
});

for (const key of ["sonar.text.activate", "sonar.text.inclusions.activate"])
for (const value of ["true", "false"])
test(`配置导出保留 ${key}=${value}`, async (t) => {
  const setting = { key, value, inherited: true };
  const f = fixture(t, [setting]);
  const run = await f.prepare();
  assert.deepEqual(read(path.join(run, "config/settings.json")).settings, [setting]);
});

test("按分析器配置前缀导出范围参数，同时隔离服务器和其他语言设置", async (t) => {
  const scope = [
    { key: "sonar.javascript.maxFileSize", value: "2000" },
    { key: "sonar.filesize.limit", value: "30" },
    { key: "sonar.sourceEncoding", value: "UTF-8" },
    { key: "sonar.cloudformation.file.identifier", value: "Resources" },
    { key: "sonar.kubernetes.activate", value: "false" },
  ];
  const f = fixture(t, [...scope,
    { key: "sonar.core.serverBaseURL", value: "https://example.invalid" },
    { key: "sonar.python.version", value: "3.12" },
    { key: "sonar.javascriptx.exclusions", value: "**/*" },
  ]);
  const run = await f.prepare();
  assert.deepEqual(read(path.join(run, "config/settings.json")).settings, scope);
});

test("JSON/YAML 的 IaC Profiles 与现有规则走相同导出路径", async (t) => {
  const languages = ["json", "cloudformation", "kubernetes", "azureresourcemanager", "ansible", "githubactions", "azurepipelines"];
  const f = fixture(t, [], { languages });
  fs.writeFileSync(path.join(f.root, "stack.yaml"), "Resources: {}\n");
  const run = await f.prepare();
  const profiles = read(path.join(run, "config/profiles.json"));
  assert.deepEqual(profiles.map((profile) => profile.language), ["js", "ts", ...languages]);
  for (const language of languages) {
    assert.ok(fs.existsSync(path.join(run, "config", `${language}.xml`)));
    assert.ok(f.calls.some((url) => url.pathname === "/api/qualityprofiles/backup" && url.searchParams.get("language") === language));
  }
});

for (const suffix of ["kt", "kts", "KTS", "xml", "XML", "xsd", "xsl", "xslt", "tf", "TF", "tfvars", "tf.json", "tfvars.json", "hcl", "bicep"])
test(`未同步相关 Profile 时拒绝 ${suffix} 源码`, async (t) => {
  const f = fixture(t, []);
  fs.writeFileSync(path.join(f.root, `source.${suffix}`), "<root/>\n");
  await assert.rejects(f.prepare(), /其他语言需先补充配置同步方案/);
  assert.equal(f.calls.length, 0);
});
