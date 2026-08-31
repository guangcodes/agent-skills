import json
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "supervisor-state.py"


class SupervisorStateTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "repo"
        self.root.mkdir()
        self.fake_bin = Path(self.temp_dir.name) / "bin"
        self.fake_bin.mkdir()
        fake_codex = self.fake_bin / "codex"
        fake_codex.write_text(
            """#!/usr/bin/env python3
import os
import subprocess
import sys
from pathlib import Path

mutation = os.environ.get("FAKE_REVIEW_MUTATE")
if mutation:
    Path(mutation).write_text("changed by fake reviewer\\n", encoding="utf-8")
if os.environ.get("FAKE_REVIEW_MOVE_HEAD"):
    subprocess.run(
        ["git", "commit", "--allow-empty", "-qm", "fake reviewer moved HEAD"],
        check=True,
    )
output = os.environ.get("FAKE_REVIEW_OUTPUT")
if output:
    target = Path(sys.argv[sys.argv.index("--output-last-message") + 1])
    target.write_text(output, encoding="utf-8")
    print('{"type":"turn.completed"}')
raise SystemExit(int(os.environ.get("FAKE_REVIEW_EXIT", "0")))
""",
            encoding="utf-8",
        )
        fake_codex.chmod(0o755)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Test"], check=True)
        (self.root / "tracked.txt").write_text("base\n", encoding="utf-8")
        (self.root / "dependency.txt").write_text("dependency\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "base"], check=True)
        self.baseline_head = self.git("rev-parse", "HEAD").strip()
        (self.root / "tracked.txt").write_text("initial change\n", encoding="utf-8")

        self.requirements = Path(self.temp_dir.name) / "requirements.json"
        self.requirements.write_text(
            json.dumps(
                {
                    "original_request": "Review and repair the current local diff.",
                    "accepted_amendments": [],
                    "test_mode": "light",
                    "test_mode_reason": "user:light",
                    "required_outcomes": ["full diff review converges"],
                    "allowed_scope": ["current diff and direct dependencies"],
                    "non_goals": ["no publish"],
                    "acceptance_criteria": ["latest full review has no actionable findings"],
                    "authorization_boundary": ["project writes only"],
                    "authorized_scope_extensions": ["dependency.txt", "tests/new-regression.txt"],
                    "unresolved_decisions": [],
                }
            ),
            encoding="utf-8",
        )
        self.state = Path(self.temp_dir.name) / "state.json"
        self.init_state(self.state)

    def tearDown(self):
        try:
            os.chmod(self.root / "secret-token.pem", 0o600)
        except FileNotFoundError:
            pass
        self.temp_dir.cleanup()

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            text=True,
            capture_output=True,
            check=True,
        ).stdout

    def invoke(self, *args, expected=0, env=None):
        process_env = os.environ.copy()
        process_env["PATH"] = f"{self.fake_bin}{os.pathsep}{process_env['PATH']}"
        process_env.update(env or {})
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            text=True,
            capture_output=True,
            check=False,
            env=process_env,
        )
        self.assertEqual(result.returncode, expected, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def init_state(self, state, *, root=None, baseline=None, max_rounds="4", expected=0):
        return self.invoke(
            "init",
            "--state",
            str(state),
            "--project-root",
            str(root or self.root),
            "--baseline-head",
            baseline or self.baseline_head,
            "--requirements-file",
            str(self.requirements),
            "--max-rounds",
            max_rounds,
            expected=expected,
        )

    def current_paths(self):
        staged = self.git("diff", "--cached", "--name-only", "HEAD", "--").splitlines()
        unstaged = self.git("diff", "--name-only", "--").splitlines()
        untracked = self.git("ls-files", "--others", "--exclude-standard").splitlines()
        return sorted(set(staged + unstaged + untracked))

    def drift_file(self, *, result="aligned", digest=None, paths=None, screening=None):
        state = self.invoke("status", "--state", str(self.state))
        untracked = self.git("ls-files", "--others", "--exclude-standard").splitlines()
        path = Path(self.temp_dir.name) / "drift-check.json"
        path.write_text(
            json.dumps(
                {
                    "result": result,
                    "requirement_digest": digest or state["requirement_digest"],
                    "original_request": state["requirements"]["original_request"],
                    "accepted_amendments": state["requirements"]["accepted_amendments"],
                    "current_diff_files": paths if paths is not None else self.current_paths(),
                    "planned_actions": ["review complete diff"],
                    "alignment_summary": "checked against the original request",
                    "untracked_review_screening": (
                        screening
                        if screening is not None
                        else [
                            {
                                "path": item,
                                "classification": "safe_to_review",
                                "reason": "test fixture contains no sensitive material",
                            }
                            for item in untracked
                        ]
                    ),
                }
            ),
            encoding="utf-8",
        )
        return path

    def begin(self, *, expected=0, drift_file=None):
        return self.invoke(
            "begin-review",
            "--state",
            str(self.state),
            "--drift-check-file",
            str(drift_file or self.drift_file()),
            expected=expected,
        )

    def record_review(
        self,
        findings,
        complete="true",
        expected=0,
        mutate=None,
        move_head=False,
    ):
        env = {"FAKE_REVIEW_EXIT": "0" if complete == "true" else "1"}
        if complete == "true":
            env["FAKE_REVIEW_OUTPUT"] = json.dumps(
                {"coverage_complete": True, "findings": findings}
            )
        if mutate is not None:
            env["FAKE_REVIEW_MUTATE"] = str(mutate)
        if move_head:
            env["FAKE_REVIEW_MOVE_HEAD"] = "1"
        return self.invoke(
            "execute-review",
            "--state",
            str(self.state),
            expected=expected,
            env=env,
        )

    def finding(self, fingerprint, status="actionable"):
        return {
            "fingerprint": f"tracked.txt|symbol|{fingerprint}|trigger",
            "status": status,
            "severity": "P1",
            "location": "tracked.txt:1",
            "summary": fingerprint,
        }

    def canonical_fingerprint(self, label):
        return hashlib.sha256(
            f"tracked.txt\0symbol\0{label}\0trigger".encode("utf-8")
        ).hexdigest()

    def record_fix(self, marker, *, expected=0, **overrides):
        (self.root / "tracked.txt").write_text(f"fixed {marker}\n", encoding="utf-8")
        result = {
            "status": "complete",
            "fix_rounds": 1,
            "unresolved_count": 0,
            "coverage_status": "partial",
            "validation_status": "focused",
            "requested_test_mode": "light",
            "effective_test_mode": "light",
            "test_mode_reason": "user:light",
        }
        result.update(overrides)
        result_file = Path(self.temp_dir.name) / "fix-result.json"
        result_file.write_text(json.dumps(result), encoding="utf-8")
        return self.invoke(
            "record-fix",
            "--state",
            str(self.state),
            "--skill-result-file",
            str(result_file),
            expected=expected,
        )

    def test_zero_findings_completes(self):
        self.begin()
        result = self.record_review([])
        self.assertEqual(result["status"], "complete")

    def test_fix_result_must_report_the_transferred_test_mode(self):
        self.begin()
        self.record_review([self.finding("test-mode-transfer")])
        result = self.record_fix(
            "test-mode-mismatch",
            requested_test_mode="none",
            effective_test_mode="none",
            test_mode_reason="default",
            expected=3,
        )
        self.assertEqual(
            result["exit_reason"],
            "code_review_fix_loop_test_mode_mismatch",
        )
        self.assertEqual(result["expected_test_mode"], "light")

    def test_fix_result_cannot_downgrade_the_transferred_test_mode(self):
        self.begin()
        self.record_review([self.finding("test-mode-downgrade")])
        result = self.record_fix(
            "test-mode-downgrade",
            requested_test_mode="deep",
            effective_test_mode="light",
            test_mode_reason="invalid downgrade",
            expected=2,
        )
        self.assertEqual(
            result["error"],
            "effective_test_mode cannot downgrade requested_test_mode",
        )

    def test_max_rounds_defaults_to_four_and_cannot_exceed_four(self):
        current = self.invoke("status", "--state", str(self.state))
        self.assertEqual(current["max_full_review_rounds"], 4)
        state = Path(self.temp_dir.name) / "too-many.json"
        result = self.init_state(state, max_rounds="5", expected=2)
        self.assertIn("between 1 and 4", result["error"])

    def test_max_rounds_accepts_a_user_selected_value_up_to_four(self):
        state = Path(self.temp_dir.name) / "two-rounds.json"
        result = self.init_state(state, max_rounds="2")
        self.assertEqual(result["max_full_review_rounds"], 2)

    def test_state_file_cannot_be_inside_project_root(self):
        result = self.init_state(self.root / ".supervisor-state.json", expected=2)
        self.assertIn("outside project root", result["error"])

    def test_init_rejects_non_git_root_and_invalid_baseline(self):
        non_git = Path(self.temp_dir.name) / "not-git"
        non_git.mkdir()
        result = self.init_state(
            Path(self.temp_dir.name) / "non-git.json",
            root=non_git,
            expected=2,
        )
        self.assertIn("Git repository", result["error"])
        result = self.init_state(
            Path(self.temp_dir.name) / "wrong-head.json",
            baseline="not-a-real-sha",
            expected=2,
        )
        self.assertIn("baseline HEAD", result["error"])

    def test_init_rejects_unresolved_requirement_decisions(self):
        requirements = json.loads(self.requirements.read_text(encoding="utf-8"))
        requirements["unresolved_decisions"] = ["choose authorization semantics"]
        self.requirements.write_text(json.dumps(requirements), encoding="utf-8")
        result = self.init_state(
            Path(self.temp_dir.name) / "unresolved-requirements.json",
            expected=2,
        )
        self.assertIn("unresolved decisions", result["error"])

    def test_init_accepts_a_legal_colon_prefixed_git_path(self):
        (self.root / ":config").write_text("valid git path\n", encoding="utf-8")
        result = self.init_state(Path(self.temp_dir.name) / "colon-path.json")
        self.assertIn(":config", result["initial_scope_files"])

    def test_init_serializes_a_non_utf8_git_path(self):
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["diagnostic_path"] = "non-utf8-\udcff"
        self.state.write_text(
            json.dumps(state, ensure_ascii=True),
            encoding="ascii",
        )
        result = self.invoke("status", "--state", str(self.state))
        self.assertTrue(result["diagnostic_path"].startswith("non-utf8-"))

    def test_head_drift_blocks_before_review(self):
        subprocess.run(["git", "-C", str(self.root), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "external commit"], check=True)
        result = self.begin(expected=3)
        self.assertEqual(result["exit_reason"], "baseline_head_changed")
        self.assertEqual(result["full_review_round"], 0)

    def test_staged_change_is_in_scope_when_worktree_matches_head(self):
        (self.root / "dependency.txt").write_text("staged dependency\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "dependency.txt"], check=True)
        (self.root / "dependency.txt").write_text("dependency\n", encoding="utf-8")
        paths_file = Path(self.temp_dir.name) / "staged-scope.json"
        paths_file.write_text(json.dumps(["dependency.txt"]), encoding="utf-8")
        self.invoke(
            "extend-scope",
            "--state",
            str(self.state),
            "--paths-file",
            str(paths_file),
            "--reason",
            "user_authorized",
        )
        started = self.begin()
        self.assertIn("dependency.txt", started["history"][-1]["diff_files"])

    def test_index_change_during_review_invalidates_snapshot(self):
        self.begin()
        subprocess.run(["git", "-C", str(self.root), "add", "tracked.txt"], check=True)
        result = self.record_review([], expected=3)
        self.assertEqual(result["exit_reason"], "review_workspace_changed_before_reviewer_launch")

    def test_hash_diff_does_not_read_untracked_file_contents(self):
        candidate = self.root / "benign-note.dat"
        candidate.write_text("safe fixture\n", encoding="utf-8")
        candidate.chmod(0)
        result = self.invoke("hash-diff", "--state", str(self.state))
        self.assertEqual(result["untracked_hash_mode"], "metadata-only")
        self.assertIn("benign-note.dat", result["untracked_paths"])

    def test_hash_diff_does_not_read_untracked_symlink_target(self):
        (self.root / "benign-link").symlink_to("sensitive-target-text")
        result = self.invoke("hash-diff", "--state", str(self.state), expected=3)
        self.assertEqual(result["exit_reason"], "unsafe_untracked_file")
        self.assertEqual(result["sensitive_path"], "benign-link")
        self.assertNotIn("os.readlink", SCRIPT.read_text(encoding="utf-8"))

    def test_hash_diff_blocks_sensitive_tracked_path_before_content_hashing(self):
        sensitive = self.root / ".env.local"
        sensitive.write_text("placeholder\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", ".env.local"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "tracked fixture"], check=True)
        self.baseline_head = self.git("rev-parse", "HEAD").strip()
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["baseline_head"] = self.baseline_head
        sensitive.write_text("changed placeholder\n", encoding="utf-8")
        state["initial_scope_files"] = [".env.local", "tracked.txt"]
        state["approved_scope_files"] = [".env.local", "tracked.txt"]
        self.state.write_text(json.dumps(state), encoding="utf-8")

        result = self.invoke("hash-diff", "--state", str(self.state), expected=3)

        self.assertEqual(result["exit_reason"], "sensitive_changed_path")
        self.assertEqual(result["sensitive_path"], ".env.local")

    def test_rename_from_sensitive_tracked_path_is_screened_before_hashing(self):
        sensitive = self.root / ".env.local"
        sensitive.write_text("placeholder\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", ".env.local"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "tracked fixture"], check=True)
        self.baseline_head = self.git("rev-parse", "HEAD").strip()
        subprocess.run(
            ["git", "-C", str(self.root), "mv", ".env.local", "config.txt"],
            check=True,
        )
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["baseline_head"] = self.baseline_head
        state["initial_scope_files"] = [".env.local", "config.txt", "tracked.txt"]
        state["approved_scope_files"] = [".env.local", "config.txt", "tracked.txt"]
        self.state.write_text(json.dumps(state), encoding="utf-8")

        result = self.invoke("hash-diff", "--state", str(self.state), expected=3)

        self.assertEqual(result["exit_reason"], "sensitive_changed_path")
        self.assertEqual(result["sensitive_path"], ".env.local")

    def test_begin_review_blocks_sensitive_untracked_path(self):
        (self.root / "secret-token.pem").write_text("not-a-real-secret\n", encoding="utf-8")
        result = self.begin(expected=3)
        self.assertEqual(result["exit_reason"], "sensitive_changed_path")

    def test_begin_review_blocks_sensitive_tracked_path(self):
        sensitive = self.root / ".env.local"
        sensitive.write_text("placeholder\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", ".env.local"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "tracked fixture"], check=True)
        self.baseline_head = self.git("rev-parse", "HEAD").strip()
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["baseline_head"] = self.baseline_head
        sensitive.write_text("changed placeholder\n", encoding="utf-8")
        state["initial_scope_files"] = [".env.local", "tracked.txt"]
        state["approved_scope_files"] = [".env.local", "tracked.txt"]
        self.state.write_text(json.dumps(state), encoding="utf-8")

        result = self.begin(expected=3)

        self.assertEqual(result["exit_reason"], "sensitive_changed_path")
        self.assertEqual(result["sensitive_path"], ".env.local")

    def test_begin_review_requires_complete_untracked_screening(self):
        (self.root / "benign-note.txt").write_text("safe fixture\n", encoding="utf-8")
        result = self.begin(expected=3, drift_file=self.drift_file(screening=[]))
        self.assertEqual(result["exit_reason"], "untracked_screening_mismatch")

    def test_requirement_digest_mismatch_blocks(self):
        result = self.begin(
            expected=3,
            drift_file=self.drift_file(digest="incorrect-digest"),
        )
        self.assertEqual(result["exit_reason"], "requirement_baseline_mismatch")

    def test_malformed_drift_result_returns_structured_validation_error(self):
        drift = json.loads(self.drift_file().read_text(encoding="utf-8"))
        drift["result"] = ["aligned"]
        path = Path(self.temp_dir.name) / "malformed-drift-result.json"
        path.write_text(json.dumps(drift), encoding="utf-8")

        result = self.begin(expected=2, drift_file=path)

        self.assertEqual(result["error"], "drift check result must be a string")

    def test_invalid_drift_paths_block_through_the_state_protocol(self):
        cases = (
            {"paths": ["../outside"], "screening": []},
            {
                "paths": self.current_paths(),
                "screening": [
                    {
                        "path": "./tracked.txt",
                        "classification": "safe_to_review",
                        "reason": "invalid fixture path",
                    }
                ],
            },
        )

        for index, values in enumerate(cases):
            with self.subTest(values=values):
                if index:
                    self.state.unlink()
                    self.init_state(self.state)
                result = self.begin(
                    expected=3,
                    drift_file=self.drift_file(**values),
                )
                self.assertEqual(result["exit_reason"], "invalid_drift_path")

    def test_requirement_quote_mismatch_blocks(self):
        drift = json.loads(self.drift_file().read_text(encoding="utf-8"))
        drift["original_request"] = "a summarized replacement"
        path = Path(self.temp_dir.name) / "bad-original-request.json"
        path.write_text(json.dumps(drift), encoding="utf-8")
        result = self.begin(expected=3, drift_file=path)
        self.assertEqual(result["exit_reason"], "requirement_source_mismatch")

    def test_unapproved_scope_expansion_blocks(self):
        (self.root / "new-scope.txt").write_text("new\n", encoding="utf-8")
        result = self.begin(expected=3)
        self.assertEqual(result["exit_reason"], "scope_expanded_without_authorization")

    def test_direct_dependency_scope_extension_requires_ready_for_fix(self):
        paths_file = Path(self.temp_dir.name) / "paths.json"
        paths_file.write_text(json.dumps(["dependency.txt"]), encoding="utf-8")
        result = self.invoke(
            "extend-scope",
            "--state",
            str(self.state),
            "--paths-file",
            str(paths_file),
            "--reason",
            "direct_dependency",
            expected=2,
        )
        self.assertIn("expected ready_for_fix", result["error"])

    def test_user_authorized_scope_extension_allows_a_future_new_file(self):
        paths_file = Path(self.temp_dir.name) / "new-paths.json"
        paths_file.write_text(json.dumps(["tests/new-regression.txt"]), encoding="utf-8")
        result = self.invoke(
            "extend-scope",
            "--state",
            str(self.state),
            "--paths-file",
            str(paths_file),
            "--reason",
            "user_authorized",
        )
        self.assertIn("tests/new-regression.txt", result["approved_scope_files"])
        (self.root / "tests").mkdir()
        (self.root / "tests/new-regression.txt").write_text("regression\n", encoding="utf-8")
        started = self.begin()
        self.assertEqual(started["phase"], "reviewing")

    def test_user_authorized_scope_requires_frozen_baseline_entry(self):
        paths_file = Path(self.temp_dir.name) / "unauthorized-paths.json"
        paths_file.write_text(json.dumps(["tests/not-authorized.txt"]), encoding="utf-8")
        result = self.invoke(
            "extend-scope",
            "--state",
            str(self.state),
            "--paths-file",
            str(paths_file),
            "--reason",
            "user_authorized",
            expected=2,
        )
        self.assertIn("requirements baseline", result["error"])

    def test_direct_dependency_cannot_be_authorized_after_it_changed(self):
        self.begin()
        self.record_review([self.finding("dependency-fix")])
        (self.root / "dependency.txt").write_text("already changed\n", encoding="utf-8")
        paths_file = Path(self.temp_dir.name) / "late-paths.json"
        paths_file.write_text(json.dumps(["dependency.txt"]), encoding="utf-8")
        result = self.invoke(
            "extend-scope",
            "--state",
            str(self.state),
            "--paths-file",
            str(paths_file),
            "--reason",
            "direct_dependency",
            expected=2,
        )
        self.assertIn("before it is modified", result["error"])

    def test_direct_dependency_can_be_authorized_after_review_before_fix(self):
        self.begin()
        self.record_review([self.finding("dependency-fix")])
        paths_file = Path(self.temp_dir.name) / "review-dependency-paths.json"
        paths_file.write_text(json.dumps(["dependency.txt"]), encoding="utf-8")
        extended = self.invoke(
            "extend-scope",
            "--state",
            str(self.state),
            "--paths-file",
            str(paths_file),
            "--reason",
            "direct_dependency",
        )
        self.assertEqual(extended["phase"], "ready_for_fix")
        (self.root / "dependency.txt").write_text("fixed dependency\n", encoding="utf-8")
        result = self.record_fix("dependency-fix")
        self.assertEqual(result["phase"], "ready_for_review")

    def test_unresolved_decision_cannot_complete(self):
        self.begin()
        result = self.record_review(
            [self.finding("decision-needed", status="needs_decision")],
            expected=3,
        )
        self.assertEqual(result["exit_reason"], "unresolved_review_items")

    def test_workspace_change_during_review_cannot_converge(self):
        self.begin()
        (self.root / "tracked.txt").write_text("changed after review started\n", encoding="utf-8")
        result = self.record_review([], expected=3)
        self.assertEqual(result["exit_reason"], "review_workspace_changed_before_reviewer_launch")
        self.assertNotIn("latest_review_result", result)

    def test_head_change_during_review_cannot_converge(self):
        self.begin()

        result = self.record_review([], move_head=True, expected=3)

        self.assertEqual(result["exit_reason"], "baseline_head_changed")
        self.assertEqual(result["full_review_round"], 0)

    def test_scope_change_during_review_cannot_converge(self):
        self.begin()
        (self.root / "unexpected.txt").write_text("appeared during review\n", encoding="utf-8")
        result = self.record_review([], expected=3)
        self.assertEqual(result["exit_reason"], "scope_expanded_before_reviewer_launch")
        self.assertNotIn("latest_review_result", result)

    def test_untracked_file_replaced_by_symlink_is_blocked_before_reviewer_launch(self):
        paths_file = Path(self.temp_dir.name) / "authorized-symlink-path.json"
        paths_file.write_text(json.dumps(["tests/new-regression.txt"]), encoding="utf-8")
        self.invoke(
            "extend-scope",
            "--state",
            str(self.state),
            "--paths-file",
            str(paths_file),
            "--reason",
            "user_authorized",
        )
        candidate = self.root / "tests" / "new-regression.txt"
        candidate.parent.mkdir()
        candidate.write_text("safe fixture\n", encoding="utf-8")
        self.begin()
        candidate.unlink()
        candidate.symlink_to(self.root / "tracked.txt")

        result = self.record_review([], expected=3)

        self.assertEqual(
            result["exit_reason"],
            "unsafe_untracked_file_before_reviewer_launch",
        )
        self.assertNotIn("latest_review_result", result)

    def test_fourth_review_is_a_hard_final_gate(self):
        for round_number in range(1, 4):
            started = self.begin()
            self.assertEqual(started["active_review_round"], round_number)
            self.record_review([self.finding(f"finding-{round_number}")])
            self.record_fix(round_number)

        started = self.begin()
        self.assertTrue(started["is_final_gate"])
        result = self.record_review([self.finding("still-broken")], expected=3)
        self.assertEqual(result["exit_reason"], "max_full_review_rounds_reached")

    def test_incomplete_fourth_review_can_retry_without_consuming_round(self):
        for round_number in range(1, 4):
            self.begin()
            self.record_review([self.finding(f"finding-{round_number}")])
            self.record_fix(round_number)
        started = self.begin()
        self.assertEqual(started["active_review_round"], 4)
        incomplete = self.record_review([], complete="false", expected=4)
        self.assertEqual(incomplete["full_review_round"], 3)
        retry = self.begin()
        self.assertEqual(retry["active_review_round"], 4)
        result = self.record_review([])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["full_review_round"], 4)

    def test_end_to_end_review_fix_review_converges_with_evidence(self):
        first = self.begin()
        self.assertEqual(first["active_review_round"], 1)
        reviewed = self.record_review([self.finding("root-cause")])
        self.assertEqual(reviewed["phase"], "ready_for_fix")
        fixed = self.record_fix("root-cause")
        self.assertEqual(fixed["phase"], "ready_for_review")
        second = self.begin()
        self.assertEqual(second["active_review_round"], 2)
        completed = self.record_review([])
        self.assertEqual(completed["status"], "complete")
        self.assertEqual(completed["full_review_round"], 2)
        self.assertEqual(completed["total_fix_rounds"], 1)
        self.assertEqual(
            [event["event"] for event in completed["history"]],
            [
                "initialized",
                "review_started",
                "review_recorded",
                "fix_recorded",
                "review_started",
                "review_recorded",
            ],
        )

    def test_repeated_fingerprint_blocks_on_third_consecutive_review(self):
        self.begin()
        self.record_review([self.finding("same-root-cause")])
        self.record_fix("first")
        self.begin()
        second = self.record_review(
            [self.finding("same-root-cause"), self.finding("new-finding")],
        )
        self.assertEqual(second["phase"], "ready_for_fix")
        self.record_fix("second")
        self.begin()
        result = self.record_review([self.finding("same-root-cause")], expected=3)
        self.assertEqual(result["exit_reason"], "repeated_finding_without_progress")
        self.assertEqual(
            result["repeated_finding_fingerprints"],
            [self.canonical_fingerprint("same-root-cause")],
        )

    def test_review_findings_are_derived_from_reviewer_execution(self):
        self.begin()
        result = self.record_review([self.finding("native-finding")])
        self.assertEqual(result["phase"], "ready_for_fix")
        self.assertEqual(
            result["pending_actionable_fingerprints"],
            [self.canonical_fingerprint("native-finding")],
        )
        self.assertEqual(result["latest_review_result"]["reviewer_exit_code"], 0)
        self.assertGreater(result["latest_review_result"]["raw_output_bytes"], 0)

    def test_incomplete_review_checks_workspace_before_retry(self):
        self.begin()
        result = self.record_review(
            [],
            complete="false",
            mutate=self.root / "tracked.txt",
            expected=3,
        )
        self.assertEqual(result["exit_reason"], "review_workspace_changed")

    def test_failed_reviewer_with_empty_output_counts_as_incomplete(self):
        self.begin()
        result = self.record_review([], complete="false", expected=4)
        self.assertEqual(result["phase"], "ready_for_review")
        self.assertEqual(result["review_failure_count"], 1)

    def test_semantically_invalid_reviewer_output_counts_as_incomplete(self):
        self.begin()
        duplicate = self.finding("duplicate")
        result = self.record_review([duplicate, duplicate], expected=4)
        self.assertEqual(result["phase"], "ready_for_review")
        self.assertEqual(result["review_failure_count"], 1)
        self.assertIn("duplicate", result["latest_review_result"]["execution_error"])

    def test_legacy_version_two_state_is_migrated_before_next_review(self):
        legacy = json.loads(self.state.read_text(encoding="utf-8"))
        legacy["version"] = 2
        legacy.pop("active_review_round")
        legacy.pop("review_attempt_count")
        self.state.write_text(json.dumps(legacy), encoding="utf-8")
        started = self.begin()
        self.assertEqual(started["version"], 4)
        self.assertEqual(started["active_review_round"], 1)
        self.assertEqual(started["review_attempt_count"], 1)

    def test_legacy_failed_review_restores_unconsumed_round(self):
        legacy = json.loads(self.state.read_text(encoding="utf-8"))
        legacy.update(
            {
                "version": 2,
                "max_full_review_rounds": 1,
                "full_review_round": 1,
                "review_failure_count": 1,
                "phase": "ready_for_review",
            }
        )
        legacy.pop("active_review_round")
        legacy.pop("review_attempt_count")
        self.state.write_text(json.dumps(legacy), encoding="utf-8")
        started = self.begin()
        self.assertEqual(started["active_review_round"], 1)
        self.assertEqual(started["full_review_round"], 0)

    def test_legacy_state_round_limit_is_tightened_to_four(self):
        legacy = json.loads(self.state.read_text(encoding="utf-8"))
        legacy["version"] = 3
        legacy["max_full_review_rounds"] = 10
        legacy.pop("outer_execution_policy")
        self.state.write_text(json.dumps(legacy), encoding="utf-8")
        result = self.invoke("status", "--state", str(self.state))
        self.assertEqual(result["version"], 4)
        self.assertEqual(result["max_full_review_rounds"], 4)
        self.assertFalse(
            result["outer_execution_policy"]["propagate_to_child_processes_or_subskills"]
        )

    def test_legacy_requirements_without_scope_field_fail_closed(self):
        legacy = json.loads(self.state.read_text(encoding="utf-8"))
        legacy["version"] = 2
        legacy["requirements"].pop("authorized_scope_extensions")
        legacy.pop("active_review_round")
        legacy.pop("review_attempt_count")
        self.state.write_text(json.dumps(legacy), encoding="utf-8")
        paths_file = Path(self.temp_dir.name) / "legacy-scope.json"
        paths_file.write_text(json.dumps(["tests/new-regression.txt"]), encoding="utf-8")
        result = self.invoke(
            "extend-scope",
            "--state",
            str(self.state),
            "--paths-file",
            str(paths_file),
            "--reason",
            "user_authorized",
            expected=2,
        )
        self.assertIn("requirements baseline", result["error"])

    def test_fix_result_with_unresolved_items_blocks(self):
        self.begin()
        self.record_review([self.finding("finding-a")])
        result = self.record_fix("blocked", unresolved_count=1, expected=3)
        self.assertEqual(result["exit_reason"], "code_review_fix_loop_unresolved")

    def test_fix_result_with_blocked_validation_blocks(self):
        self.begin()
        self.record_review([self.finding("finding-a")])
        result = self.record_fix("blocked-validation", validation_status="blocked", expected=3)
        self.assertEqual(result["exit_reason"], "code_review_fix_loop_validation_blocked")

    def test_fix_result_rejects_booleans_for_integer_fields(self):
        self.begin()
        self.record_review([self.finding("finding-a")])
        result = self.record_fix("boolean-count", unresolved_count=False, expected=2)
        self.assertIn("unresolved_count must be int", result["error"])


if __name__ == "__main__":
    unittest.main()
