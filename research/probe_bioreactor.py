"""Reproducible probe for the Subnautica 2 Bioreactor.

Loads BP_Bioreactor's CDO, resolves the inventory + power generator
components, scans every UWEItemType for BioFuelConsumption tags, and
writes structured JSON + a human-readable Markdown summary.

Outputs:
  D:/subnautica/miner/out/research/bioreactor.json
  D:/subnautica/miner/out/research/bioreactor.md

Usage:
  cd D:/subnautica/miner
  python research/probe_bioreactor.py
"""
from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

# Make 'helpers', 'provider' importable when run from research/
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.WARNING)

from provider import create_provider  # noqa: E402
from helpers import (  # noqa: E402
    safe_load_package, find_export, prop, prop_str, prop_array, prop_int,
    prop_object_path, prop_tags, unwrap_struct, obj_ref_path,
    short_name_from_path, array_values, _export_class,
    extract_gameplay_tags,
)


OUT_DIR = ROOT / "out" / "research"
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Tunable map decoding
# ---------------------------------------------------------------------------

def _tag_from_struct(kg) -> str | None:
    """Pull a tag name out of a TunableData map key (FStructFallback wrapping
    an FGameplayTag). Returns None if no tag-name can be recovered."""
    tn = getattr(kg, "TagName", None)
    if tn is not None:
        return str(tn)
    inner = getattr(kg, "Properties", None)
    if inner is not None:
        for pt in inner:
            n = pt.Name.Text
            if n in ("TagName", "Tag"):
                v = pt.Tag.GenericValue if pt.Tag is not None else None
                if v is not None:
                    return str(v)
    u = unwrap_struct(kg)
    if u is not None:
        ipr = getattr(u, "Properties", None)
        if ipr is not None:
            for pt in ipr:
                if pt.Name.Text in ("TagName", "Tag"):
                    v = pt.Tag.GenericValue if pt.Tag is not None else None
                    if v is not None:
                        return str(v)
    return None


def tunable_map(m):
    """Decode a UScriptMap<FGameplayTag, float> as a list of (tag, value)."""
    out = []
    props = getattr(m, "Properties", None)
    if props is None:
        return out
    for kv in props:
        tag = _tag_from_struct(kv.Key.GenericValue) or f"<unknown:{repr(kv.Key.GenericValue)[:50]}>"
        vg = kv.Value.GenericValue
        try:
            val = float(vg)
        except Exception:
            val = vg
        out.append((tag, val))
    return out


# ---------------------------------------------------------------------------
# CDO loaders
# ---------------------------------------------------------------------------

def load_bp_bioreactor(prov):
    """Return a dict of every relevant property scraped from BP_Bioreactor."""
    pkg = safe_load_package(prov, 'Subnautica2/Content/Blueprints/BaseBuilding/BP_Bioreactor')
    out = {
        "asset": "Subnautica2/Content/Blueprints/BaseBuilding/BP_Bioreactor",
        "cdo": None,
        "inventory": None,
        "power_generator": None,
    }
    if pkg is None:
        return out

    for ex in pkg.GetExports():
        name = str(ex.Name)
        if name == "Default__BP_Bioreactor_C":
            cue = prop(ex, "ActiveCue")
            cue_tags = extract_gameplay_tags(cue)
            # ActiveCue is an FScriptStruct (FGameplayCueTag) -> unwrap to find TagName
            if not cue_tags and cue is not None:
                u = unwrap_struct(cue)
                if u is not None:
                    tn = None
                    inner = getattr(u, "Properties", None)
                    if inner is not None:
                        for pt in inner:
                            if pt.Name.Text == "TagName":
                                v = pt.Tag.GenericValue if pt.Tag is not None else None
                                if v is not None:
                                    tn = str(v)
                                    break
                    if tn:
                        cue_tags = [tn]
            pcn = prop(ex, "PowerConsumptionNormal")
            pco = prop(ex, "PowerConsumptionOverdrive")
            pcn_u = unwrap_struct(pcn) if pcn is not None else None
            pco_u = unwrap_struct(pco) if pco is not None else None
            out["cdo"] = {
                "active_cue_tag": cue_tags[0] if cue_tags else None,
                "mixer_max_turn_speed": float(prop(ex, "MixerMaxTurnSpeed") or 0.0),
                "power_production_normal": float(prop(pcn_u, "PowerProduction") or 0.0)
                                            if pcn_u is not None else None,
                "power_production_overdrive": float(prop(pco_u, "PowerProduction") or 0.0)
                                               if pco_u is not None else None,
                "net_update_frequency": float(prop(ex, "NetUpdateFrequency") or 0.0),
            }
        elif name == "InventoryComponent":
            allowed = prop(ex, "AllowedTags")
            out["inventory"] = {
                "max_items": int(prop(ex, "MaxItems") or 0),
                "allowed_tags": extract_gameplay_tags(allowed),
                "allowed_tags_raw": str(allowed) if allowed is not None else None,
            }
        elif name == "PowerGeneratorComponent":
            psc = prop(ex, "PowerSimulationClass")
            psc_path = obj_ref_path(psc) if psc is not None else None
            out["power_generator"] = {
                "power_simulation_class": psc_path,
                "is_native_class": bool(psc_path and "/Script/" in psc_path),
            }
    return out


