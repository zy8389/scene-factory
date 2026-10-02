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
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from scene_factory.cli import main
from scene_factory.dataset import (
    audit_dataset,
    reproduce_dataset,
    semantic_fingerprint,
    sha256_file,
    validate_dataset,
    write_json_atomic,
    write_manifest_atomic,
)
from scene_factory.factory import SceneFactory
from scene_factory.models import ValidationIssue, ValidationReport
from scene_factory.webapp import SceneWebApplication


class DatasetQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with patch.dict(os.environ, {"SCENE_FACTORY_LLM_MODE": "off"}):
            cls.factory = SceneFactory()

    def _build(self, root: Path, count: int = 3) -> list[dict]:
        return self.factory.build_batch(
            root, count=count, seed_start=100, recipe_name="living_room_recent_snacking"
        )

    def test_new_artifact_is_hashed_bundled_and_visible_in_web_payload(self) -> None:
        result = self.factory.build_from_recipe("living_room_recent_snacking", 100)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = self.factory.write_result(result, root / "scene")
            self.assertEqual(json.loads(Path(files["quality"]).read_text("utf-8")), result.quality)
            with zipfile.ZipFile(files["bundle"]) as archive:
                self.assertEqual(json.loads(archive.read("scene/quality.json")), result.quality)
                self.assertIn("quality", json.loads(archive.read("manifest.json"))["artifacts"])
            with patch.dict(os.environ, {"SCENE_FACTORY_LLM_MODE": "off"}):
                app = SceneWebApplication(root / "web")
            payload = app._result_payload(result, files)
            self.assertEqual(payload["quality"], result.quality)
            self.assertIn("quality", payload["files"])
            rows = self._build(root / "dataset", count=1)
            self.assertEqual(
                rows[0]["quality_report"]["sha256"],
                sha256_file(root / "dataset" / rows[0]["quality_report"]["path"]),
            )

    def test_layout_selection_and_diversity_do_not_claim_simulation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = self._build(root)
            report = audit_dataset(root).to_dict()
            self.assertTrue(report["valid"], report)
            self.assertEqual(report["selected"], rows)
            self.assertEqual(report["summary"]["selected_count"], 3)
            self.assertEqual(report["summary"]["layer_counts"]["physics"]["not_verified"], 3)
            self.assertEqual(report["summary"]["layer_counts"]["task"]["passed"], 0)
            self.assertEqual(report["diversity"]["all"]["recipe_counts"], {"living_room_recent_snacking": 3})
            self.assertGreater(report["diversity"]["selected"]["unique_asset_count"], 0)
            for level in ("physics", "task"):
                strict = audit_dataset(root, minimum_level=level).to_dict()
                self.assertFalse(strict["valid"])
                self.assertEqual(strict["reason"], "no_eligible_scenes")
                self.assertEqual(strict["selected"], [])
                self.assertEqual(strict["summary"]["rejection_counts"]["physics_not_verified"], 3)
                self.assertEqual(strict["diversity"]["selected"]["scene_count"], 0)

    def test_mixed_quality_selects_valid_scenes_and_counts_reasons_per_scene(self) -> None:
        original_build = self.factory.build_from_recipe

        def varied_build(recipe: str, seed: int):
            result = original_build(recipe, seed)
            if seed == 101:
                issue = ValidationIssue("overlap", "overlap", ("cup", "plate"))
                return replace(result, validation=ValidationReport(False, (issue, issue), {}))
            if seed == 102:
                warning = ValidationIssue("proxy_asset", "proxy geometry", (), "warning")
                return replace(result, validation=ValidationReport(True, (warning,), {}))
            return result

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(self.factory, "build_from_recipe", side_effect=varied_build):
                self._build(root)
            self.assertFalse(validate_dataset(root).valid)
            report = audit_dataset(root)
            self.assertTrue(report.valid)
            self.assertEqual([row["seed"] for row in report["selected"]], [100, 102])
            self.assertEqual(report["summary"]["failure_counts"], {"overlap": 1})
            self.assertEqual(report["summary"]["failure_category_counts"], {"overlap": 1})
            self.assertEqual(report["summary"]["warning_counts"], {"proxy_asset": 1})

    def test_deduplication_ignores_seed_but_keeps_first_eligible_scene(self) -> None:
        base = self.factory.build_from_recipe("living_room_recent_snacking", 100)

        def same_layout(recipe: str, seed: int):
            scene = replace(base.scene, scene_id=f"same_layout_{seed}", seed=seed)
            validation = base.validation if seed != 100 else ValidationReport(False, (), {})
            return replace(base, scene=scene, validation=validation)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(self.factory, "build_from_recipe", side_effect=same_layout):
                rows = self._build(root)
            self.assertEqual(len({row["fingerprint"] for row in rows}), 3)
            self.assertEqual(len(audit_dataset(root)["selected"]), 2)
            report = audit_dataset(root, deduplicate=True)
            self.assertTrue(report.valid)
            self.assertEqual([row["seed"] for row in report["selected"]], [101])
            self.assertEqual(report["summary"]["eligible_count"], 2)
            self.assertEqual(report["summary"]["rejection_counts"], {
                "duplicate_layout": 1, "layout_validation_failed": 1,
            })
            self.assertEqual(report["scenes"][2]["duplicate_of"], "same_layout_101")
            self.assertEqual(report["diversity"]["all"]["unique_layout_count"], 1)
            self.assertEqual(len(report["diversity"]["exact_duplicate_groups"]), 1)

    def test_existing_v1_without_quality_still_validates_reproduces_and_audits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = self._build(root, count=1)
            fingerprint = rows[0]["fingerprint"]
            (root / rows[0].pop("quality_report")["path"]).unlink()
            write_manifest_atomic(root / "manifest.jsonl", rows)
            self.assertTrue(validate_dataset(root).valid)
            self.assertTrue(reproduce_dataset(root).valid)
            report = audit_dataset(root)
            self.assertTrue(report.valid)
            self.assertEqual(report["selected"][0]["fingerprint"], fingerprint)
            self.assertNotIn("quality", report["selected"][0]["files"])
            self.assertNotIn("quality_report", report["selected"][0])

    def test_corruption_and_forged_quality_fail_closed(self) -> None:
        for mutation in ("file_hash", "forged_quality", "missing_file", "path_escape", "descriptor"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                rows = self._build(root, count=1)
                quality_path = root / rows[0]["quality_report"]["path"]
                if mutation == "file_hash":
                    quality_path.write_text("{}", encoding="utf-8")
                elif mutation == "forged_quality":
                    quality = json.loads(quality_path.read_text("utf-8"))
                    quality["layers"]["physics"]["status"] = "passed"
                    write_json_atomic(quality_path, quality)
                    rows[0]["quality_report"]["sha256"] = sha256_file(quality_path)
                elif mutation == "missing_file":
                    quality_path.unlink()
                elif mutation == "path_escape":
                    rows[0]["quality_report"]["path"] = "../outside.json"
                else:
                    rows[0]["quality_report"] = None
                write_manifest_atomic(root / "manifest.jsonl", rows)
                report = audit_dataset(root)
                self.assertFalse(report.valid)
                self.assertEqual(report["reason"], "dataset_integrity_failed")
                self.assertEqual(report["selected"], [])
                self.assertFalse(validate_dataset(root).valid)

    def test_incomplete_dataset_cannot_be_selected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._build(root)
            metadata = json.loads((root / "dataset.json").read_text("utf-8"))
            metadata["status"] = "incomplete"
            metadata["result"] = "incomplete"
            write_json_atomic(root / "dataset.json", metadata)
            report = audit_dataset(root)
            self.assertFalse(report.valid)
            self.assertEqual(report["selected"], [])

    def test_generation_failure_reports_missing_seeds_without_selecting_prefix(self) -> None:
        original_build = self.factory.build_from_recipe

        def failing_build(recipe: str, seed: int):
            if seed == 101:
                raise RuntimeError("placement failed")
            return original_build(recipe, seed)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(self.factory, "build_from_recipe", side_effect=failing_build):
                with self.assertRaises(RuntimeError):
                    self._build(root)
            report = audit_dataset(root)
            self.assertFalse(report.valid)
            self.assertEqual(report["selected"], [])
            self.assertEqual(report["generation"], {
                "status": "incomplete", "error_type": "RuntimeError",
                "expected_count": 3, "generated_count": 1, "missing_seeds": [101, 102],
            })

    def test_malformed_layout_is_rejected_even_with_updated_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = self._build(root, count=1)
            layout_path = root / rows[0]["files"]["layout"]
            layout = json.loads(layout_path.read_text("utf-8"))
            layout["objects"][0]["pose"] = []
            write_json_atomic(layout_path, layout)
            rows[0]["sha256"]["layout"] = sha256_file(layout_path)
            rows[0]["fingerprint"] = semantic_fingerprint({
                name: root / path for name, path in rows[0]["files"].items()
            })
            write_manifest_atomic(root / "manifest.jsonl", rows)
            report = audit_dataset(root)
            self.assertFalse(report.valid)
            self.assertEqual(report["reason"], "dataset_integrity_failed")
            self.assertEqual(report["selected"], [])

    def test_audit_is_portable_deterministic_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            self._build(source)
            before = {path.relative_to(source): sha256_file(path) for path in source.rglob("*") if path.is_file()}
            first = audit_dataset(source, deduplicate=True).to_dict()
            shutil.copytree(source, root / "copy")
            second = audit_dataset(root / "copy", deduplicate=True).to_dict()
            self.assertEqual(first, second)
            self.assertEqual(first, audit_dataset(source, deduplicate=True).to_dict())
            after = {path.relative_to(source): sha256_file(path) for path in source.rglob("*") if path.is_file()}
            self.assertEqual(before, after)

    def test_cli_writes_external_report_and_rejects_unverified_or_nested_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "dataset"
            self._build(source, count=1)
            output = root / "reports" / "audit.json"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["dataset", "audit", str(source), "--output", str(output)]), 0)
                self.assertEqual(main(["dataset", "audit", str(source), "--minimum-level", "task"]), 2)
            self.assertEqual(json.loads(output.read_text("utf-8"))["summary"]["selected_count"], 1)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main([
                    "dataset", "audit", str(source), "--output", str(source / "manifest.jsonl"),
                ]), 1)
            self.assertTrue(validate_dataset(source).valid)

    def test_no_optional_runtime_import_is_needed(self) -> None:
        process = subprocess.run([
            sys.executable, "-c",
            "import sys; from scene_factory import audit_dataset, assess_scene_quality; "
            "assert not ({'isaacsim', 'numpy', 'mujoco', 'gymnasium', 'pxr'} & set(sys.modules))",
        ], capture_output=True, text=True, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)


if __name__ == "__main__":
    unittest.main()
