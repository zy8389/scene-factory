from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


DATASET_SCHEMA_VERSION = "scene_factory.dataset.v1"
SCENE_SCHEMA_VERSION = "scene_factory.dataset_scene.v1"
_REQUIRED_FILES = ("scene_spec", "layout", "validation", "preview")
_OPTIONAL_FILE_NAMES = {"intent": "scene_intent.json", "revision": "revision.json", "usd": "scene.usd"}
_FILE_NAMES = {
    "scene_spec": "scene_spec.json",
    "layout": "layout.json",
    "validation": "validation.json",
    "preview": "preview.svg",
    **_OPTIONAL_FILE_NAMES,
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class DatasetError(ValueError):
    """Raised when a SceneFactory dataset cannot satisfy its contract."""


@dataclass(frozen=True)
class DatasetResult:
    result: str
    valid: bool
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"result": self.result, "valid": self.valid, **self.details}

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)


@dataclass(frozen=True)
class DatasetSnapshot:
    root: Path
    metadata: dict[str, Any]
    records: tuple[dict[str, Any], ...]


def canonical_json(value: Any) -> bytes:
    """Encode JSON semantics independently of whitespace or output location."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def write_json_atomic(path: str | Path, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    _atomic_write_bytes(Path(path), (encoded + "\n").encode("utf-8"))


def write_manifest_atomic(path: str | Path, records: list[dict[str, Any]]) -> None:
    content = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        for record in records
    )
    _atomic_write_bytes(Path(path), content.encode("utf-8"))


def dataset_identity(
    *,
    source: Mapping[str, Any],
    count: int,
    seed_start: int,
    export_usd: bool,
) -> str:
    identity = {
        "source": dict(source),
        "count": count,
        "seed_start": seed_start,
        "export_usd": export_usd,
    }
    return hashlib.sha256(canonical_json(identity)).hexdigest()[:16]


def make_dataset_metadata(
    *,
    recipe_name: str | None,
    prompt: str | None,
    count: int,
    seed_start: int,
    export_usd: bool,
    status: str = "in_progress",
) -> dict[str, Any]:
    if bool(recipe_name) == bool(prompt):
        raise DatasetError("provide exactly one of recipe_name or prompt")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise DatasetError("count must be a positive integer")
    if isinstance(seed_start, bool) or not isinstance(seed_start, int):
        raise DatasetError("seed_start must be an integer")
    if not isinstance(export_usd, bool):
        raise DatasetError("export_usd must be boolean")
    if status not in {"in_progress", "incomplete", "complete"}:
        raise DatasetError("invalid dataset status")
    if recipe_name is not None and not isinstance(recipe_name, str):
        raise DatasetError("recipe_name must be a string")
    if prompt is not None and not isinstance(prompt, str):
        raise DatasetError("prompt must be a string")
    source: dict[str, Any] = {"type": "recipe", "recipe": recipe_name} if recipe_name else {
        "type": "prompt",
        "prompt": prompt,
    }
    identity = dataset_identity(
        source=source,
        count=count,
        seed_start=seed_start,
        export_usd=export_usd,
    )
    return {
        "schema_version": DATASET_SCHEMA_VERSION,
        "dataset_id": identity,
        "source": source,
        "seed_start": seed_start,
        "count": count,
        "expected_seed_end": seed_start + count - 1,
        "manifest": "manifest.jsonl",
        "export_usd": export_usd,
        "status": status,
        "result": "incomplete" if status != "complete" else "passed",
        "scene_count": 0,
        "valid_scene_count": 0,
        "invalid_scene_count": 0,
        "generation_error": None,
    }


def update_dataset_metadata(
    metadata: Mapping[str, Any],
    records: list[Mapping[str, Any]],
    *,
    status: str,
    generation_error: str | None = None,
) -> dict[str, Any]:
    valid_count = sum(bool(record.get("valid")) for record in records)
    updated = dict(metadata)
    updated.update(
        {
            "status": status,
            "result": "passed" if status == "complete" and valid_count == len(records) else "incomplete",
            "scene_count": len(records),
            "valid_scene_count": valid_count,
            "invalid_scene_count": len(records) - valid_count,
            "generation_error": generation_error,
        }
    )
    if status == "complete" and valid_count != len(records):
        updated["result"] = "failed"
    return updated


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DatasetError(f"invalid JSON {path.name}: {exc}") from exc


def _safe_relative_path(value: Any) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise DatasetError(f"manifest path is not a non-empty string: {value!r}")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or normalized.startswith("//")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(":" in part for part in path.parts)
    ):
        raise DatasetError(f"manifest path is not a safe relative path: {value!r}")
    return path


def _resolve_manifest_file(root: Path, value: Any) -> tuple[PurePosixPath, Path]:
    relative = _safe_relative_path(value)
    root_resolved = root.resolve()
    candidate = (root / Path(*relative.parts)).resolve(strict=False)
    if not candidate.is_relative_to(root_resolved):
        raise DatasetError(f"manifest path escapes dataset root: {value!r}")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise DatasetError(f"manifest path contains symlink: {value!r}")
    if not candidate.is_file():
        raise DatasetError(f"manifest artifact is missing: {value!r}")
    return relative, candidate


def _scene_id_is_safe(scene_id: Any) -> bool:
    return (
        isinstance(scene_id, str)
        and bool(scene_id)
        and scene_id not in {".", ".."}
        and "/" not in scene_id
        and "\\" not in scene_id
        and not re.match(r"^[A-Za-z]:", scene_id)
    )


def _load_manifest(root: Path) -> list[dict[str, Any]]:
    path = root / "manifest.jsonl"
    if not path.is_file() or path.is_symlink():
        raise DatasetError("manifest.jsonl is missing or is a symlink")
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise DatasetError(f"cannot read manifest.jsonl: {exc}") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            raise DatasetError(f"manifest.jsonl line {line_number} is blank")
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"manifest.jsonl line {line_number} is malformed: {exc}") from exc
        if not isinstance(record, dict):
            raise DatasetError(f"manifest.jsonl line {line_number} is not an object")
        records.append(record)
    return records


def _validate_metadata(metadata: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(metadata, dict):
        return ["dataset.json must contain a JSON object"]
    if metadata.get("schema_version") != DATASET_SCHEMA_VERSION:
        errors.append("dataset.json has an unsupported schema_version")
    source = metadata.get("source")
    if not isinstance(source, dict) or source.get("type") not in {"recipe", "prompt"}:
        errors.append("dataset.json source must be a recipe or prompt object")
    elif source["type"] == "recipe" and (
        not isinstance(source.get("recipe"), str) or not source.get("recipe", "").strip()
    ):
        errors.append("dataset.json recipe source requires a recipe string")
    elif source["type"] == "prompt" and (
        not isinstance(source.get("prompt"), str) or not source.get("prompt", "").strip()
    ):
        errors.append("dataset.json prompt source requires a prompt string")
    count = metadata.get("count")
    seed_start = metadata.get("seed_start")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        errors.append("dataset.json count must be a positive integer")
    if isinstance(seed_start, bool) or not isinstance(seed_start, int):
        errors.append("dataset.json seed_start must be an integer")
    if isinstance(count, int) and count > 0 and isinstance(seed_start, int):
        if metadata.get("expected_seed_end") != seed_start + count - 1:
            errors.append("dataset.json expected_seed_end does not match count")
    if metadata.get("manifest") != "manifest.jsonl":
        errors.append("dataset.json manifest must be manifest.jsonl")
    if not isinstance(metadata.get("export_usd"), bool):
        errors.append("dataset.json export_usd must be boolean")
    if metadata.get("status") not in {"in_progress", "incomplete", "complete"}:
        errors.append("dataset.json status is invalid")
    if metadata.get("generation_error") is not None and not isinstance(
        metadata.get("generation_error"), str
    ):
        errors.append("dataset.json generation_error must be a string or null")
    if metadata.get("status") == "complete" and metadata.get("generation_error") is not None:
        errors.append("complete dataset cannot retain a generation_error")
    expected_identity = None
    if isinstance(source, dict) and isinstance(count, int) and isinstance(seed_start, int):
        expected_identity = dataset_identity(
            source=source,
            count=count,
            seed_start=seed_start,
            export_usd=bool(metadata.get("export_usd")),
        )
    if metadata.get("dataset_id") != expected_identity:
        errors.append("dataset.json dataset_id does not match deterministic source metadata")
    return errors


def _semantic_scene_payload(paths: Mapping[str, Path]) -> dict[str, Any]:
    scene_spec = _read_json(paths["scene_spec"])
    layout = _read_json(paths["layout"])
    validation = _read_json(paths["validation"])
    if not all(isinstance(item, dict) for item in (scene_spec, layout, validation)):
        raise DatasetError("scene_spec, layout and validation artifacts must be JSON objects")
    scene_spec = {
        key: value
        for key, value in scene_spec.items()
        if key not in {"prompt_parser", "parser_warning", "revision_of", "revision_instruction"}
    }
    payload: dict[str, Any] = {
        "scene_spec": scene_spec,
        "layout": layout,
        "validation": validation,
    }
    for name in ("intent", "revision"):
        if name in paths:
            optional = _read_json(paths[name])
            if not isinstance(optional, dict):
                raise DatasetError(f"{name} artifact must be a JSON object")
            payload[name] = optional
    return payload


def semantic_fingerprint(paths: Mapping[str, str | Path]) -> str:
    resolved = {name: Path(path) for name, path in paths.items()}
    return hashlib.sha256(canonical_json(_semantic_scene_payload(resolved))).hexdigest()


def make_manifest_record(
    result: Any,
    files: Mapping[str, str | Path],
    dataset_root: str | Path,
    scene_id: str,
) -> dict[str, Any]:
    if not _scene_id_is_safe(scene_id):
        raise DatasetError(f"unsafe scene_id: {scene_id!r}")
    if getattr(result.scene, "scene_id", None) != scene_id:
        raise DatasetError("manifest scene_id does not match build result")
    root = Path(dataset_root).resolve()
    actual_paths: dict[str, Path] = {}
    portable_files: dict[str, str] = {}
    for name, raw_path in files.items():
        if name not in _FILE_NAMES:
            raise DatasetError(f"unknown scene artifact name: {name}")
        path = Path(raw_path)
        if path.name != _FILE_NAMES[name] or not path.is_file():
            raise DatasetError(f"invalid scene artifact for {name}: {path}")
        if not path.resolve().is_relative_to(root):
            raise DatasetError(f"scene artifact escapes dataset root: {path}")
        actual_relative = path.resolve().relative_to(root)
        current = root
        for part in actual_relative.parts:
            current /= part
            if current.is_symlink():
                raise DatasetError(f"scene artifact contains symlink: {path}")
        actual_paths[name] = path
        portable_files[name] = f"{scene_id}/{path.name}"
    for name in _REQUIRED_FILES:
        if name not in actual_paths:
            raise DatasetError(f"required scene artifact is missing: {name}")
    return {
        "schema_version": SCENE_SCHEMA_VERSION,
        "scene_id": scene_id,
        "seed": int(result.scene.seed),
        "recipe": str(result.scene.recipe_name),
        "valid": bool(result.valid),
        "prompt_parser": str(result.prompt_parser),
        "parser_warning": result.parser_warning,
        "files": portable_files,
        "sha256": {name: sha256_file(path) for name, path in actual_paths.items()},
        "fingerprint": semantic_fingerprint(actual_paths),
    }


def _validate_record(root: Path, record: Any, metadata: Mapping[str, Any]) -> tuple[str | None, int | None, list[str]]:
    errors: list[str] = []
    if not isinstance(record, dict):
        return None, None, ["manifest record is not an object"]
    if record.get("schema_version") != SCENE_SCHEMA_VERSION:
        errors.append("unsupported scene manifest schema_version")
    scene_id = record.get("scene_id")
    seed = record.get("seed")
    if not _scene_id_is_safe(scene_id):
        errors.append(f"unsafe scene_id: {scene_id!r}")
        scene_id = None
    if isinstance(seed, bool) or not isinstance(seed, int):
        errors.append(f"{record.get('scene_id', '<unknown>')}: seed must be an integer")
        seed = None
    if not isinstance(record.get("recipe"), str) or not record.get("recipe"):
        errors.append(f"{scene_id or '<unknown>'}: recipe is missing")
    if not isinstance(record.get("valid"), bool):
        errors.append(f"{scene_id or '<unknown>'}: valid must be boolean")
    files = record.get("files")
    hashes = record.get("sha256")
    if not isinstance(files, dict) or not isinstance(hashes, dict):
        errors.append(f"{scene_id or '<unknown>'}: files and sha256 must be objects")
        return scene_id, seed, errors
    if set(files) != set(hashes):
        errors.append(f"{scene_id or '<unknown>'}: files and sha256 keys differ")
    for name in _REQUIRED_FILES:
        if name not in files:
            errors.append(f"{scene_id or '<unknown>'}: required artifact {name} is missing")
    if set(files) - set(_REQUIRED_FILES) - set(_OPTIONAL_FILE_NAMES):
        errors.append(f"{scene_id or '<unknown>'}: unknown artifact key")
    if metadata.get("export_usd") is True and "usd" not in files:
        errors.append(f"{scene_id or '<unknown>'}: export_usd dataset requires scene.usd")
    resolved_files: dict[str, Path] = {}
    for name, value in files.items():
        try:
            relative, path = _resolve_manifest_file(root, value)
            if scene_id is not None and (
                len(relative.parts) != 2
                or relative.parts[0] != scene_id
                or relative.parts[1] != _FILE_NAMES.get(name)
            ):
                errors.append(f"{scene_id}: artifact path is not the canonical scene path: {value!r}")
            resolved_files[name] = path
        except DatasetError as exc:
            errors.append(f"{scene_id or '<unknown>'} {name}: {exc}")
        expected_hash = hashes.get(name)
        if not isinstance(expected_hash, str) or not _SHA256_RE.fullmatch(expected_hash):
            errors.append(f"{scene_id or '<unknown>'} {name}: invalid sha256")
        elif name in resolved_files:
            try:
                actual_hash = sha256_file(resolved_files[name])
            except OSError as exc:
                errors.append(f"{scene_id} {name}: cannot hash artifact: {exc}")
            else:
                if actual_hash != expected_hash:
                    errors.append(
                        f"{scene_id} {name}: sha256 mismatch expected={expected_hash} actual={actual_hash}"
                    )
    fingerprint = record.get("fingerprint")
    if not isinstance(fingerprint, str) or not _SHA256_RE.fullmatch(fingerprint):
        errors.append(f"{scene_id or '<unknown>'}: invalid fingerprint")
    elif all(name in resolved_files for name in _REQUIRED_FILES):
        try:
            actual_fingerprint = semantic_fingerprint(resolved_files)
            if actual_fingerprint != fingerprint:
                errors.append(
                    f"{scene_id}: fingerprint mismatch expected={fingerprint} actual={actual_fingerprint}"
                )
        except DatasetError as exc:
            errors.append(f"{scene_id}: cannot calculate fingerprint: {exc}")
    for key, expected_name in _FILE_NAMES.items():
        if key in files and Path(str(files[key])).name != expected_name:
            errors.append(f"{scene_id or '<unknown>'}: {key} must reference {expected_name}")
    if resolved_files.get("layout"):
        try:
            layout = _read_json(resolved_files["layout"])
            if isinstance(layout, dict):
                if layout.get("scene_id") != scene_id:
                    errors.append(f"{scene_id}: layout scene_id does not match manifest")
                if layout.get("seed") != seed:
                    errors.append(f"{scene_id}: layout seed does not match manifest")
                if layout.get("recipe_name") != record.get("recipe"):
                    errors.append(f"{scene_id}: layout recipe does not match manifest")
        except DatasetError as exc:
            errors.append(f"{scene_id}: {exc}")
    if resolved_files.get("scene_spec"):
        try:
            spec = _read_json(resolved_files["scene_spec"])
            if isinstance(spec, dict):
                if spec.get("scene_id") != scene_id or spec.get("seed") != seed:
                    errors.append(f"{scene_id}: scene_spec identity does not match manifest")
        except DatasetError as exc:
            errors.append(f"{scene_id}: {exc}")
    if resolved_files.get("validation"):
        try:
            validation = _read_json(resolved_files["validation"])
            if isinstance(validation, dict) and validation.get("valid") != record.get("valid"):
                errors.append(f"{scene_id}: validation.valid does not match manifest.valid")
        except DatasetError as exc:
            errors.append(f"{scene_id}: {exc}")
    source = metadata.get("source")
    if isinstance(source, dict) and source.get("type") == "recipe" and record.get("recipe") != source.get("recipe"):
        errors.append(f"{scene_id}: record recipe does not match dataset source")
    return scene_id, seed, errors


def _collect_dataset(
    path: str | Path,
    *,
    allow_incomplete: bool,
) -> tuple[Path, dict[str, Any], list[dict[str, Any]], list[str], dict[str, Any]]:
    root = Path(path).expanduser().resolve()
    errors: list[str] = []
    if not root.is_dir() or root.is_symlink():
        return root, {}, [], [f"dataset root is missing or is a symlink: {root}"], {}
    metadata_path = root / "dataset.json"
    if not metadata_path.is_file() or metadata_path.is_symlink():
        if (root / "manifest.jsonl").is_file():
            return root, {}, [], ["legacy_dataset: dataset.json is missing; upgrade_required"], {
                "legacy": True
            }
        return root, {}, [], ["dataset.json is missing"], {}
    try:
        metadata = _read_json(metadata_path)
    except DatasetError as exc:
        return root, {}, [], [str(exc)], {}
    errors.extend(_validate_metadata(metadata))
    try:
        records = _load_manifest(root)
    except DatasetError as exc:
        return root, metadata if isinstance(metadata, dict) else {}, [], [str(exc)], {}
    if not isinstance(metadata, dict):
        return root, {}, records, errors, {}
    count = metadata.get("count")
    seed_start = metadata.get("seed_start")
    expected_seeds = set(range(seed_start, seed_start + count)) if isinstance(count, int) and isinstance(seed_start, int) and count > 0 else set()
    seen_seeds: set[int] = set()
    seen_scene_ids: set[str] = set()
    for record in records:
        scene_id, seed, record_errors = _validate_record(root, record, metadata)
        errors.extend(record_errors)
        if scene_id is not None:
            if scene_id in seen_scene_ids:
                errors.append(f"duplicate scene_id: {scene_id}")
            seen_scene_ids.add(scene_id)
            scene_dir = root / scene_id
            if not scene_dir.is_dir() or scene_dir.is_symlink():
                errors.append(f"{scene_id}: scene directory is missing or is a symlink")
        if seed is not None:
            if seed in seen_seeds:
                errors.append(f"duplicate seed: {seed}")
            seen_seeds.add(seed)
            if expected_seeds and seed not in expected_seeds:
                errors.append(f"seed outside expected range: {seed}")
    record_seeds = [record.get("seed") for record in records]
    if all(isinstance(seed, int) and not isinstance(seed, bool) for seed in record_seeds):
        if record_seeds != sorted(record_seeds):
            errors.append("manifest records are not ordered by ascending seed")
    missing_seeds = sorted(expected_seeds - seen_seeds)
    if not allow_incomplete and missing_seeds:
        errors.append(f"missing expected seeds: {missing_seeds}")
    if not allow_incomplete and metadata.get("status") != "complete":
        errors.append(f"dataset status is {metadata.get('status')!r}, expected complete")
    if not allow_incomplete and any(not record.get("valid", False) for record in records):
        errors.append("dataset contains invalid scene records")
    if metadata.get("status") == "complete" and not allow_incomplete and metadata.get("result") != "passed":
        errors.append("complete dataset does not have result=passed")
    root_scene_ids = set()
    for child in root.iterdir():
        if child.name in {"dataset.json", "manifest.jsonl"}:
            continue
        if child.name == ".staging":
            if not child.is_dir() or child.is_symlink():
                errors.append(".staging is not a real directory")
            elif any(child.iterdir()):
                errors.append(".staging contains uncommitted residue")
            continue
        if child.is_dir():
            root_scene_ids.add(child.name)
        else:
            errors.append(f"untracked file at dataset root: {child.name}")
    extra_scene_ids = sorted(root_scene_ids - seen_scene_ids)
    missing_scene_dirs = sorted(seen_scene_ids - root_scene_ids)
    if extra_scene_ids:
        errors.append(f"untracked scene directories: {extra_scene_ids}")
    if missing_scene_dirs:
        errors.append(f"missing scene directories: {missing_scene_dirs}")
    summary = {
        "scene_count": len(records),
        "valid_scene_count": sum(bool(record.get("valid")) for record in records),
        "invalid_scene_count": sum(not bool(record.get("valid")) for record in records),
        "expected_count": count,
        "missing_seeds": missing_seeds,
        "seed_coverage": sorted(seen_seeds),
        "errors": errors,
    }
    expected_counts = {
        "scene_count": len(records),
        "valid_scene_count": sum(bool(record.get("valid")) for record in records),
        "invalid_scene_count": sum(not bool(record.get("valid")) for record in records),
    }
    for field, value in expected_counts.items():
        if metadata.get(field) != value:
            errors.append(f"dataset.json {field} does not match manifest: expected={value}")
    if metadata.get("status") != "complete" and metadata.get("result") != "incomplete":
        errors.append("incomplete dataset must have result=incomplete")
    if metadata.get("status") == "complete":
        expected_result = "passed" if expected_counts["invalid_scene_count"] == 0 else "failed"
        if metadata.get("result") != expected_result:
            errors.append(f"complete dataset must have result={expected_result}")
    return root, metadata, records, errors, summary


def load_dataset_snapshot(path: str | Path, *, allow_incomplete: bool = False) -> DatasetSnapshot:
    root, metadata, records, errors, _summary = _collect_dataset(path, allow_incomplete=allow_incomplete)
    structural_errors = [error for error in errors if not (
        error.startswith("missing expected seeds:")
        or error.startswith("dataset status is ")
        or error == "dataset contains invalid scene records"
        or error == "complete dataset does not have result=passed"
        or error == ".staging contains uncommitted residue"
    )]
    if structural_errors:
        raise DatasetError("; ".join(structural_errors))
    return DatasetSnapshot(root, metadata, tuple(records))


def inspect_dataset(path: str | Path) -> DatasetResult:
    root = Path(path).expanduser().resolve()
    if not (root / "dataset.json").is_file() and (root / "manifest.jsonl").is_file():
        try:
            records = _load_manifest(root)
            return DatasetResult(
                "legacy_dataset",
                False,
                {"path": str(root), "upgrade_required": True, "manifest_records": len(records)},
            )
        except DatasetError as exc:
            return DatasetResult("legacy_dataset", False, {"path": str(root), "errors": [str(exc)]})
    try:
        _root, metadata, records, errors, summary = _collect_dataset(path, allow_incomplete=True)
    except OSError as exc:
        return DatasetResult("failed", False, {"path": str(root), "errors": [str(exc)]})
    return DatasetResult(
        "passed" if not errors and metadata.get("status") == "complete" else "failed",
        not errors and metadata.get("status") == "complete",
        {"path": str(root), "metadata": metadata, "records": len(records), "summary": summary, "errors": errors},
    )


def validate_dataset(path: str | Path, *, allow_incomplete: bool = False) -> DatasetResult:
    root = Path(path).expanduser().resolve()
    try:
        root, metadata, records, errors, summary = _collect_dataset(
            path, allow_incomplete=allow_incomplete
        )
    except OSError as exc:
        return DatasetResult(
            "failed",
            False,
            {"path": str(root), "metadata": {}, "records": 0, "summary": {}, "errors": [str(exc)]},
        )
    valid = not errors
    result = "passed" if valid else ("incomplete" if allow_incomplete and metadata else "failed")
    return DatasetResult(
        result,
        valid,
        {
            "path": str(root),
            "metadata": metadata,
            "records": len(records),
            "summary": summary,
            "errors": errors,
        },
    )


def _reproduction_build(factory: Any, source: Mapping[str, Any], seed: int) -> Any:
    from .factory import BuildResult

    if source["type"] == "recipe":
        return factory.build_from_recipe(source["recipe"], seed)
    recipe = factory.recipes.match_prompt(source["prompt"])
    scene = factory.layout_solver.compile(recipe, seed, description_override=source["prompt"])
    return BuildResult(recipe, scene, factory.validator.validate(scene), prompt_parser="keyword")


def reproduce_dataset(path: str | Path) -> DatasetResult:
    snapshot_result = validate_dataset(path)
    if not snapshot_result.valid:
        return DatasetResult(
            "failed",
            False,
            {"reason": "dataset_invalid", "validation": snapshot_result.to_dict()},
        )
    snapshot = load_dataset_snapshot(path)
    source = snapshot.metadata["source"]
    if source["type"] == "prompt":
        parsers = {str(record.get("prompt_parser", "")) for record in snapshot.records}
        if any(parser.startswith("llm:") for parser in parsers):
            return DatasetResult(
                "not_available",
                False,
                {"reason": "nondeterministic_external_parser", "parsers": sorted(parsers)},
            )
    try:
        from .factory import SceneFactory

        factory = SceneFactory()
    except Exception as exc:
        return DatasetResult("failed", False, {"reason": "factory_unavailable", "error": str(exc)})
    comparisons: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="scene_factory_reproduce_") as temporary_directory:
        for record in sorted(snapshot.records, key=lambda item: item["seed"]):
            seed = record["seed"]
            scene_id = record["scene_id"]
            try:
                rebuilt = _reproduction_build(factory, source, seed)
                scene_dir = Path(temporary_directory) / scene_id
                rebuilt_files = factory.write_result(rebuilt, scene_dir, export_usd=False)
                reproduced_fingerprint = semantic_fingerprint(rebuilt_files)
                match = (
                    rebuilt.scene.scene_id == scene_id
                    and reproduced_fingerprint == record["fingerprint"]
                )
                comparisons.append(
                    {
                        "seed": seed,
                        "scene_id": scene_id,
                        "recorded_fingerprint": record["fingerprint"],
                        "reproduced_fingerprint": reproduced_fingerprint,
                        "match": match,
                    }
                )
            except Exception as exc:
                comparisons.append(
                    {
                        "seed": seed,
                        "scene_id": scene_id,
                        "recorded_fingerprint": record.get("fingerprint"),
                        "reproduced_fingerprint": None,
                        "match": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
    valid = all(item["match"] for item in comparisons)
    return DatasetResult(
        "passed" if valid else "failed",
        valid,
        {"source": source, "comparisons": comparisons, "scene_count": len(comparisons)},
    )


def clean_known_staging(
    root: str | Path,
    dataset_key: str,
    *,
    seed_start: int | None = None,
    count: int | None = None,
) -> None:
    staging_root = Path(root) / ".staging"
    if not staging_root.exists():
        return
    if not staging_root.is_dir() or staging_root.is_symlink():
        raise DatasetError(".staging is not a real directory")
    for child in staging_root.iterdir():
        marker = child / ".scene_factory_staging.json"
        known = False
        if child.is_dir() and not child.is_symlink() and marker.is_file() and not marker.is_symlink():
            try:
                payload = _read_json(marker)
                known = (
                    isinstance(payload, dict)
                    and payload.get("schema_version") == SCENE_SCHEMA_VERSION
                    and payload.get("dataset_id") == dataset_key
                    and payload.get("scene_id") == child.name
                    and isinstance(payload.get("seed"), int)
                    and not isinstance(payload.get("seed"), bool)
                    and (
                        seed_start is None
                        or count is None
                        or seed_start <= payload["seed"] < seed_start + count
                    )
                    and _scene_id_is_safe(child.name)
                )
            except DatasetError:
                known = False
        if not known:
            raise DatasetError(f"unknown staging residue cannot be cleaned: {child}")
        shutil.rmtree(child)
    try:
        staging_root.rmdir()
    except OSError:
        pass


def write_staging_marker(path: str | Path, *, dataset_key: str, scene_id: str, seed: int) -> None:
    write_json_atomic(
        Path(path) / ".scene_factory_staging.json",
        {
            "schema_version": SCENE_SCHEMA_VERSION,
            "dataset_id": dataset_key,
            "scene_id": scene_id,
            "seed": seed,
        },
    )
