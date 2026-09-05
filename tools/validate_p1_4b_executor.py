"""Run the P1-4B frozen full-drawer acceptance in fresh Isaac processes.

The parent process intentionally imports no Isaac modules.  Each runtime child
owns one clean ``IsaacInteractionExecutor`` lifecycle and writes its report
before Kit teardown, which keeps failures and shutdown behavior auditable.
"""

from __future__ import annotations

import argparse
import hashlib
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
import os
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scene_factory.backends.isaac_binding import (  # noqa: E402
    SEKTION_TOP_DRAWER_BINDING,
    SEKTION_TOP_DRAWER_RUNTIME_ROOT,
)
from scene_factory.backends.isaac_interaction import (  # noqa: E402
    IsaacInteractionExecutor,
    _IsaacInteractionConfig,
    _IsaacInteractionRuntime,
    _ORIENTATION_FAMILY_DEGREES,
    _ORIENTATION_REFINEMENT_DEGREES,
    _PULL_JOINT_CONVERGENCE_RAD,
    _branch_continuity_diagnostic,
    _compose_transform,
    _continuity_policy,
    _displacement_accounting,
    _fixture_candidate_validity,
    _inverse_transform,
    _joint_delta_metrics,
    _joint_limit_diagnostic,
    _matrix_to_quaternion,
    _pose_transform,
    _rank_fixture_candidates,
    _rank_long_horizon_orientation_candidates,
)
from scene_factory.execution import (  # noqa: E402
    EXECUTION_TRACE_SCHEMA_VERSION, ExecutionCommand, ExecutionTrace, ExecutionTraceStep,
    validate_execution_trace,
)
from scene_factory.backends.isaac_acceptance import (  # noqa: E402
    REPORT_VERSION, TARGET_POSITION_M, TARGET_RANGE_M, acceptance_plan, acceptance_scene,
    canonical_hash, task_status, positive_checks, negative_checks, validate_acceptance,
)
from scene_factory.planning import InteractionAction, InteractionWorldState  # noqa: E402
from scene_factory.robotics import quaternion_angular_distance  # noqa: E402


_ACTIONS = ("approach", "grasp", "pull", "release")
_ORIENTATION_REFINEMENT_SWEEP_DEGREES = (-25, -20, -10, -5, 5, 10, 20, 25)
_BASELINE_BRANCH_PREVIOUS_Q = (
    0.6474552804268174,
    1.3244498533210853,
    1.8888707080112561,
    -3.0429288252356628,
    2.8402357733408956,
    2.789661896431579,
    -1.684788295328363,
)
_BASELINE_PHYSICAL_GRASP_TERMINAL_Q = (
    0.646114706993103,
    1.3132420778274536,
    1.901023268699646,
    -3.045461654663086,
    2.8374855518341064,
    2.7785139083862305,
    -1.6528061628341675,
)
_BASELINE_PHYSICAL_GRASP_BASE_POSITION_M = (0.7, 0.0, 0.78)
_BASELINE_BRANCH_OBSERVED_Q = (
    0.6459134221076965,
    1.3197474479675293,
    1.8946889638900757,
    -3.0451910495758057,
    2.8371288776397705,
    2.783684730529785,
    -1.6605244874954224,
)
_BASELINE_BRANCH_SWITCHED_Q = (
    -2.494893297780138,
    -1.3300600448344424,
    -1.2581454241756633,
    -3.0430682855469335,
    2.8402053632125526,
    2.7939313346380885,
    -1.6921317073516609,
)
_BASELINE_PREVIOUS_TARGET = (0.346151081434247, -0.012099951402307983, 1.105898088167334)
_BASELINE_SWITCHED_TARGET = (0.346699808407284, -0.012099951402307983, 1.105898088167334)


