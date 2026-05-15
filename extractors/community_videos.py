"""
Community-video matcher for the Subnautica 2 wiki.

Pulls recent uploads from a curated list of SN2 YouTube creators
(currently just `@QuickTipshow`), classifies each by title pattern,
and emits `out/community_videos.json` for the wiki to consume.

The wiki reads this file at request-time and renders the matched
clip(s) on the relevant detail page (creature / item / biomod /
vehicle / flora) or hub page (base-building, story, FAQ, landing).

Run with: `python run.py community-videos`
Optional flags:
  --days N        lookback window in days (default 7)
  --channel HND   YouTube handle to scrape (default QuickTipshow)
  --limit N       max videos to inspect (default 80)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Curator-defined per-video overrides
# ---------------------------------------------------------------------------
#
# Maps known YouTube video IDs to their canonical wiki target. Heuristic
# matching handles the long tail; this table pins the cases where the
# title would otherwise mis-route (typos like "Processer", in-game
# nicknames like "Old Habitat" with no entity page, story-only content
# that belongs on a hub).
#
# Each entry: (category, slug, caption)
#   category: "creatures" | "items" | "biomods" | "vehicles" | "flora"
#             | "hub:base-building" | "hub:story" | "hub:faq" | "hub:landing"
#   slug:     URL slug for entity pages, ignored for hubs.
#   caption:  eyebrow text ("Find guide", "Walkthrough", "How-to").
VIDEO_OVERRIDES: dict[str, tuple[str, str | None, str]] = {
    # ── Creatures ────────────────────────────────────────────────
    "ETzGJg1fhGE": ("creatures", "bullethead", "Find guide"),
    "dgqyi8MoJ0E": ("creatures", "collector-leviathan", "Scan guide"),
    # ── Items: blueprint locations + crafting ─────────────────────
    "QofOyCew26g": ("items", "bioscanner", "Find guide"),
    "DwKDUr0AzLM": ("items", "improved-fins", "Find guide"),
    "tWrdDwVk3rM": ("items", "modification-station", "Find guide"),
    "JTIdSACaTHs": ("items", "feedback-resonator", "Find guide"),
    "tOuydCpZCnE": ("items", "sonic-resonator", "Find guide"),
    "v7a_3vBNH2U": ("items", "time-of-day-display", "Find guide"),
    "eXzeHFkzfrw": ("items", "processor", "Find guide"),
    "Iz6abuu45ZM": ("items", "scout-ray-chassis", "Find guide"),
    "05yTxBqWOH8": ("items", "wakemaker", "Find guide"),
    "CFDgxWaCLRM": ("items", "repair-tool", "Find guide"),
    "0lnvgGS2s2g": ("items", "rebreather", "Find guide"),
    "Xci_SiCpuTU": ("items", "habitat-builder", "Find guide"),
    "DAcAT87E46U": ("items", "single-bed", "Find guide"),
    "XU5RMRrfDg8": ("items", "axum-bacterial-culture", "Find guide"),
    "tqXzR1tusJs": ("items", "necrolei-cyst", "Find guide"),
    "1oMTL8dnnVI": ("items", "lucifer-rotsac", "Find guide"),
    "axYzZhJ4yXQ": ("items", "fibrous-pulp", "Find guide"),
    "8NF5DQaKeZc": ("items", "rubber", "How-to"),
    "7_Vyf_YjeEU": ("items", "titanium-ingot", "How-to"),
    "HqBD-a9FwJs": ("items", "water", "How-to"),
    "Tuxp2ZiyTNU": ("items", "strong-acid", "How-to"),
    "NWTvuoCRrIs": ("items", "fabricator", "How-to"),
    "qG84GDIlg48": ("items", "thermal-plant", "How-to"),
    "SKGs2YcZ-Dk": ("items", "battery-terminal", "How-to"),
    "8CL6xIH9EeA": ("items", "battery-terminal", "Find guide"),
    "tkkRTtFsU74": ("items", "processor", "How-to"),
    "De0-Wht97aw": ("items", "high-capacity-air-tank", "How-to"),
    "B_oCAamC404": ("items", "troilite", "Find guide"),
    # ── Flora deposit pages (resource locators) ──────────────────
    "NMt38tbVXwM": ("flora", "lead-deposit", "Find guide"),
    "bqVu4gTR1ks": ("flora", "quartz-deposit", "Find guide"),
    "e-RR_mJFTPM": ("flora", "conduit-crystal-deposit", "How-to"),
    "6nGpsFHdRUU": ("flora", "conduit-crystal-deposit", "Find guide"),
    "B4J9SrezJfU": ("flora", "salt-deposit", "Find guide"),
    "n41E_7cuDL0": ("flora", "copper-deposit", "Find guide"),
    "jHSouz5SgtI": ("flora", "silver-deposit", "Find guide"),
    "gGd6KsHZDPI": ("flora", "atacamite", "Find guide"),
    "n8VpudaMkFg": ("flora", "large-sulfur-crystal", "Find guide"),
    "FmXEMRF8ch4": ("flora", "lithium-node", "Find guide"),
    # ── Vehicles ─────────────────────────────────────────────────
    "cMoLNGuK96Y": ("vehicles", "tadpole", "Find guide"),
    "XUf5peCWzG4": ("vehicles", "tadpole", "How-to"),
    "f-G86kwA0mw": ("vehicles", "tadpole", "How-to"),
    "hMW5C-JZBjA": ("vehicles", "lifepod", "How-to"),
    # ── Biomods ──────────────────────────────────────────────────
    "X09xnILxsIU": ("biomods", "dash", "How-to"),
    # ── Hub: base-building (pieces without dedicated detail pages) ─
    "Xv3gy9o-e9o": ("hub:base-building", None, "Find guide"),  # Scanner Station
    "4cr5UHfp1aw": ("hub:base-building", None, "Find guide"),  # Growbed
    "b1XHQAlTSCw": ("hub:base-building", None, "Find guide"),  # Interior Wall
    "L4qTaI6lBiA": ("hub:base-building", None, "Find guide"),  # Room
    "giOxbXeqibE": ("hub:base-building", None, "Find guide"),  # Nook
    "UCoZKtYa0Ls": ("hub:base-building", None, "Find guide"),  # Moonpool
    "OvzqEC5uOtE": ("hub:base-building", None, "Find guide"),  # Half Round Room
    "0hDCZq4OKA8": ("hub:base-building", None, "How-to"),      # Air in Your Base
    # ── Hub: story (Blackbox runs + plot beats) ──────────────────
    "gvAFvhAVRNg": ("hub:story", None, "Walkthrough"),  # Blackbox Quaker
    "0jAqJf8OXnY": ("hub:story", None, "Walkthrough"),  # Blackbox Wander
    "oSqA-TlSI30": ("hub:story", None, "Walkthrough"),  # Blackbox Zip
    "Iq4kjoJfxzA": ("hub:story", None, "Walkthrough"),  # Old Habitat
    "ge4U2u_hQQQ": ("hub:story", None, "Walkthrough"),  # Tadpole Pens
    "iMFgH6YHV1c": ("hub:story", None, "Walkthrough"),  # Giant Alien Power Plant
    "vqCtQYTmDm8": ("hub:story", None, "Walkthrough"),  # 1st Angel Comb
    "rTo920B1Pck": ("hub:story", None, "Walkthrough"),  # 2nd Angel Comb
    # ── Hub: faq (UI / settings) ─────────────────────────────────
    "TsU3NM_41bQ": ("hub:faq", None, "How-to"),  # Upgrade Inventory Space
    "htU34lN6tP4": ("hub:faq", None, "How-to"),  # More Hotbar Space
    "FPbnRnjVVc4": ("hub:faq", None, "How-to"),  # Map Icons
    "c2Pj2F_YTzo": ("hub:faq", None, "How-to"),  # Use Scanner Station
    "tHIf3DYVny8": ("hub:faq", None, "How-to"),  # Digestive Incompatibility
    "aYRfZl1R6oc": ("hub:faq", None, "How-to"),  # Charge Your Tools
    # ── Hub: landing (flagship / beginner overviews) ─────────────
    "df8ynhpl8a4": ("hub:landing", None, "Flagship"),  # 25 Tips & Tricks
    "gPLfoOcm76E": ("hub:landing", None, "Beginner"),  # Food Early Game
}


# Channel display name → full channel URL. Wiki uses this to render
# the clickable channel chip in the info column.
KNOWN_CHANNELS: dict[str, str] = {
    "QuickTipshow": "https://www.youtube.com/@QuickTipshow",
}


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass
class VideoEntry:
    id: str
    title: str
    channel: str
    channel_url: str | None
    duration_seconds: int | None
    published_at: str  # ISO YYYY-MM-DD
    caption: str | None
    excerpt: str | None = None


# ---------------------------------------------------------------------------
# yt-dlp fetch
# ---------------------------------------------------------------------------


def _fetch_recent_uploads(
    handle: str, limit: int, since_utc: datetime
) -> list[dict[str, Any]]:
    """Return raw yt-dlp info dicts for the channel's `limit` newest
    uploads, filtered to entries uploaded after `since_utc`. Channel
    is identified by `handle` (e.g. ``QuickTipshow``)."""
    try:
        import yt_dlp  # noqa: WPS433 (lazy import — optional dep)
    except ImportError as exc:
        raise RuntimeError(
            "yt-dlp not installed. Run: pip install yt-dlp"
        ) from exc

    flat_url = f"https://www.youtube.com/@{handle}/videos"
    flat_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "playlist_items": f"1-{limit}",
    }
    with yt_dlp.YoutubeDL(flat_opts) as ydl:
        flat = ydl.extract_info(flat_url, download=False)
    entries = flat.get("entries") or []
    log.info("Flat playlist returned %d entries", len(entries))

    detail_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    full: list[dict[str, Any]] = []
    with yt_dlp.YoutubeDL(detail_opts) as ydl:
        for entry in entries:
            vid = entry.get("id")
            if not vid:
                continue
            try:
                info = ydl.extract_info(
                    f"https://www.youtube.com/watch?v={vid}", download=False
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Failed to fetch %s: %s", vid, exc)
                continue
            ts = info.get("timestamp") or 0
            if ts and datetime.fromtimestamp(ts, tz=timezone.utc) < since_utc:
                # Past the lookback window. Channel feed is newest-first,
                # so we can stop here.
                break
            full.append(info)
    log.info("Fetched %d videos uploaded after %s", len(full), since_utc.isoformat())
    return full


# ---------------------------------------------------------------------------
# Title → wiki target heuristics (fallback when no override)
# ---------------------------------------------------------------------------


_RE_BLUEPRINT = re.compile(r"^(.+?)\s+Blueprint\s+Locations?\b", re.IGNORECASE)
_RE_HOW_TO_FIND = re.compile(r"^How\s+To\s+(?:Find|Get)\s+(.+?)(?:\s+in\s+Subnautica)?$", re.IGNORECASE)
_RE_HOW_TO_USE = re.compile(r"^How\s+To\s+(?:Use|Make)\s+(?:The\s+|A\s+)?(.+?)(?:\s+in\s+Subnautica)?$", re.IGNORECASE)


def _heuristic_target(title: str) -> tuple[str, str | None, str] | None:
    """Best-effort categorisation when the video isn't in the override
    table. Returns ``(category, slug, caption)`` or ``None`` to mark the
    video as unmatched."""
    clean = title.replace(" in Subnautica 2", "").strip()

    m = _RE_BLUEPRINT.match(clean)
    if m:
        return ("items", _slug(m.group(1)), "Find guide")

    m = _RE_HOW_TO_FIND.match(clean)
    if m:
        return ("flora", _slug(m.group(1)) + "-deposit", "Find guide")

    m = _RE_HOW_TO_USE.match(clean)
    if m:
        return ("items", _slug(m.group(1)), "How-to")

    return None


def _slug(text: str) -> str:
    """Reproduces the wiki's ``kebabifySlug`` helper. Drops everything
    except a-z/0-9, splits camelCase, collapses runs of dashes."""
    text = re.sub(r"_+", "-", text)
    text = re.sub(r"([a-z])([A-Z])", r"\1-\2", text)
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", text)
    text = re.sub(r"[^a-zA-Z0-9-]", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-").lower()


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------


def run(handle: str = "QuickTipshow", days: int = 7, limit: int = 80) -> dict:
    """Build the community-videos JSON. Layout:

        {
          "generated_at": ISO,
          "channel": "@QuickTipshow",
          "by_category": {
            "creatures": {"<slug>": [VideoEntry, ...]},
            "items":     {"<slug>": [VideoEntry, ...]},
            ...
          },
          "hubs": {
            "base-building": [VideoEntry, ...],
            "story":         [...],
            "faq":           [...],
            "landing":       [...],
          },
          "unmatched": [VideoEntry, ...]
        }
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    raw = _fetch_recent_uploads(handle, limit=limit, since_utc=since)

    by_cat: dict[str, dict[str, list[dict]]] = {
        "creatures": {},
        "items": {},
        "biomods": {},
        "vehicles": {},
        "flora": {},
    }
    hubs: dict[str, list[dict]] = {
        "base-building": [],
        "story": [],
        "faq": [],
        "landing": [],
    }
    unmatched: list[dict] = []

    sn2_keywords = ("subnautica 2", "subnautica2")

    for info in raw:
        title: str = info.get("title", "")
        if not any(kw in title.lower() for kw in sn2_keywords):
            log.info("Skipping non-SN2 video: %s", title)
            continue

        target = VIDEO_OVERRIDES.get(info["id"]) or _heuristic_target(title)
        ts = info.get("timestamp") or 0
        published = (
            datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            if ts
            else ""
        )
        channel_name = info.get("channel") or info.get("uploader") or handle
        entry = VideoEntry(
            id=info["id"],
            title=title,
            channel=channel_name,
            channel_url=KNOWN_CHANNELS.get(channel_name) or info.get("channel_url"),
            duration_seconds=int(info.get("duration") or 0) or None,
            published_at=published,
            caption=(target[2] if target else None),
        )

        if target is None:
            unmatched.append(asdict(entry))
            continue

        category, slug, _caption = target
        if category.startswith("hub:"):
            hubs[category.split(":", 1)[1]].append(asdict(entry))
        else:
            if slug is None:
                log.warning("Override for %s has no slug", info["id"])
                unmatched.append(asdict(entry))
                continue
            by_cat[category].setdefault(slug, []).append(asdict(entry))

    # Sort each bucket: most-recent first.
    for cat_dict in by_cat.values():
        for slug, videos in cat_dict.items():
            videos.sort(key=lambda v: v["published_at"], reverse=True)
    for videos in hubs.values():
        videos.sort(key=lambda v: v["published_at"], reverse=True)
    unmatched.sort(key=lambda v: v["published_at"], reverse=True)

    total_targeted = sum(
        len(v) for cat in by_cat.values() for v in cat.values()
    ) + sum(len(v) for v in hubs.values())
    log.info(
        "Mapped %d videos (%d unmatched)", total_targeted, len(unmatched)
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "channel": f"@{handle}",
        "by_category": by_cat,
        "hubs": hubs,
        "unmatched": unmatched,
    }
