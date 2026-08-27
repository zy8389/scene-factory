from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
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


def _finite_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be finite")
    return result


def _contract_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _contract_vec3(value: Any, field_name: str, *, positive: bool = False) -> Vec3:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{field_name} must contain exactly three numbers")
    result = tuple(
        _finite_float(item, f"{field_name}[{index}]") for index, item in enumerate(value)
    )
    if positive and any(item <= 0.0 for item in result):
        raise ValueError(f"{field_name} must contain positive numbers")
    return result  # type: ignore[return-value]


def _normalized_axis(value: Any, field_name: str) -> Vec3:
    axis = _contract_vec3(value, field_name)
    norm = math.sqrt(sum(item * item for item in axis))
    if norm == 0.0 or not math.isfinite(norm):
        raise ValueError(f"{field_name} must be non-zero")
    return tuple(item / norm for item in axis)  # type: ignore[return-value]


def _contract_keys(
    raw: dict[str, Any], required: set[str], allowed: set[str], label: str
) -> None:
    missing = sorted(required - set(raw))
    if missing:
        raise ValueError(f"{label} is missing required fields: {', '.join(missing)}")
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"{label} contains unsupported fields: {', '.join(unknown)}")


@dataclass(frozen=True)
class ArticulationJoint:
    joint_id: str
    joint_type: str
    parent: str
    child: str
    axis: Vec3
    lower_limit: float
    upper_limit: float
    default_position: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "joint_id", _contract_text(self.joint_id, "joint_id"))
        object.__setattr__(self, "joint_type", _contract_text(self.joint_type, "joint_type"))
        object.__setattr__(self, "parent", _contract_text(self.parent, "parent"))
        object.__setattr__(self, "child", _contract_text(self.child, "child"))
        if self.joint_type not in {"revolute", "prismatic"}:
            raise ValueError(f"unsupported joint type: {self.joint_type}")
        if self.parent == self.child:
            raise ValueError("joint parent and child must differ")
        object.__setattr__(self, "axis", _normalized_axis(self.axis, "joint.axis"))
        lower = _finite_float(self.lower_limit, "joint.lower_limit")
        upper = _finite_float(self.upper_limit, "joint.upper_limit")
        default = _finite_float(self.default_position, "joint.default_position")
        if lower >= upper:
            raise ValueError("joint.lower_limit must be less than upper_limit")
        if not lower <= default <= upper:
            raise ValueError("joint.default_position must be within joint limits")
        object.__setattr__(self, "lower_limit", lower)
        object.__setattr__(self, "upper_limit", upper)
        object.__setattr__(self, "default_position", default)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ArticulationJoint":
        if not isinstance(raw, dict):
            raise ValueError("articulation joint must be a JSON object")
        fields = {
            "joint_id", "joint_type", "parent", "child", "axis",
            "lower_limit", "upper_limit", "default_position",
        }
        _contract_keys(raw, fields, fields, "articulation joint")
        return cls(
            joint_id=raw["joint_id"],
            joint_type=raw["joint_type"],
            parent=raw["parent"],
            child=raw["child"],
            axis=raw["axis"],
            lower_limit=raw["lower_limit"],
            upper_limit=raw["upper_limit"],
            default_position=raw["default_position"],
        )


@dataclass(frozen=True)
class InteractionRegion:
    region_id: str
    kind: str
    link: str
    center: Vec3
    size: Vec3
    approach_axis: Vec3
    allowed_actions: tuple[str, ...]
    controlled_joint: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "region_id", _contract_text(self.region_id, "region_id"))
        object.__setattr__(self, "kind", _contract_text(self.kind, "region.kind"))
        object.__setattr__(self, "link", _contract_text(self.link, "region.link"))
        if self.kind not in {"handle", "grasp", "push", "pull", "button"}:
            raise ValueError(f"unsupported interaction region kind: {self.kind}")
        object.__setattr__(self, "center", _contract_vec3(self.center, "region.center"))
        object.__setattr__(self, "size", _contract_vec3(self.size, "region.size", positive=True))
        object.__setattr__(
            self,
            "approach_axis",
            _normalized_axis(self.approach_axis, "region.approach_axis"),
        )
        actions = tuple(
            _contract_text(item, "region.allowed_actions item")
            for item in self.allowed_actions
        )
        if not actions:
            raise ValueError("region.allowed_actions must not be empty")
        if len(actions) != len(set(actions)):
            raise ValueError("region.allowed_actions must not contain duplicates")
        if any(
            item not in {"grasp", "pull", "push", "rotate", "lift"}
            for item in actions
        ):
            raise ValueError("region.allowed_actions contains an unsupported action")
        object.__setattr__(self, "allowed_actions", actions)
        if self.controlled_joint is not None:
            object.__setattr__(
                self,
                "controlled_joint",
                _contract_text(self.controlled_joint, "region.controlled_joint"),
            )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "InteractionRegion":
        if not isinstance(raw, dict):
            raise ValueError("interaction region must be a JSON object")
        fields = {
            "region_id", "kind", "link", "center", "size", "approach_axis",
            "allowed_actions", "controlled_joint",
        }
        _contract_keys(raw, fields - {"controlled_joint"}, fields, "interaction region")
        if not isinstance(raw["allowed_actions"], (list, tuple)):
            raise ValueError("region.allowed_actions must be an array")
        return cls(
            region_id=raw["region_id"],
            kind=raw["kind"],
            link=raw["link"],
            center=raw["center"],
            size=raw["size"],
            approach_axis=raw["approach_axis"],
            allowed_actions=tuple(raw["allowed_actions"]),
            controlled_joint=raw.get("controlled_joint"),
        )


