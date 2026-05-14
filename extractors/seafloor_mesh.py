"""
Extract a seafloor heightmap by walking every StaticMesh*Actor in the playable
area, transforming each referenced mesh's collision-hull vertices into world
space, and aggregating the minimum Z into a global grid.

Why ConvexElems instead of RenderData?
  Subnautica 2 uses Nanite for almost every static mesh, so the UStaticMesh's
  cooked RenderData is not exposed by CUE4Parse (it's stored as a Nanite resource
  blob).  Each UBodySetup, however, carries an FKAggregateGeom with ConvexElems
  that contain raw FVector[] vertex data and triangle indices for physics.  These
  hulls trace the mesh's outline closely enough for a seafloor-depth map.

Output: ``out/seafloor_mesh.json`` with a ``z_cm`` 2D grid plus stats.
"""
from __future__ import annotations

import logging
import math
import os
import sys

from helpers import _export_class, prop, obj_ref_path

logger = logging.getLogger(__name__)

CELL_PREFIX = "Subnautica2/Content/Maps/Main/L_Main/_Generated_/"


def _list_cells(provider) -> list[str]:
    return sorted(p for p in provider.Files.Keys
                  if p.startswith(CELL_PREFIX) and p.endswith(".umap"))


def _read_fvector(val):
    """Return [x, y, z] from an FVector / FScriptStruct / string."""
    if val is None:
        return None
    if all(hasattr(val, c) for c in ("X", "Y", "Z")):
        try:
            return [float(val.X), float(val.Y), float(val.Z)]
        except Exception:
            pass
    # Use the regex fallback baked into helpers._read_struct_components
    from helpers import _read_struct_components
    return _read_struct_components(val, ("X", "Y", "Z"))


def _read_frotator(val):
    if val is None:
        return None
    for triple in (("Pitch", "Yaw", "Roll"),):
        if all(hasattr(val, c) for c in triple):
            try:
                return [float(getattr(val, triple[0])),
                        float(getattr(val, triple[1])),
                        float(getattr(val, triple[2]))]
            except Exception:
                pass
    from helpers import _read_struct_components
    return _read_struct_components(val, ("Pitch", "Yaw", "Roll"))


def _resolve(ref):
    if ref is None:
        return None
    for f in (lambda: ref.Load(), lambda: ref.ResolvedObject.Object.Value):
        try:
            o = f()
            if o is not None:
                return o
        except Exception:
            continue
    return None


def _mesh_hull_local_zs(provider, mesh_path: str, cache: dict) -> tuple[list[tuple[float, float, float]], list[float]] | None:
    """Load a static mesh and return (vertices_local, bbox_local).

    Cached.  Returns ``([(x,y,z), ...], [min_x, min_y, min_z, max_x, max_y, max_z])``
    or ``None`` if no collision hulls are available.
    """
    if mesh_path in cache:
        return cache[mesh_path]

    ok, pkg = False, None
    try:
        ok, pkg = provider.TryLoadPackage(mesh_path)
    except Exception:
        pass
    if not ok or pkg is None:
        cache[mesh_path] = None
        return None

    # Find the UStaticMesh export
    sm = None
    for e in pkg.GetExports():
        if _export_class(e) == "StaticMesh":
            sm = e
            break
    if sm is None:
        cache[mesh_path] = None
        return None

    bs_ref = getattr(sm, "BodySetup", None)
    bs = _resolve(bs_ref)
    if bs is None:
        cache[mesh_path] = None
        return None

    ag = getattr(bs, "AggGeom", None)
    if ag is None:
        cache[mesh_path] = None
        return None

    verts: list[tuple[float, float, float]] = []

    # Convex hulls (most common in SN2: rocks, coral, etc.)
    convex = getattr(ag, "ConvexElems", None) or []
    for elem in convex:
        vd = getattr(elem, "VertexData", None)
        if vd is None:
            continue
        try:
            for v in vd:
                verts.append((float(v.X), float(v.Y), float(v.Z)))
        except Exception:
            continue

    # Box collision (small meshes with simple cuboids)
    box_elems = getattr(ag, "BoxElems", None) or []
    for elem in box_elems:
        center = _read_fvector(getattr(elem, "Center", None)) or [0, 0, 0]
        x, y, z = (getattr(elem, "X", 0), getattr(elem, "Y", 0), getattr(elem, "Z", 0))
        # 8 corner verts of the local box
        hx, hy, hz = x / 2.0, y / 2.0, z / 2.0
        cx, cy, cz = center
        for dx in (-hx, hx):
            for dy in (-hy, hy):
                for dz in (-hz, hz):
                    verts.append((cx + dx, cy + dy, cz + dz))

    # Sphere collision (rare for terrain decoration; just sample 6 axis-aligned points)
    for elem in getattr(ag, "SphereElems", None) or []:
        center = _read_fvector(getattr(elem, "Center", None)) or [0, 0, 0]
        r = float(getattr(elem, "Radius", 0) or 0)
        cx, cy, cz = center
        for dx, dy, dz in ((r,0,0),(-r,0,0),(0,r,0),(0,-r,0),(0,0,r),(0,0,-r)):
            verts.append((cx + dx, cy + dy, cz + dz))

    if not verts:
        cache[mesh_path] = None
        return None

    xs = [v[0] for v in verts]; ys = [v[1] for v in verts]; zs = [v[2] for v in verts]
    bbox = [min(xs), min(ys), min(zs), max(xs), max(ys), max(zs)]
    out = (verts, bbox)
    cache[mesh_path] = out
    return out


