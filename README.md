# Mainboard IPOs Live — Widget

A mobile-first dashboard that shows currently-open (and upcoming/closed)
Indian **Mainboard** IPOs — GMP, QIB/NII/Retail/Total subscription, issue
size, and key dates — pulled from InvestorGain.com. SME IPOs are always
excluded.

**Want this on your phone, working anywhere, for free? → See
`START-HERE.md` / `DEPLOY.md`.** Everything below is technical background
for anyone curious how it's built.

## Current status

- **Data source confirmed and verified against real captured traffic**
  (see `network_capture.txt` used during development). The scraper reads
  InvestorGain's own internal JSON API directly — no browser automation
  needed.
- Backend, frontend, caching, refresh logic, and PWA install are all
  complete and ready to use.

## How the pieces fit together

```
ipo-widget/
├── scraper/
│   ├── models.py                  # the normalized data shape
│   └── investorgain_scraper.py    # calls InvestorGain's own JSON API
│                                   # directly (see file header for the
│                                   # exact endpoints and field mapping)
├── backend/
│   └── api.py                     # FastAPI server: refreshes the scraper
│                                   # on a timer, serves JSON, also serves
│                                   # the frontend at /app so ONE deployed
│                                   # service does everything
├── frontend/
│   ├── index.html / app.js / style.css   # the dashboard itself
│   ├── manifest.json / sw.js             # what makes it "Add to Home
│                                            Screen"-able
├── data/
│   └── ipo_cache.json             # last-known-good data (auto-created)
├── requirements.txt
├── render.yaml                    # one-click free deploy config for Render
├── .env.example
├── DEPLOY.md                      # step-by-step: get this on your phone, free
└── README.md   ← you are here
```

**Why a direct API call instead of a headless browser?**
InvestorGain's GMP/Subscription tables are filled in by the page's own
JavaScript, which calls a plain JSON endpoint
(`webnodejs.investorgain.com/cloud/v2/report/data-read/...`). Earlier
versions of this project used Playwright (a real, invisible Chrome
browser) to work around that — calling the same JSON endpoint directly is
simpler, faster, and much lighter to deploy (no Chromium download needed).

**Why a PWA instead of a native Android widget?**
A true Android home-screen widget (the little live box) needs to be built
as a compiled Android app (Kotlin + Android Studio + an APK) — a
different toolchain entirely. A PWA gets you full-screen, one-tap access
from your home screen with almost none of that overhead — you "Add to
Home Screen" from Chrome and it behaves like an app icon. If you want the
true native widget later, the backend API here is exactly what it would
call — nothing would need to be rebuilt, just a new Android front-end
added.

## Running it (for local testing/development only)

Most people should just follow `DEPLOY.md` instead — it gets you a
permanent phone-ready link with no local setup. This section is for
poking at the code itself.

```bash
pip install -r requirements.txt
cp .env.example .env
cd backend
uvicorn api:app --reload --port 8000
```

Then visit `http://localhost:8000/app/index.html`. Check
`http://localhost:8000/api/health` to confirm the background refresh is
working.

## Changing the refresh interval

Edit `GMP_REFRESH_SECONDS` / `SUBSCRIPTION_REFRESH_SECONDS` in `.env`
(locally) or in the Render dashboard's Environment tab (deployed), and
also update `REFRESH_MS` near the top of `frontend/app.js` to match (it
controls how often the *frontend* re-polls the backend, and is used to
compute the "Next Update" time shown on screen).

## How the widget gets "updated"

There's no push mechanism — the frontend polls the backend every
`REFRESH_MS`, and the backend independently refreshes its own cache from
InvestorGain every `GMP_REFRESH_SECONDS` on a background timer. They
don't have to be the same interval, but keeping them close avoids the
frontend polling for data that hasn't actually changed yet.

## Known limitations

- **iOS**: "Add to Home Screen" also works in Safari, but iOS PWAs have
  weaker background-refresh support than Android — the app will refresh
  when opened, but won't reliably update while closed.
- **GMP is unofficial** — always labeled "Indicative" per the spec; this
  app never claims it as a guaranteed listing price.
- If InvestorGain changes their internal API shape, the scraper will need
  a small update — see the `--inspect` / `--inspect-subscription` flags
  in `investorgain_scraper.py` to capture the new shape for a fix.
- **Render's free tier sleeps after 15 min of inactivity** and takes
  ~30-50s to wake up on the next visit — `DEPLOY.md` includes an optional
  free step (UptimeRobot) to minimize this.

## Testing checklist

- [ ] Only Mainboard IPOs appear; SME IPOs are excluded
- [ ] GMP displays, or "Not Available" if missing — never a guessed number
- [ ] QIB/NII/Retail/Total subscription display correctly, or "Not Started"
- [ ] Issue size, Open/Close/BOA/Listing dates display correctly
- [ ] Upcoming tab shows IPOs with subscription "Not Started"
- [ ] A field InvestorGain doesn't have doesn't break the card layout
- [ ] Pulling/refreshing updates the "Last Updated" timestamp
- [ ] Losing connectivity shows the stale-data warning, not fake fresh data
- [ ] Layout is comfortable on an actual phone screen
