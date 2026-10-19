import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync, symlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { collect, loadConfig } from '../scripts/diagnose.mjs';

test('缺少 token 时不联网；dotenv 不执行命令且只读取 Sonar 配置', () => {
  const root = mkdtempSync(join(tmpdir(), 'sonar-config-'));
  try {
    writeFileSync(join(root, 'sonar-project.properties'), 'sonar.projectKey=business-admin\n');
    assert.throws(() => loadConfig(root, {}, () => {}), /SONAR_TOKEN/);
    writeFileSync(join(root, '.env.local'), 'SONAR_TOKEN="squ_fixture"\nUNRELATED=$(exit 1)\n');
    assert.throws(() => loadConfig(root, {}, () => { throw Error('tracked'); }), /Git 忽略/);
    assert.equal(loadConfig(root, {}, () => {}).token, 'squ_fixture');
  } finally { rmSync(root, { recursive: true }); }
});
test('项目标识采用 Properties 分隔符、转义、续行和最后赋值语义', () => {
  const root = mkdtempSync(join(tmpdir(), 'sonar-project-key-'));
  try {
    for (const [properties, expected] of [
      ['sonar.projectKey=first\nsonar.projectKey=final\n', 'final'],
      ['sonar.projectKey:colon\n', 'colon'],
      ['sonar.projectKey whitespace\n', 'whitespace'],
      [String.raw`sonar\.projectKey=escaped\u002dkey`, 'escaped-key'],
      ['sonar.exclusions=first/**,\\\n  second/**\nsonar.projectKey=current\n', 'current'],
      ['sonar.project\\\n  Key=continued\n', 'continued'],
    ]) {
      writeFileSync(join(root, 'sonar-project.properties'), properties);
      assert.equal(loadConfig(root, { SONAR_TOKEN: 'squ_fixture' }).project, expected);
    }
  } finally { rmSync(root, { recursive: true }); }
});

function client({ revision = 'current', changed = false, status = 200 } = {}) {
  let ceReads = 0;
  const calls = [];
  return { calls, fetcher: async (url, options) => {
    calls.push(url);
    assert.equal(options.redirect, 'error');
    const u = new URL(url);
    let body;
    if (u.pathname.endsWith('/ce/component')) body = { current: { status: 'SUCCESS', analysisId: changed && ceReads++ ? 'other' : 'analysis' } };
    else if (u.pathname.endsWith('/project_analyses/search')) body = { analyses: [{ key: 'analysis', revision }] };
    else if (u.pathname.endsWith('/project_status')) body = { projectStatus: { status: 'ERROR', conditions: [] } };
    else if (u.pathname.endsWith('/issues/search')) {
      assert.equal(u.searchParams.get('componentKeys'), 'business-admin');
      body = { paging: { total: 101 }, issues: Array.from({ length: u.searchParams.get('p') === '1' ? 100 : 1 }, () => ({ rule: 'typescript:S3776', component: 'business-admin:app.ts', line: 3, message: 'complexity' })) };
    } else body = { paging: { total: 0 }, hotspots: [] };
    return { ok: status === 200, status, json: async () => body };
  }};
}
const config = { host: 'https://sonar.rydev.work', token: 'squ_fixture', project: 'business-admin' };
test('完整分页并绑定分析提交，门禁使用 analysisId', async () => {
  const c = client();
  const result = await collect(config, 'current', c.fetcher);
  assert.equal(result.issues.length, 101);
  assert.equal(result.safeToFix, true);
  assert.ok(c.calls.some(url => url.includes('analysisId=analysis')));
});
test('历史诊断使用新版热点 API 的 project 参数', async () => {
  const c = client();
  await collect(config, 'current', async (url, options) => {
    const u = new URL(url);
    if (u.pathname === '/api/hotspots/search') {
      assert.equal(u.searchParams.get('project'), config.project);
      assert.equal(u.searchParams.has('projectKey'), false);
    }
    return c.fetcher(url, options);
  });
});
test('历史诊断拒绝分页期间总数变化及返回条数超过总数', async () => {
  for (const total of [100, 0]) {
    const c = client();
    await assert.rejects(collect(config, 'current', async (url, options) => {
      const response = await c.fetcher(url, options);
      const u = new URL(url);
      if (u.pathname === '/api/issues/search' && (total === 0 || u.searchParams.get('p') === '2')) {
        const data = await response.json();
        return { ...response, json: async () => ({ ...data, paging: { total } }) };
      }
      return response;
    }), /分页.*变化|总数不符/);
  }
});
test('旧提交或诊断期间分析发生变化时不允许据此修复', async () => {
  assert.equal((await collect(config, 'current', client({ revision: 'old' }).fetcher)).safeToFix, false);
  assert.equal((await collect(config, 'current', client({ changed: true }).fetcher)).safeToFix, false);
});
test('401/403 不泄露 token 或远端响应', async () => {
  for (const status of [401, 403]) {
    await assert.rejects(collect(config, 'current', client({ status }).fetcher), error => !error.message.includes(config.token) && error.message.includes(String(status)));
  }
});
test('拒绝将凭据发往其他主机或带 userinfo 的 URL', () => {
  for (const host of ['https://example.com', 'https://user:password@sonar.rydev.work']) {
    assert.throws(() => loadConfig('/nonexistent', { SONAR_TOKEN: 'squ_fixture', SONAR_HOST_URL: host }), /拒绝/);
  }
});
test('网络超时不泄露 fetch 错误中的凭据', async () => {
  await assert.rejects(collect(config, 'current', async () => { throw Error(config.token); }), error => !error.message.includes(config.token));
});
test('缺少分页元数据或存在排队扫描时停止诊断', async () => {
  const base = client();
  await assert.rejects(collect(config, 'current', async (url, options) => {
    if (url.includes('/issues/search')) return { ok: true, json: async () => ({ issues: [] }) };
    return base.fetcher(url, options);
  }), /分页/);
  await assert.rejects(collect(config, 'current', async () => ({ ok: true, json: async () => ({ current: { status: 'SUCCESS', analysisId: 'analysis' }, queue: [{}] }) })), /待处理/);
});

test('通过 Codex 安装链接运行时必须执行 CLI，而非静默退出', () => {
  const root = mkdtempSync(join(tmpdir(), 'sonar-cli-'));
  try {
    const alias = join(root, 'diagnose.mjs');
    symlinkSync(fileURLToPath(new URL('../scripts/diagnose.mjs', import.meta.url)), alias);
    const result = spawnSync(process.execPath, [alias], { cwd: root, encoding: 'utf8' });
    assert.notEqual(result.status, 0);
    assert.ok(result.stderr.length > 0);
  } finally { rmSync(root, { recursive: true }); }
});

test('非空热点保留 ruleKey，供用户按规则审核', async () => {
  const base = client();
  const hotspot = { key: 'credential-review', ruleKey: 'typescript:S2068', component: 'business-admin:settings.ts', line: 8, message: 'Review credential', vulnerabilityProbability: 'HIGH' };
  const result = await collect(config, 'current', async (url, options) => {
    if (new URL(url).pathname.endsWith('/hotspots/search')) {
      return { ok: true, json: async () => ({ paging: { total: 1 }, hotspots: [hotspot] }) };
    }
    return base.fetcher(url, options);
  });
  assert.deepEqual(result.hotspots, [hotspot]);
});