def _baseline_branch_fk_evidence(
    runtime: _IsaacInteractionRuntime,
    orientation: tuple[float, ...],
) -> dict[str, Any]:
    previous_fk = dict(runtime.compute_arm_fk(_BASELINE_BRANCH_PREVIOUS_Q))
    switched_fk = dict(runtime.compute_arm_fk(_BASELINE_BRANCH_SWITCHED_Q))
    joint_names = runtime.arm_joint_names()
    continuity = _branch_continuity_diagnostic(
        _BASELINE_BRANCH_SWITCHED_Q,
        _BASELINE_BRANCH_OBSERVED_Q,
        _BASELINE_BRANCH_PREVIOUS_Q,
        joint_names=joint_names,
        cartesian_target_delta_m=math.dist(
            _BASELINE_PREVIOUS_TARGET,
            _BASELINE_SWITCHED_TARGET,
        ),
    )
    previous_fk["target_position"] = list(_BASELINE_PREVIOUS_TARGET)
    previous_fk["position_error_m"] = math.dist(
        previous_fk["position"],
        _BASELINE_PREVIOUS_TARGET,
    )
    previous_fk["orientation_error_rad"] = quaternion_angular_distance(
        previous_fk["orientation_wxyz"],
        orientation,
    )
    switched_fk["target_position"] = list(_BASELINE_SWITCHED_TARGET)
    switched_fk["position_error_m"] = math.dist(
        switched_fk["position"],
        _BASELINE_SWITCHED_TARGET,
    )
    switched_fk["orientation_error_rad"] = quaternion_angular_distance(
        switched_fk["orientation_wxyz"],
        orientation,
    )
    return {
        "source": "guarded_fresh_3mm_reproduction_20260831",
        "failure_physics_step": 325,
        "joint_names": joint_names,
        "q_previous": list(_BASELINE_BRANCH_PREVIOUS_Q),
        "q_observed": list(_BASELINE_BRANCH_OBSERVED_Q),
        "q_new": list(_BASELINE_BRANCH_SWITCHED_Q),
        "previous_target_position": list(_BASELINE_PREVIOUS_TARGET),
        "switched_target_position": list(_BASELINE_SWITCHED_TARGET),
        "continuity": continuity,
        "fk_previous": previous_fk,
        "fk_new": switched_fk,
        "fk_position_separation_m": math.dist(previous_fk["position"], switched_fk["position"]),
        "fk_orientation_separation_rad": quaternion_angular_distance(
            previous_fk["orientation_wxyz"],
            switched_fk["orientation_wxyz"],
        ),
        "multi_branch_ik_discontinuity": (
            continuity["branch_jump_detected"]
            and math.dist(previous_fk["position"], switched_fk["position"]) <= 0.002
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the P1-4B physical Isaac interaction primitive acceptance."
    )
    parser.add_argument(
        "--report",
        type=Path,
        required=True,
        help="Final report path outside the repository",
    )
    parser.add_argument(
        "--isaac-python",
        type=Path,
        default=Path(sys.executable),
        help="Isaac Sim Python executable used for fresh runtime children",
    )
    parser.add_argument(
        "--runtime-only",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--negative-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--kinematics-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--motion-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--static-grasp", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--traction-mm",
        type=int,
        choices=(1, 3, 5, 10, 15, 20, 30, 35, 40, 50),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--fixture-base-x-m", type=float, help=argparse.SUPPRESS)
    parser.add_argument("--fixture-base-y-m", type=float, help=argparse.SUPPRESS)
    parser.add_argument("--fixture-base-z-m", type=float, help=argparse.SUPPRESS)
    parser.add_argument("--fixture-base-yaw-deg", type=float, help=argparse.SUPPRESS)
    parser.add_argument(
        "--orientation-angle-deg",
        type=int,
        choices=_ORIENTATION_REFINEMENT_DEGREES,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--orientation-sweep", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--orientation-refinement-sweep",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--fixture-kinematics", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--fixture-sensitivity", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--fixture-coarse-search", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--fixture-refinement-search", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--child-report", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--head", help=argparse.SUPPRESS)
    return parser


def _write(path: Path, payload: MappingLike) -> None:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


class MappingLike(dict[str, Any]):
    """Typing-only alias that keeps report construction ordinary dictionaries."""


def _fixture_base_position_from_args(args: argparse.Namespace) -> tuple[float, float, float]:
    default = _IsaacInteractionConfig.fixture_robot_base_position_m
    return (
        default[0] if args.fixture_base_x_m is None else args.fixture_base_x_m,
        default[1] if args.fixture_base_y_m is None else args.fixture_base_y_m,
        default[2] if args.fixture_base_z_m is None else args.fixture_base_z_m,
    )


def _fixture_override(
    base_position_m: tuple[float, float, float] | None,
    base_yaw_deg: float | None,
    orientation_angle_deg: int | None = None,
) -> dict[str, Any] | None:
    if base_position_m is None and base_yaw_deg is None and orientation_angle_deg is None:
        return None
    fixture: dict[str, Any] = {}
    if base_position_m is not None:
        fixture["fixture_robot_base_position_m"] = base_position_m
    if base_yaw_deg is not None:
        half_yaw = math.radians(float(base_yaw_deg)) * 0.5
        fixture["fixture_robot_base_orientation_wxyz"] = (
            math.cos(half_yaw),
            0.0,
            0.0,
            math.sin(half_yaw),
        )
    if orientation_angle_deg is not None:
        fixture["grasp_topology"] = "top_bottom_pinch"
        fixture["orientation_family_angle_deg"] = orientation_angle_deg
    return fixture


def _git_head() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _outside_repository(path: Path) -> bool:
    try:
        path.expanduser().resolve().relative_to(PROJECT_ROOT)
    except ValueError:
        return True
    return False


def _initial_state() -> InteractionWorldState:
    return InteractionWorldState(
        joint_positions={
            SEKTION_TOP_DRAWER_BINDING.semantic_asset_id: {
                SEKTION_TOP_DRAWER_BINDING.semantic_joint_id: SEKTION_TOP_DRAWER_BINDING.expected_default_position,
            }
        }
    )


def _command(action: str, step_id: int, *, target: float | None = None) -> ExecutionCommand:
    return ExecutionCommand.from_action(
        "b" * 64,
        InteractionAction(
            step_id=step_id,
            action=action,
            object_id=SEKTION_TOP_DRAWER_BINDING.semantic_asset_id,
            region_id=SEKTION_TOP_DRAWER_BINDING.semantic_region_id,
            joint_id=SEKTION_TOP_DRAWER_BINDING.semantic_joint_id if action == "pull" else None,
            target_position=target,
        ),
    )


def _result_summary(result: Any) -> dict[str, Any]:
    return result.to_dict() if hasattr(result, "to_dict") else dict(result)


def _source_provenance() -> dict[str, Any]:
    files = subprocess.check_output(["git", "ls-files", "-z"], cwd=PROJECT_ROOT).split(b"\0")
    digest = hashlib.sha256()
    for raw_path in sorted(path for path in files if path):
        path = PROJECT_ROOT / raw_path.decode("utf-8")
        digest.update(raw_path + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    clean = not subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=PROJECT_ROOT
    ).strip()
    return {"git_head": _git_head(), "clean": clean, "source_sha256": digest.hexdigest()}


def _asset_manifest(executor: IsaacInteractionExecutor) -> list[dict[str, str]]:
    # Immutable on-disk USD dependencies, not anonymous session/root layers.
    root = Path(os.environ["ISAACSIM_ASSET_ROOT"]).resolve()
    layers = []
    for layer in executor._runtime._stage.GetUsedLayers():
        if layer.anonymous:
            continue
        path = Path(layer.realPath).resolve(strict=True)
        relative = path.relative_to(root).as_posix()
        layers.append({"asset_relative_path": relative,
                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return sorted(layers, key=lambda item: item["asset_relative_path"])


def _run_child(report_path: Path, *, negative: bool, expected_head: str | None) -> int:
    if report_path.exists():
        raise FileExistsError(f"refusing to overwrite runtime evidence: {report_path}")
    report: dict[str, Any] = {
        "report_version": REPORT_VERSION, "result": "failed",
        "mode": "negative" if negative else "positive", "git_head": _git_head(),
        "expected_git_head": expected_head, "runtime": {}, "actions": {}, "checks": {},
        "errors": [], "closed_cleanly": False, "run_id": str(uuid.uuid4()),
        "process_id": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat(),
    }
    executor: IsaacInteractionExecutor | None = None
    plan, scene = acceptance_plan(), acceptance_scene()
    records = []
    failure_reason = "runtime_not_completed"
    try:
        report["source_before"] = _source_provenance()
        if (not expected_head or report["git_head"] != expected_head
                or not report["source_before"]["clean"]):
            raise ValueError("full acceptance requires the expected exact clean commit")
        executor = IsaacInteractionExecutor()
        report["capabilities"] = executor.capabilities().to_dict()
        executor.reset(scene, _initial_state())
        initial_snapshot = executor.snapshot()
        report["initial_snapshot"] = initial_snapshot
        report["runtime"] = {
            "binding_id": SEKTION_TOP_DRAWER_BINDING.binding_id,
            "isaac_version": initial_snapshot.get("isaac_version"),
            "diagnostics": initial_snapshot.get("runtime_diagnostics", {}),
        }
        report["configuration"] = {
            "binding": SEKTION_TOP_DRAWER_BINDING.to_dict(),
            "target_position_m": TARGET_POSITION_M, "target_range_m": list(TARGET_RANGE_M),
            "seed": 0, "controller": asdict(executor._config),
            "usd_layers": _asset_manifest(executor),
        }
        report["configuration_sha256"] = canonical_hash(report["configuration"])
        if negative:
            result = executor.execute(_command("pull", 0, target=TARGET_POSITION_M))
            report["actions"]["pull_without_grasp"] = result.to_dict()
            failure_reason = None if result.status == "failed" and result.reason == "pull_before_grasp" else "negative_not_rejected"
        else:
            for action in plan.steps:
                command = ExecutionCommand.from_action(plan.plan_sha256, action)
                result = executor.execute(command)
                records.append(ExecutionTraceStep(command, result))
                report["actions"][action.action] = result.to_dict()
                if result.status != "succeeded":
                    failure_reason = result.reason or "action_failed"
                    break
            else:
                failure_reason = None
        report["snapshot"] = executor.snapshot()
        report["task_status"] = task_status(report["snapshot"])
        if not negative:
            if not report["task_status"]["task_success"] and failure_reason is None:
                failure_reason = "goal_not_observed"
            trace = ExecutionTrace(
                schema_version=EXECUTION_TRACE_SCHEMA_VERSION, plan_sha256=plan.plan_sha256,
                scene_id=scene["scene_id"], executor=executor.capabilities(),
                result="failed" if failure_reason else "passed", steps=tuple(records),
                final_evidence=report["snapshot"], goal_status=report["task_status"],
                failure_reason=("executor_step_failed" if records and records[-1].result.status != "succeeded" else "goal_not_satisfied") if failure_reason else None,
            )
            report["execution_trace"] = trace.to_dict()
            validation = validate_execution_trace(scene, plan, trace)
            report["trace_validation"] = validation.to_dict()
            if not validation.valid:
                failure_reason = failure_reason or "trace_validation_failed"
        if _asset_manifest(executor) != report["configuration"]["usd_layers"]:
            failure_reason = "assets_changed_during_execution"
        report["result"] = "failed" if failure_reason else "passed"
        report["failure_reason"] = failure_reason
    except Exception as exc:
        report["result"] = "failed"
        report["errors"].append({"type": type(exc).__name__, "message": " ".join(str(exc).split())[:1000]})
        report["traceback"] = traceback.format_exc()
    finally:
        # First persist before Kit teardown, then attest that close returned.
        _write(report_path, report)
        if executor is not None:
            try:
                executor.close()
                report["closed_cleanly"] = True
            except Exception as exc:
                report["errors"].append({"type": type(exc).__name__, "message": f"close: {exc}"})
                report["result"] = "failed"
        try:
            report["source_after"] = _source_provenance()
            # The parent independently replaces this provisional exit code
            # with the observed OS exit status in the saved child report.
            preview = dict(report, process_returncode=0)
            checker = negative_checks if negative else positive_checks
            report["checks"] = checker(preview, expected_head or "")
            if not all(report["checks"].values()):
                report["result"] = "failed"
        except Exception as exc:
            report["errors"].append({"type": type(exc).__name__, "message": f"evidence: {exc}"})
            report["result"] = "failed"
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write(report_path, report)
        print("SCENE_FACTORY_P1_4B_REPORT=" + json.dumps(report, ensure_ascii=False, allow_nan=False), flush=True)
    return 0 if report["result"] == "passed" else 2


def _route_targets(plan: dict[str, Any]) -> list[tuple[str, tuple[float, ...]]]:
    entry_waypoints = [tuple(item) for item in plan["pregrasp_entry_waypoints_world"]]
    targets = [
        (
            "pregrasp" if index == len(entry_waypoints) else f"clearance_entry_{index}",
            waypoint,
        )
        for index, waypoint in enumerate(entry_waypoints, start=1)
    ]
    approach_waypoints = [tuple(item) for item in plan["approach_waypoints_world"]]
    if approach_waypoints and approach_waypoints[0] == tuple(plan["pregrasp_position_world"]):
        approach_waypoints = approach_waypoints[1:]
    targets.extend(
        (f"approach_waypoint_{index}", waypoint)
        for index, waypoint in enumerate(approach_waypoints, start=2)
    )
    return targets


def _move_open_route(
    executor: IsaacInteractionExecutor,
    plan: dict[str, Any],
) -> list[dict[str, Any]]:
    orientation = tuple(plan["orientation_wxyz"])
    return [
        {
            "label": label,
            **executor._move_to_pose(
                target,
                executor._config.approach_steps
                if label == "pregrasp"
                else executor._config.grasp_motion_steps,
                orientation,
            ),
        }
        for label, target in _route_targets(plan)
    ]


def _run_kinematics_child(report_path: Path, *, expected_head: str | None) -> int:
    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "result": "failed",
        "mode": "kinematics_only",
        "git_head": _git_head(),
        "expected_git_head": expected_head,
        "runtime": {},
        "checks": {},
        "errors": [],
    }
    runtime: _IsaacInteractionRuntime | None = None
    try:
        runtime = _IsaacInteractionRuntime(_IsaacInteractionConfig(headless=True))
        runtime.reset({"scene_id": "p1_4b_kinematics_only"}, 0.0)
        resolution = runtime.resolve_binding(SEKTION_TOP_DRAWER_BINDING)
        resolution.require_valid()
        diagnostics = dict(runtime.diagnostics(SEKTION_TOP_DRAWER_BINDING))
        analysis = diagnostics.get("grasp_analysis", {})
        selected = analysis.get("selected_candidate")
        candidates = analysis.get("candidates", [])
        top_bottom = next(
            (item for item in candidates if item.get("candidate_id") == "top_bottom_pinch"),
            {},
        )
        report["capabilities"] = {
            "executor": "isaac_interaction",
            "version": "1",
            "physical": True,
            "articulation_execution": True,
            "supported_actions": list(_ACTIONS),
        }
        report["runtime"] = {
            "binding_id": SEKTION_TOP_DRAWER_BINDING.binding_id,
            "runtime_root": SEKTION_TOP_DRAWER_RUNTIME_ROOT,
            "asset_relative_path": SEKTION_TOP_DRAWER_BINDING.asset_relative_path,
            "isaac_version": runtime.isaac_sim_version(),
            "diagnostics": diagnostics,
            "action_sequence": [],
            "contact_attempted": False,
        }
        report["checks"] = {
            "same_git_head": expected_head is None or report["git_head"] == expected_head,
            "asset_root_resolved": diagnostics.get("asset_root_resolution_status") == "resolved",
            "lula_frame_list_complete": all(
                frame in diagnostics.get("lula_frame_names", [])
                for frame in (
                    "panda_hand",
                    "right_gripper",
                    "panda_leftfinger",
                    "panda_rightfinger",
                    "panda_leftfingertip",
                    "panda_rightfingertip",
                )
            ),
            "configured_kinematic_frame_is_right_gripper": diagnostics.get("configured_kinematic_frame") == "right_gripper",
            "controller_target_frame_is_right_gripper": diagnostics.get("controller_target_frame") == "right_gripper",
            "observation_frame_is_panda_hand": diagnostics.get("observation_frame") == "panda_hand",
            "base_pose_synchronized": diagnostics.get("base_pose_synchronized") is True,
            "frame_transform_constant": diagnostics.get("frame_transform_diagnostics", {}).get("constant_transform") is True,
            "top_bottom_geometry_valid": top_bottom.get("geometry_screen", {}).get("valid") is True,
            "top_bottom_ik_valid": top_bottom.get("ik_screen", {}).get("valid") is True,
            "selected_top_bottom": isinstance(selected, dict) and selected.get("topology") == "top_bottom_pinch",
            "selected_orientation_family_angle": isinstance(selected, dict)
            and selected.get("orientation_family_angle_deg") == 15,
            "execution_joint_writes_zero": diagnostics.get("execution_joint_write_count") == 0,
            "no_action_commands": report["runtime"]["action_sequence"] == [],
            "no_contact_attempted": report["runtime"]["contact_attempted"] is False,
        }
        report["result"] = "passed" if all(report["checks"].values()) else "failed"
    except Exception as exc:
        report["errors"].append(
            {
                "type": type(exc).__name__,
                "message": " ".join(str(exc).split())[:1000],
            }
        )
        report["traceback"] = traceback.format_exc()
    finally:
        _write(report_path, report)
        if runtime is not None:
            try:
                runtime.close()
            except Exception as exc:
                report["errors"].append(
                    {"type": type(exc).__name__, "message": f"close: {exc}"}
                )
                report["result"] = "failed"
        _write(report_path, report)
        print("SCENE_FACTORY_P1_4B_KINEMATICS_REPORT=" + json.dumps(report, ensure_ascii=False, allow_nan=False), flush=True)
    return 0 if report["result"] == "passed" else 2


def _run_continuation_kinematics_child(
    report_path: Path,
    *,
    expected_head: str | None,
    distance_mm: int,
    fixture_base_position_m: tuple[float, float, float] | None = None,
    fixture_base_yaw_deg: float | None = None,
    orientation_angle_deg: int | None = None,
) -> int:
    distance_m = distance_mm / 1000.0
    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "result": "failed",
        "mode": "pull_kinematic_continuation",
        "traction_distance_mm": distance_mm,
        "orientation_family_angle_deg": orientation_angle_deg,
        "git_head": _git_head(),
        "expected_git_head": expected_head,
        "segments": [],
        "errors": [],
    }
    executor: IsaacInteractionExecutor | None = None
    try:
        fixture = _fixture_override(
            fixture_base_position_m,
            fixture_base_yaw_deg,
            orientation_angle_deg,
        )
        executor = IsaacInteractionExecutor(headless=True, fixture=fixture)
        executor.reset({"scene_id": f"p1_4b_kinematic_continuation_{distance_mm}mm"}, _initial_state())
        runtime = executor._runtime
        if runtime is None:
            raise RuntimeError("kinematic continuation executor has no runtime")
        required_short_probes = (
            "pre_grasp",
            "grasp",
            "grasp_plus_1mm",
            "grasp_plus_3mm",
            "grasp_plus_5mm",
        )
        analysis = runtime._ensure_grasp_analysis()
        requested_candidate = next(
            (
                candidate
                for candidate in analysis.get("candidates", [])
                if candidate.get("topology") == "top_bottom_pinch"
                and (
                    orientation_angle_deg is None
                    or candidate.get("orientation_family_angle_deg")
                    == orientation_angle_deg
                )
            ),
            None,
        )
        if not isinstance(requested_candidate, dict):
            raise RuntimeError("requested top-bottom orientation candidate was not reconstructed")
        short_probes = requested_candidate.get("ik_screen", {}).get("probes", {})
        short_probe_results = {
            name: isinstance(short_probes.get(name), dict)
            and short_probes[name].get("success") is True
            for name in required_short_probes
        }
        geometry_valid = requested_candidate.get("geometry_screen", {}).get("valid") is True
        report["short_range_probe_results"] = short_probe_results
        report["short_range_ik_5_of_5_pass"] = all(short_probe_results.values())
        report["short_range_pass"] = geometry_valid and report["short_range_ik_5_of_5_pass"]
        report["orientation_screen"] = {
            "geometry_valid": geometry_valid,
            "geometry_reasons": requested_candidate.get("geometry_screen", {}).get(
                "reasons", []
            ),
            "ik_valid": requested_candidate.get("ik_screen", {}).get("valid") is True,
            "short_range_probe_results": short_probe_results,
        }
        if analysis.get("selected_candidate") is None:
            raise RuntimeError(
                "requested top-bottom orientation failed geometry or short-range IK screening"
            )
        plan = executor._grasp_plan()
        orientation = tuple(plan["orientation_wxyz"])
        report["segments"] = _move_open_route(executor, plan)
        route_eef_end = dict(runtime.read_eef_pose())
        route_terminal_q = [
            float(value) for value in runtime.read_robot_joint_positions()
        ]
        configured_base = tuple(executor._config.fixture_robot_base_position_m)
        if (
            configured_base == _BASELINE_PHYSICAL_GRASP_BASE_POSITION_M
            and plan.get("orientation_family_angle_deg") == 15
        ):
            continuation_initial_q = list(_BASELINE_PHYSICAL_GRASP_TERMINAL_Q)
            continuation_initial_q_source = "measured_physical_grasp_terminal_q"
        else:
            continuation_initial_q = route_terminal_q
            continuation_initial_q_source = "equivalent_open_gripper_grasp_endpoint_q"
        eef_start = dict(runtime.compute_arm_fk(continuation_initial_q))
        axis = tuple(float(value) for value in plan["opening_axis_world"])
        axis_norm = math.sqrt(sum(value * value for value in axis))
        axis = tuple(value / axis_norm for value in axis)
        drawer_before = runtime.read_joint()
        path = executor._precompute_pull_ik_path(
            tuple(float(value) for value in eef_start["position"]),
            axis,
            distance_m,
            orientation,
            initial_joint_positions=continuation_initial_q,
        )
        drawer_after = runtime.read_joint()
        contacts = dict(runtime.read_contacts())
        branch_free = path.get("success") is True and all(
            attempt.get("continuity", {}).get("branch_jump_detected") is False
            for attempt in path.get("attempts", [])
            if attempt.get("ik_success") is True
        )
        initial_limit_diagnostic = path.get("initial_joint_limit_diagnostic", {})
        margin_profile = [
            {
                "cartesian_progress_m": 0.0,
                "joint_positions": list(continuation_initial_q),
                "joint_limit_diagnostic": initial_limit_diagnostic,
                "minimum_joint_limit_margin_rad": initial_limit_diagnostic.get(
                    "minimum_joint_limit_margin_rad"
                ),
                "limiting_joint": initial_limit_diagnostic.get("limiting_joint"),
            },
            *[
                {
                    "cartesian_progress_m": waypoint["cartesian_progress_m"],
                    "joint_positions": waypoint["joint_positions"],
                    "joint_limit_diagnostic": waypoint["joint_limit_diagnostic"],
                    "minimum_joint_limit_margin_rad": waypoint[
                        "minimum_joint_limit_margin_rad"
                    ],
                    "limiting_joint": waypoint["limiting_joint"],
                }
                for waypoint in path.get("accepted_waypoints", [])
            ],
        ]
        requested_checkpoints_mm = (0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50)
        checkpoint_diagnostics = []
        for checkpoint_mm in requested_checkpoints_mm:
            if checkpoint_mm > distance_mm:
                continue
            target_progress = checkpoint_mm / 1000.0
            match = next(
                (
                    item
                    for item in margin_profile
                    if abs(float(item["cartesian_progress_m"]) - target_progress)
                    <= 1.0e-9
                ),
                None,
            )
            checkpoint_diagnostics.append(
                {
                    "checkpoint_mm": checkpoint_mm,
                    "reachable": match is not None,
                    "diagnostic": match,
                }
            )
        first_limit_failure = _first_joint_limit_failure(
            path,
            joint_limits=runtime.arm_joint_limits(),
            joint_names=path.get("joint_names", runtime.arm_joint_names()),
        )
        all_profile_margins = [
            float(item["minimum_joint_limit_margin_rad"])
            for item in margin_profile
            if item.get("minimum_joint_limit_margin_rad") is not None
        ]
        joint_path = [
            list(continuation_initial_q),
            *[
                list(waypoint["joint_positions"])
                for waypoint in path.get("accepted_waypoints", [])
            ],
        ]
        joint_space_path_length = sum(
            math.dist(previous, current)
            for previous, current in zip(joint_path, joint_path[1:])
        )
        maximum_waypoint_dq = max(
            (
                max(abs(current - previous) for previous, current in zip(q0, q1))
                for q0, q1 in zip(joint_path, joint_path[1:])
            ),
            default=0.0,
        )
        limiting_profile_sample = (
            min(
                (
                    item
                    for item in margin_profile
                    if item.get("minimum_joint_limit_margin_rad") is not None
                ),
                key=lambda item: float(item["minimum_joint_limit_margin_rad"]),
            )
            if all_profile_margins
            else None
        )
        selected_family_member = next(
            (
                member
                for member in plan.get("orientation_family", [])
                if member.get("degrees_about_closing_axis")
                == plan.get("orientation_family_angle_deg")
            ),
            {},
        )
        report.update(
            {
                "runtime": {
                    "isaac_version": runtime.isaac_sim_version(),
                    "binding_id": SEKTION_TOP_DRAWER_BINDING.binding_id,
                    "runtime_root": SEKTION_TOP_DRAWER_RUNTIME_ROOT,
                    "controller_frame": "right_gripper",
                    "observation_frame": runtime._observation_frame,
                    "ik_api": runtime.ik_api_diagnostics(),
                },
                "selected_grasp": plan,
                "selected_orientation_wxyz": list(orientation),
                "approach_alignment_to_negative_opening": selected_family_member.get(
                    "approach_alignment_to_negative_opening"
                ),
                "route_eef_end": route_eef_end,
                "physical_grasp_terminal_joint_positions": list(
                    continuation_initial_q
                ),
                "continuation_initial_q_source": continuation_initial_q_source,
                "route_terminal_joint_positions": route_terminal_q,
                "fixture_robot_base_position_m": configured_base,
                "fixture_robot_base_orientation_wxyz": tuple(
                    executor._config.fixture_robot_base_orientation_wxyz
                ),
                "fixture_robot_base_yaw_deg": fixture_base_yaw_deg or 0.0,
                "eef_start": eef_start,
                "opening_axis_world": axis,
                "continuity_policy": _continuity_policy(),
                "ik_continuation": path,
                "baseline_branch_evidence": _baseline_branch_fk_evidence(
                    runtime,
                    orientation,
                ),
                "joint_margin_profile": margin_profile,
                "checkpoint_diagnostics": checkpoint_diagnostics,
                "first_joint_limit_failure": first_limit_failure,
                "minimum_joint_limit_margin_rad": (
                    min(all_profile_margins) if all_profile_margins else None
                ),
                "path_limiting_sample": limiting_profile_sample,
                "joint_space_path_length_rad": joint_space_path_length,
                "maximum_waypoint_dq_rad": maximum_waypoint_dq,
                "drawer_joint_before": drawer_before,
                "drawer_joint_after": drawer_after,
                "drawer_joint_delta": drawer_after - drawer_before,
                "gripper": dict(runtime.read_gripper()),
                "contacts": contacts,
                "execution_joint_write_count": runtime.write_counters()[
                    "execution_joint_write_count"
                ],
            }
        )
        report["checks"] = {
            "same_git_head": expected_head is None or report["git_head"] == expected_head,
            "route_reached_grasp_endpoint": all(
                segment["position_error_m"] <= 0.010
                and segment["orientation_error_rad"] <= 0.100
                for segment in report["segments"]
            ),
            "short_range_ik_5_of_5_passed": report["short_range_ik_5_of_5_pass"],
            "short_range_geometry_and_ik_passed": report["short_range_pass"],
            "continuation_precompute_passed": path.get("success") is True,
            "branch_continuity_passed": branch_free,
            "joint_limits_passed": path.get("success") is True
            and bool(all_profile_margins)
            and min(all_profile_margins) > 0.0,
            "explicit_sequential_warm_starts": all(
                attempt.get("direct_solve", {}).get("continuation_seed_source")
                == "previous_accepted_solution"
                and (
                    attempt.get("predictor_fallback", {}).get("attempted") is not True
                    or attempt.get("predictor_fallback", {})
                    .get("retry_solve", {})
                    .get("continuation_seed_source")
                    == "jacobian_predicted_local_seed"
                )
                for attempt in path.get("attempts", [])
            ),
            "gripper_remained_open": report["gripper"].get("open") is True,
            "no_finger_handle_contact": contacts.get("left_contact") is not True
            and contacts.get("right_contact") is not True,
            "drawer_unchanged": abs(report["drawer_joint_delta"]) <= 1.0e-6,
            "execution_drawer_writes_zero": report["execution_joint_write_count"] == 0,
        }
        report["result"] = "passed" if all(report["checks"].values()) else "failed"
        if distance_mm >= 50 and first_limit_failure is not None:
            report["failure_classification"] = "LONG_HORIZON_WORKSPACE_BLOCK"
    except Exception as exc:
        report["errors"].append(
            {"type": type(exc).__name__, "message": " ".join(str(exc).split())[:1000]}
        )
        report["traceback"] = traceback.format_exc()
    finally:
        _write(report_path, report)
        if executor is not None:
            try:
                executor.close()
            except Exception as exc:
                report["errors"].append(
                    {"type": type(exc).__name__, "message": f"close: {exc}"}
                )
                report["result"] = "failed"
        _write(report_path, report)
        print(
            "SCENE_FACTORY_P1_4B_KINEMATIC_CONTINUATION_REPORT="
            + json.dumps(report, ensure_ascii=False, allow_nan=False),
            flush=True,
        )
    return 0 if report["result"] == "passed" else 2


def _run_fixture_kinematics_child(
    report_path: Path,
    *,
    expected_head: str | None,
    fixture_base_position_m: tuple[float, float, float],
    fixture_base_yaw_deg: float,
    orientation_angle_deg: int = 15,
) -> int:
    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "result": "failed",
        "mode": "fixture_kinematics_50mm",
        "git_head": _git_head(),
        "expected_git_head": expected_head,
        "fixture_robot_base_position_m": list(fixture_base_position_m),
        "fixture_robot_base_yaw_deg": fixture_base_yaw_deg,
        "orientation_family_angle_deg": orientation_angle_deg,
        "errors": [],
    }
    executor: IsaacInteractionExecutor | None = None
    try:
        fixture = _fixture_override(
            fixture_base_position_m,
            fixture_base_yaw_deg,
            orientation_angle_deg,
        )
        executor = IsaacInteractionExecutor(headless=True, fixture=fixture)
        executor.reset({"scene_id": "p1_4b_fixture_kinematics"}, _initial_state())
        runtime = executor._runtime
        if runtime is None:
            raise RuntimeError("fixture-kinematics executor has no runtime")

        initial_events = _install_global_contact_audit(runtime)
        for _ in range(5):
            executor._step()
        initial_robot_cabinet_events = _physical_contact_events(initial_events)
        fixture_validity = _fixture_candidate_validity(
            fixture_base_position_m,
            fixture_base_yaw_deg,
            initial_robot_cabinet_contact_count=len(initial_robot_cabinet_events),
        )
        drawer_before = runtime.read_joint()
        report.update(
            {
                "fixture_validity": fixture_validity,
                "physically_valid": fixture_validity["valid"],
                "initial_robot_cabinet_contact_events": initial_robot_cabinet_events,
                "drawer_joint_before": drawer_before,
                "drawer_joint_after": drawer_before,
                "drawer_joint_delta": 0.0,
                "execution_joint_write_count": runtime.write_counters()[
                    "execution_joint_write_count"
                ],
                "contact_attempted": False,
            }
        )
        plan = executor._grasp_plan()
        orientation = tuple(plan["orientation_wxyz"])
        route_targets = _route_targets(plan)
        joint_names = runtime.arm_joint_names()
        previous_q = [float(value) for value in runtime.read_robot_joint_positions()]
        joint_path = [list(previous_q)]
        route_results: list[dict[str, Any]] = []
        pregrasp_reached = False
        route_success = True
        for label, target in route_targets:
            solve = executor._solve_ik(
                target,
                orientation,
                position_tolerance_m=0.01,
            )
            item: dict[str, Any] = {
                "label": label,
                "target_position": list(target),
                "ik_success": solve.get("ik_success") is True,
                "solve": solve,
            }
            if solve.get("ik_success") is not True:
                route_success = False
                route_results.append(item)
                break
            candidate_q = [float(value) for value in solve["joint_positions"]]
            item["continuity"] = _branch_continuity_diagnostic(
                candidate_q,
                previous_q,
                previous_q,
                joint_names=joint_names,
                cartesian_target_delta_m=(
                    0.0
                    if len(route_results) == 0
                    else math.dist(
                        route_results[-1]["target_position"],
                        item["target_position"],
                    )
                ),
            )
            route_results.append(item)
            previous_q = candidate_q
            joint_path.append(list(candidate_q))
            if label == "pregrasp":
                pregrasp_reached = True

        pull_path: dict[str, Any] | None = None
        if route_success:
            eef_start = dict(runtime.compute_arm_fk(previous_q))
            axis = tuple(float(value) for value in plan["opening_axis_world"])
            axis_norm = math.sqrt(sum(value * value for value in axis))
            axis = tuple(value / axis_norm for value in axis)
            pull_path = executor._precompute_pull_ik_path(
                tuple(float(value) for value in eef_start["position"]),
                axis,
                0.05,
                orientation,
                initial_joint_positions=previous_q,
            )
            joint_path.extend(
                list(waypoint["joint_positions"])
                for waypoint in pull_path.get("accepted_waypoints", [])
            )

        margin_samples: list[dict[str, Any]] = []
        for item in route_results:
            solve = item["solve"]
            if solve.get("minimum_joint_limit_margin_rad") is not None:
                margin_samples.append(
                    {
                        "phase": "approach",
                        "label": item["label"],
                        "minimum_joint_limit_margin_rad": solve[
                            "minimum_joint_limit_margin_rad"
                        ],
                        "limiting_joint": solve.get("limiting_joint"),
                    }
                )
        if pull_path is not None:
            initial_diagnostic = pull_path.get("initial_joint_limit_diagnostic", {})
            if initial_diagnostic.get("minimum_joint_limit_margin_rad") is not None:
                margin_samples.append(
                    {
                        "phase": "pull",
                        "cartesian_progress_m": 0.0,
                        "minimum_joint_limit_margin_rad": initial_diagnostic[
                            "minimum_joint_limit_margin_rad"
                        ],
                        "limiting_joint": initial_diagnostic.get("limiting_joint"),
                    }
                )
            margin_samples.extend(
                {
                    "phase": "pull",
                    "cartesian_progress_m": waypoint["cartesian_progress_m"],
                    "minimum_joint_limit_margin_rad": waypoint[
                        "minimum_joint_limit_margin_rad"
                    ],
                    "limiting_joint": waypoint.get("limiting_joint"),
                }
                for waypoint in pull_path.get("accepted_waypoints", [])
            )
        limiting_sample = (
            min(
                margin_samples,
                key=lambda item: float(item["minimum_joint_limit_margin_rad"]),
            )
            if margin_samples
            else None
        )
        checkpoints = []
        for checkpoint_mm in (0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50):
            target = checkpoint_mm / 1000.0
            if checkpoint_mm == 0 and pull_path is not None:
                diagnostic = pull_path.get("initial_joint_limit_diagnostic", {})
                match = {
                    "minimum_joint_limit_margin_rad": diagnostic.get(
                        "minimum_joint_limit_margin_rad"
                    ),
                    "limiting_joint": diagnostic.get("limiting_joint"),
                    "joint_limit_diagnostic": diagnostic,
                }
            else:
                match = next(
                    (
                        waypoint
                        for waypoint in (pull_path or {}).get("accepted_waypoints", [])
                        if abs(float(waypoint["cartesian_progress_m"]) - target) <= 1.0e-9
                    ),
                    None,
                )
            checkpoints.append(
                {
                    "checkpoint_mm": checkpoint_mm,
                    "reachable": match is not None,
                    "minimum_joint_limit_margin_rad": (
                        None
                        if match is None
                        else match.get("minimum_joint_limit_margin_rad")
                    ),
                    "limiting_joint": (
                        None if match is None else match.get("limiting_joint")
                    ),
                    "panda_joint4_margin_rad": (
                        None
                        if match is None
                        else next(
                            (
                                joint["distance_to_nearest_limit_rad"]
                                for joint in match.get("joint_limit_diagnostic", {}).get(
                                    "joints", []
                                )
                                if joint.get("joint_name") == "panda_joint4"
                            ),
                            None,
                        )
                    ),
                }
            )

        joint_space_path_length = sum(
            math.dist(previous, current)
            for previous, current in zip(joint_path, joint_path[1:])
        )
        max_waypoint_dq = max(
            (
                max(abs(current - previous) for previous, current in zip(q0, q1))
                for q0, q1 in zip(joint_path, joint_path[1:])
            ),
            default=0.0,
        )
        drawer_after = runtime.read_joint()
        reach_50mm = pull_path is not None and pull_path.get("success") is True
        fixture_deviation = math.dist(
            fixture_base_position_m,
            _IsaacInteractionConfig.fixture_robot_base_position_m,
        ) + abs(math.radians(fixture_base_yaw_deg)) * 0.1
        report.update(
            {
                "fixture_validity": fixture_validity,
                "physically_valid": fixture_validity["valid"],
                "initial_robot_cabinet_contact_events": initial_robot_cabinet_events,
                "selected_grasp": plan,
                "approach_ik_viable": pregrasp_reached,
                "grasp_ik_viable": route_success,
                "route_ik": route_results,
                "pull_ik": pull_path,
                "reach_50mm": reach_50mm,
                "checkpoint_diagnostics": checkpoints,
                "margin_samples": margin_samples,
                "minimum_path_margin_rad": (
                    None
                    if limiting_sample is None
                    else limiting_sample["minimum_joint_limit_margin_rad"]
                ),
                "limiting_sample": limiting_sample,
                "joint_space_path_length_rad": joint_space_path_length,
                "maximum_waypoint_dq_rad": max_waypoint_dq,
                "fixture_deviation_norm": fixture_deviation,
                "drawer_joint_before": drawer_before,
                "drawer_joint_after": drawer_after,
                "drawer_joint_delta": drawer_after - drawer_before,
                "execution_joint_write_count": runtime.write_counters()[
                    "execution_joint_write_count"
                ],
                "contact_attempted": False,
            }
        )
        report["checks"] = {
            "same_git_head": expected_head is None or report["git_head"] == expected_head,
            "fixture_physically_valid": fixture_validity["valid"],
            "approach_ik_viable": pregrasp_reached,
            "grasp_ik_viable": route_success,
            "continuous_50mm_reachable": reach_50mm,
            "positive_path_margin": report["minimum_path_margin_rad"] is not None
            and report["minimum_path_margin_rad"] > 0.0,
            "drawer_unchanged": abs(report["drawer_joint_delta"]) <= 1.0e-6,
            "execution_drawer_writes_zero": report["execution_joint_write_count"] == 0,
            "no_contact_attempted": report["contact_attempted"] is False,
        }
        report["result"] = "passed" if all(report["checks"].values()) else "failed"
    except Exception as exc:
        report["errors"].append(
            {"type": type(exc).__name__, "message": " ".join(str(exc).split())[:1000]}
        )
        report["traceback"] = traceback.format_exc()
    finally:
        _write(report_path, report)
        if executor is not None:
            try:
                executor.close()
            except Exception as exc:
                report["errors"].append(
                    {"type": type(exc).__name__, "message": f"close: {exc}"}
                )
                report["result"] = "failed"
        _write(report_path, report)
        print(
            "SCENE_FACTORY_P1_4B_FIXTURE_KINEMATICS_REPORT="
            + json.dumps(report, ensure_ascii=False, allow_nan=False),
            flush=True,
        )
    return 0 if report["result"] == "passed" else 2


def _install_global_contact_audit(runtime: _IsaacInteractionRuntime) -> list[dict[str, Any]]:
    from omni.physics.core import ContactEventType
    from pxr import PhysicsSchemaTools, PhysxSchema, UsdPhysics

    events: list[dict[str, Any]] = []
    stage = runtime._stage
    if stage is None or runtime._contact_interface is None:
        raise RuntimeError("contact audit requires an initialized Isaac stage")
    for prim in stage.Traverse():
        path = str(prim.GetPath())
        if (
            (path.startswith("/World/Franka/") or path.startswith("/World/Cabinet/"))
            and prim.HasAPI(UsdPhysics.RigidBodyAPI)
        ):
            PhysxSchema.PhysxContactReportAPI.Apply(prim).CreateThresholdAttr().Set(0.0)

    old_subscription = runtime._contact_subscription
    if old_subscription is not None:
        if hasattr(old_subscription, "unsubscribe"):
            old_subscription.unsubscribe()
        else:
            runtime._contact_interface.unsubscribe_physics_contact_report_events(old_subscription)

    def on_contact_event(headers: Any, contact_data: Any, _friction_anchors: Any) -> None:
        for header in headers:
            if header.type not in (
                ContactEventType.CONTACT_FOUND,
                ContactEventType.CONTACT_PERSIST,
                ContactEventType.CONTACT_LOST,
            ):
                continue
            collider0 = str(PhysicsSchemaTools.intToSdfPath(header.collider0))
            collider1 = str(PhysicsSchemaTools.intToSdfPath(header.collider1))
            if not (
                (collider0.startswith("/World/Franka") and collider1.startswith("/World/Cabinet"))
                or (collider1.startswith("/World/Franka") and collider0.startswith("/World/Cabinet"))
            ):
                continue
            offset = int(getattr(header, "contact_data_offset", 0))
            count = int(getattr(header, "num_contact_data", 0))
            details = []
            for item in list(contact_data)[offset : offset + count]:
                impulse = [float(value) for value in item.impulse]
                details.append(
                    {
                        "position": [float(value) for value in item.position],
                        "normal": [float(value) for value in item.normal],
                        "impulse": impulse,
                        "force_n": sum(value * value for value in impulse) ** 0.5 / runtime.config.physics_dt,
                        "separation_m": float(item.separation),
                    }
                )
            events.append(
                {
                    "physics_step": runtime._physics_steps,
                    "collider0": collider0,
                    "collider1": collider1,
                    "type": int(header.type),
                    "event": {
                        int(ContactEventType.CONTACT_FOUND): "found",
                        int(ContactEventType.CONTACT_PERSIST): "persist",
                        int(ContactEventType.CONTACT_LOST): "lost",
                    }[int(header.type)],
                    "details": details,
                }
            )

    runtime._contact_subscription = runtime._contact_interface.subscribe_physics_contact_report_events(
        on_contact_event
    )
    return events


def _run_motion_only_child(
    report_path: Path,
    *,
    expected_head: str | None,
    distance_mm: int | None = None,
    fixture_base_position_m: tuple[float, float, float] | None = None,
    fixture_base_yaw_deg: float | None = None,
    orientation_angle_deg: int | None = None,
) -> int:
    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "result": "failed",
        "mode": "motion_only" if distance_mm is None else "pull_motion_only",
        "traction_distance_mm": distance_mm,
        "git_head": _git_head(),
        "expected_git_head": expected_head,
        "segments": [],
        "contact_events": [],
        "gripper_closed": False,
        "contact_acceptance_run": False,
        "drawer_pull_run": False,
        "errors": [],
    }
    executor: IsaacInteractionExecutor | None = None
    try:
        executor = IsaacInteractionExecutor(
            headless=True,
            fixture=_fixture_override(
                fixture_base_position_m,
                fixture_base_yaw_deg,
                orientation_angle_deg,
            ),
        )
        executor.reset({"scene_id": "p1_4b_motion_only"}, _initial_state())
        runtime = executor._runtime
        if runtime is None:
            raise RuntimeError("motion-only executor has no runtime")
        contacts = _install_global_contact_audit(runtime)
        plan = executor._grasp_plan()
        orientation = tuple(plan["orientation_wxyz"])
        drawer_before = runtime.read_joint()
        gripper_before = dict(runtime.read_gripper())
        report["segments"] = _move_open_route(executor, plan)
        continuation = None
        continuation_samples: list[dict[str, Any]] = []
        continuation_converged = True
        terminal_alignment: dict[str, Any] | None = None
        if distance_mm is not None:
            distance_m = distance_mm / 1000.0
            alignment_start_q = [
                float(value) for value in runtime.read_robot_joint_positions()
            ]
            configured_base = tuple(executor._config.fixture_robot_base_position_m)
            if configured_base == _BASELINE_PHYSICAL_GRASP_BASE_POSITION_M:
                alignment_target_q = list(_BASELINE_PHYSICAL_GRASP_TERMINAL_Q)
                alignment_source = "measured_physical_grasp_terminal_q"
            else:
                alignment_target_q = list(alignment_start_q)
                alignment_source = "equivalent_open_gripper_grasp_endpoint_q"
            alignment_continuity = _branch_continuity_diagnostic(
                alignment_target_q,
                alignment_start_q,
                alignment_start_q,
                joint_names=runtime.arm_joint_names(),
                cartesian_target_delta_m=math.dist(
                    runtime.read_eef_pose()["position"],
                    runtime.compute_arm_fk(alignment_target_q)["position"],
                ),
            )
            alignment_samples = []
            alignment_converged = not alignment_continuity["branch_jump_detected"]
            if alignment_converged:
                runtime.apply_arm_targets(alignment_target_q)
                alignment_converged = False
                for _ in range(executor._config.pull_steps_per_waypoint):
                    runtime.apply_arm_targets(alignment_target_q)
                    executor._step()
                    observed_q = [
                        float(value) for value in runtime.read_robot_joint_positions()
                    ]
                    error = _joint_delta_metrics(
                        alignment_target_q,
                        observed_q,
                        runtime.arm_joint_names(),
                    )
                    alignment_samples.append(
                        {
                            "physics_step": runtime._physics_steps,
                            "observed_joint_positions": observed_q,
                            "target_error": error,
                            "observed_eef_pose": dict(runtime.read_eef_pose()),
                        }
                    )
                    if error["max_abs_delta_rad"] <= _PULL_JOINT_CONVERGENCE_RAD:
                        alignment_converged = True
                        break
            terminal_alignment = {
                "target_joint_positions": alignment_target_q,
                "target_source": alignment_source,
                "continuity": alignment_continuity,
                "samples": alignment_samples,
                "converged": alignment_converged,
            }
            continuation_converged = alignment_converged
            eef_start = dict(runtime.read_eef_pose())
            axis = tuple(float(value) for value in plan["opening_axis_world"])
            axis_norm = math.sqrt(sum(value * value for value in axis))
            axis = tuple(value / axis_norm for value in axis)
            if continuation_converged:
                continuation = executor._precompute_pull_ik_path(
                    tuple(float(value) for value in eef_start["position"]),
                    axis,
                    distance_m,
                    orientation,
                )
                continuation_converged = continuation.get("success") is True
            for waypoint in continuation.get("accepted_waypoints", []) if continuation_converged else []:
                target_position = tuple(
                    float(value) for value in waypoint["target_position"]
                )
                joints = [float(value) for value in waypoint["joint_positions"]]
                runtime.apply_arm_targets(joints)
                waypoint_converged = False
                for _ in range(executor._config.pull_steps_per_waypoint):
                    runtime.apply_arm_targets(joints)
                    executor._step()
                    observed_pose = dict(runtime.read_eef_pose())
                    observed_joints = [
                        float(value) for value in runtime.read_robot_joint_positions()
                    ]
                    joint_error = _joint_delta_metrics(
                        joints,
                        observed_joints,
                        continuation["joint_names"],
                    )
                    sample = {
                        "physics_step": runtime._physics_steps,
                        "waypoint_id": waypoint["waypoint_id"],
                        "target_position": list(target_position),
                        "observed_pose": observed_pose,
                        "eef_position_error_m": math.dist(
                            observed_pose["position"],
                            target_position,
                        ),
                        "eef_orientation_error_rad": quaternion_angular_distance(
                            observed_pose["orientation_wxyz"],
                            orientation,
                        ),
                        "target_joint_positions": joints,
                        "observed_joint_positions": observed_joints,
                        "joint_target_error": joint_error,
                    }
                    continuation_samples.append(sample)
                    if joint_error["max_abs_delta_rad"] <= _PULL_JOINT_CONVERGENCE_RAD:
                        waypoint_converged = True
                        break
                if not waypoint_converged:
                    continuation_converged = False
                    break
        gripper_after = dict(runtime.read_gripper())
        drawer_after = runtime.read_joint()
        physical_contacts = [
            event
            for event in contacts
            if any(
                detail["force_n"] > 1.0e-6 or detail["separation_m"] < 0.0
                for detail in event["details"]
            )
        ]
        report.update(
            {
                "runtime": {
                    "isaac_version": runtime.isaac_sim_version(),
                    "binding_id": SEKTION_TOP_DRAWER_BINDING.binding_id,
                    "runtime_root": SEKTION_TOP_DRAWER_RUNTIME_ROOT,
                    "controller_frame": "right_gripper",
                    "observation_frame": runtime._observation_frame,
                    "ik_api": runtime.ik_api_diagnostics(),
                    "fixture_robot_base_position_m": list(
                        executor._config.fixture_robot_base_position_m
                    ),
                    "fixture_robot_base_orientation_wxyz": list(
                        executor._config.fixture_robot_base_orientation_wxyz
                    ),
                    "fixture_robot_base_yaw_deg": fixture_base_yaw_deg or 0.0,
                },
                "selected_grasp": plan,
                "target_grasp_position": plan["grasp_position_world"],
                "target_pregrasp_position": plan["pregrasp_position_world"],
                "orientation_wxyz": plan["orientation_wxyz"],
                "continuity_policy": _continuity_policy(),
                "physical_grasp_terminal_alignment": terminal_alignment,
                "ik_continuation": continuation,
                "continuation_samples": continuation_samples,
                "continuation_motion_converged": continuation_converged,
                "max_continuation_eef_deviation_m": max(
                    (
                        float(sample["eef_position_error_m"])
                        for sample in continuation_samples
                    ),
                    default=None,
                ),
                "contact_events": contacts,
                "physical_contact_events": physical_contacts,
                "gripper_before": gripper_before,
                "gripper_after": gripper_after,
                "drawer_joint_before": drawer_before,
                "drawer_joint_after": drawer_after,
                "drawer_joint_delta": drawer_after - drawer_before,
                "execution_joint_write_count": runtime.write_counters()["execution_joint_write_count"],
            }
        )
        report["checks"] = {
            "same_git_head": expected_head is None or report["git_head"] == expected_head,
            "all_segments_within_tolerance": all(
                segment["position_error_m"] <= 0.010
                and segment["orientation_error_rad"] <= 0.100
                for segment in report["segments"]
            ),
            "final_endpoint_reached": report["segments"][-1]["target_pose"]["position"]
            == list(plan["grasp_position_world"]),
            "no_physical_robot_environment_contact": not physical_contacts,
            "gripper_remained_open": gripper_before.get("open") is True
            and gripper_after.get("open") is True,
            "drawer_unchanged": abs(report["drawer_joint_delta"]) <= 1.0e-6,
            "execution_drawer_writes_zero": report["execution_joint_write_count"] == 0,
            "contact_acceptance_not_run": report["contact_acceptance_run"] is False,
            "drawer_pull_not_run": report["drawer_pull_run"] is False,
            "continuation_precompute_passed": continuation is None
            if distance_mm is None
            else continuation is not None and continuation.get("success") is True,
            "physical_grasp_terminal_alignment_passed": terminal_alignment is None
            or terminal_alignment["converged"] is True,
            "continuation_motion_converged": continuation is None
            or continuation_converged,
            "continuation_no_branch_jump": continuation is None
            or all(
                attempt.get("continuity", {}).get("branch_jump_detected") is False
                for attempt in continuation.get("attempts", [])
                if attempt.get("ik_success") is True
            ),
            "continuation_no_large_eef_deviation": continuation is None
            or report["max_continuation_eef_deviation_m"] <= 0.005,
        }
        report["result"] = "passed" if all(report["checks"].values()) else "failed"
    except Exception as exc:
        report["errors"].append(
            {"type": type(exc).__name__, "message": " ".join(str(exc).split())[:1000]}
        )
        report["traceback"] = traceback.format_exc()
    finally:
        _write(report_path, report)
        if executor is not None:
            try:
                executor.close()
            except Exception as exc:
                report["errors"].append(
                    {"type": type(exc).__name__, "message": f"close: {exc}"}
                )
                report["result"] = "failed"
        _write(report_path, report)
        print(
            "SCENE_FACTORY_P1_4B_MOTION_ONLY_REPORT="
            + json.dumps(report, ensure_ascii=False, allow_nan=False),
            flush=True,
        )
    return 0 if report["result"] == "passed" else 2


