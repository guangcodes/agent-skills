import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync, spawn } from "node:child_process";
import {
  acquireProjectLock,
  acquireScanLock,
  claimProjectCleanup,
  claimRunStartup,
  claimRunScan,
  claimRunCleanup,
  watchStartup,
  projectIdentity,
  projectKey,
  readProjectLock,
  releaseProjectLock,
} from "../scripts/lock.mjs";

const docker = {
  context: "default",
  socket: "unix:///var/run/docker.sock",
  daemonId: "daemon-a",
};

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "sonar-lock-test-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const identity = `project-root:${root}`;
  return {
    root,
    identity,
    key: projectKey(identity),
    lockRoot: path.join(root, "locks"),
  };
}

function metadata(f, id = "0123456789abcdef") {
  return {
    projectIdentity: f.identity,
    projectKey: f.key,
    runId: id,
    runDir: path.join(f.root, id),
    docker,
  };
}

function status(hasResources = false) {
  return { hasResources, details: { server: hasResources ? "present" : "absent" } };
}

test("Git worktree 使用同一个 canonical git-common-dir 项目身份", async (t) => {
  const f = fixture(t);
  const repo = path.join(f.root, "repo");
  fs.mkdirSync(repo);
  execFileSync("git", ["init", "-q", "--template="], { cwd: repo });
  fs.writeFileSync(path.join(repo, "file.txt"), "one\n");
  execFileSync("git", ["add", "file.txt"], { cwd: repo });
  execFileSync("git", ["-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial"], { cwd: repo });
  const worktree = path.join(f.root, "worktree");
  execFileSync("git", ["worktree", "add", "-q", worktree], { cwd: repo });
  t.after(() => fs.rmSync(worktree, { recursive: true, force: true }));
  assert.equal(projectIdentity(repo), projectIdentity(worktree));
});

test("同一项目两个并发获取只有一个成功", async (t) => {
  const f = fixture(t);
  const source = JSON.stringify(metadata(f));
  const script = `import { acquireProjectLock } from ${JSON.stringify(new URL("../scripts/lock.mjs", import.meta.url).href)};
const m=${source}; try { await acquireProjectLock(m,{lockRoot:${JSON.stringify(f.lockRoot)},currentDocker:m.docker,resourceStatus:()=>({hasResources:false,details:{}}),cleanupResources:()=>{},holdProcess:false}); console.log('ok'); setTimeout(()=>{},700); } catch(e) { console.log('error:'+e.message); }`;
  const runChild = () => new Promise((resolve) => {
    const child = spawn(process.execPath, ["--input-type=module", "-e", script], { encoding: "utf8" });
    let out = "";
    child.stdout.on("data", (chunk) => { out += chunk; });
    child.on("close", (code) => resolve({ code, out }));
  });
  const results = await Promise.all([runChild(), runChild()]);
  assert.equal(results.filter((r) => r.out.startsWith("ok")).length, 1);
  assert.equal(results.filter((r) => r.out.startsWith("error:")).length, 1);
  const lock = readProjectLock(f.key, f.lockRoot);
  if (lock) {
    try { process.kill(lock.metadata.pid, "SIGTERM"); } catch {}
    fs.rmSync(lock.lockDir, { recursive: true, force: true });
  }
});

test("不同项目可以并发持有各自项目锁", async (t) => {
  const a = fixture(t), b = fixture(t);
  const la = await acquireProjectLock(metadata(a), { lockRoot: a.lockRoot, currentDocker: docker, resourceStatus: () => status(), cleanupResources: () => {}, holdProcess: false });
  const lb = await acquireProjectLock(metadata(b), { lockRoot: b.lockRoot, currentDocker: docker, resourceStatus: () => status(), cleanupResources: () => {}, holdProcess: false });
  assert.ok(la && lb);
  releaseProjectLock(metadata(a), { lockRoot: a.lockRoot, currentDocker: docker });
  releaseProjectLock(metadata(b), { lockRoot: b.lockRoot, currentDocker: docker });
});

