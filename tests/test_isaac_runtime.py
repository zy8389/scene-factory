from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scene_factory import isaac_runtime
from scene_factory.webapp import _default_output


class IsaacRuntimeTests(unittest.TestCase):
    def test_loading_restores_realpath_on_success_and_failure(self) -> None:
        original = os.path.realpath
        app_class = object()
        for outcome in (Mock(SimulationApp=app_class), ImportError("missing Isaac")):
            with self.subTest(outcome=type(outcome).__name__):
                with (
                    patch.object(isaac_runtime, "activate_isaac_kit_runtime"),
                    patch.object(isaac_runtime, "import_module") as importer,
                ):
                    if isinstance(outcome, Exception):
                        importer.side_effect = outcome
                        with self.assertRaises(ImportError):
                            isaac_runtime.load_simulation_app()
                    else:
                        importer.return_value = outcome
                        self.assertIs(isaac_runtime.load_simulation_app(), app_class)
                self.assertIs(os.path.realpath, original)

    @unittest.skipUnless(os.name == "nt", "Windows junction compatibility only")
    def test_alias_hook_is_scoped_and_preserves_strict_and_bytes_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            alias = Path(directory) / "isaac_alias"
            target = Path(directory) / "实际环境"
            original = os.path.realpath

            def resolve(path, *, strict=False):
                decoded = os.fsdecode(path)
                if strict and decoded.endswith("missing"):
                    raise FileNotFoundError(decoded)
                return os.fsencode(decoded) if isinstance(path, bytes) else decoded

            resolver = Mock(side_effect=resolve)
            with (
                patch.object(isaac_runtime, "activate_isaac_kit_runtime"),
                patch.object(isaac_runtime, "prepare_isaac_python", return_value=alias / "Scripts/python.exe"),
                patch.object(isaac_runtime, "_NATIVE_REALPATH", return_value=str(target)),
                patch.object(os.path, "realpath", resolver),
            ):
                with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
                    with isaac_runtime.isaac_kit_runtime():
                        self.assertEqual(os.path.realpath(target / "Lib/module.py"), str(alias / "Lib/module.py"))
                        encoded = os.fsencode(target / "Lib/module.py")
                        self.assertEqual(os.path.realpath(encoded), os.fsencode(alias / "Lib/module.py"))
                        unrelated = str(target) + "-other\\module.py"
                        self.assertEqual(os.path.realpath(unrelated), unrelated)
                        with self.assertRaises(FileNotFoundError):
                            os.path.realpath(target / "missing", strict=True)
                        raise RuntimeError("bootstrap failed")
                self.assertIs(os.path.realpath, resolver)
            self.assertIs(os.path.realpath, original)

    def test_worker_environment_separates_code_from_runtime_resources(self) -> None:
        resources = Path(tempfile.gettempdir()) / "separate_resources"
        with (
            patch.object(isaac_runtime, "project_root", return_value=resources),
            patch.dict(os.environ, {"PYTHONPATH": "existing-path"}),
        ):
            environment = isaac_runtime.isaac_process_environment()
        self.assertEqual(environment["SCENE_FACTORY_HOME"], str(resources))
        self.assertEqual(
            environment["PYTHONPATH"].split(os.pathsep),
            [str(Path(isaac_runtime.__file__).resolve().parent.parent), "existing-path"],
        )

    def test_export_runs_packaged_worker_module(self) -> None:
        with (
            patch.object(isaac_runtime, "find_isaac_python", return_value=Path("isaac-python")),
            patch.object(isaac_runtime.subprocess, "run", return_value=Mock(returncode=0)) as run,
        ):
            output = isaac_runtime.export_usd_with_isaac("layout.json", "registry.jsonl", "scene.usd")
        self.assertEqual(run.call_args.args[0][:3], ["isaac-python", "-m", "scene_factory.isaac_export"])
        self.assertEqual(output, Path("scene.usd").resolve())
        self.assertIn("SCENE_FACTORY_HOME", run.call_args.kwargs["env"])

    def test_packaging_runs_isolated_worker_and_reads_generated_package(self) -> None:
        def run_worker(command, **kwargs):
            self.assertEqual(command[:3], ["isaac-python", "-m", "scene_factory.isaac_export"])
            self.assertIn("--package", command)
            output = Path(command[command.index("--output") + 1])
            self.assertEqual(Path(kwargs["cwd"]), output.parent)
            output.write_bytes(b"localized USDZ")
            return Mock(returncode=0)

        with (
            patch.object(isaac_runtime, "find_isaac_python", return_value=Path("isaac-python")),
            patch.object(isaac_runtime.subprocess, "run", side_effect=run_worker),
        ):
            self.assertEqual(isaac_runtime.package_usd_with_isaac("scene.usd"), b"localized USDZ")

    def test_packaging_refuses_failed_or_missing_output(self) -> None:
        for returncode in (0, 1):
            with self.subTest(returncode=returncode):
                with (
                    patch.object(isaac_runtime, "find_isaac_python", return_value=Path("isaac-python")),
                    patch.object(
                        isaac_runtime.subprocess, "run",
                        return_value=Mock(returncode=returncode, stderr="missing dependencies", stdout=""),
                    ),
                ):
                    with self.assertRaisesRegex(RuntimeError, "Portable USD packaging failed"):
                        isaac_runtime.package_usd_with_isaac("scene.usd")

    @unittest.skipUnless(os.name == "nt", "Windows output path compatibility only")
    def test_default_web_output_is_ascii_under_chinese_checkout(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            output = _default_output()
        expected = Path.cwd() / "outputs/web"
        if str(expected).isascii():
            self.assertEqual(output, Path("outputs/web"))
        else:
            self.assertEqual(output, Path(expected.anchor) / "scene_factory_runtime/web")
            self.assertTrue(str(output).isascii())

    def test_web_output_environment_override_is_preserved(self) -> None:
        with patch.dict(os.environ, {"SCENE_FACTORY_WEB_OUTPUT": "custom-web"}):
            self.assertEqual(_default_output(), Path("custom-web"))


@unittest.skipUnless(importlib.util.find_spec("pxr"), "OpenUSD is an optional integration")
class NativeUsdPackagingTests(unittest.TestCase):
    def test_localized_usd_opens_without_original_layers_or_textures(self) -> None:
        from pxr import Sdf, Usd, UsdGeom
        from scene_factory.bundle import _portable_usd_package
        from scene_factory.isaac_export import package_usd

        with tempfile.TemporaryDirectory(prefix="scene_factory_usd_fixture_") as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            texture = source / "albedo.png"
            texture.write_bytes(b"fixture texture")
            collision = Usd.Stage.CreateNew(str(source / "collision.usda"))
            collision.SetDefaultPrim(UsdGeom.Cube.Define(collision, "/Collision").GetPrim())
            collision.GetRootLayer().Save()
            model = Usd.Stage.CreateNew(str(source / "model.usda"))
            model.SetDefaultPrim(UsdGeom.Xform.Define(model, "/Model").GetPrim())
            model.DefinePrim("/Model/Collision").GetReferences().AddReference(str(source / "collision.usda"))
            material = model.DefinePrim("/Model/Material", "Shader")
            material.CreateAttribute("inputs:file", Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath(str(texture)))
            model.GetRootLayer().Save()
            sublayer = Usd.Stage.CreateNew(str(source / "metadata.usda"))
            sublayer.DefinePrim("/World/Marker", "Scope")
            sublayer.GetRootLayer().Save()
            stage = Usd.Stage.CreateNew(str(source / "root.usda"))
            stage.SetDefaultPrim(UsdGeom.Xform.Define(stage, "/World").GetPrim())
            stage.GetRootLayer().subLayerPaths = [str(source / "metadata.usda")]
            stage.DefinePrim("/World/Object").GetReferences().AddReference(str(source / "model.usda"))
            stage.GetRootLayer().Save()
            original = {path.name: path.read_bytes() for path in source.iterdir()}
            package = root / "scene.usdz"
            package_usd(source / "root.usda", package)
            self.assertEqual({path.name: path.read_bytes() for path in source.iterdir()}, original)
            with patch("scene_factory.bundle.package_usd_with_isaac", return_value=package.read_bytes()):
                packaged, dependencies = _portable_usd_package(source / "root.usda")
            self.assertEqual(len(dependencies), 4)
            extracted = root / "extracted"
            target = extracted / "scene/scene.usdz"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(packaged)
            source.rename(root / "unavailable-source")
            probe = (
                "import json,sys; from pxr import Ar,Usd,Sdf,UsdUtils; "
                "stage=Usd.Stage.Open(sys.argv[1]); "
                "dependencies=UsdUtils.ComputeAllDependencies(Sdf.AssetPath(sys.argv[1])); "
                "texture=stage.GetPrimAtPath('/World/Object/Material').GetAttribute('inputs:file').Get(); "
                "print(json.dumps({'unresolved':list(dependencies[2]), "
                "'collision':bool(stage.GetPrimAtPath('/World/Object/Collision')), "
                "'marker':bool(stage.GetPrimAtPath('/World/Marker')), "
                "'texture_exists':bool(Ar.GetResolver().Resolve(texture.resolvedPath))}))"
            )
            result = subprocess.run(
                [sys.executable, "-c", probe, str(target)],
                cwd=root, capture_output=True, text=True, check=True, timeout=30,
            )
            self.assertEqual(json.loads(result.stdout), {
                "unresolved": [], "collision": True, "marker": True, "texture_exists": True,
            })

    def test_unresolved_usd_dependency_is_rejected(self) -> None:
        from pxr import Usd
        from scene_factory.isaac_export import package_usd

        with tempfile.TemporaryDirectory(prefix="scene_factory_usd_fixture_") as directory:
            root = Path(directory)
            stage = Usd.Stage.CreateNew(str(root / "source.usda"))
            stage.DefinePrim("/Object").GetReferences().AddReference("missing.usda")
            stage.GetRootLayer().Save()
            with self.assertRaisesRegex(RuntimeError, "unresolved dependencies"):
                package_usd(root / "source.usda", root / "scene.usdz")


if __name__ == "__main__":
    unittest.main()
