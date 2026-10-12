"""Portable task/action contracts shared by all domain packs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from .domains import domain_pack_for_room
from .scene_ir import SceneIR


@dataclass(frozen=True)
class TaskAction:
    action: str
    actor: str | None = None
    subject: str | None = None
    target: str | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)
    preconditions: tuple[str, ...] = ()
    effects: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskGraph:
    schema_version: str
    domain: str
    actions: tuple[TaskAction, ...]

    @classmethod
    def from_scene_ir(cls, scene: SceneIR, actions: list[Mapping[str, Any]] | None = None) -> "TaskGraph":
        scene.validate()
        raw_actions = actions if actions is not None else [asdict(task) for task in scene.tasks]
        graph = cls(
            schema_version="scene_factory.task_graph.v1",
            domain=scene.domain,
            actions=tuple(
                TaskAction(
                    action=str(item["action"]), actor=item.get("actor"),
                    subject=item.get("subject"), target=item.get("target"),
                    parameters=item.get("parameters", {}),
                    preconditions=tuple(item.get("preconditions", ())),
                    effects=tuple(item.get("effects", ())),
                )
                for item in raw_actions
            ),
        )
        graph.validate(scene)
        return graph

    def validate(self, scene: SceneIR) -> None:
        if self.domain != scene.domain:
            raise ValueError("task graph domain does not match scene IR domain")
        allowed = set(domain_pack_for_room(scene.scene_type).task_actions)
        known = {entity.entity_id for entity in scene.entities}
        for item in self.actions:
            if item.action not in allowed:
                raise ValueError(f"action {item.action!r} is not supported by domain {self.domain}")
            for ref in (item.actor, item.subject, item.target):
                if ref is not None and ref not in known:
                    raise ValueError(f"task action {item.action!r} references unknown entity {ref!r}")
            if not isinstance(item.parameters, Mapping):
                raise ValueError(f"task action {item.action!r} parameters must be an object")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "domain": self.domain,
            "actions": [asdict(action) for action in self.actions],
        }
