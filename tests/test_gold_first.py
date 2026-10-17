"""Gold/synthesis lake-first tests for book-opportunity-intelligence."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from book_opportunities import config, gold
from book_opportunities.domain_client import DomainLoadResult, fetch_all_domains
from book_opportunities.ingest import (
    project_csv,
    run_synthesis,
    write_gold_from_synthesis,
)
from book_opportunities.store import (
    load_history,
    load_records,
    seed_gold_from_synthesis,
)
from book_opportunities.synthesis import synthesize


DOMAIN_FIXTURE_ROOT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "domain-products"
)


def _lake_deps_available() -> bool:
    return gold.shared_runtime_available()


class SynthesisLineageTests(unittest.TestCase):
    def test_items_carry_synthesis_run_and_input_lineage(self):
        results = fetch_all_domains(use_fixtures=True, fixtures_dir=DOMAIN_FIXTURE_ROOT)
        out = synthesize(results, synthesis_run_id="test-run-fixed")
        self.assertEqual(out["synthesis_run_id"], "test-run-fixed")
        self.assertEqual(out["layer"], "gold")
        self.assertGreaterEqual(len(out["items"]), 1)
        for item in out["items"]:
            self.assertEqual(item["synthesis_run_id"], "test-run-fixed")
            self.assertTrue(item.get("input_source_product"))
            # Domain fixtures include record_id / event lineage.
            self.assertTrue(
                item.get("input_source_record_id") or item.get("url"),
                msg=item,
            )
            self.assertIn("input_event_id", item)
            self.assertIn("input_raw_object_key", item)
            self.assertIn("input_ingest_run_id", item)
            self.assertIn("rank_position", item)

    def test_failed_domains_preserve_input_summary(self):
        results = [
            DomainLoadResult(
                product_id="crypto",
                port=8101,
                state="unavailable",
                source="none",
                items=[],
                error_message="Local domain API unavailable",
            ),
            DomainLoadResult(
                product_id="stocks",
                port=8102,
                state="timeout",
                source="none",
                items=[],
                error_message="Local domain API timed out",
            ),
            DomainLoadResult(
                product_id="ai_tools",
                port=8107,
                state="stale",
                source="fixture",
                envelope={
                    "schema_version": "ai_tools.v1",
                    "source": "book-ai-tools-data",
                    "retrieved_at": "2026-08-01T12:00:00Z",
                    "data_status": "stale",
                    "items": [
                        {
                            "name": "Example AI Chat",
                            "url": "https://example.com/ai-chat",
                            "upvotes": "100",
                            "pricing": "free",
                            "record_id": "example-ai-chat",
                            "event_id": "evt-ai-1",
                            "raw_object_key": "landing/ai/1.json",
                            "ingest_run_id": "run-ai-1",
                        }
                    ],
                    "next_cursor": None,
                },
                items=[
                    {
                        "name": "Example AI Chat",
                        "url": "https://example.com/ai-chat",
                        "upvotes": "100",
                        "pricing": "free",
                        "record_id": "example-ai-chat",
                        "event_id": "evt-ai-1",
                        "raw_object_key": "landing/ai/1.json",
                        "ingest_run_id": "run-ai-1",
                    }
                ],
            ),
        ]
        out = synthesize(results, synthesis_run_id="partial-run")
        self.assertEqual(len(out["input_summary"]), 3)
        states = {row["product_id"]: row["state"] for row in out["input_summary"]}
        self.assertEqual(states["crypto"], "unavailable")
        self.assertEqual(states["stocks"], "timeout")
        self.assertEqual(states["ai_tools"], "stale")
        self.assertGreaterEqual(len(out["items"]), 1)
        winner = out["items"][0]
        self.assertEqual(winner["input_source_product"], "ai_tools")
        self.assertEqual(winner["input_source_record_id"], "example-ai-chat")
        self.assertEqual(winner["input_event_id"], "evt-ai-1")
        self.assertEqual(winner["input_raw_object_key"], "landing/ai/1.json")
        self.assertEqual(winner["input_ingest_run_id"], "run-ai-1")

    def test_scoring_replay_deterministic(self):
        results = fetch_all_domains(use_fixtures=True, fixtures_dir=DOMAIN_FIXTURE_ROOT)
        a = synthesize(
            results, synthesis_run_id="replay-a", scraped_at="2026-08-07 10:00:00"
        )
        b = synthesize(
            results, synthesis_run_id="replay-b", scraped_at="2026-08-07 10:00:00"
        )
        self.assertEqual(
            [
                (i["title"], i["trend_score"], i["url"], i["rank_position"])
                for i in a["items"]
            ],
            [
                (i["title"], i["trend_score"], i["url"], i["rank_position"])
                for i in b["items"]
            ],
        )


@unittest.skipUnless(
    _lake_deps_available(),
    "shared data_lake runtime not installed (pip install -e '.[lake]')",
)
class GoldWriteReadTests(unittest.TestCase):
    def test_write_and_read_gold_without_csv(self):
        results = fetch_all_domains(use_fixtures=True, fixtures_dir=DOMAIN_FIXTURE_ROOT)
        synthesized = synthesize(
            results,
            synthesis_run_id="gold-write-1",
            scraped_at="2026-08-07 12:00:00",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lake_uri = str(root / "lake")
            out = root / "data"
            out.mkdir()
            gold_result = write_gold_from_synthesis(
                synthesized,
                data_lake_uri=lake_uri,
                output_dir=out,
            )
            self.assertEqual(gold_result["synthesis_run_id"], "gold-write-1")
            self.assertTrue(
                (Path(lake_uri) / gold_result["opportunities"]["gold_key"]).is_file()
            )
            self.assertTrue(
                (Path(lake_uri) / gold_result["history"]["gold_key"]).is_file()
            )
            self.assertTrue((out / config.LINEAGE_FILE).is_file())
            # No CSV unless projected.
            self.assertFalse((out / config.RECORDS_FILE).exists())

            with (
                mock.patch.object(config, "DATA_LAKE_URI", lake_uri),
                mock.patch.object(config, "DATA_DIR", out),
            ):
                records = load_records(data_lake_uri=lake_uri)
                history = load_history(data_lake_uri=lake_uri)
            self.assertEqual(records["source_kind"], "gold_parquet")
            self.assertEqual(history["source_kind"], "gold_parquet")
            self.assertGreaterEqual(len(records["items"]), 1)
            self.assertGreaterEqual(len(history["items"]), 1)
            self.assertEqual(records["synthesis_run_id"], "gold-write-1")
            sample = records["items"][0]
            self.assertEqual(sample["synthesis_run_id"], "gold-write-1")
            self.assertTrue(sample.get("input_source_product"))
            # Scores match synthesis (replay).
            expected = {
                (i["title"], int(i["trend_score"]), i["url"])
                for i in synthesized["items"]
            }
            got = {
                (i["title"], int(i["trend_score"]), i["url"]) for i in records["items"]
            }
            self.assertEqual(expected, got)

    def test_csv_not_written_when_gold_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)

            def boom(*_a, **_k):
                raise gold.GoldWriteError("simulated gold failure")

            with (
                mock.patch(
                    "book_opportunities.ingest.write_gold_from_synthesis",
                    side_effect=boom,
                ),
                mock.patch.object(config, "DOMAIN_FIXTURES_DIR", DOMAIN_FIXTURE_ROOT),
            ):
                with self.assertRaises(gold.GoldWriteError):
                    run_synthesis(
                        use_fixtures=True,
                        dry_run=False,
                        output_dir=out,
                        project_csv_flag=True,
                    )
            self.assertFalse((out / config.RECORDS_FILE).exists())
            self.assertFalse((out / config.HISTORY_FILE).exists())

    def test_csv_written_only_after_successful_gold(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "data"
            out.mkdir()
            lake_uri = str(root / "lake")
            order: list[str] = []

            def fake_gold(synthesized, **kwargs):
                order.append("gold")
                self.assertTrue(synthesized.get("synthesis_run_id"))
                return {
                    "synthesis_run_id": synthesized["synthesis_run_id"],
                    "opportunities": {
                        "gold_key": "gold/test",
                        "manifest_key": "control/test",
                        "record_count": len(synthesized["items"]),
                    },
                    "history": {
                        "gold_key": "gold/hist",
                        "manifest_key": "control/hist",
                        "record_count": len(synthesized["items"]),
                    },
                    "manifest_path": str(out / config.LINEAGE_FILE),
                    "data_lake_uri": lake_uri,
                }

            def fake_csv(items, *, output_dir):
                order.append("csv")
                return project_csv(items, output_dir=output_dir)

            with (
                mock.patch(
                    "book_opportunities.ingest.write_gold_from_synthesis",
                    side_effect=fake_gold,
                ),
                mock.patch(
                    "book_opportunities.ingest.project_csv", side_effect=fake_csv
                ),
                mock.patch.object(config, "DOMAIN_FIXTURES_DIR", DOMAIN_FIXTURE_ROOT),
            ):
                run_synthesis(
                    use_fixtures=True,
                    dry_run=False,
                    output_dir=out,
                    data_lake_uri=lake_uri,
                    project_csv_flag=True,
                )
            self.assertEqual(order, ["gold", "csv"])
            self.assertTrue((out / config.RECORDS_FILE).exists())

    def test_api_store_ignores_csv_projection(self):
        """Even if CSV exists with different rows, store reads Gold only."""
        results = fetch_all_domains(use_fixtures=True, fixtures_dir=DOMAIN_FIXTURE_ROOT)
        synthesized = synthesize(
            results,
            synthesis_run_id="csv-ignore",
            scraped_at="2026-08-07 12:00:00",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lake_uri = str(root / "lake")
            data_dir = root / "data"
            data_dir.mkdir()
            seed_gold_from_synthesis(
                synthesized["items"],
                data_lake_uri=lake_uri,
                data_dir=data_dir,
                synthesis_run_id="csv-ignore",
                scraped_at="2026-08-07 12:00:00",
                input_summary=synthesized["input_summary"],
                data_status=synthesized["data_status"],
            )
            # Poison CSV with a fake row the Gold path must not surface.
            (data_dir / config.RECORDS_FILE).write_text(
                "title,category,source,price,url,trend_score,competition_level,notes,scraped_at,score_delta,trend_direction\n"
                "POISON ROW,tcg,domain:x,1,local://poison,99,low,n,2026-08-07 12:00:00,0,stable\n",
                encoding="utf-8",
            )
            with mock.patch.object(config, "DATA_DIR", data_dir):
                payload = load_records(data_lake_uri=lake_uri)
            titles = [i["title"] for i in payload["items"]]
            self.assertNotIn("POISON ROW", titles)
            self.assertEqual(payload["source_kind"], "gold_parquet")
            self.assertGreaterEqual(len(payload["items"]), 1)

    def test_empty_domain_set_still_writes_manifest_lineage(self):
        results = [
            DomainLoadResult(
                product_id="crypto",
                port=8101,
                state="empty",
                source="api",
                items=[],
                error_message="Domain product returned empty data",
            )
        ]
        synthesized = synthesize(results, synthesis_run_id="empty-run")
        self.assertEqual(synthesized["data_status"], "empty")
        self.assertEqual(synthesized["items"], [])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lake_uri = str(root / "lake")
            out = root / "data"
            out.mkdir()
            gold_result = write_gold_from_synthesis(
                synthesized,
                data_lake_uri=lake_uri,
                output_dir=out,
            )
            self.assertEqual(gold_result["opportunities"]["record_count"], 0)
            lineage = json.loads(
                (out / config.LINEAGE_FILE).read_text(encoding="utf-8")
            )
            self.assertEqual(lineage["synthesis_run_id"], "empty-run")
            self.assertEqual(lineage["data_status"], "empty")
            self.assertEqual(len(lineage["input_summary"]), 1)
            self.assertEqual(lineage["input_summary"][0]["state"], "empty")


class NoExternalSideEffectsTests(unittest.TestCase):
    def test_runtime_modules_have_no_external_write_endpoints(self):
        src_root = Path(__file__).resolve().parents[1] / "src" / "book_opportunities"
        for name in (
            "ingest.py",
            "domain_client.py",
            "synthesis.py",
            "gold.py",
            "store.py",
            "api.py",
        ):
            text = (src_root / name).read_text(encoding="utf-8")
            self.assertNotIn("api.firecrawl.dev", text)
            self.assertNotIn("api.telegram.org", text)
            self.assertNotIn("api.todoist.com", text)
            self.assertNotIn("FIRECRAWL_API_KEY", text)


if __name__ == "__main__":
    src = Path(__file__).resolve().parents[1] / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    unittest.main()
