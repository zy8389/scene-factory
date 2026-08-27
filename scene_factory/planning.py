"""Deterministic, simulator-neutral symbolic interaction planning.

This module deliberately operates on the self-contained interaction snapshot in
an exported ``layout.json``.  It never imports a simulator, a physics backend,
or a robot controller.  The final articulation goal check is delegated to the
existing :class:`~scene_factory.tasks.TaskEvaluator`.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .tasks import TaskEvaluator


PLAN_SCHEMA_VERSION = "scene_factory.interaction_plan.v1"
PLANNER_VERSION = "deterministic_symbolic_v1"
SUPPORTED_ACTIONS = frozenset(
    {"approach", "grasp", "pull", "push", "rotate", "release"}
)
_ACTUATION_ACTIONS = frozenset({"pull", "push", "rotate"})
_PLAN_FIELDS = {
    "schema_version",
    "goal",
    "initial_state",
    "steps",
    "expected_final_state",
    "planner",
    "plan_sha256",
}
_WORLD_FIELDS = {"joint_positions", "holding", "approached_region"}
_MAX_INPUT_BYTES = 2 * 1024 * 1024
_MAX_STEPS = 256
_REGION_KINDS = frozenset({"handle", "grasp", "push", "pull", "button"})
_PLAN_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class PlanningError(ValueError):
    """A structured planner or scene-input failure."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class PlanFormatError(PlanningError):
    """A malformed or unsupported serialized interaction plan."""


