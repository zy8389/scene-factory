"""Sequential, source-bound Isaac acceptance runs for frozen sampled layouts.

The parent never imports Isaac/pxr. A worker's own PASS is insufficient: its
identity, input hashes, trace and actual process exit must all agree before the
recorded observations are assessed. Offline validation replays that assessment.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from .curation import configuration_fingerprint
from .dataset import DatasetError, canonical_json, sha256_file, write_json_atomic
from .isaac_runtime import export_usd_with_isaac, find_isaac_python, isaac_process_environment
from .models import CompiledScene
from .paths import default_registry_path
from .registry import AssetRegistry
from .sampling import validate_sampling_collection


RUNTIME_BATCH_SCHEMA_VERSION = "scene_factory.runtime_batch.v1"
RUNTIME_JOB_SCHEMA_VERSION = "scene_factory.runtime_job.v1"
RUNTIME_WORKER_SCHEMA_VERSION = "scene_factory.runtime_worker.v1"
RUNTIME_IDENTITY_SCHEMA_VERSION = "scene_factory.runtime_identity.v1"
RUNTIME_CASE_SCHEMA_VERSION = "scene_factory.runtime_case.v1"
_EVENTS = {"robot_mug_lift": "lift", "robot_mug_pick_place": "place"}
_DEFAULT_SETTINGS = {
    "physics_dt": 1.0 / 60.0, "settle_steps": 240, "stable_window_steps": 60,
    "max_linear_speed_mps": 0.01, "max_angular_speed_rps": 0.1,
    "max_position_drift_m": 0.005, "support_tolerance_m": 0.01, "max_steps": 720,
}
_RUNTIME_ASSET_KEYS = {"robot_asset_root", "robot_asset_path"}


def normalize_runtime_settings(settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    raw = dict(settings or {})
    if set(raw) - set(_DEFAULT_SETTINGS) - _RUNTIME_ASSET_KEYS:
        raise DatasetError("unknown runtime settings: " + ", ".join(sorted(set(raw) - set(_DEFAULT_SETTINGS) - _RUNTIME_ASSET_KEYS)))
    result = {**_DEFAULT_SETTINGS, **raw}
    for key in _DEFAULT_SETTINGS:
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise DatasetError(f"runtime {key} must be positive and finite")
        if key.endswith("steps") and not isinstance(value, int):
            raise DatasetError(f"runtime {key} must be an integer")
    if result["stable_window_steps"] > result["settle_steps"]:
        raise DatasetError("stable window exceeds settling duration")
    for key in _RUNTIME_ASSET_KEYS & result.keys():
        if not isinstance(result[key], str) or not result[key].strip():
            raise DatasetError(f"runtime {key} must be a local path")
        # Keep Windows junction spelling: resolving it reintroduces Unicode into Kit.
        result[key] = os.path.abspath(os.path.expanduser(result[key]))
    return result


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _safe_file(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise DatasetError("artifact path must be relative")
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise DatasetError(f"missing or unsafe artifact: {relative}")
    return path


def _code_fingerprint() -> str:
    root = Path(__file__).parent
    return _hash({path.relative_to(root).as_posix(): sha256_file(path)
                  for path in sorted(root.rglob("*.py")) if not path.is_symlink()})


def _select(collection: Path, limit: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    validation = validate_sampling_collection(collection).to_dict()
    if not validation.get("valid"):
        raise DatasetError("source collection invalid: " + repr(validation.get("errors", [])))
    groups: dict[str, list[dict[str, Any]]] = {}
    for selected in validation["selected"]:
        record = selected["record"]
        dataset = collection / selected["dataset_path"]
        layout = _read_json(_safe_file(dataset, record["files"]["layout"]))
        if layout.get("event") not in _EVENTS:
            raise DatasetError("runtime batch supports only frozen mug lift/place task pairs")
        fingerprint = configuration_fingerprint(layout)
        entry = {"case_id": f"{fingerprint[:20]}-{_EVENTS[layout['event']]}",
                 "configuration_fingerprint": fingerprint, "event": layout["event"],
                 "scene_id": record["scene_id"], "source": selected}
        groups.setdefault(fingerprint, []).append(entry)
    for fingerprint, members in groups.items():
        if len(members) != 2 or {member["event"] for member in members} != set(_EVENTS):
            raise DatasetError(f"configuration {fingerprint} must contain exactly one lift and one place task")
    ordered = [entry for fingerprint in sorted(groups)[:limit]
               for entry in sorted(groups[fingerprint], key=lambda item: item["event"])]
    all_entries = [entry for fingerprint in sorted(groups)
                   for entry in sorted(groups[fingerprint], key=lambda item: item["event"])]
    if not ordered or len({entry["case_id"] for entry in all_entries}) != len(all_entries):
        raise DatasetError("empty or ambiguous runtime selection")
    return validation, ordered


def _local_dependencies(path: Path) -> set[Path]:
    """Bind text USD references and model-directory files without loading pxr."""
    found: set[Path] = set()
    pending = [path]
    while pending:
        item = pending.pop()
        if item in found:
            continue
        if not item.is_file() or item.is_symlink():
            raise DatasetError(f"missing local asset dependency: {item}")
        found.add(item)
        if item.suffix.lower() not in {".usd", ".usda"}:
            continue
        try:
            content = item.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for reference in re.findall(r"@([^@]+)@", content):
            if "://" in reference or reference.startswith("${"):
                raise DatasetError("remote or unresolved asset dependencies cannot be frozen")
            dependent = Path(os.path.abspath(item.parent / reference))
            pending.append(dependent)
    return found


def _asset_context(entries: list[dict[str, Any]], collection: Path, settings: Mapping[str, Any]) -> dict[str, Any]:
    registry_path = default_registry_path()
    registry = AssetRegistry.load(registry_path)
    used: dict[str, Any] = {}
    binaries: dict[str, str] = {}
    for entry in entries:
        selected = entry["source"]
        layout = _read_json(collection / selected["dataset_path"] / selected["record"]["files"]["layout"])
        for item in CompiledScene.from_dict(layout).objects:
            asset = registry.get(item.asset_id)
            if (asset.category != item.category or len(asset.bbox_m) != len(item.bbox_m)
                    or any(not math.isclose(a, b, rel_tol=0.0, abs_tol=1e-9) for a, b in zip(asset.bbox_m, item.bbox_m))
                    or asset.status not in {"ready", "validated"} or not asset.collision_enabled
                    or (item.dynamic and not asset.rigid_body)):
                raise DatasetError(f"current asset is incompatible with frozen layout: {item.asset_id}")
            metadata = registry.metadata(item.asset_id).to_dict()
            for field, resolved in (("usd_path", registry.resolve_source_path(asset)),
                                    ("collision_path", registry.resolve_collision_path(asset))):
                if resolved:
                    if "://" in resolved:
                        raise DatasetError("runtime batch requires local frozen assets")
                    metadata[field] = os.path.abspath(resolved)
                    for dependent in _local_dependencies(Path(metadata[field])):
                        binaries[str(dependent)] = sha256_file(dependent)
            used[item.asset_id] = metadata
    runtime_assets = {key: settings[key] for key in _RUNTIME_ASSET_KEYS if key in settings}
    robot_path = runtime_assets.get("robot_asset_path")
    if not robot_path and runtime_assets.get("robot_asset_root"):
        robot_path = str(Path(runtime_assets["robot_asset_root"]) / "Isaac/Robots/FrankaRobotics/FrankaPanda/franka.usd")
        runtime_assets["robot_asset_path"] = robot_path
    if not robot_path:
        raise DatasetError("provide robot_asset_root or robot_asset_path to freeze the local official Franka asset")
    if robot_path:
        if not str(robot_path).isascii():
            raise DatasetError("official robot asset path must use an ASCII directory or junction")
        dependencies = _local_dependencies(Path(robot_path))
        dependencies.update(path for path in Path(robot_path).parent.rglob("*") if path.is_file())
        for dependent in dependencies:
            binaries[str(dependent)] = sha256_file(dependent)
        runtime_assets["robot_asset_sha256"] = sha256_file(robot_path)
    records = [used[key] for key in sorted(used)]
    context = {"registry_path": str(registry_path), "records": records,
            "current_registry_semantic_sha256": _hash([asdict(asset) for asset in sorted(registry.list(), key=lambda asset: asset.asset_id)]),
            "compatibility_basis": "frozen_layout_bbox_category_and_current_asset_binary",
            "binary_sha256": dict(sorted(binaries.items())), "runtime_assets": runtime_assets,
            "used_assets_sha256": _hash({"records": records, "binary_sha256": binaries, "runtime_assets": runtime_assets})}
    # Registry dataclasses contain tuples; persist and compare their JSON form.
    return json.loads(canonical_json(context))


def _not_verified(reason: str) -> dict[str, Any]:
    return {"status": "not_verified", "checks": {}, "metrics": {}, "failure_reasons": [reason]}


def _trace_samples(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    raw = _read_json(path)
    return raw if isinstance(raw, list) else raw["samples"]


def _process_parent_snapshot() -> dict[int, int]:
    """Read live Windows process ancestry independently of worker claims."""
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    processes: dict[int, int] = {}
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        success = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while success:
            processes[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            success = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() not in (0, 18):  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(snapshot)
    return processes


def _process_platform() -> str:
    return os.name


def _check_identity_contract(identity: Mapping[str, Any], job: Mapping[str, Any], job_sha256: str) -> None:
    if identity.get("schema_version") != RUNTIME_IDENTITY_SCHEMA_VERSION:
        raise DatasetError("worker_identity_schema_mismatch")
    for key in ("run_id", "case_id", "mode", "bindings"):
        if identity.get(key) != job[key]:
            raise DatasetError(f"worker_identity_{key}_mismatch")
    if identity.get("job_sha256") != job_sha256:
        raise DatasetError("worker_identity_job_sha256_mismatch")
    if identity.get("observed_code_sha256") != job["bindings"]["code_sha256"]:
        raise DatasetError("worker_identity_code_sha256_mismatch")
    for field in ("pid", "parent_pid"):
        value = identity.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise DatasetError(f"worker_identity_{field}_invalid")


def _capture_worker_lineage(stage_dir: Path, job: Mapping[str, Any], job_sha256: str,
                            launcher_pid: int, platform: str) -> dict[str, Any]:
    """Capture ancestry while the worker is live; offline validation uses this attestation."""
    path = _safe_file(stage_dir, "worker_identity.json")
    identity = _read_json(path)
    _check_identity_contract(identity, job, job_sha256)
    worker_pid = identity["pid"]
    lineage = []
    if platform == "nt":
        live = _process_parent_snapshot()
        if launcher_pid not in live or worker_pid not in live:
            raise DatasetError("worker_identity_process_not_live")
        if live[worker_pid] != identity["parent_pid"]:
            raise DatasetError("worker_identity_parent_pid_differs_from_live_process")
        pid = worker_pid
        seen = set()
        for _ in range(16):
            if pid in seen or pid not in live:
                raise DatasetError("worker_identity_live_lineage_incomplete")
            seen.add(pid)
            ppid = live[pid]
            lineage.append({"pid": pid, "parent_pid": ppid})
            if pid == launcher_pid or ppid == launcher_pid:
                break
            pid = ppid
        else:
            raise DatasetError("worker_identity_is_not_owned_descendant")
        if lineage[-1]["pid"] != launcher_pid and lineage[-1]["parent_pid"] != launcher_pid:
            raise DatasetError("worker_identity_is_not_owned_descendant")
        source = "windows_toolhelp32_snapshot"
    else:
        if worker_pid != launcher_pid or identity["parent_pid"] != os.getpid():
            raise DatasetError("worker_identity_does_not_match_owned_posix_process")
        lineage = [{"pid": launcher_pid, "parent_pid": os.getpid()}]
        source = "owned_popen_pid"
    return {"worker_pid": worker_pid, "worker_identity_sha256": sha256_file(path),
            "worker_lineage": {"verified": True, "source": source, "platform": platform,
                               "launcher_pid": launcher_pid, "worker_pid": worker_pid,
                               "observed_at_unix": time.time(), "lineage": lineage}}


def _validate_worker_lineage(stage_dir: Path, job: Mapping[str, Any], parent: Mapping[str, Any],
                             report: Mapping[str, Any]) -> None:
    """Check the recorded authoritative observation without querying dead/reused PIDs."""
    identity_path = _safe_file(stage_dir, "worker_identity.json")
    identity = _read_json(identity_path)
    _check_identity_contract(identity, job, parent["job_sha256"])
    digest = sha256_file(identity_path)
    if parent.get("worker_identity_sha256") != digest or report.get("worker_identity_sha256") != digest:
        raise DatasetError("worker_identity_sha256_mismatch")
    launcher_pid = parent.get("launcher_pid")
    worker_pid = parent.get("worker_pid")
    if (launcher_pid != parent.get("pid") or worker_pid != identity["pid"]
            or report.get("pid") != worker_pid or report.get("parent_pid") != identity["parent_pid"]):
        raise DatasetError("worker_report_pid_not_verified_owned_worker")
    observed = parent.get("worker_lineage", {})
    if (observed.get("verified") is not True or observed.get("worker_pid") != worker_pid
            or observed.get("launcher_pid") != launcher_pid
            or observed.get("platform") != parent.get("process_platform")):
        raise DatasetError("worker_live_lineage_attestation_missing")
    lineage = observed.get("lineage")
    if not isinstance(lineage, list) or not lineage or lineage[0] != {"pid": worker_pid, "parent_pid": identity["parent_pid"]}:
        raise DatasetError("worker_live_lineage_attestation_invalid")
    if any(not isinstance(item, dict) or set(item) != {"pid", "parent_pid"}
           or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in item.values())
           for item in lineage):
        raise DatasetError("worker_live_lineage_attestation_invalid")
    if len({item["pid"] for item in lineage}) != len(lineage):
        raise DatasetError("worker_live_lineage_cycle")
    if any(a["parent_pid"] != b["pid"] for a, b in zip(lineage, lineage[1:])):
        raise DatasetError("worker_live_lineage_chain_broken")
    if parent["process_platform"] == "nt":
        if observed.get("source") != "windows_toolhelp32_snapshot":
            raise DatasetError("worker_lineage_source_not_authoritative")
        if lineage[-1]["pid"] != launcher_pid and lineage[-1]["parent_pid"] != launcher_pid:
            raise DatasetError("worker_lineage_does_not_reach_launcher")
    elif observed.get("source") != "owned_popen_pid" or worker_pid != launcher_pid or len(lineage) != 1:
        raise DatasetError("worker_lineage_does_not_match_posix_process")


def _assess_stage(scene: CompiledScene, stage_dir: Path, mode: str,
                  expected_job: Mapping[str, Any], parent: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    errors = []
    if parent.get("timed_out"):
        errors.append("worker_timeout")
    if parent.get("returncode") != 0:
        errors.append("worker_exit_not_zero")
    report_path = stage_dir / "worker_report.json"
    report = None
    try:
        report = _read_json(report_path)
        if report.get("schema_version") != RUNTIME_WORKER_SCHEMA_VERSION:
            errors.append("worker_report_schema_mismatch")
        for key in ("run_id", "case_id", "mode", "bindings"):
            if report.get(key) != expected_job[key]:
                errors.append(f"worker_report_{key}_mismatch")
        if report.get("observed_code_sha256") != expected_job["bindings"]["code_sha256"]:
            errors.append("worker_observed_code_sha256_mismatch")
        if report.get("final_code_sha256") != expected_job["bindings"]["code_sha256"]:
            errors.append("worker_final_code_sha256_mismatch")
        expected_inputs = {"layout_sha256": expected_job["bindings"]["input_sha256"]["layout"],
                           "usd_sha256": expected_job["bindings"]["usd_sha256"]}
        for prefix in ("observed_", "final_"):
            if report.get(prefix + "inputs") != expected_inputs:
                errors.append(f"worker_{prefix}input_sha256_mismatch")
            expected_robot = expected_job["runtime_assets"].get("robot_asset_sha256")
            if not expected_robot or report.get(prefix + "robot_asset_sha256") != expected_robot:
                errors.append(f"worker_{prefix}robot_asset_sha256_mismatch")
        try:
            _validate_worker_lineage(stage_dir, expected_job, parent, report)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            errors.append(f"worker_lineage_invalid:{exc}")
        allowed_results = {"completed"} if mode == "physics" else {"task_success", "task_failure", "truncated"}
        if (report.get("result") not in allowed_results or report.get("worker_exit_code") != 0
                or not isinstance(report.get("completed_steps"), int)
                or isinstance(report.get("completed_steps"), bool)):
            errors.append("worker_report_result_or_steps_invalid")
        if report.get("settings") != expected_job["settings"] or report.get("runtime_assets") != expected_job["runtime_assets"]:
            errors.append("worker_runtime_configuration_mismatch")
        if not report.get("isaac_sim_version") or not isinstance(report.get("runtime_summary"), dict):
            errors.append("worker_runtime_metadata_missing")
        trace_raw = report.get("trace_path", "")
        if not isinstance(trace_raw, str) or not trace_raw:
            raise DatasetError("worker_trace_missing")
        trace_path = Path(trace_raw)
        if trace_path.is_absolute():
            if trace_path.is_symlink() or not trace_path.resolve().is_relative_to(stage_dir.resolve()):
                raise DatasetError("worker_trace_escapes_stage")
        else:
            trace_path = _safe_file(stage_dir, trace_raw)
        if sha256_file(trace_path) != report.get("trace_sha256"):
            errors.append("worker_trace_sha256_mismatch")
        if mode == "task":
            expected_robot_hash = expected_job["runtime_assets"].get("robot_asset_sha256")
            if not expected_robot_hash or report.get("loaded_asset_sha256") != expected_robot_hash:
                errors.append("loaded_robot_asset_sha256_mismatch")
        if errors:
            return _not_verified(";".join(errors)), errors
        samples = _trace_samples(trace_path)
        if not samples or any(not isinstance(sample, dict) for sample in samples):
            raise DatasetError("worker_trace_empty_or_invalid")
        if report["completed_steps"] != len(samples) - 1:
            raise DatasetError("worker_trace_steps_mismatch")
        if mode == "physics":
            _settled_input(stage_dir)
        from .runtime_evidence import assess_physics_trace, assess_task_trace
        settings = {**expected_job["settings"], "runtime_summary": report["runtime_summary"],
                    "isaac_sim_version": report["isaac_sim_version"],
                    "robot_asset_source": report.get("robot_asset_source")}
        evaluator = assess_physics_trace if mode == "physics" else assess_task_trace
        assessment = evaluator(scene.to_dict(), samples, settings)
        # A worker's explicit runtime failure may include facts not represented in the samples.
        if mode == "task" and report["result"] != "task_success" and assessment["status"] == "passed":
            assessment = {**assessment, "status": "failed",
                          "failure_reasons": [*assessment.get("failure_reasons", []),
                                              report.get("failure_reason") or "worker_report_not_passed"]}
        return assessment, []
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        errors.append(f"worker_evidence_invalid:{exc}")
        return _not_verified(";".join(errors)), errors


def _settled_input(stage_dir: Path) -> Path:
    report = _read_json(stage_dir / "worker_report.json")
    summary = report["runtime_summary"]
    raw = summary["settled_usd_path"]
    path = Path(raw)
    if not path.is_absolute():
        path = _safe_file(stage_dir, raw)
    if path.is_symlink() or not path.resolve().is_relative_to(stage_dir.resolve()) or not path.is_file():
        raise DatasetError("settled USD missing or outside physics stage")
    if sha256_file(path) != summary["settled_usd_sha256"]:
        raise DatasetError("settled USD hash mismatch")
    if summary.get("settled_velocity_cleared") is not True:
        raise DatasetError("settled scene did not clear residual velocities")
    return path


def _stop_process_tree(process: Any) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       capture_output=True, timeout=20, check=False,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=20)


def _run_stage(runtime: Path, job: dict[str, Any], timeout: float) -> dict[str, Any]:
    directory = Path(job["output_dir"])
    directory.mkdir(parents=True, exist_ok=False)
    job_path = directory / "job.json"
    write_json_atomic(job_path, job)
    environment = isaac_process_environment()
    command = [str(runtime), "-m", "scene_factory.runtime_worker", str(job_path)]
    parent: dict[str, Any] = {"pid": None, "launcher_pid": None, "worker_pid": None,
                              "process_platform": _process_platform(), "returncode": None, "timed_out": False,
                              "stage_timeout_s": timeout, "command": command,
                              "started_at_unix": time.time(), "job_sha256": sha256_file(job_path)}
    options = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {"start_new_session": True}
    process = None
    with (directory / "stdout.log").open("wb") as stdout, (directory / "stderr.log").open("wb") as stderr:
        try:
            process = subprocess.Popen(command, cwd=str(directory), env=environment,
                                       stdout=stdout, stderr=stderr, **options)
            parent["pid"] = process.pid
            parent["launcher_pid"] = process.pid
            deadline = time.monotonic() + timeout
            while True:
                if parent["worker_pid"] is None and (directory / "worker_identity.json").is_file():
                    try:
                        parent.update(_capture_worker_lineage(directory, job, parent["job_sha256"],
                                                             process.pid, parent["process_platform"]))
                        parent.pop("worker_identity_error", None)
                    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                        parent["worker_identity_error"] = str(exc)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    parent["timed_out"] = True
                    _stop_process_tree(process)
                    parent["returncode"] = process.returncode
                    break
                try:
                    parent["returncode"] = process.wait(timeout=min(0.25, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
            if parent["worker_pid"] is None:
                parent.setdefault("worker_identity_error", "worker_identity_missing_or_not_observed_live")
        except OSError as exc:
            parent["spawn_error"] = str(exc)
        except BaseException:
            if process is not None:
                _stop_process_tree(process)
            raise
    parent["finished_at_unix"] = time.time()
    parent["log_sha256"] = {name: sha256_file(directory / name) for name in ("stdout.log", "stderr.log")}
    write_json_atomic(directory / "parent_process.json", parent)
    return parent


def _stage_record(scene: CompiledScene, directory: Path, job: dict[str, Any], parent: dict[str, Any]) -> dict[str, Any]:
    assessment, errors = _assess_stage(scene, directory, job["mode"], job, parent)
    return {"state": "attempted", "assessment": assessment, "integrity_errors": errors,
            "job_sha256": sha256_file(directory / "job.json"),
            "parent_sha256": sha256_file(directory / "parent_process.json"),
            "worker_report_sha256": sha256_file(directory / "worker_report.json") if (directory / "worker_report.json").is_file() else None}


def _summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    complete = [case for case in cases if case.get("state") == "complete"]
    return {"case_count": len(cases), "attempted_case_count": len(complete),
            "layout_count": len({case["configuration_fingerprint"] for case in cases}),
            "evidence_complete_count": sum(bool(case.get("evidence_complete")) for case in complete),
            "physics_status_counts": dict(Counter(case.get("physics_status", "not_verified") for case in cases)),
            "task_status_counts": dict(Counter(case.get("task_status", "not_verified") for case in cases)),
            "all_task_pass": len(complete) == len(cases) and all(case.get("task_status") == "passed" for case in complete)}


def _save_manifest(root: Path, manifest: dict[str, Any]) -> None:
    manifest["summary"] = _summary(manifest["cases"])
    write_json_atomic(root / "manifest.json", manifest)
    rows = []
    status_names = {"passed": "通过", "failed": "失败", "not_verified": "未验证"}
    event_names = {"robot_mug_lift": "抓杯抬起", "robot_mug_pick_place": "抓杯并放置"}
    for case in manifest["cases"]:
        failures = html.escape("；".join(case.get("failure_reasons", [])))
        links = []
        for relative, label in (("result.json", "运行报告"), ("quality.json", "质量结论"),
                                ("physics/physics_trace.jsonl", "物理轨迹"), ("task/task_trace.jsonl", "机器人轨迹")):
            if (root / "cases" / case["case_id"] / relative).is_file():
                links.append(f'<a href="cases/{case["case_id"]}/{relative}">{label}</a>')
        physics_status = status_names.get(case.get("physics_status"), "待运行")
        task_status = status_names.get(case.get("task_status"), "待运行")
        rows.append(f'<tr><td>{html.escape(case["scene_id"])}<br>{" · ".join(links)}</td>'
                    f'<td>{event_names.get(case["event"], html.escape(case["event"]))}</td><td>{physics_status}</td>'
                    f'<td>{task_status}</td><td>{failures}</td></tr>')
    summary = manifest["summary"]
    document = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>场景运行验收</title>'
                '<style>body{font-family:Arial,sans-serif;padding:32px;color:#163047;background:#f6f8fb}'
                'table{border-collapse:collapse;width:100%;background:white}td,th{padding:12px;border:1px solid #dce4ed;text-align:left}'
                'a{color:#146ca4}</style><h1>场景运行验收</h1>'
                f'<p>{summary["layout_count"]} 个布局 · {summary["case_count"]} 个任务 · '
                f'{summary["evidence_complete_count"]} 个完整运行证据。失败结果保留在此批次。</p>'
                '<p>在已有冻结布局上使用当前登记的真实资产重新验收。物理与任务结论由原始轨迹计算。</p>'
                '<table><thead><tr><th>场景与报告</th><th>任务</th><th>物理</th><th>任务结果</th><th>原因</th></tr></thead><tbody>'
                + "".join(rows) + '</tbody></table></html>')
    (root / "index.html").write_text(document, encoding="utf-8")


def _case_bindings(entry: Mapping[str, Any], source_hashes: Mapping[str, str],
                   manifest: Mapping[str, Any], usd_sha256: str | None) -> dict[str, Any]:
    return {"input_sha256": dict(source_hashes), "source_fingerprint": entry["source"]["record"]["fingerprint"],
            "configuration_fingerprint": entry["configuration_fingerprint"],
            "usd_sha256": usd_sha256, "assets_sha256": manifest["assets"]["used_assets_sha256"],
            "code_sha256": manifest["code_sha256"], "settings_sha256": _hash(manifest["settings"]),
            "runtime_assets_sha256": _hash(manifest["assets"]["runtime_assets"])}


def _source_files(collection: Path, entry: Mapping[str, Any]) -> dict[str, Path]:
    selected = entry["source"]
    dataset = collection / selected["dataset_path"]
    record = selected["record"]
    files = {name: _safe_file(dataset, record["files"][name]) for name in ("layout", "validation", "scene_spec")}
    files["source_quality"] = _safe_file(dataset, record["quality_report"]["path"])
    for name, path in files.items():
        expected = record["quality_report"]["sha256"] if name == "source_quality" else record["sha256"][name]
        if sha256_file(path) != expected:
            raise DatasetError(f"source {name} hash changed")
    return files


def run_runtime_batch(collection_path: str | Path, output_dir: str | Path, *,
                      isaac_python: str | Path | None = None, limit_layouts: int = 20,
                      resume: bool = False, stage_timeout_s: float = 300,
                      settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(limit_layouts, bool) or not isinstance(limit_layouts, int) or not 1 <= limit_layouts <= 20:
        raise DatasetError("limit_layouts must be an integer from 1 to 20")
    if isinstance(stage_timeout_s, bool) or not isinstance(stage_timeout_s, (int, float)) or not math.isfinite(stage_timeout_s) or stage_timeout_s <= 0:
        raise DatasetError("stage_timeout_s must be positive and finite")
    collection = Path(collection_path).expanduser().resolve()
    root = Path(os.path.abspath(os.path.expanduser(str(output_dir))))
    if not str(root).isascii():
        raise DatasetError("Isaac batch output directory must use an ASCII path")
    if root.is_symlink() or root.resolve().is_relative_to(collection) or collection.is_relative_to(root.resolve()):
        raise DatasetError("runtime output must be separate from the read-only collection")
    normalized = normalize_runtime_settings(settings)
    collection_report, entries = _select(collection, limit_layouts)
    assets = _asset_context(entries, collection, normalized)
    source = {"collection_path": str(collection), "collection_sha256": sha256_file(collection / "collection.json"),
              "sampling_plan_sha256": sha256_file(collection / "sampling_plan.json"), "sources": collection_report["sources"]}
    code_sha256 = _code_fingerprint()
    if resume:
        manifest = _read_json(root / "manifest.json")
        if (manifest.get("schema_version") != RUNTIME_BATCH_SCHEMA_VERSION or manifest.get("source") != source
                or manifest.get("settings") != normalized or manifest.get("assets") != assets
                or manifest.get("code_sha256") != code_sha256 or manifest.get("stage_timeout_s") != stage_timeout_s):
            raise DatasetError("resume source, assets, code, settings or timeout changed")
        if manifest["limit_layouts"] > limit_layouts:
            raise DatasetError("resume cannot remove selected layouts")
        existing = {case["case_id"]: case for case in manifest["cases"]}
        expected_entries = {entry["case_id"]: entry for entry in entries}
        if set(existing) - set(expected_entries):
            raise DatasetError("resume selection changed")
        for case in manifest["cases"]:
            if {key: case[key] for key in expected_entries[case["case_id"]]} != expected_entries[case["case_id"]]:
                raise DatasetError("resume selected source changed")
            if case.get("state") == "complete":
                errors = _validate_case(root, manifest, case)
                if errors:
                    raise DatasetError("resume refuses damaged or incomplete case evidence: " + repr(errors))
            elif (root / "cases" / case["case_id"]).exists():
                raise DatasetError("resume refuses partially written case; preserve it and use a new output directory")
        manifest["cases"] = [existing.get(entry["case_id"], {**entry, "state": "pending"}) for entry in entries]
        manifest["limit_layouts"] = limit_layouts
    else:
        if root.exists() and any(root.iterdir()):
            raise DatasetError("runtime output directory must be empty; use resume for a compatible batch")
        root.mkdir(parents=True, exist_ok=True)
        manifest = {"schema_version": RUNTIME_BATCH_SCHEMA_VERSION, "run_id": uuid.uuid4().hex,
                    "state": "running", "source": source, "settings": normalized,
                    "stage_timeout_s": stage_timeout_s, "limit_layouts": limit_layouts,
                    "assets": assets, "code_sha256": code_sha256,
                    "cases": [{**entry, "state": "pending"} for entry in entries]}
        shutil.copyfile(collection / "collection.json", root / "source_collection.json")
        shutil.copyfile(collection / "sampling_plan.json", root / "source_sampling_plan.json")
        registry_snapshot = "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in assets["records"])
        (root / "runtime_registry.jsonl").write_text(registry_snapshot, encoding="utf-8")
        manifest["runtime_registry_sha256"] = sha256_file(root / "runtime_registry.jsonl")
    manifest["state"] = "running"
    _save_manifest(root, manifest)
    runtime = Path(isaac_python) if isaac_python is not None else find_isaac_python()
    from .runtime_evidence import runtime_quality
    numeric_settings = {key: normalized[key] for key in _DEFAULT_SETTINGS}
    for case in manifest["cases"]:
        if case["state"] == "complete":
            continue
        directory = root / "cases" / case["case_id"]
        directory.mkdir(parents=True, exist_ok=False)
        files = _source_files(collection, case)
        source_hashes = {name: sha256_file(path) for name, path in files.items()}
        for name, path in files.items():
            shutil.copyfile(path, directory / f"{name}.json")
        layout = _read_json(directory / "layout.json")
        validation = _read_json(directory / "validation.json")
        scene = CompiledScene.from_dict(layout)
        usd = directory / "scene.usd"
        export_error = None
        try:
            export_usd_with_isaac(directory / "layout.json", root / "runtime_registry.jsonl", usd,
                                  isaac_python=runtime)
            if not usd.is_file():
                raise DatasetError("USD export returned without scene.usd")
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            export_error = str(exc)
        bindings = _case_bindings(case, source_hashes, manifest, sha256_file(usd) if usd.is_file() and export_error is None else None)
        stages: dict[str, Any] = {}
        physics = _not_verified("usd_export_failed")
        task = _not_verified("physics_not_passed")
        if export_error is None:
            for mode in ("physics", "task"):
                if mode == "task" and physics["status"] != "passed":
                    break
                stage_dir = directory / mode
                stage_usd = usd if mode == "physics" else _settled_input(directory / "physics")
                stage_bindings = {**bindings, "usd_sha256": sha256_file(stage_usd)}
                job = {"schema_version": RUNTIME_JOB_SCHEMA_VERSION, "run_id": manifest["run_id"],
                       "case_id": case["case_id"], "mode": mode, "layout_path": str(directory / "layout.json"),
                       "usd_path": str(stage_usd), "output_dir": str(stage_dir), "bindings": stage_bindings,
                       "settings": numeric_settings, "runtime_assets": assets["runtime_assets"]}
                parent = _run_stage(runtime, job, stage_timeout_s)
                stage = _stage_record(scene, stage_dir, job, parent)
                stages[mode] = stage
                if mode == "physics":
                    physics = stage["assessment"]
                else:
                    task = stage["assessment"]
        if "physics" not in stages:
            stages["physics"] = {"state": "not_run", "reason": "usd_export_failed", "assessment": physics}
        if "task" not in stages:
            stages["task"] = {"state": "not_run", "reason": "physics_not_passed", "assessment": task}
        quality = runtime_quality(layout, validation, physics, task)
        write_json_atomic(directory / "quality.json", quality)
        evidence_complete = (export_error is None and not any(stage.get("integrity_errors") for stage in stages.values())
                             and physics.get("metrics", {}).get("evidence_complete") is True
                             and (task.get("metrics", {}).get("evidence_complete") is True or physics["status"] != "passed"))
        result = {"schema_version": RUNTIME_CASE_SCHEMA_VERSION, "run_id": manifest["run_id"],
                  "case_id": case["case_id"], "configuration_fingerprint": case["configuration_fingerprint"],
                  "input_sha256": source_hashes, "bindings": bindings, "stages": stages,
                  "export_error": export_error, "evidence_complete": evidence_complete,
                  "quality_sha256": sha256_file(directory / "quality.json")}
        write_json_atomic(directory / "result.json", result)
        case.update({"state": "complete", "result_sha256": sha256_file(directory / "result.json"),
                     "physics_status": physics["status"], "task_status": task["status"],
                     "evidence_complete": evidence_complete,
                     "failure_reasons": [*physics.get("failure_reasons", []), *task.get("failure_reasons", [])]})
        _save_manifest(root, manifest)
    manifest["state"] = "complete"
    _save_manifest(root, manifest)
    validation_report = validate_runtime_batch(root)
    return {**manifest, "valid": validation_report["valid"], "result": validation_report["result"],
            "validation": validation_report}


def _validate_case(root: Path, manifest: Mapping[str, Any], case: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    directory = root / "cases" / case["case_id"]
    try:
        result_path = _safe_file(directory, "result.json")
        if sha256_file(result_path) != case["result_sha256"]:
            raise DatasetError("case result hash mismatch")
        result = _read_json(result_path)
        if result.get("schema_version") != RUNTIME_CASE_SCHEMA_VERSION:
            raise DatasetError("case result schema mismatch")
        for key in ("run_id", "case_id", "configuration_fingerprint"):
            expected = manifest["run_id"] if key == "run_id" else case[key]
            if result.get(key) != expected:
                raise DatasetError(f"case {key} mismatch")
        files = {name: _safe_file(directory, f"{name}.json") for name in ("layout", "validation", "source_quality", "scene_spec")}
        hashes = {name: sha256_file(path) for name, path in files.items()}
        if hashes != result["input_sha256"]:
            raise DatasetError("case source snapshot hash mismatch")
        selected = case["source"]["record"]
        expected_hashes = {name: selected["sha256"][name] for name in ("layout", "validation", "scene_spec")}
        expected_hashes["source_quality"] = selected["quality_report"]["sha256"]
        if hashes != expected_hashes:
            raise DatasetError("case snapshots differ from selected candidate")
        layout, validation = _read_json(files["layout"]), _read_json(files["validation"])
        scene = CompiledScene.from_dict(layout)
        if configuration_fingerprint(layout) != case["configuration_fingerprint"] or scene.scene_id != case["scene_id"] or scene.event != case["event"]:
            raise DatasetError("case layout identity mismatch")
        export_error = result.get("export_error")
        bindings = _case_bindings(case, hashes, manifest, sha256_file(_safe_file(directory, "scene.usd")) if export_error is None else None)
        if result["bindings"] != bindings:
            raise DatasetError("case input bindings mismatch")
        assessments = {}
        for mode in ("physics", "task"):
            stage = result["stages"][mode]
            if stage["state"] == "not_run":
                reason = "usd_export_failed" if mode == "physics" else "physics_not_passed"
                if mode == "physics" and export_error is None:
                    raise DatasetError("physics stage omitted without export failure")
                if mode == "task" and assessments["physics"]["status"] == "passed":
                    raise DatasetError("task stage omitted after successful physics")
                if stage != {"state": "not_run", "reason": reason, "assessment": _not_verified(reason)}:
                    raise DatasetError("skipped stage reason or assessment mismatch")
                assessments[mode] = stage["assessment"]
                continue
            if stage["state"] != "attempted":
                raise DatasetError("unknown stage state")
            stage_dir = directory / mode
            job_path, parent_path = _safe_file(stage_dir, "job.json"), _safe_file(stage_dir, "parent_process.json")
            if sha256_file(job_path) != stage["job_sha256"] or sha256_file(parent_path) != stage["parent_sha256"]:
                raise DatasetError("job or parent process hash mismatch")
            job, parent = _read_json(job_path), _read_json(parent_path)
            stage_usd = directory / "scene.usd" if mode == "physics" else _settled_input(directory / "physics")
            stage_bindings = {**bindings, "usd_sha256": sha256_file(stage_usd)}
            expected_job = {"schema_version": RUNTIME_JOB_SCHEMA_VERSION, "run_id": manifest["run_id"],
                            "case_id": case["case_id"], "mode": mode,
                            "layout_path": str(directory / "layout.json"), "usd_path": str(stage_usd),
                            "output_dir": str(stage_dir), "bindings": stage_bindings,
                            "settings": {key: manifest["settings"][key] for key in _DEFAULT_SETTINGS},
                            "runtime_assets": manifest["assets"]["runtime_assets"]}
            if job != expected_job or parent.get("job_sha256") != stage["job_sha256"]:
                raise DatasetError("job contract does not match batch inputs")
            if parent.get("stage_timeout_s") != manifest["stage_timeout_s"]:
                raise DatasetError("parent timeout does not match batch")
            for name in ("stdout.log", "stderr.log"):
                if sha256_file(_safe_file(stage_dir, name)) != parent["log_sha256"][name]:
                    raise DatasetError("parent log hash mismatch")
            report_path = stage_dir / "worker_report.json"
            report_hash = sha256_file(report_path) if report_path.is_file() else None
            if report_hash != stage["worker_report_sha256"]:
                raise DatasetError("worker report hash mismatch")
            actual_assessment, actual_errors = _assess_stage(scene, stage_dir, mode, expected_job, parent)
            if actual_assessment != stage["assessment"] or actual_errors != stage["integrity_errors"]:
                raise DatasetError("stage derived evidence differs from recorded assessment")
            errors.extend(f"{mode}:{error}" for error in actual_errors)
            assessments[mode] = actual_assessment
        from .runtime_evidence import runtime_quality
        quality = runtime_quality(layout, validation, assessments["physics"], assessments["task"])
        quality_path = _safe_file(directory, "quality.json")
        if sha256_file(quality_path) != result["quality_sha256"] or _read_json(quality_path) != quality:
            raise DatasetError("derived runtime quality mismatch")
        evidence_complete = (export_error is None and not errors
                             and assessments["physics"].get("metrics", {}).get("evidence_complete") is True
                             and (assessments["task"].get("metrics", {}).get("evidence_complete") is True
                                  or assessments["physics"]["status"] != "passed"))
        if export_error is not None:
            errors.append("usd_export_failed:" + export_error)
        if result["evidence_complete"] != evidence_complete or case.get("evidence_complete") != evidence_complete:
            raise DatasetError("evidence completeness mismatch")
        for mode in ("physics", "task"):
            if case[f"{mode}_status"] != assessments[mode]["status"]:
                raise DatasetError("manifest status differs from case assessment")
        failures = [*assessments["physics"].get("failure_reasons", []), *assessments["task"].get("failure_reasons", [])]
        if case["failure_reasons"] != failures:
            raise DatasetError("manifest failure reasons mismatch")
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        errors.append(str(exc))
    return [f"{case['case_id']}: {error}" for error in errors]


def validate_runtime_batch(output_dir: str | Path) -> dict[str, Any]:
    root = Path(os.path.abspath(os.path.expanduser(str(output_dir))))
    errors: list[str] = []
    manifest: dict[str, Any] = {}
    try:
        manifest = _read_json(_safe_file(root, "manifest.json"))
        if manifest.get("schema_version") != RUNTIME_BATCH_SCHEMA_VERSION or manifest.get("state") != "complete":
            raise DatasetError("runtime batch schema or completion state invalid")
        if normalize_runtime_settings(manifest["settings"]) != manifest["settings"]:
            raise DatasetError("runtime settings are not normalized")
        if (sha256_file(_safe_file(root, "source_collection.json")) != manifest["source"]["collection_sha256"]
                or sha256_file(_safe_file(root, "source_sampling_plan.json")) != manifest["source"]["sampling_plan_sha256"]):
            raise DatasetError("source collection control snapshots changed")
        source_collection = _read_json(root / "source_collection.json")
        if source_collection["sources"] != manifest["source"]["sources"]:
            raise DatasetError("source provenance differs from frozen collection")
        if sha256_file(_safe_file(root, "runtime_registry.jsonl")) != manifest["runtime_registry_sha256"]:
            raise DatasetError("frozen runtime registry hash mismatch")
        records = [json.loads(line) for line in (root / "runtime_registry.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        if records != manifest["assets"]["records"]:
            raise DatasetError("frozen registry differs from bound asset metadata")
        assets = manifest["assets"]
        if _hash({"records": assets["records"], "binary_sha256": assets["binary_sha256"],
                  "runtime_assets": assets["runtime_assets"]}) != assets["used_assets_sha256"]:
            raise DatasetError("used asset binding hash mismatch")
        for path, digest in assets["binary_sha256"].items():
            if sha256_file(path) != digest:
                raise DatasetError("bound runtime asset binary changed: " + path)
        cases = manifest["cases"]
        if not isinstance(cases, list) or not cases or len({case["case_id"] for case in cases}) != len(cases):
            raise DatasetError("runtime case list empty or duplicated")
        groups: dict[str, list[str]] = {}
        for case in cases:
            identifier = case["case_id"]
            if not re.fullmatch(r"[0-9a-f]{20}-(lift|place)", identifier):
                raise DatasetError("unsafe runtime case_id")
            if case["source"] not in source_collection["selected"]:
                raise DatasetError("case source is absent from frozen collection selection")
            groups.setdefault(case["configuration_fingerprint"], []).append(case["event"])
        expected_layouts = min(manifest["limit_layouts"], len(source_collection["selected"]) // 2)
        if any(sorted(events) != sorted(_EVENTS) for events in groups.values()) or len(groups) != expected_layouts:
            raise DatasetError("runtime task pairs or layout count invalid")
        for case in cases:
            if case.get("state") != "complete":
                errors.append(case["case_id"] + ": case not complete")
            else:
                errors.extend(_validate_case(root, manifest, case))
        if manifest["summary"] != _summary(cases):
            raise DatasetError("runtime batch summary mismatch")
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        errors.append(str(exc))
    return {"schema_version": "scene_factory.runtime_batch_validation.v1", "valid": not errors,
            "result": "passed" if not errors else "failed", "evidence_scope": "offline_recorded_runtime_traces",
            "all_task_pass": bool(manifest.get("summary", {}).get("all_task_pass")) and not errors,
            "summary": manifest.get("summary", {}), "errors": errors}
