"""Generated-scene queue and source binding tests; never claim real physics."""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from scene_factory.dataset import DatasetResult, canonical_json, sha256_file, write_json_atomic
from scene_factory.factory import SceneFactory
from scene_factory.runtime_batch import _select
from scene_factory.scene_runtime import inspect_scene_runtime, prepare_scene_runtime_collection
from scene_factory.web_runtime import WebRuntimeManager


class WebRuntimeSceneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="scene-web-source-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.python = self.root / "python.exe"
        self.python.write_bytes(b"not a real interpreter")
        self.assets = self.root / "assets"
        robot = self.assets / "Isaac/Robots/FrankaRobotics/FrankaPanda/franka.usd"
        robot.parent.mkdir(parents=True)
        robot.write_text("#usda 1.0", encoding="utf-8")
        self.source = self.root / "generated-scene"
        self.source.mkdir()
        self.layout = {
            "scene_id": "generated-scene", "seed": 37, "room_type": "kitchen",
            "room_dimensions_m": [4.0, 3.0, 2.8],
            "objects": [{"object_id": "mug_1", "asset_id": "ycb_025_mug",
                         "position_m": [0.58, -0.03, 0.72]}],
        }
        write_json_atomic(self.source / "layout.json", self.layout)
        write_json_atomic(self.source / "scene_intent.json", {"instruction": "移动这个杯子"})
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.managers = []
        self.addCleanup(self._close)
        self.sampling = self.stack.enter_context(patch(
            "scene_factory.web_runtime.build_sampling_collection",
            return_value=DatasetResult("passed", True, {}),
        ))
        self.inspector = self.stack.enter_context(patch(
            "scene_factory.web_runtime.inspect_scene_runtime", side_effect=self._inspection,
        ))
        self.builder = self.stack.enter_context(patch(
            "scene_factory.web_runtime.prepare_scene_runtime_collection", side_effect=self._prepare,
        ))
        self.runner = self.stack.enter_context(patch(
            "scene_factory.web_runtime.run_runtime_batch", side_effect=self._run,
        ))
        self.validator = self.stack.enter_context(patch(
            "scene_factory.web_runtime.validate_runtime_batch", return_value={"valid": True, "errors": []},
        ))

    def _close(self) -> None:
        for manager in self.managers:
            manager.close()
            manager._thread.join(timeout=3)

    def _manager(self, factory=None) -> WebRuntimeManager:
        manager = WebRuntimeManager(self.root / "queue", factory or object(), isaac_python=self.python,
                                    asset_root=self.assets)
        self.managers.append(manager)
        return manager

    def _wait(self, manager: WebRuntimeManager, identifier: str) -> dict:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            record = manager.get_job(identifier)
            if record["status"] in {"completed", "failed", "interrupted"}:
                return record
            time.sleep(0.01)
        self.fail("scene job did not complete")

    @staticmethod
    def _inspection(factory, directory) -> dict:
        directory = Path(directory)
        layout = json.loads((directory / "layout.json").read_text(encoding="utf-8"))
        return {"eligible": True, "reasons": [], "rejection_codes": [],
                "source_scene_id": layout["scene_id"],
                "source_layout_sha256": sha256_file(directory / "layout.json"),
                "source_intent_sha256": sha256_file(directory / "scene_intent.json")
                if (directory / "scene_intent.json").is_file() else None,
                "plan": {"added_objects": []}}

    @classmethod
    def _prepare(cls, factory, source, collection, *, profile="tracking") -> dict:
        source, collection = Path(source), Path(collection)
        inspected = cls._inspection(factory, source)
        layout = json.loads((source / "layout.json").read_text(encoding="utf-8"))
        provenance = {
            "schema_version": "scene_factory.scene_runtime_source.v1",
            **{key: inspected[key] for key in ("source_scene_id", "source_layout_sha256", "source_intent_sha256")},
            "source_layout": layout,
            "source_objects_sha256": hashlib.sha256(canonical_json(layout["objects"])).hexdigest(),
            "plan": inspected["plan"], "profile": profile,
        }
        collection.mkdir()
        write_json_atomic(collection / "frozen-test-layout.json",
                          {**layout, "task": {"scene_runtime_source": provenance}})
        return {**inspected, "eligibility": inspected, "collection_path": str(collection), "task_count": 2}

    @staticmethod
    def _run(collection, output, **kwargs) -> dict:
        collection, output = Path(collection), Path(output)
        source = collection / "frozen-test-layout.json"
        layout = json.loads(source.read_text(encoding="utf-8")) if source.is_file() else {}
        output.mkdir(parents=True)
        cases = []
        for event, suffix in (("robot_mug_lift", "lift"), ("robot_mug_pick_place", "place")):
            case_id = "a" * 20 + "-" + suffix
            case_root = output / "cases" / case_id
            (case_root / "task").mkdir(parents=True)
            write_json_atomic(case_root / "layout.json", {**layout, "event": event})
            (case_root / "task/task_trace.jsonl").write_text('{"step":0}\n', encoding="utf-8")
            cases.append({"case_id": case_id, "state": "complete", "event": event,
                          "scene_id": layout.get("scene_id"), "physics_status": "passed",
                          "task_status": "passed", "evidence_complete": True})
        manifest = {"state": "complete", "limit_layouts": 1, "cases": cases}
        write_json_atomic(output / "manifest.json", manifest)
        (output / "index.html").write_text("<h1>test double</h1>", encoding="utf-8")
        return manifest

    def test_submission_freezes_bytes_before_waiting_in_fifo(self) -> None:
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def run(collection, output, **kwargs):
            if not (Path(collection) / "frozen-test-layout.json").is_file():
                started.set()
                release.wait(timeout=3)
            return self._run(collection, output, **kwargs)

        self.runner.side_effect = run
        manager = self._manager()
        first = manager.create({})
        self.assertTrue(started.wait(timeout=2))
        originals = {path.name: path.read_bytes() for path in self.source.iterdir()}
        queued = manager.create_scene(self.source, {"scene_id": "generated-scene"})
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["kind"], "scene")
        frozen_dir = manager.root / "jobs" / queued["job_id"] / "source"
        self.assertEqual({path.name: path.read_bytes() for path in frozen_dir.iterdir()}, originals)
        changed = copy.deepcopy(self.layout)
        changed["objects"][0]["position_m"][0] = 1.25
        write_json_atomic(self.source / "layout.json", changed)
        (self.source / "scene_intent.json").unlink()
        release.set()
        self.assertEqual(self._wait(manager, first["job_id"])["status"], "completed")
        record = self._wait(manager, queued["job_id"])
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["summary"]["task_status_counts"], {"passed": 2})
        self.assertEqual(record["source_layout_sha256"], hashlib.sha256(originals["layout.json"]).hexdigest())
        self.assertEqual(Path(self.builder.call_args.args[1]), frozen_dir)
        self.assertEqual(self.sampling.call_count, 1)
        replay = manager.case_data(record["job_id"], record["cases"][0]["case_id"])
        self.assertEqual(replay["scene"]["objects"], self.layout["objects"])

    def test_ineligible_scene_is_rejected_without_registration_or_execution(self) -> None:
        self.inspector.side_effect = None
        self.inspector.return_value = {"eligible": False, "reasons": ["需要一个可抓取杯子"]}
        manager = self._manager()
        with self.assertRaisesRegex(ValueError, "可抓取杯子"):
            manager.create_scene(self.source, {"scene_id": "generated-scene"})
        self.assertEqual(manager.list_jobs(), [])
        self.assertFalse(manager.has_active_jobs)
        self.builder.assert_not_called()
        self.runner.assert_not_called()

    def test_scene_payload_cannot_override_count_or_supply_geometry(self) -> None:
        manager = self._manager()
        for payload in ({"scene_id": "generated-scene", "limit_layouts": 2},
                        {"scene_id": "generated-scene", "layout": self.layout},
                        {"scene_id": "generated-scene", "source_dir": str(self.source)},
                        {"scene_id": "wrong-scene"}, {"scene_id": []},
                        {"scene_id": "generated-scene", "profile": "unknown"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                manager.create_scene(self.source, payload)
        self.assertEqual(manager.list_jobs(), [])
        self.runner.assert_not_called()

    def test_changed_frozen_source_removes_accepted_result(self) -> None:
        manager = self._manager()
        record = self._wait(manager, manager.create_scene(self.source, {"scene_id": "generated-scene"})["job_id"])
        self.assertEqual(record["status"], "completed")
        frozen = manager.root / "jobs" / record["job_id"] / "source/layout.json"
        changed = copy.deepcopy(self.layout)
        changed["objects"][0]["position_m"][0] = 1.25
        write_json_atomic(frozen, changed)
        rejected = manager.get_job(record["job_id"])
        self.assertEqual(rejected["status"], "failed")
        self.assertIn("冻结源文件已改变", " ".join(rejected["validation"]["errors"]))
        self.assertNotIn("passed", rejected["summary"]["task_status_counts"])
        self.assertIsNone(rejected["report_url"])
        self.assertFalse(any(case["replay_available"] for case in rejected["cases"]))

    def test_independent_valid_evidence_for_changed_objects_is_rejected(self) -> None:
        def run(collection, output, **kwargs):
            result = self._run(collection, output, **kwargs)
            path = Path(output) / "cases" / result["cases"][0]["case_id"] / "layout.json"
            layout = json.loads(path.read_text(encoding="utf-8"))
            layout["objects"][0]["position_m"][0] += 0.01
            write_json_atomic(path, layout)
            return result

        self.runner.side_effect = run
        manager = self._manager()
        record = self._wait(manager, manager.create_scene(self.source, {"scene_id": "generated-scene"})["job_id"])
        self.assertEqual(record["status"], "failed")
        self.assertIn("布局与原场景不一致", " ".join(record["validation"]["errors"]))
        self.validator.assert_called()

    def test_scene_without_intent_preserves_none_binding(self) -> None:
        (self.source / "scene_intent.json").unlink()
        manager = self._manager()
        record = self._wait(manager, manager.create_scene(self.source, {"scene_id": "generated-scene"})["job_id"])
        self.assertEqual(record["status"], "completed")
        self.assertIsNone(record["source_intent_sha256"])
        self.assertEqual(set(record["source_files_sha256"]), {"layout.json"})
        self.sampling.assert_not_called()

    def test_restart_keeps_source_binding_and_revalidates_without_rerunning(self) -> None:
        manager = self._manager()
        initial = self._wait(manager, manager.create_scene(self.source, {"scene_id": "generated-scene"})["job_id"])
        self.assertEqual(initial["status"], "completed")
        manager.close()
        manager._thread.join(timeout=2)
        self.runner.reset_mock()
        self.builder.reset_mock()
        restored = self._manager()
        record = restored.get_job(initial["job_id"])
        self.assertEqual(record["status"], "completed")
        for field in ("kind", "source_scene_id", "source_layout_sha256", "source_intent_sha256", "source_plan"):
            self.assertEqual(record[field], initial[field])
        self.runner.assert_not_called()
        self.builder.assert_not_called()

    def test_builder_with_different_source_hash_never_launches_simulator(self) -> None:
        def prepare(*args, **kwargs):
            generated = self._prepare(*args, **kwargs)
            generated["source_layout_sha256"] = "0" * 64
            return generated

        self.builder.side_effect = prepare
        manager = self._manager()
        record = self._wait(manager, manager.create_scene(self.source, {"scene_id": "generated-scene"})["job_id"])
        self.assertEqual(record["status"], "failed")
        self.assertIn("未绑定", record["error"])
        self.runner.assert_not_called()

    def test_real_compiler_collection_and_declared_mount_match_final_source_check(self) -> None:
        factory = SceneFactory()
        built = factory.build_from_recipe("kitchen_franka_mug_lift_tracking_data", 1004)
        # Removing the original stand exercises the explicitly declared addition.
        scene = replace(built.scene, objects=tuple(item for item in built.scene.objects
                                                   if item.object_id != "robot_mount_1"))
        built = replace(built, scene=scene)
        (self.source / "scene_intent.json").unlink()
        factory.write_result(built, self.source, export_bundle=False)
        self.inspector.side_effect = inspect_scene_runtime
        self.builder.side_effect = prepare_scene_runtime_collection

        def run(collection, output, **kwargs):
            _, selected = _select(Path(collection), 1)
            output = Path(output)
            output.mkdir()
            cases = []
            for entry in selected:
                source = entry["source"]
                layout = Path(collection) / source["dataset_path"] / source["record"]["files"]["layout"]
                target = output / "cases" / entry["case_id"]
                (target / "task").mkdir(parents=True)
                (target / "layout.json").write_bytes(layout.read_bytes())
                (target / "task/task_trace.jsonl").write_text('{"step":0}\n', encoding="utf-8")
                cases.append({**entry, "state": "complete", "physics_status": "passed",
                              "task_status": "passed", "evidence_complete": True})
            write_json_atomic(output / "manifest.json", {"state": "complete", "cases": cases})
            return {"cases": cases}

        self.runner.side_effect = run
        manager = self._manager(factory)
        record = self._wait(manager, manager.create_scene(self.source, {"scene_id": built.scene.scene_id})["job_id"])
        self.assertEqual(record["status"], "completed", record["validation"]["errors"])
        self.assertEqual(len(record["source_plan"]["added_objects"]), 1)
        data = manager.case_data(record["job_id"], record["cases"][0]["case_id"])
        original = json.loads((self.source / "layout.json").read_text(encoding="utf-8"))
        self.assertEqual(data["scene"]["objects"], original["objects"] + record["source_plan"]["added_objects"])
        self.sampling.assert_not_called()


if __name__ == "__main__":
    unittest.main()