def _physical_contact_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        event
        for event in events
        if any(
            detail["force_n"] > 1.0e-6 or detail["separation_m"] < 0.0
            for detail in event["details"]
        )
    ]


def _is_finger_handle_event(
    runtime: _IsaacInteractionRuntime,
    event: dict[str, Any],
) -> bool:
    handle_path = SEKTION_TOP_DRAWER_RUNTIME_ROOT + "/drawer_handle_top"
    pair = (event["collider0"], event["collider1"])
    finger0 = any(
        pair[0] == path or pair[0].startswith(path.rstrip("/") + "/")
        for path in runtime._finger_root_paths
    )
    finger1 = any(
        pair[1] == path or pair[1].startswith(path.rstrip("/") + "/")
        for path in runtime._finger_root_paths
    )
    return (
        finger0 and pair[1].startswith(handle_path)
    ) or (
        finger1 and pair[0].startswith(handle_path)
    )


def _static_runtime_state(runtime: _IsaacInteractionRuntime) -> dict[str, Any]:
    return {
        "physics_step": runtime._physics_steps,
        "right_gripper": dict(runtime.read_eef_pose()),
        "panda_hand": runtime._read_prim_frame(runtime._hand_root_path),
        "left_finger": runtime._read_prim_frame(runtime._finger_root_paths[0]),
        "right_finger": runtime._read_prim_frame(runtime._finger_root_paths[1]),
        "gripper": dict(runtime.read_gripper()),
        "handle": dict(runtime.read_handle_frame()),
        "drawer_joint": runtime.read_joint(),
        "contacts": dict(runtime.read_contacts()),
    }


