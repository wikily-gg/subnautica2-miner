"""
Blender headless render for SN2 *composite* meshes — full vehicles
assembled from many separate part GLBs in one scene.

Run via:
    blender --background --python _blender_composite.py -- \
        <out_png> <slug> <manifest.json>

The manifest is a JSON file produced by `meshes.composite` listing
every PLACEMENT in the vehicle's Blueprint Simple Construction Script:

    {
      "assembly_slug": "vehicle_tadpole",
      "placements": [
        {
          "component_name": "SM_Tadpole_UpgradeSlot_R_GEN_VARIABLE",
          "mesh_slug": "SM_Tadpole_UpgradeSlot",
          "glb_path": "...",
          "location": [0, 0, 0],   // UE cm, X-forward Y-right Z-up
          "rotation": [0, -90, 0], // UE deg, Pitch Yaw Roll
          "scale":    [-1, 1, 1],  // UE-style, negative = mirror
        },
        ...
      ]
    }

Each placement is imported as its own copy and positioned in the
scene per the BP transform. A single mesh can appear in multiple
placements (e.g. `SM_Tadpole_UpgradeSlot` placed twice to make left
and right slots from a single source GLB).

This script shares lighting / camera / material logic with
`_blender_render.py` but:

  - imports MULTIPLE GLB placements (one per BP component) instead of
    a single mesh
  - applies per-placement Loc / Rot / Scale from the manifest
  - skips the aux-mesh filter (we want every imported part)
  - computes the camera framing off the UNION bounding box
  - uses the vehicle archetype framing regardless of slug

Output:
    <out_png>  PNG with transparent background, 1024×1024
"""

from __future__ import annotations

import bpy
import math
import mathutils
import os
import sys


# Render settings — matched to _blender_render.py so composite + single
# renders look consistent in the wiki grid.
RES = 1024
SAMPLES = 64
BG_TRANSPARENT = True


# ---------------------------------------------------------------------------
# Scene setup
# ---------------------------------------------------------------------------


def clear_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


# Mesh slugs that should be rendered as clean transparent glass instead
# of having their (broken) PBR materials reconstructed. SN2's glass /
# canopy materials use unreal-specific shader graphs that CUE4Parse
# can't replicate, so by default they render as dark opaque blobs. We
# detect them by name and stamp a Principled BSDF with high transmission
# + low roughness + white tint, which reads correctly as glass dome.
_GLASS_TOKENS: tuple[str, ...] = (
    "glass",
    "skm_tadpole",          # cockpit canopy on the base Tadpole
    "canopy",
    "dome",
    "membrane",             # SM_Hatch_Membrane_Lifepod
)


# Per-mesh rotation correction. SN2's vehicle Blueprints apply a
# RelativeRotation to certain components at runtime — typically the
# skeletal-mesh canopies + glass meshes ship in one orientation and
# the BP rotates them -90 yaw to line up with the static-mesh body
# parts. Our composite imports every GLB at canonical pose, so without
# correction the skeletal canopy and glass end up rotated 90° from
# everything else. Values are (Pitch, Yaw, Roll) in DEGREES, mirroring
# the BP's FRotator convention.
#
# Source: dumped from each chassis BP_*.uasset's USCS_Node tree. We
# could read these dynamically per assembly, but the rotations are
# stable across builds so a slug-keyed table is enough.
_ROTATIONS_BY_SLUG: dict[str, tuple[float, float, float]] = {
    # Base Tadpole canopy (skeletal) + cockpit glass meshes
    "SKM_Tadpole": (0.0, -90.0, 0.0),
    "SM_Tadpole_Glass_Exterior": (0.0, -90.0, 0.0),
    "SM_Tadpole_Glass_Interior": (0.0, -90.0, 0.0),
    # HAUL skeletal hull
    "SKM_Tadpole_HAUL": (0.0, -90.0, 0.0),
    # ScoutRay skeletal hull (manta-pose body)
    "SK_Tadpole_Scout_Ray": (0.0, -90.0, 0.0),
}


def _rotation_for_part(part_slug: str) -> tuple[float, float, float] | None:
    """Return (Pitch, Yaw, Roll) in degrees for the given mesh slug,
    or None if no rotation correction applies. Match is case-sensitive
    on the canonical slug name (no `.glb` suffix).
    """
    return _ROTATIONS_BY_SLUG.get(part_slug)


