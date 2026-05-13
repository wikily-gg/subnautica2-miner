"""
Authoritative biome / sub-region classification for Subnautica 2.

Three layers, applied in priority order to each placement (x, y, z):

  1. **UWEBoxWorldZone**: 204 axis-aligned box zones placed in world cells.
     Each one references a ``UWEWorldRegionDataAsset`` whose ``RegionTag``
     looks like ``WorldPopulation.Region.CoralGardens.Plateaus``.  When a
     point falls inside a box, this gives the most specific sub-region.
  2. **PrefabActor → PrefabComponent → PrefabAssetInterface**: the prefab
     asset path encodes the biome (``/Game/Art/Environment/Biome/<X>/...``).
     Used for PrefabActors that don't fall inside any region box.
  3. **StaticMesh path regex** (fallback): same as the original biomes.py
     extractor — match ``/Biome/<Name>/`` in the actor's static mesh.

Outputs:
  out/region_zones.json   — every UWEBoxWorldZone with bounds + region tag
  out/regions.json        — the 19 UWEWorldRegionDataAsset definitions
  out/biome_points_v2.json — classified placements (also includes L_Main
                             top-level actors, not just _Generated_ cells)
"""
from __future__ import annotations

import logging
import os
import re

from helpers import (
    _export_class, _extract_soft_path, prop, prop_array, rot_to_list,
    unwrap_struct, vec_to_list,
)

logger = logging.getLogger(__name__)

L_MAIN_TOP = "Subnautica2/Content/Maps/Main/L_Main"
CELL_PREFIX = L_MAIN_TOP + "/_Generated_/"
REGIONS_DIR = "Subnautica2/Content/Data/WorldPopulation2/Regions/"

# Tag prefix -> root biome name
TAG_TO_BIOME_ROOT = {
    "WorldPopulation.Region.CoralGardens":     "CoralGardens",
    "WorldPopulation.Region.OvergrownRuins":   "OvergrownRuins",
    "WorldPopulation.Region.SparsePlains":     "SparsePlains",
    "WorldPopulation.Region.JellyPlateaus":    "JellyPlateaus",
    "WorldPopulation.Region.DeepStart":        "DeepStart",
    "WorldPopulation.Region.WorldTree":        "WorldTree",
    "WorldPopulation.Region.Void":             "Void",
    "WorldPopulation.Region.KelpForest":       "KelpForest",
}

# Asset-name prefix → (biome, sub_region) fallback when RegionTag is missing
# or shared between assets (some AR-* regions all carry the same tag in the
# game's data, so we use the asset short name as the authoritative sub-region).
ASSET_PREFIX_MAP: dict[str, tuple[str, str]] = {
    "DA_CG":              ("CoralGardens",   None),   # sub = remainder
    "DA_AR":              ("OvergrownRuins", None),
    "DA_SparsePlains":    ("SparsePlains",   None),
    "DA_JP":              ("JellyPlateaus",  None),
    "DA_DeepStart":       ("DeepStart",      None),
    "DA_CollectorLeviathanRegion": ("CollectorLeviathanRegion", None),
}

# Coarse-biome → canonical-biome mapping.  Mesh-path regex returns "CoralGarden"
# (singular, from /Biome/CoralGarden/) but the tag namespace uses plural
# "CoralGardens" — normalise so the two sources agree.
BIOME_ALIASES = {
    "CoralGarden": "CoralGardens",
}

# Assets we deliberately ignore — test or template regions
SKIP_REGION_IDS = {"DA_L_Gameplay_Stimuli", "DA_L_Template_World", "DA_WorldPopTest"}


def _normalize_biome(name: str | None) -> str | None:
    if not name:
        return name
    return BIOME_ALIASES.get(name, name)


def _meta_from_asset_short(short: str) -> tuple[str | None, str | None]:
    """Fallback when RegionTag is missing or duplicated across regions.

    ``DA_CGPlateaus``     → ("CoralGardens",   "Plateaus")
    ``DA_AR-Observatory`` → ("OvergrownRuins", "Observatory")
    ``DA_DeepStart``      → ("DeepStart",      None)
    """
    for prefix, (biome, _) in ASSET_PREFIX_MAP.items():
        if short.startswith(prefix):
            rest = short[len(prefix):].lstrip("-_")
            # Drop a trailing _AbvWater / _Surface marker
            for suffix in ("_AbvWater", "_Surface", "_Combined"):
                if rest.endswith(suffix):
                    rest = rest[:-len(suffix)]
            return biome, (rest or None)
    return None, None

