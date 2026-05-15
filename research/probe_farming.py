"""Probe Subnautica 2 farming mechanics.

Outputs:
  out/research/farming.json   -- structured per-plant data + metal farm + planter
  out/research/farming.md     -- human-readable summary

Subnautica 2 uses a UWEFarming plugin that exposes UWESeedGrowerComponent
(per-slot grower) and UWEPlantGrowerComponent (wild regrowing plant).
Farmable buildables live under
  /Game/Blueprints/BaseBuilding/FarmablePlants/BP_Farmable*
with paired
  /Game/Data/ItemType/FarmablePlants/DA_Farmable_*_ItemType
  /Game/Data/CraftingRecipes/Builder/FarmablePlants/DA_Farmable_*_Recipe
  /Game/Data/BaseBuilding/BuilderActions/FarmablePlants/DA_Farmable_*_ConstructData

Two farmables (NecroleiCyst, CradleShootroot via NecroleiCyst dir) keep
their DAs alongside the BPs instead.

The MetalFarm is a sibling system that grows a planted resource node
(e.g., Titanium / Copper) using the same UWESeedGrowerComponent class
but configured by DA_MetalFarmMapping (ItemType -> ResonatableData).

Run from D:\\subnautica\\miner:
    python research/probe_farming.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.WARNING)

# Allow running from research/ subdir
MINER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MINER_DIR))

from provider import create_provider  # noqa: E402
from helpers import (  # noqa: E402
    _export_class, _extract_soft_path,
    array_values, find_export, obj_ref_path,
    prop, prop_array, prop_float, prop_int, prop_object_path, prop_str,
    prop_tags, safe_load_package, short_name_from_path, unwrap_struct,
)

OUT_DIR = MINER_DIR / "out" / "research"
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ----- helpers -----------------------------------------------------

def _last_segment(asset_path: str | None) -> str | None:
    if not asset_path:
        return None
    return asset_path.split(".")[0].rsplit("/", 1)[-1]


def _short(path: str | None) -> str | None:
    """Trim '/Game/.../X.X' to '/Game/.../X' (strip class suffix)."""
    if not path:
        return None
    return path.split(".", 1)[0]


def _load(prov, pkg_path: str):
    return safe_load_package(prov, pkg_path)


def _bp_pkg_from_actor(actor_class: str | None) -> str | None:
    if not actor_class:
        return None
    # /Game/Blueprints/.../BP_X.BP_X_C  -> Subnautica2/Content/Blueprints/.../BP_X
    base = actor_class.split(".", 1)[0]  # /Game/Blueprints/.../BP_X
    if base.startswith("/Game/"):
        base = "Subnautica2/Content/" + base[len("/Game/"):]
    return base


def _da_pkg_from_path(asset_path: str | None) -> str | None:
    """Convert '/Game/Data/.../DA_X.DA_X' to a provider package path."""
    if not asset_path:
        return None
    base = asset_path.split(".", 1)[0]
    if base.startswith("/Game/"):
        base = "Subnautica2/Content/" + base[len("/Game/"):]
    return base


# ----- farmable plant extraction ----------------------------------

FARMABLE_BP_DIRS = (
    "Subnautica2/Content/Blueprints/BaseBuilding/FarmablePlants/",
)


def find_farmable_bps(prov) -> list[str]:
    out = []
    for path in prov.Files.Keys:
        if not path.endswith(".uasset"):
            continue
        if not path.startswith(FARMABLE_BP_DIRS):
            continue
        name = _last_segment(path) or ""
        # Top-level Farmable BPs only; ghosts/customizers go into a separate field
        if not name.startswith("BP_") or "Ghost" in name or "Customizer" in name:
            continue
        if "Farmable" not in name and "Freesia_BasePlant_Farmable" not in name:
            continue
        out.append(path[:-7])
    return sorted(out)


def extract_growers(pkg) -> list[dict]:
    """Read every UWESeedGrowerComponent template inside a Farmable BP."""
    out = []
    for ex in pkg.GetExports():
        if _export_class(ex) != "UWESeedGrowerComponent":
            continue
        out.append({
            "slot": str(ex.Name),
            "ripen_time_s": prop_float(ex, "RipenTime"),
            "spawn_rate": prop_float(ex, "SpawnRate"),
            "spawn_time_variance_s": prop_float(ex, "SpawnTimeVariance"),
            "starts_grown": bool(prop(ex, "bStartsGrown")),
            "high_priority_spawn": bool(prop(ex, "HighPrioritySpawn", True)),
            "seed_class": prop_object_path(ex, "SeedClass"),
            "show_ripening_progress": bool(prop(ex, "ShowRipeningProgress")),
        })
    # Stable order
    out.sort(key=lambda d: d["slot"])
    return out


_PLANT_ID_TO_DA_ID = {
    # BP id stem -> matching DA file id stem (handles inconsistent suffixes)
    "AcidAnemone": "AcidAnemone",
    "CherimoyaRotsac": "CherimoyaRotsac",
    "CradleShootRoot": "CradleShootroot",  # capital R differs across BP/DA
    "NecroleiCyst": "NecroleiCyst",
    "CG_CoralWafers": "CoralWafer",        # plural-vs-singular mismatch
    "CoralPad": "CoralPad",
    "FeelerTree": "FeelerTree",
    "Freesia": "Freesia",
}


def _da_stem_for_plant(bp_id: str) -> list[str]:
    """Map a Farmable BP id to one or more candidate DA stems."""
    # Strip prefixes
    stem = bp_id
    for pref in ("BP_Farmable_", "BP_Farmable"):
        if stem.startswith(pref):
            stem = stem[len(pref):]
            break
    if "Freesia_BasePlant" in bp_id:
        stem = "Freesia"
    # Build candidates: raw stem, plus known mapping
    candidates = {stem}
    if stem in _PLANT_ID_TO_DA_ID:
        candidates.add(_PLANT_ID_TO_DA_ID[stem])
    # Also try lowercasing the trailing letter-case difference
    return list(candidates)


def find_construct_data(prov, bp_id: str) -> str | None:
    """Return a ConstructData package path for a given Farmable BP id."""
    for stem in _da_stem_for_plant(bp_id):
        candidates = [
            f"Subnautica2/Content/Data/BaseBuilding/BuilderActions/FarmablePlants/DA_Farmable_{stem}_ConstructData",
            f"Subnautica2/Content/Data/BaseBuilding/BuilderActions/FarmablePlants/DA_{stem}_ConstructData",
            f"Subnautica2/Content/Blueprints/BaseBuilding/FarmablePlants/DA_{stem}_ConstructData",
            f"Subnautica2/Content/Blueprints/BaseBuilding/FarmablePlants/DA_Farmable_{stem}_ConstructData",
        ]
        for c in candidates:
            if (c + ".uasset") in prov.Files.Keys:
                return c
    # Case-insensitive scan as final fallback
    for stem in _da_stem_for_plant(bp_id):
        sl = stem.lower()
        for path in prov.Files.Keys:
            if not path.endswith("_ConstructData.uasset"):
                continue
            if "FarmablePlants" not in path:
                continue
            if sl in path.lower():
                return path[:-7]
    return None


def find_recipe(prov, bp_id: str) -> str | None:
    for stem in _da_stem_for_plant(bp_id):
        candidates = [
            f"Subnautica2/Content/Data/CraftingRecipes/Builder/FarmablePlants/DA_Farmable_{stem}_Recipe",
            f"Subnautica2/Content/Data/CraftingRecipes/Builder/FarmablePlants/DA_{stem}_Recipe",
            f"Subnautica2/Content/Blueprints/BaseBuilding/FarmablePlants/DA_{stem}_Recipe",
            f"Subnautica2/Content/Blueprints/BaseBuilding/FarmablePlants/DA_Farmable_{stem}_Recipe",
        ]
        for c in candidates:
            if (c + ".uasset") in prov.Files.Keys:
                return c
    for stem in _da_stem_for_plant(bp_id):
        sl = stem.lower()
        for path in prov.Files.Keys:
            if not path.endswith("_Recipe.uasset"):
                continue
            if "Farm" not in path:
                continue
            if sl in path.lower():
                return path[:-7]
    return None


def extract_construct_data(prov, asset_path: str | None) -> dict | None:
    if not asset_path:
        return None
    pkg = _load(prov, asset_path)
    if pkg is None:
        return None
    for ex in pkg.GetExports():
        if _export_class(ex) != "SN2BuilderConstructActionData":
            continue
        pp = prop(ex, "PlacementParams")
        u = unwrap_struct(pp)
        surface_tags = prop_tags(u, "AllowedSurfaceTags") if u else []
        return {
            "asset": asset_path,
            "name": prop_str(ex, "Name"),
            "description": prop_str(ex, "Description"),
            "category": str(prop(ex, "Category") or ""),
            "thumbnail": prop_object_path(ex, "Thumbnail"),
            "custom_ghost": prop_object_path(ex, "CustomGhost"),
            "surface_tags": surface_tags,
            "interact_distance": prop_float(u, "InteractDistance") if u else 0.0,
            "default_unlock_state": str(prop(ex, "DefaultUnlockState") or ""),
        }
    return None


def extract_recipe(prov, asset_path: str | None) -> dict | None:
    if not asset_path:
        return None
    pkg = _load(prov, asset_path)
    if pkg is None:
        return None
    for ex in pkg.GetExports():
        if _export_class(ex) != "UWECraftingRecipe":
            continue
        outs = []
        for o in array_values(prop_array(ex, "Output")):
            u = unwrap_struct(o)
            if u is None:
                continue
            outs.append({
                "item_type": prop_object_path(u, "ItemType"),
                "count": prop_int(u, "NumItems") or 1,
            })
        reqs = []
        for r in array_values(prop_array(ex, "Requirements")):
            u = unwrap_struct(r)
            if u is None:
                continue
            reqs.append({
                "item_type": prop_object_path(u, "ItemType"),
                "count": prop_int(u, "NumItems") or 1,
            })
        return {
            "asset": asset_path,
            "name": prop_str(ex, "Name"),
            "description": prop_str(ex, "Description"),
            "thumbnail": prop_object_path(ex, "Thumbnail"),
            "outputs": outs,
            "requirements": reqs,
            "default_state": str(prop(ex, "DefaultRecipeState") or ""),
            "category": prop_object_path(ex, "Category"),
        }
    return None


def extract_item_type(prov, asset_path: str | None) -> dict | None:
    if not asset_path:
        return None
    pkg = _load(prov, asset_path)
    if pkg is None:
        return None
    for ex in pkg.GetExports():
        if _export_class(ex) != "UWEItemType":
            continue
        return {
            "asset": asset_path,
            "name": prop_str(ex, "Name"),
            "description": prop_str(ex, "ItemDescription"),
            "actor_class": prop_object_path(ex, "ActorClass"),
            "icon": prop_object_path(ex, "Icon"),
            "thumbnail": prop_object_path(ex, "Thumbnail"),
            "tooltip_icon": prop_object_path(ex, "TooltipIcon"),
            "tags": prop_tags(ex, "IdentifierTag"),
        }
    return None


def extract_plant(prov, bp_pkg_path: str, items_index: dict) -> dict:
    pkg = _load(prov, bp_pkg_path)
    plant_id = short_name_from_path(bp_pkg_path)
    growers = extract_growers(pkg) if pkg else []

    # Map BP id -> DA stem(s) and locate ConstructData / Recipe.
    construct_path = find_construct_data(prov, plant_id)
    recipe_path = find_recipe(prov, plant_id)
    construct = extract_construct_data(prov, construct_path)
    recipe = extract_recipe(prov, recipe_path)

    # Resolve harvested item type from recipe outputs
    harvested_item = None
    if recipe and recipe["outputs"]:
        ito = recipe["outputs"][0]["item_type"]
        # The recipe output IS the Farmable_*_ItemType (the buildable itself)
        # which holds the ActorClass back to this BP. Confirm.
        harvested_item = extract_item_type(prov, _da_pkg_from_path(ito))

    # Seed required is recipe.requirements[0]
    seed_item = None
    seed_count = None
    if recipe and recipe["requirements"]:
        seed_path = recipe["requirements"][0]["item_type"]
        seed_count = recipe["requirements"][0]["count"]
        seed_item = items_index.get(_last_segment(seed_path))

    # Per-plant aggregate stats
    slots = len(growers)
    rt_values = [g["ripen_time_s"] for g in growers if g["ripen_time_s"] > 0]
    starts_grown = any(g["starts_grown"] for g in growers)
    # Regrow detection: SpawnRate > 0 means continuous spawning (regrow).
    # Otherwise grow once per slot.
    regrows = any(g["spawn_rate"] > 0 for g in growers)

    return {
        "id": plant_id,
        "bp_asset": bp_pkg_path,
        "display_name": (harvested_item or {}).get("name") if harvested_item else None,
        "item_type_asset": (harvested_item or {}).get("asset"),
        "actor_class": (harvested_item or {}).get("actor_class"),
        "description": (harvested_item or {}).get("description"),
        "icon": (harvested_item or {}).get("icon") or (harvested_item or {}).get("thumbnail"),
        "construct_action": construct,
        "recipe": recipe,
        "seed": {
            "input_item_id": _last_segment((recipe["requirements"][0]["item_type"]
                                            if recipe and recipe["requirements"] else None)),
            "input_item_path": (recipe["requirements"][0]["item_type"]
                                if recipe and recipe["requirements"] else None),
            "input_count": seed_count,
            "input_item_name": (seed_item or {}).get("name") if seed_item else None,
            "input_item_description": (seed_item or {}).get("description") if seed_item else None,
        },
        "yield": {
            "harvested_item_id": _last_segment((recipe["outputs"][0]["item_type"]
                                                 if recipe and recipe["outputs"] else None)),
            "harvested_item_path": (recipe["outputs"][0]["item_type"]
                                     if recipe and recipe["outputs"] else None),
            "count_per_slot": (recipe["outputs"][0]["count"]
                                if recipe and recipe["outputs"] else None),
            "total_slots": slots,
        },
        "growth": {
            "grower_slots": slots,
            "ripen_times_s": rt_values,
            "min_ripen_s": min(rt_values) if rt_values else None,
            "max_ripen_s": max(rt_values) if rt_values else None,
            "starts_grown": starts_grown,
            "regrows": regrows,
            "growers": growers,
        },
    }


# ----- planter / container ----------------------------------------

def extract_planter_pieces(prov) -> dict:
    """Identify the buildable that exposes BuildableSurface.Planter."""
    out = {
        "surface_tag": "BuildableSurface.Planter",
        "buildable_actions": [],
        "exterior_planter_meshes": [],
        "interior_planter_meshes": [],
    }
    # Look for BuilderConstruct actions on planter pieces
    planter_dirs = [
        "Subnautica2/Content/Data/BaseBuilding/BuilderActions/Axum/",
        "Subnautica2/Content/Data/BaseBuilding/BuilderActions/Buildables/",
    ]
    interesting_actions = []
    for path in prov.Files.Keys:
        if not path.endswith(".uasset"):
            continue
        leaf = _last_segment(path) or ""
        if not leaf.lower().startswith("da_"):
            continue
        if "planter" not in leaf.lower() and "axumpot" not in leaf.lower() and "axum_pot" not in leaf.lower():
            continue
        interesting_actions.append(path[:-7])
    for ap in sorted(set(interesting_actions)):
        pkg = _load(prov, ap)
        if pkg is None:
            continue
        for ex in pkg.GetExports():
            cls = _export_class(ex)
            if not cls.startswith("SN2Builder"):
                continue
            pp = prop(ex, "PlacementParams")
            u = unwrap_struct(pp)
            surface_tags = prop_tags(u, "AllowedSurfaceTags") if u else []
            out["buildable_actions"].append({
                "asset": ap,
                "action_class": cls,
                "name": prop_str(ex, "Name"),
                "description": prop_str(ex, "Description"),
                "surface_tags": surface_tags,
                "category": str(prop(ex, "Category") or ""),
            })

    for path in prov.Files.Keys:
        if not path.endswith(".uasset"):
            continue
        leaf = _last_segment(path) or ""
        if "SM_Planter_Exterior" in leaf:
            out["exterior_planter_meshes"].append(path[:-7])
        elif "SM_Small_Planter" in leaf or "SM_Planter_Single" in leaf:
            out["interior_planter_meshes"].append(path[:-7])
    out["exterior_planter_meshes"].sort()
    out["interior_planter_meshes"].sort()
    return out


# ----- metal farm --------------------------------------------------

def extract_metal_farm(prov) -> dict:
    out = {
        "construct_action": None,
        "recipe": None,
        "item_type": None,
        "actor_bp": "Subnautica2/Content/Blueprints/BaseBuilding/BP_MetalFarm",
        "seed_bp": "Subnautica2/Content/Blueprints/World/ResourceDeposits/BP_Resource_MetalFarmSeed",
        "seed_mapping_asset": "Subnautica2/Content/Data/MetalFarmTuning/DA_MetalFarmMapping",
        "seed_grower": None,
        "seed_mappings": [],
    }
    out["construct_action"] = extract_construct_data(
        prov, "Subnautica2/Content/Data/BaseBuilding/BuilderActions/WorldObjects/DA_MetalFarm_ConstructData"
    )
    out["recipe"] = extract_recipe(
        prov, "Subnautica2/Content/Data/CraftingRecipes/Builder/DA_MetalFarm_Recipe"
    )
    out["item_type"] = extract_item_type(
        prov, "Subnautica2/Content/Data/ItemType/DA_MetalFarm_ItemType"
    )
    pkg = _load(prov, out["actor_bp"])
    if pkg is not None:
        growers = extract_growers(pkg)
        out["seed_grower"] = growers[0] if growers else None

    mp_pkg = _load(prov, out["seed_mapping_asset"])
    if mp_pkg is not None:
        for ex in mp_pkg.GetExports():
            m = prop(ex, "ItemTypeToMetalSeed")
            if m is None:
                continue
            for kv in m.Properties:
                k = kv.Key.GenericValue
                v = kv.Value.GenericValue
                key_path = obj_ref_path(k) or _extract_soft_path(k)
                u = unwrap_struct(v)
                if u is None:
                    continue
                res_data = prop(u, "ResonatableData")
                tier = prop(u, "MetalTier")
                tu = unwrap_struct(tier)
                tier_tag = None
                if tu is not None:
                    tag_obj = prop(tu, "TagName")
                    if tag_obj is not None:
                        tier_tag = str(tag_obj)
                out["seed_mappings"].append({
                    "input_item_id": _last_segment(key_path),
                    "input_item_path": key_path,
                    "resonatable_data": _short(obj_ref_path(res_data) or "") if res_data is not None else None,
                    "resonatable_id": _last_segment(obj_ref_path(res_data) or "") if res_data is not None else None,
                    "tier_tag": tier_tag,
                })
    out["seed_mappings"].sort(key=lambda d: d.get("input_item_id") or "")
    return out


# ----- wild regrowing plants --------------------------------------

def extract_wild_regrowing(prov) -> dict:
    """Find any BP using UWEPlantGrowerComponent (different mechanic).

    SN2 has at least one wild-world cuttable+regrowing plant (Freesia).
    These do NOT use SeedGrower slots, instead use UWEPlantGrower for an
    auto-regrowing world plant.
    """
    out = {"plants": []}
    candidates = []
    for path in prov.Files.Keys:
        if not path.endswith(".uasset"):
            continue
        leaf = _last_segment(path) or ""
        if "CuttableRegrowing" in leaf or "Regrowing" in leaf:
            candidates.append(path[:-7])
    for cp in sorted(set(candidates)):
        pkg = _load(prov, cp)
        if pkg is None:
            continue
        comps = [ex for ex in pkg.GetExports()
                 if _export_class(ex) in ("UWEPlantGrowerComponent",
                                          "UWESeedGrowerComponent")]
        if not comps:
            continue
        out["plants"].append({
            "asset": cp,
            "components": [
                {"class": _export_class(ex), "name": str(ex.Name)}
                for ex in comps
            ],
        })
    return out


# ----- main --------------------------------------------------------

def build_item_index(items_json_path: Path) -> dict:
    try:
        items = json.loads(items_json_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {it["id"]: it for it in items}


def main():
    prov = create_provider()

    items_index = build_item_index(MINER_DIR / "out" / "items.json")

    plants = []
    for bp_path in find_farmable_bps(prov):
        plants.append(extract_plant(prov, bp_path, items_index))

    planter = extract_planter_pieces(prov)
    metal_farm = extract_metal_farm(prov)
    wild = extract_wild_regrowing(prov)

    # UWEFarming plugin module signature
    uwe_farming = {
        "uplugin": "Subnautica2/Plugins/UWEFarming/UWEFarming.uplugin",
        "config_ini": "Subnautica2/Plugins/UWEFarming/Config/DefaultUWEFarming.ini",
        "components": {
            "UWESeedGrowerComponent": (
                "Per-slot seed-spawn component. Properties: RipenTime "
                "(seconds), SeedClass (FSoftObjectPath, actor to spawn), "
                "SpawnRate, SpawnTimeVariance, bStartsGrown, HighPrioritySpawn."
            ),
            "UWESeedGrowerReplicatorComponent": (
                "Net-replication helper attached alongside each SeedGrower."
            ),
            "UWEPlantGrowerComponent": (
                "Used by wild regrowing plants (e.g. Freesia_CuttableRegrowing). "
                "Property data not exposed in CDO overrides for this build."
            ),
        },
        "enum_redirect": (
            "EUWESeedRipenFunction -> EUWEGrowthFunction (from DefaultUWEFarming.ini)"
        ),
    }

    out_payload = {
        "schema_version": 1,
        "build_notes": (
            "Subnautica 2 farming is driven by the UWEFarming plugin. "
            "A Farmable Plant BP carries one or more UWESeedGrowerComponent "
            "templates. Each grower slot ripens independently in RipenTime "
            "seconds and then spawns a SeedClass actor (or, when SeedClass "
            "is unset on the slot, the harvest is delivered via the recipe "
            "Output ItemType). Plants are placed by a Builder Construct "
            "action that requires AllowedSurfaceTags='BuildableSurface.Planter', "
            "i.e. they must sit on a Planter base piece. The recipe "
            "Requirements list the seed item the player consumes to plant."
        ),
        "uwe_farming_plugin": uwe_farming,
        "farmable_plants_count": len(plants),
        "farmable_plants": plants,
        "planter_container": planter,
        "metal_farm": metal_farm,
        "wild_regrowing_plants": wild,
    }

    out_json = OUT_DIR / "farming.json"
    out_json.write_text(json.dumps(out_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {out_json}")

    # Build markdown summary
    md = render_markdown(out_payload)
    out_md = OUT_DIR / "farming.md"
    out_md.write_text(md, encoding="utf-8")
    print(f"wrote {out_md}")


def _fmt_time(s: float | None) -> str:
    if s is None:
        return "n/a"
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s/60:.1f} min"
    return f"{s/3600:.2f} h"


def render_markdown(payload: dict) -> str:
    lines = []
    push = lines.append
    push("# Subnautica 2 farming research")
    push("")
    push("Source: CUE4Parse over the pre-Early Access PAK. Verified by direct property reads on `UWESeedGrowerComponent` templates inside each Farmable BP and on the related Recipe / ItemType / ConstructData assets.")
    push("")
    push("## Mechanic overview")
    push("")
    push(payload["build_notes"])
    push("")
    push("Plugin module: `UWEFarming` (runtime, C++). Three relevant components:")
    push("")
    for k, v in payload["uwe_farming_plugin"]["components"].items():
        push(f"- `{k}`: {v}")
    push("")
    push(f"Enum redirect from plugin config: {payload['uwe_farming_plugin']['enum_redirect']}")
    push("")
    push("## Planter container")
    push("")
    pc = payload["planter_container"]
    push(f"Surface tag farmables require: `{pc['surface_tag']}`.")
    push("")
    push("Buildable actions that produce a Planter (or that share the Planter tag set):")
    push("")
    for a in pc["buildable_actions"]:
        push(f"- `{a['name']}` ({_last_segment(a['asset'])}, action={a['action_class']}, surface_tags={a['surface_tags']})")
    push("")
    push(f"Exterior planter modular meshes: {len(pc['exterior_planter_meshes'])} pieces under `Subnautica2/Content/Art/Bases/BasePieces/Planter/Exterior/`.")
    push(f"Interior planter pieces: {len(pc['interior_planter_meshes'])} pieces (`SM_Small_Planter_01`, `SM_Planter_Single_Micro`).")
    push("")
    push("Note: `BP_BaseModule_HydroponicTank_01a` (under AbandonedBases/KitPieces) is purely decorative (StaticMesh + Niagara, no SeedGrower component). It is set-dressing, not a functional growbed.")
    push("")
    push("## Farmable plants")
    push("")
    push(f"Count: **{payload['farmable_plants_count']}** Farmable BP variants.")
    push("")
    push("| Plant id | Display name | Slots | Ripen times (s) | Seed input (item) | Yield per slot |")
    push("|---|---|---:|---|---|---:|")
    for p in payload["farmable_plants"]:
        rts = ", ".join(f"{int(r)}" for r in p["growth"]["ripen_times_s"]) or "n/a"
        seed_name = p["seed"].get("input_item_name") or p["seed"].get("input_item_id") or "n/a"
        seed_qty = p["seed"]["input_count"] or 1
        yld = p["yield"]["count_per_slot"] or 1
        name = p["display_name"] or "(no name)"
        push(f"| `{p['id']}` | {name} | {p['growth']['grower_slots']} | {rts} | {seed_name} x{seed_qty} | {yld} |")
    push("")
    push("### Per-plant detail")
    push("")
    for p in payload["farmable_plants"]:
        nm = p["display_name"]
        if not nm or "PLACEHOLDER" in nm:
            nm = f"{p['id']} (no display name yet)"
        push(f"#### {nm}")
        push("")
        push(f"- BP: `{p['bp_asset']}`")
        push(f"- ItemType: `{p['item_type_asset']}`")
        push(f"- Description: {p['description'] or '(empty)'}")
        push(f"- Grower slots: {p['growth']['grower_slots']}")
        push(f"- Ripen times: {[int(r) for r in p['growth']['ripen_times_s']]} seconds (min {_fmt_time(p['growth']['min_ripen_s'])}, max {_fmt_time(p['growth']['max_ripen_s'])})")
        push(f"- Starts grown: {p['growth']['starts_grown']}")
        push(f"- Regrows after harvest: {p['growth']['regrows']} (SpawnRate>0 on any slot)")
        seed = p["seed"]
        push(f"- Seed input: `{seed.get('input_item_id')}` x{seed.get('input_count') or 1} (\"{seed.get('input_item_name') or '?'}\")")
        push(f"- Harvested item: `{p['yield']['harvested_item_id']}` x{p['yield']['count_per_slot'] or 1} per ripened slot")
        ca = p["construct_action"] or {}
        push(f"- Construct surface tag(s): {ca.get('surface_tags')}")
        # Per-slot detail when slots differ
        slots = p["growth"]["growers"]
        if slots:
            push(f"- Slot detail:")
            for g in slots:
                seed_class = g["seed_class"] or "(inherited / set by recipe output)"
                push(f"  - `{g['slot']}` ripen={int(g['ripen_time_s'])}s seed_class={seed_class}")
        push("")
    push("## Wild cuttable + regrowing plants (different mechanic)")
    push("")
    push("These are world-placed plants that regrow after being cut. They use `UWEPlantGrowerComponent` instead of `UWESeedGrowerComponent`, and they are NOT placed by the player.")
    push("")
    for w in payload["wild_regrowing_plants"]["plants"]:
        push(f"- `{w['asset']}`")
        for c in w["components"]:
            push(f"  - {c['class']} `{c['name']}`")
    push("")
    push("Note: `BP_FarmableCradleShootRoot` is a **planted (player-built) farmable**, not a wild regrowing plant. It uses `UWESeedGrowerComponent` (1 slot, 120s ripen) just like the other farmables and its recipe consumes a `DA_LuciferRotsac_ItemType` as the seed input. The display name resolves to placeholder text in this build.")
    push("")
    push("## Metal Farm")
    push("")
    mf = payload["metal_farm"]
    name = (mf["item_type"] or {}).get("name") or "Metal Farm"
    desc = (mf["item_type"] or {}).get("description") or ""
    push(f"`{name}`. {desc}")
    push("")
    push(f"- Buildable BP: `{mf['actor_bp']}`")
    push(f"- Seed actor BP: `{mf['seed_bp']}` (`BP_Resource_MetalFarmSeed_C`)")
    push(f"- Seed mapping data: `{mf['seed_mapping_asset']}` (`MetalFarmSeedMapping_DataAsset_C`)")
    sg = mf["seed_grower"] or {}
    push(f"- Has `UWESeedGrowerComponent`: yes (same plugin component as Farmable Plants). RipenTime override on the BP's grower template: `{sg.get('ripen_time_s')}` (0 = inherits parent default, configured at runtime by Tier tag).")
    push("")
    push("The Metal Farm IS a real farming mechanic. Player inserts a seed item (one of 14 metal/resource ItemTypes), the seed grows into a resonatable resource deposit that drops the matched resource. Growth tier (Fast/Medium/Slow) is selected per resource by a GameplayTag (`ItemType.TunableData.SeedGrowerTime.{Fast,Medium,Slow}`); the actual numeric duration is not stored on the data asset and is interpreted at runtime by `UWESeedGrowerComponent`.")
    push("")
    push("Seed mappings:")
    push("")
    push("| Input item | Tier tag | Resonatable spawned |")
    push("|---|---|---|")
    for m in mf["seed_mappings"]:
        push(f"| `{m['input_item_id']}` | `{m['tier_tag']}` | `{m['resonatable_id']}` |")
    push("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
