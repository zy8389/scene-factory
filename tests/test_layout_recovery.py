from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scene_factory import SceneFactory, reproduce_dataset, validate_dataset
from scene_factory.layout import LayoutError, LayoutSolver
from scene_factory.models import AssetRecord, ObjectRequest, Pose, SceneRecipe, SupportSurface
from scene_factory.registry import AssetRegistry
from scene_factory.validation import SceneValidator


class LayoutRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with patch.dict(os.environ, {"SCENE_FACTORY_LLM_MODE": "off"}):
            cls.factory = SceneFactory()

    def test_crowded_kitchen_recovers_without_changing_assets_or_fixtures(self) -> None:
        recipe = self.factory.recipes.get("kitchen_after_cooking")
        original_solver = LayoutSolver(self.factory.registry, max_layout_attempts=1)
        with self.assertRaises(LayoutError):
            original_solver.compile(recipe, 1010)
        with patch.object(
            self.factory.registry, "resolve_with_fallback",
            wraps=self.factory.registry.resolve_with_fallback,
        ) as resolve:
            scene = self.factory.layout_solver.compile(recipe, 1010)
        self.assertEqual(resolve.call_count, len(recipe.objects))
        self.assertTrue(self.factory.validator.validate(scene).valid)
        by_id = {item.object_id: item for item in scene.objects}
        for request in recipe.objects:
            if request.fixed_pose is not None:
                self.assertEqual(by_id[request.object_id].pose, request.fixed_pose)
        self.assertEqual(scene, self.factory.layout_solver.compile(recipe, 1010))

    def test_one_hundred_kitchen_seeds_are_valid(self) -> None:
        for seed in range(1000, 1100):
            with self.subTest(seed=seed):
                result = self.factory.build_from_recipe("kitchen_after_cooking", seed)
                self.assertTrue(result.valid, result.validation.to_dict())

    def test_incomplete_kitchen_batch_resumes_preserving_committed_scene(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            arguments = {
                "output_root": directory, "count": 2, "seed_start": 1009,
                "recipe_name": "kitchen_after_cooking",
            }
            with patch.object(self.factory.layout_solver, "max_layout_attempts", 1):
                with self.assertRaises(LayoutError):
                    self.factory.build_batch(**arguments)
            root = Path(directory)
            original_record = json.loads((root / "manifest.jsonl").read_text("utf-8"))
            scene_mtime = (root / original_record["files"]["layout"]).stat().st_mtime_ns
            resumed = self.factory.build_batch(**arguments, resume=True)
            self.assertEqual(resumed[0], original_record)
            self.assertEqual((root / resumed[0]["files"]["layout"]).stat().st_mtime_ns, scene_mtime)
            self.assertEqual([record["seed"] for record in resumed], [1009, 1010])
            self.assertTrue(validate_dataset(root).valid)
            self.assertTrue(reproduce_dataset(root).valid)

    @staticmethod
    def _small_scene(region: tuple[float, float, float, float]) -> tuple[SceneRecipe, AssetRegistry]:
        table = AssetRecord(
            asset_id="table", category="table", bbox_m=(1.0, 1.0, 0.5),
            support_surfaces=(SupportSurface("top", (0.0, 0.0, 0.25), (0.8, 0.8)),),
        )
        item = AssetRecord(asset_id="item", category="item", bbox_m=(0.06, 0.06, 0.08))
        registry = AssetRegistry([table, item])
        recipe = SceneRecipe(
            name="narrow-region", room_type="test", room_dimensions_m=(3.0, 3.0, 3.0),
            event="test", description="", keywords=(), objects=(
                ObjectRequest("table", "table", asset_id="table", dynamic=False,
                              fixed_pose=Pose((0.0, 0.0, 0.25), 45.0)),
                ObjectRequest("item", "item", asset_id="item", support="table:top",
                              region_xy=region, yaw_range_deg=(0.0, 0.0)),
            ),
        )
        return recipe, registry

    def test_narrow_region_on_rotated_support_is_sampled_without_leaving_bounds(self) -> None:
        region = (0.08, 0.0801, 0.09, 0.0901)
        recipe, registry = self._small_scene(region)
        scene = LayoutSolver(registry, max_attempts=32, max_layout_attempts=2).compile(recipe, 7)
        subject = next(item for item in scene.objects if item.object_id == "item")
        self.assertTrue(region[0] <= subject.pose.position[0] <= region[1])
        self.assertTrue(region[2] <= subject.pose.position[1] <= region[3])
        self.assertTrue(SceneValidator(registry).validate(scene).valid)

    def test_impossible_region_has_bounded_retries_and_no_successful_scene(self) -> None:
        recipe, registry = self._small_scene((10.0, 10.1, 10.0, 10.1))
        solver = LayoutSolver(registry, max_attempts=4, max_layout_attempts=3)
        with patch.object(solver, "_place_objects", wraps=solver._place_objects) as placements:
            with self.assertRaisesRegex(LayoutError, "exhausted 3 layout attempts"):
                solver.compile(recipe, 7)
        self.assertEqual(placements.call_count, 3)

    def test_permanent_fixed_collision_is_not_retried(self) -> None:
        registry = AssetRegistry([AssetRecord("block", "block", bbox_m=(0.2, 0.2, 0.2))])
        recipe = SceneRecipe(
            "fixed-collision", "test", (3.0, 3.0, 3.0), "test", "", (), (
                ObjectRequest("first", "block", fixed_pose=Pose((0.0, 0.0, 0.1))),
                ObjectRequest("second", "block", fixed_pose=Pose((0.0, 0.0, 0.1))),
            ),
        )
        solver = LayoutSolver(registry)
        with patch.object(solver, "_place_objects", wraps=solver._place_objects) as placements:
            with self.assertRaisesRegex(LayoutError, "fixed object second collides"):
                solver.compile(recipe, 7)
        self.assertEqual(placements.call_count, 1)

    def test_retry_budgets_must_be_positive_integers(self) -> None:
        for name in ("max_attempts", "max_layout_attempts"):
            for value in (0, -1, True, 1.5):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    LayoutSolver(self.factory.registry, **{name: value})


if __name__ == "__main__":
    unittest.main()
