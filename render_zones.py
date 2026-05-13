"""
Render the Subnautica 2 region map directly from UWEBoxWorldZone bounds,
with gaps filled by nearest-zone expansion bounded by the edge-of-world walls.

Pipeline:
  1. Paint the 204 region boxes onto a 10 m/pixel grid; smaller boxes win on
     overlap to preserve sub-region specificity.
  2. Build a "playable area" polygon from the 58 BP_EdgeOfWorldVolume_C walls
     by sorting their centres in angular order around the centroid.
  3. For every empty pixel *inside the playable polygon*, copy the label of
     the nearest filled pixel (scipy distance transform).  This eliminates
     gaps between boxes without bleeding outside the wall ring.
  4. Render the filled raster, the unioned polygons, the wall ring outline,
     and the raw wall segments.

Outputs:
  out/zone_filled_raster.png     — filled raster
  out/zone_filled_overlay.png    — clean polygons with labels + wall outline
  out/zone_filled_polygons.geojson
  out/zone_filled_polygons.json
  out/edge_walls.geojson         — wall segments as LineString features
  out/world_outline.geojson      — closed perimeter polygon
"""
from __future__ import annotations

import json
import logging
import os

import matplotlib.pyplot as plt
import numpy as np
from matplotlib import patches as mpatches
from matplotlib import patheffects
from matplotlib.path import Path as MplPath
from scipy.ndimage import distance_transform_edt

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


