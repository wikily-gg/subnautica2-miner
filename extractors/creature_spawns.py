"""
Extract real creature spawn locations from the World Population 2 seeded data.

The world-population system stores PCG-generated creature spawns in per-zone
`UWESeededCreatureDataAsset` packages under:
    Subnautica2/Content/Data/WorldPopulation2/SeededData/DA_SeededCreatureData*

Each asset's ``SeededData`` array holds a struct per spawn:

    Transform.Translation  FVector   world XYZ (UE cm)
    Transform.Rotation     FQuat
    Transform.Scale3D      FVector
    CreatureClass          /Game/Blueprints/AI/Agents/.../BP_<Species>.BP_<Species>_C
    ResourceGUID, RuleGUID, ZoneGUID, HandPlaced, bIsPersistent, bAutomaticSpawn

We dump every entry from L_Main (skipping zoo/dev maps) to ``out/creature_spawns.json``.
"""
from __future__ import annotations

import logging
import re

from helpers import (
    _export_class,
    array_values,
    obj_ref_path,
    prop,
    prop_array,
    prop_bool,
    prop_str,
    unwrap_struct,
    vec_to_list,
)

logger = logging.getLogger(__name__)

PREFIX = "Subnautica2/Content/Data/WorldPopulation2/SeededData/"

# Skip zoo / dev / test maps that share the same PCG dataset format.
SKIP_SUBSTRINGS = (
    "GameplayZoo",
    "DevMap",
    "PCG_Zoo",
    "VFX_Test",
    "CollectorLeviathanTest",
)


def _is_creature_file(path: str) -> bool:
    if "DA_SeededCreatureData" not in path:
        return False
    if any(s in path for s in SKIP_SUBSTRINGS):
        return False
    return path.endswith(".uasset")


def _short_class(bp_path: str | None) -> str:
    """Turn ``/Game/.../BP_Halfmoon.BP_Halfmoon_C`` into ``BP_Halfmoon``."""
    if not bp_path:
        return ""
    s = bp_path.rsplit("/", 1)[-1].split("'")[0]
    s = re.sub(r"_C$", "", s)
    # UE asset paths repeat the asset name (PackagePath.AssetName); strip the dup.
    if "." in s:
        a, b = s.split(".", 1)
        if a == b:
            s = a
    return s


def _list_creature_files(provider) -> list[str]:
    return sorted(
        str(f) for f in provider.Files.Keys
        if str(f).startswith(PREFIX) and _is_creature_file(str(f))
    )


def run(provider) -> dict:
    files = _list_creature_files(provider)
    logger.info("CreatureSpawns: scanning %d seeded-data files", len(files))

    spawns: list[dict] = []
    class_counts: dict[str, int] = {}
    zones_seen: set[str] = set()
    skipped_non_main = 0
    failed = 0

    for i, asset_path in enumerate(files, 1):
        pkg_path = asset_path[:-7]
        try:
            ok, package = provider.TryLoadPackage(pkg_path)
        except Exception:
            failed += 1
            continue
        if not ok or package is None:
            failed += 1
            continue

        export = None
        for e in package.GetExports():
            if _export_class(e) == "UWESeededCreatureDataAsset":
                export = e
                break
        if export is None:
            del package
            continue

        map_name = prop_str(export, "MapName")
        if map_name != "L_Main":
            skipped_non_main += 1
            del package
            continue

        arr = prop_array(export, "SeededData")
        for el in array_values(arr):
            u = unwrap_struct(el)
            if u is None:
                continue
            transform = prop(u, "Transform")
            t_struct = unwrap_struct(transform)
            location = None
            if t_struct is not None:
                trans = prop(t_struct, "Translation")
                if trans is not None:
                    location = vec_to_list(trans)
            if location is None:
                continue
            cls_ref = prop(u, "CreatureClass")
            cls_path = obj_ref_path(cls_ref) if cls_ref is not None else None
            short = _short_class(cls_path)
            class_counts[short] = class_counts.get(short, 0) + 1
            zone_guid = prop_str(u, "ZoneGUID")
            rule_guid = prop_str(u, "RuleGUID")
            if zone_guid:
                zones_seen.add(zone_guid)
            spawns.append({
                "class": short,
                "class_path": cls_path,
                "x": location[0],
                "y": location[1],
                "z": location[2],
                "rule_guid": rule_guid,
                "zone_guid": zone_guid,
                "hand_placed": prop_bool(u, "HandPlaced"),
                "persistent": prop_bool(u, "bIsPersistent"),
                "automatic_spawn": prop_bool(u, "bAutomaticSpawn"),
                "spawn_range_multiplier": float(prop(u, "SpawnRangeMultiplier") or 0.0),
                "asset": pkg_path,
            })

        del package
        if i % 50 == 0:
            logger.info("  files %d/%d, spawns: %d, zones: %d",
                        i, len(files), len(spawns), len(zones_seen))

    logger.info(
        "CreatureSpawns done: %d spawns across %d zones (%d non-L_Main skipped, %d failed)",
        len(spawns), len(zones_seen), skipped_non_main, failed,
    )

    return {
        "summary": {
            "spawn_count": len(spawns),
            "zone_count": len(zones_seen),
            "files_scanned": len(files),
            "files_skipped_non_main": skipped_non_main,
            "files_failed": failed,
            "by_class": dict(sorted(class_counts.items(), key=lambda kv: -kv[1])),
        },
        "spawns": spawns,
    }
