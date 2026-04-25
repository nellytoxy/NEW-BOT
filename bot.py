"""
SweepBot V4 — SMC Dual Engine + LTF Flow Reversal Scanner
Railway-deployable async scanner for Binance Futures (USDT perps)

Logic ported from SweepBot V4 Pine Script:
  - HTF (4H) structure: Higher High / Higher Low = BULL, Lower High / Lower Low = BEAR
  - MTF (1H) sweep: liquidity sweep detection with rejection + displacement
  - LTF (15m) entry: V2 flow reversal engine (absorption + OI trap + delta flip + engulf)
  - Entries ONLY in direction of HTF trend
  - Score model gates alerts (min score configurable)
"""

import asyncio
import aiohttp
import time
import os
import logging
from datetime import datetime, timezone
from collections import deque

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("sweepbot")

# ─────────────────────────────────────────────
# CONFIG (all from environment variables)
# ─────────────────────────────────────────────
TELEGRAM_TOKEN       = os.environ["TELEGRAM_TOKEN"]
CHAT_ID_BULL         = os.environ["CHAT_ID_BULL"]
CHAT_ID_BEAR         = os.environ["CHAT_ID_BEAR"]

BINANCE_BASE         = os.getenv("BINANCE_BASE", "https://fapi.binance.com")
SCAN_INTERVAL        = int(os.getenv("SCAN_INTERVAL", "60"))        # seconds between scans
CONCURRENCY          = int(os.getenv("CONCURRENCY", "15"))           # parallel symbol requests
ALERT_COOLDOWN       = int(os.getenv("ALERT_COOLDOWN", "7200"))      # 2h default
MIN_SCORE            = int(os.getenv("MIN_SCORE", "18"))             # min score to alert
CANDLE_CLOSE_WINDOW  = int(os.getenv("CANDLE_CLOSE_WINDOW", "180")) # seconds before 15m close
TOP_N_SYMBOLS        = int(os.getenv("TOP_N_SYMBOLS", "80"))         # scan top N by OI

# SMC thresholds
REJECTION_RATIO      = float(os.getenv("REJECTION_RATIO", "1.5"))
MIN_BODY_RATIO       = float(os.getenv("MIN_BODY_RATIO", "0.35"))
EXHAUSTION_VOL_MULT  = float(os.getenv("EXHAUSTION_VOL_MULT", "2.5"))
PIVOT_DEPTH          = int(os.getenv("PIVOT_DEPTH", "5"))            # swing detection depth

# ─────────────────────────────────────────────
# RUNTIME STATE
# ─────────────────────────────────────────────
_alerted: dict[str, int] = {}

# ─────────────────────────────────────────────
# TIME UTILS
# ─────────────────────────────────────────────
def now_ts() -> int:
    return int(time.time())

def seconds_to_close(tf_sec: int) -> int:
    return tf_sec - (now_ts() % tf_sec)

def near_candle_close(tf_sec: int = 900, window: int = CANDLE_CLOSE_WINDOW) -> bool:
    """True if we are within `window` seconds of a 15m candle close."""
    return seconds_to_close(tf_sec) <= window

def cooldown_key(symbol: str, direction: str) -> str:
    return f"{symbol}:{direction}"

def on_cooldown(key: str) -> bool:
    return now_ts() - _alerted.get(key, 0) < ALERT_COOLDOWN

def mark_alert(key: str):
    _alerted[key] = now_ts()

def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

# ─────────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────────
async def fetch_json(session: aiohttp.ClientSession, url: str, retries: int = 3):
    for attempt in range(retries):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    return await r.json()
                await asyncio.sleep(0.5 * (attempt + 1))
        except Exception as e:
            if attempt == retries - 1:
                log.debug(f"fetch failed {url}: {e}")
            await asyncio.sleep(0.5 * (attempt + 1))
    return None

# ─────────────────────────────────────────────
# BINANCE DATA
# ─────────────────────────────────────────────
async def get_usdt_symbols(session: aiohttp.ClientSession) -> list[str]:
    data = await fetch_json(session, f"{BINANCE_BASE}/fapi/v1/exchangeInfo")
    if not data:
        return []
    return [
        s["symbol"] for s in data.get("symbols", [])
        if s["status"] == "TRADING" and s["symbol"].endswith("USDT")
    ]

