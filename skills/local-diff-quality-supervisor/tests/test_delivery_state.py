from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = SKILL_ROOT / "scripts" / "delivery-state.py"


class DeliveryStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        self.state = Path(self.temporary.name) / "state.json"
        self.run_git("init", "-b", "main")
        self.run_git("config", "user.email", "test@example.com")
        self.run_git("config", "user.name", "Test User")
        (self.root / "feature.txt").write_text("baseline\n", encoding="utf-8")
        self.run_git("add", "feature.txt")
        self.run_git("commit", "-m", "baseline")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()

    def write_json(self, name: str, value: Any) -> Path:
        path = Path(self.temporary.name) / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def run_state(
        self, *args: str, expected: int = 0
    ) -> tuple[dict[str, Any], subprocess.CompletedProcess[str]]:
        completed = subprocess.run(
            ["python3", str(SCRIPT), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(
            completed.returncode,
            expected,
            msg=f"stdout={completed.stdout}\nstderr={completed.stderr}",
        )
        self.assertTrue(completed.stdout.strip(), msg=completed.stderr)
        return json.loads(completed.stdout), completed

    def requirements(
        self,
        *,
        delivery_target: str = "local_diff",
        threshold: int = 2,
    ) -> dict[str, Any]:
        return {
            "objective": "Deliver the feature completely with zero known bugs.",
            "scope": ["Feature behavior"],
            "non_goals": ["Production deployment"],
            "delivery_target": delivery_target,
            "acceptance_criteria": [
                {"id": "A1", "description": "The requested behavior works."}
            ],
            "invariants": [
                {"id": "I1", "description": "No introduced regression remains."}
            ],
            "review_surfaces": [
                {"id": "R1", "description": "Behavior is complete and correct."},
                {"id": "R2", "description": "Tests are reliable and isolated."},
            ],
            "required_validations": [
                {"id": "V1", "description": "Direct tests pass."},
                {"id": "V2", "description": "Diff integrity passes."},
            ],
            "design_review_thresholds": {
                "high_priority_after_fix_batches": threshold,
                "surface_failures": threshold + 1,
                "surface_invalidations": threshold + 2,
            },
            "unresolved_decisions": [],
        }

    def init(
        self,
        requirements: dict[str, Any] | None = None,
        *,
        local_text: str | None = "existing local work\n",
    ) -> dict[str, Any]:
        if local_text is not None:
            (self.root / "feature.txt").write_text(local_text, encoding="utf-8")
        path = self.write_json("requirements.json", requirements or self.requirements())
        result, _ = self.run_state(
            "init",
            "--state",
            str(self.state),
            "--project-root",
            str(self.root),
            "--requirements-file",
            str(path),
        )
        return result

    def status(self) -> dict[str, Any]:
        result, _ = self.run_state("status", "--state", str(self.state))
        return result

    def modify(self, text: str) -> str:
        (self.root / "feature.txt").write_text(text, encoding="utf-8")
        return self.status()["live_diff_hash"]

    def finding(
        self,
        finding_id: str,
        *,
        classification: str = "introduced_regression",
        root_cause: str = "shared root cause",
        severity: str = "P1",
        surface: str = "R1",
        source: str = "review",
    ) -> dict[str, Any]:
        return {
            "id": finding_id,
            "classification": classification,
            "severity": severity,
            "summary": f"Finding {finding_id}",
            "trigger": f"Trigger {finding_id}",
            "root_cause": root_cause,
            "location": "feature.txt",
            "affected_surfaces": [surface],
            "evidence": f"Evidence {finding_id}",
            "source": source,
        }

    def record_initial(
        self,
        diff_hash: str,
        findings: list[dict[str, Any]] | None = None,
        *,
        failed_surface: str | None = None,
        expected: int = 0,
    ) -> dict[str, Any]:
        surfaces = []
        for surface_id in ["R1", "R2"]:
            status = "failed" if surface_id == failed_surface else "clean"
            surfaces.append(
                {
                    "id": surface_id,
                    "status": status,
                    "evidence": f"{surface_id} review evidence",
                }
            )
        result_file = self.write_json(
            "initial-review.json",
            {
                "kind": "initial",
                "diff_hash": diff_hash,
                "coverage_complete": True,
                "surfaces": surfaces,
                "findings": findings or [],
            },
        )
        result, _ = self.run_state(
            "record-review",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
            expected=expected,
        )
        return result

    def record_impact(
        self,
        diff_hash: str,
        findings: list[dict[str, Any]] | None = None,
        *,
        status: str = "clean",
        expected: int = 0,
    ) -> dict[str, Any]:
        live_state = json.loads(self.state.read_text(encoding="utf-8"))
        invalidated = [
            surface_id
            for surface_id, item in live_state["review_surfaces"].items()
            if item["status"] in {"invalidated", "failed", "blocked"}
        ]
        result_file = self.write_json(
            "impact-review.json",
            {
                "kind": "impact",
                "diff_hash": diff_hash,
                "coverage_complete": True,
                "surfaces": [
                    {
                        "id": surface_id,
                        "status": status,
                        "evidence": f"{surface_id} impact evidence",
                    }
                    for surface_id in invalidated
                ],
                "findings": findings or [],
            },
        )
        result, _ = self.run_state(
            "record-review",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
            expected=expected,
        )
        return result

    def begin_and_record_fix(self, finding_id: str, new_text: str) -> str:
        ids = self.write_json("finding-ids.json", [finding_id])
        batch_paths = self.write_json("batch-paths.json", ["feature.txt"])
        begun, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(batch_paths),
        )
        before = begun["active_fix"]["diff_hash_before"]
        after = self.modify(new_text)
        result_file = self.write_json(
            "fix-result.json",
            {
                "finding_ids": [finding_id],
                "diff_hash_before": before,
                "diff_hash_after": after,
                "changed_paths": ["feature.txt"],
                "executor": {
                    "name": "code-review-fix-loop",
                    "mode": "frozen_batch",
                },
                "test_mode": "none",
                "invalidated_surfaces": ["R1"],
                "direct_validation": [
                    {"status": "pass", "evidence": "Focused regression test passed."}
                ],
            },
        )
        self.run_state(
            "record-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
        )
        return after

    def record_full_validation(
        self,
        diff_hash: str,
        *,
        verified: list[str] | None = None,
    ) -> dict[str, Any]:
        result_file = self.write_json(
            "validation.json",
            {
                "diff_hash": diff_hash,
                "validations": [
                    {"id": "V1", "status": "pass", "evidence": "Tests passed."},
                    {"id": "V2", "status": "pass", "evidence": "Diff check passed."},
                ],
                "acceptance": [
                    {"id": "A1", "status": "pass", "evidence": "Behavior proved."}
                ],
                "verified_findings": verified or [],
                "finding_evidence": {
                    finding_id: "Regression and impact evidence passed."
                    for finding_id in (verified or [])
                },
                "new_findings": [],
            },
        )
        result, _ = self.run_state(
            "record-validation",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
        )
        return result

    def make_ready_without_findings(self) -> str:
        initialized = self.init(local_text="implemented\n")
        diff_hash = initialized["live_diff_hash"]
        self.record_initial(diff_hash)
        self.record_full_validation(diff_hash)
        self.run_state("evaluate-ready", "--state", str(self.state))
        return diff_hash

    def test_init_installs_self_contained_defaults(self) -> None:
        requirements = {
            "objective": "Deliver a feature.",
            "scope": ["Feature"],
            "acceptance_criteria": ["Feature works."],
        }
        result = self.init(requirements)
        self.assertEqual(result["phase"], "initial_review")
        self.assertEqual(len(result["review_surfaces"]), 12)
        self.assertEqual(len(result["validations"]), 4)
        self.assertFalse(result["ready"])

    def test_invalid_design_threshold_is_reported_without_traceback(self) -> None:
        requirements = self.requirements()
        requirements["design_review_thresholds"] = "two"
        (self.root / "feature.txt").write_text("implementation\n", encoding="utf-8")
        requirements_file = self.write_json("bad-thresholds.json", requirements)
        result, completed = self.run_state(
            "init",
            "--state",
            str(self.state),
            "--project-root",
            str(self.root),
            "--requirements-file",
            str(requirements_file),
            expected=2,
        )
        self.assertIn("must be an object", result["error"])
        self.assertEqual(completed.stderr, "")

    def test_invalid_optional_string_arrays_are_structured_errors(self) -> None:
        for field_name in ["non_goals", "authorization_boundary"]:
            with self.subTest(field_name=field_name):
                requirements = self.requirements()
                requirements[field_name] = "not-an-array"
                (self.root / "feature.txt").write_text(
                    "implementation\n",
                    encoding="utf-8",
                )
                requirements_file = self.write_json(
                    f"bad-{field_name}.json",
                    requirements,
                )
                result, completed = self.run_state(
                    "init",
                    "--state",
                    str(self.state),
                    "--project-root",
                    str(self.root),
                    "--requirements-file",
                    str(requirements_file),
                    expected=2,
                )
                self.assertIn("must be a string array", result["error"])
                self.assertEqual(completed.stderr, "")

    def test_default_campaign_reaches_ready_local_diff_without_repo_agents(self) -> None:
        requirements = {
            "objective": "Deliver a self-contained feature.",
            "scope": ["Feature"],
            "acceptance_criteria": [
                {"id": "A1", "description": "Feature works completely."}
            ],
        }
        initialized = self.init(
            requirements,
            local_text="self-contained implementation\n",
        )
        diff_hash = initialized["live_diff_hash"]
        review_file = self.write_json(
            "default-review.json",
            {
                "kind": "initial",
                "diff_hash": diff_hash,
                "coverage_complete": True,
                "surfaces": [
                    {
                        "id": surface_id,
                        "status": "clean",
                        "evidence": f"{surface_id} examined against the current diff.",
                    }
                    for surface_id in initialized["review_surfaces"]
                ],
                "findings": [],
            },
        )
        self.run_state(
            "record-review",
            "--state",
            str(self.state),
            "--result-file",
            str(review_file),
        )
        validation_file = self.write_json(
            "default-validation.json",
            {
                "diff_hash": diff_hash,
                "validations": [
                    {
                        "id": validation_id,
                        "status": "pass",
                        "evidence": f"{validation_id} passed.",
                    }
                    for validation_id in initialized["validations"]
                ],
                "acceptance": [
                    {"id": "A1", "status": "pass", "evidence": "Behavior proved."}
                ],
                "verified_findings": [],
                "finding_evidence": {},
                "new_findings": [],
            },
        )
        self.run_state(
            "record-validation",
            "--state",
            str(self.state),
            "--result-file",
            str(validation_file),
        )
        ready, _ = self.run_state("evaluate-ready", "--state", str(self.state))
        self.assertTrue(ready["ready_local_diff"])
        self.assertEqual(ready["phase"], "ready_local_diff")

    def test_existing_state_cannot_be_reset(self) -> None:
        self.init()
        requirements = self.write_json("requirements-again.json", self.requirements())
        result, _ = self.run_state(
            "init",
            "--state",
            str(self.state),
            "--project-root",
            str(self.root),
            "--requirements-file",
            str(requirements),
            expected=3,
        )
        self.assertIn("resume", result["error"])

    def test_version_two_state_migrates_without_resetting_history(self) -> None:
        initialized = self.init()
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-MIGRATE")],
            failed_surface="R1",
        )
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        persisted["version"] = 2
        for key in [
            "authorized_paths",
            "scope_extensions",
            "derived_requirements",
            "design_epoch",
            "epoch_fix_batch_count",
            "ready_local_diff",
            "root_cause_stats",
        ]:
            persisted.pop(key, None)
        persisted["delivered"] = False
        persisted["delivery"] = None
        for surface in persisted["review_surfaces"].values():
            surface.pop("epoch_failure_count", None)
            surface.pop("epoch_invalidation_count", None)
        self.state.write_text(json.dumps(persisted), encoding="utf-8")

        migrated = self.status()
        self.assertEqual(migrated["authorized_paths"], initialized["live_paths"])
        self.assertEqual(migrated["design_epoch"], 1)
        stored = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(stored["version"], 9)
        self.assertEqual(
            stored["derived_requirements"]["REQ-F-MIGRATE"]["status"],
            "open",
        )
        self.assertTrue(stored["root_cause_stats"])
        self.assertTrue(
            any(item["event"] == "state_migrated" for item in stored["history"])
        )

    def test_migration_revokes_legacy_ready_claim(self) -> None:
        self.make_ready_without_findings()
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        persisted["version"] = 4
        persisted["status"] = "ready"
        persisted["phase"] = "ready_local_diff"
        self.state.write_text(json.dumps(persisted), encoding="utf-8")

        migrated = self.status()
        self.assertFalse(migrated["ready"])
        self.assertFalse(migrated["ready_local_diff"])
        self.assertEqual(migrated["status"], "active")
        self.assertEqual(migrated["phase"], "validation")
        stored = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIsNone(stored["ready_manifest"])
        self.assertTrue(
            any(
                event["event"] == "state_migrated" and event["ready_revoked"]
                for event in stored["history"]
            )
        )

    def test_version_two_false_positive_requirement_remains_non_blocking(self) -> None:
        initialized = self.init(local_text="suspected bug\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-MIGRATE-FP")],
            failed_surface="R1",
        )
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        persisted["findings"]["F-MIGRATE-FP"]["status"] = "false_positive"
        persisted["findings"]["F-MIGRATE-FP"]["evidence"] = "Legacy evidence."
        persisted["version"] = 2
        for key in [
            "authorized_paths",
            "scope_extensions",
            "derived_requirements",
            "design_epoch",
            "epoch_fix_batch_count",
            "ready_local_diff",
            "root_cause_stats",
        ]:
            persisted.pop(key, None)
        for surface in persisted["review_surfaces"].values():
            surface.pop("epoch_failure_count", None)
            surface.pop("epoch_invalidation_count", None)
        self.state.write_text(json.dumps(persisted), encoding="utf-8")

        self.status()
        stored = json.loads(self.state.read_text(encoding="utf-8"))
        finding = stored["findings"]["F-MIGRATE-FP"]
        requirement = stored["derived_requirements"]["REQ-F-MIGRATE-FP"]
        self.assertEqual(finding["status"], "false_positive")
        self.assertEqual(requirement["status"], "false_positive")
        self.assertEqual(
            requirement["evidence_diff_hash"],
            stored["current_diff_hash"],
        )

    def test_version_three_active_fix_migration_reopens_batch(self) -> None:
        initialized = self.init(local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-MIGRATE-ACTIVE")],
            failed_surface="R1",
        )
        ids = self.write_json("migration-active-ids.json", ["F-MIGRATE-ACTIVE"])
        paths = self.write_json("migration-active-paths.json", ["feature.txt"])
        self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
        )
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        persisted["version"] = 3
        persisted.pop("root_cause_stats", None)
        persisted["findings"]["F-MIGRATE-ACTIVE"]["recurrence_count"] = 2
        persisted["findings"]["F-MIGRATE-ACTIVE"]["epoch_recurrence_count"] = 2
        self.state.write_text(json.dumps(persisted), encoding="utf-8")

        migrated = self.status()
        self.assertEqual(migrated["phase"], "remediation")
        stored = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(stored["version"], 9)
        self.assertIsNone(stored["active_fix"])
        self.assertEqual(stored["findings"]["F-MIGRATE-ACTIVE"]["status"], "open")
        root_stat = next(iter(stored["root_cause_stats"].values()))
        self.assertEqual(root_stat["recurrence_count"], 2)
        self.assertEqual(root_stat["epoch_recurrence_count"], 2)
        self.assertTrue(
            any(
                item["event"] == "state_migrated"
                and item["reopened_active_fix"]
                for item in stored["history"]
            )
        )

    def test_version_eight_active_fix_migration_reopens_unsafe_batch(self) -> None:
        initialized = self.init(local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-MIGRATE-DIRECTORY-BOUNDARY")],
            failed_surface="R1",
        )
        ids = self.write_json(
            "migration-directory-ids.json",
            ["F-MIGRATE-DIRECTORY-BOUNDARY"],
        )
        paths = self.write_json("migration-directory-paths.json", ["feature.txt"])
        self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
        )
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        persisted["version"] = 8
        self.state.write_text(json.dumps(persisted), encoding="utf-8")

        migrated = self.status()
        self.assertEqual(migrated["phase"], "remediation")
        stored = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(stored["version"], 9)
        self.assertIsNone(stored["active_fix"])
        self.assertEqual(
            stored["findings"]["F-MIGRATE-DIRECTORY-BOUNDARY"]["status"],
            "open",
        )
        self.assertTrue(
            any(
                item["event"] == "state_migrated"
                and item["from_version"] == 8
                and item["reopened_active_fix"]
                for item in stored["history"]
            )
        )

    def test_version_eight_migration_backfills_current_required_fields(self) -> None:
        initialized = self.init(local_text="implementation\n")
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        persisted["version"] = 8
        for key in [
            "authorized_paths",
            "scope_extensions",
            "derived_requirements",
            "design_epoch",
            "epoch_fix_batch_count",
            "design_review_count",
            "convergence_events",
            "deferred_design_review",
            "complete_validation_campaign",
            "follow_up_requirements",
            "active_fix",
            "ready",
            "ready_local_diff",
            "ready_diff_hash",
            "ready_manifest",
            "requirements_digest",
            "invariants",
            "review_campaign_count",
            "fix_batch_count",
        ]:
            persisted.pop(key, None)
        for surface in persisted["review_surfaces"].values():
            surface.pop("epoch_failure_count", None)
            surface.pop("epoch_invalidation_count", None)
        self.state.write_text(json.dumps(persisted), encoding="utf-8")

        migrated = self.status()
        self.assertEqual(migrated["authorized_paths"], initialized["live_paths"])
        stored = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(stored["version"], 9)
        for key in [
            "convergence_events",
            "deferred_design_review",
            "complete_validation_campaign",
            "derived_requirements",
            "scope_extensions",
            "follow_up_requirements",
            "active_fix",
            "invariants",
        ]:
            self.assertIn(key, stored)
        for surface in stored["review_surfaces"].values():
            self.assertIn("epoch_failure_count", surface)
            self.assertIn("epoch_invalidation_count", surface)

    def test_version_eight_migration_backfills_current_threshold_defaults(self) -> None:
        self.init(local_text="implementation\n")
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        persisted["version"] = 8
        persisted["requirements"]["design_review_thresholds"].pop(
            "root_cause_recurrences"
        )
        self.state.write_text(json.dumps(persisted), encoding="utf-8")

        self.status()

        stored = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(
            stored["requirements"]["design_review_thresholds"][
                "root_cause_recurrences"
            ],
            2,
        )

    def test_version_three_migration_aggregates_same_root_findings(self) -> None:
        initialized = self.init(local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [
                self.finding("F-MIGRATE-ROOT-1"),
                self.finding("F-MIGRATE-ROOT-2"),
            ],
            failed_surface="R1",
        )
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        persisted["version"] = 3
        persisted.pop("root_cause_stats", None)
        self.state.write_text(json.dumps(persisted), encoding="utf-8")

        self.status()
        stored = json.loads(self.state.read_text(encoding="utf-8"))
        root_stat = next(iter(stored["root_cause_stats"].values()))
        self.assertEqual(root_stat["epoch_occurrence_count"], 2)
        self.assertEqual(root_stat["epoch_recurrence_count"], 1)

    def test_version_three_migration_does_not_count_decision_resolution(self) -> None:
        initialized = self.init(local_text="ambiguous bug\n")
        decision = self.finding(
            "F-MIGRATE-DECISION",
            classification="decision_required",
            root_cause="pending root cause",
        )
        review_file = self.write_json(
            "migration-decision-review.json",
            {
                "kind": "initial",
                "diff_hash": initialized["live_diff_hash"],
                "coverage_complete": True,
                "surfaces": [
                    {"id": "R1", "status": "failed", "evidence": "Decision required."},
                    {"id": "R2", "status": "clean", "evidence": "Clean."},
                ],
                "findings": [decision],
            },
        )
        self.run_state(
            "record-review",
            "--state",
            str(self.state),
            "--result-file",
            str(review_file),
            expected=3,
        )
        resolution_file = self.write_json(
            "migration-decision-resolution.json",
            {
                "resolved_decisions": [],
                "finding_resolutions": [
                    {
                        "id": "F-MIGRATE-DECISION",
                        "classification": "introduced_regression",
                        "root_cause": "resolved root cause",
                        "evidence": "The implementation owns this behavior.",
                    }
                ],
            },
        )
        self.run_state(
            "resolve-decision",
            "--state",
            str(self.state),
            "--result-file",
            str(resolution_file),
        )
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        persisted["version"] = 3
        persisted.pop("root_cause_stats", None)
        self.state.write_text(json.dumps(persisted), encoding="utf-8")

        self.status()
        stored = json.loads(self.state.read_text(encoding="utf-8"))
        root_stat = next(iter(stored["root_cause_stats"].values()))
        self.assertEqual(root_stat["epoch_occurrence_count"], 1)
        self.assertEqual(root_stat["epoch_recurrence_count"], 0)

    def test_requirement_decision_pauses_initialization(self) -> None:
        requirements = self.requirements()
        requirements["unresolved_decisions"] = ["Choose the state owner."]
        result = self.init_paused(requirements)
        self.assertEqual(result["phase"], "decision_required")

    def test_decision_resolution_rejects_paused_diff_drift(self) -> None:
        requirements = self.requirements()
        requirements["unresolved_decisions"] = ["Choose the state owner."]
        self.init_paused(requirements)
        self.modify("changed while decision was paused\n")
        resolution_file = self.write_json(
            "drifted-decision-resolution.json",
            {
                "resolved_decisions": [
                    {
                        "decision": "Choose the state owner.",
                        "resolution": "The database is authoritative.",
                        "evidence": "The user confirmed the owner.",
                    }
                ],
                "finding_resolutions": [],
            },
        )
        rejected, _ = self.run_state(
            "resolve-decision",
            "--state",
            str(self.state),
            "--result-file",
            str(resolution_file),
            expected=2,
        )
        self.assertIn("correctness decision was paused", rejected["error"])

    def init_paused(self, requirements: dict[str, Any]) -> dict[str, Any]:
        (self.root / "feature.txt").write_text(
            "existing local work\n",
            encoding="utf-8",
        )
        path = self.write_json("requirements-paused.json", requirements)
        result, _ = self.run_state(
            "init",
            "--state",
            str(self.state),
            "--project-root",
            str(self.root),
            "--requirements-file",
            str(path),
            expected=3,
        )
        return result

    def test_fix_impact_review_validation_and_ready(self) -> None:
        initialized = self.init(local_text="buggy implementation\n")
        initial_hash = initialized["live_diff_hash"]
        self.record_initial(
            initial_hash,
            [self.finding("F1")],
            failed_surface="R1",
        )
        fixed_hash = self.begin_and_record_fix("F1", "fixed implementation\n")
        impact = self.record_impact(fixed_hash)
        self.assertEqual(impact["review_surfaces"]["R1"], "clean")
        self.assertEqual(impact["phase"], "validation")
        self.assertEqual(impact["remediable_findings"], [])
        self.assertEqual(impact["verification_pending_findings"], ["F1"])
        validation = self.record_full_validation(fixed_hash, verified=["F1"])
        self.assertEqual(validation["phase"], "acceptance_audit")
        self.assertEqual(
            validation["derived_requirements"]["REQ-F1"]["status"],
            "verified",
        )
        ready, _ = self.run_state("evaluate-ready", "--state", str(self.state))
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["status"], "ready")

    def test_validation_cannot_skip_required_impact_review(self) -> None:
        initialized = self.init(local_text="buggy implementation\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-IMPACT")],
            failed_surface="R1",
        )
        fixed_hash = self.begin_and_record_fix("F-IMPACT", "fixed implementation\n")
        validation_file = self.write_json(
            "premature-validation.json",
            {
                "diff_hash": fixed_hash,
                "validations": [
                    {"id": "V1", "status": "pass", "evidence": "Tests passed."}
                ],
                "acceptance": [
                    {"id": "A1", "status": "pass", "evidence": "Behavior passed."}
                ],
                "verified_findings": ["F-IMPACT"],
                "finding_evidence": {"F-IMPACT": "Focused evidence passed."},
                "new_findings": [],
            },
        )
        result, _ = self.run_state(
            "record-validation",
            "--state",
            str(self.state),
            "--result-file",
            str(validation_file),
            expected=2,
        )
        self.assertIn("not ready for validation", result["error"])

    def test_impact_review_cannot_clean_a_surface_with_an_open_finding(self) -> None:
        initialized = self.init(local_text="buggy implementation\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [
                self.finding("F-FIXED", root_cause="first cause"),
                self.finding("F-OPEN", root_cause="second cause"),
            ],
            failed_surface="R1",
        )
        fixed_hash = self.begin_and_record_fix("F-FIXED", "partial fix\n")
        result = self.record_impact(fixed_hash, expected=2)
        self.assertIn("requires affected surfaces to be failed", result["error"])

    def test_introduced_regression_blocks_ready_and_is_not_follow_up(self) -> None:
        initialized = self.init(local_text="implementation\n")
        diff_hash = initialized["live_diff_hash"]
        review = self.record_initial(
            diff_hash,
            [self.finding("F1", classification="introduced_regression")],
            failed_surface="R1",
        )
        self.assertIn("F1", review["open_findings"])
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertNotIn("F1", state["follow_up_requirements"])
        result, _ = self.run_state(
            "evaluate-ready", "--state", str(self.state), expected=3
        )
        self.assertTrue(any("finding F1" in failure for failure in result["failures"]))

    def test_independent_follow_up_does_not_block_ready(self) -> None:
        initialized = self.init(local_text="implementation\n")
        diff_hash = initialized["live_diff_hash"]
        review = self.record_initial(
            diff_hash,
            [
                self.finding(
                    "F2",
                    classification="independent_follow_up",
                    severity="P3",
                )
            ],
        )
        self.assertEqual(review["open_findings"], [])
        self.assertEqual(review["follow_up_requirements"], ["F2"])
        self.record_full_validation(diff_hash)
        ready, _ = self.run_state("evaluate-ready", "--state", str(self.state))
        self.assertTrue(ready["ready"])

    def test_repeated_high_priority_finding_requires_design_review(self) -> None:
        initialized = self.init(
            self.requirements(threshold=1),
            local_text="buggy\n",
        )
        initial_hash = initialized["live_diff_hash"]
        self.record_initial(
            initial_hash,
            [self.finding("F1")],
            failed_surface="R1",
        )
        fixed_hash = self.begin_and_record_fix("F1", "first fix\n")
        result = self.record_impact(
            fixed_hash,
            [
                self.finding(
                    "F2",
                    root_cause="new symptom after patch",
                    severity="P1",
                )
            ],
            status="failed",
            expected=3,
        )
        self.assertEqual(result["phase"], "design_review_required")
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(persisted["phase"], "design_review_required")
        self.assertEqual(persisted["fix_batch_count"], 1)

    def test_ready_rejects_stale_diff_evidence(self) -> None:
        diff_hash = self.make_ready_without_findings()
        self.assertIsInstance(diff_hash, str)
        (self.root / "feature.txt").write_text("changed after ready\n", encoding="utf-8")
        result = self.status()
        self.assertFalse(result["state_matches_live_diff"])
        self.assertNotEqual(result["live_diff_hash"], result["current_diff_hash"])

    def test_ready_rejects_change_during_snapshot_or_manifest_capture(self) -> None:
        initialized = self.init(local_text="implemented\n")
        diff_hash = initialized["live_diff_hash"]
        self.record_initial(diff_hash)
        self.record_full_validation(diff_hash)

        spec = importlib.util.spec_from_file_location("delivery_state_under_test", SCRIPT)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original_manifest = module.build_ready_manifest

        def racing_manifest(root: Path, paths: list[str]) -> dict[str, dict[str, Any]]:
            (root / "feature.txt").write_text(
                "changed during READY\n",
                encoding="utf-8",
            )
            return original_manifest(root, paths)

        module.build_ready_manifest = racing_manifest
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as stopped:
                module.command_evaluate_ready(SimpleNamespace(state=str(self.state)))
        self.assertNotEqual(stopped.exception.code, 0)
        result = json.loads(output.getvalue())
        if "error" in result:
            self.assertIn("changed while capturing READY", result["error"])
        else:
            self.assertFalse(result["ready"])
            self.assertIn(
                "live diff does not match the latest recorded evidence",
                result["failures"],
            )
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertFalse(persisted["ready"])
        self.assertFalse(persisted["ready_local_diff"])

    def test_ready_prohibits_another_review(self) -> None:
        diff_hash = self.make_ready_without_findings()
        review_file = self.write_json(
            "late-review.json",
            {
                "kind": "impact",
                "diff_hash": diff_hash,
                "coverage_complete": True,
                "surfaces": [
                    {"id": "R1", "status": "clean", "evidence": "Still clean."}
                ],
                "findings": [],
            },
        )
        result, _ = self.run_state(
            "record-review",
            "--state",
            str(self.state),
            "--result-file",
            str(review_file),
            expected=2,
        )
        self.assertIn("active", result["error"])

    def test_ready_is_terminal_local_diff(self) -> None:
        diff_hash = self.make_ready_without_findings()
        result = self.status()
        self.assertTrue(result["ready_local_diff"])
        self.assertEqual(result["phase"], "ready_local_diff")
        self.assertEqual(result["current_diff_hash"], diff_hash)

    def test_pr_finding_revokes_ready_without_resetting_history(self) -> None:
        diff_hash = self.make_ready_without_findings()
        result_file = self.write_json(
            "new-findings.json",
            {
                "diff_hash": diff_hash,
                "findings": [self.finding("F9", source="pr-review")],
            },
        )
        result, _ = self.run_state(
            "add-findings",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
        )
        self.assertFalse(result["ready"])
        self.assertEqual(result["phase"], "remediation")
        self.assertIn("F9", result["open_findings"])
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertTrue(persisted["initial_review_completed"])
        self.assertEqual(persisted["review_campaign_count"], 1)

    def test_late_finding_fix_counts_one_epoch_invalidation(self) -> None:
        diff_hash = self.make_ready_without_findings()
        result_file = self.write_json(
            "late-finding.json",
            {
                "diff_hash": diff_hash,
                "findings": [self.finding("F-LATE", source="external-review")],
            },
        )
        self.run_state(
            "add-findings",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
        )
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(
            persisted["review_surfaces"]["R1"]["epoch_invalidation_count"],
            1,
        )

        self.begin_and_record_fix("F-LATE", "late finding fixed\n")
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(
            persisted["review_surfaces"]["R1"]["epoch_invalidation_count"],
            1,
        )

    def test_sensitive_untracked_path_is_rejected(self) -> None:
        (self.root / ".env.local").write_text("SECRET=value\n", encoding="utf-8")
        requirements = self.write_json("requirements-sensitive.json", self.requirements())
        result, _ = self.run_state(
            "init",
            "--state",
            str(self.state),
            "--project-root",
            str(self.root),
            "--requirements-file",
            str(requirements),
            expected=2,
        )
        self.assertIn("sensitive-looking", result["error"])
        self.assertNotIn("SECRET=value", json.dumps(result))

    def test_allowed_paths_reject_scope_expansion(self) -> None:
        requirements = self.requirements()
        requirements["allowed_paths"] = ["allowed"]
        (self.root / "feature.txt").write_text(
            "outside allowed paths\n",
            encoding="utf-8",
        )
        requirements_file = self.write_json("scope-requirements.json", requirements)
        result, _ = self.run_state(
            "init",
            "--state",
            str(self.state),
            "--project-root",
            str(self.root),
            "--requirements-file",
            str(requirements_file),
            expected=2,
        )
        self.assertIn("initial local diff contains paths outside", result["error"])

    def test_scope_extension_is_exact_linked_and_required_before_fix(self) -> None:
        requirements = self.requirements()
        requirements["allowed_paths"] = ["feature.txt", "dependency.txt"]
        requirements["design_review_thresholds"]["surface_invalidations"] = 2
        initialized = self.init(requirements, local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-SCOPE")],
            failed_surface="R1",
        )
        ids = self.write_json("scope-finding-ids.json", ["F-SCOPE"])
        unauthorized_paths = self.write_json(
            "unauthorized-batch-paths.json",
            ["feature.txt", "dependency.txt"],
        )
        rejected, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(unauthorized_paths),
            expected=2,
        )
        self.assertIn("initial scope or extend-scope", rejected["error"])

        extension_file = self.write_json(
            "scope-extension.json",
            {
                "reason": "The current regression requires its direct dependency.",
                "finding_id": "F-SCOPE",
                "paths": ["dependency.txt"],
                "invalidated_surfaces": ["R1"],
            },
        )
        extended, _ = self.run_state(
            "extend-scope",
            "--state",
            str(self.state),
            "--result-file",
            str(extension_file),
        )
        self.assertEqual(
            extended["authorized_paths"],
            ["dependency.txt", "feature.txt"],
        )
        self.assertEqual(
            extended["scope_extension"]["parent"],
            {"kind": "finding", "id": "F-SCOPE"},
        )
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(
            persisted["review_surfaces"]["R1"]["epoch_invalidation_count"],
            1,
        )

        begun, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(unauthorized_paths),
            "--test-mode",
            "light",
        )
        contract = begun["active_fix"]["executor_contract"]
        self.assertEqual(contract["preferred"], "code-review-fix-loop")
        self.assertEqual(contract["mode"], "frozen_batch")
        self.assertFalse(contract["may_initial_review"])
        self.assertFalse(contract["may_expand_scope"])
        self.assertFalse(contract["may_claim_ready"])

        (self.root / "feature.txt").write_text("fixed\n", encoding="utf-8")
        (self.root / "dependency.txt").write_text("required dependency\n", encoding="utf-8")
        after = self.status()["live_diff_hash"]
        fix_file = self.write_json(
            "scope-fix-result.json",
            {
                "finding_ids": ["F-SCOPE"],
                "diff_hash_before": begun["active_fix"]["diff_hash_before"],
                "diff_hash_after": after,
                "changed_paths": ["feature.txt", "dependency.txt"],
                "executor": {
                    "name": "code-review-fix-loop",
                    "mode": "frozen_batch",
                },
                "test_mode": "light",
                "invalidated_surfaces": ["R1"],
                "direct_validation": [
                    {"status": "pass", "evidence": "Focused tests passed."}
                ],
            },
        )
        result, _ = self.run_state(
            "record-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(fix_file),
        )
        self.assertEqual(result["phase"], "impact_review")
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(
            persisted["review_surfaces"]["R1"]["epoch_invalidation_count"],
            1,
        )

    def test_allowed_file_boundary_does_not_authorize_descendants(self) -> None:
        requirements = self.requirements()
        requirements["allowed_paths"] = ["feature.txt", "config/runtime.json"]
        initialized = self.init(requirements, local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-EXACT-BOUNDARY")],
            failed_surface="R1",
        )
        extension_file = self.write_json(
            "exact-file-boundary-extension.json",
            {
                "reason": "Attempt to widen one exact file authorization.",
                "finding_id": "F-EXACT-BOUNDARY",
                "paths": ["config/runtime.json/generated.py"],
                "invalidated_surfaces": ["R1"],
            },
        )

        rejected, _ = self.run_state(
            "extend-scope",
            "--state",
            str(self.state),
            "--result-file",
            str(extension_file),
            expected=2,
        )
        self.assertIn("authorization boundary", rejected["error"])

    def test_scope_extension_rejects_symlinked_parent(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (outside / "dependency.txt").write_text("outside\n", encoding="utf-8")
        (self.root / "vendor").symlink_to(outside, target_is_directory=True)
        requirements = self.requirements()
        requirements["allowed_paths"] = ["feature.txt", "vendor"]
        initialized = self.init(requirements, local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-SYMLINK-SCOPE")],
            failed_surface="R1",
        )
        extension_file = self.write_json(
            "symlink-scope-extension.json",
            {
                "reason": "Attempt to extend through a repository symlink.",
                "finding_id": "F-SYMLINK-SCOPE",
                "paths": ["vendor/dependency.txt"],
                "invalidated_surfaces": ["R1"],
            },
        )
        rejected, _ = self.run_state(
            "extend-scope",
            "--state",
            str(self.state),
            "--result-file",
            str(extension_file),
            expected=2,
        )
        self.assertIn("symlinked parent", rejected["error"])

    def test_begin_fix_rejects_symlink_endpoint(self) -> None:
        outside = Path(self.temporary.name) / "outside-target.txt"
        outside.write_text("outside\n", encoding="utf-8")
        feature = self.root / "feature.txt"
        feature.unlink()
        feature.symlink_to(outside)
        initialized = self.init(local_text=None)
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-SYMLINK-ENDPOINT")],
            failed_surface="R1",
        )
        ids = self.write_json("symlink-endpoint-ids.json", ["F-SYMLINK-ENDPOINT"])
        paths = self.write_json("symlink-endpoint-paths.json", ["feature.txt"])
        rejected, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
            expected=2,
        )
        self.assertIn("symlink endpoints", rejected["error"])
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIsNone(persisted["active_fix"])
        self.assertEqual(persisted["findings"]["F-SYMLINK-ENDPOINT"]["status"], "open")

    def test_scope_extension_rejects_git_invisible_paths(self) -> None:
        (self.root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        self.run_git("add", ".gitignore")
        self.run_git("commit", "-m", "ignore generated file")
        requirements = self.requirements()
        requirements["allowed_paths"] = [
            "feature.txt",
            "ignored.txt",
            ".git/hooks/pre-commit",
        ]
        initialized = self.init(requirements, local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-INVISIBLE-SCOPE")],
            failed_surface="R1",
        )

        for name, path, expected_message in [
            ("ignored", "ignored.txt", "ignored"),
            ("git-metadata", ".git/hooks/pre-commit", "Git metadata"),
        ]:
            with self.subTest(path=path):
                extension_file = self.write_json(
                    f"{name}-scope-extension.json",
                    {
                        "reason": "Attempt to authorize a Git-invisible write.",
                        "finding_id": "F-INVISIBLE-SCOPE",
                        "paths": [path],
                        "invalidated_surfaces": ["R1"],
                    },
                )
                rejected, _ = self.run_state(
                    "extend-scope",
                    "--state",
                    str(self.state),
                    "--result-file",
                    str(extension_file),
                    expected=2,
                )
                self.assertIn(expected_message, rejected["error"])

    def test_scope_extension_rejects_git_pathspec_magic(self) -> None:
        requirements = self.requirements()
        requirements["allowed_paths"] = ["feature.txt", ":(exclude)feature.txt"]
        requirements_file = self.write_json(
            "pathspec-requirements.json",
            requirements,
        )
        rejected, _ = self.run_state(
            "init",
            "--state",
            str(self.state),
            "--project-root",
            str(self.root),
            "--requirements-file",
            str(requirements_file),
            expected=2,
        )
        self.assertIn("pathspec magic", rejected["error"])

    def test_record_fix_rejects_newly_ignored_persistent_write(self) -> None:
        (self.root / ".gitignore").write_text("baseline-ignore\n", encoding="utf-8")
        self.run_git("add", ".gitignore")
        self.run_git("commit", "-m", "baseline ignore file")
        requirements = self.requirements()
        requirements["allowed_paths"] = [
            "feature.txt",
            ".gitignore",
            "generated.txt",
        ]
        (self.root / ".gitignore").write_text("pending-ignore\n", encoding="utf-8")
        initialized = self.init(requirements, local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-NEWLY-IGNORED")],
            failed_surface="R1",
        )
        extension_file = self.write_json(
            "newly-ignored-extension.json",
            {
                "reason": "The fix initially requires a generated file.",
                "finding_id": "F-NEWLY-IGNORED",
                "paths": ["generated.txt"],
                "invalidated_surfaces": ["R1"],
            },
        )
        self.run_state(
            "extend-scope",
            "--state",
            str(self.state),
            "--result-file",
            str(extension_file),
        )
        ids = self.write_json("newly-ignored-ids.json", ["F-NEWLY-IGNORED"])
        paths = self.write_json(
            "newly-ignored-paths.json",
            ["feature.txt", ".gitignore", "generated.txt"],
        )
        begun, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
        )
        (self.root / "feature.txt").write_text("fixed\n", encoding="utf-8")
        (self.root / ".gitignore").write_text("generated.txt\n", encoding="utf-8")
        (self.root / "generated.txt").write_text("persistent write\n", encoding="utf-8")
        after = self.status()["live_diff_hash"]
        result_file = self.write_json(
            "newly-ignored-result.json",
            {
                "finding_ids": ["F-NEWLY-IGNORED"],
                "diff_hash_before": begun["active_fix"]["diff_hash_before"],
                "diff_hash_after": after,
                "changed_paths": ["feature.txt", ".gitignore", "generated.txt"],
                "executor": {"name": "direct", "mode": "frozen_batch"},
                "test_mode": "none",
                "invalidated_surfaces": ["R1"],
                "direct_validation": [
                    {"status": "pass", "evidence": "Focused reproduction completed."}
                ],
            },
        )
        rejected, _ = self.run_state(
            "record-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
            expected=2,
        )
        self.assertIn("outside the supervised Git diff", rejected["error"])
        self.assertIn("generated.txt", rejected["error"])

    def test_record_fix_rejects_ignored_write_outside_frozen_batch(self) -> None:
        (self.root / ".gitignore").write_text("outside.log\n", encoding="utf-8")
        self.run_git("add", ".gitignore")
        self.run_git("commit", "-m", "ignore outside log")
        initialized = self.init(local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-IGNORED-OUTSIDE")],
            failed_surface="R1",
        )
        ids = self.write_json("ignored-outside-ids.json", ["F-IGNORED-OUTSIDE"])
        paths = self.write_json("ignored-outside-paths.json", ["feature.txt"])
        begun, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
        )
        (self.root / "feature.txt").write_text("fixed\n", encoding="utf-8")
        (self.root / "outside.log").write_text("out-of-batch write\n", encoding="utf-8")
        after = self.status()["live_diff_hash"]
        result_file = self.write_json(
            "ignored-outside-result.json",
            {
                "finding_ids": ["F-IGNORED-OUTSIDE"],
                "diff_hash_before": begun["active_fix"]["diff_hash_before"],
                "diff_hash_after": after,
                "changed_paths": ["feature.txt"],
                "executor": {"name": "direct", "mode": "frozen_batch"},
                "test_mode": "none",
                "invalidated_surfaces": ["R1"],
                "direct_validation": [
                    {"status": "pass", "evidence": "The claimed fix passed."}
                ],
            },
        )
        rejected, _ = self.run_state(
            "record-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
            expected=2,
        )
        self.assertIn("outside the frozen batch", rejected["error"])
        self.assertIn("outside.log", rejected["error"])

    def test_record_fix_compares_ignored_metadata_not_wall_clock(self) -> None:
        (self.root / ".gitignore").write_text("outside.log\n", encoding="utf-8")
        self.run_git("add", ".gitignore")
        self.run_git("commit", "-m", "ignore outside log")
        outside = self.root / "outside.log"
        outside.write_text("before\n", encoding="utf-8")
        initialized = self.init(local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-IGNORED-METADATA")],
            failed_surface="R1",
        )
        ids = self.write_json("ignored-metadata-ids.json", ["F-IGNORED-METADATA"])
        paths = self.write_json("ignored-metadata-paths.json", ["feature.txt"])
        begun, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
        )
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        persisted["active_fix"]["started_at_ns"] = 2**63 - 1
        self.state.write_text(json.dumps(persisted), encoding="utf-8")
        old_stat = outside.stat()
        (self.root / "feature.txt").write_text("fixed\n", encoding="utf-8")
        outside.write_text("after!\n", encoding="utf-8")
        os.utime(outside, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        after = self.status()["live_diff_hash"]
        result_file = self.write_json(
            "ignored-metadata-result.json",
            {
                "finding_ids": ["F-IGNORED-METADATA"],
                "diff_hash_before": begun["active_fix"]["diff_hash_before"],
                "diff_hash_after": after,
                "changed_paths": ["feature.txt"],
                "executor": {"name": "direct", "mode": "frozen_batch"},
                "test_mode": "none",
                "invalidated_surfaces": ["R1"],
                "direct_validation": [
                    {"status": "pass", "evidence": "The claimed fix passed."}
                ],
            },
        )
        rejected, _ = self.run_state(
            "record-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
            expected=2,
        )
        self.assertIn("outside the frozen batch", rejected["error"])
        self.assertIn("outside.log", rejected["error"])

    def test_record_fix_rejects_ignored_deletion_outside_frozen_batch(self) -> None:
        (self.root / ".gitignore").write_text("outside.log\n", encoding="utf-8")
        self.run_git("add", ".gitignore")
        self.run_git("commit", "-m", "ignore outside log")
        (self.root / "outside.log").write_text("existing ignored data\n", encoding="utf-8")
        initialized = self.init(local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-IGNORED-DELETE")],
            failed_surface="R1",
        )
        ids = self.write_json("ignored-delete-ids.json", ["F-IGNORED-DELETE"])
        paths = self.write_json("ignored-delete-paths.json", ["feature.txt"])
        begun, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
        )
        (self.root / "feature.txt").write_text("fixed\n", encoding="utf-8")
        (self.root / "outside.log").unlink()
        after = self.status()["live_diff_hash"]
        result_file = self.write_json(
            "ignored-delete-result.json",
            {
                "finding_ids": ["F-IGNORED-DELETE"],
                "diff_hash_before": begun["active_fix"]["diff_hash_before"],
                "diff_hash_after": after,
                "changed_paths": ["feature.txt"],
                "executor": {"name": "direct", "mode": "frozen_batch"},
                "test_mode": "none",
                "invalidated_surfaces": ["R1"],
                "direct_validation": [
                    {"status": "pass", "evidence": "The claimed fix passed."}
                ],
            },
        )
        rejected, _ = self.run_state(
            "record-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
            expected=2,
        )
        self.assertIn("outside the frozen batch", rejected["error"])
        self.assertIn("outside.log", rejected["error"])

    def test_record_fix_rejects_empty_directory_outside_frozen_batch(self) -> None:
        initialized = self.init(local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-EMPTY-DIRECTORY")],
            failed_surface="R1",
        )
        ids = self.write_json("empty-directory-ids.json", ["F-EMPTY-DIRECTORY"])
        paths = self.write_json("empty-directory-paths.json", ["feature.txt"])
        begun, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
        )
        (self.root / "feature.txt").write_text("fixed\n", encoding="utf-8")
        (self.root / "outside-empty").mkdir()
        after = self.status()["live_diff_hash"]
        result_file = self.write_json(
            "empty-directory-result.json",
            {
                "finding_ids": ["F-EMPTY-DIRECTORY"],
                "diff_hash_before": begun["active_fix"]["diff_hash_before"],
                "diff_hash_after": after,
                "changed_paths": ["feature.txt"],
                "executor": {"name": "direct", "mode": "frozen_batch"},
                "test_mode": "none",
                "invalidated_surfaces": ["R1"],
                "direct_validation": [
                    {"status": "pass", "evidence": "The claimed fix passed."}
                ],
            },
        )
        rejected, _ = self.run_state(
            "record-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
            expected=2,
        )
        self.assertIn("outside the frozen batch", rejected["error"])
        self.assertIn("outside-empty", rejected["error"])

    def test_record_fix_rejects_directory_metadata_change_outside_batch(self) -> None:
        outside = self.root / "outside-directory"
        outside.mkdir(mode=0o755)
        initialized = self.init(local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-DIRECTORY-METADATA")],
            failed_surface="R1",
        )
        ids = self.write_json("directory-metadata-ids.json", ["F-DIRECTORY-METADATA"])
        paths = self.write_json("directory-metadata-paths.json", ["feature.txt"])
        begun, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
        )
        (self.root / "feature.txt").write_text("fixed\n", encoding="utf-8")
        outside.chmod(0o700)
        after = self.status()["live_diff_hash"]
        result_file = self.write_json(
            "directory-metadata-result.json",
            {
                "finding_ids": ["F-DIRECTORY-METADATA"],
                "diff_hash_before": begun["active_fix"]["diff_hash_before"],
                "diff_hash_after": after,
                "changed_paths": ["feature.txt"],
                "executor": {"name": "direct", "mode": "frozen_batch"},
                "test_mode": "none",
                "invalidated_surfaces": ["R1"],
                "direct_validation": [
                    {"status": "pass", "evidence": "The claimed fix passed."}
                ],
            },
        )
        rejected, _ = self.run_state(
            "record-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
            expected=2,
        )
        self.assertIn("outside the frozen batch", rejected["error"])
        self.assertIn("outside-directory", rejected["error"])

    def test_record_fix_rejects_authorized_path_replaced_by_symlink(self) -> None:
        outside = Path(self.temporary.name) / "outside-target.txt"
        outside.write_text("outside\n", encoding="utf-8")
        initialized = self.init(local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-SYMLINK-REPLACEMENT")],
            failed_surface="R1",
        )
        ids = self.write_json("symlink-replacement-ids.json", ["F-SYMLINK-REPLACEMENT"])
        paths = self.write_json("symlink-replacement-paths.json", ["feature.txt"])
        begun, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
        )
        feature = self.root / "feature.txt"
        feature.unlink()
        feature.symlink_to(outside)
        feature.write_text("escaped write\n", encoding="utf-8")
        after = self.status()["live_diff_hash"]
        result_file = self.write_json(
            "symlink-replacement-result.json",
            {
                "finding_ids": ["F-SYMLINK-REPLACEMENT"],
                "diff_hash_before": begun["active_fix"]["diff_hash_before"],
                "diff_hash_after": after,
                "changed_paths": ["feature.txt"],
                "executor": {"name": "direct", "mode": "frozen_batch"},
                "test_mode": "none",
                "invalidated_surfaces": ["R1"],
                "direct_validation": [
                    {"status": "pass", "evidence": "The claimed fix passed."}
                ],
            },
        )
        rejected, _ = self.run_state(
            "record-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
            expected=2,
        )
        self.assertIn("symlink endpoints", rejected["error"])

    def test_record_fix_allows_timestamp_churn_on_authorized_parent(self) -> None:
        nested = self.root / "nested"
        nested.mkdir()
        target = nested / "feature.txt"
        target.write_text("baseline nested\n", encoding="utf-8")
        self.run_git("add", "nested/feature.txt")
        self.run_git("commit", "-m", "add nested feature")
        target.write_text("buggy nested\n", encoding="utf-8")
        initialized = self.init(local_text=None)
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-AUTHORIZED-PARENT")],
            failed_surface="R1",
        )
        ids = self.write_json("authorized-parent-ids.json", ["F-AUTHORIZED-PARENT"])
        paths = self.write_json("authorized-parent-paths.json", ["nested/feature.txt"])
        begun, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
        )
        replacement = nested / ".feature.tmp"
        replacement.write_text("fixed nested\n", encoding="utf-8")
        replacement.replace(target)
        after = self.status()["live_diff_hash"]
        result_file = self.write_json(
            "authorized-parent-result.json",
            {
                "finding_ids": ["F-AUTHORIZED-PARENT"],
                "diff_hash_before": begun["active_fix"]["diff_hash_before"],
                "diff_hash_after": after,
                "changed_paths": ["nested/feature.txt"],
                "executor": {"name": "direct", "mode": "frozen_batch"},
                "test_mode": "none",
                "invalidated_surfaces": ["R1"],
                "direct_validation": [
                    {"status": "pass", "evidence": "The claimed fix passed."}
                ],
            },
        )
        recorded, _ = self.run_state(
            "record-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
        )
        self.assertEqual(recorded["phase"], "impact_review")

    def test_record_fix_rejects_deleted_git_control_file(self) -> None:
        initialized = self.init(local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-GIT-CONTROL-DELETE")],
            failed_surface="R1",
        )
        ids = self.write_json("git-control-delete-ids.json", ["F-GIT-CONTROL-DELETE"])
        paths = self.write_json("git-control-delete-paths.json", ["feature.txt"])
        begun, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
        )
        (self.root / "feature.txt").write_text("fixed\n", encoding="utf-8")
        (self.root / ".git" / "config").unlink()
        after = self.status()["live_diff_hash"]
        result_file = self.write_json(
            "git-control-delete-result.json",
            {
                "finding_ids": ["F-GIT-CONTROL-DELETE"],
                "diff_hash_before": begun["active_fix"]["diff_hash_before"],
                "diff_hash_after": after,
                "changed_paths": ["feature.txt"],
                "executor": {"name": "direct", "mode": "frozen_batch"},
                "test_mode": "none",
                "invalidated_surfaces": ["R1"],
                "direct_validation": [
                    {"status": "pass", "evidence": "The claimed fix passed."}
                ],
            },
        )
        rejected, _ = self.run_state(
            "record-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
            expected=2,
        )
        self.assertIn("outside the frozen batch", rejected["error"])
        self.assertIn("<git-dir>/config", rejected["error"])

    def test_git_control_manifest_prunes_object_store(self) -> None:
        object_leaf = self.root / ".git" / "objects" / "aa"
        object_leaf.mkdir(parents=True, exist_ok=True)
        (object_leaf / "sentinel").write_text("loose object\n", encoding="utf-8")
        spec = importlib.util.spec_from_file_location("delivery_state_under_test", SCRIPT)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        real_walk = module.os.walk
        visited: list[Path] = []

        def observed_walk(*args: Any, **kwargs: Any) -> Any:
            for directory, dirnames, filenames in real_walk(*args, **kwargs):
                visited.append(Path(directory).resolve())
                yield directory, dirnames, filenames

        with mock.patch.object(module.os, "walk", side_effect=observed_walk):
            module.git_control_manifest(self.root)

        objects = (self.root / ".git" / "objects").resolve()
        self.assertFalse(
            any(path == objects or objects in path.parents for path in visited),
            msg=f"visited object-store paths: {visited}",
        )

    def test_record_fix_rejects_persistent_git_index_lock(self) -> None:
        initialized = self.init(local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-INDEX-LOCK")],
            failed_surface="R1",
        )
        ids = self.write_json("index-lock-ids.json", ["F-INDEX-LOCK"])
        paths = self.write_json("index-lock-paths.json", ["feature.txt"])
        begun, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
        )
        (self.root / "feature.txt").write_text("fixed\n", encoding="utf-8")
        after = self.status()["live_diff_hash"]
        (self.root / ".git" / "index.lock").write_text("persistent lock\n", encoding="utf-8")
        result_file = self.write_json(
            "index-lock-result.json",
            {
                "finding_ids": ["F-INDEX-LOCK"],
                "diff_hash_before": begun["active_fix"]["diff_hash_before"],
                "diff_hash_after": after,
                "changed_paths": ["feature.txt"],
                "executor": {"name": "direct", "mode": "frozen_batch"},
                "test_mode": "none",
                "invalidated_surfaces": ["R1"],
                "direct_validation": [
                    {"status": "pass", "evidence": "The claimed fix passed."}
                ],
            },
        )
        rejected, _ = self.run_state(
            "record-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
            expected=2,
        )
        self.assertIn("outside the frozen batch", rejected["error"])
        self.assertIn("<git-dir>/index.lock", rejected["error"])

    def test_record_fix_rejects_linked_worktree_common_git_control_change(self) -> None:
        main_root = self.root
        linked_root = Path(self.temporary.name) / "linked"
        self.run_git("worktree", "add", "-b", "linked-test", str(linked_root))
        self.root = linked_root
        initialized = self.init(local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-COMMON-GIT-CONTROL")],
            failed_surface="R1",
        )
        ids = self.write_json("common-git-control-ids.json", ["F-COMMON-GIT-CONTROL"])
        paths = self.write_json("common-git-control-paths.json", ["feature.txt"])
        begun, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
        )
        (self.root / "feature.txt").write_text("fixed\n", encoding="utf-8")
        common_config = main_root / ".git" / "config"
        common_config.write_text(
            common_config.read_text(encoding="utf-8") + "\n# out-of-batch change\n",
            encoding="utf-8",
        )
        after = self.status()["live_diff_hash"]
        result_file = self.write_json(
            "common-git-control-result.json",
            {
                "finding_ids": ["F-COMMON-GIT-CONTROL"],
                "diff_hash_before": begun["active_fix"]["diff_hash_before"],
                "diff_hash_after": after,
                "changed_paths": ["feature.txt"],
                "executor": {"name": "direct", "mode": "frozen_batch"},
                "test_mode": "none",
                "invalidated_surfaces": ["R1"],
                "direct_validation": [
                    {"status": "pass", "evidence": "The claimed fix passed."}
                ],
            },
        )
        rejected, _ = self.run_state(
            "record-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
            expected=2,
        )
        self.assertIn("outside the frozen batch", rejected["error"])
        self.assertIn("<git-common-dir>/config", rejected["error"])

    def test_begin_fix_rejects_unreviewed_diff_drift(self) -> None:
        initialized = self.init(local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-DRIFT")],
            failed_surface="R1",
        )
        self.modify("unreviewed drift\n")
        ids = self.write_json("drift-ids.json", ["F-DRIFT"])
        paths = self.write_json("drift-paths.json", ["feature.txt"])
        rejected, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
            expected=2,
        )
        self.assertIn("changed after the latest recorded evidence", rejected["error"])

    def test_impact_review_rejects_drift_after_recorded_fix(self) -> None:
        initialized = self.init(local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-IMPACT-DRIFT")],
            failed_surface="R1",
        )
        self.begin_and_record_fix("F-IMPACT-DRIFT", "recorded fix\n")
        drifted_hash = self.modify("manual drift after fix\n")
        rejected = self.record_impact(drifted_hash, expected=2)
        self.assertIn("changed after the recorded fix", rejected["error"])

    def test_record_fix_rejects_actual_change_outside_frozen_batch(self) -> None:
        (self.root / "other.txt").write_text("initial local change\n", encoding="utf-8")
        initialized = self.init(local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-FROZEN")],
            failed_surface="R1",
        )
        ids = self.write_json("frozen-ids.json", ["F-FROZEN"])
        paths = self.write_json("frozen-paths.json", ["feature.txt"])
        begun, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
        )
        (self.root / "other.txt").write_text(
            "changed outside frozen batch\n",
            encoding="utf-8",
        )
        after = self.status()["live_diff_hash"]
        result_file = self.write_json(
            "frozen-result.json",
            {
                "finding_ids": ["F-FROZEN"],
                "diff_hash_before": begun["active_fix"]["diff_hash_before"],
                "diff_hash_after": after,
                "changed_paths": ["feature.txt"],
                "executor": {"name": "direct", "mode": "frozen_batch"},
                "test_mode": "none",
                "invalidated_surfaces": ["R1"],
                "direct_validation": [
                    {"status": "pass", "evidence": "Claimed focused pass."}
                ],
            },
        )
        rejected, _ = self.run_state(
            "record-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
            expected=2,
        )
        self.assertIn("actual fix delta", rejected["error"])
        self.assertIn("other.txt", rejected["error"])

    def test_record_fix_rejects_index_change_outside_frozen_batch(self) -> None:
        (self.root / "other.txt").write_text("unchanged local content\n", encoding="utf-8")
        initialized = self.init(local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-INDEX-FROZEN")],
            failed_surface="R1",
        )
        ids = self.write_json("index-frozen-ids.json", ["F-INDEX-FROZEN"])
        paths = self.write_json("index-frozen-paths.json", ["feature.txt"])
        begun, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
        )
        self.run_git("add", "other.txt")
        after = self.status()["live_diff_hash"]
        result_file = self.write_json(
            "index-frozen-result.json",
            {
                "finding_ids": ["F-INDEX-FROZEN"],
                "diff_hash_before": begun["active_fix"]["diff_hash_before"],
                "diff_hash_after": after,
                "changed_paths": ["feature.txt"],
                "executor": {"name": "direct", "mode": "frozen_batch"},
                "test_mode": "none",
                "invalidated_surfaces": ["R1"],
                "direct_validation": [
                    {"status": "pass", "evidence": "Claimed focused pass."}
                ],
            },
        )
        rejected, _ = self.run_state(
            "record-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
            expected=2,
        )
        self.assertIn("actual fix delta", rejected["error"])
        self.assertIn("other.txt", rejected["error"])

    def test_failed_validation_without_finding_pauses(self) -> None:
        initialized = self.init(local_text="implementation\n")
        diff_hash = initialized["live_diff_hash"]
        self.record_initial(diff_hash)
        result_file = self.write_json(
            "failed-validation.json",
            {
                "diff_hash": diff_hash,
                "validations": [
                    {"id": "V1", "status": "fail", "evidence": "One direct test failed."}
                ],
                "acceptance": [],
                "verified_findings": [],
                "new_findings": [],
            },
        )
        result, _ = self.run_state(
            "record-validation",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
            expected=3,
        )
        self.assertEqual(result["phase"], "validation_failed")
        self.assertEqual(
            result["reason"], "each_failed_item_requires_its_own_blocking_finding"
        )

    def test_design_review_resume_preserves_fix_count(self) -> None:
        initialized = self.init(
            self.requirements(threshold=1),
            local_text="buggy\n",
        )
        initial_hash = initialized["live_diff_hash"]
        self.record_initial(
            initial_hash,
            [self.finding("F1")],
            failed_surface="R1",
        )
        fixed_hash = self.begin_and_record_fix("F1", "first fix\n")
        self.record_impact(
            fixed_hash,
            [self.finding("F2", root_cause="new root cause")],
            status="failed",
            expected=3,
        )
        design_file = self.write_json(
            "design-review.json",
            {
                "decision": "resume",
                "diagnosis": "The implementation has two competing authorities.",
                "strategy": "Use one authoritative state and rewrite the affected evaluator.",
                "invalidated_surfaces": ["R1"],
                "added_acceptance": [
                    {"id": "A2", "description": "Only authoritative state is accepted."}
                ],
                "added_invariants": [
                    {"id": "I2", "description": "There is one serviceability authority."}
                ],
            },
        )
        result, _ = self.run_state(
            "resolve-design-review",
            "--state",
            str(self.state),
            "--result-file",
            str(design_file),
        )
        self.assertEqual(result["phase"], "remediation")
        self.assertEqual(result["fix_batch_count"], 1)
        self.assertEqual(result["epoch_fix_batch_count"], 0)
        self.assertEqual(result["design_epoch"], 2)
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(persisted["design_review_count"], 1)
        self.assertIn("A2", persisted["acceptance"])
        self.assertIn("I2", persisted["invariants"])
        second_hash = self.begin_and_record_fix("F2", "second design fix\n")
        impact = self.record_impact(second_hash)
        self.assertEqual(impact["phase"], "validation")
        self.assertEqual(impact["design_epoch"], 2)

    def test_design_review_resolution_rejects_paused_diff_drift(self) -> None:
        initialized = self.init(
            self.requirements(threshold=1),
            local_text="buggy\n",
        )
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-DESIGN-1")],
            failed_surface="R1",
        )
        fixed_hash = self.begin_and_record_fix("F-DESIGN-1", "first fix\n")
        self.record_impact(
            fixed_hash,
            [self.finding("F-DESIGN-2", root_cause="new root cause")],
            status="failed",
            expected=3,
        )
        self.modify("changed while design review was paused\n")
        design_file = self.write_json(
            "drifted-design-review.json",
            {
                "decision": "resume",
                "diagnosis": "The implementation needs one authority.",
                "strategy": "Use the database as the authority.",
                "invalidated_surfaces": ["R1"],
            },
        )
        rejected, _ = self.run_state(
            "resolve-design-review",
            "--state",
            str(self.state),
            "--result-file",
            str(design_file),
            expected=2,
        )
        self.assertIn("design review was paused", rejected["error"])

    def test_design_review_blocked_records_exit_reason(self) -> None:
        initialized = self.init(
            self.requirements(threshold=1),
            local_text="buggy\n",
        )
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F1")],
            failed_surface="R1",
        )
        fixed_hash = self.begin_and_record_fix("F1", "first fix\n")
        self.record_impact(
            fixed_hash,
            [self.finding("F2", root_cause="new root cause")],
            status="failed",
            expected=3,
        )
        design_file = self.write_json(
            "blocked-design-review.json",
            {
                "decision": "blocked",
                "diagnosis": "The required authority is unavailable.",
                "strategy": "Pause until the authority is established.",
            },
        )
        result, _ = self.run_state(
            "resolve-design-review",
            "--state",
            str(self.state),
            "--result-file",
            str(design_file),
            expected=3,
        )
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["phase"], "blocked")
        self.assertEqual(result["exit_reason"], "design_review_blocked")

    def test_blocked_design_review_can_resume_same_state(self) -> None:
        initialized = self.init(
            self.requirements(threshold=1),
            local_text="buggy\n",
        )
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-BLOCKED-RESUME")],
            failed_surface="R1",
        )
        fixed_hash = self.begin_and_record_fix(
            "F-BLOCKED-RESUME",
            "first fix\n",
        )
        self.record_impact(
            fixed_hash,
            [self.finding("F-BLOCKER", root_cause="new root cause")],
            status="failed",
            expected=3,
        )
        blocked_file = self.write_json(
            "blocked-then-resume.json",
            {
                "decision": "blocked",
                "diagnosis": "An external dependency is unavailable.",
                "strategy": "Resume after the dependency is restored.",
            },
        )
        self.run_state(
            "resolve-design-review",
            "--state",
            str(self.state),
            "--result-file",
            str(blocked_file),
            expected=3,
        )
        resume_file = self.write_json(
            "resume-blocked-design-review.json",
            {
                "decision": "resume",
                "diagnosis": "The external dependency is now available.",
                "strategy": "Continue with the corrected implementation authority.",
                "invalidated_surfaces": ["R1"],
            },
        )
        resumed, _ = self.run_state(
            "resolve-design-review",
            "--state",
            str(self.state),
            "--result-file",
            str(resume_file),
        )
        self.assertEqual(resumed["status"], "active")
        self.assertEqual(resumed["phase"], "remediation")
        self.assertIsNone(resumed["exit_reason"])

    def test_initial_and_impact_failures_share_one_design_epoch(self) -> None:
        requirements = self.requirements(threshold=50)
        requirements["design_review_thresholds"]["surface_failures"] = 2
        initialized = self.init(requirements, local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-EPOCH-FAIL-1")],
            failed_surface="R1",
        )
        fixed_hash = self.begin_and_record_fix("F-EPOCH-FAIL-1", "first fix\n")
        stopped = self.record_impact(
            fixed_hash,
            [self.finding("F-EPOCH-FAIL-2", root_cause="second root cause")],
            status="failed",
            expected=3,
        )
        self.assertEqual(stopped["phase"], "design_review_required")
        self.assertTrue(stopped["repeated_surface"])
        stored = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(
            stored["review_surfaces"]["R1"]["epoch_failure_count"],
            2,
        )

    def test_design_review_invalidation_updates_lifetime_history(self) -> None:
        requirements = self.requirements(threshold=50)
        requirements["design_review_thresholds"]["root_cause_recurrences"] = 1
        initialized = self.init(requirements, local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-DESIGN-INVALIDATE")],
            failed_surface="R1",
        )
        fixed_hash = self.begin_and_record_fix("F-DESIGN-INVALIDATE", "fixed\n")
        self.record_impact(fixed_hash)
        self.record_full_validation(fixed_hash, verified=["F-DESIGN-INVALIDATE"])
        repeated = self.finding("F-DESIGN-INVALIDATE-AGAIN", source="external-review")
        repeated["trigger"] = "Trigger F-DESIGN-INVALIDATE"
        finding_file = self.write_json(
            "design-invalidation-finding.json",
            {"diff_hash": fixed_hash, "findings": [repeated]},
        )
        self.run_state(
            "add-findings",
            "--state",
            str(self.state),
            "--result-file",
            str(finding_file),
            expected=3,
        )
        before = json.loads(self.state.read_text(encoding="utf-8"))
        lifetime_before = before["review_surfaces"]["R1"]["invalidation_count"]
        design_file = self.write_json(
            "lifetime-design-resolution.json",
            {
                "decision": "resume",
                "diagnosis": "The lifecycle needs one coherent transition model.",
                "strategy": "Apply the unified transition model.",
                "invalidated_surfaces": ["R1"],
            },
        )
        self.run_state(
            "resolve-design-review",
            "--state",
            str(self.state),
            "--result-file",
            str(design_file),
        )
        stored = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(
            stored["review_surfaces"]["R1"]["invalidation_count"],
            lifetime_before + 1,
        )
        self.assertEqual(
            stored["review_surfaces"]["R1"]["epoch_invalidation_count"],
            0,
        )

    def test_non_local_delivery_target_is_rejected(self) -> None:
        requirements = self.requirements(delivery_target="pr")
        (self.root / "feature.txt").write_text("implementation\n", encoding="utf-8")
        requirements_file = self.write_json("pr-target.json", requirements)
        result, _ = self.run_state(
            "init",
            "--state",
            str(self.state),
            "--project-root",
            str(self.root),
            "--requirements-file",
            str(requirements_file),
            expected=2,
        )
        self.assertIn("only produces a READY local diff", result["error"])

    def test_independent_follow_up_after_ready_preserves_ready(self) -> None:
        diff_hash = self.make_ready_without_findings()
        result_file = self.write_json(
            "ready-follow-up.json",
            {
                "diff_hash": diff_hash,
                "findings": [
                    self.finding(
                        "FU1",
                        classification="independent_follow_up",
                        severity="P3",
                    )
                ],
            },
        )
        result, _ = self.run_state(
            "add-findings",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
        )
        self.assertTrue(result["ready"])
        self.assertEqual(result["phase"], "ready_local_diff")
        self.assertEqual(result["follow_up_requirements"], ["FU1"])
        self.assertEqual(result["review_surfaces"]["R1"], "clean")

    def test_repeated_verified_finding_reopens_existing_ledger_entry(self) -> None:
        initialized = self.init(local_text="buggy\n")
        initial_hash = initialized["live_diff_hash"]
        self.record_initial(
            initial_hash,
            [self.finding("F1")],
            failed_surface="R1",
        )
        fixed_hash = self.begin_and_record_fix("F1", "fixed\n")
        self.record_impact(fixed_hash)
        self.record_full_validation(fixed_hash, verified=["F1"])
        repeated = self.finding("F1-AGAIN", source="validation")
        repeated["trigger"] = "Trigger F1"
        result_file = self.write_json(
            "repeated-finding.json",
            {"diff_hash": fixed_hash, "findings": [repeated]},
        )
        result, _ = self.run_state(
            "add-findings",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
        )
        self.assertIn("F1", result["open_findings"])
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertNotIn("F1-AGAIN", persisted["findings"])
        self.assertEqual(persisted["findings"]["F1"]["recurrence_count"], 1)

    def test_rediscovered_false_positive_reopens_existing_finding(self) -> None:
        initialized = self.init(local_text="suspected bug\n")
        diff_hash = initialized["live_diff_hash"]
        self.record_initial(
            diff_hash,
            [self.finding("F-REOPEN-FP")],
            failed_surface="R1",
        )
        ids = self.write_json("reopen-fp-ids.json", ["F-REOPEN-FP"])
        paths = self.write_json("reopen-fp-paths.json", ["feature.txt"])
        self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
        )
        cancellation = self.write_json(
            "reopen-fp-cancellation.json",
            {
                "finding_resolutions": [
                    {
                        "id": "F-REOPEN-FP",
                        "disposition": "false_positive",
                        "evidence": "The first reproducer did not fail.",
                    }
                ]
            },
        )
        self.run_state(
            "cancel-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(cancellation),
        )
        self.record_impact(diff_hash)
        self.record_full_validation(diff_hash)
        self.run_state("evaluate-ready", "--state", str(self.state))

        rediscovered = self.finding("F-REOPEN-FP-AGAIN", source="external-review")
        rediscovered["trigger"] = "Trigger F-REOPEN-FP"
        finding_file = self.write_json(
            "rediscovered-false-positive.json",
            {"diff_hash": diff_hash, "findings": [rediscovered]},
        )
        reopened, _ = self.run_state(
            "add-findings",
            "--state",
            str(self.state),
            "--result-file",
            str(finding_file),
        )
        self.assertFalse(reopened["ready"])
        self.assertIn("F-REOPEN-FP", reopened["open_findings"])
        self.assertEqual(reopened["review_surfaces"]["R1"], "invalidated")

    def test_add_findings_enforces_root_cause_convergence_gate(self) -> None:
        requirements = self.requirements(threshold=50)
        requirements["design_review_thresholds"]["root_cause_recurrences"] = 1
        initialized = self.init(requirements, local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-ADD-GATE")],
            failed_surface="R1",
        )
        fixed_hash = self.begin_and_record_fix("F-ADD-GATE", "fixed\n")
        self.record_impact(fixed_hash)
        self.record_full_validation(fixed_hash, verified=["F-ADD-GATE"])
        repeated = self.finding("F-ADD-GATE-AGAIN", source="external-review")
        repeated["trigger"] = "Trigger F-ADD-GATE"
        finding_file = self.write_json(
            "add-findings-convergence.json",
            {"diff_hash": fixed_hash, "findings": [repeated]},
        )
        stopped, _ = self.run_state(
            "add-findings",
            "--state",
            str(self.state),
            "--result-file",
            str(finding_file),
            expected=3,
        )
        self.assertEqual(stopped["phase"], "design_review_required")
        self.assertEqual(
            stopped["reason"],
            "added_findings_reached_root_cause_threshold",
        )

    def test_add_findings_rejects_an_active_fix_batch(self) -> None:
        initialized = self.init(local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-ACTIVE-BATCH")],
            failed_surface="R1",
        )
        ids = self.write_json("active-batch-ids.json", ["F-ACTIVE-BATCH"])
        paths = self.write_json("active-batch-paths.json", ["feature.txt"])
        self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
        )
        finding_file = self.write_json(
            "active-batch-findings.json",
            {
                "diff_hash": initialized["live_diff_hash"],
                "findings": [
                    self.finding("F-DURING-ACTIVE-BATCH", source="external-review")
                ],
            },
        )
        rejected, _ = self.run_state(
            "add-findings",
            "--state",
            str(self.state),
            "--result-file",
            str(finding_file),
            expected=2,
        )
        self.assertIn("active fix batch", rejected["error"])
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIsNotNone(persisted["active_fix"])
        self.assertNotIn("F-DURING-ACTIVE-BATCH", persisted["findings"])

    def test_rediscovery_can_reuse_verified_finding_id(self) -> None:
        initialized = self.init(local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-STABLE-ID")],
            failed_surface="R1",
        )
        fixed_hash = self.begin_and_record_fix("F-STABLE-ID", "fixed\n")
        self.record_impact(fixed_hash)
        self.record_full_validation(fixed_hash, verified=["F-STABLE-ID"])
        rediscovered = self.finding("F-STABLE-ID", source="external-review")
        finding_file = self.write_json(
            "stable-id-rediscovery.json",
            {"diff_hash": fixed_hash, "findings": [rediscovered]},
        )
        reopened, _ = self.run_state(
            "add-findings",
            "--state",
            str(self.state),
            "--result-file",
            str(finding_file),
        )
        self.assertIn("F-STABLE-ID", reopened["remediable_findings"])
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(persisted["findings"]["F-STABLE-ID"]["status"], "open")
        self.assertEqual(len(persisted["findings"]), 1)

    def test_deferred_convergence_gate_runs_after_decision_resolution(self) -> None:
        requirements = self.requirements(threshold=50)
        requirements["design_review_thresholds"]["root_cause_recurrences"] = 1
        requirements["unresolved_decisions"] = ["Choose the lifecycle authority."]
        (self.root / "feature.txt").write_text("buggy\n", encoding="utf-8")
        requirements_file = self.write_json("deferred-gate-requirements.json", requirements)
        initialized, _ = self.run_state(
            "init",
            "--state",
            str(self.state),
            "--project-root",
            str(self.root),
            "--requirements-file",
            str(requirements_file),
            expected=3,
        )
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        finding_file = self.write_json(
            "deferred-gate-findings.json",
            {
                "diff_hash": persisted["current_diff_hash"],
                "findings": [
                    self.finding("F-DEFER-1"),
                    self.finding("F-DEFER-2"),
                ],
            },
        )
        preserved, _ = self.run_state(
            "add-findings",
            "--state",
            str(self.state),
            "--result-file",
            str(finding_file),
        )
        self.assertEqual(preserved["phase"], "decision_required")
        resolution_file = self.write_json(
            "deferred-gate-resolution.json",
            {
                "resolved_decisions": [
                    {
                        "decision": "Choose the lifecycle authority.",
                        "resolution": "Use the persisted lifecycle state.",
                        "evidence": "The requirement owner confirmed the authority.",
                    }
                ],
                "finding_resolutions": [],
            },
        )
        stopped, _ = self.run_state(
            "resolve-decision",
            "--state",
            str(self.state),
            "--result-file",
            str(resolution_file),
            expected=3,
        )
        self.assertEqual(stopped["phase"], "design_review_required")
        self.assertEqual(
            stopped["reason"],
            "deferred_design_review_after_hard_gate",
        )
        self.assertEqual(stopped["cleared_gate"], "decision_required")

    def test_validation_findings_enforce_root_cause_convergence_gate(self) -> None:
        requirements = self.requirements(threshold=50)
        requirements["design_review_thresholds"]["root_cause_recurrences"] = 1
        initialized = self.init(requirements, local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-VALIDATION-GATE")],
            failed_surface="R1",
        )
        fixed_hash = self.begin_and_record_fix("F-VALIDATION-GATE", "fixed\n")
        self.record_impact(fixed_hash)
        repeated = self.finding("F-VALIDATION-GATE-AGAIN", source="validation")
        repeated["trigger"] = "Trigger F-VALIDATION-GATE"
        validation_file = self.write_json(
            "validation-convergence.json",
            {
                "diff_hash": fixed_hash,
                "validations": [
                    {"id": "V1", "status": "pass", "evidence": "Tests passed."},
                    {"id": "V2", "status": "pass", "evidence": "Diff passed."},
                ],
                "acceptance": [
                    {"id": "A1", "status": "pass", "evidence": "Behavior passed."}
                ],
                "verified_findings": ["F-VALIDATION-GATE"],
                "finding_evidence": {
                    "F-VALIDATION-GATE": "The original regression test passed."
                },
                "new_findings": [repeated],
            },
        )
        stopped, _ = self.run_state(
            "record-validation",
            "--state",
            str(self.state),
            "--result-file",
            str(validation_file),
            expected=3,
        )
        self.assertEqual(stopped["phase"], "design_review_required")
        self.assertEqual(
            stopped["reason"],
            "validation_findings_reached_root_cause_threshold",
        )

    def test_same_root_cause_different_symptom_requires_design_review(self) -> None:
        requirements = self.requirements(threshold=50)
        requirements["design_review_thresholds"]["root_cause_recurrences"] = 1
        initialized = self.init(requirements, local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-ROOT-1")],
            failed_surface="R1",
        )
        fixed_hash = self.begin_and_record_fix("F-ROOT-1", "first root fix\n")
        self.record_impact(fixed_hash)
        self.record_full_validation(fixed_hash, verified=["F-ROOT-1"])

        repeated = self.finding("F-ROOT-2", source="external-review")
        repeated["trigger"] = "A different symptom from the same root cause."
        repeated["location"] = "different-location.py"
        result_file = self.write_json(
            "same-root-different-symptom.json",
            {"diff_hash": fixed_hash, "findings": [repeated]},
        )
        stopped, _ = self.run_state(
            "add-findings",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
            expected=3,
        )
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        root_stat = next(iter(persisted["root_cause_stats"].values()))
        self.assertEqual(root_stat["epoch_recurrence_count"], 1)
        self.assertEqual(stopped["phase"], "design_review_required")
        self.assertEqual(
            stopped["reason"],
            "added_findings_reached_root_cause_threshold",
        )

    def test_initial_review_enforces_root_cause_convergence_gate(self) -> None:
        requirements = self.requirements(threshold=50)
        requirements["design_review_thresholds"]["root_cause_recurrences"] = 1
        initialized = self.init(requirements, local_text="two related bugs\n")
        stopped = self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-INITIAL-ROOT-1"), self.finding("F-INITIAL-ROOT-2")],
            failed_surface="R1",
            expected=3,
        )
        self.assertEqual(stopped["phase"], "design_review_required")
        self.assertEqual(stopped["reason"], "review_not_converging")
        self.assertTrue(stopped["repeated_root_cause"])

    def test_review_decision_precedes_deferred_convergence_gate(self) -> None:
        requirements = self.requirements(threshold=50)
        requirements["design_review_thresholds"]["root_cause_recurrences"] = 1
        initialized = self.init(requirements, local_text="ambiguous related bugs\n")
        stopped = self.record_initial(
            initialized["live_diff_hash"],
            [
                self.finding("F-REVIEW-ROOT-1"),
                self.finding("F-REVIEW-ROOT-2"),
                self.finding(
                    "D-REVIEW-ROOT",
                    classification="decision_required",
                    root_cause="authority is undecided",
                ),
            ],
            failed_surface="R1",
            expected=3,
        )
        self.assertEqual(stopped["phase"], "decision_required")
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(
            persisted["deferred_design_review"]["reasons"],
            ["review_not_converging"],
        )
        resolution_file = self.write_json(
            "review-decision-before-convergence.json",
            {
                "resolved_decisions": [],
                "finding_resolutions": [
                    {
                        "id": "D-REVIEW-ROOT",
                        "classification": "independent_follow_up",
                        "evidence": "The authority decision is outside this delivery.",
                    }
                ],
            },
        )
        resumed, _ = self.run_state(
            "resolve-decision",
            "--state",
            str(self.state),
            "--result-file",
            str(resolution_file),
            expected=3,
        )
        self.assertEqual(resumed["phase"], "design_review_required")
        self.assertEqual(
            resumed["reason"],
            "deferred_design_review_after_hard_gate",
        )

    def test_begin_fix_groups_normalized_root_cause_identity(self) -> None:
        initialized = self.init(local_text="two related bugs\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [
                self.finding("F-NORMALIZED-1", root_cause="Shared   Cause"),
                self.finding("F-NORMALIZED-2", root_cause=" shared cause "),
            ],
            failed_surface="R1",
        )
        ids = self.write_json(
            "normalized-root-ids.json",
            ["F-NORMALIZED-1", "F-NORMALIZED-2"],
        )
        paths = self.write_json("normalized-root-paths.json", ["feature.txt"])
        begun, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
        )
        self.assertEqual(begun["active_fix"]["root_cause"], "shared cause")
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        keys = {
            persisted["findings"][finding_id]["root_cause_key"]
            for finding_id in ["F-NORMALIZED-1", "F-NORMALIZED-2"]
        }
        self.assertEqual(keys, {begun["active_fix"]["root_cause_key"]})

    def test_later_fix_invalidates_previous_finding_verification(self) -> None:
        initialized = self.init(local_text="first bug\n")
        first_hash = initialized["live_diff_hash"]
        self.record_initial(
            first_hash,
            [self.finding("F-STALE-1", root_cause="first root cause")],
            failed_surface="R1",
        )
        first_fixed_hash = self.begin_and_record_fix(
            "F-STALE-1",
            "first bug fixed\n",
        )
        self.record_impact(first_fixed_hash)
        self.record_full_validation(first_fixed_hash, verified=["F-STALE-1"])

        second_finding_file = self.write_json(
            "later-finding.json",
            {
                "diff_hash": first_fixed_hash,
                "findings": [
                    self.finding(
                        "F-STALE-2",
                        root_cause="second root cause",
                        source="external-review",
                    )
                ],
            },
        )
        self.run_state(
            "add-findings",
            "--state",
            str(self.state),
            "--result-file",
            str(second_finding_file),
        )
        second_fixed_hash = self.begin_and_record_fix(
            "F-STALE-2",
            "both bugs fixed\n",
        )
        after_second_fix = self.status()
        self.assertEqual(
            after_second_fix["verification_pending_findings"],
            ["F-STALE-1", "F-STALE-2"],
        )
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(persisted["findings"]["F-STALE-1"]["status"], "fixed_unverified")
        self.assertIsNone(
            persisted["findings"]["F-STALE-1"]["verified_diff_hash"]
        )
        self.assertEqual(
            persisted["derived_requirements"]["REQ-F-STALE-1"]["status"],
            "fixed_unverified",
        )

        self.record_impact(second_fixed_hash)
        self.record_full_validation(
            second_fixed_hash,
            verified=["F-STALE-1", "F-STALE-2"],
        )
        ready, _ = self.run_state("evaluate-ready", "--state", str(self.state))
        self.assertTrue(ready["ready_local_diff"])

    def test_later_fix_requires_fresh_false_positive_evidence(self) -> None:
        initialized = self.init(local_text="implementation with one bug\n")
        false_positive = self.finding(
            "F-STALE-FP",
            classification="false_positive",
            root_cause="suspected but valid behavior",
            surface="R2",
        )
        self.record_initial(
            initialized["live_diff_hash"],
            [
                self.finding("F-REAL", root_cause="real bug", surface="R1"),
                false_positive,
            ],
            failed_surface="R1",
        )
        ids = self.write_json("false-positive-fix-ids.json", ["F-REAL"])
        paths = self.write_json("false-positive-fix-paths.json", ["feature.txt"])
        begun, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
        )
        after = self.modify("fixed implementation\n")
        result_file = self.write_json(
            "false-positive-fix-result.json",
            {
                "finding_ids": ["F-REAL"],
                "diff_hash_before": begun["active_fix"]["diff_hash_before"],
                "diff_hash_after": after,
                "changed_paths": ["feature.txt"],
                "executor": {"name": "direct", "mode": "frozen_batch"},
                "test_mode": "none",
                "invalidated_surfaces": ["R1"],
                "direct_validation": [
                    {"status": "pass", "evidence": "The real bug no longer reproduces."}
                ],
            },
        )
        stopped, _ = self.run_state(
            "record-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
            expected=3,
        )
        self.assertEqual(stopped["phase"], "decision_required")
        self.assertEqual(
            stopped["reason"],
            "later_fix_invalidated_false_positive_evidence",
        )
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(
            persisted["findings"]["F-STALE-FP"]["status"],
            "decision_required",
        )
        self.assertIsNone(
            persisted["findings"]["F-STALE-FP"]["evidence_diff_hash"]
        )

        resolution_file = self.write_json(
            "fresh-false-positive-evidence.json",
            {
                "resolved_decisions": [],
                "finding_resolutions": [
                    {
                        "id": "F-STALE-FP",
                        "classification": "false_positive",
                        "evidence": "Rechecked against the new diff; behavior remains valid.",
                    }
                ],
            },
        )
        resumed, _ = self.run_state(
            "resolve-decision",
            "--state",
            str(self.state),
            "--result-file",
            str(resolution_file),
        )
        self.assertEqual(resumed["status"], "active")
        self.assertEqual(resumed["phase"], "impact_review")
        stored = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(
            stored["findings"]["F-STALE-FP"]["evidence_diff_hash"],
            after,
        )

    def test_false_positive_decision_preserves_invalidation_convergence_gate(self) -> None:
        requirements = self.requirements(threshold=50)
        requirements["design_review_thresholds"]["surface_invalidations"] = 1
        initialized = self.init(requirements, local_text="implementation with one bug\n")
        false_positive = self.finding(
            "F-DEFERRED-FP",
            classification="false_positive",
            root_cause="suspected but valid behavior",
            surface="R2",
        )
        self.record_initial(
            initialized["live_diff_hash"],
            [
                self.finding("F-DEFERRED-REAL", root_cause="real bug", surface="R1"),
                false_positive,
            ],
            failed_surface="R1",
        )
        ids = self.write_json("deferred-fp-fix-ids.json", ["F-DEFERRED-REAL"])
        paths = self.write_json("deferred-fp-fix-paths.json", ["feature.txt"])
        begun, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
        )
        after = self.modify("fixed implementation\n")
        result_file = self.write_json(
            "deferred-fp-fix-result.json",
            {
                "finding_ids": ["F-DEFERRED-REAL"],
                "diff_hash_before": begun["active_fix"]["diff_hash_before"],
                "diff_hash_after": after,
                "changed_paths": ["feature.txt"],
                "executor": {"name": "direct", "mode": "frozen_batch"},
                "test_mode": "none",
                "invalidated_surfaces": ["R1"],
                "direct_validation": [
                    {"status": "pass", "evidence": "The real bug no longer reproduces."}
                ],
            },
        )
        paused, _ = self.run_state(
            "record-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
            expected=3,
        )
        self.assertEqual(paused["phase"], "decision_required")
        resolution_file = self.write_json(
            "deferred-fp-resolution.json",
            {
                "resolved_decisions": [],
                "finding_resolutions": [
                    {
                        "id": "F-DEFERRED-FP",
                        "classification": "false_positive",
                        "evidence": "Fresh evidence confirms the behavior remains valid.",
                    }
                ],
            },
        )
        stopped, _ = self.run_state(
            "resolve-decision",
            "--state",
            str(self.state),
            "--result-file",
            str(resolution_file),
            expected=3,
        )
        self.assertEqual(stopped["phase"], "design_review_required")
        self.assertEqual(
            stopped["reason"],
            "deferred_design_review_after_hard_gate",
        )

    def test_fix_with_failed_direct_validation_is_not_recorded(self) -> None:
        initialized = self.init(local_text="buggy\n")
        initial_hash = initialized["live_diff_hash"]
        self.record_initial(
            initial_hash,
            [self.finding("F1")],
            failed_surface="R1",
        )
        ids = self.write_json("failed-fix-ids.json", ["F1"])
        batch_paths = self.write_json("failed-fix-paths.json", ["feature.txt"])
        begun, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(batch_paths),
        )
        after = self.modify("broken fix\n")
        result_file = self.write_json(
            "failed-fix.json",
            {
                "finding_ids": ["F1"],
                "diff_hash_before": begun["active_fix"]["diff_hash_before"],
                "diff_hash_after": after,
                "changed_paths": ["feature.txt"],
                "executor": {"name": "direct", "mode": "frozen_batch"},
                "test_mode": "none",
                "invalidated_surfaces": ["R1"],
                "direct_validation": [
                    {"status": "fail", "evidence": "Regression test still fails."}
                ],
            },
        )
        result, _ = self.run_state(
            "record-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
            expected=2,
        )
        self.assertIn("must pass", result["error"])
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(persisted["findings"]["F1"]["status"], "fixing")
        self.assertEqual(persisted["fix_batch_count"], 0)

    def test_no_op_fix_batch_can_cancel_as_false_positive(self) -> None:
        initialized = self.init(local_text="suspected bug\n")
        diff_hash = initialized["live_diff_hash"]
        self.record_initial(
            diff_hash,
            [self.finding("F-NO-OP")],
            failed_surface="R1",
        )
        ids = self.write_json("no-op-ids.json", ["F-NO-OP"])
        paths = self.write_json("no-op-paths.json", ["feature.txt"])
        self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
        )
        cancellation = self.write_json(
            "no-op-cancellation.json",
            {
                "finding_resolutions": [
                    {
                        "id": "F-NO-OP",
                        "disposition": "false_positive",
                        "evidence": "Reproduction proves the suspected behavior is correct.",
                    }
                ]
            },
        )
        cancelled, _ = self.run_state(
            "cancel-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(cancellation),
        )
        self.assertEqual(cancelled["phase"], "impact_review")
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIsNone(persisted["active_fix"])
        self.assertEqual(persisted["findings"]["F-NO-OP"]["status"], "false_positive")
        self.assertEqual(
            persisted["derived_requirements"]["REQ-F-NO-OP"]["status"],
            "false_positive",
        )

        self.record_impact(diff_hash)
        self.record_full_validation(diff_hash)
        ready, _ = self.run_state("evaluate-ready", "--state", str(self.state))
        self.assertTrue(ready["ready_local_diff"])

    def test_cancel_fix_enforces_surface_invalidation_convergence(self) -> None:
        requirements = self.requirements(threshold=50)
        requirements["design_review_thresholds"]["surface_invalidations"] = 1
        initialized = self.init(requirements, local_text="suspected bug\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-CANCEL-CONVERGENCE")],
            failed_surface="R1",
        )
        ids = self.write_json("cancel-convergence-ids.json", ["F-CANCEL-CONVERGENCE"])
        paths = self.write_json("cancel-convergence-paths.json", ["feature.txt"])
        self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
        )
        cancellation = self.write_json(
            "cancel-convergence-result.json",
            {
                "finding_resolutions": [
                    {
                        "id": "F-CANCEL-CONVERGENCE",
                        "disposition": "false_positive",
                        "evidence": "The reproducer proves the behavior is valid.",
                    }
                ]
            },
        )
        stopped, _ = self.run_state(
            "cancel-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(cancellation),
            expected=3,
        )
        self.assertEqual(stopped["phase"], "design_review_required")
        self.assertEqual(
            stopped["reason"],
            "review_surfaces_repeatedly_invalidated",
        )

    def test_cancel_fix_rejects_out_of_scope_git_control_change(self) -> None:
        initialized = self.init(local_text="suspected bug\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-CANCEL-BOUNDARY")],
            failed_surface="R1",
        )
        ids = self.write_json("cancel-boundary-ids.json", ["F-CANCEL-BOUNDARY"])
        paths = self.write_json("cancel-boundary-paths.json", ["feature.txt"])
        self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
        )
        git_config = self.root / ".git" / "config"
        git_config.write_text(
            git_config.read_text(encoding="utf-8") + "\n# unauthorized\n",
            encoding="utf-8",
        )
        cancellation = self.write_json(
            "cancel-boundary-result.json",
            {
                "finding_resolutions": [
                    {
                        "id": "F-CANCEL-BOUNDARY",
                        "disposition": "retry",
                        "evidence": "No product change was retained.",
                    }
                ]
            },
        )
        rejected, _ = self.run_state(
            "cancel-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(cancellation),
            expected=2,
        )
        self.assertIn("outside the frozen batch", rejected["error"])
        self.assertIn("<git-dir>/config", rejected["error"])

    def test_evidence_only_preflight_can_skip_impact_reviewer(self) -> None:
        tests_dir = self.root / "tests"
        tests_dir.mkdir()
        test_path = tests_dir / "feature.test.txt"
        test_path.write_text("baseline evidence\n", encoding="utf-8")
        self.run_git("add", "tests/feature.test.txt")
        self.run_git("commit", "-m", "add test evidence baseline")
        test_path.write_text("stale evidence\n", encoding="utf-8")
        requirements = self.requirements()
        requirements["review_surfaces"] = [
            {"id": "test-reliability", "description": "Test evidence is reliable."}
        ]
        initialized = self.init(requirements, local_text=None)
        review_file = self.write_json(
            "evidence-only-review.json",
            {
                "kind": "initial",
                "diff_hash": initialized["live_diff_hash"],
                "coverage_complete": True,
                "surfaces": [
                    {
                        "id": "test-reliability",
                        "status": "failed",
                        "evidence": "The evidence file is stale.",
                    }
                ],
                "findings": [
                    self.finding(
                        "F-EVIDENCE-ONLY",
                        classification="test_infrastructure",
                        surface="test-reliability",
                    )
                ],
            },
        )
        self.run_state(
            "record-review",
            "--state",
            str(self.state),
            "--result-file",
            str(review_file),
        )
        ids = self.write_json("evidence-only-ids.json", ["F-EVIDENCE-ONLY"])
        paths = self.write_json("evidence-only-paths.json", ["tests/feature.test.txt"])
        begun, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
            "--test-mode",
            "light",
        )
        self.assertEqual(
            [
                item["id"]
                for item in begun["active_fix"]["executor_contract"]["preflight_checklist"]
            ],
            ["changed-path-integrity", "test-fixture-isolation"],
        )
        test_path.write_text("fresh isolated evidence\n", encoding="utf-8")
        after = self.status()["live_diff_hash"]
        result_file = self.write_json(
            "evidence-only-fix.json",
            {
                "finding_ids": ["F-EVIDENCE-ONLY"],
                "diff_hash_before": begun["active_fix"]["diff_hash_before"],
                "diff_hash_after": after,
                "changed_paths": ["tests/feature.test.txt"],
                "executor": {"name": "direct", "mode": "frozen_batch"},
                "test_mode": "light",
                "invalidated_surfaces": ["test-reliability"],
                "direct_validation": [
                    {
                        "id": "changed-path-integrity",
                        "status": "pass",
                        "evidence": "Only the evidence file changed.",
                    },
                    {
                        "id": "test-fixture-isolation",
                        "status": "pass",
                        "evidence": "No shared fixture semantics changed.",
                    },
                    {
                        "id": "targeted-tests",
                        "status": "pass",
                        "evidence": "The targeted evidence test passed.",
                    },
                ],
                "evidence_only_fast_path": {
                    "changes_product_behavior": False,
                    "changes_fixture_semantics": False,
                    "changes_shared_test_helpers": False,
                    "evidence": "Local static checks and the targeted test prove test reliability.",
                },
            },
        )
        recorded, _ = self.run_state(
            "record-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
        )
        self.assertEqual(recorded["phase"], "validation")
        self.assertEqual(recorded["review_surfaces"]["test-reliability"], "clean")

    def test_evidence_only_path_requires_an_actual_test_root_or_test_filename(self) -> None:
        spec = importlib.util.spec_from_file_location("delivery_state_under_test", SCRIPT)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        self.assertFalse(module.is_test_evidence_path("src/fixtures/catalog.json"))
        self.assertFalse(module.is_test_evidence_path("app/snapshots/default.json"))
        self.assertTrue(module.is_test_evidence_path("tests/fixtures/catalog.json"))
        self.assertTrue(module.is_test_evidence_path("src/catalog.test.json"))

    def test_diff_identity_includes_unfiltered_tracked_worktree_bytes(self) -> None:
        attributes = self.root / ".gitattributes"
        attributes.write_text("*.txt text eol=lf\n", encoding="utf-8")
        self.run_git("add", ".gitattributes")
        self.run_git("commit", "-m", "normalize text files")
        initialized = self.init(local_text="semantic change\n")

        (self.root / "feature.txt").write_bytes(b"semantic change\r\n")
        live = self.status()

        self.assertEqual(live["live_paths"], ["feature.txt"])
        self.assertNotEqual(live["live_diff_hash"], initialized["live_diff_hash"])

    def test_ready_manifest_hashes_unfiltered_worktree_bytes(self) -> None:
        attributes = self.root / ".gitattributes"
        filtered = self.root / "filtered.txt"
        attributes.write_text("*.txt text eol=lf\n", encoding="utf-8")
        filtered.write_bytes(b"one\ntwo\n")
        self.run_git("add", ".gitattributes", "filtered.txt")
        self.run_git("commit", "-m", "add normalized text")
        spec = importlib.util.spec_from_file_location("delivery_state_under_test", SCRIPT)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        before = module.build_ready_manifest(self.root, ["filtered.txt"])
        filtered.write_bytes(b"one\r\ntwo\r\n")
        after = module.build_ready_manifest(self.root, ["filtered.txt"])

        self.assertNotEqual(
            before["filtered.txt"]["worktree"]["oid"],
            after["filtered.txt"]["worktree"]["oid"],
        )

    def test_record_fix_requires_preflight_for_every_invalidated_surface(self) -> None:
        requirements = self.requirements()
        requirements["review_surfaces"] = [
            {"id": "R1", "description": "Behavior is complete and correct."},
            {"id": "time-semantics", "description": "Time behavior is correct."},
        ]
        initialized = self.init(requirements, local_text="buggy\n")
        review_file = self.write_json(
            "preflight-expanded-surface-review.json",
            {
                "kind": "initial",
                "diff_hash": initialized["live_diff_hash"],
                "coverage_complete": True,
                "surfaces": [
                    {"id": "R1", "status": "failed", "evidence": "Bug found."},
                    {
                        "id": "time-semantics",
                        "status": "clean",
                        "evidence": "Initially unaffected.",
                    },
                ],
                "findings": [self.finding("F-PREFLIGHT-EXPANDED", surface="R1")],
            },
        )
        self.run_state(
            "record-review",
            "--state",
            str(self.state),
            "--result-file",
            str(review_file),
        )
        ids = self.write_json("preflight-expanded-ids.json", ["F-PREFLIGHT-EXPANDED"])
        paths = self.write_json("preflight-expanded-paths.json", ["feature.txt"])
        begun, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
        )
        (self.root / "feature.txt").write_text("fixed\n", encoding="utf-8")
        after = self.status()["live_diff_hash"]
        result_file = self.write_json(
            "preflight-expanded-result.json",
            {
                "finding_ids": ["F-PREFLIGHT-EXPANDED"],
                "diff_hash_before": begun["active_fix"]["diff_hash_before"],
                "diff_hash_after": after,
                "changed_paths": ["feature.txt"],
                "executor": {"name": "direct", "mode": "frozen_batch"},
                "test_mode": "none",
                "invalidated_surfaces": ["R1", "time-semantics"],
                "direct_validation": [
                    {
                        "id": "changed-path-integrity",
                        "status": "pass",
                        "evidence": "The frozen path is exact.",
                    }
                ],
            },
        )
        rejected, _ = self.run_state(
            "record-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
            expected=2,
        )
        self.assertIn("expiry-time-boundaries", rejected["error"])

    def test_false_positive_reclassified_as_follow_up_removes_derived_requirement(self) -> None:
        initialized = self.init(local_text="suspected bug\n")
        diff_hash = initialized["live_diff_hash"]
        self.record_initial(
            diff_hash,
            [self.finding("F-FOLLOW-UP")],
            failed_surface="R1",
        )
        ids = self.write_json("follow-up-cancel-ids.json", ["F-FOLLOW-UP"])
        paths = self.write_json("follow-up-cancel-paths.json", ["feature.txt"])
        self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(paths),
        )
        cancellation = self.write_json(
            "follow-up-cancellation.json",
            {
                "finding_resolutions": [
                    {
                        "id": "F-FOLLOW-UP",
                        "disposition": "false_positive",
                        "evidence": "The original behavior is valid on this diff.",
                    }
                ]
            },
        )
        self.run_state(
            "cancel-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(cancellation),
        )
        self.record_impact(diff_hash)
        self.record_full_validation(diff_hash)

        later_file = self.write_json(
            "follow-up-later-finding.json",
            {
                "diff_hash": diff_hash,
                "findings": [
                    self.finding(
                        "F-LATER-FIX",
                        root_cause="later real bug",
                        source="external-review",
                    )
                ],
            },
        )
        self.run_state(
            "add-findings",
            "--state",
            str(self.state),
            "--result-file",
            str(later_file),
        )
        later_ids = self.write_json("later-fix-ids.json", ["F-LATER-FIX"])
        begun, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(later_ids),
            "--batch-paths-file",
            str(paths),
        )
        after = self.modify("later bug fixed\n")
        fix_file = self.write_json(
            "follow-up-later-fix.json",
            {
                "finding_ids": ["F-LATER-FIX"],
                "diff_hash_before": begun["active_fix"]["diff_hash_before"],
                "diff_hash_after": after,
                "changed_paths": ["feature.txt"],
                "executor": {"name": "direct", "mode": "frozen_batch"},
                "test_mode": "none",
                "invalidated_surfaces": ["R1"],
                "direct_validation": [
                    {"status": "pass", "evidence": "The later bug is fixed."}
                ],
            },
        )
        self.run_state(
            "record-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(fix_file),
            expected=3,
        )
        resolution_file = self.write_json(
            "follow-up-reclassification.json",
            {
                "resolved_decisions": [],
                "finding_resolutions": [
                    {
                        "id": "F-FOLLOW-UP",
                        "classification": "enhancement",
                        "evidence": "On the new diff this is optional enhancement work.",
                    }
                ],
            },
        )
        resumed, _ = self.run_state(
            "resolve-decision",
            "--state",
            str(self.state),
            "--result-file",
            str(resolution_file),
        )
        self.assertEqual(resumed["phase"], "impact_review")
        stored = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(stored["findings"]["F-FOLLOW-UP"]["status"], "follow_up")
        self.assertNotIn("REQ-F-FOLLOW-UP", stored["derived_requirements"])

    def test_decision_resolution_does_not_recount_registered_root_cause(self) -> None:
        initialized = self.init(local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-NO-RECOUNT")],
            failed_surface="R1",
        )
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        root_key, root_stat = next(iter(persisted["root_cause_stats"].items()))
        self.assertEqual(root_stat["occurrence_count"], 1)
        persisted["findings"]["F-NO-RECOUNT"]["status"] = "decision_required"
        persisted["status"] = "paused"
        persisted["phase"] = "decision_required"
        persisted["exit_reason"] = "test_reclassification"
        self.state.write_text(json.dumps(persisted), encoding="utf-8")
        resolution_file = self.write_json(
            "registered-root-resolution.json",
            {
                "resolved_decisions": [],
                "finding_resolutions": [
                    {
                        "id": "F-NO-RECOUNT",
                        "classification": "introduced_regression",
                        "evidence": "The existing finding remains a regression.",
                    }
                ],
            },
        )
        self.run_state(
            "resolve-decision",
            "--state",
            str(self.state),
            "--result-file",
            str(resolution_file),
        )
        stored = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(stored["root_cause_stats"][root_key]["occurrence_count"], 1)
        self.assertEqual(stored["root_cause_stats"][root_key]["recurrence_count"], 0)

    def test_decision_root_cause_correction_reassigns_aggregate_stats(self) -> None:
        initialized = self.init(local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-ROOT-CORRECTION", root_cause="obsolete cause")],
            failed_surface="R1",
        )
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        old_key = persisted["findings"]["F-ROOT-CORRECTION"]["root_cause_key"]
        persisted["findings"]["F-ROOT-CORRECTION"]["status"] = "decision_required"
        persisted["status"] = "paused"
        persisted["phase"] = "decision_required"
        persisted["exit_reason"] = "test_root_cause_correction"
        self.state.write_text(json.dumps(persisted), encoding="utf-8")
        resolution_file = self.write_json(
            "corrected-root-resolution.json",
            {
                "resolved_decisions": [],
                "finding_resolutions": [
                    {
                        "id": "F-ROOT-CORRECTION",
                        "classification": "introduced_regression",
                        "root_cause": "Canonical Lifecycle Cause",
                        "evidence": "Design review identified the canonical cause.",
                    }
                ],
            },
        )
        self.run_state(
            "resolve-decision",
            "--state",
            str(self.state),
            "--result-file",
            str(resolution_file),
        )
        stored = json.loads(self.state.read_text(encoding="utf-8"))
        finding = stored["findings"]["F-ROOT-CORRECTION"]
        self.assertEqual(finding["normalized_root_cause"], "canonical lifecycle cause")
        self.assertNotEqual(finding["root_cause_key"], old_key)
        self.assertNotIn(old_key, stored["root_cause_stats"])
        self.assertEqual(
            stored["root_cause_stats"][finding["root_cause_key"]]["occurrence_count"],
            1,
        )

    def test_blocking_finding_cannot_be_reported_on_clean_surface(self) -> None:
        initialized = self.init(local_text="buggy\n")
        diff_hash = initialized["live_diff_hash"]
        review_file = self.write_json(
            "inconsistent-review.json",
            {
                "kind": "initial",
                "diff_hash": diff_hash,
                "coverage_complete": True,
                "surfaces": [
                    {"id": "R1", "status": "clean", "evidence": "Claimed clean."},
                    {"id": "R2", "status": "clean", "evidence": "Clean."},
                ],
                "findings": [self.finding("F1")],
            },
        )
        result, _ = self.run_state(
            "record-review",
            "--state",
            str(self.state),
            "--result-file",
            str(review_file),
            expected=2,
        )
        self.assertIn("failed or blocked", result["error"])

    def test_paused_requirement_decision_can_resume(self) -> None:
        requirements = self.requirements()
        requirements["unresolved_decisions"] = ["Choose the authoritative state owner."]
        self.init_paused(requirements)
        decision_file = self.write_json(
            "decision.json",
            {
                "resolved_decisions": [
                    {
                        "decision": "Choose the authoritative state owner.",
                        "resolution": "The database acknowledgement is authoritative.",
                        "evidence": "The user explicitly selected the database authority.",
                    }
                ],
                "finding_resolutions": [],
            },
        )
        result, _ = self.run_state(
            "resolve-decision",
            "--state",
            str(self.state),
            "--result-file",
            str(decision_file),
        )
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["phase"], "initial_review")
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(persisted["requirements"]["unresolved_decisions"], [])

    def test_duplicate_validation_id_is_rejected(self) -> None:
        initialized = self.init(local_text="implementation\n")
        diff_hash = initialized["live_diff_hash"]
        self.record_initial(diff_hash)
        validation_file = self.write_json(
            "duplicate-validation.json",
            {
                "diff_hash": diff_hash,
                "validations": [
                    {"id": "V1", "status": "fail", "evidence": "Failed.", "finding_id": "F1"},
                    {"id": "V1", "status": "pass", "evidence": "Later pass."},
                ],
                "acceptance": [],
                "verified_findings": [],
                "new_findings": [self.finding("F1", source="validation")],
            },
        )
        result, _ = self.run_state(
            "record-validation",
            "--state",
            str(self.state),
            "--result-file",
            str(validation_file),
            expected=2,
        )
        self.assertIn("duplicate validations id", result["error"])

    def test_failed_validation_must_bind_its_own_finding(self) -> None:
        initialized = self.init(local_text="buggy\n")
        diff_hash = initialized["live_diff_hash"]
        self.record_initial(diff_hash)
        validation_file = self.write_json(
            "unbound-validation.json",
            {
                "diff_hash": diff_hash,
                "validations": [
                    {
                        "id": "V1",
                        "status": "fail",
                        "evidence": "A different behavior failed.",
                        "finding_id": "F-MISSING",
                    }
                ],
                "acceptance": [],
                "verified_findings": [],
                "new_findings": [],
            },
        )
        result, _ = self.run_state(
            "record-validation",
            "--state",
            str(self.state),
            "--result-file",
            str(validation_file),
            expected=3,
        )
        self.assertEqual(
            result["reason"], "each_failed_item_requires_its_own_blocking_finding"
        )

    def test_validation_rejects_code_changed_after_review(self) -> None:
        initialized = self.init(local_text="reviewed implementation\n")
        reviewed_hash = initialized["live_diff_hash"]
        self.record_initial(reviewed_hash)
        changed_hash = self.modify("changed after review\n")
        validation_file = self.write_json(
            "stale-review-validation.json",
            {
                "diff_hash": changed_hash,
                "validations": [
                    {"id": "V1", "status": "pass", "evidence": "Test passed."}
                ],
                "acceptance": [],
                "verified_findings": [],
                "finding_evidence": {},
                "new_findings": [],
            },
        )
        result, _ = self.run_state(
            "record-validation",
            "--state",
            str(self.state),
            "--result-file",
            str(validation_file),
            expected=2,
        )
        self.assertIn("changed after review", result["error"])

    def test_failed_validation_reopens_fixed_finding_for_remediation(self) -> None:
        initialized = self.init(local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-FAILED-FIX")],
            failed_surface="R1",
        )
        fixed_hash = self.begin_and_record_fix("F-FAILED-FIX", "attempted fix\n")
        self.record_impact(fixed_hash)
        validation_file = self.write_json(
            "failed-fix-validation.json",
            {
                "diff_hash": fixed_hash,
                "validations": [
                    {
                        "id": "V1",
                        "status": "fail",
                        "evidence": "The regression still reproduces.",
                        "finding_id": "F-FAILED-FIX",
                    }
                ],
                "acceptance": [],
                "verified_findings": [],
                "new_findings": [],
            },
        )
        reopened, _ = self.run_state(
            "record-validation",
            "--state",
            str(self.state),
            "--result-file",
            str(validation_file),
        )
        self.assertEqual(reopened["phase"], "remediation")
        self.assertIn("F-FAILED-FIX", reopened["remediable_findings"])
        self.assertEqual(reopened["review_surfaces"]["R1"], "invalidated")

    def test_ready_rejects_piecemeal_validation_evidence(self) -> None:
        initialized = self.init(local_text="implemented\n")
        diff_hash = initialized["live_diff_hash"]
        self.record_initial(diff_hash)
        first = self.write_json(
            "piecemeal-validation-first.json",
            {
                "diff_hash": diff_hash,
                "validations": [
                    {"id": "V1", "status": "pass", "evidence": "Direct tests passed."}
                ],
                "acceptance": [
                    {"id": "A1", "status": "pass", "evidence": "Behavior proved."}
                ],
                "verified_findings": [],
                "new_findings": [],
            },
        )
        self.run_state(
            "record-validation",
            "--state",
            str(self.state),
            "--result-file",
            str(first),
        )
        second = self.write_json(
            "piecemeal-validation-second.json",
            {
                "diff_hash": diff_hash,
                "validations": [
                    {"id": "V2", "status": "pass", "evidence": "Diff integrity passed."}
                ],
                "acceptance": [],
                "verified_findings": [],
                "new_findings": [],
            },
        )
        self.run_state(
            "record-validation",
            "--state",
            str(self.state),
            "--result-file",
            str(second),
        )
        stopped, _ = self.run_state(
            "evaluate-ready",
            "--state",
            str(self.state),
            expected=3,
        )
        self.assertIn(
            "no complete final validation campaign",
            " ".join(stopped["failures"]),
        )

    def test_failed_validation_clears_older_complete_campaign(self) -> None:
        initialized = self.init(local_text="implemented\n")
        diff_hash = initialized["live_diff_hash"]
        self.record_initial(diff_hash)
        self.record_full_validation(diff_hash)
        failed = self.write_json(
            "failure-after-complete-campaign.json",
            {
                "diff_hash": diff_hash,
                "validations": [
                    {
                        "id": "V1",
                        "status": "fail",
                        "evidence": "A later rerun failed.",
                    }
                ],
                "acceptance": [],
                "verified_findings": [],
                "new_findings": [],
            },
        )
        self.run_state(
            "record-validation",
            "--state",
            str(self.state),
            "--result-file",
            str(failed),
            expected=3,
        )
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIsNone(persisted["complete_validation_campaign"])

    def test_partial_validation_clears_older_complete_campaign(self) -> None:
        initialized = self.init(local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-PARTIAL-CAMPAIGN")],
            failed_surface="R1",
        )
        fixed_hash = self.begin_and_record_fix(
            "F-PARTIAL-CAMPAIGN",
            "fixed\n",
        )
        self.record_impact(fixed_hash)
        self.record_full_validation(fixed_hash)
        partial = self.write_json(
            "partial-after-complete-campaign.json",
            {
                "diff_hash": fixed_hash,
                "validations": [],
                "acceptance": [],
                "verified_findings": ["F-PARTIAL-CAMPAIGN"],
                "finding_evidence": {
                    "F-PARTIAL-CAMPAIGN": "The focused regression evidence passed."
                },
                "new_findings": [],
            },
        )

        self.run_state(
            "record-validation",
            "--state",
            str(self.state),
            "--result-file",
            str(partial),
        )

        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIsNone(persisted["complete_validation_campaign"])
        stopped, _ = self.run_state(
            "evaluate-ready",
            "--state",
            str(self.state),
            expected=3,
        )
        self.assertIn(
            "no complete final validation campaign",
            " ".join(stopped["failures"]),
        )

    def test_validation_rejects_verified_finding_bound_to_failure(self) -> None:
        initialized = self.init(local_text="buggy\n")
        self.record_initial(
            initialized["live_diff_hash"],
            [self.finding("F-CONTRADICTORY-VALIDATION")],
            failed_surface="R1",
        )
        fixed_hash = self.begin_and_record_fix(
            "F-CONTRADICTORY-VALIDATION",
            "attempted fix\n",
        )
        self.record_impact(fixed_hash)
        contradictory = self.write_json(
            "contradictory-validation.json",
            {
                "diff_hash": fixed_hash,
                "validations": [
                    {
                        "id": "V1",
                        "status": "fail",
                        "evidence": "The finding still reproduces.",
                        "finding_id": "F-CONTRADICTORY-VALIDATION",
                    }
                ],
                "acceptance": [],
                "verified_findings": ["F-CONTRADICTORY-VALIDATION"],
                "finding_evidence": {
                    "F-CONTRADICTORY-VALIDATION": "Contradictory pass evidence."
                },
                "new_findings": [],
            },
        )
        rejected, _ = self.run_state(
            "record-validation",
            "--state",
            str(self.state),
            "--result-file",
            str(contradictory),
            expected=2,
        )
        self.assertIn("cannot be verified and bound to failed evidence", rejected["error"])
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(
            persisted["findings"]["F-CONTRADICTORY-VALIDATION"]["status"],
            "fixed_unverified",
        )

    def test_late_finding_invalidates_complete_validation_campaign(self) -> None:
        diff_hash = self.make_ready_without_findings()
        finding_file = self.write_json(
            "late-campaign-finding.json",
            {
                "diff_hash": diff_hash,
                "findings": [self.finding("F-LATE-CAMPAIGN", source="external-review")],
            },
        )
        self.run_state(
            "add-findings",
            "--state",
            str(self.state),
            "--result-file",
            str(finding_file),
        )
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIsNone(persisted["complete_validation_campaign"])

    def test_failed_review_surface_requires_finding(self) -> None:
        initialized = self.init(local_text="implementation\n")
        diff_hash = initialized["live_diff_hash"]
        review_file = self.write_json(
            "untracked-failed-surface.json",
            {
                "kind": "initial",
                "diff_hash": diff_hash,
                "coverage_complete": True,
                "surfaces": [
                    {"id": "R1", "status": "failed", "evidence": "A defect exists."},
                    {"id": "R2", "status": "clean", "evidence": "Clean."},
                ],
                "findings": [],
            },
        )
        result, _ = self.run_state(
            "record-review",
            "--state",
            str(self.state),
            "--result-file",
            str(review_file),
            expected=2,
        )
        self.assertIn("requires a blocking finding", result["error"])

    def test_add_findings_cannot_bypass_design_review_gate(self) -> None:
        initialized = self.init(
            self.requirements(threshold=1),
            local_text="buggy\n",
        )
        initial_hash = initialized["live_diff_hash"]
        self.record_initial(
            initial_hash,
            [self.finding("F1")],
            failed_surface="R1",
        )
        fixed_hash = self.begin_and_record_fix("F1", "first fix\n")
        self.record_impact(
            fixed_hash,
            [self.finding("F2", root_cause="second root cause")],
            status="failed",
            expected=3,
        )
        result_file = self.write_json(
            "finding-during-design-gate.json",
            {
                "diff_hash": fixed_hash,
                "findings": [self.finding("F3", surface="R2", source="external-review")],
            },
        )
        result, _ = self.run_state(
            "add-findings",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
        )
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["phase"], "design_review_required")
        self.assertIn("F3", result["open_findings"])
        self.assertEqual(result["review_surfaces"]["R2"], "invalidated")

    def test_validation_decision_finding_pauses_and_invalidates_surfaces(self) -> None:
        initialized = self.init(local_text="implementation\n")
        diff_hash = initialized["live_diff_hash"]
        self.record_initial(diff_hash)
        decision = self.finding(
            "D1",
            classification="decision_required",
            surface="R1",
            source="validation",
        )
        decision["affected_surfaces"] = ["R1", "R2"]
        validation_file = self.write_json(
            "decision-validation.json",
            {
                "diff_hash": diff_hash,
                "validations": [
                    {
                        "id": "V1",
                        "status": "fail",
                        "evidence": "The authority is ambiguous.",
                        "finding_id": "D1",
                    }
                ],
                "acceptance": [],
                "verified_findings": [],
                "finding_evidence": {},
                "new_findings": [decision],
            },
        )
        result, _ = self.run_state(
            "record-validation",
            "--state",
            str(self.state),
            "--result-file",
            str(validation_file),
            expected=3,
        )
        self.assertEqual(result["phase"], "decision_required")
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(persisted["review_surfaces"]["R1"]["status"], "invalidated")
        self.assertEqual(persisted["review_surfaces"]["R2"]["status"], "invalidated")

        decision_file = self.write_json(
            "resolve-validation-decision.json",
            {
                "resolved_decisions": [],
                "finding_resolutions": [
                    {
                        "id": "D1",
                        "classification": "introduced_regression",
                        "root_cause": "One authority must own the state.",
                        "evidence": "The database acknowledgement was selected.",
                    }
                ],
            },
        )
        resumed, _ = self.run_state(
            "resolve-decision",
            "--state",
            str(self.state),
            "--result-file",
            str(decision_file),
        )
        self.assertEqual(resumed["phase"], "remediation")

    def test_validation_decision_preserves_reached_convergence_gate(self) -> None:
        requirements = self.requirements(threshold=50)
        requirements["design_review_thresholds"]["root_cause_recurrences"] = 1
        initialized = self.init(requirements, local_text="implementation\n")
        diff_hash = initialized["live_diff_hash"]
        self.record_initial(diff_hash)
        decision = self.finding(
            "D-CONVERGENCE",
            classification="decision_required",
            source="validation",
        )
        validation_file = self.write_json(
            "validation-decision-convergence.json",
            {
                "diff_hash": diff_hash,
                "validations": [
                    {
                        "id": "V1",
                        "status": "fail",
                        "evidence": "Authority is undecided.",
                        "finding_id": "D-CONVERGENCE",
                    }
                ],
                "acceptance": [],
                "verified_findings": [],
                "new_findings": [
                    decision,
                    self.finding("F-CONVERGENCE-1", source="validation"),
                    self.finding("F-CONVERGENCE-2", source="validation"),
                ],
            },
        )
        paused, _ = self.run_state(
            "record-validation",
            "--state",
            str(self.state),
            "--result-file",
            str(validation_file),
            expected=3,
        )
        self.assertEqual(paused["phase"], "decision_required")
        resolution_file = self.write_json(
            "validation-decision-convergence-resolution.json",
            {
                "resolved_decisions": [],
                "finding_resolutions": [
                    {
                        "id": "D-CONVERGENCE",
                        "classification": "independent_follow_up",
                        "evidence": "The authority choice is outside this delivery.",
                    }
                ],
            },
        )
        stopped, _ = self.run_state(
            "resolve-decision",
            "--state",
            str(self.state),
            "--result-file",
            str(resolution_file),
            expected=3,
        )
        self.assertEqual(stopped["phase"], "design_review_required")
        self.assertEqual(
            stopped["reason"],
            "deferred_design_review_after_hard_gate",
        )

    def test_untracked_file_mode_is_part_of_diff_identity(self) -> None:
        self.init()
        script = self.root / "new-script.sh"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o644)
        before = self.status()["live_diff_hash"]
        script.chmod(0o755)
        after = self.status()["live_diff_hash"]
        self.assertNotEqual(before, after)

    def test_resolved_decision_recomputes_finding_fingerprint(self) -> None:
        initialized = self.init(local_text="implementation\n")
        diff_hash = initialized["live_diff_hash"]
        self.record_initial(diff_hash)
        finding_file = self.write_json(
            "decision-finding.json",
            {
                "diff_hash": diff_hash,
                "findings": [
                    self.finding(
                        "D1",
                        classification="decision_required",
                        source="review",
                    )
                ],
            },
        )
        self.run_state(
            "add-findings",
            "--state",
            str(self.state),
            "--result-file",
            str(finding_file),
            expected=3,
        )
        resolution_file = self.write_json(
            "fingerprint-resolution.json",
            {
                "resolved_decisions": [],
                "finding_resolutions": [
                    {
                        "id": "D1",
                        "classification": "introduced_regression",
                        "root_cause": "resolved root cause",
                        "evidence": "The ambiguity was resolved.",
                    }
                ],
            },
        )
        self.run_state(
            "resolve-decision",
            "--state",
            str(self.state),
            "--result-file",
            str(resolution_file),
        )
        duplicate = self.finding(
            "D1-DUPLICATE",
            classification="introduced_regression",
            root_cause="resolved root cause",
            source="later-review",
        )
        duplicate["trigger"] = "Trigger D1"
        duplicate_file = self.write_json(
            "resolved-duplicate.json",
            {"diff_hash": diff_hash, "findings": [duplicate]},
        )
        result, _ = self.run_state(
            "add-findings",
            "--state",
            str(self.state),
            "--result-file",
            str(duplicate_file),
        )
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(result["duplicates"], ["D1"])
        self.assertNotIn("D1-DUPLICATE", persisted["findings"])

    def test_fix_must_invalidate_every_affected_surface(self) -> None:
        initialized = self.init(local_text="buggy\n")
        diff_hash = initialized["live_diff_hash"]
        self.record_initial(diff_hash)
        finding = self.finding("F-BOTH", source="external-review")
        finding["affected_surfaces"] = ["R1", "R2"]
        finding_file = self.write_json(
            "multi-surface-finding.json",
            {"diff_hash": diff_hash, "findings": [finding]},
        )
        self.run_state(
            "add-findings",
            "--state",
            str(self.state),
            "--result-file",
            str(finding_file),
        )
        ids = self.write_json("multi-surface-ids.json", ["F-BOTH"])
        batch_paths = self.write_json("multi-surface-paths.json", ["feature.txt"])
        begun, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(batch_paths),
        )
        after = self.modify("fixed\n")
        result_file = self.write_json(
            "incomplete-invalidation.json",
            {
                "finding_ids": ["F-BOTH"],
                "diff_hash_before": begun["active_fix"]["diff_hash_before"],
                "diff_hash_after": after,
                "changed_paths": ["feature.txt"],
                "executor": {"name": "direct", "mode": "frozen_batch"},
                "test_mode": "none",
                "invalidated_surfaces": ["R1"],
                "direct_validation": [
                    {"status": "pass", "evidence": "Focused test passed."}
                ],
            },
        )
        result, _ = self.run_state(
            "record-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
            expected=2,
        )
        self.assertIn("every surface affected", result["error"])

    def test_add_findings_rejects_unreviewed_diff_drift(self) -> None:
        initialized = self.init(local_text="reviewed\n")
        reviewed_hash = initialized["live_diff_hash"]
        self.record_initial(reviewed_hash)
        drifted_hash = self.modify("changed after review\n")
        follow_up_file = self.write_json(
            "follow-up-on-drift.json",
            {
                "diff_hash": drifted_hash,
                "findings": [
                    self.finding(
                        "FU-DRIFT",
                        classification="independent_follow_up",
                        severity="P3",
                    )
                ],
            },
        )
        result, _ = self.run_state(
            "add-findings",
            "--state",
            str(self.state),
            "--result-file",
            str(follow_up_file),
            expected=2,
        )
        self.assertIn("changed after the latest recorded evidence", result["error"])

    def test_validation_failed_gate_can_be_retried_with_bound_finding(self) -> None:
        initialized = self.init(local_text="implementation\n")
        diff_hash = initialized["live_diff_hash"]
        self.record_initial(diff_hash)
        missing_file = self.write_json(
            "missing-validation-finding.json",
            {
                "diff_hash": diff_hash,
                "validations": [
                    {"id": "V1", "status": "fail", "evidence": "A regression failed."}
                ],
                "acceptance": [],
                "verified_findings": [],
                "new_findings": [],
            },
        )
        self.run_state(
            "record-validation",
            "--state",
            str(self.state),
            "--result-file",
            str(missing_file),
            expected=3,
        )
        corrected_file = self.write_json(
            "corrected-validation-finding.json",
            {
                "diff_hash": diff_hash,
                "validations": [
                    {
                        "id": "V1",
                        "status": "fail",
                        "evidence": "A regression failed.",
                        "finding_id": "F-VALIDATION",
                    }
                ],
                "acceptance": [],
                "verified_findings": [],
                "new_findings": [
                    self.finding("F-VALIDATION", source="validation")
                ],
            },
        )
        result, _ = self.run_state(
            "record-validation",
            "--state",
            str(self.state),
            "--result-file",
            str(corrected_file),
        )
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["phase"], "remediation")
        self.assertIn("F-VALIDATION", result["open_findings"])

    def test_duplicate_finding_merges_newly_affected_surfaces(self) -> None:
        initialized = self.init(local_text="implementation\n")
        diff_hash = initialized["live_diff_hash"]
        self.record_initial(diff_hash)
        first_file = self.write_json(
            "first-surface-finding.json",
            {
                "diff_hash": diff_hash,
                "findings": [self.finding("F-MERGE", surface="R1")],
            },
        )
        self.run_state(
            "add-findings",
            "--state",
            str(self.state),
            "--result-file",
            str(first_file),
        )
        duplicate = self.finding("F-MERGE-AGAIN", surface="R1")
        duplicate["trigger"] = "Trigger F-MERGE"
        duplicate["affected_surfaces"] = ["R1", "R2"]
        second_file = self.write_json(
            "expanded-surface-finding.json",
            {"diff_hash": diff_hash, "findings": [duplicate]},
        )
        result, _ = self.run_state(
            "add-findings",
            "--state",
            str(self.state),
            "--result-file",
            str(second_file),
        )
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(result["review_surfaces"]["R2"], "invalidated")
        self.assertEqual(
            persisted["findings"]["F-MERGE"]["affected_surfaces"],
            ["R1", "R2"],
        )
        self.assertNotIn("F-MERGE-AGAIN", persisted["findings"])
        root_stat = next(iter(persisted["root_cause_stats"].values()))
        self.assertEqual(root_stat["epoch_occurrence_count"], 1)
        persisted["version"] = 8
        self.state.write_text(json.dumps(persisted), encoding="utf-8")

        self.status()
        migrated = json.loads(self.state.read_text(encoding="utf-8"))
        root_stat = next(iter(migrated["root_cause_stats"].values()))
        self.assertEqual(root_stat["epoch_occurrence_count"], 1)
        self.assertEqual(root_stat["epoch_recurrence_count"], 0)

    def test_clean_worktree_is_rejected(self) -> None:
        requirements_file = self.write_json(
            "clean-worktree-requirements.json",
            self.requirements(),
        )
        result, _ = self.run_state(
            "init",
            "--state",
            str(self.state),
            "--project-root",
            str(self.root),
            "--requirements-file",
            str(requirements_file),
            expected=2,
        )
        self.assertIn("requires an existing local diff", result["error"])
        self.assertFalse(self.state.exists())

    def test_initialization_rejects_paths_unsupported_by_frozen_batches(self) -> None:
        (self.root / "star*.txt").write_text("unsupported literal path\n", encoding="utf-8")
        requirements_file = self.write_json(
            "unsupported-path-requirements.json",
            self.requirements(),
        )
        result, _ = self.run_state(
            "init",
            "--state",
            str(self.state),
            "--project-root",
            str(self.root),
            "--requirements-file",
            str(requirements_file),
            expected=2,
        )
        self.assertIn("initial diff path must not be a Git pathspec or glob", result["error"])
        self.assertFalse(self.state.exists())

    def test_changed_submodule_is_rejected_during_initialization(self) -> None:
        baseline_commit = self.run_git("rev-parse", "HEAD")
        self.run_git(
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{baseline_commit},module",
        )
        self.run_git("commit", "-m", "add gitlink baseline")
        requirements_file = self.write_json(
            "submodule-requirements.json",
            self.requirements(),
        )
        result, _ = self.run_state(
            "init",
            "--state",
            str(self.state),
            "--project-root",
            str(self.root),
            "--requirements-file",
            str(requirements_file),
            expected=2,
        )
        self.assertIn("changed submodule paths are unsupported", result["error"])
        self.assertIn("module", result["error"])

    def test_deleted_submodule_is_rejected_during_initialization(self) -> None:
        baseline_commit = self.run_git("rev-parse", "HEAD")
        self.run_git(
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{baseline_commit},module",
        )
        self.run_git("commit", "-m", "add gitlink baseline")
        self.run_git("update-index", "--force-remove", "module")
        requirements_file = self.write_json(
            "deleted-submodule-requirements.json",
            self.requirements(),
        )
        result, _ = self.run_state(
            "init",
            "--state",
            str(self.state),
            "--project-root",
            str(self.root),
            "--requirements-file",
            str(requirements_file),
            expected=2,
        )
        self.assertIn("changed submodule paths are unsupported", result["error"])
        self.assertIn("module", result["error"])

    def test_replaced_submodule_is_rejected_during_initialization(self) -> None:
        baseline_commit = self.run_git("rev-parse", "HEAD")
        self.run_git(
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{baseline_commit},module",
        )
        self.run_git("commit", "-m", "add gitlink baseline")
        self.run_git("update-index", "--force-remove", "module")
        (self.root / "module").write_text("regular replacement\n", encoding="utf-8")
        self.run_git("add", "module")
        requirements_file = self.write_json(
            "replaced-submodule-requirements.json",
            self.requirements(),
        )
        result, _ = self.run_state(
            "init",
            "--state",
            str(self.state),
            "--project-root",
            str(self.root),
            "--requirements-file",
            str(requirements_file),
            expected=2,
        )
        self.assertIn("changed submodule paths are unsupported", result["error"])
        self.assertIn("module", result["error"])

    def test_initial_review_rejects_diff_changed_after_initialization(self) -> None:
        initialized = self.init(local_text="frozen local work\n")
        drifted_hash = self.modify("changed before initial review\n")
        review_file = self.write_json(
            "drifted-initial-review.json",
            {
                "kind": "initial",
                "diff_hash": drifted_hash,
                "coverage_complete": True,
                "surfaces": [
                    {"id": "R1", "status": "clean", "evidence": "Reviewed."},
                    {"id": "R2", "status": "clean", "evidence": "Reviewed."},
                ],
                "findings": [],
            },
        )
        result, _ = self.run_state(
            "record-review",
            "--state",
            str(self.state),
            "--result-file",
            str(review_file),
            expected=2,
        )
        self.assertNotEqual(initialized["live_diff_hash"], drifted_hash)
        self.assertIn("local diff changed after supervisor initialization", result["error"])

    def test_initial_scope_includes_staged_unstaged_and_untracked_work(self) -> None:
        (self.root / "feature.txt").write_text("staged work\n", encoding="utf-8")
        self.run_git("add", "feature.txt")
        (self.root / "feature.txt").write_text(
            "staged plus unstaged work\n",
            encoding="utf-8",
        )
        (self.root / "new-file.txt").write_text("untracked work\n", encoding="utf-8")
        initialized = self.init(local_text=None)
        self.assertEqual(initialized["phase"], "initial_review")
        self.assertEqual(
            initialized["live_paths"],
            ["feature.txt", "new-file.txt"],
        )
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(persisted["initial_paths"], initialized["live_paths"])

    def test_staged_index_drift_changes_diff_hash(self) -> None:
        (self.root / "feature.txt").write_text("first index version\n", encoding="utf-8")
        self.run_git("add", "feature.txt")
        (self.root / "feature.txt").write_text("stable worktree version\n", encoding="utf-8")
        initialized = self.init(local_text=None)

        (self.root / "feature.txt").write_text("second index version\n", encoding="utf-8")
        self.run_git("add", "feature.txt")
        (self.root / "feature.txt").write_text("stable worktree version\n", encoding="utf-8")
        drifted = self.status()

        self.assertEqual(drifted["live_paths"], initialized["live_paths"])
        self.assertNotEqual(drifted["live_diff_hash"], initialized["live_diff_hash"])

    def test_fix_can_fully_revert_a_bad_local_change(self) -> None:
        initialized = self.init(local_text="bad local change\n")
        diff_hash = initialized["live_diff_hash"]
        self.record_initial(
            diff_hash,
            [self.finding("F-REVERT")],
            failed_surface="R1",
        )
        ids = self.write_json("revert-finding-ids.json", ["F-REVERT"])
        batch_paths = self.write_json("revert-fix-paths.json", ["feature.txt"])
        begun, _ = self.run_state(
            "begin-fix",
            "--state",
            str(self.state),
            "--finding-ids-file",
            str(ids),
            "--batch-paths-file",
            str(batch_paths),
        )
        (self.root / "feature.txt").write_text("baseline\n", encoding="utf-8")
        reverted = self.status()
        result_file = self.write_json(
            "revert-fix-result.json",
            {
                "finding_ids": ["F-REVERT"],
                "diff_hash_before": begun["active_fix"]["diff_hash_before"],
                "diff_hash_after": reverted["live_diff_hash"],
                "changed_paths": ["feature.txt"],
                "executor": {"name": "direct", "mode": "frozen_batch"},
                "test_mode": "none",
                "invalidated_surfaces": ["R1"],
                "direct_validation": [
                    {
                        "status": "pass",
                        "evidence": "The incorrect local change was fully reverted.",
                    }
                ],
            },
        )
        result, _ = self.run_state(
            "record-fix",
            "--state",
            str(self.state),
            "--result-file",
            str(result_file),
        )
        self.assertEqual(result["phase"], "impact_review")
        self.assertEqual(result["live_paths"], [])

    def test_state_file_inside_repository_is_rejected(self) -> None:
        (self.root / "feature.txt").write_text("local work\n", encoding="utf-8")
        requirements_file = self.write_json(
            "inside-state-requirements.json",
            self.requirements(),
        )
        inside_state = self.root / ".quality-delivery-state.json"
        result, _ = self.run_state(
            "init",
            "--state",
            str(inside_state),
            "--project-root",
            str(self.root),
            "--requirements-file",
            str(requirements_file),
            expected=2,
        )
        self.assertIn("must be outside", result["error"])
        self.assertFalse(inside_state.exists())

    def test_plural_credentials_filename_is_rejected_before_hashing(self) -> None:
        (self.root / "credentials.json").write_text(
            '{"token":"do-not-read"}\n',
            encoding="utf-8",
        )
        requirements_file = self.write_json(
            "credentials-requirements.json",
            self.requirements(),
        )
        result, _ = self.run_state(
            "init",
            "--state",
            str(self.state),
            "--project-root",
            str(self.root),
            "--requirements-file",
            str(requirements_file),
            expected=2,
        )
        self.assertIn("sensitive-looking", result["error"])
        self.assertNotIn("do-not-read", json.dumps(result))

    def test_standard_private_key_filename_is_rejected_before_hashing(self) -> None:
        (self.root / "id_ed25519").write_text(
            "do-not-read-private-key\n",
            encoding="utf-8",
        )
        requirements_file = self.write_json(
            "private-key-requirements.json",
            self.requirements(),
        )
        result, _ = self.run_state(
            "init",
            "--state",
            str(self.state),
            "--project-root",
            str(self.root),
            "--requirements-file",
            str(requirements_file),
            expected=2,
        )
        self.assertIn("sensitive-looking", result["error"])
        self.assertNotIn("do-not-read-private-key", json.dumps(result))

    def test_untracked_symlink_is_hashed_as_local_diff(self) -> None:
        (self.root / "linked-feature").symlink_to("feature.txt")
        initialized = self.init(local_text="implementation\n")
        self.assertEqual(
            initialized["live_paths"],
            ["feature.txt", "linked-feature"],
        )
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(
            persisted["initial_diff_hash"],
            initialized["live_diff_hash"],
        )

    def test_failed_items_require_distinct_finding_bindings(self) -> None:
        initialized = self.init(local_text="implementation\n")
        diff_hash = initialized["live_diff_hash"]
        self.record_initial(diff_hash)
        validation_file = self.write_json(
            "duplicate-finding-bindings.json",
            {
                "diff_hash": diff_hash,
                "validations": [
                    {
                        "id": "V1",
                        "status": "fail",
                        "evidence": "First validation failed.",
                        "finding_id": "F-BIND",
                    },
                    {
                        "id": "V2",
                        "status": "fail",
                        "evidence": "Second validation failed.",
                        "finding_id": "F-BIND",
                    },
                ],
                "acceptance": [],
                "verified_findings": [],
                "new_findings": [self.finding("F-BIND", source="validation")],
            },
        )
        result, _ = self.run_state(
            "record-validation",
            "--state",
            str(self.state),
            "--result-file",
            str(validation_file),
            expected=3,
        )
        self.assertEqual(
            result["reason"], "each_failed_item_requires_its_own_blocking_finding"
        )
        self.assertEqual(result["failed"], ["validations:V1", "validations:V2"])


if __name__ == "__main__":
    unittest.main()
