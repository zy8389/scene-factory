from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable


def _strict_keys(raw: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"{label} contains unsupported fields: {', '.join(unknown)}")


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be an array")
    return tuple(str(item).strip() for item in value if str(item).strip())


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
        _strict_keys(
            raw,
            {"object_id", "category", "dynamic", "support_hint", "attributes", "state"},
            "intent object",
        )
        object_id = str(raw.get("object_id", "")).strip()
        category = str(raw.get("category", "")).strip()
        if not object_id or not category:
            raise ValueError("intent object requires object_id and category")
        if allowed_categories is not None and category not in allowed_categories:
            raise ValueError(f"unknown asset category in intent: {category}")
        support_hint = raw.get("support_hint")
        if support_hint is not None:
            support_hint = str(support_hint).strip() or None
        return cls(
            object_id=object_id,
            category=category,
            dynamic=bool(raw.get("dynamic", True)),
            support_hint=support_hint,
            attributes=_string_tuple(raw.get("attributes"), "intent object.attributes"),
            state=_string_tuple(raw.get("state"), "intent object.state"),
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
        _strict_keys(raw, {"subject", "predicate", "target"}, "intent relation")
        subject = str(raw.get("subject", "")).strip()
        predicate = str(raw.get("predicate", "")).strip()
        target = str(raw.get("target", "")).strip()
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
        _strict_keys(
            raw,
            {
                "room_type",
                "event",
                "description",
                "objects",
                "relations",
                "room_dimensions_m",
                "clutter_level",
                "layout_style",
            },
            "scene intent",
        )
        room_type = str(raw.get("room_type", "")).strip()
        event = str(raw.get("event", "")).strip()
        description = str(raw.get("description", "")).strip()
        if not room_type or not event:
            raise ValueError("scene intent requires room_type and event")

        room_types = set(allowed_room_types or ())
        events = set(allowed_events or ())
        categories = set(allowed_categories) if allowed_categories is not None else None
        if room_types and room_type not in room_types:
            raise ValueError(f"unsupported room_type in intent: {room_type}")
        if events and event not in events:
            raise ValueError(f"unsupported event in intent: {event}")

        objects_raw = raw.get("objects")
        if not isinstance(objects_raw, list) or not objects_raw:
            raise ValueError("scene intent.objects must be a non-empty array")
        objects = tuple(IntentObject.from_dict(item, categories) for item in objects_raw)
        object_ids = [item.object_id for item in objects]
        if len(object_ids) != len(set(object_ids)):
            raise ValueError("scene intent contains duplicate object IDs")

        relations_raw = raw.get("relations", [])
        if not isinstance(relations_raw, list):
            raise ValueError("scene intent.relations must be an array")
        relations = tuple(IntentRelation.from_dict(item) for item in relations_raw)

        dimensions_raw = raw.get("room_dimensions_m")
        dimensions = None
        if dimensions_raw is not None:
            if not isinstance(dimensions_raw, list) or len(dimensions_raw) != 3:
                raise ValueError("room_dimensions_m must contain exactly three numbers")
            dimensions = tuple(float(value) for value in dimensions_raw)
            if any(value <= 0 for value in dimensions):
                raise ValueError("room_dimensions_m values must be positive")

        clutter_level = float(raw.get("clutter_level", 0.5))
        if not 0.0 <= clutter_level <= 1.0:
            raise ValueError("clutter_level must be between 0 and 1")
        layout_style = str(raw.get("layout_style", "casual")).strip() or "casual"
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
        return asdict(self)


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
                        "subject": {"type": "string"},
                        "predicate": {
                            "type": "string",
                            "enum": ["on", "near", "partly_occluded_by"],
                        },
                        "target": {"type": "string"},
                    },
                },
            },
        },
    }
