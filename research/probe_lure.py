"""Reproducible probe for SN2 lure / decoy / stun / non-lethal subsystems.

Walks the PAK for keyword-matched assets, then extracts:
- Stimulus emitter components (Shape, StimulusDuration, Tags)
- GameplayEffect modifiers + durations
- Stimulus sensor data (UWEStimulusDataAsset.Sensors)
- BioAbility data (cooldown, unlock cost)
- Behavior trees (Tasks / Services / Tags)
- Item / tool blueprints that emit stimuli

Outputs:
- D:/subnautica/miner/out/research/lure_subdue.json  (structured)
- D:/subnautica/miner/out/research/lure_subdue.md     (human-readable)
"""
from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

MINER_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MINER_ROOT))
logging.basicConfig(level=logging.WARNING)

from provider import create_provider
from helpers import (
    safe_load_package, find_export, find_exports_by_class, prop, prop_str,
    prop_array, prop_object_path, prop_tags, prop_float, prop_bool,
    unwrap_struct, obj_ref_path, short_name_from_path, array_values,
    _export_class, extract_gameplay_tags,
)

OUT_DIR = MINER_ROOT / "out" / "research"
OUT_DIR.mkdir(parents=True, exist_ok=True)

prov = create_provider()


# ---------- generic helpers on top of helpers.py ----------

def all_props(export) -> dict:
    """Dump a property bag to a plain dict of repr-friendly strings."""
    out = {}
    props = getattr(export, "Properties", None)
    if props is None:
        return out
    for tag in props:
        name = tag.Name.Text
        val = tag.Tag.GenericValue if tag.Tag is not None else None
        out[name] = val
    return out


def find_default(package):
    for ex in package.GetExports():
        if str(ex.Name).startswith("Default__"):
            return ex
    return None


def extract_struct_tags(struct_val) -> list[str]:
    """Get any TagName / GameplayTags array out of an FScriptStruct value."""
    return extract_gameplay_tags(struct_val)


def extract_inherited_tags(export, prop_name: str) -> list[str]:
    """An FInheritedTagContainer has `.CombinedTags` and `.Added` arrays.

    GE assets store `InheritableAssetTags` and `InheritableGameplayEffectTags`
    as FInheritedTagContainer. We pull the union of CombinedTags / Added.
    """
    val = prop(export, prop_name)
    if val is None:
        return []
    out: list[str] = []
    u = unwrap_struct(val)
    if u is not None:
        for k in ("CombinedTags", "Added"):
            inner = prop(u, k)
            if inner is not None:
                out.extend(extract_gameplay_tags(inner))
    # Also try direct tag extraction on the value
    if not out:
        out = extract_gameplay_tags(val)
    # Dedupe
    seen = set()
    res = []
    for t in out:
        if t not in seen:
            seen.add(t)
            res.append(t)
    return res


def extract_scalable_float(val) -> float | None:
    u = unwrap_struct(val)
    if u is None:
        return None
    # Top-level Value
    direct = prop(u, "Value")
    if direct is not None:
        try:
            return float(direct)
        except (TypeError, ValueError):
            pass
    # ScalableFloatMagnitude.Value
    sf = unwrap_struct(prop(u, "ScalableFloatMagnitude"))
    if sf is not None:
        v = prop(sf, "Value")
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def extract_duration_magnitude(default_ex) -> dict | None:
    """A GE has DurationMagnitude → ScalableFloatMagnitude.Value (seconds)."""
    val = prop(default_ex, "DurationMagnitude")
    if val is None:
        return None
    f = extract_scalable_float(val)
    if f is None:
        return None
    return {"value": f}


def extract_period(default_ex) -> float | None:
    val = prop(default_ex, "Period")
    if val is None:
        return None
    return extract_scalable_float(val)


def extract_shape(default_ex) -> dict | None:
    """A stimulus shape sometimes has Radius / HalfHeight / Angle."""
    for ex in [default_ex]:
        val = prop(ex, "Shape")
        if val is None:
            continue
        u = unwrap_struct(val)
        if u is None:
            continue
        out = {}
        for k in ("Radius", "Angle", "HalfHeight", "Distance", "Range",
                  "OuterRadius", "InnerRadius", "Length"):
            v = prop(u, k)
            if v is not None:
                try:
                    out[k] = float(v)
                except (TypeError, ValueError):
                    out[k] = str(v)
        if out:
            return out
    return None


