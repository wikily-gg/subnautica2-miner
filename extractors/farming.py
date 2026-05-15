"""Extract Subnautica 2 farmable plants + the MetalFarm sibling system.

Subnautica 2 ships its plant cultivation via the `UWEFarming` plugin. The
core building block is `UWESeedGrowerComponent`: a per-slot grower that
ripens after `RipenTime` seconds and then either spawns a `SeedClass` actor
or surfaces a harvestable through the construct recipe's `Output` ItemType.

A "Farmable Plant" is one of eight BPs under
    Subnautica2/Content/Blueprints/BaseBuilding/FarmablePlants/BP_Farmable*
Each is paired with:
  - a DA_*_ItemType (the inventory representation),
  - a DA_*_Recipe   (seed inputs + harvest outputs),
  - a DA_*_ConstructData (builder action; requires
    `AllowedSurfaceTags = BuildableSurface.Planter`).

The Planter is built separately: an "Axum Pot" (`DA_AxumPlanter_ConstructData`)
buildable that provides the surface tag.

The MetalFarm is a sibling system (`BP_MetalFarm` + `BP_Resource_MetalFarmSeed`
+ `DA_MetalFarmMapping`) that uses the same SeedGrower component to grow
14 metals/resources from planted seeds. Tier durations live in C++ and are
not present in the data assets.

Notes:
- `SpawnRate == 0` on every shipped farmable: a slot ripens once and stays
  ripe until harvested. No continuous regrow.
- The wild "cuttable+regrowing" mechanic uses a different component
  (`UWEPlantGrowerComponent`); we surface those as a separate section.
- All seven shipped Construct actions display the placeholder name
  "Axum Pot": the real plant name is on the harvested ItemType.
"""
from __future__ import annotations

import logging

from helpers import (
    _export_class, _extract_soft_path, array_values, find_export,
    obj_ref_path, prop, prop_array, prop_float, prop_int, prop_object_path,
    prop_str, prop_tags, safe_load_package, short_name_from_path,
    unwrap_struct,
)

logger = logging.getLogger(__name__)

FARMABLE_BP_DIR = "Subnautica2/Content/Blueprints/BaseBuilding/FarmablePlants/"

# BP id stem -> DA file stem (Unknown Worlds aren't consistent: BP says
# `CradleShootRoot`, the DA says `CradleShootroot`; BP `CG_CoralWafers`,
# DA `CoralWafer`). One canonical mapping table keeps the resolver simple.
BP_STEM_TO_DA_STEM = {
    "AcidAnemone": "AcidAnemone",
    "CherimoyaRotsac": "CherimoyaRotsac",
    "CradleShootRoot": "CradleShootroot",
    "NecroleiCyst": "NecroleiCyst",
    "CG_CoralWafers": "CoralWafer",
    "CoralPad": "CoralPad",
    "FeelerTree": "FeelerTree",
    "Freesia": "Freesia",
}

METAL_FARM_BP = "Subnautica2/Content/Blueprints/BaseBuilding/BP_MetalFarm"
METAL_FARM_SEED_BP = "Subnautica2/Content/Blueprints/World/ResourceDeposits/BP_Resource_MetalFarmSeed"
METAL_FARM_MAPPING = "Subnautica2/Content/Data/MetalFarmTuning/DA_MetalFarmMapping"
METAL_FARM_CONSTRUCT = "Subnautica2/Content/Data/BaseBuilding/BuilderActions/WorldObjects/DA_MetalFarm_ConstructData"
METAL_FARM_RECIPE = "Subnautica2/Content/Data/CraftingRecipes/Builder/DA_MetalFarm_Recipe"
METAL_FARM_ITEMTYPE = "Subnautica2/Content/Data/ItemType/DA_MetalFarm_ItemType"
PLANTER_CONSTRUCT_CANDIDATES = [
    "Subnautica2/Content/Data/BaseBuilding/BuilderActions/Axum/DA_AxumPlanter_ConstructData",
]


# ---------- generic helpers ----------

def _last_segment(asset_path: str | None) -> str | None:
    if not asset_path:
        return None
    return asset_path.split(".")[0].rsplit("/", 1)[-1]


