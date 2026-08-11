"""Read-only client for domain data-product APIs on ports 8101–8107.

Rules:
- GET /v1/records only
- Loopback hosts only
- Fixture mode never opens network
- Safe load states: ready / empty / stale / timeout / unavailable / error
- Never scrape upstream providers; never call Firecrawl/Telegram/Todoist
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import config
from .http_client import UpstreamError, request_json
from .policy import domain_provider_name, require_provider


ENVELOPE_KEYS = (
    "schema_version",
    "source",
    "retrieved_at",
    "data_status",
    "items",
    "next_cursor",
)

FetchImpl = Callable[..., Any]


@dataclass
class DomainLoadResult:
    product_id: str
    port: int
    state: str  # ready | empty | stale | timeout | unavailable | error
    source: str  # api | fixture | none
    envelope: Optional[dict[str, Any]] = None
    error_message: str = ""
    free_only: bool = True
    items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok_for_synthesis(self) -> bool:
        return self.envelope is not None and isinstance(self.items, list)


def is_data_product_envelope(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    for key in ENVELOPE_KEYS:
        if key not in value:
            return False
    if not isinstance(value.get("items"), list):
        return False
    return isinstance(value.get("schema_version"), str) and isinstance(value.get("source"), str)


def sanitize_user_message(
    kind: str,
    *,
    status: int | None = None,
    using_fixture: bool = False,
) -> str:
    """Safe messages only — never raw provider/network secrets."""
    suffix = "; using fixture fallback" if using_fixture else ""
    if kind == "timeout":
        return f"Local domain API timed out{suffix}"
    if kind == "unavailable":
        return f"Local domain API unavailable{suffix}"
    if kind == "http":
        return f"Local domain API returned HTTP {status if status is not None else 'error'}{suffix}"
    if kind == "invalid_envelope":
        return "Response is not a versioned data-product envelope"
    if kind == "fixture_failed":
        return "Offline domain fixture unavailable"
    if kind == "empty":
        return "Domain product returned empty data"
    return "Unable to load domain product"


def state_from_envelope(envelope: dict[str, Any]) -> str:
    status = str(envelope.get("data_status") or "")
    items = envelope.get("items") or []
    if status == "empty" or len(items) == 0:
        return "empty"
    if status == "stale":
        return "stale"
    if status in {"malformed", "error"}:
        return "error"
    return "ready"


def load_domain_fixture(
    product_id: str,
    fixtures_dir: Optional[Path] = None,
) -> dict[str, Any]:
    require_provider("offline_domain_fixtures")
    fixtures_dir = Path(fixtures_dir or config.DOMAIN_FIXTURES_DIR)
    path = fixtures_dir / f"{product_id}.json"
    raw = path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    if not is_data_product_envelope(parsed):
        raise ValueError(f"Invalid fixture envelope for {product_id}")
    return parsed


def fetch_domain_records(
    product_id: str,
    *,
    use_fixtures: bool = False,
    fixtures_dir: Optional[Path] = None,
    timeout_ms: Optional[float] = None,
    limit: Optional[int] = None,
    fetch_impl: Optional[FetchImpl] = None,
    base_url: Optional[str] = None,
) -> DomainLoadResult:
    product = config.get_domain_product(product_id)
    if product is None:
        return DomainLoadResult(
            product_id=product_id,
            port=0,
            state="error",
            source="none",
            error_message=f"Unknown domain product id: {product_id}",
        )

    port = int(product["port"])
    provider = domain_provider_name(product_id)

    if use_fixtures:
        try:
            envelope = load_domain_fixture(product_id, fixtures_dir=fixtures_dir)
            return DomainLoadResult(
                product_id=product_id,
                port=port,
                state=state_from_envelope(envelope),
                source="fixture",
                envelope=envelope,
                items=list(envelope.get("items") or []),
            )
        except Exception:
            return DomainLoadResult(
                product_id=product_id,
                port=port,
                state="unavailable",
                source="none",
                error_message=sanitize_user_message("fixture_failed"),
            )

    require_provider(provider)
    base = (base_url or product["base_url"]).rstrip("/")
    limit = limit if limit is not None else config.DOMAIN_FETCH_LIMIT
    url = f"{base}/v1/records?limit={int(limit)}"
    timeout = (
        (timeout_ms / 1000.0)
        if timeout_ms is not None
        else config.REQUEST_TIMEOUT_SECONDS
    )

    try:
        if fetch_impl is not None:
            # Test hook: must still be GET-only loopback semantics.
            body = fetch_impl(url)
        else:
            body = request_json(
                "GET",
                url,
                provider=provider,
                timeout=timeout,
            )
        if not is_data_product_envelope(body):
            # Prefer fixture fallback for offline resilience.
            try:
                envelope = load_domain_fixture(product_id, fixtures_dir=fixtures_dir)
                return DomainLoadResult(
                    product_id=product_id,
                    port=port,
                    state=state_from_envelope(envelope),
                    source="fixture",
                    envelope=envelope,
                    items=list(envelope.get("items") or []),
                    error_message=sanitize_user_message(
                        "invalid_envelope"
                    )
                    + "; using fixture fallback",
                )
            except Exception:
                return DomainLoadResult(
                    product_id=product_id,
                    port=port,
                    state="error",
                    source="api",
                    error_message=sanitize_user_message("invalid_envelope"),
                )
        return DomainLoadResult(
            product_id=product_id,
            port=port,
            state=state_from_envelope(body),
            source="api",
            envelope=body,
            items=list(body.get("items") or []),
        )
    except UpstreamError as exc:
        timed_out = exc.kind == "timeout"
        try:
            envelope = load_domain_fixture(product_id, fixtures_dir=fixtures_dir)
            return DomainLoadResult(
                product_id=product_id,
                port=port,
                state="timeout" if timed_out else state_from_envelope(envelope),
                source="fixture",
                envelope=envelope,
                items=list(envelope.get("items") or []),
                error_message=sanitize_user_message(
                    "timeout" if timed_out else "unavailable",
                    using_fixture=True,
                ),
            )
        except Exception:
            return DomainLoadResult(
                product_id=product_id,
                port=port,
                state="timeout" if timed_out else "unavailable",
                source="none",
                error_message=sanitize_user_message(
                    "timeout" if timed_out else "unavailable"
                ),
            )
    except Exception:
        try:
            envelope = load_domain_fixture(product_id, fixtures_dir=fixtures_dir)
            return DomainLoadResult(
                product_id=product_id,
                port=port,
                state=state_from_envelope(envelope),
                source="fixture",
                envelope=envelope,
                items=list(envelope.get("items") or []),
                error_message=sanitize_user_message("unavailable", using_fixture=True),
            )
        except Exception:
            return DomainLoadResult(
                product_id=product_id,
                port=port,
                state="unavailable",
                source="none",
                error_message=sanitize_user_message("unavailable"),
            )


def fetch_all_domains(
    *,
    use_fixtures: bool = False,
    fixtures_dir: Optional[Path] = None,
    timeout_ms: Optional[float] = None,
    limit: Optional[int] = None,
    fetch_impl: Optional[FetchImpl] = None,
) -> list[DomainLoadResult]:
    results: list[DomainLoadResult] = []
    for product in config.DOMAIN_PRODUCTS:
        results.append(
            fetch_domain_records(
                product["id"],
                use_fixtures=use_fixtures,
                fixtures_dir=fixtures_dir,
                timeout_ms=timeout_ms,
                limit=limit,
                fetch_impl=fetch_impl,
            )
        )
    return results
