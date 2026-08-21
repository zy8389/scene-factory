from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open a SceneFactory USD in Isaac Sim and step PhysX headlessly."
    )
    parser.add_argument("usd", type=Path, help="ASCII-only USD path on Windows")
    parser.add_argument("--steps", type=int, default=240, help="Physics steps to run")
    parser.add_argument("--report", type=Path, required=True, help="JSON report path")
    parser.add_argument("--asset-id", help="Asset identifier for the QA report")
    parser.add_argument(
        "--collision-required",
        action="store_true",
        help="Require authored collision beneath /World/TestAsset",
    )
    parser.add_argument(
        "--mesh-required",
        action="store_true",
        help="Require at least one UsdGeom.Mesh beneath /World/TestAsset",
    )
    parser.add_argument(
        "--mass-required",
        action="store_true",
        help="Require a finite positive physics mass beneath /World/TestAsset",
    )
    parser.add_argument(
        "--physics-material-required",
        action="store_true",
        help="Require static and dynamic friction on a bound PhysicsMaterial",
    )
    return parser.parse_args()


def _has_non_ascii(value: str) -> bool:
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return True
    return False


def _world_position(prim, Usd, UsdGeom) -> list[float]:
    matrix = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
    translation = matrix.ExtractTranslation()
    return [float(translation[0]), float(translation[1]), float(translation[2])]


def _vector_attribute(prim, name: str) -> list[float] | None:
    attribute = prim.GetAttribute(name)
    if not attribute.IsValid():
        return None
    value = attribute.Get()
    if value is None:
        return None
    return [float(value[index]) for index in range(3)]


