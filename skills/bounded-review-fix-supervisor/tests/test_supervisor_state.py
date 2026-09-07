import importlib.util
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "supervisor-state.py"
SPEC = importlib.util.spec_from_file_location("supervisor_state", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
SUPERVISOR_STATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SUPERVISOR_STATE)


class ReviewCommandTests(unittest.TestCase):
    def test_initial_review_uses_a_static_complete_diff_prompt(self) -> None:
        self.assertTrue(
            hasattr(SUPERVISOR_STATE, "build_review_command"),
            "review command construction must be independently testable",
        )
        command = SUPERVISOR_STATE.build_review_command(
            reviewer="/usr/local/bin/codex",
            schema_path=Path("/tmp/review-schema.json"),
            final_path=Path("/tmp/review-final.json"),
        )

        self.assertEqual(
            command[:5],
            ["/usr/local/bin/codex", "exec", "--sandbox", "read-only", "review"],
        )
        self.assertNotIn("--uncommitted", command)
        self.assertEqual(command[-3:-1], ["--output-last-message", "/tmp/review-final.json"])
        prompt = command[-1]
        self.assertIn("read-only static review", prompt)
        self.assertIn("all current staged, unstaged, and untracked changes", prompt)
        self.assertIn("Do not run test, lint, typecheck, build", prompt)

    def test_sensitive_path_filter_covers_common_credential_and_cookie_names(self) -> None:
        sensitive_paths = (
            ".npmrc",
            ".netrc",
            ".pypirc",
            "cookies.json",
            "api-key.txt",
            "access_key.json",
            "accessKey.json",
            ".aws/config",
            ".ssh/config",
            ".docker/config.json",
            ".kube/config",
            ".config/gh/hosts.yml",
            ".config/gcloud/credentials.db",
            ".azure/accessTokens.json",
            "backup/.docker/config.json",
            "home/.kube/config",
            "home/.config/gh/hosts.yml",
            "home/.config/gcloud/credentials.db",
            "backup/.azure/accessTokens.json",
            ".envrc.local",
            ".environment",
            ".env/config.yaml",
            "backup/.env.production/config.yaml",
            "server.pem.bak",
            "id_rsa.old",
        )

        for relative in sensitive_paths:
            with self.subTest(relative=relative):
                self.assertIsNotNone(SUPERVISOR_STATE.sensitive_path_reason(relative))

    def test_sensitive_path_filter_does_not_block_token_processing_source_files(self) -> None:
        ordinary_paths = ("src/tokenizer.py", "src/token_bucket.py")
        credential_paths = ("token.json", "github-token.txt", "refresh_token_backup.json")

        for relative in ordinary_paths:
            with self.subTest(relative=relative):
                self.assertIsNone(SUPERVISOR_STATE.sensitive_path_reason(relative))
        for relative in credential_paths:
            with self.subTest(relative=relative):
                self.assertIsNotNone(SUPERVISOR_STATE.sensitive_path_reason(relative))

    def test_reviewer_schema_uses_the_same_severity_enum_as_the_parser(self) -> None:
        severity = (
            SUPERVISOR_STATE.review_output_schema()["properties"]["findings"]["items"]
            ["properties"]["severity"]
        )

        self.assertEqual(severity, {"type": "string", "enum": ["P0", "P1", "P2", "P3"]})


class NativeReviewOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(
            hasattr(SUPERVISOR_STATE, "parse_review_output"),
            "native review output parsing must be independently testable",
        )

    def test_parses_native_markdown_finding(self) -> None:
        output = b"""The implementation has a correctness issue.\n\nReview comment:\n\n- [P1] Return the sum from `add` \xe2\x80\x94 /tmp/example.py:2-2\n  Normal callers currently receive a subtraction result.\n"""

        findings = SUPERVISOR_STATE.parse_review_output(output)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["status"], "actionable")
        self.assertEqual(findings[0]["severity"], "P1")
        self.assertEqual(findings[0]["location"], "/tmp/example.py:2-2")
        self.assertIn("Return the sum", findings[0]["summary"])
        self.assertEqual(len(findings[0]["fingerprint"]), 64)

    def test_accepts_explicit_native_clean_result(self) -> None:
        output = (
            b"The only change adds a straightforward typed addition function, "
            b"with no evident correctness or compatibility issues."
        )

        self.assertEqual(SUPERVISOR_STATE.parse_review_output(output), [])

    def test_accepts_explicit_chinese_native_clean_result(self) -> None:
        output = "未发现由 allowlisted 变更引入的离散、可操作缺陷。按要求仅进行了只读静态检查。".encode()

        self.assertEqual(SUPERVISOR_STATE.parse_review_output(output), [])

    def test_rejects_unknown_native_prose(self) -> None:
        with self.assertRaisesRegex(ValueError, "incomplete coverage"):
            SUPERVISOR_STATE.parse_review_output(b"Review could not be completed.")

    def test_rejects_qualified_or_negated_clean_language(self) -> None:
        outputs = (
            b"Review could not be completed, so I cannot establish that there are no actionable issues.",
            b"This is not a claim that there are no issues; the diff was unavailable.",
            b"I did not find any actionable issues. However, the review could not be completed.",
            b"No actionable findings. This conclusion is conditional on uninspected files.",
            b"No new issues.",
            b"The patch is improved, with no new issues.",
        )

        for output in outputs:
            with self.subTest(output=output):
                with self.assertRaisesRegex(
                    ValueError,
                    "unrecognized native review output|incomplete coverage",
                ):
                    SUPERVISOR_STATE.parse_review_output(output)

    def test_chinese_product_impact_is_not_treated_as_a_coverage_caveat(self) -> None:
        output = (
            "- [P1] 恢复报告权限 — src/a.py:1\n"
            "  用户无法审查报告。"
        ).encode()

        self.assertEqual(len(SUPERVISOR_STATE.parse_review_output(output)), 1)

    def test_rejects_chinese_reviewer_coverage_caveats(self) -> None:
        outputs = (
            "我无法审查未跟踪文件。\n\n- [P1] 修复问题 — src/a.py:1",
            "本次审查未完成。\n\n- [P1] 修复问题 — src/a.py:1",
            "部分文件未审查。\n\n- [P1] 修复问题 — src/a.py:1",
        )

        for output in outputs:
            with self.subTest(output=output):
                with self.assertRaisesRegex(ValueError, "incomplete coverage"):
                    SUPERVISOR_STATE.parse_review_output(output.encode())

    def test_rejects_coverage_caveats_even_when_findings_are_present(self) -> None:
        outputs = (
            b"I could not inspect the untracked files.\n\n- [P1] Fix the bug \xe2\x80\x94 src/a.py:1",
            b"- [P1] Fix the bug \xe2\x80\x94 src/a.py:1\n  The rest of the diff was not reviewed.",
            b"I only reviewed tracked files.\n\n- [P1] Fix the bug \xe2\x80\x94 src/a.py:1",
            b"Review was limited to src/a.py.\n\n- [P1] Fix the bug \xe2\x80\x94 src/a.py:1",
            b"I could inspect only src/a.py.\n\n- [P1] Fix the bug \xe2\x80\x94 src/a.py:1",
            b"I did not inspect the untracked files.\n\n- [P1] Fix the bug \xe2\x80\x94 src/a.py:1",
            b"I skipped reviewing the untracked files.\n\n- [P1] Fix the bug \xe2\x80\x94 src/a.py:1",
            b"We omitted checking generated changes.\n\n- [P1] Fix the bug \xe2\x80\x94 src/a.py:1",
        )

        for output in outputs:
            with self.subTest(output=output):
                with self.assertRaisesRegex(ValueError, "incomplete coverage"):
                    SUPERVISOR_STATE.parse_review_output(output)

    def test_does_not_treat_ordinary_bug_wording_as_a_coverage_caveat(self) -> None:
        outputs = (
            b"- [P1] Preserve file loading \xe2\x80\x94 src/a.py:1\n  The parser cannot read empty files.",
            b"- [P1] Restore job status \xe2\x80\x94 src/a.py:1\n  Failed jobs cannot complete cleanup.",
            b"- [P1] Restore report permissions \xe2\x80\x94 src/a.py:1\n  Users cannot review the report.",
            b"- [P1] Retry record loading \xe2\x80\x94 src/a.py:1\n  The service failed to access the record.",
        )

        for output in outputs:
            with self.subTest(output=output):
                self.assertEqual(len(SUPERVISOR_STATE.parse_review_output(output)), 1)

    def test_native_fingerprint_is_stable_across_line_and_severity_changes(self) -> None:
        first = b"- [P1] Preserve the transaction boundary \xe2\x80\x94 src/a.py:10-12"
        shifted = b"- [P2] Preserve the transaction boundary \xe2\x80\x94 src/a.py:90-92"

        first_finding = SUPERVISOR_STATE.parse_review_output(first)[0]
        shifted_finding = SUPERVISOR_STATE.parse_review_output(shifted)[0]

        self.assertEqual(first_finding["fingerprint"], shifted_finding["fingerprint"])

    def test_rejects_unsupported_json_severity(self) -> None:
        output = b"""{"coverage_complete":true,"findings":[{"fingerprint":"a.py|symbol|cause|trigger","status":"actionable","severity":"critical","location":"a.py:1","summary":"bad"}]}"""

        with self.assertRaisesRegex(ValueError, "unsupported finding severity"):
            SUPERVISOR_STATE.parse_review_output(output)

    def test_json_fingerprint_requires_stable_components_and_ignores_line_number(self) -> None:
        first = b"""{"coverage_complete":true,"findings":[{"fingerprint":"a.py|symbol|root cause|trigger","status":"actionable","severity":"P1","location":"a.py:1","summary":"bad"}]}"""
        shifted = b"""{"coverage_complete":true,"findings":[{"fingerprint":"a.py|symbol|root cause|trigger","status":"actionable","severity":"P2","location":"a.py:99","summary":"wording changed"}]}"""

        first_finding = SUPERVISOR_STATE.parse_review_output(first)[0]
        shifted_finding = SUPERVISOR_STATE.parse_review_output(shifted)[0]

        self.assertEqual(first_finding["fingerprint"], shifted_finding["fingerprint"])

    def test_json_fingerprint_rejects_unstructured_or_mismatched_path(self) -> None:
        outputs = (
            b"""{"coverage_complete":true,"findings":[{"fingerprint":"arbitrary","status":"actionable","severity":"P1","location":"a.py:1","summary":"bad"}]}""",
            b"""{"coverage_complete":true,"findings":[{"fingerprint":"b.py|symbol|cause|trigger","status":"actionable","severity":"P1","location":"a.py:1","summary":"bad"}]}""",
        )

        for output in outputs:
            with self.subTest(output=output):
                with self.assertRaisesRegex(ValueError, "fingerprint"):
                    SUPERVISOR_STATE.parse_review_output(output)

    def test_structured_output_requires_explicit_complete_coverage(self) -> None:
        outputs = (
            b"""{"findings":[]}""",
            b"""{"coverage_complete":false,"findings":[]}""",
        )

        for output in outputs:
            with self.subTest(output=output):
                with self.assertRaisesRegex(ValueError, "coverage|contain"):
                    SUPERVISOR_STATE.parse_review_output(output)


if __name__ == "__main__":
    unittest.main()