async def get_top_symbols_by_oi(session: aiohttp.ClientSession, symbols: list[str], n: int) -> list[str]:
    """Return top N symbols ranked by open interest (proxy for liquidity)."""
    tasks = [
        fetch_json(session, f"{BINANCE_BASE}/fapi/v1/openInterest?symbol={sym}")
        for sym in symbols
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    ranked = []
    for sym, res in zip(symbols, results):
        if isinstance(res, dict) and "openInterest" in res:
            try:
                ranked.append((sym, float(res["openInterest"])))
            except (ValueError, TypeError):
                pass
    ranked.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in ranked[:n]]

async def get_klines(session: aiohttp.ClientSession, symbol: str, interval: str, limit: int = 60) -> list | None:
    url = f"{BINANCE_BASE}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    return await fetch_json(session, url)

async def get_oi_history(session: aiohttp.ClientSession, symbol: str, limit: int = 10) -> list[float] | None:
    """Fetch OI history from /futures/data/openInterestHist (5m buckets)."""
    url = (
        f"{BINANCE_BASE}/futures/data/openInterestHist"
        f"?symbol={symbol}&period=5m&limit={limit}"
    )
    data = await fetch_json(session, url)
    if not data or not isinstance(data, list):
        return None
    try:
        return [float(d["sumOpenInterest"]) for d in data]
    except (KeyError, TypeError, ValueError):
        return None

# ─────────────────────────────────────────────
# CANDLE HELPERS
# ─────────────────────────────────────────────
def o(k): return float(k[1])
def h(k): return float(k[2])
def l(k): return float(k[3])
def c(k): return float(k[4])
def v(k): return float(k[5])

def candle_delta(k) -> float:
    """Approximate delta: volume weighted by candle direction."""
    rng = max(h(k) - l(k), 1e-10)
    return v(k) * (c(k) - o(k)) / rng

def body_ratio(k) -> float:
    rng = max(h(k) - l(k), 1e-10)
    return abs(c(k) - o(k)) / rng

def upper_wick(k) -> float:
    top  = max(o(k), c(k))
    return h(k) - top

def lower_wick(k) -> float:
    bot  = min(o(k), c(k))
    return bot - l(k)

def vol_ma(klines: list, period: int = 20) -> float:
    vols = [v(k) for k in klines[-period - 1:-1]]
    return sum(vols) / len(vols) if vols else 1.0

# ─────────────────────────────────────────────
# PIVOT / SWING DETECTION
# ─────────────────────────────────────────────
def find_pivots(klines: list, depth: int = PIVOT_DEPTH) -> list[dict]:
    """
    Returns list of {'type': 'high'|'low', 'price': float, 'idx': int}
    sorted oldest→newest, alternating high/low.
    """
    highs, lows = [], []
    n = len(klines)
    for i in range(depth, n - depth):
        # pivot high: highest in window
        if h(klines[i]) == max(h(klines[j]) for j in range(i - depth, i + depth + 1)):
            highs.append({"type": "high", "price": h(klines[i]), "idx": i})
        # pivot low: lowest in window
        if l(klines[i]) == min(l(klines[j]) for j in range(i - depth, i + depth + 1)):
            lows.append({"type": "low",  "price": l(klines[i]), "idx": i})

    # Merge and sort by idx, then deduplicate consecutive same types
    merged = sorted(highs + lows, key=lambda x: x["idx"])
    deduped = []
    for p in merged:
        if deduped and deduped[-1]["type"] == p["type"]:
            # keep the more extreme one
            if p["type"] == "high" and p["price"] > deduped[-1]["price"]:
                deduped[-1] = p
            elif p["type"] == "low" and p["price"] < deduped[-1]["price"]:
                deduped[-1] = p
        else:
            deduped.append(p)
    return deduped

