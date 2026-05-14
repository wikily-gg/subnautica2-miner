"""
Composite vehicle rendering for SN2.

For multi-part assemblies (Tadpole, Tadpole HAUL, Tadpole ScoutRay,
Lifepod) we export each part to its own GLB via the standard
`meshes.exporter`, then run a special Blender script that imports ALL
the parts into one scene and renders one composed PNG.

The assembly model is BP-driven: each vehicle slug maps to a list of
chassis Blueprints. We walk each BP's Simple Construction Script to
get the full list of mesh components with their per-component
RelativeLocation / RelativeRotation / RelativeScale3D. Chassis
variants inherit the base Tadpole BP first, then the chassis BP on
top, so e.g. the HAUL gets the base Tadpole hull + cockpit + the
HAUL-specific propeller housing and storage bays.

Output:
    out/renders/<assembly_slug>.png       composite render
    out/renders/<assembly_slug>.json      per-component transform list
                                          (sidecar used by the Blender
                                          render script)

CLI:
    python run.py mesh-composite                          # render every
                                                          # assembly in
                                                          # VEHICLE_BP_ASSEMBLIES
    python run.py mesh-composite vehicle_tadpole          # render one
    python run.py mesh-composite --filter tadpole         # filter by substr
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Iterable

import config
from meshes.exporter import (
    VEHICLE_BP_ASSEMBLIES,
    _resolve_catalog,
    export_one,
)
from meshes.renderer import BLENDER_EXE, _find_glb_for_slug
from meshes.bp_transforms import (
    BPComponent,
    read_merged_bp_components,
)

logger = logging.getLogger(__name__)

_MESHES_ROOT = os.path.join(config.OUTPUT_DIR, "meshes")
_RENDERS_ROOT = os.path.join(config.OUTPUT_DIR, "renders")
_SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "_blender_composite.py")


def _components_for_assembly(provider, assembly_slug: str) -> list[BPComponent]:
    """Look up the BP list for an assembly and return its merged component
    list. Returns [] when the assembly isn't registered.
    """
    bp_paths = VEHICLE_BP_ASSEMBLIES.get(assembly_slug)
    if not bp_paths:
        logger.error("composite: unknown assembly %r", assembly_slug)
        return []
    return read_merged_bp_components(provider, bp_paths)


def _ensure_mesh_exported(
    provider, mesh_slug: str, catalog: dict[str, str],
) -> str | None:
    """Export a single mesh to GLB if not already on disk. Returns the
    GLB path on success, None on failure.
    """
    existing = _find_glb_for_slug(mesh_slug)
    if existing and os.path.exists(existing):
        return existing
    pkg = catalog.get(mesh_slug)
    if not pkg:
        # Catalog doesn't know about this mesh - skip silently. Some BP
        # references point at meshes outside our discovered set (e.g.
        # Powercell, hardpoint markers).
        return None
    glb = export_one(provider, mesh_slug, pkg)
    return glb if (glb and os.path.exists(glb)) else None


def render_assembly(assembly_slug: str, force_export: bool = False) -> str | None:
    """Render one assembly to `out/renders/<assembly_slug>.png`.

    Returns the output path on success, None on failure.
    """
    if assembly_slug not in VEHICLE_BP_ASSEMBLIES:
        logger.error(
            "render_assembly: unknown assembly %r. Known: %s",
            assembly_slug, sorted(VEHICLE_BP_ASSEMBLIES.keys()),
        )
        return None

    from provider import create_provider
    provider = create_provider()

    components = _components_for_assembly(provider, assembly_slug)
    if not components:
        logger.error("[%s] composite: no components from BPs, aborting",
                     assembly_slug)
        return None
    logger.info("[%s] composite: %d components from %d BP(s)",
                assembly_slug, len(components),
                len(VEHICLE_BP_ASSEMBLIES[assembly_slug]))

    # Export every UNIQUE mesh referenced by the components. The same
    # mesh slug can appear multiple times in the component list (left /
    # right propellers) but we only need to export the GLB once.
    catalog = _resolve_catalog()
    mesh_to_glb: dict[str, str] = {}
    missing: list[str] = []
    unique_slugs = {c.mesh_slug for c in components}
    if force_export:
        # Re-export everything from paks even if cached on disk.
        for slug in sorted(unique_slugs):
            pkg = catalog.get(slug)
            if pkg:
                export_one(provider, slug, pkg)

    for slug in sorted(unique_slugs):
        glb = _ensure_mesh_exported(provider, slug, catalog)
        if glb:
            mesh_to_glb[slug] = glb
        else:
            missing.append(slug)

    if missing:
        logger.warning("[%s] composite: %d mesh(es) had no GLB: %s",
                       assembly_slug, len(missing),
                       ", ".join(missing[:5])
                       + ("..." if len(missing) > 5 else ""))

    # Build the manifest the Blender script consumes. Each entry is one
    # mesh PLACEMENT - same mesh can appear multiple times at different
    # transforms (mirrored upgrade slots, dual propellers, etc.).
    placements: list[dict] = []
    for c in components:
        glb = mesh_to_glb.get(c.mesh_slug)
        if not glb:
            continue
        placements.append({
            "component_name": c.component_name,
            "mesh_slug": c.mesh_slug,
            "glb_path": glb,
            "location": list(c.location) if c.location else None,
            "rotation": list(c.rotation) if c.rotation else None,
            "scale": list(c.scale) if c.scale else None,
        })

    if not placements:
        logger.error("[%s] composite: no usable placements", assembly_slug)
        return None

    if not os.path.exists(BLENDER_EXE):
        logger.error("Blender not found at %s", BLENDER_EXE)
        return None

    os.makedirs(_RENDERS_ROOT, exist_ok=True)
    out_png = os.path.join(_RENDERS_ROOT, f"{assembly_slug}.png")
    manifest_path = os.path.join(_RENDERS_ROOT, f"{assembly_slug}.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({
            "assembly_slug": assembly_slug,
            "placements": placements,
        }, f, indent=2)

    cmd = [
        BLENDER_EXE,
        "--background",
        "--factory-startup",
        "--python", _SCRIPT_PATH,
        "--", out_png, assembly_slug, manifest_path,
    ]
    logger.info("[%s] running Blender with %d placements...",
                assembly_slug, len(placements))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        logger.error("[%s] Blender timed out after 15 min", assembly_slug)
        return None

    if proc.returncode != 0:
        logger.warning("[%s] Blender exited with %d", assembly_slug, proc.returncode)
        for line in (proc.stderr or "").splitlines()[-15:]:
            logger.warning("  stderr> %s", line)
        for line in (proc.stdout or "").splitlines()[-15:]:
            logger.warning("  stdout> %s", line)
        return None

    if not os.path.exists(out_png):
        logger.warning("[%s] png missing after render", assembly_slug)
        return None

    size_kb = os.path.getsize(out_png) // 1024
    logger.info("[%s] -> %s (%d KB)", assembly_slug, out_png, size_kb)
    return out_png


def render_assemblies(
    assemblies: Iterable[str] | None = None,
    *,
    filter_substr: str | None = None,
    force_export: bool = False,
) -> dict[str, str]:
    """Render multiple assemblies. Returns slug → output PNG path map."""
    if assemblies is None:
        assemblies = list(VEHICLE_BP_ASSEMBLIES.keys())
    else:
        assemblies = [a for a in assemblies if a in VEHICLE_BP_ASSEMBLIES]

    if filter_substr:
        f = filter_substr.lower()
        assemblies = [a for a in assemblies if f in a.lower()]

    out: dict[str, str] = {}
    for slug in assemblies:
        png = render_assembly(slug, force_export=force_export)
        if png:
            out[slug] = png
    logger.info("Composite render complete: %d / %d assemblies",
                len(out), len(list(assemblies)))
    return out
