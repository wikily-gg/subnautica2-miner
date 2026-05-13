"""
Configuration for the Subnautica 2 data miner.

All paths are read from environment variables so this file can be committed
without leaking your local layout.  Set them in a ``.env`` file (use
``.env.example`` as a template) or export them in your shell before running.
"""

from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    """Read a path from env, expanding ``~`` and resolving to absolute."""
    val = os.environ.get(name, default)
    if val is None or val == "":
        return None
    return str(Path(val).expanduser().resolve())


# Try to load a sibling .env file if present (very small parser — no python-dotenv dep)
_ENV_FILE = Path(__file__).with_name(".env")
if _ENV_FILE.exists():
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ---------------------------------------------------------------------------
# Paths (required)
# ---------------------------------------------------------------------------

# Directory containing the Subnautica 2 .pak / .utoc / .ucas files
PAKS_DIR: str | None = _env("SN2_PAKS_DIR")

# Directory containing CUE4Parse.dll and dependencies (net8.0 build of CUE4Parse)
CUE4PARSE_DLL_DIR: str | None = _env("SN2_CUE4PARSE_DLL_DIR")

# Path to the Subnautica 2 .usmap mappings file (required for UE5 unversioned
# property deserialization).  Subnautica 2 ships unversioned, so this MUST
# match the build's version.
MAPPINGS_PATH: str | None = _env("SN2_MAPPINGS_PATH")

# Output directory for all extractor JSON / PNG / GeoJSON files.  Defaults to
# ``./out`` next to this file.
OUTPUT_DIR: str = _env("SN2_OUTPUT_DIR") or str(Path(__file__).with_name("out"))

# ---------------------------------------------------------------------------
# Game version
# ---------------------------------------------------------------------------

# Subnautica 2 is built on UE 5.6 (per the .usmap filename).
UE_GAME: str = os.environ.get("SN2_UE_GAME", "GAME_UE5_6")

# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------

# Subnautica 2 ships unencrypted (as of the pre-EA build).
AES_KEY: str | None = os.environ.get("SN2_AES_KEY") or None


def validate() -> None:
    """Raise a helpful error if required paths are missing."""
    missing = []
    if not PAKS_DIR:
        missing.append("SN2_PAKS_DIR")
    if not CUE4PARSE_DLL_DIR:
        missing.append("SN2_CUE4PARSE_DLL_DIR")
    if not MAPPINGS_PATH:
        missing.append("SN2_MAPPINGS_PATH")
    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
            + "\nCopy .env.example to .env and fill in your local paths, "
              "or export the variables in your shell."
        )
