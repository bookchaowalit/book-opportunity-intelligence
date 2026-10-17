"""Deterministic opportunity synthesis from domain data-product envelopes.

Pipeline: normalize → deduplicate → categorize → score → rank

Does not scrape upstream sources. Does not perform external writes.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from . import config
from .domain_client import DomainLoadResult


# Stable category taxonomy (kept for opportunity.v1 consumers).
OPPORTUNITY_CATEGORIES: dict[str, dict[str, Any]] = {
    "tcg": {
        "name": "TCG Cards",
        "keywords": ("pokemon", "tcg", "trading card"),
    },
    "digital-products": {
        "name": "Digital Products",
        "keywords": ("printable", "template", "digital", "svg"),
    },
    "ai-content": {
        "name": "AI-Generated Content",
        "keywords": ("ai", "agent", "llm", "gpt", "automation"),
    },
    "print-on-demand": {
        "name": "Print on Demand",
        "keywords": ("print", "merch", "pod"),
    },
    "courses": {
        "name": "Online Courses",
        "keywords": ("course", "tutorial", "masterclass"),
    },
    "freelance-services": {
        "name": "Freelance Services",
        "keywords": ("freelance", "seo", "developer", "consulting"),
    },
    "trending-products": {
        "name": "Trending Physical Products",
        "keywords": ("viral", "trending", "bestseller", "market"),
    },
    "content-creation": {
        "name": "Content Creation",
        "keywords": ("youtube", "tiktok", "newsletter", "blog"),
    },
}

DOMAIN_DEFAULT_CATEGORY: dict[str, str] = {
    "crypto": "trending-products",
    "stocks": "trending-products",
    "fx": "trending-products",
    "defi": "trending-products",
    "flights": "trending-products",
    "seo": "freelance-services",
    "ai_tools": "ai-content",
}

FIELDNAMES = [
    "title",
    "category",
    "source",
    "price",
    "url",
    "trend_score",
    "competition_level",
    "notes",
    "scraped_at",
    "score_delta",
    "trend_direction",
]


def utc_now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Parse a domain value as a finite float; NaN/inf/garbage -> default.

    Domain APIs are untrusted input: a NaN or Infinity (valid in Python's JSON
    decoder) would otherwise crash ``int(abs(x) * k)`` in the scorers.
    """
    try:
        if value is None or value == "" or isinstance(value, bool):
            return default
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _slug_url(product_id: str, key: str) -> str:
    digest = hashlib.sha1(f"{product_id}:{key}".encode("utf-8")).hexdigest()[:12]
    return f"local://{product_id}/{digest}"


def _extract_input_lineage(item: dict[str, Any]) -> dict[str, str]:
    """Pull lake lineage from a domain product item when present."""
    return {
        "input_source_record_id": str(
            item.get("record_id")
            or item.get("source_record_id")
            or item.get("id")
            or ""
        ),
        "input_event_id": str(item.get("event_id") or ""),
        "input_raw_object_key": str(item.get("raw_object_key") or ""),
        "input_ingest_run_id": str(item.get("ingest_run_id") or ""),
    }


