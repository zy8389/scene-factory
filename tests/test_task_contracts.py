import pytest

from scene_factory.intent import SceneIntent
from scene_factory.scene_ir import SceneIR
from scene_factory.task_contracts import TaskGraph


def _warehouse_scene() -> SceneIR:
    intent = SceneIntent.from_dict({
        "room_type": "warehouse", "event": "warehouse_retrieval",
        "description": "retrieve a box",
        "objects": [
            {"object_id": "robot_1", "category": "mobile_manipulator", "dynamic": True, "support_hint": None, "attributes": [], "state": []},
            {"object_id": "box_1", "category": "box", "dynamic": True, "support_hint": None, "attributes": [], "state": []},
        ],
        "relations": [], "room_dimensions_m": [8.0, 6.0, 4.0],
        "clutter_level": 0.2, "layout_style": "aisle",
    })
    return SceneIR.from_intent(intent)


def test_warehouse_task_graph_accepts_retrieve_sequence():
    scene = _warehouse_scene()
    graph = TaskGraph.from_scene_ir(scene, [
        {"action": "navigate", "actor": "robot_1", "target": "box_1"},
        {"action": "grasp", "actor": "robot_1", "subject": "box_1"},
        {"action": "retrieve", "actor": "robot_1", "subject": "box_1"},
        {"action": "place", "actor": "robot_1", "subject": "box_1"},
    ])
    assert [item.action for item in graph.actions] == ["navigate", "grasp", "retrieve", "place"]


def test_task_graph_rejects_unknown_action():
    scene = _warehouse_scene()
    with pytest.raises(ValueError, match="not supported"):
        TaskGraph.from_scene_ir(scene, [{"action": "pour", "actor": "robot_1", "subject": "box_1"}])