BIOME_COLORS = {
    "CoralGardens":      "#ff6b9a",
    "OvergrownRuins":    "#7ad06a",
    "JellyPlateaus":     "#a48cff",
    "SparsePlains":      "#e7c068",
    "WorldTree":         "#9e6536",
    "Void":              "#222b3a",
    "DeepStart":         "#3a8bff",
    "CollectorLeviathanRegion": "#ff5a3b",
    "Generic":           "#8a96a6",
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
WALL_COLOR = "#e6364a"  # red — physical barriers visible

PIXEL_CM = 1000  # 10 m / pixel


# ---------------- IO ----------------

def load_zones() -> list[dict]:
    with open(os.path.join(OUT_DIR, "region_zones.json"), encoding="utf-8") as f:
        return json.load(f)


def load_walls() -> list[dict]:
    bd_path = os.path.join(OUT_DIR, "world_boundaries.json")
    if not os.path.exists(bd_path):
        return []
    with open(bd_path, encoding="utf-8") as f:
        return json.load(f).get("edge_walls", [])


def load_bounds() -> dict:
    bd_path = os.path.join(OUT_DIR, "world_boundaries.json")
    with open(bd_path, encoding="utf-8") as f:
        return json.load(f)["bounds"]


def _zone_aabb(z: dict):
    c, e = z.get("center"), z.get("extent")
    if not c or not e:
        return None
    return c[0] - e[0], c[0] + e[0], c[1] - e[1], c[1] + e[1]


def _zone_key(z: dict) -> str:
    biome = z.get("biome") or "Unknown"
    sub = z.get("sub_region")
    return f"{biome}.{sub}" if sub else biome


def _zone_area(z: dict) -> float:
    e = z.get("extent") or [0, 0, 0]
    return abs((2 * e[0]) * (2 * e[1]))


def _color_for(key: str) -> str:
    if key in SUBREGION_COLORS:
        return SUBREGION_COLORS[key]
    biome = key.split(".", 1)[0]
    return BIOME_COLORS.get(biome, DEFAULT_COLOR)


# ---------------- perimeter polygon from walls ----------------

def build_perimeter_polygon(walls: list[dict]) -> np.ndarray:
    """Sort wall centres in angular order around their centroid.

    The 58 walls aren't head-to-tail connected, but their centres lie on the
    perimeter ring.  Sorting them by polar angle gives a closed polygon that
    cleanly approximates the playable area boundary.
    """
    centers = np.array([w["center"][:2] for w in walls])
    cx, cy = centers.mean(axis=0)
    angles = np.arctan2(centers[:, 1] - cy, centers[:, 0] - cx)
    order = np.argsort(angles)
    poly = centers[order]
    # Close the loop
    return np.vstack([poly, poly[:1]])


def perimeter_mask(perimeter_xy: np.ndarray, bounds: dict, pixel_cm: int = PIXEL_CM,
                   inset_cm: float = 0.0) -> tuple[np.ndarray, tuple]:
    """Return (H×W bool) mask of cells inside the perimeter polygon, plus extent."""
    x_min, x_max = bounds["x_min"], bounds["x_max"]
    y_min, y_max = bounds["y_min"], bounds["y_max"]
    W = int(np.ceil((x_max - x_min) / pixel_cm))
    H = int(np.ceil((y_max - y_min) / pixel_cm))
    xs = x_min + (np.arange(W) + 0.5) * pixel_cm
    ys = y_min + (np.arange(H) + 0.5) * pixel_cm
    XX, YY = np.meshgrid(xs, ys)
    pts = np.column_stack([XX.ravel(), YY.ravel()])
    path = MplPath(perimeter_xy)
    inside = path.contains_points(pts).reshape(H, W)
    return inside, (x_min, x_max, y_min, y_max)


# ---------------- paint zone boxes ----------------

def paint_zones(zones: list[dict], bounds: dict):
    """Return (grid, keys, extent) with each pixel labeled by smallest containing box."""
    keys = sorted({_zone_key(z) for z in zones if z.get("biome")})
    key_to_idx = {k: i for i, k in enumerate(keys)}
    x_min, x_max = bounds["x_min"], bounds["x_max"]
    y_min, y_max = bounds["y_min"], bounds["y_max"]
    W = int(np.ceil((x_max - x_min) / PIXEL_CM))
    H = int(np.ceil((y_max - y_min) / PIXEL_CM))
    grid = -np.ones((H, W), dtype=np.int32)
    area_grid = np.full((H, W), np.inf, dtype=np.float64)
    for z in sorted(zones, key=lambda z: -_zone_area(z)):
        biome = z.get("biome")
        if not biome:
            continue
        aabb = _zone_aabb(z)
        if aabb is None:
            continue
        x_lo, x_hi, y_lo, y_hi = aabb
        col_lo = max(0, int((x_lo - x_min) / PIXEL_CM))
        col_hi = min(W, int(np.ceil((x_hi - x_min) / PIXEL_CM)))
        row_lo = max(0, int((y_lo - y_min) / PIXEL_CM))
        row_hi = min(H, int(np.ceil((y_hi - y_min) / PIXEL_CM)))
        if col_lo >= col_hi or row_lo >= row_hi:
            continue
        area = _zone_area(z)
        idx = key_to_idx[_zone_key(z)]
        sub_g = grid[row_lo:row_hi, col_lo:col_hi]
        sub_a = area_grid[row_lo:row_hi, col_lo:col_hi]
        win = area < sub_a
        sub_g[win] = idx
        sub_a[win] = area
    return grid, keys, (x_min, x_max, y_min, y_max)


def fill_gaps(grid: np.ndarray, playable_mask: np.ndarray) -> np.ndarray:
    """Fill empty cells inside *playable_mask* with the label of the nearest filled cell."""
    filled = grid >= 0
    # distance_transform_edt computes nearest non-zero pixel for each zero pixel.
    # We feed it the "filled" mask negated: where there's no zone (foreground=True).
    # Returns indices of nearest "False" pixel (= nearest filled).
    distances, (rows_idx, cols_idx) = distance_transform_edt(
        ~filled, return_indices=True)
    nearest_labels = grid[rows_idx, cols_idx]
    out = grid.copy()
    fill_mask = (~filled) & playable_mask
    out[fill_mask] = nearest_labels[fill_mask]
    return out


# ---------------- polygon extraction ----------------

def extract_polygons(grid: np.ndarray, keys: list[str], extent: tuple) -> dict:
    x_min, x_max, y_min, y_max = extent
    H, W = grid.shape
    xs = x_min + (np.arange(W) + 0.5) * PIXEL_CM
    ys = y_min + (np.arange(H) + 0.5) * PIXEL_CM
    polys_by_key: dict[str, list[list[list[float]]]] = {}
    for i, key in enumerate(keys):
        mask = (grid == i)
        if mask.sum() == 0:
            continue
        z = mask.astype(np.float32)
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
    return polys_by_key


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


def _draw_walls(ax, walls: list[dict], color: str = WALL_COLOR,
                linewidth: float = 2.0, alpha: float = 0.95):
    """Draw each wall as a properly-oriented line segment (uses true yaw)."""
    for w in walls:
        p1, p2 = w["p1"], w["p2"]
        ax.plot([p1[0], p2[0]], [p1[1], p2[1]],
                color=color, linewidth=linewidth, alpha=alpha,
                solid_capstyle="round")


def _draw_perimeter(ax, perim: np.ndarray, color: str = "#ffffff",
                    linewidth: float = 1.0, alpha: float = 0.5):
    ax.plot(perim[:, 0], perim[:, 1], color=color, linewidth=linewidth,
            alpha=alpha, linestyle="--")


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
              interpolation="nearest")
    _draw_perimeter(ax, perim)
    _draw_walls(ax, walls)
    handles = [mpatches.Patch(facecolor=_color_for(k), label=k) for k in keys]
    ax.legend(handles=handles, loc="lower right",
              facecolor="#0e1f33", edgecolor="#2a3e5c", labelcolor="white",
              fontsize=8, framealpha=0.85, ncol=2)
    _setup_axes(ax, f"Subnautica 2 — Region Map (filled, {PIXEL_CM/100:.0f} m/pixel)",
                bounds)
    plt.tight_layout()
    fig.savefig(out_path, dpi=140, facecolor=fig.get_facecolor())
    plt.close(fig)
    logger.info("wrote %s", out_path)


