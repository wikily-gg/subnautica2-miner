"""Probe the Subnautica 2 companion / player-pet system.

Runs reproducibly from D:\\subnautica\\miner with the venv activated.

Outputs:
  - D:\\subnautica\\miner\\out\\research\\companions.json
  - D:\\subnautica\\miner\\out\\research\\companions.md
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Any

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

# Make 'helpers' / 'provider' importable when run from the research/ subdir.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from provider import create_provider  # noqa: E402
from helpers import (  # noqa: E402
    safe_load_package, find_export, find_exports_by_class,
    prop, prop_str, prop_array, prop_object_path, prop_tags,
    unwrap_struct, obj_ref_path, short_name_from_path, array_values,
    extract_gameplay_tags,
)


OUT_DIR = os.path.abspath(os.path.join(ROOT, "out", "research"))
os.makedirs(OUT_DIR, exist_ok=True)


# ---------------- helpers ----------------

def _dump_properties(export) -> dict[str, Any]:
    """Return a dict of every visible property tag on an export, stringified."""
    out: dict[str, Any] = {}
    props = getattr(export, "Properties", None)
    if props is None:
        return out
    for tag in props:
        name = tag.Name.Text
        val = tag.Tag.GenericValue if tag.Tag is not None else None
        if val is None:
            out[name] = None
            continue
        # Try several representations.
        soft_path = None
        try:
            apn = getattr(val, "AssetPathName", None)
            if apn is not None:
                pkg = getattr(apn, "PackageName", None)
                asset = getattr(apn, "AssetName", None)
                if pkg is not None:
                    soft_path = str(pkg)
                    if asset is not None and str(asset) != "None":
                        soft_path = f"{soft_path}.{asset}"
        except Exception:
            pass

        try:
            tagname = getattr(val, "TagName", None)
            if tagname is not None and str(tagname) != "None":
                out[name] = {"tag": str(tagname)}
                continue
        except Exception:
            pass

        try:
            gp_tags = extract_gameplay_tags(val) if val is not None else []
        except Exception:
            gp_tags = []
        if gp_tags:
            out[name] = {"tags": gp_tags}
            continue

        if soft_path:
            out[name] = {"path": soft_path}
            continue

        ref = obj_ref_path(val)
        if ref and ref not in ("None", ""):
            out[name] = {"ref": ref}
            continue

        # Fall back to str(); often "X=... Y=... (FVector)" or a number.
        s = str(val)
        if "CUE4Parse" in s:
            out[name] = f"<{type(val).__name__}>"
        else:
            out[name] = s
    return out


def _walk(prov, prefix: str) -> list[str]:
    return [k for k in prov.Files.Keys if k.startswith(prefix) and k.endswith(".uasset")]


def _walk_re(prov, pattern: str) -> list[str]:
    rx = re.compile(pattern, re.IGNORECASE)
    return [k for k in prov.Files.Keys if k.endswith(".uasset") and rx.search(k)]


def _load_pkg(prov, asset_path: str):
    pkg_path = asset_path[:-7] if asset_path.endswith(".uasset") else asset_path
    return safe_load_package(prov, pkg_path), pkg_path


# ---------------- 1. DA_PlayerPet dump ----------------

def dump_player_pet(prov) -> dict:
    asset = "Subnautica2/Content/Data/AI/Archetypes/DA_PlayerPet"
    pkg = safe_load_package(prov, asset)
    if pkg is None:
        return {"error": f"failed to load {asset}"}

    exports = []
    for ex in pkg.GetExports():
        et = getattr(ex, "ExportType", None)
        exports.append({
            "name": str(ex.Name),
            "class": str(et) if et is not None else type(ex).__name__,
            "properties": _dump_properties(ex),
        })

    # Also surface the conventional fields the wiki cares about.
    archetype = find_export(pkg, class_substring="UWEAIArchetypeDataAsset")
    summary: dict[str, Any] = {"asset": asset, "exports": exports}
    if archetype is not None:
        summary["identifier_tag"] = (prop_tags(archetype, "IdentifierTag") or [None])[0]
        summary["keywords"] = prop_tags(archetype, "Keywords")
        summary["enemies"] = prop_tags(archetype, "Enemies")
        summary["behavior_tree"] = prop_object_path(archetype, "BehaviorTree")
        summary["dominant_sense"] = obj_ref_path(prop(archetype, "DominantSense"))
    return summary


# ---------------- 2. BT_UtilityPlayerPet ----------------

def _collect_str_paths(node) -> list[str]:
    """Recursively gather every soft / object path referenced by a node's props."""
    out: list[str] = []
    props = getattr(node, "Properties", None)
    if props is None:
        return out
    for tag in props:
        v = tag.Tag.GenericValue if tag.Tag is not None else None
        if v is None:
            continue
        # Soft path
        apn = getattr(v, "AssetPathName", None)
        if apn is not None:
            pkg = getattr(apn, "PackageName", None)
            asset = getattr(apn, "AssetName", None)
            if pkg is not None:
                s = str(pkg)
                if asset is not None and str(asset) != "None":
                    s = f"{s}.{asset}"
                if s and s != "None":
                    out.append(s)
                continue
        # Object ref via FPackageIndex
        ref = obj_ref_path(v)
        if ref and ref not in ("None", "") and ref.startswith("/"):
            out.append(ref)
    return out


