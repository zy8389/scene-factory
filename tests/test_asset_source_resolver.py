from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scene_factory.asset_sources import AssetSourceResolver


class AssetSourceResolverTests(unittest.TestCase):
    def test_fetch_writes_manifest_and_is_idempotent(self) -> None:
        config = {
            "assets": [{
                "asset_id": "bowl_001",
                "preferred_sources": [{
                    "source_name": "test",
                    "source_url": "https://example.invalid/model.glb",
                    "license": "CC BY 4.0",
                    "source_revision": "rev1",
                    "files": [{"role": "visual", "relative_path": "model.glb", "url": "https://example.invalid/model.glb"}],
                }],
            }]
        }
        resolver = AssetSourceResolver.from_config(config)

        def downloader(_url: str, destination: Path) -> None:
            destination.write_bytes(b"glTF" + b"asset")

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            first = resolver.fetch("bowl_001", output, downloader=downloader)
            second = resolver.fetch("bowl_001", output, downloader=downloader)
            self.assertEqual(first["result"], "passed")
            self.assertTrue(second["idempotent"])
            manifest = output / "SOURCE.json"
            self.assertTrue(manifest.is_file())
            self.assertEqual(first["source_revision"], "rev1")

    def test_missing_license_is_blocked(self) -> None:
        resolver = AssetSourceResolver.from_config({"assets": [{"asset_id": "bad", "preferred_sources": [{"files": [{"relative_path": "x.glb", "url": "https://example.invalid/x.glb"}]}]}]})
        report = resolver.fetch("bad", Path(tempfile.mkdtemp()))
        self.assertEqual(report["result"], "blocked")
        self.assertEqual(report["issues"][0]["code"], "source_unresolved")


if __name__ == "__main__":
    unittest.main()
