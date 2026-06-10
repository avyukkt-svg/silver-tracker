# Silver Tracker 🪙 (INR)

A web platform that tracks **silver prices in ₹**, maps **support / resistance
zones**, pulls **every silver-relevant news story** (explained in plain English),
rolls it all into one **Buy Score /100**, and **emails / texts you** when it's a
good time to buy or sell. No API keys for news, no login. Self-updating.

## What it does

**Buy Score (0–100)** (`score_engine.py`)
- One easy number: **higher = better time to buy, lower = better time to sell**
- Blends three forces, shown as a live gauge with a breakdown + plain reasons:
  - **News** — bullish news pushes it up, bearish down (±25)
  - **Levels** — price near **support** pushes up, near **resistance** down (±25)
  - **Momentum** — oversold RSI pushes up, overbought down (±15)
- Bands: ≥75 Strong Buy · 60–74 Buy · 40–59 Hold · 25–39 Sell · <25 Strong Sell

**Alerts (email / phone)** (`alerts.py`)
- Fires when the score crosses a threshold: **≥ 75 → BUY**, **≤ 25 → SELL**
- Email over SMTP. For your **phone**, set the recipient to your carrier's free
  email-to-SMS gateway (e.g. `9876543210@vtext.com`) — no paid SMS API needed
- De-duped: only fires on a fresh signal or after a cooldown (default 180 min)
- Runs **server-side on a timer**, so alerts fire even with the page closed
- Setup: copy `alert_config.example.json` → `alert_config.json` and fill in SMTP.
  Without it, alerts are still logged to `alerts.log` and shown in the UI.

**Prices in INR** (Yahoo Finance via `yfinance`)
- Silver in **₹/kg** (India's convention) and **₹/10g**, plus the raw $/oz & USDINR
- Source: COMEX futures `SI=F` × live `USD/INR`, falling back to `SLV` / `XAGUSD=X`
- Trend (EMA 20/50/200 stack), RSI(14), ATR(14), 52-week high/low
- 120-day price chart (₹/kg) with support/resistance lines drawn on it

**Self-updating**
- Browser auto-refreshes every ~60s; a background scheduler refreshes the data
  and re-checks alerts every ~180s regardless of whether anyone is viewing

**Support / resistance zones** (`silver_data.py`)
- Detects swing pivots, clusters nearby pivots into **zones** (not just single lines)
- Each zone shows: price, number of touches, distance from price, strength bars
- Zone selection blends **strength × proximity** so the levels that matter for the
  next move surface first

**News engine** (`news_engine.py`) — free, no key
- Pulls Google News RSS across the themes that drive silver: silver/gold price,
  the Fed & interest rates, US inflation/CPI, the US dollar (DXY), industrial &
  solar demand, and geopolitical risk
- Classifies every headline:
  - **catalyst vs recap** — separates news that can *cause* the next move (Fed
    decisions, CPI/jobs surprises, war/sanctions, supply shocks, policy shifts)
    from routine "silver price today / fell X%" recaps. The feed defaults to
    **⚡ Movers only**; recaps are heavily down-ranked and hidden by default
  - **move potential** — 0–100, dominated by catalyst strength (not just sentiment)
  - **catalyst type** — Fed/Rates, Inflation data, Geopolitics, Supply/Demand, …
  - **direction** — 🟢 Good / 🔴 Bad / ⚪ Neutral *for silver*
  - **directness** — `direct` (silver/precious metals) vs `indirect` (a macro driver)
  - **plain English** — a one-line "what this means for silver" under each headline
- **Context-aware**: knows the dollar and bond yields move *inversely* to silver,
  so "dollar gains" reads bearish while "silver gains" reads bullish
- Aggregates into a single **news-driven silver bias** (−100…+100)

## Run it

```bash
cd "silver rate tracker"
pip install -r requirements.txt
python3 app.py
# open http://localhost:8000
```

It binds to `0.0.0.0:8000`, so anyone on your network can open it at
`http://<your-ip>:8000`.

## API

| Endpoint        | Returns                                            |
|-----------------|----------------------------------------------------|
| `GET /api/market` | price (INR), indicators, support/resistance, candles |
| `GET /api/news`   | classified + explained news items + aggregate bias |
| `GET /api/score`  | buy score (0–100) + alert status                   |
| `GET /api/all`    | market + news + score + alert (used by dashboard)  |

Responses are cached in-process: market 60s, news 5min.

## Tuning

- **More/different news themes** → edit `TOPICS` in `news_engine.py`
- **Classifier sensitivity** → edit the `BULLISH` / `BEARISH` phrase lexicons and
  the `UP_WORDS` / `DOWN_WORDS` / `INVERSE_SUBJECTS` lists in `news_engine.py`
- **Zone tightness** → `SWING` (pivot window) and `CLUSTER_TOL` in `silver_data.py`

## Notes
- Educational tool — **not financial advice**.
- Headline-level sentiment is a heuristic; mixed-subject headlines intentionally
  resolve to *neutral* rather than guessing.
