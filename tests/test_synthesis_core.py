"""Unit tests for the pure synthesis core: normalize, dedupe, score, rank."""
from __future__ import annotations

import unittest

from book_opportunities.domain_client import DomainLoadResult
from book_opportunities.synthesis import (
    _has_keyword,
    _safe_float,
    _safe_int,
    categorize_opportunity,
    deduplicate,
    finalize_records,
    fuzzy_title_match,
    normalize_domain_item,
    rank_opportunities,
    score_opportunity,
    synthesize,
)


class SafeParsingTests(unittest.TestCase):
    def test_safe_float_rejects_non_finite_and_garbage(self) -> None:
        for bad in ("nan", "NaN", "inf", "-Infinity", float("nan"), float("inf"), "abc", None, "", [], True):
            with self.subTest(value=bad):
                self.assertEqual(_safe_float(bad, 1.5), 1.5)
        self.assertEqual(_safe_float("2.5"), 2.5)
        self.assertEqual(_safe_float(-3), -3.0)

    def test_safe_int_handles_floats_and_overflow(self) -> None:
        self.assertEqual(_safe_int("72.9"), 72)
        self.assertEqual(_safe_int(float("inf"), 7), 7)
        self.assertEqual(_safe_int("nan", 7), 7)
        self.assertEqual(_safe_int(None, 50), 50)


class NormalizeTests(unittest.TestCase):
    def test_crypto_with_nan_change_does_not_crash(self) -> None:
        row = normalize_domain_item("crypto", {"coin_id": "btc", "change_24h_pct": float("nan")})
        self.assertEqual(row["trend_score"], 50)
        self.assertIn("+0.00%", row["title"])

    def test_crypto_score_caps_and_competition(self) -> None:
        row = normalize_domain_item("crypto", {"coin_id": "eth", "change_24h_pct": "-25"})
        self.assertEqual(row["trend_score"], 90)
        self.assertEqual(row["competition_level"], "high")
        self.assertTrue(row["url"].startswith("local://crypto/"))

    def test_url_slug_is_deterministic(self) -> None:
        a = normalize_domain_item("stocks", {"symbol": "AAPL"})
        b = normalize_domain_item("stocks", {"symbol": "AAPL"})
        self.assertEqual(a["url"], b["url"])

    def test_seo_ranked_vs_unranked(self) -> None:
        ranked = normalize_domain_item("seo", {"keyword": "k", "best_rank": 2, "found": "true"})
        self.assertEqual((ranked["trend_score"], ranked["competition_level"]), (84, "low"))
        unranked = normalize_domain_item("seo", {"keyword": "k", "found": "no"})
        self.assertEqual((unranked["trend_score"], unranked["competition_level"]), (45, "high"))
        self.assertIn("not ranked yet", unranked["notes"])

    def test_flights_cheaper_routes_score_higher(self) -> None:
        cheap = normalize_domain_item("flights", {"origin": "BKK", "destination": "CNX", "price_thb": "1500"})
        mid = normalize_domain_item("flights", {"origin": "BKK", "destination": "NRT", "price_thb": "8000"})
        dear = normalize_domain_item("flights", {"origin": "BKK", "destination": "LHR", "price_thb": "25000"})
        self.assertEqual([cheap["trend_score"], mid["trend_score"], dear["trend_score"]], [70, 60, 50])
        self.assertEqual(cheap["price"], "฿1500")

    def test_domain_status_penalties_and_lineage(self) -> None:
        item = {"name": "Tool", "upvotes": 50, "pricing": "Free", "record_id": "r1", "event_id": "e1"}
        ok = normalize_domain_item("ai_tools", item)
        stale = normalize_domain_item("ai_tools", item, domain_status="stale")
        empty = normalize_domain_item("ai_tools", item, domain_status="empty")
        self.assertEqual(ok["trend_score"] - stale["trend_score"], 5)
        self.assertEqual(empty["trend_score"], 0)
        self.assertEqual(ok["input_source_record_id"], "r1")
        self.assertEqual(ok["input_event_id"], "e1")
        self.assertEqual(ok["competition_level"], "low")

    def test_unknown_product_falls_back_to_title(self) -> None:
        row = normalize_domain_item("other", {"title": "X" * 300})
        self.assertEqual(len(row["title"]), 160)