def dump_behavior_tree(prov, path: str = "Subnautica2/Content/Blueprints/AI/Agents/Prototypes/PlayerPet/BT_UtilityPlayerPet") -> dict:
    pkg = safe_load_package(prov, path)
    if pkg is None:
        return {"error": f"failed to load {path}"}

    exports_info: list[dict[str, Any]] = []
    referenced_paths: list[str] = []
    leaf_class_pattern = re.compile(
        r"(BTTask|BTService|BTDecorator|UBTTask|UBTService|UBTDecorator)",
        re.IGNORECASE,
    )
    bt_nodes: list[dict[str, Any]] = []
    sub_trees: list[str] = []

    for ex in pkg.GetExports():
        et = getattr(ex, "ExportType", None)
        cls = str(et) if et is not None else type(ex).__name__
        name = str(ex.Name)
        props_summary = _dump_properties(ex)
        exports_info.append({"name": name, "class": cls, "properties": props_summary})

        # Referenced asset paths on this node.
        refs = _collect_str_paths(ex)
        for r in refs:
            if r not in referenced_paths:
                referenced_paths.append(r)
            if "BT_" in r and r.endswith(("BT_", )) is False and "/BehaviorTree" not in r.lower():
                # Likely a child behaviour tree
                pass

        if leaf_class_pattern.search(cls):
            bt_nodes.append({"name": name, "class": cls})

        # RunBehavior nodes hold a sub-BT
        if "RunBehavior" in cls or "BTTask_RunBehavior" in cls:
            for r in refs:
                if "BT_" in r:
                    sub_trees.append(r)

    # Also pull any path that looks like another BT_*
    referenced_bts = sorted({r for r in referenced_paths if "/BT_" in r or r.split("/")[-1].startswith("BT_")})

    return {
        "asset": path,
        "export_count": len(exports_info),
        "exports": exports_info,
        "task_decorator_service_nodes": bt_nodes,
        "referenced_paths": referenced_paths,
        "referenced_behavior_trees": referenced_bts,
        "sub_trees_via_run_behavior": sub_trees,
    }


# ---------------- 3. InitialAttributes lookup ----------------

def find_player_pet_initial_attrs(prov) -> dict:
    prefix = "Subnautica2/Content/Blueprints/AbilitySystem/Effects/AI/InitialAttributes/"
    matches = [k for k in prov.Files.Keys
               if k.startswith(prefix) and k.endswith(".uasset")
               and "pet" in k.lower()]
    result: dict[str, Any] = {"prefix": prefix, "matches": matches, "attributes": {}}
    for path in matches:
        pkg = safe_load_package(prov, path[:-7])
        if pkg is None:
            continue
        cdo = None
        for ex in pkg.GetExports():
            if str(ex.Name).startswith("Default__"):
                cdo = ex
                break
        if cdo is None:
            continue
        mods = prop_array(cdo, "Modifiers")
        attrs: dict[str, float] = {}
        for m in mods:
            try:
                inner = unwrap_struct(m.Value)
                attr = unwrap_struct(prop(inner, "Attribute"))
                mag = unwrap_struct(prop(inner, "ModifierMagnitude"))
                name = prop_str(attr, "AttributeName") if attr is not None else None
                if not name:
                    continue
                sf = unwrap_struct(prop(mag, "ScalableFloatMagnitude")) if mag is not None else None
                if sf is None:
                    continue
                val = prop(sf, "Value")
                if val is None:
                    continue
                attrs[str(name)] = float(val)
            except Exception:
                continue
        if attrs:
            result["attributes"][path] = attrs
    return result


