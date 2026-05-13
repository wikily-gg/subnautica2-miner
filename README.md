# Subnautica 2 Data Miner

Static data extractor for **Subnautica 2** (Unknown Worlds Entertainment, UE 5.6).
Reads the game's `.pak` / `.ucas` / `.utoc` files via [CUE4Parse](https://github.com/FabianFG/CUE4Parse)
and exports structured JSON + map renders for wiki use.

> Built against the pre–Early Access build (5.6.1-112084). EA launches 2026-05-14.

## What it extracts

| Output | Rows | What it covers |
|---|---:|---|
| `items.json` | 329 | `UWEItemType` — every item with name, description, actor class |
| `recipes.json` | 430 + 656 | Crafting recipes + base-building actions, with `item_type` refs and counts |
| `databank.json` | 505 | Full PDA databank entries (title, text, image, story-goal unlocks) |
| `scan_data.json` | 395 | Scanner targets → databank links |
| `story_goals.json` | 515 | Story goal nodes |
| `creature_archetypes.json` | 51 | AI archetypes with gameplay tags, behaviour tree, enemies |
| `biomods.json` | 81 | Bio-abilities (Active / Passive), icons, descriptions |
| `resonatables.json` | 39 | Resource deposit definitions (drop counts, chances) |
| `characters.json` | 36 | Player customization items |
| `locations.json` | 218 | World POIs with XYZ coordinates and screenshots |
| `pings.json` | 33 | Beacon / ping definitions |
| `string_tables.json` | 215 | Localized UI text rows |
| `regions.json` | 16 | `UWEWorldRegionDataAsset` definitions (CG / AR sub-regions + tags) |
| `region_zones.json` | 204 | `UWEBoxWorldZone` axis-aligned region boxes |
| `world_map.json` | 14,740 | Placed actors with XYZ — resources, creatures, POIs, loot |
| `biome_points_v2.json` | 76,525 | Per-placement biome + sub-region classification |
| `world_boundaries.json` | — | 58 `BP_EdgeOfWorldVolume_C` wall slabs forming the playable perimeter |
| `landscape_heights.json` | 529 | UE5 Landscape component height extents (`CachedLocalBox`) |
| `pop_settings.json` | 8 | `UWEWorldPopCreaturePopulationDA` per-creature spawn caps |
| `region_loot.json` | 1 | Region-specific loot table |
| `landscape_mappings.json` | 13 | Biome surface material lists |
| `surface_spawn.json` | 54 | Spawn-point tag bindings |
| `static_gameplay_tags.json` | 58 | Tag bundles |
| `ability_sets.json` | 34 | Creature ability/effect grants |

## Map outputs

The renderers produce GeoJSON + PNG layers ready for HTML / Leaflet / SVG:

- **Biome map** — wall-chained perimeter polygon (117 vertices, 0 cm closure), 12 authoritative sub-regions (`CoralGardens.{Plateaus, Graveyard, …}`, `OvergrownRuins.{Observatory, PowerPlant, RootCanyon}`), organic boundaries via gaussian-smoothed point density + Voronoi gap fill.
- **Heightmap** — seafloor depth (~ -784 m to surface), built from placement-point Z values.
- **Terrain composite** — hillshaded relief multiplied by per-cell biome tint.
- **Wall outline** — exact closed polygon from the game's 58 `BP_EdgeOfWorldVolume_C` segments.

## Setup

```bash
# 1. Python 3.11+ venv
python -m venv .venv
.venv\Scripts\activate           # Windows
# source .venv/bin/activate      # Linux/macOS
pip install -r requirements.txt

# 2. Build CUE4Parse (https://github.com/FabianFG/CUE4Parse) for net8.0

# 3. Configure paths
copy .env.example .env           # then edit .env
```

Required env vars (in `.env` or shell):

| Var | Meaning |
|---|---|
| `SN2_PAKS_DIR` | Folder containing `Subnautica2-Windows.pak` / `.ucas` / `.utoc` |
| `SN2_CUE4PARSE_DLL_DIR` | Folder with built `CUE4Parse.dll` (net8.0) |
| `SN2_MAPPINGS_PATH` | `.usmap` file matching the game build |
| `SN2_OUTPUT_DIR` | (optional) where JSON / PNGs go — defaults to `./out` |

