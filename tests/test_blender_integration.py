from __future__ import annotations

import json
import math
import subprocess
import sys
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scene_factory.cli import main
from scene_factory.factory import SceneFactory
from scene_factory.exporters.blender import BlenderExporter, render_manifest
from scene_factory.exporters.blender_render import _fit_transform, _visual
from scene_factory.robot_specs import FRANKA_SPEC, RobotCapability, supported_robots
from scene_factory.webapp import SceneWebApplication


def test_franka_is_only_registered_runtime_robot() -> None:
    assert supported_robots() == (FRANKA_SPEC,)
    assert FRANKA_SPEC.kinematics_frame("isaacsim_bundled_franka_urdf") == "panda_hand"
    assert FRANKA_SPEC.kinematics_frame("nucleus_franka_usd") == "right_gripper"
    assert RobotCapability.PICK_PLACE in FRANKA_SPEC.capabilities


def test_blender_case_prepares_without_bpy(tmp_path: Path) -> None:
    factory = SceneFactory()
    result = factory.build_from_recipe("kitchen_after_cooking", 42)
    files = factory.write_result(result, tmp_path, export_bundle=False, export_blender=True)
    manifest = json.loads(Path(files["blender_manifest"]).read_text(encoding="utf-8"))
    assert manifest["scene_id"] == result.scene.scene_id
    assert len(manifest["objects"]) == len(result.scene.objects)
    assert Path(files["blender_script"]).is_file()
    assert Path(files["blender_case"]).is_file()
    with zipfile.ZipFile(files["blender_case"]) as archive:
        names = set(archive.namelist())
    assert "blender_manifest.json" in names
    assert "blender_render.py" in names
    assert any(name.startswith("blender_assets/") and name.endswith(".glb") for name in names)
    for item in manifest["objects"]:
        visual = item["visual"]
        if visual:
            assert (tmp_path / visual).is_file()
            assert (tmp_path / visual).resolve().is_relative_to(tmp_path.resolve())
    assert any(item["visual"] for item in manifest["objects"])


def test_cli_blender_build_and_missing_renderer(tmp_path: Path, monkeypatch, capsys) -> None:
    assert main(["build", "--recipe", "kitchen_after_cooking", "--output", str(tmp_path),
                 "--blender", "--no-mjcf"]) == 0
    assert (tmp_path / "blender_manifest.json").is_file()
    monkeypatch.delenv("BLENDER_EXE", raising=False)
    monkeypatch.setattr("scene_factory.exporters.blender.shutil.which", lambda _: None)
    assert main(["blender", "render", str(tmp_path / "blender_manifest.json")]) == 1
    assert "not installed" in capsys.readouterr().err


def test_web_can_generate_and_later_export_blender_case(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SCENE_FACTORY_LLM_MODE", "off")
    app = SceneWebApplication(tmp_path)

    generated = app.generate(
        {
            "prompt": "刚做完饭，厨房台面上有杯子、碗和刀。",
            "seed": 42,
            "count": 1,
            "export_mjcf": False,
            "export_blender": True,
        }
    )
    item = generated["items"][0]
    scene_id = item["scene"]["scene_id"]
    assert generated["export_blender"] is True
    assert item["files"]["blender_case"].startswith("/outputs/")
    assert app.resolve_output(item["files"]["blender_case"].removeprefix("/outputs/")).is_file()

    without_case = app.generate(
        {
            "prompt": "刚回到家，背包和鞋留在入口附近。",
            "seed": 43,
            "count": 1,
            "export_mjcf": False,
            "export_blender": False,
        }
    )
    late_scene_id = without_case["items"][0]["scene"]["scene_id"]
    assert "blender_case" not in without_case["items"][0]["files"]
    exported = app.export_blender_case({"scene_id": late_scene_id})
    assert exported["scene_id"] == late_scene_id
    assert exported["files"]["blender_case"].startswith("/outputs/")
    late_case = app.resolve_output(exported["files"]["blender_case"].removeprefix("/outputs/"))
    with zipfile.ZipFile(late_case) as archive:
        assert "blender_manifest.json" in archive.namelist()

    with zipfile.ZipFile(app.output_root / scene_id / f"{scene_id}.blender-case.zip") as archive:
        assert "blender_render.py" in archive.namelist()


def test_blender_case_excludes_stale_assets(tmp_path: Path) -> None:
    assets = tmp_path / "blender_assets"
    assets.mkdir()
    unrelated = assets / "unrelated.glb"
    unrelated.write_bytes(b"private stale content")
    factory = SceneFactory()
    scene = factory.build_from_recipe("kitchen_after_cooking", 42).scene
    files = BlenderExporter(factory.registry).export(scene, tmp_path)
    manifest = json.loads(Path(files["blender_manifest"]).read_text(encoding="utf-8"))
    expected = {"blender_manifest.json", "blender_render.py"}
    expected.update(item["visual"] for item in manifest["objects"] if item["visual"])
    with zipfile.ZipFile(files["blender_case"]) as archive:
        assert set(archive.namelist()) == expected
    assert unrelated.read_bytes() == b"private stale content"


def test_blender_case_sanitizes_scene_filename(tmp_path: Path) -> None:
    factory = SceneFactory()
    scene = replace(factory.build_from_recipe("living_room_returned_home", 42).scene,
                    scene_id="../outside")
    files = BlenderExporter(factory.registry).export(scene, tmp_path)
    assert Path(files["blender_case"]).parent == tmp_path.resolve()


@pytest.fixture
def prepared_renderer(tmp_path: Path) -> tuple[Path, Path]:
    manifest = tmp_path / "blender_manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    manifest.with_name("blender_render.py").write_text("", encoding="utf-8")
    executable = tmp_path / "blender.exe"
    executable.write_bytes(b"")
    return manifest, executable


@pytest.mark.parametrize("png", [True, False])
def test_blender_render_requires_fresh_outputs(prepared_renderer, monkeypatch, png) -> None:
    manifest, executable = prepared_renderer
    manifest.with_name("scene.blend").write_bytes(b"old blend")
    manifest.with_name("render.png").write_bytes(b"old png")
    monkeypatch.setattr("scene_factory.exporters.blender.subprocess.run",
                        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "script failed", ""))
    with pytest.raises(RuntimeError, match="script failed"):
        render_manifest(manifest, blender_exe=executable, png=png)
    assert manifest.with_name("scene.blend").read_bytes() == b"old blend"
    assert manifest.with_name("render.png").read_bytes() == b"old png"
    assert not list(manifest.parent.glob(".blender-render-*"))


