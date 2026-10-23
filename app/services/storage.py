"""Public product-image storage: local disk (dev) or S3-compatible bucket such as Cloudflare R2.

Uploads are decoded with Pillow and re-encoded to WebP, which validates the type by content (not by
extension), strips metadata (EXIF/GPS) and bounds dimensions. Sensitive attachments are out of scope
for this public bucket (future private bucket + short-lived signed URLs, see docs/security.md).
"""

from __future__ import annotations

import io
import secrets
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.config import get_settings

MAX_PIXELS = 40_000_000
MAX_SIDE = 1600
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class UploadError(ValueError):
    pass


def process_image(data: bytes) -> bytes:
    settings = get_settings()
    if len(data) > settings.max_upload_bytes:
        raise UploadError("file_too_large")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.format not in ("JPEG", "PNG", "WEBP", "GIF"):
                raise UploadError("unsupported_type")
            probe.verify()
        with Image.open(io.BytesIO(data)) as img:
            img.seek(0)
            img = img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB")
            img.thumbnail((MAX_SIDE, MAX_SIDE))
            out = io.BytesIO()
            img.save(out, format="WEBP", quality=82, method=4)
            return out.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise UploadError("invalid_image") from exc


def store_public_image(data: bytes) -> str:
    """Validate + store an image; returns its public URL."""
    settings = get_settings()
    webp = process_image(data)
    name = f"products/{secrets.token_hex(12)}.webp"
    if settings.storage_backend == "s3":
        import boto3

        client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            region_name="auto",
        )
        client.put_object(
            Bucket=settings.s3_bucket_public,
            Key=name,
            Body=webp,
            ContentType="image/webp",
            CacheControl="public, max-age=31536000, immutable",
        )
        return f"{settings.s3_public_base_url.rstrip('/')}/{name}"
    path = Path(settings.local_media_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(webp)
    return f"/media/{name}"
