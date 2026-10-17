#!/usr/bin/env python3
"""Opportunity synthesis CLI — Gold-first consumer of domain data-product APIs.

Default path:
  1. GET http://127.0.0.1:8101–8107/v1/records (read-only; no upstream scrape)
  2. Fall back to fixtures/domain-products/*.json per product when APIs are down
  3. Deterministic synthesis: normalize → dedupe → categorize → score → rank
  4. Write Gold Parquet + synthesis manifest (durable)
  5. Optional CSV projection under repo-local data/ (CLI only)

Never scrapes upstream providers. Never calls Firecrawl, Telegram, or Todoist
in the default free-only path. Legacy mega-scraper lives under archive/.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import config
from . import gold
from .domain_client import fetch_all_domains
from .policy import external_writes_allowed, require_external_writes
from .store import seed_fixtures
from .synthesis import FIELDNAMES, synthesize


def _write_csv(path: Path, rows: list[dict[str, Any]], *, append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    file_exists = path.exists() and append
    with path.open(mode, newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in FIELDNAMES})


def project_csv(
    items: list[dict[str, Any]],
    *,
    output_dir: Path,
) -> tuple[Path, Path]:
    """Optional CLI projection after successful Gold write."""
    records_path = output_dir / config.RECORDS_FILE
    history_path = output_dir / config.HISTORY_FILE
    _write_csv(records_path, items, append=False)
    _write_csv(history_path, items, append=True)
    return records_path, history_path


def _print_summary(result: dict[str, Any]) -> None:
    print(f"  schema_version={result['schema_version']}")
    print(f"  data_status={result['data_status']}")
    print(f"  synthesis_run_id={result.get('synthesis_run_id')}")
    print(f"  domains_usable={result['domains_usable']}/{result['domains_total']}")
    print(f"  opportunities={len(result['items'])}")
    for row in result["input_summary"]:
        err = f" ({row['error_message']})" if row.get("error_message") else ""
        print(
            f"    - {row['product_id']:10s} :{row['port']} "
            f"state={row['state']:11s} source={row['source']:7s} "
            f"items={row['item_count']}{err}"
        )
    for i, item in enumerate(result["items"][:5], 1):
        print(
            f"    #{i} [{item.get('trend_score')}] "
            f"{item.get('title', '')[:70]} ({item.get('category')})"
        )


def write_gold_from_synthesis(
    synthesized: dict[str, Any],
    *,
    data_lake_uri: Optional[str],
    output_dir: Path,
) -> dict[str, Any]:
    """Durable Gold write for both snapshot + history datasets + local manifest."""
    items = list(synthesized.get("items") or [])
    run_id = str(synthesized.get("synthesis_run_id") or gold.make_synthesis_run_id())
    scraped_at = ""
    if items:
        scraped_at = str(items[0].get("scraped_at") or "")
    if not scraped_at:
        # UTC: naive stamps are read back as UTC by the shared product_store.
        scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    input_summary = list(synthesized.get("input_summary") or [])
    data_status = str(synthesized.get("data_status") or "ok")
    uri = data_lake_uri or gold.default_data_lake_uri()

    gold_ops = gold.write_gold_dataset(
        items,
        dataset=config.LAKE_DATASET_OPPORTUNITIES,
        synthesis_run_id=run_id,
        scraped_at=scraped_at,
        data_lake_uri=uri,
        input_summary=input_summary,
        data_status=data_status,
    )
    gold_hist = gold.write_gold_dataset(
        items,
        dataset=config.LAKE_DATASET_HISTORY,
        synthesis_run_id=run_id,
        scraped_at=scraped_at,
        data_lake_uri=uri,
        input_summary=input_summary,
        data_status=data_status,
    )
    manifest_path = gold.write_local_synthesis_manifest(
        data_dir=output_dir,
        synthesis_run_id=run_id,
        gold_result=gold_ops,
        input_summary=input_summary,
        data_status=data_status,
        item_count=len(items),
    )
    print(
        f"  Gold opportunities: run_id={run_id} records={gold_ops.get('record_count')} "
        f"key={gold_ops.get('gold_key')}"
    )
    print(
        f"  Gold history: records={gold_hist.get('record_count')} "
        f"key={gold_hist.get('gold_key')}"
    )
    print(f"  Synthesis manifest → {manifest_path}")
    return {
        "synthesis_run_id": run_id,
        "opportunities": gold_ops,
        "history": gold_hist,
        "manifest_path": str(manifest_path),
        "data_lake_uri": uri,
    }


def run_synthesis(
    *,
    use_fixtures: bool,
    dry_run: bool,
    output_dir: Path,
    min_score: int = 0,
    timeout_ms: Optional[float] = None,
    fetch_impl=None,
    data_lake_uri: Optional[str] = None,
    project_csv_flag: bool = True,
) -> dict[str, Any]:
    results = fetch_all_domains(
        use_fixtures=use_fixtures,
        timeout_ms=timeout_ms,
        fetch_impl=fetch_impl,
    )
    synthesized = synthesize(results)
    items = [
        item
        for item in synthesized["items"]
        if int(item.get("trend_score") or 0) >= min_score
    ]
    synthesized = {**synthesized, "items": items}

    if dry_run:
        print("  Dry-run: not writing Gold or CSV.")
        return synthesized

    output_dir.mkdir(parents=True, exist_ok=True)

    # Fail-closed: Gold must succeed before any CSV projection.
    try:
        gold_result = write_gold_from_synthesis(
            synthesized,
            data_lake_uri=data_lake_uri,
            output_dir=output_dir,
        )
    except (gold.GoldUnavailable, gold.GoldWriteError) as exc:
        print(f"  Gold write failed (CSV not written): {exc}")
        raise

    # Machine-readable synthesis report (local only; includes input lineage).
    report_path = output_dir / "synthesis_report.json"
    report = {
        "schema_version": synthesized["schema_version"],
        "source": synthesized["source"],
        "retrieved_at": synthesized["retrieved_at"],
        "data_status": synthesized["data_status"],
        "synthesis_run_id": synthesized.get("synthesis_run_id"),
        "layer": "gold",
        "item_count": len(items),
        "input_summary": synthesized["input_summary"],
        "domains_usable": synthesized["domains_usable"],
        "domains_total": synthesized["domains_total"],
        "gold": {
            "opportunities_key": gold_result["opportunities"].get("gold_key"),
            "history_key": gold_result["history"].get("gold_key"),
            "manifest_key": gold_result["opportunities"].get("manifest_key"),
            "data_lake_uri": gold_result.get("data_lake_uri"),
        },
        "free_only": config.FREE_ONLY,
        "allow_external_writes": config.ALLOW_EXTERNAL_WRITES,
        "allow_paid_providers": config.ALLOW_PAID_PROVIDERS,
        "no_upstream_scrape": True,
    }
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"  Report → {report_path}")

    if project_csv_flag:
        records_path, history_path = project_csv(items, output_dir=output_dir)
        print(f"  CSV projection: {len(items)} records → {records_path}")
        print(f"  CSV history append → {history_path}")
    else:
        print("  CSV projection skipped (--no-project-csv)")

    synthesized = {
        **synthesized,
        "gold": gold_result,
        "projected_csv": project_csv_flag,
    }
    return synthesized


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Synthesize opportunities from local domain data-product APIs "
            "(ports 8101–8107). Gold-first, free-only, GET /v1/records only, "
            "no upstream scrapes."
        )
    )
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="Use offline domain fixtures only (no network)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Synthesize from fixtures and print results without writing Gold/CSV",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=0,
        help="Minimum trend_score filter (0-100)",
    )
    parser.add_argument(
        "--output-dir",
        default=str(config.DATA_DIR),
        help="Local output directory for manifest/CSV projection (default: ./data)",
    )
    parser.add_argument(
        "--data-lake-uri",
        default=None,
        help="Object storage / local lake URI (default: SOLO_EMPIRE_DATA_LAKE_URI or monorepo data/lake)",
    )
    parser.add_argument(
        "--no-project-csv",
        action="store_true",
        help="Skip local CSV projection after Gold write",
    )
    parser.add_argument(
        "--timeout-ms",
        type=float,
        default=None,
        help="Per-domain API timeout in milliseconds (default from env)",
    )
    parser.add_argument(
        "--seed-opportunity-fixtures",
        action="store_true",
        help="Copy legacy opportunity CSV fixtures into data/ without synthesis",
    )
    # Explicitly rejected side-effect flags (kept for CLI compatibility messaging).
    parser.add_argument(
        "--send-digest",
        action="store_true",
        help="(Blocked) Telegram digest — external write disabled by default",
    )
    parser.add_argument(
        "--push-todoist",
        action="store_true",
        help="(Blocked) Todoist push — external write disabled by default",
    )
    parser.add_argument(
        "--legacy-scrape",
        action="store_true",
        help="(Blocked) Former mega-scraper path is archived and unreachable",
    )
    args = parser.parse_args(argv)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] book-opportunity-intelligence synthesis consumer (gold-first)")
    print(
        f"  free_only={config.FREE_ONLY} allow_paid={config.ALLOW_PAID_PROVIDERS} "
        f"allow_external_writes={config.ALLOW_EXTERNAL_WRITES}"
    )

    if args.send_digest or args.push_todoist:
        action = "telegram" if args.send_digest else "todoist"
        try:
            require_external_writes(action)
        except PermissionError as exc:
            print(f"  Blocked external write: {exc}")
            return 2
        if not external_writes_allowed():
            print("  External writes are disabled.")
            return 2

    if args.legacy_scrape:
        print(
            "  Blocked: legacy mega-scraper is archived under archive/legacy_scraper_ingest.py "
            "and is not part of the default free-only path."
        )
        return 2

    out = Path(args.output_dir)
    config.DATA_DIR = out  # type: ignore[misc]

    if args.seed_opportunity_fixtures:
        seed_fixtures(out)
        print(f"  Seeded opportunity CSV fixtures under {out}")
        return 0

    use_fixtures = bool(args.fixture or args.dry_run)
    if use_fixtures:
        print("  Mode: offline domain fixtures (no network)")
    else:
        print("  Mode: local domain APIs 127.0.0.1:8101–8107 with fixture fallback")

    if args.data_lake_uri:
        print(f"  Data lake: {args.data_lake_uri}")
    else:
        try:
            print(f"  Data lake: {gold.default_data_lake_uri()}")
        except Exception:  # noqa: BLE001
            print("  Data lake: (default discovery)")

    try:
        result = run_synthesis(
            use_fixtures=use_fixtures,
            dry_run=bool(args.dry_run),
            output_dir=out,
            min_score=int(args.min_score),
            timeout_ms=args.timeout_ms,
            data_lake_uri=args.data_lake_uri,
            project_csv_flag=not bool(args.no_project_csv),
        )
    except (gold.GoldUnavailable, gold.GoldWriteError) as exc:
        print(f"  Failed: {exc}")
        return 1

    _print_summary(result)
    print(
        "  Done. Own API remains available on port 8108 "
        "(GET Gold via DuckDB; POST /v1/refresh → 403)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
