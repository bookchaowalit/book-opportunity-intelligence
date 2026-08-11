"""Gold-backed data store for opportunity intelligence.

HTTP API reads Gold Parquet via DuckDB only. CSV is a CLI projection helper
and is never consulted by load_records / load_history / get_record.
"""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

from . import config
from . import gold


TIMESTAMP_KEYS = (
    "updated_at",
    "scraped_at",
    "date",
    "timestamp",
    "retrieved_at",
    "event_time",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_ts(value: str) -> Optional[datetime]:
    if not value:
        return None
    value = value.strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def make_record_id(row: dict[str, Any]) -> str:
    parts = []
    for field in config.ID_FIELDS:
        raw = str(row.get(field, "")).strip()
        if not raw and field == "url":
            raw = str(row.get("name", "") or row.get("title", "")).strip()
        parts.append(raw)
    joined = config.ID_SEP.join(parts)
    if not joined or joined == config.ID_SEP * (len(parts) - 1):
        blob = json.dumps(row, sort_keys=True, default=str)
        return "hash:" + hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]
    # URLs and other path-hostile IDs become stable hashes so /v1/records/{id} works
    if any(ch in joined for ch in ("://", "?", "#", " ")) or joined.count("/") > 0:
        return "hash:" + hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]
    return joined.replace("/", "_")


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _as_int_score(value: Any) -> int:
    try:
        if value is None or value == "":
            return 0
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def gold_opportunity_item(
    row: dict[str, Any],
    *,
    history: bool = False,
    history_idx: int = 0,
) -> dict[str, Any]:
    """Project one Gold Parquet row into opportunity.v1 API item shape."""
    trend_score = _as_int_score(row.get("trend_score"))
    item: dict[str, Any] = {
        "title": _stringify(row.get("title")),
        "category": _stringify(row.get("category")),
        "source": _stringify(row.get("source")),
        "price": _stringify(row.get("price")),
        "url": _stringify(row.get("url")),
        "trend_score": trend_score,
        "competition_level": _stringify(row.get("competition_level")),
        "notes": _stringify(row.get("notes")),
        "scraped_at": _stringify(row.get("scraped_at") or row.get("event_time")),
        "event_time": _stringify(row.get("event_time") or row.get("scraped_at")),
        "score_delta": _stringify(row.get("score_delta", 0)),
        "trend_direction": _stringify(row.get("trend_direction") or "stable"),
        "synthesis_run_id": _stringify(row.get("synthesis_run_id")),
        "rank_position": _as_int_score(row.get("rank_position")),
        "input_source_product": _stringify(row.get("input_source_product")),
        "input_source_record_id": _stringify(row.get("input_source_record_id")),
        "input_event_id": _stringify(row.get("input_event_id")),
        "input_raw_object_key": _stringify(row.get("input_raw_object_key")),
        "input_ingest_run_id": _stringify(row.get("input_ingest_run_id")),
        "input_domain_status": _stringify(row.get("input_domain_status")),
        "input_domain_source": _stringify(row.get("input_domain_source")),
        "layer": _stringify(row.get("layer") or config.LAKE_LAYER),
        "schema_version": _stringify(row.get("schema_version") or config.LAKE_GOLD_SCHEMA_VERSION),
    }
    if history:
        item["record_id"] = make_record_id(item) + f"#h{history_idx}"
    else:
        item["record_id"] = make_record_id(item)
    return item


def _lake_uri_for_reads() -> Optional[str]:
    if config.DATA_LAKE_URI.strip():
        return config.DATA_LAKE_URI.strip()
    try:
        return gold.default_data_lake_uri()
    except Exception:  # noqa: BLE001
        return None


def _freshness(rows: list[dict[str, Any]]) -> tuple[str, Optional[str]]:
    if not rows:
        return "empty", None
    latest: Optional[datetime] = None
    latest_raw = None
    for row in rows:
        for key in TIMESTAMP_KEYS:
            if key in row and row[key]:
                parsed = _parse_ts(str(row[key]))
                if parsed and (latest is None or parsed > latest):
                    latest = parsed
                    latest_raw = str(row[key])
                break
    if latest is None:
        return "ok", latest_raw
    age_hours = (datetime.now(timezone.utc) - latest).total_seconds() / 3600.0
    if age_hours > config.STALE_AFTER_HOURS:
        return "stale", latest_raw
    return "ok", latest_raw


