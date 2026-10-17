"""Offline tests for synthesis consumer + free-only API contract."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from book_opportunities import config, gold, policy
from book_opportunities.api import DataProductHandler
from book_opportunities.domain_client import (
    fetch_all_domains,
    fetch_domain_records,
    is_data_product_envelope,
    sanitize_user_message,
)
from book_opportunities.http_client import UpstreamError, request_json
from book_opportunities.ingest import main as ingest_main, run_synthesis
from book_opportunities.store import (
    get_record,
    load_csv_projection,
    load_history,
    load_records,
    seed_gold_from_synthesis,
)
from book_opportunities.domain_client import DomainLoadResult
from book_opportunities.synthesis import deduplicate, synthesize


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures"
DOMAIN_FIXTURE_ROOT = FIXTURE_ROOT / "domain-products"
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "book_opportunities"


def _set_data_dir(path: Path):
    return mock.patch.object(config, "DATA_DIR", path)


def _set_lake_uri(uri: str):
    return mock.patch.object(config, "DATA_LAKE_URI", uri)


def _lake_deps_available() -> bool:
    return gold.shared_runtime_available()


def _seed_gold_from_domain_fixtures(
    *,
    data_lake_uri: str,
    data_dir: Path,
    synthesis_run_id: str = "test-api-run",
    scraped_at: str = "2026-08-01 12:00:00",
) -> dict:
    results = fetch_all_domains(use_fixtures=True, fixtures_dir=DOMAIN_FIXTURE_ROOT)
    synthesized = synthesize(
        results,
        synthesis_run_id=synthesis_run_id,
        scraped_at=scraped_at,
    )
    seed_gold_from_synthesis(
        synthesized["items"],
        data_lake_uri=data_lake_uri,
        data_dir=data_dir,
        synthesis_run_id=synthesis_run_id,
        scraped_at=scraped_at,
        input_summary=synthesized["input_summary"],
        data_status=synthesized["data_status"],
    )
    return synthesized


@unittest.skipUnless(
    _lake_deps_available(),
    "shared data_lake runtime not installed (pip install -e '.[lake]')",
)
class StoreTests(unittest.TestCase):
    def test_gold_records_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir()
            lake_uri = str(root / "lake")
            _seed_gold_from_domain_fixtures(data_lake_uri=lake_uri, data_dir=data_dir)
            with _set_data_dir(data_dir), _set_lake_uri(lake_uri):
                payload = load_records(data_dir, data_lake_uri=lake_uri)
            self.assertEqual(payload["source_kind"], "gold_parquet")
            self.assertEqual(payload["data_status"], "stale")
            self.assertGreaterEqual(len(payload["items"]), 1)
            self.assertIn("record_id", payload["items"][0])
            self.assertTrue(payload["items"][0].get("synthesis_run_id"))
            self.assertTrue(payload["items"][0].get("input_source_product"))

    def test_empty_gold(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lake_uri = str(root / "lake")
            Path(lake_uri).mkdir()
            payload = load_records(data_lake_uri=lake_uri)
            self.assertEqual(payload["data_status"], "empty")
            self.assertEqual(payload["items"], [])
            self.assertEqual(payload["source_kind"], "gold_parquet")

    def test_csv_poison_ignored(self):
        """API store must not read money_opportunities.csv."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir()
            lake_uri = str(root / "lake")
            Path(lake_uri).mkdir()
            # CSV exists but Gold is empty — store must report empty gold, not CSV.
            (data_dir / config.RECORDS_FILE).write_text(
                "title,category,source,price,url,trend_score,competition_level,notes,scraped_at,score_delta,trend_direction\n"
                "CSV ONLY,tcg,domain:x,1,local://csv,50,low,n,2026-08-01 12:00:00,0,stable\n",
                encoding="utf-8",
            )
            with _set_data_dir(data_dir), _set_lake_uri(lake_uri):
                payload = load_records(data_dir, data_lake_uri=lake_uri)
            self.assertEqual(payload["source_kind"], "gold_parquet")
            self.assertEqual(payload["items"], [])
            # CLI helper still sees CSV.
            csv_payload = load_csv_projection(kind="records", data_dir=data_dir)
            self.assertEqual(csv_payload["source_kind"], "csv_projection")
            self.assertEqual(len(csv_payload["items"]), 1)

    def test_invalid_record_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir()
            lake_uri = str(root / "lake")
            _seed_gold_from_domain_fixtures(data_lake_uri=lake_uri, data_dir=data_dir)
            with _set_data_dir(data_dir), _set_lake_uri(lake_uri):
                self.assertIsNone(
                    get_record(
                        "this-id-does-not-exist", data_dir, data_lake_uri=lake_uri
                    )
                )

    def test_history_load_from_gold(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir()
            lake_uri = str(root / "lake")
            _seed_gold_from_domain_fixtures(data_lake_uri=lake_uri, data_dir=data_dir)
            with _set_data_dir(data_dir), _set_lake_uri(lake_uri):
                hist = load_history(data_dir, data_lake_uri=lake_uri)
            self.assertEqual(hist["source_kind"], "gold_parquet")
            self.assertGreaterEqual(len(hist["items"]), 1)


class PolicyTests(unittest.TestCase):
    def test_free_only_blocks_paid_and_writes(self):
        with (
            mock.patch.object(config, "FREE_ONLY", True),
            mock.patch.object(config, "ALLOW_PAID_PROVIDERS", False),
        ):
            for name in ("firecrawl", "telegram_write", "todoist_write", "paid_search"):
                decision = policy.evaluate_provider(name)
                self.assertFalse(decision.allowed, name)
                self.assertEqual(decision.status, "blocked")

    def test_domain_apis_allowed(self):
        with (
            mock.patch.object(config, "FREE_ONLY", True),
            mock.patch.object(config, "ALLOW_PAID_PROVIDERS", False),
        ):
            for product in config.DOMAIN_PRODUCTS:
                name = policy.domain_provider_name(product["id"])
                decision = policy.evaluate_provider(name)
                self.assertTrue(decision.allowed, name)
                self.assertEqual(decision.status, "free")

    def test_legacy_scrapers_blocked(self):
        for name in ("hacker_news_public", "github_public", "ddg_html"):
            decision = policy.evaluate_provider(name)
            self.assertFalse(decision.allowed, name)

    def test_missing_credentials(self):
        with mock.patch.dict(os.environ, {"FIRECRAWL_API_KEY": ""}, clear=False):
            status = policy.missing_credential_status("FIRECRAWL_API_KEY")
            self.assertEqual(status["status"], "missing")
            self.assertFalse(status["present"])

    def test_external_writes_blocked(self):
        with (
            mock.patch.object(config, "FREE_ONLY", True),
            mock.patch.object(config, "ALLOW_EXTERNAL_WRITES", False),
        ):
            self.assertFalse(policy.external_writes_allowed())
            with self.assertRaises(PermissionError):
                policy.require_external_writes("telegram")


class HttpClientTests(unittest.TestCase):
    def test_blocks_paid_provider_before_request(self):
        with (
            mock.patch.object(config, "FREE_ONLY", True),
            mock.patch.object(config, "ALLOW_PAID_PROVIDERS", False),
        ):
            with self.assertRaises(PermissionError):
                request_json(
                    "GET", "http://127.0.0.1:8101/v1/records", provider="firecrawl"
                )

    def test_blocks_non_loopback_host(self):
        with self.assertRaises(UpstreamError) as ctx:
            request_json(
                "GET",
                "https://api.firecrawl.dev/v1/search",
                provider="domain_api_crypto",
            )
        self.assertEqual(ctx.exception.kind, "host_forbidden")

    def test_blocks_non_get(self):
        with self.assertRaises(UpstreamError) as ctx:
            request_json(
                "POST",
                "http://127.0.0.1:8101/v1/records",
                provider="domain_api_crypto",
            )
        self.assertEqual(ctx.exception.kind, "method_forbidden")

    def test_timeout(self):
        class Boom:
            def request(self, *args, **kwargs):
                raise TimeoutError("timed out")

        fake_httpx = mock.Mock()
        fake_httpx.request = Boom().request
        with (
            mock.patch("book_opportunities.http_client.httpx", fake_httpx),
            mock.patch.object(config, "MAX_RETRIES", 0),
        ):
            with self.assertRaises(UpstreamError) as ctx:
                request_json(
                    "GET",
                    "http://127.0.0.1:8101/v1/records",
                    provider="domain_api_crypto",
                    sleep=lambda _: None,
                )
            self.assertEqual(ctx.exception.kind, "timeout")

    def test_http_429(self):
        resp = mock.Mock()
        resp.status_code = 429
        resp.content = b"rate limited"
        fake_httpx = mock.Mock()
        fake_httpx.request.return_value = resp
        with (
            mock.patch("book_opportunities.http_client.httpx", fake_httpx),
            mock.patch.object(config, "MAX_RETRIES", 0),
        ):
            with self.assertRaises(UpstreamError) as ctx:
                request_json(
                    "GET",
                    "http://127.0.0.1:8101/v1/records",
                    provider="domain_api_crypto",
                    sleep=lambda _: None,
                )
            self.assertEqual(ctx.exception.status_code, 429)
            self.assertEqual(ctx.exception.kind, "rate_limit")


class DomainClientTests(unittest.TestCase):
    def test_fixture_mode_no_network(self):
        calls = {"n": 0}

        def boom(_url):
            calls["n"] += 1
            raise RuntimeError("network forbidden")

        results = fetch_all_domains(
            use_fixtures=True,
            fixtures_dir=DOMAIN_FIXTURE_ROOT,
            fetch_impl=boom,
        )
        self.assertEqual(calls["n"], 0)
        self.assertEqual(len(results), 7)
        self.assertTrue(all(r.source == "fixture" for r in results))
        self.assertTrue(
            all(r.envelope and is_data_product_envelope(r.envelope) for r in results)
        )
        ports = sorted(r.port for r in results)
        self.assertEqual(ports, [8101, 8102, 8103, 8104, 8105, 8106, 8107])

    def test_only_loopback_ports_8101_8107(self):
        seen_urls: list[str] = []

        def capture(url: str):
            seen_urls.append(url)
            # Return empty envelope so fixture fallback is not required for path check
            product_id = None
            for p in config.DOMAIN_PRODUCTS:
                if f":{p['port']}" in url:
                    product_id = p["id"]
                    break
            assert product_id
            fixture = json.loads(
                (DOMAIN_FIXTURE_ROOT / f"{product_id}.json").read_text(encoding="utf-8")
            )
            return fixture

        results = fetch_all_domains(use_fixtures=False, fetch_impl=capture)
        self.assertEqual(len(seen_urls), 7)
        for url in seen_urls:
            self.assertTrue(url.startswith("http://127.0.0.1:810"))
            self.assertIn("/v1/records?", url)
            self.assertNotIn("/v1/refresh", url)
            self.assertNotIn("firecrawl", url.lower())
            self.assertNotIn("telegram", url.lower())
            self.assertNotIn("todoist", url.lower())
        self.assertTrue(all(r.source == "api" for r in results))

    def test_timeout_falls_back_to_fixture_with_safe_message(self):
        with mock.patch(
            "book_opportunities.domain_client.request_json",
            side_effect=UpstreamError("Upstream request timed out", kind="timeout"),
        ):
            result = fetch_domain_records(
                "crypto",
                use_fixtures=False,
                fixtures_dir=DOMAIN_FIXTURE_ROOT,
            )
        self.assertEqual(result.source, "fixture")
        self.assertEqual(result.state, "timeout")
        self.assertEqual(
            result.error_message,
            "Local domain API timed out; using fixture fallback",
        )
        self.assertNotIn("secret", result.error_message.lower())

    def test_unavailable_falls_back_safely(self):
        with mock.patch(
            "book_opportunities.domain_client.request_json",
            side_effect=UpstreamError("Upstream request failed", kind="network"),
        ):
            result = fetch_domain_records(
                "stocks",
                use_fixtures=False,
                fixtures_dir=DOMAIN_FIXTURE_ROOT,
            )
        self.assertEqual(result.source, "fixture")
        self.assertIn("unavailable", result.error_message)
        self.assertNotIn("ECONNREFUSED", result.error_message)

    def test_empty_domain_state(self):
        empty = {
            "schema_version": "fx.v1",
            "source": "book-fx-data",
            "retrieved_at": "2026-08-04T00:00:00Z",
            "data_status": "empty",
            "items": [],
            "next_cursor": None,
        }
        result = fetch_domain_records(
            "fx",
            use_fixtures=False,
            fetch_impl=lambda _url: empty,
        )
        self.assertEqual(result.state, "empty")
        self.assertEqual(result.items, [])

    def test_stale_domain_state(self):
        fixture = json.loads(
            (DOMAIN_FIXTURE_ROOT / "defi.json").read_text(encoding="utf-8")
        )
        fixture["data_status"] = "stale"
        result = fetch_domain_records(
            "defi",
            use_fixtures=False,
            fetch_impl=lambda _url: fixture,
        )
        self.assertEqual(result.state, "stale")

    def test_sanitize_messages(self):
        self.assertEqual(
            sanitize_user_message("timeout", using_fixture=True),
            "Local domain API timed out; using fixture fallback",
        )
        self.assertNotIn("api_key", sanitize_user_message("unavailable"))


class SynthesisTests(unittest.TestCase):
    def _fixture_results(self) -> list[DomainLoadResult]:
        return fetch_all_domains(use_fixtures=True, fixtures_dir=DOMAIN_FIXTURE_ROOT)

    def test_opportunity_v1_contract(self):
        result = synthesize(self._fixture_results())
        for key in (
            "schema_version",
            "source",
            "retrieved_at",
            "data_status",
            "items",
            "next_cursor",
        ):
            self.assertIn(key, result)
        self.assertEqual(result["schema_version"], "opportunity.v1")
        self.assertEqual(result["source"], "book-opportunity-intelligence")
        self.assertIsInstance(result["items"], list)
        self.assertGreaterEqual(len(result["items"]), 1)
        for item in result["items"]:
            for field in (
                "title",
                "category",
                "source",
                "price",
                "url",
                "trend_score",
                "competition_level",
                "notes",
                "scraped_at",
            ):
                self.assertIn(field, item)
            self.assertIsInstance(item["trend_score"], int)
            self.assertTrue(str(item["source"]).startswith("domain:"))

    def test_ranking_deterministic(self):
        a = synthesize(self._fixture_results())
        b = synthesize(self._fixture_results())
        self.assertEqual(
            [(i["title"], i["trend_score"], i["url"]) for i in a["items"]],
            [(i["title"], i["trend_score"], i["url"]) for i in b["items"]],
        )
        scores = [int(i["trend_score"]) for i in a["items"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_deduplication(self):
        rows = [
            {
                "title": "Crypto signal: bitcoin (+1.00% 24h)",
                "url": "local://crypto/btc",
                "trend_score": 60,
                "source": "domain:crypto",
                "notes": "a",
                "category": "trending-products",
                "competition_level": "medium",
            },
            {
                "title": "Crypto signal: bitcoin (+1.00% 24h)",
                "url": "local://crypto/btc",
                "trend_score": 70,
                "source": "domain:crypto",
                "notes": "b",
                "category": "trending-products",
                "competition_level": "medium",
            },
        ]
        merged = deduplicate(rows)
        self.assertEqual(len(merged), 1)
        self.assertGreaterEqual(int(merged[0]["trend_score"]), 70)

    def test_stale_empty_timeout_inputs_safe(self):
        results = [
            DomainLoadResult(
                product_id="crypto",
                port=8101,
                state="timeout",
                source="none",
                items=[],
                error_message="Local domain API timed out",
            ),
            DomainLoadResult(
                product_id="stocks",
                port=8102,
                state="empty",
                source="api",
                envelope={
                    "schema_version": "stock.v1",
                    "source": "book-stock-data",
                    "retrieved_at": "2026-08-04T00:00:00Z",
                    "data_status": "empty",
                    "items": [],
                    "next_cursor": None,
                },
                items=[],
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
                    }
                ],
            ),
        ]
        out = synthesize(results)
        self.assertEqual(out["schema_version"], "opportunity.v1")
        self.assertGreaterEqual(len(out["items"]), 1)
        self.assertEqual(out["data_status"], "stale")
        # No raw secret-looking fields
        blob = json.dumps(out)
        self.assertNotRegex(
            blob, r"(?i)api[_-]?key|bearer\s|FIRECRAWL|TEQUILA|password="
        )


@unittest.skipUnless(
    _lake_deps_available(),
    "shared data_lake runtime not installed (pip install -e '.[lake]')",
)
class IngestCliTests(unittest.TestCase):
    def test_fixture_mode_writes_gold_and_csv_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "data"
            out.mkdir()
            lake_uri = str(root / "lake")
            calls = {"n": 0}

            def boom(*_a, **_k):
                calls["n"] += 1
                raise RuntimeError("network forbidden")

            with (
                mock.patch(
                    "book_opportunities.domain_client.request_json", side_effect=boom
                ),
                mock.patch.object(config, "DOMAIN_FIXTURES_DIR", DOMAIN_FIXTURE_ROOT),
            ):
                result = run_synthesis(
                    use_fixtures=True,
                    dry_run=False,
                    output_dir=out,
                    data_lake_uri=lake_uri,
                    project_csv_flag=True,
                )
            self.assertEqual(calls["n"], 0)
            self.assertTrue((out / config.RECORDS_FILE).exists())
            self.assertTrue((out / config.LINEAGE_FILE).exists())
            self.assertTrue(result.get("gold"))
            self.assertTrue(
                (Path(lake_uri) / result["gold"]["opportunities"]["gold_key"]).is_file()
            )
            self.assertGreaterEqual(len(result["items"]), 1)
            self.assertEqual(result["schema_version"], "opportunity.v1")
            self.assertTrue(result.get("synthesis_run_id"))

    def test_dry_run_no_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with mock.patch.object(config, "DOMAIN_FIXTURES_DIR", DOMAIN_FIXTURE_ROOT):
                result = run_synthesis(
                    use_fixtures=True,
                    dry_run=True,
                    output_dir=out,
                )
            self.assertFalse((out / config.RECORDS_FILE).exists())
            self.assertFalse((out / config.LINEAGE_FILE).exists())
            self.assertGreaterEqual(len(result["items"]), 1)

    def test_cli_help(self):
        with self.assertRaises(SystemExit) as ctx:
            ingest_main(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_cli_fixture_and_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "data"
            out.mkdir()
            lake_uri = str(root / "lake")
            dry = root / "dry"
            with mock.patch.object(config, "DOMAIN_FIXTURES_DIR", DOMAIN_FIXTURE_ROOT):
                code = ingest_main(
                    [
                        "--fixture",
                        "--output-dir",
                        str(out),
                        "--data-lake-uri",
                        lake_uri,
                    ]
                )
            self.assertEqual(code, 0)
            self.assertTrue((out / config.RECORDS_FILE).exists())
            self.assertTrue((out / config.LINEAGE_FILE).exists())
            with mock.patch.object(config, "DOMAIN_FIXTURES_DIR", DOMAIN_FIXTURE_ROOT):
                code = ingest_main(["--dry-run", "--output-dir", str(dry)])
            self.assertEqual(code, 0)
            self.assertFalse((dry / config.RECORDS_FILE).exists())

    def test_blocks_external_write_flags(self):
        code = ingest_main(["--send-digest"])
        self.assertEqual(code, 2)
        code = ingest_main(["--push-todoist"])
        self.assertEqual(code, 2)
        code = ingest_main(["--legacy-scrape"])
        self.assertEqual(code, 2)

    def test_no_firecrawl_telegram_todoist_in_runtime_modules(self):
        for name in (
            "ingest.py",
            "domain_client.py",
            "synthesis.py",
            "http_client.py",
            "gold.py",
            "store.py",
        ):
            src = (SRC_ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("api.firecrawl.dev", src)
            self.assertNotIn("api.telegram.org", src)
            self.assertNotIn("api.todoist.com", src)
            self.assertNotIn("FIRECRAWL_API_KEY", src)
            self.assertNotIn("TODOIST_API_TOKEN", src)
            self.assertNotIn("TELEGRAM_TRADING_BOT_TOKEN", src)


@unittest.skipUnless(
    _lake_deps_available(),
    "shared data_lake runtime not installed (pip install -e '.[lake]')",
)
class ApiContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data_dir = root / "data"
        self.data_dir.mkdir()
        self.lake_uri = str(root / "lake")
        # Gold only — no CSV files for the API surface.
        _seed_gold_from_domain_fixtures(
            data_lake_uri=self.lake_uri, data_dir=self.data_dir
        )
        self.assertFalse((self.data_dir / config.RECORDS_FILE).exists())
        self.data_patch = _set_data_dir(self.data_dir)
        self.lake_patch = _set_lake_uri(self.lake_uri)
        self.data_patch.start()
        self.lake_patch.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), DataProductHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.lake_patch.stop()
        self.data_patch.stop()
        self.tmp.cleanup()

    def _get(self, path: str):
        try:
            with urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def _post(self, path: str, headers=None):
        req = Request(f"http://127.0.0.1:{self.port}{path}", data=b"{}", method="POST")
        req.add_header("Content-Type", "application/json")
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_healthz(self):
        status, body = self._get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["repository"], config.REPO_NAME)
        self.assertIn(body["data_status"], {"ok", "empty", "stale", "malformed"})

    def test_metadata_envelope(self):
        status, body = self._get("/v1/metadata")
        self.assertEqual(status, 200)
        for key in (
            "schema_version",
            "source",
            "retrieved_at",
            "data_status",
            "items",
            "next_cursor",
        ):
            self.assertIn(key, body)
        self.assertEqual(body["schema_version"], config.SCHEMA_VERSION)
        self.assertEqual(body["source"], config.REPO_NAME)
        self.assertTrue(body["items"])
        meta = body["items"][0]
        self.assertEqual(meta["role"], "synthesis_consumer")
        self.assertEqual(
            meta["consumes_ports"], [8101, 8102, 8103, 8104, 8105, 8106, 8107]
        )
        self.assertIn("providers", meta)
        self.assertEqual(meta["storage_model"], "lake_first_gold_duckdb")
        self.assertEqual(meta["records_source_kind"], "gold_parquet")
        self.assertEqual(meta["history_source_kind"], "gold_parquet")
        self.assertEqual(meta["layer"], "gold")
        self.assertTrue(meta.get("no_upstream_scrape"))

    def test_records_and_history(self):
        status, body = self._get("/v1/records?limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["items"]), 1)
        self.assertTrue(body["items"][0]["record_id"])
        self.assertTrue(body["items"][0].get("synthesis_run_id"))
        self.assertTrue(body["items"][0].get("input_source_product"))
        status, hist = self._get("/v1/history?limit=1")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(hist["items"]), 1)

    def test_record_by_id(self):
        _, body = self._get("/v1/records?limit=1")
        rid = body["items"][0]["record_id"]
        from urllib.parse import quote

        status, one = self._get(f"/v1/records/{quote(rid, safe='')}")
        self.assertEqual(status, 200)
        self.assertEqual(one["items"][0]["record_id"], rid)

    def test_invalid_record_id_404(self):
        status, body = self._get("/v1/records/not-a-real-record-id")
        self.assertEqual(status, 404)
        self.assertEqual(body["data_status"], "not_found")

    def test_refresh_forbidden_by_default(self):
        with mock.patch.object(config, "ALLOW_REFRESH", False):
            status, body = self._post("/v1/refresh")
            self.assertEqual(status, 403)
            self.assertEqual(body["data_status"], "forbidden")

    def test_get_never_needs_network(self):
        status, body = self._get("/v1/records")
        self.assertEqual(status, 200)
        self.assertIn("items", body)

    def test_get_only_contract_no_secret_leak(self):
        _, body = self._get("/v1/records")
        blob = json.dumps(body)
        self.assertNotRegex(blob, r"(?i)api[_-]?key|bearer\s|FIRECRAWL|password=")

    def test_cors_allows_only_local_origins(self):
        from urllib.request import Request, urlopen

        def get_with_origin(origin: str):
            req = Request(
                f"http://127.0.0.1:{self.port}/v1/records?limit=1",
                headers={"Accept": "application/json", "Origin": origin},
            )
            with urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers)

        status, headers = get_with_origin("http://127.0.0.1:4173")
        self.assertEqual(status, 200)
        self.assertEqual(
            headers.get("Access-Control-Allow-Origin"), "http://127.0.0.1:4173"
        )

        status, headers = get_with_origin("https://evil.example")
        self.assertEqual(status, 200)
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

        # Preflight
        req = Request(
            f"http://127.0.0.1:{self.port}/v1/records",
            method="OPTIONS",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        with urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 204)
            self.assertEqual(
                resp.headers.get("Access-Control-Allow-Origin"), "http://localhost:3000"
            )
            methods = (resp.headers.get("Access-Control-Allow-Methods") or "").upper()
            self.assertIn("GET", methods)
            self.assertNotIn("POST", methods)

    def test_api_gold_only_no_csv_required(self):
        """Hard guarantee: API works with Gold only and zero CSV projections."""
        self.assertFalse((self.data_dir / config.RECORDS_FILE).exists())
        self.assertFalse((self.data_dir / config.HISTORY_FILE).exists())
        status, body = self._get("/v1/records")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(body["items"]), 1)
        status, meta = self._get("/v1/metadata")
        self.assertEqual(status, 200)
        self.assertEqual(meta["items"][0]["storage_model"], "lake_first_gold_duckdb")
        self.assertEqual(meta["items"][0]["records_source_kind"], "gold_parquet")


