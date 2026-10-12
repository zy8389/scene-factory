"""Domain-neutral intermediate representation for complex scene requests.

The IR deliberately contains semantics and constraints, rather than renderer
details.  Existing ``SceneIntent`` values can be upgraded to this form without
changing the deterministic recipe compiler.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from .domains import domain_pack_for_room
from .intent import SceneIntent


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
        return cls(
            schema_version="scene_factory.scene_ir.v1",
            domain=pack.domain_id,
            scene_type=intent.room_type,
            description=intent.description,
            dimensions_m=intent.room_dimensions_m,
            entities=entities,
            relations=relations,
            constraints=tuple(constraints),
            assumptions=(f"domain inferred from room_type={intent.room_type}",),
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SceneIR":
        if not isinstance(raw, Mapping):
            raise ValueError("scene IR must be an object")
        entities = tuple(IREntity(**item) for item in raw.get("entities", []))
        relations = tuple(IRRelation(**item) for item in raw.get("relations", []))
        constraints = tuple(IRConstraint(**item) for item in raw.get("constraints", []))
        tasks = tuple(IRTask(**item) for item in raw.get("tasks", []))
        result = cls(
            schema_version=str(raw.get("schema_version", "scene_factory.scene_ir.v1")),
            domain=str(raw["domain"]), scene_type=str(raw["scene_type"]),
            description=str(raw.get("description", "")),
            dimensions_m=tuple(raw["dimensions_m"]) if raw.get("dimensions_m") is not None else None,
            entities=entities, relations=relations, constraints=constraints, tasks=tasks,
            assumptions=tuple(str(item) for item in raw.get("assumptions", [])),
        )
        result.validate()
        return result

    def validate(self) -> None:
        ids = [item.entity_id for item in self.entities]
        if not self.domain or not self.scene_type:
            raise ValueError("scene IR requires domain and scene_type")
        if len(ids) != len(set(ids)) or any(not item for item in ids):
            raise ValueError("scene IR entity IDs must be unique and non-empty")
        known = set(ids)
        for relation in self.relations:
            if relation.subject not in known or relation.target not in known:
                raise ValueError("scene IR relation references an unknown entity")
        for task in self.tasks:
            for ref in (task.actor, task.subject, task.target):
                if ref is not None and ref not in known:
                    raise ValueError("scene IR task references an unknown entity")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        payload = asdict(self)
        if self.dimensions_m is not None:
            payload["dimensions_m"] = list(self.dimensions_m)
        return payload
