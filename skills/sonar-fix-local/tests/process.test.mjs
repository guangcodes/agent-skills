import test from "node:test";
import assert from "node:assert/strict";
import { runChild } from "../scripts/process.mjs";

test("子进程超时后升级 SIGKILL 并结束等待", { timeout: 5000 }, async () => {
  const began = Date.now();
  await assert.rejects(runChild(process.execPath, ["-e", "process.on('SIGTERM',()=>{});setInterval(()=>{},1000)"], {
    timeoutMs: 300, graceMs: 100,
  }), /超时/);
  assert.ok(Date.now() - began < 3000);
});

test("已取消的命令不启动子进程", async () => {
  const controller = new AbortController();
  controller.abort(Error("cancelled-before-spawn"));
  await assert.rejects(runChild("nonexistent-command", [], { signal: controller.signal }), /cancelled-before-spawn/);
});

test("子进程输出完整保留且启动失败不会挂起", async () => {
  const output = "x".repeat(100000);
  const result = await runChild(process.execPath, ["-e", `process.stdout.write(${JSON.stringify(output)});process.stderr.write('error-log')`]);
  assert.equal(result.code, 0);
  assert.equal(result.stdout, output);
  assert.equal(result.stderr, "error-log");
  await assert.rejects(runChild("/nonexistent-sonar-test-command", []), /ENOENT/);
});
