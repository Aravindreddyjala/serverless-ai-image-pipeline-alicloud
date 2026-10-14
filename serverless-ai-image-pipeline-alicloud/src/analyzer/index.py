"""Image analyzer: OSS processed/ upload -> Qwen-VL tags + alt-text via DashScope.

Runtime: Alibaba Cloud Function Compute 3.0, Python 3.10.
Trigger: OSS ObjectCreated events filtered on the ``processed/`` prefix.
Output: structured metadata is logged and written to ``metadata/<path>.json``.
"""

import json
import logging
import os
import posixpath
import re
import time

import dashscope
import oss2
from dashscope import MultiModalConversation

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

SOURCE_PREFIX = os.environ.get("SOURCE_PREFIX", "processed/")
METADATA_PREFIX = os.environ.get("METADATA_PREFIX", "metadata/")
WRITE_METADATA = os.environ.get("WRITE_METADATA", "true").lower() == "true"
QWEN_MODEL = os.environ.get("QWEN_VL_MODEL", "qwen-vl-plus")
DASHSCOPE_BASE_URL = os.environ.get("DASHSCOPE_BASE_URL", "https://dashscope-intl.aliyuncs.com/api/v1")
SIGNED_URL_TTL_SECONDS = int(os.environ.get("SIGNED_URL_TTL_SECONDS", "900"))
MAX_TAGS = int(os.environ.get("MAX_TAGS", "10"))
USE_INTERNAL_ENDPOINT = os.environ.get("OSS_USE_INTERNAL_ENDPOINT", "true").lower() == "true"

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

PROMPT = f"""You are an image cataloguing assistant. Analyze the image and respond with ONLY a JSON object,
no markdown fences, using exactly this schema:
{{
  "alt_text": "one concise sentence (max 125 characters) suitable for an HTML alt attribute",
  "description": "two or three sentences describing the scene, subjects and context",
  "tags": ["up to {MAX_TAGS} lowercase single- or two-word tags, most relevant first"],
  "dominant_colors": ["up to 5 common color names"],
  "contains_text": true or false,
  "contains_people": true or false
}}"""

dashscope.base_http_api_url = DASHSCOPE_BASE_URL


class SkipObject(Exception):
    """Raised for inputs that can never succeed; the event is logged and acknowledged, not retried."""


class AnalysisError(Exception):
    """Raised when DashScope returns an error or an unusable response."""


def log_event(level, message, **fields):
    logger.log(level, json.dumps({"message": message, **fields}, default=str, ensure_ascii=False))


def get_bucket(context, endpoint, bucket_name):
    creds = context.credentials
    auth = oss2.StsAuth(creds.access_key_id, creds.access_key_secret, creds.security_token)
    return oss2.Bucket(auth, endpoint, bucket_name)


def extract_json(text):
    """Parse the model's reply, tolerating stray markdown fences or surrounding prose."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def normalize(analysis):
    tags = [str(t).strip().lower() for t in analysis.get("tags", []) if str(t).strip()]
    return {
        "alt_text": str(analysis.get("alt_text", "")).strip()[:125],
        "description": str(analysis.get("description", "")).strip(),
        "tags": list(dict.fromkeys(tags))[:MAX_TAGS],
        "dominant_colors": [str(c).strip().lower() for c in analysis.get("dominant_colors", [])][:5],
        "contains_text": bool(analysis.get("contains_text", False)),
        "contains_people": bool(analysis.get("contains_people", False)),
    }


def analyze_image(image_url, api_key):
    messages = [{"role": "user", "content": [{"image": image_url}, {"text": PROMPT}]}]
    response = MultiModalConversation.call(api_key=api_key, model=QWEN_MODEL, messages=messages)

    if response.status_code != 200:
        raise AnalysisError(
            f"DashScope error status={response.status_code} code={response.code} "
            f"message={response.message} request_id={response.request_id}"
        )

    content = response.output.choices[0].message.content
    text = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    try:
        analysis = normalize(extract_json(text))
    except (json.JSONDecodeError, AttributeError, TypeError) as exc:
        raise AnalysisError(f"Model returned non-JSON output: {text[:500]!r}") from exc

    usage = response.usage or {}
    return analysis, {
        "dashscope_request_id": response.request_id,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
    }


def process_record(record, context, api_key):
    oss_info = record["oss"]
    bucket_name = oss_info["bucket"]["name"]
    key = oss_info["object"]["key"]
    region = record.get("region") or context.region

    if not key.startswith(SOURCE_PREFIX):
        raise SkipObject(f"Key outside {SOURCE_PREFIX!r} prefix")
    extension = posixpath.splitext(key)[1].lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise SkipObject(f"Unsupported extension {extension!r}")

    internal = "-internal" if USE_INTERNAL_ENDPOINT else ""
    io_bucket = get_bucket(context, f"https://oss-{region}{internal}.aliyuncs.com", bucket_name)
    # DashScope fetches the image over the public internet, so sign against the public endpoint.
    public_bucket = get_bucket(context, f"https://oss-{region}.aliyuncs.com", bucket_name)

    try:
        head = io_bucket.head_object(key)
    except oss2.exceptions.NotFound as exc:
        raise SkipObject("Object was deleted before it could be analyzed") from exc

    image_url = public_bucket.sign_url("GET", key, SIGNED_URL_TTL_SECONDS, slash_safe=True)

    started = time.perf_counter()
    analysis, usage = analyze_image(image_url, api_key)

    metadata = {
        "bucket": bucket_name,
        "image_key": key,
        "source_key": head.headers.get("x-oss-meta-source-key"),
        "content_type": head.content_type,
        "size_bytes": head.content_length,
        "model": QWEN_MODEL,
        "analyzed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "analysis": analysis,
        "usage": usage,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
    }

    if WRITE_METADATA:
        stem, _ = posixpath.splitext(key[len(SOURCE_PREFIX):])
        metadata_key = f"{METADATA_PREFIX}{stem}.json"
        io_bucket.put_object(
            metadata_key,
            json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        metadata["metadata_key"] = metadata_key

    log_event(logging.INFO, "image_analyzed", **metadata)
    return metadata


def handler(event, context):
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY environment variable is not set")

    payload = json.loads(event)
    records = payload.get("events", [])
    request_id = getattr(context, "request_id", None)
    log_event(logging.INFO, "invocation_started", request_id=request_id, record_count=len(records))

    results = []
    for record in records:
        key = record.get("oss", {}).get("object", {}).get("key")
        try:
            results.append({"status": "analyzed", **process_record(record, context, api_key)})
        except SkipObject as exc:
            log_event(logging.WARNING, "image_skipped", key=key, reason=str(exc))
            results.append({"status": "skipped", "key": key, "reason": str(exc)})
        except AnalysisError as exc:
            # Re-raise so FC's async retry policy can retry throttling or transient model errors.
            log_event(logging.ERROR, "analysis_failed", key=key, detail=str(exc))
            raise
        except oss2.exceptions.OssError as exc:
            log_event(
                logging.ERROR, "oss_error", key=key, status=exc.status,
                code=exc.code, oss_request_id=exc.request_id, detail=exc.message,
            )
            raise
        except Exception:
            logger.exception(json.dumps({"message": "unexpected_error", "key": key}))
            raise

    return json.dumps({"request_id": request_id, "results": results}, default=str, ensure_ascii=False)
