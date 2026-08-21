from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scene_factory.registry import AssetRegistry


class RegistryAtomicUpdateTests(unittest.TestCase):
    def test_batch_upsert_adds_record_and_keeps_jsonl_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "registry.jsonl"
            path.write_text(json.dumps({"asset_id": "proxy", "category": "mug", "bbox_m": [1, 1, 1], "status": "validated"}) + "\n", encoding="utf-8")
            registry = AssetRegistry.load(path)
            registry.upsert_batch(
                [{"asset_id": "blocked", "category": "bowl", "bbox_m": [1, 1, 1], "mass": 1, "status": "rejected", "failure_reason": "no source"}],
                persist_path=path,
                batch_id="test_batch",
            )
            reloaded = AssetRegistry.load(path)
            self.assertEqual(reloaded.metadata("blocked").status, "rejected")
            self.assertEqual(reloaded.metadata("blocked").batch_id, "test_batch")
            self.assertEqual(reloaded.metadata("blocked").failure_reason, "no source")

    def test_ready_asset_cannot_be_downgraded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.jsonl"
            path.write_text(json.dumps({"asset_id": "ready", "category": "mug", "bbox_m": [1, 1, 1], "mass": 1, "status": "ready"}) + "\n", encoding="utf-8")
            registry = AssetRegistry.load(path)
            with self.assertRaises(ValueError):
                registry.upsert_batch([{"asset_id": "ready", "status": "rejected"}], persist_path=path)


if __name__ == "__main__":
    unittest.main()