test("活动锁只报告已有 run，不清理或抢占", async (t) => {
  const f = fixture(t);
  const first = metadata(f);
  await acquireProjectLock(first, { lockRoot: f.lockRoot, currentDocker: docker, resourceStatus: () => status(true), cleanupResources: () => { throw Error("must not clean"); }, holdProcess: false });
  await assert.rejects(() => acquireProjectLock({ ...metadata(f, "fedcba9876543210"), runDir: path.join(f.root, "new") }, { lockRoot: f.lockRoot, currentDocker: docker, resourceStatus: () => status(true), cleanupResources: () => { throw Error("must not clean"); }, holdProcess: false }), /已有活动 Sonar 运行.*0123456789abcdef/);
  releaseProjectLock(first, { lockRoot: f.lockRoot, currentDocker: docker });
});

test("进程退出且资源不存在时清理过期锁", async (t) => {
  const f = fixture(t);
  const stale = metadata(f);
  await acquireProjectLock(stale, { lockRoot: f.lockRoot, currentDocker: docker, resourceStatus: () => status(), cleanupResources: () => {}, holdProcess: false });
  const lockFile = path.join(f.lockRoot, `${f.key}.lock`, "lock.json");
  const saved = JSON.parse(fs.readFileSync(lockFile, "utf8"));
  saved.pid = 99999999;
  fs.writeFileSync(lockFile, JSON.stringify(saved));
  const next = metadata(f, "fedcba9876543210");
  await acquireProjectLock(next, { lockRoot: f.lockRoot, currentDocker: docker, resourceStatus: () => status(), cleanupResources: () => {}, holdProcess: false });
  assert.equal(readProjectLock(f.key, f.lockRoot).metadata.runId, next.runId);
  const releaseScan = acquireScanLock(next, { lockRoot: f.lockRoot, currentDocker: docker });
  releaseScan();
  releaseProjectLock(next, { lockRoot: f.lockRoot, currentDocker: docker });
});

test("进程退出但资源仍在时只按 run ID 清理精确资源", async (t) => {
  const f = fixture(t);
  const stale = metadata(f);
  await acquireProjectLock(stale, { lockRoot: f.lockRoot, currentDocker: docker, resourceStatus: () => status(), cleanupResources: () => {}, holdProcess: false });
  const lockFile = path.join(f.lockRoot, `${f.key}.lock`, "lock.json");
  const saved = JSON.parse(fs.readFileSync(lockFile, "utf8"));
  saved.pid = 99999999;
  fs.writeFileSync(lockFile, JSON.stringify(saved));
  let present = true;
  let cleaned = 0;
  const next = metadata(f, "fedcba9876543210");
  await acquireProjectLock(next, { lockRoot: f.lockRoot, currentDocker: docker, resourceStatus: () => status(present), cleanupResources: (old) => { assert.equal(old.runId, stale.runId); present = false; cleaned++; }, holdProcess: false });
  assert.equal(cleaned, 1);
  const releaseScan = acquireScanLock(next, { lockRoot: f.lockRoot, currentDocker: docker });
  releaseScan();
  releaseProjectLock(next, { lockRoot: f.lockRoot, currentDocker: docker });
});

test("Docker context、socket 或 daemon 改变时保留锁并停止清理", async (t) => {
  const f = fixture(t);
  const stale = metadata(f);
  await acquireProjectLock(stale, { lockRoot: f.lockRoot, currentDocker: docker, resourceStatus: () => status(), cleanupResources: () => {}, holdProcess: false });
  acquireScanLock(stale, { lockRoot: f.lockRoot, currentDocker: docker });
  const lockFile = path.join(f.lockRoot, `${f.key}.lock`, "lock.json");
  const saved = JSON.parse(fs.readFileSync(lockFile, "utf8"));
  saved.pid = 99999999;
  fs.writeFileSync(lockFile, JSON.stringify(saved));
  let cleaned = false;
  await assert.rejects(() => acquireProjectLock(metadata(f, "fedcba9876543210"), { lockRoot: f.lockRoot, currentDocker: { ...docker, daemonId: "daemon-b" }, resourceStatus: () => status(true), cleanupResources: () => { cleaned = true; }, holdProcess: false }), /Docker context、socket 或 daemon 已变化/);
  assert.equal(cleaned, false);
  assert.ok(readProjectLock(f.key, f.lockRoot));
  assert.ok(fs.existsSync(path.join(f.lockRoot, `${f.key}.lock`, `.scan-${saved.holderToken}`)));
});

