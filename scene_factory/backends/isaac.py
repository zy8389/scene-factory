from __future__ import annotations

import os
import sys
import math
from importlib.metadata import PackageNotFoundError, version as distribution_version
from importlib import import_module
from numbers import Integral
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ..exporters.isaac_usd import IsaacBackendUnavailable
from ..robotics import (
    MugLiftController,
    MugLiftPhase,
    Vec3,
    quaternion_angular_distance,
)
from ..tasks import TaskEvaluator


_BUNDLED_FRANKA_ARM_STIFFNESS = 1000.0
_BUNDLED_FRANKA_ARM_DAMPING = 200.0
_BUNDLED_FRANKA_ARM_MAX_FORCE = 2000.0
_FRANKA_FINGER_STIFFNESS = 1000.0
_FRANKA_FINGER_DAMPING = 200.0
_FRANKA_FINGER_MAX_FORCE = 2000.0
_BUNDLED_FRANKA_FINGER_ROOT_OFFSET_M = 0.0584
_NUCLEUS_FRANKA_APPROACH_CLEARANCE_X_M = 0.06
_NUCLEUS_FRANKA_GRASP_OFFSET_Z_M = -0.005
_GRASP_HOLD_CLOSING_MARGIN_M = 0.007


def _franka_kinematics_frame(asset_source: str | None) -> str:
    return (
        "panda_hand"
        if asset_source == "isaacsim_bundled_franka_urdf"
        else "right_gripper"
    )


def _load_simulation_app():
    try:
        module = import_module("isaacsim")
        return module.SimulationApp
    except (AttributeError, ImportError, ModuleNotFoundError) as exc:
        raise IsaacBackendUnavailable(
            "IsaacSimBackend requires the Isaac Sim Python environment. "
            "Run the command with Isaac Sim's python executable."
        ) from exc


def build_observation(
    *,
    instruction: str,
    object_positions: dict[str, Vec3],
    joint_positions: list[float],
    end_effector_position: Vec3,
    end_effector_orientation: tuple[float, float, float, float],
    phase: str,
    failure_reason: str | None,
    task_success: bool,
    simulation_step: int,
    orientation_error_rad: float | None = None,
    finger_positions: dict[str, Vec3] | None = None,
    finger_bounds: dict[str, dict[str, Vec3]] | None = None,
    grasp_diagnostics: dict[str, Any] | None = None,
    ik_target_joint_positions: list[float] | None = None,
    applied_joint_position_targets: list[float] | None = None,
    task_oracle: dict[str, Any] | None = None,
    pick_status: str = "not_run",
    place_status: str = "not_run",
    released: bool = False,
    joint_velocities: list[float] | None = None,
    action: dict[str, Any] | None = None,
    timestamp: float | None = None,
) -> dict[str, Any]:
    return {
        "language_instruction": instruction,
        "objects": {
            object_id: {"position": [float(value) for value in position]}
            for object_id, position in object_positions.items()
        },
        "robot": {
            "name": "franka",
            "joint_positions": [float(value) for value in joint_positions],
            "joint_velocities": [float(value) for value in (joint_velocities or [])],
            "end_effector_pose": {
                "position": [float(value) for value in end_effector_position],
                "orientation_wxyz": [float(value) for value in end_effector_orientation],
            },
            "finger_positions": {
                name: [float(value) for value in position]
                for name, position in (finger_positions or {}).items()
            },
            "finger_bounds": {
                name: {
                    edge: [float(value) for value in position]
                    for edge, position in bounds.items()
                }
                for name, bounds in (finger_bounds or {}).items()
            },
            "grasp_diagnostics": grasp_diagnostics or {},
            "task_oracle": task_oracle or {},
            "pick_status": pick_status,
            "place_status": place_status,
            "released": bool(released),
            "action": action,
            "ik_target_joint_positions": ik_target_joint_positions or [],
            "applied_joint_position_targets": applied_joint_position_targets or [],
            "phase": phase,
            "failure_reason": failure_reason,
            "orientation_error_rad": (
                float(orientation_error_rad) if orientation_error_rad is not None else None
            ),
        },
        "task_success": bool(task_success),
        "simulation_step": int(simulation_step),
        "timestamp": float(timestamp if timestamp is not None else simulation_step),
        "action": action,
    }


