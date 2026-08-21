from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


class YcbAssetManifestTests(unittest.TestCase):
    def test_committed_manifest_hashes_match_real_files(self) -> None:
        root = Path("data/assets/source/ycb_025_mug")
        manifest = json.loads((root / "SOURCE.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "imported")
        self.assertEqual(manifest["license"], "CC BY 4.0")
        for entry in manifest["source_files"]:
            path = root / entry["path"]
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(digest, entry["sha256"])
            self.assertEqual(path.stat().st_size, entry["bytes"])


if __name__ == "__main__":
    unittest.main()
