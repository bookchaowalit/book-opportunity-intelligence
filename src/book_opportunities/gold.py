"""Gold/synthesis Parquet writer + DuckDB reader for opportunity intelligence.

Does not scrape. Writes purpose-built Gold datasets under:
  gold/product=book-opportunity-intelligence/dataset=<name>/...

CSV remains an optional CLI projection only.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import config


GOLD_COLUMNS = (
    "id",
    "synthesis_run_id",
    "title",
    "category",
    "source",
    "price",
    "url",
    "trend_score",
    "competition_level",
    "notes",
    "scraped_at",
    "event_time",
    "score_delta",
    "trend_direction",
    "rank_position",
    "input_source_product",
    "input_source_record_id",
    "input_event_id",
    "input_raw_object_key",
    "input_ingest_run_id",
    "input_domain_status",
    "input_domain_source",
    "layer",
    "schema_version",
)


def _shared_scripts_candidates() -> list[Path]:
    """Where the monorepo ``infra/scripts/data_lake`` helpers may live.

    ``SOLO_EMPIRE_ROOT`` wins, so a standalone clone (not nested inside the
    monorepo) can still use the shared lake helpers; otherwise walk up from
    this project as before.
    """
    candidates: list[Path] = []
    if config.SOLO_EMPIRE_ROOT:
        candidates.append(Path(config.SOLO_EMPIRE_ROOT).expanduser() / "infra" / "scripts")
    cur = config.PROJECT_ROOT.resolve()
    candidates.extend(parent / "infra" / "scripts" for parent in [cur, *cur.parents])
    return candidates


def _load_shared():
    for scripts in _shared_scripts_candidates():
        if (scripts / "data_lake" / "product_adapter.py").is_file():
            if str(scripts) not in sys.path:
                sys.path.insert(0, str(scripts))
            break
    from data_lake import product_adapter as pa  # type: ignore
    from data_lake.storage import ObjectStore, StorageError  # type: ignore
    from data_lake.query import query_parquet, DuckDBUnavailable  # type: ignore
    return pa, ObjectStore, StorageError, query_parquet, DuckDBUnavailable


def shared_runtime_available() -> bool:
    """Return True when the shared ``data_lake`` runtime and its deps import.

    Covers the installed ``[lake]`` extra (``solo-empire-data-lake`` with
    pyarrow/duckdb) and the ``SOLO_EMPIRE_ROOT`` / parent-checkout fallback.
    """
    try:
        import duckdb  # noqa: F401
        import pyarrow  # noqa: F401

        _load_shared()
    except ImportError:
        return False
    return True


class GoldUnavailable(RuntimeError):
    pass


class GoldWriteError(RuntimeError):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def make_synthesis_run_id(seed: str = "") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha1(
        f"{stamp}:{seed}:{config.REPO_NAME}".encode("utf-8")
    ).hexdigest()[:12]
    return f"{stamp}-{digest}"


def default_data_lake_uri() -> str:
    pa, *_ = _load_shared()
    return pa.default_data_lake_uri(
        data_lake_uri=config.DATA_LAKE_URI,
        project_root=config.PROJECT_ROOT,
        solo_empire_root=config.SOLO_EMPIRE_ROOT,
    )


def gold_dataset_dir(
    dataset: str,
    *,
    data_lake_uri: Optional[str] = None,
) -> Path:
    pa, ObjectStore, StorageError, query_parquet, DuckDBUnavailable = _load_shared()
    del ObjectStore, StorageError, query_parquet, DuckDBUnavailable
    uri = data_lake_uri or default_data_lake_uri()
    root = pa.resolve_lake_root(uri)
    return (
        root
        / "gold"
        / f"product={config.LAKE_SOURCE}"
        / f"dataset={dataset}"
        / f"schema_version={config.LAKE_GOLD_SCHEMA_VERSION}"
    )


def list_gold_parquet_files(
    dataset: str,
    *,
    data_lake_uri: Optional[str] = None,
) -> list[Path]:
    base = gold_dataset_dir(dataset, data_lake_uri=data_lake_uri)
    if not base.exists():
        return []
    return sorted(p for p in base.rglob("*.parquet") if p.is_file())


def _write_gold_parquet_local(rows: list[dict[str, Any]], destination: Path) -> None:
    try:
        import pyarrow as pa  # type: ignore
        import pyarrow.parquet as pq  # type: ignore
    except ImportError as exc:
        raise GoldUnavailable(
            "Gold Parquet write requires pyarrow "
            "(infra/requirements-data-lake.txt)"
        ) from exc
    schema = pa.schema([(c, pa.string()) for c in GOLD_COLUMNS])
    arrays = [
        pa.array([str(row.get(c, "") if row.get(c) is not None else "") for row in rows], type=pa.string())
        for c in GOLD_COLUMNS
    ]
    table = pa.Table.from_arrays(arrays, schema=schema)
    destination.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, destination, compression="zstd", version="2.6")


def opportunities_to_gold_rows(
    items: list[dict[str, Any]],
    *,
    synthesis_run_id: str,
    scraped_at: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, item in enumerate(items, 1):
        row = {c: "" for c in GOLD_COLUMNS}
        row.update(
            {
                "id": str(item.get("id") or item.get("record_id") or item.get("url") or idx),
                "synthesis_run_id": synthesis_run_id,
                "title": str(item.get("title") or ""),
                "category": str(item.get("category") or ""),
                "source": str(item.get("source") or ""),
                "price": str(item.get("price") or ""),
                "url": str(item.get("url") or ""),
                "trend_score": str(item.get("trend_score") if item.get("trend_score") is not None else ""),
                "competition_level": str(item.get("competition_level") or ""),
                "notes": str(item.get("notes") or ""),
                "scraped_at": str(item.get("scraped_at") or scraped_at),
                "event_time": str(item.get("event_time") or item.get("scraped_at") or scraped_at),
                "score_delta": str(item.get("score_delta", 0)),
                "trend_direction": str(item.get("trend_direction") or "stable"),
                "rank_position": str(idx),
                "input_source_product": str(item.get("input_source_product") or item.get("domain_product") or ""),
                "input_source_record_id": str(item.get("input_source_record_id") or ""),
                "input_event_id": str(item.get("input_event_id") or ""),
                "input_raw_object_key": str(item.get("input_raw_object_key") or ""),
                "input_ingest_run_id": str(item.get("input_ingest_run_id") or ""),
                "input_domain_status": str(item.get("input_domain_status") or item.get("domain_status") or ""),
                "input_domain_source": str(item.get("input_domain_source") or item.get("domain_input_source") or ""),
                "layer": config.LAKE_LAYER,
                "schema_version": config.LAKE_GOLD_SCHEMA_VERSION,
            }
        )
        rows.append(row)
    return rows


def write_gold_dataset(
    items: list[dict[str, Any]],
    *,
    dataset: str,
    synthesis_run_id: str,
    scraped_at: str,
    data_lake_uri: Optional[str] = None,
    input_summary: Optional[list[dict[str, Any]]] = None,
    data_status: str = "ok",
) -> dict[str, Any]:
    """Write Gold Parquet part + synthesis control manifest. No CSV."""
    pa, ObjectStore, StorageError, _query_parquet, _DuckDBUnavailable = _load_shared()
    uri = data_lake_uri or default_data_lake_uri()
    rows = opportunities_to_gold_rows(
        items, synthesis_run_id=synthesis_run_id, scraped_at=scraped_at
    )
    as_of = scraped_at[:10] if len(scraped_at) >= 10 else utc_now_iso()[:10]
    # Prefer ISO date from synthesis_run_id when present
    if "T" in synthesis_run_id:
        as_of = synthesis_run_id[:8]
        as_of = f"{as_of[:4]}-{as_of[4:6]}-{as_of[6:8]}"
    part_name = f"part-{synthesis_run_id}.parquet"
    gold_key = (
        f"gold/product={config.LAKE_SOURCE}/dataset={dataset}/"
        f"schema_version={config.LAKE_GOLD_SCHEMA_VERSION}/"
        f"as_of_date={as_of}/{part_name}"
    )
    manifest_key = (
        f"control/manifests/synthesis/source={config.LAKE_SOURCE}/"
        f"dataset={dataset}/run_id={synthesis_run_id}.json"
    )

    try:
        with tempfile.TemporaryDirectory(prefix="solo-empire-gold-") as temp_dir:
            local_path = Path(temp_dir) / "part.parquet"
            _write_gold_parquet_local(rows, local_path)
            parquet_bytes = local_path.read_bytes()
        store = ObjectStore(uri)
        gold_put = store.put_bytes(
            gold_key, parquet_bytes, content_type="application/vnd.apache.parquet"
        )
        manifest = {
            "manifest_version": "1",
            "layer": "gold",
            "synthesis_run_id": synthesis_run_id,
            "source": config.LAKE_SOURCE,
            "domain": config.LAKE_DOMAIN,
            "dataset": dataset,
            "schema_version": config.LAKE_GOLD_SCHEMA_VERSION,
            "product_schema_version": config.SCHEMA_VERSION,
            "received_at": utc_now_iso(),
            "record_count": len(rows),
            "data_status": data_status,
            "gold": {
                "key": gold_put.key,
                "sha256": gold_put.sha256,
                "bytes": len(parquet_bytes),
                "columns": list(GOLD_COLUMNS),
            },
            "input_summary": input_summary or [],
            "privacy_class": config.LAKE_PRIVACY_CLASS,
            "retention_class": config.LAKE_RETENTION_CLASS,
            "metadata": {
                "adapter": config.LAKE_SOURCE,
                "role": "gold_synthesis",
                "no_upstream_scrape": True,
            },
        }
        store.put_bytes(
            manifest_key,
            (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode(
                "utf-8"
            ),
            content_type="application/json",
        )
    except Exception as exc:  # noqa: BLE001
        raise GoldWriteError(f"Gold write failed: {exc}") from exc

    return {
        "status": "success",
        "synthesis_run_id": synthesis_run_id,
        "record_count": len(rows),
        "gold_key": gold_key,
        "manifest_key": manifest_key,
        "data_lake_uri": uri,
        "layer": "gold",
    }


def write_local_synthesis_manifest(
    *,
    data_dir: Path,
    synthesis_run_id: str,
    gold_result: dict[str, Any],
    input_summary: list[dict[str, Any]],
    data_status: str,
    item_count: int,
) -> Path:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / config.LINEAGE_FILE
    body = {
        "schema_version": config.SCHEMA_VERSION,
        "layer": "gold",
        "source": config.LAKE_SOURCE,
        "domain": config.LAKE_DOMAIN,
        "synthesis_run_id": synthesis_run_id,
        "data_status": data_status,
        "item_count": item_count,
        "retrieved_at": utc_now_iso(),
        "gold": {
            "key": gold_result.get("gold_key"),
            "manifest_key": gold_result.get("manifest_key"),
            "record_count": gold_result.get("record_count"),
        },
        "input_summary": input_summary,
        "free_only": config.FREE_ONLY,
        "allow_external_writes": config.ALLOW_EXTERNAL_WRITES,
        "no_upstream_scrape": True,
    }
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    return path


def load_lineage(data_dir: Optional[Path] = None) -> Optional[dict[str, Any]]:
    path = Path(data_dir or config.DATA_DIR) / config.LINEAGE_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def read_gold_rows(
    dataset: str,
    *,
    data_lake_uri: Optional[str] = None,
    latest_run_only: bool = True,
) -> list[dict[str, Any]]:
    """Read Gold Parquet via DuckDB. Empty list if no parts yet."""
    files = list_gold_parquet_files(dataset, data_lake_uri=data_lake_uri)
    if not files:
        return []
    pa, ObjectStore, StorageError, query_parquet, DuckDBUnavailable = _load_shared()
    del pa, ObjectStore, StorageError
    try:
        rows = query_parquet(
            [str(p) for p in files],
            "SELECT * FROM lake_table ORDER BY synthesis_run_id DESC, rank_position ASC",
        )
    except DuckDBUnavailable as exc:
        raise GoldUnavailable(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise GoldWriteError(f"Gold DuckDB query failed: {exc}") from exc

    if not latest_run_only or not rows:
        return rows
    # Keep only the latest synthesis_run_id partition for snapshot API.
    latest = max(str(r.get("synthesis_run_id") or "") for r in rows)
    return [r for r in rows if str(r.get("synthesis_run_id") or "") == latest]
