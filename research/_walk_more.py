"""More walks: ScareCreatures, EmitSpores, FoodBank, CreatureFollow."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import logging
logging.basicConfig(level=logging.WARNING)
from provider import create_provider
prov = create_provider()

PATTERNS = [
    "ScareCreatures", "EmitSpores", "Chum", "FoodBank", "CreatureFollow",
    "Hypnoti", "FleeStimulus", "AttractedStimulus", "AttractFood",
    "AttractedFood", "Stalker", "GrabOnDash", "GrabbedByCreature",
    "Repulse", "Push", "DistractFood",
]

for kw in PATTERNS:
    print(f"\n=== {kw} ===")
    for path in sorted(prov.Files.Keys):
        if path.endswith(".uasset") and kw.lower() in path.lower():
            print(f"  {path}")
