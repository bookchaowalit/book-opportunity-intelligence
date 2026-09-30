# Upgrade Plan

**Current state: 7/10** — clean synthesis consumer with offline CI, but 27 of
84 tests (Gold/DuckDB API) only run inside the parent monorepo, so the
lake-first path is unverified in standalone CI.

## Backlog

### P0
- (none open)

### P1
- Make the Gold/API tests runnable standalone: add an optional
  `[project.optional-dependencies] lake = ["pyarrow", "duckdb"]` plus a
  minimal local `data_lake` shim or fixture store, and a CI job using it.
- `fuzzy_title_match` uses `overlap / min(len)`, so a one-word title
  ("Bitcoin") merges with any title containing that word. Consider Jaccard
  or requiring >= 2 shared tokens; add regression fixtures first.
- CI only triggers on `main`; add `push` for all branches if feature
  branches should be gated.

### P2
- `synthesize()` has a dead branch (`failed_states` partial → "ok" twice);
  decide whether partial outages should surface as `partial`.
- Type-check `src/` with `mypy --strict` or `pyright` in CI.

## Done in this pass (2026-09-30)
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