class SmokeCompileTests(unittest.TestCase):
    def test_modules_import(self):
        from book_opportunities import (
            api,
            config as cfg,
            domain_client,
            gold as gold_mod,
            http_client,
            ingest,
            policy as pol,
            store,
            synthesis,
        )

        self.assertTrue(cfg.FREE_ONLY)
        self.assertFalse(cfg.ALLOW_PAID_PROVIDERS)
        self.assertFalse(cfg.ALLOW_EXTERNAL_WRITES)
        self.assertEqual(len(cfg.DOMAIN_PRODUCTS), 7)
        self.assertEqual(cfg.LAKE_LAYER, "gold")
        self.assertEqual(cfg.LAKE_DATASET_OPPORTUNITIES, "money_opportunities")
        self.assertTrue(hasattr(api, "main"))
        self.assertTrue(hasattr(store, "load_records"))
        self.assertTrue(hasattr(gold_mod, "write_gold_dataset"))
        self.assertTrue(hasattr(pol, "provider_matrix"))
        self.assertTrue(hasattr(http_client, "request_json"))
        self.assertTrue(hasattr(domain_client, "fetch_all_domains"))
        self.assertTrue(hasattr(synthesis, "synthesize"))
        self.assertTrue(hasattr(ingest, "main"))

    def test_legacy_archive_exists_but_not_imported(self):
        archive = REPO_ROOT / "archive" / "legacy_scraper_ingest.py"
        self.assertTrue(archive.exists())
        ingest_src = (SRC_ROOT / "ingest.py").read_text(encoding="utf-8")
        self.assertNotIn("scrape_hackernews", ingest_src)
        self.assertNotIn("scrape_github_trending", ingest_src)
        self.assertNotIn("_firecrawl_fallback", ingest_src)


if __name__ == "__main__":
    unittest.main()
