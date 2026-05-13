"""
Generate biome / sub-region map outputs from biome_points_v2.json and
world_boundaries.json.

Outputs:
  out/biome_raster.png         — pixel grid, colored by majority biome
  out/biome_overlay.png        — biome polygons with labels
  out/biome_polygons.geojson   — GeoJSON FeatureCollection (biome polygons)
  out/biome_polygons.json      — flat ``{biome: [[[x,y]...]]}``
  out/subregion_raster.png     — same as above, but coloured by sub-region
  out/subregion_overlay.png    — sub-region polygons with labels
  out/subregion_polygons.geojson
  out/subregion_polygons.json

Map bounds come from ``world_boundaries.json`` (computed from the 58
BP_EdgeOfWorldVolume_C wall slabs in L_Main top-level).
"""
from __future__ import annotations

import json
import logging
import os
import sys
from collections import Counter

import matplotlib.pyplot as plt
import numpy as np
from matplotlib import patheffects

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# Biome → hex color
BIOME_COLORS = {
    "CoralGardens":      "#ff6b9a",
    "OvergrownRuins":    "#7ad06a",
    "JellyPlateaus":     "#a48cff",
    "SparsePlains":      "#e7c068",
    "KelpForest":        "#3aa66b",
    "WorldTree":         "#9e6536",
    "Void":              "#222b3a",
    "VepZone":           "#ffb05a",
    "DeepStart":         "#3a8bff",
    "Generic":           "#8a96a6",
    "CollectorLeviathanRegion": "#ff5a3b",
}

# Sub-region → hex color. Anything not listed uses BIOME_COLORS for its
# parent biome with a slight variant.
SUBREGION_COLORS = {
    # CoralGardens
    "CoralGardens.Shallows":      "#ffb3c8",  # pale pink (shallow)
    "CoralGardens.Plateaus":      "#ff6b9a",
    "CoralGardens.Graveyard":     "#6a4a52",  # dead-coral brown
    "CoralGardens.AnemoneHills":  "#ffd24a",  # anemone yellow
    "CoralGardens.TufaTowers":    "#bd8888",  # rust
    "CoralGardens.NorthRaceway":  "#ea4c8f",
    "CoralGardens.SouthRaceway":  "#c64278",
    "CoralGardens.BlightedCoral": "#7a3a5a",  # blight purple
    "CoralGardens.Leadzone":      "#4a4a5a",  # dark lead-gray
    # OvergrownRuins
    "OvergrownRuins.Observatory": "#7ad06a",
    "OvergrownRuins.PowerPlant":  "#4ec0a4",  # teal
    "OvergrownRuins.RootCanyon":  "#5a8c46",  # forest green
    # standalone
    "DeepStart":                  "#3a8bff",
    "CollectorLeviathanRegion":   "#ff5a3b",
    "SparsePlains.Clamily":       "#e7c068",
}
DEFAULT_COLOR = "#444a55"

# Pixel size in world cm.  1000 cm = 10 m per pixel → good detail at the
# size of this map (≈2.8km × 1.1km).
PIXEL_CM = 1000


# ---------------- IO ----------------

def load_points() -> list[dict]:
    with open(os.path.join(OUT_DIR, "biome_points_v2.json"), encoding="utf-8") as f:
        data = json.load(f)
    return data["points"]


def load_bounds() -> dict:
    """Return ``{x_min, x_max, y_min, y_max}`` from world_boundaries.json.

    Falls back to placement percentiles if the boundary file is missing.
    """
    bd_path = os.path.join(OUT_DIR, "world_boundaries.json")
    if os.path.exists(bd_path):
        with open(bd_path, encoding="utf-8") as f:
            bd = json.load(f)
        b = bd.get("bounds")
        if b:
            return b
    raise FileNotFoundError("world_boundaries.json missing — run `python run.py regions` first")


def load_edge_walls() -> list[dict]:
    bd_path = os.path.join(OUT_DIR, "world_boundaries.json")
    if not os.path.exists(bd_path):
        return []
    with open(bd_path, encoding="utf-8") as f:
        return json.load(f).get("edge_walls", [])


