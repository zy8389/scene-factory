from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from .dataset import (
    DatasetError,
    DatasetResult,
    SCENE_SCHEMA_VERSION,
    _FILE_NAMES,
    _OPTIONAL_FILE_NAMES,
    _REQUIRED_FILES,
    _resolve_manifest_file,
    _scene_id_is_safe,
    canonical_json,
    semantic_fingerprint,
    sha256_file,
    write_json_atomic,
)
from .models import CompiledScene
from .quality import assess_scene_quality
from .sampling import _root_path, validate_sampling_collection


CONFIGURATION_GROUP_VERSION = "scene_factory.configuration_group.v1"
SAMPLING_SPLIT_SCHEMA_VERSION = "scene_factory.sampling_split.v1"
CURATED_DATASET_SCHEMA_VERSION = "scene_factory.curated_dataset.v1"
SPLIT_NAMES = ("train", "validation", "test")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_STRATUM = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def _hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def configuration_fingerprint(layout: Mapping[str, Any]) -> str:
    scene = CompiledScene.from_dict(dict(layout))
    objects = [{
        "asset_id": item.asset_id, "bbox_m": item.bbox_m,
        "position": [0.0 if value == 0 else value for value in item.pose.position],
        "yaw_deg": item.pose.yaw_deg % 360.0,
        "dynamic": item.dynamic, "interactions": item.interactions,
    } for item in scene.objects]
    return _hash({"version": CONFIGURATION_GROUP_VERSION, "room_type": scene.room_type,
                  "room_dimensions_m": scene.room_dimensions_m,
                  "objects": sorted(objects, key=canonical_json)})


def _split_options(ratios: Any, seed: int) -> tuple[list[float], int]:
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise DatasetError("split seed must be an integer")
    if not isinstance(ratios, (list, tuple)) or len(ratios) != 3:
        raise DatasetError("split ratios must contain train, validation and test")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in ratios):
        raise DatasetError("split ratios must be numbers")
    try:
        values = [float(value) for value in ratios]
    except OverflowError as exc:
        raise DatasetError("split ratios must be finite") from exc
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise DatasetError("split ratios must be finite and non-negative")
    total = sum(values)
    if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise DatasetError("split ratios must sum to one")
    return values, seed


def _group_targets(count: int, ratios: list[float]) -> list[int]:
    active = [index for index, ratio in enumerate(ratios) if ratio > 0]
    if count < len(active):
        raise DatasetError("too few configuration groups for non-empty requested splits")
    total = sum(ratios)
    exact = [count * ratio / total for ratio in ratios]
    targets = [math.floor(value) for value in exact]
    ranking = sorted(active, key=lambda index: (-(exact[index] - targets[index]), index))
    for index in ranking[:count - sum(targets)]:
        targets[index] += 1
    for index in active:
        if targets[index] == 0:
            donor = max((candidate for candidate in active if targets[candidate] > 1),
                        key=lambda candidate: (targets[candidate] - exact[candidate], targets[candidate]))
            targets[donor] -= 1
            targets[index] += 1
    return targets