@dataclass(frozen=True)
class InteriorRegion:
    region_id: str
    link: str
    center: Vec3
    size: Vec3

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "region_id", _contract_text(self.region_id, "interior.region_id")
        )
        object.__setattr__(self, "link", _contract_text(self.link, "interior.link"))
        object.__setattr__(self, "center", _contract_vec3(self.center, "interior.center"))
        object.__setattr__(
            self, "size", _contract_vec3(self.size, "interior.size", positive=True)
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "InteriorRegion":
        if not isinstance(raw, dict):
            raise ValueError("interior region must be a JSON object")
        fields = {"region_id", "link", "center", "size"}
        _contract_keys(raw, fields, fields, "interior region")
        return cls(
            region_id=raw["region_id"],
            link=raw["link"],
            center=raw["center"],
            size=raw["size"],
        )


@dataclass(frozen=True)
class SemanticState:
    name: str
    joint: str
    range: Vec2
    target_position: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _contract_text(self.name, "semantic state.name"))
        object.__setattr__(self, "joint", _contract_text(self.joint, "semantic state.joint"))
        if not isinstance(self.range, (list, tuple)) or len(self.range) != 2:
            raise ValueError("semantic state.range must contain exactly two numbers")
        state_range = tuple(
            _finite_float(item, f"semantic state.range[{index}]")
            for index, item in enumerate(self.range)
        )
        if state_range[0] > state_range[1]:
            raise ValueError("semantic state.range lower bound must not exceed upper bound")
        object.__setattr__(self, "range", state_range)
        if self.target_position is not None:
            target = _finite_float(self.target_position, "semantic state.target_position")
            if not state_range[0] <= target <= state_range[1]:
                raise ValueError("semantic state.target_position must be within its range")
            object.__setattr__(self, "target_position", target)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SemanticState":
        if not isinstance(raw, dict):
            raise ValueError("semantic state must be a JSON object")
        fields = {"name", "joint", "range", "target_position"}
        _contract_keys(raw, fields - {"target_position"}, fields, "semantic state")
        return cls(
            name=raw["name"],
            joint=raw["joint"],
            range=raw["range"],
            target_position=raw.get("target_position"),
        )


def _contract_items(
    raw: dict[str, Any],
    field_name: str,
    parser: Any,
    maximum: int,
) -> tuple[Any, ...]:
    value = raw.get(field_name, [])
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"asset.{field_name} must be an array")
    if len(value) > maximum:
        raise ValueError(f"asset.{field_name} cannot contain more than {maximum} items")
    return tuple(parser(item) for item in value)


