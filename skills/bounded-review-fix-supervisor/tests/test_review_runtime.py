import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "review_runtime.py"
SPEC = importlib.util.spec_from_file_location("tested_review_runtime", SCRIPT)
RUNTIME = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNTIME)


class RuntimeTests(unittest.TestCase):
    def test_vendored_policy_stays_identical(self):
        other = SCRIPT.parents[2] / "code-review-fix-loop/scripts/review_runtime.py"
        self.assertEqual(SCRIPT.read_bytes(), other.read_bytes())

    def test_classification_uses_fatal_errors_not_log_warnings_or_reviewed_code(self):
        warning = b"WARN failed to run startup maintenance for logs db: attempt to write a readonly database"
        denied = b"Error: failed to initialize in-process app-server client: Operation not permitted (os error 1)"
        cases = [
            (0, b"", warning, True, "none"),
            (0, b"", warning, False, "missing_result"),
            (1, b"", warning, False, "reviewer_failed"),
            (1, b"", warning + b"\n" + denied, False, "startup_permission_denied"),
            (1, json.dumps({"type": "item.completed", "output": denied.decode()}).encode(), b"", False, "reviewer_failed"),
            (1, b'{"type":"error","message":"401 Unauthorized"}', b"", False, "authentication_failed"),
            (1, b'{"type":"turn.failed","error":{"message":"connection lost"}}', b"", False, "request_failed"),
            (124, b"", b"review_runtime_failure=timeout", False, "timeout"),
        ]
        for code, out, err, result, expected in cases:
            with self.subTest(expected=expected, code=code):
                self.assertEqual(RUNTIME.failure_kind(code, out, err, result), expected)

    def test_mcp_inventory_disables_inherited_servers_and_rechecks(self):
        calls = []
        def inventory(command, **kwargs):
            calls.append(command)
            disabled = "mcp_servers.custom-server.enabled=false" in command
            data = [{"name": "custom-server", "enabled": not disabled, "secret": "never-print-this"}]
            return subprocess.CompletedProcess(command, 0, json.dumps(data).encode(), b"")
        with patch.object(RUNTIME.subprocess, "run", side_effect=inventory):
            result = RUNTIME.disabled_mcp_args("codex", Path.cwd(), dict(os.environ))
        self.assertEqual(result, ["-c", "mcp_servers.custom-server.enabled=false"])
        self.assertEqual(len(calls), 2)
        self.assertIn("features.plugins=false", calls[0])
        self.assertIn("features.hooks=false", calls[0])
        self.assertIn("features.apps=false", calls[0])

    def test_unsafe_or_unverifiable_inventory_never_starts_reviewer(self):
        outputs = [b"broken", b"{}", b'[{"name":"unsafe.name","enabled":true}]',
                   b'[{"name":"server","enabled":true}]', b'[{"name":"server"}]']
        for output in outputs:
            with self.subTest(output=output), patch.object(
                RUNTIME.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output, b"secret")
            ), patch.object(RUNTIME.subprocess, "Popen") as launch:
                code, out, err = RUNTIME.run_reviewer(["codex", "prompt"], Path.cwd(), dict(os.environ))
                self.assertEqual(code, 1)
                self.assertTrue(err.startswith(b"review_runtime_failure=capability_check_failed\n"))
                self.assertNotIn(b"secret", err)
                diagnostic_path = Path(err.decode().split("capability_diagnostics_path=", 1)[1].strip())
                try:
                    self.assertEqual(diagnostic_path.stat().st_mode & 0o777, 0o600)
                    self.assertNotIn(output, diagnostic_path.read_bytes())
                finally:
                    diagnostic_path.unlink()
                launch.assert_not_called()

    def test_capability_failure_preserves_stderr_privately_outside_worktree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            result = subprocess.CompletedProcess([], 7, b'credential-bearing-inventory', b'configuration parse failed')
            with patch.object(RUNTIME.subprocess, "run", return_value=result):
                code, _, err = RUNTIME.run_reviewer(
                    ["codex", "prompt"], root, {**os.environ, "TMPDIR": str(root)}
                )
            self.assertEqual(code, 1)
            self.assertNotIn(b"configuration parse failed", err)
            path = Path(err.decode().split("capability_diagnostics_path=", 1)[1].strip()).resolve()
            try:
                self.assertFalse(path.is_relative_to(root))
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertIn(b"configuration parse failed", path.read_bytes())
                self.assertIn(b"status 7", path.read_bytes())
                self.assertNotIn(b"credential-bearing-inventory", path.read_bytes())
            finally:
                path.unlink()

    def test_timeout_terminates_review_process_group(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = root / "descendant-survived"
            child = f"import time,pathlib;time.sleep(1);pathlib.Path({str(marker)!r}).touch()"
            script = root / "hang.py"
            script.write_text(
                "import subprocess,sys,time\n"
                f"subprocess.Popen([sys.executable,'-c',{child!r}])\n"
                "print('started',flush=True)\ntime.sleep(60)\n"
            )
            with patch.object(RUNTIME, "disabled_mcp_args", return_value=[]):
                code, out, err = RUNTIME.run_reviewer(
                    [sys.executable, str(script)], root, dict(os.environ), timeout=0.3
                )
            self.assertEqual(code, 124)
            self.assertIn(b"started", out)
            self.assertIn(b"review_runtime_failure=timeout", err)
            import time
            time.sleep(1.1)
            self.assertFalse(marker.exists())

    def test_uncaptured_timeout_kills_descendant_after_leader_exits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = root / "descendant-survived"
            ready = root / "ready"
            child = (
                "import signal,time,pathlib;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                f"pathlib.Path({str(ready)!r}).touch();time.sleep(1);"
                f"pathlib.Path({str(marker)!r}).touch()"
            )
            script = root / "hang.py"
            script.write_text(
                "import subprocess,sys,time\n"
                f"subprocess.Popen([sys.executable,'-c',{child!r}])\n"
                "time.sleep(60)\n"
            )
            with patch.object(RUNTIME, "disabled_mcp_args", return_value=[]):
                code, _, _ = RUNTIME.run_reviewer(
                    [sys.executable, str(script)], root, dict(os.environ), capture=False, timeout=0.3
                )
            self.assertEqual(code, 124)
            self.assertTrue(ready.exists())
            import time
            time.sleep(1.1)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
