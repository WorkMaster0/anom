#!/usr/bin/env python3
"""
SMC Telegram webhook bot Render-ready (v3)
- Full scan for USDT pairs on MEXC (2h TF)
- Monitors zones and prebreak alerts
- Sends charts via Telegram
- All-in-one single file
"""

import os, time, json, logging, io, threading, requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import mplfinance as mpf
from flask import Flask, request, jsonify

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
PORT = int(os.getenv("PORT", "5000"))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

MEXC_REST_BASE = "https://api.mexc.com"
TF = "2h"
KLIMIT = 500
SCAN_WORKERS = int(os.getenv("SCAN_WORKERS", "8"))

ORDER_BLOCK_LOOKBACK = int(os.getenv("ORDER_BLOCK_LOOKBACK", "24"))
MIN_BODY_THRESHOLD = float(os.getenv("MIN_BODY_THRESHOLD", "0.002"))
FVG_MIN_GAP_PCT = float(os.getenv("FVG_MIN_GAP_PCT", "0.001"))
ZONE_PADDING_PCT = float(os.getenv("ZONE_PADDING_PCT", "0.002"))

PREBREAK_ENABLED = True
PREBREAK_PCT = float(os.getenv("PREBREAK_PCT", "0.01"))

SYMBOL_CACHE_TTL = 3600
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "smc_state.json")
SYMBOL_CACHE_FILE = os.path.join(DATA_DIR, "symbols_cache.json")

MIN_REQUEST_INTERVAL = float(os.getenv("MIN_REQUEST_INTERVAL", "0.06"))
_last_request_time = 0.0
_rate_lock = threading.Lock()

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("smc-webhook-v3")

# ---------------- STATE ----------------
def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.exception("load_state error: %s", e)
    return {"zones": {}, "last_scan": None, "events": [], "watchlist": [], "prebreak": {"enabled": PREBREAK_ENABLED, "pct": PREBREAK_PCT}, "events_marker": {}}

def save_state(st):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f, indent=2, default=str)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logger.exception("save_state error: %s", e)

state = load_state()

# ---------------- HTTP GET ----------------
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_maxsize=50, max_retries=2)
session.mount("https://", adapter)
session.mount("http://", adapter)

def rate_limited_get(url, params=None, timeout=12):
    global _last_request_time
    with _rate_lock:
        now = time.time()
        elapsed = now - _last_request_time
        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)
        r = session.get(url, params=params, timeout=timeout)
        _last_request_time = time.time()
    return r

# ---------------- TELEGRAM ----------------
def escape_md_v2(text: str) -> str:
    esc = str(text)
    for ch in r"_*[]()~`>#+-=|{}.!":
        esc = esc.replace(ch, "\\"+ch)
    return esc

def send_telegram(text: str, photo_bytes: bytes = None):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.debug("telegram disabled, would send: %s", text[:200])
        return
    try:
        if photo_bytes is not None:
            files = {'photo': ('chart.png', photo_bytes, 'image/png')}
            data = {'chat_id': CHAT_ID, 'caption': escape_md_v2(text), 'parse_mode': 'MarkdownV2'}
            session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto", data=data, files=files, timeout=15)
        else:
            payload = {"chat_id": CHAT_ID, "text": escape_md_v2(text), "parse_mode": "MarkdownV2"}
            session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        logger.exception("send_telegram error: %s", e)

def set_webhook_if_render():
    if not TELEGRAM_TOKEN or not RENDER_EXTERNAL_URL:
        return
    webhook_url = f"{RENDER_EXTERNAL_URL}/telegram_webhook/{TELEGRAM_TOKEN}"
    try:
        r = session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook", params={"url": webhook_url}, timeout=15)
        send_telegram(f"Webhook set to {webhook_url}")
    except Exception as e:
        logger.exception("set_webhook_if_render error: %s", e)

