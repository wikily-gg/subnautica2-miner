"""
Render top-down map PNGs from world_map.json placements.

Output:
  out/map_overview.png   — every placement, colored by category
  out/map_resources.png  — resource nodes/deposits + WorldPop spawns
  out/map_creatures.png  — creature spawns
  out/map_pois.png       — POIs, beacons, lifepods, abandoned bases
  out/map_loot.png       — crates / pickups
  out/map_depth.png      — XY scatter colored by depth (Z)
"""
from __future__ import annotations

import json
import logging
import os
import sys

import matplotlib.pyplot as plt
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ----------------- styling -----------------
# Color per category
CATEGORY_COLORS = {
    "resources": "#39ff14",   # neon green
    "creatures": "#ff3b3b",   # red
    "pois":      "#ffd84d",   # gold
    "volumes":   "#7aa6ff",   # light blue
    "loot":      "#ff8a3b",   # orange
}

# Per-class palette for resources (so we can color by mineral type)
RESOURCE_PALETTE = {
    "Titanium":  "#bdbdbd",
    "Copper":    "#ff8c00",
    "Silver":    "#e6e6e6",
    "Gold":      "#ffd700",
    "Lead":      "#6e6e6e",
    "Lithium":   "#ff9bd1",
    "Quartz":    "#a0ffff",
    "Sulfur":    "#ffee00",
    "Celestine": "#9d4dff",
    "Iron":      "#a06868",
    "Atacamite": "#3eb489",
}


def load_placements() -> dict:
    with open(os.path.join(OUT_DIR, "world_map.json"), encoding="utf-8") as f:
        return json.load(f)


def _filter_xyz(items, clip_play_area: bool = True):
    xs, ys, zs, classes, names = [], [], [], [], []
    for it in items:
        x, y, z = it.get("x"), it.get("y"), it.get("z")
        if x is None or y is None:
            continue
        if clip_play_area and not _in_play_area(it):
            continue
        xs.append(x)
        ys.append(y)
        zs.append(z if z is not None else 0.0)
        classes.append(it.get("class", ""))
        names.append(it.get("name", ""))
    return (np.array(xs), np.array(ys), np.array(zs), classes, names)


def _equal_aspect_bounds(xs, ys, pad=10000, percentile=(5, 99)):
    if len(xs) == 0:
        return -1, 1, -1, 1
    x_lo, x_hi = np.percentile(xs, percentile)
    y_lo, y_hi = np.percentile(ys, percentile)
    return x_lo - pad, x_hi + pad, y_lo - pad, y_hi + pad


# Subnautica 2's main play area in our extracted coords.  Determined
# empirically from the placement density (the bulk sits at high Y).
PLAY_AREA = {
    "x_min": -650000, "x_max": 50000,
    "y_min": 350000,  "y_max": 480000,
    "z_min": -60000,  "z_max": 10000,
}


def _in_play_area(it) -> bool:
    x, y, z = it.get("x"), it.get("y"), it.get("z")
    if x is None or y is None:
        return False
    return (PLAY_AREA["x_min"] <= x <= PLAY_AREA["x_max"]
            and PLAY_AREA["y_min"] <= y <= PLAY_AREA["y_max"])


def _setup_axes(ax, title, xs, ys, dark=True):
    if dark:
        ax.set_facecolor("#08121c")
    ax.set_title(title, color="white" if dark else "black", fontsize=14, pad=14)
    ax.set_xlabel("X (cm)", color="#bbb" if dark else "black", fontsize=9)
    ax.set_ylabel("Y (cm)", color="#bbb" if dark else "black", fontsize=9)
    ax.tick_params(colors="#bbb" if dark else "black", labelsize=7)
    ax.grid(True, alpha=0.15, color="#3a4452", linestyle=":")
    x_min, x_max, y_min, y_max = _equal_aspect_bounds(xs, ys)
    ax.set_xlim(x_min, x_max)
    ax.set_ylim(y_min, y_max)
    ax.set_aspect("equal", adjustable="box")


def _resource_color(cls: str) -> str:
    for mineral, color in RESOURCE_PALETTE.items():
        if mineral in cls:
            return color
    return CATEGORY_COLORS["resources"]


