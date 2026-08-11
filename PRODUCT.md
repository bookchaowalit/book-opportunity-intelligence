# book-opportunity-intelligence — Product brief

**Slug:** `bookchaowalit/book-opportunity-intelligence`  
**Generated:** 2026-08-11 (bulk Book Dev closeout)  
**Status:** starter / portfolio boundary

## Purpose

Portfolio repository under Book Dev. This brief records ownership and the
current honest status so the nested tree is not an empty shell in the task
system.

## Runnable path

See `README.md` for install and run instructions when present.

## Limits

- Not claimed as production-ready unless README and tests prove it.
- Mobile smoke / emulator acceptance is separate and toolchain-dependent.

## Source README excerpt

```
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


```
