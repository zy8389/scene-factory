from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from .dataset import (
    DatasetError,
    DatasetResult,
    audit_dataset,
    canonical_json,
    load_dataset_snapshot,
    reproduce_dataset,
    write_json_atomic,
)
from .quality import QUALITY_LEVELS


SAMPLING_PLAN_SCHEMA_VERSION = "scene_factory.sampling_plan.v1"
SAMPLING_COLLECTION_SCHEMA_VERSION = "scene_factory.sampling_collection.v1"
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_RESERVED_IDS = {"con", "prn", "aux", "nul", *(f"com{index}" for index in range(1, 10)),
                 *(f"lpt{index}" for index in range(1, 10))}


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def _sha256(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def normalize_sampling_plan(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) - {"schema_version", "minimum_level", "strata"}:
        raise DatasetError("sampling plan must be an object with known fields")
    if raw.get("schema_version") != SAMPLING_PLAN_SCHEMA_VERSION:
        raise DatasetError("unsupported sampling plan schema_version")
    level = raw.get("minimum_level", "layout")
    if level not in QUALITY_LEVELS:
        raise DatasetError("invalid sampling minimum_level")
    strata_raw = raw.get("strata")
    if not isinstance(strata_raw, list) or not strata_raw:
        raise DatasetError("sampling plan requires non-empty strata")
    strata: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    ranges: dict[str, list[tuple[int, int]]] = {}
    for entry in strata_raw:
        keys = {"id", "recipe", "quota", "candidate_count", "seed_start", "filter"}
        if not isinstance(entry, dict) or set(entry) - keys:
            raise DatasetError("sampling stratum must be an object with known fields")
        identifier = entry.get("id")
        recipe = entry.get("recipe")
        if (not isinstance(identifier, str) or not _IDENTIFIER.fullmatch(identifier)
                or identifier.lower() in _RESERVED_IDS or identifier.lower() in seen_ids):
            raise DatasetError("stratum id must be a unique portable directory name")
        seen_ids.add(identifier.lower())
        if not isinstance(recipe, str) or not recipe.strip():
            raise DatasetError("stratum recipe must be a non-empty name")
        for field in ("quota", "candidate_count", "seed_start"):
            value = entry.get(field)
            if isinstance(value, bool) or not isinstance(value, int):
                raise DatasetError(f"stratum {field} must be an integer")
        quota, count, start = entry["quota"], entry["candidate_count"], entry["seed_start"]
        if quota < 1 or count < quota:
            raise DatasetError("stratum requires candidate_count >= quota >= 1")
        end = start + count
        if any(start < prior_end and prior_start < end for prior_start, prior_end in ranges.get(recipe, [])):
            raise DatasetError("seed ranges for the same recipe must not overlap")
        ranges.setdefault(recipe, []).append((start, end))
        region_filter = entry.get("filter")
        if region_filter is not None:
            if not isinstance(region_filter, dict) or set(region_filter) != {"object_id", "region_xy"}:
                raise DatasetError("filter requires exactly object_id and region_xy")
            object_id = region_filter["object_id"]
            bounds = region_filter["region_xy"]
            if not isinstance(object_id, str) or not object_id.strip():
                raise DatasetError("filter object_id must be non-empty")
            if not isinstance(bounds, (list, tuple)) or len(bounds) != 4 or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                or (isinstance(value, float) and not math.isfinite(value)) for value in bounds
            ):
                raise DatasetError("filter region_xy must contain four finite numbers")
            try:
                bounds = [float(value) for value in bounds]
            except OverflowError as exc:
                raise DatasetError("filter region_xy numbers exceed finite bounds") from exc
            if not all(math.isfinite(value) for value in bounds) or bounds[0] >= bounds[1] or bounds[2] >= bounds[3]:
                raise DatasetError("filter region_xy must have increasing finite bounds")
            region_filter = {"object_id": object_id, "region_xy": bounds}
        strata.append({
            "id": identifier, "recipe": recipe, "quota": quota,
            "candidate_count": count, "seed_start": start, "filter": region_filter,
        })
    return {"schema_version": SAMPLING_PLAN_SCHEMA_VERSION, "minimum_level": level, "strata": strata}


