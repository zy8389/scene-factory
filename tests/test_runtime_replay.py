"""Playback integrity tests use numerical fixtures, never simulated success claims."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scene_factory.dataset import DatasetError, sha256_file, write_json_atomic
from scene_factory.runtime_evidence import assess_task_trace
from scene_factory.runtime_replay import replay_payload
from scene_factory.tasks import TaskEvaluator


class RuntimeReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="replay-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = self.root / "cases" / "fixture-lift"
        (self.case / "task").mkdir(parents=True)

    def _fixture(self, *, failure: bool = False, rotation: bool = False) -> None:
        scene = {"scene_id": "numeric-fixture", "room_dimensions_m": [4, 3, 3], "room_type": "kitchen",
                 "objects": [{"object_id": "mug_1", "asset_id": "mug_001", "dynamic": True,
                              "category": "mug", "bbox_m": [0.1, 0.1, 0.1],
                              "pose": {"position": [0, 0, 1.05], "yaw_deg": 20}, "support": "floor"}],
                 "task": {"target_object": "mug_1", "asset_id": "mug_001", "success": {
                     "predicate": "lifted", "subject": "mug_1", "min_height_delta_m": 0.1}}}
        phases = ["PRE_GRASP", "APPROACH", "FAILED"] if failure else [
            "PRE_GRASP", "APPROACH", "GRASP", "VERIFY_GRASP", "LIFT", "DONE"
        ]
        evaluator = TaskEvaluator(scene["task"], {"mug_1": (0, 0, 1.05)})
        frames = []
        for step, phase in enumerate(phases):
            position = [0, 0, 1.2 if step >= 4 else 1.05]
            pairs = [{"collider0": f"/World/Robot/panda_{side}finger", "collider1": "/World/Objects/mug_1",
                      "event_type": "CONTACT_FORCE", "force": [0.25, 0, 0], "force_magnitude": 0.25}
                     for side in ("left", "right")] if step >= 2 and not failure else []
            diagnostics = {"contact_report_available": True, "contact_report_subscribed": True,
                           "contact_force_read_valid": True, "finger_target_contact": bool(pairs),
                           "gripper_open": not bool(pairs), "contact_force_pairs": pairs}
            oracle = evaluator.status({"mug_1": tuple(position)}, diagnostics)
            obj = {"position": position}
            if rotation:
                obj["orientation_wxyz"] = [1, 0, 0, 0]
            frames.append({"simulation_step": step, "timestamp": step / 60, "objects": {"mug_1": obj},
                           "robot": {"name": "franka", "phase": phase, "joint_positions": [step / 100] * 9,
                                     "joint_velocities": [0] * 9, "end_effector_pose": {
                                         "position": [0.1, 0.02 * step, 1.3], "orientation_wxyz": [1, 0, 0, 0]},
                                     "finger_positions": {"left": [0.1, -0.02, 1.3], "right": [0.1, 0.02, 1.3]},
                                     "grasp_diagnostics": diagnostics, "task_oracle": oracle,
                                     "failure_reason": "pre_grasp_timeout" if phase == "FAILED" else None},
                           "task_success": bool(oracle["task_success"]), "reward": 1 if phase == "DONE" else 0,
                           "terminated": step == len(phases) - 1, "truncated": False})
        settings = {"physics_dt": 1 / 60}
        summary = {"robot_asset_source": "nucleus_franka_usd", "official_isaac_asset": True,
                   "ik": "passed", "grasp": "failed" if failure else "passed",
                   "failure_reason": "pre_grasp_timeout" if failure else None}
        write_json_atomic(self.case / "layout.json", scene)
        self.trace = self.case / "task/task_trace.jsonl"
        self.trace.write_text("".join(json.dumps(row) + "\n" for row in frames), encoding="utf-8")
        bindings = {"input_sha256": {"layout": sha256_file(self.case / "layout.json")}}
        job = {"run_id": "fixture-run", "case_id": self.case.name, "mode": "task",
               "bindings": bindings, "settings": settings}
        report = {**job, "schema_version": "scene_factory.runtime_worker.v1", "worker_exit_code": 0,
                  "result": "task_failure" if failure else "task_success", "runtime_summary": summary,
                  "isaac_sim_version": "numeric-fixture", "trace_path": str(self.trace),
                  "trace_rows": len(frames), "completed_steps": len(frames) - 1,
                  "trace_sha256": sha256_file(self.trace)}
        write_json_atomic(self.case / "task/job.json", job)
        write_json_atomic(self.case / "task/worker_report.json", report)
        parent = {"returncode": 0, "timed_out": False, "job_sha256": sha256_file(self.case / "task/job.json")}
        write_json_atomic(self.case / "task/parent_process.json", parent)
        assessment = assess_task_trace(scene, frames, {**settings, "runtime_summary": summary,
                                                      "isaac_sim_version": "numeric-fixture"})
        self.assertTrue(assessment["metrics"]["evidence_complete"], assessment)
        result = {"schema_version": "scene_factory.runtime_case.v1", "run_id": "fixture-run",
                  "case_id": self.case.name, "evidence_complete": True, "input_sha256": bindings["input_sha256"],
                  "stages": {"task": {"state": "attempted", "integrity_errors": [], "assessment": assessment,
                                      "job_sha256": sha256_file(self.case / "task/job.json"),
                                      "parent_sha256": sha256_file(self.case / "task/parent_process.json"),
                                      "worker_report_sha256": sha256_file(self.case / "task/worker_report.json")}}}
        write_json_atomic(self.case / "result.json", result)
        manifest = {"schema_version": "scene_factory.runtime_batch.v1", "state": "complete", "run_id": "fixture-run",
                    "cases": [{"case_id": self.case.name, "result_sha256": sha256_file(self.case / "result.json")}]}
        write_json_atomic(self.root / "manifest.json", manifest)
        self.frames = frames

    def _refresh(self, *, trace: bool = False) -> None:
        """Rebind modified bytes without changing the independently recorded assessment."""
        if trace:
            report_path = self.case / "task/worker_report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["trace_sha256"] = sha256_file(self.trace)
            write_json_atomic(report_path, report)
        result_path = self.case / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        stage = result["stages"]["task"]
        stage["worker_report_sha256"] = sha256_file(self.case / "task/worker_report.json")
        stage["parent_sha256"] = sha256_file(self.case / "task/parent_process.json")
        write_json_atomic(result_path, result)
        manifest_path = self.root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["cases"][0]["result_sha256"] = sha256_file(result_path)
        write_json_atomic(manifest_path, manifest)

    def test_preserves_every_numeric_state_time_contact_and_assets(self) -> None:
        self._fixture()
        original = self.trace.read_bytes()
        assets = {"mug_001": {"visual_url": "/assets/mug.glb"}}
        payload = replay_payload(self.case, assets=assets)
        self.assertEqual(payload["frame_count"], len(self.frames))
        self.assertEqual(payload["dt"], 1 / 60)
        self.assertEqual(payload["duration_s"], self.frames[-1]["timestamp"])
        self.assertEqual(payload["source"], "recorded_state")
        self.assertEqual(payload["assets"], assets)
        self.assertIsNot(payload["assets"], assets)
        self.assertEqual(payload["task_status"], "passed")
        for actual, original_frame in zip(payload["frames"], self.frames, strict=True):
            self.assertEqual(actual["step"], original_frame["simulation_step"])
            self.assertEqual(actual["timestamp"], original_frame["timestamp"])
            self.assertEqual(actual["objects"], original_frame["objects"])
            self.assertEqual(actual["robot"]["joint_positions"], original_frame["robot"]["joint_positions"])
            self.assertEqual(actual["robot"]["finger_positions"], original_frame["robot"]["finger_positions"])
        self.assertFalse(payload["frames"][0]["contacts"]["left"])
        self.assertTrue(payload["frames"][-1]["contacts"]["left"])
        self.assertTrue(payload["frames"][-1]["contacts"]["right"])
        self.assertFalse(payload["object_orientation_recorded"])
        self.assertFalse(payload["robot_link_poses_recorded"])
        self.assertEqual(self.trace.read_bytes(), original)

    def test_complete_failed_trajectory_is_replayable(self) -> None:
        self._fixture(failure=True)
        payload = replay_payload(self.case)
        self.assertEqual(payload["task_status"], "failed")
        self.assertEqual(payload["frame_count"], 3)
        self.assertEqual(payload["frames"][-1]["phase"], "FAILED")
        self.assertEqual(payload["frames"][-1]["failure_reason"], "pre_grasp_timeout")
        self.assertTrue(payload["frames"][-1]["terminated"])
        self.assertFalse(payload["frames"][-1]["task_success"])

    def test_recorded_rotations_are_preserved(self) -> None:
        self._fixture(rotation=True)
        payload = replay_payload(self.case)
        self.assertTrue(payload["object_orientation_recorded"])
        self.assertEqual(payload["frames"][0]["objects"]["mug_1"]["orientation_wxyz"], [1, 0, 0, 0])

    def test_each_hash_chain_artifact_rejects_replacement(self) -> None:
        for name in ("layout.json", "result.json", "task/worker_report.json", "task/job.json",
                     "task/parent_process.json", "task/task_trace.jsonl"):
            with self.subTest(name=name):
                self._fixture()
                path = self.case / name
                path.write_bytes(path.read_bytes() + b" ")
                with self.assertRaisesRegex(DatasetError, "hash mismatch"):
                    replay_payload(self.case)

    def test_forged_success_rejected_even_after_hash_rebinding(self) -> None:
        self._fixture(failure=True)
        rows = copy.deepcopy(self.frames)
        rows[-1]["task_success"] = True
        self.trace.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        self._refresh(trace=True)
        with self.assertRaisesRegex(DatasetError, "differs"):
            replay_payload(self.case)

    def test_gaps_invalid_contacts_nan_and_finger_vectors_fail_closed(self) -> None:
        for field in ("gap", "contact", "nan", "finger", "rotation"):
            with self.subTest(field=field):
                self._fixture()
                rows = copy.deepcopy(self.frames)
                if field == "gap":
                    rows[2]["simulation_step"] = 3
                elif field == "contact":
                    rows[0]["robot"]["grasp_diagnostics"]["finger_target_contact"] = True
                elif field == "nan":
                    rows[0]["objects"]["mug_1"]["position"][0] = float("nan")
                elif field == "finger":
                    rows[0]["robot"]["finger_positions"]["left"] = [0, 0]
                else:
                    rows[0]["objects"]["mug_1"]["orientation_wxyz"] = [0, 0, 0, 0]
                self.trace.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
                self._refresh(trace=True)
                with self.assertRaises(DatasetError):
                    replay_payload(self.case)

    def test_missing_and_unrun_trajectories_are_rejected(self) -> None:
        self._fixture()
        self.trace.unlink()
        with self.assertRaisesRegex(DatasetError, "missing replay"):
            replay_payload(self.case)
        self._fixture()
        result_path = self.case / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["stages"]["task"]["state"] = "not_run"
        write_json_atomic(result_path, result)
        self._refresh()
        with self.assertRaisesRegex(DatasetError, "no complete replay"):
            replay_payload(self.case)

    def test_duplicate_json_keys_and_blank_lines_are_rejected(self) -> None:
        for field in ("duplicate", "blank"):
            with self.subTest(field=field):
                self._fixture()
                raw = self.trace.read_bytes()
                if field == "duplicate":
                    raw = raw.replace(b'"simulation_step": 0', b'"simulation_step": 0, "simulation_step": 0', 1)
                else:
                    raw += b"\n"
                self.trace.write_bytes(raw)
                self._refresh(trace=True)
                with self.assertRaises(DatasetError):
                    replay_payload(self.case)

    def test_limits_reject_oversized_payload_instead_of_dropping_frames(self) -> None:
        self._fixture()
        with patch("scene_factory.runtime_replay.MAX_TRACE_BYTES", 100):
            with self.assertRaisesRegex(DatasetError, "oversized"):
                replay_payload(self.case)
        with patch("scene_factory.runtime_replay.MAX_TRACE_FRAMES", len(self.frames) - 1):
            with self.assertRaisesRegex(DatasetError, "frame count"):
                replay_payload(self.case)


if __name__ == "__main__":
    unittest.main()
