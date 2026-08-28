"""Isaac-specific binding and read-only resolution for the P1-4A fixture.

The binding and result models in this module are deliberately free of Isaac,
Omni, USD, PhysX, and NumPy imports. The runtime inspector imports those
optional modules only from :meth:`IsaacUsdBindingInspector.inspect`, after the
caller has started ``SimulationApp``.
"""

from __future__ import annotations

import json
import math
import ntpath
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Protocol

from ..models import ArticulationJoint, SemanticState


P1_4A_REPORT_VERSION = "scene_factory.p1_4a_binding_acceptance.v1"
SEKTION_CABINET_ASSET_RELATIVE_PATH = (
    "Isaac/Props/Sektion_Cabinet/sektion_cabinet_instanceable.usd"
)
SEKTION_TOP_DRAWER_BINDING_ID = "sektion_cabinet_top_drawer"
SEKTION_TOP_DRAWER_SEMANTIC_ASSET_ID = "sektion_cabinet"
SEKTION_TOP_DRAWER_SEMANTIC_JOINT_ID = "drawer_top_joint"
SEKTION_TOP_DRAWER_SEMANTIC_REGION_ID = "drawer_handle_top"
SEKTION_TOP_DRAWER_RUNTIME_ROOT = "/World/Cabinet"
LIMIT_TOLERANCE = 1.0e-6
POSITION_TOLERANCE = 1.0e-3


def _non_empty_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _finite(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be finite")
    return result


def _range(value: Any, field_name: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{field_name} must contain exactly two numbers")
    result = (_finite(value[0], f"{field_name}[0]"), _finite(value[1], f"{field_name}[1]"))
    if result[0] > result[1]:
        raise ValueError(f"{field_name} lower bound must not exceed upper bound")
    return result


def _prim_path(value: Any, field_name: str) -> str:
    path = _non_empty_text(value, field_name)
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError(f"{field_name} must be an absolute USD prim path")
    parts = path.split("/")
    if "\\" in path or any(part in {"", ".", ".."} for part in parts[1:]):
        raise ValueError(f"{field_name} must be a normalized USD prim path")
    return path


def _asset_relative_path(value: Any) -> str:
    path = _non_empty_text(value, "asset_relative_path")
    if "://" in path or ntpath.isabs(path) or path.startswith(("/", "\\")):
        raise ValueError("asset_relative_path must be relative to ISAACSIM_ASSET_ROOT")
    if "\\" in path:
        raise ValueError("asset_relative_path must use POSIX separators")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or not parsed.parts or any(part in {"", ".", ".."} for part in parsed.parts):
        raise ValueError("asset_relative_path must not contain absolute or traversal components")
    return parsed.as_posix()


def _axis(value: Any, field_name: str = "joint_axis") -> str:
    if isinstance(value, str):
        axis = value.strip().upper()
        if axis in {"X", "Y", "Z"}:
            return axis
    elif isinstance(value, (list, tuple)) and len(value) == 3:
        vector = tuple(_finite(item, f"{field_name}[{index}]") for index, item in enumerate(value))
        norm = math.sqrt(sum(item * item for item in vector))
        if norm > 0.0 and math.isfinite(norm):
            normalized = tuple(item / norm for item in vector)
            for name, expected in (
                ("X", (1.0, 0.0, 0.0)),
                ("Y", (0.0, 1.0, 0.0)),
                ("Z", (0.0, 0.0, 1.0)),
            ):
                if all(abs(normalized[index] - expected[index]) <= LIMIT_TOLERANCE for index in range(3)):
                    return name
    raise ValueError(f"{field_name} must be normalized to X, Y, or Z")


def _under(path: str, root: str, field_name: str) -> None:
    if path != root and not path.startswith(root + "/"):
        raise ValueError(f"{field_name} must be below articulation_root_prim")


def _runtime_path(source_path: str, source_root: str, runtime_root: str) -> str:
    if source_path == source_root:
        return runtime_root
    if not source_path.startswith(source_root + "/"):
        raise ValueError(f"binding path is outside articulation root: {source_path}")
    return runtime_root + source_path[len(source_root) :]


@dataclass(frozen=True)
class IsaacBindingIssue:
    """Actionable, JSON-safe validation issue."""

    code: str
    field: str
    expected: str | None
    observed: str | None
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _non_empty_text(self.code, "issue.code"))
        object.__setattr__(self, "field", _non_empty_text(self.field, "issue.field"))
        object.__setattr__(self, "reason", _non_empty_text(self.reason, "issue.reason"))
        for name in ("expected", "observed"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, str(value))

    def to_dict(self) -> dict[str, str | None]:
        return {
            "code": self.code,
            "field": self.field,
            "expected": self.expected,
            "observed": self.observed,
            "reason": self.reason,
        }


class IsaacBindingError(ValueError):
    """Fail-closed binding error with structured diagnostic fields."""

    def __init__(
        self,
        code: str,
        field: str,
        expected: Any | None,
        observed: Any | None,
        reason: str,
    ) -> None:
        self.issue = IsaacBindingIssue(code, field, _display(expected), _display(observed), reason)
        super().__init__(
            f"{self.issue.code} ({self.issue.field}): {self.issue.reason}; "
            f"expected={self.issue.expected!r}, observed={self.issue.observed!r}"
        )


def _display(value: Any | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=True, sort_keys=True)
    return str(value)


@dataclass(frozen=True)
class IsaacSemanticFixture:
    """Small simulator-neutral P1-4 semantic fixture.

    The repository has no Sektion Cabinet registry record. This fixture reuses
    the canonical articulation and semantic-state models without adding USD
    paths or machine-local data to the canonical registry.
    """

    asset_id: str
    joint: ArticulationJoint
    region_id: str
    region_link: str
    allowed_actions: tuple[str, ...]
    states: tuple[SemanticState, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "asset_id", _non_empty_text(self.asset_id, "semantic.asset_id"))
        object.__setattr__(self, "region_id", _non_empty_text(self.region_id, "semantic.region_id"))
        object.__setattr__(self, "region_link", _non_empty_text(self.region_link, "semantic.region_link"))
        actions = tuple(_non_empty_text(item, "semantic.allowed_actions item") for item in self.allowed_actions)
        if len(actions) != len(set(actions)) or not {"grasp", "pull"}.issubset(actions):
            raise ValueError("semantic.allowed_actions must contain unique grasp and pull actions")
        object.__setattr__(self, "allowed_actions", actions)
        states = tuple(self.states)
        by_name = {state.name: state for state in states}
        if set(by_name) != {"closed", "open"}:
            raise ValueError("semantic fixture must define closed and open states")
        for state in states:
            if state.joint != self.joint.joint_id:
                raise ValueError("semantic state must reference the fixture joint")
        object.__setattr__(self, "states", states)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "joint": {
                "joint_id": self.joint.joint_id,
                "joint_type": self.joint.joint_type,
                "parent": self.joint.parent,
                "child": self.joint.child,
                "axis": list(self.joint.axis),
                "lower_limit": self.joint.lower_limit,
                "upper_limit": self.joint.upper_limit,
                "default_position": self.joint.default_position,
            },
            "region_id": self.region_id,
            "region_link": self.region_link,
            "allowed_actions": list(self.allowed_actions),
            "states": [
                {
                    "name": state.name,
                    "joint": state.joint,
                    "range": list(state.range),
                    "target_position": state.target_position,
                }
                for state in self.states
            ],
        }


