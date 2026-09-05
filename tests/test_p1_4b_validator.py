from __future__ import annotations

import json
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_VALIDATOR_PATH = Path(__file__).resolve().parents[1] / "tools" / "validate_p1_4b_executor.py"
_VALIDATOR_SPEC = importlib.util.spec_from_file_location(
    "scene_factory_p1_4b_validator",
    _VALIDATOR_PATH,
)
if _VALIDATOR_SPEC is None or _VALIDATOR_SPEC.loader is None:
    raise RuntimeError(f"cannot load validator from {_VALIDATOR_PATH}")
validator = importlib.util.module_from_spec(_VALIDATOR_SPEC)
_VALIDATOR_SPEC.loader.exec_module(validator)


class P14BValidatorTests(unittest.TestCase):
    def test_runtime_motion_only_dispatches_fixture_pose(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "runtime.json"
            with patch.object(
                validator,
                "_run_motion_only_child",
                return_value=0,
            ) as run_motion:
                result = validator.main(
                    [
                        "--report",
                        str(report_path),
                        "--runtime-only",
                        "--motion-only",
                        "--fixture-base-x-m",
                        "0.725",
                        "--fixture-base-y-m",
                        "-0.04",
                        "--fixture-base-z-m",
                        "0.83",
                        "--fixture-base-yaw-deg",
                        "5",
                        "--orientation-angle-deg",
                        "15",
                    ]
                )

        self.assertEqual(result, 0)
        run_motion.assert_called_once_with(
            report_path.resolve(),
            expected_head=None,
            distance_mm=None,
            fixture_base_position_m=(0.725, -0.04, 0.83),
            fixture_base_yaw_deg=5.0,
            orientation_angle_deg=15,
        )

    def test_motion_only_parent_forwards_fixture_to_both_fresh_children(self) -> None:
        base = [0.725, -0.04, 0.83]

        def child(*args, **kwargs):  # type: ignore[no-untyped-def]
            del args
            return {
                "result": "passed",
                "git_head": "test-head",
                "target_grasp_position": [0.34, -0.01, 1.10],
                "orientation_wxyz": [1.0, 0.0, 0.0, 0.0],
                "runtime": {
                    "fixture_robot_base_position_m": base,
                    "fixture_robot_base_yaw_deg": kwargs["fixture_base_yaw_deg"],
                },
                "checks": {"gripper_remained_open": True},
                "contact_acceptance_run": False,
                "drawer_pull_run": False,
            }

        with tempfile.TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "parent.json"
            with (
                patch.object(validator, "_git_head", return_value="test-head"),
                patch.object(validator, "_spawn_child", side_effect=child) as spawn,
            ):
                result = validator.main(
                    [
                        "--report",
                        str(report_path),
                        "--isaac-python",
                        sys.executable,
                        "--motion-only",
                        "--fixture-base-x-m",
                        "0.725",
                        "--fixture-base-y-m",
                        "-0.04",
                        "--fixture-base-z-m",
                        "0.83",
                        "--fixture-base-yaw-deg",
                        "5",
                        "--orientation-angle-deg",
                        "15",
                    ]
                )
            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(report["result"], "passed")
        self.assertEqual(spawn.call_count, 2)
        for call in spawn.call_args_list:
            self.assertEqual(call.kwargs["fixture_base_x_m"], 0.725)
            self.assertEqual(call.kwargs["fixture_base_y_m"], -0.04)
            self.assertEqual(call.kwargs["fixture_base_z_m"], 0.83)
            self.assertEqual(call.kwargs["fixture_base_yaw_deg"], 5.0)


if __name__ == "__main__":
    unittest.main()
