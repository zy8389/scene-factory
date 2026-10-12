from __future__ import annotations

import os
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scene_factory import SceneFactory
from scene_factory.exporters.blender import BlenderExporter
from scene_factory.exporters.blender_case import find_blender_executable
from scene_factory.webapp import SceneWebApplication


@pytest.fixture
def factory(monkeypatch):
    monkeypatch.setenv("SCENE_FACTORY_LLM_MODE", "off")
    return SceneFactory()


def test_portable_case_includes_launcher_and_instructions_without_rendering(tmp_path, factory):
    scene = factory.build_from_recipe("kitchen_after_cooking", 42).scene
    (tmp_path / "scene.blend").write_bytes(b"old-unrelated-result")
    with patch("scene_factory.exporters.blender.render_manifest") as renderer:
        files = BlenderExporter(factory.registry).export(scene, tmp_path)
    renderer.assert_not_called()
    with zipfile.ZipFile(files["blender_case"]) as archive:
        assert "generate_blend.bat" in archive.namelist()
        assert "README_BLENDER.txt" in archive.namelist()
        assert "scene.blend" not in archive.namelist()
    launcher = Path(files["blender_launcher"]).read_text("utf-8")
    assert "--factory-startup" in launcher
    assert "--python-exit-code 1" in launcher
    assert "BLENDER_EXE" in launcher
    assert "choice /c YN" in launcher
    assert "python.exe" not in launcher


def test_requested_blend_is_added_to_zip_and_returned_files(tmp_path, factory):
    scene = factory.build_from_recipe("kitchen_after_cooking", 42).scene

    def render(manifest, **options):
        assert options["png"] is False
        blend = manifest.with_name("scene.blend")
        blend.write_bytes(b"BLENDER-test-result")
        return {"blend": str(blend)}

    with patch("scene_factory.exporters.blender.render_manifest", side_effect=render):
        files = BlenderExporter(factory.registry).export(scene, tmp_path, render_blend=True)
    assert Path(files["blend"]).is_file()
    with zipfile.ZipFile(files["blender_case"]) as archive:
        assert archive.read("scene.blend") == b"BLENDER-test-result"


def test_render_failure_keeps_existing_case_zip(tmp_path, factory):
    scene = factory.build_from_recipe("kitchen_after_cooking", 42).scene
    exporter = BlenderExporter(factory.registry)
    files = exporter.export(scene, tmp_path)
    original = Path(files["blender_case"]).read_bytes()
    with patch("scene_factory.exporters.blender.render_manifest", side_effect=RuntimeError("render failed")):
        with pytest.raises(RuntimeError):
            exporter.export(scene, tmp_path, render_blend=True)
    assert Path(files["blender_case"]).read_bytes() == original


def test_web_render_flag_is_checked_and_returns_downloadable_blend(tmp_path, factory):
    app = SceneWebApplication(tmp_path, factory=factory)
    scene = factory.build_from_recipe("kitchen_after_cooking", 42)
    scene_root = tmp_path / scene.scene.scene_id
    factory.write_result(scene, scene_root, export_bundle=False)

    def render(manifest, **options):
        blend = manifest.with_name("scene.blend")
        blend.write_bytes(b"BLENDER-test-result")
        return {"blend": str(blend)}

    with patch("scene_factory.exporters.blender.render_manifest", side_effect=render) as renderer:
        result = app.export_blender_case({"scene_id": scene.scene.scene_id, "render_blend": True})
        renderer.assert_called_once()
    assert result["files"]["blend"].endswith("/scene.blend")
    assert app.resolve_output(result["files"]["blend"].removeprefix("/outputs/")).is_file()
    with pytest.raises(ValueError):
        app.export_blender_case({"scene_id": scene.scene.scene_id, "render_blend": "true"})


def test_discovery_supports_environment_path_and_running_windows_blender(tmp_path, monkeypatch):
    executable = tmp_path / "blender.exe"
    executable.write_bytes(b"test")
    monkeypatch.setenv("BLENDER_EXE", str(executable))
    assert find_blender_executable() == str(executable)
    monkeypatch.delenv("BLENDER_EXE")
    monkeypatch.setattr("scene_factory.exporters.blender_case.shutil.which", lambda _: None)
    if os.name == "nt":
        with patch("scene_factory.exporters.blender_case.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=str(executable) + "\n")) as query:
            assert find_blender_executable() == str(executable)
            assert "Get-Process -Name blender" in query.call_args.args[0][-1]


def test_missing_or_redirected_scene_never_invokes_blender(tmp_path, factory):
    app = SceneWebApplication(tmp_path, factory=factory)
    with patch("scene_factory.exporters.blender.render_manifest") as renderer:
        with pytest.raises(ValueError):
            app.export_blender_case({"scene_id": "../outside", "render_blend": True})
        with pytest.raises(FileNotFoundError):
            app.export_blender_case({"scene_id": "missing", "render_blend": True})
    renderer.assert_not_called()
