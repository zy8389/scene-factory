from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scene_factory.conformance import (
    CONFORMANCE_SCHEMA_VERSION,
    CORE_CONFORMANCE_CASES,
    ConformanceError,
    DryRunInteractionExecutor,
    capability_sha256,
    normalize_executor_capabilities,
    run_executor_conformance,
    validate_conformance_report,
    write_conformance_report_atomic,
)
from scene_factory.execution import ExecutionStepResult, ExecutorCapabilities


class _DelegatingExecutor:
    def __init__(self) -> None:
        self.inner = DryRunInteractionExecutor()

    def capabilities(self):
        return self.inner.capabilities()

    def reset(self, scene, initial_state) -> None:
        self.inner.reset(scene, initial_state)

    def execute(self, command):
        return self.inner.execute(command)

    def snapshot(self):
        return self.inner.snapshot()

    def close(self) -> None:
        self.inner.close()


class _MissingActionExecutor(_DelegatingExecutor):
    def capabilities(self):
        return ExecutorCapabilities(
            executor="missing_action",
            version="1",
            physical=False,
            supported_actions={"approach", "grasp", "push", "release"},
            articulation_execution=True,
        )


class _LyingExecutor(_DelegatingExecutor):
    def execute(self, command):
        if command.action.action == "pull":
            return ExecutionStepResult(
                command.command_id,
                command.step_id,
                "not_supported",
                "injected_not_supported",
                {},
            )
        return super().execute(command)


class _WrongCorrelationExecutor(_DelegatingExecutor):
    def execute(self, command):
        return ExecutionStepResult("wrong-command-id", command.step_id, "succeeded", None, {})


class _InvalidEvidenceExecutor(_DelegatingExecutor):
    def execute(self, command):
        return {
            "command_id": command.command_id,
            "step_id": command.step_id,
            "status": "succeeded",
            "reason": None,
            "evidence": {"invalid": float("nan")},
        }


class _GoalFailureExecutor(_DelegatingExecutor):
    def execute(self, command):
        return ExecutionStepResult(command.command_id, command.step_id, "succeeded", None, {})


class _CloseFailureExecutor(_DelegatingExecutor):
    def close(self) -> None:
        self.inner.close()
        raise RuntimeError("injected close failure")