@pytest.mark.parametrize("png", [True, False])
def test_blender_render_publishes_new_outputs(prepared_renderer, monkeypatch, png) -> None:
    manifest, executable = prepared_renderer

    def complete(command, **options):
        assert command[command.index("--python-exit-code") + 1] == "1"
        assert options["encoding"] == "utf-8"
        assert options["errors"] == "replace"
        Path(command[command.index("--blend") + 1]).write_bytes(b"new blend")
        if png:
            Path(command[command.index("--png") + 1]).write_bytes(b"new png")
        else:
            assert "--png" not in command
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("scene_factory.exporters.blender.subprocess.run", complete)
    files = render_manifest(manifest, blender_exe=executable, png=png)
    assert Path(files["blend"]).read_bytes() == b"new blend"
    assert ("png" in files) is png
    if png:
        assert Path(files["png"]).read_bytes() == b"new png"
    assert not list(manifest.parent.glob(".blender-render-*"))


def test_blender_render_timeout_preserves_existing_outputs(prepared_renderer, monkeypatch) -> None:
    manifest, executable = prepared_renderer
    manifest.with_name("scene.blend").write_bytes(b"old blend")

    def timeout(command, **options):
        raise subprocess.TimeoutExpired(command, options["timeout"])

    monkeypatch.setattr("scene_factory.exporters.blender.subprocess.run", timeout)
    with pytest.raises(RuntimeError, match="timed out"):
        render_manifest(manifest, blender_exe=executable)
    assert manifest.with_name("scene.blend").read_bytes() == b"old blend"
    assert not list(manifest.parent.glob(".blender-render-*"))


def test_visual_fit_anchors_to_bottom_of_centered_bbox() -> None:
    scale, location = _fit_transform((-1, -1, -1), (1, 1, 1), (2, 2, 4))
    assert scale == 1
    assert location == (0, 0, -1)
    assert location[2] - scale == -2


def test_visual_import_undoes_blender_gltf_axis_conversion(tmp_path: Path, monkeypatch) -> None:
    visual = tmp_path / "visual.glb"
    visual.write_bytes(b"")
    matrix = Mock()
    matrix.__matmul__ = Mock(side_effect=lambda vector: vector)
    matrix.inverted.return_value = matrix
    mesh = Mock(type="MESH", parent=None, matrix_world=matrix,
                bound_box=[(-1, -1, -1), (1, 1, 1)])
    oriented = Mock()
    basis = Mock()
    holder = Mock(matrix_world=matrix)
    objects = Mock()
    objects.__iter__ = Mock(side_effect=lambda: iter(imported))
    objects.new.side_effect = [oriented, basis]
    imported = []
    bpy = Mock()
    bpy.data.objects = objects
    bpy.ops.import_scene.gltf.side_effect = lambda **kwargs: imported.append(mesh)
    monkeypatch.setitem(sys.modules, "mathutils", SimpleNamespace(Vector=lambda vector: vector))
    item = {"object_id": "mug", "visual": visual.name, "bbox_m": [2, 2, 2],
            "visual_transform": {"rotation_euler_deg": [90, 0, 0]}}
    assert _visual(bpy, item, tmp_path, holder)
    assert oriented.parent is holder
    assert basis.parent is oriented
    assert mesh.parent is basis
    assert oriented.rotation_euler == (math.pi / 2, 0, 0)
    assert basis.rotation_euler == (-math.pi / 2, 0, 0)


def test_web_blender_export_rejects_path_escape(tmp_path: Path) -> None:
    app = SceneWebApplication(tmp_path)
    with pytest.raises(ValueError, match="invalid scene_id"):
        app.export_blender_case({"scene_id": "../outside"})
