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


# ---------------------------------------------------------------------------
# Per-locale .locres overlays
# ---------------------------------------------------------------------------
#
# Subnautica 2 ships its translations as `.locres` files inside
# `Subnautica2/Content/Localization/Game/<locale>/Game.locres`. Each file
# holds a "namespace.key → translated string" map that overlays the source
# strings in the `UStringTable` data assets.
#
# CUE4Parse exposes `.locres` via `FTextLocalizationResource`, but the
# Wikily front-end already uses the namespace-prefixed `<ST_Table>.<Key>`
# format from `string_tables.json`. To stay compatible we re-emit each
# locale's overlay in the SAME shape — `[{id, entries, row_count}]` — keyed
# by the matching `ST_*` table id so the lookup code doesn't have to know
# anything about FText namespaces.

# Map game-locale identifiers (folder names) → wiki locale codes used in
# `langUtils.ts`. Only locales SN2 actually ships are listed.
LOCALE_MAP = {
    "en":       "en",
    "de-DE":    "de",
    "es-419":   "es-la",
    "fr-FR":    "fr",
    "it":       "it",
    "ja-JP":    "ja",
    "ko-KR":    "ko",
    "pt-BR":    "pt-br",
    "ru-RU":    "ru",
    "uk-UA":    "uk",
    "zh-Hans":  "zh-cn",
}


def _load_locres(provider, asset_path: str) -> dict[str, dict[str, str]] | None:
    """Read a `.locres` and return `{namespace: {key: value}}`.

    CUE4Parse exposes the parsed resource via `provider.LoadFileBytes` +
    `FTextLocalizationResource` constructor. We do this the simple way: call
    `provider.LoadFile(path).GetPayload()` shaped through pythonnet.
    """
    try:
        archive = provider.TryCreateReader(asset_path)
        # `TryCreateReader` returns a tuple via pythonnet — `(ok, reader)`
        if isinstance(archive, tuple):
            ok, reader = archive
            if not ok or reader is None:
                return None
        else:
            reader = archive
        if reader is None:
            return None
    except Exception as e:
        logger.debug("locres reader open failed (%s): %s", asset_path, e)
        return None

    try:
        import clr  # noqa: F401
        clr.AddReference("CUE4Parse")
        from CUE4Parse.UE4.Localization import FTextLocalizationResource  # noqa: E402
        resource = FTextLocalizationResource(reader)
    except Exception as e:
        logger.debug("locres parse failed (%s): %s", asset_path, e)
        return None

    out: dict[str, dict[str, str]] = {}
    try:
        for ns_pair in resource.Entries:
            namespace = str(ns_pair.Key.Str)
            ns_out = out.setdefault(namespace, {})
            for key_pair in ns_pair.Value:
                k = str(key_pair.Key.Str)
                v = getattr(key_pair.Value, "LocalizedString", None)
                if v is None:
                    continue
                ns_out[k] = str(v)
    except Exception as e:
        logger.debug("locres iter failed (%s): %s", asset_path, e)
        return None
    return out


def run_locales(provider) -> dict[str, list[dict]]:
    """Extract per-locale string overlays.

    Returns `{wiki_locale: [{id, entries, row_count}, ...]}`. The shape per
    locale matches the existing `string_tables.json` output so the wiki can
    reuse its lookup code by just swapping the source file.
    """
    LOCALE_DIR_PREFIX = "Subnautica2/Content/Localization/Game/"
    out: dict[str, list[dict]] = {}
    for game_locale, wiki_locale in LOCALE_MAP.items():
        path = f"{LOCALE_DIR_PREFIX}{game_locale}/Game.locres"
        if path not in provider.Files.Keys:
            logger.warning("locres missing for %s (%s)", wiki_locale, path)
            continue
        parsed = _load_locres(provider, path)
        if not parsed:
            continue
        # Flatten the namespace/key map into table-shaped records. The SN2
        # build uses `ST_<TableName>` as the namespace in `.locres` so we can
        # use it directly as the table id.
        tables = [
            {"id": ns, "entries": entries, "row_count": len(entries)}
            for ns, entries in parsed.items()
        ]
        out[wiki_locale] = tables
        logger.info(
            "locres %s → %d tables, %d total strings",
            wiki_locale,
            len(tables),
            sum(t["row_count"] for t in tables),
        )
    return out
