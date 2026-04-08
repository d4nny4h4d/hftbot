"""
BB Mean Reversion — 1-Year Rolling Backtest (4 x 3-month windows)
==================================================================
Period 1: Dec 2024 - Mar 2025
Period 2: Mar 2025 - Jun 2025
Period 3: Jun 2025 - Sep 2025
Period 4: Sep 2025 - Dec 2025

Uses M5 bars (M1 only available from Dec 2025).
Current strategy config: BLOCKED_REGIMES = {"Trending", "Transitional"}
"""

import datetime as dt
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple

import MetaTrader5 as mt5
import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SYMBOL = "XAUUSDm"
TIMEFRAME = mt5.TIMEFRAME_M1
COMMISSION_PER_LOT = 7.0
SPREAD_POINTS = 20
POINT_VALUE = 0.001
CONTRACT_SIZE = 100
INITIAL_BALANCE = 1_000.0

BLOCKED_REGIMES = {"Trending", "Transitional"}

BOT_CONFIGS = [
    {"name": "Bot 1: London 1%",    "risk_pct": 0.01, "allowed_hours": set(range(7, 13))},
    {"name": "Bot 2: London 2%",    "risk_pct": 0.02, "allowed_hours": set(range(7, 13))},
    {"name": "Bot 3: London+NY 1%", "risk_pct": 0.01, "allowed_hours": set(range(7, 20))},
]

# M1 data available: ~Dec 1 2025 to Mar 16 2026 (~3.5 months)
# Split into monthly windows for granular analysis
# offset = months back from latest data, duration = 1 month each
PERIOD_CONFIGS = [
    {"offset": 3, "duration": 1, "label": "Dec 2025"},
    {"offset": 2, "duration": 1, "label": "Jan 2026"},
    {"offset": 1, "duration": 1, "label": "Feb 2026"},
    {"offset": 0, "duration": 1, "label": "Mar 2026 (partial)"},
]


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def connect_mt5() -> bool:
    if not mt5.initialize():
        print(f"MT5 init failed: {mt5.last_error()}")
        return False
    print(f"MT5 connected – build {mt5.version()}")
    return True


def fetch_all_available() -> pd.DataFrame:
    mt5.symbol_select(SYMBOL, True)
    CHUNK = 50_000
    all_frames = []
    offset = 0
    while offset < 600_000:
        rates = mt5.copy_rates_from_pos(SYMBOL, TIMEFRAME, offset, CHUNK)
        if rates is None or len(rates) == 0:
            break
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.set_index("time", inplace=True)
        df.rename(columns={"tick_volume": "volume"}, inplace=True)
        all_frames.append(df)
        print(f"  Fetched {len(df):,} bars (offset {offset:,})")
        offset += len(df)
        if len(df) < CHUNK:
            break
    if not all_frames:
        raise RuntimeError(f"No data for {SYMBOL}")
    full = pd.concat(all_frames).sort_index()
    full = full[~full.index.duplicated(keep="first")]
    print(f"Total: {len(full):,} bars | {full.index[0]} -> {full.index[-1]}")
    return full


def slice_period(df: pd.DataFrame, end_offset_months: int, duration: int = 3) -> pd.DataFrame:
    latest = df.index[-1]
    end = latest - pd.DateOffset(months=end_offset_months)
    start = end - pd.DateOffset(months=duration)
    return df.loc[start:end]


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------
def sma(s, p): return s.rolling(p).mean()

def rsi(series, period):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ag = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    al = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    return 100 - (100 / (1 + ag / al))

def atr(df, period):
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()

def adx(df, period=14):
    plus_dm = df["high"].diff().clip(lower=0)
    minus_dm = (-df["low"].diff()).clip(lower=0)
    plus_dm[plus_dm < minus_dm] = 0
    minus_dm[minus_dm < plus_dm] = 0
    a = atr(df, period)
    plus_di = 100 * (plus_dm.ewm(span=period, adjust=False).mean() / a)
    minus_di = 100 * (minus_dm.ewm(span=period, adjust=False).mean() / a)
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di) * 100
    return dx.ewm(span=period, adjust=False).mean()

def bollinger_bands(series, period=20, num_std=2.0):
    mid = sma(series, period)
    std = series.rolling(period).std()
    return mid + num_std * std, mid, mid - num_std * std


