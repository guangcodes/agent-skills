import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import crypto from "node:crypto";
import { execFileSync, spawn } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";

export const LOCK_ROOT = path.join(
  os.homedir(),
  ".local",
  "share",
  "sonar-fix-local",
  "locks",
);
const RUN_ID = /^[a-f0-9]{16}$/;
const PROJECT_KEY = /^[a-f0-9]{32,64}$/;

export function lockRootFor(state) {
  if (state.lockRoot) return path.resolve(state.lockRoot);
  if (process.env.SONAR_FIX_LOCAL_LOCK_ROOT)
    return path.resolve(process.env.SONAR_FIX_LOCAL_LOCK_ROOT);
  try {
    fs.mkdirSync(LOCK_ROOT, { recursive: true, mode: 0o700 });
    fs.chmodSync(LOCK_ROOT, 0o700);
    return LOCK_ROOT;
  } catch (error) {
    // Restricted hosts can use the same per-cache fallback as runtime start.
    if (error?.code !== "EACCES" && error?.code !== "EPERM") throw error;
    const fallback = path.join(state.cache, "locks");
    fs.mkdirSync(fallback, { recursive: true, mode: 0o700 });
    return fallback;
  }
}

// Capture synchronously before an operation awaits. null means that there
// was no lease, not permission to clean a lease created later.
export function captureRunLease(run) {
  const state = JSON.parse(fs.readFileSync(path.join(run, "state.json"), "utf8"));
  if (!state.docker) return { holderToken: null };
  const key = state.projectKey || projectKey(state.projectIdentity || projectIdentity(state.root));
  return { holderToken: readProjectLock(key, lockRootFor(state))?.metadata.holderToken ?? null };
}

function stableHash(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

/**
 * Return the identity shared by all worktrees of one Git repository.
 * Non-Git directories use their canonical directory path instead.
 */
export function projectIdentity(root) {
  const canonicalRoot = fs.realpathSync(root);
  try {
    const common = execFileSync(
      "git",
      ["-C", canonicalRoot, "rev-parse", "--git-common-dir"],
      { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] },
    ).trim();
    if (common) {
      const commonDir = fs.realpathSync(path.resolve(canonicalRoot, common));
      return `git-common-dir:${commonDir}`;
    }
  } catch {
    // A non-Git project is intentionally supported.  A malformed Git
    // checkout falls back to its canonical root and cannot collide with a
    // normal Git identity because of the explicit prefix.
  }
  return `project-root:${canonicalRoot}`;
}

export function projectKey(identity) {
  if (typeof identity !== "string" || !identity)
    throw Error("项目身份不能为空");
  return stableHash(identity);
}

export function projectLockPath(key, lockRoot = LOCK_ROOT) {
  if (!PROJECT_KEY.test(key)) throw Error("无效项目 key");
  return path.join(lockRoot, `${key}.lock`);
}

function writeJson(filename, value, mode = 0o600) {
  const temporary = `${filename}.tmp-${process.pid}-${crypto.randomBytes(4).toString("hex")}`;
  fs.writeFileSync(temporary, JSON.stringify(value, null, 2) + "\n", { mode });
  fs.renameSync(temporary, filename);
}

function readLock(lockDir) {
  const file = path.join(lockDir, "lock.json");
  if (!fs.existsSync(file))
    throw Error(`项目锁缺少元数据：${lockDir}`);
  let metadata;
  try {
    metadata = JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    throw Error(`项目锁元数据无效：${lockDir}`);
  }
  if (
    !metadata ||
    typeof metadata !== "object" ||
    typeof metadata.projectIdentity !== "string" ||
    !PROJECT_KEY.test(metadata.projectKey) ||
    !RUN_ID.test(metadata.runId) ||
    typeof metadata.runDir !== "string" ||
    !Number.isInteger(metadata.pid) ||
    metadata.pid <= 0 ||
    typeof metadata.createdAt !== "string" ||
    !metadata.docker ||
    typeof metadata.docker.context !== "string" ||
    typeof (metadata.docker.socket || metadata.docker.host) !== "string" ||
    typeof metadata.docker.daemonId !== "string"
  )
    throw Error(`项目锁元数据字段不完整：${lockDir}`);
  if (projectKey(metadata.projectIdentity) !== metadata.projectKey)
    throw Error(`项目锁 project key 与原始项目身份不匹配：${lockDir}`);
  if (!metadata.docker.socket)
    metadata.docker.socket = metadata.docker.host;
  return metadata;
}

