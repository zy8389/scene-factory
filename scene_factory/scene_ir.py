"""Domain-neutral intermediate representation for complex scene requests.

The IR deliberately contains semantics and constraints, rather than renderer
details.  Existing ``SceneIntent`` values can be upgraded to this form without
changing the deterministic recipe compiler.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
import json
import math
from typing import Any, Mapping

from .domains import domain_pack_for_room
from .intent import SceneIntent


SCENE_IR_SCHEMA_VERSION = "scene_factory.scene_ir.v1"


def _text(value: Any, label: str, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f"{label} must be a {'non-empty ' if not allow_empty else ''}string")


def _array(value: Any, label: str) -> None:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be an array")


def _texts(value: Any, label: str) -> None:
    _array(value, label)
    for item in value:
        _text(item, label)


def _parameters(value: Any, label: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} parameters must be an object")
    try:
        json.dumps(dict(value), allow_nan=False)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{label} parameters must contain finite JSON values") from exc


def _dimensions(value: Any) -> None:
    _array(value, "dimensions_m")
    if len(value) != 3 or any(
        isinstance(item, bool) or not isinstance(item, (int, float))
        or not math.isfinite(item) or item <= 0 for item in value
    ):
        raise ValueError("dimensions_m must contain three positive finite numbers")


def _reference(value: Any, known: set[str], label: str) -> None:
    if value is not None:
        _text(value, label)
        if value not in known:
            raise ValueError(f"{label} references an unknown entity: {value!r}")


def _items(raw: Mapping[str, Any], name: str, cls: type) -> tuple:
    values = raw.get(name, ())
    _array(values, name)
    result = []
    for item in values:
        if not isinstance(item, Mapping):
            raise ValueError(f"{name} entries must be objects")
        try:
            parsed = cls(**item)
        except TypeError as exc:
            raise ValueError(f"invalid {name} entry: {exc}") from exc
        # JSON arrays and Python tuples must produce the same immutable sequence fields.
        if isinstance(parsed, IREntity):
            for attribute in ("attributes", "states"):
                value = getattr(parsed, attribute)
                _texts(value, f"entity.{attribute}")
                object.__setattr__(parsed, attribute, tuple(value))
        result.append(parsed)
    return tuple(result)


@dataclass(frozen=True)
class IREntity:
    entity_id: str
    kind: str
    dynamic: bool = True
    attributes: tuple[str, ...] = ()
    states: tuple[str, ...] = ()


@dataclass(frozen=True)
class IRRelation:
    subject: str
    predicate: str
    target: str


@dataclass(frozen=True)
class IRConstraint:
    constraint_type: str
    subject: str | None = None
    target: str | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IRTask:
    action: str
    actor: str | None = None
    subject: str | None = None
    target: str | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SceneIR:
    schema_version: str
    domain: str
    scene_type: str
    description: str
    dimensions_m: tuple[float, float, float] | None
    entities: tuple[IREntity, ...]
    relations: tuple[IRRelation, ...] = ()
    constraints: tuple[IRConstraint, ...] = ()
    tasks: tuple[IRTask, ...] = ()
    assumptions: tuple[str, ...] = ()

    @classmethod
    def from_intent(cls, intent: SceneIntent) -> "SceneIR":
        if not isinstance(intent, SceneIntent):
            raise TypeError("intent must be a SceneIntent")
        pack = domain_pack_for_room(intent.room_type)
        entities = tuple(
            IREntity(item.object_id, item.category, item.dynamic, item.attributes, item.state)
            for item in intent.objects
        )
        relations = tuple(IRRelation(item.subject, item.predicate, item.target) for item in intent.relations)
        constraints: list[IRConstraint] = []
        if intent.room_dimensions_m is not None:
            constraints.append(IRConstraint(
                "room_bounds", parameters={"dimensions_m": list(intent.room_dimensions_m)}
            ))
        result = cls(
            schema_version=SCENE_IR_SCHEMA_VERSION,
            domain=pack.domain_id,
            scene_type=intent.room_type,
            description=intent.description,
            dimensions_m=intent.room_dimensions_m,
            entities=entities,
            relations=relations,
            constraints=tuple(constraints),
            assumptions=(f"domain inferred from room_type={intent.room_type}",),
        )
        result.validate()
        return result

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SceneIR":
        if not isinstance(raw, Mapping):
            raise ValueError("scene IR must be an object")
        unknown = set(raw) - {item.name for item in fields(cls)}
        if unknown:
            raise ValueError(f"scene IR contains unsupported fields: {sorted(unknown, key=str)}")
        entities = _items(raw, "entities", IREntity)
        relations = _items(raw, "relations", IRRelation)
        constraints = _items(raw, "constraints", IRConstraint)
        tasks = _items(raw, "tasks", IRTask)
        dimensions = raw.get("dimensions_m")
        if dimensions is not None:
            _dimensions(dimensions)
        assumptions = raw.get("assumptions", ())
        _texts(assumptions, "assumptions")
        result = cls(
            schema_version=raw.get("schema_version", SCENE_IR_SCHEMA_VERSION),
            domain=raw.get("domain"), scene_type=raw.get("scene_type"),
            description=raw.get("description", ""),
            dimensions_m=tuple(dimensions) if dimensions is not None else None,
            entities=entities, relations=relations, constraints=constraints, tasks=tasks,
            assumptions=tuple(assumptions),
        )
        result.validate()
        return result

    def validate(self) -> None:
        if self.schema_version != SCENE_IR_SCHEMA_VERSION:
            raise ValueError(f"unsupported scene IR schema_version: {self.schema_version!r}")
        _text(self.domain, "domain")
        _text(self.scene_type, "scene_type")
        _text(self.description, "description", allow_empty=True)
        _texts(self.assumptions, "assumptions")
        pack = domain_pack_for_room(self.scene_type)
        if self.domain != pack.domain_id:
            raise ValueError("scene IR domain does not match scene_type")
        if self.dimensions_m is not None:
            _dimensions(self.dimensions_m)
        for name, cls in (("entities", IREntity), ("relations", IRRelation),
                          ("constraints", IRConstraint), ("tasks", IRTask)):
            values = getattr(self, name)
            _array(values, name)
            if any(not isinstance(item, cls) for item in values):
                raise ValueError(f"{name} must contain {cls.__name__} values")
        for entity in self.entities:
            _text(entity.entity_id, "entity.entity_id")
            _text(entity.kind, "entity.kind")
            if not isinstance(entity.dynamic, bool):
                raise ValueError("entity.dynamic must be a boolean")
            _texts(entity.attributes, "entity.attributes")
            _texts(entity.states, "entity.states")
        ids = [item.entity_id for item in self.entities]
        if len(ids) != len(set(ids)) or any(not item for item in ids):
            raise ValueError("scene IR entity IDs must be unique and non-empty")
        known = set(ids)
        for relation in self.relations:
            for ref in (relation.subject, relation.target):
                _text(ref, "relation reference")
                _reference(ref, known, "scene IR relation")
            _text(relation.predicate, "relation.predicate")
        for constraint in self.constraints:
            _text(constraint.constraint_type, "constraint.constraint_type")
            for ref in (constraint.subject, constraint.target):
                _reference(ref, known, "scene IR constraint")
            _parameters(constraint.parameters, "constraint")
            if constraint.constraint_type == "room_bounds":
                bounds = constraint.parameters.get("dimensions_m")
                _dimensions(bounds)
                if self.dimensions_m is not None and tuple(bounds) != tuple(self.dimensions_m):
                    raise ValueError("room_bounds dimensions_m does not match scene dimensions_m")
        for task in self.tasks:
            _text(task.action, "task.action")
            if task.action not in pack.task_actions:
                raise ValueError(f"action {task.action!r} is not supported by domain {self.domain}")
            for ref in (task.actor, task.subject, task.target):
                _reference(ref, known, "scene IR task")
            _parameters(task.parameters, "task")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        payload = asdict(self)
        if self.dimensions_m is not None:
            payload["dimensions_m"] = list(self.dimensions_m)
        return payload
