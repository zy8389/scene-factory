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
        self._validate_task()

    def evaluate(
        self,
        state: dict[str, tuple[float, float, float]],
        evidence: dict[str, Any] | None = None,
    ) -> bool:
        return bool(self.status(state, evidence).get("task_success"))

    def status(
        self,
        state: dict[str, tuple[float, float, float]],
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        success = self.task.get("success", {})
        predicate = success.get("predicate")
        if predicate == "pick_and_place":
            return self._pick_and_place_status(state, evidence or {})
        return {"task_success": self._evaluate_simple(success, state)}

    def _evaluate_simple(
        self,
        success: dict[str, Any],
        state: dict[str, tuple[float, float, float]],
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

    def _validate_task(self) -> None:
        success = self.task.get("success", {})
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
        )
        self._released = bool(gripper_open and not contact and contact_api_valid)
        if self._released and in_region:
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