def _apply_rotation(objs, pitch_deg: float, yaw_deg: float, roll_deg: float) -> None:
    """Rotate a group of mesh objects around the WORLD origin by the
    given (Pitch, Yaw, Roll) Euler angles, in degrees. Matches UE's
    FRotator convention:

        Pitch -> rotation around Y (nose up / down)
        Yaw   -> rotation around Z (turn left / right)
        Roll  -> rotation around X (tilt left / right)

    CUE4Parse exports GLBs with Y-up flipped to match glTF / Blender's
    Z-up coordinate system. UE's Yaw (around Z up) corresponds 1:1 to
    Blender's rotation around Z, but the handedness flip inverts the
    sign — so we negate Yaw on the way in.
    """
    import math
    # UE -> Blender sign conversion. UE is left-handed Z-up; Blender is
    # right-handed Z-up. The handedness flip inverts Yaw (rotation
    # around the up axis) but not Pitch / Roll.
    rx = math.radians(roll_deg)
    ry = math.radians(pitch_deg)
    rz = math.radians(-yaw_deg)
    eu = mathutils.Euler((rx, ry, rz), "XYZ")
    rot_mat = eu.to_matrix().to_4x4()
    for o in objs:
        o.matrix_world = rot_mat @ o.matrix_world


# Per-mesh base-colour tint. The SN2 Alterra materials sample colour
# from a CurveAtlas gradient inside the shader graph: each mesh's UV
# layout maps different parts to different points on the gradient
# (t=0.11-0.41 reads as deep orange, t=0.99+ reads as cream). CUE4Parse
# extracts the textures but not the shader graph, so the rebuild can
# not vary colour across UV regions - everything reads as the wear
# pattern (off-white).
#
# Workaround: for meshes where one UV region dominates the silhouette
# (e.g. the Lifepod inflatable float, which samples the orange end of
# the curve almost exclusively), stamp the corresponding colour on top
# of the rebuilt material. Match by full mesh slug (suffix-aware) so
# meshes that share a prefix don't accidentally get the wrong tint.
#
# Values are linear-space RGB triplets.
_TINT_BY_EXACT_SLUG: dict[str, tuple[float, float, float]] = {
    # ROSETTE V Lifepod's bottom inflatable float - the iconic orange
    # ring around the base of every Alterra lifepod. Tube + TubeCover
    # together make the ring; the main pod body keeps its wear-pattern
    # cream so the silhouette has the cream-on-orange contrast the
    # in-game model has.
    "sm_lifepod_tube": (1.0, 0.42, 0.08),
    "sm_lifepod_tubecover": (1.0, 0.42, 0.08),
    # Hatch membrane is the orange flexible seal around the door.
    "sm_hatch_membrane_lifepod": (1.0, 0.42, 0.08),
}


def _tint_for_part(part_slug: str) -> tuple[float, float, float] | None:
    """Return an RGB tint to multiply onto the rebuilt base colour,
    or None when no override applies. Matches by EXACT mesh slug so
    we don't bleed orange onto the main pod body.
    """
    return _TINT_BY_EXACT_SLUG.get(part_slug.lower())


def _apply_base_color_tint(obj, tint_rgb: tuple[float, float, float]) -> None:
    """Multiply the existing Principled BSDF Base Color by a flat RGB
    tint, so a washed-grey wear texture gets pushed towards the actual
    Alterra branding colour. Skips meshes that don't have a Principled
    BSDF in their material (e.g. glass override stamps a fresh tree).
    """
    for slot in obj.material_slots:
        mat = slot.material
        if not mat or not mat.use_nodes:
            continue
        nt = mat.node_tree
        bsdf = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if bsdf is None:
            continue
        base = bsdf.inputs.get("Base Color")
        if base is None:
            continue
        # Find what's currently feeding the Base Color socket.
        link = next((l for l in nt.links if l.to_socket == base), None)
        # Add a Mix RGB node that multiplies the rebuilt color by our tint.
        mix = nt.nodes.new("ShaderNodeMixRGB")
        mix.blend_type = "MULTIPLY"
        mix.inputs["Fac"].default_value = 1.0
        mix.inputs["Color2"].default_value = (*tint_rgb, 1.0)
        mix.location = (bsdf.location[0] - 220, bsdf.location[1])
        if link is not None:
            # Re-route: source -> Mix.Color1 -> Mix.out -> BSDF.Base Color
            nt.links.new(link.from_socket, mix.inputs["Color1"])
            nt.links.remove(link)
        else:
            # No upstream texture - feed the current default colour into
            # Color1 so the multiply still produces something usable.
            mix.inputs["Color1"].default_value = base.default_value
        nt.links.new(mix.outputs["Color"], base)


