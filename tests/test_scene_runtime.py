"""Frozen scene compilation and geometry checks, without a simulator."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scene_factory import SceneFactory
from scene_factory.dataset import DatasetError, canonical_json, sha256_file, write_json_atomic
from scene_factory.geometry import inverse_rotate_xy, rotated_half_extents_xy
from scene_factory.runtime_batch import _select
from scene_factory.sampling import validate_sampling_collection
from scene_factory.scene_runtime import inspect_scene_runtime, prepare_scene_runtime_collection


class SceneRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, {"SCENE_FACTORY_LLM_MODE": "off"}):
            cls.factory = SceneFactory()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="scene-runtime-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        result = self.factory.build_from_recipe("kitchen_franka_mug_lift_tracking_data", 1000)
        self.layout = json.loads(canonical_json(result.scene.to_dict()))
        self.layout["scene_id"] = "web-generated-kitchen-123"
        self.layout["event"] = "after_cooking"
        self.layout["task"] = {"instruction": "保持用户原始任务"}
        self._save()

    def _save(self):
        # Deliberately use different byte formatting to exercise file identity.
        (self.source / "layout.json").write_text(json.dumps(self.layout, indent=4), encoding="utf-8")

    def _mug(self):
        return next(item for item in self.layout["objects"] if item["category"] == "mug")

    def _support(self):
        return next(item for item in self.layout["objects"] if item["object_id"] == "island_1")

    def _remove_mount(self):
        self.layout["objects"] = [item for item in self.layout["objects"] if item["object_id"] != "robot_mount_1"]

    def test_uses_recorded_positions_and_existing_grounded_mount(self):
        before = copy.deepcopy(self.layout)
        result = inspect_scene_runtime(self.factory, self.source)
        self.assertTrue(result["eligible"], result)
        self.assertEqual(result["source_layout_sha256"], sha256_file(self.source / "layout.json"))
        self.assertEqual(result["source_scene_id"], self.layout["scene_id"])
        self.assertEqual(result["plan"]["robot_base_position_m"], [-0.05, 0.0, 0.78])
        self.assertEqual(result["plan"]["added_objects"], [])
        self.assertEqual(result["evidence_scope"], "geometric_screening_only")
        self.assertEqual(self.layout, before)

    def test_adds_explicit_grounded_mount_without_moving_source(self):
        self._remove_mount()
        self._save()
        result = inspect_scene_runtime(self.factory, self.source)
        self.assertTrue(result["eligible"], result)
        additions = result["plan"]["added_objects"]
        self.assertEqual(len(additions), 1)
        self.assertEqual(additions[0]["asset_id"], "side_cabinet_basic")
        self.assertEqual(additions[0]["pose"]["position"][2], 0.39)
        self.assertEqual(additions[0]["bbox_m"][2], 0.78)
        self.assertEqual(result["plan"]["robot_base_position_m"][2], 0.78)
        self.assertEqual(json.loads((self.source / "layout.json").read_text(encoding="utf-8")), self.layout)

    def test_frozen_pair_passes_existing_collection_and_selection_contract(self):
        self._remove_mount()
        self._save()
        write_json_atomic(self.source / "scene_intent.json", {"description": "用户的杯子场景"})
        before = {path.name: sha256_file(path) for path in self.source.iterdir()}
        collection = self.root / "collection"
        with patch.object(self.factory.layout_solver, "compile", side_effect=AssertionError("must not resample")):
            result = prepare_scene_runtime_collection(self.factory, self.source, collection)
        self.assertEqual(result["task_count"], 2)
        checked = validate_sampling_collection(collection)
        self.assertTrue(checked.valid, checked.to_dict())
        _, selected = _select(collection, 1)
        self.assertEqual({entry["event"] for entry in selected}, {"robot_mug_lift", "robot_mug_pick_place"})
        for entry in checked["selected"]:
            layout_path = collection / entry["dataset_path"] / entry["record"]["files"]["layout"]
            task_layout = json.loads(layout_path.read_text(encoding="utf-8"))
            original = {item["object_id"]: item for item in self.layout["objects"]}
            actual = {item["object_id"]: item for item in task_layout["objects"]}
            self.assertEqual({key: actual[key] for key in original}, original)
            source = task_layout["task"]["scene_runtime_source"]
            self.assertEqual(source["source_layout"], self.layout)
            self.assertEqual(source["source_layout_sha256"], before["layout.json"])
            self.assertEqual(source["source_intent_sha256"], before["scene_intent.json"])
            self.assertEqual([actual[item["object_id"]] for item in source["plan"]["added_objects"]],
                             source["plan"]["added_objects"])
            self.assertEqual(task_layout["task"]["controller"]["arm_stiffness"], 2000.0)
            if task_layout["event"] == "robot_mug_pick_place":
                success = task_layout["task"]["success"]
                self.assertEqual(success["min_lift_delta_m"], 0.1)
                self.assertEqual(success["settle_steps"], 20)
                self.assertEqual(success["target_tolerance_m"], [0.09, 0.09, 0.03])
        self.assertEqual(before, {path.name: sha256_file(path) for path in self.source.iterdir()})

    def test_baseline_preserves_default_controller_and_fixed_success_gate(self):
        output = self.root / "baseline"
        prepare_scene_runtime_collection(self.factory, self.source, output, profile="baseline")
        checked = validate_sampling_collection(output)
        for entry in checked["selected"]:
            path = output / entry["dataset_path"] / entry["record"]["files"]["layout"]
            task = json.loads(path.read_text(encoding="utf-8"))["task"]
            self.assertEqual(task["controller"], {})
            self.assertEqual(task["scene_runtime_source"]["profile"], "baseline")
            self.assertGreaterEqual(task["success"].get("min_height_delta_m", task["success"].get("min_lift_delta_m")), 0.1)

    def test_accepts_small_recorded_yaw_and_checks_rotated_surface(self):
        self._remove_mount()
        self._support()["pose"]["yaw_deg"] = 2.0
        self._mug()["pose"]["yaw_deg"] = 15.0
        self._save()
        result = inspect_scene_runtime(self.factory, self.source)
        self.assertTrue(result["eligible"], result)
        plan = result["plan"]
        self.assertEqual(plan["source_mug_yaw_deg"], 15.0)
        self.assertEqual(plan["source_support_yaw_deg"], 2.0)
        target = plan["target_position_m"]
        support = self._support()
        local = inverse_rotate_xy((target[0] - support["pose"]["position"][0], target[1]), 2.0)
        hx, hy = rotated_half_extents_xy(tuple(self._mug()["bbox_m"]), 13.0)
        self.assertLessEqual(abs(local[0]) + hx + 0.025, 1.32 / 2.0)
        self.assertLessEqual(abs(local[1]) + hy + 0.025, 0.70 / 2.0)

    def test_unreachable_centered_cup_is_rejected_without_repositioning(self):
        self._remove_mount()
        self._mug()["pose"]["position"][0] = 1.15
        self._save()
        result = inspect_scene_runtime(self.factory, self.source)
        self.assertFalse(result["eligible"])
        self.assertIn("mug_out_of_reach", result["rejection_codes"])
        self.assertEqual(self._mug()["pose"]["position"][0], 1.15)

    def test_large_yaw_and_wrong_room_are_actionable_rejections(self):
        for field, value, expected in (("mug", 30.0, "unsupported_mug_yaw"),
                                       ("support", 6.0, "unsupported_support"),
                                       ("room", "bedroom", "unsupported_room")):
            with self.subTest(field=field):
                layout = copy.deepcopy(self.layout)
                if field == "room":
                    layout["room_type"] = value
                else:
                    category = "mug" if field == "mug" else "kitchen_island"
                    next(item for item in layout["objects"] if item["category"] == category)["pose"]["yaw_deg"] = value
                result = inspect_scene_runtime(self.factory, layout)
                self.assertIn(expected, result["rejection_codes"])
                self.assertTrue(result["reasons"])

    def test_rejects_multiple_mugs_and_extra_dynamic_objects(self):
        duplicate = copy.deepcopy(self._mug())
        duplicate["object_id"] = "mug_2"
        self.layout["objects"].append(duplicate)
        self.assertIn("single_mug_required", inspect_scene_runtime(self.factory, self.layout)["rejection_codes"])
        self.layout["objects"].pop()
        self._support()["dynamic"] = True
        self.assertIn("extra_dynamic_objects", inspect_scene_runtime(self.factory, self.layout)["rejection_codes"])

    def test_mount_outside_room_is_rejected(self):
        self._remove_mount()
        for item in self.layout["objects"]:
            item["pose"]["position"][0] -= 1.9
        self.assertIn("mount_out_of_room", inspect_scene_runtime(self.factory, self.layout)["rejection_codes"])

    def test_obstructed_mount_is_rejected(self):
        self._remove_mount()
        obstacle = copy.deepcopy(next(item for item in self.factory.build_from_recipe(
            "kitchen_franka_mug_lift_tracking_data", 1000).scene.to_dict()["objects"]
            if item["asset_id"] == "side_cabinet_basic"))
        obstacle = json.loads(canonical_json(obstacle))
        obstacle["object_id"] = "obstacle"
        obstacle["pose"]["position"] = [-0.05, 0.0, 0.39]
        obstacle["pose"]["yaw_deg"] = 90.0
        self.layout["objects"].append(obstacle)
        result = inspect_scene_runtime(self.factory, self.layout)
        self.assertIn("mount_obstructed", result["rejection_codes"])

    def test_unavailable_placement_is_rejected_without_shifting_cup(self):
        # Two upright static furniture objects obstruct both possible transfer
        # directions while leaving the cup's initial placement untouched.
        asset = self.factory.registry.get("side_cabinet_basic")
        for identifier, y in (("front_block", 0.28), ("back_block", -0.28)):
            self.layout["objects"].append({
                "object_id": identifier, "asset_id": asset.asset_id, "category": asset.category,
                "bbox_m": list(asset.bbox_m), "pose": {"position": [0.67, y, 1.39], "yaw_deg": 0.0},
                "dynamic": False, "support": None, "relations": [], "fallback_reason": None,
                "interactions": None,
            })
        before = copy.deepcopy(self._mug())
        result = inspect_scene_runtime(self.factory, self.layout)
        self.assertIn("no_clear_placement", result["rejection_codes"])
        self.assertEqual(self._mug(), before)

    def test_proxy_mug_and_height_outside_mount_range_are_rejected(self):
        self._mug()["asset_id"] = "mug_blue"
        self.assertIn("unsupported_mug", inspect_scene_runtime(self.factory, self.layout)["rejection_codes"])
        self._mug()["asset_id"] = "mug_001"
        self._support()["pose"]["position"][2] += 0.09
        self._mug()["pose"]["position"][2] += 0.09
        self.assertIn("unsupported_table_height", inspect_scene_runtime(self.factory, self.layout)["rejection_codes"])

    def test_source_and_evidence_cannot_be_overwritten(self):
        with self.assertRaises(DatasetError):
            prepare_scene_runtime_collection(self.factory, self.source, self.source)
        output = self.root / "collection"
        output.mkdir()
        (output / "diagnostic.json").write_text("preserve", encoding="utf-8")
        with self.assertRaises(DatasetError):
            prepare_scene_runtime_collection(self.factory, self.source, output)
        self.assertEqual((output / "diagnostic.json").read_text(encoding="utf-8"), "preserve")
        with self.assertRaises(DatasetError):
            prepare_scene_runtime_collection(self.factory, self.source, self.root / "invalid", profile="unsafe")

    def test_layout_change_after_inspection_is_detected(self):
        original = inspect_scene_runtime(self.factory, self.source)
        self._mug()["pose"]["position"][0] += 0.001
        self._save()
        with patch("scene_factory.scene_runtime.inspect_scene_runtime", return_value=original):
            with self.assertRaisesRegex(DatasetError, "发生变化"):
                prepare_scene_runtime_collection(self.factory, self.source, self.root / "collection")

    def test_missing_or_noncanonical_layout_is_rejected(self):
        self.assertFalse(inspect_scene_runtime(self.factory, self.root / "missing")["eligible"])
        del self._mug()["relations"]
        self.assertIn("noncanonical_layout", inspect_scene_runtime(self.factory, self.layout)["rejection_codes"])


if __name__ == "__main__":
    unittest.main()
