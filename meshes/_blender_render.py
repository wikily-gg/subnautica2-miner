"""
Blender headless render script for Subnautica 2 meshes.

Run via:
    blender --background --python _blender_render.py -- <glb_path> <out_png> [<slug> [<archetype>]]

Reads a .glb (CUE4Parse-exported with PNG textures alongside), rebuilds
Principled BSDF materials from the BaseColor/Normal/ORM PNGs, frames a
3/4-front camera around the mesh, sets up a studio rig, and renders a
transparent-bg PNG at 1024x1024.

Archetypes only influence camera framing (no posing - SN2 creatures have
unique rigs the FFW pose presets can't target safely):
    "static"   - centered camera, modest backoff. Used for flora, resources, items.
    "vehicle"  - wider FOV + further backoff for elongated subs / mech.
    "creature" - slight upward tilt + side angle, treats body as elongated.

This script runs INSIDE Blender's Python. Keep imports limited to bpy /
mathutils / stdlib.

Ported from farfarwest-data-miner/meshes/_blender_render.py with posing
removed.
"""

import bpy
import math
import mathutils
import os
import sys


# Render settings
RES = 1024
SAMPLES = 64
BG_TRANSPARENT = True


# ---------------------------------------------------------------------------
# Scene + import
# ---------------------------------------------------------------------------


def clear_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def import_glb(path: str):
    """Import .glb. Returns the kept mesh objects, dropping glTF placeholders."""
    glb_stem = os.path.splitext(os.path.basename(path))[0].lower()

    before = set(o.name for o in bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=path)
    new_names = set(o.name for o in bpy.data.objects) - before

    keep = []
    for n in list(new_names):
        if n not in bpy.data.objects:
            continue
        o = bpy.data.objects[n]
        if o.type != "MESH":
            continue
        has_arm_mod = any(m.type == "ARMATURE" for m in o.modifiers)
        if not has_arm_mod:
            n_verts = len(o.data.vertices)
            dims = o.dimensions
            is_placeholder = n_verts < 100 and max(dims) < 3.0
            if is_placeholder:
                print(f"  removing placeholder mesh: {o.name} ({n_verts} verts)")
                bpy.data.objects.remove(o, do_unlink=True)
                continue
            keep.append(o)
            continue
        # Skinned mesh - drop sub-meshes whose name shares no token with the glb basename
        on = o.name.lower().replace(".", "_").split("_")
        sn = glb_stem.replace(".", "_").split("_")
        # `len(part) > 3` was too strict — meshes like `SK_EMT` (token `EMT`,
        # 3 chars) failed the gate and got hidden even though they ARE the
        # main mesh. Drop the length filter and just require a token match.
        if not any(part in on for part in sn):
            print(f"  hiding aux mesh: {o.name}")
            o.hide_render = True
            o.hide_viewport = True
            continue
        keep.append(o)
    # Safety net: if every imported mesh got hidden, keep them all visible.
    # Better to render an ambiguous group than ship a blank PNG.
    if not keep:
        print("  aux-mesh filter would empty the scene — restoring all meshes")
        for n in new_names:
            o = bpy.data.objects.get(n)
            if o and o.type == "MESH":
                o.hide_render = False
                o.hide_viewport = False
                keep.append(o)
    return keep


# ---------------------------------------------------------------------------
# PBR materials - rebuild Principled BSDF from the extracted PNG textures
# ---------------------------------------------------------------------------


