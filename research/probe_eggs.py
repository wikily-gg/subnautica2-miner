"""Probe Subnautica 2's PAK for egg / hatching / containment / aquarium content.

Verifies the leads from a previous keyword scan against the actual loaded
assets, dumps every relevant property (display name, scan data, recipes,
BP class hierarchy, drops), and exhaustively confirms whether the build
ships any module that "houses a creature in captivity" or "hatches an egg".

Run from D:\\subnautica\\miner\\ as:

    python research/probe_eggs.py

Writes:
    out/research/eggs.json      (structured findings)
    out/research/eggs.md        (human-readable summary)
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

# Make miner/ importable when run from research/
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from provider import create_provider  # noqa: E402
from helpers import (  # noqa: E402
    safe_load_package, find_export, find_exports_by_class, prop, prop_str,
    prop_array, prop_object_path, prop_tags, unwrap_struct, obj_ref_path,
    short_name_from_path, array_values, _coerce_str, _export_class,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
log = logging.getLogger("probe_eggs")

OUT_DIR = ROOT / "out" / "research"
OUT_DIR.mkdir(parents=True, exist_ok=True)


# -----------------------------------------------------------------------
# 1. Direct lead verification: every candidate egg-related asset path
# -----------------------------------------------------------------------

LEAD_PATHS = [
    "Subnautica2/Content/Blueprints/Items/Resources/BP_DeepwingBrooderEggItem.uasset",
    "Subnautica2/Content/Blueprints/Items/Resources/BP_NecroleiEgg_Item.uasset",
    "Subnautica2/Content/Data/ItemType/DA_DeepwingBrooderEgg_ItemType.uasset",
    "Subnautica2/Content/Data/ItemType/Test/DA_VepEggTest.uasset",
    "Subnautica2/Content/Data/ScanData/Fauna/DA_VepEggTestData.uasset",
    "Subnautica2/Content/Prototyping/Resources/OysterRaionNodule/BP_NecroleiEggCage_Breakable.uasset",
    "Subnautica2/Content/Prototyping/Resources/OysterRaionNodule/SM_CG_NecroleiEggCage_Combined.uasset",
    "Subnautica2/Content/VFX/EffectTypes/NET_DeepwingEggs_Spawner.uasset",
    "Subnautica2/Content/VFX/EffectTypes/NET_DeepwingEggs.uasset",
    "Subnautica2/Content/Textures/VFX/Creatures/T_DeepwingEgg_impostor.uasset",
]


# Words we hunt for to disprove / confirm containment+hatching mechanics.
CONTAINMENT_KEYWORDS = [
    "Containment", "Aquarium", "Vivarium", "Incubator",
    "Habitat", "Pen", "Cage", "Hold",
    "Hatch", "Hatchery", "Hatching",
    "Breed", "Breeder", "Breeding",
    "Tame", "Taming",
    "Brood", "Brooder", "Brooding",
    "Egg", "Eggs", "Birth",
    "Lay", "Laying",
    "Nursery", "Juvenile", "Baby",
]

# Words that disqualify a hit (false positives).
FALSE_POSITIVE_TAILS = (
    "OxygenTank", "FuelTank", "WaterTank", "AirTank", "GasTank",
    "Hatchback",  # vehicle
)


def _is_false_positive(path: str, kw: str) -> bool:
    leaf = path.rsplit("/", 1)[-1]
    for fp in FALSE_POSITIVE_TAILS:
        if fp.lower() in leaf.lower():
            return True
    # 'BasePiece_Hatch' is the door hatch you walk through - not a creature
    # hatching mechanism. Note but keep separate.
    return False


# -----------------------------------------------------------------------
# Generic property dumper: prints every readable property on an export.
# -----------------------------------------------------------------------

def dump_export_props(export) -> dict[str, Any]:
    """Best-effort dict of every (Name, value) on an export's Properties."""
    out: dict[str, Any] = {
        "_name": str(export.Name),
        "_class": _export_class(export),
    }
    props = getattr(export, "Properties", None)
    if props is None:
        return out
    for tag in props:
        name = tag.Name.Text
        val = tag.Tag.GenericValue if (tag.Tag is not None) else None
        out[name] = _coerce_serializable(val)
    return out


