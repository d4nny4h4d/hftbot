import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime, timezone

mt5.initialize()
mt5.symbol_select('XAUUSDm', True)

# Fetch today's full M1 data with enough lookback for indicators
from_time = datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc)  # extra day for warmup
to_time = datetime(2026, 3, 17, 0, 0, tzinfo=timezone.utc)
rates = mt5.copy_rates_range('XAUUSDm', mt5.TIMEFRAME_M1, from_time, to_time)
df = pd.DataFrame(rates)
df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
df.set_index('time', inplace=True)
df.rename(columns={'tick_volume': 'volume'}, inplace=True)

print(f'Data rows: {len(df)}')
print(f'Date range: {df.index[0]} to {df.index[-1]}')

# Compute indicators
def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ag = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    al = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    return 100 - (100 / (1 + ag / al))

def atr(df, period=14):
    hl = df['high'] - df['low']
    hc = (df['high'] - df['close'].shift()).abs()
    lc = (df['low'] - df['close'].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()

def adx(df, period=14):
    plus_dm = df['high'].diff().clip(lower=0)
    minus_dm = (-df['low'].diff()).clip(lower=0)
    plus_dm_c = plus_dm.copy()
    minus_dm_c = minus_dm.copy()
    plus_dm_c[plus_dm_c < minus_dm_c] = 0
    minus_dm_c[minus_dm_c < plus_dm_c] = 0
    a = atr(df, period)
    plus_di = 100 * (plus_dm_c.ewm(span=period, adjust=False).mean() / a)
    minus_di = 100 * (minus_dm_c.ewm(span=period, adjust=False).mean() / a)
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di) * 100
    return dx.ewm(span=period, adjust=False).mean()

def bollinger_bands(series, period=20, std=2.0):
    mid = series.rolling(period).mean()
    s = series.rolling(period).std()
    return mid + std * s, mid, mid - std * s

df['rsi14'] = rsi(df['close'], 14)
df['atr14'] = atr(df, 14)
df['adx14'] = adx(df, 14)
df['bb_upper'], df['bb_mid'], df['bb_lower'] = bollinger_bands(df['close'], 20, 2.0)

def classify_regime(adx_val):
    if adx_val < 20: return 'ranging'
    elif adx_val < 30: return 'transitional'
    elif adx_val < 50: return 'trending'
    return 'strong_trend'

BLOCKED = {'trending', 'transitional'}
SPREAD_POINTS = 20
POINT_VALUE = 0.001
CONTRACT_SIZE = 100
COMMISSION_PER_LOT = 7.0

# Filter to today only for signal generation (but use full data for indicator warmup)
today_start = datetime(2026, 3, 16, 0, 0, tzinfo=timezone.utc)

BOT_CONFIGS = [
    {'name': 'Bot 1: London 1%', 'risk_pct': 0.01, 'hours': set(range(7, 13)), 'balance': 1000.0},
    {'name': 'Bot 2: London 2%', 'risk_pct': 0.02, 'hours': set(range(7, 13)), 'balance': 1000.0},
    {'name': 'Bot 3: London+NY 1%', 'risk_pct': 0.01, 'hours': set(range(7, 20)), 'balance': 1000.0},
]

