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
    VERIFY_GRASP = "VERIFY_GRASP"
    LIFT = "LIFT"
    TRANSFER = "TRANSFER"
    LOWER = "LOWER"
    RELEASE = "RELEASE"
    VERIFY_PLACE = "VERIFY_PLACE"
    DONE = "DONE"
    FAILED = "FAILED"


# The offline episode validator consumes the same transition contract as the
# runtime controller.  A phase may be repeated for multiple simulation steps,
# but it may only advance along one of these edges.
MUG_LIFT_PHASE_TRANSITIONS: dict[MugLiftPhase, tuple[MugLiftPhase, ...]] = {
    MugLiftPhase.PRE_GRASP: (MugLiftPhase.APPROACH,),
    MugLiftPhase.APPROACH: (MugLiftPhase.GRASP,),
    MugLiftPhase.GRASP: (MugLiftPhase.VERIFY_GRASP,),
    MugLiftPhase.VERIFY_GRASP: (MugLiftPhase.LIFT,),
    MugLiftPhase.LIFT: (MugLiftPhase.TRANSFER, MugLiftPhase.DONE),
    MugLiftPhase.TRANSFER: (MugLiftPhase.LOWER,),
    MugLiftPhase.LOWER: (MugLiftPhase.RELEASE,),
    MugLiftPhase.RELEASE: (MugLiftPhase.VERIFY_PLACE,),
    MugLiftPhase.VERIFY_PLACE: (MugLiftPhase.DONE,),
}


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
    _VERIFY_GRASP_STEPS = 60
    _VERIFY_GRASP_TARGET_DELTA_M = 0.05
    _VERIFY_GRASP_MIN_DELTA_M = 0.005
    _VERIFY_GRASP_MIN_CONTACT_STEPS = 10
    _MAX_LIFT_STEPS = 240
    _MAX_TRANSFER_STEPS = 240
    _TRANSFER_RAMP_STEPS = 90
    _MAX_LOWER_STEPS = 180
    _LOWER_RAMP_STEPS = 45
    _RELEASE_STEPS = 140
    _VERIFY_PLACE_STEPS = 90
    _MAX_CONSECUTIVE_IK_FAILURES = 30
    _ORIENTATION_TOLERANCE_RAD = 0.15

    def __init__(
        self,
        initial_target_position: Vec3,
        max_steps: int = 720,
        grasp_offset: Vec3 = (0.0, 0.0, 0.0),
        approach_clearance_x_m: float = 0.0,
        approach_clearance_y_m: float = 0.0,
        hold_at_approach_clearance: bool = False,
        task_mode: str = "lift",
        place_target_position: Vec3 | None = None,
        transfer_clearance_m: float = 0.2,
        lift_height_m: float | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self.initial_target_position = tuple(float(value) for value in initial_target_position)
        self.grasp_offset = tuple(float(value) for value in grasp_offset)
        self.approach_clearance_x_m = float(approach_clearance_x_m)
        self.approach_clearance_y_m = float(approach_clearance_y_m)
        self.hold_at_approach_clearance = bool(hold_at_approach_clearance)
        if task_mode not in {"lift", "pick_place"}:
            raise ValueError(f"unsupported task mode: {task_mode!r}")
        if task_mode == "pick_place" and place_target_position is None:
            raise ValueError("pick_place mode requires place_target_position")
        self.task_mode = task_mode
        self.place_target_position = (
            tuple(float(value) for value in place_target_position)
            if place_target_position is not None
            else None
        )
        self.transfer_clearance_m = float(transfer_clearance_m)
        if self.transfer_clearance_m <= 0.0:
            raise ValueError("transfer_clearance_m must be positive")
        self.lift_height_m = float(
            self.transfer_clearance_m if lift_height_m is None else lift_height_m
        )
        if self.lift_height_m <= 0.0:
            raise ValueError("lift_height_m must be positive")
        self.max_steps = max_steps
        self.phase = MugLiftPhase.PRE_GRASP
        self.total_steps = 0
        self.phase_steps = 0
        self.consecutive_ik_failures = 0
        self.ik_status = "not_run"
        self.grasp_status = "open"
        self.pick_status = "not_run"
        self.place_status = "not_run"
        self.released = False
        self.transfer_start_position: Vec3 | None = None
        self.failure_reason: str | None = None
        self.max_lift_delta_m = 0.0
        self.verify_contact_steps = 0
        self.verify_target_start_z: float | None = None

    def command(self, target_position: Vec3) -> MugLiftCommand:
        del target_position
        x, y, initial_z = (
            self.initial_target_position[index] + self.grasp_offset[index]
            for index in range(3)
        )
        approach_x = x + self.approach_clearance_x_m
        approach_y = y + self.approach_clearance_y_m
        grasp_x = approach_x if self.hold_at_approach_clearance else x
        grasp_y = approach_y if self.hold_at_approach_clearance else y
        if self.phase == MugLiftPhase.PRE_GRASP:
            return MugLiftCommand(
                self.phase, (approach_x, approach_y, initial_z + 0.14), "open", True
            )
        if self.phase == MugLiftPhase.APPROACH:
            return MugLiftCommand(
                self.phase, (approach_x, approach_y, initial_z), "open", True
            )
        if self.phase == MugLiftPhase.GRASP:
            if self.approach_clearance_x_m or self.approach_clearance_y_m:
                return MugLiftCommand(
                    self.phase, (grasp_x, grasp_y, initial_z), "close", True
                )
            return MugLiftCommand(self.phase, None, "close", False)
        if self.phase == MugLiftPhase.VERIFY_GRASP:
            return MugLiftCommand(
                self.phase,
                (grasp_x, grasp_y, initial_z + self._VERIFY_GRASP_TARGET_DELTA_M),
                "closed",
                True,
            )
        if self.phase == MugLiftPhase.LIFT:
            if self.task_mode == "pick_place":
                lift_height = min(
                    self.lift_height_m,
                    self._VERIFY_GRASP_TARGET_DELTA_M + min(
                        0.24, self.phase_steps * 0.002
                    ),
                )
            else:
                lift_height = self._VERIFY_GRASP_TARGET_DELTA_M + min(
                    0.24, self.phase_steps * 0.002
                )
            return MugLiftCommand(
                self.phase, (grasp_x, grasp_y, initial_z + lift_height), "closed", True
            )
        if self.phase == MugLiftPhase.TRANSFER:
            if self.place_target_position is None:
                raise RuntimeError("pick_place controller has no place target")
            source_x, source_y, source_z = self.transfer_start_position or tuple(
                self.initial_target_position[index] + self.grasp_offset[index]
                for index in range(3)
            )
            place_x, place_y, place_z = (
                self.place_target_position[index] + self.grasp_offset[index]
                for index in range(3)
            )
            progress = min(1.0, self.phase_steps / self._TRANSFER_RAMP_STEPS)
            return MugLiftCommand(
                self.phase,
                (
                    source_x + (place_x - source_x) * progress,
                    source_y + (place_y - source_y) * progress,
                    source_z,
                ),
                "closed",
                True,
            )
        if self.phase == MugLiftPhase.LOWER:
            if self.place_target_position is None:
                raise RuntimeError("pick_place controller has no place target")
            place_x, place_y, place_z = (
                self.place_target_position[index] + self.grasp_offset[index]
                for index in range(3)
            )
            progress = min(1.0, self.phase_steps / self._LOWER_RAMP_STEPS)
            start_z = (
                self.transfer_start_position[2]
                if self.transfer_start_position is not None
                else place_z + self.transfer_clearance_m
            )
            return MugLiftCommand(
                self.phase,
                (
                    place_x,
                    place_y,
                    place_z + (start_z - place_z) * (1.0 - progress),
                ),
                "closed",
                True,
            )
        if self.phase in {MugLiftPhase.RELEASE, MugLiftPhase.VERIFY_PLACE}:
            return MugLiftCommand(self.phase, None, "open", False)
        return MugLiftCommand(self.phase, None, "hold", False)

    def advance(
        self,
        *,
        target_position: Vec3,
        end_effector_position: Vec3,
        orientation_error_rad: float = 0.0,
        ik_success: bool | None,
        task_success: bool,
        grasp_diagnostics: dict[str, Any] | None = None,
        task_state: dict[str, Any] | None = None,
    ) -> None:
        if self.phase in {MugLiftPhase.DONE, MugLiftPhase.FAILED}:
            return

        self.total_steps += 1
        self.phase_steps += 1
        lift_delta = float(target_position[2]) - self.initial_target_position[2]
        self.max_lift_delta_m = max(self.max_lift_delta_m, lift_delta)

        if (
            self.task_mode == "lift"
            and self.phase == MugLiftPhase.LIFT
            and task_success
            and lift_delta >= 0.10
        ):
            self.phase = MugLiftPhase.DONE
            self.grasp_status = "passed"
            return
        if (
            self.task_mode == "pick_place"
            and self.phase == MugLiftPhase.VERIFY_PLACE
            and task_success
            and task_state
            and task_state.get("released") is True
        ):
            self.phase = MugLiftPhase.DONE
            self.place_status = "passed"
            self.released = True
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

        if (
            self.task_mode == "pick_place"
            and self.phase in {MugLiftPhase.TRANSFER, MugLiftPhase.LOWER}
            and task_state
            and task_state.get("pick_success") is True
            and task_state.get("holding") is not True
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
            self.grasp_status = "verifying"
            self._next_phase()
        elif self.phase == MugLiftPhase.VERIFY_GRASP:
            if self.verify_target_start_z is None:
                self.verify_target_start_z = float(target_position[2])
            if _grasp_diagnostics_unavailable(grasp_diagnostics):
                self.fail("grasp_diagnostics_unavailable")
                return
            diagnostics = grasp_diagnostics or {}
            if diagnostics.get("finger_target_contact"):
                self.verify_contact_steps += 1
            else:
                self.verify_contact_steps = 0
            verify_delta = float(target_position[2]) - self.verify_target_start_z
            if (
                self.phase_steps >= self._VERIFY_GRASP_STEPS
                and self.verify_contact_steps >= self._VERIFY_GRASP_MIN_CONTACT_STEPS
                and verify_delta >= self._VERIFY_GRASP_MIN_DELTA_M
            ):
                self.grasp_status = "passed"
                self._next_phase()
            elif self.phase_steps >= self._VERIFY_GRASP_STEPS:
                self.fail("grasp_failure")
        elif self.phase == MugLiftPhase.LIFT:
            if self.task_mode == "pick_place":
                if (
                    task_state
                    and task_state.get("pick_success") is True
                    and float(task_state.get("max_lift_delta_m", 0.0))
                    >= self.transfer_clearance_m
                ):
                    self.pick_status = "passed"
                    self.transfer_start_position = tuple(
                        float(value) for value in end_effector_position
                    )
                    self._next_phase()
                elif self.phase_steps >= self._MAX_LIFT_STEPS:
                    self.grasp_status = "failed"
                    self.fail("grasp_failure")
            elif self.phase_steps >= self._MAX_LIFT_STEPS:
                self.grasp_status = "failed"
                self.fail("grasp_failure")
        elif self.phase == MugLiftPhase.TRANSFER:
            if (
                self.phase_steps >= self._TRANSFER_RAMP_STEPS
                and self._horizontal_pose_reached(
                    end_effector_position, orientation_error_rad, command, 0.03
                )
            ):
                if self.phase_steps >= self._MIN_REACH_STEPS:
                    self._next_phase()
            elif self.phase_steps >= self._MAX_TRANSFER_STEPS:
                self.fail("transfer_failure")
        elif self.phase == MugLiftPhase.LOWER:
            in_target_region = bool(task_state and task_state.get("in_target_region"))
            if (
                self.phase_steps >= self._LOWER_RAMP_STEPS
                and in_target_region
                and self._pose_reached(end_effector_position, orientation_error_rad, command, 0.025)
            ):
                if self.phase_steps >= self._MIN_REACH_STEPS:
                    self._next_phase()
            elif self.phase_steps >= self._MAX_LOWER_STEPS:
                self.fail("place_failure")
        elif self.phase == MugLiftPhase.RELEASE:
            if self.phase_steps >= self._RELEASE_STEPS:
                if task_state and task_state.get("gripper_open") is True:
                    self._next_phase()
                else:
                    self.fail("release_failure")
        elif self.phase == MugLiftPhase.VERIFY_PLACE:
            if task_state:
                self.released = task_state.get("released") is True
            if self.phase_steps >= self._VERIFY_PLACE_STEPS:
                if not task_state or task_state.get("gripper_open") is not True:
                    self.fail("release_failure")
                else:
                    self.fail("place_failure")

    def fail(self, reason: str) -> None:
        self.phase = MugLiftPhase.FAILED
        self.failure_reason = reason
        if reason == "ik_failure":
            self.ik_status = "failed"
        if reason in {"grasp_failure", "grasp_diagnostics_unavailable", "object_lost"}:
            self.grasp_status = "failed"
        if self.task_mode == "pick_place":
            if reason in {"grasp_failure", "grasp_diagnostics_unavailable"}:
                self.pick_status = "failed"
            if reason in {"object_lost", "transfer_failure", "place_failure", "release_failure"}:
                self.place_status = "failed"

    def _next_phase(self) -> None:
        next_phases = MUG_LIFT_PHASE_TRANSITIONS[self.phase]
        if self.task_mode == "lift" and self.phase == MugLiftPhase.LIFT:
            next_phases = (MugLiftPhase.DONE,)
        self.phase = next_phases[0]
        self.phase_steps = 0

    @staticmethod
    def _pose_reached(
        end_effector_position: Vec3,
        orientation_error_rad: float,
        command: MugLiftCommand,
        position_tolerance: float,
    ) -> bool:
        return (
            command.goal_position is not None
            and math.dist(end_effector_position, command.goal_position) <= position_tolerance
            and orientation_error_rad <= MugLiftController._ORIENTATION_TOLERANCE_RAD
        )

    @staticmethod
    def _horizontal_pose_reached(
        end_effector_position: Vec3,
        orientation_error_rad: float,
        command: MugLiftCommand,
        position_tolerance: float,
    ) -> bool:
        return (
            command.goal_position is not None
            and math.dist(end_effector_position[:2], command.goal_position[:2])
            <= position_tolerance
            and orientation_error_rad <= MugLiftController._ORIENTATION_TOLERANCE_RAD
        )


def _grasp_diagnostics_unavailable(diagnostics: dict[str, Any] | None) -> bool:
    """Return true when the runtime cannot provide auditable grasp evidence."""
    if not isinstance(diagnostics, dict):
        return True
    required_flags = (
        "dof_limits_available",
        "dof_limits_valid",
        "all_finger_positions_within_limits",
        "contact_report_available",
        "contact_report_subscribed",
        "contact_force_read_valid",
        "finger_material_resolved",
        "target_material_resolution",
    )
    return any(diagnostics.get(flag) is not True for flag in required_flags)


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
    grasp_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    initial_position = _observation_position(initial_observation, target_object)
    final_position = _observation_position(final_observation, target_object)
    lift_delta = (
        final_position[2] - initial_position[2]
        if initial_position is not None and final_position is not None
        else 0.0
    )
    task_success = bool(final_observation and final_observation.get("task_success"))
    initial_ee = _robot_value(initial_observation, "end_effector_pose", "position")
    final_ee = _robot_value(final_observation, "end_effector_pose", "position")
    final_joints = _robot_value(final_observation, "joint_positions")
    final_diagnostics = grasp_diagnostics
    if final_diagnostics is None:
        final_diagnostics = _robot_value(final_observation, "grasp_diagnostics")
    phase = _robot_value(final_observation, "phase") or "not_started"
    phase_gate = phase == "DONE"
    passed = task_success and lift_delta >= 0.10 and phase_gate and grasp == "passed"
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
        "grasp_diagnostics": final_diagnostics or {},
        "task_success": task_success,
        "result": "passed" if passed else "failed",
        "failure_reason": None if passed else (failure_reason or "task_not_satisfied"),
    }


def build_pick_place_acceptance_report(
    *,
    scene_id: str,
    initial_observation: dict[str, Any] | None,
    final_observation: dict[str, Any] | None,
    steps: int,
    ik: str,
    pick: str,
    place: str,
    released: bool,
    failure_reason: str | None,
    target_object: str = "mug_1",
    asset_id: str = "mug_001",
    grasp_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    initial_position = _observation_position(initial_observation, target_object)
    final_position = _observation_position(final_observation, target_object)
    final_diagnostics = grasp_diagnostics
    if final_diagnostics is None:
        final_diagnostics = _robot_value(final_observation, "grasp_diagnostics")
    oracle = _robot_value(final_observation, "task_oracle") or {}
    phase = _robot_value(final_observation, "phase") or "not_started"
    task_success = bool(
        final_observation
        and final_observation.get("task_success") is True
        and oracle.get("task_success") is True
    )
    gripper_open = bool((final_diagnostics or {}).get("gripper_open"))
    current_contact = bool((final_diagnostics or {}).get("finger_target_contact"))
    released = bool(released and oracle.get("released") is True)
    passed = bool(
        task_success
        and phase == "DONE"
        and pick == "passed"
        and place == "passed"
        and released
        and gripper_open
        and not current_contact
    )
    return {
        "scene_id": scene_id,
        "backend": "isaac",
        "robot": "franka",
        "task": "pick_and_place",
        "target_object": target_object,
        "asset_id": asset_id,
        "initial_target_position": initial_position or [],
        "final_target_position": final_position or [],
        "max_lift_delta_m": float(oracle.get("max_lift_delta_m", 0.0)),
        "target_position_m": oracle.get("target_position_m", []),
        "target_support": oracle.get("target_support"),
        "placement_stable_steps": int(oracle.get("placement_stable_steps", 0)),
        "steps": int(steps),
        "ik": ik,
        "pick": pick,
        "place": place,
        "released": released,
        "gripper_open": gripper_open,
        "finger_target_contact": current_contact,
        "phase": phase,
        "task_oracle": oracle,
        "grasp_diagnostics": final_diagnostics or {},
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
