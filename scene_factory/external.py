"""Strict, offline adapters for externally produced :class:`SceneIntent` JSON."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .intent import SceneIntent, scene_intent_schema


EXTERNAL_SCHEMA_VERSION = "scene_factory.external_scene.v1"
RAW_SOURCE_FORMAT = "raw_scene_intent"
ENVELOPE_SOURCE_FORMAT = "external_scene_v1"
MAX_EXTERNAL_SCENE_BYTES = 1024 * 1024
_ENVELOPE_FIELDS = {"schema_version", "producer", "intent"}
_PRODUCER_FIELDS = {"name", "version", "revision"}


class ExternalSceneError(ValueError):
    """Raised when an external scene document violates the input contract."""


def external_scene_schema(
    categories: Iterable[str],
    room_types: Iterable[str],
    events: Iterable[str],
) -> dict[str, Any]:
    """Build the versioned envelope schema around the canonical SceneIntent schema."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": EXTERNAL_SCHEMA_VERSION,
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "producer", "intent"],
        "properties": {
            "schema_version": {"const": EXTERNAL_SCHEMA_VERSION},
            "producer": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    field: {"type": "string", "minLength": 1}
                    for field in sorted(_PRODUCER_FIELDS)
                },
            },
            "intent": scene_intent_schema(categories, room_types, events),
        },
    }


@dataclass(frozen=True)
class ExternalSceneDocument:
    intent: SceneIntent
    source_format: str
    schema_version: str
    producer: dict[str, str]
    canonical_sha256: str

    @property
    def intent_sha256(self) -> str:
        return self.canonical_sha256

    @property
    def input_source(self) -> dict[str, Any]:
        return {
            "type": "external_intent",
            "format": self.source_format,
            "producer": dict(self.producer),
            "intent_sha256": self.canonical_sha256,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_format": self.source_format,
            "producer": dict(self.producer),
            "intent_sha256": self.canonical_sha256,
            "intent": self.intent.to_dict(),
        }


def canonical_intent_sha256(intent: SceneIntent | Mapping[str, Any]) -> str:
    """Hash normalized SceneIntent JSON, excluding external provenance."""
    if isinstance(intent, SceneIntent):
        payload = intent.to_dict()
    else:
        try:
            payload = SceneIntent.from_dict(dict(intent)).to_dict()
        except (TypeError, ValueError) as exc:
            raise ExternalSceneError(f"intent cannot be normalized: {exc}") from exc
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ExternalSceneError(f"intent cannot be canonically encoded: {exc}") from exc
    return hashlib.sha256(encoded).hexdigest()


