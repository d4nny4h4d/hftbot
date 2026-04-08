"""Backtest BB Mean Reversion on 5 target dates with corrected MT5-matching indicators.

Indicators:
  - RSI(14): Wilder's smoothing (alpha=1/period) -- matches MT5 iRSI
  - ATR(14): Wilder's smoothing (alpha=1/period) -- matches MT5 iATR
  - ADX(14): span-based EMA (ewm(span=period)) -- matches MT5 iADX
  - BB(20,2): SMA + 2*std -- matches MT5 iBands

Constraints (matching bot config):
  - max_open_positions: 1 (only 1 trade at a time)
  - min_time_between_trades: 60s
  - max_trades_per_day: 20
  - Blocked regimes: Transitional (ADX 20-30), Trending (ADX 30-50)
"""

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

# Pull M1 data from late Feb for warmup through mid-March
start = datetime(2026, 2, 25, 0, 0, tzinfo=timezone.utc)
end = datetime(2026, 3, 17, 23, 59, tzinfo=timezone.utc)
rates = mt5.copy_rates_range(sym, mt5.TIMEFRAME_M1, start, end)
df = pd.DataFrame(rates)
df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
print(f"Data: {df['time'].iloc[0]} to {df['time'].iloc[-1]} ({len(df)} bars)")

close = df["close"]

# --- BB(20,2) ---
df["sma20"] = close.rolling(20).mean()
df["std20"] = close.rolling(20).std()
df["bb_upper"] = df["sma20"] + 2 * df["std20"]
df["bb_lower"] = df["sma20"] - 2 * df["std20"]

# --- RSI(14) - Wilder's smoothing ---
delta = close.diff()
gain = delta.clip(lower=0)
loss = -delta.clip(upper=0)
avg_gain = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
avg_loss = loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
rs = avg_gain / avg_loss
df["rsi"] = 100 - (100 / (1 + rs))

# --- ATR(14) - Wilder's smoothing ---
tr = np.maximum(
    df["high"] - df["low"],
    np.maximum(
        abs(df["high"] - df["close"].shift(1)),
        abs(df["low"] - df["close"].shift(1)),
    ),
)
df["atr"] = tr.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()

# --- ADX(14) - span-based EMA (matches MT5) ---
plus_dm = df["high"].diff()
minus_dm = -df["low"].diff()
plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
tr2 = pd.concat(
    [
        df["high"] - df["low"],
        (df["high"] - df["close"].shift(1)).abs(),
        (df["low"] - df["close"].shift(1)).abs(),
    ],
    axis=1,
).max(axis=1)
atr2 = tr2.ewm(span=14, adjust=False).mean()
plus_di = 100 * (plus_dm.ewm(span=14, adjust=False).mean() / atr2)
minus_di = 100 * (minus_dm.ewm(span=14, adjust=False).mean() / atr2)
dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
df["adx"] = dx.ewm(span=14, adjust=False).mean()


def regime(adx):
    if pd.isna(adx):
        return "Unknown"
    if adx < 20:
        return "Ranging"
    if adx < 30:
        return "Transitional"
    if adx < 50:
        return "Trending"
    return "Strong_Trend"


df["regime"] = df["adx"].apply(regime)
BLOCKED = {"Trending", "Transitional"}

# Bot configs
bots = {
    "Bot1 (London 1%)": {"start": 7, "end": 13, "risk_pct": 0.01},
    "Bot2 (London 2%)": {"start": 7, "end": 13, "risk_pct": 0.02},
    "Bot3 (LDN+NY 1%)": {"start": 7, "end": 20, "risk_pct": 0.01},
}

target_dates = [2, 4, 5, 12, 13]
point = 0.001
starting_equity = 1000.0

all_results = {}

