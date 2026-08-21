from __future__ import annotations

import unittest

from scene_factory.factory import SceneFactory


class SceneRealAssetMappingTests(unittest.TestCase):
    def test_kitchen_declares_real_assets_and_fallbacks(self) -> None:
        result = SceneFactory().build_from_recipe("kitchen_after_cooking", 42)
        objects = {item.object_id: item for item in result.scene.objects}
        self.assertEqual(objects["mug_1"].asset_id, "mug_001")
        self.assertEqual(objects["bowl_1"].asset_id, "bowl_001")
        self.assertIsNone(objects["bowl_1"].fallback_reason)
        self.assertEqual(objects["plate_1"].asset_id, "plate_001")
        self.assertIsNone(objects["plate_1"].fallback_reason)
        self.assertEqual(objects["knife_1"].asset_id, "knife_001")
        self.assertIsNone(objects["knife_1"].fallback_reason)
        self.assertEqual(objects["pot_1"].asset_id, "pot_basic")

    def test_living_room_declares_real_entry_assets(self) -> None:
        result = SceneFactory().build_from_recipe("living_room_returned_home", 42)
        objects = {item.object_id: item for item in result.scene.objects}
        self.assertEqual(objects["backpack_1"].asset_id, "backpack_gray")
        self.assertIn("backpack_001", objects["backpack_1"].fallback_reason or "")
        self.assertEqual(objects["keys_1"].asset_id, "keyring_metal")
        self.assertIn("keys_001", objects["keys_1"].fallback_reason or "")


if __name__ == "__main__":
    unittest.main()
