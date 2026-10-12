from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from scene_factory import SceneFactory
from scene_factory.intent import IntentObject, IntentRelation, SceneIntent
from scene_factory.llm import StructuredLLMIntentParser
from scene_factory.webapp import SceneWebApplication


def living_intent():
    return SceneIntent(
        room_type="living_room", event="recent_snacking", description="两个杯子放茶几，遥控器放沙发。",
        objects=(IntentObject("table", "coffee_table", dynamic=False),
                 IntentObject("sofa", "sofa", dynamic=False),
                 IntentObject("blue_cup", "mug", support_hint="table", attributes=("蓝色",)),
                 IntentObject("cream_cup", "mug", support_hint="table", attributes=("cream",)),
                 IntentObject("remote", "remote_control", support_hint="sofa")),
        relations=(IntentRelation("blue_cup", "on", "table"),
                   IntentRelation("cream_cup", "on", "table"),
                   IntentRelation("remote", "on", "sofa")),
    )


def entry_intent():
    return SceneIntent(
        room_type="living_room", event="returned_home", description="背包和一双鞋在入口，钥匙在换鞋凳上。",
        objects=(IntentObject("bench", "entry_bench", dynamic=False),
                 IntentObject("bag", "backpack", support_hint="floor"),
                 IntentObject("left_shoe", "shoe", support_hint="floor"),
                 IntentObject("right_shoe", "shoe", support_hint="floor"),
                 IntentObject("keys", "keys", support_hint="bench")),
        relations=(IntentRelation("bag", "near", "bench"),
                   IntentRelation("left_shoe", "near", "bench"),
                   IntentRelation("right_shoe", "near", "bench"),
                   IntentRelation("keys", "on", "bench")),
    )


@pytest.fixture
def factory(monkeypatch):
    monkeypatch.setenv("SCENE_FACTORY_LLM_MODE", "off")
    return SceneFactory()


def test_detailed_prompt_uses_llm_even_with_fast_templates_enabled():
    parser = Mock(spec=StructuredLLMIntentParser)
    parser.name = "llm:regression"
    parser.parse.return_value = living_intent()
    with patch.dict(os.environ, {"SCENE_FACTORY_LLM_MODE": "auto"}):
        factory = SceneFactory(intent_parser=parser)
    factory.llm_settings["keyword_fast_path"] = True
    prompt = "刚在客厅吃完零食，两个杯子放茶几，遥控器留在沙发上。"
    result = factory.build_from_prompt(prompt, 42)
    parser.parse.assert_called_once_with(prompt)
    assert result.prompt_parser == parser.name
    assert Counter(item.category for item in result.scene.objects)["mug"] == 2
    objects = {item.object_id: item for item in result.scene.objects}
    assert objects["blue_cup"].asset_id == "mug_blue"
    assert objects["cream_cup"].asset_id == "mug_cream"
    assert objects["remote"].support == "sofa_1:seat"
    assert result.valid


def test_entry_counts_regions_and_supports_survive_compilation(factory):
    intent = entry_intent()
    recipe = factory.intent_compiler.compile(intent, intent.description)
    requests = {item.object_id: item for item in recipe.objects}
    for object_id in ("bag", "left_shoe", "right_shoe"):
        assert requests[object_id].support == "floor"
        assert requests[object_id].region_xy is not None
    assert requests["keys"].support == "entry_bench_1:top"
    for seed in range(20):
        result = factory.build_from_intent(intent, seed)
        assert result.valid
        assert Counter(item.category for item in result.scene.objects)["shoe"] == 2
        for item in result.scene.objects:
            if item.object_id in ("bag", "left_shoe", "right_shoe"):
                xmin, xmax, ymin, ymax = requests[item.object_id].region_xy
                assert xmin <= item.pose.position[0] <= xmax
                assert ymin <= item.pose.position[1] <= ymax