for day in target_dates:
    day_df = df[(df["time"].dt.month == 3) & (df["time"].dt.day == day)].copy()
    if len(day_df) == 0:
        print(f"March {day}: NO DATA")
        continue

    print(f"========== MARCH {day} ==========")
    print(
        f"Bars: {len(day_df)} | "
        f"Range: {day_df['time'].iloc[0].strftime('%H:%M')} - "
        f"{day_df['time'].iloc[-1].strftime('%H:%M')}"
    )
    regime_counts = day_df["regime"].value_counts()
    print(f"Regime: {dict(regime_counts)}")
    print()

    for bot_name, cfg in bots.items():
        session = day_df[
            (day_df["time"].dt.hour >= cfg["start"])
            & (day_df["time"].dt.hour < cfg["end"])
        ]

        trades = []
        in_trade = False
        last_trade_time = None
        trade_count = 0

        for i in range(len(session)):
            idx = session.index[i]
            row = df.loc[idx]

            if (
                pd.isna(row["adx"])
                or pd.isna(row["rsi"])
                or pd.isna(row["atr"])
                or pd.isna(row["bb_lower"])
            ):
                continue

            # Manage open trade: check SL/TP
            if in_trade:
                t = trades[-1]
                if t["dir"] == "BUY":
                    if row["low"] <= t["sl"]:
                        t["result"] = "SL"
                        t["exit_price"] = t["sl"]
                        t["exit_time"] = row["time"]
                        in_trade = False
                    elif row["high"] >= t["tp"]:
                        t["result"] = "TP"
                        t["exit_price"] = t["tp"]
                        t["exit_time"] = row["time"]
                        in_trade = False
                else:  # SELL
                    if row["high"] >= t["sl"]:
                        t["result"] = "SL"
                        t["exit_price"] = t["sl"]
                        t["exit_time"] = row["time"]
                        in_trade = False
                    elif row["low"] <= t["tp"]:
                        t["result"] = "TP"
                        t["exit_price"] = t["tp"]
                        t["exit_time"] = row["time"]
                        in_trade = False
                continue  # Don't open new trade while one is open

            # Max trades per day
            if trade_count >= 20:
                continue

            # Throttle: 60s between trades
            if last_trade_time:
                elapsed = (row["time"] - last_trade_time).total_seconds()
                if elapsed < 60:
                    continue

            # Regime filter
            if row["regime"] in BLOCKED:
                continue

            # BUY signal: low touches/pierces lower BB AND RSI < 30
            if row["low"] <= row["bb_lower"] and row["rsi"] < 30:
                entry = row["close"]
                sl = entry - row["atr"] * 1.0
                tp = entry + row["atr"] * 1.5
                trades.append(
                    {
                        "time": row["time"],
                        "dir": "BUY",
                        "entry": entry,
                        "sl": sl,
                        "tp": tp,
                        "rsi": row["rsi"],
                        "adx": row["adx"],
                        "atr": row["atr"],
                        "regime": row["regime"],
                        "result": "OPEN",
                        "exit_price": None,
                        "exit_time": None,
                    }
                )
                in_trade = True
                last_trade_time = row["time"]
                trade_count += 1

            # SELL signal: high touches/pierces upper BB AND RSI > 70
            elif row["high"] >= row["bb_upper"] and row["rsi"] > 70:
                entry = row["close"]
                sl = entry + row["atr"] * 1.0
                tp = entry - row["atr"] * 1.5
                trades.append(
                    {
                        "time": row["time"],
                        "dir": "SELL",
                        "entry": entry,
                        "sl": sl,
                        "tp": tp,
                        "rsi": row["rsi"],
                        "adx": row["adx"],
                        "atr": row["atr"],
                        "regime": row["regime"],
                        "result": "OPEN",
                        "exit_price": None,
                        "exit_time": None,
                    }
                )
                in_trade = True
                last_trade_time = row["time"]
                trade_count += 1

        # Close any remaining open trade at session end
        if in_trade and trades:
            last_bar = session.iloc[-1]
            trades[-1]["exit_price"] = last_bar["close"]
            trades[-1]["exit_time"] = last_bar["time"]
            trades[-1]["result"] = "SESSION_END"

        # Calculate P&L with proper lot sizing
        equity = starting_equity
        cumulative_pnl = 0.0

        for t in trades:
            if t["exit_price"] is not None:
                if t["dir"] == "BUY":
                    t["pnl_pts"] = (t["exit_price"] - t["entry"]) / point
                else:
                    t["pnl_pts"] = (t["entry"] - t["exit_price"]) / point
            else:
                t["pnl_pts"] = 0

            # Lot sizing: risk% of current equity / SL distance in USD
            sl_dist_usd = abs(t["entry"] - t["sl"]) * 100  # 100oz per lot
            if sl_dist_usd > 0:
                lot_size = (equity * cfg["risk_pct"]) / sl_dist_usd
            else:
                lot_size = 0.01
            lot_size = max(0.01, min(1.0, round(lot_size, 2)))
            t["lots"] = lot_size
            t["pnl_usd"] = t["pnl_pts"] * point * 100 * lot_size
            equity += t["pnl_usd"]
            cumulative_pnl += t["pnl_usd"]

        total_trades = len(trades)
        wins = sum(1 for t in trades if t["pnl_pts"] > 0)
        losses = total_trades - wins
        total_pts = sum(t["pnl_pts"] for t in trades)

        print(
            f"  {bot_name}: {total_trades} trades, {wins}W/{losses}L, "
            f"{total_pts:+.0f} pts, ~USD {cumulative_pnl:+.2f} "
            f"({cumulative_pnl / starting_equity * 100:+.2f}%)"
        )
        for t in trades:
            et = t["exit_time"].strftime("%H:%M") if t["exit_time"] else "??"
            print(
                f"    {t['time'].strftime('%H:%M')} {t['dir']} "
                f"@ {t['entry']:.2f} SL={t['sl']:.2f} TP={t['tp']:.2f} | "
                f"RSI={t['rsi']:.1f} ADX={t['adx']:.1f} ({t['regime']}) | "
                f"{t['result']} @ {t['exit_price']:.2f} ({et}) | "
                f"{t['pnl_pts']:+.0f}pts {t['lots']:.2f}L "
                f"USD{t['pnl_usd']:+.2f}"
            )

        all_results[(day, bot_name)] = {
            "trades": total_trades,
            "wins": wins,
            "losses": losses,
            "pts": total_pts,
            "usd": cumulative_pnl,
        }

    print()

