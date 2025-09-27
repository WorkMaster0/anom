# backtest_pattern_combos.py
import time, math, json, os
from datetime import datetime, timedelta, timezone
import requests
import pandas as pd
import numpy as np
import traceback
from collections import defaultdict, Counter

# --------- Параметри -------------
API_KEY = "stNlgF28IUaYQIgVzJPQe2q5uvC715YFrc2xIpMVyyFe0ER6A8jfDem0rOTbxQXU"     # опціонально
API_SECRET = "vyvwQBhjIGgLoV6jZbvCz7pfCtEFWUMlXIkw343E5PgzZlAHbEjGsRPzN8h9yeQq"  # опціонально
BINANCE_FAPI = "https://fapi.binance.com"
TOP_N = 12                       # скільки символів брати (щоб швидше)
INTERVAL = "15m"
DAYS = 92                        # ~3 місяці
LIMIT = 1500                     # max per request
FUTURE_LOOKAHEAD_BARS = 48      # скільки барів вперед дивимось на досягення TP/SL (48*15m = 12 год)
MIN_BARS_REQUIRED = 60
OUT_CSV = "combo_stats.csv"

# ---- Встав твої функції apply_pro_features та detect_signal_pro тут або імпортуй їх ----
# Для зручності я вставляю копію — якщо ти вже маєш їх у модулі, імпортуй замість вставки.
# --------------------------------------------------
import ta
def apply_pro_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["support"] = df["low"].rolling(20).min()
    df["resistance"] = df["high"].rolling(20).max()
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_spike"] = df["volume"] > 1.5 * df["vol_ma20"]
    df["volume_cluster"] = df["volume"] > 2 * df["vol_ma20"]
    df["body"] = df["close"] - df["open"]
    df["range"] = df["high"] - df["low"]
    df["upper_shadow"] = df["high"] - df[["close","open"]].max(axis=1)
    df["lower_shadow"] = df[["close","open"]].min(axis=1) - df["low"]
    df["liquidity_grab_long"] = (df["low"] < df["support"]) & (df["close"] > df["support"])
    df["liquidity_grab_short"] = (df["high"] > df["resistance"]) & (df["close"] < df["resistance"])
    df["false_break_high"] = (df["high"] > df["resistance"]) & (df["close"] < df["resistance"])
    df["false_break_low"] = (df["low"] < df["support"]) & (df["close"] > df["support"])
    df["bull_trap"] = (df["close"] < df["open"]) & (df["high"] > df["resistance"])
    df["bear_trap"] = (df["close"] > df["open"]) & (df["low"] < df["support"])
    df["retest_support"] = abs(df["close"] - df["support"]) / df["support"] < 0.003
    df["retest_resistance"] = abs(df["close"] - df["resistance"]) / df["resistance"] < 0.003
    df["trend_ma"] = df["close"].rolling(20).mean()
    df["trend_up"] = df["close"] > df["trend_ma"]
    df["trend_down"] = df["close"] < df["trend_ma"]
    df["long_lower_wick"] = df["lower_shadow"] > 2 * abs(df["body"])
    df["long_upper_wick"] = df["upper_shadow"] > 2 * abs(df["body"])
    df["imbalance_up"] = (df["body"] > 0) & (df["body"] > df["range"] * 0.6)
    df["imbalance_down"] = (df["body"] < 0) & (abs(df["body"]) > df["range"] * 0.6)
    df["atr"] = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range()
    df["squeeze"] = df["atr"] < df["atr"].rolling(50).mean() * 0.7
    df["delta_div_long"] = (df["body"] > 0) & (df["volume"] < df["vol_ma20"])
    df["delta_div_short"] = (df["body"] < 0) & (df["volume"] < df["vol_ma20"])
    df["breakout_cont_long"] = (df["close"] > df["resistance"]) & (df["volume"] > df["vol_ma20"])
    df["breakout_cont_short"] = (df["close"] < df["support"]) & (df["volume"] > df["vol_ma20"])
    df["combo_bullish"] = df["imbalance_up"] & df["vol_spike"] & df["trend_up"]
    df["combo_bearish"] = df["imbalance_down"] & df["vol_spike"] & df["trend_down"]
    df["accumulation_zone"] = ((df["range"] < df["range"].rolling(20).mean() * 0.5) &
                                (df["volume"] > df["vol_ma20"]))
    return df

