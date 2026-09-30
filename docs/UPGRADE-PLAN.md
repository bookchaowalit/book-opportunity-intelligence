# Upgrade Plan

**Current state: 8/10** (pass 1: 7; pass 2: 7.5; pass 3: 8) — clean synthesis
consumer; CI gates every branch, builds the wheel and now runs all 27
Gold/DuckDB tests via the pinned `solo-empire-data-lake` `[lake]` extra.

## Backlog

### P0
- (none open)

### P1
- Confirm the first CI run with `.[dev,lake]` is green (84/85 locally).
- `_title_tokens` keeps only `[a-z0-9]`, so Thai titles never fuzzy-match;
  add Thai tokenisation (e.g. character n-grams) with fixtures.

### P2
- `synthesize()` has a dead branch (`failed_states` partial → "ok" twice);
  decide whether partial outages should surface as `partial`.
- Type-check `src/` with `mypy --strict` or `pyright` in CI.

## Done in this pass (pass 1, 2026-09-30)
- Input hardening: `_safe_float` rejects NaN/Infinity/bools (a NaN
  `change_24h_pct` from a domain API crashed normalization); `_safe_int`
  catches `OverflowError`.
- Bug: `score_opportunity` treated a real score of 0 (empty domain) as
  missing and reset it to 50; dedupe/rank no longer crash on float-string
  scores.
- New `tests/test_synthesis_core.py` (20 tests) covering normalize,
  scoring, ranking, dedupe and `synthesize()` status logic.
- Removed 3 pyflakes findings; CI now runs `ruff check --select F`.
- README test section documents which suites skip standalone and why.

## Done in this pass (pass 2)
- CI triggers on push/PR for all branches (was `main` only), installs
  `.[dev]`, builds sdist + wheel and imports the installed wheel.
- `pyproject.toml`: explicit setuptools build-system, `src/` package
  discovery, `lake` (pyarrow, duckdb) and `dev` extras.
- `gold._load_shared` honours `SOLO_EMPIRE_ROOT`, so a standalone clone can
  use the monorepo lake helpers; verified locally: 84/85 tests run (was 57),
  only the opt-in e2e skips.
- `fuzzy_title_match`: a one-word title only matches an identical title and
  multi-word titles need >= 2 shared tokens (regression test added).

## Done in this pass (pass 2, continued: shared lake package)
- `[lake]` extra now pins `solo-empire-data-lake[lake]` (archive of commit
  `3523a62`), which provides every helper `gold.py` imports
  (`product_adapter`, `storage.ObjectStore/StorageError`,
  `query.query_parquet/DuckDBUnavailable`).
- New `gold.shared_runtime_available()`; test skip guards use it.
- CI installs `.[dev,lake]`, asserts the runtime imports, and runs the suite
  against a scratch `SOLO_EMPIRE_DATA_LAKE_URI`. Verified locally: 84/85 with
  the package (no `SOLO_EMPIRE_ROOT`), 58/85 + 27 clean skips without it.
