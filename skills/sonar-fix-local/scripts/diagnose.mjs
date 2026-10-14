import { readFileSync, existsSync, realpathSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { parseEnv } from 'node:util';
import { execFileSync } from 'node:child_process';

// 只解析配置，不执行 .env.local；Git 忽略检查失败时不读取其中的凭据。
export function loadConfig(root, env = process.env, checkIgnored = () => execFileSync('git', ['check-ignore', '-q', '.env.local'], { cwd: root, stdio: 'ignore' })) {
  let local = {};
  if (existsSync(resolve(root, '.env.local'))) {
    try { checkIgnored(); } catch { throw Error('.env.local 必须被 Git 忽略'); }
    local = parseEnv(readFileSync(resolve(root, '.env.local'), 'utf8'));
  }
  const token = env.SONAR_TOKEN || local.SONAR_TOKEN;
  if (!token) throw Error('缺少 SONAR_TOKEN：跳过 API，请提供门禁及 文件:行号 + 规则号');
  if (!token.startsWith('squ_')) throw Error('需要具备项目 Browse 权限的 User Token（squ_）');
  const host = env.SONAR_HOST_URL || local.SONAR_HOST_URL || 'https://sonar.rydev.work';
  const url = new URL(host);
  if (url.origin !== 'https://sonar.rydev.work' || url.username || url.password || url.search || url.hash || url.pathname !== '/') throw Error('仅支持 https://sonar.rydev.work，拒绝向其他目标发送凭据');
  const properties = readFileSync(resolve(root, 'sonar-project.properties'), 'utf8');
  const project = properties.match(/^\s*sonar\.projectKey\s*=\s*(\S+)\s*$/m)?.[1];
  if (!project) throw Error('缺少 sonar.projectKey');
  return { host: url.origin, token, project };
}

export async function collect(config, head, fetcher = fetch) {
  const get = async (path, params) => {
    const url = new URL(path, config.host);
    url.search = new URLSearchParams(params).toString();
    let response;
    try {
      response = await fetcher(url.href, { headers: { Authorization: `Bearer ${config.token}` }, redirect: 'error', signal: AbortSignal.timeout(20000) });
    } catch { throw Error('Sonar 只读请求失败或超时，未输出凭据或响应正文'); }
    if (!response.ok) throw Error(`Sonar HTTP ${response.status}：检查权限/服务状态；停止自动修复`);
    try { return await response.json(); } catch { throw Error('Sonar 响应不是有效 JSON'); }
  };
  const pages = async (path, params, field) => {
    const rows = [];
    for (let page = 1; page <= 100; page++) {
      const data = await get(path, { ...params, p: String(page), ps: '100' });
      if (!Array.isArray(data[field]) || !Number.isInteger(data.paging?.total)) throw Error('分页响应缺少数据或总数，停止自动修复');
      rows.push(...data[field]);
      if (rows.length >= data.paging.total) return rows;
      if (!data[field].length) throw Error('分页提前结束，结果不完整');
    }
    throw Error('分页超过 100 页，结果不完整');
  };
  const before = await get('/api/ce/component', { component: config.project });
  const analysisId = before.current?.analysisId;
  if (!analysisId || before.current.status !== 'SUCCESS' || before.queue?.length) throw Error('缺少成功分析或仍有待处理扫描，停止自动修复');
  const history = await get('/api/project_analyses/search', { project: config.project, ps: '100' });
  const analysis = history.analyses?.find(item => item.key === analysisId);
  const gate = await get('/api/qualitygates/project_status', { analysisId });
  if (!gate.projectStatus?.status) throw Error('缺少有效门禁状态，停止自动修复');
  const issues = await pages('/api/issues/search', { componentKeys: config.project, resolved: 'false', inNewCodePeriod: 'true' }, 'issues');
  const hotspots = await pages('/api/hotspots/search', { projectKey: config.project, status: 'TO_REVIEW', inNewCodePeriod: 'true' }, 'hotspots');
  const after = await get('/api/ce/component', { component: config.project });
  return {
    project: config.project, analysisId, analysisRevision: analysis?.revision ?? null, localHead: head,
    safeToFix: analysis?.revision === head && after.current?.analysisId === analysisId && after.current?.status === 'SUCCESS' && !after.queue?.length,
    gate: gate.projectStatus,
    issues: issues.map(({ rule, component, line, message }) => ({ rule, component, line, message })),
    hotspots: hotspots.map(({ key, ruleKey, component, line, message, vulnerabilityProbability }) => ({ key, ruleKey, component, line, message, vulnerabilityProbability })),
  };
}

if (process.argv[1] && import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href) {
  try {
    const root = execFileSync('git', ['rev-parse', '--show-toplevel'], { encoding: 'utf8' }).trim();
    const head = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim();
    const result = await collect(loadConfig(root), head);
    process.stdout.write(JSON.stringify(result, null, 2) + '\n');
    if (!result.safeToFix) process.exitCode = 2;
  } catch (error) { process.stderr.write(error.message + '\n'); process.exitCode = 1; }
}
