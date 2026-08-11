# Archive (unreachable from default runtime)

This directory keeps the pre-migration **mega-scraper** implementation for
historical reference only.

| File | Notes |
|---|---|
| `legacy_scraper_ingest.py` | Former `book_opportunities.ingest` that scraped HN/GitHub/DDG and optionally Firecrawl/Telegram/Todoist |

**Not imported** by the package default path. Runtime synthesis reads only local
data-product APIs on ports **8101–8107** (or offline domain fixtures).

Do not re-enable Firecrawl, Telegram send, Todoist push, or other external
writes without explicit owner approval and `ALLOW_EXTERNAL_WRITES=true` /
`ALLOW_PAID_PROVIDERS=true`.
