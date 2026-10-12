from __future__ import annotations

from dataclasses import replace
import json
import math

import pytest

from scene_factory.factory import SceneFactory
from scene_factory.llm import LLMConfig, LLMParserError, StructuredLLMClient
from scene_factory.models import Pose
from scene_factory.priority import (
    ask_priority, build_priority_scenario, edge_observation, load_priority_scenario,
    validate_priority_scenario, write_priority_scenario,
)
from scene_factory.webapp import SceneWebApplication


@pytest.fixture
def factory(monkeypatch):
    monkeypatch.setenv("SCENE_FACTORY_LLM_MODE", "off")
    return SceneFactory()


def test_clearance_is_measured_and_current_task_changes_identity(factory):
    result, scenario = build_priority_scenario(factory, clearance_m=0.005)
    assert result.valid
    assert scenario["cup"]["minimum_clearance_m"] == pytest.approx(0.005)
    assert scenario["cup"]["nearest_edge"] == "+x"
    assert scenario["motion"]["velocity_measured"] is False
    assert scenario["physics_verified"] is False
    _, center = build_priority_scenario(factory, clearance_m=0.20, motion_state="stationary")
    assert center["cup"]["minimum_clearance_m"] == pytest.approx(0.20)
    _, critical = build_priority_scenario(factory, current_task="扶稳正在倾倒的热锅。",
                                          urgency="safety_critical", safe_to_pause=False)
    assert critical["scene_id"] != scenario["scene_id"]
    assert critical["current_task"]["safe_to_pause"] is False


def test_measurements_respect_rotated_support_frame(factory):
    from scene_factory.geometry import rotate_xy

    result, scenario = build_priority_scenario(factory)
    rotated = replace(result.scene, objects=tuple(
        replace(item, pose=Pose((*rotate_xy(item.pose.position[:2], 37), item.pose.position[2]),
                                item.pose.yaw_deg + 37))
        for item in result.scene.objects
    ))
    measured = edge_observation(rotated, factory)
    assert measured["edge_clearances_m"] == scenario["cup"]["edge_clearances_m"]


@pytest.mark.parametrize("parameters", [
    {"clearance_m": -0.001}, {"clearance_m": math.nan}, {"clearance_m": 0.5},
    {"clearance_m": True}, {"seed": True}, {"motion_state": "falling_verified"},
    {"safe_to_pause": "yes"},
])
def test_invalid_or_unverified_states_are_rejected(factory, parameters):
    with pytest.raises(ValueError):
        build_priority_scenario(factory, **parameters)


def test_scenario_rejects_claimed_motion_measurement_and_stale_layout(factory, tmp_path):
    files = write_priority_scenario(factory, tmp_path)
    scenario = load_priority_scenario(files["priority_scenario"])
    scenario["motion"]["velocity_measured"] = True
    with pytest.raises(ValueError, match="measured motion"):
        validate_priority_scenario(scenario)
    layout_path = tmp_path / "layout.json"
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    layout["objects"][0]["pose"]["position"][0] += 0.1
    layout_path.write_text(json.dumps(layout), encoding="utf-8")
    with pytest.raises(ValueError, match="布局已改变"):
        load_priority_scenario(files["priority_scenario"])


def test_stored_risk_measurement_must_match_layout(factory, tmp_path):
    from pathlib import Path

    files = write_priority_scenario(factory, tmp_path)
    path = Path(files["priority_scenario"])
    scenario = load_priority_scenario(path)
    scenario["cup"]["minimum_clearance_m"] = 0.02
    scenario["cup"]["edge_clearances_m"]["+x"] = 0.02
    path.write_text(json.dumps(scenario), encoding="utf-8")
    with pytest.raises(ValueError, match="几何观测"):
        load_priority_scenario(path)


@pytest.mark.parametrize("action, safe_to_pause", [("secure_cup", True), ("continue_current_task", False)])
def test_ai_receives_task_context_without_a_forced_answer(factory, monkeypatch, action, safe_to_pause):
    _, scenario = build_priority_scenario(factory, safe_to_pause=safe_to_pause)
    client = StructuredLLMClient(LLMConfig(base_url="https://example.test/v1", model="test-model"))

    def respond(payload):
        supplied = json.loads(payload["messages"][1]["content"])
        assert supplied["current_task"]["safe_to_pause"] is safe_to_pause
        assert supplied["cup"]["minimum_clearance_m"] == 0.005
        assert payload["response_format"]["json_schema"]["name"] == "task_priority"
        return {"choices": [{"message": {"content": json.dumps({
            "action": action, "reason": "根据杯子风险与当前任务的暂停后果选择顺序。",
            "missing_information": [], "next_steps": ["先确认可安全暂停的时机。"],
        })}}]}

    monkeypatch.setattr(client, "_post", respond)
    decision = ask_priority(scenario, client=client)
    assert decision["action"] == action
    assert decision["source"] == "llm"
    assert decision["executed"] is False


def test_missing_llm_does_not_silently_return_a_rule_based_answer(factory):
    _, scenario = build_priority_scenario(factory)
    with pytest.raises(ValueError, match="配置 LLM"):
        ask_priority(scenario)


def test_unsupported_ai_action_is_rejected(factory, monkeypatch):
    _, scenario = build_priority_scenario(factory)
    client = StructuredLLMClient(LLMConfig(base_url="https://example.test", model="test"))
    monkeypatch.setattr(client, "request_json", lambda *args, **kwargs: {
        "action": "execute_robot", "reason": "done", "missing_information": [], "next_steps": [],
    })
    with pytest.raises(LLMParserError, match="invalid priority decision"):
        ask_priority(scenario, client=client)


def test_web_generation_saves_observation_and_asks_about_the_same_scene(factory, tmp_path, monkeypatch):
    app = SceneWebApplication(tmp_path, factory=factory)
    item = app.generate_priority({"clearance_m": 0.008})["items"][0]
    assert item["priority_scenario"]["cup"]["minimum_clearance_m"] == 0.008
    assert "priority_scenario" in item["files"]
    seen = []
    monkeypatch.setattr("scene_factory.priority.ask_priority", lambda scenario: seen.append(scenario) or {
        "action": "secure_cup", "source": "llm", "executed": False,
    })
    response = app.ask_priority({"scene_id": item["scene"]["scene_id"]})
    assert seen[0] == item["priority_scenario"]
    assert "priority_decision" in response["files"]
    with pytest.raises(ValueError, match="invalid scene_id"):
        app.ask_priority({"scene_id": "../outside"})


def test_http_priority_routes_preserve_the_scene_and_decision(factory, tmp_path, monkeypatch):
    import threading
    from urllib.request import Request, urlopen
    from scene_factory.webapp import SceneFactoryServer

    app = SceneWebApplication(tmp_path, factory=factory)
    server = SceneFactoryServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr("scene_factory.priority.ask_priority", lambda scenario: {
        "scene_id": scenario["scene_id"], "action": "continue_current_task", "executed": False,
    })

    def post(route, payload):
        request = Request(f"http://127.0.0.1:{server.server_port}{route}",
                          data=json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    try:
        item = post("/api/priority/generate", {"safe_to_pause": False})["items"][0]
        scene_id = item["scene"]["scene_id"]
        response = post("/api/priority/ask", {"scene_id": scene_id})
        assert response["decision"]["scene_id"] == scene_id
        assert response["decision"]["executed"] is False
        assert item["priority_scenario"]["current_task"]["safe_to_pause"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
