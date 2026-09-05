from __future__ import annotations

import json
import math
import subprocess
import sys
import unittest
from unittest.mock import patch

from scene_factory.backends.isaac_binding import (
    SEKTION_TOP_DRAWER_BINDING,
    SEKTION_TOP_DRAWER_RUNTIME_ROOT,
)
from scene_factory.backends.isaac_interaction import (
    IsaacInteractionExecutor,
    _IsaacInteractionRuntime,
)
from scene_factory.backends.isaac_interaction import (
    GraspTopologyConfig,
    _ORIENTATION_FAMILY_DEGREES,
    _axis_angle_quaternion,
    _basis_rotation_quaternion,
    _branch_continuity_diagnostic,
    _cartesian_waypoints,
    _continuity_policy,
    _contact,
    _displacement_accounting,
    _fixture_candidate_validity,
    _compose_transform,
    _inverse_transform,
    _joint_limit_diagnostic,
    _matrix_to_quaternion,
    _orientation_family_variants,
    _pose_transform,
    _rank_long_horizon_orientation_candidates,
    _rank_fixture_candidates,
    _rotate_vector,
    _required_outward_clearance,
    _transform_point,
)
from scene_factory.execution import ExecutionCommand, ExecutionStepResult
from scene_factory.planning import InteractionAction, InteractionWorldState


