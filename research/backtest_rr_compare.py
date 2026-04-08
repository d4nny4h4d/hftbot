"""Compare RR ratios: 1:1.5 (current) vs 1:2 using MT5's exact ADX values."""

from dotenv import load_dotenv
import os, sys
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import timezone

load_dotenv(".env.bot3", override=True)
mt5.initialize(
    path=os.getenv("MT5_PATH"),
    login=int(os.getenv("MT5_LOGIN")),
    password=os.getenv("MT5_PASSWORD"),
    server=os.getenv("MT5_SERVER"),
)
sym = os.getenv("MT5_SYMBOL", "XAUUSDm")

# Load MT5 exported ADX
adx_path = os.path.expanduser(
    "~/AppData/Roaming/MetaQuotes/Terminal/Common/Files/adx_export.csv"
)
adx_df = pd.read_csv(adx_path, encoding="utf-16")
adx_df.columns = [c.strip() for c in adx_df.columns]
adx_df["time"] = pd.to_datetime(adx_df["time"])
print(f"ADX export: {len(adx_df)} bars, {adx_df['time'].iloc[0]} to {adx_df['time'].iloc[-1]}")

# Get M1 data (max available)
rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M1, 0, 99999)
df = pd.DataFrame(rates)
df["time"] = pd.to_datetime(df["time"], unit="s", utc=True).dt.tz_localize(None)
print(f"M1 data: {len(df)} bars")

# Merge ADX
df = pd.merge(df, adx_df[["time", "adx"]], on="time", how="left")
df["adx"] = df["adx"].ffill()

# BB(20,2)
df["sma20"] = df["close"].rolling(20).mean()
df["std20"] = df["close"].rolling(20).std()
df["bb_upper"] = df["sma20"] + 2 * df["std20"]
df["bb_lower"] = df["sma20"] - 2 * df["std20"]

# RSI(14) Wilder
delta = df["close"].diff()
gain = delta.clip(lower=0)
loss_s = -delta.clip(upper=0)
avg_gain = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
avg_loss = loss_s.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
rs = avg_gain / avg_loss
df["rsi"] = 100 - (100 / (1 + rs))

# ATR(14) Wilder
tr = np.maximum(
    df["high"] - df["low"],
    np.maximum(
        abs(df["high"] - df["close"].shift(1)),
        abs(df["low"] - df["close"].shift(1)),
    ),
)
df["atr"] = tr.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()

# Regime
def regime(a):
    if pd.isna(a):
        return "Unknown"
    if a < 20:
        return "Ranging"
    if a < 30:
        return "Transitional"
    if a < 50:
        return "Trending"
    return "Strong_Trend"

df["regime"] = df["adx"].apply(regime)

BLOCKED = {"Trending", "Transitional"}

# Bot configs
bots = {
    "Bot1 (London 1%)": {"start": 7, "end": 13, "risk_pct": 0.01},
    "Bot2 (London 2%)": {"start": 7, "end": 13, "risk_pct": 0.02},
    "Bot3 (Ldn+NY 1%)": {"start": 7, "end": 20, "risk_pct": 0.01},
}

# RR configs to compare
rr_configs = {
    "1:1.5 (current)": {"sl_mult": 1.0, "tp_mult": 1.5},
    "1:1.25 (Bot3 test)": {"sl_mult": 1.0, "tp_mult": 1.25},
}

# Get unique trading days (weekdays only)
df["date"] = df["time"].dt.date
trading_days = sorted(d for d in df["date"].unique() if pd.Timestamp(d).weekday() < 5)
print(f"Trading days: {len(trading_days)} ({trading_days[0]} to {trading_days[-1]})")
print()


