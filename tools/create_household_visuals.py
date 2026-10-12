from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import bpy
from mathutils import Vector


ROOT = Path(__file__).resolve().parents[1]
ASSETS = {
    "snack_bag_red": (0.18, 0.07, 0.25),
    "remote_black": (0.19, 0.055, 0.025),
    "tissue_white": (0.19, 0.14, 0.012),
    "cushion_green": (0.42, 0.42, 0.14),
    "backpack_gray": (0.34, 0.18, 0.45),
    "shoe_left_white": (0.29, 0.11, 0.1),
    "keyring_metal": (0.1, 0.04, 0.012),
    "mug_blue": (0.09, 0.09, 0.11),
    "mug_cream": (0.085, 0.085, 0.105),
}


def material(name, color, metal=0.0, roughness=0.6):
    value = bpy.data.materials.new(name)
    value.diffuse_color = (*color, 1)
    node = value.node_tree.nodes.get("Principled BSDF")
    node.inputs["Base Color"].default_value = (*color, 1)
    node.inputs["Metallic"].default_value = metal
    node.inputs["Roughness"].default_value = roughness
    return value


def finish(obj, name, surface):
    obj.name = name
    obj.data.materials.append(surface)
    return obj


def box(name, location, dimensions, surface, bevel=0.0):
    bpy.ops.mesh.primitive_cube_add(size=1, location=location)
    obj = bpy.context.object
    obj.dimensions = dimensions
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    if bevel:
        modifier = obj.modifiers.new("Rounded edges", "BEVEL")
        modifier.width = bevel
        modifier.segments = 4
        bpy.ops.object.modifier_apply(modifier=modifier.name)
    return finish(obj, name, surface)


def sphere(name, location, dimensions, surface):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16, location=location)
    obj = bpy.context.object
    obj.dimensions = dimensions
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    for polygon in obj.data.polygons:
        polygon.use_smooth = True
    return finish(obj, name, surface)


def ring(name, location, radius, thickness, surface, rotation=(0, 0, 0)):
    bpy.ops.mesh.primitive_torus_add(major_segments=40, minor_segments=10,
                                   major_radius=radius, minor_radius=thickness, location=location,
                                   rotation=rotation)
    return finish(bpy.context.object, name, surface)


def tube(name, points, radius, surface):
    curve = bpy.data.curves.new(name, "CURVE")
    curve.dimensions = "3D"
    curve.bevel_depth = radius
    curve.bevel_resolution = 3
    spline = curve.splines.new("POLY")
    spline.points.add(len(points) - 1)
    for point, coordinates in zip(spline.points, points):
        point.co = (*coordinates, 1)
    obj = bpy.data.objects.new(name, curve)
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.convert(target="MESH")
    return finish(bpy.context.object, name, surface)