class ExecutorConformanceTests(unittest.TestCase):
    def test_dry_run_core_conformance_passes(self) -> None:
        report = run_executor_conformance(DryRunInteractionExecutor)
        self.assertTrue(report.result == "passed", report.to_dict())
        self.assertEqual(report.summary, {"total": 12, "passed": 12, "failed": 0, "not_applicable": 0})
        self.assertEqual(tuple(case.case_id for case in report.cases), CORE_CONFORMANCE_CASES)
        self.assertEqual(report.capabilities["executor"], "dry_run")
        self.assertFalse(report.executor["physical"])
        self.assertTrue(validate_conformance_report(report).valid)

    def test_capability_validation_and_hash_are_stable(self) -> None:
        capabilities = ExecutorCapabilities(
            executor="dry_run",
            version="1",
            physical=False,
            supported_actions={"release", "approach", "grasp", "pull", "push", "rotate"},
            articulation_execution=True,
        )
        hashes = {capability_sha256(capabilities) for _ in range(100)}
        self.assertEqual(len(hashes), 1)
        self.assertEqual(normalize_executor_capabilities(capabilities).to_dict(), capabilities.to_dict())
        with self.assertRaises(ConformanceError):
            normalize_executor_capabilities({**capabilities.to_dict(), "supported_actions": ["pull", "pull"]})
        with self.assertRaises(ConformanceError):
            normalize_executor_capabilities({**capabilities.to_dict(), "supported_actions": ["teleport"]})

    def test_lifecycle_and_report_round_trip(self) -> None:
        report = run_executor_conformance(DryRunInteractionExecutor)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            write_conformance_report_atomic(path, report)
            loaded = validate_conformance_report(path)
        self.assertTrue(loaded.valid, loaded.to_dict())
        self.assertEqual(loaded.report.to_dict(), report.to_dict())
        self.assertEqual(loaded.report.schema_version, CONFORMANCE_SCHEMA_VERSION)

    def test_dry_run_conformance_is_deterministic(self) -> None:
        reports = [run_executor_conformance(DryRunInteractionExecutor).to_dict() for _ in range(20)]
        self.assertEqual(len({json.dumps(report, sort_keys=True) for report in reports}), 1)

    def test_report_validator_rejects_mutations(self) -> None:
        report = run_executor_conformance(DryRunInteractionExecutor)
        for mutation in ("summary", "capability_hash", "duplicate_case", "result"):
            payload = copy.deepcopy(report.to_dict())
            if mutation == "summary":
                payload["summary"]["passed"] -= 1
            elif mutation == "capability_hash":
                payload["capability_sha256"] = "0" * 64
            elif mutation == "duplicate_case":
                payload["cases"][1]["case_id"] = payload["cases"][0]["case_id"]
            else:
                payload["result"] = "failed"
            validation = validate_conformance_report(payload)
            self.assertFalse(validation.valid, (mutation, validation.to_dict()))

    def test_broken_executors_are_detected(self) -> None:
        cases = {
            _MissingActionExecutor: "missing_required_capability",
            _LyingExecutor: "capability_execution_mismatch",
            _WrongCorrelationExecutor: "correlation_mismatch",
            _InvalidEvidenceExecutor: "invalid_evidence",
            _GoalFailureExecutor: "goal_not_satisfied",
            _CloseFailureExecutor: "close_failed",
        }
        for factory, expected_reason in cases.items():
            with self.subTest(factory=factory.__name__):
                report = run_executor_conformance(factory)
                self.assertEqual(report.result, "failed", report.to_dict())
                self.assertTrue(
                    any(case.reason == expected_reason for case in report.cases), report.to_dict()
                )
                self.assertTrue(validate_conformance_report(report).valid, report.to_dict())

    def test_factory_failure_is_structured(self) -> None:
        def broken_factory():
            raise RuntimeError("factory unavailable")

        report = run_executor_conformance(broken_factory)
        self.assertEqual(report.result, "failed")
        self.assertEqual(report.failure_reason, "executor_factory_failed")
        self.assertEqual(report.cases[0].reason, "executor_factory_failed")
        self.assertEqual(report.summary["not_applicable"], len(CORE_CONFORMANCE_CASES) - 1)
        self.assertTrue(validate_conformance_report(report).valid, report.to_dict())

    def test_cli_inspect_conformance_and_validate_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "conformance.json"
            commands = [
                [sys.executable, "-m", "scene_factory", "executor", "inspect", "--executor", "dry-run"],
                [
                    sys.executable,
                    "-m",
                    "scene_factory",
                    "executor",
                    "conformance",
                    "--executor",
                    "dry-run",
                    "--output",
                    str(report_path),
                ],
                [sys.executable, "-m", "scene_factory", "executor", "validate-report", str(report_path)],
            ]
            for command in commands:
                completed = subprocess.run(command, capture_output=True, text=True, check=False)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertTrue(json.loads(completed.stdout))
            self.assertTrue(report_path.is_file())

    def test_no_isaac_numpy_imports(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; import scene_factory.conformance; "
                    "assert not any(name in sys.modules for name in "
                    "('isaacsim', 'omni', 'pxr', 'carb', 'numpy'))"
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_schema_resources_are_valid_json(self) -> None:
        for filename, expected_id in (
            ("executor_capabilities.schema.json", "scene_factory.executor_capabilities.v1"),
            ("executor_conformance.schema.json", "scene_factory.executor_conformance.v1"),
        ):
            schema = json.loads(Path("schemas", filename).read_text(encoding="utf-8"))
            self.assertEqual(schema["$id"], expected_id)

    def test_report_input_is_strictly_bounded_and_json_safe(self) -> None:
        report = run_executor_conformance(DryRunInteractionExecutor)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            duplicate = root / "duplicate.json"
            duplicate.write_text('{"schema_version":"x","schema_version":"y"}', encoding="utf-8")
            self.assertFalse(validate_conformance_report(duplicate).valid)

            nonfinite = root / "nonfinite.json"
            nonfinite.write_text('{"value":NaN}', encoding="utf-8")
            self.assertFalse(validate_conformance_report(nonfinite).valid)

            oversized = root / "oversized.json"
            oversized.write_text(
                json.dumps({"x": "a" * (2 * 1024 * 1024)}), encoding="utf-8"
            )
            self.assertFalse(validate_conformance_report(oversized).valid)

        self.assertTrue(validate_conformance_report(report).valid)


if __name__ == "__main__":
    unittest.main()
