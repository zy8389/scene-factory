from __future__ import annotations

import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock, patch

from scene_factory.agent import DryRunBackend, SceneFactoryEnv
from scene_factory.asset_pipeline import build_asset_record
from scene_factory.factory import SceneFactory
from scene_factory.intent import SceneIntent
from scene_factory.llm import (
    LLMConfig,
    LLMParserError,
    StructuredLLMIntentParser,
    load_llm_settings,
)
from scene_factory.tasks import TaskEvaluator
from scene_factory.webapp import SceneWebApplication


class SceneFactoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with patch.dict(os.environ, {"SCENE_FACTORY_LLM_MODE": "off"}):
            cls.factory = SceneFactory()

    def test_generation_is_deterministic(self) -> None:
        first = self.factory.build_from_recipe("living_room_recent_snacking", 42)
        second = self.factory.build_from_recipe("living_room_recent_snacking", 42)
        self.assertEqual(first.scene.to_dict(), second.scene.to_dict())
        self.assertTrue(first.valid)

    def test_prompt_selects_matching_event(self) -> None:
        result = self.factory.build_from_prompt("刚回家，背包和鞋放在进门处", 7)
        self.assertEqual(result.scene.event, "returned_home")
        self.assertTrue(result.valid)

    def test_all_recipes_survive_multiple_seeds(self) -> None:
        for recipe in self.factory.recipes.names():
            for seed in range(25):
                with self.subTest(recipe=recipe, seed=seed):
                    result = self.factory.build_from_recipe(recipe, seed)
                    self.assertTrue(result.valid, result.validation.to_dict())

    def test_write_result_and_agent_facade(self) -> None:
        result = self.factory.build_from_recipe("kitchen_after_cooking", 11)
        with tempfile.TemporaryDirectory() as temporary_directory:
            files = self.factory.write_result(result, temporary_directory)
            self.assertTrue(Path(files["preview"]).is_file())
            ET.parse(files["preview"])
            env = SceneFactoryEnv(files["layout"], backend=DryRunBackend(max_steps=2))
            observation, info = env.reset()
            self.assertEqual(info["scene_id"], result.scene.scene_id)
            self.assertIn("scene_graph", observation)
            _, _, _, truncated, _ = env.step([0.0])
            self.assertFalse(truncated)
            _, _, _, truncated, _ = env.step([0.0])
            self.assertTrue(truncated)
            env.close()

    def test_task_evaluator_lifted(self) -> None:
        task = {
            "target_object": "mug_1",
            "success": {"predicate": "lifted", "min_height_delta_m": 0.1},
        }
        evaluator = TaskEvaluator(task, {"mug_1": (0.0, 0.0, 0.5)})
        self.assertFalse(evaluator.evaluate({"mug_1": (0.0, 0.0, 0.59)}))
        self.assertTrue(evaluator.evaluate({"mug_1": (0.0, 0.0, 0.61)}))

    def test_batch_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest = self.factory.build_batch(
                output_root=temporary_directory,
                count=4,
                seed_start=100,
                recipe_name="living_room_returned_home",
            )
            self.assertEqual(len(manifest), 4)
            self.assertTrue(all(item["valid"] for item in manifest))
            manifest_path = Path(temporary_directory) / "manifest.jsonl"
            rows = [json.loads(line) for line in manifest_path.read_text("utf-8").splitlines()]
            self.assertEqual(len(rows), 4)

    def test_web_app_generates_visible_variants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.dict(os.environ, {"SCENE_FACTORY_LLM_MODE": "off"}):
                app = SceneWebApplication(temporary_directory)
            response = app.generate(
                {
                    "prompt": "刚回到家，背包和鞋留在入口附近，钥匙在换鞋凳上。",
                    "seed": 900,
                    "count": 2,
                    "export_usd": False,
                }
            )
            self.assertEqual(response["count"], 2)
            self.assertEqual(response["valid_count"], 2)
            self.assertTrue(response["items"][0]["files"]["preview"].startswith("/outputs/"))
            self.assertEqual(
                response["items"][0]["matched_recipe"]["name"],
                "living_room_returned_home",
            )

    def test_web_app_launches_generated_usd_in_isaac(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.dict(os.environ, {"SCENE_FACTORY_LLM_MODE": "off"}):
                app = SceneWebApplication(temporary_directory)
            scene_dir = Path(temporary_directory) / "example-scene"
            scene_dir.mkdir()
            (scene_dir / "scene.usd").write_bytes(b"dummy usd")
            process = Mock(pid=12345)
            with patch("scene_factory.webapp.subprocess.Popen", return_value=process) as popen:
                response = app.open_in_isaac({"scene_id": "example-scene"})
            self.assertTrue(response["ok"])
            self.assertEqual(response["pid"], 12345)
            popen.assert_called_once()

    def test_scene_intent_compiles_to_valid_seed_variants(self) -> None:
        intent = SceneIntent.from_dict(
            {
                "room_type": "kitchen",
                "event": "after_cooking",
                "description": "锅在岛台，刀在砧板上。",
                "room_dimensions_m": None,
                "clutter_level": 0.65,
                "layout_style": "casual",
                "objects": [
                    {
                        "object_id": "counter",
                        "category": "kitchen_counter",
                        "dynamic": False,
                        "support_hint": None,
                        "attributes": [],
                        "state": [],
                    },
                    {
                        "object_id": "island",
                        "category": "kitchen_island",
                        "dynamic": False,
                        "support_hint": None,
                        "attributes": [],
                        "state": [],
                    },
                    {
                        "object_id": "board",
                        "category": "cutting_board",
                        "dynamic": False,
                        "support_hint": "counter",
                        "attributes": [],
                        "state": [],
                    },
                    {
                        "object_id": "knife",
                        "category": "kitchen_knife",
                        "dynamic": True,
                        "support_hint": "board",
                        "attributes": [],
                        "state": ["斜放"],
                    },
                    {
                        "object_id": "pot",
                        "category": "pot",
                        "dynamic": True,
                        "support_hint": "island",
                        "attributes": [],
                        "state": [],
                    },
                ],
                "relations": [{"subject": "knife", "predicate": "on", "target": "board"}],
            },
            allowed_categories=self.factory.registry.categories(),
            allowed_room_types=self.factory.recipes.room_types(),
            allowed_events=self.factory.recipes.events(),
        )
        recipe = self.factory.intent_compiler.compile(intent, intent.description)
        for seed in range(20):
            with self.subTest(seed=seed):
                scene = self.factory.layout_solver.compile(recipe, seed)
                self.assertTrue(self.factory.validator.validate(scene).valid)

    def test_structured_llm_parser_uses_disk_cache(self) -> None:
        intent_payload = {
            "room_type": "living_room",
            "event": "returned_home",
            "description": "刚回家，背包放在地上。",
            "room_dimensions_m": None,
            "clutter_level": 0.4,
            "layout_style": "casual",
            "objects": [
                {
                    "object_id": "backpack_1",
                    "category": "backpack",
                    "dynamic": True,
                    "support_hint": "floor",
                    "attributes": ["gray"],
                    "state": ["dropped"],
                }
            ],
            "relations": [],
        }
        response_payload = {
            "choices": [{"message": {"content": json.dumps(intent_payload)}}]
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(response_payload).encode("utf-8")

        with tempfile.TemporaryDirectory() as temporary_directory:
            parser = StructuredLLMIntentParser(
                LLMConfig(
                    base_url="http://llm.invalid/v1",
                    model="test-model",
                    cache_dir=Path(temporary_directory),
                ),
                categories=self.factory.registry.categories(),
                room_types=self.factory.recipes.room_types(),
                events=self.factory.recipes.events(),
            )
            with patch("scene_factory.llm.urlopen", return_value=FakeResponse()) as request:
                first = parser.parse("刚回家，背包放在地上")
                second = parser.parse("刚回家，背包放在地上")
            self.assertEqual(first, second)
            self.assertEqual(first.objects[0].category, "backpack")
            request.assert_called_once()

    def test_factory_records_injected_llm_intent(self) -> None:
        intent = SceneIntent.from_dict(
            {
                "room_type": "living_room",
                "event": "returned_home",
                "description": "背包留在入口。",
                "room_dimensions_m": None,
                "clutter_level": 0.4,
                "layout_style": "casual",
                "objects": [
                    {
                        "object_id": "backpack_1",
                        "category": "backpack",
                        "dynamic": True,
                        "support_hint": "floor",
                        "attributes": [],
                        "state": [],
                    }
                ],
                "relations": [],
            },
            allowed_categories=self.factory.registry.categories(),
            allowed_room_types=self.factory.recipes.room_types(),
            allowed_events=self.factory.recipes.events(),
        )

        class FakeParser:
            name = "llm:test-model"

            def parse(self, _prompt: str) -> SceneIntent:
                return intent

        factory = SceneFactory(intent_parser=FakeParser())
        result = factory.build_from_prompt("背包留在入口", 42)
        self.assertEqual(result.prompt_parser, "llm:test-model")
        self.assertIsNotNone(result.intent)
        with tempfile.TemporaryDirectory() as temporary_directory:
            files = factory.write_result(result, temporary_directory)
            self.assertTrue(Path(files["intent"]).is_file())

    def test_structured_llm_parser_revises_complete_intent_and_caches(self) -> None:
        current = SceneIntent.from_dict(
            {
                "room_type": "living_room",
                "event": "recent_snacking",
                "description": "茶几上有一个杯子。",
                "room_dimensions_m": None,
                "clutter_level": 0.5,
                "layout_style": "casual",
                "objects": [
                    {
                        "object_id": "mug_1",
                        "category": "mug",
                        "dynamic": True,
                        "support_hint": "coffee_table_1",
                        "attributes": [],
                        "state": [],
                    }
                ],
                "relations": [],
            },
            allowed_categories=self.factory.registry.categories(),
            allowed_room_types=self.factory.recipes.room_types(),
            allowed_events=self.factory.recipes.events(),
        )
        revised_payload = current.to_dict()
        revised_payload["description"] = "茶几上有一个杯子和一张纸巾。"
        revised_payload["objects"] = [
            *revised_payload["objects"],
            {
                "object_id": "tissue_1",
                "category": "tissue",
                "dynamic": True,
                "support_hint": "coffee_table_1",
                "attributes": [],
                "state": ["casual"],
            },
        ]
        response_payload = {
            "choices": [{"message": {"content": json.dumps(revised_payload)}}]
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(response_payload).encode("utf-8")

        with tempfile.TemporaryDirectory() as temporary_directory:
            parser = StructuredLLMIntentParser(
                LLMConfig(
                    base_url="http://llm.invalid/v1",
                    model="test-model",
                    cache_dir=Path(temporary_directory),
                ),
                categories=self.factory.registry.categories(),
                room_types=self.factory.recipes.room_types(),
                events=self.factory.recipes.events(),
            )
            with patch("scene_factory.llm.urlopen", return_value=FakeResponse()) as request:
                first = parser.revise(current, "再加一张纸巾，其他内容保持不变")
                second = parser.revise(current, "再加一张纸巾，其他内容保持不变")
            self.assertEqual(first, second)
            self.assertEqual([item.category for item in first.objects], ["mug", "tissue"])
            request.assert_called_once()
            sent = json.loads(request.call_args.args[0].data.decode("utf-8"))
            user_content = sent["messages"][1]["content"]
            self.assertIn("mug_1", user_content)
            self.assertIn("再加一张纸巾", user_content)

    def test_web_app_revises_scene_as_new_version(self) -> None:
        current = SceneIntent.from_dict(
            {
                "room_type": "living_room",
                "event": "recent_snacking",
                "description": "茶几上有一个杯子。",
                "room_dimensions_m": None,
                "clutter_level": 0.5,
                "layout_style": "casual",
                "objects": [
                    {
                        "object_id": "mug_1",
                        "category": "mug",
                        "dynamic": True,
                        "support_hint": None,
                        "attributes": [],
                        "state": [],
                    }
                ],
                "relations": [],
            },
            allowed_categories=self.factory.registry.categories(),
            allowed_room_types=self.factory.recipes.room_types(),
            allowed_events=self.factory.recipes.events(),
        )
        current_payload = json.loads(json.dumps(current.to_dict()))
        revised = SceneIntent.from_dict(
            {
                **current_payload,
                "description": "茶几上有一个杯子和一张纸巾。",
                "objects": [
                    *current_payload["objects"],
                    {
                        "object_id": "tissue_1",
                        "category": "tissue",
                        "dynamic": True,
                        "support_hint": None,
                        "attributes": [],
                        "state": [],
                    },
                ],
            },
            allowed_categories=self.factory.registry.categories(),
            allowed_room_types=self.factory.recipes.room_types(),
            allowed_events=self.factory.recipes.events(),
        )

        class FakeRevisionParser:
            name = "llm:test-reviser"

            def parse(self, _prompt: str) -> SceneIntent:
                return current

            def revise(self, source: SceneIntent, instruction: str) -> SceneIntent:
                self.source = source
                self.instruction = instruction
                return revised

        parser = FakeRevisionParser()
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.dict(os.environ, {"SCENE_FACTORY_LLM_MODE": "off"}):
                factory = SceneFactory(intent_parser=parser)
            app = SceneWebApplication(temporary_directory, factory=factory)
            generated = app.generate(
                {
                    "prompt": "茶几上有一个杯子",
                    "seed": 71,
                    "count": 1,
                    "export_usd": False,
                }
            )
            source_id = generated["items"][0]["scene"]["scene_id"]
            response = app.revise(
                {
                    "scene_id": source_id,
                    "instruction": "再加一张纸巾，其他内容保持不变",
                    "seed": 71,
                    "export_usd": False,
                }
            )
            item = response["item"]
            revised_id = item["scene"]["scene_id"]
            self.assertNotEqual(source_id, revised_id)
            self.assertEqual(item["revision"]["source_scene_id"], source_id)
            self.assertEqual(len(item["scene"]["objects"]), 5)
            self.assertIn("revision", item["files"])
            self.assertTrue((Path(temporary_directory) / source_id / "scene_intent.json").is_file())
            revision_record = json.loads(
                (Path(temporary_directory) / revised_id / "revision.json").read_text("utf-8")
            )
            self.assertEqual(revision_record["source_scene_id"], source_id)
            self.assertEqual(parser.instruction, "再加一张纸巾，其他内容保持不变")

    def test_llm_settings_load_non_secret_file_and_secret_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "llm.json"
            config_path.write_text(
                json.dumps(
                    {
                        "mode": "required",
                        "base_url": "http://127.0.0.1:8000/v1",
                        "model": "test-model",
                        "api_key_env": "TEST_SCENE_FACTORY_KEY",
                        "timeout_seconds": 12,
                        "cache_dir": "cache",
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "SCENE_FACTORY_LLM_CONFIG": str(config_path),
                    "TEST_SCENE_FACTORY_KEY": "secret-value",
                },
                clear=True,
            ):
                settings = load_llm_settings()
            self.assertEqual(settings["mode"], "required")
            self.assertEqual(settings["model"], "test-model")
            self.assertEqual(settings["api_key"], "secret-value")
            self.assertEqual(settings["cache_dir"], (root / "cache").resolve())

    def test_llm_failure_is_visible_when_keyword_fallback_is_used(self) -> None:
        class FailingParser:
            name = "llm:unavailable-model"

            def parse(self, _prompt: str):
                raise LLMParserError("endpoint unavailable")

        factory = SceneFactory(intent_parser=FailingParser())
        result = factory.build_from_prompt("刚回到家，背包和鞋留在入口附近", 123)
        self.assertEqual(result.prompt_parser, "keyword_fallback")
        self.assertIn("endpoint unavailable", result.parser_warning or "")
        self.assertTrue(result.valid)

    def test_llm_config_rejects_api_key_pasted_as_environment_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "llm.json"
            config_path.write_text(
                json.dumps(
                    {
                        "mode": "auto",
                        "base_url": "https://provider.example/v1",
                        "model": "test-model",
                        "api_key_env": "looksLikeAnActualSecret123456",
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"SCENE_FACTORY_LLM_CONFIG": str(config_path)},
                clear=True,
            ):
                with self.assertRaisesRegex(ValueError, "not an API key value"):
                    load_llm_settings()

    def test_registry_resolves_relative_local_usd_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry_path = root / "registry.jsonl"
            registry_path.write_text(
                json.dumps(
                    {
                        "asset_id": "real_mug_test",
                        "category": "mug",
                        "bbox_m": [0.09, 0.09, 0.11],
                        "source_path": "assets/real_mug.usd",
                        "source_type": "local_usd",
                        "collision_mode": "proxy_box",
                        "mass_kg": 0.3,
                        "friction": 0.5,
                        "status": "quarantine",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            from scene_factory.registry import AssetRegistry

            registry = AssetRegistry.load(registry_path)
            asset = registry.get("real_mug_test")
            self.assertEqual(asset.source_type, "local_usd")
            self.assertEqual(asset.collision_mode, "proxy_box")
            self.assertEqual(
                registry.resolve_source_path(asset),
                str((root / "assets" / "real_mug.usd").resolve()),
            )
            self.assertEqual(registry.candidates("mug"), ())

    def test_imported_asset_record_starts_in_quarantine(self) -> None:
        report = {
            "valid": True,
            "asset_id": "wrapped_table",
            "category": "coffee_table",
            "wrapped_bbox_m": [1.1, 0.65, 0.42],
            "collision_mode": "proxy_box",
        }
        record = build_asset_record(
            report,
            source_path="assets/wrapped_table.usd",
            mass_kg=18.0,
            friction=0.62,
            support_top=True,
        )
        self.assertEqual(record["status"], "quarantine")
        self.assertEqual(record["support_surfaces"][0]["center"][2], 0.21)


if __name__ == "__main__":
    unittest.main()