def normalize_domain_item(
    product_id: str,
    item: dict[str, Any],
    *,
    domain_status: str = "ok",
    domain_source: str = "api",
) -> dict[str, Any]:
    """Map one domain record into a raw opportunity candidate (pre-score)."""
    item = dict(item or {})
    lineage = _extract_input_lineage(item)
    category = DOMAIN_DEFAULT_CATEGORY.get(product_id, "trending-products")
    competition = "medium"
    price = ""
    title = ""
    url = ""
    notes = f"domain={product_id}; input_status={domain_status}; input_source={domain_source}"
    base_score = 55

    if product_id == "crypto":
        coin = str(item.get("coin_id") or item.get("symbol") or "asset")
        change = _safe_float(item.get("change_24h_pct"))
        price = str(item.get("price") or "")
        currency = str(item.get("currency") or "usd")
        title = f"Crypto signal: {coin} ({change:+.2f}% 24h)"
        url = str(item.get("url") or _slug_url(product_id, f"{coin}:{currency}"))
        notes = f"{notes}; volume={item.get('volume_24h', '')}"
        base_score = 50 + min(40, int(abs(change) * 4))
        competition = "high" if abs(change) > 5 else "medium"

    elif product_id == "stocks":
        symbol = str(item.get("symbol") or "TICKER")
        change_pct = _safe_float(item.get("change_pct"))
        price = str(item.get("price") or "")
        title = f"Stock signal: {symbol} ({change_pct:+.2f}%)"
        url = str(item.get("url") or _slug_url(product_id, symbol))
        base_score = 50 + min(35, int(abs(change_pct) * 6))
        competition = "high"

    elif product_id == "fx":
        base = str(item.get("base") or "?")
        currency = str(item.get("currency") or "?")
        rate = str(item.get("rate") or "")
        change = _safe_float(item.get("trend_change_pct"))
        title = f"FX signal: {base}/{currency} rate {rate}"
        price = rate
        url = str(item.get("url") or _slug_url(product_id, f"{base}:{currency}"))
        base_score = 48 + min(30, int(abs(change) * 20))
        competition = "medium"

    elif product_id == "defi":
        project = str(item.get("project") or "pool")
        symbol = str(item.get("symbol") or "")
        apy = _safe_float(item.get("apy"))
        title = f"DeFi yield: {project} {symbol} APY {apy:.2f}%"
        price = f"{apy:.2f}%"
        url = str(
            item.get("url")
            or _slug_url(product_id, str(item.get("pool_id") or project))
        )
        base_score = 55 + min(35, int(apy * 2))
        competition = "medium" if apy < 20 else "high"
        category = "trending-products"

    elif product_id == "flights":
        origin = str(item.get("origin") or "?")
        dest = str(item.get("destination") or "?")
        price_thb = str(item.get("price_thb") or "")
        title = f"Flight deal: {origin}-{dest}"
        price = f"฿{price_thb}" if price_thb else ""
        url = str(item.get("url") or _slug_url(product_id, f"{origin}-{dest}"))
        # Cheaper routes score higher (deterministic).
        p = _safe_float(price_thb, 99999)
        base_score = 70 if p and p < 5000 else 60 if p < 10000 else 50
        competition = "medium"

    elif product_id == "seo":
        keyword = str(item.get("keyword") or "keyword")
        rank = item.get("best_rank")
        found = str(item.get("found") or "").lower() in {"true", "1", "yes"}
        title = f"SEO keyword: {keyword}"
        url = str(
            item.get("best_url") or item.get("url") or _slug_url(product_id, keyword)
        )
        rank_i = _safe_int(rank, 0)
        if found and rank_i > 0:
            base_score = max(40, 90 - rank_i * 3)
            competition = "low" if rank_i <= 5 else "medium"
        else:
            base_score = 45
            competition = "high"
            notes = f"{notes}; not ranked yet"
        category = "freelance-services"

    elif product_id == "ai_tools":
        name = str(item.get("name") or "AI tool")
        upvotes = _safe_int(item.get("upvotes"))
        pricing = str(item.get("pricing") or "")
        title = f"AI tool: {name}"
        url = str(item.get("url") or _slug_url(product_id, name))
        price = pricing
        base_score = 55 + min(35, upvotes // 5)
        competition = "low" if pricing.lower() == "free" else "medium"
        category = "ai-content"
        notes = (
            f"{notes}; tags={item.get('opportunity_tags') or item.get('tags') or ''}"
        )

    else:
        title = str(item.get("title") or item.get("name") or product_id)
        url = str(item.get("url") or _slug_url(product_id, title))

    # Domain input health soft-penalty (still usable for synthesis).
    if domain_status == "stale":
        base_score = max(0, base_score - 5)
    elif domain_status == "empty":
        base_score = 0

    return {
        "title": title[:160],
        "category": category,
        "source": f"domain:{product_id}",
        "price": price,
        "url": url,
        "trend_score": int(max(0, min(100, base_score))),
        "competition_level": competition,
        "notes": notes[:240],
        "domain_product": product_id,
        "domain_status": domain_status,
        "domain_input_source": domain_source,
        "input_source_product": product_id,
        "input_source_record_id": lineage["input_source_record_id"],
        "input_event_id": lineage["input_event_id"],
        "input_raw_object_key": lineage["input_raw_object_key"],
        "input_ingest_run_id": lineage["input_ingest_run_id"],
        "input_domain_status": domain_status,
        "input_domain_source": domain_source,
        "_dedupe_key": (url or title).strip().lower(),
    }


def normalize_domain_results(
    results: Iterable[DomainLoadResult],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for result in results:
        # empty/unavailable/timeout/error with no items contribute nothing
        if not result.items:
            continue
        domain_status = result.state
        if result.envelope and result.envelope.get("data_status"):
            # Prefer envelope data_status for scoring context when present.
            domain_status = str(result.envelope.get("data_status"))
        for item in result.items:
            if not isinstance(item, dict):
                continue
            candidates.append(
                normalize_domain_item(
                    result.product_id,
                    item,
                    domain_status=domain_status,
                    domain_source=result.source,
                )
            )
    return candidates


_THAI_CHARS = re.compile(r"[\u0e00-\u0e7f]+")
_THAI_NGRAM = 3


def _fold(text: str) -> str:
    """NFKC + lowercase for matching.

    NFKC maps full-width Latin ("ＢＴＣ") to ASCII and the precomposed Thai
    sara am (U+0E33) to nikhahit + sara aa, so the two spellings sources use
    produce the same tokens and trigrams.
    """
    return unicodedata.normalize("NFKC", text).lower()


def _title_tokens(title: str) -> set[str]:
    """Latin/digit words; Thai has no word spaces, so see ``_thai_ngrams``."""
    return {t for t in re.split(r"[^a-z0-9]+", _fold(title)) if t}


def _thai_ngrams(title: str) -> set[str]:
    """Character trigrams of the title's Thai text, ignoring spacing.

    Thai is written without spaces between words, so word tokens cannot be
    compared. All Thai characters are joined (spacing and punctuation vary
    between sources) and split into overlapping trigrams.
    """
    thai = "".join(_THAI_CHARS.findall(_fold(title)))
    if not thai:
        return set()
    if len(thai) <= _THAI_NGRAM:
        return {"th:" + thai}
    return {
        "th:" + thai[i : i + _THAI_NGRAM] for i in range(len(thai) - _THAI_NGRAM + 1)
    }


def fuzzy_title_match(title_a: str, title_b: str, threshold: float = 0.6) -> bool:
    thai_a = _thai_ngrams(title_a)
    thai_b = _thai_ngrams(title_b)
    if thai_a or thai_b:
        # Thai (or mixed) titles: compare trigram + Latin/digit token sets
        # against the LARGER set. Unlike the Latin rule below there is no
        # word count to guard against a short title contained in a long one
        # ("บิตคอยน์" vs "ราคาบิตคอยน์วันนี้"), so both must overlap mostly.
        grams_a = thai_a | _title_tokens(title_a)
        grams_b = thai_b | _title_tokens(title_b)
        shared = len(grams_a & grams_b)
        return shared >= 2 and shared / max(len(grams_a), len(grams_b)) >= threshold
    words_a = _title_tokens(title_a)
    words_b = _title_tokens(title_b)
    if not words_a or not words_b:
        return False
    shared = len(words_a & words_b)
    shorter = min(len(words_a), len(words_b))
    if shorter == 1:
        # A one-word title ("Bitcoin") used to merge with any title containing
        # that word; only an identical one-word title is a duplicate now.
        return words_a == words_b
    return shared >= 2 and shared / shorter >= threshold


def deduplicate(opportunities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic cross-source dedupe by URL then fuzzy title.

    Keeps highest score; stable order by first-seen index among winners.
    """
    merged: list[dict[str, Any]] = []
    seen_titles: list[tuple[str, int]] = []

    for opp in opportunities:
        url = str(opp.get("url") or "")
        title = str(opp.get("title") or "")
        url_match = None
        if url:
            for i, existing in enumerate(merged):
                if existing.get("url") == url:
                    url_match = i
                    break
        if url_match is not None:
            existing = merged[url_match]
            if _safe_int(opp.get("trend_score")) > _safe_int(
                existing.get("trend_score")
            ):
                old_source = existing.get("source")
                merged[url_match] = dict(opp)
                merged[url_match]["notes"] = (
                    f"Cross-source: {old_source}, {opp.get('source')} | {opp.get('notes', '')}"
                )[:240]
            else:
                existing["notes"] = (
                    f"Cross-source: {existing.get('source')}, {opp.get('source')} | "
                    f"{existing.get('notes', '')}"
                )[:240]
            merged[url_match]["trend_score"] = min(
                100, _safe_int(merged[url_match].get("trend_score")) + 3
            )
            continue

        title_match = None
        for st_title, st_idx in seen_titles:
            if fuzzy_title_match(title, st_title):
                title_match = st_idx
                break
        if title_match is not None:
            existing = merged[title_match]
            if _safe_int(opp.get("trend_score")) > _safe_int(
                existing.get("trend_score")
            ):
                old_source = existing.get("source")
                merged[title_match] = dict(opp)
                merged[title_match]["notes"] = (
                    f"Cross-source: {old_source}, {opp.get('source')} | {opp.get('notes', '')}"
                )[:240]
            else:
                existing["notes"] = (
                    f"Cross-source: {existing.get('source')}, {opp.get('source')} | "
                    f"{existing.get('notes', '')}"
                )[:240]
            merged[title_match]["trend_score"] = min(
                100, _safe_int(merged[title_match].get("trend_score")) + 3
            )
            continue

        seen_titles.append((title, len(merged)))
        merged.append(dict(opp))

    return merged


def _has_keyword(text: str, keyword: str) -> bool:
    """Whole-word / phrase match so 'ai' does not match inside 'domain'."""
    if not text or not keyword:
        return False
    if " " in keyword or "-" in keyword:
        return keyword in text
    return (
        re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None
    )


def categorize_opportunity(opportunity: dict[str, Any]) -> str:
    # Prefer the domain-mapped category; only reclassify on strong title keywords.
    title_lower = _fold(str(opportunity.get("title") or ""))
    default = str(opportunity.get("category") or "trending-products")
    best_match = None
    best_count = 0
    for category, meta in OPPORTUNITY_CATEGORIES.items():
        count = sum(1 for kw in meta["keywords"] if _has_keyword(title_lower, kw))
        if count > best_count:
            best_count = count
            best_match = category
    return best_match if best_count > 0 else default


def score_opportunity(opportunity: dict[str, Any]) -> int:
    score = _safe_int(opportunity.get("trend_score"), 50)
    title_lower = _fold(str(opportunity.get("title") or ""))
    if _has_keyword(title_lower, "ai"):
        score += 10
    if any(
        _has_keyword(title_lower, kw)
        for kw in ("money", "revenue", "profit", "income", "yield", "deal")
    ):
        score += 5
    if opportunity.get("competition_level") == "low":
        score += 10
    elif opportunity.get("competition_level") == "high":
        score -= 10
    if opportunity.get("domain_status") == "stale":
        score -= 5
    return max(0, min(100, score))


def rank_opportunities(opportunities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable rank: score desc, then title asc, then url asc."""
    return sorted(
        opportunities,
        key=lambda o: (
            -_safe_int(o.get("trend_score")),
            str(o.get("title") or ""),
            str(o.get("url") or ""),
        ),
    )


def finalize_records(
    opportunities: list[dict[str, Any]],
    *,
    scraped_at: Optional[str] = None,
    synthesis_run_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    stamp = scraped_at or utc_now_stamp()
    finalized: list[dict[str, Any]] = []
    for opp in opportunities:
        row = {k: opp.get(k, "") for k in FIELDNAMES}
        row["title"] = str(opp.get("title") or "")[:160]
        row["category"] = categorize_opportunity(opp)
        row["source"] = str(opp.get("source") or "domain")
        row["price"] = str(opp.get("price") or "")
        row["url"] = str(opp.get("url") or "")
        row["trend_score"] = int(
            score_opportunity({**opp, "category": row["category"]})
        )
        row["competition_level"] = str(opp.get("competition_level") or "medium")
        row["notes"] = str(opp.get("notes") or "")[:240]
        row["scraped_at"] = stamp
        row["event_time"] = stamp
        row["score_delta"] = str(opp.get("score_delta", 0))
        row["trend_direction"] = str(opp.get("trend_direction") or "stable")
        # Gold lineage — preserved through score/rank for replay + audit.
        row["synthesis_run_id"] = str(
            synthesis_run_id or opp.get("synthesis_run_id") or ""
        )
        row["input_source_product"] = str(
            opp.get("input_source_product") or opp.get("domain_product") or ""
        )
        row["input_source_record_id"] = str(opp.get("input_source_record_id") or "")
        row["input_event_id"] = str(opp.get("input_event_id") or "")
        row["input_raw_object_key"] = str(opp.get("input_raw_object_key") or "")
        row["input_ingest_run_id"] = str(opp.get("input_ingest_run_id") or "")
        row["input_domain_status"] = str(
            opp.get("input_domain_status") or opp.get("domain_status") or ""
        )
        row["input_domain_source"] = str(
            opp.get("input_domain_source") or opp.get("domain_input_source") or ""
        )
        # Keep domain_* aliases for scoring-related soft signals if re-scored.
        row["domain_product"] = row["input_source_product"]
        row["domain_status"] = row["input_domain_status"]
        row["domain_input_source"] = row["input_domain_source"]
        finalized.append(row)
    ranked = rank_opportunities(finalized)
    for idx, row in enumerate(ranked, 1):
        row["rank_position"] = idx
        # Stable product id for opportunity.v1 consumers (url-based hash).
        if not row.get("id"):
            row["id"] = row.get("url") or f"rank:{idx}"
    return ranked


def synthesize(
    results: list[DomainLoadResult],
    *,
    scraped_at: Optional[str] = None,
    synthesis_run_id: Optional[str] = None,
) -> dict[str, Any]:
    """Full deterministic pipeline from domain load results to ranked opportunities."""
    # Local import avoids circular dependency at module load time.
    from .gold import make_synthesis_run_id

    run_id = synthesis_run_id or make_synthesis_run_id(
        seed=",".join(f"{r.product_id}:{r.state}:{len(r.items)}" for r in results)
    )
    stamp = scraped_at or utc_now_stamp()
    normalized = normalize_domain_results(results)
    deduped = deduplicate(normalized)
    ranked = finalize_records(deduped, scraped_at=stamp, synthesis_run_id=run_id)

    input_summary = [
        {
            "product_id": r.product_id,
            "port": r.port,
            "repo": next(
                (
                    p.get("repo")
                    for p in config.DOMAIN_PRODUCTS
                    if p["id"] == r.product_id
                ),
                "",
            ),
            "state": r.state,
            "source": r.source,
            "item_count": len(r.items),
            "error_message": r.error_message,
            # Sample lineage from first item when present (domain may be empty/down).
            "sample_record_id": (
                str(
                    (
                        r.items[0].get("record_id")
                        or r.items[0].get("source_record_id")
                        or ""
                    )
                )
                if r.items
                else ""
            ),
            "sample_event_id": (
                str(r.items[0].get("event_id") or "") if r.items else ""
            ),
            "sample_raw_object_key": (
                str(r.items[0].get("raw_object_key") or "") if r.items else ""
            ),
            "sample_ingest_run_id": (
                str(r.items[0].get("ingest_run_id") or "") if r.items else ""
            ),
        }
        for r in results
    ]
    usable = sum(1 for r in results if r.items)
    # Domain outages must not erase input_summary; surface aggregate health.
    failed_states = {"timeout", "unavailable", "error"}
    if not ranked:
        data_status = "empty"
    elif any(r.state == "stale" for r in results if r.items):
        data_status = "stale"
    elif any(r.state in failed_states for r in results) and usable < len(results):
        # Partial domain availability still produces usable synthesis.
        data_status = "ok"
    else:
        data_status = "ok"

    return {
        "schema_version": "opportunity.v1",
        "source": "book-opportunity-intelligence",
        "retrieved_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "data_status": data_status,
        "synthesis_run_id": run_id,
        "items": ranked,
        "next_cursor": None,
        "input_summary": input_summary,
        "domains_usable": usable,
        "domains_total": len(results),
        "layer": "gold",
    }
