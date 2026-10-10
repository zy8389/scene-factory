from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scene_factory import (
    SceneFactory,
    build_sampling_collection,
    configuration_fingerprint,
    export_sampling_selection,
    split_sampling_collection,
    validate_curated_dataset,
    validate_sampling_split,
)
from scene_factory.cli import main
from scene_factory.curation import _preview_html
from scene_factory.dataset import sha256_file, write_json_atomic


class CurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.storage = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.storage.cleanup)
        cls.source = Path(cls.storage.name) / "source"
        with patch.dict(os.environ, {"SCENE_FACTORY_LLM_MODE": "off"}):
            cls.factory = SceneFactory()
        plan = {"schema_version": "scene_factory.sampling_plan.v1", "strata": [
            {"id": "lift", "recipe": "kitchen_franka_mug_lift_data", "quota": 4,
             "candidate_count": 4, "seed_start": 1000},
            {"id": "place", "recipe": "kitchen_franka_mug_pick_place_data", "quota": 4,
             "candidate_count": 4, "seed_start": 1000},
        ]}
        cls.collection = build_sampling_collection(plan, cls.source, factory=cls.factory)
        assert cls.collection.valid, cls.collection.to_dict()

    @staticmethod
    def _hashes(root: Path) -> dict:
        return {path.relative_to(root).as_posix(): sha256_file(path)
                for path in root.rglob("*") if path.is_file()}

    def test_configuration_key_ignores_task_names_ids_and_object_order(self) -> None:
        original = self.factory.build_from_recipe("kitchen_franka_mug_lift_data", 1000).scene.to_dict()
        variant = self.factory.build_from_recipe("kitchen_franka_mug_pick_place_data", 1000).scene.to_dict()
        self.assertEqual(configuration_fingerprint(original), configuration_fingerprint(variant))
        variant = copy.deepcopy(original)
        variant.update(scene_id="renamed", seed=900, recipe_name="different", event="different", task={"goal": "other"})
        variant["objects"] = list(reversed(variant["objects"]))
        for item in variant["objects"]:
            item["object_id"] = "renamed_" + item["object_id"]
            item["support"] = None
            item["relations"] = []
            item["pose"]["yaw_deg"] += 360.0
        self.assertEqual(configuration_fingerprint(original), configuration_fingerprint(variant))
        variant["objects"][0]["pose"]["position"] = [1.5, 0.0, 0.5]
        self.assertNotEqual(configuration_fingerprint(original), configuration_fingerprint(variant))

    def test_same_configuration_across_tasks_never_crosses_split_boundaries(self) -> None:
        report = split_sampling_collection(self.source, seed=42)
        self.assertTrue(report.valid, report.to_dict())
        self.assertEqual(report["summary"]["scene_count"], 8)
        self.assertEqual(report["summary"]["configuration_group_count"], 4)
        self.assertEqual(report["summary"]["group_counts"], {"train": 2, "validation": 1, "test": 1})
        groups = {}
        for assignment in report["assignments"]:
            groups.setdefault(assignment["configuration_fingerprint"], []).append(assignment)
        for members in groups.values():
            self.assertEqual(len(members), 2)
            self.assertEqual(len({item["split"] for item in members}), 1)
        self.assertTrue(validate_sampling_split(self.source, report.to_dict()).valid)

    def test_seed_and_directory_determinism_preserves_sources(self) -> None:
        before = self._hashes(self.source)
        first = split_sampling_collection(self.source, seed=42).to_dict()
        self.assertEqual(first, split_sampling_collection(self.source, seed=42).to_dict())
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "copied"
            shutil.copytree(self.source, copied)
            self.assertEqual(first, split_sampling_collection(copied, seed=42).to_dict())
        self.assertEqual(before, self._hashes(self.source))
        self.assertNotEqual(first["assignments"], split_sampling_collection(self.source, seed=43)["assignments"])

    def test_requested_nonempty_splits_and_small_group_shortage(self) -> None:
        report = split_sampling_collection(self.source, ratios=(0.98, 0.01, 0.01))
        self.assertTrue(report.valid)
        self.assertTrue(all(count >= 1 for count in report["summary"]["group_counts"].values()))
        plan = {"schema_version": "scene_factory.sampling_plan.v1", "strata": [
            {"id": "lift", "recipe": "kitchen_franka_mug_lift", "quota": 1, "candidate_count": 1, "seed_start": 0},
            {"id": "place", "recipe": "kitchen_franka_mug_pick_place", "quota": 1, "candidate_count": 1, "seed_start": 0},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            self.assertTrue(build_sampling_collection(plan, source, factory=self.factory).valid)
            rejected = split_sampling_collection(source)
            self.assertFalse(rejected.valid)
            self.assertEqual(rejected["assignments"], [])
            accepted = split_sampling_collection(source, ratios=(1, 0, 0))
            self.assertTrue(accepted.valid)
            self.assertEqual(accepted["summary"]["scene_counts"], {"train": 2, "validation": 0, "test": 0})
            output = Path(directory) / "package"
            self.assertFalse(export_sampling_selection(source, output).valid)
            self.assertFalse(output.exists())

    def test_bad_options_and_tampered_split_reports_are_rejected(self) -> None:
        for ratios in ((0, 0, 0), (0.7, 0.2, 0.2), (True, 0, 0), (float("nan"), 0, 1), (10 ** 400, 0, 0)):
            with self.subTest(ratios=ratios), self.assertRaises(ValueError):
                split_sampling_collection(self.source, ratios=ratios)
        with self.assertRaises(ValueError):
            split_sampling_collection(self.source, seed=True)
        report = split_sampling_collection(self.source).to_dict()
        report["assignments"][0]["split"] = "validation" if report["assignments"][0]["split"] != "validation" else "test"
        self.assertFalse(validate_sampling_split(self.source, report).valid)
        report["schema_version"] = "scene_factory.sampling_split.v99"
        self.assertFalse(validate_sampling_split(self.source, report).valid)
        approximate = split_sampling_collection(self.source, ratios=(0.1, 0.2, 0.7000000001))
        self.assertTrue(approximate.valid)
        self.assertTrue(validate_sampling_split(self.source, approximate.to_dict()).valid)

    def test_export_is_self_contained_portable_and_preserves_artifact_hashes(self) -> None:
        before = self._hashes(self.source)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "package"
            report = export_sampling_selection(self.source, target, seed=42)
            self.assertTrue(report.valid, report.to_dict())
            manifest = json.loads((target / "manifest.json").read_text("utf-8"))
            originals = {(entry["stratum_id"], entry["record"]["scene_id"]): entry["record"] for entry in self.collection["selected"]}
            for entry in manifest["entries"]:
                record = entry["record"]
                original = originals[(entry["stratum_id"], record["scene_id"])]
                self.assertEqual(record["sha256"], original["sha256"])
                self.assertEqual(record["fingerprint"], original["fingerprint"])
                self.assertEqual(record["quality_report"]["sha256"], original["quality_report"]["sha256"])
            shutil.copytree(target, root / "moved")
            with patch("scene_factory.curation.validate_sampling_collection", side_effect=AssertionError("source access")):
                self.assertTrue(validate_curated_dataset(root / "moved").valid)
            self.assertNotIn(str(self.source), (target / "manifest.json").read_text("utf-8"))
            self.assertTrue((target / "index.html").is_file())
        self.assertEqual(before, self._hashes(self.source))

    def test_export_copies_only_selected_records(self) -> None:
        plan = {"schema_version": "scene_factory.sampling_plan.v1", "strata": [
            {"id": "lift", "recipe": "kitchen_franka_mug_lift_data", "quota": 3, "candidate_count": 6, "seed_start": 1000},
            {"id": "place", "recipe": "kitchen_franka_mug_pick_place_data", "quota": 3, "candidate_count": 6, "seed_start": 1000},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertTrue(build_sampling_collection(plan, root / "source", factory=self.factory).valid)
            exported = export_sampling_selection(root / "source", root / "package")
            self.assertTrue(exported.valid)
            self.assertEqual(len(list((root / "package/scenes").glob("*/*/layout.json"))), 6)

    def test_corruption_extra_files_and_split_leakage_in_export_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertTrue(export_sampling_selection(self.source, root / "original").valid)
            for mutation in ("artifact", "extra", "split", "group", "index", "version"):
                with self.subTest(mutation=mutation):
                    target = root / mutation
                    shutil.copytree(root / "original", target)
                    manifest = json.loads((target / "manifest.json").read_text("utf-8"))
                    if mutation == "artifact":
                        (target / manifest["entries"][0]["record"]["files"]["preview"]).write_text("corrupted", encoding="utf-8")
                    elif mutation == "extra":
                        (target / "untracked.json").write_text("{}", encoding="utf-8")
                    elif mutation == "split":
                        entry = manifest["entries"][0]
                        entry["split"] = "test" if entry["split"] != "test" else "train"
                    elif mutation == "group":
                        manifest["entries"][0]["configuration_fingerprint"] = "0" * 64
                    elif mutation == "index":
                        (target / "index.html").write_text("changed", encoding="utf-8")
                    else:
                        manifest["schema_version"] = "scene_factory.curated_dataset.v99"
                    write_json_atomic(target / "manifest.json", manifest)
                    self.assertFalse(validate_curated_dataset(target).valid)

    def test_existing_outputs_source_paths_and_mid_export_failure_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / "existing"
            existing.mkdir()
            sentinel = existing / "sentinel.txt"
            sentinel.write_text("keep", encoding="utf-8")
            with self.assertRaises(ValueError):
                export_sampling_selection(self.source, existing)
            with self.assertRaises(ValueError):
                export_sampling_selection(self.source, self.source / "new_package")
            target = root / "failed"
            with patch("scene_factory.curation.os.rename", side_effect=OSError("injected publish failure")):
                report = export_sampling_selection(self.source, target)
            self.assertFalse(report.valid)
            self.assertFalse(target.exists())
            self.assertTrue(Path(report["staging_path"]).is_dir())
            self.assertEqual(sentinel.read_text("utf-8"), "keep")

    def test_preview_escapes_labels_and_exposes_quality_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "package"
            self.assertTrue(export_sampling_selection(self.source, target).valid)
            manifest = json.loads((target / "manifest.json").read_text("utf-8"))
            manifest["entries"][0]["record"]["recipe"] = '<script>alert("injection")</script>'
            preview = _preview_html(manifest).decode("utf-8")
            self.assertNotIn("<script>", preview)
            self.assertIn("&lt;script&gt;", preview)
            self.assertIn("机器人可达性和任务成功尚未验证", preview)

    def test_cli_split_export_and_standalone_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            split_path = root / "split.json"
            package = root / "package"
            self.assertEqual(main(["dataset", "sampling-split", str(self.source), "--output", str(split_path), "--seed", "42"]), 0)
            self.assertEqual(main(["dataset", "sampling-split-validate", str(self.source), str(split_path)]), 0)
            self.assertEqual(main(["dataset", "sampling-export", str(self.source), "--output", str(package), "--seed", "42"]), 0)
            self.assertEqual(main(["dataset", "curated-validate", str(package)]), 0)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["dataset", "sampling-split", str(self.source), "--output", str(self.source / "collection.json")]), 1)

    def test_curation_import_requires_no_optional_runtime(self) -> None:
        result = subprocess.run([sys.executable, "-c", "import sys; import scene_factory.curation; "
                                 "assert not ({'numpy', 'mujoco', 'isaacsim', 'pxr', 'gymnasium'} & set(sys.modules))"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
