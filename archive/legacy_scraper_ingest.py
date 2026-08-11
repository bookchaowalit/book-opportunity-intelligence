#!/usr/bin/env python3
"""
Money Opportunity Finder — Scrape trending opportunities from multiple sources.

Discovers current and emerging money-making opportunities:
- TCG cards (Pokémon, One Piece, etc.)
- AI-generated content (music, art, videos)
- Digital products (printables, templates, courses)
- Trending physical products
- Freelance/service opportunities
- Content creation trends

Sources (anti-bot resilient):
- Reddit RSS feeds (no auth needed)
- DuckDuckGo search (multi-query)
- Hacker News API (free, no auth)
- GitHub Trending (free API)
- ProductHunt RSS
- Etsy/eBay via DuckDuckGo
- Amazon/TikTok trending via DuckDuckGo

Outputs:
    - data/money_opportunities.csv (latest snapshot)
    - data/money_opportunities_history.csv (appended)
    - Console alerts for high-value opportunities

Usage:
    python -m book_opportunities.ingest
    python -m book_opportunities.ingest --sources reddit,firecrawl-search,hn
    python -m book_opportunities.ingest --categories tcg,digital-products
    python -m book_opportunities.ingest --min-score 70
"""

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any
from xml.etree import ElementTree as ET

PROJECT_ROOT = Path(__file__).resolve().parents[2]

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

def _require_httpx():
    try:
        import httpx as _httpx
    except ImportError:
        print("ERROR: httpx required for live ingestion. Install: pip install httpx")
        raise SystemExit(1)
    return _httpx


class _HttpxProxy:
    def __getattr__(self, name):
        return getattr(_require_httpx(), name)


httpx = _HttpxProxy()

try:
    from bs4 import BeautifulSoup
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "beautifulsoup4", "-q"])
    from bs4 import BeautifulSoup

OUTPUT_DIR = PROJECT_ROOT / "data"

# Free-only policy / local store (API-compatible local data)
try:
    from . import config as _dp_config
    from .policy import evaluate_provider, require_provider, require_external_writes, external_writes_allowed
    from .store import seed_fixtures
except ImportError:  # pragma: no cover
    _dp_config = None
    def evaluate_provider(name):
        class D:
            allowed = True
            status = "free"
            reason = ""
        return D()
    def require_provider(name):
        return evaluate_provider(name)
    def require_external_writes(action):
        return None
    def external_writes_allowed():
        return False
    def seed_fixtures(data_dir=None):
        return None

DB_PATH = OUTPUT_DIR / "opportunity_intelligence.db"
SOURCE_HEALTH_FILE = OUTPUT_DIR / "source_health.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# ── DuckDuckGo HTML search helper ────────────────────────────────
def ddg_search(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Search DuckDuckGo HTML endpoint (best-effort; unknown under free-only).
    Firecrawl fallback is blocked under free-only defaults."""
    decision = evaluate_provider("ddg_html")
    if not decision.allowed:
        print(f"    DDG HTML blocked ({decision.status}): {decision.reason}")
        return []
    for attempt in range(2):  # Only 2 attempts before fallback
        try:
            resp = httpx.get(
                "https://html.duckduckgo.com/html/",
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"},
                params={"q": query},
                timeout=15,
                follow_redirects=True,
            )
            if resp.status_code == 202:
                wait = 2 * (attempt + 1)
                print(f"    DDG rate-limited, waiting {wait}s (attempt {attempt+1}/2)")
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                return _firecrawl_fallback(query, limit)
            soup = BeautifulSoup(resp.text, "html.parser")
            results = []
            for result in soup.select(".result"):
                title_el = result.select_one(".result__title a, .result__a")
                snippet_el = result.select_one(".result__snippet")
                if title_el:
                    title = title_el.get_text(strip=True)
                    url = title_el.get("href", "")
                    if "uddg=" in url:
                        from urllib.parse import unquote, urlparse, parse_qs
                        parsed = parse_qs(urlparse(url).query)
                        url = unquote(parsed.get("uddg", [url])[0])
                    snippet = snippet_el.get_text(strip=True) if snippet_el else ""
                    results.append({"title": title, "url": url, "description": snippet})
                if len(results) >= limit:
                    break
            if results:
                return results
            return _firecrawl_fallback(query, limit)
        except Exception as e:
            err_str = str(e).lower()
            # Fast-fail on DNS/network errors — no point retrying
            if any(kw in err_str for kw in ['name resolution', 'dns', 'network is unreachable', 'no route']):
                return _firecrawl_fallback(query, limit)
            print(f"    Warning: DuckDuckGo search failed for '{query[:40]}': {e}")
            return _firecrawl_fallback(query, limit)
    return _firecrawl_fallback(query, limit)


def _firecrawl_fallback(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Fallback search via Firecrawl API when DuckDuckGo fails."""
    # FREE-ONLY GATE
    decision = evaluate_provider("firecrawl")
    if not decision.allowed:
        print(f"    Firecrawl blocked ({decision.status}): {decision.reason}")
        return []
    api_key = os.environ.get("FIRECRAWL_API_KEY", "")
    if not api_key:
        print("    Firecrawl skipped: missing FIRECRAWL_API_KEY")
        return []
    try:
        resp = httpx.post(
            "https://api.firecrawl.dev/v1/search",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"query": query, "limit": limit},
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"    Firecrawl fallback returned {resp.status_code}")
            return []
        data = resp.json()
        results = []
        for item in data.get("data", []):
            title = item.get("title", "") or item.get("metadata", {}).get("title", "")
            url = item.get("url", "") or item.get("metadata", {}).get("sourceURL", "")
            desc = item.get("description", "") or item.get("markdown", "")[:200]
            if title and url:
                results.append({"title": title, "url": url, "description": desc})
            if len(results) >= limit:
                break
        if results:
            print(f"    Firecrawl fallback: {len(results)} results for '{query[:50]}'")
        return results
    except Exception as e:
        print(f"    Firecrawl fallback failed: {e}")
        return []


# ── Opportunity categories ──────────────────────────────────────
OPPORTUNITY_CATEGORIES = {
    "tcg": {
        "name": "TCG Cards",
        "keywords": ["pokemon", "one piece", "magic the gathering", "yu-gi-oh",
                      "trading cards", "tcg", "booster box", "card game"],
    },
    "digital-products": {
        "name": "Digital Products",
        "keywords": ["printable", "template", "digital download", "planner",
                      "svg", "clipart", "digital product", "notion template"],
    },
    "ai-content": {
        "name": "AI-Generated Content",
        "keywords": ["ai art", "ai music", "ai generated", "midjourney",
                      "stable diffusion", "ai video", "ai tool", "ai agent",
                      "ai saas", "ai writing", "ai image"],
    },
    "print-on-demand": {
        "name": "Print on Demand",
        "keywords": ["t-shirt", "mug", "poster", "sublimation", "merch",
                      "custom print", "print on demand", "pod"],
    },
    "courses": {
        "name": "Online Courses",
        "keywords": ["course", "tutorial", "masterclass", "teaching",
                      "online course", "udemy", "skillshare"],
    },
    "freelance-services": {
        "name": "Freelance Services",
        "keywords": ["freelance", "service", "consulting", "agency",
                      "contract work", "fiverr", "upwork"],
    },
    "trending-products": {
        "name": "Trending Physical Products",
        "keywords": ["viral", "trending", "bestseller", "hot product",
                      "dropshipping", "ecommerce", "tiktok shop"],
    },
    "content-creation": {
        "name": "Content Creation",
        "keywords": ["youtube", "tiktok", "podcast", "newsletter",
                      "blog", "content creator", "influencer"],
    },
}