class FakeInteractionRuntime:
    """Deterministic physical boundary fake; drawer moves only during pull steps."""

    def __init__(self, config, *, mode: str = "success") -> None:
        self.config = config
        self.mode = mode
        self.calls: list[str] = []
        self.closed = False
        self.reset_position = 0.0
        self.drawer_position = 0.0
        self.eef_position = (0.0, 0.0, 0.0)
        self.eef_target = self.eef_position
        self.arm_targets: list[list[float]] = []
        self.robot_joint_positions = [0.0] * 7
        self.solve_requests: list[dict[str, object]] = []
        self.pull_origin: tuple[float, ...] | None = None
        self.gripper_open = True
        self.release_started = False
        self.release_steps = 0
        self.pull_started = False
        self.pull_steps = 0
        self.solve_count = 0
        self.predictor_count = 0
        self.physics_steps = 0
        self.reset_joint_write_count = 0
        self.execution_joint_write_count = 0

    def reset(self, scene, initial_position: float) -> None:
        del scene
        self.calls.append("reset")
        self.reset_position = initial_position
        self.drawer_position = initial_position
        self.gripper_open = True

    def resolve_binding(self, binding):
        self.calls.append("resolve_binding")
        return {
            "valid": True,
            "binding_id": binding.binding_id,
            "runtime_asset_root_prim": SEKTION_TOP_DRAWER_RUNTIME_ROOT,
        }

    def read_joint(self) -> float:
        return self.drawer_position

    def read_joint_velocity(self):
        return 0.02 if self.mode == "release_velocity" and self.release_started else 0.0

    def read_contacts(self):
        if self.gripper_open:
            if (self.mode == "persistent_release" and self.release_started) or (self.mode == "release_recontact" and self.release_steps >= 10):
                return self._contact(True, True)
            return self._contact(False, False)
        if self.mode == "missing_left":
            return self._contact(False, True)
        if self.mode == "missing_right":
            return self._contact(True, False)
        if self.mode == "nonfinite_force":
            return {
                "left_contact": True,
                "right_contact": True,
                "force_valid": True,
                "nonzero_force": True,
                "force_samples": [[math.nan, 1.0, 0.0]],
                "contact_pairs": [],
            }
        if self.mode == "contact_loss" and self.pull_started and self.pull_steps >= 2:
            return self._contact(False, False)
        return self._contact(True, True)

    @staticmethod
    def _contact(left: bool, right: bool):
        return {
            "left_contact": left,
            "right_contact": right,
            "force_valid": True,
            "nonzero_force": left and right,
            "force_samples": [
                {"finger": "left", "force": [1.0, 0.0, 0.0]},
                {"finger": "right", "force": [1.0, 0.0, 0.0]},
            ],
            "contact_pairs": [
                {"finger": "left", "handle": "handle"},
                {"finger": "right", "handle": "handle"},
            ],
        }

    def read_gripper(self):
        return {
            "positions": [0.04, 0.04] if self.gripper_open else [0.0, 0.0],
            "open": self.gripper_open,
        }

    def close_gripper(self) -> None:
        self.calls.append("close_gripper")
        self.gripper_open = False

    def open_gripper(self) -> None:
        self.calls.append("open_gripper")
        self.gripper_open = True
        self.release_started = True

    def read_handle_frame(self):
        return {
            "position": [1.0, 2.0, 3.0],
            "transform": [
                1.0,
                0.0,
                0.0,
                0.0,
                0.0,
                1.0,
                0.0,
                0.0,
                0.0,
                0.0,
                1.0,
                0.0,
                1.0,
                2.0,
                3.0,
                1.0,
            ],
        }

    def read_opening_axis_world(self):
        return (0.0, 1.0, 0.0)

    def read_eef_pose(self):
        return {
            "position": list(self.eef_position),
            "orientation_wxyz": [0.0, 1.0, 0.0, 0.0],
        }

    def solve_ik(
        self,
        position,
        orientation,
        *,
        warm_start=None,
        position_tolerance_m=0.01,
    ):
        del orientation
        self.calls.append("solve_ik")
        self.solve_count += 1
        self.solve_requests.append(
            {
                "position": list(position),
                "warm_start": None if warm_start is None else list(warm_start),
                "position_tolerance_m": position_tolerance_m,
            }
        )
        if self.mode == "ik_failure":
            return {"success": False, "joint_positions": [], "reason": "injected_ik_failure"}
        self.eef_target = tuple(position)
        if self.solve_count >= 3:
            self.pull_started = True
        if warm_start is None:
            return {"success": True, "joint_positions": [0.0] * 7}
        if self.pull_origin is None:
            self.pull_origin = tuple(
                float(value) - (0.001 if index == 1 else 0.0)
                for index, value in enumerate(position)
            )
        progress = float(position[1]) - self.pull_origin[1]
        local_solution = [progress * 32.0] * 7
        previous_progress = float(warm_start[0]) / 32.0
        spacing = progress - previous_progress
        if self.mode == "branch_jump":
            local_solution[0] = float(warm_start[0]) + 1.0
        elif self.mode == "subdivide_once" and spacing > 0.0005 + 1.0e-12:
            local_solution[0] = float(warm_start[0]) + 1.0
        elif self.mode in {"predictor_recovery", "predictor_failure"}:
            seed_is_predicted = all(
                abs(float(seed) - local) <= 1.0e-12
                for seed, local in zip(warm_start, local_solution, strict=True)
            )
            if not seed_is_predicted or self.mode == "predictor_failure":
                return {
                    "success": False,
                    "joint_positions": [],
                    "reason": "injected_local_seed_required",
                    "seed_source": "explicit_previous_accepted_q",
                    "warm_start": list(warm_start),
                }
        return {
            "success": True,
            "joint_positions": local_solution,
            "seed_source": "explicit_previous_accepted_q",
            "warm_start": list(warm_start),
            "position_tolerance_m": position_tolerance_m,
        }

    def arm_joint_limits(self):
        return [(-3.0, 3.0)] * 7

    def arm_joint_names(self):
        return [f"panda_joint{index}" for index in range(1, 8)]

    def compute_arm_fk(self, joint_positions):
        origin = self.pull_origin or self.eef_position
        return {
            "frame": "right_gripper",
            "position": [origin[0], origin[1] + float(joint_positions[0]) / 32.0, origin[2]],
            "orientation_wxyz": [0.0, 1.0, 0.0, 0.0],
        }

    def predict_local_ik_seed(self, position, orientation, joint_positions):
        del orientation
        self.calls.append("predict_local_ik_seed")
        self.predictor_count += 1
        origin = self.pull_origin or self.eef_position
        progress = float(position[1]) - float(origin[1])
        predicted = [progress * 32.0] * 7
        delta = [
            value - float(previous)
            for value, previous in zip(predicted, joint_positions, strict=True)
        ]
        return {
            "joint_positions": predicted,
            "previous_joint_positions": list(joint_positions),
            "predicted_joint_delta_rad": delta,
            "unscaled_max_abs_delta_rad": max(abs(value) for value in delta),
            "applied_scale": 1.0,
            "max_abs_delta_limit_rad": 0.06031647148119346,
            "finite_difference_rad": 1.0e-5,
            "position_error_before_m": abs(progress),
            "orientation_target_wxyz": [0.0, 1.0, 0.0, 0.0],
            "singular_values": [1.0] * 6,
            "condition_number": 1.0,
            "method": "deterministic_test_predictor",
        }

    def apply_arm_targets(self, positions):
        self.calls.append("apply_arm_targets")
        self.arm_targets.append(list(positions))
        if self.pull_origin is not None:
            self.eef_target = tuple(self.compute_arm_fk(positions)["position"])

    def read_robot_joint_positions(self):
        return list(self.robot_joint_positions)

    def step_physics(self) -> None:
        self.calls.append("step_physics")
        self.physics_steps += 1
        if self.release_started:
            self.release_steps += 1
            if self.mode == "release_drift" and self.release_steps >= 10:
                self.drawer_position += 0.006
                return

        if self.arm_targets and self.mode != "joint_convergence_failure":
            self.robot_joint_positions = list(self.arm_targets[-1])
            if self.mode != "convergence_failure":
                self.eef_position = self.eef_target
        if self.pull_started:
            self.pull_steps += 1
            if self.mode == "no_motion":
                return
            if self.mode == "wrong_direction":
                self.drawer_position = max(-0.05, self.drawer_position - 0.005)
            else:
                self.drawer_position = min(0.05, self.drawer_position + 0.005)
        elif self.mode != "convergence_failure":
            self.eef_position = self.eef_target

    def joint_limits(self):
        if self.mode == "runtime_limits_mismatch":
            return (0.0, 0.2)
        return (SEKTION_TOP_DRAWER_BINDING.expected_lower_limit, SEKTION_TOP_DRAWER_BINDING.expected_upper_limit)

    def write_counters(self):
        return {
            "reset_joint_write_count": self.reset_joint_write_count,
            "execution_joint_write_count": self.execution_joint_write_count,
        }

    def isaac_sim_version(self):
        return "6.0.1.0-fake"

    def diagnostics(self, binding):
        return {"binding_id": binding.binding_id, "runtime": "deterministic_fake"}

    def close(self) -> None:
        self.calls.append("close")
        self.closed = True


