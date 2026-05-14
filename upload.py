"""
Cloudflare R2 upload utilities for the Subnautica 2 miner.

Port of palworld_data_miner/upload.py. Uses boto3 S3-compatible API.
Credentials are read from environment vars loaded by config.py from .env.

Smart-diff: tracks local MD5 manifest and skips unchanged files.
Versioned backup: copies live keys to `subnautica-2/_versions/{ts}/`
before overwriting (opt-in with backup=True).
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
from datetime import datetime, timezone

import config

logger = logging.getLogger(__name__)


_MIME_TYPES: dict[str, str] = {
    ".json": "application/json",
    ".webp": "image/webp",
    ".png": "image/png",
}

# R2 paths
R2_DATA_PREFIX = "subnautica-2/"
R2_ICON_PREFIX = "images/subnautica-2/icons/"
R2_RENDER_PREFIX = "images/subnautica-2/renders/"

_VERSIONS_PREFIX = "subnautica-2/_versions/"
_MANIFEST_KEY = "subnautica-2/_versions/manifest.json"

_LOCAL_MANIFEST_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "out", ".upload_manifest.json"
)


# ---------------------------------------------------------------------------
# Credentials (read from env, also picked up from .env via config.py)
# ---------------------------------------------------------------------------

R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_ENDPOINT_URL = os.environ.get(
    "R2_ENDPOINT_URL",
    "https://6d10a7c0fb01a4ba7b310a383bfebdc3.r2.cloudflarestorage.com",
)
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "survivalytics-bucket")


def get_r2_client():
    """Create a boto3 S3 client for Cloudflare R2. Returns None on missing creds."""
    if not R2_ACCESS_KEY_ID or not R2_SECRET_ACCESS_KEY:
        logger.warning("R2 credentials not configured - upload disabled")
        return None

    try:
        import boto3
        from botocore.client import Config

        client = boto3.client(
            "s3",
            endpoint_url=R2_ENDPOINT_URL,
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            config=Config(signature_version="s3v4", region_name="auto"),
        )
        logger.info("R2 client ready (bucket=%s)", R2_BUCKET_NAME)
        return client
    except Exception as e:
        logger.warning("Failed to create R2 client: %s", e)
        return None


# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------


def _load_manifest(r2_client) -> dict:
    try:
        response = r2_client.get_object(Bucket=R2_BUCKET_NAME, Key=_MANIFEST_KEY)
        return json.loads(response["Body"].read())
    except Exception:
        return {"versions": []}


def _save_manifest(r2_client, manifest: dict) -> None:
    body = json.dumps(manifest, indent=2, ensure_ascii=False)
    r2_client.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=_MANIFEST_KEY,
        Body=body.encode("utf-8"),
        ContentType="application/json",
    )
    logger.info("Updated R2 version manifest")


def _list_live_keys(r2_client, prefix: str) -> list[str]:
    keys: list[str] = []
    continuation_token = None
    while True:
        kwargs = {"Bucket": R2_BUCKET_NAME, "Prefix": prefix, "MaxKeys": 1000}
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        response = r2_client.list_objects_v2(**kwargs)
        for obj in response.get("Contents", []):
            key = obj["Key"]
            if "/_versions/" not in key:
                keys.append(key)
        if response.get("IsTruncated"):
            continuation_token = response["NextContinuationToken"]
        else:
            break
    return keys


def backup_current(r2_client, live_prefix: str, version_label: str | None = None) -> str | None:
    if r2_client is None:
        return None
    now = datetime.now(timezone.utc)
    version_id = version_label or now.strftime("%Y%m%d_%H%M%S")
    backup_prefix = f"{_VERSIONS_PREFIX}{version_id}/"
    live_keys = _list_live_keys(r2_client, live_prefix)
    if not live_keys:
        logger.info("No existing files to back up under %s", live_prefix)
        return None
    logger.info("Backing up %d files from %s to %s", len(live_keys), live_prefix, backup_prefix)
    backed_up = 0
    for key in live_keys:
        rel = key[len(live_prefix):]
        backup_key = f"{backup_prefix}{rel}"
        try:
            r2_client.copy_object(
                Bucket=R2_BUCKET_NAME,
                CopySource={"Bucket": R2_BUCKET_NAME, "Key": key},
                Key=backup_key,
            )
            backed_up += 1
        except Exception:
            logger.warning("Failed to back up %s", key)
    logger.info("Backed up %d/%d files to version %s", backed_up, len(live_keys), version_id)
    manifest = _load_manifest(r2_client)
    manifest["versions"].append({
        "id": version_id,
        "timestamp": now.isoformat(),
        "prefix": backup_prefix,
        "file_count": backed_up,
    })
    _save_manifest(r2_client, manifest)
    return version_id


# ---------------------------------------------------------------------------
# Upload primitives
# ---------------------------------------------------------------------------


def upload_file(r2_client, local_path: str, r2_key: str, content_type: str | None = None) -> bool:
    if content_type is None:
        ext = os.path.splitext(local_path)[1].lower()
        content_type = _MIME_TYPES.get(ext)
        if content_type is None:
            content_type, _ = mimetypes.guess_type(local_path)
            content_type = content_type or "application/octet-stream"
    try:
        with open(local_path, "rb") as f:
            r2_client.put_object(
                Bucket=R2_BUCKET_NAME,
                Key=r2_key,
                Body=f,
                ContentType=content_type,
            )
        logger.info("Uploaded: %s -> %s", os.path.basename(local_path), r2_key)
        return True
    except Exception:
        logger.exception("Failed to upload %s", local_path)
        return False


def _file_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_local_manifest() -> dict[str, str]:
    if os.path.exists(_LOCAL_MANIFEST_PATH):
        try:
            with open(_LOCAL_MANIFEST_PATH, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_local_manifest(manifest: dict[str, str]) -> None:
    os.makedirs(os.path.dirname(_LOCAL_MANIFEST_PATH), exist_ok=True)
    with open(_LOCAL_MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)


def upload_directory(
    local_dir: str,
    r2_prefix: str,
    r2_client=None,
    backup: bool = False,
    force: bool = False,
) -> tuple[int, int]:
    """Upload changed files in local_dir to R2 under r2_prefix.

    Smart-diff via MD5 manifest. Returns (uploaded_count, total_count).
    """
    if r2_client is None:
        r2_client = get_r2_client()
    if r2_client is None:
        logger.error("Cannot upload - R2 client not available")
        return 0, 0

    old_manifest = _load_local_manifest() if not force else {}
    new_manifest: dict[str, str] = dict(old_manifest)

    all_files: list[tuple[str, str, str]] = []
    changed_files: list[tuple[str, str]] = []

    for root, _dirs, files in os.walk(local_dir):
        for fname in files:
            if fname.startswith("."):
                continue
            local_path = os.path.join(root, fname)
            rel_path = os.path.relpath(local_path, local_dir).replace("\\", "/")
            r2_key = f"{r2_prefix}{rel_path}"
            md5 = _file_md5(local_path)
            new_manifest[r2_key] = md5
            all_files.append((local_path, r2_key, md5))
            if force or old_manifest.get(r2_key) != md5:
                changed_files.append((local_path, r2_key))

    skipped = len(all_files) - len(changed_files)
    logger.info("Files: %d total, %d changed, %d unchanged", len(all_files), len(changed_files), skipped)

    if not changed_files:
        _save_local_manifest(new_manifest)
        return 0, len(all_files)

    if backup:
        version_id = backup_current(r2_client, r2_prefix)
        if version_id:
            logger.info("Created backup version: %s", version_id)

    uploaded = 0
    for local_path, r2_key in changed_files:
        if upload_file(r2_client, local_path, r2_key):
            uploaded += 1

    logger.info("Upload complete: %d/%d changed files uploaded", uploaded, len(changed_files))
    _save_local_manifest(new_manifest)
    return uploaded, len(all_files)
