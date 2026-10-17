"""Gold fallback ``scraped_at`` must be UTC: readers treat naive stamps as UTC."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from book_opportunities import gold, ingest


@unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset")
class GoldScrapedAtUtcTests(unittest.TestCase):
    def test_fallback_scraped_at_is_utc_on_a_non_utc_host(self):
        seen: list[str] = []

        def fake_write(items, **kwargs):
            seen.append(kwargs["scraped_at"])
            return {"record_count": 0, "gold_key": "gold/test"}

        old = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Bangkok"  # UTC+7, no DST
        time.tzset()
        try:
            with (
                tempfile.TemporaryDirectory() as tmp,
                mock.patch.object(gold, "write_gold_dataset", side_effect=fake_write),
                mock.patch.object(
                    gold,
                    "write_local_synthesis_manifest",
                    return_value=Path(tmp) / "m.json",
                ),
                mock.patch("builtins.print"),
            ):
                ingest.write_gold_from_synthesis(
                    {"items": [], "synthesis_run_id": "run-1"},
                    data_lake_uri="file:///tmp/unused",
                    output_dir=Path(tmp),
                )
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()
        self.assertTrue(seen)
        parsed = datetime.strptime(seen[0], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
        drift = abs((datetime.now(timezone.utc) - parsed).total_seconds())
        self.assertLess(drift, 300, f"scraped_at {seen[0]!r} is not UTC")


if __name__ == "__main__":
    unittest.main()