# Cached at module level so the classifier can reuse them
_BIOME_PATH_RE = re.compile(r"/Biome/([A-Za-z0-9_]+)/")


# ---------------- region asset definitions ----------------

def _read_region_tag(export) -> str | None:
    """Read the FGameplayTag TagName from a region asset's RegionTag struct."""
    val = prop(export, "RegionTag")
    u = unwrap_struct(val)
    if u is None:
        return None
    tn = prop(u, "TagName")
    if tn is None:
        return None
    s = str(tn)
    return s if s and s != "None" else None


def _biome_root_from_tag(tag: str | None) -> str | None:
    if not tag:
        return None
    for prefix, biome in TAG_TO_BIOME_ROOT.items():
        if tag.startswith(prefix):
            return biome
    # Tag without a known prefix: take the third segment if available
    parts = tag.split(".")
    if len(parts) >= 3 and parts[0] == "WorldPopulation" and parts[1] == "Region":
        return parts[2]
    return None


def _sub_region_from_tag(tag: str | None) -> str | None:
    if not tag:
        return None
    parts = tag.split(".")
    if len(parts) >= 4 and parts[0] == "WorldPopulation" and parts[1] == "Region":
        return ".".join(parts[3:])
    return None


def _normalize_region_key(ref_or_path: str) -> str:
    """Normalize any form of region reference to a stable key.

    Soft refs from UWEBoxWorldZone look like:
        /Game/Data/WorldPopulation2/Regions/DA_CGPlateaus.DA_CGPlateaus
    Asset paths from provider.Files.Keys look like:
        Subnautica2/Content/Data/WorldPopulation2/Regions/DA_CGPlateaus.uasset

    Both reduce to the short asset name ``DA_CGPlateaus``.
    """
    if not ref_or_path:
        return ""
    s = ref_or_path.replace("\\", "/")
    # Strip .uasset
    if s.lower().endswith(".uasset"):
        s = s[:-7]
    # Take last segment, then strip any .AssetName suffix
    last = s.rsplit("/", 1)[-1]
    if "." in last:
        last = last.split(".", 1)[0]
    return last.lower()


def load_region_definitions(provider) -> dict[str, dict]:
    """Load the UWEWorldRegionDataAsset entries.

    Keyed by short asset id (``da_cgplateaus``) so soft refs from
    UWEBoxWorldZone (``/Game/.../DA_CGPlateaus.DA_CGPlateaus``) and Subnautica
    asset paths (``Subnautica2/Content/.../DA_CGPlateaus.uasset``) both resolve.
    """
    out: dict[str, dict] = {}
    for path in sorted(provider.Files.Keys):
        if not path.startswith(REGIONS_DIR) or not path.endswith(".uasset"):
            continue
        if path.endswith("_Config.uasset"):
            continue
        pkg_path = path[:-7]
        short = pkg_path.rsplit("/", 1)[-1]
        if short in SKIP_REGION_IDS:
            continue
        try:
            ok, pkg = provider.TryLoadPackage(pkg_path)
        except Exception:
            continue
        if not ok or pkg is None:
            continue
        for e in pkg.GetExports():
            if _export_class(e) != "UWEWorldRegionDataAsset":
                continue
            tag = _read_region_tag(e)
            disp = prop(e, "DisplayName")
            from helpers import _coerce_str
            disp_s = _coerce_str(disp) or short.replace("DA_", "")
            biome = _biome_root_from_tag(tag)
            sub = _sub_region_from_tag(tag)
            # Fall back to asset short name when tag is missing or duplicated
            # across distinct regions (the AR-* assets all carry the same tag).
            fb_biome, fb_sub = _meta_from_asset_short(short)
            if not biome:
                biome = fb_biome
            if not sub or sub.startswith("Observatory"):
                # AR-* regions all map their tag to "Observatory" — override
                if fb_sub:
                    sub = fb_sub
            key = _normalize_region_key(pkg_path)
            out[key] = {
                "asset_id": pkg_path,
                "short": short,
                "display_name": disp_s,
                "tag": tag,
                "biome": biome,
                "sub_region": sub,
            }
            break
    logger.info("Loaded %d region definitions", len(out))
    return out