def load_recipe(prov):
    pkg = safe_load_package(prov, 'Subnautica2/Content/Data/CraftingRecipes/Builder/DA_BioreactorRecipe')
    if pkg is None:
        return None
    ex = find_export(pkg, class_substring="UWECraftingRecipe")
    if ex is None:
        return None
    reqs = []
    for el in array_values(prop_array(ex, "Requirements")):
        u = unwrap_struct(el)
        if u is None:
            continue
        reqs.append({
            "item_type": prop_object_path(u, "ItemType"),
            "count": int(prop(u, "NumItems") or prop(u, "Count") or 1),
        })
    return {
        "asset": "Subnautica2/Content/Data/CraftingRecipes/Builder/DA_BioreactorRecipe",
        "name": prop_str(ex, "Name"),
        "description": prop_str(ex, "Description"),
        "crafting_time_seconds": float(prop(ex, "CraftingTime") or 0.0),
        "requirements": reqs,
    }


def load_construct_data(prov):
    pkg = safe_load_package(prov, 'Subnautica2/Content/Data/BaseBuilding/BuilderActions/DA_BioreactorConstructData')
    if pkg is None:
        return None
    ex = find_export(pkg)
    return {
        "asset": "Subnautica2/Content/Data/BaseBuilding/BuilderActions/DA_BioreactorConstructData",
        "name": prop_str(ex, "Name"),
        "description": prop_str(ex, "Description"),
        "secondary_description": prop_str(ex, "SecondaryDescription"),
        "power_generation_text": prop_str(ex, "PowerGenerationText"),
        "category": str(prop(ex, "Category") or ""),
    }


def load_databank(prov):
    pkg = safe_load_package(prov, 'Subnautica2/Content/Data/DatabankEntry/BaseBuilding/DA_BasePiece_Bioreactor_DatabankEntry')
    if pkg is None:
        return None
    ex = find_export(pkg, class_substring="UWEDatabankEntry")
    return {
        "asset": "Subnautica2/Content/Data/DatabankEntry/BaseBuilding/DA_BasePiece_Bioreactor_DatabankEntry",
        "title": prop_str(ex, "EntryTitle"),
        "text": prop_str(ex, "EntryText"),
    }


def load_scan(prov):
    pkg = safe_load_package(prov, 'Subnautica2/Content/Data/ScanData/BaseBuilding/DA_BasePiece_Bioreactor_ScanData')
    if pkg is None:
        return None
    ex = find_export(pkg, class_substring="UWEScanData")
    return {
        "asset": "Subnautica2/Content/Data/ScanData/BaseBuilding/DA_BasePiece_Bioreactor_ScanData",
        "name": prop_str(ex, "Name"),
        "scan_duration_seconds": float(prop(ex, "ScanDuration") or 0.0),
        "num_required": int(prop(ex, "NumRequired") or 0),
    }