# ---------------- MEXC SYMBOLS ----------------
def get_spot_symbols_cached(limit=1000):
    try:
        if os.path.exists(SYMBOL_CACHE_FILE):
            with open(SYMBOL_CACHE_FILE, "r") as f:
                cache = json.load(f)
            if time.time() - cache.get("fetched_at",0) < SYMBOL_CACHE_TTL:
                return cache.get("symbols",[])[:limit]
    except: pass
    try:
        r = rate_limited_get(MEXC_REST_BASE + "/api/v3/ticker/24hr", timeout=15)
        data = r.json()
        syms = [d['symbol'] for d in data if d.get('symbol','').endswith("USDT")]
        with open(SYMBOL_CACHE_FILE, "w") as f:
            json.dump({"fetched_at": time.time(), "symbols": syms}, f)
        return syms[:limit]
    except: return ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT"]

def fetch_klines(symbol, interval, limit=500):
    try:
        params = {"symbol":symbol, "interval":interval, "limit":limit}
        r = rate_limited_get(MEXC_REST_BASE+"/api/v3/klines", params=params, timeout=15)
        arr = r.json()
        if not isinstance(arr,list): return None
        df = pd.DataFrame(arr, columns=["open_time","open","high","low","close","volume","close_time","qav","trades","taker_base","taker_quote","ignore"])
        for c in ["open","high","low","close","volume"]: df[c] = df[c].astype(float)
        df["open_time"]=pd.to_datetime(df["open_time"],unit="ms",utc=True)
        df.set_index("open_time", inplace=True)
        return df
    except: return None

# ---------------- SMC ----------------
def detect_order_blocks(df):
    zones=[]
    n=len(df); start=max(2,n-ORDER_BLOCK_LOOKBACK-3)
    for i in range(start,n-1):
        c=df.iloc[i]; p=df.iloc[i-1]
        body=abs(c['close']-c['open'])
        body_pct = body/c['close'] if c['close'] else 0
        if body_pct<MIN_BODY_THRESHOLD: continue
        if c['close']>c['open'] and p['close']<p['open'] and c['open']<p['close']:
            low=min(c['open'],c['close'],p['low']); high=max(c['open'],c['close'],p['high'])
            zones.append({'type':'orderblock','side':'bull','zone':[low,high],'index':i})
        if c['close']<c['open'] and p['close']>p['open'] and c['open']>p['close']:
            low=min(c['open'],c['close'],p['low']); high=max(c['open'],c['close'],p['high'])
            zones.append({'type':'orderblock','side':'bear','zone':[low,high],'index':i})
    return zones

def detect_fvg(df):
    zones=[]; n=len(df); start=max(0,n-ORDER_BLOCK_LOOKBACK-4)
    for i in range(start,n-2):
        A=df.iloc[i]; B=df.iloc[i+1]; C=df.iloc[i+2]
        if B['close']<B['open'] and A['high']<C['low']:
            gap_pct=(C['low']-A['high'])/(A['high'] if A['high'] else 1)
            if gap_pct>=FVG_MIN_GAP_PCT:
                zones.append({'type':'fvg','side':'bull','zone':[A['high'],C['low']],'index':i+1})
        if B['close']>B['open'] and A['low']>C['high']:
            gap_pct=(A['low']-C['high'])/(C['high'] if C['high'] else 1)
            if gap_pct>=FVG_MIN_GAP_PCT:
                zones.append({'type':'fvg','side':'bear','zone':[C['high'],A['low']],'index':i+1})
    return zones

def expand_zone(zone):
    low,high=zone; pad=(high-low)*ZONE_PADDING_PCT; return [low-pad,high+pad]

def build_chart_bytes(df,symbol,interval,zone=None):
    try:
        fig, axes = mpf.plot(df.tail(200), type='candle', style='yahoo', returnfig=True)
        ax=axes[0] if isinstance(axes,list) else axes
        if zone: ax.axhspan(zone[0],zone[1],alpha=0.25,color='orange')
        ax.set_title(f"{symbol} [{interval}]")
        buf=io.BytesIO(); fig.savefig(buf,format='png',bbox_inches='tight'); plt.close(fig); buf.seek(0)
        return buf.getvalue()
    except: return None

