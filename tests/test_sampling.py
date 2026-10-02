from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from scene_factory import SceneFactory
from scene_factory.cli import main
from scene_factory.dataset import sha256_file, validate_dataset, write_json_atomic
from scene_factory.sampling import (
    SAMPLING_PLAN_SCHEMA_VERSION,
    build_sampling_collection,
    load_sampling_plan,
    normalize_sampling_plan,
    reproduce_sampling_collection,
    validate_sampling_collection,
)


class SamplingCollectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with patch.dict(os.environ, {"SCENE_FACTORY_LLM_MODE": "off"}):
            cls.factory = SceneFactory()

    @staticmethod
    def _plan() -> dict:
        return {
            "schema_version": SAMPLING_PLAN_SCHEMA_VERSION,
            "strata": [
                {"id": "left", "recipe": "kitchen_franka_mug_lift_data", "quota": 2,
                 "candidate_count": 12, "seed_start": 1000,
                 "filter": {"object_id": "mug_1", "region_xy": [0.56, 0.60, -0.08, 0.02]}},
                {"id": "right", "recipe": "kitchen_franka_mug_lift_data", "quota": 2,
                 "candidate_count": 12, "seed_start": 2000,
                 "filter": {"object_id": "mug_1", "region_xy": [0.60, 0.64, -0.08, 0.02]}},
            ],
        }

    def _build(self, root: Path, plan: dict | None = None, **arguments):
        return build_sampling_collection(plan or self._plan(), root, factory=self.factory, **arguments)

    @staticmethod
    def _hashes(root: Path) -> dict:
        return {path.relative_to(root).as_posix(): sha256_file(path)
                for path in root.rglob("*") if path.is_file()}

    def test_quotas_filter_bounds_global_uniqueness_and_existing_child_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "collection"
            report = self._build(root)
            self.assertTrue(report.valid, report.to_dict())
            self.assertEqual(report["summary"], {"candidate_count": 24, "quota": 4,
                                                 "qualified_count": 4, "selected_count": 4})
            self.assertEqual(report["coverage"]["recipe_counts"], {"kitchen_franka_mug_lift_data": 4})
            self.assertEqual(len({entry["content_fingerprint"] for entry in report["selected"]}), 4)
            for stratum in report["strata"]:
                self.assertTrue(stratum["quota_met"])
                self.assertEqual(stratum["selected_count"], 2)
                self.assertTrue(validate_dataset(root / stratum["dataset_path"]).valid)
            for selected in report["selected"]:
                record = selected["record"]
                layout = json.loads((root / selected["dataset_path"] / record["files"]["layout"]).read_text("utf-8"))
                mug = next(item for item in layout["objects"] if item["object_id"] == "mug_1")
                bounds = [entry for entry in self._plan()["strata"] if entry["id"] == selected["stratum_id"]][0]["filter"]["region_xy"]
                self.assertTrue(bounds[0] <= mug["pose"]["position"][0] < bounds[1])
            self.assertTrue(validate_sampling_collection(root).valid)
            self.assertTrue(reproduce_sampling_collection(root).valid)

    def test_resume_and_validation_preserve_candidate_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initial = self._build(root).to_dict()
            before = self._hashes(root)
            mtimes = {path: (root / path).stat().st_mtime_ns for path in before if path.startswith("datasets/")}
            resumed = self._build(root, resume=True).to_dict()
            self.assertEqual(initial, resumed)
            self.assertEqual(before, self._hashes(root))
            self.assertEqual(mtimes, {path: (root / path).stat().st_mtime_ns for path in mtimes})
            self.assertTrue(validate_sampling_collection(root).valid)
            self.assertEqual(before, self._hashes(root))

    def test_collection_is_portable_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._build(root / "first").to_dict()
            second = self._build(root / "second").to_dict()
            self.assertEqual(first, second)
            shutil.copytree(root / "first", root / "copied")
            self.assertTrue(validate_sampling_collection(root / "copied").valid)

    def test_missing_physical_evidence_releases_no_selection(self) -> None:
        plan = self._plan()
        plan["minimum_level"] = "task"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = self._build(root, plan)
            self.assertFalse(report.valid)
            self.assertEqual(report["selected"], [])
            self.assertEqual(report["summary"]["selected_count"], 0)
            self.assertTrue(all(not entry["quota_met"] for entry in report["strata"]))
            self.assertEqual(report["strata"][0]["rejection_counts"]["physics_not_verified"], 12)
            self.assertFalse(validate_sampling_collection(root).valid)

    def test_duplicate_layout_shortfall_blocks_the_entire_collection(self) -> None:
        plan = self._plan()
        plan["strata"][0] = {"id": "fixed", "recipe": "kitchen_franka_mug_lift",
                             "quota": 2, "candidate_count": 3, "seed_start": 300}
        with tempfile.TemporaryDirectory() as directory:
            report = self._build(Path(directory), plan)
            self.assertFalse(report.valid)
            self.assertEqual(report["selected"], [])
            self.assertEqual(report["strata"][0]["available_count"], 1)
            self.assertEqual(report["strata"][0]["rejection_counts"], {"duplicate_layout": 2})
            self.assertTrue(report["strata"][1]["quota_met"])
            self.assertEqual(report["strata"][1]["selected_count"], 0)

    def test_duplicates_across_nonoverlapping_seed_ranges_do_not_fill_two_quotas(self) -> None:
        plan = {"schema_version": SAMPLING_PLAN_SCHEMA_VERSION, "strata": [
            {"id": "first", "recipe": "kitchen_franka_mug_lift", "quota": 1, "candidate_count": 2, "seed_start": 300},
            {"id": "second", "recipe": "kitchen_franka_mug_lift", "quota": 1, "candidate_count": 2, "seed_start": 400},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            report = self._build(Path(directory), plan)
            self.assertFalse(report.valid)
            self.assertEqual(report["selected"], [])
            self.assertEqual(report["strata"][1]["rejection_counts"]["duplicate_across_strata"], 1)

    def test_failed_generation_is_resumable_without_rewriting_completed_candidates(self) -> None:
        original = self.factory.build_from_recipe

        def fail_once(recipe: str, seed: int):
            if seed == 2001:
                raise RuntimeError("injected generation failure")
            return original(recipe, seed)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(self.factory, "build_from_recipe", side_effect=fail_once):
                failed = self._build(root)
            self.assertFalse(failed.valid)
            self.assertEqual(failed["reason"], "candidate_generation_failed")
            self.assertEqual(failed["selected"], [])
            completed = root / "datasets/left/manifest.jsonl"
            before_hash, before_mtime = sha256_file(completed), completed.stat().st_mtime_ns
            self.assertTrue(self._build(root, resume=True).valid)
            self.assertEqual((sha256_file(completed), completed.stat().st_mtime_ns), (before_hash, before_mtime))
            self.assertTrue(validate_sampling_collection(root).valid)

    def test_changed_plan_or_source_is_rejected_before_resume_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._build(root)
            before = self._hashes(root)
            changed = self._plan()
            changed["strata"][0]["quota"] = 1
            with self.assertRaises(ValueError):
                self._build(root, changed, resume=True)
            original_get = self.factory.recipes.get

            def changed_recipe(name: str):
                return replace(original_get(name), description="changed source")

            with patch.object(self.factory.recipes, "get", side_effect=changed_recipe):
                with self.assertRaisesRegex(ValueError, "source fingerprints changed"):
                    self._build(root, resume=True)
            self.assertEqual(before, self._hashes(root))

    def test_tampered_selection_candidate_hash_and_unknown_versions_fail_closed(self) -> None:
        for mutation in ("selection", "candidate", "version", "extra_file", "candidate_path"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self._build(root)
                report = json.loads((root / "collection.json").read_text("utf-8"))
                if mutation == "selection":
                    report["selected"][0]["stratum_id"] = "right"
                elif mutation == "version":
                    report["schema_version"] = "scene_factory.sampling_collection.v99"
                elif mutation == "candidate_path":
                    report["selected"][0]["dataset_path"] = "../outside"
                elif mutation == "extra_file":
                    (root / "unexpected.json").write_text("{}", encoding="utf-8")
                else:
                    selected = report["selected"][0]
                    (root / selected["dataset_path"] / selected["record"]["files"]["preview"]).write_text("broken", encoding="utf-8")
                write_json_atomic(root / "collection.json", report)
                before = self._hashes(root)
                validation = validate_sampling_collection(root)
                self.assertFalse(validation.valid)
                self.assertEqual(validation["selected"], [])
                with self.assertRaises(ValueError):
                    self._build(root, resume=True)
                self.assertEqual(before, self._hashes(root))

    def test_unknown_recipe_or_filter_object_is_rejected_before_output_creation(self) -> None:
        for mutation in ("recipe", "object_id"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "new"
                plan = self._plan()
                if mutation == "recipe":
                    plan["strata"][0]["recipe"] = "missing_recipe"
                else:
                    plan["strata"][0]["filter"]["object_id"] = "missing_object"
                with self.assertRaises((ValueError, KeyError)):
                    self._build(root, plan)
                self.assertFalse(root.exists())

    def test_redirected_dataset_directory_is_rejected_before_validation_or_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "collection"
            self._build(root)
            original_resolve = Path.resolve

            def redirected(path, *arguments, **keywords):
                if path == root / "datasets":
                    return original_resolve(Path(directory) / "outside")
                return original_resolve(path, *arguments, **keywords)

            before = self._hashes(root)
            with patch.object(Path, "resolve", autospec=True, side_effect=redirected):
                self.assertFalse(validate_sampling_collection(root).valid)
                with self.assertRaisesRegex(ValueError, "unexpected collection entry"):
                    self._build(root, resume=True)
            self.assertEqual(before, self._hashes(root))

    def test_invalid_plans_reject_typoes_unsafe_names_overlap_and_nonfinite_bounds(self) -> None:
        mutations = []
        for field, value in (("schema_version", "v99"), ("minimum_level", "training"), ("strata", []), ("typo", True)):
            plan = self._plan()
            plan[field] = value
            mutations.append(plan)
        for field, value in (("id", "../escape"), ("id", "CON"), ("quota", True), ("quota", 13), ("candidate_count", 0), ("seed_start", False)):
            plan = self._plan()
            plan["strata"][0][field] = value
            mutations.append(plan)
        for bounds in ([0.6, 0.5, 0.0, 0.1], [0.5, float("nan"), 0.0, 0.1], [0.5, 10 ** 400, 0.0, 0.1]):
            plan = self._plan()
            plan["strata"][0]["filter"]["region_xy"] = bounds
            mutations.append(plan)
        duplicate = self._plan()
        duplicate["strata"][1]["id"] = "LEFT"
        mutations.append(duplicate)
        overlap = self._plan()
        overlap["strata"][1]["seed_start"] = 1001
        mutations.append(overlap)
        for plan in mutations:
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                normalize_sampling_plan(plan)

    def test_half_open_regions_assign_boundary_to_only_one_stratum(self) -> None:
        plan = self._plan()
        for index, entry in enumerate(plan["strata"]):
            entry.update(recipe="kitchen_franka_mug_lift", quota=1, candidate_count=1,
                         seed_start=300 + index)
        plan["strata"][0]["filter"]["region_xy"] = [0.50, 0.56, -0.10, 0.10]
        plan["strata"][1]["filter"]["region_xy"] = [0.56, 0.60, -0.10, 0.10]
        with tempfile.TemporaryDirectory() as directory:
            report = self._build(Path(directory), plan)
            self.assertFalse(report.valid)
            self.assertEqual(report["strata"][0]["matched_count"], 0)
            self.assertEqual(report["strata"][1]["matched_count"], 1)

    def test_cli_bom_plan_generation_validation_reproduction_and_shortfall_codes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(self._plan()), encoding="utf-8-sig")
            self.assertEqual(load_sampling_plan(plan_path), normalize_sampling_plan(self._plan()))
            with patch.dict(os.environ, {"SCENE_FACTORY_LLM_MODE": "off"}), contextlib.redirect_stdout(io.StringIO()):
                output = root / "collection"
                self.assertEqual(main(["dataset", "sample", str(plan_path), "--output", str(output)]), 0)
                self.assertEqual(main(["dataset", "sampling-validate", str(output)]), 0)
                self.assertEqual(main(["dataset", "sampling-reproduce", str(output)]), 0)
                self.assertEqual(main(["dataset", "sample", str(plan_path), "--output", str(output), "--resume"]), 0)
                plan = self._plan()
                plan["minimum_level"] = "physics"
                write_json_atomic(plan_path, plan)
                self.assertEqual(main(["dataset", "sample", str(plan_path), "--output", str(root / "rejected")]), 2)

    def test_sampling_import_requires_no_optional_runtime(self) -> None:
        process = subprocess.run([
            sys.executable, "-c", "import sys; import scene_factory.sampling; "
            "assert not ({'numpy', 'mujoco', 'isaacsim', 'pxr', 'gymnasium'} & set(sys.modules))",
        ], capture_output=True, text=True, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)


if __name__ == "__main__":
    unittest.main()
