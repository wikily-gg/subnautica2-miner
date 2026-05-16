"""Extract PDA databank entries + scan data + story goals + ping data."""
from __future__ import annotations

import logging

from helpers import (
    _export_class, array_values, find_export, obj_ref_path, prop_array,
    prop_enum, prop_int, prop_object_path, prop_str, prop_tags,
    safe_load_package, short_name_from_path, struct_obj_path, unwrap_struct,
    vec_to_list, rot_to_list, prop,
)

logger = logging.getLogger(__name__)


def _walk(provider, prefix: str):
    for path in provider.Files.Keys:
        if path.startswith(prefix) and path.endswith(".uasset"):
            yield path


# ---------------- DatabankEntry ----------------

def extract_databank(provider, asset_path: str) -> dict | None:
    pkg_path = asset_path[:-7]
    package = safe_load_package(provider, pkg_path)
    if package is None:
        return None
    export = find_export(package, class_substring="UWEDatabankEntry")
    if export is None:
        return None

    cats = []
    for el in array_values(prop_array(export, "Categories")):
        from helpers import _coerce_str
        s = _coerce_str(el)
        if s:
            cats.append(s)

    unlocks = []
    for e in package.GetExports():
        if _export_class(e) == "UWERequiredStoryGoalRule":
            unlocks.append({
                "story_goal": obj_ref_path(prop(e, "RequiredStoryGoalRef"))
                              or prop_object_path(e, "RequiredStoryGoalRef"),
                "tag": (prop_tags(e, "RequiredTag") or [None])[0],
            })

    return {
        "id": short_name_from_path(pkg_path),
        "asset": pkg_path,
        "title": prop_str(export, "EntryTitle"),
        "text": prop_str(export, "EntryText"),
        "image": prop_object_path(export, "EntryImage"),
        "categories": cats,
        "unlocking_requirements": unlocks,
    }


def run_databank(provider) -> list[dict]:
    paths = sorted(_walk(provider, "Subnautica2/Content/Data/DatabankEntry/"))
    logger.info("DatabankEntry: %d candidates", len(paths))
    out = []
    for i, p in enumerate(paths, 1):
        e = extract_databank(provider, p)
        if e:
            out.append(e)
        if i % 100 == 0:
            logger.info("  databank: %d / %d", i, len(paths))
    logger.info("DatabankEntry: %d extracted", len(out))
    return out


# ---------------- ScanData ----------------

def extract_scan(provider, asset_path: str) -> dict | None:
    pkg_path = asset_path[:-7]
    package = safe_load_package(provider, pkg_path)
    if package is None:
        return None
    export = find_export(package, class_substring="UWEScanData")
    if export is None:
        return None
    return {
        "id": short_name_from_path(pkg_path),
        "asset": pkg_path,
        "name": prop_str(export, "Name"),
        "description": prop_str(export, "Description"),
        "scan_duration": float(prop(export, "ScanDuration", 0.0) or 0.0),
        "icon": prop_object_path(export, "Icon"),
        # Player-facing fragment count ("Scan N fragments to unlock"). Defaults
        # to 1 on assets that don't override it. For items unlocked by scans
        # (Flashlight, Habitat Builder, …) this is the value the in-game HUD
        # shows as "X / N".
        "num_required": prop_int(export, "NumRequired", 1),
        # Same image the in-game scan UI displays for the fragment.
        "thumbnail": prop_object_path(export, "Thumbnail"),
        "scan_object_type": prop_enum(export, "ScanObjectType") or None,
        "encyclopedia_entry": prop_object_path(export, "EncyclopediaEntry"),
        "databank_entry": prop_object_path(export, "DatabankEntry"),
        "story_goal": prop_object_path(export, "StoryGoal"),
        "story_goals_on_scan": prop_object_path(export, "StoryGoalsToTriggerOnScan"),
        "tags": prop_tags(export, "IdentifierTag"),
    }


def run_scan(provider) -> list[dict]:
    paths = sorted(_walk(provider, "Subnautica2/Content/Data/ScanData/"))
    logger.info("ScanData: %d candidates", len(paths))
    out = []
    for i, p in enumerate(paths, 1):
        e = extract_scan(provider, p)
        if e:
            out.append(e)
        if i % 100 == 0:
            logger.info("  scan: %d / %d", i, len(paths))
    logger.info("ScanData: %d extracted", len(out))
    return out


# ---------------- StoryGoal ----------------

def extract_story(provider, asset_path: str) -> dict | None:
    pkg_path = asset_path[:-7]
    package = safe_load_package(provider, pkg_path)
    if package is None:
        return None
    export = find_export(package, class_substring="UWEStoryGoal")
    if export is None:
        return None
    return {
        "id": short_name_from_path(pkg_path),
        "asset": pkg_path,
        "type": prop_enum(export, "StoryGoalType"),
        "delay": float(prop(export, "Delay", 0.0) or 0.0),
        "tags": prop_tags(export, "IdentifierTag"),
    }


def run_story(provider) -> list[dict]:
    paths = sorted(_walk(provider, "Subnautica2/Content/Data/StoryGoal/"))
    logger.info("StoryGoal: %d candidates", len(paths))
    out = []
    for i, p in enumerate(paths, 1):
        e = extract_story(provider, p)
        if e:
            out.append(e)
        if i % 100 == 0:
            logger.info("  story: %d / %d", i, len(paths))
    logger.info("StoryGoal: %d extracted", len(out))
    return out


# ---------------- Pings ----------------

def extract_ping(provider, asset_path: str) -> dict | None:
    pkg_path = asset_path[:-7]
    package = safe_load_package(provider, pkg_path)
    if package is None:
        return None
    export = find_export(package, class_substring="UWEPingData")
    if export is None:
        return None
    return {
        "id": short_name_from_path(pkg_path),
        "asset": pkg_path,
        "thumbnail": prop_object_path(export, "Thumbnail"),
        "tags": prop_tags(export, "IdentifierTag"),
    }


def run_pings(provider) -> list[dict]:
    paths = sorted(_walk(provider, "Subnautica2/Content/Data/Pings/"))
    out = [r for r in (extract_ping(provider, p) for p in paths) if r]
    logger.info("Pings: %d extracted", len(out))
    return out


# ---------------- GotoLocation (POIs) ----------------

def extract_location(provider, asset_path: str) -> dict | None:
    pkg_path = asset_path[:-7]
    package = safe_load_package(provider, pkg_path)
    if package is None:
        return None
    export = None
    for e in package.GetExports():
        if _export_class(e) == "SN2GotoLocation":
            export = e
            break
    if export is None:
        return None
    return {
        "id": short_name_from_path(pkg_path),
        "asset": pkg_path,
        "name": prop_str(export, "Name"),
        "location": vec_to_list(prop(export, "Location")),
        "rotation": rot_to_list(prop(export, "Rotation")),
        "image": prop_object_path(export, "Image"),
    }


def run_locations(provider) -> list[dict]:
    paths = sorted(_walk(provider, "Subnautica2/Content/Data/GotoLocations/"))
    out = [r for r in (extract_location(provider, p) for p in paths) if r]
    logger.info("GotoLocations: %d extracted", len(out))
    return out
