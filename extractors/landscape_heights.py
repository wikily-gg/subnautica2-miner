"""
Extract per-pixel terrain heights from UE5 LandscapeComponent HeightmapTextures.

Each landscape proxy owns 16 components (4 x 4 grid), each component is 62 quads
on a side and references a 64 x 64 BGRA8 HeightmapTexture (one heightmap shared
or one-per-component depending on the project).  The height of each vertex is:

    raw    = (R << 8) | G                          # uint16, 0..65535
    local  = (raw - 32768) / 128                   # local-space cm
    worldZ = ProxyRelativeLocation.Z + local * ProxyRelativeScale3D.Z

World XY for sample (i, j) of a component is:

    worldX = ProxyLoc.X + (SectionBaseX + i) * ProxyScale.X
    worldY = ProxyLoc.Y + (SectionBaseY + j) * ProxyScale.Y

We stitch every component into a single global grid covering the playable wall
polygon's AABB, at the landscape's native resolution (1 sample per quad).
"""
from __future__ import annotations

import logging
import os
import struct
import sys

from helpers import (
    _export_class,
    prop,
    prop_int,
    vec_to_list,
    _read_struct_components,
)

logger = logging.getLogger(__name__)

CELL_PREFIX = "Subnautica2/Content/Maps/Main/L_Main/_Generated_/"

# UE5 landscape constants.  See Engine/Source/Runtime/Landscape/Private/Landscape.cpp
LANDSCAPE_INV_ZSCALE = 128.0  # raw height units per world Z unit


def _list_cells(provider) -> list[str]:
    return sorted(p for p in provider.Files.Keys
                  if p.startswith(CELL_PREFIX) and p.endswith(".umap"))


def _resolve_actor_transform(actor):
    """Return (loc_xyz, scale_xyz) by following RootComponent."""
    rc = prop(actor, "RootComponent")
    if rc is None:
        return None, None
    try:
        obj = rc.Load()
    except Exception:
        obj = None
    if obj is None:
        try:
            obj = rc.ResolvedObject.Object.Value
        except Exception:
            return None, None
    loc = vec_to_list(prop(obj, "RelativeLocation")) or [0.0, 0.0, 0.0]
    sc = vec_to_list(prop(obj, "RelativeScale3D")) or [1.0, 1.0, 1.0]
    return loc, sc


def _resolve_texture(ref):
    if ref is None:
        return None
    for try_ in (
        lambda: ref.Load(),
        lambda: ref.ResolvedObject.Object.Value,
    ):
        try:
            tex = try_()
            if tex is not None:
                return tex
        except Exception:
            continue
    return None


def _read_heightmap_handle(texture):
    """Return (.NET Byte[] data, width, height) for the texture's mip 0.

    Reading raw bytes one at a time via pythonnet is the only portable path
    (slicing Byte[] directly raises in pythonnet).  We iterate the array later;
    decoding only touches the R and G channels of each vertex sample.
    """
    try:
        mip = texture.GetFirstMip()
    except Exception:
        return None, 0, 0
    if mip is None:
        return None, 0, 0
    try:
        w = int(mip.SizeX); h = int(mip.SizeY)
        data = mip.BulkData.Data
        if data is None:
            return None, w, h
        return data, w, h
    except Exception as exc:
        logger.debug("mip data read failed: %s", exc)
        return None, 0, 0


