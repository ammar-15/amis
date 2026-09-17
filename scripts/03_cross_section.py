"""Standalone illustrative shaft section. Reads no data or configuration files.

Run in Blender's bundled Python:
Blender --background --factory-startup --python scripts/03_cross_section.py
"""

from pathlib import Path
import math

import bpy
from mathutils import Vector


ROOT = Path(__file__).resolve().parents[1]
# Illustrative dimensions only: one scene unit represents one metre.
SHAFT_DEPTH = 55.0
SHAFT_LEFT, SHAFT_RIGHT = -27.0, -23.0
UNKNOWN_LEFT, UNKNOWN_RIGHT = 8.0, 12.0


def flat_material(name, color, strength=1, alpha=1):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    nodes.clear()
    emission = nodes.new("ShaderNodeEmission")
    emission.inputs["Color"].default_value = (*color, 1)
    emission.inputs["Strength"].default_value = strength
    out = nodes.new("ShaderNodeOutputMaterial")
    if alpha < 1:
        transparent = nodes.new("ShaderNodeBsdfTransparent")
        mix = nodes.new("ShaderNodeMixShader")
        mix.inputs[0].default_value = alpha
        mat.node_tree.links.new(transparent.outputs[0], mix.inputs[1])
        mat.node_tree.links.new(emission.outputs[0], mix.inputs[2])
        mat.node_tree.links.new(mix.outputs[0], out.inputs["Surface"])
    else:
        mat.node_tree.links.new(emission.outputs[0], out.inputs["Surface"])
    return mat


def box(name, xmin, xmax, ymin, ymax, zmin, zmax, material):
    bpy.ops.mesh.primitive_cube_add(size=1, location=((xmin+xmax)/2, (ymin+ymax)/2, (zmin+zmax)/2))
    obj = bpy.context.object
    obj.name = name
    obj.dimensions = (xmax-xmin, ymax-ymin, zmax-zmin)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    obj.data.materials.append(material)
    return obj


def stroke(name, points, mat, radius=.045):
    curve = bpy.data.curves.new(name, "CURVE")
    curve.dimensions = "3D"
    curve.bevel_depth = radius
    curve.bevel_resolution = 0
    spline = curve.splines.new("POLY")
    spline.points.add(len(points)-1)
    for vertex, point in zip(spline.points, points):
        vertex.co = (*point, 1)
    obj = bpy.data.objects.new(name, curve)
    bpy.context.collection.objects.link(obj)
    obj.data.materials.append(mat)


def text(name, body, x, z, size, mat, align="LEFT"):
    curve = bpy.data.curves.new(name, "FONT")
    curve.body = body
    curve.size = size
    curve.space_character = 1.08
    curve.align_x = align
    obj = bpy.data.objects.new(name, curve)
    bpy.context.collection.objects.link(obj)
    obj.location = (x, -1, z)
    obj.rotation_euler = (math.pi/2, 0, 0)
    obj.data.materials.append(mat)


