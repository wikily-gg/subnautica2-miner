"""
Extract icon / image / dossier / encyclopedia textures from Subnautica 2
paks and save as WebP. Output is a flat folder under `out/icons/`.

Two-pass discovery:

1. **JSON-ref sweep** - regex over the extracted JSONs in `out/` for
   `/Game/.../T_*` style refs that look like UI textures. This picks up
   every icon the gameplay data file explicitly references.

2. **Path-based sweep** - walks `provider.Files.Keys` for assets that
   live in well-known UI / icon / dossier directories. Catches assets
   the JSONs don't reference (databank panels, biomod ability glyphs,
   PDA tab icons, posters, character portrait thumbs, etc.).

Both passes feed the same flat output folder, with `_2`/`_3` suffixes
appended when names collide.

Decoding goes through CUE4Parse-Conversion's `TextureDecoder.Decode()`,
which understands SN2's `.ubulk` payload files. The python-only
`texture2ddecoder` path was missing the bulk data of every texture
larger than ~64 KB (every dossier / hero image).
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from CUE4Parse.FileProvider import DefaultFileProvider

import config

logger = logging.getLogger(__name__)


# /Game/Foo/Bar/T_Baz  or  /Game/Foo/Bar/T_Baz.T_Baz  (both occur in serialized JSON)
_TEXTURE_REF_RE = re.compile(r"/Game/[A-Za-z0-9_./]+")


# Suffixes that mark a texture as a per-mesh PBR map - NEVER include these
# in the icon sweep, they're surface textures not UI assets.
#
# NOTE: `_BCO` (BaseColor + Opacity in alpha) is intentionally NOT here.
# SN2 posters / decals use it as a channel-pack and the visible image IS
# the BC channel, so we want to extract them.
_MESH_TEXTURE_SUFFIXES = (
    "_BC", "_BCM", "_BCA", "_BCE", "_BCM_NVT", "_NVT",
    "_NRS", "_NRH", "_NRT", "_NRMSK", "_NRT_NVT",
    "_OAE", "_ORM", "_AOM", "_CAOT", "_OMRA", "_OPRA", "_OGI",
    "_Mask", "_MASK", "_IDMask", "_GlowMask", "_PupilMask", "_Alpha", "_BN",
    "_N", "_NM", "_Normal", "_PNRH",
    "_R", "_M", "_AO", "_A", "_E",
)


def _looks_like_pbr_suffix(leaf: str) -> bool:
    base = leaf.rsplit(".", 1)[0]
    return any(base.endswith(s) for s in _MESH_TEXTURE_SUFFIXES)


# ---------------------------------------------------------------------------
# Path-based sweep - what counts as a UI / icon / dossier asset
# ---------------------------------------------------------------------------

# Whitelist directory substrings (lowercased). Asset paths containing ANY of
# these are eligible for the path-based sweep.
_UI_DIR_PATTERNS = (
    "/ui/",
    "/utility/editor/iconbaker/",
    "/blueprints/ui/",
    "/blueprints/mainmenulogorendertarget/",   # T_SN2_Logo_2D / _3D
    "/prototyping/deepstart/subnautica2logo/", # T_SN2_Logo
    "/art/items/posterframe/",
    "/art/environment/set/alterra/",           # Alterra propaganda + cat posters
    "/posters/",                                # any poster dir generally
    "/uiimages/",
    "/icons/",
    "/icon/",
    "/uiabilityicons/",
    "/biomod",            # picks /Biomods/ + UI_biomodAbilities under UI
    "/glyphs/",
    "/tabsicons/",
    "/iconbaker/",
    "/art/character/humanoid/",   # player portraits / thumbs / color variants
    "/loadingscreens/",            # painted concept-art biome hero shots
    "/textures/vfx/screens/",      # HUD overlay backgrounds (DataCard, HullPlate)
)

# Filename-suffix whitelist: even if the directory check fails, accept any
# texture whose leaf ends in one of these (portraits / thumbs / avatars).
_UI_LEAF_SUFFIXES = (
    "_portrait", "_thumb", "_avatar", "_card", "_dossier",
)

# Blacklist directories - even if they live under one of the whitelist
# patterns, drop them (mesh textures, VFX, shaders, audio buses).
_UI_DIR_BLACKLIST = (
    "/textures/vfx/",
    "/art/environment/",
    "/art/surfaces/",
    "/art/character/mannequins/",
    "/materials/textures/",
    "/fmod/",
    "/external/",
    "/prototyping/",
)


def _is_ui_path(path: str) -> bool:
    p = "/" + path.lower()
    if any(b in p for b in _UI_DIR_BLACKLIST):
        return False
    if any(b in p for b in _UI_DIR_PATTERNS):
        return True
    # Filename-suffix path: portrait / thumb / dossier files outside /UIImages/
    leaf = p.rsplit("/", 1)[-1]
    leaf_base = leaf.rsplit(".", 1)[0]
    if any(leaf_base.endswith(s) for s in _UI_LEAF_SUFFIXES):
        return True
    return False


def collect_path_based_refs(provider) -> dict[str, str]:
    """Walk provider.Files.Keys for UTexture2D-shaped assets in UI/icon
    directories that the JSON-ref sweep doesn't cover.

    Returns: `{texture_name: /Game/-style pkg path}`.
    """
    out: dict[str, str] = {}
    for path in provider.Files.Keys:
        if not path.lower().endswith(".uasset"):
            continue
        leaf = path.rsplit("/", 1)[-1]
        # Must be texture-prefixed
        leaf_lower = leaf.lower()
        if not (
            leaf_lower.startswith("t_")
            or leaf_lower.startswith("tex_")
            or leaf_lower.startswith("img_")
            or leaf_lower.startswith("image_")
            or leaf_lower.startswith("ui_")
            or leaf_lower.startswith("icon")
            or leaf_lower.startswith("poster_")
        ):
            continue
        # Drop PBR maps (mesh surface textures)
        if _looks_like_pbr_suffix(leaf):
            continue
        if not _is_ui_path(path):
            continue
        pkg = path
        if pkg.lower().endswith(".uasset"):
            pkg = pkg[: -len(".uasset")]
        if pkg.startswith("Subnautica2/Content/"):
            pkg = "/Game/" + pkg[len("Subnautica2/Content/"):]
        name = leaf.rsplit(".", 1)[0]
        out.setdefault(name, pkg)
    return out


# ---------------------------------------------------------------------------
# JSON-ref sweep
# ---------------------------------------------------------------------------


def _is_texture_path(p: str) -> bool:
    leaf = p.rsplit("/", 1)[-1]
    if leaf.startswith("T_"):
        return True
    return any(seg in p for seg in ("/Icons/", "/UI/", "/Textures/", "/Images/", "/UIImages/"))


def collect_json_refs(output_dir: str) -> dict[str, str]:
    refs: dict[str, str] = {}
    for json_path in sorted(glob.glob(os.path.join(output_dir, "**", "*.json"), recursive=True)):
        if ".upload_manifest" in json_path or "/icons/" in json_path.replace("\\", "/"):
            continue
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                blob = f.read()
        except Exception:
            continue
        for m in _TEXTURE_REF_RE.finditer(blob):
            raw = m.group(0)
            if "." in raw.rsplit("/", 1)[-1]:
                pkg = raw.rsplit(".", 1)[0]
            else:
                pkg = raw
            if not _is_texture_path(pkg):
                continue
            leaf = pkg.rsplit("/", 1)[-1]
            if _looks_like_pbr_suffix(leaf):
                continue
            refs.setdefault(leaf, pkg)
    return refs


# ---------------------------------------------------------------------------
# CUE4Parse-Conversion TextureDecoder (handles .ubulk payloads)
# ---------------------------------------------------------------------------


def _pkg_to_load_path(pkg: str) -> str:
    if pkg.startswith("/Game/"):
        return "Subnautica2/Content/" + pkg[len("/Game/"):]
    return pkg


_DETEX_INITIALIZED = False


def _ensure_detex():
    """Initialize CUE4Parse's Detex helper exactly once per process.

    SN2 stores its small UI icons as BC7-compressed textures.
    `TextureDecoder.Decode()` delegates BC7 decompression to the
    native `Detex.dll`, but only if `DetexHelper.Initialize()` has
    been called first. Without this every BC7 icon raises
    'Detex decompression failed: not initialized' silently.
    """
    global _DETEX_INITIALIZED
    if _DETEX_INITIALIZED:
        return
    import clr  # noqa: F401
    clr.AddReference("CUE4Parse-Conversion")
    from CUE4Parse_Conversion.Textures.BC import DetexHelper  # type: ignore

    candidates = [
        os.path.join(config.CUE4PARSE_DLL_DIR, "Detex.dll"),
        r"A:\Python\CUE4Parse\CUE4Parse-Conversion\Resources\Detex.dll",
    ]
    initialized_from = None
    for cand in candidates:
        if os.path.exists(cand):
            try:
                DetexHelper.Initialize(cand)
                initialized_from = cand
                break
            except Exception as e:
                logger.warning("Detex Initialize failed for %s: %s", cand, e)
    if initialized_from is None:
        # Try the embedded resource (writes Detex.dll to cwd)
        try:
            DetexHelper.LoadDll(None)
            DetexHelper.Initialize("Detex.dll")
            initialized_from = "embedded"
        except Exception as e:
            logger.warning("Detex bootstrap failed: %s", e)

    if initialized_from:
        logger.info("Detex initialized from %s", initialized_from)
    _DETEX_INITIALIZED = True


def _decode_texture(texture_export):
    """Decode a UTexture2D to a PIL Image (RGBA) via the C# TextureDecoder.

    `TextureDecoder.Decode()` handles `.ubulk` streaming payloads, which
    is where SN2 stores every texture larger than ~64 KB. The earlier
    python-only `texture2ddecoder` path returned empty mips for those
    and silently produced no output.

    Detex must be initialized before BC7 textures can decode - we ensure
    that once at first call.
    """
    from PIL import Image
    import clr  # noqa: F401
    clr.AddReference("CUE4Parse-Conversion")
    from CUE4Parse_Conversion.Textures import TextureDecoder  # type: ignore

    _ensure_detex()

    fmt = str(getattr(texture_export, "Format", "?"))

    try:
        ctex = TextureDecoder.Decode(texture_export)
    except Exception as e:
        return None, f"{fmt} (decode-exc: {str(e)[:120]})"
    if ctex is None:
        return None, fmt

    w, h = ctex.Width, ctex.Height
    raw = bytes(ctex.Data)
    if not raw:
        return None, fmt

    try:
        img = Image.frombytes("RGBA", (w, h), raw, "raw", "BGRA")
    except Exception as e:
        return None, f"{fmt} (frombytes-exc: {str(e)[:120]})"
    return img, fmt


def _next_unique_name(name: str, used: set[str]) -> str:
    if name not in used:
        return name
    i = 2
    while True:
        cand = f"{name}_{i}"
        if cand not in used:
            return cand
        i += 1


def extract_one(
    provider: "DefaultFileProvider",
    name: str,
    pkg_path: str,
    output_dir: str,
    used_names: set[str],
) -> str | None:
    load_path = _pkg_to_load_path(pkg_path)
    try:
        ok, package = provider.TryLoadPackage(load_path)
    except Exception as e:
        logger.warning("  [%s] load failed: %s", name, e)
        return None
    if not ok or package is None:
        logger.warning("  [%s] package missing: %s", name, load_path)
        return None

    texture_export = None
    for export in package.GetExports():
        if "Texture2D" in type(export).__name__:
            texture_export = export
            break
    if texture_export is None:
        logger.warning("  [%s] no Texture2D export", name)
        return None

    img, fmt = _decode_texture(texture_export)
    if img is None:
        logger.warning("  [%s] decode None (fmt=%s)", name, fmt)
        return None

    # Cosmetic dedup: only suffix if it's actually a different texture path
    final_name = _next_unique_name(name, used_names)
    used_names.add(final_name)
    out_path = os.path.join(output_dir, f"{final_name}.webp")
    img.save(out_path, "WEBP", quality=90, method=6)
    logger.info("  [%s] %dx%d %s -> %s", final_name, img.width, img.height, fmt, os.path.basename(out_path))
    return f"{final_name}.webp"


def extract_all() -> dict[str, str]:
    """Run both passes and decode every unique texture found.

    Returns `{TextureName: filename.webp}` for the manifest.
    """
    from provider import create_provider, flush_memory

    provider = create_provider()

    json_refs = collect_json_refs(config.OUTPUT_DIR)
    path_refs = collect_path_based_refs(provider)

    # Merge - JSON refs win (their package paths come straight from the
    # gameplay data so they're guaranteed to be the canonical path)
    all_refs: dict[str, str] = dict(path_refs)
    for k, v in json_refs.items():
        all_refs[k] = v

    logger.info(
        "Discovery: %d json refs + %d path refs -> %d unique (overlap %d)",
        len(json_refs), len(path_refs), len(all_refs),
        len(json_refs) + len(path_refs) - len(all_refs),
    )

    output_dir = os.path.join(config.OUTPUT_DIR, "icons")
    os.makedirs(output_dir, exist_ok=True)

    used_names: set[str] = set()
    # Pre-seed with anything already on disk so a re-run is stable
    for fn in os.listdir(output_dir):
        if fn.endswith(".webp"):
            used_names.add(os.path.splitext(fn)[0])

    name_to_file: dict[str, str] = {}
    success = 0
    skipped = 0
    total = len(all_refs)
    for i, (name, pkg) in enumerate(sorted(all_refs.items()), 1):
        # Skip if we already have it on disk (idempotent re-runs)
        candidate = os.path.join(output_dir, f"{name}.webp")
        if os.path.exists(candidate):
            name_to_file[name] = f"{name}.webp"
            skipped += 1
            success += 1
            continue
        webp_name = extract_one(provider, name, pkg, output_dir, used_names)
        if webp_name:
            name_to_file[name] = webp_name
            success += 1
        if i % 100 == 0:
            flush_memory()
            logger.info("Progress: %d/%d (%d ok, %d cached)", i, total, success - skipped, skipped)

    manifest_path = os.path.join(output_dir, "_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({
            "icons": name_to_file,
            "stats": {
                "json_refs": len(json_refs),
                "path_refs": len(path_refs),
                "unique_refs": total,
                "extracted": success,
                "cached": skipped,
            },
        }, f, indent=2, ensure_ascii=False, sort_keys=True)

    logger.info(
        "Icon extraction complete: %d / %d ok (%d cached, %d failed) -> %s",
        success, total, skipped, total - success, manifest_path,
    )
    return name_to_file
