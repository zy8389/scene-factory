"""Fail-closed, simulator-free evidence gate for the frozen P1-4 drawer task.

Validating a report never runs physics. A PASS means that all required recorded
observations satisfy the contract, not that this module independently measured
the simulator. Runtime reports and their logs must remain available for review.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any

from scene_factory.backends.isaac_binding import (
    SEKTION_TOP_DRAWER_BINDING as BINDING,
    SEKTION_TOP_DRAWER_SEMANTICS as SEMANTICS,
)
from scene_factory.execution import validate_execution_trace
from scene_factory.planning import plan_interaction
from scene_factory.tasks import TaskEvaluator

REPORT_VERSION = "scene_factory.p1_4b_executor_acceptance.v4"
GATE_VERSION = "scene_factory.p1_4c_evidence_gate.v2"
TARGET_POSITION_M = 0.35
TARGET_RANGE_M = (0.32, 0.38)
RELEASE_STEPS = 30
RELEASE_MAX_VELOCITY_M_S = 0.01
RELEASE_MAX_DRIFT_M = 0.005


def canonical_hash(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def acceptance_scene() -> dict[str, Any]:
    semantic = SEMANTICS.to_dict()
    joint = dict(semantic["joint"])
    joint["position"] = joint.pop("default_position")
    return {
        "scene_id": "p1_4_sektion_drawer_open",
        "seed": 0,
        "recipe_name": "p1_4_reference_fixture",
        "objects": [
            {
                "object_id": BINDING.semantic_asset_id,
                "asset_id": BINDING.semantic_asset_id,
                "interactions": {
                    "joints": [joint],
                    "regions": [
                        {
                            "region_id": semantic["region_id"],
                            "kind": "handle",
                            "link": semantic["region_link"],
                            "controlled_joint": BINDING.semantic_joint_id,
                            "allowed_actions": semantic["allowed_actions"],
                        }
                    ],
                    "semantic_states": semantic["states"],
                },
            }
        ],
    }


def acceptance_plan():
    planned = plan_interaction(
        acceptance_scene(), object_id=BINDING.semantic_asset_id, state="open"
    )
    if not planned.valid or planned.plan is None:
        raise ValueError(f"invalid frozen acceptance plan: {planned.to_dict()}")
    return planned.plan


def task_status(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    evaluator = TaskEvaluator(
        {
            "success": {
                "predicate": "articulation_state",
                "object_id": BINDING.semantic_asset_id,
                "joint": BINDING.semantic_joint_id,
                "state": "open",
                "range": list(TARGET_RANGE_M),
            }
        },
        {},
    )
    return evaluator.status({}, {"articulation_positions": snapshot.get("joint_positions", {})})


def _number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _between(value: Any, low: float, high: float) -> bool:
    return _number(value) and low <= value <= high


def _sha(value: Any, length: int = 64) -> bool:
    return isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None


def _joint(snapshot: Mapping[str, Any]) -> Any:
    return (
        snapshot.get("joint_positions", {})
        .get(BINDING.semantic_asset_id, {})
        .get(BINDING.semantic_joint_id)
    )


def common_checks(report: Mapping[str, Any], expected_head: str) -> dict[str, bool]:
    before, after = report.get("source_before", {}), report.get("source_after", {})
    preclose = report.get("source_before_shutdown", {})
    config = report.get("configuration", {})
    layers = config.get("usd_layers", [])
    return {
        "report_version": report.get("report_version") == REPORT_VERSION,
        "report_passed": report.get("result") == "passed" and report.get("errors") == [],
        "clean_exit": report.get("process_returncode") == 0
        and report.get("parent_observed_exit") is True
        and report.get("evidence_persisted_before_shutdown") is True
        and report.get("shutdown_requested") is True
        and report.get("shutdown_mode") == "kit_fast_shutdown",
        "exact_clean_commit": _sha(expected_head, 40)
        and report.get("git_head") == expected_head
        and before.get("git_head") == after.get("git_head") == expected_head
        and before.get("clean") is True
        and after.get("clean") is True
        and preclose == before,
        "unchanged_source": _sha(before.get("source_sha256"))
        and before.get("source_sha256") == after.get("source_sha256"),
        "configuration_hash": _sha(report.get("configuration_sha256"))
        and report.get("configuration_sha256") == canonical_hash(config),
        "frozen_binding": config.get("binding") == BINDING.to_dict(),
        "frozen_goal": config.get("target_position_m") == TARGET_POSITION_M
        and config.get("target_range_m") == list(TARGET_RANGE_M)
        and config.get("seed") == 0,
        "physics_dt": config.get("controller", {}).get("physics_dt") == 1.0 / 60.0,
        "usd_layer_hashes": isinstance(layers, list)
        and bool(layers)
        and all(
            isinstance(item, dict)
            and isinstance(item.get("asset_relative_path"), str)
            and _sha(item.get("sha256"))
            for item in layers
        )
        and any(item.get("asset_relative_path") == BINDING.asset_relative_path for item in layers)
        and any(
            item.get("asset_relative_path") == "Isaac/Robots/FrankaRobotics/FrankaPanda/franka.usd"
            for item in layers
        ),
        "reference_runtime": report.get("runtime", {}).get("isaac_version") == "6.0.1.0"
        and report.get("runtime", {}).get("diagnostics", {}).get("official_franka_asset") is True,
        "run_identity": isinstance(report.get("run_id"), str)
        and bool(report.get("run_id"))
        and isinstance(report.get("process_id"), int)
        and not isinstance(report.get("process_id"), bool)
        and report.get("process_id", 0) > 0,
    }


def positive_checks(report: Mapping[str, Any], expected_head: str) -> dict[str, bool]:
    checks = common_checks(report, expected_head)
    actions = report.get("actions", {})
    grasp = actions.get("grasp", {}).get("evidence", {})
    pull = actions.get("pull", {}).get("evidence", {})
    release = actions.get("release", {}).get("evidence", {})
    snapshot, initial = report.get("snapshot", {}), report.get("initial_snapshot", {})
    stability = release.get("stability_samples", [])
    release_position = release.get("drawer_position_release")
    stable = (
        isinstance(stability, list)
        and len(stability) == RELEASE_STEPS
        and _number(release_position)
    )
    if stable:
        steps = [sample.get("physics_step") for sample in stability]
        stable = all(isinstance(step, int) and not isinstance(step, bool) for step in steps)
        stable = stable and all(b == a + 1 for a, b in zip(steps, steps[1:]))
        stable = stable and all(
            _between(s.get("joint_position_m"), *TARGET_RANGE_M)
            and _between(
                s.get("joint_velocity_m_s"), -RELEASE_MAX_VELOCITY_M_S, RELEASE_MAX_VELOCITY_M_S
            )
            and abs(s["joint_position_m"] - release_position) <= RELEASE_MAX_DRIFT_M
            and s.get("contact_separated") is True
            and s.get("contact_observation_valid") is True
            for s in stability
        )
    validated = validate_execution_trace(
        acceptance_scene(), acceptance_plan(), report.get("execution_trace", {})
    )
    trace = validated.trace
    correlated = (
        trace is not None
        and len(trace.steps) == 4
        and all(
            actions.get(step.command.action.action) == step.result.to_dict() for step in trace.steps
        )
    )
    status = task_status(snapshot)
    checks.update(
        {
            "positive_mode": report.get("mode") == "positive",
            "initial_closed": _between(_joint(initial), 0.0, 0.02),
            "four_successful_actions": all(
                actions.get(name, {}).get("status") == "succeeded"
                for name in ("approach", "grasp", "pull", "release")
            ),
            "opposed_grasp": grasp.get("left_contact") is True
            and grasp.get("right_contact") is True
            and grasp.get("contact_force_valid") is True
            and (
                grasp.get("opposed_contact") is True
                or grasp.get("contact_diagnostics", {}).get("opposed_contact") is True
            ),
            "contact_driven_pull": pull.get("contact_maintained") is True
            and pull.get("positive_axial_traction_observed") is True
            and _between(pull.get("drawer_joint_delta"), 0.30, 0.40),
            "open_range_not_5cm_pilot": _between(pull.get("drawer_joint_after"), *TARGET_RANGE_M)
            and _between(_joint(snapshot), *TARGET_RANGE_M),
            "no_execution_writes": initial.get("execution_joint_write_count") == 0
            and snapshot.get("execution_joint_write_count") == 0
            and all(
                actions.get(name, {}).get("evidence", {}).get("execution_joint_write_count") == 0
                for name in ("approach", "grasp", "pull", "release")
            ),
            "released": release.get("gripper_open") is True
            and release.get("contact_separated") is True
            and snapshot.get("holding") is None,
            "thirty_consecutive_stable_steps": bool(stable),
            "terminal_task_evaluator": status.get("task_success") is True
            and report.get("task_status") == status,
            "correlated_valid_trace": validated.valid
            and validated.trace_result == "passed"
            and validated.physical_execution
            and correlated
            and trace.final_evidence == snapshot
            and trace.goal_status == status,
        }
    )
    return checks


def negative_checks(report: Mapping[str, Any], expected_head: str) -> dict[str, bool]:
    checks = common_checks(report, expected_head)
    action = report.get("actions", {}).get("pull_without_grasp", {})
    initial, final = report.get("initial_snapshot", {}), report.get("snapshot", {})
    q0, q1 = _joint(initial), _joint(final)
    checks.update(
        {
            "negative_mode": report.get("mode") == "negative",
        "initial_closed": _between(q0, 0.0, 0.02),
            "rejected_before_grasp": action.get("status") == "failed"
            and action.get("reason") == "pull_before_grasp",
            "no_drawer_motion": _number(q0) and _number(q1) and abs(q1 - q0) <= 1.0e-6,
            "no_execution_writes": initial.get("execution_joint_write_count") == 0
            and final.get("execution_joint_write_count") == 0
            and action.get("evidence", {}).get("execution_joint_write_count") == 0,
        }
    )
    return checks


def validate_acceptance(
    run1: Mapping[str, Any],
    run2: Mapping[str, Any],
    negative: Mapping[str, Any],
    *,
    expected_head: str,
) -> dict[str, Any]:
    groups: dict[str, dict[str, bool]] = {}
    for name, report, check in (
        ("run1", run1, positive_checks),
        ("run2", run2, positive_checks),
        ("negative", negative, negative_checks),
    ):
        try:
            groups[name] = check(report, expected_head)
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            groups[name] = {"well_formed_report": False}
    reports = (run1, run2, negative)
    try:
        groups["repeatability"] = {
            "three_independent_runs": len({r.get("run_id") for r in reports}) == 3
            and len({r.get("process_id") for r in reports}) == 3,
            "same_configuration": _sha(run1.get("configuration_sha256"))
            and len({r.get("configuration_sha256") for r in reports}) == 1,
            "same_sources": _sha(run1.get("source_before", {}).get("source_sha256"))
            and len({r.get("source_before", {}).get("source_sha256") for r in reports}) == 1,
        }
    except (TypeError, AttributeError):
        groups["repeatability"] = {"well_formed_run_identities": False}
    failed = [
        f"{name}.{key}"
        for name, checks in groups.items()
        for key, value in checks.items()
        if value is not True
    ]
    return {
        "report_version": GATE_VERSION,
        "result": "failed" if failed else "passed",
        "expected_git_head": expected_head,
        "scope": "offline audit of recorded physical evidence",
        "physical_runs_executed_by_this_gate": False,
        "checks": groups,
        "failed_checks": failed,
    }
