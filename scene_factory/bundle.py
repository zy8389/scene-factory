from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from pathlib import Path
from typing import Any, Mapping

from .models import CompiledScene
from .paths import project_root
from .registry import AssetRegistry


BUNDLE_SCHEMA = "scene_factory.scene_bundle.v1"
BUNDLE_VERSION = 1

ARTIFACT_PATHS = {
    "scene_spec": "scene/scene_spec.json",
    "layout": "scene/layout.json",
    "validation": "scene/validation.json",
    "preview": "scene/preview.svg",
    "intent": "scene/scene_intent.json",
    "revision": "scene/revision.json",
    "mjcf": "scene/scene.xml",
    "usd": "scene/scene.usd",
}
REQUIRED_ARTIFACTS = {"scene_spec", "layout", "validation", "preview"}
_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]+")


class SceneBundleError(ValueError):
    """Raised when a portable scene bundle cannot be created."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_descriptor(path: str, data: bytes) -> dict[str, Any]:
    return {"path": path, "size": len(data), "sha256": sha256_bytes(data)}


def _safe_component(value: str) -> str:
    candidate = _SAFE_COMPONENT_RE.sub("_", value).strip("._-")[:80]
    if not candidate:
        candidate = "asset"
    if candidate != value:
        candidate = f"{candidate}-{sha256_bytes(value.encode('utf-8'))[:8]}"
    return candidate


def _load_visual_assets(asset_root: Path) -> dict[str, dict[str, Any]]:
    discovered: dict[str, dict[str, Any]] = {}
    if not asset_root.is_dir():
        return discovered
    for metadata_path in asset_root.glob("*/SOURCE.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            asset_id = str(metadata["asset_id"]).strip()
            source_geometry = Path(str(metadata["source_geometry"]))
            geometry_path = (metadata_path.parent / source_geometry).resolve()
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            asset_id
            and geometry_path.is_relative_to(asset_root)
            and geometry_path.is_file()
            and geometry_path.suffix.lower() == ".glb"
        ):
            transform = metadata.get("visual_transform")
            if not isinstance(transform, dict):
                transform = {}
            rotation = transform.get("rotation_euler_deg", [90.0, 0.0, 0.0])
            if (
                not isinstance(rotation, list)
                or len(rotation) != 3
                or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in rotation)
            ):
                rotation = [90.0, 0.0, 0.0]
            discovered[asset_id] = {
                "path": geometry_path,
                "transform": {
                    "up_axis": str(transform.get("up_axis", "Y")),
                    "rotation_euler_deg": [float(value) for value in rotation],
                    "scale_mode": "uniform_contain",
                    "anchor": "bottom_center",
                },
            }
    return discovered


class SceneBundleExporter:
    """Create a self-contained, versioned scene ZIP for preview and simulation."""

    def __init__(
        self,
        registry: AssetRegistry,
        asset_root: str | Path | None = None,
    ) -> None:
        self.registry = registry
        self.asset_root = Path(
            asset_root or project_root() / "data" / "assets" / "source"
        ).resolve()
        self.visual_assets = _load_visual_assets(self.asset_root)

    def export(
        self,
        scene: CompiledScene,
        files: Mapping[str, str | Path],
        output_path: str | Path,
    ) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        archive_files: dict[str, bytes] = {}
        artifacts: dict[str, dict[str, Any]] = {}
        for name, archive_path in ARTIFACT_PATHS.items():
            raw_path = files.get(name)
            if raw_path is None:
                continue
            source = Path(raw_path)
            if not source.is_file():
                raise SceneBundleError(f"scene artifact is missing: {name}")
            data = source.read_bytes()
            archive_files[archive_path] = data
            artifacts[name] = _file_descriptor(archive_path, data)
        missing = sorted(REQUIRED_ARTIFACTS - set(artifacts))
        if missing:
            raise SceneBundleError(
                f"scene bundle is missing required artifacts: {', '.join(missing)}"
            )

        assets: dict[str, dict[str, Any]] = {}
        for asset_id in dict.fromkeys(item.asset_id for item in scene.objects):
            record = self.registry.get(asset_id)
            asset: dict[str, Any] = {
                "asset_id": asset_id,
                "name": record.name or asset_id,
                "primitive": record.primitive,
                "color": list(record.color),
                "bbox_m": list(record.bbox_m),
                "source_type": record.source_type,
                "license": record.license,
            }
            visual = self.visual_assets.get(asset_id)
            if visual is not None:
                archive_path = f"assets/{_safe_component(asset_id)}/visual.glb"
                data = Path(visual["path"]).read_bytes()
                archive_files[archive_path] = data
                asset["visual"] = _file_descriptor(archive_path, data)
                asset["visual_transform"] = visual["transform"]
            else:
                asset["visual"] = None
                asset["visual_transform"] = None
            assets[asset_id] = asset

        manifest = {
            "schema": BUNDLE_SCHEMA,
            "version": BUNDLE_VERSION,
            "scene_id": scene.scene_id,
            "created_by": {"name": "scene-factory", "version": "0.1.0"},
            "backends": {
                "default_execution": "mujoco",
                "web_preview": "threejs",
                "high_fidelity_validation": "isaac_sim_optional",
                "collision_geometry": "bounding_box_proxy",
            },
            "artifacts": artifacts,
            "assets": assets,
        }
        manifest_data = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")

        temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
        try:
            with zipfile.ZipFile(
                temporary_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as archive:
                archive.writestr("manifest.json", manifest_data)
                for archive_path, data in archive_files.items():
                    compression = (
                        zipfile.ZIP_STORED
                        if archive_path.lower().endswith(".glb")
                        else zipfile.ZIP_DEFLATED
                    )
                    archive.writestr(archive_path, data, compress_type=compression)
            os.replace(temporary_path, output_path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
        return output_path