# ---------------- UWEBoxWorldZone sweep ----------------

def _resolve_box_world_bounds(zone):
    """Return ``(center_xyz, half_extent_xyz)`` for a UWEBoxWorldZone or None."""
    rc = prop(zone, "RootComponent")
    if rc is None:
        return None
    try:
        comp = rc.Load()
    except Exception:
        return None
    if comp is None:
        return None
    center = vec_to_list(prop(comp, "RelativeLocation"))
    extent = vec_to_list(prop(comp, "BoxExtent"))
    if center is None or extent is None:
        return None
    return center, extent


def _list_cells(provider) -> list[str]:
    return sorted(p for p in provider.Files.Keys
                  if p.startswith(CELL_PREFIX) and p.endswith(".umap"))


def collect_box_zones(provider, regions_by_path: dict) -> list[dict]:
    """Sweep cells + L_Main top-level for UWEBoxWorldZone actors."""
    zones: list[dict] = []
    targets = _list_cells(provider) + [L_MAIN_TOP + ".umap"]
    logger.info("Box zone sweep: %d targets", len(targets))
    for i, cell_path in enumerate(targets, 1):
        try:
            ok, pkg = provider.TryLoadPackage(cell_path[:-5])
        except Exception:
            continue
        if not ok or pkg is None:
            continue
        cell_id = cell_path.rsplit("/", 1)[-1].replace(".umap", "")
        for e in pkg.GetExports():
            if _export_class(e) != "UWEBoxWorldZone":
                continue
            bounds = _resolve_box_world_bounds(e)
            if bounds is None:
                continue
            center, extent = bounds
            region_ref = _extract_soft_path(prop(e, "Region"))
            region_meta = regions_by_path.get(_normalize_region_key(region_ref or ""))
            zones.append({
                "name": str(e.Name),
                "cell": cell_id,
                "center": center,
                "extent": extent,
                "region_ref": region_ref,
                "biome": (region_meta or {}).get("biome"),
                "sub_region": (region_meta or {}).get("sub_region"),
                "display_name": (region_meta or {}).get("display_name"),
                "tag": (region_meta or {}).get("tag"),
            })
        if i % 400 == 0:
            logger.info("  cells %d/%d, zones: %d", i, len(targets), len(zones))
        del pkg
    logger.info("Found %d UWEBoxWorldZone instances", len(zones))
    return zones


# ---------------- prefab-ref biome lookup ----------------

def _prefab_biome(actor) -> tuple[str | None, str | None]:
    """For a PrefabActor, return ``(biome, prefab_asset_path)`` from its prefab ref."""
    pc_ref = prop(actor, "PrefabComponent")
    if pc_ref is None:
        return None, None
    try:
        pc = pc_ref.Load()
    except Exception:
        return None, None
    if pc is None:
        return None, None
    path = _extract_soft_path(prop(pc, "PrefabAssetInterface"))
    if not path:
        return None, None
    m = _BIOME_PATH_RE.search(path)
    return (m.group(1) if m else None), path


# ---------------- layered point classifier ----------------

class RegionClassifier:
    """AABB-test a 3D point against all UWEBoxWorldZone bounds, smallest wins."""

    def __init__(self, zones: list[dict]):
        self.zones = []
        for z in zones:
            cx, cy, cz = z["center"]
            ex, ey, ez = z["extent"]
            volume = abs(ex * ey * ez)
            self.zones.append({
                "x_min": cx - ex, "x_max": cx + ex,
                "y_min": cy - ey, "y_max": cy + ey,
                "z_min": cz - ez, "z_max": cz + ez,
                "volume": volume,
                "biome": z.get("biome"),
                "sub_region": z.get("sub_region"),
                "display_name": z.get("display_name"),
                "tag": z.get("tag"),
            })

    def classify(self, x: float, y: float, z: float) -> dict | None:
        """Return the smallest zone containing (x, y, z), or None."""
        best = None
        best_vol = float("inf")
        for zn in self.zones:
            if (zn["x_min"] <= x <= zn["x_max"]
                    and zn["y_min"] <= y <= zn["y_max"]
                    and zn["z_min"] <= z <= zn["z_max"]):
                if zn["volume"] < best_vol:
                    best = zn
                    best_vol = zn["volume"]
        return best


