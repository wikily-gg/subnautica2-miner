"""Enumerate every asset path matching the lure/stun/decoy/etc keyword set."""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.WARNING)

from provider import create_provider

prov = create_provider()

KEYWORDS = [
    "Stasis", "Repulsion", "Propulsion", "Stun", "Knockout", "Subdue",
    "Pacify", "Calm", "Sedate", "Decoy", "Distract", "Frighten", "Scare",
    "Trap", "Net", "Snare", "Catch", "Bait", "Lure", "Beacon", "Sonar",
    "Pheromone", "Scent", "Stimulus",
]

by_kw: dict[str, list[str]] = {kw: [] for kw in KEYWORDS}

for path in prov.Files.Keys:
    if not path.endswith(".uasset"):
        continue
    for kw in KEYWORDS:
        if kw in path:
            by_kw[kw].append(path)

for kw, paths in by_kw.items():
    print(f"\n=== {kw}: {len(paths)} ===")
    for p in sorted(paths)[:200]:
        print(f"  {p}")