# ─────────────────────────────────────────────
# HTF STRUCTURE  (ported from V4 Pine)
# ─────────────────────────────────────────────
def detect_htf_structure(klines_4h: list) -> dict:
    """
    Returns:
      bias     : 'BULL' | 'BEAR' | 'CHOP'
      bos      : bool
      struct_h : float  (recent swing high)
      struct_l : float  (recent swing low)
    """
    pivots = find_pivots(klines_4h, depth=PIVOT_DEPTH)
    if len(pivots) < 4:
        return {"bias": "CHOP", "bos": False, "struct_h": 0.0, "struct_l": 0.0}

    p = pivots[-4:]  # last 4 alternating swings
    t = [x["type"] for x in p]
    v4 = [x["price"] for x in p]

    bias      = "CHOP"
    bos       = False
    struct_h  = 0.0
    struct_l  = 0.0

    # Bullish: low, high, low, high — each higher than previous same type
    if t == ["low", "high", "low", "high"]:
        if v4[3] > v4[1] and v4[2] > v4[0]:     # HH + HL
            bias     = "BULL"
            bos      = True
            struct_h = v4[3]
            struct_l = v4[2]

    # Bearish: high, low, high, low — each lower
    elif t == ["high", "low", "high", "low"]:
        if v4[3] < v4[1] and v4[2] < v4[0]:     # LL + LH
            bias     = "BEAR"
            bos      = True
            struct_h = v4[2]
            struct_l = v4[3]

    return {"bias": bias, "bos": bos, "struct_h": struct_h, "struct_l": struct_l}

# ─────────────────────────────────────────────
# MTF SWEEP DETECTION  (ported from V4 Pine)
# ─────────────────────────────────────────────
def detect_mtf_sweep(klines_1h: list) -> dict:
    """
    Detects liquidity sweep of the most recent swing high or low on 1H.
    Returns: direction 'BULL'|'BEAR'|None, sweep_price float, quality str
    """
    pivots = find_pivots(klines_1h, depth=PIVOT_DEPTH)
    if not pivots:
        return {"direction": None, "sweep_price": 0.0, "quality": "NONE"}

    last_pivot = pivots[-1]
    candle     = klines_1h[-2]  # last closed candle

    body   = abs(c(candle) - o(candle))
    rng    = max(h(candle) - l(candle), 1e-10)
    lw     = (min(o(candle), c(candle)) - l(candle))
    uw     = (h(candle) - max(o(candle), c(candle)))
    br     = body / rng

    bull_rej  = lw > uw * REJECTION_RATIO and br > MIN_BODY_RATIO and c(candle) > o(candle)
    bear_rej  = uw > lw * REJECTION_RATIO and br > MIN_BODY_RATIO and c(candle) < o(candle)

    if last_pivot["type"] == "low":
        if l(candle) < last_pivot["price"] and c(candle) > last_pivot["price"] and bull_rej:
            return {"direction": "BULL", "sweep_price": last_pivot["price"], "quality": "STRONG"}

    if last_pivot["type"] == "high":
        if h(candle) > last_pivot["price"] and c(candle) < last_pivot["price"] and bear_rej:
            return {"direction": "BEAR", "sweep_price": last_pivot["price"], "quality": "STRONG"}

    return {"direction": None, "sweep_price": 0.0, "quality": "NONE"}

# ─────────────────────────────────────────────
# FIBONACCI CHECK
# ─────────────────────────────────────────────
def check_fib_pullback(price: float, struct_h: float, struct_l: float, direction: str) -> dict:
    if struct_h <= 0 or struct_l <= 0 or struct_h == struct_l:
        return {"valid": False, "depth": 0.0, "quality": "—"}

    rng = struct_h - struct_l
    if direction == "BULL":
        depth = (struct_h - price) / rng
    else:
        depth = (price - struct_l) / rng

    valid   = 0.50 <= depth <= 0.886
    quality = "DEEP"   if depth >= 0.706 else \
              "GOOD"   if depth >= 0.618 else \
              "DECENT" if depth >= 0.50  else "—"

    return {"valid": valid, "depth": round(depth * 100, 1), "quality": quality}

# ─────────────────────────────────────────────
# FVG CHECK
# ─────────────────────────────────────────────
def check_fvg(klines: list, direction: str) -> bool:
    if len(klines) < 3:
        return False
    k0, k1, k2 = klines[-3], klines[-2], klines[-1]
    if direction == "BULL":
        return l(k2) > h(k0)   # gap up
    else:
        return h(k2) < l(k0)   # gap down

