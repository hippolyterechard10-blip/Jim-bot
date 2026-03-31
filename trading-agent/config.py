import os

# Anthropic
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Alpaca
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"

# Trading rules

MAX_POSITION_PCT = 0.30
GLOBAL_STOP_LOSS_PCT = 0.20
TRADE_STOP_LOSS_PCT = 0.05
MAX_POSITIONS = 5

# Trailing stop distances — LONG positions (fraction below highest price)
TRAILING_STOP_CRYPTO = 0.03   # 3% for crypto longs
TRAILING_STOP_STOCK  = 0.05   # 5% for stocks/ETFs longs

# Trailing stop distances — SHORT positions (fraction above lowest price)
TRAILING_STOP_SHORT_CRYPTO = 0.03  # 3% for crypto shorts (paper only)
TRAILING_STOP_SHORT_STOCK  = 0.06  # 6% for stocks/ETF shorts

# Short selling rules
MAX_SHORT_SIZE_PCT   = 0.15   # Max 15% of portfolio per short position
SHORT_ENTRY_RSI_MIN  = 70     # RSI must be above this to short
SHORT_ENTRY_CONF_MAX = 0.30   # Claude confidence must be below this

# Partial profit taking
PARTIAL_PROFIT_PCT   = 0.03   # Take partial profits at +3% unrealised gain
PARTIAL_PROFIT_RATIO = 0.50   # Sell / cover this fraction of the position (50%)

# Universe
CRYPTO_SYMBOLS   = ["BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "DOGE/USD", "XRP/USD", "LINK/USD", "SHIB/USD", "MATIC/USD"]
STOCK_SYMBOLS    = ["AAPL", "NVDA", "TSLA", "META", "GOOGL", "MSFT", "AMD"]
ETF_SYMBOLS      = ["QQQ", "SPY", "ARKK"]
# Fixed blue-chip list always evaluated every cycle regardless of top movers
BLUECHIP_SYMBOLS = ["AAPL", "NVDA", "TSLA", "META", "GOOGL", "MSFT", "AMD", "QQQ", "SPY"]
ALL_SYMBOLS      = CRYPTO_SYMBOLS + STOCK_SYMBOLS + ETF_SYMBOLS

# Loop speeds
LOOP_INTERVAL_SECONDS      = 300   # Slow loop: full synthesis + movers refresh
FAST_LOOP_INTERVAL_SECONDS = 30    # Fast loop: position stops + score triggers
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "1000.0"))
