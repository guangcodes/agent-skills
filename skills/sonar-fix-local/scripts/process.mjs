import { spawn } from "node:child_process";

export function interruptionError(signal) {
  if (signal?.reason instanceof Error) return signal.reason;
  const error = Error("当前操作被中断");
  error.name = "AbortError";
  return error;
}

export function throwIfAborted(signal) {
  if (signal?.aborted) throw interruptionError(signal);
}

// Each command owns a process group. Keep the escalation timer even if a
// package-manager/CLI parent exits before its workers do.
export async function runChild(command, args, {
  cwd, env, stdio = ["ignore", "pipe", "pipe"], signal,
  timeoutMs = 0, graceMs = 1000,
} = {}) {
  throwIfAborted(signal);
  return new Promise((resolve, reject) => {
    const grouped = process.platform !== "win32";
    const child = spawn(command, args, { cwd, env, stdio, detached: grouped });
    let stdout = "", stderr = "", exited = false, closed = false, code, failure;
    let killTimer, timeoutTimer, stopping = false, settled = false;
    child.stdout?.on("data", (data) => { stdout += data; });
    child.stderr?.on("data", (data) => { stderr += data; });
    const send = (name) => {
      if (!child.pid) return;
      try {
        if (grouped) process.kill(-child.pid, name);
        else child.kill(name);
      } catch (error) {
        if (error.code !== "ESRCH") failure ||= error;
      }
    };
    const finish = () => {
      if (settled || !exited || !closed || killTimer) return;
      settled = true;
      clearTimeout(timeoutTimer);
      signal?.removeEventListener("abort", onAbort);
      if (failure) reject(failure);
      else resolve({ code, stdout, stderr });
    };
    const stop = (reason, name = "SIGTERM") => {
      failure ||= reason;
      if (stopping) return;
      stopping = true;
      send(name);
      killTimer = setTimeout(() => {
        send("SIGKILL");
        killTimer = null;
        finish();
      }, graceMs);
    };
    const onAbort = () => stop(interruptionError(signal), signal.reason?.signal || "SIGTERM");
    child.once("error", (error) => {
      failure ||= error;
      exited = true;
      finish();
    });
    child.once("exit", (exitCode) => {
      code = exitCode;
      exited = true;
      // A successful wrapper can also leave workers behind. They belong to
      // this invocation and must not outlive it.
      if (grouped && !stopping && child.pid) {
        try { process.kill(-child.pid, 0); stop(null); }
        catch (error) { if (error.code !== "ESRCH") failure ||= error; }
      }
      finish();
    });
    child.once("close", () => { closed = true; finish(); });
    signal?.addEventListener("abort", onAbort, { once: true });
    if (signal?.aborted) onAbort();
    if (timeoutMs) timeoutTimer = setTimeout(() => stop(Error("子进程执行超时")), timeoutMs);
  });
}

export function waitWithSignal(milliseconds, signal) {
  throwIfAborted(signal);
  if (!signal) return new Promise((resolve) => setTimeout(resolve, milliseconds));
  return new Promise((resolve, reject) => {
    const timer = setTimeout(done, milliseconds);
    const onAbort = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", onAbort);
      reject(interruptionError(signal));
    };
    function done() {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }
    signal.addEventListener("abort", onAbort, { once: true });
  });
}
export function timeoutSignal(signal, milliseconds) {
  const controller = new AbortController();
  const timer = setTimeout(
    () => controller.abort(Error("本机请求超时")),
    milliseconds,
  );
  let onAbort;
  if (signal) {
    onAbort = () => controller.abort(signal.reason);
    if (signal.aborted) onAbort();
    else signal.addEventListener("abort", onAbort, { once: true });
  }
  return {
    signal: controller.signal,
    dispose() {
      clearTimeout(timer);
      if (signal && onAbort) signal.removeEventListener("abort", onAbort);
      // Releasing a request also closes any unread response body, including
      // non-2xx responses rejected before their body is consumed.
      controller.abort();
    },
  };
}
