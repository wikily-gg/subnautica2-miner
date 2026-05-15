"""Dump exports + properties for a list of candidate assets."""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.WARNING)

from provider import create_provider
from helpers import safe_load_package, _export_class

prov = create_provider()

ASSETS = [
    # The lure/stim leads
    'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Creatures/Sandspear/GE_Sandspear_LureStimulus',
    'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Creatures/Sandspear/GE_Sandpear_Stun',
    'Subnautica2/Content/Blueprints/AI/Agents/LargeCreature021_Sandspear/Services/BTService_Sandspear_LureStimulus',
    'Subnautica2/Content/Blueprints/AI/Agents/Common/Behaviors/SmallCreature/BT_SmallCreatureLure',
    'Subnautica2/Content/GameplayCueNotifies/AI/Sandspear/GC_Sandspear_Lure',

    # Stunner effects on different creatures
    'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Creatures/GE_Cerathecan_StunPrey',
    'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Creatures/TwinEels/GE_TwinEels_StunPrey',
    'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Creatures/TwinEels/GE_TwinEels_StunVehicle',
    'Subnautica2/Content/Blueprints/AbilitySystem/Abilities/AI/GA_AI_Cerathecan_Stun',
    'Subnautica2/Content/Blueprints/AbilitySystem/Abilities/AI/GA_AI_NeedlerShark_Shoot_Stun',
    'Subnautica2/Content/Blueprints/AbilitySystem/Abilities/AI/GA_AI_TwinEels_SingleStun',
    'Subnautica2/Content/Blueprints/AI/Agents/Common/Behaviors/BT_Stunned',
    'Subnautica2/Content/Blueprints/AI/Agents/LargeCreature010_JetoCaris/Behaviours/BT_JetoCaris_Stunned',

    # Player-side biomod stun
    'Subnautica2/Content/Blueprints/AbilitySystem/Abilities/BioAbilities/Positive/GA_CreatureStunOnStop_BioAbility',
    'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Biomods/BioEffects/GE_CreatureStunShortTime',
    'Subnautica2/Content/Data/Biomods/BioAbilityData/Positive/DA_CreatureStunOnStop_BioAbilityData1',
    'Subnautica2/Content/GameplayCueNotifies/Biomods/GC_StunFieldLooping',

    # Stimulus taxonomy
    'Subnautica2/Content/Data/Stimulus/DA_BigFishEyesAndEars',
    'Subnautica2/Content/Data/Stimulus/DA_SmallFishEyesAndEars',
    'Subnautica2/Content/Data/Stimulus/DA_MediumFishEyesAndEars',
    'Subnautica2/Content/Data/Stimulus/DA_HugeFishEyesAndEars',
    'Subnautica2/Content/Data/Stimulus/DA_BulletheadEyesAndEars1',
    'Subnautica2/Content/Data/Stimulus/DA_ClamthuluSensors',
    'Subnautica2/Content/Data/Stimulus/DA_FieldSensors',
    'Subnautica2/Content/Data/Stimulus/DA_GlowShroomSensors',
    'Subnautica2/Content/Data/Stimulus/DA_OxygenPlantSensors',
    'Subnautica2/Content/Data/Stimulus/DA_ProximitySensors',
    'Subnautica2/Content/Data/Stimulus/DA_TendrilSensors',

    # Generic stimulus effects
    'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Stimulus/AI/GE_ContinuousFollowStimulus',
    'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Stimulus/AI/GE_SymbioteCallStimulus',
    'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Stimulus/AI/GE_AngelCombStimulus',
    'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Stimulus/GE_BreakNodeStimulus',
    'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Stimulus/GE_HitNodeStimulus',

    # Sonic resonator (looks like a player tool)
    'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Stimulus/Tools/GE_SonicResonatorBlastFleeStimulus',
    'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Stimulus/Tools/GE_SonicResonatorBlastSoundStimulus',
    'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Stimulus/Tools/GE_InfiniteStimulusCone_Flashlight',

    # Vehicle stimulus (Tadpole = sub)
    'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Stimulus/Vehicle/GE_TadpoleEngineStimulus',
    'Subnautica2/Content/Blueprints/AbilitySystem/Effects/Stimulus/Vehicle/GE_TadpoleLightStimulus',

    # Biomod 'ScareCreaturesOnDash' icon hint
    # No DA found yet, but search later
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
            if len(s) > 600:
                s = s[:600] + "...(trunc)"
            print(f"    {tag_name} = {s}")
