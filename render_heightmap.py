"""
Subnautica 2 heightmap render — terrain-style: hillshade + biome tint.

Visual approach (instead of the previous garish gradient):
  1. Grid placement-point Z values (min-Z per cell) → seafloor heightmap.
  2. Hillshade the heightmap with matplotlib's LightSource (directional
     lighting + slope shading) → grayscale relief.
  3. Multiply the hillshade by per-pixel biome colours → "coloured terrain".
  4. Mask to the wall polygon, draw walls + 50m contours on top.
"""
from __future__ import annotations

import json
import logging
import os

import matplotlib.pyplot as plt
import numpy as np
from matplotlib import patheffects
from matplotlib.colors import LightSource, to_rgb
from matplotlib.path import Path as MplPath
from scipy.ndimage import distance_transform_edt, gaussian_filter

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


PIXEL_CM = 500
SMOOTH_SIGMA_PX = 3
HILLSHADE_AZDEG = 315     # light from NW
HILLSHADE_ALTDEG = 30     # lower sun = longer shadows = more drama
VERT_EXAG = 0.01          # heavy vertical exaggeration — game underwater terrain
                          #   has gentle slopes, need exaggeration to show relief

WALL_COLOR = "#ffffff"

# Per-region colours (muted oceanic palette — looks better at low saturation
# when multiplied with hillshade).
SUBREGION_COLORS = {
    "CoralGardens.Shallows":      "#d99fb8",
    "CoralGardens.Plateaus":      "#c46789",
    "CoralGardens.Graveyard":     "#6a4e58",
    "CoralGardens.AnemoneHills":  "#caa253",
    "CoralGardens.TufaTowers":    "#a07a78",
    "CoralGardens.NorthRaceway":  "#a8497a",
    "CoralGardens.SouthRaceway":  "#923c64",
    "CoralGardens.BlightedCoral": "#623a55",
    "CoralGardens.Leadzone":      "#535566",
    "OvergrownRuins.Observatory": "#6ea868",
    "OvergrownRuins.PowerPlant":  "#4a9d8b",
    "OvergrownRuins.RootCanyon":  "#5a7e4b",
}
UNKNOWN_TINT = (0.35, 0.45, 0.55)   # neutral steel blue for unlabelled cells


# ---------------- IO ----------------

def load_points():
    with open(os.path.join(OUT_DIR, "biome_points_v2.json"), encoding="utf-8") as f:
        return json.load(f)["points"]


def load_walls():
    with open(os.path.join(OUT_DIR, "world_boundaries.json"), encoding="utf-8") as f:
        return json.load(f)["edge_walls"]


def load_bounds():
    with open(os.path.join(OUT_DIR, "world_boundaries.json"), encoding="utf-8") as f:
        return json.load(f)["bounds"]


def load_polygons():
    with open(os.path.join(OUT_DIR, "organic_polygons.geojson"), encoding="utf-8") as f:
        return json.load(f)["features"]


# ---------------- wall chain ----------------

def chain_walls(walls):
    remaining = list(range(len(walls)))
    order = []
    current = remaining.pop(0)
    order.append((current, False))
    end = np.array(walls[current]["p2"])
    while remaining:
        best, best_d, best_rev = None, float("inf"), False
        for ri in remaining:
            for use_p2 in (False, True):
                other = np.array(walls[ri]["p2"] if use_p2 else walls[ri]["p1"])
                d = np.linalg.norm(end - other)
                if d < best_d:
                    best_d, best, best_rev = d, ri, use_p2
        remaining.remove(best)
        order.append((best, best_rev))
        end = np.array(walls[best]["p1"] if best_rev else walls[best]["p2"])
    pts = []
    for idx, rev in order:
        w = walls[idx]
        if rev:
            pts.append(w["p2"]); pts.append(w["p1"])
        else:
            pts.append(w["p1"]); pts.append(w["p2"])
    pts.append(pts[0])
    return np.array(pts)


def polygon_mask(poly, bounds, pixel_cm):
    x_min, x_max = bounds["x_min"], bounds["x_max"]
    y_min, y_max = bounds["y_min"], bounds["y_max"]
    W = int(np.ceil((x_max - x_min) / pixel_cm))
    H = int(np.ceil((y_max - y_min) / pixel_cm))
    xs = x_min + (np.arange(W) + 0.5) * pixel_cm
    ys = y_min + (np.arange(H) + 0.5) * pixel_cm
    XX, YY = np.meshgrid(xs, ys)
    inside = MplPath(poly).contains_points(
        np.column_stack([XX.ravel(), YY.ravel()])).reshape(H, W)
    return inside


# ---------------- heightmap ----------------

