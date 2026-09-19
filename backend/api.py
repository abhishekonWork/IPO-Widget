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
import traceback
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

sys.path.append(str(Path(__file__).resolve().parent.parent / "scraper"))
import investorgain_scraper as scraper  # noqa: E402

load_dotenv()

GMP_REFRESH_SECONDS = int(os.getenv("GMP_REFRESH_SECONDS", "120"))          # 2 min
SUBSCRIPTION_REFRESH_SECONDS = int(os.getenv("SUBSCRIPTION_REFRESH_SECONDS", "120"))
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
_last_upcoming_error: str | None = None
_last_closed_error: str | None = None


def _refresh_loop():
    """Background thread: refreshes Open and Upcoming Mainboard IPO data on
    GMP_REFRESH_SECONDS. Deliberately conservative — sequential fetches,
    no parallel hammering of InvestorGain."""
    global _last_refresh_ok, _last_refresh_error, _last_upcoming_error, _last_closed_error
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
            _last_upcoming_error = None
        except Exception as e:
            # Full traceback (not just str(e)) -- captured here since this
            # is the ONLY place we can see it; Render's own logs aren't
            # easily accessible to the site owner, and a bare exception
            # message often isn't enough to diagnose the real cause.
            _last_upcoming_error = traceback.format_exc()
            print(f"WARNING: upcoming-tab refresh failed: {e}", file=sys.stderr)

        try:
            closed = scraper.scrape_closed_mainboard_ipos()
            scraper.save_cache(closed, "closed")
            _last_closed_error = None
        except Exception as e:
            # Previously silently swallowed (and even after adding a print,
            # that only went to Render's server logs, invisible to the site
            # owner) -- this hid real failures and caused "closed" to show
            # nothing with no visible reason why. Now captured with full
            # traceback and exposed via /api/health.
            _last_closed_error = traceback.format_exc()
            print(f"WARNING: closed-tab refresh failed: {e}", file=sys.stderr)

        time.sleep(GMP_REFRESH_SECONDS)


@app.on_event("startup")
def start_background_refresh():
    if os.getenv("DISABLE_BACKGROUND_REFRESH") == "1":
        return  # handy for local testing without hitting the live site
    t = threading.Thread(target=_refresh_loop, daemon=True)
    t.start()


