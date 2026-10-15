# book-opportunity-intelligence

**Synthesis consumer** for the free-only data-product portfolio.

This repository no longer scrapes upstream websites or search APIs in its
default path. It **reads normalized envelopes** from the seven local domain
data-product APIs, synthesizes ranked opportunities, and serves the
`opportunity.v1` contract on port **8108**.

```text
domain APIs 8101–8107
  → normalize / dedupe / score / rank
  → Gold Parquet (money_opportunities + history)
  → synthesis manifest + input lineage
  → DuckDB API :8108 (opportunity.v1)
  → optional CSV projection (CLI only)
```

Migration status: **Gold/synthesis lake-first**. API 8108 reads Gold via DuckDB
only — never `money_opportunities.csv`. Free-only, no upstream scrape, no paid
providers, no external writes (Todoist/Telegram), no public deployment by default.

## Domain inputs (read-only)

| Domain | Port | Schema | Repository |
|---|---|---|---|
| Crypto | 8101 | `crypto.v1` | `book-crypto-data` |
| Stocks | 8102 | `stock.v1` | `book-stock-data` |
| FX | 8103 | `fx.v1` | `book-fx-data` |
| DeFi | 8104 | `defi.v1` | `book-defi-data` |
| Flights | 8105 | `flight.v1` | `book-flight-data` |
| SEO | 8106 | `seo.v1` | `book-seo-data` |
| AI Tools | 8107 | `ai_tools.v1` | `book-ai-tools-data` |

Only `GET /v1/records` is used. No `POST /v1/refresh` against domain APIs.
When a domain API is down, timed out, empty, or stale, synthesis continues
safely with fixture fallback and records per-domain status.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .
python -m book_opportunities.ingest --help
book-opportunity-intelligence --help
```

### CLI (synthesis → Gold)

```bash
# Offline fixtures only (no network)
python -m book_opportunities.ingest --fixture
python -m book_opportunities.ingest --dry-run

# Default: local domain APIs 127.0.0.1:8101–8107, fixture fallback per product
python -m book_opportunities.ingest

