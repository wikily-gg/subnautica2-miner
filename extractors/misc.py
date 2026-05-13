"""Remaining wiki-relevant data: biomods, resource nodes, characters,
   surface-spawn data, biome/world-pop."""
from __future__ import annotations

import logging

from helpers import (
    _export_class, array_values, find_export, prop, prop_array, prop_enum,
    prop_int, prop_object_path, prop_str, prop_tags, safe_load_package,
    short_name_from_path, struct_int, struct_obj_path, struct_str, unwrap_struct,
)

logger = logging.getLogger(__name__)


def _walk(provider, prefix: str):
    for path in provider.Files.Keys:
        if path.startswith(prefix) and path.endswith(".uasset"):
            yield path


# ---------------- Biomods ----------------

def extract_biomod(provider, asset_path: str) -> dict | None:
    pkg_path = asset_path[:-7]
    package = safe_load_package(provider, pkg_path)
    if package is None:
        return None
    export = find_export(package, class_substring="UWEBioAbilityData")
    if export is None:
        return None
    return {
        "id": short_name_from_path(pkg_path),
        "asset": pkg_path,
        "name": prop_str(export, "Name"),
        "description": prop_str(export, "Description"),
        "icon": prop_object_path(export, "Icon"),
        "ability": prop_object_path(export, "BioAbility"),
        "type": prop_enum(export, "BioAbilityType"),
        "ability_tag": (prop_tags(export, "AbilityTag") or [None])[0],
    }


def run_biomods(provider) -> list[dict]:
    paths = sorted(_walk(provider, "Subnautica2/Content/Data/Biomods/"))
    out = [r for r in (extract_biomod(provider, p) for p in paths) if r]
    logger.info("Biomods: %d extracted", len(out))
    return out


# ---------------- Resource nodes (Resonatable) ----------------

def extract_resonatable(provider, asset_path: str) -> dict | None:
    pkg_path = asset_path[:-7]
    package = safe_load_package(provider, pkg_path)
    if package is None:
        return None
    export = find_export(package, class_substring="UWEResonatableData")
    if export is None:
        return None

    contents = []
    for el in array_values(prop_array(export, "Content")):
        u = unwrap_struct(el)
        if u is None:
            continue
        contents.append({
            "resource_class": prop_object_path(u, "ResourceClass"),
            "num_to_drop": prop_int(u, "NumResourcesToDrop") or 1,
            "drop_chance": float(prop(u, "DropChance", 1.0) or 1.0),
            "spawn_impulse": float(prop(u, "SpawnImpulse", 0.0) or 0.0),
            "auto_pickup": bool(prop(u, "bAutoPickup", False)),
            "auto_pickup_range": float(prop(u, "AutoPickupRange", 0.0) or 0.0),
        })

    return {
        "id": short_name_from_path(pkg_path),
        "asset": pkg_path,
        "name": prop_str(export, "ResourceNodeName"),
        "break_effect": prop_object_path(export, "BreakStimulusEffect"),
        "tags_break_cue": prop_tags(export, "BreakCueTag"),
        "tags_resonating_cue": prop_tags(export, "ResonatingCueTag"),
        "contents": contents,
    }


def run_resonatables(provider) -> list[dict]:
    paths = sorted(_walk(provider, "Subnautica2/Content/Data/Resonatable/"))
    out = [r for r in (extract_resonatable(provider, p) for p in paths) if r]
    logger.info("Resonatables: %d extracted", len(out))
    return out


# ---------------- Player characters / customization ----------------

def extract_character(provider, asset_path: str) -> dict | None:
    pkg_path = asset_path[:-7]
    package = safe_load_package(provider, pkg_path)
    if package is None:
        return None
    export = None
    for e in package.GetExports():
        cls = _export_class(e)
        if "PlayerCustomization" in cls or "CustomizationItem" in cls:
            export = e
            break
    if export is None:
        return None
    return {
        "id": short_name_from_path(pkg_path),
        "asset": pkg_path,
        "class": _export_class(export),
        "display_name": prop_str(export, "DisplayName"),
        "display_image": prop_object_path(export, "DisplayImage"),
        "icon": prop_object_path(export, "Icon"),
        "mesh": prop_object_path(export, "MeshAsset"),
        "part_type": prop_enum(export, "PartType"),
        "default_pattern": prop_object_path(export, "DefaultPatternSelection"),
        "display_order": int(prop(export, "DisplayOrder", 0) or 0),
    }


def run_characters(provider) -> list[dict]:
    paths = sorted(_walk(provider, "Subnautica2/Content/Data/Character/"))
    out = [r for r in (extract_character(provider, p) for p in paths) if r]
    logger.info("Characters: %d extracted", len(out))
    return out


# ---------------- Surface spawn data ----------------

def extract_surface_spawn(provider, asset_path: str) -> dict | None:
    pkg_path = asset_path[:-7]
    package = safe_load_package(provider, pkg_path)
    if package is None:
        return None
    export = find_export(package, class_substring="UWESurfaceSpawnData")
    if export is None:
        return None
    return {
        "id": short_name_from_path(pkg_path),
        "asset": pkg_path,
        "tags": prop_tags(export, "TagsToApplyToSpawnPoints"),
    }


def run_surface_spawn(provider) -> list[dict]:
    paths = sorted(_walk(provider, "Subnautica2/Content/Data/SurfaceSpawnData/"))
    out = [r for r in (extract_surface_spawn(provider, p) for p in paths) if r]
    logger.info("SurfaceSpawnData: %d extracted", len(out))
    return out
