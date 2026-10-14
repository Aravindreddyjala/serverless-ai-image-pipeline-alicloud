# Serverless AI-Powered Image Processing Pipeline on Alibaba Cloud

An event-driven, fully serverless pipeline on Alibaba Cloud. When you upload an image, it is resized and compressed automatically. **Qwen-VL** (through DashScope) then writes its alt-text, description and searchable tags. There are no servers to manage and no idle cost. One `git push` deploys everything.

![Python](https://img.shields.io/badge/Python-3.10-3776AB?logo=python&logoColor=white)
![Function Compute](https://img.shields.io/badge/Alibaba%20Cloud-Function%20Compute%203.0-FF6A00?logo=alibabacloud&logoColor=white)
![Serverless Devs](https://img.shields.io/badge/IaC-Serverless%20Devs%20%2B%20ROS-blue)
![Qwen-VL](https://img.shields.io/badge/AI-Qwen--VL-6E44FF)

---

## Architecture

```
                         ┌──────────────────────────────────────────────────────────┐
                         │                 OSS bucket (private, SSE)                │
                         │                                                          │
  upload ───────────────►│  raw/…          processed/…           metadata/….json    │
                         └────┬────────────────▲──────┬─────────────────▲───────────┘
                              │ ObjectCreated  │      │ ObjectCreated   │
                              │ (prefix raw/)  │      │ (prefix         │
                              ▼                │      ▼  processed/)    │
                   ┌─────────────────────┐     │  ┌──────────────────────┐   HTTPS (signed URL)
                   │ FC 3.0: processor   │─────┘  │ FC 3.0: analyzer     │──────────────────────┐
                   │ Python 3.10, Pillow │        │ Python 3.10,         │                      ▼
                   │ resize → JPEG/WebP  │        │ dashscope SDK        │◄──── JSON ──── DashScope Qwen-VL
                   └─────────────────────┘        └──────────────────────┘
                              │                              │
                              └──────────► Simple Log Service (structured JSON logs)
```

| Stage | Component | What it does |
|-------|-----------|--------------|
| 1 | **OSS** `raw/` | Holds the original uploads. |
| 2 | **FC `img-pipeline-processor`** | Downloads the original from OSS over the internal endpoint. Applies the EXIF orientation, then fits the image inside 1600 px using Lanczos resampling. Flattens transparency for JPEG output, strips EXIF (which removes GPS and device data) and writes a progressive, optimized JPEG or WebP to `processed/`. |
| 3 | **FC `img-pipeline-analyzer`** | Creates a short-lived pre-signed URL for the processed image and sends it to **Qwen-VL**. The model returns structured JSON (`alt_text`, `description`, `tags`, `dominant_colors`, `contains_text`, `contains_people`). The analyzer logs the result and saves it to `metadata/<path>.json`. |
| 4 | **ROS stack** `img-pipeline-infra` | Creates the OSS bucket, the function execution role and the OSS invocation role from `infra/ros-template.yml`. |

### Design decisions

- **No recursive triggers.** Each stage writes to a prefix that its own trigger does not watch: `raw/` → `processed/` → `metadata/`. The trigger prefixes never overlap.
- **Least-privilege RAM.** The functions can only call `GetObject`, `PutObject` and `GetObjectMeta` on this one bucket, plus write logs. OSS can only invoke functions named `img-pipeline-*`.
- **No long-lived keys in functions.** The code uses the STS credentials that FC injects through `context.credentials`.
- **Safe retries.** OSS triggers invoke functions asynchronously. Transient failures (OSS or DashScope errors) are re-raised so that FC's async retry policy runs again. Inputs that can never succeed (unsupported or corrupt files, oversized objects, decompression bombs) are logged as `image_skipped` and acknowledged. Both stages overwrite their outputs, so replays are idempotent.
- **Private bucket.** DashScope never gets public bucket access. It only receives a 15-minute signed URL.
- **Fast internal traffic.** Reads and writes inside FC use the `oss-<region>-internal` endpoint, which avoids internet egress fees.

---

## Repository layout

```
.
├── .github/workflows/deploy.yml   # CI/CD: push to main → s deploy
├── infra/ros-template.yml         # OSS bucket + RAM roles (Alibaba Cloud ROS)
├── s.yaml                         # Serverless Devs app: ROS stack + 2 FC functions + OSS triggers
└── src/
    ├── processor/                 # raw/ → processed/  (Pillow)
    │   ├── index.py
    │   └── requirements.txt
    └── analyzer/                  # processed/ → metadata/ (Qwen-VL via DashScope)
        ├── index.py
        └── requirements.txt
```

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Alibaba Cloud account | Activate **Function Compute**, **OSS**, **ROS** and **Simple Log Service**. |
| RAM user for deployment | Create an AccessKey pair. See [Deployer permissions](#deployer-permissions). |
| DashScope API key | Create one in [Model Studio](https://modelstudio.console.alibabacloud.com/) (international) or [Bailian](https://bailian.console.aliyun.com/) (Chinese mainland). The key must belong to the same site as `DASHSCOPE_BASE_URL`. |
| Node.js ≥ 18 | Needed for the Serverless Devs CLI. |
| Python 3.10+ with `pip` | Used by the pre-deploy step to vendor the Linux wheels. |
| Serverless Devs CLI | `npm install -g @serverless-devs/s` |

### Deployer permissions

The RAM user behind `ACCESS_KEY_ID` needs these permissions:

- `AliyunFCFullAccess`: functions and triggers
- `AliyunROSFullAccess`: the infrastructure stack
- `AliyunOSSFullAccess`: bucket creation through ROS
- `AliyunRAMFullAccess`: role and policy creation through ROS (you can scope this down after the first deploy)
- `AliyunLogFullAccess`: `logConfig: auto` creates the SLS project and logstore

---

## Configuration

### GitHub repository secrets (*Settings → Secrets and variables → Actions → Secrets*)

| Secret | Description |
|--------|-------------|
| `ACCOUNT_ID` | Your Alibaba Cloud account ID (UID). |
| `ACCESS_KEY_ID` | AccessKey ID of the deployer RAM user. |
| `ACCESS_KEY_SECRET` | AccessKey secret of the deployer RAM user. |
| `DASHSCOPE_API_KEY` | DashScope API key that the analyzer function uses. |

The workflow uses a GitHub environment named `production`. GitHub creates it on the first run. You can add required reviewers to it if you want manual approval before each deploy.

### Optional GitHub repository variables (*… → Variables*)

| Variable | Default | Description |
|----------|---------|-------------|
| `ALIBABA_CLOUD_REGION` | `ap-southeast-1` | Region for all resources. |
| `DASHSCOPE_BASE_URL` | `https://dashscope-intl.aliyuncs.com/api/v1` | Use `https://dashscope.aliyuncs.com/api/v1` for Chinese mainland regions and keys. |
| `QWEN_VL_MODEL` | `qwen-vl-plus` | Set this to `qwen-vl-max` for higher-quality results. |

### Function environment variables (set in `s.yaml`)

| Function | Variable | Default | Purpose |
|----------|----------|---------|---------|
| processor | `MAX_DIMENSION` | `1600` | Longest edge in pixels. The image keeps its aspect ratio and is never upscaled. |
| processor | `OUTPUT_FORMAT` | `JPEG` | `JPEG` or `WEBP`. |
| processor | `OUTPUT_QUALITY` | `82` | Encoder quality, from 1 to 100. |
| processor | `MAX_INPUT_BYTES` | `26214400` | Larger uploads are skipped. |
| processor | `MAX_IMAGE_PIXELS` | `50000000` | Limit used to detect decompression bombs. |
| analyzer | `QWEN_VL_MODEL` | `qwen-vl-plus` | DashScope multimodal model. |
| analyzer | `DASHSCOPE_API_KEY` | — | Injected at deploy time from the environment. |
| analyzer | `DASHSCOPE_BASE_URL` | intl endpoint | DashScope API base URL. |
| analyzer | `WRITE_METADATA` | `true` | Saves the result JSON to `metadata/`. |
| analyzer | `MAX_TAGS` | `10` | Maximum number of tags kept. |
| analyzer | `SIGNED_URL_TTL_SECONDS` | `900` | How long the image URL handed to DashScope stays valid. |
| both | `OSS_USE_INTERNAL_ENDPOINT` | `true` | Set this to `false` only if the function runs outside the bucket's region. |
| both | `LOG_LEVEL` | `INFO` | Python log level. |

> **Dependencies:** `oss2` comes preinstalled in the FC Python 3.10 runtime. `Pillow` and `dashscope` are vendored into `src/<fn>/python/` as `manylinux2014_x86_64` wheels by the `pre-deploy` action in `s.yaml`. `PYTHONPATH=/code/python` makes them importable. Because pip downloads Linux wheels, deploying from Windows or macOS produces the same package as CI.

---

## Deployment

### Option A: CI/CD (recommended)

1. Push this repository to GitHub.
2. Add the four [secrets](#github-repository-secrets-settings--secrets-and-variables--actions--secrets). You can also add the optional variables.
3. Push to `main`, or start **Actions → Deploy to Alibaba Cloud → Run workflow**.

The workflow compile-checks the sources, installs Node.js and the Serverless Devs CLI, and writes your credentials to a local `default` access profile. It then runs `s deploy --use-local -y`. The ROS stack is created first, and both functions then pick up its outputs (bucket name and role ARNs).

### Option B: from your workstation

```bash
# 1. Install the CLI
npm install -g @serverless-devs/s

# 2. Register credentials (stored in ~/.s/access.yaml)
s config add --AccountID <your-account-id> --AccessKeyID <key-id> --AccessKeySecret <key-secret> -a default -f

# 3. Export runtime settings
export DASHSCOPE_API_KEY="sk-..."             # PowerShell: $env:DASHSCOPE_API_KEY="sk-..."
export ALIBABA_CLOUD_REGION="ap-southeast-1"  # optional

# 4. Deploy everything
s deploy -y
```

To redeploy only one part, name the resource: `s processor deploy -y`, `s analyzer deploy -y` or `s infra deploy -y`.

---

## Try it out

The bucket is named `img-pipeline-<ACCOUNT_ID>-<REGION>`. Upload any image to `raw/` with the OSS console, [ossutil](https://www.alibabacloud.com/help/en/oss/developer-reference/ossutil) or an SDK:

```bash
ossutil cp ./holiday.png oss://img-pipeline-<ACCOUNT_ID>-ap-southeast-1/raw/2026/holiday.png
```

A few seconds later the bucket contains:

```
raw/2026/holiday.png          # original (untouched)
processed/2026/holiday.jpg    # resized + compressed
metadata/2026/holiday.json    # AI analysis
```

Example `metadata/2026/holiday.json`:

```json
{
  "bucket": "img-pipeline-5123456789012345-ap-southeast-1",
  "image_key": "processed/2026/holiday.jpg",
  "source_key": "raw/2026/holiday.png",
  "content_type": "image/jpeg",
  "size_bytes": 184233,
  "model": "qwen-vl-plus",
  "analyzed_at": "2026-10-14T10:02:11Z",
  "analysis": {
    "alt_text": "Two people walking along a sandy beach at sunset with gentle waves.",
    "description": "A wide coastal scene at golden hour. Two people walk barefoot along the waterline while low waves roll in. The sky glows orange and pink above the horizon.",
    "tags": ["beach", "sunset", "ocean", "people", "walking", "coast", "golden hour", "waves"],
    "dominant_colors": ["orange", "blue", "beige", "pink"],
    "contains_text": false,
    "contains_people": true
  },
  "usage": { "dashscope_request_id": "6f3c…", "input_tokens": 1265, "output_tokens": 112 },
  "duration_ms": 2874.3
}
```

### Observability

Both functions write one structured JSON line per event. The event names are `invocation_started`, `image_processed`, `image_analyzed`, `image_skipped`, `oss_error` and `analysis_failed`. They go to the SLS project `<ACCOUNT_ID>-<REGION>-project`, logstore `function-logstore`.

```bash
s processor logs --tail
s analyzer logs --tail
```

You can also query the logs in SLS, for example: `message: image_processed | select avg(reduction_pct), avg(duration_ms)`.

---

## Cleanup

```bash
s analyzer remove -y
s processor remove -y
# ROS cannot delete a non-empty bucket: empty it first
ossutil rm oss://img-pipeline-<ACCOUNT_ID>-<REGION>/ -r -f
s infra remove -y
```

---

## Production hardening checklist

- [ ] Store `DASHSCOPE_API_KEY` in **KMS Secrets Manager** and read it at cold start, instead of using a plain environment variable.
- [ ] Add an **async invocation destination** (`asyncInvokeConfig`) that sends failed events to MNS or EventBridge for dead-letter handling.
- [ ] Add **OSS lifecycle rules** that expire or archive `raw/` objects once they are processed.
- [ ] Use **GitHub OIDC → RAM role** federation instead of static AccessKeys in CI.
- [ ] Add **CloudMonitor alarms** on function errors and throttles.
- [ ] Index `metadata/*.json` into OpenSearch or Tablestore so images can be searched by tag.