def _da_pkg_from_path(asset_path: str | None) -> str | None:
    """Convert `/Game/Data/.../DA_X.DA_X` to a provider package path."""
    if not asset_path:
        return None
    base = asset_path.split(".", 1)[0]
    if base.startswith("/Game/"):
        base = "Subnautica2/Content/" + base[len("/Game/"):]
    return base


def _stem_for_bp(bp_id: str) -> str:
    stem = bp_id
    for pref in ("BP_Farmable_", "BP_Farmable"):
        if stem.startswith(pref):
            stem = stem[len(pref):]
            break
    if "Freesia_BasePlant" in bp_id:
        stem = "Freesia"
    return stem


# ---------- SeedGrower / planter components ----------

def _extract_growers(pkg) -> list[dict]:
    out = []
    for ex in pkg.GetExports():
        if _export_class(ex) != "UWESeedGrowerComponent":
            continue
        out.append({
            "slot": str(ex.Name),
            "ripen_time_s": prop_float(ex, "RipenTime") or None,
            "spawn_rate": prop_float(ex, "SpawnRate"),
            "spawn_time_variance_s": prop_float(ex, "SpawnTimeVariance"),
            "starts_grown": bool(prop(ex, "bStartsGrown")),
            "high_priority_spawn": bool(prop(ex, "HighPrioritySpawn", True)),
            "seed_class": prop_object_path(ex, "SeedClass"),
            "show_ripening_progress": bool(prop(ex, "ShowRipeningProgress")),
        })
    out.sort(key=lambda d: d["slot"])
    return out


# ---------- recipe / construct / item type ----------

def _recipe_for_stem(provider, stems: list[str]) -> str | None:
    """Find a recipe asset path for a given DA stem."""
    candidate_dirs = [
        "Subnautica2/Content/Data/CraftingRecipes/Builder/FarmablePlants/",
        "Subnautica2/Content/Blueprints/BaseBuilding/FarmablePlants/",
    ]
    for stem in stems:
        for d in candidate_dirs:
            for name in (
                f"DA_Farmable_{stem}_Recipe",
                f"DA_Farmable{stem}_Recipe",
                f"DA_{stem}_Recipe",
            ):
                p = f"{d}{name}"
                if (p + ".uasset") in provider.Files.Keys:
                    return p
    # Final fallback: case-insensitive substring match
    for stem in stems:
        sl = stem.lower()
        for path in provider.Files.Keys:
            if not path.endswith("_Recipe.uasset"):
                continue
            if "Farm" not in path:
                continue
            if sl in path.lower():
                return path[:-7]
    return None


def _construct_for_stem(provider, stems: list[str]) -> str | None:
    candidate_dirs = [
        "Subnautica2/Content/Data/BaseBuilding/BuilderActions/FarmablePlants/",
        "Subnautica2/Content/Blueprints/BaseBuilding/FarmablePlants/",
    ]
    for stem in stems:
        for d in candidate_dirs:
            for name in (
                f"DA_Farmable_{stem}_ConstructData",
                f"DA_Farmable{stem}_ConstructData",
                f"DA_{stem}_ConstructData",
            ):
                p = f"{d}{name}"
                if (p + ".uasset") in provider.Files.Keys:
                    return p
    for stem in stems:
        sl = stem.lower()
        for path in provider.Files.Keys:
            if not path.endswith("_ConstructData.uasset"):
                continue
            if "FarmablePlants" not in path:
                continue
            if sl in path.lower():
                return path[:-7]
    return None


def _read_recipe(provider, asset_path: str | None) -> dict | None:
    if not asset_path:
        return None
    pkg = safe_load_package(provider, asset_path)
    if pkg is None:
        return None
    ex = find_export(pkg, class_substring="UWECraftingRecipe")
    if ex is None:
        return None
    outputs = []
    for o in array_values(prop_array(ex, "Output")):
        u = unwrap_struct(o)
        if u is None:
            continue
        outputs.append({
            "item_type": prop_object_path(u, "ItemType"),
            "count": prop_int(u, "NumItems") or 1,
        })
    requirements = []
    for r in array_values(prop_array(ex, "Requirements")):
        u = unwrap_struct(r)
        if u is None:
            continue
        requirements.append({
            "item_type": prop_object_path(u, "ItemType"),
            "count": prop_int(u, "NumItems") or 1,
        })
    return {
        "id": short_name_from_path(asset_path),
        "asset": asset_path,
        "name": prop_str(ex, "Name"),
        "description": prop_str(ex, "Description"),
        "crafting_time_seconds": prop_float(ex, "CraftingTime") or None,
        "outputs": outputs,
        "requirements": requirements,
        "category": prop_object_path(ex, "Category"),
        "default_state": str(prop(ex, "DefaultRecipeState") or "") or None,
    }


