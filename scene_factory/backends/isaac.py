from __future__ import annotations

import os
import sys
from importlib import import_module
from pathlib import Path
from typing import Any

from ..exporters.isaac_usd import IsaacBackendUnavailable
from ..robotics import (
    MugLiftController,
    MugLiftPhase,
    Vec3,
    quaternion_angular_distance,
)
from ..tasks import TaskEvaluator


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
            "phase": phase,
            "failure_reason": failure_reason,
            "orientation_error_rad": (
                float(orientation_error_rad) if orientation_error_rad is not None else None
            ),
        },
        "task_success": bool(task_success),
        "simulation_step": int(simulation_step),
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
    ) -> None:
        self.usd_path = Path(usd_path).expanduser().resolve()
        self.headless = headless
        self.max_steps = max_steps
        self.physics_dt = physics_dt
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
        self._controller: MugLiftController | None = None
        self._last_observation: dict[str, Any] | None = None

    @property
    def runtime_summary(self) -> dict[str, Any]:
        controller = self._controller
        return {
            "steps": self.steps,
            "ik": controller.ik_status if controller else "not_run",
            "grasp": controller.grasp_status if controller else "not_run",
            "phase": controller.phase.value if controller else "not_started",
            "failure_reason": controller.failure_reason if controller else None,
        }

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
        SimulationApp = _load_simulation_app()
        original_argv = sys.argv
        sys.argv = [sys.argv[0]]
        try:
            self._app = SimulationApp(
                {
                    "headless": self.headless,
                    "hide_ui": self.headless,
                    "renderer": "Minimal",
                    "minimal_shading_mode": 4,
                    "anti_aliasing": 0,
                    "multi_gpu": False,
                    "max_gpu_count": 1,
                    "width": 640,
                    "height": 480,
                    "disable_viewport_updates": self.headless,
                    "fast_shutdown": True,
                    "open_usd": self.usd_path.as_posix(),
                }
            )
        except (ImportError, OSError, RuntimeError) as exc:
            raise IsaacBackendUnavailable(f"Isaac Sim failed to start: {exc}") from exc
        finally:
            sys.argv = original_argv

        self._initialize_runtime()
        positions = self._read_object_positions()
        self._initial_positions = positions
        target_id = _target_object(scene)
        if target_id not in positions:
            raise RuntimeError(f"target object is not mapped in USD: {target_id}")
        self._task_evaluator = TaskEvaluator(scene["task"], positions)
        grasp_offset = scene["task"].get("grasp_offset_m", [0.0, 0.0, 0.0])
        if not isinstance(grasp_offset, (list, tuple)) or len(grasp_offset) != 3:
            raise ValueError("task.grasp_offset_m must contain exactly three values")
        self._controller = MugLiftController(
            positions[target_id],
            self.max_steps,
            tuple(float(value) for value in grasp_offset),
        )
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
        self._simulation.step(render=not self.headless)
        self.steps += 1

        positions = self._read_object_positions()
        task_success = bool(self._task_evaluator and self._task_evaluator.evaluate(positions))
        ee_position, ee_orientation = self._end_effector_pose()
        orientation_error = self._orientation_error(ee_orientation)
        self._controller.advance(
            target_position=positions[target_id],
            end_effector_position=tuple(float(value) for value in ee_position),
            orientation_error_rad=orientation_error,
            ik_success=ik_success,
            task_success=task_success,
        )
        observation = self._observation(task_success=task_success, positions=positions)
        self._last_observation = observation
        timed_out = self._controller.failure_reason == "timeout"
        terminated = self._controller.phase in {MugLiftPhase.DONE, MugLiftPhase.FAILED} and not timed_out
        truncated = timed_out
        reward = 1.0 if task_success else 0.0
        return observation, reward, terminated, truncated, self.runtime_summary

    def render(self) -> None:
        if self._app is None:
            raise RuntimeError("reset must be called before render")
        self._app.update()
        return None

    def close(self) -> None:
        simulation, app = self._simulation, self._app
        self._simulation = None
        self._app = None
        if simulation is not None:
            try:
                simulation.stop()
            except (AttributeError, RuntimeError):
                pass
        if app is not None:
            app.close()
        self.scene = None
        self._stage = None
        self._robot = None
        self._gripper = None
        self._kinematics = None
        self._object_prims = {}
        self._task_evaluator = None
        self._controller = None

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
            from pxr import UsdPhysics, UsdShade
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

        assets_root = get_assets_root_path()
        if not assets_root:
            raise RuntimeError("Isaac Sim assets root is unavailable")
        robot_prim_path = "/World/Robot"
        robot_usd = assets_root + "/Isaac/Robots/FrankaRobotics/FrankaPanda/franka.usd"
        add_reference_to_stage(usd_path=robot_usd, prim_path=robot_prim_path)
        stage.Load(robot_prim_path)
        self._app.update()
        self._configure_gripper_material(stage, UsdPhysics, UsdShade)
        base_position = self.scene.get("task", {}).get(
            "robot_base_position_m", [-0.05, 0.0, 0.78]
        )
        base_orientation = self.scene.get("task", {}).get(
            "robot_base_orientation_wxyz", [1.0, 0.0, 0.0, 0.0]
        )
        self._robot = SingleArticulation(
            robot_prim_path,
            name="franka",
            position=np.asarray(base_position, dtype=float),
            orientation=np.asarray(base_orientation, dtype=float),
        )
        self._simulation = SimulationContext(
            physics_dt=self.physics_dt,
            rendering_dt=self.physics_dt,
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
        base_position = np.asarray(base_position, dtype=float)
        base_orientation = np.asarray(base_orientation, dtype=float)
        self._robot.set_world_pose(
            position=base_position,
            orientation=base_orientation,
        )
        default_joints = np.asarray(
            [0.0, -0.3, 0.0, -2.0, 0.0, 1.7, 0.8, 0.05, 0.05], dtype=float
        )
        self._robot.set_joint_positions(default_joints)
        self._robot.apply_action(ArticulationAction(joint_positions=default_joints))

        self._gripper = ParallelGripper(
            end_effector_prim_path=f"{robot_prim_path}/panda_rightfinger",
            joint_prim_names=["panda_finger_joint1", "panda_finger_joint2"],
            joint_opened_positions=np.asarray([0.05, 0.05], dtype=float),
            joint_closed_positions=np.asarray([0.0, 0.0], dtype=float),
            action_deltas=np.asarray([0.005, 0.005], dtype=float),
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
            self._robot, lula, "right_gripper"
        )
        self._simulation.play()
        self._robot.set_world_pose(position=base_position, orientation=base_orientation)
        self._robot.set_joint_positions(default_joints)
        self._robot.apply_action(ArticulationAction(joint_positions=default_joints))
        self._kinematics.get_kinematics_solver().set_robot_base_pose(
            base_position, base_orientation
        )
        for _ in range(10):
            self._simulation.step(render=False)
        actual_base_position, _ = self._robot.get_world_pose()
        if np.linalg.norm(actual_base_position - base_position) > 0.01:
            raise RuntimeError(
                "Franka base pose did not persist after physics initialization: "
                f"expected={base_position.tolist()}, actual={actual_base_position.tolist()}"
            )

    @staticmethod
    def _configure_gripper_material(stage, UsdPhysics, UsdShade) -> None:
        material = UsdShade.Material.Define(
            stage, "/World/Robot/GripperPhysicsMaterial"
        )
        physics_material = UsdPhysics.MaterialAPI.Apply(material.GetPrim())
        physics_material.CreateStaticFrictionAttr().Set(2.0)
        physics_material.CreateDynamicFrictionAttr().Set(1.5)
        physics_material.CreateRestitutionAttr().Set(0.0)

        finger_paths = (
            "/World/Robot/panda_leftfinger",
            "/World/Robot/panda_rightfinger",
        )
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
        elif command.gripper in {"close", "closed"}:
            self._gripper.close()
        if not command.requires_ik or command.goal_position is None:
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
            self._robot.apply_action(action)
        return bool(success)

    def _observation(
        self,
        *,
        task_success: bool,
        positions: dict[str, Vec3] | None = None,
    ) -> dict[str, Any]:
        positions = positions or self._read_object_positions()
        joint_positions = self._robot.get_joint_positions()
        ee_position, ee_orientation = self._end_effector_pose()
        orientation_error = self._orientation_error(ee_orientation)
        finger_positions = self._read_prim_positions(
            {
                "left": "/World/Robot/panda_leftfinger",
                "right": "/World/Robot/panda_rightfinger",
            }
        )
        finger_bounds = self._read_prim_bounds(
            {
                "left": "/World/Robot/panda_leftfinger",
                "right": "/World/Robot/panda_rightfinger",
            }
        )
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
        )

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


def _target_object(scene: dict[str, Any]) -> str:
    task = scene.get("task", {})
    target = task.get("target_object") or task.get("success", {}).get("subject")
    if not target:
        raise ValueError("scene task requires target_object or success.subject")
    return str(target)


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