def _is_glass_mesh(slug_or_name: str) -> bool:
    """Return True if the mesh slug looks like a transparent glass /
    canopy part. We match conservatively (token-in-name) since SN2
    ships its glass meshes under a few different naming conventions.
    """
    s = slug_or_name.lower()
    return any(tok in s for tok in _GLASS_TOKENS)


def _apply_glass_material(obj):
    """Swap an object's materials for a clean transparent glass shader.

    Uses a Principled BSDF with `Transmission = 1.0`, `Roughness = 0.05`
    and `Base Color = white` so the canopy reads as clean glass over
    the cockpit body instead of the dark opaque blob the broken PBR
    rebuild produces.
    """
    mat = bpy.data.materials.new(name=f"GlassOverride_{obj.name}")
    mat.use_nodes = True
    nt = mat.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (-300, 0)
    out.location = (0, 0)
    # Principled BSDF socket indices vary by Blender version — set by
    # name to stay forward-compatible. `inputs.get(...)` returns None
    # when the socket doesn't exist on this Blender build.
    def _set(name, value):
        soc = bsdf.inputs.get(name)
        if soc is not None:
            soc.default_value = value
    _set("Base Color", (1.0, 1.0, 1.0, 1.0))
    _set("Roughness", 0.05)
    # Blender 4+ renamed `Transmission` → `Transmission Weight`. Try
    # both to stay portable.
    if bsdf.inputs.get("Transmission Weight") is not None:
        _set("Transmission Weight", 1.0)
    else:
        _set("Transmission", 1.0)
    _set("IOR", 1.45)
    _set("Alpha", 0.35)
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    mat.blend_method = "BLEND"
    if hasattr(mat, "shadow_method"):
        mat.shadow_method = "HASHED"
    # Replace every material slot on the object with this glass material.
    obj.data.materials.clear()
    obj.data.materials.append(mat)


def import_one_glb(path: str) -> list[bpy.types.Object]:
    """Import a single GLB and return all newly-added mesh objects.

    Unlike `_blender_render.py:import_glb` we do NOT filter aux meshes —
    composite assemblies need every part imported as-is.
    """
    before = set(o.name for o in bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=path)
    new_names = set(o.name for o in bpy.data.objects) - before

    keep: list[bpy.types.Object] = []
    for n in new_names:
        o = bpy.data.objects.get(n)
        if o is None or o.type != "MESH":
            continue
        # Drop tiny placeholder meshes (CUE4Parse sometimes emits an
        # empty mesh stub alongside the real geometry).
        n_verts = len(o.data.vertices) if o.data else 0
        dims = o.dimensions
        if n_verts < 4 and max(dims) < 0.01:
            print(f"  removing placeholder mesh: {o.name} ({n_verts} verts)")
            bpy.data.objects.remove(o, do_unlink=True)
            continue
        keep.append(o)
    return keep


# ---------------------------------------------------------------------------
# Materials — slim copy from _blender_render.py. Composite parts each
# ship their own Materials/ folder next to the GLB; we run the texture
# rebuild once per GLB.
# ---------------------------------------------------------------------------


# Re-export the texture helpers from _blender_render so we stay
# consistent. Use exec() rather than import because Blender's script
# directory is not on PYTHONPATH by default.
HERE = os.path.dirname(os.path.abspath(__file__))
_SINGLE_RENDER_PATH = os.path.join(HERE, "_blender_render.py")

_single_render_globals: dict = {}


def _load_single_render_module():
    """Load `_blender_render.py` so we can reuse its texture pipeline."""
    if _single_render_globals:
        return _single_render_globals
    with open(_SINGLE_RENDER_PATH, "r", encoding="utf-8") as f:
        code = f.read()
    g: dict = {"__name__": "_blender_render_helpers"}
    exec(compile(code, _SINGLE_RENDER_PATH, "exec"), g)
    _single_render_globals.update(g)
    return _single_render_globals


# ---------------------------------------------------------------------------
# Camera + lighting — composite vehicles always use the "vehicle"
# archetype framing. Pulled from _blender_render to stay consistent.
# ---------------------------------------------------------------------------