def _read_construct(provider, asset_path: str | None) -> dict | None:
    if not asset_path:
        return None
    pkg = safe_load_package(provider, asset_path)
    if pkg is None:
        return None
    ex = None
    for e in pkg.GetExports():
        cls = _export_class(e)
        if cls.startswith("SN2Builder") or "BuilderActionData" in cls:
            ex = e
            break
    if ex is None:
        return None
    placement = unwrap_struct(prop(ex, "PlacementParams"))
    surface_tags = prop_tags(placement, "AllowedSurfaceTags") if placement is not None else []
    interact_distance = prop_float(placement, "InteractDistance") if placement is not None else 0.0
    return {
        "id": short_name_from_path(asset_path),
        "asset": asset_path,
        "name": prop_str(ex, "Name"),
        "description": prop_str(ex, "Description"),
        "secondary_description": prop_str(ex, "SecondaryDescription"),
        "category": str(prop(ex, "Category") or "") or None,
        "thumbnail": prop_object_path(ex, "Thumbnail"),
        "custom_ghost": prop_object_path(ex, "CustomGhost"),
        "surface_tags": surface_tags,
        "interact_distance": interact_distance or None,
        "default_unlock_state": str(prop(ex, "DefaultUnlockState") or "") or None,
    }


def _read_item_type(provider, asset_path: str | None) -> dict | None:
    if not asset_path:
        return None
    pkg = safe_load_package(provider, asset_path)
    if pkg is None:
        return None
    ex = find_export(pkg, class_substring="UWEItemType")
    if ex is None:
        return None
    return {
        "id": short_name_from_path(asset_path),
        "asset": asset_path,
        "name": prop_str(ex, "Name"),
        "description": prop_str(ex, "ItemDescription"),
        "actor_class": prop_object_path(ex, "ActorClass"),
        "icon": prop_object_path(ex, "Icon"),
        "thumbnail": prop_object_path(ex, "Thumbnail"),
        "tooltip_icon": prop_object_path(ex, "TooltipIcon"),
    }


# ---------- per-plant extraction ----------

def _walk_farmable_bps(provider) -> list[str]:
    out = []
    for path in provider.Files.Keys:
        if not path.endswith(".uasset"):
            continue
        if not path.startswith(FARMABLE_BP_DIR):
            continue
        name = _last_segment(path) or ""
        if not name.startswith("BP_") or "Ghost" in name or "Customizer" in name:
            continue
        if "Farmable" not in name and "Freesia_BasePlant_Farmable" not in name:
            continue
        out.append(path[:-7])
    return sorted(out)


