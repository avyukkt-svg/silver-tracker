#!/usr/bin/env python3
"""
Silver Tracker -- web platform (INR).

Combines:
  * live silver price & support/resistance zones, in INR   (silver_data.py)
  * ranked, plain-English news feed + impact scoring        (news_engine.py)
  * a single Buy Score (0-100)                              (score_engine.py)
  * email / phone alerts when the score crosses a threshold (alerts.py)

A background scheduler refreshes the data and checks alerts on a timer, so the
platform keeps working (and alerting) even with no browser open.

Run:
    pip install -r requirements.txt
    python3 app.py
    open http://localhost:8000
"""
import os
import threading
import time
import traceback

from flask import Flask, jsonify, render_template

import alerts
import llm_analyzer
import news_engine
import score_engine
import silver_data

app = Flask(__name__)

_CACHE = {
    "market": None, "market_ts": 0,
    "news": None, "news_ts": 0,
    "score": None, "alert": None, "updated": None,
}
_MARKET_TTL = 60         # seconds  (price is cheap -> refresh often)
_NEWS_TTL = 300          # seconds  (collect is parallel ~2s; Gemini enrich is non-blocking)
_REFRESH_EVERY = 180     # background scheduler period (seconds)
_lock = threading.Lock()
_alert_lock = threading.Lock()

# placeholder until the scheduler runs its first alert check
_NO_ALERT = {"signal": None, "fired": False, "detail": "starting up",
             "smtp_configured": False, "buy_threshold": 75,
             "sell_threshold": 25, "enabled": True, "checked_at": None}


def _market(force=False):
    now = time.time()
    if not force and _CACHE["market"] and now - _CACHE["market_ts"] < _MARKET_TTL:
        return _CACHE["market"]
    with _lock:
        if not force and _CACHE["market"] and time.time() - _CACHE["market_ts"] < _MARKET_TTL:
            return _CACHE["market"]
        data = silver_data.snapshot()
        _CACHE["market"], _CACHE["market_ts"] = data, time.time()
        return data


def _news(force=False):
    now = time.time()
    if not force and _CACHE["news"] and now - _CACHE["news_ts"] < _NEWS_TTL:
        return _CACHE["news"]
    with _lock:
        if not force and _CACHE["news"] and time.time() - _CACHE["news_ts"] < _NEWS_TTL:
            return _CACHE["news"]
        items = news_engine.collect()
        items, llm_meta = llm_analyzer.enrich(items)   # Gemini re-judges top items
        payload = {"summary": news_engine.summarize(items),
                   "items": items, "llm": llm_meta}
        _CACHE["news"], _CACHE["news_ts"] = payload, time.time()
        return payload


def refresh_all(force=False, run_alerts=False):
    """Recompute market, news and score.

    Only the background scheduler passes run_alerts=True, so alerts fire from a
    single thread (under a lock) -- API requests never trigger a send, which
    avoids duplicate emails from concurrent calls racing on the alert state.
    """
    market = _market(force)
    news = _news()           # never forced -> respects the 30-min TTL (saves AI quota)
    score = score_engine.compute(market, news)
    _CACHE["score"] = score
    if run_alerts:
        with _alert_lock:
            _CACHE["alert"] = alerts.check(score, market)
    _CACHE["updated"] = time.time()
    return {"market": market, "news": news, "score": score,
            "alert": _CACHE["alert"] or _NO_ALERT}


def _scheduler():
    """Background loop: keeps data fresh and fires alerts without a browser open."""
    while True:
        try:
            refresh_all(force=True, run_alerts=True)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        time.sleep(_REFRESH_EVERY)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/all")
def api_all():
    try:
        return jsonify(refresh_all())
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"error": str(e)}), 502


@app.route("/api/market")
def api_market():
    try:
        return jsonify(_market())
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 502


@app.route("/api/news")
def api_news():
    try:
        return jsonify(_news())
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 502


@app.route("/api/score")
def api_score():
    try:
        bundle = refresh_all()
        return jsonify({"score": bundle["score"], "alert": bundle["alert"]})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 502


if __name__ == "__main__":
    threading.Thread(target=_scheduler, daemon=True).start()
    # cloud hosts (Render/Railway/Fly) inject the port via $PORT; default 8000 locally
    port = int(os.environ.get("PORT", 8000))
    print(f"Silver Tracker -> http://localhost:{port}  (Ctrl+C to stop)")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
