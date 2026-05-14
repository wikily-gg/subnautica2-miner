"""
Fallback texture extractor for meshes whose materials don't decode via
CUE4Parse-Conversion's MaterialExporter2.

Two failure modes we handle here:

1. **Material exporter skips a referenced material** (VT / Substrate /
   custom shading model). Mesh DOES have a real material ref but the
   exporter can't decode it. We walk the material chain directly.

2. **Cooked mesh slot points at `WorldGridMaterial`** (engine debug fallback).
   The real material is assigned at runtime by the actor's Blueprint,
   not stored in the .uasset. For these we sweep the paks for sibling
   `MI_*` material instances under predictable paths
   (`Blueprints/AI/Agents/Prototypes/<Name>/`,
   `Art/Vehicles/<Name>/Materials/`,
   `Art/Creatures/<Name>/Materials/`) and decode every texture they
   reference.

For every UTexture2D we find, we decode it (same logic as icons/extractor)
and write it next to the .glb under `<slug>/_fallback_textures/<name>.png`
so the existing Blender material-attach pass can pick it up.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from CUE4Parse.FileProvider import DefaultFileProvider

logger = logging.getLogger(__name__)


def _decode_texture(texture_export, provider=None):
    """Decode a UTexture2D to a PIL.Image (RGBA).

    Uses CUE4Parse-Conversion's `TextureDecoder.Decode()` extension
    method, which understands SN2's `.ubulk` payload files. The Python
    `texture2ddecoder` route only sees the in-`.uasset` bytes and
    returns empty mips for any texture whose data lives in a sibling
    `.ubulk` (every body / vehicle texture in SN2).
    """
    from PIL import Image
    import clr  # noqa: F401
    clr.AddReference("CUE4Parse-Conversion")
    from CUE4Parse_Conversion.Textures import TextureDecoder  # type: ignore

    fmt = str(getattr(texture_export, "Format", "?"))

    # CTexture? Decode(this UTexture texture, ETexturePlatform platform = DesktopMobile)
    try:
        ctex = TextureDecoder.Decode(texture_export)
    except Exception as e:
        return None, f"{fmt} (decode-exc: {str(e)[:120]})"
    if ctex is None:
        return None, fmt

    w = ctex.Width
    h = ctex.Height
    raw = bytes(ctex.Data)
    if not raw:
        return None, fmt

    # TextureDecoder returns a BGRA byte stream (regardless of source format)
    try:
        img = Image.frombytes("RGBA", (w, h), raw, "raw", "BGRA")
    except Exception as e:
        return None, f"{fmt} (frombytes-exc: {str(e)[:120]})"
    return img, fmt


def _resolve_object_path(provider, soft_path) -> object | None:
    """Take a UE SoftObjectPath / ObjectPath and load the target export."""
    # SoftObjectPath shows up as either FSoftObjectPath or a stringy thing
    pkg = None
    if hasattr(soft_path, "AssetPathName"):
        # FSoftObjectPath
        path_obj = soft_path.AssetPathName
        pkg = str(path_obj).strip()
    elif hasattr(soft_path, "ObjectPath"):
        pkg = str(soft_path.ObjectPath).strip()
    elif hasattr(soft_path, "ToString"):
        pkg = soft_path.ToString().strip()
    else:
        pkg = str(soft_path).strip()
    if not pkg or pkg.lower() == "none":
        return None

    # Strip object name suffix after the dot
    if "." in pkg.rsplit("/", 1)[-1]:
        pkg = pkg.rsplit(".", 1)[0]

    # `/Game/...` -> `Subnautica2/Content/...`
    if pkg.startswith("/Game/"):
        pkg = "Subnautica2/Content/" + pkg[len("/Game/"):]

    try:
        ok, package = provider.TryLoadPackage(pkg)
    except Exception:
        return None
    if not ok or package is None:
        return None
    return package


def _is_texture2d(export) -> bool:
    return "Texture2D" in type(export).__name__


def _walk_material(provider, material_obj, found_textures: dict[str, object], depth: int = 0):
    """Recursively walk a UMaterialInterface chain pulling out Texture2D refs."""
    if material_obj is None or depth > 5:
        return

    # MaterialInstance has TextureParameterValues
    tex_params_field = None
    for name in ("TextureParameterValues",):
        try:
            v = getattr(material_obj, name, None)
            if v is not None:
                tex_params_field = v
                break
        except Exception:
            continue

    if tex_params_field is not None:
        try:
            for entry in tex_params_field:
                # entry is FTextureParameterValue with .ParameterInfo.Name and .ParameterValue (UTexture2D ref)
                param_info = getattr(entry, "ParameterInfo", None)
                name = None
                if param_info is not None:
                    name = str(getattr(param_info, "Name", "")).strip()
                if not name:
                    name = "tex"
                value = getattr(entry, "ParameterValue", None)
                if value is None:
                    continue
                # value is a UTexture2D export reference (lazy)
                resolved = None
                try:
                    resolved = value.Load()  # PackageIndex.Load() returns the export
                except Exception:
                    pass
                if resolved is None:
                    try:
                        resolved = value.ResolvedObject
                        if resolved is not None:
                            resolved = resolved.Load()
                    except Exception:
                        pass
                if resolved is None or not _is_texture2d(resolved):
                    continue
                tex_name = str(resolved.Name).strip()
                if tex_name and tex_name not in found_textures:
                    found_textures[tex_name] = resolved
        except Exception:
            pass

    # Walk to parent material
    parent = None
    try:
        parent_ref = getattr(material_obj, "Parent", None)
        if parent_ref is not None:
            try:
                parent = parent_ref.Load()
            except Exception:
                pass
    except Exception:
        pass
    if parent is not None and parent is not material_obj:
        _walk_material(provider, parent, found_textures, depth + 1)


def collect_textures_from_mesh(provider, mesh_export) -> dict[str, object]:
    """Return {texture_name: UTexture2D export} for every texture reachable
    from the mesh's material slot interfaces."""
    found: dict[str, object] = {}

    # Both UStaticMesh and USkeletalMesh expose materials. Try common names.
    for field_name in ("StaticMaterials", "Materials", "SkeletalMaterials"):
        slots = getattr(mesh_export, field_name, None)
        if slots is None:
            continue
        try:
            iterable = list(slots)
        except Exception:
            continue
        for slot in iterable:
            # Slot can be FStaticMaterial / FSkeletalMaterial - has .Material or .MaterialInterface
            mat_ref = None
            for cand in ("MaterialInterface", "Material"):
                mat_ref = getattr(slot, cand, None)
                if mat_ref is not None:
                    break
            if mat_ref is None:
                continue
            try:
                mat_obj = mat_ref.Load()
            except Exception:
                mat_obj = None
            if mat_obj is None:
                continue
            _walk_material(provider, mat_obj, found)

    return found