def _extract_plant(provider, bp_pkg_path: str) -> dict:
    pkg = safe_load_package(provider, bp_pkg_path)
    plant_id = short_name_from_path(bp_pkg_path)
    growers = _extract_growers(pkg) if pkg else []

    stem = _stem_for_bp(plant_id)
    candidates = {stem, BP_STEM_TO_DA_STEM.get(stem, stem)}
    construct_path = _construct_for_stem(provider, sorted(candidates))
    recipe_path = _recipe_for_stem(provider, sorted(candidates))

    construct = _read_construct(provider, construct_path)
    recipe = _read_recipe(provider, recipe_path)

    # The recipe Output[0] points to the harvested ItemType. Resolve it so
    # the wiki has a clean display-name + icon + description per plant.
    harvested_item_path: str | None = None
    seed_item_path: str | None = None
    seed_count: int | None = None
    yield_count: int | None = None
    if recipe:
        if recipe["outputs"]:
            harvested_item_path = recipe["outputs"][0].get("item_type")
            yield_count = recipe["outputs"][0].get("count")
        if recipe["requirements"]:
            seed_item_path = recipe["requirements"][0].get("item_type")
            seed_count = recipe["requirements"][0].get("count")

    harvested_item = _read_item_type(
        provider, _da_pkg_from_path(harvested_item_path)
    )
    seed_item = _read_item_type(
        provider, _da_pkg_from_path(seed_item_path)
    )

    ripen_values = [g["ripen_time_s"] for g in growers if g.get("ripen_time_s")]
    regrows = any((g.get("spawn_rate") or 0) > 0 for g in growers)
    starts_grown = any(g.get("starts_grown") for g in growers)

    return {
        "id": plant_id,
        "bp_asset": bp_pkg_path,
        "display_name": (harvested_item or {}).get("name"),
        "description": (harvested_item or {}).get("description"),
        "icon": (harvested_item or {}).get("icon") or (harvested_item or {}).get("thumbnail"),
        "harvested_item": harvested_item,
        "seed_item": seed_item,
        "recipe": recipe,
        "construct_action": construct,
        "yield": {
            "harvested_item_id": _last_segment(harvested_item_path),
            "harvested_item_path": harvested_item_path,
            "count_per_slot": yield_count,
            "total_slots": len(growers),
        },
        "seed": {
            "input_item_id": _last_segment(seed_item_path),
            "input_item_path": seed_item_path,
            "input_count": seed_count,
        },
        "growth": {
            "grower_slots": len(growers),
            "ripen_times_s": ripen_values,
            "min_ripen_s": min(ripen_values) if ripen_values else None,
            "max_ripen_s": max(ripen_values) if ripen_values else None,
            "starts_grown": starts_grown,
            "regrows": regrows,
            "growers": growers,
        },
    }


# ---------- planter container ----------

def _extract_planter(provider) -> dict:
    out = {
        "surface_tag": "BuildableSurface.Planter",
        "construct_actions": [],
        "exterior_meshes": [],
        "interior_meshes": [],
    }
    for path in PLANTER_CONSTRUCT_CANDIDATES:
        if (path + ".uasset") in provider.Files.Keys:
            c = _read_construct(provider, path)
            if c is not None:
                out["construct_actions"].append(c)
    # Fallback substring search
    if not out["construct_actions"]:
        for path in provider.Files.Keys:
            if not path.endswith(".uasset"):
                continue
            leaf = (_last_segment(path) or "").lower()
            if not leaf.startswith("da_"):
                continue
            if "planter" in leaf or "axumpot" in leaf or "axum_pot" in leaf:
                c = _read_construct(provider, path[:-7])
                if c is not None:
                    out["construct_actions"].append(c)
    # Mesh inventory (handy for the wiki's piece gallery)
    for path in provider.Files.Keys:
        if not path.endswith(".uasset"):
            continue
        leaf = _last_segment(path) or ""
        if "SM_Planter_Exterior" in leaf:
            out["exterior_meshes"].append(path[:-7])
        elif "SM_Small_Planter" in leaf or "SM_Planter_Single" in leaf:
            out["interior_meshes"].append(path[:-7])
    out["exterior_meshes"].sort()
    out["interior_meshes"].sort()
    return out


# ---------- MetalFarm ----------

