from __future__ import annotations

import unittest

from scene_factory.controller_settings import (
    controller_reach_kwargs,
    normalize_controller_settings,
)
from scene_factory.robotics import MugLiftController


class ControllerSettingsTests(unittest.TestCase):
    def test_absent_settings_preserve_existing_defaults(self) -> None:
        expected = {
            "pre_grasp_position_tolerance_m": 0.04,
            "approach_position_tolerance_m": 0.025,
            "max_reach_steps": 180,
            "grasp_hold_closing_margin_m": 0.007,
            "arm_stiffness": 1000.0,
            "arm_damping": 200.0,
            "arm_max_force": 2000.0,
        }
        for raw in (None, {}):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_controller_settings(raw), expected)

    def test_partial_override_preserves_other_defaults_and_input(self) -> None:
        raw = {"arm_stiffness": 2000}
        settings = normalize_controller_settings(raw)
        self.assertEqual(settings["arm_stiffness"], 2000.0)
        self.assertIsInstance(settings["arm_stiffness"], float)
        self.assertEqual(settings["pre_grasp_position_tolerance_m"], 0.04)
        self.assertEqual(settings["arm_damping"], 200.0)
        self.assertEqual(settings["arm_max_force"], 2000.0)
        self.assertEqual(raw, {"arm_stiffness": 2000})
        settings["arm_damping"] = 400.0
        self.assertEqual(normalize_controller_settings(None)["arm_damping"], 200.0)

    def test_boundaries_and_complete_valid_configuration(self) -> None:
        raw = {
            "pre_grasp_position_tolerance_m": 0.01,
            "approach_position_tolerance_m": 0.01,
            "max_reach_steps": 20,
            "grasp_hold_closing_margin_m": 0,
            "arm_stiffness": 2000,
            "arm_damping": 300,
            "arm_max_force": 1000,
        }
        settings = normalize_controller_settings(raw)
        self.assertEqual(settings, raw)
        self.assertIsInstance(settings["max_reach_steps"], int)
        self.assertIsInstance(settings["grasp_hold_closing_margin_m"], float)

    def test_rejects_non_objects_and_unknown_settings(self) -> None:
        for raw in ("", "{}", [], 0, False, True, {"arm_stifness": 1000}, {1: 1000}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                normalize_controller_settings(raw)

    def test_rejects_non_numbers_and_nonfinite_values(self) -> None:
        numeric_keys = set(normalize_controller_settings(None)) - {"max_reach_steps"}
        invalid = (
            None,
            "0.04",
            True,
            False,
            [],
            {},
            complex(1, 0),
            float("nan"),
            float("inf"),
            float("-inf"),
            10**1000,
        )
        for key in numeric_keys:
            for value in invalid:
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, key):
                    normalize_controller_settings({key: value})

    def test_positive_settings_reject_zero_and_negative_values(self) -> None:
        positive_keys = (
            "pre_grasp_position_tolerance_m",
            "approach_position_tolerance_m",
            "arm_stiffness",
            "arm_damping",
            "arm_max_force",
        )
        for key in positive_keys:
            for value in (0, 0.0, -0.0, -1, -0.001):
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, key):
                    normalize_controller_settings({key: value})

    def test_closing_margin_rejects_negative_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "grasp_hold_closing_margin_m"):
            normalize_controller_settings({"grasp_hold_closing_margin_m": -0.001})
        self.assertEqual(
            normalize_controller_settings({"grasp_hold_closing_margin_m": 0})[
                "grasp_hold_closing_margin_m"
            ],
            0.0,
        )

    def test_reach_steps_require_integer_at_least_twenty(self) -> None:
        for value in (None, "180", True, False, 0, -1, 19, 19.9, 20.0, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "max_reach_steps"):
                normalize_controller_settings({"max_reach_steps": value})
        self.assertEqual(normalize_controller_settings({"max_reach_steps": 20})["max_reach_steps"], 20)

    def test_reach_kwargs_filter_gains_and_gripper_settings(self) -> None:
        settings = {
            "pre_grasp_position_tolerance_m": 0.03,
            "approach_position_tolerance_m": 0.02,
            "max_reach_steps": 240,
            "grasp_hold_closing_margin_m": 0,
            "arm_stiffness": 2000,
            "arm_damping": 300,
            "arm_max_force": 1500,
        }
        kwargs = controller_reach_kwargs(settings)
        self.assertEqual(
            kwargs,
            {
                "pre_grasp_position_tolerance_m": 0.03,
                "approach_position_tolerance_m": 0.02,
                "max_reach_steps": 240,
            },
        )
        # Constructing the pure state machine also guards against accidentally
        # passing backend-only gripper or drive options into its constructor.
        MugLiftController((0.5, 0.0, 0.9), **kwargs)

    def test_reach_kwargs_validate_filtered_out_settings(self) -> None:
        invalid = (
            {"arm_stiffness": 0},
            {"arm_damping": "200"},
            {"arm_max_force": float("inf")},
            {"grasp_hold_closing_margin_m": True},
            {"unknown": 1},
        )
        for settings in invalid:
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                controller_reach_kwargs(settings)
        self.assertEqual(
            controller_reach_kwargs(None),
            {
                "pre_grasp_position_tolerance_m": 0.04,
                "approach_position_tolerance_m": 0.025,
                "max_reach_steps": 180,
            },
        )


if __name__ == "__main__":
    unittest.main()