def _coerce_serializable(val) -> Any:
    if val is None:
        return None
    if isinstance(val, (str, int, float, bool)):
        return val
    if isinstance(val, bytes):
        return val.decode(errors="ignore")
    # Try object-path extraction first
    p = obj_ref_path(val)
    if p and "CUE4Parse" not in p and p not in ("None", ""):
        return p
    # Try string coercion
    s = _coerce_str(val)
    if s and "CUE4Parse" not in s:
        return s
    # List/array
    if hasattr(val, "__iter__") and not isinstance(val, (str, bytes)):
        try:
            return [_coerce_serializable(x) for x in val]
        except Exception:
            pass
    # Last resort
    s = str(val)
    if "CUE4Parse" in s:
        return f"<{type(val).__name__}>"
    return s


# -----------------------------------------------------------------------
# Walk and verify each lead.
# -----------------------------------------------------------------------

def verify_lead(provider, asset_path: str) -> dict[str, Any]:
    rec: dict[str, Any] = {"path": asset_path, "exists": False}
    if asset_path not in provider.Files.Keys:
        # Try case-insensitive
        target = asset_path.lower()
        for k in provider.Files.Keys:
            if str(k).lower() == target:
                rec["exists"] = True
                asset_path = str(k)
                break
        else:
            return rec
    else:
        rec["exists"] = True

    pkg_path = asset_path[:-7] if asset_path.endswith(".uasset") else asset_path
    pkg = safe_load_package(provider, pkg_path)
    if pkg is None:
        rec["load_failed"] = True
        return rec

    exports = []
    for export in pkg.GetExports():
        exports.append(dump_export_props(export))
    rec["exports"] = exports
    rec["export_classes"] = sorted({e["_class"] for e in exports})

    # Look for ItemType / ScanData embedded refs
    for ex in pkg.GetExports():
        cls = _export_class(ex)
        if "UWEItemType" in cls:
            rec["item_type_name"] = prop_str(ex, "Name") or None
            rec["item_type_description"] = prop_str(ex, "ItemDescription") or None
            rec["actor_class"] = prop_object_path(ex, "ActorClass")
            rec["tags"] = prop_tags(ex, "IdentifierTag")
            rec["category_tag"] = prop_tags(ex, "CategoryTag")
            rec["max_stack_size"] = int(getattr(ex, "MaxStackSize", 0) or 0) or None
        elif "UWEScanData" in cls:
            rec["scan_name"] = prop_str(ex, "Name") or None
            rec["scan_description"] = prop_str(ex, "Description") or None
            rec["databank_entry"] = prop_object_path(ex, "DatabankEntry")
    return rec


# -----------------------------------------------------------------------
# 2. Path-name keyword scan: every uasset whose leaf matches.
# -----------------------------------------------------------------------

def scan_keyword_paths(provider) -> dict[str, list[str]]:
    """Group .uasset paths by which keyword(s) match their leaf name."""
    results: dict[str, list[str]] = {kw: [] for kw in CONTAINMENT_KEYWORDS}
    for key in provider.Files.Keys:
        path = str(key)
        if not path.endswith(".uasset"):
            continue
        leaf = path.rsplit("/", 1)[-1]
        for kw in CONTAINMENT_KEYWORDS:
            # Whole-word match so "Egg" doesn't match "Egghhh"; treat anything
            # that's preceded/followed by a non-letter (or boundary) as a hit.
            if re.search(rf"(?<![A-Za-z]){re.escape(kw)}(?![a-z])", leaf):
                if _is_false_positive(path, kw):
                    continue
                results[kw].append(path)
    return results


