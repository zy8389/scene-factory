from __future__ import annotations

import itertools
import json
import os
from pathlib import Path
from typing import Any


class AssetPipelineUnavailable(RuntimeError):
    pass


def _pxr_modules():
    try:
        from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade
    except ImportError as exc:
        raise AssetPipelineUnavailable(
            "Asset preparation requires Isaac Sim's Python environment (pxr is missing)."
        ) from exc
    return Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade


def _local_usd_path(value: str | Path, *, must_exist: bool) -> Path:
    text = str(value)
    if "://" in text:
        raise ValueError("offline asset preparation accepts local USD paths only")
    path = Path(value).expanduser().resolve()
    if os.name == "nt" and not str(path).isascii():
        raise ValueError("use an ASCII-only path for OpenUSD assets on Windows")
    if must_exist and not path.is_file():
        raise FileNotFoundError(path)
    return path


def _json_write(path: str | Path, payload: dict[str, Any]) -> Path:
    output = Path(path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    return output


def _selected_root(stage):
    default_prim = stage.GetDefaultPrim()
    if default_prim and default_prim.IsValid():
        return default_prim, True
    roots = list(stage.GetPseudoRoot().GetChildren())
    return (roots[0], False) if roots else (None, False)


def inspect_usd(source: str | Path) -> dict[str, Any]:
    """Inspect one local USD without changing it."""
    _, _, Usd, UsdGeom, UsdPhysics, UsdShade = _pxr_modules()
    source_path = _local_usd_path(source, must_exist=True)
    stage = Usd.Stage.Open(source_path.as_posix())
    if stage is None:
        raise RuntimeError(f"OpenUSD could not open {source_path}")

    root, has_default_prim = _selected_root(stage)
    if root is None:
        raise ValueError("USD has no root prim")
    purposes = [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy]
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), purposes, useExtentsHint=True)
    aligned = cache.ComputeWorldBound(root).ComputeAlignedRange()
    if aligned.IsEmpty():
        minimum = maximum = size_units = (0.0, 0.0, 0.0)
    else:
        raw_minimum = aligned.GetMin()
        raw_maximum = aligned.GetMax()
        minimum = tuple(float(raw_minimum[index]) for index in range(3))
        maximum = tuple(float(raw_maximum[index]) for index in range(3))
        size_units = tuple(maximum[index] - minimum[index] for index in range(3))

    meters_per_unit = float(UsdGeom.GetStageMetersPerUnit(stage))
    size_m = tuple(value * meters_per_unit for value in size_units)
    counts = {
        "prims": 0,
        "geometry_prims": 0,
        "mesh_prims": 0,
        "material_prims": 0,
        "collision_prims": 0,
        "rigid_body_prims": 0,
    }
    for prim in stage.Traverse():
        counts["prims"] += 1
        if prim.IsA(UsdGeom.Gprim):
            counts["geometry_prims"] += 1
        if prim.IsA(UsdGeom.Mesh):
            counts["mesh_prims"] += 1
        if prim.IsA(UsdShade.Material):
            counts["material_prims"] += 1
        if prim.HasAPI(UsdPhysics.CollisionAPI):
            counts["collision_prims"] += 1
        if prim.HasAPI(UsdPhysics.RigidBodyAPI):
            counts["rigid_body_prims"] += 1

    up_axis = str(UsdGeom.GetStageUpAxis(stage)).upper()
    warnings = []
    if not has_default_prim:
        warnings.append("missing_default_prim")
    if counts["geometry_prims"] == 0:
        warnings.append("no_geometry")
    if any(value <= 1.0e-9 for value in size_units):
        warnings.append("invalid_or_flat_bbox")
    if abs(meters_per_unit - 1.0) > 1.0e-9:
        warnings.append("non_meter_stage_units")
    if up_axis not in {"Y", "Z"}:
        warnings.append("unsupported_up_axis")
    if counts["collision_prims"] == 0:
        warnings.append("no_authored_collision")

    return {
        "source_path": str(source_path),
        "valid": counts["geometry_prims"] > 0 and all(value > 1.0e-9 for value in size_units),
        "has_default_prim": has_default_prim,
        "selected_root_path": str(root.GetPath()),
        "up_axis": up_axis,
        "meters_per_unit": meters_per_unit,
        "bbox_stage_units": {
            "min": list(minimum),
            "max": list(maximum),
            "size": list(size_units),
        },
        "bbox_m": list(size_m),
        "counts": counts,
        "used_layers": sorted(
            layer.realPath or layer.identifier for layer in stage.GetUsedLayers()
        ),
        "warnings": warnings,
    }


