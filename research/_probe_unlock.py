"""Why is UnlockCost an FStructFallback?"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import logging
logging.basicConfig(level=logging.WARNING)

from provider import create_provider
from helpers import (
    safe_load_package, find_export, prop, unwrap_struct, prop_int,
)
prov = create_provider()

for path in [
    'Subnautica2/Content/Data/Biomods/BioAbilityData/Positive/DA_CreatureStunOnStop_BioAbilityData1',
    'Subnautica2/Content/Data/Biomods/BioAbilityData/Positive/DA_CreatureFollowOnEatOrDrink_BioAbilityData',
    'Subnautica2/Content/Data/Biomods/BioAbilityData/Active/DA_EmitSpores_ActiveBioAbility',
]:
    pkg = safe_load_package(prov, path)
    if pkg is None:
        print(f"FAIL load {path}")
        continue
    ex = find_export(pkg, class_substring="UWEBioAbilityData")
    if ex is None:
        continue
    uc = prop(ex, "UnlockCost")
    print(f"\n{path}")
    print(f"  UnlockCost raw: {uc!r}")
    u = unwrap_struct(uc)
    if u:
        for tag in getattr(u, "Properties", []) or []:
            print(f"    {tag.Name.Text} = {tag.Tag.GenericValue!r}")
    # Also AbilityTag, PublishedStatus
    pub = prop(ex, "PublishedStatus")
    print(f"  PublishedStatus: {pub!r}")
    bat = prop(ex, "BioAbilityType")
    print(f"  BioAbilityType: {bat!r}")
