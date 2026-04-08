"""
Gold (XAU/USD) 1-Minute HFT Backtesting Engine
================================================
Backtests 7 scalping strategies on 1-minute XAU/USD data from MetaTrader5.

Strategies:
  A. EMA Momentum Scalper
  B. Bollinger Band Mean Reversion
  C. VWAP Bounce Scalper
  D. London Open Momentum
  E. NY Session Scalper
  F. London Fix Fade
  G. Micro Range Breakout

Usage:
  python backtester.py              # run all strategies
  python backtester.py --strategy A # run a single strategy
"""

import argparse
import datetime as dt
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

import MetaTrader5 as mt5
import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SYMBOL = "XAUUSDm"
TIMEFRAME = mt5.TIMEFRAME_M1
DATA_MONTHS = 3                     # how many months of history to fetch
COMMISSION_PER_LOT = 7.0            # round-turn USD per standard lot
SPREAD_POINTS = 20                  # typical spread in points (0.20 USD)
POINT_VALUE = 0.001                 # 1 point = $0.001 for XAUUSDm on Exness
LOT_SIZE = 1.0                     # standard lot
CONTRACT_SIZE = 100                 # 100 oz per lot
INITIAL_BALANCE = 100_000.0


# ---------------------------------------------------------------------------
# Data Acquisition
# ---------------------------------------------------------------------------

def connect_mt5() -> bool:
    """Initialise MT5 terminal connection."""
    if not mt5.initialize():
        print(f"MT5 initialisation failed: {mt5.last_error()}")
        return False
    print(f"MT5 connected – build {mt5.version()}")
    return True


def fetch_data(months: int = DATA_MONTHS) -> pd.DataFrame:
    """Download 1-minute XAU/USD bars from MT5 in chunks."""
    mt5.symbol_select(SYMBOL, True)

    CHUNK = 50_000
    total_bars = months * 30 * 24 * 60
    all_frames = []
    offset = 0

    while offset < total_bars:
        chunk_size = min(CHUNK, total_bars - offset)
        rates = mt5.copy_rates_from_pos(SYMBOL, TIMEFRAME, offset, chunk_size)
        if rates is None or len(rates) == 0:
            break
        all_frames.append(pd.DataFrame(rates))
        print(f"  Fetched chunk: {len(rates):,} bars (offset {offset:,})")
        offset += len(rates)
        if len(rates) < chunk_size:
            break

    if not all_frames:
        raise RuntimeError(f"No data returned for {SYMBOL}: {mt5.last_error()}")

    df = pd.concat(all_frames, ignore_index=True).drop_duplicates(subset=["time"])
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    df.sort_index(inplace=True)
    df.rename(columns={"tick_volume": "volume"}, inplace=True)

    for col in ("open", "high", "low", "close", "volume"):
        if col not in df.columns:
            raise RuntimeError(f"Missing column: {col}")

    print(f"Loaded {len(df):,} bars  |  {df.index[0]} -> {df.index[-1]}")
    return df


# ---------------------------------------------------------------------------
# Technical Indicator Library
# ---------------------------------------------------------------------------

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


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

    # Zero out when the other is larger
    plus_dm[plus_dm < minus_dm] = 0
    minus_dm[minus_dm < plus_dm] = 0

    atr_vals = atr(df, period)
    plus_di = 100 * (plus_dm.ewm(span=period, adjust=False).mean() / atr_vals)
    minus_di = 100 * (minus_dm.ewm(span=period, adjust=False).mean() / atr_vals)

    dx = (plus_di - minus_di).abs() / (plus_di + minus_di) * 100
    return dx.ewm(span=period, adjust=False).mean()


