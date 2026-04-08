"""
Strategy B (BB Mean Reversion) – Refined Backtester
=====================================================
Changes from original:
  1. London + NY session filter only (07:00-20:00 UTC)
  2. 2% equity-based risk per trade (dynamic lot sizing)
  3. Multi-period runner: backtests across multiple 3-month windows

Usage:
  python backtester_b_refined.py                   # latest 3 months
  python backtester_b_refined.py --periods 3       # 3 rolling windows
  python backtester_b_refined.py --periods 4       # 4 rolling windows
"""

import argparse
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
TIMEFRAME = mt5.TIMEFRAME_M1        # overridden by --timeframe arg
COMMISSION_PER_LOT = 7.0            # round-turn USD per standard lot
SPREAD_POINTS = 20                  # typical spread in points
POINT_VALUE = 0.001                 # 1 point for XAUUSDm on Exness
CONTRACT_SIZE = 100                 # 100 oz per lot
INITIAL_BALANCE = 1_000.0
RISK_PCT = 0.01                     # 1% equity risk per trade

# Session filter: London only (07-13 UTC)
ALLOWED_HOURS = set(range(7, 13))   # 07:00 - 12:59 UTC

# Regime filter: exclude normal "Trending" (ADX 30-50)
# Allow: Ranging (<20), Transitional (20-30), Strong Trend (50+)
BLOCKED_REGIMES = {"Trending", "Transitional"}

TF_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
}


# ---------------------------------------------------------------------------
# Data Acquisition
# ---------------------------------------------------------------------------

def connect_mt5() -> bool:
    if not mt5.initialize():
        print(f"MT5 init failed: {mt5.last_error()}")
        return False
    print(f"MT5 connected – build {mt5.version()}")
    return True


def fetch_data_chunk(offset: int, count: int) -> Optional[pd.DataFrame]:
    """Fetch a single chunk from MT5."""
    rates = mt5.copy_rates_from_pos(SYMBOL, TIMEFRAME, offset, count)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    df.rename(columns={"tick_volume": "volume"}, inplace=True)
    return df


def fetch_all_available() -> pd.DataFrame:
    """Fetch as much M1 data as MT5 will give us (in 50K chunks)."""
    mt5.symbol_select(SYMBOL, True)
    CHUNK = 50_000
    all_frames = []
    offset = 0
    max_bars = 600_000  # ~1 year of M1

    while offset < max_bars:
        chunk = fetch_data_chunk(offset, CHUNK)
        if chunk is None or len(chunk) == 0:
            break
        all_frames.append(chunk)
        print(f"  Fetched {len(chunk):,} bars (offset {offset:,})")
        offset += len(chunk)
        if len(chunk) < CHUNK:
            break

    if not all_frames:
        raise RuntimeError(f"No data for {SYMBOL}: {mt5.last_error()}")

    df = pd.concat(all_frames).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    print(f"Total: {len(df):,} bars | {df.index[0]} -> {df.index[-1]}")
    return df


def slice_period(df: pd.DataFrame, end_offset_months: int, duration_months: int = 3) -> pd.DataFrame:
    """Slice a 3-month window ending `end_offset_months` months before the latest bar."""
    latest = df.index[-1]
    end_date = latest - pd.DateOffset(months=end_offset_months)
    start_date = end_date - pd.DateOffset(months=duration_months)
    subset = df.loc[start_date:end_date]
    return subset


# ---------------------------------------------------------------------------
# Technical Indicators
# ---------------------------------------------------------------------------

def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(df: pd.DataFrame, period: int) -> pd.Series:
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    plus_dm = df["high"].diff().clip(lower=0)
    minus_dm = (-df["low"].diff()).clip(lower=0)
    plus_dm[plus_dm < minus_dm] = 0
    minus_dm[minus_dm < plus_dm] = 0
    atr_vals = atr(df, period)
    plus_di = 100 * (plus_dm.ewm(span=period, adjust=False).mean() / atr_vals)
    minus_di = 100 * (minus_dm.ewm(span=period, adjust=False).mean() / atr_vals)
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di) * 100
    return dx.ewm(span=period, adjust=False).mean()


