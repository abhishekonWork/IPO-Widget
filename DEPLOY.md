# Get this on your phone — no laptop, no Wi-Fi matching, 100% free

This replaces the old "run it on your laptop and connect your phone to the
same Wi-Fi" approach. Once you finish this, your IPO tracker lives on the
internet permanently, at its own link, and you (or anyone you share the
link with) can open it on any phone, on any network, anytime.

Total cost: ₹0. No credit card needed anywhere in this guide.

## What you'll create (all free)

1. A GitHub account — free code storage that Render reads from.
2. A Render account — free hosting that runs your app 24/7.
3. (Optional but recommended) A UptimeRobot account — keeps the free
   Render app from "falling asleep" between visits.

---

## Step 1 — Put this code on GitHub

1. Go to [github.com](https://github.com) → **Sign up** (free).
2. Once logged in, click the **+** in the top-right → **New repository**.
3. Name it something like `ipo-widget`. Keep it **Public** (required for
   Render's free tier to read it). Click **Create repository**.
4. On the new repo's page, click **uploading an existing file**.
5. Drag in every file and folder from this project (`backend/`,
   `frontend/`, `scraper/`, `requirements.txt`, `render.yaml`, etc.) — you
   can drag whole folders.
6. Scroll down, click **Commit changes**.

## Step 2 — Deploy it on Render

1. Go to [render.com](https://render.com) → **Get Started** → sign up
   using **your GitHub account** (this lets Render see your repo).
2. On the Render dashboard, click **New +** → **Blueprint**.
3. Pick the `ipo-widget` repository you just created. Render will read
   the `render.yaml` file in this project automatically and pre-fill
   everything (it's already set up for you — free plan, correct start
   command, correct settings).
4. Click **Apply** / **Create**. Render will build and start it — this
   takes a few minutes the first time.
5. When it's done, Render shows you a URL like:
   `https://ipo-widget-xxxx.onrender.com`

## Step 3 — Check it's actually working

Visit, in your browser:
- `https://ipo-widget-xxxx.onrender.com/api/health` → should show
  `"status":"ok"`. If `last_refresh_error` shows a message instead of
  `null`, the scraper needs a small fix — see **Troubleshooting** below.
- `https://ipo-widget-xxxx.onrender.com/app/index.html` → your actual
  dashboard, showing live IPO data.

## Step 4 — Add it to your phone's home screen

1. Open `https://ipo-widget-xxxx.onrender.com/app/index.html` in Chrome
   (Android) or Safari (iPhone) — on your phone, on **any** network
   (mobile data, any Wi-Fi, doesn't matter anymore).
2. Tap the menu (⋮ on Android, share icon on iPhone) → **Add to Home
   Screen**.
3. It now sits on your home screen as its own icon and opens full-screen,
   like a real app.
4. **To share it with someone else:** just send them the same
   `.../app/index.html` link — they do the same "Add to Home Screen" step
   on their own phone. Nothing to install, nothing to configure.

## Step 5 — Keep it from "falling asleep" (optional, still free)

Render's free tier pauses your app after 15 minutes with no visitors, and
takes ~30-50 seconds to wake back up on the next visit. To avoid that
first-load delay:

1. Go to [uptimerobot.com](https://uptimerobot.com) → sign up free.
2. **Add New Monitor** → type **HTTP(s)** → paste your
   `.../api/health` URL → set check interval to **5 minutes** → save.
3. UptimeRobot will now "ping" your app every 5 minutes, keeping it awake
   almost all the time, for free.

---

## Troubleshooting

**`last_refresh_error` shows something in `/api/health`:**
InvestorGain occasionally tweaks their internal API. Send me (Claude) the
exact error text and I can update `scraper/investorgain_scraper.py` to
match — this is a small, contained fix, not a rebuild.

**The dashboard loads but shows no IPOs:**
Check `/api/ipos/open` directly in your browser. If it's an empty list,
it may genuinely mean no Mainboard IPOs are open right now (normal) — try
`/api/ipos/upcoming` to confirm the pipeline itself is working.

**I want to change how often it refreshes:**
On Render, go to your service → **Environment** tab → edit
`GMP_REFRESH_SECONDS`. Lower = more frequent, but be considerate of
InvestorGain's servers (5 minutes, the default, is plenty for tracking
GMP movement).
