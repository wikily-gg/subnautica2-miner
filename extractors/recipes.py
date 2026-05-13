"""Extract UWECraftingRecipe and SN2BuilderConstructActionData."""
from __future__ import annotations

import logging

from helpers import (
    _export_class, array_values, find_export, find_exports_by_class,
    obj_ref_path, prop, prop_array, prop_enum, prop_int, prop_object_path,
    prop_str, prop_tags, safe_load_package, short_name_from_path,
    struct_int, struct_obj_path, struct_str, struct_tags, unwrap_struct,
)

logger = logging.getLogger(__name__)

RECIPES_DIR = "Subnautica2/Content/Data/CraftingRecipes/"
BUILDER_DIR = "Subnautica2/Content/Data/BaseBuilding/"


def find_recipe_paths(provider) -> list[str]:
    out = []
    for path in provider.Files.Keys:
        if path.startswith(RECIPES_DIR) and path.endswith(".uasset"):
            out.append(path)
    return sorted(out)


def find_builder_paths(provider) -> list[str]:
    out = []
    for path in provider.Files.Keys:
        if path.startswith(BUILDER_DIR) and path.endswith(".uasset"):
            out.append(path)
    return sorted(out)


def _struct_to_item_count(s):
    """Convert a recipe Output/Requirement struct -> {item_type, count}."""
    u = unwrap_struct(s)
    if u is None:
        return None
    return {
        "item_type": prop_object_path(u, "ItemType")
                     or prop_object_path(u, "ItemData")
                     or prop_object_path(u, "ItemClass"),
        "count": prop_int(u, "NumItems") or prop_int(u, "Count") or prop_int(u, "Quantity") or 1,
    }


def extract_recipe(provider, asset_path: str) -> dict | None:
    pkg_path = asset_path[:-7]
    package = safe_load_package(provider, pkg_path)
    if package is None:
        return None
    export = find_export(package, class_substring="UWECraftingRecipe")
    if export is None:
        return None

    outputs = []
    for el in array_values(prop_array(export, "Output")):
        ic = _struct_to_item_count(el)
        if ic:
            outputs.append(ic)
    reqs = []
    for el in array_values(prop_array(export, "Requirements")):
        ic = _struct_to_item_count(el)
        if ic:
            reqs.append(ic)
    unlocks = []
    for el in array_values(prop_array(export, "UpdatedUnlockingRequirements")):
        u = unwrap_struct(el)
        if u is None:
            continue
        rule_entries = []
        for inner in array_values(prop_array(u, "Entries")):
            iu = unwrap_struct(inner)
            if iu is None:
                continue
            rule_entries.append({
                "story_goal": prop_object_path(iu, "RequiredStoryGoal") or obj_ref_path(prop(iu, "RequiredStoryGoal")),
                "tag": (prop_tags(iu, "RequiredTag") or [None])[0],
                "rule": prop_str(iu, "RuleName"),
            })
        unlocks.append({
            "rule_name": prop_str(u, "RuleName"),
            "entries": rule_entries,
        })

    return {
        "id": short_name_from_path(pkg_path),
        "asset": pkg_path,
        "name": prop_str(export, "Name"),
        "description": prop_str(export, "Description"),
        "thumbnail": prop_object_path(export, "Thumbnail"),
        "outputs": outputs,
        "requirements": reqs,
        "default_state": prop_enum(export, "DefaultRecipeState"),
        "unlocking_requirements": unlocks,
        "category": prop_object_path(export, "Category"),
        "ordering_index": prop_int(export, "OrderingIndex"),
        "duplicates_builder_action": bool(prop_str(export, "DuplicatesBuilderActionData")),
        "published_status": prop_enum(export, "PublishedStatus"),
    }


def extract_builder_action(provider, asset_path: str) -> dict | None:
    pkg_path = asset_path[:-7]
    package = safe_load_package(provider, pkg_path)
    if package is None:
        return None
    # SN2BuilderConstructActionData / SN2BuilderDestroyActionData / ...
    export = None
    for e in package.GetExports():
        cls = _export_class(e)
        if cls.startswith("SN2Builder") or "BuilderActionData" in cls:
            export = e
            break
    if export is None:
        return None
    return {
        "id": short_name_from_path(pkg_path),
        "asset": pkg_path,
        "action_class": _export_class(export),
        "name": prop_str(export, "Name"),
        "description": prop_str(export, "Description") or prop_str(export, "SecondaryDescription"),
        "thumbnail": prop_object_path(export, "Thumbnail"),
        "recipe": prop_object_path(export, "Recipe"),
        "category": prop_enum(export, "Category"),
        "published_status": prop_enum(export, "PublishedStatus"),
    }


def run(provider) -> dict:
    recipe_paths = find_recipe_paths(provider)
    builder_paths = find_builder_paths(provider)
    logger.info("Recipes: %d candidates; Builder actions: %d candidates",
                len(recipe_paths), len(builder_paths))

    recipes = []
    for i, p in enumerate(recipe_paths, 1):
        r = extract_recipe(provider, p)
        if r:
            recipes.append(r)
        if i % 100 == 0:
            logger.info("  recipes: %d / %d", i, len(recipe_paths))

    actions = []
    for i, p in enumerate(builder_paths, 1):
        a = extract_builder_action(provider, p)
        if a:
            actions.append(a)
        if i % 200 == 0:
            logger.info("  builder: %d / %d", i, len(builder_paths))

    logger.info("Recipes: %d extracted; Builder actions: %d extracted",
                len(recipes), len(actions))
    return {"recipes": recipes, "builder_actions": actions}
