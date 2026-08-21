from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scene_factory.factory import SceneFactory
from scene_factory.registry import AssetRegistry


class MugAssetReferenceTests(unittest.TestCase):
    def test_ready_mug_is_selected_by_kitchen_recipe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            usd = root / "mug_001.usd"
            usd.write_text("#usda 1.0\n", encoding="utf-8")
            registry_path = root / "registry.jsonl"
            records = json.loads(
                "[" + ",".join(Path("data/assets/registry.jsonl").read_text().splitlines()) + "]"
            )
            records.append(
                {
                    "asset_id": "mug_001",
                    "category": "mug",
                    "bbox_m": [0.09, 0.09, 0.11],
                    "usd_path": str(usd),
                    "source_type": "local_usd",
                    "collision_mode": "authored",
                    "collision_path": str(root / "mug_collision.usd"),
                    "collision_status": "validated",
                    "mass": 0.3,
                    "friction": 0.4,
                    "static_friction": 0.5,
                    "dynamic_friction": 0.4,
                    "collision_enabled": True,
                    "status": "ready",
                }
            )
            (root / "mug_collision.usd").write_text("#usda 1.0\n", encoding="utf-8")
            registry_path.write_text(
                "".join(json.dumps(item) + "\n" for item in records), encoding="utf-8"
            )
            factory = SceneFactory(registry_path=registry_path, recipes_dir="recipes")
            result = factory.build_from_recipe("kitchen_after_cooking", 9)
            mug = next(item for item in result.scene.objects if item.object_id == "mug_1")
            self.assertEqual(mug.asset_id, "mug_001")
            self.assertIsNone(mug.fallback_reason)


if __name__ == "__main__":
    unittest.main()