export function readProjectLock(key, lockRoot = LOCK_ROOT) {
  const lockDir = projectLockPath(key, lockRoot);
  if (!fs.existsSync(lockDir)) return null;
  return { lockDir, metadata: readLock(lockDir) };
}

export function isProcessAlive(pid, processApi = process) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    processApi.kill(pid, 0);
    return true;
  } catch (error) {
    return error?.code === "EPERM";
  }
}

function sameDocker(a, b) {
  return (
    a?.context === b?.context &&
    (a?.socket || a?.host) === (b?.socket || b?.host) &&
    a?.daemonId === b?.daemonId
  );
}

function startHolder(lockDir, holderToken) {
  const holder = spawn(
    process.execPath,
    [fileURLToPath(import.meta.url), "--hold", lockDir, holderToken],
    { detached: true, stdio: "ignore" },
  );
  holder.unref();
  if (!holder.pid) throw Error("无法启动项目锁持有进程");
  return holder.pid;
}

function activeMessage(metadata, details) {
  return [
    "同一项目已有活动 Sonar 运行，当前运行已停止",
    `run ID: ${metadata.runId}`,
    `运行目录: ${metadata.runDir}`,
    `资源状态: ${JSON.stringify(details)}`,
  ].join("；");
}

function checkStatus(status) {
  if (!status || typeof status !== "object" || typeof status.hasResources !== "boolean")
    throw Error("Docker 资源状态响应无效");
  return status;
}

/**
 * Atomically create a project lock.  The lock is backed by a detached holder
 * process so the documented start/scan/stop CLI sequence keeps the lock
 * alive across separate Node processes.
 */