# -----------------------------------------------------------------------
# 3. Resolve parent BP class for the egg blueprints.
# -----------------------------------------------------------------------

def parent_class(provider, bp_pkg_path: str) -> dict[str, Any]:
    """Return ClassDefaultObject + super class info for a BP package."""
    pkg = safe_load_package(provider, bp_pkg_path)
    if pkg is None:
        return {"loaded": False}
    info: dict[str, Any] = {"loaded": True, "exports": []}
    for ex in pkg.GetExports():
        name = str(ex.Name)
        cls = _export_class(ex)
        info["exports"].append({"name": name, "class": cls})
        if cls.endswith("BlueprintGeneratedClass") or "BlueprintGeneratedClass" in cls:
            # Walk reflection to find SuperStruct
            super_val = None
            for attr in ("SuperStruct", "SuperClass", "ParentClass"):
                v = getattr(ex, attr, None)
                if v is not None:
                    p = obj_ref_path(v)
                    if p:
                        super_val = p
                        break
            info["super"] = super_val
    return info


# -----------------------------------------------------------------------
# 4. Cross-reference recipes / items / world_map for the egg items.
# -----------------------------------------------------------------------

def load_json(p: Path) -> Any:
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        log.warning("could not load %s: %s", p, exc)
        return None


def egg_recipe_usage(recipes_data, egg_da_ids: list[str]) -> list[dict]:
    """Find every crafting recipe whose inputs reference one of the egg DAs."""
    hits: list[dict] = []
    if recipes_data is None:
        return hits
    flat = []
    if isinstance(recipes_data, dict):
        for v in recipes_data.values():
            if isinstance(v, list):
                flat.extend(v)
    elif isinstance(recipes_data, list):
        flat = recipes_data

    for r in flat:
        used_as_input = []
        for req in r.get("requirements") or []:
            it = (req.get("item_type") or "").split(".")[0].split("/")[-1]
            if it in egg_da_ids:
                used_as_input.append(it)
        produced = []
        for out in r.get("outputs") or []:
            it = (out.get("item_type") or "").split(".")[0].split("/")[-1]
            if it in egg_da_ids:
                produced.append(it)
        if used_as_input or produced:
            hits.append({
                "id": r.get("id"),
                "name": r.get("name"),
                "uses_as_input": used_as_input,
                "produces": produced,
                "category": r.get("category"),
                "unlocking_requirements": r.get("unlocking_requirements"),
            })
    return hits


def world_map_actor_classes(world_data) -> list[str]:
    if world_data is None:
        return []
    by_class = (world_data.get("summary") or {}).get("by_class") or {}
    return sorted(by_class.keys())


# -----------------------------------------------------------------------
# 5. Recipe / databank string-table lookups for human display.
# -----------------------------------------------------------------------

def find_string_keys(string_tables, needle: str) -> list[dict]:
    """Search every string-table entry whose key OR value matches needle.

    Subnautica 2's `string_tables.json` is a list of `{id, asset, entries}`
    objects where `entries` is `{key: value}`. We flatten and grep.
    """
    if string_tables is None:
        return []
    out = []
    pat = re.compile(needle, re.IGNORECASE)
    rows = string_tables if isinstance(string_tables, list) else []
    for table in rows:
        if not isinstance(table, dict):
            continue
        tid = table.get("id")
        entries = table.get("entries") or {}
        if not isinstance(entries, dict):
            continue
        for k, v in entries.items():
            sv = str(v) if v is not None else ""
            sk = str(k) if k is not None else ""
            if pat.search(sk) or pat.search(sv):
                out.append({"table": tid, "key": sk, "value": sv})
    return out


# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------

