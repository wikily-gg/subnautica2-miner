"""Quick test: render Cerathecan with UV.x scaled to test if the
2x1 atlas is being sampled correctly.

Theory 1: UV.x in [0,2] with image=600x300 and Repeat mode tiles the
whole image twice horizontally.

Theory 2: The artist authored UV.x in [0,2] expecting a 1200x300 texture
(or equivalent atlas where x=0..2 maps to left panel + right panel).
CUE4Parse exported the texture at 600x300 (correctly its native res),
but our UV interpretation is wrong because we need to divide UV.x by 2.

Run via:
    blender --background --factory-startup --python _uv_test.py -- <glb> <out_png> <slug>
"""

import bpy
import math
import mathutils
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _blender_render as br


def attach_pbr_uvscaled(meshes, glb_path, slug, ux_scale=1.0, uy_scale=1.0):
    """Like br.attach_pbr_materials but wires every texture through a
    Mapping node that scales UV by (ux_scale, uy_scale)."""
    root = br._texture_search_root(glb_path)
    tex_index = br._find_textures(root)
    if not tex_index:
        return
    stem_tokens = {stem: br._normalize_tokens(stem) for stem in tex_index}
    slug_toks = br._normalize_tokens(slug)

    seen = set()
    for o in meshes:
        for slot in o.material_slots:
            mat = slot.material
            if mat is None:
                continue
            key = mat.name.lower()
            if key in seen:
                continue
            seen.add(key)
            mat_toks = br._normalize_tokens(mat.name)
            is_engine_default = mat.name.lower() in br._ENGINE_DEFAULT_NAMES

            best = None
            best_score = 0
            for stem, paths in tex_index.items():
                if not any(k in paths for k in ("basecolor", "basecolor_metallic")):
                    continue
                score = br._score_match(mat_toks, stem_tokens[stem])
                score += br._tag_bonus(stem_tokens[stem], mat_toks)
                if score > best_score:
                    best_score = score
                    best = (stem, paths)
            if (best is None or best_score == 0) and is_engine_default:
                best = br._pick_body_fallback(tex_index, stem_tokens, slug_toks)
            if best is None:
                continue
            stem, paths = best
            new_mat = br._build_pbr_material(mat.name + "_PBR", paths)
            # Inject Mapping nodes between every TexImage and its consumers
            if ux_scale != 1.0 or uy_scale != 1.0:
                nt = new_mat.node_tree
                tex_input = nt.nodes.new("ShaderNodeTexCoord")
                tex_input.location = (-1300, 200)
                mapping = nt.nodes.new("ShaderNodeMapping")
                mapping.location = (-1100, 200)
                mapping.inputs["Scale"].default_value[0] = ux_scale
                mapping.inputs["Scale"].default_value[1] = uy_scale
                nt.links.new(tex_input.outputs["UV"], mapping.inputs["Vector"])
                for node in list(nt.nodes):
                    if node.type == "TEX_IMAGE":
                        nt.links.new(mapping.outputs["Vector"], node.inputs["Vector"])
            slot.material = new_mat


def main():
    args = sys.argv[sys.argv.index("--") + 1:]
    glb = args[0]
    out_png = args[1]
    slug = args[2]
    ux = float(args[3])
    uy = float(args[4])

    br.clear_scene()
    meshes = br.import_glb(glb)
    attach_pbr_uvscaled(meshes, glb, slug, ux_scale=ux, uy_scale=uy)
    center, extent, size = br.compute_bounds(meshes)
    cam = br.setup_camera(center, size, extent, "creature")
    br.setup_lights(center, size, cam.location)
    br.configure_view_settings()
    br.configure_render()
    bpy.context.scene.render.filepath = out_png
    bpy.ops.render.render(write_still=True)
    print(f"DONE: {out_png}")


if __name__ == "__main__":
    main()