def load_zones() -> list[dict]:
    zp = os.path.join(OUT_DIR, "region_zones.json")
    if not os.path.exists(zp):
        return []
    with open(zp, encoding="utf-8") as f:
        return json.load(f)


# ---------------- raster generation ----------------

def build_raster(pts: list[dict], key_fn, bounds: dict):
    """Build a 2D raster of majority value-per-cell.

    *key_fn(point)* → string key (or None to skip).
    Returns (grid, key_list, extent, counts).
    """
    keys = sorted({k for p in pts if (k := key_fn(p)) is not None})
    key_to_idx = {k: i for i, k in enumerate(keys)}

    x_min, x_max = bounds["x_min"], bounds["x_max"]
    y_min, y_max = bounds["y_min"], bounds["y_max"]
    W = int(np.ceil((x_max - x_min) / PIXEL_CM))
    H = int(np.ceil((y_max - y_min) / PIXEL_CM))

    counts = np.zeros((len(keys), H, W), dtype=np.int32)
    for p in pts:
        k = key_fn(p)
        if k is None:
            continue
        x, y = p.get("x"), p.get("y")
        if x is None or y is None:
            continue
        if not (x_min <= x <= x_max and y_min <= y <= y_max):
            continue
        kidx = key_to_idx[k]
        col = int((x - x_min) / PIXEL_CM)
        row = int((y - y_min) / PIXEL_CM)
        if 0 <= row < H and 0 <= col < W:
            counts[kidx, row, col] += 1

    totals = counts.sum(axis=0)
    grid = np.where(totals > 0, counts.argmax(axis=0), -1)
    extent = (x_min, x_max, y_min, y_max)
    return grid, keys, extent, counts


# ---------------- polygon extraction ----------------

def _smooth_grid(mask: np.ndarray, passes: int = 1) -> np.ndarray:
    out = mask.copy()
    for _ in range(passes):
        padded = np.pad(out, 1, mode="constant", constant_values=False).astype(np.int8)
        s = (padded[:-2, :-2] + padded[:-2, 1:-1] + padded[:-2, 2:]
             + padded[1:-1, :-2] + padded[1:-1, 1:-1] + padded[1:-1, 2:]
             + padded[2:, :-2] + padded[2:, 1:-1] + padded[2:, 2:])
        out = (s >= 5)
    return out


def extract_polygons(grid, keys, extent, min_area_cells: int = 6,
                     smooth_passes: int = 2) -> dict:
    x_min, x_max, y_min, y_max = extent
    H, W = grid.shape
    xs = x_min + (np.arange(W) + 0.5) * PIXEL_CM
    ys = y_min + (np.arange(H) + 0.5) * PIXEL_CM

    polys_by_key: dict[str, list[list[list[float]]]] = {}
    for i, key in enumerate(keys):
        mask = (grid == i)
        if mask.sum() < min_area_cells:
            continue
        smoothed = _smooth_grid(mask, passes=smooth_passes)
        if smoothed.sum() < min_area_cells:
            continue
        z = smoothed.astype(np.float32)
        try:
            fig, ax = plt.subplots()
            cs = ax.contour(xs, ys, z, levels=[0.5])
            polys: list[list[list[float]]] = []
            try:
                for path in cs.get_paths():
                    for seg in path.to_polygons(closed_only=True):
                        if len(seg) >= 4:
                            polys.append([[float(x), float(y)] for x, y in seg])
            except Exception:
                for col in cs.collections:
                    for path in col.get_paths():
                        for seg in path.to_polygons(closed_only=True):
                            if len(seg) >= 4:
                                polys.append([[float(x), float(y)] for x, y in seg])
            plt.close(fig)
            if polys:
                polys_by_key[key] = polys
        except Exception as ex:
            logger.warning("contour failed for %s: %s", key, ex)
    return polys_by_key


