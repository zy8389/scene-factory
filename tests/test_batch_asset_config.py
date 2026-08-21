from __future__ import annotations

import json
import unittest
from pathlib import Path

from scene_factory.batch_ingestion import validate_batch_config


class BatchAssetConfigTests(unittest.TestCase):
    def test_first_batch_config_is_complete_and_unique(self) -> None:
        config = json.loads(Path("configs/assets_batch.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_batch_config(config), [])
        asset_ids = [item["asset_id"] for item in config["assets"]]
        self.assertEqual(len(asset_ids), len(set(asset_ids)))
        self.assertEqual(
            set(asset_ids),
            {"mug_001", "bowl_001", "plate_001", "pot_001", "knife_001", "backpack_001", "keys_001"},
        )
        knife = next(item for item in config["assets"] if item["asset_id"] == "knife_001")
        self.assertEqual(knife["category"], "kitchen_knife")
        self.assertEqual(knife["validation_profile"], "drop_thin_object")


if __name__ == "__main__":
    unittest.main()
