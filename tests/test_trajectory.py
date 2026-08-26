from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scene_factory.trajectory import DatasetError, EpisodeRecorder, load_episode


class TrajectoryDatasetTests(unittest.TestCase):
    @staticmethod
    def _metadata() -> dict:
        return {
            "schema_version": "scene_factory.trajectory.v1",
            "scene_id": "scene-1",
            "recipe": "recipe-1",
            "seed": 7,
            "robot_asset_source": "nucleus_franka_usd",
            "isaac_sim_version": "6.0.1",
            "camera": {
                "prim_path": "/World/Sensors/SceneFactoryRGBD",
                "resolution": [2, 1],
            },
            "intrinsics": None,
            "extrinsics": None,
            "control_frequency_hz": 60.0,
            "sensor_frequency_hz": 60.0,
        }

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

    def _append(
        self,
        recorder: EpisodeRecorder,
        frame_id: int = 0,
        sim_step: int = 0,
        phase: str = "PRE_GRASP",
        truncated: bool = False,
    ) -> None:
        recorder.append(
            episode_id="episode-1",
            frame_id=frame_id,
            sim_step=sim_step,
            timestamp=float(sim_step) / 60.0,
            sensor=self._sensor(),
            robot_state={"joint_positions": [0.0], "joint_velocities": [0.0]},
            object_state={"mug_1": {"position": [0.0, 0.0, 1.0]}},
            action={"phase": phase},
            oracle={"task_success": False},
            phase=phase,
            contact_diagnostics={"contact_force_read_valid": True},
            reward=0.0,
            terminated=False,
            truncated=truncated,
        )

    def test_round_trip_schema_and_media_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "episode_000000"
            recorder = EpisodeRecorder(root, self._metadata())
            self._append(recorder)
            recorder.finalize({"result": "passed", "task_success": True})
            episode = load_episode(root)
            self.assertEqual(len(episode), 1)
            self.assertEqual(episode[0]["rgb_path"], "rgb/000000.png")
            self.assertEqual(episode[0]["depth_path"], "depth/000000.npy")
            self.assertEqual(episode[0].read_depth()[0][0], 1.0)
            self.assertEqual(episode[0]["sim_step"], 0)

    def test_frame_and_step_ordering_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recorder = EpisodeRecorder(Path(directory) / "episode", self._metadata())
            self._append(recorder)
            with self.assertRaises(DatasetError):
                self._append(recorder, frame_id=2, sim_step=1)
            with self.assertRaises(DatasetError):
                self._append(recorder, frame_id=1, sim_step=0)
            recorder.finalize({"result": "passed"})
            result = json.loads((Path(directory) / "episode" / "result.json").read_text())
            self.assertEqual(result["result"], "failed")

    def test_missing_and_malformed_media_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "episode"
            recorder = EpisodeRecorder(root, self._metadata())
            self._append(recorder)
            recorder.finalize({"result": "passed"})
            (root / "depth" / "000000.npy").unlink()
            with self.assertRaises(DatasetError):
                load_episode(root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "episode"
            recorder = EpisodeRecorder(root, self._metadata())
            self._append(recorder)
            recorder.finalize({"result": "passed"})
            png = bytearray((root / "rgb" / "000000.png").read_bytes())
            png[-1] ^= 0x01
            (root / "rgb" / "000000.png").write_bytes(png)
            with self.assertRaises(DatasetError):
                load_episode(root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "episode"
            recorder = EpisodeRecorder(root, self._metadata())
            self._append(recorder)
            recorder.finalize({"result": "passed"})
            (root / "depth" / "000000.npy").write_bytes(b"\x93NUMPY")
            with self.assertRaises(DatasetError):
                load_episode(root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "episode"
            recorder = EpisodeRecorder(root, self._metadata())
            self._append(recorder)
            recorder.finalize({"result": "passed"})
            (root / "rgb" / "000000.png").write_bytes(b"not png")
            with self.assertRaises(DatasetError):
                load_episode(root)

    def test_mismatched_media_count_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "episode"
            recorder = EpisodeRecorder(root, self._metadata())
            self._append(recorder)
            self._append(recorder, frame_id=1, sim_step=1)
            recorder.finalize({"result": "passed"})
            (root / "rgb" / "000001.png").unlink()
            with self.assertRaises(DatasetError):
                load_episode(root)

    def test_sensor_failure_cannot_finalize_as_passed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "episode"
            recorder = EpisodeRecorder(root, self._metadata())
            with self.assertRaises(DatasetError):
                recorder.append(
                    episode_id="episode-1",
                    frame_id=0,
                    sim_step=0,
                    timestamp=0.0,
                    sensor={"status": "error", "error": "render failed"},
                    robot_state={},
                    object_state={},
                    action=None,
                    oracle={},
                    phase="PRE_GRASP",
                    contact_diagnostics={},
                    reward=0.0,
                    terminated=False,
                    truncated=False,
                )
            result = recorder.finalize({"result": "passed"})
            self.assertEqual(result["result"], "failed")
            self.assertEqual(result["trajectory_export"]["result"], "failed")

    def test_failed_or_truncated_frame_cannot_finalize_as_passed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "episode"
            recorder = EpisodeRecorder(root, self._metadata())
            self._append(recorder, phase="FAILED", truncated=True)
            result = recorder.finalize({"result": "passed"})
            self.assertEqual(result["result"], "failed")
            self.assertEqual(result["failure_reason"], "failed_or_truncated_frame")
            with self.assertRaises(DatasetError):
                load_episode(root)

    def test_sensor_step_must_match_trajectory_step(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recorder = EpisodeRecorder(Path(directory) / "episode", self._metadata())
            sensor = self._sensor()
            sensor["simulation_step"] = 1
            with self.assertRaises(DatasetError):
                recorder.append(
                    episode_id="episode-1",
                    frame_id=0,
                    sim_step=0,
                    timestamp=0.0,
                    sensor=sensor,
                    robot_state={},
                    object_state={},
                    action=None,
                    oracle={},
                    phase="PRE_GRASP",
                    contact_diagnostics={},
                    reward=0.0,
                    terminated=False,
                    truncated=False,
                )
            result = recorder.finalize({"result": "passed"})
            self.assertEqual(result["result"], "failed")

    def test_record_scalar_types_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recorder = EpisodeRecorder(Path(directory) / "episode", self._metadata())
            with self.assertRaises(DatasetError):
                self._append(recorder, frame_id=False)
            recorder.finalize({"result": "passed"})

        with tempfile.TemporaryDirectory() as directory:
            recorder = EpisodeRecorder(Path(directory) / "episode", self._metadata())
            with self.assertRaises(DatasetError):
                recorder.append(
                    episode_id="episode-1",
                    frame_id=0,
                    sim_step=0,
                    timestamp="invalid",
                    sensor=self._sensor(),
                    robot_state={},
                    object_state={},
                    action=None,
                    oracle={},
                    phase="PRE_GRASP",
                    contact_diagnostics={},
                    reward=0.0,
                    terminated=False,
                    truncated=False,
                )
            recorder.finalize({"result": "passed"})

    def test_malformed_sensor_record_and_timestamp_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "episode"
            recorder = EpisodeRecorder(root, self._metadata())
            self._append(recorder)
            recorder.finalize({"result": "passed"})
            record_path = root / "trajectory.jsonl"
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["sensor"] = "invalid"
            record_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            with self.assertRaises(DatasetError):
                load_episode(root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "episode"
            recorder = EpisodeRecorder(root, self._metadata())
            self._append(recorder)
            recorder.finalize({"result": "passed"})
            record_path = root / "trajectory.jsonl"
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["timestamp"] = "nan"
            record_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            with self.assertRaises(DatasetError):
                load_episode(root)

    def test_trajectory_module_has_no_isaac_or_numpy_import_dependency(self) -> None:
        code = (
            "import sys; import scene_factory.trajectory; "
            "assert 'isaacsim' not in sys.modules; assert 'numpy' not in sys.modules"
        )
        completed = subprocess.run([sys.executable, "-c", code], check=False)
        self.assertEqual(completed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
