from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from scene_factory.runtime_worker import (
    IDENTITY_SCHEMA,
    JOB_SCHEMA,
    WORKER_SCHEMA,
    TraceWriter,
    _atomic_json,
    _check_usd_pose_sync,
    _json_line,
    _live_rigid_state,
    _new_rigid_view,
    _observe_frozen_inputs,
    _observed_code_fingerprint,
    load_job,
)


def _job(tmp_path: Path, **overrides) -> tuple[Path, dict]:
    layout, usd = tmp_path / "layout.json", tmp_path / "scene.usda"
    layout.write_text('{"objects": []}', encoding="utf-8")
    usd.write_text("#usda 1.0\n", encoding="utf-8")
    payload = {"schema_version": JOB_SCHEMA, "run_id": "run-1", "case_id": "case-1",
               "mode": "physics", "layout_path": str(layout), "usd_path": str(usd),
               "output_dir": str(tmp_path / "evidence"),
               "bindings": {"layout_sha256": hashlib.sha256(layout.read_bytes()).hexdigest()},
               "settings": {"settle_steps": 240, "stable_window_steps": 60}, **overrides}
    path = tmp_path / "job.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path, payload


def test_import_never_bootstraps_isaac_or_openusd() -> None:
    command = [sys.executable, "-c",
               "import sys; import scene_factory.runtime_worker; "
               "assert not any(n == 'pxr' or n.startswith('pxr.') or "
               "n == 'omni' or n.startswith('omni.') or "
               "n == 'isaacsim' or n.startswith('isaacsim.') for n in sys.modules)"]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_load_job_preserves_absolute_paths_and_frozen_bindings(tmp_path: Path) -> None:
    path, payload = _job(tmp_path)
    job = load_job(path)
    assert job.layout_path == Path(payload["layout_path"])
    assert job.usd_path == Path(payload["usd_path"])
    assert job.settings["physics_dt"] == 1.0 / 60.0
    assert job.settings["max_steps"] == 720
    assert job.bindings == payload["bindings"]
    assert not job.output_dir.exists(), "validation should not create runtime evidence"


@pytest.mark.parametrize("override, message", [
    ({"layout_path": "layout.json"}, "absolute path"),
    ({"output_dir": "evidence"}, "absolute path"),
    ({"mode": "fake"}, "physics or task"),
    ({"bindings": {}}, "frozen fingerprint"),
    ({"settings": {"physics_dt": 0}}, "finite and positive"),
    ({"settings": {"physics_dt": math.inf}}, "finite and positive"),
    ({"settings": {"max_steps": True}}, "positive integer"),
    ({"settings": {"settle_steps": 10, "stable_window_steps": 20}}, "must not exceed"),
])
def test_invalid_job_is_rejected_before_simulator_import(
    tmp_path: Path, override: dict, message: str,
) -> None:
    path, _ = _job(tmp_path, **override)
    with pytest.raises(ValueError, match=message):
        load_job(path)


def test_non_ascii_output_is_rejected(tmp_path: Path) -> None:
    path, _ = _job(tmp_path, output_dir=str(tmp_path / "运行证据"))
    with pytest.raises(ValueError, match="ASCII-only"):
        load_job(path)


def test_json_trace_rejects_nonfinite_values_and_preserves_prefix(tmp_path: Path) -> None:
    writer = TraceWriter(tmp_path / "physics_trace.jsonl")
    frame = {"step": 0, "timestamp": 0.0, "objects": {"mug": {"position": [0.1, 0.2, 0.8]}}}
    writer.write(frame)
    with pytest.raises(ValueError):
        writer.write({"step": 1, "value": float("nan")})
    writer.close()
    assert writer.rows == 1
    assert json.loads(writer.path.read_text(encoding="utf-8")) == frame
    with pytest.raises(FileExistsError):
        TraceWriter(writer.path)


def test_atomic_report_uses_real_json_and_absolute_paths(tmp_path: Path) -> None:
    report_path = tmp_path / "worker_report.json"
    payload = {"schema_version": WORKER_SCHEMA, "trace_path": tmp_path / "task_trace.jsonl",
               "failure_reason": "接触检查失败\nretain prefix", "result": "worker_error"}
    _atomic_json(report_path, payload)
    read = json.loads(report_path.read_text(encoding="utf-8"))
    assert read["trace_path"] == str(payload["trace_path"])
    assert read["failure_reason"] == payload["failure_reason"]
    assert not report_path.with_name("worker_report.json.tmp").exists()
    with pytest.raises(TypeError):
        _json_line({"bad": object()})


def test_code_fingerprint_covers_actual_python_sources(tmp_path: Path) -> None:
    from scene_factory.dataset import canonical_json

    first = tmp_path / "worker.py"
    nested = tmp_path / "backend"
    nested.mkdir()
    second = nested / "controller.py"
    first.write_text("version = 1\n", encoding="utf-8")
    second.write_text("phase = 'PRE_GRASP'\n", encoding="utf-8")
    (tmp_path / "unrelated.log").write_text("ignored", encoding="utf-8")
    expected = {path.relative_to(tmp_path).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (first, second)}
    fingerprint = _observed_code_fingerprint(tmp_path)
    assert fingerprint == hashlib.sha256(canonical_json(expected)).hexdigest()
    second.write_text("phase = 'FAILED'\n", encoding="utf-8")
    assert _observed_code_fingerprint(tmp_path) != fingerprint


