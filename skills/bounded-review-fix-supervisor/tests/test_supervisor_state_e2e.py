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

if os.environ.get("GIT_NO_LAZY_FETCH") != "1":
    raise SystemExit(18)
if os.environ.get("GIT_TERMINAL_PROMPT") != "0":
    raise SystemExit(19)
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

    def init_state(self, state, *, root=None, baseline=None, max_windows="4", expected=0):
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
            "--max-windows",
            max_windows,
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

    def begin_fix_window(self, *, expected=0, drift_file=None):
        return self.invoke(
            "begin-fix-window",
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

    def write_fix_result(
        self,
        *,
        checkpoint_findings=None,
        checkpoint_snapshot=None,
        review_baseline=None,
        **overrides,
    ):
        if checkpoint_findings is None:
            checkpoint_findings = []
        snapshot = checkpoint_snapshot or self.invoke(
            "hash-diff",
            "--state",
            str(self.state),
        )
        current = self.invoke("status", "--state", str(self.state))
        active_directive = current["active_fix_directive"]
        result = {
            "status": "complete",
            "exit_reason": "converged",
            "requirements_snapshot": current["requirements"],
            "requirement_digest": current["requirement_digest"],
            "fix_rounds": 1,
            "unresolved_count": 0,
            "coverage_status": "covered",
            "review_checkpoint": {
                "source": "code-review-fix-loop",
                "snapshot_contract": "git-cumulative-diff-sha256-v1",
                "review_complete": True,
                "coverage_status": "covered",
                "review_baseline": review_baseline or current["baseline_tree"],
                "reviewed_diff_hash": snapshot["diff_hash"],
                "changed_paths": snapshot["changed_paths"],
                "findings": checkpoint_findings,
            },
            "validation_status": "focused",
            "test_mode": "light",
            "test_mode_reason": "default",
            "exit_test_count": 1,
            "exit_test_status": "passed",
            "exit_test_skip_reason": None,
            "alignment_directive": active_directive,
            "alignment_directive_status": (
                "resolved" if active_directive is not None else "not_applicable"
            ),
            "alignment_resolution_evidence": (
                "The child verified the correction against the final cumulative diff."
                if active_directive is not None
                else None
            ),
        }
        result.update(overrides)
        result_file = Path(self.temp_dir.name) / "fix-result.json"
        result_file.write_text(json.dumps(result), encoding="utf-8")
        return result_file

    def record_fix(self, marker, *, expected=0, begin_window=True, change=True, **overrides):
        if begin_window:
            self.begin_fix_window()
        if change:
            (self.root / "tracked.txt").write_text(f"fixed {marker}\n", encoding="utf-8")
        result_file = self.write_fix_result(**overrides)
        return self.invoke(
            "record-fix",
            "--state",
            str(self.state),
            "--skill-result-file",
            str(result_file),
            expected=expected,
        )

    def adopt_fix_result(self, *, expected=0, **overrides):
        result_file = self.write_fix_result(**overrides)
        return self.invoke(
            "adopt-fix-result",
            "--state",
            str(self.state),
            "--skill-result-file",
            str(result_file),
            expected=expected,
        )

    def record_alignment(self, outcome="continue", *, expected=0, **overrides):
        snapshot = self.invoke("hash-diff", "--state", str(self.state))
        current = self.invoke("status", "--state", str(self.state))
        review_checkpoint_digest = hashlib.sha256(
            json.dumps(
                current["latest_review_result"],
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        results = {
            "continue": {
                "requirement_alignment": "aligned",
                "implementation_completeness": "fixable_gap",
                "design_convergence": "convergent",
                "minimality": "minimum_sufficient",
                "alignment_findings": [],
                "correction_directive": None,
                "developer_question": None,
                "blocker": None,
            },
            "redirect": {
                "requirement_alignment": "aligned",
                "implementation_completeness": "fixable_gap",
                "design_convergence": "needs_redirect",
                "minimality": "excess_or_redundant",
                "alignment_findings": [
                    {
                        "kind": "redundancy",
                        "summary": "duplicate state transition",
                        "evidence": "the current diff contains two equivalent paths",
                        "related_paths": ["tracked.txt"],
                    }
                ],
                "correction_directive": "Keep one state transition and remove the duplicate.",
                "developer_question": None,
                "blocker": None,
            },
            "ask_developer": {
                "requirement_alignment": "uncertain",
                "implementation_completeness": "decision_gap",
                "design_convergence": "decision_required",
                "minimality": "uncertain",
                "alignment_findings": [
                    {
                        "kind": "missing_information",
                        "summary": "public behavior is ambiguous",
                        "evidence": "two user-visible outcomes satisfy the existing request",
                        "related_paths": ["tracked.txt"],
                    }
                ],
                "correction_directive": None,
                "developer_question": "Which user-visible behavior should be preserved?",
                "blocker": None,
            },
            "blocked": {
                "requirement_alignment": "uncertain",
                "implementation_completeness": "blocked",
                "design_convergence": "blocked",
                "minimality": "uncertain",
                "alignment_findings": [],
                "correction_directive": None,
                "developer_question": None,
                "blocker": "the review checkpoint cannot be verified",
            },
        }
        result = {
            "source": "review-fix-alignment-supervisor",
            "outcome": outcome,
            "snapshot_contract": snapshot["snapshot_contract"],
            "review_baseline": snapshot["baseline_tree"],
            "assessed_head": snapshot["head"],
            "assessed_diff_hash": snapshot["diff_hash"],
            "changed_paths": snapshot["changed_paths"],
            "requirement_digest": current["requirement_digest"],
            "review_checkpoint_digest": review_checkpoint_digest,
            **results[outcome],
        }
        result.update(overrides)
        result_file = Path(self.temp_dir.name) / "alignment-result.json"
        result_file.write_text(json.dumps(result), encoding="utf-8")
        return self.invoke(
            "record-alignment",
            "--state",
            str(self.state),
            "--alignment-result-file",
            str(result_file),
            expected=expected,
        )

    def test_zero_findings_completes(self):
        self.begin()
        result = self.record_review([])
        self.assertEqual(result["status"], "complete")

    def test_default_requirements_omit_the_child_test_mode(self):
        current = self.invoke("status", "--state", str(self.state))
        self.assertNotIn("test_mode", current["requirements"])
        self.assertNotIn("test_mode_reason", current["requirements"])

    def test_init_preserves_explicit_user_test_mode_for_passthrough(self):
        requirements = json.loads(self.requirements.read_text(encoding="utf-8"))
        requirements["test_mode"] = "deep"
        requirements["test_mode_reason"] = "user:deep"
        self.requirements.write_text(json.dumps(requirements), encoding="utf-8")
        state = Path(self.temp_dir.name) / "explicit-user-mode.json"

        initialized = self.init_state(state)

        self.assertEqual(initialized["requirements"]["test_mode"], "deep")
        self.assertEqual(initialized["requirements"]["test_mode_reason"], "user:deep")

    def test_init_rejects_non_user_test_mode_provenance(self):
        requirements = json.loads(self.requirements.read_text(encoding="utf-8"))
        requirements["test_mode"] = "deep"
        requirements["test_mode_reason"] = "caller override"
        self.requirements.write_text(json.dumps(requirements), encoding="utf-8")
        state = Path(self.temp_dir.name) / "invented-mode.json"

        result = self.init_state(state, expected=2)

        self.assertEqual(
            result["error"],
            "requirements test_mode_reason must equal user:<test_mode>",
        )

    def test_init_rejects_unpaired_user_test_mode(self):
        requirements = json.loads(self.requirements.read_text(encoding="utf-8"))
        requirements["test_mode"] = "light"
        self.requirements.write_text(json.dumps(requirements), encoding="utf-8")
        state = Path(self.temp_dir.name) / "unpaired-mode.json"

        result = self.init_state(state, expected=2)

        self.assertEqual(
            result["error"],
            "requirements test_mode and test_mode_reason must be provided together",
        )

    def test_fix_result_records_the_explicit_user_test_mode(self):
        requirements = json.loads(self.requirements.read_text(encoding="utf-8"))
        requirements["test_mode"] = "deep"
        requirements["test_mode_reason"] = "user:deep"
        self.requirements.write_text(json.dumps(requirements), encoding="utf-8")
        self.state.unlink()
        self.init_state(self.state)
        self.begin()
        self.record_review([self.finding("child-selected-mode")])
        result = self.record_fix(
            "child-selected-mode",
            test_mode="deep",
            test_mode_reason="user:deep",
            validation_status="complete",
        )
        self.assertEqual(
            result["latest_fix_result"]["test_mode"],
            "deep",
        )

    def test_fix_result_rejects_a_mode_the_user_did_not_select(self):
        self.begin()
        self.record_review([self.finding("unapproved-deep")])
        result = self.record_fix(
            "unapproved-deep",
            test_mode="deep",
            test_mode_reason="user:deep",
            validation_status="complete",
            expected=3,
        )
        self.assertEqual(result["exit_reason"], "code_review_fix_loop_test_mode_mismatch")

    def test_fix_result_rejects_an_invalid_child_test_mode(self):
        self.begin()
        self.record_review([self.finding("invalid-child-mode")])
        result = self.record_fix(
            "invalid-child-mode",
            test_mode="turbo",
            expected=2,
        )
        self.assertEqual(
            result["error"],
            "invalid test mode in fix result",
        )

    def test_max_fix_windows_defaults_to_four_and_cannot_exceed_four(self):
        current = self.invoke("status", "--state", str(self.state))
        self.assertEqual(current["max_fix_windows"], 4)
        state = Path(self.temp_dir.name) / "too-many.json"
        result = self.init_state(state, max_windows="5", expected=2)
        self.assertIn("between 1 and 4", result["error"])

    def test_max_fix_windows_accepts_a_user_selected_value_up_to_four(self):
        state = Path(self.temp_dir.name) / "two-rounds.json"
        result = self.init_state(state, max_windows="2")
        self.assertEqual(result["max_fix_windows"], 2)

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
        self.assertEqual(result["initial_review_count"], 0)

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

    def test_hash_diff_rejects_a_changed_tracked_symlink(self):
        outside = Path(self.temp_dir.name) / "outside-credential.txt"
        outside.write_text("secret\n", encoding="utf-8")
        tracked = self.root / "tracked.txt"
        tracked.unlink()
        tracked.symlink_to(outside)

        result = self.invoke("hash-diff", "--state", str(self.state), expected=3)

        self.assertEqual(result["exit_reason"], "unsafe_tracked_worktree_file")
        self.assertEqual(result["sensitive_path"], "tracked.txt")
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
        self.begin_fix_window()
        (self.root / "dependency.txt").write_text("fixed dependency\n", encoding="utf-8")
        result = self.record_fix("dependency-fix", begin_window=False)
        self.assertEqual(result["phase"], "stopped")
        self.assertEqual(result["status"], "complete")

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
        self.assertEqual(result["initial_review_count"], 0)

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

    def test_fix_window_limit_extends_the_child_loop_but_stops_globally(self):
        self.state.unlink()
        self.init_state(self.state, max_windows="2")
        self.begin()
        self.record_review([self.finding("root-cause")])

        first = self.record_fix(
            "window-one",
            status="incomplete",
            exit_reason="max_fix_rounds_reached",
            fix_rounds=5,
            unresolved_count=1,
            checkpoint_findings=[self.finding("root-cause")],
        )
        self.assertEqual(first["phase"], "alignment_required")
        self.assertEqual(first["fix_window_count"], 1)
        self.assertEqual(first["total_fix_rounds"], 5)

        premature = self.begin_fix_window(expected=2)
        self.assertIn("expected ready_for_fix", premature["error"])
        aligned = self.record_alignment()
        self.assertEqual(aligned["phase"], "ready_for_fix")
        self.assertEqual(aligned["alignment_check_count"], 1)

        result = self.record_fix(
            "window-two",
            status="incomplete",
            exit_reason="max_fix_rounds_reached",
            fix_rounds=5,
            unresolved_count=1,
            checkpoint_findings=[self.finding("root-cause")],
            expected=3,
        )
        self.assertEqual(result["exit_reason"], "max_fix_windows_reached")
        self.assertEqual(result["fix_window_count"], 2)
        self.assertEqual(result["total_fix_rounds"], 10)

    def test_redirect_is_forwarded_to_the_next_fix_window(self):
        self.state.unlink()
        self.init_state(self.state, max_windows="2")
        self.begin()
        self.record_review([self.finding("root-cause")])
        self.record_fix(
            "window-one",
            status="incomplete",
            exit_reason="max_fix_rounds_reached",
            fix_rounds=5,
            unresolved_count=1,
            checkpoint_findings=[self.finding("root-cause")],
        )

        redirected = self.record_alignment("redirect")
        self.assertEqual(
            redirected["pending_alignment_directive"],
            "Keep one state transition and remove the duplicate.",
        )
        started = self.begin_fix_window()
        self.assertEqual(
            started["active_fix_directive"],
            "Keep one state transition and remove the duplicate.",
        )
        self.assertEqual(
            started["history"][-1]["alignment_directive"],
            "Keep one state transition and remove the duplicate.",
        )

        completed = self.record_fix("window-two", begin_window=False)
        self.assertEqual(completed["status"], "complete")
        self.assertEqual(completed["exit_reason"], "review_fix_converged")

    def test_clean_child_cannot_discard_an_unresolved_redirect(self):
        self.state.unlink()
        self.init_state(self.state, max_windows="2")
        self.begin()
        self.record_review([self.finding("root-cause")])
        self.record_fix(
            "window-one",
            status="incomplete",
            exit_reason="max_fix_rounds_reached",
            fix_rounds=5,
            unresolved_count=1,
            checkpoint_findings=[self.finding("root-cause")],
        )
        self.record_alignment("redirect")
        self.begin_fix_window()

        result = self.record_fix(
            "window-two",
            begin_window=False,
            alignment_directive_status="unresolved",
            alignment_resolution_evidence=(
                "The final diff still contains the duplicate state transition."
            ),
            expected=3,
        )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["exit_reason"], "alignment_directive_unresolved")

    def test_child_must_echo_the_exact_redirect_directive(self):
        self.state.unlink()
        self.init_state(self.state, max_windows="2")
        self.begin()
        self.record_review([self.finding("root-cause")])
        self.record_fix(
            "window-one",
            status="incomplete",
            exit_reason="max_fix_rounds_reached",
            fix_rounds=5,
            unresolved_count=1,
            checkpoint_findings=[self.finding("root-cause")],
        )
        self.record_alignment("redirect")
        self.begin_fix_window()

        result = self.record_fix(
            "window-two",
            begin_window=False,
            alignment_directive="A different directive.",
            expected=3,
        )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(
            result["exit_reason"],
            "alignment_directive_contract_mismatch",
        )

    def test_alignment_decision_stops_before_another_window(self):
        self.begin()
        self.record_review([self.finding("root-cause")])
        self.record_fix(
            "window-one",
            status="incomplete",
            exit_reason="max_fix_rounds_reached",
            fix_rounds=5,
            unresolved_count=1,
            checkpoint_findings=[self.finding("root-cause")],
        )

        result = self.record_alignment("ask_developer", expected=3)

        self.assertEqual(result["exit_reason"], "alignment_requires_developer_decision")
        self.assertEqual(
            result["developer_question"],
            "Which user-visible behavior should be preserved?",
        )
        self.assertEqual(result["alignment_check_count"], 1)

    def test_stale_alignment_checkpoint_stops_the_campaign(self):
        self.begin()
        self.record_review([self.finding("root-cause")])
        self.record_fix(
            "window-one",
            status="incomplete",
            exit_reason="max_fix_rounds_reached",
            fix_rounds=5,
            unresolved_count=1,
            checkpoint_findings=[self.finding("root-cause")],
        )

        result = self.record_alignment(
            assessed_diff_hash="0" * 64,
            expected=3,
        )

        self.assertEqual(result["exit_reason"], "alignment_checkpoint_stale")

    def test_alignment_checkpoint_rejects_mismatched_snapshot_binding(self):
        self.begin()
        self.record_review([self.finding("root-cause")])
        self.record_fix(
            "window-one",
            status="incomplete",
            exit_reason="max_fix_rounds_reached",
            fix_rounds=5,
            unresolved_count=1,
            checkpoint_findings=[self.finding("root-cause")],
        )

        result = self.record_alignment(
            changed_paths=[],
            expected=3,
        )

        self.assertEqual(result["exit_reason"], "alignment_checkpoint_stale")
        self.assertEqual(result["mismatched_fields"], ["changed_paths"])

    def test_alignment_checkpoint_rejects_mismatched_requirements(self):
        self.begin()
        self.record_review([self.finding("root-cause")])
        self.record_fix(
            "window-one",
            status="incomplete",
            exit_reason="max_fix_rounds_reached",
            fix_rounds=5,
            unresolved_count=1,
            checkpoint_findings=[self.finding("root-cause")],
        )

        result = self.record_alignment(
            requirement_digest="0" * 64,
            expected=3,
        )

        self.assertEqual(result["exit_reason"], "alignment_checkpoint_stale")
        self.assertEqual(result["mismatched_fields"], ["requirement_digest"])

    def test_alignment_checkpoint_rejects_a_different_review_checkpoint(self):
        self.begin()
        self.record_review([self.finding("root-cause")])
        self.record_fix(
            "window-one",
            status="incomplete",
            exit_reason="max_fix_rounds_reached",
            fix_rounds=5,
            unresolved_count=1,
            checkpoint_findings=[self.finding("root-cause")],
        )

        result = self.record_alignment(
            review_checkpoint_digest="0" * 64,
            expected=3,
        )

        self.assertEqual(result["exit_reason"], "alignment_checkpoint_stale")
        self.assertEqual(result["mismatched_fields"], ["review_checkpoint_digest"])

    def test_alignment_blocker_stops_before_another_window(self):
        self.begin()
        self.record_review([self.finding("root-cause")])
        self.record_fix(
            "window-one",
            status="incomplete",
            exit_reason="max_fix_rounds_reached",
            fix_rounds=5,
            unresolved_count=1,
            checkpoint_findings=[self.finding("root-cause")],
        )

        result = self.record_alignment("blocked", expected=3)

        self.assertEqual(result["exit_reason"], "alignment_supervisor_blocked")
        self.assertEqual(
            result["alignment_blocker"],
            "the review checkpoint cannot be verified",
        )

    def test_redirect_cannot_hide_an_uncertain_requirement(self):
        self.begin()
        self.record_review([self.finding("root-cause")])
        self.record_fix(
            "window-one",
            status="incomplete",
            exit_reason="max_fix_rounds_reached",
            fix_rounds=5,
            unresolved_count=1,
            checkpoint_findings=[self.finding("root-cause")],
        )

        result = self.record_alignment(
            "redirect",
            requirement_alignment="uncertain",
            expected=2,
        )

        self.assertEqual(
            result["error"],
            "redirect alignment result has an invalid field combination",
        )
        current = self.invoke("status", "--state", str(self.state))
        self.assertEqual(current["phase"], "alignment_required")

    def test_ask_developer_cannot_hide_a_blocked_alignment_state(self):
        self.begin()
        self.record_review([self.finding("root-cause")])
        self.record_fix(
            "window-one",
            status="incomplete",
            exit_reason="max_fix_rounds_reached",
            fix_rounds=5,
            unresolved_count=1,
            checkpoint_findings=[self.finding("root-cause")],
        )

        result = self.record_alignment(
            "ask_developer",
            implementation_completeness="blocked",
            expected=2,
        )

        self.assertEqual(
            result["error"],
            "ask_developer alignment result has an invalid field combination",
        )

    def test_end_to_end_reuses_child_review_checkpoint_without_duplicate_review(self):
        first = self.begin()
        self.assertEqual(first["active_review_attempt"], 1)
        reviewed = self.record_review([self.finding("root-cause")])
        self.assertEqual(reviewed["phase"], "ready_for_fix")
        fixed = self.record_fix("root-cause")
        self.assertEqual(fixed["status"], "complete")
        self.assertEqual(fixed["initial_review_count"], 1)
        self.assertEqual(fixed["fix_window_count"], 1)
        self.assertEqual(fixed["review_checkpoint_count"], 2)
        self.assertEqual(fixed["total_fix_rounds"], 1)
        self.assertEqual(
            [event["event"] for event in fixed["history"]],
            [
                "initialized",
                "initial_review_started",
                "initial_review_recorded",
                "fix_window_started",
                "fix_window_checkpoint_accepted",
            ],
        )

    def test_clean_no_change_child_checkpoint_can_complete(self):
        self.begin()
        self.record_review([self.finding("false-positive")])
        reclassified = self.finding("false-positive", status="not_actionable")
        reclassified["fingerprint"] = self.canonical_fingerprint("false-positive")
        result = self.record_fix(
            "false-positive",
            change=False,
            fix_rounds=0,
            checkpoint_findings=[reclassified],
            exit_test_count=0,
            exit_test_status="skipped",
            exit_test_skip_reason="no_persistent_change",
            validation_status="unverified",
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["total_fix_rounds"], 0)

    def test_zero_change_checkpoint_cannot_drop_the_input_findings(self):
        self.begin()
        self.record_review([self.finding("false-positive")])

        result = self.record_fix(
            "false-positive",
            change=False,
            fix_rounds=0,
            checkpoint_findings=[],
            exit_test_count=0,
            exit_test_status="skipped",
            exit_test_skip_reason="no_persistent_change",
            validation_status="unverified",
            expected=3,
        )

        self.assertEqual(
            result["exit_reason"],
            "zero_change_checkpoint_finding_set_mismatch",
        )

    def test_missing_child_checkpoint_fails_closed_and_persists_the_block(self):
        self.begin()
        self.record_review([self.finding("missing-checkpoint")])

        result = self.record_fix(
            "missing-checkpoint",
            review_checkpoint=None,
            expected=3,
        )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["phase"], "stopped")
        self.assertEqual(result["exit_reason"], "invalid_child_review_checkpoint")
        persisted = self.invoke("status", "--state", str(self.state))
        self.assertEqual(persisted["exit_reason"], "invalid_child_review_checkpoint")

    def test_malformed_child_checkpoint_fails_closed(self):
        self.begin()
        self.record_review([self.finding("malformed-checkpoint")])

        result = self.record_fix(
            "malformed-checkpoint",
            review_checkpoint={
                "source": "code-review-fix-loop",
                "snapshot_contract": "git-cumulative-diff-sha256-v1",
            },
            expected=3,
        )

        self.assertEqual(result["exit_reason"], "invalid_child_review_checkpoint")
        self.assertIn("review_complete", result["checkpoint_error"])

    def test_persistent_change_cannot_skip_exit_validation_without_a_reason(self):
        self.begin()
        self.record_review([self.finding("missing-skip-reason")])

        result = self.record_fix(
            "missing-skip-reason",
            exit_test_count=0,
            exit_test_status="skipped",
            exit_test_skip_reason=None,
            validation_status="unverified",
            expected=2,
        )

        self.assertEqual(
            result["error"],
            "skipped exit validation requires a recognized skip reason",
        )

    def test_persistent_change_can_record_no_applicable_automated_test(self):
        self.begin()
        self.record_review([self.finding("no-applicable-test")])

        result = self.record_fix(
            "no-applicable-test",
            exit_test_count=0,
            exit_test_status="skipped",
            exit_test_skip_reason="no_applicable_automated_test",
            validation_status="unverified",
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(
            result["latest_fix_result"]["exit_test_skip_reason"],
            "no_applicable_automated_test",
        )

    def test_zero_fix_rounds_cannot_execute_exit_validation(self):
        self.begin()
        self.record_review([self.finding("no-change-test")])
        reclassified = self.finding("no-change-test", status="not_actionable")
        reclassified["fingerprint"] = self.canonical_fingerprint("no-change-test")

        result = self.record_fix(
            "no-change-test",
            change=False,
            fix_rounds=0,
            checkpoint_findings=[reclassified],
            expected=2,
        )

        self.assertEqual(result["error"], "zero fix_rounds cannot execute exit validation")

    def test_adopts_an_exhausted_child_checkpoint_without_an_initial_review(self):
        adopted = self.adopt_fix_result(
            status="incomplete",
            exit_reason="max_fix_rounds_reached",
            fix_rounds=5,
            unresolved_count=1,
            checkpoint_findings=[self.finding("adopted-root-cause")],
        )

        self.assertEqual(adopted["phase"], "alignment_required")
        self.assertEqual(adopted["initial_review_count"], 0)
        self.assertEqual(adopted["fix_window_count"], 1)
        self.assertEqual(adopted["review_checkpoint_count"], 1)
        self.assertEqual(adopted["total_fix_rounds"], 5)
        self.assertEqual(adopted["history"][-1]["event"], "fix_result_adopted")

    def test_adoption_carries_an_older_child_review_baseline(self):
        original_tree = self.git("rev-parse", "HEAD^{tree}").strip()
        subprocess.run(["git", "-C", str(self.root), "add", "tracked.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-qm", "feature"],
            check=True,
        )
        self.baseline_head = self.git("rev-parse", "HEAD").strip()
        self.state.unlink()
        self.init_state(self.state)

        snapshot_state = Path(self.temp_dir.name) / "older-baseline-snapshot.json"
        self.init_state(snapshot_state)
        snapshot_state_value = json.loads(snapshot_state.read_text(encoding="utf-8"))
        snapshot_state_value["baseline_tree"] = original_tree
        snapshot_state.write_text(json.dumps(snapshot_state_value), encoding="utf-8")
        child_snapshot = self.invoke(
            "hash-diff",
            "--state",
            str(snapshot_state),
        )

        adopted = self.adopt_fix_result(
            status="incomplete",
            exit_reason="max_fix_rounds_reached",
            fix_rounds=5,
            unresolved_count=1,
            checkpoint_findings=[self.finding("committed-root-cause")],
            checkpoint_snapshot=child_snapshot,
            review_baseline=original_tree,
        )

        self.assertEqual(adopted["baseline_tree"], original_tree)
        self.assertEqual(adopted["phase"], "alignment_required")
        self.assertIn("tracked.txt", adopted["initial_scope_files"])

    def test_adopts_a_converged_child_checkpoint_without_an_initial_review(self):
        adopted = self.adopt_fix_result()

        self.assertEqual(adopted["status"], "complete")
        self.assertEqual(adopted["initial_review_count"], 0)
        self.assertEqual(adopted["fix_window_count"], 1)

    def test_adoption_rejects_a_result_from_different_requirements(self):
        requirements = json.loads(self.requirements.read_text(encoding="utf-8"))
        requirements["accepted_amendments"] = ["different accepted requirement"]
        digest = hashlib.sha256(
            json.dumps(
                requirements,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        result = self.adopt_fix_result(
            requirements_snapshot=requirements,
            requirement_digest=digest,
            expected=3,
        )

        self.assertEqual(result["exit_reason"], "child_requirement_digest_mismatch")

    def test_fix_result_rejects_a_digest_without_its_matching_snapshot(self):
        result_file = self.write_fix_result(requirement_digest="0" * 64)

        result = self.invoke(
            "adopt-fix-result",
            "--state",
            str(self.state),
            "--skill-result-file",
            str(result_file),
            expected=2,
        )

        self.assertEqual(
            result["error"],
            "fix result requirement snapshot digest mismatch",
        )

    def test_stale_adopted_checkpoint_fails_closed(self):
        result = self.adopt_fix_result(
            review_checkpoint={
                "source": "code-review-fix-loop",
                "snapshot_contract": "git-cumulative-diff-sha256-v1",
                "review_complete": True,
                "coverage_status": "covered",
                "review_baseline": self.git("rev-parse", "HEAD^{tree}").strip(),
                "reviewed_diff_hash": "0" * 64,
                "changed_paths": ["tracked.txt"],
                "findings": [],
            },
            expected=3,
        )

        self.assertEqual(result["exit_reason"], "child_review_checkpoint_stale")

    def test_stale_child_review_checkpoint_blocks(self):
        self.begin()
        self.record_review([self.finding("stale-checkpoint")])
        result = self.record_fix(
            "stale-checkpoint",
            review_checkpoint={
                "source": "code-review-fix-loop",
                "snapshot_contract": "git-cumulative-diff-sha256-v1",
                "review_complete": True,
                "coverage_status": "covered",
                "review_baseline": self.git("rev-parse", "HEAD^{tree}").strip(),
                "reviewed_diff_hash": "0" * 64,
                "changed_paths": ["tracked.txt"],
                "findings": [],
            },
            expected=3,
        )
        self.assertEqual(result["exit_reason"], "child_review_checkpoint_stale")

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

    def test_v7_state_requires_reinitialization(self):
        legacy = json.loads(self.state.read_text(encoding="utf-8"))
        legacy["version"] = 7
        self.state.write_text(json.dumps(legacy), encoding="utf-8")

        result = self.invoke("status", "--state", str(self.state), expected=2)

        self.assertIn("reinitialize", result["error"])

    def test_current_state_with_missing_requirement_field_fails_closed(self):
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["requirements"].pop("authorized_scope_extensions")
        self.state.write_text(json.dumps(state), encoding="utf-8")

        result = self.invoke("status", "--state", str(self.state), expected=2)

        self.assertIn("authorized_scope_extensions", result["error"])

    def test_fix_result_with_inconsistent_unresolved_count_blocks(self):
        self.begin()
        self.record_review([self.finding("finding-a")])
        result = self.record_fix("blocked", unresolved_count=1, expected=3)
        self.assertEqual(result["exit_reason"], "child_unresolved_count_mismatch")

    def test_fix_result_with_blocked_validation_blocks(self):
        self.begin()
        self.record_review([self.finding("finding-a")])
        result = self.record_fix(
            "blocked-validation",
            validation_status="blocked",
            exit_test_status="blocked",
            expected=3,
        )
        self.assertEqual(result["exit_reason"], "code_review_fix_loop_validation_blocked")

    def test_fix_result_with_failed_validation_blocks(self):
        self.begin()
        self.record_review([self.finding("finding-a")])
        result = self.record_fix(
            "failed-validation",
            validation_status="failed",
            exit_test_status="failed",
            expected=3,
        )
        self.assertEqual(result["exit_reason"], "code_review_fix_loop_validation_failed")

    def test_fix_result_rejects_booleans_for_integer_fields(self):
        self.begin()
        self.record_review([self.finding("finding-a")])
        result = self.record_fix("boolean-count", unresolved_count=False, expected=2)
        self.assertIn("unresolved_count must be int", result["error"])


if __name__ == "__main__":
    unittest.main()
