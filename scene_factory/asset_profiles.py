from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CollisionProfile:
    name: str
    strategy: str
    validated_use_cases: tuple[str, ...]
    unsupported_use_cases: tuple[str, ...]


@dataclass(frozen=True)
class ValidationProfile:
    name: str
    drop_height_m: float
    steps: int
    thin_object: bool = False


@dataclass(frozen=True)
class PhysicsDefaults:
    mass_kg: float
    static_friction: float
    dynamic_friction: float
    source: str = "project_default"


COLLISION_PROFILES: dict[str, CollisionProfile] = {
    "concave_container_l1": CollisionProfile(
        "concave_container_l1",
        "authored_convex_decomposition_mesh",
        ("drop", "grasp", "桌面接触"),
        ("containment", "fluid_simulation"),
    ),
    "thin_object_l1": CollisionProfile(
        "thin_object_l1",
        "authored_convex_decomposition_mesh",
        ("drop", "grasp", "桌面接触"),
        ("cutting", "high_speed_tunneling"),
    ),
    "irregular_soft_object_proxy_l1": CollisionProfile(
        "irregular_soft_object_proxy_l1",
        "authored_convex_decomposition_mesh",
        ("drop", "grasp", "floor_contact"),
        ("deformable_simulation", "cloth_simulation"),
    ),
    "small_complex_object_l1": CollisionProfile(
        "small_complex_object_l1",
        "authored_convex_decomposition_mesh",
        ("drop", "grasp", "table_contact"),
        ("articulation", "key_lock_interaction"),
    ),
}


VALIDATION_PROFILES: dict[str, ValidationProfile] = {
    "drop": ValidationProfile("drop", drop_height_m=1.0, steps=360),
    "drop_thin_object": ValidationProfile(
        "drop_thin_object", drop_height_m=0.75, steps=480, thin_object=True
    ),
}


PHYSICS_DEFAULTS: dict[str, PhysicsDefaults] = {
    "bowl": PhysicsDefaults(0.35, 0.50, 0.40),
    "plate": PhysicsDefaults(0.25, 0.45, 0.35),
    "pot": PhysicsDefaults(1.00, 0.50, 0.40),
    "knife": PhysicsDefaults(0.15, 0.35, 0.25),
    "kitchen_knife": PhysicsDefaults(0.15, 0.35, 0.25),
    "backpack": PhysicsDefaults(1.00, 0.60, 0.50),
    "keys": PhysicsDefaults(0.08, 0.30, 0.20),
    "mug": PhysicsDefaults(0.30, 0.50, 0.40),
}


CATEGORY_PROFILES: dict[str, dict[str, str]] = {
    "bowl": {"collision": "concave_container_l1", "validation": "drop"},
    "plate": {"collision": "thin_object_l1", "validation": "drop"},
    "pot": {"collision": "concave_container_l1", "validation": "drop"},
    "knife": {"collision": "thin_object_l1", "validation": "drop_thin_object"},
    "kitchen_knife": {"collision": "thin_object_l1", "validation": "drop_thin_object"},
    "backpack": {
        "collision": "irregular_soft_object_proxy_l1",
        "validation": "drop",
    },
    "keys": {"collision": "small_complex_object_l1", "validation": "drop"},
    "mug": {"collision": "concave_container_l1", "validation": "drop"},
}


def collision_profile(name: str) -> CollisionProfile:
    try:
        return COLLISION_PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"unknown collision profile: {name}") from exc


def validation_profile(name: str) -> ValidationProfile:
    try:
        return VALIDATION_PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"unknown validation profile: {name}") from exc


def category_profile(category: str) -> dict[str, str]:
    try:
        return CATEGORY_PROFILES[category]
    except KeyError as exc:
        raise ValueError(f"unknown asset category profile: {category}") from exc


def physics_defaults(category: str, override: dict[str, Any] | None = None) -> PhysicsDefaults:
    base = PHYSICS_DEFAULTS.get(category, PhysicsDefaults(0.5, 0.5, 0.4))
    values = override or {}
    return PhysicsDefaults(
        mass_kg=float(values.get("mass_kg", base.mass_kg)),
        static_friction=float(values.get("static_friction", base.static_friction)),
        dynamic_friction=float(values.get("dynamic_friction", base.dynamic_friction)),
        source=str(values.get("source", base.source)),
    )


def sanity_check_bbox(category: str, bbox_m: tuple[float, float, float] | list[float]) -> list[str]:
    """Return category-specific size issues without silently changing scale."""
    if len(bbox_m) != 3 or any(float(value) <= 0 for value in bbox_m):
        return ["bbox must contain three positive dimensions"]
    dimensions = [float(value) for value in bbox_m]
    largest = max(dimensions)
    smallest = min(dimensions)
    bounds = {
        "bowl": (0.05, 0.80),
        "plate": (0.08, 0.80),
        "pot": (0.10, 1.00),
        "knife": (0.05, 1.20),
        "backpack": (0.15, 1.50),
        "keys": (0.01, 0.35),
        "mug": (0.05, 0.60),
    }
    effective_category = "knife" if category == "kitchen_knife" else category
    lower, upper = bounds.get(effective_category, (0.01, 2.0))
    issues: list[str] = []
    if largest < lower or largest > upper:
        issues.append(f"largest dimension {largest:.6f} m outside {category} range [{lower}, {upper}]")
    if effective_category in {"plate", "knife"} and smallest < 0.002:
        issues.append(f"thin dimension {smallest:.6f} m is below collision sanity floor")
    return issues