def run(provider, max_cells: int | None = None, pixel_cm: int = 1000) -> dict:
    """Extract heights and rasterise to a global grid at ``pixel_cm`` resolution.

    pixel_cm matches Subnautica 2's per-quad world scale (RelativeScale3D.X = 1000).
    Resulting grid covers the wall-polygon AABB.
    """
    cells = _list_cells(provider)
    if max_cells:
        cells = cells[:max_cells]
    logger.info("Landscape heights: scanning %d cells for landscape data", len(cells))

    # Phase 1: walk every cell, gather every ULandscapeComponent + proxy transform.
    components = []  # list of dicts: world AABB and per-sample heights flattened
    proxies = {}     # proxy_name -> (loc, scale)
    failed = 0

    for ci, cell_path in enumerate(cells, 1):
        ok, package = None, None
        try:
            ok, package = provider.TryLoadPackage(cell_path[:-5])
        except Exception:
            failed += 1
            continue
        if not ok or package is None:
            failed += 1
            continue

        proxy_loc = None; proxy_scale = None
        cell_proxies = []
        for export in package.GetExports():
            cls = _export_class(export)
            if cls == "Landscape" or "LandscapeStreamingProxy" in cls or "LandscapeProxy" in cls:
                loc, sc = _resolve_actor_transform(export)
                if loc is None:
                    continue
                key = str(export.Name)
                proxies[key] = {"loc": loc, "scale": sc}
                cell_proxies.append((key, loc, sc))
                proxy_loc, proxy_scale = loc, sc

        for export in package.GetExports():
            cls = _export_class(export)
            if cls != "LandscapeComponent" and not cls.endswith("LandscapeComponent"):
                continue
            sbx = prop_int(export, "SectionBaseX")
            sby = prop_int(export, "SectionBaseY")
            quads = prop_int(export, "ComponentSizeQuads") or 62

            scale_bias = prop(export, "HeightmapScaleBias")
            sb_vals = _read_struct_components(scale_bias, ("X", "Y", "Z", "W"))
            if sb_vals is None:
                sb_vals = [1.0 / 64, 1.0 / 64, 0.0, 0.0]

            hm_ref = prop(export, "HeightmapTexture")
            texture = _resolve_texture(hm_ref)
            if texture is None:
                continue
            data, tex_w, tex_h = _read_heightmap_handle(texture)
            if data is None or tex_w == 0:
                continue

            if proxy_loc is None:
                # find via AttachParent's owner; fall back to first proxy seen
                proxy_loc = next(iter(proxies.values()))["loc"]
                proxy_scale = next(iter(proxies.values()))["scale"]

            inv_x, inv_y, bias_u, bias_v = sb_vals
            # Subsample step: each LandscapeComponent samples (quads + 1) vertices on a
            # side, but with NumSubsections=2 the texture has a duplicated row between
            # subsections, so the vertex grid is exactly tex_w x tex_h pixels at
            # (bias_u, bias_v) origin within the texture.
            n = quads + 1

            heights = bytearray(n * n * 2)  # store raw 16-bit, packed little-endian
            for j in range(n):
                tv = int(bias_v * tex_h + j)
                if tv >= tex_h: tv = tex_h - 1
                row_off = tv * tex_w * 4
                for i in range(n):
                    tu = int(bias_u * tex_w + i)
                    if tu >= tex_w: tu = tex_w - 1
                    idx = row_off + tu * 4
                    # BGRA: data[idx]=B, [idx+1]=G, [idx+2]=R, [idx+3]=A
                    g = data[idx + 1] & 0xff
                    r = data[idx + 2] & 0xff
                    raw = (r << 8) | g  # uint16
                    pi = (j * n + i) * 2
                    heights[pi]     = raw & 0xff
                    heights[pi + 1] = (raw >> 8) & 0xff

            components.append({
                "cell": cell_path.rsplit("/", 1)[-1].replace(".umap", ""),
                "proxy_loc": proxy_loc,
                "proxy_scale": proxy_scale,
                "section_base": [sbx, sby],
                "n": n,                          # samples per side (vertex grid)
                "scale_x": proxy_scale[0],       # cm per quad in world space
                "scale_y": proxy_scale[1],
                "scale_z": proxy_scale[2],
                "heights_hex": bytes(heights).hex(),
            })

        del package

        if ci % 200 == 0:
            logger.info("  cells %d/%d, components: %d (proxies: %d)",
                        ci, len(cells), len(components), len(proxies))

    logger.info("Landscape: %d components from %d proxies (%d cells failed)",
                len(components), len(proxies), failed)

    return {
        "summary": {
            "components": len(components),
            "proxies": len(proxies),
            "cells_scanned": len(cells),
        },
        "components": components,
    }