def render_overview(data: dict) -> None:
    fig, ax = plt.subplots(figsize=(20, 12), dpi=120)
    fig.patch.set_facecolor("#000814")

    all_xs, all_ys = [], []
    legend_handles = []
    for cat in ("volumes", "pois", "creatures", "loot", "resources"):
        items = data["placements"].get(cat, [])
        xs, ys, _zs, _cls, _names = _filter_xyz(items)
        if len(xs) == 0:
            continue
        all_xs.append(xs)
        all_ys.append(ys)
        size = {"resources": 4, "creatures": 9, "pois": 18, "volumes": 12, "loot": 16}.get(cat, 6)
        marker = {"pois": "*", "loot": "s", "volumes": "D"}.get(cat, "o")
        scatter = ax.scatter(xs, ys, s=size, c=CATEGORY_COLORS[cat],
                             alpha=0.78, marker=marker, edgecolors="none")
        legend_handles.append(
            plt.scatter([], [], s=max(60, size * 4), c=CATEGORY_COLORS[cat],
                        marker=marker, label=f"{cat} ({len(xs)})")
        )

    if all_xs:
        all_xs = np.concatenate(all_xs)
        all_ys = np.concatenate(all_ys)
    else:
        all_xs = np.array([0])
        all_ys = np.array([0])
    _setup_axes(ax, "Subnautica 2 — Placement Overview", all_xs, all_ys)
    leg = ax.legend(handles=legend_handles, loc="lower right",
                    facecolor="#0e1f33", edgecolor="#2a3e5c", labelcolor="white",
                    fontsize=10, framealpha=0.85)
    plt.tight_layout()
    out = os.path.join(OUT_DIR, "map_overview.png")
    fig.savefig(out, dpi=120, facecolor=fig.get_facecolor())
    plt.close(fig)
    logger.info("wrote %s", out)


def render_resources(data: dict) -> None:
    items = data["placements"]["resources"]
    xs, ys, _zs, classes, _names = _filter_xyz(items)
    fig, ax = plt.subplots(figsize=(20, 12), dpi=120)
    fig.patch.set_facecolor("#000814")
    colors = [_resource_color(c) for c in classes]
    ax.scatter(xs, ys, s=6, c=colors, alpha=0.85, edgecolors="none")
    _setup_axes(ax, f"Subnautica 2 — Resource Spawns ({len(xs)})", xs, ys)
    # legend: one swatch per mineral that's actually present
    minerals_present = sorted({m for m in RESOURCE_PALETTE if any(m in c for c in classes)})
    handles = [plt.scatter([], [], s=80, c=RESOURCE_PALETTE[m], label=m) for m in minerals_present]
    if handles:
        ax.legend(handles=handles, loc="lower right", facecolor="#0e1f33",
                  edgecolor="#2a3e5c", labelcolor="white", fontsize=10, framealpha=0.85)
    plt.tight_layout()
    out = os.path.join(OUT_DIR, "map_resources.png")
    fig.savefig(out, dpi=120, facecolor=fig.get_facecolor())
    plt.close(fig)
    logger.info("wrote %s", out)


def render_creatures(data: dict) -> None:
    items = data["placements"]["creatures"]
    xs, ys, _zs, classes, _names = _filter_xyz(items)
    fig, ax = plt.subplots(figsize=(20, 12), dpi=120)
    fig.patch.set_facecolor("#000814")
    # Color by unique creature class (cycle)
    unique_classes = sorted(set(classes))
    cmap = plt.colormaps.get_cmap("tab20")
    cls_to_color = {c: cmap(i % 20) for i, c in enumerate(unique_classes)}
    colors = [cls_to_color[c] for c in classes]
    ax.scatter(xs, ys, s=14, c=colors, alpha=0.85, edgecolors="none")
    _setup_axes(ax, f"Subnautica 2 — Creature Spawns ({len(xs)})", xs, ys)
    # top 12 species in legend
    from collections import Counter
    top = Counter(classes).most_common(12)
    handles = [plt.scatter([], [], s=80, c=[cls_to_color[c]], label=f"{c} ({n})")
               for c, n in top]
    ax.legend(handles=handles, loc="lower right", facecolor="#0e1f33",
              edgecolor="#2a3e5c", labelcolor="white", fontsize=8, framealpha=0.85)
    plt.tight_layout()
    out = os.path.join(OUT_DIR, "map_creatures.png")
    fig.savefig(out, dpi=120, facecolor=fig.get_facecolor())
    plt.close(fig)
    logger.info("wrote %s", out)