def main():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
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

    ink = flat_material("Same pale title ink", (.8, .84, .86))
    muted = flat_material("Muted annotations", (.43, .49, .52))
    # Linear RGB corresponding to the amber #E8B33A used in the main renders.
    amber = flat_material("Amber shaft void", (.807, .451, .042), .85)
    hatch = flat_material("Rock section hatch", (.16, .18, .18), .8)
    rock = bpy.data.materials.new("Grey matte rock")
    rock.use_nodes = True
    nodes, links = rock.node_tree.nodes, rock.node_tree.links
    bsdf = nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = (.27, .29, .28, 1)
    bsdf.inputs["Roughness"].default_value = .95
    noise = nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = .65
    noise.inputs["Detail"].default_value = 2
    coords = nodes.new("ShaderNodeTexCoord")
    links.new(coords.outputs["Object"], noise.inputs["Vector"])
    bump = nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = .12
    bump.inputs["Distance"].default_value = .15
    links.new(noise.outputs["Fac"], bump.inputs["Height"])
    links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])

    # Equal-width surface openings; only the left shaft has a depicted endpoint.
    box("Rock left of shaft", -43, SHAFT_LEFT, 0, 8, -63, 0, rock)
    box("Rock between shafts", SHAFT_RIGHT, UNKNOWN_LEFT, 0, 8, -63, 0, rock)
    box("Rock right of shafts", UNKNOWN_RIGHT, 26, 0, 8, -63, 0, rock)
    box("Rock below shaft", SHAFT_LEFT, SHAFT_RIGHT, 0, 8, -63, -SHAFT_DEPTH, rock)
    box("Rock behind unknown depth", UNKNOWN_LEFT, UNKNOWN_RIGHT, 0, 8, -63, -8, rock)
    void_fill = flat_material("Amber void fill", (.807, .451, .042), .55)
    box("Recorded shaft void", SHAFT_LEFT, SHAFT_RIGHT, .15, .2, -SHAFT_DEPTH, 0, void_fill)
    box("Unrecorded shaft opening", UNKNOWN_LEFT, UNKNOWN_RIGHT, .15, .2, -8, 0, void_fill)
    stroke("Recorded solid outline", [(SHAFT_LEFT, -.2, 0), (SHAFT_LEFT, -.2, -55),
           (SHAFT_RIGHT, -.2, -55), (SHAFT_RIGHT, -.2, 0)], amber, .09)
    for x in (UNKNOWN_LEFT, UNKNOWN_RIGHT):
        stroke(f"Unrecorded solid upper wall {x}", [(x, -.2, 0), (x, -.2, -8)], amber, .09)
    # Fade is a drawing convention, not an estimated depth or a closed shaft end.
    # Translucent dashes let the actual rock shading show through as they vanish.
    for index in range(10):
        depth = 8 + index * 3
        alpha = max(0, 1 - index / 10) ** 1.7
        dash_mat = flat_material(f"Unknown outline fade {index}", (.807, .451, .042), .85, alpha)
        for x in (UNKNOWN_LEFT, UNKNOWN_RIGHT):
            stroke(f"Unknown wall dash {index} {x}", [(x, -.22, -depth), (x, -.22, -depth-1.8)],
                   dash_mat, .09)
    for index in range(56):
        depth = 8 + index * .5
        alpha = .3 * (1 - index / 56) ** 2
        wash = flat_material(f"Unknown void fade {index}", (.807, .451, .042), .55, alpha)
        box(f"Unknown depth fading wash {index}", UNKNOWN_LEFT+.1, UNKNOWN_RIGHT-.1,
            -.12, -.1, -depth-.5, -depth, wash)
    # Sparse drafting hatch denotes cut rock, not surveyed geological layers.
    for i in range(15):
        z = -4-i*4
        for x in (-38, -16, -5, 18):
            stroke(f"Rock hatch {i} {x}", [(x, -.07, z), (x+2.5, -.07, z+1.4)], hatch, .028)
    stroke("Surface left", [(-45, -.1, 0), (SHAFT_LEFT, -.1, 0)], ink, .08)
    stroke("Surface middle", [(SHAFT_RIGHT, -.1, 0), (UNKNOWN_LEFT, -.1, 0)], ink, .08)
    stroke("Surface right", [(UNKNOWN_RIGHT, -.1, 0), (28, -.1, 0)], ink, .08)
    text("Recorded label", "DEPTH RECORDED: 55 m", -39, 1.9, 1.25, ink)
    text("Unrecorded label", "DEPTH NOT RECORDED", -1, 1.9, 1.25, ink)

    # Fixed depth axis: 0 is the surface, positive depth runs downwards.
    stroke("Depth axis", [(33, -.1, 0), (33, -.1, -60)], muted)
    text("Depth scale heading", "DEPTH (m)", 32, 2.5, 1.15, ink)
    for depth in range(0, 61, 5):
        major = depth % 10 == 0
        stroke(f"Depth tick {depth}", [(33, -.1, -depth), (34.4 if major else 33.8, -.1, -depth)], ink)
        if major:
            text(f"Depth {depth} m", str(depth), 36, -depth-.5, 1.4, ink)
    text("Unknown note", "DEPTH UNKNOWN", -2, -41, 1.05, muted)
    text("Rock label", "ROCK / CUT FACE", -4, -49, 1.05, muted)
    text("Shaft depth label", "55 m", -19, -55, 1.25, ink)
    stroke("Bottom leader", [(-20, -.2, -55.6), (SHAFT_RIGHT, -.2, -55.6),
                              (SHAFT_RIGHT, -.2, -SHAFT_DEPTH)], amber)

    text("Header", "MINE SHAFT - TYPICAL SECTION", -67.5, 9, 2.25, ink)
    text("Subtitle", "ILLUSTRATIVE VERTICAL SECTION / ORTHOGRAPHIC VIEW", -67.5, 5.7, 1.1, ink)
    text("Mandatory disclaimer", "SCHEMATIC. NOT SURVEY DATA.", -67.5, -66.3, 1.25, ink)
    # User-confirmed count from the reviewed Phase 1 window.
    text("Depth completeness footer", "621 OF 1,604 FEATURES IN THIS WINDOW HAVE NO USABLE DEPTH MEASUREMENT",
         -67.5, -69.1, 1.0, muted)

    bpy.ops.object.light_add(type="AREA", location=(-20, -35, 20))
    light = bpy.context.object
    light.name = "Soft front illumination"
    light.data.energy = 18000
    light.data.shape = "DISK"
    light.data.size = 45
    light.rotation_euler = (Vector((0, 0, -25))-light.location).to_track_quat('-Z', 'Y').to_euler()
    bpy.ops.object.camera_add(location=(0, -130, -28))
    camera = bpy.context.object
    camera.name = "Straight-on section camera"
    camera.rotation_euler = (math.pi/2, 0, 0)
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = 150
    camera.data.clip_end = 300
    scene.camera = camera
    output = ROOT / "renders/04_cross_section.png"
    output.parent.mkdir(exist_ok=True)
    scene.render.filepath = str(output)
    print("Standalone two-shaft schematic: recorded 55 m vs unknown depth; no data files read.", flush=True)
    bpy.ops.render.render(write_still=True)
    print(f"Phase 3 complete: {output}", flush=True)


if __name__ == "__main__":
    main()
