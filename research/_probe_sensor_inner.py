"""Drill into Shape + StimulusTypeTags of one sensor."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import logging
logging.basicConfig(level=logging.WARNING)

from provider import create_provider
from helpers import (
    safe_load_package, find_export, prop_array, unwrap_struct, prop,
    extract_gameplay_tags,
)
prov = create_provider()

pkg = safe_load_package(prov, 'Subnautica2/Content/Data/Stimulus/DA_SmallFishEyesAndEars')
ex = find_export(pkg, class_substring="UWEStimulusDataAsset")
sensors = prop_array(ex, "Sensors")

for i, el in enumerate(sensors):
    gv = getattr(el, "GenericValue", el)
    u = unwrap_struct(gv)
    print(f"\nSENSOR {i}")

    shape = prop(u, "Shape")
    print(f"  Shape generic: {shape!r}")
    su = unwrap_struct(shape)
    if su is not None:
        for tag in getattr(su, "Properties", []) or []:
            print(f"    Shape.{tag.Name.Text} = {tag.Tag.GenericValue!r}")
        # Try reflection on shape's struct type
        st = getattr(shape, "StructType", None)
        if st is not None:
            try:
                t = st.GetType()
                print(f"    Shape struct type name: {t.Name}")
                for prp in t.GetProperties()[:10]:
                    try:
                        v = prp.GetValue(st)
                        print(f"      [refl] {prp.Name} = {v!r}")
                    except Exception:
                        pass
            except Exception as e:
                print(f"    refl err: {e}")

    stt = prop(u, "StimulusTypeTags")
    print(f"  StimulusTypeTags generic: {stt!r}")
    print(f"  has StructType: {getattr(stt, 'StructType', None)!r}")
    print(f"  has GameplayTags attr: {getattr(stt, 'GameplayTags', '__missing__')!r}")
    print(f"  has TagName attr: {getattr(stt, 'TagName', '__missing__')!r}")
    print(f"  str(stt): {str(stt)[:200]!r}")
    print(f"  extract_gameplay_tags result: {extract_gameplay_tags(stt)!r}")
    sttu = unwrap_struct(stt)
    print(f"  unwrap result: {sttu!r}")
    if sttu is not None:
        props = list(getattr(sttu, "Properties", []) or [])
        print(f"    has {len(props)} properties")
        for tag in props:
            v = tag.Tag.GenericValue if tag.Tag is not None else None
            print(f"    StimulusTypeTags.{tag.Name.Text} = {v!r}")
            # Drill into GameplayTags array
            if tag.Name.Text in ("GameplayTags",):
                # It's an FScriptArray of FGameplayTag
                if hasattr(v, "__iter__"):
                    for sub in v:
                        sgv = getattr(sub, "GenericValue", sub)
                        print(f"      ITEM: {sgv!r}")
                        sub_u = unwrap_struct(sgv)
                        if sub_u:
                            for st in getattr(sub_u, "Properties", []) or []:
                                print(f"        {st.Name.Text} = {st.Tag.GenericValue!r}")
        tags = extract_gameplay_tags(stt)
        print(f"    extract_gameplay_tags: {tags}")
        # Try sttu directly
        from helpers import prop_tags
        more_tags = prop_tags(sttu, "GameplayTags")
        print(f"    prop_tags(GameplayTags): {more_tags}")
