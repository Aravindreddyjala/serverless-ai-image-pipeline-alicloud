"""Image processor: OSS raw/ upload -> resized, compressed image under processed/.

Runtime: Alibaba Cloud Function Compute 3.0, Python 3.10.
Trigger: OSS ObjectCreated events filtered on the ``raw/`` prefix.
"""

import io
import json
import logging
import os
import posixpath
import time

import oss2
from PIL import Image, ImageOps, UnidentifiedImageError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

SOURCE_PREFIX = os.environ.get("SOURCE_PREFIX", "raw/")
TARGET_PREFIX = os.environ.get("TARGET_PREFIX", "processed/")
MAX_DIMENSION = int(os.environ.get("MAX_DIMENSION", "1600"))
OUTPUT_FORMAT = os.environ.get("OUTPUT_FORMAT", "JPEG").upper()
OUTPUT_QUALITY = int(os.environ.get("OUTPUT_QUALITY", "82"))
MAX_INPUT_BYTES = int(os.environ.get("MAX_INPUT_BYTES", str(25 * 1024 * 1024)))
USE_INTERNAL_ENDPOINT = os.environ.get("OSS_USE_INTERNAL_ENDPOINT", "true").lower() == "true"

# Guard against decompression bombs: Pillow raises once an image exceeds 2x this.
Image.MAX_IMAGE_PIXELS = int(os.environ.get("MAX_IMAGE_PIXELS", "50000000"))

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
FORMAT_SETTINGS = {
    "JPEG": {"extension": ".jpg", "content_type": "image/jpeg"},
    "WEBP": {"extension": ".webp", "content_type": "image/webp"},
}

if OUTPUT_FORMAT not in FORMAT_SETTINGS:
    raise ValueError(f"OUTPUT_FORMAT must be one of {sorted(FORMAT_SETTINGS)}, got {OUTPUT_FORMAT!r}")


class SkipObject(Exception):
    """Raised for inputs that can never succeed; the event is logged and acknowledged, not retried."""


def log_event(level, message, **fields):
    logger.log(level, json.dumps({"message": message, **fields}, default=str, ensure_ascii=False))


def oss_endpoint(region):
    suffix = "-internal" if USE_INTERNAL_ENDPOINT else ""
    return f"https://oss-{region}{suffix}.aliyuncs.com"


def get_bucket(context, region, bucket_name):
    creds = context.credentials
    auth = oss2.StsAuth(creds.access_key_id, creds.access_key_secret, creds.security_token)
    return oss2.Bucket(auth, oss_endpoint(region), bucket_name)


def build_target_key(source_key):
    relative = source_key[len(SOURCE_PREFIX):]
    stem, _ = posixpath.splitext(relative)
    return f"{TARGET_PREFIX}{stem}{FORMAT_SETTINGS[OUTPUT_FORMAT]['extension']}"


def optimize_image(raw_bytes):
    """Return (optimized_bytes, details) for the given image payload."""
    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            source_format = img.format
            source_size = img.size
            # Animated formats: only the first frame is processed.
            img.seek(0)
            img = ImageOps.exif_transpose(img)
            img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.Resampling.LANCZOS)

            if OUTPUT_FORMAT == "JPEG" and img.mode not in ("RGB", "L"):
                img = img.convert("RGBA")
                background = Image.new("RGB", img.size, (255, 255, 255))
                background.paste(img, mask=img.getchannel("A"))
                img = background
            elif OUTPUT_FORMAT == "WEBP" and img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA" if "A" in img.getbands() else "RGB")

            buffer = io.BytesIO()
            save_kwargs = {"quality": OUTPUT_QUALITY, "optimize": True}
            if OUTPUT_FORMAT == "JPEG":
                save_kwargs["progressive"] = True
            else:
                save_kwargs["method"] = 6
            # EXIF is intentionally not carried over (strips GPS and device metadata).
            img.save(buffer, format=OUTPUT_FORMAT, **save_kwargs)
            output_size = img.size
    except (UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise SkipObject(f"Cannot decode image: {exc}") from exc

    return buffer.getvalue(), {
        "source_format": source_format,
        "source_dimensions": source_size,
        "output_dimensions": output_size,
    }


def process_record(record, context):
    oss_info = record["oss"]
    bucket_name = oss_info["bucket"]["name"]
    source_key = oss_info["object"]["key"]
    region = record.get("region") or context.region

    if not source_key.startswith(SOURCE_PREFIX):
        raise SkipObject(f"Key outside {SOURCE_PREFIX!r} prefix")
    if source_key.endswith("/"):
        raise SkipObject("Key is a folder placeholder")
    extension = posixpath.splitext(source_key)[1].lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise SkipObject(f"Unsupported extension {extension!r}")

    declared_size = oss_info["object"].get("size")
    if declared_size and int(declared_size) > MAX_INPUT_BYTES:
        raise SkipObject(f"Object is {declared_size} bytes, limit is {MAX_INPUT_BYTES}")

    bucket = get_bucket(context, region, bucket_name)
    started = time.perf_counter()

    try:
        raw_bytes = bucket.get_object(source_key).read()
    except oss2.exceptions.NoSuchKey as exc:
        raise SkipObject("Object was deleted before it could be processed") from exc

    optimized, details = optimize_image(raw_bytes)
    target_key = build_target_key(source_key)

    bucket.put_object(
        target_key,
        optimized,
        headers={
            "Content-Type": FORMAT_SETTINGS[OUTPUT_FORMAT]["content_type"],
            "Cache-Control": "public, max-age=31536000, immutable",
            "x-oss-meta-source-key": source_key,
            "x-oss-meta-processor": "fc-image-processor",
        },
    )

    result = {
        "bucket": bucket_name,
        "source_key": source_key,
        "target_key": target_key,
        "input_bytes": len(raw_bytes),
        "output_bytes": len(optimized),
        "reduction_pct": round(100 * (1 - len(optimized) / len(raw_bytes)), 2) if raw_bytes else 0,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        **details,
    }
    log_event(logging.INFO, "image_processed", **result)
    return result


def handler(event, context):
    payload = json.loads(event)
    records = payload.get("events", [])
    request_id = getattr(context, "request_id", None)
    log_event(logging.INFO, "invocation_started", request_id=request_id, record_count=len(records))

    results = []
    for record in records:
        key = record.get("oss", {}).get("object", {}).get("key")
        try:
            results.append({"status": "processed", **process_record(record, context)})
        except SkipObject as exc:
            log_event(logging.WARNING, "image_skipped", key=key, reason=str(exc))
            results.append({"status": "skipped", "key": key, "reason": str(exc)})
        except oss2.exceptions.OssError as exc:
            # Transient/permission errors: re-raise so FC's async retry policy kicks in.
            log_event(
                logging.ERROR, "oss_error", key=key, status=exc.status,
                code=exc.code, oss_request_id=exc.request_id, detail=exc.message,
            )
            raise
        except Exception:
            logger.exception(json.dumps({"message": "unexpected_error", "key": key}))
            raise

    return json.dumps({"request_id": request_id, "results": results}, default=str)
