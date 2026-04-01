"""
GeometricExpert — Pilier 2 ($500 virtual pool)
STUB: evaluate() logs candidates only. No trades placed until next session.
Metrics: Confluence score (1-5), Market structure, Timeframe alignment, RSI divergence.
Stop: 1x ATR below level. Entry: rejection candle breakout.
"""
import logging, json
import config

logger = logging.getLogger(__name__)

class GeometricExpert:
    def __init__(self, broker, memory, geometry, regime, correlations):
        self.broker = broker
        self.memory = memory
        self.geometry = geometry
        self.regime = regime
        self.correlations = correlations
        self._candidates = []

    def add_candidate(self, symbol: str):
        if symbol not in self._candidates:
            self._candidates.append(symbol)

    def flush_candidates(self) -> list:
        c = list(self._candidates)
        self._candidates.clear()
        return c

    def get_deployed_capital(self) -> float:
        try:
            total = 0.0
            for t in self.memory.get_open_trades():
                ctx = t.get("market_context") or {}
                if isinstance(ctx, str):
                    try: ctx = json.loads(ctx)
                    except: ctx = {}
                if ctx.get("strategy_source") == "geometric":
                    total += float(t.get("entry_price", 0)) * float(t.get("qty", 0))
            return total
        except Exception as e:
            logger.error(f"GeometricExpert.get_deployed_capital: {e}")
            return config.STRATEGY_CAPITAL["geometric"]

    def get_available_capital(self) -> float:
        return max(0.0, config.STRATEGY_CAPITAL["geometric"] - self.get_deployed_capital())

    def has_capital(self) -> bool:
        return self.get_available_capital() >= 50.0

    def get_capital_pct_for_setup(self, confluence_score: int) -> float:
        """Capital deployed based on setup quality."""
        if confluence_score >= 5: return 0.95
        if confluence_score >= 4: return 0.90
        return 0.80

    def evaluate(self, symbol: str):
        """STUB — logs candidate. No trades until expert logic is built."""
        logger.debug(f"[GEO] 🔍 CANDIDATE: {symbol} | capital=${self.get_available_capital():.0f} | STUB — no trade")
