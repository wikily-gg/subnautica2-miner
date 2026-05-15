"""Extract the Bioreactor base piece + the bioreactor-eligible item set.

The base piece is `BP_Bioreactor` (Subnautica2/Content/Blueprints/BaseBuilding/
BP_Bioreactor). Its CDO carries:
  - `InventoryComponent.MaxItems`              -> slot count (10 in this build).
  - `InventoryComponent.AllowedTags`           -> filter tags. In the pre-EA
    build these are `ItemType.Flora, Fauna, Fuel`, but NO `UWEItemType` in
    the PAK carries any of them. The native sim bypasses the filter.
  - `PowerConsumptionNormal.PowerProduction`   -> 10.0 units.
  - `PowerConsumptionOverdrive.PowerProduction` -> 20.0 units.
  - `PowerGeneratorComponent.PowerSimulationClass` -> `/Script/Subnautica2.
    SN2BioreactorSimulation` (native C++, not in any data asset).

Per-item energy is not stored as a number. Every accepted item carries one
of four discrete tags:
    ItemType.TunableData.BioFuelConsumption.{Short, Medium, Long, ExtraLong}
The native simulation interprets the tier durations; they are not present
in any DataTable / Curve / DataAsset in the PAK. The wiki shows the tier
ordinal and leaves absolute seconds for the runtime tester to time.

We pair the reactor with its recipe / scan / databank entry so the wiki
page can be rendered from a single JSON.
"""
from __future__ import annotations

import logging
import re

from helpers import (
    _export_class, array_values, extract_gameplay_tags, find_export,
    obj_ref_path, prop, prop_array, prop_float, prop_int, prop_object_path,
    prop_str, prop_tags, safe_load_package, short_name_from_path,
    unwrap_struct,
)

logger = logging.getLogger(__name__)

BP_PATH = "Subnautica2/Content/Blueprints/BaseBuilding/BP_Bioreactor"
RECIPE_PATH = "Subnautica2/Content/Data/CraftingRecipes/Builder/DA_BioreactorRecipe"
CONSTRUCT_PATH = "Subnautica2/Content/Data/BaseBuilding/BuilderActions/DA_BioreactorConstructData"
DATABANK_PATH = "Subnautica2/Content/Data/DatabankEntry/BaseBuilding/DA_BasePiece_Bioreactor_DatabankEntry"
SCAN_PATH = "Subnautica2/Content/Data/ScanData/BaseBuilding/DA_BasePiece_Bioreactor_ScanData"
ITEM_TYPE_PATH = "Subnautica2/Content/Data/ItemType/BaseBuilding/DA_Bioreactor_ItemType"

ITEM_TYPES_PREFIX = "Subnautica2/Content/Data/ItemType/"

TIER_RE = re.compile(r"^ItemType\.TunableData\.BioFuelConsumption\.([A-Za-z]+)$")
TIER_ORDER = ["Short", "Medium", "Long", "ExtraLong"]


# ---------- BP_Bioreactor CDO ----------

def _read_blueprint(provider) -> dict:
    out: dict = {
        "asset": BP_PATH,
        "max_slots": None,
        "allowed_tags": [],
        "power_production_normal": None,
        "power_production_overdrive": None,
        "active_cue_tag": None,
        "mixer_max_turn_speed": None,
        "power_simulation_class": None,
        "power_simulation_is_native": False,
    }
    pkg = safe_load_package(provider, BP_PATH)
    if pkg is None:
        return out
    for ex in pkg.GetExports():
        name = str(ex.Name)
        if name == "Default__BP_Bioreactor_C":
            pcn_u = unwrap_struct(prop(ex, "PowerConsumptionNormal"))
            pco_u = unwrap_struct(prop(ex, "PowerConsumptionOverdrive"))
            if pcn_u is not None:
                out["power_production_normal"] = prop_float(pcn_u, "PowerProduction")
            if pco_u is not None:
                out["power_production_overdrive"] = prop_float(pco_u, "PowerProduction")
            out["mixer_max_turn_speed"] = prop_float(ex, "MixerMaxTurnSpeed") or None
            cue_u = unwrap_struct(prop(ex, "ActiveCue"))
            if cue_u is not None:
                tag = prop_str(cue_u, "TagName") or None
                out["active_cue_tag"] = tag
        elif name == "InventoryComponent":
            out["max_slots"] = prop_int(ex, "MaxItems") or None
            out["allowed_tags"] = extract_gameplay_tags(prop(ex, "AllowedTags"))
        elif name == "PowerGeneratorComponent":
            psc = obj_ref_path(prop(ex, "PowerSimulationClass"))
            out["power_simulation_class"] = psc
            out["power_simulation_is_native"] = bool(psc and "/Script/" in psc)
    return out


# ---------- Recipe / construct / databank / scan ----------

def _read_recipe(provider) -> dict | None:
    pkg = safe_load_package(provider, RECIPE_PATH)
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
        "id": short_name_from_path(RECIPE_PATH),
        "asset": RECIPE_PATH,
        "name": prop_str(ex, "Name"),
        "description": prop_str(ex, "Description"),
        "crafting_time_seconds": prop_float(ex, "CraftingTime") or None,
        "outputs": outputs,
        "requirements": requirements,
        "category": prop_object_path(ex, "Category"),
        "default_state": str(prop(ex, "DefaultRecipeState") or "") or None,
    }


def _read_construct(provider) -> dict | None:
    pkg = safe_load_package(provider, CONSTRUCT_PATH)
    if pkg is None:
        return None
    ex = None
    for e in pkg.GetExports():
        if _export_class(e).startswith("SN2Builder"):
            ex = e
            break
    if ex is None:
        return None
    return {
        "asset": CONSTRUCT_PATH,
        "name": prop_str(ex, "Name"),
        "description": prop_str(ex, "Description"),
        "secondary_description": prop_str(ex, "SecondaryDescription"),
        "power_generation_text": prop_str(ex, "PowerGenerationText") or None,
        "category": str(prop(ex, "Category") or "") or None,
        "thumbnail": prop_object_path(ex, "Thumbnail"),
    }


