from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scene_factory.registry import AssetLoader, AssetRegistry


class AssetRegistryV2Tests(unittest.TestCase):
    def _write_registry(self, root: Path, *records: dict) -> Path:
        path = root / "registry.jsonl"
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        return path

    def test_v2_metadata_and_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "usd").mkdir()
            (root / "collision").mkdir()
            (root / "usd" / "mug.usda").write_text("#usda 1.0\n", encoding="utf-8")
            (root / "collision" / "mug.usda").write_text("#usda 1.0\n", encoding="utf-8")
            registry = AssetRegistry.load(
                self._write_registry(
                    root,
                    {
                        "asset_id": "mug",
                        "name": "Blue Mug",
                        "category": "mug",
                        "hash": "sha256:test",
                        "usd_path": "usd/mug.usda",
                        "collision_path": "collision/mug.usda",
                        "mass": 0.3,
                        "friction": 0.5,
                        "support_surface": {
                            "name": "rim",
                            "center": [0, 0, 0.05],
                            "size": [0.04, 0.04],
                        },
                        "status": "quarantine",
                    },
                )
            )
            asset = registry.get("mug")
            self.assertEqual(asset.name, "Blue Mug")
            self.assertEqual(registry.metadata("mug").hash, "sha256:test")
            self.assertEqual(registry.metadata("mug").support_surface[0].name, "rim")
            loader = AssetLoader(registry)
            self.assertTrue(loader.load("mug", require_collision=True).usd_path.endswith("mug.usda"))
            self.assertTrue(registry.validate()["valid"])
            self.assertEqual(registry.list(statuses=["quarantine"])[0].asset_id, "mug")

    def test_legacy_proxy_records_remain_compatible(self) -> None:
        registry = AssetRegistry.load(Path("data/assets/registry.jsonl"))
        self.assertEqual(len(registry), 20)
        self.assertEqual(registry.get("mug_blue").source_type, "primitive")
        self.assertEqual(registry.resolve_collision_path(registry.get("mug_blue")), None)
        self.assertEqual(registry.validate()["issues"], [])

    def test_duplicate_and_invalid_status_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = {
                "asset_id": "same",
                "category": "box",
                "bbox_m": [1, 1, 1],
                "mass_kg": 1,
                "friction": 0.5,
                "status": "validated",
            }
            with self.assertRaises(ValueError, msg="duplicate asset ID"):
                AssetRegistry.load(self._write_registry(root, record, record))
            invalid = {**record, "asset_id": "bad", "status": "broken"}
            with self.assertRaises(ValueError):
                AssetRegistry.load(self._write_registry(root, invalid))

    def test_unknown_asset_and_missing_files_are_clear(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = AssetRegistry.load(
                self._write_registry(
                    root,
                    {
                        "asset_id": "missing",
                        "category": "box",
                        "bbox_m": [1, 1, 1],
                        "mass": 1,
                        "friction": 0.5,
                        "usd_path": "nope.usda",
                        "status": "quarantine",
                    },
                )
            )
            with self.assertRaises(KeyError):
                registry.get("unknown")
            with self.assertRaises(FileNotFoundError):
                AssetLoader(registry).load("missing")

    def test_registry_update_and_save_preserve_v2_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "mug.usda").write_text("#usda 1.0\n", encoding="utf-8")
            registry_path = self._write_registry(
                root,
                {
                    "asset_id": "mug",
                    "category": "mug",
                    "bbox_m": [0.1, 0.1, 0.12],
                    "usd_path": "mug.usda",
                    "mass": 0.3,
                    "static_friction": 0.5,
                    "dynamic_friction": 0.4,
                    "friction": 0.4,
                    "collision_enabled": False,
                    "status": "normalized",
                },
            )
            registry = AssetRegistry.load(registry_path)
            registry.update("mug", {"status": "ready", "qa_report": "qa/mug.json"})
            saved = root / "saved.jsonl"
            registry.save(saved)
            reloaded = AssetRegistry.load(saved)
            self.assertEqual(reloaded.get("mug").status, "ready")
            self.assertEqual(reloaded.metadata("mug").qa_report, "qa/mug.json")
            self.assertFalse(reloaded.get("mug").collision_enabled)


if __name__ == "__main__":
    unittest.main()
