"""Prepare a Blender scene without importing Blender into the portable SDK."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from ..bundle import _load_visual_assets, _safe_component
from ..models import CompiledScene
from ..registry import AssetRegistry


class BlenderExporter:
    def __init__(self, registry: AssetRegistry) -> None:
        self.registry = registry

    def export(self, scene: CompiledScene, output_dir: str | Path) -> dict[str, str]:
        output = Path(output_dir).resolve()
        output.mkdir(parents=True, exist_ok=True)
        assets = _load_visual_assets(self.registry.base_dir / "source")
        asset_dir = output / "blender_assets"
        visual_files: set[Path] = set()
        objects = []
        for item in scene.objects:
            record = self.registry.get(item.asset_id)
            visual = assets.get(item.asset_id)
            path = None
            if visual:
                asset_dir.mkdir(exist_ok=True)
                source = Path(visual["path"])
                destination = asset_dir / f"{_safe_component(item.asset_id)}.glb"
                if destination not in visual_files:
                    shutil.copyfile(source, destination)
                    visual_files.add(destination)
                path = destination.relative_to(output).as_posix()
            objects.append({
                "object_id": item.object_id,
                "asset_id": item.asset_id,
                "pose": {"position": list(item.pose.position), "yaw_deg": item.pose.yaw_deg},
                "bbox_m": list(item.bbox_m),
                "color": list(record.color),
                "primitive": record.primitive,
                "visual": path,
                "visual_transform": visual["transform"] if visual else None,
            })
        manifest = output / "blender_manifest.json"
        manifest.write_text(json.dumps({
            "schema": "scene_factory.blender.v1",
            "scene_id": scene.scene_id,
            "room_dimensions_m": list(scene.room_dimensions_m),
            "objects": objects,
            "note": "Visualization only; not a robot articulation or physics acceptance export.",
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        script = output / "blender_render.py"
        shutil.copyfile(Path(__file__).with_name("blender_render.py"), script)
        case_path = output / f"{_safe_component(scene.scene_id)}.blender-case.zip"
        with tempfile.NamedTemporaryFile(
            prefix=".blender-case-", suffix=".zip", dir=output, delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        try:
            with zipfile.ZipFile(
                temporary_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as archive:
                archive.write(manifest, manifest.relative_to(output).as_posix())
                archive.write(script, script.relative_to(output).as_posix())
                for asset_path in sorted(visual_files):
                    archive.write(asset_path, asset_path.relative_to(output).as_posix())
            os.replace(temporary_path, case_path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
        return {
            "blender_manifest": str(manifest),
            "blender_script": str(script),
            "blender_case": str(case_path),
        }


def render_manifest(
    manifest: str | Path, *, blender_exe: str | Path | None = None, png: bool = True,
) -> dict[str, str]:
    manifest = Path(manifest).resolve()
    if not manifest.is_file():
        raise FileNotFoundError(manifest)
    executable = str(blender_exe) if blender_exe else (os.environ.get("BLENDER_EXE") or shutil.which("blender"))
    if not executable or not (Path(executable).is_file() or shutil.which(executable)):
        raise RuntimeError("Blender is not installed; install it and pass --blender-exe or set BLENDER_EXE")
    script = manifest.with_name("blender_render.py")
    if not script.is_file():
        raise FileNotFoundError(script)
    blend = manifest.with_name("scene.blend")
    image = manifest.with_name("render.png")
    with tempfile.TemporaryDirectory(prefix=".blender-render-", dir=manifest.parent) as directory:
        temporary_blend = Path(directory) / "scene.blend"
        temporary_image = Path(directory) / "render.png"
        command = [
            executable, "-b", "--python-exit-code", "1", "--python", str(script), "--",
            "--manifest", str(manifest), "--blend", str(temporary_blend),
        ]
        if png:
            command.extend(["--png", str(temporary_image)])
        try:
            completed = subprocess.run(
                command, cwd=manifest.parent, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=600, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Blender render timed out after 600 seconds") from exc
        if (
            completed.returncode != 0
            or not temporary_blend.is_file()
            or (png and not temporary_image.is_file())
        ):
            diagnostics = "\n".join((completed.stdout or "", completed.stderr or ""))[-1500:]
            raise RuntimeError(f"Blender render failed: {diagnostics}")
        os.replace(temporary_blend, blend)
        if png:
            os.replace(temporary_image, image)
    return {"blend": str(blend), **({"png": str(image)} if png else {})}
