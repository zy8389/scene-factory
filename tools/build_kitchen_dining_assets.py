"""Author the project's original kitchen/dining USD and GLB pack from one mesh source.

Only the standard library is needed. Geometry is in metres, Z up, centred on
the declared bounding box. Colliders are separate convex parts, preserving
open shelves and vessel cavities. No downloaded or third-party geometry.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WOOD = (0.48, 0.29, 0.14)
WHITE = (0.86, 0.85, 0.78)
METAL = (0.15, 0.18, 0.21)
BLUE = (0.13, 0.36, 0.65)


def box(name, size, center, color=WOOD):
    vertices = [
        (center[0] + x * size[0] / 2, center[1] + y * size[1] / 2, center[2] + z * size[2] / 2)
        for x, y, z in (
            (-1, -1, -1),
            (1, -1, -1),
            (1, 1, -1),
            (-1, 1, -1),
            (-1, -1, 1),
            (1, -1, 1),
            (1, 1, 1),
            (-1, 1, 1),
        )
    ]
    return (
        name,
        vertices,
        [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)],
        color,
    )


def cylinder(name, radius, height, z, color=WHITE, segments=32):
    vertices = [
        (
            radius * math.cos(i * 2 * math.pi / segments),
            radius * math.sin(i * 2 * math.pi / segments),
            z + side * height / 2,
        )
        for side in (-1, 1)
        for i in range(segments)
    ]
    faces = [tuple(reversed(range(segments))), tuple(range(segments, 2 * segments))]
    faces += [
        (i, (i + 1) % segments, (i + 1) % segments + segments, i + segments)
        for i in range(segments)
    ]
    return name, vertices, faces, color


def ring_parts(name, bottom_outer, top_outer, bottom_inner, top_inner, z0, z1, color, n=32):
    parts = []
    for i in range(n):
        angles = (i * 2 * math.pi / n, (i + 1) * 2 * math.pi / n)
        vertices = [
            (r * math.cos(a), r * math.sin(a), z)
            for r, z in ((bottom_outer, z0), (top_outer, z1), (top_inner, z1), (bottom_inner, z0))
            for a in angles
        ]
        faces = [(0, 1, 3, 2), (2, 3, 5, 4), (4, 5, 7, 6), (6, 7, 1, 0), (0, 2, 4, 6), (1, 7, 5, 3)]
        parts.append((f"{name}_{i:02}", vertices, faces, color))
    return parts


def definition(kind):
    parts, surfaces, interiors = [], [], []
    static = kind in {"table", "counter", "island", "cabinet", "shelf", "chair"}

    def surface(name, z, sx, sy):
        surfaces.append({"name": name, "center": [0, 0, z], "size": [sx, sy]})

    if kind in {"table", "counter", "island"}:
        size = {
            "table": (1.6, 0.9, 0.76),
            "counter": (2.2, 0.86, 0.9),
            "island": (1.4, 0.78, 0.92),
        }[kind]
        sx, sy, sz = size
        parts.append(box("Worktop", (sx, sy, 0.04), (0, 0, sz / 2 - 0.02), WHITE))
        if kind == "table":
            for i, x in enumerate((-sx / 2 + 0.08, sx / 2 - 0.08)):
                for j, y in enumerate((-sy / 2 + 0.08, sy / 2 - 0.08)):
                    parts.append(box(f"Leg_{i}_{j}", (0.065, 0.065, sz - 0.04), (x, y, -0.02)))
        else:
            parts.append(box("CabinetBody", (sx - 0.08, sy - 0.06, sz - 0.04), (0, 0, -0.02)))
            for i in range(4):
                parts.append(
                    box(
                        f"Door_{i}",
                        ((sx - 0.12) / 4 - 0.008, 0.02, sz - 0.13),
                        (-sx / 2 + 0.06 + (i + 0.5) * (sx - 0.12) / 4, -sy / 2 + 0.02, -0.015),
                        WHITE,
                    )
                )
                parts.append(
                    box(
                        f"Handle_{i}",
                        (0.10, 0.014, 0.016),
                        (
                            -sx / 2 + 0.06 + (i + 0.5) * (sx - 0.12) / 4,
                            -sy / 2 + 0.010,
                            sz / 2 - 0.16,
                        ),
                        METAL,
                    )
                )
        surface("top", sz / 2, sx - 0.06, sy - 0.06)
        category = {
            "table": "dining_table",
            "counter": "kitchen_counter",
            "island": "kitchen_island",
        }[kind]
        mass = 28 if kind == "table" else 65
    elif kind in {"cabinet", "shelf"}:
        size = (0.9, 0.5, 1.8) if kind == "cabinet" else (0.9, 0.4, 1.6)
        sx, sy, sz = size
        t = 0.025
        for i, x in enumerate((-sx / 2 + t / 2, sx / 2 - t / 2)):
            parts.append(box(f"Side_{i}", (t, sy, sz), (x, 0, 0)))
        parts.append(box("Back", (sx - 2 * t, t, sz), (0, sy / 2 - t / 2, 0), WHITE))
        levels = [-sz / 2 + t / 2, -sz / 6, sz / 6, sz / 2 - t / 2]
        for i, z in enumerate(levels):
            parts.append(box(f"Shelf_{i}", (sx - 2 * t, sy - t, t), (0, -t / 2, z)))
            if i < len(levels) - 1:
                floor = z + t / 2
                ceiling = levels[i + 1] - t / 2
                surface(f"shelf_{i}", floor, sx - 2 * t - 0.025, sy - 2 * t - 0.025)
                interiors.append(
                    {
                        "region_id": f"shelf_{i}",
                        "link": "body",
                        "center": [0, 0, (floor + ceiling) / 2],
                        "size": [sx - 2 * t - 0.025, sy - 2 * t - 0.025, ceiling - floor],
                    }
                )
        surface("top", sz / 2, sx - 0.06, sy - 0.06)
        category = "kitchen_cabinet" if kind == "cabinet" else "storage_shelf"
        mass = 32
    elif kind == "chair":
        size = (0.46, 0.5, 0.86)
        parts.append(box("Seat", (0.46, 0.45, 0.045), (0, -0.025, 0.015), WHITE))
        for i, x in enumerate((-0.18, 0.18)):
            for j, y in enumerate((-0.19, 0.18)):
                parts.append(box(f"Leg_{i}_{j}", (0.04, 0.04, 0.425), (x, y, -0.2175)))
        parts.append(box("Backrest", (0.46, 0.04, 0.385), (0, 0.23, 0.2375)))
        surface("seat", 0.0375, 0.40, 0.36)
        category, mass = "chair", 5.0
    elif kind == "mug":
        size = (0.12, 0.09, 0.11)
        # The handle extends in +X; body and complete asset bounds stay centred.
        parts.append(cylinder("Bottom", 0.045, 0.006, -0.052, color=BLUE))
        parts += ring_parts("Wall", 0.045, 0.045, 0.039, 0.039, -0.049, 0.055, BLUE)
        # Shift body to leave room for the handle, then make a genuine open handle.
        parts = [(n, [(x - 0.015, y, z) for x, y, z in v], f, c) for n, v, f, c in parts]
        parts += [
            box("HandleTop", (0.032, 0.014, 0.012), (0.044, 0, 0.029), BLUE),
            box("HandleEnd", (0.012, 0.014, 0.070), (0.054, 0, 0), BLUE),
            box("HandleBottom", (0.032, 0.014, 0.012), (0.044, 0, -0.029), BLUE),
        ]
        category, mass = "mug", 0.30
    elif kind in {"bowl", "plate"}:
        size = (0.18, 0.18, 0.07) if kind == "bowl" else (0.24, 0.24, 0.024)
        radius = size[0] / 2
        h = size[2]
        base_r = 0.046 if kind == "bowl" else 0.088
        parts.append(cylinder("Bottom", base_r, 0.006, -h / 2 + 0.003))
        parts += ring_parts(
            "Rim", base_r, radius, base_r - 0.004, radius - 0.004, -h / 2 + 0.006, h / 2, WHITE
        )
        category, mass = kind, 0.28 if kind == "bowl" else 0.25
    elif kind == "bottle":
        size = (0.075, 0.075, 0.24)
        parts = [
            cylinder("Body", 0.0375, 0.18, -0.03, BLUE),
            cylinder("Shoulder", 0.030, 0.025, 0.0725, BLUE),
            cylinder("Neck", 0.016, 0.025, 0.0975, BLUE),
            cylinder("Cap", 0.018, 0.010, 0.115, WHITE),
        ]
        category, mass = "bottle", 0.4
    else:
        size = (0.18, 0.14, 0.12)
        parts = [box("Body", size, (0, 0, 0), (0.65, 0.43, 0.22))]
        category, mass = "box", 0.2
    return {
        "asset_id": f"kd_{kind}_01",
        "category": category,
        "bbox_m": size,
        "static": static,
        "mass": mass,
        "parts": parts,
        "support_surface": surfaces,
        "interior_regions": interiors,
    }


KINDS = (
    "table",
    "counter",
    "island",
    "cabinet",
    "shelf",
    "chair",
    "mug",
    "bowl",
    "plate",
    "bottle",
    "box",
)


def glb_bytes(parts):
    data = {
        "asset": {"version": "2.0", "generator": "SceneFactory original kitchen/dining pack"},
        "scene": 0,
        "scenes": [{"nodes": []}],
        "nodes": [],
        "meshes": [],
        "materials": [],
        "accessors": [],
        "bufferViews": [],
        "buffers": [],
    }
    binary = bytearray()

    def accessor(values, code, component, count, type_, **extra):
        while len(binary) % 4:
            binary.append(0)
        start = len(binary)
        binary.extend(struct.pack("<" + code * len(values), *values))
        view = len(data["bufferViews"])
        data["bufferViews"].append(
            {"buffer": 0, "byteOffset": start, "byteLength": len(binary) - start}
        )
        index = len(data["accessors"])
        data["accessors"].append(
            {"bufferView": view, "componentType": component, "count": count, "type": type_, **extra}
        )
        return index

    for name, vertices, faces, color in parts:
        # Flat per-face normals keep furniture faces crisp and shade curved facets correctly.
        points, normals, indices = [], [], []
        for face in faces:
            a, b, c = [vertices[i] for i in face[:3]]
            u = [b[i] - a[i] for i in range(3)]
            v = [c[i] - a[i] for i in range(3)]
            normal = [
                u[1] * v[2] - u[2] * v[1],
                u[2] * v[0] - u[0] * v[2],
                u[0] * v[1] - u[1] * v[0],
            ]
            length = math.sqrt(sum(x * x for x in normal))
            normal = [x / length for x in normal]
            start = len(points)
            for i in face:
                points.append(vertices[i])
                normals.append(normal)
            for i in range(1, len(face) - 1):
                indices.extend((start, start + i, start + i + 1))
        bounds = {
            "min": [min(v[i] for v in points) for i in range(3)],
            "max": [max(v[i] for v in points) for i in range(3)],
        }
        pos = accessor([x for p in points for x in p], "f", 5126, len(points), "VEC3", **bounds)
        norm = accessor([x for p in normals for x in p], "f", 5126, len(normals), "VEC3")
        idx = accessor(indices, "I", 5125, len(indices), "SCALAR")
        material = len(data["materials"])
        data["materials"].append(
            {
                "name": name,
                "pbrMetallicRoughness": {
                    "baseColorFactor": [*color, 1],
                    "metallicFactor": 0.0,
                    "roughnessFactor": 0.65,
                },
            }
        )
        mesh = len(data["meshes"])
        data["meshes"].append(
            {
                "name": name,
                "primitives": [
                    {
                        "attributes": {"POSITION": pos, "NORMAL": norm},
                        "indices": idx,
                        "material": material,
                    }
                ],
            }
        )
        data["scenes"][0]["nodes"].append(len(data["nodes"]))
        data["nodes"].append({"name": name, "mesh": mesh})
    while len(binary) % 4:
        binary.append(0)
    # glTF convention is Y up. Keep canonical meshes in Z up under a -90°
    # basis node; the documented +90° import transform restores canonical axes.
    children = data["scenes"][0]["nodes"]
    data["scenes"][0]["nodes"] = [len(data["nodes"])]
    data["nodes"].append(
        {
            "name": "Z_to_glTF",
            "children": children,
            "rotation": [-math.sqrt(0.5), 0, 0, math.sqrt(0.5)],
        }
    )
    data["buffers"] = [{"byteLength": len(binary)}]
    encoded = json.dumps(data, separators=(",", ":")).encode()
    encoded += b" " * ((-len(encoded)) % 4)
    return (
        struct.pack("<III", 0x46546C67, 2, 28 + len(encoded) + len(binary))
        + struct.pack("<II", len(encoded), 0x4E4F534A)
        + encoded
        + struct.pack("<II", len(binary), 0x004E4942)
        + binary
    )


def usd_text(asset, collision=False):
    root = "Asset"
    lines = [
        "#usda 1.0",
        '(\n    defaultPrim = "Asset"\n    metersPerUnit = 1\n    upAxis = "Z"\n)',
        f'def Xform "{root}" {{',
        '    def Scope "Materials" {',
    ]
    for index, (_, _, _, color) in enumerate(asset["parts"]):
        lines += [
            f'        def Material "M{index}" {{',
            f"            token outputs:surface.connect = </Asset/Materials/M{index}/Shader.outputs:surface>",
            '            def Shader "Shader" {',
            '                uniform token info:id = "UsdPreviewSurface"',
            f"                color3f inputs:diffuseColor = {tuple(color)}",
            "                float inputs:roughness = 0.65",
            "                token outputs:surface",
            "            }",
            "        }",
        ]
    lines += [
        '        def Material "Physics" (prepend apiSchemas = ["PhysicsMaterialAPI"]) {',
        "            float physics:staticFriction = 0.55",
        "            float physics:dynamicFriction = 0.4",
        "            float physics:restitution = 0",
        "        }",
        "    }",
    ]
    for index, (name, vertices, faces, _) in enumerate(asset["parts"]):
        apis = (
            '["MaterialBindingAPI", "PhysicsCollisionAPI", "PhysicsMeshCollisionAPI"]'
            if collision
            else '["MaterialBindingAPI"]'
        )
        lines += [
            f'    def Mesh "{name}" (prepend apiSchemas = {apis}) {{',
            '        uniform token subdivisionScheme = "none"',
            "        point3f[] points = ["
            + ", ".join(str(tuple(round(x, 9) for x in v)) for v in vertices)
            + "]",
            "        int[] faceVertexCounts = " + str([len(f) for f in faces]),
            "        int[] faceVertexIndices = " + str([i for f in faces for i in f]),
            f"        rel material:binding = </Asset/Materials/M{index}>",
        ]
        if collision:
            lines += [
                '        token visibility = "invisible"',
                "        bool physics:collisionEnabled = true",
                '        token physics:approximation = "convexHull"',
                "        rel material:binding:physics = </Asset/Materials/Physics>",
            ]
        lines.append("    }")
    return "\n".join(lines + ["}"]) + "\n"


def build(root=ROOT):
    asset_root = Path(root) / "data/assets"
    records = []
    for kind in KINDS:
        asset = definition(kind)
        asset_id = asset["asset_id"]
        folder = asset_root / "source" / asset_id
        folder.mkdir(parents=True, exist_ok=True)
        visual = glb_bytes(asset["parts"])
        (folder / "visual.glb").write_bytes(visual)
        geometry = json.dumps(
            {
                "parts": [
                    {"name": name, "vertices": vertices, "faces": faces, "color": color}
                    for name, vertices, faces, color in asset["parts"]
                ]
            },
            separators=(",", ":"),
        ).encode()
        (folder / "geometry.json").write_bytes(geometry)
        for collision, subdir in ((False, "usd"), (True, "collision")):
            target = asset_root / subdir / f"{asset_id}.usd"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(usd_text(asset, collision), encoding="utf-8")
        manifest = {
            "asset_id": asset_id,
            "source": "SceneFactory original procedural geometry",
            "license": "MIT",
            "source_geometry": "visual.glb",
            "sha256": hashlib.sha256(visual).hexdigest(),
            "visual_transform": {
                "up_axis": "Y",
                "rotation_euler_deg": [90, 0, 0],
                "anchor": "bottom_center",
            },
            "bbox_m": asset["bbox_m"],
            "units": "m",
            "origin": "bbox_center",
            "generator": "tools/build_kitchen_dining_assets.py",
            "collision_strategy": "separate_convex_parts",
        }
        manifest.update(
            {
                "source_url": "tools/build_kitchen_dining_assets.py",
                "source_revision": "sha256:"
                + hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "source_name": "SceneFactory original kitchen/dining pack",
                "geometry_origin": "procedural",
                "license_source": "LICENSE",
                "source_files": [
                    {
                        "role": "visual",
                        "path": "visual.glb",
                        "sha256": hashlib.sha256(visual).hexdigest(),
                        "bytes": len(visual),
                    }
                ],
            }
        )
        manifest["geometry_sha256"] = hashlib.sha256(geometry).hexdigest()
        manifest["usd_sha256"] = hashlib.sha256(
            (asset_root / "usd" / f"{asset_id}.usd").read_bytes()
        ).hexdigest()
        manifest["collision_sha256"] = hashlib.sha256(
            (asset_root / "collision" / f"{asset_id}.usd").read_bytes()
        ).hexdigest()
        (folder / "SOURCE.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        records.append(
            {
                "asset_id": asset_id,
                "name": f"Kitchen/dining {kind}",
                "category": asset["category"],
                "bbox_m": asset["bbox_m"],
                "primitive": "cube",
                "color": list(asset["parts"][0][3]),
                "mass": asset["mass"],
                "friction": 0.4,
                "static_friction": 0.55,
                "dynamic_friction": 0.4,
                "rigid_body": not asset["static"],
                "collision_enabled": True,
                "source_type": "local_usd",
                "usd_path": f"usd/{asset_id}.usd",
                "source_path": f"usd/{asset_id}.usd",
                "collision_path": f"collision/{asset_id}.usd",
                "collision_status": "authored",
                "collision_mode": "authored",
                "source": "SceneFactory original procedural geometry",
                "license": "MIT",
                "hash": hashlib.sha256(
                    (asset_root / "usd" / f"{asset_id}.usd").read_bytes()
                ).hexdigest(),
                "status": "normalized",
                "support_surface": asset["support_surface"],
                "interior_regions": asset["interior_regions"],
                "tags": ["kitchen_dining_pack", "original_geometry"],
                "physics_parameters_source": "project_default",
            }
        )
    registry_path = asset_root / "registry.jsonl"
    old = registry_path.read_text(encoding="utf-8").splitlines() if registry_path.exists() else []
    existing = {
        json.loads(line)["asset_id"]: json.loads(line)
        for line in old
        if line.strip() and not line.startswith("#")
    }
    for record in records:
        prior = existing.get(record["asset_id"], {})
        # Preserve QA only when the exact USD/collision/GLB is unchanged.
        report_path = asset_root / prior.get("qa_report", "__missing__")
        report = json.loads(report_path.read_text()) if report_path.is_file() else {}
        source = json.loads(
            (asset_root / "source" / record["asset_id"] / "SOURCE.json").read_text()
        )
        unchanged = (
            report.get("usd_sha256") == source["usd_sha256"]
            and report.get("collision_sha256") == source["collision_sha256"]
            and report.get("glb_sha256") == source["sha256"]
        )
        if prior.get("hash") == record["hash"] and unchanged:
            for key in ("status", "collision_status", "qa_report"):
                if key in prior:
                    record[key] = prior[key]
    ids = {r["asset_id"] for r in records}
    retained = [
        line
        for line in old
        if not line.strip() or line.startswith("#") or json.loads(line)["asset_id"] not in ids
    ]
    registry_path.write_text(
        "\n".join(retained + [json.dumps(r, separators=(",", ":")) for r in records]) + "\n",
        encoding="utf-8",
    )
    return records


if __name__ == "__main__":
    print(json.dumps({"assets": [r["asset_id"] for r in build()]}))
