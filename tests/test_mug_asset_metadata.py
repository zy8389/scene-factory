from __future__ import annotations

import json
import unittest
from pathlib import Path


class MugAssetMetadataTests(unittest.TestCase):
    def test_real_metadata_records_ready_asset_and_l1_limits(self) -> None:
        metadata = json.loads(
            Path("data/assets/metadata/mug_001.json").read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["status"], "ready")
        self.assertEqual(metadata["license"], "CC BY 4.0")
        self.assertTrue(metadata["hash"].startswith("sha256:"))
        self.assertEqual(metadata["collision_level"], "L1")
        self.assertIn("not validated for containment tasks", metadata["limitations"])

    def test_template_declares_source_and_explicit_physics_defaults(self) -> None:
        metadata = json.loads(
            Path("data/assets/metadata/mug_001.template.json").read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["source_asset"], "YCB 025_mug")
        self.assertEqual(metadata["up_axis"], "Z")
        self.assertEqual(metadata["meters_per_unit"], 1.0)
        self.assertEqual(metadata["physics_parameters_source"], "project_default")
        self.assertEqual(metadata["collision_level"], "L1")
        self.assertFalse(metadata["normalized"])
        self.assertEqual(metadata["status"], "raw")
        self.assertIsNone(metadata["hash"])


if __name__ == "__main__":
    unittest.main()
