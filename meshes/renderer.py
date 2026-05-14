"""
Render exported SN2 meshes to PNG via headless Blender.

Looks up the .glb produced by `meshes.exporter` for each slug, then runs
Blender with `_blender_render.py` to produce a 1024x1024 transparent PNG
at `out/renders/<slug>.png`.

CLI:
    python run.py mesh-render <slug>
    python run.py mesh-render --all
    python run.py mesh-render --filter vehicle
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Iterable

import config
from meshes.exporter import (
    ARCHETYPES,
    _resolve_catalog,
    export_slugs,
)

logger = logging.getLogger(__name__)

_MESHES_ROOT = os.path.join(config.OUTPUT_DIR, "meshes")
_RENDERS_ROOT = os.path.join(config.OUTPUT_DIR, "renders")

# Blender install - same path as FFW renderer
BLENDER_EXE = r"C:\Program Files (x86)\Steam\steamapps\common\Blender\blender.exe"

_SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "_blender_render.py")


def _find_glb_for_slug(slug: str) -> str | None:
    base = os.path.join(_MESHES_ROOT, slug)
    if not os.path.isdir(base):
        return None
    for root, _dirs, files in os.walk(base):
        for fn in files:
            if fn.lower().endswith(".glb"):
                return os.path.join(root, fn)
    return None


def render_one(slug: str, force_export: bool = False, angles: list[str] | None = None) -> list[str]:
    """Render a single slug at one or more camera angles.

    *angles* — list of strings, each "auto" (default bbox-derived 3/4 view)
    or "front" / "side" / "back" / "<N>deg" (explicit azimuth). When the
    list contains multiple entries, output filenames are suffixed
    `_<angle>` (e.g. `SKM_Tadpole_front.png`). With a single "auto"
    entry the filename stays `<slug>.png` for backwards compatibility.

    Returns a list of output PNG paths (empty on full failure).
    """
    angles = angles or ["auto"]
    catalog = _resolve_catalog()
    glb = _find_glb_for_slug(slug)
    if glb is None or force_export:
        if slug not in catalog:
            logger.warning("[%s] not in CATALOG / discovery", slug)
            return []
        logger.info("[%s] glb missing - exporting first", slug)
        results = export_slugs([slug])
        if slug not in results:
            logger.warning("[%s] export failed; cannot render", slug)
            return []
        glb = results[slug]

    if not os.path.exists(glb):
        logger.warning("[%s] glb path doesn't exist: %s", slug, glb)
        return []

    if not os.path.exists(BLENDER_EXE):
        logger.error("Blender not found at %s", BLENDER_EXE)
        return []

    os.makedirs(_RENDERS_ROOT, exist_ok=True)
    archetype = ARCHETYPES.get(slug, "static")

    outputs: list[str] = []
    single_angle = len(angles) == 1 and angles[0] == "auto"
    for angle in angles:
        if single_angle:
            out_png = os.path.join(_RENDERS_ROOT, f"{slug}.png")
        else:
            out_png = os.path.join(_RENDERS_ROOT, f"{slug}_{angle}.png")

        cmd = [
            BLENDER_EXE,
            "--background",
            "--factory-startup",
            "--python", _SCRIPT_PATH,
            "--", glb, out_png, slug, archetype, angle,
        ]
        logger.info("[%s/%s] running Blender (%s)...", slug, angle, archetype)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            logger.error("[%s/%s] Blender timed out after 10 min", slug, angle)
            continue

        if proc.returncode != 0:
            logger.warning("[%s/%s] Blender exited with %d", slug, angle, proc.returncode)
            for line in (proc.stderr or "").splitlines()[-10:]:
                logger.warning("  stderr> %s", line)
            for line in (proc.stdout or "").splitlines()[-10:]:
                logger.warning("  stdout> %s", line)
            continue

        if not os.path.exists(out_png):
            logger.warning("[%s/%s] png missing", slug, angle)
            continue

        size_kb = os.path.getsize(out_png) // 1024
        logger.info("[%s/%s] -> %s (%d KB)", slug, angle, out_png, size_kb)
        outputs.append(out_png)
    return outputs


def render_slugs(slugs: Iterable[str], angles: list[str] | None = None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    slug_list = list(slugs)
    for slug in slug_list:
        pngs = render_one(slug, angles=angles)
        if pngs:
            out[slug] = pngs
    logger.info("Render complete: %d / %d slugs", len(out), len(slug_list))
    return out


def render_all(filter_substr: str | None = None, angles: list[str] | None = None) -> dict[str, list[str]]:
    catalog = _resolve_catalog()
    slugs = list(catalog.keys())
    if filter_substr:
        f = filter_substr.lower()
        slugs = [s for s in slugs if f in s.lower()]
    return render_slugs(slugs, angles=angles)
