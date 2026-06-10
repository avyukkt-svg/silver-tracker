#!/usr/bin/env python3
"""
Free silver-news engine + impact classifier.

NO API KEY REQUIRED. News is pulled from Google News RSS feeds across the set of
themes that move silver, then each headline is scored for:

    * direction  -> bullish / bearish / neutral for silver
    * directness -> direct (silver/precious-metals) vs indirect (macro driver)
    * impact     -> composite 0..100 magnitude

WHY THESE THEMES MOVE SILVER
----------------------------
Silver is a hybrid precious + industrial metal, so it reacts to:
  - US dollar (DXY)        : inverse -- strong USD = bearish silver
  - Fed / interest rates   : higher rates / hawkish = bearish (no yield on metal)
  - inflation / CPI        : higher inflation = bullish (store of value)
  - industrial demand      : solar, EV, electronics = bullish
  - safe-haven / geopolitics: war, crisis, risk-off = bullish
  - gold                   : silver tends to follow gold's direction
  - mine supply            : supply disruption = bullish
"""
import html
import re
import ssl
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

USER_AGENT = "Mozilla/5.0 (SilverTracker/1.0)"

# macOS framework Python often ships without CA certs -> use certifi's bundle if
# available, else fall back to the default context.
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:  # noqa: BLE001
    _SSL_CTX = ssl.create_default_context()

# RSS topic queries -> (label, directness). Google News turns any query into RSS.
TOPICS = [
    ("silver price",                       "direct"),
    ("silver futures COMEX",               "direct"),
    ("precious metals market",             "direct"),
    ("gold price",                         "direct"),
    ("Federal Reserve interest rates",     "indirect"),
    ("US inflation CPI",                   "indirect"),
    ("US dollar index DXY",                "indirect"),
    ("solar panel silver demand",          "indirect"),
    ("industrial metals demand",           "indirect"),
    ("geopolitical risk safe haven",       "indirect"),
    # --- geopolitics / safe-haven (kept lean; near-duplicates are merged later) ---
    ("Middle East war safe haven gold",    "indirect"),
    # --- other macro drivers that move silver ---
    ("Treasury yields real interest rates", "indirect"),
    ("crude oil price inflation",          "indirect"),
    ("central bank gold buying reserves",  "indirect"),
    ("gold silver ratio",                  "direct"),
    ("silver mine supply deficit",         "direct"),
    ("silver ETF holdings investment demand", "direct"),
    # --- INDIA / INR specific: these move the rupee price of silver directly ---
    ("silver price India MCX",             "direct"),
    ("rupee vs dollar exchange rate",      "indirect"),
    ("India silver import customs duty",   "indirect"),
    ("RBI policy rupee",                   "indirect"),
]

# ---- THEMATIC lexicon: unambiguous phrases, sign is fixed regardless of subject.
#      weight = + bullish for silver, - bearish for silver ----
BULLISH = {
    "rate cut": 3, "cuts rates": 3, "cut rates": 3, "dovish": 2, "easing": 2,
    "stimulus": 2, "inflation rises": 3, "inflation surges": 3, "hot cpi": 3,
    "cpi rises": 2, "sticky inflation": 2, "inflation persists": 2,
    "safe haven": 2, "safe-haven": 2, "war": 3, "conflict": 2, "crisis": 2,
    "escalates": 2, "escalating": 2, "escalation": 2, "tension": 2, "tensions": 2,
    "attack": 2, "attacks": 2, "strike": 2, "strikes": 2, "airstrike": 2,
    "airstrikes": 2, "missile": 2, "missiles": 2, "bombing": 2, "invades": 3,
    "invasion": 3, "retaliation": 2, "retaliates": 2,
    "geopolitical": 2, "recession": 2, "uncertainty": 1,
    "demand grows": 3, "supply deficit": 3, "shortage": 3, "solar demand": 3,
    "ev demand": 2, "buying": 1, "bullish": 2, "supports": 1, "record high": 2,
    "all-time high": 2, "breakout": 2,
    # India / INR specific (raise the domestic rupee price of silver)
    "duty hike": 3, "import duty hike": 3, "duty increased": 2, "duty raised": 2,
    "weak rupee": 2, "rupee record low": 3, "rupee at record low": 3,
    "festival demand": 2, "wedding season": 2,
}
BEARISH = {
    "rate hike": 3, "hikes rates": 3, "hike rates": 3, "hawkish": 2,
    "tightening": 2, "higher for longer": 2, "inflation cools": 3,
    "inflation eases": 3, "cooling inflation": 3, "soft cpi": 3, "disinflation": 2,
    "demand weak": 2, "weak demand": 2, "oversupply": 3, "surplus": 2,
    "profit taking": 1, "profit-taking": 1, "bearish": 2, "correction": 1,
    # India / INR specific (lower the domestic rupee price of silver)
    "duty cut": 3, "import duty cut": 3, "duty reduced": 2, "duty slashed": 3,
    "strong rupee": 2,
}