class IsaacSimBackend:
    """Isaac Sim 6.0 backend for the deterministic Franka mug-lift slice."""

    def __init__(
        self,
        usd_path: str | Path,
        *,
        headless: bool = True,
        max_steps: int = 720,
        physics_dt: float = 1.0 / 60.0,
        enable_rgbd: bool = False,
    ) -> None:
        self.usd_path = Path(usd_path).expanduser().resolve()
        self.headless = headless
        self.max_steps = max_steps
        self.physics_dt = physics_dt
        self.enable_rgbd = enable_rgbd
        self.scene: dict[str, Any] | None = None
        self.steps = 0
        self._app = None
        self._simulation = None
        self._stage = None
        self._robot = None
        self._gripper = None
        self._kinematics = None
        self._object_prims: dict[str, Any] = {}
        self._initial_positions: dict[str, Vec3] = {}
        self._task_evaluator: TaskEvaluator | None = None
        self._task_status: dict[str, Any] = {}
        self._controller: MugLiftController | None = None
        self._last_observation: dict[str, Any] | None = None
        self._grasp_diagnostics: dict[str, Any] = _empty_grasp_diagnostics()
        self._material_diagnostics: dict[str, Any] = {}
        self._finger_gripper_config: dict[str, Any] = {}
        self._robot_asset_source: str | None = None
        self._asset_root_diagnostics = _empty_asset_root_diagnostics()
        self._hand_root_path: str | None = None
        self._kinematics_frame = _franka_kinematics_frame(self._robot_asset_source)
        self._last_ik_target_joint_positions: list[float] | None = None
        self._last_applied_joint_position_targets: list[float] | None = None
        self._grasp_hold_positions: list[float] | None = None
        self._contact_subscription = None
        self._contact_interface = None
        self._contact_events: list[dict[str, Any]] = []
        self._pending_contact_events: list[dict[str, Any]] = []
        self._active_contact_pairs: dict[str, dict[str, Any]] = {}
        self._contact_views: list[tuple[str, Any]] = []
        self._contact_force_pairs: dict[str, dict[str, Any]] = {}
        self._contact_force_read_valid = False
        self._contact_event_count = 0
        self._finger_root_paths: tuple[str, str] = (
            "/World/Robot/panda_leftfinger",
            "/World/Robot/panda_rightfinger",
        )
        self._target_root_path: str | None = None
        self._last_command: dict[str, Any] | None = None
        self._camera = None
        self._camera_config: dict[str, Any] = {}
        self._last_camera_frame_marker: str | None = None

    @property
    def runtime_summary(self) -> dict[str, Any]:
        controller = self._controller
        return {
            "steps": self.steps,
            "ik": controller.ik_status if controller else "not_run",
            "grasp": controller.grasp_status if controller else "not_run",
            "phase": controller.phase.value if controller else "not_started",
            "failure_reason": controller.failure_reason if controller else None,
            "grasp_diagnostics": self._grasp_diagnostics,
            "task_oracle": self._task_status,
            "pick_status": controller.pick_status if controller else "not_run",
            "place_status": controller.place_status if controller else "not_run",
            "released": controller.released if controller else False,
            "robot_asset_source": self._robot_asset_source,
            "action": self._last_command,
            **self._asset_root_diagnostics,
        }

    @property
    def camera_config(self) -> dict[str, Any]:
        return dict(self._camera_config)

    @property
    def isaac_sim_version(self) -> str:
        try:
            module = import_module("isaacsim")
            version = getattr(module, "__version__", None)
            if version:
                return str(version)
        except (ImportError, ModuleNotFoundError):
            module = None
        try:
            version = distribution_version("isaacsim")
            if version:
                return str(version)
        except PackageNotFoundError:
            pass
        if module is not None:
            module_file = getattr(module, "__file__", None)
            if module_file:
                version_path = Path(module_file).with_name("VERSION")
                try:
                    version = version_path.read_text(encoding="utf-8").strip()
                except (OSError, UnicodeError):
                    pass
                else:
                    if version:
                        return version
        return "unknown"

    def reset(self, scene: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        self.close()
        if not self.usd_path.is_file():
            raise FileNotFoundError(self.usd_path)
        if sys.platform == "win32" and not _is_ascii(str(self.usd_path)):
            raise ValueError("Isaac Sim requires an ASCII-only root USD path on Windows")
        _validate_scene_payload(scene)
        self.scene = scene
        self.steps = 0
        os.environ.setdefault("OMNI_KIT_ACCEPT_EULA", "YES")
        try:
            SimulationApp = _load_simulation_app()
        except Exception:
            self.close()
            raise
        original_argv = sys.argv
        sys.argv = [sys.argv[0]]
        try:
            self._app = SimulationApp(
                {
                    "headless": self.headless,
                    "hide_ui": self.headless,
                    "renderer": "RayTracedLighting" if self.enable_rgbd else "Minimal",
                    "minimal_shading_mode": 4,
                    "anti_aliasing": 0,
                    "multi_gpu": False,
                    "max_gpu_count": 1,
                    "width": 640,
                    "height": 480,
                    "disable_viewport_updates": self.headless and not self.enable_rgbd,
                    "fast_shutdown": True,
                    "open_usd": self.usd_path.as_posix(),
                }
            )
        except (ImportError, OSError, RuntimeError) as exc:
            raise IsaacBackendUnavailable(f"Isaac Sim failed to start: {exc}") from exc
        finally:
            sys.argv = original_argv

        try:
            self._initialize_runtime()
            positions = self._read_object_positions()
            self._initial_positions = positions
            target_id = _target_object(scene)
            if target_id not in positions:
                raise RuntimeError(f"target object is not mapped in USD: {target_id}")
            self._task_evaluator = TaskEvaluator(scene["task"], positions)
            self._task_status = self._task_evaluator.status(
                positions,
                self._task_evidence(),
            )
            grasp_offset = scene["task"].get("grasp_offset_m", [0.0, 0.0, 0.0])
            if not isinstance(grasp_offset, (list, tuple)) or len(grasp_offset) != 3:
                raise ValueError("task.grasp_offset_m must contain exactly three values")
            grasp_offset = tuple(float(value) for value in grasp_offset)
            if self._robot_asset_source == "isaacsim_bundled_franka_urdf":
                # The recipe offset is calibrated for the Nucleus right_gripper
                # frame.  The bundled URDF uses panda_hand, whose origin is already
                # centered over the mug in X; retain only the lateral calibration
                # and move the hand up to put the finger links at mug height.
                grasp_offset = (
                    0.0,
                    grasp_offset[1],
                    grasp_offset[2] + _BUNDLED_FRANKA_FINGER_ROOT_OFFSET_M,
                )
            else:
                # The authored mug collision is offset from its wrapper pose.  Keep
                # the Nucleus right_gripper at the collision body's centerline; the
                # finger links then span the mug wall instead of contacting only its
                # rim during the lift.
                grasp_offset = (
                    0.0,
                    -0.007,
                    _NUCLEUS_FRANKA_GRASP_OFFSET_Z_M,
                )
            self._controller = MugLiftController(
                positions[target_id],
                self.max_steps,
                grasp_offset,
                approach_clearance_x_m=_NUCLEUS_FRANKA_APPROACH_CLEARANCE_X_M,
                task_mode=(
                    "pick_place"
                    if scene["task"].get("success", {}).get("predicate") == "pick_and_place"
                    else "lift"
                ),
                place_target_position=_pick_place_target_position(scene["task"]),
                transfer_clearance_m=_pick_place_transfer_clearance(scene["task"]),
                lift_height_m=_pick_place_lift_height(scene["task"]),
            )
            self._target_root_path = str(self._object_prims[target_id].GetPath())
            self._refresh_grasp_diagnostics()
            observation = self._observation(task_success=False)
            self._last_observation = observation
            return observation, {
                "scene_id": scene["scene_id"],
                "backend": "isaac",
                "robot": "franka",
                "usd": str(self.usd_path),
                "object_prims": {
                    object_id: str(prim.GetPath())
                    for object_id, prim in self._object_prims.items()
                },
            }
        except Exception:
            self.close()
            raise

    def step(
        self, action: Any
    ) -> tuple[dict[str, Any], float, bool, bool, dict[str, Any]]:
        if self.scene is None or self._controller is None or self._simulation is None:
            raise RuntimeError("reset must be called before step")
        if action not in (None, "scripted") and not (
            isinstance(action, dict) and action.get("mode") == "scripted"
        ):
            raise ValueError("IsaacSimBackend currently supports only the scripted mug-lift action")
        if self._controller.phase in {MugLiftPhase.DONE, MugLiftPhase.FAILED}:
            raise RuntimeError("episode has already ended; call reset before stepping again")

        target_id = _target_object(self.scene)
        before_positions = self._read_object_positions()
        command = self._controller.command(before_positions[target_id])
        ik_success = self._apply_command(command)
        self._last_command = _serialize_command(command)
        self._simulation.step(
            render=self._camera is not None or not self.headless,
            update_fabric=self._camera is not None,
        )
        self.steps += 1
        self._refresh_grasp_diagnostics()

        positions = self._read_object_positions()
        self._task_status = (
            self._task_evaluator.status(positions, self._task_evidence())
            if self._task_evaluator
            else {"task_success": False}
        )
        task_success = bool(self._task_status.get("task_success"))
        ee_position, ee_orientation = self._end_effector_pose()
        orientation_error = self._orientation_error(ee_orientation)
        self._controller.advance(
            target_position=positions[target_id],
            end_effector_position=tuple(float(value) for value in ee_position),
            orientation_error_rad=orientation_error,
            ik_success=ik_success,
            task_success=task_success,
            grasp_diagnostics=self._grasp_diagnostics,
            task_state=self._task_status,
        )
        if (
            command.phase == MugLiftPhase.GRASP
            and self._controller.phase == MugLiftPhase.VERIFY_GRASP
        ):
            self._capture_grasp_hold_positions()
        observation = self._observation(task_success=task_success, positions=positions)
        self._last_observation = observation
        timed_out = self._controller.failure_reason == "timeout"
        terminated = (
            self._controller.phase in {MugLiftPhase.DONE, MugLiftPhase.FAILED}
            and not timed_out
        )
        truncated = timed_out
        reward = 1.0 if self._controller.phase == MugLiftPhase.DONE else 0.0
        return observation, reward, terminated, truncated, self.runtime_summary

    def _capture_grasp_hold_positions(self) -> None:
        """Hold the in-contact finger pose instead of driving through the mug."""
        try:
            import numpy as np

            positions = np.asarray(self._robot.get_joint_positions(), dtype=float)
            indices = self._finger_gripper_config["indices"]
            lower = np.asarray(
                self._finger_gripper_config["closed_positions"], dtype=float
            )
            upper = np.asarray(
                self._finger_gripper_config["open_positions"], dtype=float
            )
            values = positions[np.asarray(indices, dtype=int)]
            if np.all(values >= lower - 1e-6) and np.all(values <= upper + 1e-6):
                values = np.maximum(lower, values - _GRASP_HOLD_CLOSING_MARGIN_M)
                self._grasp_hold_positions = [float(value) for value in values]
        except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):
            self._grasp_hold_positions = None

    def render(self) -> None:
        if self._app is None:
            raise RuntimeError("reset must be called before render")
        self._app.update()
        return None

    def close(self) -> None:
        simulation, app = self._simulation, self._app
        camera = self._camera
        self._simulation = None
        self._app = None
        self._camera = None
        if camera is not None:
            try:
                camera.destroy()
            except Exception:
                pass
        if self._contact_subscription is not None:
            try:
                if hasattr(self._contact_subscription, "unsubscribe"):
                    self._contact_subscription.unsubscribe()
                elif self._contact_interface is not None:
                    self._contact_interface.unsubscribe_physics_contact_report_events(
                        self._contact_subscription
                    )
            except Exception:
                pass
        if simulation is not None:
            try:
                simulation.stop()
            except Exception:
                pass
        if app is not None:
            try:
                app.close()
            except Exception:
                pass
        self.scene = None
        self.steps = 0
        self._stage = None
        self._robot = None
        self._gripper = None
        self._kinematics = None
        self._object_prims = {}
        self._initial_positions = {}
        self._task_evaluator = None
        self._task_status = {}
        self._controller = None
        self._last_observation = None
        self._grasp_diagnostics = _empty_grasp_diagnostics()
        self._material_diagnostics = {}
        self._finger_gripper_config = {}
        self._robot_asset_source = None
        self._asset_root_diagnostics = _empty_asset_root_diagnostics()
        self._hand_root_path = None
        self._kinematics_frame = _franka_kinematics_frame(self._robot_asset_source)
        self._last_ik_target_joint_positions = None
        self._last_applied_joint_position_targets = None
        self._grasp_hold_positions = None
        self._contact_subscription = None
        self._contact_interface = None
        self._contact_events = []
        self._pending_contact_events = []
        self._active_contact_pairs = {}
        self._contact_views = []
        self._contact_force_pairs = {}
        self._contact_force_read_valid = False
        self._contact_event_count = 0
        self._finger_root_paths = (
            "/World/Robot/panda_leftfinger",
            "/World/Robot/panda_rightfinger",
        )
        self._target_root_path = None
        self._last_command = None
        self._camera = None
        self._camera_config = {}
        self._last_camera_frame_marker = None

    def _initialize_runtime(self) -> None:
        try:
            import numpy as np
            import omni.usd
            from isaacsim.core.api import SimulationContext
            from isaacsim.core.prims import SingleArticulation
            from isaacsim.core.utils.stage import add_reference_to_stage
            from isaacsim.core.utils.types import ArticulationAction
            from isaacsim.robot.manipulators.grippers.parallel_gripper import ParallelGripper
            from isaacsim.robot_motion.motion_generation import (
                ArticulationKinematicsSolver,
                LulaKinematicsSolver,
                load_supported_lula_kinematics_solver_config,
            )
            from isaacsim.storage.native import get_assets_root_path
            from pxr import Gf, UsdGeom, UsdPhysics, UsdShade
        except (ImportError, ModuleNotFoundError) as exc:
            raise IsaacBackendUnavailable(
                "Isaac Sim core, manipulator, or motion-generation APIs are unavailable"
            ) from exc

        stage = omni.usd.get_context().get_stage()
        if stage is None:
            raise RuntimeError("Isaac Sim returned no active USD stage")
        required = ("/World", "/World/PhysicsScene", "/World/Objects")
        missing = [path for path in required if not stage.GetPrimAtPath(path).IsValid()]
        if missing:
            raise RuntimeError(f"SceneFactory USD is missing required prims: {missing}")
        self._stage = stage
        self._object_prims = self._map_object_prims(stage)
        target_id = _target_object(self.scene or {})
        if target_id not in self._object_prims:
            raise RuntimeError(f"target object is not mapped in USD: {target_id}")
        self._target_root_path = str(self._object_prims[target_id].GetPath())

        robot_prim_path = "/World/Robot"
        self._asset_root_diagnostics = _empty_asset_root_diagnostics()
        robot_usd, self._robot_asset_source = _resolve_franka_usd(
            get_assets_root_path,
            self.usd_path.parent,
            self._asset_root_diagnostics,
        )
        try:
            add_reference_to_stage(usd_path=robot_usd, prim_path=robot_prim_path)
            stage.Load(robot_prim_path)
            self._app.update()
            self._finger_root_paths, self._hand_root_path = _resolve_franka_link_paths(
                stage, robot_prim_path
            )
        except Exception as exc:
            self._asset_root_diagnostics["franka_usd_accessible"] = False
            self._asset_root_diagnostics["asset_root_error"] = _asset_error(
                exc, self._asset_root_diagnostics.get("franka_usd")
            )
            raise
        else:
            self._asset_root_diagnostics["franka_usd_accessible"] = True
        self._kinematics_frame = _franka_kinematics_frame(self._robot_asset_source)
        if self._robot_asset_source == "isaacsim_bundled_franka_urdf":
            _configure_bundled_franka_drives(stage, robot_prim_path, UsdPhysics)
        self._configure_gripper_material(
            stage,
            UsdPhysics,
            UsdShade,
            self._finger_root_paths,
        )
        base_position = self.scene.get("task", {}).get(
            "robot_base_position_m", [-0.05, 0.0, 0.78]
        )
        base_orientation = self.scene.get("task", {}).get(
            "robot_base_orientation_wxyz", [1.0, 0.0, 0.0, 0.0]
        )
        if self._robot_asset_source == "isaacsim_bundled_franka_urdf":
            # The imported URDF has a world fixed joint, so author its mount pose
            # before PhysX initialization rather than relying on a dynamic pose write.
            UsdGeom.XformCommonAPI(stage.GetPrimAtPath(robot_prim_path)).SetTranslate(
                Gf.Vec3d(*(float(value) for value in base_position))
            )
        self._robot = SingleArticulation(
            robot_prim_path,
            name="franka",
            position=np.asarray(base_position, dtype=float),
            orientation=np.asarray(base_orientation, dtype=float),
        )
        self._simulation = SimulationContext(
            physics_dt=self.physics_dt,
            rendering_dt=0.0 if self.enable_rgbd else self.physics_dt,
            stage_units_in_meters=1.0,
            physics_prim_path="/World/PhysicsScene",
            stage=stage,
        )
        physics_context = self._simulation.get_physics_context()
        physics_context.set_physx_update_transformations_settings(
            update_to_usd=True, update_velocities_to_usd=True
        )
        self._simulation.initialize_physics()
        self._robot.initialize()
        self._finger_gripper_config = _resolve_finger_gripper_config(self._robot)
        base_position = np.asarray(base_position, dtype=float)
        base_orientation = np.asarray(base_orientation, dtype=float)
        self._robot.set_world_pose(
            position=base_position,
            orientation=base_orientation,
        )
        default_joints = np.asarray(
            [0.0, -0.3, 0.0, -2.0, 0.0, 1.7, 0.8, 0.0, 0.0], dtype=float
        )
        for index, position in zip(
            self._finger_gripper_config["indices"],
            self._finger_gripper_config["open_positions"],
            strict=True,
        ):
            default_joints[index] = position
        self._robot.set_joint_positions(default_joints)
        self._robot.apply_action(ArticulationAction(joint_positions=default_joints))

        self._gripper = ParallelGripper(
            end_effector_prim_path=self._hand_root_path,
            joint_prim_names=["panda_finger_joint1", "panda_finger_joint2"],
            joint_opened_positions=np.asarray(
                self._finger_gripper_config["open_positions"], dtype=float
            ),
            joint_closed_positions=np.asarray(
                self._finger_gripper_config["closed_positions"], dtype=float
            ),
            action_deltas=np.asarray(
                self._finger_gripper_config["action_deltas"], dtype=float
            ),
        )
        self._gripper.initialize(
            articulation_apply_action_func=self._robot.apply_action,
            get_joint_positions_func=self._robot.get_joint_positions,
            set_joint_positions_func=self._robot.set_joint_positions,
            dof_names=self._robot.dof_names,
        )
        config = load_supported_lula_kinematics_solver_config("Franka")
        if not config:
            raise RuntimeError("Isaac Sim has no Lula kinematics config for Franka")
        lula = LulaKinematicsSolver(**config)
        self._kinematics = ArticulationKinematicsSolver(
            self._robot, lula, self._kinematics_frame
        )
        self._simulation.play()
        self._app.update()
        _configure_franka_runtime_drives(
            self._robot,
            self._finger_gripper_config["indices"],
        )
        self._robot.set_world_pose(position=base_position, orientation=base_orientation)
        self._robot.set_joint_positions(default_joints)
        self._robot.apply_action(ArticulationAction(joint_positions=default_joints))
        self._kinematics.get_kinematics_solver().set_robot_base_pose(
            base_position, base_orientation
        )
        self._initialize_contact_reporting(stage)
        self._initialize_rgbd_camera()
        for _ in range(10):
            self._simulation.step(render=self._camera is not None)
        actual_base_position, _ = self._robot.get_world_pose()
        if np.linalg.norm(actual_base_position - base_position) > 0.01:
            raise RuntimeError(
                "Franka base pose did not persist after physics initialization: "
                f"expected={base_position.tolist()}, actual={actual_base_position.tolist()}"
            )

        self._material_diagnostics = self._read_material_diagnostics()
        self._refresh_grasp_diagnostics()

    def _initialize_rgbd_camera(self) -> None:
        if not self.enable_rgbd:
            return
        config = _rgbd_camera_config((self.scene or {}).get("task", {}))
        if config is None:
            return
        try:
            import numpy as np
            from isaacsim.sensors.camera import Camera

            camera = Camera(
                prim_path=config["prim_path"],
                name=config["name"],
                frequency=config["frequency_hz"],
                resolution=tuple(config["resolution"]),
                position=np.asarray(config["position"], dtype=float),
                orientation=np.asarray(config["orientation_wxyz"], dtype=float),
            )
            camera.initialize(self._simulation.physics_sim_view)
            camera.add_distance_to_image_plane_to_frame()
        except (
            ImportError,
            ModuleNotFoundError,
            AttributeError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            raise IsaacBackendUnavailable(
                f"Isaac Sim RGB-D camera initialization failed: {type(exc).__name__}: {exc}"
            ) from exc
        self._camera = camera
        self._camera_config = config
        self._last_camera_frame_marker = None

    def capture_rgbd_observation(self) -> dict[str, Any]:
        """Capture RGB-D and calibration for the current backend simulation step."""
        if self._camera is None or self._app is None:
            raise RuntimeError("RGB-D camera is not configured; reset a sensor-enabled scene first")
        import numpy as np

        previous_marker = self._last_camera_frame_marker
        last_error = "camera returned no valid frame"
        for _ in range(12):
            try:
                self._run_rgbd_render_step()
            except Exception as exc:
                last_error = f"render graph step failed: {type(exc).__name__}: {exc}"
                continue
            reference_time = self._camera_reference_time()
            rgb = self._camera.get_rgb(device="cpu")
            depth = self._camera.get_depth(device="cpu")
            if rgb is None or depth is None:
                last_error = "camera RGB-D annotators returned no data"
                continue
            rgb = np.asarray(rgb).copy()
            depth = np.asarray(depth).copy()
            expected_width, expected_height = self._camera.get_resolution()
            if rgb.ndim != 3 or rgb.shape != (expected_height, expected_width, 3):
                last_error = f"invalid RGB shape: {tuple(rgb.shape)}"
                continue
            if depth.ndim != 2 or depth.shape != (expected_height, expected_width):
                last_error = f"invalid depth shape: {tuple(depth.shape)}"
                continue
            if reference_time is None:
                last_error = "camera reference time is unavailable"
                continue
            reference_error = self._camera_reference_time_error(reference_time, previous_marker)
            if reference_error is not None:
                last_error = reference_error
                continue
            finite_depth = np.isfinite(depth)
            if not bool(np.any(finite_depth)):
                last_error = (
                    "depth frame contains no finite values; "
                    f"rgb_min={float(np.min(rgb))}, rgb_max={float(np.max(rgb))}, "
                    f"depth_min={float(np.min(depth))}, depth_max={float(np.max(depth))}"
                )
                continue
            intrinsics = np.asarray(
                self._camera.get_intrinsics_matrix(device="cpu"), dtype=float
            )
            position, orientation = self._camera.get_world_pose(camera_axes="world")
            extrinsics = _camera_world_matrix(position, orientation)
            if not np.isfinite(intrinsics).all() or not np.isfinite(extrinsics).all():
                last_error = "camera calibration contains non-finite values"
                continue
            self._last_camera_frame_marker = repr(reference_time)
            return {
                "status": "ok",
                "simulation_step": int(self.steps),
                "rgb": rgb,
                "depth": depth,
                "intrinsics": intrinsics,
                "extrinsics": extrinsics,
                "rendering_frame": reference_time,
            }
        try:
            render_product = self._camera.get_render_product_path()
            frame_state = self._camera.get_current_frame(clone=True)
            world_pose = self._camera.get_world_pose(camera_axes="world")
            usd_pose = self._camera.get_world_pose(camera_axes="usd")
            diagnostics = (
                f"render_product={render_product!r}, "
                f"camera_paused={self._camera.is_paused()}, "
                f"frame_keys={sorted(frame_state)}, "
                f"rendering_frame={frame_state.get('rendering_frame')!r}, "
                f"reference_time={self._camera_reference_time()!r}, "
                f"world_pose={world_pose!r}, usd_pose={usd_pose!r}"
            )
        except Exception as exc:
            diagnostics = f"camera diagnostics unavailable: {type(exc).__name__}: {exc}"
        raise RuntimeError(
            f"RGB-D capture failed at simulation step {self.steps}: {last_error}; {diagnostics}"
        )

    def _run_rgbd_render_step(self) -> None:
        """Flush one camera render without advancing PhysX."""
        if self._app is None or self._simulation is None:
            raise RuntimeError("SimulationApp is not running")
        # Isaac's camera callback observes the previous post-render annotator
        # state, so two render-only updates flush RGB-D and ReferenceTime.
        for _ in range(2):
            self._simulation.render()

    def _camera_reference_time(self) -> dict[str, int] | None:
        """Read the render product time after its post-render annotators update."""
        if self._camera is None:
            return None
        annotator = getattr(self._camera, "_fabric_time_annotator", None)
        if annotator is not None:
            try:
                value = annotator.get_data()
            except Exception:
                value = None
            normalized = self._normalize_reference_time(value)
            if normalized is not None:
                return normalized
        frame = self._camera.get_current_frame(clone=True)
        value = frame.get("rendering_frame") if isinstance(frame, dict) else None
        return self._normalize_reference_time(value)

    def _camera_reference_time_error(
        self,
        reference_time: dict[str, int],
        previous_marker: str | None,
    ) -> str | None:
        marker = repr(reference_time)
        if previous_marker is not None and marker == previous_marker:
            return "camera frame did not advance after simulation step"
        reference_seconds = (
            reference_time["referenceTimeNumerator"]
            / reference_time["referenceTimeDenominator"]
        )
        simulation_time = self._simulation.current_time
        if not math.isclose(
            reference_seconds,
            simulation_time,
            rel_tol=0.0,
            abs_tol=max(1e-5, self.physics_dt * 0.1),
        ):
            return (
                "camera reference time does not match simulation time: "
                f"camera={reference_seconds}, simulation={simulation_time}"
            )
        return None

    @staticmethod
    def _normalize_reference_time(value: Any) -> dict[str, int] | None:
        if not isinstance(value, dict):
            return None
        numerator = value.get("referenceTimeNumerator")
        denominator = value.get("referenceTimeDenominator")
        if (
            isinstance(numerator, bool)
            or isinstance(denominator, bool)
            or not isinstance(numerator, Integral)
            or not isinstance(denominator, Integral)
            or denominator <= 0
        ):
            return None
        return {
            "referenceTimeNumerator": int(numerator),
            "referenceTimeDenominator": int(denominator),
        }

    @staticmethod
    def _configure_gripper_material(
        stage,
        UsdPhysics,
        UsdShade,
        finger_paths: tuple[str, str],
    ) -> None:
        material = UsdShade.Material.Define(
            stage, "/World/Robot/GripperPhysicsMaterial"
        )
        physics_material = UsdPhysics.MaterialAPI.Apply(material.GetPrim())
        physics_material.CreateStaticFrictionAttr().Set(2.0)
        physics_material.CreateDynamicFrictionAttr().Set(1.5)
        physics_material.CreateRestitutionAttr().Set(0.0)

        for path in finger_paths:
            prim = stage.GetPrimAtPath(path)
            if not prim.IsValid():
                raise RuntimeError(f"Franka finger prim is unavailable: {path}")
            binding = UsdShade.MaterialBindingAPI.Apply(prim)
            binding.Bind(
                material,
                UsdShade.Tokens.weakerThanDescendants,
                "physics",
            )

    def _apply_command(self, command) -> bool | None:
        import numpy as np

        if command.gripper == "open":
            self._gripper.open()
            from isaacsim.core.utils.types import ArticulationAction

            self._robot.apply_action(
                ArticulationAction(
                    joint_positions=np.asarray(
                        self._finger_gripper_config["open_positions"],
                        dtype=float,
                    ),
                    joint_velocities=(
                        np.asarray(
                            self._finger_gripper_config["action_deltas"],
                            dtype=float,
                        )
                        / self.physics_dt
                        * 0.5
                        if command.phase in {MugLiftPhase.RELEASE, MugLiftPhase.VERIFY_PLACE}
                        else None
                    ),
                    joint_indices=np.asarray(
                        self._finger_gripper_config["indices"],
                        dtype=int,
                    ),
                )
            )
        elif command.gripper in {"close", "closed"}:
            self._gripper.close()
            # Keep ParallelGripper as the command interface, then submit the
            # runtime lower limits explicitly so both Franka asset layouts close
            # instead of stopping after one incremental delta per frame.
            from isaacsim.core.utils.types import ArticulationAction

            finger_positions = self._grasp_hold_positions or self._finger_gripper_config[
                "closed_positions"
            ]
            self._robot.apply_action(
                ArticulationAction(
                    joint_positions=np.asarray(
                        finger_positions,
                        dtype=float,
                    ),
                    joint_indices=np.asarray(
                        self._finger_gripper_config["indices"],
                        dtype=int,
                    ),
                )
            )
        if not command.requires_ik or command.goal_position is None:
            self._last_applied_joint_position_targets = _read_applied_joint_position_targets(
                self._robot
            )
            return None
        base_position, base_orientation = self._robot.get_world_pose()
        self._kinematics.get_kinematics_solver().set_robot_base_pose(
            base_position, base_orientation
        )
        action, success = self._kinematics.compute_inverse_kinematics(
            np.asarray(command.goal_position, dtype=float),
            np.asarray(
                self.scene["task"].get(
                    "grasp_orientation_wxyz", [0.0, 1.0, 0.0, 0.0]
                ),
                dtype=float,
            ),
            position_tolerance=0.01,
            orientation_tolerance=0.1,
        )
        if success:
            self._last_ik_target_joint_positions = _action_joint_positions(action)
            self._robot.apply_action(action)
        self._last_applied_joint_position_targets = _read_applied_joint_position_targets(
            self._robot
        )
        return bool(success)

    def _initialize_contact_reporting(self, stage) -> None:
        """Subscribe to PhysX contact reports for the actual rigid-body roots."""
        try:
            from omni.physics.core import get_physics_simulation_interface
            from pxr import PhysxSchema
        except (ImportError, ModuleNotFoundError) as exc:
            self._grasp_diagnostics.update(
                {
                    "contact_report_available": False,
                    "contact_report_error": f"{type(exc).__name__}: {exc}",
                }
            )
            return

        target_id = _target_object(self.scene or {})
        target_prim = self._object_prims.get(target_id)
        roots = [stage.GetPrimAtPath(path) for path in self._finger_root_paths]
        if target_prim is not None:
            roots.append(target_prim)
        invalid = [str(prim.GetPath()) for prim in roots if not prim.IsValid()]
        if invalid:
            self._grasp_diagnostics.update(
                {
                    "contact_report_available": False,
                    "contact_report_error": f"missing contact report prims: {invalid}",
                }
            )
            return
        try:
            for prim in roots:
                api = PhysxSchema.PhysxContactReportAPI.Apply(prim)
                api.CreateThresholdAttr().Set(0.0)
            interface = get_physics_simulation_interface()
            self._contact_interface = interface
            self._contact_subscription = interface.subscribe_physics_contact_report_events(
                self._on_contact_report
            )
            from isaacsim.core.api.sensors import RigidContactView

            physics_sim_view = self._simulation.physics_sim_view
            self._contact_views = []
            for index, finger_path in enumerate(self._finger_root_paths):
                view = RigidContactView(
                    prim_paths_expr=finger_path,
                    filter_paths_expr=[self._target_root_path],
                    name=f"franka_finger_target_contact_{index}",
                    max_contact_count=32,
                )
                view.initialize(physics_sim_view)
                self._contact_views.append((finger_path, view))
        except (AttributeError, RuntimeError, TypeError) as exc:
            self._grasp_diagnostics.update(
                {
                    "contact_report_available": False,
                    "contact_report_error": f"{type(exc).__name__}: {exc}",
                }
            )
            return
        self._grasp_diagnostics.update(
            {
                "contact_report_available": True,
                "contact_report_subscribed": True,
            }
        )

    def _refresh_contact_force_pairs(self) -> None:
        self._contact_force_pairs = {}
        self._contact_force_read_valid = False
        if not self._contact_views or not self._target_root_path:
            return
        try:
            import numpy as np

            current: dict[str, dict[str, Any]] = {}
            for finger_path, view in self._contact_views:
                matrix = np.asarray(view.get_contact_force_matrix(dt=self.physics_dt))
                if matrix.ndim != 3 or matrix.shape[0] < 1 or matrix.shape[1] < 1:
                    raise RuntimeError("invalid Franka contact force matrix")
                force = np.asarray(matrix[0, 0], dtype=float)
                magnitude = float(np.linalg.norm(force))
                if magnitude <= 1e-6:
                    continue
                key = "|".join(sorted((finger_path, self._target_root_path)))
                current[key] = {
                    "collider0": finger_path,
                    "collider1": self._target_root_path,
                    "event_type": "CONTACT_FORCE",
                    "force": [float(value) for value in force],
                    "force_magnitude": magnitude,
                }
            self._contact_force_pairs = current
            self._contact_force_read_valid = True
        except (AttributeError, RuntimeError, TypeError, ValueError):
            self._contact_force_pairs = {}

    def _on_contact_report(self, *args) -> None:
        if not args:
            return
        headers = args[0]
        if not isinstance(headers, (list, tuple)):
            headers = (headers,)
        try:
            from pxr import PhysicsSchemaTools
        except (ImportError, ModuleNotFoundError):
            return
        for header in headers:
            try:
                collider0 = _contact_path(PhysicsSchemaTools, header.collider0)
                collider1 = _contact_path(PhysicsSchemaTools, header.collider1)
                event_type = getattr(header, "type", None)
                event_name = getattr(event_type, "name", str(event_type))
            except (AttributeError, RuntimeError, TypeError):
                continue
            pair = {
                "collider0": collider0,
                "collider1": collider1,
                "event_type": str(event_name),
            }
            self._contact_event_count += 1
            self._contact_events.append(pair)
            self._pending_contact_events.append(pair)
            if len(self._contact_events) > 128:
                del self._contact_events[:-128]
            if not self._is_finger_target_pair(collider0, collider1):
                continue
            key = "|".join(sorted((collider0, collider1)))
            if "LOST" in str(event_name).upper():
                self._active_contact_pairs.pop(key, None)
            else:
                self._active_contact_pairs[key] = pair

    def _is_finger_target_pair(self, first: str, second: str) -> bool:
        target = self._target_root_path
        if not target:
            return False
        first_finger = any(_is_descendant(first, root) for root in self._finger_root_paths)
        second_finger = any(_is_descendant(second, root) for root in self._finger_root_paths)
        return (first_finger and _is_descendant(second, target)) or (
            second_finger and _is_descendant(first, target)
        )

    def _refresh_grasp_diagnostics(self) -> None:
        positions = None
        try:
            positions = self._robot.get_joint_positions() if self._robot is not None else None
        except (AttributeError, RuntimeError):
            positions = None
        diagnostics = _read_finger_dof_diagnostics(self._robot, positions)
        diagnostics.update(self._material_diagnostics)
        self._refresh_contact_force_pairs()
        active_contact_pairs = dict(self._contact_force_pairs)
        diagnostics.update(
            {
                "contact_report_available": bool(
                    self._grasp_diagnostics.get("contact_report_available")
                ),
                "contact_report_subscribed": bool(
                    self._grasp_diagnostics.get("contact_report_subscribed")
                ),
                # Event reports are retained for diagnostics, but a begin event
                # can outlive the current physics contact when no LOST event is
                # emitted.  Acceptance must use the current force view instead.
                "finger_target_contact": bool(self._contact_force_pairs),
                "active_contact_pairs": list(active_contact_pairs.values()),
                "event_contact_pairs": list(self._active_contact_pairs.values()),
                "contact_force_pair_count": len(self._contact_force_pairs),
                "contact_force_pairs": list(self._contact_force_pairs.values()),
                "contact_force_read_valid": bool(self._contact_force_read_valid),
                "last_step_events": list(self._pending_contact_events),
                "contact_event_count": int(self._contact_event_count),
                "finger_gripper_config": self._finger_gripper_config,
            }
        )
        diagnostics["gripper_open"] = _finger_gripper_is_open(diagnostics)
        for key in ("contact_report_error", "dof_limits_error", "material_resolution_error"):
            if key in self._grasp_diagnostics:
                diagnostics[key] = self._grasp_diagnostics[key]
        self._pending_contact_events = []
        self._grasp_diagnostics = diagnostics

    def _observation(
        self,
        *,
        task_success: bool,
        positions: dict[str, Vec3] | None = None,
    ) -> dict[str, Any]:
        positions = positions or self._read_object_positions()
        joint_positions = self._robot.get_joint_positions()
        joint_velocities = self._robot.get_joint_velocities()
        ee_position, ee_orientation = self._end_effector_pose()
        orientation_error = self._orientation_error(ee_orientation)
        finger_paths = {
            "left": self._finger_root_paths[0],
            "right": self._finger_root_paths[1],
        }
        finger_positions = self._read_prim_positions(finger_paths)
        finger_bounds = self._read_prim_bounds(finger_paths)
        controller = self._controller
        return build_observation(
            instruction=str(self.scene.get("task", {}).get("instruction", "")),
            object_positions=positions,
            joint_positions=[float(value) for value in joint_positions],
            end_effector_position=tuple(float(value) for value in ee_position),
            end_effector_orientation=tuple(float(value) for value in ee_orientation),
            phase=controller.phase.value if controller else MugLiftPhase.PRE_GRASP.value,
            failure_reason=controller.failure_reason if controller else None,
            task_success=task_success,
            simulation_step=self.steps,
            orientation_error_rad=orientation_error,
            finger_positions=finger_positions,
            finger_bounds=finger_bounds,
            grasp_diagnostics=self._grasp_diagnostics,
            ik_target_joint_positions=self._last_ik_target_joint_positions,
            applied_joint_position_targets=self._last_applied_joint_position_targets,
            task_oracle=self._task_status,
            pick_status=controller.pick_status if controller else "not_run",
            place_status=controller.place_status if controller else "not_run",
            released=controller.released if controller else False,
            joint_velocities=[float(value) for value in joint_velocities],
            action=self._last_command,
            timestamp=self.steps * self.physics_dt,
        )

    def _task_evidence(self) -> dict[str, Any]:
        return dict(self._grasp_diagnostics)

    def _end_effector_pose(self):
        from isaacsim.core.utils.numpy.rotations import rot_matrices_to_quats

        position, rotation = self._kinematics.compute_end_effector_pose()
        return position, rot_matrices_to_quats(rotation)

    def _orientation_error(self, orientation) -> float:
        target = self.scene["task"].get(
            "grasp_orientation_wxyz", [0.0, 1.0, 0.0, 0.0]
        )
        return quaternion_angular_distance(
            tuple(float(value) for value in orientation),
            tuple(float(value) for value in target),
        )

    def _read_object_positions(self) -> dict[str, Vec3]:
        from pxr import Usd, UsdGeom

        result: dict[str, Vec3] = {}
        for object_id, prim in self._object_prims.items():
            matrix = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
            value = matrix.ExtractTranslation()
            result[object_id] = tuple(float(value[index]) for index in range(3))
        return result

    def _read_prim_positions(self, paths: dict[str, str]) -> dict[str, Vec3]:
        from pxr import Usd, UsdGeom

        result: dict[str, Vec3] = {}
        for name, path in paths.items():
            prim = self._stage.GetPrimAtPath(path)
            if not prim.IsValid():
                raise RuntimeError(f"Franka prim is missing: {path}")
            matrix = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
            value = matrix.ExtractTranslation()
            result[name] = tuple(float(value[index]) for index in range(3))
        return result

    def _read_prim_bounds(self, paths: dict[str, str]) -> dict[str, dict[str, Vec3]]:
        from pxr import Usd, UsdGeom

        cache = UsdGeom.BBoxCache(
            Usd.TimeCode.Default(),
            [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy],
        )
        result: dict[str, dict[str, Vec3]] = {}
        for name, path in paths.items():
            prim = self._stage.GetPrimAtPath(path)
            if not prim.IsValid():
                raise RuntimeError(f"Franka prim is missing: {path}")
            bounds = cache.ComputeWorldBound(prim).ComputeAlignedBox()
            result[name] = {
                "min": tuple(float(value) for value in bounds.GetMin()),
                "max": tuple(float(value) for value in bounds.GetMax()),
            }
        return result

    def _read_material_diagnostics(self) -> dict[str, Any]:
        if self._stage is None:
            return {
                "finger_materials": [],
                "target_materials": [],
                "finger_material_resolved": False,
                "target_material_resolution": False,
            }
        target_path = self._target_root_path
        finger_materials = [
            material
            for root in self._finger_root_paths
            for material in _resolve_materials_for_root(self._stage, root)
        ]
        target_materials = (
            _resolve_materials_for_root(self._stage, target_path)
            if target_path
            else []
        )
        return {
            "finger_materials": finger_materials,
            "target_materials": target_materials,
            "finger_material_resolved": bool(finger_materials)
            and _materials_resolved(finger_materials),
            "target_material_resolution": _materials_resolved(target_materials),
        }

    @staticmethod
    def _map_object_prims(stage) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for prim in stage.Traverse():
            attribute = prim.GetAttribute("sceneFactory:objectId")
            if not attribute.IsValid():
                continue
            object_id = attribute.Get()
            if object_id:
                if object_id in result:
                    raise RuntimeError(f"duplicate sceneFactory:objectId in USD: {object_id}")
                result[str(object_id)] = prim
        if not result:
            raise RuntimeError("SceneFactory USD contains no sceneFactory:objectId mappings")
        return result


def _resolve_franka_usd(
    get_assets_root_path,
    cache_parent: Path,
    diagnostics: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Resolve a real Franka asset without requiring a reachable Nucleus server."""
    diagnostics = diagnostics if diagnostics is not None else _empty_asset_root_diagnostics()
    diagnostics.update(_empty_asset_root_diagnostics())
    try:
        assets_root = get_assets_root_path()
    except Exception as exc:
        diagnostics["asset_root_resolution_status"] = "failed"
        diagnostics["asset_root_error"] = _asset_error(exc)
        assets_root = None
    if assets_root:
        asset_root = _safe_asset_value(assets_root)
        franka_usd = (
            str(assets_root).rstrip("/")
            + "/Isaac/Robots/FrankaRobotics/FrankaPanda/franka.usd"
        )
        diagnostics.update(
            {
                "asset_root_resolution_status": "resolved",
                "asset_root": asset_root,
                "franka_usd": _safe_asset_value(franka_usd),
                "asset_transport": _asset_transport(assets_root),
                "official_isaac_asset": True,
                "franka_usd_accessible": _is_local_asset_file(franka_usd),
                "robot_asset_source": "nucleus_franka_usd",
            }
        )
        return (
            franka_usd,
            "nucleus_franka_usd",
        )

    if diagnostics["asset_root_resolution_status"] == "not_attempted":
        diagnostics["asset_root_resolution_status"] = "empty"

    try:
        import isaacsim.asset.importer.urdf as urdf_module
        from isaacsim.asset.importer.urdf import URDFImporter, URDFImporterConfig
    except (ImportError, ModuleNotFoundError) as exc:
        raise RuntimeError(
            "Isaac assets root is unavailable and the bundled Franka URDF importer "
            "is not installed"
        ) from exc

    extension_root = Path(urdf_module.__file__).resolve().parents[4]
    urdf_path = (
        extension_root
        / "data"
        / "urdf"
        / "robots"
        / "franka_description"
        / "robots"
        / "panda_arm_hand.urdf"
    )
    if not urdf_path.is_file():
        raise RuntimeError(f"bundled Franka URDF is missing: {urdf_path}")

    output_root = cache_parent / ".scene_factory_franka_with_drives"
    output_path = output_root / "panda_arm_hand" / "panda_arm_hand.usda"
    if not output_path.is_file():
        output_root.mkdir(parents=True, exist_ok=True)
        config = URDFImporterConfig(
            urdf_path=str(urdf_path),
            usd_path=str(output_root),
            merge_fixed_joints=False,
            collision_from_visuals=False,
            fix_base=True,
            joint_drive_type="force",
            joint_target_type="position",
            override_joint_stiffness=400.0,
            override_joint_damping=80.0,
        )
        generated_path = Path(URDFImporter(config).import_urdf())
        if generated_path != output_path and generated_path.is_file():
            output_path = generated_path
    if not output_path.is_file():
        raise RuntimeError(f"bundled Franka URDF import did not produce USD: {output_path}")
    diagnostics.update(
        {
            "asset_root_resolution_status": "fallback_bundled",
            "franka_usd": str(output_path),
            "asset_transport": "local",
            "official_isaac_asset": False,
            "franka_usd_accessible": output_path.is_file(),
            "robot_asset_source": "isaacsim_bundled_franka_urdf",
        }
    )
    return str(output_path), "isaacsim_bundled_franka_urdf"


def _resolve_franka_link_paths(stage: Any, robot_root: str) -> tuple[tuple[str, str], str]:
    """Find Franka links by their authored names across USD asset layouts."""
    matches: dict[str, list[str]] = {
        "panda_leftfinger": [],
        "panda_rightfinger": [],
        "panda_hand": [],
    }
    for prim in stage.Traverse():
        path = str(prim.GetPath())
        if not _is_descendant(path, robot_root):
            continue
        name = str(prim.GetName())
        if name in matches:
            matches[name].append(path)

    missing = [name for name, paths in matches.items() if not paths]
    if missing:
        raise RuntimeError(f"Franka links are missing below {robot_root}: {missing}")
    ambiguous = {name: paths for name, paths in matches.items() if len(paths) > 1}
    if ambiguous:
        raise RuntimeError(f"Franka links are ambiguous below {robot_root}: {ambiguous}")
    return (
        (matches["panda_leftfinger"][0], matches["panda_rightfinger"][0]),
        matches["panda_hand"][0],
    )


def _configure_bundled_franka_drives(stage: Any, robot_root: str, UsdPhysics) -> None:
    """Give the bundled URDF arm usable position drives before PhysX starts."""
    matches: dict[str, list[Any]] = {f"panda_joint{index}": [] for index in range(1, 8)}
    for prim in stage.Traverse():
        path = str(prim.GetPath())
        if _is_descendant(path, robot_root) and str(prim.GetName()) in matches:
            matches[str(prim.GetName())].append(prim)

    missing = [name for name, prims in matches.items() if not prims]
    if missing:
        raise RuntimeError(f"bundled Franka arm joints are missing below {robot_root}: {missing}")
    ambiguous = {
        name: [str(prim.GetPath()) for prim in prims]
        for name, prims in matches.items()
        if len(prims) > 1
    }
    if ambiguous:
        raise RuntimeError(f"bundled Franka arm joints are ambiguous: {ambiguous}")

    for prims in matches.values():
        drive = UsdPhysics.DriveAPI.Get(prims[0], "angular")
        if not drive:
            drive = UsdPhysics.DriveAPI.Apply(prims[0], "angular")
        drive.GetStiffnessAttr().Set(_BUNDLED_FRANKA_ARM_STIFFNESS)
        drive.GetDampingAttr().Set(_BUNDLED_FRANKA_ARM_DAMPING)
        drive.GetMaxForceAttr().Set(_BUNDLED_FRANKA_ARM_MAX_FORCE)


def _configure_franka_runtime_drives(robot: Any, finger_indices: Any) -> None:
    """Update active Franka arm and finger PhysX gains after initialization."""
    import numpy as np

    view = getattr(robot, "_articulation_view", None)
    set_gains = getattr(view, "set_gains", None)
    set_max_efforts = getattr(view, "set_max_efforts", None)
    if not callable(set_gains) or not callable(set_max_efforts):
        raise RuntimeError("Isaac articulation view cannot configure Franka arm drives")
    arm_indices = np.arange(7, dtype=int)
    set_gains(
        kps=np.full((1, 7), _BUNDLED_FRANKA_ARM_STIFFNESS, dtype=float),
        kds=np.full((1, 7), _BUNDLED_FRANKA_ARM_DAMPING, dtype=float),
        joint_indices=arm_indices,
    )
    set_max_efforts(
        values=np.full((1, 7), _BUNDLED_FRANKA_ARM_MAX_FORCE, dtype=float),
        joint_indices=arm_indices,
    )
    finger_indices = np.asarray(finger_indices, dtype=int)
    if finger_indices.shape != (2,):
        raise RuntimeError("Franka runtime finger indices are invalid")
    set_gains(
        kps=np.full((1, 2), _FRANKA_FINGER_STIFFNESS, dtype=float),
        kds=np.full((1, 2), _FRANKA_FINGER_DAMPING, dtype=float),
        joint_indices=finger_indices,
    )
    set_max_efforts(
        values=np.full((1, 2), _FRANKA_FINGER_MAX_FORCE, dtype=float),
        joint_indices=finger_indices,
    )


def _action_joint_positions(action: Any) -> list[float] | None:
    try:
        import numpy as np

        values = np.asarray(action.joint_positions, dtype=float)
        if values.ndim != 1:
            return None
        return [float(value) for value in values]
    except (AttributeError, TypeError, ValueError):
        return None


def _read_applied_joint_position_targets(robot: Any) -> list[float] | None:
    try:
        import numpy as np

        view = getattr(robot, "_articulation_view")
        actions = view.get_applied_actions()
        values = np.asarray(actions.joint_positions, dtype=float)
        if values.ndim == 2:
            values = values[0]
        if values.ndim != 1:
            return None
        return [float(value) for value in values]
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return None


def _target_object(scene: dict[str, Any]) -> str:
    task = scene.get("task", {})
    target = task.get("target_object") or task.get("success", {}).get("subject")
    if not target:
        raise ValueError("scene task requires target_object or success.subject")
    return str(target)


def _pick_place_target_position(task: dict[str, Any]) -> Vec3 | None:
    success = task.get("success", {})
    if success.get("predicate") != "pick_and_place":
        return None
    target = success.get("target_position_m")
    if not isinstance(target, (list, tuple)) or len(target) != 3:
        raise ValueError("pick_and_place requires task.success.target_position_m")
    return tuple(float(value) for value in target)


def _pick_place_transfer_clearance(task: dict[str, Any]) -> float:
    success = task.get("success", {})
    if success.get("predicate") != "pick_and_place":
        return 0.2
    value = float(success.get("transfer_clearance_m", 0.0))
    if value <= 0.0:
        raise ValueError("pick_and_place requires a positive transfer_clearance_m")
    return value


def _pick_place_lift_height(task: dict[str, Any]) -> float | None:
    success = task.get("success", {})
    if success.get("predicate") != "pick_and_place":
        return None
    value = float(success.get("lift_height_m", 0.0))
    if value <= 0.0:
        raise ValueError("pick_and_place requires a positive lift_height_m")
    return value


def _rgbd_camera_config(task: dict[str, Any]) -> dict[str, Any] | None:
    raw = task.get("camera")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("task.camera must be an object")
    prim_path = str(raw.get("prim_path", ""))
    if not prim_path.startswith("/World/"):
        raise ValueError("task.camera.prim_path must be under /World")
    resolution = raw.get("resolution", [160, 120])
    if (
        not isinstance(resolution, (list, tuple))
        or len(resolution) != 2
        or any(int(value) <= 0 for value in resolution)
    ):
        raise ValueError("task.camera.resolution must contain two positive values")
    position = raw.get("position")
    orientation = raw.get("orientation_wxyz")
    if not isinstance(position, (list, tuple)) or len(position) != 3:
        raise ValueError("task.camera.position must contain three values")
    if not isinstance(orientation, (list, tuple)) or len(orientation) != 4:
        raise ValueError("task.camera.orientation_wxyz must contain four values")
    position = [float(value) for value in position]
    orientation = [float(value) for value in orientation]
    if not all(math.isfinite(value) for value in position + orientation):
        raise ValueError("task.camera pose must contain finite values")
    if math.isclose(sum(value * value for value in orientation), 0.0):
        raise ValueError("task.camera.orientation_wxyz must be non-zero")
    frequency = int(raw.get("frequency_hz", 60))
    if frequency <= 0:
        raise ValueError("task.camera.frequency_hz must be positive")
    return {
        "prim_path": prim_path,
        "name": str(raw.get("name", "scene_factory_rgbd")),
        "resolution": [int(value) for value in resolution],
        "position": position,
        "orientation_wxyz": orientation,
        "frequency_hz": frequency,
    }


def _serialize_command(command: Any) -> dict[str, Any]:
    return {
        "phase": command.phase.value if hasattr(command.phase, "value") else str(command.phase),
        "goal_position": (
            [float(value) for value in command.goal_position]
            if command.goal_position is not None
            else None
        ),
        "gripper": str(command.gripper),
        "requires_ik": bool(command.requires_ik),
    }


def _camera_world_matrix(position: Any, orientation_wxyz: Any) -> list[list[float]]:
    w, x, y, z = (float(value) for value in orientation_wxyz)
    norm = math.sqrt(w * w + x * x + y * y + z * z)
    if norm == 0.0:
        raise ValueError("camera orientation must have non-zero norm")
    w, x, y, z = (value / norm for value in (w, x, y, z))
    return [
        [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w), 2.0 * (x * z + y * w), float(position[0])],
        [2.0 * (x * y + z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - x * w), float(position[1])],
        [2.0 * (x * z - y * w), 2.0 * (y * z + x * w), 1.0 - 2.0 * (x * x + y * y), float(position[2])],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _validate_scene_payload(scene: dict[str, Any]) -> None:
    if not isinstance(scene, dict) or not scene.get("scene_id"):
        raise ValueError("backend reset requires a SceneFactory scene payload")
    if not isinstance(scene.get("objects"), (list, tuple)):
        raise ValueError("scene payload requires an objects array")
    _target_object(scene)


def _is_ascii(value: str) -> bool:
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def _empty_asset_root_diagnostics() -> dict[str, Any]:
    return {
        "asset_root_resolution_status": "not_attempted",
        "asset_root": None,
        "asset_root_error": None,
        "franka_usd": None,
        "franka_usd_accessible": False,
        "asset_transport": None,
        "official_isaac_asset": False,
        "robot_asset_source": None,
    }


def _safe_asset_value(value: Any) -> str:
    """Keep asset diagnostics useful without persisting URL credentials."""
    text = str(value)
    parsed = urlsplit(text)
    if not parsed.scheme or not parsed.netloc:
        return text
    return urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path.rstrip("/"), "", ""))


def _asset_transport(value: Any) -> str:
    parsed = urlsplit(str(value))
    if not parsed.netloc or len(parsed.scheme) == 1:
        return "local"
    return parsed.scheme


def _is_local_asset_file(value: Any) -> bool:
    text = str(value)
    return "://" not in text and Path(text).is_file()


def _asset_error(exc: BaseException, asset_value: Any | None = None) -> str:
    message = f"{type(exc).__name__}: {exc}"
    if asset_value is not None:
        message = message.replace(str(asset_value), _safe_asset_value(asset_value))
    return message


def _empty_grasp_diagnostics() -> dict[str, Any]:
    return {
        "dof_limits_available": False,
        "dof_limits_valid": False,
        "finger_dofs": [],
        "all_finger_positions_within_limits": False,
        "contact_report_available": False,
        "contact_report_subscribed": False,
        "contact_force_read_valid": False,
        "finger_target_contact": False,
        "gripper_open": False,
        "active_contact_pairs": [],
        "event_contact_pairs": [],
        "last_step_events": [],
        "contact_event_count": 0,
        "finger_materials": [],
        "target_materials": [],
        "finger_material_resolved": False,
        "target_material_resolution": False,
        "finger_gripper_config": {},
    }


def _read_finger_dof_diagnostics(robot: Any, positions: Any) -> dict[str, Any]:
    result = {
        "dof_limits_available": False,
        "dof_limits_valid": False,
        "finger_dofs": [],
        "all_finger_positions_within_limits": False,
    }
    if robot is None:
        return result
    try:
        names = [str(name) for name in robot.dof_names]
        limits = _read_runtime_dof_limits(robot)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return result
    try:
        raw_limits = limits.tolist() if hasattr(limits, "tolist") else limits
        while (
            isinstance(raw_limits, list)
            and len(raw_limits) == 1
            and isinstance(raw_limits[0], list)
        ):
            raw_limits = raw_limits[0]
        raw_positions = positions.tolist() if hasattr(positions, "tolist") else positions
        finger_names = ("panda_finger_joint1", "panda_finger_joint2")
        entries = []
        for name in finger_names:
            index = names.index(name)
            lower, upper = raw_limits[index]
            position = float(raw_positions[index])
            lower = float(lower)
            upper = float(upper)
            entries.append(
                {
                    "name": name,
                    "index": index,
                    "position": position,
                    "lower": lower,
                    "upper": upper,
                    "within_limits": lower - 1e-6 <= position <= upper + 1e-6,
                }
            )
        result.update(
            {
                "dof_limits_available": True,
                "dof_limits_valid": all(
                    entry["lower"] <= entry["upper"] for entry in entries
                ),
                "finger_dofs": entries,
                "all_finger_positions_within_limits": all(
                    entry["within_limits"] for entry in entries
                ),
            }
        )
    except (IndexError, KeyError, TypeError, ValueError, AttributeError):
        return result
    return result


def _finger_gripper_is_open(diagnostics: dict[str, Any]) -> bool:
    config = diagnostics.get("finger_gripper_config") or {}
    entries = diagnostics.get("finger_dofs") or []
    open_positions = config.get("open_positions")
    action_deltas = config.get("action_deltas")
    if (
        not isinstance(open_positions, (list, tuple))
        or not isinstance(action_deltas, (list, tuple))
        or len(entries) != 2
        or len(action_deltas) != 2
    ):
        return False
    try:
        return all(
            float(entry["position"])
            >= float(open_position) - max(2e-4, abs(float(action_delta)) / 8.0)
            for entry, open_position, action_delta in zip(
                entries, open_positions, action_deltas, strict=True
            )
        )
    except (KeyError, TypeError, ValueError):
        return False


def _read_runtime_dof_limits(robot: Any) -> Any:
    """Read limits from both legacy and Isaac Sim 6 articulation wrappers."""
    get_limits = getattr(robot, "get_dof_limits", None)
    if callable(get_limits):
        return get_limits()

    properties = getattr(robot, "dof_properties")
    names = getattr(getattr(properties, "dtype", None), "names", None)
    if names and "lower" in names and "upper" in names:
        return [[row["lower"], row["upper"]] for row in properties]

    view = getattr(robot, "_articulation_view", None)
    get_view_limits = getattr(view, "get_dof_limits", None)
    if callable(get_view_limits):
        return get_view_limits()
    raise AttributeError("articulation exposes no runtime DOF limit API")


def _resolve_finger_gripper_config(robot: Any) -> dict[str, Any]:
    """Build gripper commands from the articulation's runtime finger limits."""
    try:
        positions = robot.get_joint_positions()
    except (AttributeError, RuntimeError, TypeError):
        positions = None
    diagnostics = _read_finger_dof_diagnostics(robot, positions)
    if not (
        diagnostics["dof_limits_available"]
        and diagnostics["dof_limits_valid"]
        and len(diagnostics["finger_dofs"]) == 2
    ):
        raise RuntimeError(
            "Franka finger DOF limits are unavailable or invalid; "
            "cannot initialize ParallelGripper from runtime limits"
        )
    entries = diagnostics["finger_dofs"]
    open_positions = [float(entry["upper"]) for entry in entries]
    closed_positions = [float(entry["lower"]) for entry in entries]
    action_deltas = [
        max((upper - lower) * 0.1, 1e-6)
        for lower, upper in zip(closed_positions, open_positions, strict=True)
    ]
    return {
        "source": "runtime_dof_limits",
        "joint_names": [entry["name"] for entry in entries],
        "indices": [int(entry["index"]) for entry in entries],
        "open_positions": open_positions,
        "closed_positions": closed_positions,
        "action_deltas": action_deltas,
    }


def _resolve_materials_for_root(stage: Any, root_path: str | None) -> list[dict[str, Any]]:
    if not root_path:
        return []
    try:
        from pxr import UsdPhysics, UsdShade
    except (ImportError, ModuleNotFoundError):
        return []
    root = stage.GetPrimAtPath(root_path)
    if not root.IsValid():
        return []
    result = []
    seen_paths: set[str] = set()
    candidate_prims = [
        prim
        for prim in stage.Traverse()
        if _is_descendant(str(prim.GetPath()), root_path)
    ]
    for prim in candidate_prims:
        try:
            material_binding = UsdShade.MaterialBindingAPI(prim)
            try:
                physics_purpose = getattr(UsdShade.Tokens, "physics", "physics")
                bound = material_binding.ComputeBoundMaterial(physics_purpose)
            except TypeError:
                bound = material_binding.ComputeBoundMaterial()
            material = bound[0] if isinstance(bound, tuple) else bound
            if material is None or not material.GetPrim().IsValid():
                continue
            material_prim = material.GetPrim()
            material_path = str(material_prim.GetPath())
            if material_path in seen_paths:
                continue
            seen_paths.add(material_path)
            physics = UsdPhysics.MaterialAPI(material_prim)
            static = physics.GetStaticFrictionAttr().Get()
            dynamic = physics.GetDynamicFrictionAttr().Get()
            restitution = physics.GetRestitutionAttr().Get()
            result.append(
                {
                    "bound_prim": str(prim.GetPath()),
                    "material_path": material_path,
                    "static_friction": float(static) if static is not None else None,
                    "dynamic_friction": float(dynamic) if dynamic is not None else None,
                    "restitution": float(restitution) if restitution is not None else None,
                    "resolved": (
                        static is not None
                        and dynamic is not None
                        and restitution is not None
                    ),
                }
            )
        except (AttributeError, RuntimeError, TypeError, ValueError):
            continue
    return result


def _materials_resolved(materials: list[dict[str, Any]]) -> bool:
    """Require resolution for reported physics materials, excluding visual-only bindings."""
    physics_materials = [
        material
        for material in materials
        if any(
            material.get(field) is not None
            for field in ("static_friction", "dynamic_friction", "restitution")
        )
    ]
    return bool(physics_materials) and all(
        material.get("resolved") is True for material in physics_materials
    )


def _contact_path(physics_schema_tools: Any, value: Any) -> str:
    try:
        return str(physics_schema_tools.intToSdfPath(value))
    except (AttributeError, TypeError, ValueError):
        return str(value)


def _is_descendant(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")
