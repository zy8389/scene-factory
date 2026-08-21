from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scene_factory.batch_ingestion import BatchAssetResult, run_independent_batch
from tools.ingest_assets import _copy_source_to_stage


class BatchResumeTests(unittest.TestCase):
    def test_ready_asset_is_reused_without_processor(self) -> None:
        config = {
            "batch_id": "resume_batch",
            "assets": [{"asset_id": "ready_1", "category": "mug", "collision_profile": "concave_container_l1", "validation_profile": "drop", "physics": {"mass_kg": 1}}],
        }

        def processor(_item: dict, _result: BatchAssetResult) -> BatchAssetResult:
            raise AssertionError("ready asset should not be processed")

        report = run_independent_batch(
            config,
            processor,
            existing={"ready_1": {"state": "ready", "qa_report": "qa.json"}},
        )
        self.assertEqual(report.ready, 1)
        self.assertTrue(report.assets["ready_1"].resumed)

    def test_stage_copy_preserves_outputs_until_force(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            stage = root / "stage"
            source.mkdir()
            (source / "model.glb").write_bytes(b"glTF source")
            stage.mkdir()
            (stage / "prior.usd").write_text("prior", encoding="ascii")
            _copy_source_to_stage(source, stage)
            self.assertTrue((stage / "prior.usd").is_file())
            _copy_source_to_stage(source, stage, force=True)
            self.assertFalse((stage / "prior.usd").exists())


if __name__ == "__main__":
    unittest.main()
