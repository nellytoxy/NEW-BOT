FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

RUN mkdir -p /app/data

ENV EXCHANGE=binance \
    TESTNET=true \
    SYMBOL=BTC/USDT:USDT \
    HTF=4h \
    MTF=1h \
    LTF=5m \
    PIVOT_LEN=5 \
    VOL_MULT=1.8 \
    MIN_RR=2.0 \
    SCORE_THRESHOLD=15 \
    RISK_PCT=1.0 \
    LEVERAGE=5 \
    MAX_OPEN_TRADES=1 \
    POLL_INTERVAL_SEC=15 \
    LOG_LEVEL=INFO \
    STATE_FILE=/app/data/bot_state.json

CMD ["python", "-u", "bot.py"]