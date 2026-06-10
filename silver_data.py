#!/usr/bin/env python3
"""
Silver price + support/resistance engine (Yahoo Finance).

Pulls silver market data and derives support / resistance ZONES using swing-pivot
clustering -- the same idea used in the NSE scanner, adapted for a single
commodity across multiple timeframes.

Symbols tried in order until one returns data:
    SI=F     COMEX silver futures (front month)   <- primary, USD/oz
    SLV      iShares Silver Trust ETF             <- liquid proxy
    XAGUSD=X spot silver                          <- FX feed fallback
"""
import warnings
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import yfinance as yf

warnings.simplefilter("ignore")

SYMBOLS = ["SI=F", "SLV", "XAGUSD=X"]

# pivot detection: a swing is the local extreme within +/- SWING bars
SWING = 3
# two pivots within CLUSTER_TOL of each other belong to the same zone
CLUSTER_TOL = 0.012  # 1.2%


# ----------------------------- indicators -----------------------------
def _ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def _rsi(c, n=14):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def _atr(df, n=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


# ----------------------------- data fetch -----------------------------
def fetch(period="1y", interval="1d"):
    """Return (symbol, dataframe) for the first symbol that yields data."""
    last_err = None
    for sym in SYMBOLS:
        try:
            df = yf.download(
                sym, period=period, interval=interval,
                auto_adjust=False, progress=False, threads=False,
            )
            if df is not None and len(df) > 30:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                return sym, df.dropna()
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    raise RuntimeError(f"No silver data from {SYMBOLS}: {last_err}")


GRAMS_PER_OZ = 31.1034768
FX_SYMBOLS = ["INR=X", "USDINR=X"]


def fetch_fx():
    """Return live USD->INR rate (how many rupees per US dollar)."""
    for sym in FX_SYMBOLS:
        try:
            df = yf.download(sym, period="5d", interval="1d",
                             progress=False, threads=False)
            if df is not None and len(df):
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                return float(df["Close"].dropna().iloc[-1])
        except Exception:  # noqa: BLE001
            continue
    return 83.0  # sensible fallback if the FX feed is down


# ----------------------------- pivots / zones -----------------------------
def _pivots(df, k=SWING):
    H, L = df["High"].values, df["Low"].values
    n = len(df)
    highs, lows = [], []
    for i in range(k, n - k):
        if H[i] == H[i - k:i + k + 1].max():
            highs.append((i, float(H[i])))
        if L[i] == L[i - k:i + k + 1].min():
            lows.append((i, float(L[i])))
    return highs, lows


def _cluster(pivs, n_bars, tol=CLUSTER_TOL):
    """Group nearby pivots into zones. Each zone = {price, lo, hi, touches, recency}."""
    if not pivs:
        return []
    pts = sorted(pivs, key=lambda x: x[1])
    zones, grp = [], [pts[0]]
    for ix, p in pts[1:]:
        if (p - grp[0][1]) / grp[0][1] > tol:
            zones.append(_make_zone(grp, n_bars))
            grp = []
        grp.append((ix, p))
    zones.append(_make_zone(grp, n_bars))
    return zones


def _make_zone(grp, n_bars):
    prices = [p for _, p in grp]
    idxs = [i for i, _ in grp]
    return {
        "price": round(sum(prices) / len(prices), 3),
        "lo": round(min(prices), 3),
        "hi": round(max(prices), 3),
        "touches": len(grp),
        # recency: 1.0 if the most recent touch is the latest bar, ->0 if old
        "recency": round(max(idxs) / max(n_bars - 1, 1), 3),
    }


def _strength(zone):
    """Composite strength score: more touches + more recent = stronger."""
    return round(zone["touches"] * (0.6 + 0.4 * zone["recency"]), 2)


def support_resistance(df, price):
    """Split clustered swing zones into support (below price) and resistance (above)."""
    n = len(df)
    highs, lows = _pivots(df)
    all_piv = highs + lows
    zones = _cluster(all_piv, n)
    for z in zones:
        z["strength"] = _strength(z)
        z["dist_pct"] = round((z["price"] - price) / price * 100, 2)

    # relevance blends zone strength with proximity to price -- a strong zone
    # far away matters less to the next move than a decent zone right overhead.
    def relevance(z):
        prox = 1 / (1 + abs(z["dist_pct"]) / 8)   # ~halves every 8% away
        return z["strength"] * prox

    support = [z for z in zones if z["hi"] < price]
    resistance = [z for z in zones if z["lo"] > price]
    support = sorted(support, key=relevance, reverse=True)[:5]
    resistance = sorted(resistance, key=relevance, reverse=True)[:5]
    support.sort(key=lambda z: -z["price"])      # nearest first (just below price)
    resistance.sort(key=lambda z: z["price"])    # nearest first (just above price)
    return support, resistance


# ----------------------------- public snapshot -----------------------------
def snapshot():
    """Full silver snapshot: price, change, indicators, S/R zones, recent candles."""
    sym, daily = fetch(period="1y", interval="1d")
    c = daily["Close"]
    price = float(c.iloc[-1])
    prev = float(c.iloc[-2])
    chg = price - prev
    chg_pct = chg / prev * 100

    ema20 = float(_ema(c, 20).iloc[-1])
    ema50 = float(_ema(c, 50).iloc[-1])
    ema200 = float(_ema(c, 200).iloc[-1]) if len(c) >= 200 else None
    rsi = float(_rsi(c).iloc[-1])
    atr = float(_atr(daily).iloc[-1])

    # trend read from EMA stack
    if ema200 and ema20 > ema50 > ema200:
        trend = "uptrend"
    elif ema200 and ema20 < ema50 < ema200:
        trend = "downtrend"
    elif ema20 > ema50:
        trend = "mild up"
    elif ema20 < ema50:
        trend = "mild down"
    else:
        trend = "range"

    support, resistance = support_resistance(daily, price)

    # last ~120 daily candles for the chart
    tail = daily.tail(120)
    candles = [
        {
            "t": idx.strftime("%Y-%m-%d"),
            "o": round(float(r.Open), 3),
            "h": round(float(r.High), 3),
            "l": round(float(r.Low), 3),
            "c": round(float(r.Close), 3),
        }
        for idx, r in tail.iterrows()
    ]

    # 52-week context
    hi52 = float(daily["High"].tail(252).max())
    lo52 = float(daily["Low"].tail(252).min())

    # ---- convert USD -> INR. India quotes silver per KG, so for per-ounce feeds
    #      we go USD/oz -> INR/kg; for the SLV ETF proxy we just convert the share.
    usdinr = fetch_fx()
    if sym == "SLV":
        factor, unit = usdinr, "INR/share"
    else:
        factor, unit = usdinr * 1000 / GRAMS_PER_OZ, "INR/kg"

    def cv(x):
        return round(x * factor) if x is not None else None

    for z in support + resistance:
        z["price"], z["lo"], z["hi"] = cv(z["price"]), cv(z["lo"]), cv(z["hi"])
    for c in candles:
        c["o"], c["h"], c["l"], c["c"] = cv(c["o"]), cv(c["h"]), cv(c["l"]), cv(c["c"])

    return {
        "symbol": sym,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "price": cv(price),
        "change": cv(chg),
        "change_pct": round(chg_pct, 2),
        "currency": unit,
        "fx": {
            "usdinr": round(usdinr, 3),
            "usd_per_oz": round(price, 3),
            "inr_per_10g": cv(price) / 100 if unit == "INR/kg" else None,
        },
        "indicators": {
            "ema20": cv(ema20),
            "ema50": cv(ema50),
            "ema200": cv(ema200),
            "rsi14": round(rsi, 1),
            "atr14": cv(atr),
            "trend": trend,
            "hi52w": cv(hi52),
            "lo52w": cv(lo52),
        },
        "support": support,
        "resistance": resistance,
        "candles": candles,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(snapshot(), indent=2))
