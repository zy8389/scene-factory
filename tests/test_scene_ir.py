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


def test_warehouse_recipe_can_be_represented_as_scene_source():
    result = SceneFactory().build_from_recipe("warehouse_retrieval", 42)
    assert result.valid
    assert result.scene.room_type == "warehouse"