def _vector_subtract(first: tuple[float, ...], second: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(a - b for a, b in zip(first, second, strict=True))


def _vector_dot(first: tuple[float, ...], second: tuple[float, ...]) -> float:
    return sum(a * b for a, b in zip(first, second, strict=True))


def _vector_norm(value: tuple[float, ...]) -> float:
    return math.sqrt(sum(item * item for item in value))


def _finger_midpoint(state: dict[str, Any]) -> tuple[float, float, float]:
    left = tuple(float(value) for value in state["left_finger"]["position"])
    right = tuple(float(value) for value in state["right_finger"]["position"])
    return tuple((a + b) * 0.5 for a, b in zip(left, right, strict=True))


def _relative_motion_diagnostic(
    initial: dict[str, Any],
    current: dict[str, Any],
    opening_axis: tuple[float, float, float],
) -> dict[str, Any]:
    initial_gripper = _pose_transform(
        tuple(float(value) for value in initial["right_gripper"]["position"]),
        tuple(float(value) for value in initial["right_gripper"]["orientation_wxyz"]),
    )
    current_gripper = _pose_transform(
        tuple(float(value) for value in current["right_gripper"]["position"]),
        tuple(float(value) for value in current["right_gripper"]["orientation_wxyz"]),
    )
    initial_handle = tuple(float(value) for value in initial["handle"]["transform"])
    current_handle = tuple(float(value) for value in current["handle"]["transform"])
    initial_relative = _compose_transform(
        _inverse_transform(initial_gripper, "initial right_gripper"),
        initial_handle,
    )
    current_relative = _compose_transform(
        _inverse_transform(current_gripper, "current right_gripper"),
        current_handle,
    )
    initial_world_offset = _vector_subtract(
        tuple(float(value) for value in initial["handle"]["position"]),
        tuple(float(value) for value in initial["right_gripper"]["position"]),
    )
    current_world_offset = _vector_subtract(
        tuple(float(value) for value in current["handle"]["position"]),
        tuple(float(value) for value in current["right_gripper"]["position"]),
    )
    world_change = _vector_subtract(current_world_offset, initial_world_offset)
    vertical_reference = (0.0, 0.0, 1.0)
    vertical_residual = _vector_subtract(
        vertical_reference,
        tuple(opening_axis[index] * _vector_dot(vertical_reference, opening_axis) for index in range(3)),
    )
    if _vector_norm(vertical_residual) <= 1.0e-9:
        vertical_residual = (0.0, 1.0, 0.0)
    vertical_norm = _vector_norm(vertical_residual)
    vertical = tuple(value / vertical_norm for value in vertical_residual)
    lateral = (
        vertical[1] * opening_axis[2] - vertical[2] * opening_axis[1],
        vertical[2] * opening_axis[0] - vertical[0] * opening_axis[2],
        vertical[0] * opening_axis[1] - vertical[1] * opening_axis[0],
    )
    return {
        "handle_minus_right_gripper_world_change_m": world_change,
        "relative_axial_displacement_m": _vector_dot(world_change, opening_axis),
        "relative_lateral_displacement_m": _vector_dot(world_change, lateral),
        "relative_vertical_displacement_m": _vector_dot(world_change, vertical),
        "relative_translation_norm_m": _vector_norm(world_change),
        "relative_rotation_rad": quaternion_angular_distance(
            _matrix_to_quaternion(initial_relative),
            _matrix_to_quaternion(current_relative),
        ),
    }


def _contact_point_migration(
    initial_contacts: dict[str, Any],
    current_contacts: dict[str, Any],
) -> dict[str, Any]:
    initial_by_finger = {
        str(sample.get("finger")): sample
        for sample in initial_contacts.get("force_samples", [])
        if sample.get("contact_point_handle_local") is not None
    }
    migrations = []
    for sample in current_contacts.get("force_samples", []):
        finger = str(sample.get("finger"))
        point = sample.get("contact_point_handle_local")
        initial_sample = initial_by_finger.get(finger)
        if point is None or initial_sample is None:
            continue
        origin = initial_sample.get("contact_point_handle_local")
        if origin is None:
            continue
        delta = _vector_subtract(
            tuple(float(value) for value in point),
            tuple(float(value) for value in origin),
        )
        migrations.append(
            {
                "finger": finger,
                "delta_handle_local_m": delta,
                "migration_norm_m": _vector_norm(delta),
            }
        )
    return {
        "per_finger": migrations,
        "maximum_migration_norm_m": max(
            (item["migration_norm_m"] for item in migrations),
            default=None,
        ),
    }


def _contact_force_components(contacts: dict[str, Any]) -> dict[str, Any]:
    normal = []
    friction = []
    for sample in contacts.get("force_samples", []):
        normal_force = sample.get("normal_force_on_finger_world")
        friction_force = sample.get("friction_force_on_finger_world")
        if normal_force is not None:
            normal.append(_vector_norm(tuple(float(value) for value in normal_force)))
        if friction_force is not None:
            friction.append(_vector_norm(tuple(float(value) for value in friction_force)))
    return {
        "normal_force_magnitudes_n": normal,
        "total_normal_force_magnitude_n": sum(normal) if normal else None,
        "friction_force_magnitudes_n": friction,
        "total_friction_force_magnitude_n": sum(friction) if friction else None,
    }


def _first_joint_limit_failure(
    path: dict[str, Any],
    *,
    joint_limits: list[tuple[float, float]] | None = None,
    joint_names: list[str] | None = None,
) -> dict[str, Any] | None:
    for attempt in path.get("attempts", []):
        candidates = [attempt.get("direct_solve", {})]
        retry = attempt.get("predictor_fallback", {}).get("retry_solve")
        if isinstance(retry, dict):
            candidates.append(retry)
        for solve in candidates:
            if solve.get("reason") == "ik_solution_outside_limits":
                return {
                    "failure_mode": "ik_solution_outside_limits",
                    "cartesian_progress_m": attempt.get("cartesian_progress_m"),
                    "target_position": attempt.get("target_position"),
                    "joint_positions": solve.get("joint_positions"),
                    "joint_limit_diagnostic": solve.get("joint_limit_diagnostic"),
                    "limiting_joint": solve.get("limiting_joint"),
                }
        fallback = attempt.get("predictor_fallback", {})
        prediction = fallback.get("prediction")
        retry = fallback.get("retry_solve", {})
        if (
            joint_limits is not None
            and joint_names is not None
            and isinstance(prediction, dict)
            and retry.get("ik_success") is not True
        ):
            predicted_positions = prediction.get("joint_positions", [])
            if len(predicted_positions) == len(joint_limits):
                diagnostic = _joint_limit_diagnostic(
                    [float(value) for value in predicted_positions],
                    joint_limits,
                    joint_names,
                )
                if diagnostic["minimum_joint_limit_margin_rad"] <= 1.0e-9:
                    return {
                        "failure_mode": "ik_failed_with_predictor_at_joint_limit",
                        "cartesian_progress_m": attempt.get("cartesian_progress_m"),
                        "target_position": attempt.get("target_position"),
                        "joint_positions": predicted_positions,
                        "joint_limit_diagnostic": diagnostic,
                        "limiting_joint": diagnostic["limiting_joint"],
                        "retry_reason": retry.get("reason"),
                    }
    return None


def _static_sample_passes(sample: dict[str, Any]) -> bool:
    contacts = sample["contacts"]
    convention = contacts.get("force_convention", {})
    return (
        contacts.get("left_contact") is True
        and contacts.get("right_contact") is True
        and contacts.get("force_valid") is True
        and contacts.get("nonzero_force") is True
        and contacts.get("contact_detail_available") is True
        and contacts.get("surface_pair_valid") is True
        and contacts.get("opposed_contact") is True
        and contacts.get("normal_dot_left_right") is not None
        and contacts["normal_dot_left_right"] < 0.0
        and convention.get("newton_pair_observed") is True
    )


def _run_static_grasp_child(
    report_path: Path,
    *,
    expected_head: str | None,
    fixture_base_position_m: tuple[float, float, float] | None = None,
    fixture_base_yaw_deg: float | None = None,
    orientation_angle_deg: int | None = None,
) -> int:
    hold_steps = 60
    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "result": "failed",
        "mode": "static_grasp",
        "git_head": _git_head(),
        "expected_git_head": expected_head,
        "segments": [],
        "gripper_close_command_count": 0,
        "hold_steps": hold_steps,
        "traction_attempted": False,
        "drawer_pull_run": False,
        "errors": [],
    }
    executor: IsaacInteractionExecutor | None = None
    try:
        fixture = _fixture_override(
            fixture_base_position_m,
            fixture_base_yaw_deg,
            orientation_angle_deg,
        )
        executor = IsaacInteractionExecutor(headless=True, fixture=fixture)
        executor.reset({"scene_id": "p1_4b_static_grasp"}, _initial_state())
        runtime = executor._runtime
        if runtime is None:
            raise RuntimeError("static-grasp executor has no runtime")
        events = _install_global_contact_audit(runtime)
        plan = executor._grasp_plan()
        orientation = tuple(plan["orientation_wxyz"])
        entry_waypoints = [tuple(item) for item in plan["pregrasp_entry_waypoints_world"]]
        targets = [
            (
                "pregrasp" if index == len(entry_waypoints) else f"clearance_entry_{index}",
                waypoint,
            )
            for index, waypoint in enumerate(entry_waypoints, start=1)
        ]
        approach_waypoints = [tuple(item) for item in plan["approach_waypoints_world"]]
        if approach_waypoints and approach_waypoints[0] == tuple(plan["pregrasp_position_world"]):
            approach_waypoints = approach_waypoints[1:]
        targets.extend(
            (f"approach_waypoint_{index}", waypoint)
            for index, waypoint in enumerate(approach_waypoints, start=2)
        )
        for label, target in targets:
            motion = executor._move_to_pose(
                target,
                executor._config.approach_steps
                if label == "pregrasp"
                else executor._config.grasp_motion_steps,
                orientation,
            )
            report["segments"].append({"label": label, **motion})

        executor._step()
        pre_close = _static_runtime_state(runtime)
        pre_close_event_count = len(events)
        pre_close_physical = _physical_contact_events(events)
        drawer_before_close = pre_close["drawer_joint"]
        report["pre_close_state"] = pre_close
        report["pre_close_contact_events"] = pre_close_physical

        runtime.close_gripper()
        report["gripper_close_command_count"] += 1
        samples = []
        max_consecutive = 0
        consecutive = 0
        for _ in range(hold_steps):
            executor._step()
            state = _static_runtime_state(runtime)
            samples.append(state)
            if _static_sample_passes(state):
                consecutive += 1
                max_consecutive = max(max_consecutive, consecutive)
            else:
                consecutive = 0

        physical_events = _physical_contact_events(events)
        close_events = _physical_contact_events(events[pre_close_event_count:])
        invalid_close_events = [
            event for event in close_events if not _is_finger_handle_event(runtime, event)
        ]
        stable_samples = [sample for sample in samples if _static_sample_passes(sample)]
        qualification_state = stable_samples[-1] if stable_samples else samples[-1]
        qualification_contacts = qualification_state["contacts"]
        force_samples = qualification_contacts.get("force_samples", [])
        surfaces = [
            sample.get("surface")
            for sample in force_samples
            if sample.get("force_magnitude", 0.0) > 1.0e-6
        ]
        final_state = samples[-1]
        final_contacts = final_state["contacts"]
        lost_events = [
            event
            for event in events[pre_close_event_count:]
            if event["event"] == "lost" and _is_finger_handle_event(runtime, event)
        ]
        drawer_after_hold = final_state["drawer_joint"]
        drawer_delta = drawer_after_hold - drawer_before_close
        report.update(
            {
                "runtime": {
                    "isaac_version": runtime.isaac_sim_version(),
                    "binding_id": SEKTION_TOP_DRAWER_BINDING.binding_id,
                    "runtime_root": SEKTION_TOP_DRAWER_RUNTIME_ROOT,
                    "controller_frame": "right_gripper",
                    "observation_frame": runtime._observation_frame,
                },
                "selected_grasp": plan,
                "expected_surface_pair": list(plan["handle_surface_pair"]),
                "expected_contact_normals": list(plan["expected_contact_normals"]),
                "static_samples": samples,
                "qualification_state": qualification_state,
                "final_state": final_state,
                "contact_events": events,
                "physical_contact_events": physical_events,
                "close_contact_events": close_events,
                "invalid_close_contact_events": invalid_close_events,
                "contact_lost_events": lost_events,
                "stable_observation_count": len(stable_samples),
                "max_consecutive_stable_observations": max_consecutive,
                "final_consecutive_stable_observations": consecutive,
                "observed_qualification_surfaces": surfaces,
                "final_contact_sensor_active": final_contacts.get("left_contact") is True
                and final_contacts.get("right_contact") is True,
                "drawer_joint_before_close": drawer_before_close,
                "drawer_joint_after_hold": drawer_after_hold,
                "drawer_joint_delta_during_close_hold": drawer_delta,
                "execution_joint_write_count": runtime.write_counters()[
                    "execution_joint_write_count"
                ],
            }
        )
        report["checks"] = {
            "same_git_head": expected_head is None or report["git_head"] == expected_head,
            "all_segments_within_tolerance": all(
                segment["position_error_m"] <= 0.010
                and segment["orientation_error_rad"] <= 0.100
                for segment in report["segments"]
            ),
            "pre_close_left_handle_contact_zero": pre_close["contacts"]["left_contact"]
            is False,
            "pre_close_right_handle_contact_zero": pre_close["contacts"]["right_contact"]
            is False,
            "pre_close_physical_contact_events_zero": not pre_close_physical,
            "physical_close_called_once": report["gripper_close_command_count"] == 1,
            "gripper_close_observed": final_state["gripper"].get("open") is False,
            "left_handle_contact": qualification_contacts.get("left_contact") is True,
            "right_handle_contact": qualification_contacts.get("right_contact") is True,
            "top_bottom_surface_pair": sorted(str(surface) for surface in surfaces)
            == ["bottom", "top"],
            "opposed_contact_normals": qualification_contacts.get("opposed_contact") is True,
            "finite_nonzero_force": qualification_contacts.get("force_valid") is True
            and qualification_contacts.get("nonzero_force") is True,
            "contact_details_available": qualification_contacts.get("contact_detail_available")
            is True,
            "force_convention_verified": qualification_contacts.get("force_convention", {}).get(
                "newton_pair_observed"
            )
            is True,
            "five_consecutive_stable_observations": max_consecutive >= 5,
            "thirty_to_sixty_step_hold_observed": len(samples) >= 30,
            "no_physical_contact_lost_event": not lost_events,
            "no_invalid_cabinet_collision_blocker": not invalid_close_events,
            "drawer_near_reset": abs(drawer_delta) <= 1.0e-4,
            "execution_drawer_writes_zero": report["execution_joint_write_count"] == 0,
            "traction_not_attempted": report["traction_attempted"] is False,
        }
        report["result"] = "passed" if all(report["checks"].values()) else "failed"
        if report["result"] == "failed":
            report["failure_classification"] = (
                "GRASP_CLOSING_COLLISION"
                if invalid_close_events
                else "PHYSICAL_STATIC_GRASP_FAILED"
            )
    except Exception as exc:
        report["errors"].append(
            {"type": type(exc).__name__, "message": " ".join(str(exc).split())[:1000]}
        )
        report["traceback"] = traceback.format_exc()
    finally:
        _write(report_path, report)
        if executor is not None:
            try:
                executor.close()
            except Exception as exc:
                report["errors"].append(
                    {"type": type(exc).__name__, "message": f"close: {exc}"}
                )
                report["result"] = "failed"
        _write(report_path, report)
        print(
            "SCENE_FACTORY_P1_4B_STATIC_GRASP_REPORT="
            + json.dumps(report, ensure_ascii=False, allow_nan=False),
            flush=True,
        )
    return 0 if report["result"] == "passed" else 2


