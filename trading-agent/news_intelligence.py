"""
news_intelligence.py — Advanced News Intelligence
Tier 1-4 classification, Trump direction detection, earnings whisper,
post-earnings gap protocol. Upgrades scanner.py keyword detection
into real market impact analysis.
"""
import logging
import re
from datetime import datetime, timezone
from typing import Optional
import requests
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

# ── Tier definitions ──────────────────────────────────────────────────────────

TIER_1_KEYWORDS = {
    "bullish": [
        "fed rate cut", "emergency rate cut", "fed pivot", "quantitative easing",
        "stimulus package", "trade deal signed", "ceasefire", "inflation falls",
        "cpi miss lower", "jobs beat", "gdp beat", "rate cut surprise"
    ],
    "bearish": [
        "fed rate hike surprise", "emergency hike", "bank failure", "default",
        "debt ceiling breach", "war declaration", "nuclear", "exchange hack",
        "crypto ban", "market circuit breaker", "flash crash", "cpi beat higher",
        "jobs miss", "gdp miss", "recession confirmed"
    ]
}

TIER_2_KEYWORDS = {
    "bullish": [
        "fed dovish", "rate cut expected", "inflation cooling", "strong earnings",
        "beat expectations", "raised guidance", "buyback", "dividend increase",
        "etf approval", "institutional buying", "trade talks progress",
        "deregulation", "tax cut"
    ],
    "bearish": [
        "fed hawkish", "rate hike expected", "inflation rising", "missed expectations",
        "lowered guidance", "layoffs", "bankruptcy", "sec investigation",
        "tariff", "trade war", "sanctions", "yield curve inversion",
        "credit downgrade", "margin call"
    ]
}

TIER_3_KEYWORDS = {
    "bullish": [
        "analyst upgrade", "price target raised", "strong demand", "market share gain",
        "partnership", "contract win", "product launch", "revenue beat"
    ],
    "bearish": [
        "analyst downgrade", "price target cut", "weak demand", "market share loss",
        "contract loss", "product recall", "revenue miss", "cost overrun"
    ]
}

# Trump-specific framework — direction matters, not just detection
TRUMP_BULLISH = [
    "tariff pause", "tariff rollback", "trade deal", "deregulation",
    "tax cut", "pro-crypto", "bitcoin reserve", "rate cut pressure",
    "market rally", "strong economy", "jobs record"
]

TRUMP_BEARISH = [
    "new tariff", "tariff increase", "trade war", "china tariff",
    "fed independence", "fed chair fire", "sanction", "import tax",
    "reciprocal tariff", "trade deficit anger"
]

# Assets specifically impacted by news
ASSET_KEYWORDS = {
    "BTC/USD": ["bitcoin", "btc", "crypto", "digital asset", "coinbase", "binance"],
    "ETH/USD": ["ethereum", "eth", "defi", "smart contract", "layer 2"],
    "NVDA": ["nvidia", "nvda", "gpu", "ai chip", "data center", "cuda", "blackwell"],
    "TSLA": ["tesla", "tsla", "elon", "musk", "ev", "electric vehicle", "cybertruck"],
    "META": ["meta", "facebook", "instagram", "zuckerberg", "metaverse", "threads"],
    "GOOGL": ["google", "alphabet", "googl", "gemini", "search", "youtube", "antitrust"],
    "AAPL": ["apple", "aapl", "iphone", "tim cook", "app store", "vision pro"],
    "MSFT": ["microsoft", "msft", "azure", "copilot", "openai", "windows", "activision"],
    "AMD": ["amd", "advanced micro", "ryzen", "radeon", "lisa su"],
    "QQQ":  ["nasdaq", "tech sector", "qqq", "tech stocks"],
    "SPY":  ["s&p 500", "spy", "dow jones", "market crash", "stock market"],
}

# Earnings calendar with whisper context
EARNINGS_CALENDAR = {
    "TSLA": {"date": "2026-04-22", "whisper_note": "Delivery miss risk — analyst estimates range wide"},
    "NVDA": {"date": "2026-05-28", "whisper_note": "Bar is extremely high — any China export concern = miss"},
    "AAPL": {"date": "2026-05-01", "whisper_note": "Services revenue key — hardware expected flat"},
    "META": {"date": "2026-04-30", "whisper_note": "Ad revenue + AI spend balance — guidance critical"},
    "GOOGL": {"date": "2026-04-29", "whisper_note": "Search market share vs AI threat — key narrative"},
    "MSFT": {"date": "2026-04-30", "whisper_note": "Azure growth rate — any deceleration = selloff"},
    "AMD": {"date": "2026-05-06", "whisper_note": "MI300 AI chip demand vs NVDA — market share story"},
}


