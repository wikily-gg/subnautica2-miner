"""
Discover the static / skeletal mesh attached to each item's Blueprint.

Many SN2 ItemType data assets ship without an Icon property, and their
ItemType BP doesn't define one either. The in-game UI renders a 3D
preview of the item's mesh at runtime via UMG widgets. We can replicate
that statically by:

  1. Loading each item's BP (`actor_class` -> BP package)
  2. Walking the BP's UStaticMeshComponent / USkeletalMeshComponent
     exports and reading their StaticMesh / SkeletalMesh refs
  3. Picking the "preview" mesh: priority order is
     EquippedMesh > Mesh > StaticMesh > the first mesh component
     we find. Items like the Disperser have both EquippedMesh and
     Mesh; EquippedMesh is the inventory thumbnail mesh.
  4. Resolving the asset path so the existing
     `meshes/exporter.py` can export it to GLB.

Output: list of dicts saved to `out/item_meshes.json`:

    [
      {
        "item_id":      "DA_Disperser_ItemType",
        "item_slug":    "Disperser",
        "mesh_slug":    "SKM_Disperser_01",
        "mesh_pkg":     "/Game/Art/Items/Tools/Disperser/Mesh/SKM_Disperser_01",
        "component":    "EquippedMesh",
        "class":        "USkeletalMeshComponent",
      },
      ...
    ]
"""

from __future__ import annotations

import json
import logging
import os
import sys

from helpers import (
    obj_ref_path,
    prop_object_path,
    safe_load_package,
)

logger = logging.getLogger(__name__)

# Preview-mesh component preference. EquippedMesh (held-in-hand) usually
# matches the in-game inventory thumbnail; plain Mesh is the world-drop
# variant. If both exist we prefer EquippedMesh.
_MESH_COMPONENT_PRIORITY = (
    "EquippedMesh",
    "Mesh",
    "StaticMesh",
    "SkeletalMesh",
    "InventoryMesh",
    "ItemMesh",
    "WorldMesh",
    "DroppedMesh",
)

# Class names CUE4Parse reports for mesh-bearing components.
_MESH_COMPONENT_CLASSES = (
    "UStaticMeshComponent",
    "USkeletalMeshComponent",
)


def _bp_pkg_from_actor_class(actor_class: str | None) -> str | None:
    """`/Game/Blueprints/Items/Tools/BP_Disperser.BP_Disperser_C`
    -> `/Game/Blueprints/Items/Tools/BP_Disperser`."""
    if not actor_class:
        return None
    pkg = actor_class.split(".", 1)[0]
    return pkg or None


def _resolve_mesh_path(value) -> str | None:
    """Extract an object-path from one of CUE4Parse's mesh-ref shapes
    (FSoftObjectPath, FObjectPath, or a string)."""
    if value is None:
        return None
    # Direct string
    if isinstance(value, str):
        s = value.strip()
        if not s or s == "None":
            return None
        # Strip class prefix `StaticMesh'/Game/.../Foo.Foo'`
        if "'" in s:
            s = s.split("'", 2)[1]
        if "." in s:
            s = s.split(".", 1)[0]
        return s if s.startswith("/") else None
    # dict-like (FSoftObjectPath with AssetPathName or ObjectPath)
    if isinstance(value, dict):
        for k in ("ObjectPath", "AssetPathName", "ObjectName"):
            v = value.get(k)
            if v:
                return _resolve_mesh_path(v)
        return None
    # Probably a CUE4Parse property object
    try:
        return obj_ref_path(value)
    except Exception:
        return None


def _component_export_name(ex) -> str:
    """The trailing `_GEN_VARIABLE` decoration is noise; strip it so
    we compare component names like `EquippedMesh` directly."""
    name = str(getattr(ex, "Name", ""))
    if name.endswith("_GEN_VARIABLE"):
        name = name[: -len("_GEN_VARIABLE")]
    return name


def _clean_object_path(p: str | None) -> str | None:
    """Strip the `ClassName'...'` decoration and `.AssetName` tail
    from an Unreal object-path string, leaving a bare `/Game/...`
    package path. `StaticMesh'/Game/Art/Foo.Foo'` -> `/Game/Art/Foo`."""
    if not p:
        return None
    s = str(p).strip()
    # Strip the ClassName'...' wrapper - keep what's inside the quotes
    if "'" in s:
        # `Class'path'` -> take index 1 between quotes
        parts = s.split("'")
        if len(parts) >= 2 and parts[1]:
            s = parts[1]
    # Strip a trailing .AssetName (Unreal repeats the asset name after
    # a dot when referencing an export inside a package; we want the
    # package path only)
    if "." in s:
        s = s.split(".", 1)[0]
    s = s.strip()
    return s if s.startswith("/") else None


