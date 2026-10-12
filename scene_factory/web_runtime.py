"""Local, sequential Isaac jobs for the web UI, with read-only history.

This module never imports the simulator. Results shown as accepted come from
the offline runtime validator, rather than a worker's own success claim.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import queue
import re
import threading
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping

from .dataset import DatasetError, canonical_json, sha256_file, write_json_atomic
from .paths import project_root
from .runtime_batch import run_runtime_batch, validate_runtime_batch
from .sampling import SAMPLING_PLAN_SCHEMA_VERSION, build_sampling_collection
from .scene_runtime import inspect_scene_runtime, prepare_scene_runtime_collection


_SCHEMA = "scene_factory.web_runtime_job.v1"
_ACTIVE = {"queued", "preparing", "running", "validating"}
_JOB_ID = re.compile(r"(?:job-[0-9a-f]{32}|history-[0-9a-f]{24})\Z")
_CASE_ID = re.compile(r"[0-9a-f]{20}-(?:lift|place)\Z")
_PROFILES = [{"id": "tracking", "label": "优化控制器"},
             {"id": "baseline", "label": "原始控制器"}]
_FRANKA = "Isaac/Robots/FrankaRobotics/FrankaPanda/franka.usd"
_SOURCE_FILES = ("layout.json", "scene_intent.json", "scene_spec.json", "validation.json")
_SOURCE_FILE_LIMIT = 8 * 1024 * 1024


def _absolute(path: str | Path) -> Path:
    # Preserve the ASCII junction spelling that Isaac requires on Windows.
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _safe_relative(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or ":" in relative:
        raise ValueError("文件路径必须是已登记运行目录内的相对路径")
    parts = relative.replace("\\", "/").split("/")
    if PureWindowsPath(relative).is_absolute() or relative.startswith("/") or any(
        part in {"", ".", ".."} for part in parts
    ):
        raise ValueError("不允许访问运行目录以外的文件")
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("运行文件不存在或路径不安全")
    return path


def _sampling_plan(profile: str) -> dict[str, Any]:
    suffix = "_tracking_data" if profile == "tracking" else "_data"
    strata = []
    for event, stem in (("lift", "kitchen_franka_mug_lift"),
                        ("placement", "kitchen_franka_mug_pick_place")):
        for side, seed, x0, x1 in (("left", 1000, 0.56, 0.60),
                                    ("right", 2000, 0.60, 0.64)):
            strata.append({"id": f"{event}_{side}", "recipe": stem + suffix,
                           "quota": 10, "candidate_count": 40, "seed_start": seed,
                           "filter": {"object_id": "mug_1",
                                      "region_xy": [x0, x1, -0.08, 0.02]}})
    return {"schema_version": SAMPLING_PLAN_SCHEMA_VERSION,
            "minimum_level": "layout", "strata": strata}


def _tail_frame(path: Path, limit: int = 65536) -> dict[str, Any]:
    """Read only a bounded tail; ignore an unfinished worker JSONL row."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            offset = max(0, size - limit)
            handle.seek(offset)
            data = handle.read(limit)
        if offset:
            data = data.partition(b"\n")[2]
        rows = data.split(b"\n")[:-1]
        for row in reversed(rows):
            if row.strip():
                value = json.loads(row)
                return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        pass
    return {}


@dataclass
class _Job:
    record: dict[str, Any]
    runtime_root: Path
    metadata_path: Path | None
    signature: tuple | None = None


