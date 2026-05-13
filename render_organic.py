"""
Subnautica 2 biome map — clean version.

Key insight: the 58 BP_EdgeOfWorldVolume_C wall segments are already
*chained head-to-tail with 0 cm gaps* — they form a perfectly closed
polygon.  Greedy nearest-endpoint walk gives the canonical perimeter, no
alpha-shape / morphology / approximation needed.

Pipeline:
  1. Chain walls into a closed polygon via greedy walk on endpoint matches.
  2. Use that polygon as the playable-area mask (matplotlib.path.contains_points).
  3. For each sub-region, build a gaussian-smoothed density raster.
  4. Argmax → label grid; fill empty cells via distance transform.
  5. Strictly clip the result to the wall polygon — nothing leaks outside.
  6. Render organic polygons + visible wall segments.
"""
from __future__ import annotations

import json
import logging
import os
from collections import Counter

import matplotlib.pyplot as plt
import numpy as np
from matplotlib import patches as mpatches
from matplotlib import patheffects
from matplotlib.path import Path as MplPath
from scipy.ndimage import distance_transform_edt, gaussian_filter

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ---------------- colour tables ----------------

BIOME_COLORS = {
    "CoralGardens":   "#ff6b9a",
    "OvergrownRuins": "#7ad06a",
    "JellyPlateaus":  "#a48cff",
    "SparsePlains":   "#e7c068",
    "WorldTree":      "#9e6536",
    "Generic":        "#8a96a6",
}
SUBREGION_COLORS = {
    "CoralGardens.Shallows":      "#ffb3c8",
    "CoralGardens.Plateaus":      "#ff6b9a",
    "CoralGardens.Graveyard":     "#6a4a52",
    "CoralGardens.AnemoneHills":  "#ffd24a",
    "CoralGardens.TufaTowers":    "#bd8888",
    "CoralGardens.NorthRaceway":  "#ea4c8f",
    "CoralGardens.SouthRaceway":  "#c64278",
    "CoralGardens.BlightedCoral": "#7a3a5a",
    "CoralGardens.Leadzone":      "#4a4a5a",
    "OvergrownRuins.Observatory": "#7ad06a",
    "OvergrownRuins.PowerPlant":  "#4ec0a4",
    "OvergrownRuins.RootCanyon":  "#5a8c46",
}
DEFAULT_COLOR = "#444a55"
WALL_COLOR    = "#e6364a"

PIXEL_CM        = 500   # 5 m / pixel
SMOOTH_SIGMA_PX = 6     # larger blur = more organic curves, fewer fragments
MIN_REGION_AREA_PX = 60 # drop tiny argmax islands before contouring


def _color_for(key: str) -> str:
    if key in SUBREGION_COLORS:
        return SUBREGION_COLORS[key]
    return BIOME_COLORS.get(key.split(".", 1)[0], DEFAULT_COLOR)


# ---------------- IO ----------------

def load_points() -> list[dict]:
    with open(os.path.join(OUT_DIR, "biome_points_v2.json"), encoding="utf-8") as f:
        return json.load(f)["points"]


def load_walls() -> list[dict]:
    with open(os.path.join(OUT_DIR, "world_boundaries.json"), encoding="utf-8") as f:
        return json.load(f)["edge_walls"]


def load_bounds() -> dict:
    with open(os.path.join(OUT_DIR, "world_boundaries.json"), encoding="utf-8") as f:
        return json.load(f)["bounds"]


# ---------------- wall chain → closed polygon ----------------

def chain_walls(walls: list[dict]) -> np.ndarray:
    """Greedy nearest-endpoint walk through the 58 wall segments.

    Each wall is a segment with (p1, p2); the game's walls share endpoints
    exactly (0 cm gaps), so this walk produces a perfect closed polygon.
    Returns Nx2 ndarray of (x, y) vertices, with start point appended for closure.
    """
    remaining = list(range(len(walls)))
    order: list[tuple[int, bool]] = []  # (wall_idx, reversed?)
    current = remaining.pop(0)
    order.append((current, False))
    end = np.array(walls[current]["p2"])
    while remaining:
        best, best_d, best_rev = None, float("inf"), False
        for ri in remaining:
            for use_p2 in (False, True):
                other_start = np.array(walls[ri]["p2"] if use_p2 else walls[ri]["p1"])
                d = np.linalg.norm(end - other_start)
                if d < best_d:
                    best_d, best, best_rev = d, ri, use_p2
        remaining.remove(best)
        order.append((best, best_rev))
        end = np.array(walls[best]["p1"] if best_rev else walls[best]["p2"])

    pts: list[list[float]] = []
    for idx, rev in order:
        w = walls[idx]
        if rev:
            pts.append(w["p2"])
            pts.append(w["p1"])
        else:
            pts.append(w["p1"])
            pts.append(w["p2"])
    # Close the loop
    pts.append(pts[0])
    return np.array(pts)