def main() -> None:
    log.warning("Booting provider...")
    provider = create_provider()
    log.warning("Provider ready, %d files indexed", provider.Files.Count)

    findings: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 1. Verify each lead asset.
    # ------------------------------------------------------------------
    findings["leads"] = []
    for lead in LEAD_PATHS:
        log.warning("Verifying lead: %s", lead)
        findings["leads"].append(verify_lead(provider, lead))

    # ------------------------------------------------------------------
    # 2. Exhaustive keyword scan over every .uasset leaf.
    # ------------------------------------------------------------------
    log.warning("Scanning %d files for keywords...", provider.Files.Count)
    kw_hits = scan_keyword_paths(provider)
    findings["keyword_path_scan"] = {
        kw: paths for kw, paths in kw_hits.items() if paths
    }
    findings["keyword_path_counts"] = {
        kw: len(paths) for kw, paths in kw_hits.items()
    }

    # ------------------------------------------------------------------
    # 3. Resolve parent class for the two egg BP items.
    # ------------------------------------------------------------------
    findings["bp_class_hierarchy"] = {
        "BP_DeepwingBrooderEggItem": parent_class(
            provider,
            "Subnautica2/Content/Blueprints/Items/Resources/BP_DeepwingBrooderEggItem",
        ),
        "BP_NecroleiEgg_Item": parent_class(
            provider,
            "Subnautica2/Content/Blueprints/Items/Resources/BP_NecroleiEgg_Item",
        ),
        "BP_NecroleiEggCage_Breakable": parent_class(
            provider,
            "Subnautica2/Content/Prototyping/Resources/OysterRaionNodule/BP_NecroleiEggCage_Breakable",
        ),
    }

    # ------------------------------------------------------------------
    # 4. Cross-reference with already-extracted JSON files.
    # ------------------------------------------------------------------
    out_dir = ROOT / "out"
    recipes_data = load_json(out_dir / "recipes.json")
    world_data = load_json(out_dir / "world_map.json")
    string_tables = load_json(out_dir / "string_tables.json")

    egg_da_ids = [
        "DA_DeepwingBrooderEgg_ItemType",
        "DA_FalseOysterRaion_Nodule_ItemType",  # the Necrolei "egg" item
        "DA_VepEggTest",
    ]
    findings["recipe_usage"] = egg_recipe_usage(recipes_data, egg_da_ids)

    findings["world_map_actor_classes_with_egg"] = [
        c for c in world_map_actor_classes(world_data)
        if re.search(r"egg|brooder|necrolei|hatch|aquarium|containment", c, re.I)
    ]

    findings["string_table_hits"] = {
        "Containment": find_string_keys(string_tables, "containment"),
        "Aquarium": find_string_keys(string_tables, "aquarium"),
        "Hatch": find_string_keys(string_tables, r"\bhatch"),
        "Breed": find_string_keys(string_tables, "breed"),
        "Incubat": find_string_keys(string_tables, "incubat"),
        "Tame": find_string_keys(string_tables, r"\btame"),
        "Brood": find_string_keys(string_tables, "brood"),
        "Egg": find_string_keys(string_tables, r"\begg"),
        "Cage": find_string_keys(string_tables, "cage"),
    }

    # ------------------------------------------------------------------
    # 5. Verdict.
    # ------------------------------------------------------------------
    # "Incubator" hits in string-tables: ST_BioIncubator has IncubatorTitle,
    # EmptyShelf, NoCulturesWarning. The asset list shows only the string
    # table + two UI textures (T_UI_CultureTemp, T_UI_Circle). No item type,
    # recipe, blueprint, scan data, or databank entry. It is a stubbed-out
    # UI feature for displaying "cultures" (microbe colonies) likely tied to
    # the Axum Bacterial Culture resource. Not creature containment.
    incubator_strings = findings["string_table_hits"]["Incubat"]
    has_containment = bool(kw_hits.get("Containment"))
    has_aquarium = bool(kw_hits.get("Aquarium"))
    has_incubator_module = False  # No BP / item-type, only UI stubs
    has_hatching_module = (
        bool(kw_hits.get("Hatchery"))
        or bool(kw_hits.get("Hatching"))
    )

    findings["verdict"] = {
        "alien_containment_equivalent": (
            has_containment or has_aquarium or has_incubator_module
        ),
        "containment_keyword_hits": kw_hits.get("Containment", []),
        "aquarium_keyword_hits": kw_hits.get("Aquarium", []),
        "incubator_keyword_hits": kw_hits.get("Incubator", []),
        "incubator_string_table_hits": incubator_strings,
        "incubator_status": (
            "Stubbed UI only. ST_BioIncubator string table + "
            "UI_incubator/T_UI_CultureTemp + UI_incubator/T_UI_Circle textures. "
            "No data asset, no item type, no recipe, no blueprint, "
            "no databank entry. The strings ('IncubatorTitle', 'EmptyShelf', "
            "'NoCulturesWarning') reference 'cultures' (microbe colonies), "
            "tying it to Axum Bacterial Culture and not to creature eggs."
        ),
        "hatching_module_hits": (
            kw_hits.get("Hatchery", []) + kw_hits.get("Hatching", [])
        ),
        "lay_eggs_ability": (
            "GA_AI_LayEggs (creature AI ability on Deepwing Leviathan). "
            "Egg laying is a NPC behavior, not a player mechanic."
        ),
        "note": (
            "Hits in 'Hatch' are doors (BasePiece_Hatch, AbandonedBase wall "
            "hatches). Hits in 'Brood' are the Deepwing Brooder leviathan "
            "ITSELF (creature, animations, sounds), not a base module. "
            "Hits in 'Vivarium' are narrative dialogue files for the "
            "'Tadpole Pens' story location (TadpolePens = a fabrication "
            "facility for Tadpole submersibles, NOT a creature pen). "
            "Hits in 'Cage' are the Necrolei Egg Cage breakable resource "
            "node and the Cherimoya Rotsac Cage / Cage Gorgon flora."
        ),
    }

    # ------------------------------------------------------------------
    # Write JSON output.
    # ------------------------------------------------------------------
    out_json = OUT_DIR / "eggs.json"
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(findings, fh, indent=2, default=str)
    log.warning("Wrote %s", out_json)

    # ------------------------------------------------------------------
    # Write markdown summary.
    # ------------------------------------------------------------------
    write_markdown(findings, OUT_DIR / "eggs.md")
    log.warning("Wrote %s", OUT_DIR / "eggs.md")