# ---------- per-asset extractors ----------

def dump_stimulus_emitter_effect(asset_path: str) -> dict | None:
    """A GE that carries a UWEStimulusEmitterEffectComponent export.

    Properties of interest:
      StimulusDuration (float) - how long the stim lingers in the perception system
      Shape (struct) - cone/sphere/etc with radii
      AssetTagsGameplayEffectComponent.InheritableAssetTags - the stim tag itself
      DurationPolicy / DurationMagnitude / Period - effect lifetime
    """
    pkg = safe_load_package(prov, asset_path)
    if pkg is None:
        return None

    default_ex = find_default(pkg)
    emitters = []
    for ex in pkg.GetExports():
        if _export_class(ex) == "UWEStimulusEmitterEffectComponent":
            emitter = {
                "stimulus_duration": prop_float(ex, "StimulusDuration"),
                "shape": extract_shape(ex),
            }
            emitters.append(emitter)

    asset_tags: list[str] = []
    target_tags: list[str] = []
    granted_tags: list[str] = []
    for ex in pkg.GetExports():
        cls = _export_class(ex)
        if cls == "AssetTagsGameplayEffectComponent":
            asset_tags.extend(extract_inherited_tags(ex, "InheritableAssetTags"))
        elif cls == "TargetTagsGameplayEffectComponent":
            target_tags.extend(extract_inherited_tags(ex, "InheritableGrantedTagsContainer"))

    duration_policy = None
    duration_magnitude = None
    period = None
    inheritable_ge_tags = []
    granted_owned_tags = []
    if default_ex is not None:
        dp = prop(default_ex, "DurationPolicy")
        duration_policy = str(dp) if dp is not None else None
        duration_magnitude = extract_duration_magnitude(default_ex)
        period = extract_period(default_ex)
        inheritable_ge_tags = extract_inherited_tags(default_ex, "InheritableGameplayEffectTags")
        granted_owned_tags = extract_inherited_tags(default_ex, "InheritableOwnedTagsContainer")

    return {
        "asset": asset_path,
        "id": short_name_from_path(asset_path),
        "duration_policy": duration_policy,
        "duration_magnitude": duration_magnitude,
        "period": period,
        "asset_tags": asset_tags,
        "inheritable_ge_tags": inheritable_ge_tags,
        "granted_owned_tags": granted_owned_tags,
        "target_granted_tags": target_tags,
        "stimulus_emitters": emitters,
    }


def dump_stimulus_data_asset(asset_path: str) -> dict | None:
    """A UWEStimulusDataAsset has a Sensors array of FUWEStimulusSensor structs.

    Each sensor has exactly two fields in the build:
      Shape (FScriptStruct: Radius, HalfAngle, Transform)
      StimulusTypeTags (FGameplayTagContainer, e.g. 'StimulusType.Light')
    """
    pkg = safe_load_package(prov, asset_path)
    if pkg is None:
        return None
    export = find_export(pkg, class_substring="UWEStimulusDataAsset")
    if export is None:
        return None
    sensors = []
    for el in prop_array(export, "Sensors"):
        gv = getattr(el, "GenericValue", el)
        u = unwrap_struct(gv)
        if u is None:
            continue
        s = {}
        # Shape: read Radius / HalfAngle from FStructFallback
        shape_val = prop(u, "Shape")
        shape_u = unwrap_struct(shape_val) if shape_val is not None else None
        if shape_u is not None:
            shape: dict = {}
            for k in ("Radius", "HalfAngle", "InnerRadius", "OuterRadius", "Length"):
                v = prop(shape_u, k)
                if v is not None:
                    try:
                        shape[k] = float(v)
                    except (TypeError, ValueError):
                        shape[k] = str(v)
            if shape:
                s["shape"] = shape
        # StimulusTypeTags: a FGameplayTagContainer; extract_gameplay_tags
        # falls back to parsing "Tag1, Tag2 (FGameplayTagContainer)" from str()
        stt_val = prop(u, "StimulusTypeTags")
        if stt_val is not None:
            tags = extract_gameplay_tags(stt_val)
            if tags:
                s["stimulus_type_tags"] = tags
        if s:
            sensors.append(s)
    return {
        "asset": asset_path,
        "id": short_name_from_path(asset_path),
        "sensor_count": len(sensors),
        "sensors": sensors,
    }


