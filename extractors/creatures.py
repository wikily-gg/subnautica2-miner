"""Extract UWEAIArchetypeDataAsset (creatures), GameplayTags, AbilitySets."""
from __future__ import annotations

import logging

from helpers import (
    array_values, find_export, obj_ref_path, prop, prop_array, prop_object_path,
    prop_str, prop_tags, safe_load_package, short_name_from_path,
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


def run_archetypes(provider) -> list[dict]:
    paths = sorted(_walk(provider, "Subnautica2/Content/Data/AI/"))
    logger.info("AI archetypes: %d candidates", len(paths))
    out = []
    for i, p in enumerate(paths, 1):
        e = extract_archetype(provider, p)
        if e:
            out.append(e)
        if i % 100 == 0:
            logger.info("  archetypes: %d / %d", i, len(paths))
    logger.info("AI archetypes: %d extracted", len(out))
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