def validate_articulation_contract(
    articulations: tuple[ArticulationJoint, ...],
    interaction_regions: tuple[InteractionRegion, ...],
    interior_regions: tuple[InteriorRegion, ...],
    semantic_states: tuple[SemanticState, ...],
) -> None:
    if len(articulations) > 64:
        raise ValueError("articulation joints cannot contain more than 64 items")
    if len(interaction_regions) > 128:
        raise ValueError("interaction regions cannot contain more than 128 items")
    if len(interior_regions) > 128:
        raise ValueError("interior regions cannot contain more than 128 items")
    if len(semantic_states) > 128:
        raise ValueError("semantic states cannot contain more than 128 items")
    joint_ids = [item.joint_id for item in articulations]
    if len(joint_ids) != len(set(joint_ids)):
        raise ValueError("articulation joint IDs must be unique")
    child_links = [item.child for item in articulations]
    if len(child_links) != len(set(child_links)):
        raise ValueError("articulation joints cannot control the same child link twice")
    joints_by_id = {item.joint_id: item for item in articulations}
    graph: dict[str, list[str]] = {}
    for item in articulations:
        graph.setdefault(item.parent, []).append(item.child)
        graph.setdefault(item.child, [])
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ValueError("articulation joint graph contains a cycle")
        if node in visited:
            return
        visiting.add(node)
        for child in graph[node]:
            visit(child)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)

    region_ids = [item.region_id for item in interaction_regions]
    interior_ids = [item.region_id for item in interior_regions]
    if len(region_ids) != len(set(region_ids)):
        raise ValueError("interaction region IDs must be unique")
    if len(interior_ids) != len(set(interior_ids)):
        raise ValueError("interior region IDs must be unique")
    if set(region_ids) & set(interior_ids):
        raise ValueError("interaction and interior region IDs must be unique across the asset")
    for region in interaction_regions:
        if region.controlled_joint is not None and region.controlled_joint not in joints_by_id:
            raise ValueError(
                f"interaction region {region.region_id!r} references unknown joint "
                f"{region.controlled_joint!r}"
            )
    state_names = [item.name for item in semantic_states]
    if len(state_names) != len(set(state_names)):
        raise ValueError("semantic state names must be unique")
    states_by_joint: dict[str, list[SemanticState]] = {}
    for state in semantic_states:
        joint = joints_by_id.get(state.joint)
        if joint is None:
            raise ValueError(
                f"semantic state {state.name!r} references unknown joint {state.joint!r}"
            )
        if state.range[0] < joint.lower_limit or state.range[1] > joint.upper_limit:
            raise ValueError(f"semantic state {state.name!r} range is outside joint limits")
        states_by_joint.setdefault(state.joint, []).append(state)
    for states in states_by_joint.values():
        for index, first in enumerate(states):
            for second in states[index + 1 :]:
                if max(first.range[0], second.range[0]) <= min(
                    first.range[1], second.range[1]
                ):
                    raise ValueError(
                        f"semantic state ranges overlap for joint {first.joint!r}"
                    )


def validate_support_surface_links(
    support_surfaces: tuple[SupportSurface, ...],
    articulations: tuple[ArticulationJoint, ...],
) -> None:
    if not articulations:
        return
    links = {item.parent for item in articulations} | {item.child for item in articulations}
    for surface in support_surfaces:
        if surface.link is not None and surface.link not in links:
            raise ValueError(
                f"support surface {surface.name!r} references unknown link {surface.link!r}"
            )


