"""Conservative spatial contracts shared by generation and independent validation."""

from __future__ import annotations

from .geometry import inverse_rotate_xy, rotated_half_extents_xy


def blocks_clearance(item, zone, tolerance=0.003):
    # Any geometry intersecting walking height obstructs a reserved corridor.
    bottom = item.pose.position[2] - item.bbox_m[2] / 2
    if bottom >= 2.0:
        return False
    hx, hy = rotated_half_extents_xy(item.bbox_m, item.pose.yaw_deg)
    return (
        abs(item.pose.position[0] - zone.center[0]) < hx + zone.size[0] / 2 - tolerance
        and abs(item.pose.position[1] - zone.center[1]) < hy + zone.size[1] / 2 - tolerance
    )


def inside_region(item, parent, region, tolerance=0.003):
    local = inverse_rotate_xy(
        (
            item.pose.position[0] - parent.pose.position[0],
            item.pose.position[1] - parent.pose.position[1],
        ),
        parent.pose.yaw_deg,
    )
    hx, hy = rotated_half_extents_xy(item.bbox_m, item.pose.yaw_deg - parent.pose.yaw_deg)
    offsets = (
        local[0] - region.center[0],
        local[1] - region.center[1],
        item.pose.position[2] - parent.pose.position[2] - region.center[2],
    )
    return all(
        abs(offset) + half <= size / 2 + tolerance
        for offset, half, size in zip(offsets, (hx, hy, item.bbox_m[2] / 2), region.size)
    )


def support_interior(registry, item, objects):
    if not item.support or item.support == "floor":
        return None
    target, _, name = item.support.partition(":")
    parent = objects.get(target)
    if parent is None:
        return None
    region = next(
        (r for r in registry.get(parent.asset_id).interior_regions if r.region_id == name), None
    )
    return (parent, region) if region is not None else None