# ── Source: Reddit via DuckDuckGo ───────────────────────────────
def scrape_reddit_rss() -> List[Dict[str, Any]]:
    """Search Reddit via DuckDuckGo (bypasses Reddit 403/429)."""
    opportunities = []
    searches = [
        ("site:reddit.com side hustle money 2026", "freelance-services"),
        ("site:reddit.com passive income TCG AI 2026", "trending-products"),
        ("site:reddit.com print on demand Etsy digital products", "digital-products"),
    ]

    for query, default_cat in searches:
        results = ddg_search(query, limit=5)
        for r in results:
            title = r.get("title", "")
            url = r.get("url", "")
            snippet = r.get("description", "")
            if title and "reddit.com" in url:
                score = 70
                title_lower = title.lower()
                if any(kw in title_lower for kw in ["money", "income", "profit", "sell", "revenue"]):
                    score = 82
                if any(kw in title_lower for kw in ["ai", "automation", "passive"]):
                    score = 85
                opportunities.append({
                    "title": title[:120],
                    "category": default_cat,
                    "source": "Reddit",
                    "price": "",
                    "url": url,
                    "trend_score": score,
                    "competition_level": "varies",
                    "notes": snippet[:100],
                })
        time.sleep(1)

    print(f"    Found {len(opportunities)} Reddit opportunities via DuckDuckGo")
    return opportunities