export async function acquireProjectLock(
  metadata,
  {
    lockRoot = LOCK_ROOT,
    currentDocker,
    resourceStatus,
    cleanupResources,
    cleanupStale,
    holdProcess = true,
    starting = false,
    claimPublication,
    processApi = process,
  } = {},
) {
  if (!PROJECT_KEY.test(metadata.projectKey)) throw Error("无效项目 key");
  if (typeof metadata.projectIdentity !== "string" || projectKey(metadata.projectIdentity) !== metadata.projectKey)
    throw Error("项目锁 project key 与原始项目身份不匹配");
  if (!RUN_ID.test(metadata.runId)) throw Error("无效运行 ID");
  if (typeof metadata.runDir !== "string" || !metadata.runDir)
    throw Error("运行目录不能为空");
  if (!currentDocker) throw Error("无法确认当前 Docker context、socket 和 daemon");
  fs.mkdirSync(lockRoot, { recursive: true, mode: 0o700 });
  fs.chmodSync(lockRoot, 0o700);
  const lockDir = projectLockPath(metadata.projectKey, lockRoot);
  if (!sameDocker(currentDocker, metadata.docker) && !fs.existsSync(lockDir))
    throw Error("无法绑定当前 Docker context、socket 和 daemon");
  let created = false;
  try {
    fs.mkdirSync(lockDir, { mode: 0o700 });
    created = true;
  } catch (error) {
    if (error?.code !== "EEXIST") throw error;
  }

  // A reclaimer owns this marker until it has published its new generation.
  // Never remove/recreate the project directory between stale cleanup and
  // publication: contenders that read an old generation must re-read it
  // while holding the same mutation marker.
  let mutation;
  if (!created) {
    const existing = readLock(lockDir);
    if (!sameDocker(existing.docker, currentDocker))
      throw Error("Docker context、socket 或 daemon 已变化；保留已有项目锁和凭据");
    // An active lease is read-only rejection, not a mutation. Do not hold
    // the mutation marker across Docker status requests that can block stop.
    if (isProcessAlive(existing.pid, processApi)) {
      const status = checkStatus(await resourceStatus(existing));
      throw Error(activeMessage(existing, status.details ?? status));
    }
    mutation = claimMutation(lockDir);
  }
  let pid = process.pid;
  let releasePublication;
  try {
    if (!created) {
      const existing = readLock(lockDir);
      if (!sameDocker(existing.docker, currentDocker))
        throw Error("Docker context、socket 或 daemon 已变化；保留已有项目锁和凭据");
      const status = checkStatus(await resourceStatus(existing));
      if (isProcessAlive(existing.pid, processApi))
        throw Error(activeMessage(existing, status.details ?? status));
      if (fs.existsSync(leaseMarker(lockDir, existing, "startup")))
        throw Error(activeMessage(existing, { startup: "启动尚未确认结束；保留锁和资源" }));
      // The detached holder can die independently of the scanner and its
      // children. Only scan's finally or the coordinated stop path can
      // acknowledge their completion; a Docker snapshot is not sufficient.
      if (fs.existsSync(leaseMarker(lockDir, existing, "scan")))
        throw Error(activeMessage(existing, { ...status.details, scan: "扫描尚未确认结束；保留锁、资源和凭据，请先执行 stop 核验清理" }));
      if (status.hasResources) {
        await cleanupResources(existing);
        const after = checkStatus(await resourceStatus(existing));
        if (after.hasResources)
          throw Error(`旧项目锁对应资源清理不完整，保留锁：${JSON.stringify(after.details ?? after)}`);
      }
      if (cleanupStale) await cleanupStale(existing);
      // Active or abandoned scan markers cannot reach publication above.
      fs.rmSync(leaseMarker(lockDir, existing, "stop"), { recursive: true, force: true });
      fs.rmSync(path.join(lockDir, "holder.pid"), { force: true });
    }
    if (claimPublication) releasePublication = await claimPublication();
    const lockMetadata = {
      projectIdentity: metadata.projectIdentity,
      projectKey: metadata.projectKey,
      runId: metadata.runId,
      runDir: path.resolve(metadata.runDir),
      // Publish a live owner before spawning the detached holder.  A second
      // concurrent start therefore sees a complete lock instead of a
      // half-written directory.
      pid: process.pid,
      ownerPid: process.pid,
      ...(starting ? { startupPid: process.pid } : {}),
      createdAt: metadata.createdAt || new Date().toISOString(),
      holderToken: crypto.randomBytes(16).toString("hex"),
      docker: {
        context: currentDocker.context,
        socket: currentDocker.socket || currentDocker.host,
        daemonId: currentDocker.daemonId,
      },
    };
    // Publish the startup marker before lock.json: cleanup must never see a
    // complete lease without also seeing its active resource creator.
    if (starting)
      fs.mkdirSync(leaseMarker(lockDir, lockMetadata, "startup"), { mode: 0o700 });
    writeJson(path.join(lockDir, "lock.json"), lockMetadata);
    if (holdProcess) {
      pid = startHolder(lockDir, lockMetadata.holderToken);
      lockMetadata.pid = pid;
      writeJson(path.join(lockDir, "lock.json"), lockMetadata);
    }
    return { lockDir, metadata: lockMetadata };
  } catch (error) {
    if (created) fs.rmSync(lockDir, { recursive: true, force: true });
    throw error;
  } finally {
    releasePublication?.();
    if (mutation) fs.rmdirSync(mutation);
  }
}

function claimMutation(lockDir) {
  const marker = path.join(lockDir, ".mutation");
  try { fs.mkdirSync(marker, { mode: 0o700 }); }
  catch (error) {
    if (error.code !== "EEXIST") throw error;
    const existing = readLock(lockDir);
    throw Error(activeMessage(existing, { lock: "正在回收或释放；未完成的回收标记必须人工检查，保留锁" }));
  }
  return marker;
}

