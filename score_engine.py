#!/usr/bin/env python3
"""
Buy Score (0-100) for silver.

Blends three forces into one easy number:

  * NEWS       -- bullish news pushes up, bearish news pushes down   (+/- 25)
  * LEVELS     -- price near SUPPORT pushes up, near RESISTANCE down (+/- 25)
  * MOMENTUM   -- oversold (low RSI) pushes up, overbought down      (+/- 15)

50 = neutral. Higher = better time to BUY, lower = better time to SELL.

    >= 75  STRONG BUY
    60-74  BUY
    40-59  HOLD / NEUTRAL
    25-39  SELL
    <  25  STRONG SELL
"""


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def compute(market, news):
    reasons = []
    score = 50.0

    # ---- NEWS component (news bias is already -100..+100) ----
    nb = news.get("summary", {}).get("score", 0)
    news_c = _clamp(nb * 0.25, -25, 25)
    if abs(nb) >= 12:
        reasons.append(
            ("Recent news is %s (%+d)." %
             ("bullish" if nb > 0 else "bearish", nb)))
    else:
        reasons.append("News flow is mixed / neutral.")

    # ---- LEVELS component (where price sits between support & resistance) ----
    sup = market.get("support") or []
    res = market.get("resistance") or []
    near_sup = sup[0] if sup else None       # already nearest-first
    near_res = res[0] if res else None
    level_c = 0.0
    if near_sup and near_res:
        ds = abs(near_sup["dist_pct"])       # % down to support
        dr = abs(near_res["dist_pct"])       # % up to resistance
        if ds + dr > 0:
            bull = dr / (ds + dr)            # ->1 when hugging support
            level_c = _clamp((bull - 0.5) * 50, -25, 25)
        if ds <= 1.5:
            reasons.append("Price is sitting ON support — bounce zone.")
        elif dr <= 1.5:
            reasons.append("Price is pressing into resistance — capped.")
        elif ds < dr:
            reasons.append("Price is closer to support than resistance.")
        else:
            reasons.append("Price is closer to resistance than support.")
    elif near_sup and not near_res:
        level_c = 18  # clear air above, only support below
        reasons.append("No resistance overhead — clear air above.")
    elif near_res and not near_sup:
        level_c = -18
        reasons.append("No support below — air pocket underneath.")

    # ---- MOMENTUM component (RSI mean-reversion) ----
    rsi = market.get("indicators", {}).get("rsi14", 50)
    mom_c = _clamp((50 - rsi) / 50 * 15, -15, 15)
    if rsi <= 30:
        reasons.append("RSI oversold (%.0f) — stretched to the downside." % rsi)
    elif rsi >= 70:
        reasons.append("RSI overbought (%.0f) — stretched to the upside." % rsi)

    score = _clamp(50 + news_c + level_c + mom_c, 0, 100)
    score = round(score)

    if score >= 75:
        label, signal = "STRONG BUY", "buy"
    elif score >= 60:
        label, signal = "BUY", "buy"
    elif score >= 40:
        label, signal = "HOLD", "hold"
    elif score >= 25:
        label, signal = "SELL", "sell"
    else:
        label, signal = "STRONG SELL", "sell"

    return {
        "score": score,
        "label": label,
        "signal": signal,
        "components": {
            "news": round(news_c, 1),
            "levels": round(level_c, 1),
            "momentum": round(mom_c, 1),
        },
        "reasons": reasons,
    }
