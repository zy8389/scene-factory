from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scene_factory.asset_validator import validate_asset
from scene_factory.dataset import reproduce_dataset, validate_dataset
from scene_factory.intent import SceneIntent
from scene_factory.intent_compiler import IntentCompiler
from scene_factory.layout import LayoutSolver
from scene_factory.models import (
    AssetRecord,
    ObjectRequest,
    SceneRecipe,
)
from scene_factory.recipes import RecipeLibrary
from scene_factory.registry import AssetMetadata, AssetRegistry
from scene_factory.tasks import TaskEvaluator


class ArticulationFixture:
    def _raw_asset(self) -> dict:
        return {
            "asset_id": "drawer_asset",
            "name": "Test drawer",
            "category": "drawer",
            "bbox_m": [0.8, 0.5, 0.6],
            "mass": 12.0,
            "friction": 0.5,
            "status": "validated",
            "support_surface": {
                "name": "top",
                "center": [0.0, 0.0, 0.3],
                "size": [0.7, 0.4],
                "link": "cabinet_body",
            },
            "articulations": [
                {
                    "joint_id": "drawer_slide",
                    "joint_type": "prismatic",
                    "parent": "cabinet_body",
                    "child": "drawer_link",
                    "axis": [2.0, 0.0, 0.0],
                    "lower_limit": 0.0,
                    "upper_limit": 0.42,
                    "default_position": 0.0,
                },
                {
                    "joint_id": "door_hinge",
                    "joint_type": "revolute",
                    "parent": "cabinet_body",
                    "child": "door_link",
                    "axis": [0.0, 0.0, 3.0],
                    "lower_limit": -1.57,
                    "upper_limit": 1.57,
                    "default_position": 0.0,
                },
            ],
            "interaction_regions": [
                {
                    "region_id": "drawer_handle",
                    "kind": "handle",
                    "link": "drawer_link",
                    "center": [0.0, -0.28, 0.0],
                    "size": [0.18, 0.04, 0.04],
                    "approach_axis": [0.0, 2.0, 0.0],
                    "allowed_actions": ["grasp", "pull"],
                    "controlled_joint": "drawer_slide",
                }
            ],
            "interior_regions": [
                {
                    "region_id": "drawer_interior",
                    "link": "drawer_link",
                    "center": [0.0, 0.0, 0.0],
                    "size": [0.6, 0.3, 0.2],
                }
            ],
            "semantic_states": [
                {
                    "name": "closed",
                    "joint": "drawer_slide",
                    "range": [0.0, 0.02],
                },
                {
                    "name": "open",
                    "joint": "drawer_slide",
                    "range": [0.35, 0.42],
                    "target_position": 0.4,
                },
                {
                    "name": "door_closed",
                    "joint": "door_hinge",
                    "range": [-0.02, 0.02],
                },
            ],
        }

