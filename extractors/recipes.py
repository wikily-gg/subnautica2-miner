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


# Cache: event_asset path (e.g. "/Game/Data/ScanData/Tools/DA_Tools_Flashlight_ScanData.DA_Tools_Flashlight_ScanData")
# -> {"num_required": int, "name": str|None, "thumbnail": str|None}
# Computed lazily as we walk recipes; one ScanData asset may be referenced by
# multiple recipes (rare, but cheap to cache).
_scan_data_cache: dict[str, dict | None] = {}


def _canonical_game_path(path: str | None) -> str | None:
    """Strip any class prefix and trailing apostrophe.

    Inputs we see in the wild:
      - ``/Game/Data/ScanData/Tools/DA_X.DA_X``
      - ``UWEScanData'/Game/Data/ScanData/Tools/DA_X.DA_X'``
      - ``UWEScanData'/Game/Foo.Bar:Sub'``
      - ``None`` / ``""``
    Output: ``/Game/Data/ScanData/Tools/DA_X.DA_X`` (or ``None``).
    """
    if not path:
        return None
    s = str(path)
    if s in ("None", "0", ""):
        return None
    # ``Class'/Game/...'`` form
    if "'" in s:
        s = s.split("'", 1)[1]
        if s.endswith("'"):
            s = s[:-1]
    if not s or s in ("None", "0"):
        return None
    if not s.startswith("/Game/"):
        return None
    return s


def _gamepath_to_pkg_path(path: str | None) -> str | None:
    """Convert ``/Game/Foo/Bar.Bar`` to ``Subnautica2/Content/Foo/Bar``.

    Accepts both canonical paths and raw ``Class'/Game/...'`` strings.
    Strips the trailing ``.<AssetName>`` suffix so the path can be fed to
    ``safe_load_package``.
    """
    canon = _canonical_game_path(path)
    if canon is None:
        return None
    p = "Subnautica2/Content/" + canon[len("/Game/"):]
    # Sub-object references look like ``/Game/Foo/Bar.Bar:Sub`` — drop the sub.
    if ":" in p:
        p = p.split(":", 1)[0]
    last = p.rsplit("/", 1)[-1]
    if "." in last:
        head, _ = p.rsplit(".", 1)
        p = head
    return p


def _resolve_scan_data(provider, event_asset_path: str | None) -> dict | None:
    """Load a UWEScanData asset and return its display-relevant fields.

    Returns a dict ``{"num_required": int, "name": str|None, "thumbnail": str|None}``
    or ``None`` if the path doesn't resolve to a UWEScanData asset.

    Caches by canonical path because the same ScanData can be referenced by
    multiple recipe unlock entries via different string forms.
    """
    canon = _canonical_game_path(event_asset_path)
    if canon is None:
        return None
    if canon in _scan_data_cache:
        return _scan_data_cache[canon]
    pkg_path = _gamepath_to_pkg_path(canon)
    if pkg_path is None:
        _scan_data_cache[canon] = None
        return None
    package = safe_load_package(provider, pkg_path)
    if package is None:
        _scan_data_cache[canon] = None
        return None
    export = find_export(package, class_substring="UWEScanData")
    if export is None:
        _scan_data_cache[canon] = None
        return None
    data = {
        # NumRequired is the player-facing fragment count ("Scan 2 fragments
        # to unlock"). Defaults to 1 if unset on the asset.
        "num_required": prop_int(export, "NumRequired", 1),
        "name": prop_str(export, "Name") or None,
        "thumbnail": prop_object_path(export, "Thumbnail"),
    }
    _scan_data_cache[canon] = data
    return data


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
            event_asset = _canonical_game_path(
                prop_object_path(iu, "EventAsset")
                or obj_ref_path(prop(iu, "EventAsset"))
            )
            # For scan events the on-recipe RequiredCount is always 1
            # ("the player needs to complete the scan once"), and the
            # player-facing fragment count actually lives on the
            # ScanData asset that EventAsset points to (UWEScanData.
            # NumRequired). Flashlight: RequiredCount=1 + NumRequired=2
            # = "Scan 2 fragments". Resolve and surface both so the
            # frontend can prefer scan_num_required when it's > 1.
            scan = _resolve_scan_data(provider, event_asset)
            rule_entries.append({
                "story_goal": prop_object_path(iu, "RequiredStoryGoal") or obj_ref_path(prop(iu, "RequiredStoryGoal")),
                "tag": (prop_tags(iu, "RequiredTag") or [None])[0],
                "rule": prop_str(iu, "RuleName"),
                "required_count": prop_int(iu, "RequiredCount"),
                "event_type": prop_enum(iu, "EventType"),
                "event_asset": event_asset,
                "scope": prop_enum(iu, "RequirementScope"),
                "scan_num_required": scan["num_required"] if scan else None,
                "scan_name": scan["name"] if scan else None,
                "scan_thumbnail": scan["thumbnail"] if scan else None,
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
