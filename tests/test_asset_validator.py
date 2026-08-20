from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scene_factory.asset_validator import validate_asset, validate_usd
from scene_factory.cli import main
from scene_factory.registry import AssetRegistry


class AssetValidatorTests(unittest.TestCase):
    def test_proxy_metadata_passes_without_pxr(self) -> None:
        registry = AssetRegistry.load(Path("data/assets/registry.jsonl"))
        report = validate_asset("mug_blue", registry)
        self.assertTrue(report["valid"], report)
        self.assertEqual(report["checks"]["metadata"]["mass"], 0.32)

    def test_missing_usd_and_invalid_physics_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry_path = root / "registry.jsonl"
            registry_path.write_text(
                json.dumps(
                    {
                        "asset_id": "bad",
                        "name": "Bad Asset",
                        "category": "box",
                        "usd_path": "missing.usda",
                        "mass": 0,
                        "friction": -0.1,
                        "status": "quarantine",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            report_file = root / "qa" / "bad.json"
            report = validate_asset("bad", AssetRegistry.load(registry_path), report_path=report_file)
            self.assertFalse(report["valid"])
            codes = {item["code"] for item in report["issues"]}
            self.assertTrue({"missing_or_invalid_mass", "invalid_friction", "missing_usd"} <= codes)
            self.assertEqual(json.loads(report_file.read_text(encoding="utf-8"))["asset_id"], "bad")

    def test_standalone_missing_usd_is_structured(self) -> None:
        report = validate_usd("F:/does-not-exist/asset.usda")
        self.assertFalse(report["valid"])
        self.assertEqual(report["issues"][0]["code"], "missing_usd")

    def test_asset_inspect_cli(self) -> None:
        self.assertEqual(main(["asset", "inspect", "--asset-id", "mug_blue"]), 0)


if __name__ == "__main__":
    unittest.main()