class PlanValidationError(PlanningError):
    """A structurally valid plan whose symbolic preconditions fail."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _ensure_finite(value: Any, path: str = "value") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} must be finite")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _ensure_finite(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _ensure_finite(item, f"{path}[{index}]")


def _canonical_json(value: Any) -> bytes:
    _ensure_finite(value)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"value is not canonical JSON: {exc}") from exc


def _read_json(value: str | Path, label: str) -> dict[str, Any]:
    path = Path(value)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PlanningError("input_unreadable", f"cannot read {label}: {exc}") from exc
    if size > _MAX_INPUT_BYTES:
        raise PlanningError("input_too_large", f"{label} exceeds {_MAX_INPUT_BYTES} bytes")
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except UnicodeDecodeError as exc:
        raise PlanningError("invalid_utf8", f"{label} is not valid UTF-8") from exc
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise PlanningError("malformed_json", f"{label} is invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise PlanningError("invalid_input", f"{label} must contain a JSON object")
    try:
        _ensure_finite(payload)
    except ValueError as exc:
        raise PlanningError("non_finite_value", str(exc)) from exc
    return payload


def _coerce_mapping(value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        payload = dict(value)
        try:
            encoded = _canonical_json(payload)
        except ValueError as exc:
            raise PlanningError("invalid_input", f"{label} is not valid JSON: {exc}") from exc
        if len(encoded) > _MAX_INPUT_BYTES:
            raise PlanningError("input_too_large", f"{label} exceeds {_MAX_INPUT_BYTES} bytes")
        return payload
    if isinstance(value, (str, Path)):
        return _read_json(value, label)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _coerce_mapping(value.to_dict(), label)
    raise PlanningError("invalid_input", f"{label} must be a mapping or JSON path")


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _finite_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be finite")
    return result


def _strict_keys(raw: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"{label} contains unsupported fields: {', '.join(unknown)}")


@dataclass(frozen=True)
class InteractionAction:
    """One JSON-serializable symbolic interaction action."""

    step_id: int
    action: str
    object_id: str
    region_id: str | None = None
    joint_id: str | None = None
    target_position: float | None = None

    def __post_init__(self) -> None:
        if isinstance(self.step_id, bool) or not isinstance(self.step_id, int):
            raise ValueError("step_id must be an integer")
        if self.step_id < 0:
            raise ValueError("step_id must be non-negative")
        if self.action not in SUPPORTED_ACTIONS:
            raise ValueError(f"unsupported action: {self.action!r}")
        object.__setattr__(self, "object_id", _text(self.object_id, "object_id"))
        if self.region_id is not None:
            object.__setattr__(self, "region_id", _text(self.region_id, "region_id"))
        if self.joint_id is not None:
            object.__setattr__(self, "joint_id", _text(self.joint_id, "joint_id"))
        if self.target_position is not None:
            object.__setattr__(
                self,
                "target_position",
                _finite_number(self.target_position, "target_position"),
            )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "InteractionAction":
        if not isinstance(raw, Mapping):
            raise PlanFormatError("malformed_action", "interaction action must be an object")
        allowed = {"step_id", "action", "object_id", "region_id", "joint_id", "target_position"}
        try:
            _strict_keys(raw, allowed, "interaction action")
            required = {"step_id", "action", "object_id"}
            missing = sorted(required - set(raw))
            if missing:
                raise ValueError(f"interaction action is missing required fields: {', '.join(missing)}")
            action = _text(raw["action"], "action")
            if action not in SUPPORTED_ACTIONS:
                raise PlanFormatError("unsupported_action", f"unsupported action: {action!r}")
            if action in SUPPORTED_ACTIONS and (
                "region_id" not in raw or raw.get("region_id") is None
            ):
                raise ValueError(f"{action} requires region_id")
            if action in _ACTUATION_ACTIONS:
                missing = [
                    key
                    for key in ("joint_id", "target_position")
                    if key not in raw or raw.get(key) is None
                ]
                if missing:
                    raise ValueError(f"{action} is missing required fields: {', '.join(missing)}")
            elif "joint_id" in raw or "target_position" in raw:
                raise ValueError(f"{action} does not accept joint_id or target_position")
            return cls(
                step_id=raw["step_id"],
                action=action,
                object_id=raw["object_id"],
                region_id=raw.get("region_id"),
                joint_id=raw.get("joint_id"),
                target_position=raw.get("target_position"),
            )
        except PlanFormatError:
            raise
        except (TypeError, ValueError) as exc:
            raise PlanFormatError("malformed_action", str(exc)) from exc

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "step_id": self.step_id,
            "action": self.action,
            "object_id": self.object_id,
        }
        if self.region_id is not None:
            payload["region_id"] = self.region_id
        if self.joint_id is not None:
            payload["joint_id"] = self.joint_id
        if self.target_position is not None:
            payload["target_position"] = self.target_position
        return payload


@dataclass(frozen=True)
class InteractionWorldState:
    """Minimal symbolic world state used by plan validation and replay."""

    joint_positions: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    holding: tuple[str, str] | None = None
    approached_region: tuple[str, str] | None = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "InteractionWorldState":
        """Parse a serialized symbolic world state."""

        return _world_from_dict(raw, "world_state")

    def to_dict(self) -> dict[str, Any]:
        return {
            "joint_positions": {
                object_id: {
                    joint_id: float(position)
                    for joint_id, position in sorted(joints.items())
                }
                for object_id, joints in sorted(self.joint_positions.items())
            },
            "holding": _region_ref_to_dict(self.holding),
            "approached_region": _region_ref_to_dict(self.approached_region),
        }


def _region_ref_to_dict(value: tuple[str, str] | None) -> dict[str, str] | None:
    if value is None:
        return None
    return {"object_id": value[0], "region_id": value[1]}


def _parse_region_ref(value: Any, field_name: str) -> tuple[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be null or an object")
    _strict_keys(value, {"object_id", "region_id"}, field_name)
    if set(value) != {"object_id", "region_id"}:
        raise ValueError(f"{field_name} must contain object_id and region_id")
    return (_text(value["object_id"], f"{field_name}.object_id"), _text(value["region_id"], f"{field_name}.region_id"))


def _world_from_dict(raw: Mapping[str, Any], label: str) -> InteractionWorldState:
    if not isinstance(raw, Mapping):
        raise PlanFormatError("malformed_world_state", f"{label} must be an object")
    try:
        _strict_keys(raw, _WORLD_FIELDS, label)
        if set(raw) != _WORLD_FIELDS:
            raise ValueError(f"{label} must contain exactly: {', '.join(sorted(_WORLD_FIELDS))}")
        positions_raw = raw["joint_positions"]
        if not isinstance(positions_raw, Mapping):
            raise ValueError(f"{label}.joint_positions must be an object")
        positions: dict[str, dict[str, float]] = {}
        for object_id, joints_raw in positions_raw.items():
            object_key = _text(object_id, f"{label}.joint_positions object")
            if not isinstance(joints_raw, Mapping):
                raise ValueError(f"{label}.joint_positions.{object_key} must be an object")
            joints: dict[str, float] = {}
            for joint_id, position in joints_raw.items():
                joint_key = _text(joint_id, f"{label}.joint_positions joint")
                joints[joint_key] = _finite_number(position, f"{label}.{object_key}.{joint_key}")
            positions[object_key] = joints
        return InteractionWorldState(
            joint_positions=positions,
            holding=_parse_region_ref(raw["holding"], f"{label}.holding"),
            approached_region=_parse_region_ref(
                raw["approached_region"], f"{label}.approached_region"
            ),
        )
    except PlanFormatError:
        raise
    except (TypeError, ValueError) as exc:
        raise PlanFormatError("malformed_world_state", str(exc)) from exc


def _goal_dict(goal: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(goal, Mapping):
        raise PlanFormatError("malformed_goal", "goal must be an object")
    _strict_keys(goal, {"predicate", "object_id", "state", "joint_id"}, "goal")
    required = {"predicate", "object_id", "state"}
    missing = sorted(required - set(goal))
    if missing:
        raise PlanFormatError("malformed_goal", f"goal is missing required fields: {', '.join(missing)}")
    result = {
        "predicate": _text(goal["predicate"], "goal.predicate"),
        "object_id": _text(goal["object_id"], "goal.object_id"),
        "state": _text(goal["state"], "goal.state"),
    }
    if goal.get("joint_id") is not None:
        result["joint_id"] = _text(goal["joint_id"], "goal.joint_id")
    return result


def _plan_semantics(
    schema_version: str,
    goal: Mapping[str, Any],
    initial_state: Mapping[str, Any],
    steps: Sequence[InteractionAction],
    expected_final_state: Mapping[str, Any],
    planner: str,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "goal": dict(goal),
        "initial_state": dict(initial_state),
        "steps": [step.to_dict() for step in steps],
        "expected_final_state": dict(expected_final_state),
        "planner": planner,
    }


def _plan_hash(
    schema_version: str,
    goal: Mapping[str, Any],
    initial_state: Mapping[str, Any],
    steps: Sequence[InteractionAction],
    expected_final_state: Mapping[str, Any],
    planner: str,
) -> str:
    return hashlib.sha256(
        _canonical_json(
            _plan_semantics(
                schema_version,
                goal,
                initial_state,
                steps,
                expected_final_state,
                planner,
            )
        )
    ).hexdigest()


@dataclass(frozen=True)
class InteractionPlan:
    """Versioned symbolic plan with a hash over semantic content only."""

    schema_version: str
    goal: Mapping[str, Any]
    initial_state: Mapping[str, Any]
    steps: tuple[InteractionAction, ...]
    expected_final_state: Mapping[str, Any]
    planner: str = PLANNER_VERSION
    plan_sha256: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != PLAN_SCHEMA_VERSION:
            raise ValueError(f"unsupported plan schema version: {self.schema_version!r}")
        if not isinstance(self.goal, Mapping):
            raise ValueError("plan.goal must be an object")
        normalized_goal = _goal_dict(self.goal)
        if not isinstance(self.initial_state, Mapping) or not isinstance(
            self.expected_final_state, Mapping
        ):
            raise ValueError("plan states must be objects")
        if not isinstance(self.steps, tuple):
            object.__setattr__(self, "steps", tuple(self.steps))
        if len(self.steps) > _MAX_STEPS:
            raise ValueError(f"plan cannot contain more than {_MAX_STEPS} steps")
        for expected_id, step in enumerate(self.steps):
            if not isinstance(step, InteractionAction):
                raise ValueError("plan.steps must contain InteractionAction values")
            if step.step_id != expected_id:
                raise ValueError("plan step IDs must be contiguous from zero")
        initial = _world_from_dict(self.initial_state, "plan.initial_state")
        expected = _world_from_dict(self.expected_final_state, "plan.expected_final_state")
        object.__setattr__(self, "goal", normalized_goal)
        object.__setattr__(self, "initial_state", initial.to_dict())
        object.__setattr__(self, "expected_final_state", expected.to_dict())
        if not isinstance(self.planner, str) or not self.planner.strip():
            raise ValueError("plan.planner must be a non-empty string")
        calculated = _plan_hash(
            self.schema_version,
            normalized_goal,
            initial.to_dict(),
            self.steps,
            expected.to_dict(),
            self.planner,
        )
        if self.plan_sha256 and self.plan_sha256 != calculated:
            raise ValueError("plan_sha256 does not match canonical plan content")
        object.__setattr__(self, "plan_sha256", calculated)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "InteractionPlan":
        if not isinstance(raw, Mapping):
            raise PlanFormatError("malformed_plan", "interaction plan must be an object")
        try:
            _strict_keys(raw, _PLAN_FIELDS, "interaction plan")
            if set(raw) != _PLAN_FIELDS:
                raise ValueError(
                    "interaction plan must contain exactly: "
                    + ", ".join(sorted(_PLAN_FIELDS))
                )
            if raw["schema_version"] != PLAN_SCHEMA_VERSION:
                raise PlanFormatError(
                    "unsupported_schema_version",
                    f"unsupported plan schema version: {raw['schema_version']!r}",
                )
            steps_raw = raw["steps"]
            if not isinstance(steps_raw, list):
                raise ValueError("interaction plan.steps must be an array")
            if len(steps_raw) > _MAX_STEPS:
                raise ValueError(f"plan cannot contain more than {_MAX_STEPS} steps")
            plan_sha256 = raw["plan_sha256"]
            if not isinstance(plan_sha256, str) or not _PLAN_HASH_RE.fullmatch(plan_sha256):
                raise ValueError("plan_sha256 must be a 64-character lowercase SHA-256 string")
            try:
                steps = tuple(InteractionAction.from_dict(item) for item in steps_raw)
            except PlanFormatError as exc:
                if exc.reason == "malformed_action":
                    raise PlanFormatError("malformed_plan", str(exc)) from exc
                raise
            return cls(
                schema_version=raw["schema_version"],
                goal=raw["goal"],
                initial_state=raw["initial_state"],
                steps=steps,
                expected_final_state=raw["expected_final_state"],
                planner=raw["planner"],
                plan_sha256=plan_sha256,
            )
        except PlanFormatError:
            raise
        except (TypeError, ValueError) as exc:
            reason = "non_contiguous_step_ids" if "step IDs" in str(exc) else "malformed_plan"
            raise PlanFormatError(reason, str(exc)) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "goal": dict(self.goal),
            "initial_state": dict(self.initial_state),
            "steps": [step.to_dict() for step in self.steps],
            "expected_final_state": dict(self.expected_final_state),
            "planner": self.planner,
            "plan_sha256": self.plan_sha256,
        }


@dataclass(frozen=True)
class InteractionPlanningResult:
    """Structured planner, validator, or replay result."""

    result: str
    valid: bool
    failure_reason: str | None = None
    message: str | None = None
    plan: InteractionPlan | None = None
    goal_already_satisfied: bool = False
    trace: tuple[dict[str, Any], ...] = ()
    final_state: dict[str, Any] | None = None
    task_oracle: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "valid": self.valid,
            "failure_reason": self.failure_reason,
            "message": self.message,
            "goal_already_satisfied": self.goal_already_satisfied,
            "plan": self.plan.to_dict() if self.plan is not None else None,
            "trace": list(self.trace),
            "final_state": self.final_state,
            "task_oracle": self.task_oracle,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)


@dataclass(frozen=True)
class _Joint:
    object_id: str
    joint_id: str
    joint_type: str
    position: float
    lower_limit: float
    upper_limit: float


@dataclass(frozen=True)
class _Region:
    object_id: str
    region_id: str
    kind: str
    controlled_joint: str | None
    allowed_actions: frozenset[str]


@dataclass(frozen=True)
class _SemanticState:
    object_id: str
    name: str
    joint_id: str
    state_range: tuple[float, float]
    target_position: float


@dataclass(frozen=True)
class _SceneModel:
    object_ids: frozenset[str]
    joints: Mapping[tuple[str, str], _Joint]
    regions: Mapping[tuple[str, str], _Region]
    states: tuple[_SemanticState, ...]
    task: Mapping[str, Any]

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "_SceneModel":
        objects_raw = payload.get("objects")
        if not isinstance(objects_raw, list):
            raise PlanningError("invalid_scene", "scene.objects must be an array")
        object_ids: set[str] = set()
        joints: dict[tuple[str, str], _Joint] = {}
        regions: dict[tuple[str, str], _Region] = {}
        states: list[_SemanticState] = []
        try:
            for object_raw in objects_raw:
                if not isinstance(object_raw, Mapping):
                    raise ValueError("scene object must be an object")
                object_id = _text(object_raw.get("object_id"), "scene object_id")
                if object_id in object_ids:
                    raise ValueError(f"duplicate scene object_id: {object_id}")
                object_ids.add(object_id)
                interactions = object_raw.get("interactions")
                if interactions is None:
                    continue
                if not isinstance(interactions, Mapping):
                    raise ValueError(f"object {object_id} interactions must be an object")
                joints_raw = interactions.get("joints", [])
                if not isinstance(joints_raw, list):
                    raise ValueError(f"object {object_id} interactions.joints must be an array")
                for joint_raw in joints_raw:
                    if not isinstance(joint_raw, Mapping):
                        raise ValueError("interaction joint must be an object")
                    joint_id = _text(joint_raw.get("joint_id"), "joint_id")
                    key = (object_id, joint_id)
                    if key in joints:
                        raise ValueError(f"duplicate joint: {object_id}.{joint_id}")
                    joint_type = str(joint_raw.get("joint_type", ""))
                    if joint_type not in {"prismatic", "revolute"}:
                        raise ValueError(f"joint {object_id}.{joint_id} has an invalid joint_type")
                    joints[key] = _Joint(
                        object_id,
                        joint_id,
                        joint_type,
                        _finite_number(joint_raw.get("position"), f"{joint_id}.position"),
                        _finite_number(joint_raw.get("lower_limit"), f"{joint_id}.lower_limit"),
                        _finite_number(joint_raw.get("upper_limit"), f"{joint_id}.upper_limit"),
                    )
                    if joints[key].lower_limit >= joints[key].upper_limit:
                        raise ValueError(f"joint {object_id}.{joint_id} has invalid limits")
                regions_raw = interactions.get("regions", [])
                if not isinstance(regions_raw, list):
                    raise ValueError(f"object {object_id} interactions.regions must be an array")
                for region_raw in regions_raw:
                    if not isinstance(region_raw, Mapping):
                        raise ValueError("interaction region must be an object")
                    region_id = _text(region_raw.get("region_id"), "region_id")
                    key = (object_id, region_id)
                    if key in regions:
                        raise ValueError(f"duplicate interaction region: {object_id}.{region_id}")
                    kind = _text(region_raw.get("kind"), "region.kind")
                    if kind not in _REGION_KINDS:
                        raise ValueError(f"region {object_id}.{region_id} has an invalid kind")
                    actions_raw = region_raw.get("allowed_actions")
                    if not isinstance(actions_raw, list) or not actions_raw:
                        raise ValueError(f"region {object_id}.{region_id} has no allowed_actions")
                    actions = frozenset(_text(item, "allowed_actions item") for item in actions_raw)
                    if not actions <= {"grasp", "pull", "push", "rotate", "lift"}:
                        raise ValueError(f"region {object_id}.{region_id} has unsupported actions")
                    regions[key] = _Region(
                        object_id,
                        region_id,
                        kind,
                        (
                            _text(region_raw["controlled_joint"], "controlled_joint")
                            if region_raw.get("controlled_joint") is not None
                            else None
                        ),
                        actions,
                    )
                states_raw = interactions.get("semantic_states", [])
                if not isinstance(states_raw, list):
                    raise ValueError(f"object {object_id} interactions.semantic_states must be an array")
                for state_raw in states_raw:
                    if not isinstance(state_raw, Mapping):
                        raise ValueError("semantic state snapshot must be an object")
                    name = _text(state_raw.get("name"), "semantic state.name")
                    joint_id = _text(state_raw.get("joint"), "semantic state.joint")
                    range_raw = state_raw.get("range")
                    if not isinstance(range_raw, list) or len(range_raw) != 2:
                        raise ValueError(f"semantic state {name} has an invalid range")
                    state_range = (
                        _finite_number(range_raw[0], f"semantic state {name}.range[0]"),
                        _finite_number(range_raw[1], f"semantic state {name}.range[1]"),
                    )
                    if state_range[0] > state_range[1]:
                        raise ValueError(f"semantic state {name} has an inverted range")
                    target = state_raw.get("target_position")
                    if target is None:
                        target = (state_range[0] + state_range[1]) / 2.0
                    state = _SemanticState(
                        object_id,
                        name,
                        joint_id,
                        state_range,
                        _finite_number(target, f"semantic state {name}.target_position"),
                    )
                    if not state_range[0] <= state.target_position <= state_range[1]:
                        raise ValueError(f"semantic state {name} target is outside its range")
                    if any(
                        item.object_id == object_id
                        and item.name == name
                        and item.joint_id == joint_id
                        for item in states
                    ):
                        raise ValueError(f"duplicate semantic state: {object_id}.{name}.{joint_id}")
                    states.append(state)
        except PlanningError:
            raise
        except (TypeError, ValueError) as exc:
            raise PlanningError("invalid_scene", str(exc)) from exc

        for joint in joints.values():
            if joint.position < joint.lower_limit or joint.position > joint.upper_limit:
                raise PlanningError(
                    "invalid_initial_state",
                    f"initial position is outside limits: {joint.object_id}.{joint.joint_id}",
                )
        joint_keys = set(joints)
        for region in regions.values():
            if region.controlled_joint is not None and (
                region.object_id,
                region.controlled_joint,
            ) not in joint_keys:
                raise PlanningError(
                    "invalid_scene",
                    f"region {region.object_id}.{region.region_id} references unknown joint",
                )
        for state in states:
            joint = joints.get((state.object_id, state.joint_id))
            if joint is None:
                raise PlanningError(
                    "invalid_scene",
                    f"semantic state {state.object_id}.{state.name} references unknown joint",
                )
            if state.state_range[0] < joint.lower_limit or state.state_range[1] > joint.upper_limit:
                raise PlanningError(
                    "invalid_scene",
                    f"semantic state {state.object_id}.{state.name} exceeds joint limits",
                )
        task = payload.get("task", {})
        if not isinstance(task, Mapping):
            raise PlanningError("invalid_scene", "scene.task must be an object")
        return cls(frozenset(object_ids), joints, regions, tuple(states), dict(task))

    def initial_world(self) -> InteractionWorldState:
        positions: dict[str, dict[str, float]] = {}
        for (object_id, joint_id), joint in sorted(self.joints.items()):
            positions.setdefault(object_id, {})[joint_id] = joint.position
        return InteractionWorldState(positions)


@dataclass(frozen=True)
class _ResolvedGoal:
    goal: dict[str, Any]
    state: _SemanticState
    joint: _Joint


def _task_goal(task: Mapping[str, Any]) -> Mapping[str, Any] | None:
    success = task.get("success")
    if isinstance(success, Mapping) and success.get("predicate") == "articulation_state":
        return {
            "predicate": success.get("predicate"),
            "object_id": success.get("object_id")
            or task.get("object_id")
            or task.get("target_object")
            or success.get("subject"),
            "state": success.get("state"),
            "joint_id": success.get("joint_id") or success.get("joint"),
        }
    return None


def _resolve_goal(
    scene: _SceneModel,
    goal: Mapping[str, Any] | None,
    object_id: str | None = None,
    state_name: str | None = None,
    joint_id: str | None = None,
) -> _ResolvedGoal:
    if goal is None:
        goal = _task_goal(scene.task)
    if goal is None:
        if object_id is None or state_name is None:
            raise PlanningError(
                "goal_not_supported",
                "an articulation_state goal or object/state arguments are required",
            )
        goal = {
            "predicate": "articulation_state",
            "object_id": object_id,
            "state": state_name,
            "joint_id": joint_id,
        }
    elif object_id is not None or state_name is not None or joint_id is not None:
        goal = {
            "predicate": "articulation_state",
            "object_id": object_id,
            "state": state_name,
            "joint_id": joint_id,
        }
    try:
        if isinstance(goal, Mapping) and goal.get("predicate") == "articulation_state":
            # TaskEvaluator success objects use ``joint`` and may carry range
            # metadata. Normalize those input aliases before the strict plan
            # goal contract is applied.
            goal = {
                "predicate": goal.get("predicate"),
                "object_id": goal.get("object_id") or goal.get("subject"),
                "state": goal.get("state"),
                "joint_id": goal.get("joint_id") or goal.get("joint"),
            }
        normalized = _goal_dict(goal)
    except PlanFormatError as exc:
        raise PlanningError(exc.reason, str(exc)) from exc
    if normalized["predicate"] != "articulation_state":
        raise PlanningError(
            "goal_not_supported",
            f"unsupported goal predicate: {normalized['predicate']!r}",
        )
    target_object = normalized["object_id"]
    if target_object not in scene.object_ids:
        raise PlanningError("unknown_object", f"unknown object: {target_object!r}")
    requested_joint = normalized.get("joint_id")
    if requested_joint is not None and (target_object, requested_joint) not in scene.joints:
        raise PlanningError("unknown_joint", f"unknown joint: {target_object}.{requested_joint}")
    candidates = [
        item
        for item in scene.states
        if item.object_id == target_object and item.name == normalized["state"]
    ]
    if requested_joint is not None:
        candidates = [item for item in candidates if item.joint_id == requested_joint]
    if not candidates:
        raise PlanningError(
            "unknown_state",
            f"unknown state {normalized['state']!r} for object {target_object!r}",
        )
    if len(candidates) > 1:
        raise PlanningError(
            "ambiguous_state",
            f"state {normalized['state']!r} maps to multiple joints for {target_object!r}",
        )
    semantic = candidates[0]
    joint = scene.joints[(semantic.object_id, semantic.joint_id)]
    normalized["joint_id"] = semantic.joint_id
    return _ResolvedGoal(normalized, semantic, joint)


def _task_for_goal(goal: _ResolvedGoal) -> dict[str, Any]:
    return {
        "success": {
            "predicate": "articulation_state",
            "object_id": goal.state.object_id,
            "state": goal.state.name,
            "joint": goal.state.joint_id,
            "range": list(goal.state.state_range),
        }
    }


def _task_oracle(scene: _SceneModel, goal: _ResolvedGoal, world: InteractionWorldState) -> dict[str, Any]:
    evaluator = TaskEvaluator(_task_for_goal(goal), {})
    return evaluator.status({}, {"articulation_positions": world.joint_positions})


def _failure(reason: str, message: str, **kwargs: Any) -> InteractionPlanningResult:
    return InteractionPlanningResult(
        result="failed",
        valid=False,
        failure_reason=reason,
        message=message,
        **kwargs,
    )


def _success(
    plan: InteractionPlan,
    *,
    goal_already_satisfied: bool = False,
    trace: tuple[dict[str, Any], ...] = (),
    final_state: dict[str, Any] | None = None,
    task_oracle: dict[str, Any] | None = None,
) -> InteractionPlanningResult:
    return InteractionPlanningResult(
        result="passed",
        valid=True,
        plan=plan,
        goal_already_satisfied=goal_already_satisfied,
        trace=trace,
        final_state=final_state,
        task_oracle=task_oracle,
    )


def _select_region(scene: _SceneModel, goal: _ResolvedGoal, action: str) -> _Region:
    candidates = [
        region
        for region in scene.regions.values()
        if region.object_id == goal.state.object_id
        and region.controlled_joint == goal.state.joint_id
    ]
    if not candidates:
        raise PlanningError(
            "no_interaction_region",
            f"no interaction region controls {goal.state.object_id}.{goal.state.joint_id}",
        )
    candidates = [region for region in candidates if action in region.allowed_actions]
    if not candidates:
        raise PlanningError(
            "no_compatible_action",
            f"no region permits {action} for {goal.state.object_id}.{goal.state.joint_id}",
        )
    candidates.sort(
        key=lambda region: (
            0 if region.kind == "handle" else 1,
            0 if "grasp" in region.allowed_actions else 1,
            region.region_id,
        )
    )
    return candidates[0]


def _initial_plan_state(scene: _SceneModel) -> dict[str, Any]:
    return scene.initial_world().to_dict()


def plan_interaction(
    scene: Mapping[str, Any] | str | Path | Any,
    goal: Mapping[str, Any] | None = None,
    *,
    object_id: str | None = None,
    state: str | None = None,
    joint_id: str | None = None,
) -> InteractionPlanningResult:
    """Generate one deterministic symbolic plan for an articulation goal."""

    try:
        scene_model = _SceneModel.from_payload(_coerce_mapping(scene, "scene"))
        resolved = _resolve_goal(scene_model, goal, object_id, state, joint_id)
        initial_world = scene_model.initial_world()
        initial_oracle = _task_oracle(scene_model, resolved, initial_world)
        initial_state = initial_world.to_dict()
        if initial_oracle.get("task_success") is True:
            plan = InteractionPlan(
                PLAN_SCHEMA_VERSION,
                resolved.goal,
                initial_state,
                (),
                initial_state,
                PLANNER_VERSION,
            )
            return _success(
                plan,
                goal_already_satisfied=True,
                final_state=initial_state,
                task_oracle=initial_oracle,
            )

        current = initial_world.joint_positions[resolved.state.object_id][resolved.state.joint_id]
        target = resolved.state.target_position
        if resolved.joint.joint_type == "revolute":
            action_name = "rotate"
        elif target > current:
            action_name = "pull"
        elif target < current:
            action_name = "push"
        else:
            raise PlanningError("invalid_initial_state", "target position does not advance the current state")
        region = _select_region(scene_model, resolved, action_name)
        needs_grasp = (
            "grasp" in region.allowed_actions
            and region.kind not in {"push", "button"}
        )
        actions: list[InteractionAction] = [
            InteractionAction(0, "approach", resolved.state.object_id, region.region_id)
        ]
        if needs_grasp:
            actions.append(InteractionAction(1, "grasp", resolved.state.object_id, region.region_id))
        actions.append(
            InteractionAction(
                len(actions),
                action_name,
                resolved.state.object_id,
                region.region_id,
                resolved.state.joint_id,
                target,
            )
        )
        if needs_grasp:
            actions.append(
                InteractionAction(
                    len(actions), "release", resolved.state.object_id, region.region_id
                )
            )
        expected_world, trace = _simulate_steps(
            scene_model, resolved, initial_world, tuple(actions), capture_trace=True
        )
        expected_state = expected_world.to_dict()
        plan = InteractionPlan(
            PLAN_SCHEMA_VERSION,
            resolved.goal,
            initial_state,
            tuple(actions),
            expected_state,
            PLANNER_VERSION,
        )
        oracle = _task_oracle(scene_model, resolved, expected_world)
        if oracle.get("task_success") is not True:
            return _failure("goal_not_satisfied", "generated plan does not satisfy TaskEvaluator")
        return _success(
            plan,
            trace=trace,
            final_state=expected_state,
            task_oracle=oracle,
        )
    except PlanningError as exc:
        return _failure(exc.reason, str(exc))
    except (TypeError, ValueError, OSError) as exc:
        return _failure("invalid_input", str(exc))


def _world_matches(left: InteractionWorldState, right: InteractionWorldState) -> bool:
    return left.to_dict() == right.to_dict()


def _require_region(scene: _SceneModel, action: InteractionAction) -> _Region:
    if action.region_id is None:
        raise PlanValidationError("unknown_region", "action requires region_id")
    key = (action.object_id, action.region_id)
    region = scene.regions.get(key)
    if region is None:
        other = next(
            (item for item in scene.regions.values() if item.region_id == action.region_id),
            None,
        )
        if other is not None:
            raise PlanValidationError(
                "region_object_mismatch",
                f"region {action.region_id!r} belongs to {other.object_id!r}",
            )
        raise PlanValidationError("unknown_region", f"unknown region: {action.region_id!r}")
    return region


def _apply_action(
    scene: _SceneModel,
    goal: _ResolvedGoal | None,
    world: InteractionWorldState,
    action: InteractionAction,
) -> InteractionWorldState:
    if action.object_id not in scene.object_ids:
        raise PlanValidationError("unknown_object", f"unknown object: {action.object_id!r}")
    region = _require_region(scene, action)
    if action.action == "approach":
        return InteractionWorldState(world.joint_positions, None, (action.object_id, region.region_id))
    if action.action == "grasp":
        if "grasp" not in region.allowed_actions:
            raise PlanValidationError("no_compatible_action", "region does not allow grasp")
        if world.approached_region != (action.object_id, region.region_id):
            raise PlanValidationError("grasp_before_approach", "grasp requires approach to the same region")
        if world.holding is not None:
            raise PlanValidationError("holding_conflict", "cannot grasp while already holding a region")
        return InteractionWorldState(
            world.joint_positions,
            (action.object_id, region.region_id),
            world.approached_region,
        )
    if action.action == "release":
        if world.holding != (action.object_id, region.region_id):
            if world.holding is None:
                raise PlanValidationError("release_without_holding", "release requires holding the same region")
            raise PlanValidationError("release_region_mismatch", "release region does not match holding region")
        return InteractionWorldState(world.joint_positions, None, world.approached_region)

    if action.action not in _ACTUATION_ACTIONS:
        raise PlanValidationError("unsupported_action", f"unsupported action: {action.action!r}")
    if action.action not in region.allowed_actions:
        raise PlanValidationError(
            "no_compatible_action",
            f"region does not allow {action.action}",
        )
    if region.controlled_joint != action.joint_id:
        raise PlanValidationError("wrong_controlled_joint", "action joint does not match region controlled_joint")
    if goal is not None and (
        action.object_id != goal.state.object_id or action.joint_id != goal.state.joint_id
    ):
        raise PlanValidationError("unknown_joint", "single-goal plan must actuate the goal joint")
    joint = scene.joints.get((action.object_id, action.joint_id or ""))
    if joint is None:
        raise PlanValidationError("unknown_joint", f"unknown joint: {action.object_id}.{action.joint_id}")
    if action.target_position is None:
        raise PlanValidationError("malformed_action", "actuation requires target_position")
    target = action.target_position
    if not joint.lower_limit <= target <= joint.upper_limit:
        raise PlanValidationError("target_outside_limits", "target_position is outside joint limits")
    if goal is not None and not goal.state.state_range[0] <= target <= goal.state.state_range[1]:
        raise PlanValidationError("target_outside_semantic_range", "target_position is outside the goal state range")
    current = world.joint_positions[action.object_id][joint.joint_id]
    if action.action == "pull" and (joint.joint_type != "prismatic" or target <= current):
        raise PlanValidationError("invalid_direction", "pull must increase a prismatic joint position")
    if action.action == "push" and (joint.joint_type != "prismatic" or target >= current):
        raise PlanValidationError("invalid_direction", "push must decrease a prismatic joint position")
    if action.action == "rotate" and joint.joint_type != "revolute":
        raise PlanValidationError("invalid_action_for_joint", "rotate requires a revolute joint")
    requires_holding = action.action in {"pull", "rotate"} or (
        action.action == "push"
        and ("grasp" in region.allowed_actions or region.kind in {"handle", "grasp", "pull"})
    )
    if requires_holding and world.holding != (action.object_id, region.region_id):
        raise PlanValidationError("actuation_before_grasp", f"{action.action} requires holding the same region")
    updated = {
        object_id: dict(joints)
        for object_id, joints in world.joint_positions.items()
    }
    updated[action.object_id][joint.joint_id] = target
    return InteractionWorldState(updated, world.holding, world.approached_region)


def apply_symbolic_interaction_action(
    scene: Mapping[str, Any] | str | Path | Any | _SceneModel,
    world: InteractionWorldState,
    action: InteractionAction,
    *,
    goal: Mapping[str, Any] | _ResolvedGoal | None = None,
) -> InteractionWorldState:
    """Apply one shared symbolic transition for replay and execution adapters.

    A goal is supplied by plan validation/replay so semantic state-range checks
    remain active.  A dry-run executor may omit it because its orchestrator
    validates the complete plan before dispatching any command.
    """

    scene_model = scene if isinstance(scene, _SceneModel) else _SceneModel.from_payload(
        _coerce_mapping(scene, "scene")
    )
    if not isinstance(world, InteractionWorldState):
        raise PlanValidationError("malformed_world_state", "world must be an InteractionWorldState")
    if goal is None or isinstance(goal, _ResolvedGoal):
        resolved_goal = goal
    else:
        resolved_goal = _resolve_goal(scene_model, goal)
    return _apply_action(scene_model, resolved_goal, world, action)


def _simulate_steps(
    scene: _SceneModel,
    goal: _ResolvedGoal,
    initial: InteractionWorldState,
    steps: Sequence[InteractionAction],
    *,
    capture_trace: bool,
) -> tuple[InteractionWorldState, tuple[dict[str, Any], ...]]:
    world = initial
    trace: list[dict[str, Any]] = []
    released = False
    for expected_id, action in enumerate(steps):
        if action.step_id != expected_id:
            raise PlanValidationError("non_contiguous_step_ids", "plan step IDs must be contiguous from zero")
        if released:
            raise PlanValidationError("steps_after_release", "no action is allowed after release")
        before = world.to_dict()
        world = apply_symbolic_interaction_action(scene, world, action, goal=goal)
        if action.action == "release":
            released = True
        if capture_trace:
            trace.append(
                {
                    "step_id": action.step_id,
                    "action": action.action,
                    "state_before": before,
                    "state_after": world.to_dict(),
                }
            )
    return world, tuple(trace)


def validate_interaction_plan(
    scene: Mapping[str, Any] | str | Path | Any,
    plan: InteractionPlan | Mapping[str, Any] | str | Path,
) -> InteractionPlanningResult:
    """Validate a supplied plan without re-planning it."""

    try:
        scene_model = _SceneModel.from_payload(_coerce_mapping(scene, "scene"))
        if isinstance(plan, InteractionPlan):
            plan_model = plan
        else:
            plan_payload = _coerce_mapping(plan, "plan")
            plan_model = InteractionPlan.from_dict(plan_payload)
        resolved = _resolve_goal(scene_model, plan_model.goal)
        expected_initial = scene_model.initial_world()
        actual_initial = _world_from_dict(plan_model.initial_state, "plan.initial_state")
        if not _world_matches(expected_initial, actual_initial):
            raise PlanValidationError("invalid_initial_state", "plan initial_state does not match scene snapshot")
        final_world, trace = _simulate_steps(
            scene_model, resolved, actual_initial, plan_model.steps, capture_trace=True
        )
        actual_expected = _world_from_dict(
            plan_model.expected_final_state, "plan.expected_final_state"
        )
        if not _world_matches(final_world, actual_expected):
            raise PlanValidationError(
                "final_state_mismatch",
                "expected_final_state does not match symbolic action effects",
            )
        oracle = _task_oracle(scene_model, resolved, final_world)
        if oracle.get("task_success") is not True:
            raise PlanValidationError("goal_not_satisfied", "TaskEvaluator did not satisfy the final goal")
        return _success(
            plan_model,
            trace=trace,
            final_state=final_world.to_dict(),
            task_oracle=oracle,
        )
    except PlanningError as exc:
        return _failure(exc.reason, str(exc))
    except (TypeError, ValueError, OSError) as exc:
        return _failure("invalid_input", str(exc))


def replay_interaction_plan(
    scene: Mapping[str, Any] | str | Path | Any,
    plan: InteractionPlan | Mapping[str, Any] | str | Path,
) -> InteractionPlanningResult:
    """Replay a plan in the symbolic state machine and run TaskEvaluator."""

    # Validation performs the exact same ordered symbolic effects; replay is a
    # named API boundary so callers cannot confuse this with physics replay.
    return validate_interaction_plan(scene, plan)


def synthesize_articulation_task(
    scene: Mapping[str, Any] | str | Path | Any,
    object_id: str,
    state: str,
    joint_id: str | None = None,
) -> dict[str, Any]:
    """Build an existing ``TaskEvaluator`` articulation goal from scene metadata."""

    scene_model = _SceneModel.from_payload(_coerce_mapping(scene, "scene"))
    resolved = _resolve_goal(
        scene_model,
        None,
        object_id=object_id,
        state_name=state,
        joint_id=joint_id,
    )
    return _task_for_goal(resolved)


def write_plan_atomic(path: str | Path, plan: InteractionPlan) -> None:
    """Write a stable plan JSON document using an atomic replace."""

    destination = Path(path)
    encoded = json.dumps(
        plan.to_dict(), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
    ).encode("utf-8") + b"\n"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass
