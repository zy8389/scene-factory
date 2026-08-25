from __future__ import annotations

from .geometry import (
    inverse_rotate_xy,
    objects_overlap,
    rotate_xy,
    rotated_half_extents_xy,
)
from .models import CompiledScene, ValidationIssue, ValidationReport
from .registry import AssetRegistry


class SceneValidator:
    def __init__(self, registry: AssetRegistry, tolerance_m: float = 0.003) -> None:
        self.registry = registry
        self.tolerance_m = tolerance_m

    def validate(self, scene: CompiledScene) -> ValidationReport:
        issues: list[ValidationIssue] = []
        objects = {item.object_id: item for item in scene.objects}
        room_x, room_y, room_z = scene.room_dimensions_m

        for item in scene.objects:
            x, y, z = item.pose.position
            hx, hy = rotated_half_extents_xy(item.bbox_m, item.pose.yaw_deg)
            hz = item.bbox_m[2] / 2.0
            if abs(x) + hx > room_x / 2.0 + self.tolerance_m:
                issues.append(
                    ValidationIssue("out_of_bounds_x", "object is outside room X bounds", (item.object_id,))
                )
            if abs(y) + hy > room_y / 2.0 + self.tolerance_m:
                issues.append(
                    ValidationIssue("out_of_bounds_y", "object is outside room Y bounds", (item.object_id,))
                )
            if z - hz < -self.tolerance_m or z + hz > room_z + self.tolerance_m:
                issues.append(
                    ValidationIssue("out_of_bounds_z", "object is outside room Z bounds", (item.object_id,))
                )

            support_error = self._support_error(item, objects)
            if support_error is not None:
                issues.append(support_error)

        collision_pairs = 0
        for index, first in enumerate(scene.objects):
            for second in scene.objects[index + 1 :]:
                if first.support and first.support.split(":", 1)[0] == second.object_id:
                    continue
                if second.support and second.support.split(":", 1)[0] == first.object_id:
                    continue
                if objects_overlap(first, second, tolerance=self.tolerance_m):
                    collision_pairs += 1
                    issues.append(
                        ValidationIssue(
                            "overlap",
                            "objects overlap before physics settling",
                            (first.object_id, second.object_id),
                        )
                    )

        return ValidationReport(
            valid=not any(issue.severity == "error" for issue in issues),
            issues=tuple(issues),
            metrics={
                "object_count": len(scene.objects),
                "dynamic_object_count": sum(item.dynamic for item in scene.objects),
                "collision_pair_count": collision_pairs,
            },
        )

    def _support_error(self, item, objects) -> ValidationIssue | None:
        if item.support is None:
            return None
        bottom_z = item.pose.position[2] - item.bbox_m[2] / 2.0
        if item.support == "floor":
            expected_z = 0.0
        else:
            support_id, _, surface_name = item.support.partition(":")
            support_object = objects.get(support_id)
            if support_object is None:
                return ValidationIssue(
                    "missing_support", f"support {support_id} is missing", (item.object_id,)
                )
            asset = self.registry.get(support_object.asset_id)
            surfaces = asset.support_surfaces
            if surface_name:
                surfaces = tuple(surface for surface in surfaces if surface.name == surface_name)
            if not surfaces:
                return ValidationIssue(
                    "missing_surface", f"support surface {item.support} is missing", (item.object_id,)
                )
            surface = surfaces[0]
            support_yaw = support_object.pose.yaw_deg
            rotated_center = rotate_xy((surface.center[0], surface.center[1]), support_yaw)
            surface_world_xy = (
                support_object.pose.position[0] + rotated_center[0],
                support_object.pose.position[1] + rotated_center[1],
            )
            subject_offset = (
                item.pose.position[0] - surface_world_xy[0],
                item.pose.position[1] - surface_world_xy[1],
            )
            subject_local = inverse_rotate_xy(subject_offset, support_yaw)
            footprint_x, footprint_y = rotated_half_extents_xy(
                item.bbox_m, item.pose.yaw_deg - support_yaw
            )
            outside_x = abs(subject_local[0]) + footprint_x > (
                surface.size[0] / 2.0 + self.tolerance_m
            )
            outside_y = abs(subject_local[1]) + footprint_y > (
                surface.size[1] / 2.0 + self.tolerance_m
            )
            if outside_x or outside_y:
                return ValidationIssue(
                    "outside_support_surface",
                    f"object footprint is outside support surface {item.support}",
                    (item.object_id, support_object.object_id),
                )
            expected_z = support_object.pose.position[2] + surface.center[2]
        if abs(bottom_z - expected_z) > self.tolerance_m:
            return ValidationIssue(
                "floating_or_sunk",
                f"object bottom differs from support by {bottom_z - expected_z:.4f} m",
                (item.object_id,),
            )
        return None
