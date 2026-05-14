"""Extract Subnautica 2 vehicle data.

Pulls from three sources to produce a richer view than the wiki had before:

1. **`Data/VehicleChassis/DA_*_TadpoleChassis`** (UWEVehicleChassisData)
     Chassis profile — acceleration, movement type, granted abilities,
     publication status (e.g. "For EA 1.1"). One DA per chassis variant.

2. **`Blueprints/Vehicle/BP_Tadpole`** (and BP_Trident parts)
     The actual actor BP. Carries propeller params, power-cell item, slot
     names, and component references that say what inventory slots exist.

3. **String tables** (ST_Tadpole etc.)
     Polished display names + tooltips when available.

Output: `out/vehicles.json` as a list of {id, slug, name, description,
chassis variants[], stats{}, source, image_path}.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from helpers import (
    find_export, prop_array, prop_bool, prop_enum, prop_float, prop_int,
    prop_object_path, prop_str, safe_load_package, short_name_from_path,
)

logger = logging.getLogger(__name__)

CHASSIS_DIR_PREFIX = "Subnautica2/Content/Data/VehicleChassis/"

# Explicit ordering so the wiki always renders vehicles in a stable, sensible
# order regardless of file-system iteration. Tadpole first (starter sub),
# then chassis variants in capability order, then the larger Trident.
VEHICLE_ORDER = (
    "Tadpole",
    "Tadpole_ScoutRay",
    "Tadpole_Haul",
    "Tadpole_Seafrog",
    "Trident",
    "Lifepod",
)


def _extract_chassis(provider, asset_path: str) -> dict | None:
    """Pull a UWEVehicleChassisData asset → dict."""
    pkg_path = asset_path[:-7]  # strip ".uasset"
    package = safe_load_package(provider, pkg_path)
    if package is None:
        return None
    export = find_export(package, class_substring="UWEVehicleChassisData")
    if export is None:
        return None
    return {
        "id": short_name_from_path(pkg_path),
        "asset": pkg_path,
        # Capsule + collision tuning
        "adjust_capsule_size": prop_bool(export, "bAdjustCapsuleSize"),
        "capsule_half_height": prop_float(export, "CapsuleHalfHeight"),
        "use_soft_collision": prop_bool(export, "bUseSoftCollision"),
        # Input
        "input_mapping_context": prop_object_path(export, "InputMappingContext"),
        # Movement
        "movement_type": prop_enum(export, "MovementType") or "Vehicle",
        "max_swim_acceleration": prop_float(export, "MaxSwimAcceleration"),
        "max_walk_acceleration": prop_float(export, "MaxWalkAcceleration"),
        "angular_acceleration": prop_float(export, "AngularAcceleration"),
        "swimming_friction": prop_float(export, "SwimmingFriction"),
        "strafe_speed_modifier": prop_float(export, "StrafeSpeedModifier"),
        # Gameplay handles toggle (the rear "grab" position)
        "disable_attach_handles": prop_bool(export, "bDisableAttachHandles"),
        # Granted gameplay effects + abilities (array of soft refs)
        "granted_abilities_count": len(prop_array(export, "GrantedAbilities")),
        "granted_effects_count": len(prop_array(export, "GrantedEffects")),
        # Roadmap status
        "published_status": prop_enum(export, "PublishedStatus") or "Published",
        "developer_note": prop_str(export, "DeveloperNote"),
    }


def _extract_tadpole_bp(provider) -> dict | None:
    """Read the BP_Tadpole CDO for the shared chassis fields (power cell,
    crush-depth warning, propeller params). Returns a single dict the
    wiki blends into the base Tadpole entry."""
    pkg_path = "/Game/Blueprints/Vehicle/BP_Tadpole"
    package = safe_load_package(provider, pkg_path)
    if package is None:
        return None
    cdo = None
    for ex in package.GetExports():
        if str(ex.Name).startswith("Default__"):
            cdo = ex
            break
    if cdo is None:
        return None
    # Inventory components are nested objects on the BP — walk the package
    # for the named exports to read their MaxItems/Columns/InventoryName props.
    upgrade_inv = _find_named_export(package, "UpgradeInventoryComponent")
    power_inv = _find_named_export(package, "PowerInventoryComponent")
    return {
        "power_cell_item": prop_object_path(cdo, "PowerCellItemType"),
        "crush_depth_warning_delay_s": prop_float(cdo, "CrushDepthWarningDelay"),
        "default_strafe_speed_modifier": prop_float(cdo, "DefaultStrafeSpeedModifier"),
        "angular_acceleration_default": prop_float(cdo, "AngularAcceleration"),
        "propeller_pivot_weight": prop_float(cdo, "PropellerPivotWeight"),
        "propeller_rotation_main": prop_float(cdo, "PropellerRotationMultiplierMain"),
        "propeller_rotation_sides": prop_float(cdo, "PropellerRotationMultiplierSides"),
        "propeller_rotation_bottom": prop_float(cdo, "PropellerRotationMultiplierBottom"),
        "pilot_attach_slot": prop_str(cdo, "PilotAttachSlot"),
        # New: inventory layout. Pulled from the UWEInventoryComponent
        # sub-exports the BP has bound under named slots. Power Cells is
        # always 1×1 (Subnautica's classic power cell rule). Upgrades varies
        # by chassis (Tadpole base = 4 slots, 2×2 grid).
        "upgrade_slots": prop_int(upgrade_inv, "MaxItems") if upgrade_inv else None,
        "upgrade_columns": prop_int(upgrade_inv, "Columns") if upgrade_inv else None,
        "upgrade_inventory_label": prop_str(upgrade_inv, "InventoryName") if upgrade_inv else None,
        "power_cell_slots": prop_int(power_inv, "MaxItems") if power_inv else None,
        "power_cell_columns": prop_int(power_inv, "Columns") if power_inv else None,
    }


def _find_named_export(package, name_substring: str):
    """Return the first export whose .Name contains the substring."""
    for ex in package.GetExports():
        if name_substring in str(ex.Name):
            return ex
    return None


def find_chassis_paths(provider) -> list[str]:
    out = []
    for path in provider.Files.Keys:
        if path.startswith(CHASSIS_DIR_PREFIX) and path.endswith(".uasset"):
            out.append(path)
    return sorted(out)


def run(provider) -> list[dict[str, Any]]:
    """Top-level entry — read every chassis + the Tadpole BP, normalise."""
    chassis_paths = find_chassis_paths(provider)
    logger.info("Vehicles: %d chassis DAs found", len(chassis_paths))

    chassis_list: list[dict] = []
    for p in chassis_paths:
        ch = _extract_chassis(provider, p)
        if ch:
            chassis_list.append(ch)

    tadpole_bp = _extract_tadpole_bp(provider) or {}

    # Compose vehicles. SN2 ships ONE primary vehicle (Tadpole) with chassis
    # swap variants. We list each chassis as a separate vehicle entry so the
    # wiki can render a card per chassis, but link them all to the same BP.
    vehicles: list[dict] = []

    # Base Tadpole (no chassis DA — uses default movement params on the BP).
    vehicles.append({
        "id": "Tadpole",
        "slug": "tadpole",
        "name": "Tadpole",
        "category": "Submersible",
        "chassis_variant": "Base",
        "description": "Single-seat submersible. Modular hull, chassis swappable for combat / cargo / scout role.",
        "status": "revealed",
        "movement_type": "Vehicle",
        "stats": {
            "max_swim_acceleration": 1000.0,  # base BP default
            "angular_acceleration": tadpole_bp.get("angular_acceleration_default"),
            "swimming_friction": 0.25,
            "strafe_speed_modifier": tadpole_bp.get("default_strafe_speed_modifier"),
        },
        "bp_extras": tadpole_bp,
        "source_asset": "/Game/Blueprints/Vehicle/BP_Tadpole",
    })

    # Each Tadpole chassis variant.
    chassis_metadata = {
        "DA_Haul_TadpoleChassis": {
            "name": "Tadpole · Haul Chassis",
            "category": "Tadpole Chassis",
            "description": "Cargo-focused Tadpole chassis. Extra storage capacity, scales from solo runs to small co-op cargo hauls.",
            "status": "post-launch",
        },
        "DA_Seafrog_TadpoleChassis": {
            "name": "Tadpole · Seafrog Chassis",
            "category": "Tadpole Chassis",
            "description": "Walker variant of the Tadpole. Exosuit-style movement lets you traverse the seabed on foot.",
            "status": "post-launch",
        },
        "DA_ScoutRay_TadpoleChassis": {
            "name": "Tadpole · ScoutRay Chassis",
            "category": "Tadpole Chassis",
            "description": "High-speed scout chassis. Highest swim acceleration of any Tadpole variant.",
            "status": "post-launch",
        },
    }

    for ch in chassis_list:
        meta = chassis_metadata.get(ch["id"]) or {
            "name": ch["id"].replace("DA_", "").replace("_", " "),
            "category": "Tadpole Chassis",
            "description": "",
            "status": "post-launch",
        }
        slug = ch["id"].replace("DA_", "").replace("_TadpoleChassis", "")
        if slug == "ScoutRay":
            slug = "tadpole-scoutray"
        elif slug == "Haul":
            slug = "tadpole-haul"
        elif slug == "Seafrog":
            slug = "tadpole-seafrog"
        else:
            slug = slug.lower().replace("_", "-")
        vehicles.append({
            "id": ch["id"],
            "slug": slug,
            "name": meta["name"],
            "category": meta["category"],
            "chassis_variant": ch["id"].replace("DA_", "").replace("_TadpoleChassis", ""),
            "description": meta["description"],
            "status": meta["status"],
            "developer_note": ch.get("developer_note") or "",
            "movement_type": ch.get("movement_type", "Vehicle"),
            "stats": {
                "max_swim_acceleration": ch.get("max_swim_acceleration"),
                "max_walk_acceleration": ch.get("max_walk_acceleration"),
                "angular_acceleration": ch.get("angular_acceleration"),
                "swimming_friction": ch.get("swimming_friction"),
                "strafe_speed_modifier": ch.get("strafe_speed_modifier"),
                "capsule_half_height": ch.get("capsule_half_height"),
                "granted_abilities": ch.get("granted_abilities_count"),
                "granted_effects": ch.get("granted_effects_count"),
            },
            "source_asset": ch["asset"],
        })

    # Trident (post-launch large sub). We don't have a chassis DA for it yet —
    # the BP is just the in-game prototype. Mark it as teased.
    vehicles.append({
        "id": "Trident",
        "slug": "trident",
        "name": "Trident",
        "category": "Submarine",
        "chassis_variant": None,
        "description": "Large multi-crew submarine successor to Subnautica 1's Cyclops. Solo-first balance with optional crew slots. Not in the May 14 EA launch build.",
        "status": "post-launch",
        "movement_type": "Submarine",
        "stats": {},
        "source_asset": "/Game/Blueprints/Vehicle/Trident/BP_TridentEngine",
    })

    # Lifepod (starting shelter — technically a vehicle in the data sense).
    vehicles.append({
        "id": "Lifepod",
        "slug": "lifepod",
        "name": "Lifepod (Rosette V)",
        "category": "Survival Shelter",
        "chassis_variant": None,
        "description": "Starting survival shelter. ROSETTE V Rigid Hull Survival Shelter, ISO-17F9650-X compliant. Drops from CICADA at the start of the campaign.",
        "status": "revealed",
        "movement_type": "Static",
        "stats": {},
        "source_asset": "/Game/Blueprints/Vehicle/Lifepod/BP_StaticLifepod",
    })

    # Stable display order
    order_index = {name: i for i, name in enumerate(VEHICLE_ORDER)}
    vehicles.sort(key=lambda v: order_index.get(v["id"], 99))

    logger.info("Vehicles: extracted %d entries", len(vehicles))
    return vehicles