class IsaacInteractionExecutorTests(unittest.TestCase):
    plan_hash = "a" * 64

    def setUp(self) -> None:
        self.runtimes: list[FakeInteractionRuntime] = []

    def make_executor(self, mode: str = "success") -> IsaacInteractionExecutor:
        def factory(config):
            runtime = FakeInteractionRuntime(config, mode=mode)
            self.runtimes.append(runtime)
            return runtime

        executor = IsaacInteractionExecutor(runtime_factory=factory)
        executor.reset(
            {"scene_id": "fake_scene"},
            InteractionWorldState(
                joint_positions={
                    SEKTION_TOP_DRAWER_BINDING.semantic_asset_id: {
                        SEKTION_TOP_DRAWER_BINDING.semantic_joint_id: 0.0,
                    }
                }
            ),
        )
        return executor

    def command(self, action: str, *, step_id: int = 0, joint_id=None, target=None):
        return ExecutionCommand.from_action(
            self.plan_hash,
            InteractionAction(
                step_id,
                action,
                SEKTION_TOP_DRAWER_BINDING.semantic_asset_id,
                SEKTION_TOP_DRAWER_BINDING.semantic_region_id,
                joint_id,
                target,
            ),
        )

    def approach(self, executor, step_id=0):
        return executor.execute(self.command("approach", step_id=step_id))

    def grasp(self, executor, step_id=1):
        return executor.execute(self.command("grasp", step_id=step_id))

    def pull(self, executor, *, target=0.05, step_id=2, joint_id=None):
        return executor.execute(
            self.command(
                "pull",
                step_id=step_id,
                joint_id=joint_id or SEKTION_TOP_DRAWER_BINDING.semantic_joint_id,
                target=target,
            )
        )

    def release(self, executor, step_id=3):
        return executor.execute(self.command("release", step_id=step_id))

    def test_full_drawer_pull_uses_requested_35cm_path_not_5cm_pilot(self):
        executor = self.make_executor()
        self.approach(executor)
        self.grasp(executor)
        with patch.object(executor, "_precompute_pull_ik_path", return_value={"success": False}) as path:
            result = self.pull(executor, target=0.35)
        self.assertEqual(result.reason, "pull_ik_continuation_failed")
        self.assertAlmostEqual(path.call_args.args[2], 0.35)
        self.assertEqual(result.evidence["execution_joint_write_count"], 0)
        executor.close()

    def test_release_observes_thirty_post_separation_steps(self):
        executor = self.make_executor()
        self.assertEqual(self.approach(executor).status, "succeeded")
        self.assertEqual(self.grasp(executor).status, "succeeded")
        self.assertEqual(self.pull(executor).status, "succeeded")
        result = self.release(executor)
        self.assertEqual(result.status, "succeeded")
        samples = result.evidence["stability_samples"]
        self.assertEqual(len(samples), 30)
        self.assertTrue(all(b["physics_step"] == a["physics_step"] + 1 for a,b in zip(samples,samples[1:])))
        self.assertTrue(all(s["contact_separated"] and s["joint_velocity_m_s"] == 0.0 for s in samples))
        executor.close()

    def test_release_fails_on_velocity_drift_or_late_recontact(self):
        for mode in ("release_velocity", "release_drift", "release_recontact"):
            with self.subTest(mode=mode):
                executor = self.make_executor()
                self.approach(executor)
                self.grasp(executor)
                self.pull(executor)
                self.runtimes[-1].mode = mode
                self.runtimes[-1].release_steps = 0
                result = self.release(executor)
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.reason, "release_stability_failed")
                executor.close()

    def test_exact_capabilities_and_import_safety(self) -> None:
        executor = IsaacInteractionExecutor(runtime_factory=lambda config: FakeInteractionRuntime(config))
        self.assertEqual(
            executor.capabilities().to_dict(),
            {
                "executor": "isaac_interaction",
                "version": "1",
                "physical": True,
                "supported_actions": ["approach", "grasp", "pull", "release"],
                "articulation_execution": True,
            },
        )
        smoke = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from scene_factory.backends.isaac_interaction import IsaacInteractionExecutor; "
                "assert not any(name in sys.modules for name in ('isaacsim','omni','pxr','carb','numpy'))",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(smoke.returncode, 0, smoke.stderr)

    def test_grasp_topology_config_and_axis_alignment_are_geometry_driven(self) -> None:
        self.assertEqual(GraspTopologyConfig().selected_topology, "top_bottom_pinch")
        with self.assertRaises(ValueError):
            GraspTopologyConfig(selected_topology="random_quaternion")
        with self.assertRaises(ValueError):
            GraspTopologyConfig(pregrasp_distance_m=0.0)
        with self.assertRaises(ValueError):
            GraspTopologyConfig(orientation_family_angle_deg=12)
        forced = IsaacInteractionExecutor(
            runtime_factory=lambda config: FakeInteractionRuntime(config),
            fixture={"orientation_family_angle_deg": -45},
        )
        self.assertEqual(
            forced._config.grasp_topology.orientation_family_angle_deg,
            -45,
        )

        orientation = _basis_rotation_quaternion(
            (0.0, 1.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 0.0, 1.0),
            (1.0, 0.0, 0.0),
        )
        closing = _rotate_vector(orientation, (0.0, 1.0, 0.0))
        approach = _rotate_vector(orientation, (1.0, 0.0, 0.0))
        self.assertAlmostEqual(closing[0], 0.0, places=6)
        self.assertAlmostEqual(closing[1], 0.0, places=6)
        self.assertAlmostEqual(closing[2], 1.0, places=6)
        self.assertAlmostEqual(approach[0], 1.0, places=6)
        self.assertAlmostEqual(approach[1], 0.0, places=6)
        self.assertAlmostEqual(approach[2], 0.0, places=6)

        transform = (0.0, 1.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 4.0, 5.0, 6.0, 1.0)
        point = (2.0, 3.0, 4.0)
        self.assertEqual(
            _transform_point(_inverse_transform(transform, "test frame"), _transform_point(transform, point)),
            point,
        )

    def test_orientation_family_generation_is_deterministic_and_preserves_axes(self) -> None:
        closing = (0.0, 0.0, 1.0)
        approach = (-1.0, 0.0, 0.0)
        variants = _orientation_family_variants(
            (1.0, 0.0, 0.0, 0.0),
            closing,
            approach,
        )
        self.assertEqual(
            [item["degrees_about_closing_axis"] for item in variants],
            list(_ORIENTATION_FAMILY_DEGREES),
        )
        for item in variants:
            self.assertAlmostEqual(
                sum(a * b for a, b in zip(item["closing_axis_world"], closing)),
                1.0,
                places=12,
            )
            self.assertAlmostEqual(
                sum(
                    a * b
                    for a, b in zip(
                        item["approach_axis_world"],
                        item["closing_axis_world"],
                    )
                ),
                0.0,
                places=12,
            )

    def test_long_horizon_ranking_rejects_partial_paths_and_uses_margin_then_length(self) -> None:
        def candidate(
            angle: int,
            *,
            short: bool = True,
            long: bool = True,
            margin: float,
            length: float,
            alignment: float = 0.8,
        ) -> dict[str, object]:
            return {
                "orientation_family_angle_deg": angle,
                "short_range_pass": short,
                "long_range_pass": long,
                "minimum_joint_limit_margin_rad": margin,
                "joint_space_path_length_rad": length,
                "approach_alignment_to_negative_opening": alignment,
            }

        ranked = _rank_long_horizon_orientation_candidates(
            [
                candidate(-75, long=False, margin=1.0, length=0.1),
                candidate(-60, short=False, margin=1.0, length=0.1),
                candidate(-45, margin=0.2, length=1.4),
                candidate(-30, margin=0.2, length=1.1),
                candidate(-15, margin=0.3, length=2.0),
            ]
        )
        self.assertEqual(
            [item["orientation_family_angle_deg"] for item in ranked],
            [-15, -30, -45],
        )
        self.assertEqual([item["rank"] for item in ranked], [1, 2, 3])

    def test_fixture_candidate_validation_rejects_bounds_and_initial_collision(self) -> None:
        valid = _fixture_candidate_validity((0.72, -0.02, 0.83), 5.0)
        self.assertTrue(valid["valid"])
        self.assertTrue(valid["support_placement_valid"])
        self.assertTrue(valid["initial_robot_cabinet_contact_free"])

        out_of_bounds = _fixture_candidate_validity((0.90, 0.0, 0.78), 0.0)
        self.assertFalse(out_of_bounds["valid"])
        self.assertFalse(out_of_bounds["within_search_bounds"])

        collision = _fixture_candidate_validity(
            (0.70, 0.0, 0.78),
            0.0,
            initial_robot_cabinet_contact_count=1,
        )
        self.assertFalse(collision["valid"])
        self.assertFalse(collision["initial_robot_cabinet_contact_free"])

    def test_fixture_ranking_requires_50mm_and_uses_margin_then_path(self) -> None:
        def candidate(
            name: str,
            *,
            reach: bool = True,
            margin: float,
            path: float,
            deviation: float,
        ) -> dict[str, object]:
            return {
                "name": name,
                "physically_valid": True,
                "approach_ik_viable": True,
                "grasp_ik_viable": True,
                "reach_50mm": reach,
                "minimum_path_margin_rad": margin,
                "joint_space_path_length_rad": path,
                "fixture_deviation_norm": deviation,
            }

        ranked = _rank_fixture_candidates(
            [
                candidate("partial", reach=False, margin=1.0, path=0.1, deviation=0.0),
                candidate("longer", margin=0.2, path=1.4, deviation=0.1),
                candidate("shorter", margin=0.2, path=1.1, deviation=0.2),
                candidate("best_margin", margin=0.3, path=2.0, deviation=0.3),
                candidate("at_limit", margin=0.0, path=0.5, deviation=0.0),
            ]
        )
        self.assertEqual(
            [item["name"] for item in ranked],
            ["best_margin", "shorter", "longer"],
        )

    def test_feedback_cannot_be_enabled_through_fixture_before_recovery(self) -> None:
        with self.assertRaises(ValueError):
            IsaacInteractionExecutor(
                runtime_factory=lambda config: FakeInteractionRuntime(config),
                fixture={"drawer_feedback_enabled": True},
            )

    def test_endpoint_clearance_uses_all_open_gripper_components(self) -> None:
        obstacle = [
            (x, y, z)
            for x in (0.0, 1.0)
            for y in (-1.0, 1.0)
            for z in (-1.0, 1.0)
        ]
        components = {
            "panda_hand": [(1.1, 0.0, 0.0), (1.2, 0.1, 0.1)],
            "panda_leftfinger": [(0.5, 0.0, 0.0), (0.8, 0.1, 0.1)],
            "panda_rightfinger": [(0.4, 2.0, 0.0), (0.7, 2.1, 0.1)],
        }
        result = _required_outward_clearance(
            components,
            obstacle,
            (1.0, 0.0, 0.0),
            0.002,
        )
        self.assertAlmostEqual(result["required_outward_shift_m"], 0.502)
        self.assertAlmostEqual(
            result["component_required_outward_shift_m"]["panda_leftfinger"],
            0.502,
        )
        self.assertEqual(
            result["component_required_outward_shift_m"]["panda_rightfinger"],
            0.0,
        )
        self.assertAlmostEqual(
            result["component_intervals_after"]["panda_leftfinger"][0][0],
            1.002,
        )

    def test_cartesian_waypoints_are_bounded_and_include_endpoint(self) -> None:
        start = (0.06, 0.0, 0.0)
        end = (0.0, 0.0, 0.0)
        waypoints = _cartesian_waypoints(start, end, 0.01)
        previous = start
        for waypoint in waypoints:
            self.assertLessEqual(math.dist(previous, waypoint), 0.01 + 1.0e-12)
            previous = waypoint
        self.assertEqual(waypoints[-1], end)

    def test_controller_target_uses_explicit_frame_transform(self) -> None:
        observation_position = (0.4, -0.2, 1.1)
        observation_orientation = (0.5, -0.5, -0.5, 0.5)
        observation_pose = _pose_transform(observation_position, observation_orientation)
        observation_to_controller = _pose_transform((0.0, 0.0, 0.1), (0.0, 0.0, 0.0, 1.0))
        controller_pose = _compose_transform(observation_pose, observation_to_controller)
        expected_position = _transform_point(observation_pose, (0.0, 0.0, 0.1))
        self.assertEqual(controller_pose[12:15], expected_position)
        self.assertEqual(
            _matrix_to_quaternion(controller_pose),
            _matrix_to_quaternion(_compose_transform(observation_pose, observation_to_controller)),
        )

        family_rotation = _axis_angle_quaternion((0.0, 0.0, 1.0), math.radians(15.0))
        self.assertAlmostEqual(_rotate_vector(family_rotation, (0.0, 0.0, 1.0))[2], 1.0, places=6)
        approach = _rotate_vector(family_rotation, (-1.0, 0.0, 0.0))
        self.assertGreater(sum(a * b for a, b in zip(approach, (-1.0, 0.0, 0.0), strict=True)), 0.9)

    def test_contact_normal_and_traction_fields_are_preserved(self) -> None:
        contact = _contact(
            {
                "left_contact": True,
                "right_contact": True,
                "force_valid": True,
                "nonzero_force": True,
                "force_samples": [],
                "contact_pairs": [],
                "contact_detail_available": True,
                "surface_pair_valid": True,
                "normal_dot_left_right": -1.0,
                "opposed_contact": True,
                "net_force_on_handle": [0.01, 0.0, 0.0],
                "opening_axis_world": [1.0, 0.0, 0.0],
                "axial_traction": 0.01,
                "axial_traction_available": True,
            }
        )
        self.assertTrue(contact["opposed_contact"])
        self.assertEqual(contact["normal_dot_left_right"], -1.0)
        self.assertEqual(contact["axial_traction"], 0.01)

    def test_displacement_accounting_identifies_eef_tracking_loss(self) -> None:
        accounting = _displacement_accounting(
            0.010,
            0.00764773554,
            0.007576808,
            0.007744789,
            0.00774475091,
        )
        self.assertAlmostEqual(accounting["controller_tracking_loss_m"], 0.00235226446)
        self.assertAlmostEqual(accounting["gripper_internal_loss_m"], 0.00007092754)
        self.assertAlmostEqual(accounting["grasp_relative_slip_m"], -0.000167981)
        self.assertAlmostEqual(accounting["handle_drawer_loss_m"], 0.00000003809)
        self.assertAlmostEqual(accounting["accounting_residual_m"], 0.0)
        self.assertEqual(accounting["primary_loss_source"], "EEF_TRACKING_LIMIT")
        self.assertAlmostEqual(
            accounting["traction_efficiency_drawer_per_observed_right_gripper"],
            0.00774475091 / 0.00764773554,
        )

    def test_joint_limit_diagnostic_names_limiting_and_outside_joint(self) -> None:
        diagnostic = _joint_limit_diagnostic(
            [0.0, -3.08],
            [(-2.0, 2.0), (-3.0718, -0.0698)],
            ["joint_a", "panda_joint4"],
        )
        self.assertFalse(diagnostic["all_within_limits"])
        self.assertEqual(diagnostic["limiting_joint"]["joint_name"], "panda_joint4")
        self.assertEqual(diagnostic["limiting_joint"]["nearest_limit"], "lower")
        self.assertAlmostEqual(
            diagnostic["minimum_joint_limit_margin_rad"],
            -0.0082,
        )

    def test_contact_local_point_uses_current_handle_frame(self) -> None:
        runtime = object.__new__(_IsaacInteractionRuntime)
        transforms = iter(
            (
                _pose_transform((1.0, 2.0, 3.0), (1.0, 0.0, 0.0, 0.0)),
                _pose_transform((1.007, 2.0, 3.0), (1.0, 0.0, 0.0, 0.0)),
            )
        )
        runtime._current_handle_geometry_frame = lambda: {"transform": next(transforms)}
        first = runtime._contact_point_handle_local((1.1, 2.0, 3.0))
        second = runtime._contact_point_handle_local((1.107, 2.0, 3.0))
        self.assertEqual(first, second)
        self.assertAlmostEqual(first[0], 0.1)

    def test_wrong_runtime_topology_fails_closed(self) -> None:
        class WrongTopologyRuntime(FakeInteractionRuntime):
            def read_grasp_plan(self):
                return {
                    "candidate_id": "wrong",
                    "topology": "front_face_clamp",
                    "orientation_wxyz": [0.0, 1.0, 0.0, 0.0],
                    "grasp_position_world": [0.0, 0.0, 0.0],
                    "pregrasp_position_world": [-0.06, 0.0, 0.0],
                    "closing_axis_world": [0.0, 0.0, 1.0],
                    "approach_axis_world": [1.0, 0.0, 0.0],
                    "opening_axis_world": [0.0, 1.0, 0.0],
                    "handle_surface_pair": ["back", "front"],
                    "expected_contact_normals": [[0.0, 0.0, -1.0], [0.0, 0.0, 1.0]],
                    "required_gripper_opening_m": 0.03,
                }

        executor = IsaacInteractionExecutor(
            runtime_factory=lambda config: WrongTopologyRuntime(config),
        )
        executor.reset(
            {"scene_id": "fake_scene"},
            InteractionWorldState(
                joint_positions={
                    SEKTION_TOP_DRAWER_BINDING.semantic_asset_id: {
                        SEKTION_TOP_DRAWER_BINDING.semantic_joint_id: 0.0,
                    }
                }
            ),
        )
        result = self.approach(executor)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "grasp_topology_policy_mismatch")
        self.assertEqual(result.evidence["execution_joint_write_count"], 0)

    def test_push_and_rotate_fail_closed_without_drawer_writes(self) -> None:
        executor = self.make_executor()
        for action in ("push", "rotate"):
            with self.subTest(action=action):
                result = executor.execute(self.command(action))
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.reason, "unsupported_action")
                self.assertEqual(result.evidence["execution_joint_write_count"], 0)

    def test_reset_lifecycle_and_snapshot_reads_runtime(self) -> None:
        executor = self.make_executor()
        runtime = self.runtimes[0]
        self.assertEqual(runtime.calls[:2], ["reset", "resolve_binding"])
        snapshot = executor.snapshot()
        self.assertEqual(
            snapshot["articulation_positions"]["sektion_cabinet"]["drawer_top_joint"],
            0.0,
        )
        self.assertIsNone(snapshot["holding"])
        self.assertEqual(snapshot["reset_joint_write_count"], 0)
        self.assertEqual(snapshot["execution_joint_write_count"], 0)

    def test_approach_success_and_ik_failure(self) -> None:
        executor = self.make_executor()
        result = self.approach(executor)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.evidence["target_pose"]["position"], [0.94, 2.0, 2.995])
        self.assertLessEqual(result.evidence["position_error_m"], 0.01)
        self.assertLessEqual(result.evidence["orientation_error_rad"], 0.1)

        failed = self.make_executor("ik_failure").execute(self.command("approach"))
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.reason, "ik_failed")

    def test_approach_convergence_failure_is_closed(self) -> None:
        executor = self.make_executor("convergence_failure")
        result = self.approach(executor)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "motion_convergence_failed")
        self.assertIsNone(executor.snapshot()["holding"])

    def test_grasp_requires_both_contacts_finite_force_and_closed_gripper(self) -> None:
        executor = self.make_executor()
        self.assertEqual(self.approach(executor).status, "succeeded")
        result = self.grasp(executor)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.evidence["stable_contact_steps"], 5)
        self.assertEqual(result.evidence["left_contact"], True)
        self.assertEqual(result.evidence["right_contact"], True)
        self.assertEqual(result.evidence["contact_force_valid"], True)
        self.assertEqual(executor.snapshot()["holding"]["region_id"], "drawer_handle_top")

        for mode, expected in (("missing_left", False), ("missing_right", False), ("nonfinite_force", False)):
            with self.subTest(mode=mode):
                candidate = self.make_executor(mode)
                self.assertEqual(self.approach(candidate).status, "succeeded")
                failed = self.grasp(candidate)
                self.assertEqual(failed.status, "failed")
                self.assertEqual(failed.reason, "grasp_contact_gate_failed" if mode != "nonfinite_force" else "physical_runtime_failure")
                self.assertEqual(candidate.snapshot()["holding"], expected and {"object_id": "sektion_cabinet", "region_id": "drawer_handle_top"} or None)

    def test_pull_failures_and_success_have_zero_execution_drawer_writes(self) -> None:
        before_grasp = self.make_executor()
        self.assertEqual(self.pull(before_grasp).reason, "pull_before_grasp")

        for mode, kwargs, expected in (
            ("success", {"joint_id": "wrong_joint"}, "wrong_joint"),
            ("success", {"target": 0.5}, "target_outside_limits"),
        ):
            with self.subTest(expected=expected):
                candidate = self.make_executor()
                self.assertEqual(self.approach(candidate).status, "succeeded")
                self.assertEqual(self.grasp(candidate).status, "succeeded")
                result = self.pull(candidate, **kwargs)
                self.assertEqual(result.reason, expected)
                self.assertEqual(result.evidence["execution_joint_write_count"], 0)

        for mode, expected in (("no_motion", "drawer_joint_did_not_move"), ("wrong_direction", "drawer_joint_moved_wrong_direction"), ("contact_loss", "pull_contact_lost")):
            with self.subTest(mode=mode):
                candidate = self.make_executor(mode)
                self.assertEqual(self.approach(candidate).status, "succeeded")
                self.assertEqual(self.grasp(candidate).status, "succeeded")
                result = self.pull(candidate)
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.reason, expected)
                self.assertEqual(result.evidence["execution_joint_write_count"], 0)

        candidate = self.make_executor()
        self.assertEqual(self.approach(candidate).status, "succeeded")
        self.assertEqual(self.grasp(candidate).status, "succeeded")
        result = self.pull(candidate)
        self.assertEqual(result.status, "succeeded")
        self.assertAlmostEqual(result.evidence["drawer_joint_delta"], 0.05, places=6)
        self.assertEqual(result.evidence["waypoint_count"], 50)
        self.assertEqual(result.evidence["opening_axis_world"], [0.0, 1.0, 0.0])
        self.assertEqual(result.evidence["contact_maintained"], True)
        self.assertEqual(result.evidence["execution_joint_write_count"], 0)

    def test_branch_guard_is_derived_and_rejects_failed_solution(self) -> None:
        policy = _continuity_policy()
        diagnostic = _branch_continuity_diagnostic(
            [-2.494893297780138, -1.3300600448344424, -1.2581454241756633, -3.0430682855469335, 2.8402053632125526, 2.7939313346380885, -1.6921317073516609],
            [0.6459134221076965, 1.3197474479675293, 1.8946889638900757, -3.0451910495758057, 2.8371288776397705, 2.783684730529785, -1.6605244874954224],
            [0.6474552804268174, 1.3244498533210853, 1.8888707080112561, -3.0429288252356628, 2.8402357733408956, 2.789661896431579, -1.684788295328363],
            joint_names=[f"panda_joint{index}" for index in range(1, 8)],
            cartesian_target_delta_m=0.000548726973037,
        )
        self.assertEqual(
            policy["guard_derivation"],
            "geometric_midpoint_successful_local_max_and_failed_branch_min",
        )
        self.assertAlmostEqual(policy["success_to_guard_ratio"], policy["failure_to_guard_ratio"])
        self.assertTrue(diagnostic["branch_jump_detected"])
        self.assertEqual(diagnostic["dq_from_observed"]["max_joint_name"], "panda_joint3")

    def test_pull_continuation_uses_sequential_seeds_and_holds_targets(self) -> None:
        executor = self.make_executor()
        self.assertEqual(self.approach(executor).status, "succeeded")
        self.assertEqual(self.grasp(executor).status, "succeeded")
        runtime = self.runtimes[-1]
        call_start = len(runtime.calls)
        target_start = len(runtime.arm_targets)
        result = self.pull(executor)
        self.assertEqual(result.status, "succeeded")
        continuation = result.evidence["ik_continuation"]
        self.assertEqual(len(continuation["accepted_waypoints"]), 50)
        for index, waypoint in enumerate(continuation["accepted_waypoints"]):
            expected_seed = (
                continuation["initial_joint_positions"]
                if index == 0
                else continuation["accepted_waypoints"][index - 1]["joint_positions"]
            )
            self.assertEqual(waypoint["seed_joint_positions"], expected_seed)
        pull_calls = runtime.calls[call_start:]
        self.assertLess(
            max(index for index, call in enumerate(pull_calls) if call == "solve_ik"),
            min(index for index, call in enumerate(pull_calls) if call == "apply_arm_targets"),
        )
        pull_targets = runtime.arm_targets[target_start:]
        self.assertEqual(len(pull_targets), 100)
        self.assertTrue(
            all(pull_targets[index] == pull_targets[index + 1] for index in range(0, 100, 2))
        )

    def test_branch_jump_is_never_applied_and_fails_at_minimum_spacing(self) -> None:
        executor = self.make_executor("branch_jump")
        self.assertEqual(self.approach(executor).status, "succeeded")
        self.assertEqual(self.grasp(executor).status, "succeeded")
        runtime = self.runtimes[-1]
        target_count = len(runtime.arm_targets)
        result = self.pull(executor)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "pull_ik_continuation_failed")
        self.assertEqual(
            result.evidence["reason"],
            "ik_branch_discontinuity_at_minimum_spacing",
        )
        self.assertEqual(len(runtime.arm_targets), target_count)
        self.assertEqual(result.evidence["execution_joint_write_count"], 0)

    def test_deterministic_subdivision_restores_local_branch(self) -> None:
        executor = self.make_executor("subdivide_once")
        runtime = self.runtimes[-1]
        path = executor._precompute_pull_ik_path(
            (0.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            0.003,
            (0.0, 1.0, 0.0, 0.0),
        )
        self.assertTrue(path["success"])
        self.assertEqual(len(path["subdivisions"]), 3)
        self.assertEqual(len(path["accepted_waypoints"]), 6)
        self.assertTrue(
            all(
                abs(waypoint["cartesian_spacing_m"] - 0.0005) <= 1.0e-12
                for waypoint in path["accepted_waypoints"]
            )
        )
        self.assertEqual(runtime.execution_joint_write_count, 0)

    def test_jacobian_predictor_recovers_direct_local_ik_failure(self) -> None:
        executor = self.make_executor("predictor_recovery")
        runtime = self.runtimes[-1]
        path = executor._precompute_pull_ik_path(
            (0.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            0.003,
            (0.0, 1.0, 0.0, 0.0),
        )
        self.assertTrue(path["success"])
        self.assertEqual(len(path["accepted_waypoints"]), 3)
        self.assertEqual(runtime.predictor_count, 3)
        self.assertEqual(runtime.solve_count, 6)
        self.assertTrue(
            all(attempt["predictor_fallback"]["attempted"] for attempt in path["attempts"])
        )
        self.assertTrue(
            all(
                waypoint["accepted_seed_source"] == "jacobian_predicted_local_seed"
                for waypoint in path["accepted_waypoints"]
            )
        )
        self.assertTrue(
            all(
                attempt["predictor_fallback"]["prediction"]["condition_number"] == 1.0
                for attempt in path["attempts"]
            )
        )
        self.assertEqual(runtime.execution_joint_write_count, 0)

    def test_predictor_retry_failure_still_fails_closed_at_minimum_spacing(self) -> None:
        executor = self.make_executor("predictor_failure")
        runtime = self.runtimes[-1]
        path = executor._precompute_pull_ik_path(
            (0.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            0.003,
            (0.0, 1.0, 0.0, 0.0),
        )
        self.assertFalse(path["success"])
        self.assertEqual(path["reason"], "ik_failed_at_minimum_spacing")
        self.assertEqual(len(path["accepted_waypoints"]), 0)
        self.assertEqual(runtime.predictor_count, 3)
        self.assertEqual(runtime.solve_count, 6)
        self.assertTrue(
            all(attempt["predictor_fallback"]["attempted"] for attempt in path["attempts"])
        )
        self.assertEqual(runtime.execution_joint_write_count, 0)

    def test_pull_joint_convergence_failure_holds_one_solution(self) -> None:
        executor = self.make_executor("joint_convergence_failure")
        self.assertEqual(self.approach(executor).status, "succeeded")
        self.assertEqual(self.grasp(executor).status, "succeeded")
        result = self.pull(executor)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "pull_joint_convergence_failed")
        samples = result.evidence["arm_joint_samples"]
        self.assertEqual({sample["waypoint_id"] for sample in samples}, {1})
        targets = {tuple(sample["target_joint_positions"]) for sample in samples}
        self.assertEqual(len(targets), 1)
        self.assertEqual(result.evidence["execution_joint_write_count"], 0)

    def test_release_requires_contact_separation_and_observes_drawer(self) -> None:
        executor = self.make_executor()
        self.assertEqual(self.approach(executor).status, "succeeded")
        self.assertEqual(self.grasp(executor).status, "succeeded")
        self.assertEqual(self.pull(executor).status, "succeeded")
        result = self.release(executor)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.evidence["contact_separated"], True)
        self.assertGreaterEqual(result.evidence["separation_stable_steps"], 5)
        self.assertEqual(executor.snapshot()["holding"], None)

        persistent = self.make_executor("persistent_release")
        self.assertEqual(self.approach(persistent).status, "succeeded")
        self.assertEqual(self.grasp(persistent).status, "succeeded")
        failed = self.release(persistent)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.reason, "release_contact_separation_failed")

    def test_lifecycle_correlation_and_finite_json(self) -> None:
        executor = self.make_executor()
        command = self.command("approach", step_id=7)
        result = executor.execute(command)
        self.assertIsInstance(result, ExecutionStepResult)
        self.assertEqual(result.command_id, command.command_id)
        json.dumps(result.to_dict(), allow_nan=False)
        executor.close()
        executor.close()
        with self.assertRaises(RuntimeError):
            executor.execute(command)
        with self.assertRaises(RuntimeError):
            executor.snapshot()

    def test_invalid_reset_state_fails_before_runtime_start(self) -> None:
        runtimes: list[FakeInteractionRuntime] = []

        def factory(config):
            runtime = FakeInteractionRuntime(config)
            runtimes.append(runtime)
            return runtime

        executor = IsaacInteractionExecutor(runtime_factory=factory)
        with self.assertRaises(Exception):
            executor.reset({"scene_id": "fake"}, InteractionWorldState())
        self.assertEqual(runtimes, [])


if __name__ == "__main__":
    unittest.main()
