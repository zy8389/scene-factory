"""Independent OpenUSD checks and real sequential Isaac/PhysX acceptance for the pack."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def prepare(root, output):
    """Stage bytes in an ASCII directory; never change source geometry during QA."""
    from pxr import Gf, Usd, UsdGeom, UsdPhysics, UsdShade, UsdUtils
    from scene_factory.registry import AssetRegistry

    registry = AssetRegistry.load(root / "data/assets/registry.jsonl")
    records = registry.list()
    records = [r for r in records if "kitchen_dining_pack" in r.tags]
    staged = output / "assets"
    for asset in records:
        for value in (asset.source_path, asset.collision_path):
            target = staged / value
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / "data/assets" / value, target)
    stage = Usd.Stage.CreateNew(str(output / "acceptance.usda"))
    UsdGeom.SetStageMetersPerUnit(stage, 1)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.Xform.Define(stage, "/World")
    stage.SetDefaultPrim(stage.GetPrimAtPath("/World"))
    physics = UsdPhysics.Scene.Define(stage, "/World/PhysicsScene")
    physics.CreateGravityDirectionAttr(Gf.Vec3f(0, 0, -1))
    physics.CreateGravityMagnitudeAttr(9.81)
    floor = UsdGeom.Cube.Define(stage, "/World/Floor")
    floor.CreateSizeAttr(1)
    xf = UsdGeom.Xformable(floor)
    xf.AddTranslateOp().Set(Gf.Vec3d(12, 5, -0.05))
    xf.AddScaleOp().Set(Gf.Vec3f(60, 30, 0.1))
    UsdPhysics.CollisionAPI.Apply(floor.GetPrim())
    material = UsdShade.Material.Define(stage, "/World/FloorMaterial")
    mat = UsdPhysics.MaterialAPI.Apply(material.GetPrim())
    mat.CreateStaticFrictionAttr(0.55)
    mat.CreateDynamicFrictionAttr(0.4)
    UsdShade.MaterialBindingAPI.Apply(floor.GetPrim()).Bind(material, materialPurpose="physics")
    checks = {}
    probes = []

    def sphere(path, position):
        ball = UsdGeom.Sphere.Define(stage, path)
        ball.CreateRadiusAttr(0.025)
        UsdGeom.Xformable(ball).AddTranslateOp().Set(Gf.Vec3d(*position))
        UsdPhysics.CollisionAPI.Apply(ball.GetPrim())
        UsdPhysics.RigidBodyAPI.Apply(ball.GetPrim())
        UsdPhysics.MassAPI.Apply(ball.GetPrim()).CreateMassAttr(0.05)
        return ball.GetPrim()

    def reference(asset, path, position, dynamic):
        xf = UsdGeom.Xform.Define(stage, path)
        xf.GetPrim().GetReferences().AddReference((staged / asset.source_path).as_posix())
        UsdGeom.Xformable(xf).AddTranslateOp().Set(Gf.Vec3d(*position))
        collision = UsdGeom.Xform.Define(stage, path + "/Collision")
        collision.GetPrim().GetReferences().AddReference((staged / asset.collision_path).as_posix())
        if dynamic:
            UsdPhysics.RigidBodyAPI.Apply(xf.GetPrim())
            UsdPhysics.MassAPI.Apply(xf.GetPrim()).CreateMassAttr(asset.mass_kg)
        return xf.GetPrim()

    for index, asset in enumerate(records):
        visual = Usd.Stage.Open(str(staged / asset.source_path))
        collider = Usd.Stage.Open(str(staged / asset.collision_path))
        bounds = (
            UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
            .ComputeWorldBound(visual.GetDefaultPrim())
            .ComputeAlignedRange()
        )
        dims = [float(v) for v in bounds.GetSize()]
        center = [float(v) for v in bounds.GetMidpoint()]
        meshes = [p for p in visual.Traverse() if p.IsA(UsdGeom.Mesh)]
        colliders = [p for p in collider.Traverse() if p.HasAPI(UsdPhysics.CollisionAPI)]
        mesh_valid = True
        for prim in meshes:
            mesh = UsdGeom.Mesh(prim)
            points = mesh.GetPointsAttr().Get()
            counts = mesh.GetFaceVertexCountsAttr().Get()
            indices = mesh.GetFaceVertexIndicesAttr().Get()
            mesh_valid &= bool(points) and all(
                math.isfinite(float(x)) for point in points for x in point
            )
            mesh_valid &= sum(counts) == len(indices) and all(0 <= i < len(points) for i in indices)
        _, _, missing = UsdUtils.ComputeAllDependencies(str(staged / asset.source_path))
        _, _, missing_collision = UsdUtils.ComputeAllDependencies(
            str(staged / asset.collision_path)
        )
        materials = []
        for prim in colliders:
            bound, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial(
                materialPurpose="physics"
            )
            materials.append(
                bool(bound)
                and abs(float(bound.GetPrim().GetAttribute("physics:staticFriction").Get()) - 0.55)
                < 1e-6
                and abs(float(bound.GetPrim().GetAttribute("physics:dynamicFriction").Get()) - 0.4)
                < 1e-6
            )
        checks[asset.asset_id] = {
            "asset_id": asset.asset_id,
            "checks": {
                "z_up": UsdGeom.GetStageUpAxis(visual) == UsdGeom.Tokens.z,
                "metres": UsdGeom.GetStageMetersPerUnit(visual) == 1,
                "bounds_match": all(abs(a - b) < 1e-6 for a, b in zip(dims, asset.bbox_m)),
                "centred_origin": all(abs(v) < 1e-6 for v in center),
                "mesh_valid": bool(meshes) and mesh_valid,
                "collision_parts": len(colliders) == len(meshes) > 0,
                "bound_friction": all(materials) and bool(materials),
                "resolved_dependencies": not missing and not missing_collision,
            },
            "bounds_m": dims,
            "mesh_count": len(meshes),
            "collision_count": len(colliders),
            "usd_sha256": hashlib.sha256((staged / asset.source_path).read_bytes()).hexdigest(),
            "collision_sha256": hashlib.sha256(
                (staged / asset.collision_path).read_bytes()
            ).hexdigest(),
            "glb_sha256": hashlib.sha256(
                (root / "data/assets/source" / asset.asset_id / "visual.glb").read_bytes()
            ).hexdigest(),
            "physics_cases": [],
            "grasp": "not_run",
        }
        x = index * 4
        if not asset.rigid_body:
            reference(asset, f"/World/{asset.asset_id}", (x, 0, asset.bbox_m[2] / 2), False)
            for i, surface in enumerate(asset.support_surfaces):
                z = asset.bbox_m[2] / 2 + surface.center[2]
                path = f"/World/Probe_{asset.asset_id}_{i}"
                sphere(path, (x + surface.center[0], surface.center[1], z + 0.175))
                probes.append(
                    {
                        "asset_id": asset.asset_id,
                        "path": path,
                        "case": surface.name + "_contact",
                        "expected_z": z + 0.025,
                        "start_z": z + 0.175,
                        "static_parent": f"/World/{asset.asset_id}",
                    }
                )
        else:
            for case, y, height in (("floor_drop", 6, 0), ("table_contact", 9, 0.76)):
                if height:
                    table = UsdGeom.Cube.Define(stage, f"/World/Table_{asset.asset_id}")
                    table.CreateSizeAttr(1)
                    xf = UsdGeom.Xformable(table)
                    xf.AddTranslateOp().Set(Gf.Vec3d(x, y, height / 2))
                    xf.AddScaleOp().Set(Gf.Vec3f(2, 2, height))
                    UsdPhysics.CollisionAPI.Apply(table.GetPrim())
                    UsdShade.MaterialBindingAPI.Apply(table.GetPrim()).Bind(
                        material, materialPurpose="physics"
                    )
                path = f"/World/{asset.asset_id}_{case}"
                z = height + asset.bbox_m[2] / 2
                reference(asset, path, (x, y, z + 0.45), True)
                probes.append(
                    {
                        "asset_id": asset.asset_id,
                        "path": path,
                        "case": case,
                        "expected_z": z,
                        "start_z": z + 0.45,
                    }
                )
    stage.GetRootLayer().Save()
    (output / "prepared.json").write_text(
        json.dumps({"checks": checks, "probes": probes}, indent=2), encoding="utf-8"
    )
    return checks, probes


def simulate(output, checks, probes, steps, promote_after=False):
    os.environ.setdefault("OMNI_KIT_ACCEPT_EULA", "YES")
    from isaac_kit_compat import load_simulation_app

    sys.argv = [sys.argv[0]]
    app = load_simulation_app()(
        {
            "headless": True,
            "hide_ui": True,
            "renderer": "Minimal",
            "multi_gpu": False,
            "max_gpu_count": 1,
            "disable_viewport_updates": True,
            "fast_shutdown": True,
            "open_usd": str(output / "acceptance.usda"),
        }
    )
    exit_code = 1
    try:
        import omni.usd
        from isaacsim.core.api import SimulationContext
        from pxr import Usd, UsdGeom

        stage = omni.usd.get_context().get_stage()

        def position(path):
            transform = UsdGeom.Xformable(stage.GetPrimAtPath(path)).ComputeLocalToWorldTransform(
                Usd.TimeCode.Default()
            )
            return [float(x) for x in transform.ExtractTranslation()]

        initial = {p["path"]: position(p["path"]) for p in probes}
        parents = {
            p["static_parent"]: position(p["static_parent"])
            for p in probes
            if p.get("static_parent")
        }
        sim = SimulationContext(
            physics_dt=1 / 60,
            rendering_dt=1 / 60,
            stage_units_in_meters=1,
            physics_prim_path="/World/PhysicsScene",
            stage=stage,
        )
        sim.get_physics_context().set_physx_update_transformations_settings(
            update_to_usd=True, update_velocities_to_usd=True
        )
        sim.initialize_physics()
        sim.play()
        samples = {p["path"]: [] for p in probes}
        for step in range(steps):
            sim.step(render=False)
            if step >= steps - 30:
                for path in samples:
                    samples[path].append(position(path))
        for probe in probes:
            path = probe["path"]
            final = position(path)
            velocity = stage.GetPrimAtPath(path).GetAttribute("physics:velocity").Get()
            speed = (
                math.sqrt(sum(float(x) ** 2 for x in velocity)) if velocity is not None else None
            )
            stable = max(math.dist(a, b) for a, b in zip(samples[path], samples[path][1:])) < 0.001
            case_checks = {
                "fell": initial[path][2] - final[2] > 0.1,
                "on_support": abs(final[2] - probe["expected_z"]) < 0.012,
                "stable": stable,
                "stopped": speed is not None and speed < 0.02,
                "finite": all(math.isfinite(x) for x in final),
            }
            if probe.get("static_parent"):
                parent = probe["static_parent"]
                case_checks["static_fixture_unchanged"] = (
                    math.dist(parents[parent], position(parent)) < 1e-9
                )
            checks[probe["asset_id"]]["physics_cases"].append(
                {
                    "case": probe["case"],
                    "checks": case_checks,
                    "initial_position_m": initial[path],
                    "final_position_m": final,
                    "final_speed_mps": speed,
                    "expected_center_z_m": probe["expected_z"],
                    "passed": all(case_checks.values()),
                }
            )
        sim.stop()
        for report in checks.values():
            report.update(
                {
                    "validator": "OpenUSD + real Isaac Sim/PhysX",
                    "steps": steps,
                    "dt_s": 1 / 60,
                    "usd_load": "passed",
                    "mesh_check": "passed" if report["checks"]["mesh_valid"] else "failed",
                    "collision": "passed" if report["checks"]["collision_parts"] else "failed",
                    "physics": "passed"
                    if all(c["passed"] for c in report["physics_cases"])
                    else "failed",
                }
            )
            report["valid"] = all(report["checks"].values()) and report["physics"] == "passed"
        (output / "validation.json").write_text(
            json.dumps(
                {"valid": all(r["valid"] for r in checks.values()), "assets": checks}, indent=2
            ),
            encoding="utf-8",
        )
        # Kit fast shutdown exits the process. Promotion must precede app.close().
        if promote_after:
            promote(ROOT, output)
        exit_code = 0 if all(r["valid"] for r in checks.values()) else 1
    finally:
        app.close(exit_code=exit_code)


def promote(root, output):
    from scene_factory.registry import AssetRegistry

    payload = json.loads((output / "validation.json").read_text())
    if not payload["valid"]:
        raise RuntimeError("asset acceptance failed; registry not promoted")
    registry = AssetRegistry.load(root / "data/assets/registry.jsonl")
    for asset_id, report in payload["assets"].items():
        record = registry.get(asset_id)
        for key, path in (
            ("usd_sha256", root / "data/assets" / record.source_path),
            ("collision_sha256", root / "data/assets" / record.collision_path),
            ("glb_sha256", root / "data/assets/source" / asset_id / "visual.glb"),
        ):
            if hashlib.sha256(path.read_bytes()).hexdigest() != report[key]:
                raise RuntimeError(f"{asset_id} source changed after QA")
        target = root / "data/assets/qa_reports" / f"{asset_id}.json"
        target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if record.status in {"normalized", "quarantine"}:
            registry.promote_to_validated(asset_id, report)
        registry.update(asset_id, collision_status="validated")
        if registry.get(asset_id).status == "validated":
            registry.promote_to_ready(asset_id, target)
    registry.save()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--steps", type=int, default=360)
    parser.add_argument("--promote", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--promote-only", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if not str(output).isascii():
        raise ValueError("Isaac output must have an ASCII path")
    if args.promote_only:
        promote(ROOT, output)
        return
    if args.prepare_only:
        prepare(ROOT, output)
        return
    # Standalone pxr and Kit's pxr use different DLL builds on Windows. Do not
    # import standalone pxr into the process that boots SimulationApp.
    subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--output", str(output), "--prepare-only"],
        check=True,
    )
    prepared = json.loads((output / "prepared.json").read_text())
    simulate(output, prepared["checks"], prepared["probes"], args.steps, args.promote)


if __name__ == "__main__":
    main()
