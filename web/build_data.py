"""
Build the Leaflet map data bundle from the miner's JSON output.

Reads:
  ../out/world_map.json              placement coordinates (resources / creatures / pois / loot / volumes)
  ../out/world_boundaries.json       playable-area bounds + 58 edge walls
  ../out/world_outline.geojson       closed wall polygon
  ../out/organic_polygons.geojson    12 organic biome polygons
  ../out/zone_filled_polygons.geojson 17 axis-aligned biome polygons (authoritative)
  ../out/regions.json                16 region definitions
  ../out/region_zones.json           204 region boxes (point-in-AABB classifier source)
  ../out/items.json                  329 item types
  ../out/resonatables.json           39 resource deposit definitions w/ drops
  ../out/creature_archetypes.json    51 AI archetypes
  ../out/biomods.json                81 bio-abilities
  ../out/databank.json               505 PDA entries
  ../out/scan_data.json              395 scanner targets
  ../out/recipes.json                ~1100 recipes / build actions
  ../out/pings.json                  33 beacon types
  ../out/locations.json              218 named POIs

Emits into ./data/:
  meta.json          world bounds, region palette, depth range, layer manifest
  markers.geojson    every placement as a feature (depth_m, region, display_name, class)
  biomes.geojson     organic biome polygons (color + key)
  zones.geojson      zone-filled biome polygons (authoritative AABB-based fill)
  walls.geojson      world outline polygon
  regions.json       region list (for legend / filter chips)
  items.json         items lookup
  resonatables.json  resource deposit drop tables
  creatures.json     creature archetypes
  biomods.json       passes through
  databank.json      passes through (large)
  recipes.json       passes through
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
MINER_OUT = os.path.normpath(os.path.join(HERE, "..", "out"))
OUT_DIR = os.path.join(HERE, "data")
ASSETS_DIR = os.path.join(HERE, "assets")

os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(ASSETS_DIR, exist_ok=True)


def _load(name: str):
    with open(os.path.join(MINER_OUT, name), encoding="utf-8") as f:
        return json.load(f)


def _save(name: str, data, indent=None):
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent, default=str)
    size_kb = os.path.getsize(path) // 1024
    print(f"  wrote data/{name:24} {size_kb:>6} kB")
    return path


# ---------- humanize blueprint class names ----------

_RESOURCE_CLEAN = re.compile(r"^BP_(?:Resource(?:Deposit|Node)_|WorldPopSpawned)?(.+?)_C$")
_GENERIC_CLEAN = re.compile(r"^BPC?_(.+?)_C$")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def humanize(cls: str) -> str:
    """Turn 'BP_ResourceDeposit_LithiumPearl_ClamthuluOnly_C' → 'Lithium Pearl (Clamthulu Only)'."""
    if not cls:
        return ""
    m = _RESOURCE_CLEAN.match(cls) or _GENERIC_CLEAN.match(cls)
    core = m.group(1) if m else cls
    core = core.replace("_", " ")
    core = _CAMEL.sub(" ", core).strip()
    return re.sub(r"\s+", " ", core)


# ---------- region classifier (point-in-AABB against 204 zone boxes) ----------

def stitch_terrain_heights(terrain_data, bounds, pixel_cm: int = 500) -> dict | None:
    """Stitch every LandscapeComponent's 63 x 63 height samples into one global
    grid aligned to the playable wall-polygon AABB.

    Each component covers a (62 quad) x (62 quad) tile, with 63 vertex samples per
    side.  The proxy stores RelativeLocation (world cm) and RelativeScale3D
    (cm per quad in X/Y, vertical exaggeration in Z).  Height per pixel:

        raw    = uint16 little-endian out of HeightmapTexture (BGRA R<<8|G)
        local  = (raw - 32768) / 128
        z_cm   = proxy_loc.z + local * proxy_scale.z

    Samples falling outside the playable AABB are dropped.  Cells receiving zero
    samples are filled via nearest-neighbour distance transform (cheap O(N) BFS).
    """
    if not terrain_data:
        return None
    comps = terrain_data.get("components", [])
    if not comps:
        return None

    x_min, x_max = bounds["x_min"], bounds["x_max"]
    y_min, y_max = bounds["y_min"], bounds["y_max"]
    W = int(math.ceil((x_max - x_min) / pixel_cm))
    H = int(math.ceil((y_max - y_min) / pixel_cm))

    z_sum = [0.0] * (W * H)
    z_count = [0] * (W * H)
    samples_dropped = 0
    samples_kept = 0

    for comp in comps:
        sbx, sby = comp["section_base"]
        px, py, pz = comp["proxy_loc"]
        sx, sy, sz = comp["proxy_scale"]
        n = comp["n"]
        raw = bytes.fromhex(comp["heights_hex"])
        for j in range(n):
            wy_base = py + (sby + j) * sy
            row_off = j * n * 2
            for i in range(n):
                idx = row_off + i * 2
                v = raw[idx] | (raw[idx + 1] << 8)  # uint16 LE
                z_cm = pz + (v - 32768) * sz / 128.0
                wx = px + (sbx + i) * sx
                col = int((wx - x_min) / pixel_cm)
                row = int((wy_base - y_min) / pixel_cm)
                if 0 <= col < W and 0 <= row < H:
                    pi = row * W + col
                    z_sum[pi] += z_cm
                    z_count[pi] += 1
                    samples_kept += 1
                else:
                    samples_dropped += 1

    # Build average grid; flag empty cells with sentinel for fill.
    NO_DATA = float("nan")
    z_grid = [NO_DATA] * (W * H)
    filled = 0
    for pi in range(W * H):
        if z_count[pi] > 0:
            z_grid[pi] = z_sum[pi] / z_count[pi]
            filled += 1

    # Leave empty cells as None — the seafloor merge or the final NN-fill in
    # merge_seafloor_into_heightmap handles them.
    z_grid_out = [None] * (W * H)
    for pi in range(W * H):
        if z_count[pi] > 0:
            z_grid_out[pi] = z_sum[pi] / z_count[pi]
    z_grid = z_grid_out

    # Reshape to row-major nested list (bottom-up Y, matching old format).
    z_rows = [z_grid[r * W:(r + 1) * W] for r in range(H)]
    z_vals = [v for v in z_grid if v is not None]
    if z_vals:
        z_lo, z_hi = min(z_vals), max(z_vals)
    else:
        z_lo, z_hi = -1.0, 1.0

    return {
        "pixel_cm": pixel_cm,
        "bounds": {
            "x_min": x_min, "x_max": x_max,
            "y_min": y_min, "y_max": y_max,
            "z_min": z_lo, "z_max": z_hi,
            "width_cm":  x_max - x_min,
            "height_cm": y_max - y_min,
        },
        "shape": [H, W],
        "z_cm": z_rows,
        "samples_kept": samples_kept,
        "samples_dropped": samples_dropped,
        "filled_cells": filled,
        "total_cells": W * H,
        "components": len(comps),
    }


def add_placement_samples(grid: dict, placements: list, bounds: dict) -> dict:
    """Push every placement's Z into the grid as min-Z samples.  Placements live
    at the seafloor (resources / decorations / loot sit on top of it), so their
    Z is a tight lower bound on the floor at that XY."""
    H, W = grid["shape"]
    pixel_cm = grid["pixel_cm"]
    z_rows = grid["z_cm"]
    x_min = bounds["x_min"]; y_min = bounds["y_min"]
    x_max = bounds["x_max"]; y_max = bounds["y_max"]
    added = 0
    for p in placements:
        x, y, z = p.get("x"), p.get("y"), p.get("z")
        if x is None or y is None or z is None:
            continue
        if not (x_min <= x < x_max and y_min <= y < y_max):
            continue
        c = int((x - x_min) / pixel_cm)
        r = int((y - y_min) / pixel_cm)
        if 0 <= r < H and 0 <= c < W:
            cur = z_rows[r][c]
            if cur is None or z < cur:
                z_rows[r][c] = z
                added += 1
    return added


def gaussian_blur_grid(z_rows: list, sigma_px: float = 2.5) -> list:
    """In-place separable Gaussian blur on a 2D Python list grid.

    Smooths the discrete NN-fill regions into a continuous gradient.  Operates on
    floats only; assumes the caller already filled None cells.
    """
    if sigma_px <= 0:
        return z_rows
    radius = max(1, int(math.ceil(sigma_px * 3)))
    # Build 1D Gaussian kernel.
    kernel = []
    s2 = 2.0 * sigma_px * sigma_px
    for i in range(-radius, radius + 1):
        kernel.append(math.exp(-(i * i) / s2))
    ksum = sum(kernel)
    kernel = [k / ksum for k in kernel]
    H = len(z_rows); W = len(z_rows[0])
    # Horizontal pass.
    tmp = [row[:] for row in z_rows]
    for r in range(H):
        for c in range(W):
            acc = 0.0
            for ki, k in enumerate(kernel):
                cc = c + (ki - radius)
                if cc < 0: cc = 0
                elif cc >= W: cc = W - 1
                acc += z_rows[r][cc] * k
            tmp[r][c] = acc
    # Vertical pass.
    out = [row[:] for row in tmp]
    for r in range(H):
        for c in range(W):
            acc = 0.0
            for ki, k in enumerate(kernel):
                rr = r + (ki - radius)
                if rr < 0: rr = 0
                elif rr >= H: rr = H - 1
                acc += tmp[rr][c] * k
            out[r][c] = acc
    return out


def merge_seafloor_into_heightmap(landscape_grid: dict | None, seafloor_grid: dict) -> dict:
    """Merge seafloor-mesh min-Z into the landscape grid.

    Where landscape has direct data we keep it (true surface terrain).  Where it
    doesn't, we use seafloor min-Z.  Empty cells in both fall back to nearest-
    neighbour from any source.
    """
    sf_z = seafloor_grid["z_cm"]
    sf_shape = tuple(seafloor_grid["shape"])
    sf_pixel = int(seafloor_grid["pixel_cm"])

    if landscape_grid is None:
        # Build a fresh grid from seafloor only.
        H, W = sf_shape
        z_flat = [None] * (W * H)
        for r in range(H):
            row = sf_z[r]
            for c in range(W):
                v = row[c]
                if v is not None:
                    z_flat[r * W + c] = v
        bounds = dict(seafloor_grid["bounds"])
        filled = sum(1 for v in z_flat if v is not None)
    else:
        # Combine.  Landscape grid uses doubles already.
        ls_z = landscape_grid["z_cm"]
        H, W = landscape_grid["shape"]
        assert (H, W) == sf_shape, "grid shapes must match"
        # Mark which landscape cells came from real samples (not BFS fill).
        # We pessimistically rebuild from scratch, taking landscape data where its
        # samples_kept covers it... but build_data.stitch already BFS-filled it.
        # Better approach: prefer seafloor where its Z is DEEPER than landscape (the
        # landscape data above sea or shallow gets used).  In Subnautica's case the
        # seafloor min-Z is below the landscape sample for non-shallow regions.
        z_flat = [None] * (W * H)
        for r in range(H):
            sf_row = sf_z[r]
            ls_row = ls_z[r]
            for c in range(W):
                ls_v = ls_row[c]
                sf_v = sf_row[c]
                pi = r * W + c
                # If seafloor exists, prefer the deeper of the two (seafloor wins
                # everywhere there's no landscape; in overlap zones landscape wins
                # because its samples are real surface terrain not just lowest hull).
                if ls_v is not None and sf_v is not None:
                    # Take landscape if it's shallower (= the surface terrain);
                    # take seafloor if it's deeper (= the actual floor).
                    z_flat[pi] = min(ls_v, sf_v)
                elif ls_v is not None:
                    z_flat[pi] = ls_v
                elif sf_v is not None:
                    z_flat[pi] = sf_v
        filled = sum(1 for v in z_flat if v is not None)
        bounds = dict(landscape_grid["bounds"])

    z_rows = [z_flat[r * W:(r + 1) * W] for r in range(H)]
    vals = [v for v in z_flat if v is not None]
    bounds["z_min"] = min(vals) if vals else 0
    bounds["z_max"] = max(vals) if vals else 0

    return {
        "pixel_cm": sf_pixel,
        "bounds": bounds,
        "shape": [H, W],
        "z_cm": z_rows,
        "filled_cells": filled,
        "total_cells": W * H,
        "samples_kept": filled,  # for log line compatibility
        "components": landscape_grid.get("components", 0) if landscape_grid else 0,
    }


def fill_and_smooth_grid(grid: dict, sigma_px: float = 6.0) -> dict:
    """Gaussian KDE-style fill.

    Each direct sample (cell whose Z is not None) gets weight 1; we convolve the
    sample grid AND the weight grid with the same separable Gaussian, then
    divide.  Cells far from any sample still get a value from the diffused
    weights, with a smooth gradient between samples.  This avoids the flat NN-
    plateaus that an interior-fill-then-blur produces.
    """
    z_rows = grid["z_cm"]
    H = len(z_rows); W = len(z_rows[0])
    wz = [0.0] * (W * H)
    w  = [0.0] * (W * H)
    direct = 0
    for r in range(H):
        row = z_rows[r]
        for c in range(W):
            v = row[c]
            if v is not None:
                pi = r * W + c
                wz[pi] = float(v)
                w[pi]  = 1.0
                direct += 1

    def _blur1d(values, sigma):
        radius = max(2, int(math.ceil(sigma * 3)))
        s2 = 2.0 * sigma * sigma
        kernel = [math.exp(-(i * i) / s2) for i in range(-radius, radius + 1)]
        # Horizontal pass.
        tmp = [0.0] * (W * H)
        for r in range(H):
            row_off = r * W
            for c in range(W):
                acc = 0.0
                for ki, k in enumerate(kernel):
                    cc = c + (ki - radius)
                    if cc < 0: cc = 0
                    elif cc >= W: cc = W - 1
                    acc += values[row_off + cc] * k
                tmp[row_off + c] = acc
        # Vertical pass.
        out = [0.0] * (W * H)
        for r in range(H):
            row_off = r * W
            for c in range(W):
                acc = 0.0
                for ki, k in enumerate(kernel):
                    rr = r + (ki - radius)
                    if rr < 0: rr = 0
                    elif rr >= H: rr = H - 1
                    acc += tmp[rr * W + c] * k
                out[row_off + c] = acc
        return out

    wz = _blur1d(wz, sigma_px)
    w  = _blur1d(w,  sigma_px)

    # Cells with effectively zero weight (extreme corners): widen with a bigger
    # blur on what we have.
    if min(w) < 1e-9:
        wz = _blur1d(wz, sigma_px * 4)
        w  = _blur1d(w,  sigma_px * 4)

    z_rows_out = []
    vmin =  math.inf; vmax = -math.inf
    for r in range(H):
        row = []
        for c in range(W):
            pi = r * W + c
            ww = w[pi]
            v = (wz[pi] / ww) if ww > 1e-9 else 0.0
            row.append(v)
            if v < vmin: vmin = v
            if v > vmax: vmax = v
        z_rows_out.append(row)

    grid["z_cm"] = z_rows_out
    grid["bounds"]["z_min"] = vmin if math.isfinite(vmin) else 0
    grid["bounds"]["z_max"] = vmax if math.isfinite(vmax) else 0
    grid["filled_cells"] = direct
    grid["total_cells"] = W * H
    return grid


def build_heightmap_from_points(points: list, bounds: dict,
                                outline_poly: list,
                                pixel_cm: int = 500) -> dict | None:
    """Build a heightmap from biome_points_v2 min-Z per pixel.

    Pipeline:
      1. World-bounds AABB sets grid dimensions.
      2. Each playable point lowers its target cell's Z (min-Z aggregation).
      3. Cells outside the wall polygon are kept as None (no fill).
      4. Cells inside the polygon but without samples are NN-filled via a
         multi-source BFS (Euclidean-ish — same idea as scipy's distance
         transform but pure Python and bounded to the playable mask).
      5. No Gaussian smoothing.
    """
    if not points:
        return None
    x_min, x_max = bounds["x_min"], bounds["x_max"]
    y_min, y_max = bounds["y_min"], bounds["y_max"]
    W = int(math.ceil((x_max - x_min) / pixel_cm))
    H = int(math.ceil((y_max - y_min) / pixel_cm))

    # 1. Build playable-mask via point-in-polygon test on cell centres.
    mask = [False] * (W * H)
    for r in range(H):
        wy = y_min + (r + 0.5) * pixel_cm
        for c in range(W):
            wx = x_min + (c + 0.5) * pixel_cm
            if point_in_polygon(wx, wy, outline_poly):
                mask[r * W + c] = True
    playable = sum(1 for m in mask if m)

    # 2. Min-Z aggregation.
    z = [None] * (W * H)
    for p in points:
        x, y, pz = p.get("x"), p.get("y"), p.get("z")
        if x is None or y is None or pz is None:
            continue
        if not (x_min <= x < x_max and y_min <= y < y_max):
            continue
        c = int((x - x_min) / pixel_cm)
        r = int((y - y_min) / pixel_cm)
        pi = r * W + c
        if not mask[pi]:
            continue
        cur = z[pi]
        if cur is None or pz < cur:
            z[pi] = pz
    direct = sum(1 for v in z if v is not None)

    # 3. NN-fill any playable-but-empty cells via multi-source BFS bounded to
    #    the polygon mask.  Cells outside the polygon stay None.
    from collections import deque
    q = deque()
    src = [-1] * (W * H)
    for pi in range(W * H):
        if z[pi] is not None:
            src[pi] = pi
            q.append(pi)
    while q:
        pi = q.popleft()
        r = pi // W; c = pi % W
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            rr, cc = r + dr, c + dc
            if 0 <= rr < H and 0 <= cc < W:
                npi = rr * W + cc
                if not mask[npi]:
                    continue
                if src[npi] == -1:
                    src[npi] = src[pi]
                    z[npi] = z[src[pi]]
                    q.append(npi)

    z_rows = [z[r * W:(r + 1) * W] for r in range(H)]
    vals = [v for v in z if v is not None]
    if not vals:
        return None
    return {
        "pixel_cm": pixel_cm,
        "bounds": {
            "x_min": x_min, "x_max": x_max, "y_min": y_min, "y_max": y_max,
            "z_min": min(vals), "z_max": max(vals),
            "width_cm":  x_max - x_min,
            "height_cm": y_max - y_min,
        },
        "shape": [H, W],
        "z_cm": z_rows,
        "direct_cells": direct,
        "playable_cells": playable,
    }


def point_in_polygon(x, y, poly):
    """Ray-casting point-in-polygon test. poly: list of [x, y] vertices."""
    n = len(poly); inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]; xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def build_region_classifier(zones):
    boxes = []
    for z in zones:
        cx, cy, cz = z["center"]
        ex, ey, ez = z["extent"]
        boxes.append((cx - ex, cy - ey, cz - ez, cx + ex, cy + ey, cz + ez,
                      z.get("biome"), z.get("sub_region"), z.get("display_name")))

    def classify(x, y, z):
        if x is None or y is None:
            return (None, None, None)
        z_lo = (z if z is not None else -1e9)
        for x0, y0, z0, x1, y1, z1, biome, sub, name in boxes:
            if x0 <= x <= x1 and y0 <= y <= y1 and z0 <= z_lo <= z1:
                return (biome, sub, name)
        # XY-only fallback when Z is missing or outside (often above seafloor by a little)
        for x0, y0, z0, x1, y1, z1, biome, sub, name in boxes:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return (biome, sub, name)
        return (None, None, None)

    return classify


# ---------- per-category visual config ----------

CATEGORIES = ("resources", "loot", "pois", "creatures", "caves", "volumes")

CATEGORY_LABELS = {
    "resources": "Resources",
    "creatures": "Creatures & Fauna",
    "pois":      "Points of Interest",
    "loot":      "Loot & Wrecks",
    "caves":     "Caves",
    "volumes":   "Volumes (debug)",
}

# class substring → ("group", "color")
RESOURCE_GROUP = {
    "Titanium":         ("Titanium",    "#bdbdbd"),
    "Copper":           ("Copper",      "#ff8c00"),
    "Silver":           ("Silver",      "#e6e6e6"),
    "Gold":             ("Gold",        "#ffd700"),
    "Lead":             ("Lead",        "#6e6e6e"),
    "LithiumPearl":     ("Lithium Pearl", "#ff5fb0"),
    "Lithium":          ("Lithium",     "#ff9bd1"),
    "Quartz":           ("Quartz",      "#a0ffff"),
    "Sulfur":           ("Sulfur",      "#ffee00"),
    "Celestine":        ("Celestine",   "#9d4dff"),
    "Iron":             ("Iron",        "#a06868"),
    "Atacamite":        ("Atacamite",   "#3eb489"),
    "Salt":             ("Salt",        "#ffffff"),
    "Fulgurite":        ("Fulgurite",   "#7ad6ff"),
    "Greenshine":       ("Greenshine",  "#a4ff4d"),
    "NeedleShark":      ("Needle Shark","#cc8866"),
    "DeepRoot":         ("Deep Root",   "#84592a"),
}


def resource_group(cls: str):
    for tag, info in RESOURCE_GROUP.items():
        if tag in cls:
            return info
    if "WaterSlug" in cls:
        return ("Water Slug", "#73f7ff")
    return ("Other Resource", "#39ff14")


# NOTE: dict ordering matters — first matching substring wins. Specific
# tags must come before generic ones (e.g. Tadpole_HAUL before Tadpole).
POI_GROUP = {
    "Beacon":             ("Beacon",          "#ffe066"),
    "Lifepod":            ("Lifepod",         "#ff7fbb"),
    "BioBed":             ("Lifepod",         "#ff7fbb"),
    "AbandonedBase":      ("Abandoned Base",  "#ffb74d"),
    "Tadpole_HAUL":       ("HAUL Fragment",   "#ff5e57"),
    "Tadpole_ScoutRay":   ("Scout-Ray Fragment", "#9cd5ff"),
    "Tadpole":            ("Tadpole Fragment","#9cffd0"),
    "Cicada":             ("Cicada Wreck",    "#9cd5ff"),
    "UpgradeTerminal":    ("Upgrade Terminal","#ffcc66"),
    "PowerCellTerminal":  ("Power Cell",      "#ffd84d"),
    "PowerTerminalSlot":  ("Power Cell",      "#ffd84d"),
    "BasicBatteryTerminal":("Battery",        "#bfae72"),
    "Computer":           ("Terminal",        "#9ad0ff"),
    "DiveElevator":       ("Dive Elevator",   "#caa8ff"),
    "MetalSalvage":       ("Metal Salvage",   "#cccccc"),
    "Blockout_Crate":     ("Crate",           "#e58f3e"),
    "LightStick":         ("Light Stick",     "#ffffaa"),
    "AbandonedBaseSign":  ("Base Sign",       "#ffb74d"),
}


# Matches `BP_<Name>_Scannable_C`, `BP_<Name>_Scan_C` (older convention
# used by Flashlight, WakeMaker, SonicResonator, ThermalPlantHead/Body,
# PowerGridCapacitor, Axum_Drum), `BP_<Name>_Fragment_C`,
# `BP_<Name>_Fragment_<NN>_C`, and `BP_<Name>FragmentA_Scan_C`.
# Group `(1)` is the raw scannable name we humanize for the marker
# group label and slugify for the item_slug link. The longer
# `_Scannable` alternative is listed first to avoid the `_Scan`
# alternative shadowing it - both end with `_C$` so the engine's
# anchor check rejects the wrong one anyway, but listing length-
# desc keeps the intent clear.
_SCANNABLE_CLEAN = re.compile(
    r"^BP_(.+?)(?:_Scannable|_Fragment(?:_\d+)?|FragmentA_Scan|FragmentB_Scan|_Scan)_C$"
)


def scannable_info(cls: str):
    """If `cls` is a Scannable/Fragment placement, return its
    (group_label, marker_color, item_slug) tuple. Otherwise None.

    Group label uses the "<Item Name> Fragment" pattern so the wiki
    can detect them with a simple `/ Fragment$/` regex.  Slug is the
    kebab-case form of the item name so the item detail page can
    look up its own placements by `item_slug == item.urlSlug`.
    """
    m = _SCANNABLE_CLEAN.match(cls or "")
    if not m:
        return None
    raw = m.group(1)
    spaced = re.sub(r"_+", " ", raw).strip()
    spaced = _CAMEL.sub(" ", spaced).strip()
    spaced = re.sub(r"\s+", " ", spaced)
    label = f"{spaced} Fragment" if spaced else "Fragment"
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", spaced).strip("-").lower()
    return (label, "#bda4ff", slug)


def is_scannable(cls: str) -> bool:
    return scannable_info(cls) is not None


def poi_group(cls: str):
    for tag, info in POI_GROUP.items():
        if tag in cls:
            return info
    # Generic fragment / scannable fallback. Each gets its own
    # "<Item Name> Fragment" group so the wiki can list them under
    # Blueprint Sources and per-item pages can pin their spawn map.
    sc = scannable_info(cls)
    if sc:
        return (sc[0], sc[1])
    return ("POI", "#ffd84d")


# Static scannable / wreck actors that UE filed under the world_map
# "creatures" bucket but that we want shown on the Landmarks (POI) tab.
# Anything matching the `_Scannable_C` / `_Fragment_C` pattern is
# promoted dynamically by `is_scannable()`; this tuple is kept for the
# few non-pattern-matching legacy classes that pre-dated the rule.
CREATURE_AS_POI_TAGS = (
    "Tadpole_HAUL_Fragment",
    "Tadpole_ScoutRay_Fragment",
    "Tadpole_Fragment",
    "DiveElevator",
)


CREATURE_GROUP_DEFAULT = ("Creature", "#ff5e57")
LOOT_GROUP_DEFAULT = ("Loot", "#ff8a3b")
VOLUME_GROUP_DEFAULT = ("Volume", "#7aa6ff")


# Order matters: more specific substrings first.
LOOT_GROUP = (
    ("Crate_Blighted", ("Blighted Crate",         "#a43c4f")),
    ("Blockout_Crate", ("Crate",                  "#e58f3e")),
    ("MetalSalvage",   ("Metal Salvage",          "#cccccc")),
    ("OR_Wreck",       ("Overgrown Ruins Wreck",  "#9cd5ff")),
    ("Cicada_Wreck",   ("Cicada Wreck",           "#9cd5ff")),
)


def loot_group(cls: str):
    for tag, info in LOOT_GROUP:
        if tag in cls:
            return info
    return LOOT_GROUP_DEFAULT

# Map BP / BPC class names to clean creature display names.  Real creatures
# spawn dynamically; what's placed in the world is a mix of WorldPopProxy
# anchors (where dynamic spawns land), unique creatures, and decorative meshes
# (anemones, feeler trees, fish-school particle anchors).  We surface them as
# best we can using string-table names from ST_Databank_Motile + the few
# extras the game uses for hazard / flora actors.
#
# Each entry maps (class-substring -> (display group, colour)).
CREATURE_DISPLAY = [
    # Order matters: more specific substrings first.
    ("anemonecrabdark",     ("Anemone Crab (Dark)",     "#5e3b8a")),
    ("anemonecrab",         ("Anemone Crab",            "#a16ad6")),
    ("waterslugworldpop",   ("Water Slug",              "#73f7ff")),
    ("waterslug",           ("Water Slug",              "#73f7ff")),
    ("collectorleviathan",  ("Collector Leviathan",     "#ff3b3b")),
    ("hidingspotleviathan", ("Leviathan (hiding spot)", "#ff5e57")),
    ("mirrorhalfmoon",      ("Mirror Halfmoon school",  "#9cd5ff")),
    ("halfmoon_variant02",  ("Halfmoon (var. 2)",       "#7fbfff")),
    ("halfmoon_variant01",  ("Halfmoon (var. 1)",       "#a3d9ff")),
    ("halfmoon",            ("Halfmoon",                "#9cd5ff")),
    ("pelagicghost",        ("Pelagic Ghost school",    "#cdb4ff")),
    ("fluttertail",         ("Flutter Tail school",     "#a3e7ff")),
    ("acidanemone",         ("Acid Anemone (hazard)",   "#9aff8a")),
    ("ah_anemone",          ("Anemone (flora)",         "#ffd24a")),
    ("feelertree",          ("Feeler Tree (flora)",     "#84592a")),
    ("crabfeces",           ("Crab feces",              "#806753")),
    ("clamthulu",           ("Clamthulu",               "#c084fc")),
    ("tadpole_haul",        ("HAUL Tadpole wreck",      "#ff5e57")),
    ("tadpole_scoutray",    ("Scout-Ray wreck",         "#9cffd0")),
    ("tadpole_fragment",    ("Tadpole wreck",           "#9cffd0")),
    ("diveelevator",        ("Dive Elevator",           "#caa8ff")),
    ("electricgeordie",     ("Electric Geordie",        "#34d399")),
    ("geordie",             ("Geordie",                 "#80ed99")),
    ("spineytail_variant",  ("Spiney Tail (var.)",      "#ffa1a1")),
    ("spineytail",          ("Spiney Tail",             "#ff6b6b")),
    ("surgejelly",          ("Surge Jelly",             "#cdb4db")),
    ("jellyring_static",    ("Jelly Ring (static)",     "#7aaedf")),
    ("jellyring",           ("Jelly Ring",              "#a3cef1")),
    ("bullethead",          ("Bullethead",              "#ffd360")),
    ("coralcrab",           ("Coral Crab",              "#f4a261")),
    ("houndgar",            ("Houndgar",                "#f28482")),
    ("marrowbreach",        ("Marrowbreach",            "#e63946")),
    ("hammerhead",          ("Hammerhead",              "#ffb703")),
    ("greatjaw",            ("Great Jaw",               "#fb8500")),
    ("flashfish",           ("Flash Fish",              "#fff066")),
    ("foureye",             ("Four-Eye",                "#bde0fe")),
    ("pneumo",              ("Pneumo",                  "#a0d8e0")),
    ("cerathecan",          ("Cerathecan",              "#c4d39c")),
    ("sandspear_juvenile",  ("Sandspear (juvenile)",    "#dcc9a1")),
    ("sandspear",           ("Sandspear",               "#c9a96b")),
    ("epicureansymbiote",   ("Epicurean Symbiote",      "#b6e1c4")),
    ("epicurean",           ("Epicurean",               "#90ddb0")),
    ("twineel",             ("Twin Eel",                "#ad6dde")),
    ("nibblershark",        ("Nibbler Shark",           "#ffafcc")),
    ("needlershark_giant",  ("Needler Shark (giant)",   "#ff6e9c")),
    ("needlershark",        ("Needler Shark",           "#ff8fab")),
    ("nibbler",             ("Nibbler",                 "#ffafcc")),
    ("jetocaris",           ("Jetocaris",               "#bde0fe")),
    ("waxmoon",             ("Waxmoon",                 "#ffe066")),
    ("bluemoon",            ("Bluemoon",                "#90e0ef")),
    ("harvestmoon",         ("Harvestmoon",             "#ffd6a5")),
    ("heatmoon",            ("Heatmoon",                "#ff8fab")),
    ("vepssensor",          ("Veps Sensor",             "#bdb2ff")),
    ("vepsdefender",        ("Veps Defender",           "#a0c4ff")),
    ("bfj",                 ("BFJ Leviathan",           "#7c3aed")),
]


def creature_display(cls: str):
    lc = (cls or "").lower()
    for tag, info in CREATURE_DISPLAY:
        if tag in lc:
            return info
    # Fallback: strip BP_ / BPC_, _C, drop common qualifiers, humanise.
    core = humanize(cls)
    for noise in ("World Pop Proxy", "Skeletal Mesh", "Static Mesh", "Boid",
                  "BOID", "Proxy", "Scannable", "Component"):
        core = core.replace(noise, "").strip()
    return (core or "Creature", "#ff5e57")

# Cave name pattern -> group / colour. Keep small and meaningful.
def cave_group_from_name(name: str):
    n = (name or "").lower()
    if "deepcave" in n or "deep_cave" in n:
        return ("Deep Cave", "#5f3dc4")
    if "spiralcave" in n or "spiral_cave" in n:
        return ("Spiral Cave", "#9775fa")
    if "longcave" in n or "long_cave" in n:
        return ("Long Cave", "#b197fc")
    if "tunnel" in n:
        return ("Tunnel", "#7048e8")
    if "grotto" in n:
        return ("Grotto", "#c084fc")
    if "_cave_" in n or n.endswith("_cave"):
        return ("Cave Entrance", "#845ef7")
    return ("Cave", "#a78bfa")


# ---------- main pipeline ----------

def build():
    print("loading miner output...")
    world_map = _load("world_map.json")
    boundaries = _load("world_boundaries.json")
    bounds = boundaries["bounds"]

    organic = _load("organic_polygons.geojson")
    zones_geo = _load("zone_filled_polygons.geojson")
    outline = _load("world_outline.geojson")
    outline_poly = outline["features"][0]["geometry"]["coordinates"][0]

    # Heightmap: built right here from biome_points_v2 (min-Z per cell),
    # NN-filled inside the wall polygon, no Gaussian blur.  Cells outside the
    # wall polygon are kept as None so the GridLayer can render them transparent.
    try:
        biome_points = _load("biome_points_v2.json")["points"]
    except (FileNotFoundError, KeyError):
        biome_points = []
    heightmap = build_heightmap_from_points(
        biome_points, bounds, outline_poly,
        pixel_cm=500,
    )
    if heightmap is not None:
        b = heightmap["bounds"]
        print(f"  built heightmap            -> "
              f"{heightmap['shape'][0]} x {heightmap['shape'][1]}, "
              f"{heightmap['direct_cells']}/{heightmap['playable_cells']} cells direct, "
              f"Z range {b['z_min']/100:.0f} m -> {b['z_max']/100:.0f} m")

    regions = _load("regions.json")
    region_zones = _load("region_zones.json")
    items = _load("items.json")
    reso = _load("resonatables.json")
    archetypes = _load("creature_archetypes.json")
    biomods = _load("biomods.json")
    databank = _load("databank.json")
    recipes = _load("recipes.json")

    # Optional lookups (best-effort)
    try:
        scan_data = _load("scan_data.json")
    except FileNotFoundError:
        scan_data = []
    try:
        pings = _load("pings.json")
    except FileNotFoundError:
        pings = []
    try:
        locations = _load("locations.json")
    except FileNotFoundError:
        locations = []
    try:
        characters = _load("characters.json")
    except FileNotFoundError:
        characters = []
    try:
        creature_spawns = _load("creature_spawns.json")["spawns"]
    except (FileNotFoundError, KeyError):
        creature_spawns = []

    # actor_class → item (for resource ID lookups in popups)
    item_by_actor = {}
    for it in items:
        ac = (it.get("actor_class") or "").lower()
        if ac:
            item_by_actor[ac] = it
    item_by_id = {it["id"]: it for it in items}

    # resonatable lookup by name normalized to group key (best effort)
    reso_by_name = {r["name"]: r for r in reso if r.get("name")}

    classify = build_region_classifier(region_zones)

    # ---------- build placement features ----------
    features = []
    group_counts = Counter()
    class_counts = Counter()
    by_category_count = Counter()

    skipped_outside = 0
    skipped_outside_wall = 0
    bbox = bounds
    x_min, x_max = bbox["x_min"], bbox["x_max"]
    y_min, y_max = bbox["y_min"], bbox["y_max"]

    feature_id = 0
    for cat in CATEGORIES:
        if cat == "creatures":
            # Mobile creatures come from creature_spawns.json (seeded spawn
            # data) below. The world_map creatures bucket is mostly spawn
            # proxies / decorative actors which we skip — but it also holds
            # static scannable wreck/fragment actors (BP_Tadpole_Fragment_*,
            # BP_DiveElevator_Scannable). Surface those in the POIs category
            # so they show up on the Landmarks tab.
            items_in_cat = [
                p for p in world_map["placements"].get("creatures", [])
                if (any(t in (p.get("class") or "") for t in CREATURE_AS_POI_TAGS)
                    or is_scannable(p.get("class") or ""))
            ]
            effective_cat = "pois"
        else:
            items_in_cat = world_map["placements"].get(cat, [])
            effective_cat = cat
        for p in items_in_cat:
            x, y, z = p.get("x"), p.get("y"), p.get("z")
            if x is None or y is None:
                continue
            if not (x_min - 1000 <= x <= x_max + 1000 and y_min - 1000 <= y <= y_max + 1000):
                skipped_outside += 1
                continue
            # Tighter filter: point must be inside the 117-vertex wall polygon
            # (the AABB has gaps where the wall is irregular).
            if not point_in_polygon(x, y, outline_poly):
                skipped_outside_wall += 1
                continue
            cls = p.get("class") or ""
            class_counts[cls] += 1
            by_category_count[effective_cat] += 1

            if effective_cat == "resources":
                group, color = resource_group(cls)
                name = humanize(cls) or group
            elif effective_cat == "pois":
                group, color = poi_group(cls)
                name = humanize(cls) or group
            elif effective_cat == "loot":
                group, color = loot_group(cls)
                name = humanize(cls) or group
            elif effective_cat == "caves":
                group = "Cave Prefab"
                color = "#a78bfa"
                name = humanize(cls)
            else:  # volumes
                group, color = VOLUME_GROUP_DEFAULT
                name = humanize(cls)

            biome, sub, region_name = classify(x, y, z)
            depth_m = round((-z) / 100.0, 1) if z is not None else None  # UE Z up; depth below surface

            group_counts[(effective_cat, group)] += 1

            # Fragment/Scannable markers carry an `item_slug` so the
            # wiki's per-item detail page can query its own spawn
            # locations without round-tripping the BP class name.
            sc_info = scannable_info(cls)
            item_slug = sc_info[2] if sc_info else None

            feature_id += 1
            props = {
                "cat": effective_cat,
                "group": group,
                "color": color,
                "class": cls,
                "name": name,
                "depth_m": depth_m,
                "z": z,
                "biome": biome,
                "sub_region": sub,
                "region": region_name,
            }
            if item_slug:
                props["item_slug"] = item_slug
            features.append({
                "type": "Feature",
                "id": feature_id,
                "geometry": {
                    "type": "Point",
                    # Leaflet CRS.Simple: latlng = (Y, X) ; we keep raw UE cm.
                    "coordinates": [x, y],
                },
                "properties": props,
            })

    # ---------- named cave entrances + dive locations from locations.json ----------
    cave_loc_added = 0
    poi_loc_added = 0
    for loc in locations:
        xyz = loc.get("location")
        if not xyz:
            continue
        x, y, z = xyz[0], xyz[1], xyz[2]
        if not (x_min - 1000 <= x <= x_max + 1000 and y_min - 1000 <= y <= y_max + 1000):
            continue
        if not point_in_polygon(x, y, outline_poly):
            continue

        nm = loc.get("name") or loc.get("id") or "Location"
        is_cave = any(tok in nm.lower() for tok in ("cave", "cavern", "tunnel", "grotto"))
        cat = "caves" if is_cave else "pois"
        if is_cave:
            group, color = cave_group_from_name(nm)
        else:
            group, color = "Dive Location", "#ffcc66"

        biome, sub, region_name = classify(x, y, z)
        depth_m = round((-z) / 100.0, 1) if z is not None else None
        feature_id += 1
        # Clean up display name: strip prefixes like AxumRuins_ObservatoryIsland_
        display = nm.replace("_", " ")
        features.append({
            "type": "Feature",
            "id": feature_id,
            "geometry": {"type": "Point", "coordinates": [x, y]},
            "properties": {
                "cat": cat,
                "group": group,
                "color": color,
                "class": "DA_Location",
                "name": display,
                "depth_m": depth_m,
                "z": z,
                "biome": biome,
                "sub_region": sub,
                "region": region_name,
                "loc_id": loc.get("id"),
                "image": loc.get("image"),
            },
        })
        group_counts[(cat, group)] += 1
        by_category_count[cat] += 1
        if is_cave:
            cave_loc_added += 1
        else:
            poi_loc_added += 1

    # ---------- real creature spawns (PCG-seeded SeededCreatureData) ----------
    cs_added = 0
    cs_skipped = 0
    for s in creature_spawns:
        x, y, z = s.get("x"), s.get("y"), s.get("z")
        if x is None or y is None:
            cs_skipped += 1
            continue
        if not (x_min - 1000 <= x <= x_max + 1000 and y_min - 1000 <= y <= y_max + 1000):
            cs_skipped += 1
            continue
        if not point_in_polygon(x, y, outline_poly):
            cs_skipped += 1
            continue
        cls = s.get("class") or ""
        group, color = creature_display(cls)
        biome, sub, region_name = classify(x, y, z)
        depth_m = round((-z) / 100.0, 1) if z is not None else None
        feature_id += 1
        features.append({
            "type": "Feature",
            "id": feature_id,
            "geometry": {"type": "Point", "coordinates": [x, y]},
            "properties": {
                "cat": "creatures",
                "group": group,
                "color": color,
                "class": cls,
                "name": group,
                "depth_m": depth_m,
                "z": z,
                "biome": biome,
                "sub_region": sub,
                "region": region_name,
                "hand_placed": s.get("hand_placed", False),
                "persistent": s.get("persistent", False),
            },
        })
        group_counts[("creatures", group)] += 1
        by_category_count["creatures"] += 1
        cs_added += 1

    geo = {"type": "FeatureCollection", "features": features}
    print(f"  built {len(features)} placement features "
          f"(skipped {skipped_outside} outside AABB, "
          f"{skipped_outside_wall} outside wall polygon, "
          f"+{cave_loc_added} named caves, +{poi_loc_added} named dive locations, "
          f"+{cs_added} real creature spawns, {cs_skipped} creature spawns skipped)")

    # ---------- group/legend metadata ----------
    legend = {}
    for cat in CATEGORIES:
        groups = {}
        for (c, g), n in group_counts.items():
            if c != cat:
                continue
            # find a color from any feature with this group
            color = next((f["properties"]["color"] for f in features
                          if f["properties"]["cat"] == c and f["properties"]["group"] == g), "#ffffff")
            groups[g] = {"color": color, "count": n}
        legend[cat] = {
            "label": CATEGORY_LABELS[cat],
            "count": by_category_count[cat],
            "groups": dict(sorted(groups.items(), key=lambda kv: -kv[1]["count"])),
        }

    # ---------- region palette (matches render_heightmap) ----------
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
    region_meta = []
    seen = set()
    for r in regions:
        key = f"{r['biome']}.{r['sub_region']}"
        if key in seen:
            continue
        seen.add(key)
        region_meta.append({
            "key": key,
            "biome": r["biome"],
            "sub_region": r["sub_region"],
            "display_name": r["display_name"],
            "color": SUBREGION_COLORS.get(key, "#888888"),
        })
    region_meta.sort(key=lambda r: r["key"])

    # ---------- depth analysis ----------
    depths = [f["properties"]["depth_m"] for f in features
              if f["properties"]["depth_m"] is not None]
    depth_min = min(depths) if depths else 0
    depth_max = max(depths) if depths else 0

    # ---------- resonatable lookup keyed by resource group name ----------
    reso_by_group = {}
    for r in reso:
        nm = (r.get("name") or "").strip()
        if not nm:
            continue
        reso_by_group[nm] = r

    # ---------- writes ----------
    meta = {
        "world_bounds": {
            "x_min": bounds["x_min"], "x_max": bounds["x_max"],
            "y_min": bounds["y_min"], "y_max": bounds["y_max"],
            "width_m":  round((bounds["x_max"] - bounds["x_min"]) / 100, 1),
            "height_m": round((bounds["y_max"] - bounds["y_min"]) / 100, 1),
        },
        "depth_range_m": {"min": depth_min, "max": depth_max},
        "regions": region_meta,
        "legend": legend,
        "total_placements": len(features),
        "skipped_outside": skipped_outside,
        "base_image": "assets/terrain_map.png",
        "hillshade_image": "assets/hillshade.png",
        "overview_image": "assets/map_overview.png",
        "source_build": "5.6.1-112084+++Project+SN2-Release-Hotfix-Subnautica2",
    }
    _save("meta.json", meta, indent=2)
    _save("markers.geojson", geo)
    _save("biomes.geojson", organic)
    _save("zones.geojson", zones_geo)
    _save("outline.geojson", outline)
    _save("regions.json", region_meta, indent=2)
    if heightmap is not None:
        _save("heightmap.json", heightmap, indent=None)

    # Slim items list (drop bulky raw asset paths in tags etc.)
    slim_items = [{
        "id": it["id"],
        "name": it.get("name") or "",
        "description": it.get("description") or "",
        "actor_class": it.get("actor_class") or "",
        "category": it.get("category"),
        "stack_size": it.get("stack_size"),
    } for it in items]
    _save("items.json", slim_items, indent=None)
    _save("resonatables.json", reso, indent=None)
    _save("creatures.json", archetypes, indent=None)
    _save("biomods.json", biomods, indent=None)
    _save("recipes.json", recipes, indent=None)
    _save("databank.json", databank, indent=None)
    _save("scan_data.json", scan_data, indent=None)
    _save("pings.json", pings, indent=None)
    _save("locations.json", locations, indent=None)
    _save("characters.json", characters, indent=None)

    # ---------- emit JS bundle (works over file:// without CORS) ----------
    print("writing data-bundle.js (single file, no fetch needed)...")
    bundle = {
        "meta": meta,
        "markers": geo,
        "biomes": organic,
        "zones": zones_geo,
        "outline": outline,
        "regions": region_meta,
        "resonatables": reso,
        "databank": databank,
        "items": slim_items,
        "creatures": archetypes,
        "heightmap": heightmap,
    }
    js_path = os.path.join(HERE, "data-bundle.js")
    payload = json.dumps(bundle, ensure_ascii=False, default=str, separators=(",", ":"))
    # Defend against accidental "</script>" inside data.
    payload = payload.replace("</", "<\\/")
    with open(js_path, "w", encoding="utf-8") as f:
        f.write("window.SN2_DATA = ")
        f.write(payload)
        f.write(";\n")
    print(f"  wrote data-bundle.js {os.path.getsize(js_path) // 1024} kB")

    # ---------- copy base image assets ----------
    print("copying base images to assets/...")
    for src in ("terrain_map.png", "hillshade.png", "map_overview.png",
                "map_resources.png", "map_creatures.png", "map_pois.png",
                "map_depth.png", "heightmap.png", "heightmap_contours.png"):
        src_p = os.path.join(MINER_OUT, src)
        if os.path.exists(src_p):
            shutil.copy2(src_p, os.path.join(ASSETS_DIR, src))
            print(f"  assets/{src}")

    print()
    print("done.")
    print(f"  placements:   {len(features):>6}")
    print(f"  bounds (m):   width={meta['world_bounds']['width_m']}  height={meta['world_bounds']['height_m']}")
    print(f"  depth (m):    {depth_min} -> {depth_max}")
    print(f"  regions:      {len(region_meta)}")
    print(f"  categories:   " + ", ".join(f"{c}={by_category_count[c]}" for c in CATEGORIES))


if __name__ == "__main__":
    build()
