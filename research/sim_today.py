"""Simulate today's HFT bot signals — matches exact bot logic."""
import MetaTrader5 as mt5
from dotenv import load_dotenv
import os, pandas as pd, numpy as np
from datetime import datetime, timezone

load_dotenv(".env.bot1")
mt5.initialize(
    path=os.getenv("MT5_PATH"),
    login=int(os.getenv("MT5_LOGIN")),
    password=os.getenv("MT5_PASSWORD"),
    server=os.getenv("MT5_SERVER"),
)
sym = os.getenv("MT5_SYMBOL", "XAUUSDm")
mt5.symbol_select(sym, True)

# Pull M1 data -- enough for warmup + today
rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M1, 0, 2000)
df = pd.DataFrame(rates)
df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)

print(f"Data range: {df['time'].iloc[0]} to {df['time'].iloc[-1]}")
print(f"Total bars: {len(df)}")

# ---------- Indicators (matching exact bot code) ----------

# RSI -- EWM, matching bb_reversion.py _rsi()
delta = df["close"].diff()
gain = delta.clip(lower=0)
loss = -delta.clip(upper=0)
avg_gain = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
avg_loss = loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
rs = avg_gain / avg_loss
df["rsi"] = 100 - (100 / (1 + rs))

# ATR -- EWM, matching bb_reversion.py _atr()
high_low = df["high"] - df["low"]
high_close = (df["high"] - df["close"].shift()).abs()
low_close = (df["low"] - df["close"].shift()).abs()
tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
df["atr"] = tr.ewm(span=14, adjust=False).mean()

# BB(20,2)
df["sma20"] = df["close"].rolling(20).mean()
df["std20"] = df["close"].rolling(20).std()
df["bb_upper"] = df["sma20"] + 2.0 * df["std20"]
df["bb_lower"] = df["sma20"] - 2.0 * df["std20"]

# ADX -- matching regime_detector.py calc_adx() exactly
plus_dm = df["high"].diff()
minus_dm = -df["low"].diff()
plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
plus_di = 100 * (plus_dm.ewm(span=14, adjust=False).mean() / df["atr"])
minus_di = 100 * (minus_dm.ewm(span=14, adjust=False).mean() / df["atr"])
dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
df["adx"] = dx.ewm(span=14, adjust=False).mean()


# Regime classification (matching regime_detector.py)
def classify_regime(adx):
    if pd.isna(adx):
        return "transitional"
    if adx < 20:
        return "ranging"
    if adx < 30:
        return "transitional"
    if adx < 50:
        return "trending"
    return "strong_trend"


df["regime"] = df["adx"].apply(classify_regime)

# ---------- Determine today's date from latest data ----------
today_date = df["time"].iloc[-1].date()
today_bars = df[df["time"].dt.date == today_date]
print(f"\nToday ({today_date}, {today_date.strftime('%A')}):")
print(f"  Bars: {len(today_bars)}")
if len(today_bars) > 0:
    print(f"  Range: {today_bars['time'].iloc[0]} to {today_bars['time'].iloc[-1]}")

# Bot configs
BLOCKED = {"transitional", "trending"}
bots = {
    "Bot1 (London 1%)": {"hours": set(range(7, 13)), "risk_pct": 0.01},
    "Bot2 (London 2%)": {"hours": set(range(7, 13)), "risk_pct": 0.02},
    "Bot3 (LDN+NY 1%)": {"hours": set(range(7, 20)), "risk_pct": 0.01},
}
MAX_OPEN = 1
MIN_INTERVAL_BARS = 1  # 60sec on M1 = at least 1 bar gap
MAX_TRADES_DAY = 20
POINT = 0.001

