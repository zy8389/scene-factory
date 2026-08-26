from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.run_franka_mug_lift import _invalidate_report_on_process_failure
from scene_factory.agent import DryRunBackend, SimulatorBackend
from scene_factory.backends.isaac import (
    IsaacBackendUnavailable,
    IsaacSimBackend,
    _load_simulation_app,
    _franka_kinematics_frame,
    _resolve_franka_usd,
    _resolve_finger_gripper_config,
    _finger_gripper_is_open,
    build_observation,
)
from scene_factory.factory import SceneFactory
from scene_factory.robotics import (
    MugLiftController,
    MugLiftPhase,
    build_pick_place_acceptance_report,
    build_robot_acceptance_report,
    quaternion_angular_distance,
)
from scene_factory.tasks import TaskEvaluator


class RobotRuntimeTests(unittest.TestCase):
    @staticmethod
    def _pick_place_task(settle_steps: int = 2) -> dict:
        return {
            "target_object": "mug_1",
            "success": {
                "predicate": "pick_and_place",
                "subject": "mug_1",
                "source_support": "island_1",
                "target_support": "island_1",
                "target_position_m": [0.78, 0.20, 0.9665],
                "target_region_xy": [0.70, 0.86, 0.10, 0.30],
                "target_tolerance_m": [0.09, 0.09, 0.03],
                "min_lift_delta_m": 0.10,
                "settle_steps": settle_steps,
                "max_settle_step_distance_m": 0.005,
            },
        }

    @staticmethod
    def _release_evidence(*, contact: bool, gripper_open: bool) -> dict:
        return {
            "finger_target_contact": contact,
            "gripper_open": gripper_open,
            "contact_report_available": True,
            "contact_report_subscribed": True,
            "contact_force_read_valid": True,
        }

    def test_gripper_commands_use_runtime_finger_limits(self) -> None:
        class FakeArticulation:
            dof_names = [
                "panda_joint1",
                "panda_joint2",
                "panda_joint3",
                "panda_joint4",
                "panda_joint5",
                "panda_joint6",
                "panda_joint7",
                "panda_finger_joint1",
                "panda_finger_joint2",
            ]

            @staticmethod
            def get_dof_limits():
                return [[-1.0, 1.0]] * 7 + [[0.0, 0.041], [0.0, 0.039]]

            @staticmethod
            def get_joint_positions():
                return [0.0] * 9

        config = _resolve_finger_gripper_config(FakeArticulation())
        self.assertEqual(config["source"], "runtime_dof_limits")
        self.assertEqual(config["indices"], [7, 8])
        self.assertEqual(config["open_positions"], [0.041, 0.039])
        self.assertEqual(config["closed_positions"], [0.0, 0.0])
        self.assertAlmostEqual(config["action_deltas"][0], 0.0041)
        self.assertAlmostEqual(config["action_deltas"][1], 0.0039)

    def test_gripper_commands_read_isaac6_dof_properties(self) -> None:
        class FakeDofProperties(list):
            dtype = type("Dtype", (), {"names": ("lower", "upper")})()

        class FakeArticulation:
            dof_names = [
                "panda_joint1",
                "panda_joint2",
                "panda_joint3",
                "panda_joint4",
                "panda_joint5",
                "panda_joint6",
                "panda_joint7",
                "panda_finger_joint1",
                "panda_finger_joint2",
            ]
            dof_properties = FakeDofProperties(
                [
                    {"lower": 0.0, "upper": 1.0},
                    {"lower": 0.0, "upper": 1.0},
                    {"lower": 0.0, "upper": 1.0},
                    {"lower": 0.0, "upper": 1.0},
                    {"lower": 0.0, "upper": 1.0},
                    {"lower": 0.0, "upper": 1.0},
                    {"lower": 0.0, "upper": 1.0},
                    {"lower": 0.0, "upper": 0.04},
                    {"lower": 0.0, "upper": 0.039},
                ]
            )

            @staticmethod
            def get_joint_positions():
                return [0.0] * 9

        config = _resolve_finger_gripper_config(FakeArticulation())
        self.assertEqual(config["source"], "runtime_dof_limits")
        self.assertEqual(config["open_positions"], [0.04, 0.039])
        self.assertEqual(config["closed_positions"], [0.0, 0.0])

    def test_gripper_open_uses_runtime_delta_tolerance(self) -> None:
        diagnostics = {
            "finger_dofs": [
                {"position": 0.0396},
                {"position": 0.0395},
            ],
            "finger_gripper_config": {
                "open_positions": [0.04, 0.04],
                "action_deltas": [0.004, 0.004],
            },
        }
        self.assertTrue(_finger_gripper_is_open(diagnostics))
        diagnostics["finger_dofs"][0]["position"] = 0.0394
        self.assertFalse(_finger_gripper_is_open(diagnostics))

    @staticmethod
    def _valid_grasp_diagnostics(contact: bool = True) -> dict:
        return {
            "dof_limits_available": True,
            "dof_limits_valid": True,
            "all_finger_positions_within_limits": True,
            "contact_report_available": True,
            "contact_report_subscribed": True,
            "contact_force_read_valid": True,
            "finger_material_resolved": True,
            "target_material_resolution": True,
            "finger_target_contact": contact,
        }

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

    def test_reset_failure_closes_started_simulation_app(self) -> None:
        class FakeApp:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            usd_path = Path(directory) / "scene.usd"
            usd_path.write_text("#usda 1.0\n", encoding="utf-8")
            app = FakeApp()
            backend = IsaacSimBackend(usd_path)
            with patch(
                "scene_factory.backends.isaac._load_simulation_app",
                return_value=lambda settings: app,
            ), patch.object(
                backend,
                "_initialize_runtime",
                side_effect=RuntimeError("initialization failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "initialization failed"):
                    backend.reset({
                        "scene_id": "scene",
                        "objects": [],
                        "task": {"target_object": "mug_1"},
                    })
            self.assertTrue(app.closed)
            self.assertIsNone(backend.scene)
            self.assertEqual(backend.steps, 0)
            self.assertIsNone(backend._last_observation)
            self.assertEqual(backend._initial_positions, {})

    def test_close_clears_episode_state(self) -> None:
        backend = IsaacSimBackend("scene.usd")
        backend.steps = 42
        backend.scene = {"scene_id": "scene"}
        backend._initial_positions = {"mug_1": (0.0, 0.0, 0.9)}
        backend._last_observation = {"task_success": False}
        backend.close()
        self.assertEqual(backend.steps, 0)
        self.assertIsNone(backend.scene)
        self.assertEqual(backend._initial_positions, {})
        self.assertIsNone(backend._last_observation)

    def test_asset_root_diagnostics_capture_official_local_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            franka = root / "Isaac" / "Robots" / "FrankaRobotics" / "FrankaPanda" / "franka.usd"
            franka.parent.mkdir(parents=True)
            franka.write_text("#usda 1.0\n", encoding="utf-8")
            diagnostics = {}

            usd_path, source = _resolve_franka_usd(lambda: str(root), root, diagnostics)

            self.assertEqual(source, "nucleus_franka_usd")
            self.assertEqual(Path(usd_path).resolve(), franka.resolve())
            self.assertEqual(diagnostics["asset_root_resolution_status"], "resolved")
            self.assertEqual(diagnostics["asset_root"], str(root))
            self.assertEqual(Path(diagnostics["franka_usd"]).resolve(), franka.resolve())
            self.assertEqual(diagnostics["asset_transport"], "local")
            self.assertTrue(diagnostics["official_isaac_asset"])
            self.assertTrue(diagnostics["franka_usd_accessible"])
            self.assertEqual(diagnostics["robot_asset_source"], "nucleus_franka_usd")

    def test_asset_root_diagnostics_capture_resolution_failure(self) -> None:
        diagnostics = {}

        with self.assertRaises(RuntimeError):
            _resolve_franka_usd(
                lambda: (_ for _ in ()).throw(RuntimeError("asset root unavailable")),
                Path("."),
                diagnostics,
            )

        self.assertEqual(diagnostics["asset_root_resolution_status"], "failed")
        self.assertIn("RuntimeError: asset root unavailable", diagnostics["asset_root_error"])
        self.assertIsNone(diagnostics["asset_root"])
        self.assertFalse(diagnostics["franka_usd_accessible"])

    def test_bundled_franka_uses_its_panda_hand_kinematics_frame(self) -> None:
        self.assertEqual(
            _franka_kinematics_frame("isaacsim_bundled_franka_urdf"),
            "panda_hand",
        )
        self.assertEqual(_franka_kinematics_frame("nucleus_franka_usd"), "right_gripper")

    def test_stale_contact_event_cannot_set_current_contact_flag(self) -> None:
        backend = IsaacSimBackend("scene.usd")
        backend._active_contact_pairs = {
            "stale": {
                "collider0": "/World/Robot/panda_leftfinger",
                "collider1": "/World/Objects/mug_1",
                "event_type": "CONTACT_FOUND",
            }
        }
        with patch.object(backend, "_refresh_contact_force_pairs"):
            backend._refresh_grasp_diagnostics()
        self.assertFalse(backend._grasp_diagnostics["finger_target_contact"])
        self.assertEqual(backend._grasp_diagnostics["active_contact_pairs"], [])
        self.assertEqual(len(backend._grasp_diagnostics["event_contact_pairs"]), 1)

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
        self.assertEqual(observation["robot"]["grasp_diagnostics"], {})

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
        self.assertEqual(controller.phase, MugLiftPhase.VERIFY_GRASP)
        for index in range(controller._VERIFY_GRASP_STEPS):
            controller.advance(
                target_position=(0.5, 0.0, 0.906 if index >= 20 else 0.9),
                end_effector_position=(0.5, 0.0, 0.91),
                ik_success=True,
                task_success=False,
                grasp_diagnostics=self._valid_grasp_diagnostics(),
            )
        self.assertEqual(controller.phase, MugLiftPhase.LIFT)
        controller.advance(
            target_position=(0.5, 0.0, 1.01),
            end_effector_position=(0.5, 0.0, 1.1),
            ik_success=True,
            task_success=True,
        )
        self.assertEqual(controller.phase, MugLiftPhase.DONE)

    def test_verify_grasp_without_contact_fails_before_lift(self) -> None:
        initial = (0.5, 0.0, 0.9)
        controller = MugLiftController(initial, max_steps=400)
        controller.phase = MugLiftPhase.VERIFY_GRASP
        for _ in range(controller._VERIFY_GRASP_STEPS):
            controller.advance(
                target_position=initial,
                end_effector_position=initial,
                ik_success=True,
                task_success=False,
                grasp_diagnostics=self._valid_grasp_diagnostics(contact=False),
            )
        self.assertEqual(controller.phase, MugLiftPhase.FAILED)
        self.assertEqual(controller.failure_reason, "grasp_failure")

    def test_verify_grasp_requires_auditable_diagnostics(self) -> None:
        controller = MugLiftController((0.5, 0.0, 0.9), max_steps=100)
        controller.phase = MugLiftPhase.VERIFY_GRASP
        controller.advance(
            target_position=(0.5, 0.0, 0.9),
            end_effector_position=(0.5, 0.0, 0.9),
            ik_success=True,
            task_success=False,
        )
        self.assertEqual(controller.phase, MugLiftPhase.FAILED)
        self.assertEqual(controller.failure_reason, "grasp_diagnostics_unavailable")

    def test_controller_does_not_chase_a_displaced_target(self) -> None:
        initial = (0.5, 0.0, 0.9)
        controller = MugLiftController(initial, grasp_offset=(0.053, 0.007, 0.0))
        displaced = (0.2, -0.3, 0.1)
        for phase, expected_z in (
            (MugLiftPhase.PRE_GRASP, 1.04),
            (MugLiftPhase.APPROACH, 0.9),
            (MugLiftPhase.LIFT, 1.19),
        ):
            controller.phase = phase
            controller.phase_steps = 120 if phase == MugLiftPhase.LIFT else 0
            goal = controller.command(displaced).goal_position
            self.assertIsNotNone(goal)
            expected_x = 0.553
            self.assertEqual(goal[:2], (expected_x, 0.007))
            self.assertAlmostEqual(goal[2], expected_z)

    def test_pick_place_oracle_requires_pick_target_release_and_stability(self) -> None:
        task = self._pick_place_task(settle_steps=2)
        evaluator = TaskEvaluator(
            task,
            {"mug_1": (0.56, 0.0, 0.9665), "island_1": (1.15, 0.0, 0.46)},
        )
        self.assertFalse(
            evaluator.evaluate(
                {"mug_1": (0.56, 0.0, 0.9665), "island_1": (1.15, 0.0, 0.46)},
                self._release_evidence(contact=False, gripper_open=True),
            )
        )
        lifted = {"mug_1": (0.56, 0.0, 1.08), "island_1": (1.15, 0.0, 0.46)}
        picked_status = evaluator.status(
            lifted,
            self._release_evidence(contact=True, gripper_open=False),
        )
        self.assertTrue(picked_status["pick_success"])
        target = {"mug_1": (0.78, 0.20, 0.9665), "island_1": (1.15, 0.0, 0.46)}
        self.assertFalse(
            evaluator.evaluate(target, self._release_evidence(contact=True, gripper_open=False))
        )
        self.assertFalse(
            evaluator.evaluate(target, self._release_evidence(contact=False, gripper_open=True))
        )
        self.assertTrue(
            evaluator.evaluate(target, self._release_evidence(contact=False, gripper_open=True))
        )
        final_status = evaluator.status(
            target,
            self._release_evidence(contact=False, gripper_open=True),
        )
        self.assertTrue(final_status["released"])
        self.assertTrue(final_status["placement_stable"])

    def test_pick_place_stability_rejects_fast_target_motion(self) -> None:
        evaluator = TaskEvaluator(
            self._pick_place_task(settle_steps=2),
            {"mug_1": (0.56, 0.0, 0.9665), "island_1": (1.15, 0.0, 0.46)},
        )
        evidence = self._release_evidence(contact=True, gripper_open=False)
        evaluator.status(
            {"mug_1": (0.56, 0.0, 1.08), "island_1": (1.15, 0.0, 0.46)},
            evidence,
        )
        status = evaluator.status(
            {"mug_1": (0.78, 0.20, 0.9665), "island_1": (1.15, 0.0, 0.46)},
            self._release_evidence(contact=False, gripper_open=True),
        )
        self.assertFalse(status["placement_motion_stable"])
        self.assertEqual(status["placement_stable_steps"], 0)
        self.assertFalse(status["task_success"])

    def test_pick_place_contact_read_failure_cannot_verify_release(self) -> None:
        evaluator = TaskEvaluator(
            self._pick_place_task(settle_steps=1),
            {"mug_1": (0.56, 0.0, 0.9665), "island_1": (1.15, 0.0, 0.46)},
        )
        evaluator.status(
            {"mug_1": (0.56, 0.0, 1.08), "island_1": (1.15, 0.0, 0.46)},
            self._release_evidence(contact=True, gripper_open=False),
        )
        evidence = self._release_evidence(contact=False, gripper_open=True)
        evidence["contact_force_read_valid"] = False
        status = evaluator.status(
            {"mug_1": (0.78, 0.20, 0.9665), "island_1": (1.15, 0.0, 0.46)},
            evidence,
        )
        self.assertFalse(status["released"])
        self.assertFalse(status["task_success"])

    def test_pick_place_controller_transitions_through_release(self) -> None:
        controller = MugLiftController(
            (0.56, 0.0, 0.9665),
            max_steps=500,
            place_target_position=(0.78, 0.20, 0.9665),
            task_mode="pick_place",
            transfer_clearance_m=0.20,
        )
        controller.phase = MugLiftPhase.LIFT
        lift_command = controller.command((0.56, 0.0, 1.08))
        controller.advance(
            target_position=(0.56, 0.0, 1.08),
            end_effector_position=lift_command.goal_position,
            ik_success=True,
            task_success=False,
            task_state={"pick_success": True, "holding": True, "max_lift_delta_m": 0.20},
        )
        self.assertEqual(controller.phase, MugLiftPhase.TRANSFER)

        controller.phase_steps = controller._TRANSFER_RAMP_STEPS
        transfer_command = controller.command((0.56, 0.0, 1.08))
        controller.advance(
            target_position=(0.56, 0.0, 1.08),
            end_effector_position=transfer_command.goal_position,
            ik_success=True,
            task_success=False,
            task_state={"pick_success": True, "holding": True},
        )
        self.assertEqual(controller.phase, MugLiftPhase.LOWER)

        controller.phase_steps = controller._LOWER_RAMP_STEPS
        lower_command = controller.command((0.78, 0.20, 0.9665))
        controller.advance(
            target_position=(0.78, 0.20, 0.9665),
            end_effector_position=lower_command.goal_position,
            ik_success=True,
            task_success=False,
            task_state={"pick_success": True, "holding": True, "in_target_region": True},
        )
        self.assertEqual(controller.phase, MugLiftPhase.RELEASE)

        controller.phase_steps = controller._RELEASE_STEPS
        controller.advance(
            target_position=(0.78, 0.20, 0.9665),
            end_effector_position=(0.78, 0.20, 1.15),
            ik_success=None,
            task_success=False,
            task_state={"gripper_open": True, "released": False},
        )
        self.assertEqual(controller.phase, MugLiftPhase.VERIFY_PLACE)

        controller.advance(
            target_position=(0.78, 0.20, 0.9665),
            end_effector_position=(0.78, 0.20, 1.15),
            ik_success=None,
            task_success=True,
            task_state={"gripper_open": True, "released": True},
        )
        self.assertEqual(controller.phase, MugLiftPhase.DONE)

    def test_pick_place_invalid_target_and_failed_placement_fail_closed(self) -> None:
        invalid = self._pick_place_task()
        invalid["success"].pop("target_position_m")
        with self.assertRaises(ValueError):
            TaskEvaluator(invalid, {"mug_1": (0.0, 0.0, 0.9)})

        controller = MugLiftController(
            (0.56, 0.0, 0.9665),
            max_steps=200,
            place_target_position=(0.78, 0.20, 0.9665),
            task_mode="pick_place",
        )
        controller.phase = MugLiftPhase.VERIFY_PLACE
        controller.phase_steps = controller._VERIFY_PLACE_STEPS - 1
        controller.advance(
            target_position=(0.56, 0.0, 0.9665),
            end_effector_position=(0.56, 0.0, 1.1),
            ik_success=None,
            task_success=False,
            task_state={"gripper_open": True, "released": False},
        )
        self.assertEqual(controller.phase, MugLiftPhase.FAILED)
        self.assertEqual(controller.failure_reason, "place_failure")

    def test_pick_place_report_requires_oracle_success_even_when_phase_is_done(self) -> None:
        report = build_pick_place_acceptance_report(
            scene_id="scene",
            initial_observation={"objects": {"mug_1": {"position": [0.56, 0.0, 0.9665]}}},
            final_observation={
                "objects": {"mug_1": {"position": [0.78, 0.20, 0.9665]}},
                "task_success": True,
                "robot": {
                    "phase": "DONE",
                    "pick_status": "passed",
                    "place_status": "passed",
                    "released": True,
                    "task_oracle": {"task_success": False},
                    "grasp_diagnostics": {
                        "gripper_open": True,
                        "finger_target_contact": False,
                    },
                },
            },
            steps=200,
            ik="passed",
            pick="passed",
            place="passed",
            released=True,
            failure_reason=None,
        )
        self.assertEqual(report["result"], "failed")

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
            "robot": {"phase": "DONE"},
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
                "grasp_diagnostics",
                "task_success",
                "result",
                "failure_reason",
            }.issubset(report),
        )
        failed_final = {
            "objects": {"mug_1": {"position": [0.5, 0.0, 1.01]}},
            "task_success": True,
            "robot": {"phase": "FAILED"},
        }
        failed_report = build_robot_acceptance_report(
            scene_id="scene",
            initial_observation=initial,
            final_observation=failed_final,
            steps=120,
            ik="passed",
            grasp="failed",
            failure_reason="grasp_failure",
        )
        self.assertEqual(failed_report["result"], "failed")

        missing_phase_report = build_robot_acceptance_report(
            scene_id="scene",
            initial_observation=initial,
            final_observation={
                "objects": {"mug_1": {"position": [0.5, 0.0, 1.01]}},
                "task_success": True,
                "robot": {},
            },
            steps=120,
            ik="passed",
            grasp="passed",
            failure_reason=None,
        )
        self.assertEqual(missing_phase_report["result"], "failed")

    def test_failed_runtime_process_cannot_leave_passed_report(self) -> None:
        report = {"result": "passed", "failure_reason": None}
        _invalidate_report_on_process_failure(report, 1)
        self.assertEqual(report["result"], "failed")
        self.assertIn("runtime_process_failed", report["failure_reason"])
        self.assertEqual(report["runtime_process_returncode"], 1)

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

    def test_pick_place_recipe_uses_ready_real_mug_and_target_spec(self) -> None:
        factory = SceneFactory()
        result = factory.build_from_recipe("kitchen_franka_mug_pick_place", 77)
        self.assertTrue(result.valid, result.validation.to_dict())
        mug = next(item for item in result.scene.objects if item.object_id == "mug_1")
        success = result.scene.task["success"]
        self.assertEqual(mug.asset_id, "mug_001")
        self.assertEqual(success["predicate"], "pick_and_place")
        self.assertEqual(success["target_support"], "island_1")
        self.assertEqual(len(success["target_position_m"]), 3)


if __name__ == "__main__":
    unittest.main()
