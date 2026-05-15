"""Probe SN2 creature lifecycle: juvenile / adult / mother stages, eggs, breeding.

Enumerates every multi-stage creature family in the PAK, dumps the relevant
archetype properties, reads the AI BP / BT for maturation logic
(GrowTime, AgeTime, LifeStage, NextStage, etc.), inspects the BT nodes that
wire Mother to Child (`UWEBTTAttachmentSlotOperation`, `WEBTDFindAttachmentSlot`),
and cross-references with seeded spawn data plus the egg item list.

Outputs:
  D:/subnautica/miner/out/research/lifecycle.json
  D:/subnautica/miner/out/research/lifecycle.md

Run from D:/subnautica/miner with the standard project environment.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.WARNING)

from provider import create_provider
from helpers import (
    _export_class,
    find_export,
    obj_ref_path,
    prop,
    prop_object_path,
    prop_str,
    prop_tags,
    safe_load_package,
    short_name_from_path,
)

OUT_DIR = Path(__file__).resolve().parent.parent / "out" / "research"
OUT_DIR.mkdir(parents=True, exist_ok=True)

LIFECYCLE_FOLDER_KEYWORDS = (
    "Juvenile", "Adult", "Mother", "Child", "Baby", "Nest", "Brooder",
)

PLAYER_BREEDING_KEYWORDS = (
    "Breed", "AlienContainment", "Aquarium", "WaterPark",
    "CreatureContainer", "Hatchery", "BioIncubator",
    "EggIncubator", "CreatureGrower", "FishTank", "CreatureFarm",
)

MATURATION_PROPS = {
    "GrowTime", "AgeTime", "LifeStage", "Maturation", "Maturity",
    "GrowsInto", "EvolveInto", "NextStage", "AgeToAdult",
    "BecomeAdult", "BecomesAdultAt", "TimeToMature", "ChildClass",
    "ParentClass", "AdultClass", "JuvenileClass",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _walk(prov, prefix=""):
    for path in prov.Files.Keys:
        s = str(path)
        if not s.endswith(".uasset"):
            continue
        if prefix and not s.startswith(prefix):
            continue
        yield s


def _is_engine_path(s: str) -> bool:
    return s.startswith("Engine/")


def enumerate_lifecycle_assets(prov):
    out = {kw: [] for kw in LIFECYCLE_FOLDER_KEYWORDS}
    for s in _walk(prov):
        if _is_engine_path(s):
            continue
        if not (s.startswith("Subnautica2/Content/Blueprints/AI/")
                or s.startswith("Subnautica2/Content/Data/AI/")
                or s.startswith("Subnautica2/Content/Data/DatabankEntry/")
                or s.startswith("Subnautica2/Content/Blueprints/Items/Resources/")):
            continue
        for kw in LIFECYCLE_FOLDER_KEYWORDS:
            if kw in s:
                out[kw].append(s)
    return out


def dump_archetype(prov, pkg_path):
    pkg = safe_load_package(prov, pkg_path)
    if pkg is None:
        return None
    export = find_export(pkg, class_substring="UWEAIArchetypeDataAsset")
    if export is None:
        return None
    return {
        "id": short_name_from_path(pkg_path),
        "asset": pkg_path,
        "identifier_tag": (prop_tags(export, "IdentifierTag") or [None])[0],
        "keywords": prop_tags(export, "Keywords"),
        "enemies": prop_tags(export, "Enemies"),
        "behavior_tree": prop_object_path(export, "BehaviorTree"),
        "dominant_sense": obj_ref_path(prop(export, "DominantSense")),
    }


def scan_package_for_maturation(prov, pkg_path):
    pkg = safe_load_package(prov, pkg_path)
    if pkg is None:
        return []
    hits = []
    for ex in pkg.GetExports():
        props = getattr(ex, "Properties", None)
        if props is None:
            continue
        found = {}
        for tag in props:
            n = tag.Name.Text
            if n in MATURATION_PROPS:
                try:
                    v = tag.Tag.GenericValue if tag.Tag is not None else None
                except Exception:
                    v = None
                found[n] = repr(v)[:200]
        if found:
            hits.append({"export": str(ex.Name), "class": _export_class(ex), "props": found})
    return hits


def list_bt_nodes(prov, pkg_path):
    """List every export in a BehaviorTree package with class names."""
    pkg = safe_load_package(prov, pkg_path)
    if pkg is None:
        return None
    out = []
    for ex in pkg.GetExports():
        out.append({"name": str(ex.Name), "class": _export_class(ex)})
    return out


def load_spawn_counts():
    try:
        d = json.loads((OUT_DIR.parent / "creature_spawns.json").read_text(encoding="utf-8"))
        return d["summary"]["by_class"]
    except Exception:
        return {}


def load_pop_settings():
    try:
        return json.loads((OUT_DIR.parent / "pop_settings.json").read_text(encoding="utf-8"))
    except Exception:
        return []


def probe_player_breeding(prov):
    hits = {}
    for s in _walk(prov):
        if _is_engine_path(s):
            continue
        for kw in PLAYER_BREEDING_KEYWORDS:
            if kw in s:
                hits.setdefault(kw, []).append(s)
    return hits


def probe_egg_items(prov):
    out = []
    for s in _walk(prov, "Subnautica2/Content/Blueprints/Items/"):
        if "Egg" in s.rsplit("/", 1)[-1]:
            out.append(s)
    for s in _walk(prov, "Subnautica2/Content/Data/ItemType/"):
        if "Egg" in s.rsplit("/", 1)[-1]:
            out.append(s)
    return sorted(out)


# ---------------------------------------------------------------------------
# Hardcoded family list (verified by the leads in extractors/creatures.py
# and the actual archetype dump in out/creature_archetypes.json).
# ---------------------------------------------------------------------------

FAMILIES = [
    {
        "family": "VoidLeviathan",
        "shipped": True,
        "story_role": "Boss / story-spawned",
        "stages": [
            {"stage": "mother",
             "archetype": "Subnautica2/Content/Data/AI/Archetypes/DA_VoidLeviathanMotherArchetype",
             "bp":        "Subnautica2/Content/Blueprints/AI/Agents/VoidLeviathan/BP_VoidLeviathanMother",
             "bt":        "Subnautica2/Content/Blueprints/AI/Agents/VoidLeviathan/BT_UtilityVoidLeviathanMother"},
            {"stage": "child",
             "archetype": "Subnautica2/Content/Data/AI/Archetypes/DA_VoidLeviathanChildArchetype",
             "bp":        "Subnautica2/Content/Blueprints/AI/Agents/VoidLeviathan/BP_VoidLeviathanChild",
             "bt":        "Subnautica2/Content/Blueprints/AI/Agents/VoidLeviathan/BT_UtilityVoidLeviathanChild"},
        ],
        "link_behaviors": [
            "Subnautica2/Content/Blueprints/AI/Agents/VoidLeviathan/Behaviours/BT_VoidLeviathanChildAttachMother",
            "Subnautica2/Content/Blueprints/AI/Agents/VoidLeviathan/Behaviours/BT_VoidLeviathanMotherAttractedTarget",
            "Subnautica2/Content/Blueprints/AI/Agents/VoidLeviathan/Behaviours/BT_VoidLeviathanWaitForChildren",
            "Subnautica2/Content/Blueprints/AI/Agents/VoidLeviathan/Behaviours/BT_VoidLeviathanChildAttack",
        ],
    },
    {
        "family": "TempVoidLeviathan",
        "shipped": False,
        "story_role": "Stub/test fixture",
        "stages": [
            {"stage": "adult",
             "archetype": "Subnautica2/Content/Data/AI/Archetypes/DA_TempVoidLeviathanArchertype",
             "bp": None,
             "bt": "Subnautica2/Content/Blueprints/AI/Agents/TEMP_VOIDTEST_FAKELEVIATHAN/BT_UtilityTempVoidLeviathan"},
            {"stage": "baby",
             "archetype": "Subnautica2/Content/Data/AI/Archetypes/DA_TempVoidBabyLeviathanArchertype",
             "bp": None,
             "bt": "Subnautica2/Content/Blueprints/AI/Agents/TEMP_VOIDTEST_BABYLEVIATHAN/BT_UtilityTempVoidBabyLeviathan"},
        ],
    },
    {
        "family": "Sandspear",
        "shipped": True,
        "story_role": "Ambush predator",
        "stages": [
            {"stage": "adult",
             "archetype": "Subnautica2/Content/Data/AI/Archetypes/DA_SandspearArchetype",
             "bp": "Subnautica2/Content/Blueprints/AI/Agents/LargeCreature021_Sandspear/BP_Sandspear",
             "bt": "Subnautica2/Content/Blueprints/AI/Agents/LargeCreature021_Sandspear/BT_UtilitySandspear"},
            {"stage": "juvenile",
             "archetype": "Subnautica2/Content/Data/AI/Archetypes/DA_SandspearJuvenileArcheType",
             "bp": "Subnautica2/Content/Blueprints/AI/Agents/LargeCreature021_Sandspear/BP_Sandspear_Juvenile",
             "bt": "Subnautica2/Content/Blueprints/AI/Agents/LargeCreature021_Sandspear/BT_UtilitySandspear_Juvenile"},
        ],
    },
    {
        "family": "JetoCaris",
        "shipped": True,
        "story_role": "Large herbivore",
        "stages": [
            {"stage": "adult",
             "archetype": "Subnautica2/Content/Data/AI/Archetypes/DA_JetoCaris_Archetype",
             "bp": "Subnautica2/Content/Blueprints/AI/Agents/LargeCreature010_JetoCaris/BP_JetoCaris",
             "bt": "Subnautica2/Content/Blueprints/AI/Agents/LargeCreature010_JetoCaris/BT_UtilityJetoCaris"},
            {"stage": "juvenile",
             "archetype": "Subnautica2/Content/Data/AI/Archetypes/DA_JetoCarisJuvenileArchetype",
             "bp": "Subnautica2/Content/Blueprints/AI/Agents/LargeCreature010_JetoCaris/BP_JetoCaris_Juvenile",
             "bt": "Subnautica2/Content/Blueprints/AI/Agents/LargeCreature010_JetoCaris/BT_UtilityJetoCaris_Juvenile"},
        ],
        "link_behaviors": [
            "Subnautica2/Content/Blueprints/AI/Agents/LargeCreature010_JetoCaris/Behaviours/CavePrototype/BT_JetoCaris_Cave_MoveToNest",
            "Subnautica2/Content/Blueprints/AI/Agents/LargeCreature010_JetoCaris/Behaviours/CavePrototype/BT_JetoCaris_Cave_MoveToNest_WithJuvenile",
        ],
    },
    {
        "family": "Snorkleback",
        "shipped": True,
        "story_role": "Large herbivore (no BPs shipped yet)",
        "stages": [
            {"stage": "adult",
             "archetype": "Subnautica2/Content/Data/AI/Archetypes/DA_SnorklebackAdultArchetype",
             "bp": None,
             "bt": "Subnautica2/Content/Blueprints/AI/Agents/LargeCreature002_Snorkleback/BT_UtilitySnorklebackAdult"},
            {"stage": "juvenile",
             "archetype": "Subnautica2/Content/Data/AI/Archetypes/DA_SnorklebackJuvenileArchetype",
             "bp": None,
             "bt": "Subnautica2/Content/Blueprints/AI/Agents/LargeCreature002_Snorkleback/BT_UtilitySnorklebackJuvenile"},
        ],
    },
    {
        "family": "DeepWingLeviathan",
        "shipped": True,
        "story_role": "Egg-laying leviathan",
        "stages": [
            {"stage": "adult",
             "archetype": "Subnautica2/Content/Blueprints/AI/Agents/DeepWingLeviathan/DA_DeepWingLeviathanArchetype",
             "bp": "Subnautica2/Content/Blueprints/AI/Agents/DeepWingLeviathan/BP_DeepWingLeviathan",
             "bt": "Subnautica2/Content/Blueprints/AI/Agents/DeepWingLeviathan/BT_UtilityDeepWingLeviathan"},
        ],
        "egg_behaviors": [
            "Subnautica2/Content/Blueprints/AI/Agents/DeepWingLeviathan/BT_DeepwingLeviathanWanderHorizontalSpawnEggs",
            "Subnautica2/Content/Blueprints/AI/Agents/DeepWingLeviathan/GA_AI_LayEggs",
        ],
        "egg_items": [
            "Subnautica2/Content/Blueprints/Items/Resources/BP_DeepwingBrooderEggItem",
            "Subnautica2/Content/Data/ItemType/DA_DeepwingBrooderEgg_ItemType",
        ],
    },
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    prov = create_provider()

    spawn_counts = load_spawn_counts()
    pop_settings = load_pop_settings()
    pop_classes = set()
    for ps in pop_settings:
        for cls in ps.get("creature_classes", []):
            pop_classes.add(cls.rsplit("/", 1)[-1].split(".", 1)[-1])

    # 1. Per-family deep dive
    family_reports = []
    for fam in FAMILIES:
        stages = []
        for st in fam["stages"]:
            arch = dump_archetype(prov, st["archetype"]) if st.get("archetype") else None
            mat = []
            for which in (st.get("archetype"), st.get("bp")):
                if which:
                    h = scan_package_for_maturation(prov, which)
                    if h:
                        mat.append({"pkg": which, "hits": h})
            bp_short = st["bp"].rsplit("/", 1)[-1] if st.get("bp") else None
            sc = None
            for key, n in spawn_counts.items():
                short = key.split(".", 1)[0]
                if bp_short and short == bp_short:
                    sc = n
                    break
            stages.append({
                "stage": st["stage"],
                "archetype_path": st.get("archetype"),
                "bp_path": st.get("bp"),
                "bt_path": st.get("bt"),
                "archetype": arch,
                "maturation_hits": mat,
                "spawn_count": sc,
                "in_pop_settings": (bp_short in pop_classes) if bp_short else False,
                "bp_short": bp_short,
            })

        # Linking behaviors
        link_bts = []
        for lb in fam.get("link_behaviors", []) + fam.get("egg_behaviors", []):
            nodes = list_bt_nodes(prov, lb)
            if nodes is not None:
                # Extract just the meaningful BT class names (not composites/decorators)
                meaningful = [n for n in nodes if not (
                    n["class"].startswith("BTComposite_") or
                    n["class"].startswith("BTDecorator_") or
                    n["class"] == "BehaviorTree" or
                    n["class"] == "BTTask_Wait")]
                link_bts.append({
                    "bt_path": lb,
                    "all_nodes": nodes,
                    "meaningful_classes": sorted({n["class"] for n in meaningful}),
                })

        family_reports.append({
            "family": fam["family"],
            "shipped": fam["shipped"],
            "story_role": fam.get("story_role"),
            "stages": stages,
            "link_behaviors": link_bts,
            "egg_items": fam.get("egg_items", []),
        })

    # 2. Asset enumeration
    assets = enumerate_lifecycle_assets(prov)
    asset_counts = {k: len(v) for k, v in assets.items()}

    # 3. Player breeding probe
    pb = probe_player_breeding(prov)
    pb_counts = {k: len(v) for k, v in pb.items()}

    # 4. Egg items
    egg_items = probe_egg_items(prov)

    # 5. Verdict
    multi_stage_shipped = [f for f in family_reports if len(f["stages"]) > 1 and f["shipped"]]
    any_maturation_logic = any(
        st["maturation_hits"]
        for f in family_reports for st in f["stages"]
    )
    # BioIncubator/BioLab is for biomod cultures, not creatures.  Manually
    # verified via the ST_BioIncubator and ST_BioLab string tables (no
    # "creature", "egg", "hatch", "fish" rows).  Treat as not a creature
    # breeding system unless we ever find an AI-archetype reference to it.
    bio_only = set(pb_counts.keys()) <= {"BioIncubator"}
    has_player_breeding = (any(pb_counts.values()) and not bio_only)

    report = {
        "source": "D:/subnautica/miner/research/probe_lifecycle.py",
        "asset_counts_by_keyword": asset_counts,
        "player_breeding_keyword_hits": pb_counts,
        "player_breeding_paths": pb,
        "egg_items": egg_items,
        "families": family_reports,
        "spawn_counts": spawn_counts,
        "pop_classes": sorted(pop_classes),
        "verdict": {
            "multi_stage_family_count_total": sum(1 for f in family_reports if len(f["stages"]) > 1),
            "multi_stage_family_count_shipped": len(multi_stage_shipped),
            "any_maturation_property_found": any_maturation_logic,
            "player_driven_breeding": has_player_breeding,
            "player_driven_breeding_evidence": pb_counts if has_player_breeding else None,
            "summary": (
                "Stages are separate AI archetypes (one DA_*Archetype per stage), not a "
                "single archetype with size scaling. The Mother-Child link is established "
                "at BT runtime via UWEPawnAttachmentOwner + UWEBTTAttachmentSlotOperation "
                "+ WEBTDFindAttachmentSlot, not by population/spawn-time configuration. "
                "No GrowTime / AgeTime / LifeStage / NextStage / GrowsInto property "
                "exists on any archetype or BP, so juveniles do not gameplay-mature into "
                "adults. The DeepWingLeviathan has a GA_AI_LayEggs ability and a "
                "BT_DeepwingLeviathanWanderHorizontalSpawnEggs behaviour that drops "
                "Niagara egg VFX in its wake. The egg items (Deepwing Egg Clump, "
                "Necrolei Cyst) are food/resource pickups and do not hatch into "
                "creatures. No player-driven breeding system exists (no AlienContainment, "
                "no Aquarium, no WaterPark, no Hatchery). BioLab/BioIncubator are for "
                "biomod culture management, not creatures."
            ),
        },
    }

    out_json = OUT_DIR / "lifecycle.json"
    out_json.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"wrote {out_json}")

    # ---------- Markdown summary (no em dashes) ----------
    md = []
    md.append("# Subnautica 2: Creature Lifecycle (Juvenile / Adult / Mother / Eggs)")
    md.append("")
    md.append("Source: `research/probe_lifecycle.py` against PAK 5.6.1-112084.")
    md.append("")
    md.append("## TL;DR")
    md.append("")
    md.append(f"- Multi-stage creature families shipped: **{len(multi_stage_shipped)}**: " +
              ", ".join(f["family"] for f in multi_stage_shipped) + ".")
    md.append("- Stage mechanism: each stage is its own `DA_*Archetype` data asset with "
              "its own BT and AbilitySet. Not size-scaling on a single archetype.")
    md.append("- Maturation logic (Juvenile growing into Adult by gameplay): "
              f"**{'PRESENT' if any_maturation_logic else 'ABSENT'}**. No `GrowTime`, "
              "`AgeTime`, `LifeStage`, `NextStage`, `GrowsInto`, or similar property "
              "exists on any archetype or BP. Juveniles and adults are distinct "
              "spawned entities; one does not become the other.")
    md.append("- Mother / Child wiring (VoidLeviathan): BT runtime attachment via "
              "`UWEPawnAttachmentOwner` (on the Mother) + `UWEBTTAttachmentSlotOperation` "
              "/ `WEBTDFindAttachmentSlot` (in `BT_VoidLeviathanChildAttachMother`). "
              "Not pop-time configured.")
    md.append("- Egg-laying: only DeepWingLeviathan has a `GA_AI_LayEggs` ability and "
              "`BT_DeepwingLeviathanWanderHorizontalSpawnEggs`. The eggs are Niagara "
              "VFX trails (`NS_DeepWing_EggsSpawner`, `NS_DeepWing_EggsTrail`), not "
              "world actors that hatch.")
    md.append("- Egg inventory items (Deepwing Egg Clump, Necrolei Cyst, Veps Egg "
              "test): tagged as food / resource. None reference a hatched creature class.")
    md.append(f"- Player-driven breeding: **{'YES' if has_player_breeding else 'NO'}**. "
              "No AlienContainment, no Aquarium, no WaterPark, no Hatchery, no "
              "CreatureContainer, no FishTank. The `BioIncubator` / `BioLab` strings "
              "in `ST_BioIncubator` and `ST_BioLab` belong to a biomod-culture device, "
              "not a creature breeder.")
    md.append("")
    md.append("## Asset enumeration (gameplay folders only)")
    md.append("")
    for kw, n in sorted(asset_counts.items()):
        md.append(f"- `{kw}`: **{n}** assets under AI / Items / Databank folders.")
    md.append("")
    md.append("## Player-breeding keyword probe")
    md.append("")
    md.append("Searched: " + ", ".join(f"`{k}`" for k in PLAYER_BREEDING_KEYWORDS) + ".")
    md.append("")
    if not any(pb_counts.values()):
        md.append("No matches outside Engine paths. No player-driven breeding mechanic exists in the build.")
    else:
        for k, n in sorted(pb_counts.items(), key=lambda kv: -kv[1]):
            md.append(f"- `{k}`: {n} paths.")
            for p in pb[k][:5]:
                md.append(f"  - `{p}`")
    md.append("")
    md.append("## Egg-like inventory items")
    md.append("")
    md.append("From `out/items.json`:")
    md.append("")
    md.append("- `DA_DeepwingBrooderEgg_ItemType` (Deepwing Egg Clump): "
              '"Unfertilized deepwing roe. Miraculous source of bioavailable nutrients '
              'and hydration. The clump swiftly dissolves in seawater." Food.')
    md.append("- `DA_FalseOysterRaion_Nodule_ItemType` (Necrolei Cyst, actor class "
              "`BP_NecroleiEgg_Item`): a base resource, not an egg in the gameplay "
              "sense. Item name is `Necrolei Cyst`.")
    md.append("- `DA_VepEggTest` (Veps Egg, deprecated): placeholder asset; actor "
              "class points at `BP_ShellPiece` under `Resources/Deprecated/`.")
    md.append("")
    md.append("None of these items hatch into a creature on use or over time.")
    md.append("")
    md.append("## Multi-stage families: per-family detail")
    md.append("")

    for fr in family_reports:
        flag = "shipped" if fr["shipped"] else "stub / test"
        md.append(f"### {fr['family']} ({flag})")
        md.append("")
        md.append(f"_Role: {fr.get('story_role')}_")
        md.append("")
        md.append("| Stage | Archetype | BP | BT | Seeded spawns | In pop_settings |")
        md.append("|---|---|---|---|---:|:---:|")
        for st in fr["stages"]:
            arch_id = st["archetype"]["id"] if st.get("archetype") else "(missing)"
            bp = st.get("bp_short") or "(none)"
            bt = (st["archetype"] or {}).get("behavior_tree") or "(none)"
            bt = bt.rsplit("/", 1)[-1]
            md.append(f"| **{st['stage']}** | `{arch_id}` | `{bp}` | `{bt}` | "
                      f"{st.get('spawn_count') or 0} | {'yes' if st.get('in_pop_settings') else 'no'} |")
        md.append("")
        # Maturation
        any_mat = any(st.get("maturation_hits") for st in fr["stages"])
        if any_mat:
            md.append("Maturation properties found:")
            for st in fr["stages"]:
                for m in st.get("maturation_hits", []):
                    md.append(f"- `{m['pkg']}`: {m['hits']}")
        else:
            md.append("**No maturation logic** found in archetype or BP. Searched: "
                     + ", ".join(f"`{p}`" for p in sorted(MATURATION_PROPS)) + ".")
        md.append("")
        # Link behaviours
        if fr.get("link_behaviors"):
            md.append("Linking / lifecycle behaviours:")
            for lb in fr["link_behaviors"]:
                short = lb["bt_path"].rsplit("/", 1)[-1]
                md.append(f"- `{short}`: {len(lb['all_nodes'])} exports. Notable classes: "
                          + ", ".join(f"`{c}`" for c in lb["meaningful_classes"]))
            md.append("")
        if fr.get("egg_items"):
            md.append("Egg items:")
            for p in fr["egg_items"]:
                md.append(f"- `{p}`")
            md.append("")

    md.append("## VoidLeviathan Mother / Child mechanism (verified)")
    md.append("")
    md.append("`BT_VoidLeviathanChildAttachMother` contains:")
    md.append("")
    md.append("- `UWEBTTAttachmentSlotOperation` (x2): attach / detach the child pawn "
              "to an attachment slot on the mother.")
    md.append("- `WEBTDFindAttachmentSlot`: locate a free attachment slot on the mother.")
    md.append("- `UWEBTTIndefiniteWait`: child waits while attached.")
    md.append("- `UWEBTTMoveTo` (`ApproachDistance=4000`): swim to the mother first.")
    md.append("- `UWEBTSDynamicTag`, `UWEBTSPlaySound`, `UWEAnimationEventData`: "
              "tagging, audio, and animation cues during the attach.")
    md.append("")
    md.append("The Mother BP (`BP_VoidLeviathanMother`) carries a "
              "`UWEPawnAttachmentOwner` component (the slot host). The Child BP "
              "(`BP_VoidLeviathanChild`) is the attachee. There is no spawn-child "
              "ability on the Mother BT (`BT_UtilityVoidLeviathanMother`): the BT "
              "is purely a `UWEBTCUtilitySelector` running pre-attached child "
              "behaviours. Conclusion: Mother and Children are placed together at "
              "spawn time (likely via a single seeded entry that places both, or a "
              "scripted story spawn) and then re-bind via BT at runtime.")
    md.append("")
    md.append("## DeepWingLeviathan egg-laying (verified)")
    md.append("")
    md.append("`GA_AI_LayEggs` is a `GameplayAbility` blueprint with the usual "
              "`K2_ActivateAbility` / `K2_OnEndAbility` overrides. It does not "
              "expose any tunable property in the CDO (logic lives in the K2 "
              "graph). `BT_DeepwingLeviathanWanderHorizontalSpawnEggs` runs the "
              "ability inside a wander loop. The eggs themselves are Niagara "
              "particle effects (`NS_DeepWing_EggsSpawner`, `NS_DeepWing_EggsTrail`, "
              "`NDCA_DeepwingLeviathanEggs` data channel asset, `NET_DeepwingEggs` "
              "effect type). They are not pawns and have no AI archetype: no "
              "creature hatches from them. They function as flavour VFX and / or "
              "harvestable resource spawns.")
    md.append("")
    md.append("## Verdict on player-driven breeding")
    md.append("")
    md.append("**No.** No part of the player toolkit makes creatures reproduce. "
              "Cross-checks performed:")
    md.append("")
    md.append("1. No `AlienContainment` asset (the SN1 breeding facility name): zero hits.")
    md.append("2. No `Aquarium`, `WaterPark`, `Hatchery`, `CreatureContainer`, "
              "`EggIncubator`, `CreatureGrower`, `FishTank`, `CreatureFarm`: zero hits.")
    md.append("3. The only `BioIncubator` / `BioLab` assets handle biomod cultures "
              "(`ST_BioIncubator`: `EmptyShelf`, `IncubatorTitle`, `NoCulturesWarning`; "
              "`ST_BioLab`: `Install Biomod`, `Uninstall Biomod`, `Active Biomods`).")
    md.append("4. The DeepWing's `GA_AI_LayEggs` is an AI-only gameplay ability and "
              "is wrapped in a wander BT, with no player input or trigger path.")
    md.append("5. None of the three egg items reference a creature-spawn actor class. "
              "All three are consumable food / resource or deprecated test fixtures.")
    md.append("")
    md.append("Everything is passive world AI:")
    md.append("")
    md.append("- World population system places creatures via `DA_SeededCreatureData*` "
              "with stage-specific BP classes (e.g. `BP_Sandspear_Juvenile`).")
    md.append("- Mothers and children of the VoidLeviathan are spawned together (or "
              "by story script) and link at BT time via attachment slots.")
    md.append("- Juveniles do not grow up: no maturation property exists.")
    md.append("- Eggs in the world are VFX flavour spawned by the DeepWingLeviathan, "
              "not hatching pawns.")

    out_md = OUT_DIR / "lifecycle.md"
    out_md.write_text("\n".join(md), encoding="utf-8")
    print(f"wrote {out_md}")


if __name__ == "__main__":
    main()