def find_zones_for_symbol(symbol):
    df = fetch_klines(symbol,TF,KLIMIT)
    if df is None or len(df)<60: return []
    ob=detect_order_blocks(df); fvg=detect_fvg(df)
    zones = ob+fvg
    results=[]
    for z in zones:
        ez=expand_zone(z['zone']); zid=f"{symbol}|{TF}|{z['type']}|{z['index']}"
        results.append({"id":zid,"symbol":symbol,"tf":TF,"type":z['type'],"side":z.get('side'),"zone":ez,"detected_at":datetime.now(timezone.utc).isoformat(),"hit_at":None})
    return results

# ---------------- SCAN ----------------
def scan_all_pairs_and_store():
    logger.info("Starting full scan for all USDT pairs TF=%s",TF)
    symbols=get_spot_symbols_cached()
    new=0
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        futures={ex.submit(find_zones_for_symbol,s):s for s in symbols}
        for fut,s in futures.items():
            try:
                zs=fut.result(timeout=90)
                if not zs: continue
                for z in zs:
                    symzones=state.setdefault("zones",{}).setdefault(z['symbol'],[])
                    if any(x['id']==z['id'] for x in symzones): continue
                    symzones.append(z); new+=1
                    txt=f"🔎 Zone detected\nSymbol:{z['symbol']}\nTF:{z['tf']}\nType:{z['type']}\nZone:{z['zone'][0]:.6f}-{z['zone'][1]:.6f}\nDetected:{z['detected_at']}"
                    df_chart=fetch_klines(z['symbol'],z['tf'],limit=200)
                    img=build_chart_bytes(df_chart,z['symbol'],z['tf'],zone=z['zone']) if df_chart else None
                    send_telegram(txt,photo_bytes=img)
            except Exception as e: logger.exception("scan worker error %s: %s",s,e)
    state['last_scan']=datetime.now(timezone.utc).isoformat()
    save_state(state)
    logger.info("Scan finished, new zones=%d",new)

# ---------------- PAIR ----------------
def analyze_pair_and_send(symbol):
    zs=find_zones_for_symbol(symbol)
    if not zs: send_telegram(f"🔍 No zones found for {symbol} on {TF}."); return
    for z in zs:
        txt=f"🔎 Zone detected\nSymbol:{z['symbol']}\nTF:{z['tf']}\nType:{z['type']}\nZone:{z['zone'][0]:.6f}-{z['zone'][1]:.6f}\nDetected:{z['detected_at']}"
        df_chart=fetch_klines(z['symbol'],z['tf'],limit=200)
        img=build_chart_bytes(df_chart,z['symbol'],z['tf'],zone=z['zone']) if df_chart else None
        send_telegram(txt,photo_bytes=img)
        symzones=state.setdefault("zones",{}).setdefault(z['symbol'],[])
        if not any(x['id']==z['id'] for x in symzones): symzones.append(z)
    save_state(state)

# ---------------- WATCHLIST ----------------
def add_watch(symbol):
    sym=symbol.upper()
    if sym not in state.get("watchlist",[]): state.setdefault("watchlist",[]).append(sym); save_state(state); return True
    return False

def remove_watch(symbol):
    sym=symbol.upper()
    if sym in state.get("watchlist",[]): state["watchlist"].remove(sym); save_state(state); return True
    return False