def _safe_tags(v) -> list[str]:
    try:
        return extract_gameplay_tags(v)
    except Exception:
        return []


def dump_behavior_tree(asset_path: str) -> dict | None:
    pkg = safe_load_package(prov, asset_path)
    if pkg is None:
        return None
    nodes = []
    for ex in pkg.GetExports():
        cls = _export_class(ex)
        name = str(ex.Name)
        if cls in ("BehaviorTree", "BlackboardData") or "Default__" in name:
            continue
        node = {"name": name, "class": cls}
        # Pull tag query data
        for key in ("Tag", "SoundTag", "QueryTags", "GameplayTagQuery"):
            v = prop(ex, key)
            if v is None:
                continue
            tags = _safe_tags(v)
            if tags:
                node[key] = tags
        # Pull blackboard / action data refs
        for key in ("ActionData", "BlackboardKey", "TargetSelector",
                    "TimeLimit", "ApproachDistance", "StartForceProportion",
                    "FinishForceProportion"):
            v = prop(ex, key)
            if v is None:
                continue
            sf = extract_scalable_float(v) if hasattr(v, "GenericValue") or hasattr(v, "Properties") else None
            if sf is not None:
                node[key] = sf
                continue
            ref = obj_ref_path(v) if hasattr(v, "ResolvedObject") else None
            if ref:
                node[key] = ref
                continue
            try:
                node[key] = float(v)
                continue
            except (TypeError, ValueError):
                pass
            node[key] = str(v)
        if len(node) > 2:
            nodes.append(node)
    return {
        "asset": asset_path,
        "id": short_name_from_path(asset_path),
        "node_count": len(nodes),
        "nodes": nodes,
    }


def dump_bioability_data(asset_path: str) -> dict | None:
    pkg = safe_load_package(prov, asset_path)
    if pkg is None:
        return None
    ex = find_export(pkg, class_substring="UWEBioAbilityData")
    if ex is None:
        return None
    # UnlockCost is a struct: { CurrencyTag, Quantity? } — pull what we can
    unlock = {}
    uc_val = prop(ex, "UnlockCost")
    uc_u = unwrap_struct(uc_val) if uc_val is not None else None
    if uc_u is not None:
        ct = prop(uc_u, "CurrencyTag")
        if ct is not None:
            ct_tags = extract_gameplay_tags(ct)
            if ct_tags:
                unlock["currency_tag"] = ct_tags[0]
        q = prop(uc_u, "Quantity")
        if q is not None:
            try:
                unlock["quantity"] = int(q)
            except (TypeError, ValueError):
                unlock["quantity"] = str(q)
    return {
        "asset": asset_path,
        "id": short_name_from_path(asset_path),
        "name": prop_str(ex, "Name"),
        "description": prop_str(ex, "Description"),
        "icon": prop_object_path(ex, "Icon"),
        "bio_ability": prop_object_path(ex, "BioAbility"),
        "type": str(prop(ex, "BioAbilityType") or ""),
        "ability_tag": (prop_tags(ex, "AbilityTag") or [None])[0],
        "unlock_cost": unlock or None,
    }


# ---------- top-level walks ----------

KEYWORDS_PRIMARY = [
    "Stun", "Lure", "Bait", "Decoy", "Stasis", "Pacify", "Calm", "Sedate",
    "Repulsion", "Propulsion", "Subdue", "Pheromone", "Scent", "Knockout",
    "Snare", "Trap", "Distract", "Frighten", "Scare", "Sonar",
    "Sonic", "Resonator", "Tractor", "Push", "Pull", "Grab", "Net",
]

KEYWORDS_AI = ["AttractedStimulus", "FleeStimulus", "ContinuousFollow",
               "SymbioteCall", "LureStimulus"]


def walk_paths() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {kw: [] for kw in KEYWORDS_PRIMARY + KEYWORDS_AI}
    for path in prov.Files.Keys:
        if not path.endswith(".uasset"):
            continue
        for kw in out:
            if kw in path:
                out[kw].append(path)
    return out