# ---- CONTEXT-AWARE momentum: generic up/down words whose meaning for silver
#      depends on the SUBJECT of the headline.
UP_WORDS = ["surge", "surges", "soars", "soar", "jumps", "jump", "rally",
            "rallies", "gains", "gain", "rises", "rise", "climbs", "climb",
            "higher", "spikes", "spike", "rebounds", "advance",
            "strengthens", "strengthen", "appreciates", "firms"]
DOWN_WORDS = ["falls", "fall", "drops", "drop", "tumbles", "tumble", "sinks",
              "sink", "slumps", "slump", "plunges", "plunge", "slides", "slide",
              "lower", "sell-off", "selloff", "retreats", "dips", "dip", "eases",
              "weakens", "weaken", "depreciates", "softens"]
# subjects that move INVERSELY to the silver price: up = bearish.
# NOTE the rupee is here too -- a STRONGER rupee means FEWER rupees per kg, so the
# INR silver price FALLS (bearish); a weaker rupee lifts the INR price.
INVERSE_SUBJECTS = ["dollar", "dxy", "greenback", "yield", "yields", "treasury",
                    "treasuries", "real rate", "bond", "rupee", "inr"]
# subjects that move WITH silver: up = bullish for silver
DIRECT_SUBJECTS = ["silver", "gold", "precious metal", "bullion", "xag", "xau",
                   "metal"]


def _fetch_rss(query, limit=12):
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(query)
           + "&hl=en-US&gl=US&ceid=US:en")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=12, context=_SSL_CTX) as r:
        raw = r.read()
    root = ET.fromstring(raw)
    items = []
    for it in root.iter("item"):
        title = it.findtext("title") or ""
        link = it.findtext("link") or ""
        pub = it.findtext("pubDate") or ""
        src_el = it.find("source")
        source = (src_el.text if src_el is not None else "") or ""
        items.append({
            "title": html.unescape(title.strip()),
            "link": link.strip(),
            "published": pub.strip(),
            "source": html.unescape(source.strip()),
        })
        if len(items) >= limit:
            break
    return items


_WORD_RE = {}


def _present(term, t):
    """Whole-word match so 'war' does not fire inside 'warns', 'ban' in 'urban'."""
    rx = _WORD_RE.get(term)
    if rx is None:
        rx = _WORD_RE[term] = re.compile(r"\b" + re.escape(term) + r"\b")
    return rx.search(t) is not None


def _has(words, t):
    return any(" " + w + " " in t or "-" + w in t for w in words)


def _count(words, t):
    return sum(1 for w in words if (" " + w + " ") in t or ("-" + w) in t)