test("过期资源清理失败时保留旧租约", async (t) => {
  const f = fixture(t);
  const stale = metadata(f);
  const options = { lockRoot: f.lockRoot, currentDocker: docker, holdProcess: false };
  await acquireProjectLock(stale, options);
  const file = path.join(f.lockRoot, `${f.key}.lock`, "lock.json");
  const saved = JSON.parse(fs.readFileSync(file));
  saved.pid = 99999999;
  fs.writeFileSync(file, JSON.stringify(saved));
  await assert.rejects(acquireProjectLock(metadata(f, "fedcba9876543210"), {
    ...options, resourceStatus: () => status(true), cleanupResources: () => { throw Error("cleanup-failed"); },
  }), /cleanup-failed/);
  assert.equal(readProjectLock(f.key, f.lockRoot).metadata.runId, stale.runId);
  assert.equal(readProjectLock(f.key, f.lockRoot).metadata.holderToken, saved.holderToken);
});

for (const owner of ["alive", "dead", "unknown"]) {
  for (const hasResources of [false, true]) {
    test(`holder 退出但扫描未确认结束时拒绝回收（扫描 ${owner}，资源 ${hasResources}）`, async (t) => {
      const f = fixture(t);
      const stale = metadata(f);
      fs.mkdirSync(stale.runDir);
      const options = { lockRoot: f.lockRoot, currentDocker: docker, holdProcess: false };
      const lock = await acquireProjectLock(stale, options);
      const releaseScan = acquireScanLock(stale, options);
      const operation = owner === "unknown" ? null : claimRunScan(stale.runDir);
      try {
        if (owner === "dead")
          fs.writeFileSync(path.join(stale.runDir, ".lifecycle/startup.json"), JSON.stringify({ pid: 99999999, operation: "扫描" }));
        const file = path.join(lock.lockDir, "lock.json");
        fs.writeFileSync(file, JSON.stringify({ ...lock.metadata, pid: 99999999 }));
        const credentials = path.join(stale.runDir, "credentials.json");
        fs.writeFileSync(credentials, "retained-credential");
        const next = metadata(f, "fedcba9876543210");
        await assert.rejects(acquireProjectLock(next, {
          ...options, resourceStatus: () => status(hasResources),
          cleanupResources: () => assert.fail("must not delete resources"),
          cleanupStale: () => assert.fail("must not delete credentials"),
        }), /扫描尚未确认结束/);
        assert.equal(operation?.signal.aborted ?? false, false);
        assert.equal(fs.readFileSync(credentials, "utf8"), "retained-credential");
        assert.equal(readProjectLock(f.key, f.lockRoot).metadata.holderToken, lock.metadata.holderToken);
        assert.ok(fs.existsSync(path.join(lock.lockDir, `.scan-${lock.metadata.holderToken}`)));
        assert.equal(fs.existsSync(path.join(lock.lockDir, ".mutation")), false);
      } finally { operation?.finish(); releaseScan(); }
    });
  }
}

test("过期锁并发回收只允许一个请求发布新锁", async (t) => {
  const f = fixture(t);
  const options = { lockRoot: f.lockRoot, currentDocker: docker, holdProcess: false,
    resourceStatus: () => status(), cleanupResources: () => {} };
  await acquireProjectLock(metadata(f), options);
  const file = path.join(f.lockRoot, `${f.key}.lock`, "lock.json");
  const old = JSON.parse(fs.readFileSync(file));
  old.pid = 99999999;
  fs.writeFileSync(file, JSON.stringify(old));
  let resume, entered;
  const reached = new Promise((resolve) => { entered = resolve; });
  const winner = metadata(f, "aaaaaaaaaaaaaaaa");
  const first = acquireProjectLock(winner, { ...options, resourceStatus: async () => {
    entered();
    await new Promise((resolve) => { resume = resolve; });
    return status();
  } });
  await reached;
  await assert.rejects(acquireProjectLock(metadata(f, "bbbbbbbbbbbbbbbb"), options), /已有活动/);
  resume();
  await first;
  assert.equal(readProjectLock(f.key, f.lockRoot).metadata.runId, winner.runId);
  await assert.rejects(acquireProjectLock(metadata(f, "bbbbbbbbbbbbbbbb"), options), /已有活动/);
  releaseProjectLock(winner, { lockRoot: f.lockRoot, currentDocker: docker });
});