def _reject_constant(value: str) -> None:
    raise ExternalSceneError(f"JSON constant is not allowed: {value}")


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExternalSceneError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _decode_json_bytes(raw: bytes) -> Any:
    if len(raw) > MAX_EXTERNAL_SCENE_BYTES:
        raise ExternalSceneError(
            f"external scene input exceeds {MAX_EXTERNAL_SCENE_BYTES} bytes"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExternalSceneError(f"external scene input is not valid UTF-8: {exc}") from exc
    if not text.strip():
        raise ExternalSceneError("external scene input is blank")
    try:
        return json.loads(
            text,
            object_pairs_hook=_object_pairs,
            parse_constant=_reject_constant,
        )
    except ExternalSceneError:
        raise
    except json.JSONDecodeError as exc:
        raise ExternalSceneError(f"external scene input is malformed JSON: {exc}") from exc
    except RecursionError as exc:
        raise ExternalSceneError("external scene input is too deeply nested") from exc


def _decode_payload(payload: Any) -> Any:
    if isinstance(payload, bytes):
        return _decode_json_bytes(payload)
    if isinstance(payload, str):
        try:
            raw = payload.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ExternalSceneError(f"external scene input is not valid UTF-8: {exc}") from exc
        return _decode_json_bytes(raw)
    if isinstance(payload, Mapping):
        try:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise ExternalSceneError(f"external scene payload is not JSON-safe: {exc}") from exc
        if len(encoded) > MAX_EXTERNAL_SCENE_BYTES:
            raise ExternalSceneError(
                f"external scene input exceeds {MAX_EXTERNAL_SCENE_BYTES} bytes"
            )
        return dict(payload)
    raise ExternalSceneError("external scene input must be JSON text or an object")


def _validate_producer(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ExternalSceneError("external scene producer must be an object")
    unknown = sorted(set(value) - _PRODUCER_FIELDS)
    if unknown:
        raise ExternalSceneError(
            f"external scene producer contains unsupported fields: {', '.join(unknown)}"
        )
    producer: dict[str, str] = {}
    for key, raw_value in value.items():
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise ExternalSceneError(f"external scene producer.{key} must be a non-empty string")
        producer[key] = raw_value.strip()
    return producer


def normalize_producer(value: Any) -> dict[str, str]:
    """Validate the small, non-secret provenance record used by scene outputs."""
    return _validate_producer(value)


def adapt_external_scene(
    payload: Any,
    *,
    allowed_categories: Iterable[str] | None = None,
    allowed_room_types: Iterable[str] | None = None,
    allowed_events: Iterable[str] | None = None,
) -> ExternalSceneDocument:
    """Validate raw SceneIntent JSON or an external_scene.v1 envelope.

    This function only parses and validates data.  It never imports a producer,
    follows a path from the document, contacts a network service, or executes code.
    """
    decoded = _decode_payload(payload)
    if not isinstance(decoded, dict):
        raise ExternalSceneError("external scene JSON root must be an object")

    if (
        allowed_categories is None
        or allowed_room_types is None
        or allowed_events is None
    ):
        from .paths import default_recipes_dir, default_registry_path
        from .recipes import RecipeLibrary
        from .registry import AssetRegistry

        try:
            registry = AssetRegistry.load(default_registry_path())
            recipes = RecipeLibrary.load(default_recipes_dir())
        except (OSError, TypeError, ValueError) as exc:
            raise ExternalSceneError(f"cannot load SceneIntent validation domains: {exc}") from exc
        if allowed_categories is None:
            allowed_categories = registry.categories()
        if allowed_room_types is None:
            allowed_room_types = recipes.room_types()
        if allowed_events is None:
            allowed_events = recipes.events()

    if "schema_version" in decoded:
        unknown = sorted(set(decoded) - _ENVELOPE_FIELDS)
        if unknown:
            raise ExternalSceneError(
                f"external scene envelope contains unsupported fields: {', '.join(unknown)}"
            )
        if set(decoded) != _ENVELOPE_FIELDS:
            missing = sorted(_ENVELOPE_FIELDS - set(decoded))
            raise ExternalSceneError(
                f"external scene envelope is missing required fields: {', '.join(missing)}"
            )
        if decoded["schema_version"] != EXTERNAL_SCHEMA_VERSION:
            raise ExternalSceneError(
                f"unsupported external scene schema_version: {decoded['schema_version']!r}"
            )
        producer = _validate_producer(decoded["producer"])
        intent_payload = decoded["intent"]
        source_format = ENVELOPE_SOURCE_FORMAT
        schema_version = EXTERNAL_SCHEMA_VERSION
    else:
        producer = {}
        intent_payload = decoded
        source_format = RAW_SOURCE_FORMAT
        schema_version = EXTERNAL_SCHEMA_VERSION

    if not isinstance(intent_payload, dict):
        raise ExternalSceneError("external scene intent must be an object")
    try:
        intent = SceneIntent.from_dict(
            intent_payload,
            allowed_categories=allowed_categories,
            allowed_room_types=allowed_room_types,
            allowed_events=allowed_events,
        )
    except (TypeError, ValueError) as exc:
        raise ExternalSceneError(f"invalid SceneIntent: {exc}") from exc
    return ExternalSceneDocument(
        intent=intent,
        source_format=source_format,
        schema_version=schema_version,
        producer=producer,
        canonical_sha256=canonical_intent_sha256(intent),
    )


def load_external_scene(
    path: str | Path,
    *,
    allowed_categories: Iterable[str] | None = None,
    allowed_room_types: Iterable[str] | None = None,
    allowed_events: Iterable[str] | None = None,
    stream: Any | None = None,
) -> ExternalSceneDocument:
    """Load a bounded local file, or ``-`` from the supplied stdin stream."""
    if str(path) == "-":
        source = stream if stream is not None else sys.stdin.buffer
        try:
            raw = source.read(MAX_EXTERNAL_SCENE_BYTES + 1)
        except (OSError, ValueError) as exc:
            raise ExternalSceneError(f"cannot read external scene stdin: {exc}") from exc
        if isinstance(raw, str):
            try:
                raw = raw.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ExternalSceneError(f"external scene input is not valid UTF-8: {exc}") from exc
        if not isinstance(raw, bytes):
            raise ExternalSceneError("external scene stdin did not return bytes or text")
    else:
        source_path = Path(path).expanduser()
        try:
            with source_path.open("rb") as handle:
                raw = handle.read(MAX_EXTERNAL_SCENE_BYTES + 1)
        except (OSError, ValueError) as exc:
            raise ExternalSceneError(f"cannot read external scene {source_path}: {exc}") from exc
    return adapt_external_scene(
        raw,
        allowed_categories=allowed_categories,
        allowed_room_types=allowed_room_types,
        allowed_events=allowed_events,
    )
