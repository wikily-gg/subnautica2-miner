"""Extract actor placements from the L_Main world partition cells.

Sweeps every .umap under Maps/Main/L_Main/_Generated_, follows each actor's
RootComponent to read its world position, and emits filtered placements:
resources, creatures, POIs, volumes, lootable crates, lifepods, etc.
"""
from __future__ import annotations

import logging
import re

from helpers import _export_class, prop, vec_to_list

logger = logging.getLogger(__name__)

CELL_PREFIX = "Subnautica2/Content/Maps/Main/L_Main/_Generated_/"

# Classes we always keep (substring match, case-sensitive on class name).
KEEP_SUBSTRINGS = (
    "WorldPopSpawned",         # resource spawns (Titanium, Copper, etc.)
    "ResourceNode",
    "ResourceDeposit",
    "_Crate",                  # loot crates / blockout crates
    "Lifepod",
    "AlterraCrate",
    "PrefabActor",             # bundled POI prefabs
    "UWEVolumeActor",
    "TemperatureRegionVolume", # biome temperature
    "TempCheckVolume",
    "BiomeVolume",
    "Spawner",
    "SpawnPoint",
    "Salvage",
    "Wreck",
    "Databank",                # placed PDA pickups
    "ScanData",
    "Pickup",
    "_Egg_",
    "_Egg.",
    "Tadpole",                 # vehicle / lifepod-ish
    "BP_LightStick",
    "Beacon",
    "Plinth",                  # narrative props
    "Terminal",
    "AudioLog",
    "Audiolog",
    "AbandonedBase",
)

# Classes that we drop outright (low signal, high volume).
DROP_EXACT = {
    "Level", "Model", "NavigationSystemModuleConfig", "World", "WorldSettings",
    "StaticMeshActor", "DecalActor", "InstancedFoliageActor",
    "Brush", "BrushComponent",
}

# Drop class names matching any of these substrings (decorative).
DROP_SUBSTRINGS = (
    "BP_CG_",            # coral gardens decoration
    "BP_CoralPad",
    "BP_CoralSea",
    "BP_CoralCabbage",
    "BP_PlantPaddle",
    "BP_FeelerTree",
    "BP_GlowLettuce",
    "BP_HangingVine",
    "BP_BaseModule_",    # abandoned base wall/floor pieces
    "BP_BaseRoom_",
    "BP_BO_",            # blockout art props — too noisy
    "_GENERATED_",
    "_GEN_VARIABLE",
)

# Always-keep creature blueprints — populated from archetype data if present.
def _build_creature_class_set() -> set[str]:
    """Build a set of expected creature blueprint short-names from gameplay tags."""
    # We use a regex approach instead, since creature blueprint names follow the
    # convention BP_<Name>_C and are identified by being mentioned in our
    # extracted archetype data.  Keeping this set empty is fine because the
    # KEEP_SUBSTRINGS already covers WorldPopSpawned proxies which is how
    # spawned creatures are placed in the world.
    return set()


CREATURE_BP_RE = re.compile(r"^(BP_|BPC_).*(?:Crab|Leviathan|Anemone|Slug|Eel|Shark|Worm|Fish|Whale|"
                            r"Geordie|Halfmoon|Bullethead|Cerathecan|Pneumo|Tadpole|Spinney|"
                            r"FourEyes|Clamthulu|WaterSlug|AcidAnemone|Boid|FishSchool)", re.I)

# Cave-prefab actors (BP_BO_*Cave*, BP_*Cavern*, etc.) override DROP_SUBSTRINGS
# but exclude flora props (CavePlantTendrils) and VFX triggers.
CAVE_KEEP_RE = re.compile(r"(?<!Plant)(Cave|Cavern|Tunnel|Grotto)", re.I)
CAVE_DROP_RE = re.compile(r"CavePlant|VFXTrigger|Decal|Foliage", re.I)


def _is_cave_class(class_name: str) -> bool:
    return bool(CAVE_KEEP_RE.search(class_name)) and not bool(CAVE_DROP_RE.search(class_name))