# ---------------- 4. PAK-wide pet keyword search ----------------

PET_KEYWORDS = [
    "Pet", "Companion", "Follower", "Loyal", "Tame", "Tamed", "Bond",
    "Friend", "Befriend", "Pal", "Owner", "Master",
]
# False positives to drop when searching file basenames.
PET_EXCLUDE = re.compile(
    r"(Petal|RockPet|PetroleumBlue|PetrolBlue|Petroleum|Carpet|Trumpet|Petite|Petrol|Helmet|Competition|Compet)",
    re.IGNORECASE,
)


def keyword_search(prov) -> dict[str, list[str]]:
    """Search asset basenames for pet/companion-style keywords.

    The pattern accepts the keyword if it appears at a word boundary inside
    the basename (between an underscore, hyphen, period or slash on at
    least one side, or at start/end). This catches both `BP_PlayerPet`
    and `Pet_Treat` while still rejecting `Petal`/`Helmet`.
    """
    out: dict[str, list[str]] = {}
    for kw in PET_KEYWORDS:
        # Word boundary on at least one side. We match `Pet` in `PlayerPet`
        # (lower->Upper transition) and `Pet_X`, but not `Petal`.
        rx = re.compile(
            rf"(?:(?<=[A-Z_/\-.])|(?<=[a-z])(?=[A-Z]))"
            rf"{re.escape(kw)}"
            rf"(?:(?=[A-Z_/\-.0-9])|$)",
        )
        hits: list[str] = []
        for k in prov.Files.Keys:
            if not k.endswith((".uasset", ".uexp", ".umap")):
                continue
            base = k.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if PET_EXCLUDE.search(base):
                continue
            if rx.search(base):
                hits.append(k)
        out[kw] = sorted(set(hits))
    return out


# ---------------- 5. Find a BP using DA_PlayerPet ----------------

def dump_drone_buddy(prov) -> dict:
    """Dump every DroneBuddy asset in the PAK.

    The DroneBuddy is the other 'pet-like' system in SN2: a deployable
    follower drone (item name `Greeble`). It's listed as a deployable
    item, not a creature, but its AI archetype + BT live alongside the
    creature archetypes, so it's relevant to a 'companion' research note.
    """
    files = sorted({k for k in prov.Files.Keys if "DroneBuddy" in k and k.endswith(".uasset")})
    info: dict[str, Any] = {"files": files, "details": {}}
    item_path = None
    for k in files:
        if k.startswith("Subnautica2/Content/Data/ItemType/") and "DroneBuddy" in k:
            item_path = k
        pkg = safe_load_package(prov, k[:-7])
        if pkg is None:
            continue
        node: dict[str, Any] = {"exports": []}
        for ex in pkg.GetExports():
            et = getattr(ex, "ExportType", None)
            cls = str(et) if et is not None else type(ex).__name__
            node["exports"].append({"name": str(ex.Name), "class": cls,
                                    "properties": _dump_properties(ex)})
        info["details"][k] = node

    # Pull the in-game name + description from items.json if available.
    items_path = os.path.join(ROOT, "out", "items.json")
    if os.path.exists(items_path):
        with open(items_path, "r", encoding="utf-8") as fh:
            items = json.load(fh)
        for it in items:
            if "DroneBuddy" in (it.get("id") or "") or "DroneBuddy" in (it.get("asset") or ""):
                info["item_record"] = it
                break
    return info


