from __future__ import annotations

import copy
import math
import unittest

from scene_factory.quality import quality_rejection_codes
from scene_factory.runtime_evidence import assess_physics_trace, assess_task_trace, runtime_quality
from scene_factory.tasks import TaskEvaluator


class RuntimeEvidenceTests(unittest.TestCase):
    @staticmethod
    def scene(predicate: str = "lifted") -> dict:
        success = {"predicate": "lifted", "subject": "mug_1", "min_height_delta_m": 0.1}
        if predicate == "pick_and_place":
            success = {"predicate": predicate, "subject": "mug_1", "target_support": "table_1",
                       "target_position_m": [0.2, 0, 1.05], "target_tolerance_m": [0.05, 0.05, 0.03],
                       "min_lift_delta_m": 0.1, "settle_steps": 2, "max_settle_step_distance_m": 0.005}
        return {"scene_id": "runtime-example", "seed": 8, "room_dimensions_m": [4, 3, 3],
                "objects": [{"object_id": "table_1", "dynamic": False, "bbox_m": [1, 1, 1],
                             "pose": {"position": [0, 0, 0.5], "yaw_deg": 0}, "support": None},
                            {"object_id": "mug_1", "dynamic": True, "bbox_m": [0.1, 0.1, 0.1],
                             "pose": {"position": [0, 0, 1.05], "yaw_deg": 0}, "support": "table_1:top"}],
                "task": {"target_object": "mug_1", "asset_id": "mug_001", "success": success}}

    @staticmethod
    def physics_samples() -> list[dict]:
        return [{"step": step, "timestamp": step / 60, "stable": True,
                 "objects": {"mug_1": {"position": [0, 0, 1.05], "orientation_wxyz": [1, 0, 0, 0],
                                       "linear_velocity_mps": [0, 0, 0], "angular_velocity_rps": [0, 0, 0],
                                       "world_bbox_min": [-0.05, -0.05, 1], "world_bbox_max": [0.05, 0.05, 1.1]},
                             "table_1": {"world_bbox_min": [-0.5, -0.5, 0], "world_bbox_max": [0.5, 0.5, 1]}}}
                for step in range(241)]

    @classmethod
    def task_frames(cls, predicate: str = "lifted") -> tuple[dict, list[dict], dict]:
        scene = cls.scene(predicate)
        phases = ["PRE_GRASP", "APPROACH", "GRASP", "VERIFY_GRASP", "LIFT"]
        phases += ["TRANSFER", "LOWER", "RELEASE", "VERIFY_PLACE", "DONE"] if predicate == "pick_and_place" else ["DONE"]
        initial = {"table_1": (0, 0, 0.5), "mug_1": (0, 0, 1.05)}
        evaluator = TaskEvaluator(scene["task"], initial)
        frames = []
        for step, phase in enumerate(phases):
            closed = phase in ("GRASP", "VERIFY_GRASP", "LIFT", "TRANSFER", "LOWER") or (predicate == "lifted" and phase == "DONE")
            contact = closed
            lifted = step >= 4 and (predicate == "lifted" or phase in ("LIFT", "TRANSFER", "LOWER"))
            x = 0.2 if predicate == "pick_and_place" and step >= 5 else 0
            position = (x, 0, 1.2 if lifted else 1.05)
            pairs = [{"collider0": "/World/Robot/panda_leftfinger", "collider1": "/World/Objects/mug_1",
                      "event_type": "CONTACT_FORCE", "force": [0.25, 0, 0], "force_magnitude": 0.25}] if contact else []
            diagnostics = {"contact_report_available": True, "contact_report_subscribed": True,
                           "contact_force_read_valid": True, "finger_target_contact": contact,
                           "gripper_open": not closed, "contact_force_pairs": pairs}
            state = {"table_1": initial["table_1"], "mug_1": position}
            oracle = evaluator.status(state, diagnostics)
            frames.append({"simulation_step": step, "timestamp": step / 60,
                           "objects": {key: {"position": list(value)} for key, value in state.items()},
                           "robot": {"name": "franka", "phase": phase, "joint_positions": [0] * 9,
                                     "joint_velocities": [0] * 9,
                                     "end_effector_pose": {"position": [0, 0, 1.3], "orientation_wxyz": [1, 0, 0, 0]},
                                     "grasp_diagnostics": diagnostics, "task_oracle": oracle,
                                     "pick_status": "passed", "place_status": "passed", "released": not closed,
                                     "failure_reason": None},
                           "task_success": bool(oracle["task_success"]), "reward": 1 if phase == "DONE" else 0,
                           "terminated": phase == "DONE", "truncated": False})
        settings = {"runtime_summary": {"robot_asset_source": "nucleus_franka_usd", "official_isaac_asset": True,
                                        "ik": "passed", "grasp": "passed", "pick_status": "passed",
                                        "place_status": "passed", "released": predicate == "pick_and_place",
                                        "failure_reason": None}, "isaac_sim_version": "6.0.1"}
        return scene, frames, settings

    def test_settling_requires_measured_speed_drift_and_support(self) -> None:
        samples = self.physics_samples()
        result = assess_physics_trace(self.scene(), samples, {})
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["metrics"]["evidence_complete"])
        for row in samples[-60:]:
            row["objects"]["mug_1"]["linear_velocity_mps"] = [0.5, 0, 0]
        result = assess_physics_trace(self.scene(), samples, {})
        self.assertEqual(result["status"], "failed")
        self.assertIn("physics_not_stable:mug_1", result["failure_reasons"])
        self.assertTrue(result["metrics"]["evidence_complete"])

    def test_oscillation_with_zero_velocity_is_not_stable(self) -> None:
        samples = self.physics_samples()
        for step, row in enumerate(samples[-60:]):
            row["objects"]["mug_1"]["position"][0] = 0.008 * math.sin(step)
        result = assess_physics_trace(self.scene(), samples, {})
        self.assertFalse(result["checks"]["stable"])
        self.assertGreater(result["metrics"]["objects"]["mug_1"]["max_position_drift_m"], 0.01)

    def test_missing_nonfinite_and_short_physics_trace_fail_closed(self) -> None:
        cases = []
        missing = self.physics_samples()
        missing.pop(100)
        cases.append(missing)
        nonfinite = self.physics_samples()
        nonfinite[100]["objects"]["mug_1"]["angular_velocity_rps"][0] = float("nan")
        cases.append(nonfinite)
        cases.append(self.physics_samples()[:200])
        for samples in cases:
            with self.subTest(length=len(samples)):
                result = assess_physics_trace(self.scene(), samples, {})
                self.assertEqual(result["status"], "failed")
                self.assertFalse(result["metrics"]["evidence_complete"])

    def test_stable_object_that_fell_off_table_fails_support(self) -> None:
        samples = self.physics_samples()
        for row in samples[100:]:
            mug = row["objects"]["mug_1"]
            mug["position"] = [1, 0, 0.05]
            mug["world_bbox_min"] = [0.95, -0.05, 0]
            mug["world_bbox_max"] = [1.05, 0.05, 0.1]
        result = assess_physics_trace(self.scene(), samples, {})
        self.assertTrue(result["checks"]["stable"])
        self.assertFalse(result["checks"]["support_retained"])
        self.assertTrue(result["metrics"]["evidence_complete"])

    def test_physics_uses_real_bbox_bottom_not_pose_or_raw_pass(self) -> None:
        samples = self.physics_samples()
        for row in samples:
            row["objects"]["mug_1"]["world_bbox_min"][2] += 0.025
        result = assess_physics_trace(self.scene(), samples, {})
        self.assertIn("physics_support_lost:mug_1", result["failure_reasons"])
        for row in samples:
            row["objects"].pop("table_1")
        result = assess_physics_trace(self.scene(), samples, {})
        self.assertFalse(result["checks"]["support_measured"])

    def test_lift_and_pick_place_are_recomputed_without_mutating_trace(self) -> None:
        for predicate in ("lifted", "pick_and_place"):
            with self.subTest(predicate=predicate):
                scene, frames, settings = self.task_frames(predicate)
                before = copy.deepcopy(frames)
                result = assess_task_trace(scene, frames, settings)
                self.assertEqual(result["status"], "passed", result)
                self.assertTrue(result["metrics"]["observed_success"])
                self.assertTrue(result["metrics"]["evidence_complete"])
                self.assertEqual(frames, before)

    def test_contact_api_missing_and_bogus_force_cannot_pass(self) -> None:
        scene, frames, settings = self.task_frames()
        for frame in frames:
            frame["robot"]["grasp_diagnostics"].pop("contact_report_subscribed")
        result = assess_task_trace(scene, frames, settings)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["checks"]["contact_api_available"])
        scene, frames, settings = self.task_frames()
        for frame in frames:
            for pair in frame["robot"]["grasp_diagnostics"]["contact_force_pairs"]:
                pair["collider1"] = "/World/Objects/other_mug"
        result = assess_task_trace(scene, frames, settings)
        self.assertFalse(result["checks"]["physical_grasp_observed"])

    def test_forged_final_success_without_pick_history_is_rejected(self) -> None:
        scene, frames, settings = self.task_frames("pick_and_place")
        for frame in frames:
            frame["objects"]["mug_1"]["position"][2] = 1.05
        result = assess_task_trace(scene, frames, settings)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["metrics"]["replayed_task_success"])
        self.assertFalse(result["checks"]["oracle_consistency"])
        self.assertFalse(result["metrics"]["evidence_complete"])

    def test_complete_failure_does_not_require_successful_grasp(self) -> None:
        scene, frames, settings = self.task_frames()
        for frame in frames:
            diagnostics = frame["robot"]["grasp_diagnostics"]
            diagnostics["finger_target_contact"] = False
            diagnostics["contact_force_pairs"] = []
            diagnostics["contact_force_read_valid"] = False
        final = frames[-1]
        final["robot"]["phase"] = "FAILED"
        final["robot"]["failure_reason"] = "grasp_diagnostics_unavailable"
        final["reward"] = 0
        settings["runtime_summary"]["failure_reason"] = "grasp_diagnostics_unavailable"
        settings["runtime_summary"]["grasp"] = "failed"
        result = assess_task_trace(scene, frames, settings)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["metrics"]["evidence_complete"], result)
        self.assertFalse(result["metrics"]["observed_success"])
        self.assertEqual(result["failure_reasons"][-1], "grasp_diagnostics_unavailable")
        self.assertEqual(result["metrics"]["runtime_failure_reason"], "grasp_diagnostics_unavailable")

    def test_task_missing_frames_bad_terminal_and_wrong_source_rejected(self) -> None:
        scene, frames, settings = self.task_frames()
        frames.pop(2)
        result = assess_task_trace(scene, frames, settings)
        self.assertFalse(result["checks"]["continuous_time"])
        scene, frames, settings = self.task_frames()
        frames[-1]["terminated"] = False
        result = assess_task_trace(scene, frames, settings)
        self.assertFalse(result["checks"]["terminal_consistency"])
        scene, frames, settings = self.task_frames()
        settings["runtime_summary"]["robot_asset_source"] = "isaacsim_bundled_franka_urdf"
        result = assess_task_trace(scene, frames, settings)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["checks"]["official_franka"])
        self.assertTrue(result["metrics"]["evidence_complete"])

    def test_runtime_quality_preserves_layer_failures_and_contiguous_level(self) -> None:
        scene, frames, settings = self.task_frames()
        physics = assess_physics_trace(scene, self.physics_samples(), {})
        task = assess_task_trace(scene, frames, settings)
        quality = runtime_quality(scene, {"valid": True, "issues": [], "metrics": {}}, physics, task)
        self.assertEqual(quality["highest_verified_level"], "task")
        self.assertEqual(quality_rejection_codes(quality, "task"), [])
        physics["status"] = "failed"
        physics["failure_reasons"] = ["physics_not_stable:mug_1"]
        quality = runtime_quality(scene, {"valid": True, "issues": [], "metrics": {}}, physics, task)
        self.assertEqual(quality["highest_verified_level"], "layout")
        self.assertEqual(quality_rejection_codes(quality, "task"), ["physics_not_stable:mug_1"])
        self.assertEqual(assess_task_trace(scene, [], settings)["status"], "not_verified")
        self.assertEqual(assess_physics_trace(scene, [], {})["status"], "not_verified")


if __name__ == "__main__":
    unittest.main()