def _is_interesting(class_name: str) -> bool:
    if class_name in DROP_EXACT:
        return False
    if _is_cave_class(class_name):
        return True
    if any(d in class_name for d in DROP_SUBSTRINGS):
        # Allow creatures even if they also match a drop pattern
        if CREATURE_BP_RE.search(class_name):
            return True
        return False
    if any(k in class_name for k in KEEP_SUBSTRINGS):
        return True
    if CREATURE_BP_RE.search(class_name):
        return True
    return False


def _component_from_package_index(rc):
    """Resolve an FPackageIndex (UObject ref) to its UObject.

    CUE4Parse exposes several access paths; we try them in order.
    """
    if rc is None:
        return None
    # 1) FPackageIndex.Load() returns the UObject (synchronous)
    try:
        obj = rc.Load()
        if obj is not None:
            return obj
    except Exception:
        pass
    # 2) ResolvedObject.Object.Value (lazy)
    resolved = getattr(rc, "ResolvedObject", None)
    if resolved is not None:
        try:
            obj_val = resolved.Object
            if obj_val is not None:
                # Lazy<T> wrapper
                val = getattr(obj_val, "Value", None)
                if val is not None:
                    return val
        except Exception:
            pass
    return None


def _resolve_root_location(actor) -> tuple[list[float] | None, list[float] | None]:
    """Return ``(world_location, rotation)`` for *actor* by following RootComponent."""
    rc = prop(actor, "RootComponent")
    obj = _component_from_package_index(rc)
    if obj is None:
        return None, None
    loc = vec_to_list(prop(obj, "RelativeLocation"))
    rot_val = prop(obj, "RelativeRotation")
    rot = None
    if rot_val is not None:
        try:
            rot = [float(rot_val.Pitch), float(rot_val.Yaw), float(rot_val.Roll)]
        except Exception:
            rot = None
    return loc, rot


def _list_cells(provider) -> list[str]:
    return sorted(p for p in provider.Files.Keys
                  if p.startswith(CELL_PREFIX) and p.endswith(".umap"))


def run(provider, max_cells: int | None = None) -> dict:
    cells = _list_cells(provider)
    if max_cells:
        cells = cells[:max_cells]
    logger.info("World map: scanning %d cells", len(cells))

    placements: list[dict] = []
    class_counts: dict[str, int] = {}
    failed = 0

    for i, cell_path in enumerate(cells, 1):
        try:
            ok, package = provider.TryLoadPackage(cell_path[:-5])
        except Exception:
            failed += 1
            continue
        if not ok or package is None:
            failed += 1
            continue

        cell_id = cell_path.rsplit("/", 1)[-1].replace(".umap", "")
        for export in package.GetExports():
            cls = _export_class(export)
            if not _is_interesting(cls):
                continue
            loc, rot = _resolve_root_location(export)
            if loc is None:
                # Try the export itself (PCG actors, etc.)
                loc = vec_to_list(prop(export, "RootLocation")) or vec_to_list(prop(export, "Location"))
            placements.append({
                "class": cls,
                "name": str(export.Name),
                "cell": cell_id,
                "x": loc[0] if loc else None,
                "y": loc[1] if loc else None,
                "z": loc[2] if loc else None,
                "rot": rot,
            })
            class_counts[cls] = class_counts.get(cls, 0) + 1

        if i % 200 == 0:
            logger.info("  cells %d/%d, placements: %d", i, len(cells), len(placements))
        # Free package
        del package

    logger.info("World map: %d placements across %d cells (%d failed)",
                len(placements), len(cells), failed)

    # Bucket placements by category for easy wiki use
    buckets: dict[str, list[dict]] = {
        "resources": [],
        "creatures": [],
        "pois": [],
        "caves": [],
        "volumes": [],
        "loot": [],
        "other": [],
    }
    for p in placements:
        cls = p["class"]
        if _is_cave_class(cls):
            buckets["caves"].append(p)
        elif "Resource" in cls or "WorldPopSpawned" in cls:
            buckets["resources"].append(p)
        elif CREATURE_BP_RE.search(cls):
            buckets["creatures"].append(p)
        elif "Volume" in cls:
            buckets["volumes"].append(p)
        elif "Crate" in cls or "Pickup" in cls or "Salvage" in cls or "Wreck" in cls:
            buckets["loot"].append(p)
        elif any(k in cls for k in ("PrefabActor", "Beacon", "Terminal", "AudioLog",
                                    "Audiolog", "Lifepod", "AbandonedBase", "LightStick",
                                    "Plinth", "Databank", "ScanData")):
            buckets["pois"].append(p)
        else:
            buckets["other"].append(p)

    return {
        "summary": {
            "total_placements": len(placements),
            "cells_scanned": len(cells),
            "cells_failed": failed,
            "by_class": dict(sorted(class_counts.items(), key=lambda x: -x[1])),
            "by_category": {k: len(v) for k, v in buckets.items()},
        },
        "placements": buckets,
    }