def _score(title):
    """Return (direction, net, magnitude, hits) for a headline string.

    Two layers: (1) fixed-sign thematic phrases, (2) context-aware momentum where
    a generic up/down word is interpreted against the headline's subject -- e.g.
    'dollar gains' is bearish for silver, 'silver gains' is bullish.
    """
    t = " " + re.sub(r"[^a-z0-9 \-]", " ", title.lower()) + " "
    hits = []

    # layer 1: thematic phrases (sign fixed)
    bull = 0
    bear = 0
    for k, w in BULLISH.items():
        if _present(k, t):
            bull += w
            hits.append(k)
    for k, w in BEARISH.items():
        if _present(k, t):
            bear += w
            hits.append(k)

    # layer 2: context-aware momentum
    ups, downs = _count(UP_WORDS, t), _count(DOWN_WORDS, t)
    has_inv = _has(INVERSE_SUBJECTS, t)
    has_dir = _has(DIRECT_SUBJECTS, t)
    if ups or downs:
        # inverse subject and NOT primarily about the metal -> flip the sign
        if has_inv and not has_dir:
            bull += 2 * downs
            bear += 2 * ups
            subj = "rupee/inr" if _has(["rupee", "inr"], t) else "dollar/yields"
            hits.append(subj + (" down" if downs >= ups else " up"))
        else:  # metal subject (or default to direct reading)
            bull += 2 * ups
            bear += 2 * downs
            if ups or downs:
                hits.append(("price up" if ups >= downs else "price down"))

    # layer 3: India import-duty (word order varies: "hikes import duty" etc.)
    if _present("duty", t):
        if _has(["hike", "hikes", "raised", "raise", "increased", "increase",
                 "higher", "hiked"], t) and "duty hike" not in hits:
            bull += 3
            hits.append("duty hike")
        elif _has(["cut", "cuts", "slashed", "slashes", "reduced", "reduce",
                   "lowered", "scrapped", "scraps"], t) and "duty cut" not in hits:
            bear += 3
            hits.append("duty cut")

    net = bull - bear
    direction = "bullish" if net > 0 else "bearish" if net < 0 else "neutral"
    magnitude = bull + bear
    return direction, net, magnitude, hits