def bollinger_bands(
    series: pd.Series, period: int = 20, num_std: float = 2.0
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    mid = sma(series, period)
    std = series.rolling(period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP – resets at 00:00 UTC each day."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"].replace(0, np.nan)
    day = df.index.date

    cum_tp_vol = (typical * vol).groupby(day).cumsum()
    cum_vol = vol.groupby(day).cumsum()
    return cum_tp_vol / cum_vol


# ---------------------------------------------------------------------------
# Trade / Position Tracking
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    entry_time: pd.Timestamp
    exit_time: Optional[pd.Timestamp] = None
    direction: int = 0          # 1 = long, -1 = short
    entry_price: float = 0.0
    exit_price: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    duration_minutes: int = 0
    session: str = ""
    regime: str = ""
    regime_adx: float = 0.0


def classify_session(ts: pd.Timestamp) -> str:
    h = ts.hour
    if 0 <= h < 7:
        return "Asian"
    elif 7 <= h < 13:
        return "London"
    elif 13 <= h < 20:
        return "NY"
    return "Off-hours"


def classify_regime(adx_val: float) -> str:
    if adx_val < 20:
        return "Ranging"
    elif adx_val < 30:
        return "Transitional"
    elif adx_val < 50:
        return "Trending"
    return "Strong Trend"


# ---------------------------------------------------------------------------
# Backtesting Engine
# ---------------------------------------------------------------------------

class Backtester:
    """Event-driven bar-by-bar backtesting engine."""

    def __init__(self, df: pd.DataFrame, strategy_name: str):
        self.df = df.copy()
        self.strategy_name = strategy_name
        self.trades: List[Trade] = []
        self.balance = INITIAL_BALANCE
        self.equity_curve: List[float] = []
        self.position: Optional[Trade] = None

        # Pre-compute common indicators
        self._precompute()

    def _precompute(self):
        df = self.df
        df["ema9"] = ema(df["close"], 9)
        df["ema21"] = ema(df["close"], 21)
        df["ema200_5m"] = ema(df["close"], 200 * 5)  # proxy for 5m EMA(200)
        df["rsi7"] = rsi(df["close"], 7)
        df["rsi14"] = rsi(df["close"], 14)
        df["atr14"] = atr(df, 14)
        df["atr50"] = atr(df, 50)
        df["adx14"] = adx(df, 14)
        df["bb_upper"], df["bb_mid"], df["bb_lower"] = bollinger_bands(df["close"], 20, 2.0)
        df["vwap"] = vwap(df)

        # ATR percentile for volatility regime
        df["atr_pct"] = df["atr14"].rolling(500).apply(
            lambda x: pd.Series(x).rank(pct=True).iloc[-1], raw=False
        )
        # Volume SMA for spike detection
        df["vol_sma20"] = sma(df["volume"].astype(float), 20)

    # ---------- position management ----------

    def _open_position(self, idx: int, direction: int, sl: float, tp: float):
        if self.position is not None:
            return  # already in a trade

        row = self.df.iloc[idx]
        spread = SPREAD_POINTS * POINT_VALUE
        entry = row["close"] + spread / 2 if direction == 1 else row["close"] - spread / 2

        self.position = Trade(
            entry_time=self.df.index[idx],
            direction=direction,
            entry_price=entry,
            sl=sl,
            tp=tp,
            session=classify_session(self.df.index[idx]),
            regime=classify_regime(row["adx14"]),
            regime_adx=row["adx14"],
        )

    def _check_exit(self, idx: int) -> bool:
        if self.position is None:
            return False

        row = self.df.iloc[idx]
        pos = self.position

        hit_sl = False
        hit_tp = False

        if pos.direction == 1:
            hit_sl = row["low"] <= pos.sl
            hit_tp = row["high"] >= pos.tp
        else:
            hit_sl = row["high"] >= pos.sl
            hit_tp = row["low"] <= pos.tp

        if hit_sl or hit_tp:
            if hit_sl and hit_tp:
                # Assume worst-case: SL hit first
                exit_price = pos.sl
            elif hit_tp:
                exit_price = pos.tp
            else:
                exit_price = pos.sl

            self._close_position(idx, exit_price)
            return True
        return False

    def _close_position(self, idx: int, exit_price: float):
        pos = self.position
        if pos is None:
            return

        spread_cost = SPREAD_POINTS * POINT_VALUE
        commission = COMMISSION_PER_LOT * LOT_SIZE

        raw_pnl = (exit_price - pos.entry_price) * pos.direction * CONTRACT_SIZE * LOT_SIZE
        pos.pnl = raw_pnl - commission - (spread_cost * CONTRACT_SIZE * LOT_SIZE * 0.5)
        pos.exit_price = exit_price
        pos.exit_time = self.df.index[idx]
        pos.duration_minutes = int((pos.exit_time - pos.entry_time).total_seconds() / 60)
        pos.pnl_pct = pos.pnl / self.balance * 100

        self.balance += pos.pnl
        self.trades.append(pos)
        self.position = None

    # ---------- strategy signal generators ----------

    def _strategy_a_ema_momentum(self, i: int) -> Optional[Tuple[int, float, float]]:
        """EMA(9)/EMA(21) crossover with RSI(7) filter."""
        df = self.df
        if i < 1:
            return None

        prev_ema9 = df["ema9"].iloc[i - 1]
        prev_ema21 = df["ema21"].iloc[i - 1]
        curr_ema9 = df["ema9"].iloc[i]
        curr_ema21 = df["ema21"].iloc[i]
        rsi_val = df["rsi7"].iloc[i]
        atr_val = df["atr14"].iloc[i]
        close = df["close"].iloc[i]

        if np.isnan(atr_val) or atr_val <= 0:
            return None

        # Bullish crossover
        if prev_ema9 <= prev_ema21 and curr_ema9 > curr_ema21 and rsi_val > 50:
            sl = close - 1.5 * atr_val
            tp = close + 2.0 * atr_val
            return (1, sl, tp)

        # Bearish crossover
        if prev_ema9 >= prev_ema21 and curr_ema9 < curr_ema21 and rsi_val < 50:
            sl = close + 1.5 * atr_val
            tp = close - 2.0 * atr_val
            return (-1, sl, tp)

        return None

    def _strategy_b_bb_reversion(self, i: int) -> Optional[Tuple[int, float, float]]:
        """Bollinger Band Mean Reversion with RSI(14)."""
        df = self.df
        row = df.iloc[i]
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

    def _strategy_c_vwap_bounce(self, i: int) -> Optional[Tuple[int, float, float]]:
        """VWAP Bounce Scalper – bounce off VWAP with volume spike."""
        df = self.df
        if i < 2:
            return None

        row = df.iloc[i]
        prev = df.iloc[i - 1]
        prev2 = df.iloc[i - 2]
        close = row["close"]
        vwap_val = row["vwap"]
        atr_val = row["atr14"]
        vol = row["volume"]
        vol_avg = row["vol_sma20"]

        if np.isnan(vwap_val) or np.isnan(atr_val) or atr_val <= 0:
            return None
        if np.isnan(vol_avg) or vol_avg <= 0:
            return None

        vol_spike = vol > 1.5 * vol_avg
        near_vwap = abs(row["low"] - vwap_val) < 0.3 * atr_val

        # Buy: price pulled back to VWAP from above, bounces with volume
        if (prev2["close"] > prev2["vwap"] and
                near_vwap and close > vwap_val and vol_spike):
            sl = vwap_val - 0.5 * atr_val
            tp = close + 2.0 * atr_val
            return (1, sl, tp)

        near_vwap_high = abs(row["high"] - vwap_val) < 0.3 * atr_val

        # Sell: price pulled back to VWAP from below, rejects with volume
        if (prev2["close"] < prev2["vwap"] and
                near_vwap_high and close < vwap_val and vol_spike):
            sl = vwap_val + 0.5 * atr_val
            tp = close - 2.0 * atr_val
            return (-1, sl, tp)

        return None

    def _strategy_d_london_open(self, i: int) -> Optional[Tuple[int, float, float]]:
        """London Open Momentum – breakout of 07:00-07:59 range at 08:00-08:15."""
        df = self.df
        ts = df.index[i]

        # Only trade 08:00 - 08:15 UTC
        if not (ts.hour == 8 and ts.minute < 15):
            return None

        # Compute 07:00 - 07:59 range
        day = ts.date()
        range_start = pd.Timestamp(day, tz="UTC") + pd.Timedelta(hours=7)
        range_end = pd.Timestamp(day, tz="UTC") + pd.Timedelta(hours=8)

        mask = (df.index >= range_start) & (df.index < range_end)
        pre_london = df.loc[mask]

        if len(pre_london) < 10:
            return None

        range_high = pre_london["high"].max()
        range_low = pre_london["low"].min()
        range_width = range_high - range_low

        if range_width <= 0:
            return None

        close = df["close"].iloc[i]

        # Buy breakout
        if close > range_high:
            sl = range_low
            tp = close + 1.5 * range_width
            return (1, sl, tp)

        # Sell breakout
        if close < range_low:
            sl = range_high
            tp = close - 1.5 * range_width
            return (-1, sl, tp)

        return None

    def _strategy_e_ny_session(self, i: int) -> Optional[Tuple[int, float, float]]:
        """NY Session Scalper – momentum in first 5 candles after 13:30 UTC."""
        df = self.df
        ts = df.index[i]

        # Only trigger at 13:35 (after 5 candles of 13:30-13:34)
        if not (ts.hour == 13 and ts.minute == 35):
            return None

        # Look back at 13:30 - 13:34 (5 bars)
        start = pd.Timestamp(ts.date(), tz="UTC") + pd.Timedelta(hours=13, minutes=30)
        end = pd.Timestamp(ts.date(), tz="UTC") + pd.Timedelta(hours=13, minutes=35)
        mask = (df.index >= start) & (df.index < end)
        candles = df.loc[mask]

        if len(candles) < 5:
            return None

        bullish = (candles["close"] > candles["open"]).sum()
        bearish = (candles["close"] < candles["open"]).sum()
        volumes = candles["volume"].values

        # Check increasing volume (at least 3 of 4 consecutive increases)
        vol_increases = sum(1 for j in range(1, len(volumes)) if volumes[j] > volumes[j - 1])
        increasing_vol = vol_increases >= 3

        atr_val = df["atr14"].iloc[i]
        close = df["close"].iloc[i]

        if np.isnan(atr_val) or atr_val <= 0:
            return None

        # 3+ bullish candles with increasing volume
        if bullish >= 3 and increasing_vol:
            sl = close - 1.5 * atr_val
            tp = close + 2.5 * atr_val
            return (1, sl, tp)

        # 3+ bearish candles with increasing volume
        if bearish >= 3 and increasing_vol:
            sl = close + 1.5 * atr_val
            tp = close - 2.5 * atr_val
            return (-1, sl, tp)

        return None

    def _strategy_f_london_fix_fade(self, i: int) -> Optional[Tuple[int, float, float]]:
        """London Fix Fade – fade the pre-fix move at 10:30 UTC."""
        df = self.df
        ts = df.index[i]

        # Only trigger at 10:30 UTC
        if not (ts.hour == 10 and ts.minute == 30):
            return None

        day = ts.date()
        pre_start = pd.Timestamp(day, tz="UTC") + pd.Timedelta(hours=10)
        pre_end = pd.Timestamp(day, tz="UTC") + pd.Timedelta(hours=10, minutes=30)
        mask = (df.index >= pre_start) & (df.index < pre_end)
        pre_fix = df.loc[mask]

        if len(pre_fix) < 5:
            return None

        move = pre_fix["close"].iloc[-1] - pre_fix["open"].iloc[0]
        atr_val = df["atr14"].iloc[i]
        close = df["close"].iloc[i]

        if np.isnan(atr_val) or atr_val <= 0:
            return None

        # Need a meaningful pre-fix move (at least 0.3 ATR)
        if abs(move) < 0.3 * atr_val:
            return None

        # Fade the move
        if move > 0:
            # Price went up -> sell
            sl = close + 2.0 * atr_val
            tp = close - 3.0 * atr_val
            return (-1, sl, tp)
        else:
            # Price went down -> buy
            sl = close - 2.0 * atr_val
            tp = close + 3.0 * atr_val
            return (1, sl, tp)

    def _strategy_g_micro_range_breakout(self, i: int) -> Optional[Tuple[int, float, float]]:
        """Micro Range Breakout – detect 10-15 bar consolidation, trade breakout."""
        df = self.df
        if i < 20:
            return None

        atr50 = df["atr50"].iloc[i]
        if np.isnan(atr50) or atr50 <= 0:
            return None

        # Check last 10-15 candles for consolidation
        for lookback in (15, 12, 10):
            window = df.iloc[i - lookback: i]
            range_high = window["high"].max()
            range_low = window["low"].min()
            range_width = range_high - range_low

            if range_width < 0.5 * atr50 and range_width > 0:
                close = df["close"].iloc[i]
                ema200 = df["ema200_5m"].iloc[i]

                if np.isnan(ema200):
                    return None

                mid_range = (range_high + range_low) / 2

                # Breakout above range in bullish trend
                if close > range_high and close > ema200:
                    sl = mid_range
                    tp = close + 2.0 * range_width
                    return (1, sl, tp)

                # Breakout below range in bearish trend
                if close < range_low and close < ema200:
                    sl = mid_range
                    tp = close - 2.0 * range_width
                    return (-1, sl, tp)

        return None

    # ---------- run engine ----------

    STRATEGY_MAP = {
        "A": ("EMA Momentum Scalper", "_strategy_a_ema_momentum"),
        "B": ("BB Mean Reversion", "_strategy_b_bb_reversion"),
        "C": ("VWAP Bounce Scalper", "_strategy_c_vwap_bounce"),
        "D": ("London Open Momentum", "_strategy_d_london_open"),
        "E": ("NY Session Scalper", "_strategy_e_ny_session"),
        "F": ("London Fix Fade", "_strategy_f_london_fix_fade"),
        "G": ("Micro Range Breakout", "_strategy_g_micro_range_breakout"),
    }

    def run(self, strategy_key: str) -> List[Trade]:
        name, method_name = self.STRATEGY_MAP[strategy_key]
        signal_fn = getattr(self, method_name)
        self.strategy_name = name
        self.trades = []
        self.balance = INITIAL_BALANCE
        self.equity_curve = [INITIAL_BALANCE]
        self.position = None

        n = len(self.df)
        for i in range(1, n):
            # Check exits first
            self._check_exit(i)

            # Record equity
            self.equity_curve.append(self.balance)

            # Check for new signal if flat
            if self.position is None:
                sig = signal_fn(i)
                if sig is not None:
                    direction, sl, tp = sig
                    self._open_position(i, direction, sl, tp)

        # Force-close any open position at end
        if self.position is not None:
            self._close_position(n - 1, self.df["close"].iloc[-1])

        return self.trades


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

def compute_metrics(trades: List[Trade], equity_curve: List[float]) -> Dict:
    """Compute comprehensive performance metrics."""
    if not trades:
        return {"total_trades": 0}

    pnls = np.array([t.pnl for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]

    total = len(pnls)
    win_count = len(wins)
    loss_count = len(losses)
    win_rate = win_count / total * 100 if total > 0 else 0

    avg_win = wins.mean() if len(wins) > 0 else 0
    avg_loss = abs(losses.mean()) if len(losses) > 0 else 1
    avg_rr = avg_win / avg_loss if avg_loss > 0 else 0

    gross_profit = wins.sum() if len(wins) > 0 else 0
    gross_loss = abs(losses.sum()) if len(losses) > 0 else 1
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Max consecutive losses
    max_consec_loss = 0
    current_streak = 0
    for p in pnls:
        if p < 0:
            current_streak += 1
            max_consec_loss = max(max_consec_loss, current_streak)
        else:
            current_streak = 0

    # Max drawdown
    eq = np.array(equity_curve)
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak * 100
    max_dd = dd.min()

    # Sharpe ratio (annualized, 1-minute bars ~252 * 1440 bars/year)
    if len(pnls) > 1:
        bars_per_year = 252 * 1440
        avg_duration = np.mean([t.duration_minutes for t in trades])
        trades_per_year = bars_per_year / max(avg_duration, 1)
        mean_ret = pnls.mean()
        std_ret = pnls.std()
        sharpe = (mean_ret / std_ret) * np.sqrt(trades_per_year) if std_ret > 0 else 0
    else:
        sharpe = 0
        avg_duration = 0

    avg_duration = np.mean([t.duration_minutes for t in trades])

    return {
        "total_trades": total,
        "win_rate": round(win_rate, 2),
        "avg_rr": round(avg_rr, 2),
        "profit_factor": round(profit_factor, 3),
        "max_consecutive_losses": max_consec_loss,
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe_ratio": round(sharpe, 3),
        "avg_trade_duration_min": round(avg_duration, 1),
        "total_pnl": round(pnls.sum(), 2),
        "avg_pnl_per_trade": round(pnls.mean(), 2),
        "net_return_pct": round((eq[-1] - INITIAL_BALANCE) / INITIAL_BALANCE * 100, 2),
    }


def performance_by_regime(trades: List[Trade]) -> pd.DataFrame:
    """Break down performance by ADX market regime."""
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
            "regime": regime,
            "trades": len(pnls),
            "win_rate": round(wr, 1),
            "avg_pnl": round(pnls.mean(), 2),
            "pf": round(pf, 3) if pf != float("inf") else "inf",
        })
    return pd.DataFrame(records)


