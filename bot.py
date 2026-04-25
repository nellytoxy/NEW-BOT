import os
import time
import random
import requests
from datetime import datetime, timezone
import logging
logging.basicConfig(level=logging.INFO)

# ============================================================
# V5 ANTI-BLOCK RAILWAY BINANCE SCANNER BOT (SYNC STABLE)
# FIXES:
# - Binance 451 mitigation (header rotation + retry backoff)
# - Session reuse (faster + less blocking)
# - safer request layer
# - stable Railway loop (NO asyncio)
# ============================================================

BOT_TOKEN =  "8649950519:AAHb4UUejJZJuVuQjqL8nBqj69FW1k3tTmg"
BULL_CHAT_ID =  "-1003965900583"
BEAR_CHAT_ID = "-1003723283209"

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))

BINANCE_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
BINANCE_KLINES = "https://fapi.binance.com/fapi/v1/klines"
TELEGRAM_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# ============================================================
# ANTI-BLOCK SESSION + HEADERS ROTATION
# ============================================================

session = requests.Session()

HEADERS_POOL = [
    {"User-Agent": "Mozilla/5.0"},
    {"User-Agent": "Chrome/120.0"},
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
]

def get_headers():
    return random.choice(HEADERS_POOL)

# ============================================================
# SAFE HTTP (ANTI BLOCK V5)
# ============================================================

def fetch_json(url, params=None, retries=5):
    for attempt in range(retries):
        try:
            r = session.get(
                url,
                params=params,
                headers=get_headers(),
                timeout=15
            )

            if r.status_code == 200:
                return r.json()

            # Binance 451 / blocking handling
            print(f"HTTP {r.status_code} -> retrying ({attempt+1})")

        except Exception as e:
            print(f"Fetch error ({attempt+1}/{retries}): {e}")

        time.sleep(1.5 * (attempt + 1))

    return None

# ============================================================
# SYMBOLS
# ============================================================

def get_symbols():
    data = fetch_json(BINANCE_EXCHANGE_INFO)

    if not data:
        print("Failed to fetch exchange info")
        return []

    symbols = data.get("symbols", [])
    print("Total symbols:", len(symbols))

    out = []
    for s in symbols:
        try:
            if s.get("status") == "TRADING" and s.get("symbol", "").endswith("USDT"):
                out.append(s["symbol"])
        except:
            pass

    print("Filtered symbols:", len(out))
    return out

# ============================================================
# KLINES
# ============================================================

def get_klines(symbol):
    return fetch_json(
        BINANCE_KLINES,
        params={"symbol": symbol, "interval": "15m", "limit": 50}
    )

# ============================================================
# SIMPLE SIGNAL ENGINE
# ============================================================

def detect_signal(klines):
    if not klines or len(klines) < 30:
        return None

    closes = [float(x[4]) for x in klines]
    highs = [float(x[2]) for x in klines]
    lows = [float(x[3]) for x in klines]
    vols = [float(x[5]) for x in klines]

    last = closes[-1]
    prev = closes[-2]

    recent_high = max(highs[-10:])
    recent_low = min(lows[-10:])

    vol_ok = vols[-1] > sum(vols[-20:]) / 20

    if lows[-2] < recent_low and last > recent_low and last > prev and vol_ok:
        return "LONG"

    if highs[-2] > recent_high and last < recent_high and last < prev and vol_ok:
        return "SHORT"

    return None

# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(chat_id, msg):
    if not BOT_TOKEN:
        print("Missing BOT_TOKEN")
        return

    try:
        session.post(
            TELEGRAM_URL,
            json={"chat_id": chat_id, "text": msg},
            timeout=10
        )
    except Exception as e:
        print("Telegram error:", e)

# ============================================================
# MESSAGE
# ============================================================

def build_message(symbol, side):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"🚨 {side} SIGNAL\n\n"
        f"Symbol: {symbol}\n"
        f"Time: {now}\n"
    )

# ============================================================
# MAIN LOOP
# ============================================================

def scan_market():
    cache = set()

    while True:
        print("\n==============================")
        print("Starting market scan...")
        print("==============================")

        symbols = get_symbols()
        if not symbols:
            time.sleep(SCAN_INTERVAL)
            continue

        for sym in symbols[:100]:
            try:
                kl = get_klines(sym)
                sig = detect_signal(kl)

                if not sig:
                    continue

                key = sym + sig
                if key in cache:
                    continue

                cache.add(key)

                if len(cache) > 500:
                    cache.clear()

                msg = build_message(sym, sig)

                if sig == "LONG":
                    send_telegram(BULL_CHAT_ID, msg)
                    print("LONG", sym)
                else:
                    send_telegram(BEAR_CHAT_ID, msg)
                    print("SHORT", sym)

                time.sleep(0.1)  # 100ms between symbols

            except Exception as e:
                print("Error:", e)

        print("Cycle done")
        time.sleep(SCAN_INTERVAL)

# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    print("V5 Anti-Block Bot Starting...")
    scan_market()
