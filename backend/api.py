"""
Backend API for the Mainboard IPO widget.

Responsibilities (and only these — scraping lives in scraper/, UI lives in
frontend/):
  - Serve normalized JSON to the frontend.
  - Own the refresh schedule (so the frontend never scrapes directly).
  - Never invent data: if a live refresh fails, keep serving the last good
    cache and say how old it is.

Run with:  uvicorn api:app --reload --port 8000   (from inside backend/)
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

sys.path.append(str(Path(__file__).resolve().parent.parent / "scraper"))
import investorgain_scraper as scraper  # noqa: E402

load_dotenv()

GMP_REFRESH_SECONDS = int(os.getenv("GMP_REFRESH_SECONDS", "300"))          # 5 min
SUBSCRIPTION_REFRESH_SECONDS = int(os.getenv("SUBSCRIPTION_REFRESH_SECONDS", "300"))
METADATA_REFRESH_SECONDS = int(os.getenv("METADATA_REFRESH_SECONDS", "3600"))  # 1 hr

app = FastAPI(title="Mainboard IPO Widget API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ALLOWED_ORIGINS", "*").split(","),
    allow_methods=["GET"],
    allow_headers=["*"],
)

_last_refresh_ok: float | None = None
_last_refresh_error: str | None = None


def _refresh_loop():
    """Background thread: refreshes Open and Upcoming Mainboard IPO data on
    GMP_REFRESH_SECONDS. Deliberately conservative — sequential fetches,
    no parallel hammering of InvestorGain."""
    global _last_refresh_ok, _last_refresh_error
    while True:
        try:
            records = scraper.scrape_open_mainboard_ipos()
            scraper.save_cache(records, "open")
            _last_refresh_ok = time.time()
            _last_refresh_error = None
        except Exception as e:  # noqa: BLE001 — we want to survive any scrape failure
            _last_refresh_error = str(e)
            # Deliberately do NOT clear the existing cache — stale-but-real
            # data beats no data or fabricated data.

        try:
            upcoming = scraper.scrape_upcoming_mainboard_ipos()
            scraper.save_cache(upcoming, "upcoming")
        except Exception:
            pass  # Upcoming failures don't block Open, which is the priority tab

        try:
            closed = scraper.scrape_closed_mainboard_ipos()
            scraper.save_cache(closed, "closed")
        except Exception:
            pass  # Closed failures don't block Open either

        time.sleep(GMP_REFRESH_SECONDS)


@app.on_event("startup")
def start_background_refresh():
    if os.getenv("DISABLE_BACKGROUND_REFRESH") == "1":
        return  # handy for local testing without hitting the live site
    t = threading.Thread(target=_refresh_loop, daemon=True)
    t.start()


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "last_refresh_ok": _last_refresh_ok,
        "last_refresh_error": _last_refresh_error,
        "refresh_interval_seconds": GMP_REFRESH_SECONDS,
    }


@app.get("/api/debug/frontend")
def debug_frontend():
    """Temporary diagnostic: shows exactly what the deployed server sees on
    disk for the frontend folder, to debug a 404 without needing to find
    Render's log viewer. Safe to remove once the site is confirmed working."""
    frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
    repo_root = Path(__file__).resolve().parent.parent
    return {
        "backend_file_location": str(Path(__file__).resolve()),
        "computed_repo_root": str(repo_root),
        "repo_root_contents": sorted(p.name for p in repo_root.iterdir()) if repo_root.exists() else "REPO ROOT MISSING",
        "computed_frontend_dir": str(frontend_dir),
        "frontend_dir_exists": frontend_dir.exists(),
        "frontend_dir_contents": sorted(p.name for p in frontend_dir.iterdir()) if frontend_dir.exists() else "FRONTEND DIR MISSING",
        "index_html_exists": (frontend_dir / "index.html").exists(),
    }


@app.get("/api/debug/listing-performance-raw")
def debug_listing_performance_raw():
    """Temporary diagnostic: fetches InvestorGain's report 377 (GMP
    Performance Tracker) directly and returns the raw first row, so we can
    see the REAL field names their API uses instead of guessing. Safe to
    remove once listing-gain data is confirmed working."""
    import requests as _requests
    from datetime import datetime as _dt
    year = _dt.now().year
    url = f"https://webnodejs.investorgain.com/cloud/v2/report/data-read/377/1/8/{year}/all/0/all?year={year}"
    try:
        resp = _requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "Referer": "https://www.investorgain.com/",
                "Accept": "application/json",
            },
            timeout=20,
        )
        data = resp.json()
        rows = data.get("reportTableData", [])
        return {
            "url_fetched": url,
            "http_status": resp.status_code,
            "row_count": len(rows),
            "first_row_raw": rows[0] if rows else None,
            "all_field_names_in_first_row": sorted(rows[0].keys()) if rows else [],
        }
    except Exception as e:
        return {"url_fetched": url, "error": str(e)}


@app.get("/api/ipos/open")
def get_open_ipos():
    cache = scraper.load_cache("open")
    if not cache["records"]:
        raise HTTPException(
            status_code=503,
            detail="No IPO data available yet. First refresh may still be running, "
                   "or the scraper's selectors need updating — see scraper/investorgain_scraper.py",
        )
    return cache


@app.get("/api/ipos/upcoming")
def get_upcoming_ipos():
    cache = scraper.load_cache("upcoming")
    return cache  # empty is a valid state here, unlike /open


@app.get("/api/ipos/closed")
def get_closed_ipos():
    cache = scraper.load_cache("closed")
    return cache


@app.get("/api/ipos/{company_slug}")
def get_ipo_detail(company_slug: str):
    for key in ("open", "upcoming", "closed"):
        cache = scraper.load_cache(key)
        for r in cache["records"]:
            slug = r["company_name"].lower().replace(" ", "-")
            if slug == company_slug:
                return r
    raise HTTPException(status_code=404, detail="IPO not found in current cache")


# Serves the dashboard itself (frontend/) so a non-technical user only ever
# has to start ONE thing. Visit http://localhost:8000/app/index.html
_frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/app", StaticFiles(directory=str(_frontend_dir), html=True), name="frontend")
