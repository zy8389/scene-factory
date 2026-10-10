"""Isolated Isaac workers that persist real simulation evidence before shutdown.

No Isaac, omni, or pxr module is imported until SimulationApp is constructed.
The parent owns process exit validation and the final quality decision; a worker
report only describes the execution and binds its trace to the submitted job.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import traceback
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any


JOB_SCHEMA = "scene_factory.runtime_job.v1"
WORKER_SCHEMA = "scene_factory.runtime_worker.v1"
IDENTITY_SCHEMA = "scene_factory.runtime_identity.v1"


@dataclass(frozen=True)
class RuntimeJob:
    run_id: str
    case_id: str
    mode: str
    layout_path: Path
    usd_path: Path
    output_dir: Path
    bindings: dict[str, Any]
    settings: dict[str, Any]
    runtime_assets: dict[str, Any] = field(default_factory=dict)
    job_path: Path | None = None


def _absolute_path(value: Any, field: str, *, must_exist: bool = False) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a nonempty absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{field} must be an absolute path")
    # Resolving a junction would destroy the ASCII Isaac environment spelling.
    path = Path(os.path.abspath(os.fspath(path)))
    if must_exist and not path.is_file():
        raise FileNotFoundError(f"{field} does not exist: {path}")
    return path


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"settings.{name} must be a positive integer")
    return value


def load_job(path: str | Path) -> RuntimeJob:
    job_path = _absolute_path(os.fspath(path), "job_path", must_exist=True)
    raw = json.loads(job_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != JOB_SCHEMA:
        raise ValueError(f"job.schema_version must be {JOB_SCHEMA!r}")
    for name in ("run_id", "case_id"):
        if not isinstance(raw.get(name), str) or not raw[name].strip():
            raise ValueError(f"job.{name} must be a nonempty string")
    if raw.get("mode") not in {"physics", "task"}:
        raise ValueError("job.mode must be physics or task")
    layout = _absolute_path(raw.get("layout_path"), "layout_path", must_exist=True)
    usd = _absolute_path(raw.get("usd_path"), "usd_path", must_exist=True)
    output = _absolute_path(raw.get("output_dir"), "output_dir")
    if not str(output).isascii():
        raise ValueError("output_dir must use an ASCII-only path")
    if sys.platform == "win32" and not str(usd).isascii():
        raise ValueError("usd_path must use an ASCII-only path on Windows")
    if output.exists() and not output.is_dir():
        raise ValueError("output_dir must be a directory")
    bindings = raw.get("bindings")
    if not isinstance(bindings, dict) or not bindings:
        raise ValueError("job.bindings must be a nonempty frozen fingerprint dictionary")
    settings = raw.get("settings", {})
    if not isinstance(settings, dict):
        raise ValueError("job.settings must be a dictionary")
    settings = {"physics_dt": 1.0 / 60.0, "settle_steps": 240,
                "stable_window_steps": 60, "max_steps": 720, **settings}
    dt = settings["physics_dt"]
    if isinstance(dt, bool) or not isinstance(dt, (int, float)) or not math.isfinite(dt) or dt <= 0:
        raise ValueError("settings.physics_dt must be finite and positive")
    for name in ("settle_steps", "stable_window_steps", "max_steps"):
        _positive_int(settings[name], name)
    if settings["stable_window_steps"] > settings["settle_steps"]:
        raise ValueError("settings.stable_window_steps must not exceed settle_steps")
    # Strict JSON roundtripping freezes values and rejects malformed NaN bindings.
    bindings = json.loads(json.dumps(bindings, allow_nan=False))
    settings = json.loads(json.dumps(settings, allow_nan=False))
    runtime_assets = raw.get("runtime_assets", {})
    if not isinstance(runtime_assets, dict):
        raise ValueError("job.runtime_assets must be a dictionary")
    for key in ("asset_root", "robot_asset_root", "robot_asset_path", "path"):
        if runtime_assets.get(key):
            asset_path = _absolute_path(runtime_assets[key], f"runtime_assets.{key}")
            if not str(asset_path).isascii():
                raise ValueError(f"runtime_assets.{key} must use an ASCII-only path")
            runtime_assets[key] = str(asset_path)
    return RuntimeJob(raw["run_id"], raw["case_id"], raw["mode"], layout, usd,
                      output, bindings, settings, runtime_assets, job_path)


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    # Numpy values are produced by real Isaac observations; avoid importing
    # numpy, let alone simulator modules, just to import this worker.
    if hasattr(value, "tolist"):
        return value.tolist()
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"unsupported evidence value: {type(value).__name__}")


def _json_line(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      default=_json_default, separators=(",", ":")) + "\n"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _observed_code_fingerprint(root: Path | None = None) -> str:
    """Hash the implementation actually available to this child process.

    Keep the canonical format shared with the parent's code binding without
    importing its orchestration module or accepting its asserted fingerprint.
    """
    from .dataset import canonical_json

    root = root or Path(__file__).parent
    files = {path.relative_to(root).as_posix(): _sha256(path)
             for path in sorted(root.rglob("*.py")) if not path.is_symlink()}
    return hashlib.sha256(canonical_json(files)).hexdigest()


def _observe_frozen_inputs(job: RuntimeJob, report: dict[str, Any], *, final: bool = False) -> None:
    prefix = "final_" if final else "observed_"
    code_hash = _observed_code_fingerprint()
    report[prefix + "code_sha256"] = code_hash
    expected_code = job.bindings.get("code_sha256")
    if expected_code is not None and code_hash != expected_code:
        raise RuntimeError("worker execution code differs from frozen code binding")
    if final and code_hash != report.get("observed_code_sha256"):
        raise RuntimeError("worker execution code changed during this run")
    inputs = {"layout_sha256": _sha256(job.layout_path), "usd_sha256": _sha256(job.usd_path)}
    report[prefix + "inputs"] = inputs
    expected_layout = job.bindings.get("input_sha256", {}).get("layout")
    if expected_layout is not None and inputs["layout_sha256"] != expected_layout:
        raise RuntimeError("layout input differs from frozen layout binding")
    expected_usd = job.bindings.get("usd_sha256")
    if expected_usd is not None and inputs["usd_sha256"] != expected_usd:
        raise RuntimeError("USD input differs from frozen USD binding")
    if final and inputs != report.get("observed_inputs"):
        raise RuntimeError("simulation inputs changed during this run")
    robot_path = job.runtime_assets.get("robot_asset_path") or job.runtime_assets.get("path")
    if robot_path:
        robot_hash = _sha256(Path(robot_path))
        report[prefix + "robot_asset_sha256"] = robot_hash
        expected_robot = job.runtime_assets.get("robot_asset_sha256")
        if expected_robot is not None and robot_hash != expected_robot:
            raise RuntimeError("robot asset differs from frozen binary binding")
        if final and robot_hash != report.get("observed_robot_asset_sha256"):
            raise RuntimeError("robot asset changed during this run")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(_json_line(payload))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class TraceWriter:
    def __init__(self, path: Path):
        self.path = path
        self.stream = path.open("x", encoding="utf-8", newline="\n")
        self.rows = 0

    def write(self, frame: dict[str, Any]) -> None:
        line = _json_line(frame)
        self.stream.write(line)
        self.stream.flush()
        self.rows += 1

    def close(self) -> None:
        if not self.stream.closed:
            self.stream.flush()
            os.fsync(self.stream.fileno())
            self.stream.close()


def _base_report(job: RuntimeJob) -> dict[str, Any]:
    return {"schema_version": WORKER_SCHEMA, "run_id": job.run_id,
            "case_id": job.case_id, "mode": job.mode, "bindings": job.bindings,
            "pid": os.getpid(), "parent_pid": os.getppid(), "isaac_sim_version": "unknown",
            "robot_asset_source": None, "completed_steps": 0,
            "trace_path": str(job.output_dir / f"{job.mode}_trace.jsonl"),
            "trace_sha256": None, "result": "worker_error", "failure_reason": None,
            "runtime_summary": {}, "settings": job.settings,
            "runtime_assets": job.runtime_assets, "observed_code_sha256": None}


def _isaac_version() -> str:
    # Only called after app construction.
    module = sys.modules.get("isaacsim")
    value = getattr(module, "__version__", None)
    if value:
        return str(value)
    try:
        return version("isaacsim")
    except PackageNotFoundError:
        module_path = getattr(module, "__file__", None)
        if module_path:
            path = Path(module_path).with_name("VERSION")
            if path.is_file():
                return path.read_text(encoding="utf-8").strip()
        return "unknown"


def _map_objects(stage: Any, scene: dict[str, Any], UsdPhysics: Any) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for prim in stage.Traverse():
        attr = prim.GetAttribute("sceneFactory:objectId")
        object_id = attr.Get() if attr.IsValid() else None
        if object_id:
            object_id = str(object_id)
            if object_id in mapping:
                raise RuntimeError(f"duplicate sceneFactory:objectId: {object_id}")
            mapping[object_id] = prim
    expected = {item["object_id"]: item for item in scene["objects"]}
    if not expected or set(mapping) != set(expected):
        raise RuntimeError(f"USD/layout object mapping mismatch: {sorted(mapping)} vs {sorted(expected)}")
    for object_id, item in expected.items():
        if item.get("dynamic") and not mapping[object_id].HasAPI(UsdPhysics.RigidBodyAPI):
            raise RuntimeError(f"dynamic object has no root RigidBodyAPI: {object_id}")
    if not any(item.get("dynamic") for item in expected.values()):
        raise RuntimeError("physics job requires at least one dynamic layout object")
    return mapping


def _vector(value: Any, size: int = 3) -> list[float]:
    result = [float(value[index]) for index in range(size)]
    if not all(math.isfinite(number) for number in result):
        raise RuntimeError("simulation produced a nonfinite vector")
    return result


def _new_rigid_view(factory: Any, prim_path: str, object_id: str) -> Any:
    # SingleRigidPrim hides these defaults and prepares contact sensors,
    # thereby authoring sleepThreshold=0 even when only reading motion.
    return factory(prim_paths_expr=prim_path, name=f"evidence_{object_id}",
                   reset_xform_properties=False, prepare_contact_sensors=False,
                   track_contact_forces=False, disable_stablization=False)


def _sleep_threshold_state(prim: Any) -> dict[str, Any]:
    attr = prim.GetAttribute("physxRigidBody:sleepThreshold")
    exists = attr.IsValid()
    value = attr.Get() if exists else None
    return {"attribute_exists": bool(exists),
            "authored": bool(attr.HasAuthoredValueOpinion()) if exists else False,
            "value": float(value) if value is not None else None}


def _verify_sleep_thresholds(before: dict[str, Any], mapping: dict[str, Any]) -> dict[str, Any]:
    after = {object_id: _sleep_threshold_state(mapping[object_id]) for object_id in before}
    if after != before:
        raise RuntimeError("motion measurement changed authored rigid-body sleep thresholds")
    return after


def _live_rigid_state(view: Any, object_id: str) -> dict[str, Any]:
    """Read an initialized PhysX handle; never accept the USD fallback path."""
    is_valid = getattr(view, "is_physics_handle_valid", None)
    if not callable(is_valid) or not is_valid():
        raise RuntimeError(f"no live PhysX handle for dynamic object: {object_id}")
    if view.count != 1:
        raise RuntimeError(f"motion view must map exactly one dynamic object: {object_id}")
    positions, orientations = view.get_world_poses()
    state = {"position": _vector(positions[0]), "orientation_wxyz": _vector(orientations[0], 4),
             "linear_velocity_mps": _vector(view.get_linear_velocities()[0]),
             # The live tensor API already returns rad/s. Only USD diagnostic
             # angularVelocity values have units of degrees/s.
             "angular_velocity_rps": _vector(view.get_angular_velocities()[0])}
    if not is_valid():
        raise RuntimeError(f"PhysX handle invalidated while reading dynamic object: {object_id}")
    return {**state, "physics_handle_valid": True,
            "pose_source": "PhysX tensor API", "velocity_source": "PhysX tensor API",
            "angular_velocity_source_unit": "rad/s", "physics_view_class": "RigidPrim"}


def _check_usd_pose_sync(live: dict[str, Any], usd_position: list[float],
                         usd_orientation: list[float], object_id: str) -> dict[str, float]:
    position_error = math.sqrt(sum((a - b) ** 2
                                  for a, b in zip(live["position"], usd_position, strict=True)))
    live_q = live["orientation_wxyz"]
    live_norm = math.sqrt(sum(value ** 2 for value in live_q))
    usd_norm = math.sqrt(sum(value ** 2 for value in usd_orientation))
    if live_norm < 1e-8 or usd_norm < 1e-8:
        raise RuntimeError(f"invalid quaternion while checking USD/PhysX sync: {object_id}")
    dot = sum(a * b for a, b in zip(live_q, usd_orientation, strict=True)) / (live_norm * usd_norm)
    orientation_error = 2.0 * math.acos(min(1.0, abs(dot)))
    # These are numerical synchronization guards, not settling thresholds.
    # A stale USD pose would also make its world bounding box stale.
    if position_error > 1e-4 or orientation_error > 1e-3:
        raise RuntimeError(f"USD bounds pose is stale for {object_id}: "
                           f"position_error_m={position_error}, orientation_error_rad={orientation_error}")
    return {"usd_pose_position_error_m": position_error,
            "usd_pose_orientation_error_rad": orientation_error}


def _physics_frame(stage: Any, mapping: dict[str, Any], scene: dict[str, Any],
                   step: int, dt: float, Gf: Any, Usd: Any, UsdGeom: Any,
                   UsdPhysics: Any, rigid_bodies: dict[str, Any]) -> dict[str, Any]:
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(),
                            [UsdGeom.Tokens.default_, UsdGeom.Tokens.render,
                             UsdGeom.Tokens.proxy], useExtentsHint=True)
    objects: dict[str, Any] = {}
    for object_id, prim in mapping.items():
        matrix = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        quat = Gf.Transform(matrix).GetRotation().GetQuat()
        imaginary = quat.GetImaginary()
        usd_orientation = [float(quat.GetReal()), *(_vector(imaginary))]
        usd_position = _vector(matrix.ExtractTranslation())
        bounds = cache.ComputeWorldBound(prim).ComputeAlignedRange()
        if bounds.IsEmpty():
            raise RuntimeError(f"object has empty world bounds: {object_id}")
        dynamic = prim.HasAPI(UsdPhysics.RigidBodyAPI)
        if dynamic:
            state = _live_rigid_state(rigid_bodies.get(object_id), object_id)
            sync = _check_usd_pose_sync(state, usd_position, usd_orientation, object_id)
            api = UsdPhysics.RigidBodyAPI(prim)
            raw_linear = api.GetVelocityAttr().Get()
            raw_angular = api.GetAngularVelocityAttr().Get()
            diagnostics = {"usd_linear_velocity_mps": _vector(raw_linear) if raw_linear is not None else None,
                           "usd_angular_velocity_deg_s": _vector(raw_angular) if raw_angular is not None else None,
                           "usd_world_position_m": usd_position,
                           "usd_orientation_wxyz": usd_orientation, **sync}
        else:
            state = {"position": usd_position, "orientation_wxyz": usd_orientation,
                     "linear_velocity_mps": [0.0] * 3, "angular_velocity_rps": [0.0] * 3,
                     "pose_source": "USD static transform", "velocity_source": "static object"}
            diagnostics = {}
        objects[object_id] = {**state, **diagnostics,
                              "world_bbox_min": _vector(bounds.GetMin()),
                              "world_bbox_max": _vector(bounds.GetMax()),
                              "dynamic": dynamic, "prim_path": str(prim.GetPath())}
    return {"step": step, "timestamp": step * dt, "objects": objects}


def _export_settled(stage: Any, destination: Path, mapping: dict[str, Any],
                    scene: dict[str, Any], Gf: Any, Sdf: Any, Usd: Any,
                    UsdPhysics: Any, UsdUtils: Any) -> dict[str, Any]:
    # Flatten incorporates current, PhysX-updated transforms and anchors asset
    # paths. Exporting the original root layer elsewhere breaks relative refs.
    for prim in stage.Traverse():
        if prim.HasAPI(UsdPhysics.RigidBodyAPI):
            api = UsdPhysics.RigidBodyAPI(prim)
            api.GetVelocityAttr().Set(Gf.Vec3f(0.0))
            api.GetAngularVelocityAttr().Set(Gf.Vec3f(0.0))
    if not stage.Flatten().Export(str(destination)):
        raise RuntimeError("failed to export settled USD")
    reopened = Usd.Stage.Open(str(destination))
    if reopened is None or reopened.GetCompositionErrors():
        raise RuntimeError("settled USD has unresolved composition references")
    reloaded_mapping = _map_objects(reopened, scene, UsdPhysics)
    if set(reloaded_mapping) != set(mapping):
        raise RuntimeError("settled USD lost object mappings")
    layers, assets, unresolved = UsdUtils.ComputeAllDependencies(Sdf.AssetPath(str(destination)))
    if unresolved:
        raise RuntimeError(f"settled USD has unresolved asset paths: {list(unresolved)}")
    return {"settled_usd_path": str(destination), "settled_usd_sha256": _sha256(destination),
            "settled_dependency_check": {"layer_count": len(layers),
                                         "asset_count": len(assets), "unresolved": []},
            "settled_velocity_cleared": True}


def _run_physics(job: RuntimeJob, report: dict[str, Any], writer: TraceWriter,
                 resources: dict[str, Any]) -> None:
    from .isaac_runtime import load_simulation_app

    SimulationApp = load_simulation_app()
    original_argv = sys.argv
    sys.argv = [sys.argv[0]]
    try:
        resources["app"] = SimulationApp({"headless": True, "hide_ui": True,
            "renderer": "Minimal", "minimal_shading_mode": 4, "anti_aliasing": 0,
            "multi_gpu": False, "max_gpu_count": 1, "width": 640, "height": 480,
            "disable_viewport_updates": True, "fast_shutdown": True,
            "open_usd": job.usd_path.as_posix()})
    finally:
        sys.argv = original_argv
    report["isaac_sim_version"] = _isaac_version()
    import omni.usd
    from isaacsim.core.api import SimulationContext
    from isaacsim.core.prims import RigidPrim
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdUtils

    stage = omni.usd.get_context().get_stage()
    if stage is None or not os.path.samefile(stage.GetRootLayer().realPath, job.usd_path):
        raise RuntimeError("Isaac Sim did not open the requested USD stage")
    if not math.isclose(UsdGeom.GetStageMetersPerUnit(stage), 1.0):
        raise RuntimeError("runtime evidence requires stage units in meters")
    scene = json.loads(job.layout_path.read_text(encoding="utf-8"))
    mapping = _map_objects(stage, scene, UsdPhysics)
    dt = float(job.settings["physics_dt"])
    simulation = SimulationContext(physics_dt=dt, rendering_dt=dt,
        stage_units_in_meters=1.0, physics_prim_path="/World/PhysicsScene", stage=stage)
    resources["simulation"] = simulation
    context = simulation.get_physics_context()
    context.set_physx_update_transformations_settings(update_to_usd=True,
                                                     update_velocities_to_usd=True)
    dynamic_mapping = {object_id: prim for object_id, prim in mapping.items()
                       if prim.HasAPI(UsdPhysics.RigidBodyAPI)}
    sleep_before = {object_id: _sleep_threshold_state(prim) for object_id, prim in dynamic_mapping.items()}
    report["runtime_summary"] = {"sleep_threshold_before": sleep_before}
    rigid_bodies = {object_id: _new_rigid_view(RigidPrim, str(prim.GetPath()), object_id)
                    for object_id, prim in dynamic_mapping.items()}
    resources["rigid_bodies"] = rigid_bodies
    report["runtime_summary"]["sleep_threshold_after_view_construction"] = _verify_sleep_thresholds(sleep_before, mapping)
    simulation.initialize_physics()
    simulation.play()
    for body in rigid_bodies.values():
        body.initialize()
    report["runtime_summary"]["sleep_threshold_after_initialization"] = _verify_sleep_thresholds(sleep_before, mapping)
    writer.write(_physics_frame(stage, mapping, scene, 0, dt, Gf, Usd, UsdGeom, UsdPhysics, rigid_bodies))
    for step in range(1, job.settings["settle_steps"] + 1):
        simulation.step(render=False)
        writer.write(_physics_frame(stage, mapping, scene, step, dt, Gf, Usd, UsdGeom, UsdPhysics, rigid_bodies))
        report["completed_steps"] = step
    report["runtime_summary"] = {"physics_engine": "PhysX", "physics_dt": dt,
        "steps": report["completed_steps"], "trace_rows": writer.rows,
        "object_prims": {key: str(prim.GetPath()) for key, prim in mapping.items()},
        "pose_source": "PhysX tensor API", "velocity_source": "PhysX tensor API",
        "angular_velocity_source_unit": "rad/s", "live_physics_handles_valid": True,
        "physics_view_class": "RigidPrim", "measurement_contact_sensors_prepared": False,
        "usd_velocity_diagnostic_only": True, "usd_pose_sync_checked": True,
        "invalid_handle_policy": "worker_error_no_usd_fallback",
        "sleep_threshold_before": sleep_before,
        "sleep_threshold_after": _verify_sleep_thresholds(sleep_before, mapping),
        "sleep_threshold_unchanged": True,
        **_export_settled(stage, job.output_dir / "settled.usda", mapping, scene,
                          Gf, Sdf, Usd, UsdPhysics, UsdUtils)}
    report["result"] = "completed"


def _task_frame(observation: dict[str, Any], step: int, reward: float,
                terminated: bool, truncated: bool, summary: dict[str, Any]) -> dict[str, Any]:
    return {**observation, "step": step, "reward": float(reward),
            "terminated": bool(terminated), "truncated": bool(truncated),
            "runtime_summary": summary}


def _run_task(job: RuntimeJob, report: dict[str, Any], writer: TraceWriter,
              resources: dict[str, Any]) -> None:
    # The backend is pure Python on import. Its reset constructs SimulationApp
    # before any pxr/omni use, following the same bootstrap order as physics.
    from .backends.isaac import IsaacSimBackend

    class EvidenceBackend(IsaacSimBackend):
        """Defer reset's emergency fast shutdown until evidence is persisted."""
        def close(self) -> None:
            if getattr(self, "_worker_can_close", False):
                super().close()

        def _initialize_runtime(self) -> None:
            asset_root = job.runtime_assets.get("asset_root") or job.runtime_assets.get("robot_asset_root")
            if asset_root:
                # SimulationApp already exists at this point. Settings are
                # process local; never save a user-wide asset root config.
                import carb

                carb.settings.get_settings().set_string(
                    "/persistent/isaac/asset_root/default", Path(asset_root).as_posix())
            super()._initialize_runtime()

    backend = EvidenceBackend(job.usd_path, headless=True,
        max_steps=job.settings["max_steps"], physics_dt=float(job.settings["physics_dt"]),
        enable_rgbd=False)
    resources["backend"] = backend
    scene = json.loads(job.layout_path.read_text(encoding="utf-8"))
    observation, reset_info = backend.reset(scene)
    summary = backend.runtime_summary
    report["isaac_sim_version"] = backend.isaac_sim_version
    report["robot_asset_source"] = summary.get("robot_asset_source")
    report["runtime_summary"] = {**summary, "reset_info": reset_info}
    writer.write(_task_frame(observation, 0, 0.0, False, False, summary))
    if report["robot_asset_source"] != "nucleus_franka_usd":
        raise RuntimeError(f"official Franka asset required; got {report['robot_asset_source']!r}")
    loaded_asset_path = _absolute_path(summary.get("franka_usd"), "loaded franka_usd", must_exist=True)
    expected_asset_path = job.runtime_assets.get("robot_asset_path") or job.runtime_assets.get("path")
    if expected_asset_path and not os.path.samefile(loaded_asset_path, expected_asset_path):
        raise RuntimeError("loaded Franka USD differs from frozen runtime asset")
    report["official_isaac_asset"] = bool(summary.get("official_isaac_asset"))
    report["franka_usd"] = str(loaded_asset_path)
    report["loaded_asset_sha256"] = _sha256(loaded_asset_path)
    expected_robot_hash = job.runtime_assets.get("robot_asset_sha256")
    if expected_robot_hash and report["loaded_asset_sha256"] != expected_robot_hash:
        raise RuntimeError("actually loaded Franka binary differs from frozen asset hash")
    terminated = truncated = False
    for step in range(1, job.settings["max_steps"] + 1):
        observation, reward, terminated, truncated, summary = backend.step("scripted")
        writer.write(_task_frame(observation, step, reward, terminated, truncated, summary))
        report["completed_steps"] = step
        report["runtime_summary"] = summary
        if terminated or truncated:
            break
    report["result"] = ("task_success" if observation.get("task_success") and terminated
                        else "truncated" if truncated else "task_failure")
    report["failure_reason"] = summary.get("failure_reason")
    if not terminated and not truncated:
        report["result"] = "truncated"
        report["failure_reason"] = "worker_max_steps"
    report["runtime_summary"] = {**summary, "official_isaac_asset": True,
                                  "trace_rows": writer.rows, "reset_info": reset_info}


