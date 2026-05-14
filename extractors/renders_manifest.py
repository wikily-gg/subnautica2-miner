"""Build a renders manifest for the SN2 wiki.

Walks `out/renders/*.png` and writes `out/renders/_manifest.json` listing every
render by its basename + canonical entity base name. The wiki uses this as a
fallback when an item has no IconBaker icon (e.g. Scuba Mask, Disperser etc.)
but does have a Blender mesh render.

Each entry:
  {
    "name": "SKM_ScubaMask",           # exact filename minus .png
    "file": "SKM_ScubaMask.png",       # filename
    "entity": "ScubaMask",             # canonical entity base (strip SKM_/SK_/SM_, suffixes)
    "angle": null,                     # one of front/back/side or null
    "variant": null                    # one of 01/02/01a/Infestation_01/Juvenile etc., or null
  }

The frontend uses `entity` to match an item by its BP class name (stripped of
`BP_` prefix) and falls back to `angle="front"` when multiple angles exist.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

RENDERS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "out", "renders",
)

_PREFIX_RE = re.compile(r"^(SKM|SK|SM)_")
_ANGLE_RE = re.compile(r"_(front|back|side)$")
# Trailing variant numbers / labels: _01, _01a, _02, _Juvenile, _Infestation_01
# Kept conservative — only strip clearly numeric / known-suffix tails so we don't
# eat real entity-name segments.
_VARIANT_RE = re.compile(
    r"_(?:0[0-9][a-z]?|Juvenile|Adult|Infestation_0[0-9]|Infestation_0[0-9][a-z]+|T[0-9])$",
    re.IGNORECASE,
)


def _parse_render_filename(stem: str) -> dict:
    """Decompose `SKM_ScubaMask` / `SKM_Marrowbreach_01_Infestation_01` into
    {entity, angle, variant}."""
    name = stem
    # Strip prefix (SKM_ / SK_ / SM_)
    name = _PREFIX_RE.sub("", name)

    # Pull angle suffix if present
    angle: Optional[str] = None
    m = _ANGLE_RE.search(name)
    if m:
        angle = m.group(1)
        name = name[: m.start()]

    # Pull variant tail if present (loop because some are stacked,
    # e.g. `Marrowbreach_01_Infestation_01`)
    variant_parts: list[str] = []
    while True:
        m = _VARIANT_RE.search(name)
        if not m:
            break
        variant_parts.insert(0, m.group(0).lstrip("_"))
        name = name[: m.start()]

    variant = "_".join(variant_parts) if variant_parts else None
    return {"entity": name, "angle": angle, "variant": variant}


def build_manifest(renders_dir: str = RENDERS_DIR) -> list[dict]:
    if not os.path.isdir(renders_dir):
        return []
    out: list[dict] = []
    for fname in sorted(os.listdir(renders_dir)):
        if not fname.endswith(".png"):
            continue
        stem = fname[: -len(".png")]
        parsed = _parse_render_filename(stem)
        out.append(
            {
                "name": stem,
                "file": fname,
                "entity": parsed["entity"],
                "angle": parsed["angle"],
                "variant": parsed["variant"],
            }
        )
    return out


def run() -> None:
    entries = build_manifest()
    if not entries:
        logger.warning("No renders found in %s", RENDERS_DIR)
        return
    manifest_path = os.path.join(RENDERS_DIR, "_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"renders": entries}, f, indent=2, ensure_ascii=False)
    logger.info("Wrote renders manifest: %d entries -> %s", len(entries), manifest_path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    run()