# ─────────────────────────────────────────────
# LTF FLOW REVERSAL ENGINE  (V2 + V3 hybrid)
# ─────────────────────────────────────────────
def detect_ltf_reversal(klines_15m: list, oi_history: list[float], direction: str) -> dict:
    """
    Returns dict with all LTF flow signals. `direction` is from MTF sweep.
    """
    k     = klines_15m
    vm    = vol_ma(k, 20)
    last  = k[-2]   # last CLOSED candle
    prev  = k[-3]

    v_ratio  = v(last) / vm if vm > 0 else 0
    vol_spike = v_ratio > EXHAUSTION_VOL_MULT
    vol_climax = v_ratio > EXHAUSTION_VOL_MULT * 1.5

    delta_curr = candle_delta(last)
    delta_prev = candle_delta(prev)

    delta_flip_bull = delta_curr > 0 and delta_prev < 0
    delta_flip_bear = delta_curr < 0 and delta_prev > 0
    delta_flip      = delta_flip_bull or delta_flip_bear

    # Absorption: price swept the prev low/high but CLOSED back inside it.
    # Delta direction on the absorption candle itself doesn't matter — what matters
    # is the close recovering back above (bull) or below (bear) the swept level.
    # We look at the PREVIOUS candle's delta to confirm selling/buying pressure was
    # present into the sweep, and the current candle closes back through it.
    bull_absorption = (l(last) < l(prev) and c(last) > l(prev))   # swept low, closed back above
    bear_absorption = (h(last) > h(prev) and c(last) < h(prev))   # swept high, closed back below

    # Engulfing candle
    bull_engulf = (c(last) > o(last) and c(last) > o(prev) and
                   l(last) < l(prev) and v(last) > vm)
    bear_engulf = (c(last) < o(last) and c(last) < o(prev) and
                   h(last) > h(prev) and v(last) > vm)

    # ── OI ENGINE ────────────────────────────────────────────────────────
    # Use last 3 OI buckets:
    #   oi_building = OI increased in the most recent bucket (new positions opening)
    #   oi_unwind   = OI decreased in the most recent bucket (positions closing)
    # These can occur across DIFFERENT candles — building happens INTO the sweep,
    # unwind happens AFTER the reversal. We track both independently.
    oi_building   = False
    oi_unwind     = False
    oi_change_pct = 0.0

    if oi_history and len(oi_history) >= 2:
        oi_prev_val   = oi_history[-2]
        oi_curr_val   = oi_history[-1]
        if oi_prev_val > 0:
            oi_change_pct = (oi_curr_val - oi_prev_val) / oi_prev_val * 100
        oi_building = oi_change_pct > 0
        oi_unwind   = oi_change_pct < 0

    # Look back further for OI build-up into the sweep (within last 4 buckets)
    # This catches the case where OI built 1–2 buckets ago and is now unwinding
    oi_built_recently = False
    if oi_history and len(oi_history) >= 4:
        recent_oi = oi_history[-4:]
        oi_built_recently = recent_oi[-1] > recent_oi[0]  # net OI increase over last 4 buckets

    # OI trap: OI was building INTO the sweep level (trapped shorts/longs)
    # Bull trap: OI increased + price is closing above the swept low → shorts are trapped
    bull_trap = (oi_building or oi_built_recently) and c(last) > l(prev)
    # Bear trap: OI increased + price is closing below the swept high → longs are trapped
    bear_trap = (oi_building or oi_built_recently) and c(last) < h(prev)

    # Short cover: price rising + OI unwind = trapped shorts being forced to buy back
    short_cover = c(last) > c(prev) and oi_unwind
    # Long cover:  price falling + OI unwind = trapped longs being forced to sell
    long_cover  = c(last) < c(prev) and oi_unwind

    # ── V2 RAW REVERSAL ───────────────────────────────────────────────────
    # Core: vol spike + absorption + OI trap + delta flip in direction
    ltf_bull_rev = (direction == "BULL" and vol_spike and bull_absorption and bull_trap and delta_flip_bull)
    ltf_bear_rev = (direction == "BEAR" and vol_spike and bear_absorption and bear_trap and delta_flip_bear)

    # ── V2 PREMIUM ────────────────────────────────────────────────────────
    # Premium adds short/long cover on TOP of the raw reversal.
    # NOTE: short_cover (OI unwind) and bull_trap (OI building) measure DIFFERENT
    # time windows, so they are NOT mutually exclusive — OI can build into sweep
    # then start unwinding on the reversal candle.
    ltf_bull_premium = ltf_bull_rev and short_cover
    ltf_bear_premium = ltf_bear_rev and long_cover

    # ── CONFIRMATION ──────────────────────────────────────────────────────
    # bull_confirmed fires on EITHER:
    #   (a) full premium: raw reversal + short cover + engulf, OR
    #   (b) raw reversal alone + engulf (OI trap without confirmed cover yet)
    # This ensures you still get alerted when the trap forms even if cover hasn't
    # shown up in the 5-min OI bucket yet.
    bull_confirmed = (ltf_bull_rev and bull_engulf)        # trap formed + engulf = alert
    bear_confirmed = (ltf_bear_rev and bear_engulf)        # trap formed + engulf = alert

    return {
        "v_ratio":          round(v_ratio, 2),
        "vol_spike":        vol_spike,
        "vol_climax":       vol_climax,
        "delta_curr":       round(delta_curr, 4),
        "delta_flip":       delta_flip,
        "delta_flip_bull":  delta_flip_bull,
        "delta_flip_bear":  delta_flip_bear,
        "bull_absorption":  bull_absorption,
        "bear_absorption":  bear_absorption,
        "bull_engulf":      bull_engulf,
        "bear_engulf":      bear_engulf,
        "oi_change_pct":    round(oi_change_pct, 3),
        "oi_building":      oi_building,
        "oi_built_recently":oi_built_recently,
        "oi_unwind":        oi_unwind,
        "short_cover":      short_cover,
        "long_cover":       long_cover,
        "bull_trap":        bull_trap,
        "bear_trap":        bear_trap,
        "bull_confirmed":   bull_confirmed,
        "bear_confirmed":   bear_confirmed,
        "ltf_bull_premium": ltf_bull_premium,
        "ltf_bear_premium": ltf_bear_premium,
    }