def run_job(job: RuntimeJob) -> int:
    job.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = job.output_dir / "worker_report.json"
    if report_path.exists():
        raise FileExistsError(f"refusing to overwrite worker evidence: {report_path}")
    identity_path = job.output_dir / "worker_identity.json"
    if identity_path.exists():
        raise FileExistsError(f"refusing to overwrite worker identity: {identity_path}")
    report = _base_report(job)
    # A Windows venv python launcher may spawn the actual interpreter under a
    # different PID. Give the parent an early identity while both processes
    # are alive, so it can independently verify their OS process relationship.
    identity = {"schema_version": IDENTITY_SCHEMA, "run_id": job.run_id,
                "case_id": job.case_id, "mode": job.mode, "pid": os.getpid(),
                "parent_pid": os.getppid(), "bindings": job.bindings,
                "job_sha256": _sha256(job.job_path) if job.job_path else None,
                "observed_code_sha256": _observed_code_fingerprint()}
    _atomic_json(identity_path, identity)
    report["worker_identity_sha256"] = _sha256(identity_path)
    writer: TraceWriter | None = None
    resources: dict[str, Any] = {}
    exit_code = 0
    os.environ.setdefault("OMNI_KIT_ACCEPT_EULA", "YES")
    try:
        writer = TraceWriter(Path(report["trace_path"]))
        _observe_frozen_inputs(job, report)
        if job.mode == "physics":
            _run_physics(job, report, writer, resources)
        else:
            _run_task(job, report, writer, resources)
    except Exception as exc:
        exit_code = 1
        report["result"] = "worker_error"
        report["failure_reason"] = f"{type(exc).__name__}: {exc}"
        report["exception_traceback"] = traceback.format_exc()
        backend = resources.get("backend")
        if backend is not None:
            report["runtime_summary"] = backend.runtime_summary
            report["robot_asset_source"] = backend.runtime_summary.get("robot_asset_source")
            report["isaac_sim_version"] = backend.isaac_sim_version
    finally:
        try:
            _observe_frozen_inputs(job, report, final=True)
        except Exception as exc:
            exit_code = 1
            report["result"] = "worker_error"
            detail = f"{type(exc).__name__}: {exc}"
            report["failure_reason"] = "; ".join(
                reason for reason in (report.get("failure_reason"), detail) if reason)
        if writer is not None:
            writer.close()
            report["trace_sha256"] = _sha256(writer.path)
            report["trace_rows"] = writer.rows
        report["worker_exit_code"] = exit_code
        # fast_shutdown can exit the Python process. Flush the complete trace
        # and report before touching any simulator teardown API.
        _atomic_json(report_path, report)
        try:
            backend = resources.get("backend")
            if backend is not None:
                if exit_code and backend._app is not None:
                    # Isaac 6 supports preserving a nonzero status before its
                    # fast shutdown can replace it with an OS status of zero.
                    backend._app.close(exit_code=exit_code)
                backend._worker_can_close = True
                backend.close()
            else:
                if resources.get("simulation") is not None:
                    resources["simulation"].stop()
                if resources.get("app") is not None:
                    resources["app"].close(exit_code=exit_code)
        except Exception as exc:
            exit_code = 1
            report["shutdown_failure"] = f"{type(exc).__name__}: {exc}"
            report["worker_exit_code"] = 1
            _atomic_json(report_path, report)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Persist real Isaac simulation evidence for one job")
    parser.add_argument("job", type=Path)
    args = parser.parse_args(argv)
    try:
        job = load_job(args.job)
        return run_job(job)
    except Exception as exc:
        # Invalid job paths cannot safely identify an evidence directory. The
        # parent records this nonzero process result with captured stderr.
        print(f"runtime worker failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
