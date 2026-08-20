from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scene_factory.registry import AssetLoader, AssetRegistry


class AssetLoadingTests(unittest.TestCase):
    def test_loader_accepts_primitive_without_usd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "asset_id": "proxy",
                        "category": "box",
                        "bbox_m": [1, 1, 1],
                        "mass_kg": 1,
                        "friction": 0.5,
                        "status": "validated",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            registry = AssetRegistry.load(path)
            self.assertEqual(AssetLoader(registry).load("proxy").asset_id, "proxy")

    def test_loader_resolves_usd_and_collision_independently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "model.usda").write_text("#usda 1.0\n", encoding="utf-8")
            (root / "collision.usda").write_text("#usda 1.0\n", encoding="utf-8")
            (root / "registry.jsonl").write_text(
                json.dumps(
                    {
                        "asset_id": "real",
                        "category": "box",
                        "bbox_m": [1, 1, 1],
                        "mass": 2,
                        "friction": 0.6,
                        "usd_path": "model.usda",
                        "collision_path": "collision.usda",
                        "collision_mode": "authored",
                        "status": "quarantine",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            loader = AssetLoader(AssetRegistry.load(root / "registry.jsonl"))
            self.assertEqual(Path(loader.resolve_usd_path("real")).name, "model.usda")
            self.assertEqual(Path(loader.resolve_collision_path("real")).name, "collision.usda")
            self.assertEqual(loader.load("real", require_collision=True).asset_id, "real")


if __name__ == "__main__":
    unittest.main()