def _looks_like_engine_default(material_obj) -> bool:
    """Detect the WorldGridMaterial / DefaultMaterial fallbacks that
    leak into uncooked mesh material slots."""
    if material_obj is None:
        return True
    name = str(getattr(material_obj, "Name", "")).lower()
    if name in ("worldgridmaterial", "defaultmaterial", "default", "basicshapematerial"):
        return True
    pkg_path = ""
    try:
        pkg_path = str(material_obj.Owner.Name).lower()
    except Exception:
        pass
    return "/engine/enginematerials" in pkg_path


def _extract_creature_name(slug: str) -> str:
    """Pull the canonical CamelCase creature/vehicle name out of a mesh slug.

    Examples:
        SKM_AnemoneCrab            -> AnemoneCrab
        SKM_DeepwingBrooder_01     -> DeepwingBrooder
        SKM_ElusiveLeviathan       -> ElusiveLeviathan
        SKM_Tadpole_HAUL           -> Tadpole_HAUL
        Resources_SM_X_Celestine_01a -> Celestine
    """
    s = slug
    for pref in ("Resources_", "Animation_", "Mesh_", "Flashfish_In_World_"):
        if s.startswith(pref):
            s = s[len(pref):]
    for pref in ("SKM_", "SK_", "SM_"):
        if s.startswith(pref):
            s = s[len(pref):]
            break
    # Trim trailing numeric variants `_01`, `_01a`, `_02`
    import re
    s = re.sub(r"_\d+[a-z]?$", "", s)
    return s


