"""Portable episode recording and loading for synchronized simulator trajectories.

This module intentionally uses only the Python standard library.  Isaac Sim and
array libraries are needed only by the runtime that supplies frame data to
``EpisodeRecorder``.
"""

from __future__ import annotations

import ast
import binascii
import json
import math
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Iterator

from .robotics import MUG_LIFT_PHASE_TRANSITIONS, MugLiftPhase


SCHEMA_VERSION = "scene_factory.trajectory.v1"
_MAX_JSON_BYTES = 16 * 1024 * 1024
_MAX_JSONL_LINE_BYTES = 16 * 1024 * 1024
_MAX_NPY_ELEMENTS = 16 * 1024 * 1024
_MAX_SENSOR_PIXELS = 16 * 1024 * 1024
_REQUIRED_METADATA = {
    "schema_version",
    "scene_id",
    "recipe",
    "seed",
    "robot_asset_source",
    "isaac_sim_version",
    "camera",
    "intrinsics",
    "extrinsics",
    "control_frequency_hz",
    "sensor_frequency_hz",
}
_REQUIRED_RECORD_FIELDS = {
    "episode_id",
    "frame_id",
    "sim_step",
    "timestamp",
    "rgb_path",
    "depth_path",
    "sensor",
    "robot_state",
    "object_state",
    "action",
    "oracle",
    "phase",
    "contact_diagnostics",
    "reward",
    "terminated",
    "truncated",
    "failed",
}


class DatasetError(ValueError):
    """Raised when an episode is incomplete, malformed, or not synchronized."""


