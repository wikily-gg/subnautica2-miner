"""
Render a creature's body BC texture onto a sphere primitive.

Used as a fallback for creatures whose static Nanite mesh CUE4Parse can't
decode (Veps in SN2). The texture itself is the only visual identity we
have access to; wrapping it on a sphere with our standard 3-light rig
produces a "creature blob" portrait that matches the visual style of
the proper mesh renders.

Run via:
    blender --background --factory-startup --python _blender_texture_blob.py \\
      -- <bc_texture_path> <out_png> <slug>
"""

import bpy
import math
import mathutils
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _blender_render as br


def main():
    if "--" not in sys.argv:
        sys.exit(1)
    args = sys.argv[sys.argv.index("--") + 1:]
    bc_path = args[0]
    out_png = args[1]
    slug = args[2] if len(args) > 2 else os.path.splitext(os.path.basename(out_png))[0]

    print(f"\n=== Rendering texture blob: {bc_path} -> {out_png} (slug={slug})")
    bpy.ops.wm.read_factory_settings(use_empty=True)

    # Create a UV-mapped sphere as the canvas
    bpy.ops.mesh.primitive_uv_sphere_add(radius=1.0, segments=64, ring_count=32)
    sphere = bpy.context.active_object
    sphere.name = f"Blob_{slug}"
    # Smooth shading
    for poly in sphere.data.polygons:
        poly.use_smooth = True

    # Build a PBR material using just the BC texture
    mat = bpy.data.materials.new(f"Blob_{slug}_Mat")
    mat.use_nodes = True
    try:
        mat.blend_method = "OPAQUE"
    except AttributeError:
        pass
    nt = mat.node_tree
    nt.nodes.clear()

    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (700, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (400, 0)
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.location = (-200, 200)
    img = bpy.data.images.load(bc_path, check_existing=True)
    img.colorspace_settings.name = "sRGB"
    try:
        img.alpha_mode = "CHANNEL_PACKED"
    except AttributeError:
        pass
    tex.image = img
    nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 0.55

    sphere.data.materials.append(mat)

    # Camera + lights via the shared render module
    center = sphere.location.copy()
    size = 2.0  # sphere radius * 2

    cam_data = bpy.data.cameras.new("ShotCam")
    cam_data.lens = 50
    cam = bpy.data.objects.new("ShotCam", cam_data)
    bpy.context.collection.objects.link(cam)
    az = math.radians(45)
    el = math.radians(15)
    dist = size * 2.5
    cam.location = center + mathutils.Vector(
        (math.cos(az) * math.cos(el) * dist,
         math.sin(az) * math.cos(el) * dist,
         math.sin(el) * dist),
    )
    direction = center - cam.location
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    bpy.context.scene.camera = cam

    br.setup_lights(center, size, cam.location)
    br.configure_view_settings()
    br.configure_render()

    bpy.context.scene.render.filepath = out_png
    bpy.ops.render.render(write_still=True)
    print(f"DONE -> {out_png}")


if __name__ == "__main__":
    main()
