# opportunity.v1 consumer scan — 2026-10-15

Source owner: `bookchaowalit/opportunity-intelligence-data`, formerly
`book-opportunity-intelligence`; GitHub alias metadata reports public and
`archived=false`. Source HEAD: `e623c2c6aae61c72c9f3b26939f230991083a332`.

## Scope and method

The original `infra catalog` directories moved into owning repositories.
This scan checks **352 local Git checkouts** under the canonical GitHub clone
folder (17,712 total tracked files; Markdown excluded from matching). It also
checks 34 untracked code/config candidates (excluding credential-like paths);
none matched. No checkout was created, archived or deleted, and no service,
collector, provider or schedule was started.

For each checkout, record its HEAD, tracked-file count, working-tree status,
match path/line, matched-file SHA-256 and exit status using:

```bash
git grep -I -n -E 'opportunity\.v1|book_opportunities|opportunity_client' -- ':(exclude)*.md'
```

The full reproducible local snapshot is the sprint handoff artifact
`opportunity-consumer-scan.json` (2026-10-15). This report records all matching
owners below; Markdown inventory/history is outside runtime-caller counting.
The scan is a local source snapshot, not proof about undiscovered checkouts,
remote-only branches, deployed services or external clients. Port 8108 alone
is insufficient evidence of this contract because `news.v1` now uses it.

## Findings

| Owning checkout / HEAD | Matching paths | Classification |
|---|---|---|
| `platform-ops-cli` / `0c25202f4001f7f71372b75b31b930478e89220b` | `infra/scripts/utils/data_product_contract_guard.py:483-484` | Negative guard: rejects `opportunity.v1` in Track B consumers; does not call the service |
| `data-lake-pipelines` / `e66ec59b5e383097b6f4e4cb14fbf1d789064e24` | `infra/scripts/utils/data_product_contract_guard.py:470-471`; `infra/tests/data_lake/discovery_portfolio_test.py:61` | Negative contract guard/test; no runtime API call |
| `data-lake-pipelines` / same HEAD | `systems/architecture/data-product-contracts-v1.yaml` | Legacy registry metadata still names the old producer/consumers; not an executable client |
| `architecture-docs` / `a550ade4c40592e6f8416b5ee52d603357733629` | `systems/architecture/data-product-contracts-v1.yaml` | Same legacy registry metadata; separate registry cleanup remains |
| `sales-business-directory-scraper` / `6c334a742e3def9940738d1fa424571411cfae8a` | `location_intelligence/report.py:23`; `tests/test_location_intelligence.py:151,156` | Substring match on the distinct `location-opportunity.v1` schema; no dependency on this package or API |
| `opportunity-intelligence-data` / source HEAD above | `src/book_opportunities/`, `pyproject.toml`, `scripts/start_local_data_apis.py`, `.github/workflows/ci.yml`, `tests/`, `archive/legacy_scraper_ingest.py` | Own producer/runtime, installed entry points, manual local orchestration, test/CI references and archived source; not external downstream consumers |

`business-catalog` (the current catalog owner), `market-insights-web`, the
portfolio frontend and every other discovered checkout have no matches for
the three requested tokens. The former `cross_match_opportunities.py` and
`opportunity_client.py` files are absent from the discovered checkout tree;
their remaining references are the legacy registry rows above.

## Disposition of the scan

**No external runtime caller was found in the stated local scope. A global
zero-caller or archive-ready claim is not made.** Active source remains:

- `scripts/start_local_data_apis.py:35,132` still includes this producer in its
  service list and invokes `book_opportunities.ingest` when manually run.
- `pyproject.toml:24-25` still installs ingest/API CLI entry points, and
  `src/book_opportunities/config.py` still defines `opportunity.v1` on 8108.
- The legacy v1 contract registry still lists the synthesis and old consumers.

No remaining external caller required a bounded migration. Replacing the
manual synthesis service with a different domain contract is not an obvious
compatibility-preserving change, so its disposition and registry cleanup are
left to the owner before declaring full retirement. This scan does not change
runtime behavior or authorize publication, collection or an archive action.