def sektion_top_drawer_semantics() -> IsaacSemanticFixture:
    """Return the frozen semantic fixture for the selected top drawer."""

    joint = ArticulationJoint(
        joint_id=SEKTION_TOP_DRAWER_SEMANTIC_JOINT_ID,
        joint_type="prismatic",
        parent="sektion",
        child="drawer_top",
        axis=(1.0, 0.0, 0.0),
        lower_limit=0.0,
        upper_limit=0.40000000596,
        default_position=0.0,
    )
    return IsaacSemanticFixture(
        asset_id=SEKTION_TOP_DRAWER_SEMANTIC_ASSET_ID,
        joint=joint,
        region_id=SEKTION_TOP_DRAWER_SEMANTIC_REGION_ID,
        region_link="drawer_handle_top",
        allowed_actions=("grasp", "pull"),
        states=(
            SemanticState("closed", joint.joint_id, (0.0, 0.02)),
            SemanticState("open", joint.joint_id, (0.32, 0.38), 0.35),
        ),
    )


@dataclass(frozen=True)
class IsaacArticulationBinding:
    """Pure-Python mapping from semantic ids to verified Isaac prim paths."""

    binding_id: str
    asset_relative_path: str
    articulation_root_prim: str
    semantic_joint_id: str
    joint_prim: str
    joint_name: str
    joint_type: str
    parent_link_prim: str
    child_link_prim: str
    joint_axis: str
    expected_lower_limit: float
    expected_upper_limit: float
    expected_default_position: float
    semantic_region_id: str
    handle_link_prim: str
    handle_fixed_joint_prim: str
    handle_fixed_joint_body0: str
    handle_fixed_joint_body1: str
    handle_frame_prim: str
    semantic_asset_id: str = SEKTION_TOP_DRAWER_SEMANTIC_ASSET_ID
    closed_range: tuple[float, float] = (0.0, 0.02)
    open_range: tuple[float, float] = (0.32, 0.38)
    target_position: float = 0.35

    def __post_init__(self) -> None:
        object.__setattr__(self, "binding_id", _non_empty_text(self.binding_id, "binding_id"))
        object.__setattr__(self, "asset_relative_path", _asset_relative_path(self.asset_relative_path))
        object.__setattr__(
            self,
            "articulation_root_prim",
            _prim_path(self.articulation_root_prim, "articulation_root_prim"),
        )
        object.__setattr__(self, "semantic_asset_id", _non_empty_text(self.semantic_asset_id, "semantic_asset_id"))
        object.__setattr__(self, "semantic_joint_id", _non_empty_text(self.semantic_joint_id, "semantic_joint_id"))
        object.__setattr__(self, "semantic_region_id", _non_empty_text(self.semantic_region_id, "semantic_region_id"))
        object.__setattr__(self, "joint_name", _non_empty_text(self.joint_name, "joint_name"))
        joint_type = _non_empty_text(self.joint_type, "joint_type").lower()
        if joint_type not in {"prismatic", "revolute"}:
            raise ValueError("joint_type must be prismatic or revolute")
        object.__setattr__(self, "joint_type", joint_type)
        for name in (
            "joint_prim",
            "parent_link_prim",
            "child_link_prim",
            "handle_link_prim",
            "handle_fixed_joint_prim",
            "handle_fixed_joint_body0",
            "handle_fixed_joint_body1",
            "handle_frame_prim",
        ):
            value = _prim_path(getattr(self, name), name)
            object.__setattr__(self, name, value)
            _under(value, self.articulation_root_prim, name)
        if self.parent_link_prim == self.child_link_prim:
            raise ValueError("parent_link_prim and child_link_prim must differ")
        _under(self.handle_fixed_joint_prim, self.child_link_prim, "handle_fixed_joint_prim")
        _under(self.handle_frame_prim, self.handle_link_prim, "handle_frame_prim")
        object.__setattr__(self, "joint_axis", _axis(self.joint_axis))
        lower = _finite(self.expected_lower_limit, "expected_lower_limit")
        upper = _finite(self.expected_upper_limit, "expected_upper_limit")
        default = _finite(self.expected_default_position, "expected_default_position")
        if lower >= upper:
            raise ValueError("expected_lower_limit must be less than expected_upper_limit")
        if not lower <= default <= upper:
            raise ValueError("expected_default_position must be within expected limits")
        object.__setattr__(self, "expected_lower_limit", lower)
        object.__setattr__(self, "expected_upper_limit", upper)
        object.__setattr__(self, "expected_default_position", default)
        closed = _range(self.closed_range, "closed_range")
        opened = _range(self.open_range, "open_range")
        if closed[0] < lower or closed[1] > upper or opened[0] < lower or opened[1] > upper:
            raise ValueError("semantic ranges must be within expected joint limits")
        if max(closed[0], opened[0]) <= min(closed[1], opened[1]):
            raise ValueError("closed_range and open_range must not overlap")
        target = _finite(self.target_position, "target_position")
        if not opened[0] <= target <= opened[1]:
            raise ValueError("target_position must be within open_range")
        object.__setattr__(self, "closed_range", closed)
        object.__setattr__(self, "open_range", opened)
        object.__setattr__(self, "target_position", target)

    def resolve_asset_path(self, asset_root: str | os.PathLike[str] | None = None) -> Path:
        """Resolve the relative asset path without permitting traversal."""

        root_value = asset_root if asset_root is not None else os.environ.get("ISAACSIM_ASSET_ROOT")
        if root_value is None or not str(root_value).strip():
            raise IsaacBindingError(
                "asset_root_missing",
                "ISAACSIM_ASSET_ROOT",
                "a local asset root",
                root_value,
                "set ISAACSIM_ASSET_ROOT for the official Local Assets root",
            )
        root_text = str(root_value).strip()
        if "://" in root_text:
            raise IsaacBindingError(
                "asset_root_not_local",
                "ISAACSIM_ASSET_ROOT",
                "local filesystem path",
                root_text,
                "cloud or URI roots are not accepted by P1-4A",
            )
        root = Path(root_text).expanduser().resolve()
        if not root.is_dir():
            raise IsaacBindingError(
                "asset_root_missing",
                "ISAACSIM_ASSET_ROOT",
                "existing directory",
                root,
                "official Local Assets root does not exist",
            )
        candidate = (root / self.asset_relative_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise IsaacBindingError(
                "asset_path_traversal",
                "asset_relative_path",
                "path below ISAACSIM_ASSET_ROOT",
                self.asset_relative_path,
                "resolved asset escaped the configured root",
            ) from exc
        if candidate.suffix.lower() not in {".usd", ".usda", ".usdc"}:
            raise IsaacBindingError(
                "asset_not_usd",
                "asset_relative_path",
                "USD file",
                candidate,
                "P1-4A only accepts USD assets",
            )
        if not candidate.is_file():
            raise IsaacBindingError(
                "asset_missing",
                "asset_relative_path",
                "existing USD file",
                candidate,
                "official bound asset does not exist",
            )
        return candidate

    def runtime_paths(self, runtime_root_prim: str) -> dict[str, str]:
        runtime_root = _prim_path(runtime_root_prim, "runtime_root_prim")
        return {
            "articulation_root_prim": _runtime_path(
                self.articulation_root_prim, self.articulation_root_prim, runtime_root
            ),
            "joint_prim": _runtime_path(self.joint_prim, self.articulation_root_prim, runtime_root),
            "parent_link_prim": _runtime_path(
                self.parent_link_prim, self.articulation_root_prim, runtime_root
            ),
            "child_link_prim": _runtime_path(
                self.child_link_prim, self.articulation_root_prim, runtime_root
            ),
            "handle_link_prim": _runtime_path(
                self.handle_link_prim, self.articulation_root_prim, runtime_root
            ),
            "handle_fixed_joint_prim": _runtime_path(
                self.handle_fixed_joint_prim, self.articulation_root_prim, runtime_root
            ),
            "handle_fixed_joint_body0": _runtime_path(
                self.handle_fixed_joint_body0, self.articulation_root_prim, runtime_root
            ),
            "handle_fixed_joint_body1": _runtime_path(
                self.handle_fixed_joint_body1, self.articulation_root_prim, runtime_root
            ),
            "handle_frame_prim": _runtime_path(
                self.handle_frame_prim, self.articulation_root_prim, runtime_root
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "asset_relative_path": self.asset_relative_path,
            "articulation_root_prim": self.articulation_root_prim,
            "semantic_asset_id": self.semantic_asset_id,
            "semantic_joint_id": self.semantic_joint_id,
            "joint_prim": self.joint_prim,
            "joint_name": self.joint_name,
            "joint_type": self.joint_type,
            "parent_link_prim": self.parent_link_prim,
            "child_link_prim": self.child_link_prim,
            "joint_axis": self.joint_axis,
            "expected_lower_limit": self.expected_lower_limit,
            "expected_upper_limit": self.expected_upper_limit,
            "expected_default_position": self.expected_default_position,
            "semantic_region_id": self.semantic_region_id,
            "handle_link_prim": self.handle_link_prim,
            "handle_fixed_joint_prim": self.handle_fixed_joint_prim,
            "handle_fixed_joint_body0": self.handle_fixed_joint_body0,
            "handle_fixed_joint_body1": self.handle_fixed_joint_body1,
            "handle_frame_prim": self.handle_frame_prim,
            "closed_range": list(self.closed_range),
            "open_range": list(self.open_range),
            "target_position": self.target_position,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "IsaacArticulationBinding":
        if not isinstance(raw, Mapping):
            raise TypeError("Isaac articulation binding must be a JSON object")
        required = {
            "binding_id",
            "asset_relative_path",
            "articulation_root_prim",
            "semantic_joint_id",
            "joint_prim",
            "joint_name",
            "joint_type",
            "parent_link_prim",
            "child_link_prim",
            "joint_axis",
            "expected_lower_limit",
            "expected_upper_limit",
            "expected_default_position",
            "semantic_region_id",
            "handle_link_prim",
            "handle_fixed_joint_prim",
            "handle_fixed_joint_body0",
            "handle_fixed_joint_body1",
            "handle_frame_prim",
        }
        missing = sorted(required - set(raw))
        if missing:
            raise ValueError(f"Isaac articulation binding is missing fields: {', '.join(missing)}")
        allowed = required | {
            "semantic_asset_id",
            "closed_range",
            "open_range",
            "target_position",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError(f"Isaac articulation binding has unsupported fields: {', '.join(unknown)}")
        return cls(**dict(raw))


def sektion_top_drawer_binding() -> IsaacArticulationBinding:
    """Return the frozen Isaac binding for the official Sektion top drawer."""

    return IsaacArticulationBinding(
        binding_id=SEKTION_TOP_DRAWER_BINDING_ID,
        asset_relative_path=SEKTION_CABINET_ASSET_RELATIVE_PATH,
        articulation_root_prim="/cabinet",
        semantic_joint_id=SEKTION_TOP_DRAWER_SEMANTIC_JOINT_ID,
        joint_prim="/cabinet/sektion/drawer_top_joint",
        joint_name="drawer_top_joint",
        joint_type="prismatic",
        parent_link_prim="/cabinet/sektion",
        child_link_prim="/cabinet/drawer_top",
        joint_axis="X",
        expected_lower_limit=0.0,
        expected_upper_limit=0.40000000596,
        expected_default_position=0.0,
        semantic_region_id=SEKTION_TOP_DRAWER_SEMANTIC_REGION_ID,
        handle_link_prim="/cabinet/drawer_handle_top",
        handle_fixed_joint_prim="/cabinet/drawer_top/drawer_handle_top_joint",
        handle_fixed_joint_body0="/cabinet/drawer_top",
        handle_fixed_joint_body1="/cabinet/drawer_handle_top",
        handle_frame_prim="/cabinet/drawer_handle_top/drawer_handle_frame",
    )


SEKTION_TOP_DRAWER_BINDING = sektion_top_drawer_binding()
SEKTION_TOP_DRAWER_SEMANTICS = sektion_top_drawer_semantics()


@dataclass(frozen=True)
class IsaacArticulationBindingResolution:
    """JSON-safe observed result of resolving one Isaac binding."""

    binding_id: str
    asset_source: str | None
    runtime_asset_root_prim: str | None
    articulation_root_prim: str | None
    joint_prim: str | None
    joint_name: str | None
    runtime_joint_type: str | None
    runtime_axis: str | None
    runtime_lower_limit: float | None
    runtime_upper_limit: float | None
    runtime_default_position: float | None
    runtime_current_position: float | None
    parent_link_prim: str | None
    child_link_prim: str | None
    handle_link_prim: str | None
    handle_fixed_joint_prim: str | None
    handle_fixed_joint_body0: str | None
    handle_fixed_joint_body1: str | None
    handle_frame_prim: str | None
    handle_frame_transform: tuple[float, ...] | None
    collision_available: bool
    collision_apis: tuple[str, ...]
    collision_prim_paths: tuple[str, ...]
    semantic_asset_id: str
    semantic_joint_id: str
    semantic_region_id: str
    closed_range: tuple[float, float]
    open_range: tuple[float, float]
    target_position: float
    valid: bool
    errors: tuple[IsaacBindingIssue, ...] = ()

    def require_valid(self) -> "IsaacArticulationBindingResolution":
        if not self.valid:
            details = "; ".join(issue.reason for issue in self.errors)
            raise IsaacBindingError(
                "binding_invalid",
                "resolution",
                "valid binding",
                details,
                "; ".join(issue.code for issue in self.errors),
            )
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "asset_source": self.asset_source,
            "runtime_asset_root_prim": self.runtime_asset_root_prim,
            "articulation_root_prim": self.articulation_root_prim,
            "joint": {
                "prim": self.joint_prim,
                "name": self.joint_name,
                "type": self.runtime_joint_type,
                "axis": self.runtime_axis,
                "lower": self.runtime_lower_limit,
                "upper": self.runtime_upper_limit,
                "default": self.runtime_default_position,
                "current": self.runtime_current_position,
            },
            "parent_link_prim": self.parent_link_prim,
            "child_link_prim": self.child_link_prim,
            "handle": {
                "link": self.handle_link_prim,
                "fixed_joint": self.handle_fixed_joint_prim,
                "body0": self.handle_fixed_joint_body0,
                "body1": self.handle_fixed_joint_body1,
                "frame": self.handle_frame_prim,
                "transform": list(self.handle_frame_transform)
                if self.handle_frame_transform is not None
                else None,
            },
            "collision": {
                "available": self.collision_available,
                "apis": list(self.collision_apis),
                "prim_paths": list(self.collision_prim_paths),
            },
            "semantic": {
                "asset_id": self.semantic_asset_id,
                "joint_id": self.semantic_joint_id,
                "region_id": self.semantic_region_id,
                "closed_range": list(self.closed_range),
                "open_range": list(self.open_range),
                "target_position": self.target_position,
            },
            "valid": self.valid,
            "errors": [issue.to_dict() for issue in self.errors],
        }


class BindingInspector(Protocol):
    def inspect(self, binding: IsaacArticulationBinding, asset_source: Path) -> Mapping[str, Any]:
        ...


def _issue(
    errors: list[IsaacBindingIssue],
    code: str,
    field: str,
    expected: Any | None,
    observed: Any | None,
    reason: str,
) -> None:
    errors.append(IsaacBindingIssue(code, field, _display(expected), _display(observed), reason))


def _value(observation: Mapping[str, Any], name: str) -> Any:
    return observation.get(name)


def _compare_path(
    errors: list[IsaacBindingIssue],
    observation: Mapping[str, Any],
    name: str,
    expected: str,
) -> None:
    observed = _value(observation, name)
    if observed != expected:
        _issue(errors, f"{name}_mismatch", name, expected, observed, "resolved USD prim path does not match frozen binding")


def _compare_text(
    errors: list[IsaacBindingIssue],
    observation: Mapping[str, Any],
    name: str,
    expected: str,
    code: str,
) -> None:
    observed = _value(observation, name)
    if not isinstance(observed, str) or observed.strip().lower() != expected.lower():
        _issue(errors, code, name, expected, observed, "runtime value does not match frozen binding")


def _compare_number(
    errors: list[IsaacBindingIssue],
    observation: Mapping[str, Any],
    name: str,
    expected: float,
    code: str,
    tolerance: float = LIMIT_TOLERANCE,
) -> None:
    observed = _value(observation, name)
    try:
        actual = _finite(observed, name)
    except ValueError:
        _issue(errors, code, name, expected, observed, "runtime value is missing or non-finite")
        return
    if abs(actual - expected) > tolerance:
        _issue(errors, code, name, expected, actual, "runtime numeric value differs from frozen binding")


def validate_semantic_binding(
    binding: IsaacArticulationBinding,
    semantic: IsaacSemanticFixture | None = None,
) -> tuple[IsaacBindingIssue, ...]:
    """Cross-check the Isaac binding against simulator-neutral semantics."""

    semantic = semantic or SEKTION_TOP_DRAWER_SEMANTICS
    errors: list[IsaacBindingIssue] = []
    if binding.semantic_asset_id != semantic.asset_id:
        _issue(errors, "semantic_asset_mismatch", "semantic_asset_id", semantic.asset_id, binding.semantic_asset_id, "binding targets the wrong semantic asset")
    if binding.semantic_joint_id != semantic.joint.joint_id:
        _issue(errors, "semantic_joint_mismatch", "semantic_joint_id", semantic.joint.joint_id, binding.semantic_joint_id, "binding targets the wrong semantic joint")
    if binding.semantic_region_id != semantic.region_id:
        _issue(errors, "semantic_region_mismatch", "semantic_region_id", semantic.region_id, binding.semantic_region_id, "binding targets the wrong semantic interaction region")
    if binding.joint_type != semantic.joint.joint_type:
        _issue(errors, "semantic_joint_type_mismatch", "joint_type", semantic.joint.joint_type, binding.joint_type, "semantic and physical joint types differ")
    if binding.joint_axis != _axis(semantic.joint.axis):
        _issue(errors, "semantic_joint_axis_mismatch", "joint_axis", _axis(semantic.joint.axis), binding.joint_axis, "semantic and physical axes differ")
    for field_name, expected, observed in (
        ("parent_link_prim", semantic.joint.parent, binding.parent_link_prim.rsplit("/", 1)[-1]),
        ("child_link_prim", semantic.joint.child, binding.child_link_prim.rsplit("/", 1)[-1]),
    ):
        if expected != observed:
            _issue(errors, "semantic_link_mismatch", field_name, expected, observed, "semantic link does not match the frozen physical link")
    _compare_number(errors, {"value": binding.expected_lower_limit}, "value", semantic.joint.lower_limit, "semantic_lower_limit_mismatch")
    _compare_number(errors, {"value": binding.expected_upper_limit}, "value", semantic.joint.upper_limit, "semantic_upper_limit_mismatch")
    _compare_number(errors, {"value": binding.expected_default_position}, "value", semantic.joint.default_position, "semantic_default_mismatch")
    states = {state.name: state for state in semantic.states}
    if binding.closed_range != states["closed"].range:
        _issue(errors, "semantic_closed_range_mismatch", "closed_range", states["closed"].range, binding.closed_range, "closed semantic range differs")
    if binding.open_range != states["open"].range:
        _issue(errors, "semantic_open_range_mismatch", "open_range", states["open"].range, binding.open_range, "open semantic range differs")
    if binding.target_position != states["open"].target_position:
        _issue(errors, "semantic_target_mismatch", "target_position", states["open"].target_position, binding.target_position, "open target differs")
    if binding.handle_link_prim.rsplit("/", 1)[-1] != semantic.region_link:
        _issue(errors, "semantic_region_link_mismatch", "handle_link_prim", semantic.region_link, binding.handle_link_prim, "interaction region does not map to the bound handle")
    if not {"grasp", "pull"}.issubset(semantic.allowed_actions):
        _issue(errors, "semantic_action_mismatch", "allowed_actions", "grasp and pull", semantic.allowed_actions, "top drawer fixture lacks the required action vocabulary")
    return tuple(errors)


def _invalid_resolution(
    binding: IsaacArticulationBinding,
    errors: tuple[IsaacBindingIssue, ...],
    *,
    asset_source: str | None = None,
    runtime_asset_root_prim: str | None = None,
    observation: Mapping[str, Any] | None = None,
) -> IsaacArticulationBindingResolution:
    observed = observation or {}
    transform = observed.get("handle_frame_transform")
    if isinstance(transform, (list, tuple)):
        try:
            transform_value = tuple(float(item) for item in transform)
        except (TypeError, ValueError):
            transform_value = None
    else:
        transform_value = None
    return IsaacArticulationBindingResolution(
        binding_id=binding.binding_id,
        asset_source=asset_source,
        runtime_asset_root_prim=runtime_asset_root_prim,
        articulation_root_prim=observed.get("articulation_root_prim"),
        joint_prim=observed.get("joint_prim"),
        joint_name=observed.get("joint_name"),
        runtime_joint_type=observed.get("runtime_joint_type"),
        runtime_axis=observed.get("runtime_axis"),
        runtime_lower_limit=_safe_optional_float(observed.get("runtime_lower_limit")),
        runtime_upper_limit=_safe_optional_float(observed.get("runtime_upper_limit")),
        runtime_default_position=_safe_optional_float(observed.get("runtime_default_position")),
        runtime_current_position=_safe_optional_float(observed.get("runtime_current_position")),
        parent_link_prim=observed.get("parent_link_prim"),
        child_link_prim=observed.get("child_link_prim"),
        handle_link_prim=observed.get("handle_link_prim"),
        handle_fixed_joint_prim=observed.get("handle_fixed_joint_prim"),
        handle_fixed_joint_body0=observed.get("handle_fixed_joint_body0"),
        handle_fixed_joint_body1=observed.get("handle_fixed_joint_body1"),
        handle_frame_prim=observed.get("handle_frame_prim"),
        handle_frame_transform=transform_value,
        collision_available=bool(observed.get("collision_available", False)),
        collision_apis=tuple(str(item) for item in observed.get("collision_apis", ())),
        collision_prim_paths=tuple(str(item) for item in observed.get("collision_prim_paths", ())),
        semantic_asset_id=binding.semantic_asset_id,
        semantic_joint_id=binding.semantic_joint_id,
        semantic_region_id=binding.semantic_region_id,
        closed_range=binding.closed_range,
        open_range=binding.open_range,
        target_position=binding.target_position,
        valid=not errors,
        errors=errors,
    )


def _safe_optional_float(value: Any) -> float | None:
    try:
        return _finite(value, "runtime value")
    except ValueError:
        return None


def resolve_binding_observation(
    binding: IsaacArticulationBinding,
    observation: Mapping[str, Any],
    *,
    asset_source: str | Path | None = None,
    runtime_asset_root_prim: str = SEKTION_TOP_DRAWER_RUNTIME_ROOT,
    semantic: IsaacSemanticFixture | None = None,
) -> IsaacArticulationBindingResolution:
    """Validate a JSON-like inspection observation without Isaac imports."""

    if not isinstance(observation, Mapping):
        raise TypeError("binding observation must be a mapping")
    errors = list(validate_semantic_binding(binding, semantic))
    try:
        expected_paths = binding.runtime_paths(runtime_asset_root_prim)
    except ValueError as exc:
        _issue(errors, "runtime_root_invalid", "runtime_asset_root_prim", "absolute USD prim path", runtime_asset_root_prim, str(exc))
        expected_paths = {}
    for name, expected in expected_paths.items():
        _compare_path(errors, observation, name, expected)
    _compare_text(errors, observation, "joint_name", binding.joint_name, "joint_name_mismatch")
    _compare_text(errors, observation, "runtime_joint_type", binding.joint_type, "wrong_joint_type")
    _compare_text(errors, observation, "runtime_axis", binding.joint_axis, "wrong_joint_axis")
    _compare_number(errors, observation, "runtime_lower_limit", binding.expected_lower_limit, "joint_limits_mismatch")
    _compare_number(errors, observation, "runtime_upper_limit", binding.expected_upper_limit, "joint_limits_mismatch")
    _compare_number(
        errors,
        observation,
        "runtime_default_position",
        binding.expected_default_position,
        "joint_default_mismatch",
        POSITION_TOLERANCE,
    )
    current = _value(observation, "runtime_current_position")
    try:
        current_value = _finite(current, "runtime_current_position")
        lower = _finite(_value(observation, "runtime_lower_limit"), "runtime_lower_limit")
        upper = _finite(_value(observation, "runtime_upper_limit"), "runtime_upper_limit")
        if not lower - POSITION_TOLERANCE <= current_value <= upper + POSITION_TOLERANCE:
            _issue(errors, "joint_position_out_of_limits", "runtime_current_position", f"{lower} .. {upper}", current_value, "current runtime joint position is outside limits")
    except ValueError:
        _issue(errors, "joint_position_invalid", "runtime_current_position", "finite value within runtime limits", current, "current runtime joint position is missing or non-finite")
    transform = _value(observation, "handle_frame_transform")
    if not isinstance(transform, (list, tuple)) or len(transform) != 16:
        _issue(errors, "handle_frame_invalid", "handle_frame_transform", "16 finite matrix values", transform, "handle frame transform is missing or malformed")
    else:
        try:
            matrix = tuple(_finite(item, f"handle_frame_transform[{index}]") for index, item in enumerate(transform))
            determinant = (
                matrix[0] * (matrix[5] * matrix[10] - matrix[6] * matrix[9])
                - matrix[1] * (matrix[4] * matrix[10] - matrix[6] * matrix[8])
                + matrix[2] * (matrix[4] * matrix[9] - matrix[5] * matrix[8])
            )
            if abs(determinant) <= LIMIT_TOLERANCE:
                _issue(errors, "handle_frame_singular", "handle_frame_transform", "invertible transform", determinant, "handle frame transform is singular")
        except ValueError:
            _issue(errors, "handle_frame_invalid", "handle_frame_transform", "16 finite matrix values", transform, "handle frame transform contains a non-finite value")
    collision_apis = tuple(str(item) for item in observation.get("collision_apis", ()))
    if not bool(observation.get("collision_available")) or not any(
        item.lower() == "physicscollisionapi" for item in collision_apis
    ):
        _issue(errors, "collision_missing", "collision", "PhysicsCollisionAPI-backed handle collision", {"available": observation.get("collision_available"), "apis": collision_apis}, "handle collision was not resolved, including instance prototypes")
    return _invalid_resolution(
        binding,
        tuple(errors),
        asset_source=str(asset_source) if asset_source is not None else None,
        runtime_asset_root_prim=runtime_asset_root_prim,
        observation=observation,
    )


def resolve_isaac_articulation_binding(
    binding: IsaacArticulationBinding,
    *,
    asset_root: str | os.PathLike[str] | None = None,
    runtime_asset_root_prim: str = SEKTION_TOP_DRAWER_RUNTIME_ROOT,
    inspector: BindingInspector | None = None,
    semantic: IsaacSemanticFixture | None = None,
) -> IsaacArticulationBindingResolution:
    """Resolve and validate one binding using a real or fake inspector.

    With no inspector, ``IsaacUsdBindingInspector`` is created and performs
    lazy Isaac/USD imports. Tests can pass a narrow fake inspector and remain
    pure Python.
    """

    try:
        asset_source = binding.resolve_asset_path(asset_root)
    except IsaacBindingError as exc:
        return _invalid_resolution(binding, (exc.issue,))
    selected_inspector = inspector or IsaacUsdBindingInspector(
        runtime_asset_root_prim=runtime_asset_root_prim
    )
    try:
        observation = selected_inspector.inspect(binding, asset_source)
    except IsaacBindingError as exc:
        return _invalid_resolution(
            binding,
            (exc.issue,),
            asset_source=str(asset_source),
            runtime_asset_root_prim=runtime_asset_root_prim,
        )
    except (AttributeError, ImportError, ModuleNotFoundError, OSError, RuntimeError, TypeError, ValueError) as exc:
        issue = IsaacBindingIssue(
            "asset_load_failed",
            "asset_source",
            str(asset_source),
            type(exc).__name__,
            f"Isaac/USD inspection failed: {exc}",
        )
        return _invalid_resolution(
            binding,
            (issue,),
            asset_source=str(asset_source),
            runtime_asset_root_prim=runtime_asset_root_prim,
        )
    return resolve_binding_observation(
        binding,
        observation,
        asset_source=asset_source,
        runtime_asset_root_prim=runtime_asset_root_prim,
        semantic=semantic,
    )


def _relationship_targets(prim: Any, name: str) -> list[str]:
    relationship = prim.GetRelationship(name)
    if not relationship or not relationship.IsValid():
        return []
    return [str(target) for target in relationship.GetTargets()]


def _runtime_dof_limits(articulation: Any) -> list[tuple[float, float]]:
    get_limits = getattr(articulation, "get_dof_limits", None)
    if callable(get_limits):
        values = _flat_numeric_values(get_limits())
        if len(values) % 2:
            raise ValueError("runtime DOF limits do not contain lower/upper pairs")
        return list(zip(values[::2], values[1::2], strict=True))
    properties = getattr(articulation, "dof_properties", None)
    names = getattr(getattr(properties, "dtype", None), "names", None)
    if names and "lower" in names and "upper" in names:
        return [(float(row["lower"]), float(row["upper"])) for row in properties]
    view = getattr(articulation, "_articulation_view", None)
    get_view_limits = getattr(view, "get_dof_limits", None)
    if callable(get_view_limits):
        values = _flat_numeric_values(get_view_limits())
        if len(values) % 2:
            raise ValueError("runtime view DOF limits do not contain lower/upper pairs")
        return list(zip(values[::2], values[1::2], strict=True))
    raise AttributeError("articulation exposes no runtime DOF limit API")


def _flat_numeric_values(raw: Any) -> list[float]:
    if hasattr(raw, "tolist"):
        raw = raw.tolist()
    if isinstance(raw, (list, tuple)):
        result: list[float] = []
        for value in raw:
            result.extend(_flat_numeric_values(value))
        return result
    return [float(raw)]


def _runtime_joint_facts(articulation: Any, joint_name: str) -> tuple[float, float, float]:
    names = [str(name) for name in getattr(articulation, "dof_names", ())]
    if joint_name not in names:
        raise IsaacBindingError("joint_missing", "joint_name", joint_name, names, "runtime articulation does not expose the frozen joint")
    index = names.index(joint_name)
    positions = _flat_numeric_values(articulation.get_joint_positions())
    limits = _runtime_dof_limits(articulation)
    if index >= len(positions) or index >= len(limits):
        raise IsaacBindingError("joint_runtime_index_invalid", "joint_name", joint_name, index, "runtime position or limit arrays are shorter than dof_names")
    lower, upper = limits[index]
    return lower, upper, positions[index]


class IsaacUsdBindingInspector:
    """Inspect one referenced asset stage using Isaac/USD runtime APIs."""

    def __init__(
        self,
        *,
        stage: Any | None = None,
        runtime_asset_root_prim: str = SEKTION_TOP_DRAWER_RUNTIME_ROOT,
        runtime_articulation: Any | None = None,
    ) -> None:
        self.stage = stage
        self.runtime_asset_root_prim = _prim_path(runtime_asset_root_prim, "runtime_asset_root_prim")
        self.runtime_articulation = runtime_articulation

    def inspect(self, binding: IsaacArticulationBinding, asset_source: Path) -> Mapping[str, Any]:
        # These imports are intentionally inside the runtime call boundary.
        import omni.usd
        from pxr import Usd, UsdGeom, UsdPhysics

        stage = self.stage
        if stage is None:
            stage = omni.usd.get_context().get_stage()
        if stage is None:
            raise IsaacBindingError("stage_missing", "stage", "active USD stage", None, "Isaac returned no active USD stage")
        paths = binding.runtime_paths(self.runtime_asset_root_prim)
        root = stage.GetPrimAtPath(paths["articulation_root_prim"])
        if not root.IsValid():
            raise IsaacBindingError("articulation_root_missing", "articulation_root_prim", paths["articulation_root_prim"], None, "frozen articulation root is not present")
        if not root.HasAPI(UsdPhysics.ArticulationRootAPI):
            raise IsaacBindingError("articulation_root_missing", "articulation_root_prim", paths["articulation_root_prim"], root.GetAppliedSchemas(), "prim exists but has no ArticulationRootAPI")
        joint = stage.GetPrimAtPath(paths["joint_prim"])
        if not joint.IsValid():
            raise IsaacBindingError("joint_missing", "joint_prim", paths["joint_prim"], None, "frozen controlled joint is not present")
        if not (joint.IsA(UsdPhysics.Joint) or "Joint" in str(joint.GetTypeName())):
            raise IsaacBindingError("joint_missing", "joint_prim", "PhysicsJoint", joint.GetTypeName(), "controlled prim is not a physics joint")
        joint_type = _joint_type(joint)
        axis = str(_attribute_value(joint, "physics:axis"))
        parent_targets = _mapped_targets(joint, "physics:body0", binding, self.runtime_asset_root_prim)
        child_targets = _mapped_targets(joint, "physics:body1", binding, self.runtime_asset_root_prim)
        if len(parent_targets) != 1 or parent_targets[0] != paths["parent_link_prim"]:
            raise IsaacBindingError("parent_link_mismatch", "joint.physics:body0", paths["parent_link_prim"], parent_targets, "controlled joint parent relationship differs")
        if len(child_targets) != 1 or child_targets[0] != paths["child_link_prim"]:
            raise IsaacBindingError("child_link_mismatch", "joint.physics:body1", paths["child_link_prim"], child_targets, "controlled joint child relationship differs")
        handle_link = _require_prim(stage, paths["handle_link_prim"], "handle_link_missing")
        fixed_joint = _require_prim(stage, paths["handle_fixed_joint_prim"], "fixed_joint_missing")
        if "fixed" not in str(fixed_joint.GetTypeName()).lower():
            raise IsaacBindingError("fixed_joint_missing", "handle_fixed_joint_prim", "PhysicsFixedJoint", fixed_joint.GetTypeName(), "handle relationship is not a fixed joint")
        fixed_body0 = _mapped_targets(fixed_joint, "physics:body0", binding, self.runtime_asset_root_prim)
        fixed_body1 = _mapped_targets(fixed_joint, "physics:body1", binding, self.runtime_asset_root_prim)
        if fixed_body0 != [paths["handle_fixed_joint_body0"]]:
            raise IsaacBindingError("fixed_joint_body_mismatch", "handle_fixed_joint_body0", paths["handle_fixed_joint_body0"], fixed_body0, "handle fixed-joint body0 differs")
        if fixed_body1 != [paths["handle_fixed_joint_body1"]]:
            raise IsaacBindingError("fixed_joint_body_mismatch", "handle_fixed_joint_body1", paths["handle_fixed_joint_body1"], fixed_body1, "handle fixed-joint body1 differs")
        handle_frame = _require_prim(stage, paths["handle_frame_prim"], "handle_frame_missing")
        matrix = UsdGeom.Xformable(handle_frame).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        transform = tuple(float(matrix[row][column]) for row in range(4) for column in range(4))
        collision_paths, collision_apis = _handle_collision_primitives(
            stage, handle_link, Usd, UsdGeom, UsdPhysics
        )
        if not collision_paths:
            raise IsaacBindingError("collision_missing", "handle_link_prim", paths["handle_link_prim"], None, "no handle collision was found in the bound descendants or their instance prototypes")
        runtime_lower, runtime_upper, runtime_current = _runtime_joint_facts(
            self.runtime_articulation, binding.joint_name
        ) if self.runtime_articulation is not None else (
            _finite(_attribute_value(joint, "physics:lowerLimit"), "physics:lowerLimit"),
            _finite(_attribute_value(joint, "physics:upperLimit"), "physics:upperLimit"),
            _finite(_attribute_value(joint, "physics:jointPosition"), "physics:jointPosition"),
        )
        return {
            "articulation_root_prim": paths["articulation_root_prim"],
            "joint_prim": paths["joint_prim"],
            "joint_name": binding.joint_name,
            "runtime_joint_type": joint_type,
            "runtime_axis": axis,
            "runtime_lower_limit": runtime_lower,
            "runtime_upper_limit": runtime_upper,
            "runtime_default_position": runtime_current,
            "runtime_current_position": runtime_current,
            "parent_link_prim": paths["parent_link_prim"],
            "child_link_prim": paths["child_link_prim"],
            "handle_link_prim": paths["handle_link_prim"],
            "handle_fixed_joint_prim": paths["handle_fixed_joint_prim"],
            "handle_fixed_joint_body0": paths["handle_fixed_joint_body0"],
            "handle_fixed_joint_body1": paths["handle_fixed_joint_body1"],
            "handle_frame_prim": paths["handle_frame_prim"],
            "handle_frame_transform": transform,
            "collision_available": bool(collision_paths),
            "collision_apis": tuple(sorted(collision_apis)),
            "collision_prim_paths": tuple(sorted(collision_paths)),
        }


def _require_prim(stage: Any, path: str, code: str) -> Any:
    prim = stage.GetPrimAtPath(path)
    if not prim.IsValid():
        raise IsaacBindingError(code, path, path, None, "required frozen prim is not present")
    return prim


def _attribute_value(prim: Any, name: str) -> Any:
    attribute = prim.GetAttribute(name)
    if not attribute or not attribute.IsValid():
        return None
    return attribute.Get()


def _joint_type(prim: Any) -> str:
    type_name = str(prim.GetTypeName()).lower()
    if "prismatic" in type_name:
        return "prismatic"
    if "revolute" in type_name:
        return "revolute"
    return type_name


def _mapped_targets(
    prim: Any,
    relationship_name: str,
    binding: IsaacArticulationBinding,
    runtime_root: str,
) -> list[str]:
    targets = _relationship_targets(prim, relationship_name)
    result: list[str] = []
    for target in targets:
        try:
            result.append(_runtime_path(target, binding.articulation_root_prim, runtime_root))
        except ValueError:
            result.append(target)
    return result


def _handle_collision_primitives(
    stage: Any,
    handle_link: Any,
    Usd: Any,
    UsdGeom: Any,
    UsdPhysics: Any,
) -> tuple[set[str], set[str]]:
    collision_paths: set[str] = set()
    collision_apis: set[str] = set()
    traverse = getattr(stage, "TraverseAll", stage.Traverse)
    descendants = [prim for prim in traverse() if str(prim.GetPath()) == str(handle_link.GetPath()) or str(prim.GetPath()).startswith(str(handle_link.GetPath()) + "/")]
    prototype_paths: set[str] = set()
    for prim in descendants:
        if _has_schema(prim, "PhysicsCollisionAPI", UsdPhysics.CollisionAPI):
            collision_paths.add(str(prim.GetPath()))
            collision_apis.add("PhysicsCollisionAPI")
        if _has_schema(prim, "PhysicsMeshCollisionAPI"):
            collision_apis.add("PhysicsMeshCollisionAPI")
        instance = prim
        while instance and instance.IsValid():
            if instance.IsInstance():
                prototype = instance.GetPrototype()
                if prototype and prototype.IsValid():
                    prototype_paths.add(str(prototype.GetPath()))
                break
            instance = instance.GetParent()
    for prototype_root in prototype_paths:
        prototype = stage.GetPrimAtPath(prototype_root)
        if not prototype.IsValid():
            continue
        for prim in Usd.PrimRange.AllPrims(prototype):
            if _has_schema(prim, "PhysicsCollisionAPI", UsdPhysics.CollisionAPI):
                collision_paths.add(str(prim.GetPath()))
                collision_apis.add("PhysicsCollisionAPI")
            if _has_schema(prim, "PhysicsMeshCollisionAPI"):
                collision_apis.add("PhysicsMeshCollisionAPI")
            if prim.IsA(UsdGeom.Mesh) and _has_schema(prim, "PhysicsCollisionAPI", UsdPhysics.CollisionAPI):
                collision_apis.add("PhysicsMeshCollisionAPI")
    return collision_paths, collision_apis


def _has_schema(prim: Any, schema_name: str, schema_type: Any | None = None) -> bool:
    """Check an applied USD schema without requiring optional schema wrappers."""

    try:
        if schema_name in {str(item) for item in prim.GetAppliedSchemas()}:
            return True
    except (AttributeError, TypeError):
        pass
    if schema_type is None:
        return False
    try:
        return bool(prim.HasAPI(schema_type))
    except (AttributeError, TypeError):
        return False


__all__ = [
    "BindingInspector",
    "IsaacArticulationBinding",
    "IsaacArticulationBindingResolution",
    "IsaacBindingError",
    "IsaacBindingIssue",
    "IsaacSemanticFixture",
    "IsaacUsdBindingInspector",
    "LIMIT_TOLERANCE",
    "P1_4A_REPORT_VERSION",
    "POSITION_TOLERANCE",
    "SEKTION_CABINET_ASSET_RELATIVE_PATH",
    "SEKTION_TOP_DRAWER_BINDING",
    "SEKTION_TOP_DRAWER_BINDING_ID",
    "SEKTION_TOP_DRAWER_SEMANTICS",
    "resolve_binding_observation",
    "resolve_isaac_articulation_binding",
    "sektion_top_drawer_binding",
    "sektion_top_drawer_semantics",
    "validate_semantic_binding",
]
