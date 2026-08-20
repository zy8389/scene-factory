from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a SceneFactory USD with OpenUSD/pxr.")
    parser.add_argument("usd", type=Path, help="USD file to validate")
    parser.add_argument("--report", type=Path, help="Optional JSON report path")
    return parser.parse_args()


def _has_non_ascii(value: str) -> bool:
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return True
    return False


def main() -> int:
    args = _parse_args()
    usd_path = args.usd.resolve()
    if not usd_path.is_file():
        raise FileNotFoundError(usd_path)
    if sys.platform == "win32" and _has_non_ascii(str(usd_path)):
        raise RuntimeError(
            "OpenUSD 25.05 on Windows cannot reliably reopen this non-ASCII path. "
            "Export to an ASCII-only path such as F:/scene_factory_runtime/scene.usd."
        )

    try:
        from pxr import Usd, UsdGeom, UsdPhysics, UsdUtils
    except ImportError as exc:
        raise RuntimeError("Run this validator with the Isaac Sim Python executable.") from exc

    stage = Usd.Stage.Open(usd_path.as_posix())
    if stage is None:
        raise RuntimeError(f"OpenUSD failed to open {usd_path}")

    object_prims = []
    collision_prims = []
    rigid_body_prims = []
    mass_prims = []
    unresolved_assets = []
    for prim in stage.Traverse():
        path = str(prim.GetPath())
        if prim.HasAPI(UsdPhysics.CollisionAPI):
            collision_prims.append(path)
        if prim.HasAPI(UsdPhysics.RigidBodyAPI):
            rigid_body_prims.append(path)
        if prim.HasAPI(UsdPhysics.MassAPI):
            mass_prims.append(path)
    objects_container = stage.GetPrimAtPath("/World/Objects")
    if objects_container.IsValid():
        object_prims = list(objects_container.GetChildren())

    def has_collision(prim) -> bool:
        return prim.HasAPI(UsdPhysics.CollisionAPI) or any(
            descendant.HasAPI(UsdPhysics.CollisionAPI)
            for descendant in Usd.PrimRange(prim)
            if descendant != prim
        )
    # Resolve the full dependency graph, including references that Stage.Open could
    # not add to GetUsedLayers(). The third return value contains unresolved paths.
    _, _, unresolved_paths = UsdUtils.ComputeAllDependencies(usd_path.as_posix())
    unresolved_assets.extend(str(path) for path in unresolved_paths)

    checks = {
        "default_prim_is_world": str(stage.GetDefaultPrim().GetPath()) == "/World",
        "up_axis_is_z": UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.z,
        "meters_per_unit_is_one": abs(UsdGeom.GetStageMetersPerUnit(stage) - 1.0) < 1e-9,
        "physics_scene_exists": stage.GetPrimAtPath("/World/PhysicsScene").IsValid(),
        "objects_exist": bool(object_prims),
        "all_objects_have_collision": all(has_collision(prim) for prim in object_prims),
        "all_rigid_bodies_have_mass": set(rigid_body_prims).issubset(mass_prims),
        "no_unresolved_layers": not unresolved_assets,
    }
    report = {
        "validator": "OpenUSD/pxr",
        "usd": str(usd_path),
        "usd_version": list(Usd.GetVersion()),
        "valid": all(checks.values()),
        "checks": checks,
        "counts": {
            "objects": len(object_prims),
            "collisions": len(collision_prims),
            "rigid_bodies": len(rigid_body_prims),
            "mass_apis": len(mass_prims),
        },
        "object_prims": [str(prim.GetPath()) for prim in object_prims],
        "rigid_body_prims": rigid_body_prims,
        "unresolved_layers": unresolved_assets,
    }

    if args.report:
        report_path = args.report.resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
