from __future__ import annotations

import math

from .models import PlacedObject, Pose, Vec2, Vec3


def rotate_xy(point: Vec2, yaw_deg: float) -> Vec2:
    angle = math.radians(yaw_deg)
    cosine, sine = math.cos(angle), math.sin(angle)
    return (cosine * point[0] - sine * point[1], sine * point[0] + cosine * point[1])


def inverse_rotate_xy(point: Vec2, yaw_deg: float) -> Vec2:
    return rotate_xy(point, -yaw_deg)


def rotated_half_extents_xy(bbox: Vec3, yaw_deg: float) -> Vec2:
    half_x, half_y = bbox[0] / 2.0, bbox[1] / 2.0
    angle = math.radians(yaw_deg)
    cosine, sine = abs(math.cos(angle)), abs(math.sin(angle))
    return (cosine * half_x + sine * half_y, sine * half_x + cosine * half_y)


def z_interval(position_z: float, bbox_z: float) -> tuple[float, float]:
    return (position_z - bbox_z / 2.0, position_z + bbox_z / 2.0)


def objects_overlap(a: PlacedObject, b: PlacedObject, tolerance: float = 1e-5) -> bool:
    az0, az1 = z_interval(a.pose.position[2], a.bbox_m[2])
    bz0, bz1 = z_interval(b.pose.position[2], b.bbox_m[2])
    if min(az1, bz1) - max(az0, bz0) <= tolerance:
        return False
    ahx, ahy = rotated_half_extents_xy(a.bbox_m, a.pose.yaw_deg)
    bhx, bhy = rotated_half_extents_xy(b.bbox_m, b.pose.yaw_deg)
    dx = abs(a.pose.position[0] - b.pose.position[0])
    dy = abs(a.pose.position[1] - b.pose.position[1])
    return dx < ahx + bhx - tolerance and dy < ahy + bhy - tolerance


def distance_xy(a: Pose, b: Pose) -> float:
    return math.hypot(a.position[0] - b.position[0], a.position[1] - b.position[1])

