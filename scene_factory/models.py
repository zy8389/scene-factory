from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


Vec2 = tuple[float, float]
Vec3 = tuple[float, float, float]


def _tuple2(value: Any, field_name: str) -> Vec2:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{field_name} must contain exactly 2 numbers")
    return (float(value[0]), float(value[1]))


def _tuple3(value: Any, field_name: str) -> Vec3:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{field_name} must contain exactly 3 numbers")
    return (float(value[0]), float(value[1]), float(value[2]))


@dataclass(frozen=True)
class Pose:
    position: Vec3
    yaw_deg: float = 0.0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Pose":
        return cls(
            position=_tuple3(raw.get("position"), "pose.position"),
            yaw_deg=float(raw.get("yaw_deg", 0.0)),
        )


@dataclass(frozen=True)
class SupportSurface:
    name: str
    center: Vec3
    size: Vec2

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SupportSurface":
        return cls(
            name=str(raw["name"]),
            center=_tuple3(raw["center"], "support_surface.center"),
            size=_tuple2(raw["size"], "support_surface.size"),
        )


@dataclass(frozen=True)
class AssetRecord:
    asset_id: str
    category: str
    bbox_m: Vec3
    primitive: str = "cube"
    color: Vec3 = (0.6, 0.6, 0.6)
    mass_kg: float = 1.0
    friction: float = 0.5
    support_surfaces: tuple[SupportSurface, ...] = ()
    source_path: str | None = None
    source_type: str = "primitive"
    collision_mode: str = "primitive"
    qa_report_path: str | None = None
    license: str | None = None
    status: str = "validated"
    tags: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AssetRecord":
        bbox = _tuple3(raw["bbox_m"], "asset.bbox_m")
        if any(size <= 0 for size in bbox):
            raise ValueError(f"asset {raw.get('asset_id')} has a non-positive bbox")
        source_path = raw.get("source_path")
        collision_mode = str(
            raw.get("collision_mode", "authored" if source_path else "primitive")
        )
        if collision_mode not in {"primitive", "authored", "proxy_box", "none"}:
            raise ValueError(f"unsupported collision_mode: {collision_mode}")
        return cls(
            asset_id=str(raw["asset_id"]),
            category=str(raw["category"]),
            bbox_m=bbox,
            primitive=str(raw.get("primitive", "cube")),
            color=_tuple3(raw.get("color", (0.6, 0.6, 0.6)), "asset.color"),
            mass_kg=float(raw.get("mass_kg", 1.0)),
            friction=float(raw.get("friction", 0.5)),
            support_surfaces=tuple(
                SupportSurface.from_dict(item) for item in raw.get("support_surfaces", [])
            ),
            source_path=source_path,
            source_type=str(raw.get("source_type", "local_usd" if source_path else "primitive")),
            collision_mode=collision_mode,
            qa_report_path=raw.get("qa_report_path"),
            license=raw.get("license"),
            status=str(raw.get("status", "validated")),
            tags=tuple(str(tag) for tag in raw.get("tags", [])),
        )


@dataclass(frozen=True)
class Relation:
    kind: str
    target: str
    min_distance_m: float = 0.08
    max_distance_m: float = 0.35

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Relation":
        relation = cls(
            kind=str(raw["kind"]),
            target=str(raw["target"]),
            min_distance_m=float(raw.get("min_distance_m", 0.08)),
            max_distance_m=float(raw.get("max_distance_m", 0.35)),
        )
        if relation.min_distance_m < 0 or relation.max_distance_m < relation.min_distance_m:
            raise ValueError(f"invalid relation distance range for target {relation.target}")
        return relation


@dataclass(frozen=True)
class ObjectRequest:
    object_id: str
    category: str
    asset_id: str | None = None
    support: str | None = None
    dynamic: bool = True
    fixed_pose: Pose | None = None
    yaw_range_deg: Vec2 = (-180.0, 180.0)
    region_xy: tuple[float, float, float, float] | None = None
    edge_bias: bool = False
    relations: tuple[Relation, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ObjectRequest":
        fixed_pose = Pose.from_dict(raw["fixed_pose"]) if raw.get("fixed_pose") else None
        region_raw = raw.get("region_xy")
        region = None
        if region_raw is not None:
            if not isinstance(region_raw, (list, tuple)) or len(region_raw) != 4:
                raise ValueError("object.region_xy must contain [xmin, xmax, ymin, ymax]")
            region = tuple(float(value) for value in region_raw)
        result = cls(
            object_id=str(raw["object_id"]),
            category=str(raw["category"]),
            asset_id=raw.get("asset_id"),
            support=raw.get("support"),
            dynamic=bool(raw.get("dynamic", True)),
            fixed_pose=fixed_pose,
            yaw_range_deg=_tuple2(raw.get("yaw_range_deg", (-180, 180)), "yaw_range_deg"),
            region_xy=region,
            edge_bias=bool(raw.get("edge_bias", False)),
            relations=tuple(Relation.from_dict(item) for item in raw.get("relations", [])),
        )
        if result.fixed_pose is None and result.support is None:
            raise ValueError(f"object {result.object_id} needs fixed_pose or support")
        return result


@dataclass(frozen=True)
class SceneRecipe:
    name: str
    room_type: str
    room_dimensions_m: Vec3
    event: str
    description: str
    keywords: tuple[str, ...]
    objects: tuple[ObjectRequest, ...]
    task: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SceneRecipe":
        recipe = cls(
            name=str(raw["name"]),
            room_type=str(raw["room_type"]),
            room_dimensions_m=_tuple3(raw["room_dimensions_m"], "room_dimensions_m"),
            event=str(raw["event"]),
            description=str(raw.get("description", "")),
            keywords=tuple(str(item).lower() for item in raw.get("keywords", [])),
            objects=tuple(ObjectRequest.from_dict(item) for item in raw["objects"]),
            task=dict(raw.get("task", {})),
        )
        ids = [item.object_id for item in recipe.objects]
        if len(ids) != len(set(ids)):
            raise ValueError(f"recipe {recipe.name} contains duplicate object IDs")
        return recipe

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlacedObject:
    object_id: str
    asset_id: str
    category: str
    bbox_m: Vec3
    pose: Pose
    dynamic: bool
    support: str | None
    relations: tuple[Relation, ...] = ()


@dataclass(frozen=True)
class CompiledScene:
    scene_id: str
    seed: int
    recipe_name: str
    room_type: str
    room_dimensions_m: Vec3
    event: str
    description: str
    objects: tuple[PlacedObject, ...]
    task: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    object_ids: tuple[str, ...] = ()
    severity: str = "error"


@dataclass(frozen=True)
class ValidationReport:
    valid: bool
    issues: tuple[ValidationIssue, ...]
    metrics: dict[str, float | int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