def _run_traction_child(
    report_path: Path,
    *,
    expected_head: str | None,
    distance_mm: int,
    fixture_base_position_m: tuple[float, float, float] | None = None,
    fixture_base_yaw_deg: float | None = None,
    orientation_angle_deg: int | None = None,
) -> int:
    distance_m = distance_mm / 1000.0
    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "result": "failed",
        "mode": "traction_gate",
        "traction_distance_mm": distance_mm,
        "git_head": _git_head(),
        "expected_git_head": expected_head,
        "segments": [],
        "gripper_close_command_count": 0,
        "traction_attempted": False,
        "drawer_pull_run": False,
        "errors": [],
    }
    executor: IsaacInteractionExecutor | None = None
    try:
        fixture = _fixture_override(
            fixture_base_position_m,
            fixture_base_yaw_deg,
            orientation_angle_deg,
        )
        executor = IsaacInteractionExecutor(headless=True, fixture=fixture)
        executor.reset(
            {"scene_id": f"p1_4b_traction_{distance_mm}mm"},
            _initial_state(),
        )
        runtime = executor._runtime
        if runtime is None:
            raise RuntimeError("traction executor has no runtime")
        events = _install_global_contact_audit(runtime)
        plan = executor._grasp_plan()
        orientation = tuple(plan["orientation_wxyz"])
        entry_waypoints = [tuple(item) for item in plan["pregrasp_entry_waypoints_world"]]
        targets = [
            (
                "pregrasp" if index == len(entry_waypoints) else f"clearance_entry_{index}",
                waypoint,
            )
            for index, waypoint in enumerate(entry_waypoints, start=1)
        ]
        approach_waypoints = [tuple(item) for item in plan["approach_waypoints_world"]]
        if approach_waypoints and approach_waypoints[0] == tuple(plan["pregrasp_position_world"]):
            approach_waypoints = approach_waypoints[1:]
        targets.extend(
            (f"approach_waypoint_{index}", waypoint)
            for index, waypoint in enumerate(approach_waypoints, start=2)
        )
        for label, target in targets:
            motion = executor._move_to_pose(
                target,
                executor._config.approach_steps
                if label == "pregrasp"
                else executor._config.grasp_motion_steps,
                orientation,
            )
            report["segments"].append({"label": label, **motion})

        executor._step()
        pre_close = _static_runtime_state(runtime)
        pre_close_event_count = len(events)
        pre_close_physical = _physical_contact_events(events)
        report["pre_close_state"] = pre_close
        report["pre_close_contact_events"] = pre_close_physical
        route_passed = all(
            segment["position_error_m"] <= 0.010
            and segment["orientation_error_rad"] <= 0.100
            for segment in report["segments"]
        )
        pre_close_passed = (
            route_passed
            and pre_close["contacts"]["left_contact"] is False
            and pre_close["contacts"]["right_contact"] is False
            and not pre_close_physical
        )
        if not pre_close_passed:
            raise RuntimeError("motion or pre-close contact gate failed")

        runtime.close_gripper()
        report["gripper_close_command_count"] += 1
        close_samples = []
        consecutive = 0
        for _ in range(executor._config.grasp_contact_steps):
            executor._step()
            state = _static_runtime_state(runtime)
            close_samples.append(state)
            consecutive = consecutive + 1 if _static_sample_passes(state) else 0
            if consecutive >= 5:
                break
        close_physical = _physical_contact_events(events[pre_close_event_count:])
        invalid_close_events = [
            event
            for event in close_physical
            if not _is_finger_handle_event(runtime, event)
        ]
        close_lost_events = [
            event
            for event in events[pre_close_event_count:]
            if event["event"] == "lost" and _is_finger_handle_event(runtime, event)
        ]
        qualification_state = close_samples[-1]
        qualification_contacts = qualification_state["contacts"]
        static_gate = (
            consecutive >= 5
            and not invalid_close_events
            and not close_lost_events
            and qualification_state["gripper"].get("open") is False
            and report["gripper_close_command_count"] == 1
        )
        report.update(
            {
                "selected_grasp": plan,
                "close_samples": close_samples,
                "static_qualification_state": qualification_state,
                "static_consecutive_observations": consecutive,
                "invalid_close_contact_events": invalid_close_events,
                "close_contact_lost_events": close_lost_events,
                "physical_static_grasp": "passed" if static_gate else "failed",
            }
        )
        if not static_gate:
            report["failure_classification"] = (
                "GRASP_CLOSING_COLLISION"
                if invalid_close_events
                else "PHYSICAL_STATIC_GRASP_FAILED"
            )
            raise RuntimeError("physical static grasp hard gate failed")

        axis = tuple(float(value) for value in plan["opening_axis_world"])
        axis_norm = math.sqrt(sum(value * value for value in axis))
        axis = tuple(value / axis_norm for value in axis)
        eef_start_observation = dict(runtime.read_eef_pose())
        eef_start = tuple(float(value) for value in eef_start_observation["position"])
        drawer_before = runtime.read_joint()
        static_axial = float(qualification_contacts["axial_traction"])
        pull_event_start = len(events)
        traction_samples = []
        arm_joint_samples = []
        previous_traction_state = qualification_state
        first_lost_contact_step = None
        first_topology_change_step = None
        commanded_target = tuple(
            eef_start[index] + axis[index] * distance_m for index in range(3)
        )
        final_target = commanded_target
        observed_pose = eef_start_observation
        report["traction_attempted"] = True
        report["drawer_pull_run"] = True
        ik_path = executor._precompute_pull_ik_path(
            eef_start,
            axis,
            distance_m,
            orientation,
        )
        waypoints = list(ik_path.get("accepted_waypoints", []))
        waypoint_count = len(waypoints)
        motion_converged = ik_path.get("success") is True
        if not motion_converged:
            report["failure_classification"] = "TRACTION_IK_CONTINUATION_FAILED"

        for waypoint in waypoints if motion_converged else []:
            waypoint_index = int(waypoint["waypoint_id"])
            final_target = tuple(float(value) for value in waypoint["target_position"])
            joints = [float(value) for value in waypoint["joint_positions"]]
            runtime.apply_arm_targets(joints)
            waypoint_converged = False
            for _ in range(executor._config.pull_steps_per_waypoint):
                runtime.apply_arm_targets(joints)
                executor._step()
                state = _static_runtime_state(runtime)
                contacts = state["contacts"]
                elapsed_steps = max(
                    1,
                    int(state["physics_step"])
                    - int(previous_traction_state["physics_step"]),
                )
                state["drawer_velocity_m_per_s"] = (
                    float(state["drawer_joint"])
                    - float(previous_traction_state["drawer_joint"])
                ) / (elapsed_steps * executor._config.physics_dt)
                state["relative_gripper_handle_motion"] = _relative_motion_diagnostic(
                    qualification_state,
                    state,
                    axis,
                )
                state["contact_point_migration"] = _contact_point_migration(
                    qualification_contacts,
                    contacts,
                )
                state["contact_force_components"] = _contact_force_components(contacts)
                observed_pose = state["right_gripper"]
                observed_position = tuple(float(value) for value in observed_pose["position"])
                observed_joints = [
                    float(value) for value in runtime.read_robot_joint_positions()
                ]
                joint_target_error = _joint_delta_metrics(
                    joints,
                    observed_joints,
                    ik_path["joint_names"],
                )
                observed_joint_limit_diagnostic = _joint_limit_diagnostic(
                    observed_joints,
                    runtime.arm_joint_limits(),
                    ik_path["joint_names"],
                )
                target_error = math.dist(observed_position, final_target)
                orientation_error = quaternion_angular_distance(
                    observed_pose["orientation_wxyz"], orientation
                )
                surfaces = [
                    sample.get("surface")
                    for sample in contacts.get("force_samples", [])
                    if sample.get("force_magnitude", 0.0) > 1.0e-6
                ]
                both_contacts = (
                    contacts.get("left_contact") is True
                    and contacts.get("right_contact") is True
                )
                topology_retained = (
                    contacts.get("surface_pair_valid") is True
                    and contacts.get("opposed_contact") is True
                    and sorted(str(surface) for surface in surfaces) == ["bottom", "top"]
                )
                traction_samples.append(
                    {
                        **state,
                        "phase": "traction",
                        "waypoint_index": waypoint_index,
                        "eef_target": {
                            "position": final_target,
                            "orientation_wxyz": orientation,
                        },
                        "eef_position_error_m": target_error,
                        "eef_orientation_error_rad": orientation_error,
                        "target_joint_positions": joints,
                        "observed_joint_positions": observed_joints,
                        "joint_target_error": joint_target_error,
                        "predicted_joint_limit_diagnostic": waypoint[
                            "joint_limit_diagnostic"
                        ],
                        "observed_joint_limit_diagnostic": observed_joint_limit_diagnostic,
                        "surface_pair": surfaces,
                        "execution_joint_write_count": runtime.write_counters()[
                            "execution_joint_write_count"
                        ],
                    }
                )
                arm_joint_samples.append(
                    {
                        "physics_step": state["physics_step"],
                        "waypoint_index": waypoint_index,
                        "target_joint_positions": joints,
                        "observed_joint_positions": observed_joints,
                        "joint_target_error": joint_target_error,
                        "predicted_joint_limit_diagnostic": waypoint[
                            "joint_limit_diagnostic"
                        ],
                        "observed_joint_limit_diagnostic": observed_joint_limit_diagnostic,
                    }
                )
                previous_traction_state = state
                if not both_contacts:
                    first_lost_contact_step = state["physics_step"]
                    break
                if not topology_retained:
                    first_topology_change_step = state["physics_step"]
                    break
                if joint_target_error["max_abs_delta_rad"] <= _PULL_JOINT_CONVERGENCE_RAD:
                    waypoint_converged = True
                    break
            if first_lost_contact_step is not None or first_topology_change_step is not None:
                motion_converged = False
                report["failure_classification"] = "GRASP_RETENTION_FAILURE"
                break
            if not waypoint_converged:
                motion_converged = False
                report["failure_classification"] = "TRACTION_JOINT_CONVERGENCE_FAILED"
                break

        drawer_after = runtime.read_joint()
        drawer_delta = drawer_after - drawer_before
        observed_end = tuple(float(value) for value in observed_pose["position"])
        observed_vector = tuple(
            observed_end[index] - eef_start[index] for index in range(3)
        )
        observed_axial_displacement = sum(
            observed_vector[index] * axis[index] for index in range(3)
        )
        final_state = traction_samples[-1] if traction_samples else qualification_state
        finger_start = _finger_midpoint(qualification_state)
        finger_end = _finger_midpoint(final_state)
        finger_axial_displacement = _vector_dot(
            _vector_subtract(finger_end, finger_start),
            axis,
        )
        handle_start = tuple(
            float(value) for value in qualification_state["handle"]["position"]
        )
        handle_end = tuple(float(value) for value in final_state["handle"]["position"])
        handle_axial_displacement = _vector_dot(
            _vector_subtract(handle_end, handle_start),
            axis,
        )
        displacement_accounting = _displacement_accounting(
            distance_m,
            observed_axial_displacement,
            finger_axial_displacement,
            handle_axial_displacement,
            drawer_delta,
        )
        final_target = commanded_target
        final_eef_position_error = math.dist(observed_end, commanded_target)
        max_eef_position_error = max(
            (
                float(sample["eef_position_error_m"])
                for sample in traction_samples
            ),
            default=None,
        )
        axial_samples = [
            float(sample["contacts"]["axial_traction"])
            for sample in traction_samples
            if sample["contacts"].get("axial_traction_available") is True
        ]
        positive_traction = any(value > 0.0 for value in axial_samples)
        pull_physical = _physical_contact_events(events[pull_event_start:])
        invalid_pull_events = [
            event
            for event in pull_physical
            if not _is_finger_handle_event(runtime, event)
        ]
        pull_lost_events = [
            event
            for event in events[pull_event_start:]
            if event["event"] == "lost" and _is_finger_handle_event(runtime, event)
        ]
        execution_writes = runtime.write_counters()["execution_joint_write_count"]
        contact_migration_values = [
            float(sample["contact_point_migration"]["maximum_migration_norm_m"])
            for sample in traction_samples
            if sample["contact_point_migration"]["maximum_migration_norm_m"]
            is not None
        ]
        relative_translation_values = [
            float(
                sample["relative_gripper_handle_motion"][
                    "relative_translation_norm_m"
                ]
            )
            for sample in traction_samples
        ]
        observed_margin_values = [
            float(
                sample["observed_joint_limit_diagnostic"][
                    "minimum_joint_limit_margin_rad"
                ]
            )
            for sample in traction_samples
        ]
        report.update(
            {
                "runtime": {
                    "isaac_version": runtime.isaac_sim_version(),
                    "binding_id": SEKTION_TOP_DRAWER_BINDING.binding_id,
                    "runtime_root": SEKTION_TOP_DRAWER_RUNTIME_ROOT,
                    "controller_frame": "right_gripper",
                    "observation_frame": runtime._observation_frame,
                    "ik_api": runtime.ik_api_diagnostics(),
                },
                "continuity_policy": _continuity_policy(),
                "baseline_branch_evidence": _baseline_branch_fk_evidence(
                    runtime,
                    orientation,
                ),
                "opening_axis_world": axis,
                "eef_commanded_displacement_m": distance_m,
                "eef_start": eef_start_observation,
                "eef_target": {"position": final_target, "orientation_wxyz": orientation},
                "eef_observed_end": observed_pose,
                "eef_observed_displacement_vector_m": observed_vector,
                "eef_observed_axial_displacement_m": observed_axial_displacement,
                "finger_midpoint_start_m": finger_start,
                "finger_midpoint_end_m": finger_end,
                "finger_midpoint_axial_displacement_m": finger_axial_displacement,
                "handle_start_m": handle_start,
                "handle_end_m": handle_end,
                "handle_axial_displacement_m": handle_axial_displacement,
                "displacement_accounting": displacement_accounting,
                "traction_efficiency": displacement_accounting[
                    "traction_efficiency_drawer_per_observed_right_gripper"
                ],
                "final_eef_position_error_m": final_eef_position_error,
                "max_traction_eef_position_error_m": max_eef_position_error,
                "ik_solutions": ik_path.get("attempts", []),
                "ik_continuation": ik_path,
                "pull_waypoint_count": waypoint_count,
                "arm_joint_samples": arm_joint_samples,
                "traction_samples": traction_samples,
                "static_axial_traction_n": static_axial,
                "axial_traction_samples_n": axial_samples,
                "max_axial_traction_n": max(axial_samples) if axial_samples else None,
                "min_axial_traction_n": min(axial_samples) if axial_samples else None,
                "final_axial_traction_n": axial_samples[-1] if axial_samples else None,
                "maximum_contact_point_migration_m": (
                    max(contact_migration_values) if contact_migration_values else None
                ),
                "maximum_relative_gripper_handle_translation_m": (
                    max(relative_translation_values)
                    if relative_translation_values
                    else None
                ),
                "minimum_observed_joint_limit_margin_rad": (
                    min(observed_margin_values) if observed_margin_values else None
                ),
                "positive_axial_traction_observed": positive_traction,
                "first_lost_contact_step": first_lost_contact_step,
                "first_topology_change_step": first_topology_change_step,
                "pull_contact_lost_events": pull_lost_events,
                "invalid_pull_contact_events": invalid_pull_events,
                "drawer_joint_before": drawer_before,
                "drawer_joint_after": drawer_after,
                "drawer_joint_delta": drawer_delta,
                "execution_joint_write_count": execution_writes,
            }
        )
        report["checks"] = {
            "same_git_head": expected_head is None or report["git_head"] == expected_head,
            "motion_route_passed": route_passed,
            "pre_close_contacts_zero": pre_close_passed,
            "physical_static_grasp_passed": static_gate,
            "force_convention_verified": qualification_contacts.get(
                "force_convention", {}
            ).get("newton_pair_observed")
            is True,
            "traction_ik_and_motion_converged": motion_converged,
            "traction_no_large_eef_deviation": max_eef_position_error is not None
            and max_eef_position_error <= 0.005,
            "ik_branch_continuity_passed": ik_path.get("success") is True
            and all(
                attempt.get("continuity", {}).get("branch_jump_detected") is False
                for attempt in ik_path.get("attempts", [])
                if attempt.get("ik_success") is True
            ),
            "positive_drawer_axis_traction": positive_traction,
            "drawer_moved_positive": drawer_delta > 0.0,
            "ten_mm_drawer_recovery_gate": distance_mm != 10 or drawer_delta >= 0.010,
            "contacts_retained": first_lost_contact_step is None and not pull_lost_events,
            "top_bottom_topology_retained": first_topology_change_step is None,
            "eef_moved_positive_drawer_axis": observed_axial_displacement > 0.0,
            "no_invalid_cabinet_collision_blocker": not invalid_close_events
            and not invalid_pull_events,
            "execution_drawer_writes_zero": execution_writes == 0,
        }
        report["result"] = "passed" if all(report["checks"].values()) else "failed"
        if report["result"] == "failed" and "failure_classification" not in report:
            report["failure_classification"] = (
                "TEN_MM_DRAWER_RECOVERY_GATE_FAILED"
                if distance_mm == 10
                and report["checks"]["ten_mm_drawer_recovery_gate"] is False
                else "POSITIVE_TRACTION_GATE_FAILED"
            )
    except Exception as exc:
        report["errors"].append(
            {"type": type(exc).__name__, "message": " ".join(str(exc).split())[:1000]}
        )
        report["traceback"] = traceback.format_exc()
    finally:
        _write(report_path, report)
        if executor is not None:
            try:
                executor.close()
            except Exception as exc:
                report["errors"].append(
                    {"type": type(exc).__name__, "message": f"close: {exc}"}
                )
                report["result"] = "failed"
        _write(report_path, report)
        print(
            "SCENE_FACTORY_P1_4B_TRACTION_REPORT="
            + json.dumps(report, ensure_ascii=False, allow_nan=False),
            flush=True,
        )
    return 0 if report["result"] == "passed" else 2