@dataclass(frozen=True)
class EpisodeFrame:
    """One synchronized trajectory record and its episode directory."""

    record: dict[str, Any]
    episode_dir: Path

    def __getitem__(self, key: str) -> Any:
        return self.record[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.record.get(key, default)

    @property
    def frame_id(self) -> int:
        return int(self.record["frame_id"])

    @property
    def sim_step(self) -> int:
        return int(self.record["sim_step"])

    @property
    def timestamp(self) -> float:
        return float(self.record["timestamp"])

    @property
    def rgb_path(self) -> Path:
        return self._media_path("rgb_path")

    @property
    def depth_path(self) -> Path:
        return self._media_path("depth_path")

    @property
    def rgb_data(self) -> bytes:
        return self.rgb_path.read_bytes()

    @property
    def depth_data(self) -> list[list[float]]:
        return _read_npy_float32(self.depth_path)

    def read_rgb(self) -> bytes:
        return self.rgb_data

    def read_depth(self) -> list[list[float]]:
        return self.depth_data

    def _media_path(self, key: str) -> Path:
        value = self.record.get(key)
        if not isinstance(value, str) or not value:
            raise DatasetError(f"trajectory record has no {key}")
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise DatasetError(f"media path escapes episode directory: {value!r}")
        path = (self.episode_dir / relative).resolve()
        root = self.episode_dir.resolve()
        if path != root and root not in path.parents:
            raise DatasetError(f"media path escapes episode directory: {value!r}")
        if not path.is_file():
            raise DatasetError(f"missing media file: {value}")
        return path


@dataclass(frozen=True)
class Episode:
    """A validated, indexable episode dataset."""

    path: Path
    metadata: dict[str, Any]
    result: dict[str, Any]
    frames: tuple[EpisodeFrame, ...]

    def __len__(self) -> int:
        return len(self.frames)

    def __getitem__(self, index: int) -> EpisodeFrame:
        return self.frames[index]

    def __iter__(self) -> Iterator[EpisodeFrame]:
        return iter(self.frames)


@dataclass(frozen=True)
class EpisodeOperationResult:
    """Serializable result returned by an offline episode operation."""

    operation: str
    path: str
    result: str
    valid: bool
    checks: dict[str, bool] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.result == "passed"

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "path": self.path,
            "result": self.result,
            "valid": self.valid,
            "checks": dict(self.checks),
            "summary": dict(self.summary),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


class EpisodeInspectionResult(EpisodeOperationResult):
    """Result type for :func:`inspect_episode`."""


class EpisodeValidationResult(EpisodeOperationResult):
    """Result type for :func:`validate_episode`."""


class EpisodeReplayResult(EpisodeOperationResult):
    """Result type for :func:`replay_episode`."""


class EpisodeRecorder:
    """Write an episode without silently dropping sensor frames."""

    def __init__(self, path: str | Path, metadata: dict[str, Any]) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.mkdir(parents=True, exist_ok=True)
        self.rgb_dir = self.path / "rgb"
        self.depth_dir = self.path / "depth"
        self.rgb_dir.mkdir(exist_ok=True)
        self.depth_dir.mkdir(exist_ok=True)
        if (self.path / "trajectory.jsonl").exists() or (self.path / "result.json").exists():
            raise DatasetError(f"episode output already contains a completed dataset: {self.path}")
        missing = sorted(_REQUIRED_METADATA - set(metadata))
        if missing:
            raise DatasetError(f"metadata is missing required fields: {missing}")
        if metadata.get("schema_version") != SCHEMA_VERSION:
            raise DatasetError(f"unsupported trajectory schema: {metadata.get('schema_version')!r}")
        self.metadata = dict(metadata)
        self.metadata["frame_count"] = 0
        self.metadata["start_step"] = None
        self.metadata["end_step"] = None
        self._trajectory = (self.path / "trajectory.jsonl").open(
            "w", encoding="utf-8", newline="\n"
        )
        self._next_frame_id = 0
        self._last_sim_step: int | None = None
        self._last_timestamp: float | None = None
        self._episode_id: str | None = None
        self._failure_reason: str | None = None
        self._failed_frame_seen = False
        self._finalized = False
        _write_json(self.path / "metadata.json", self.metadata)

    @property
    def frame_count(self) -> int:
        return self._next_frame_id

    def append(
        self,
        *,
        episode_id: str,
        frame_id: int,
        sim_step: int,
        timestamp: float,
        sensor: dict[str, Any],
        robot_state: dict[str, Any],
        object_state: dict[str, Any],
        action: dict[str, Any] | None,
        oracle: dict[str, Any],
        phase: str,
        contact_diagnostics: dict[str, Any],
        reward: float,
        terminated: bool,
        truncated: bool,
    ) -> dict[str, Any]:
        if self._finalized:
            raise DatasetError("cannot append after finalizing an episode")
        if self._failure_reason:
            raise DatasetError(f"episode is already failed: {self._failure_reason}")
        if isinstance(frame_id, bool) or not isinstance(frame_id, int):
            self._reject("frame_id must be an integer")
        if isinstance(sim_step, bool) or not isinstance(sim_step, int):
            self._reject("sim_step must be an integer")
        if frame_id != self._next_frame_id:
            self._reject(
                f"frame_id must be contiguous from zero: expected {self._next_frame_id}, got {frame_id}"
            )
        if self._last_sim_step is not None and sim_step <= self._last_sim_step:
            self._reject(
                f"sim_step must be strictly monotonic: previous {self._last_sim_step}, got {sim_step}"
            )
        if not isinstance(episode_id, str) or not episode_id:
            raise DatasetError("episode_id must be a non-empty string")
        if self._episode_id is None:
            self._episode_id = episode_id
            self.metadata["episode_id"] = episode_id
        elif episode_id != self._episode_id:
            raise DatasetError("all trajectory records must use one episode_id")
        if not isinstance(phase, str) or not phase:
            raise DatasetError("trajectory phase must be a non-empty string")
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            self._reject("trajectory timestamp must be a number")
        if not math.isfinite(float(timestamp)):
            raise DatasetError("trajectory timestamp must be finite")
        if self._last_timestamp is not None and timestamp <= self._last_timestamp:
            self._reject(
                f"timestamp must be strictly monotonic: previous {self._last_timestamp}, got {timestamp}"
            )
        if isinstance(reward, bool) or not isinstance(reward, (int, float)):
            self._reject("trajectory reward must be a number")
        if not math.isfinite(float(reward)):
            self._reject("trajectory reward must be finite")

        try:
            rgb_path, depth_path, sensor_summary = self._write_sensor_frame(
                frame_id, sim_step, sensor
            )
        except Exception as exc:
            self._failure_reason = f"sensor_write_failure: {type(exc).__name__}: {exc}"
            raise DatasetError(self._failure_reason) from exc

        record = {
            "episode_id": episode_id,
            "frame_id": frame_id,
            "sim_step": int(sim_step),
            "timestamp": float(timestamp),
            "rgb_path": rgb_path,
            "depth_path": depth_path,
            "sensor": sensor_summary,
            "robot_state": robot_state,
            "object_state": object_state,
            "action": action,
            "oracle": oracle,
            "phase": phase,
            "contact_diagnostics": contact_diagnostics,
            "reward": float(reward),
            "terminated": bool(terminated),
            "truncated": bool(truncated),
            "failed": bool(phase == "FAILED" or truncated),
        }
        if record["failed"]:
            self._failed_frame_seen = True
        try:
            encoded = json.dumps(record, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            self._failure_reason = f"trajectory_record_not_serializable: {exc}"
            raise DatasetError(self._failure_reason) from exc
        self._trajectory.write(encoded + "\n")
        self._trajectory.flush()
        self._next_frame_id += 1
        self._last_sim_step = int(sim_step)
        self._last_timestamp = float(timestamp)
        self.metadata["frame_count"] = self._next_frame_id
        if self.metadata["start_step"] is None:
            self.metadata["start_step"] = int(sim_step)
        self.metadata["end_step"] = int(sim_step)
        if self.metadata.get("intrinsics") is None:
            self.metadata["intrinsics"] = sensor_summary["intrinsics"]
            self.metadata["extrinsics"] = sensor_summary["extrinsics"]
        _write_json(self.path / "metadata.json", self.metadata)
        return record

    def finalize(self, result: dict[str, Any]) -> dict[str, Any]:
        if self._finalized:
            raise DatasetError("episode has already been finalized")
        self._trajectory.close()
        self._finalized = True
        payload = dict(result)
        passed = payload.get("result") == "passed" and self._failure_reason is None
        if self._failed_frame_seen:
            passed = False
            self._failure_reason = self._failure_reason or "failed_or_truncated_frame"
        if self._next_frame_id == 0:
            passed = False
            self._failure_reason = self._failure_reason or "no_sensor_frames"
        if self.metadata.get("frame_count") != self._next_frame_id:
            passed = False
            self._failure_reason = self._failure_reason or "frame_count_mismatch"
        export = {
            "result": "passed" if passed else "failed",
            "frame_count": self._next_frame_id,
            "rgb_frame_count": len(list(self.rgb_dir.glob("*.png"))),
            "depth_frame_count": len(list(self.depth_dir.glob("*.npy"))),
            "sensor_state_synchronized": bool(passed),
            "failure_reason": None if passed else (self._failure_reason or payload.get("failure_reason")),
        }
        payload["trajectory_export"] = export
        payload["frame_count"] = self._next_frame_id
        if not passed:
            payload["result"] = "failed"
            payload["failure_reason"] = export["failure_reason"] or "trajectory_export_failed"
        _write_json(self.path / "metadata.json", self.metadata)
        _write_json(self.path / "result.json", payload)
        return payload

    def fail(self, reason: str) -> None:
        if not self._failure_reason:
            self._failure_reason = str(reason)

    def _write_sensor_frame(
        self, frame_id: int, sim_step: int, sensor: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        if not isinstance(sensor, dict):
            raise DatasetError("sensor capture must be a mapping")
        if sensor.get("status", "ok") != "ok":
            raise DatasetError(f"sensor capture failed: {sensor.get('error', 'unknown error')}")
        sensor_step = sensor.get("simulation_step", sim_step)
        if isinstance(sensor_step, bool) or not isinstance(sensor_step, int):
            raise DatasetError("sensor simulation_step must be an integer")
        if sensor_step != sim_step:
            raise DatasetError(
                f"sensor simulation_step {sensor_step} does not match trajectory step {sim_step}"
            )
        if "rgb" not in sensor or "depth" not in sensor:
            raise DatasetError("sensor capture must contain rgb and depth")
        rgb_shape = _shape(sensor["rgb"])
        depth_shape = _shape(sensor["depth"])
        if len(rgb_shape) != 3 or rgb_shape[2] != 3:
            raise DatasetError(f"RGB must have shape (height, width, 3), got {rgb_shape}")
        if len(depth_shape) != 2:
            raise DatasetError(f"depth must have shape (height, width), got {depth_shape}")
        width, height = _camera_resolution(self.metadata, rgb_shape)
        if (rgb_shape[1], rgb_shape[0]) != (width, height) or depth_shape != (height, width):
            raise DatasetError(
                f"sensor shape does not match camera resolution {(width, height)}: "
                f"rgb={rgb_shape}, depth={depth_shape}"
            )
        rgb_file = self.rgb_dir / f"{frame_id:06d}.png"
        depth_file = self.depth_dir / f"{frame_id:06d}.npy"
        _write_png_rgb(rgb_file, sensor["rgb"])
        finite_depth = _write_npy_float32(depth_file, sensor["depth"])
        if finite_depth <= 0:
            raise DatasetError("depth frame contains no finite values")
        intrinsics = _finite_matrix(sensor.get("intrinsics"), "intrinsics")
        extrinsics = _finite_matrix(sensor.get("extrinsics"), "extrinsics")
        return (
            rgb_file.relative_to(self.path).as_posix(),
            depth_file.relative_to(self.path).as_posix(),
            {
                "status": "ok",
                "simulation_step": int(sim_step),
                "rgb_shape": list(rgb_shape),
                "depth_shape": list(depth_shape),
                "finite_depth_values": finite_depth,
                "intrinsics": intrinsics,
                "extrinsics": extrinsics,
            },
        )

    def _reject(self, reason: str) -> None:
        self._failure_reason = reason
        raise DatasetError(reason)


def load_episode(path: str | Path) -> Episode:
    """Load and validate an episode using no Isaac Sim or NumPy imports."""

    episode_dir = Path(path).expanduser().resolve()
    if not episode_dir.is_dir():
        raise DatasetError(f"episode directory does not exist: {episode_dir}")
    metadata = _read_json(episode_dir / "metadata.json")
    result = _read_json(episode_dir / "result.json")
    if metadata.get("schema_version") != SCHEMA_VERSION:
        raise DatasetError(f"unsupported trajectory schema: {metadata.get('schema_version')!r}")
    missing = sorted(_REQUIRED_METADATA - set(metadata))
    if missing:
        raise DatasetError(f"metadata is missing required fields: {missing}")
    if metadata.get("intrinsics") is None or metadata.get("extrinsics") is None:
        raise DatasetError("metadata is missing camera intrinsics or extrinsics")
    _finite_matrix(metadata["intrinsics"], "intrinsics")
    _finite_matrix(metadata["extrinsics"], "extrinsics")
    trajectory_path = episode_dir / "trajectory.jsonl"
    if not trajectory_path.is_file():
        raise DatasetError("episode is missing trajectory.jsonl")
    records: list[dict[str, Any]] = []
    try:
        trajectory_size = trajectory_path.stat().st_size
    except OSError as exc:
        raise DatasetError(f"cannot stat trajectory.jsonl: {exc}") from exc
    if trajectory_size > _MAX_JSONL_LINE_BYTES * 1000:
        raise DatasetError("trajectory.jsonl is too large")
    try:
        trajectory_handle = trajectory_path.open("r", encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise DatasetError(f"cannot read trajectory.jsonl: {exc}") from exc
    with trajectory_handle:
        for line_number, line in enumerate(trajectory_handle, 1):
            if len(line.encode("utf-8")) > _MAX_JSONL_LINE_BYTES:
                raise DatasetError(f"trajectory record is too large at line {line_number}")
            if not line.strip():
                raise DatasetError(f"blank trajectory record at line {line_number}")
            try:
                record = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DatasetError(f"malformed trajectory JSON at line {line_number}: {exc}") from exc
            if not isinstance(record, dict):
                raise DatasetError(f"trajectory line {line_number} is not an object")
            records.append(record)
    frames: list[EpisodeFrame] = []
    previous_step: int | None = None
    previous_timestamp: float | None = None
    episode_id: str | None = None
    for expected_id, record in enumerate(records):
        missing_fields = sorted(_REQUIRED_RECORD_FIELDS - set(record))
        if missing_fields:
            raise DatasetError(f"trajectory frame {expected_id} is missing fields: {missing_fields}")
        frame_id = record.get("frame_id")
        if isinstance(frame_id, bool) or not isinstance(frame_id, int):
            raise DatasetError(f"invalid frame_id at record {expected_id}")
        if frame_id != expected_id:
            raise DatasetError(f"frame_id is not contiguous at record {expected_id}")
        step = record.get("sim_step")
        if not isinstance(step, int) or isinstance(step, bool):
            raise DatasetError(f"invalid sim_step at frame {expected_id}")
        if previous_step is not None and step <= previous_step:
            raise DatasetError(f"sim_step is not strictly monotonic at frame {expected_id}")
        previous_step = step
        current_episode_id = record.get("episode_id")
        if not isinstance(current_episode_id, str) or not current_episode_id:
            raise DatasetError(f"invalid episode_id at frame {expected_id}")
        if episode_id is None:
            episode_id = current_episode_id
        elif current_episode_id != episode_id:
            raise DatasetError("trajectory contains multiple episode IDs")
        sensor = record.get("sensor")
        if not isinstance(sensor, dict) or sensor.get("status") != "ok":
            raise DatasetError(f"sensor failure recorded at frame {expected_id}")
        if sensor.get("simulation_step") != step:
            raise DatasetError(f"sensor step does not match trajectory step at frame {expected_id}")
        timestamp = record.get("timestamp")
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            raise DatasetError(f"invalid timestamp at frame {expected_id}")
        if not math.isfinite(float(timestamp)):
            raise DatasetError(f"timestamp is not finite at frame {expected_id}")
        if previous_timestamp is not None and timestamp <= previous_timestamp:
            raise DatasetError(f"timestamp is not strictly monotonic at frame {expected_id}")
        previous_timestamp = float(timestamp)
        if not isinstance(record["robot_state"], dict) or not isinstance(record["object_state"], dict):
            raise DatasetError(f"invalid state mapping at frame {expected_id}")
        if not isinstance(record["oracle"], dict) or not isinstance(record["contact_diagnostics"], dict):
            raise DatasetError(f"invalid diagnostic mapping at frame {expected_id}")
        if isinstance(record["reward"], bool) or not isinstance(record["reward"], (int, float)):
            raise DatasetError(f"invalid reward at frame {expected_id}")
        if not math.isfinite(float(record["reward"])):
            raise DatasetError(f"reward is not finite at frame {expected_id}")
        if not isinstance(record["phase"], str) or not record["phase"]:
            raise DatasetError(f"invalid phase at frame {expected_id}")
        if not isinstance(record["failed"], bool):
            raise DatasetError(f"invalid failed flag at frame {expected_id}")
        if record["failed"]:
            raise DatasetError(f"failed trajectory frame at frame {expected_id}")
        frame = EpisodeFrame(record, episode_dir)
        rgb_width, rgb_height = _validate_png_rgb(frame.rgb_path)
        depth_values = _read_npy_float32(frame.depth_path)
        if sensor.get("rgb_shape") != [rgb_height, rgb_width, 3]:
            raise DatasetError(f"RGB shape metadata mismatch at frame {expected_id}")
        if sensor.get("depth_shape") != [len(depth_values), len(depth_values[0])]:
            raise DatasetError(f"depth shape metadata mismatch at frame {expected_id}")
        finite_depth_values = sum(
            math.isfinite(value) for row in depth_values for value in row
        )
        if sensor.get("finite_depth_values") != finite_depth_values:
            raise DatasetError(f"finite depth metadata mismatch at frame {expected_id}")
        if finite_depth_values <= 0:
            raise DatasetError(f"depth frame has no finite values at frame {expected_id}")
        frames.append(frame)
    expected_count = metadata.get("frame_count")
    if expected_count != len(frames):
        raise DatasetError(f"metadata frame_count {expected_count!r} != {len(frames)} records")
    rgb_count = len(list((episode_dir / "rgb").glob("*.png")))
    depth_count = len(list((episode_dir / "depth").glob("*.npy")))
    if rgb_count != len(frames) or depth_count != len(frames):
        raise DatasetError(
            f"media count mismatch: records={len(frames)}, rgb={rgb_count}, depth={depth_count}"
        )
    if result.get("trajectory_export", {}).get("result") != "passed":
        raise DatasetError("episode result does not confirm a passed trajectory export")
    if result.get("result") != "passed":
        raise DatasetError("episode task result is not passed")
    if not frames:
        raise DatasetError("episode has no frames")
    return Episode(episode_dir, metadata, result, tuple(frames))


@dataclass
class _EpisodeAnalysis:
    path: Path
    metadata: dict[str, Any] = field(default_factory=dict)
    result_payload: dict[str, Any] = field(default_factory=dict)
    episode: Episode | None = None
    checks: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_error(self, message: str) -> None:
        if message not in self.errors:
            self.errors.append(message)


def inspect_episode(path: str | Path) -> EpisodeInspectionResult:
    """Inspect an episode and return a deterministic, JSON-serializable summary.

    Inspection is intentionally read-only.  It reports the same integrity
    failures as validation, but does not import Isaac Sim or array libraries.
    """

    analysis = _analyze_episode(path)
    return EpisodeInspectionResult(
        operation="inspect",
        path=str(analysis.path),
        result="passed" if not analysis.errors else "failed",
        valid=not analysis.errors,
        checks=analysis.checks,
        summary=_episode_summary(analysis),
        errors=tuple(analysis.errors),
        warnings=tuple(analysis.warnings),
    )


def validate_episode(path: str | Path) -> EpisodeValidationResult:
    """Validate a complete episode without running simulator physics.

    A passed result is deliberately stricter than merely being loadable: it
    requires a terminal, successful task report, legal controller phases, and
    media/calibration/count consistency derived from the files themselves.
    """

    analysis = _analyze_episode(path)
    return EpisodeValidationResult(
        operation="validate",
        path=str(analysis.path),
        result="passed" if not analysis.errors else "failed",
        valid=not analysis.errors,
        checks=analysis.checks,
        summary=_episode_summary(analysis),
        errors=tuple(analysis.errors),
        warnings=tuple(analysis.warnings),
    )


def replay_episode(path: str | Path) -> EpisodeReplayResult:
    """Replay recorded frames as an offline consistency check.

    This does not replay physics.  When a pure-Python task snapshot is present
    in ``metadata.json`` under ``task_spec`` (or the legacy ``task`` key), the
    task oracle is recomputed frame by frame.  Older episodes without that
    snapshot still receive an integrity replay result, while task replay is
    explicitly reported as ``not_available``.
    """

    analysis = _analyze_episode(path)
    replay_checks = dict(analysis.checks)
    replay_errors = list(analysis.errors)
    replay_summary = _episode_summary(analysis)
    task_replay: dict[str, Any]
    if analysis.errors or analysis.episode is None:
        task_replay = {
            "result": "skipped",
            "reason": "episode_validation_failed",
        }
        replay_checks["task_replay_available"] = False
    else:
        task_replay, task_errors = _replay_task_oracle(analysis.episode)
        replay_errors.extend(task_errors)
        replay_checks["task_replay_available"] = task_replay["result"] != "not_available"
        replay_checks["task_replay"] = task_replay["result"] in {"passed", "not_available"}
    replay_summary["task_replay"] = task_replay
    return EpisodeReplayResult(
        operation="replay",
        path=str(analysis.path),
        result="passed" if not replay_errors else "failed",
        valid=not replay_errors,
        checks=replay_checks,
        summary=replay_summary,
        errors=tuple(replay_errors),
        warnings=tuple(analysis.warnings),
    )


def _analyze_episode(path: str | Path) -> _EpisodeAnalysis:
    episode_path = Path(path).expanduser().resolve()
    analysis = _EpisodeAnalysis(episode_path)
    root_structure_ok = True
    if not episode_path.is_dir():
        analysis.add_error(f"episode directory does not exist: {episode_path}")
        analysis.checks["root_structure"] = False
        return analysis

    required_files = ("metadata.json", "trajectory.jsonl", "result.json")
    for name in required_files:
        target = episode_path / name
        if not target.is_file() or target.is_symlink():
            analysis.add_error(f"episode is missing {name}")
            root_structure_ok = False
    for name in ("rgb", "depth"):
        directory = episode_path / name
        if not directory.is_dir() or directory.is_symlink():
            analysis.add_error(f"episode is missing {name}/ directory")
            root_structure_ok = False
    analysis.checks["root_structure"] = root_structure_ok

    for name, target in (("metadata", episode_path / "metadata.json"), ("result", episode_path / "result.json")):
        if not target.is_file():
            continue
        try:
            payload = _read_json(target)
        except DatasetError as exc:
            analysis.add_error(str(exc))
            continue
        if name == "metadata":
            analysis.metadata = payload
        else:
            analysis.result_payload = payload

    metadata_ok = _validate_metadata(analysis.metadata, analysis)
    analysis.checks["metadata"] = metadata_ok
    result_ok = _validate_result_json_shape(analysis.result_payload, analysis)
    analysis.checks["result_json"] = result_ok

    if (
        analysis.checks.get("root_structure")
        and analysis.checks.get("metadata")
        and analysis.checks.get("result_json")
    ):
        try:
            analysis.episode = load_episode(episode_path)
        except (DatasetError, OSError, TypeError, ValueError, OverflowError) as exc:
            analysis.add_error(f"episode load failed: {exc}")
    else:
        analysis.add_error("episode cannot be loaded until required files and JSON are valid")

    if analysis.episode is not None:
        _validate_loaded_episode(analysis.episode, analysis)
        analysis.checks["loadable"] = True
    else:
        analysis.checks["loadable"] = False
    return analysis


def _episode_summary(analysis: _EpisodeAnalysis) -> dict[str, Any]:
    metadata = analysis.metadata
    result = analysis.result_payload
    frames = list(analysis.episode) if analysis.episode is not None else []
    phases: list[str] = []
    for frame in frames:
        phase = str(frame.get("phase"))
        if not phases or phases[-1] != phase:
            phases.append(phase)
    return {
        "scene_id": metadata.get("scene_id"),
        "recipe": metadata.get("recipe"),
        "episode_id": metadata.get("episode_id")
        or (frames[0].get("episode_id") if frames else None),
        "schema_version": metadata.get("schema_version"),
        "frame_count": len(frames) if analysis.episode is not None else metadata.get("frame_count"),
        "start_step": metadata.get("start_step"),
        "end_step": metadata.get("end_step"),
        "phase_coverage": phases,
        "result": result.get("result"),
        "task_success": result.get("task_success"),
        "files": {
            "metadata": (analysis.path / "metadata.json").is_file(),
            "trajectory": (analysis.path / "trajectory.jsonl").is_file(),
            "result": (analysis.path / "result.json").is_file(),
            "rgb": (analysis.path / "rgb").is_dir(),
            "depth": (analysis.path / "depth").is_dir(),
        },
    }


def _validate_metadata(metadata: dict[str, Any], analysis: _EpisodeAnalysis) -> bool:
    if not metadata:
        analysis.add_error("metadata.json is empty or unavailable")
        return False
    ok = True
    if metadata.get("schema_version") != SCHEMA_VERSION:
        analysis.add_error(f"unsupported trajectory schema: {metadata.get('schema_version')!r}")
        ok = False
    missing = sorted(_REQUIRED_METADATA - set(metadata))
    if missing:
        analysis.add_error(f"metadata is missing required fields: {missing}")
        ok = False
    for key in ("scene_id", "recipe", "robot_asset_source", "isaac_sim_version"):
        if not isinstance(metadata.get(key), str) or not metadata[key]:
            analysis.add_error(f"metadata field {key} must be a non-empty string")
            ok = False
    if isinstance(metadata.get("seed"), bool) or not isinstance(metadata.get("seed"), int):
        analysis.add_error("metadata seed must be an integer")
        ok = False
    frame_count = metadata.get("frame_count")
    if isinstance(frame_count, bool) or not isinstance(frame_count, int) or frame_count < 1:
        analysis.add_error("metadata frame_count must be a positive integer")
        ok = False
    for key in ("control_frequency_hz", "sensor_frequency_hz"):
        value = metadata.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value <= 0:
            analysis.add_error(f"metadata {key} must be a finite positive number")
            ok = False
    camera = metadata.get("camera")
    if not isinstance(camera, dict):
        analysis.add_error("metadata camera must be an object")
        ok = False
    else:
        resolution = camera.get("resolution")
        if (
            not isinstance(resolution, (list, tuple))
            or len(resolution) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in resolution)
        ):
            analysis.add_error("metadata camera resolution must contain two positive integers")
            ok = False
    for key, shape in (("intrinsics", (3, 3)), ("extrinsics", (4, 4))):
        try:
            matrix = _finite_matrix(metadata.get(key), key)
            if _nested_shape(matrix) != shape:
                raise DatasetError(f"metadata {key} must have shape {shape}")
        except (DatasetError, TypeError, ValueError, OverflowError) as exc:
            analysis.add_error(str(exc))
            ok = False
    return ok


def _validate_result_json_shape(result: dict[str, Any], analysis: _EpisodeAnalysis) -> bool:
    if not result:
        analysis.add_error("result.json is empty or unavailable")
        return False
    if result.get("result") not in {"passed", "failed"}:
        analysis.add_error("result.json result must be 'passed' or 'failed'")
        return False
    return True


def _validate_loaded_episode(episode: Episode, analysis: _EpisodeAnalysis) -> None:
    frames = list(episode)
    metadata = episode.metadata
    result = episode.result
    _validate_record_contract(frames, metadata, result, analysis)
    _validate_media_contract(episode, analysis)
    _validate_result_contract(episode, analysis)


def _validate_record_contract(
    frames: list[EpisodeFrame],
    metadata: dict[str, Any],
    result: dict[str, Any],
    analysis: _EpisodeAnalysis,
) -> None:
    ok = True
    if not frames:
        analysis.add_error("episode has no frames")
        analysis.checks["trajectory_records"] = False
        return
    record_episode_id = frames[0].get("episode_id")
    if metadata.get("episode_id") not in (None, record_episode_id):
        analysis.add_error("metadata episode_id does not match trajectory records")
        ok = False
    if metadata.get("start_step") != frames[0].sim_step:
        analysis.add_error("metadata start_step does not match the first trajectory step")
        ok = False
    if metadata.get("end_step") != frames[-1].sim_step:
        analysis.add_error("metadata end_step does not match the last trajectory step")
        ok = False
    expected_phases = [MugLiftPhase.PRE_GRASP.value]
    phase_values = {phase.value for phase in MugLiftPhase}
    phase_sequence: list[str] = []
    terminal_index: int | None = None
    for index, frame in enumerate(frames):
        record = frame.record
        phase = record.get("phase")
        if phase not in phase_values:
            analysis.add_error(f"invalid phase at frame {index}: {phase!r}")
            ok = False
        if not isinstance(record.get("terminated"), bool) or not isinstance(record.get("truncated"), bool):
            analysis.add_error(f"invalid terminal flags at frame {index}")
            ok = False
        if record.get("terminated") and record.get("truncated"):
            analysis.add_error(f"terminated and truncated cannot both be true at frame {index}")
            ok = False
        expected_failed = phase == MugLiftPhase.FAILED.value or record.get("truncated") is True
        if record.get("failed") is not expected_failed:
            analysis.add_error(f"failed flag is inconsistent at frame {index}")
            ok = False
        if phase not in {MugLiftPhase.DONE.value, MugLiftPhase.FAILED.value}:
            if record.get("terminated") or record.get("truncated"):
                analysis.add_error(f"non-terminal phase has terminal flags at frame {index}")
                ok = False
        elif terminal_index is None:
            terminal_index = index
        else:
            analysis.add_error(f"trajectory contains frames after terminal phase at frame {index}")
            ok = False
        if not phase_sequence or phase_sequence[-1] != phase:
            phase_sequence.append(phase)
        for state_key in ("robot_state", "object_state", "oracle", "contact_diagnostics"):
            if not isinstance(record.get(state_key), dict):
                analysis.add_error(f"{state_key} must be an object at frame {index}")
                ok = False
        action = record.get("action")
        if action is not None and not isinstance(action, dict):
            analysis.add_error(f"action must be an object or null at frame {index}")
            ok = False
        if index:
            previous = frames[index - 1].get("phase")
            if phase != previous:
                try:
                    previous_phase = MugLiftPhase(previous)
                    current_phase = MugLiftPhase(phase)
                    legal = current_phase in MUG_LIFT_PHASE_TRANSITIONS.get(previous_phase, ())
                    legal = legal or current_phase == MugLiftPhase.FAILED
                except (TypeError, ValueError):
                    legal = False
                if not legal:
                    analysis.add_error(
                        f"illegal phase transition at frame {index}: {previous!r} -> {phase!r}"
                    )
                    ok = False
    if frames[0].get("phase") != expected_phases[0]:
        analysis.add_error("trajectory must start in PRE_GRASP")
        ok = False
    if terminal_index != len(frames) - 1:
        analysis.add_error("terminal phase must be the last trajectory frame")
        ok = False
    final = frames[-1]
    if not (final.get("terminated") or final.get("truncated")):
        analysis.add_error("last trajectory frame is not terminal")
        ok = False
    if final.get("phase") == MugLiftPhase.DONE.value:
        if final.get("truncated") or not final.get("terminated") or final.get("failed"):
            analysis.add_error("DONE frame has inconsistent terminal flags")
            ok = False
    elif final.get("phase") == MugLiftPhase.FAILED.value:
        if not final.get("failed"):
            analysis.add_error("FAILED frame must have failed=true")
            ok = False
    else:
        analysis.add_error("last trajectory phase must be DONE or FAILED")
        ok = False
    pick_place_phases = {
        MugLiftPhase.TRANSFER.value,
        MugLiftPhase.LOWER.value,
        MugLiftPhase.RELEASE.value,
        MugLiftPhase.VERIFY_PLACE.value,
    }
    is_pick_place = (
        result.get("task") == "pick_and_place"
        or metadata.get("task_mode") == "pick_place"
        or bool(pick_place_phases & set(phase_sequence))
        or result.get("pick") is not None
        or result.get("place") is not None
    )
    if is_pick_place:
        required = {
            MugLiftPhase.PRE_GRASP.value,
            MugLiftPhase.APPROACH.value,
            MugLiftPhase.GRASP.value,
            MugLiftPhase.VERIFY_GRASP.value,
            MugLiftPhase.LIFT.value,
            MugLiftPhase.TRANSFER.value,
            MugLiftPhase.LOWER.value,
            MugLiftPhase.RELEASE.value,
            MugLiftPhase.VERIFY_PLACE.value,
            MugLiftPhase.DONE.value,
        }
        if not required.issubset(set(phase_sequence)) and final.get("phase") == MugLiftPhase.DONE.value:
            analysis.add_error(f"pick-and-place phase coverage is incomplete: {sorted(required - set(phase_sequence))}")
            ok = False
    analysis.checks["trajectory_records"] = ok
    analysis.checks["phase_transitions"] = ok
    analysis.checks["terminal_consistency"] = ok
    analysis.checks["phase_coverage"] = not is_pick_place or required.issubset(set(phase_sequence))
    if not analysis.checks["phase_coverage"]:
        ok = False


def _validate_media_contract(episode: Episode, analysis: _EpisodeAnalysis) -> None:
    root = episode.path
    frames = list(episode)
    rgb_paths: set[Path] = set()
    depth_paths: set[Path] = set()
    ok = True
    camera = episode.metadata.get("camera", {})
    width, height = camera.get("resolution", (0, 0))
    for index, frame in enumerate(frames):
        try:
            rgb = _strict_media_path(root, frame.get("rgb_path"), "rgb", ".png", frame.frame_id)
            depth = _strict_media_path(root, frame.get("depth_path"), "depth", ".npy", frame.frame_id)
            if rgb in rgb_paths or depth in depth_paths:
                raise DatasetError(f"media path is reused at frame {index}")
            rgb_paths.add(rgb)
            depth_paths.add(depth)
            rgb_width, rgb_height = _validate_png_rgb(rgb)
            depth_values = _read_npy_float32(depth)
            sensor = frame.get("sensor")
            if sensor.get("rgb_shape") != [rgb_height, rgb_width, 3]:
                raise DatasetError(f"RGB shape metadata mismatch at frame {index}")
            if sensor.get("depth_shape") != [len(depth_values), len(depth_values[0])]:
                raise DatasetError(f"depth shape metadata mismatch at frame {index}")
            if (rgb_width, rgb_height) != (width, height):
                raise DatasetError(f"RGB dimensions do not match camera resolution at frame {index}")
            if (len(depth_values[0]), len(depth_values)) != (width, height):
                raise DatasetError(f"depth dimensions do not match camera resolution at frame {index}")
            finite_count = sum(math.isfinite(value) for row in depth_values for value in row)
            positive_count = sum(
                math.isfinite(value) and value > 0.0 for row in depth_values for value in row
            )
            if sensor.get("finite_depth_values") != finite_count or positive_count <= 0:
                raise DatasetError(f"depth finite-value metadata is invalid at frame {index}")
            sensor_intrinsics = _finite_matrix(sensor.get("intrinsics"), "intrinsics")
            sensor_extrinsics = _finite_matrix(sensor.get("extrinsics"), "extrinsics")
            if _nested_shape(sensor_intrinsics) != (3, 3) or _nested_shape(sensor_extrinsics) != (4, 4):
                raise DatasetError(f"sensor calibration shape is invalid at frame {index}")
            if not _values_equal(sensor_intrinsics, episode.metadata["intrinsics"]):
                raise DatasetError(f"sensor intrinsics do not match metadata at frame {index}")
            if not _values_equal(sensor_extrinsics, episode.metadata["extrinsics"]):
                raise DatasetError(f"sensor extrinsics do not match metadata at frame {index}")
        except (DatasetError, OSError, TypeError, ValueError, IndexError, OverflowError, AttributeError) as exc:
            analysis.add_error(f"media validation failed at frame {index}: {exc}")
            ok = False
    rgb_files = _media_directory_files(root / "rgb", ".png", analysis)
    depth_files = _media_directory_files(root / "depth", ".npy", analysis)
    if len(rgb_files) != len(frames) or len(depth_files) != len(frames):
        analysis.add_error(
            f"media count mismatch: records={len(frames)}, rgb={len(rgb_files)}, depth={len(depth_files)}"
        )
        ok = False
    if rgb_files != rgb_paths or depth_files != depth_paths:
        analysis.add_error("media files do not exactly match trajectory path references")
        ok = False
    analysis.checks["media_integrity"] = ok
    analysis.checks["sensor_state_synchronization"] = ok


def _validate_result_contract(episode: Episode, analysis: _EpisodeAnalysis) -> None:
    result = episode.result
    frames = list(episode)
    final = frames[-1]
    oracle = final.get("oracle")
    ok = True
    export = result.get("trajectory_export")
    if not isinstance(export, dict):
        analysis.add_error("result.json is missing trajectory_export")
        ok = False
    else:
        if export.get("result") != "passed":
            analysis.add_error("result.json does not confirm a passed trajectory export")
            ok = False
        if export.get("failure_reason") not in (None, ""):
            analysis.add_error("passed trajectory export contains a failure_reason")
            ok = False
        for key, expected in (
            ("frame_count", len(frames)),
            ("trajectory_record_count", len(frames)),
            ("rgb_frame_count", len(frames)),
            ("depth_frame_count", len(frames)),
        ):
            if key in export and export.get(key) != expected:
                analysis.add_error(f"result trajectory_export {key} does not match the dataset")
                ok = False
        if "sensor_state_synchronized" in export and export.get("sensor_state_synchronized") is not True:
            analysis.add_error("result trajectory_export reports unsynchronized sensor state")
            ok = False
    if result.get("result") != "passed":
        analysis.add_error("result.json task result is not passed")
        ok = False
    if result.get("failure_reason") not in (None, ""):
        analysis.add_error("passed result contains a failure_reason")
        ok = False
    if "frame_count" in result and result.get("frame_count") != len(frames):
        analysis.add_error("result.json frame_count does not match the dataset")
        ok = False
    if result.get("task_success") is not True:
        analysis.add_error("passed result must contain task_success=true")
        ok = False
    if oracle.get("task_success") is not True:
        analysis.add_error("final recorded oracle must contain task_success=true")
        ok = False
    top_oracle = result.get("task_oracle")
    if isinstance(top_oracle, dict) and "task_success" in top_oracle and top_oracle.get("task_success") is not True:
        analysis.add_error("top-level task_oracle does not confirm task success")
        ok = False
    for result_key, oracle_key in (
        ("pick", "pick_success"),
        ("place", "placement_stable"),
        ("released", "released"),
        ("gripper_open", "gripper_open"),
    ):
        result_value = result.get(result_key)
        if (result_value == "passed" or result_value is True) and oracle.get(oracle_key) is not True:
            analysis.add_error(f"result {result_key} is inconsistent with the final task oracle")
            ok = False
    if result.get("released") is True and oracle.get("holding") is True:
        analysis.add_error("passed release result is inconsistent with holding=true")
        ok = False
    if result.get("finger_target_contact") is False and oracle.get("released") is not True:
        analysis.add_error("released contact result is inconsistent with the final task oracle")
        ok = False
    if final.get("phase") != MugLiftPhase.DONE.value:
        analysis.add_error("successful result requires final phase DONE")
        ok = False
    if result.get("task") == "pick_and_place" or result.get("pick") is not None or result.get("place") is not None:
        for key, expected in (
            ("pick", "passed"),
            ("place", "passed"),
            ("released", True),
            ("gripper_open", True),
            ("finger_target_contact", False),
        ):
            if result.get(key) is not expected and result.get(key) != expected:
                analysis.add_error(f"pick-and-place result requires {key}={expected!r}")
                ok = False
    for frame in frames:
        expected_reward = 1.0 if frame.get("phase") == MugLiftPhase.DONE.value else 0.0
        if not math.isclose(float(frame.get("reward")), expected_reward, rel_tol=0.0, abs_tol=1e-9):
            analysis.add_error(f"reward is inconsistent with phase at frame {frame.frame_id}")
            ok = False
    analysis.checks["result_consistency"] = ok
    analysis.checks["task_success_contract"] = ok


def _strict_media_path(root: Path, value: Any, directory: str, suffix: str, frame_id: int) -> Path:
    if not isinstance(value, str) or not value:
        raise DatasetError(f"frame {frame_id} has no {directory} media path")
    relative = Path(value)
    windows_relative = PureWindowsPath(value)
    if (
        relative.is_absolute()
        or windows_relative.is_absolute()
        or windows_relative.drive
        or ".." in relative.parts
        or ".." in windows_relative.parts
    ):
        raise DatasetError(f"media path escapes episode directory: {value!r}")
    if relative.parts != (directory, f"{frame_id:06d}{suffix}"):
        raise DatasetError(f"media path is not mapped to frame {frame_id}: {value!r}")
    candidate = root / relative
    if candidate.is_symlink():
        raise DatasetError(f"media path is a symlink: {value!r}")
    path = candidate.resolve()
    resolved_root = root.resolve()
    if resolved_root not in path.parents or path.is_symlink() or not path.is_file():
        raise DatasetError(f"media path is missing or escapes episode directory: {value!r}")
    return path


def _media_directory_files(directory: Path, suffix: str, analysis: _EpisodeAnalysis) -> set[Path]:
    if not directory.is_dir() or directory.is_symlink():
        return set()
    files: set[Path] = set()
    try:
        entries = sorted(directory.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        analysis.add_error(f"cannot read media directory {directory.name}: {exc}")
        return set()
    for entry in entries:
        if entry.is_symlink():
            analysis.add_error(f"media directory contains a symlink: {entry.name}")
            continue
        if entry.is_file() and entry.suffix.lower() == suffix:
            files.add(entry.resolve())
    return files


def _nested_shape(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list):
        return ()
    if not value:
        return (0,)
    child_shapes = [_nested_shape(child) for child in value]
    if any(shape != child_shapes[0] for shape in child_shapes):
        return (-1,)
    return (len(value),) + child_shapes[0]


def _values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(_values_equal(a, b) for a, b in zip(left, right, strict=True))
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-9)
    return left == right


def _replay_task_oracle(episode: Episode) -> tuple[dict[str, Any], list[str]]:
    task = episode.metadata.get("task_spec")
    if task is None:
        task = episode.metadata.get("task")
    if task is None:
        return {"result": "not_available", "reason": "metadata_missing_task_spec"}, []
    if not isinstance(task, dict):
        return {"result": "failed", "reason": "metadata_task_spec_is_not_an_object"}, [
            "task replay metadata task_spec must be an object"
        ]
    from .tasks import TaskEvaluator

    frames = list(episode)
    initial_state, error = _object_positions(frames[0])
    if error:
        return {"result": "failed", "reason": error}, [error]
    try:
        evaluator = TaskEvaluator(task, initial_state)
    except (TypeError, ValueError, KeyError) as exc:
        message = f"cannot initialize task replay evaluator: {exc}"
        return {"result": "failed", "reason": message}, [message]
    errors: list[str] = []
    statuses: list[dict[str, Any]] = []
    for frame in frames:
        state, state_error = _object_positions(frame)
        if state_error:
            errors.append(f"frame {frame.frame_id}: {state_error}")
            continue
        status = evaluator.status(state, dict(frame.get("contact_diagnostics") or {}))
        statuses.append(status)
        recorded = frame.get("oracle") or {}
        if not isinstance(recorded, dict):
            errors.append(f"frame {frame.frame_id}: oracle is not an object")
            continue
        for key, recorded_value in recorded.items():
            if key in status and not _values_equal(recorded_value, status[key]):
                errors.append(
                    f"frame {frame.frame_id}: oracle field {key!r} differs from recomputed value"
                )
    if errors:
        return {"result": "failed", "frames_replayed": len(statuses)}, errors
    return {
        "result": "passed",
        "frames_replayed": len(statuses),
        "task_success": bool(statuses and statuses[-1].get("task_success")),
    }, []


def _object_positions(frame: EpisodeFrame) -> tuple[dict[str, tuple[float, float, float]], str | None]:
    raw_objects = frame.get("object_state")
    if not isinstance(raw_objects, dict):
        return {}, "object_state is not an object"
    positions: dict[str, tuple[float, float, float]] = {}
    for object_id, payload in raw_objects.items():
        position = payload.get("position") if isinstance(payload, dict) else payload
        if not isinstance(position, (list, tuple)) or len(position) != 3:
            return {}, f"object {object_id!r} has no three-dimensional position"
        try:
            values = tuple(float(value) for value in position)
        except (TypeError, ValueError) as exc:
            return {}, f"object {object_id!r} position is invalid: {exc}"
        if not all(math.isfinite(value) for value in values):
            return {}, f"object {object_id!r} position is non-finite"
        positions[str(object_id)] = values
    return positions, None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise DatasetError(f"missing required dataset file: {path.name}")
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            raise DatasetError(f"JSON file is too large: {path.name}")
    except OSError as exc:
        raise DatasetError(f"cannot stat JSON file {path.name}: {exc}") from exc
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DatasetError(f"malformed JSON file {path.name}: {exc}") from exc
    if not isinstance(payload, dict):
        raise DatasetError(f"JSON file {path.name} must contain an object")
    return payload


def _shape(value: Any) -> tuple[int, ...]:
    shape = getattr(value, "shape", None)
    if shape is not None:
        try:
            return tuple(int(item) for item in shape)
        except (TypeError, ValueError) as exc:
            raise DatasetError("sensor array has an invalid shape") from exc
    if not isinstance(value, (list, tuple)):
        raise DatasetError("sensor value is not an array")
    if not value:
        return (0,)
    child = _shape(value[0]) if isinstance(value[0], (list, tuple)) else ()
    for item in value:
        if child:
            if _shape(item) != child:
                raise DatasetError("sensor array is ragged")
        elif isinstance(item, (list, tuple)):
            raise DatasetError("sensor array is ragged")
    return (len(value),) + child


def _tolist(value: Any) -> Any:
    method = getattr(value, "tolist", None)
    if callable(method):
        return method()
    return value


def _camera_resolution(metadata: dict[str, Any], rgb_shape: tuple[int, ...]) -> tuple[int, int]:
    camera = metadata.get("camera", {})
    resolution = camera.get("resolution") if isinstance(camera, dict) else None
    if not isinstance(resolution, (list, tuple)) or len(resolution) != 2:
        return rgb_shape[1], rgb_shape[0]
    width, height = (int(value) for value in resolution)
    if width <= 0 or height <= 0:
        raise DatasetError("camera resolution must be positive")
    return width, height


def _finite_matrix(value: Any, name: str) -> list[Any]:
    if value is None:
        raise DatasetError(f"sensor capture has no {name}")
    data = _tolist(value)

    def walk(item: Any) -> list[Any] | float:
        if isinstance(item, (list, tuple)):
            return [walk(child) for child in item]
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise DatasetError(f"sensor {name} contains a non-numeric value")
        number = float(item)
        if not math.isfinite(number):
            raise DatasetError(f"sensor {name} contains a non-finite value")
        return number

    result = walk(data)
    if not isinstance(result, list):
        raise DatasetError(f"sensor {name} must be an array")
    return result


def _write_png_rgb(path: Path, value: Any) -> None:
    data = _tolist(value)
    shape = _shape(data)
    if len(shape) != 3 or shape[2] != 3:
        raise DatasetError(f"RGB must have shape (height, width, 3), got {shape}")
    rows: list[bytes] = []
    for row in data:
        packed = bytearray()
        for pixel in row:
            for channel in pixel:
                number = int(channel)
                if number < 0 or number > 255 or float(channel) != number:
                    raise DatasetError("RGB values must be integer values in [0, 255]")
                packed.append(number)
        rows.append(bytes(packed))
    raw = b"".join(b"\x00" + row for row in rows)
    height, width = shape[0], shape[1]

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
        )

    payload = b"\x89PNG\r\n\x1a\n"
    payload += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    payload += chunk(b"IDAT", zlib.compress(raw, level=1))
    payload += chunk(b"IEND", b"")
    _atomic_bytes(path, payload)


def _validate_png_rgb(path: Path) -> tuple[int, int]:
    payload = path.read_bytes()
    if len(payload) < 33 or payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise DatasetError(f"malformed RGB PNG: {path.name}")
    offset = 8
    width = height = None
    idat = bytearray()
    saw_iend = False
    while offset < len(payload):
        if len(payload) - offset < 12:
            raise DatasetError(f"truncated RGB PNG chunk: {path.name}")
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        kind = payload[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        chunk_end = data_end + 4
        if chunk_end > len(payload):
            raise DatasetError(f"truncated RGB PNG chunk payload: {path.name}")
        data = payload[data_start:data_end]
        expected_crc = struct.unpack(">I", payload[data_end:chunk_end])[0]
        actual_crc = binascii.crc32(kind + data) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            raise DatasetError(f"RGB PNG chunk CRC mismatch: {path.name}")
        if kind == b"IHDR":
            if offset != 8 or width is not None or length != 13:
                raise DatasetError(f"invalid RGB PNG IHDR: {path.name}")
            width, height, depth, color_type, compression, filter_method, interlace = struct.unpack(
                ">IIBBBBB", data
            )
            if (
                depth != 8
                or color_type != 2
                or compression != 0
                or filter_method != 0
                or interlace != 0
                or width <= 0
                or height <= 0
            ):
                raise DatasetError(f"unsupported RGB PNG encoding: {path.name}")
            if width * height > _MAX_SENSOR_PIXELS:
                raise DatasetError(f"RGB PNG dimensions are too large: {path.name}")
        elif kind == b"IDAT":
            idat.extend(data)
        elif kind == b"IEND":
            if length != 0:
                raise DatasetError(f"invalid RGB PNG IEND: {path.name}")
            saw_iend = True
            offset = chunk_end
            break
        offset = chunk_end
    if width is None or height is None or not idat or not saw_iend or offset != len(payload):
        raise DatasetError(f"incomplete RGB PNG: {path.name}")
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise DatasetError(f"invalid RGB PNG image data: {path.name}") from exc
    row_size = 1 + width * 3
    if len(raw) != height * row_size:
        raise DatasetError(f"RGB PNG image data size mismatch: {path.name}")
    if any(raw[row * row_size] > 4 for row in range(height)):
        raise DatasetError(f"unsupported RGB PNG filter: {path.name}")
    return width, height


def _write_npy_float32(path: Path, value: Any) -> int:
    data = _tolist(value)
    shape = _shape(data)
    if len(shape) != 2:
        raise DatasetError(f"depth must have shape (height, width), got {shape}")
    flattened: list[float] = []
    finite_count = 0
    for row in data:
        for item in row:
            number = float(item)
            flattened.append(number)
            if math.isfinite(number):
                finite_count += 1
    header = repr({"descr": "<f4", "fortran_order": False, "shape": shape})
    header_bytes = header.encode("latin1")
    padding = 16 - ((10 + len(header_bytes) + 1) % 16)
    header_bytes += b" " * (padding - 1) + b"\n"
    if len(header_bytes) > 65535:
        raise DatasetError("depth array header is too large for NPY v1")
    payload = b"\x93NUMPY" + bytes((1, 0)) + struct.pack("<H", len(header_bytes))
    payload += header_bytes
    payload += b"".join(struct.pack("<f", item) for item in flattened)
    _atomic_bytes(path, payload)
    return finite_count


def _read_npy_float32(path: Path) -> list[list[float]]:
    payload = path.read_bytes()
    if len(payload) < 10:
        raise DatasetError(f"truncated depth NPY file: {path.name}")
    if payload[:6] != b"\x93NUMPY" or payload[6:8] != b"\x01\x00":
        raise DatasetError(f"unsupported depth NPY format: {path.name}")
    header_length = struct.unpack("<H", payload[8:10])[0]
    if header_length > 65535:
        raise DatasetError(f"depth NPY header is too large: {path.name}")
    header_end = 10 + header_length
    if header_end > len(payload):
        raise DatasetError(f"truncated depth NPY header: {path.name}")
    try:
        header = ast.literal_eval(payload[10:header_end].decode("latin1").strip())
    except (SyntaxError, ValueError, UnicodeDecodeError) as exc:
        raise DatasetError(f"malformed depth NPY header: {path.name}") from exc
    if (
        not isinstance(header, dict)
        or header.get("descr") != "<f4"
        or header.get("fortran_order") is not False
        or not isinstance(header.get("shape"), tuple)
        or len(header["shape"]) != 2
    ):
        raise DatasetError(f"unsupported depth NPY header: {path.name}")
    raw_shape = header["shape"]
    if any(isinstance(value, bool) or not isinstance(value, int) for value in raw_shape):
        raise DatasetError(f"depth NPY shape is invalid: {path.name}")
    height, width = (int(value) for value in raw_shape)
    if height <= 0 or width <= 0:
        raise DatasetError(f"depth NPY shape must be positive: {path.name}")
    element_count = height * width
    if element_count > _MAX_NPY_ELEMENTS:
        raise DatasetError(f"depth NPY array is too large: {path.name}")
    expected = element_count * 4
    data = payload[header_end:]
    if len(data) != expected:
        raise DatasetError(f"depth NPY payload size mismatch: {path.name}")
    try:
        values = struct.unpack(f"<{height * width}f", data)
    except struct.error as exc:
        raise DatasetError(f"malformed depth NPY payload: {path.name}") from exc
    return [list(values[row * width : (row + 1) * width]) for row in range(height)]


def _atomic_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)