def _find_textures(root_dir: str):
    """Scan root_dir for SN2-named textures and bucket them by stem.

    SN2 ships its own suffix convention (matched against UE asset names
    in the cooked package, not the CUE4Parse FFW-style suffixes):

        _BC    BaseColor
        _BCM   BaseColor + Metallic (M channel packed in alpha)
        _N     Normal
        _OAE   Occlusion / Ambient-Occlusion / Emissive  (R = AO, G = ?, B = Emissive)
        _ORM   Occlusion / Roughness / Metallic        (rare on creatures)
        _M     Metallic mask
        _R     Roughness
        _E     Emissive

    Returns a stem-keyed map:
        {stem.lower(): {"basecolor": path, "normal": path, ...}}
    """
    # SN2 channel-packed convention (confirmed from extracted samples):
    #   _BC    sRGB albedo
    #   _BCM   sRGB albedo + Metallic in alpha
    #   _NRS   Normal (RG) + Roughness (B) + Specular (A)
    #   _NRH   Normal (RG) + Roughness (B) + Height (A)
    #   _OAE   Occlusion (R) + AmbientOcclusion (G) + Emissive (B)
    #   _Mask  Eye / UV1 fragment masks  - SKIPPED
    SUFFIX_MAP = {
        # BaseColor variants
        "bc": "basecolor",
        "basecolor": "basecolor",
        "diffuse": "basecolor",         # SN2 tools use _diffuse for albedo
        "albedo": "basecolor",
        "bcm": "basecolor_metallic",   # alpha = cavity (often)
        "bce": "basecolor_metallic",   # alpha = emissive mask - same RGB sampling
        # Normal + packed channels
        "n": "normal",
        "nm": "normal",
        "normal": "normal",
        "nr": "normal_roughness_specular",   # SN2 short form, treat like NRS
        "nrs": "normal_roughness_specular",
        "nrh": "normal_roughness_height",
        "nrt": "normal_roughness_specular",  # Normal + Roughness + Translucency, same RG=normal
        "nrmsk": "normal_roughness_specular",
        # OAE family
        "oae": "occlusion_ambient_emissive",
        "aom": "occlusion_ambient_emissive",
        "caot": "occlusion_ambient_emissive",
        # Standalone PBR
        "orm": "orm",
        "occlusionroughnessmetallic": "orm",
        "r": "roughness",
        "roughness": "roughness",
        "m": "metallic",
        "metallic": "metallic",
        "e": "emissive",
        "emissive": "emissive",
        "ao": "ao",
        "spec": "roughness",            # SN2 specular tex maps roughly to roughness
        "specular": "roughness",
    }

    out = {}
    for cur_root, _dirs, files in os.walk(root_dir):
        for fn in files:
            if not fn.lower().endswith(".png"):
                continue
            # Strip extension first, then any trailing junk underscore.
            # SN2 ships some textures like `T_Jetocaris_01_Body_BC_.png` with
            # a trailing `_` (artist typo or export quirk). Without trimming
            # it the rpartition picks the wrong split point and the suffix
            # becomes "" / ".png".
            base = fn[:-4]
            while base.endswith("_"):
                base = base[:-1]
            stem, _, suffix = base.rpartition("_")
            suffix = suffix.lower()
            mapped = SUFFIX_MAP.get(suffix)
            if mapped is None:
                continue
            key = stem.lower()
            # First-write-wins: prefer textures with cleaner filenames so a
            # cleanly-named BC doesn't get overwritten by an Eye or Mask
            # variant in the same stem.
            slot = out.setdefault(key, {})
            slot.setdefault(mapped, os.path.join(cur_root, fn))
    return out


def _load_image(path: str, colorspace: str = "sRGB"):
    img = bpy.data.images.load(path, check_existing=True)
    img.colorspace_settings.name = colorspace
    # SN2 BCM textures store the cavity map (not opacity!) in alpha and
    # the cavity is often near-zero everywhere. Blender's default
    # alpha_mode treats PNG alpha as straight which premultiplies into
    # RGB on certain code paths, making the surface render pure black
    # for any BCM with alpha~0 (e.g. SK_Scanner). Setting alpha_mode to
    # CHANNEL_PACKED tells Blender "the alpha is data, not opacity" -
    # the RGB output stays full-color, alpha output stays available as
    # a separate signal if we want it.
    try:
        img.alpha_mode = "CHANNEL_PACKED"
    except AttributeError:
        pass
    return img


def _layer_uv_range(layer) -> tuple[float, float, float, float]:
    """Return (min_x, max_x, min_y, max_y) of a UV layer."""
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")
    for d in layer.data:
        x, y = d.uv.x, d.uv.y
        if x < min_x: min_x = x
        if x > max_x: max_x = x
        if y < min_y: min_y = y
        if y > max_y: max_y = y
    return (min_x, max_x, min_y, max_y)


def _pick_uv_layer(meshes) -> tuple[str | None, tuple[float, float, float, float]]:
    """Pick the first UV layer with non-trivial data.

    SN2 meshes ship with up to 8 UV slots. Usually 'UVMap' (the first)
    has the real coords, but on some creatures (e.g. ElusiveLeviathan)
    EVERY UV layer is degenerate (all loops at the same point) - the
    cooked mesh just lost its UV data somewhere in the bake. For those
    we return `None` so the material builder falls back to procedural
    coords (Generated bbox projection) - that gives at least some
    variation across the surface instead of a solid grey blob.

    Returns (uv_layer_name_or_None, (min_x, max_x, min_y, max_y)).
    """
    for o in meshes:
        if o.type != "MESH":
            continue
        for layer in o.data.uv_layers:
            r = _layer_uv_range(layer)
            span_x = r[1] - r[0]
            span_y = r[3] - r[2]
            if span_x > 0.01 or span_y > 0.01:
                return (layer.name, r)
    return (None, (0.0, 1.0, 0.0, 1.0))


