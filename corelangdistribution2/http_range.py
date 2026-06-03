from __future__ import annotations

import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin
from typing import Any, MutableMapping


def is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def url_join(base: str, name: str) -> str:
    if not base.endswith("/"):
        base += "/"
    return urljoin(base, name)


def new_network_stats() -> dict[str, Any]:
    return {
        "full_get_requests": 0,
        "head_requests": 0,
        "range_requests": 0,
        "range_retries": 0,
        "full_get_retries": 0,
        "range_failures": 0,
        "full_get_failures": 0,
        "if_range_used": 0,
        "etag_observed": 0,
        "bytes_received": 0,
        "retry_events": [],
    }


def _record(stats: MutableMapping[str, Any] | None, key: str, amount: int = 1) -> None:
    if stats is not None:
        stats[key] = int(stats.get(key, 0)) + amount


def _record_retry(stats: MutableMapping[str, Any] | None, *, url: str, op: str, attempt: int, error: Exception | str) -> None:
    if stats is None:
        return
    events = stats.setdefault("retry_events", [])
    if len(events) < 50:
        events.append({"op": op, "url": url, "attempt": attempt, "error": str(error)[:240]})


def _sleep_backoff(backoff: float, attempt: int) -> None:
    if backoff > 0:
        time.sleep(backoff * (2 ** max(0, attempt - 1)))


def read_url(url: str, *, retries: int = 3, backoff: float = 0.05, stats: MutableMapping[str, Any] | None = None) -> bytes:
    """Read a whole URL with small retry/backoff logic for alpha8 HTTP tests."""
    last: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            _record(stats, "full_get_requests")
            with urllib.request.urlopen(url, timeout=30) as r:
                data = r.read()
            _record(stats, "bytes_received", len(data))
            return data
        except Exception as e:
            last = e
            if attempt >= max(1, retries):
                break
            _record(stats, "full_get_retries")
            _record_retry(stats, url=url, op="GET", attempt=attempt, error=e)
            _sleep_backoff(backoff, attempt)
    _record(stats, "full_get_failures")
    raise IOError(f"GET failed after {max(1, retries)} attempts for {url}: {last}") from last


def head_url(url: str, *, retries: int = 3, backoff: float = 0.05, stats: MutableMapping[str, Any] | None = None) -> dict[str, str]:
    """Return simple HTTP metadata. The most important field for alpha8 is ETag."""
    last: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            _record(stats, "head_requests")
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=30) as r:
                headers = {k.lower(): v for k, v in r.headers.items()}
            if headers.get("etag"):
                _record(stats, "etag_observed")
            return headers
        except Exception as e:
            last = e
            if attempt >= max(1, retries):
                break
            _record_retry(stats, url=url, op="HEAD", attempt=attempt, error=e)
            _sleep_backoff(backoff, attempt)
    raise IOError(f"HEAD failed after {max(1, retries)} attempts for {url}: {last}") from last


def read_url_range(
    url: str,
    start: int,
    length: int,
    *,
    etag: str | None = None,
    retries: int = 3,
    backoff: float = 0.05,
    stats: MutableMapping[str, Any] | None = None,
) -> bytes:
    """Read an HTTP byte range with retry/backoff and optional If-Range.

    Alpha8 behavior:
    - sends Range for every chunk read;
    - sends If-Range when an ETag is known;
    - retries transient HTTP/network failures and truncated reads;
    - records simple metrics in `stats` when provided.
    """
    if length == 0:
        return b""
    end = start + length - 1
    last: Exception | None = None
    attempts = max(1, retries)
    for attempt in range(1, attempts + 1):
        headers = {"Range": f"bytes={start}-{end}"}
        if etag:
            headers["If-Range"] = etag
            _record(stats, "if_range_used")
        try:
            _record(stats, "range_requests")
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                status = getattr(r, "status", None)
                data = r.read()
            _record(stats, "bytes_received", len(data))
            if status == 200 and etag:
                # If-Range mismatch normally returns a full 200 response. Treat it as
                # unsafe for chunk reuse rather than silently slicing stale data.
                raise IOError("If-Range was not honored; possible ETag mismatch")
            if len(data) != length:
                # Some simple servers ignore Range and return the whole file. This
                # fallback is only accepted when no If-Range trust was involved.
                if not etag and len(data) >= start + length:
                    data = data[start : start + length]
                if len(data) != length:
                    raise IOError(f"range read returned {len(data)} bytes, expected {length}")
            return data
        except urllib.error.HTTPError as e:
            last = e
            retryable = e.code in (408, 425, 429, 500, 502, 503, 504)
            if (not retryable) or attempt >= attempts:
                break
            _record(stats, "range_retries")
            _record_retry(stats, url=url, op="RANGE", attempt=attempt, error=e)
            _sleep_backoff(backoff, attempt)
        except Exception as e:
            last = e
            if attempt >= attempts:
                break
            _record(stats, "range_retries")
            _record_retry(stats, url=url, op="RANGE", attempt=attempt, error=e)
            _sleep_backoff(backoff, attempt)
    _record(stats, "range_failures")
    raise IOError(f"range read failed after {attempts} attempts for {url} bytes={start}-{end}: {last}") from last


def read_local_range(path: Path, start: int, length: int) -> bytes:
    with path.open("rb") as f:
        f.seek(start)
        data = f.read(length)
    if len(data) != length:
        raise IOError(f"local range read returned {len(data)} bytes, expected {length}")
    return data