export function assertProjectLock(
  metadata,
  { lockRoot = LOCK_ROOT, currentDocker, processApi = process } = {},
) {
  const current = readProjectLock(metadata.projectKey, lockRoot);
  if (!current) return false;
  if (
    current.metadata.runId !== metadata.runId ||
    path.resolve(current.metadata.runDir) !== path.resolve(metadata.runDir)
  )
    throw Error(
      activeMessage(current.metadata, {
        lock: "belongs-to-another-run",
      }),
    );
  if (!sameDocker(current.metadata.docker, currentDocker))
    throw Error("Docker context、socket 或 daemon 已变化；保留项目锁和凭据");
  if (metadata.holderToken !== undefined && current.metadata.holderToken !== metadata.holderToken)
    throw Error("项目锁租约已变化；保留新运行的资源和凭据");
  if (!isProcessAlive(current.metadata.pid, processApi))
    throw Error("项目锁持有进程已退出；请先执行 stop 清理当前 run");
  return true;
}

// Hold this claim from before the first resource deletion through credential
// removal and lease release. Failure retains the lease for a safe retry.
export function claimProjectCleanup(
  metadata,
  { lockRoot = LOCK_ROOT, currentDocker } = {},
) {
  const lockDir = projectLockPath(metadata.projectKey, lockRoot);
  if (!fs.existsSync(lockDir)) {
    if (metadata.holderToken) throw Error("原项目锁租约已结束；拒绝清理后续运行");
    return { async waitForStartup() {}, release() {}, dispose() {} };
  }
  const mutation = claimMutation(lockDir);
  let removed = false;
  try {
    const current = readLock(lockDir);
    if (
      current.runId !== metadata.runId ||
      path.resolve(current.runDir) !== path.resolve(metadata.runDir)
    )
      throw Error("拒绝释放其他运行的项目锁");
    if (!sameDocker(current.docker, currentDocker))
      throw Error("Docker context、socket 或 daemon 已变化；保留项目锁和凭据");
    if (metadata.holderToken !== undefined && current.holderToken !== metadata.holderToken)
      throw Error("项目锁租约已变化；保留新运行的资源和凭据");
    return {
      async waitForStartup({ timeoutMs = 10000 } = {}) {
        const startup = leaseMarker(lockDir, current, "startup");
        if (!fs.existsSync(startup)) return;
        try { fs.mkdirSync(leaseMarker(lockDir, current, "stop"), { mode: 0o700 }); }
        catch (error) { if (error.code !== "EEXIST") throw error; }
        const deadline = Date.now() + timeoutMs;
        while (fs.existsSync(startup)) {
          if (!isProcessAlive(current.startupPid))
            throw Error("启动进程已退出但未确认子进程停止；保留项目锁、资源和凭据，请检查运行现场");
          if (Date.now() >= deadline)
            throw Error("等待启动流程停止超时；保留项目锁、资源和凭据");
          await new Promise((resolve) => setTimeout(resolve, 50));
        }
      },
      release() {
        if (removed) return;
        if (fs.existsSync(leaseMarker(lockDir, current, "startup")))
          throw Error("启动尚未确认结束，拒绝释放项目锁");
        fs.rmSync(lockDir, { recursive: true, force: false });
        removed = true;
        if (fs.existsSync(lockDir)) throw Error("项目锁释放后仍然残留");
      },
      dispose() {
        if (!removed && fs.existsSync(mutation)) fs.rmdirSync(mutation);
      },
    };
  } catch (error) {
    fs.rmdirSync(mutation);
    throw error;
  }
}

export function releaseProjectLock(metadata, options) {
  const claim = claimProjectCleanup(metadata, options);
  try { claim.release(); }
  finally { claim.dispose(); }
}

function leaseMarker(lockDir, metadata, phase) {
  if (!/^[a-f0-9]{32}$/.test(metadata.holderToken))
    throw Error(`项目锁缺少有效租约标识：${lockDir}`);
  return path.join(lockDir, `.${phase}-${metadata.holderToken}`);
}

function removeEmptyDirectory(directory) {
  try { fs.rmdirSync(directory); }
  catch (error) {
    if (!["ENOENT", "ENOTEMPTY", "EEXIST"].includes(error.code)) throw error;
  }
}