def build_interaction_snapshot(
    articulations: tuple[ArticulationJoint, ...],
    interaction_regions: tuple[InteractionRegion, ...],
    interior_regions: tuple[InteriorRegion, ...],
    semantic_states: tuple[SemanticState, ...],
    requested_states: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    if not any((articulations, interaction_regions, interior_regions, semantic_states)):
        return None
    validate_articulation_contract(
        articulations, interaction_regions, interior_regions, semantic_states
    )
    state_by_name = {item.name: item for item in semantic_states}
    resolved_positions = {item.joint_id: item.default_position for item in articulations}
    assigned_joints: set[str] = set()
    resolved_states: list[dict[str, Any]] = []
    for name in requested_states:
        if semantic_states and name not in state_by_name:
            raise ValueError(f"unknown semantic state for articulated asset: {name}")
        state = state_by_name.get(name)
        if state is None:
            continue
        position = state.target_position
        if position is None:
            position = (state.range[0] + state.range[1]) / 2.0
        existing = resolved_positions.get(state.joint)
        if state.joint in assigned_joints and existing != position:
            raise ValueError(
                f"requested semantic states conflict on joint {state.joint!r}"
            )
        resolved_positions[state.joint] = position
        assigned_joints.add(state.joint)
        resolved_states.append({"name": state.name, "joint": state.joint, "position": position})
    return {
        "joints": [
            {"joint_id": item.joint_id, "position": resolved_positions[item.joint_id]}
            for item in articulations
        ],
        "regions": [
            {
                "region_id": item.region_id,
                "kind": item.kind,
                "link": item.link,
                "controlled_joint": item.controlled_joint,
            }
            for item in interaction_regions
        ],
        "states": resolved_states,
        "interior_regions": [
            {"region_id": item.region_id, "link": item.link}
            for item in interior_regions
        ],
    }


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
    link: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SupportSurface":
        return cls(
            name=str(raw["name"]),
            center=_tuple3(raw["center"], "support_surface.center"),
            size=_tuple2(raw["size"], "support_surface.size"),
            link=(
                _contract_text(raw["link"], "support_surface.link")
                if raw.get("link") is not None
                else None
            ),
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
    batch_id: str | None = None
    last_validation: str | None = None
    failure_reason: str | None = None
    physics_parameters_source: str = "project_default"
    articulations: tuple[ArticulationJoint, ...] = ()
    interaction_regions: tuple[InteractionRegion, ...] = ()
    interior_regions: tuple[InteriorRegion, ...] = ()
    semantic_states: tuple[SemanticState, ...] = ()

    def __post_init__(self) -> None:
        validate_articulation_contract(
            self.articulations,
            self.interaction_regions,
            self.interior_regions,
            self.semantic_states,
        )
        validate_support_surface_links(
            self.metadata_support_surface or self.support_surfaces,
            self.articulations,
        )

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

    @property
    def joints(self) -> tuple[ArticulationJoint, ...]:
        """Compatibility alias for callers that call articulated joints directly."""
        return self.articulations

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
        mass_value = raw.get("mass_kg")
        if mass_value is None:
            mass_value = raw.get("mass", 1.0)
        friction_value = raw.get("friction")
        if friction_value is None:
            friction_value = raw.get("dynamic_friction", 0.5)
        articulations = _contract_items(
            raw, "articulations", ArticulationJoint.from_dict, 64
        )
        support_surfaces = tuple(
            SupportSurface.from_dict(item) for item in _support_surface_items(raw)
        )
        interaction_regions = _contract_items(
            raw, "interaction_regions", InteractionRegion.from_dict, 128
        )
        interior_regions = _contract_items(
            raw, "interior_regions", InteriorRegion.from_dict, 128
        )
        semantic_states = _contract_items(
            raw, "semantic_states", SemanticState.from_dict, 128
        )
        validate_articulation_contract(
            articulations, interaction_regions, interior_regions, semantic_states
        )
        validate_support_surface_links(support_surfaces, articulations)
        return cls(
            asset_id=asset_id,
            category=category,
            bbox_m=bbox,
            primitive=str(raw.get("primitive", "cube")),
            color=_tuple3(raw.get("color", (0.6, 0.6, 0.6)), "asset.color"),
            mass_kg=float(mass_value),
            friction=float(friction_value),
            support_surfaces=support_surfaces,
            articulations=articulations,
            interaction_regions=interaction_regions,
            interior_regions=interior_regions,
            semantic_states=semantic_states,
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
                float(raw["mass"]) if raw.get("mass") is not None else
                (float(raw["mass_kg"]) if raw.get("mass_kg") is not None else None)
            ),
            metadata_friction=(float(raw["friction"]) if raw.get("friction") is not None else None),
            static_friction=(
                float(raw["static_friction"])
                if raw.get("static_friction") is not None
                else (float(raw["friction"]) if raw.get("friction") is not None else None)
            ),
            dynamic_friction=(
                float(raw["dynamic_friction"])
                if raw.get("dynamic_friction") is not None
                else (float(raw["friction"]) if raw.get("friction") is not None else None)
            ),
            rigid_body=bool(raw.get("rigid_body", True)),
            collision_enabled=bool(raw.get("collision_enabled", True)),
            qa_report=raw.get("qa_report", raw.get("qa_report_path")),
            metadata_support_surface=support_surfaces,
            metadata_present=any(
                field in raw
                for field in (
                    "name", "hash", "asset_hash", "usd_path", "collision_path",
                    "mass", "support_surface", "grasp_region", "source",
                    "articulations", "interaction_regions", "interior_regions",
                    "semantic_states",
                )
            ),
            batch_id=raw.get("batch_id"),
            last_validation=raw.get("last_validation"),
            failure_reason=raw.get("failure_reason"),
            physics_parameters_source=str(raw.get("physics_parameters_source", "project_default")),
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
    fallback_policy: str = "error"
    state: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ObjectRequest":
        fixed_pose = Pose.from_dict(raw["fixed_pose"]) if raw.get("fixed_pose") else None
        region_raw = raw.get("region_xy")
        region = None
        if region_raw is not None:
            if not isinstance(region_raw, (list, tuple)) or len(region_raw) != 4:
                raise ValueError("object.region_xy must contain [xmin, xmax, ymin, ymax]")
            region = tuple(float(value) for value in region_raw)
        state_raw = raw.get("state", [])
        if not isinstance(state_raw, (list, tuple)) or any(
            not isinstance(value, str) for value in state_raw
        ):
            raise ValueError("object.state must be an array of strings")
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
            fallback_policy=str(raw.get("fallback_policy", "error")),
            state=tuple(item.strip() for item in state_raw if item.strip()),
        )
        if result.fallback_policy not in {"error", "proxy"}:
            raise ValueError(
                f"unsupported fallback policy for object {result.object_id}: "
                f"{result.fallback_policy}"
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
    fallback_reason: str | None = None
    interactions: dict[str, Any] | None = None


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