def detect_signal_pro(df: pd.DataFrame):
    last = df.iloc[-1]
    votes = []
    confidence = 0.5
    pretop = False
    if last["liquidity_grab_long"]: votes.append("liquidity_grab_long"); confidence += 0.08
    if last["liquidity_grab_short"]: votes.append("liquidity_grab_short"); confidence += 0.08
    if last["bull_trap"]: votes.append("bull_trap"); confidence += 0.05
    if last["bear_trap"]: votes.append("bear_trap"); confidence += 0.05
    if last["false_break_high"]: votes.append("false_break_high"); confidence += 0.05
    if last["false_break_low"]: votes.append("false_break_low"); confidence += 0.05
    if last["volume_cluster"]: votes.append("volume_cluster"); confidence += 0.05
    if last["breakout_cont_long"]: votes.append("breakout_cont_long"); confidence += 0.07
    if last["breakout_cont_short"]: votes.append("breakout_cont_short"); confidence += 0.07
    if last["imbalance_up"]: votes.append("imbalance_up"); confidence += 0.05
    if last["imbalance_down"]: votes.append("imbalance_down"); confidence += 0.05
    if last["squeeze"]: votes.append("volatility_squeeze"); confidence += 0.03
    if last["trend_up"]: votes.append("trend_up"); confidence += 0.05
    if last["trend_down"]: votes.append("trend_down"); confidence += 0.05
    if last["long_lower_wick"]: votes.append("long_lower_wick"); confidence += 0.04
    if last["long_upper_wick"]: votes.append("long_upper_wick"); confidence += 0.04
    if last["retest_support"]: votes.append("retest_support"); confidence += 0.05
    if last["retest_resistance"]: votes.append("retest_resistance"); confidence += 0.05
    if last["delta_div_long"]: votes.append("delta_div_long"); confidence += 0.06
    if last["delta_div_short"]: votes.append("delta_div_short"); confidence += 0.06
    if last["combo_bullish"]: votes.append("combo_bullish"); confidence += 0.1
    if last["combo_bearish"]: votes.append("combo_bearish"); confidence += 0.1
    if last["accumulation_zone"]: votes.append("accumulation_zone"); confidence += 0.03
    if len(df) >= 10 and (last["close"] - df["close"].iloc[-10]) / df["close"].iloc[-10] > 0.10:
        pretop = True
        votes.append("pretop"); confidence += 0.1
    action = "WATCH"
    if "combo_bullish" in votes or "breakout_cont_long" in votes or "delta_div_long" in votes:
        action = "LONG"
    elif "combo_bearish" in votes or "breakout_cont_short" in votes or "delta_div_short" in votes:
        action = "SHORT"
    else:
        near_resistance = last["close"] >= last["resistance"] * 0.98 if not pd.isna(last["resistance"]) else False
        near_support = last["close"] <= last["support"] * 1.02 if not pd.isna(last["support"]) else False
        if near_resistance: action = "SHORT"
        elif near_support: action = "LONG"
    confidence = max(0.0, min(1.0, confidence))
    return action, votes, pretop, last, confidence

# -------------- утиліти для завантаження -------------
def unix_ms(dt):
    return int(dt.replace(tzinfo=timezone.utc).timestamp()*1000)

def fetch_top_symbols(limit=TOP_N):
    # повертає top по абсолютній зміні % за 24h
    r = requests.get(f"{BINANCE_FAPI}/fapi/v1/ticker/24hr", timeout=15)
    arr = r.json()
    usdt = [t for t in arr if t["symbol"].endswith("USDT")]
    usdt_sorted = sorted(usdt, key=lambda x: abs(float(x.get("priceChangePercent", 0))), reverse=True)
    return [d["symbol"] for d in usdt_sorted[:limit]]

def fetch_klines_range(symbol, interval, start_ms, end_ms):
    out = []
    cur_start = start_ms
    while True:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": cur_start,
            "endTime": end_ms,
            "limit": LIMIT
        }
        r = requests.get(f"{BINANCE_FAPI}/fapi/v1/klines", params=params, timeout=20)
        data = r.json()
        if not data:
            break
        out.extend(data)
        if len(data) < LIMIT:
            break
        # advance: last open time + interval
        last_open = int(data[-1][0])
        # next start = last_open + 1 ms OR compute interval length
        cur_start = last_open + 1
        # safety sleep to avoid rate limits
        time.sleep(0.2)
    # convert to df
    df = pd.DataFrame(out, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","quote_asset_volume","trades",
        "taker_buy_base","taker_buy_quote","ignore"
    ])
    if df.empty:
        return None
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.set_index("open_time", inplace=True)
    return df

