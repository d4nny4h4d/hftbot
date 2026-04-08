# XAU/USD 1-Minute HFT Backtest Results Summary

## Methodology

### Data
- **Instrument:** XAU/USD (Gold vs US Dollar)
- **Timeframe:** 1-minute bars
- **Source:** MetaTrader 5 terminal (broker live/demo server)
- **Period:** 3 months rolling (configurable via `--months`)
- **Bars expected:** ~130,000 (approx 60 bars/hr x 24hr x 5 days x 13 weeks)

### Execution Assumptions
| Parameter | Value |
|---|---|
| Spread | 20 points (0.20 USD) |
| Commission | $7.00 round-turn per standard lot |
| Slippage | Not modelled (conservative SL/TP widths compensate) |
| Lot size | 1.0 standard lot (100 oz) |
| Starting balance | $100,000 |
| Position sizing | Fixed 1.0 lot per trade |

### Market Regime Classification (ADX 14)
| ADX Range | Label |
|---|---|
| < 20 | Ranging |
| 20 - 30 | Transitional |
| 30 - 50 | Trending |
| > 50 | Strong Trend |

### Session Definitions (UTC)
| Session | Hours |
|---|---|
| Asian | 00:00 - 07:00 |
| London | 07:00 - 13:00 |
| NY | 13:00 - 20:00 |
| Off-hours | 20:00 - 00:00 |

---

## Strategies Tested

### A. EMA Momentum Scalper
- **Signal:** EMA(9) / EMA(21) crossover on 1m
- **Filter:** RSI(7) > 50 for longs, < 50 for shorts
- **SL:** 1.5 x ATR(14) | **TP:** 2.0 x ATR(14)
- **Expected character:** Trend-following; performs best in Trending/Strong regimes. Likely whipsawed in Ranging markets.

### B. Bollinger Band Mean Reversion
- **Signal:** Price touches lower BB(20,2) + RSI(14) < 30 for longs; upper BB + RSI > 70 for shorts
- **SL:** 1.0 x ATR(14) | **TP:** 1.5 x ATR(14)
- **Expected character:** Counter-trend; best in Ranging regimes. May suffer in strong trends.

### C. VWAP Bounce Scalper
- **Signal:** Price pulls back to session VWAP, bounces with volume spike (1.5x avg volume)
- **SL:** VWAP +/- 0.5 x ATR | **TP:** 2.0 x ATR(14)
- **Expected character:** Intraday mean-reversion to VWAP. Session-dependent; London and NY expected to outperform.

### D. London Open Momentum
- **Signal:** Breakout of 07:00-07:59 UTC range during 08:00-08:15 UTC
- **SL:** Opposite side of range | **TP:** 1.5 x range width
- **Expected character:** Time-specific breakout. Limited trade frequency (max 1/day). Depends on pre-London consolidation quality.

### E. NY Session Scalper
- **Signal:** 3+ directional candles with increasing volume in first 5 bars after 13:30 UTC
- **SL:** 1.5 x ATR(14) | **TP:** 2.5 x ATR(14)
- **Expected character:** Momentum capture at US open. Low frequency (max 1/day). High R:R target.

### F. London Fix Fade
- **Signal:** Fade the 10:00-10:30 UTC price direction at 10:30 UTC
- **SL:** 2.0 x ATR(14) | **TP:** 3.0 x ATR(14)
- **Expected character:** Mean-reversion around London AM fix. Very low frequency. High R:R target offsets low win rate.

### G. Micro Range Breakout
- **Signal:** 10-15 bar consolidation (range < 0.5 x ATR(50)), breakout aligned with EMA(200) on 5m equivalent
- **SL:** Mid-range | **TP:** 2.0 x range width
- **Expected character:** Volatility expansion after compression. Works across sessions, best in Transitional-to-Trending transitions.

---

## Results Template

> Run `python backtester.py` to populate with actual numbers.

### Per-Strategy Metrics

| Metric | A | B | C | D | E | F | G |
|---|---|---|---|---|---|---|---|
| Total trades | - | - | - | - | - | - | - |
| Win rate (%) | - | - | - | - | - | - | - |
| Avg R:R achieved | - | - | - | - | - | - | - |
| Profit factor | - | - | - | - | - | - | - |
| Max consec. losses | - | - | - | - | - | - | - |
| Max drawdown (%) | - | - | - | - | - | - | - |
| Sharpe ratio (ann.) | - | - | - | - | - | - | - |
| Avg trade duration | - | - | - | - | - | - | - |
| Total P&L ($) | - | - | - | - | - | - | - |
| Net return (%) | - | - | - | - | - | - | - |

### Performance by Market Regime (per strategy)

| Strategy | Ranging | Transitional | Trending | Strong Trend |
|---|---|---|---|---|
| A. EMA Momentum | -/- | -/- | -/- | -/- |
| B. BB Reversion | -/- | -/- | -/- | -/- |
| C. VWAP Bounce | -/- | -/- | -/- | -/- |
| D. London Open | -/- | -/- | -/- | -/- |
| E. NY Scalper | -/- | -/- | -/- | -/- |
| F. London Fix | -/- | -/- | -/- | -/- |
| G. Micro Range | -/- | -/- | -/- | -/- |

*Format: trades / win rate (%)*

### Performance by Session (per strategy)

| Strategy | Asian | London | NY | Off-hours |
|---|---|---|---|---|
| A. EMA Momentum | -/- | -/- | -/- | -/- |
| B. BB Reversion | -/- | -/- | -/- | -/- |
| C. VWAP Bounce | -/- | -/- | -/- | -/- |
| D. London Open | -/- | -/- | -/- | -/- |
| E. NY Scalper | -/- | -/- | -/- | -/- |
| F. London Fix | -/- | -/- | -/- | -/- |
| G. Micro Range | -/- | -/- | -/- | -/- |

*Format: trades / win rate (%)*

---

## Final Ranking

| Rank | Strategy | Sharpe | PF | Win% | MaxDD% | P&L |
|---|---|---|---|---|---|---|
| 1 | - | - | - | - | - | - |
| 2 | - | - | - | - | - | - |
| 3 | - | - | - | - | - | - |
| 4 | - | - | - | - | - | - |
| 5 | - | - | - | - | - | - |
| 6 | - | - | - | - | - | - |
| 7 | - | - | - | - | - | - |

---

## Notes & Considerations

1. **Spread & commission** are included in all P&L calculations. Spread is applied at entry; commission deducted at exit.
2. **No slippage model** -- the ATR-based SL/TP widths provide buffer, but live execution on 1m XAU/USD will see slippage during high-impact news.
3. **Tick volume** is used as a proxy for real volume from MT5. Tick volume correlates well with actual volume on liquid instruments but is not identical.
4. **VWAP** resets daily at 00:00 UTC. Some brokers use different session boundaries.
5. **Time-based strategies** (D, E, F) produce far fewer trades than indicator-based ones. Statistical significance requires longer test periods.
6. **Regime filtering** can be applied post-hoc: e.g., only deploy Strategy A during Trending regimes and Strategy B during Ranging regimes.
7. **Position sizing** is fixed at 1.0 lot. A Kelly criterion or fractional sizing model should be layered on top for live deployment.
8. Results saved to `backtest_results.csv` alongside this file for programmatic analysis.
