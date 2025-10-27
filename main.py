#!/usr/bin/env python3
"""
SMC Telegram webhook bot для Render — повний готовий файл
"""
import os, time, json, logging, io
from datetime import datetime, timezone
from threading import Thread, Lock
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests
import matplotlib.pyplot as plt
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

ORDER_BLOCK_LOOKBACK = 24
MIN_BODY_THRESHOLD = 0.002
FVG_MIN_GAP_PCT = 0.001
ZONE_PADDING_PCT = 0.002

PREBREAK_ENABLED = True
PREBREAK_PCT = 0.01

SYMBOL_CACHE_TTL = 3600
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "smc_state.json")
SYMBOL_CACHE_FILE = os.path.join(DATA_DIR, "symbols_cache.json")

MIN_REQUEST_INTERVAL = 0.06
_last_request_time = 0.0
_rate_lock = Lock()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("smc-webhook-v2")

# ---------------- STATE ----------------
def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE,"r") as f:
                return json.load(f)
    except:
        pass
    return {"zones": {}, "last_scan": None, "events": [], "watchlist": [], "prebreak":{"enabled":PREBREAK_ENABLED,"pct":PREBREAK_PCT}}

def save_state(st):
    try:
        tmp = STATE_FILE+".tmp"
        with open(tmp,"w") as f:
            json.dump(st,f,indent=2,default=str)
        os.replace(tmp,STATE_FILE)
    except Exception as e:
        logger.exception("save_state error: %s", e)

state = load_state()

# ---------------- HTTP ----------------
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_maxsize=50,max_retries=2)
session.mount("https://",adapter)
session.mount("http://",adapter)

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
def escape_md_v2(text:str)->str:
    esc = str(text)
    for ch in r"_*[]()~`>#+-=|{}.!":
        esc = esc.replace(ch,"\\"+ch)
    return esc

def send_telegram(text:str, photo_bytes:bytes=None):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.debug("telegram disabled, would send: %s", text[:200])
        return
    try:
        if photo_bytes:
            files={'photo':('chart.png',photo_bytes,'image/png')}
            data={'chat_id':CHAT_ID,'caption':escape_md_v2(text),'parse_mode':'MarkdownV2'}
            r=session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",data=data,files=files,timeout=15)
        else:
            payload={"chat_id":CHAT_ID,"text":escape_md_v2(text),"parse_mode":"MarkdownV2"}
            r=session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",json=payload,timeout=10)
        logger.info("Telegram send response: %s %s", r.status_code, r.text)
    except Exception as e:
        logger.exception("send_telegram error: %s", e)

def set_webhook_if_render():
    if not TELEGRAM_TOKEN or not RENDER_EXTERNAL_URL:
        logger.info("Skipping webhook setup")
        return
    webhook_url = f"{RENDER_EXTERNAL_URL}/telegram_webhook/{TELEGRAM_TOKEN}"
    try:
        logger.info("Registering webhook: %s", webhook_url)
        r=session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook", params={"url":webhook_url},timeout=15)
        logger.info("setWebhook response: %s", r.json())
        send_telegram(f"Webhook set to {webhook_url}")
    except Exception as e:
        logger.exception("Webhook setup error: %s", e)

# ---------------- MEXC ----------------
def get_spot_symbols_cached(limit=1000):
    try:
        if os.path.exists(SYMBOL_CACHE_FILE):
            with open(SYMBOL_CACHE_FILE,"r") as f:
                cache=json.load(f)
            if time.time()-cache.get("fetched_at",0)<SYMBOL_CACHE_TTL:
                return cache.get("symbols",[])[:limit]
    except:
        pass
    try:
        r=rate_limited_get(MEXC_REST_BASE+"/api/v3/ticker/24hr",timeout=15)
        data=r.json()
        syms=[d['symbol'] for d in data if d.get('symbol','').endswith("USDT")]
        with open(SYMBOL_CACHE_FILE,"w") as f:
            json.dump({"fetched_at":time.time(),"symbols":syms},f)
        return syms[:limit]
    except:
        return ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT"]

def fetch_klines(symbol:str, interval:str, limit:int=500):
    try:
        params={"symbol":symbol,"interval":interval,"limit":limit}
        r=rate_limited_get(MEXC_REST_BASE+"/api/v3/klines",params=params,timeout=15)
        arr=r.json()
        if not isinstance(arr,list): return None
        df=pd.DataFrame(arr,columns=["open_time","open","high","low","close","volume","close_time","qav","trades","taker_base","taker_quote","ignore"])
        for c in ["open","high","low","close","volume"]: df[c]=df[c].astype(float)
        df["open_time"]=pd.to_datetime(df["open_time"],unit="ms",utc=True)
        df.set_index("open_time",inplace=True)
        return df
    except Exception as e:
        logger.debug("fetch_klines error %s %s", symbol, e)
        return None