def _snapshot_joint(snapshot: MappingLike | dict[str, Any]) -> float:
    value = snapshot["articulation_positions"][SEKTION_TOP_DRAWER_BINDING.semantic_asset_id][SEKTION_TOP_DRAWER_BINDING.semantic_joint_id]
    return float(value)


def math_is_finite(value: float) -> bool:
    return value == value and abs(value) < 1.0e6


_PULL_TARGET = 0.05
_PULL_TOLERANCE = 0.015


def _read_child_report(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "report_version": REPORT_VERSION,
            "result": "failed",
            "errors": [{"type": "missing_child_report", "message": str(path)}],
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "report_version": REPORT_VERSION,
            "result": "failed",
            "errors": [{"type": type(exc).__name__, "message": str(exc)}],
        }


def _spawn_child(
    isaac_python: Path,
    report_path: Path,
    log_path: Path,
    *,
    negative: bool,
    expected_head: str | None,
    kinematics_only: bool = False,
    fixture_kinematics: bool = False,
    motion_only: bool = False,
    static_grasp: bool = False,
    traction_mm: int | None = None,
    fixture_base_x_m: float | None = None,
    fixture_base_y_m: float | None = None,
    fixture_base_z_m: float | None = None,
    fixture_base_yaw_deg: float | None = None,
    orientation_angle_deg: int | None = None,
) -> dict[str, Any]:
    command = [
        str(isaac_python.expanduser().resolve()),
        str(Path(__file__).resolve()),
        "--runtime-only",
        "--report",
        str(report_path),
        "--head",
        expected_head or "",
    ]
    if negative:
        command.append("--negative-only")
    if kinematics_only:
        command.append("--kinematics-only")
    if fixture_kinematics:
        command.append("--fixture-kinematics")
    if motion_only:
        command.append("--motion-only")
    if static_grasp:
        command.append("--static-grasp")
    if traction_mm is not None:
        command.extend(("--traction-mm", str(traction_mm)))
    if fixture_base_x_m is not None:
        command.extend(("--fixture-base-x-m", str(fixture_base_x_m)))
    if fixture_base_y_m is not None:
        command.extend(("--fixture-base-y-m", str(fixture_base_y_m)))
    if fixture_base_z_m is not None:
        command.extend(("--fixture-base-z-m", str(fixture_base_z_m)))
    if fixture_base_yaw_deg is not None:
        command.extend(("--fixture-base-yaw-deg", str(fixture_base_yaw_deg)))
    if orientation_angle_deg is not None:
        command.extend(("--orientation-angle-deg", str(orientation_angle_deg)))
    environment = os.environ.copy()
    environment.setdefault("OMNI_KIT_ACCEPT_EULA", "YES")
    if report_path.exists() or log_path.exists():
        raise FileExistsError("refusing to overwrite runtime evidence; use a new report basename")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        try:
            process = subprocess.run(
                command, cwd=PROJECT_ROOT, env=environment, stdout=log,
                stderr=subprocess.STDOUT, text=True, check=False, timeout=1200,
            )
            returncode = process.returncode
        except subprocess.TimeoutExpired:
            returncode = 124
            log.write("\nSCENE_FACTORY_CHILD_TIMEOUT after 1200 seconds\n")
    report = _read_child_report(report_path)
    report["process_returncode"] = returncode
    report["runtime_log"] = str(log_path)
    if returncode != 0:
        report["result"] = "failed"
        report.setdefault("errors", []).append(
            {"type": "runtime_process_failed", "message": str(returncode)}
        )
    _write(report_path, report)
    return report