def find_stimulus_data_assets() -> list[str]:
    return sorted(
        p for p in prov.Files.Keys
        if p.endswith(".uasset") and p.startswith("Subnautica2/Content/Data/Stimulus/")
    )


def find_stimulus_ge_assets() -> list[str]:
    """All GameplayEffects that emit stimuli (everything under Effects/Stimulus/)."""
    return sorted(
        p for p in prov.Files.Keys
        if p.endswith(".uasset") and "Effects/Stimulus/" in p
    )


def find_creature_specific_stims() -> list[str]:
    """Per-creature lure/stun GEs (Sandspear, TwinEels, Cerathecan, etc.)."""
    return sorted(
        p for p in prov.Files.Keys
        if p.endswith(".uasset") and "Effects/Creatures/" in p
        and any(k in p for k in ("Stun", "Lure", "Bait", "Stimulus"))
    )


def find_player_tool_assets() -> list[str]:
    """Look for items / tools that emit stimuli or pacify creatures."""
    return sorted(
        p for p in prov.Files.Keys
        if p.endswith(".uasset") and (
            "Blueprints/Items/Tools/" in p
            or "Blueprints/Items/Carryables/" in p
            or "Data/ItemType/" in p
        )
    )


def find_biomod_data_assets() -> list[str]:
    return sorted(
        p for p in prov.Files.Keys
        if p.endswith(".uasset") and "Data/Biomods/BioAbilityData/" in p
    )


def find_biomod_abilities() -> list[str]:
    return sorted(
        p for p in prov.Files.Keys
        if p.endswith(".uasset") and "Abilities/BioAbilities/" in p
    )


# ---------- main extraction ----------

