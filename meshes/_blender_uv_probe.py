"""
Inspect what UV channel each original glTF material uses by looking
at the imported nodes. The glTF importer wires each TexImage node's
Vector input to a UVMap node pointing at TEXCOORD_<n>.

Run via:
    blender --background --factory-startup --python _blender_uv_probe.py -- <glb_path>
"""

import bpy
import sys


def main():
    if "--" not in sys.argv:
        print("ERR: missing -- separator")
        sys.exit(1)
    args = sys.argv[sys.argv.index("--") + 1:]
    if not args:
        print("Usage: ... -- <glb_path>")
        sys.exit(1)
    glb = args[0]

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=glb)

    for mat in bpy.data.materials:
        if mat.name in ("Dots Stroke",):
            continue
        if not mat.use_nodes:
            continue
        print(f"\n=== Material: {mat.name} ===")
        for node in mat.node_tree.nodes:
            if node.type != "TEX_IMAGE":
                continue
            img_name = node.image.name if node.image else "<none>"
            uv_node = None
            for link in mat.node_tree.links:
                if link.to_node is node and link.to_socket.name == "Vector":
                    uv_node = link.from_node
                    break
            if uv_node and uv_node.type == "UVMAP":
                print(f"  {node.name} = {img_name} -> {uv_node.uv_map}")
            elif uv_node is not None:
                print(f"  {node.name} = {img_name} (Vector connected to {uv_node.type})")
            else:
                print(f"  {node.name} = {img_name} (UV not explicit, uses first)")


if __name__ == "__main__":
    main()
