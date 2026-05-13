"""Extract UWEItemType assets — every item the player can hold/use/find."""
from __future__ import annotations

import logging
from typing import Iterable

from helpers import (
    find_export, prop_str, prop_object_path, prop_tags,
    safe_load_package, short_name_from_path,
)

logger = logging.getLogger(__name__)

DIR_PREFIX = "Subnautica2/Content/Data/ItemType/"


def find_paths(provider) -> list[str]:
    out = []
    for path in provider.Files.Keys:
        if path.startswith(DIR_PREFIX) and path.endswith(".uasset"):
            out.append(path)
    return sorted(out)


def extract_item(provider, asset_path: str) -> dict | None:
    pkg_path = asset_path[:-7]
    package = safe_load_package(provider, pkg_path)
    if package is None:
        return None
    export = find_export(package, class_substring="UWEItemType")
    if export is None:
        return None

    return {
        "id": short_name_from_path(pkg_path),
        "asset": pkg_path,
        "name": prop_str(export, "Name"),
        "description": prop_str(export, "ItemDescription"),
        "actor_class": prop_object_path(export, "ActorClass"),
        "icon": prop_object_path(export, "Icon"),
        "thumbnail": prop_object_path(export, "Thumbnail"),
        "category": prop_object_path(export, "Category"),
        "tags": prop_tags(export, "IdentifierTag"),
        "category_tag": prop_tags(export, "CategoryTag"),
        "tooltip_icon": prop_object_path(export, "TooltipIcon"),
        "stack_size": int(getattr(export, "MaxStackSize", 0) or 0) or None,
    }


def run(provider) -> list[dict]:
    paths = find_paths(provider)
    logger.info("Items: %d candidate assets", len(paths))
    out = []
    for i, p in enumerate(paths, 1):
        item = extract_item(provider, p)
        if item is not None:
            out.append(item)
        if i % 200 == 0:
            logger.info("  items: %d / %d", i, len(paths))
    logger.info("Items: extracted %d", len(out))
    return out