# ---------------- SMC ----------------
def detect_order_blocks(df):
    zones=[]
    n=len(df)
    start=max(2,n-ORDER_BLOCK_LOOKBACK-3)
    for i in range(start,n-1):
        c,p=df.iloc[i],df.iloc[i-1]
        body=abs(c['close']-c['open'])
        body_pct=(body/c['close']) if c['close'] else 0
        if body_pct<MIN_BODY_THRESHOLD: continue
        if c['close']>c['open'] and p['close']<p['open'] and c['open']<p['close']:
            low=min(c['open'],c['close'],p['low']); high=max(c['open'],c['close'],p['high'])
            zones.append({'type':'orderblock','side':'bull','zone':[low,high],'index':i})
        if c['close']<c['open'] and p['close']>p['open'] and c['open']>p['close']:
            low=min(c['open'],c['close'],p['low']); high=max(c['open'],c['close'],p['high'])
            zones.append({'type':'orderblock','side':'bear','zone':[low,high],'index':i})
    return zones

def detect_fvg(df):
    zones=[]
    n=len(df)
    start=max(0,n-ORDER_BLOCK_LOOKBACK-4)
    for i in range(start,n-2):
        A,B,C=df.iloc[i],df.iloc[i+1],df.iloc[i+2]
        if B['close']<B['open'] and A['high']<C['low']:
            gap=(C['low']-A['high'])/(A['high'] if A['high'] else 1)
            if gap>=FVG_MIN_GAP_PCT:
                zones.append({'type':'fvg','side':'bull','zone':[A['high'],C['low']],'index':i+1})
        if B['close']>B['open'] and A['low']>C['high']:
            gap=(A['low']-C['high'])/(C['high'] if C['high'] else 1)
            if gap>=FVG_MIN_GAP_PCT:
                zones.append({'type':'fvg','side':'bear','zone':[C['high'],A['low']],'index':i+1})
    return zones

def expand_zone(zone):
    low,high=zone
    pad=(high-low)*ZONE_PADDING_PCT
    return [low-pad,high+pad]

def build_chart_bytes(df,symbol,interval,zone=None):
    try:
        fig,axes=mpf.plot(df.tail(200),type='candle',style='yahoo',returnfig=True)
        ax=axes[0] if isinstance(axes,list) else axes
        if zone: ax.axhspan(zone[0],zone[1],alpha=0.25,color='orange')
        ax.set_title(f"{symbol} [{interval}]")
        buf=io.BytesIO()
        fig.savefig(buf,format='png',bbox_inches='tight')
        plt.close(fig)
        buf.seek(0)
        return buf.getvalue()
    except Exception as e:
        logger.exception("build_chart_bytes error: %s", e)
        return None

def find_zones_for_symbol(symbol:str):
    df=fetch_klines(symbol,TF,KLIMIT)
    if df is None or len(df)<60: return []
    ob=detect_order_blocks(df)
    fvg=detect_fvg(df)
    zones=ob+fvg
    results=[]
    for z in zones:
        ez=expand_zone(z['zone'])
        zid=f"{symbol}|{TF}|{z['type']}|{z['index']}"
        results.append({"id":zid,"symbol":symbol,"tf":TF,"type":z['type'],"side":z.get("side"),"zone":ez,"detected_at":datetime.now(timezone.utc).isoformat(),"hit_at":None})
    return results

# ---------------- SCAN ----------------
def scan_all_pairs_and_store():
    logger.info("Starting full scan TF=%s", TF)
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
                    symzones.append(z)
                    new+=1
                    txt=(f"🔎 Zone detected\nSymbol: {z['symbol']}\nTF:{z['tf']}\nType:{z['type']}\nZone:{z['zone'][0]:.6f}-{z['zone'][1]:.6f}")
                    df_chart=fetch_klines(z['symbol'],z['tf'],limit=200)
                    img=build_chart_bytes(df_chart,z['symbol'],z['tf'],z['zone']) if df_chart is not None else None
                    send_telegram(txt,photo_bytes=img)
            except Exception as e:
                logger.exception("scan worker error for %s: %s",s,e)
    state['last_scan']=datetime.now(timezone.utc).isoformat()
    save_state(state)
    logger.info("Scan finished, new zones=%d",new)
    return new

