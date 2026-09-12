from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..models import CompiledScene
from ..registry import AssetRegistry


_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _value(item: Any, name: str) -> Any:
    return item[name] if isinstance(item, Mapping) else getattr(item, name)


def _numbers(values: Any) -> str:
    return " ".join(f"{float(value):.9g}" for value in values)


class MujocoMjcfExporter:
    """Export simulator-neutral scene layouts as lightweight MuJoCo MJCF."""

    def __init__(
        self,
        registry: AssetRegistry,
        *,
        offscreen_width: int = 960,
        offscreen_height: int = 720,
    ) -> None:
        self.registry = registry
        self.offscreen_width = offscreen_width
        self.offscreen_height = offscreen_height

    @staticmethod
    def body_name(object_id: str) -> str:
        normalized = _NAME_RE.sub("_", object_id).strip("_.-") or "object"
        return f"object__{normalized}"

    def to_string(self, scene: CompiledScene | Mapping[str, Any]) -> str:
        scene_id = str(_value(scene, "scene_id"))
        room_x, room_y, room_z = _value(scene, "room_dimensions_m")
        root = ET.Element("mujoco", {"model": self.body_name(scene_id)})
        ET.SubElement(
            root,
            "compiler",
            {"angle": "degree", "autolimits": "true", "balanceinertia": "true"},
        )
        ET.SubElement(
            root,
            "option",
            {"timestep": "0.002", "gravity": "0 0 -9.81", "integrator": "implicitfast"},
        )
        default = ET.SubElement(root, "default")
        ET.SubElement(
            default,
            "geom",
            {
                "condim": "4",
                "solref": "0.02 1",
                "solimp": "0.9 0.95 0.001",
            },
        )
        visual = ET.SubElement(root, "visual")
        ET.SubElement(
            visual,
            "global",
            {
                "azimuth": "135",
                "elevation": "-24",
                "offwidth": str(self.offscreen_width),
                "offheight": str(self.offscreen_height),
            },
        )
        ET.SubElement(visual, "headlight", {"ambient": "0.35 0.35 0.35"})

        worldbody = ET.SubElement(root, "worldbody")
        ET.SubElement(
            worldbody,
            "light",
            {
                "name": "key_light",
                "directional": "true",
                "pos": _numbers((0.0, 0.0, float(room_z) + 2.0)),
                "dir": "0 0 -1",
                "diffuse": "0.8 0.8 0.8",
            },
        )
        ET.SubElement(
            worldbody,
            "geom",
            {
                "name": "room_floor",
                "type": "plane",
                "pos": "0 0 0",
                "size": _numbers((float(room_x) / 2.0, float(room_y) / 2.0, 0.05)),
                "rgba": "0.82 0.83 0.8 1",
                "friction": "0.8 0.01 0.001",
            },
        )

        used_names: set[str] = set()
        for index, item in enumerate(_value(scene, "objects")):
            object_id = str(_value(item, "object_id"))
            body_name = self.body_name(object_id)
            if body_name in used_names:
                body_name = f"{body_name}_{index}"
            used_names.add(body_name)
            asset = self.registry.get(str(_value(item, "asset_id")))
            pose = _value(item, "pose")
            position = _value(pose, "position")
            yaw_deg = float(_value(pose, "yaw_deg"))
            quaternion = (
                math.cos(math.radians(yaw_deg) / 2.0),
                0.0,
                0.0,
                math.sin(math.radians(yaw_deg) / 2.0),
            )
            body = ET.SubElement(
                worldbody,
                "body",
                {
                    "name": body_name,
                    "pos": _numbers(position),
                    "quat": _numbers(quaternion),
                },
            )
            dynamic = bool(_value(item, "dynamic"))
            if dynamic:
                ET.SubElement(body, "freejoint", {"name": f"joint__{body_name}"})

            bbox = tuple(float(value) for value in _value(item, "bbox_m"))
            primitive = (asset.primitive or "cube").lower()
            geom_type, size = self._geom_shape(primitive, bbox)
            static_friction = asset.static_friction or asset.friction or 0.5
            dynamic_friction = asset.dynamic_friction or asset.friction or 0.5
            geom_attributes = {
                "name": f"geom__{body_name}",
                "type": geom_type,
                "size": _numbers(size),
                "rgba": _numbers((*asset.color, 1.0)),
                "friction": _numbers((static_friction, dynamic_friction * 0.02, 0.001)),
                "contype": "1" if asset.collision_enabled else "0",
                "conaffinity": "1" if asset.collision_enabled else "0",
            }
            if dynamic:
                geom_attributes["mass"] = f"{max(float(asset.mass_kg), 1e-6):.9g}"
            ET.SubElement(body, "geom", geom_attributes)

        ET.indent(root, space="  ")
        return ET.tostring(root, encoding="unicode", xml_declaration=False) + "\n"

    def export(
        self,
        scene: CompiledScene | Mapping[str, Any],
        output_path: str | Path,
    ) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(self.to_string(scene), encoding="utf-8")
        return output_path

    @staticmethod
    def _geom_shape(
        primitive: str,
        bbox: tuple[float, float, float],
    ) -> tuple[str, tuple[float, ...]]:
        if primitive == "sphere":
            return "sphere", (max(bbox) / 2.0,)
        if primitive in {"cylinder", "capsule"}:
            return primitive, (max(bbox[0], bbox[1]) / 2.0, bbox[2] / 2.0)
        return "box", tuple(value / 2.0 for value in bbox)
