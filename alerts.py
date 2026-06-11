#!/usr/bin/env python3
"""
Alert engine.

Fires an alert when the Buy Score crosses a threshold:
    score >= BUY threshold  (default 75)  -> "time to BUY"
    score <= SELL threshold (default 25)  -> "time to SELL"

Delivery: email over SMTP. To reach a PHONE, set the recipient to your carrier's
email-to-SMS gateway address (e.g. 9876543210@txt.example) -- free, no SMS API.

Config lives in alert_config.json next to this file. If SMTP isn't configured,
alerts are still logged to alerts.log (and exposed in the UI) so nothing is lost.

alert_config.json example:
{
  "enabled": true,
  "buy_threshold": 75,
  "sell_threshold": 25,
  "cooldown_minutes": 180,
  "smtp_host": "smtp.gmail.com",
  "smtp_port": 465,
  "smtp_user": "you@gmail.com",
  "smtp_pass": "your-app-password",
  "from_addr": "you@gmail.com",
  "to_addrs": ["you@gmail.com", "9876543210@vtext.com"]
}
"""
import json
import os
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage

# macOS framework Python often lacks CA certs -> use certifi's bundle if present
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:  # noqa: BLE001
    _SSL_CTX = ssl.create_default_context()

_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_DIR, "alert_config.json")
STATE_PATH = os.path.join(_DIR, "alert_state.json")
LOG_PATH = os.path.join(_DIR, "alerts.log")

DEFAULTS = {
    "enabled": True,
    "buy_threshold": 75,
    "sell_threshold": 25,
    "cooldown_minutes": 180,
    "smtp_host": "", "smtp_port": 465, "smtp_user": "", "smtp_pass": "",
    "from_addr": "", "to_addrs": [],
}


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            cfg.update(json.load(open(CONFIG_PATH)))
        except Exception:  # noqa: BLE001
            pass
    # env overlay so cloud hosts can configure alerts without a committed file
    env = os.environ
    for k, ek in [("smtp_host", "SMTP_HOST"), ("smtp_user", "SMTP_USER"),
                  ("smtp_pass", "SMTP_PASS"), ("from_addr", "ALERT_FROM")]:
        if env.get(ek):
            cfg[k] = env[ek]
    if env.get("SMTP_PORT"):
        cfg["smtp_port"] = int(env["SMTP_PORT"])
    if env.get("ALERT_TO"):
        cfg["to_addrs"] = [a.strip() for a in env["ALERT_TO"].split(",") if a.strip()]
    if env.get("BUY_THRESHOLD"):
        cfg["buy_threshold"] = int(env["BUY_THRESHOLD"])
    if env.get("SELL_THRESHOLD"):
        cfg["sell_threshold"] = int(env["SELL_THRESHOLD"])
    return cfg


def _state():
    if os.path.exists(STATE_PATH):
        try:
            return json.load(open(STATE_PATH))
        except Exception:  # noqa: BLE001
            pass
    return {"last_signal": None, "last_sent": None}


def _save_state(s):
    try:
        json.dump(s, open(STATE_PATH, "w"))
    except Exception:  # noqa: BLE001
        pass


def _smtp_ready(cfg):
    return bool(cfg["smtp_host"] and cfg["smtp_user"] and cfg["smtp_pass"]
                and cfg["to_addrs"])


def _send_email(cfg, subject, body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from_addr"] or cfg["smtp_user"]
    msg["To"] = ", ".join(cfg["to_addrs"])
    msg.set_content(body)
    ctx = _SSL_CTX
    port = int(cfg["smtp_port"])
    if port == 465:
        with smtplib.SMTP_SSL(cfg["smtp_host"], port, context=ctx, timeout=20) as s:
            s.login(cfg["smtp_user"], cfg["smtp_pass"])
            s.send_message(msg)
    else:  # 587 / STARTTLS
        with smtplib.SMTP(cfg["smtp_host"], port, timeout=20) as s:
            s.starttls(context=ctx)
            s.login(cfg["smtp_user"], cfg["smtp_pass"])
            s.send_message(msg)


def _log(line):
    stamp = datetime.now(timezone.utc).isoformat()
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"{stamp}  {line}\n")
    except Exception:  # noqa: BLE001
        pass


def check(score_data, market):
    """Evaluate the latest score and fire an alert if a threshold is crossed.

    Returns a status dict the UI can show. De-dupes: only fires when the signal
    changes (hold->buy / hold->sell) or after the cooldown has elapsed.
    """
    cfg = load_config()
    score = score_data["score"]
    now = datetime.now(timezone.utc)

    if score >= cfg["buy_threshold"]:
        signal = "buy"
    elif score <= cfg["sell_threshold"]:
        signal = "sell"
    else:
        signal = None

    st = _state()
    last_signal = st.get("last_signal")
    last_sent = st.get("last_sent")
    cooled = True
    if last_sent:
        try:
            mins = (now - datetime.fromisoformat(last_sent)).total_seconds() / 60
            cooled = mins >= cfg["cooldown_minutes"]
        except Exception:  # noqa: BLE001
            cooled = True

    fired = False
    detail = "no threshold crossed"
    if signal and cfg["enabled"] and (signal != last_signal or cooled):
        price = market.get("price")
        cur = market.get("currency", "")
        verb = "BUY" if signal == "buy" else "SELL"
        emoji = "🟢" if signal == "buy" else "🔴"
        subject = f"🪙 Silver {verb} signal — score {score}/100"
        body = (
            f"{emoji} Silver {verb} signal — score {score}/100 ({score_data['label']})\n"
            f"Price: ₹{price:,} {cur}\n\n"
            "Why:\n• " + "\n• ".join(score_data["reasons"]) + "\n\n"
            f"News {score_data['components']['news']:+} | "
            f"Levels {score_data['components']['levels']:+} | "
            f"Momentum {score_data['components']['momentum']:+}\n\n"
            "— Silver Tracker (educational, not advice)"
        )
        if _smtp_ready(cfg):
            try:
                _send_email(cfg, subject, body)
                detail = f"email sent for {verb} ({score})"
                fired = True
            except Exception as e:  # noqa: BLE001
                detail = f"email FAILED: {e}"
                _log(detail)
        else:
            detail = f"{verb} signal ({score}) — email not configured, logged only"
        _log(detail)
        if fired or not _smtp_ready(cfg):
            st["last_signal"] = signal
            st["last_sent"] = now.isoformat()
            _save_state(st)

    return {
        "signal": signal,
        "fired": fired,
        "detail": detail,
        "smtp_configured": _smtp_ready(cfg),
        "buy_threshold": cfg["buy_threshold"],
        "sell_threshold": cfg["sell_threshold"],
        "enabled": cfg["enabled"],
        "checked_at": now.isoformat(),
    }