def _compute_uv_transform(
    uv_range: tuple[float, float, float, float],
    tex_path: str,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Decide UV (scale, offset) for a texture based on how its image
    aspect ratio compares to the UV range ratio.

    SN2 mixes two conventions on the same UV layout:

      (a) ATLAS: texture is e.g. 4096x2048 (2:1), mesh UVs span x=[0,2]
          intentionally so each UV unit equals one "tile" of the atlas.
          We must scale UV.x by 0.5 so the whole atlas is sampled.

      (b) TILE: texture is e.g. 1024x1024 (1:1), mesh UVs also span
          x=[0,2] but the artist meant the small pattern to TILE across
          the surface. Blender's Repeat wrap mode does the right thing
          natively; scaling UVs by 0.5 here would sample only half the
          texture's content (wrong).

    Distinguish by checking the texture aspect ratio:
      - If image is square-ish (aspect <= 1.2) -> TILE (scale 1.0)
      - If image is N:1 wide -> ATLAS (scale 1/N)

    Vertical atlases (taller than wide) are theoretically possible but
    we haven't seen any in SN2 so far - same logic applies if found.
    """
    import os
    import struct

    min_x, max_x, min_y, max_y = uv_range

    # Read PNG dimensions from header (no PIL in Blender's bundled python).
    # PNG: 8-byte sig + IHDR chunk where width and height are big-endian
    # uint32 at offsets 16 and 20 in the file.
    w, h = 1, 1
    try:
        with open(tex_path, "rb") as f:
            head = f.read(24)
        if len(head) >= 24 and head[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", head[16:24])
    except Exception:
        pass

    aspect_w = w / h if h else 1.0  # >1 = wider than tall
    aspect_h = h / w if w else 1.0  # >1 = taller than wide

    def _bucket(v: float) -> float:
        if v <= 1.05: return 1.0
        if v <= 2.05: return 2.0
        if v <= 3.05: return 3.0
        return float(round(v))

    # Horizontal: only scale when texture aspect MATCHES the UV span
    # (within tolerance). Otherwise the UV spread is intentional tile.
    if max_x > 1.1 and aspect_w >= 1.4:
        atlas_x = _bucket(max_x)
        if abs(aspect_w - atlas_x) <= 0.3:
            sx = 1.0 / atlas_x
        else:
            sx = 1.0
    else:
        sx = 1.0

    if max_y > 1.1 and aspect_h >= 1.4:
        atlas_y = _bucket(max_y)
        if abs(aspect_h - atlas_y) <= 0.3:
            sy = 1.0 / atlas_y
        else:
            sy = 1.0
    else:
        sy = 1.0

    # If UV starts off-zero (e.g. Clamthulu x in [1, 3]), shift the
    # sampled region so it lands in [0, 1] post-scale.
    # Mapping node order: scale first, then offset on the scaled coords.
    # We want: (uv - min_x) * sx in [0, sx*span]. So Mapping inputs are
    # Scale = sx, Location = -min_x * sx.
    ox = -min_x * sx if min_x > 0.05 else 0.0
    oy = -min_y * sy if min_y > 0.05 else 0.0

    return ((sx, sy), (ox, oy))


def _build_pbr_material(
    name: str,
    tex,
    uv_scale: tuple[float, float] = (1.0, 1.0),
    uv_offset: tuple[float, float] = (0.0, 0.0),
    uv_layer: str | None = "UVMap",
):
    """Build a Principled BSDF from any combination of SN2 textures.

    SN2 channel-pack handling (confirmed by inspecting MI parameter names
    in extracted material instances - 'BC BaseColor|Cavity Map',
    'NRS Normal|Roughness|SSS', 'OAE Opacity|AO|Emissive'):

      - BC         RGB = base color (sRGB)
      - BCM        RGB = base color (sRGB), A = CAVITY MAP (not metallic).
                   We use RGB only; alpha is ignored. Wiring it to the
                   Metallic input made every BCM-textured creature render
                   chrome-white (Halfmoon body alpha avg = 255).
      - NRS / NRH  R,G = normal (Z reconstructed), B = roughness.
                   The 4th channel (S = SSS, H = height) is not exposed.
      - OAE        R = opacity / cavity, G = AO, B = emissive mask.
                   We ignore this texture entirely by default. On many
                   SN2 creatures the B channel is uniformly high (e.g.
                   Halfmoon B avg = 255) so feeding it into Emission
                   blows out the whole surface. Re-enable per-slug later
                   if we identify genuine bioluminescent materials.
      - ORM        Standalone Occlusion / Roughness / Metallic pack.
                   Only wired when no NRS/NRH is present.
    """
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    # Force opaque blend so low alpha in BCM textures (the SN2 convention
    # stores cavity-mask or other data in alpha that we don't want as
    # opacity) doesn't render the mesh as see-through black. The
    # Principled BSDF's Alpha input also stays at its default 1.0
    # because we never wire the texture's Alpha output to it.
    try:
        mat.blend_method = "OPAQUE"
    except AttributeError:
        pass
    try:
        mat.surface_render_method = "DITHERED"
    except AttributeError:
        pass
    nt = mat.node_tree
    N, L = nt.nodes, nt.links
    N.clear()

    out = N.new("ShaderNodeOutputMaterial");  out.location = (900, 0)
    bsdf = N.new("ShaderNodeBsdfPrincipled"); bsdf.location = (600, 0)
    L.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    # Explicit UV chain feeding every TexImage Vector input. When the
    # mesh has a valid UV layer we route UVMap -> Mapping -> TexImage.
    # When ALL UV layers are degenerate (e.g. ElusiveLeviathan's cooked
    # mesh ships with every UV slot collapsed to (0,1)), uv_layer is
    # None and we fall through to TexCoord.Generated - the bounding-box
    # procedural projection. That's not "correct" texturing but it at
    # least varies the sample point across the surface, which is what
    # the previous default-Blender-behavior rendered for those meshes.
    if uv_layer is None:
        src = N.new("ShaderNodeTexCoord"); src.location = (-1100, 600)
        src_socket = src.outputs["Generated"]
    else:
        src = N.new("ShaderNodeUVMap"); src.location = (-1100, 600)
        src.uv_map = uv_layer
        src_socket = src.outputs["UV"]

    mapping = N.new("ShaderNodeMapping"); mapping.location = (-900, 600)
    mapping.vector_type = "POINT"
    mapping.inputs["Location"].default_value[0] = uv_offset[0]
    mapping.inputs["Location"].default_value[1] = uv_offset[1]
    mapping.inputs["Location"].default_value[2] = 0.0
    mapping.inputs["Scale"].default_value[0] = uv_scale[0]
    mapping.inputs["Scale"].default_value[1] = uv_scale[1]
    mapping.inputs["Scale"].default_value[2] = 1.0
    L.new(src_socket, mapping.inputs["Vector"])

    def _new_tex(path, colorspace, loc):
        n = N.new("ShaderNodeTexImage")
        n.location = loc
        n.image = _load_image(path, colorspace)
        # REPEAT wrap is the SN2 artist-intended convention. When the
        # texture is a true atlas (e.g. Cerathecan 2:1), we pre-scale
        # UVs to land in [0,1] so the wrap mode never triggers. When
        # the texture is square and UVs span [0,2] (e.g. AnemoneCrab,
        # BlightParasite), the artist wants the texture tiled - REPEAT
        # gives that. EXTEND was wrong here because the "out-of-range"
        # half landed on the right edge pixel column = visible smear.
        n.extension = "REPEAT"
        L.new(mapping.outputs["Vector"], n.inputs["Vector"])
        return n

    # ---- Base color ----
    bc_path = tex.get("basecolor") or tex.get("basecolor_metallic")
    if bc_path:
        bcn = _new_tex(bc_path, "sRGB", (-600, 400))
        L.new(bcn.outputs["Color"], bsdf.inputs["Base Color"])
    else:
        bsdf.inputs["Base Color"].default_value = (0.7, 0.72, 0.74, 1.0)

    # ---- Normal + Roughness from packed NRS/NRH textures ----
    #
    # SN2 NRS layout (confirmed via inspecting Cerathecan body):
    #   R = Normal X    (DirectX-style: tangent-space X, -1..+1 mapped 0..255)
    #   G = Normal Y    (DirectX-style: Y points DOWN, opposite of OpenGL)
    #   B = Roughness
    #   A = SSS / specular (unused)
    #
    # UE cooks normal maps in DirectX convention. Blender's NormalMap
    # node expects OpenGL convention, so we MUST flip G (1 - G) before
    # feeding it in or surface lighting reads convex/concave inverted.
    nrx_path = tex.get("normal_roughness_specular") or tex.get("normal_roughness_height")
    if nrx_path:
        nrxn = _new_tex(nrx_path, "Non-Color", (-600, -100))
        sep = N.new("ShaderNodeSeparateColor"); sep.location = (-350, -100)
        L.new(nrxn.outputs["Color"], sep.inputs["Color"])

        # Flip G: invert = (1 - G). DirectX -> OpenGL normal map convention.
        ginv = N.new("ShaderNodeMath"); ginv.location = (-200, -50)
        ginv.operation = "SUBTRACT"
        ginv.inputs[0].default_value = 1.0
        L.new(sep.outputs["Green"], ginv.inputs[1])

        comb = N.new("ShaderNodeCombineColor"); comb.location = (0, 0)
        L.new(sep.outputs["Red"], comb.inputs["Red"])
        L.new(ginv.outputs["Value"], comb.inputs["Green"])
        comb.inputs["Blue"].default_value = 1.0
        nmap = N.new("ShaderNodeNormalMap"); nmap.location = (200, 0)
        nmap.inputs["Strength"].default_value = 1.0
        L.new(comb.outputs["Color"], nmap.inputs["Color"])
        L.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])

        L.new(sep.outputs["Blue"], bsdf.inputs["Roughness"])
    elif "normal" in tex:
        nimg = _new_tex(tex["normal"], "Non-Color", (-600, -100))
        nmap = N.new("ShaderNodeNormalMap"); nmap.location = (-200, -100)
        L.new(nimg.outputs["Color"], nmap.inputs["Color"])
        L.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])

    # ---- Standalone ORM (rare in SN2, but handle it for completeness) ----
    if not nrx_path and "orm" in tex:
        ormn = _new_tex(tex["orm"], "Non-Color", (-600, -400))
        sep = N.new("ShaderNodeSeparateColor"); sep.location = (-300, -400)
        L.new(ormn.outputs["Color"], sep.inputs["Color"])
        L.new(sep.outputs["Green"], bsdf.inputs["Roughness"])
        L.new(sep.outputs["Blue"], bsdf.inputs["Metallic"])

    # OAE is INTENTIONALLY not wired - see docstring.

    # ---- Standalone roughness / metallic / emissive (rare) ----
    if not nrx_path and "orm" not in tex and "roughness" in tex:
        n = _new_tex(tex["roughness"], "Non-Color", (-400, -400))
        L.new(n.outputs["Color"], bsdf.inputs["Roughness"])
    if "orm" not in tex and "metallic" in tex:
        n = _new_tex(tex["metallic"], "Non-Color", (-400, -550))
        L.new(n.outputs["Color"], bsdf.inputs["Metallic"])
    if "emissive" in tex:
        n = _new_tex(tex["emissive"], "sRGB", (-400, -850))
        L.new(n.outputs["Color"], bsdf.inputs["Emission Color"])
        bsdf.inputs["Emission Strength"].default_value = 0.5

    # Sensible defaults
    if not (nrx_path or "orm" in tex or "roughness" in tex):
        bsdf.inputs["Roughness"].default_value = 0.55
    return mat


_PREFIX_RE_PATTERNS = ("t_", "ti_", "mi_", "mm_", "m_", "skm_", "sk_", "sm_")


def _normalize_tokens(name: str) -> list[str]:
    """Lowercase, strip common prefixes, split on `_` / digits-vs-letters."""
    import re
    s = name.lower()
    for p in _PREFIX_RE_PATTERNS:
        if s.startswith(p):
            s = s[len(p):]
            break
    # Split on _, keep tokens with at least 3 chars (drop "01a", "B", etc.)
    raw = re.split(r"[_\.\s]+", s)
    out = []
    for tok in raw:
        if not tok:
            continue
        # Strip trailing letter variants from numeric tokens: "01a" -> "01"
        m = re.match(r"^(\d+)[a-z]+$", tok)
        if m:
            tok = m.group(1)
        out.append(tok)
    return [t for t in out if len(t) >= 3]


def _score_match(mat_tokens: list[str], stem_tokens: list[str]) -> int:
    """Higher = better. Counts shared meaningful tokens with length-weight,
    plus partial-substring matches.

    Examples:
      - exact: mat 'body' vs stem 'body' -> +4
      - substring: mat 'blightinfestation' vs stem 'blight' -> +6
        (the shorter token must be >=4 chars to avoid garbage matches
        like 'eye' inside 'eyestalk')
    """
    s = 0
    for mt in mat_tokens:
        for st in stem_tokens:
            if mt == st:
                s += len(mt)
            elif len(st) >= 4 and st in mt:
                # stem token contained in material token (e.g. "blight"
                # inside "blightinfestation")
                s += len(st) - 1
            elif len(mt) >= 4 and mt in st:
                # material token contained in stem token (rare but symmetric)
                s += len(mt) - 1
    return s


_PRIMARY_SURFACE_TAGS = ("body", "exterior", "main", "skin")
# Part tags - if the material name carries one of these, treat the
# material as "specific" so we DON'T apply a generic body-bonus that
# could outscore a correct part-match. E.g. MI_Cerathecan_01_Eye
# should match T_Cerathecan_01_Eye, not get pulled to body.
_PART_TAGS = (
    "eye", "eyes", "head", "horn", "tail", "tentacle", "tentacles",
    "claw", "claws", "fin", "fins", "wing", "wings", "tongue", "teeth",
    "shell", "scale", "root", "interior", "controls", "propellers",
    "haul", "glass", "cockpit",
)


def _tag_bonus(stem_tokens: list[str], mat_tokens: list[str]) -> int:
    """Tie-breaker bonus that only fires when it's *unambiguously* the
    right call:

    Awards +5 to a stem tagged body/exterior/main/skin ONLY if the
    material name itself also carries a body-style tag OR the material
    name carries NO part-specific tag at all. This stops the bonus from
    misrouting `MI_Cerathecan_01_Eye` to `T_Cerathecan_01_Body`.
    """
    stem_has_primary = any(t in _PRIMARY_SURFACE_TAGS for t in stem_tokens)
    if not stem_has_primary:
        return 0
    mat_has_primary = any(t in _PRIMARY_SURFACE_TAGS for t in mat_tokens)
    mat_has_part = any(t in _PART_TAGS for t in mat_tokens)
    if mat_has_primary or not mat_has_part:
        return 5
    return 0


def _texture_search_root(glb_path: str) -> str:
    """Find the slug root (`out/meshes/<slug>/`) so the texture walk
    sees both `Subnautica2/Content/.../Textures/` (CUE4Parse export
    tree) AND `_fallback_textures/` (sibling, populated by our texture
    fallback module).

    Algorithm: walk up from the .glb. The slug root is the directory
    whose PARENT is named `meshes` (regardless of case), since the
    pipeline writes everything to `out/meshes/<slug>/`.
    """
    cur = os.path.dirname(glb_path)
    while cur and os.path.dirname(cur) != cur:
        parent = os.path.dirname(cur)
        if os.path.basename(parent).lower() == "meshes":
            return cur
        cur = parent
    # Fallback: scan upward for a dir containing `_fallback_textures` or `Subnautica2`
    cur = os.path.dirname(glb_path)
    while cur and os.path.dirname(cur) != cur:
        if os.path.isdir(os.path.join(cur, "_fallback_textures")) or \
           os.path.isdir(os.path.join(cur, "Subnautica2")):
            return cur
        cur = os.path.dirname(cur)
    return os.path.dirname(glb_path)


_ENGINE_DEFAULT_NAMES = (
    "worldgridmaterial", "defaultmaterial", "default", "basicshapematerial",
)


def _pick_body_fallback(tex_index: dict, stem_tokens: dict, slug_toks: list[str]) -> tuple | None:
    """When a material name has zero overlap with any texture stem
    (e.g. WorldGridMaterial), pick whichever extracted texture is most
    likely the "main body" surface for this slug.

    Heuristic, in priority order:
      1. Stems matching slug name + 'body' / 'exterior'
         (e.g. SKM_Tadpole's WorldGridMaterial -> T_Tadpole_Exterior)
      2. Stems matching slug name alone
      3. Stems containing 'body' as a token
      4. First stem with a basecolor variant
    """
    def _has_bc(paths):
        return any(k in paths for k in ("basecolor", "basecolor_metallic"))

    PRIMARY_TAGS = ("body", "exterior", "main", "skin")

    # Score by (slug match, primary tag match)
    scored = []
    for stem, paths in tex_index.items():
        if not _has_bc(paths):
            continue
        toks = stem_tokens[stem]
        slug_match = sum(1 for t in slug_toks if t in toks)
        primary_match = sum(1 for t in PRIMARY_TAGS if t in toks)
        scored.append((slug_match * 100 + primary_match, stem, paths))
    if scored:
        scored.sort(key=lambda x: -x[0])
        if scored[0][0] > 0:
            return (scored[0][1], scored[0][2])

    # Nothing distinguishable - take any basecolor stem
    for stem, paths in tex_index.items():
        if _has_bc(paths):
            return (stem, paths)
    return None


def _apply_neutral_fallback_material(meshes, label: str = "neutral_fallback"):
    """Stamp a light-grey Principled BSDF onto every material slot.

    Used when texture rebuild can't find any usable PNGs (Alterra
    trimsheet meshes, base-piece props that ship without sibling
    textures). Without this the slots keep their default state which
    renders as solid black under our studio lighting - we'd rather
    show the silhouette as clean concept-art grey than a dark blob.

    Light grey base, mid roughness, no metallic. Identical to the
    style we apply for missing-icon vehicle parts.
    """
    mat = bpy.data.materials.new(name=label)
    mat.use_nodes = True
    nt = mat.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (-300, 0)
    out.location = (0, 0)

    def _set(name, value):
        soc = bsdf.inputs.get(name)
        if soc is not None:
            soc.default_value = value

    _set("Base Color", (0.78, 0.79, 0.82, 1.0))  # warm neutral grey
    _set("Roughness", 0.55)
    _set("Specular IOR Level", 0.5)
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    for o in meshes:
        if not o.material_slots:
            o.data.materials.append(mat)
            continue
        for slot in o.material_slots:
            slot.material = mat


def attach_pbr_materials(meshes, glb_path: str, slug: str = ""):
    """Walk up from the glb's directory looking for sibling texture PNGs and
    rebuild each material from them.

    Match strategy:
      1. Token-overlap score across material name and texture stems
         (e.g. `MI_Anemonecrab_01_Body` matches `T_Anemonecrab_01a_Body`).
      2. When the mesh has an engine-default material slot
         (`WorldGridMaterial`, etc) - score will be zero across the
         board. Fall back to a texture stem matching the SLUG name
         + a 'body'/'exterior' tag, so e.g. SKM_Tadpole's empty slot
         picks `T_Tadpole_Exterior` not `T_Alterra_BasePlastic_01`.

    When NO usable texture stems are found in the search root, every
    material slot gets a neutral fallback grey instead - prevents the
    Alterra trimsheet meshes from rendering as solid black.
    """
    root = _texture_search_root(glb_path)
    print(f"  texture-search root: {root}")
    tex_index = _find_textures(root)
    print(f"  found {len(tex_index)} texture stems")
    if not tex_index:
        print(f"  no textures - applying neutral grey fallback for {slug or 'mesh'}")
        _apply_neutral_fallback_material(meshes, label=f"{slug or 'mesh'}_neutral")
        return

    uv_layer, uv_range = _pick_uv_layer(meshes)
    if uv_layer is None:
        print("  No valid UV layer found - falling back to Generated coords (procedural projection)")
    else:
        print(f"  UV layer = {uv_layer!r}, range = x[{uv_range[0]:.2f}, {uv_range[1]:.2f}] y[{uv_range[2]:.2f}, {uv_range[3]:.2f}]")

    stem_tokens = {stem: _normalize_tokens(stem) for stem in tex_index}
    slug_toks = _normalize_tokens(slug) if slug else []

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
            mat_toks = _normalize_tokens(mat.name)
            is_engine_default = mat.name.lower() in _ENGINE_DEFAULT_NAMES

            best = None
            best_score = 0
            for stem, paths in tex_index.items():
                if not any(k in paths for k in ("basecolor", "basecolor_metallic")):
                    continue
                score = _score_match(mat_toks, stem_tokens[stem])
                score += _tag_bonus(stem_tokens[stem], mat_toks)
                if score > best_score:
                    best_score = score
                    best = (stem, paths)

            if best is None or best_score == 0:
                # Two fallback paths:
                # 1) Engine-default material (WorldGridMaterial etc.) - assume
                #    the artist meant the slug's main body texture
                # 2) ANY material with no token match BUT exactly one BC stem
                #    in the search root - the mesh has placeholder/blockout
                #    materials and only one texture exists (e.g. Glowstick
                #    uses MI_Blockout_Emissive_Fushia and ships with a single
                #    T_KF_PlantDisc texture as a stand-in)
                # 3) Final fallback: stamp a neutral grey Principled BSDF
                #    so the slot doesn't render as solid black. Better to
                #    show the mesh silhouette in clean concept-grey than
                #    a dark blob with no surface detail.
                bc_stems = [(s, p) for s, p in tex_index.items()
                            if any(k in p for k in ("basecolor", "basecolor_metallic"))]
                if is_engine_default:
                    best = _pick_body_fallback(tex_index, stem_tokens, slug_toks)
                    if best is not None:
                        print(f"  material {mat.name} -> {best[0]} (engine-default fallback)")
                elif len(bc_stems) == 1:
                    best = bc_stems[0]
                    print(f"  material {mat.name} -> {best[0]} (sole-texture fallback)")
                else:
                    print(f"  material {mat.name} -> no texture match (toks={mat_toks}) - neutral grey")
                    slot.material = bpy.data.materials.new(name=f"{mat.name}_neutral")
                    slot.material.use_nodes = True
                    _nt = slot.material.node_tree
                    for _n in list(_nt.nodes):
                        _nt.nodes.remove(_n)
                    _out = _nt.nodes.new("ShaderNodeOutputMaterial")
                    _bsdf = _nt.nodes.new("ShaderNodeBsdfPrincipled")
                    _bsdf.location = (-300, 0)
                    bc_input = _bsdf.inputs.get("Base Color")
                    if bc_input is not None:
                        bc_input.default_value = (0.78, 0.79, 0.82, 1.0)
                    rough = _bsdf.inputs.get("Roughness")
                    if rough is not None:
                        rough.default_value = 0.55
                    _nt.links.new(_bsdf.outputs["BSDF"], _out.inputs["Surface"])
                    continue
            else:
                stem = best[0]
                print(f"  material {mat.name} -> {stem} (score={best_score}, toks={mat_toks} vs {stem_tokens[stem]})")

            if best is None:
                continue
            stem, paths = best
            # Compute per-texture UV transform based on this texture's
            # aspect ratio. The body BC drives the decision; we apply
            # the same transform to all maps in this stem (normal, OAE
            # etc. share UVs by definition).
            bc_path = paths.get("basecolor") or paths.get("basecolor_metallic") or next(iter(paths.values()))
            uv_scale, uv_offset = _compute_uv_transform(uv_range, bc_path)
            print(f"    -> scale={uv_scale}, offset={uv_offset} (img={os.path.basename(bc_path)})")
            slot.material = _build_pbr_material(
                mat.name + "_PBR", paths,
                uv_scale=uv_scale,
                uv_offset=uv_offset,
                uv_layer=uv_layer,
            )


# ---------------------------------------------------------------------------
# Camera + lighting + render
# ---------------------------------------------------------------------------


def compute_bounds(meshes):
    pts = []
    for o in meshes:
        if o.hide_render:
            continue
        for v in o.bound_box:
            pts.append(o.matrix_world @ mathutils.Vector(v))
    if not pts:
        return mathutils.Vector((0, 0, 0)), mathutils.Vector((1, 1, 1)), 1.0
    minv = mathutils.Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    maxv = mathutils.Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    center = (minv + maxv) / 2
    size = (maxv - minv).length
    return center, maxv - minv, size


def setup_camera(
    target: mathutils.Vector,
    size: float,
    extent: mathutils.Vector,
    archetype: str,
    angle: str = "auto",
):
    """Place a camera framed on the mesh.

    *angle* selects the azimuth:
      "auto"  - bbox-derived 3/4 view (default)
      "front" - az 0deg (view down -Y onto +Y face)
      "side"  - az 90deg (view down -X onto +X face)
      "back"  - az 180deg
      "<N>"   - explicit azimuth degrees (e.g. "45")

    SN2 creature glbs don't share a canonical forward axis. The "auto"
    mode picks a 45deg offset from the SHORTER horizontal extent so the
    silhouette stays broadside.
    """
    cam_data = bpy.data.cameras.new(name="ShotCam")
    cam_data.lens = 50
    cam = bpy.data.objects.new("ShotCam", cam_data)
    bpy.context.collection.objects.link(cam)

    if archetype == "vehicle":
        dist_factor = 2.1
        el_deg = 14
    elif archetype == "creature":
        dist_factor = 1.85
        el_deg = 12
    else:  # static (flora / resources / items)
        dist_factor = 1.8
        el_deg = 14

    abs_x, abs_y = abs(extent.x), abs(extent.y)

    if angle == "auto":
        if abs_x >= abs_y:
            base_az = 90.0
        else:
            base_az = 0.0
        az_deg = base_az + 45.0
    elif angle == "front":
        az_deg = (90.0 if abs_x >= abs_y else 0.0) + 45.0
    elif angle == "side":
        az_deg = (90.0 if abs_x >= abs_y else 0.0) - 45.0
    elif angle == "back":
        az_deg = (90.0 if abs_x >= abs_y else 0.0) + 225.0
    else:
        try:
            az_deg = float(angle)
        except (TypeError, ValueError):
            az_deg = 45.0

    longest = max(abs_x, abs_y, abs(extent.z), size)

    az = math.radians(az_deg)
    el = math.radians(el_deg)
    dist = longest * dist_factor

    cam.location = target + mathutils.Vector(
        (math.cos(az) * math.cos(el) * dist,
         math.sin(az) * math.cos(el) * dist,
         math.sin(el) * dist + size * 0.05),
    )
    direction = target - cam.location
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()

    bpy.context.scene.camera = cam
    return cam


def setup_lights(target: mathutils.Vector, size: float, cam_location: mathutils.Vector):
    """3-light studio rig oriented to the camera, sized to the mesh.

    Energies scaled by size^2 (Cycles area-light falloff is r^2). Lights
    are placed in CAMERA SPACE so changing camera azimuth keeps the rig
    pointing at the same face of the mesh: key from upper-right of
    camera, fill from upper-left, rim from behind to silhouette the
    subject.
    """
    scale2 = (size / 4.0) ** 2
    key_energy = 700 * scale2
    fill_energy = 220 * scale2
    rim_energy = 400 * scale2

    # Camera basis vectors: forward (cam->target), right, up
    cam_forward = (target - cam_location).normalized()
    world_up = mathutils.Vector((0, 0, 1))
    cam_right = cam_forward.cross(world_up).normalized()
    cam_up = cam_right.cross(cam_forward).normalized()
    cam_back = -cam_forward

    def _place(name, light_type, energy, color, size_factor, offset):
        light = bpy.data.lights.new(name=name, type=light_type)
        light.energy = energy
        light.size = size * size_factor
        light.color = color
        obj = bpy.data.objects.new(name, light)
        bpy.context.collection.objects.link(obj)
        right_off, up_off, fwd_off = offset
        obj.location = (
            target
            + cam_right * (right_off * size)
            + cam_up * (up_off * size)
            + cam_back * (fwd_off * size)  # negative pushes toward camera
        )
        direction = target - obj.location
        obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
        return obj

    # Key: upper-right of camera POV, slightly behind subject to wrap light
    _place("Key", "AREA", key_energy, (1.0, 0.97, 0.90), 1.2,
           offset=(1.6, 1.1, -1.0))
    # Fill: lower-left of camera, cool tint for underwater feel
    _place("Fill", "AREA", fill_energy, (0.80, 0.90, 1.0), 1.6,
           offset=(-1.4, 0.4, -0.8))
    # Rim: behind subject relative to camera, slightly above
    _place("Rim", "AREA", rim_energy, (0.95, 0.95, 1.0), 0.9,
           offset=(0.4, 1.2, 1.4))

    world = bpy.context.scene.world or bpy.data.worlds.new("World")
    bpy.context.scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (0.05, 0.06, 0.08, 1.0)
        bg.inputs[1].default_value = 0.2


def configure_view_settings():
    """Pin exposure + view transform so brightness is consistent across slugs.

    Standard view transform (linear sRGB output, no Filmic compression) so
    the texture colors land on screen at full saturation. Exposure 0
    keeps highlights from being clipped without crushing shadows.
    """
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


def main():
    if "--" not in sys.argv:
        print("ERR: missing -- separator")
        sys.exit(1)
    args = sys.argv[sys.argv.index("--") + 1:]
    if len(args) < 2:
        print("Usage: blender ... -- <glb_path> <out_png> [<slug> [<archetype>]]")
        sys.exit(1)

    glb_path = args[0]
    out_png = args[1]
    slug = args[2] if len(args) > 2 else os.path.splitext(os.path.basename(out_png))[0]
    archetype = args[3] if len(args) > 3 else "static"
    angle = args[4] if len(args) > 4 else "auto"

    print(f"\n=== Rendering {glb_path} -> {out_png}")
    print(f"    slug={slug}, archetype={archetype}, angle={angle}")
    clear_scene()
    meshes = import_glb(glb_path)
    if not meshes:
        print("ERR: no mesh objects after import")
        sys.exit(2)
    print(f"Imported {len(meshes)} mesh objects")

    attach_pbr_materials(meshes, glb_path, slug=slug)

    center, extent, size = compute_bounds(meshes)
    print(f"Bounds center={center}, size={size:.2f}")

    cam = setup_camera(center, size, extent, archetype, angle=angle)
    setup_lights(center, size, cam.location)
    configure_view_settings()
    configure_render()

    os.makedirs(os.path.dirname(out_png), exist_ok=True)
    render_to(out_png)
    print(f"DONE -> {out_png}")


if __name__ == "__main__":
    main()