def _last_axial_traction(pull: dict[str, Any]) -> float:
    samples = pull.get("contact_samples", [])
    if not isinstance(samples, list) or not samples:
        return 0.0
    value = samples[-1].get("axial_traction", 0.0)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _compare_runs(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    first_pull = first.get("actions", {}).get("pull", {}).get("evidence", {})
    second_pull = second.get("actions", {}).get("pull", {}).get("evidence", {})
    first_grasp = first.get("actions", {}).get("grasp", {}).get("evidence", {})
    second_grasp = second.get("actions", {}).get("grasp", {}).get("evidence", {})
    return {
        "same_git_head": first.get("git_head") == second.get("git_head"),
        "same_config": first.get("runtime", {}).get("asset_relative_path")
        == second.get("runtime", {}).get("asset_relative_path"),
        "same_action_statuses": {
            action: first.get("actions", {}).get(action, {}).get("status")
            == second.get("actions", {}).get(action, {}).get("status")
            for action in _ACTIONS
        },
        "pull_delta_difference_within_tolerance": abs(
            float(first_pull.get("drawer_joint_delta", 0.0))
            - float(second_pull.get("drawer_joint_delta", 0.0))
        )
        <= _PULL_TOLERANCE,
        "same_grasp_topology": first_grasp.get("grasp_topology") == second_grasp.get("grasp_topology"),
        "same_contact_surface_pairs": first_grasp.get("contact_diagnostics", {}).get("surface_pair_valid")
        == second_grasp.get("contact_diagnostics", {}).get("surface_pair_valid"),
        "same_force_direction_sign": (_last_axial_traction(first_pull) > 0.0)
        == (_last_axial_traction(second_pull) > 0.0),
        "same_contact_retention": first_pull.get("contact_maintained")
        == second_pull.get("contact_maintained"),
        "same_drawer_before_after_direction": (
            float(first_pull.get("drawer_joint_delta", 0.0)) > 0.0
        )
        == (float(second_pull.get("drawer_joint_delta", 0.0)) > 0.0),
        "both_execution_write_counts_zero": first_pull.get("execution_joint_write_count") == 0
        and second_pull.get("execution_joint_write_count") == 0,
    }


def _orientation_sweep_row(
    angle_deg: int,
    child: dict[str, Any],
    child_report_path: Path,
) -> dict[str, Any]:
    checkpoints = {
        int(item["checkpoint_mm"]): item
        for item in child.get("checkpoint_diagnostics", [])
        if isinstance(item, dict) and item.get("checkpoint_mm") is not None
    }
    limiting_sample = child.get("path_limiting_sample") or {}
    limiting_joint = limiting_sample.get("limiting_joint") or {}
    long_range_pass = (
        child.get("result") == "passed"
        and checkpoints.get(50, {}).get("reachable") is True
    )
    return {
        "orientation_family_angle_deg": angle_deg,
        "orientation_wxyz": child.get("selected_orientation_wxyz"),
        "short_range_pass": child.get("short_range_pass") is True,
        "short_range_probe_results": child.get("short_range_probe_results", {}),
        "long_range_pass": long_range_pass,
        "checkpoint_reachability": {
            str(distance): checkpoints.get(distance, {}).get("reachable") is True
            for distance in (10, 20, 35, 50)
        },
        "minimum_joint_limit_margin_rad": child.get(
            "minimum_joint_limit_margin_rad"
        ),
        "limiting_joint": limiting_joint.get("joint_name"),
        "limiting_joint_detail": limiting_joint or None,
        "joint_space_path_length_rad": child.get("joint_space_path_length_rad"),
        "maximum_waypoint_dq_rad": child.get("maximum_waypoint_dq_rad"),
        "approach_alignment_to_negative_opening": child.get(
            "approach_alignment_to_negative_opening"
        ),
        "branch_continuity_pass": child.get("checks", {}).get(
            "branch_continuity_passed"
        )
        is True,
        "execution_drawer_writes_zero": child.get("execution_joint_write_count") == 0,
        "failure_classification": child.get("failure_classification"),
        "process_returncode": child.get("process_returncode"),
        "child_report": str(child_report_path),
        "runtime_log": child.get("runtime_log"),
    }


def _run_orientation_sweep_parent(
    *,
    isaac_python: Path,
    report_path: Path,
    expected_head: str | None,
    angles: tuple[int, ...] = _ORIENTATION_FAMILY_DEGREES,
    mode: str = "long_horizon_orientation_sweep",
) -> dict[str, Any]:
    root = report_path.parent
    stem = report_path.stem
    rows: list[dict[str, Any]] = []
    for angle in angles:
        angle_label = f"p{angle}" if angle >= 0 else f"m{abs(angle)}"
        child_path = root / f"{stem}.{angle_label}.json"
        child = _spawn_child(
            isaac_python,
            child_path,
            root / f"{stem}.{angle_label}.log",
            negative=False,
            expected_head=expected_head,
            kinematics_only=True,
            traction_mm=50,
            orientation_angle_deg=angle,
        )
        rows.append(_orientation_sweep_row(angle, child, child_path))
    ranked = _rank_long_horizon_orientation_candidates(rows)
    report = {
        "report_version": REPORT_VERSION,
        "mode": mode,
        "result": "passed" if ranked else "failed",
        "git_head": expected_head,
        "isaac_python": str(isaac_python.expanduser().resolve()),
        "base_changed": False,
        "fixture_robot_base_position_m": list(
            _IsaacInteractionConfig.fixture_robot_base_position_m
        ),
        "orientation_generation_order_deg": list(angles),
        "objective_order": [
            "maximize_0_to_50mm_minimum_joint_limit_margin",
            "minimize_joint_space_path_length",
            "maximize_approach_alignment_to_negative_opening",
        ],
        "candidates": rows,
        "ranked_candidates": ranked,
        "selected_for_physical_gates": ranked[0] if ranked else None,
        "hard_stop": None if ranked else "NO_TOPOLOGY_PRESERVING_ORIENTATION_SUPPORTS_50MM",
    }
    return report


def _fixture_probe_row(
    probe: dict[str, Any],
    child: dict[str, Any],
    child_path: Path,
) -> dict[str, Any]:
    checkpoints = {
        int(item["checkpoint_mm"]): item
        for item in child.get("checkpoint_diagnostics", [])
        if isinstance(item, dict) and item.get("checkpoint_mm") is not None
    }
    reachable = [
        distance
        for distance, diagnostic in checkpoints.items()
        if diagnostic.get("reachable") is True
    ]
    limiting = child.get("limiting_sample") or {}
    limiting_joint = limiting.get("limiting_joint") or {}
    return {
        "probe_id": probe["probe_id"],
        "dimension": probe["dimension"],
        "offset": probe["offset"],
        "base_position_m": list(probe["base_position_m"]),
        "base_yaw_deg": probe["base_yaw_deg"],
        "physically_valid": child.get("physically_valid") is True,
        "fixture_validity": child.get("fixture_validity"),
        "approach_ik_viable": child.get("approach_ik_viable") is True,
        "grasp_ik_viable": child.get("grasp_ik_viable") is True,
        "reach_50mm": child.get("reach_50mm") is True,
        "max_reachable_checkpoint_mm": max(reachable, default=None),
        "checkpoint_diagnostics": {
            str(distance): checkpoints.get(distance)
            for distance in (0, 10, 20, 35, 50)
        },
        "minimum_path_margin_rad": child.get("minimum_path_margin_rad"),
        "limiting_joint": limiting_joint.get("joint_name"),
        "limiting_sample": limiting or None,
        "joint_space_path_length_rad": child.get("joint_space_path_length_rad"),
        "maximum_waypoint_dq_rad": child.get("maximum_waypoint_dq_rad"),
        "fixture_deviation_norm": child.get("fixture_deviation_norm"),
        "initial_robot_cabinet_contact_count": len(
            child.get("initial_robot_cabinet_contact_events", [])
        ),
        "drawer_joint_delta": child.get("drawer_joint_delta"),
        "execution_drawer_writes_zero": child.get("execution_joint_write_count") == 0,
        "errors": child.get("errors", []),
        "child_report": str(child_path),
        "runtime_log": child.get("runtime_log"),
    }


def _run_fixture_sensitivity_parent(
    *,
    isaac_python: Path,
    report_path: Path,
    expected_head: str | None,
) -> dict[str, Any]:
    baseline = _IsaacInteractionConfig.fixture_robot_base_position_m
    probes = [
        {
            "probe_id": "baseline",
            "dimension": "baseline",
            "offset": 0.0,
            "base_position_m": baseline,
            "base_yaw_deg": 0.0,
        },
        *[
            {
                "probe_id": f"x_{offset:+.3f}",
                "dimension": "x",
                "offset": offset,
                "base_position_m": (baseline[0] + offset, baseline[1], baseline[2]),
                "base_yaw_deg": 0.0,
            }
            for offset in (-0.025, 0.025)
        ],
        *[
            {
                "probe_id": f"y_{offset:+.3f}",
                "dimension": "y",
                "offset": offset,
                "base_position_m": (baseline[0], baseline[1] + offset, baseline[2]),
                "base_yaw_deg": 0.0,
            }
            for offset in (-0.030, 0.030)
        ],
        *[
            {
                "probe_id": f"z_{offset:+.3f}",
                "dimension": "z",
                "offset": offset,
                "base_position_m": (baseline[0], baseline[1], baseline[2] + offset),
                "base_yaw_deg": 0.0,
            }
            for offset in (-0.050, 0.050)
        ],
        *[
            {
                "probe_id": f"yaw_{offset:+.1f}",
                "dimension": "yaw",
                "offset": offset,
                "base_position_m": baseline,
                "base_yaw_deg": offset,
            }
            for offset in (-5.0, 5.0)
        ],
    ]
    root = report_path.parent
    stem = report_path.stem
    rows: list[dict[str, Any]] = []
    for probe in probes:
        label = probe["probe_id"].replace("+", "p").replace("-", "m").replace(".", "_")
        child_path = root / f"{stem}.{label}.json"
        child = _spawn_child(
            isaac_python,
            child_path,
            root / f"{stem}.{label}.log",
            negative=False,
            expected_head=expected_head,
            fixture_kinematics=True,
            fixture_base_x_m=probe["base_position_m"][0],
            fixture_base_y_m=probe["base_position_m"][1],
            fixture_base_z_m=probe["base_position_m"][2],
            fixture_base_yaw_deg=probe["base_yaw_deg"],
            orientation_angle_deg=15,
        )
        rows.append(_fixture_probe_row(probe, child, child_path))
    ranked = _rank_fixture_candidates(rows)
    complete = all(
        row["execution_drawer_writes_zero"]
        and row["drawer_joint_delta"] is not None
        and abs(float(row["drawer_joint_delta"])) <= 1.0e-6
        for row in rows
    )
    return {
        "report_version": REPORT_VERSION,
        "mode": "fixture_one_axis_sensitivity",
        "result": "passed" if complete else "failed",
        "git_head": expected_head,
        "isaac_python": str(isaac_python.expanduser().resolve()),
        "orientation_family_angle_deg": 15,
        "probe_policy": {
            "x_offset_m": [-0.025, 0.025],
            "y_offset_m": [-0.030, 0.030],
            "z_offset_m": [-0.050, 0.050],
            "yaw_offset_deg": [-5.0, 5.0],
            "one_axis_at_a_time": True,
        },
        "probes": rows,
        "ranked_50mm_candidates": ranked,
        "no_direct_drawer_writes": all(
            row["execution_drawer_writes_zero"] for row in rows
        ),
    }


def _run_fixture_coarse_search_parent(
    *,
    isaac_python: Path,
    report_path: Path,
    expected_head: str | None,
) -> dict[str, Any]:
    candidates = [
        ("x_strong", (0.745, 0.000, 0.780)),
        ("y_strong", (0.700, -0.050, 0.780)),
        ("z_strong", (0.700, 0.000, 0.860)),
        ("xy_moderate", (0.725, -0.030, 0.780)),
        ("xz_moderate", (0.725, 0.000, 0.830)),
        ("yz_moderate", (0.700, -0.030, 0.830)),
        ("xyz_moderate", (0.725, -0.030, 0.830)),
    ]
    root = report_path.parent
    stem = report_path.stem
    rows: list[dict[str, Any]] = []
    for candidate_id, base_position in candidates:
        child_path = root / f"{stem}.{candidate_id}.json"
        child = _spawn_child(
            isaac_python,
            child_path,
            root / f"{stem}.{candidate_id}.log",
            negative=False,
            expected_head=expected_head,
            fixture_kinematics=True,
            fixture_base_x_m=base_position[0],
            fixture_base_y_m=base_position[1],
            fixture_base_z_m=base_position[2],
            fixture_base_yaw_deg=0.0,
            orientation_angle_deg=15,
        )
        rows.append(
            _fixture_probe_row(
                {
                    "probe_id": candidate_id,
                    "dimension": "bounded_xyz_coarse",
                    "offset": [
                        base_position[index]
                        - _IsaacInteractionConfig.fixture_robot_base_position_m[index]
                        for index in range(3)
                    ],
                    "base_position_m": base_position,
                    "base_yaw_deg": 0.0,
                },
                child,
                child_path,
            )
        )
    ranked = _rank_fixture_candidates(rows)
    complete = all(
        row["execution_drawer_writes_zero"]
        and row["drawer_joint_delta"] is not None
        and abs(float(row["drawer_joint_delta"])) <= 1.0e-6
        for row in rows
    )
    return {
        "report_version": REPORT_VERSION,
        "mode": "fixture_bounded_xyz_coarse_search",
        "result": "passed" if complete else "failed",
        "git_head": expected_head,
        "isaac_python": str(isaac_python.expanduser().resolve()),
        "orientation_family_angle_deg": 15,
        "beneficial_dimensions": ["positive_x", "negative_y", "positive_z"],
        "candidate_policy": "three_strong_axes_three_moderate_pairs_one_moderate_triple",
        "candidate_count": len(candidates),
        "candidates": rows,
        "ranked_50mm_candidates": ranked,
        "no_direct_drawer_writes": all(
            row["execution_drawer_writes_zero"] for row in rows
        ),
    }


def _run_fixture_refinement_search_parent(
    *,
    isaac_python: Path,
    report_path: Path,
    expected_head: str | None,
) -> dict[str, Any]:
    candidates = [
        ("xyz_y_m040", (0.725, -0.040, 0.830)),
        ("xyz_z_0840", (0.725, -0.030, 0.840)),
        ("y_m040", (0.700, -0.040, 0.780)),
        ("y_m060", (0.700, -0.060, 0.780)),
        ("xy_x0715", (0.715, -0.030, 0.780)),
        ("xy_x0735", (0.735, -0.030, 0.780)),
    ]
    root = report_path.parent
    stem = report_path.stem
    rows: list[dict[str, Any]] = []
    for candidate_id, base_position in candidates:
        child_path = root / f"{stem}.{candidate_id}.json"
        child = _spawn_child(
            isaac_python,
            child_path,
            root / f"{stem}.{candidate_id}.log",
            negative=False,
            expected_head=expected_head,
            fixture_kinematics=True,
            fixture_base_x_m=base_position[0],
            fixture_base_y_m=base_position[1],
            fixture_base_z_m=base_position[2],
            fixture_base_yaw_deg=0.0,
            orientation_angle_deg=15,
        )
        rows.append(
            _fixture_probe_row(
                {
                    "probe_id": candidate_id,
                    "dimension": "local_refinement",
                    "offset": [
                        base_position[index]
                        - _IsaacInteractionConfig.fixture_robot_base_position_m[index]
                        for index in range(3)
                    ],
                    "base_position_m": base_position,
                    "base_yaw_deg": 0.0,
                },
                child,
                child_path,
            )
        )
    ranked = _rank_fixture_candidates(rows)
    complete = all(
        row["execution_drawer_writes_zero"]
        and row["drawer_joint_delta"] is not None
        and abs(float(row["drawer_joint_delta"])) <= 1.0e-6
        for row in rows
    )
    return {
        "report_version": REPORT_VERSION,
        "mode": "fixture_local_refinement",
        "result": "passed" if complete else "failed",
        "git_head": expected_head,
        "isaac_python": str(isaac_python.expanduser().resolve()),
        "orientation_family_angle_deg": 15,
        "source_candidates": ["xyz_moderate", "y_strong", "xy_moderate"],
        "candidate_count": len(candidates),
        "candidates": rows,
        "ranked_50mm_candidates": ranked,
        "no_direct_drawer_writes": all(
            row["execution_drawer_writes_zero"] for row in rows
        ),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report_path = args.report.expanduser().resolve()
    if not _outside_repository(report_path):
        raise ValueError("P1-4B reports must be written outside the repository")
    if args.runtime_only:
        if args.child_report is not None:
            report_path = args.child_report.expanduser().resolve()
        if args.fixture_kinematics:
            return _run_fixture_kinematics_child(
                report_path,
                expected_head=args.head or None,
                fixture_base_position_m=_fixture_base_position_from_args(args),
                fixture_base_yaw_deg=args.fixture_base_yaw_deg or 0.0,
                orientation_angle_deg=(
                    15
                    if args.orientation_angle_deg is None
                    else args.orientation_angle_deg
                ),
            )
        if args.kinematics_only:
            if args.traction_mm is not None:
                return _run_continuation_kinematics_child(
                    report_path,
                    expected_head=args.head or None,
                    distance_mm=args.traction_mm,
                    fixture_base_position_m=_fixture_base_position_from_args(args),
                    fixture_base_yaw_deg=args.fixture_base_yaw_deg,
                    orientation_angle_deg=args.orientation_angle_deg,
                )
            return _run_kinematics_child(report_path, expected_head=args.head or None)
        if args.motion_only:
            return _run_motion_only_child(
                report_path,
                expected_head=args.head or None,
                distance_mm=args.traction_mm,
                fixture_base_position_m=_fixture_base_position_from_args(args),
                fixture_base_yaw_deg=args.fixture_base_yaw_deg,
                orientation_angle_deg=args.orientation_angle_deg,
            )
        if args.static_grasp:
            return _run_static_grasp_child(
                report_path,
                expected_head=args.head or None,
                fixture_base_position_m=_fixture_base_position_from_args(args),
                fixture_base_yaw_deg=args.fixture_base_yaw_deg,
                orientation_angle_deg=args.orientation_angle_deg,
            )
        if args.traction_mm is not None:
            return _run_traction_child(
                report_path,
                expected_head=args.head or None,
                distance_mm=args.traction_mm,
                fixture_base_position_m=_fixture_base_position_from_args(args),
                fixture_base_yaw_deg=args.fixture_base_yaw_deg,
                orientation_angle_deg=args.orientation_angle_deg,
            )
        return _run_child(report_path, negative=args.negative_only, expected_head=args.head or None)

    expected_head = _git_head()
    root = report_path.parent
    stem = report_path.stem
    if args.fixture_sensitivity:
        report = _run_fixture_sensitivity_parent(
            isaac_python=args.isaac_python,
            report_path=report_path,
            expected_head=expected_head,
        )
        _write(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
        return 0 if report["result"] == "passed" else 2
    if args.fixture_coarse_search:
        report = _run_fixture_coarse_search_parent(
            isaac_python=args.isaac_python,
            report_path=report_path,
            expected_head=expected_head,
        )
        _write(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
        return 0 if report["result"] == "passed" else 2
    if args.fixture_refinement_search:
        report = _run_fixture_refinement_search_parent(
            isaac_python=args.isaac_python,
            report_path=report_path,
            expected_head=expected_head,
        )
        _write(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
        return 0 if report["result"] == "passed" else 2
    if args.fixture_kinematics:
        report = _spawn_child(
            args.isaac_python,
            root / f"{stem}.run.json",
            root / f"{stem}.run.log",
            negative=False,
            expected_head=expected_head,
            fixture_kinematics=True,
            fixture_base_x_m=args.fixture_base_x_m,
            fixture_base_y_m=args.fixture_base_y_m,
            fixture_base_z_m=args.fixture_base_z_m,
            fixture_base_yaw_deg=args.fixture_base_yaw_deg,
            orientation_angle_deg=(
                15
                if args.orientation_angle_deg is None
                else args.orientation_angle_deg
            ),
        )
        _write(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
        return 0 if report.get("result") == "passed" else 2
    if args.orientation_sweep or args.orientation_refinement_sweep:
        report = _run_orientation_sweep_parent(
            isaac_python=args.isaac_python,
            report_path=report_path,
            expected_head=expected_head,
            angles=(
                _ORIENTATION_REFINEMENT_SWEEP_DEGREES
                if args.orientation_refinement_sweep
                else _ORIENTATION_FAMILY_DEGREES
            ),
            mode=(
                "long_horizon_orientation_refinement_sweep"
                if args.orientation_refinement_sweep
                else "long_horizon_orientation_sweep"
            ),
        )
        _write(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
        return 0 if report["result"] == "passed" else 2
    if args.traction_mm is not None and (args.kinematics_only or args.motion_only):
        report = _spawn_child(
            args.isaac_python,
            root / f"{stem}.run.json",
            root / f"{stem}.run.log",
            negative=False,
            expected_head=expected_head,
            kinematics_only=args.kinematics_only,
            motion_only=args.motion_only,
            traction_mm=args.traction_mm,
            fixture_base_x_m=args.fixture_base_x_m,
            fixture_base_y_m=args.fixture_base_y_m,
            fixture_base_z_m=args.fixture_base_z_m,
            fixture_base_yaw_deg=args.fixture_base_yaw_deg,
            orientation_angle_deg=args.orientation_angle_deg,
        )
        _write(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
        return 0 if report.get("result") == "passed" else 2
    if args.traction_mm is not None:
        report = _spawn_child(
            args.isaac_python,
            root / f"{stem}.run.json",
            root / f"{stem}.run.log",
            negative=False,
            expected_head=expected_head,
            traction_mm=args.traction_mm,
            fixture_base_x_m=args.fixture_base_x_m,
            fixture_base_y_m=args.fixture_base_y_m,
            fixture_base_z_m=args.fixture_base_z_m,
            fixture_base_yaw_deg=args.fixture_base_yaw_deg,
            orientation_angle_deg=args.orientation_angle_deg,
        )
        _write(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
        return 0 if report.get("result") == "passed" else 2
    if args.static_grasp:
        report = _spawn_child(
            args.isaac_python,
            root / f"{stem}.run.json",
            root / f"{stem}.run.log",
            negative=False,
            expected_head=expected_head,
            static_grasp=True,
            fixture_base_x_m=args.fixture_base_x_m,
            fixture_base_y_m=args.fixture_base_y_m,
            fixture_base_z_m=args.fixture_base_z_m,
            fixture_base_yaw_deg=args.fixture_base_yaw_deg,
            orientation_angle_deg=args.orientation_angle_deg,
        )
        _write(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
        return 0 if report.get("result") == "passed" else 2
    if args.kinematics_only:
        run1_path = root / f"{stem}.run1.json"
        run2_path = root / f"{stem}.run2.json"
        run1 = _spawn_child(
            args.isaac_python,
            run1_path,
            root / f"{stem}.run1.log",
            negative=False,
            kinematics_only=True,
            expected_head=expected_head,
            orientation_angle_deg=args.orientation_angle_deg,
        )
        run2 = _spawn_child(
            args.isaac_python,
            run2_path,
            root / f"{stem}.run2.log",
            negative=False,
            kinematics_only=True,
            expected_head=expected_head,
            orientation_angle_deg=args.orientation_angle_deg,
        )
        report = {
            "report_version": REPORT_VERSION,
            "result": "failed",
            "mode": "kinematics_only",
            "git_head": expected_head,
            "isaac_python": str(args.isaac_python.expanduser().resolve()),
            "run1": run1,
            "run2": run2,
            "checks": {
                "same_exact_head": expected_head is not None
                and run1.get("git_head") == expected_head
                and run2.get("git_head") == expected_head,
                "run1_passed": run1.get("result") == "passed",
                "run2_passed": run2.get("result") == "passed",
                "same_frame_mapping": run1.get("runtime", {}).get("diagnostics", {}).get("configured_kinematic_frame")
                == run2.get("runtime", {}).get("diagnostics", {}).get("configured_kinematic_frame")
                == "right_gripper",
                "same_observation_frame": run1.get("runtime", {}).get("diagnostics", {}).get("observation_frame")
                == run2.get("runtime", {}).get("diagnostics", {}).get("observation_frame")
                == "panda_hand",
                "same_selected_angle": run1.get("runtime", {}).get("diagnostics", {}).get("selected_grasp", {}).get("orientation_family_angle_deg")
                == run2.get("runtime", {}).get("diagnostics", {}).get("selected_grasp", {}).get("orientation_family_angle_deg"),
                "no_contact_attempted": run1.get("runtime", {}).get("contact_attempted") is False
                and run2.get("runtime", {}).get("contact_attempted") is False,
            },
        }
        report["result"] = "passed" if all(report["checks"].values()) else "failed"
        _write(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
        return 0 if report["result"] == "passed" else 2
    if args.motion_only:
        run1_path = root / f"{stem}.run1.json"
        run2_path = root / f"{stem}.run2.json"
        run1 = _spawn_child(
            args.isaac_python,
            run1_path,
            root / f"{stem}.run1.log",
            negative=False,
            expected_head=expected_head,
            motion_only=True,
            fixture_base_x_m=args.fixture_base_x_m,
            fixture_base_y_m=args.fixture_base_y_m,
            fixture_base_z_m=args.fixture_base_z_m,
            fixture_base_yaw_deg=args.fixture_base_yaw_deg,
            orientation_angle_deg=args.orientation_angle_deg,
        )
        run2 = _spawn_child(
            args.isaac_python,
            run2_path,
            root / f"{stem}.run2.log",
            negative=False,
            expected_head=expected_head,
            motion_only=True,
            fixture_base_x_m=args.fixture_base_x_m,
            fixture_base_y_m=args.fixture_base_y_m,
            fixture_base_z_m=args.fixture_base_z_m,
            fixture_base_yaw_deg=args.fixture_base_yaw_deg,
            orientation_angle_deg=args.orientation_angle_deg,
        )
        report = {
            "report_version": REPORT_VERSION,
            "result": "failed",
            "mode": "motion_only",
            "git_head": expected_head,
            "isaac_python": str(args.isaac_python.expanduser().resolve()),
            "run1": run1,
            "run2": run2,
            "checks": {
                "same_exact_head": expected_head is not None
                and run1.get("git_head") == expected_head
                and run2.get("git_head") == expected_head,
                "run1_passed": run1.get("result") == "passed",
                "run2_passed": run2.get("result") == "passed",
                "same_target": run1.get("target_grasp_position")
                == run2.get("target_grasp_position"),
                "same_orientation": run1.get("orientation_wxyz")
                == run2.get("orientation_wxyz"),
                "same_fixture_base": run1.get("runtime", {}).get(
                    "fixture_robot_base_position_m"
                )
                == run2.get("runtime", {}).get("fixture_robot_base_position_m")
                == list(_fixture_base_position_from_args(args)),
                "same_fixture_yaw": run1.get("runtime", {}).get(
                    "fixture_robot_base_yaw_deg"
                )
                == run2.get("runtime", {}).get("fixture_robot_base_yaw_deg")
                == (args.fixture_base_yaw_deg or 0.0),
                "both_grippers_open": run1.get("checks", {}).get("gripper_remained_open") is True
                and run2.get("checks", {}).get("gripper_remained_open") is True,
                "no_contact_acceptance": run1.get("contact_acceptance_run") is False
                and run2.get("contact_acceptance_run") is False,
                "no_drawer_pull": run1.get("drawer_pull_run") is False
                and run2.get("drawer_pull_run") is False,
            },
        }
        report["result"] = "passed" if all(report["checks"].values()) else "failed"
        _write(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
        return 0 if report["result"] == "passed" else 2
    run1_path = root / f"{stem}.run1.json"
    run2_path = root / f"{stem}.run2.json"
    negative_path = root / f"{stem}.negative.json"
    run1 = _spawn_child(args.isaac_python, run1_path, root / f"{stem}.run1.log", negative=False, expected_head=expected_head)
    run2 = _spawn_child(args.isaac_python, run2_path, root / f"{stem}.run2.log", negative=False, expected_head=expected_head)
    negative = _spawn_child(args.isaac_python, negative_path, root / f"{stem}.negative.log", negative=True, expected_head=expected_head)
    evidence_gate = validate_acceptance(run1, run2, negative, expected_head=expected_head or "")
    report = {
        "report_version": REPORT_VERSION, "result": evidence_gate["result"],
        "scope": "frozen full 0.35 m drawer task; not the 0.05 m primitive pilot",
        "git_head": expected_head, "run1": run1, "run2": run2, "negative": negative,
        "evidence_gate": evidence_gate,
    }
    _write(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
    return 0 if report["result"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