## Run

```bash
# All extractors (~5 min for the full 3,513-cell world sweep)
python run.py all

# Or one at a time
python run.py items
python run.py recipes
python run.py databank
python run.py creatures
python run.py biomods
python run.py characters
python run.py string_tables
python run.py regions            # zone boxes + edge walls + biome classification
python run.py world_map          # placement coordinates
python run.py biomes             # per-mesh biome via path regex (fallback method)

# Smoke test (~30 sec)
python run.py smoke
```

After extraction, build the maps:

```bash
python render_organic.py         # biome polygons (organic), GeoJSON + PNG
python render_zones.py           # raw region boxes (authoritative)
python render_heightmap.py       # seafloor depth + terrain composite
python render_map.py             # placement-cloud overview (resources, creatures, POIs)
python render_biomes.py          # alternate biome rendering (regex-based)
```

## Architecture

```
miner/
├── config.py            # env-var configuration
├── provider.py          # CUE4Parse provider setup (Oodle, mounting, mappings)
├── helpers.py           # property readers (FText / FVector / FGameplayTag / FBox via reflection)
├── run.py               # entry point — dispatches to extractors
├── extractors/
│   ├── items.py         # UWEItemType
│   ├── recipes.py       # UWECraftingRecipe + SN2BuilderConstructActionData
│   ├── databank.py      # UWEDatabankEntry / ScanData / StoryGoal / Pings / GotoLocations
│   ├── creatures.py     # UWEAIArchetypeDataAsset / AbilitySet / GameplayTags
│   ├── biomes.py        # Per-mesh biome via path regex (legacy)
│   ├── regions.py       # UWEBoxWorldZone + region defs + edge-of-world walls
│   ├── world_map.py     # All world-partition cell placements
│   ├── misc.py          # Biomods, resonatables, characters, surface spawn
│   └── string_tables.py # Localised UI text
├── render_zones.py      # Authoritative region-box maps
├── render_organic.py    # Organic biome polygons (gaussian + Voronoi fill)
├── render_heightmap.py  # Hillshaded seafloor + biome composite
├── render_map.py        # Placement-cloud overview
└── render_biomes.py     # Alternate biome render (regex method)
```

### Key technique notes

- **`unwrap_struct` + `_reflect_field`** in `helpers.py` — CUE4Parse exposes UE struct types
  via the `IUStruct` interface, which pythonnet hides; .NET reflection is used to read
  `FVector.X/Y/Z`, `FRotator.Pitch/Yaw/Roll`, `FBox.Min/Max`, `FGameplayTag.TagName`, etc.
- **Wall chain → playable polygon** — the 58 `BP_EdgeOfWorldVolume_C` segments chain
  head-to-tail with **0 cm gaps**; greedy nearest-endpoint walk produces an exact closed
  polygon (117 vertices) used as the strict outer mask.
- **Layered biome classifier** in `extractors/regions.py`:
  1. Point-in-AABB test against the 204 `UWEBoxWorldZone` boxes (most authoritative)
  2. `PrefabActor.PrefabComponent.PrefabAssetInterface` → biome from `/Biome/<X>/` path
  3. Static mesh path regex fallback
- **Heightmap** uses `min(Z)` of placement points per cell as a seafloor approximation,
  plus the actual UE5 `LandscapeComponent.CachedLocalBox` data for the underlying terrain.

## Legal

This tool **reads** the game's installed asset files for static-data extraction (item
stats, biome layout, etc.) — the same approach used by wiki contributors for every UE
game. It does not redistribute or modify game assets.

Game content (textures, models, audio, names, story text) is © Unknown Worlds
Entertainment. The miner code itself is released under MIT.

You must own a copy of Subnautica 2 to use this — point `SN2_PAKS_DIR` at your install's
`Paks` folder.

## License

MIT — see `LICENSE`.
