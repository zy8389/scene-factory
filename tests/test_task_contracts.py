from dataclasses import replace

import pytest

from scene_factory.intent import SceneIntent
from scene_factory.scene_ir import SceneIR
from scene_factory.task_contracts import TaskAction, TaskGraph


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



@pytest.mark.parametrize("action", [
    None,
    {},
    {"action": 42},
    {"action": "grasp", "actor": "missing"},
    {"action": "grasp", "subject": []},
    {"action": "grasp", "parameters": []},
    {"action": "grasp", "parameters": {"force": float("inf")}},
    {"action": "grasp", "preconditions": "reachable"},
    {"action": "grasp", "effects": [None]},
    {"action": "grasp", "typo_field": True},
])
def test_task_graph_rejects_malformed_actions(action):
    with pytest.raises(ValueError):
        TaskGraph.from_scene_ir(_warehouse_scene(), [action])


def test_task_graph_validates_scene_and_version_for_direct_constructors():
    scene = _warehouse_scene()
    invalid_scene = replace(scene, scene_type="kitchen")
    graph = TaskGraph("scene_factory.task_graph.v1", "warehouse", (TaskAction("clean"),))
    with pytest.raises(ValueError, match="domain"):
        graph.validate(invalid_scene)
    with pytest.raises(ValueError, match="domain"):
        TaskGraph.from_scene_ir(invalid_scene, [{"action": "clean"}])
    with pytest.raises(ValueError, match="schema_version"):
        replace(graph, schema_version="invalid.v999").validate(scene)


def test_task_graph_preserves_conditions_and_effects():
    graph = TaskGraph.from_scene_ir(_warehouse_scene(), [{
        "action": "grasp", "subject": "box_1", "preconditions": ["reachable"],
        "effects": ["held"], "parameters": {"force": 1.5},
    }])
    assert graph.actions[0].preconditions == ("reachable",)
    assert graph.actions[0].effects == ("held",)
    assert graph.to_dict()["actions"][0]["parameters"] == {"force": 1.5}
