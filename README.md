# SweepBot V4 — Railway Deployment Guide

SMC Dual Engine + LTF Flow Reversal Scanner for Binance Futures (USDT perps).

Fires Telegram alerts only when ALL of these align:
1. **HTF (4H) structure** is clearly bullish or bearish (HH/HL or LL/LH)
2. **MTF (1H) sweep** detects liquidity grab with rejection + displacement
3. **Sweep direction matches HTF bias** (trend-aligned entries only)
4. **LTF (15m) reversal engine** confirms: exhaustion volume + delta flip + OI trap + absorption + engulfing candle
5. **Conviction score ≥ MIN_SCORE** (default 18/23)

---

## Quick Deploy to Railway

### 1. Push to GitHub
```bash
git init
git add .
git commit -m "SweepBot V4"
git remote add origin https://github.com/YOUR_USER/sweepbot-v4.git
git push -u origin main
```

### 2. Create Railway project
1. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
2. Select your repo
3. Railway auto-detects Python via `nixpacks.toml` — no extra setup needed

### 3. Set environment variables
In Railway → your service → **Variables** tab, add:

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_TOKEN` | ✅ | Your bot token from @BotFather |
| `CHAT_ID_BULL` | ✅ | Telegram group/channel ID for long signals |
| `CHAT_ID_BEAR` | ✅ | Telegram group/channel ID for short signals |
| `MIN_SCORE` | optional | Min score to fire alert (default: 18) |
| `TOP_N_SYMBOLS` | optional | How many symbols to scan (default: 80) |
| `ALERT_COOLDOWN` | optional | Re-alert window in seconds (default: 7200 = 2h) |
| `CONCURRENCY` | optional | Parallel requests (default: 15) |
| `CANDLE_CLOSE_WINDOW` | optional | Seconds before 15m close to scan (default: 180) |

See `.env.example` for all variables.

### 4. Deploy
Railway automatically deploys on every push to `main`. The bot starts logging immediately.

---

## Telegram Setup

### Get your bot token
1. Open Telegram → message `@BotFather`
2. `/newbot` → follow prompts → copy the token

### Get chat IDs
1. Add your bot to the group/channel
2. Send a message in the group
3. Visit: `https://api.telegram.org/bot<TOKEN>/getUpdates`
4. Find `"chat":{"id":...}` — that's your chat ID (negative number for groups)

---

## Signal Logic (from SweepBot V4 Pine Script)

```
HTF (4H) → detect bullish/bearish structure via pivot swings
MTF (1H) → detect liquidity sweep with rejection candle
              ↓ must align with HTF bias
LTF (15m) → V2 Flow Reversal Engine fires entry:
              • Exhaustion volume spike
              • Delta flip (seller→buyer or buyer→seller)
              • OI trap (open interest building into sweep)
              • Absorption candle (low below prev low, closes back above)
              • Engulfing candle confirmation
              • Short/long cover via OI unwind (V2 premium)
```

### Score breakdown (max 23)
| Component | Points |
|---|---|
| HTF structure detected | 3 |
| HTF break of structure | 2 |
| MTF sweep detected | 2 |
| Fibonacci depth 50-88.6% | 1 |
| Fib quality DEEP/GOOD | 1 |
| FVG present | 2 |
| Delta flip | 1 + 2 |
| Volume spike | 1, climax = 2 |
| OI building + sweep | 2 |
| LTF reversal confirmed | 2 |
| Absorption detected | 1 |
| LTF V2 premium entry | 1 |

### Alert grades
| Score | Grade |
|---|---|
| 18+ | A+ PREMIUM |
| 15+ | A+ GRADE |
| 12+ | A GRADE |
| 8+ | B GRADE |
| <8 | C GRADE |

---

## Logs
View real-time logs in Railway → your service → **Logs** tab.

```
2025-01-01 10:14:00 | INFO | Scan #47 | Near 15m close — scanning now
2025-01-01 10:14:07 | INFO | SIGNAL BULL BTCUSDT | Score 20 | A+ PREMIUM
2025-01-01 10:14:07 | INFO | SIGNAL BEAR ETHUSDT | Score 18 | A+ PREMIUM
2025-01-01 10:14:09 | INFO | Scan #47 complete in 9.2s
```

---

## Troubleshooting

**Bot starts but no alerts:**
- Temporarily lower `MIN_SCORE` to `8` to verify signals are flowing
- Check Telegram token and chat IDs are correct
- Ensure bot is admin in the group/channel

**Railway build fails:**
- Confirm `requirements.txt` and `nixpacks.toml` are committed
- Check Railway build logs for pip errors

**Rate limit errors from Binance:**
- Lower `CONCURRENCY` to `8`
- Lower `TOP_N_SYMBOLS` to `50`
