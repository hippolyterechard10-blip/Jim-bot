"""
GapperExpert — Pilier 1 ($500 virtual pool)
STUB: evaluate() logs candidates only. No trades placed until next session.
Metrics: Float (primary), Gap%, Volume, Catalyst type, Short interest.
Stop: First 5-min candle low -0.5% | Hard: -10% | Breakeven after +10% partial.
"""
import logging, json
import config

logger = logging.getLogger(__name__)

class GapperExpert:
    def __init__(self, broker, memory, scanner, regime):
        self.broker = broker
        self.memory = memory
        self.scanner = scanner
        self.regime = regime
        self._candidates = []

    def add_candidate(self, mover: dict):
        self._candidates.append(mover)

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
                if ctx.get("strategy_source") == "gapper":
                    total += float(t.get("entry_price", 0)) * float(t.get("qty", 0))
            return total
        except Exception as e:
            logger.error(f"GapperExpert.get_deployed_capital: {e}")
            return config.STRATEGY_CAPITAL["gapper"]

    def get_available_capital(self) -> float:
        return max(0.0, config.STRATEGY_CAPITAL["gapper"] - self.get_deployed_capital())

    def has_capital(self) -> bool:
        return self.get_available_capital() >= 50.0

    def evaluate(self, candidate: dict):
        """STUB — logs candidate analysis. No trades until expert logic is built."""
        sym = candidate.get("symbol", "?")
        chg = candidate.get("change_pct", 0)
        vol = candidate.get("volume_ratio", 0)
        flt = candidate.get("float_shares")
        cat = candidate.get("catalyst", "unknown")
        float_str = f"{flt/1e6:.1f}M" if flt else "unknown"
        logger.info(
            f"[GAPPER] 🔍 CANDIDATE: {sym} | gap={chg:+.1f}% | vol={vol:.1f}x | "
            f"float={float_str} | catalyst={cat} | "
            f"capital_available=${self.get_available_capital():.0f} | STUB — no trade"
        )
