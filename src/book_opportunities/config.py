"""Runtime configuration for book-opportunity-intelligence (synthesis consumer)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(PROJECT_ROOT / "data")))
FIXTURES_DIR = Path(os.environ.get("FIXTURES_DIR", str(PROJECT_ROOT / "fixtures")))
DOMAIN_FIXTURES_DIR = Path(
    os.environ.get("DOMAIN_FIXTURES_DIR", str(FIXTURES_DIR / "domain-products"))
)

REPO_NAME = "book-opportunity-intelligence"
DOMAIN = "opportunities"
SCHEMA_VERSION = "opportunity.v1"
RECORDS_FILE = "money_opportunities.csv"
HISTORY_FILE = "money_opportunities_history.csv"
LINEAGE_FILE = "synthesis_manifest.json"
ID_FIELDS = ["url"]
ID_SEP = ":"

# Gold/synthesis lake contract (not Bronze — reads domain products, writes Gold).
LAKE_SOURCE = REPO_NAME
LAKE_DOMAIN = "opportunities"
LAKE_DATASET_OPPORTUNITIES = "money_opportunities"
LAKE_DATASET_HISTORY = "money_opportunities_history"
LAKE_BRONZE_SCHEMA_VERSION = "1"  # unused for gold writes
LAKE_GOLD_SCHEMA_VERSION = "1"
LAKE_LAYER = "gold"
LAKE_PRIVACY_CLASS = "internal"
LAKE_RETENTION_CLASS = "operational"
DATA_LAKE_URI = os.environ.get(
    "SOLO_EMPIRE_DATA_LAKE_URI",
    os.environ.get("DATA_LAKE_URI", ""),
)
SOLO_DATA_LAKE_ROOT = os.environ.get("SOLO_DATA_LAKE_ROOT", "")

# This product's own API (synthesis output)
API_HOST = os.environ.get("API_HOST", "127.0.0.1")
API_PORT = int(os.environ.get("API_PORT", "8108"))


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


FREE_ONLY = env_bool("FREE_ONLY", True)
ALLOW_PAID_PROVIDERS = env_bool("ALLOW_PAID_PROVIDERS", False)
ALLOW_EXTERNAL_WRITES = env_bool("ALLOW_EXTERNAL_WRITES", False)
ALLOW_REFRESH = env_bool("ALLOW_REFRESH", False)
REFRESH_TOKEN = os.environ.get("REFRESH_TOKEN", "")
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "4"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "1"))
MAX_RESPONSE_BYTES = int(os.environ.get("MAX_RESPONSE_BYTES", str(2_000_000)))
STALE_AFTER_HOURS = float(os.environ.get("STALE_AFTER_HOURS", "24"))
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "20"))
DOMAIN_FETCH_LIMIT = int(os.environ.get("DOMAIN_FETCH_LIMIT", "50"))

# Upstream domain data products consumed for synthesis (GET /v1/records only).
# Ports match the free-only local data-product portfolio.
DOMAIN_PRODUCTS: tuple[dict[str, Any], ...] = (
    {
        "id": "crypto",
        "title": "Crypto Markets",
        "repo": "book-crypto-data",
        "schema_version": "crypto.v1",
        "port": 8101,
        "base_url": "http://127.0.0.1:8101",
    },
    {
        "id": "stocks",
        "title": "Stock Portfolio",
        "repo": "book-stock-data",
        "schema_version": "stock.v1",
        "port": 8102,
        "base_url": "http://127.0.0.1:8102",
    },
    {
        "id": "fx",
        "title": "Exchange Rates",
        "repo": "book-fx-data",
        "schema_version": "fx.v1",
        "port": 8103,
        "base_url": "http://127.0.0.1:8103",
    },
    {
        "id": "defi",
        "title": "DeFi Yields",
        "repo": "book-defi-data",
        "schema_version": "defi.v1",
        "port": 8104,
        "base_url": "http://127.0.0.1:8104",
    },
    {
        "id": "flights",
        "title": "Flight Prices",
        "repo": "book-flight-data",
        "schema_version": "flight.v1",
        "port": 8105,
        "base_url": "http://127.0.0.1:8105",
    },
    {
        "id": "seo",
        "title": "SEO Rankings",
        "repo": "book-seo-data",
        "schema_version": "seo.v1",
        "port": 8106,
        "base_url": "http://127.0.0.1:8106",
    },
    {
        "id": "ai_tools",
        "title": "AI Tools",
        "repo": "book-ai-tools-data",
        "schema_version": "ai_tools.v1",
        "port": 8107,
        "base_url": "http://127.0.0.1:8107",
    },
)


def get_domain_product(product_id: str) -> dict[str, Any] | None:
    for product in DOMAIN_PRODUCTS:
        if product["id"] == product_id:
            return product
    return None
