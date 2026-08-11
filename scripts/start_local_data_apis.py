#!/usr/bin/env python3
"""Start free-only local data-product APIs on 127.0.0.1:8101–8108 (fixtures only).

Usage:
  python3 scripts/start_local_data_apis.py
  python3 scripts/start_local_data_apis.py --ready-file /tmp/apis.ready
  # Ctrl+C for clean shutdown

Does not scrape upstream, open public binds, or enable paid providers.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


TOOLS_ROOT = Path(__file__).resolve().parents[2]
OPP_ROOT = Path(__file__).resolve().parents[1]

SERVICES = [
    ("crypto", "book-crypto-data", "book_crypto", 8101),
    ("stocks", "book-stock-data", "book_stock", 8102),
    ("fx", "book-fx-data", "book_fx", 8103),
    ("defi", "book-defi-data", "book_defi", 8104),
    ("flights", "book-flight-data", "book_flight", 8105),
    ("seo", "book-seo-data", "book_seo", 8106),
    ("ai_tools", "book-ai-tools-data", "book_ai_tools", 8107),
    ("opportunities", "book-opportunity-intelligence", "book_opportunities", 8108),
]


def _env(data_dir: Path, fixtures_dir: Path, port: int, pythonpath: str) -> dict[str, str]:
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
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "http_proxy": "",
            "https_proxy": "",
        }
    )
    return env


def _wait(url: str, timeout_s: float = 20.0) -> None:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:
            last = exc
            time.sleep(0.1)
    raise TimeoutError(f"Not ready: {url} ({last})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ready-file", default="", help="Touch this file when all ports are up")
    parser.add_argument("--workdir", default="", help="Base temp dir for DATA_DIR (default: mkdtemp)")
    args = parser.parse_args(argv)

    base = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="data-apis-"))
    base.mkdir(parents=True, exist_ok=True)
    procs: list[subprocess.Popen] = []

    def shutdown(*_a):
        for p in reversed(procs):
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=4)
            except subprocess.TimeoutExpired:
                p.kill()
        print("\nAll local data-product APIs stopped.", flush=True)
        raise SystemExit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(f"DATA root: {base}")
    for name, repo_dir, module, port in SERVICES:
        repo = (TOOLS_ROOT / repo_dir).resolve()
        if not repo.exists():
            print(f"ERROR: missing sibling repo {repo}", file=sys.stderr)
            shutdown()
        data_dir = base / name
        data_dir.mkdir(parents=True, exist_ok=True)
        fixtures = repo / "fixtures"
        # Opportunity synthesis: seed CSV fixtures for API serve.
        env = _env(data_dir, fixtures, port, str(repo / "src"))
        seed = subprocess.run(
            [sys.executable, "-c", f"from {module}.store import seed_fixtures; seed_fixtures()"],
            cwd=str(repo),
            env=env,
            capture_output=True,
            text=True,
        )
        if seed.returncode != 0:
            print(f"ERROR seeding {name}: {seed.stderr or seed.stdout}", file=sys.stderr)
            shutdown()
        # For opportunity, also run fixture synthesis so records look consumer-like.
        if name == "opportunities":
            env_opp = env.copy()
            env_opp["PYTHONPATH"] = str(OPP_ROOT / "src")
            # Prefer domain-product fixtures if domain APIs not yet up: use --fixture synthesis
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "book_opportunities.ingest",
                    "--fixture",
                    "--output-dir",
                    str(data_dir),
                ],
                cwd=str(OPP_ROOT),
                env=env_opp,
                check=False,
                capture_output=True,
                text=True,
            )
        p = subprocess.Popen(
            [sys.executable, "-m", f"{module}.api", "--host", "127.0.0.1", "--port", str(port)],
            cwd=str(repo),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(p)
        print(f"  starting {name} on 127.0.0.1:{port} (pid {p.pid})")

    for name, _repo, _mod, port in SERVICES:
        _wait(f"http://127.0.0.1:{port}/healthz")
        print(f"  ready  :{port} ({name})")

    if args.ready_file:
        Path(args.ready_file).write_text("ready\n", encoding="utf-8")

    print("All APIs ready on 127.0.0.1:8101–8108 (fixture/local data only).")
    print("Press Ctrl+C to stop.")
    while True:
        for p in procs:
            if p.poll() is not None:
                print(f"ERROR: child exited early pid={p.pid} code={p.returncode}", file=sys.stderr)
                shutdown()
        time.sleep(0.5)


if __name__ == "__main__":
    raise SystemExit(main())
