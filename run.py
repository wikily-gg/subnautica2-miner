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
from extractors import vehicles as ex_vehicles  # noqa: E402
from extractors import landscape_heights as ex_landscape_heights  # noqa: E402
from extractors import seafloor_mesh as ex_seafloor_mesh  # noqa: E402
from extractors import creature_spawns as ex_creature_spawns  # noqa: E402

# Icons / R2 upload are pulled in lazily so JSON-only runs don't hard-fail
# if Pillow / texture2ddecoder / boto3 aren't installed.


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
    if _enabled("vehicles"):
        _write("vehicles", ex_vehicles.run(provider))
        flush_memory()
    if _enabled("characters"):
        _write("characters", ex_misc.run_characters(provider))
    if _enabled("surface_spawn"):
        _write("surface_spawn", ex_misc.run_surface_spawn(provider))
    if _enabled("string_tables"):
        _write("string_tables", ex_strings.run(provider))
        flush_memory()
    if _enabled("locales"):
        # Per-locale `.locres` overlays for the 11 SN2 languages. Writes one
        # JSON per locale at `out/string_tables_<wiki_locale>.json` so the
        # wiki's loader can fetch the user-locale file and only fall back to
        # English when a key is missing.
        per_locale = ex_strings.run_locales(provider)
        for wiki_locale, tables in per_locale.items():
            _write(f"string_tables_{wiki_locale}", tables)
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
    if _enabled("terrain"):
        _write("terrain_heights", ex_landscape_heights.run(provider))
    if _enabled("seafloor"):
        _write("seafloor_mesh", ex_seafloor_mesh.run(provider))
    if _enabled("creature_spawns"):
        _write("creature_spawns", ex_creature_spawns.run(provider))


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


def run_icons() -> None:
    """Decode every Texture2D referenced by the extracted JSONs -> WebP."""
    from icons.extractor import extract_all
    extract_all()


def run_mesh_discover() -> None:
    """Walk paks for SkeletalMesh / StaticMesh assets and dump categorized list."""
    from meshes.discover import main as discover_main
    discover_main()


def run_mesh_export(args: list[str]) -> None:
    """Export glb + textures for a slug list, --all, or --filter <substr>."""
    from meshes.exporter import export_slugs, export_all
    if "--all" in args:
        export_all()
        return
    if "--filter" in args:
        idx = args.index("--filter")
        sub = args[idx + 1] if idx + 1 < len(args) else ""
        export_all(filter_substr=sub)
        return
    slugs = [a for a in args if not a.startswith("--")]
    if not slugs:
        logging.error("usage: python run.py mesh-export <slug> [<slug>...] | --all | --filter <substr>")
        return
    export_slugs(slugs)


def run_mesh_render(args: list[str]) -> None:
    """Render glbs to 1024x1024 PNGs via headless Blender.

    Flags:
      --all                       render every CATALOG entry
      --filter <substr>           filter CATALOG by substring
      --angles <a,b,c>            comma-separated angle list. Each angle
                                  can be: auto, front, side, back, or an
                                  azimuth in degrees. Default: auto only.
                                  Multiple angles append `_<angle>` to
                                  the output filename.
    """
    from meshes.renderer import render_slugs, render_all

    angles = ["auto"]
    if "--angles" in args:
        idx = args.index("--angles")
        if idx + 1 < len(args):
            raw = args[idx + 1]
            angles = [a.strip() for a in raw.split(",") if a.strip()]

    if "--all" in args:
        render_all(angles=angles)
        return
    if "--filter" in args:
        idx = args.index("--filter")
        sub = args[idx + 1] if idx + 1 < len(args) else ""
        render_all(filter_substr=sub, angles=angles)
        return

    # Skip --angles + its value when collecting slug positional args
    skip_next = False
    slugs: list[str] = []
    for a in args:
        if skip_next:
            skip_next = False
            continue
        if a == "--angles":
            skip_next = True
            continue
        if a.startswith("--"):
            continue
        slugs.append(a)
    if not slugs:
        logging.error("usage: python run.py mesh-render <slug> [<slug>...] [--angles a,b,c] | --all | --filter <substr>")
        return
    render_slugs(slugs, angles=angles)


def run_mesh_composite(args: list[str]) -> None:
    """Composite multi-part vehicles into a single PNG via Blender.

    Flags:
      --all                       render every VEHICLE_ASSEMBLIES entry
      --filter <substr>           filter assemblies by substring
      --force-export              re-export part GLBs even if cached
    """
    from meshes.composite import render_assembly, render_assemblies
    from meshes.exporter import VEHICLE_ASSEMBLIES

    force_export = "--force-export" in args

    if "--all" in args:
        render_assemblies(force_export=force_export)
        return
    if "--filter" in args:
        idx = args.index("--filter")
        sub = args[idx + 1] if idx + 1 < len(args) else ""
        render_assemblies(filter_substr=sub, force_export=force_export)
        return

    # Skip flags + their values when collecting positional args
    skip_next = False
    slugs: list[str] = []
    for a in args:
        if skip_next:
            skip_next = False
            continue
        if a == "--filter":
            skip_next = True
            continue
        if a.startswith("--"):
            continue
        slugs.append(a)
    if not slugs:
        logging.error(
            "usage: python run.py mesh-composite <assembly> [<assembly>...] "
            "| --all | --filter <substr>\n"
            "Known assemblies: %s",
            ", ".join(sorted(VEHICLE_ASSEMBLIES.keys())),
        )
        return
    for s in slugs:
        render_assembly(s, force_export=force_export)


