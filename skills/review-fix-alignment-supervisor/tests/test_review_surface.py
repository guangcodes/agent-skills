import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "screen-review-surface.py"
SPEC = importlib.util.spec_from_file_location("alignment_review_surface", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
SURFACE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SURFACE)


class ReviewSurfaceTest(unittest.TestCase):
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
        (self.root / "source.py").write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "source.py"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-qm", "base"],
            check=True,
        )
        self.baseline_tree = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD^{tree}"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_accepts_ordinary_changed_source(self) -> None:
        (self.root / "source.py").write_text("after\n", encoding="utf-8")

        result = SURFACE.screen_review_surface(self.root, self.baseline_tree)

        self.assertEqual(result["screening_status"], "safe")
        self.assertEqual(result["changed_paths"], ["source.py"])

    def test_blocks_sensitive_tracked_and_untracked_paths(self) -> None:
        tracked_secret = self.root / ".env.local"
        tracked_secret.write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", ".env.local"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-qm", "tracked secret fixture"],
            check=True,
        )
        tracked_baseline = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD^{tree}"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        tracked_secret.write_text("after\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "sensitive changed path"):
            SURFACE.screen_review_surface(self.root, tracked_baseline)
        tracked_secret.write_text("before\n", encoding="utf-8")

        for relative in (
            "client-secret.pem",
            "cookies.json",
            "api_keys.json",
            "apikeys.json",
            "access_key.json",
            "access_keys.json",
            "accessKey.json",
            "private_keys.json",
            "server.pem.bak",
            "id_rsa.old",
        ):
            with self.subTest(relative=relative):
                candidate = self.root / relative
                candidate.write_text("placeholder\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "sensitive changed path"):
                    SURFACE.screen_review_surface(self.root, tracked_baseline)
                candidate.unlink()

    def test_blocks_special_untracked_files(self) -> None:
        (self.root / "ordinary-link").symlink_to(self.root / "source.py")

        with self.assertRaisesRegex(ValueError, "unsafe untracked file type"):
            SURFACE.screen_review_surface(self.root, self.baseline_tree)

    def test_blocks_changed_tracked_symlink_before_reading_it(self) -> None:
        outside = Path(self.temp_dir.name) / "outside-credential.txt"
        outside.write_text("secret\n", encoding="utf-8")
        tracked = self.root / "source.py"
        tracked.unlink()
        tracked.symlink_to(outside)

        with self.assertRaisesRegex(ValueError, "unsafe tracked worktree file type"):
            SURFACE.screen_review_surface(self.root, self.baseline_tree)

    def test_includes_committed_paths_and_binds_the_snapshot_hash(self) -> None:
        (self.root / "source.py").write_text("committed\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "source.py"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-qm", "feature"],
            check=True,
        )

        result = SURFACE.screen_review_surface(self.root, self.baseline_tree)

        self.assertEqual(result["committed_paths"], ["source.py"])
        self.assertEqual(result["changed_paths"], ["source.py"])
        self.assertEqual(
            result["snapshot_contract"],
            "git-cumulative-diff-sha256-v1",
        )
        self.assertEqual(len(result["diff_hash"]), 64)


if __name__ == "__main__":
    unittest.main()
