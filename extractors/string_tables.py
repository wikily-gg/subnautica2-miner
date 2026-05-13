"""Dump every StringTable so wiki authors can map IDs -> localized strings."""
from __future__ import annotations

import logging

from helpers import _export_class, safe_load_package, short_name_from_path

logger = logging.getLogger(__name__)

DIR_PREFIX = "Subnautica2/Content/StringTables/"


def _extract_table(export) -> dict[str, str] | None:
    """A CUE4Parse StringTable exposes its rows via the .NET StringTable.KeysToEntries dict.

    We try several attribute names to be robust across CUE4Parse versions.
    """
    table = getattr(export, "StringTable", None)
    if table is None:
        # Some versions expose entries directly on the export
        table = export
    for attr in ("KeysToEntries", "KeysToMetaData", "Entries"):
        entries = getattr(table, attr, None)
        if entries is None:
            continue
        out: dict[str, str] = {}
        try:
            for kv in entries:
                k = getattr(kv, "Key", None)
                v = getattr(kv, "Value", None)
                if k is None and v is None:
                    continue
                out[str(k)] = str(v)
        except Exception:
            continue
        if out:
            return out
    return None


def extract(provider, asset_path: str) -> dict | None:
    pkg_path = asset_path[:-7]
    package = safe_load_package(provider, pkg_path)
    if package is None:
        return None
    for export in package.GetExports():
        cls = _export_class(export)
        if cls == "UStringTable" or "StringTable" in cls or type(export).__name__ == "UStringTable":
            entries = _extract_table(export)
            if entries:
                return {
                    "id": short_name_from_path(pkg_path),
                    "asset": pkg_path,
                    "entries": entries,
                    "row_count": len(entries),
                }
    return None


def run(provider) -> list[dict]:
    paths = sorted(p for p in provider.Files.Keys
                   if p.startswith(DIR_PREFIX) and p.endswith(".uasset"))
    logger.info("StringTables: %d candidates", len(paths))
    out = []
    for i, p in enumerate(paths, 1):
        e = extract(provider, p)
        if e:
            out.append(e)
        if i % 50 == 0:
            logger.info("  stringtables: %d / %d", i, len(paths))
    logger.info("StringTables: %d extracted", len(out))
    return out
