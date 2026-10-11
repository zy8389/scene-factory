"""Bounded, source-checked playback payloads from recorded simulator state.

Playback preserves every recorded frame. It does not infer robot link poses or
object rotations that were absent from the original trajectory.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

from .dataset import DatasetError
from .runtime_evidence import assess_task_trace


MAX_TRACE_BYTES = 64 * 1024 * 1024
MAX_TRACE_FRAMES = 20_000
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_SCENE_OBJECTS = 1_000


def _read(root: Path, relative: str, maximum: int) -> bytes:
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise DatasetError(f"unsafe replay artifact: {relative}")
    try:
        with path.open("rb") as handle:
            raw = handle.read(maximum + 1)
    except OSError as exc:
        raise DatasetError(f"missing replay artifact: {relative}") from exc
    if not raw or len(raw) > maximum:
        raise DatasetError(f"empty or oversized replay artifact: {relative}")
    return raw


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DatasetError("duplicate key in replay JSON")
        result[key] = value
    return result


def _parse(raw: bytes) -> Any:
    def invalid_constant(value: str) -> None:
        raise DatasetError(f"nonfinite replay value: {value}")

    try:
        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_pairs, parse_constant=invalid_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise DatasetError("invalid replay JSON") from exc


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _verified(root: Path, relative: str, expected: Any, maximum: int = MAX_JSON_BYTES) -> bytes:
    raw = _read(root, relative, maximum)
    if not isinstance(expected, str) or _digest(raw) != expected:
        raise DatasetError(f"replay artifact hash mismatch: {relative}")
    return raw


def _vector(value: Any, size: int) -> bool:
    return isinstance(value, list) and len(value) == size and all(
        isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item)
        for item in value
    )


def _contacts(diagnostics: dict, target: str) -> dict[str, Any]:
    available = all(diagnostics.get(key) is True for key in (
        "contact_report_available", "contact_report_subscribed", "contact_force_read_valid"
    ))
    result: dict[str, Any] = {"left": False, "right": False, "available": available, "force_pairs": []}
    if not available:
        return result
    root = f"/World/Objects/{target}"
    pairs = diagnostics.get("contact_force_pairs", [])
    if not isinstance(pairs, list):
        raise DatasetError("invalid replay contact pairs")
    for pair in pairs:
        if not isinstance(pair, dict) or pair.get("event_type") != "CONTACT_FORCE":
            continue
        paths = [pair.get("collider0"), pair.get("collider1")]
        index = next((index for index, path in enumerate(paths)
                      if isinstance(path, str) and (path == root or path.startswith(root + "/"))), None)
        if index is None or not isinstance(paths[1 - index], str):
            continue
        force, magnitude = pair.get("force"), pair.get("force_magnitude")
        if not _vector(force, 3) or isinstance(magnitude, bool) or not isinstance(magnitude, (int, float)):
            continue
        if not math.isfinite(magnitude) or magnitude <= 1e-6 or not math.isclose(
            math.sqrt(sum(item * item for item in force)), magnitude, rel_tol=1e-4, abs_tol=1e-6
        ):
            continue
        finger = paths[1 - index]
        for side in ("left", "right"):
            if f"/panda_{side}finger" in finger:
                result[side] = True
                result["force_pairs"].append(copy.deepcopy(pair))
    return result


def replay_payload(case_dir: str | Path, *, assets: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return all task states of a completed case, including valid failures.

    The caller first validates its batch. This read additionally binds the bytes
    actually served to the manifest, case result, worker report, and trace hashes
    and independently recomputes task evidence. No Isaac or NumPy is imported.
    """
    root = Path(case_dir)
    try:
        if root.is_symlink() or not root.is_dir() or root.parent.name != "cases":
            raise DatasetError("replay case must belong to a runtime batch")
        batch = root.parent.parent
        manifest = _parse(_read(batch, "manifest.json", MAX_JSON_BYTES))
        if manifest.get("schema_version") != "scene_factory.runtime_batch.v1" or manifest.get("state") != "complete":
            raise DatasetError("replay batch is not complete")
        entries = [entry for entry in manifest["cases"] if entry.get("case_id") == root.name]
        if len(entries) != 1:
            raise DatasetError("replay case is not uniquely bound to its batch")
        entry = entries[0]
        result = _parse(_verified(root, "result.json", entry.get("result_sha256")))
        if (result.get("schema_version") != "scene_factory.runtime_case.v1"
                or result.get("case_id") != root.name or result.get("run_id") != manifest.get("run_id")):
            raise DatasetError("replay case identity mismatch")
        stage = result["stages"]["task"]
        if (result.get("evidence_complete") is not True or stage.get("state") != "attempted"
                or stage.get("integrity_errors") != []
                or stage.get("assessment", {}).get("metrics", {}).get("evidence_complete") is not True):
            raise DatasetError("task has no complete replay evidence")
        layout = _parse(_verified(root, "layout.json", result["input_sha256"]["layout"]))
        objects = layout["objects"]
        if not isinstance(objects, list) or not 1 <= len(objects) <= MAX_SCENE_OBJECTS:
            raise DatasetError("invalid replay object count")
        object_ids = {item["object_id"] for item in objects}
        if len(object_ids) != len(objects):
            raise DatasetError("duplicate replay object identifiers")
        report = _parse(_verified(root, "task/worker_report.json", stage.get("worker_report_sha256")))
        job = _parse(_verified(root, "task/job.json", stage.get("job_sha256")))
        parent = _parse(_verified(root, "task/parent_process.json", stage.get("parent_sha256")))
        if (any(report.get(key) != job.get(key) for key in ("run_id", "case_id", "mode", "bindings"))
                or report.get("run_id") != manifest.get("run_id") or report.get("case_id") != root.name
                or report.get("mode") != "task" or report.get("schema_version") != "scene_factory.runtime_worker.v1"
                or report.get("worker_exit_code") != 0 or parent.get("returncode") != 0
                or parent.get("timed_out") is not False or parent.get("job_sha256") != stage.get("job_sha256")
                or report.get("result") not in {"task_success", "task_failure", "truncated"}
                or report.get("settings") != job.get("settings")):
            raise DatasetError("replay worker execution mismatch")
        if job["bindings"]["input_sha256"]["layout"] != result["input_sha256"]["layout"]:
            raise DatasetError("replay worker layout mismatch")
        trace_path = Path(report["trace_path"])
        if not trace_path.is_absolute():
            trace_path = root / "task" / trace_path
        if trace_path.resolve() != (root / "task/task_trace.jsonl").resolve():
            raise DatasetError("unsafe replay trace location")
        raw = _verified(root, "task/task_trace.jsonl", report.get("trace_sha256"), MAX_TRACE_BYTES)
        lines = raw.splitlines()
        if not 1 <= len(lines) <= MAX_TRACE_FRAMES or any(not line.strip() for line in lines):
            raise DatasetError("invalid replay frame count or blank frame")
        recorded = [_parse(line) for line in lines]
        completed = report.get("completed_steps")
        if isinstance(completed, bool) or not isinstance(completed, int) or completed != len(recorded) - 1:
            raise DatasetError("replay worker frame count mismatch")
        if report.get("trace_rows") != len(recorded):
            raise DatasetError("replay trace row count mismatch")
        settings = {**job["settings"], "runtime_summary": report["runtime_summary"],
                    "isaac_sim_version": report["isaac_sim_version"],
                    "robot_asset_source": report.get("robot_asset_source")}
        assessment = assess_task_trace(layout, recorded, settings)
        if report["result"] != "task_success" and assessment["status"] == "passed":
            assessment = {**assessment, "status": "failed", "failure_reasons": [
                *assessment.get("failure_reasons", []), report.get("failure_reason") or "worker_report_not_passed"
            ]}
        if assessment.get("metrics", {}).get("evidence_complete") is not True or assessment != stage["assessment"]:
            raise DatasetError("replay evidence differs from its recorded assessment")
        target = layout["task"].get("target_object") or layout["task"]["success"].get("subject")
        frames = []
        missing_orientation = set()
        missing_fingers = False
        for row in recorded:
            positions = {}
            for object_id in object_ids:
                item = row["objects"][object_id]
                positions[object_id] = {"position": list(item["position"])}
                if "orientation_wxyz" in item:
                    quaternion = item["orientation_wxyz"]
                    if not _vector(quaternion, 4) or not 0.99 <= math.sqrt(sum(value * value for value in quaternion)) <= 1.01:
                        raise DatasetError("invalid recorded object rotation")
                    positions[object_id]["orientation_wxyz"] = list(quaternion)
                else:
                    missing_orientation.add(object_id)
            robot = row["robot"]
            fingers = robot.get("finger_positions", {})
            if not isinstance(fingers, dict) or any(not _vector(value, 3) for value in fingers.values()):
                raise DatasetError("invalid recorded finger positions")
            missing_fingers = missing_fingers or not all(side in fingers for side in ("left", "right"))
            frames.append({"step": row["simulation_step"], "timestamp": row["timestamp"],
                           "phase": robot["phase"], "objects": positions,
                           "robot": {"joint_positions": list(robot["joint_positions"]),
                                     "joint_velocities": list(robot["joint_velocities"]),
                                     "end_effector_pose": copy.deepcopy(robot["end_effector_pose"]),
                                     "finger_positions": copy.deepcopy(fingers)},
                           "contacts": _contacts(robot["grasp_diagnostics"], target),
                           "task_success": row["task_success"], "terminated": row["terminated"],
                           "truncated": row["truncated"], "failure_reason": robot.get("failure_reason")})
        notes = ["回放显示记录的物体位置、末端和双指轨迹及关节数值；本次未记录完整机械臂各连杆位姿。"]
        if missing_orientation:
            notes.append("部分物体未记录旋转，外观沿用场景初始朝向；回放不表示其真实旋转。")
        if missing_fingers:
            notes.append("部分帧缺少双指位置，对应标记隐藏。")
        return {"schema_version": "scene_factory.runtime_replay.v1", "source": "recorded_state",
                "case_id": root.name, "scene": layout, "assets": copy.deepcopy(dict(assets or {})),
                "frames": frames, "dt": settings.get("physics_dt", 1 / 60), "frame_count": len(frames),
                "duration_s": frames[-1]["timestamp"] - frames[0]["timestamp"],
                "task_status": assessment["status"], "evidence_complete": True,
                "trace_sha256": report["trace_sha256"], "notes": notes,
                "object_orientation_recorded": not missing_orientation,
                "robot_link_poses_recorded": False}
    except DatasetError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        raise DatasetError(f"invalid replay evidence: {exc}") from exc
