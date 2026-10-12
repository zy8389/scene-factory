from __future__ import annotations

import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from scene_factory import SceneFactory
from scene_factory.webapp import SceneFactoryServer, SceneWebApplication


@pytest.fixture
def scene_server(tmp_path, monkeypatch):
    monkeypatch.setenv("SCENE_FACTORY_LLM_MODE", "off")
    factory = SceneFactory()
    app = SceneWebApplication(tmp_path / "web", factory=factory)
    result = factory.build_from_recipe("kitchen_franka_mug_lift_tracking_data", 1000)
    source = app.output_root / result.scene.scene_id
    source.mkdir()
    (source / "layout.json").write_text(json.dumps(result.scene.to_dict()), encoding="utf-8")

    class Manager:
        closed = False
        calls = []

        def config(self):
            return {"available": False, "message": "环境未配置"}

        def create_scene(self, directory, payload):
            self.calls.append((directory, payload))
            return {"job_id": "scene-job", "status": "queued", "source_scene_id": payload["scene_id"]}

        def create(self, payload):
            return {"job_id": "fixed-job", "status": "queued"}

        def close(self):
            self.closed = True

    manager = Manager()
    app._runtime_manager = manager
    server = SceneFactoryServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(route, payload):
        req = Request(
            f"http://127.0.0.1:{server.server_port}{route}",
            data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"},
        )
        try:
            response = urlopen(req, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    yield app, manager, source, request
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    assert manager.closed


def test_scene_check_returns_plan_without_claiming_physical_success(scene_server, monkeypatch):
    _, manager, source, request = scene_server
    plan = {"robot_base_position_m": [-0.05, 0, 0.78], "target_position_m": [0.62, 0.12, 0.97]}
    monkeypatch.setattr("scene_factory.scene_runtime.inspect_scene_runtime", lambda *args: {
        "eligible": True, "reasons": [], "source_scene_id": source.name, "plan": plan,
    })
    status, result = request("/api/runtime/scene-check", {"scene_id": source.name})
    assert status == 200
    assert result["scene_id"] == source.name
    assert result["task_count"] == 2
    assert result["robot_base_position_m"] == plan["robot_base_position_m"]
    assert result["runtime_available"] is False
    assert "validation" not in result
    assert manager.calls == []


def test_scene_check_explains_unsupported_source(scene_server, monkeypatch):
    _, _, source, request = scene_server
    monkeypatch.setattr("scene_factory.scene_runtime.inspect_scene_runtime", lambda *args: {
        "eligible": False, "reasons": ["当前只支持一个 YCB 杯子"], "plan": None,
    })
    status, result = request("/api/runtime/scene-check", {"scene_id": source.name})
    assert status == 200
    assert result["task_count"] == 0
    assert result["reasons"] == ["当前只支持一个 YCB 杯子"]


def test_scene_job_uses_server_resolved_source_and_retains_fixed_batch_route(scene_server):
    _, manager, source, request = scene_server
    payload = {"scene_id": source.name, "profile": "tracking"}
    status, result = request("/api/runtime/jobs", payload)
    assert status == 202
    assert result["source_scene_id"] == source.name
    assert manager.calls == [(source, payload)]
    status, result = request("/api/runtime/jobs", {"profile": "tracking", "limit_layouts": 1})
    assert status == 202
    assert result["job_id"] == "fixed-job"


@pytest.mark.parametrize("scene_id", ["../outside", "..\\outside", "F:/outside", "..", "", 123, " padded "])
def test_scene_routes_refuse_outside_paths(scene_server, scene_id):
    _, manager, _, request = scene_server
    for route in ["/api/runtime/scene-check", "/api/runtime/jobs"]:
        status, _ = request(route, {"scene_id": scene_id})
        assert status == 400
    assert manager.calls == []


def test_scene_routes_refuse_changed_identity_and_missing_layout(scene_server):
    _, manager, source, request = scene_server
    layout = source / "layout.json"
    value = json.loads(layout.read_text(encoding="utf-8"))
    value["scene_id"] = "another-scene"
    layout.write_text(json.dumps(value), encoding="utf-8")
    assert request("/api/runtime/jobs", {"scene_id": source.name})[0] == 400
    layout.unlink()
    assert request("/api/runtime/scene-check", {"scene_id": source.name})[0] == 400
    assert manager.calls == []


def test_scene_check_refuses_client_geometry_and_unknown_profile(scene_server):
    _, _, source, request = scene_server
    assert request("/api/runtime/scene-check", {"scene_id": source.name, "layout": {}})[0] == 400
    assert request("/api/runtime/scene-check", {"scene_id": source.name, "profile": "custom"})[0] == 400
