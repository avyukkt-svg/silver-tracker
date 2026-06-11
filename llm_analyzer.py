#!/usr/bin/env python3
"""
LLM news analyzer (Google Gemini).

Upgrades the keyword classifier: Gemini reads each headline and judges its impact
on the INR silver price, returning a verdict, a 0-100 impact, whether it's a true
market-moving catalyst, and a plain-English reason. Falls back silently to the
keyword classification if Gemini is unconfigured or errors.

NO heavy SDK — talks to the Gemini REST API over urllib with a certifi SSL context
(macOS framework Python lacks CA certs otherwise). Config in gemini_config.json.
"""
import hashlib
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:  # noqa: BLE001
    _SSL_CTX = ssl.create_default_context()

_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_DIR, "gemini_config.json")

# tiny cache so repeated scheduler ticks don't re-call Gemini for the same news
_CACHE = {"key": None, "result": None}

_SYSTEM = (
    "You are a precious-metals analyst. For each news headline decide its likely "
    "effect on the SILVER price in India (priced in INR, = global silver in USD x "
    "the USD/INR rate). Remember the drivers: a weaker US dollar, lower interest "
    "rates, higher inflation, war / geopolitical risk, strong industrial+solar "
    "demand, supply shortages, a WEAKER rupee, and higher Indian import duty all "
    "push the INR silver price UP. A stronger dollar, rate hikes, cooling inflation, "
    "a STRONGER rupee, and duty cuts push it DOWN.\n"
    "For each item return: verdict ('good'|'bad'|'neutral' for silver), impact "
    "(0-100 = how much this could actually MOVE the price; routine price recaps and "
    "forecasts are low, real events/decisions/shocks are high), catalyst (true only "
    "if it could cause a surge or reversal, not just describe one), and reason (a "
    "plain, <=14-word explanation a beginner understands)."
)

_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "INTEGER"},
            "verdict": {"type": "STRING", "enum": ["good", "bad", "neutral"]},
            "impact": {"type": "INTEGER"},
            "catalyst": {"type": "BOOLEAN"},
            "reason": {"type": "STRING"},
        },
        "required": ["id", "verdict", "impact", "catalyst", "reason"],
    },
}


def load_config():
    cfg = {"enabled": False, "model": "gemini-flash-latest",
           "api_key": "", "api_keys": [], "max_items": 45}
    if os.path.exists(CONFIG_PATH):
        try:
            cfg.update(json.load(open(CONFIG_PATH)))
        except Exception:  # noqa: BLE001
            pass
    # normalize to a single ordered key list (env wins, then list, then single).
    # On cloud hosts set GEMINI_API_KEYS (comma-separated) or GEMINI_API_KEY.
    keys = []
    env_multi = os.environ.get("GEMINI_API_KEYS")
    if env_multi:
        keys += [k.strip() for k in env_multi.split(",") if k.strip()]
    env = os.environ.get("GEMINI_API_KEY")
    if env:
        keys.append(env)
    keys += [k for k in (cfg.get("api_keys") or []) if k]
    if cfg.get("api_key"):
        keys.append(cfg["api_key"])
    if env_multi or env:                 # env present -> we're cloud-configured
        cfg["enabled"] = True
    seen = set()
    cfg["keys"] = [k for k in keys if not (k in seen or seen.add(k))]
    return cfg


def is_configured():
    cfg = load_config()
    return bool(cfg["enabled"] and cfg["keys"])


def _post(model, api_key, body):
    # header auth (X-goog-api-key) works for both AIza... and newer AQ.* keys,
    # and keeps the key out of the URL / logs.
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "X-goog-api-key": api_key,
    })
    with urllib.request.urlopen(req, timeout=45, context=_SSL_CTX) as r:
        return json.load(r)


def _call_gemini(cfg, headlines):
    listing = "\n".join(f'{h["id"]}. {h["title"]}' for h in headlines)
    prompt = (_SYSTEM + "\n\nClassify these headlines:\n" + listing
              + "\n\nReturn a JSON array, one object per id.")
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "responseSchema": _SCHEMA,
        },
    }).encode()
    # try each model in turn; for each, rotate through the keys.
    #   429 (quota) -> next KEY (per-key limit), 401/403 -> skip that key,
    #   500/503/529 (overload) -> next MODEL after a short retry.
    models = [cfg["model"]] + [m for m in
              ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"]
              if m != cfg["model"]]
    keys = cfg["keys"]
    last = None
    for model in models:
        overloaded = False
        for key in keys:
            try:
                data = _post(model, key, body)
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text)
            except urllib.error.HTTPError as e:
                last = e
                if e.code == 429:           # this key is rate-limited -> next key
                    continue
                if e.code in (401, 403):    # bad key -> next key
                    continue
                if e.code in (500, 503, 529):  # model overloaded -> next model
                    overloaded = True
                    break
                raise
            except Exception as e:  # noqa: BLE001
                last = e
                continue
        if overloaded:
            time.sleep(1.0)
            continue
    raise last if last else RuntimeError("gemini call failed")


_PREMIUM_CACHE = {"pct": None, "reason": "", "ts": 0, "fail_ts": 0}
_premium_busy = False


