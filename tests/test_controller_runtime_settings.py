from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest

from scene_factory.factory import SceneFactory


class ControllerRecipeTests(unittest.TestCase):
    def test_tracking_recipes_keep_the_baseline_layout_and_task_goal(self) -> None:
        factory = SceneFactory()
        for event in ("mug_lift", "mug_pick_place"):
            for seed in (1000, 1004, 2011):
                with self.subTest(event=event, seed=seed):
                    baseline = factory.build_from_recipe(f"kitchen_franka_{event}_data", seed).scene
                    tracking = factory.build_from_recipe(f"kitchen_franka_{event}_tracking_data", seed).scene
                    self.assertEqual(baseline.objects, tracking.objects)
                    self.assertEqual(baseline.task["success"], tracking.task["success"])
                    self.assertNotIn("controller", baseline.task)
                    self.assertEqual(tracking.task["controller"], {
                        "arm_stiffness": 2000.0, "grasp_hold_closing_margin_m": 0.012,
                    })


@unittest.skipUnless(importlib.util.find_spec("numpy"), "runtime drive integration requires NumPy")
class ActiveDriveSettingsTests(unittest.TestCase):
    def _run_drive_check(self, *, ignore_arm_setting: bool = False) -> None:
        # Keep optional runtime imports out of the parent core test process.
        script = '''
import sys
import numpy as np
from types import SimpleNamespace
from scene_factory.backends.isaac import _configure_franka_runtime_drives
ignore_arm_setting = sys.argv[1] == "ignore"
if True:
        class View:
            def __init__(self):
                self.kps = np.full((1, 9), 1000.0)
                self.kds = np.full((1, 9), 200.0)
                self.forces = np.full((1, 9), 2000.0)

            def set_gains(self, *, kps, kds, joint_indices):
                if not (ignore_arm_setting and len(joint_indices) == 7):
                    self.kps[:, joint_indices] = kps
                    self.kds[:, joint_indices] = kds

            def set_max_efforts(self, *, values, joint_indices):
                self.forces[:, joint_indices] = values

            def get_gains(self, *, joint_indices):
                return self.kps[:, joint_indices], self.kds[:, joint_indices]

            def get_max_efforts(self, *, joint_indices):
                return self.forces[:, joint_indices]

        view = View()
        if ignore_arm_setting:
            try:
                _configure_franka_runtime_drives(SimpleNamespace(_articulation_view=view), [7, 8], {"arm_stiffness": 2000.0})
            except RuntimeError as error:
                assert "differ from requested" in str(error)
            else:
                raise AssertionError("ignored gain update was accepted")
        else:
            report = _configure_franka_runtime_drives(SimpleNamespace(_articulation_view=view), [7, 8], {"arm_stiffness": 2000.0})
            assert report["verified"]
            assert report["arm"]["stiffness"] == [2000.0] * 7
            assert report["fingers"]["stiffness"] == [1000.0, 1000.0]
'''
        result = subprocess.run(
            [sys.executable, "-c", script, "ignore" if ignore_arm_setting else "apply"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_configures_arm_and_reads_back_without_changing_finger_gains(self) -> None:
        self._run_drive_check()

    def test_does_not_accept_an_ignored_runtime_gain_update(self) -> None:
        self._run_drive_check(ignore_arm_setting=True)


if __name__ == "__main__":
    unittest.main()