def run_upload(backup: bool = False, force: bool = False) -> None:
    """Upload out/ to R2.

    Layout:
        out/icons/<name>.webp   -> images/subnautica-2/icons/<name>.webp
        out/renders/<name>.png  -> images/subnautica-2/renders/<name>.png
        out/*.json              -> subnautica-2/data/<name>.json
    """
    from upload import (
        get_r2_client,
        upload_directory,
        R2_ICON_PREFIX,
        R2_RENDER_PREFIX,
        R2_DATA_PREFIX,
    )

    client = get_r2_client()
    if client is None:
        logging.error("R2 credentials missing - aborting upload")
        return

    icons_dir = os.path.join(config.OUTPUT_DIR, "icons")
    if os.path.isdir(icons_dir):
        logging.info("Uploading icons -> %s", R2_ICON_PREFIX)
        upload_directory(icons_dir, R2_ICON_PREFIX, r2_client=client, backup=backup, force=force)
    else:
        logging.info("No out/icons/ directory - skipping icon upload")

    renders_dir = os.path.join(config.OUTPUT_DIR, "renders")
    if os.path.isdir(renders_dir):
        logging.info("Uploading renders -> %s", R2_RENDER_PREFIX)
        upload_directory(renders_dir, R2_RENDER_PREFIX, r2_client=client, backup=backup, force=force)

    # JSON data files - top-level *.json in out/, not inside subdirs
    import glob
    json_files = sorted(glob.glob(os.path.join(config.OUTPUT_DIR, "*.json")))
    if json_files:
        data_prefix = R2_DATA_PREFIX + "data/"
        logging.info("Uploading %d JSON files -> %s", len(json_files), data_prefix)
        _upload_json_files(client, json_files, data_prefix, force=force)
    else:
        logging.info("No top-level *.json files - skipping data upload")


def _upload_json_files(r2_client, paths: list[str], r2_prefix: str, force: bool = False) -> None:
    """Upload a list of JSON files to R2 with the same smart-diff logic
    upload_directory uses, but driven by an explicit list (we don't want
    to pull in icons/ or renders/ subdirs)."""
    import hashlib, json as _json
    from upload import upload_file, _LOCAL_MANIFEST_PATH

    old_manifest: dict[str, str] = {}
    if os.path.exists(_LOCAL_MANIFEST_PATH) and not force:
        try:
            with open(_LOCAL_MANIFEST_PATH, "r") as f:
                old_manifest = _json.load(f)
        except Exception:
            pass
    new_manifest = dict(old_manifest)

    uploaded = 0
    skipped = 0
    for p in paths:
        name = os.path.basename(p)
        key = f"{r2_prefix}{name}"
        with open(p, "rb") as fh:
            md5 = hashlib.md5(fh.read()).hexdigest()
        new_manifest[key] = md5
        if not force and old_manifest.get(key) == md5:
            skipped += 1
            continue
        if upload_file(r2_client, p, key):
            uploaded += 1

    os.makedirs(os.path.dirname(_LOCAL_MANIFEST_PATH), exist_ok=True)
    with open(_LOCAL_MANIFEST_PATH, "w") as f:
        _json.dump(new_manifest, f, indent=2)

    logging.info("JSON upload: %d uploaded, %d unchanged (%d total)", uploaded, skipped, len(paths))


def main():
    args = sys.argv[1:]
    if not args:
        args = ["all"]

    # Special non-extractor commands
    if "icons" in args and args == ["icons"]:
        run_icons()
        return
    if args[0] == "upload":
        backup = "--backup" in args
        force = "--force" in args
        run_upload(backup=backup, force=force)
        return
    if args[0] == "mesh-discover":
        run_mesh_discover()
        return
    if args[0] == "mesh-export":
        run_mesh_export(args[1:])
        return
    if args[0] == "mesh-render":
        run_mesh_render(args[1:])
        return
    if args[0] == "mesh-composite":
        run_mesh_composite(args[1:])
        return
    if args[0] == "item-mesh-discover":
        # Walk every item BP and write out/item_meshes.json with the
        # static / skeletal mesh refs needed to render inventory
        # thumbnails for icon-less items.
        from extractors import item_meshes as ex_item_meshes
        provider = create_provider()
        rows = ex_item_meshes.run(provider)
        _write("item_meshes", rows)
        return
    if args[0] == "item-icons":
        # Render PNG thumbnails for icon-less items using their
        # actor-class BP's preview mesh. Output: out/renders/<mesh>.png.
        from meshes.item_render import run as run_item_icons
        include_all = "--all" in args[1:]
        skip_existing = "--force" not in args[1:]
        filter_substr = None
        rest = args[1:]
        if "--filter" in rest:
            idx = rest.index("--filter")
            if idx + 1 < len(rest):
                filter_substr = rest[idx + 1]
        run_item_icons(
            include_all=include_all,
            filter_substr=filter_substr,
            skip_existing=skip_existing,
        )
        return

    provider = create_provider()

    if "smoke" in args:
        run_smoke(provider)
        return

    if args[0] == "all":
        run_full(provider, only=None)
        # `all` does NOT include icons - run them explicitly with
        # `python run.py icons` after extraction completes.
    else:
        run_full(provider, only=set(args))


if __name__ == "__main__":
    main()