def bollinger_bands(series: pd.Series, period: int = 20, num_std: float = 2.0):
    mid = sma(series, period)
    std = series.rolling(period).std()
    return mid + num_std * std, mid, mid - num_std * std


# ---------------------------------------------------------------------------
# Trade Tracking
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


def classify_session(ts: pd.Timestamp) -> str:
    h = ts.hour
    if 7 <= h < 13:
        return "London"
    elif 13 <= h < 20:
        return "NY"
    return "Other"


def classify_regime(adx_val: float) -> str:
    if adx_val < 20:
        return "Ranging"
    elif adx_val < 30:
        return "Transitional"
    elif adx_val < 50:
        return "Trending"
    return "Strong Trend"


# ---------------------------------------------------------------------------
# Backtester – Strategy B only, with session filter & % risk
# ---------------------------------------------------------------------------

class StrategyBBacktester:
    """BB Mean Reversion with configurable session filter and risk sizing."""

    def __init__(self, df: pd.DataFrame, risk_pct: float = None,
                 allowed_hours: set = None, label: str = ""):
        self.df = df.copy()
        self.trades: List[Trade] = []
        self.balance = INITIAL_BALANCE
        self.equity_curve: List[float] = []
        self.position: Optional[Trade] = None
        self.risk_pct = risk_pct if risk_pct is not None else RISK_PCT
        self.allowed_hours = allowed_hours if allowed_hours is not None else ALLOWED_HOURS
        self.label = label
        self._precompute()

    def _precompute(self):
        df = self.df
        df["rsi14"] = rsi(df["close"], 14)
        df["atr14"] = atr(df, 14)
        df["adx14"] = adx(df, 14)
        df["bb_upper"], df["bb_mid"], df["bb_lower"] = bollinger_bands(df["close"], 20, 2.0)

    def _calc_lot_size(self, entry_price: float, sl_price: float) -> float:
        """Calculate lot size based on configured risk %."""
        risk_amount = self.balance * self.risk_pct  # dollars to risk
        sl_distance = abs(entry_price - sl_price)  # price distance
        if sl_distance <= 0:
            return 0.0
        # PnL per lot = price_move * CONTRACT_SIZE
        # lot_size = risk_amount / (sl_distance * CONTRACT_SIZE)
        lot_size = risk_amount / (sl_distance * CONTRACT_SIZE)
        # Clamp to reasonable bounds (0.01 min, 10.0 max)
        lot_size = max(0.01, min(lot_size, 10.0))
        return round(lot_size, 2)

    def _open_position(self, idx: int, direction: int, sl: float, tp: float):
        if self.position is not None:
            return

        row = self.df.iloc[idx]
        spread = SPREAD_POINTS * POINT_VALUE
        entry = row["close"] + spread / 2 if direction == 1 else row["close"] - spread / 2

        lot_size = self._calc_lot_size(entry, sl)
        if lot_size <= 0:
            return

        self.position = Trade(
            entry_time=self.df.index[idx],
            direction=direction,
            entry_price=entry,
            sl=sl,
            tp=tp,
            lot_size=lot_size,
            session=classify_session(self.df.index[idx]),
            regime=classify_regime(row["adx14"]),
            regime_adx=row["adx14"],
        )

    def _check_exit(self, idx: int) -> bool:
        if self.position is None:
            return False

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

    def _close_position(self, idx: int, exit_price: float):
        pos = self.position
        if pos is None:
            return

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

    def _signal(self, i: int) -> Optional[Tuple[int, float, float]]:
        """BB Mean Reversion signal with London+NY session filter + regime filter."""
        df = self.df
        ts = df.index[i]

        # SESSION FILTER
        if ts.hour not in self.allowed_hours:
            return None

        row = df.iloc[i]

        # REGIME FILTER: skip normal trending (ADX 30-50)
        adx_val = row["adx14"]
        if not np.isnan(adx_val):
            regime = classify_regime(adx_val)
            if regime in BLOCKED_REGIMES:
                return None

        close = row["close"]
        low = row["low"]
        high = row["high"]
        atr_val = row["atr14"]

        if np.isnan(atr_val) or atr_val <= 0:
            return None
        if np.isnan(row["bb_lower"]) or np.isnan(row["bb_upper"]):
            return None

        # Buy: price touches lower BB and RSI < 30
        if low <= row["bb_lower"] and row["rsi14"] < 30:
            sl = close - 1.0 * atr_val
            tp = close + 1.5 * atr_val
            return (1, sl, tp)

        # Sell: price touches upper BB and RSI > 70
        if high >= row["bb_upper"] and row["rsi14"] > 70:
            sl = close + 1.0 * atr_val
            tp = close - 1.5 * atr_val
            return (-1, sl, tp)

        return None

    def run(self) -> List[Trade]:
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
                if sig is not None:
                    direction, sl, tp = sig
                    self._open_position(i, direction, sl, tp)

        if self.position is not None:
            self._close_position(n - 1, self.df["close"].iloc[-1])

        return self.trades


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

