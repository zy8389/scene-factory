from __future__ import annotations

import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from tools.import_ycb_025_mug import import_ycb_asset


class YcbImportTests(unittest.TestCase):
    def test_network_block_is_reported_without_creating_source_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "source" / "ycb_025_mug"
            with patch(
                "tools.import_ycb_025_mug._download",
                side_effect=URLError("network gateway blocked"),
            ):
                report = import_ycb_asset(
                    source_dir=source_dir,
                    report_path=root / "blocked.json",
                )
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(report["issues"][0]["code"], "source_unavailable")
            self.assertFalse(source_dir.exists())

    def test_valid_archive_is_extracted_and_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "025_mug.tgz"
            fixture = root / "fixture" / "025_mug" / "google_16k"
            fixture.mkdir(parents=True)
            (fixture / "textured.obj").write_text("# test fixture\n", encoding="utf-8")
            (fixture / "textured.mtl").write_text("# test fixture\n", encoding="utf-8")
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(fixture.parent, arcname="025_mug")

            source_dir = root / "source" / "ycb_025_mug"
            report = import_ycb_asset(
                archive_path=archive,
                source_dir=source_dir,
                source_url="local-test://025_mug.tgz",
                license_name="test-license",
                report_path=root / "import.json",
            )
            self.assertEqual(report["result"], "passed")
            self.assertEqual(report["source_geometry"], "025_mug/google_16k/textured.obj")
            self.assertTrue((source_dir / "SOURCE.json").is_file())
            manifest = json.loads((source_dir / "SOURCE.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["archive_sha256"], report["archive_sha256"])
            self.assertTrue(manifest["source_files"])
            self.assertEqual(manifest["status"], "imported")

    def test_archive_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "malicious.tgz"
            info = tarfile.TarInfo("../outside.obj")
            content = b"not a real asset"
            info.size = len(content)
            with tarfile.open(archive, "w:gz") as tar:
                import io

                tar.addfile(info, io.BytesIO(content))
            report = import_ycb_asset(archive_path=archive, source_dir=root / "source")
            self.assertEqual(report["result"], "blocked")
            self.assertEqual(report["issues"][0]["code"], "invalid_ycb_archive")
            self.assertFalse((root / "source").exists())


if __name__ == "__main__":
    unittest.main()