def compute_union_bounds(meshes: list[bpy.types.Object]):
    """Compute the world-space bounding box of every mesh in the scene.

    Returns (center, extent, size). `extent` is the half-size on each
    axis; `size` is the largest dimension (used for camera distance).
    """
    if not meshes:
        return mathutils.Vector((0, 0, 0)), mathutils.Vector((1, 1, 1)), 1.0

    bbox_min = mathutils.Vector((float("inf"), float("inf"), float("inf")))
    bbox_max = mathutils.Vector((float("-inf"), float("-inf"), float("-inf")))
    for o in meshes:
        if o.type != "MESH":
            continue
        for corner in o.bound_box:
            world_corner = o.matrix_world @ mathutils.Vector(corner)
            for i in range(3):
                if world_corner[i] < bbox_min[i]:
                    bbox_min[i] = world_corner[i]
                if world_corner[i] > bbox_max[i]:
                    bbox_max[i] = world_corner[i]
    center = (bbox_min + bbox_max) * 0.5
    extent = (bbox_max - bbox_min) * 0.5
    size = max(extent.x, extent.y, extent.z) * 2.0
    return center, extent, size


def setup_vehicle_camera(center, size: float, extent):
    """3/4-front camera framed around the assembled vehicle.

    Mirrors `_blender_render.setup_camera` with archetype='vehicle' but
    skips the per-archetype dispatch — composite renders are always
    vehicle-style.

    Backoff is keyed off the *diagonal* of the bounding box plus a
    generous safety margin so wide vehicles (HAUL) don't get clipped
    from the 3/4 view. Earlier `size * 2.2` was tuned for the more
    compact Tadpole base and clipped the HAUL's storage bay extensions.
    """
    fov_deg = 28.0
    # Use the full diagonal (sqrt(x^2 + y^2 + z^2) * 2) rather than the
    # max single-axis size — this matters for HAUL where the depth +
    # width combined exceed the single-axis size by ~40%. The 2.6
    # multiplier keeps comfortable padding around the chassis.
    diag = (extent.x ** 2 + extent.y ** 2 + extent.z ** 2) ** 0.5 * 2.0
    backoff = max(diag * 2.6, size * 2.4, 5.0)
    azim_deg = 35.0
    elev_deg = 18.0

    azim = math.radians(azim_deg)
    elev = math.radians(elev_deg)

    cam_data = bpy.data.cameras.new("Camera")
    cam_data.angle = math.radians(fov_deg)
    cam_obj = bpy.data.objects.new("Camera", cam_data)
    bpy.context.collection.objects.link(cam_obj)
    bpy.context.scene.camera = cam_obj

    # Place camera on a sphere around the bounding-box center.
    cam_obj.location = (
        center.x + backoff * math.cos(elev) * math.sin(azim),
        center.y - backoff * math.cos(elev) * math.cos(azim),
        center.z + backoff * math.sin(elev),
    )

    # Aim camera at the bbox center.
    direction = mathutils.Vector(center) - mathutils.Vector(cam_obj.location)
    rot_quat = direction.to_track_quat("-Z", "Y")
    cam_obj.rotation_euler = rot_quat.to_euler()

    return cam_obj


