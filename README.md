# HFTbot - Bollinger Band Mean Reversion Scalper

High-frequency XAUUSD scalping bot using Bollinger Band mean reversion on the M1 timeframe. Supports multiple independent bot instances with different configurations.

## Strategy

Scalps gold by entering at Bollinger Band extremes when confirmed by RSI:

- **BUY**: Price touches/pierces lower BB(20, 2.0) AND RSI(14) < 30 (oversold)
- **SELL**: Price touches/pierces upper BB(20, 2.0) AND RSI(14) > 70 (overbought)
- **SL**: 1.0x ATR(14) from entry
- **TP**: 1.5x ATR(14) from entry (1.5:1 R:R)
- **Session Filter**: London hours (07:00-13:00 UTC), with London+NY variant
- **Spread Cap**: Skips entries when spread > 30 points

## Multi-Bot Architecture

Runs up to 3 independent bot instances, each with its own:
- MT5 terminal connection and credentials
- Configuration profile (session hours, risk %)
- Trade journal database

```
Bot 1: London session, 1% risk
Bot 2: London session, 1% risk (different account)
Bot 3: London + New York session, 2% risk
```

## Architecture

```
src/
  strategy/
    bb_reversion.py      # Bollinger Band mean reversion logic
    engine.py            # Strategy orchestrator
    base_strategy.py     # Signal/trade abstractions
  data/
    market_feed.py       # MT5 M1 candle feed
    regime_detector.py   # Market regime classification (trend avoidance)
  execution/
    mt5_executor.py      # MT5 order execution
  risk/
    manager.py           # Position sizing, circuit breakers
  db/
    database.py          # SQLite trade journal
  alerts/
    notifier.py          # Telegram trade notifications
  api/
    fastapi_server.py    # Dashboard API endpoint
  main.py               # Entry point

run_bot1.py              # Launch Bot 1
run_bot2.py              # Launch Bot 2
run_bot3.py              # Launch Bot 3
```

## Tech Stack

- **Python 3.11+**
- **MetaTrader5** - M1 market data and order execution
- **pandas / numpy** - Data processing and indicator calculation
- **SQLite** - Per-bot trade journaling
- **Telegram Bot API** - Real-time trade alerts
- **FastAPI** - Dashboard integration

## Setup

1. Install dependencies: `pip install -r requirements.txt`
2. Copy `.env.example` to `.env.bot1`, `.env.bot2`, `.env.bot3` and fill in credentials
3. Run individual bots: `python run_bot1.py`

## Risk Management

- Configurable risk per trade (1-2% equity)
- ATR-based dynamic SL/TP
- Max 20 trades per day, 60s minimum between entries
- Regime filter avoids strong trending markets
- Circuit breaker on consecutive losses
