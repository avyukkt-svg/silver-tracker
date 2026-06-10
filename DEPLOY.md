# Deploying Silver Tracker to the cloud (free, 24/7)

Goal: the app runs and **alerts fire around the clock**, even when your Mac is
off. We use **Render** (free, no credit card) + a free uptime pinger to keep it
awake. Total time ~15 minutes.

> Why a pinger? Render's free tier puts the app to sleep after 15 min with no
> traffic — and a sleeping app can't run the alert scheduler. A pinger hits the
> app every few minutes so it never sleeps. Free, set-and-forget.

---

## Step 1 — Put the code on GitHub

1. Create a free account at https://github.com (if you don't have one).
2. Make a new **private** repo named `silver-tracker` (Don't add a README).
3. On your Mac, push this folder. In Terminal:

   ```bash
   cd "/Users/avyukktvuppalanchi/silver rate tracker"
   git init                       # already done if you see "Reinitialized"
   git add .
   git commit -m "Silver Tracker"
   git branch -M main
   git remote add origin https://github.com/<YOUR_USERNAME>/silver-tracker.git
   git push -u origin main
   ```

   ✅ Your secret files (`gemini_config.json`, `alert_config.json`) are in
   `.gitignore` and will **NOT** be uploaded. You'll set those as env vars below.

---

## Step 2 — Create the Render service

1. Sign up at https://render.com (use "Sign in with GitHub").
2. Click **New +** → **Web Service** → connect your `silver-tracker` repo.
3. Render auto-detects the `render.yaml` blueprint. Confirm:
   - **Runtime:** Python · **Plan:** Free
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `python app.py`
4. Click **Create Web Service**. First build takes ~2–3 min.

You'll get a public URL like `https://silver-tracker-xxxx.onrender.com` — that's
your platform, reachable from any device, anywhere.

---

## Step 3 — Add your secrets (env vars)

In the Render dashboard → your service → **Environment** → add these
(**Key** = **Value**). This replaces your local config files:

| Key | Value |
|---|---|
| `GEMINI_API_KEYS` | your Gemini keys, comma-separated: `key1,key2,key3` |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `465` |
| `SMTP_USER` | `avyukkt@gmail.com` |
| `SMTP_PASS` | your Gmail app password |
| `ALERT_FROM` | `avyukkt@gmail.com` |
| `ALERT_TO` | `vavyukkt@gmail.com` |
| `BUY_THRESHOLD` | `75` |
| `SELL_THRESHOLD` | `25` |

Click **Save** — Render redeploys automatically. Visit your URL to confirm the
dashboard loads.

---

## Step 4 — Keep it awake (so alerts run 24/7)

1. Sign up free at https://cron-job.org (or https://uptimerobot.com).
2. Create a job that requests your URL every **10 minutes**:
   - URL: `https://silver-tracker-xxxx.onrender.com/api/score`
   - Interval: every 10 minutes
3. Save. This keeps the app awake so the alert scheduler keeps running.

---

## Done ✅
- Public link works from any device, even with your Mac off.
- Alerts fire 24/7 (buy ≥ 75, sell ≤ 25) to your email/phone.
- To change anything later: edit code locally → `git push` → Render auto-redeploys.

### Notes
- Render free filesystem is ephemeral: the alert de-dupe state resets on each
  redeploy/restart, so right after a deploy you might get one alert that would
  otherwise have been on cooldown. Harmless.
- Free tier has limited monthly hours; the pinger keeps one small service well
  within them. If you ever outgrow it, Render's paid tier removes the sleep and
  you can drop the pinger.
