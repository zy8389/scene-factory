"""Standalone Blender Python renderer; requires bpy only when executed by Blender."""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path


def _cube(bpy, name, location, dimensions, color):
    bpy.ops.mesh.primitive_cube_add(size=1, location=location)
    obj = bpy.context.object
    obj.name = name
    obj.dimensions = dimensions
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    material = bpy.data.materials.new(name + "Material")
    material.diffuse_color = (*color, 1)
    material.use_nodes = True
    node = material.node_tree.nodes.get("Principled BSDF")
    if node:
        node.inputs["Base Color"].default_value = (*color, 1)
    obj.data.materials.append(material)
    return obj


def _visual(bpy, item, root, holder):
    from mathutils import Vector

    path = (root / item["visual"]).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return False
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=str(path))
    imported = set(bpy.data.objects) - before
    meshes = [obj for obj in imported if obj.type == "MESH"]
    if not meshes:
        return False
    oriented = bpy.data.objects.new(item["object_id"] + "_visual", None)
    bpy.context.collection.objects.link(oriented)
    oriented.parent = holder
    rotation = (item.get("visual_transform") or {}).get("rotation_euler_deg", [90, 0, 0])
    oriented.rotation_euler = tuple(math.radians(float(angle)) for angle in rotation)
    gltf_basis = bpy.data.objects.new(item["object_id"] + "_gltf_basis", None)
    bpy.context.collection.objects.link(gltf_basis)
    gltf_basis.parent = oriented
    gltf_basis.rotation_euler = (-math.pi / 2, 0, 0)
    roots = [obj for obj in imported if obj.parent not in imported]
    for obj in roots:
        obj.parent = gltf_basis
    bpy.context.view_layer.update()
    corners = [holder.matrix_world.inverted() @ obj.matrix_world @ Vector(corner)
               for obj in meshes for corner in obj.bound_box]
    minimum = [min(corner[axis] for corner in corners) for axis in range(3)]
    maximum = [max(corner[axis] for corner in corners) for axis in range(3)]
    scale, location = _fit_transform(minimum, maximum, item["bbox_m"])
    oriented.scale = (scale,) * 3
    oriented.location = location
    return True


def _fit_transform(minimum, maximum, bbox):
    scale = min(
        float(bbox[axis]) / max(maximum[axis] - minimum[axis], 1e-6)
        for axis in range(3)
    )
    return scale, (
        -scale * (minimum[0] + maximum[0]) / 2,
        -scale * (minimum[1] + maximum[1]) / 2,
        -float(bbox[2]) / 2 - scale * minimum[2],
    )


def build(manifest_path: Path, blend_path: Path, png_path: Path | None = None) -> None:
    import bpy
    from mathutils import Vector

    manifest_path = manifest_path.resolve()
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if data.get("schema") != "scene_factory.blender.v1":
        raise ValueError("Unknown Blender manifest schema")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    width, depth, height = map(float, data["room_dimensions_m"])
    _cube(bpy, "Floor", (0, 0, -0.02), (width, depth, 0.04), (0.72, 0.70, 0.65))
    _cube(bpy, "BackWall", (0, depth / 2 + 0.04, height / 2),
          (width, 0.08, height), (0.90, 0.89, 0.85))
    _cube(bpy, "LeftWall", (-width / 2 - 0.04, 0, height / 2),
          (0.08, depth, height), (0.90, 0.89, 0.85))
    for item in data["objects"]:
        holder = bpy.data.objects.new(item["object_id"], None)
        bpy.context.collection.objects.link(holder)
        holder.location = item["pose"]["position"]
        holder.rotation_euler.z = math.radians(float(item["pose"].get("yaw_deg", 0)))
        if item.get("visual") and _visual(bpy, item, manifest_path.parent, holder):
            continue
        dims = item["bbox_m"]
        proxy = _cube(bpy, item["object_id"] + "_proxy", (0, 0, 0),
                      dims, item["color"])
        proxy.parent = holder
    camera_data = bpy.data.cameras.new("SceneCamera")
    camera = bpy.data.objects.new("SceneCamera", camera_data)
    bpy.context.collection.objects.link(camera)
    camera.location = (width * 0.9, -depth * 1.1, height * 1.35)
    target = Vector((0, 0, min(height * 0.3, 1.0)))
    camera.rotation_euler = (target - camera.location).to_track_quat("-Z", "Y").to_euler()
    camera_data.type = "ORTHO"
    camera_data.ortho_scale = max(width * 1.8, depth * 1.8)
    bpy.context.scene.camera = camera
    for index, location in enumerate(((0, 0, height * 0.9), (width * 0.3, -depth * 0.4, height * 1.2))):
        lamp_data = bpy.data.lights.new(f"Area{index}", "AREA")
        lamp_data.energy = 850
        lamp_data.shape = "DISK"
        lamp_data.size = max(width, depth) * 0.7
        lamp = bpy.data.objects.new(f"Area{index}", lamp_data)
        bpy.context.collection.objects.link(lamp)
        lamp.location = location
        lamp.rotation_euler = (target - lamp.location).to_track_quat("-Z", "Y").to_euler()
    scene = bpy.context.scene
    scene.render.resolution_x = 1280
    scene.render.resolution_y = 720
    scene.render.resolution_percentage = 100
    blend_path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
    if png_path is not None:
        png_path.parent.mkdir(parents=True, exist_ok=True)
        scene.render.filepath = str(png_path)
        scene.render.image_settings.file_format = "PNG"
        bpy.ops.render.render(write_still=True)


def main(argv=None):
    args = sys.argv[sys.argv.index("--") + 1:] if argv is None and "--" in sys.argv else argv
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--blend", type=Path, required=True)
    parser.add_argument("--png", type=Path)
    options = parser.parse_args(args)
    build(options.manifest, options.blend, options.png)


if __name__ == "__main__":
    main()
