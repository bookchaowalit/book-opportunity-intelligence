"""Free-only provider policy for opportunity synthesis consumer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import config


# Default path consumes only local domain-product loopback APIs / offline fixtures.
# Legacy scrapers, paid search, and external writes stay blocked.
PROVIDERS: dict[str, dict[str, str]] = {
    "domain_api_crypto": {
        "status": "free",
        "reason": "Local book-crypto-data API on 127.0.0.1:8101 (GET only)",
    },
    "domain_api_stocks": {
        "status": "free",
        "reason": "Local book-stock-data API on 127.0.0.1:8102 (GET only)",
    },
    "domain_api_fx": {
        "status": "free",
        "reason": "Local book-fx-data API on 127.0.0.1:8103 (GET only)",
    },
    "domain_api_defi": {
        "status": "free",
        "reason": "Local book-defi-data API on 127.0.0.1:8104 (GET only)",
    },
    "domain_api_flights": {
        "status": "free",
        "reason": "Local book-flight-data API on 127.0.0.1:8105 (GET only)",
    },
    "domain_api_seo": {
        "status": "free",
        "reason": "Local book-seo-data API on 127.0.0.1:8106 (GET only)",
    },
    "domain_api_ai_tools": {
        "status": "free",
        "reason": "Local book-ai-tools-data API on 127.0.0.1:8107 (GET only)",
    },
    "offline_domain_fixtures": {
        "status": "free",
        "reason": "Sanitized domain fixtures under fixtures/domain-products/",
    },
    "offline_fixtures": {
        "status": "free",
        "reason": "Local opportunity fixtures / synthesized snapshot data",
    },
    # Legacy / side-effect providers — unreachable from default synthesis path
    "hacker_news_public": {
        "status": "blocked",
        "reason": "Legacy direct HN scrape removed; use domain APIs instead",
    },
    "github_public": {
        "status": "blocked",
        "reason": "Legacy direct GitHub scrape removed; use domain APIs instead",
    },
    "ddg_html": {
        "status": "blocked",
        "reason": "Direct DuckDuckGo HTML scrape removed from synthesis consumer",
    },
    "firecrawl": {
        "status": "blocked",
        "reason": "Firecrawl paid/optional path disabled",
    },
    "paid_search": {
        "status": "blocked",
        "reason": "Paid search APIs disabled",
    },
    "telegram_write": {
        "status": "blocked",
        "reason": "Telegram send is an external write",
    },
    "todoist_write": {
        "status": "blocked",
        "reason": "Todoist push is an external write",
    },
    "email_write": {
        "status": "blocked",
        "reason": "Email send is an external write",
    },
}


@dataclass(frozen=True)
class ProviderDecision:
    name: str
    allowed: bool
    status: str
    reason: str


def provider_status(name: str) -> str:
    meta = PROVIDERS.get(name)
    if not meta:
        return "unknown"
    return meta["status"]


def evaluate_provider(name: str) -> ProviderDecision:
    meta = PROVIDERS.get(name)
    if meta is None:
        return ProviderDecision(
            name=name,
            allowed=False,
            status="unknown",
            reason="Provider is not positively classified as free for the intended use",
        )

    status = meta["status"]
    reason = meta["reason"]

    if status == "blocked":
        return ProviderDecision(name=name, allowed=False, status=status, reason=reason)

    if status == "unknown":
        if config.FREE_ONLY and not config.ALLOW_PAID_PROVIDERS:
            return ProviderDecision(
                name=name,
                allowed=False,
                status=status,
                reason=reason + " (disabled while FREE_ONLY=true)",
            )
        return ProviderDecision(name=name, allowed=True, status=status, reason=reason)

    if status != "free":
        return ProviderDecision(
            name=name,
            allowed=False,
            status="unknown",
            reason=f"Unexpected provider status: {status}",
        )

    return ProviderDecision(name=name, allowed=True, status=status, reason=reason)


def require_provider(name: str) -> ProviderDecision:
    decision = evaluate_provider(name)
    if not decision.allowed:
        raise PermissionError(
            f"Provider {name!r} blocked (status={decision.status}): {decision.reason}"
        )
    return decision


def external_writes_allowed() -> bool:
    return bool(config.ALLOW_EXTERNAL_WRITES) and not config.FREE_ONLY


def require_external_writes(action: str) -> None:
    if not external_writes_allowed():
        raise PermissionError(
            f"External write {action!r} blocked: ALLOW_EXTERNAL_WRITES=false / FREE_ONLY=true"
        )


def provider_matrix() -> list[dict[str, Any]]:
    rows = []
    for name in sorted(PROVIDERS):
        decision = evaluate_provider(name)
        rows.append(
            {
                "provider": name,
                "classification": PROVIDERS[name]["status"],
                "allowed_now": decision.allowed,
                "reason": decision.reason,
            }
        )
    return rows


def missing_credential_status(env_var: str) -> dict[str, Any]:
    import os

    present = bool(os.environ.get(env_var, "").strip())
    return {
        "credential": env_var,
        "present": present,
        "status": "present" if present else "missing",
        "effect": "ignored_in_free_only" if not present else "optional_only",
    }


def domain_provider_name(product_id: str) -> str:
    return f"domain_api_{product_id}"