# plain-English explanation of WHY a trigger matters for silver
EXPLAIN = {
    "rate cut": "Lower interest rates make non-yielding silver more attractive.",
    "cuts rates": "Lower interest rates make non-yielding silver more attractive.",
    "cut rates": "Lower interest rates make non-yielding silver more attractive.",
    "dovish": "A softer central bank stance tends to lift silver.",
    "easing": "Looser policy / more money in the system supports silver.",
    "stimulus": "Stimulus weakens the dollar and supports silver.",
    "rate hike": "Higher interest rates pull money away from silver.",
    "hikes rates": "Higher interest rates pull money away from silver.",
    "hike rates": "Higher interest rates pull money away from silver.",
    "hawkish": "A tougher central bank stance tends to weigh on silver.",
    "tightening": "Tighter policy is a headwind for silver.",
    "higher for longer": "Rates staying high is a headwind for silver.",
    "hot cpi": "Hot inflation boosts silver as a store of value.",
    "inflation rises": "Rising inflation boosts silver as a store of value.",
    "inflation surges": "Surging inflation boosts silver as a store of value.",
    "sticky inflation": "Persistent inflation supports silver as a hedge.",
    "inflation persists": "Persistent inflation supports silver as a hedge.",
    "inflation cools": "Cooling inflation reduces silver's hedge appeal.",
    "inflation eases": "Easing inflation reduces silver's hedge appeal.",
    "cooling inflation": "Cooling inflation reduces silver's hedge appeal.",
    "soft cpi": "Soft inflation reduces silver's hedge appeal.",
    "disinflation": "Falling inflation reduces silver's hedge appeal.",
    "safe haven": "Investors buy silver as a safe haven in times of fear.",
    "safe-haven": "Investors buy silver as a safe haven in times of fear.",
    "war": "Conflict drives safe-haven buying of silver.",
    "conflict": "Conflict drives safe-haven buying of silver.",
    "escalat": "Escalating tension drives safe-haven buying of silver.",
    "crisis": "Crises push investors toward safe-haven silver.",
    "tension": "Geopolitical tension supports safe-haven silver.",
    "geopolitical": "Geopolitical risk supports safe-haven silver.",
    "recession": "Recession fears push investors toward silver.",
    "solar demand": "Solar panels use silver — more demand lifts prices.",
    "ev demand": "EVs and electronics use silver — demand lifts prices.",
    "demand grows": "Stronger industrial demand supports silver.",
    "supply deficit": "Less silver being mined than used pushes prices up.",
    "shortage": "A supply shortage pushes silver prices up.",
    "oversupply": "Too much silver supply pushes prices down.",
    "surplus": "A supply surplus weighs on silver prices.",
    "demand weak": "Weak industrial demand weighs on silver.",
    "weak demand": "Weak industrial demand weighs on silver.",
    "dollar/yields up": "A stronger dollar / higher yields usually push silver DOWN.",
    "dollar/yields down": "A weaker dollar / lower yields usually push silver UP.",
    "rupee/inr up": "A stronger rupee means fewer ₹ per kg — INR silver price falls.",
    "rupee/inr down": "A weaker rupee means more ₹ per kg — INR silver price rises.",
    "weak rupee": "A weak rupee pushes the rupee price of silver up.",
    "strong rupee": "A strong rupee pulls the rupee price of silver down.",
    "rupee record low": "Rupee at a record low — silver costs more in ₹.",
    "duty hike": "Higher import duty makes silver pricier in India.",
    "import duty hike": "Higher import duty makes silver pricier in India.",
    "duty cut": "Lower import duty makes silver cheaper in India.",
    "import duty cut": "Lower import duty makes silver cheaper in India.",
    "festival demand": "Festival/wedding buying lifts Indian silver demand.",
    "wedding season": "Wedding-season buying lifts Indian silver demand.",
    "attack": "Military escalation drives safe-haven buying of silver.",
    "strikes": "Military strikes drive safe-haven buying of silver.",
    "missile": "Missile escalation drives safe-haven buying of silver.",
    "invades": "An invasion triggers strong safe-haven buying of silver.",
    "escalates": "Escalating conflict drives safe-haven buying of silver.",
    "price up": "Precious-metal prices are climbing.",
    "price down": "Precious-metal prices are falling.",
    "bullish": "Analysts are positive on the metal.",
    "bearish": "Analysts are negative on the metal.",
    "record high": "Prices are hitting record highs — strong momentum.",
    "all-time high": "Prices are hitting all-time highs — strong momentum.",
    "breakout": "A technical breakout signals further upside.",
    "correction": "A pullback after a strong run.",
    "profit taking": "Traders banking gains, a mild pullback.",
    "profit-taking": "Traders banking gains, a mild pullback.",
    "supports": "Supportive backdrop for prices.",
}


def _trigger_sign(t):
    """+1 if this trigger is bullish for silver, -1 if bearish, 0 if ambiguous."""
    if t in BULLISH or t in ("dollar/yields down", "price up", "rupee/inr down"):
        return 1
    if t in BEARISH or t in ("dollar/yields up", "price down", "rupee/inr up"):
        return -1
    return 0


def explain(direction, triggers):
    """Plain-English read of what a headline means for silver.

    Prefer an explanation whose direction matches the headline's net verdict, so
    a 'bearish' item never gets explained with a bullish-sounding reason.
    """
    want = 1 if direction == "bullish" else -1 if direction == "bearish" else 0
    if want:
        for t in triggers:
            if t in EXPLAIN and _trigger_sign(t) == want:
                return EXPLAIN[t]
    if direction == "neutral":
        return "Mixed / offsetting signals — no clear net effect on silver."
    for t in triggers:           # fall back to any known explanation
        if t in EXPLAIN:
            return EXPLAIN[t]
    if direction == "bullish":
        return "Broadly positive for silver."
    return "Broadly negative for silver."


