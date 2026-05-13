"""
Property readers for CUE4Parse UObject / FStructFallback exports.

Subnautica 2 uses UE 5.6.  All readers are tolerant of missing properties
and unwrap pythonnet/CUE4Parse type wrappers (FPropertyTagType, FText,
FGameplayTagContainer, FSoftObjectPath, etc).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ---------- low-level tag access ----------

def _find_tag(obj, key: str):
    props = getattr(obj, "Properties", None)
    if props is None:
        return None
    for tag in props:
        if tag.Name.Text == key:
            return tag
    return None


def _tag_value(tag) -> Any:
    if tag is None or tag.Tag is None:
        return None
    return tag.Tag.GenericValue


def prop(obj, key: str, default: Any = None) -> Any:
    tag = _find_tag(obj, key)
    val = _tag_value(tag)
    return val if val is not None else default


def prop_bool(obj, key: str, default: bool = False) -> bool:
    val = prop(obj, key)
    return bool(val) if val is not None else default


def prop_float(obj, key: str, default: float = 0.0) -> float:
    val = prop(obj, key)
    if val is None:
        return default
    try:
        return float(val)
    except Exception:
        return default


def prop_int(obj, key: str, default: int = 0) -> int:
    val = prop(obj, key)
    if val is None:
        return default
    try:
        return int(val)
    except Exception:
        return default


def prop_str(obj, key: str, default: str = "") -> str:
    val = prop(obj, key)
    if val is None:
        return default
    return _coerce_str(val) or default


def prop_enum(obj, key: str, default: str = "") -> str:
    val = prop(obj, key)
    if val is None:
        return default
    s = str(val)
    return s.split("::", 1)[-1] if "::" in s else s


# ---------- string coercion (FText, FName, etc) ----------

def _coerce_str(val) -> str:
    """Best-effort conversion of a CUE4Parse value to a plain string.

    Handles FText (LocalizedString/SourceString/CultureInvariantString) and
    falls back to .ToString()/str().
    """
    if val is None:
        return ""
    if isinstance(val, (str, bytes)):
        return val.decode() if isinstance(val, bytes) else val

    # FText shape — try every common form
    for attr in ("Text", "LocalizedString", "SourceString",
                 "CultureInvariantString", "Value", "Key", "TableId"):
        v = getattr(val, attr, None)
        if v is not None:
            s = str(v)
            if s and s != "None":
                return s

    s = str(val)
    if "CUE4Parse" in s or s == "None":
        return ""
    return s


# ---------- array property ----------

def prop_array(obj, key: str) -> list:
    tag = _find_tag(obj, key)
    if tag is None or tag.Tag is None:
        return []
    gv = tag.Tag.GenericValue
    if gv is None:
        return []
    inner = getattr(gv, "Properties", None)
    if inner is not None:
        return list(inner)
    if isinstance(gv, (list, tuple)):
        return list(gv)
    return []


def array_values(arr) -> list:
    """Convert an array of FPropertyTagType elements into their GenericValues."""
    out = []
    for el in arr:
        gv = getattr(el, "GenericValue", el)
        out.append(gv)
    return out


# ---------- object / soft-object path ----------

def _extract_soft_path(val) -> str | None:
    if val is None:
        return None
    # FSoftObjectPath / FSoftClassPath
    apn = getattr(val, "AssetPathName", None)
    if apn is not None:
        pkg_name = getattr(apn, "PackageName", None)
        if pkg_name is not None:
            path = str(pkg_name)
            asset_name = getattr(apn, "AssetName", None)
            if asset_name is not None:
                aname = str(asset_name)
                if aname and aname != "None":
                    path = f"{path}.{aname}"
            if path and path != "None":
                return path
        apn_str = str(apn)
        if apn_str and apn_str not in ("None", ""):
            return apn_str
    # Already a path string
    s = str(val)
    if "CUE4Parse" in s:
        # Try ToString() via .NET
        try:
            ts = val.ToString()
            if ts and "CUE4Parse" not in ts and ts != "None":
                return ts
        except Exception:
            pass
        return None
    if s in ("None", ""):
        return None
    return s


def prop_object_path(obj, key: str) -> str | None:
    val = prop(obj, key)
    return _extract_soft_path(val) if val is not None else None


def obj_ref_path(val) -> str | None:
    """Extract a path from a UObject reference (e.g. 'BPC'/'/Game/...').

    Handles ``FPackageIndex`` (with ResolvedObject) and plain str() output
    like ``Class'/Game/...'``.
    """
    if val is None:
        return None
    # FPackageIndex
    resolved = getattr(val, "ResolvedObject", None)
    if resolved is not None:
        for attr in ("Path", "GetPathName"):
            v = getattr(resolved, attr, None)
            if v is not None:
                try:
                    s = str(v() if callable(v) else v)
                    if s and s not in ("None", "") and "CUE4Parse" not in s:
                        return s
                except Exception:
                    pass
        # Fall through to str()
    s = str(val)
    # Class'/Game/foo.foo' or Class'/Game/foo.foo:sub'
    if "'" in s:
        inside = s.split("'", 1)[1]
        if inside.endswith("'"):
            inside = inside[:-1]
        # Truncate to the first "." dot for asset paths
        if inside and inside not in ("None", ""):
            return inside
    return s if s not in ("None", "") and "CUE4Parse" not in s else None


# ---------- struct unwrapping ----------

class _Reflected:
    __slots__ = ("Properties",)
    def __init__(self, p): self.Properties = p


def _reflect_props(net_obj):
    try:
        pp = net_obj.GetType().GetProperty("Properties")
        if pp is None:
            return None
        plist = pp.GetValue(net_obj)
        if plist is None:
            return None
        return _Reflected(plist)
    except Exception:
        return None


def unwrap_struct(s):
    if s is None:
        return None
    if hasattr(s, "Properties") and getattr(s, "Properties", None) is not None:
        return s
    gv = getattr(s, "GenericValue", None)
    if gv is not None and hasattr(gv, "Properties") and getattr(gv, "Properties", None) is not None:
        return gv
    target = gv if gv is not None else s
    st = getattr(target, "StructType", None)
    if st is not None:
        r = _reflect_props(st)
        if r is not None:
            return r
        if hasattr(st, "Properties"):
            return st
    return None


def struct_str(s, key: str, default: str = "") -> str:
    u = unwrap_struct(s)
    if u is None:
        return default
    return prop_str(u, key, default)


def struct_int(s, key: str, default: int = 0) -> int:
    u = unwrap_struct(s)
    if u is None:
        return default
    return prop_int(u, key, default)


def struct_float(s, key: str, default: float = 0.0) -> float:
    u = unwrap_struct(s)
    if u is None:
        return default
    return prop_float(u, key, default)


def struct_obj_path(s, key: str) -> str | None:
    u = unwrap_struct(s)
    if u is None:
        return None
    return prop_object_path(u, key)


def struct_array(s, key: str) -> list:
    u = unwrap_struct(s)
    if u is None:
        return []
    return prop_array(u, key)


# ---------- gameplay tag containers ----------

def extract_gameplay_tags(val) -> list[str]:
    """Flatten a FGameplayTagContainer or FGameplayTag into a list of tag strings."""
    if val is None:
        return []
    # FGameplayTag with TagName
    tagname = getattr(val, "TagName", None)
    if tagname is not None:
        s = str(tagname)
        if s and s != "None":
            return [s]
    # FGameplayTagContainer.GameplayTags -> List<FGameplayTag>
    tags = getattr(val, "GameplayTags", None)
    out: list[str] = []
    if tags is not None:
        for t in tags:
            tn = getattr(t, "TagName", None)
            if tn is None:
                u = unwrap_struct(t)
                if u is not None:
                    tn = prop(u, "TagName")
            if tn is not None:
                s = str(tn)
                if s and s != "None":
                    out.append(s)
        if out:
            return out
    # FScriptStruct wrapper — unwrap to a property holder and look for GameplayTags array
    u = unwrap_struct(val)
    if u is not None:
        arr = prop_array(u, "GameplayTags")
        for el in arr:
            gv = getattr(el, "GenericValue", el)
            tn = getattr(gv, "TagName", None)
            if tn is None:
                inner = unwrap_struct(gv)
                if inner is not None:
                    tn = prop(inner, "TagName")
            if tn is not None:
                s = str(tn)
                if s and s != "None":
                    out.append(s)
        if out:
            return out
    # Fallback: parse from string repr "Tag1, Tag2 (FGameplayTagContainer)"
    s = str(val)
    # Strip trailing "(FGameplayTagContainer)" or similar
    if "(" in s:
        s = s.rsplit("(", 1)[0].strip()
    if s in ("None", "") or "CUE4Parse" in s:
        return []
    if "," in s:
        return [t.strip() for t in s.split(",") if t.strip()]
    return [s] if s else []


def prop_tags(obj, key: str) -> list[str]:
    return extract_gameplay_tags(prop(obj, key))


def struct_tags(s, key: str) -> list[str]:
    u = unwrap_struct(s)
    if u is None:
        return []
    return prop_tags(u, key)


# ---------- vector / rotator ----------

def _reflect_field(net_obj, name: str):
    """Read a .NET property/field via System.Reflection.

    pythonnet hides members defined on an interface (IUStruct) — concrete
    FVector/FRotator fields like X/Y/Z aren't visible through getattr.
    """
    if net_obj is None:
        return None
    try:
        t = net_obj.GetType()
        p = t.GetProperty(name)
        if p is not None:
            return p.GetValue(net_obj)
        f = t.GetField(name)
        if f is not None:
            return f.GetValue(net_obj)
    except Exception:
        return None
    return None


def _read_struct_components(val, components: tuple[str, ...]) -> list[float] | None:
    """Read named float components from a struct value.

    Handles: direct fields (FVector with .X/.Y/.Z), FScriptStruct (whose
    ``StructType`` is an IUStruct interface — reflection needed), and
    FStructFallback (with .Properties).
    """
    if val is None:
        return None
    # Direct fields (FVector C# class)
    if all(hasattr(val, c) for c in components):
        try:
            return [float(getattr(val, c)) for c in components]
        except Exception:
            pass
    # FScriptStruct.StructType (often hidden behind IUStruct interface)
    st = getattr(val, "StructType", None)
    if st is not None:
        # Try direct attribute first (works for some struct types)
        try:
            if all(hasattr(st, c) for c in components):
                return [float(getattr(st, c)) for c in components]
        except Exception:
            pass
        # Fall back to .NET reflection
        try:
            vals = [_reflect_field(st, c) for c in components]
            if all(v is not None for v in vals):
                return [float(v) for v in vals]
        except Exception:
            pass
    # FStructFallback shape (read components as properties)
    u = unwrap_struct(val)
    if u is not None:
        try:
            return [prop_float(u, c) for c in components]
        except Exception:
            pass
    return None


def vec_to_list(val) -> list[float] | None:
    return _read_struct_components(val, ("X", "Y", "Z"))


def rot_to_list(val) -> list[float] | None:
    return _read_struct_components(val, ("Pitch", "Yaw", "Roll"))


# ---------- package loading ----------

def safe_load_package(provider, pkg_path: str):
    try:
        ok, package = provider.TryLoadPackage(pkg_path)
    except Exception as exc:
        logger.debug("load failed for %s: %s", pkg_path, exc)
        return None
    if not ok or package is None:
        return None
    return package


def _export_class(export) -> str:
    """Return the UE class name for an export.

    CUE4Parse maps unknown classes to a wrapper (e.g. UPrimaryDataAsset)
    and exposes the real class via ``ExportType``.  Fall back to Python
    type if ExportType is missing.
    """
    et = getattr(export, "ExportType", None)
    if et is not None:
        s = str(et)
        if s and s != "None":
            return s
    return type(export).__name__


def find_export(package, class_substring: str | None = None, name_substring: str | None = None):
    """Find the first export matching the given class / name substring.

    Both substrings are optional; passing neither returns the first non-default export.
    """
    for export in package.GetExports():
        cls = _export_class(export)
        name = str(export.Name)
        if class_substring and class_substring.lower() not in cls.lower():
            continue
        if name_substring and name_substring.lower() not in name.lower():
            continue
        return export
    return None


def find_exports_by_class(package, class_substring: str) -> list:
    out = []
    for export in package.GetExports():
        cls = _export_class(export)
        if class_substring.lower() in cls.lower():
            out.append(export)
    return out


def short_name_from_path(pkg_path: str) -> str:
    return pkg_path.rsplit("/", 1)[-1]