class NewsIntelligence:
    """
    Advanced news classification engine.
    Goes beyond keyword detection to understand impact, direction, and timing.
    """

    def __init__(self):
        self._cache = {"articles": [], "cached_at": None}
        self._rss_feeds = [
            "https://feeds.marketwatch.com/marketwatch/topstories/",
            "https://feeds.content.dowjones.io/public/rss/mw_topstories",
            "https://cnbc.com/id/100003114/device/rss/rss.html",
            "https://feeds.reuters.com/reuters/businessNews",
            "https://news.google.com/rss/search?q=stock+market+finance&hl=en-US&gl=US&ceid=US:en",
        ]
        logger.info("✅ NewsIntelligence initialized")

    # ── RSS Fetching ──────────────────────────────────────────────────────────

    def fetch_articles(self, force_refresh: bool = False) -> list:
        """Fetch and cache news articles from RSS feeds."""
        now = datetime.now(timezone.utc)
        if not force_refresh and self._cache["cached_at"]:
            age = (now - self._cache["cached_at"]).total_seconds()
            if age < 300:  # 5 minute cache
                return self._cache["articles"]

        articles = []
        for feed_url in self._rss_feeds:
            try:
                headers = {"User-Agent": "Mozilla/5.0 (compatible; JimBot/1.0)"}
                resp = requests.get(feed_url, headers=headers, timeout=5)
                root = ET.fromstring(resp.content)
                for item in root.findall(".//item")[:8]:
                    title = item.find("title")
                    desc = item.find("description")
                    pub_date = item.find("pubDate")
                    if title is not None and title.text:
                        articles.append({
                            "title": title.text.strip(),
                            "description": (desc.text or "").strip()[:200],
                            "published": pub_date.text if pub_date is not None else "",
                            "source": feed_url.split("/")[2],
                            "text": (title.text + " " + (desc.text or "")).lower()
                        })
            except Exception as e:
                logger.warning(f"RSS fetch failed {feed_url}: {e}")

        self._cache = {"articles": articles, "cached_at": now}
        logger.info(f"📰 NewsIntelligence: {len(articles)} articles fetched")
        return articles

    # ── Tier Classification ───────────────────────────────────────────────────

    def classify_article(self, article: dict) -> dict:
        """
        Classify a single article into Tier 1-4 with direction and impact.
        """
        text = article["text"]
        result = {
            "tier": 4,
            "direction": "neutral",
            "score_adjustment": 0,
            "impact": "noise",
            "trump_signal": None,
            "affected_assets": [],
            "headline": article["title"]
        }

        # Tier 1 — Act within 60 seconds
        for kw in TIER_1_KEYWORDS["bullish"]:
            if kw in text:
                result.update({"tier": 1, "direction": "bullish",
                    "score_adjustment": +25, "impact": "market_moving"})
                return result
        for kw in TIER_1_KEYWORDS["bearish"]:
            if kw in text:
                result.update({"tier": 1, "direction": "bearish",
                    "score_adjustment": -25, "impact": "market_moving"})
                return result

        # Trump framework — check direction before scoring
        if "trump" in text:
            for kw in TRUMP_BULLISH:
                if kw in text:
                    result.update({"tier": 2, "direction": "bullish",
                        "score_adjustment": +15, "impact": "directional",
                        "trump_signal": f"TRUMP BULLISH: {kw}"})
                    break
            else:
                for kw in TRUMP_BEARISH:
                    if kw in text:
                        result.update({"tier": 2, "direction": "bearish",
                            "score_adjustment": -15, "impact": "directional",
                            "trump_signal": f"TRUMP BEARISH: {kw}"})
                        break
                else:
                    # Trump mentioned but direction unclear
                    result.update({"tier": 3, "direction": "uncertain",
                        "score_adjustment": -5, "impact": "contextual",
                        "trump_signal": "TRUMP: direction unclear — reduce conviction"})

        # Tier 2 — Directional signal
        if result["tier"] == 4:
            for kw in TIER_2_KEYWORDS["bullish"]:
                if kw in text:
                    result.update({"tier": 2, "direction": "bullish",
                        "score_adjustment": +12, "impact": "directional"})
                    break
            for kw in TIER_2_KEYWORDS["bearish"]:
                if kw in text:
                    result.update({"tier": 2, "direction": "bearish",
                        "score_adjustment": -12, "impact": "directional"})
                    break

        # Tier 3 — Contextual
        if result["tier"] == 4:
            for kw in TIER_3_KEYWORDS["bullish"]:
                if kw in text:
                    result.update({"tier": 3, "direction": "bullish",
                        "score_adjustment": +6, "impact": "contextual"})
                    break
            for kw in TIER_3_KEYWORDS["bearish"]:
                if kw in text:
                    result.update({"tier": 3, "direction": "bearish",
                        "score_adjustment": -6, "impact": "contextual"})
                    break

        # Detect affected assets
        for asset, keywords in ASSET_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                result["affected_assets"].append(asset)

        return result

    # ── Earnings Intelligence ─────────────────────────────────────────────────

    def get_earnings_context(self, symbol: str) -> dict:
        """
        Return earnings intelligence for a specific stock.
        Includes whisper note, days until earnings, and recommended action.
        """
        if symbol not in EARNINGS_CALENDAR:
            return {"has_earnings": False}

        info = EARNINGS_CALENDAR[symbol]
        earnings_date = datetime.strptime(info["date"], "%Y-%m-%d")
        today = datetime.now()
        days_until = (earnings_date - today).days

        if days_until < 0:
            # Past earnings — check if it was recent (post-earnings gap opportunity)
            days_since = abs(days_until)
            if days_since <= 3:
                return {
                    "has_earnings": True,
                    "timing": "post_earnings",
                    "days_since": days_since,
                    "whisper": info["whisper_note"],
                    "action": "GAPPER_PROTOCOL — evaluate post-earnings gap",
                    "score_adjustment": 0  # Neutral — let gap speak
                }
            return {"has_earnings": False}

        elif days_until == 0:
            return {
                "has_earnings": True,
                "timing": "earnings_day",
                "days_until": 0,
                "whisper": info["whisper_note"],
                "action": "SKIP — earnings day, too risky",
                "score_adjustment": -100  # Force skip
            }
        elif days_until <= 2:
            return {
                "has_earnings": True,
                "timing": "pre_earnings",
                "days_until": days_until,
                "whisper": info["whisper_note"],
                "action": f"CAUTION — earnings in {days_until} day(s), cap position at 10%",
                "score_adjustment": -20
            }
        elif days_until <= 7:
            return {
                "has_earnings": True,
                "timing": "earnings_week",
                "days_until": days_until,
                "whisper": info["whisper_note"],
                "action": f"AWARE — earnings in {days_until} days",
                "score_adjustment": -5
            }

        return {"has_earnings": False}

    # ── Master Analysis ───────────────────────────────────────────────────────

    def analyze(self, symbol: str = None) -> dict:
        """
        Run full news intelligence analysis.
        Returns classified articles, aggregate sentiment, and symbol-specific impact.
        """
        articles = self.fetch_articles()
        if not articles:
            return {
                "overall_sentiment": "neutral",
                "overall_score_adjustment": 0,
                "tier1_alerts": [],
                "trump_signals": [],
                "symbol_impact": 0,
                "earnings": {"has_earnings": False},
                "context": "No news data available",
                "top_headlines": [],
                "total_score_adjustment": 0,
            }

        classified = [self.classify_article(a) for a in articles]

        # Aggregate sentiment
        total_adj = sum(c["score_adjustment"] for c in classified)
        tier1_alerts = [c for c in classified if c["tier"] == 1]
        trump_signals = [c["trump_signal"] for c in classified if c["trump_signal"]]

        # Symbol-specific impact
        symbol_impact = 0
        symbol_articles = []
        if symbol:
            for c in classified:
                if symbol in c["affected_assets"]:
                    symbol_impact += c["score_adjustment"]
                    symbol_articles.append(c)

        # Overall sentiment label
        if total_adj >= 20:
            sentiment = "very_bullish"
        elif total_adj >= 8:
            sentiment = "bullish"
        elif total_adj <= -20:
            sentiment = "very_bearish"
        elif total_adj <= -8:
            sentiment = "bearish"
        else:
            sentiment = "neutral"

        # Earnings context
        earnings = self.get_earnings_context(symbol) if symbol else {"has_earnings": False}

        # Build context string for Claude
        lines = ["=== NEWS INTELLIGENCE ==="]
        lines.append(f"Overall sentiment: {sentiment.upper()} (aggregate score: {total_adj:+d})")

        if tier1_alerts:
            lines.append(f"⚡ TIER 1 ALERTS ({len(tier1_alerts)}):")
            for alert in tier1_alerts[:2]:
                lines.append(f"  → {alert['headline'][:80]} [{alert['direction'].upper()}]")

        if trump_signals:
            for sig in trump_signals[:2]:
                lines.append(f"🇺🇸 {sig}")

        if symbol and symbol_articles:
            lines.append(f"📌 {symbol}-specific news (impact: {symbol_impact:+d}):")
            for art in symbol_articles[:2]:
                lines.append(f"  → T{art['tier']} {art['direction'].upper()}: {art['headline'][:70]}")
        elif symbol:
            lines.append(f"📌 No {symbol}-specific news found")

        if earnings.get("has_earnings"):
            lines.append(f"📅 EARNINGS: {earnings['action']}")
            lines.append(f"   Whisper: {earnings['whisper']}")

        # Top 3 relevant headlines
        relevant = [c for c in classified if c["tier"] <= 3 and c["direction"] != "neutral"]
        for art in relevant[:3]:
            emoji = "🟢" if art["direction"] == "bullish" else "🔴"
            lines.append(f"{emoji} T{art['tier']}: {art['headline'][:75]}")

        total_score_adj = total_adj + symbol_impact + earnings.get("score_adjustment", 0)

        return {
            "overall_sentiment": sentiment,
            "overall_score_adjustment": total_adj,
            "symbol_score_adjustment": symbol_impact,
            "earnings_score_adjustment": earnings.get("score_adjustment", 0),
            "total_score_adjustment": total_score_adj,
            "tier1_alerts": tier1_alerts,
            "trump_signals": trump_signals,
            "earnings": earnings,
            "context": "\n".join(lines),
            "top_headlines": [c["headline"] for c in relevant[:3]],
        }

    def build_news_context(self, symbol: str = None) -> str:
        """Returns just the context string for injection into Claude prompt."""
        return self.analyze(symbol)["context"]