# ============================================================================
# CATALYST DETECTION
# A "catalyst" is news that can CAUSE the next move (a decision, surprise, shock,
# escalation, fresh data) -- as opposed to a recap that just DESCRIBES a move
# that already happened ("silver price today", "gold falls 2%", forecasts).
# Each catalyst term -> (weight, category). Higher weight = bigger mover.
# ============================================================================
CATALYST_TERMS = {
    # --- monetary policy (the single biggest silver driver) ---
    "rate cut": (3, "Fed / Rates"), "rate hike": (3, "Fed / Rates"),
    "cuts rates": (3, "Fed / Rates"), "hikes rates": (3, "Fed / Rates"),
    "cut rates": (3, "Fed / Rates"), "hike rates": (3, "Fed / Rates"),
    "rate decision": (3, "Fed / Rates"), "fomc": (3, "Fed / Rates"),
    "fed meeting": (3, "Fed / Rates"), "federal reserve": (2, "Fed / Rates"),
    "powell": (2, "Fed / Rates"), "ecb": (2, "Fed / Rates"),
    "central bank": (2, "Fed / Rates"), "pivot": (2, "Fed / Rates"),
    "dovish": (2, "Fed / Rates"), "hawkish": (2, "Fed / Rates"),
    "emergency": (3, "Fed / Rates"), "rate path": (2, "Fed / Rates"),
    # --- economic data prints (move markets on release / surprise) ---
    "cpi": (2, "Inflation data"), "inflation data": (3, "Inflation data"),
    "inflation report": (3, "Inflation data"), "ppi": (2, "Inflation data"),
    "jobs report": (2, "Jobs data"), "payrolls": (2, "Jobs data"),
    "nonfarm": (2, "Jobs data"), "unemployment": (2, "Jobs data"),
    "gdp": (2, "Growth data"), "pce": (2, "Inflation data"),
    # --- surprise / shock modifiers (turn data into a catalyst) ---
    "unexpected": (2, "Surprise"), "surprise": (2, "Surprise"),
    "shock": (2, "Surprise"), "hotter than expected": (3, "Surprise"),
    "cooler than expected": (3, "Surprise"), "higher than expected": (2, "Surprise"),
    "lower than expected": (2, "Surprise"), "beats estimates": (2, "Surprise"),
    "misses estimates": (2, "Surprise"), "smashes": (2, "Surprise"),
    "stronger than expected": (2, "Surprise"), "weaker than expected": (2, "Surprise"),
    # --- geopolitics / risk shocks ---
    "war": (3, "Geopolitics"), "invades": (3, "Geopolitics"),
    "invasion": (3, "Geopolitics"), "attack": (2, "Geopolitics"),
    "strikes": (2, "Geopolitics"), "missile": (3, "Geopolitics"),
    "nuclear": (3, "Geopolitics"), "sanction": (2, "Geopolitics"),
    "sanctions": (2, "Geopolitics"), "tariff": (2, "Trade / Policy"),
    "tariffs": (2, "Trade / Policy"), "embargo": (3, "Trade / Policy"),
    "ban": (2, "Trade / Policy"), "ceasefire": (2, "Geopolitics"),
    "escalate": (2, "Geopolitics"), "escalates": (2, "Geopolitics"),
    "escalation": (2, "Geopolitics"), "conflict": (2, "Geopolitics"),
    "crisis": (2, "Geopolitics"), "default": (3, "Geopolitics"),
    # --- supply / demand shocks specific to silver ---
    "supply deficit": (3, "Supply / Demand"), "shortage": (3, "Supply / Demand"),
    "mine strike": (3, "Supply / Demand"), "production halt": (3, "Supply / Demand"),
    "output cut": (2, "Supply / Demand"), "squeeze": (3, "Supply / Demand"),
    "record demand": (2, "Supply / Demand"), "stockpile": (2, "Supply / Demand"),
    "oversupply": (2, "Supply / Demand"), "surplus": (2, "Supply / Demand"),
    "solar demand": (2, "Supply / Demand"), "ev demand": (2, "Supply / Demand"),
    "industrial demand": (2, "Supply / Demand"), "deficit": (2, "Supply / Demand"),
    # --- structural / stimulus ---
    "stimulus": (2, "Policy / Stimulus"), "quantitative easing": (3, "Policy / Stimulus"),
    "intervention": (2, "Policy / Stimulus"), "import duty": (2, "India / Duty"),
    "duty hike": (3, "India / Duty"), "duty cut": (3, "India / Duty"),
    "import duty hike": (3, "India / Duty"), "import duty cut": (3, "India / Duty"),
    "duty slashed": (3, "India / Duty"), "rupee record low": (3, "Rupee / FX"),
    "breakout": (2, "Technical break"), "all-time high": (2, "Technical break"),
    "record high": (2, "Technical break"),
}

