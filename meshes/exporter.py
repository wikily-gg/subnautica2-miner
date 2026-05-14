"""
Export USkeletalMesh / UStaticMesh assets to glTF (.glb) via CUE4Parse.

Adapted from farfarwest-data-miner/meshes/exporter.py.

CUE4Parse-Conversion's `MeshExporter` writes a .glb plus PNG textures
for BaseColor / Normal / ORM into a directory we own. We share the same
provider as the rest of the miner so paks + mappings load once.

CLI:
    python run.py mesh-export <slug>
    python run.py mesh-export --all
    python run.py mesh-export --filter vehicle

Output:
    out/meshes/<slug>/<slug>.glb
    out/meshes/<slug>/Materials/*.png   (auto-extracted by CUE4Parse)

The CATALOG is a curated map of `slug -> /Game/...` package path for
every mesh we want to render. Slugs are chosen so the website can join
to the same identifier the JSON extractors produce (items.json `id`,
creature_archetypes.json `id`, resonatables.json `id`).
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from CUE4Parse.FileProvider import DefaultFileProvider

import config

logger = logging.getLogger(__name__)

_OUT_ROOT = os.path.join(config.OUTPUT_DIR, "meshes")


# ---------------------------------------------------------------------------
# Curated catalog
# ---------------------------------------------------------------------------
# Built from `python meshes/discover.py` output. Slugs match the website
# database identifier so /subnautica-2/<page>/<slug>/ can resolve a render
# at /images/subnautica-2/renders/<slug>.png.

# Vehicles - Tadpole + future big sub + PRAWN-class go here as confirmed.
VEHICLES: dict[str, str] = {
    # Populated from discovery output - see out/mesh_discovery.json
    # Initial seeds (these MUST exist in the discovery output for renders to work):
    # "Tadpole":           "/Game/Vehicles/Tadpole/Meshes/SK_Tadpole",
}

# Fauna - leviathans, fish, swarm creatures
FAUNA: dict[str, str] = {}

# Flora - plants, kelp, coral, anemones
FLORA: dict[str, str] = {}

# Resources - mineable deposits, harvestable nodes
RESOURCES: dict[str, str] = {}

# Items - hand-held / inventory items with renderable meshes
ITEMS: dict[str, str] = {}

# Multi-part composites - creatures whose in-world appearance is
# assembled from SEPARATE meshes by the actor blueprint. Render each
# part individually; the frontend stitches them at display time.
COMPOSITE_PARTS: dict[str, list[str]] = {
    "SKM_Clamthulu_01": [
        "SM_Clamthulu_01",            # main body shell (static)
        "SM_Clamthulu_01_Shell_Hinge",
    ],
}

# Vehicle assembly → list of chassis Blueprints to merge. The composite
# renderer reads each BP's Simple Construction Script to get the
# authoritative list of mesh components + per-component transforms
# (RelativeLocation / Rotation / Scale). Same mesh placed twice in the
# BP with different transforms creates left/right mirrored variants
# automatically — no hardcoded duplication needed.
#
# Chassis variants list BOTH the base Tadpole BP and their own chassis
# BP so the chassis-specific hardware sits on top of the base hull's
# components.
VEHICLE_BP_ASSEMBLIES: dict[str, list[str]] = {
    "vehicle_tadpole": [
        "/Game/Blueprints/Vehicle/BP_Tadpole",
    ],
    # HAUL is *standalone* — its Blueprint has its own complete hull
    # (`SM_Tadpole_HAUL` + `SKM_Tadpole_HAUL`), its own glass, chairs,
    # storage doors, spotlight, propeller housing, and re-uses just
    # the base Tadpole's propeller meshes via new transforms inside
    # the HAUL BP itself. Merging in the base Tadpole BP causes
    # double-body geometry and overlapping propellers — the HAUL
    # chassis swap REPLACES the base body in-engine.
    "vehicle_tadpole_haul": [
        "/Game/Blueprints/Vehicle/Tadpole/BP_Haul_TadpoleChassis",
    ],
    # ScoutRay and Seafrog are overlay chassis - their BPs only define
    # the chassis-specific hardware (manta wings + arms for ScoutRay,
    # placeholder collision cubes for Seafrog) and rely on the base
    # Tadpole hull to provide the body underneath.
    "vehicle_tadpole_scoutray": [
        "/Game/Blueprints/Vehicle/BP_Tadpole",
        "/Game/Blueprints/Vehicle/Tadpole/BP_ScoutRay_TadpoleChassis",
    ],
    "vehicle_tadpole_seafrog": [
        "/Game/Blueprints/Vehicle/BP_Tadpole",
        "/Game/Blueprints/Vehicle/Tadpole/BP_Seafrog_TadpoleChassis",
    ],
    "vehicle_lifepod": [
        "/Game/Blueprints/Vehicle/BP_Lifepod",
    ],
}


# Vehicle assemblies — full vehicles built from many static + skeletal
# meshes. The composite renderer (`render_assembly`) exports each part
# (via the standard `export_one`) and composes them in a single Blender
# scene, then renders ONE PNG. Output goes to `out/renders/<slug>.png`
# with `<slug>` matching the SN2 wiki vehicle slug.
#
# Part order doesn't affect the render (we import everything at the
# scene origin). Keys are wiki vehicle slugs so the website can resolve
# `<R2>/renders/vehicle_<slug>.png` directly.
#
# Notes:
#   • Trident — only accessory props are in the public build (lights,
#     drainage, transformer). No main hull, so we don't ship a Trident
#     assembly here.
#   • Lifepod — interior decals are mostly tiny coplanar decals that
#     don't help silhouette; included anyway because they're cheap.
#   • Glass meshes are excluded from the default assembly because they
#     dominate the bbox and Cycles renders them opaque-white when their
#     PBR materials aren't reconstructed correctly. Re-add if/when the
#     glass-shader path improves.
VEHICLE_ASSEMBLIES: dict[str, list[str]] = {
    "vehicle_tadpole": [
        "SKM_Tadpole",                       # cockpit canopy (skeletal)
        "SM_Tadpole_Body_Nanite",            # main hull body
        "SM_Tadpole_DM_1",
        "SM_Tadpole_DM_2",
        "SM_Tadpole_DM_3",
        "SM_Tadpole_DM_Screen",              # cockpit display
        "SM_Tadpole_Handlebar_L",
        "SM_Tadpole_Handlebar_R",
        "SM_Tadpole_Oxygenport",
        "SM_Tadpole_Prop_Main",              # main propeller
        "SM_Tadpole_Prop_Extend_Main",
        "SM_Tadpole_Prop_Secondary_Blades",
        "SM_Tadpole_Prop_Secondary_Boost",
        "SM_Tadpole_Prop_Secondary_LR",
        "SM_Tadpole_Storage",
        "SM_Tadpole_UpgradeSlot",
    ],
    "vehicle_tadpole_haul": [
        # HAUL is the BASE Tadpole with a cargo-hauler chassis overlay -
        # the chassis system extends the base sub with extra storage
        # bays, chairs, and a redesigned propeller housing, but the
        # cockpit canopy and core hull come from the base Tadpole.
        # Include every base Tadpole part PLUS the HAUL-specific cargo
        # hardware so the render has glass + cockpit + body.
        "SKM_Tadpole",                        # base cockpit canopy (glass)
        "SM_Tadpole_Body_Nanite",
        "SM_Tadpole_DM_1",
        "SM_Tadpole_DM_2",
        "SM_Tadpole_DM_3",
        "SM_Tadpole_DM_Screen",
        "SM_Tadpole_Handlebar_L",
        "SM_Tadpole_Handlebar_R",
        "SM_Tadpole_Oxygenport",
        "SM_Tadpole_Storage",
        "SM_Tadpole_UpgradeSlot",
        # HAUL-specific cargo-chassis attachments.
        "SKM_Tadpole_HAUL",
        "SM_Tadpole_HAUL",
        "SM_Tadpole_Haul_Chair_L",
        "SM_Tadpole_Haul_Chair_R",
        "SM_Tadpole_Haul_Handlebar",
        "SM_Tadpole_Haul_PropellorMainHousing",
        "SM_Tadpole_Haul_Spotlight",
        "SM_Tadpole_Haul_StorageDoor_L",
        "SM_Tadpole_Haul_StorageDoor_R",
        "SM_Tadpole_Haul_UpgradePanel",
    ],
    "vehicle_tadpole_scoutray": [
        # ScoutRay is the BASE Tadpole hull with a manta-wing chassis
        # overlay — the chassis system only swaps movement hardware,
        # not the sub itself. Include every base Tadpole part PLUS the
        # ScoutRay-specific manta wings.
        "SKM_Tadpole",                        # base cockpit canopy
        "SM_Tadpole_Body_Nanite",
        "SM_Tadpole_DM_1",
        "SM_Tadpole_DM_2",
        "SM_Tadpole_DM_3",
        "SM_Tadpole_DM_Screen",
        "SM_Tadpole_Handlebar_L",
        "SM_Tadpole_Handlebar_R",
        "SM_Tadpole_Oxygenport",
        "SM_Tadpole_Storage",
        "SM_Tadpole_UpgradeSlot",
        # ScoutRay-specific manta wing chassis attachments.
        "SK_Tadpole_Scout_Ray",
        "SM_Tadpole_Chassis_MantaWings",
        "SM_Tadpole_MantaWings_Arm",
        "SM_Tadpole_MantaWings_Chassis",
        "SM_Tadpole_MantaWings_L",
        "SM_Tadpole_MantaWings_Wing_L",
        "SM_Tadpole_MantaWings_Wing_R",
    ],
    "vehicle_lifepod": [
        "SM_Lifepod",                        # main pod body
        "SM_Lifepod_CeilingPanel",
        "SM_Lifepod_Door",
        "SM_Lifepod_Door_Glass",             # window in the hatch
        "SM_Lifepod_Door_Latch",
        "SM_Lifepod_Glass",                  # main canopy glass
        "SM_Lifepod_Interior",
        "SM_Lifepod_Interior_Decals",
        "SM_Lifepod_MedkitDispenser",
        "SM_Lifepod_NOA_Glass",              # NoA AI eye lens
        "SM_Lifepod_Radio",
        "SM_Lifepod_ReleaseHandle",
        "SM_Lifepod_Tube",
        "SM_Lifepod_TubeCover",
        "SM_MiniNoA_Eye",
        "SM_MiniNoA_EyeGlass",               # AI eye glass cover
        "SM_Hatch_Membrane_Lifepod",
    ],
}


# Mesh archetype hints fed to the renderer for camera framing.
# Slugs not listed default to "static" (no rig pose needed).
ARCHETYPES: dict[str, str] = {}
ARCHETYPES.update({s: "vehicle" for s in VEHICLES})
ARCHETYPES.update({s: "creature" for s in FAUNA})
ARCHETYPES.update({s: "static" for s in FLORA})
ARCHETYPES.update({s: "static" for s in RESOURCES})
ARCHETYPES.update({s: "static" for s in ITEMS})


CATALOG: dict[str, str] = {
    **VEHICLES, **FAUNA, **FLORA, **RESOURCES, **ITEMS,
}


def load_catalog_from_discovery(path: str | None = None) -> dict[str, str]:
    """Convenience loader for ad-hoc renders. Reads out/mesh_discovery.json
    and returns its `{cat: {slug: pkg}}` payload as a flat slug->pkg dict.

    The hand-curated CATALOG above is the production source of truth.
    This helper exists so the renderer / smoke tests can render anything
    discovered even before manual curation.
    """
    import json
    p = path or os.path.join(config.OUTPUT_DIR, "mesh_discovery.json")
    if not os.path.exists(p):
        return {}
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    flat: dict[str, str] = {}
    for _cat, slugs in data.items():
        flat.update(slugs)
    return flat


# ---------------------------------------------------------------------------
# CUE4Parse mesh export
# ---------------------------------------------------------------------------


def _import_exporter_types():
    """Import CUE4Parse-Conversion .NET types via pythonnet (lazy).

    Also initializes Detex - required for SN2 vehicle / hero meshes whose
    materials reference virtual-texture (VT) format textures. Without
    Detex, MaterialExporter2 fails with 'Detex decompression failed: not
    initialized' and the .glb has no PNG textures alongside it.
    """
    import clr  # noqa: F401
    clr.AddReference("CUE4Parse-Conversion")

    from CUE4Parse_Conversion import ExporterOptions  # noqa: E402
    from CUE4Parse_Conversion.Meshes import MeshExporter, EMeshFormat  # noqa: E402
    from CUE4Parse_Conversion.Textures.BC import DetexHelper  # noqa: E402

    # `ENaniteMeshFormat` lives in CUE4Parse (not -Conversion). Import it so
    # we can opt into Nanite-LOD extraction — SN2 ships ~80% of static meshes
    # as Nanite-only, and without this option CUE4Parse's `TryConvert`
    # silently returns 0 LODs for them. The enum's first value is
    # `OnlyNaniteLOD`, so we must set the field explicitly (struct zero-init
    # from pythonnet otherwise gives `OnlyNormalLODs` and skips Nanite).
    ENaniteMeshFormat = None
    try:
        clr.AddReference("CUE4Parse")
        from CUE4Parse.UE4.Assets.Exports.Nanite import (  # type: ignore
            ENaniteMeshFormat as _ENMF,
        )
        ENaniteMeshFormat = _ENMF
    except Exception as e:
        logger.warning("ENaniteMeshFormat import failed: %s — static Nanite meshes will be skipped", e)

    # Locate Detex.dll - it ships as an embedded resource inside
    # CUE4Parse-Conversion.dll, plus a sibling copy in
    # CUE4Parse/CUE4Parse-Conversion/Resources/Detex.dll
    detex_candidates = [
        os.path.join(config.CUE4PARSE_DLL_DIR, "Detex.dll"),
        os.path.join(os.path.dirname(config.CUE4PARSE_DLL_DIR), "..", "..", "..",
                     "CUE4Parse-Conversion", "Resources", "Detex.dll"),
        r"A:\Python\CUE4Parse\CUE4Parse-Conversion\Resources\Detex.dll",
    ]
    detex_path = None
    for cand in detex_candidates:
        if os.path.exists(cand):
            detex_path = cand
            break
    if detex_path is not None:
        try:
            DetexHelper.Initialize(detex_path)
            logger.info("Detex initialised from %s", detex_path)
        except Exception as e:
            logger.warning("Detex Initialize failed: %s", e)
    else:
        try:
            DetexHelper.LoadDll(None)  # extracts embedded resource to cwd
            DetexHelper.Initialize("Detex.dll")
            logger.info("Detex initialised from embedded resource")
        except Exception as e:
            logger.warning("Detex bootstrap failed: %s", e)

    ELodFormat = None
    for path in ("CUE4Parse_Conversion.Meshes", "CUE4Parse_Conversion"):
        try:
            mod = __import__(path, fromlist=["ELodFormat"])
            ELodFormat = getattr(mod, "ELodFormat", None)
            if ELodFormat is not None:
                break
        except Exception:
            pass

    return ExporterOptions, MeshExporter, EMeshFormat, ELodFormat, ENaniteMeshFormat


def _pkg_to_load_path(pkg: str) -> str:
    """Translate /Game/Foo/Bar -> Subnautica2/Content/Foo/Bar."""
    if pkg.startswith("/Game/"):
        return "Subnautica2/Content/" + pkg[len("/Game/"):]
    return pkg


def _find_mesh_export(package):
    """Pick the first SkeletalMesh / StaticMesh export from a package."""
    for export in package.GetExports():
        type_name = type(export).__name__
        if "SkeletalMesh" in type_name or "StaticMesh" in type_name:
            return export
    return None


def export_one(provider: "DefaultFileProvider", slug: str, pkg_path: str) -> str | None:
    """Export a single mesh slug. Returns path to the .glb on success."""
    ExporterOptions, MeshExporter, EMeshFormat, ELodFormat, ENaniteMeshFormat = _import_exporter_types()

    load_path = _pkg_to_load_path(pkg_path)
    try:
        ok, package = provider.TryLoadPackage(load_path)
    except Exception as e:
        logger.warning("[%s] load failed: %s", slug, e)
        return None
    if not ok or package is None:
        logger.warning("[%s] package missing: %s", slug, load_path)
        return None

    mesh = _find_mesh_export(package)
    if mesh is None:
        logger.warning("[%s] no SkeletalMesh / StaticMesh in package %s", slug, pkg_path)
        return None

    out_dir = os.path.join(_OUT_ROOT, slug)
    os.makedirs(out_dir, exist_ok=True)

    options = ExporterOptions()
    if EMeshFormat is not None:
        for cand in ("Gltf2", "Gltf"):
            if hasattr(EMeshFormat, cand):
                options.MeshFormat = getattr(EMeshFormat, cand)
                break
    if ELodFormat is not None and hasattr(ELodFormat, "FirstLod"):
        options.LodFormat = ELodFormat.FirstLod
    options.ExportMorphTargets = False
    options.ExportMaterials = True
    # Enable Nanite mesh extraction. SN2 ships most SM_* statics as
    # Nanite-only (no normal LODs). Without this option CUE4Parse's
    # `TryConvert` returns 0 LODs and the export fails. Default is
    # `OnlyNormalLODs` (struct zero-init from pythonnet) — we override
    # to `AllLayersNaniteFirst` so we get Nanite geometry when present
    # but still keep any pre-baked normal LOD as a fallback.
    if ENaniteMeshFormat is not None:
        for cand in ("AllLayersNaniteFirst", "OnlyNaniteLOD"):
            if hasattr(ENaniteMeshFormat, cand):
                options.NaniteMeshFormat = getattr(ENaniteMeshFormat, cand)
                break

    try:
        exporter = MeshExporter(mesh, options)
    except Exception as e:
        logger.warning("[%s] exporter ctor failed: %s", slug, e)
        return None

    # The C# MeshExporter has two signatures of TryWriteToDir and several
    # call paths internally raise on missing materials / textures. Try the
    # direct call first, then walk MeshLods manually as a fallback.
    ok2 = False
    written_dir = out_dir
    written_file = None
    try:
        ok2, written_dir, written_file = exporter.TryWriteToDir(
            __import__("System.IO", fromlist=["DirectoryInfo"]).DirectoryInfo(out_dir),
            "", "",
        )
    except Exception as e:
        logger.info("[%s] TryWriteToDir raised (%s); falling back to MeshLods", slug, str(e)[:200])

    if not ok2:
        # Manual fallback: walk LODs and write bytes directly
        try:
            for lod in exporter.MeshLods:
                fname = str(lod.FileName)
                data = bytes(lod.FileData)
                fpath = os.path.join(out_dir, fname)
                os.makedirs(os.path.dirname(fpath), exist_ok=True)
                with open(fpath, "wb") as f:
                    f.write(data)
                if fname.lower().endswith((".glb", ".gltf")):
                    written_file = fpath
                    ok2 = True
        except Exception as e2:
            logger.warning("[%s] manual fallback failed: %s", slug, str(e2)[:200])
            return None

    if not ok2:
        logger.warning("[%s] export reported failure", slug)
        return None

    glb_path = None
    if written_file:
        candidate = str(written_file)
        if os.path.isabs(candidate) and os.path.exists(candidate):
            glb_path = candidate
        else:
            cand2 = os.path.join(str(written_dir or out_dir), candidate)
            if os.path.exists(cand2):
                glb_path = cand2
    if glb_path is None:
        for root, _dirs, files in os.walk(out_dir):
            for fn in files:
                if fn.lower().endswith((".glb", ".gltf")):
                    glb_path = os.path.join(root, fn)
                    break
            if glb_path:
                break

    if glb_path is None:
        logger.warning("[%s] export wrote no .glb / .gltf", slug)
        return None

    # Always run the fallback texture extractor - it's cheap and fills in
    # textures for materials MaterialExporter2 can't handle (VT, Substrate,
    # custom shading models). Idempotent: skips textures already on disk.
    try:
        from meshes.texture_fallback import write_fallback_textures
        saved = write_fallback_textures(provider, mesh, out_dir, slug=slug)
        if saved == 0:
            logger.info("[%s] fallback textures: none added", slug)
    except Exception as e:
        logger.warning("[%s] fallback extraction errored: %s", slug, str(e)[:200])

    logger.info("[%s] -> %s", slug, glb_path)
    return glb_path


def _resolve_catalog() -> dict[str, str]:
    """Use CATALOG if non-empty, otherwise fall back to discovered meshes."""
    if CATALOG:
        return CATALOG
    return load_catalog_from_discovery()


def export_slugs(slugs: list[str]) -> dict[str, str]:
    """Export a list of slugs. Returns {slug: glb_path} for successes."""
    from provider import create_provider

    catalog = _resolve_catalog()
    unknown = [s for s in slugs if s not in catalog]
    if unknown:
        logger.warning("Unknown slugs ignored: %s", unknown[:5])

    targets = {s: catalog[s] for s in slugs if s in catalog}
    if not targets:
        logger.warning("Nothing to export.")
        return {}

    # SN2 ships most static meshes as Nanite-only - need read_nanite=True
    # so UStaticMesh.TryConvert finds its LODs. Also disable
    # SkipReferencedTextures so MaterialExporter2 can decode VT textures.
    provider = create_provider(read_nanite=True, skip_textures=False)

    out: dict[str, str] = {}
    for slug, pkg in targets.items():
        glb = export_one(provider, slug, pkg)
        if glb:
            out[slug] = glb
    logger.info("Mesh export complete: %d/%d ok", len(out), len(targets))
    return out


def export_all(filter_substr: str | None = None) -> dict[str, str]:
    catalog = _resolve_catalog()
    slugs = list(catalog.keys())
    if filter_substr:
        f = filter_substr.lower()
        slugs = [s for s in slugs if f in s.lower()]
    return export_slugs(slugs)