# ─────────────────────────────────────────────
# CONVICTION SCORE  (mirrors V4 Pine scoring)
# ─────────────────────────────────────────────
def score_signal(htf: dict, sweep: dict, fib: dict, fvg: bool, ltf: dict) -> dict:
    """
    Engine 1 — SMC structure (max 12)
    Engine 2 — LTF Flow (max 11)
    """
    e1 = 0
    if htf["bias"] in ("BULL", "BEAR"):  e1 += 3
    if htf["bos"]:                        e1 += 2
    if sweep["direction"]:                e1 += 2
    if fib["valid"]:                      e1 += 1
    if fib["quality"] in ("DEEP","GOOD"): e1 += 1
    if fvg:                               e1 += 2
    if ltf["delta_flip"]:                 e1 += 1   # delta flip at structure

    e2 = 0
    if ltf["vol_climax"]:                 e2 += 2
    elif ltf["vol_spike"]:                e2 += 1
    if ltf["delta_flip"]:                 e2 += 2
    if ltf["oi_building"] and sweep["direction"]: e2 += 2
    if ltf["bull_confirmed"] or ltf["bear_confirmed"]: e2 += 2
    if ltf["bull_absorption"] or ltf["bear_absorption"]: e2 += 1
    if ltf["ltf_bull_premium"] or ltf["ltf_bear_premium"]: e2 += 1

    total = e1 + e2
    grade = (
        "A+ PREMIUM" if total >= 18 else
        "A+ GRADE"   if total >= 15 else
        "A GRADE"    if total >= 12 else
        "B GRADE"    if total >= 8  else
        "C GRADE"
    )
    return {"e1": e1, "e2": e2, "total": total, "grade": grade}

# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
async def send_telegram(session: aiohttp.ClientSession, msg: str, direction: str):
    chat_id = CHAT_ID_BULL if direction == "BULL" else CHAT_ID_BEAR
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                log.warning(f"Telegram non-200: {r.status}")
    except Exception as e:
        log.error(f"Telegram send error: {e}")

def build_message(symbol: str, direction: str, price: float,
                  htf: dict, sweep: dict, fib: dict, fvg: bool,
                  ltf: dict, score: dict) -> str:
    arrow = "🟢" if direction == "BULL" else "🔴"
    entry = "LONG ▲" if direction == "BULL" else "SHORT ▼"

    vol_tag  = "⚡ CLIMAX"    if ltf["vol_climax"] else \
               "🔥 SPIKE"     if ltf["vol_spike"]  else "normal"

    # OI trap status — distinguish between current-candle build vs recent build
    if ltf["oi_building"]:
        oi_tag = "🪤 BUILDING (trap active)"
    elif ltf["oi_built_recently"]:
        oi_tag = "🪤 BUILT RECENTLY (trap set)"
    elif ltf["oi_unwind"]:
        oi_tag = "📉 UNWINDING (cover)"
    else:
        oi_tag = "FLAT"

    trap_tag = "🪤 BULL TRAP ✅" if ltf["bull_trap"] else \
               "🪤 BEAR TRAP ✅" if ltf["bear_trap"] else "—"

    cover_tag = "SHORT COVER ✅" if ltf["short_cover"] else \
                "LONG COVER ✅"  if ltf["long_cover"]  else "—"

    premium_tag = "⭐ V2 BULL PREMIUM" if ltf["ltf_bull_premium"] else \
                  "⭐ V2 BEAR PREMIUM" if ltf["ltf_bear_premium"]  else "base signal"

    return f"""{arrow} <b>{score['grade']} — {entry} {symbol}</b>

💰 Price        : <code>{price:.4f}</code>
📊 Score        : <b>{score['total']}</b>  [SMC: {score['e1']} | FLOW: {score['e2']}]

🏗 HTF Structure : <b>{htf['bias']}</b>  BOS: {'✅' if htf['bos'] else '—'}
🧹 MTF Sweep     : <b>{sweep['direction']}</b>  @ {sweep['sweep_price']:.4f}  ({sweep['quality']})
📐 Fib Depth     : {fib['depth']}%  {fib['quality']}  {'✅' if fib['valid'] else '—'}
📦 FVG           : {'✅ YES' if fvg else '—'}

🔄 Delta Flip    : {'✅' if ltf['delta_flip'] else '—'}  ({ltf['delta_curr']:+.4f})
📊 Volume        : {ltf['v_ratio']:.2f}x  {vol_tag}
📊 OI Change     : {ltf['oi_change_pct']:+.3f}%  {oi_tag}
{trap_tag}
🔁 Cover         : {cover_tag}
🪤 Absorption    : {'🟢 BULL' if ltf['bull_absorption'] else '🔴 BEAR' if ltf['bear_absorption'] else '—'}
🎯 Entry Type    : {premium_tag}

⏰ {utc_now()}"""

# ─────────────────────────────────────────────
# CORE ANALYSIS
# ─────────────────────────────────────────────
async def analyze_symbol(session: aiohttp.ClientSession, symbol: str) -> dict | None:
    # Fetch all timeframes in parallel
    k15_task = get_klines(session, symbol, "15m", 60)
    k1h_task = get_klines(session, symbol, "1h",  60)
    k4h_task = get_klines(session, symbol, "4h",  60)
    oi_task  = get_oi_history(session, symbol, limit=10)

    k15, k1h, k4h, oi_hist = await asyncio.gather(k15_task, k1h_task, k4h_task, oi_task)

    if not k15 or not k1h or not k4h or len(k15) < 20:
        return None

    # --- HTF structure (4H) ---
    htf = detect_htf_structure(k4h)
    if htf["bias"] == "CHOP":
        return None   # no bias → skip

    # --- MTF sweep (1H) ---
    sweep = detect_mtf_sweep(k1h)
    if not sweep["direction"]:
        return None   # no sweep → skip

    # --- Trend alignment: sweep must match HTF bias ---
    if sweep["direction"] != htf["bias"]:
        return None

    direction   = sweep["direction"]
    price       = c(k15[-2])  # last closed candle close

    # --- Fib pullback ---
    fib = check_fib_pullback(price, htf["struct_h"], htf["struct_l"], direction)

    # --- FVG (15m) ---
    fvg = check_fvg(k15, direction)

    # --- LTF reversal engine ---
    ltf = detect_ltf_reversal(k15, oi_hist or [], direction)

    # --- Score ---
    score = score_signal(htf, sweep, fib, fvg, ltf)

    # --- Gate: must have confirmed LTF reversal in trend direction ---
    if direction == "BULL" and not ltf["bull_confirmed"]:
        return None
    if direction == "BEAR" and not ltf["bear_confirmed"]:
        return None

    return {
        "symbol":    symbol,
        "direction": direction,
        "price":     price,
        "htf":       htf,
        "sweep":     sweep,
        "fib":       fib,
        "fvg":       fvg,
        "ltf":       ltf,
        "score":     score,
    }