# markers of a routine recap / explainer (NOT a catalyst by themselves)
RECAP_TERMS = [
    "price today", "rate today", "prices today", "rates today", "today:",
    "live updates", "live:", "forecast", "outlook", "technical analysis",
    "analysis:", "what to watch", "week ahead", "preview", "recap", "wrap",
    "closes", "settles", "settled", "session", "morning", "midday", "afternoon",
    "in your city", "across cities", "per 10 gram", "per gram", "10 grams",
    "tola", "price on", "prices on", "check rates", "check gold", "mcx rates",
    "here's why", "heres why", "why are", "why is", "explained", "things to know",
]

_PCT_MOVE = re.compile(r"\b\d+(?:\.\d+)?\s?%")


def classify_catalyst(title, hits):
    """Return (is_catalyst, category, cat_score) for a headline.

    A headline is a catalyst if it names a real market-moving event/surprise and
    isn't merely a price recap. Recaps with no catalyst term get cat_score 0.
    """
    raw = title.lower()
    t = " " + re.sub(r"[^a-z0-9 \-]", " ", raw) + " "
    cat_score = 0
    cats = {}
    for term, (w, cat) in CATALYST_TERMS.items():
        if _present(term, t):
            cat_score += w
            cats[cat] = cats.get(cat, 0) + w

    # recap markers are matched against the raw title so 'today:' etc. still count
    is_recap = any(m in raw for m in RECAP_TERMS)
    # a bare "% move" headline with no catalyst is just a recap of a past move
    if _PCT_MOVE.search(raw) and cat_score < 3:
        is_recap = True

    is_catalyst = cat_score >= 3
    category = max(cats, key=cats.get) if cats else None
    return is_catalyst, category, cat_score, is_recap