def polygon_mask(poly: np.ndarray, bounds: dict, pixel_cm: int
                 ) -> tuple[np.ndarray, tuple]:
    x_min, x_max = bounds["x_min"], bounds["x_max"]
    y_min, y_max = bounds["y_min"], bounds["y_max"]
    W = int(np.ceil((x_max - x_min) / pixel_cm))
    H = int(np.ceil((y_max - y_min) / pixel_cm))
    xs = x_min + (np.arange(W) + 0.5) * pixel_cm
    ys = y_min + (np.arange(H) + 0.5) * pixel_cm
    XX, YY = np.meshgrid(xs, ys)
    pts = np.column_stack([XX.ravel(), YY.ravel()])
    inside = MplPath(poly).contains_points(pts).reshape(H, W)
    return inside, (x_min, x_max, y_min, y_max)


# ---------------- density-based labels ----------------

def build_labels(pts: list[dict], bounds: dict, key_fn,
                 playable: np.ndarray, sigma_px: int,
                 data_dilate_px: int = 12):
    """Per-key density rasters → argmax label grid.

    Only labels cells that have real point data within ``data_dilate_px``
    pixels.  This prevents argmax from spreading a label into the wall-bounded
    interior areas that have no zones and no placements (e.g. the empty
    top-left of the map).
    """
    from scipy.ndimage import binary_dilation

    keys = sorted({k for p in pts if (k := key_fn(p)) is not None})
    key_to_idx = {k: i for i, k in enumerate(keys)}
    x_min, x_max = bounds["x_min"], bounds["x_max"]
    y_min, y_max = bounds["y_min"], bounds["y_max"]
    W = int(np.ceil((x_max - x_min) / PIXEL_CM))
    H = int(np.ceil((y_max - y_min) / PIXEL_CM))

    density = np.zeros((len(keys), H, W), dtype=np.float32)
    for p in pts:
        k = key_fn(p)
        if k is None:
            continue
        x, y = p.get("x"), p.get("y")
        if x is None or y is None:
            continue
        if not (x_min <= x <= x_max and y_min <= y <= y_max):
            continue
        col = int((x - x_min) / PIXEL_CM)
        row = int((y - y_min) / PIXEL_CM)
        if 0 <= row < H and 0 <= col < W:
            density[key_to_idx[k], row, col] += 1

    # Build a "has nearby data" mask BEFORE smoothing — any cell with at least
    # one real point, dilated by data_dilate_px so gaussian falloff is covered.
    raw_total = density.sum(axis=0)
    has_data = raw_total > 0
    data_mask = binary_dilation(has_data, iterations=data_dilate_px)

    smoothed = np.empty_like(density)
    for i in range(len(keys)):
        smoothed[i] = gaussian_filter(density[i], sigma=sigma_px, mode="constant")

    # Restrict to playable AND near-data
    valid = playable & data_mask
    smoothed *= valid.astype(np.float32)[None, :, :]

    totals = smoothed.sum(axis=0)
    grid = np.where((totals > 1e-3) & valid, smoothed.argmax(axis=0), -1)
    return grid, keys, (x_min, x_max, y_min, y_max)


def remove_small_islands(grid: np.ndarray, min_area_px: int) -> np.ndarray:
    """For each label, drop connected components smaller than *min_area_px*."""
    from scipy.ndimage import label as cc_label
    out = grid.copy()
    n_labels = grid.max() + 1
    for lab in range(n_labels):
        mask = (grid == lab)
        cc, ncc = cc_label(mask)
        for k in range(1, ncc + 1):
            if (cc == k).sum() < min_area_px:
                out[cc == k] = -1
    return out


def fill_holes_with_nearest(grid: np.ndarray, playable: np.ndarray,
                             max_distance_px: int = 20) -> np.ndarray:
    """Fill empty cells with the nearest filled label, but ONLY within
    ``max_distance_px`` pixels of a real labeled cell.

    Areas far from any actual region data (e.g. the empty top-left where
    there are no zone boxes or placements) stay unlabeled instead of being
    greedily painted with a distant region's colour.
    """
    filled = grid >= 0
    if not filled.any():
        return grid
    distances, (rows, cols) = distance_transform_edt(~filled, return_indices=True)
    nearest = grid[rows, cols]
    out = grid.copy()
    # Only fill empty cells that are CLOSE to a real labeled cell
    holes = (~filled) & playable & (distances <= max_distance_px)
    out[holes] = nearest[holes]
    out[~playable] = -1
    return out