class ScoreAndRankTests(unittest.TestCase):
    def test_zero_score_is_not_treated_as_missing(self) -> None:
        self.assertEqual(score_opportunity({"trend_score": 0, "title": "flat"}), 0)
        self.assertEqual(score_opportunity({"title": "flat"}), 50)

    def test_keyword_boosts_are_whole_word(self) -> None:
        self.assertTrue(_has_keyword("new ai tool", "ai"))
        self.assertFalse(_has_keyword("domain names", "ai"))
        self.assertEqual(score_opportunity({"trend_score": 50, "title": "AI revenue deal"}), 65)
        self.assertEqual(score_opportunity({"trend_score": 50, "title": "Domain"}), 50)

    def test_competition_and_stale_adjustments_are_clamped(self) -> None:
        self.assertEqual(score_opportunity({"trend_score": 98, "title": "ai", "competition_level": "low"}), 100)
        self.assertEqual(
            score_opportunity({"trend_score": 3, "competition_level": "high", "domain_status": "stale"}), 0
        )

    def test_rank_is_stable_and_tolerates_bad_scores(self) -> None:
        rows = [
            {"title": "b", "url": "2", "trend_score": 60},
            {"title": "a", "url": "1", "trend_score": "60.0"},
            {"title": "c", "url": "3", "trend_score": "nan"},
        ]
        self.assertEqual([r["title"] for r in rank_opportunities(rows)], ["a", "b", "c"])

    def test_categorize_prefers_domain_category_without_keywords(self) -> None:
        self.assertEqual(categorize_opportunity({"title": "zzz", "category": "custom"}), "custom")


class DedupeTests(unittest.TestCase):
    def test_fuzzy_title_match(self) -> None:
        self.assertTrue(fuzzy_title_match("Crypto signal: BTC up", "crypto signal btc"))
        self.assertFalse(fuzzy_title_match("", "anything"))
        self.assertFalse(fuzzy_title_match("alpha beta", "gamma delta"))

    def test_same_url_keeps_higher_score_and_boosts(self) -> None:
        rows = [
            {"title": "One", "url": "u", "trend_score": 40, "source": "a"},
            {"title": "Different words", "url": "u", "trend_score": 70, "source": "b"},
        ]
        out = deduplicate(rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["trend_score"], 73)
        self.assertEqual(out[0]["source"], "b")
        self.assertIn("Cross-source: a, b", out[0]["notes"])

    def test_boost_is_capped_at_100_and_input_not_mutated(self) -> None:
        first = {"title": "x y", "url": "u", "trend_score": 99, "source": "a"}
        out = deduplicate([first, {"title": "x y", "url": "u", "trend_score": 10, "source": "b"}])
        self.assertEqual(out[0]["trend_score"], 100)
        self.assertEqual(first["trend_score"], 99)


class SynthesizeTests(unittest.TestCase):
    def _result(self, product_id: str, state: str, items: list[dict]) -> DomainLoadResult:
        return DomainLoadResult(product_id=product_id, port=8101, state=state, source="fixture",
                                envelope={"items": items}, items=items)

    def test_end_to_end_is_deterministic_and_ranked(self) -> None:
        results = [
            self._result("crypto", "ready", [{"coin_id": "btc", "change_24h_pct": "3"}]),
            self._result("ai_tools", "ready", [{"name": "Writer", "upvotes": 100, "pricing": "free"}]),
            self._result("fx", "timeout", []),
        ]
        a = synthesize(results, scraped_at="2026-01-01 00:00:00", synthesis_run_id="run-1")
        b = synthesize(results, scraped_at="2026-01-01 00:00:00", synthesis_run_id="run-1")
        self.assertEqual(a["items"], b["items"])
        self.assertEqual(a["data_status"], "ok")
        self.assertEqual((a["domains_usable"], a["domains_total"]), (2, 3))
        positions = [r["rank_position"] for r in a["items"]]
        self.assertEqual(positions, list(range(1, len(positions) + 1)))
        scores = [r["trend_score"] for r in a["items"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(all(r["synthesis_run_id"] == "run-1" for r in a["items"]))

    def test_stale_and_empty_status(self) -> None:
        stale = synthesize([self._result("stocks", "stale", [{"symbol": "X"}])], synthesis_run_id="r")
        self.assertEqual(stale["data_status"], "stale")
        empty = synthesize([self._result("stocks", "unavailable", [])], synthesis_run_id="r")
        self.assertEqual(empty["data_status"], "empty")
        self.assertEqual(empty["items"], [])

    def test_finalize_assigns_ids_from_url(self) -> None:
        rows = finalize_records([{"title": "t", "url": "local://x/1", "trend_score": 10}], scraped_at="s")
        self.assertEqual(rows[0]["id"], "local://x/1")
        self.assertEqual(rows[0]["scraped_at"], "s")


if __name__ == "__main__":
    unittest.main()