@app.get("/api/health")
def health(response: Response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    # Per-tab freshness, not just the Open tab -- this is what would have
    # caught "closed" silently going stale while "open" looked fine.
    tabs = {}
    for key in ("open", "upcoming", "closed"):
        cache = scraper.load_cache(key)
        tabs[key] = {
            "attempted_at": cache.get("attempted_at") or cache.get("fetched_at"),
            "data_changed_at": cache.get("data_changed_at") or cache.get("fetched_at"),
            "record_count": len(cache.get("records", [])),
        }
    return {
        "status": "ok",
        "last_refresh_ok": _last_refresh_ok,
        "last_refresh_error": _last_refresh_error,
        "last_upcoming_error": _last_upcoming_error,
        "last_closed_error": _last_closed_error,
        "refresh_interval_seconds": GMP_REFRESH_SECONDS,
        "tabs": tabs,
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


@app.get("/api/debug/subscription-raw")
def debug_subscription_raw():
    """Temporary diagnostic: fetches InvestorGain's report 333 (IPO Live
    Subscription) directly and returns the raw first row, so we can see
    the REAL field names for SHNI/BHNI before writing any parsing logic
    that guesses at them. Same pattern used successfully for report 377's
    listing-performance fields. Safe to remove once SHNI/BHNI are
    confirmed working correctly."""
    import requests as _requests
    from datetime import datetime as _dt
    year = _dt.now().year
    fy_start = year if _dt.now().month >= 4 else year - 1
    fiscal_year = f"{fy_start}-{str(fy_start + 1)[-2:]}"
    url = f"https://webnodejs.investorgain.com/cloud/v2/report/data-read/333/1/8/{year}/{fiscal_year}/0/all"
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


@app.get("/api/debug/gmp-report-raw")
def debug_gmp_report_raw(company: str = ""):
    """Diagnostic: fetches InvestorGain's report 331 (Live GMP) directly --
    the SAME report our actual scraper reads GMP from -- and returns the
    raw row(s) so we can see EXACTLY what InvestorGain's live table says
    right now, unprocessed by any of our own parsing logic. This is the
    ground truth to compare our /api/ipos/open output against when a GMP
    value looks wrong.

    Pass ?company=Manika to filter to rows whose Name field contains that
    text (case-insensitive substring match); omit to see all rows."""
    import requests as _requests
    from datetime import datetime as _dt
    year = _dt.now().year
    url = f"https://webnodejs.investorgain.com/cloud/v2/report/data-read/331/1/8/{year}/{year}-{str(year+1)[-2:]}/0/all"
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
        if company:
            rows = [r for r in rows if company.lower() in (r.get("Name") or "").lower()]
        return {
            "url_fetched": url,
            "http_status": resp.status_code,
            "server_response_headers_date": resp.headers.get("Date"),  # when InvestorGain's server generated this response
            "row_count": len(rows),
            "matching_rows": rows,
        }
    except Exception as e:
        return {"url_fetched": url, "error": str(e)}


@app.get("/api/debug/gmp-page-raw")
def debug_gmp_page_raw(company: str = "Hero"):
    """Diagnostic: fetches the /gmp/{slug}/{id}/ page (the "Day-wise GMP
    Trend" page the site owner has been comparing against -- a DIFFERENT
    page from /ipo/{slug}/{id}/, which we already use for Registrar/Price
    Band) and dumps its structure so we can see EXACTLY where the live GMP
    figure lives in the HTML before writing any parsing logic. Same
    careful, evidence-first approach used for every other field on this
    site. Safe to remove once this is confirmed working.

    Pass ?company=Hero (or any substring) to pick which currently-tracked
    IPO to test against."""
    try:
        raw_rows = scraper.fetch_report(scraper.GMP_REPORT_ID)
    except Exception as e:
        return {"error": f"Could not fetch raw GMP report: {e}"}

    matching_row = None
    matched_name = None
    for row in raw_rows:
        name, _cat, _status = scraper._parse_name_cell(row.get("Name", ""))
        if name and company.lower() in name.lower():
            matching_row = row
            matched_name = name
            break
    if not matching_row:
        return {"error": f"No IPO matching '{company}' found in the current live report"}

    slug, ipo_id = scraper._extract_url_slug_and_id(matching_row)
    if not slug or not ipo_id:
        return {"error": f"Could not extract slug/id for {matched_name}", "raw_row": matching_row}

    url = f"https://www.investorgain.com/gmp/{slug}/{ipo_id}/"
    try:
        resp = scraper.requests.get(url, headers=scraper.HEADERS, timeout=scraper.REQUEST_TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        return {"error": f"Could not fetch GMP page: {e}", "url_tried": url}

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(resp.text, "html.parser")

    # Any two-column table rows (same structure as the /ipo/ page, in case
    # this page ALSO has one)
    table_rows = []
    for row_el in soup.find_all("tr"):
        cells = row_el.find_all(["td", "th"])
        if len(cells) == 2:
            label = scraper._strip_tags(cells[0].get_text()).strip()
            value = scraper._strip_tags(cells[1].get_text()).strip()
            table_rows.append({"label": label, "value": value})

    # Any element whose class name hints at GMP/price/live -- likely where
    # the stat-card figure actually lives, since this page probably isn't
    # a simple table for its headline number.
    candidate_elements = []
    for el in soup.find_all(class_=True):
        classes = " ".join(el.get("class", [])).lower()
        if any(hint in classes for hint in ["gmp", "live", "price", "stat", "card"]):
            text = scraper._strip_tags(el.get_text()).strip()
            if text and len(text) < 200:  # skip huge wrapper containers, keep only compact leaf-ish text
                candidate_elements.append({"tag": el.name, "class": el.get("class"), "text": text})

    # Raw text search for "GMP" as a last resort -- shows surrounding
    # context even if it's in a paragraph rather than a labeled element.
    full_text = scraper._strip_tags(resp.text)
    gmp_mentions = []
    idx = 0
    lower_text = full_text.lower()
    while True:
        idx = lower_text.find("gmp", idx)
        if idx == -1 or len(gmp_mentions) >= 8:
            break
        gmp_mentions.append(full_text[max(0, idx - 40):idx + 60].strip())
        idx += 3

    return {
        "company_tested": matched_name,
        "url_fetched": url,
        "two_column_table_rows": table_rows,
        "candidate_gmp_related_elements": candidate_elements[:30],  # cap for readability
        "raw_text_context_around_gmp_mentions": gmp_mentions,
    }


@app.get("/api/debug/price-band-raw")
def debug_price_band_raw():
    """Diagnostic: fetches ONE real, currently-open IPO's individual detail
    page (same page type already used for Registrar) and returns the raw
    table rows so we can see the EXACT label InvestorGain uses for price
    band before writing any parsing logic that guesses at it -- same
    careful approach used for report 377 and the SHNI/BHNI fields. Safe to
    remove once price band is confirmed working correctly."""
    try:
        open_records = scraper.scrape_open_mainboard_ipos()
    except Exception as e:
        return {"error": f"Could not fetch open IPOs to test against: {e}"}
    if not open_records:
        return {"error": "No open IPOs available right now to test against"}

    # Re-fetch the raw GMP report rows so we can find this IPO's exact
    # URL slug/id, same lookup registrar enrichment already does.
    try:
        raw_rows = scraper.fetch_report(scraper.GMP_REPORT_ID)
    except Exception as e:
        return {"error": f"Could not fetch raw GMP report: {e}"}

    target_name = open_records[0].company_name
    matching_row = None
    for row in raw_rows:
        name, _cat, _status = scraper._parse_name_cell(row.get("Name", ""))
        if name == target_name:
            matching_row = row
            break
    if not matching_row:
        return {"error": f"Could not find raw row for {target_name}"}

    slug, ipo_id = scraper._extract_url_slug_and_id(matching_row)
    if not slug or not ipo_id:
        return {"error": f"Could not extract slug/id for {target_name}", "raw_row": matching_row}

    url = f"https://www.investorgain.com/ipo/{slug}/{ipo_id}/"
    try:
        resp = scraper.requests.get(url, headers=scraper.HEADERS, timeout=scraper.REQUEST_TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        return {"error": f"Could not fetch detail page: {e}", "url_tried": url}

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(resp.text, "html.parser")
    table_rows = []
    for row_el in soup.find_all("tr"):
        cells = row_el.find_all(["td", "th"])
        if len(cells) == 2:
            label = scraper._strip_tags(cells[0].get_text()).strip()
            value = scraper._strip_tags(cells[1].get_text()).strip()
            table_rows.append({"label": label, "value": value})

    return {
        "company_tested": target_name,
        "url_fetched": url,
        "all_two_column_table_rows": table_rows,
    }


@app.get("/api/debug/github-persistence")
def debug_github_persistence():
    """Diagnostic: confirms whether GMP-direction memory is actually
    surviving redeploys via GitHub, or silently falling back to
    local-disk-only (which resets every deploy). Checks:
      1. Is GITHUB_TOKEN even set?
      2. Can we successfully read gmp_direction_state.json from the repo?
      3. What's actually in it right now?
    Safe to leave in permanently -- it never exposes the token itself,
    only whether one is configured."""
    token_configured = bool(scraper.GITHUB_TOKEN)
    result = {
        "github_token_configured": token_configured,
        "github_repo": scraper.GITHUB_REPO,
        "github_branch": scraper.GITHUB_BRANCH,
    }
    if not token_configured:
        result["status"] = "NOT CONFIGURED -- GMP direction memory will reset on every redeploy. Set GITHUB_TOKEN in Render's Environment tab."
        return result

    state = scraper._github_get_file(scraper.GMP_DIRECTION_STATE_GITHUB_PATH)
    if state is None:
        result["status"] = "TOKEN SET, but could not read state file yet (may not exist until the first Open-tab refresh writes it, or the token/repo/permissions are misconfigured -- check server logs for the exact WARNING)"
        result["companies_tracked"] = 0
    else:
        result["status"] = "WORKING -- state is being read from GitHub successfully"
        result["companies_tracked"] = len(state)
        result["sample"] = dict(list(state.items())[:3])  # first few entries, not the whole thing
    return result


@app.get("/api/ipos/open")
def get_open_ipos(response: Response):
    # Explicit no-cache headers on this response itself -- this is the
    # authoritative fix: it tells every layer (browser HTTP cache, any
    # carrier/CDN proxy, the service worker) not to reuse this response,
    # rather than relying only on the frontend's request-side settings
    # which not every client/network respects equally. This is what was
    # letting two phones disagree on "X mins ago" at the same moment even
    # though the backend itself was current on both.
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    cache = scraper.load_cache("open")
    if not cache["records"]:
        raise HTTPException(
            status_code=503,
            detail="No IPO data available yet. First refresh may still be running, "
                   "or the scraper's selectors need updating — see scraper/investorgain_scraper.py",
        )
    return cache


@app.get("/api/ipos/upcoming")
def get_upcoming_ipos(response: Response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    cache = scraper.load_cache("upcoming")
    return cache  # empty is a valid state here, unlike /open


@app.get("/api/ipos/closed")
def get_closed_ipos(response: Response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
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
