from __future__ import annotations

from .geometry import (
    inverse_rotate_xy,
    objects_overlap,
    rotate_xy,
    rotated_half_extents_xy,
)
from .models import CompiledScene, ValidationIssue, ValidationReport
from .registry import AssetRegistry
from .spatial import blocks_clearance, inside_region, support_interior


class SceneValidator:
    def __init__(self, registry: AssetRegistry, tolerance_m: float = 0.003) -> None:
        self.registry = registry
        self.tolerance_m = tolerance_m

    def validate(self, scene: CompiledScene) -> ValidationReport:
        issues: list[ValidationIssue] = []
        objects = {item.object_id: item for item in scene.objects}
        room_x, room_y, room_z = scene.room_dimensions_m
        for zone in scene.clearance_zones:
            if (abs(zone.center[0])+zone.size[0]/2 > room_x/2+self.tolerance_m
                    or abs(zone.center[1])+zone.size[1]/2 > room_y/2+self.tolerance_m):
                issues.append(ValidationIssue("clearance_out_of_bounds",
                              f"reserved corridor {zone.name} exceeds room bounds",()))

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
            interior = support_interior(self.registry,item,objects)
            if interior and not inside_region(item,*interior,tolerance=self.tolerance_m):
                issues.append(ValidationIssue("outside_storage_interior",
                              "object exceeds the registered storage interior",(item.object_id,interior[0].object_id)))
            for relation in item.relations:
                if relation.kind == "inside":
                    if interior is None or interior[0].object_id != relation.target:
                        issues.append(ValidationIssue("invalid_inside_relation",
                                      "inside requires a matching registered shelf support",(item.object_id,relation.target)))
                if relation.kind == "around":
                    target = objects.get(relation.target)
                    if item.category != "chair" or item.support != "floor" or (target and target.category != "dining_table"):
                        issues.append(ValidationIssue("invalid_around_relation",
                                      "around requires a floor chair and dining table",(item.object_id,relation.target)))
                    if target is None:
                        issues.append(ValidationIssue("missing_relation_target","around target is missing",(item.object_id,)))
                    else:
                        local=inverse_rotate_xy((x-target.pose.position[0],y-target.pose.position[1]),target.pose.yaw_deg)
                        ax,ay=rotated_half_extents_xy(item.bbox_m,item.pose.yaw_deg-target.pose.yaw_deg)
                        gaps=(abs(local[0])-target.bbox_m[0]/2-ax,abs(local[1])-target.bbox_m[1]/2-ay)
                        gap=max(gaps)
                        if gap < relation.min_distance_m-self.tolerance_m or gap > relation.max_distance_m+self.tolerance_m:
                            issues.append(ValidationIssue("invalid_around_distance","chair does not have the requested table clearance",(item.object_id,target.object_id)))
            for zone in scene.clearance_zones:
                if blocks_clearance(item,zone,self.tolerance_m):
                    issues.append(ValidationIssue("blocked_clearance",f"reserved corridor {zone.name} is obstructed",(item.object_id,)))

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
                "clearance_zone_count": len(scene.clearance_zones),
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