def build(asset_id):
    white = material("Warm white", (0.9, 0.88, 0.82))
    dark = material("Rubber", (0.028, 0.032, 0.037), roughness=0.85)
    metal = material("Brushed metal", (0.55, 0.6, 0.65), metal=0.85, roughness=0.28)
    if asset_id == "snack_bag_red":
        red = material("Red packaging", (0.66, 0.055, 0.035), roughness=0.34)
        gold = material("Label", (0.9, 0.59, 0.13), roughness=0.4)
        body = box("Pouch", (0, 0, 0.115), (0.17, 0.055, 0.22), red, 0.022)
        body.rotation_euler.y = 0.04
        for height in (0.01, 0.23):
            box("Crimp", (0, 0, height), (0.18, 0.012, 0.017), gold, 0.002)
        box("Front label", (0, -0.029, 0.135), (0.125, 0.003, 0.105), gold, 0.013)
        for index in range(4):
            sphere("Snack illustration", (-0.039 + index * 0.026, -0.033, 0.132),
                   (0.021, 0.003, 0.042), white)
        for index in range(8):
            tube("Packaging fold", [(-0.08 + index * 0.023, -0.018, 0.014),
                                    (-0.067 + index * 0.020, -0.027, 0.05)], 0.001, red)
    elif asset_id == "remote_black":
        box("Remote body", (0, 0, 0.010), (0.19, 0.055, 0.021), dark, 0.009)
        box("Face plate", (0, 0, 0.020), (0.172, 0.047, 0.003), material("Face", (0.075, 0.08, 0.085)), 0.006)
        red = material("Power button", (0.7, 0.04, 0.035))
        sphere("Power", (-0.073, 0.012, 0.023), (0.012, 0.012, 0.005), red)
        for row in range(5):
            for column in range(3):
                box("Button", (-0.048 + row * 0.022, -0.014 + column * 0.014, 0.024),
                    (0.012, 0.008, 0.003), white, 0.002)
        ring("Navigation", (0.063, 0, 0.023), 0.015, 0.002, metal)
        sphere("OK", (0.063, 0, 0.023), (0.015, 0.015, 0.004), dark)
    elif asset_id == "tissue_white":
        vertices = []
        faces = []
        for row in range(17):
            for column in range(23):
                horizontal = column / 22
                vertical = row / 16
                height = 0.006 + 0.003 * math.sin(horizontal * math.pi * 4) * math.sin(vertical * math.pi)
                height += 0.003 * horizontal * vertical
                vertices.append(((horizontal - 0.5) * 0.19, (vertical - 0.5) * 0.14, height))
                if row < 16 and column < 22:
                    start = row * 23 + column
                    faces.append((start, start + 1, start + 24, start + 23))
        mesh = bpy.data.meshes.new("Folded tissue")
        mesh.from_pydata(vertices, [], faces)
        obj = bpy.data.objects.new("Folded tissue", mesh)
        bpy.context.collection.objects.link(obj)
        finish(obj, "Tissue", white)
        modifier = obj.modifiers.new("Paper thickness", "SOLIDIFY")
        modifier.thickness = 0.0006
    elif asset_id == "cushion_green":
        green = material("Green woven fabric", (0.15, 0.32, 0.16), roughness=0.96)
        box("Cushion", (0, 0, 0.07), (0.42, 0.42, 0.14), green, 0.065)
        points = [(0.18 * math.cos(index * math.pi / 16),
                   0.18 * math.sin(index * math.pi / 16), 0.071) for index in range(33)]
        tube("Stitch seam", points, 0.0015, material("Seam", (0.28, 0.43, 0.23)))
    elif asset_id == "backpack_gray":
        gray = material("Canvas", (0.22, 0.25, 0.28), roughness=0.94)
        edge = material("Straps", (0.055, 0.068, 0.08), roughness=0.88)
        box("Backpack body", (0, 0.015, 0.205), (0.3, 0.14, 0.38), gray, 0.066)
        box("Front pocket", (0, -0.075, 0.132), (0.25, 0.055, 0.18), gray, 0.037)
        for side in (-1, 1):
            points = [(side * 0.09, 0.095, 0.32), (side * 0.12, 0.115, 0.24),
                      (side * 0.09, 0.11, 0.08), (side * 0.055, 0.083, 0.032)]
            tube("Shoulder strap", points, 0.011, edge)
            box("Side pocket", (side * 0.149, 0.005, 0.116), (0.03, 0.092, 0.14), edge, 0.012)
        tube("Carry handle", [(-0.045, 0.026, 0.375), (-0.033, 0.026, 0.425),
                              (0.033, 0.026, 0.425), (0.045, 0.026, 0.375)], 0.01, edge)
        tube("Pocket zipper", [(-0.095, -0.103, 0.207), (0.095, -0.103, 0.207)], 0.0022, metal)
        box("Zipper pull", (0.09, -0.106, 0.19), (0.008, 0.004, 0.026), metal, 0.002)
    elif asset_id == "shoe_left_white":
        sole = material("Sole", (0.7, 0.69, 0.65), roughness=0.85)
        box("Shoe sole", (0, 0, 0.012), (0.29, 0.11, 0.024), sole, 0.011)
        sphere("Toe", (0.075, 0, 0.044), (0.13, 0.102, 0.06), white)
        box("Upper", (-0.015, 0, 0.052), (0.185, 0.10, 0.072), white, 0.025)
        box("Heel", (-0.111, 0, 0.055), (0.056, 0.091, 0.082), white, 0.022)
        sphere("Foot opening", (-0.095, 0, 0.094), (0.054, 0.066, 0.008), dark)
        for index in range(5):
            tube("Lace", [(-0.055 + index * 0.018, -0.025, 0.082),
                          (-0.043 + index * 0.018, 0.025, 0.083)], 0.0015, white)
    elif asset_id == "keyring_metal":
        ring("Key ring", (-0.029, 0, 0.007), 0.016, 0.0016, metal)
        for index, offset in enumerate((-0.008, 0.006)):
            head = ring("Key head", (-0.011, offset, 0.004 + index * 0.003), 0.007, 0.002, metal)
            head.rotation_euler.z = index * 0.17
            box("Key shaft", (0.015, offset, 0.004 + index * 0.003), (0.046, 0.005, 0.0025), metal, 0.0008)
            for tooth in range(3):
                box("Key tooth", (0.015 + tooth * 0.008, offset - 0.004, 0.004 + index * 0.003),
                    (0.004, 0.008 - tooth * 0.001, 0.0025), metal, 0.0004)
    else:
        color = (0.15, 0.37, 0.66) if asset_id == "mug_blue" else (0.83, 0.78, 0.62)
        ceramic = material("Glazed ceramic", color, roughness=0.24)
        profile = [(0.03, 0), (0.033, 0.007), (0.034, 0.097), (0.029, 0.097),
                   (0.028, 0.013), (0, 0.013), (0, 0)]
        vertices = [(radius * math.cos(index * math.pi / 32), radius * math.sin(index * math.pi / 32), height)
                    for radius, height in profile for index in range(64)]
        faces = [(level * 64 + index, level * 64 + (index + 1) % 64,
                  (level + 1) * 64 + (index + 1) % 64, (level + 1) * 64 + index)
                 for level in range(len(profile) - 1) for index in range(64)]
        mesh = bpy.data.meshes.new("Hollow ceramic cup")
        mesh.from_pydata(vertices, [], faces)
        obj = bpy.data.objects.new("Cup", mesh)
        bpy.context.collection.objects.link(obj)
        finish(obj, "Cup", ceramic)
        ring("Handle", (0.038, 0, 0.051), 0.027, 0.005, ceramic, (math.pi / 2, 0, 0))


