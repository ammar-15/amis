"""Render the three Phase 2 stills in Blender's bundled Python.

Reads only the three data/processed contract files; numpy and Blender APIs only.
Run: Blender --background --factory-startup --python scripts/02_build_scene.py
"""

from pathlib import Path
import base64
import json
import math
import zlib

import bpy
from mathutils import Vector
import numpy as np


ROOT = Path(__file__).resolve().parents[1]


def material(name, color, emission=0):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    shader = mat.node_tree.nodes.get("Principled BSDF")
    shader.inputs["Base Color"].default_value = (*color, 1)
    shader.inputs["Roughness"].default_value = .88
    shader.inputs["Emission Color"].default_value = (*color, 1)
    shader.inputs["Emission Strength"].default_value = emission
    return mat


def linear_hex(value):
    rgb = np.array([int(value[i:i+2], 16) / 255 for i in (1, 3, 5)])
    return tuple(np.where(rgb <= .04045, rgb / 12.92, ((rgb + .055) / 1.055) ** 2.4))


def terrain(meta):
    image = bpy.data.images.load(str(ROOT / "data/processed/heightmap.png"), check_existing=False)
    image.colorspace_settings.name = "Non-Color"
    n = meta["raster_px"]
    assert tuple(image.size) == (n, n)
    rgba = np.empty(n * n * 4, dtype=np.float32)
    image.pixels.foreach_get(rgba)
    # Blender exposes image pixels bottom-up: rows here already run south -> north.
    heights = rgba.reshape(n, n, 4)[:, :, 0]
    height_scale = ((meta["elevation_max_m"] - meta["elevation_min_m"])
                    * meta["scene_units_per_m"] * meta["vertical_exaggeration"])
    heights = np.pad(heights * height_scale, 1, mode="edge")
    encoded = meta["filled_nodata_mask"]
    assert encoded["encoding"] == "base64-zlib-packbits-little"
    assert encoded["shape"] == [n, n] and encoded["row_order"] == "north_to_south"
    packed = np.frombuffer(zlib.decompress(base64.b64decode(encoded["data"])), dtype=np.uint8)
    missing = np.unpackbits(packed, bitorder="little", count=n*n).reshape(n, n).astype(bool)
    assert int(missing.sum()) == meta["nodata_pixels_filled"]
    # Match Blender's south-to-north vertex rows and duplicated edge samples.
    missing = np.pad(missing[::-1], 1, mode="edge")
    # A face touching any filled height sample includes unsupported elevation.
    filled_faces = (missing[:-1, :-1] | missing[:-1, 1:]
                    | missing[1:, :-1] | missing[1:, 1:]).ravel()
    extent = meta["scene_extent"]
    coords = np.concatenate(([-extent / 2], (np.arange(n) + .5) / n * extent - extent / 2,
                             [extent / 2])).astype(np.float32)
    side = n + 2
    vertices = np.empty((side * side, 3), dtype=np.float32)
    vertices[:, 0] = np.tile(coords, side)
    vertices[:, 1] = np.repeat(coords, side)
    vertices[:, 2] = heights.ravel()
    start = (np.arange(side - 1)[:, None] * side + np.arange(side - 1)).ravel()
    faces = np.column_stack((start, start + 1, start + side + 1, start + side)).astype(np.int32)
    mesh = bpy.data.meshes.new("Heightmap grid — all native samples")
    mesh.vertices.add(len(vertices))
    mesh.vertices.foreach_set("co", vertices.ravel())
    mesh.loops.add(faces.size)
    mesh.loops.foreach_set("vertex_index", faces.ravel())
    mesh.polygons.add(len(faces))
    mesh.polygons.foreach_set("loop_start", np.arange(len(faces), dtype=np.int32) * 4)
    mesh.polygons.foreach_set("loop_total", np.full(len(faces), 4, dtype=np.int32))
    mesh.polygons.foreach_set("use_smooth", ~filled_faces)
    provenance = mesh.attributes.new("filled_nodata", "BOOLEAN", "FACE")
    provenance.data.foreach_set("value", filled_faces)
    mesh.update()
    obj = bpy.data.objects.new("Terrain", mesh)
    bpy.context.collection.objects.link(obj)
    obj.data.materials.append(material("Neutral matte terrain", (.27, .29, .28)))
    nodata_material = bpy.data.materials.new("No elevation data — flat blue")
    nodata_material.use_nodes = True
    nodes, links = nodata_material.node_tree.nodes, nodata_material.node_tree.links
    flat = nodes.new("ShaderNodeEmission")
    flat.inputs["Color"].default_value = (.12, .30, .43, 1)
    flat.inputs["Strength"].default_value = .8
    links.new(flat.outputs[0], nodes.get("Material Output").inputs["Surface"])
    obj.data.materials.append(nodata_material)
    mesh.polygons.foreach_set("material_index", filled_faces.astype(np.int32))
    print(f"Nodata provenance: {meta['nodata_pixels_filled']:,} filled pixels; "
          f"{filled_faces.sum():,} tagged faces", flush=True)
    print(f"Terrain: {len(vertices):,} vertices, {len(faces):,} quads", flush=True)
    return height_scale


