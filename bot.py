import os
import asyncio
import requests
from datetime import datetime

# ============================================================
# RAILWAY READY BINANCE SCANNER BOT
# - Binance API fetch fix
# - Telegram Bull/Bear channels
# - No A+ filter
# - Environment variable ready
# ============================================================

BOT_TOKEN = "8649950519:AAHb4UUejJZJuVuQjqL8nBqj69FW1k3tTmg"
BULL_CHAT_ID ="-1003965900583"
BEAR_CHAT_ID ="-1003723283209"
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))

# Use HTTP instead of HTTPS for environments where SSL support is broken
BINANCE_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
BINANCE_KLINES = "https://fapi.binance.com/fapi/v1/klines"
BINANCE_TICKER = "https://fapi.binance.com/fapi/v1/ticker/24hr"
TELEGRAM_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"


# ============================================================
# SAFE FETCH JSON (REQUESTS VERSION)
# ============================================================

def fetch_json(url, params=None, retries=3):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json"
    }

    for attempt in range(retries):
        try:
            response = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=20
            )

            if response.status_code == 200:
                return response.json()

            print(f"HTTP {response.status_code} -> {url}")

        except Exception as e:
            print(f"Fetch error ({attempt + 1}/{retries}): {e}")

        import time
        time.sleep(1)

    return None


# ============================================================
# GET SYMBOLS (FIXED)
# ============================================================

def get_symbols():
    data = fetch_json(BINANCE_EXCHANGE_INFO)

    if not data:
        print("Failed to fetch exchange info")
        return []

    symbols_raw = data.get("symbols", [])
    print(f"Total symbols from API: {len(symbols_raw)}")

    filtered = []

    for s in symbols_raw:
        try:
            symbol = s.get("symbol", "")
            status = s.get("status", "")
            contract_type = s.get("contractType", "")
            quote = s.get("quoteAsset", "")

            if (
                quote == "USDT"
                and status == "TRADING"
                and contract_type == "PERPETUAL"
                and "_" not in symbol
            ):
                filtered.append(symbol)

        except Exception:
            continue

    print(f"Filtered USDT symbols: {len(filtered)}")
    return filtered


# ============================================================
# SIMPLE SIGNAL ENGINE
# ============================================================

def get_klines(symbol, interval="15m", limit=50):
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }

    return fetch_json(BINANCE_KLINES, params=params)


def ema(values, length):
    if len(values) < length:
        return None
    return sum(values[-length:]) / length


def detect_signal(klines):
    if not klines or len(klines) < 30:
        return None

    closes = [float(k[4]) for k in klines]
    volumes = [float(k[5]) for k in klines]

    last_close = closes[-1]
    prev_close = closes[-2]

    ema_fast = ema(closes, 9)
    ema_slow = ema(closes, 21)
    avg_vol = sum(volumes[-20:]) / 20
    last_vol = volumes[-1]

    if not ema_fast or not ema_slow:
        return None

    bull_condition = (
        last_close > ema_fast
        and ema_fast > ema_slow
        and last_close > prev_close
        and last_vol > avg_vol
    )

    bear_condition = (
        last_close < ema_fast
        and ema_fast < ema_slow
        and last_close < prev_close
        and last_vol > avg_vol
    )

    if bull_condition:
        return "LONG"

    if bear_condition:
        return "SHORT"

    return None


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(chat_id, message):
    if not BOT_TOKEN or not chat_id:
        print("Telegram config missing")
        return

    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }

    try:
        response = requests.post(
            TELEGRAM_URL,
            json=payload,
            timeout=20
        )

        if response.status_code != 200:
            print("Telegram error:", response.text)

    except Exception as e:
        print("Telegram send failed:", e)


def build_message(symbol, side):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    return (
        f"🚨 *{side} SIGNAL*\n\n"
        f"Symbol: `{symbol}`\n"
        f"Setup: Momentum Break\n"
        f"Time: {now}\n\n"
        f"Railway Scanner Active ✅"
    )


# ============================================================
# MAIN SCANNER LOOP
# ============================================================

async def scan_market():
    sent_cache = set()

    while True:
        while True:
            try:
                print("\n==============================")
                print("Starting market scan...")
                print("==============================")

                symbols = get_symbols()

                if not symbols:
                    print("No symbols found. Retrying...")
                    await asyncio.sleep(SCAN_INTERVAL)
                    continue

                for symbol in symbols[:150]:
                    try:
                        klines = get_klines(symbol)
                        signal = detect_signal(klines)

                        if not signal:
                            continue

                        cache_key = f"{symbol}-{signal}"

                        if cache_key in sent_cache:
                            continue

                        sent_cache.add(cache_key)

                        if len(sent_cache) > 500:
                            sent_cache.clear()

                        message = build_message(symbol, signal)

                        if signal == "LONG":
                            send_telegram(BULL_CHAT_ID, message)
                            print(f"Bull alert sent -> {symbol}")

                        if signal == "SHORT":
                            send_telegram(BEAR_CHAT_ID, message)
                            print(f"Bear alert sent -> {symbol}")

                        await asyncio.sleep(0.2)

                    except Exception as e:
                        print(f"Scan error on {symbol}: {e}")

                print(f"Sleeping {SCAN_INTERVAL}s before next scan...")
                await asyncio.sleep(SCAN_INTERVAL)

            except Exception as e:
                print("Main loop error:", e)
                await asyncio.sleep(10)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    print("Starting Railway Binance Scanner Bot...")

    try:
        loop = asyncio.get_running_loop()
        # Already inside an event loop (not typical for Railway)
        loop.create_task(scan_market())
    except RuntimeError:
        # Normal Railway / local execution
        asyncio.run(scan_market())