test("释放锁不向持久化 PID 发送终止信号", async (t) => {
  const f = fixture(t);
  const value = metadata(f);
  await acquireProjectLock(value, { lockRoot: f.lockRoot, currentDocker: docker, holdProcess: false });
  const file = path.join(f.lockRoot, `${f.key}.lock`, "lock.json");
  const saved = JSON.parse(fs.readFileSync(file));
  // A live unrelated process represents a recycled PID.
  const unrelated = spawn(process.execPath, ["-e", "setInterval(()=>{},1000)"], { stdio: "ignore" });
  t.after(() => unrelated.kill("SIGKILL"));
  saved.pid = unrelated.pid;
  fs.writeFileSync(file, JSON.stringify(saved));
  releaseProjectLock(value, { lockRoot: f.lockRoot, currentDocker: docker });
  await new Promise((resolve) => setTimeout(resolve, 150));
  assert.equal(unrelated.exitCode, null);
  assert.equal(unrelated.signalCode, null);
  assert.equal(readProjectLock(f.key, f.lockRoot), null);
});

test("内部 holder 在租约移除后自行退出", async (t) => {
  const f = fixture(t);
  const value = metadata(f);
  const lock = await acquireProjectLock(value, { lockRoot: f.lockRoot, currentDocker: docker });
  releaseProjectLock(value, { lockRoot: f.lockRoot, currentDocker: docker });
  const deadline = Date.now() + 3000;
  let alive = true;
  while (alive && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 20));
    try { process.kill(lock.metadata.pid, 0); } catch { alive = false; }
  }
  assert.equal(alive, false);
});

test("同一项目租约只允许一个扫描，释放后可再次扫描", async (t) => {
  const f = fixture(t);
  const value = metadata(f);
  const options = { lockRoot: f.lockRoot, currentDocker: docker, holdProcess: false };
  await acquireProjectLock(value, options);
  const release = acquireScanLock(value, options);
  assert.throws(() => acquireScanLock(value, options), /拒绝并发扫描/);
  assert.equal(readProjectLock(f.key, f.lockRoot).metadata.runId, value.runId);
  release();
  const releaseNext = acquireScanLock(value, options);
  release();
  assert.throws(() => acquireScanLock(value, options), /拒绝并发扫描/);
  releaseNext();
  releaseProjectLock(value, options);
});

test("旧扫描的 finally 不移除新项目租约的扫描锁", async (t) => {
  const f = fixture(t);
  const value = metadata(f);
  const options = { lockRoot: f.lockRoot, currentDocker: docker, holdProcess: false };
  await acquireProjectLock(value, options);
  const releaseOld = acquireScanLock(value, options);
  releaseProjectLock(value, options);
  const next = metadata(f, "fedcba9876543210");
  await acquireProjectLock(next, options);
  const releaseNext = acquireScanLock(next, options);
  releaseOld();
  assert.throws(() => acquireScanLock(next, options), /拒绝并发扫描/);
  releaseNext();
  releaseProjectLock(next, options);
});

test("竞争 start 检查资源时不阻塞当前扫描释放标记", async (t) => {
  const f = fixture(t);
  const value = metadata(f);
  const options = { lockRoot: f.lockRoot, currentDocker: docker, holdProcess: false };
  const lock = await acquireProjectLock(value, options);
  const release = acquireScanLock(value, options);
  let entered, resume;
  const reached = new Promise((resolve) => { entered = resolve; });
  const inspected = new Promise((resolve) => { resume = resolve; });
  const pending = acquireProjectLock(metadata(f, "fedcba9876543210"), {
    ...options, resourceStatus: async () => { entered(); await inspected; return status(true); },
    cleanupResources: () => { throw Error("must not clean"); },
  });
  const rejected = assert.rejects(pending, /已有活动 Sonar 运行/);
  await reached;
  try {
    release();
    assert.equal(fs.existsSync(path.join(lock.lockDir, `.scan-${lock.metadata.holderToken}`)), false);
  } finally {
    resume();
    await rejected;
  }
  const releaseNext = acquireScanLock(value, options);
  releaseNext();
  releaseProjectLock(value, options);
});

test("启动完成只移除启动标记，项目租约继续供扫描使用", async (t) => {
  const f = fixture(t);
  const value = metadata(f);
  const options = { lockRoot: f.lockRoot, currentDocker: docker, holdProcess: false, starting: true };
  const lock = await acquireProjectLock(value, options);
  const startup = watchStartup(lock);
  t.after(() => startup.finish());
  assert.throws(() => acquireScanLock(value, options), /仍在启动/);
  assert.equal(startup.finish(), false);
  assert.ok(readProjectLock(f.key, f.lockRoot));
  const releaseScan = acquireScanLock(value, options);
  releaseScan();
  releaseProjectLock(value, options);
});