def main() -> int:
    args = _parse_args()
    # SimulationApp forwards unknown process arguments to Kit. Keep our validator
    # arguments (including possibly non-ASCII report paths) out of Kit's parser.
    sys.argv = [sys.argv[0]]
    usd_path = args.usd.resolve()
    report_path = args.report.resolve()
    if args.steps < 1:
        raise ValueError("--steps must be positive")
    if not usd_path.is_file():
        raise FileNotFoundError(usd_path)
    if sys.platform == "win32" and _has_non_ascii(str(usd_path)):
        raise RuntimeError(
            "Isaac Sim/OpenUSD cannot reliably open non-ASCII USD paths on Windows. "
            "Use an ASCII-only output path."
        )

    os.environ.setdefault("OMNI_KIT_ACCEPT_EULA", "YES")
    # SimulationApp must be constructed before importing omni or pxr modules.
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
            "width": 640,
            "height": 480,
            "disable_viewport_updates": True,
            # Isaac 6.0.1 can access-violate during full Windows teardown after all
            # extensions have already shut down. Fast shutdown is NVIDIA's default
            # for standalone SimulationApp scripts and preserves the chosen exit code.
            "fast_shutdown": True,
            "open_usd": usd_path.as_posix(),
            "extra_args": [
                "--/app/renderer/skipWhileMinimized=true",
                "--/rtx-transient/resourcemanager/texturestreaming/enabled=false",
            ],
        }
    )

    report: dict[str, object] = {
        "validator": "Isaac Sim/PhysX",
        "usd": str(usd_path),
        "asset_id": args.asset_id,
        "requested_steps": args.steps,
        "valid": False,
    }
    exit_code = 1
    try:
        import carb
        import omni.usd
        from isaacsim.core.api import SimulationContext
        from pxr import Usd, UsdGeom, UsdPhysics

        stage = omni.usd.get_context().get_stage()
        if stage is None or stage.GetRootLayer().realPath.replace("\\", "/").lower() != usd_path.as_posix().lower():
            raise RuntimeError("Isaac Sim did not open the requested USD stage")

        rigid_prims = [
            prim for prim in stage.Traverse() if prim.HasAPI(UsdPhysics.RigidBodyAPI)
        ]
        if not rigid_prims:
            raise RuntimeError("The stage contains no rigid bodies to simulate")
        asset_prim = stage.GetPrimAtPath("/World/TestAsset")
        asset_collision_prims = []
        asset_mesh_prims = []
        asset_mass_values = []
        asset_material_prims = []
        if asset_prim.IsValid():
            asset_collision_prims = [
                prim
                for prim in Usd.PrimRange(asset_prim)
                if prim.HasAPI(UsdPhysics.CollisionAPI)
            ]
            asset_mesh_prims = [
                prim for prim in Usd.PrimRange(asset_prim) if prim.IsA(UsdGeom.Mesh)
            ]
            for prim in Usd.PrimRange(asset_prim):
                if prim.HasAPI(UsdPhysics.MassAPI):
                    mass_attr = prim.GetAttribute("physics:mass")
                    if mass_attr.IsValid() and mass_attr.Get() is not None:
                        asset_mass_values.append(float(mass_attr.Get()))
                if prim.HasAPI(UsdPhysics.MaterialAPI):
                    asset_material_prims.append(prim)
        asset_height_attr = (
            asset_prim.GetAttribute("sceneFactory:assetHeightM")
            if asset_prim.IsValid()
            else None
        )
        asset_height_m = (
            float(asset_height_attr.Get())
            if asset_height_attr and asset_height_attr.IsValid() and asset_height_attr.Get() is not None
            else None
        )
        drop_height_attr = (
            asset_prim.GetAttribute("sceneFactory:dropHeightM")
            if asset_prim.IsValid()
            else None
        )
        drop_height_m = (
            float(drop_height_attr.Get())
            if drop_height_attr and drop_height_attr.IsValid() and drop_height_attr.Get() is not None
            else None
        )
        initial_positions = {
            str(prim.GetPath()): _world_position(prim, Usd, UsdGeom) for prim in rigid_prims
        }

        simulation = SimulationContext(
            physics_dt=1.0 / 60.0,
            rendering_dt=1.0 / 60.0,
            stage_units_in_meters=1.0,
            physics_prim_path="/World/PhysicsScene",
            stage=stage,
        )
        physics_context = simulation.get_physics_context()
        physics_context.set_physx_update_transformations_settings(
            update_to_usd=True,
            update_velocities_to_usd=True,
        )
        simulation.initialize_physics()
        simulation.play()
        recent_positions: dict[str, list[list[float]]] = {
            path: [] for path in initial_positions
        }
        for _ in range(args.steps):
            simulation.step(render=False)
            if _ >= max(0, args.steps - 30):
                for prim in rigid_prims:
                    path = str(prim.GetPath())
                    recent_positions[path].append(_world_position(prim, Usd, UsdGeom))
        final_positions = {
            str(prim.GetPath()): _world_position(prim, Usd, UsdGeom) for prim in rigid_prims
        }
        final_asset_bbox = None
        if asset_prim.IsValid():
            bbox_cache = UsdGeom.BBoxCache(
                Usd.TimeCode.Default(),
                [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy],
                useExtentsHint=True,
            )
            aligned = bbox_cache.ComputeWorldBound(asset_prim).ComputeAlignedRange()
            if not aligned.IsEmpty():
                minimum = aligned.GetMin()
                maximum = aligned.GetMax()
                final_asset_bbox = {
                    "min": [float(minimum[index]) for index in range(3)],
                    "max": [float(maximum[index]) for index in range(3)],
                }
        final_linear_velocity = {
            str(prim.GetPath()): velocity
            for prim in rigid_prims
            if (velocity := _vector_attribute(prim, "physics:velocity")) is not None
        }
        final_angular_velocity = {
            str(prim.GetPath()): velocity
            for prim in rigid_prims
            if (velocity := _vector_attribute(prim, "physics:angularVelocity")) is not None
        }
        # stop() resets Isaac's time and step counters, so capture them first.
        completed_steps = simulation.current_time_step_index
        simulated_seconds = simulation.current_time
        simulation.stop()

        displacements = {
            path: [final_positions[path][axis] - start[axis] for axis in range(3)]
            for path, start in initial_positions.items()
        }
        finite_positions = all(
            abs(value) < 1.0e6
            for position in final_positions.values()
            for value in position
        )
        # Objects should settle on their supports, not tunnel through the floor or explode.
        physically_bounded = all(
            -0.25 <= position[2] <= 10.0 for position in final_positions.values()
        )
        drop_test_enabled = asset_height_m is not None and drop_height_m is not None
        drop_displacement_m = None
        no_floor_penetration = True
        stable_stop = True
        if drop_test_enabled and "/World/TestAsset" in initial_positions:
            start_z = initial_positions["/World/TestAsset"][2]
            final_z = final_positions["/World/TestAsset"][2]
            drop_displacement_m = start_z - final_z
            if final_asset_bbox is not None:
                no_floor_penetration = final_asset_bbox["min"][2] >= -0.01
            else:
                no_floor_penetration = final_z >= asset_height_m / 2.0 - 0.01
            samples = recent_positions.get("/World/TestAsset", [])
            if len(samples) >= 2:
                stable_stop = max(
                    abs(samples[index][2] - samples[index - 1][2])
                    for index in range(1, len(samples))
                ) <= 0.01
            else:
                stable_stop = False
        collision_passed = bool(asset_collision_prims) if args.collision_required else True
        mesh_passed = bool(asset_mesh_prims) if args.mesh_required else True
        mass_passed = (
            bool(asset_mass_values) and all(value > 0.0 for value in asset_mass_values)
            if args.mass_required
            else True
        )
        friction_passed = True
        if args.physics_material_required:
            friction_passed = False
            for prim in asset_material_prims:
                static = prim.GetAttribute("physics:staticFriction").Get()
                dynamic = prim.GetAttribute("physics:dynamicFriction").Get()
                if (
                    static is not None
                    and dynamic is not None
                    and float(static) >= 0.0
                    and float(dynamic) >= 0.0
                ):
                    friction_passed = True
                    break
        checks = {
            "stage_opened": True,
            "physics_scene_exists": stage.GetPrimAtPath("/World/PhysicsScene").IsValid(),
            "rigid_bodies_found": bool(rigid_prims),
            "asset_collision_found": collision_passed,
            "asset_mesh_found": mesh_passed,
            "mass_found": mass_passed,
            "physics_material_found": friction_passed,
            "requested_steps_completed": completed_steps >= args.steps,
            "final_positions_finite": finite_positions,
            "rigid_bodies_physically_bounded": physically_bounded,
        }
        if drop_test_enabled:
            checks.update(
                {
                    "drop_occurred": bool(drop_displacement_m is not None and drop_displacement_m > 0.1),
                    "no_floor_penetration": no_floor_penetration,
                    "stable_stop": stable_stop,
                }
            )
        report.update(
            {
                "isaac_sim_version": "6.0.1.0",
                "kit_build": carb.settings.get_settings().get_as_string("/app/buildVersion"),
                "completed_steps": completed_steps,
                "simulated_seconds": simulated_seconds,
                "rigid_body_count": len(rigid_prims),
                "initial_positions_m": initial_positions,
                "final_positions_m": final_positions,
                "final_asset_bbox_m": final_asset_bbox,
                "final_linear_velocity_mps": final_linear_velocity,
                "final_angular_velocity_rps": final_angular_velocity,
                "displacements_m": displacements,
                "collision_prims_under_asset": [
                    str(prim.GetPath()) for prim in asset_collision_prims
                ],
                "mesh_prims_under_asset": [str(prim.GetPath()) for prim in asset_mesh_prims],
                "mass_values_kg": asset_mass_values,
                "physics_material_prims": [str(prim.GetPath()) for prim in asset_material_prims],
                "drop_test": {
                    "enabled": drop_test_enabled,
                    "drop_height_m": drop_height_m,
                    "asset_height_m": asset_height_m,
                    "drop_displacement_m": drop_displacement_m,
                    "no_floor_penetration": no_floor_penetration,
                    "stable_stop": stable_stop,
                },
                "checks": checks,
                "usd_load": "passed",
                "mesh_check": "passed" if mesh_passed else "failed",
                "mass_check": "passed" if mass_passed else "failed",
                "friction_check": "passed" if friction_passed else "failed",
                "collision": "passed" if collision_passed else "failed",
                "physics": "passed" if all(checks.values()) else "failed",
                "valid": all(checks.values()),
            }
        )
        exit_code = 0 if report["valid"] else 1
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()
    finally:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("SCENE_FACTORY_ISAAC_REPORT=" + json.dumps(report, ensure_ascii=False), flush=True)
        app.close(exit_code=exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
