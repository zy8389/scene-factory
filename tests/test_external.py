from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scene_factory.cli import main
from scene_factory.dataset import inspect_dataset, reproduce_dataset, validate_dataset
from scene_factory.external import (
    ENVELOPE_SOURCE_FORMAT,
    EXTERNAL_SCHEMA_VERSION,
    ExternalSceneError,
    adapt_external_scene,
    external_scene_schema,
    load_external_scene,
)
from scene_factory.factory import SceneFactory


def _intent_payload() -> dict:
    return {
        "room_type": "living_room",
        "event": "recent_snacking",
        "description": "茶几上有一个杯子。",
        "objects": [
            {
                "object_id": "sofa_1",
                "category": "sofa",
                "dynamic": False,
                "support_hint": None,
                "attributes": [],
                "state": [],
            },
            {
                "object_id": "coffee_table_1",
                "category": "coffee_table",
                "dynamic": False,
                "support_hint": None,
                "attributes": [],
                "state": [],
            },
            {
                "object_id": "mug_1",
                "category": "mug",
                "dynamic": True,
                "support_hint": "coffee_table_1",
                "attributes": [],
                "state": [],
            },
        ],
        "relations": [
            {"subject": "mug_1", "predicate": "on", "target": "coffee_table_1"}
        ],
        "room_dimensions_m": None,
        "clutter_level": 0.5,
        "layout_style": "casual",
    }


class ExternalSceneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with patch.dict(os.environ, {"SCENE_FACTORY_LLM_MODE": "off"}):
            cls.factory = SceneFactory()

    def _adapt(self, payload):
        return adapt_external_scene(
            payload,
            allowed_categories=self.factory.registry.categories(),
            allowed_room_types=self.factory.recipes.room_types(),
            allowed_events=self.factory.recipes.events(),
        )

    def test_raw_and_envelope_are_semantically_equivalent(self) -> None:
        raw = _intent_payload()
        envelope = {
            "schema_version": EXTERNAL_SCHEMA_VERSION,
            "producer": {"name": "generator-a", "version": "1"},
            "intent": raw,
        }
        first = self._adapt(raw)
        second = self._adapt(envelope)
        self.assertEqual(first.intent, second.intent)
        self.assertEqual(first.canonical_sha256, second.canonical_sha256)
        self.assertEqual(first.source_format, "raw_scene_intent")
        self.assertEqual(second.source_format, ENVELOPE_SOURCE_FORMAT)
        self.assertNotEqual(first.input_source["format"], second.input_source["format"])

    def test_producer_metadata_does_not_change_scene_or_intent_hash(self) -> None:
        first = self._adapt(
            {
                "schema_version": EXTERNAL_SCHEMA_VERSION,
                "producer": {"name": "a", "version": "1"},
                "intent": _intent_payload(),
            }
        )
        second = self._adapt(
            {
                "schema_version": EXTERNAL_SCHEMA_VERSION,
                "producer": {"name": "b", "version": "2", "revision": "r9"},
                "intent": _intent_payload(),
            }
        )
        self.assertEqual(first.canonical_sha256, second.canonical_sha256)
        self.assertEqual(
            self.factory.build_from_intent(first.intent, 31).scene.scene_id,
            self.factory.build_from_intent(second.intent, 31).scene.scene_id,
        )

    def test_strict_validation_rejects_malformed_external_documents(self) -> None:
        invalid_documents = {
            "blank": " \n\t",
            "malformed": "{broken",
            "array": "[]",
            "unsupported_version": {
                "schema_version": "scene_factory.external_scene.v9",
                "producer": {},
                "intent": _intent_payload(),
            },
            "unknown_envelope_field": {
                "schema_version": EXTERNAL_SCHEMA_VERSION,
                "producer": {},
                "intent": _intent_payload(),
                "extra": True,
            },
            "malformed_producer": {
                "schema_version": EXTERNAL_SCHEMA_VERSION,
                "producer": ["not-an-object"],
                "intent": _intent_payload(),
            },
            "missing_intent": {
                "schema_version": EXTERNAL_SCHEMA_VERSION,
                "producer": {},
            },
        }
        for name, document in invalid_documents.items():
            with self.subTest(name=name), self.assertRaises(ExternalSceneError):
                self._adapt(document)

    def test_external_domains_reject_unknown_room_event_and_category(self) -> None:
        cases = [
            ("room", {"room_type": "unknown_room"}),
            ("event", {"event": "unknown_event"}),
            ("category", {"objects": [{**_intent_payload()["objects"][2], "category": "unknown"}]}),
        ]
        for name, mutation in cases:
            payload = _intent_payload()
            payload.update(mutation)
            with self.subTest(name=name), self.assertRaises(ExternalSceneError):
                adapt_external_scene(payload)

    def test_non_finite_json_constants_are_rejected(self) -> None:
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value), self.assertRaises(ExternalSceneError):
                self._adapt(json.dumps(_intent_payload()).replace("0.5", value, 1))

    def test_scene_intent_validation_rejects_unsafe_semantics(self) -> None:
        cases = [
            ("unknown_field", lambda payload: payload.update(unexpected=1)),
            ("invalid_id", lambda payload: payload["objects"][2].update(object_id="../mug")),
            (
                "duplicate_object",
                lambda payload: payload["objects"][1].update(object_id="sofa_1"),
            ),
            (
                "bad_relation_predicate",
                lambda payload: payload["relations"][0].update(predicate="bad"),
            ),
            (
                "dangling_relation",
                lambda payload: payload["relations"][0].update(target="missing"),
            ),
            (
                "self_relation",
                lambda payload: payload["relations"].__setitem__(
                    0,
                    {"subject": "mug_1", "predicate": "near", "target": "mug_1"},
                ),
            ),
            (
                "duplicate_relation",
                lambda payload: payload["relations"].append(payload["relations"][0]),
            ),
            (
                "negative_dimension",
                lambda payload: payload.update(room_dimensions_m=[-1, 2, 3]),
            ),
            ("clutter_out_of_range", lambda payload: payload.update(clutter_level=2)),
            ("nan", lambda payload: payload.update(clutter_level=float("nan"))),
        ]
        for name, mutate in cases:
            payload = json.loads(json.dumps(_intent_payload(), ensure_ascii=False))
            mutate(payload)
            with self.subTest(name=name), self.assertRaises(ExternalSceneError):
                self._adapt(payload)

    def test_load_is_bounded_and_rejects_invalid_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intent.json"
            path.write_bytes(b"\xff")
            with self.assertRaises(ExternalSceneError):
                load_external_scene(path)
            path.write_bytes(b"{" + b"\"description\":\"" + b"a" * (1024 * 1024) + b"\"}")
            with self.assertRaises(ExternalSceneError):
                load_external_scene(path)

    def test_build_from_intent_reuses_canonical_pipeline(self) -> None:
        document = self._adapt(_intent_payload())
        result = self.factory.build_from_intent(
            document.intent,
            seed=41,
            input_source=document.input_source,
        )
        self.assertIs(result.intent, document.intent)
        self.assertTrue(result.valid, result.validation.to_dict())
        self.assertEqual(result.prompt_parser, "external_intent")
        with tempfile.TemporaryDirectory() as directory:
            files = self.factory.write_result(result, directory)
            spec = json.loads(Path(files["scene_spec"]).read_text(encoding="utf-8"))
            self.assertEqual(spec["input_source"]["type"], "external_intent")
            self.assertTrue(Path(files["intent"]).is_file())

    def test_intent_dataset_reloads_reproduces_and_ignores_provenance(self) -> None:
        document = self._adapt(
            {
                "schema_version": EXTERNAL_SCHEMA_VERSION,
                "producer": {"name": "original", "version": "1"},
                "intent": _intent_payload(),
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            self.factory.build_batch(
                directory,
                count=3,
                seed_start=70,
                intent=document.intent,
                input_source=document.input_source,
            )
            self.assertTrue(validate_dataset(directory).valid)
            self.assertTrue(reproduce_dataset(directory).valid)
            self.assertEqual(inspect_dataset(directory)["metadata"]["source"]["type"], "intent")
            metadata_path = Path(directory) / "dataset.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["source"]["producer"] = {"name": "different", "version": "99"}
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            self.assertTrue(validate_dataset(directory).valid)
            self.assertTrue(reproduce_dataset(directory).valid)

    def test_intent_resume_accepts_raw_and_envelope_equivalence(self) -> None:
        raw_document = self._adapt(_intent_payload())
        envelope_document = self._adapt(
            {
                "schema_version": EXTERNAL_SCHEMA_VERSION,
                "producer": {"name": "resume-generator", "version": "2"},
                "intent": _intent_payload(),
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            calls = 0
            original_write = self.factory.write_result

            def fail_second(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("simulated external intent export failure")
                return original_write(*args, **kwargs)

            with patch.object(self.factory, "write_result", side_effect=fail_second):
                with self.assertRaises(RuntimeError):
                    self.factory.build_batch(
                        directory,
                        count=3,
                        seed_start=120,
                        intent=raw_document.intent,
                    )
            self.factory.build_batch(
                directory,
                count=3,
                seed_start=120,
                intent=envelope_document.intent,
                input_source=envelope_document.input_source,
                resume=True,
            )
            self.assertTrue(validate_dataset(directory).valid)

    def test_cli_supports_intent_commands_build_batch_and_stdin(self) -> None:
        payload = json.dumps(_intent_payload(), ensure_ascii=False)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "intent.json"
            source.write_text(payload, encoding="utf-8")
            with patch.dict(os.environ, {"SCENE_FACTORY_LLM_MODE": "off"}):
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main(["intent", "validate", str(source)]), 0)
                    self.assertIn('"valid": true', output.getvalue())
                self.assertEqual(main(["intent", "inspect", str(source)]), 0)
                self.assertEqual(main(["build", "--intent", str(source), "--output", str(Path(directory) / "one")]), 0)
                with patch("sys.stdin", io.TextIOWrapper(io.BytesIO(payload.encode("utf-8")))):
                    self.assertEqual(
                        main(
                            [
                                "batch",
                                "--intent",
                                "-",
                                "--count",
                                "1",
                                "--output",
                                str(Path(directory) / "stdin"),
                            ]
                        ),
                        0,
                    )

    def test_no_isaac_or_numpy_import_is_needed(self) -> None:
        import sys

        self.assertNotIn("isaacsim", sys.modules)
        self.assertNotIn("numpy", sys.modules)

    def test_external_schema_wraps_canonical_scene_intent_schema(self) -> None:
        schema = external_scene_schema(
            self.factory.registry.categories(),
            self.factory.recipes.room_types(),
            self.factory.recipes.events(),
        )
        self.assertEqual(schema["title"], EXTERNAL_SCHEMA_VERSION)
        self.assertEqual(schema["properties"]["intent"]["additionalProperties"], False)
        self.assertEqual(schema["properties"]["intent"]["properties"]["objects"]["maxItems"], 32)


if __name__ == "__main__":
    unittest.main()