def _find_sibling_mis(provider, slug: str) -> list[str]:
    """Walk paks for MI_*.uasset files that look related to this slug.

    Heuristic: any MI whose pak-relative path contains the creature name
    AND is under one of the predictable directories that SN2 uses for
    runtime material overrides.
    """
    name = _extract_creature_name(slug)
    name_lower = name.lower()
    hits: list[str] = []
    for path in provider.Files.Keys:
        p = path.replace("\\", "/").lower()
        if not p.endswith(".uasset"):
            continue
        leaf = p.rsplit("/", 1)[-1]
        if not leaf.startswith("mi_"):
            continue
        if name_lower not in p:
            continue
        # Only sweep under predictable runtime-override directories
        if not any(seg in p for seg in (
            "/blueprints/ai/agents/", "/blueprints/vehicles/",
            "/blueprints/creatures/", "/art/vehicles/", "/art/creatures/",
            "/art/tools/", "/blueprints/items/", "/materials/",
        )):
            continue
        hits.append(path)
    return hits


def _find_sibling_textures(provider, slug: str) -> list[str]:
    """Walk paks for T_*.uasset files that look related to this slug.

    Some SN2 tools (e.g. SKM_AirBladder) have NO Material Instances and
    their parent material is empty. The actual surface textures live as
    standalone `T_<slug>_diffuse|normal|spec.uasset` files alongside the
    mesh. We grab those directly so the matcher has something to bind.
    """
    name = _extract_creature_name(slug)
    name_lower = name.lower()
    hits: list[str] = []
    for path in provider.Files.Keys:
        if not path.endswith(".uasset"):
            continue
        p = path.replace("\\", "/").lower()
        leaf = p.rsplit("/", 1)[-1]
        if not leaf.startswith("t_"):
            continue
        if name_lower not in p:
            continue
        # Stay under the asset's own folder + nearby Texture folders
        if not any(seg in p for seg in (
            "/art/tools/", "/art/vehicles/", "/art/creatures/",
            "/art/items/", "/blueprints/", "/materials/",
        )):
            continue
        # Skip UI icons, fabrication FX, etc.
        if "/utility/editor/" in p or "iconbaker" in p:
            continue
        hits.append(path)
    return hits


def _decode_texture_path(provider, texture_path: str, name_hint: str = "") -> tuple[object, str] | None:
    """Load a texture .uasset by path and return (PIL.Image, format).

    Returns None on failure. Same decoding strategy as
    write_fallback_textures - uses CUE4Parse-Conversion TextureDecoder.
    """
    pkg_path = texture_path
    if pkg_path.endswith(".uasset"):
        pkg_path = pkg_path[:-7]
    try:
        ok, pkg = provider.TryLoadPackage(pkg_path)
    except Exception:
        return None
    if not ok or pkg is None:
        return None
    for ex in pkg.GetExports():
        if "Texture2D" not in type(ex).__name__:
            continue
        try:
            img, fmt = _decode_texture(ex)
        except Exception:
            return None
        if img is None:
            return None
        return (img, fmt)
    return None


def _extract_textures_from_mi_package(provider, mi_path: str, found: dict[str, str]):
    """Load an MI .uasset and collect every texture parameter value as
    a `{texture_name: texture_package_path}` mapping.

    We store package paths (not exports) so the later decode pass can
    do a fresh `TryLoadPackage` on each texture - same pattern that
    icons/extractor.py uses successfully for 436+ textures.
    """
    pkg_path = mi_path
    if pkg_path.endswith(".uasset"):
        pkg_path = pkg_path[:-7]
    try:
        ok, package = provider.TryLoadPackage(pkg_path)
    except Exception:
        return
    if not ok or package is None:
        return
    for ex in package.GetExports():
        tname = type(ex).__name__
        if "Material" not in tname:
            continue
        _walk_material_paths(provider, ex, found)


def _walk_material_paths(provider, material_obj, found: dict[str, str], depth: int = 0):
    """Like _walk_material but stores texture package paths instead of
    holding live export references that might be missing bulk data."""
    if material_obj is None or depth > 5:
        return

    tex_params = getattr(material_obj, "TextureParameterValues", None)
    if tex_params is not None:
        try:
            for entry in tex_params:
                value = getattr(entry, "ParameterValue", None)
                if value is None:
                    continue
                resolved = None
                try:
                    resolved = value.Load()
                except Exception:
                    pass
                if resolved is None or not _is_texture2d(resolved):
                    continue
                tex_name = str(resolved.Name).strip()
                owner = getattr(resolved, "Owner", None)
                pkg_name = str(owner.Name) if owner is not None else None
                if not (tex_name and pkg_name):
                    continue
                if pkg_name.startswith("/Game/"):
                    pkg_name = "Subnautica2/Content/" + pkg_name[len("/Game/"):]
                if tex_name not in found:
                    found[tex_name] = pkg_name
        except Exception:
            pass

    # Walk to parent material
    parent_ref = getattr(material_obj, "Parent", None)
    if parent_ref is not None:
        try:
            parent_obj = parent_ref.Load()
        except Exception:
            parent_obj = None
        if parent_obj is not None and parent_obj is not material_obj:
            _walk_material_paths(provider, parent_obj, found, depth + 1)


