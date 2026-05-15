"""Dump GE and GA assets for the relevant player biomods."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import logging
logging.basicConfig(level=logging.WARNING)

from provider import create_provider
from helpers import safe_load_package, _export_class
prov = create_provider()

ASSETS = [
    'Subnautica2/Content/Blueprints/AbilitySystem/Abilities/BioAbilities/Active/GA_EmitSpores_ActiveBioAbility',
    'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Biomods/BioEffects/GE_EmitSporesActiveCooldown',
    'Subnautica2/Content/GameplayCueNotifies/Biomods/GC_EmitSpores_Burst_ActiveBioAbility',
    'Subnautica2/Content/GameplayCueNotifies/Biomods/GC_EmitSpores_Looping_ActiveBioAbility',
    'Subnautica2/Content/Blueprints/AbilitySystem/Abilities/BioAbilities/Positive/GA_CreatureFollowAfterEatOrDrink_BioAbility',
    'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Biomods/BioEffects/GE_CreatureFollowOnEatOrDrink',
    'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Stimulus/Biomods/GE_ElectrostaticDischarge_Stimulus',
    'Subnautica2/Content/Blueprints/AI/Agents/Common/Behaviors/LargeCreature/BT_LargeCreatureFollow',
    'Subnautica2/Content/Blueprints/AI/Agents/Common/Behaviors/LargeCreature/BT_SmallCreatureFollowLarge',
    'Subnautica2/Content/Blueprints/AbilitySystem/Abilities/BioAbilities/Positive/CreatureStun/M_CreatureStunBubbleTempDoubleSided',
]

for path in ASSETS:
    print(f"\n========== {path} ==========")
    pkg = safe_load_package(prov, path)
    if pkg is None:
        print("  FAILED TO LOAD")
        continue
    for ex in pkg.GetExports():
        cls = _export_class(ex)
        name = str(ex.Name)
        print(f"\n  EXPORT name={name} class={cls}")
        props = getattr(ex, "Properties", None)
        if props is None:
            print("    (no Properties)")
            continue
        for tag in props:
            tag_name = tag.Name.Text
            val = tag.Tag.GenericValue if tag.Tag is not None else None
            s = repr(val)
            if len(s) > 500:
                s = s[:500] + "...(trunc)"
            print(f"    {tag_name} = {s}")
