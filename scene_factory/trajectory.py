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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = "scene_factory.trajectory.v1"
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
    for line_number, line in enumerate(trajectory_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            raise DatasetError(f"blank trajectory record at line {line_number}")
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
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
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
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
            if width is not None or length != 13:
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
    height, width = (int(value) for value in header["shape"])
    if height <= 0 or width <= 0:
        raise DatasetError(f"depth NPY shape must be positive: {path.name}")
    expected = height * width * 4
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