# ---------------- world population rules ----------------

def _walk(provider, prefix: str):
    for p in provider.Files.Keys:
        if p.startswith(prefix) and p.endswith(".uasset"):
            yield p


def extract_pop_settings(provider, asset_path: str) -> dict | None:
    from helpers import prop_array, prop_int, prop_object_path, safe_load_package, short_name_from_path, array_values
    pkg_path = asset_path[:-7]
    package = safe_load_package(provider, pkg_path)
    if package is None:
        return None
    export = None
    for e in package.GetExports():
        et = _export_class(e)
        if et == "UWEWorldPopCreaturePopulationDA" or "PopulationDA" in et:
            export = e
            break
    if export is None:
        return None
    classes = []
    for el in array_values(prop_array(export, "CreatureClasses")):
        from helpers import _extract_soft_path
        cp = _extract_soft_path(el)
        if cp:
            classes.append(cp)
    return {
        "id": short_name_from_path(pkg_path),
        "asset": pkg_path,
        "creature_classes": classes,
        "max_population": prop_int(export, "MaxPopulationControl"),
    }


def run_pop_settings(provider) -> list[dict]:
    paths = sorted(_walk(provider, "Subnautica2/Content/Data/WorldPopulation2/PopSettings/"))
    out = [r for r in (extract_pop_settings(provider, p) for p in paths) if r]
    logger.info("PopSettings: %d extracted", len(out))
    return out


def extract_region_loot(provider, asset_path: str) -> dict | None:
    from helpers import prop_object_path, safe_load_package, short_name_from_path
    pkg_path = asset_path[:-7]
    package = safe_load_package(provider, pkg_path)
    if package is None:
        return None
    export = None
    for e in package.GetExports():
        if "RegionLootDataAsset" in _export_class(e):
            export = e
            break
    if export is None:
        return None
    return {
        "id": short_name_from_path(pkg_path),
        "asset": pkg_path,
        "default_resource": prop_object_path(export, "DefaultResourceClass"),
    }


def run_region_loot(provider) -> list[dict]:
    paths = sorted(_walk(provider, "Subnautica2/Content/Data/WorldPopulation2/Loot/"))
    out = [r for r in (extract_region_loot(provider, p) for p in paths) if r]
    logger.info("RegionLoot: %d extracted", len(out))
    return out


def extract_landscape_mapping(provider, asset_path: str) -> dict | None:
    from helpers import array_values, prop_array, safe_load_package, short_name_from_path, _extract_soft_path
    pkg_path = asset_path[:-7]
    package = safe_load_package(provider, pkg_path)
    if package is None:
        return None
    export = None
    for e in package.GetExports():
        if "MaterialMapping" in _export_class(e) or "LandscapeMapping" in _export_class(e):
            export = e
            break
    if export is None:
        return None
    mats = []
    for el in array_values(prop_array(export, "AllowedMaterials")):
        sp = _extract_soft_path(el)
        if sp:
            mats.append(sp)
    return {
        "id": short_name_from_path(pkg_path),
        "asset": pkg_path,
        "allowed_materials": mats,
    }


def run_landscape(provider) -> list[dict]:
    paths = sorted(_walk(provider, "Subnautica2/Content/Data/WorldPopulation2/LandscapeMappings/"))
    out = [r for r in (extract_landscape_mapping(provider, p) for p in paths) if r]
    logger.info("LandscapeMappings: %d extracted", len(out))
    return out