def _extract_metal_farm(provider) -> dict:
    out: dict = {
        "actor_bp": METAL_FARM_BP,
        "seed_bp": METAL_FARM_SEED_BP,
        "mapping_asset": METAL_FARM_MAPPING,
        "item_type": _read_item_type(provider, METAL_FARM_ITEMTYPE),
        "recipe": _read_recipe(provider, METAL_FARM_RECIPE),
        "construct_action": _read_construct(provider, METAL_FARM_CONSTRUCT),
        "seed_grower": None,
        "mappings": [],
    }
    pkg = safe_load_package(provider, METAL_FARM_BP)
    if pkg is not None:
        growers = _extract_growers(pkg)
        out["seed_grower"] = growers[0] if growers else None
    mp_pkg = safe_load_package(provider, METAL_FARM_MAPPING)
    if mp_pkg is not None:
        for ex in mp_pkg.GetExports():
            m = prop(ex, "ItemTypeToMetalSeed")
            if m is None:
                continue
            props = getattr(m, "Properties", None)
            if props is None:
                continue
            for kv in props:
                key_g = getattr(kv, "Key", None)
                val_g = getattr(kv, "Value", None)
                kg = key_g.GenericValue if key_g is not None else None
                vg = val_g.GenericValue if val_g is not None else None
                key_path = obj_ref_path(kg) or _extract_soft_path(kg)
                u = unwrap_struct(vg)
                if u is None:
                    continue
                res_data = prop(u, "ResonatableData")
                tier = unwrap_struct(prop(u, "MetalTier"))
                tier_tag = None
                if tier is not None:
                    tag_obj = prop(tier, "TagName")
                    if tag_obj is not None:
                        tier_tag = str(tag_obj)
                res_path = obj_ref_path(res_data) if res_data is not None else None
                out["mappings"].append({
                    "input_item_id": _last_segment(key_path),
                    "input_item_path": key_path,
                    "resonatable_path": res_path.split(".", 1)[0] if res_path else None,
                    "resonatable_id": _last_segment(res_path),
                    "tier_tag": tier_tag,
                })
    out["mappings"].sort(key=lambda d: d.get("input_item_id") or "")
    return out


# ---------- wild regrowing plants ----------

def _extract_wild_regrowing(provider) -> list[dict]:
    """Surface BPs that use UWEPlantGrowerComponent (different mechanic).

    These are wild plants in the world that regrow after the player harvests
    a piece. They do not use the player-side farming system but live in the
    same plugin so we publish them on the same page.
    """
    seen: set[str] = set()
    out: list[dict] = []
    for path in provider.Files.Keys:
        if not path.endswith(".uasset"):
            continue
        leaf = _last_segment(path) or ""
        if "CuttableRegrowing" not in leaf and "Regrowing" not in leaf:
            continue
        pkg_path = path[:-7]
        if pkg_path in seen:
            continue
        seen.add(pkg_path)
        pkg = safe_load_package(provider, pkg_path)
        if pkg is None:
            continue
        comps = [
            ex for ex in pkg.GetExports()
            if _export_class(ex) in (
                "UWEPlantGrowerComponent", "UWESeedGrowerComponent",
            )
        ]
        if not comps:
            continue
        out.append({
            "id": short_name_from_path(pkg_path),
            "asset": pkg_path,
            "components": [
                {"class": _export_class(ex), "name": str(ex.Name)}
                for ex in comps
            ],
        })
    out.sort(key=lambda d: d["id"])
    return out


# ---------- entry point ----------

def run(provider) -> dict:
    bps = _walk_farmable_bps(provider)
    logger.info("Farmable BPs: %d candidates", len(bps))
    plants = [_extract_plant(provider, p) for p in bps]
    plants.sort(key=lambda p: (p.get("display_name") or p["id"]).lower())

    planter = _extract_planter(provider)
    metal_farm = _extract_metal_farm(provider)
    wild = _extract_wild_regrowing(provider)

    logger.info(
        "Farming: %d plants, %d planter actions, %d metal mappings, %d wild regrowing",
        len(plants), len(planter["construct_actions"]),
        len(metal_farm["mappings"]), len(wild),
    )

    return {
        "schema_version": 1,
        "plugin": {
            "module": "UWEFarming",
            "uplugin": "Subnautica2/Plugins/UWEFarming/UWEFarming.uplugin",
            "components": {
                "UWESeedGrowerComponent": (
                    "Per-slot grower. Properties: RipenTime (seconds), "
                    "SeedClass (FSoftObjectPath, actor to spawn), SpawnRate, "
                    "SpawnTimeVariance, bStartsGrown, HighPrioritySpawn."
                ),
                "UWESeedGrowerReplicatorComponent": (
                    "Net-replication helper attached alongside each SeedGrower."
                ),
                "UWEPlantGrowerComponent": (
                    "Used by wild regrowing plants. Property data not exposed "
                    "in CDO overrides for this build."
                ),
            },
        },
        "plants": plants,
        "planter": planter,
        "metal_farm": metal_farm,
        "wild_regrowing_plants": wild,
    }
