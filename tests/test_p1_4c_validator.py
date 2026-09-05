"""Synthetic contract fixtures only: these tests do not qualify physical execution."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

import pytest

from scene_factory.backends.isaac_acceptance import (
    BINDING,
    REPORT_VERSION,
    acceptance_plan,
    acceptance_scene,
    canonical_hash,
    task_status,
    validate_acceptance,
)
from scene_factory.backends.isaac_interaction import (
    IsaacInteractionExecutor,
    _IsaacInteractionConfig,
)
from scene_factory.execution import (
    EXECUTION_TRACE_SCHEMA_VERSION,
    ExecutionCommand,
    ExecutionStepResult,
    ExecutionTrace,
    ExecutionTraceStep,
    validate_execution_trace,
)
from tools import validate_p1_4b_executor as runner
from tools import validate_p1_4c_acceptance as cli

HEAD = "a" * 40
SOURCE = {"git_head": HEAD, "clean": True, "source_sha256": "b" * 64}
LAYERS = [
    {"asset_relative_path": BINDING.asset_relative_path, "sha256": "c" * 64},
    {
        "asset_relative_path": "Isaac/Robots/FrankaRobotics/FrankaPanda/franka.usd",
        "sha256": "d" * 64,
    },
]


def snapshot(q):
    positions = {BINDING.semantic_asset_id: {BINDING.semantic_joint_id: q}}
    return {
        "joint_positions": positions,
        "articulation_positions": positions,
        "holding": None,
        "execution_joint_write_count": 0,
        "physical": True,
    }


def synthetic_report(pid, *, negative=False, position=0.349):
    # Deliberately synthetic data to test the audit algorithm, never runtime evidence.
    config = {
        "binding": BINDING.to_dict(),
        "target_position_m": 0.35,
        "target_range_m": [0.32, 0.38],
        "seed": 0,
        "controller": asdict(_IsaacInteractionConfig()),
        "usd_layers": LAYERS,
    }
    report = {
        "report_version": REPORT_VERSION,
        "mode": "negative" if negative else "positive",
        "git_head": HEAD,
        "result": "passed",
        "errors": [],
        "source_before": dict(SOURCE),
        "source_after": dict(SOURCE),
        "configuration": config,
        "configuration_sha256": canonical_hash(config),
        "runtime": {"isaac_version": "6.0.1.0", "diagnostics": {"official_franka_asset": True}},
        "run_id": str(uuid.uuid4()),
        "process_id": pid,
        "process_returncode": 0,
        "closed_cleanly": True,
        "initial_snapshot": snapshot(0),
        "snapshot": snapshot(0 if negative else position),
        "actions": {},
    }
    if negative:
        report["actions"]["pull_without_grasp"] = {
            "status": "failed",
            "reason": "pull_before_grasp",
            "evidence": {"execution_joint_write_count": 0},
        }
        return report
    plan = acceptance_plan()
    report["task_status"] = task_status(report["snapshot"])
    steps = []
    for action in plan.steps:
        evidence = {"execution_joint_write_count": 0}
        if action.action == "grasp":
            evidence.update(
                left_contact=True,
                right_contact=True,
                contact_force_valid=True,
                opposed_contact=True,
            )
        if action.action == "pull":
            evidence.update(
                contact_maintained=True,
                positive_axial_traction_observed=True,
                drawer_joint_delta=position,
                drawer_joint_after=position,
            )
        if action.action == "release":
            evidence.update(
                gripper_open=True,
                contact_separated=True,
                drawer_position_release=position,
                stability_samples=[
                    {
                        "physics_step": i,
                        "joint_position_m": position,
                        "joint_velocity_m_s": 0.0,
                        "contact_separated": True,
                        "contact_observation_valid": True,
                    }
                    for i in range(100, 130)
                ],
            )
        command = ExecutionCommand.from_action(plan.plan_sha256, action)
        result = ExecutionStepResult(
            command.command_id, command.step_id, "succeeded", None, evidence
        )
        report["actions"][action.action] = result.to_dict()
        steps.append(ExecutionTraceStep(command, result))
    trace = ExecutionTrace(
        EXECUTION_TRACE_SCHEMA_VERSION,
        plan.plan_sha256,
        acceptance_scene()["scene_id"],
        IsaacInteractionExecutor().capabilities(),
        "passed",
        tuple(steps),
        report["snapshot"],
        report["task_status"],
    )
    report["execution_trace"] = trace.to_dict()
    return report


def bundle():
    return [synthetic_report(101), synthetic_report(102), synthetic_report(103, negative=True)]


def test_frozen_plan_and_valid_synthetic_evidence():
    assert acceptance_plan().steps[2].target_position == 0.35
    result = validate_acceptance(*bundle(), expected_head=HEAD)
    assert result["result"] == "passed", result
    assert result["physical_runs_executed_by_this_gate"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("closed_cleanly", False),
        ("process_returncode", 1),
        ("git_head", "e" * 40),
        ("report_version", "scene_factory.p1_4b_executor_acceptance.v2"),
        ("execution_trace", {}),
        ("task_status", {"task_success": True}),
    ],
)
def test_rejects_incomplete_or_mismatched_reports(field, value):
    reports = bundle()
    reports[0][field] = value
    assert validate_acceptance(*reports, expected_head=HEAD)["result"] == "failed"


@pytest.mark.parametrize(
    "fault",
    [
        "dirty",
        "source_changed",
        "config_changed",
        "no_robot_hash",
        "fake_runtime",
        "reused_process",
        "reused_run",
        "writes",
        "negative_motion",
        "fifty_mm",
        "missing_sample",
        "nonconsecutive",
        "velocity",
        "drift",
        "recontact",
        "invalid_contact",
        "nan",
    ],
)
def test_rejects_unsafe_evidence(fault):
    reports = bundle()
    report = reports[0]
    samples = report["actions"]["release"]["evidence"]["stability_samples"]
    if fault == "dirty":
        report["source_before"]["clean"] = False
    elif fault == "source_changed":
        report["source_after"]["source_sha256"] = "e" * 64
    elif fault == "config_changed":
        report["configuration"]["seed"] = 1
    elif fault == "no_robot_hash":
        report["configuration"]["usd_layers"] = [LAYERS[0]]
        report["configuration_sha256"] = canonical_hash(report["configuration"])
    elif fault == "fake_runtime":
        report["runtime"]["isaac_version"] += "-fake"
    elif fault == "reused_process":
        reports[1]["process_id"] = report["process_id"]
    elif fault == "reused_run":
        reports[1]["run_id"] = report["run_id"]
    elif fault == "writes":
        report["snapshot"]["execution_joint_write_count"] = 1
    elif fault == "negative_motion":
        reports[2]["snapshot"] = snapshot(0.001)
    elif fault == "fifty_mm":
        report["snapshot"] = snapshot(0.05)
    elif fault == "missing_sample":
        samples.pop()
    elif fault == "nonconsecutive":
        samples[-1]["physics_step"] += 1
    elif fault == "velocity":
        samples[-1]["joint_velocity_m_s"] = 0.01001
    elif fault == "drift":
        samples[-1]["joint_position_m"] += 0.006
    elif fault == "recontact":
        samples[-1]["contact_separated"] = False
    elif fault == "invalid_contact":
        samples[-1]["contact_observation_valid"] = False
    elif fault == "nan":
        samples[-1]["joint_velocity_m_s"] = float("nan")
    result = validate_acceptance(*reports, expected_head=HEAD)
    assert result["result"] == "failed", (fault, result)


def test_physical_trace_uses_measured_goal_range_not_exact_symbolic_target():
    report = synthetic_report(101, position=0.349)
    validation = validate_execution_trace(
        acceptance_scene(), acceptance_plan(), report["execution_trace"]
    )
    assert validation.valid, validation.to_dict()
    trace = validation.trace
    dry = replace(trace, executor=replace(trace.executor, physical=False), trace_sha256="")
    assert not validate_execution_trace(acceptance_scene(), acceptance_plan(), dry).valid
    bad = replace(trace, final_evidence=snapshot(0.05), trace_sha256="")
    assert not validate_execution_trace(acceptance_scene(), acceptance_plan(), bad).valid
    missing = replace(trace, final_evidence={}, trace_sha256="")
    assert not validate_execution_trace(acceptance_scene(), acceptance_plan(), missing).valid


@pytest.mark.parametrize("text", ['{"x":1,"x":2}', '{"x":NaN}', "[]"])
def test_cli_rejects_ambiguous_json(tmp_path, text):
    source = tmp_path / "source.json"
    source.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        cli.read_report(source)


def test_cli_round_trip_and_overwrite_guard(tmp_path):
    paths = [tmp_path / f"{name}.json" for name in ("run1", "run2", "negative")]
    for path, report in zip(paths, bundle()):
        path.write_text(json.dumps(report), encoding="utf-8")
    args = [
        "--run1",
        str(paths[0]),
        "--run2",
        str(paths[1]),
        "--negative",
        str(paths[2]),
        "--expected-head",
        HEAD,
    ]
    out = tmp_path / "audit.json"
    assert cli.main(args + ["--report", str(out)]) == 0
    assert json.loads(out.read_text())["result"] == "passed"
    with pytest.raises(SystemExit):
        cli.main(args + ["--report", str(paths[0])])


def test_child_negative_close_failure_cannot_exit_zero(tmp_path):
    class Executor:
        _config = _IsaacInteractionConfig()

        def capabilities(self):
            return IsaacInteractionExecutor().capabilities()

        def reset(self, *args):
            pass

        def snapshot(self):
            return dict(
                snapshot(0),
                isaac_version="6.0.1.0",
                runtime_diagnostics={"official_franka_asset": True},
            )

        def execute(self, command):
            return ExecutionStepResult(
                command.command_id,
                command.step_id,
                "failed",
                "pull_before_grasp",
                {"execution_joint_write_count": 0},
            )

        def close(self):
            raise RuntimeError("injected shutdown failure")

    with (
        patch.object(runner, "IsaacInteractionExecutor", return_value=Executor()),
        patch.object(runner, "_git_head", return_value=HEAD),
        patch.object(runner, "_source_provenance", return_value=SOURCE),
        patch.object(runner, "_asset_manifest", return_value=LAYERS),
    ):
        path = tmp_path / "negative.json"
        assert runner._run_child(path, negative=True, expected_head=HEAD) == 2
    report = json.loads(path.read_text())
    assert not report["closed_cleanly"]
    assert report["result"] == "failed"


def test_spawn_persists_actual_exit_code_and_refuses_overwrite(tmp_path):
    path, log = tmp_path / "child.json", tmp_path / "child.log"

    def process(*args, **kwargs):
        assert kwargs["timeout"] == 1200
        path.write_text(json.dumps({"result": "passed"}), encoding="utf-8")
        return runner.subprocess.CompletedProcess(args, 5)

    with patch.object(runner.subprocess, "run", side_effect=process):
        result = runner._spawn_child(Path("python"), path, log, negative=False, expected_head=HEAD)
    assert result["result"] == "failed"
    assert json.loads(path.read_text())["process_returncode"] == 5
    with pytest.raises(FileExistsError):
        runner._spawn_child(Path("python"), path, log, negative=False, expected_head=HEAD)


def test_malformed_report_fails_closed():
    for malformed in ({}, {"configuration": None}, {"source_before": []}):
        assert (
            validate_acceptance(malformed, *bundle()[1:], expected_head=HEAD)["result"] == "failed"
        )


def test_child_records_a_valid_terminal_failure_trace(tmp_path):
    class Executor:
        _config = _IsaacInteractionConfig()

        def capabilities(self):
            return IsaacInteractionExecutor().capabilities()

        def reset(self, *args):
            pass

        def snapshot(self):
            return dict(
                snapshot(0),
                isaac_version="6.0.1.0",
                runtime_diagnostics={"official_franka_asset": True},
            )

        def execute(self, command):
            return ExecutionStepResult(
                command.command_id,
                command.step_id,
                "failed",
                "motion_convergence_failed",
                {"execution_joint_write_count": 0},
            )

        def close(self):
            pass

    with (
        patch.object(runner, "IsaacInteractionExecutor", return_value=Executor()),
        patch.object(runner, "_git_head", return_value=HEAD),
        patch.object(runner, "_source_provenance", return_value=SOURCE),
        patch.object(runner, "_asset_manifest", return_value=LAYERS),
    ):
        path = tmp_path / "positive.json"
        assert runner._run_child(path, negative=False, expected_head=HEAD) == 2
    report = json.loads(path.read_text())
    assert report["trace_validation"]["valid"] is True
    assert report["execution_trace"]["result"] == "failed"
    assert len(report["execution_trace"]["steps"]) == 1
    assert report["failure_reason"] == "motion_convergence_failed"
