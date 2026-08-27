from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from scene_factory.dataset import inspect_dataset, reproduce_dataset, validate_dataset
from scene_factory.factory import BuildResult, SceneFactory
from scene_factory.models import ValidationReport


class DatasetContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with patch.dict(os.environ, {"SCENE_FACTORY_LLM_MODE": "off"}):
            cls.factory = SceneFactory()

    def _build(
        self, root: str, *, count: int = 1, seed_start: int = 100, resume: bool = False
    ) -> list[dict]:
        return self.factory.build_batch(
            root,
            count=count,
            seed_start=seed_start,
            recipe_name="living_room_recent_snacking",
            resume=resume,
        )

    @staticmethod
    def _manifest(root: str | Path) -> list[dict]:
        return [
            json.loads(line)
            for line in (Path(root) / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        ]

    def test_one_scene_dataset_contract_and_reproduction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = self._build(directory)
            metadata = json.loads((Path(directory) / "dataset.json").read_text(encoding="utf-8"))
            row = self._manifest(directory)[0]
            self.assertEqual(len(manifest), 1)
            self.assertEqual(metadata["schema_version"], "scene_factory.dataset.v1")
            self.assertEqual(metadata["status"], "complete")
            self.assertEqual(metadata["count"], 1)
            self.assertEqual(metadata["expected_seed_end"], 100)
            self.assertEqual(metadata["scene_count"], 1)
            self.assertEqual(set(row["files"]), {"scene_spec", "layout", "validation", "preview"})
            self.assertEqual(set(row["files"]), set(row["sha256"]))
            self.assertTrue(all(not Path(value).is_absolute() for value in row["files"].values()))
            self.assertTrue(all("\\" not in value for value in row["files"].values()))
            self.assertEqual(validate_dataset(directory)["result"], "passed")
            self.assertTrue(reproduce_dataset(directory)["valid"])

    def test_three_and_ten_scene_datasets_are_ordered(self) -> None:
        for count in (3, 10):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                self._build(directory, count=count, seed_start=200)
                rows = self._manifest(directory)
                self.assertEqual([row["seed"] for row in rows], list(range(200, 200 + count)))
                self.assertEqual(validate_dataset(directory)["result"], "passed")

    def test_one_hundred_seed_lightweight_regression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self._build(directory, count=100, seed_start=1000)
            result = validate_dataset(directory)
            self.assertTrue(result["valid"], result.to_dict())
            self.assertEqual(result["summary"]["scene_count"], 100)

    def test_semantic_fingerprint_is_independent_of_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            self._build(first, seed_start=301)
            self._build(second, seed_start=301)
            first_row = self._manifest(first)[0]
            second_row = self._manifest(second)[0]
            self.assertEqual(first_row["scene_id"], second_row["scene_id"])
            self.assertEqual(first_row["fingerprint"], second_row["fingerprint"])

    def test_keyword_prompt_dataset_reproduces_without_llm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.factory.build_batch(
                directory,
                count=2,
                seed_start=350,
                prompt="刚回家，背包和鞋在入口附近",
            )
            self.assertEqual(validate_dataset(directory)["result"], "passed")
            reproduction = reproduce_dataset(directory)
            self.assertEqual(reproduction["result"], "passed")

    def test_external_prompt_parser_is_explicitly_not_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.factory.build_batch(
                directory,
                count=1,
                seed_start=360,
                prompt="刚回家，背包和鞋在入口附近",
            )
            root = Path(directory)
            rows = self._manifest(root)
            rows[0]["prompt_parser"] = "llm:remote-model"
            (root / "manifest.jsonl").write_text(
                json.dumps(rows[0], sort_keys=True) + "\n", encoding="utf-8"
            )
            report = reproduce_dataset(directory)
            self.assertEqual(report["result"], "not_available")
            self.assertEqual(report["reason"], "nondeterministic_external_parser")

    def test_modified_artifact_and_path_fail_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self._build(directory)
            root = Path(directory)
            row = self._manifest(directory)[0]
            layout = root / row["files"]["layout"]
            layout.write_text(layout.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            result = validate_dataset(directory)
            self.assertFalse(result["valid"])
            self.assertTrue(any("layout" in error and "sha256" in error for error in result["errors"]))

            row["files"]["layout"] = "../escape/layout.json"
            (root / "manifest.jsonl").write_text(
                json.dumps(row, sort_keys=True) + "\n", encoding="utf-8"
            )
            result = validate_dataset(directory)
            self.assertFalse(result["valid"])
            self.assertTrue(any("safe relative path" in error for error in result["errors"]))

    def test_missing_and_extra_dataset_files_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self._build(directory)
            root = Path(directory)
            dataset_json = root / "dataset.json"
            original = dataset_json.read_text(encoding="utf-8")
            dataset_json.unlink()
            self.assertFalse(validate_dataset(directory)["valid"])
            dataset_json.write_text(original, encoding="utf-8")
            (root / "untracked-scene").mkdir()
            self.assertFalse(validate_dataset(directory)["valid"])

    def test_manifest_shape_and_seed_coverage_are_strict(self) -> None:
        mutations = {
            "missing_manifest": lambda root: (root / "manifest.jsonl").unlink(),
            "malformed_line": lambda root: (root / "manifest.jsonl").write_text(
                "{broken}\n", encoding="utf-8"
            ),
            "blank_line": lambda root: (root / "manifest.jsonl").write_text("\n", encoding="utf-8"),
            "duplicate_seed": lambda root: self._rewrite_manifest(root, lambda rows: rows[:1] + [rows[0]]),
            "missing_seed": lambda root: self._rewrite_manifest(root, lambda rows: rows[:1]),
            "extra_seed": lambda root: self._rewrite_manifest(
                root, lambda rows: rows[:2] + [{**rows[2], "seed": 9999}]
            ),
            "absolute_path": lambda root: self._rewrite_manifest(
                root, lambda rows: [{**rows[0], "files": {**rows[0]["files"], "layout": "C:/outside.json"}}]
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(mutation=name), tempfile.TemporaryDirectory() as directory:
                self._build(directory, count=3, seed_start=600)
                mutate(Path(directory))
                self.assertFalse(validate_dataset(directory)["valid"])

    @staticmethod
    def _rewrite_manifest(root: Path, transform) -> None:
        rows = [
            json.loads(line)
            for line in (root / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        (root / "manifest.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in transform(rows)),
            encoding="utf-8",
        )

    def test_failed_build_leaves_no_committed_scene_and_is_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calls = 0
            original_write = self.factory.write_result

            def fail_second(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("simulated export failure")
                return original_write(*args, **kwargs)

            with patch.object(self.factory, "write_result", side_effect=fail_second):
                with self.assertRaises(RuntimeError):
                    self._build(directory, count=3, seed_start=400)
            root = Path(directory)
            partial = self._manifest(directory)
            self.assertEqual([row["seed"] for row in partial], [400])
            committed_dirs = sorted(
                child.name
                for child in root.iterdir()
                if child.is_dir() and child.name != ".staging"
            )
            self.assertEqual(committed_dirs, [partial[0]["scene_id"]])
            self.assertEqual(json.loads((root / "dataset.json").read_text())['status'], "incomplete")
            self.assertTrue((root / ".staging").is_dir())
            existing_scene = root / partial[0]["scene_id"]
            existing_mtime = existing_scene.stat().st_mtime_ns

            resumed = self._build(directory, count=3, seed_start=400, resume=True)
            self.assertEqual(len(resumed), 3)
            self.assertEqual(validate_dataset(directory)["result"], "passed")
            self.assertEqual(existing_scene.stat().st_mtime_ns, existing_mtime)
            self.assertFalse((root / ".staging").exists())

    def test_resume_rejects_wrong_invocation_and_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self._build(directory, count=3, seed_start=500)
            with self.assertRaises(ValueError):
                self.factory.build_batch(
                    directory,
                    count=3,
                    seed_start=500,
                    recipe_name="living_room_returned_home",
                    resume=True,
                )
            row = self._manifest(directory)[0]
            (Path(directory) / row["files"]["preview"]).write_text("broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                self._build(directory, count=3, seed_start=500, resume=True)
            with self.assertRaises(ValueError):
                self.factory.build_batch(
                    directory,
                    count=3,
                    seed_start=500,
                    recipe_name="living_room_recent_snacking",
                    resume=True,
                )

    def test_invalid_scene_is_not_reported_as_passed(self) -> None:
        original_build = self.factory.build_from_recipe

        def invalid_build(recipe: str, seed: int) -> BuildResult:
            result = original_build(recipe, seed)
            return replace(result, validation=ValidationReport(False, (), {"forced": 1}))

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(self.factory, "build_from_recipe", side_effect=invalid_build):
                rows = self._build(directory)
            self.assertFalse(rows[0]["valid"])
            self.assertEqual(json.loads((Path(directory) / "dataset.json").read_text())["result"], "failed")
            self.assertFalse(validate_dataset(directory)["valid"])

    def test_no_isaac_import_is_needed_for_dataset_api(self) -> None:
        self.assertNotIn("isaacsim", __import__("sys").modules)
        self.assertNotIn("numpy", __import__("sys").modules)

    def test_inspect_supports_legacy_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.jsonl").write_text('{"scene_id":"legacy"}\n', encoding="utf-8")
            report = inspect_dataset(directory)
            self.assertEqual(report["result"], "legacy_dataset")
            self.assertTrue(report["upgrade_required"])


if __name__ == "__main__":
    unittest.main()
