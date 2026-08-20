from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scene_factory.registry import AssetLoader, AssetRegistry


class RealUsdAssetLoadingTests(unittest.TestCase):
    def test_physx_metadata_and_ready_status_are_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "mug.usda").write_text("#usda 1.0\n", encoding="utf-8")
            registry_path = root / "registry.jsonl"
            registry_path.write_text(
                json.dumps(
                    {
                        "asset_id": "mug_001",
                        "name": "Mug 001",
                        "category": "mug",
                        "usd_path": "mug.usda",
                        "mass": 0.3,
                        "friction": 0.4,
                        "static_friction": 0.5,
                        "dynamic_friction": 0.4,
                        "rigid_body": True,
                        "collision_enabled": False,
                        "collision_status": "not_provided",
                        "status": "ready",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            registry = AssetRegistry.load(registry_path)
            asset = registry.get("mug_001")
            self.assertEqual(asset.mass, 0.3)
            self.assertEqual(asset.static_friction, 0.5)
            self.assertEqual(asset.dynamic_friction, 0.4)
            self.assertFalse(asset.collision_enabled)
            self.assertEqual(registry.candidates("mug")[0].asset_id, "mug_001")
            self.assertEqual(AssetLoader(registry).load("mug_001").asset_id, "mug_001")

    def test_proxy_assets_keep_the_legacy_layout_contract(self) -> None:
        registry = AssetRegistry.load(Path("data/assets/registry.jsonl"))
        proxy = registry.get("mug_blue")
        self.assertEqual(proxy.source_type, "primitive")
        self.assertEqual(proxy.mass_kg, proxy.mass)
        self.assertIn(proxy, registry.candidates("mug"))


if __name__ == "__main__":
    unittest.main()
