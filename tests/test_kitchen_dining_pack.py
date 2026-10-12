from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
import unittest
import xml.etree.ElementTree as ET
from dataclasses import replace
from unittest.mock import patch

from scene_factory import SceneFactory
from scene_factory.asset_geometry import load_asset_parts
from scene_factory.exporters.mujoco_mjcf import MujocoMjcfExporter
from scene_factory.intent import IntentObject, IntentRelation, SceneIntent
from scene_factory.layout import LayoutError
from scene_factory.models import ClearanceZone, CompiledScene, ObjectRequest, Pose
from scene_factory.webapp import SceneWebApplication


class KitchenDiningPackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, {"SCENE_FACTORY_LLM_MODE": "off"}):
            cls.factory = SceneFactory()
        cls.assets = [a for a in cls.factory.registry.list() if "kitchen_dining_pack" in a.tags]

    def test_assets_have_bound_real_qa_and_matching_geometry(self):
        self.assertEqual(len(self.assets), 11)
        for asset in self.assets:
            with self.subTest(asset=asset.asset_id):
                self.assertEqual(asset.status, "ready")
                self.assertEqual(asset.collision_status, "validated")
                report = json.loads((self.factory.registry.base_dir / asset.qa_report).read_text())
                self.assertTrue(report["valid"])
                self.assertEqual(report["grasp"], "not_run")
                self.assertTrue(all(case["passed"] for case in report["physics_cases"]))
                folder = self.factory.registry.base_dir / "source" / asset.asset_id
                for key, path in (
                    ("usd_sha256", self.factory.registry.base_dir / asset.source_path),
                    ("collision_sha256", self.factory.registry.base_dir / asset.collision_path),
                    ("glb_sha256", folder / "visual.glb"),
                ):
                    self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), report[key])
                data = (folder / "visual.glb").read_bytes()
                self.assertEqual(struct.unpack_from("<I", data)[0], 0x46546C67)
                gltf = json.loads(data[20 : 20 + struct.unpack_from("<I", data, 12)[0]])
                geometry = load_asset_parts(self.factory.registry, asset)
                self.assertEqual(len(gltf["meshes"]), len(geometry))
                for part, mesh in zip(geometry, gltf["meshes"]):
                    acc = gltf["accessors"][mesh["primitives"][0]["attributes"]["POSITION"]]
                    for axis in range(3):
                        self.assertAlmostEqual(
                            acc["min"][axis], min(v[axis] for v in part["vertices"])
                        )
                        self.assertAlmostEqual(
                            acc["max"][axis], max(v[axis] for v in part["vertices"])
                        )

    def test_three_recipes_are_valid_and_deterministic_across_100_seeds(self):
        for name in ("kitchen_core_assets", "kitchen_storage_assets", "dining_core_assets"):
            for seed in range(100):
                with self.subTest(recipe=name, seed=seed):
                    result = self.factory.build_from_recipe(name, seed)
                    self.assertTrue(result.valid, result.validation.to_dict())
                    self.assertTrue(all(o.asset_id.startswith("kd_") for o in result.scene.objects))
            self.assertEqual(result.scene, self.factory.build_from_recipe(name, 99).scene)
            self.assertEqual(CompiledScene.from_dict(result.scene.to_dict()), result.scene)

    def test_storage_rejects_over_height_and_wrong_containment(self):
        result = self.factory.build_from_recipe("kitchen_storage_assets", 42)
        box = next(o for o in result.scene.objects if o.object_id == "box_1")
        too_tall = replace(
            box,
            bbox_m=(0.18, 0.14, 0.8),
            pose=replace(box.pose, position=(*box.pose.position[:2], 0.425)),
        )
        scene = replace(
            result.scene,
            objects=tuple(too_tall if o.object_id == "box_1" else o for o in result.scene.objects),
        )
        codes = {i.code for i in self.factory.validator.validate(scene).issues}
        self.assertIn("outside_storage_interior", codes)
        recipe = replace(
            result.recipe,
            objects=tuple(
                replace(
                    r,
                    asset_id="kd_bottle_01",
                    category="bottle",
                    fixed_pose=Pose((*box.pose.position[:2], 1.79)),
                )
                if r.object_id == "box_1"
                else r
                for r in result.recipe.objects
            ),
        )
        with self.assertRaisesRegex(LayoutError, "storage interior"):
            self.factory.layout_solver.compile(recipe, 42)

    def test_corridor_violation_is_detected_and_generation_rejects_it(self):
        result = self.factory.build_from_recipe("dining_core_assets", 42)
        box = next(o for o in result.scene.objects if o.object_id == "box_1")
        blocked = replace(box, pose=Pose((2.35, 0, 0.06)), support="floor", relations=())
        scene = replace(
            result.scene,
            objects=tuple(blocked if o.object_id == "box_1" else o for o in result.scene.objects),
        )
        self.assertIn(
            "blocked_clearance", {i.code for i in self.factory.validator.validate(scene).issues}
        )
        recipe = replace(
            result.recipe,
            objects=result.recipe.objects
            + (
                ObjectRequest(
                    "blocker", "box", "kd_box_01", support="floor", fixed_pose=Pose((2.35, 0, 0.06))
                ),
            ),
        )
        with self.assertRaisesRegex(LayoutError, "corridor"):
            self.factory.layout_solver.compile(recipe, 42)

    def test_chair_around_constraint_is_independently_checked(self):
        result = self.factory.build_from_recipe("dining_core_assets", 42)
        chair = next(o for o in result.scene.objects if o.object_id == "chair_1")
        distant = replace(chair, pose=Pose((0, -2.2, 0.43)))
        scene = replace(
            result.scene,
            objects=tuple(distant if o.object_id == "chair_1" else o for o in result.scene.objects),
        )
        self.assertIn(
            "invalid_around_distance",
            {i.code for i in self.factory.validator.validate(scene).issues},
        )

    def test_intent_inside_respects_named_shelf_and_on_uses_top(self):
        for predicate, hint, expected in (
            ("inside", "cab:shelf_2", "cabinet_1:shelf_2"),
            ("inside", None, "cabinet_1:shelf_0"),
            ("on", None, "cabinet_1:top"),
        ):
            intent = SceneIntent(
                "kitchen",
                "kitchen_storage",
                "",
                (
                    IntentObject("cab", "kitchen_cabinet", False),
                    IntentObject("parcel", "box", True, hint),
                ),
                (IntentRelation("parcel", predicate, "cab"),),
            )
            recipe = self.factory.intent_compiler.compile(intent, "厨房储物区")
            request = next(r for r in recipe.objects if r.object_id == "parcel")
            self.assertEqual(request.support, expected)
            scene = self.factory.layout_solver.compile(recipe, 42)
            self.assertTrue(self.factory.validator.validate(scene).valid)

    def test_intent_chair_count_and_around_relation_survive_compilation(self):
        intent = SceneIntent(
            "dining_room",
            "dining_setup",
            "",
            (IntentObject("table", "dining_table", False),)
            + tuple(IntentObject(f"c{i}", "chair", False) for i in range(4)),
            tuple(IntentRelation(f"c{i}", "around", "table") for i in range(4)),
        )
        recipe = self.factory.intent_compiler.compile(intent, "四把椅子围着餐桌")
        result = self.factory.layout_solver.compile(recipe, 42)
        self.assertTrue(self.factory.validator.validate(result).valid)
        self.assertEqual(sum(o.category == "chair" for o in result.objects), 4)
        self.assertTrue(all(o.support == "floor" for o in result.objects if o.category == "chair"))

    def test_descriptive_attributes_keep_the_verified_pack_asset_family(self):
        intent=SceneIntent("kitchen","kitchen_storage","",
            (IntentObject("cab","kitchen_cabinet",False),
             IntentObject("counter","kitchen_counter",False),
             IntentObject("bowl","bowl",True,attributes=("bowl","white"))),
            (IntentRelation("bowl","on","counter"),))
        recipe=self.factory.intent_compiler.compile(intent,"厨房储物区")
        self.assertEqual(next(o.asset_id for o in recipe.objects if o.object_id=="bowl"),"kd_bowl_01")

    def test_mjcf_uses_convex_parts_instead_of_solid_cabinet_proxy(self):
        scene = self.factory.build_from_recipe("kitchen_storage_assets", 42).scene
        root = ET.fromstring(MujocoMjcfExporter(self.factory.registry).to_string(scene))
        cabinet = root.find(".//body[@name='object__cabinet_1']")
        self.assertEqual(len(cabinet.findall("geom")), 7)
        self.assertTrue(all(g.get("type") == "mesh" for g in cabinet.findall("geom")))
        self.assertTrue(all(m.get("vertex") and m.get("face") for m in root.findall("asset/mesh")))

    def test_web_has_full_models_and_spatial_metadata(self):
        with tempfile.TemporaryDirectory() as output:
            app = SceneWebApplication(output, factory=self.factory)
            for asset in self.assets:
                payload = app._asset_payload(asset.asset_id)
                self.assertTrue(payload["visual_url"])
                self.assertEqual(payload["visual_transform"]["rotation_euler_deg"], [90, 0, 0])
                self.assertEqual(payload["physics"]["collision_status"], "validated")
            cabinet = app._asset_payload("kd_cabinet_01")
            self.assertEqual(len(cabinet["interior_regions"]), 3)
            app.close()

    def test_corridor_contract_rejects_nonfinite_and_nonpositive_sizes(self):
        for size in ((0, 1), (-1, 1), (float("nan"), 1)):
            with self.assertRaises(ValueError):
                ClearanceZone.from_dict({"name": "aisle", "center": [0, 0], "size": size})

    def test_corridor_outside_room_cannot_pass_validation(self):
        scene = self.factory.build_from_recipe("dining_core_assets", 42).scene
        outside = ClearanceZone("outside", (5, 0), (0.9, 1))
        report = self.factory.validator.validate(replace(scene, clearance_zones=(outside,)))
        self.assertIn("clearance_out_of_bounds", {issue.code for issue in report.issues})


if __name__ == "__main__":
    unittest.main()