# ---------------- MONITOR ----------------
def monitor_zone_hits():
    logger.info("Zone monitor thread started.")
    while True:
        try:
            if not state["watchlist"]:
                logger.info("No pairs in watchlist yet.")
                time.sleep(30)
                continue

            for symbol in state["watchlist"]:
                zones = state["zones"].get(symbol, [])
                if not zones:
                    logger.info("%s has no zones stored. Try running /scan first.", symbol)
                    continue

                df = get_klines(symbol, limit=3)
                if df is None or df.empty:
                    logger.info("%s returned no klines.", symbol)
                    continue

                last_price = df["c"].iloc[-1]
                logger.info("Checking %s price: %.4f (%d zones)", symbol, last_price, len(zones))

                hit_any = False
                for z in zones:
                    zlow, zhigh = z["low"], z["high"]
                    logger.info("Zone: %s %.4f–%.4f | price %.4f", z["type"], zlow, zhigh, last_price)

                    # HIT
                    if zlow <= last_price <= zhigh:
                        send_telegram(f"✅ Zone HIT on {symbol} ({z['type']}) "
                                      f"at {last_price:.4f}\nZone {zlow:.4f}-{zhigh:.4f}")
                        hit_any = True

                # 🧪 тест — якщо не знайдено, пробуємо штучний сигнал
                if not hit_any:
                    logger.info("No zone hit for %s. Sending test alert.", symbol)
                    send_telegram(f"🔍 Checked {symbol} — no zone hit (price={last_price:.4f})")

                time.sleep(2)

            time.sleep(15)
        except Exception as e:
            logger.exception("Monitor error: %s", e)
            time.sleep(10)

# ---------------- FLASK ----------------
app=Flask(__name__)

@app.route("/telegram_webhook/<token>",methods=["POST"])
def telegram_webhook(token):
    if token!=TELEGRAM_TOKEN:
        return jsonify({"ok":False,"error":"invalid token"}),403
    update=request.get_json(force=True)
    msg=update.get("message") or update.get("edited_message") or {}
    text=msg.get("text","").strip()
    if not text: return jsonify({"ok":True})
    parts=text.split()
    cmd=parts[0].lower()
    if cmd=="/scan":
        Thread(target=scan_all_pairs_and_store,daemon=True).start()
        send_telegram("⚡ Full scan started.")
    elif cmd=="/pair" and len(parts)>1:
        sym=parts[1].upper()
        Thread(target=scan_all_pairs_and_store,daemon=True).start()
        send_telegram(f"🔍 Analyzing {sym} on {TF}...")
    elif cmd=="/watch" and len(parts)>1:
        sym=parts[1].upper()
        if sym not in state['watchlist']:
            state['watchlist'].append(sym)
            save_state(state)
        send_telegram(f"👁️ Watching {sym}")
    elif cmd=="/unwatch" and len(parts)>1:
        sym=parts[1].upper()
        if sym in state['watchlist']:
            state['watchlist'].remove(sym)
            save_state(state)
        send_telegram(f"🚫 Unwatched {sym}")
    elif cmd=="/listzones":
        msg="\n".join([f"{z['symbol']} {z['tf']} {z['type']} {z['zone'][0]:.6f}-{z['zone'][1]:.6f}" for syms in state.get("zones",{}).values() for z in syms])
        send_telegram(f"📄 Zones:\n{msg[:4000]}")
    elif cmd=="/prebreak":
        if len(parts)>1 and parts[1].lower() in ["on","off"]:
            state['prebreak']['enabled']=(parts[1].lower()=="on")
            save_state(state)
            send_telegram(f"⚠️ Prebreak set to {state['prebreak']['enabled']}")
        elif len(parts)>2:
            try: state['prebreak']['pct']=float(parts[2])
            except: pass
            save_state(state)
            send_telegram(f"⚠️ Prebreak pct set to {state['prebreak']['pct']}")
    elif cmd=="/status":
        send_telegram(json.dumps({"watchlist":state['watchlist'],"last_scan":state.get("last_scan"),"prebreak":state.get("prebreak")},indent=2))
    elif cmd=="/help":
        send_telegram("/scan /pair SYMBOL /watch SYMBOL /unwatch SYMBOL /listzones /prebreak on/off pct /status /help")
    return jsonify({"ok":True})

# ---------------- STARTUP ----------------
if __name__=="__main__":
    logger.info("Starting SMC Telegram bot v2 (TF=%s)",TF)
    set_webhook_if_render()
    Thread(target=monitor_zone_hits,daemon=True).start()
    app.run(host="0.0.0.0",port=PORT)