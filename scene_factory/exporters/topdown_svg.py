from __future__ import annotations

import html
from pathlib import Path

from ..models import CompiledScene
from ..registry import AssetRegistry


class TopDownSvgExporter:
    def __init__(self, registry: AssetRegistry, width: int = 960, height: int = 720) -> None:
        self.registry = registry
        self.width = width
        self.height = height

    def export(self, scene: CompiledScene, output_path: str | Path) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        room_x, room_y, _ = scene.room_dimensions_m
        margin = 48
        scale = min((self.width - 2 * margin) / room_x, (self.height - 2 * margin) / room_y)
        room_width, room_height = room_x * scale, room_y * scale
        origin_x = (self.width - room_width) / 2.0
        origin_y = (self.height - room_height) / 2.0

        lines = [
            '<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {self.width} {self.height}" role="img" '
            f'aria-label="Top-down preview of {html.escape(scene.scene_id)}">',
            "<style>",
            "text{font-family:Arial,sans-serif;font-size:12px;fill:#202124}",
            ".room{fill:#f4f1ea;stroke:#4d5156;stroke-width:3}",
            ".object{stroke:#202124;stroke-width:1.2;fill-opacity:.88}",
            ".dynamic{stroke-dasharray:5 3;stroke-width:2}",
            "</style>",
            f'<rect class="room" x="{origin_x:.2f}" y="{origin_y:.2f}" '
            f'width="{room_width:.2f}" height="{room_height:.2f}"/>',
        ]

        for item in sorted(scene.objects, key=lambda obj: (obj.dynamic, obj.pose.position[2])):
            asset = self.registry.get(item.asset_id)
            x = origin_x + (item.pose.position[0] + room_x / 2.0) * scale
            y = origin_y + (room_y / 2.0 - item.pose.position[1]) * scale
            width, height = item.bbox_m[0] * scale, item.bbox_m[1] * scale
            color = "#%02x%02x%02x" % tuple(
                max(0, min(255, round(channel * 255))) for channel in asset.color
            )
            css_class = "object dynamic" if item.dynamic else "object"
            label = html.escape(item.object_id)
            lines.extend(
                [
                    f'<g transform="rotate({-item.pose.yaw_deg:.2f} {x:.2f} {y:.2f})">',
                    f'<rect class="{css_class}" x="{x - width / 2:.2f}" '
                    f'y="{y - height / 2:.2f}" width="{width:.2f}" height="{height:.2f}" '
                    f'rx="3" fill="{color}"><title>{label}</title></rect>',
                    "</g>",
                    f'<text x="{x:.2f}" y="{y + 4:.2f}" text-anchor="middle">{label}</text>',
                ]
            )

        lines.append("</svg>")
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return output_path