def load_sampling_plan(path: str | Path) -> dict[str, Any]:
    return normalize_sampling_plan(_read_json(Path(path).expanduser()))


def _root_path(path: str | Path) -> Path:
    raw = Path(path).expanduser().absolute()
    if raw.is_symlink():
        raise DatasetError("sampling collection root must not be a symlink")
    return raw.resolve()


def _check_root(root: Path, plan: Mapping[str, Any]) -> None:
    if not root.is_dir():
        raise DatasetError("sampling collection directory is missing")
    for child in root.iterdir():
        if (child.name not in {"sampling_plan.json", "collection.json", "datasets"}
                or child.is_symlink() or not child.resolve().is_relative_to(root)):
            raise DatasetError(f"unexpected collection entry: {child.name}")
        if child.name != "datasets" and not child.is_file():
            raise DatasetError(f"collection control file is not a file: {child.name}")
    datasets = root / "datasets"
    if datasets.exists():
        if not datasets.is_dir():
            raise DatasetError("collection datasets must be a directory")
        identifiers = {entry["id"] for entry in plan["strata"]}
        for child in datasets.iterdir():
            if (child.name not in identifiers or child.is_symlink() or not child.is_dir()
                    or not child.resolve().is_relative_to(root)):
                raise DatasetError(f"unexpected collection dataset: {child.name}")


def _source_fingerprints(factory: Any, plan: Mapping[str, Any]) -> dict[str, Any]:
    recipes = {}
    for entry in plan["strata"]:
        recipe = factory.recipes.get(entry["recipe"])
        if entry["filter"] and entry["filter"]["object_id"] not in {item.object_id for item in recipe.objects}:
            raise DatasetError(f"filter object is absent from recipe: {entry['id']}")
        recipes[recipe.name] = _sha256(recipe.to_dict())
    records = sorted(factory.registry.list(), key=lambda item: item.asset_id)
    return {"registry_sha256": _sha256([asdict(item) for item in records]),
            "recipe_sha256": dict(sorted(recipes.items()))}


def _read_state(root: Path, plan: Mapping[str, Any]) -> dict[str, Any]:
    state = _read_json(root / "collection.json")
    if (not isinstance(state, dict) or state.get("schema_version") != SAMPLING_COLLECTION_SCHEMA_VERSION
            or state.get("plan_sha256") != _sha256(plan)
            or state.get("status") not in {"in_progress", "incomplete", "complete"}):
        raise DatasetError("collection version, plan identity or status is invalid")
    sources = state.get("sources")
    names = {entry["recipe"] for entry in plan["strata"]}
    if (not isinstance(sources, dict) or set(sources) != {"registry_sha256", "recipe_sha256"}
            or not isinstance(sources["recipe_sha256"], dict) or set(sources["recipe_sha256"]) != names
            or any(not isinstance(value, str) or not _HASH.fullmatch(value)
                   for value in [sources["registry_sha256"], *sources["recipe_sha256"].values()])):
        raise DatasetError("collection source fingerprints are invalid")
    if not isinstance(state.get("valid"), bool) or not isinstance(state.get("selected"), list):
        raise DatasetError("collection validity and selection are malformed")
    if state["status"] != "complete" and (state["valid"] or state["selected"]):
        raise DatasetError("incomplete collection must not release a selection")
    return state


def _base_report(plan: Mapping[str, Any], sources: Mapping[str, Any]) -> dict[str, Any]:
    return {"schema_version": SAMPLING_COLLECTION_SCHEMA_VERSION, "plan_sha256": _sha256(plan),
            "sources": dict(sources), "minimum_level": plan["minimum_level"],
            "evidence_scope": "offline_recorded_layout_only", "selected": []}