def _fetch_premium(cfg, parity_inr_kg):
    """Blocking Gemini call to refresh the India premium (runs in a bg thread)."""
    global _premium_busy
    try:
        ctx = (f"International parity is about Rs {parity_inr_kg:,}/kg right now. "
               if parity_inr_kg else "")
        prompt = (
            "You are an India bullion-market expert. " + ctx +
            "Estimate the percentage that MCX / domestic Indian silver currently trades "
            "ABOVE the international parity price (global silver converted at USD/INR). "
            "Account for: import customs duty (incl. AIDC), 3% GST, and the prevailing "
            "local market premium or discount. Reply ONLY as JSON: "
            '{"premium_pct": <number>, "reason": "<=12 words"}.')
        body = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
        }).encode()
        for model in [cfg["model"], "gemini-2.5-flash", "gemini-2.0-flash"]:
            for key in cfg["keys"]:
                try:
                    data = _post(model, key, body)
                    obj = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
                    pct = max(8.0, min(13.0, float(obj["premium_pct"])))  # realistic MCX band
                    _PREMIUM_CACHE.update(pct=round(pct, 2),
                                          reason=str(obj.get("reason", ""))[:60],
                                          ts=time.time())
                    return
                except urllib.error.HTTPError as e:
                    if e.code in (429, 401, 403):
                        continue
                    break
                except Exception:  # noqa: BLE001
                    continue
        _PREMIUM_CACHE["fail_ts"] = time.time()       # back off after a full miss
    finally:
        _premium_busy = False


def estimate_india_premium(parity_inr_kg=None, default=9.3, ttl=21600, fail_ttl=1800):
    """Return the AI-estimated India premium % WITHOUT blocking the request.

    Serves the cached value (or `default`) instantly; if stale, it kicks off the
    Gemini refresh in a background thread so the next call picks up the fresh
    number. This keeps the price path fast even on a cold start."""
    global _premium_busy
    cfg = load_config()
    if not (cfg["enabled"] and cfg["keys"]):
        return default, "default"
    now = time.time()
    fresh = _PREMIUM_CACHE["pct"] is not None and now - _PREMIUM_CACHE["ts"] < ttl
    if fresh:
        return _PREMIUM_CACHE["pct"], "AI"
    # refresh in the background (unless we just failed, or one is already running)
    if not _premium_busy and now - _PREMIUM_CACHE["fail_ts"] >= fail_ttl:
        _premium_busy = True
        threading.Thread(target=_fetch_premium, args=(cfg, parity_inr_kg),
                         daemon=True).start()
    # serve last-known value if we have one, else default — never block
    if _PREMIUM_CACHE["pct"] is not None:
        return _PREMIUM_CACHE["pct"], "AI"
    return default, "default"


_VERDICT_CACHE = {}        # title.lower() -> verdict dict (persists across refreshes)
_enrich_busy = False
_enrich_last = 0
_DIRMAP = {"good": "bullish", "bad": "bearish", "neutral": "neutral"}


def _apply(it, o):
    it["direction"] = _DIRMAP.get(o.get("verdict", "neutral"), "neutral")
    it["label"] = ("GOOD for silver" if it["direction"] == "bullish"
                   else "BAD for silver" if it["direction"] == "bearish" else "Neutral")
    it["impact"] = round(max(0, min(100, float(o.get("impact", it["impact"])))), 1)
    it["catalyst"] = bool(o.get("catalyst", it["catalyst"]))
    it["is_recap"] = it["is_recap"] and not it["catalyst"]
    it["meaning"] = (o.get("reason") or it["meaning"]).strip() or it["meaning"]
    it["ai"] = True


def _bg_enrich(cfg, headlines):
    """Call Gemini for headlines without a cached verdict; store by title (bg thread)."""
    global _enrich_busy
    try:
        arr = _call_gemini(cfg, [{"id": i, "title": h} for i, h in enumerate(headlines)])
        for o in arr:
            try:
                _VERDICT_CACHE[headlines[int(o["id"])].lower()] = o
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    finally:
        _enrich_busy = False


def enrich(items):
    """Apply cached Gemini verdicts instantly; fetch missing ones in the background.

    Never blocks the request: headlines we've already judged are upgraded on the
    spot, brand-new ones keep their keyword classification until the background
    Gemini call (throttled) fills the verdict cache for the next refresh.
    """
    global _enrich_busy, _enrich_last
    cfg = load_config()
    if not (cfg["enabled"] and cfg["keys"]) or not items:
        return items, {"used": False, "reason": "not configured"}

    top = items[:cfg["max_items"]]
    applied, missing = 0, []
    for it in top:
        v = _VERDICT_CACHE.get(it["title"].lower())
        if v:
            _apply(it, v)
            applied += 1
        else:
            missing.append(it["title"])

    # kick a single background Gemini call for the unseen headlines (throttled)
    now = time.time()
    if missing and not _enrich_busy and now - _enrich_last > 45:
        _enrich_busy, _enrich_last = True, now
        threading.Thread(target=_bg_enrich, args=(cfg, missing[:cfg["max_items"]]),
                         daemon=True).start()

    items.sort(key=lambda x: (not x["catalyst"], -x["impact"],
                              x["age_hours"] if x.get("age_hours") else 999))
    used = applied > 0
    return items, {"used": used, "analyzed": applied,
                   "model": cfg["model"] if used else "pending",
                   "reason": "warming up" if not used else "ok"}


if __name__ == "__main__":
    import news_engine
    news = news_engine.collect()
    enriched, meta = enrich(news)
    print("meta:", meta)
    for i in [x for x in enriched if x.get("ai")][:8]:
        print(f"  {'⚡' if i['catalyst'] else '  '} [{i['impact']:>4}] {i['direction']:>7} "
              f"| {i['title'][:50]} :: {i['meaning']}")