def cones(markers, mapping):
    assert mapping["marker_height_from"] == "severity"
    assert mapping["marker_color_from"] == "confidence"
    # Cone base at z=0 so point coordinates are the exact contact locations.
    bpy.ops.mesh.primitive_cone_add(vertices=12, radius1=.055, radius2=0, depth=1,
                                    end_fill_type="NGON", location=(0, 0, 0))
    cone = bpy.context.object
    cone.name = "Single shared cone source"
    for vertex in cone.data.vertices:
        vertex.co.z += .5
    cone.hide_render = True
    cone.hide_set(True)
    mat = material("Confidence — amber to red", (.8, .2, .04), .7)
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    attr = nodes.new("ShaderNodeAttribute")
    attr.attribute_name = "confidence"
    attr.attribute_type = "INSTANCER"
    ramp = nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = (*linear_hex(mapping["low_confidence_color"]), 1)
    ramp.color_ramp.elements[1].color = (*linear_hex(mapping["high_confidence_color"]), 1)
    links.new(attr.outputs["Fac"], ramp.inputs["Fac"])
    shader = nodes.get("Principled BSDF")
    links.new(ramp.outputs["Color"], shader.inputs["Base Color"])
    links.new(ramp.outputs["Color"], shader.inputs["Emission Color"])
    cone.data.materials.append(mat)

    mesh = bpy.data.meshes.new("Marker locations and attributes")
    mesh.from_pydata([(m["x"], m["y"], m["z"]) for m in markers], [], [])
    for name in ("score", "confidence"):
        attribute = mesh.attributes.new(name, "FLOAT", "POINT")
        attribute.data.foreach_set("value", [m[name] for m in markers])
    selected = mesh.attributes.new("selected", "BOOLEAN", "POINT")
    selected.data.foreach_set("value", [True] * len(markers))
    obj = bpy.data.objects.new("Instanced hazard markers", mesh)
    bpy.context.collection.objects.link(obj)
    group = bpy.data.node_groups.new("Single-cone instancing", "GeometryNodeTree")
    group.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    group.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    nodes, links = group.nodes, group.links
    inp, out = nodes.new("NodeGroupInput"), nodes.new("NodeGroupOutput")
    instance = nodes.new("GeometryNodeInstanceOnPoints")
    source = nodes.new("GeometryNodeObjectInfo")
    source.inputs["Object"].default_value = cone
    source.inputs["As Instance"].default_value = True
    source.transform_space = "ORIGINAL"
    active = nodes.new("GeometryNodeInputNamedAttribute")
    active.data_type = "BOOLEAN"
    active.inputs["Name"].default_value = "selected"
    score = nodes.new("GeometryNodeInputNamedAttribute")
    score.data_type = "FLOAT"
    score.inputs["Name"].default_value = "score"
    height = nodes.new("ShaderNodeMapRange")
    height.inputs["From Min"].default_value = 0
    height.inputs["From Max"].default_value = 1
    height.inputs["To Min"].default_value = mapping["min_marker_scale"]
    height.inputs["To Max"].default_value = mapping["max_marker_scale"]
    scale = nodes.new("ShaderNodeCombineXYZ")
    scale.inputs["X"].default_value = 1
    scale.inputs["Y"].default_value = 1
    links.new(score.outputs["Attribute"], height.inputs["Value"])
    links.new(height.outputs["Result"], scale.inputs["Z"])
    links.new(inp.outputs["Geometry"], instance.inputs["Points"])
    links.new(source.outputs["Geometry"], instance.inputs["Instance"])
    links.new(active.outputs["Attribute"], instance.inputs["Selection"])
    links.new(scale.outputs["Vector"], instance.inputs["Scale"])
    links.new(instance.outputs["Instances"], out.inputs["Geometry"])
    obj.modifiers.new("Instance one cone on selected points", "NODES").node_group = group
    return obj


