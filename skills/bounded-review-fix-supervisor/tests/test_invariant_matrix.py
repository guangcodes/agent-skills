import importlib.util
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "supervisor-state.py"
SPEC = importlib.util.spec_from_file_location("supervisor_state_matrix", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
SUPERVISOR_STATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SUPERVISOR_STATE)


class SensitivePathMatrixTests(unittest.TestCase):
    def test_path_semantics_matrix(self) -> None:
        cases = {
            ".env/config.yaml": True,
            "backup/.env.production/config.yaml": True,
            ".npmrc.local": True,
            ".netrc.backup": True,
            "auth.json.old": True,
            "home/.config/gh/hosts.yml": True,
            "backup/.docker/config.json": True,
            "backup/.docker/config.json.backup": True,
            "home/.kube/config.backup": True,
            "tokens.json": True,
            "access_tokens.json": True,
            "accessToken.json": True,
            "session_tokens.txt": True,
            "refresh_token_backup.json": True,
            "client_secret.txt": True,
            "clientSecret.txt": True,
            "APIKey.json": True,
            "APIKeys.json": True,
            "apikeys.json": True,
            "access_key.json": True,
            "access_keys.json": True,
            "accessKey.json": True,
            "private_keys.json": True,
            "password_backup.json": True,
            "secret_old.txt": True,
            "server.pem.bak": True,
            "id_rsa.old": True,
            "credentials/prod.json": True,
            "src/tokenizer.py": False,
            "src/token_bucket.py": False,
            "src/secret_parser.py": False,
            "docs/cookie_policy.md": False,
        }

        for relative, sensitive in cases.items():
            with self.subTest(relative=relative):
                reason = SUPERVISOR_STATE.sensitive_path_reason(relative)
                self.assertEqual(reason is not None, sensitive)


class UntrackedFileTypeMatrixTests(unittest.TestCase):
    def test_only_regular_files_are_reviewable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            regular = root / "regular.txt"
            regular.write_text("safe\n", encoding="utf-8")
            directory = root / "directory"
            directory.mkdir()
            symlink = root / "symlink"
            symlink.symlink_to(regular)
            fifo = root / "fifo"
            os.mkfifo(fifo)
            cases = {
                "regular.txt": False,
                "directory": True,
                "symlink": True,
                "fifo": True,
            }
            for relative, rejected in cases.items():
                with self.subTest(relative=relative):
                    reason = SUPERVISOR_STATE.untracked_file_reason(root, relative)
                    self.assertEqual(reason is not None, rejected)

    def test_file_mode_matrix_rejects_all_special_types(self) -> None:
        cases = {
            stat.S_IFREG | 0o600: False,
            stat.S_IFDIR | 0o700: True,
            stat.S_IFLNK | 0o777: True,
            stat.S_IFIFO | 0o600: True,
            stat.S_IFSOCK | 0o600: True,
            stat.S_IFCHR | 0o600: True,
            stat.S_IFBLK | 0o600: True,
        }

        for mode, rejected in cases.items():
            with self.subTest(mode=mode):
                reason = SUPERVISOR_STATE.untracked_mode_reason(mode)
                self.assertEqual(reason is not None, rejected)


class ScreenedDiffMatrixTests(unittest.TestCase):
    def test_diff_read_is_bounded_to_literal_screened_paths(self) -> None:
        paths = ["src/ordinary.py", ":config"]
        with patch.object(
            SUPERVISOR_STATE,
            "run_git",
            return_value=b"screened diff",
        ) as run_git:
            result = SUPERVISOR_STATE.read_screened_diff(
                Path("/repo"),
                ("diff", "--cached", "--no-renames", "--binary", "HEAD"),
                paths,
            )

        self.assertEqual(result, b"screened diff")
        run_git.assert_called_once_with(
            Path("/repo"),
            "diff",
            "--cached",
            "--no-renames",
            "--binary",
            "HEAD",
            "--",
            ":(literal)src/ordinary.py",
            ":(literal):config",
        )

    def test_empty_screened_path_set_never_reads_a_live_diff(self) -> None:
        with patch.object(SUPERVISOR_STATE, "run_git") as run_git:
            result = SUPERVISOR_STATE.read_screened_diff(
                Path("/repo"),
                ("diff", "--no-renames", "--binary"),
                [],
            )

        self.assertEqual(result, b"")
        run_git.assert_not_called()


class GitlinkSnapshotMatrixTests(unittest.TestCase):
    def test_changed_gitlinks_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "changed gitlinks"):
            SUPERVISOR_STATE.reject_changed_gitlinks(
                ["src/app.py", "vendor/module"],
                ["vendor/module"],
            )

    def test_unchanged_gitlinks_do_not_block_other_changes(self) -> None:
        SUPERVISOR_STATE.reject_changed_gitlinks(
            ["src/app.py"],
            ["vendor/module"],
        )


class NativeOutputGrammarMatrixTests(unittest.TestCase):
    def test_explicit_coverage_grammar_does_not_match_product_impact(self) -> None:
        cases = {
            "I could not inspect the untracked files.": True,
            "I was only able to review tracked files.": True,
            "We were only able to inspect src/a.py.": True,
            "Coverage did not include untracked files.": True,
            "Coverage excluded generated changes.": True,
            "Untracked files were not reviewed.": True,
            "Generated files were not inspected.": True,
            "未跟踪文件未审查。": True,
            "Review was limited to src/a.py.": True,
            "The rest of the diff was not reviewed.": True,
            "本次审查未完成。": True,
            "部分文件未审查。": True,
            "Users cannot review the report.": False,
            "The parser cannot read empty files.": False,
            "用户无法审查报告。": False,
        }

        for text, incomplete in cases.items():
            with self.subTest(text=text):
                self.assertEqual(
                    SUPERVISOR_STATE.native_coverage_caveat(text),
                    incomplete,
                )

    def test_clean_grammar_requires_an_explicit_complete_scope(self) -> None:
        accepted = (
            "I did not find any actionable issues in the complete current diff.",
            "No material issues were found across the entire review scope.",
            "The only change adds a helper, with no evident correctness or compatibility issues.",
            "未发现由 allowlisted 变更引入的离散、可操作缺陷。按要求仅进行了只读静态检查。",
        )
        rejected = (
            "I did not find any actionable issues.",
            "src/a.py is valid, with no evident issues.",
            "No evident issues.",
            "未发现需要修复的问题。",
        )

        for text in accepted:
            with self.subTest(text=text):
                self.assertEqual(SUPERVISOR_STATE.parse_review_output(text.encode()), [])
        for text in rejected:
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, "unrecognized native review output"):
                    SUPERVISOR_STATE.parse_review_output(text.encode())


if __name__ == "__main__":
    unittest.main()
