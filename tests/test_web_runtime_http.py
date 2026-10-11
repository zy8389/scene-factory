from __future__ import annotations

import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from scene_factory import SceneFactory
from scene_factory.webapp import SceneFactoryServer, SceneWebApplication


@pytest.fixture
def runtime_server(tmp_path, monkeypatch):
    monkeypatch.setenv("SCENE_FACTORY_LLM_MODE", "off")
    factory = SceneFactory()
    scene = factory.build_from_recipe("kitchen_franka_mug_lift_tracking_data", 1000).scene.to_dict()
    report = tmp_path / "index.html"
    report.write_text("<html>recorded evidence</html>", encoding="utf-8")

    class Manager:
        has_active_jobs = False
        valid = True
        closed = False

        def config(self):
            return {"available": False, "message": "未配置运行环境", "profiles": [], "max_layouts": 20}

        def list_jobs(self):
            return [self.get_job("reference")]

        def get_job(self, job_id):
            if job_id != "reference":
                raise FileNotFoundError(job_id)
            return {"job_id": job_id, "status": "completed", "validation": {"valid": self.valid}}

        def create(self, payload):
            if set(payload) != {"profile", "limit_layouts"}:
                raise ValueError("unknown job options")
            return {"job_id": "queued-job", "status": "queued"}

        def case_data(self, job_id, case_id):
            self.get_job(job_id)
            if case_id != "recorded-lift":
                raise FileNotFoundError(case_id)
            return {"case_root": tmp_path / "cases" / case_id, "scene": scene}

        def resolve_file(self, job_id, relative):
            self.get_job(job_id)
            if relative != "index.html":
                raise FileNotFoundError(relative)
            return report

        def close(self):
            self.closed = True

    manager = Manager()
    app = SceneWebApplication(tmp_path / "scenes", factory=factory)
    app._runtime_manager = manager
    server = SceneFactoryServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(route, payload=None):
        req = Request(
            f"http://127.0.0.1:{server.server_port}{route}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json"},
        )
        try:
            response = urlopen(req, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            body = response.read()
            return response.status, json.loads(body) if response.headers.get_content_type() == "application/json" else body

    yield app, manager, request
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    assert manager.closed


def test_runtime_http_queues_without_blocking_scene_api(runtime_server):
    _, _, request = runtime_server
    assert request("/api/runtime/config")[1]["available"] is False
    assert request("/api/runtime/jobs")[1]["jobs"][0]["job_id"] == "reference"
    status, job = request("/api/runtime/jobs", {"profile": "tracking", "limit_layouts": 1})
    assert status == 202
    assert job["status"] == "queued"
    assert request("/api/health")[1]["ok"] is True
    assert request("/api/runtime/jobs", {"output_dir": "outside"})[0] == 400
    assert request("/api/runtime/jobs/missing")[0] == 404


def test_runtime_http_replay_adds_local_visual_assets(runtime_server, monkeypatch):
    _, _, request = runtime_server
    seen = []

    def replay(case_root, *, assets):
        seen.append((case_root, assets))
        return {"frames": [{"step": 0}], "source": "recorded_state"}

    monkeypatch.setattr("scene_factory.runtime_replay.replay_payload", replay)
    status, payload = request("/api/runtime/jobs/reference/cases/recorded-lift/replay")
    assert status == 200
    assert payload["source"] == "recorded_state"
    assert seen[0][0].name == "recorded-lift"
    assert seen[0][1]["mug_001"]["visual_url"].startswith("/assets/")
    assert request("/api/runtime/jobs/reference/cases/missing/replay")[0] == 404


def test_runtime_http_refuses_unverified_replay_and_unknown_artifacts(runtime_server, monkeypatch):
    _, manager, request = runtime_server
    manager.valid = False

    def must_not_replay(*args, **kwargs):
        raise AssertionError("unverified evidence must not reach the player")

    monkeypatch.setattr("scene_factory.runtime_replay.replay_payload", must_not_replay)
    assert request("/api/runtime/jobs/reference/cases/recorded-lift/replay")[0] == 409
    assert request("/api/runtime/jobs/reference/files/index.html")[0] == 200
    assert request("/api/runtime/jobs/reference/files/%2e%2e%2foutside.json")[0] == 404
    assert request("/api/runtime/jobs/reference/unknown")[0] == 404


def test_active_acceptance_prevents_a_second_isaac_preview(runtime_server, monkeypatch):
    app, manager, _ = runtime_server
    manager.has_active_jobs = True
    monkeypatch.setattr("scene_factory.webapp.subprocess.Popen", lambda *a, **k: pytest.fail("unexpected GPU process"))
    with pytest.raises(RuntimeError, match="队列"):
        app.open_in_isaac({"scene_id": "any-scene"})


def test_runtime_manager_is_lazy_for_ordinary_scene_generation(tmp_path, monkeypatch):
    monkeypatch.setenv("SCENE_FACTORY_LLM_MODE", "off")
    app = SceneWebApplication(tmp_path)
    assert app._runtime_manager is None
    assert not (tmp_path / "_runtime").exists()
    app.generate({"prompt": "刚回到家，背包在入口。", "seed": 42, "export_mjcf": False})
    assert app._runtime_manager is None
    app.close()