# ---------------- main placement sweep with layered classifier ----------------

def _mesh_biome(actor) -> tuple[str | None, str | None]:
    rc = prop(actor, "RootComponent")
    if rc is None:
        return None, None
    try:
        comp = rc.Load()
    except Exception:
        return None, None
    if comp is None:
        return None, None
    mesh_ref = prop(comp, "StaticMesh")
    if mesh_ref is None:
        return None, None
    s = str(mesh_ref)
    if "'" in s:
        mesh_path = s.split("'", 1)[1].rstrip("'")
    else:
        mesh_path = s
    m = _BIOME_PATH_RE.search(mesh_path)
    return (m.group(1) if m else None), mesh_path


def _actor_xyz(actor) -> list[float] | None:
    rc = prop(actor, "RootComponent")
    if rc is None:
        return None
    try:
        comp = rc.Load()
    except Exception:
        return None
    if comp is None:
        return None
    return vec_to_list(prop(comp, "RelativeLocation"))


SKIP_CLASSES = {
    "Level", "Model", "NavigationSystemModuleConfig",
    "World", "WorldSettings", "Brush",
    "UWEBoxWorldZone",  # the regions themselves
}


def classify_placements(provider, classifier: RegionClassifier) -> dict:
    """Sweep all targets and classify each placement with the layered pipeline."""
    targets = _list_cells(provider) + [L_MAIN_TOP + ".umap"]
    logger.info("Placement classification sweep: %d targets", len(targets))

    points: list[dict] = []
    by_source = {"region_box": 0, "prefab_ref": 0, "mesh_path": 0, "unknown": 0}

    for i, cell_path in enumerate(targets, 1):
        try:
            ok, pkg = provider.TryLoadPackage(cell_path[:-5])
        except Exception:
            continue
        if not ok or pkg is None:
            continue
        cell_id = cell_path.rsplit("/", 1)[-1].replace(".umap", "")
        for e in pkg.GetExports():
            cls = _export_class(e)
            if cls in SKIP_CLASSES:
                continue
            loc = _actor_xyz(e)
            if loc is None:
                continue
            # 1) region box
            zone = classifier.classify(*loc)
            source = "unknown"
            biome = sub_region = display_name = tag = None
            extra_ref = None
            if zone is not None:
                source = "region_box"
                biome = zone["biome"]
                sub_region = zone["sub_region"]
                display_name = zone["display_name"]
                tag = zone["tag"]
            elif cls == "PrefabActor":
                pb, pp = _prefab_biome(e)
                if pb:
                    source = "prefab_ref"
                    biome = pb
                    extra_ref = pp
            if biome is None:
                mb, mp = _mesh_biome(e)
                if mb:
                    source = "mesh_path"
                    biome = mb
                    extra_ref = mp
            if biome is None:
                by_source["unknown"] += 1
                continue
            # Normalize biome name (CoralGarden → CoralGardens)
            biome = _normalize_biome(biome)
            by_source[source] += 1
            points.append({
                "biome": biome,
                "sub_region": sub_region,
                "tag": tag,
                "display_name": display_name,
                "source": source,
                "cls": cls,
                "cell": cell_id,
                "x": loc[0], "y": loc[1], "z": loc[2],
                "ref": extra_ref,
            })
        if i % 400 == 0:
            logger.info("  targets %d/%d, points: %d", i, len(targets), len(points))
        del pkg

    by_biome: dict[str, int] = {}
    by_sub: dict[str, int] = {}
    for p in points:
        by_biome[p["biome"]] = by_biome.get(p["biome"], 0) + 1
        if p["sub_region"]:
            key = f"{p['biome']}.{p['sub_region']}"
            by_sub[key] = by_sub.get(key, 0) + 1

    logger.info("Classified %d points (sources: %s)", len(points), by_source)
    return {
        "summary": {
            "total_points": len(points),
            "by_source": by_source,
            "by_biome": dict(sorted(by_biome.items(), key=lambda x: -x[1])),
            "by_sub_region": dict(sorted(by_sub.items(), key=lambda x: -x[1])),
        },
        "points": points,
    }


# ---------------- edge-of-world + habitation volumes ----------------