def build_heightmap(pts, bounds, pixel_cm, sigma_px):
    x_min, x_max = bounds["x_min"], bounds["x_max"]
    y_min, y_max = bounds["y_min"], bounds["y_max"]
    W = int(np.ceil((x_max - x_min) / pixel_cm))
    H = int(np.ceil((y_max - y_min) / pixel_cm))
    z_grid = np.full((H, W), np.nan, dtype=np.float32)
    for p in pts:
        x, y, z = p.get("x"), p.get("y"), p.get("z")
        if x is None or y is None or z is None:
            continue
        if not (x_min <= x <= x_max and y_min <= y <= y_max):
            continue
        col = int((x - x_min) / pixel_cm)
        row = int((y - y_min) / pixel_cm)
        if 0 <= row < H and 0 <= col < W:
            cur = z_grid[row, col]
            if np.isnan(cur) or z < cur:
                z_grid[row, col] = z
    nan_mask = np.isnan(z_grid)
    if nan_mask.any():
        _, (rr, cc) = distance_transform_edt(nan_mask, return_indices=True)
        z_grid = z_grid[rr, cc]
    z_grid = gaussian_filter(z_grid, sigma=sigma_px, mode="nearest")
    return z_grid


# ---------------- biome tint per pixel ----------------

def build_biome_tint(features, bounds, pixel_cm):
    """Rasterize biome polygons into an RGB float image (H, W, 3) in [0,1]."""
    x_min, x_max = bounds["x_min"], bounds["x_max"]
    y_min, y_max = bounds["y_min"], bounds["y_max"]
    W = int(np.ceil((x_max - x_min) / pixel_cm))
    H = int(np.ceil((y_max - y_min) / pixel_cm))
    xs = x_min + (np.arange(W) + 0.5) * pixel_cm
    ys = y_min + (np.arange(H) + 0.5) * pixel_cm
    XX, YY = np.meshgrid(xs, ys)
    flat = np.column_stack([XX.ravel(), YY.ravel()])

    tint = np.tile(np.array(UNKNOWN_TINT, dtype=np.float32),
                   (H, W, 1))
    for feat in features:
        coords = np.array(feat["geometry"]["coordinates"][0])
        key = feat["properties"]["key"]
        color = SUBREGION_COLORS.get(key, feat["properties"].get("color"))
        if color is None:
            continue
        rgb = np.array(to_rgb(color), dtype=np.float32)
        inside = MplPath(coords).contains_points(flat).reshape(H, W)
        for c in range(3):
            tint[..., c] = np.where(inside, rgb[c], tint[..., c])
    return tint


# ---------------- composite ----------------

def composite(z_grid, biome_tint, playable, vert_exag=VERT_EXAG):
    ls = LightSource(azdeg=HILLSHADE_AZDEG, altdeg=HILLSHADE_ALTDEG)
    shade = ls.hillshade(z_grid, vert_exag=vert_exag,
                         dx=PIXEL_CM, dy=PIXEL_CM)
    # Stretch contrast: deep shadows (0.25) → highlights (1.2 → clipped to 1.0)
    shade = np.clip(0.25 + 1.05 * (shade - 0.5), 0.0, 1.0)
    # Multiply biome tint by hillshade — terrain darkens regions facing away
    rgb = biome_tint * shade[..., None]
    rgb = np.clip(rgb, 0.0, 1.0)
    alpha = playable.astype(np.float32)
    rgba = np.concatenate([rgb, alpha[..., None]], axis=-1)
    return (rgba * 255).astype(np.uint8)


# ---------------- render ----------------

def _figsize(bounds, max_dim=22):
    w = bounds["x_max"] - bounds["x_min"]
    h = bounds["y_max"] - bounds["y_min"]
    a = w / max(h, 1)
    return (max_dim, max_dim / a) if a >= 1 else (max_dim * a, max_dim)


def _setup_axes(ax, title, bounds):
    ax.set_facecolor("#02080f")
    ax.set_title(title, color="white", fontsize=14, pad=14)
    ax.set_xlabel("X (cm)", color="#888", fontsize=9)
    ax.set_ylabel("Y (cm)", color="#888", fontsize=9)
    ax.tick_params(colors="#888", labelsize=7)
    ax.set_xlim(bounds["x_min"], bounds["x_max"])
    ax.set_ylim(bounds["y_min"], bounds["y_max"])
    ax.set_aspect("equal", adjustable="box")
    for s in ax.spines.values():
        s.set_color("#2a3e5c")


def _draw_walls(ax, walls, color=WALL_COLOR, lw=1.4, alpha=0.7):
    for w in walls:
        p1, p2 = w["p1"], w["p2"]
        ax.plot([p1[0], p2[0]], [p1[1], p2[1]], color=color,
                linewidth=lw, alpha=alpha, solid_capstyle="round")