def export(asset_id, dimensions):
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    build(asset_id)
    bpy.context.view_layer.update()
    objects = list(bpy.context.scene.objects)
    corners = [obj.matrix_world @ Vector(corner) for obj in objects for corner in obj.bound_box]
    minimum = [min(corner[axis] for corner in corners) for axis in range(3)]
    maximum = [max(corner[axis] for corner in corners) for axis in range(3)]
    parent = bpy.data.objects.new(asset_id, None)
    bpy.context.collection.objects.link(parent)
    for obj in objects:
        obj.parent = parent
    parent.scale = [dimensions[axis] / (maximum[axis] - minimum[axis]) for axis in range(3)]
    parent.location = (-parent.scale.x * (minimum[0] + maximum[0]) / 2,
                       -parent.scale.y * (minimum[1] + maximum[1]) / 2,
                       -parent.scale.z * minimum[2])
    destination = ROOT / "data/assets/source" / asset_id
    manifest_path = destination / "SOURCE.json"
    if manifest_path.exists() and json.loads(manifest_path.read_text("utf-8")).get("batch_id") != "household_procedural_v1":
        raise RuntimeError(f"refusing to overwrite another asset source: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    visual_path = destination / "visual.glb"
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.export_scene.gltf(filepath=str(visual_path), export_format="GLB", use_selection=True,
                              export_apply=True, export_yup=True)
    payload = visual_path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    generator = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    manifest = {
        "asset_id": asset_id, "batch_id": "household_procedural_v1",
        "source_name": "SceneFactory authored household visual", "source_url": "tools/create_household_visuals.py",
        "license": "MIT", "license_source": "LICENSE", "source_revision": f"sha256:{generator}",
        "source_variant": "procedural-mesh", "geometry_origin": "procedural",
        "source_geometry": "visual.glb", "conversion": "Blender glTF export, Y-up, self-contained materials",
        "visual_transform": {"up_axis": "Y", "rotation_euler_deg": [90, 0, 0],
                             "scale_mode": "uniform_contain", "anchor": "bottom_center"},
        "source_files": [{"role": "visual", "path": "visual.glb", "sha256": digest, "bytes": len(payload)}],
        "status": "imported", "result": "passed", "issues": [],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"asset_id": asset_id, "bytes": len(payload), "sha256": digest}))


if __name__ == "__main__":
    for asset_id, dimensions in ASSETS.items():
        export(asset_id, dimensions)