test("清理请求取消启动并等到启动确认后才允许释放租约", { timeout: 5000 }, async (t) => {
  const f = fixture(t);
  const value = metadata(f);
  const options = { lockRoot: f.lockRoot, currentDocker: docker, holdProcess: false, starting: true };
  const lock = await acquireProjectLock(value, options);
  const startup = watchStartup(lock);
  t.after(() => startup.finish());
  const claim = claimProjectCleanup(value, options);
  t.after(() => claim.dispose());
  const waiting = claim.waitForStartup();
  await new Promise((resolve) => startup.signal.addEventListener("abort", resolve, { once: true }));
  assert.match(startup.signal.reason.message, /已请求停止/);
  assert.throws(() => claim.release(), /启动尚未确认结束/);
  assert.ok(fs.existsSync(lock.lockDir));
  assert.equal(startup.finish(), true);
  await waiting;
  claim.release();
  assert.equal(fs.existsSync(lock.lockDir), false);
});

test("启动未确认停止时清理超时保留租约", async (t) => {
  const f = fixture(t);
  const value = metadata(f);
  const options = { lockRoot: f.lockRoot, currentDocker: docker, holdProcess: false, starting: true };
  const lock = await acquireProjectLock(value, options);
  const claim = claimProjectCleanup(value, options);
  try {
    await assert.rejects(claim.waitForStartup({ timeoutMs: 20 }), /等待启动流程停止超时/);
    assert.throws(() => claim.release(), /启动尚未确认结束/);
    assert.ok(fs.existsSync(lock.lockDir));
    assert.ok(fs.existsSync(path.join(lock.lockDir, `.startup-${lock.metadata.holderToken}`)));
  } finally { claim.dispose(); }
});

test("holder 退出但启动仍未确认结束时不回收旧运行", async (t) => {
  const f = fixture(t);
  const value = metadata(f);
  const options = { lockRoot: f.lockRoot, currentDocker: docker, holdProcess: false, starting: true };
  const lock = await acquireProjectLock(value, options);
  const file = path.join(lock.lockDir, "lock.json");
  const saved = JSON.parse(fs.readFileSync(file));
  saved.pid = 99999999;
  fs.writeFileSync(file, JSON.stringify(saved));
  await assert.rejects(acquireProjectLock(metadata(f, "fedcba9876543210"), {
    ...options, resourceStatus: () => status(false), cleanupResources: () => { throw Error("must not clean"); },
  }), /启动尚未确认结束/);
  assert.equal(readProjectLock(f.key, f.lockRoot).metadata.runId, value.runId);
});

test("预检停止等待启动确认，清理期间拒绝新 start，结束后允许重启", async (t) => {
  const f = fixture(t);
  const startup = claimRunStartup(f.root);
  t.after(() => startup.finish());
  const cleanup = claimRunCleanup(f.root);
  const waiting = cleanup.waitForStartup();
  await new Promise((resolve) => startup.signal.addEventListener("abort", resolve, { once: true }));
  assert.match(startup.signal.reason.message, /已请求停止/);
  assert.throws(() => claimRunStartup(f.root), /已有启动、扫描或清理/);
  assert.equal(startup.finish(), true);
  await waiting;
  assert.throws(() => claimRunStartup(f.root), /已有启动、扫描或清理/);
  cleanup.dispose();
  assert.equal(fs.existsSync(path.join(f.root, ".lifecycle")), false);
  const next = claimRunStartup(f.root);
  assert.equal(next.finish(), false);
  assert.equal(fs.existsSync(path.join(f.root, ".lifecycle")), false);
});

test("预检未确认停止时超时保留启动状态", async (t) => {
  const f = fixture(t);
  const startup = claimRunStartup(f.root);
  t.after(() => startup.finish());
  const cleanup = claimRunCleanup(f.root);
  try {
    await assert.rejects(cleanup.waitForStartup({ timeoutMs: 20 }), /等待启动流程停止超时/);
    assert.throws(() => claimRunStartup(f.root), /已有启动、扫描或清理/);
  } finally { cleanup.dispose(); }
  assert.ok(fs.existsSync(path.join(f.root, ".lifecycle/startup.json")));
  startup.finish();
});