class WebRuntimeManager:
    """Own one FIFO worker; registered historic batch roots are never changed."""

    def __init__(self, root: str | Path, factory: Any, *,
                 isaac_python: str | Path | None = None,
                 asset_root: str | Path | None = None,
                 history_roots: tuple[str | Path, ...] = ()) -> None:
        self.root = _absolute(root)
        self.factory = factory
        self.isaac_python = _absolute(isaac_python) if isaac_python else self._find_python()
        self.asset_root = _absolute(asset_root) if asset_root else self._find_asset_root()
        self._lock = threading.RLock()
        self._validation_lock = threading.Lock()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._jobs: dict[str, _Job] = {}
        self._closed = False
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "jobs").mkdir(exist_ok=True)
        self._root_lock = self._acquire_root_lock()
        self._restore_jobs()
        for history in history_roots:
            self._register_history(_absolute(history))
        self._thread = threading.Thread(target=self._work, name="scene-factory-web-isaac", daemon=True)
        self._thread.start()

    def _acquire_root_lock(self):
        """Prevent two web services from independently owning the same queue."""
        path = self.root / ".web_runtime.lock"
        if not path.resolve().is_relative_to(self.root.resolve()) or path.is_symlink():
            raise ValueError("运行队列锁路径不安全")
        handle = path.open("a+b")
        try:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise ValueError("该运行目录已有网页服务占用，请复用现有服务或另选目录。") from exc
        return handle

    def _release_root_lock(self) -> None:
        handle = self._root_lock
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    @staticmethod
    def _find_python() -> Path | None:
        configured = os.environ.get("SCENE_FACTORY_ISAAC_PYTHON", "").strip()
        if configured:
            return _absolute(configured)
        alias = Path(project_root().anchor) / "scene_factory_isaac_py312" / "Scripts/python.exe"
        local = project_root() / "local_resources/environments/scene_factory_isaac_py312/Scripts/python.exe"
        return next((path for path in (alias, local) if path.is_file()), None)

    @staticmethod
    def _find_asset_root() -> Path | None:
        configured = os.environ.get("ISAACSIM_ASSET_ROOT", "").strip()
        if configured:
            return _absolute(configured)
        alias = Path(project_root().anchor) / "scene_factory_local_assets"
        local = project_root() / "local_resources/assets/isaacsim_assets/Assets/Isaac/6.0"
        return next((path for path in (alias, local) if (path / _FRANKA).is_file()), None)

    def config(self) -> dict[str, Any]:
        message = "单队列顺序运行，每个布局分别执行抓杯和放杯；首次启动仿真需要等待。"
        available = True
        if not str(self.root).isascii():
            available, message = False, "真实仿真需要纯英文运行目录，请配置 --runtime-output。"
        elif self.isaac_python is None or not self.isaac_python.is_file():
            available, message = False, "未配置本地 Isaac Python；仍可浏览已登记的历史结果。"
        elif not str(self.isaac_python).isascii():
            available, message = False, "Isaac Python 需要纯英文路径或已存在的英文目录联接。"
        elif self.asset_root is None or not (self.asset_root / _FRANKA).is_file():
            available, message = False, "未找到官方本地 Franka 资产，请配置 --asset-root。"
        elif not str(self.asset_root).isascii():
            available, message = False, "Franka 本地资产需要纯英文路径或已存在的英文目录联接。"
        elif self._closed:
            available, message = False, "运行服务已关闭；仍可浏览历史结果。"
        return {"available": available, "message": message,
                "profiles": copy.deepcopy(_PROFILES), "max_layouts": 20}

    @property
    def has_active_jobs(self) -> bool:
        with self._lock:
            return any(job.record["status"] in _ACTIVE for job in self._jobs.values())

    def _save(self, job: _Job) -> None:
        if job.metadata_path is not None:
            write_json_atomic(job.metadata_path, {"schema_version": _SCHEMA, **job.record})

    def _restore_jobs(self) -> None:
        for directory in sorted((self.root / "jobs").iterdir()):
            if not _JOB_ID.fullmatch(directory.name) or directory.is_symlink():
                continue
            try:
                path = _safe_relative(self.root, f"jobs/{directory.name}/job.json")
                record = _read_json(path)
                if record.pop("schema_version", None) != _SCHEMA or record.get("job_id") != directory.name:
                    continue
                if record.get("profile") not in {"tracking", "baseline"}:
                    continue
                if type(record.get("limit_layouts")) is not int or not 1 <= record["limit_layouts"] <= 20:
                    continue
                if record.get("kind", "sampling") not in {"sampling", "scene"}:
                    continue
                if record.get("kind") == "scene" and record["limit_layouts"] != 1:
                    continue
                job = _Job(record, directory / "runtime", path)
                self._jobs[directory.name] = job
                if record.get("status") in _ACTIVE:
                    record.update(status="interrupted", error="网页服务重启，未完成任务已保留；请新建运行。",
                                  validation={"valid": None, "errors": []})
                    self._save(job)
            except (OSError, ValueError, TypeError, KeyError):
                continue

    def _register_history(self, root: Path) -> None:
        if not (root / "manifest.json").is_file():
            return
        identifier = "history-" + hashlib.sha256(os.fsencode(root.resolve())).hexdigest()[:24]
        if identifier in self._jobs:
            return
        try:
            manifest = _read_json(_safe_relative(root, "manifest.json"))
            limit = manifest.get("limit_layouts", 20)
            if type(limit) is not int or not 1 <= limit <= 20:
                limit = 20
            profile = "tracking" if "tracking" in str(manifest.get("cases", [])) else "baseline"
            created = datetime.fromtimestamp((root / "manifest.json").stat().st_mtime, timezone.utc).isoformat()
            record = self._new_record(identifier, root.name, profile, limit, created, history=True)
            record["status"] = "validating" if manifest.get("state") == "complete" else "interrupted"
            job = _Job(record, root, None)
            self._jobs[identifier] = job
            if manifest.get("state") == "complete":
                # Import can replay many MB of traces. Keep initial HTTP config
                # and job listing responsive while this read-only check runs.
                self._queue.put(identifier)
            else:
                record["error"] = "历史批次未完成，保留原始证据并等待新建任务。"
        except (OSError, ValueError, TypeError, KeyError):
            # Invalid explicit history is omitted rather than opening an arbitrary path.
            return

    @staticmethod
    def _new_record(identifier: str, label: str, profile: str, limit: int,
                    created: str, *, history: bool = False) -> dict[str, Any]:
        return {"job_id": identifier, "label": label, "status": "queued", "profile": profile,
                "limit_layouts": limit, "created_at": created, "error": None, "history": history,
                "validation": {"valid": None, "errors": []}}

    def create(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping) or set(payload) - {"profile", "limit_layouts", "label"}:
            raise ValueError("运行参数只支持 profile、limit_layouts 和 label")
        profile, limit = payload.get("profile", "tracking"), payload.get("limit_layouts", 1)
        if not isinstance(profile, str) or profile not in {"tracking", "baseline"}:
            raise ValueError("请选择 tracking 或 baseline 控制器")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("布局数量必须是 1 至 20 的整数")
        label = payload.get("label", f"{'优化' if profile == 'tracking' else '原始'}控制器 · {limit} 个布局")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 120:
            raise ValueError("运行名称须为 1 至 120 个字符")
        with self._lock:
            config = self.config()
            if not config["available"]:
                raise ValueError(config["message"])
            identifier = "job-" + uuid.uuid4().hex
            directory = self.root / "jobs" / identifier
            if not directory.resolve().is_relative_to(self.root.resolve()):
                raise ValueError("运行目录不安全")
            directory.mkdir(exist_ok=False)
            record = self._new_record(identifier, label.strip(), profile, limit,
                                      datetime.now(timezone.utc).isoformat())
            job = _Job(record, directory / "runtime", directory / "job.json")
            self._jobs[identifier] = job
            self._save(job)
            self._queue.put(identifier)
            # Snapshot before releasing the lock keeps the POST response queued.
            return self._snapshot(job)

    def create_scene(self, source_dir: str | Path, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Queue the exact generated layout captured now, rather than resampling."""
        if not isinstance(payload, Mapping) or set(payload) - {"scene_id", "profile", "label"}:
            raise ValueError("当前场景运行参数只支持 scene_id、profile 和 label")
        scene_id, profile = payload.get("scene_id"), payload.get("profile", "tracking")
        if not isinstance(scene_id, str) or not scene_id or len(scene_id) > 160:
            raise ValueError("请选择已生成的场景")
        if not isinstance(profile, str) or profile not in {"tracking", "baseline"}:
            raise ValueError("请选择 tracking 或 baseline 控制器")
        label = payload.get("label", f"当前场景 · {scene_id}")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 120:
            raise ValueError("运行名称须为 1 至 120 个字符")

        # Read fixed local artifact names only. The HTTP layer resolves source_dir
        # inside the generated-scene root; client paths never enter this method.
        source_root = _absolute(source_dir)
        frozen: dict[str, bytes] = {}
        for name in _SOURCE_FILES:
            path = source_root / name
            if name != "layout.json" and not path.exists():
                continue
            path = _safe_relative(source_root, name)
            if path.stat().st_size > _SOURCE_FILE_LIMIT:
                raise ValueError("场景源文件超过 8 MiB，不能进入运行队列")
            data = path.read_bytes()
            if len(data) > _SOURCE_FILE_LIMIT:
                raise ValueError("场景源文件超过 8 MiB，不能进入运行队列")
            frozen[name] = data
        try:
            layout = json.loads(frozen["layout.json"].decode("utf-8-sig"))
        except (ValueError, UnicodeError, KeyError) as exc:
            raise ValueError("当前场景的 layout.json 无法读取") from exc
        if not isinstance(layout, dict) or layout.get("scene_id") != scene_id:
            raise ValueError("layout.json 中的 scene_id 与请求不一致")

        with self._lock:
            config = self.config()
            if not config["available"]:
                raise ValueError(config["message"])
            identifier = "job-" + uuid.uuid4().hex
            directory = self.root / "jobs" / identifier
            if not directory.resolve().is_relative_to(self.root.resolve()):
                raise ValueError("运行目录不安全")
            frozen_dir = directory / "source"
            frozen_dir.mkdir(parents=True, exist_ok=False)
            for name, data in frozen.items():
                (frozen_dir / name).write_bytes(data)
            eligibility = inspect_scene_runtime(self.factory, frozen_dir)
            if eligibility.get("eligible") is not True:
                # Leave the rejected source snapshot as diagnostic evidence; it
                # has no job.json, registration, or queue entry.
                raise ValueError("当前场景暂不能执行：" + "；".join(eligibility.get("reasons", [])))
            layout_hash = hashlib.sha256(frozen["layout.json"]).hexdigest()
            intent_hash = hashlib.sha256(frozen["scene_intent.json"]).hexdigest() if "scene_intent.json" in frozen else None
            if (eligibility.get("source_scene_id") != scene_id
                    or eligibility.get("source_layout_sha256") != layout_hash
                    or eligibility.get("source_intent_sha256") != intent_hash):
                raise ValueError("场景源文件在提交校验期间发生变化")
            record = self._new_record(identifier, label.strip(), profile, 1,
                                      datetime.now(timezone.utc).isoformat())
            record.update(kind="scene", source_scene_id=scene_id,
                          source_layout_sha256=layout_hash, source_intent_sha256=intent_hash,
                          source_files_sha256={name: hashlib.sha256(data).hexdigest()
                                               for name, data in frozen.items()},
                          source_plan=copy.deepcopy(eligibility.get("plan", {})))
            job = _Job(record, directory / "runtime", directory / "job.json")
            self._jobs[identifier] = job
            self._save(job)
            self._queue.put(identifier)
            return self._snapshot(job)

    def _job(self, identifier: str) -> _Job:
        if not isinstance(identifier, str) or not _JOB_ID.fullmatch(identifier) or identifier not in self._jobs:
            raise ValueError("运行任务不存在")
        return self._jobs[identifier]

    @staticmethod
    def _manifest(job: _Job) -> dict[str, Any]:
        try:
            value = _read_json(_safe_relative(job.runtime_root, "manifest.json"))
            if isinstance(value, dict) and isinstance(value.get("cases", []), list):
                return value
        except (OSError, ValueError, TypeError):
            pass
        return {}

    @staticmethod
    def _signature(root: Path) -> tuple:
        # A cheap change detector avoids replaying tens of MB at every UI poll.
        # Any changed/added/deleted evidence file forces a fresh offline check.
        entries = []
        for path in sorted(root.rglob("*")):
            if path.is_file():
                stat = path.stat()
                entries.append((path.relative_to(root).as_posix(), stat.st_size, stat.st_mtime_ns))
        return tuple(entries)

    def _job_signature(self, job: _Job) -> tuple:
        runtime = self._signature(job.runtime_root)
        if job.record.get("kind") != "scene":
            return runtime
        return runtime, self._signature(job.runtime_root.parent / "source")

    @staticmethod
    def _source_layout(job: _Job) -> dict[str, Any]:
        source_root = job.runtime_root.parent / "source"
        hashes = job.record.get("source_files_sha256")
        if (not isinstance(hashes, dict) or "layout.json" not in hashes
                or set(hashes) - set(_SOURCE_FILES)):
            raise DatasetError("当前场景运行缺少冻结源文件记录")
        for name, expected in hashes.items():
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise DatasetError("当前场景源文件哈希记录无效")
            path = _safe_relative(source_root, name)
            if sha256_file(path) != expected:
                raise DatasetError("当前场景冻结源文件已改变：" + name)
        if (hashes["layout.json"] != job.record.get("source_layout_sha256")
                or hashes.get("scene_intent.json") != job.record.get("source_intent_sha256")):
            raise DatasetError("当前场景来源记录与冻结文件不一致")
        layout = _read_json(_safe_relative(source_root, "layout.json"))
        if not isinstance(layout, dict) or layout.get("scene_id") != job.record.get("source_scene_id"):
            raise DatasetError("当前场景来源标识不一致")
        return layout

    def _validate_scene_layouts(self, job: _Job) -> None:
        """Reject valid robot evidence produced for a different source layout."""
        source = self._source_layout(job)
        plan = job.record.get("source_plan", {})
        added = plan.get("added_objects", []) if isinstance(plan, dict) else None
        source_objects = source.get("objects")
        if not isinstance(added, list) or not isinstance(source_objects, list):
            raise DatasetError("当前场景物体来源记录无效")
        if any(not isinstance(item, dict) or not isinstance(item.get("object_id"), str)
               for item in source_objects + added):
            raise DatasetError("当前场景物体来源标识无效")
        expected_objects = {item["object_id"]: item for item in source_objects + added}
        if len(expected_objects) != len(source_objects) + len(added):
            raise DatasetError("当前场景物体来源标识重复")
        provenance = {
            "schema_version": "scene_factory.scene_runtime_source.v1",
            "source_scene_id": job.record["source_scene_id"],
            "source_layout_sha256": job.record["source_layout_sha256"],
            "source_intent_sha256": job.record["source_intent_sha256"],
            "source_objects_sha256": hashlib.sha256(canonical_json(source_objects)).hexdigest(),
            "source_layout": source, "plan": plan, "profile": job.record["profile"],
        }
        manifest = self._manifest(job)
        cases = manifest.get("cases", [])
        if len(cases) != 2:
            raise DatasetError("当前场景必须恰好包含提起和放置两项任务")
        for case in cases:
            if not isinstance(case, dict):
                raise DatasetError("当前场景运行案例记录无效")
            case_id = case.get("case_id")
            if not isinstance(case_id, str) or not _CASE_ID.fullmatch(case_id):
                raise DatasetError("当前场景运行案例标识无效")
            actual = _read_json(_safe_relative(job.runtime_root, f"cases/{case_id}/layout.json"))
            if not isinstance(actual, dict):
                raise DatasetError("当前场景运行布局记录无效")
            task = actual.get("task", {})
            if not isinstance(task, dict) or task.get("scene_runtime_source") != provenance:
                raise DatasetError("运行任务未绑定提交时冻结的场景来源")
            objects = actual.get("objects", [])
            if (not isinstance(objects, list) or len(objects) != len(expected_objects)
                    or any(not isinstance(item, dict) or not isinstance(item.get("object_id"), str)
                           for item in objects)
                    or {item["object_id"]: item for item in objects} != expected_objects
                    or actual.get("room_type") != source.get("room_type")
                    or actual.get("room_dimensions_m") != source.get("room_dimensions_m")
                    or actual.get("seed") != source.get("seed")):
                raise DatasetError("运行布局与原场景不一致，物体或房间已改变")

    def _validate(self, job: _Job) -> None:
        try:
            before = self._job_signature(job)
            if job.record.get("kind") == "scene":
                self._source_layout(job)
            result = validate_runtime_batch(job.runtime_root)
            if result.get("valid") is True and job.record.get("kind") == "scene":
                self._validate_scene_layouts(job)
            after = self._job_signature(job)
            if before != after:
                result = {"valid": False, "errors": ["校验期间运行证据发生变化"]}
        except (OSError, ValueError, RuntimeError, TypeError, KeyError) as exc:
            after = None
            result = {"valid": False, "errors": [str(exc)]}
        with self._lock:
            valid = result.get("valid") is True
            job.record["validation"] = {"valid": valid, "errors": list(result.get("errors", []))}
            job.record["status"] = "completed" if valid else "failed"
            job.record["error"] = None if valid else "独立证据校验未通过，不能认定这些任务成功。"
            job.signature = after
            self._save(job)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._job(job_id)
            check = job.record["status"] == "completed" or (
                job.record["status"] == "failed" and (
                    job.signature is not None or self._manifest(job).get("state") == "complete"
                )
            )
        if check:
            with self._validation_lock:
                try:
                    changed = job.signature != self._job_signature(job)
                except OSError:
                    changed = True
                if changed:
                    self._validate(job)
        with self._lock:
            return self._snapshot(job)

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            identifiers = sorted(self._jobs, key=lambda item: self._jobs[item].record["created_at"], reverse=True)
        return [self.get_job(identifier) for identifier in identifiers]

    def _snapshot(self, job: _Job) -> dict[str, Any]:
        record = copy.deepcopy(job.record)
        manifest = self._manifest(job)
        accepted = record["validation"]["valid"] is True and record["status"] == "completed"
        cases = []
        for case in manifest.get("cases", []):
            if not isinstance(case, dict) or not _CASE_ID.fullmatch(str(case.get("case_id", ""))):
                continue
            complete = case.get("state") == "complete"
            status = "pending_validation" if complete and record["status"] in _ACTIVE else "not_verified"
            trace = f"cases/{case['case_id']}/task/task_trace.jsonl"
            try:
                _safe_relative(job.runtime_root, trace)
                replay = accepted and complete
            except ValueError:
                replay = False
            source = case.get("source", {})
            source = source if isinstance(source, dict) else {}
            source_record = source.get("record", {})
            source_record = source_record if isinstance(source_record, dict) else {}
            reasons = case.get("failure_reasons", [])
            cases.append({"case_id": case["case_id"], "event": case.get("event"),
                          "scene_id": case.get("scene_id"),
                          "seed": source_record.get("seed"),
                          "physics_status": case.get("physics_status", "not_verified") if accepted else status,
                          "task_status": case.get("task_status", "not_verified") if accepted else status,
                          "evidence_complete": accepted and case.get("evidence_complete") is True,
                          "failure_reasons": list(reasons) if isinstance(reasons, list) else [],
                          "replay_available": replay})
        record["cases"] = cases
        record["summary"] = {"case_count": len(cases),
                             "layout_count": len({case["case_id"].rsplit("-", 1)[0] for case in cases}),
                             "physics_status_counts": dict(Counter(case["physics_status"] for case in cases)),
                             "task_status_counts": dict(Counter(case["task_status"] for case in cases)),
                             "evidence_complete_count": sum(case["evidence_complete"] for case in cases)}
        record["progress"] = self._progress(job, manifest)
        try:
            _safe_relative(job.runtime_root, "index.html")
            record["report_url"] = f"/api/runtime/jobs/{record['job_id']}/files/index.html" if accepted else None
        except ValueError:
            record["report_url"] = None
        return record

    @staticmethod
    def _progress(job: _Job, manifest: dict[str, Any]) -> dict[str, Any]:
        cases = [case for case in manifest.get("cases", []) if isinstance(case, dict)]
        progress = {"completed": sum(case.get("state") == "complete" for case in cases),
                    "total": job.record["limit_layouts"] * 2,
                    "stage": job.record["status"], "case_id": None, "step": 0, "total_steps": 0}
        if job.record["status"] != "running":
            return progress
        case = next((case for case in cases if case.get("state") != "complete"), None)
        if case is None or not _CASE_ID.fullmatch(str(case.get("case_id", ""))):
            return progress
        progress["case_id"] = case["case_id"]
        for mode, setting in (("task", "max_steps"), ("physics", "settle_steps")):
            try:
                path = _safe_relative(job.runtime_root, f"cases/{case['case_id']}/{mode}/{mode}_trace.jsonl")
            except ValueError:
                continue
            frame = _tail_frame(path)
            step = frame.get("step", 0)
            progress.update(stage=mode, step=step if type(step) is int and step >= 0 else 0,
                            total_steps=manifest.get("settings", {}).get(setting, 720 if mode == "task" else 240))
            break
        else:
            progress["stage"] = "export"
        return progress

    def _set_status(self, job: _Job, status: str, error: str | None = None) -> None:
        with self._lock:
            job.record.update(status=status, error=error)
            self._save(job)

    def _work(self) -> None:
        while True:
            identifier = self._queue.get()
            try:
                if identifier is None:
                    self._release_root_lock()
                    return
                with self._lock:
                    job = self._job(identifier)
                    if self._closed:
                        self._set_status(job, "interrupted", "服务关闭，排队任务未启动；请新建运行。")
                        continue
                    self._set_status(job, "validating" if job.metadata_path is None else "preparing")
                try:
                    if job.metadata_path is None:
                        with self._validation_lock:
                            self._validate(job)
                        continue
                    collection = job.metadata_path.parent / "collection"
                    if job.record.get("kind") == "scene":
                        self._source_layout(job)
                        generated = prepare_scene_runtime_collection(
                            self.factory, job.metadata_path.parent / "source", collection,
                            profile=job.record["profile"],
                        )
                        if (generated.get("source_scene_id") != job.record["source_scene_id"]
                                or generated.get("source_layout_sha256") != job.record["source_layout_sha256"]
                                or generated.get("source_intent_sha256") != job.record["source_intent_sha256"]
                                or generated.get("task_count") != 2):
                            raise DatasetError("运行集合未绑定提交时冻结的场景来源")
                        self._source_layout(job)
                    else:
                        generated = build_sampling_collection(_sampling_plan(job.record["profile"]), collection,
                                                              factory=self.factory)
                        if not generated.valid:
                            raise DatasetError("布局集合生成失败：" + repr(generated.to_dict().get("errors", [])))
                    self._set_status(job, "running")
                    run_runtime_batch(collection, job.runtime_root, isaac_python=self.isaac_python,
                                      limit_layouts=job.record["limit_layouts"],
                                      settings={"robot_asset_root": os.fspath(self.asset_root)})
                    self._set_status(job, "validating")
                    with self._validation_lock:
                        self._validate(job)
                except Exception as exc:
                    # Keep all partial evidence. A failed robot task is handled by
                    # the batch validator and does not raise this program error.
                    self._set_status(job, "failed", str(exc))
                    with self._lock:
                        job.record["validation"] = {"valid": False, "errors": [str(exc)]}
                        self._save(job)
            finally:
                self._queue.task_done()

    def case_data(self, job_id: str, case_id: str) -> dict[str, Any]:
        record = self.get_job(job_id)
        case = next((case for case in record["cases"] if case["case_id"] == case_id), None)
        if case is None or not _CASE_ID.fullmatch(case_id):
            raise ValueError("运行案例不存在")
        with self._lock:
            root = self._job(job_id).runtime_root
        layout = _safe_relative(root, f"cases/{case_id}/layout.json")
        trace = _safe_relative(root, f"cases/{case_id}/task/task_trace.jsonl") if case["replay_available"] else None
        return {"root": root, "case_root": layout.parent, "scene": _read_json(layout),
                "layout_path": layout, "trace_path": trace, "case": case}

    def resolve_file(self, job_id: str, relative: str) -> Path:
        with self._lock:
            root = self._job(job_id).runtime_root
        return _safe_relative(root, relative)

    def close(self) -> None:
        """Stop accepting jobs; let the current simulator finish its evidence."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for job in self._jobs.values():
                if job.record["status"] == "queued":
                    self._set_status(job, "interrupted", "服务关闭，排队任务未启动；请新建运行。")
            self._queue.put(None)