# ─────────────────────────────────────────────
# SCAN
# ─────────────────────────────────────────────
async def scan_symbol(session: aiohttp.ClientSession, symbol: str, sem: asyncio.Semaphore):
    async with sem:
        try:
            result = await analyze_symbol(session, symbol)
            if not result:
                return

            score = result["score"]
            if score["total"] < MIN_SCORE:
                return

            direction = result["direction"]
            key       = cooldown_key(symbol, direction)

            if on_cooldown(key):
                return

            msg = build_message(
                symbol    = symbol,
                direction = direction,
                price     = result["price"],
                htf       = result["htf"],
                sweep     = result["sweep"],
                fib       = result["fib"],
                fvg       = result["fvg"],
                ltf       = result["ltf"],
                score     = score,
            )

            await send_telegram(session, msg, direction)
            mark_alert(key)

            log.info(f"SIGNAL {direction} {symbol} | Score {score['total']} | {score['grade']}")

        except Exception as e:
            log.error(f"Error scanning {symbol}: {e}", exc_info=True)

# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────
async def main():
    log.info("=" * 60)
    log.info("SweepBot V4 — SMC Dual Engine + LTF Flow")
    log.info(f"Min score: {MIN_SCORE} | Top N symbols: {TOP_N_SYMBOLS}")
    log.info(f"Cooldown: {ALERT_COOLDOWN}s | Concurrency: {CONCURRENCY}")
    log.info("=" * 60)

    connector = aiohttp.TCPConnector(limit=100, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:

        # Get all symbols once
        log.info("Fetching symbol list...")
        all_symbols = await get_usdt_symbols(session)
        log.info(f"Found {len(all_symbols)} USDT perp symbols")

        # Rank by OI and keep top N
        log.info(f"Ranking top {TOP_N_SYMBOLS} by OI...")
        symbols = await get_top_symbols_by_oi(session, all_symbols, TOP_N_SYMBOLS)
        log.info(f"Scanning: {', '.join(symbols[:10])} ...")

        scan_count = 0
        while True:
            scan_count += 1
            ts_start = time.perf_counter()

            if not near_candle_close(900, CANDLE_CLOSE_WINDOW):
                secs_left = seconds_to_close(900)
                log.info(f"Scan #{scan_count} | Next 15m close in {secs_left}s — sleeping {SCAN_INTERVAL}s")
                await asyncio.sleep(SCAN_INTERVAL)
                continue

            log.info(f"Scan #{scan_count} | {len(symbols)} symbols | Near 15m close — scanning now")

            sem   = asyncio.Semaphore(CONCURRENCY)
            tasks = [scan_symbol(session, sym, sem) for sym in symbols]
            await asyncio.gather(*tasks)

            elapsed = time.perf_counter() - ts_start
            log.info(f"Scan #{scan_count} complete in {elapsed:.1f}s")

            # Refresh symbol list every 50 scans (to catch new listings)
            if scan_count % 50 == 0:
                log.info("Refreshing symbol list...")
                all_symbols = await get_usdt_symbols(session)
                symbols     = await get_top_symbols_by_oi(session, all_symbols, TOP_N_SYMBOLS)

            await asyncio.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