def camera_and_lights(meta, max_height):
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 32
    scene.cycles.use_denoising = True
    scene.cycles.seed = 42
    scene.render.resolution_x, scene.render.resolution_y = 1920, 1080
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.view_settings.view_transform = "AgX"
    scene.world.use_nodes = True
    scene.world.node_tree.nodes["Background"].inputs["Color"].default_value = (.12, .15, .19, 1)
    scene.world.node_tree.nodes["Background"].inputs["Strength"].default_value = .55
    bpy.ops.object.light_add(type="SUN", location=(-12, -8, 15))
    sun = bpy.context.object
    sun.name = "Low-angle sun"
    sun.data.energy = 2.4
    sun.data.angle = math.radians(12)
    sun.rotation_euler = Vector((.8, .3, -.45)).to_track_quat('-Z', 'Y').to_euler()
    bpy.ops.object.camera_add(location=(27, -35, 31))
    camera = bpy.context.object
    camera.name = "Fixed three-quarter camera"
    target = Vector((0, 0, max_height / 2))
    camera.rotation_euler = (target - camera.location).to_track_quat('-Z', 'Y').to_euler()
    camera.data.type = "ORTHO"
    scene.camera = camera
    bpy.context.view_layer.update()
    inverse = camera.matrix_world.inverted()
    half = meta["scene_extent"] / 2
    corners = [inverse @ Vector((x, y, z)) for x in (-half, half) for y in (-half, half)
               for z in (0, max_height + meta["render"]["max_marker_scale"])]
    width = max(v.x for v in corners) - min(v.x for v in corners)
    height = max(v.y for v in corners) - min(v.y for v in corners)
    camera.data.ortho_scale = max(width / .88, height * (1920 / 1080) / .78)
    return camera


def label(camera, text, x, y, size, name):
    curve = bpy.data.curves.new(name, "FONT")
    curve.body = text
    curve.size = size
    curve.space_character = 1.08
    obj = bpy.data.objects.new(name, curve)
    bpy.context.collection.objects.link(obj)
    obj.parent = camera
    obj.location = (x, y, -5)
    obj.data.materials.append(material(name + " ink", (.8, .84, .86), 1))
    return curve


def main():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    meta = json.loads((ROOT / "data/processed/terrain_meta.json").read_text())
    markers = json.loads((ROOT / "data/processed/markers.json").read_text())
    assert len(markers) == meta["marker_count"] > 0
    max_height = terrain(meta)
    points = cones(markers, meta["render"])
    camera = camera_and_lights(meta, max_height)
    width = camera.data.ortho_scale
    height = width * 1080 / 1920
    label(camera, "COBALT  /  ABANDONED MINE HAZARDS", -width*.45, height*.425, width*.015, "Heading")
    subtitle = label(camera, "", -width*.45, height*.375, width*.009, "Selection")
    label(camera, "HEIGHT: SEVERITY    |    COLOUR: CONFIDENCE  (AMBER LOW / RED HIGH)",
          -width*.45, -height*.40, width*.008, "Legend")
    label(camera, "FLAT WEDGE = NO ELEVATION DATA (QUEBEC BORDER)",
          -width*.45, -height*.44, width*.008, "Nodata footer")
    label(camera, f"TERRAIN EXAGGERATION {meta['vertical_exaggeration']:g}x  |  NORTH = +Y",
          -width*.45, -height*.475, width*.007, "Terrain note")
    variants = [
        ("01_full_scene.png", "All features", [True] * len(markers)),
        ("02_high_severity.png", "Severity > 0.7", [m["score"] > .7 for m in markers]),
        ("03_high_severity_low_confidence.png", "Severity > 0.7 / confidence < 0.3",
         [m["score"] > .7 and m["confidence"] < .3 for m in markers]),
    ]
    output = ROOT / "renders"
    output.mkdir(exist_ok=True)
    for filename, title, selected in variants:
        count = sum(selected)
        assert count > 0
        points.data.attributes["selected"].data.foreach_set("value", selected)
        points.data.update()
        bpy.context.view_layer.update()
        subtitle.body = f"{title}   /   {count:,} markers"
        bpy.context.scene.render.filepath = str(output / filename)
        print(f"Rendering {filename}: {count} cone instances; {len(bpy.data.objects)} total objects", flush=True)
        bpy.ops.render.render(write_still=True)
    print("Phase 2 complete: three stills rendered. No subsequent phase executed.", flush=True)


if __name__ == "__main__":
    main()