def load_string_table(prov):
    pkg = safe_load_package(prov, 'Subnautica2/Content/StringTables/ST_Bioreactor')
    if pkg is None:
        return None
    # Use the existing extractor logic
    sys.path.insert(0, str(ROOT))
    from extractors.string_tables import _extract_table  # type: ignore
    for ex in pkg.GetExports():
        cls = _export_class(ex)
        if "StringTable" in cls:
            entries = _extract_table(ex)
            if entries:
                return {
                    "asset": "Subnautica2/Content/StringTables/ST_Bioreactor",
                    "entries": entries,
                }
    return None


# ---------------------------------------------------------------------------
# Item scan
# ---------------------------------------------------------------------------

def scan_eligible_items(prov):
    """Scan every UWEItemType for BioFuelConsumption tags and TunableData.

    The Bioreactor inventory's AllowedTags is `ItemType.Flora, ItemType.Fauna,
    ItemType.Fuel`. In this build no item actually carries those tags. Items
    that are bioreactor-eligible all carry an
    `ItemType.TunableData.BioFuelConsumption.{Short, Medium, Long, ExtraLong}`
    tag, which is the burn-time / energy classification used by the native
    `SN2BioreactorSimulation` class.

    Returns a list of dicts: {id, name, asset, biofuel_tier, gameplay_tags, tunable_data}.
    """
    all_keys = list(prov.Files.Keys)
    item_keys = sorted(k for k in all_keys
                       if k.startswith("Subnautica2/Content/Data/ItemType/")
                       and k.endswith(".uasset"))

    tier_re = re.compile(r"ItemType\.TunableData\.BioFuelConsumption\.([A-Za-z]+)$")
    out = []
    for ik in item_keys:
        pkg = safe_load_package(prov, ik[:-7])
        if pkg is None:
            continue
        ex = find_export(pkg, class_substring="UWEItemType")
        if ex is None:
            continue
        gtags = extract_gameplay_tags(prop(ex, "GameplayTags"))
        typetag = extract_gameplay_tags(prop(ex, "TypeTag"))
        all_tags = list(gtags) + list(typetag)

        tier = None
        for t in all_tags:
            m = tier_re.match(t)
            if m:
                tier = m.group(1)
                break
        if tier is None:
            continue  # not eligible

        td = prop(ex, "TunableData")
        tunable_pairs = tunable_map(td) if td is not None else []
        out.append({
            "id": short_name_from_path(ik[:-7]),
            "asset": ik[:-7],
            "name": prop_str(ex, "Name"),
            "description": prop_str(ex, "ItemDescription"),
            "biofuel_tier": tier,
            "gameplay_tags": gtags,
            "type_tag": typetag,
            "tunable_data": [{"tag": t, "value": v} for t, v in tunable_pairs],
        })
    return out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main():
    prov = create_provider()

    bp = load_bp_bioreactor(prov)
    recipe = load_recipe(prov)
    construct = load_construct_data(prov)
    databank = load_databank(prov)
    scan = load_scan(prov)
    st = load_string_table(prov)
    eligible = scan_eligible_items(prov)

    tier_counts = {}
    for it in eligible:
        tier_counts[it["biofuel_tier"]] = tier_counts.get(it["biofuel_tier"], 0) + 1

    findings = {
        "build": "Subnautica 2 pre-Early Access (5.6.1-112084)",
        "blueprint": bp,
        "recipe": recipe,
        "construct_data": construct,
        "databank": databank,
        "scan_data": scan,
        "string_table": st,
        "accepts_eggs": _check_accepts_eggs(eligible),
        "summary": {
            "max_inventory_slots": (bp.get("inventory") or {}).get("max_items"),
            "power_production_normal": (bp.get("cdo") or {}).get("power_production_normal"),
            "power_production_overdrive": (bp.get("cdo") or {}).get("power_production_overdrive"),
            "power_generation_text": (construct or {}).get("power_generation_text"),
            "allowed_tags_filter": (bp.get("inventory") or {}).get("allowed_tags"),
            "power_simulation_class": (bp.get("power_generator") or {}).get("power_simulation_class"),
            "biofuel_tier_counts": tier_counts,
            "eligible_item_count": len(eligible),
        },
        "eligible_items": eligible,
    }

    json_path = OUT_DIR / "bioreactor.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(findings, f, indent=2, ensure_ascii=False)
    print(f"wrote {json_path}")

    md_path = OUT_DIR / "bioreactor.md"
    md_path.write_text(_render_markdown(findings), encoding="utf-8")
    print(f"wrote {md_path}")


