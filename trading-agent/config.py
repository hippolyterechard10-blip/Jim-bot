import os

# Anthropic
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Alpaca
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"

# Trading rules
MAX_LEVERAGE = 3
MAX_POSITION_PCT = 0.30
GLOBAL_STOP_LOSS_PCT = 0.20
TRADE_STOP_LOSS_PCT = 0.05
MAX_POSITIONS = 5

# Trailing stop distances (fraction below highest price reached)
TRAILING_STOP_CRYPTO = 0.03   # 3% for crypto
TRAILING_STOP_STOCK  = 0.05   # 5% for stocks/ETFs

# Universe
CRYPTO_SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "DOGE/USD", "XRP/USD", "LINK/USD", "SHIB/USD", "MATIC/USD"]
STOCK_SYMBOLS = ["AAPL", "NVDA", "TSLA", "META", "GOOGL", "MSFT", "AMD"]
ETF_SYMBOLS = ["QQQ", "SPY", "ARKK"]
ALL_SYMBOLS = CRYPTO_SYMBOLS + STOCK_SYMBOLS + ETF_SYMBOLS

# Loop
LOOP_INTERVAL_SECONDS = 300
INITIAL_CAPITAL = 1000.0