def setup_lights(center, size, cam_location):
    """Studio rig tuned to make the vehicle read as clean and new.

    Mid-range brightness that works across all 4 chassis colour
    palettes: Tadpole (light grey body), HAUL (olive-brown), ScoutRay
    (matte black wings), Lifepod (near-white shell). A 4× boost over
    the original creature lighting blew out the Lifepod's white
    materials; the original lighting made Tadpole / HAUL look rusty
    from heavy AO baked into the BCM textures. ~1.7× is the sweet spot
    where the white pod doesn't clip and the darker chassis still get
    a clean factory-fresh wash instead of looking grimy.
    """
    key_energy = max(1400.0, size * 150.0)
    fill_energy = key_energy * 0.55
    rim_energy = key_energy * 0.65

    def _place(name, type_, energy, color, spread, offset):
        d = bpy.data.lights.new(name, type=type_)
        d.energy = energy
        d.color = color
        if hasattr(d, "size"):
            d.size = spread
        obj = bpy.data.objects.new(name, d)
        bpy.context.collection.objects.link(obj)
        # Offset from camera, NOT origin — guarantees key light always
        # sits above-front-left of the subject regardless of bbox center.
        obj.location = (
            cam_location[0] + offset[0] * size,
            cam_location[1] + offset[1] * size,
            cam_location[2] + offset[2] * size,
        )
        # Aim at scene center.
        direction = mathutils.Vector(center) - mathutils.Vector(obj.location)
        rot_quat = direction.to_track_quat("-Z", "Y")
        obj.rotation_euler = rot_quat.to_euler()
        return obj

    _place("Key", "AREA", key_energy, (1.0, 0.99, 0.97), 2.4,
           offset=(0.8, -0.3, 1.2))
    _place("Fill", "AREA", fill_energy, (0.92, 0.96, 1.0), 2.0,
           offset=(-1.4, 0.4, -0.8))
    _place("Rim", "AREA", rim_energy, (0.98, 0.98, 1.0), 1.1,
           offset=(0.4, 1.2, 1.4))

    # Neutral environment fill - bright enough to keep dark vehicles
    # like ScoutRay / HAUL from reading as muddy, dim enough not to
    # blow out white-based vehicles like the Lifepod. Strength 0.55
    # is the floor that still helps; anything higher pushes the white
    # pod past 1.0 in linear space and the highlights clip.
    world = bpy.context.scene.world or bpy.data.worlds.new("World")
    bpy.context.scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (0.78, 0.82, 0.88, 1.0)
        bg.inputs[1].default_value = 0.55


def configure_view_settings():
    s = bpy.context.scene
    s.view_settings.view_transform = "Standard"
    s.view_settings.look = "None"
    s.view_settings.exposure = 0.0
    s.view_settings.gamma = 1.0


def configure_render():
    s = bpy.context.scene
    s.render.engine = "CYCLES"
    s.cycles.samples = SAMPLES
    s.cycles.use_denoising = True
    s.cycles.device = "GPU"
    prefs = bpy.context.preferences.addons.get("cycles")
    if prefs is not None:
        cprefs = prefs.preferences
        for dev_type in ("OPTIX", "CUDA", "ONEAPI", "HIP", "OPENCL"):
            try:
                cprefs.compute_device_type = dev_type
                break
            except TypeError:
                continue
        try:
            cprefs.get_devices()
            for d in cprefs.devices:
                d.use = True
        except Exception:
            pass

    s.render.resolution_x = RES
    s.render.resolution_y = RES
    s.render.resolution_percentage = 100
    s.render.film_transparent = BG_TRANSPARENT
    s.render.image_settings.file_format = "PNG"
    s.render.image_settings.color_mode = "RGBA"