def _read_databank(provider) -> dict | None:
    pkg = safe_load_package(provider, DATABANK_PATH)
    if pkg is None:
        return None
    ex = find_export(pkg, class_substring="UWEDatabankEntry")
    if ex is None:
        return None
    return {
        "id": short_name_from_path(DATABANK_PATH),
        "asset": DATABANK_PATH,
        "title": prop_str(ex, "EntryTitle"),
        "text": prop_str(ex, "EntryText"),
    }


def _read_scan(provider) -> dict | None:
    pkg = safe_load_package(provider, SCAN_PATH)
    if pkg is None:
        return None
    ex = find_export(pkg, class_substring="UWEScanData")
    if ex is None:
        return None
    return {
        "id": short_name_from_path(SCAN_PATH),
        "asset": SCAN_PATH,
        "name": prop_str(ex, "Name"),
        "scan_duration_seconds": prop_float(ex, "ScanDuration") or None,
        "num_required": prop_int(ex, "NumRequired") or None,
    }


def _read_item_type(provider) -> dict | None:
    pkg = safe_load_package(provider, ITEM_TYPE_PATH)
    if pkg is None:
        return None
    ex = find_export(pkg, class_substring="UWEItemType")
    if ex is None:
        return None
    return {
        "id": short_name_from_path(ITEM_TYPE_PATH),
        "asset": ITEM_TYPE_PATH,
        "name": prop_str(ex, "Name"),
        "description": prop_str(ex, "ItemDescription"),
        "icon": prop_object_path(ex, "Icon"),
        "thumbnail": prop_object_path(ex, "Thumbnail"),
    }


# ---------- Eligible items (scan every UWEItemType for BioFuelConsumption) ----------

def _read_tunable_map(td) -> list[dict]:
    """Decode `UWEItemType.TunableData` (a UScriptMap of FGameplayTag -> float).

    Items carry per-tag tunable data the simulation may consume. We surface
    everything, not just BioFuel rows, so the wiki can compare burn time vs
    other gameplay tunables on the same item.
    """
    if td is None:
        return []
    props = getattr(td, "Properties", None)
    if props is None:
        return []
    out: list[dict] = []
    for kv in props:
        key_g = getattr(kv, "Key", None)
        val_g = getattr(kv, "Value", None)
        kg = key_g.GenericValue if key_g is not None else None
        vg = val_g.GenericValue if val_g is not None else None
        # Tag name lives on a wrapped struct
        tag = None
        if kg is not None:
            tag = getattr(kg, "TagName", None)
            if tag is None:
                u = unwrap_struct(kg)
                if u is not None:
                    tag = prop_str(u, "TagName") or prop_str(u, "Tag") or None
        try:
            value = float(vg) if vg is not None else None
        except Exception:
            value = None
        out.append({"tag": str(tag) if tag else None, "value": value})
    return out


def _scan_eligible_items(provider) -> list[dict]:
    keys = sorted(
        k for k in provider.Files.Keys
        if k.startswith(ITEM_TYPES_PREFIX) and k.endswith(".uasset")
    )
    out: list[dict] = []
    for k in keys:
        pkg = safe_load_package(provider, k[:-7])
        if pkg is None:
            continue
        ex = find_export(pkg, class_substring="UWEItemType")
        if ex is None:
            continue
        gtags = extract_gameplay_tags(prop(ex, "GameplayTags"))
        type_tag = extract_gameplay_tags(prop(ex, "TypeTag"))
        all_tags = list(gtags) + list(type_tag)
        tier = None
        for t in all_tags:
            m = TIER_RE.match(t)
            if m:
                tier = m.group(1)
                break
        if tier is None:
            continue
        tunables = _read_tunable_map(prop(ex, "TunableData"))
        out.append({
            "id": short_name_from_path(k[:-7]),
            "asset": k[:-7],
            "name": prop_str(ex, "Name"),
            "description": prop_str(ex, "ItemDescription"),
            "icon": prop_object_path(ex, "Icon"),
            "thumbnail": prop_object_path(ex, "Thumbnail"),
            "tier": tier,
            "gameplay_tags": gtags,
            "type_tag": type_tag,
            "tunables": tunables,
        })
    return out


# ---------- Entry point ----------

def run(provider) -> dict:
    bp = _read_blueprint(provider)
    recipe = _read_recipe(provider)
    construct = _read_construct(provider)
    databank = _read_databank(provider)
    scan = _read_scan(provider)
    item_type = _read_item_type(provider)
    eligible = _scan_eligible_items(provider)

    tier_counts: dict[str, int] = {}
    for it in eligible:
        tier_counts[it["tier"]] = tier_counts.get(it["tier"], 0) + 1

    # Stable sort by tier order then by name for readable JSON.
    tier_index = {t: i for i, t in enumerate(TIER_ORDER)}
    eligible.sort(key=lambda it: (
        tier_index.get(it["tier"], 99),
        (it.get("name") or it.get("id") or "").lower(),
    ))

    logger.info(
        "Bioreactor: %d eligible items (counts %s)", len(eligible), tier_counts
    )

    return {
        "bioreactor": {
            "blueprint": bp,
            "item_type": item_type,
            "recipe": recipe,
            "construct_action": construct,
            "databank": databank,
            "scan_data": scan,
        },
        "tier_order": TIER_ORDER,
        "tier_counts": tier_counts,
        "fuels": eligible,
    }
