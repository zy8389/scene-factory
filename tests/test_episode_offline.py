from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scene_factory.cli import main
from scene_factory.tasks import TaskEvaluator
from scene_factory.trajectory import (
    EpisodeRecorder,
    _write_npy_float32,
    inspect_episode,
    replay_episode,
    validate_episode,
)


class OfflineEpisodeTests(unittest.TestCase):
    _PHASES = (
        "PRE_GRASP",
        "APPROACH",
        "GRASP",
        "VERIFY_GRASP",
        "LIFT",
        "TRANSFER",
        "LOWER",
        "RELEASE",
        "VERIFY_PLACE",
        "DONE",
    )

    @staticmethod
    def _task_spec() -> dict:
        return {
            "target_object": "mug_1",
            "success": {
                "predicate": "pick_and_place",
                "subject": "mug_1",
                "target_support": "island_1",
                "target_position_m": [0.0, 0.0, 1.0],
                "target_tolerance_m": [0.05, 0.05, 0.03],
                "min_lift_delta_m": 0.1,
                "settle_steps": 1,
                "max_settle_step_distance_m": 0.005,
            },
        }

    @classmethod
    def _metadata(cls, include_task: bool = True) -> dict:
        metadata = {
            "schema_version": "scene_factory.trajectory.v1",
            "scene_id": "scene-offline",
            "recipe": "kitchen_franka_mug_pick_place",
            "seed": 7,
            "episode_id": "episode-offline",
            "robot_asset_source": "nucleus_franka_usd",
            "isaac_sim_version": "6.0.1",
            "camera": {"prim_path": "/World/Sensors/SceneFactoryRGBD", "resolution": [2, 1]},
            "intrinsics": None,
            "extrinsics": None,
            "control_frequency_hz": 60.0,
            "sensor_frequency_hz": 60.0,
        }
        if include_task:
            metadata["task_spec"] = cls._task_spec()
        return metadata

    @staticmethod
    def _sensor() -> dict:
        return {
            "rgb": [[[1, 2, 3], [4, 5, 6]]],
            "depth": [[1.0, float("inf")]],
            "intrinsics": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            "extrinsics": [
                [1.0, 0.0, 0.0, 0.0],
                [0.0, 1.0, 0.0, 0.0],
                [0.0, 0.0, 1.0, 0.0],
                [0.0, 0.0, 0.0, 1.0],
            ],
        }

    @classmethod
    def _write_episode(cls, root: Path, include_task: bool = True) -> None:
        recorder = EpisodeRecorder(root, cls._metadata(include_task))
        positions = ([0.0, 0.0, 0.9], [0.0, 0.0, 0.9], [0.0, 0.0, 1.0], [0.0, 0.0, 1.05])
        positions += ([0.0, 0.0, 1.1], [0.0, 0.0, 1.1], [0.0, 0.0, 1.0])
        positions += ([0.0, 0.0, 1.0], [0.0, 0.0, 1.0], [0.0, 0.0, 1.0])
        evaluator = TaskEvaluator(cls._task_spec(), {"mug_1": tuple(positions[0]), "island_1": (0.0, 0.0, 0.8)})
        for frame_id, (phase, position) in enumerate(zip(cls._PHASES, positions, strict=True)):
            contact = phase in {"GRASP", "VERIFY_GRASP", "LIFT", "TRANSFER", "LOWER"}
            gripper_open = phase in {"PRE_GRASP", "APPROACH", "RELEASE", "VERIFY_PLACE", "DONE"}
            evidence = {
                "finger_target_contact": contact,
                "gripper_open": gripper_open,
                "contact_report_available": True,
                "contact_report_subscribed": True,
                "contact_force_read_valid": True,
            }
            state = {"mug_1": {"position": list(position)}, "island_1": {"position": [0.0, 0.0, 0.8]}}
            oracle = evaluator.status(
                {key: tuple(value["position"]) for key, value in state.items()}, evidence
            )
            recorder.append(
                episode_id="episode-offline",
                frame_id=frame_id,
                sim_step=frame_id,
                timestamp=frame_id / 60.0,
                sensor=cls._sensor(),
                robot_state={"joint_positions": [0.0], "joint_velocities": [], "grasp_diagnostics": evidence},
                object_state=state,
                action={"phase": phase},
                oracle=oracle,
                phase=phase,
                contact_diagnostics=evidence,
                reward=1.0 if phase == "DONE" else 0.0,
                terminated=phase == "DONE",
                truncated=False,
            )
        final_oracle = evaluator.status(
            {"mug_1": tuple(positions[-1]), "island_1": (0.0, 0.0, 0.8)},
            {
                "finger_target_contact": False,
                "gripper_open": True,
                "contact_report_available": True,
                "contact_report_subscribed": True,
                "contact_force_read_valid": True,
            },
        )
        recorder.finalize(
            {
                "result": "passed",
                "task": "pick_and_place",
                "task_success": True,
                "task_oracle": final_oracle,
                "pick": "passed",
                "place": "passed",
                "released": True,
                "gripper_open": True,
                "finger_target_contact": False,
            }
        )

    @staticmethod
    def _read_json(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def test_inspect_validate_and_replay_complete_episode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "episode_000000"
            self._write_episode(root)
            inspected = inspect_episode(root)
            validated = validate_episode(root)
            replayed = replay_episode(root)
            self.assertTrue(inspected.valid)
            self.assertTrue(validated.valid)
            self.assertTrue(replayed.valid)
            self.assertEqual(validated.summary["frame_count"], 10)
            self.assertEqual(replayed.summary["task_replay"]["result"], "passed")
            self.assertEqual(replayed.summary["task_replay"]["frames_replayed"], 10)
            self.assertEqual(validate_episode(root).to_dict(), validated.to_dict())

    def test_replay_explicitly_reports_missing_task_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "episode_000000"
            self._write_episode(root, include_task=False)
            replayed = replay_episode(root)
            self.assertTrue(replayed.valid)
            self.assertEqual(replayed.summary["task_replay"]["result"], "not_available")

    def test_phase_and_terminal_contract_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "episode_000000"
            self._write_episode(root)
            records = [json.loads(line) for line in (root / "trajectory.jsonl").read_text().splitlines()]
            records[1]["phase"] = "LIFT"
            (root / "trajectory.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
            )
            report = validate_episode(root)
            self.assertFalse(report.valid)
            self.assertTrue(any("illegal phase transition" in error for error in report.errors))

            records[-1]["phase"] = "PRE_GRASP"
            (root / "trajectory.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
            )
            report = validate_episode(root)
            self.assertFalse(report.valid)
            self.assertTrue(any("successful result requires final phase DONE" in error for error in report.errors))

    def test_media_path_and_count_corruption_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "episode_000000"
            self._write_episode(root)
            records = [json.loads(line) for line in (root / "trajectory.jsonl").read_text().splitlines()]
            records[0]["rgb_path"] = "../../outside.png"
            (root / "trajectory.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
            )
            report = validate_episode(root)
            self.assertFalse(report.valid)
            self.assertTrue(any("escapes episode directory" in error for error in report.errors))

            records[0]["rgb_path"] = "rgb/000000.png"
            (root / "trajectory.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
            )
            (root / "depth" / "000009.npy").unlink()
            report = validate_episode(root)
            self.assertFalse(report.valid)
            self.assertTrue(any("media" in error for error in report.errors))

    def test_malformed_json_and_npy_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "episode_000000"
            self._write_episode(root)
            (root / "metadata.json").write_text("{malformed", encoding="utf-8")
            self.assertFalse(validate_episode(root).valid)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "episode_000000"
            self._write_episode(root)
            (root / "depth" / "000000.npy").write_bytes(b"\x93NUMPY\x01\x00\x00\x00")
            report = validate_episode(root)
            self.assertFalse(report.valid)
            self.assertTrue(any("depth" in error for error in report.errors))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "episode_000000"
            self._write_episode(root)
            _write_npy_float32(root / "depth" / "000000.npy", [[float("nan"), float("inf")]])
            report = validate_episode(root)
            self.assertFalse(report.valid)
            self.assertTrue(any("finite" in error for error in report.errors))

    def test_stale_passed_result_and_non_monotonic_timestamp_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "episode_000000"
            self._write_episode(root)
            result = self._read_json(root / "result.json")
            result["failure_reason"] = "timeout"
            self._write_json(root / "result.json", result)
            report = validate_episode(root)
            self.assertFalse(report.valid)
            self.assertTrue(any("failure_reason" in error for error in report.errors))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "episode_000000"
            self._write_episode(root)
            records = [json.loads(line) for line in (root / "trajectory.jsonl").read_text().splitlines()]
            records[4]["timestamp"] = records[3]["timestamp"]
            (root / "trajectory.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
            )
            report = validate_episode(root)
            self.assertFalse(report.valid)
            self.assertTrue(any("timestamp" in error for error in report.errors))

    def test_cli_episode_validate_returns_structured_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "episode_000000"
            self._write_episode(root)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["episode", "validate", str(root)])
            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["valid"])
            self.assertEqual(payload["operation"], "validate")

    def test_offline_episode_layer_has_no_simulator_or_numpy_imports(self) -> None:
        code = (
            "import sys; import scene_factory.trajectory; "
            "assert not any(name in sys.modules for name in "
            "('isaacsim', 'omni', 'pxr', 'carb', 'numpy'))"
        )
        completed = subprocess.run([sys.executable, "-c", code], check=False)
        self.assertEqual(completed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
