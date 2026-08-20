from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scene_factory.asset_pipeline import AssetNormalizer


class AssetNormalizerTests(unittest.TestCase):
    def test_missing_usd_returns_report_without_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "usd" / "mug_001.usda"
            report = AssetNormalizer().normalize(
                root / "missing.usda",
                output,
                asset_id="mug_001",
                category="mug",
            )
            self.assertFalse(report["valid"])
            self.assertEqual(report["status"], "raw")
            self.assertFalse(output.exists())
            self.assertEqual(report["issues"][0]["code"], "missing_source_usd")

    def test_normalization_delegates_to_usd_wrapper_without_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.usda"
            output = root / "normalized.usda"
            source.write_text("#usda 1.0\n", encoding="utf-8")
            wrapped = {
                "valid": True,
                "asset_id": "mug_001",
                "category": "mug",
                "wrapped_bbox_m": [0.09, 0.09, 0.11],
                "up_axis": "Z",
                "meters_per_unit": 1.0,
                "counts": {"mesh_prims": 1, "material_prims": 1},
                "used_layers": [str(output)],
            }
            with patch("scene_factory.asset_pipeline.wrap_usd", return_value=wrapped) as mocked:
                report = AssetNormalizer().normalize(
                    source,
                    output,
                    asset_id="mug_001",
                    category="mug",
                )
            self.assertTrue(report["valid"])
            self.assertEqual(report["status"], "normalized")
            self.assertEqual(report["collision_status"], "not_provided")
            self.assertIsNone(report["collision_path"])
            self.assertEqual(mocked.call_args.kwargs["collision_mode"], "none")

    def test_metadata_template_is_explicitly_raw(self) -> None:
        template = AssetNormalizer.metadata_template(
            asset_id="mug_001",
            name="Mug 001",
            category="mug",
            usd_path="../usd/mug_001.usda",
            mass=0.3,
            static_friction=0.5,
            dynamic_friction=0.4,
        )
        self.assertEqual(template["status"], "raw")
        self.assertFalse(template["collision_enabled"])
        self.assertIsNone(template["collision_path"])


if __name__ == "__main__":
    unittest.main()
