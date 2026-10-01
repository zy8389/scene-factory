from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scene_factory.bundle import (
    BUNDLE_SCHEMA,
    SceneBundleError,
    SceneBundleExporter,
    _portable_usd_package,
)
from scene_factory.factory import SceneFactory
from scene_factory.webapp import SceneWebApplication


class SceneBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with patch.dict(os.environ, {"SCENE_FACTORY_LLM_MODE": "off"}):
            cls.factory = SceneFactory()

    @staticmethod
    def usd_package(members: dict[str, bytes]) -> bytes:
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for name, content in members.items():
                member = zipfile.ZipInfo(name)
                member.filename = name
                archive.writestr(member, content)
        return stream.getvalue()

    def test_usd_bundle_contains_localized_dependencies_and_hashes(self) -> None:
        result = self.factory.build_from_recipe("kitchen_after_cooking", 42)
        members = {
            "scene.usd": b"localized root layer",
            "0/model.usda": b"model layer",
            "1/collision.usd": b"collision layer",
            "2/textures/albedo.png": b"texture",
        }
        with tempfile.TemporaryDirectory() as directory:
            files = self.factory.write_result(result, directory)
            source = Path(directory) / "scene.usd"
            source.write_bytes(b"original external references")
            files["usd"] = str(source)
            with patch(
                "scene_factory.bundle.package_usd_with_isaac",
                return_value=self.usd_package(members),
            ):
                output = SceneBundleExporter(self.factory.registry).export(
                    result.scene, files, Path(directory) / "portable.zip"
                )
            with zipfile.ZipFile(output) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                package_data = archive.read(manifest["artifacts"]["usd"]["path"])
                package = zipfile.ZipFile(io.BytesIO(package_data))
                self.assertEqual(package.read("scene.usd"), members["scene.usd"])
                self.assertEqual(len(manifest["usd_dependencies"]), 3)
                for descriptor in manifest["usd_dependencies"]:
                    self.assertEqual(descriptor["package"], "scene/scene.usdz")
                    content = package.read(descriptor["path"])
                    self.assertEqual(descriptor["size"], len(content))
                    self.assertEqual(descriptor["sha256"], hashlib.sha256(content).hexdigest())
                self.assertEqual(
                    manifest["artifacts"]["usd"]["sha256"], hashlib.sha256(package_data).hexdigest()
                )
                package.close()
            self.assertEqual(source.read_bytes(), b"original external references")

    def test_usd_package_rejects_unsafe_or_conflicting_members(self) -> None:
        unsafe = (
            "../outside.usd", "/absolute.usd", "C:/absolute.usd", "folder\\asset.usd",
            "folder/../asset.usd", "folder//asset.usd", "SCENE.USD",
        )
        for name in unsafe:
            with self.subTest(name=name):
                with patch(
                    "scene_factory.bundle.package_usd_with_isaac",
                    return_value=self.usd_package({"scene.usd": b"root", name: b"unsafe"}),
                ):
                    with self.assertRaises(SceneBundleError):
                        _portable_usd_package(Path("source.usd"))

    def test_usd_package_rejects_missing_root_or_invalid_zip(self) -> None:
        for package in (self.usd_package({"asset.usda": b"layer"}), b"invalid zip"):
            with self.subTest(package=package[:10]):
                with patch("scene_factory.bundle.package_usd_with_isaac", return_value=package):
                    with self.assertRaises(SceneBundleError):
                        _portable_usd_package(Path("source.usd"))

    def test_usd_packaging_failure_preserves_existing_bundle(self) -> None:
        result = self.factory.build_from_recipe("kitchen_after_cooking", 42)
        with tempfile.TemporaryDirectory() as directory:
            files = self.factory.write_result(result, directory)
            output = Path(files["bundle"])
            original = output.read_bytes()
            source = Path(directory) / "scene.usd"
            source.write_bytes(b"missing dependencies")
            files["usd"] = str(source)
            with patch(
                "scene_factory.bundle.package_usd_with_isaac",
                side_effect=RuntimeError("unresolved dependency"),
            ):
                with self.assertRaisesRegex(SceneBundleError, "unresolved dependency"):
                    SceneBundleExporter(self.factory.registry).export(result.scene, files, output)
            self.assertEqual(output.read_bytes(), original)
            self.assertFalse(output.with_suffix(".zip.tmp").exists())

    def test_export_contains_hashed_scene_mjcf_and_real_visual_assets(self) -> None:
        result = self.factory.build_from_recipe("kitchen_after_cooking", 42)
        with tempfile.TemporaryDirectory() as directory:
            files = self.factory.write_result(result, directory, export_mjcf=True)
            bundle_path = Path(files["bundle"])
            self.assertTrue(bundle_path.is_file())
            with zipfile.ZipFile(bundle_path) as archive:
                names = set(archive.namelist())
                manifest = json.loads(archive.read("manifest.json"))
                descriptors = list(manifest["artifacts"].values()) + [
                    asset["visual"]
                    for asset in manifest["assets"].values()
                    if asset["visual"] is not None
                ]
                for descriptor in descriptors:
                    content = archive.read(descriptor["path"])
                    self.assertEqual(len(content), descriptor["size"])
                    self.assertEqual(hashlib.sha256(content).hexdigest(), descriptor["sha256"])

            self.assertEqual(manifest["schema"], BUNDLE_SCHEMA)
            self.assertEqual(manifest["scene_id"], result.scene.scene_id)
            self.assertIn("scene/scene.xml", names)
            self.assertIn("scene/preview.svg", names)
            self.assertEqual(
                set(manifest["assets"]),
                {item.asset_id for item in result.scene.objects},
            )
            visual_paths = {
                item["visual"]["path"]
                for item in manifest["assets"].values()
                if item["visual"] is not None
            }
            self.assertGreaterEqual(len(visual_paths), 4)
            self.assertTrue(visual_paths.issubset(names))
            self.assertEqual(
                manifest["assets"]["pot_basic"]["visual_transform"]["scale_mode"],
                "uniform_contain",
            )

    def test_web_generation_exposes_downloadable_scene_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"SCENE_FACTORY_LLM_MODE": "off"}):
                app = SceneWebApplication(directory)
            response = app.generate(
                {
                    "prompt": "刚做完饭，厨房里有杯子、碗、盘子和刀。",
                    "seed": 51,
                    "count": 1,
                    "export_mjcf": True,
                    "export_usd": False,
                }
            )
            item = response["items"][0]
            self.assertIn("bundle", item["files"])
            relative = item["files"]["bundle"].removeprefix("/outputs/")
            bundle_path = app.resolve_output(relative)
            self.assertEqual(bundle_path.suffix, ".zip")
            self.assertGreater(bundle_path.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
