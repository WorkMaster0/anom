import asyncio
import json
import pandas as pd
import websockets
import logging

logger = logging.getLogger("ws-manager")

class WebSocketKlineManager:
    def __init__(self, symbols, interval="15m", batch_size=100):
        self.symbols = [s.lower() for s in symbols]
        self.interval = interval
        self.batch_size = batch_size
        self.data = {s.upper(): pd.DataFrame(columns=["open", "high", "low", "close", "volume"]) for s in symbols}

    async def _subscribe_batch(self, batch):
        streams = "/".join([f"{s}@kline_{self.interval}" for s in batch])
        url = f"wss://stream.binance.com:9443/stream?streams={streams}"
        while True:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    async for msg in ws:
                        data = json.loads(msg)
                        if "data" not in data:
                            continue
                        kline = data["data"]["k"]
                        if not kline["x"]:  # свічка не закрилася
                            continue
                        symbol = kline["s"]
                        ts = pd.to_datetime(kline["t"], unit="ms", utc=True)
                        self.data[symbol].loc[ts] = {
                            "open": float(kline["o"]),
                            "high": float(kline["h"]),
                            "low": float(kline["l"]),
                            "close": float(kline["c"]),
                            "volume": float(kline["v"])
                        }
            except Exception as e:
                logger.error(f"[WebSocket] error in batch {batch[0]}..{batch[-1]}: {e}, reconnecting in 5s...")
                await asyncio.sleep(5)

    def start(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        tasks = []
        # Розбиваємо символи на батчі по batch_size
        for i in range(0, len(self.symbols), self.batch_size):
            batch = self.symbols[i:i + self.batch_size]
            tasks.append(loop.create_task(self._subscribe_batch(batch)))
        loop.run_until_complete(asyncio.gather(*tasks))

    def get_klines(self, symbol, limit=500):
        df = self.data.get(symbol.upper())
        if df is None or len(df) < 10:
            return None
        return df.tail(limit).copy()