"""
Render any creature / item / resonatable mesh that doesn't already have a
Blender render in `out/renders/`.

Pipeline:
  1. Read out/items.json + resonatables.json + creature_archetypes.json
  2. For each entry, derive the BP class basename from `actor_class` (items /
     resonatables) or from `id` (creatures). Strip `BP_`, `BP_Resource_`,
     `_Archetype`, etc.
  3. Match the basename to mesh discovery (`out/mesh_discovery.json`) — try
     `SKM_<base>`, `SK_<base>`, `SM_<base>`, plus a few common suffixes
     (`_01`, `_01a`, `Resource_`, `Deposit`).
  4. Compare against existing renders (`out/renders/<slug>.png`). Anything
     missing goes on the to-render list.
  5. Call `meshes.exporter.export_slugs(...)` + `meshes.renderer.render_slugs(...)`
     to do the actual Blender work.

Usage:
    python render_missing.py                # render every missing match
    python render_missing.py --dry-run      # just print the to-render list
    python render_missing.py --limit 20     # stop after N renders (for smoke)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Iterable

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import config  # noqa: E402

logger = logging.getLogger(__name__)

OUT_DIR = os.path.join(HERE, "out")
RENDERS_DIR = os.path.join(OUT_DIR, "renders")
DISCOVERY_PATH = os.path.join(OUT_DIR, "mesh_discovery.json")


# ── BP basename derivation ───────────────────────────────────────────────────

BP_PREFIXES = (
    "BP_Resource_",
    "BP_Farmable_",
    "BP_Farmable",
    "BP_",
)
ARCHETYPE_SUFFIXES = ("Archetype", "ArcheType", "Archertype")


def bp_base_from_actor_class(actor_class: str | None) -> str | None:
    """`/Game/.../BP_ScubaMask.BP_ScubaMask_C` → `ScubaMask`."""
    if not actor_class:
        return None
    last = actor_class.split(".")[0].split("/")[-1]
    for p in BP_PREFIXES:
        if last.startswith(p):
            last = last[len(p):]
            break
    return last or None


def archetype_base_from_id(arch_id: str) -> str | None:
    """`DA_VoidLeviathanChildArchetype` → `VoidLeviathanChild`."""
    s = arch_id
    if s.startswith("DA_"):
        s = s[3:]
    for suf in ARCHETYPE_SUFFIXES:
        if s.endswith(suf):
            s = s[: -len(suf)]
            if s.endswith("_"):
                s = s[:-1]
            return s
        if s.endswith("_" + suf):
            return s[: -(len(suf) + 1)]
    return s or None


# ── Discovery → catalog matching ─────────────────────────────────────────────


def load_discovery() -> dict[str, str]:
    """Flatten mesh_discovery.json → {slug: pkg_path}."""
    if not os.path.exists(DISCOVERY_PATH):
        return {}
    with open(DISCOVERY_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    flat: dict[str, str] = {}
    for _cat, slugs in data.items():
        flat.update(slugs)
    return flat


def candidates_for_base(base: str) -> list[str]:
    """Return mesh-slug candidates ordered by preference.

    Probes `SKM_*`/`SK_*` (skeletal) first, then `SM_*` (static). The static
    branch became viable after the UE5.6 Nanite parser fix in CUE4Parse
    (FStaticMeshLODResources / FNaniteResources: PR May 2026): the previously
    broken CoarseMeshStreamingIndex skip was UE5.5-only, and the
    NANITE_RESOURCE_FLAG_STREAMING_DATA_IN_DDC bit changed from 0x4 to 0x1.
    Without that fix CUE4Parse misaligned the asset stream and returned
    zero-LOD geometry for every SN2 static mesh.

    Suffix order (`_01`, `_01a`) matches Unknown Worlds' asset naming —
    creature meshes tend to live under `Creatures/<Name>_01/Mesh/SM_<Name>_01`,
    sometimes with an `a` variant for revisions.
    """
    out: list[str] = []
    for prefix in ("SKM_", "SK_", "SM_"):
        out.append(f"{prefix}{base}")
        out.append(f"{prefix}{base}_01")
        out.append(f"{prefix}{base}_01a")
        # SN2 ships separate DistanceLOD static-mesh variants for Nanite-enabled
        # creatures (e.g. SM_VepsDefender_01_DistanceLOD). When the Nanite-only
        # mesh has no normal LOD, the DistanceLOD variant is the fallback.
        out.append(f"{prefix}{base}_DistanceLOD")
        out.append(f"{prefix}{base}_01_DistanceLOD")
    return out


def match_mesh_slug(base: str, discovery: dict[str, str]) -> str | None:
    """First candidate that exists in discovery, or None."""
    if not base:
        return None
    for c in candidates_for_base(base):
        if c in discovery:
            return c
    # Case-insensitive fallback
    lower_index = {k.lower(): k for k in discovery}
    for c in candidates_for_base(base):
        hit = lower_index.get(c.lower())
        if hit:
            return hit
    # Substring fallback — find any mesh whose name contains the base. Skeletal
    # is preferred when available (better topology + skinning data); fall back
    # to static when only `SM_*` exists.
    base_l = base.lower()
    for prefixes in (("SKM_", "SK_"), ("SM_",)):
        for slug in discovery:
            if base_l in slug.lower() and any(slug.startswith(p) for p in prefixes):
                return slug
    return None


# ── Already-rendered detection ───────────────────────────────────────────────


def existing_renders() -> set[str]:
    """Set of slug names that already have a PNG in out/renders/."""
    if not os.path.isdir(RENDERS_DIR):
        return set()
    out = set()
    for fn in os.listdir(RENDERS_DIR):
        if fn.endswith(".png"):
            out.add(fn[: -len(".png")])
    return out


# ── Entry sources ────────────────────────────────────────────────────────────


def collect_targets(discovery: dict[str, str], rendered: set[str]) -> list[tuple[str, str, str]]:
    """Return list of (entity_label, mesh_slug, pkg_path) tuples to render."""
    targets: list[tuple[str, str, str]] = []
    seen_slugs: set[str] = set()

    def add(label: str, base: str | None) -> None:
        if not base:
            return
        slug = match_mesh_slug(base, discovery)
        if not slug:
            return
        # Strip rendered-suffix patterns to compare with already-rendered files.
        # `SKM_VoidLeviathan_Juvenile` is considered rendered if either the
        # exact name or any front/side/back variant exists.
        candidates = [slug, slug + "_front", slug + "_side", slug + "_back"]
        if any(c in rendered for c in candidates):
            return
        if slug in seen_slugs:
            return
        seen_slugs.add(slug)
        targets.append((label, slug, discovery[slug]))

    # Items — drive off items.json
    items_path = os.path.join(OUT_DIR, "items.json")
    if os.path.exists(items_path):
        with open(items_path, "r", encoding="utf-8") as f:
            items = json.load(f)
        for it in items:
            base = bp_base_from_actor_class(it.get("actor_class"))
            add(f"item:{it.get('name') or it.get('id')}", base)

    # Resonatables (flora / ore deposits)
    res_path = os.path.join(OUT_DIR, "resonatables.json")
    if os.path.exists(res_path):
        with open(res_path, "r", encoding="utf-8") as f:
            resonatables = json.load(f)
        for r in resonatables:
            actor = r.get("actor_class") or r.get("actorClass")
            base = bp_base_from_actor_class(actor)
            if not base:
                # Fall back to id-derived base (strip DA_/_ResonatableData)
                rid = r.get("id", "")
                base = rid.removeprefix("DA_").removesuffix("_ResonatableData")
            add(f"flora:{r.get('name') or r.get('id')}", base)

    # Creatures — drive off creature_archetypes.json
    arch_path = os.path.join(OUT_DIR, "creature_archetypes.json")
    if os.path.exists(arch_path):
        with open(arch_path, "r", encoding="utf-8") as f:
            archetypes = json.load(f)
        for a in archetypes:
            base = archetype_base_from_id(a.get("id", ""))
            add(f"creature:{a.get('id')}", base)

    return targets


# ── Driver ───────────────────────────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="Just print, don't render")
    ap.add_argument("--limit", type=int, default=0, help="Stop after N renders (0 = no limit)")
    ap.add_argument("--export-only", action="store_true", help="Export glb but skip render")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    discovery = load_discovery()
    if not discovery:
        logger.error("mesh_discovery.json missing — run `python run.py mesh-discover` first")
        sys.exit(1)
    logger.info("Discovery: %d total meshes indexed", len(discovery))

    rendered = existing_renders()
    logger.info("Already rendered: %d PNGs in %s", len(rendered), RENDERS_DIR)

    targets = collect_targets(discovery, rendered)
    logger.info("To-render: %d new meshes", len(targets))
    if args.limit > 0:
        targets = targets[: args.limit]
        logger.info("Limited to first %d", len(targets))

    # Print plan
    for label, slug, pkg in targets[:20]:
        logger.info("  %s → %s (%s)", label, slug, pkg)
    if len(targets) > 20:
        logger.info("  ... and %d more", len(targets) - 20)

    if args.dry_run:
        return
    if not targets:
        logger.info("Nothing to render — every match is already rendered.")
        return

    # Inject targets into the exporter catalog and run.
    from meshes.exporter import CATALOG, export_slugs
    for _label, slug, pkg in targets:
        CATALOG.setdefault(slug, pkg)
    slug_list = [s for _, s, _ in targets]

    logger.info("Exporting %d meshes...", len(slug_list))
    exported = export_slugs(slug_list)
    logger.info("Exported %d / %d successfully", len(exported), len(slug_list))

    if args.export_only:
        return

    from meshes.renderer import render_slugs
    successful_slugs = list(exported.keys())
    logger.info("Rendering %d meshes...", len(successful_slugs))
    render_slugs(successful_slugs)
    logger.info("Done.")


if __name__ == "__main__":
    main()
