"""
Subnautica 2 data miner — top-level runner.

Usage:
    python run.py all                  # run every extractor
    python run.py items recipes        # run a subset
    python run.py smoke                # quick smoke test (~20 assets each)
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys

# Force UTF-8 on Windows console
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import config  # noqa: E402
from provider import create_provider, flush_memory  # noqa: E402

from extractors import items as ex_items  # noqa: E402
from extractors import recipes as ex_recipes  # noqa: E402
from extractors import databank as ex_databank  # noqa: E402
from extractors import creatures as ex_creatures  # noqa: E402
from extractors import misc as ex_misc  # noqa: E402
from extractors import string_tables as ex_strings  # noqa: E402
from extractors import world_map as ex_world  # noqa: E402
from extractors import biomes as ex_biomes  # noqa: E402
from extractors import regions as ex_regions  # noqa: E402


def _write(name: str, data) -> None:
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    path = os.path.join(config.OUTPUT_DIR, f"{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    if isinstance(data, list):
        size = len(data)
    elif isinstance(data, dict):
        size = sum(len(v) if isinstance(v, list) else 1 for v in data.values())
    else:
        size = "?"
    logging.info("wrote %s (%s rows) -> %s", name, size, path)


def run_full(provider, only: set[str] | None = None) -> None:
    """Run every extractor. *only* limits to a specific set."""
    def _enabled(name):
        return only is None or name in only

    if _enabled("items"):
        _write("items", ex_items.run(provider))
        flush_memory()
    if _enabled("recipes"):
        _write("recipes", ex_recipes.run(provider))
        flush_memory()
    if _enabled("databank"):
        _write("databank", ex_databank.run_databank(provider))
        flush_memory()
    if _enabled("scan_data"):
        _write("scan_data", ex_databank.run_scan(provider))
        flush_memory()
    if _enabled("story_goals"):
        _write("story_goals", ex_databank.run_story(provider))
        flush_memory()
    if _enabled("pings"):
        _write("pings", ex_databank.run_pings(provider))
    if _enabled("locations"):
        _write("locations", ex_databank.run_locations(provider))
    if _enabled("archetypes"):
        _write("creature_archetypes", ex_creatures.run_archetypes(provider))
        flush_memory()
    if _enabled("tags"):
        _write("static_gameplay_tags", ex_creatures.run_tags(provider))
    if _enabled("ability_sets"):
        _write("ability_sets", ex_creatures.run_ability_sets(provider))
    if _enabled("biomods"):
        _write("biomods", ex_misc.run_biomods(provider))
    if _enabled("resonatables"):
        _write("resonatables", ex_misc.run_resonatables(provider))
    if _enabled("characters"):
        _write("characters", ex_misc.run_characters(provider))
    if _enabled("surface_spawn"):
        _write("surface_spawn", ex_misc.run_surface_spawn(provider))
    if _enabled("string_tables"):
        _write("string_tables", ex_strings.run(provider))
        flush_memory()
    if _enabled("pop_settings"):
        _write("pop_settings", ex_world.run_pop_settings(provider))
    if _enabled("region_loot"):
        _write("region_loot", ex_world.run_region_loot(provider))
    if _enabled("landscape"):
        _write("landscape_mappings", ex_world.run_landscape(provider))
    if _enabled("world_map"):
        _write("world_map", ex_world.run(provider))
    if _enabled("biomes"):
        _write("biome_points", ex_biomes.run(provider))
    if _enabled("regions"):
        result = ex_regions.run(provider)
        _write("world_boundaries", result["boundaries"])
        _write("regions", result["regions"])
        _write("region_zones", result["zones"])
        _write("biome_points_v2", result["classified"])


def run_smoke(provider) -> None:
    """Process ~10 assets per extractor for fast validation."""
    print("\n=== SMOKE TEST ===\n")
    # Items
    paths = ex_items.find_paths(provider)[:10]
    items = [r for r in (ex_items.extract_item(provider, p) for p in paths) if r]
    print(f"items: {len(items)} sample")
    for it in items[:3]:
        print(" ", it)

    # Recipes
    paths = ex_recipes.find_recipe_paths(provider)[:10]
    recipes = [r for r in (ex_recipes.extract_recipe(provider, p) for p in paths) if r]
    print(f"recipes: {len(recipes)} sample")
    for r in recipes[:2]:
        print(" ", r)

    # Databank
    paths = sorted(p for p in provider.Files.Keys
                   if p.startswith("Subnautica2/Content/Data/DatabankEntry/") and p.endswith(".uasset"))[:10]
    out = [r for r in (ex_databank.extract_databank(provider, p) for p in paths) if r]
    print(f"databank: {len(out)} sample")
    if out:
        print(" ", out[0])

    # Creatures
    paths = sorted(p for p in provider.Files.Keys
                   if p.startswith("Subnautica2/Content/Data/AI/") and p.endswith(".uasset"))[:10]
    out = [r for r in (ex_creatures.extract_archetype(provider, p) for p in paths) if r]
    print(f"archetypes: {len(out)} sample")
    if out:
        print(" ", out[0])

    # Biomods
    paths = sorted(p for p in provider.Files.Keys
                   if p.startswith("Subnautica2/Content/Data/Biomods/") and p.endswith(".uasset"))[:10]
    out = [r for r in (ex_misc.extract_biomod(provider, p) for p in paths) if r]
    print(f"biomods: {len(out)} sample")
    if out:
        print(" ", out[0])

    # Resonatable
    paths = sorted(p for p in provider.Files.Keys
                   if p.startswith("Subnautica2/Content/Data/Resonatable/") and p.endswith(".uasset"))[:10]
    out = [r for r in (ex_misc.extract_resonatable(provider, p) for p in paths) if r]
    print(f"resonatables: {len(out)} sample")
    if out:
        print(" ", out[0])

    # String tables
    paths = sorted(p for p in provider.Files.Keys
                   if p.startswith("Subnautica2/Content/StringTables/") and p.endswith(".uasset"))[:5]
    out = [r for r in (ex_strings.extract(provider, p) for p in paths) if r]
    print(f"string_tables: {len(out)} sample")
    if out:
        sample = out[0]
        keys = list(sample.get("entries", {}).items())[:3]
        print(" ", {"id": sample["id"], "row_count": sample["row_count"], "first_rows": keys})


def main():
    args = sys.argv[1:]
    if not args:
        args = ["all"]

    provider = create_provider()

    if "smoke" in args:
        run_smoke(provider)
        return

    if args[0] == "all":
        run_full(provider, only=None)
    else:
        run_full(provider, only=set(args))


if __name__ == "__main__":
    main()