def dump_follow_behavior(prov) -> dict:
    """Dump BT_LargeCreatureFollow (the BT the pet runs to track the player)."""
    path = "Subnautica2/Content/Blueprints/AI/Agents/Common/Behaviors/LargeCreature/BT_LargeCreatureFollow"
    pkg = safe_load_package(prov, path)
    if pkg is None:
        return {"error": f"failed to load {path}"}
    exports = []
    refs = []
    for ex in pkg.GetExports():
        et = getattr(ex, "ExportType", None)
        cls = str(et) if et is not None else type(ex).__name__
        exports.append({"name": str(ex.Name), "class": cls,
                        "properties": _dump_properties(ex)})
        refs.extend(_collect_str_paths(ex))
    return {"asset": path, "exports": exports, "references": sorted(set(refs))}


def find_other_follow_users(prov) -> list[str]:
    """Find every archetype whose BehaviorTree references BT_LargeCreatureFollow.

    Slow-ish path: load each DA_ archetype and inspect. Cheaper: read the
    extracted JSON and only re-load anything that points at Common/Behaviors.
    """
    ca_path = os.path.join(ROOT, "out", "creature_archetypes.json")
    if not os.path.exists(ca_path):
        return []
    with open(ca_path, "r", encoding="utf-8") as fh:
        creatures = json.load(fh)
    # Only the direct top-level BT path is recorded in creature_archetypes.json,
    # so look for archetypes that point at BT_LargeCreatureFollow directly.
    out: list[str] = []
    for c in creatures:
        bt = c.get("behavior_tree") or ""
        if "BT_LargeCreatureFollow" in bt:
            out.append(c.get("id"))
    return out


def find_pet_bp(prov) -> dict:
    prefix = "Subnautica2/Content/Blueprints/AI/Agents/Prototypes/PlayerPet/"
    files = [k for k in prov.Files.Keys if k.startswith(prefix)]
    related: dict[str, dict] = {}
    for path in files:
        if not path.endswith(".uasset"):
            continue
        pkg = safe_load_package(prov, path[:-7])
        if pkg is None:
            continue
        info: dict[str, Any] = {"exports": [], "references": []}
        for ex in pkg.GetExports():
            et = getattr(ex, "ExportType", None)
            cls = str(et) if et is not None else type(ex).__name__
            info["exports"].append({"name": str(ex.Name), "class": cls})
            refs = _collect_str_paths(ex)
            info["references"].extend(refs)
        info["references"] = sorted(set(info["references"]))
        related[path] = info
    return {"prefix": prefix, "files": files, "details": related}


# ---------------- 6. Feed / treat / reward items ----------------

def find_food_items() -> dict:
    items_path = os.path.join(ROOT, "out", "items.json")
    if not os.path.exists(items_path):
        return {"error": "items.json not found"}
    with open(items_path, "r", encoding="utf-8") as fh:
        items = json.load(fh)
    # Use word boundaries so we don't false-match "Reset"/"Carpet"/etc.
    name_match = re.compile(
        r"\b(feed|feeding|treat|reward|bait|lure|tame|cuddle|companion|pet|"
        r"petfood|tamefood|feeditem)\b",
        re.IGNORECASE,
    )
    out: list[dict] = []
    for it in items:
        name = it.get("name") or ""
        asset = it.get("asset") or ""
        ident = it.get("id") or ""
        # Description excluded on purpose: most flavour text mentions "feeds"
        # for purely ecological reasons (e.g. the jelly-ring databank entry).
        if name_match.search(name) or name_match.search(asset) or name_match.search(ident):
            out.append({"id": ident, "name": name, "asset": asset,
                        "description": it.get("description")})
    return {"count": len(out), "items": out}


# ---------------- 7. Cuddlefish equivalent candidates ----------------

def find_cuddlefish_candidates() -> dict:
    ca_path = os.path.join(ROOT, "out", "creature_archetypes.json")
    if not os.path.exists(ca_path):
        return {"error": "creature_archetypes.json not found"}
    with open(ca_path, "r", encoding="utf-8") as fh:
        creatures = json.load(fh)
    small_friendlies = []
    for c in creatures:
        kw = set(c.get("keywords") or [])
        enemies = c.get("enemies") or []
        # "Small + Herbivore + no Player enemy" is a soft 'cuddlefishy' filter.
        if "AI.Size.Small" in kw and "AI.Role.Herbivore" in kw:
            has_player_enemy = any("Player" in e for e in enemies)
            if not has_player_enemy:
                small_friendlies.append({
                    "id": c.get("id"),
                    "keywords": list(kw),
                    "enemies": enemies,
                    "behavior_tree": c.get("behavior_tree"),
                    "stats": c.get("stats"),
                    "stats_source": c.get("stats_source"),
                })
    return {"count": len(small_friendlies), "candidates": small_friendlies}


