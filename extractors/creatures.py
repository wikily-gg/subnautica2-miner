"""Extract UWEAIArchetypeDataAsset (creatures), GameplayTags, AbilitySets.

Creature stats (HP, swim speed, food, stamina, etc.) live on separate
`GE_<CreatureName>InitialAttributes` GameplayEffect assets. Each of those
holds a `Modifiers` array of GameplayModifierInfo structs:

    Modifiers[i].Attribute.AttributeName  → "MaxHealth" / "Health" / ...
    Modifiers[i].ModifierMagnitude.ScalableFloatMagnitude.Value → 1000.0

We scan that folder once, build a `slug → {attribute: value}` map, then
merge it into the creature archetype record. The slug match is fuzzy
(case-insensitive substring) because Unknown Worlds isn't consistent about
the BP vs DA capitalisation (`Marrowbreach` vs `MarrowBreach`).
"""
from __future__ import annotations

import logging

from helpers import (
    array_values, find_export, obj_ref_path, prop, prop_array, prop_object_path,
    prop_str, prop_tags, safe_load_package, short_name_from_path, unwrap_struct,
)

logger = logging.getLogger(__name__)


def _walk(provider, prefix: str):
    for path in provider.Files.Keys:
        if path.startswith(prefix) and path.endswith(".uasset"):
            yield path


# ---------------- Creature archetypes ----------------

def extract_archetype(provider, asset_path: str) -> dict | None:
    pkg_path = asset_path[:-7]
    package = safe_load_package(provider, pkg_path)
    if package is None:
        return None
    export = find_export(package, class_substring="UWEAIArchetypeDataAsset")
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


def _extract_initial_attrs(provider, pkg_path: str) -> dict[str, float] | None:
    """Read a `GE_<X>InitialAttributes` asset and return `{attr: value}`.

    Each modifier has an Attribute struct (with AttributeName) and a
    ModifierMagnitude struct whose ScalableFloatMagnitude.Value holds the
    base number. Override-op modifiers are direct values; we skip Add/
    Multiply variants since the base attribute set already has defaults.
    """
    package = safe_load_package(provider, pkg_path)
    if package is None:
        return None
    cdo = None
    for ex in package.GetExports():
        if str(ex.Name).startswith("Default__"):
            cdo = ex
            break
    if cdo is None:
        return None
    mods = prop_array(cdo, "Modifiers")
    out: dict[str, float] = {}
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
            out[str(name)] = float(val)
        except Exception:
            continue
    return out or None


def _build_initial_attrs_index(provider) -> dict[str, dict[str, float]]:
    """Walk `/Game/Blueprints/AbilitySystem/Effects/AI/InitialAttributes/`
    and return `{<creature_key>: {<attribute>: value}}`.

    The creature key is the part between `GE_` and `InitialAttributes`,
    lowercased so the archetype side can match case-insensitively.
    """
    prefix = "Subnautica2/Content/Blueprints/AbilitySystem/Effects/AI/InitialAttributes/"
    out: dict[str, dict[str, float]] = {}
    for path in _walk(provider, prefix):
        # `GE_MarrowBreachInitialAttributes.uasset` → marrowbreach
        # `GE_BulletheadInitialAttributes1.uasset` → bullethead (strip trailing digits)
        # `GE_Test_Creature1_InitialAttributes.uasset` → skip (test fixture)
        leaf = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if not leaf.startswith("GE_"):
            continue
        if "Test" in leaf or "_InitialAttribute" in leaf:
            # Underscore-separated test/fixture variants — skip.
            continue
        # Match `GE_NameInitialAttributes(\d*)`
        import re
        m = re.match(r"^GE_(.+?)InitialAttributes\d*$", leaf)
        if not m:
            continue
        key = m.group(1).lower()
        attrs = _extract_initial_attrs(provider, path[:-7])
        if attrs:
            out[key] = attrs
    logger.info("InitialAttributes index: %d creatures", len(out))
    return out


def _stats_for_archetype(
    archetype_id: str,
    attrs_index: dict[str, dict[str, float]],
) -> dict[str, float] | None:
    """Match `DA_<Name>Archetype` (or `_Archetype`) to a `GE_<X>InitialAttributes` key.

    Tries the cleaned slug verbatim first, then several rewrites that handle
    the build's naming inconsistencies (Marrowbreach/MarrowBreach,
    JetoCaris/Jetocaris, etc.).
    """
    raw = archetype_id
    if raw.startswith("DA_"):
        raw = raw[3:]
    raw = (
        raw.replace("Archetype", "")
        .replace("Archertype", "")
        .replace("ArcheType", "")
        .rstrip("_")
    )
    base = raw.lower().replace("_", "")
    # Strip variant/age suffixes so `MarrowbreachJuvenile` → `marrowbreach`.
    stripped = (
        base.replace("juvenile", "").replace("adult", "").replace("child", "").replace("mother", "")
        .replace("firstencounter", "").replace("outerbounds", "")
    )
    candidates = {raw.lower(), base, stripped}
    for c in candidates:
        if c in attrs_index:
            return attrs_index[c]
    return None


