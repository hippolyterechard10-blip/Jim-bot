"""
scanner.py — Dynamic Top Movers + News Sentiment
"""
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import requests
import alpaca_trade_api as tradeapi
import config

logger = logging.getLogger(__name__)

ECONOMIC_EVENTS = [
    {"date": "2026-04-02", "event": "Fed Meeting"},
    {"date": "2026-04-03", "event": "Jobs Report (NFP)"},
    {"date": "2026-04-14", "event": "CPI Inflation"},
    {"date": "2026-04-29", "event": "Fed Meeting"},
    {"date": "2026-05-08", "event": "Jobs Report"},
    {"date": "2026-05-13", "event": "CPI Inflation"},
    {"date": "2026-06-17", "event": "Fed Meeting"},
]

# Confirmed Q1/Q2 2026 earnings dates — update as IRs announce
EARNINGS_DATES = {
    "TSLA":  "2026-04-22",
    "GOOGL": "2026-04-29",
    "MSFT":  "2026-04-30",
    "META":  "2026-04-30",
    "AAPL":  "2026-05-01",
    "AMD":   "2026-05-06",
    "NVDA":  "2026-05-28",
}

NEWS_FEEDS = [
    "https://feeds.marketwatch.com/marketwatch/topstories/",
    "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "https://news.google.com/rss/search?q=stock+market+economy+fed&hl=en-US&gl=US&ceid=US:en",
]

RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TradingAgent/1.0; +https://alpaca.markets)"
}

BULLISH_KEYWORDS = ["rate cut", "fed pivot", "strong jobs", "beat expectations",
    "record high", "bull market", "stimulus", "better than expected", "upgrade"]

BEARISH_KEYWORDS = ["tariff", "trade war", "recession", "rate hike", "layoffs",
    "miss expectations", "downgrade", "inflation surge", "trump tariff",
    "china trade", "worse than expected", "hawkish"]

HIGH_ALERT_KEYWORDS = ["trump", "federal reserve", "emergency rate",
    "war", "sanctions", "default", "collapse", "crisis"]

# Only keep headlines relevant to financial markets — excludes personal finance,
# tax advice, lifestyle articles, etc.
FINANCE_FILTER_KEYWORDS = [
    "stock", "market", "share", "equity", "index", "dow", "nasdaq", "s&p", "spy",
    "fed", "federal reserve", "interest rate", "rate cut", "rate hike", "powell",
    "earnings", "revenue", "profit", "quarter", "ipo", "merger", "acquisition",
    "crypto", "bitcoin", "ethereum", "blockchain", "coin",
    "tariff", "trade war", "sanction", "import duty", "export",
    "inflation", "cpi", "pce", "gdp", "recession", "jobs report", "unemployment",
    "oil", "gold", "commodity", "bond", "treasury", "yield", "debt",
    "dollar", "currency", "forex", "yuan", "yen", "euro",
    "wall street", "hedge fund", "bank", "jpmorgan", "goldman", "morgan stanley",
    "apple", "nvidia", "tesla", "meta", "google", "amazon", "microsoft", "intel",
    "tech stock", "semiconductor", "chip", "ai stock",
    "economy", "economic", "fiscal", "monetary", "deficit",
    "opec", "china trade", "supply chain", "manufacturing",
    "rally", "selloff", "correction", "bull market", "bear market",
    "war", "sanctions", "geopolit", "trump", "biden", "white house policy",
]

