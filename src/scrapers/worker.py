"""
src/scrapers/worker.py
======================
Bronze-layer ingestion worker — updated with R2 batch flush.

Responsibilities
----------------
* Pull scrape jobs off the Redis/Arq queue.
* Rotate through a pool of HTTP proxies sourced from the live Django ProxyPool.
* Execute async HTTP GET requests via ``curl_cffi`` with Chrome TLS impersonation.
* Accumulate raw HTML responses in an in-process buffer (``R2BronzeBuffer``).
* When the buffer reaches ``R2_FLUSH_BATCH_SIZE`` (default 500), compress the
  batch to ``.jsonl.gz`` in memory and stream it to Cloudflare R2 via S3
  multipart upload.
* Post a ``ScrapeLog`` row and update ``ScrapedURL.status`` via the Django API.

Architecture contract (CLAUDE.md §1)
--------------------------------------
- Workers NEVER write to the database directly — all DB interaction goes through
  the Django API endpoints (``/api/v1/scrapers/logs/``, ``/api/v1/scrapers/queue/``).
- Workers NEVER parse HTML — raw bytes go straight to R2.
- One ``.jsonl.gz`` file per flush: ``bronze/{domain}/{YYYY-MM-DD}/{uuid}.jsonl.gz``
- Multipart upload is used so the gzip stream never fully materialises in RAM.

R2 object key schema
---------------------
    bronze/<domain>/<YYYY-MM-DD>/<batch-uuid>.jsonl.gz

Each line in the .jsonl.gz is a JSON-serialised ``FetchResult.to_dict()``.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import math
import os
import random
import socket
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv

# Load src/scrapers/.env — must happen before any os.getenv() call
load_dotenv(Path(__file__).resolve().parent / ".env")

import boto3
from arq.connections import RedisSettings
from botocore.config import Config as BotoConfig
from curl_cffi.requests import AsyncSession, RequestsError

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)


# ===========================================================================
# Constants & Tunables
# ===========================================================================

IMPERSONATE_PROFILE: str = os.getenv("SCRAPER_IMPERSONATE_PROFILE", "chrome")
MAX_CONCURRENCY_PER_DOMAIN: int = int(os.getenv("SCRAPER_MAX_CONCURRENCY_PER_DOMAIN", "5"))
REQUEST_TIMEOUT: float = float(os.getenv("SCRAPER_REQUEST_TIMEOUT_SECONDS", "30.0"))
MAX_RETRIES: int = int(os.getenv("SCRAPER_MAX_RETRIES", "3"))
RETRY_BACKOFF_BASE: float = float(os.getenv("SCRAPER_RETRY_BACKOFF_SECONDS", "5.0"))

TERMINAL_STATUS_CODES: frozenset[int] = frozenset({403, 404, 410, 451})
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

# R2 / S3 upload settings
R2_FLUSH_BATCH_SIZE: int = int(os.getenv("R2_FLUSH_BATCH_SIZE", "500"))
R2_BRONZE_BUCKET: str = os.getenv("R2_BRONZE_BUCKET", "pipeline-bronze")
R2_ENDPOINT_URL: str = os.getenv("R2_ENDPOINT_URL", "")
R2_ACCESS_KEY_ID: str = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY: str = os.getenv("R2_SECRET_ACCESS_KEY", "")

# Multipart upload — R2 minimum part size is 5 MiB
R2_MULTIPART_CHUNK_BYTES: int = int(os.getenv("R2_MULTIPART_CHUNK_BYTES", str(5 * 1024 * 1024)))

# Django API base URL for posting ScrapeLog rows and updating URL status
DJANGO_API_BASE: str = os.getenv("DJANGO_API_BASE", "http://backend:8000/api/v1")
DJANGO_API_KEY: str = os.getenv("DJANGO_API_KEY", "")


# ===========================================================================
# Proxy Pool  (reads live from Django ProxyPool via API on startup)
# ===========================================================================

_PROXY_POOL: list[str] = []


async def _refresh_proxy_pool(session: AsyncSession) -> None:
    """
    Refresh the in-process proxy pool from the Django ProxyPool API.

    Called once at worker startup and can be re-called on a schedule.
    Falls back to PROXY_LIST env var if the API is unreachable.
    """
    global _PROXY_POOL
    try:
        resp = await session.get(
            f"{DJANGO_API_BASE}/scrapers/proxies/",
            headers={"Authorization": f"Bearer {DJANGO_API_KEY}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        _PROXY_POOL = [p["endpoint"] for p in data if p.get("endpoint")]
        logger.info("Proxy pool refreshed: %d endpoint(s) from Django API.", len(_PROXY_POOL))
        return
    except Exception as exc:
        logger.warning("Could not refresh proxy pool from API: %s. Falling back to env.", exc)

    # Fallback: PROXY_LIST env var
    raw = os.getenv("PROXY_LIST", "").strip()
    if raw:
        _PROXY_POOL = [p.strip() for p in raw.split(",") if p.strip()]
        logger.info("Proxy pool loaded from PROXY_LIST: %d endpoint(s).", len(_PROXY_POOL))
    else:
        single = os.getenv("PROXY_ENDPOINT", "").strip()
        _PROXY_POOL = [single] if single else []
        logger.warning("No proxy configuration found. Requests will use host IP.")


def _pick_proxy(domain: str) -> str | None:
    """Uniform random proxy selection. See CLAUDE.md §4."""
    if not _PROXY_POOL:
        return None
    chosen = random.choice(_PROXY_POOL)
    logger.debug("Proxy for %s → %s", domain, _obfuscate_proxy(chosen))
    return chosen


def _obfuscate_proxy(proxy: str) -> str:
    """Mask credentials in a proxy DSN for safe logging."""
    try:
        p = urlparse(proxy)
        if p.password:
            return proxy.replace(p.password, "***")
    except Exception:
        pass
    return proxy


# ===========================================================================
# FetchResult — value object
# ===========================================================================

class FetchStatus(str, Enum):
    SUCCESS = "success"
    BLOCKED = "blocked"
    NOT_FOUND = "not_found"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    HTTP_ERROR = "http_error"
    NETWORK_ERROR = "network_error"
    UNKNOWN_ERROR = "unknown_error"


@dataclass
class FetchResult:
    """
    Immutable record of a single fetch attempt.

    Serialised to JSON for every line in the ``.jsonl.gz`` Bronze file.
    The ``html`` field carries the raw response body — no parsing.
    """

    url: str
    status: FetchStatus

    html: str | None = None
    http_status_code: int | None = None
    final_url: str | None = None
    content_type: str | None = None
    content_length_bytes: int | None = None

    proxy_used: str | None = None
    attempt_count: int = 1
    elapsed_seconds: float = 0.0
    fetched_at_utc: float = field(default_factory=time.time)

    # Set by the worker after the result is collected, before flushing
    scraped_url_id: str | None = None
    site_id: str | None = None
    arq_job_id: str | None = None

    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


# ===========================================================================
# R2 Bronze Buffer  — batch → gzip → multipart upload
# ===========================================================================

class R2BronzeBuffer:
    """
    In-process accumulator that collects ``FetchResult`` records and flushes
    them to Cloudflare R2 as a ``.jsonl.gz`` file when the batch is full.

    Each worker process holds one shared instance (stored in Arq ``ctx``).

    Flush triggers
    --------------
    - Buffer reaches ``R2_FLUSH_BATCH_SIZE`` (default 500 records).
    - ``flush()`` is called explicitly (e.g. in the Arq shutdown hook).

    R2 object key
    -------------
    ``bronze/<domain>/<YYYY-MM-DD>/<batch-uuid>.jsonl.gz``

    The domain segment uses the first domain seen in the batch.  Each site's
    Bronze files therefore land in separate prefixes, making per-site dbt
    model scoping straightforward.

    Upload method
    -------------
    S3 Multipart Upload is used so the compressed stream never fully
    materialises in RAM.  Parts are 5 MiB each (R2's minimum).
    boto3 is called via ``asyncio.to_thread`` to keep the event loop free.
    """

    def __init__(self) -> None:
        self._records: list[FetchResult] = []
        self._lock = asyncio.Lock()
        self._hostname: str = socket.gethostname()

        # R2 is considered configured when both endpoint and key are present.
        # R2_LOCAL_DEV_MODE=True explicitly opts into local file fallback.
        self._r2_enabled: bool = bool(R2_ENDPOINT_URL and R2_ACCESS_KEY_ID)
        self._local_dev_mode: bool = (
            os.getenv("R2_LOCAL_DEV_MODE", "False").lower() in {"1", "true", "yes"}
        )

        if self._r2_enabled:
            self._s3 = self._build_s3_client()
            self._validate_r2_connection()
        elif self._local_dev_mode:
            self._s3 = None
            logger.warning(
                "R2_LOCAL_DEV_MODE is enabled — Bronze records will be written "
                "to /tmp/bronze/ instead of Cloudflare R2. "
                "Do NOT use this in production."
            )
        else:
            self._s3 = None
            raise RuntimeError(
                "R2 is not configured and R2_LOCAL_DEV_MODE is not enabled.\n"
                "Either:\n"
                "  1. Set R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY "
                "in your .env to use Cloudflare R2.\n"
                "  2. Set R2_LOCAL_DEV_MODE=True in your .env to write Bronze "
                "files locally to /tmp/bronze/ for development."
            )

    @staticmethod
    def _build_s3_client():
        """Build a boto3 S3 client pointing at Cloudflare R2."""
        return boto3.client(
            "s3",
            endpoint_url=R2_ENDPOINT_URL,
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            region_name="auto",
            config=BotoConfig(
                retries={"max_attempts": 3, "mode": "adaptive"},
                max_pool_connections=10,
            ),
        )

    def _validate_r2_connection(self) -> None:
        """
        Eagerly validate R2 connectivity on startup.
        Fails loudly if the bucket doesn't exist or credentials are wrong,
        rather than silently failing on the first flush.
        """
        try:
            self._s3.head_bucket(Bucket=R2_BRONZE_BUCKET)
            logger.info("R2 connection validated — bucket '%s' is reachable.", R2_BRONZE_BUCKET)
        except Exception as exc:
            error_str = str(exc)
            if "404" in error_str or "NoSuchBucket" in error_str:
                raise RuntimeError(
                    f"R2 bucket '{R2_BRONZE_BUCKET}' does not exist. "
                    f"Create it in the Cloudflare R2 dashboard first."
                ) from exc
            elif "403" in error_str or "AccessDenied" in error_str or "InvalidAccessKeyId" in error_str:
                raise RuntimeError(
                    f"R2 credentials are invalid or lack permission to access "
                    f"bucket '{R2_BRONZE_BUCKET}'. Check R2_ACCESS_KEY_ID and "
                    f"R2_SECRET_ACCESS_KEY in your .env."
                ) from exc
            else:
                raise RuntimeError(
                    f"Could not connect to R2 endpoint '{R2_ENDPOINT_URL}': {exc}\n"
                    f"Check R2_ENDPOINT_URL in your .env."
                ) from exc

    async def add(self, result: FetchResult) -> tuple[str, list[FetchResult]] | tuple[None, None]:
        """
        Append a result to the buffer.

        If the buffer reaches ``R2_FLUSH_BATCH_SIZE`` after appending,
        flushes automatically and returns (object_key, flushed_records).
        The caller uses flushed_records to know which scraped_url_ids to
        mark as done — only after confirmed write to disk/R2.

        Returns
        -------
        tuple[str, list[FetchResult]] | tuple[None, None]
            (r2_key, flushed_records) if a flush was triggered, else (None, None).
        """
        async with self._lock:
            self._records.append(result)
            if len(self._records) >= R2_FLUSH_BATCH_SIZE:
                return await self._flush_locked()
        return None, None

    async def flush(self) -> tuple[str, list[FetchResult]] | tuple[None, None]:
        """
        Force-flush the buffer regardless of size.

        Called in the Arq shutdown hook and by Dagster between asset runs.
        Returns (None, None) if the buffer is empty.
        """
        async with self._lock:
            if not self._records:
                return None, None
            return await self._flush_locked()

    async def size(self) -> int:
        """Return the current number of buffered records."""
        async with self._lock:
            return len(self._records)

    # -----------------------------------------------------------------------
    # Internal flush — caller must hold self._lock
    # -----------------------------------------------------------------------

    async def _flush_locked(self) -> tuple[str, list[FetchResult]]:
        """
        Compress buffered records to gzip JSON Lines and write to R2 or local disk.
        Must be called while ``self._lock`` is held.

        Returns
        -------
        tuple[str, list[FetchResult]]
            (object_key, flushed_records) — the caller uses flushed_records
            to update ScrapedURL statuses only after confirmed write.
        """
        batch = self._records.copy()
        self._records.clear()

        first_url = batch[0].url if batch else "unknown"
        domain = urlparse(first_url).netloc or "unknown"
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        batch_id = str(uuid.uuid4())
        object_key = f"bronze/{domain}/{date_str}/{batch_id}.jsonl.gz"

        compressed = await asyncio.to_thread(self._compress_batch, batch)

        if self._r2_enabled:
            logger.info(
                "Flushing %d records → s3://%s/%s",
                len(batch), R2_BRONZE_BUCKET, object_key,
            )
            await asyncio.to_thread(self._multipart_upload, compressed, object_key)
            logger.info(
                "R2 upload complete: %s (%.1f KB compressed).",
                object_key, len(compressed) / 1024,
            )
        else:
            # Local dev mode — write to <project_root>/data/bronze/
            # Path resolves to the same level as the src/ folder
            project_root = Path(__file__).resolve().parent.parent.parent
            local_path = project_root / "data" / object_key
            local_path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(local_path.write_bytes, compressed)
            logger.info(
                "DEV MODE: %d records saved locally → %s (%.1f KB).",
                len(batch), local_path, len(compressed) / 1024,
            )

        return object_key, batch

    @staticmethod
    def _compress_batch(records: list[FetchResult]) -> bytes:
        """
        Serialise and gzip-compress a list of FetchResult records.

        Each record becomes one JSON line.  The html field is stripped of
        null bytes to ensure valid JSON encoding.

        Parameters
        ----------
        records:
            List of FetchResult instances to compress.

        Returns
        -------
        bytes
            Gzip-compressed JSON Lines bytes.
        """
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as gz:
            for record in records:
                row = record.to_dict()
                # Sanitise null bytes that would break JSON parsing downstream
                if row.get("html"):
                    row["html"] = row["html"].replace("\x00", "")
                line = json.dumps(row, ensure_ascii=False, default=str)
                gz.write((line + "\n").encode("utf-8"))
        return buf.getvalue()

    def _multipart_upload(self, data: bytes, object_key: str) -> None:
        """
        Upload ``data`` to R2 using S3 Multipart Upload.

        Splits ``data`` into ``R2_MULTIPART_CHUNK_BYTES`` parts (min 5 MiB
        as required by R2).  For batches small enough to fit in one part,
        falls back to a standard ``put_object`` call.

        Parameters
        ----------
        data:
            Gzip-compressed bytes to upload.
        object_key:
            The destination R2 object key.
        """
        total_bytes = len(data)

        # Small batches: simple put_object is faster than multipart overhead
        if total_bytes <= R2_MULTIPART_CHUNK_BYTES:
            self._s3.put_object(
                Bucket=R2_BRONZE_BUCKET,
                Key=object_key,
                Body=data,
                ContentType="application/gzip",
                ContentEncoding="gzip",
                Metadata={
                    "pipeline-layer": "bronze",
                    "compressor": "gzip-level-6",
                },
            )
            return

        # Large batches: multipart upload
        mpu = self._s3.create_multipart_upload(
            Bucket=R2_BRONZE_BUCKET,
            Key=object_key,
            ContentType="application/gzip",
            ContentEncoding="gzip",
            Metadata={"pipeline-layer": "bronze"},
        )
        upload_id = mpu["UploadId"]
        parts: list[dict] = []

        try:
            num_parts = math.ceil(total_bytes / R2_MULTIPART_CHUNK_BYTES)
            for part_num in range(1, num_parts + 1):
                start = (part_num - 1) * R2_MULTIPART_CHUNK_BYTES
                chunk = data[start : start + R2_MULTIPART_CHUNK_BYTES]
                resp = self._s3.upload_part(
                    Bucket=R2_BRONZE_BUCKET,
                    Key=object_key,
                    PartNumber=part_num,
                    UploadId=upload_id,
                    Body=chunk,
                )
                parts.append({"PartNumber": part_num, "ETag": resp["ETag"]})
                logger.debug(
                    "Uploaded part %d/%d for %s.", part_num, num_parts, object_key
                )

            self._s3.complete_multipart_upload(
                Bucket=R2_BRONZE_BUCKET,
                Key=object_key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except Exception:
            # Abort to avoid orphaned parts incurring R2 storage charges
            self._s3.abort_multipart_upload(
                Bucket=R2_BRONZE_BUCKET,
                Key=object_key,
                UploadId=upload_id,
            )
            raise


# ===========================================================================
# Per-domain concurrency semaphores (CLAUDE.md §4)
# ===========================================================================

_domain_semaphores: dict[str, asyncio.Semaphore] = {}


def _get_domain_semaphore(domain: str) -> asyncio.Semaphore:
    """Return (creating lazily) a per-domain semaphore."""
    if domain not in _domain_semaphores:
        _domain_semaphores[domain] = asyncio.Semaphore(MAX_CONCURRENCY_PER_DOMAIN)
    return _domain_semaphores[domain]


# ===========================================================================
# Core HTTP request executor
# ===========================================================================

async def _execute_request(
    url: str,
    proxy: str | None,
    *,
    timeout: float = REQUEST_TIMEOUT,
) -> tuple[int, str, str, str | None]:
    """
    Perform a single async HTTP GET with Chrome TLS impersonation.

    Returns
    -------
    tuple[int, str, str, str | None]
        (http_status_code, response_text, final_url, content_type)
    """
    proxies = {"http": proxy, "https": proxy} if proxy else None

    async with AsyncSession(impersonate=IMPERSONATE_PROFILE) as session:
        response = await session.get(
            url,
            proxies=proxies,
            timeout=timeout,
            allow_redirects=True,
            headers={
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Upgrade-Insecure-Requests": "1",
            },
        )

    content_type = response.headers.get("content-type")
    return response.status_code, response.text, str(response.url), content_type


# ===========================================================================
# Django API helpers  (post ScrapeLog, update URL status)
# ===========================================================================

async def _post_scrape_log(
    ctx_session: AsyncSession,
    result: FetchResult,
    r2_key: str,
) -> None:
    """POST a ScrapeLog row to the Django API after a completed fetch."""
    payload = {
        "scraped_url_id": result.scraped_url_id,
        "site_id": result.site_id,
        "url": result.url,
        "final_url": result.final_url or "",
        "status": result.status.value,
        "http_status_code": result.http_status_code,
        "elapsed_seconds": result.elapsed_seconds,
        "attempt_count": result.attempt_count,
        "content_length_bytes": result.content_length_bytes,
        "content_type": result.content_type or "",
        "proxy_used": result.proxy_used or "",
        "arq_job_id": result.arq_job_id or "",
        "r2_bronze_key": r2_key,
        "error_message": result.error_message or "",
        "fetched_at": datetime.fromtimestamp(
            result.fetched_at_utc, tz=timezone.utc
        ).isoformat(),
    }
    try:
        await ctx_session.post(
            f"{DJANGO_API_BASE}/scrapers/logs/",
            json=payload,
            headers={"Authorization": f"Bearer {DJANGO_API_KEY}"},
            timeout=10,
        )
    except Exception as exc:
        logger.warning("Failed to post ScrapeLog: %s", exc)


async def _update_url_status(
    ctx_session: AsyncSession,
    scraped_url_id: str,
    status: str,
) -> None:
    """PATCH the ScrapedURL status via the Django API."""
    try:
        await ctx_session.patch(
            f"{DJANGO_API_BASE}/scrapers/queue/{scraped_url_id}/status/",
            json={"status": status, "last_scraped_at": datetime.now(timezone.utc).isoformat()},
            headers={"Authorization": f"Bearer {DJANGO_API_KEY}"},
            timeout=10,
        )
    except Exception as exc:
        logger.warning("Failed to update URL status %s: %s", scraped_url_id, exc)


# ===========================================================================
# Back-off helper
# ===========================================================================

async def _bulk_update_statuses(
    ctx_session: AsyncSession,
    flushed_records: list[FetchResult],
) -> None:
    """
    Bulk-update ScrapedURL statuses and post ScrapeLog rows for all records
    in a flushed batch, now that they are confirmed written to disk/R2.

    Called only after a successful flush — never before.
    """
    for record in flushed_records:
        if not record.scraped_url_id:
            continue

        # Map FetchStatus to ScrapedURL status string
        status_map = {
            FetchStatus.SUCCESS:       "done",
            FetchStatus.BLOCKED:       "blocked",
            FetchStatus.NOT_FOUND:     "not_found",
            FetchStatus.TIMEOUT:       "failed",
            FetchStatus.RATE_LIMITED:  "failed",
            FetchStatus.HTTP_ERROR:    "failed",
            FetchStatus.NETWORK_ERROR: "failed",
            FetchStatus.UNKNOWN_ERROR: "failed",
        }
        db_status = status_map.get(record.status, "failed")
        await _update_url_status(ctx_session, record.scraped_url_id, db_status)
    """Full-jitter exponential back-off."""
    ceiling = base * (2 ** (attempt - 1))
    delay = random.uniform(0, ceiling)
    logger.debug("Back-off: %.2fs before retry %d.", delay, attempt + 1)
    await asyncio.sleep(delay)


# ===========================================================================
# Arq task: fetch_product_page
# ===========================================================================

async def fetch_product_page(
    ctx: dict[str, Any],
    url: str,
    *,
    scraped_url_id: str | None = None,
    site_id: str | None = None,
    timeout: float = REQUEST_TIMEOUT,
    max_retries: int = MAX_RETRIES,
) -> dict[str, Any]:
    """
    Arq task — fetch one product page, buffer the result, flush to R2 when full.

    Status update contract
    ----------------------
    ScrapedURL.status is updated to its final value (done/failed/blocked/not_found)
    ONLY after the batch containing this record is confirmed written to disk or R2.
    Until then the row stays as ``in_progress``.

    This means if the worker crashes mid-batch, those URLs stay ``in_progress``
    and get re-queued on the next discoverer run — no data loss.
    """
    buffer: R2BronzeBuffer = ctx["buffer"]
    api_session: AsyncSession = ctx["api_session"]
    job_id: str = ctx.get("job_id", "")

    domain = urlparse(url).netloc
    semaphore = _get_domain_semaphore(domain)
    proxy = _pick_proxy(domain)
    proxy_label = _obfuscate_proxy(proxy) if proxy else None

    attempt = 0
    start_time = time.monotonic()

    logger.info("Fetching %s (proxy=%s)", url, proxy_label or "none")

    async with semaphore:
        while attempt < max_retries:
            attempt += 1
            try:
                http_code, html, final_url, content_type = await asyncio.wait_for(
                    _execute_request(url, proxy, timeout=timeout),
                    timeout=timeout + 5,
                )

            except asyncio.TimeoutError:
                elapsed = time.monotonic() - start_time
                logger.warning("TIMEOUT [%d/%d] %s after %.2fs", attempt, max_retries, url, elapsed)
                result = FetchResult(
                    url=url, status=FetchStatus.TIMEOUT,
                    proxy_used=proxy_label, attempt_count=attempt,
                    elapsed_seconds=round(elapsed, 3),
                    error_message=f"Timeout after {timeout}s",
                    scraped_url_id=scraped_url_id, site_id=site_id, arq_job_id=job_id,
                )
                r2_key, flushed = await buffer.add(result)
                if r2_key and flushed:
                    await _bulk_update_statuses(api_session, flushed)
                    await _post_scrape_log(api_session, result, r2_key)
                return {**result.to_dict(), "html": None}

            except RequestsError as exc:
                elapsed = time.monotonic() - start_time
                logger.warning("Network error [%d/%d] %s: %s", attempt, max_retries, url, exc)
                if attempt >= max_retries:
                    result = FetchResult(
                        url=url, status=FetchStatus.NETWORK_ERROR,
                        proxy_used=proxy_label, attempt_count=attempt,
                        elapsed_seconds=round(elapsed, 3), error_message=str(exc),
                        scraped_url_id=scraped_url_id, site_id=site_id, arq_job_id=job_id,
                    )
                    r2_key, flushed = await buffer.add(result)
                    if r2_key and flushed:
                        await _bulk_update_statuses(api_session, flushed)
                        await _post_scrape_log(api_session, result, r2_key)
                    return {**result.to_dict(), "html": None}
                await _backoff(attempt)
                proxy = _pick_proxy(domain)
                proxy_label = _obfuscate_proxy(proxy) if proxy else None
                continue

            except Exception as exc:  # noqa: BLE001
                elapsed = time.monotonic() - start_time
                logger.exception("Unexpected error fetching %s: %s", url, exc)
                result = FetchResult(
                    url=url, status=FetchStatus.UNKNOWN_ERROR,
                    proxy_used=proxy_label, attempt_count=attempt,
                    elapsed_seconds=round(elapsed, 3),
                    error_message=f"{type(exc).__name__}: {exc}",
                    scraped_url_id=scraped_url_id, site_id=site_id, arq_job_id=job_id,
                )
                r2_key, flushed = await buffer.add(result)
                if r2_key and flushed:
                    await _bulk_update_statuses(api_session, flushed)
                    await _post_scrape_log(api_session, result, r2_key)
                return {**result.to_dict(), "html": None}

            # ----------------------------------------------------------------
            # HTTP response received
            # ----------------------------------------------------------------
            elapsed = time.monotonic() - start_time

            if 200 <= http_code < 300:
                logger.info("SUCCESS %s HTTP %d in %.2fs", url, http_code, elapsed)
                result = FetchResult(
                    url=url, status=FetchStatus.SUCCESS, html=html,
                    http_status_code=http_code, final_url=final_url,
                    content_type=content_type,
                    content_length_bytes=len(html.encode("utf-8")) if html else 0,
                    proxy_used=proxy_label, attempt_count=attempt,
                    elapsed_seconds=round(elapsed, 3),
                    scraped_url_id=scraped_url_id, site_id=site_id, arq_job_id=job_id,
                )
                r2_key, flushed = await buffer.add(result)
                if r2_key and flushed:
                    await _bulk_update_statuses(api_session, flushed)
                    await _post_scrape_log(api_session, result, r2_key)
                return {**result.to_dict(), "html": None, "r2_bronze_key": r2_key or ""}

            if http_code == 403:
                logger.warning("BLOCKED 403 %s", url)
                result = FetchResult(
                    url=url, status=FetchStatus.BLOCKED,
                    http_status_code=http_code, final_url=final_url,
                    proxy_used=proxy_label, attempt_count=attempt,
                    elapsed_seconds=round(elapsed, 3),
                    error_message="HTTP 403 Forbidden",
                    scraped_url_id=scraped_url_id, site_id=site_id, arq_job_id=job_id,
                )
                r2_key, flushed = await buffer.add(result)
                if r2_key and flushed:
                    await _bulk_update_statuses(api_session, flushed)
                    await _post_scrape_log(api_session, result, r2_key)
                return {**result.to_dict(), "html": None}

            if http_code in TERMINAL_STATUS_CODES:
                logger.info("TERMINAL HTTP %d %s", http_code, url)
                result = FetchResult(
                    url=url, status=FetchStatus.NOT_FOUND,
                    http_status_code=http_code, final_url=final_url,
                    proxy_used=proxy_label, attempt_count=attempt,
                    elapsed_seconds=round(elapsed, 3),
                    error_message=f"HTTP {http_code}",
                    scraped_url_id=scraped_url_id, site_id=site_id, arq_job_id=job_id,
                )
                r2_key, flushed = await buffer.add(result)
                if r2_key and flushed:
                    await _bulk_update_statuses(api_session, flushed)
                    await _post_scrape_log(api_session, result, r2_key)
                return {**result.to_dict(), "html": None}

            if http_code in RETRYABLE_STATUS_CODES:
                logger.warning("Retryable HTTP %d %s [%d/%d]", http_code, url, attempt, max_retries)
                if attempt >= max_retries:
                    status_val = FetchStatus.RATE_LIMITED if http_code == 429 else FetchStatus.HTTP_ERROR
                    result = FetchResult(
                        url=url, status=status_val,
                        http_status_code=http_code, final_url=final_url,
                        proxy_used=proxy_label, attempt_count=attempt,
                        elapsed_seconds=round(elapsed, 3),
                        error_message=f"HTTP {http_code} after {max_retries} retries",
                        scraped_url_id=scraped_url_id, site_id=site_id, arq_job_id=job_id,
                    )
                    r2_key, flushed = await buffer.add(result)
                    if r2_key and flushed:
                        await _bulk_update_statuses(api_session, flushed)
                        await _post_scrape_log(api_session, result, r2_key)
                    return {**result.to_dict(), "html": None}
                await _backoff(attempt)
                proxy = _pick_proxy(domain)
                proxy_label = _obfuscate_proxy(proxy) if proxy else None
                continue

            # Other non-2xx
            logger.warning("Unhandled HTTP %d %s", http_code, url)
            result = FetchResult(
                url=url, status=FetchStatus.HTTP_ERROR,
                http_status_code=http_code, final_url=final_url,
                proxy_used=proxy_label, attempt_count=attempt,
                elapsed_seconds=round(elapsed, 3),
                error_message=f"Unhandled HTTP {http_code}",
                scraped_url_id=scraped_url_id, site_id=site_id, arq_job_id=job_id,
            )
            r2_key, flushed = await buffer.add(result)
            if r2_key and flushed:
                await _bulk_update_statuses(api_session, flushed)
                await _post_scrape_log(api_session, result, r2_key)
            return {**result.to_dict(), "html": None}

    return FetchResult(
        url=url, status=FetchStatus.UNKNOWN_ERROR,
        error_message="Exited retry loop without result.",
    ).to_dict()


# ===========================================================================
# Arq Lifecycle Hooks
# ===========================================================================

async def startup(ctx: dict[str, Any]) -> None:
    """
    Arq startup hook — initialise shared resources once per worker process.

    Stored in ``ctx`` so all tasks in the process share them:
    - ``buffer``: R2BronzeBuffer (accumulates FetchResult, flushes to R2)
    - ``api_session``: AsyncSession for Django API calls (ScrapeLog, URL status)
    """
    logger.info(
        "Arq worker starting. Profile=%s | ConcurrencyPerDomain=%d | "
        "MaxRetries=%d | Timeout=%.1fs | FlushBatchSize=%d",
        IMPERSONATE_PROFILE,
        MAX_CONCURRENCY_PER_DOMAIN,
        MAX_RETRIES,
        REQUEST_TIMEOUT,
        R2_FLUSH_BATCH_SIZE,
    )

    # Shared R2 buffer
    ctx["buffer"] = R2BronzeBuffer()

    # Persistent AsyncSession for Django API calls (reused across jobs)
    ctx["api_session"] = AsyncSession(impersonate=IMPERSONATE_PROFILE)

    # Refresh proxy pool from Django API
    await _refresh_proxy_pool(ctx["api_session"])

    # Django ORM setup (needed if any future task uses ORM directly)
    import django  # noqa: PLC0415
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
    django.setup()
    logger.info("Django ORM initialised.")


async def shutdown(ctx: dict[str, Any]) -> None:
    """
    Arq shutdown hook — flush remaining buffered records to R2/disk,
    then update ScrapedURL statuses for everything in the final batch.
    """
    buffer: R2BronzeBuffer | None = ctx.get("buffer")
    api_session: AsyncSession | None = ctx.get("api_session")

    if buffer:
        remaining = await buffer.size()
        if remaining:
            logger.info("Shutdown: flushing %d remaining records.", remaining)
            r2_key, flushed = await buffer.flush()
            if r2_key and flushed:
                logger.info("Final flush complete → %s", r2_key)
                if api_session:
                    await _bulk_update_statuses(api_session, flushed)

    if api_session:
        await api_session.close()

    _domain_semaphores.clear()
    logger.info("Arq worker shut down cleanly.")


# ===========================================================================
# WorkerSettings — Arq entry point
# ===========================================================================

class WorkerSettings:
    """
    Arq worker configuration.

    Run with:
        arq src.scrapers.worker.WorkerSettings

    Tunables are read from environment variables so they can be updated
    via the Django Admin without redeploying containers (CLAUDE.md §4).
    """

    functions = [fetch_product_page]
    on_startup = startup
    on_shutdown = shutdown

    redis_settings = RedisSettings.from_dsn(
        os.getenv("REDIS_URL", "redis://localhost:6379/0")
    )

    max_jobs: int = int(os.getenv("ARQ_MAX_JOBS", "50"))

    # Timeout = enough for all retries + upload time
    job_timeout: int = int(REQUEST_TIMEOUT * MAX_RETRIES * 2 + 60)

    keep_result: int = 3_600        # 1 hour — Dagster sensors can read results
    keep_result_forever: bool = False
    max_tries: int = 1              # Internal retry loop handles retries