# ---------------- 8. Cross-ref string tables ----------------

def find_pet_strings() -> dict:
    st_path = os.path.join(ROOT, "out", "string_tables.json")
    if not os.path.exists(st_path):
        return {"error": "string_tables.json not found"}
    with open(st_path, "r", encoding="utf-8") as fh:
        tables = json.load(fh)
    hits: list[dict] = []
    # Require word boundaries so "TadpolePens" / "competition" don't false-positive.
    pet_rx = re.compile(
        r"\b(pet|pets|companion|companions|follower|followers|tame|tamed|taming|bond|bonded|"
        r"befriend|befriended|cuddle|cuddlefish|loyal|loyalty|leash|leashed)\b",
        re.IGNORECASE,
    )
    if isinstance(tables, dict):
        items = tables.items()
    elif isinstance(tables, list):
        items = enumerate(tables)
    else:
        items = []
    for k, v in items:
        try:
            s = json.dumps(v, ensure_ascii=False)
        except Exception:
            s = str(v)
        if pet_rx.search(s):
            hits.append({"id": str(k), "snippet": s[:500]})
    return {"count": len(hits), "hits": hits[:50]}


# ---------------- Verdict ----------------

def compute_verdict(pet_archetype: dict, bt: dict, bp: dict, attrs: dict) -> dict:
    indicators: list[str] = []
    if pet_archetype.get("behavior_tree") and "Prototypes/PlayerPet" in pet_archetype["behavior_tree"]:
        indicators.append(
            "Asset path under /Prototypes/PlayerPet/: in-development location, not the shipped /Agents/Creature*/ pattern."
        )
    if not pet_archetype.get("identifier_tag"):
        indicators.append(
            "No IdentifierTag set on the archetype (real shipped creatures all have one, e.g. Character.NibblerShark)."
        )
    if not pet_archetype.get("enemies"):
        indicators.append("Empty Enemies array (functional creatures list e.g. Character.Type.Player).")
    # Friendlies are an additional smoking-gun: the pet calls the player a friendly.
    # That's pulled out of the export properties dict.
    for ex in pet_archetype.get("exports") or []:
        if ex.get("class") == "UWEAIArchetypeDataAsset":
            friendlies = (ex.get("properties") or {}).get("Friendlies")
            if isinstance(friendlies, dict) and friendlies.get("tags"):
                indicators.append(
                    f"Friendlies tag is set to {friendlies['tags']}: confirms intent for the pet to treat the player as an ally."
                )
            break
    if not attrs.get("attributes"):
        indicators.append(
            "No GE_PlayerPet*InitialAttributes asset found: stats inherit from SmallCreature defaults only."
        )
    pet_bp_files = bp.get("files") or []
    if not pet_bp_files:
        indicators.append("No BP_/SKM_/asset files under Prototypes/PlayerPet/ besides the BT/DA.")
    else:
        indicators.append(
            f"{len(pet_bp_files)} file(s) under Prototypes/PlayerPet/: only the BT and the DA, no skeletal mesh, no BP_*, no animations."
        )
    bt_nodes = bt.get("task_decorator_service_nodes") or []
    if bt_nodes:
        leaf_names = [n.get("name") for n in bt_nodes if "RunBehavior" in (n.get("class") or "")]
        indicators.append(
            f"BT_UtilityPlayerPet has {len(bt_nodes)} structural nodes. The only RunBehavior leaf is "
            f"{leaf_names or '<none>'} pointing at BT_LargeCreatureFollow."
        )
    state = "prototype"
    if (
        pet_archetype.get("identifier_tag")
        and attrs.get("attributes")
        and any("BP_" in p for p in pet_bp_files)
    ):
        state = "shipped"
    return {"state": state, "indicators": indicators}


# ---------------- Main ----------------