# --------------- симулятор сигналів ----------------
def simulate_signals_on_df(df):
    df = df.copy()
    df = apply_pro_features(df)
    results = []  # list of dicts for each signal occurrence
    # iterate over bars starting from MIN_BARS_REQUIRED to end-1
    for i in range(MIN_BARS_REQUIRED, len(df)-1):
        window = df.iloc[:i+1].copy()
        action, votes, pretop, last, confidence = detect_signal_pro(window)
        if action == "WATCH":
            continue
        # compute entry/sl/tp as in твоєму коді (use last row values)
        if action == "LONG":
            entry = last["support"] * 1.001
            stop_loss = last["support"] * 0.99
            tp1 = entry + (last["resistance"] - entry) * 0.33
            tp2 = entry + (last["resistance"] - entry) * 0.66
            tp3 = last["resistance"]
        else:
            entry = last["resistance"] * 0.999
            stop_loss = last["resistance"] * 1.01
            tp1 = entry - (entry - last["support"]) * 0.33
            tp2 = entry - (entry - last["support"]) * 0.66
            tp3 = last["support"]
        # simulate next FUTURE_LOOKAHEAD_BARS bars
        future = df.iloc[i+1 : i+1+FUTURE_LOOKAHEAD_BARS]
        hit = {"tp1": False, "tp2": False, "tp3": False, "sl": False, "time_to_hit": None}
        for j, (_, frow) in enumerate(future.iterrows(), start=1):
            # For LONG: high >= tp => hit; low <= sl => sl
            if action == "LONG":
                if not hit["tp1"] and frow["high"] >= tp1: hit["tp1"] = True; hit["time_to_hit"] = j
                if not hit["tp2"] and frow["high"] >= tp2: hit["tp2"] = True
                if not hit["tp3"] and frow["high"] >= tp3: hit["tp3"] = True
                if frow["low"] <= stop_loss:
                    hit["sl"] = True
                    if hit["time_to_hit"] is None:
                        hit["time_to_hit"] = j
                    break
            else:
                if not hit["tp1"] and frow["low"] <= tp1: hit["tp1"] = True; hit["time_to_hit"] = j
                if not hit["tp2"] and frow["low"] <= tp2: hit["tp2"] = True
                if not hit["tp3"] and frow["low"] <= tp3: hit["tp3"] = True
                if frow["high"] >= stop_loss:
                    hit["sl"] = True
                    if hit["time_to_hit"] is None:
                        hit["time_to_hit"] = j
                    break
        # quality metrics
        rr1 = (tp1 - entry)/(entry - stop_loss) if action=="LONG" else (entry - tp1)/(stop_loss - entry)
        rr2 = (tp2 - entry)/(entry - stop_loss) if action=="LONG" else (entry - tp2)/(stop_loss - entry)
        rr3 = (tp3 - entry)/(entry - stop_loss) if action=="LONG" else (entry - tp3)/(stop_loss - entry)
        results.append({
            "time": last.name, "action": action, "votes": tuple(sorted(votes)),
            "entry": entry, "sl": stop_loss, "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "hit_tp1": hit["tp1"], "hit_tp2": hit["tp2"], "hit_tp3": hit["tp3"], "hit_sl": hit["sl"],
            "time_to_hit": hit["time_to_hit"], "rr1": rr1, "rr2": rr2, "rr3": rr3, "confidence": confidence
        })
    return results

# --------------- головна логіка ---------------
def main():
    try:
        end = datetime.utcnow().replace(tzinfo=timezone.utc)
        start = end - timedelta(days=DAYS)
        start_ms = unix_ms(start)
        end_ms = unix_ms(end)
        symbols = fetch_top_symbols(limit=TOP_N)
        print("Top symbols:", symbols)
        all_results = []
        for sym in symbols:
            print("Fetching", sym)
            df = fetch_klines_range(sym, INTERVAL, start_ms, end_ms)
            if df is None or len(df) < MIN_BARS_REQUIRED:
                print("skip", sym, "not enough bars")
                continue
            res = simulate_signals_on_df(df)
            print(f"  signals found: {len(res)}")
            for r in res:
                r["symbol"] = sym
            all_results.extend(res)
            # safety sleep
            time.sleep(0.5)
        if not all_results:
            print("No signals in dataset.")
            return
        dfres = pd.DataFrame(all_results)
        # aggregate by (action, votes)
        agg = []
        grouped = dfres.groupby(["action","votes"])
        for (action,votes), g in grouped:
            cnt = len(g)
            hit1 = g["hit_tp1"].mean()
            hit2 = g["hit_tp2"].mean()
            hit3 = g["hit_tp3"].mean()
            sl_rate = g["hit_sl"].mean()
            mean_rr1 = g["rr1"].replace([np.inf, -np.inf], np.nan).mean()
            median_time = g["time_to_hit"].dropna().median()
            agg.append({
                "action": action, "votes": "|".join(votes), "count": cnt,
                "hit1": hit1, "hit2": hit2, "hit3": hit3, "sl_rate": sl_rate,
                "mean_rr1": mean_rr1, "median_time_to_hit_bars": median_time
            })
        aggdf = pd.DataFrame(agg).sort_values(["action","hit1"], ascending=[True, False])
        aggdf.to_csv(OUT_CSV, index=False)
        print("Saved aggregated combo stats to", OUT_CSV)
        # print top 5 for each action
        for act in ["LONG","SHORT"]:
            top = aggdf[aggdf["action"]==act].sort_values("hit1", ascending=False).head(5)
            print("\nTop combos for", act)
            print(top[["votes","count","hit1","hit2","hit3","sl_rate","mean_rr1","median_time_to_hit_bars"]].to_string(index=False))
    except Exception as e:
        print("Error:", e)
        traceback.print_exc()

if __name__ == "__main__":
    main()