def run_backtest(day_data_full, session_df, sl_mult, tp_mult):
    """Run backtest on one session. Returns list of trade dicts."""
    trades = []
    open_trade = None

    for idx in session_df.index:
        r = day_data_full.loc[idx]

        # Check if open trade hits SL/TP
        if open_trade is not None:
            if open_trade["dir"] == "BUY":
                if r["low"] <= open_trade["sl"]:
                    open_trade["result"] = "SL"
                    open_trade["pnl_pts"] = (open_trade["sl"] - open_trade["entry"]) / 0.001
                    trades.append(open_trade)
                    open_trade = None
                elif r["high"] >= open_trade["tp"]:
                    open_trade["result"] = "TP"
                    open_trade["pnl_pts"] = (open_trade["tp"] - open_trade["entry"]) / 0.001
                    trades.append(open_trade)
                    open_trade = None
            else:  # SELL
                if r["high"] >= open_trade["sl"]:
                    open_trade["result"] = "SL"
                    open_trade["pnl_pts"] = (open_trade["entry"] - open_trade["sl"]) / 0.001
                    trades.append(open_trade)
                    open_trade = None
                elif r["low"] <= open_trade["tp"]:
                    open_trade["result"] = "TP"
                    open_trade["pnl_pts"] = (open_trade["entry"] - open_trade["tp"]) / 0.001
                    trades.append(open_trade)
                    open_trade = None

        # Skip if already in a trade
        if open_trade is not None:
            continue

        # Check signals
        if pd.isna(r["bb_lower"]) or pd.isna(r["rsi"]) or pd.isna(r["atr"]) or pd.isna(r["adx"]):
            continue
        if r["regime"] in BLOCKED:
            continue

        if r["close"] < r["bb_lower"] and r["rsi"] < 30:
            entry = r["close"]
            open_trade = {
                "time": r["time"],
                "dir": "BUY",
                "entry": entry,
                "sl": entry - sl_mult * r["atr"],
                "tp": entry + tp_mult * r["atr"],
                "atr": r["atr"],
            }
        elif r["close"] > r["bb_upper"] and r["rsi"] > 70:
            entry = r["close"]
            open_trade = {
                "time": r["time"],
                "dir": "SELL",
                "entry": entry,
                "sl": entry + sl_mult * r["atr"],
                "tp": entry - tp_mult * r["atr"],
                "atr": r["atr"],
            }

    # Close open trade at session end
    if open_trade is not None:
        last = session_df.iloc[-1]
        if open_trade["dir"] == "BUY":
            open_trade["pnl_pts"] = (day_data_full.loc[last.name, "close"] - open_trade["entry"]) / 0.001
        else:
            open_trade["pnl_pts"] = (open_trade["entry"] - day_data_full.loc[last.name, "close"]) / 0.001
        open_trade["result"] = "EOD"
        trades.append(open_trade)

    return trades


for rr_name, rr in rr_configs.items():
    print(f"====== {rr_name} (SL={rr['sl_mult']}xATR, TP={rr['tp_mult']}xATR) ======")

    # For 1:1.25 test, only run Bot3 at 2% risk
    if "1:1.25" in rr_name:
        test_bots = {"Bot3 (Ldn+NY 2%)": {"start": 7, "end": 20, "risk_pct": 0.02}}
    else:
        test_bots = bots

    for bot_name, cfg in test_bots.items():
        all_trades = []

        for day in trading_days:
            day_df = df[df["date"] == day]
            session = day_df[
                (day_df["time"].dt.hour >= cfg["start"])
                & (day_df["time"].dt.hour < cfg["end"])
            ]
            if session.empty:
                continue
            trades = run_backtest(df, session, rr["sl_mult"], rr["tp_mult"])
            all_trades.extend(trades)

        if not all_trades:
            print(f"  {bot_name}: No trades")
            continue

        total = len(all_trades)
        wins = sum(1 for t in all_trades if t["pnl_pts"] > 0)
        losses = total - wins
        wr = wins / total * 100
        total_pts = sum(t["pnl_pts"] for t in all_trades)
        avg_win = np.mean([t["pnl_pts"] for t in all_trades if t["pnl_pts"] > 0]) if wins else 0
        avg_loss_val = abs(np.mean([t["pnl_pts"] for t in all_trades if t["pnl_pts"] <= 0])) if losses else 0

        win_pts = sum(t["pnl_pts"] for t in all_trades if t["pnl_pts"] > 0)
        loss_pts = abs(sum(t["pnl_pts"] for t in all_trades if t["pnl_pts"] <= 0))
        pf = win_pts / loss_pts if loss_pts > 0 else float("inf")

        # Max consecutive losses
        max_cl = 0
        curr = 0
        for t in all_trades:
            if t["pnl_pts"] <= 0:
                curr += 1
                max_cl = max(max_cl, curr)
            else:
                curr = 0

        tp_count = sum(1 for t in all_trades if t.get("result") == "TP")
        sl_count = sum(1 for t in all_trades if t.get("result") == "SL")
        eod_count = sum(1 for t in all_trades if t.get("result") == "EOD")
        trades_per_day = total / len(trading_days)

        # Equity curve simulation (compound)
        equity = 1000.0
        min_eq = equity
        max_dd_pct = 0
        for t in all_trades:
            risk_amt = equity * cfg["risk_pct"]
            if avg_loss_val > 0:
                dollar_per_pt = risk_amt / avg_loss_val
                pnl_dollar = t["pnl_pts"] * dollar_per_pt
            else:
                pnl_dollar = 0
            equity += pnl_dollar
            if equity < min_eq:
                min_eq = equity
                dd = (1000 - min_eq) / 1000 * 100
                max_dd_pct = max(max_dd_pct, dd)

        final_return = (equity - 1000) / 1000 * 100

        print(f"  {bot_name}: {total} trades ({trades_per_day:.1f}/day) | WR={wr:.1f}% ({wins}W/{losses}L) | PF={pf:.2f}")
        print(f"    TP={tp_count} SL={sl_count} EOD={eod_count} | Avg Win={avg_win:.0f}pts Avg Loss={avg_loss_val:.0f}pts | Max Consec Loss={max_cl}")
        print(f"    Total pts: {total_pts:+,.0f} | Return: {final_return:+.1f}% | Max DD: {max_dd_pct:.1f}%")
    print()

mt5.shutdown()
print("Done.")
