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

### Opt-in local end-to-end smoke (ports 8101–8108)

Starts sibling domain APIs on `127.0.0.1:8101–8107` with **fixture/local data
only**, runs synthesis against those live loopback APIs, serves this product on
`:8108`, and checks contracts. Requires free ports and the seven sibling
repositories under `../`. No paid APIs, credentials, or internet access.

```bash
# from this repository root
RUN_LOCAL_E2E=1 PYTHONPATH=src python3 tests/integration/test_local_e2e_smoke.py

# or via unittest
RUN_LOCAL_E2E=1 PYTHONPATH=src python3 -m unittest tests.integration.test_local_e2e_smoke -v
```

Without `RUN_LOCAL_E2E=1` the e2e test is skipped so default CI stays offline.

Lake-backed Gold/API tests (27 cases in `test_gold_first.py` and
`test_api_free_only.py`) also skip unless `pyarrow`, `duckdb` and the parent
monorepo `data_lake` package are importable; they do not run in standalone CI.

Startup map:

| Port | Process | Data |
|---|---|---|
| 8101–8107 | sibling `*.api` modules | each repo `fixtures/` seeded into a temp `DATA_DIR` |
| 8108 | `book_opportunities.api` | synthesis output under a temp `data/` |

The e2e runner asserts only loopback hosts are contacted, checks
`opportunity.v1` synthesis output, verifies
`GET /healthz|/v1/metadata|/v1/records|/v1/history` on `:8108`, expects
`POST /v1/refresh → 403`, and terminates all child processes on exit.
Domain HTTP uses `httpx` when installed, otherwise stdlib `urllib` for
loopback GET.

## Safety

- CLI default does **not** run the archived mega-scraper.
- API GET handlers read local synthesized data only.
- No secrets in source, fixtures, or `.env.example`.
- Do not enable billing, public bind, external writes, or `ALLOW_REFRESH`
  without owner approval.
