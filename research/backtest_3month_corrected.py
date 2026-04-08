"""Backtest BB Mean Reversion strategy on M1 data with corrected ADX (Wilder's sum-based).

Uses the same logic as the live bot:
- BB(20,2) + RSI(14) signals on M1
- ADX(14) regime filter: blocks Transitional (20-30) and Trending (30-50)
- SL = 1.0x ATR(14), TP = 1.5x ATR(14)
- max_open_positions = 1
- min_time_between_trades = 60 seconds
- max_trades_per_day = 20
- spread filter (skipped in backtest - no tick spread in historical bars)
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
import MetaTrader5 as mt5
import pandas as pd
import numpy as np

load_dotenv(".env.bot1", override=True)
mt5.initialize(
    path=os.getenv("MT5_PATH"),
    login=int(os.getenv("MT5_LOGIN")),
    password=os.getenv("MT5_PASSWORD"),
    server=os.getenv("MT5_SERVER"),
)
sym = os.getenv("MT5_SYMBOL", "XAUUSDm")

# Pull all M1 data
rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M1, 0, 99999)
mt5.shutdown()

df = pd.DataFrame(rates)
df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)

# ── Indicators (Wilder's sum-based, matching MT5) ──────────────────────────

def calc_adx(high, low, close, period=14):
    n = len(high)
    tr_raw = np.zeros(n)
    pdm_raw = np.zeros(n)
    mdm_raw = np.zeros(n)
    for i in range(1, n):
        hl = high[i] - low[i]
        hc = abs(high[i] - close[i - 1])
        lc = abs(low[i] - close[i - 1])
        tr_raw[i] = max(hl, hc, lc)
        up = high[i] - high[i - 1]
        dn = low[i - 1] - low[i]
        if up < 0: up = 0.0
        if dn < 0: dn = 0.0
        if up == dn: up = dn = 0.0
        elif up < dn: up = 0.0
        else: dn = 0.0
        pdm_raw[i] = up
        mdm_raw[i] = dn

    adx_out = np.full(n, np.nan)
    seed = period
    if seed >= n:
        return adx_out
    sm_tr = np.sum(tr_raw[1:seed + 1])
    sm_pdm = np.sum(pdm_raw[1:seed + 1])
    sm_mdm = np.sum(mdm_raw[1:seed + 1])
    dx_buf = np.full(n, np.nan)

    if sm_tr > 0:
        pdi = 100.0 * sm_pdm / sm_tr
        mdi = 100.0 * sm_mdm / sm_tr
    else:
        pdi = mdi = 0.0
    di_sum = pdi + mdi
    dx_buf[seed] = (100.0 * abs(pdi - mdi) / di_sum) if di_sum > 0 else 0.0

    for i in range(seed + 1, n):
        sm_tr = sm_tr - sm_tr / period + tr_raw[i]
        sm_pdm = sm_pdm - sm_pdm / period + pdm_raw[i]
        sm_mdm = sm_mdm - sm_mdm / period + mdm_raw[i]
        if sm_tr > 0:
            pdi = 100.0 * sm_pdm / sm_tr
            mdi = 100.0 * sm_mdm / sm_tr
        else:
            pdi = mdi = 0.0
        di_sum = pdi + mdi
        dx_buf[i] = (100.0 * abs(pdi - mdi) / di_sum) if di_sum > 0 else 0.0

    adx_seed_end = seed + period
    if adx_seed_end > n:
        return adx_out
    adx_val = np.mean(dx_buf[seed:adx_seed_end])
    adx_out[adx_seed_end - 1] = adx_val
    for i in range(adx_seed_end, n):
        adx_val = (adx_val * (period - 1) + dx_buf[i]) / period
        adx_out[i] = adx_val
    return adx_out


def calc_rsi(close, period=14):
    delta = np.diff(close, prepend=np.nan)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    # Wilder's EMA
    avg_gain = np.full(len(close), np.nan)
    avg_loss = np.full(len(close), np.nan)
    avg_gain[period] = np.mean(gain[1:period + 1])
    avg_loss[period] = np.mean(loss[1:period + 1])
    for i in range(period + 1, len(close)):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain[i]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss[i]) / period
    rs = np.where(avg_loss > 0, avg_gain / avg_loss, 100.0)
    rsi = 100 - (100 / (1 + rs))
    rsi[:period] = np.nan
    return rsi


def calc_atr(high, low, close, period=14):
    n = len(high)
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    atr = np.full(n, np.nan)
    atr[period] = np.mean(tr[1:period + 1])
    for i in range(period + 1, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


# ── Compute indicators ─────────────────────────────────────────────────────

high = df["high"].values
low = df["low"].values
close = df["close"].values

df["adx"] = calc_adx(high, low, close, 14)
df["rsi"] = calc_rsi(close, 14)
df["atr"] = calc_atr(high, low, close, 14)
df["sma20"] = df["close"].rolling(20).mean()
df["std20"] = df["close"].rolling(20).std()
df["bb_upper"] = df["sma20"] + 2 * df["std20"]
df["bb_lower"] = df["sma20"] - 2 * df["std20"]


def regime(adx):
    if np.isnan(adx): return "Unknown"
    if adx < 20: return "Ranging"
    if adx < 30: return "Transitional"
    if adx < 50: return "Trending"
    return "Strong_Trend"


df["regime"] = [regime(a) for a in df["adx"].values]

# ── Bot configs ────────────────────────────────────────────────────────────

BLOCKED = {"Trending", "Transitional"}
BOTS = {
    "Bot1 (London 1%)":  {"start": 7, "end": 13, "risk_pct": 0.01},
    "Bot2 (London 2%)":  {"start": 7, "end": 13, "risk_pct": 0.02},
    "Bot3 (Ldn+NY 1%)":  {"start": 7, "end": 20, "risk_pct": 0.01},
}
STARTING_EQUITY = 1000.0
MAX_OPEN = 1
MIN_TIME_BETWEEN = 60  # seconds
MAX_TRADES_PER_DAY = 20
POINT = 0.001

# ── Simulation ─────────────────────────────────────────────────────────────

times = df["time"].values
highs = df["high"].values
lows = df["low"].values
closes = df["close"].values
adxs = df["adx"].values
rsis = df["rsi"].values
atrs = df["atr"].values
bb_uppers = df["bb_upper"].values
bb_lowers = df["bb_lower"].values
regimes = df["regime"].values

for bot_name, cfg in BOTS.items():
    equity = STARTING_EQUITY
    trades = []
    open_pos = None
    last_trade_time = None
    day_trade_count = {}

    for i in range(50, len(df)):
        t = pd.Timestamp(times[i])
        h = t.hour
        day_key = t.date()

        # Check open position for exit
        if open_pos is not None:
            hit_sl = False
            hit_tp = False
            if open_pos["dir"] == "BUY":
                if lows[i] <= open_pos["sl"]:
                    hit_sl = True
                    exit_price = open_pos["sl"]
                elif highs[i] >= open_pos["tp"]:
                    hit_tp = True
                    exit_price = open_pos["tp"]
            else:
                if highs[i] >= open_pos["sl"]:
                    hit_sl = True
                    exit_price = open_pos["sl"]
                elif lows[i] <= open_pos["tp"]:
                    hit_tp = True
                    exit_price = open_pos["tp"]

            if hit_sl or hit_tp:
                if open_pos["dir"] == "BUY":
                    pnl_price = exit_price - open_pos["entry"]
                else:
                    pnl_price = open_pos["entry"] - exit_price
                # Convert to USD: lot * contract_size * price_change
                # For XAU, 0.01 lot = 1oz, tick_value varies
                # Simplified: pnl = lots * 100 * pnl_price (standard lot = 100oz)
                lots = open_pos["lots"]
                pnl_usd = lots * 100 * pnl_price
                equity += pnl_usd
                trades.append({
                    "open_time": open_pos["time"],
                    "close_time": t,
                    "dir": open_pos["dir"],
                    "entry": open_pos["entry"],
                    "exit": exit_price,
                    "sl": open_pos["sl"],
                    "tp": open_pos["tp"],
                    "lots": lots,
                    "pnl_usd": pnl_usd,
                    "result": "TP" if hit_tp else "SL",
                    "equity_after": equity,
                })
                open_pos = None

        # Session filter
        if h < cfg["start"] or h >= cfg["end"]:
            continue

        # Skip if position already open
        if open_pos is not None:
            continue

        # Max trades per day
        if day_trade_count.get(day_key, 0) >= MAX_TRADES_PER_DAY:
            continue

        # Min time between trades
        if last_trade_time is not None:
            elapsed = (t - last_trade_time).total_seconds()
            if elapsed < MIN_TIME_BETWEEN:
                continue

        # Skip if indicators not ready
        if np.isnan(adxs[i]) or np.isnan(rsis[i]) or np.isnan(atrs[i]):
            continue
        if np.isnan(bb_uppers[i]) or np.isnan(bb_lowers[i]):
            continue

        # Regime filter
        if regimes[i] in BLOCKED:
            continue

        # Signal check
        signal = None
        if lows[i] <= bb_lowers[i] and rsis[i] < 30:
            signal = "BUY"
        elif highs[i] >= bb_uppers[i] and rsis[i] > 70:
            signal = "SELL"

        if signal is None:
            continue

        # Position sizing: risk% of equity
        atr_val = atrs[i]
        sl_dist = atr_val * 1.0
        tp_dist = atr_val * 1.5
        entry = closes[i]

        if signal == "BUY":
            sl = entry - sl_dist
            tp = entry + tp_dist
        else:
            sl = entry + sl_dist
            tp = entry - tp_dist

        # Lots = (equity * risk%) / (sl_distance * 100)
        risk_usd = equity * cfg["risk_pct"]
        lots = risk_usd / (sl_dist * 100)
        lots = max(0.01, round(lots, 2))

        open_pos = {
            "time": t, "dir": signal, "entry": entry,
            "sl": sl, "tp": tp, "lots": lots,
        }
        last_trade_time = t
        day_trade_count[day_key] = day_trade_count.get(day_key, 0) + 1

    # Close any remaining open position at last bar's close
    if open_pos is not None:
        exit_price = closes[-1]
        if open_pos["dir"] == "BUY":
            pnl_price = exit_price - open_pos["entry"]
        else:
            pnl_price = open_pos["entry"] - exit_price
        lots = open_pos["lots"]
        pnl_usd = lots * 100 * pnl_price
        equity += pnl_usd
        trades.append({
            "open_time": open_pos["time"],
            "close_time": pd.Timestamp(times[-1]),
            "dir": open_pos["dir"],
            "entry": open_pos["entry"],
            "exit": exit_price,
            "pnl_usd": pnl_usd,
            "result": "OPEN",
            "equity_after": equity,
        })

    # ── Results ────────────────────────────────────────────────────────────

    print(f"\n{'='*60}")
    print(f"  {bot_name}")
    print(f"  Period: {df['time'].iloc[50].strftime('%Y-%m-%d')} to {df['time'].iloc[-1].strftime('%Y-%m-%d')}")
    print(f"{'='*60}")

    if not trades:
        print("  No trades.")
        continue

    wins = [t for t in trades if t["pnl_usd"] > 0]
    losses = [t for t in trades if t["pnl_usd"] <= 0]
    total_pnl = sum(t["pnl_usd"] for t in trades)
    gross_profit = sum(t["pnl_usd"] for t in wins) if wins else 0
    gross_loss = abs(sum(t["pnl_usd"] for t in losses)) if losses else 0.01
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    wr = len(wins) / len(trades) * 100

    # Max drawdown
    peak = STARTING_EQUITY
    max_dd = 0
    for t in trades:
        eq = t["equity_after"]
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # Monthly breakdown
    monthly = {}
    for t in trades:
        key = t["open_time"].strftime("%Y-%m")
        if key not in monthly:
            monthly[key] = {"trades": 0, "wins": 0, "pnl": 0.0}
        monthly[key]["trades"] += 1
        if t["pnl_usd"] > 0:
            monthly[key]["wins"] += 1
        monthly[key]["pnl"] += t["pnl_usd"]

    print(f"  Total trades:  {len(trades)}")
    print(f"  Wins/Losses:   {len(wins)}W / {len(losses)}L")
    print(f"  Win rate:      {wr:.1f}%")
    print(f"  Total P&L:     ${total_pnl:+.2f} ({total_pnl/STARTING_EQUITY*100:+.1f}%)")
    print(f"  Final equity:  ${equity:.2f}")
    print(f"  Profit factor: {pf:.2f}")
    print(f"  Max drawdown:  {max_dd:.1f}%")
    print(f"  Avg trade:     ${total_pnl/len(trades):+.2f}")
    print()
    print(f"  {'Month':<10} {'Trades':>7} {'WR':>7} {'P&L':>12}")
    print(f"  {'-'*40}")
    for m in sorted(monthly):
        d = monthly[m]
        m_wr = d["wins"] / d["trades"] * 100 if d["trades"] else 0
        print(f"  {m:<10} {d['trades']:>7} {m_wr:>6.1f}% ${d['pnl']:>+10.2f}")

    # Regime distribution of trades
    regime_stats = {}
    for t in trades:
        # Find regime at trade open
        idx = df[df["time"] == t["open_time"]].index
        if len(idx) > 0:
            r = regimes[idx[0]]
            if r not in regime_stats:
                regime_stats[r] = {"count": 0, "wins": 0, "pnl": 0.0}
            regime_stats[r]["count"] += 1
            if t["pnl_usd"] > 0:
                regime_stats[r]["wins"] += 1
            regime_stats[r]["pnl"] += t["pnl_usd"]

    print()
    print(f"  {'Regime':<15} {'Trades':>7} {'WR':>7} {'P&L':>12}")
    print(f"  {'-'*45}")
    for r in sorted(regime_stats):
        d = regime_stats[r]
        r_wr = d["wins"] / d["count"] * 100 if d["count"] else 0
        print(f"  {r:<15} {d['count']:>7} {r_wr:>6.1f}% ${d['pnl']:>+10.2f}")
