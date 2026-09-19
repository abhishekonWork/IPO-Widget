# IPOAbhi — Invest se Pehle Observe

A mobile-first tracker for currently-open, upcoming, and recently-closed
Indian **Mainboard** IPOs — live GMP with up/down movement, full
subscription breakdown (QIB/SHNI/BHNI/NII/Retail/Total), registrar, price
band, and key dates. SME IPOs are always excluded. Data comes from
InvestorGain.com, refreshed automatically every 2 minutes.

Live at: `https://ipo-widget.onrender.com/app/index.html`

## What it does

- **GMP tracking** — current value, percent, and a live up/down/unchanged
  indicator based on real movement since the last check (not just "did a
  refresh happen"). Also shows each IPO's opening GMP and all-time
  high/low, which freeze once the IPO actually lists.
- **Subscription** — QIB, Small HNI, Big HNI, NII, Retail, and Total,
  shown as "Not Started" until real bidding data exists (never a fake 0x).
- **Registrar and price band** — fetched once per IPO and cached
  permanently, since neither ever changes after filing.
- **Closed tab retention** — a listed IPO stays visible for 10 days after
  InvestorGain stops actively tracking it, then quietly drops off, instead
  of vanishing or going stale the moment InvestorGain's own tracking window
  ends.
- **Installable as a home-screen app** (Add to Home Screen) — works fully
  offline for the app shell; live data always requires a connection.

## How it's built

```
IPO-Widget/
├── scraper/
│   ├── models.py                  # IPORecord / Subscription data shapes
│   └── investorgain_scraper.py    # reads InvestorGain's own JSON report
│                                   # API directly (no browser automation)
│                                   # — see the file's own header comments
│                                   # for the exact report IDs and fields
├── backend/
│   └── api.py                     # FastAPI server: runs the refresh loop,
│                                   # serves JSON, also serves the frontend
│                                   # at /app so one deployed service does
│                                   # everything
├── frontend/
│   ├── index.html / app.js / style.css   # the dashboard itself
│   ├── manifest.json / sw.js             # PWA / "Add to Home Screen"
├── data/                          # auto-created; local cache files (see
│                                   # note on GITHUB_TOKEN below)
├── requirements.txt
├── render.yaml                    # Render deploy config
├── .env.example
└── DEPLOY.md                      # step-by-step first-time deploy guide
```

**Why a direct API call instead of a headless browser?** InvestorGain's
own tables are filled in by a plain JSON endpoint
(`webnodejs.investorgain.com/cloud/v2/report/data-read/...`). Calling that
directly is far lighter than running a real browser (Playwright) server-side
— no Chromium download, faster refreshes, cheaper to host free.

**One refresh cycle, one fetch.** Each report (GMP, subscription,
performance) is fetched exactly once per 2-minute cycle and shared across
the Open/Upcoming/Closed tabs, rather than each tab re-fetching
independently.

## Required setup: GITHUB_TOKEN

GMP direction (up/down/unchanged) needs to remember each IPO's previous
value — including across deploys, and across the specific "compare to
yesterday's close" rule the direction indicator uses. Render's free tier
wipes local disk on every redeploy, so that memory is instead saved back
into this repo (`data/gmp_direction_state.json`) via GitHub's API.

To enable this:
1. Create a GitHub **fine-grained personal access token**, scoped to
   **only this repository**, with **Contents: Read and write** permission
   and nothing else.
2. Add it in Render's dashboard → your service → **Environment** tab, as
   `GITHUB_TOKEN`. Never commit a token into this repo's files.

Without it, the app still works fully — GMP direction just resets after
each redeploy instead of persisting.

## Running locally (for development only)

```bash
pip install -r requirements.txt
cp .env.example .env
cd backend
uvicorn api:app --reload --port 8000
```

Visit `http://localhost:8000/app/index.html`. Check
`http://localhost:8000/api/health` to confirm the refresh loop is running.

Diagnostic endpoints (`/api/debug/*`) are disabled by default in
production. Set `DEBUG_ENDPOINTS_ENABLED=1` locally, or temporarily in
Render's Environment tab, to use them.

## Known limitations

- **iOS**: "Add to Home Screen" works in Safari, but iOS PWAs have weaker
  background refresh than Android — data updates when the app is opened,
  not reliably while it's closed.
- **GMP is unofficial** — always labeled "Indicative"; never presented as
  a guaranteed listing price.
- **Render's free tier sleeps after ~15 min of inactivity**, adding a
  30–50s delay on the next visit. A free external cron ping (see
  `DEPLOY.md`) keeps it mostly awake.
- If InvestorGain changes their report structure, the scraper will need a
  small update — the `/api/debug/*` endpoints (see above) exist for
  exactly this kind of troubleshooting.