def load_records(
    data_dir: Optional[Path] = None,
    *,
    data_lake_uri: Optional[str] = None,
) -> dict[str, Any]:
    """Load latest synthesis snapshot from Gold Parquet (not CSV)."""
    del data_dir  # API/store path is gold-only; kept for call-site compatibility.
    uri = data_lake_uri or _lake_uri_for_reads()
    gold_path = str(
        gold.gold_dataset_dir(config.LAKE_DATASET_OPPORTUNITIES, data_lake_uri=uri)
        if uri
        else Path("gold/unavailable")
    )
    try:
        rows = gold.read_gold_rows(
            config.LAKE_DATASET_OPPORTUNITIES,
            data_lake_uri=uri,
            latest_run_only=True,
        )
        items = [gold_opportunity_item(row, history=False) for row in rows]
        # Stable rank order for API consumers.
        items.sort(
            key=lambda i: (
                -int(i.get("trend_score") or 0),
                int(i.get("rank_position") or 0) or 10**9,
                str(i.get("title") or ""),
                str(i.get("url") or ""),
            )
        )
        status, _ = _freshness(items)
        lineage = gold.load_lineage()
        return {
            "items": items,
            "data_status": status if items else "empty",
            "error": None,
            "path": gold_path,
            "source_kind": "gold_parquet",
            "synthesis_run_id": (
                items[0].get("synthesis_run_id")
                if items
                else (lineage or {}).get("synthesis_run_id")
            ),
            "lineage": lineage,
            "retrieved_at": utc_now_iso(),
        }
    except gold.GoldUnavailable as exc:
        return {
            "items": [],
            "data_status": "error",
            "error": str(exc),
            "path": gold_path,
            "source_kind": "gold_parquet",
            "synthesis_run_id": None,
            "lineage": gold.load_lineage(),
            "retrieved_at": utc_now_iso(),
        }
    except gold.GoldWriteError as exc:
        return {
            "items": [],
            "data_status": "malformed",
            "error": str(exc),
            "path": gold_path,
            "source_kind": "gold_parquet",
            "synthesis_run_id": None,
            "lineage": gold.load_lineage(),
            "retrieved_at": utc_now_iso(),
        }


def load_history(
    data_dir: Optional[Path] = None,
    *,
    data_lake_uri: Optional[str] = None,
) -> dict[str, Any]:
    """Load full synthesis history from Gold Parquet parts (not CSV)."""
    del data_dir
    uri = data_lake_uri or _lake_uri_for_reads()
    gold_path = str(
        gold.gold_dataset_dir(config.LAKE_DATASET_HISTORY, data_lake_uri=uri)
        if uri
        else Path("gold/unavailable")
    )
    try:
        rows = gold.read_gold_rows(
            config.LAKE_DATASET_HISTORY,
            data_lake_uri=uri,
            latest_run_only=False,
        )
        items = [
            gold_opportunity_item(row, history=True, history_idx=idx)
            for idx, row in enumerate(rows)
        ]
        status, _ = _freshness(items)
        return {
            "items": items,
            "data_status": status if items else "empty",
            "error": None,
            "path": gold_path,
            "source_kind": "gold_parquet",
            "retrieved_at": utc_now_iso(),
        }
    except gold.GoldUnavailable as exc:
        return {
            "items": [],
            "data_status": "error",
            "error": str(exc),
            "path": gold_path,
            "source_kind": "gold_parquet",
            "retrieved_at": utc_now_iso(),
        }
    except gold.GoldWriteError as exc:
        return {
            "items": [],
            "data_status": "malformed",
            "error": str(exc),
            "path": gold_path,
            "source_kind": "gold_parquet",
            "retrieved_at": utc_now_iso(),
        }


