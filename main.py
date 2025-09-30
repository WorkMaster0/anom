import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import io
from datetime import datetime, timezone

# ========== Indicators ==========
def ema(series, period=50):
    return series.ewm(span=period).mean()

def adx(df, period=14):
    df = df.copy()
    df["H-L"] = df["high"] - df["low"]
    df["H-C"] = (df["high"] - df["close"].shift()).abs()
    df["L-C"] = (df["low"] - df["close"].shift()).abs()
    df["TR"] = df[["H-L","H-C","L-C"]].max(axis=1)
    df["+DM"] = np.where((df["high"]-df["high"].shift()) > (df["low"].shift()-df["low"]), 
                         np.maximum(df["high"]-df["high"].shift(),0),0)
    df["-DM"] = np.where((df["low"].shift()-df["low"]) > (df["high"]-df["high"].shift()), 
                         np.maximum(df["low"].shift()-df["low"],0),0)
    df["+DI"] = 100*(df["+DM"].ewm(span=period).mean()/df["TR"].ewm(span=period).mean())
    df["-DI"] = 100*(df["-DM"].ewm(span=period).mean()/df["TR"].ewm(span=period).mean())
    df["DX"] = (abs(df["+DI"]-df["-DI"])/(df["+DI"]+df["-DI"]))*100
    return df["DX"].ewm(span=period).mean()

# ========== Swing Levels ==========
def find_swing_low(df, lookback=20):
    lows = df["low"].rolling(lookback).min()
    return lows.iloc[-1]

def find_swing_high(df, lookback=20):
    highs = df["high"].rolling(lookback).max()
    return highs.iloc[-1]

# ========== Entry Logic ==========
def detect_signal(df):
    df["ema50"] = ema(df["close"], 50)
    df["adx"] = adx(df)

    last = df.iloc[-1]
    prev = df.iloc[-2]

    action = None

    # Long: ціна вище EMA, ADX > 20, довгий нижній хвіст
    if last["close"] > last["ema50"] and last["adx"] > 20:
        if (last["low"] < prev["low"]) and (last["close"] > (last["open"] + last["high"]) / 2):
            action = "LONG"

    # Short: ціна нижче EMA, ADX > 20, довгий верхній хвіст
    if last["close"] < last["ema50"] and last["adx"] > 20:
        if (last["high"] > prev["high"]) and (last["close"] < (last["open"] + last["low"]) / 2):
            action = "SHORT"

    return action

# ========== TP/SL ==========
def calculate_levels(df, action, rr_target=2.5):
    entry = df["close"].iloc[-1]
    if action == "LONG":
        sl = find_swing_low(df, 20)
        tp = entry + (entry - sl) * rr_target
    else:
        sl = find_swing_high(df, 20)
        tp = entry - (sl - entry) * rr_target
    return entry, sl, tp

# ========== Backtest ==========
def backtest(df):
    signals = []
    for i in range(50, len(df)-20):
        sub = df.iloc[:i+1].copy()
        action = detect_signal(sub)
        if action:
            entry, sl, tp = calculate_levels(sub, action)
            future = df.iloc[i+1:i+21]
            hit_tp = (future["high"] >= tp).any() if action=="LONG" else (future["low"] <= tp).any()
            hit_sl = (future["low"] <= sl).any() if action=="LONG" else (future["high"] >= sl).any()
            result = "TP" if hit_tp else "SL" if hit_sl else "NONE"
            signals.append((sub.index[-1], action, entry, sl, tp, result))
    return pd.DataFrame(signals, columns=["time","action","entry","sl","tp","result"])

# ========== Plot ==========
def plot_signal(df, entry, sl, tp, action, symbol="PAIR"):
    df = df.tail(100)
    fig, ax = plt.subplots(figsize=(12,6))

    # Свічки
    for idx, row in df.iterrows():
        color = "green" if row["close"] >= row["open"] else "red"
        ax.plot([idx, idx], [row["low"], row["high"]], color=color)
        ax.add_patch(plt.Rectangle((idx, min(row["open"],row["close"])),
                                   0.5, abs(row["close"]-row["open"]),
                                   color=color))

    ax.axhline(entry, color="orange", linestyle="--", label=f"Entry {entry:.4f}")
    ax.axhline(sl, color="red", linestyle="--", label=f"SL {sl:.4f}")
    ax.axhline(tp, color="green", linestyle="--", label=f"TP {tp:.4f}")

    ax.set_title(f"{symbol} {action} Signal")
    ax.legend()

    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf