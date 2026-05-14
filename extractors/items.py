"""Extract UWEItemType assets — every item the player can hold/use/find.

Each item is sourced from two places:

1. **DA_*_ItemType** data asset
     The primary metadata sheet: name, description, icon refs, stack size.
     Set up by designers in the editor.

2. **BP_* Actor blueprint** (when DA_* has no icon)
     Many items leave the Icon/Thumbnail/TooltipIcon slots empty on the data
     asset and instead set the icon directly on the BP class. To get a
     ground-truth icon for every item we follow `ActorClass` to the BP, find
     its CDO export, and read the Icon/Texture properties off it. This
     replaces the frontend's fragile slug-pattern guessing for items like
     Disperser, Scuba Mask, Gateway Crystal, Biosampling Kit, etc. where the
     data asset is empty but the BP carries the real texture path.
"""
from __future__ import annotations

import logging
from typing import Iterable

from helpers import (
    find_export, prop_str, prop_object_path, prop_array, prop_tags,
    safe_load_package, short_name_from_path,
    _extract_soft_path, obj_ref_path,
)

logger = logging.getLogger(__name__)

DIR_PREFIX = "Subnautica2/Content/Data/ItemType/"

# Texture paths that mean "we haven't authored art for this yet" — Unknown
# Worlds uses them as placeholders throughout the pre-EA build. Treat them
# as missing so the BP fallback gets a chance to find a real icon.
PLACEHOLDER_TEXTURES = (
    "T_DefaultImage",
    "T_AlienFace",
    "T_UI_Alterra_LOgo",
    "T_UI_Alterra_Logo",
    "T_PlaceholderImage",
    "T_Placeholder",
)

# Properties on a BP CDO that might hold the inventory icon texture. Game-side
# naming isn't strictly consistent: pre-EA items use a mix of these. We try
# them in order and use the first non-placeholder hit.
BP_ICON_PROPERTIES = (
    "Icon",
    "ItemIcon",
    "InventoryIcon",
    "UIIcon",
    "WorldIcon",
    "Thumbnail",
    "TooltipIcon",
)


def _is_placeholder_path(path: str | None) -> bool:
    """True if the texture path is missing or points at a known placeholder."""
    if not path:
        return True
    # `/Game/UI/Hud/Art/T_DefaultImage.T_DefaultImage` → last segment
    leaf = path.split(".")[0].split("/")[-1]
    return leaf in PLACEHOLDER_TEXTURES


def _bp_pkg_from_actor_class(actor_class_path: str | None) -> str | None:
    """`/Game/.../BP_Disperser.BP_Disperser_C` → `/Game/.../BP_Disperser`."""
    if not actor_class_path:
        return None
    return actor_class_path.split(".", 1)[0]


def _find_bp_cdo(package):
    """Return the Class Default Object export from a BP package.

    UE5 stores it as a `Default__ClassName_C` export. We find it by name
    prefix since the class name varies per BP.
    """
    for export in package.GetExports():
        name = str(export.Name)
        if name.startswith("Default__"):
            return export
    return None


def _resolve_actor_icon(provider, actor_class_path: str | None) -> str | None:
    """Follow ActorClass to the BP, return the first icon-property path.

    Searches in priority order:
      1. Icon properties directly on the BP CDO (e.g. `Icon`, `ItemIcon`).
      2. `Thumbnail` on every UWEScanData attached via
         `UWEAssetUserData_0.DataAssets`. Items like Gateway Crystal don't
         set an icon on the DA_*_ItemType but DO have a sibling ScanData
         asset with a `Thumbnail` property pointing at the IconBaker output.

    Skips placeholder textures so a BP that explicitly points at
    T_DefaultImage doesn't mask a real icon from a downstream property.
    """
    pkg_path = _bp_pkg_from_actor_class(actor_class_path)
    if pkg_path is None:
        return None
    package = safe_load_package(provider, pkg_path)
    if package is None:
        return None
    cdo = _find_bp_cdo(package)
    if cdo is None:
        return None

    # Tier 1 — direct icon properties on the BP CDO.
    for prop_name in BP_ICON_PROPERTIES:
        path = prop_object_path(cdo, prop_name)
        if path and not _is_placeholder_path(path):
            return path

    # Tier 2 — follow UWEAssetUserData_0.DataAssets[] and look at Thumbnails
    # on attached scan-data / ping-data assets.
    for ex in package.GetExports():
        if str(ex.Name) != "UWEAssetUserData_0":
            continue
        for da_ref in prop_array(ex, "DataAssets"):
            da_path = obj_ref_path(da_ref) or _extract_soft_path(da_ref)
            if not da_path:
                continue
            # Strip the trailing `'(ObjectProperty)` decoration if present and
            # any `.AssetName` suffix.
            da_path = da_path.split("'")[0].split(".")[0]
            # Skip the DA_*_ItemType — that's where we started.
            if da_path.endswith("_ItemType") or "/ItemType/" in da_path:
                continue
            da_pkg = safe_load_package(provider, da_path)
            if da_pkg is None:
                continue
            for da_ex in da_pkg.GetExports():
                etype = str(getattr(da_ex, "ExportType", "") or "")
                # UWEScanData carries the IconBaker thumbnail for items with
                # a scan flow (Gateway Crystal, Lightstick, Power Cell, etc.)
                if etype not in ("UWEScanData", "UWEPingData"):
                    continue
                thumb = prop_object_path(da_ex, "Thumbnail")
                if thumb and not _is_placeholder_path(thumb):
                    # Skip the generic ping texture - it's reused across
                    # every mineral ping and isn't specific to this item.
                    leaf = thumb.split(".")[0].split("/")[-1]
                    if leaf.endswith("PingST") or leaf.startswith("T_Icon_ResourcePing"):
                        continue
                    return thumb
    return None


def find_paths(provider) -> list[str]:
    out = []
    for path in provider.Files.Keys:
        if path.startswith(DIR_PREFIX) and path.endswith(".uasset"):
            out.append(path)
    return sorted(out)


def extract_item(provider, asset_path: str) -> dict | None:
    pkg_path = asset_path[:-7]
    package = safe_load_package(provider, pkg_path)
    if package is None:
        return None
    export = find_export(package, class_substring="UWEItemType")
    if export is None:
        return None

    icon = prop_object_path(export, "Icon")
    thumbnail = prop_object_path(export, "Thumbnail")
    tooltip_icon = prop_object_path(export, "TooltipIcon")
    actor_class = prop_object_path(export, "ActorClass")

    # If none of the DA_*_ItemType icon slots have a real texture, follow the
    # BP class to find one. Promote it into the `icon` field so the frontend's
    # already-implemented `parseTextureRef([icon, thumbnail, tooltip_icon])`
    # pipeline picks it up automatically, with no slug guessing needed.
    if (
        _is_placeholder_path(icon)
        and _is_placeholder_path(thumbnail)
        and _is_placeholder_path(tooltip_icon)
    ):
        bp_icon = _resolve_actor_icon(provider, actor_class)
        if bp_icon:
            icon = bp_icon

    return {
        "id": short_name_from_path(pkg_path),
        "asset": pkg_path,
        "name": prop_str(export, "Name"),
        "description": prop_str(export, "ItemDescription"),
        "actor_class": actor_class,
        "icon": icon,
        "thumbnail": thumbnail,
        "category": prop_object_path(export, "Category"),
        "tags": prop_tags(export, "IdentifierTag"),
        "category_tag": prop_tags(export, "CategoryTag"),
        "tooltip_icon": tooltip_icon,
        "stack_size": int(getattr(export, "MaxStackSize", 0) or 0) or None,
    }


def run(provider) -> list[dict]:
    paths = find_paths(provider)
    logger.info("Items: %d candidate assets", len(paths))
    out = []
    for i, p in enumerate(paths, 1):
        item = extract_item(provider, p)
        if item is not None:
            out.append(item)
        if i % 200 == 0:
            logger.info("  items: %d / %d", i, len(paths))
    logger.info("Items: extracted %d", len(out))
    return out