def write_fallback_textures(provider, mesh_export, out_dir: str, slug: str | None = None) -> int:
    """Find every texture the mesh and sibling MIs reference and write
    a decoded PNG per texture into `<out_dir>/_fallback_textures/`.

    Strategy:
      1. Collect texture *package paths* (not loaded exports) via the
         mesh's own material slots + a slug-based MI sweep.
      2. Decode each by doing a fresh `TryLoadPackage` on the texture's
         own package - same pattern icons/extractor.py uses, which is
         the only known way to get reliable bulk data in our setup.

    Returns count of new PNGs written.
    """
    found: dict[str, str] = {}

    # 1) From mesh slots (skipping engine defaults)
    for field_name in ("StaticMaterials", "Materials", "SkeletalMaterials"):
        slots = getattr(mesh_export, field_name, None)
        if slots is None:
            continue
        try:
            iterable = list(slots)
        except Exception:
            continue
        for slot in iterable:
            mat_ref = None
            for cand in ("MaterialInterface", "Material"):
                mat_ref = getattr(slot, cand, None)
                if mat_ref is not None:
                    break
            if mat_ref is None:
                continue
            try:
                mat_obj = mat_ref.Load()
            except Exception:
                mat_obj = None
            if _looks_like_engine_default(mat_obj):
                continue
            _walk_material_paths(provider, mat_obj, found)

    # 2) Sibling MI sweep by slug name
    if slug:
        for mi_path in _find_sibling_mis(provider, slug):
            _extract_textures_from_mi_package(provider, mi_path, found)

    # 3) Sibling TEXTURE sweep - for assets whose materials are empty
    # placeholders (e.g. SKM_AirBladder) but the actual T_<slug>_diffuse,
    # T_<slug>_normal, etc. live next to the mesh. We don't go through
    # any MI; just decode the .uasset directly.
    sibling_texture_paths: dict[str, str] = {}
    if slug:
        for tex_path in _find_sibling_textures(provider, slug):
            tex_name = tex_path.rsplit("/", 1)[-1]
            if tex_name.lower().endswith(".uasset"):
                tex_name = tex_name[: -len(".uasset")]
            # Avoid duplicates of textures we already found through MIs
            if tex_name not in found:
                sibling_texture_paths[tex_name] = tex_path

    if not found and not sibling_texture_paths:
        return 0

    dump_dir = os.path.join(out_dir, "_fallback_textures")
    os.makedirs(dump_dir, exist_ok=True)

    saved = 0

    # 3a) Decode sibling-texture sweep first (these come from direct
    # uasset paths and don't need MI walking)
    for tex_name, tex_path in sibling_texture_paths.items():
        out_path = os.path.join(dump_dir, f"{tex_name}.png")
        if os.path.exists(out_path):
            continue
        result = _decode_texture_path(provider, tex_path, tex_name)
        if result is None:
            continue
        img, fmt = result
        img.save(out_path, "PNG")
        saved += 1

    for tex_name, pkg_path in found.items():
        out_path = os.path.join(dump_dir, f"{tex_name}.png")
        if os.path.exists(out_path):
            continue

        # Fresh package load for reliable bulk data
        try:
            ok, package = provider.TryLoadPackage(pkg_path)
        except Exception as e:
            logger.warning("  load failed for %s: %s", tex_name, str(e)[:160])
            continue
        if not ok or package is None:
            logger.warning("  could not load %s package %s", tex_name, pkg_path)
            continue

        tex_export = None
        for ex in package.GetExports():
            if "Texture2D" in type(ex).__name__:
                tex_export = ex
                break
        if tex_export is None:
            continue

        try:
            img, fmt = _decode_texture(tex_export)
        except Exception as e:
            logger.warning("  decode failed for %s: %s", tex_name, str(e)[:160])
            continue
        if img is None:
            logger.warning("  decode None (fmt=%s) for %s", fmt, tex_name)
            continue

        img.save(out_path, "PNG")
        saved += 1

    if saved:
        logger.info("  fallback textures: wrote %d PNGs to %s", saved, dump_dir)
    return saved
