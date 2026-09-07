import importlib.util
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).parents[2]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHILD_SNAPSHOT = load_module(
    "code_review_workspace_snapshot",
    ROOT / "skills/code-review-fix-loop/scripts/workspace-snapshot.py",
)
SUPERVISOR_STATE = load_module(
    "bounded_review_supervisor_state",
    ROOT / "skills/bounded-review-fix-supervisor/scripts/supervisor-state.py",
)
ALIGNMENT_SNAPSHOT = load_module(
    "alignment_review_surface",
    ROOT
    / "skills/review-fix-alignment-supervisor/scripts/screen-review-surface.py",
)


class SnapshotCompatibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "repo"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.name", "Test"],
            check=True,
        )
        (self.root / "committed.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-qm", "base"],
            check=True,
        )
        self.baseline_tree = self.git("rev-parse", "HEAD^{tree}").strip()
        (self.root / "committed.txt").write_text("committed\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-qm", "feature"],
            check=True,
        )
        (self.root / "staged.txt").write_text("staged\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.root), "add", "staged.txt"],
            check=True,
        )
        (self.root / "committed.txt").write_text("unstaged\n", encoding="utf-8")
        (self.root / "untracked.txt").write_text("untracked\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            text=True,
            capture_output=True,
            check=True,
        ).stdout

    def test_all_snapshot_implementations_match_for_the_complete_diff(self) -> None:
        child = CHILD_SNAPSHOT.compute_workspace_snapshot(
            self.root,
            self.baseline_tree,
        )
        parent_paths = SUPERVISOR_STATE.collect_workspace_paths(
            self.root,
            self.baseline_tree,
        )
        parent = SUPERVISOR_STATE.compute_workspace_hash(self.root, parent_paths)
        alignment = ALIGNMENT_SNAPSHOT.screen_review_surface(
            self.root,
            self.baseline_tree,
        )

        self.assertEqual(child, parent)
        self.assertEqual(
            child,
            {
                key: value
                for key, value in alignment.items()
                if key != "screening_status"
            },
        )
        self.assertEqual(
            child["changed_paths"],
            ["committed.txt", "staged.txt", "untracked.txt"],
        )
        self.assertEqual(child["committed_paths"], ["committed.txt"])

    def test_child_snapshot_rejects_unborn_head_with_clear_error(self) -> None:
        unborn = Path(self.temp_dir.name) / "unborn"
        subprocess.run(["git", "init", "-q", str(unborn)], check=True)

        with self.assertRaisesRegex(ValueError, "initial commit"):
            CHILD_SNAPSHOT.compute_workspace_snapshot(unborn, "HEAD")

    def test_all_snapshot_implementations_reject_changed_gitlinks(self) -> None:
        for implementation in (
            CHILD_SNAPSHOT,
            SUPERVISOR_STATE,
            ALIGNMENT_SNAPSHOT,
        ):
            with self.subTest(implementation=implementation.__name__):
                with self.assertRaisesRegex(ValueError, "changed gitlinks"):
                    implementation.reject_changed_gitlinks(
                        ["src/app.py", "vendor/module"],
                        ["vendor/module"],
                    )

    def test_all_snapshot_implementations_parse_tree_and_index_gitlinks(self) -> None:
        tree_entry = (
            b"160000 commit 0123456789012345678901234567890123456789\tvendor/old\0"
        )
        index_entry = (
            b"160000 0123456789012345678901234567890123456789 0\tvendor/current\0"
        )
        for implementation in (
            CHILD_SNAPSHOT,
            SUPERVISOR_STATE,
            ALIGNMENT_SNAPSHOT,
        ):
            with self.subTest(implementation=implementation.__name__):
                self.assertEqual(
                    implementation.parse_gitlink_paths(tree_entry),
                    ["vendor/old"],
                )
                self.assertEqual(
                    implementation.parse_gitlink_paths(index_entry),
                    ["vendor/current"],
                )

    def test_all_git_helpers_disable_lazy_fetch_and_prompts(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["git"],
            returncode=0,
            stdout=b"",
            stderr=b"",
        )
        for implementation in (
            CHILD_SNAPSHOT,
            SUPERVISOR_STATE,
            ALIGNMENT_SNAPSHOT,
        ):
            with self.subTest(implementation=implementation.__name__):
                with mock.patch.object(
                    implementation.subprocess,
                    "run",
                    return_value=completed,
                ) as run:
                    implementation.run_git(self.root, "status")

                call = run.call_args
                self.assertEqual(call.kwargs["env"]["GIT_NO_LAZY_FETCH"], "1")
                self.assertEqual(call.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
                self.assertIs(call.kwargs["stdin"], subprocess.DEVNULL)

    def test_all_snapshot_implementations_reject_same_path_content_drift(self) -> None:
        implementations = (
            (
                CHILD_SNAPSHOT,
                lambda: CHILD_SNAPSHOT.compute_workspace_snapshot(
                    self.root,
                    self.baseline_tree,
                ),
            ),
            (
                SUPERVISOR_STATE,
                lambda: SUPERVISOR_STATE.compute_workspace_hash(
                    self.root,
                    SUPERVISOR_STATE.collect_workspace_paths(
                        self.root,
                        self.baseline_tree,
                    ),
                ),
            ),
            (
                ALIGNMENT_SNAPSHOT,
                lambda: ALIGNMENT_SNAPSHOT.screen_review_surface(
                    self.root,
                    self.baseline_tree,
                ),
            ),
        )
        for implementation, capture in implementations:
            with self.subTest(implementation=implementation.__name__):
                changed = self.root / "committed.txt"
                changed.write_text("unstaged\n", encoding="utf-8")
                original_read = implementation.read_screened_diff
                calls = 0

                def mutate_after_first_digest(*args, **kwargs):
                    nonlocal calls
                    result = original_read(*args, **kwargs)
                    calls += 1
                    if calls == 3:
                        changed.write_text("same path, new contents\n", encoding="utf-8")
                    return result

                with mock.patch.object(
                    implementation,
                    "read_screened_diff",
                    side_effect=mutate_after_first_digest,
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "workspace contents changed",
                    ):
                        capture()

    def test_diff_config_cannot_hide_a_dirty_submodule(self) -> None:
        submodule_source = Path(self.temp_dir.name) / "submodule-source"
        submodule_source.mkdir()
        subprocess.run(["git", "init", "-q", str(submodule_source)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(submodule_source),
                "config",
                "user.email",
                "test@example.com",
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(submodule_source), "config", "user.name", "Test"],
            check=True,
        )
        (submodule_source / "nested.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(submodule_source), "add", "nested.txt"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(submodule_source), "commit", "-qm", "base"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                "-q",
                str(submodule_source),
                "vendor/module",
            ],
            check=True,
        )
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-qm", "add submodule"],
            check=True,
        )
        baseline = self.git("rev-parse", "HEAD^{tree}").strip()
        self.git("config", "diff.ignoreSubmodules", "all")
        self.git("config", "submodule.vendor/module.ignore", "all")
        (self.root / "vendor/module/nested.txt").write_text(
            "dirty\n",
            encoding="utf-8",
        )

        for implementation in (
            CHILD_SNAPSHOT,
            SUPERVISOR_STATE,
            ALIGNMENT_SNAPSHOT,
        ):
            with self.subTest(implementation=implementation.__name__):
                paths = implementation.collect_workspace_paths(self.root, baseline)
                self.assertIn("vendor/module", paths["changed_paths"])


if __name__ == "__main__":
    unittest.main()