# ---------------- polygon extraction ----------------

def extract_polygons(grid: np.ndarray, keys: list[str], extent: tuple,
                     min_area_cells: int = 30) -> dict:
    x_min, x_max, y_min, y_max = extent
    H, W = grid.shape
    xs = x_min + (np.arange(W) + 0.5) * PIXEL_CM
    ys = y_min + (np.arange(H) + 0.5) * PIXEL_CM
    out: dict[str, list[list[list[float]]]] = {}
    for i, key in enumerate(keys):
        mask = (grid == i)
        if mask.sum() < min_area_cells:
            continue
        fig, ax = plt.subplots()
        cs = ax.contour(xs, ys, mask.astype(np.float32), levels=[0.5])
        polys: list[list[list[float]]] = []
        try:
            for path in cs.get_paths():
                for seg in path.to_polygons(closed_only=True):
                    if len(seg) >= 6:
                        polys.append([[float(x), float(y)] for x, y in seg])
        except Exception:
            for col in cs.collections:
                for path in col.get_paths():
                    for seg in path.to_polygons(closed_only=True):
                        if len(seg) >= 6:
                            polys.append([[float(x), float(y)] for x, y in seg])
        plt.close(fig)
        if polys:
            out[key] = polys
    return out


# ---------------- rendering ----------------

def _figsize(bounds, max_dim=22):
    w = bounds["x_max"] - bounds["x_min"]
    h = bounds["y_max"] - bounds["y_min"]
    aspect = w / max(h, 1)
    return (max_dim, max_dim / aspect) if aspect >= 1 else (max_dim * aspect, max_dim)


def _setup_axes(ax, title, bounds):
    ax.set_facecolor("#08121c")
    ax.set_title(title, color="white", fontsize=14, pad=14)
    ax.set_xlabel("X (cm)", color="#bbb", fontsize=9)
    ax.set_ylabel("Y (cm)", color="#bbb", fontsize=9)
    ax.tick_params(colors="#bbb", labelsize=7)
    ax.grid(True, alpha=0.1, color="#3a4452", linestyle=":")
    ax.set_xlim(bounds["x_min"], bounds["x_max"])
    ax.set_ylim(bounds["y_min"], bounds["y_max"])
    ax.set_aspect("equal", adjustable="box")


def _draw_walls(ax, walls, color=WALL_COLOR, linewidth=2.4, alpha=0.95):
    for w in walls:
        p1, p2 = w["p1"], w["p2"]
        ax.plot([p1[0], p2[0]], [p1[1], p2[1]],
                color=color, linewidth=linewidth, alpha=alpha,
                solid_capstyle="round")


def render_polygons(polys_by_key, bounds, walls, perim, out_path):
    fig, ax = plt.subplots(figsize=_figsize(bounds), dpi=140)
    fig.patch.set_facecolor("#000814")
    for key, polys in polys_by_key.items():
        color = _color_for(key)
        for poly in polys:
            p = np.array(poly)
            ax.fill(p[:, 0], p[:, 1], color=color, alpha=0.75,
                    edgecolor=color, linewidth=1.0)
        biggest = max(polys, key=lambda p: len(p))
        p = np.array(biggest)
        cx, cy = p[:, 0].mean(), p[:, 1].mean()
        label = key.split(".", 1)[-1] if "." in key else key
        txt = ax.text(cx, cy, label, color="white", ha="center", va="center",
                      fontsize=8, fontweight="bold")
        txt.set_path_effects([patheffects.Stroke(linewidth=2.5, foreground="black"),
                              patheffects.Normal()])
    # Faint perimeter outline
    ax.plot(perim[:, 0], perim[:, 1], color="#ffffff", linewidth=0.6, alpha=0.4)
    _draw_walls(ax, walls)
    _setup_axes(ax, "Subnautica 2 — Biome Map (wall-bounded)", bounds)
    plt.tight_layout()
    fig.savefig(out_path, dpi=140, facecolor=fig.get_facecolor())
    plt.close(fig)
    logger.info("wrote %s", out_path)


