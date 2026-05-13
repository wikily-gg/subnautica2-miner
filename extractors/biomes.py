"""
Extract per-mesh biome classification from world-partition cells.

For each StaticMeshActor in every L_Main cell, follow RootComponent →
StaticMesh ref to determine which biome subdirectory the mesh lives in
(``/Game/Art/Environment/Biome/<BiomeName>/...``).  Emits a list of
``{biome, x, y, z, mesh}`` tuples — the raw input for biome polygon /
raster generation.
"""
from __future__ import annotations

import logging
import re

from helpers import _export_class, prop, vec_to_list

logger = logging.getLogger(__name__)

CELL_PREFIX = "Subnautica2/Content/Maps/Main/L_Main/_Generated_/"

BIOME_PATH_RE = re.compile(r"/Biome/([A-Za-z0-9_]+)/")

# Some classes carry their biome in their own name prefix (when no static mesh
# ref is available), e.g. ``BP_CG_*`` lives in CoralGardens.
CLASS_BIOME_PREFIX = {
    "BP_CG_": "CoralGarden",
    "BP_OR_": "OvergrownRuins",
    "BP_JP_": "JellyPlateaus",
    "BP_SP_": "SparsePlains",
    "BP_KF_": "KelpForest",
    "BP_VZ_": "VepZone",
    "BP_WT_": "WorldTree",
    "BP_BO_VZ_": "VepZone",
    "BP_BO_KF_": "KelpForest",
    "BP_BO_OR_": "OvergrownRuins",
    "BP_BO_CG_": "CoralGarden",
    "BP_BO_JP_": "JellyPlateaus",
    "BP_BO_SP_": "SparsePlains",
    "BP_BO_Void_": "Void",
}


def _biome_from_mesh_path(mesh_ref) -> str | None:
    if mesh_ref is None:
        return None
    s = str(mesh_ref)
    m = BIOME_PATH_RE.search(s)
    if m:
        return m.group(1)
    return None


def _biome_from_class(cls: str) -> str | None:
    for prefix, biome in CLASS_BIOME_PREFIX.items():
        if cls.startswith(prefix):
            return biome
    return None


def _resolve_actor_xyz(actor) -> tuple[list[float] | None, str | None]:
    """Return ``(world_xyz, mesh_path)`` for *actor*.

    Resolves RootComponent → SceneComponent → RelativeLocation, and reads
    the StaticMesh ref on that component (if it is a StaticMeshComponent).
    """
    rc = prop(actor, "RootComponent")
    if rc is None:
        return None, None
    try:
        comp = rc.Load()
    except Exception:
        return None, None
    if comp is None:
        return None, None
    loc = vec_to_list(prop(comp, "RelativeLocation"))
    mesh_ref = prop(comp, "StaticMesh")
    mesh_path = None
    if mesh_ref is not None:
        # FPackageIndex stringifies as "Class'/Game/.../X.X'"
        s = str(mesh_ref)
        if "'" in s:
            mesh_path = s.split("'", 1)[1].rstrip("'")
        else:
            mesh_path = s
    return loc, mesh_path


def _list_cells(provider) -> list[str]:
    return sorted(p for p in provider.Files.Keys
                  if p.startswith(CELL_PREFIX) and p.endswith(".umap"))


def run(provider, max_cells: int | None = None) -> dict:
    """Scan every L_Main cell and emit biome-classified placement points.

    Returns a dict with summary stats and a list of points
    ``{biome, x, y, z, cls, mesh}``.
    """
    cells = _list_cells(provider)
    if max_cells:
        cells = cells[:max_cells]
    logger.info("Biome sweep: %d cells", len(cells))

    points: list[dict] = []
    by_biome: dict[str, int] = {}
    unmatched = 0

    for i, cell_path in enumerate(cells, 1):
        try:
            ok, package = provider.TryLoadPackage(cell_path[:-5])
        except Exception:
            continue
        if not ok or package is None:
            continue
        cell_id = cell_path.rsplit("/", 1)[-1].replace(".umap", "")
        for export in package.GetExports():
            cls = _export_class(export)
            # Only actor classes — skip components / level / world settings
            if cls in ("Level", "Model", "NavigationSystemModuleConfig",
                       "World", "WorldSettings", "Brush"):
                continue
            # Get position
            loc, mesh_path = _resolve_actor_xyz(export)
            if loc is None:
                continue
            biome = _biome_from_mesh_path(mesh_path) or _biome_from_class(cls)
            if biome is None:
                unmatched += 1
                continue
            points.append({
                "biome": biome,
                "x": loc[0],
                "y": loc[1],
                "z": loc[2],
                "cls": cls,
                "mesh": mesh_path,
                "cell": cell_id,
            })
            by_biome[biome] = by_biome.get(biome, 0) + 1
        if i % 200 == 0:
            logger.info("  cells %d/%d, points: %d", i, len(cells), len(points))
        del package

    logger.info("Biome sweep done — %d points, %d unmatched, %d biomes",
                len(points), unmatched, len(by_biome))

    return {
        "summary": {
            "total_points": len(points),
            "cells_scanned": len(cells),
            "unmatched_count": unmatched,
            "by_biome": dict(sorted(by_biome.items(), key=lambda x: -x[1])),
        },
        "points": points,
    }