# ---------------- MONITOR ----------------
def monitor_zone_hits():
    while True:
        try:
            now=datetime.now(timezone.utc)
            cfg=state.get("prebreak",{"enabled":PREBREAK_ENABLED,"pct":PREBREAK_PCT})
            for symbol,zones in list(state.get("zones",{}).items()):
                df_latest=fetch_klines(symbol,"1m",limit=2)
                if df_latest is None or df_latest.empty: continue
                last_price=float(df_latest['close'].iloc[-1])
                for z in zones:
                    if z.get("hit_at"): continue
                    low,high=z['zone']
                    # HIT
                    if low<=last_price<=high:
                        txt=f"✅ Zone HIT\nSymbol:{symbol}\nTF:{z['tf']}\nType:{z['type']}\nPrice:{last_price:.6f}\nZone:{low:.6f}-{high:.6f}\nDetected:{z['detected_at']}"
                        df_chart=fetch_klines(symbol,z['tf'],limit=200)
                        img=build_chart_bytes(df_chart,symbol,z['tf'],zone=z['zone']) if df_chart else None
                        send_telegram(txt,photo_bytes=img)
                        z['hit_at']=now.isoformat()
                        state.setdefault("events",[]).append({"symbol":symbol,"zone_id":z['id'],"hit_at":now.isoformat(),"price":last_price})
                        save_state(state)
                    # PREBREAK
                    elif cfg.get("enabled",False):
                        pct=cfg.get("pct",PREBREAK_PCT)
                        key=f"prebreak:{z['id']}"
                        if last_price>high and (last_price-high)/high<=pct and not state.get("events_marker",{}).get(key):
                            txt=f"⚠️ Approaching zone (from above)\nSymbol:{symbol}\nTF:{z['tf']}\nType:{z['type']}\nPrice:{last_price:.6f}\nZone top:{high:.6f}\nDistance:{(last_price-high)/high:.4%}"
                            df_chart=fetch_klines(symbol,z['tf'],limit=200)
                            img=build_chart_bytes(df_chart,symbol,z['tf'],zone=z['zone']) if df_chart else None
                            send_telegram(txt,photo_bytes=img)
                            state.setdefault("events_marker",{})[key]=now.isoformat(); save_state(state)
                        elif last_price<low and (low-last_price)/low<=pct and not state.get("events_marker",{}).get(key):
                            txt=f"⚠️ Approaching zone (from below)\nSymbol:{symbol}\nTF:{z['tf']}\nType:{z['type']}\nPrice:{last_price:.6f}\nZone bottom:{low:.6f}\nDistance:{(low-last_price)/low:.4%}"
                            df_chart=fetch_klines(symbol,z['tf'],limit=200)
                            img=build_chart_bytes(df_chart,symbol,z['tf'],zone=z['zone']) if df_chart else None
                            send_telegram(txt,photo_bytes=img)
                            state.setdefault("events_marker",{})[key]=now.isoformat(); save_state(state)
        except Exception as e:
            logger.exception("monitor error: %s",e)
        time.sleep(20)

# ---------------- FLASK ----------------
app=Flask(__name__)

@app.route("/telegram_webhook/<token>",methods=["POST"])
def telegram_webhook(token):
    if token!=TELEGRAM_TOKEN: return jsonify({"ok":False,"error":"invalid token"}),403
    update=request.get_json(force=True)
    msg=update.get("message") or update.get("edited_message") or {}
    text=msg.get("text","").strip()
    if not text: return jsonify({"ok":True})
    parts=text.split(); cmd=parts[0].lower()
    if cmd=="/scan": threading.Thread(target=scan_all_pairs_and_store,daemon=True).start(); send_telegram("⚡ Full scan started...")
    elif cmd=="/pair" and len(parts)>1: threading.Thread(target=analyze_pair_and_send,args=(parts[1].upper(),),daemon=True).start(); send_telegram(f"🔍 Analyzing {parts[1].upper()}...")
    elif cmd=="/watch" and len(parts)>1: added=add_watch(parts[1].upper()); send_telegram(f"{parts[1].upper()} added" if added else "Already in watchlist")
    elif cmd=="/unwatch" and len(parts)>1: removed=remove_watch(parts[1].upper()); send_telegram(f"{parts[1].upper()} removed" if removed else "Not in watchlist")
    elif cmd=="/status": send_telegram(f"Zones:{sum(len(v) for v in state.get('zones',[]))} Watchlist:{state.get('watchlist',[])}")
    return jsonify({"ok":True})

@app.route("/status")
def status(): return jsonify({"zones":state.get("zones",{}),"watchlist":state.get("watchlist",[]),"events":state.get("events",[])})

# ---------------- START ----------------
if __name__=="__main__":
    import multiprocessing
    logger.info("Starting SMC bot v3 (TF=%s)",TF)

    # запустимо моніторинг тільки у режимі локального запуску
    if os.environ.get("RUN_MONITOR", "1")=="1":
        p = multiprocessing.Process(target=monitor_zone_hits, daemon=True)
        p.start()

    set_webhook_if_render()
    app.run(host="0.0.0.0",port=PORT)