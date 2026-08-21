from __future__ import annotations

import unittest
from pathlib import Path

from scene_factory.registry import AssetRegistry


class RealAssetRegistryStateTests(unittest.TestCase):
    def test_ycb_mug_is_not_claimed_ready_without_real_source(self) -> None:
        registry = AssetRegistry.load(Path("data/assets/registry.jsonl"))
        with self.assertRaises(KeyError):
            registry.get("mug_001")
        self.assertEqual(registry.metadata("mug_blue").status, "validated")

    def test_raw_asset_cannot_skip_to_ready(self) -> None:
        with self.assertRaises(ValueError):
            AssetRegistry.load(Path("data/assets/registry.jsonl")).promote_to_ready(
                "mug_blue",
                {
                    "asset_id": "mug_blue",
                    "valid": True,
                    "usd_load": "passed",
                    "mesh_check": "passed",
                    "collision": "passed",
                    "physics": "passed",
                },
            )


if __name__ == "__main__":
    unittest.main()