# ---------------------------------------------------------------------------
# Trade & Regime
# ---------------------------------------------------------------------------
@dataclass
class Trade:
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp = None
    direction: int = 0
    entry_price: float = 0.0
    exit_price: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    lot_size: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    duration_minutes: int = 0
    session: str = ""
    regime: str = ""
    regime_adx: float = 0.0

def classify_session(ts):
    h = ts.hour
    if 7 <= h < 13: return "London"
    elif 13 <= h < 20: return "NY"
    return "Other"

def classify_regime(adx_val):
    if adx_val < 20: return "Ranging"
    elif adx_val < 30: return "Transitional"
    elif adx_val < 50: return "Trending"
    return "Strong Trend"


# ---------------------------------------------------------------------------
# Backtester
# ---------------------------------------------------------------------------
class StrategyBBacktester:
    def __init__(self, df, risk_pct, allowed_hours, label=""):
        self.df = df.copy()
        self.trades = []
        self.balance = INITIAL_BALANCE
        self.equity_curve = []
        self.position = None
        self.risk_pct = risk_pct
        self.allowed_hours = allowed_hours
        self.label = label
        self._precompute()

    def _precompute(self):
        df = self.df
        df["rsi14"] = rsi(df["close"], 14)
        df["atr14"] = atr(df, 14)
        df["adx14"] = adx(df, 14)
        df["bb_upper"], df["bb_mid"], df["bb_lower"] = bollinger_bands(df["close"], 20, 2.0)

    def _calc_lot_size(self, entry, sl):
        risk_amount = self.balance * self.risk_pct
        sl_dist = abs(entry - sl)
        if sl_dist <= 0: return 0.0
        lot = risk_amount / (sl_dist * CONTRACT_SIZE)
        return round(max(0.01, min(lot, 10.0)), 2)

    def _open_position(self, idx, direction, sl, tp):
        if self.position is not None: return
        row = self.df.iloc[idx]
        spread = SPREAD_POINTS * POINT_VALUE
        entry = row["close"] + spread/2 if direction == 1 else row["close"] - spread/2
        lot = self._calc_lot_size(entry, sl)
        if lot <= 0: return
        self.position = Trade(
            entry_time=self.df.index[idx], direction=direction,
            entry_price=entry, sl=sl, tp=tp, lot_size=lot,
            session=classify_session(self.df.index[idx]),
            regime=classify_regime(row["adx14"]), regime_adx=row["adx14"],
        )

    def _check_exit(self, idx):
        if self.position is None: return False
        row = self.df.iloc[idx]
        pos = self.position
        if pos.direction == 1:
            hit_sl = row["low"] <= pos.sl
            hit_tp = row["high"] >= pos.tp
        else:
            hit_sl = row["high"] >= pos.sl
            hit_tp = row["low"] <= pos.tp
        if hit_sl or hit_tp:
            exit_price = pos.sl if (hit_sl and hit_tp) or hit_sl else pos.tp
            self._close_position(idx, exit_price)
            return True
        return False

    def _close_position(self, idx, exit_price):
        pos = self.position
        if pos is None: return
        commission = COMMISSION_PER_LOT * pos.lot_size
        spread_cost = SPREAD_POINTS * POINT_VALUE
        raw_pnl = (exit_price - pos.entry_price) * pos.direction * CONTRACT_SIZE * pos.lot_size
        pos.pnl = raw_pnl - commission - (spread_cost * CONTRACT_SIZE * pos.lot_size * 0.5)
        pos.exit_price = exit_price
        pos.exit_time = self.df.index[idx]
        pos.duration_minutes = int((pos.exit_time - pos.entry_time).total_seconds() / 60)
        pos.pnl_pct = pos.pnl / self.balance * 100
        self.balance += pos.pnl
        self.trades.append(pos)
        self.position = None

    def _signal(self, i):
        df = self.df
        ts = df.index[i]
        if ts.hour not in self.allowed_hours: return None
        row = df.iloc[i]
        adx_val = row["adx14"]
        if not np.isnan(adx_val):
            regime = classify_regime(adx_val)
            if regime in BLOCKED_REGIMES: return None
        close, low, high = row["close"], row["low"], row["high"]
        atr_val = row["atr14"]
        if np.isnan(atr_val) or atr_val <= 0: return None
        if np.isnan(row["bb_lower"]) or np.isnan(row["bb_upper"]): return None
        if low <= row["bb_lower"] and row["rsi14"] < 30:
            return (1, close - 1.0 * atr_val, close + 1.5 * atr_val)
        if high >= row["bb_upper"] and row["rsi14"] > 70:
            return (-1, close + 1.0 * atr_val, close - 1.5 * atr_val)
        return None

    def run(self):
        self.trades = []
        self.balance = INITIAL_BALANCE
        self.equity_curve = [INITIAL_BALANCE]
        self.position = None
        n = len(self.df)
        for i in range(1, n):
            self._check_exit(i)
            self.equity_curve.append(self.balance)
            if self.position is None:
                sig = self._signal(i)
                if sig:
                    self._open_position(i, *sig)
        if self.position is not None:
            self._close_position(n - 1, self.df["close"].iloc[-1])
        return self.trades


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def compute_metrics(trades, equity_curve):
    if not trades: return {"total_trades": 0, "win_rate": 0, "avg_rr": 0, "profit_factor": 0,
                           "max_consec_losses": 0, "max_drawdown_pct": 0, "sharpe_ratio": 0,
                           "avg_trade_duration_min": 0, "avg_lot_size": 0, "total_pnl": 0,
                           "final_balance": INITIAL_BALANCE, "net_return_pct": 0}
    pnls = np.array([t.pnl for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    total = len(pnls)
    win_rate = len(wins) / total * 100
    avg_win = wins.mean() if len(wins) else 0
    avg_loss = abs(losses.mean()) if len(losses) else 1
    avg_rr = avg_win / avg_loss if avg_loss > 0 else 0
    gross_profit = wins.sum() if len(wins) else 0
    gross_loss = abs(losses.sum()) if len(losses) else 1
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    streak = 0; max_cl = 0
    for p in pnls:
        if p < 0: streak += 1; max_cl = max(max_cl, streak)
        else: streak = 0
    eq = np.array(equity_curve)
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak * 100
    max_dd = dd.min()
    avg_dur = np.mean([t.duration_minutes for t in trades])
    avg_lot = np.mean([t.lot_size for t in trades])
    if len(pnls) > 1:
        trades_per_year = (252 * 1440) / max(avg_dur, 1)
        sharpe = (pnls.mean() / pnls.std()) * np.sqrt(trades_per_year) if pnls.std() > 0 else 0
    else:
        sharpe = 0
    return {
        "total_trades": total, "win_rate": round(win_rate, 2),
        "avg_rr": round(avg_rr, 2), "profit_factor": round(pf, 3),
        "max_consec_losses": max_cl, "max_drawdown_pct": round(max_dd, 2),
        "sharpe_ratio": round(sharpe, 3), "avg_trade_duration_min": round(avg_dur, 1),
        "avg_lot_size": round(avg_lot, 3), "total_pnl": round(pnls.sum(), 2),
        "final_balance": round(eq[-1], 2),
        "net_return_pct": round((eq[-1] - INITIAL_BALANCE) / INITIAL_BALANCE * 100, 2),
    }


def regime_breakdown(trades):
    if not trades: return {}
    result = {}
    for regime in ("Ranging", "Transitional", "Trending", "Strong Trend"):
        subset = [t for t in trades if t.regime == regime]
        if not subset: continue
        pnls = np.array([t.pnl for t in subset])
        w = pnls[pnls > 0]; l = pnls[pnls < 0]
        wr = len(w) / len(pnls) * 100
        pf = w.sum() / abs(l.sum()) if len(l) and l.sum() != 0 else float("inf")
        result[regime] = {"trades": len(pnls), "win%": round(wr, 1),
                          "pf": round(pf, 3) if pf != float("inf") else "inf"}
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if not connect_mt5():
        return

    try:
        full_df = fetch_all_available()
    except RuntimeError as e:
        print(f"Data error: {e}")
        mt5.shutdown()
        return

    print(f"\n{'='*90}")
    print(f"  M1 BACKTEST — BB Mean Reversion (Current Strategy)")
    print(f"  Blocked regimes: {BLOCKED_REGIMES}")
    print(f"  Timeframe: M1 | Starting balance: ${INITIAL_BALANCE:,.0f}")
    print(f"{'='*90}")

    all_rows = []

    for pcfg in PERIOD_CONFIGS:
        subset = slice_period(full_df, end_offset_months=pcfg["offset"], duration=pcfg["duration"])

        if len(subset) < 500:
            print(f"\n  [SKIP] Offset {offset}mo — only {len(subset)} bars available")
            continue

        date_range = f"{subset.index[0].strftime('%Y-%m-%d')} -> {subset.index[-1].strftime('%Y-%m-%d')}"
        print(f"\n{'-'*90}")
        print(f"  PERIOD: {pcfg['label']}  ({date_range}, {len(subset):,} M1 bars)")
        print(f"{'-'*90}")

        for cfg in BOT_CONFIGS:
            bt = StrategyBBacktester(
                subset,
                risk_pct=cfg["risk_pct"],
                allowed_hours=cfg["allowed_hours"],
                label=cfg["name"],
            )
            trades = bt.run()
            m = compute_metrics(trades, bt.equity_curve)
            rb = regime_breakdown(trades)

            regime_str = "  ".join(f"{k}: {v['trades']}t/{v['win%']}%WR/PF{v['pf']}" for k, v in rb.items())

            print(f"  {cfg['name']:22s} | {m['total_trades']:3d} trades | "
                  f"Win: {m['win_rate']:5.1f}% | PF: {m['profit_factor']:5.3f} | "
                  f"Sharpe: {m['sharpe_ratio']:7.1f} | MaxDD: {m['max_drawdown_pct']:6.1f}% | "
                  f"P&L: ${m['total_pnl']:>8,.2f} | Ret: {m['net_return_pct']:>7.1f}%")
            if regime_str:
                print(f"  {'':22s}   {regime_str}")

            all_rows.append({
                "period": date_range, "bot": cfg["name"], **m,
            })

    # ── Summary table ─────────────────────────────────────────────────────
    print(f"\n{'='*120}")
    print(f"  FULL YEAR SUMMARY")
    print(f"{'='*120}")

    df_out = pd.DataFrame(all_rows)
    if df_out.empty:
        print("  No data.")
        mt5.shutdown()
        return

    cols = ["period", "bot", "total_trades", "win_rate", "avg_rr", "profit_factor",
            "max_consec_losses", "max_drawdown_pct", "sharpe_ratio", "total_pnl", "net_return_pct"]
    print(df_out[cols].to_string(index=False))

    # ── Averages per bot ──────────────────────────────────────────────────
    print(f"\n{'-'*90}")
    print(f"  AVERAGED ACROSS ALL PERIODS (per bot)")
    print(f"{'-'*90}")

    for bot_name in [c["name"] for c in BOT_CONFIGS]:
        bot_df = df_out[df_out["bot"] == bot_name]
        if bot_df.empty: continue
        print(f"\n  {bot_name}:")
        print(f"    Avg Trades/period:  {bot_df['total_trades'].mean():.0f}")
        print(f"    Avg Win Rate:       {bot_df['win_rate'].mean():.1f}%")
        print(f"    Avg Profit Factor:  {bot_df['profit_factor'].mean():.3f}")
        print(f"    Avg Sharpe:         {bot_df['sharpe_ratio'].mean():.1f}")
        print(f"    Avg MaxDD:          {bot_df['max_drawdown_pct'].mean():.1f}%")
        print(f"    Avg P&L/period:     ${bot_df['total_pnl'].mean():,.2f}")
        print(f"    Avg Return/period:  {bot_df['net_return_pct'].mean():.1f}%")
        print(f"    Total P&L (year):   ${bot_df['total_pnl'].sum():,.2f}")
        # Win/loss periods
        winning = (bot_df['total_pnl'] > 0).sum()
        losing = (bot_df['total_pnl'] <= 0).sum()
        print(f"    Winning periods:    {winning}/{len(bot_df)}")

    # Save
    out_path = "C:/Users/d4nny/AhadAI/HFTbot/research/backtest_yearly.csv"
    df_out.to_csv(out_path, index=False)
    print(f"\nResults saved to {out_path}")

    mt5.shutdown()
    print("Done.")


if __name__ == "__main__":
    main()
