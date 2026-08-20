from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scene_factory.asset_pipeline import CollisionProcessor


class CollisionPipelineTests(unittest.TestCase):
    def test_no_collision_is_reported_without_synthesizing_one(self) -> None:
        report = CollisionProcessor().process(None)
        self.assertTrue(report["valid"])
        self.assertEqual(report["collision_status"], "not_provided")
        self.assertFalse(report["collision_enabled"])
        self.assertFalse(report["generated"])

    def test_missing_authored_collision_fails(self) -> None:
        report = CollisionProcessor().process(
            "F:/missing/mug_collision.usda",
            collision_status="authored",
            collision_enabled=True,
        )
        self.assertFalse(report["valid"])
        self.assertEqual(report["issues"][0]["code"], "missing_collision_file")

    def test_existing_collision_path_can_be_attached_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collision = root / "mug_collision.usda"
            collision.write_text("#usda 1.0\n", encoding="utf-8")
            report_path = root / "collision_report.json"
            report = CollisionProcessor().process(
                collision,
                collision_status="provided",
                collision_enabled=True,
                report_path=report_path,
            )
            self.assertTrue(report["valid"])
            self.assertFalse(report["generated"])
            self.assertEqual(
                json.loads(report_path.read_text(encoding="utf-8"))["collision_status"],
                "provided",
            )


if __name__ == "__main__":
    unittest.main()