# Attribute names we care about for the wiki. Anything else extracted from
# the GE gets dropped — surfacing low-level engine values like `Bulk` would
# noise up the UI.
INTERESTING_ATTRS = {
    "MaxHealth": "max_health",
    "Health": "health",
    "MaxFood": "max_food",
    "Food": "food",
    "MaxStamina": "max_stamina",
    "Stamina": "stamina",
    "MaxTemper": "max_temper",
    "Temper": "temper",
    "MaxSwimSpeed": "max_swim_speed",
    "Bulk": "bulk",
}


def _curate_stats(attrs: dict[str, float]) -> dict[str, float]:
    """Filter to the wiki's curated attribute subset, with snake_case keys."""
    out: dict[str, float] = {}
    for raw_key, wiki_key in INTERESTING_ATTRS.items():
        if raw_key in attrs:
            out[wiki_key] = attrs[raw_key]
    return out


def _size_default_stats(
    keywords: list[str],
    large_defaults: dict[str, float] | None,
    small_defaults: dict[str, float] | None,
) -> dict[str, float] | None:
    """Fall back to the size-class GE defaults (LargeCreature / SmallCreature).

    SN2 doesn't ship a `MediumCreature` GE — medium-tagged creatures inherit
    LargeCreature attributes in the engine. Leviathans use LargeCreature too.
    """
    kw = set(keywords or [])
    if "AI.Size.Small" in kw and small_defaults:
        return small_defaults
    if (
        "AI.Size.Big" in kw
        or "AI.Size.Medium" in kw
        or "AI.Size.Leviathan" in kw
        or "AI.Archetype.Leviathan" in kw
    ) and large_defaults:
        return large_defaults
    return None


def run_archetypes(provider) -> list[dict]:
    paths = sorted(_walk(provider, "Subnautica2/Content/Data/AI/"))
    logger.info("AI archetypes: %d candidates", len(paths))
    attrs_index = _build_initial_attrs_index(provider)
    # Size-class defaults for creatures with no specific InitialAttributes GE.
    large_defaults = _curate_stats(attrs_index.get("largecreature") or {})
    small_defaults = _curate_stats(attrs_index.get("smallcreature") or {})
    out = []
    for i, p in enumerate(paths, 1):
        e = extract_archetype(provider, p)
        if not e:
            continue
        attrs = _stats_for_archetype(e["id"], attrs_index)
        if attrs:
            stats = _curate_stats(attrs)
            if stats:
                e["stats"] = stats
                e["stats_source"] = "specific"
        else:
            # Fall back to size-class defaults so the wiki can still show
            # sensible numbers — mark them as `size-default` so the UI can
            # render a note.
            fallback = _size_default_stats(
                e.get("keywords") or [], large_defaults, small_defaults
            )
            if fallback:
                e["stats"] = fallback
                e["stats_source"] = "size-default"
        out.append(e)
        if i % 100 == 0:
            logger.info("  archetypes: %d / %d", i, len(paths))
    with_stats = sum(1 for r in out if r.get("stats"))
    logger.info(
        "AI archetypes: %d extracted (%d with stats)", len(out), with_stats
    )
    return out


# ---------------- Static gameplay tags ----------------

def extract_tags(provider, asset_path: str) -> dict | None:
    pkg_path = asset_path[:-7]
    package = safe_load_package(provider, pkg_path)
    if package is None:
        return None
    export = find_export(package, class_substring="UWEGameplayTagsData")
    if export is None:
        return None
    return {
        "id": short_name_from_path(pkg_path),
        "asset": pkg_path,
        "tags": prop_tags(export, "Tags"),
    }


def run_tags(provider) -> list[dict]:
    paths = sorted(_walk(provider, "Subnautica2/Content/Data/StaticGameplayTags/"))
    out = [r for r in (extract_tags(provider, p) for p in paths) if r]
    logger.info("StaticGameplayTags: %d extracted", len(out))
    return out


# ---------------- Ability sets ----------------

def extract_ability_set(provider, asset_path: str) -> dict | None:
    pkg_path = asset_path[:-7]
    package = safe_load_package(provider, pkg_path)
    if package is None:
        return None
    export = find_export(package, class_substring="UWEAbilitySet")
    if export is None:
        return None
    granted_abilities = []
    for el in array_values(prop_array(export, "GrantedAbilities")):
        p = obj_ref_path(el) or str(el)
        if p:
            granted_abilities.append(p)
    granted_effects = []
    for el in array_values(prop_array(export, "GrantedEffects")):
        p = obj_ref_path(el) or str(el)
        if p:
            granted_effects.append(p)
    return {
        "id": short_name_from_path(pkg_path),
        "asset": pkg_path,
        "granted_abilities": granted_abilities,
        "granted_effects": granted_effects,
        "tag_response_table": prop_object_path(export, "TagResponseTable"),
    }


def run_ability_sets(provider) -> list[dict]:
    paths = sorted(_walk(provider, "Subnautica2/Content/Data/AbilitySets/"))
    out = [r for r in (extract_ability_set(provider, p) for p in paths) if r]
    logger.info("AbilitySets: %d extracted", len(out))
    return out