def extract_world_boundaries(provider) -> dict:
    """Extract BP_EdgeOfWorldVolume_C + UWEHabitationVolume from L_Main top-level.

    The edge volumes are 58 thin wall slabs that ring the playable area.  Each
    has a position, yaw, and X-scale; their combined AABB is the canonical
    map bounds.  Habitation volumes mark zones where the player can build a
    habitat.
    """
    import math
    from helpers import extract_gameplay_tags

    try:
        ok, pkg = provider.TryLoadPackage(L_MAIN_TOP)
    except Exception:
        return {"edge_walls": [], "habitation_volumes": [], "bounds": None}
    if not ok or pkg is None:
        return {"edge_walls": [], "habitation_volumes": [], "bounds": None}

    walls = []
    hab_volumes = []
    x_lo = y_lo = z_lo = float("inf")
    x_hi = y_hi = z_hi = float("-inf")

    for e in pkg.GetExports():
        cls = _export_class(e)
        rc = prop(e, "RootComponent")
        if rc is None:
            continue
        try:
            comp = rc.Load()
        except Exception:
            continue
        if comp is None:
            continue
        loc = vec_to_list(prop(comp, "RelativeLocation"))
        if loc is None:
            continue
        scale = vec_to_list(prop(comp, "RelativeScale3D")) or [1.0, 1.0, 1.0]
        # IMPORTANT: must use rot_to_list (reflection-based) — direct
        # getattr on the FRotator silently returns 0 because Pitch/Yaw/Roll
        # are hidden behind the IUStruct interface in pythonnet.
        rot_pyr = rot_to_list(prop(comp, "RelativeRotation")) or [0.0, 0.0, 0.0]
        pitch, yaw, roll = rot_pyr

        if cls == "BP_EdgeOfWorldVolume_C":
            # Wall slab: half-length along local +X = scale.X * 50 cm
            half_len = abs(scale[0]) * 50.0
            rad = math.radians(yaw)
            dx, dy = math.cos(rad) * half_len, math.sin(rad) * half_len
            p1 = [loc[0] - dx, loc[1] - dy]
            p2 = [loc[0] + dx, loc[1] + dy]
            walls.append({
                "name": str(e.Name),
                "center": loc,
                "yaw": yaw,
                "scale": scale,
                "p1": p1,
                "p2": p2,
                "length_cm": 2 * half_len,
            })
            for px, py in (p1, p2):
                x_lo = min(x_lo, px); x_hi = max(x_hi, px)
                y_lo = min(y_lo, py); y_hi = max(y_hi, py)
            z_lo = min(z_lo, loc[2]); z_hi = max(z_hi, loc[2])
        elif cls == "UWEHabitationVolume":
            tag_val = prop(e, "HabitationAreaTag")
            tags = extract_gameplay_tags(tag_val)
            hab_volumes.append({
                "name": str(e.Name),
                "center": loc,
                "scale": scale,
                "yaw": yaw,
                "area_tags": tags,
            })

    bounds = None
    if walls:
        bounds = {
            "x_min": x_lo, "x_max": x_hi,
            "y_min": y_lo, "y_max": y_hi,
            "z_min": z_lo, "z_max": z_hi,
            "width_cm": x_hi - x_lo,
            "height_cm": y_hi - y_lo,
        }

    logger.info("World boundary: %d edge walls, %d habitation volumes",
                len(walls), len(hab_volumes))
    if bounds:
        logger.info("  map bounds X=[%.0f, %.0f] Y=[%.0f, %.0f]  (%.0fm × %.0fm)",
                    bounds["x_min"], bounds["x_max"], bounds["y_min"], bounds["y_max"],
                    bounds["width_cm"] / 100, bounds["height_cm"] / 100)

    return {"edge_walls": walls, "habitation_volumes": hab_volumes, "bounds": bounds}


# ---------------- orchestrator ----------------

def run(provider) -> dict:
    boundaries = extract_world_boundaries(provider)
    regions = load_region_definitions(provider)
    zones = collect_box_zones(provider, regions)
    classifier = RegionClassifier(zones)
    classified = classify_placements(provider, classifier)
    return {
        "boundaries": boundaries,
        "regions": list(regions.values()),
        "zones": zones,
        "classified": classified,
    }