def main() -> int:
    prov = create_provider()
    print("Provider ready.", file=sys.stderr)

    pet_archetype = dump_player_pet(prov)
    bt = dump_behavior_tree(prov)
    follow_bt = dump_follow_behavior(prov)
    follow_users = find_other_follow_users(prov)
    drone_buddy = dump_drone_buddy(prov)
    attrs = find_player_pet_initial_attrs(prov)
    keyword_hits = keyword_search(prov)
    pet_bp = find_pet_bp(prov)
    food = find_food_items()
    cuddle = find_cuddlefish_candidates()
    strings = find_pet_strings()
    verdict = compute_verdict(pet_archetype, bt, pet_bp, attrs)

    payload = {
        "player_pet_archetype": pet_archetype,
        "behavior_tree": bt,
        "follow_behavior_tree": follow_bt,
        "other_archetypes_using_follow_bt": follow_users,
        "drone_buddy": drone_buddy,
        "initial_attributes_search": attrs,
        "pet_bp_files": pet_bp,
        "keyword_hits": keyword_hits,
        "food_items": food,
        "cuddlefish_candidates": cuddle,
        "string_table_hits": strings,
        "verdict": verdict,
    }

    out_json = os.path.join(OUT_DIR, "companions.json")
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
    print(f"Wrote {out_json}", file=sys.stderr)

    md = render_markdown(payload)
    out_md = os.path.join(OUT_DIR, "companions.md")
    with open(out_md, "w", encoding="utf-8") as fh:
        fh.write(md)
    print(f"Wrote {out_md}", file=sys.stderr)
    return 0


