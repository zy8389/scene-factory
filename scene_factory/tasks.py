from __future__ import annotations

import math
from typing import Any


class TaskEvaluator:
    """Evaluate simple task predicates from simulator object poses."""

    def __init__(self, task: dict[str, Any], initial_state: dict[str, tuple[float, float, float]]) -> None:
        self.task = task
        self.initial_state = initial_state

    def evaluate(self, state: dict[str, tuple[float, float, float]]) -> bool:
        success = self.task.get("success", {})
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