def render_pois(data: dict) -> None:
    items = data["placements"]["pois"] + data["placements"]["loot"]
    xs, ys, _zs, classes, _names = _filter_xyz(items)
    fig, ax = plt.subplots(figsize=(20, 12), dpi=120)
    fig.patch.set_facecolor("#000814")
    is_loot = [("Crate" in c or "Pickup" in c or "Salvage" in c or "Wreck" in c) for c in classes]
    colors = ["#ff8a3b" if l else "#ffd84d" for l in is_loot]
    sizes = [16 if l else 22 for l in is_loot]
    markers = ["s" if l else "*" for l in is_loot]
    # matplotlib can't take a list of markers for one scatter; loop
    for m in ("*", "s"):
        idx = [i for i, mm in enumerate(markers) if mm == m]
        if not idx:
            continue
        ax.scatter(xs[idx], ys[idx], s=[sizes[i] for i in idx],
                   c=[colors[i] for i in idx], alpha=0.85, marker=m,
                   edgecolors="none")
    _setup_axes(ax, f"Subnautica 2 — POIs & Loot ({len(xs)})", xs, ys)
    handles = [
        plt.scatter([], [], marker="*", s=110, c="#ffd84d", label="POI / beacon / lifepod"),
        plt.scatter([], [], marker="s", s=80,  c="#ff8a3b", label="loot / crate"),
    ]
    ax.legend(handles=handles, loc="lower right", facecolor="#0e1f33",
              edgecolor="#2a3e5c", labelcolor="white", fontsize=10, framealpha=0.85)
    plt.tight_layout()
    out = os.path.join(OUT_DIR, "map_pois.png")
    fig.savefig(out, dpi=120, facecolor=fig.get_facecolor())
    plt.close(fig)
    logger.info("wrote %s", out)


def render_depth(data: dict) -> None:
    """Top-down map where points are colored by Z (depth).

    Subnautica's Z is up, so deeper points have lower Z values.
    """
    all_xs, all_ys, all_zs = [], [], []
    for cat in ("resources", "creatures", "pois", "loot"):
        items = data["placements"].get(cat, [])
        xs, ys, zs, _cls, _names = _filter_xyz(items)
        all_xs.extend(xs.tolist())
        all_ys.extend(ys.tolist())
        all_zs.extend(zs.tolist())
    xs = np.array(all_xs); ys = np.array(all_ys); zs = np.array(all_zs)

    fig, ax = plt.subplots(figsize=(20, 12), dpi=120)
    fig.patch.set_facecolor("#000814")
    sc = ax.scatter(xs, ys, c=zs, s=5, cmap="viridis_r", alpha=0.85,
                    edgecolors="none")
    _setup_axes(ax, f"Subnautica 2 — Depth Map ({len(xs)} points)", xs, ys)
    cbar = fig.colorbar(sc, ax=ax, shrink=0.6, pad=0.02)
    cbar.set_label("Z (cm)  ←  deep    shallow  →", color="white", fontsize=9)
    cbar.ax.yaxis.set_tick_params(color="white", labelcolor="white", labelsize=7)
    cbar.outline.set_edgecolor("#2a3e5c")
    plt.tight_layout()
    out = os.path.join(OUT_DIR, "map_depth.png")
    fig.savefig(out, dpi=120, facecolor=fig.get_facecolor())
    plt.close(fig)
    logger.info("wrote %s", out)


def main():
    data = load_placements()
    summary = data.get("summary", {})
    logger.info("total placements: %d  by_category=%s",
                summary.get("total_placements", 0), summary.get("by_category", {}))
    render_overview(data)
    render_resources(data)
    render_creatures(data)
    render_pois(data)
    render_depth(data)


if __name__ == "__main__":
    main()