def test_frozen_inputs_detect_mid_run_changes(tmp_path: Path) -> None:
    path, _ = _job(tmp_path)
    job = load_job(path)
    report = {}
    _observe_frozen_inputs(job, report)
    _observe_frozen_inputs(job, report, final=True)
    assert report["observed_code_sha256"] == report["final_code_sha256"]
    assert report["observed_inputs"] == report["final_inputs"]
    job.layout_path.write_text('{"changed": true}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="inputs changed during"):
        _observe_frozen_inputs(job, report, final=True)


def test_wrong_code_binding_fails_before_simulator_start(tmp_path: Path) -> None:
    path, payload = _job(tmp_path, bindings={"code_sha256": "0" * 64})
    result = subprocess.run([sys.executable, "-m", "scene_factory.runtime_worker", str(path)],
                            capture_output=True, text=True, check=False, timeout=30)
    assert result.returncode == 1
    report = json.loads((Path(payload["output_dir"]) / "worker_report.json").read_text(encoding="utf-8"))
    assert report["result"] == "worker_error"
    assert report["observed_code_sha256"] != payload["bindings"]["code_sha256"]
    assert "frozen code binding" in report["failure_reason"]
    assert report["trace_rows"] == 0
    assert report["isaac_sim_version"] == "unknown"
    identity_path = Path(payload["output_dir"]) / "worker_identity.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    assert identity["schema_version"] == IDENTITY_SCHEMA
    assert identity["run_id"] == payload["run_id"]
    assert identity["case_id"] == payload["case_id"]
    assert identity["mode"] == payload["mode"]
    assert identity["bindings"] == payload["bindings"]
    assert identity["pid"] == report["pid"]
    assert identity["parent_pid"] == report["parent_pid"]
    assert identity["job_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert identity["observed_code_sha256"] == report["observed_code_sha256"]
    assert report["worker_identity_sha256"] == hashlib.sha256(identity_path.read_bytes()).hexdigest()


def test_live_rigid_getters_keep_native_angular_units() -> None:
    class LiveView:
        count = 1

        def is_physics_handle_valid(self):
            return True

        def get_world_poses(self):
            return [[0.2, 0.1, 0.9]], [[1.0, 0.0, 0.0, 0.0]]

        def get_linear_velocities(self):
            return [[0.1, -0.2, 0.3]]

        def get_angular_velocities(self):
            return [[math.pi, -0.1, 0.0]]

    state = _live_rigid_state(LiveView(), "mug")
    assert state["position"] == [0.2, 0.1, 0.9]
    assert state["orientation_wxyz"] == [1.0, 0.0, 0.0, 0.0]
    assert state["linear_velocity_mps"] == [0.1, -0.2, 0.3]
    assert state["angular_velocity_rps"] == [math.pi, -0.1, 0.0]
    assert state["physics_handle_valid"] is True
    assert state["angular_velocity_source_unit"] == "rad/s"
    assert state["velocity_source"] == "PhysX tensor API"


def test_invalid_live_handle_refuses_all_usd_fallback_getters() -> None:
    class InvalidView:
        def is_physics_handle_valid(self):
            return False

        def get_world_poses(self):
            raise AssertionError("a getter could fallback to USD; it must never be called")

    with pytest.raises(RuntimeError, match="no live PhysX handle"):
        _live_rigid_state(InvalidView(), "mug")
    with pytest.raises(RuntimeError, match="no live PhysX handle"):
        _live_rigid_state(None, "mug")


def test_motion_view_explicitly_disables_contact_preparation_without_resetting_transforms() -> None:
    captured = {}
    sentinel = object()

    def factory(**options):
        captured.update(options)
        return sentinel

    assert _new_rigid_view(factory, "/World/Objects/mug_1", "mug_1") is sentinel
    assert captured == {"prim_paths_expr": "/World/Objects/mug_1", "name": "evidence_mug_1",
                        "reset_xform_properties": False, "prepare_contact_sensors": False,
                        "track_contact_forces": False, "disable_stablization": False}


def test_usd_bounds_pose_sync_accepts_quaternion_sign_and_rejects_stale_pose() -> None:
    live = {"position": [0.2, 0.1, 0.9], "orientation_wxyz": [1.0, 0.0, 0.0, 0.0]}
    sync = _check_usd_pose_sync(live, [0.2, 0.1, 0.9], [-1.0, 0.0, 0.0, 0.0], "mug")
    assert sync["usd_pose_position_error_m"] == 0.0
    assert sync["usd_pose_orientation_error_rad"] == 0.0
    with pytest.raises(RuntimeError, match="USD bounds pose is stale"):
        _check_usd_pose_sync(live, [0.2, 0.1, 0.7], [1.0, 0.0, 0.0, 0.0], "mug")
    with pytest.raises(RuntimeError, match="USD bounds pose is stale"):
        _check_usd_pose_sync(live, [0.2, 0.1, 0.9], [0.0, 1.0, 0.0, 0.0], "mug")


@pytest.mark.skipif(importlib.util.find_spec("isaacsim") is not None,
                    reason="test needs an ordinary Python without Isaac runtime")
def test_missing_real_runtime_persists_failure_report_with_nonzero_exit(tmp_path: Path) -> None:
    path, payload = _job(tmp_path)
    result = subprocess.run([sys.executable, "-m", "scene_factory.runtime_worker", str(path)],
                            capture_output=True, text=True, check=False, timeout=30)
    assert result.returncode != 0
    directory = Path(payload["output_dir"])
    report = json.loads((directory / "worker_report.json").read_text(encoding="utf-8"))
    assert report["schema_version"] == WORKER_SCHEMA
    assert report["bindings"] == payload["bindings"]
    assert report["result"] == "worker_error"
    assert "isaacsim" in report["failure_reason"]
    assert report["completed_steps"] == 0
    assert report["trace_rows"] == 0
    assert report["trace_sha256"] == hashlib.sha256(b"").hexdigest()
    assert report["worker_exit_code"] == 1
    assert Path(report["trace_path"]).read_bytes() == b""