def performance_by_session(trades: List[Trade]) -> pd.DataFrame:
    """Break down performance by trading session."""
    if not trades:
        return pd.DataFrame()

    records = []
    for session in ("Asian", "London", "NY", "Off-hours"):
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
            "session": session,
            "trades": len(pnls),
            "win_rate": round(wr, 1),
            "avg_pnl": round(pnls.mean(), 2),
            "pf": round(pf, 3) if pf != float("inf") else "inf",
        })
    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_strategy_report(key: str, name: str, metrics: Dict,
                          regime_df: pd.DataFrame, session_df: pd.DataFrame):
    sep = "=" * 72
    print(f"\n{sep}")
    print(f"  Strategy {key}: {name}")
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
    print(f"  Total P&L:             ${metrics['total_pnl']:,.2f}")
    print(f"  Net return:            {metrics['net_return_pct']}%")

    print(f"\n  {'--- Performance by Market Regime ---':^50}")
    if not regime_df.empty:
        print(regime_df.to_string(index=False))

    print(f"\n  {'--- Performance by Session ---':^50}")
    if not session_df.empty:
        print(session_df.to_string(index=False))
    print()


def print_ranking_table(all_results: Dict[str, Dict]):
    """Print final strategy ranking sorted by Sharpe ratio."""
    print("\n" + "=" * 80)
    print("  FINAL STRATEGY RANKING (sorted by Sharpe Ratio)")
    print("=" * 80)

    rows = []
    for key, data in all_results.items():
        m = data["metrics"]
        if m["total_trades"] == 0:
            continue
        rows.append({
            "Strategy": f"{key}. {data['name']}",
            "Trades": m["total_trades"],
            "Win%": m["win_rate"],
            "R:R": m["avg_rr"],
            "PF": m["profit_factor"],
            "MaxDD%": m["max_drawdown_pct"],
            "Sharpe": m["sharpe_ratio"],
            "P&L": m["total_pnl"],
            "Ret%": m["net_return_pct"],
        })

    if not rows:
        print("  No strategies produced trades.")
        return

    ranking = pd.DataFrame(rows).sort_values("Sharpe", ascending=False).reset_index(drop=True)
    ranking.index += 1
    ranking.index.name = "Rank"
    print(ranking.to_string())
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="XAU/USD 1m HFT Backtester")
    parser.add_argument("--strategy", type=str, default="ALL",
                        help="Strategy key (A-G) or ALL")
    parser.add_argument("--months", type=int, default=DATA_MONTHS,
                        help="Months of historical data")
    args = parser.parse_args()

    if not connect_mt5():
        return

    try:
        df = fetch_data(args.months)
    except RuntimeError as e:
        print(f"Data error: {e}")
        mt5.shutdown()
        return

    keys = list(Backtester.STRATEGY_MAP.keys())
    if args.strategy.upper() != "ALL":
        keys = [args.strategy.upper()]

    all_results: Dict[str, Dict] = {}

    for key in keys:
        name, _ = Backtester.STRATEGY_MAP[key]
        print(f"\nRunning strategy {key}: {name} ...")

        bt = Backtester(df, name)
        trades = bt.run(key)
        metrics = compute_metrics(trades, bt.equity_curve)
        regime_df = performance_by_regime(trades)
        session_df = performance_by_session(trades)

        print_strategy_report(key, name, metrics, regime_df, session_df)

        all_results[key] = {
            "name": name,
            "metrics": metrics,
            "regime": regime_df,
            "session": session_df,
        }

    if len(all_results) > 1:
        print_ranking_table(all_results)

    # Save results to CSV
    summary_rows = []
    for key, data in all_results.items():
        row = {"strategy": f"{key}. {data['name']}"}
        row.update(data["metrics"])
        summary_rows.append(row)

    if summary_rows:
        out_path = "C:/Users/d4nny/AhadAI/HFTbot/research/backtest_results.csv"
        pd.DataFrame(summary_rows).to_csv(out_path, index=False)
        print(f"Results saved to {out_path}")

    mt5.shutdown()
    print("Done.")


if __name__ == "__main__":
    main()