def _mesh_ref_path(comp) -> str | None:
    """Read StaticMesh ref from an SMC and return its package path (without .uasset)."""
    ref = prop(comp, "StaticMesh") or prop(comp, "Mesh")
    if ref is None:
        return None
    p = obj_ref_path(ref)
    if p is None:
        return None
    # Drop the trailing `.AssetName` if present
    s = p.replace("\\", "/")
    if s.startswith("/Game/"):
        s = "Subnautica2/Content" + s[len("/Game"):]
    if "." in s:
        s = s.rsplit(".", 1)[0]
    return s


def _euler_zyx_matrix(pitch_deg: float, yaw_deg: float, roll_deg: float):
    """UE FRotator (Pitch=Y, Yaw=Z, Roll=X) -> 3x3 rotation matrix.

    UE uses left-handed XYZ but rotation order is Roll(X), Pitch(Y), Yaw(Z)
    composed as ``Yaw * Pitch * Roll``.  Returns row-major matrix list-of-list.
    """
    p = math.radians(pitch_deg)
    y = math.radians(yaw_deg)
    r = math.radians(roll_deg)
    cp, sp = math.cos(p), math.sin(p)
    cy, sy = math.cos(y), math.sin(y)
    cr, sr = math.cos(r), math.sin(r)
    # UE rotation:
    # rotate around X (Roll), then Y (Pitch), then Z (Yaw)
    # M = Rz(yaw) * Ry(pitch) * Rx(roll)
    m = [
        [cy*cp,                 cy*sp*sr - sy*cr,      cy*sp*cr + sy*sr],
        [sy*cp,                 sy*sp*sr + cy*cr,      sy*sp*cr - cy*sr],
        [-sp,                   cp*sr,                 cp*cr],
    ]
    return m