def _evaluate_collection(root: Path, plan: Mapping[str, Any], sources: Mapping[str, Any]) -> DatasetResult:
    selected: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    seen_content: set[str] = set()
    errors: list[str] = []
    recipe_counts: Counter[str] = Counter()
    room_counts: Counter[str] = Counter()
    event_counts: Counter[str] = Counter()
    asset_counts: Counter[str] = Counter()
    for entry in plan["strata"]:
        dataset_path = f"datasets/{entry['id']}"
        child = root / dataset_path
        audit = audit_dataset(child, minimum_level=plan["minimum_level"])
        if audit.get("integrity") != "passed":
            raise DatasetError(f"{entry['id']}: candidate integrity failed: {audit.get('errors', [])}")
        snapshot = load_dataset_snapshot(child, allow_incomplete=True)
        expected = {"type": "recipe", "recipe": entry["recipe"]}
        if (snapshot.metadata["source"] != expected or snapshot.metadata["count"] != entry["candidate_count"]
                or snapshot.metadata["seed_start"] != entry["seed_start"]
                or snapshot.metadata.get("export_usd") or snapshot.metadata.get("export_mjcf")):
            raise DatasetError(f"{entry['id']}: candidate metadata does not match sampling plan")
        records = {record["scene_id"]: record for record in snapshot.records}
        rejected: Counter[str] = Counter()
        local_content: set[str] = set()
        chosen: list[dict[str, Any]] = []
        positions: list[list[float]] = []
        matched_count = 0
        unique_count = 0
        for candidate in audit["scenes"]:
            if not candidate["eligible"]:
                rejected.update(candidate["rejection_codes"])
                continue
            record = records[candidate["scene_id"]]
            layout = _read_json(child / record["files"]["layout"])
            position = None
            if entry["filter"] is not None:
                region_filter = entry["filter"]
                subject = next((item for item in layout["objects"] if item["object_id"] == region_filter["object_id"]), None)
                if subject is None:
                    raise DatasetError(f"{entry['id']}: filter object is missing from candidate")
                position = subject["pose"]["position"]
                xmin, xmax, ymin, ymax = region_filter["region_xy"]
                if not (xmin <= position[0] < xmax and ymin <= position[1] < ymax):
                    rejected["outside_sampling_region"] += 1
                    continue
            matched_count += 1
            fingerprint = candidate["content_fingerprint"]
            if fingerprint in local_content:
                rejected["duplicate_layout"] += 1
                continue
            local_content.add(fingerprint)
            if fingerprint in seen_content:
                rejected["duplicate_across_strata"] += 1
                continue
            unique_count += 1
            if len(chosen) >= entry["quota"]:
                rejected["quota_overflow"] += 1
                continue
            seen_content.add(fingerprint)
            chosen.append({"stratum_id": entry["id"], "dataset_path": dataset_path,
                           "content_fingerprint": fingerprint, "record": record})
            if position is not None:
                positions.append(position)
            recipe_counts[record["recipe"]] += 1
            room_counts[layout["room_type"]] += 1
            event_counts[layout["event"]] += 1
            asset_counts.update(item["asset_id"] for item in layout["objects"])
        met = len(chosen) == entry["quota"]
        if not met:
            errors.append(f"{entry['id']}: quota={entry['quota']} available={len(chosen)}")
        selected.extend(chosen)
        summaries.append({
            "id": entry["id"], "recipe": entry["recipe"], "dataset_path": dataset_path,
            "dataset_id": snapshot.metadata["dataset_id"], "manifest_sha256": audit["manifest_sha256"],
            "candidate_count": entry["candidate_count"], "quota": entry["quota"],
            "eligible_count": audit["summary"]["eligible_count"], "matched_count": matched_count,
            "unique_count": unique_count, "available_count": len(chosen), "quota_met": met,
            "selected_count": len(chosen), "rejection_counts": dict(sorted(rejected.items())),
            "position_bounds_xy": [min(position[0] for position in positions), max(position[0] for position in positions),
                                   min(position[1] for position in positions), max(position[1] for position in positions)] if positions else None,
        })
    qualified_count = len(selected)
    if errors:
        selected = []
        for summary in summaries:
            summary["selected_count"] = 0
        recipe_counts.clear()
        room_counts.clear()
        event_counts.clear()
        asset_counts.clear()
    return DatasetResult("failed" if errors else "passed", not errors, {
        **_base_report(plan, sources), "status": "incomplete" if errors else "complete",
        "summary": {"candidate_count": sum(entry["candidate_count"] for entry in plan["strata"]),
                    "quota": sum(entry["quota"] for entry in plan["strata"]),
                    "qualified_count": qualified_count, "selected_count": len(selected)},
        "strata": summaries, "coverage": {
            "recipe_counts": dict(sorted(recipe_counts.items())), "room_type_counts": dict(sorted(room_counts.items())),
            "event_counts": dict(sorted(event_counts.items())), "asset_instance_counts": dict(sorted(asset_counts.items())),
        }, "selected": selected, "errors": errors,
    })