def _oriented_bounds(inspection: dict[str, Any]) -> tuple[list[float], list[float]]:
    raw = inspection["bbox_stage_units"]
    minimum = raw["min"]
    maximum = raw["max"]
    points = itertools.product(
        (minimum[0], maximum[0]),
        (minimum[1], maximum[1]),
        (minimum[2], maximum[2]),
    )
    if inspection["up_axis"] == "Y":
        transformed = [(x, -z, y) for x, y, z in points]
    else:
        transformed = list(points)
    oriented_min = [min(point[index] for point in transformed) for index in range(3)]
    oriented_max = [max(point[index] for point in transformed) for index in range(3)]
    return oriented_min, oriented_max


def wrap_usd(
    source: str | Path,
    output: str | Path,
    *,
    asset_id: str,
    category: str,
    target_bbox_m: tuple[float, float, float] | None = None,
    scale_mode: str = "uniform",
    collision_mode: str = "proxy_box",
) -> dict[str, Any]:
    """Create a centered, Z-up, meter-unit wrapper around one local USD asset."""
    Gf, Sdf, Usd, UsdGeom, UsdPhysics, _ = _pxr_modules()
    source_path = _local_usd_path(source, must_exist=True)
    output_path = _local_usd_path(output, must_exist=False)
    if output_path == source_path:
        raise ValueError("wrapper output must differ from the source USD")
    if scale_mode not in {"uniform", "exact"}:
        raise ValueError("scale_mode must be uniform or exact")
    if collision_mode not in {"proxy_box", "authored", "none"}:
        raise ValueError("collision_mode must be proxy_box, authored, or none")
    if not asset_id or not category:
        raise ValueError("asset_id and category are required")

    source_report = inspect_usd(source_path)
    if not source_report["valid"]:
        raise ValueError(f"source USD failed inspection: {source_report['warnings']}")
    oriented_min, oriented_max = _oriented_bounds(source_report)
    source_size = tuple(oriented_max[index] - oriented_min[index] for index in range(3))
    source_center = tuple((oriented_min[index] + oriented_max[index]) / 2.0 for index in range(3))
    if target_bbox_m is None:
        target_bbox_m = tuple(
            value * source_report["meters_per_unit"] for value in source_size
        )
    target_bbox_m = tuple(float(value) for value in target_bbox_m)
    if any(value <= 0 for value in target_bbox_m):
        raise ValueError("target_bbox_m values must be positive")

    axis_scales = tuple(target_bbox_m[index] / source_size[index] for index in range(3))
    if scale_mode == "uniform":
        scalar = min(axis_scales)
        scales = (scalar, scalar, scalar)
    else:
        scales = axis_scales
    wrapped_bbox = tuple(source_size[index] * scales[index] for index in range(3))
    translation = tuple(-source_center[index] * scales[index] for index in range(3))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    stage = Usd.Stage.CreateNew(output_path.as_posix())
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    root = UsdGeom.Xform.Define(stage, "/Asset")
    root.GetPrim().CreateAttribute("sceneFactory:assetId", Sdf.ValueTypeNames.String).Set(asset_id)
    root.GetPrim().CreateAttribute("sceneFactory:category", Sdf.ValueTypeNames.String).Set(category)
    root.GetPrim().CreateAttribute("sceneFactory:source", Sdf.ValueTypeNames.String).Set(
        source_path.as_posix()
    )

    visual = UsdGeom.Xform.Define(stage, "/Asset/Visual")
    visual_xform = UsdGeom.Xformable(visual)
    visual_xform.AddTranslateOp().Set(Gf.Vec3d(*translation))
    visual_xform.AddScaleOp().Set(Gf.Vec3f(*scales))
    source_root = UsdGeom.Xform.Define(stage, "/Asset/Visual/Source")
    if source_report["up_axis"] == "Y":
        UsdGeom.Xformable(source_root).AddRotateXOp().Set(90.0)
    references = source_root.GetPrim().GetReferences()
    if source_report["has_default_prim"]:
        references.AddReference(source_path.as_posix())
    else:
        references.AddReference(
            Sdf.Reference(source_path.as_posix(), source_report["selected_root_path"])
        )

    if collision_mode == "proxy_box":
        collider = UsdGeom.Cube.Define(stage, "/Asset/Collision")
        collider.CreateSizeAttr(1.0)
        collider_xform = UsdGeom.Xformable(collider)
        collider_xform.AddScaleOp().Set(Gf.Vec3f(*wrapped_bbox))
        UsdGeom.Imageable(collider).CreatePurposeAttr().Set(UsdGeom.Tokens.guide)
        UsdGeom.Imageable(collider).CreateVisibilityAttr().Set(UsdGeom.Tokens.invisible)
        UsdPhysics.CollisionAPI.Apply(collider.GetPrim())

    stage.SetDefaultPrim(root.GetPrim())
    stage.GetRootLayer().Save()
    wrapper_report = inspect_usd(output_path)
    wrapper_report.update(
        {
            "asset_id": asset_id,
            "category": category,
            "source_inspection": source_report,
            "scale_mode": scale_mode,
            "applied_scale": list(scales),
            "collision_mode": collision_mode,
            "target_bbox_m": list(target_bbox_m),
            "wrapped_bbox_m": list(wrapped_bbox),
        }
    )
    return wrapper_report