# Grand summary
print("=" * 85)
print("GRAND SUMMARY (Corrected indicators matching MT5)")
print("=" * 85)
print(
    f"{'Date':>10} {'Bot':>20} {'Trades':>7} {'Wins':>5} "
    f"{'Losses':>7} {'Points':>8} {'USD':>10} {'WR':>6}"
)
for day in target_dates:
    for bot_name in bots:
        key = (day, bot_name)
        if key in all_results:
            r = all_results[key]
            wr = r["wins"] / r["trades"] * 100 if r["trades"] > 0 else 0
            print(
                f"  Mar {day:>2}  {bot_name:>20} {r['trades']:>7} "
                f"{r['wins']:>5} {r['losses']:>7} {r['pts']:>+8.0f} "
                f"{r['usd']:>+10.2f} {wr:>5.0f}%"
            )

# Per-bot totals
print()
for bot_name in bots:
    total_t = sum(
        all_results.get((d, bot_name), {}).get("trades", 0) for d in target_dates
    )
    total_w = sum(
        all_results.get((d, bot_name), {}).get("wins", 0) for d in target_dates
    )
    total_l = sum(
        all_results.get((d, bot_name), {}).get("losses", 0) for d in target_dates
    )
    total_p = sum(
        all_results.get((d, bot_name), {}).get("pts", 0) for d in target_dates
    )
    total_u = sum(
        all_results.get((d, bot_name), {}).get("usd", 0) for d in target_dates
    )
    wr = total_w / total_t * 100 if total_t > 0 else 0
    print(
        f"TOTAL {bot_name:>20}: {total_t} trades, {total_w}W/{total_l}L, "
        f"WR={wr:.0f}%, {total_p:+.0f}pts, "
        f"USD {total_u:+.2f} ({total_u / starting_equity * 100:+.2f}%)"
    )

mt5.shutdown()