def render_to(out_png: str):
    bpy.context.scene.render.filepath = out_png
    bpy.ops.render.render(write_still=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _apply_placement_transform(objs, location, rotation, scale):
    """Apply a full UE-style transform (Loc, Rot, Scale) to a group of
    imported meshes. All three are optional; None means default (no
    translation / no rotation / 1.0 scale).

    UE -> Blender axis conversion:
      UE.X (forward)  -> Blender.Y (forward)   sign flipped
      UE.Y (right)    -> Blender.X (right)
      UE.Z (up)       -> Blender.Z (up)

    UE rotations (Pitch=Y, Yaw=Z, Roll=X) all get a sign flip on Yaw
    only due to the left-handed → right-handed conversion.

    Empirically the GLBs produced by CUE4Parse for SN2 already arrive
    in the UE-native coord frame (X-forward), NOT pre-converted. So
    here we apply the UE transform directly: translate by (X, Y, Z)
    centimetres, rotate by (Pitch around Y, Yaw around Z, Roll around
    X), scale by (X, Y, Z) with the sign carried through. We then
    flip Y at the end to land everything in Blender's Y-forward frame
    for camera framing.
    """
    import math
    rot_mat = mathutils.Matrix.Identity(4)
    scale_mat = mathutils.Matrix.Identity(4)
    trans_mat = mathutils.Matrix.Identity(4)

    if rotation:
        pitch, yaw, roll = rotation
        # UE FRotator: Pitch around Y, Yaw around Z, Roll around X
        # Compose: Yaw ∘ Pitch ∘ Roll (UE composition order)
        eu = mathutils.Euler((
            math.radians(roll),
            math.radians(pitch),
            math.radians(-yaw),  # left-handed -> right-handed flip
        ), "XYZ")
        rot_mat = eu.to_matrix().to_4x4()

    if scale:
        sx, sy, sz = scale
        scale_mat = mathutils.Matrix.Diagonal((sx, sy, sz, 1.0))

    if location:
        x, y, z = location
        # UE uses centimetres; CUE4Parse exports glTF using metres for
        # vertex positions (the standard SN2 export). UE Y points
        # right (Blender X), UE X points forward (Blender -Y on the
        # right-handed flip). For our purposes we keep UE coords and
        # just apply numerically — the artist's pivot offsets will
        # land correctly because every component shares the same
        # frame. Convert cm to m: divide by 100.
        trans_mat = mathutils.Matrix.Translation((x / 100.0, y / 100.0, z / 100.0))

    transform = trans_mat @ rot_mat @ scale_mat
    for o in objs:
        o.matrix_world = transform @ o.matrix_world


def main():
    if "--" not in sys.argv:
        print("ERR: missing -- separator")
        sys.exit(1)
    args = sys.argv[sys.argv.index("--") + 1:]
    if len(args) < 3:
        print("Usage: blender ... -- <out_png> <slug> <manifest.json>")
        sys.exit(1)

    out_png = args[0]
    slug = args[1]
    manifest_path = args[2]

    import json as _json
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = _json.load(f)
    placements = manifest.get("placements", [])

    print(f"\n=== Composite render: {slug}")
    print(f"    placements ({len(placements)}):")
    for pl in placements:
        print(f"      • {pl['component_name']} ({pl['mesh_slug']})")
    print(f"    out: {out_png}")

    clear_scene()

    # Load the helper module for materials.
    helpers = _load_single_render_module()
    attach_pbr_materials = helpers.get("attach_pbr_materials")
    if attach_pbr_materials is None:
        print("ERR: attach_pbr_materials not found in _blender_render")
        sys.exit(2)

    all_meshes: list[bpy.types.Object] = []
    for pl in placements:
        glb = pl["glb_path"]
        if not os.path.exists(glb):
            print(f"  WARN: missing GLB {glb} — skipping")
            continue
        part_meshes = import_one_glb(glb)
        if not part_meshes:
            print(f"  WARN: GLB imported zero meshes: {glb}")
            continue
        comp_name = pl["component_name"]
        mesh_slug = pl["mesh_slug"]
        print(f"  + {comp_name} <- {mesh_slug}: {len(part_meshes)} meshes")
        try:
            attach_pbr_materials(part_meshes, glb, slug=mesh_slug)
        except Exception as e:
            print(f"  WARN: material rebuild failed for {mesh_slug}: {e}")

        # Glass / canopy override - applied BEFORE transform so the
        # glass material doesn't get split between placements.
        if _is_glass_mesh(mesh_slug):
            for m in part_meshes:
                _apply_glass_material(m)
            print(f"    (glass override applied: {mesh_slug})")
        else:
            tint = _tint_for_part(mesh_slug)
            if tint is not None:
                for m in part_meshes:
                    _apply_base_color_tint(m, tint)
                print(f"    (tint applied: {mesh_slug} -> {tint})")

        # Apply this PLACEMENT's BP transform - position the mesh in
        # the actor space defined by the chassis Blueprint.
        _apply_placement_transform(
            part_meshes,
            pl.get("location"),
            pl.get("rotation"),
            pl.get("scale"),
        )
        if pl.get("location") or pl.get("rotation") or pl.get("scale"):
            print(f"    (transform: loc={pl.get('location')} "
                  f"rot={pl.get('rotation')} scale={pl.get('scale')})")
        all_meshes.extend(part_meshes)

    if not all_meshes:
        print("ERR: no parts imported")
        sys.exit(3)

    print(f"Imported {len(all_meshes)} total mesh objects across {len(placements)} placements")

    center, extent, size = compute_union_bounds(all_meshes)
    print(f"Union bounds: center={tuple(round(c, 2) for c in center)}, "
          f"extent={tuple(round(e, 2) for e in extent)}, size={size:.2f}")

    cam = setup_vehicle_camera(center, size, extent)
    setup_lights(center, size, cam.location)
    configure_view_settings()
    configure_render()

    os.makedirs(os.path.dirname(out_png), exist_ok=True)
    render_to(out_png)
    print(f"DONE -> {out_png}")


if __name__ == "__main__":
    main()
