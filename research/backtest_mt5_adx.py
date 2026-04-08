"""Backtest BB Mean Reversion using MT5's exact exported ADX values.

ADX comes from Common/Files/adx_export.csv (exported via ExportADX.mq5).
RSI, ATR, BB calculated in Python with Wilder's smoothing.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
import MetaTrader5 as mt5
import pandas as pd
import numpy as np

# ── Load MT5 ADX export ────────────────────────────────────────────────────

COMMON = os.path.expanduser("~/AppData/Roaming/MetaQuotes/Terminal/Common/Files")
mt5_adx = pd.read_csv(os.path.join(COMMON, "adx_export.csv"), encoding="utf-16")
mt5_adx.columns = mt5_adx.columns.str.strip()
mt5_adx["time"] = pd.to_datetime(mt5_adx["time"])

# ── Load M1 OHLCV ─────────────────────────────────────────────────────────

load_dotenv(".env.bot1", override=True)
mt5.initialize(
    path=os.getenv("MT5_PATH"),
    login=int(os.getenv("MT5_LOGIN")),
    password=os.getenv("MT5_PASSWORD"),
    server=os.getenv("MT5_SERVER"),
)
sym = os.getenv("MT5_SYMBOL", "XAUUSDm")
rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M1, 0, 99999)
mt5.shutdown()

df = pd.DataFrame(rates)
df["time"] = pd.to_datetime(df["time"], unit="s", utc=True).dt.tz_localize(None)

# Merge MT5 ADX
df = pd.merge(df, mt5_adx[["time", "adx"]], on="time", how="inner")
print(f"Merged: {len(df)} bars | {df['time'].iloc[0].strftime('%Y-%m-%d')} to {df['time'].iloc[-1].strftime('%Y-%m-%d')}")

# ── Indicators ─────────────────────────────────────────────────────────────

close = df["close"].values
high = df["high"].values
low = df["low"].values
n = len(close)

# RSI(14) - Wilder's
delta = np.diff(close, prepend=np.nan)
gain = np.where(delta > 0, delta, 0.0)
loss_a = np.where(delta < 0, -delta, 0.0)
avg_g = np.full(n, np.nan)
avg_l = np.full(n, np.nan)
avg_g[14] = np.mean(gain[1:15])
avg_l[14] = np.mean(loss_a[1:15])
for i in range(15, n):
    avg_g[i] = (avg_g[i - 1] * 13 + gain[i]) / 14
    avg_l[i] = (avg_l[i - 1] * 13 + loss_a[i]) / 14
rs = np.where(avg_l > 0, avg_g / avg_l, 100.0)
rsi = 100 - (100 / (1 + rs))
rsi[:14] = np.nan
df["rsi"] = rsi

# ATR(14) - Wilder's
tr_a = np.zeros(n)
for i in range(1, n):
    tr_a[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
atr = np.full(n, np.nan)
atr[14] = np.mean(tr_a[1:15])
for i in range(15, n):
    atr[i] = (atr[i - 1] * 13 + tr_a[i]) / 14
df["atr"] = atr

# BB(20,2)
df["sma20"] = df["close"].rolling(20).mean()
df["std20"] = df["close"].rolling(20).std()
df["bb_upper"] = df["sma20"] + 2 * df["std20"]
df["bb_lower"] = df["sma20"] - 2 * df["std20"]

# Regime
def regime(a):
    if np.isnan(a):
        return "Unknown"
    if a < 20:
        return "Ranging"
    if a < 30:
        return "Transitional"
    if a < 50:
        return "Trending"
    return "Strong_Trend"

df["regime"] = [regime(a) for a in df["adx"].values]

# Print regime distribution
rc = df["regime"].value_counts()
print("\nRegime distribution across all bars:")
for r, c in rc.items():
    print(f"  {r}: {c} bars ({c / len(df) * 100:.1f}%)")

# ── Simulation ─────────────────────────────────────────────────────────────

BLOCKED = {"Trending", "Transitional"}
BOTS = {
    "Bot1 (London 1%)": {"start": 7, "end": 13, "risk_pct": 0.01},
    "Bot2 (London 2%)": {"start": 7, "end": 13, "risk_pct": 0.02},
    "Bot3 (Ldn+NY 1%)": {"start": 7, "end": 20, "risk_pct": 0.01},
}
STARTING_EQUITY = 1000.0
MIN_TIME_BETWEEN = 60
MAX_TRADES_PER_DAY = 20

times = df["time"].values
highs = df["high"].values
lows = df["low"].values
closes = df["close"].values
adxs = df["adx"].values
rsis = df["rsi"].values
atrs = df["atr"].values
bb_ups = df["bb_upper"].values
bb_los = df["bb_lower"].values
regimes_a = df["regime"].values

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

        # Check SL/TP on open position
        if open_pos is not None:
            hit_sl = hit_tp = False
            exit_p = 0.0
            if open_pos["dir"] == "BUY":
                if lows[i] <= open_pos["sl"]:
                    hit_sl = True
                    exit_p = open_pos["sl"]
                elif highs[i] >= open_pos["tp"]:
                    hit_tp = True
                    exit_p = open_pos["tp"]
            else:
                if highs[i] >= open_pos["sl"]:
                    hit_sl = True
                    exit_p = open_pos["sl"]
                elif lows[i] <= open_pos["tp"]:
                    hit_tp = True
                    exit_p = open_pos["tp"]

            if hit_sl or hit_tp:
                if open_pos["dir"] == "BUY":
                    pnl_price = exit_p - open_pos["entry"]
                else:
                    pnl_price = open_pos["entry"] - exit_p
                pnl_usd = open_pos["lots"] * 100 * pnl_price
                equity += pnl_usd
                trades.append({
                    "open_time": open_pos["time"],
                    "close_time": t,
                    "dir": open_pos["dir"],
                    "entry": open_pos["entry"],
                    "exit": exit_p,
                    "lots": open_pos["lots"],
                    "pnl_usd": pnl_usd,
                    "result": "TP" if hit_tp else "SL",
                    "equity_after": equity,
                    "regime": open_pos["regime"],
                })
                open_pos = None

        # Filters
        if h < cfg["start"] or h >= cfg["end"]:
            continue
        if open_pos is not None:
            continue
        if day_trade_count.get(day_key, 0) >= MAX_TRADES_PER_DAY:
            continue
        if last_trade_time is not None:
            if (t - last_trade_time).total_seconds() < MIN_TIME_BETWEEN:
                continue
        if np.isnan(adxs[i]) or np.isnan(rsis[i]) or np.isnan(atrs[i]):
            continue
        if np.isnan(bb_ups[i]) or np.isnan(bb_los[i]):
            continue
        if regimes_a[i] in BLOCKED:
            continue

        # Signal
        signal = None
        if lows[i] <= bb_los[i] and rsis[i] < 30:
            signal = "BUY"
        elif highs[i] >= bb_ups[i] and rsis[i] > 70:
            signal = "SELL"
        if signal is None:
            continue

        # Position sizing
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

        risk_usd = equity * cfg["risk_pct"]
        lots = max(0.01, round(risk_usd / (sl_dist * 100), 2))

        open_pos = {
            "time": t, "dir": signal, "entry": entry,
            "sl": sl, "tp": tp, "lots": lots, "regime": regimes_a[i],
        }
        last_trade_time = t
        day_trade_count[day_key] = day_trade_count.get(day_key, 0) + 1

    # Close remaining open position
    if open_pos is not None:
        exit_p = closes[-1]
        if open_pos["dir"] == "BUY":
            pnl_price = exit_p - open_pos["entry"]
        else:
            pnl_price = open_pos["entry"] - exit_p
        pnl_usd = open_pos["lots"] * 100 * pnl_price
        equity += pnl_usd
        trades.append({
            "open_time": open_pos["time"],
            "close_time": pd.Timestamp(times[-1]),
            "dir": open_pos["dir"],
            "entry": open_pos["entry"],
            "exit": exit_p,
            "lots": open_pos["lots"],
            "pnl_usd": pnl_usd,
            "result": "OPEN",
            "equity_after": equity,
            "regime": open_pos.get("regime", ""),
        })

    # ── Print results ──────────────────────────────────────────────────────

    print(f"\n{'=' * 60}")
    print(f"  {bot_name}")
    print(f"  Period: {df['time'].iloc[50].strftime('%Y-%m-%d')} to "
          f"{df['time'].iloc[-1].strftime('%Y-%m-%d')}")
    print(f"{'=' * 60}")

    if not trades:
        print("  No trades.")
        continue

    wins = [t for t in trades if t["pnl_usd"] > 0]
    losses = [t for t in trades if t["pnl_usd"] <= 0]
    total_pnl = sum(t["pnl_usd"] for t in trades)
    gross_profit = sum(t["pnl_usd"] for t in wins) if wins else 0
    gross_loss = abs(sum(t["pnl_usd"] for t in losses)) if losses else 0.01
    pf = gross_profit / gross_loss
    wr = len(wins) / len(trades) * 100

    peak = STARTING_EQUITY
    max_dd = 0
    for t in trades:
        if t["equity_after"] > peak:
            peak = t["equity_after"]
        dd = (peak - t["equity_after"]) / peak * 100
        if dd > max_dd:
            max_dd = dd

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
    print(f"  Total P&L:     ${total_pnl:+.2f} ({total_pnl / STARTING_EQUITY * 100:+.1f}%)")
    print(f"  Final equity:  ${equity:.2f}")
    print(f"  Profit factor: {pf:.2f}")
    print(f"  Max drawdown:  {max_dd:.1f}%")
    print(f"  Avg trade:     ${total_pnl / len(trades):+.2f}")
    print()
    print(f"  {'Month':<10} {'Trades':>7} {'WR':>7} {'P&L':>12}")
    print(f"  {'-' * 40}")
    for m in sorted(monthly):
        d = monthly[m]
        m_wr = d["wins"] / d["trades"] * 100 if d["trades"] else 0
        print(f"  {m:<10} {d['trades']:>7} {m_wr:>6.1f}% ${d['pnl']:>+10.2f}")

    # Regime breakdown
    regime_stats = {}
    for t in trades:
        r = t.get("regime", "Unknown")
        if r not in regime_stats:
            regime_stats[r] = {"count": 0, "wins": 0, "pnl": 0.0}
        regime_stats[r]["count"] += 1
        if t["pnl_usd"] > 0:
            regime_stats[r]["wins"] += 1
        regime_stats[r]["pnl"] += t["pnl_usd"]

    print()
    print(f"  {'Regime':<15} {'Trades':>7} {'WR':>7} {'P&L':>12}")
    print(f"  {'-' * 45}")
    for r in sorted(regime_stats):
        d = regime_stats[r]
        r_wr = d["wins"] / d["count"] * 100 if d["count"] else 0
        print(f"  {r:<15} {d['count']:>7} {r_wr:>6.1f}% ${d['pnl']:>+10.2f}")
