"""Portable task/action contracts shared by all domain packs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from .domains import domain_pack_for_room
from .scene_ir import SceneIR, _array, _parameters, _reference, _text, _texts


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
        _array(raw_actions, "actions")
        parsed = []
        for item in raw_actions:
            if not isinstance(item, Mapping):
                raise ValueError("task action must be an object")
            try:
                action = TaskAction(**item)
            except TypeError as exc:
                raise ValueError(f"invalid task action: {exc}") from exc
            for name in ("preconditions", "effects"):
                value = getattr(action, name)
                _texts(value, name)
                object.__setattr__(action, name, tuple(value))
            parsed.append(action)
        graph = cls(
            schema_version="scene_factory.task_graph.v1",
            domain=scene.domain,
            actions=tuple(parsed),
        )
        graph.validate(scene)
        return graph

    def validate(self, scene: SceneIR) -> None:
        scene.validate()
        self._validate_structure()
        if self.domain != scene.domain:
            raise ValueError("task graph domain does not match scene IR domain")
        allowed = set(domain_pack_for_room(scene.scene_type).task_actions)
        known = {entity.entity_id for entity in scene.entities}
        for item in self.actions:
            if item.action not in allowed:
                raise ValueError(f"action {item.action!r} is not supported by domain {self.domain}")
            for ref in (item.actor, item.subject, item.target):
                _reference(ref, known, f"task action {item.action!r}")

    def _validate_structure(self) -> None:
        if self.schema_version != "scene_factory.task_graph.v1":
            raise ValueError(f"unsupported task graph schema_version: {self.schema_version!r}")
        _text(self.domain, "domain")
        _array(self.actions, "actions")
        for item in self.actions:
            if not isinstance(item, TaskAction):
                raise ValueError("actions must contain TaskAction values")
            _text(item.action, "action")
            _parameters(item.parameters, "task action")
            _texts(item.preconditions, "preconditions")
            _texts(item.effects, "effects")

    def to_dict(self) -> dict[str, Any]:
        self._validate_structure()
        return {
            "schema_version": self.schema_version,
            "domain": self.domain,
            "actions": [asdict(action) for action in self.actions],
        }