def render_raster(grid, keys, extent, bounds, walls, perim, out_path):
    H, W = grid.shape
    img = np.zeros((H, W, 4), dtype=np.uint8)
    for i, key in enumerate(keys):
        color = _color_for(key)
        r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
        img[grid == i] = [r, g, b, 235]
    img[grid == -1] = [0, 0, 0, 0]
    fig, ax = plt.subplots(figsize=_figsize(bounds), dpi=140)
    fig.patch.set_facecolor("#000814")
    ax.imshow(img, origin="lower",
              extent=[extent[0], extent[1], extent[2], extent[3]],
              interpolation="bilinear")
    ax.plot(perim[:, 0], perim[:, 1], color="#ffffff", linewidth=0.6, alpha=0.4)
    _draw_walls(ax, walls)
    handles = [mpatches.Patch(facecolor=_color_for(k), label=k) for k in keys]
    ax.legend(handles=handles, loc="lower right",
              facecolor="#0e1f33", edgecolor="#2a3e5c", labelcolor="white",
              fontsize=8, framealpha=0.85, ncol=2)
    _setup_axes(ax, f"Subnautica 2 — Biome Raster ({PIXEL_CM/100:.0f} m/px, σ={SMOOTH_SIGMA_PX})",
                bounds)
    plt.tight_layout()
    fig.savefig(out_path, dpi=140, facecolor=fig.get_facecolor())
    plt.close(fig)
    logger.info("wrote %s", out_path)


# ---------------- GeoJSON ----------------

def write_geojson(polys_by_key, out_path):
    features = []
    for key, polys in polys_by_key.items():
        color = _color_for(key)
        for poly in polys:
            features.append({
                "type": "Feature",
                "properties": {"key": key, "color": color},
                "geometry": {"type": "Polygon", "coordinates": [poly]},
            })
    fc = {"type": "FeatureCollection", "features": features}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(fc, f, indent=2)
    logger.info("wrote %s (%d features)", out_path, len(features))


def write_outline_geojson(perim: np.ndarray, out_path: str):
    ring = [[float(x), float(y)] for x, y in perim]
    fc = {"type": "FeatureCollection",
          "features": [{
              "type": "Feature",
              "properties": {"name": "world_outline"},
              "geometry": {"type": "Polygon", "coordinates": [ring]},
          }]}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(fc, f, indent=2)
    logger.info("wrote %s", out_path)


# ---------------- main ----------------

def main():
    pts = load_points()
    walls = load_walls()
    bounds = load_bounds()
    logger.info("loaded %d points (raw), %d walls, bounds %.0fm × %.0fm",
                len(pts), len(walls),
                (bounds["x_max"] - bounds["x_min"]) / 100,
                (bounds["y_max"] - bounds["y_min"]) / 100)

    # Keep only authoritative classifications — points actually inside a
    # UWEBoxWorldZone.  mesh_path and prefab_ref fallbacks introduce a generic
    # "CoralGardens" / "OvergrownRuins" blob that the user doesn't want
    # spilling into areas where no zone box exists (see the empty top-left
    # in zone_rectangles.png).
    pts = [p for p in pts if p.get("source") == "region_box"]
    logger.info("after source=region_box filter: %d points", len(pts))

    perim = chain_walls(walls)
    closure = float(np.linalg.norm(perim[-1] - perim[0]))
    logger.info("perimeter polygon: %d vertices, closure %.0fcm", len(perim), closure)

    write_outline_geojson(perim, os.path.join(OUT_DIR, "world_outline.geojson"))

    playable, extent = polygon_mask(perim, bounds, PIXEL_CM)
    logger.info("playable cells: %d (raster %d × %d at %dcm/px)",
                int(playable.sum()), playable.shape[1], playable.shape[0], PIXEL_CM)

    def sub_key(p):
        biome = p.get("biome")
        sub = p.get("sub_region")
        if not biome:
            return None
        return f"{biome}.{sub}" if sub else biome

    grid, keys, _ = build_labels(pts, bounds, sub_key, playable, SMOOTH_SIGMA_PX)
    logger.info("density argmax: %d labeled cells / %d playable",
                int((grid >= 0).sum()), int(playable.sum()))

    cleaned = remove_small_islands(grid, MIN_REGION_AREA_PX)
    logger.info("after island removal (<%dpx): %d labeled cells",
                MIN_REGION_AREA_PX, int((cleaned >= 0).sum()))

    filled = fill_holes_with_nearest(cleaned, playable)
    logger.info("after hole fill: %d labeled cells", int((filled >= 0).sum()))

    render_raster(filled, keys, extent, bounds, walls, perim,
                  os.path.join(OUT_DIR, "organic_raster.png"))
    polys = extract_polygons(filled, keys, extent, min_area_cells=MIN_REGION_AREA_PX)
    logger.info("polygons: %s", {k: len(v) for k, v in polys.items()})
    render_polygons(polys, bounds, walls, perim,
                    os.path.join(OUT_DIR, "organic_overlay.png"))
    write_geojson(polys, os.path.join(OUT_DIR, "organic_polygons.geojson"))


if __name__ == "__main__":
    main()
