"""Confirm the new ScanData resolution picks up NumRequired correctly."""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import logging
logging.basicConfig(level=logging.WARNING)

from provider import create_provider
from extractors.recipes import extract_recipe, _resolve_scan_data

prov = create_provider()

# Probe a few recipes via the now-resolved extractor
for sub in [
    "FlashlightRecipe",
    "HabitatBuilder",
    "Rebreather",
    "Scanner",
    "Knife",
]:
    found = [p for p in prov.Files.Keys
             if "/CraftingRecipes/" in p
             and sub.lower() in p.lower()
             and p.endswith(".uasset")]
    for path in found[:3]:
        r = extract_recipe(prov, path)
        if r is None:
            print(f"{path}: <not a recipe>")
            continue
        print(f"\n=== {r['id']} ===")
        print(f"  name = {r.get('name')!r}")
        for u in r.get("unlocking_requirements") or []:
            for e in u.get("entries") or []:
                print(
                    "    entry:"
                    f" req={e.get('required_count')}"
                    f" scan_n={e.get('scan_num_required')}"
                    f" event_asset={e.get('event_asset')}"
                    f" event_type={e.get('event_type')}"
                )