class MarketScanner:
    def __init__(self):
        self.api = tradeapi.REST(
            config.ALPACA_API_KEY,
            config.ALPACA_SECRET_KEY,
            config.ALPACA_BASE_URL
        )
        self._news_cache = {"headlines": [], "sentiment": "neutral", "cached_at": None}
        self._movers_cache = {"symbols": [], "cached_at": None}
        logger.info("✅ MarketScanner initialized")

    def get_top_movers(self, top_n=6):
        now = datetime.now(timezone.utc)
        if self._movers_cache["cached_at"]:
            age = (now - self._movers_cache["cached_at"]).total_seconds()
            if age < 900 and self._movers_cache["symbols"]:
                return self._movers_cache["symbols"]
        try:
            assets = self.api.list_assets(status="active", asset_class="us_equity")
            tradeable = [a for a in assets if a.tradable and a.fractionable]
            symbols = [a.symbol for a in tradeable[:500]]
            snapshots = self.api.get_snapshots(symbols)
            gappers = []
            movers  = []
            for symbol, snap in snapshots.items():
                try:
                    if not snap.daily_bar or not snap.prev_daily_bar:
                        continue
                    prev       = snap.prev_daily_bar.close
                    curr       = snap.daily_bar.close
                    prev_vol   = snap.prev_daily_bar.volume or 1
                    volume     = snap.daily_bar.volume
                    if prev <= 0:
                        continue
                    change_pct   = ((curr - prev) / prev) * 100
                    volume_ratio = round(volume / prev_vol, 1) if prev_vol > 0 else 0
                    is_microcap  = 0.50 <= curr <= 10.0

                    # Qualify as a mover:
                    # - Standard:  |change| ≥ 3% AND volume > 50 000
                    # - Micro/small cap ($0.50–$10): volume must exceed 500 000 (higher bar)
                    if is_microcap:
                        qualifies = abs(change_pct) >= 3 and volume > 500_000
                    else:
                        qualifies = abs(change_pct) >= 3 and volume > 50_000

                    if not qualifies:
                        continue

                    entry = {
                        "symbol":       symbol,
                        "price":        round(curr, 4),
                        "change_pct":   round(change_pct, 2),
                        "volume":       volume,
                        "volume_ratio": volume_ratio,
                        "direction":    "up" if change_pct > 0 else "down",
                        "is_gapper":    False,
                        "is_microcap":  is_microcap,
                    }

                    # GAPPER ALERT: >50% gain on >5× average volume
                    if change_pct >= 50 and volume_ratio >= 5:
                        entry["is_gapper"] = True
                        logger.info(
                            f"🚨 GAPPER ALERT: {symbol} +{change_pct:.1f}% "
                            f"volume={volume_ratio}x — potential 100-200% move"
                        )
                        gappers.append(entry)
                    else:
                        movers.append(entry)

                except Exception:
                    continue

            # Sort each bucket by magnitude; gappers always win
            gappers.sort(key=lambda x: x["change_pct"], reverse=True)
            movers.sort(key=lambda x: abs(x["change_pct"]), reverse=True)

            # Gappers all included; regular movers capped at top_n
            top = gappers + movers[:top_n]
            self._movers_cache = {"symbols": top, "cached_at": now}

            gap_labels   = [f"🚨{m['symbol']} +{m['change_pct']:.1f}% {m['volume_ratio']}x" for m in gappers]
            mover_labels = [f"{m['symbol']} {m['change_pct']:+.1f}%" for m in movers[:top_n]]
            if gap_labels:
                logger.info(f"🚨 GAPPERS: {gap_labels}")
            logger.info(f"🔥 Top movers: {mover_labels}")
            return top
        except Exception as e:
            logger.error(f"get_top_movers error: {e}")
            return []

    def get_dynamic_watchlist(self):
        from strategy import is_good_stock_window
        seen = set()
        watchlist = []

        def _add(symbols):
            for s in symbols:
                if s not in seen:
                    seen.add(s)
                    watchlist.append(s)

        if is_good_stock_window():
            movers = self.get_top_movers(top_n=6)

            # Split movers into gappers (highest priority) and regular movers
            gapper_symbols = [m["symbol"] for m in movers if m.get("is_gapper")]
            regular_symbols = [m["symbol"] for m in movers if not m.get("is_gapper")]

            _add(gapper_symbols)          # 1. GAPPERS — absolute priority
            _add(regular_symbols)         # 2. Regular top movers
            _add(config.BLUECHIP_SYMBOLS) # 3. Blue chips — always present

        _add(config.CRYPTO_SYMBOLS)       # 4. Crypto — always present

        gappers_in = [s for s in watchlist if s in {m["symbol"] for m in self._movers_cache.get("symbols", []) if m.get("is_gapper")}]
        bc_in_list  = [s for s in config.BLUECHIP_SYMBOLS if s in seen]
        mover_only  = [s for s in watchlist if s not in config.BLUECHIP_SYMBOLS and s not in config.CRYPTO_SYMBOLS and s not in gappers_in]
        logger.info(
            f"📋 Watchlist ({len(watchlist)}): "
            f"gappers={gappers_in} | "
            f"movers={mover_only} | "
            f"bluechips={bc_in_list} | "
            f"crypto={[s for s in watchlist if s in config.CRYPTO_SYMBOLS]}"
        )
        return watchlist

    def fetch_news_headlines(self):
        now = datetime.now(timezone.utc)
        if self._news_cache["cached_at"]:
            age = (now - self._news_cache["cached_at"]).total_seconds()
            if age < 180:
                return self._news_cache["headlines"]
        headlines = []
        for feed_url in NEWS_FEEDS:
            try:
                resp = requests.get(feed_url, timeout=8, headers=RSS_HEADERS)
                if resp.status_code != 200:
                    logger.warning(f"RSS {resp.status_code}: {feed_url[:60]}")
                    continue
                root = ET.fromstring(resp.content)
                fetched = 0
                for item in root.findall(".//item"):
                    title = item.find("title")
                    if title is not None and title.text:
                        text = title.text.strip()
                        # Google News wraps titles in CDATA — clean angle-bracket artefacts
                        text_lower = text.lower()
                        is_finance = any(kw in text_lower for kw in FINANCE_FILTER_KEYWORDS)
                        if text and "<" not in text and is_finance:
                            headlines.append(text)
                            fetched += 1
                    if fetched >= 5:
                        break
                logger.debug(f"RSS OK ({fetched} headlines): {feed_url[:60]}")
            except Exception as e:
                logger.warning(f"RSS error ({feed_url[:40]}): {e}")
        self._news_cache["headlines"] = headlines
        self._news_cache["cached_at"] = now
        return headlines

    def analyze_sentiment(self):
        headlines = self.fetch_news_headlines()
        if not headlines:
            return {"sentiment": "neutral", "score": 0, "alerts": []}
        score = 0
        alerts = []
        text = " ".join(headlines).lower()
        for kw in BULLISH_KEYWORDS:
            if kw in text:
                score += 1
        for kw in BEARISH_KEYWORDS:
            if kw in text:
                score -= 1
        for kw in HIGH_ALERT_KEYWORDS:
            if kw in text:
                alerts.append(f"⚡ HIGH ALERT: '{kw.upper()}' detected in headlines")
        if score >= 3: sentiment = "very_bullish"
        elif score >= 1: sentiment = "bullish"
        elif score <= -3: sentiment = "very_bearish"
        elif score <= -1: sentiment = "bearish"
        else: sentiment = "neutral"
        logger.info(f"📰 Sentiment: {sentiment} (score: {score})")
        ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
        return {"sentiment": sentiment, "score": score, "alerts": alerts, "headlines": headlines[:3], "ts": ts}

    def check_economic_calendar(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for event in ECONOMIC_EVENTS:
            if event["date"] == today:
                return {"event": event["event"], "timing": "today",
                    "note": f"⚡ {event['event']} TODAY — high volatility expected"}
        return {"event": None, "note": ""}

    def get_earnings_alerts(self, symbols=None):
        """
        Return earnings alerts for stocks within ±1 day of their earnings date.
          type='earnings_day'  → reports TODAY (skip the stock)
          type='pre_earnings'  → reports in 1–2 days (cut position to 10%)
          type='post_earnings' → reported yesterday (watch for gap play)
        """
        from datetime import date as date_cls
        today = datetime.now(timezone.utc).date()
        alerts = []
        for symbol, date_str in EARNINGS_DATES.items():
            if symbols and symbol not in symbols:
                continue
            edate     = datetime.strptime(date_str, "%Y-%m-%d").date()
            days_away = (edate - today).days
            if days_away == 0:
                alerts.append({"symbol": symbol, "days_away": 0,  "type": "earnings_day"})
            elif 1 <= days_away <= 2:
                alerts.append({"symbol": symbol, "days_away": days_away, "type": "pre_earnings"})
            elif days_away == -1:
                alerts.append({"symbol": symbol, "days_away": -1, "type": "post_earnings"})
        return alerts

    def build_market_context(self, symbol=None):
        sentiment = self.analyze_sentiment()
        calendar  = self.check_economic_calendar()
        movers    = self.get_top_movers(top_n=6)
        earnings  = self.get_earnings_alerts()

        lines = ["=== MARKET INTELLIGENCE ==="]
        emoji = {"very_bullish":"🚀","bullish":"🟢","neutral":"⚪","bearish":"🔴","very_bearish":"💀"}
        lines.append(f"📰 NEWS: {emoji.get(sentiment['sentiment'],'⚪')} {sentiment['sentiment'].upper()} (score: {sentiment['score']})")
        for alert in sentiment.get("alerts", []):
            lines.append(f"  {alert}")
        if calendar["event"]:
            lines.append(f"📅 CALENDAR: {calendar['note']}")

        # Earnings proximity warnings — passed to Claude so it can factor in volatility
        for ea in earnings:
            if ea["type"] == "earnings_day":
                lines.append(
                    f"🚨 EARNINGS DAY: {ea['symbol']} reports TODAY — "
                    f"DO NOT ENTER any position, risk is too high"
                )
            elif ea["type"] == "pre_earnings":
                lines.append(
                    f"⚠️ EARNINGS ALERT: {ea['symbol']} reports in {ea['days_away']} day(s) — "
                    f"expect high volatility, reduce position size to 10% max"
                )
            elif ea["type"] == "post_earnings":
                lines.append(
                    f"📈 POST-EARNINGS: {ea['symbol']} reported yesterday — "
                    f"if gapping up >5% on high volume, treat as priority gapper (score 90+, up to 40% position)"
                )

        if movers:
            lines.append("🔥 TOP MOVERS:")
            for m in movers[:6]:
                vol_str = f" vol={m.get('volume_ratio','')}x" if m.get("volume_ratio") else ""
                tag     = " 🚨 GAPPER — prioritize, potential 100-200% move" if m.get("is_gapper") else ""
                lines.append(f"  {'↑' if m['direction']=='up' else '↓'} {m['symbol']}: {m['change_pct']:+.1f}%{vol_str}{tag}")
        if symbol:
            sm = next((m for m in movers if m["symbol"] == symbol), None)
            if sm:
                if sm.get("is_gapper"):
                    lines.append(
                        f"🚨 {symbol} IS A CONFIRMED GAPPER: {sm['change_pct']:+.1f}% "
                        f"volume={sm.get('volume_ratio','')}x average — "
                        f"score this 90+, use max position size, set tight trailing stop"
                    )
                else:
                    lines.append(f"⭐ {symbol} IS A TOP MOVER: {sm['change_pct']:+.1f}%")
        if sentiment["sentiment"] in ["very_bullish", "bullish"]:
            lines.append("💡 Conditions favorable for LONG positions")
        elif sentiment["sentiment"] in ["very_bearish", "bearish"]:
            lines.append("💡 Conditions favorable for SHORT positions — tighten stops on longs")
        else:
            lines.append("💡 Neutral market — stick to technical signals only")
        return "\n".join(lines)