def get_record(
    record_id: str,
    data_dir: Optional[Path] = None,
    *,
    data_lake_uri: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    record_id = unquote(record_id)
    payload = load_records(data_dir, data_lake_uri=data_lake_uri)
    for item in payload["items"]:
        if item.get("record_id") == record_id:
            return item
    return None


def paginate(
    items: list[dict[str, Any]],
    *,
    limit: int = 50,
    cursor: Optional[str] = None,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    limit = max(1, min(int(limit), 500))
    start = 0
    if cursor:
        try:
            start = max(0, int(cursor))
        except ValueError:
            start = 0
    end = start + limit
    page = items[start:end]
    next_cursor = str(end) if end < len(items) else None
    return page, next_cursor


def envelope(
    *,
    items: list[dict[str, Any]],
    data_status: str,
    next_cursor: Optional[str] = None,
    retrieved_at: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    body = {
        "schema_version": config.SCHEMA_VERSION,
        "source": config.REPO_NAME,
        "retrieved_at": retrieved_at or utc_now_iso(),
        "data_status": data_status,
        "items": items,
        "next_cursor": next_cursor,
    }
    if extra:
        body.update(extra)
    return body


def _read_csv(path: Path) -> tuple[list[dict[str, str]], Optional[str]]:
    """CLI-only helper for optional CSV projections."""
    if not path.exists():
        return [], None
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return [], None
        reader = csv.DictReader(text.splitlines())
        rows = [dict(row) for row in reader]
        return rows, None
    except Exception as exc:  # noqa: BLE001
        return [], f"malformed: {exc}"


def load_csv_projection(
    *,
    kind: str = "records",
    data_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Read optional local CSV projection (CLI UX only; not used by HTTP API)."""
    data_dir = Path(data_dir or config.DATA_DIR)
    if kind == "history":
        path = data_dir / config.HISTORY_FILE
    else:
        path = data_dir / config.RECORDS_FILE
    rows, err = _read_csv(path)
    if err:
        return {
            "items": [],
            "data_status": "malformed",
            "error": err,
            "path": str(path),
            "source_kind": "csv_projection",
            "retrieved_at": utc_now_iso(),
        }
    items = []
    for idx, row in enumerate(rows):
        item = dict(row)
        item["record_id"] = make_record_id(row) + (f"#h{idx}" if kind == "history" else "")
        items.append(item)
    status, _ = _freshness(items)
    return {
        "items": items,
        "data_status": status if items else "empty",
        "error": None,
        "path": str(path),
        "source_kind": "csv_projection",
        "retrieved_at": utc_now_iso(),
    }


def seed_fixtures(data_dir: Optional[Path] = None) -> Path:
    """Copy package fixtures into the local data directory (CLI dry-run helper).

    Note: HTTP API does not read these CSVs — use seed_gold_from_fixtures for lake tests.
    """
    data_dir = Path(data_dir or config.DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    src_records = config.FIXTURES_DIR / config.RECORDS_FILE
    src_history = config.FIXTURES_DIR / config.HISTORY_FILE
    if src_records.exists():
        (data_dir / config.RECORDS_FILE).write_text(
            src_records.read_text(encoding="utf-8"), encoding="utf-8"
        )
    if src_history.exists():
        (data_dir / config.HISTORY_FILE).write_text(
            src_history.read_text(encoding="utf-8"), encoding="utf-8"
        )
    return data_dir


def seed_gold_from_synthesis(
    items: list[dict[str, Any]],
    *,
    data_lake_uri: str,
    data_dir: Optional[Path] = None,
    synthesis_run_id: Optional[str] = None,
    scraped_at: Optional[str] = None,
    input_summary: Optional[list[dict[str, Any]]] = None,
    data_status: str = "ok",
) -> dict[str, Any]:
    """Test/helper: write Gold snapshot + history from already-synthesized items."""
    run_id = synthesis_run_id or gold.make_synthesis_run_id(seed="seed")
    stamp = scraped_at or gold.utc_now_iso().replace("T", " ").replace("Z", "")
    # Normalize scraped_at format used by synthesis (YYYY-MM-DD HH:MM:SS)
    if "T" in stamp:
        stamp = stamp.replace("T", " ").replace("Z", "")[:19]
    gold_ops = gold.write_gold_dataset(
        items,
        dataset=config.LAKE_DATASET_OPPORTUNITIES,
        synthesis_run_id=run_id,
        scraped_at=stamp,
        data_lake_uri=data_lake_uri,
        input_summary=input_summary or [],
        data_status=data_status,
    )
    gold_hist = gold.write_gold_dataset(
        items,
        dataset=config.LAKE_DATASET_HISTORY,
        synthesis_run_id=run_id,
        scraped_at=stamp,
        data_lake_uri=data_lake_uri,
        input_summary=input_summary or [],
        data_status=data_status,
    )
    if data_dir is not None:
        gold.write_local_synthesis_manifest(
            data_dir=Path(data_dir),
            synthesis_run_id=run_id,
            gold_result=gold_ops,
            input_summary=input_summary or [],
            data_status=data_status,
            item_count=len(items),
        )
    return {
        "synthesis_run_id": run_id,
        "opportunities": gold_ops,
        "history": gold_hist,
    }