def compute_metrics(trades: List[Trade], equity_curve: List[float]) -> Dict:
    if not trades:
        return {"total_trades": 0}

    pnls = np.array([t.pnl for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]

    total = len(pnls)
    win_rate = len(wins) / total * 100
    avg_win = wins.mean() if len(wins) > 0 else 0
    avg_loss = abs(losses.mean()) if len(losses) > 0 else 1
    avg_rr = avg_win / avg_loss if avg_loss > 0 else 0
    gross_profit = wins.sum() if len(wins) > 0 else 0
    gross_loss = abs(losses.sum()) if len(losses) > 0 else 1
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    max_consec_loss = 0
    streak = 0
    for p in pnls:
        if p < 0:
            streak += 1
            max_consec_loss = max(max_consec_loss, streak)
        else:
            streak = 0

    eq = np.array(equity_curve)
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak * 100
    max_dd = dd.min()

    avg_duration = np.mean([t.duration_minutes for t in trades])
    avg_lot = np.mean([t.lot_size for t in trades])

    if len(pnls) > 1:
        bars_per_year = 252 * 1440
        trades_per_year = bars_per_year / max(avg_duration, 1)
        sharpe = (pnls.mean() / pnls.std()) * np.sqrt(trades_per_year) if pnls.std() > 0 else 0
    else:
        sharpe = 0

    return {
        "total_trades": total,
        "win_rate": round(win_rate, 2),
        "avg_rr": round(avg_rr, 2),
        "profit_factor": round(profit_factor, 3),
        "max_consecutive_losses": max_consec_loss,
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe_ratio": round(sharpe, 3),
        "avg_trade_duration_min": round(avg_duration, 1),
        "avg_lot_size": round(avg_lot, 3),
        "total_pnl": round(pnls.sum(), 2),
        "avg_pnl_per_trade": round(pnls.mean(), 2),
        "final_balance": round(eq[-1], 2),
        "net_return_pct": round((eq[-1] - INITIAL_BALANCE) / INITIAL_BALANCE * 100, 2),
    }


def performance_by_regime(trades: List[Trade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    records = []
    for regime in ("Ranging", "Transitional", "Trending", "Strong Trend"):
        subset = [t for t in trades if t.regime == regime]
        if not subset:
            records.append({"regime": regime, "trades": 0, "win_rate": 0, "avg_pnl": 0, "pf": 0})
            continue
        pnls = np.array([t.pnl for t in subset])
        wins = pnls[pnls > 0]
        losses = pnls[pnls < 0]
        wr = len(wins) / len(pnls) * 100
        pf = wins.sum() / abs(losses.sum()) if len(losses) > 0 and losses.sum() != 0 else float("inf")
        records.append({
            "regime": regime, "trades": len(pnls),
            "win_rate": round(wr, 1), "avg_pnl": round(pnls.mean(), 2),
            "pf": round(pf, 3) if pf != float("inf") else "inf",
        })
    return pd.DataFrame(records)


def performance_by_session(trades: List[Trade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    records = []
    for session in ("London", "NY"):
        subset = [t for t in trades if t.session == session]
        if not subset:
            records.append({"session": session, "trades": 0, "win_rate": 0, "avg_pnl": 0, "pf": 0})
            continue
        pnls = np.array([t.pnl for t in subset])
        wins = pnls[pnls > 0]
        losses = pnls[pnls < 0]
        wr = len(wins) / len(pnls) * 100
        pf = wins.sum() / abs(losses.sum()) if len(losses) > 0 and losses.sum() != 0 else float("inf")
        records.append({
            "session": session, "trades": len(pnls),
            "win_rate": round(wr, 1), "avg_pnl": round(pnls.mean(), 2),
            "pf": round(pf, 3) if pf != float("inf") else "inf",
        })
    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_period_report(period_label: str, date_range: str, metrics: Dict,
                        regime_df: pd.DataFrame, session_df: pd.DataFrame):
    sep = "=" * 72
    print(f"\n{sep}")
    print(f"  {period_label}  |  {date_range}")
    print(f"  Strategy B: BB Mean Reversion (London+NY, 2% risk)")
    print(sep)

    if metrics["total_trades"] == 0:
        print("  No trades generated.\n")
        return

    print(f"  Total trades:          {metrics['total_trades']}")
    print(f"  Win rate:              {metrics['win_rate']}%")
    print(f"  Avg R:R achieved:      {metrics['avg_rr']}")
    print(f"  Profit factor:         {metrics['profit_factor']}")
    print(f"  Max consec. losses:    {metrics['max_consecutive_losses']}")
    print(f"  Max drawdown:          {metrics['max_drawdown_pct']}%")
    print(f"  Sharpe ratio (ann.):   {metrics['sharpe_ratio']}")
    print(f"  Avg trade duration:    {metrics['avg_trade_duration_min']} min")
    print(f"  Avg lot size:          {metrics['avg_lot_size']}")
    print(f"  Total P&L:             ${metrics['total_pnl']:,.2f}")
    print(f"  Final balance:         ${metrics['final_balance']:,.2f}")
    print(f"  Net return:            {metrics['net_return_pct']}%")

    print(f"\n  {'--- Performance by Market Regime ---':^50}")
    if not regime_df.empty:
        print(regime_df.to_string(index=False))

    print(f"\n  {'--- Performance by Session ---':^50}")
    if not session_df.empty:
        print(session_df.to_string(index=False))
    print()


def print_summary(all_periods: List[Dict]):
    """Print averaged results across all periods."""
    sep = "=" * 72
    print(f"\n{sep}")
    print(f"  AVERAGED RESULTS ACROSS {len(all_periods)} PERIODS")
    print(sep)

    metrics_keys = [
        "total_trades", "win_rate", "avg_rr", "profit_factor",
        "max_consecutive_losses", "max_drawdown_pct", "sharpe_ratio",
        "avg_trade_duration_min", "avg_lot_size", "total_pnl",
        "net_return_pct",
    ]

    valid = [p["metrics"] for p in all_periods if p["metrics"]["total_trades"] > 0]
    if not valid:
        print("  No periods produced trades.")
        return

    print(f"  Periods with trades:   {len(valid)} / {len(all_periods)}")
    for key in metrics_keys:
        vals = [m[key] for m in valid]
        avg = np.mean(vals)
        std = np.std(vals) if len(vals) > 1 else 0
        if key in ("total_trades", "max_consecutive_losses"):
            print(f"  {key:28s}  avg={avg:8.1f}   std={std:8.1f}")
        elif key == "total_pnl":
            print(f"  {key:28s}  avg=${avg:>10,.2f}   std=${std:>10,.2f}")
        else:
            print(f"  {key:28s}  avg={avg:8.3f}   std={std:8.3f}")

    print()

    # Per-period summary table
    rows = []
    for p in all_periods:
        m = p["metrics"]
        if m["total_trades"] == 0:
            continue
        rows.append({
            "Period": p["label"],
            "Trades": m["total_trades"],
            "Win%": m["win_rate"],
            "PF": m["profit_factor"],
            "Sharpe": m["sharpe_ratio"],
            "MaxDD%": m["max_drawdown_pct"],
            "P&L": m["total_pnl"],
            "Ret%": m["net_return_pct"],
        })
    if rows:
        print(pd.DataFrame(rows).to_string(index=False))
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

BOT_CONFIGS = [
    {
        "name": "Bot 1: London 1%",
        "risk_pct": 0.01,
        "allowed_hours": set(range(7, 13)),
    },
    {
        "name": "Bot 2: London 2%",
        "risk_pct": 0.02,
        "allowed_hours": set(range(7, 13)),
    },
    {
        "name": "Bot 3: London+NY 1%",
        "risk_pct": 0.01,
        "allowed_hours": set(range(7, 20)),
    },
]


def main():
    global TIMEFRAME
    parser = argparse.ArgumentParser(description="Strategy B Refined Backtester")
    parser.add_argument("--period-offset", type=int, default=0,
                        help="Period offset in months from latest data (0=most recent, 3=previous)")
    parser.add_argument("--timeframe", type=str, default="M1",
                        choices=["M1", "M5", "M15"],
                        help="Timeframe to backtest on (default: M1)")
    args = parser.parse_args()

    TIMEFRAME = TF_MAP[args.timeframe]
    print(f"Timeframe: {args.timeframe}")

    if not connect_mt5():
        return

    try:
        full_df = fetch_all_available()
    except RuntimeError as e:
        print(f"Data error: {e}")
        mt5.shutdown()
        return

    subset = slice_period(full_df, args.period_offset, 3)

    if len(subset) < 1000:
        print(f"Insufficient data ({len(subset)} bars) for offset {args.period_offset} months.")
        mt5.shutdown()
        return

    date_range = f"{subset.index[0].strftime('%Y-%m-%d')} -> {subset.index[-1].strftime('%Y-%m-%d')}"
    print(f"\nData period: {date_range} ({len(subset):,} bars)")

    all_results = []

    for cfg in BOT_CONFIGS:
        print(f"\n{'-' * 72}")
        print(f"  Running: {cfg['name']}")

        bt = StrategyBBacktester(
            subset,
            risk_pct=cfg["risk_pct"],
            allowed_hours=cfg["allowed_hours"],
            label=cfg["name"],
        )
        trades = bt.run()
        metrics = compute_metrics(trades, bt.equity_curve)
        regime_df = performance_by_regime(trades)
        session_df = performance_by_session(trades)

        print_period_report(cfg["name"], date_range, metrics, regime_df, session_df)

        all_results.append({
            "label": cfg["name"],
            "date_range": date_range,
            "metrics": metrics,
            "regime": regime_df,
            "session": session_df,
        })

    # Comparison table
    print("\n" + "=" * 80)
    print("  SIDE-BY-SIDE COMPARISON")
    print("=" * 80)
    rows = []
    for r in all_results:
        m = r["metrics"]
        if m["total_trades"] == 0:
            continue
        rows.append({
            "Bot": r["label"],
            "Trades": m["total_trades"],
            "Win%": m["win_rate"],
            "R:R": m["avg_rr"],
            "PF": m["profit_factor"],
            "MaxDD%": m["max_drawdown_pct"],
            "Sharpe": m["sharpe_ratio"],
            "P&L": m["total_pnl"],
            "Ret%": m["net_return_pct"],
        })
    if rows:
        print(pd.DataFrame(rows).to_string(index=False))
    print()

    # Save to CSV
    csv_rows = []
    for r in all_results:
        row = {"bot": r["label"], "dates": r["date_range"]}
        row.update(r["metrics"])
        csv_rows.append(row)
    if csv_rows:
        out = "C:/Users/d4nny/AhadAI/HFTbot/research/backtest_b_refined.csv"
        pd.DataFrame(csv_rows).to_csv(out, index=False)
        print(f"Results saved to {out}")

    mt5.shutdown()
    print("Done.")


if __name__ == "__main__":
    main()