def main():
    print("=== walking paths ===")
    paths_by_kw = walk_paths()
    for kw, paths in paths_by_kw.items():
        if paths:
            print(f"  {kw}: {len(paths)}")

    # --- 1. Sandspear + named creature lure/stun systems ---
    creature_systems_paths = sorted(set([
        # Lure
        'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Creatures/Sandspear/GE_Sandspear_LureStimulus',
        'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Creatures/Sandspear/GE_Sandpear_Stun',
        # Stun (creature->player or creature->prey)
        'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Creatures/GE_Cerathecan_StunPrey',
        'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Creatures/TwinEels/GE_TwinEels_StunPrey',
        'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Creatures/TwinEels/GE_TwinEels_StunVehicle',
    ]))
    creature_systems = []
    for p in creature_systems_paths:
        d = dump_stimulus_emitter_effect(p)
        if d:
            creature_systems.append(d)

    # --- 2. Generic stimulus GEs (shape/duration library) ---
    generic_ge_paths = find_stimulus_ge_assets()
    generic_ges = []
    for p in generic_ge_paths:
        pkg_path = p[:-7]
        d = dump_stimulus_emitter_effect(pkg_path)
        if d:
            generic_ges.append(d)

    # --- 3. Stimulus sensor data assets ---
    sensor_paths = find_stimulus_data_assets()
    sensor_assets = []
    for p in sensor_paths:
        pkg_path = p[:-7]
        d = dump_stimulus_data_asset(pkg_path)
        if d:
            sensor_assets.append(d)

    # --- 4. Player biomod stim emitters (stun, lure, distract) ---
    biomod_systems = []
    for p in [
        'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Biomods/BioEffects/GE_CreatureStunShortTime',
        'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Stimulus/Biomods/GE_ElectrostaticDischarge_Stimulus',
        'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Biomods/BioEffects/GE_CreatureFollowOnEatOrDrink',
    ]:
        d = dump_stimulus_emitter_effect(p)
        if d:
            biomod_systems.append(d)

    # --- 5. Behavior trees: stunned states & lure behaviors ---
    bt_paths = [
        'Subnautica2/Content/Blueprints/AI/Agents/Common/Behaviors/BT_Stunned',
        'Subnautica2/Content/Blueprints/AI/Agents/Common/Behaviors/SmallCreature/BT_SmallCreatureLure',
        'Subnautica2/Content/Blueprints/AI/Agents/LargeCreature010_JetoCaris/Behaviours/BT_JetoCaris_Stunned',
        'Subnautica2/Content/Blueprints/AI/Agents/CollectorLeviathan/Behaviors/BT_CollectorLeviathanAttractedStimulus',
        'Subnautica2/Content/Blueprints/AI/Agents/LargeCreature021_Sandspear/Services/BTService_Sandspear_LureStimulus',
    ]
    behavior_trees = []
    for p in bt_paths:
        d = dump_behavior_tree(p)
        if d:
            behavior_trees.append(d)

    # --- 6. Bio-ability data assets for non-lethal interaction ---
    bioability_data = []
    for p in [
        'Subnautica2/Content/Data/Biomods/BioAbilityData/Positive/DA_CreatureStunOnStop_BioAbilityData1',
        'Subnautica2/Content/Data/Biomods/BioAbilityData/Positive/DA_CreatureFollowOnEatOrDrink_BioAbilityData',
        'Subnautica2/Content/Data/Biomods/BioAbilityData/Active/DA_EmitSpores_ActiveBioAbility',
        'Subnautica2/Content/Data/Biomods/BioAbilityData/Active/DA_EchoLocation_ActiveBioAbilityData',
    ]:
        d = dump_bioability_data(p)
        if d:
            bioability_data.append(d)

    # Scan ALL biomod DAs by name OR description for non-lethal keywords
    biomod_da_paths = find_biomod_data_assets()
    biomod_keyword_matches = []
    name_or_desc_pat = re.compile(
        r"(?i)stun|scare|lure|calm|pacify|sedate|decoy|repulsion|stasis|"
        r"distract|bait|chum|hypnoti|frighten|attract|follow|pheromone|"
        r"emit|spore|sonar|echo|sonic|repuls"
    )
    for p in biomod_da_paths:
        leaf = p.rsplit("/", 1)[-1]
        pkg_path = p[:-7]
        d = dump_bioability_data(pkg_path)
        if d is None:
            continue
        blob = f"{d['id']} {d.get('name') or ''} {d.get('description') or ''}"
        if name_or_desc_pat.search(blob):
            biomod_keyword_matches.append(d)
    # Dedupe by id
    seen = set()
    biomod_keyword_unique = []
    for d in biomod_keyword_matches:
        if d["id"] in seen:
            continue
        seen.add(d["id"])
        biomod_keyword_unique.append(d)

    # --- 7. Player-side tools: search Items/Tools for stimulus/decoy/etc ---
    item_keyword_matches = []
    for p in find_player_tool_assets():
        leaf = p.rsplit("/", 1)[-1]
        if not re.search(r"(?i)stasis|repulsion|propulsion|decoy|bait|lure|sedate|sonic|resonator|tractor|snare|net|trap", leaf):
            continue
        item_keyword_matches.append(p)

    out_struct = {
        "_meta": {
            "build": "5.6.1-112084 (pre-EA)",
            "extracted_by": "research/probe_lure.py",
            "purpose": "lure / decoy / stun / non-lethal interaction taxonomy",
        },
        "stimulus_taxonomy_notes": {
            "core_class": "UWEStimulusEmitterEffectComponent",
            "host_class": "UWEGameplayEffect (UE GAS)",
            "key_properties": [
                "StimulusDuration  - seconds the stim is broadcast",
                "Shape             - cone/sphere with Radius/Angle/HalfHeight",
                "AssetTags         - the stim tag identifying type (Sound/Light/etc)",
                "DurationPolicy    - Instant / HasDuration / Infinite",
                "DurationMagnitude.ScalableFloatMagnitude.Value - seconds effect lives",
            ],
            "perception_class": "UWEStimulusDataAsset (per-creature Sensors array)",
        },
        "creature_specific_systems": creature_systems,
        "generic_stimulus_effects": generic_ges,
        "perception_sensors": sensor_assets,
        "player_biomod_systems": biomod_systems,
        "behavior_trees": behavior_trees,
        "bioability_stun_data": bioability_data,
        "biomod_keyword_matches": biomod_keyword_unique,
        "player_tool_keyword_matches": item_keyword_matches,
        "keyword_paths": paths_by_kw,
    }

    out_json = OUT_DIR / "lure_subdue.json"
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(out_struct, f, indent=2, default=str)
    print(f"\nwrote {out_json}")
    return out_struct


if __name__ == "__main__":
    main()
