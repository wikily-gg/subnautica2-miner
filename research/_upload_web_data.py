"""Push web/data/markers.geojson + meta.json to R2 with a versioned backup.

The miner's standard `python run.py upload` only walks `out/*.json` so the
post-`build_data.py` outputs in `web/data/` never get shipped. We need
them on R2 every time the marker stream changes (today: synthetic
"Lifepod (Spawn)" marker replaces the seafloor-wreck placements).

Run from the miner root:
    python research/_upload_web_data.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import config  # noqa: F401 - side-effect: load .env into os.environ
from upload import get_r2_client, R2_BUCKET_NAME, upload_file

WEB_DATA = HERE / "web" / "data"

# Files we always push when this script runs. Add to this list as new
# post-build assets appear; everything else still flows through
# `python run.py upload`.
FILES = [
    "markers.geojson",
    "meta.json",
]

R2_PREFIX = "subnautica-2/data/"
VERSIONS_PREFIX = "subnautica-2/_versions/"


def main() -> None:
    client = get_r2_client()
    if client is None:
        sys.exit("R2 credentials missing")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    label = "lifepod-spawn-marker"
    backup_dir = f"{VERSIONS_PREFIX}{ts}_{label}/"

    for name in FILES:
        local = WEB_DATA / name
        if not local.exists():
            print(f"  ! missing {local}, skipping")
            continue
        live_key = f"{R2_PREFIX}{name}"
        backup_key = f"{backup_dir}{name}"

        # Copy live -> backup first (only if live exists).
        try:
            client.copy_object(
                Bucket=R2_BUCKET_NAME,
                CopySource={"Bucket": R2_BUCKET_NAME, "Key": live_key},
                Key=backup_key,
            )
            print(f"  backup {live_key} -> {backup_key}")
        except Exception as exc:
            print(f"  ! backup failed for {live_key}: {exc}")

        # Then overwrite.
        ok = upload_file(client, str(local), live_key)
        print(f"  {'uploaded' if ok else 'FAILED '} {live_key}  ({local.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
