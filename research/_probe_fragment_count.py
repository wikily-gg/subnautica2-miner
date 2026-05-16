"""Find out why miner shows RequiredCount=1 for Flashlight when in-game shows 2.

In-game truth (per user):
 - Flashlight needs 2 fragments
 - Habitat Builder needs 2 fragments

The recipe RequiredCount field is 1. The count must live elsewhere - either on the
ScanData asset, or as a separate UnlockMode/RuleDefinition, or computed from how
many fragment placements exist in the world.

Strategy:
 1. Find Habitat Builder recipe (path unknown).
 2. Dump every nested struct on Flashlight recipe including EventTrackerVerbTag
    and EventTag (FStructFallback contents we skipped earlier).
 3. Dump the ScanData asset for Flashlight (DA_Tools_Flashlight_ScanData).
 4. Search the world for Flashlight fragment placements - maybe count == placements.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import logging
logging.basicConfig(level=logging.WARNING)

from provider import create_provider
from helpers import safe_load_package, find_export, prop

prov = create_provider()


def list_assets(substring: str, max_n: int = 20):
    out = []
    sub = substring.lower()
    for path in prov.Files.Keys:
        if sub in path.lower() and path.endswith(".uasset"):
            out.append(path)
            if len(out) >= max_n:
                break
    return out


def dump_props(obj, indent="  "):
    """Walk every property of an export/struct and print everything."""
    if obj is None:
        print(f"{indent}<None>")
        return
    props = getattr(obj, "Properties", None) or []
    for tag in props:
        name = tag.Name.Text
        try:
            t = tag.Tag
            cls = t.__class__.__name__
            try:
                val = t.GenericValue
            except Exception:
                val = "<no GenericValue>"
            print(f"{indent}{name}: {cls} = {val!r}")
            # If the tag wraps a struct, recurse
            inner = getattr(t, "Value", None)
            if inner is not None and hasattr(inner, "Properties"):
                print(f"{indent}  -- {name} inner struct --")
                dump_props(inner, indent + "    ")
            # FStructFallback path
            sf = getattr(t, "StructType", None)
            if sf is not None:
                pass  # already covered above usually
        except Exception as exc:
            print(f"{indent}{name}: <error {exc}>")


print("\n=== STEP 1: search for Habitat Builder recipe ===")
candidates = []
for needle in ["HabitatBuilder", "Habitat_Builder", "Habitat"]:
    found = list_assets(needle, max_n=80)
    candidates.extend(found)
seen = set()
candidates = [c for c in candidates if not (c in seen or seen.add(c))]
for c in candidates:
    print(" ", c)

print("\n=== STEP 2: dump Flashlight recipe FULL ===")
fl_path = "Subnautica2/Content/Data/CraftingRecipes/Fabricator/DA_FlashlightRecipe"
pkg = safe_load_package(prov, fl_path)
if pkg is None:
    # Try alternate paths
    for path in list_assets("FlashlightRecipe", max_n=10):
        print(f"  found alt: {path}")
        pkg = safe_load_package(prov, path[:-7])
        if pkg is not None:
            fl_path = path[:-7]
            break

if pkg:
    print(f"\nLoaded {fl_path}")
    for ex in pkg.GetExports():
        cls = ex.ExportType if hasattr(ex, "ExportType") else type(ex).__name__
        print(f"\n-- Export class: {cls} --")
        dump_props(ex)

print("\n=== STEP 3: dump Flashlight ScanData ===")
sd_path = "Subnautica2/Content/Data/ScanData/Tools/DA_Tools_Flashlight_ScanData"
pkg = safe_load_package(prov, sd_path)
if pkg is None:
    for path in list_assets("Flashlight_ScanData", max_n=5):
        print(f"  found alt: {path}")
        pkg = safe_load_package(prov, path[:-7])
        if pkg is not None:
            sd_path = path[:-7]
            break

if pkg:
    print(f"\nLoaded {sd_path}")
    for ex in pkg.GetExports():
        cls = ex.ExportType if hasattr(ex, "ExportType") else type(ex).__name__
        print(f"\n-- Export class: {cls} --")
        dump_props(ex)

print("\n=== STEP 4: search for any *ScanData with multi-scan hint ===")
for path in list_assets("ScanData", max_n=4):
    pkg = safe_load_package(prov, path[:-7])
    if pkg is None:
        continue
    print(f"\n-- {path} --")
    for ex in pkg.GetExports():
        cls = ex.ExportType if hasattr(ex, "ExportType") else type(ex).__name__
        if "ScanData" not in cls and "Scan" not in cls:
            continue
        print(f"   class: {cls}")
        dump_props(ex, "     ")