# ---------- Simulate each bot ----------
for bot_name, cfg in bots.items():
    session_mask = (df["time"].dt.date == today_date) & (
        df["time"].dt.hour.isin(cfg["hours"])
    )
    session_idx = df[session_mask].index.tolist()

    print(f"\n===== {bot_name} =====")

    if not session_idx:
        print("No session bars yet today.")
        continue

    trades = []
    open_trade = None
    last_trade_bar = -999

    for idx in session_idx:
        row = df.loc[idx]

        # Skip if indicators not ready
        if pd.isna(row["adx"]) or pd.isna(row["rsi"]) or pd.isna(row["atr"]):
            continue
        if pd.isna(row["bb_lower"]) or pd.isna(row["bb_upper"]):
            continue
        if row["atr"] <= 0:
            continue

        # Check if open trade hit SL/TP on this bar
        if open_trade is not None:
            t = open_trade
            hit = None
            if t["dir"] == "BUY":
                if row["low"] <= t["sl"]:
                    hit = "SL"
                    exit_p = t["sl"]
                elif row["high"] >= t["tp"]:
                    hit = "TP"
                    exit_p = t["tp"]
            else:
                if row["high"] >= t["sl"]:
                    hit = "SL"
                    exit_p = t["sl"]
                elif row["low"] <= t["tp"]:
                    hit = "TP"
                    exit_p = t["tp"]

            if hit:
                pnl = (
                    (exit_p - t["entry"]) / POINT
                    if t["dir"] == "BUY"
                    else (t["entry"] - exit_p) / POINT
                )
                t["result"] = hit
                t["exit_price"] = exit_p
                t["exit_time"] = row["time"]
                t["pnl_pts"] = pnl
                open_trade = None

        # Can't open new trade if one is open
        if open_trade is not None:
            continue

        # Throttle: min interval between trades
        if idx - last_trade_bar < MIN_INTERVAL_BARS + 1:
            continue

        # Max trades per day
        if len(trades) >= MAX_TRADES_DAY:
            continue

        # Regime filter
        if row["regime"] in BLOCKED:
            continue

        # Signal check (matching bb_reversion.py generate_signal exactly)
        signal = None
        if row["low"] <= row["bb_lower"] and row["rsi"] < 30:
            signal = "BUY"
            sl = row["close"] - 1.0 * row["atr"]
            tp = row["close"] + 1.5 * row["atr"]
        elif row["high"] >= row["bb_upper"] and row["rsi"] > 70:
            signal = "SELL"
            sl = row["close"] + 1.0 * row["atr"]
            tp = row["close"] - 1.5 * row["atr"]

        if signal:
            trade = {
                "time": row["time"],
                "dir": signal,
                "entry": row["close"],
                "sl": sl,
                "tp": tp,
                "atr": row["atr"],
                "rsi": row["rsi"],
                "adx": row["adx"],
                "regime": row["regime"],
                "result": None,
                "exit_price": None,
                "exit_time": None,
                "pnl_pts": None,
            }
            trades.append(trade)
            open_trade = trade
            last_trade_bar = idx

    # Check still-open trades against remaining bars after session
    if open_trade is not None:
        remaining = df.loc[session_idx[-1] + 1 :]
        for _, row in remaining.iterrows():
            t = open_trade
            hit = None
            if t["dir"] == "BUY":
                if row["low"] <= t["sl"]:
                    hit = "SL"
                    exit_p = t["sl"]
                elif row["high"] >= t["tp"]:
                    hit = "TP"
                    exit_p = t["tp"]
            else:
                if row["high"] >= t["sl"]:
                    hit = "SL"
                    exit_p = t["sl"]
                elif row["low"] <= t["tp"]:
                    hit = "TP"
                    exit_p = t["tp"]
            if hit:
                pnl = (
                    (exit_p - t["entry"]) / POINT
                    if t["dir"] == "BUY"
                    else (t["entry"] - exit_p) / POINT
                )
                t["result"] = hit
                t["exit_price"] = exit_p
                t["exit_time"] = row["time"]
                t["pnl_pts"] = pnl
                open_trade = None
                break

        # If still open, mark with current price
        if open_trade is not None:
            last_bar = df.iloc[-1]
            pnl = (
                (last_bar["close"] - open_trade["entry"]) / POINT
                if open_trade["dir"] == "BUY"
                else (open_trade["entry"] - last_bar["close"]) / POINT
            )
            open_trade["result"] = "OPEN"
            open_trade["exit_price"] = last_bar["close"]
            open_trade["exit_time"] = last_bar["time"]
            open_trade["pnl_pts"] = pnl

    # Print results
    session_bars = df.loc[session_idx]
    regime_counts = session_bars["regime"].value_counts()
    eligible = session_bars[~session_bars["regime"].isin(BLOCKED)]
    print(f"Session bars: {len(session_idx)} | Eligible: {len(eligible)}")
    print(f"Regimes: {dict(regime_counts)}")

    if not trades:
        print("No signals triggered yet.")
    else:
        for t in trades:
            status = t["result"] if t["result"] else "PENDING"
            exit_str = ""
            if t["result"]:
                exit_str = f" -> {status} @ {t['exit_price']:.2f} ({t['exit_time'].strftime('%H:%M')}) | {t['pnl_pts']:+.1f} pts"
            print(
                f"  {t['time'].strftime('%H:%M')} {t['dir']} @ {t['entry']:.2f} "
                f"| SL={t['sl']:.2f} TP={t['tp']:.2f} "
                f"| RSI={t['rsi']:.1f} ADX={t['adx']:.1f} ({t['regime']})"
                f"{exit_str}"
            )

        closed = [t for t in trades if t["result"] in ("SL", "TP")]
        if closed:
            wins = sum(1 for t in closed if t["pnl_pts"] > 0)
            total_pts = sum(t["pnl_pts"] for t in closed)
            print(
                f"Closed: {len(closed)} trades, {wins}W/{len(closed)-wins}L, {total_pts:+.1f} pts"
            )
        still_open = [t for t in trades if t["result"] in ("OPEN", None)]
        if still_open:
            for t in still_open:
                print(f"  ** STILL OPEN: {t['dir']} @ {t['entry']:.2f} | P&L: {t['pnl_pts']:+.1f} pts")

mt5.shutdown()
