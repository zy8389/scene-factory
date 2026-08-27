from __future__ import annotations

import math
from typing import Any


class TaskEvaluator:
    """Evaluate task predicates from simulator poses and runtime evidence."""

    def __init__(self, task: dict[str, Any], initial_state: dict[str, tuple[float, float, float]]) -> None:
        self.task = task
        self.initial_state = initial_state
        self._max_lift_delta_m = 0.0
        self._picked = False
        self._released = False
        self._placement_stable_steps = 0
        self._last_target_position: tuple[float, float, float] | None = None
        self._last_target_step_distance_m = 0.0
        self._validate_task()

    def evaluate(
        self,
        state: dict[str, tuple[float, float, float]],
        evidence: dict[str, Any] | None = None,
    ) -> bool:
        return bool(self.status(state, evidence).get("task_success"))

    def status(
        self,
        state: dict[str, Any],
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        success = self.task.get("success", {})
        predicate = success.get("predicate")
        if predicate == "pick_and_place":
            return self._pick_and_place_status(state, evidence or {})
        if predicate == "articulation_state":
            return self._articulation_state_status(success, state, evidence or {})
        return {"task_success": self._evaluate_simple(success, state)}

    def _evaluate_simple(
        self,
        success: dict[str, Any],
        state: dict[str, Any],
    ) -> bool:
        predicate = success.get("predicate")
        if predicate == "lifted":
            object_id = self.task.get("target_object") or success.get("subject")
            if object_id not in state or object_id not in self.initial_state:
                return False
            delta = state[object_id][2] - self.initial_state[object_id][2]
            return delta >= float(success.get("min_height_delta_m", 0.1))

        subject = success.get("subject")
        target = success.get("target")
        if subject not in state or target not in state:
            return False
        subject_pose, target_pose = state[subject], state[target]
        xy_distance = math.hypot(subject_pose[0] - target_pose[0], subject_pose[1] - target_pose[1])
        if predicate == "near":
            return xy_distance <= float(success.get("max_distance_m", 0.15))
        if predicate == "on":
            return (
                xy_distance <= float(success.get("max_distance_m", 0.16))
                and subject_pose[2] > target_pose[2]
            )
        if predicate == "inside":
            radius = float(success.get("target_radius_m", 0.15))
            half_height = float(success.get("target_half_height_m", 0.15))
            return xy_distance <= radius and abs(subject_pose[2] - target_pose[2]) <= half_height
        raise ValueError(f"unsupported success predicate: {predicate!r}")

    def _articulation_state_status(
        self,
        success: dict[str, Any],
        state: dict[str, Any],
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        object_id, joint, state_name, state_range = self._articulation_target(success)
        current = self._current_joint_position(state, evidence, object_id, joint)
        passed = (
            current is not None
            and isinstance(state_range, (list, tuple))
            and len(state_range) == 2
            and float(state_range[0]) <= current <= float(state_range[1])
        )
        return {
            "task_success": bool(passed),
            "object_id": object_id,
            "joint": joint,
            "state": state_name,
            "current_position": current,
            "target_range": list(state_range) if isinstance(state_range, (list, tuple)) else None,
        }

    @staticmethod
    def _current_joint_position(
        state: dict[str, Any],
        evidence: dict[str, Any],
        object_id: Any,
        joint: Any,
    ) -> float | None:
        if not isinstance(object_id, str) or not isinstance(joint, str):
            return None
        articulation_positions = evidence.get("articulation_positions")
        candidates = []
        if isinstance(articulation_positions, dict):
            candidates.append(articulation_positions.get(object_id))
        candidates.append(state.get(object_id))
        for candidate in candidates:
            if isinstance(candidate, dict):
                joints = candidate.get("joints", candidate)
                if isinstance(joints, dict) and joint in joints:
                    value = joints[joint]
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        return None
                    value = float(value)
                    return value if math.isfinite(value) else None
                if isinstance(joints, list):
                    for item in joints:
                        if isinstance(item, dict) and item.get("joint_id") == joint:
                            value = item.get("position")
                            if isinstance(value, bool) or not isinstance(value, (int, float)):
                                return None
                            value = float(value)
                            return value if math.isfinite(value) else None
        return None

    def _articulation_target(
        self, success: dict[str, Any]
    ) -> tuple[Any, Any, Any, Any]:
        object_id = (
            success.get("object_id")
            or self.task.get("object_id")
            or self.task.get("target_object")
            or success.get("target_object")
            or success.get("subject")
        )
        state_name = success.get("state")
        joint = success.get("joint") or success.get("joint_id")
        state_range = success.get("range", success.get("state_range"))
        state_sources = (
            success.get("state_ranges"),
            success.get("states"),
            self.task.get("state_ranges"),
            self.task.get("states"),
        )
        initial_object = (
            self.initial_state.get(object_id) if isinstance(object_id, str) else None
        )
        if isinstance(initial_object, dict):
            state_sources += (
                initial_object.get("semantic_states"),
                initial_object.get("states"),
            )
        for state_specs in state_sources:
            selected = state_specs.get(state_name) if isinstance(state_specs, dict) else None
            if isinstance(selected, dict):
                joint = joint or selected.get("joint") or selected.get("joint_id")
                if state_range is None:
                    state_range = selected.get("range", selected.get("state_range"))
                if joint is not None and state_range is not None:
                    break
        return object_id, joint, state_name, state_range

    def _validate_task(self) -> None:
        success = self.task.get("success", {})
        if success.get("predicate") == "articulation_state":
            object_id, joint, state_name, state_range = self._articulation_target(success)
            if not isinstance(object_id, str) or not object_id.strip():
                raise ValueError("articulation_state requires a valid object_id")
            if not isinstance(state_name, str) or not state_name.strip():
                raise ValueError("articulation_state requires a state name")
            if not isinstance(joint, str) or not joint.strip():
                raise ValueError("articulation_state requires a joint")
            if (
                not isinstance(state_range, (list, tuple))
                or len(state_range) != 2
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    for value in state_range
                )
                or float(state_range[0]) > float(state_range[1])
            ):
                raise ValueError("articulation_state range must contain two finite ordered values")
            return
        if success.get("predicate") != "pick_and_place":
            return
        subject = self.task.get("target_object") or success.get("subject")
        if not subject or subject not in self.initial_state:
            raise ValueError("pick_and_place requires a valid target_object")
        target_support = success.get("target_support")
        if not target_support or target_support not in self.initial_state:
            raise ValueError("pick_and_place requires a valid target_support object")
        target_position = success.get("target_position_m")
        if not isinstance(target_position, (list, tuple)) or len(target_position) != 3:
            raise ValueError("pick_and_place requires target_position_m with three values")
        tolerance = success.get("target_tolerance_m", [0.08, 0.08, 0.03])
        if (
            not isinstance(tolerance, (list, tuple))
            or len(tolerance) != 3
            or any(float(value) <= 0.0 for value in tolerance)
        ):
            raise ValueError("pick_and_place target_tolerance_m must contain positive values")
        region = success.get("target_region_xy")
        if region is not None and (
            not isinstance(region, (list, tuple))
            or len(region) != 4
            or float(region[0]) >= float(region[1])
            or float(region[2]) >= float(region[3])
        ):
            raise ValueError("pick_and_place target_region_xy is invalid")
        if float(success.get("min_lift_delta_m", 0.1)) <= 0.0:
            raise ValueError("pick_and_place min_lift_delta_m must be positive")
        if int(success.get("settle_steps", 20)) < 1:
            raise ValueError("pick_and_place settle_steps must be positive")
        if float(success.get("max_settle_step_distance_m", 0.005)) <= 0.0:
            raise ValueError("pick_and_place max_settle_step_distance_m must be positive")

    def _pick_and_place_status(
        self,
        state: dict[str, tuple[float, float, float]],
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        success = self.task["success"]
        subject = self.task.get("target_object") or success["subject"]
        current = state.get(subject)
        initial = self.initial_state.get(subject)
        target_support = success["target_support"]
        target = success["target_position_m"]
        if current is None or initial is None or target_support not in state:
            return {
                "task_success": False,
                "pick_success": False,
                "in_target_region": False,
                "gripper_open": False,
                "holding": False,
                "released": False,
                "placement_stable": False,
                "placement_stable_steps": self._placement_stable_steps,
                "max_lift_delta_m": self._max_lift_delta_m,
            }

        current_position = tuple(float(value) for value in current)
        if self._last_target_position is None:
            self._last_target_step_distance_m = 0.0
        else:
            self._last_target_step_distance_m = math.dist(
                current_position, self._last_target_position
            )
        self._last_target_position = current_position
        lift_delta = float(current[2]) - float(initial[2])
        self._max_lift_delta_m = max(self._max_lift_delta_m, lift_delta)
        contact = evidence.get("finger_target_contact") is True
        gripper_open = evidence.get("gripper_open") is True
        holding = contact and not gripper_open
        min_lift = float(success.get("min_lift_delta_m", 0.1))
        if lift_delta >= min_lift and holding:
            self._picked = True

        tolerance = success.get("target_tolerance_m", [0.08, 0.08, 0.03])
        in_region = self._in_target_region(current, target, tolerance, success.get("target_region_xy"))
        contact_api_valid = (
            evidence.get("contact_report_available") is True
            and evidence.get("contact_report_subscribed") is True
            and evidence.get("contact_force_read_valid") is True
        )
        self._released = bool(gripper_open and not contact and contact_api_valid)
        max_settle_step_distance = float(
            success.get("max_settle_step_distance_m", 0.005)
        )
        placement_motion_stable = (
            self._last_target_step_distance_m <= max_settle_step_distance
        )
        if self._released and in_region and placement_motion_stable:
            self._placement_stable_steps += 1
        else:
            self._placement_stable_steps = 0
        settle_steps = int(success.get("settle_steps", 20))
        placement_stable = self._placement_stable_steps >= settle_steps
        return {
            "task_success": bool(self._picked and in_region and self._released and placement_stable),
            "pick_success": self._picked,
            "in_target_region": in_region,
            "gripper_open": gripper_open,
            "holding": holding,
            "released": self._released,
            "placement_stable": placement_stable,
            "placement_stable_steps": self._placement_stable_steps,
            "placement_motion_stable": placement_motion_stable,
            "placement_step_distance_m": self._last_target_step_distance_m,
            "max_settle_step_distance_m": max_settle_step_distance,
            "max_lift_delta_m": self._max_lift_delta_m,
            "target_support": target_support,
            "target_position_m": [float(value) for value in target],
        }

    @staticmethod
    def _in_target_region(
        current: tuple[float, float, float],
        target: list[float] | tuple[float, float, float],
        tolerance: list[float] | tuple[float, float, float],
        region: list[float] | tuple[float, float, float, float] | None,
    ) -> bool:
        x, y, z = (float(value) for value in current)
        target_x, target_y, target_z = (float(value) for value in target)
        tolerance_x, tolerance_y, tolerance_z = (float(value) for value in tolerance)
        in_xy = (
            float(region[0]) <= x <= float(region[1])
            and float(region[2]) <= y <= float(region[3])
            if region is not None
            else abs(x - target_x) <= tolerance_x and abs(y - target_y) <= tolerance_y
        )
        return in_xy and abs(z - target_z) <= tolerance_z