def render_polygons(polys_by_key, bounds, walls, perim, out_path):
    fig, ax = plt.subplots(figsize=_figsize(bounds), dpi=140)
    fig.patch.set_facecolor("#000814")
    for key, polys in polys_by_key.items():
        color = _color_for(key)
        for poly in polys:
            p = np.array(poly)
            ax.fill(p[:, 0], p[:, 1], color=color, alpha=0.65,
                    edgecolor=color, linewidth=1.3)
        biggest = max(polys, key=lambda p: len(p))
        p = np.array(biggest)
        cx, cy = p[:, 0].mean(), p[:, 1].mean()
        label = key.split(".", 1)[-1] if "." in key else key
        txt = ax.text(cx, cy, label, color="white", ha="center", va="center",
                      fontsize=8, fontweight="bold")
        txt.set_path_effects([patheffects.Stroke(linewidth=2.5, foreground="black"),
                              patheffects.Normal()])
    _draw_perimeter(ax, perim)
    _draw_walls(ax, walls)
    _setup_axes(ax, "Subnautica 2 — Region Polygons (filled, with walls)", bounds)
    plt.tight_layout()
    fig.savefig(out_path, dpi=140, facecolor=fig.get_facecolor())
    plt.close(fig)
    logger.info("wrote %s", out_path)


# ---------------- GeoJSON ----------------

def write_polygons_geojson(polys_by_key, out_path):
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


def write_walls_geojson(walls: list[dict], out_path: str):
    features = []
    for w in walls:
        features.append({
            "type": "Feature",
            "properties": {
                "name": w["name"],
                "yaw": w["yaw"],
                "length_cm": w["length_cm"],
            },
            "geometry": {"type": "LineString",
                         "coordinates": [w["p1"], w["p2"]]},
        })
    fc = {"type": "FeatureCollection", "features": features}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(fc, f, indent=2)
    logger.info("wrote %s (%d wall segments)", out_path, len(features))


def write_outline_geojson(perim: np.ndarray, out_path: str):
    ring = [[float(x), float(y)] for x, y in perim]
    fc = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"name": "world_outline"},
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        }],
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(fc, f, indent=2)
    logger.info("wrote %s", out_path)


# ---------------- main ----------------

def main():
    zones = load_zones()
    walls = load_walls()
    bounds = load_bounds()
    logger.info("zones: %d, walls: %d, bounds %.0fm × %.0fm",
                len(zones), len(walls),
                (bounds["x_max"] - bounds["x_min"]) / 100,
                (bounds["y_max"] - bounds["y_min"]) / 100)

    perim = build_perimeter_polygon(walls)
    write_outline_geojson(perim, os.path.join(OUT_DIR, "world_outline.geojson"))
    write_walls_geojson(walls, os.path.join(OUT_DIR, "edge_walls.geojson"))

    grid, keys, extent = paint_zones(zones, bounds)
    playable_mask, _ = perimeter_mask(perim, bounds)
    logger.info("painted: %d cells filled by boxes, playable area: %d cells",
                int((grid >= 0).sum()), int(playable_mask.sum()))

    filled_grid = fill_gaps(grid, playable_mask)
    logger.info("after fill: %d cells filled", int((filled_grid >= 0).sum()))

    render_raster(filled_grid, keys, extent, bounds, walls, perim,
                  os.path.join(OUT_DIR, "zone_filled_raster.png"))

    polys = extract_polygons(filled_grid, keys, extent)
    render_polygons(polys, bounds, walls, perim,
                    os.path.join(OUT_DIR, "zone_filled_overlay.png"))
    write_polygons_geojson(polys, os.path.join(OUT_DIR, "zone_filled_polygons.geojson"))
    with open(os.path.join(OUT_DIR, "zone_filled_polygons.json"), "w",
              encoding="utf-8") as f:
        json.dump(polys, f, indent=2)
    logger.info("wrote zone_filled_polygons.json (%d keys)", len(polys))


if __name__ == "__main__":
    main()