async function claimRunTransition(directory, check, deadline) {
  const transition = path.join(directory, "transition");
  while (true) {
    check();
    try { fs.mkdirSync(transition, { mode: 0o700 }); }
    catch (error) {
      if (error.code !== "EEXIST") throw error;
      if (Date.now() >= deadline) throw Error("等待启动状态切换超时；保留运行现场");
      await new Promise((resolve) => setTimeout(resolve, 50));
      continue;
    }
    return () => fs.rmdirSync(transition);
  }
}

// Register resource creators synchronously before their first await. This gate
// also covers startup preflight before a Docker identity/project lease exists.
// Empty/abandoned gates fail closed; never infer that a creator has stopped.
function claimRunOperation(run, signal, operation) {
  const directory = path.join(run, ".lifecycle");
  // Keep the existing owner filename so older unfinished starts fail closed.
  const owner = path.join(directory, "startup.json");
  const stop = path.join(directory, "cleanup", "request");
  try { fs.mkdirSync(directory, { mode: 0o700 }); }
  catch (error) {
    if (error.code !== "EEXIST") throw error;
    throw Error("当前 run 已有启动、扫描或清理操作；保留运行现场，请等待或检查 .lifecycle");
  }
  try { writeJson(owner, { pid: process.pid, operation }); }
  catch (error) { removeEmptyDirectory(directory); throw error; }
  const controller = new AbortController();
  const onAbort = () => controller.abort(signal.reason);
  signal?.addEventListener("abort", onAbort, { once: true });
  if (signal?.aborted) onAbort();
  let finished = false, retired = false;
  const check = () => {
    retired ||= fs.existsSync(stop);
    if (retired) controller.abort(Error(`当前 run 已请求停止，${operation}流程已取消`));
  };
  const timer = setInterval(check, 50);
  return {
    signal: controller.signal,
    async claimPublication() {
      const verify = () => {
        check();
        if (controller.signal.aborted)
          throw controller.signal.reason instanceof Error ? controller.signal.reason : Error("当前启动已取消");
      };
      const release = await claimRunTransition(directory, verify, Date.now() + 10000);
      // Recheck under the same transition used by cleanup's validation and
      // cancellation publication; no new lease can slip between those steps.
      try { verify(); return release; }
      catch (error) { release(); throw error; }
    },
    finish() {
      if (finished) return retired;
      check();
      clearInterval(timer);
      signal?.removeEventListener("abort", onAbort);
      // The caller has settled all resource-creating commands (and any
      // project startup marker) before acknowledging cleanup.
      fs.unlinkSync(owner);
      removeEmptyDirectory(directory);
      check();
      finished = true;
      return retired;
    },
  };
}

export function claimRunStartup(run, signal) {
  return claimRunOperation(run, signal, "启动");
}

export function claimRunScan(run, signal) {
  return claimRunOperation(run, signal, "扫描");
}

