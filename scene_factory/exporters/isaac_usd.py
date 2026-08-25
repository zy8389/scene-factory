from __future__ import annotations

import os
import re
from pathlib import Path

from ..models import AssetRecord, CompiledScene, PlacedObject
from ..registry import AssetRegistry


class IsaacBackendUnavailable(RuntimeError):
    pass


class IsaacUsdExporter:
    """Export a compiled scene to USD when executed inside an Isaac Sim Python environment."""

    def __init__(self, registry: AssetRegistry) -> None:
        self.registry = registry

    def export(self, scene: CompiledScene, output_path: str | Path) -> Path:
        try:
            from pxr import Gf, Usd, UsdGeom, UsdPhysics
        except ImportError as exc:
            raise IsaacBackendUnavailable(
                "USD export requires Isaac Sim's Python environment (the 'pxr' package is missing). "
                "Run this command with Isaac Sim's python executable."
            ) from exc

        output_path = Path(output_path).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        stage = Usd.Stage.CreateNew(str(output_path))
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
        UsdGeom.SetStageMetersPerUnit(stage, 1.0)
        UsdGeom.Xform.Define(stage, "/World")

        physics_scene = UsdPhysics.Scene.Define(stage, "/World/PhysicsScene")
        physics_scene.CreateGravityDirectionAttr().Set(Gf.Vec3f(0.0, 0.0, -1.0))
        physics_scene.CreateGravityMagnitudeAttr().Set(9.81)

        room_x, room_y, _ = scene.room_dimensions_m
        floor = UsdGeom.Cube.Define(stage, "/World/Room/Floor")
        floor.CreateSizeAttr(1.0)
        floor.CreateDisplayColorAttr([Gf.Vec3f(0.32, 0.30, 0.27)])
        floor_xform = UsdGeom.Xformable(floor)
        floor_xform.AddTranslateOp().Set(Gf.Vec3d(0.0, 0.0, -0.01))
        floor_xform.AddScaleOp().Set(Gf.Vec3f(room_x, room_y, 0.02))
        UsdPhysics.CollisionAPI.Apply(floor.GetPrim())

        for item in scene.objects:
            asset = self.registry.get(item.asset_id)
            self._add_object(stage, item, asset, output_path, Gf, Usd, UsdGeom, UsdPhysics)

        stage.SetDefaultPrim(stage.GetPrimAtPath("/World"))
        stage.GetRootLayer().Save()
        return output_path

    def _add_object(self, stage, item, asset, output_path, Gf, Usd, UsdGeom, UsdPhysics) -> None:
        prim_path = f"/World/Objects/{self._safe_name(item.object_id)}"
        if asset.source_path:
            source_reference = self.registry.resolve_source_path(asset)
            if source_reference is None:
                raise ValueError(f"asset {asset.asset_id} has an empty source path")
            if "://" not in source_reference and not Path(source_reference).is_file():
                raise FileNotFoundError(
                    f"USD source for asset {asset.asset_id} does not exist: {source_reference}"
                )
            xform = UsdGeom.Xform.Define(stage, prim_path)
            xform.GetPrim().GetReferences().AddReference(
                self._relative_reference(source_reference, output_path)
            )
            self._set_pose(UsdGeom.Xformable(xform), item, Gf, scale=None)
            physics_prim = xform.GetPrim()
        else:
            geom, scale = self._define_primitive(stage, prim_path, asset, Gf, UsdGeom)
            self._set_pose(UsdGeom.Xformable(geom), item, Gf, scale=scale)
            physics_prim = geom.GetPrim()
            UsdPhysics.CollisionAPI.Apply(physics_prim)

        physics_prim.CreateAttribute("sceneFactory:objectId", self._string_type()).Set(
            item.object_id
        )
        physics_prim.CreateAttribute("sceneFactory:category", self._string_type()).Set(
            item.category
        )
        collision_reference = self.registry.resolve_collision_path(asset)
        if collision_reference is not None:
            collision_xform = UsdGeom.Xform.Define(stage, f"{prim_path}/AuthoredCollision")
            collision_xform.GetPrim().GetReferences().AddReference(
                self._relative_reference(collision_reference, output_path)
            )
            collision_xform.GetPrim().CreateAttribute(
                "sceneFactory:collisionSource", self._string_type()
            ).Set(self._relative_reference(collision_reference, output_path))
            collision_bounds = UsdGeom.BBoxCache(
                Usd.TimeCode.Default(), [UsdGeom.Tokens.default_]
            ).ComputeLocalBound(collision_xform.GetPrim()).ComputeAlignedBox()
            if not collision_bounds.IsEmpty():
                center = collision_bounds.GetMidpoint()
                UsdGeom.Xformable(collision_xform).AddTranslateOp().Set(
                    Gf.Vec3d(-center[0], -center[1], -center[2])
                )
        if item.fallback_reason:
            physics_prim.CreateAttribute(
                "sceneFactory:fallbackReason", self._string_type()
            ).Set(item.fallback_reason)
        if item.dynamic:
            UsdPhysics.RigidBodyAPI.Apply(physics_prim)
            mass_api = UsdPhysics.MassAPI.Apply(physics_prim)
            mass_api.CreateMassAttr().Set(asset.mass_kg)

    @staticmethod
    def _define_primitive(stage, path, asset: AssetRecord, Gf, UsdGeom):
        scale = None
        if asset.primitive == "cylinder":
            geom = UsdGeom.Cylinder.Define(stage, path)
            geom.CreateAxisAttr(UsdGeom.Tokens.z)
            geom.CreateRadiusAttr(min(asset.bbox_m[0], asset.bbox_m[1]) / 2.0)
            geom.CreateHeightAttr(asset.bbox_m[2])
        elif asset.primitive == "sphere":
            geom = UsdGeom.Sphere.Define(stage, path)
            geom.CreateRadiusAttr(0.5)
            scale = asset.bbox_m
        else:
            geom = UsdGeom.Cube.Define(stage, path)
            geom.CreateSizeAttr(1.0)
            scale = asset.bbox_m
        geom.CreateDisplayColorAttr([Gf.Vec3f(*asset.color)])
        return geom, scale

    @staticmethod
    def _set_pose(xformable, item: PlacedObject, Gf, scale) -> None:
        x, y, z = item.pose.position
        xformable.AddTranslateOp().Set(Gf.Vec3d(x, y, z))
        xformable.AddRotateZOp().Set(item.pose.yaw_deg)
        if scale is not None:
            xformable.AddScaleOp().Set(Gf.Vec3f(*scale))

    @staticmethod
    def _safe_name(value: str) -> str:
        result = re.sub(r"[^A-Za-z0-9_]", "_", value)
        return result if result and not result[0].isdigit() else f"obj_{result}"

    @staticmethod
    def _relative_reference(reference: str, output_path: Path) -> str:
        if "://" in reference:
            return reference
        try:
            relative = Path(reference).resolve().relative_to(output_path.parent.resolve())
            return relative.as_posix()
        except ValueError:
            return Path(os.path.relpath(reference, output_path.parent)).as_posix()

    @staticmethod
    def _string_type():
        from pxr import Sdf

        return Sdf.ValueTypeNames.String