def _assign_groups(items: list[dict[str, Any]], ratios: list[float], seed: int) -> tuple[dict[str, str], dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        groups.setdefault(item["configuration_fingerprint"], []).append(item)
    targets = _group_targets(len(groups), ratios)
    weights = [value / sum(ratios) for value in ratios]
    assigned_groups = [0, 0, 0]
    totals = Counter(item["stratum_id"] for item in items)
    assigned_strata = [Counter(), Counter(), Counter()]
    mapping: dict[str, str] = {}
    order = sorted(groups, key=lambda fingerprint: _hash([seed, fingerprint]))
    for fingerprint in order:
        members = Counter(item["stratum_id"] for item in groups[fingerprint])
        candidates = [index for index in range(3) if assigned_groups[index] < targets[index]]

        def score(index: int) -> tuple[float, float, str]:
            deficit = sum(
                quantity * (totals[stratum] * weights[index] - assigned_strata[index][stratum]) / totals[stratum]
                for stratum, quantity in members.items()
            )
            return (deficit, (targets[index] - assigned_groups[index]) / targets[index],
                    _hash([seed, fingerprint, SPLIT_NAMES[index]]))

        chosen = max(candidates, key=score)
        mapping[fingerprint] = SPLIT_NAMES[chosen]
        assigned_groups[chosen] += 1
        assigned_strata[chosen].update(members)
    scene_counts = Counter(mapping[item["configuration_fingerprint"]] for item in items)
    strata_counts = {
        stratum: {name: assigned_strata[index][stratum] for index, name in enumerate(SPLIT_NAMES)}
        for stratum in sorted(totals)
    }
    return mapping, {
        "scene_count": len(items), "configuration_group_count": len(groups),
        "group_counts": dict(zip(SPLIT_NAMES, assigned_groups)),
        "scene_counts": {name: scene_counts[name] for name in SPLIT_NAMES},
        "stratum_counts": strata_counts,
    }


def _collection_items(root: Path, selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for entry in selected:
        _, layout_path = _resolve_manifest_file(root, f"{entry['dataset_path']}/{entry['record']['files']['layout']}")
        items.append({"stratum_id": entry["stratum_id"], "scene_id": entry["record"]["scene_id"],
                      "fingerprint": entry["record"]["fingerprint"],
                      "configuration_fingerprint": configuration_fingerprint(_read_json(layout_path))})
    return items


def split_sampling_collection(
    path: str | Path, *, ratios: tuple[float, float, float] = (0.7, 0.15, 0.15), seed: int = 0
) -> DatasetResult:
    values, seed = _split_options(ratios, seed)
    validation = validate_sampling_collection(path)
    if not validation.valid:
        return DatasetResult("failed", False, {"reason": "collection_invalid", "assignments": [], "errors": validation.get("errors", [])})
    try:
        root = _root_path(path)
        source_hash = sha256_file(root / "collection.json")
        items = _collection_items(root, validation["selected"])
        mapping, summary = _assign_groups(items, values, seed)
        assignments = [{**item, "split": mapping[item["configuration_fingerprint"]]} for item in items]
        return DatasetResult("passed", True, {
            "schema_version": SAMPLING_SPLIT_SCHEMA_VERSION, "grouping_version": CONFIGURATION_GROUP_VERSION,
            "source_collection_sha256": source_hash, "ratios": values, "seed": seed,
            "evidence_scope": "offline_recorded_layout_only", "summary": summary,
            "assignments": assignments, "errors": [],
        })
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        return DatasetResult("failed", False, {"reason": "split_unavailable", "assignments": [], "errors": [str(exc)]})


def validate_sampling_split(path: str | Path, report: Mapping[str, Any]) -> DatasetResult:
    try:
        if not isinstance(report, Mapping) or report.get("schema_version") != SAMPLING_SPLIT_SCHEMA_VERSION:
            raise DatasetError("unsupported split schema_version")
        expected = split_sampling_collection(path, ratios=report["ratios"], seed=report["seed"])
        if not expected.valid or expected.to_dict() != dict(report):
            raise DatasetError("split does not match source collection and configuration groups")
        return expected
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        return DatasetResult("failed", False, {"reason": "split_invalid", "assignments": [], "errors": [str(exc)]})


def _artifact_descriptors(record: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    files, hashes = record["files"], record["sha256"]
    allowed = set(_REQUIRED_FILES) | set(_OPTIONAL_FILE_NAMES)
    if (not isinstance(files, dict) or not isinstance(hashes, dict) or set(files) != set(hashes)
            or not set(_REQUIRED_FILES).issubset(files) or set(files) - allowed):
        raise DatasetError("curated record artifact map is invalid")
    paths, expected = dict(files), dict(hashes)
    if "quality_report" in record:
        descriptor = record["quality_report"]
        paths["quality"], expected["quality"] = descriptor["path"], descriptor["sha256"]
    if any(not isinstance(value, str) or not _HASH.fullmatch(value) for value in expected.values()):
        raise DatasetError("curated artifact hash is invalid")
    return paths, expected


def _export_summary(entries: list[dict[str, Any]], ratios: list[float], seed: int) -> dict[str, Any]:
    mapping, summary = _assign_groups(entries, ratios, seed)
    if any(entry["split"] != mapping[entry["configuration_fingerprint"]] for entry in entries):
        raise DatasetError("curated split assignments violate configuration grouping or seed")
    return summary


def _preview_html(manifest: Mapping[str, Any]) -> bytes:
    summary = manifest["summary"]
    split_labels = {"train": "训练候选", "validation": "验证候选", "test": "测试候选"}
    metrics = "".join(f'<div class="metric"><strong>{summary["scene_counts"][name]}</strong><span>{label}</span></div>'
                      for name, label in split_labels.items())
    rows = "".join(
        f'<tr><th>{html.escape(stratum)}</th>'
        + "".join(f'<td>{counts[name]}</td>' for name in SPLIT_NAMES) + '</tr>'
        for stratum, counts in summary["stratum_counts"].items()
    )
    cards = []
    for entry in manifest["entries"]:
        record = entry["record"]
        preview = html.escape(quote(record["files"]["preview"], safe="/"), quote=True)
        layout = html.escape(quote(record["files"]["layout"], safe="/"), quote=True)
        label = html.escape(f'{entry["stratum_id"]} · Seed {record["seed"]}')
        quality = record.get("quality_report")
        quality_link = (f'<a href="{html.escape(quote(quality["path"], safe="/"), quote=True)}">质量报告</a>'
                        if quality else '')
        cards.append(f'<article><a href="{preview}"><img src="{preview}" loading="lazy" alt="{label}"></a>'
                     f'<div class="card-body"><span class="tag {entry["split"]}">{split_labels[entry["split"]]}</span>'
                     f'<h3>{label}</h3><p>{html.escape(record["recipe"])}</p>'
                     f'<small>配置组 {entry["configuration_fingerprint"][:12]}</small>'
                     f'<nav><a href="{layout}">查看布局</a>{quality_link}</nav></div></article>')
    document = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>场景候选数据包</title><style>
:root{{font-family:"Segoe UI","Microsoft YaHei",sans-serif;color:#17263c;background:#f3f6fa}}
body{{margin:0}}main{{max-width:1200px;margin:auto;padding:36px 24px 60px}}h1{{font-size:30px;margin:12px 0}}
.eyebrow{{color:#4267a0;font-size:12px;font-weight:700;letter-spacing:2px}}.intro{{color:#576579;line-height:1.8}}
.notice{{padding:14px 18px;border-radius:12px;background:#fff1d8;color:#79551c;line-height:1.7}}
.metrics{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:24px 0}}.metric{{background:white;padding:20px;border-radius:14px}}
.metric strong{{font-size:32px;display:block}}.metric span{{color:#627187}}table{{width:100%;background:white;border-collapse:collapse;border-radius:14px;overflow:hidden}}
th,td{{text-align:left;padding:13px 16px;border-bottom:1px solid #e8edf4}}h2{{font-size:20px;margin-top:30px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:18px}}article{{background:white;border:1px solid #e1e7f0;border-radius:14px;overflow:hidden}}
img{{display:block;width:100%;height:210px;object-fit:contain;background:#f9fbfe}}.card-body{{padding:16px}}h3{{font-size:15px;margin:12px 0 8px}}
p,small{{overflow-wrap:anywhere}}.card-body p,small{{color:#627187;font-size:12px}}.tag{{display:inline-block;padding:5px 9px;border-radius:6px;font-size:11px;font-weight:700}}
.train{{background:#e8efff;color:#3159a7}}.validation{{background:#e5f5ed;color:#237449}}.test{{background:#f0eafd;color:#7646a2}}
nav{{display:flex;gap:18px;margin-top:15px}}a{{color:#315fa5;text-decoration:none}}a:hover{{text-decoration:underline}}a:focus-visible{{outline:2px solid #315fa5;outline-offset:3px}}
@media(max-width:600px){{main{{padding:24px 14px}}h1{{font-size:25px}}.metrics{{gap:8px}}.metric{{padding:14px}}.metric strong{{font-size:25px}}}}
</style></head><body><main><div class="eyebrow">SCENEFACTORY · 数据候选</div><h1>场景候选数据包</h1>
<p class="intro">{summary["scene_count"]} 个场景 · {summary["configuration_group_count"]} 个配置组。相同配置的不同任务样本一起划分，降低评测泄漏风险。</p>
<div class="notice">布局检查已通过。物理稳定性、机器人可达性和任务成功尚未验证。此处的划分用于候选集管理，训练就绪状态仍需后续验收。</div>
<section class="metrics">{metrics}</section><h2>各采样组的实际划分</h2><table><thead><tr><th>采样组</th><th>训练候选</th><th>验证候选</th><th>测试候选</th></tr></thead><tbody>{rows}</tbody></table>
<h2>场景预览</h2><section class="grid">{''.join(cards)}</section></main></body></html>'''
    return document.encode("utf-8")


def _check_export_tree(root: Path, expected_files: set[str]) -> None:
    directories = {"."}
    for name in expected_files:
        parent = Path(name).parent
        while parent != Path("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    pending = [root]
    while pending:
        directory = pending.pop()
        for item in directory.iterdir():
            relative = item.relative_to(root).as_posix()
            if item.is_symlink() or not item.resolve().is_relative_to(root):
                raise DatasetError("curated package contains a redirected path")
            if item.is_dir() and relative in directories:
                pending.append(item)
            elif not item.is_file() or relative not in expected_files:
                raise DatasetError(f"untracked curated package entry: {relative}")


def validate_curated_dataset(path: str | Path) -> DatasetResult:
    try:
        root = _root_path(path)
        _, manifest_path = _resolve_manifest_file(root, "manifest.json")
        manifest = _read_json(manifest_path)
        if (manifest.get("schema_version") != CURATED_DATASET_SCHEMA_VERSION
                or manifest.get("grouping_version") != CONFIGURATION_GROUP_VERSION
                or manifest.get("evidence_scope") != "offline_recorded_layout_only"):
            raise DatasetError("unsupported curated dataset contract or evidence scope")
        if any(not isinstance(manifest.get(field), str) or not _HASH.fullmatch(manifest[field])
               for field in ("source_collection_sha256", "source_plan_sha256", "index_sha256")):
            raise DatasetError("curated provenance hashes are invalid")
        ratios, seed = _split_options(manifest["ratios"], manifest["seed"])
        entries = manifest["entries"]
        if not isinstance(entries, list) or not entries:
            raise DatasetError("curated package requires selected entries")
        expected_files = {"manifest.json", "index.html"}
        seen: set[tuple[str, str]] = set()
        for entry in entries:
            stratum, record = entry["stratum_id"], entry["record"]
            scene_id = record["scene_id"]
            if (not isinstance(stratum, str) or not _STRATUM.fullmatch(stratum)
                    or record["schema_version"] != SCENE_SCHEMA_VERSION or record["valid"] is not True
                    or isinstance(record["seed"], bool) or not isinstance(record["seed"], int)
                    or not _scene_id_is_safe(scene_id) or entry["split"] not in SPLIT_NAMES
                    or not isinstance(record["recipe"], str) or not record["recipe"]):
                raise DatasetError("curated record identity or quality is invalid")
            identity = (stratum.lower(), scene_id.lower())
            if identity in seen:
                raise DatasetError("duplicate curated record")
            seen.add(identity)
            names, hashes = _artifact_descriptors(record)
            paths = {}
            for name, relative in names.items():
                if relative != f"scenes/{stratum}/{scene_id}/{_FILE_NAMES[name]}":
                    raise DatasetError("curated artifact path is not canonical")
                _, paths[name] = _resolve_manifest_file(root, relative)
                if sha256_file(paths[name]) != hashes[name]:
                    raise DatasetError("curated artifact hash mismatch")
                expected_files.add(relative)
            layout, spec = _read_json(paths["layout"]), _read_json(paths["scene_spec"])
            if (layout["scene_id"] != scene_id or layout["seed"] != record["seed"]
                    or layout["recipe_name"] != record["recipe"]
                    or spec["scene_id"] != scene_id or spec["seed"] != record["seed"]):
                raise DatasetError("curated artifact identity mismatch")
            quality = assess_scene_quality(layout, _read_json(paths["validation"]))
            if quality["layers"]["layout"]["status"] != "passed":
                raise DatasetError("curated layout quality failed")
            if "quality" in paths and _read_json(paths["quality"]) != quality:
                raise DatasetError("curated quality report does not match evidence")
            if semantic_fingerprint(paths) != record["fingerprint"]:
                raise DatasetError("curated semantic fingerprint mismatch")
            if configuration_fingerprint(layout) != entry["configuration_fingerprint"]:
                raise DatasetError("curated configuration group mismatch")
        summary = _export_summary(entries, ratios, seed)
        if summary != manifest["summary"]:
            raise DatasetError("curated summary does not match entries")
        _, index_path = _resolve_manifest_file(root, "index.html")
        if sha256_file(index_path) != manifest["index_sha256"]:
            raise DatasetError("curated preview hash mismatch")
        _check_export_tree(root, expected_files)
        return DatasetResult("passed", True, {"summary": summary, "evidence_scope": "offline_recorded_layout_only", "errors": []})
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        return DatasetResult("failed", False, {"reason": "curated_dataset_invalid", "errors": [str(exc)]})


def export_sampling_selection(
    path: str | Path, output: str | Path, *, ratios: tuple[float, float, float] = (0.7, 0.15, 0.15), seed: int = 0
) -> DatasetResult:
    split = split_sampling_collection(path, ratios=ratios, seed=seed)
    if not split.valid:
        return split
    root, target = _root_path(path), _root_path(output)
    if target.is_relative_to(root) or target.exists():
        raise DatasetError("export output must be new and outside the source collection")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent)).resolve()
    try:
        collection = _read_json(root / "collection.json")
        assignments = {(entry["stratum_id"], entry["scene_id"]): entry for entry in split["assignments"]}
        exported = []
        for selected in collection["selected"]:
            record = selected["record"]
            stratum = selected["stratum_id"]
            names, hashes = _artifact_descriptors(record)
            copied = json.loads(json.dumps(record))
            copied_files = {}
            for name, source_relative in names.items():
                _, source = _resolve_manifest_file(root, f"{selected['dataset_path']}/{source_relative}")
                content = source.read_bytes()
                if hashlib.sha256(content).hexdigest() != hashes[name]:
                    raise DatasetError("source artifact changed before export")
                relative = f"scenes/{stratum}/{record['scene_id']}/{_FILE_NAMES[name]}"
                destination = stage / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
                copied_files[name] = relative
            copied["files"] = {name: relative for name, relative in copied_files.items() if name != "quality"}
            if "quality" in copied_files:
                copied["quality_report"]["path"] = copied_files["quality"]
            assignment = assignments[(stratum, record["scene_id"])]
            exported.append({"stratum_id": stratum, "split": assignment["split"],
                             "configuration_fingerprint": assignment["configuration_fingerprint"], "record": copied})
        if sha256_file(root / "collection.json") != split["source_collection_sha256"]:
            raise DatasetError("source selection changed during export")
        manifest = {
            "schema_version": CURATED_DATASET_SCHEMA_VERSION, "grouping_version": CONFIGURATION_GROUP_VERSION,
            "source_collection_sha256": split["source_collection_sha256"],
            "source_plan_sha256": collection["plan_sha256"], "ratios": split["ratios"], "seed": seed,
            "evidence_scope": "offline_recorded_layout_only", "summary": split["summary"], "entries": exported,
        }
        preview = _preview_html(manifest)
        (stage / "index.html").write_bytes(preview)
        manifest["index_sha256"] = hashlib.sha256(preview).hexdigest()
        write_json_atomic(stage / "manifest.json", manifest)
        verification = validate_curated_dataset(stage)
        if not verification.valid:
            raise DatasetError("; ".join(verification["errors"]))
        if target.exists() or target.is_symlink():
            raise DatasetError("export output appeared during staging")
        os.rename(stage, target)
        return DatasetResult("passed", True, {"output": str(target), "summary": split["summary"],
                                               "preview": str(target / "index.html"), "errors": []})
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        return DatasetResult("failed", False, {"reason": "curated_export_failed", "staging_path": str(stage), "errors": [str(exc)]})