# Gold only (skip CSV projection)
python -m book_opportunities.ingest --fixture --no-project-csv
```

Durable lake write (before any CSV):

- `gold/product=book-opportunity-intelligence/dataset=money_opportunities/...`
- `gold/product=book-opportunity-intelligence/dataset=money_opportunities_history/...`
- `control/manifests/synthesis/source=book-opportunity-intelligence/...`

Local control files under repo-local `data/`:

- `synthesis_manifest.json` — Gold keys + per-domain input lineage
- `synthesis_report.json` — run summary
- `money_opportunities.csv` / `money_opportunities_history.csv` — **optional CLI projection only**

### Local read-only API (this product)

```bash
python -m book_opportunities.api --host 127.0.0.1 --port 8108
```

Default bind: `127.0.0.1:8108`.

| Method | Path | Notes |
|---|---|---|
| GET | `/healthz` | Liveness + Gold data status (no domain upstream calls) |
| GET | `/v1/metadata` | Schema, counts, `storage_model=lake_first_gold_duckdb`, lineage |
| GET | `/v1/records?limit=50&cursor=` | Latest Gold synthesis snapshot via DuckDB |
| GET | `/v1/records/{record_id}` | Single record |
| GET | `/v1/history?limit=50&cursor=` | Gold history parts via DuckDB |
| POST | `/v1/refresh` | **403 by default** |

Each Gold row carries `synthesis_run_id` plus input lineage:
`input_source_product`, `input_source_record_id`, `input_event_id`,
`input_raw_object_key`, `input_ingest_run_id`, and domain health fields.

Consumer example:

```bash
curl -s 'http://127.0.0.1:8108/v1/records?limit=10' | python -m json.tool
```

Versioned response envelope (`opportunity.v1`):

```json
{
  "schema_version": "opportunity.v1",
  "source": "book-opportunity-intelligence",
  "retrieved_at": "2026-08-01T12:00:00Z",
  "data_status": "ok",
  "items": [],
  "next_cursor": null
}
```

## Synthesis pipeline

Deterministic and pure over domain envelopes:

1. **normalize** — map each domain item into an opportunity candidate
2. **deduplicate** — URL match, then fuzzy title overlap
3. **categorize** — stable taxonomy (`ai-content`, `freelance-services`, …)
4. **score** — bounded 0–100 from domain signals + competition
5. **rank** — score desc, then title/url for stable ordering

## Free-only defaults

```text
FREE_ONLY=true
ALLOW_PAID_PROVIDERS=false
ALLOW_EXTERNAL_WRITES=false
API_HOST=127.0.0.1
API_PORT=8108
ALLOW_REFRESH=false
```

### Provider matrix

| Provider | Classification | Notes |
|---|---|---|
| `domain_api_crypto` … `domain_api_ai_tools` | `free` | Local loopback GET only |
| `offline_domain_fixtures` | `free` | `fixtures/domain-products/*.json` |
| `offline_fixtures` | `free` | Local opportunity snapshot fixtures |
| `hacker_news_public` / `github_public` / `ddg_html` | `blocked` | Legacy scrapes removed from default path |
| `firecrawl` | `blocked` | Paid/optional path disabled |
| `telegram_write` / `todoist_write` / `email_write` | `blocked` | External writes disabled |

## Layout

```text
src/book_opportunities/
  domain_client.py   # GET-only client for 8101–8107 + fixture fallback
  synthesis.py       # normalize → dedupe → categorize → score → rank
  ingest.py          # CLI entry (synthesis consumer)
  api.py             # own read-only API on 8108
  store.py           # local CSV store for opportunity.v1
  policy.py          # free-only provider matrix
  http_client.py     # loopback GET helper
fixtures/
  domain-products/   # sanitized domain envelopes for offline demos
  money_opportunities*.csv
archive/
  legacy_scraper_ingest.py  # pre-migration mega-scraper (unreachable)
```

## Tests

### Unit / contract tests (default, offline)

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Coverage includes:

- fixture mode with zero network calls
- only `127.0.0.1:8101–8107` domain fetches
- no Firecrawl / Telegram / Todoist usage in runtime modules
- `opportunity.v1` synthesis output
- deterministic ranking and deduplication
- stale / empty / timeout / unavailable domain inputs
- untrusted numeric inputs (NaN / Infinity / garbage) never crash scoring
- GET-only API contract and blocked `POST /v1/refresh`
- no secret or raw provider error leakage

### Retired multi-API launcher and e2e (2026-10-15)

`scripts/start_local_data_apis.py` and the opt-in
`tests/integration/test_local_e2e_smoke.py` started sibling repositories
(`book-crypto-data` … `book-ai-tools-data`) that no longer exist under those
names, and were the last in-repo callers that started this producer next to
the domain APIs. Both were removed under BOO-248; the cross-domain synthesis is
retired in the active `data-product-contracts-v2` registry. The unit/contract
suite below is the supported test path.

Lake-backed Gold/API tests (27 cases in `test_gold_first.py` and
`test_api_free_only.py`) need the shared `data_lake` runtime. The `[lake]`
extra installs it (`solo-empire-data-lake`, pinned to a commit), so they run
standalone and in CI; without it they skip with a reason:

```bash
pip install -e ".[lake]"
SOLO_EMPIRE_DATA_LAKE_URI=/tmp/opportunity-lake \
python -m unittest discover -s tests
```

Without the package, `SOLO_DATA_LAKE_ROOT` may point at a data-lake
checkout whose `infra/scripts/data_lake` helpers are used instead.

Point `SOLO_EMPIRE_DATA_LAKE_URI` at a scratch directory so tests never write
into the real lake. CI runs on every branch and also builds the sdist/wheel and
imports the built wheel.

## Safety

- CLI default does **not** run the archived mega-scraper.
- API GET handlers read local synthesized data only.
- No secrets in source, fixtures, or `.env.example`.
- Do not enable billing, public bind, external writes, or `ALLOW_REFRESH`
  without owner approval.
