from dataclasses import replace
import json

import pytest

from scene_factory.factory import SceneFactory
from scene_factory.intent import SceneIntent
from scene_factory.scene_ir import SceneIR


def test_scene_ir_upgrades_home_intent_and_round_trips():
    intent = SceneIntent.from_dict({
        "room_type": "kitchen", "event": "after_cooking",
        "description": "a bowl on the counter",
        "objects": [
            {"object_id": "counter_1", "category": "kitchen_counter", "dynamic": False, "support_hint": None, "attributes": [], "state": []},
            {"object_id": "bowl_1", "category": "bowl", "dynamic": True, "support_hint": "counter_1", "attributes": [], "state": []},
        ],
        "relations": [{"subject": "bowl_1", "predicate": "on", "target": "counter_1"}],
        "room_dimensions_m": [4.0, 3.0, 2.8], "clutter_level": 0.3, "layout_style": "casual",
    })
    ir = SceneIR.from_intent(intent)
    assert ir.domain == "home"
    assert ir.to_dict() == SceneIR.from_dict(ir.to_dict()).to_dict()
    assert ir.to_dict() == SceneIR.from_dict(json.loads(json.dumps(ir.to_dict()))).to_dict()


def test_warehouse_recipe_can_be_represented_as_scene_source():
    result = SceneFactory().build_from_recipe("warehouse_retrieval", 42)
    assert result.valid
    assert result.scene.room_type == "warehouse"



def _payload():
    return {
        "domain": "warehouse", "scene_type": "warehouse",
        "entities": [{"entity_id": "box_1", "kind": "box"}],
    }


@pytest.mark.parametrize("patch", [
    {"schema_version": "invalid.v999"},
    {"domain": "home"},
    {"domain": None},
    {"scene_type": 42},
    {"description": None},
    {"dimensions_m": [-1, 2, 3]},
    {"dimensions_m": [0, 2, 3]},
    {"dimensions_m": [True, 2, 3]},
    {"dimensions_m": [float("nan"), 2, 3]},
    {"dimensions_m": [float("inf"), 2, 3]},
    {"dimensions_m": [1, 2]},
    {"dimensions_m": "123"},
    {"assumptions": "not an array"},
    {"entities": None},
    {"entities": [None]},
    {"entities": [{"entity_id": [], "kind": "box"}]},
    {"entities": [{"entity_id": " ", "kind": "box"}]},
    {"entities": [{"entity_id": "box", "kind": "box", "dynamic": "false"}]},
    {"entities": [{"entity_id": "box", "kind": "box", "states": "open"}]},
    {"constraints": [{"constraint_type": "near", "subject": "missing"}]},
    {"constraints": [{"constraint_type": "near", "target": []}]},
    {"constraints": [{"constraint_type": "near", "parameters": []}]},
    {"constraints": [{"constraint_type": "near", "parameters": {"x": float("nan")}}]},
    {"constraints": [{"constraint_type": "room_bounds", "parameters": {"dimensions_m": [-1, 2, 3]}}]},
    {"dimensions_m": [1, 2, 3], "constraints": [
        {"constraint_type": "room_bounds", "parameters": {"dimensions_m": [4, 5, 6]}}
    ]},
    {"tasks": [{"action": "clean"}]},
    {"tasks": [{"action": "grasp", "subject": "missing"}]},
    {"tasks": [{"action": "grasp", "parameters": []}]},
    {"relations": [{"subject": None, "predicate": "near", "target": "box_1"}]},
    {"typo_field": 1},
])
def test_scene_ir_rejects_invalid_external_payload(patch):
    with pytest.raises(ValueError):
        SceneIR.from_dict({**_payload(), **patch})


def test_direct_ir_construction_cannot_bypass_validation():
    scene = SceneIR.from_dict(_payload())
    with pytest.raises(ValueError, match="domain"):
        replace(scene, domain="home").to_dict()
    with pytest.raises(ValueError, match="dimensions_m"):
        replace(scene, dimensions_m=(-1, 2, 3)).validate()