def test_explicit_location_takes_priority_over_entry_defaults(factory):
    intent = SceneIntent(
        room_type="living_room", event="returned_home", description="背包在沙发旁的地面。",
        objects=(IntentObject("sofa", "sofa", dynamic=False),
                 IntentObject("bag", "backpack", support_hint="floor")),
        relations=(IntentRelation("bag", "near", "sofa"),),
    )
    recipe = factory.intent_compiler.compile(intent, intent.description)
    bag = next(item for item in recipe.objects if item.object_id == "bag")
    assert bag.region_xy is None
    assert bag.relations[0].target == "sofa_1"
    assert factory.build_from_intent(intent, 42).valid


def test_additional_furniture_is_not_collapsed_to_existing_fixture(factory):
    intent = SceneIntent(
        room_type="living_room", event="recent_snacking", description="客厅有两个沙发。",
        objects=(IntentObject("first", "sofa", dynamic=False),
                 IntentObject("second", "sofa", dynamic=False)),
    )
    recipe = factory.intent_compiler.compile(intent, intent.description)
    sofas = [item for item in recipe.objects if item.category == "sofa"]
    assert len(sofas) == 2
    assert sofas[1].support == "floor"
    assert factory.build_from_intent(intent, 42).valid


def test_household_visual_sources_match_hashes_and_are_packaged(tmp_path, factory):
    import hashlib
    import tomllib

    expected = {"snack_bag_red", "remote_black", "tissue_white", "cushion_green", "backpack_gray",
                "shoe_left_white", "keyring_metal", "mug_blue", "mug_cream"}
    package = tomllib.loads(Path("pyproject.toml").read_text("utf-8"))["tool"]["setuptools"]["data-files"]
    app = SceneWebApplication(tmp_path, factory=factory)
    for asset_id in expected:
        root = Path("data/assets/source") / asset_id
        source = json.loads((root / "SOURCE.json").read_text("utf-8"))
        payload = (root / source["source_geometry"]).read_bytes()
        assert payload[:4] == b"glTF"
        assert source["geometry_origin"] == "procedural"
        assert source["source_files"][0]["sha256"] == hashlib.sha256(payload).hexdigest()
        assert factory.registry.get(asset_id).asset_hash == "sha256:" + hashlib.sha256(payload).hexdigest()
        assert f"share/scene-factory/data/assets/source/{asset_id}" in package
        assert app._asset_payload(asset_id)["visual_url"] is not None
    for recipe in ("living_room_recent_snacking", "living_room_returned_home"):
        result = factory.build_from_recipe(recipe, 42)
        files = factory.write_result(result, tmp_path / recipe, export_bundle=False)
        assert app._result_payload(result, files)["appearance"]["proxy_count"] == 0


def test_sofa_seat_is_at_seat_height_and_can_hold_cushion(factory):
    sofa = factory.registry.get("sofa_basic")
    seat = sofa.support_surfaces[0]
    assert seat.center[2] == 0
    assert seat.size[1] >= 0.42 * 1.3
    result = factory.build_from_recipe("living_room_recent_snacking", 42)
    objects = {item.object_id: item for item in result.scene.objects}
    remote = objects["remote_1"]
    assert abs(remote.pose.position[2] - remote.bbox_m[2] / 2 - 0.45) < 1e-6
    assert result.valid


def test_llm_cache_separates_asset_capabilities(tmp_path):
    from scene_factory.llm import LLMConfig

    config = LLMConfig("http://localhost/v1", "test", cache_dir=tmp_path)
    first = StructuredLLMIntentParser(config, categories=["mug"], room_types=["living_room"],
                                     events=["recent_snacking"], scene_context={"has_visual": False})
    second = StructuredLLMIntentParser(config, categories=["mug"], room_types=["living_room"],
                                      events=["recent_snacking"], scene_context={"has_visual": True})
    assert first._cache_path("同一句需求") != second._cache_path("同一句需求")