def render(z_grid, biome_tint, playable, bounds, walls, perim, polygons,
           out_path):
    rgba = composite(z_grid, biome_tint, playable)
    extent = (bounds["x_min"], bounds["x_max"], bounds["y_min"], bounds["y_max"])

    fig, ax = plt.subplots(figsize=_figsize(bounds), dpi=160)
    fig.patch.set_facecolor("#02080f")
    ax.imshow(rgba, origin="lower", extent=extent, interpolation="bilinear")

    # 50m bathymetric contours
    H, W = z_grid.shape
    xs = np.linspace(extent[0], extent[1], W)
    ys = np.linspace(extent[2], extent[3], H)
    z_masked = np.where(playable, z_grid, np.nan)
    z_lo = float(np.nanpercentile(z_masked, 1))
    z_hi = float(np.nanpercentile(z_masked, 99))
    levels = np.arange(np.floor(z_lo / 10000) * 10000,
                       np.ceil(z_hi / 10000) * 10000 + 10000, 10000)  # 100m
    cs = ax.contour(xs, ys, np.where(np.isnan(z_masked), z_lo, z_masked),
                    levels=levels, colors="#000000", linewidths=0.4,
                    alpha=0.35)
    ax.clabel(cs, inline=True, fmt=lambda v: f"{abs(v) / 100:.0f}m",
              fontsize=6, colors="#ffffff")

    # Region labels
    for feat in polygons:
        coords = np.array(feat["geometry"]["coordinates"][0])
        key = feat["properties"]["key"]
        cx, cy = coords[:, 0].mean(), coords[:, 1].mean()
        label = key.split(".", 1)[-1]
        txt = ax.text(cx, cy, label, color="white", ha="center", va="center",
                      fontsize=9, fontweight="bold")
        txt.set_path_effects([patheffects.Stroke(linewidth=2.5, foreground="black"),
                              patheffects.Normal()])

    _draw_walls(ax, walls)
    _setup_axes(ax, "Subnautica 2 — Biome Terrain Map", bounds)
    plt.tight_layout()
    fig.savefig(out_path, dpi=160, facecolor=fig.get_facecolor())
    plt.close(fig)
    logger.info("wrote %s", out_path)


def render_hillshade_only(z_grid, playable, bounds, walls, out_path):
    ls = LightSource(azdeg=HILLSHADE_AZDEG, altdeg=HILLSHADE_ALTDEG)
    shade = ls.hillshade(z_grid, vert_exag=VERT_EXAG, dx=PIXEL_CM, dy=PIXEL_CM)
    rgb = np.dstack([shade, shade, shade])
    rgba = np.dstack([rgb, playable.astype(np.float32)])
    rgba = (rgba * 255).astype(np.uint8)
    extent = (bounds["x_min"], bounds["x_max"], bounds["y_min"], bounds["y_max"])
    fig, ax = plt.subplots(figsize=_figsize(bounds), dpi=160)
    fig.patch.set_facecolor("#02080f")
    ax.imshow(rgba, origin="lower", extent=extent, interpolation="bilinear")
    _draw_walls(ax, walls, color="#e6364a")
    _setup_axes(ax, "Subnautica 2 — Hillshade (terrain relief)", bounds)
    plt.tight_layout()
    fig.savefig(out_path, dpi=160, facecolor=fig.get_facecolor())
    plt.close(fig)
    logger.info("wrote %s", out_path)


# ---------------- main ----------------

def main():
    pts = load_points()
    walls = load_walls()
    bounds = load_bounds()
    polygons = load_polygons()
    perim = chain_walls(walls)
    playable = polygon_mask(perim, bounds, PIXEL_CM)
    logger.info("playable cells: %d", int(playable.sum()))

    logger.info("building heightmap from %d points...", len(pts))
    z_grid = build_heightmap(pts, bounds, PIXEL_CM, SMOOTH_SIGMA_PX)
    logger.info("Z range: %.0fm to %.0fm",
                float(np.nanmin(np.where(playable, z_grid, np.nan))) / 100,
                float(np.nanmax(np.where(playable, z_grid, np.nan))) / 100)

    logger.info("rasterizing biome tint from %d polygons...", len(polygons))
    tint = build_biome_tint(polygons, bounds, PIXEL_CM)

    render(z_grid, tint, playable, bounds, walls, perim, polygons,
           os.path.join(OUT_DIR, "terrain_map.png"))
    render_hillshade_only(z_grid, playable, bounds, walls,
                          os.path.join(OUT_DIR, "hillshade.png"))


if __name__ == "__main__":
    main()
