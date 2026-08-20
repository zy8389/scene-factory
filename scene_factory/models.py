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


def _support_surface_items(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize legacy plural and Registry v2 singular support fields."""
    value = raw.get("support_surfaces", raw.get("support_surface", []))
    if value is None:
        return []
    if isinstance(value, dict):
        # Accept both one surface object and a name -> surface mapping.
        if {"name", "center", "size"}.issubset(value):
            return [value]
        return [
            {"name": name, **surface}
            for name, surface in value.items()
            if isinstance(surface, dict)
        ]
    if isinstance(value, (list, tuple)):
        return list(value)
    raise ValueError("asset.support_surface(s) must be an object or array")


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
    # Registry v2 metadata.  The legacy fields above remain the canonical
    # representation used by layout/export code.
    name: str | None = None
    asset_hash: str | None = None
    usd_path: str | None = None
    collision_path: str | None = None
    collision_status: str = "not_provided"
    grasp_region: Any = None
    source: str | None = None
    metadata_mass: float | None = None
    metadata_friction: float | None = None
    static_friction: float | None = None
    dynamic_friction: float | None = None
    rigid_body: bool = True
    collision_enabled: bool = True
    qa_report: str | None = None
    metadata_support_surface: tuple[SupportSurface, ...] = ()
    metadata_present: bool = False

    @property
    def mass(self) -> float:
        """Registry v2 alias for the legacy ``mass_kg`` field."""
        return self.mass_kg

    @property
    def hash(self) -> str | None:
        return self.asset_hash

    @property
    def support_surface(self) -> tuple[SupportSurface, ...]:
        return self.support_surfaces

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AssetRecord":
        if not isinstance(raw, dict):
            raise TypeError("asset record must be a JSON object")
        asset_id = str(raw.get("asset_id", "")).strip()
        category = str(raw.get("category", "")).strip()
        if not asset_id:
            raise ValueError("asset.asset_id is required")
        if not category:
            raise ValueError(f"asset {asset_id} requires a category")
        # v2 records use ``usd_path``/``collision_path`` and may omit a
        # precomputed bbox because it can be derived from USD.  Keep a
        # conservative placeholder for old layout callers; registry.validate
        # reports the missing bbox before such an asset can be promoted.
        bbox = _tuple3(raw.get("bbox_m", (1.0, 1.0, 1.0)), "asset.bbox_m")
        if any(size <= 0 for size in bbox):
            raise ValueError(f"asset {asset_id} has a non-positive bbox")
        source_path = raw.get("source_path") or raw.get("usd_path")
        collision_mode = str(
            raw.get("collision_mode", "authored" if source_path else "primitive")
        )
        if collision_mode not in {"primitive", "authored", "proxy_box", "none"}:
            raise ValueError(f"unsupported collision_mode: {collision_mode}")
        return cls(
            asset_id=asset_id,
            category=category,
            bbox_m=bbox,
            primitive=str(raw.get("primitive", "cube")),
            color=_tuple3(raw.get("color", (0.6, 0.6, 0.6)), "asset.color"),
            mass_kg=float(raw.get("mass_kg", raw.get("mass", 1.0))),
            friction=float(raw.get("friction", 0.5)),
            support_surfaces=tuple(
                SupportSurface.from_dict(item) for item in _support_surface_items(raw)
            ),
            source_path=source_path,
            source_type=str(raw.get("source_type", "local_usd" if source_path else "primitive")),
            collision_mode=collision_mode,
            qa_report_path=raw.get("qa_report", raw.get("qa_report_path")),
            license=raw.get("license"),
            status=str(raw.get("status", "validated")),
            tags=tuple(str(tag) for tag in raw.get("tags", [])),
            name=str(raw.get("name", raw["asset_id"])),
            asset_hash=raw.get("hash", raw.get("asset_hash")),
            usd_path=raw.get("usd_path") or source_path,
            collision_path=raw.get("collision_path"),
            collision_status=str(
                raw.get(
                    "collision_status",
                    "provided" if raw.get("collision_path") else "not_provided",
                )
            ),
            grasp_region=raw.get("grasp_region"),
            source=raw.get("source"),
            metadata_mass=(
                float(raw["mass"]) if "mass" in raw else
                (float(raw["mass_kg"]) if "mass_kg" in raw else None)
            ),
            metadata_friction=(float(raw["friction"]) if "friction" in raw else None),
            static_friction=(
                float(raw["static_friction"])
                if "static_friction" in raw
                else (float(raw["friction"]) if "friction" in raw else None)
            ),
            dynamic_friction=(
                float(raw["dynamic_friction"])
                if "dynamic_friction" in raw
                else (float(raw["friction"]) if "friction" in raw else None)
            ),
            rigid_body=bool(raw.get("rigid_body", True)),
            collision_enabled=bool(raw.get("collision_enabled", True)),
            qa_report=raw.get("qa_report", raw.get("qa_report_path")),
            metadata_support_surface=tuple(
                SupportSurface.from_dict(item)
                for item in _support_surface_items(raw)
            ),
            metadata_present=any(
                field in raw
                for field in (
                    "name", "hash", "asset_hash", "usd_path", "collision_path",
                    "mass", "support_surface", "grasp_region", "source",
                )
            ),
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
