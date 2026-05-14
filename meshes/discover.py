"""
Walk the mounted SN2 paks and dump every SkeletalMesh / StaticMesh path
under categories we care about (vehicles, fauna/creatures, flora, resources).

Run via:
    python -m meshes.discover
or  python meshes/discover.py

Outputs:
    out/mesh_discovery.json   nested {category: {slug: pkg_path}}

This is just a survey - the curated render CATALOG lives in
meshes/exporter.py and is built BY HAND from this output.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
from provider import create_provider  # noqa: E402

logger = logging.getLogger(__name__)


# Subnautica 2 cooked paths look like:
#   Subnautica2/Content/Blueprints/Vehicles/Tadpole/Meshes/SK_Tadpole.uasset
#   Subnautica2/Content/Blueprints/Creatures/Fauna/Reaper/Meshes/SKM_Reaper.uasset
#   Subnautica2/Content/Art/Creatures/.../SK_*.uasset
#   Subnautica2/Content/Art/Vehicles/.../SK_*.uasset
# We match by directory keyword.

_MESH_PREFIXES = ("/SK_", "/SKM_", "/SM_")

# Keyword -> category. Order matters: vehicles checked before fauna because
# something like ".../Vehicles/.../Decals/SKM_Foo" should still bucket as vehicle.
_KEYWORDS: list[tuple[str, str]] = [
    ("/Vehicles/", "vehicles"),
    ("/Vehicle/", "vehicles"),
    ("/Tadpole/", "vehicles"),
    ("/Creatures/", "fauna"),
    ("/Creature/", "fauna"),
    ("/AI/Agents/", "fauna"),
    ("/Fauna/", "fauna"),
    ("/Fish/", "fauna"),
    ("/Flora/", "flora"),
    ("/Plants/", "flora"),
    ("/Coral/", "flora"),
    ("/Resonatable/", "resources"),
    ("/Resources/", "resources"),
    ("/Resource/", "resources"),
    ("/BaseBuilding/", "building"),
    ("/BaseObjects/", "building"),
    ("/Builder/", "building"),
    ("/Furniture/", "building"),
    ("/Habitat/", "building"),
    ("/Buildings/", "building"),
    ("/Architecture/", "building"),
    ("/Tools/", "items"),
    ("/Equipment/", "items"),
    ("/Items/", "items"),
    ("/Item/", "items"),
]


def _categorize(path: str) -> str | None:
    p = "/" + path.replace("\\", "/")
    for kw, cat in _KEYWORDS:
        if kw in p:
            return cat
    return None


# Junk suffix patterns - these are physics/skeleton/LOD/proxy assets that
# share the SK_/SM_ prefix but aren't renderable themselves.
_JUNK_SUFFIXES = (
    "_PhysicsAsset", "_Skeleton", "_LODSettings", "_Proxy", "_Collision",
    "_Skin", "_PostProcessAnimBP", "_AnimBP", "_AnimGraph",
    "_BluePrint",
)
# Also skip ldkit-pieces (small modular bits not whole creatures/objects)
# unless they happen to be the canonical body - we keep the curated CATALOG
# free from these. They go in the discovery file but get filtered out at
# render time via _is_renderable_slug.


def _is_mesh_asset(path: str) -> bool:
    if not path.lower().endswith(".uasset"):
        return False
    leaf = path.rsplit("/", 1)[-1]
    if not any(leaf.startswith(pref[1:]) for pref in _MESH_PREFIXES):
        return False
    stem = leaf[:-len(".uasset")] if leaf.lower().endswith(".uasset") else leaf
    if any(stem.endswith(s) for s in _JUNK_SUFFIXES):
        return False
    return True


def discover() -> dict[str, dict[str, str]]:
    provider = create_provider()
    logger.info("Walking %d files for mesh assets", provider.Files.Count)

    by_cat: dict[str, dict[str, str]] = {}
    for path in provider.Files.Keys:
        if not _is_mesh_asset(path):
            continue
        cat = _categorize(path)
        if cat is None:
            continue
        # Strip extension + Content prefix to get a /Game-style pkg path
        pkg = path
        if pkg.lower().endswith(".uasset"):
            pkg = pkg[:-7]
        # Subnautica2/Content/... -> /Game/...
        if pkg.startswith("Subnautica2/Content/"):
            pkg = "/Game/" + pkg[len("Subnautica2/Content/"):]
        slug = pkg.rsplit("/", 1)[-1]
        # Disambiguate slugs that repeat across directories
        existing = by_cat.setdefault(cat, {})
        if slug in existing and existing[slug] != pkg:
            # Use parent dir as disambiguator
            parts = pkg.split("/")
            for p in reversed(parts[:-1]):
                cand = f"{p}_{slug}"
                if cand not in existing:
                    slug = cand
                    break
        existing[slug] = pkg

    return by_cat


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    out = discover()
    out_path = os.path.join(config.OUTPUT_DIR, "mesh_discovery.json")
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False, sort_keys=True)

    print()
    print("Mesh discovery summary:")
    for cat in sorted(out):
        print(f"  {cat:12s} {len(out[cat]):>5d}")
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
