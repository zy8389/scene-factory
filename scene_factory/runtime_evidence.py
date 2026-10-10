"""Recompute runtime acceptance from recorded simulator measurements.

This module deliberately has no Isaac or NumPy dependency. A trace can prove a
failed execution is complete without proving that its scene or task passed.
"""
from __future__ import annotations

import copy
import math
from typing import Any

from .quality import assess_scene_quality
from .robotics import (
    MUG_LIFT_PHASE_TRANSITIONS,
    MugLiftPhase,
    build_pick_place_acceptance_report,
    build_robot_acceptance_report,
)
from .tasks import TaskEvaluator


def _number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _vector(value: Any, size: int) -> bool:
    return isinstance(value, (list, tuple)) and len(value) == size and all(_number(v) for v in value)


def _finite_tree(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite_tree(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return all(_finite_tree(item) for item in value)
    return True


def _norm(vector: list[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def _result(checks: dict[str, bool], metrics: dict[str, Any], reasons: list[str], *, empty: bool = False) -> dict:
    return {
        "status": "not_verified" if empty else ("failed" if reasons else "passed"),
        "checks": checks,
        "metrics": metrics,
        "failure_reasons": list(dict.fromkeys(reasons)),
    }


def _timeline(rows: list[dict], step_key: str, dt: float) -> bool:
    return bool(rows) and all(
        isinstance(row, dict)
        and not isinstance(row.get(step_key), bool)
        and row.get(step_key) == index
        and isinstance(row.get(step_key), int)
        and _number(row.get("timestamp"))
        and math.isclose(row["timestamp"], index * dt, rel_tol=0.0, abs_tol=max(1e-7, dt * 1e-4))
        for index, row in enumerate(rows)
    )


def assess_physics_trace(scene: dict, samples: list[dict], settings: dict) -> dict:
    """Verify settling, motion and original support from real world bounds.

    Samples cover step zero through at least ``settle_steps``, at physics_dt.
    Dynamic objects require every measurement in every sample. Static supports
    may be recorded once at step zero or in every sample. The first-stage batch
    supports floor and the top of a support object; other named surfaces are
    rejected until their actual surface geometry is recorded.
    """
    checks: dict[str, bool] = {}
    metrics: dict[str, Any] = {"sample_count": len(samples), "evidence_complete": False}
    reasons: list[str] = []
    if not samples:
        return _result(checks, metrics, ["physics_trace_missing"], empty=True)
    defaults = {
        "physics_dt": 1 / 60,
        "settle_steps": 240,
        "stable_window_steps": 60,
        "max_linear_speed_mps": 0.01,
        "max_angular_speed_rps": 0.1,
        "max_position_drift_m": 0.005,
        "support_tolerance_m": 0.01,
    }
    config = {key: settings.get(key, value) for key, value in defaults.items()}
    checks["settings_valid"] = all(_number(v) and v > 0 for v in config.values()) and all(
        not isinstance(config[key], bool) and isinstance(config[key], int)
        for key in ("settle_steps", "stable_window_steps")
    ) and 2 <= config["stable_window_steps"] <= config["settle_steps"]
    if not checks["settings_valid"]:
        return _result(checks, metrics, ["physics_settings_invalid"])
    dt = config["physics_dt"]
    checks["continuous_time"] = _timeline(samples, "step", dt)
    checks["enough_steps"] = len(samples) >= config["settle_steps"] + 1
    raw_objects = scene.get("objects", [])
    objects = {item["object_id"]: item for item in raw_objects if isinstance(item, dict) and isinstance(item.get("object_id"), str)}
    checks["scene_objects_valid"] = bool(objects) and len(objects) == len(raw_objects)
    dynamic_ids = [key for key, item in objects.items() if item.get("dynamic") is True]
    checks["dynamic_objects_present"] = bool(dynamic_ids)
    measurements: dict[str, list[dict]] = {key: [] for key in dynamic_ids}
    fields = {"position": 3, "orientation_wxyz": 4, "linear_velocity_mps": 3,
              "angular_velocity_rps": 3, "world_bbox_min": 3, "world_bbox_max": 3}
    checks["measurements_valid"] = True
    for sample in samples:
        recorded = sample.get("objects", {}) if isinstance(sample, dict) else {}
        if not isinstance(recorded, dict):
            checks["measurements_valid"] = False
            continue
        for object_id in dynamic_ids:
            item = recorded.get(object_id)
            valid = isinstance(item, dict) and all(_vector(item.get(field), size) for field, size in fields.items())
            if valid:
                valid = 0.99 <= _norm(item["orientation_wxyz"]) <= 1.01 and all(
                    low <= high for low, high in zip(item["world_bbox_min"], item["world_bbox_max"], strict=True)
                )
            if not valid:
                checks["measurements_valid"] = False
            else:
                measurements[object_id].append(item)
    checks["finite_values"] = all(_finite_tree(sample) for sample in samples)
    for key in ("continuous_time", "enough_steps", "scene_objects_valid", "dynamic_objects_present", "measurements_valid", "finite_values"):
        if not checks[key]:
            reasons.append(f"physics_{key}_failed")
    metrics.update({"dynamic_object_count": len(dynamic_ids), "physics_dt": dt,
                    "settle_steps": config["settle_steps"], "stable_window_steps": config["stable_window_steps"]})
    if not checks["measurements_valid"] or not checks["finite_values"]:
        return _result(checks, metrics, reasons)
    per_object: dict[str, dict] = {}
    window_size = config["stable_window_steps"]
    for object_id in dynamic_ids:
        rows = measurements[object_id]
        window = rows[-window_size:]
        linear = max(_norm(row["linear_velocity_mps"]) for row in window)
        angular = max(_norm(row["angular_velocity_rps"]) for row in window)
        # Maximum pairwise distance catches oscillation and drift even when the
        # endpoints coincide or the recorded velocity is near zero.
        drift = max((math.dist(a["position"], b["position"]) for a in window for b in window), default=0.0)
        stable = len(window) == window_size and linear <= config["max_linear_speed_mps"] and angular <= config["max_angular_speed_rps"] and drift <= config["max_position_drift_m"]
        support = objects[object_id].get("support")
        supported, support_measured, max_gap = True, True, 0.0
        if support == "floor":
            room = scene.get("room_dimensions_m")
            support_measured = _vector(room, 3)
            bounds = ([-room[0] / 2, -room[1] / 2, 0], [room[0] / 2, room[1] / 2, 0]) if support_measured else None
        elif isinstance(support, str):
            support_id, _, surface = support.partition(":")
            first_objects = samples[0].get("objects", {})
            first_support = first_objects.get(support_id, {}) if isinstance(first_objects, dict) else {}
            support_measured = surface in ("", "top") and _vector(first_support.get("world_bbox_min"), 3) and _vector(first_support.get("world_bbox_max"), 3)
            bounds = (first_support["world_bbox_min"], first_support["world_bbox_max"]) if support_measured else None
        else:
            bounds = None
            support_measured = False
        tolerance = config["support_tolerance_m"]
        if bounds:
            low, high = bounds
            for row in rows:
                bottom, top = row["world_bbox_min"], row["world_bbox_max"]
                supported = supported and all(bottom[axis] >= low[axis] - tolerance and top[axis] <= high[axis] + tolerance for axis in (0, 1)) and bottom[2] >= high[2] - tolerance
            max_gap = max(abs(row["world_bbox_min"][2] - high[2]) for row in window)
            supported = supported and max_gap <= tolerance
        else:
            supported = False
        per_object[object_id] = {"max_linear_speed_mps": linear, "max_angular_speed_rps": angular,
                                 "max_position_drift_m": drift, "max_support_gap_m": max_gap,
                                 "support": support, "stable": stable, "support_measured": support_measured,
                                 "support_retained": supported}
        if not stable:
            reasons.append(f"physics_not_stable:{object_id}")
        if not support_measured:
            reasons.append(f"physics_support_measurement_missing:{object_id}")
        elif not supported:
            reasons.append(f"physics_support_lost:{object_id}")
    metrics["objects"] = per_object
    checks["stable"] = all(item["stable"] for item in per_object.values())
    checks["support_measured"] = all(item["support_measured"] for item in per_object.values())
    checks["support_retained"] = all(item["support_retained"] for item in per_object.values())
    metrics["evidence_complete"] = all(checks[key] for key in (
        "continuous_time", "enough_steps", "scene_objects_valid", "dynamic_objects_present", "measurements_valid", "finite_values", "support_measured"
    ))
    return _result(checks, metrics, reasons)


def _force_contact(diagnostics: dict, target: str) -> bool:
    if not all(diagnostics.get(key) is True for key in (
        "contact_report_available", "contact_report_subscribed", "contact_force_read_valid"
    )):
        return False
    pairs = diagnostics.get("contact_force_pairs", [])
    if not isinstance(pairs, list):
        return False
    target_root = f"/World/Objects/{target}"
    for pair in pairs:
        if not isinstance(pair, dict) or pair.get("event_type") != "CONTACT_FORCE":
            continue
        paths = [pair.get("collider0"), pair.get("collider1")]
        target_index = next((i for i, path in enumerate(paths) if isinstance(path, str) and (path == target_root or path.startswith(target_root + "/"))), None)
        if target_index is None or not isinstance(paths[1 - target_index], str):
            continue
        finger_path = paths[1 - target_index]
        finger = any(f"/{name}" in finger_path for name in ("panda_leftfinger", "panda_rightfinger"))
        force = pair.get("force")
        magnitude = pair.get("force_magnitude")
        if finger and _vector(force, 3) and _number(magnitude) and magnitude > 1e-6 and math.isclose(_norm(force), magnitude, rel_tol=1e-4, abs_tol=1e-6):
            return True
    return False


def assess_task_trace(scene: dict, frames: list[dict], settings: dict) -> dict:
    """Replay each oracle from positions and force reports, including failures.

    settings.runtime_summary supplies actual official Franka source and final
    controller statuses. The original trace is never modified. Stored oracle
    booleans are compared with a fresh TaskEvaluator, not treated as evidence.
    """
    checks: dict[str, bool] = {}
    metrics: dict[str, Any] = {"frame_count": len(frames), "evidence_complete": False, "observed_success": False}
    reasons: list[str] = []
    if not frames:
        return _result(checks, metrics, ["task_trace_missing"], empty=True)
    task = scene.get("task", {})
    predicate = task.get("success", {}).get("predicate")
    if predicate not in ("lifted", "pick_and_place"):
        return _result(checks, metrics, ["task_predicate_not_supported"], empty=True)
    dt = settings.get("physics_dt", 1 / 60)
    if not _number(dt) or dt <= 0:
        return _result({"settings_valid": False}, metrics, ["task_settings_invalid"])
    checks["continuous_time"] = _timeline(frames, "simulation_step", dt)
    checks["finite_values"] = all(_finite_tree(frame) for frame in frames)
    object_ids = {item["object_id"] for item in scene.get("objects", []) if isinstance(item, dict) and isinstance(item.get("object_id"), str)}
    target = task.get("target_object") or task.get("success", {}).get("subject")
    checks["frame_fields_valid"] = bool(object_ids) and target in object_ids
    states = []
    for frame in frames:
        if not isinstance(frame, dict):
            checks["frame_fields_valid"] = False
            continue
        recorded = frame.get("objects", {})
        robot = frame.get("robot", {})
        if not isinstance(recorded, dict) or not isinstance(robot, dict):
            checks["frame_fields_valid"] = False
            continue
        state = {}
        for key in object_ids:
            item = recorded.get(key, {})
            if not isinstance(item, dict) or not _vector(item.get("position"), 3):
                checks["frame_fields_valid"] = False
            else:
                state[key] = tuple(item["position"])
        states.append(state)
        ee = robot.get("end_effector_pose", {})
        joints = robot.get("joint_positions")
        velocities = robot.get("joint_velocities")
        checks["frame_fields_valid"] = checks["frame_fields_valid"] and (
            isinstance(frame.get("task_success"), bool)
            and isinstance(frame.get("terminated"), bool)
            and isinstance(frame.get("truncated"), bool)
            and _number(frame.get("reward"))
            and isinstance(joints, (list, tuple)) and len(joints) == 9 and all(_number(v) for v in joints)
            and isinstance(velocities, (list, tuple)) and len(velocities) == 9 and all(_number(v) for v in velocities)
            and isinstance(ee, dict) and _vector(ee.get("position"), 3) and _vector(ee.get("orientation_wxyz"), 4)
            and isinstance(robot.get("grasp_diagnostics"), dict)
            and isinstance(robot.get("task_oracle"), dict)
        )
    for key in ("continuous_time", "finite_values", "frame_fields_valid"):
        if not checks[key]:
            reasons.append(f"task_{key}_failed")
    if not checks["frame_fields_valid"] or not checks["finite_values"]:
        return _result(checks, metrics, reasons)
    try:
        evaluator = TaskEvaluator(task, states[0])
    except (ValueError, TypeError, KeyError):
        return _result(checks, metrics, reasons + ["task_spec_invalid"])
    checks.update({"phase_transitions": True, "terminal_consistency": True,
                   "oracle_consistency": True, "contact_consistency": True})
    replayed = []
    contact_steps = 0
    contact_lift_steps = 0
    valid_api_steps = 0
    previous_phase = None
    for index, (frame, state) in enumerate(zip(frames, states, strict=True)):
        robot = frame["robot"]
        diagnostics = robot["grasp_diagnostics"]
        contact = _force_contact(diagnostics, target)
        api_valid = all(diagnostics.get(key) is True for key in ("contact_report_available", "contact_report_subscribed", "contact_force_read_valid"))
        valid_api_steps += int(api_valid)
        contact_steps += int(contact)
        lift = state[target][2] - states[0][target][2]
        holding = contact and diagnostics.get("gripper_open") is False
        contact_lift_steps += int(holding and lift >= float(task["success"].get("min_lift_delta_m", task["success"].get("min_height_delta_m", 0.1))))
        # API-unavailable frames can still be complete failure evidence. A false
        # positive contact flag, however, contradicts the recorded force view.
        if diagnostics.get("finger_target_contact") is True and not contact:
            checks["contact_consistency"] = False
        elif api_valid and diagnostics.get("finger_target_contact") is not contact:
            checks["contact_consistency"] = False
        evidence = dict(diagnostics)
        evidence["finger_target_contact"] = contact
        try:
            oracle = evaluator.status(state, evidence)
        except (ValueError, TypeError, KeyError, OverflowError):
            return _result(checks, metrics, reasons + ["task_replay_failed"])
        replayed.append(oracle)
        stored_oracle = robot["task_oracle"]
        if frame["task_success"] != bool(oracle["task_success"]):
            checks["oracle_consistency"] = False
        for key in ("task_success", "pick_success", "released", "placement_stable", "placement_stable_steps"):
            if key in oracle and stored_oracle.get(key) != oracle[key]:
                checks["oracle_consistency"] = False
        phase = robot.get("phase")
        try:
            phase_enum = MugLiftPhase(phase)
        except (ValueError, TypeError):
            checks["phase_transitions"] = False
            phase_enum = None
        if index == 0 and phase != "PRE_GRASP":
            checks["phase_transitions"] = False
        if previous_phase and phase != previous_phase and phase != "FAILED":
            allowed = MUG_LIFT_PHASE_TRANSITIONS.get(MugLiftPhase(previous_phase), ())
            if phase_enum not in allowed or (predicate == "pick_and_place" and previous_phase == "LIFT" and phase == "DONE"):
                checks["phase_transitions"] = False
        if phase_enum is not None:
            previous_phase = phase
        terminal = frame["terminated"] or frame["truncated"]
        if frame["terminated"] and frame["truncated"]:
            checks["terminal_consistency"] = False
        if (index < len(frames) - 1 and terminal) or (index == len(frames) - 1 and not terminal):
            checks["terminal_consistency"] = False
        if phase == "DONE" and (not frame["terminated"] or frame["truncated"]):
            checks["terminal_consistency"] = False
        if terminal and phase not in ("DONE", "FAILED"):
            checks["terminal_consistency"] = False
        if frame["reward"] != (1.0 if phase == "DONE" else 0.0):
            checks["terminal_consistency"] = False
    for key in ("phase_transitions", "terminal_consistency", "oracle_consistency", "contact_consistency"):
        if not checks[key]:
            reasons.append(f"task_{key}_failed")
    summary = settings.get("runtime_summary", {})
    summary = summary if isinstance(summary, dict) else {}
    checks["official_franka"] = summary.get("robot_asset_source", settings.get("robot_asset_source")) == "nucleus_franka_usd" and summary.get("official_isaac_asset", settings.get("official_isaac_asset")) is True
    checks["contact_api_available"] = valid_api_steps > 0
    checks["physical_grasp_observed"] = contact_lift_steps > 0
    final = copy.deepcopy(frames[-1])
    final["task_success"] = bool(replayed[-1]["task_success"])
    final["robot"]["task_oracle"] = replayed[-1]
    common = {"scene_id": scene["scene_id"], "initial_observation": frames[0], "final_observation": final,
              "steps": frames[-1]["simulation_step"], "ik": str(summary.get("ik", "not_run")),
              "failure_reason": summary.get("failure_reason") or final["robot"].get("failure_reason"),
              "target_object": target, "asset_id": task.get("asset_id", "mug_001"),
              "grasp_diagnostics": final["robot"]["grasp_diagnostics"]}
    if predicate == "pick_and_place":
        acceptance = build_pick_place_acceptance_report(**common, pick=str(summary.get("pick_status", final["robot"].get("pick_status", "not_run"))),
            place=str(summary.get("place_status", final["robot"].get("place_status", "not_run"))), released=summary.get("released", final["robot"].get("released")) is True)
    else:
        acceptance = build_robot_acceptance_report(**common, grasp=str(summary.get("grasp", "not_run")))
    checks["acceptance_passed"] = acceptance["result"] == "passed"
    checks["runtime_no_failure"] = not summary.get("failure_reason") and not final["robot"].get("failure_reason")
    for key in ("official_franka", "contact_api_available", "physical_grasp_observed", "acceptance_passed", "runtime_no_failure"):
        if not checks[key]:
            reasons.append(f"task_{key}_failed")
    runtime_failure_reason = common["failure_reason"]
    if isinstance(runtime_failure_reason, str) and runtime_failure_reason:
        reasons.append(runtime_failure_reason)
    integrity = ("continuous_time", "finite_values", "frame_fields_valid", "phase_transitions", "terminal_consistency", "oracle_consistency", "contact_consistency")
    metrics.update({"evidence_complete": all(checks[key] for key in integrity),
                    "observed_success": not reasons, "replayed_task_success": bool(replayed[-1]["task_success"]),
                    "contact_steps": contact_steps, "contact_lift_steps": contact_lift_steps,
                    "contact_api_valid_steps": valid_api_steps, "final_oracle": replayed[-1],
                    "acceptance_report": acceptance, "final_phase": final["robot"].get("phase"),
                    "final_step": frames[-1]["simulation_step"], "runtime_failure_reason": runtime_failure_reason,
                    "isaac_sim_version": settings.get("isaac_sim_version")})
    return _result(checks, metrics, reasons)


def runtime_quality(layout: dict, validation: dict, physics_result: dict | None, task_result: dict | None) -> dict:
    """Attach runtime layers while preserving the existing quality contract."""
    quality = assess_scene_quality(layout, validation)
    for name, report in (("physics", physics_result), ("task", task_result)):
        if report is None:
            continue
        if not isinstance(report, dict) or report.get("status") not in ("passed", "failed", "not_verified"):
            raise ValueError(f"invalid {name} evidence report")
        layer = copy.deepcopy(report)
        layer["basis"] = "recorded_simulator_trace"
        layer["scope"] = quality["layers"][name]["scope"]
        layer["issues"] = [{"code": code, "category": name, "severity": "error", "message": code, "object_ids": []}
                           for code in layer.get("failure_reasons", [])]
        quality["layers"][name] = layer
    highest = "none"
    for name in ("layout", "physics", "task"):
        if quality["layers"][name]["status"] != "passed":
            break
        highest = name
    quality["highest_verified_level"] = highest
    return quality