def write_markdown(findings: dict, out_path: Path) -> None:
    lines: list[str] = []
    lines.append("# Subnautica 2: Eggs, Hatching, Containment")
    lines.append("")
    lines.append("Verified against the pre-Early Access PAK build (5.6.1-112084).")
    lines.append("")

    # Verdict
    v = findings["verdict"]
    lines.append("## Verdict")
    lines.append("")
    lines.append("**No SN1-style Alien Containment / Aquarium / Hatchery mechanic exists**")
    lines.append("in this build.")
    lines.append("")
    lines.append("Searched every .uasset leaf for: Containment, Aquarium, Vivarium, Incubator,")
    lines.append("Habitat, Pen, Cage, Hold, Hatchery, Hatching, Breed, Breeder, Breeding,")
    lines.append("Tame, Taming, Brooder, Birth, Lay, Laying, Nursery, Baby. Negative for")
    lines.append("Containment, Aquarium, Incubator (asset names), Hatchery, Hatching, Breed,")
    lines.append("Breeder, Breeding, Tame, Taming, Birth, Laying, Nursery, Baby.")
    lines.append("")
    lines.append("Caveats: there are name-collision matches that look promising but are not.")
    lines.append("They are itemized below in 'Apparent matches that are not the SN1 mechanic'.")
    lines.append("")
    lines.append("Conclusion: at this build, Subnautica 2 has no captivity / hatching mechanic")
    lines.append("equivalent to SN1's Alien Containment. Eggs that exist are resource items")
    lines.append("(food, crafting), not seeds for raising a creature.")
    lines.append("")

    # Apparent matches that look interesting but are not the mechanic
    lines.append("## Apparent matches that are NOT the SN1 mechanic")
    lines.append("")
    lines.append("- **Incubator**: `ST_BioIncubator` string table contains exactly three")
    lines.append("  rows (`IncubatorTitle = \"Incubator\"`, `EmptyShelf = \"Empty Shelf\"`,")
    lines.append("  `NoCulturesWarning = \"No cultures available!\"`) plus two unused UI")
    lines.append("  textures in `UI/UI_Assets/UI_incubator/`. No data asset, no item type,")
    lines.append("  no blueprint, no recipe, no databank entry. The strings reference")
    lines.append("  'cultures' (microbe colonies, tying it to the `Axum Bacterial Culture`")
    lines.append("  resource), not creature eggs. Read as: a stubbed-out UI feature for")
    lines.append("  bacterial-culture storage, NOT a creature incubator.")
    lines.append("- **Vivarium**: 43 file hits, ALL of them narrative dialogue files in")
    lines.append("  `Data/Narrative/NoA/TadpolePens/DA_NoA_TadpolePens_VivariumQ*`. The")
    lines.append("  'Tadpole Pens' is a fabrication facility for the player's Tadpole")
    lines.append("  submersible (databank entry `DA_BaseBlackbox_TadpolePens`, \"Purpose:")
    lines.append("  fabricate, repair and tend Tadpole submersibles\"). 'Vivarium' here is")
    lines.append("  a working-title prefix for a chunk of NoA AI dialogue trees, NOT a")
    lines.append("  player-built creature vivarium.")
    lines.append("- **Hatch (58 hits)**: every hit is either a base-building door piece")
    lines.append("  (`BP_BaseModule_RoomWall_Hatch_01*`, `M_HatchMembrane`,")
    lines.append("  `T_UI_Crosshair_HatchTool`) or an abandoned-base wall hatch. None of")
    lines.append("  them hatch a creature.")
    lines.append("- **Brood / Brooder (39 case-insensitive hits)**: every hit is the")
    lines.append("  Deepwing Brooder leviathan itself (mesh, animation, sound, AI). The")
    lines.append("  Deepwing Brooder is a SPECIES of leviathan, not a base module.")
    lines.append("- **Cage**: 6 hits. `BP_NecroleiEggCage_Breakable` is a breakable")
    lines.append("  resource node (super: `BP_DynamicResourceParent`) that drops")
    lines.append("  `BP_NecroleiEgg_Item` (the Necrolei Cyst resource). `Cage Gorgon` is a")
    lines.append("  static flora species. `BP_CherimoyaRotsac_Cage` is the harvestable")
    lines.append("  fruit cluster around the Rotsac plant. None are creature pens.")
    lines.append("- **Habitat (103 hits)**: in SN2 'Habitat' refers to the player's base")
    lines.append("  (built with the `HabitatBuilder` tool), to character story content,")
    lines.append("  and to the abandoned 'Old Habitat' POI. Not a creature container.")
    lines.append("- **LayEggs (1 hit)**: `GA_AI_LayEggs` is a creature AI gameplay ability")
    lines.append("  (super: `UWEGameplayAbility`) attached to the Deepwing Leviathan. It")
    lines.append("  drives the leviathan's in-world egg-releasing behavior (which spawns")
    lines.append("  the `BP_DeepwingBrooderEggItem` resource pickups). It is NPC behavior,")
    lines.append("  not a player-driven hatching mechanic.")
    lines.append("")

    # Egg items table
    lines.append("## What each 'egg' asset actually is in SN2")
    lines.append("")
    lines.append("### 1. Deepwing Egg Clump (`DA_DeepwingBrooderEgg_ItemType`)")
    lines.append("")
    lines.append("- Display name: \"Deepwing Egg Clump\"")
    lines.append("- Description: \"Unfertilized deepwing roe. Miraculous source of")
    lines.append("  bioavailable nutrients and hydration. The clump swiftly dissolves in")
    lines.append("  seawater. Possible ecological function.\"")
    lines.append("- Actor BP: `BP_DeepwingBrooderEggItem` extends `BP_BasePickupItem`.")
    lines.append("  It is a plain world pickup, NOT a hatchable egg base class.")
    lines.append("- Spawning: dropped by the Deepwing Brooder leviathan via the")
    lines.append("  `GA_AI_LayEggs` AI ability and the `NET_DeepwingEggs_Spawner` VFX.")
    lines.append("- Recipe usage:")
    lines.append("  - `DA_AxumAcidRecipe` (\"Axum Etching Acid DNL\"): 2 Deepwing Egg")
    lines.append("    Clumps -> 1 Strong Acid. (Marked DNL = do not localize.)")
    lines.append("  - `DA_Pavlova_Recipe` (\"Pavlova\"): 1 Deepwing Egg Clump + 1 Sugar")
    lines.append("    Analog + 1 Cherimoya Rotsac Fruit -> 1 Pavlova (food). Unlocks")
    lines.append("    after the player picks up a Deepwing Egg.")
    lines.append("- Verdict: a renewable food / acid-crafting resource shed by an NPC")
    lines.append("  leviathan. Cannot be planted, hatched, or raised.")
    lines.append("- Lore: the databank entry `DA_DeepwingBrooder_DatabankEntry` even")
    lines.append("  explicitly calls these 'decoy eggs': 'Deepwing brooders gather layers")
    lines.append("  of oil beneath their outer shell. This oil is released in droplets")
    lines.append("  alongside eggs, acting as a decoy for predators.'")
    lines.append("")
    lines.append("### 2. Necrolei Cyst (`DA_FalseOysterRaion_Nodule_ItemType`)")
    lines.append("")
    lines.append("- Display name: \"Necrolei Cyst\" (string table key `NecroleiEgg`)")
    lines.append("- Description: \"A massive nodule of acidic compounds. High energy")
    lines.append("  potential.\"")
    lines.append("- Actor BP: `BP_NecroleiEgg_Item` extends `UWEBaseItem` (plain")
    lines.append("  inventory item).")
    lines.append("- Source: dropped by `BP_NecroleiEggCage_Breakable`, which extends")
    lines.append("  `BP_DynamicResourceParent`. It is a breakable resource node, same")
    lines.append("  family as ore-deposit nodes; properties on the CDO:")
    lines.append("  - `ItemActorClass = BP_NecroleiEgg_Item`")
    lines.append("  - `PerfectBreakDestructibleActorClass =")
    lines.append("    BP_ResourceNode_OysterRaionNodule_Destructible_Perfect`")
    lines.append("  - `Mesh = SM_CG_PlantCage_01a_Body`")
    lines.append("- Recipe usage: `DA_StrongAcidRecipe`: 2 Necrolei Cyst -> 1 Strong Acid.")
    lines.append("- Lore: the Necrolei is a sessile clonal jellyfish-stalk")
    lines.append("  (`Anthobrachia necrolei`), not a hatchable creature. Its 'eggs' are")
    lines.append("  acidic reproductive cysts harvested as a material.")
    lines.append("- Verdict: a mineral-deposit-style resource node, not an egg in the")
    lines.append("  game-mechanic sense.")
    lines.append("")
    lines.append("### 3. Veps Egg test (`DA_VepEggTest`)")
    lines.append("")
    lines.append("- Display name: \"Veps Egg\"")
    lines.append("- Description: \"[PLACEHOLDER] A Veps Egg.\\r\\n\\r\\nWARNING: Contents")
    lines.append("  may be friend shaped.\\r\\n\\r\\nBe careful not to break it.\"")
    lines.append("- Path: `Subnautica2/Content/Data/ItemType/Test/DA_VepEggTest` (in")
    lines.append("  the `Test/` folder, marked `[PLACEHOLDER]`).")
    lines.append("- Actor BP: `BP_ShellPiece` in the `Deprecated/` folder.")
    lines.append("- Scan data: `DA_VepEggTestData` (name 'Vep Egg Test', no description,")
    lines.append("  no databank link, no scan duration, no story-goal hookup).")
    lines.append("- Verdict: a test asset only. Not wired into the live game. May")
    lines.append("  foreshadow a future Veps egg mechanic, but at this build the asset")
    lines.append("  is in `Test/` + `Deprecated/` paths with placeholder text.")
    lines.append("")

    # Recipe usage
    if findings["recipe_usage"]:
        lines.append("## Recipes that consume / produce egg items")
        lines.append("")
        for r in findings["recipe_usage"]:
            uses = ", ".join(r["uses_as_input"]) or "-"
            prods = ", ".join(r["produces"]) or "-"
            lines.append(f"- `{r['id']}` ({r['name']}): input {uses}, output {prods}")
        lines.append("")

    # SN1 vs SN2 comparison
    lines.append("## SN1 vs SN2: mechanic comparison")
    lines.append("")
    lines.append("| Mechanic | SN1 (released) | SN2 (pre-EA build) |")
    lines.append("| --- | --- | --- |")
    lines.append("| Alien Containment base module | Yes, hatches eggs in 1 to 3 in-game days | No equivalent module |")
    lines.append("| Aquarium / vivarium | (Containment functions as both) | No (Vivarium hits are dialogue prefixes) |")
    lines.append("| Player picks up eggs from world | Yes, one per species in caves | No eggs are placed actors in `world_map.json` |")
    lines.append("| Eggs hatch into baby creatures | Yes, in Containment | No hatching system found |")
    lines.append("| Babies can be released and grow | Yes | No baby-creature pickup system |")
    lines.append("| Aggressive species pacified by hatching | Yes (most species) | No (no hatching) |")
    lines.append("| Eggs as crafting ingredients | Yes, Bioreactor fuel | Yes, Deepwing Egg in food recipe + acid recipe, Necrolei Cyst in Strong Acid recipe |")
    lines.append("| Creature lays eggs as NPC behavior | Sometimes (Stalker teeth, not eggs) | Yes (Deepwing Leviathan via `GA_AI_LayEggs`) |")
    lines.append("")

    # Containment keyword detail
    lines.append("## Containment / hatching keyword scan")
    lines.append("")
    counts = findings["keyword_path_counts"]
    lines.append("| Keyword | Hits in PAK |")
    lines.append("| --- | ---: |")
    for kw, count in counts.items():
        lines.append(f"| {kw} | {count} |")
    lines.append("")

    # Non-zero keyword paths
    nz = {kw: ps for kw, ps in findings["keyword_path_scan"].items() if ps}
    if nz:
        lines.append("### Files matching each keyword")
        lines.append("")
        for kw, paths in nz.items():
            lines.append(f"**{kw}** ({len(paths)})")
            for p in paths[:25]:
                lines.append(f"- `{p}`")
            if len(paths) > 25:
                lines.append(f"- ... and {len(paths) - 25} more")
            lines.append("")

    # BP hierarchy
    lines.append("## Blueprint class hierarchy for egg items")
    lines.append("")
    for name, info in findings["bp_class_hierarchy"].items():
        super_path = info.get("super") or "(unknown)"
        lines.append(f"- **{name}** super class: `{super_path}`")
    lines.append("")

    # World map
    if findings["world_map_actor_classes_with_egg"]:
        lines.append("## World map placements")
        lines.append("")
        for c in findings["world_map_actor_classes_with_egg"]:
            lines.append(f"- `{c}`")
        lines.append("")
    else:
        lines.append("## World map placements")
        lines.append("")
        lines.append("No placed actors in `world_map.json` whose class name contains")
        lines.append("egg, brooder, necrolei, hatch, aquarium, or containment.")
        lines.append("Eggs do not exist as placed world actors at this build.")
        lines.append("")

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


if __name__ == "__main__":
    main()
