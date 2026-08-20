from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scene_factory.asset_pipeline import AssetNormalizer, CollisionProcessor
from tools.validate_mug_asset import main


class UsdValidationTests(unittest.TestCase):
    def test_missing_usd_report_is_structured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "qa" / "mug_001.json"
            self.assertEqual(
                main([str(root / "missing.usda"), "--report", str(report_path)]),
                2,
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["usd_load"], "failed")
            self.assertEqual(report["issues"][0]["code"], "missing_usd")
            self.assertFalse(report["collision_generated"])

    def test_missing_collision_never_falls_back_to_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            usd = root / "mug_001.usda"
            usd.write_text("#usda 1.0\n", encoding="utf-8")
            report_path = root / "mug_001.json"
            self.assertEqual(main([str(usd), "--report", str(report_path)]), 2)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["issues"][0]["code"], "missing_collision")
            self.assertIsNone(report["collision_usd"])

    def test_collision_processor_reports_authored_path_without_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            collision = Path(directory) / "mug_collision.usda"
            collision.write_text("#usda 1.0\n", encoding="utf-8")
            report = CollisionProcessor().process(
                collision,
                collision_status="authored",
                collision_enabled=True,
            )
            self.assertTrue(report["valid"])
            self.assertEqual(report["collision_status"], "authored")
            self.assertFalse(report["generated"])

    def test_normalizer_does_not_write_output_without_pxr_or_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "usd" / "mug_001.usda"
            report = AssetNormalizer().normalize(
                root / "source.usda",
                output,
                asset_id="mug_001",
                category="mug",
            )
            self.assertFalse(report["valid"])
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
