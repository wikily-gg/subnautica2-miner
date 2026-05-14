"""
Standalone helper: render a single glb at 4 cardinal camera azimuths
(0/90/180/270) so we can pick which orientation shows the creature's
front face.

Run via:
    blender --background --factory-startup --python _render_angles.py \\
      -- <glb_path> <out_dir> <slug>
"""

import bpy
import math
import mathutils
import os
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _blender_render as br  # noqa: E402


def main():
    if "--" not in sys.argv:
        sys.exit(1)
    args = sys.argv[sys.argv.index("--") + 1:]
    glb, out_dir, slug = args[0], args[1], args[2]

    os.makedirs(out_dir, exist_ok=True)

    for az_deg in (0, 45, 90, 135, 180, 225, 270, 315):
        br.clear_scene()
        meshes = br.import_glb(glb)
        if not meshes:
            print(f"ERR: az={az_deg} no meshes")
            continue
        br.attach_pbr_materials(meshes, glb, slug=slug)
        center, extent, size = br.compute_bounds(meshes)

        # Custom camera at this exact azimuth
        cam_data = bpy.data.cameras.new(name="C")
        cam_data.lens = 50
        cam = bpy.data.objects.new("C", cam_data)
        bpy.context.collection.objects.link(cam)
        longest = max(abs(extent.x), abs(extent.y), abs(extent.z), size)
        dist = longest * 1.85
        az = math.radians(az_deg)
        el = math.radians(12)
        cam.location = center + mathutils.Vector(
            (math.cos(az) * math.cos(el) * dist,
             math.sin(az) * math.cos(el) * dist,
             math.sin(el) * dist + size * 0.05),
        )
        direction = center - cam.location
        cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
        bpy.context.scene.camera = cam

        br.setup_lights(center, size, cam.location)
        br.configure_view_settings()
        br.configure_render()
        out_png = os.path.join(out_dir, f"{slug}_az{az_deg:03d}.png")
        bpy.context.scene.render.filepath = out_png
        bpy.ops.render.render(write_still=True)
        print(f"DONE: {out_png}")


if __name__ == "__main__":
    main()
