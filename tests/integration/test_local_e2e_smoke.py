"""Opt-in local end-to-end smoke test for domain APIs + synthesis + :8108.

Run only when explicitly enabled:

    RUN_LOCAL_E2E=1 PYTHONPATH=src python3 -m unittest tests.integration.test_local_e2e_smoke -v

Or:

    RUN_LOCAL_E2E=1 PYTHONPATH=src python3 tests/integration/test_local_e2e_smoke.py

This starts sibling domain data-product APIs on 127.0.0.1:8101–8107 with
fixture/local data only, runs synthesis, serves opportunity API on :8108, and
verifies contracts. No paid APIs, credentials, or internet access required.

Skipped by default so normal unit tests / CI stay offline and fast.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse


# Opt-in gate — normal `unittest discover -s tests` will skip this module's tests
# unless RUN_LOCAL_E2E=1 is set.
RUN_FLAG = os.environ.get("RUN_LOCAL_E2E", "").strip().lower() in {"1", "true", "yes", "on"}

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPO_ROOT.parent  # .../book-apps/tools
SRC_ROOT = REPO_ROOT / "src"

# Sibling domain products (fixture-backed local APIs).
DOMAIN_SERVICES: list[dict[str, Any]] = [
    {
        "id": "crypto",
        "repo_dir": "book-crypto-data",
        "module": "book_crypto",
        "port": 8101,
        "schema": "crypto.v1",
    },
    {
        "id": "stocks",
        "repo_dir": "book-stock-data",
        "module": "book_stock",
        "port": 8102,
        "schema": "stock.v1",
    },
    {
        "id": "fx",
        "repo_dir": "book-fx-data",
        "module": "book_fx",
        "port": 8103,
        "schema": "fx.v1",
    },
    {
        "id": "defi",
        "repo_dir": "book-defi-data",
        "module": "book_defi",
        "port": 8104,
        "schema": "defi.v1",
    },
    {
        "id": "flights",
        "repo_dir": "book-flight-data",
        "module": "book_flight",
        "port": 8105,
        "schema": "flight.v1",
    },
    {
        "id": "seo",
        "repo_dir": "book-seo-data",
        "module": "book_seo",
        "port": 8106,
        "schema": "seo.v1",
    },
    {
        "id": "ai_tools",
        "repo_dir": "book-ai-tools-data",
        "module": "book_ai_tools",
        "port": 8107,
        "schema": "ai_tools.v1",
    },
]

OPPORTUNITY_PORT = 8108
ENVELOPE_KEYS = (
    "schema_version",
    "source",
    "retrieved_at",
    "data_status",
    "items",
    "next_cursor",
)
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def _http_json(
    method: str,
    url: str,
    *,
    timeout: float = 3.0,
    body: Optional[bytes] = None,
) -> tuple[int, Any]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in LOCAL_HOSTS:
        raise AssertionError(f"Non-localhost request forbidden in e2e: {url}")
    req = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return exc.code, payload


def _wait_http(url: str, *, timeout_s: float = 15.0) -> tuple[int, Any]:
    deadline = time.time() + timeout_s
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            return _http_json("GET", url, timeout=1.0)
        except Exception as exc:  # connection refused while booting
            last_err = exc
            time.sleep(0.1)
    raise TimeoutError(f"Service not ready at {url}: {last_err}")


def _assert_envelope(body: dict[str, Any], *, schema: Optional[str] = None) -> None:
    for key in ENVELOPE_KEYS:
        assert key in body, f"missing envelope key {key}"
    if schema is not None:
        assert body["schema_version"] == schema


class ManagedProcess:
    def __init__(
        self,
        *,
        name: str,
        port: int,
        popen: subprocess.Popen,
        data_dir: Path,
    ) -> None:
        self.name = name
        self.port = port
        self.popen = popen
        self.data_dir = data_dir

    def terminate(self) -> None:
        if self.popen.poll() is not None:
            return
        try:
            self.popen.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            self.popen.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.popen.kill()
            self.popen.wait(timeout=5)


@unittest.skipUnless(
    RUN_FLAG,
    "opt-in only: set RUN_LOCAL_E2E=1 to run local multi-API e2e smoke",
)
class LocalEndToEndSmokeTest(unittest.TestCase):
    """Boots 8101–8108 on loopback with fixtures and verifies synthesis contracts."""

    processes: list[ManagedProcess]
    temp_root: tempfile.TemporaryDirectory
    synth_dir: Path
    seen_urls: list[str]

    @classmethod
    def setUpClass(cls) -> None:
        missing = []
        for svc in DOMAIN_SERVICES:
            path = TOOLS_ROOT / svc["repo_dir"]
            if not (path / "src").is_dir() or not (path / "fixtures").is_dir():
                missing.append(svc["repo_dir"])
        if missing:
            raise unittest.SkipTest(
                "sibling domain repositories unavailable: " + ", ".join(missing)
            )

        needed_ports = [s["port"] for s in DOMAIN_SERVICES] + [OPPORTUNITY_PORT]
        busy = [p for p in needed_ports if not _port_free(p)]
        if busy:
            raise unittest.SkipTest(
                f"required loopback ports already in use: {busy}; free them or skip e2e"
            )

        cls.processes = []
        cls.temp_root = tempfile.TemporaryDirectory(prefix="opp-e2e-")
        cls.seen_urls = []
        root = Path(cls.temp_root.name)
        cls.synth_dir = root / "opportunity-data"
        cls.synth_dir.mkdir(parents=True, exist_ok=True)

        try:
            for svc in DOMAIN_SERVICES:
                cls.processes.append(cls._start_domain_api(svc, root / svc["id"]))
            # Wait for all domain health endpoints
            for svc in DOMAIN_SERVICES:
                status, body = _wait_http(f"http://127.0.0.1:{svc['port']}/healthz")
                assert status == 200, f"{svc['id']} healthz={status}"
                assert body.get("status") == "ok"
        except Exception:
            cls._shutdown_all()
            cls.temp_root.cleanup()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls._shutdown_all()
        try:
            cls.temp_root.cleanup()
        except Exception:
            pass

    @classmethod
    def _shutdown_all(cls) -> None:
        for proc in reversed(getattr(cls, "processes", []) or []):
            try:
                proc.terminate()
            except Exception:
                pass
        cls.processes = []

    @classmethod
    def _base_env(cls, *, data_dir: Path, fixtures_dir: Path, port: int, pythonpath: str) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "DATA_DIR": str(data_dir),
                "FIXTURES_DIR": str(fixtures_dir),
                "API_HOST": "127.0.0.1",
                "API_PORT": str(port),
                "FREE_ONLY": "true",
                "ALLOW_PAID_PROVIDERS": "false",
                "ALLOW_EXTERNAL_WRITES": "false",
                "ALLOW_REFRESH": "false",
                "PYTHONPATH": pythonpath,
                # Hardening: discourage accidental proxy egress
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
                "HTTP_PROXY": "",
                "HTTPS_PROXY": "",
                "http_proxy": "",
                "https_proxy": "",
                "ALL_PROXY": "",
            }
        )
        return env

    @classmethod
    def _start_domain_api(cls, svc: dict[str, Any], data_dir: Path) -> ManagedProcess:
        repo = (TOOLS_ROOT / svc["repo_dir"]).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        fixtures_dir = repo / "fixtures"
        pythonpath = str(repo / "src")
        env = cls._base_env(
            data_dir=data_dir,
            fixtures_dir=fixtures_dir,
            port=int(svc["port"]),
            pythonpath=pythonpath,
        )

        seed = subprocess.run(
            [
                sys.executable,
                "-c",
                f"from {svc['module']}.store import seed_fixtures; seed_fixtures()",
            ],
            cwd=str(repo),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if seed.returncode != 0:
            raise RuntimeError(
                f"Failed to seed fixtures for {svc['id']}: {seed.stderr or seed.stdout}"
            )

        popen = subprocess.Popen(
            [
                sys.executable,
                "-m",
                f"{svc['module']}.api",
                "--host",
                "127.0.0.1",
                "--port",
                str(svc["port"]),
            ],
            cwd=str(repo),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return ManagedProcess(
            name=svc["id"],
            port=int(svc["port"]),
            popen=popen,
            data_dir=data_dir,
        )

    def _start_opportunity_api(self) -> ManagedProcess:
        pythonpath = str(SRC_ROOT.resolve())
        env = self._base_env(
            data_dir=self.synth_dir,
            fixtures_dir=REPO_ROOT / "fixtures",
            port=OPPORTUNITY_PORT,
            pythonpath=pythonpath,
        )
        # Prefer already-synthesized local data; do not re-scrape.
        popen = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "book_opportunities.api",
                "--host",
                "127.0.0.1",
                "--port",
                str(OPPORTUNITY_PORT),
            ],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        managed = ManagedProcess(
            name="opportunity",
            port=OPPORTUNITY_PORT,
            popen=popen,
            data_dir=self.synth_dir,
        )
        self.processes.append(managed)
        _wait_http(f"http://127.0.0.1:{OPPORTUNITY_PORT}/healthz")
        return managed

    def test_e2e_domain_apis_synthesis_and_opportunity_api(self) -> None:
        # 1) Every domain responds to GET /v1/records on loopback.
        for svc in DOMAIN_SERVICES:
            url = f"http://127.0.0.1:{svc['port']}/v1/records?limit=5"
            status, body = _http_json("GET", url)
            self.assertEqual(status, 200, svc["id"])
            _assert_envelope(body, schema=svc["schema"])
            self.assertIsInstance(body["items"], list)
            self.assertGreaterEqual(len(body["items"]), 1, svc["id"])
            self.assertIn(body["data_status"], {"ok", "stale", "empty", "malformed"})

        # 2) Synthesis against live local APIs (not fixture short-circuit).
        if str(SRC_ROOT) not in sys.path:
            sys.path.insert(0, str(SRC_ROOT))
        from book_opportunities import config as opp_config
        from book_opportunities.domain_client import fetch_all_domains
        from book_opportunities.http_client import request_json
        from book_opportunities.ingest import run_synthesis

        recorded: list[str] = []
        original_request_json = request_json

        def tracking_request_json(method: str, url: str, **kwargs):
            recorded.append(url)
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            self.assertIn(host, LOCAL_HOSTS, f"egress forbidden: {url}")
            self.assertEqual(method.upper(), "GET")
            self.assertTrue((parsed.path or "").startswith("/v1/"))
            port = parsed.port
            self.assertIsNotNone(port)
            self.assertIn(port, {s["port"] for s in DOMAIN_SERVICES})
            return original_request_json(method, url, **kwargs)

        # Patch both import locations used by the client.
        import book_opportunities.domain_client as domain_client_mod
        import book_opportunities.http_client as http_client_mod

        domain_client_mod.request_json = tracking_request_json  # type: ignore[assignment]
        http_client_mod.request_json = tracking_request_json  # type: ignore[assignment]
        try:
            domain_results = fetch_all_domains(use_fixtures=False, timeout_ms=4000)
            self.assertEqual(len(domain_results), 7)
            for result in domain_results:
                self.assertEqual(result.source, "api", result.product_id)
                self.assertTrue(result.items, result.product_id)

            # Ensure synthesis contacted only 8101–8107 loopback GETs.
            self.assertEqual(len(recorded), 7)
            ports_hit = sorted({urlparse(u).port for u in recorded})
            self.assertEqual(ports_hit, [8101, 8102, 8103, 8104, 8105, 8106, 8107])
            for url in recorded:
                self.assertIn("127.0.0.1", url)
                self.assertIn("/v1/records", url)
                self.assertNotIn("firecrawl", url.lower())
                self.assertNotIn("telegram", url.lower())
                self.assertNotIn("todoist", url.lower())

            synth = run_synthesis(
                use_fixtures=False,
                dry_run=False,
                output_dir=self.synth_dir,
                timeout_ms=4000,
            )
        finally:
            domain_client_mod.request_json = original_request_json  # type: ignore[assignment]
            http_client_mod.request_json = original_request_json  # type: ignore[assignment]

        self.assertEqual(synth["schema_version"], "opportunity.v1")
        self.assertEqual(synth["source"], "book-opportunity-intelligence")
        self.assertIn(synth["data_status"], {"ok", "stale", "empty"})
        self.assertIsInstance(synth["items"], list)
        self.assertGreaterEqual(len(synth["items"]), 1)
        self.assertTrue((self.synth_dir / opp_config.RECORDS_FILE).exists())

        # 3) Opportunity API on 8108 from synthesized local data.
        self._start_opportunity_api()

        status, health = _http_json("GET", f"http://127.0.0.1:{OPPORTUNITY_PORT}/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["repository"], "book-opportunity-intelligence")

        status, meta = _http_json("GET", f"http://127.0.0.1:{OPPORTUNITY_PORT}/v1/metadata")
        self.assertEqual(status, 200)
        _assert_envelope(meta, schema="opportunity.v1")
        self.assertEqual(meta["source"], "book-opportunity-intelligence")
        self.assertTrue(meta["items"])
        self.assertEqual(meta["items"][0].get("role"), "synthesis_consumer")
        self.assertEqual(
            meta["items"][0].get("consumes_ports"),
            [8101, 8102, 8103, 8104, 8105, 8106, 8107],
        )

        status, records = _http_json(
            "GET", f"http://127.0.0.1:{OPPORTUNITY_PORT}/v1/records?limit=10"
        )
        self.assertEqual(status, 200)
        _assert_envelope(records, schema="opportunity.v1")
        self.assertGreaterEqual(len(records["items"]), 1)

        status, history = _http_json(
            "GET", f"http://127.0.0.1:{OPPORTUNITY_PORT}/v1/history?limit=10"
        )
        self.assertEqual(status, 200)
        _assert_envelope(history, schema="opportunity.v1")
        self.assertGreaterEqual(len(history["items"]), 1)

        # 4) POST /v1/refresh remains forbidden by default.
        status, refresh = _http_json(
            "POST",
            f"http://127.0.0.1:{OPPORTUNITY_PORT}/v1/refresh",
            body=b"{}",
        )
        self.assertEqual(status, 403)
        self.assertEqual(refresh["data_status"], "forbidden")

        # 5) Child processes still alive (no crash) and bound only to started ports.
        for proc in self.processes:
            self.assertIsNone(proc.popen.poll(), f"process exited early: {proc.name}")


def main() -> int:
    if not RUN_FLAG:
        print(
            "Local e2e smoke is opt-in.\n"
            "  RUN_LOCAL_E2E=1 PYTHONPATH=src python3 tests/integration/test_local_e2e_smoke.py\n"
            "Requires sibling domain repos under ../ and free ports 8101–8108."
        )
        return 2
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(LocalEndToEndSmokeTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