for cfg in BOT_CONFIGS:
    balance = cfg['balance']
    trades = []
    position = None  # (direction, entry_price, sl, tp, lots, entry_time)

    for i in range(1, len(df)):
        ts = df.index[i]
        if ts < today_start:
            continue

        row = df.iloc[i]

        # Check exit first
        if position is not None:
            direction, entry_price, sl, tp, lots, entry_time = position
            if direction == 1:  # BUY
                hit_sl = row['low'] <= sl
                hit_tp = row['high'] >= tp
            else:  # SELL
                hit_sl = row['high'] >= sl
                hit_tp = row['low'] <= tp

            if hit_sl or hit_tp:
                exit_price = sl if hit_sl else tp
                spread_cost = SPREAD_POINTS * POINT_VALUE
                raw_pnl = (exit_price - entry_price) * direction * CONTRACT_SIZE * lots
                commission = COMMISSION_PER_LOT * lots
                net_pnl = raw_pnl - commission - (spread_cost * CONTRACT_SIZE * lots * 0.5)
                balance += net_pnl
                result = 'WIN' if net_pnl > 0 else 'LOSS'
                trades.append({
                    'entry_time': entry_time, 'exit_time': ts,
                    'dir': 'BUY' if direction == 1 else 'SELL',
                    'entry': entry_price, 'exit': exit_price,
                    'lots': lots, 'pnl': round(net_pnl, 2), 'result': result,
                    'balance': round(balance, 2)
                })
                position = None

        # Check for new signal (only if no position)
        if position is None and ts.hour in cfg['hours']:
            adx_val = row['adx14']
            if pd.isna(adx_val) or pd.isna(row['bb_lower']) or pd.isna(row['atr14']) or pd.isna(row['rsi14']):
                continue
            regime = classify_regime(adx_val)
            if regime in BLOCKED:
                continue

            atr_val = row['atr14']
            if atr_val <= 0:
                continue

            signal = None
            if row['low'] <= row['bb_lower'] and row['rsi14'] < 30:
                signal = 1  # BUY
            elif row['high'] >= row['bb_upper'] and row['rsi14'] > 70:
                signal = -1  # SELL

            if signal is not None:
                spread = SPREAD_POINTS * POINT_VALUE
                entry = row['close'] + spread/2 if signal == 1 else row['close'] - spread/2
                sl_price = row['close'] - 1.0 * atr_val if signal == 1 else row['close'] + 1.0 * atr_val
                tp_price = row['close'] + 1.5 * atr_val if signal == 1 else row['close'] - 1.5 * atr_val

                # Position sizing
                risk_amount = balance * cfg['risk_pct']
                sl_dist = abs(entry - sl_price)
                if sl_dist > 0:
                    lots = risk_amount / (sl_dist * CONTRACT_SIZE)
                    lots = round(max(0.01, min(lots, 1.0)), 2)
                    position = (signal, entry, sl_price, tp_price, lots, ts)

    # Close any open position at end of day
    if position is not None:
        direction, entry_price, sl, tp, lots, entry_time = position
        exit_price = df.iloc[-1]['close']
        raw_pnl = (exit_price - entry_price) * direction * CONTRACT_SIZE * lots
        commission = COMMISSION_PER_LOT * lots
        spread_cost = SPREAD_POINTS * POINT_VALUE
        net_pnl = raw_pnl - commission - (spread_cost * CONTRACT_SIZE * lots * 0.5)
        balance += net_pnl
        trades.append({
            'entry_time': entry_time, 'exit_time': df.index[-1],
            'dir': 'BUY' if direction == 1 else 'SELL',
            'entry': entry_price, 'exit': exit_price,
            'lots': lots, 'pnl': round(net_pnl, 2), 'result': 'WIN' if net_pnl > 0 else 'LOSS',
            'balance': round(balance, 2)
        })

    print(f"\n{'='*80}")
    print(f"  {cfg['name']} -- Today's Simulated Trades")
    print(f"{'='*80}")
    wins = sum(1 for t in trades if t['result'] == 'WIN')
    losses = sum(1 for t in trades if t['result'] == 'LOSS')
    total_pnl = sum(t['pnl'] for t in trades)
    print(f"  Trades: {len(trades)} | Wins: {wins} | Losses: {losses}")
    print(f"  Total P&L: ${total_pnl:.2f} | Final Balance: ${balance:.2f} | Return: {(balance - cfg['balance'])/cfg['balance']*100:.2f}%")
    print()
    for t in trades:
        duration = (t['exit_time'] - t['entry_time']).total_seconds() / 60
        print(f"  {t['entry_time'].strftime('%H:%M')} -> {t['exit_time'].strftime('%H:%M')} ({duration:.0f}m) | "
              f"{t['dir']} | Entry: {t['entry']:.2f} -> Exit: {t['exit']:.2f} | "
              f"Lots: {t['lots']} | P&L: ${t['pnl']:.2f} ({t['result']}) | Bal: ${t['balance']:.2f}")

mt5.shutdown()