def build_drop_test_scene(
    asset_usd: str | Path,
    output: str | Path,
    *,
    mass_kg: float,
    drop_height_m: float = 0.12,
) -> dict[str, Any]:
    """Build a small PhysX scene consumed by validate_isaac_runtime.py."""
    Gf, _, Usd, UsdGeom, UsdPhysics, _ = _pxr_modules()
    asset_path = _local_usd_path(asset_usd, must_exist=True)
    output_path = _local_usd_path(output, must_exist=False)
    if mass_kg <= 0 or drop_height_m < 0:
        raise ValueError("mass_kg must be positive and drop_height_m non-negative")
    asset_report = inspect_usd(asset_path)
    if not asset_report["valid"]:
        raise ValueError("asset USD is not valid")
    bbox_z = float(asset_report["bbox_m"][2])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    stage = Usd.Stage.CreateNew(output_path.as_posix())
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    world = UsdGeom.Xform.Define(stage, "/World")
    physics = UsdPhysics.Scene.Define(stage, "/World/PhysicsScene")
    physics.CreateGravityDirectionAttr().Set(Gf.Vec3f(0.0, 0.0, -1.0))
    physics.CreateGravityMagnitudeAttr().Set(9.81)

    floor = UsdGeom.Cube.Define(stage, "/World/Floor")
    floor.CreateSizeAttr(1.0)
    floor_xform = UsdGeom.Xformable(floor)
    floor_xform.AddTranslateOp().Set(Gf.Vec3d(0.0, 0.0, -0.025))
    floor_xform.AddScaleOp().Set(Gf.Vec3f(2.0, 2.0, 0.05))
    UsdPhysics.CollisionAPI.Apply(floor.GetPrim())

    instance = UsdGeom.Xform.Define(stage, "/World/TestAsset")
    instance.GetPrim().GetReferences().AddReference(asset_path.as_posix())
    UsdGeom.Xformable(instance).AddTranslateOp().Set(
        Gf.Vec3d(0.0, 0.0, bbox_z / 2.0 + drop_height_m)
    )
    UsdPhysics.RigidBodyAPI.Apply(instance.GetPrim())
    UsdPhysics.MassAPI.Apply(instance.GetPrim()).CreateMassAttr().Set(float(mass_kg))

    stage.SetDefaultPrim(world.GetPrim())
    stage.GetRootLayer().Save()
    return {
        "valid": True,
        "asset_usd": str(asset_path),
        "drop_scene_usd": str(output_path),
        "mass_kg": float(mass_kg),
        "drop_height_m": float(drop_height_m),
        "initial_center_z_m": bbox_z / 2.0 + drop_height_m,
    }


def build_asset_record(
    wrapper_report: dict[str, Any],
    *,
    source_path: str | Path,
    mass_kg: float,
    friction: float,
    support_top: bool = False,
    source_type: str = "local_usd",
    license_name: str | None = None,
) -> dict[str, Any]:
    if not wrapper_report.get("valid"):
        raise ValueError("wrapper report must be valid")
    bbox = tuple(float(value) for value in wrapper_report["wrapped_bbox_m"])
    record: dict[str, Any] = {
        "asset_id": str(wrapper_report["asset_id"]),
        "category": str(wrapper_report["category"]),
        "bbox_m": list(bbox),
        "source_path": str(Path(source_path).expanduser().resolve()),
        "source_type": source_type,
        "collision_mode": str(wrapper_report["collision_mode"]),
        "mass_kg": float(mass_kg),
        "friction": float(friction),
        "qa_report_path": None,
        "license": license_name,
        "status": "quarantine",
        "tags": ["imported"],
    }
    if support_top:
        record["support_surfaces"] = [
            {
                "name": "top",
                "center": [0.0, 0.0, bbox[2] / 2.0],
                "size": [bbox[0] * 0.94, bbox[1] * 0.94],
            }
        ]
    return record


def promote_asset_record(
    record_path: str | Path,
    runtime_report_path: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    record_file = Path(record_path).expanduser().resolve()
    runtime_file = Path(runtime_report_path).expanduser().resolve()
    record = json.loads(record_file.read_text("utf-8"))
    runtime_report = json.loads(runtime_file.read_text("utf-8"))
    if not runtime_report.get("valid"):
        raise ValueError("runtime report did not pass PhysX validation")
    record["status"] = "validated"
    record["qa_report_path"] = str(runtime_file)
    destination = Path(output_path).expanduser().resolve() if output_path else record_file
    _json_write(destination, record)
    return record


def write_json_report(path: str | Path, payload: dict[str, Any]) -> Path:
    return _json_write(path, payload)