test("预检进程退出但未确认子进程结束时保留状态", async (t) => {
  const f = fixture(t);
  fs.mkdirSync(path.join(f.root, ".lifecycle"));
  fs.writeFileSync(path.join(f.root, ".lifecycle/startup.json"), JSON.stringify({ pid: 99999999 }));
  const cleanup = claimRunCleanup(f.root);
  try { await assert.rejects(cleanup.waitForStartup(), /启动进程已退出但未确认/); }
  finally { cleanup.dispose(); }
  assert.ok(fs.existsSync(path.join(f.root, ".lifecycle/startup.json")));
  assert.throws(() => claimRunStartup(f.root), /已有启动、扫描或清理/);
});

test("失效清理权限不会给替代启动发送取消请求", async (t) => {
  const f = fixture(t);
  const startup = claimRunStartup(f.root);
  const cleanup = claimRunCleanup(f.root, { beforeStop() { throw Error("stale lease"); } });
  try { await assert.rejects(cleanup.waitForStartup(), /stale lease/); }
  finally { cleanup.dispose(); }
  await new Promise((resolve) => setTimeout(resolve, 75));
  assert.equal(startup.signal.aborted, false);
  assert.equal(startup.finish(), false);
  assert.equal(fs.existsSync(path.join(f.root, ".lifecycle")), false);
});

test("扫描的清理权限绑定原租约，拒绝删除同 run 的新租约", async (t) => {
  const f = fixture(t);
  const value = metadata(f);
  const options = { lockRoot: f.lockRoot, currentDocker: docker, holdProcess: false };
  const original = await acquireProjectLock(value, options);
  const releaseScan = acquireScanLock(value, options);
  assert.equal(releaseScan.holderToken, original.metadata.holderToken);
  releaseProjectLock(value, options);
  const replacement = await acquireProjectLock(value, options);
  assert.throws(() => claimProjectCleanup({ ...value, holderToken: releaseScan.holderToken }, options), /租约已变化/);
  assert.equal(readProjectLock(f.key, f.lockRoot).metadata.holderToken, replacement.metadata.holderToken);
  releaseScan();
  releaseProjectLock(value, options);
});

test("空租约清理先请求停止时，启动不能再发布项目租约", async (t) => {
  const f = fixture(t);
  const startup = claimRunStartup(f.root);
  const cleanup = claimRunCleanup(f.root, { beforeStop() {
    assert.equal(readProjectLock(f.key, f.lockRoot), null);
  } });
  const waiting = cleanup.waitForStartup();
  await new Promise((resolve) => startup.signal.addEventListener("abort", resolve, { once: true }));
  await assert.rejects(acquireProjectLock(metadata(f), {
    lockRoot: f.lockRoot, currentDocker: docker, holdProcess: false, starting: true,
    claimPublication: () => startup.claimPublication(),
  }), /已请求停止/);
  assert.equal(readProjectLock(f.key, f.lockRoot), null);
  assert.equal(startup.finish(), true);
  await waiting;
  cleanup.dispose();
  assert.equal(fs.existsSync(path.join(f.root, ".lifecycle")), false);
});

test("项目租约先发布时，空租约清理重新核验且不取消启动", async (t) => {
  const f = fixture(t);
  const startup = claimRunStartup(f.root);
  let entered, resume, checked = false;
  const reached = new Promise((resolve) => { entered = resolve; });
  const proceed = new Promise((resolve) => { resume = resolve; });
  const options = { lockRoot: f.lockRoot, currentDocker: docker, holdProcess: false };
  const pending = acquireProjectLock(metadata(f), { ...options, claimPublication: async () => {
    const release = await startup.claimPublication();
    entered(); await proceed;
    return release;
  } });
  await reached;
  const cleanup = claimRunCleanup(f.root, { beforeStop() {
    checked = true;
    if (readProjectLock(f.key, f.lockRoot)) throw Error("stale lease");
  } });
  const rejected = assert.rejects(cleanup.waitForStartup(), /stale lease/);
  await new Promise((resolve) => setTimeout(resolve, 75));
  assert.equal(checked, false);
  resume(); await pending; await rejected;
  cleanup.dispose();
  assert.equal(startup.signal.aborted, false);
  assert.equal(startup.finish(), false);
  releaseProjectLock(metadata(f), options);
});