def _extract_component_mesh(ex) -> tuple[str, str] | None:
    """For a mesh-component export, return (StaticMesh package path,
    component class) or None if the component has no mesh ref. Path
    is cleaned of Unreal's `ClassName'...'` + `.AssetName` decorations
    so the caller can hand it straight to `provider.TryLoadPackage`.
    """
    # Try StaticMesh, SkeletalMesh, SkeletalMeshAsset properties.
    for prop_name in ("StaticMesh", "SkeletalMesh", "SkeletalMeshAsset"):
        raw = prop_object_path(ex, prop_name)
        cleaned = _clean_object_path(raw)
        if cleaned:
            return cleaned, type(ex).__name__
    return None


def discover_item_mesh(provider, item_id: str, actor_class: str | None) -> dict | None:
    """For one item, find the inventory-preview mesh on its BP. Returns
    None when the BP can't be loaded or has no mesh component.

    `item_id` is the DA_*_ItemType slug only - included in the output so
    the caller can correlate without a separate join.
    """
    pkg_path = _bp_pkg_from_actor_class(actor_class)
    if not pkg_path:
        return None
    package = safe_load_package(provider, pkg_path)
    if package is None:
        return None

    # Group mesh-component exports by their cleaned name so we can pick
    # the preferred slot (EquippedMesh > Mesh > ...). The same BP may
    # have several mesh components (Disperser has EquippedMesh +
    # plain Mesh + a HologramScreen widget); we want the first one
    # that priorities for inventory preview.
    by_name: dict[str, object] = {}
    for ex in package.GetExports():
        cls = type(ex).__name__
        if cls not in _MESH_COMPONENT_CLASSES:
            continue
        nm = _component_export_name(ex)
        if nm and nm not in by_name:
            by_name[nm] = ex

    # Walk priority order
    candidates: list[tuple[str, object]] = []
    for prio in _MESH_COMPONENT_PRIORITY:
        if prio in by_name:
            candidates.append((prio, by_name[prio]))
    # Then any remaining mesh components in BP-defined order.
    for nm, ex in by_name.items():
        if nm not in [c[0] for c in candidates]:
            candidates.append((nm, ex))

    for nm, ex in candidates:
        m = _extract_component_mesh(ex)
        if not m:
            continue
        mesh_pkg, cls_name = m
        # Mesh slug is the package basename.
        mesh_slug = mesh_pkg.rsplit("/", 1)[-1]
        item_slug = item_id.replace("DA_", "").replace("_ItemType", "")
        return {
            "item_id": item_id,
            "item_slug": item_slug,
            "mesh_slug": mesh_slug,
            "mesh_pkg": mesh_pkg,
            "component": nm,
            "class": cls_name,
        }
    return None


def run(provider, items: list[dict] | None = None) -> list[dict]:
    """Walk every item BP, return the mesh refs for inventory-preview
    rendering. Caller can filter to icon-less items before calling
    `meshes.exporter.export_one` / `meshes.renderer.render_one`.
    """
    if items is None:
        from extractors import items as ex_items
        # Build items here to avoid re-loading.
        paths = ex_items.find_paths(provider)
        items = [r for r in (ex_items.extract_item(provider, p) for p in paths) if r]
    out: list[dict] = []
    seen_meshes: set[str] = set()
    for i, it in enumerate(items, 1):
        ac = it.get("actor_class")
        item_id = it.get("id")
        if not ac or not item_id:
            continue
        mesh = discover_item_mesh(provider, item_id, ac)
        if mesh:
            seen_meshes.add(mesh["mesh_slug"])
            out.append(mesh)
        if i % 100 == 0:
            logger.info("item_meshes: %d / %d processed, %d found",
                        i, len(items), len(out))
    logger.info("item_meshes: %d items got a mesh ref (unique meshes: %d)",
                len(out), len(seen_meshes))
    return out


if __name__ == "__main__":
    # CLI: `python -m extractors.item_meshes`
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, here)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    import config
    from provider import create_provider
    provider = create_provider()
    rows = run(provider)
    out_path = os.path.join(config.OUTPUT_DIR, "item_meshes.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)
    print(f"wrote {len(rows)} rows -> {out_path}")
