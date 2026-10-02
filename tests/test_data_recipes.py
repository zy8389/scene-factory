from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scene_factory import SceneFactory, audit_dataset, reproduce_dataset


class DataRecipeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with patch.dict(os.environ, {"SCENE_FACTORY_LLM_MODE": "off"}):
            cls.factory = SceneFactory()

    def test_data_variants_have_distinct_valid_starts_and_preserve_task_fixtures(self) -> None:
        for base_name in ("kitchen_franka_mug_lift", "kitchen_franka_mug_pick_place"):
            base = self.factory.build_from_recipe(base_name, 42)
            fixtures = {item.object_id: item for item in base.scene.objects if item.object_id != "mug_1"}
            positions = set()
            for seed in range(1000, 1100):
                with self.subTest(recipe=base_name, seed=seed):
                    result = self.factory.build_from_recipe(f"{base_name}_data", seed)
                    self.assertTrue(result.valid, result.validation.to_dict())
                    self.assertEqual(result.scene.task, base.scene.task)
                    by_id = {item.object_id: item for item in result.scene.objects}
                    for object_id, fixture in fixtures.items():
                        self.assertEqual(by_id[object_id], fixture)
                    mug = by_id["mug_1"]
                    self.assertEqual(mug.asset_id, "mug_001")
                    self.assertEqual(mug.pose.yaw_deg, 0.0)
                    self.assertTrue(0.56 <= mug.pose.position[0] <= 0.64)
                    self.assertTrue(-0.08 <= mug.pose.position[1] <= 0.02)
                    if "target_region_xy" in result.scene.task["success"]:
                        self.assertLess(mug.pose.position[1], result.scene.task["success"]["target_region_xy"][2])
                    positions.add(mug.pose.position)
            self.assertEqual(len(positions), 100)

    def test_fixed_recipes_remain_fixed_and_data_recipes_reproduce_by_seed(self) -> None:
        for base_name in ("kitchen_franka_mug_lift", "kitchen_franka_mug_pick_place"):
            first = self.factory.build_from_recipe(base_name, 1000)
            second = self.factory.build_from_recipe(base_name, 1001)
            self.assertEqual(first.scene.objects, second.scene.objects)
            name = f"{base_name}_data"
            self.assertEqual(
                self.factory.build_from_recipe(name, 1010),
                self.factory.build_from_recipe(name, 1010),
            )

    def test_data_batch_reproduces_and_is_not_accepted_as_physical_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for base_name in ("kitchen_franka_mug_lift", "kitchen_franka_mug_pick_place"):
                root = Path(directory) / base_name
                self.factory.build_batch(root, count=20, seed_start=1000, recipe_name=f"{base_name}_data")
                report = audit_dataset(root, deduplicate=True)
                self.assertTrue(report.valid)
                self.assertEqual(report["summary"]["selected_count"], 20)
                self.assertEqual(report["diversity"]["all"]["unique_layout_count"], 20)
                self.assertTrue(reproduce_dataset(root).valid)
                self.assertFalse(audit_dataset(root, minimum_level="physics").valid)
                self.assertFalse(audit_dataset(root, minimum_level="task").valid)

    def test_general_prompts_still_choose_fixed_recipes(self) -> None:
        for prompt, expected in (
            ("franka mug lift", "kitchen_franka_mug_lift"),
            ("franka pick and place", "kitchen_franka_mug_pick_place"),
            ("做饭后的厨房", "kitchen_after_cooking"),
            ("抓杯数据", "kitchen_franka_mug_lift_data"),
            ("抓取放置数据", "kitchen_franka_mug_pick_place_data"),
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(self.factory.recipes.match_prompt(prompt).name, expected)


if __name__ == "__main__":
    unittest.main()
