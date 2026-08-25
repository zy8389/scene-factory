from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scene_factory.agent import DryRunBackend, SimulatorBackend
from scene_factory.backends.isaac import (
    IsaacBackendUnavailable,
    IsaacSimBackend,
    _load_simulation_app,
    build_observation,
)
from scene_factory.factory import SceneFactory
from scene_factory.robotics import (
    MugLiftController,
    MugLiftPhase,
    build_robot_acceptance_report,
    quaternion_angular_distance,
)
from scene_factory.tasks import TaskEvaluator


class RobotRuntimeTests(unittest.TestCase):
    def test_simulator_backend_contract_and_lazy_isaac_import(self) -> None:
        self.assertIsInstance(DryRunBackend(), SimulatorBackend)
        backend = IsaacSimBackend("scene.usd")
        self.assertIsInstance(backend, SimulatorBackend)
        with patch(
            "scene_factory.backends.isaac.import_module",
            side_effect=ModuleNotFoundError("isaacsim"),
        ):
            with self.assertRaises(IsaacBackendUnavailable):
                _load_simulation_app()

    def test_observation_schema(self) -> None:
        observation = build_observation(
            instruction="把杯子拿起来。",
            object_positions={"mug_1": (0.5, 0.0, 0.9)},
            joint_positions=[0.0] * 9,
            end_effector_position=(0.4, 0.0, 1.0),
            end_effector_orientation=(1.0, 0.0, 0.0, 0.0),
            phase="PRE_GRASP",
            failure_reason=None,
            task_success=False,
            simulation_step=0,
        )
        self.assertEqual(observation["objects"]["mug_1"]["position"], [0.5, 0.0, 0.9])
        self.assertEqual(observation["robot"]["name"], "franka")
        self.assertEqual(len(observation["robot"]["joint_positions"]), 9)
        self.assertIsNone(observation["robot"]["orientation_error_rad"])
        self.assertFalse(observation["task_success"])

    def test_reach_phase_waits_for_orientation_convergence(self) -> None:
        initial = (0.5, 0.0, 0.9)
        controller = MugLiftController(initial)
        command = controller.command(initial)
        for _ in range(25):
            controller.advance(
                target_position=initial,
                end_effector_position=command.goal_position,
                orientation_error_rad=0.3,
                ik_success=True,
                task_success=False,
            )
        self.assertEqual(controller.phase, MugLiftPhase.PRE_GRASP)
        controller.advance(
            target_position=initial,
            end_effector_position=command.goal_position,
            orientation_error_rad=0.1,
            ik_success=True,
            task_success=False,
        )
        self.assertEqual(controller.phase, MugLiftPhase.APPROACH)

    def test_quaternion_distance_is_sign_invariant(self) -> None:
        self.assertAlmostEqual(
            quaternion_angular_distance((0.0, 1.0, 0.0, 0.0), (0.0, -1.0, 0.0, 0.0)),
            0.0,
        )
        self.assertAlmostEqual(
            quaternion_angular_distance(
                (1.0, 0.0, 0.0, 0.0),
                (0.0, 0.0, 0.0, 1.0),
            ),
            math.pi,
        )

    def test_controller_reaches_done_without_unbounded_loop(self) -> None:
        initial = (0.5, 0.0, 0.9)
        controller = MugLiftController(initial, max_steps=400)
        for expected_phase in (MugLiftPhase.PRE_GRASP, MugLiftPhase.APPROACH):
            self.assertEqual(controller.phase, expected_phase)
            for _ in range(20):
                command = controller.command(initial)
                controller.advance(
                    target_position=initial,
                    end_effector_position=command.goal_position,
                    ik_success=True,
                    task_success=False,
                )
        self.assertEqual(controller.phase, MugLiftPhase.GRASP)
        for _ in range(120):
            controller.advance(
                target_position=initial,
                end_effector_position=initial,
                ik_success=None,
                task_success=False,
            )
        self.assertEqual(controller.phase, MugLiftPhase.LIFT)
        controller.advance(
            target_position=(0.5, 0.0, 1.01),
            end_effector_position=(0.5, 0.0, 1.1),
            ik_success=True,
            task_success=True,
        )
        self.assertEqual(controller.phase, MugLiftPhase.DONE)

    def test_controller_does_not_chase_a_displaced_target(self) -> None:
        initial = (0.5, 0.0, 0.9)
        controller = MugLiftController(initial, grasp_offset=(0.053, 0.007, 0.0))
        displaced = (0.2, -0.3, 0.1)
        for phase, expected_z in (
            (MugLiftPhase.PRE_GRASP, 1.04),
            (MugLiftPhase.APPROACH, 0.9),
            (MugLiftPhase.LIFT, 1.14),
        ):
            controller.phase = phase
            controller.phase_steps = 120 if phase == MugLiftPhase.LIFT else 0
            goal = controller.command(displaced).goal_position
            self.assertIsNotNone(goal)
            self.assertEqual(goal[:2], (0.553, 0.007))
            self.assertAlmostEqual(goal[2], expected_z)

    def test_task_evaluator_and_acceptance_report_share_real_pose_delta(self) -> None:
        task = {
            "target_object": "mug_1",
            "success": {
                "predicate": "lifted",
                "subject": "mug_1",
                "min_height_delta_m": 0.1,
            },
        }
        evaluator = TaskEvaluator(task, {"mug_1": (0.5, 0.0, 0.9)})
        self.assertTrue(evaluator.evaluate({"mug_1": (0.5, 0.0, 1.01)}))
        initial = {"objects": {"mug_1": {"position": [0.5, 0.0, 0.9]}}}
        final = {
            "objects": {"mug_1": {"position": [0.5, 0.0, 1.01]}},
            "task_success": True,
        }
        report = build_robot_acceptance_report(
            scene_id="scene",
            initial_observation=initial,
            final_observation=final,
            steps=120,
            ik="passed",
            grasp="passed",
            failure_reason=None,
        )
        self.assertEqual(report["result"], "passed")
        self.assertAlmostEqual(report["lift_delta_m"], 0.11)
        self.assertTrue(
            {
                "scene_id",
                "backend",
                "robot",
                "target_object",
                "asset_id",
                "initial_target_position",
                "final_target_position",
                "lift_delta_m",
                "steps",
                "ik",
                "grasp",
                "phase",
                "initial_end_effector_position",
                "final_end_effector_position",
                "final_joint_positions",
                "task_success",
                "result",
                "failure_reason",
            }.issubset(report),
        )

    def test_acceptance_recipe_uses_ready_real_mug(self) -> None:
        factory = SceneFactory()
        result = factory.build_from_recipe("kitchen_franka_mug_lift", 77)
        self.assertTrue(result.valid, result.validation.to_dict())
        mug = next(item for item in result.scene.objects if item.object_id == "mug_1")
        self.assertEqual(mug.asset_id, "mug_001")
        self.assertEqual(factory.registry.get(mug.asset_id).status, "ready")
        self.assertIsNone(mug.fallback_reason)
        with tempfile.TemporaryDirectory() as directory:
            files = factory.write_result(result, directory)
            self.assertTrue(Path(files["layout"]).is_file())


if __name__ == "__main__":
    unittest.main()
