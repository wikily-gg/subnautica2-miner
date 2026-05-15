"""Probe a stimulus sensor DA in detail to learn its struct shape."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import logging
logging.basicConfig(level=logging.WARNING)

from provider import create_provider
from helpers import (
    safe_load_package, find_export, prop_array, unwrap_struct, prop,
    _export_class, extract_gameplay_tags,
)
prov = create_provider()

# Pick a few sensor assets
ASSETS = [
    'Subnautica2/Content/Data/Stimulus/DA_SmallFishEyesAndEars',
    'Subnautica2/Content/Data/Stimulus/DA_BigFishEyesAndEars',
    'Subnautica2/Content/Data/Stimulus/DA_BulletheadEyesAndEars1',
    'Subnautica2/Content/Data/Stimulus/DA_ProximitySensors',
]

for path in ASSETS:
    print(f"\n========== {path} ==========")
    pkg = safe_load_package(prov, path)
    if pkg is None:
        print("  FAILED")
        continue
    ex = find_export(pkg, class_substring="UWEStimulusDataAsset")
    if ex is None:
        print("  no UWEStimulusDataAsset export")
        continue
    sensors = prop_array(ex, "Sensors")
    print(f"  Sensors raw count: {len(sensors)}")
    for i, el in enumerate(sensors):
        gv = getattr(el, "GenericValue", el)
        print(f"\n  [SENSOR {i}] gv type: {type(gv).__name__}")
        # Try unwrap
        u = unwrap_struct(gv)
        if u is None:
            print(f"    unwrap returned None; gv repr: {repr(gv)[:300]}")
            # Try direct props
            direct_props = getattr(gv, "Properties", None)
            if direct_props:
                print(f"    direct.Properties exists, len={len(list(direct_props))}")
            # Try StructType
            st = getattr(gv, "StructType", None)
            print(f"    .StructType = {st!r}")
            if st is not None:
                st_props = getattr(st, "Properties", None)
                if st_props:
                    print("    via StructType.Properties:")
                    for tag in st_props:
                        n = tag.Name.Text
                        v = tag.Tag.GenericValue if tag.Tag is not None else None
                        s = repr(v)
                        if len(s) > 300:
                            s = s[:300] + "..."
                        print(f"      {n} = {s}")
                else:
                    # Try reflection
                    try:
                        t = st.GetType()
                        for prp in t.GetProperties():
                            try:
                                v = prp.GetValue(st)
                                print(f"      [reflected] {prp.Name} = {repr(v)[:200]}")
                            except Exception as e:
                                pass
                    except Exception as e:
                        print(f"    reflection failed: {e}")
            continue
        # Yay, got Properties
        props = getattr(u, "Properties", None)
        if props is None:
            print("    unwrapped but no Properties")
            continue
        for tag in props:
            n = tag.Name.Text
            v = tag.Tag.GenericValue if tag.Tag is not None else None
            s = repr(v)
            if len(s) > 300:
                s = s[:300] + "..."
            print(f"      {n} = {s}")
        if i >= 4:
            print("    (truncated)")
            break