def write_geojson(polys_by_key: dict, color_lookup: dict, out_path: str,
                  property_name: str = "biome") -> None:
    features = []
    for key, polys in polys_by_key.items():
        color = color_lookup.get(key, DEFAULT_COLOR)
        for poly in polys:
            features.append({
                "type": "Feature",
                "properties": {property_name: key, "color": color},
                "geometry": {"type": "Polygon", "coordinates": [poly]},
            })
    fc = {"type": "FeatureCollection", "features": features}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(fc, f, indent=2)
    logger.info("wrote %s (%d features)", out_path, len(features))


def write_flat(polys_by_key: dict, out_path: str) -> None:
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(polys_by_key, f, indent=2)
    logger.info("wrote %s (%d keys)", out_path, len(polys_by_key))


# ---------------- rendering ----------------

def _setup_axes(ax, title, extent, dark=True):
    if dark:
        ax.set_facecolor("#08121c")
    ax.set_title(title, color="white" if dark else "black", fontsize=14, pad=14)
    ax.set_xlabel("X (cm)", color="#bbb" if dark else "black", fontsize=9)
    ax.set_ylabel("Y (cm)", color="#bbb" if dark else "black", fontsize=9)
    ax.tick_params(colors="#bbb" if dark else "black", labelsize=7)
    ax.grid(True, alpha=0.1, color="#3a4452", linestyle=":")
    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])
    ax.set_aspect("equal", adjustable="box")


def _draw_edge_walls(ax, walls: list[dict], color: str = "#5a7fa6",
                     linewidth: float = 1.0, alpha: float = 0.6) -> None:
    for w in walls:
        p1, p2 = w["p1"], w["p2"]
        ax.plot([p1[0], p2[0]], [p1[1], p2[1]],
                color=color, linewidth=linewidth, alpha=alpha)


def _figsize_for_bounds(bounds: dict, max_dim: float = 22.0):
    w = bounds["x_max"] - bounds["x_min"]
    h = bounds["y_max"] - bounds["y_min"]
    aspect = w / max(h, 1)
    if aspect >= 1:
        return (max_dim, max_dim / aspect)
    return (max_dim * aspect, max_dim)


def render_raster_png(grid, keys, extent, bounds, color_lookup, title,
                      out_path: str, walls: list[dict]):
    H, W = grid.shape
    img = np.zeros((H, W, 4), dtype=np.uint8)
    for i, key in enumerate(keys):
        color = color_lookup.get(key, DEFAULT_COLOR)
        r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
        mask = grid == i
        img[mask] = [r, g, b, 230]
    img[grid == -1] = [0, 0, 0, 0]

    fig, ax = plt.subplots(figsize=_figsize_for_bounds(bounds), dpi=140)
    fig.patch.set_facecolor("#000814")
    ax.imshow(img, origin="lower",
              extent=[extent[0], extent[1], extent[2], extent[3]],
              interpolation="nearest")
    _draw_edge_walls(ax, walls)
    _setup_axes(ax, title, (extent[0], extent[1], extent[2], extent[3]))
    handles = [plt.Rectangle((0, 0), 1, 1, fc=color_lookup.get(k, DEFAULT_COLOR))
               for k in keys]
    ax.legend(handles, keys, loc="lower right",
              facecolor="#0e1f33", edgecolor="#2a3e5c", labelcolor="white",
              fontsize=8, framealpha=0.85, ncol=2)
    plt.tight_layout()
    fig.savefig(out_path, dpi=140, facecolor=fig.get_facecolor())
    plt.close(fig)
    logger.info("wrote %s", out_path)


def render_polygons_png(polys_by_key, extent, bounds, color_lookup, title,
                        out_path: str, walls: list[dict], label_short=False):
    fig, ax = plt.subplots(figsize=_figsize_for_bounds(bounds), dpi=140)
    fig.patch.set_facecolor("#000814")
    for key, polys in polys_by_key.items():
        color = color_lookup.get(key, DEFAULT_COLOR)
        for poly in polys:
            poly_arr = np.array(poly)
            ax.fill(poly_arr[:, 0], poly_arr[:, 1], color=color, alpha=0.55,
                    edgecolor=color, linewidth=1.3)
        biggest = max(polys, key=lambda p: len(p))
        poly_arr = np.array(biggest)
        cx, cy = poly_arr[:, 0].mean(), poly_arr[:, 1].mean()
        if label_short:
            label = key.split(".", 1)[-1]
        else:
            label = key
        txt = ax.text(cx, cy, label, color="white", ha="center", va="center",
                      fontsize=8, fontweight="bold")
        txt.set_path_effects([patheffects.Stroke(linewidth=2.5, foreground="black"),
                              patheffects.Normal()])
    _draw_edge_walls(ax, walls, linewidth=1.6, alpha=0.9)
    _setup_axes(ax, title, (extent[0], extent[1], extent[2], extent[3]))
    plt.tight_layout()
    fig.savefig(out_path, dpi=140, facecolor=fig.get_facecolor())
    plt.close(fig)
    logger.info("wrote %s", out_path)


