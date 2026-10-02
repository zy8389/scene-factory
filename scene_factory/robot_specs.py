"""Robot integration contracts, separate from static scene asset records.

Only Franka has a runtime adapter today. These types do not imply that an
additional robot, controller, hardware interface, or Isaac Lab task exists.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class RobotCapability(str, Enum):
    ARM = "arm"
    GRIPPER = "gripper"
    IK = "ik"
    PICK_PLACE = "pick_place"
    RGBD = "rgbd"


@dataclass(frozen=True)
class GripperSpec:
    joint_names: tuple[str, ...]
    finger_link_names: tuple[str, ...]


@dataclass(frozen=True)
class RobotSpec:
    name: str
    backend: str
    arm_joint_names: tuple[str, ...]
    gripper: GripperSpec | None
    capabilities: frozenset[RobotCapability]
    base_prim_path: str
    bundled_kinematics_frame: str
    nucleus_kinematics_frame: str
    default_joint_positions: tuple[float, ...]

    def kinematics_frame(self, asset_source: str | None) -> str:
        return (self.bundled_kinematics_frame
                if asset_source == "isaacsim_bundled_franka_urdf"
                else self.nucleus_kinematics_frame)


@dataclass(frozen=True)
class RobotObservation:
    joint_positions: tuple[float, ...]
    end_effector_position_m: tuple[float, float, float]
    gripper_state: str | None = None


@dataclass(frozen=True)
class RobotAction:
    target_position_m: tuple[float, float, float] | None = None
    gripper_command: str | None = None


@runtime_checkable
class RobotAdapter(Protocol):
    @property
    def spec(self) -> RobotSpec: ...

    def observe(self) -> RobotObservation: ...

    def apply(self, action: RobotAction) -> None: ...


FRANKA_SPEC = RobotSpec(
    name="franka",
    backend="isaac",
    arm_joint_names=tuple(f"panda_joint{index}" for index in range(1, 8)),
    gripper=GripperSpec(
        joint_names=("panda_finger_joint1", "panda_finger_joint2"),
        finger_link_names=("panda_leftfinger", "panda_rightfinger"),
    ),
    capabilities=frozenset({RobotCapability.ARM, RobotCapability.GRIPPER,
                            RobotCapability.IK, RobotCapability.PICK_PLACE,
                            RobotCapability.RGBD}),
    base_prim_path="/World/Robot",
    bundled_kinematics_frame="panda_hand",
    nucleus_kinematics_frame="right_gripper",
    default_joint_positions=(0.0, -0.3, 0.0, -2.0, 0.0, 1.7, 0.8, 0.0, 0.0),
)


def supported_robots() -> tuple[RobotSpec, ...]:
    """Return robots with an actual runtime integration, not planned models."""
    return (FRANKA_SPEC,)
