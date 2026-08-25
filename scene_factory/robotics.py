from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any


Vec3 = tuple[float, float, float]


class MugLiftPhase(str, Enum):
    PRE_GRASP = "PRE_GRASP"
    APPROACH = "APPROACH"
    GRASP = "GRASP"
    LIFT = "LIFT"
    DONE = "DONE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class MugLiftCommand:
    phase: MugLiftPhase
    goal_position: Vec3 | None
    gripper: str
    requires_ik: bool


class MugLiftController:
    """Deterministic, bounded Franka command state machine for one mug lift."""

    _MIN_REACH_STEPS = 20
    _MAX_REACH_STEPS = 180
    _GRASP_STEPS = 120
    _MAX_LIFT_STEPS = 240
    _MAX_CONSECUTIVE_IK_FAILURES = 30
    _ORIENTATION_TOLERANCE_RAD = 0.15

    def __init__(
        self,
        initial_target_position: Vec3,
        max_steps: int = 720,
        grasp_offset: Vec3 = (0.0, 0.0, 0.0),
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self.initial_target_position = tuple(float(value) for value in initial_target_position)
        self.grasp_offset = tuple(float(value) for value in grasp_offset)
        self.max_steps = max_steps
        self.phase = MugLiftPhase.PRE_GRASP
        self.total_steps = 0
        self.phase_steps = 0
        self.consecutive_ik_failures = 0
        self.ik_status = "not_run"
        self.grasp_status = "open"
        self.failure_reason: str | None = None
        self.max_lift_delta_m = 0.0

    def command(self, target_position: Vec3) -> MugLiftCommand:
        del target_position
        x, y, initial_z = (
            self.initial_target_position[index] + self.grasp_offset[index]
            for index in range(3)
        )
        if self.phase == MugLiftPhase.PRE_GRASP:
            return MugLiftCommand(self.phase, (x, y, initial_z + 0.14), "open", True)
        if self.phase == MugLiftPhase.APPROACH:
            return MugLiftCommand(self.phase, (x, y, initial_z), "open", True)
        if self.phase == MugLiftPhase.GRASP:
            return MugLiftCommand(self.phase, None, "close", False)
        if self.phase == MugLiftPhase.LIFT:
            lift_height = min(0.24, self.phase_steps * 0.002)
            return MugLiftCommand(
                self.phase, (x, y, initial_z + lift_height), "closed", True
            )
        return MugLiftCommand(self.phase, None, "hold", False)

    def advance(
        self,
        *,
        target_position: Vec3,
        end_effector_position: Vec3,
        orientation_error_rad: float = 0.0,
        ik_success: bool | None,
        task_success: bool,
    ) -> None:
        if self.phase in {MugLiftPhase.DONE, MugLiftPhase.FAILED}:
            return

        self.total_steps += 1
        self.phase_steps += 1
        lift_delta = float(target_position[2]) - self.initial_target_position[2]
        self.max_lift_delta_m = max(self.max_lift_delta_m, lift_delta)

        if task_success:
            self.phase = MugLiftPhase.DONE
            self.grasp_status = "passed"
            return
        if self.total_steps >= self.max_steps:
            self.fail("timeout")
            return

        command = self.command(target_position)
        if command.requires_ik:
            if ik_success:
                self.consecutive_ik_failures = 0
                self.ik_status = "passed"
            else:
                self.consecutive_ik_failures += 1
                self.ik_status = "failed"
                if self.consecutive_ik_failures >= self._MAX_CONSECUTIVE_IK_FAILURES:
                    self.fail("ik_failure")
                    return

        if (
            self.phase == MugLiftPhase.LIFT
            and self.max_lift_delta_m >= 0.03
            and lift_delta < 0.01
        ):
            self.fail("object_lost")
            return

        if self.phase in {MugLiftPhase.PRE_GRASP, MugLiftPhase.APPROACH}:
            if command.goal_position is None:
                raise RuntimeError("reach phase has no goal position")
            distance = math.dist(end_effector_position, command.goal_position)
            tolerance = 0.04 if self.phase == MugLiftPhase.PRE_GRASP else 0.025
            pose_reached = (
                distance <= tolerance
                and orientation_error_rad <= self._ORIENTATION_TOLERANCE_RAD
            )
            if self.phase_steps >= self._MIN_REACH_STEPS and pose_reached:
                self._next_phase()
            elif self.phase_steps >= self._MAX_REACH_STEPS:
                self.fail(f"{self.phase.value.lower()}_timeout")
        elif self.phase == MugLiftPhase.GRASP and self.phase_steps >= self._GRASP_STEPS:
            self.grasp_status = "closed"
            self._next_phase()
        elif self.phase == MugLiftPhase.LIFT and self.phase_steps >= self._MAX_LIFT_STEPS:
            self.grasp_status = "failed"
            self.fail("grasp_failure")

    def fail(self, reason: str) -> None:
        self.phase = MugLiftPhase.FAILED
        self.failure_reason = reason
        if reason == "ik_failure":
            self.ik_status = "failed"
        if reason in {"grasp_failure", "object_lost"}:
            self.grasp_status = "failed"

    def _next_phase(self) -> None:
        transitions = {
            MugLiftPhase.PRE_GRASP: MugLiftPhase.APPROACH,
            MugLiftPhase.APPROACH: MugLiftPhase.GRASP,
            MugLiftPhase.GRASP: MugLiftPhase.LIFT,
        }
        self.phase = transitions[self.phase]
        self.phase_steps = 0


def quaternion_angular_distance(
    first_wxyz: tuple[float, float, float, float],
    second_wxyz: tuple[float, float, float, float],
) -> float:
    """Return the shortest angular distance between two scalar-first quaternions."""
    first_norm = math.sqrt(sum(value * value for value in first_wxyz))
    second_norm = math.sqrt(sum(value * value for value in second_wxyz))
    if first_norm == 0.0 or second_norm == 0.0:
        raise ValueError("quaternions must have non-zero norm")
    dot = sum(
        first * second for first, second in zip(first_wxyz, second_wxyz, strict=True)
    ) / (first_norm * second_norm)
    return 2.0 * math.acos(min(1.0, abs(dot)))


def build_robot_acceptance_report(
    *,
    scene_id: str,
    initial_observation: dict[str, Any] | None,
    final_observation: dict[str, Any] | None,
    steps: int,
    ik: str,
    grasp: str,
    failure_reason: str | None,
    target_object: str = "mug_1",
    asset_id: str = "mug_001",
) -> dict[str, Any]:
    initial_position = _observation_position(initial_observation, target_object)
    final_position = _observation_position(final_observation, target_object)
    lift_delta = (
        final_position[2] - initial_position[2]
        if initial_position is not None and final_position is not None
        else 0.0
    )
    task_success = bool(final_observation and final_observation.get("task_success"))
    passed = task_success and lift_delta >= 0.10
    initial_ee = _robot_value(initial_observation, "end_effector_pose", "position")
    final_ee = _robot_value(final_observation, "end_effector_pose", "position")
    final_joints = _robot_value(final_observation, "joint_positions")
    phase = _robot_value(final_observation, "phase") or "not_started"
    return {
        "scene_id": scene_id,
        "backend": "isaac",
        "robot": "franka",
        "target_object": target_object,
        "asset_id": asset_id,
        "initial_target_position": initial_position or [],
        "final_target_position": final_position or [],
        "lift_delta_m": lift_delta,
        "steps": int(steps),
        "ik": ik,
        "grasp": grasp,
        "phase": phase,
        "initial_end_effector_position": initial_ee or [],
        "final_end_effector_position": final_ee or [],
        "final_joint_positions": final_joints or [],
        "task_success": task_success,
        "result": "passed" if passed else "failed",
        "failure_reason": None if passed else (failure_reason or "task_not_satisfied"),
    }


def _observation_position(
    observation: dict[str, Any] | None, object_id: str
) -> list[float] | None:
    if not observation:
        return None
    position = observation.get("objects", {}).get(object_id, {}).get("position")
    if not isinstance(position, (list, tuple)) or len(position) != 3:
        return None
    return [float(value) for value in position]


def _robot_value(observation: dict[str, Any] | None, *path: str) -> Any:
    value: Any = observation.get("robot", {}) if observation else {}
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value
