"""Compile a recorded kitchen layout into source-bound Franka task copies.

This module performs conservative geometric screening only. Robot reachability,
settling, contact, and success remain the responsibility of the existing Isaac
workers and independent evidence validator. No simulator is imported here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Mapping

from .dataset import (
    DatasetError,
    canonical_json,
    make_dataset_metadata,
    make_manifest_record,
    sha256_file,
    update_dataset_metadata,
    write_json_atomic,
    write_manifest_atomic,
)
from .geometry import inverse_rotate_xy, objects_overlap, rotate_xy, rotated_half_extents_xy
from .models import CompiledScene, PlacedObject, Pose, SceneRecipe
from .sampling import (
    SAMPLING_PLAN_SCHEMA_VERSION,
    _evaluate_collection,
    normalize_sampling_plan,
    validate_sampling_collection,
)


SCENE_RUNTIME_SCHEMA_VERSION = "scene_factory.scene_runtime.v1"
SCENE_RUNTIME_SOURCE_SCHEMA_VERSION = "scene_factory.scene_runtime_source.v1"
_MOUNT_ASSET = "side_cabinet_basic"
_TRACKING_CONTROLLER = {"arm_stiffness": 2000.0, "grasp_hold_closing_margin_m": 0.012}


def _hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _source(source: Any) -> tuple[dict[str, Any], str, str | None]:
    if isinstance(source, CompiledScene):
        raw = json.loads(canonical_json(source.to_dict()))
        return raw, _hash(raw), None
    if isinstance(source, Mapping):
        raw = json.loads(canonical_json(source))
        return raw, _hash(raw), None
    root = Path(source).expanduser().absolute()
    path = root / "layout.json" if root.is_dir() else root
    if path.name != "layout.json" or path.is_symlink() or not path.is_file():
        raise DatasetError("场景布局文件缺失或不可用，请重新生成场景。")
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    intent = path.parent / "scene_intent.json"
    if intent.is_symlink():
        raise DatasetError("场景意图文件不可使用符号链接，请重新生成场景。")
    return raw, sha256_file(path), sha256_file(intent) if intent.is_file() else None


def _box(object_id: str, bbox: tuple[float, float, float], position: tuple[float, float, float]) -> PlacedObject:
    return PlacedObject(object_id, "geometric_screening_only", "screening", bbox,
                        Pose(position, 0.0), False, None)


def _within_room(item: PlacedObject, room: tuple[float, float, float]) -> bool:
    hx, hy = rotated_half_extents_xy(item.bbox_m, item.pose.yaw_deg)
    x, y, z = item.pose.position
    return (abs(x) + hx <= room[0] / 2.0 and abs(y) + hy <= room[1] / 2.0
            and z - item.bbox_m[2] / 2.0 >= -1e-9
            and z + item.bbox_m[2] / 2.0 <= room[2])


def _yaw_magnitude(item: PlacedObject) -> float:
    return abs((item.pose.yaw_deg + 180.0) % 360.0 - 180.0)


def _zero_yaw(item: PlacedObject) -> bool:
    return _yaw_magnitude(item) <= 1e-7


def inspect_scene_runtime(factory: Any, source: Any) -> dict[str, Any]:
    """Describe eligibility and an explicit plan without changing the scene.

    ``source`` may be a scene directory, its ``layout.json``, a CompiledScene,
    or a layout mapping. Directory inputs bind the actual source file bytes.
    The calibrated first version faces global +X and screens small yaw changes
    on static rectangular support surfaces. These changes still require actual
    physics acceptance; screening does not claim calibrated grasp success.
    """
    response: dict[str, Any] = {
        "schema_version": SCENE_RUNTIME_SCHEMA_VERSION, "eligible": False,
        "reasons": [], "rejection_codes": [], "plan": None,
        "source_scene_id": None, "source_layout_sha256": None, "source_intent_sha256": None,
        "evidence_scope": "geometric_screening_only",
    }

    def reject(code: str, reason: str) -> dict[str, Any]:
        response["rejection_codes"].append(code)
        response["reasons"].append(reason)
        return response

    try:
        raw, layout_hash, intent_hash = _source(source)
        scene = CompiledScene.from_dict(raw)
        response.update(source_scene_id=scene.scene_id, source_layout_sha256=layout_hash,
                        source_intent_sha256=intent_hash)
        if raw.get("objects") != json.loads(canonical_json(scene.to_dict()))["objects"]:
            return reject("noncanonical_layout", "场景布局格式不完整，请使用网页重新生成场景后运行。")
        if (len({item.object_id for item in scene.objects}) != len(scene.objects)
                or any(any(not math.isfinite(value) or value <= 0 for value in item.bbox_m)
                       for item in scene.objects)
                or any(not math.isfinite(value) or value <= 0 for value in scene.room_dimensions_m)):
            return reject("invalid_layout", "场景对象标识或尺寸无效，请重新生成场景。")
        if scene.room_type != "kitchen":
            return reject("unsupported_room", "目前支持厨房单杯任务，请生成厨房场景。")
        mugs = [item for item in scene.objects if item.category == "mug"]
        if len(mugs) != 1:
            return reject("single_mug_required", "场景需要且只能有一个杯子，请保留一个 YCB 杯子。")
        mug = mugs[0]
        if mug.asset_id != "mug_001" or not mug.dynamic or mug.fallback_reason:
            return reject("unsupported_mug", "请使用可动态抓取的 YCB mug_001 杯子，去掉替代模型。")
        if _yaw_magnitude(mug) > 25.0:
            return reject("unsupported_mug_yaw", "杯子旋转角度超过当前范围，请使用朝向在 ±25° 内的杯子。")
        if any(item.dynamic and item.object_id != mug.object_id for item in scene.objects):
            return reject("extra_dynamic_objects", "当前版本只运行一个动态杯子，请去掉其他可移动物体。")
        by_id = {item.object_id: item for item in scene.objects}
        support_id, _, surface_name = (mug.support or "").partition(":")
        support = by_id.get(support_id)
        if support is None or surface_name != "top" or support.dynamic or _yaw_magnitude(support) > 5.0:
            return reject("unsupported_support", "杯子需要放在朝向在 ±5° 内的静态矩形桌面 top 表面上。")
        for item in scene.objects:
            asset = factory.registry.get(item.asset_id)
            if (asset.category != item.category or tuple(asset.bbox_m) != item.bbox_m
                    or asset.status not in {"ready", "validated"} or not asset.collision_enabled
                    or (item.dynamic and not asset.rigid_body) or item.interactions):
                return reject("incompatible_asset", f"对象 {item.object_id} 的资产或物理参数不受支持，请使用已验证资产。")
            if item.object_id == mug.object_id:
                paths = (factory.registry.resolve_source_path(asset), factory.registry.resolve_collision_path(asset))
                if (asset.source_type != "local_usd" or asset.collision_status != "validated"
                        or any(not path or "://" in path or not Path(path).is_file() for path in paths)):
                    return reject("missing_mug_assets", "YCB 杯子的本地模型或已验证碰撞文件缺失，请先准备杯子资产。")
            elif asset.source_type != "primitive" or asset.primitive != "cube":
                return reject("unsupported_asset", f"当前版本不支持对象 {item.object_id} 的模型，请使用简单静态矩形家具。")
        support_asset = factory.registry.get(support.asset_id)
        surface = next((entry for entry in support_asset.support_surfaces if entry.name == "top"), None)
        if surface is None:
            return reject("missing_support_surface", "支撑物缺少已知 top 表面，请改用已验证的厨房岛台。")
        validation = factory.validator.validate(scene)
        if not validation.valid:
            return reject("invalid_layout", "场景布局存在碰撞、悬空或越界，请先修正布局后运行。")
        top_z = support.pose.position[2] + surface.center[2]
        if not 0.86 <= top_z <= 1.00:
            return reject("unsupported_table_height", "当前机械臂底座支持 0.86–1.00 米高桌面，请使用该高度范围的静态岛台。")
        rotated_center = rotate_xy(surface.center[:2], support.pose.yaw_deg)
        surface_x = support.pose.position[0] + rotated_center[0]
        surface_y = support.pose.position[1] + rotated_center[1]
        left_edge = support.pose.position[0] - rotated_half_extents_xy(support.bbox_m, support.pose.yaw_deg)[0]
        mx, my, mz = mug.pose.position
        mount_asset = factory.registry.get(_MOUNT_ASSET)
        if (mount_asset.source_type != "primitive" or mount_asset.primitive != "cube"
                or mount_asset.status not in {"ready", "validated"} or not mount_asset.collision_enabled
                or tuple(mount_asset.bbox_m) != (0.9, 0.42, 0.78)):
            return reject("unsupported_mount_asset", "机械臂安装台资产不可用，请恢复已验证的 side_cabinet_basic。")
        mounts = sorted((item for item in scene.objects if item.asset_id == _MOUNT_ASSET
                         and not item.dynamic and _zero_yaw(item)
                         and abs(item.pose.position[2] - 0.39) <= 1e-7
                         and item.pose.position[0] + 0.45 <= left_edge - 0.03
                         and abs(item.pose.position[1] - my) <= 0.12),
                        key=lambda item: (math.dist(item.pose.position[:2], mug.pose.position[:2]), item.object_id))
        added: list[PlacedObject] = []
        if mounts:
            mount = mounts[0]
        else:
            identifier = "runtime_robot_mount"
            while identifier in by_id:
                identifier += "_1"
            mount = PlacedObject(identifier, _MOUNT_ASSET, mount_asset.category, mount_asset.bbox_m,
                                 Pose((left_edge - 0.05 - 0.45, my, 0.39), 0.0), False, None)
            added = [mount]
        base = (mount.pose.position[0], mount.pose.position[1], 0.78)
        if not 0.52 <= mx - base[0] <= 0.75 or abs(my - base[1]) > 0.12:
            return reject("mug_out_of_reach", "杯子距桌面左边缘过远；请生成杯子中心在左边缘内侧约 5–25 厘米的布局。")
        if not _within_room(mount, scene.room_dimensions_m):
            return reject("mount_out_of_room", "桌面左侧没有足够安装空间，请给左侧安装台留出至少 1 米空间。")
        if added and any(objects_overlap(mount, item) for item in scene.objects):
            return reject("mount_obstructed", "桌面左侧安装台位置被家具占用，请清空桌面左侧安装区域。")
        robot_envelope = _box("robot_base_clearance", (0.32, 0.38, 1.02),
                              (base[0], base[1], base[2] + 0.51))
        if not _within_room(robot_envelope, scene.room_dimensions_m):
            return reject("robot_out_of_room", "机械臂安装位置越过房间边界，请增大房间或把桌面放在房间内部。")
        if any(objects_overlap(robot_envelope, item) for item in scene.objects
               if item.object_id != mount.object_id):
            return reject("robot_base_obstructed", "机械臂底座上方空间被其他对象占用，请清空桌面左侧上方。")
        obstacles = [item for item in scene.objects
                     if item.object_id not in {mug.object_id, support.object_id, mount.object_id}]
        # The robot approaches from negative world X. This is a conservative
        # swept box for the calibrated grasp/lift/transfer corridor, not an IK test.
        target = None
        footprint_x, footprint_y = rotated_half_extents_xy(mug.bbox_m, mug.pose.yaw_deg - support.pose.yaw_deg)
        for dx, dy in ((0.07, 0.17), (0.07, -0.17), (0.0, 0.20), (0.0, -0.20)):
            candidate = (mx + dx, my + dy, top_z + mug.bbox_m[2] / 2.0)
            local_candidate = inverse_rotate_xy((candidate[0] - surface_x, candidate[1] - surface_y), support.pose.yaw_deg)
            if (abs(local_candidate[0]) + footprint_x + 0.025 > surface.size[0] / 2.0
                    or abs(local_candidate[1]) + footprint_y + 0.025 > surface.size[1] / 2.0
                    or math.hypot(candidate[0] - base[0], candidate[1] - base[1]) > 0.79):
                continue
            lower_y, upper_y = min(my, candidate[1]) - 0.16, max(my, candidate[1]) + 0.16
            corridor = _box("arm_corridor", (candidate[0] - base[0] + 0.22, upper_y - lower_y, 0.50),
                            ((base[0] + candidate[0]) / 2.0 + 0.05,
                             (lower_y + upper_y) / 2.0, top_z + 0.25))
            if any(objects_overlap(corridor, item) for item in obstacles):
                continue
            target = candidate
            break
        if target is None:
            return reject("no_clear_placement", "杯子周围缺少可达的空放置区，请给杯子侧面留出至少 20 厘米空间。")
        # Existing fixtures and explicit additions are checked identically.
        compiled = replace(scene, objects=scene.objects + tuple(added))
        if not factory.validator.validate(compiled).valid:
            return reject("mount_layout_invalid", "安装台与当前布局冲突，请扩大桌面左侧空地。")
        response.update(eligible=True, plan={
            "target_object": mug.object_id, "asset_id": mug.asset_id,
            "support_object": support.object_id, "support_surface": "top",
            "support_top_z_m": top_z, "robot": "franka",
            "robot_base_position_m": list(base), "robot_base_orientation_wxyz": [1.0, 0.0, 0.0, 0.0],
            "mount_object": mount.object_id, "added_objects": [asdict(item) for item in added],
            "target_position_m": list(target),
            "target_region_xy": [target[0] - 0.08, target[0] + 0.08, target[1] - 0.09, target[1] + 0.09],
            "source_objects_sha256": _hash(raw["objects"]),
            "source_mug_yaw_deg": mug.pose.yaw_deg, "source_support_yaw_deg": support.pose.yaw_deg,
            "task_count": 2, "tasks": ["robot_mug_lift", "robot_mug_pick_place"],
            "screening": "fixed_world_x_calibration_and_conservative_bounding_boxes",
            "notice": "可运行仅代表几何检查通过；真实可达性、接触与任务成败由仿真和独立证据校验判定。",
        })
        return json.loads(canonical_json(response))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        return reject("scene_unavailable", f"场景暂时无法检查，请重新生成场景：{exc}")


def prepare_scene_runtime_collection(factory: Any, source_dir: str | Path, collection_dir: str | Path,
                                     *, profile: str = "tracking") -> dict[str, Any]:
    """Create exactly two frozen task copies accepted by runtime_batch.

    Original object dictionaries and poses are retained. Any mounting fixture is
    an explicit addition present in both layouts and the embedded source plan.
    Source provenance is inside each hashed layout and scene specification.
    """
    if profile not in {"tracking", "baseline"}:
        raise DatasetError("运行配置只能是 tracking 或 baseline。")
    source_path = Path(source_dir).expanduser().absolute()
    output = Path(collection_dir).expanduser().absolute()
    if output.is_symlink() or output.resolve() == source_path.resolve() or source_path.resolve().is_relative_to(output.resolve()):
        raise DatasetError("运行集合必须与原始场景目录分开。")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise DatasetError("运行集合目录已包含文件，不能覆盖现有证据。")
    eligibility = inspect_scene_runtime(factory, source_path)
    if not eligibility["eligible"]:
        raise DatasetError("；".join(eligibility["reasons"]))
    raw, source_hash, intent_hash = _source(source_path)
    if source_hash != eligibility["source_layout_sha256"] or intent_hash != eligibility["source_intent_sha256"]:
        raise DatasetError("源场景在检查后发生变化，请重新提交。")
    source_scene = CompiledScene.from_dict(raw)
    plan = eligibility["plan"]
    provenance = {
        "schema_version": SCENE_RUNTIME_SOURCE_SCHEMA_VERSION,
        "source_scene_id": source_scene.scene_id, "source_layout_sha256": source_hash,
        "source_intent_sha256": intent_hash, "source_objects_sha256": _hash(raw["objects"]),
        "source_layout": raw, "plan": plan, "profile": profile,
    }
    objects = source_scene.objects + tuple(CompiledScene.from_dict({**raw, "objects": plan["added_objects"]}).objects)
    from .factory import BuildResult

    results = []
    for suffix, event in (("lift", "robot_mug_lift"), ("place", "robot_mug_pick_place")):
        recipe_name = f"current_scene_{source_hash[:16]}_{suffix}"
        success = {"predicate": "lifted", "subject": plan["target_object"], "min_height_delta_m": 0.1}
        if suffix == "place":
            success = {
                "predicate": "pick_and_place", "subject": plan["target_object"],
                "source_support": plan["support_object"], "target_support": plan["support_object"],
                "target_support_surface": "top", "target_position_m": plan["target_position_m"],
                "target_region_xy": plan["target_region_xy"], "target_tolerance_m": [0.09, 0.09, 0.03],
                "min_lift_delta_m": 0.1, "lift_height_m": 0.14, "transfer_clearance_m": 0.1,
                "settle_steps": 20, "max_settle_step_distance_m": 0.005,
            }
        task = {
            "instruction": "把当前场景的杯子提起。" if suffix == "lift" else "把当前场景的杯子提起并放到旁边空区域。",
            "target_object": plan["target_object"], "asset_id": "mug_001", "robot": "franka",
            "robot_base_position_m": plan["robot_base_position_m"],
            "robot_base_orientation_wxyz": plan["robot_base_orientation_wxyz"],
            "grasp_offset_m": [0.053, 0.007, 0.0], "grasp_orientation_wxyz": [0.0, 1.0, 0.0, 0.0],
            "success": success, "controller": dict(_TRACKING_CONTROLLER) if profile == "tracking" else {},
            "scene_runtime_source": copy.deepcopy(provenance),
        }
        scene = replace(source_scene, scene_id=f"current-{source_hash[:20]}-{suffix}",
                        recipe_name=recipe_name, event=event, objects=objects, task=task)
        recipe = SceneRecipe.from_dict({
            "name": recipe_name, "room_type": scene.room_type,
            "room_dimensions_m": scene.room_dimensions_m, "event": event,
            "description": scene.description, "keywords": [], "task": task,
            "objects": [{"object_id": item.object_id, "category": item.category,
                         "asset_id": item.asset_id, "dynamic": item.dynamic, "support": item.support,
                         "fixed_pose": asdict(item.pose), "relations": [asdict(entry) for entry in item.relations]}
                        for item in objects],
        })
        results.append(BuildResult(recipe, scene, factory.validator.validate(scene), prompt_parser="frozen_scene_runtime"))
    sampling_plan = normalize_sampling_plan({
        "schema_version": SAMPLING_PLAN_SCHEMA_VERSION, "minimum_level": "layout",
        "strata": [{"id": suffix, "recipe": result.recipe.name, "quota": 1,
                    "candidate_count": 1, "seed_start": source_scene.seed}
                   for suffix, result in zip(("lift", "place"), results)],
    })
    output.mkdir(parents=True, exist_ok=True)
    write_json_atomic(output / "sampling_plan.json", sampling_plan)
    for suffix, result in zip(("lift", "place"), results):
        dataset = output / "datasets" / suffix
        metadata = make_dataset_metadata(recipe_name=result.recipe.name, prompt=None, count=1,
                                         seed_start=source_scene.seed, export_usd=False)
        files = factory.write_result(result, dataset / result.scene.scene_id, export_bundle=False)
        record = make_manifest_record(result, files, dataset, result.scene.scene_id)
        write_manifest_atomic(dataset / "manifest.jsonl", [record])
        write_json_atomic(dataset / "dataset.json", update_dataset_metadata(metadata, [record], status="complete"))
    sources = {
        "registry_sha256": _hash([asdict(item) for item in sorted(factory.registry.list(), key=lambda item: item.asset_id)]),
        "recipe_sha256": {result.recipe.name: _hash(result.recipe.to_dict()) for result in results},
    }
    report = _evaluate_collection(output, sampling_plan, sources)
    write_json_atomic(output / "collection.json", report.to_dict())
    checked = validate_sampling_collection(output)
    if not checked.valid:
        raise DatasetError("冻结任务集合校验失败：" + repr(checked.get("errors", [])))
    if _source(source_path)[1:] != (source_hash, intent_hash):
        raise DatasetError("源场景在冻结过程中发生变化，请重新提交。")
    return {
        "schema_version": SCENE_RUNTIME_SCHEMA_VERSION, "collection_path": str(output),
        "source_scene_id": source_scene.scene_id, "source_layout_sha256": source_hash,
        "source_intent_sha256": intent_hash, "task_count": 2, "eligibility": eligibility,
    }
