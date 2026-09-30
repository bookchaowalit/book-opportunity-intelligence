# Upgrade Plan

**Current state: 7.5/10** (pass 1: 7; pass 2: 7 -> 7.5) — clean synthesis
consumer; CI gates every branch and builds the wheel; the 27 Gold/DuckDB tests
now run from a standalone clone via `SOLO_EMPIRE_ROOT` + `.[lake]`, but still
not in CI (the monorepo helpers are not public).

## Backlog

### P0
- (none open)

### P1
- Run the lake tests in CI: either vendor a minimal `data_lake` shim
  (`product_adapter.default_data_lake_uri/resolve_lake_root`, `ObjectStore`,
  `query_parquet`) under `tests/` or check out the monorepo with a read-only
  deploy key; then add a CI job with `.[lake]`.
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
