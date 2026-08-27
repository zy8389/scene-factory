from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import re
from typing import Any, Iterable


_OBJECT_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INTENT_FIELDS = {
    "room_type",
    "event",
    "description",
    "objects",
    "relations",
    "room_dimensions_m",
    "clutter_level",
    "layout_style",
}
_OBJECT_FIELDS = {"object_id", "category", "dynamic", "support_hint", "attributes", "state"}


def _strict_keys(raw: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"{label} contains unsupported fields: {', '.join(unknown)}")


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be an array")
    if any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must contain only strings")
    return tuple(item.strip() for item in value if item.strip())


def _required_keys(raw: dict[str, Any], required: set[str], label: str) -> None:
    missing = sorted(required - set(raw))
    if missing:
        raise ValueError(f"{label} is missing required fields: {', '.join(missing)}")


def _strict_string(value: Any, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    result = value.strip()
    if not allow_empty and not result:
        raise ValueError(f"{field_name} must not be empty")
    return result


def _finite_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be finite")
    return result


@dataclass(frozen=True)
class IntentObject:
    object_id: str
    category: str
    dynamic: bool = True
    support_hint: str | None = None
    attributes: tuple[str, ...] = ()
    state: tuple[str, ...] = ()

    @classmethod
    def from_dict(
        cls,
        raw: dict[str, Any],
        allowed_categories: set[str] | None = None,
    ) -> "IntentObject":
        if not isinstance(raw, dict):
            raise ValueError("intent object must be a JSON object")
        _required_keys(raw, _OBJECT_FIELDS, "intent object")
        _strict_keys(
            raw,
            _OBJECT_FIELDS,
            "intent object",
        )
        object_id = _strict_string(raw["object_id"], "intent object.object_id")
        category = _strict_string(raw["category"], "intent object.category")
        if not _OBJECT_ID_RE.fullmatch(object_id):
            raise ValueError(f"invalid intent object ID: {object_id!r}")
        if not category:
            raise ValueError("intent object requires object_id and category")
        if allowed_categories is not None and category not in allowed_categories:
            raise ValueError(f"unknown asset category in intent: {category}")
        support_hint = raw.get("support_hint")
        if support_hint is not None:
            support_hint = _strict_string(support_hint, "intent object.support_hint")
        if not isinstance(raw["dynamic"], bool):
            raise ValueError("intent object.dynamic must be a boolean")
        return cls(
            object_id=object_id,
            category=category,
            dynamic=raw["dynamic"],
            support_hint=support_hint,
            attributes=_string_tuple(raw["attributes"], "intent object.attributes"),
            state=_string_tuple(raw["state"], "intent object.state"),
        )


@dataclass(frozen=True)
class IntentRelation:
    subject: str
    predicate: str
    target: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "IntentRelation":
        if not isinstance(raw, dict):
            raise ValueError("intent relation must be a JSON object")
        _required_keys(raw, {"subject", "predicate", "target"}, "intent relation")
        _strict_keys(raw, {"subject", "predicate", "target"}, "intent relation")
        subject = _strict_string(raw["subject"], "intent relation.subject")
        predicate = _strict_string(raw["predicate"], "intent relation.predicate")
        target = _strict_string(raw["target"], "intent relation.target")
        if not subject or not target:
            raise ValueError("intent relation requires subject and target")
        if predicate not in {"on", "near", "partly_occluded_by"}:
            raise ValueError(f"unsupported intent relation: {predicate}")
        return cls(subject=subject, predicate=predicate, target=target)


@dataclass(frozen=True)
class SceneIntent:
    room_type: str
    event: str
    description: str
    objects: tuple[IntentObject, ...]
    relations: tuple[IntentRelation, ...] = ()
    room_dimensions_m: tuple[float, float, float] | None = None
    clutter_level: float = 0.5
    layout_style: str = "casual"

    @classmethod
    def from_dict(
        cls,
        raw: dict[str, Any],
        *,
        allowed_categories: Iterable[str] | None = None,
        allowed_room_types: Iterable[str] | None = None,
        allowed_events: Iterable[str] | None = None,
    ) -> "SceneIntent":
        if not isinstance(raw, dict):
            raise ValueError("scene intent must be a JSON object")
        _required_keys(raw, _INTENT_FIELDS, "scene intent")
        _strict_keys(
            raw,
            _INTENT_FIELDS,
            "scene intent",
        )
        room_type = _strict_string(raw["room_type"], "scene intent.room_type")
        event = _strict_string(raw["event"], "scene intent.event")
        description = _strict_string(raw["description"], "scene intent.description", allow_empty=True)

        room_types = set(allowed_room_types or ())
        events = set(allowed_events or ())
        categories = set(allowed_categories) if allowed_categories is not None else None
        if room_types and room_type not in room_types:
            raise ValueError(f"unsupported room_type in intent: {room_type}")
        if events and event not in events:
            raise ValueError(f"unsupported event in intent: {event}")

        objects_raw = raw["objects"]
        if not isinstance(objects_raw, list) or not objects_raw:
            raise ValueError("scene intent.objects must be a non-empty array")
        if len(objects_raw) > 32:
            raise ValueError("scene intent.objects cannot contain more than 32 objects")
        objects = tuple(IntentObject.from_dict(item, categories) for item in objects_raw)
        object_ids = [item.object_id for item in objects]
        if len(object_ids) != len(set(object_ids)):
            raise ValueError("scene intent contains duplicate object IDs")

        relations_raw = raw["relations"]
        if not isinstance(relations_raw, list):
            raise ValueError("scene intent.relations must be an array")
        relations = tuple(IntentRelation.from_dict(item) for item in relations_raw)
        object_id_set = set(object_ids)
        relation_keys = set()
        for relation in relations:
            if relation.subject not in object_id_set or relation.target not in object_id_set:
                raise ValueError(
                    f"intent relation references unknown object: "
                    f"{relation.subject!r} -> {relation.target!r}"
                )
            if relation.subject == relation.target:
                raise ValueError(f"intent relation cannot reference itself: {relation.subject!r}")
            key = (relation.subject, relation.predicate, relation.target)
            if key in relation_keys:
                raise ValueError(f"scene intent contains duplicate relation: {key!r}")
            relation_keys.add(key)

        dimensions_raw = raw["room_dimensions_m"]
        dimensions = None
        if dimensions_raw is not None:
            if not isinstance(dimensions_raw, list) or len(dimensions_raw) != 3:
                raise ValueError("room_dimensions_m must contain exactly three numbers")
            dimensions = tuple(
                _finite_number(value, "room_dimensions_m") for value in dimensions_raw
            )
            if any(value <= 0 for value in dimensions):
                raise ValueError("room_dimensions_m values must be positive")

        clutter_level = _finite_number(raw["clutter_level"], "clutter_level")
        if not 0.0 <= clutter_level <= 1.0:
            raise ValueError("clutter_level must be between 0 and 1")
        layout_style = _strict_string(raw["layout_style"], "scene intent.layout_style")
        return cls(
            room_type=room_type,
            event=event,
            description=description,
            objects=objects,
            relations=relations,
            room_dimensions_m=dimensions,
            clutter_level=clutter_level,
            layout_style=layout_style,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["objects"] = [
            {
                **asdict(item),
                "attributes": list(item.attributes),
                "state": list(item.state),
            }
            for item in self.objects
        ]
        payload["relations"] = [asdict(item) for item in self.relations]
        if self.room_dimensions_m is not None:
            payload["room_dimensions_m"] = list(self.room_dimensions_m)
        return payload


def scene_intent_schema(
    categories: Iterable[str],
    room_types: Iterable[str],
    events: Iterable[str],
) -> dict[str, Any]:
    """Build the strict JSON Schema sent to a structured-output LLM."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "room_type",
            "event",
            "description",
            "objects",
            "relations",
            "room_dimensions_m",
            "clutter_level",
            "layout_style",
        ],
        "properties": {
            "room_type": {"type": "string", "enum": sorted(set(room_types))},
            "event": {"type": "string", "enum": sorted(set(events))},
            "description": {"type": "string"},
            "room_dimensions_m": {
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "array",
                        "items": {"type": "number", "exclusiveMinimum": 0},
                        "minItems": 3,
                        "maxItems": 3,
                    },
                ]
            },
            "clutter_level": {"type": "number", "minimum": 0, "maximum": 1},
            "layout_style": {"type": "string"},
            "objects": {
                "type": "array",
                "minItems": 1,
                "maxItems": 32,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "object_id",
                        "category",
                        "dynamic",
                        "support_hint",
                        "attributes",
                        "state",
                    ],
                    "properties": {
                        "object_id": {
                            "type": "string",
                            "pattern": "^[A-Za-z_][A-Za-z0-9_]*$",
                        },
                        "category": {"type": "string", "enum": sorted(set(categories))},
                        "dynamic": {"type": "boolean"},
                        "support_hint": {"type": ["string", "null"]},
                        "attributes": {"type": "array", "items": {"type": "string"}},
                        "state": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "relations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["subject", "predicate", "target"],
                    "properties": {
                        "subject": {
                            "type": "string",
                            "pattern": "^[A-Za-z_][A-Za-z0-9_]*$",
                        },
                        "predicate": {
                            "type": "string",
                            "enum": ["on", "near", "partly_occluded_by"],
                        },
                        "target": {
                            "type": "string",
                            "pattern": "^[A-Za-z_][A-Za-z0-9_]*$",
                        },
                    },
                },
            },
        },
    }