// Keep the gate throughout cleanup so a new start/scan cannot enter after the
// absence check and create resources after stop has reported success.
export function claimRunCleanup(run, { beforeStop = () => {} } = {}) {
  const directory = path.join(run, ".lifecycle");
  const owner = path.join(directory, "startup.json");
  const stop = path.join(directory, "cleanup");
  let created = false;
  try { fs.mkdirSync(directory, { mode: 0o700 }); created = true; }
  catch (error) { if (error.code !== "EEXIST") throw error; }
  if (!created && !fs.existsSync(owner) && !fs.existsSync(stop))
    throw Error("启动状态尚未确认；保留运行现场，请重试或检查 .lifecycle");
  try { fs.mkdirSync(stop, { mode: 0o700 }); }
  catch (error) {
    if (created) removeEmptyDirectory(directory);
    if (error.code !== "EEXIST") throw error;
    throw Error("当前 run 正在清理；保留运行现场");
  }
  const dispose = () => {
    fs.rmSync(path.join(stop, "request"), { force: true });
    fs.rmdirSync(stop);
    removeEmptyDirectory(directory);
  };
  return {
    async waitForStartup({ timeoutMs = 10000 } = {}) {
      const deadline = Date.now() + timeoutMs;
      const release = await claimRunTransition(directory, () => {}, deadline);
      try {
        // Validate and request cancellation atomically with lease publication.
        beforeStop();
        fs.writeFileSync(path.join(stop, "request"), "", { mode: 0o600 });
      } finally { release(); }
      while (fs.existsSync(owner)) {
        let metadata;
        try { metadata = JSON.parse(fs.readFileSync(owner, "utf8")); }
        catch (error) { if (error.code === "ENOENT") break; throw error; }
        if (!isProcessAlive(metadata.pid))
          throw Error(`${metadata.operation || "启动"}进程已退出但未确认子进程停止；保留运行现场，请检查 .lifecycle`);
        if (Date.now() >= deadline)
          throw Error(`等待${metadata.operation || "启动"}流程停止超时；保留运行现场`);
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
    },
    dispose,
  };
}

// Only the creator aborts its own children. Cleanup requests cancellation by
// lease token and waits for finish(), never signalling a persisted PID.
export function watchStartup({ lockDir, metadata }, signal) {
  const controller = new AbortController();
  const startup = leaseMarker(lockDir, metadata, "startup");
  const stop = leaseMarker(lockDir, metadata, "stop");
  let finished = false, retired = false;
  const onAbort = () => controller.abort(signal.reason);
  signal?.addEventListener("abort", onAbort, { once: true });
  if (signal?.aborted) onAbort();
  const check = () => {
    try {
      retired ||= fs.existsSync(stop) || readLock(lockDir).holderToken !== metadata.holderToken;
    } catch { retired = true; }
    if (retired) controller.abort(Error("当前 run 已请求停止，启动流程已取消"));
  };
  check();
  const timer = setInterval(check, 50);
  return {
    signal: controller.signal,
    finish() {
      if (finished) return retired;
      check();
      clearInterval(timer);
      signal?.removeEventListener("abort", onAbort);
      // All creator commands must have settled before acknowledging stop.
      try { fs.rmdirSync(startup); }
      catch (error) { if (error.code !== "ENOENT") throw error; }
      finished = true;
      return retired;
    },
  };
}

// Claim before asynchronous work so a rejected caller never owns the active
// scan's failure cleanup. A generation-specific path can be released without
// contending with a start that is inspecting resources under .mutation.
export function acquireScanLock(metadata, options = {}) {
  const lockDir = projectLockPath(metadata.projectKey, options.lockRoot);
  if (!fs.existsSync(lockDir)) return null;
  const mutation = claimMutation(lockDir);
  try {
    assertProjectLock(metadata, options);
    const current = readLock(lockDir);
    if (fs.existsSync(leaseMarker(lockDir, current, "startup")))
      throw Error("当前 run 仍在启动，拒绝扫描");
    if (fs.existsSync(leaseMarker(lockDir, current, "stop")))
      throw Error("当前 run 已请求停止，请先完成清理");
    const marker = leaseMarker(lockDir, current, "scan");
    try { fs.mkdirSync(marker, { mode: 0o700 }); }
    catch (error) {
      if (error.code !== "EEXIST") throw error;
      throw Error(activeMessage(metadata, { scanner: "当前 run 已有 scan 调用；保留资源，拒绝并发扫描" }));
    }
    let released = false;
    const release = () => {
      if (released) return;
      try { fs.rmdirSync(marker); }
      catch (error) { if (error.code !== "ENOENT") throw error; }
      released = true;
    };
    release.holderToken = current.holderToken;
    return release;
  } finally {
    fs.rmdirSync(mutation);
  }
}

if (
  process.argv[2] === "--hold" &&
  process.argv[1] &&
  pathToFileURL(fs.realpathSync(process.argv[1])).href === import.meta.url
) {
  const lockDir = process.argv[3], holderToken = process.argv[4];
  // Never signal a PID read from disk: it may now identify an unrelated
  // process. Each holder terminates itself when its lease is removed/replaced.
  const checkLease = () => {
    try {
      if (readLock(lockDir).holderToken === holderToken) return;
    } catch {
      // Deleted or invalid lease cannot keep this holder alive.
    }
    process.exit(0);
  };
  checkLease();
  setInterval(checkLease, 100);
}
