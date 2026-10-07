"""Reviewer launch policy. Kept locally in each Skill; no sibling runtime imports."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path


REVIEW_TIMEOUT_SECONDS = 1800
POLICY_ARGS = ["--ignore-rules", "--strict-config"]
for setting in (
    'approval_policy="never"',
    "features.apps=false",
    "features.plugins=false",
    "features.hooks=false",
    "features.browser_use=false",
    "features.computer_use=false",
    "features.multi_agent=false",
    "features.memories=false",
    'web_search="disabled"',
    "notify=[]",
):
    POLICY_ARGS.extend(["-c", setting])


class CapabilityCheckError(Exception):
    def __init__(self, message: str, stderr: bytes = b""):
        super().__init__(message)
        self.diagnostics = message.encode() + b"\n" + stderr


def external_review_temp_root(root: Path, env: dict[str, str]) -> Path:
    # Never create diagnostics or reviewer inputs inside a repository-local TMPDIR.
    candidates = [env.get(key) for key in ("TMPDIR", "TEMP", "TMP")]
    candidates.extend(("/tmp", "/private/tmp", "/var/tmp"))
    for candidate in candidates:
        if candidate:
            directory = Path(candidate).resolve()
            if (not directory.is_relative_to(root.resolve()) and directory.is_dir()
                    and os.access(directory, os.W_OK | os.X_OK)):
                return directory
    raise ValueError("no writable system temporary directory outside the worktree")


def disabled_mcp_args(reviewer: str, cwd: Path, env: dict[str, str]) -> list[str]:
    # Empty TOML tables merge with inherited config; they do not disable every MCP.
    # Inspect metadata only, never connect servers or print credential-bearing JSON.
    config_args = POLICY_ARGS[2:]  # mcp list rejects exec-only options, including --strict-config.

    def inventory(extra: list[str]) -> list[dict]:
        try:
            result = subprocess.run(
                [reviewer, *config_args, *extra, "mcp", "list", "--json"],
                cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CapabilityCheckError("MCP inventory timed out", exc.stderr or b"") from exc
        except OSError as exc:
            raise CapabilityCheckError(f"MCP inventory launch failed: errno={exc.errno}") from exc
        if result.returncode:
            raise CapabilityCheckError(f"MCP inventory exited with status {result.returncode}", result.stderr)
        try:
            servers = json.loads(result.stdout)
            if not isinstance(servers, list) or any(
                not isinstance(server, dict)
                or not isinstance(server.get("name"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]+", server["name"])
                or not isinstance(server.get("enabled"), bool)
                for server in servers
            ):
                raise ValueError("unsupported inventory")
            return servers
        except ValueError as exc:
            # The inventory stdout can contain credentials, even when malformed.
            raise CapabilityCheckError("invalid MCP inventory JSON or schema", result.stderr) from exc

    overrides = []
    for name in sorted({server["name"] for server in inventory([])}):
        overrides.extend(["-c", f"mcp_servers.{name}.enabled=false"])
    if any(server["enabled"] for server in inventory(overrides)):
        raise CapabilityCheckError("an MCP server remains enabled")
    return overrides


def run_reviewer(
    command: list[str], cwd: Path, env: dict[str, str], *, capture: bool = True,
    timeout: float = REVIEW_TIMEOUT_SECONDS,
) -> tuple[int, bytes, bytes]:
    try:
        overrides = disabled_mcp_args(command[0], cwd, env)
    except CapabilityCheckError as exc:
        error = b"review_runtime_failure=capability_check_failed\n"
        try:
            descriptor, diagnostic_path = tempfile.mkstemp(
                prefix="review-capability-", suffix=".log", dir=external_review_temp_root(cwd, env)
            )
            with os.fdopen(descriptor, "wb") as diagnostic_file:
                diagnostic_file.write(exc.diagnostics)
            error += f"capability_diagnostics_path={diagnostic_path}\n".encode()
        except (OSError, ValueError):
            error += b"capability_diagnostics_unavailable=true\n"
        if not capture:
            sys.stderr.buffer.write(error)
        return 1, b"", error

    # Insert before the final prompt; the wrapper accepts no caller-supplied flags.
    command = [*command[:-1], *overrides, command[-1]]
    try:
        process = subprocess.Popen(
            command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            start_new_session=True,
        )
    except OSError:
        error = b"review_runtime_failure=launch_failed\n"
        if not capture:
            sys.stderr.buffer.write(error)
        return 127, b"", error
    def interrupted(_signal, _frame):
        raise KeyboardInterrupt

    previous_handler = signal.signal(signal.SIGTERM, interrupted)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return process.returncode, stdout or b"", stderr or b""
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        # Stop this review's process group, including model tool subprocesses.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        needs_drain = False
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            needs_drain = True
        finally:
            # In uncaptured mode the leader can exit while descendants ignore TERM.
            # Reap the whole group even when communicate() has already returned.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if needs_drain:
            stdout, stderr = process.communicate()
        kind = "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "interrupted"
        error = f"review_runtime_failure={kind}\n".encode()
        if not capture:
            sys.stderr.buffer.write(error)
        return (124 if kind == "timeout" else 130), stdout or b"", (stderr or b"") + error
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


def failure_kind(code: int, stdout: bytes, stderr: bytes, has_result: bool) -> str:
    if code == 0:
        return "none" if has_result else "missing_result"
    errors = stderr.decode(errors="replace")
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") in {"error", "turn.failed"}:
            errors += "\n" + json.dumps(event)
    for kind in ("capability_check_failed", "launch_failed", "timeout", "interrupted"):
        if f"review_runtime_failure={kind}" in errors:
            return kind
    # A readonly logs-db warning alone does not establish the fatal failure.
    if re.search(
        r"(?mi)^Error: failed to initialize in-process app-server client:.*"
        r"(?:Operation not permitted|Permission denied|os error (?:1|13)\b)", errors
    ):
        return "startup_permission_denied"
    if re.search(r"(?i)\b401\b|\bunauthorized\b|missing (?:bearer|authentication)", errors):
        return "authentication_failed"
    if any(marker in errors for marker in ('"type": "turn.failed"', '"type": "error"')):
        return "request_failed"
    return "reviewer_failed"


def main() -> int:
    if sys.argv[1:] == ["policy-args"]:
        print("\n".join(POLICY_ARGS))
        return 0
    if sys.argv[1] == "classify":
        _, _, code, events, errors, result = sys.argv
        result_path = Path(result)
        kind = failure_kind(
            int(code), Path(events).read_bytes(), Path(errors).read_bytes(),
            result_path.is_file() and result_path.stat().st_size > 0,
        )
        print(kind)
        return 0
    code, _, _ = run_reviewer(sys.argv[1:], Path.cwd(), dict(os.environ), capture=False)
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    raise SystemExit(main())