class ArticulationContractTests(ArticulationFixture, unittest.TestCase):

    def test_contract_round_trip_normalizes_axes_and_preserves_metadata(self) -> None:
        raw = self._raw_asset()
        record = AssetRecord.from_dict(raw)
        self.assertEqual(record.joints[0].axis, (1.0, 0.0, 0.0))
        self.assertEqual(record.joints[1].axis, (0.0, 0.0, 1.0))
        self.assertEqual(record.support_surface[0].link, "cabinet_body")
        metadata = AssetMetadata.from_dict(raw)
        reloaded = AssetMetadata.from_dict(metadata.to_dict())
        self.assertEqual(reloaded.articulations, metadata.articulations)
        self.assertEqual(reloaded.interaction_regions, metadata.interaction_regions)
        self.assertEqual(reloaded.interior_regions, metadata.interior_regions)
        self.assertEqual(reloaded.semantic_states, metadata.semantic_states)

    def test_asset_inspect_reports_articulation_summary(self) -> None:
        registry = AssetRegistry([AssetRecord.from_dict(self._raw_asset())])
        report = validate_asset("drawer_asset", registry)
        checks = report["checks"]["metadata"]
        self.assertTrue(checks["articulated"])
        self.assertEqual(checks["joint_count"], 2)
        self.assertEqual(checks["joint_ids"], ["drawer_slide", "door_hinge"])
        self.assertEqual(checks["joint_types"], ["prismatic", "revolute"])
        self.assertEqual(checks["interaction_region_count"], 1)
        self.assertEqual(checks["semantic_state_names"], ["closed", "open", "door_closed"])
        self.assertEqual(checks["interior_region_count"], 1)

    def test_legacy_asset_is_not_articulated(self) -> None:
        registry = AssetRegistry.load(Path("data/assets/registry.jsonl"))
        report = validate_asset("mug_001", registry)
        self.assertFalse(report["checks"]["metadata"]["articulated"])
        self.assertEqual(report["checks"]["metadata"]["joint_count"], 0)

    def test_invalid_contracts_are_rejected(self) -> None:
        mutations = {
            "duplicate joint ID": lambda raw: raw["articulations"].append(
                copy.deepcopy(raw["articulations"][0])
            ),
            "unknown joint type": lambda raw: raw["articulations"][0].update(
                joint_type="fixed"
            ),
            "zero axis": lambda raw: raw["articulations"][0].update(axis=[0, 0, 0]),
            "non-finite axis": lambda raw: raw["articulations"][0].update(axis=["nan", 0, 0]),
            "invalid limits": lambda raw: raw["articulations"][0].update(
                lower_limit=1.0, upper_limit=1.0
            ),
            "default outside limits": lambda raw: raw["articulations"][0].update(
                default_position=0.5
            ),
            "parent equals child": lambda raw: raw["articulations"][0].update(
                child="cabinet_body"
            ),
            "unknown support link": lambda raw: raw["support_surface"].update(
                link="missing_link"
            ),
            "graph cycle": lambda raw: raw["articulations"][1].update(
                parent="drawer_link", child="cabinet_body"
            ),
            "duplicate region": lambda raw: raw["interaction_regions"].append(
                copy.deepcopy(raw["interaction_regions"][0])
            ),
            "unknown controlled joint": lambda raw: raw["interaction_regions"][0].update(
                controlled_joint="missing_joint"
            ),
            "invalid action": lambda raw: raw["interaction_regions"][0].update(
                allowed_actions=["teleport"]
            ),
            "negative region size": lambda raw: raw["interaction_regions"][0].update(
                size=[0.1, -0.1, 0.1]
            ),
            "overlapping states": lambda raw: raw["semantic_states"][1].update(
                range=[0.01, 0.42]
            ),
            "state outside joint limits": lambda raw: raw["semantic_states"][1].update(
                range=[0.43, 0.44]
            ),
            "duplicate state": lambda raw: raw["semantic_states"].append(
                copy.deepcopy(raw["semantic_states"][0])
            ),
            "malformed interior": lambda raw: raw["interior_regions"][0].pop("size"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                raw = self._raw_asset()
                mutate(raw)
                with self.assertRaises(ValueError):
                    AssetRecord.from_dict(raw)

    def test_limits_and_region_bounds_reject_non_finite_values(self) -> None:
        raw = self._raw_asset()
        raw["articulations"][0]["upper_limit"] = float("inf")
        with self.assertRaises(ValueError):
            AssetRecord.from_dict(raw)
        raw = self._raw_asset()
        raw["interior_regions"][0]["center"] = [0.0, float("nan"), 0.0]
        with self.assertRaises(ValueError):
            AssetRecord.from_dict(raw)


class ArticulationSceneAndTaskTests(ArticulationFixture, unittest.TestCase):
    def _registry(self) -> AssetRegistry:
        return AssetRegistry([AssetRecord.from_dict(self._raw_asset())])

    def _recipe_library(self) -> RecipeLibrary:
        recipe = SceneRecipe(
            name="test_articulated",
            room_type="test_room",
            room_dimensions_m=(2.0, 2.0, 2.0),
            event="test_event",
            description="articulated test",
            keywords=(),
            objects=(),
        )
        return RecipeLibrary([recipe])

    def test_intent_state_compiles_to_initial_joint_position(self) -> None:
        registry = self._registry()
        intent = SceneIntent.from_dict(
            {
                "room_type": "test_room",
                "event": "test_event",
                "description": "open drawer",
                "objects": [
                    {
                        "object_id": "drawer_1",
                        "category": "drawer",
                        "dynamic": False,
                        "support_hint": None,
                        "attributes": [],
                        "state": ["open"],
                    }
                ],
                "relations": [],
                "room_dimensions_m": None,
                "clutter_level": 0.0,
                "layout_style": "casual",
            },
            allowed_categories=registry.categories(),
            allowed_room_types=["test_room"],
            allowed_events=["test_event"],
        )
        compiler = IntentCompiler(registry, self._recipe_library())
        recipe = compiler.compile(intent, intent.description)
        self.assertEqual(recipe.objects[0].state, ("open",))
        scene = LayoutSolver(registry).compile(recipe, 7)
        self.assertEqual(
            scene.objects[0].interactions["states"],
            [{"name": "open", "joint": "drawer_slide", "position": 0.4}],
        )
        self.assertEqual(scene.objects[0].interactions["joints"][0]["position"], 0.4)
        closed_recipe = SceneRecipe.from_dict(
            {
                **recipe.to_dict(),
                "objects": [
                    {
                        "object_id": "drawer_1",
                        "category": "drawer",
                        "support": "floor",
                        "dynamic": False,
                        "state": ["closed"],
                    }
                ],
            }
        )
        closed_scene = LayoutSolver(registry).compile(closed_recipe, 7)
        self.assertEqual(closed_scene.objects[0].interactions["joints"][0]["position"], 0.01)

    def test_unknown_intent_state_fails_closed(self) -> None:
        registry = self._registry()
        request = ObjectRequest(
            object_id="drawer_1",
            category="drawer",
            support="floor",
            dynamic=False,
            state=("missing",),
        )
        recipe = SceneRecipe(
            name="invalid_state",
            room_type="test_room",
            room_dimensions_m=(2.0, 2.0, 2.0),
            event="test_event",
            description="",
            keywords=(),
            objects=(request,),
        )
        with self.assertRaisesRegex(ValueError, "unknown semantic state"):
            LayoutSolver(registry).compile(recipe, 1)

    def test_task_evaluator_open_and_closed_state(self) -> None:
        open_task = {
            "success": {
                "predicate": "articulation_state",
                "object_id": "drawer_1",
                "state": "open",
                "joint": "drawer_slide",
                "range": [0.35, 0.42],
            }
        }
        evaluator = TaskEvaluator(open_task, {"drawer_1": (0.0, 0.0, 0.0)})
        self.assertTrue(
            evaluator.evaluate({"drawer_1": {"joints": {"drawer_slide": 0.4}}})
        )
        self.assertFalse(
            evaluator.evaluate({"drawer_1": {"joints": {"drawer_slide": 0.1}}})
        )
        closed_task = {
            "success": {
                "predicate": "articulation_state",
                "target_object": "drawer_1",
                "state": "closed",
                "state_ranges": {
                    "closed": {"joint": "drawer_slide", "range": [0.0, 0.02]}
                },
            }
        }
        self.assertTrue(
            TaskEvaluator(closed_task, {}).evaluate(
                {}, {"articulation_positions": {"drawer_1": {"drawer_slide": 0.01}}}
            )
        )
        with self.assertRaises(ValueError):
            TaskEvaluator(
                {
                    "success": {
                        "predicate": "articulation_state",
                        "object_id": "drawer_1",
                        "state": "open",
                        "joint": "drawer_slide",
                    }
                },
                {},
            )

    def test_existing_lift_evaluator_remains_unchanged(self) -> None:
        evaluator = TaskEvaluator(
            {"target_object": "mug_1", "success": {"predicate": "lifted"}},
            {"mug_1": (0.0, 0.0, 0.5)},
        )
        self.assertTrue(evaluator.evaluate({"mug_1": (0.0, 0.0, 0.61)}))

    def test_deterministic_articulated_dataset_validate_reproduce_and_resume(self) -> None:
        registry = self._registry()
        recipe = SceneRecipe(
            name="test_articulated",
            room_type="test_room",
            room_dimensions_m=(2.0, 2.0, 2.0),
            event="test_event",
            description="articulated dataset",
            keywords=(),
            objects=(
                ObjectRequest(
                    object_id="drawer_1",
                    category="drawer",
                    support="floor",
                    dynamic=False,
                    state=("open",),
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            factory = _FactoryForTest(registry, recipe)
            factory.build_batch(directory, count=3, seed_start=10, recipe_name=recipe.name)
            self.assertTrue(validate_dataset(directory).valid)
            with patch("scene_factory.factory.SceneFactory", return_value=factory):
                self.assertTrue(reproduce_dataset(directory).valid)
            resumed = factory.build_batch(
                directory, count=3, seed_start=10, recipe_name=recipe.name, resume=True
            )
            self.assertEqual(len(resumed), 3)
            self.assertTrue(validate_dataset(directory).valid)

    def test_initial_joint_state_is_in_dataset_fingerprint(self) -> None:
        registry = self._registry()
        recipe = SceneRecipe(
            name="test_articulated",
            room_type="test_room",
            room_dimensions_m=(2.0, 2.0, 2.0),
            event="test_event",
            description="articulated fingerprint",
            keywords=(),
            objects=(
                ObjectRequest(
                    object_id="drawer_1",
                    category="drawer",
                    support="floor",
                    dynamic=False,
                    state=("open",),
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            factory = _FactoryForTest(registry, recipe)
            factory.build_batch(directory, count=1, seed_start=33, recipe_name=recipe.name)
            row = json.loads((Path(directory) / "manifest.jsonl").read_text().splitlines()[0])
            layout_path = Path(directory) / row["files"]["layout"]
            layout = json.loads(layout_path.read_text())
            layout["objects"][0]["interactions"]["joints"][0]["position"] = 0.39
            layout_path.write_text(json.dumps(layout, sort_keys=True), encoding="utf-8")
            self.assertFalse(validate_dataset(directory).valid)

    def test_no_isaac_or_numpy_import_is_needed(self) -> None:
        self.assertNotIn("isaacsim", sys.modules)
        self.assertNotIn("numpy", sys.modules)


class _FactoryForTest:
    def __init__(self, registry: AssetRegistry, recipe: SceneRecipe) -> None:
        from scene_factory.factory import SceneFactory

        self._factory = object.__new__(SceneFactory)
        self._factory.registry = registry
        self._factory.recipes = RecipeLibrary([recipe])
        self._factory.layout_solver = LayoutSolver(registry)
        from scene_factory.validation import SceneValidator

        self._factory.validator = SceneValidator(registry)
        self._factory.intent_compiler = IntentCompiler(registry, self._factory.recipes)

    def __getattr__(self, name):
        return getattr(self._factory, name)


if __name__ == "__main__":
    with patch.dict(os.environ, {"SCENE_FACTORY_LLM_MODE": "off"}):
        unittest.main()
