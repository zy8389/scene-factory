from __future__ import annotations

import unittest

from scene_factory.quality import assess_scene_quality, quality_rejection_codes


class SceneQualityTests(unittest.TestCase):
    def test_layout_pass_does_not_imply_physics_or_task_pass(self) -> None:
        quality = assess_scene_quality(
            {"scene_id": "example", "seed": 42},
            {"valid": True, "issues": [], "metrics": {"object_count": 2}},
        )
        self.assertEqual(quality["highest_verified_level"], "layout")
        self.assertEqual(quality["layers"]["physics"]["status"], "not_verified")
        self.assertEqual(quality["layers"]["task"]["status"], "not_verified")
        self.assertEqual(quality_rejection_codes(quality, "layout"), [])
        self.assertEqual(quality_rejection_codes(quality, "physics"), ["physics_not_verified"])
        self.assertEqual(
            quality_rejection_codes(quality, "task"),
            ["physics_not_verified", "task_not_verified"],
        )

    def test_failure_taxonomy_preserves_unknown_codes(self) -> None:
        categories = {
            "out_of_bounds_x": "bounds", "out_of_bounds_y": "bounds",
            "out_of_bounds_z": "bounds", "overlap": "overlap",
            "missing_support": "support", "missing_surface": "support",
            "outside_support_surface": "support", "floating_or_sunk": "support",
            "future_check_failed": "other",
        }
        for code, category in categories.items():
            with self.subTest(code=code):
                quality = assess_scene_quality(
                    {"scene_id": "example", "seed": 42},
                    {"valid": False, "metrics": {}, "issues": [
                        {"code": code, "message": "failed", "object_ids": ["cup"]},
                    ]},
                )
                self.assertEqual(quality["highest_verified_level"], "none")
                self.assertEqual(quality["layers"]["layout"]["issues"][0]["category"], category)
                self.assertEqual(quality_rejection_codes(quality, "layout"), [code])

    def test_invalid_report_without_error_reason_is_still_rejected(self) -> None:
        quality = assess_scene_quality(
            {"scene_id": "example", "seed": 42},
            {"valid": False, "issues": [], "metrics": {}},
        )
        self.assertEqual(quality_rejection_codes(quality, "layout"), ["layout_validation_failed"])

    def test_warnings_are_not_failures(self) -> None:
        quality = assess_scene_quality(
            {"scene_id": "example", "seed": 42},
            {"valid": True, "metrics": {}, "issues": [
                {"code": "proxy_asset", "message": "proxy", "severity": "warning"},
            ]},
        )
        self.assertEqual(quality_rejection_codes(quality, "layout"), [])
        with self.assertRaises(ValueError):
            quality_rejection_codes(quality, "training")

    def test_malformed_and_contradictory_evidence_is_not_accepted(self) -> None:
        invalid_reports = [
            {},
            {"valid": 1, "issues": [], "metrics": {}},
            {"valid": True, "issues": "none", "metrics": {}},
            {"valid": True, "issues": [], "metrics": {"count": float("nan")}},
            {"valid": True, "issues": [], "metrics": {"count": float("inf")}},
            {"valid": True, "issues": [None], "metrics": {}},
            {"valid": True, "issues": [{"code": "overlap", "message": "overlap"}], "metrics": {}},
            {"valid": False, "issues": [{"code": "bad", "message": "bad", "severity": "pass"}], "metrics": {}},
        ]
        for report in invalid_reports:
            with self.subTest(report=report), self.assertRaises(ValueError):
                assess_scene_quality({"scene_id": "example", "seed": 42}, report)


if __name__ == "__main__":
    unittest.main()
