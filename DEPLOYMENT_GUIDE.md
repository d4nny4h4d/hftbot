# HFTBot — Deployment Guide & Manager Summary

## Project Overview

HFTBot is a high-frequency gold (XAU/USD) trading bot operating on the 1-minute
timeframe. It runs alongside GoldBot (15m timeframe) as part of the AhadAI
Trading Hub. HFTBot uses a separate MT5 demo account for isolated testing.

---

## Architecture

```
HFTbot/
├── config/strategies.yaml      ← All strategy parameters + risk config
├── src/
│   ├── main.py                 ← Entry point (HFTBot class, scheduler)
│   ├── data/
│   │   ├── market_feed.py      ← MT5 connection + price data
│   │   └── regime_detector.py  ← ADX trend + ATR volatility regimes
│   ├── strategy/
│   │   ├── base_strategy.py    ← Abstract strategy interface
│   │   ├── engine.py           ← Signal orchestrator (7 strategies)
│   │   ├── ema_momentum.py     ← EMA(9/21) crossover scalper
│   │   ├── bollinger_reversion.py  ← BB(20,2) mean reversion
│   │   ├── vwap_bounce.py      ← VWAP bounce with volume
│   │   ├── london_open.py      ← 08:00 UTC breakout
│   │   ├── ny_session.py       ← 13:30 UTC momentum
│   │   ├── london_fix.py       ← 10:30 UTC fix fade
│   │   └── micro_breakout.py   ← Consolidation breakout
│   ├── execution/mt5_executor.py   ← MT5 order management
│   ├── risk/manager.py         ← Position sizing + circuit breakers
│   ├── db/database.py          ← SQLite trade journal
│   ├── alerts/notifier.py      ← Telegram alerts
│   └── api/fastapi_server.py   ← REST API for dashboard
├── research/
│   ├── strategy_research.md    ← Strategy research report
│   ├── backtester.py           ← Historical backtesting engine
│   └── backtest_results_summary.md ← Results template
├── data/                       ← SQLite DB + logs (auto-created)
└── .env.example                ← MT5 credential template
```

---

## Key Differences from GoldBot

| Aspect | GoldBot | HFTBot |
|--------|---------|--------|
| Timeframe | M15 (15-min) | M1 (1-min) |
| Check interval | 15 seconds | 5 seconds |
| Risk per trade | 2.0% | 1.0% |
| Max trades/day | ~3-5 | 20 |
| Strategies | 1 (ITF) | 7 (scalping + time-based) |
| Magic number | 123456 | 234567 |
| Comment prefix | GB_ | HF_ |
| Database | goldbot.db | hftbot.db |
| MT5 account | Main demo | New demo account |
| Equity snapshots | Every 5 min | Every 2 min |

---

## Strategy Overview

### Indicator-Based (run continuously during active sessions)
1. **EMA Momentum Scalper** — EMA(9/21) crossover + RSI(7) momentum
2. **Bollinger Mean Reversion** — BB(20,2) touch + RSI(14) extremes
3. **VWAP Bounce** — Session VWAP bounce with volume spike
4. **Micro Range Breakout** — Tight consolidation breakout with trend filter

### Time-Based (one trade per day, specific UTC times)
5. **London Open** — 08:00 UTC breakout of pre-London range
6. **NY Session** — 13:30 UTC momentum from first 5 candles
7. **London Fix Fade** — 10:30 UTC mean reversion after fix

---

## Setup Steps

### 1. Create New Demo Account
- Open Exness platform
- Create a NEW demo account (separate from GoldBot)
- Note the login, password, and server name

### 2. Configure Environment
```bash
cd HFTbot
cp .env.example .env
# Edit .env with new demo account credentials
```

### 3. Run Backtester First
```bash
cd HFTbot
pip install -r requirements.txt
python research/backtester.py
```
Review backtest results before enabling live demo trading.

### 4. Start HFTBot
```bash
cd HFTbot
python src/main.py
```

### 5. Dashboard
The shared dashboard already includes HFTBot. Run from the goldbot directory:
```bash
cd goldbot
streamlit run dashboard/dashboard.py
```
The sidebar will show both GoldBot and HFTBot with their own pages.
Portfolio overview aggregates across both bots.

---

## Risk Controls

- **1% risk per trade** (half of GoldBot's 2%)
- **3% daily circuit breaker** — all trading stops
- **20 max trades/day** — prevents overtrading
- **60-second minimum** between new entries
- **30-point max spread** — skip entries during wide spreads
- **3 max open positions** (2 same direction)
- **Hard exits** at strategy-specific times (no overnight holds)
- **ADX regime filtering** — each strategy has optimal regime weights
- **Volatility regime** — ATR percentile-based vol classification

---

## Dashboard Integration

The goldbot dashboard (`dashboard/dashboard.py`) has been updated to:
- **Default to Portfolio Overview** — shows all bots aggregated
- **Portfolio Performance** — combined equity curve + per-bot comparison
- **HFTBot section in sidebar** — own Overview, Trades, Calendar, Equity, Reports
- **Account stats** — per-bot equity in sidebar with total

No changes were made to GoldBot's trading code or configuration.

---

## Phased Deployment Plan

### Phase 1: Backtest & Validate (Week 1)
- Run `backtester.py` on 3 months of M1 data
- Review win rates, profit factors, regime performance
- Disable strategies with PF < 1.2

### Phase 2: Demo with Core Strategies (Weeks 2-3)
- Enable: EMA Momentum + Bollinger Reversion
- These are complementary (trend + range) and highest frequency
- Monitor on dashboard for 2 weeks

### Phase 3: Add Time-Based Strategies (Weeks 3-4)
- Enable: London Open + NY Session
- These add 1-2 high-quality trades per day
- Continue monitoring

### Phase 4: Full Strategy Suite (Weeks 4-6)
- Enable: VWAP Bounce + London Fix + Micro Breakout
- All 7 strategies running on demo
- 30-day evaluation period

### Phase 5: Live Decision
- After 30 days of full demo testing
- Review: Win rate, PF, max DD, regime adaptability
- Decision to go live or refine

---

## Files NOT Modified in GoldBot

Per instructions, the following goldbot files remain unchanged:
- `goldbot/src/` — all source code untouched
- `goldbot/config/strategies.yaml` — configuration unchanged
- `goldbot/.env` — credentials unchanged
- `goldbot/data/goldbot.db` — database untouched

The ONLY goldbot file modified was `goldbot/dashboard/dashboard.py` to add:
- HFTBot to the BOT_REGISTRY
- Portfolio pages in sidebar navigation
- Portfolio page routing
- Per-bot sidebar stats

---

*Manager summary prepared: 2026-03-14*
*GoldBot status: Running, no changes until 30-day test complete*
*HFTBot status: Code complete, awaiting new demo account setup and backtesting*
