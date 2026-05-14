"""
Render inventory-preview thumbnails for SN2 items that lack a baked
2D icon.

The pipeline:

  1. `extractors.item_meshes.run()` walks every item BP and writes the
     mesh-component refs to `out/item_meshes.json`. Each row maps
     `item_id` -> `mesh_slug` + full package path.
  2. This module reads that manifest, filters to items that have NO
     icon in `items.json`, then runs the unique meshes through the
     existing `meshes.exporter` + `meshes.renderer` pipeline (mesh GLB
     export via CUE4Parse-Conversion, then Blender headless render to
     a transparent 1024x1024 PNG).
  3. Output PNGs land in `out/renders/<mesh_slug>.png` alongside the
     existing creature/vehicle/flora renders. The wiki frontend uses
     `iconName ?? renderUrl` so the new renders only fire as a final
     fallback when the icon resolution already failed.

CLI: `python run.py item-icons` (only renders icon-less items + skips
meshes that already have a render). Pass `--all` to also re-render
items that DO have a baked icon, or `--filter <substr>` to scope.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Iterable

import config

logger = logging.getLogger(__name__)


def _load_items_index() -> dict[str, dict]:
    """Map item_id -> item dict from items.json."""
    path = os.path.join(config.OUTPUT_DIR, "items.json")
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        items = json.load(f)
    return {i["id"]: i for i in items if isinstance(i, dict) and i.get("id")}


def _load_item_meshes() -> list[dict]:
    """List of {item_id, item_slug, mesh_slug, mesh_pkg, ...}"""
    path = os.path.join(config.OUTPUT_DIR, "item_meshes.json")
    if not os.path.isfile(path):
        logger.error("item_meshes.json missing - run "
                     "`python -m extractors.item_meshes` first")
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def collect_targets(*, include_all: bool = False, filter_substr: str | None = None) -> list[dict]:
    """Return the unique mesh export targets for the items we need
    renders for. Each target is `{mesh_slug, mesh_pkg}` - the caller
    can pass these to `meshes.exporter.export_one` / `renderer.render_one`.

    Filters:
      - include_all=False (default): only items where `items.json`
        has no icon. Avoids re-rendering items that already have a
        polished 2D icon in the build.
      - filter_substr: limit to items whose id contains this substring.
    """
    items_idx = _load_items_index()
    rows = _load_item_meshes()
    targets: dict[str, dict] = {}  # mesh_slug -> {mesh_slug, mesh_pkg, item_count}
    for r in rows:
        iid = r.get("item_id")
        if iid is None:
            continue
        it = items_idx.get(iid)
        if it is None:
            continue
        if not include_all and it.get("icon"):
            continue
        if filter_substr and filter_substr.lower() not in iid.lower():
            continue
        slug = r.get("mesh_slug")
        pkg = r.get("mesh_pkg")
        if not slug or not pkg:
            continue
        if slug in targets:
            targets[slug]["item_count"] += 1
        else:
            targets[slug] = {
                "mesh_slug": slug,
                "mesh_pkg": pkg,
                "item_count": 1,
            }
    return list(targets.values())


def run(
    *,
    include_all: bool = False,
    filter_substr: str | None = None,
    skip_existing: bool = True,
) -> dict[str, str]:
    """Export + render every unique mesh from `item_meshes.json` whose
    parent item has no baked icon. Returns mesh_slug -> output PNG path.

    `skip_existing=True` (default) skips meshes that already have a
    PNG on disk. Set False to force a re-render.
    """
    from meshes.exporter import export_one
    from meshes.renderer import render_one
    from provider import create_provider

    targets = collect_targets(include_all=include_all, filter_substr=filter_substr)
    if not targets:
        logger.info("No item-mesh targets to render")
        return {}
    logger.info("item-icons: %d unique meshes to process", len(targets))

    renders_root = os.path.join(config.OUTPUT_DIR, "renders")
    os.makedirs(renders_root, exist_ok=True)

    out: dict[str, str] = {}
    provider = create_provider()
    for i, t in enumerate(targets, 1):
        slug = t["mesh_slug"]
        pkg = t["mesh_pkg"]
        png = os.path.join(renders_root, f"{slug}.png")
        if skip_existing and os.path.exists(png):
            logger.info("[%d/%d] %s skip (already rendered)", i, len(targets), slug)
            out[slug] = png
            continue
        logger.info("[%d/%d] %s exporting from %s", i, len(targets), slug, pkg)
        glb = export_one(provider, slug, pkg)
        if not glb or not os.path.exists(glb):
            logger.warning("[%s] export failed", slug)
            continue
        # Reuse the single-mesh render path. The "static" archetype gives
        # us a centred 3/4 framing tuned for items. Skipping angles =>
        # the default single auto-angle is enough for inventory icons.
        pngs = render_one(slug)
        if not pngs:
            logger.warning("[%s] render produced no PNG", slug)
            continue
        out[slug] = pngs[0]
    logger.info("item-icons: %d / %d rendered", len(out), len(targets))
    return out