def _check_accepts_eggs(eligible):
    """SN1 famously accepted eggs. Does SN2 list any egg in the eligible set?"""
    egg_items = [
        it for it in eligible
        if "egg" in (it.get("id") or "").lower() or "egg" in (it.get("name") or "").lower()
    ]
    return {
        "count": len(egg_items),
        "items": [{"id": it["id"], "name": it["name"], "tier": it["biofuel_tier"]}
                  for it in egg_items],
    }


def _render_markdown(f):
    bp = f["blueprint"]
    cdo = bp.get("cdo") or {}
    inv = bp.get("inventory") or {}
    pg = bp.get("power_generator") or {}
    recipe = f["recipe"] or {}
    construct = f["construct_data"] or {}
    databank = f["databank"] or {}
    scan = f["scan_data"] or {}
    st = f["string_table"] or {}
    tier_counts = f["summary"]["biofuel_tier_counts"]

    md = []
    md.append("# Subnautica 2: Bioreactor")
    md.append("")
    md.append(f"Build: {f['build']}")
    md.append("")
    md.append("## Top-level numbers")
    md.append("")
    md.append("| Field | Value |")
    md.append("|---|---|")
    md.append(f"| Inventory slots (`MaxItems`) | {inv.get('max_items')} |")
    md.append(f"| Power production, Normal mode | {cdo.get('power_production_normal')} |")
    md.append(f"| Power production, Overdrive mode | {cdo.get('power_production_overdrive')} |")
    md.append(f"| Power generation UI text | `{construct.get('power_generation_text')}` |")
    md.append(f"| Mixer turn speed | {cdo.get('mixer_max_turn_speed')} |")
    md.append(f"| Active gameplay cue tag | `{cdo.get('active_cue_tag')}` |")
    md.append(f"| Power simulation class | `{pg.get('power_simulation_class')}` (native C++) |")
    md.append("")
    md.append("## Input filter")
    md.append("")
    md.append(f"`UWEInventoryComponent.AllowedTags` = `{', '.join(inv.get('allowed_tags') or []) or '(empty)'}`")
    md.append("")
    md.append("Important: in this build NO `UWEItemType` asset actually carries `ItemType.Flora`, "
              "`ItemType.Fauna`, or `ItemType.Fuel`. Items that the bioreactor accepts in practice are "
              "instead tagged with `ItemType.TunableData.BioFuelConsumption.{Short, Medium, Long, ExtraLong}`. "
              "The native `SN2BioreactorSimulation` class is what reads those tags. Conversion rates and "
              "burn durations per tier live in C++, not in any data asset shipped with the pre-Early "
              "Access PAK.")
    md.append("")
    md.append("## Eligible items by burn-time tier")
    md.append("")
    md.append(f"Total eligible items: **{f['summary']['eligible_item_count']}**")
    md.append("")
    md.append("| Tier | Count |")
    md.append("|---|---:|")
    for tier in ("Short", "Medium", "Long", "ExtraLong"):
        md.append(f"| {tier} | {tier_counts.get(tier, 0)} |")
    md.append("")
    for tier in ("Short", "Medium", "Long", "ExtraLong"):
        items = [it for it in f["eligible_items"] if it["biofuel_tier"] == tier]
        if not items:
            continue
        md.append(f"### {tier} ({len(items)})")
        md.append("")
        md.append("| Item ID | Display name |")
        md.append("|---|---|")
        for it in sorted(items, key=lambda x: (x.get("name") or "", x["id"])):
            n = (it.get("name") or "").replace("|", "/")
            md.append(f"| `{it['id']}` | {n} |")
        md.append("")
    md.append("## SN1 vs SN2 comparison")
    md.append("")
    md.append("- Subnautica 1: each bioreactor-eligible item had a numeric `EnergyValue` baked onto "
              "the item, plus a single global conversion rate.")
    md.append("- Subnautica 2: per-item energy is NOT a free-form scalar. Each accepted item carries "
              "ONE of four discrete gameplay tags "
              "(`ItemType.TunableData.BioFuelConsumption.Short / Medium / Long / ExtraLong`). The "
              "native `SN2BioreactorSimulation` translates each tier into burn time and power. The "
              "exact per-tier numbers are not exposed in any data asset in this build.")
    md.append("- The bioreactor itself has TWO operating modes with explicit power-production "
              f"floats: Normal = {cdo.get('power_production_normal')}, Overdrive = "
              f"{cdo.get('power_production_overdrive')}. The construct UI lists the user-facing "
              f"range as `{construct.get('power_generation_text')}`.")
    md.append("")
    md.append("## Does it accept eggs (SN1 compatibility check)?")
    md.append("")
    eggs = f["accepts_eggs"]
    md.append(f"Egg items present in the BioFuelConsumption-tagged set: **{eggs['count']}**")
    if eggs["items"]:
        md.append("")
        md.append("| Item ID | Name | Tier |")
        md.append("|---|---|---|")
        for e in eggs["items"]:
            md.append(f"| `{e['id']}` | {e['name']} | {e['tier']} |")
    md.append("")
    md.append("## Recipe")
    md.append("")
    md.append(f"- Name: {recipe.get('name')}")
    md.append(f"- Description: {recipe.get('description')}")
    md.append(f"- Crafting time: {recipe.get('crafting_time_seconds')} seconds")
    md.append("- Requirements:")
    for r in (recipe.get("requirements") or []):
        md.append(f"    - {r['item_type']} x{r['count']}")
    md.append("")
    md.append("## Construct (builder) data")
    md.append("")
    md.append(f"- Description: {construct.get('description')}")
    md.append(f"- Secondary description: {construct.get('secondary_description')}")
    md.append(f"- Power generation text: `{construct.get('power_generation_text')}`")
    md.append(f"- Category: {construct.get('category')}")
    md.append("")
    md.append("## Scan data")
    md.append("")
    md.append(f"- Scan duration: {scan.get('scan_duration_seconds')} seconds")
    md.append(f"- Fragments required: {scan.get('num_required')}")
    md.append("")
    md.append("## Databank entry")
    md.append("")
    md.append(f"Title: {databank.get('title')}")
    md.append("")
    md.append("Text:")
    md.append("")
    text = databank.get("text") or ""
    # Source uses U+2014 (em-dash) as a bullet glyph. The user explicitly
    # forbids em-dashes in user-facing output, so replace with a regular
    # ASCII dash. Also swap any U+FFFD replacement char that came through
    # from a non-UTF8 decode.
    text_clean = text.replace("—", "-").replace("–", "-").replace("�", "-")
    for line in text_clean.split("\n"):
        md.append(f"> {line}")
    md.append("")
    md.append("## String table (ST_Bioreactor)")
    md.append("")
    md.append("| Key | Value |")
    md.append("|---|---|")
    for k, v in (st.get("entries") or {}).items():
        md.append(f"| {k} | {v} |")
    md.append("")
    md.append("## Unknown / not in this build")
    md.append("")
    md.append("- Per-tier (Short / Medium / Long / ExtraLong) energy and burn-time values. These "
              "are bound inside `SN2BioreactorSimulation` (C++ native class at "
              "`/Script/Subnautica2.SN2BioreactorSimulation`) and are not present as any DataTable, "
              "CurveTable, or DataAsset in the PAK.")
    md.append("- Per-item `EnergyValue` field as it existed in Subnautica 1: unknown (not in this build). "
              "Items are bucketed by tag instead of carrying numeric energy.")
    md.append("- `ConversionDuration` per item or per tier: unknown (not in this build).")
    md.append("")
    return "\n".join(md)


if __name__ == "__main__":
    main()
