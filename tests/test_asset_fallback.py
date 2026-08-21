from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path

from scene_factory.factory import SceneFactory


class AssetFallbackTests(unittest.TestCase):
    def test_kitchen_recipe_records_proxy_fallback_until_real_mug_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = [
                json.loads(line)
                for line in Path("data/assets/registry.jsonl").read_text(encoding="utf-8").splitlines()
                if json.loads(line)["asset_id"] != "mug_001"
            ]
            registry = root / "registry.jsonl"
            registry.write_text(
                "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
            )
            factory = SceneFactory(registry, Path("recipes"))
            result = factory.build_from_recipe("kitchen_after_cooking", 9)
            mug = next(item for item in result.scene.objects if item.object_id == "mug_1")
            self.assertIn(mug.asset_id, {"mug_blue", "mug_cream"})
            self.assertIsNotNone(mug.fallback_reason)
            self.assertIn("mug_001", mug.fallback_reason)


if __name__ == "__main__":
    unittest.main()
