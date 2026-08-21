from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Author a real L1 collision USD from a converted collision mesh."
    )
    parser.add_argument("source", type=Path, help="Converted collision source USD")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--asset-id", default="mug_001")
    parser.add_argument("--static-friction", type=float, default=0.5)
    parser.add_argument("--dynamic-friction", type=float, default=0.4)
    parser.add_argument("--report", type=Path, required=True)
    return parser


def _ascii(path: Path) -> bool:
    try:
        str(path).encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def _write(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # Keep authoring CLI arguments out of Kit's argument parser.
    sys.argv = [sys.argv[0]]
    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    report_path = args.report.expanduser().resolve()
    report: dict[str, Any] = {
        "asset_id": args.asset_id,
        "source": str(source),
        "output": str(output),
        "collision_level": "L1",
        "strategy": "authored_convex_decomposition_mesh",
        "static_friction": args.static_friction,
        "dynamic_friction": args.dynamic_friction,
        "generated": False,
        "result": "blocked",
        "issues": [],
    }
    if not source.is_file():
        report["issues"].append({"code": "missing_source_usd", "message": str(source)})
        _write(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    if output.exists():
        report["issues"].append({"code": "output_exists", "message": str(output)})
        _write(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    if args.static_friction < 0 or args.dynamic_friction < 0:
        raise ValueError("friction values must be non-negative")
    if sys.platform == "win32" and (not _ascii(source) or not _ascii(output)):
        report["issues"].append(
            {"code": "non_ascii_path", "message": "Isaac USD paths must be ASCII-only on Windows"}
        )
        _write(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    os.environ.setdefault("OMNI_KIT_ACCEPT_EULA", "YES")
    app = None
    try:
        from isaacsim import SimulationApp

        app = SimulationApp(
            {
                "headless": True,
                "hide_ui": True,
                "renderer": "Minimal",
                "minimal_shading_mode": 4,
                "anti_aliasing": 0,
                "multi_gpu": False,
                "max_gpu_count": 1,
                "fast_shutdown": True,
                "width": 320,
                "height": 240,
                "disable_viewport_updates": True,
            }
        )
        from pxr import Sdf, Usd, UsdGeom, UsdPhysics, UsdShade

        source_stage = Usd.Stage.Open(source.as_posix())
        if source_stage is None:
            raise RuntimeError(f"could not open collision source USD: {source}")
        source_meshes = [prim for prim in source_stage.Traverse() if prim.IsA(UsdGeom.Mesh)]
        if not source_meshes:
            raise ValueError("collision source contains no UsdGeom.Mesh prims")

        stage = Usd.Stage.CreateNew(output.as_posix())
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
        UsdGeom.SetStageMetersPerUnit(stage, 1.0)
        root = UsdGeom.Xform.Define(stage, "/Collision")
        root_prim = root.GetPrim()
        root_prim.CreateAttribute("sceneFactory:assetId", Sdf.ValueTypeNames.String).Set(args.asset_id)
        root_prim.CreateAttribute("sceneFactory:collisionLevel", Sdf.ValueTypeNames.String).Set("L1")
        root_prim.CreateAttribute(
            "sceneFactory:collisionStrategy", Sdf.ValueTypeNames.String
        ).Set("authored_convex_decomposition_mesh")
        source_root = UsdGeom.Xform.Define(stage, "/Collision/Source")
        # The YCB collision GLB is Y-up. Keep the authored collider in the
        # same Z-up frame as the normalized visual asset and its drop scene.
        if UsdGeom.GetStageUpAxis(source_stage) == UsdGeom.Tokens.y:
            UsdGeom.Xformable(source_root).AddRotateXOp().Set(90.0)
        reference = os.path.relpath(source, output.parent).replace("\\", "/")
        source_root.GetPrim().GetReferences().AddReference(reference)
        stage.SetDefaultPrim(root_prim)
        stage.Load()

        material = UsdShade.Material.Define(stage, "/Collision/PhysicsMaterial")
        material_api = UsdPhysics.MaterialAPI.Apply(material.GetPrim())
        material_api.CreateStaticFrictionAttr().Set(float(args.static_friction))
        material_api.CreateDynamicFrictionAttr().Set(float(args.dynamic_friction))
        for prim in stage.Traverse():
            if not prim.IsA(UsdGeom.Mesh):
                continue
            UsdPhysics.CollisionAPI.Apply(prim)
            # CollisionAPI alone is insufficient for a dynamic mesh in PhysX;
            # explicitly select a supported convex approximation.
            mesh_collision = UsdPhysics.MeshCollisionAPI.Apply(prim)
            mesh_collision.CreateApproximationAttr().Set("convexHull")
            UsdShade.MaterialBindingAPI(prim).Bind(material)

        flattened = stage.Flatten()
        # Avoid persisting the machine-local source path in layer metadata.
        flattened.documentation = "SceneFactory authored L1 collision USD"
        flattened.Export(output.as_posix())
        report.update(
            {
                "mesh_count": len(source_meshes),
                "material_path": "/Collision/PhysicsMaterial",
                "collision_status": "authored",
                "collision_enabled": True,
                "generated": False,
                "result": "passed",
            }
        )
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        report["issues"].append({"code": "collision_authoring_failed", "message": str(exc)})
    finally:
        _write(report_path, report)
        if app is not None:
            app.close(exit_code=0 if report["result"] == "passed" else 1)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["result"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