# ── Source: Hacker News API ─────────────────────────────────────
def scrape_hackernews() -> List[Dict[str, Any]]:
    """Scrape Hacker News top stories for build opportunities."""
    opportunities = []
    try:
        require_provider("hacker_news_public")
        resp = httpx.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json",
            headers=HEADERS, timeout=30,
        )
        resp.raise_for_status()
        top_ids = resp.json()[:30]

        for story_id in top_ids:
            try:
                s_resp = httpx.get(
                    f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json",
                    headers=HEADERS, timeout=10,
                )
                story = s_resp.json()
                if not story or story.get("type") != "story":
                    continue

                title = story.get("title", "")
                score = story.get("score", 0)
                url = story.get("url", "")
                title_lower = title.lower()

                # Only include opportunity-related stories
                opp_keywords = [
                    "launch", "show hn", "startup", "saas", "ai", "tool",
                    "product", "revenue", "money", "business", "side project",
                    "open source", "framework", "platform", "marketplace",
                    "generate", "automate", "build",
                ]
                if score > 50 and any(kw in title_lower for kw in opp_keywords):
                    opportunities.append({
                        "title": title[:120],
                        "category": "ai-content" if "ai" in title_lower else "trending-products",
                        "source": "Hacker News",
                        "price": "",
                        "url": url or f"https://news.ycombinator.com/item?id={story_id}",
                        "trend_score": min(95, 50 + score // 20),
                        "competition_level": "medium",
                        "notes": f"HN story with {score} points",
                    })
            except Exception:
                continue

        print(f"    Found {len(opportunities)} HN opportunities")

    except Exception as e:
        print(f"  Warning: HN scrape failed: {e}")

    return opportunities


# ── Source: GitHub Trending ─────────────────────────────────────
def scrape_github_trending() -> List[Dict[str, Any]]:
    """Scrape GitHub trending repos for build/tool opportunities."""
    opportunities = []
    try:
        require_provider("github_public")
        # Look for repos created in the last 7 days with growing stars
        from datetime import timedelta
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        resp = httpx.get(
            "https://api.github.com/search/repositories",
            headers={**HEADERS, "Accept": "application/vnd.github.v3+json"},
            params={
                "q": f"created:>{week_ago} stars:>5",
                "sort": "stars",
                "order": "desc",
                "per_page": 30,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        for repo in data.get("items", [])[:20]:
            name = repo.get("full_name", "")
            desc = repo.get("description", "") or ""
            stars = repo.get("stargazers_count", 0)
            url = repo.get("html_url", "")
            topics = repo.get("topics", [])
            combined = f"{name} {desc} {' '.join(topics)}".lower()

            if stars > 5 and any(kw in combined for kw in [
                "ai", "agent", "saas", "tool", "bot", "automation",
                "scraper", "api", "marketplace", "generator", "money",
                "ecommerce", "finance", "productivity", "llm", "gpt",
            ]):
                opportunities.append({
                    "title": f"{name}: {desc[:80]}",
                    "category": "ai-content" if "ai" in combined else "trending-products",
                    "source": "GitHub Trending",
                    "price": "",
                    "url": url,
                    "trend_score": min(90, 50 + stars // 5),
                    "competition_level": "low",
                    "notes": f"New repo with {stars} stars today",
                })

        print(f"    Found {len(opportunities)} GitHub trending repos")

    except Exception as e:
        print(f"  Warning: GitHub trending failed: {e}")

    return opportunities


# ── Source: DuckDuckGo Search (multi-query) ─────────────────────
def scrape_firecrawl_search() -> List[Dict[str, Any]]:
    """Use DuckDuckGo search to find trending opportunities across the web."""
    opportunities = []

    queries = [
        ("trending TCG cards most valuable 2026", "tcg"),
        ("best AI tools make money 2026", "ai-content"),
        ("trending digital products sell Etsy 2026", "digital-products"),
        ("print on demand trending niches 2026", "print-on-demand"),
        ("most profitable side hustles 2026", "freelance-services"),
        ("viral products TikTok shop dropshipping 2026", "trending-products"),
    ]

    for query, category in queries:
        results = ddg_search(query, limit=5)
        for r in results:
            title = r.get("title", "")
            url = r.get("url", "")
            desc = r.get("description", "")

            if title:
                opportunities.append({
                    "title": title[:120],
                    "category": category,
                    "source": "Web Search",
                    "price": "",
                    "url": url,
                    "trend_score": 75,
                    "competition_level": "medium",
                    "notes": f"Query: {query[:60]} | {desc[:80]}",
                })
        time.sleep(1)

    print(f"    Found {len(opportunities)} web search results")
    return opportunities


# ── Source: Etsy trending via DuckDuckGo ────────────────────────
def scrape_etsy_firecrawl() -> List[Dict[str, Any]]:
    """Search Etsy trending via DuckDuckGo."""
    opportunities = []
    queries = [
        "site:etsy.com trending best seller 2026",
        "site:etsy.com most popular digital download",
        "site:etsy.com trending sublimation design",
    ]
    for query in queries:
        results = ddg_search(query, limit=8)
        for r in results:
            title = r.get("title", "")
            url = r.get("url", "")
            if title and "etsy.com" in url:
                opportunities.append({
                    "title": title[:120],
                    "category": "digital-products",
                    "source": "Etsy",
                    "price": "",
                    "url": url,
                    "trend_score": 72,
                    "competition_level": "medium",
                    "notes": "Etsy trending listing",
                })
        time.sleep(1)
    print(f"    Found {len(opportunities)} Etsy trending items")
    return opportunities


# ── Source: eBay trending via DuckDuckGo ────────────────────────
def scrape_ebay_firecrawl() -> List[Dict[str, Any]]:
    """Search eBay trending deals via DuckDuckGo."""
    opportunities = []
    queries = [
        "site:ebay.com trending deals 2026",
        "site:ebay.com TCG booster box best price",
        "site:ebay.com hot selling collectibles",
    ]
    for query in queries:
        results = ddg_search(query, limit=8)
        for r in results:
            title = r.get("title", "")
            url = r.get("url", "")
            if title and "ebay.com" in url:
                import re
                price = ""
                price_match = re.search(r'\$[\d,]+\.?\d*', title)
                if price_match:
                    price = price_match.group()
                opportunities.append({
                    "title": title[:120],
                    "category": "trending-products",
                    "source": "eBay",
                    "price": price,
                    "url": url,
                    "trend_score": 72,
                    "competition_level": "medium",
                    "notes": "eBay trending deal",
                })
        time.sleep(1)
    print(f"    Found {len(opportunities)} eBay trending items")
    return opportunities


# ── Source: ProductHunt via DuckDuckGo ──────────────────────────
def scrape_producthunt_rss() -> List[Dict[str, Any]]:
    """Search ProductHunt via DuckDuckGo."""
    opportunities = []
    results = ddg_search("site:producthunt.com new launch 2026", limit=15)
    for r in results:
        title = r.get("title", "")
        url = r.get("url", "")
        desc = r.get("description", "")
        if title and "producthunt.com" in url:
            opportunities.append({
                "title": title[:120],
                "category": "ai-content",
                "source": "ProductHunt",
                "price": "",
                "url": url,
                "trend_score": 70,
                "competition_level": "medium",
                "notes": desc[:100],
            })
    print(f"    Found {len(opportunities)} ProductHunt launches")
    return opportunities


# ── Source: TikTok trending via DuckDuckGo ──────────────────────
def scrape_tiktok_trending() -> List[Dict[str, Any]]:
    """Search TikTok trending products and money-making via DuckDuckGo."""
    opportunities = []
    queries = [
        ("site:tiktok.com viral product 2026", "trending-products"),
        ("TikTok shop best selling products 2026", "trending-products"),
        ("TikTok trending side hustle money 2026", "freelance-services"),
    ]
    for query, default_cat in queries:
        results = ddg_search(query, limit=6)
        for r in results:
            title = r.get("title", "")
            url = r.get("url", "")
            desc = r.get("description", "")
            if title:
                opportunities.append({
                    "title": title[:120],
                    "category": default_cat,
                    "source": "TikTok",
                    "price": "",
                    "url": url,
                    "trend_score": 74,
                    "competition_level": "medium",
                    "notes": desc[:100],
                })
        time.sleep(1)
    print(f"    Found {len(opportunities)} TikTok trending items")
    return opportunities


# ── Source: Amazon movers & shakers via DuckDuckGo ──────────────
def scrape_amazon_trending() -> List[Dict[str, Any]]:
    """Search Amazon trending/best-selling products via DuckDuckGo."""
    opportunities = []
    queries = [
        ("site:amazon.com movers shakers best seller 2026", "trending-products"),
        ("Amazon trending products dropshipping 2026", "trending-products"),
        ("Amazon digital products Kindle bestseller 2026", "digital-products"),
    ]
    for query, default_cat in queries:
        results = ddg_search(query, limit=6)
        for r in results:
            title = r.get("title", "")
            url = r.get("url", "")
            desc = r.get("description", "")
            if title:
                import re
                price = ""
                price_match = re.search(r'\$[\d,]+\.?\d*', title + " " + desc)
                if price_match:
                    price = price_match.group()
                opportunities.append({
                    "title": title[:120],
                    "category": default_cat,
                    "source": "Amazon",
                    "price": price,
                    "url": url,
                    "trend_score": 72,
                    "competition_level": "high",
                    "notes": desc[:100],
                })
        time.sleep(1)
    print(f"    Found {len(opportunities)} Amazon trending items")
    return opportunities



# ── Categorize & Score ──────────────────────────────────────────
def categorize_opportunity(opportunity: Dict[str, Any]) -> str:
    """Categorize opportunity based on title and keywords."""
    title_lower = opportunity.get('title', '').lower()
    notes_lower = opportunity.get('notes', '').lower()
    combined = f"{title_lower} {notes_lower}"

    best_match = None
    best_count = 0

    for category, config in OPPORTUNITY_CATEGORIES.items():
        count = sum(1 for kw in config['keywords'] if kw in combined)
        if count > best_count:
            best_count = count
            best_match = category

    return best_match or opportunity.get("category", "trending-products")


def score_opportunity(opportunity: Dict[str, Any]) -> int:
    """Score opportunity based on multiple factors."""
    score = opportunity.get('trend_score', 50)

    title_lower = opportunity.get('title', '').lower()

    # Boost for AI-related
    if 'ai' in title_lower:
        score += 10

    # Boost for money-related
    if any(kw in title_lower for kw in ['money', 'revenue', 'profit', 'income', 'sell']):
        score += 5

    # Boost for low competition
    if opportunity.get('competition_level') == 'low':
        score += 10
    elif opportunity.get('competition_level') == 'high':
        score -= 10

    return min(100, max(0, score))


# ── Save / Load ─────────────────────────────────────────────────
def save_opportunities(opportunities: List[Dict[str, Any]]):
    """Save latest opportunity snapshot."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filepath = OUTPUT_DIR / "money_opportunities.csv"

    fieldnames = [
        "title", "category", "source", "price", "url",
        "trend_score", "competition_level", "notes", "scraped_at",
        "score_delta", "trend_direction"
    ]

    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for opp in opportunities:
            opp['scraped_at'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            writer.writerow(opp)

    print(f"  Saved {len(opportunities)} opportunities to {filepath}")


def append_history(opportunities: List[Dict[str, Any]]):
    """Append to history CSV."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filepath = OUTPUT_DIR / "money_opportunities_history.csv"

    fieldnames = [
        "title", "category", "source", "price", "url",
        "trend_score", "competition_level", "notes", "scraped_at",
        "score_delta", "trend_direction"
    ]

    file_exists = filepath.exists()

    with open(filepath, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        for opp in opportunities:
            opp['scraped_at'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            writer.writerow(opp)

    print(f"  Appended {len(opportunities)} rows to history")


def load_previous_urls() -> set:
    """Load previously seen opportunity URLs."""
    history_file = OUTPUT_DIR / "money_opportunities_history.csv"
    urls = set()
    if history_file.exists():
        with open(history_file, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("url"):
                    urls.add(row["url"])
    return urls


# ── Source Health Tracking ──────────────────────────────────────
def track_source_health(source: str, status: str, items: int, duration: float, error: str = None) -> Dict[str, Any]:
    """Track source health metrics."""
    return {
        "source": source,
        "status": status,  # ok, error, timeout
        "items": items,
        "duration": round(duration, 2),
        "error": error,
        "timestamp": datetime.now().isoformat()
    }


def save_source_health(health_records: List[Dict[str, Any]]):
    """Save source health report to JSON."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(SOURCE_HEALTH_FILE, 'w') as f:
        json.dump(health_records, f, indent=2)
    print(f"  Source health saved to {SOURCE_HEALTH_FILE.name}")


def print_source_health_report(health_records: List[Dict[str, Any]]):
    """Print source health summary."""
    print(f"\n{'='*60}")
    print(f"  SOURCE HEALTH REPORT")
    print(f"{'='*60}")

    ok_sources = [h for h in health_records if h['status'] == 'ok']
    err_sources = [h for h in health_records if h['status'] in ('error', 'timeout')]

    if ok_sources:
        print(f"\n  ✓ HEALTHY ({len(ok_sources)}):")
        for h in ok_sources:
            print(f"    {h['source']:20s} | {h['items']:3d} items | {h['duration']:5.2f}s")

    if err_sources:
        print(f"\n  ✗ FAILED ({len(err_sources)}):")
        for h in err_sources:
            err_msg = h.get('error', 'Unknown error')[:50]
            print(f"    {h['source']:20s} | {h['status']:7s} | {h['duration']:5.2f}s | {err_msg}")

    total_items = sum(h['items'] for h in health_records)
    total_time = sum(h['duration'] for h in health_records)
    print(f"\n  Total: {total_items} items from {len(ok_sources)}/{len(health_records)} sources in {total_time:.2f}s")


def generate_dashboard_json(health_records: List[Dict[str, Any]], opportunities: List[Dict[str, Any]], new_count: int):
    """Update scraper_dashboard.json with opportunities data.

    Merges opportunity scraper health into the existing dashboard JSON
    so the morning digest can show opportunity highlights.
    """
    dashboard_path = OUTPUT_DIR / "scraper_dashboard.json"

    # Load existing dashboard
    dashboard = {}
    if dashboard_path.exists():
        try:
            with open(dashboard_path) as f:
                dashboard = json.load(f)
        except Exception:
            dashboard = {}

    # Ensure 'sources' key exists
    if "sources" not in dashboard:
        dashboard["sources"] = {}

    # Update opportunities section
    ok_count = sum(1 for h in health_records if h['status'] == 'ok')
    total_count = len(health_records)

    dashboard["sources"]["opportunities"] = {
        "name": "Money Opportunities",
        "icon": "💰",
        "rows": len(opportunities),
        "new_items": new_count,
        "last_updated": "just now",
        "status": "active" if ok_count > 0 else "degraded",
        "health": {
            "ok": ok_count,
            "failed": total_count - ok_count,
            "total_sources": total_count,
            "sources": {h['source']: {"status": h['status'], "items": h['items'], "duration": h['duration']} for h in health_records}
        },
        "top_opportunities": [
            {
                "title": o.get('title', '')[:60],
                "score": o.get('trend_score', 0),
                "category": o.get('category', ''),
                "source": o.get('source', ''),
                "url": o.get('url', '')[:100],
                "direction": o.get('trend_direction', 'stable'),
            }
            for o in opportunities[:5]
        ],
    }

    # Update generated_at timestamp
    dashboard["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Save
    dashboard_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dashboard_path, 'w') as f:
        json.dump(dashboard, f, indent=2)
    print(f"  Dashboard updated: {dashboard_path.name}")


# ── Opportunity Action Tracker ──────────────────────────────────
def init_action_tracker():
    """Initialize opportunity_actions table if not exists."""
    if not DB_PATH.exists():
        return False

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS opportunity_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL,
                title TEXT,
                source TEXT,
                category TEXT,
                trend_score INTEGER,
                action TEXT CHECK(action IN ('act_on', 'skip', 'monitor')) DEFAULT 'monitor',
                action_notes TEXT,
                revenue_actual REAL DEFAULT 0,
                revenue_expected REAL DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"  Warning: Could not init action tracker: {e}")
        return False


def track_action(url: str, action: str, title: str = None, source: str = None,
                 category: str = None, score: int = None, notes: str = None):
    """Track action for an opportunity (act_on/skip/monitor)."""
    if not DB_PATH.exists():
        print(f"  Warning: DB not found at {DB_PATH}")
        return False

    try:
        conn = sqlite3.connect(DB_PATH)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Check if exists
        existing = conn.execute(
            "SELECT id FROM opportunity_actions WHERE url = ?", (url,)
        ).fetchone()

        if existing:
            conn.execute("""
                UPDATE opportunity_actions SET
                    action = ?, action_notes = ?, updated_at = ?
                WHERE url = ?
            """, (action, notes, now, url))
        else:
            conn.execute("""
                INSERT INTO opportunity_actions
                (url, title, source, category, trend_score, action, action_notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (url, title, source, category, score, action, notes, now, now))

        conn.commit()
        conn.close()
        print(f"  Tracked action '{action}' for: {title[:50] if title else url[:50]}")
        return True
    except Exception as e:
        print(f"  Warning: Could not track action: {e}")
        return False


def track_revenue(url: str, actual: float = None, expected: float = None):
    """Track revenue for an opportunity."""
    if not DB_PATH.exists():
        print(f"  Warning: DB not found at {DB_PATH}")
        return False

    try:
        conn = sqlite3.connect(DB_PATH)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        updates = []
        params = []
        if actual is not None:
            updates.append("revenue_actual = ?")
            params.append(actual)
        if expected is not None:
            updates.append("revenue_expected = ?")
            params.append(expected)

        if not updates:
            print("  Warning: No revenue values provided")
            return False

        updates.append("updated_at = ?")
        params.append(now)
        params.append(url)

        conn.execute(f"""
            UPDATE opportunity_actions SET {', '.join(updates)} WHERE url = ?
        """, params)
        conn.commit()
        conn.close()
        print(f"  Updated revenue for: {url[:50]}")
        return True
    except Exception as e:
        print(f"  Warning: Could not track revenue: {e}")
        return False


def list_actions(action_filter: str = None):
    """List tracked actions, optionally filtered by action type."""
    if not DB_PATH.exists():
        print(f"  Warning: DB not found at {DB_PATH}")
        return []

    try:
        conn = sqlite3.connect(DB_PATH)

        if action_filter:
            rows = conn.execute("""
                SELECT url, title, source, category, trend_score, action,
                       action_notes, revenue_actual, revenue_expected, created_at
                FROM opportunity_actions
                WHERE action = ?
                ORDER BY trend_score DESC
            """, (action_filter,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT url, title, source, category, trend_score, action,
                       action_notes, revenue_actual, revenue_expected, created_at
                FROM opportunity_actions
                ORDER BY trend_score DESC
            """).fetchall()

        conn.close()

        if not rows:
            print("  No tracked actions found")
            return []

        print(f"\n{'='*80}")
        print(f"  TRACKED ACTIONS ({len(rows)})")
        print(f"{'='*80}")

        for row in rows:
            url, title, source, category, score, action, notes, rev_actual, rev_expected, created = row
            action_icon = {'act_on': '✓', 'skip': '✗', 'monitor': '⚲'}.get(action, '?')
            score_str = f"{score:3d}" if score is not None else "  N/A"
            title_str = (title or "Untitled")[:60]
            source_str = (source or "Unknown")[:20]
            category_str = (category or "Uncategorized")[:20]
            created_str = (created or "")[:10]
            print(f"\n  {action_icon} [{action:7s}] Score:{score_str} | {title_str}")
            print(f"    Source: {source_str:20s} | Category: {category_str:20s} | Added: {created_str}")
            if rev_actual or rev_expected:
                print(f"    Revenue: ${rev_actual or 0:.2f} actual / ${rev_expected or 0:.2f} expected")
            if notes:
                print(f"    Notes: {notes[:60]}")
            print(f"    URL: {url[:70]}")

        return rows
    except Exception as e:
        print(f"  Warning: Could not list actions: {e}")
        return []


# ── Category → DB type mapping ─────────────────────────────────
CATEGORY_TO_TYPE = {
    "ai-content": "ai_automation",
    "digital-products": "product",
    "tcg": "product",
    "print-on-demand": "product",
    "courses": "content",
    "freelance-services": "service",
    "trending-products": "product",
    "content-creation": "content",
}


def save_to_db(opportunities: List[Dict[str, Any]]) -> int:
    """Save opportunities to the SQLite opportunities table.

    Maps scraper fields to the DB schema:
    - name, description, type, tier, status
    - pain_level, budget_level, ai_leverage_level, competition_level
    - total_score, target_market, revenue_model, price_range, notes

    Returns number of new rows inserted.
    """
    if not DB_PATH.exists():
        print(f"  Warning: DB not found at {DB_PATH}, skipping DB save")
        return 0

    try:
        conn = sqlite3.connect(DB_PATH)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        inserted = 0

        for opp in opportunities:
            url = opp.get("url", "")
            title = opp.get("title", "")[:200]
            category = opp.get("category", "trending-products")
            score = opp.get("trend_score", 50)
            source = opp.get("source", "")
            price = opp.get("price", "")
            notes = opp.get("notes", "")
            competition = opp.get("competition_level", "medium")

            # Map category to DB type
            opp_type = CATEGORY_TO_TYPE.get(category, "other")

            # Map score to tier: 80+ = 1 (enter now), 60-79 = 2 (niche), <60 = 3 (coming soon)
            if score >= 80:
                tier = 1
            elif score >= 60:
                tier = 2
            else:
                tier = 3

            # Map competition level to 1-5 scale
            comp_map = {"low": 2, "varies": 3, "medium": 3, "high": 4}
            comp_level = comp_map.get(competition, 3)

            # Derive scoring fields from trend_score
            pain = min(5, max(1, score // 20))
            budget = min(5, max(1, (score + 10) // 20))
            ai_lev = 5 if "ai" in title.lower() else 3
            dist = 3
            fit = min(5, max(1, score // 20))

            # Check if already exists (by name + target_market combo)
            existing = conn.execute(
                "SELECT id FROM opportunities WHERE name = ? AND target_market = ?",
                (title, category),
            ).fetchone()

            if existing:
                # Update existing record
                conn.execute("""
                    UPDATE opportunities SET
                        total_score = ?, tier = ?, competition_level = ?,
                        ai_leverage_level = ?, pain_level = ?, budget_level = ?,
                        notes = ?, price_range = ?, updated_at = ?
                    WHERE id = ?
                """, (score, tier, comp_level, ai_lev, pain, budget,
                      f"{source}: {notes}", price, now, existing[0]))
            else:
                conn.execute("""
                    INSERT INTO opportunities
                    (name, description, type, tier, status,
                     pain_level, budget_level, frequency_level,
                     ai_leverage_level, competition_level, distribution_level, fit_level,
                     total_score, target_market, revenue_model, price_range,
                     window_status, notes, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'researching',
                            ?, ?, 3,
                            ?, ?, ?, ?,
                            ?, ?, ?, ?,
                            ?, ?, ?, ?)
                """, (
                    title, f"{source}: {notes[:200]}", opp_type, tier,
                    pain, budget,
                    ai_lev, comp_level, dist, fit,
                    score, category, source, price,
                    "open" if score >= 70 else "opening",
                    f"URL: {url} | {notes[:200]}",
                    now, now,
                ))
                inserted += 1

        conn.commit()
        conn.close()
        print(f"  Saved {inserted} new opportunities to DB ({len(opportunities)} total processed)")
        return inserted

    except Exception as e:
        print(f"  Warning: DB save failed: {e}")
        return 0


# ── Source: TCG card prices (CardKingdom) ──────────────────────
def scrape_tcg_prices() -> List[Dict[str, Any]]:
    """Scrape TCG card price data from CardKingdom via DuckDuckGo."""
    opportunities = []
    queries = [
        ("site:cardkingdom.com singles most wanted 2026", "tcg"),
        ("pokemon TCG most valuable cards price 2026", "tcg"),
        ("one piece TCG card prices trending 2026", "tcg"),
    ]
    for query, default_cat in queries:
        results = ddg_search(query, limit=6)
        for r in results:
            title = r.get("title", "")
            url = r.get("url", "")
            desc = r.get("description", "")
            if title:
                import re
                price = ""
                price_match = re.search(r'\$[\d,]+\.?\d*', title + " " + desc)
                if price_match:
                    price = price_match.group()
                opportunities.append({
                    "title": title[:120],
                    "category": default_cat,
                    "source": "TCG Prices",
                    "price": price,
                    "url": url,
                    "trend_score": 78 if price else 70,
                    "competition_level": "medium",
                    "notes": desc[:100],
                })
        time.sleep(1)
    print(f"    Found {len(opportunities)} TCG price opportunities")
    return opportunities


# ── Source: Etsy sold listings (real market data) ───────────────
def scrape_etsy_sold() -> List[Dict[str, Any]]:
    """Search Etsy recently sold listings for real market signals."""
    opportunities = []
    queries = [
        ("site:etsy.com \"sold\" digital download best seller 2026", "digital-products"),
        ("site:etsy.com \"sold\" SVG bundle sublimation trending", "print-on-demand"),
        ("site:etsy.com \"sold\" notion template planner 2026", "digital-products"),
    ]
    for query, default_cat in queries:
        results = ddg_search(query, limit=6)
        for r in results:
            title = r.get("title", "")
            url = r.get("url", "")
            desc = r.get("description", "")
            if title and "etsy.com" in url:
                import re
                price = ""
                price_match = re.search(r'\$[\d,]+\.?\d*', title + " " + desc)
                if price_match:
                    price = price_match.group()
                opportunities.append({
                    "title": title[:120],
                    "category": default_cat,
                    "source": "Etsy Sold",
                    "price": price,
                    "url": url,
                    "trend_score": 80 if price else 72,
                    "competition_level": "medium",
                    "notes": f"Sold listing | {desc[:80]}",
                })
        time.sleep(1)
    print(f"    Found {len(opportunities)} Etsy sold listings")
    return opportunities


# ── Source: Amazon BSR tracking ─────────────────────────────────
def scrape_amazon_bsr() -> List[Dict[str, Any]]:
    """Track Amazon Best Sellers Rank movers via DuckDuckGo."""
    opportunities = []
    queries = [
        ("Amazon best sellers rank movers shakers electronics 2026", "trending-products"),
        ("Amazon BSR biggest gainers home kitchen 2026", "trending-products"),
        ("Amazon new releases best seller rank trending 2026", "trending-products"),
    ]
    for query, default_cat in queries:
        results = ddg_search(query, limit=5)
        for r in results:
            title = r.get("title", "")
            url = r.get("url", "")
            desc = r.get("description", "")
            if title:
                import re
                price = ""
                price_match = re.search(r'\$[\d,]+\.?\d*', title + " " + desc)
                if price_match:
                    price = price_match.group()
                opportunities.append({
                    "title": title[:120],
                    "category": default_cat,
                    "source": "Amazon BSR",
                    "price": price,
                    "url": url,
                    "trend_score": 76 if price else 70,
                    "competition_level": "high",
                    "notes": f"BSR mover | {desc[:80]}",
                })
        time.sleep(1)
    print(f"    Found {len(opportunities)} Amazon BSR movers")
    return opportunities


# ── Deduplication + History Analysis ────────────────────────────
def fuzzy_title_match(title_a: str, title_b: str) -> bool:
    """Check if two titles are similar (>60% word overlap)."""
    words_a = set(title_a.lower().split())
    words_b = set(title_b.lower().split())
    if not words_a or not words_b:
        return False
    overlap = len(words_a & words_b) / min(len(words_a), len(words_b))
    return overlap > 0.6


def deduplicate_cross_source(opportunities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicate across sources using URL + fuzzy title matching.

    When same opportunity appears in multiple sources:
    - Keep the highest score
    - Merge source info into notes
    - Track cross-source appearances
    """
    merged = []
    seen_titles = []  # (title, index_in_merged)

    for opp in opportunities:
        url = opp.get("url", "")
        title = opp.get("title", "")

        # Check URL match
        url_match = None
        for i, m in enumerate(merged):
            if m.get("url") == url and url:
                url_match = i
                break

        if url_match is not None:
            # Same URL — merge sources
            existing = merged[url_match]
            if opp["trend_score"] > existing["trend_score"]:
                old_source = existing["source"]
                merged[url_match] = opp.copy()
                merged[url_match]["notes"] = f"Cross-source: {old_source}, {opp['source']} | {opp.get('notes', '')}"
            else:
                existing["notes"] = f"Cross-source: {existing['source']}, {opp['source']} | {existing.get('notes', '')}"
            # Boost score for cross-source validation
            merged[url_match]["trend_score"] = min(100, merged[url_match]["trend_score"] + 3)
            continue

        # Check fuzzy title match
        title_match = None
        for st_title, st_idx in seen_titles:
            if fuzzy_title_match(title, st_title):
                title_match = st_idx
                break

        if title_match is not None:
            existing = merged[title_match]
            if opp["trend_score"] > existing["trend_score"]:
                old_source = existing["source"]
                merged[title_match] = opp.copy()
                merged[title_match]["notes"] = f"Cross-source: {old_source}, {opp['source']} | {opp.get('notes', '')}"
            else:
                existing["notes"] = f"Cross-source: {existing['source']}, {opp['source']} | {existing.get('notes', '')}"
            merged[title_match]["trend_score"] = min(100, merged[title_match]["trend_score"] + 3)
            continue

        seen_titles.append((title, len(merged)))
        merged.append(opp.copy())

    dupes_removed = len(opportunities) - len(merged)
    if dupes_removed > 0:
        print(f"  Deduplication: merged {dupes_removed} cross-source duplicates")
    return merged


def compute_score_deltas(opportunities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Compare current scores with previous run to compute deltas.

    Adds 'score_delta' and 'trend_direction' fields.
    """
    history_file = OUTPUT_DIR / "money_opportunities.csv"
    prev_scores = {}

    if history_file.exists():
        try:
            with open(history_file, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    title = row.get("title", "")
                    score = int(row.get("trend_score", 0))
                    if title:
                        prev_scores[title] = score
        except Exception:
            pass

    for opp in opportunities:
        title = opp.get("title", "")
        current = opp.get("trend_score", 0)
        prev = prev_scores.get(title, 0)
        delta = current - prev if prev else 0
        opp["score_delta"] = delta
        if delta > 5:
            opp["trend_direction"] = "rising"
        elif delta < -5:
            opp["trend_direction"] = "falling"
        else:
            opp["trend_direction"] = "stable"

    rising = sum(1 for o in opportunities if o.get("trend_direction") == "rising")
    falling = sum(1 for o in opportunities if o.get("trend_direction") == "falling")
    if rising or falling:
        print(f"  Score deltas: {rising} rising, {falling} falling, {len(opportunities) - rising - falling} stable")

    return opportunities


# ── Telegram Daily Digest (Action Layer) ────────────────────────
def send_telegram_digest(opportunities: List[Dict[str, Any]], new_count: int):
    """Send daily top-3 opportunities digest to Telegram.

    Uses TELEGRAM_TRADING_BOT_TOKEN + TELEGRAM_CHAT_ID.
    """
    bot_token = os.environ.get("TELEGRAM_TRADING_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        print("  Warning: Telegram credentials not set, skipping digest")
        return False

    # Pick top 3 by score, one per category if possible
    seen_cats = set()
    top3 = []
    for opp in opportunities:
        cat = opp.get("category", "")
        if cat not in seen_cats and opp.get("trend_score", 0) >= 70:
            top3.append(opp)
            seen_cats.add(cat)
            if len(top3) >= 3:
                break
    # Fill remaining slots if needed
    if len(top3) < 3:
        for opp in opportunities:
            if opp not in top3:
                top3.append(opp)
                if len(top3) >= 3:
                    break

    if not top3:
        print("  No opportunities worth digesting")
        return False

    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"💰 *Daily Opportunity Digest* ({today})", f"Total: {len(opportunities)} | New: {new_count}", ""]

    for i, opp in enumerate(top3, 1):
        score = opp.get("trend_score", 0)
        title = opp.get("title", "?")[:60]
        cat = opp.get("category", "?")
        source = opp.get("source", "?")
        delta = opp.get("score_delta", 0)
        direction = opp.get("trend_direction", "stable")
        url = opp.get("url", "")

        arrow = "🔺" if direction == "rising" else ("🔻" if direction == "falling" else "➖")
        delta_str = f"{arrow} {'+' if delta > 0 else ''}{delta}" if delta else ""

        lines.append(f"*{i}. [{score}]{delta_str} {title}*")
        lines.append(f"   📂 {cat} | 📡 {source}")
        if url:
            lines.append(f"   🔗 {url}")
        lines.append("")

    lines.append("_Run: python -m book_opportunities.ingest_")

    message = "\n".join(lines)

    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            print(f"  Telegram digest sent: {len(top3)} opportunities")
            return True
        else:
            # Retry without Markdown if parse fails
            resp2 = httpx.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": message.replace("*", "").replace("_", ""),
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            if resp2.status_code == 200:
                print(f"  Telegram digest sent (plain text fallback)")
                return True
            print(f"  Warning: Telegram send failed: {resp2.status_code}")
            return False
    except Exception as e:
        print(f"  Warning: Telegram digest failed: {e}")
        return False


# ── Todoist Integration ─────────────────────────────────────────
def push_to_todoist(opportunities: List[Dict[str, Any]]) -> int:
    """Push high-score opportunities (≥85) to Todoist as actionable tasks.

    Creates tasks in the '💼 Income' project with tags for category and source.
    Returns count of tasks created.
    """
    token = os.environ.get("TODOIST_API_TOKEN")
    if not token:
        print("  Warning: TODOIST_API_TOKEN not set, skipping Todoist sync")
        return 0

    # Filter high-score opportunities
    high_value = [o for o in opportunities if o.get("trend_score", 0) >= 85]
    if not high_value:
        print("  No opportunities with score ≥85 for Todoist")
        return 0

    # Limit to top 5 to avoid spam
    high_value = high_value[:5]

    base_url = "https://api.todoist.com/api/v1"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    created = 0
    for opp in high_value:
        title = opp.get("title", "?")[:80]
        score = opp.get("trend_score", 0)
        category = opp.get("category", "unknown")
        source = opp.get("source", "unknown")
        url = opp.get("url", "")
        notes = opp.get("notes", "")

        # Build task content
        content = f"💰 [{score}] {title}"
        description = f"Category: {category}\nSource: {source}\nScore: {score}\n\n{notes}\n\n{url}"

        # Create task via API
        payload = {
            "content": content,
            "description": description[:500],  # Todoist limit
            "labels": ["opportunity", category.lower().replace(" ", "-")[:20]],
            "priority": 4 if score >= 95 else 3,  # P1 if ≥95, P2 if ≥85
        }

        try:
            resp = httpx.post(
                f"{base_url}/tasks",
                headers=headers,
                json=payload,
                timeout=10,
            )
            if resp.status_code == 200:
                created += 1
            else:
                print(f"  Warning: Todoist task creation failed: {resp.status_code}")
        except Exception as e:
            print(f"  Warning: Todoist API error: {e}")

    print(f"  Created {created} Todoist tasks from high-score opportunities")
    return created


# ── Cron Setup Helper ───────────────────────────────────────────
def setup_cron():
    """Add daily cron job to system crontab if not already present."""
    import subprocess
    script_path = str(Path(__file__).resolve())
    python_path = sys.executable
    cron_cmd = f"0 10 * * * cd {PROJECT_ROOT} && {python_path} {script_path} --sources hn,github,firecrawl-search,etsy,ebay,tiktok,amazon,tcg-prices,etsy-sold,amazon-bsr --min-score 60 --send-digest --push-todoist >> {OUTPUT_DIR}/cron.log 2>&1"
    cron_marker = "scrape_money_opportunities"

    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        current_cron = result.stdout if result.returncode == 0 else ""

        if cron_marker in current_cron:
            print("  Cron job already configured")
            return

        new_cron = current_cron.rstrip("\n") + f"\n{cron_cmd}\n"
        proc = subprocess.run(["crontab", "-"], input=new_cron, capture_output=True, text=True)
        if proc.returncode == 0:
            print(f"  Cron job added: daily at 10:00 AM")
            print(f"  Command: {cron_cmd[:80]}...")
        else:
            print(f"  Warning: crontab update failed: {proc.stderr}")
    except FileNotFoundError:
        print("  Warning: crontab not available on this system")


# ── Main ────────────────────────────────────────────────────────
SOURCE_MAP = {
    "reddit": ("Reddit (via DuckDuckGo)", scrape_reddit_rss),
    "hn": ("Hacker News API", scrape_hackernews),
    "github": ("GitHub Trending", scrape_github_trending),
    "firecrawl-search": ("Web Search (DuckDuckGo)", scrape_firecrawl_search),
    "etsy": ("Etsy (via DuckDuckGo)", scrape_etsy_firecrawl),
    "ebay": ("eBay (via DuckDuckGo)", scrape_ebay_firecrawl),
    "producthunt": ("ProductHunt (via DuckDuckGo)", scrape_producthunt_rss),
    "tiktok": ("TikTok (via DuckDuckGo)", scrape_tiktok_trending),
    "amazon": ("Amazon (via DuckDuckGo)", scrape_amazon_trending),
    "tcg-prices": ("TCG Card Prices", scrape_tcg_prices),
    "etsy-sold": ("Etsy Sold Listings", scrape_etsy_sold),
    "amazon-bsr": ("Amazon BSR Movers", scrape_amazon_bsr),
}


def main():
    parser = argparse.ArgumentParser(description="Scrape money-making opportunities from multiple sources")
    parser.add_argument(
        "--sources",
        default="hn,github",
        help="Sources: reddit,hn,github,firecrawl-search,etsy,ebay,producthunt,tiktok,amazon,tcg-prices,etsy-sold,amazon-bsr"
    )
    parser.add_argument(
        "--categories",
        default="",
        help="Filter by categories: tcg,digital-products,ai-content,print-on-demand,courses,freelance-services,trending-products,content-creation"
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=0,
        help="Minimum trend score filter (0-100)"
    )
    parser.add_argument(
        "--output-dir",
        default=str(OUTPUT_DIR),
        help="Output directory"
    )
    parser.add_argument(
        "--send-digest",
        action="store_true",
        help="Send daily digest to Telegram"
    )
    parser.add_argument(
        "--setup-cron",
        action="store_true",
        help="Add daily cron job to system crontab"
    )
    parser.add_argument(
        "--push-todoist",
        action="store_true",
        help="Push high-score (≥85) opportunities to Todoist as tasks"
    )
    # Action tracker CLI
    parser.add_argument(
        "--track-action",
        nargs=3,
        metavar=("URL", "ACTION", "NOTES"),
        help="Track action for opportunity: --track-action <url> <act_on|skip|monitor> \"notes\""
    )
    parser.add_argument(
        "--track-revenue",
        nargs=3,
        metavar=("URL", "ACTUAL", "EXPECTED"),
        help="Track revenue: --track-revenue <url> <actual_amount> <expected_amount>"
    )
    parser.add_argument(
        "--list-actions",
        nargs="?",
        const="all",
        metavar="ACTION",
        help="List tracked actions (optionally filter: act_on, skip, monitor)"
    )
    parser.add_argument("--fixture", action="store_true", help="Load offline fixtures (no upstream)")
    parser.add_argument("--dry-run", action="store_true", help="Dry-run fixture mode")
    args = parser.parse_args()

    if getattr(args, "fixture", False) or getattr(args, "dry_run", False):
        out = Path(getattr(args, "output_dir", OUTPUT_DIR))
        out.mkdir(parents=True, exist_ok=True)
        if _dp_config is not None:
            _dp_config.DATA_DIR = out
        seed_fixtures(out)
        print("[" + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "] Fixture mode: seeded local data under " + str(out))
        print("  No upstream providers contacted. External writes disabled.")
        return

    # External writes are blocked under free-only defaults.
    if getattr(args, "send_digest", False):
        notify_telegram("digest requested")
        print("  --send-digest ignored under free-only defaults (external writes blocked).")
        return
    if getattr(args, "push_todoist", False):
        push_todoist("todoist push requested")
        print("  --push-todoist ignored under free-only defaults (external writes blocked).")
        return

    # Handle action tracker commands
    if args.track_action:
        url, action, notes = args.track_action
        if action not in ('act_on', 'skip', 'monitor'):
            print(f"  Error: action must be act_on, skip, or monitor (got: {action})")
            return
        init_action_tracker()
        track_action(url, action, notes=notes)
        return

    if args.track_revenue:
        url, actual, expected = args.track_revenue
        init_action_tracker()
        track_revenue(url, actual=float(actual), expected=float(expected))
        return

    if args.list_actions:
        filter_val = args.list_actions if args.list_actions != "all" else None
        list_actions(filter_val)
        return

    # Handle cron setup
    if args.setup_cron:
        setup_cron()
        return

    sources = [s.strip() for s in args.sources.split(",")]
    category_filter = [c.strip() for c in args.categories.split(",") if c.strip()] if args.categories else []

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Money Opportunity Finder")
    print(f"  Sources: {sources}")
    print(f"  Categories: {category_filter if category_filter else 'all'}")
    print(f"  Min score: {args.min_score}")

    all_opportunities = []
    health_records = []

    for source in sources:
        source_info = SOURCE_MAP.get(source)
        if not source_info:
            print(f"\n  Warning: Unknown source '{source}'")
            health_records.append(track_source_health(source, 'error', 0, 0, 'Unknown source'))
            continue
        name, scraper_fn = source_info
        print(f"\n  Scraping {name}...")

        # Track source health with timing and error handling
        start_time = time.time()
        try:
            opps = scraper_fn()
            duration = time.time() - start_time
            health_records.append(track_source_health(source, 'ok', len(opps), duration))
            all_opportunities.extend(opps)
            print(f"    ✓ {len(opps)} items in {duration:.2f}s")
        except Exception as e:
            duration = time.time() - start_time
            error_msg = str(e)[:100]
            status = 'timeout' if 'timeout' in error_msg.lower() else 'error'
            health_records.append(track_source_health(source, status, 0, duration, error_msg))
            print(f"    ✗ Failed ({status}): {error_msg}")

    # Categorize and score
    print(f"\n  Processing {len(all_opportunities)} raw opportunities...")

    for opp in all_opportunities:
        opp['category'] = categorize_opportunity(opp)
        opp['trend_score'] = score_opportunity(opp)

    # Filter by category
    if category_filter:
        all_opportunities = [o for o in all_opportunities if o['category'] in category_filter]

    # Filter by score
    if args.min_score > 0:
        all_opportunities = [o for o in all_opportunities if o['trend_score'] >= args.min_score]

    # Cross-source deduplication (URL + fuzzy title matching)
    unique_opportunities = deduplicate_cross_source(all_opportunities)

    # Compute score deltas from previous run
    unique_opportunities = compute_score_deltas(unique_opportunities)

    # Sort by trend score
    unique_opportunities.sort(key=lambda x: x['trend_score'], reverse=True)

    # Detect new opportunities (before saving, so we compare against previous state)
    previous_urls = load_previous_urls()
    new_opportunities = [o for o in unique_opportunities if o.get('url', '') not in previous_urls]

    # Save results
    save_opportunities(unique_opportunities)
    append_history(unique_opportunities)
    save_to_db(unique_opportunities)

    # Auto-track high-score opportunities (≥85) as 'monitor'
    high_score_opps = [o for o in unique_opportunities if o.get('trend_score', 0) >= 85]
    if high_score_opps and init_action_tracker():
        tracked = 0
        for opp in high_score_opps:
            url = opp.get('url', '')
            if url:
                track_action(
                    url=url,
                    action='monitor',
                    title=opp.get('title', ''),
                    source=opp.get('source', ''),
                    category=opp.get('category', ''),
                    score=opp.get('trend_score', 0),
                    notes=f"Auto-tracked: score {opp.get('trend_score', 0)}, trend {opp.get('trend_direction', 'stable')}"
                )
                tracked += 1
        if tracked:
            print(f"  Auto-tracked {tracked} high-score opportunities as 'monitor'")

    # Save and print source health report
    if health_records:
        save_source_health(health_records)
        print_source_health_report(health_records)

    # Update scraper dashboard JSON
    generate_dashboard_json(health_records, unique_opportunities, len(new_opportunities))

    # Print summary
    print(f"\n{'='*60}")
    print(f"  SUMMARY")
    print(f"{'='*60}")
    print(f"  Total opportunities: {len(unique_opportunities)}")
    print(f"  New opportunities: {len(new_opportunities)}")

    # Top opportunities by category
    print(f"\n  TOP OPPORTUNITIES BY CATEGORY:")
    for category, config in OPPORTUNITY_CATEGORIES.items():
        cat_opps = [o for o in unique_opportunities if o['category'] == category]
        if cat_opps:
            print(f"\n  {config['name']} ({len(cat_opps)} found):")
            for opp in cat_opps[:3]:
                delta = opp.get('score_delta', 0)
                delta_str = f" ({'+' if delta > 0 else ''}{delta})" if delta else ""
                print(f"    [{opp['trend_score']:3d}]{delta_str} {opp['title'][:50]:50s} | {opp['source']:20s}")

    # High-value opportunities alert
    high_value = [o for o in unique_opportunities if o['trend_score'] >= 80]
    if high_value:
        print(f"\n{'='*60}")
        print(f"  HIGH-VALUE OPPORTUNITIES ({len(high_value)})")
        print(f"{'='*60}")
        for opp in high_value[:10]:
            direction = opp.get('trend_direction', 'stable')
            arrow = " ↑" if direction == "rising" else (" ↓" if direction == "falling" else "")
            print(f"    [{opp['trend_score']:3d}]{arrow} {opp['title'][:50]:50s} | {opp['source']:20s}")

    # Send Telegram digest if requested
    if args.send_digest:
        print(f"\n  Sending Telegram digest...")
        send_telegram_digest(unique_opportunities, len(new_opportunities))

    # Push high-score opportunities to Todoist if requested
    if args.push_todoist:
        print(f"\n  Pushing to Todoist...")
        push_to_todoist(unique_opportunities)

    print(f"\n  Done.")


class MoneyOpportunityScraper:
    """Wrapper class for scheduler compatibility."""
    def __init__(self, sources=None, categories=None, min_score=0, send_digest=False, push_todoist=False, **kwargs):
        self.sources = sources or ["hn", "github", "firecrawl-search", "reddit", "etsy", "ebay", "producthunt", "tiktok", "amazon", "tcg-prices", "etsy-sold", "amazon-bsr"]
        self.categories = categories or []
        self.min_score = min_score
        self.send_digest = send_digest
        self.push_todoist = push_todoist

    async def run(self, **kwargs):
        if isinstance(self.sources, str):
            self.sources = [s.strip() for s in self.sources.split(",")]
        if isinstance(self.categories, str):
            self.categories = [c.strip() for c in self.categories.split(",") if c.strip()]

        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Money Opportunity Finder")
        print(f"  Sources: {self.sources}")
        print(f"  Categories: {self.categories if self.categories else 'all'}")
        print(f"  Min score: {self.min_score}")

        all_opportunities = []
        health_records = []

        for source in self.sources:
            source_info = SOURCE_MAP.get(source)
            if not source_info:
                print(f"\n  Warning: Unknown source '{source}'")
                health_records.append(track_source_health(source, 'error', 0, 0, 'Unknown source'))
                continue
            name, scraper_fn = source_info
            print(f"\n  Scraping {name}...")
            start_time = time.time()
            try:
                opps = scraper_fn()
                duration = time.time() - start_time
                health_records.append(track_source_health(source, 'ok', len(opps), duration))
                all_opportunities.extend(opps)
                print(f"    ✓ {len(opps)} items in {duration:.2f}s")
            except Exception as e:
                duration = time.time() - start_time
                error_msg = str(e)[:100]
                status = 'timeout' if 'timeout' in error_msg.lower() else 'error'
                health_records.append(track_source_health(source, status, 0, duration, error_msg))
                print(f"    ✗ Failed ({status}): {error_msg}")

        print(f"\n  Processing {len(all_opportunities)} raw opportunities...")
        for opp in all_opportunities:
            opp['category'] = categorize_opportunity(opp)
            opp['trend_score'] = score_opportunity(opp)

        if self.categories:
            all_opportunities = [o for o in all_opportunities if o['category'] in self.categories]
        if self.min_score > 0:
            all_opportunities = [o for o in all_opportunities if o['trend_score'] >= self.min_score]

        unique_opportunities = deduplicate_cross_source(all_opportunities)
        unique_opportunities = compute_score_deltas(unique_opportunities)
        unique_opportunities.sort(key=lambda x: x['trend_score'], reverse=True)

        previous_urls = load_previous_urls()
        new_opportunities = [o for o in unique_opportunities if o.get('url', '') not in previous_urls]

        save_opportunities(unique_opportunities)
        append_history(unique_opportunities)
        save_to_db(unique_opportunities)

        high_score_opps = [o for o in unique_opportunities if o.get('trend_score', 0) >= 85]
        if high_score_opps and init_action_tracker():
            tracked = 0
            for opp in high_score_opps:
                url = opp.get('url', '')
                if url:
                    track_action(
                        url=url, action='monitor',
                        title=opp.get('title', ''), source=opp.get('source', ''),
                        category=opp.get('category', ''), score=opp.get('trend_score', 0),
                        notes=f"Auto-tracked: score {opp.get('trend_score', 0)}, trend {opp.get('trend_direction', 'stable')}"
                    )
                    tracked += 1
            if tracked:
                print(f"  Auto-tracked {tracked} high-score opportunities as 'monitor'")

        if health_records:
            save_source_health(health_records)
            print_source_health_report(health_records)

        generate_dashboard_json(health_records, unique_opportunities, len(new_opportunities))

        print(f"\n{'='*60}")
        print(f"  SUMMARY")
        print(f"{'='*60}")
        print(f"  Total opportunities: {len(unique_opportunities)}")
        print(f"  New opportunities: {len(new_opportunities)}")

        if self.send_digest:
            print(f"\n  Sending Telegram digest...")
            send_telegram_digest(unique_opportunities, len(new_opportunities))

        if self.push_todoist:
            print(f"\n  Pushing to Todoist...")
            push_to_todoist(unique_opportunities)

        print(f"\n  Done.")
        return [{"source": "money_opportunities", "count": len(unique_opportunities)}]


if __name__ == "__main__":
    main()


# ── External writes (disabled under free-only defaults) ─────────
def notify_telegram(message: str) -> dict:
    """Blocked by default: external Telegram write."""
    try:
        require_external_writes("telegram")
    except PermissionError as e:
        print(f"  Telegram write blocked: {e}")
        return {"status": "blocked", "reason": str(e)}
    return {"status": "skipped", "reason": "no free-only telegram transport implemented"}


def push_todoist(task: str) -> dict:
    """Blocked by default: external Todoist write."""
    try:
        require_external_writes("todoist")
    except PermissionError as e:
        print(f"  Todoist write blocked: {e}")
        return {"status": "blocked", "reason": str(e)}
    return {"status": "skipped", "reason": "no free-only todoist transport implemented"}


def send_email_alert(subject: str, body: str) -> dict:
    """Blocked by default: external email write."""
    try:
        require_external_writes("email")
    except PermissionError as e:
        print(f"  Email write blocked: {e}")
        return {"status": "blocked", "reason": str(e)}
    return {"status": "skipped", "reason": "no free-only email transport implemented"}