def _parse_date(s):
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def collect(per_topic=10):
    """Fetch, classify, de-dupe and rank all silver-relevant news."""
    seen = set()
    out = []
    now = datetime.now(timezone.utc)
    for query, directness in TOPICS:
        try:
            items = _fetch_rss(query, limit=per_topic)
        except Exception:  # noqa: BLE001  -- skip a flaky feed, keep the rest
            continue
        for it in items:
            key = it["title"].lower()
            if not it["title"] or key in seen:
                continue
            seen.add(key)
            direction, net, magnitude, hits = _score(it["title"])
            is_cat, category, cat_score, is_recap = classify_catalyst(it["title"], hits)
            dt = _parse_date(it["published"])
            age_h = (now - dt).total_seconds() / 3600 if dt else 999
            # recency multiplier: full weight < 24h, decays after
            recency = max(0.2, min(1.0, 1.5 - age_h / 48))

            # MOVE POTENTIAL: how much this could actually move price.
            # Dominated by the catalyst score; sentiment words add a little.
            # Pure recaps (no catalyst) are crushed so they sink to the bottom.
            raw = cat_score * 16 + magnitude * 3
            raw *= (1.15 if directness == "direct" else 1.0)
            if is_recap and cat_score < 3:
                raw *= 0.2
            move = round(min(100, raw * recency), 1)

            simple = ("GOOD for silver" if direction == "bullish"
                      else "BAD for silver" if direction == "bearish"
                      else "Neutral")
            out.append({
                "title": it["title"],
                "source": it["source"],
                "link": it["link"],
                "published": it["published"],
                "age_hours": round(age_h, 1) if dt else None,
                "topic": query,
                "directness": directness,
                "direction": direction,
                "label": simple,
                "meaning": explain(direction, hits),
                "net": net,
                "impact": move,            # = move potential (catalyst-weighted)
                "catalyst": is_cat,
                "catalyst_type": category,
                "is_recap": bool(is_recap and not is_cat),
                "triggers": hits,
            })
    out = _merge_duplicates(out)
    # rank: catalysts first, then by move potential, then most recent
    out.sort(key=lambda x: (not x["catalyst"], -x["impact"],
                            x["age_hours"] if x["age_hours"] else 999))
    return out


_STOP = {"the", "a", "an", "to", "of", "in", "on", "for", "and", "as", "at",
         "is", "are", "with", "after", "amid", "says", "say", "new", "live",
         "updates", "update", "latest", "news", "today", "day", "over", "from"}


def _sig(title):
    """Significant-word fingerprint of a headline for near-duplicate detection."""
    words = re.sub(r"[^a-z0-9 ]", " ", title.lower()).split()
    return {w for w in words if len(w) > 3 and w not in _STOP}


def _merge_duplicates(items, thresh=0.5):
    """Collapse headlines about the same event (many outlets) into one.

    Keeps the highest-impact representative and records how many sources covered
    it -- broad coverage is itself a signal that the event is a big one.
    """
    items = sorted(items, key=lambda x: -x["impact"])
    kept = []
    sigs = []
    for it in items:
        s = _sig(it["title"])
        merged = False
        for i, ks in enumerate(sigs):
            if s and ks:
                jac = len(s & ks) / len(s | ks)
                if jac >= thresh:                    # same story, different outlet
                    kept[i]["coverage"] += 1
                    merged = True
                    break
        if not merged:
            it["coverage"] = 1
            kept.append(it)
            sigs.append(s)
    # heavier coverage nudges move-potential up (capped), as a crowd-confirmation
    for it in kept:
        if it["coverage"] > 1:
            it["impact"] = round(min(100, it["impact"] * (1 + 0.06 * min(it["coverage"] - 1, 6))), 1)
    return kept


def summarize(items):
    """Aggregate the directional read, driven by the market-MOVING items.

    Catalysts dominate the bias; routine recaps barely register because their
    move-potential (impact) was crushed in collect().
    """
    scored = [i for i in items if i["impact"] > 0 and i["direction"] != "neutral"]
    bull = sum(i["impact"] for i in scored if i["direction"] == "bullish")
    bear = sum(i["impact"] for i in scored if i["direction"] == "bearish")
    catalysts = [i for i in items if i["catalyst"]]
    total = bull + bear
    if total == 0:
        bias, score = "neutral", 0
    else:
        score = round((bull - bear) / total * 100)
        bias = "bullish" if score > 12 else "bearish" if score < -12 else "neutral"
    return {
        "bias": bias,
        "score": score,            # -100 (very bearish) .. +100 (very bullish)
        "bullish_weight": round(bull, 1),
        "bearish_weight": round(bear, 1),
        "items_scored": len(scored),
        "items_total": len(items),
        "catalyst_count": len(catalysts),
    }


if __name__ == "__main__":
    import json
    news = collect()
    print(json.dumps({"summary": summarize(news), "top": news[:10]}, indent=2))
