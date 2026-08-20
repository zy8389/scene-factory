from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scene_factory.asset_pipeline import AssetNormalizer
from scene_factory.registry import AssetRegistry


class MugAssetTests(unittest.TestCase):
    def test_template_is_raw_and_does_not_claim_a_collision(self) -> None:
        template = json.loads(
            Path("data/assets/metadata/mug_001.template.json").read_text(encoding="utf-8")
        )
        self.assertEqual(template["asset_id"], "mug_001")
        self.assertEqual(template["status"], "raw")
        self.assertFalse(template["collision_enabled"])
        self.assertIsNone(template["collision_path"])

    def test_registry_promotes_mug_through_validated_to_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            usd = root / "mug_001.usda"
            collision = root / "mug_001_collision.usda"
            usd.write_text("#usda 1.0\n", encoding="utf-8")
            collision.write_text("#usda 1.0\n", encoding="utf-8")
            registry_path = root / "registry.jsonl"
            registry_path.write_text(
                json.dumps(
                    {
                        "asset_id": "mug_001",
                        "name": "Mug 001",
                        "category": "mug",
                        "bbox_m": [0.09, 0.09, 0.11],
                        "usd_path": usd.name,
                        "collision_path": collision.name,
                        "mass": 0.3,
                        "friction": 0.4,
                        "static_friction": 0.5,
                        "dynamic_friction": 0.4,
                        "collision_status": "authored",
                        "collision_enabled": True,
                        "status": "normalized",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            registry = AssetRegistry.load(registry_path)
            registry.promote_to_validated(
                "mug_001",
                {"asset_id": "mug_001", "valid": True},
                collision_report={
                    "valid": True,
                    "generated": False,
                    "collision_path": str(collision),
                    "collision_status": "validated",
                    "collision_enabled": True,
                },
            )
            self.assertEqual(registry.metadata("mug_001").status, "validated")
            registry.promote_to_ready(
                "mug_001",
                {
                    "asset_id": "mug_001",
                    "valid": True,
                    "usd_load": "passed",
                    "collision": "passed",
                    "physics": "passed",
                },
                persist_path=registry_path,
            )
            self.assertEqual(AssetRegistry.load(registry_path).get("mug_001").status, "ready")

    def test_normalizer_keeps_missing_real_asset_raw(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = AssetNormalizer().inspect(root / "mug_original.usd")
            self.assertFalse(report["valid"])
            self.assertEqual(report["issues"][0]["code"], "missing_source_usd")


if __name__ == "__main__":
    unittest.main()
