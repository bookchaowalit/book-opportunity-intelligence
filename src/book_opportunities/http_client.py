"""Bounded GET-only HTTP helper for local domain-product loopback APIs.

Default synthesis must never call Firecrawl, Telegram, Todoist, or other
external write / paid endpoints. Only 127.0.0.1 domain APIs are allowed.

Uses httpx when installed; otherwise falls back to stdlib urllib for simple
loopback GETs so offline/local e2e works without extra installs.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from . import config
from .policy import require_provider


try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore


class UpstreamError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None, kind: str = "error"):
        super().__init__(message)
        self.status_code = status_code
        self.kind = kind


def ensure_httpx() -> Any:
    if httpx is None:
        raise UpstreamError(
            "httpx is not installed; install with: pip install httpx",
            kind="dependency",
        )
    return httpx


def _assert_loopback_get(method: str, url: str) -> None:
    method_u = (method or "GET").upper()
    if method_u != "GET":
        raise UpstreamError(
            f"Non-GET method forbidden for domain consumer: {method_u}",
            kind="method_forbidden",
        )
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise UpstreamError(
            "Domain consumer may only call loopback hosts (127.0.0.1)",
            kind="host_forbidden",
        )
    if not (parsed.path or "").startswith("/v1/"):
        raise UpstreamError(
            "Domain consumer may only call /v1/* read endpoints",
            kind="path_forbidden",
        )


def _build_url(url: str, params: Optional[dict]) -> str:
    if not params:
        return url
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    if not query:
        return url
    sep = "&" if ("?" in url) else "?"
    return f"{url}{sep}{query}"


def _request_json_urllib(
    url: str,
    *,
    headers: Optional[dict],
    timeout: float,
) -> Any:
    req = urllib.request.Request(
        url,
        method="GET",
        headers=headers or {"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200) or 200
            content = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        content = exc.read() if hasattr(exc, "read") else b""
        if status == 429:
            raise UpstreamError(
                "Upstream rate limited (HTTP 429)",
                status_code=429,
                kind="rate_limit",
            ) from exc
        raise UpstreamError(
            f"Upstream HTTP {status}",
            status_code=status,
            kind="http_error",
        ) from exc
    except TimeoutError as exc:
        raise UpstreamError("Upstream request timed out", kind="timeout") from exc
    except Exception as exc:
        name = type(exc).__name__.lower()
        msg = str(exc).lower()
        if "timeout" in name or "timeout" in msg or "timed out" in msg:
            raise UpstreamError("Upstream request timed out", kind="timeout") from exc
        raise UpstreamError("Upstream request failed", kind="network") from exc

    if status == 429:
        raise UpstreamError(
            "Upstream rate limited (HTTP 429)",
            status_code=429,
            kind="rate_limit",
        )
    if status >= 400:
        raise UpstreamError(
            f"Upstream HTTP {status}",
            status_code=status,
            kind="http_error",
        )
    if len(content) > config.MAX_RESPONSE_BYTES:
        raise UpstreamError("Upstream response exceeded size limit", kind="size_limit")
    try:
        return json.loads(content.decode("utf-8"))
    except Exception as exc:
        raise UpstreamError("Upstream request failed", kind="network") from exc


def _request_json_httpx(
    url: str,
    *,
    headers: Optional[dict],
    timeout: float,
    transport: Any,
    sleep: Callable[[float], None],
) -> Any:
    client_mod = ensure_httpx()
    last_error: Exception | None = None

    for attempt in range(config.MAX_RETRIES + 1):
        try:
            kwargs: dict[str, Any] = {
                "method": "GET",
                "url": url,
                "headers": headers or {"Accept": "application/json"},
                "timeout": timeout,
            }
            if transport is not None:
                with client_mod.Client(transport=transport, timeout=timeout) as client:
                    resp = client.request(
                        **{k: v for k, v in kwargs.items() if k != "timeout"}
                    )
            else:
                resp = client_mod.request(**kwargs)

            if resp.status_code == 429:
                if attempt >= config.MAX_RETRIES:
                    raise UpstreamError(
                        "Upstream rate limited (HTTP 429)",
                        status_code=429,
                        kind="rate_limit",
                    )
                sleep(min(2**attempt, 8))
                continue

            if resp.status_code >= 400:
                raise UpstreamError(
                    f"Upstream HTTP {resp.status_code}",
                    status_code=resp.status_code,
                    kind="http_error",
                )

            content = resp.content
            if len(content) > config.MAX_RESPONSE_BYTES:
                raise UpstreamError("Upstream response exceeded size limit", kind="size_limit")
            return resp.json()
        except UpstreamError:
            raise
        except Exception as exc:
            last_error = exc
            name = type(exc).__name__.lower()
            msg = str(exc).lower()
            if "timeout" in name or "timeout" in msg or "abort" in msg:
                if attempt >= config.MAX_RETRIES:
                    raise UpstreamError("Upstream request timed out", kind="timeout") from exc
                sleep(min(2**attempt, 8))
                continue
            raise UpstreamError("Upstream request failed", kind="network") from exc

    raise UpstreamError(f"Upstream request failed: {last_error}", kind="network")


def request_json(
    method: str,
    url: str,
    *,
    provider: str,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    json_body: Optional[dict] = None,
    timeout: Optional[float] = None,
    transport: Any = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Perform a local domain-product GET under free-only policy.

    Never used by this package's own read-only API GET handlers.
    """
    require_provider(provider)
    _assert_loopback_get(method, url)
    if json_body is not None:
        raise UpstreamError("JSON request bodies are not allowed", kind="method_forbidden")

    timeout = config.REQUEST_TIMEOUT_SECONDS if timeout is None else timeout
    full_url = _build_url(url, params)

    # Prefer httpx when available (and for transport injection in tests).
    # Fall back to stdlib urllib for dependency-light local e2e.
    if httpx is not None or transport is not None:
        return _request_json_httpx(
            full_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
            sleep=sleep,
        )

    last_error: Exception | None = None
    for attempt in range(config.MAX_RETRIES + 1):
        try:
            return _request_json_urllib(full_url, headers=headers, timeout=timeout)
        except UpstreamError as exc:
            if exc.kind == "rate_limit" and attempt < config.MAX_RETRIES:
                sleep(min(2**attempt, 8))
                last_error = exc
                continue
            if exc.kind == "timeout" and attempt < config.MAX_RETRIES:
                sleep(min(2**attempt, 8))
                last_error = exc
                continue
            raise
        except Exception as exc:  # pragma: no cover
            last_error = exc
            raise UpstreamError("Upstream request failed", kind="network") from exc
    raise UpstreamError(f"Upstream request failed: {last_error}", kind="network")
