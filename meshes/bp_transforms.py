"""
Read SN2 vehicle Blueprint component trees and return the full set of
mesh-component transforms.

Each SN2 chassis Blueprint (`BP_Tadpole`, `BP_Haul_TadpoleChassis`,
`BP_ScoutRay_TadpoleChassis`, `BP_Lifepod`) carries a Simple Construction
Script (SCS) that lists every visible mesh component with:

    - StaticMesh / SkeletalMesh reference
    - RelativeLocation  (X, Y, Z) in centimetres, UE convention
    - RelativeRotation  (Pitch, Yaw, Roll) in degrees, UE convention
    - RelativeScale3D   (X, Y, Z), 1.0 default, negative = mirror

The same mesh can appear MORE THAN ONCE in the SCS at different
transforms — that's how SN2 builds the left / right propellers from a
single `SM_Tadpole_Prop_Secondary_LR` mesh, etc. So callers must walk
the list of COMPONENTS, not the deduplicated set of meshes.

Usage:
    from meshes.bp_transforms import read_bp_components
    comps = read_bp_components(provider, "/Game/Blueprints/Vehicle/BP_Tadpole")
    for c in comps:
        print(c.mesh_slug, c.location, c.rotation, c.scale)

Output is JSON-serialisable so the composite renderer can hand it to
the Blender child process via a sidecar file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from typing import Iterable

logger = logging.getLogger(__name__)

# Mesh-component classes we care about. The SCS contains a bunch of
# non-visible component types (volume trackers, save components, ability
# system bits) that we ignore here.
_MESH_COMP_CLASSES = ("UStaticMeshComponent", "USkeletalMeshComponent")


@dataclass
class BPComponent:
    """One entry in a chassis BP's SCS - a single mesh placement.

    Fields:
        component_name  the SCS node / variable name, e.g.
                        `SM_Tadpole_UpgradeSlot_R_GEN_VARIABLE`.
                        Two instances of the same mesh have distinct
                        component names so callers can preserve both.
        mesh_slug       the bare asset name with no path, no class
                        prefix, no .uasset suffix. Matches the slug
                        we use elsewhere in the miner (e.g.
                        `SM_Tadpole_UpgradeSlot`).
        location        (X, Y, Z) in UE centimetres. None when the BP
                        leaves it at default (0, 0, 0).
        rotation        (Pitch, Yaw, Roll) in UE degrees. None when
                        default.
        scale           (X, Y, Z), 1.0 default. Negative values mirror
                        the mesh across the corresponding axis - this
                        is how the engine builds left / right variants
                        from a single source mesh.
    """
    component_name: str
    mesh_slug: str
    location: tuple[float, float, float] | None
    rotation: tuple[float, float, float] | None
    scale: tuple[float, float, float] | None

    def to_json(self) -> dict:
        return asdict(self)


def _safe_load(provider, pkg_path: str):
    """Wrap CUE4Parse's package load so a missing BP returns None
    instead of raising. Some chassis BPs may not ship in every build.
    """
    try:
        from helpers import safe_load_package
        return safe_load_package(provider, pkg_path)
    except Exception as e:
        logger.warning("bp_transforms: failed to load %s: %s", pkg_path, e)
        return None


def _parse_mesh_ref(mesh_ref) -> str | None:
    """Extract the bare slug (e.g. `SM_Tadpole_Body_Nanite`) from a CUE4Parse
    object reference. Returns None for empty / null references.
    """
    if mesh_ref is None:
        return None
    if isinstance(mesh_ref, dict):
        name = mesh_ref.get("ObjectName")
    else:
        name = str(mesh_ref)
    if not name or name == "None":
        return None
    # ObjectName looks like `StaticMesh'SM_Tadpole_Body_Nanite'` —
    # strip the class prefix + single quotes.
    if "'" in name:
        name = name.split("'", 2)[1]
    # Drop a `.subobject` tail if present
    if "." in name:
        name = name.rsplit(".", 1)[-1]
    # Some references include the full path - keep only the basename
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name or None


def _parse_vec3(value, default: tuple[float, float, float] | None = None):
    """Translate CUE4Parse's FVector dict to a tuple. Returns *default*
    when the property is missing.
    """
    if value is None:
        return default
    if isinstance(value, dict):
        x = value.get("X", 0.0)
        y = value.get("Y", 0.0)
        z = value.get("Z", 0.0)
        return (float(x), float(y), float(z))
    return default


def _parse_rotator(value, default: tuple[float, float, float] | None = None):
    """Translate CUE4Parse's FRotator dict to (Pitch, Yaw, Roll). Returns
    *default* when the property is missing.
    """
    if value is None:
        return default
    if isinstance(value, dict):
        p = value.get("Pitch", 0.0)
        y = value.get("Yaw", 0.0)
        r = value.get("Roll", 0.0)
        return (float(p), float(y), float(r))
    return default


def read_bp_components(provider, pkg_path: str) -> list[BPComponent]:
    """Walk a Blueprint's SCS and return every mesh-component entry.

    The Simple Construction Script (SCS) lives as separate
    `USCS_Node_*` exports inside the BP package. Each SCS node has a
    `ComponentTemplate` reference pointing at the actual
    `UStaticMeshComponent` / `USkeletalMeshComponent` export which
    carries the mesh + transform. We just iterate the mesh-component
    exports directly — they're a subset of the SCS nodes anyway and
    that saves us walking the indirection.

    Returns components in BP-definition order (CUE4Parse preserves
    export order, which mirrors the SCS authoring order in Unreal).
    """
    import json
    import clr
    try:
        clr.AddReference("Newtonsoft.Json")
        from Newtonsoft.Json import JsonConvert, Formatting
    except Exception:
        logger.error("bp_transforms: Newtonsoft.Json not available")
        return []

    pkg = _safe_load(provider, pkg_path)
    if pkg is None:
        return []

    components: list[BPComponent] = []
    for ex in pkg.GetExports():
        cls = type(ex).__name__
        if cls not in _MESH_COMP_CLASSES:
            continue
        try:
            j_str = JsonConvert.SerializeObject(ex, Formatting.Indented)
            j = json.loads(j_str)
        except Exception as e:
            logger.warning("bp_transforms: serialize failed for %s: %s",
                           ex.Name, e)
            continue

        props = j.get("Properties", {})
        # StaticMesh / SkeletalMesh field name varies; try both plus the
        # newer Unreal `SkeletalMeshAsset` form.
        mesh_ref = (
            props.get("StaticMesh")
            or props.get("SkeletalMesh")
            or props.get("SkeletalMeshAsset")
        )
        mesh_slug = _parse_mesh_ref(mesh_ref)
        if not mesh_slug:
            # Some hardpoint / collision components have no mesh — skip.
            continue

        loc = _parse_vec3(props.get("RelativeLocation"))
        rot = _parse_rotator(props.get("RelativeRotation"))
        scale = _parse_vec3(props.get("RelativeScale3D"))

        components.append(BPComponent(
            component_name=str(ex.Name),
            mesh_slug=mesh_slug,
            location=loc,
            rotation=rot,
            scale=scale,
        ))

    return components


def read_merged_bp_components(
    provider, pkg_paths: Iterable[str],
) -> list[BPComponent]:
    """Read multiple Blueprints (e.g. base Tadpole + a chassis BP) and
    concatenate their component lists.

    Same component name across BPs is kept verbatim — the chassis BP
    components live in their own namespace so collisions are not
    expected, but if they happen we treat the chassis entry as an
    additional placement.
    """
    out: list[BPComponent] = []
    for pp in pkg_paths:
        out.extend(read_bp_components(provider, pp))
    return out
