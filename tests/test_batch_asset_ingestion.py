from __future__ import annotations

import unittest

from scene_factory.batch_ingestion import BatchAssetResult, run_independent_batch


class BatchAssetIngestionTests(unittest.TestCase):
    def test_one_failure_does_not_stop_other_assets(self) -> None:
        config = {
            "batch_id": "test_batch",
            "assets": [
                {"asset_id": "ok_1", "category": "mug", "collision_profile": "concave_container_l1", "validation_profile": "drop", "physics": {"mass_kg": 1}},
                {"asset_id": "bad_1", "category": "mug", "collision_profile": "concave_container_l1", "validation_profile": "drop", "physics": {"mass_kg": 1}},
                {"asset_id": "ok_2", "category": "mug", "collision_profile": "concave_container_l1", "validation_profile": "drop", "physics": {"mass_kg": 1}},
            ],
        }

        def processor(item: dict, result: BatchAssetResult) -> BatchAssetResult:
            if item["asset_id"] == "bad_1":
                raise RuntimeError("conversion failed")
            result.transition("ready")
            return result

        report = run_independent_batch(config, processor)
        self.assertEqual(report.ready, 2)
        self.assertEqual(report.failed, 1)
        self.assertEqual(report.result, "partial")
        self.assertEqual(report.assets["bad_1"].issues[0]["code"], "asset_processing_failed")


if __name__ == "__main__":
    unittest.main()