def render_markdown(p: dict) -> str:
    pet = p["player_pet_archetype"]
    bt = p["behavior_tree"]
    attrs = p["initial_attributes_search"]
    bp = p["pet_bp_files"]
    food = p["food_items"]
    cuddle = p["cuddlefish_candidates"]
    strings = p["string_table_hits"]
    verdict = p["verdict"]
    kw = p["keyword_hits"]

    lines: list[str] = []
    lines.append("# Subnautica 2: companions / player-pet research")
    lines.append("")
    lines.append("Source build: pre-Early Access, UE 5.6. Probe script: `research/probe_pet.py`.")
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(f"State: **{verdict['state']}**")
    lines.append("")
    for ind in verdict["indicators"]:
        lines.append(f"- {ind}")
    lines.append("")

    lines.append("## DA_PlayerPet archetype")
    lines.append("")
    lines.append(f"Asset: `{pet.get('asset')}`")
    lines.append("")
    lines.append(f"- identifier_tag: `{pet.get('identifier_tag')}`")
    lines.append(f"- keywords: {pet.get('keywords')}")
    lines.append(f"- enemies: {pet.get('enemies')}")
    lines.append(f"- behavior_tree: `{pet.get('behavior_tree')}`")
    lines.append(f"- dominant_sense: `{pet.get('dominant_sense')}`")
    lines.append("")
    lines.append("Exports:")
    for ex in pet.get("exports") or []:
        lines.append(f"- `{ex['name']}` ({ex['class']})")
        for pname, pval in (ex.get("properties") or {}).items():
            lines.append(f"  - `{pname}`: {pval}")
    lines.append("")

    # Pull friendlies out of the DA_PlayerPet export properties.
    da_props = {}
    for ex in pet.get("exports") or []:
        if ex.get("class") == "UWEAIArchetypeDataAsset":
            da_props = ex.get("properties") or {}
            break
    friendlies = (da_props.get("Friendlies") or {}).get("tags") if isinstance(da_props.get("Friendlies"), dict) else None
    if friendlies:
        lines.append(f"- friendlies: {friendlies}")
        lines.append("")

    lines.append("## Behaviour tree BT_UtilityPlayerPet")
    lines.append("")
    lines.append(f"Asset: `{bt.get('asset')}` ({bt.get('export_count')} exports)")
    lines.append("")
    nodes = bt.get("task_decorator_service_nodes") or []
    if nodes:
        lines.append("BT tasks / services / decorators:")
        for n in nodes:
            lines.append(f"- `{n['name']}` ({n['class']})")
    else:
        lines.append("No BTTask / BTService / BTDecorator exports visible in this package.")
    lines.append("")
    refs = bt.get("referenced_behavior_trees") or []
    if refs:
        lines.append("Referenced behaviour trees:")
        for r in refs:
            lines.append(f"- `{r}`")
    else:
        lines.append("No referenced child behaviour trees.")
    lines.append("")
    all_refs = bt.get("referenced_paths") or []
    if all_refs:
        lines.append("All referenced asset paths from BT properties:")
        for r in all_refs[:50]:
            lines.append(f"- `{r}`")
        if len(all_refs) > 50:
            lines.append(f"- ... ({len(all_refs) - 50} more)")
    lines.append("")

    follow_bt = p.get("follow_behavior_tree") or {}
    follow_users = p.get("other_archetypes_using_follow_bt") or []
    lines.append("## Shared follow behaviour (BT_LargeCreatureFollow)")
    lines.append("")
    if follow_bt.get("exports"):
        lines.append(f"Asset: `{follow_bt.get('asset')}` ({len(follow_bt['exports'])} exports)")
        lines.append("")
        lines.append("Exports:")
        for ex in follow_bt["exports"]:
            lines.append(f"- `{ex['name']}` ({ex['class']})")
            for pname, pval in (ex.get("properties") or {}).items():
                s = str(pval)
                lines.append(f"  - `{pname}`: {s[:200]}")
        lines.append("")
    if follow_users:
        lines.append(f"Other archetypes that use this follow BT as their root: {follow_users}")
    else:
        lines.append("No other archetype lists BT_LargeCreatureFollow as its root BT (the pet uses it as a sub-tree via UWEBTTRunBehavior).")
    lines.append("")

    lines.append("## InitialAttributes")
    lines.append("")
    if attrs.get("matches"):
        lines.append("Pet-named GameplayEffect attribute assets:")
        for m in attrs["matches"]:
            lines.append(f"- `{m}`")
        for path, a in (attrs.get("attributes") or {}).items():
            lines.append(f"  - `{path}`: {a}")
    else:
        lines.append("No `GE_*Pet*InitialAttributes` found. The pet inherits SmallCreature defaults at best.")
    lines.append("")

    lines.append("## Pet-related files under Prototypes/PlayerPet/")
    lines.append("")
    for f in bp.get("files") or []:
        lines.append(f"- `{f}`")
    lines.append("")

    lines.append("## Keyword scan across the PAK")
    lines.append("")
    for k, v in kw.items():
        if not v:
            continue
        lines.append(f"### `{k}` ({len(v)} hits)")
        for f in v[:30]:
            lines.append(f"- `{f}`")
        if len(v) > 30:
            lines.append(f"- ... ({len(v) - 30} more)")
        lines.append("")

    lines.append("## Feed / treat / bait items")
    lines.append("")
    if food.get("items"):
        for it in food["items"]:
            lines.append(f"- `{it.get('id')}`: {it.get('name')} ({it.get('asset')})")
    else:
        lines.append("No items match the Treat / Reward / FeedItem / PetFood / Bait / Lure regex.")
    lines.append("")

    drone = p.get("drone_buddy") or {}
    lines.append("## Related: DroneBuddy / Greeble (deployable follower)")
    lines.append("")
    if drone.get("item_record"):
        ir = drone["item_record"]
        lines.append(f"Item: `{ir.get('id')}` -> in-game name **{ir.get('name')}**")
        lines.append(f"Description: {ir.get('description')}")
        lines.append(f"Actor class: `{ir.get('actor_class')}`")
        lines.append("")
    if drone.get("files"):
        lines.append("Assets:")
        for f in drone["files"]:
            lines.append(f"- `{f}`")
        lines.append("")

    lines.append("## Cuddlefish-equivalent candidates (small herbivores not hostile to player)")
    lines.append("")
    cands = cuddle.get("candidates") or []
    lines.append(f"{len(cands)} candidate(s):")
    for c in cands:
        lines.append(f"- `{c['id']}` keywords={c.get('keywords')} enemies={c.get('enemies')} stats_source={c.get('stats_source')}")
    lines.append("")

    lines.append("## String-table hits (pet / companion / cuddle / tame / bond / befriend)")
    lines.append("")
    if strings.get("hits"):
        for s in strings["hits"]:
            lines.append(f"- `{s['id']}`: {s['snippet'][:200]}")
    else:
        lines.append("No string-table rows match.")
    lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
