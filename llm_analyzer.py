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


def estimate_india_premium(parity_inr_kg=None, default=9.3, ttl=21600, fail_ttl=1800):
    """Ask Gemini for the % that MCX/domestic silver trades ABOVE global parity
    (import duty + GST + local premium). Cached ~6h on success / ~30 min on failure
    (so a dead quota doesn't get retried every refresh); clamped to a sane band;
    falls back to `default` if AI is off or errors. Returns (pct, source)."""
    cfg = load_config()
    if not (cfg["enabled"] and cfg["keys"]):
        return default, "default"
    now = time.time()
    if _PREMIUM_CACHE["pct"] is not None and now - _PREMIUM_CACHE["ts"] < ttl:
        return _PREMIUM_CACHE["pct"], "AI (cached)"
    if now - _PREMIUM_CACHE["fail_ts"] < fail_ttl:    # recently failed -> don't hammer
        return default, "default"
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
    models = [cfg["model"], "gemini-2.5-flash", "gemini-2.0-flash"]
    for model in models:
        for key in cfg["keys"]:
            try:
                data = _post(model, key, body)
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                obj = json.loads(text)
                # clamp to a realistic MCX band: ~6% duty + 3% GST + small premium
                pct = max(8.0, min(13.0, float(obj["premium_pct"])))
                _PREMIUM_CACHE.update(pct=round(pct, 2),
                                      reason=str(obj.get("reason", ""))[:60],
                                      ts=time.time())
                return _PREMIUM_CACHE["pct"], "AI"
            except urllib.error.HTTPError as e:
                if e.code in (429, 401, 403):
                    continue
                break
            except Exception:  # noqa: BLE001
                continue
    _PREMIUM_CACHE["fail_ts"] = time.time()           # back off after a full miss
    return default, "default"


def enrich(items):
    """Re-classify the top news items with Gemini. Mutates and returns items + meta.

    Only the strongest `max_items` (by keyword impact) are sent to keep one call
    cheap; the rest keep their keyword classification.
    """
    cfg = load_config()
    if not (cfg["enabled"] and cfg["keys"]) or not items:
        return items, {"used": False, "reason": "not configured"}

    top = items[:cfg["max_items"]]
    cache_key = hashlib.md5(
        "|".join(i["title"] for i in top).encode()).hexdigest()
    if _CACHE["key"] == cache_key:
        verdicts = _CACHE["result"]
    else:
        headlines = [{"id": idx, "title": it["title"]} for idx, it in enumerate(top)]
        try:
            arr = _call_gemini(cfg, headlines)
        except Exception as e:  # noqa: BLE001
            return items, {"used": False, "reason": f"gemini error: {type(e).__name__}"}
        verdicts = {int(o["id"]): o for o in arr if "id" in o}
        _CACHE["key"], _CACHE["result"] = cache_key, verdicts

    DIRMAP = {"good": "bullish", "bad": "bearish", "neutral": "neutral"}
    n = 0
    for idx, it in enumerate(top):
        o = verdicts.get(idx)
        if not o:
            continue
        n += 1
        it["direction"] = DIRMAP.get(o.get("verdict", "neutral"), "neutral")
        it["label"] = ("GOOD for silver" if it["direction"] == "bullish"
                       else "BAD for silver" if it["direction"] == "bearish"
                       else "Neutral")
        it["impact"] = round(max(0, min(100, float(o.get("impact", it["impact"])))), 1)
        it["catalyst"] = bool(o.get("catalyst", it["catalyst"]))
        it["is_recap"] = it["is_recap"] and not it["catalyst"]
        it["meaning"] = o.get("reason", it["meaning"]).strip() or it["meaning"]
        it["ai"] = True

    # re-rank with the AI-adjusted scores
    items.sort(key=lambda x: (not x["catalyst"], -x["impact"],
                              x["age_hours"] if x.get("age_hours") else 999))
    return items, {"used": True, "analyzed": n, "model": cfg["model"]}


if __name__ == "__main__":
    import news_engine
    news = news_engine.collect()
    enriched, meta = enrich(news)
    print("meta:", meta)
    for i in [x for x in enriched if x.get("ai")][:8]:
        print(f"  {'⚡' if i['catalyst'] else '  '} [{i['impact']:>4}] {i['direction']:>7} "
              f"| {i['title'][:50]} :: {i['meaning']}")