# ---------------- main ----------------

def main():
    bounds = load_bounds()
    walls = load_edge_walls()
    raw = load_points()
    logger.info("loaded %d points; map bounds %.0fm × %.0fm",
                len(raw),
                (bounds["x_max"] - bounds["x_min"]) / 100,
                (bounds["y_max"] - bounds["y_min"]) / 100)
    biome_counts = Counter(p.get("biome") for p in raw if p.get("biome"))
    logger.info("biomes: %s", dict(biome_counts.most_common()))

    # --- biome-level outputs ---
    biome_grid, biome_keys, extent, _ = build_raster(
        raw, key_fn=lambda p: p.get("biome"), bounds=bounds)
    logger.info("biome raster: %d × %d", biome_grid.shape[1], biome_grid.shape[0])
    render_raster_png(biome_grid, biome_keys, extent, bounds, BIOME_COLORS,
                      f"Subnautica 2 — Biome Raster ({PIXEL_CM/100:.0f} m/pixel)",
                      os.path.join(OUT_DIR, "biome_raster.png"), walls)
    biome_polys = extract_polygons(biome_grid, biome_keys, extent,
                                   min_area_cells=4, smooth_passes=2)
    logger.info("biome polygons: %s",
                {k: len(v) for k, v in biome_polys.items()})
    write_geojson(biome_polys, BIOME_COLORS,
                  os.path.join(OUT_DIR, "biome_polygons.geojson"),
                  property_name="biome")
    write_flat(biome_polys, os.path.join(OUT_DIR, "biome_polygons.json"))
    render_polygons_png(biome_polys, extent, bounds, BIOME_COLORS,
                        "Subnautica 2 — Biome Polygons",
                        os.path.join(OUT_DIR, "biome_overlay.png"), walls)

    # --- sub-region outputs ---
    # Combine biome + sub_region into a single key like "CoralGardens.Plateaus".
    # Points without a sub_region fall back to just the biome name.
    def sub_key(p):
        biome = p.get("biome")
        sub = p.get("sub_region")
        if not biome:
            return None
        return f"{biome}.{sub}" if sub else biome

    sub_grid, sub_keys, extent, _ = build_raster(
        raw, key_fn=sub_key, bounds=bounds)
    sub_color_lookup = {**BIOME_COLORS, **SUBREGION_COLORS}
    render_raster_png(sub_grid, sub_keys, extent, bounds, sub_color_lookup,
                      f"Subnautica 2 — Sub-Region Raster ({PIXEL_CM/100:.0f} m/pixel)",
                      os.path.join(OUT_DIR, "subregion_raster.png"), walls)
    sub_polys = extract_polygons(sub_grid, sub_keys, extent,
                                 min_area_cells=4, smooth_passes=2)
    logger.info("sub-region polygons: %s",
                {k: len(v) for k, v in sub_polys.items()})
    write_geojson(sub_polys, sub_color_lookup,
                  os.path.join(OUT_DIR, "subregion_polygons.geojson"),
                  property_name="sub_region")
    write_flat(sub_polys, os.path.join(OUT_DIR, "subregion_polygons.json"))
    render_polygons_png(sub_polys, extent, bounds, sub_color_lookup,
                        "Subnautica 2 — Sub-Region Polygons",
                        os.path.join(OUT_DIR, "subregion_overlay.png"), walls,
                        label_short=True)


if __name__ == "__main__":
    main()