def run(provider, max_cells: int | None = None, pixel_cm: int = 500,
        world_bounds: dict | None = None) -> dict:
    """Sweep cells, transform mesh hulls, aggregate min-Z per grid cell."""
    if world_bounds is None:
        # Use the playable AABB from world_boundaries.json
        wb_path = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "out", "world_boundaries.json"))
        import json
        with open(wb_path, encoding="utf-8") as f:
            world_bounds = json.load(f)["bounds"]

    x_min = world_bounds["x_min"]; x_max = world_bounds["x_max"]
    y_min = world_bounds["y_min"]; y_max = world_bounds["y_max"]
    W = int(math.ceil((x_max - x_min) / pixel_cm))
    H = int(math.ceil((y_max - y_min) / pixel_cm))
    PAD = 10_000  # ignore actors farther than 100 m outside playable rect

    cells = _list_cells(provider)
    if max_cells:
        cells = cells[:max_cells]
    logger.info("Seafloor mesh: scanning %d cells, grid %dx%d @ %d cm/pixel",
                len(cells), W, H, pixel_cm)

    z_min_grid = [None] * (W * H)
    mesh_cache: dict = {}
    instances_kept = 0
    instances_skipped = 0
    failed_cells = 0
    failed_meshes = 0

    for ci, cell_path in enumerate(cells, 1):
        try:
            ok, pkg = provider.TryLoadPackage(cell_path[:-5])
        except Exception:
            failed_cells += 1
            continue
        if not ok or pkg is None:
            failed_cells += 1
            continue

        for export in pkg.GetExports():
            cls = _export_class(export)
            if not (cls == "StaticMeshComponent" or cls == "StaticMeshActor"
                    or "InstancedStaticMeshComponent" in cls or "ISM" in cls):
                continue

            # Find the component that owns position + mesh.
            if cls == "StaticMeshActor":
                comp_ref = prop(export, "StaticMeshComponent") or prop(export, "RootComponent")
                comp = _resolve(comp_ref)
                if comp is None:
                    instances_skipped += 1
                    continue
            else:
                comp = export

            loc = _read_fvector(prop(comp, "RelativeLocation"))
            if loc is None:
                instances_skipped += 1
                continue
            if not (x_min - PAD <= loc[0] <= x_max + PAD and y_min - PAD <= loc[1] <= y_max + PAD):
                instances_skipped += 1
                continue

            mesh_path = _mesh_ref_path(comp)
            if mesh_path is None:
                # SMCs that hold no mesh themselves can still contribute their location
                # as a single Z point — but skip them for now to keep heights honest.
                instances_skipped += 1
                continue

            mesh = _mesh_hull_local_zs(provider, mesh_path, mesh_cache)
            if mesh is None:
                failed_meshes += 1
                continue
            verts_local, bbox = mesh

            rot = _read_frotator(prop(comp, "RelativeRotation")) or [0.0, 0.0, 0.0]
            sc = _read_fvector(prop(comp, "RelativeScale3D")) or [1.0, 1.0, 1.0]
            M = _euler_zyx_matrix(*rot)
            sx, sy, sz = sc
            lx, ly, lz = loc

            # Transform every vertex; accumulate min-Z per grid cell.
            for vx, vy, vz in verts_local:
                # Scale -> rotate -> translate.
                px = vx * sx; py = vy * sy; pz = vz * sz
                wx = M[0][0]*px + M[0][1]*py + M[0][2]*pz + lx
                wy = M[1][0]*px + M[1][1]*py + M[1][2]*pz + ly
                wz = M[2][0]*px + M[2][1]*py + M[2][2]*pz + lz
                col = int((wx - x_min) / pixel_cm)
                row = int((wy - y_min) / pixel_cm)
                if 0 <= col < W and 0 <= row < H:
                    pi = row * W + col
                    cur = z_min_grid[pi]
                    if cur is None or wz < cur:
                        z_min_grid[pi] = wz
            instances_kept += 1

        del pkg

        if ci % 200 == 0:
            filled = sum(1 for v in z_min_grid if v is not None)
            logger.info("  cells %d/%d, instances kept %d, skipped %d, filled cells %d/%d, mesh cache %d",
                        ci, len(cells), instances_kept, instances_skipped, filled, W * H, len(mesh_cache))

    filled = sum(1 for v in z_min_grid if v is not None)
    logger.info("Seafloor mesh done: %d instances kept, %d skipped, %d cells failed, %d meshes failed, %d unique meshes, %d/%d cells filled",
                instances_kept, instances_skipped, failed_cells, failed_meshes,
                len(mesh_cache), filled, W * H)

    # Convert None -> NaN sentinel for JSON; keep them so consumers know which
    # cells came from real data vs were never sampled.
    z_rows = []
    for r in range(H):
        row = []
        for c in range(W):
            v = z_min_grid[r * W + c]
            row.append(v if v is not None else None)
        z_rows.append(row)

    vals = [v for v in z_min_grid if v is not None]
    return {
        "pixel_cm": pixel_cm,
        "bounds": {
            "x_min": x_min, "x_max": x_max, "y_min": y_min, "y_max": y_max,
            "z_min": min(vals) if vals else None,
            "z_max": max(vals) if vals else None,
            "width_cm":  x_max - x_min,
            "height_cm": y_max - y_min,
        },
        "shape": [H, W],
        "z_cm": z_rows,
        "stats": {
            "instances_kept": instances_kept,
            "instances_skipped": instances_skipped,
            "failed_cells": failed_cells,
            "failed_meshes": failed_meshes,
            "unique_meshes": len(mesh_cache),
            "filled_cells": filled,
            "total_cells": W * H,
        },
    }