def validate_sampling_collection(path: str | Path) -> DatasetResult:
    try:
        root = _root_path(path)
        plan = load_sampling_plan(root / "sampling_plan.json")
        _check_root(root, plan)
        stored = _read_state(root, plan)
        expected = _evaluate_collection(root, plan, stored["sources"])
        if stored != expected.to_dict():
            raise DatasetError("stored collection does not match candidate evidence and quotas")
        return expected
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        return DatasetResult("failed", False, {"reason": "collection_invalid", "selected": [], "errors": [str(exc)]})


def build_sampling_collection(
    plan: Mapping[str, Any], output: str | Path, *, resume: bool = False, factory: Any = None
) -> DatasetResult:
    plan = normalize_sampling_plan(plan)
    if not isinstance(resume, bool):
        raise DatasetError("resume must be boolean")
    if factory is None:
        from .factory import SceneFactory

        factory = SceneFactory()
    sources = _source_fingerprints(factory, plan)
    root = _root_path(output)
    if resume:
        _check_root(root, plan)
        if load_sampling_plan(root / "sampling_plan.json") != plan:
            raise DatasetError("resume plan does not match existing collection")
        stored = _read_state(root, plan)
        if stored["sources"] != sources:
            raise DatasetError("resume recipe or registry source fingerprints changed")
        if stored["status"] == "complete" and not validate_sampling_collection(root).valid:
            raise DatasetError("cannot resume a corrupt completed collection")
        for entry in plan["strata"]:
            child = root / "datasets" / entry["id"]
            if child.exists():
                snapshot = load_dataset_snapshot(child, allow_incomplete=True)
                if (snapshot.metadata["source"] != {"type": "recipe", "recipe": entry["recipe"]}
                        or snapshot.metadata["count"] != entry["candidate_count"]
                        or snapshot.metadata["seed_start"] != entry["seed_start"]
                        or snapshot.metadata.get("export_usd") or snapshot.metadata.get("export_mjcf")):
                    raise DatasetError("resume candidates do not match sampling plan")
    else:
        if root.exists() and (not root.is_dir() or any(root.iterdir())):
            raise DatasetError("collection output is not empty; use --resume")
        root.mkdir(parents=True, exist_ok=True)
        write_json_atomic(root / "sampling_plan.json", plan)
    in_progress = {**_base_report(plan, sources), "status": "in_progress", "result": "incomplete", "valid": False}
    write_json_atomic(root / "collection.json", in_progress)
    try:
        for entry in plan["strata"]:
            child = root / "datasets" / entry["id"]
            if child.exists() and load_dataset_snapshot(child, allow_incomplete=True).metadata["status"] == "complete":
                continue
            factory.build_batch(child, count=entry["candidate_count"], seed_start=entry["seed_start"],
                                recipe_name=entry["recipe"], resume=child.exists())
        report = _evaluate_collection(root, plan, sources)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        report = DatasetResult("failed", False, {
            **_base_report(plan, sources), "status": "incomplete",
            "reason": "candidate_generation_failed", "errors": [str(exc)],
        })
    write_json_atomic(root / "collection.json", report.to_dict())
    return report


def reproduce_sampling_collection(path: str | Path) -> DatasetResult:
    validation = validate_sampling_collection(path)
    if not validation.valid:
        return validation
    root = _root_path(path)
    results = [{"id": entry["id"], **reproduce_dataset(root / entry["dataset_path"]).to_dict()}
               for entry in validation["strata"]]
    valid = all(result["valid"] for result in results)
    return DatasetResult("passed" if valid else "failed", valid, {"strata": results})
