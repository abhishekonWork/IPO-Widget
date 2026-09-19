"""
InvestorGain Mainboard IPO fetcher.

STATUS: rewritten from a captured real browser network trace
(network_capture.txt, taken while loading investorgain.com's GMP-live and
subscription-live report pages). That trace showed the "live table" isn't
scraped from rendered HTML at all -- the page's own JavaScript calls a
plain JSON API to fill it in:

  https://webnodejs.investorgain.com/cloud/v2/report/data-read/{report_id}/1/8/{year}/{fiscal_year}/0/all

  report_id 331 = "IPO GMP Live"          -> GMP, price, size, dates, total sub
  report_id 333 = "IPO Live Subscription" -> QIB/SHNI/BHNI/NII/RII breakdown

Both responses carry a `~IPO_Category` field of exactly "IPO" (mainboard)
or "SME" -- an explicit, reliable flag, not a text guess. Company name is
embedded as an HTML fragment in the "Name" field (e.g.
'<a ...>ESDS Software Solution</a> <span class="badge ...">IPO</span>
<span class="badge ...">O</span>'); the status badge ("O"/"U"/"C"/"L") is
inside that same fragment.

WHY NOT PLAYWRIGHT ANYMORE: hitting this JSON endpoint directly means no
headless-Chromium download, far less memory, and a faster/cheaper deploy
(fits comfortably on a free hosting tier). If InvestorGain ever changes
this internal API, `--inspect` below saves the raw response so it can be
diffed against the shapes documented here.

NOTE ON DATES: the GMP-report gives Open/Close/BoA/Listing as "28-Aug" (no
year). This code assumes the current calendar year, which is correct
essentially all year except right at a Dec -> Jan boundary.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Optional

import requests

from models import IPORecord, Subscription, now_iso

BASE = "https://webnodejs.investorgain.com/cloud/v2/report/data-read"
GMP_REPORT_ID = 331
SUBSCRIPTION_REPORT_ID = 333
PERFORMANCE_REPORT_ID = 377  # "IPO GMP Performance Tracker" -- actual listing price vs issue price

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.investorgain.com/",
    "Accept": "application/json",
}
REQUEST_TIMEOUT = 20  # seconds

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
CACHE_FILE = DATA_DIR / "ipo_cache.json"
REGISTRAR_CACHE_FILE = DATA_DIR / "registrar_cache.json"
GMP_DIRECTION_STATE_FILE = DATA_DIR / "gmp_direction_state.json"

# --- GitHub-backed persistence for GMP direction memory --------------------
# Render's free tier wipes local disk on every redeploy, which was silently
# breaking the "unchanged since yesterday's close" rule (needs memory to
# survive across days/deploys -- see the 2026-09-12 conversation this was
# built from). Instead of local-disk-only storage, this state is also
# pushed to a JSON file in this same GitHub repo via GitHub's Contents API,
# which is permanent free storage. Requires a GITHUB_TOKEN environment
# variable (a fine-grained personal access token scoped to ONLY this repo,
# with "Contents: Read and write" permission -- nothing else) set in
# Render's dashboard, NOT committed to the repo itself.
#
# If GITHUB_TOKEN isn't set, everything still works exactly as before
# (local-disk-only, resets on redeploy) -- this is a pure enhancement, not
# a hard requirement, so a missing/misconfigured token never breaks the
# core app.
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO", "abhishekonWork/IPO-Widget")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GMP_DIRECTION_STATE_GITHUB_PATH = "data/gmp_direction_state.json"
GITHUB_API_BASE = "https://api.github.com"
_github_file_sha_cache: dict[str, str] = {}  # path -> last-known sha, avoids a GET before every PUT


def _github_get_file(repo_path: str) -> Optional[dict]:
    """Reads and JSON-decodes a file from the repo via GitHub's Contents
    API. Returns None (never raises) if GITHUB_TOKEN isn't set, the file
    doesn't exist yet, or any request fails -- callers should treat None
    exactly like "no state file existed yet", never as a hard error."""
    if not GITHUB_TOKEN:
        return None
    url = f"{GITHUB_API_BASE}/repos/{GITHUB_REPO}/contents/{repo_path}?ref={GITHUB_BRANCH}"
    try:
        resp = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
            },
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 404:
            return None  # file doesn't exist yet -- not an error, just "no memory yet"
        resp.raise_for_status()
        payload = resp.json()
        _github_file_sha_cache[repo_path] = payload["sha"]  # needed for the next PUT
        content_b64 = payload["content"]
        decoded = base64.b64decode(content_b64).decode("utf-8")
        return json.loads(decoded)
    except Exception as e:
        print(f"WARNING: GitHub state read failed for {repo_path}: {e}", file=sys.stderr)
        return None


def _github_put_file(repo_path: str, data: dict, commit_message: str) -> bool:
    """Writes (creates or updates) a JSON file in the repo via GitHub's
    Contents API. Returns True/False, never raises -- a failed write here
    should never take down the actual IPO-data refresh, since the local
    disk copy is always written first as a safety net (see _save_gmp_direction_state)."""
    if not GITHUB_TOKEN:
        return False
    url = f"{GITHUB_API_BASE}/repos/{GITHUB_REPO}/contents/{repo_path}"
    content_b64 = base64.b64encode(json.dumps(data, indent=2).encode("utf-8")).decode("ascii")
    body = {
        "message": commit_message,
        "content": content_b64,
        "branch": GITHUB_BRANCH,
    }
    sha = _github_file_sha_cache.get(repo_path)
    if sha:
        body["sha"] = sha  # required by GitHub's API when updating an existing file
    try:
        resp = requests.put(
            url,
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
            },
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 409 or (resp.status_code == 422 and sha):
            # Our cached sha is stale (someone/something else wrote the
            # file since our last GET) -- fetch the current sha once and
            # retry exactly once, rather than looping or failing silently.
            fresh = _github_get_file(repo_path)
            body["sha"] = _github_file_sha_cache.get(repo_path)
            resp = requests.put(
                url,
                headers={
                    "Authorization": f"Bearer {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github+json",
                },
                json=body,
                timeout=REQUEST_TIMEOUT,
            )
        resp.raise_for_status()
        _github_file_sha_cache[repo_path] = resp.json()["content"]["sha"]
        return True
    except Exception as e:
        print(f"WARNING: GitHub state write failed for {repo_path}: {e}", file=sys.stderr)
        return False

STATUS_MAP = {"O": "open", "U": "upcoming", "C": "closed", "CT": "closed", "L": "listed"}

NAME_LINK_RE = re.compile(r"<a[^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
BADGE_RE = re.compile(r'<span[^>]*class="[^"]*badge[^"]*"[^>]*>(.*?)</span>', re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")


def _report_url(report_id: int) -> str:
    year = datetime.now().year
    # Matches the fiscal-year segment InvestorGain's own front-end sends
    # (e.g. "2026-27"). Indian FY runs Apr-Mar; before April, the FY
    # started the previous calendar year.
    fy_start = year if datetime.now().month >= 4 else year - 1
    fiscal_year = f"{fy_start}-{str(fy_start + 1)[-2:]}"
    return f"{BASE}/{report_id}/1/8/{year}/{fiscal_year}/0/all"


def _strip_tags(text: str) -> str:
    return unescape(TAG_RE.sub("", text)).strip()


def _first_number(text: str) -> Optional[float]:
    if not text:
        return None
    # Includes an optional leading "-" so negative GMP / negative listing
    # gain (a real, meaningful value -- grey market expects a listing
    # BELOW issue price) isn't silently turned positive or dropped.
    m = re.search(r"-?[\d,]+\.?\d*", text.replace(",", ""))
    return float(m.group().replace(",", "")) if m else None


def _parse_date(day_month: Optional[str]) -> Optional[str]:
    """'28-Aug' -> '2026-08-28'. API gives no year; assumes current year.
    This can be wrong for an IPO whose open date is in December and
    close/BoA/listing date is in January -- both would get stamped with
    the SAME year, making close appear to come before open. See
    _roll_year_if_before_open, which corrects exactly this case; it's
    applied by the caller (using the open date as an anchor), not here,
    since this function only ever sees one date at a time and has no way
    to know which other date in the same IPO's timeline it should be
    compared against."""
    if not day_month:
        return None
    day_month = day_month.strip()
    try:
        dt = datetime.strptime(f"{day_month}-{datetime.now().year}", "%d-%b-%Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return day_month or None  # fall back to raw text rather than losing the data


def _roll_year_if_before_open(date_iso: Optional[str], open_date_iso: Optional[str]) -> Optional[str]:
    """Fixes the December-open/January-close year-guessing bug: if
    date_iso's MONTH is earlier than open_date_iso's month (e.g. open is
    December, close/BoA/listing is January), the later date must actually
    fall in the FOLLOWING year -- an IPO's timeline only ever moves
    forward (open -> close -> BoA -> listing), never backward.

    IMPORTANT: only call this on a date that came from _parse_date's
    year-GUESSING fallback. InvestorGain's own pre-formatted ~Srt_Open /
    ~Srt_Close / ~Srt_BoA_Dt / ~Str_Listing fields already carry the
    correct year and must NEVER be passed through this correction -- doing
    so would roll an already-correct January date forward by an extra,
    wrong year. See the call site in the row-parsing loop below, which
    only applies this when the corresponding ~Srt_* field was absent."""
    if not date_iso or not open_date_iso:
        return date_iso
    try:
        d = datetime.strptime(date_iso, "%Y-%m-%d")
        o = datetime.strptime(open_date_iso, "%Y-%m-%d")
    except ValueError:
        return date_iso  # not a clean ISO date (e.g. raw fallback text) -- leave it alone rather than guess further
    if d.month < o.month:
        d = d.replace(year=d.year + 1)
        return d.strftime("%Y-%m-%d")
    return date_iso


def _parse_name_cell(raw_html: str) -> tuple[Optional[str], str, str]:
    """Returns (company_name, ipo_category_badge_text, status_badge_text).

    The API's "Name" field is an HTML fragment: a link with the company
    name, then 1-2 badge spans (category, then open/upcoming/closed status).
    """
    link_match = NAME_LINK_RE.search(raw_html)
    company_name = _strip_tags(link_match.group(1)) if link_match else None

    badges = [_strip_tags(b) for b in BADGE_RE.findall(raw_html)]
    category_text = badges[0] if len(badges) > 0 else ""
    status_text = badges[1] if len(badges) > 1 else ""
    return company_name, category_text, status_text


def fetch_report(report_id: int) -> list[dict]:
    """Fetches one InvestorGain report and returns its raw row dicts
    (still HTML-fragment-laden -- not yet normalized into IPORecord)."""
    resp = requests.get(_report_url(report_id), headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    return payload.get("reportTableData", [])


def _is_mainboard(category_text: str) -> bool:
    # Confirmed via captured data: "~IPO_Category" / the visible badge is
    # exactly "IPO" for mainboard and "SME" for SME rows.
    return "SME" not in category_text.upper()


def scrape_open_mainboard_ipos() -> list[IPORecord]:
    return _scrape_gmp_report(status_filter="open")


def scrape_upcoming_mainboard_ipos() -> list[IPORecord]:
    return _scrape_gmp_report(status_filter="upcoming")


def _derive_status(open_d: Optional[str], close_d: Optional[str], site_status: str) -> str:
    """Determines Open/Upcoming/Closed from actual dates rather than trusting
    InvestorGain's own status flag alone -- observed in practice to lag or
    stay stuck on 'Open' well after an IPO's close/listing date has passed.
    Falls back to the site's flag only when we have no usable dates at all."""
    today = datetime.now().date()
    try:
        open_dt = datetime.strptime(open_d, "%Y-%m-%d").date() if open_d else None
        close_dt = datetime.strptime(close_d, "%Y-%m-%d").date() if close_d else None
    except ValueError:
        open_dt = close_dt = None

    if close_dt is not None:
        if today > close_dt:
            return "closed"
        if open_dt is not None and today < open_dt:
            return "upcoming"
        if open_dt is not None and open_dt <= today <= close_dt:
            return "open"
        # Only a close date, no open date -- still safe to say open if not yet closed
        if today <= close_dt:
            return "open"

    return STATUS_MAP.get(site_status, "unknown")


CLOSED_IPO_ARCHIVE_FILE = DATA_DIR / "closed_ipo_archive.json"
CLOSED_IPO_RETENTION_DAYS = 10  # keep a listed IPO on the Closed tab for this many days after listing, even after InvestorGain's own live report stops including it


def _load_closed_ipo_archive() -> dict:
    if not CLOSED_IPO_ARCHIVE_FILE.exists():
        return {}
    try:
        return json.loads(CLOSED_IPO_ARCHIVE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_closed_ipo_archive(archive: dict) -> None:
    try:
        CLOSED_IPO_ARCHIVE_FILE.write_text(json.dumps(archive, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"WARNING: could not save closed IPO archive: {e}", file=sys.stderr)


def _merge_closed_with_archive(live_closed: list[IPORecord]) -> list[IPORecord]:
    """Extracted from scrape_closed_mainboard_ipos so both the standalone
    entry point and the single-fetch orchestrator (fetch_all_mainboard_data)
    can reuse the exact same archive-merge logic without duplicating it.
    See scrape_closed_mainboard_ipos's docstring for why this archive
    exists (InvestorGain's live report drops listed IPOs after some days;
    we keep showing them for CLOSED_IPO_RETENTION_DAYS regardless)."""
    archive = _load_closed_ipo_archive()
    today = datetime.now().date()

    # Update the archive with everything InvestorGain currently shows as closed.
    for rec in live_closed:
        archive[rec.company_name] = {
            "record": rec.to_dict(),
            "last_seen_date": today.isoformat(),
        }

    # Prune anything too old, and build the final list: live data takes
    # priority (it's fresher); archived-only entries fill in anything
    # InvestorGain has since dropped, as long as they're within the
    # retention window.
    live_names = {r.company_name for r in live_closed}
    pruned_archive = {}
    archived_only_records = []
    for name, entry in archive.items():
        try:
            last_seen = datetime.fromisoformat(entry["last_seen_date"]).date()
        except (ValueError, KeyError):
            continue  # malformed entry -- drop it rather than risk keeping bad data forever
        age_days = (today - last_seen).days
        if age_days > CLOSED_IPO_RETENTION_DAYS:
            continue  # past retention window -- quietly retire it, as requested
        pruned_archive[name] = entry
        if name not in live_names:
            # InvestorGain no longer lists it live, but it's still within
            # our retention window -- reconstruct an IPORecord from the
            # archived dict so it still shows up on the Closed tab.
            try:
                archived_only_records.append(_ipo_record_from_dict(entry["record"]))
            except Exception as e:
                print(f"WARNING: could not reconstruct archived closed IPO {name}: {e}", file=sys.stderr)

    _save_closed_ipo_archive(pruned_archive)
    return live_closed + archived_only_records


def scrape_closed_mainboard_ipos() -> list[IPORecord]:
    """Covers both 'closed, not yet listed' and 'listed' rows.

    Standalone entry point (CLI script, diagnostic endpoints) -- does its
    own full fetch when called alone. For the live backend's refresh loop,
    which needs Open/Upcoming/Closed together every cycle, use
    fetch_all_mainboard_data() instead -- see its docstring.

    IMPORTANT FIX (2026-09-16): InvestorGain's own live report (331) only
    keeps actively-tracked IPOs -- once they stop tracking a listed IPO's
    GMP (which happens some days after listing), it VANISHES from their
    report entirely, and previously this function would silently lose it
    too, which could make the Closed tab show stale/blank data with no
    graceful fallback ("never updated" bug reported by the site owner).

    Fix: every closed/listed IPO we see gets archived locally (with the
    date we last actually saw it) -- see _merge_closed_with_archive."""
    all_rows = _scrape_gmp_report(status_filter=None)
    live_closed = [r for r in all_rows if r.status == "closed"]
    return _merge_closed_with_archive(live_closed)


def fetch_all_mainboard_data() -> dict[str, list[IPORecord]]:
    """Fetches GMP report 331, subscription report 333, and listing-
    performance report 377 EXACTLY ONCE EACH, then derives Open, Upcoming,
    and Closed lists from that single fetch. This is the entry point the
    live backend's refresh loop should use (see backend/api.py's
    _refresh_loop) -- it replaces the previous approach of calling
    scrape_open_mainboard_ipos(), scrape_upcoming_mainboard_ipos(), and
    scrape_closed_mainboard_ipos() independently every cycle, each of
    which re-fetched all three reports from scratch (tripling every
    InvestorGain request per cycle for no benefit, since the underlying
    data is identical regardless of which tab asks for it).

    Refactored 2026-09-18 at the site owner's request. No change to WHAT
    data is shown or how status/caching/GitHub-persistence behave --
    Closed still gets the exact same archive-merge treatment as the
    standalone scrape_closed_mainboard_ipos(). Only the number of network
    calls per refresh cycle changes."""
    all_records = _fetch_and_build_all_records()
    open_records = [r for r in all_records if r.status == "open"]
    upcoming_records = [r for r in all_records if r.status == "upcoming"]
    live_closed = [r for r in all_records if r.status == "closed"]
    closed_records = _merge_closed_with_archive(live_closed)
    return {"open": open_records, "upcoming": upcoming_records, "closed": closed_records}


def _ipo_record_from_dict(d: dict) -> IPORecord:
    """Reconstructs an IPORecord from its dict form (as produced by
    to_dict()), for replaying an archived closed IPO back into a live
    result list. Subscription is a nested dict and needs its own
    reconstruction; everything else maps 1:1."""
    d = dict(d)  # don't mutate the archived copy
    sub_dict = d.pop("subscription", None) or {}
    sub = Subscription(**{k: v for k, v in sub_dict.items() if k in Subscription.__dataclass_fields__})
    known_fields = {f for f in IPORecord.__dataclass_fields__}
    filtered = {k: v for k, v in d.items() if k in known_fields}
    return IPORecord(subscription=sub, **filtered)


def _fetch_and_build_all_records() -> list[IPORecord]:
    """Fetches GMP report 331, subscription report 333, and listing-
    performance report 377 EXACTLY ONCE EACH, and builds an IPORecord for
    every mainboard IPO currently in the GMP report -- every status
    (open/upcoming/closed), unfiltered. Callers filter by .status
    afterward (see _scrape_gmp_report and fetch_all_mainboard_data below).

    Refactored 2026-09-18 at the site owner's request: previously, each of
    scrape_open_mainboard_ipos() / scrape_upcoming_mainboard_ipos() /
    scrape_closed_mainboard_ipos() called _scrape_gmp_report(status_filter=...)
    independently, and EACH of those calls re-fetched all three reports
    from scratch -- tripling every InvestorGain request per refresh cycle
    for no benefit, since the underlying data is identical regardless of
    which tab is asking. This function is the single source of truth: one
    fetch, one build, reused by every tab in a given cycle. No change to
    WHAT data is shown -- only how many network calls it costs to produce it."""
    rows = fetch_report(GMP_REPORT_ID)
    sub_map = scrape_subscription_breakdown()
    perf_map = scrape_listing_performance()  # always fetched once now (previously conditionally skipped per-call, but skipping no longer saves anything since this function itself is only ever called once per cycle by its callers below)
    records: list[IPORecord] = []

    for row in rows:
        # ~IPO_Category is authoritative and reliable for mainboard/SME.
        # ~ipo_status1 (the site's own O/U/C/L flag), however, has been
        # observed to lag -- staying "O" days after the real close/listing
        # date has passed. We read it as a fallback only; the real status
        # is derived from dates below, once we've parsed them.
        category_text = row.get("~IPO_Category") or ""
        site_status_text = row.get("~ipo_status1") or ""
        company_name, badge_category, badge_status = _parse_name_cell(row.get("Name", ""))
        category_text = category_text or badge_category
        site_status_text = site_status_text or badge_status

        if not company_name or not _is_mainboard(category_text):
            continue

        gmp_raw = _strip_tags(row.get("GMP", ""))
        gmp_val = None if "--" in gmp_raw.split("(")[0] else _first_number(gmp_raw)
        gmp_pct_match = re.search(r"\((-?[\d.]+)\s*%\)", gmp_raw)
        gmp_pct = float(gmp_pct_match.group(1)) if gmp_pct_match else None

        sub_text = _strip_tags(row.get("Sub", ""))
        sub_total = _first_number(sub_text)

        size_val = _first_number(unescape(row.get("IPO Size", "")))
        # Prefer the pre-formatted ISO date fields (~Srt_Open etc.) -- these
        # already carry the correct year and need no parsing/guessing at
        # all. Fall back to parsing the display text ("1-Sep") only if the
        # ISO field is ever missing.
        open_d = row.get("~Srt_Open") or _parse_date(row.get("Open"))
        close_d = row.get("~Srt_Close") or _parse_date(row.get("Close"))
        boa_d = row.get("~Srt_BoA_Dt") or _parse_date(row.get("BoA Dt"))
        listing_d = row.get("~Str_Listing") or _parse_date(row.get("Listing"))

        # Year-boundary fix: only applies when we had to GUESS the year via
        # _parse_date's fallback (i.e. InvestorGain's own ~Srt_* field was
        # missing for that specific date) -- never touches the ~Srt_*
        # fields themselves, which already carry the correct year. Fixes a
        # December-open/January-close IPO where both dates would otherwise
        # get stamped with the same year, making close appear to precede open.
        if not row.get("~Srt_Close"):
            close_d = _roll_year_if_before_open(close_d, open_d)
        if not row.get("~Srt_BoA_Dt"):
            boa_d = _roll_year_if_before_open(boa_d, open_d)
        if not row.get("~Str_Listing"):
            listing_d = _roll_year_if_before_open(listing_d, open_d)
        updated_text = _strip_tags(row.get("Updated-On", "")) or None

        status = _derive_status(open_d, close_d, site_status_text)

        rec = IPORecord(
            company_name=company_name,
            ipo_type="Mainboard",
            gmp=int(gmp_val) if gmp_val is not None else None,
            gmp_percent=gmp_pct,
            gmp_updated_at=updated_text,
            subscription=Subscription(
                qib=None, nii=None, retail=None,
                total=sub_total,
                started=sub_total is not None,
            ),
            issue_size_cr=size_val,
            open_date=open_d,
            close_date=close_d,
            boa_date=boa_d,
            listing_date=listing_d,
            status=status,
            source_url="https://www.investorgain.com/report/ipo-gmp-live/331/",
            last_updated=now_iso(),
        )

        detail = sub_map.get(company_name)
        if detail:
            rec.subscription.qib = detail.get("qib")
            rec.subscription.shni = detail.get("shni")
            rec.subscription.bhni = detail.get("bhni")
            rec.subscription.nii = detail.get("nii")
            rec.subscription.retail = detail.get("retail")
            if detail.get("total") is not None:
                rec.subscription.total = detail["total"]
            rec.subscription.started = True

        perf = perf_map.get(_normalize_company_name(company_name))
        if perf:
            rec.listing_price = perf.get("listing_price")
            rec.listing_gain_percent = perf.get("listing_gain_percent")

        records.append(rec)

    # Registrar/price-band enrichment now runs ONCE here on the full,
    # unfiltered set -- instead of once per tab on each tab's filtered
    # subset, which previously meant the same lookups (and the same
    # per-cycle new-fetch cap, see enrich_with_registrar's max_new_fetches)
    # were redundantly repeated up to three times per refresh cycle.
    try:
        rows_by_name = {}
        for row in rows:
            name, badge_cat, _s = _parse_name_cell(row.get("Name", ""))
            if name:
                rows_by_name[name] = row
        enrich_with_registrar(records, rows_by_name)
    except Exception as e:
        print(f"WARNING: registrar enrichment skipped: {e}", file=sys.stderr)

    return records


def _scrape_gmp_report(status_filter: Optional[str] = None) -> list[IPORecord]:
    """Standalone/back-compat entry point -- fetches everything fresh
    (via _fetch_and_build_all_records) and filters by status. Used by
    scrape_open_mainboard_ipos/scrape_upcoming_mainboard_ipos/
    scrape_closed_mainboard_ipos for standalone calls (CLI script,
    diagnostic endpoints) where only one tab's data is wanted in
    isolation -- each such call still does its own full fetch, exactly as
    before.

    For the live backend's refresh loop, which needs all three tabs
    together every cycle, use fetch_all_mainboard_data() instead -- that
    one fetches ONCE and derives all three tabs from the single result,
    rather than calling this function (and therefore re-fetching) three
    separate times."""
    all_records = _fetch_and_build_all_records()
    if status_filter is None:
        return all_records
    return [r for r in all_records if r.status == status_filter]


def _normalize_company_name(name: str) -> str:
    """Report 377 has been observed with trailing/extra whitespace in
    company names (e.g. 'Symbiotec Pharmalab ') that reports 331/333 don't
    have -- normalize before using as a merge key across reports, or
    matches silently fail."""
    return " ".join((name or "").split())


def _safe_float(value) -> Optional[float]:
    """Converts a value to float, safely handling the real failure modes
    seen from InvestorGain's API: None, an empty string (confirmed live
    2026-09-17 -- ~str_closing_gain_in_per can be "" rather than missing
    entirely, which crashed float('') and took down the whole Closed tab
    refresh), or any other non-numeric junk. Returns None rather than
    raising, in every case where the value isn't a clean number."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def scrape_listing_performance() -> dict[str, dict]:
    """Returns {normalized_company_name: {listing_price, issue_price,
    listing_gain_percent, closing_gain_percent}} from InvestorGain's
    dedicated GMP Performance Tracker report (id 377) -- the ACTUAL
    listing-day price change, not a GMP-based estimate.

    Confirmed real field names via a live capture of this report
    (2026-09-01), which differ from reports 331/333 in several ways:
      - company name field is "IPO", not "Name" (and may have trailing
        whitespace -- see _normalize_company_name)
      - "IPO Price" is the clean issue price (not "Issue Price")
      - "~str_listing_gain_in_per" is the ready-made actual listing-day
        gain % -- this is the number we want, no HTML parsing needed
      - "~str_closing_gain_in_per" is gain as of latest close (not just
        listing day) -- kept as a secondary field, not the primary one,
        since "listing gain" specifically means listing-day performance
      - "Listing Price" is an HTML-wrapped fragment like
        "<span class='text-success'>₹988.00 (0.00%)</span>" -- only used
        as a fallback if the clean ~str_listing_gain_in_per is ever absent

    Report 377 takes `year` as a query param rather than in the URL path
    like reports 331/333 do."""
    result: dict[str, dict] = {}
    year = datetime.now().year
    url = f"{BASE}/{PERFORMANCE_REPORT_ID}/1/8/{year}/all/0/all?year={year}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        rows = resp.json().get("reportTableData", [])
    except (requests.RequestException, ValueError) as e:
        print(f"WARNING: listing performance fetch failed: {e}", file=sys.stderr)
        return result

    for row in rows:
        category_text = row.get("~IPO_Category") or ""
        company_name = _normalize_company_name(row.get("IPO", ""))
        if not company_name or not _is_mainboard(category_text):
            continue

        issue_price = _first_number(unescape(row.get("IPO Price", "")))

        gain_pct = row.get("~str_listing_gain_in_per")
        gain_pct = _safe_float(gain_pct)
        if gain_pct is None:
            # Fallback: parse the % out of the HTML-wrapped "Listing Price"
            # fragment, e.g. "...(0.00%)</span>" -- only reached if
            # InvestorGain ever removes/empties the clean field above.
            listing_price_html = unescape(row.get("Listing Price", ""))
            pct_match = re.search(r"\(([-\d.]+)\s*%\)", listing_price_html)
            gain_pct = _safe_float(pct_match.group(1)) if pct_match else None

        listing_price = None
        if issue_price is not None and gain_pct is not None:
            listing_price = round(issue_price * (1 + gain_pct / 100), 2)
        else:
            listing_price_html = unescape(row.get("Listing Price", ""))
            listing_price = _first_number(_strip_tags(listing_price_html).split("(")[0])

        closing_gain_pct = row.get("~str_closing_gain_in_per")
        closing_gain_pct = _safe_float(closing_gain_pct)

        result[company_name] = {
            "listing_price": listing_price,
            "issue_price": issue_price,
            "listing_gain_percent": gain_pct,
            "closing_gain_percent": closing_gain_pct,
        }
    return result


def scrape_subscription_breakdown() -> dict[str, dict]:
    """Returns {company_name: {qib, nii, retail, total}} for Mainboard IPOs,
    read from the dedicated subscription report (id 333)."""
    result: dict[str, dict] = {}
    try:
        rows = fetch_report(SUBSCRIPTION_REPORT_ID)
    except requests.RequestException as e:
        print(f"WARNING: subscription breakdown fetch failed: {e}", file=sys.stderr)
        return result

    for row in rows:
        category_text = row.get("~IPO_Category") or ""
        company_name, badge_category, _status = _parse_name_cell(row.get("Name", ""))
        category_text = category_text or badge_category
        if not company_name or not _is_mainboard(category_text):
            continue

        result[company_name] = {
            "total": _first_number(_strip_tags(row.get("Total", ""))),
            "qib": _first_number(row.get("QIB", "")),
            "shni": _first_number(row.get("SHNI", "")),  # Small HNI (₹2-10L bids) -- confirmed real field name, live-verified 2026-09-13
            "bhni": _first_number(row.get("BHNI", "")),  # Big HNI (₹10L+ bids) -- same
            "nii": _first_number(row.get("NII", "")),
            "retail": _first_number(row.get("RII", "")),
        }
    return result


def _extract_url_slug_and_id(row: dict) -> tuple[Optional[str], Optional[int]]:
    """Pulls the slug + numeric id out of a "~URLRewrite_Folder_Name"-style
    field (case has been observed to vary between reports -- check both).
    e.g. "/gmp/rays-of-belief-ipo/2041/" -> ("rays-of-belief-ipo", 2041)."""
    raw = row.get("~URLRewrite_Folder_Name") or row.get("~urlrewrite_folder_name") or ""
    parts = [p for p in raw.split("/") if p]
    if len(parts) < 2:
        return None, None
    slug = parts[-2]
    try:
        ipo_id = int(parts[-1])
    except ValueError:
        return slug, None
    return slug, ipo_id


def _parse_price_band(raw_text: str) -> tuple[Optional[float], Optional[float]]:
    """Parses InvestorGain's "Price Band" table value, confirmed live
    2026-09-13 via investorgain.com/ipo/manika-plastech-ipo/1806/ to be
    formatted like "₹40.00-43.00 per share". Returns (floor, cap).

    Falls back to treating a single number as both floor and cap for the
    rare fixed-price issue case (no real range), rather than failing
    silently -- but never invents a range that wasn't actually there."""
    if not raw_text:
        return None, None
    clean = raw_text.replace("₹", "").replace("per share", "").strip()
    range_match = re.search(r"([\d,]+\.?\d*)\s*-\s*([\d,]+\.?\d*)", clean)
    if range_match:
        floor = _first_number(range_match.group(1))
        cap = _first_number(range_match.group(2))
        return floor, cap
    single_match = re.search(r"([\d,]+\.?\d*)", clean)
    if single_match:
        price = _first_number(single_match.group(1))
        return price, price  # fixed-price issue -- floor == cap, not a guessed range
    return None, None


def scrape_ipo_detail_page(url_slug: str, ipo_id: int) -> dict:
    """Fetches ONE IPO's individual detail page ONCE and extracts BOTH
    registrar and price band from it -- these used to be two separate
    functions each doing their own fetch; combined into one request since
    both values live on the same page (confirmed via the same live fetch
    that verified the Price Band field format above).

    This page is genuinely server-rendered HTML (not a JS/JSON API like
    reports 331/333/377) -- good for reliability, but means ONE extra
    network request per IPO, so callers should cache aggressively (see
    enrich_with_ipo_details below) rather than refetch every cycle.

    Returns {"registrar": str|None, "price_band_floor": float|None,
    "price_band_cap": float|None}. Never raises -- a fetch/parse failure
    just means all three come back None, exactly as if the row wasn't found."""
    result = {"registrar": None, "price_band_floor": None, "price_band_cap": None}
    url = f"https://www.investorgain.com/ipo/{url_slug}/{ipo_id}/"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"WARNING: IPO detail page fetch failed for {url}: {e}", file=sys.stderr)
        return result

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")
        for row_el in soup.find_all("tr"):
            cells = row_el.find_all(["td", "th"])
            if len(cells) != 2:
                continue
            label = _strip_tags(cells[0].get_text()).strip().lower()
            value = _strip_tags(cells[1].get_text()).strip()
            if label == "registrar":
                result["registrar"] = value or None
            elif label == "price band":
                floor, cap = _parse_price_band(value)
                result["price_band_floor"] = floor
                result["price_band_cap"] = cap
    except Exception as e:
        print(f"WARNING: IPO detail page parse failed for {url}: {e}", file=sys.stderr)
    return result


def _load_registrar_cache() -> dict[str, dict]:
    if not REGISTRAR_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(REGISTRAR_CACHE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_registrar_cache(cache: dict[str, dict]) -> None:
    try:
        REGISTRAR_CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"WARNING: could not save registrar/price-band cache: {e}", file=sys.stderr)


def enrich_with_registrar(records: list[IPORecord], gmp_rows_by_name: dict[str, dict], max_new_fetches: int = 6) -> None:
    """Mutates records in place, adding .registrar, .price_band_floor, and
    .price_band_cap -- all three come from the same individual IPO detail
    page (see scrape_ipo_detail_page), fetched ONCE per IPO and cached
    permanently (registrar_cache.json) since none of these three values
    ever change once an IPO is filed. Only genuinely new IPOs (not yet in
    the cache) trigger a live page fetch -- this is what makes it
    affordable to show these on every tab (Open/Upcoming/Closed).

    max_new_fetches caps how many LIVE page fetches happen in a single
    call (default 6). This matters most right after a redeploy, when the
    cache is empty and potentially every IPO across all three tabs needs a
    fresh fetch simultaneously -- without a cap, a burst of many new IPOs
    could make a single refresh cycle take minutes, risking it never
    completing (this was suspected as a possible cause of the Closed tab
    going persistently blank -- 2026-09-17). Any IPO past the cap simply
    waits for a later cycle; nothing is lost, just spread out over time.

    Note: Render's free-tier filesystem resets on every redeploy, so this
    cache persists between refresh cycles but not across deploys -- still
    a large reduction in fetches versus no caching at all. (Unlike GMP
    direction state, this cache is NOT GitHub-backed -- registrar/price
    band are cheap to re-fetch once per IPO after a redeploy, so the
    added complexity wasn't worth it the way it was for GMP direction,
    which needs to survive across CALENDAR DAYS, not just redeploys.)"""
    cache = _load_registrar_cache()
    cache_dirty = False
    new_fetches_done = 0

    for rec in records:
        row = gmp_rows_by_name.get(rec.company_name)
        if not row:
            continue
        slug, ipo_id = _extract_url_slug_and_id(row)
        if not slug or not ipo_id:
            continue

        cache_key = str(ipo_id)
        if cache_key in cache:
            cached = cache[cache_key]
            # Backward compatibility: older cache entries (before this
            # feature) stored a plain string, not a dict -- treat those as
            # registrar-only with no price band cached yet.
            if isinstance(cached, str):
                rec.registrar = cached or None
            else:
                rec.registrar = cached.get("registrar")
                rec.price_band_floor = cached.get("price_band_floor")
                rec.price_band_cap = cached.get("price_band_cap")
            continue

        if new_fetches_done >= max_new_fetches:
            continue  # cap reached this cycle -- leave unset, pick it up next cycle instead of risking a long stall

        detail = scrape_ipo_detail_page(slug, ipo_id)
        rec.registrar = detail["registrar"]
        rec.price_band_floor = detail["price_band_floor"]
        rec.price_band_cap = detail["price_band_cap"]
        cache[cache_key] = detail  # caches the miss too (all-None), so we don't retry every cycle
        cache_dirty = True
        new_fetches_done += 1

    if cache_dirty:
        _save_registrar_cache(cache)


def _load_gmp_direction_state() -> dict:
    """Loads GMP direction memory. Tries GitHub first (survives redeploys
    -- see _github_get_file), falls back to the local disk copy, which is
    only reliable WITHIN a single running process since Render's free tier
    wipes local disk on every redeploy. See module docstring note on
    GITHUB_TOKEN for why this exists."""
    github_state = _github_get_file(GMP_DIRECTION_STATE_GITHUB_PATH)
    if github_state is not None:
        return github_state
    if not GMP_DIRECTION_STATE_FILE.exists():
        return {}
    try:
        return json.loads(GMP_DIRECTION_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_gmp_direction_state(state: dict) -> None:
    # Always write the local copy too -- fast, and a safety net if the
    # GitHub push below fails for any reason (rate limit, network blip).
    try:
        GMP_DIRECTION_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"WARNING: could not save gmp_direction_state locally: {e}", file=sys.stderr)

    _github_put_file(
        GMP_DIRECTION_STATE_GITHUB_PATH,
        state,
        commit_message="Update GMP direction memory [automated]",
    )


def _update_gmp_direction(company_name: str, new_gmp, state: dict) -> Optional[str]:
    """Implements the exact rule specified by the site owner (2026-09-12):

    1. FIRST check of a new calendar day for a given IPO: compare today's
       GMP to YESTERDAY's last recorded GMP.
         - equal      -> "flat" (grey dash) -- the ONLY time flat can appear
         - higher     -> "up"
         - lower      -> "down"
    2. EVERY check after that, same day: compare to the immediately
       previous check (normal 2-minute comparison).
         - higher     -> "up"
         - lower      -> "down"
         - unchanged  -> KEEP the existing direction as-is (persists
           through quiet gaps -- never falls back to "flat" again that
           day, even if the value happens to revisit yesterday's number)

    Mutates `state` in place (per-company entries) and returns the
    direction to attach to this record. `state` is persisted to
    GMP_DIRECTION_STATE_FILE by the caller after processing all records.

    No prior state at all for this company (brand new IPO) -> None, never
    a guessed direction. This is decision-relevant data; see the original
    2026-09-11 direction-indicator notes for why we refuse to guess."""
    if new_gmp is None:
        return None  # can't judge direction without a real number

    today = datetime.now().date().isoformat()
    entry = state.get(company_name)

    if entry is None:
        # Never seen this IPO before -- nothing to compare against yet.
        # IMPORTANT: only set OUR OWN keys, never replace the whole dict --
        # _update_gmp_extremes (called right after this, same company, same
        # state dict) stores its own "gmp_extremes" key here too. A full
        # dict replacement here would silently wipe that out. This was a
        # real bug (fixed 2026-09-13): Opening/Highest/Lowest kept
        # resetting to the same number every day because THIS function's
        # day-rollover branch was clobbering the whole entry.
        state[company_name] = {
            "last_gmp": new_gmp,
            "last_direction": None,
            "last_seen_day": today,
            "prev_day_close_gmp": None,
        }
        return None

    if entry.get("last_seen_day") != today:
        # First check of a NEW day for this IPO -- the special "vs
        # yesterday's close" comparison, the only path that can produce "flat".
        yesterday_gmp = entry.get("last_gmp")  # last value recorded before today
        if yesterday_gmp is None:
            direction = None
        elif new_gmp == yesterday_gmp:
            direction = "flat"
        elif new_gmp > yesterday_gmp:
            direction = "up"
        else:
            direction = "down"

        # Mutate the EXISTING entry in place rather than replacing it --
        # preserves "gmp_extremes" (and any other keys other functions
        # sharing this same state dict may have written). See the note
        # above the entry-is-None branch for the bug this fixes.
        entry["last_gmp"] = new_gmp
        entry["last_direction"] = direction
        entry["last_seen_day"] = today
        entry["prev_day_close_gmp"] = yesterday_gmp
        state[company_name] = entry
        return direction

    # Same day as last check -- normal comparison; unchanged PERSISTS the
    # existing direction rather than reverting to flat.
    prev_gmp = entry.get("last_gmp")
    if prev_gmp is None:
        direction = entry.get("last_direction")
    elif new_gmp > prev_gmp:
        direction = "up"
    elif new_gmp < prev_gmp:
        direction = "down"
    else:
        direction = entry.get("last_direction")  # unchanged -- keep prior direction, no flat

    entry["last_gmp"] = new_gmp
    entry["last_direction"] = direction
    state[company_name] = entry
    return direction


def _update_gmp_extremes(company_name: str, new_gmp_percent, status: str, state: dict) -> dict:
    """Tracks Opening / Highest / Lowest GMP PERCENT for one company,
    per the site owner's exact rule (2026-09-13):
      - Opening: the very FIRST GMP% ever recorded for this IPO. Frozen
        forever once set -- never overwritten, no matter what happens later.
      - Highest / Lowest: keep updating live (grows/shrinks as real values
        come in) for as long as the IPO is actively tracked -- Upcoming,
        Open, or Closed-but-not-yet-listed.
      - The moment status becomes "listed", all three FREEZE permanently --
        no more grey-market price discovery happens after real trading starts.

    Returns {"opening": float|None, "highest": float|None, "lowest": float|None}
    to attach to the record. Mutates `state` in place; persisted by the
    caller exactly like _update_gmp_direction's state (same file, same
    GitHub-backed persistence -- see _load_gmp_direction_state)."""
    entry = state.get(company_name, {})
    extremes = entry.get("gmp_extremes", {
        "opening": None,
        "highest": None,
        "lowest": None,
        "frozen": False,  # True once the IPO has listed -- stop updating highest/lowest forever
    })

    if extremes.get("frozen"):
        # Already listed -- these numbers are permanent history now, never touch them again.
        entry["gmp_extremes"] = extremes
        state[company_name] = entry
        return {"opening": extremes["opening"], "highest": extremes["highest"], "lowest": extremes["lowest"]}

    if new_gmp_percent is not None:
        if extremes["opening"] is None:
            extremes["opening"] = new_gmp_percent  # first real value ever seen -- set once, never again
        if extremes["highest"] is None or new_gmp_percent > extremes["highest"]:
            extremes["highest"] = new_gmp_percent
        if extremes["lowest"] is None or new_gmp_percent < extremes["lowest"]:
            extremes["lowest"] = new_gmp_percent

    if status == "listed":
        extremes["frozen"] = True  # from here on, no further updates ever

    entry["gmp_extremes"] = extremes
    state[company_name] = entry
    return {"opening": extremes["opening"], "highest": extremes["highest"], "lowest": extremes["lowest"]}


def save_cache(
    records: list[IPORecord],
    key: str = "open",
    direction_state: Optional[dict] = None,
    persist_direction_state: bool = True,
) -> None:
    """Writes cache with THREE honest timestamps instead of one that
    conflates "we tried" with "data actually changed":
      - attempted_at: every call, no matter what (proves the refresh loop
        is alive at all)
      - data_changed_at: only advances when the new records are genuinely
        different from what was cached before -- this is what answers
        "is this ACTUALLY current data" rather than "did a request run"
      - fetch_returned_records: whether this attempt got any data at all
        (an empty/failed scrape shouldn't silently look identical to a
        successful one that happened to find zero matching IPOs)

    This directly addresses a real bug found 2026-09-09: the old version
    stamped fetched_at on every call regardless of whether the scrape
    actually produced new data, so the UI could claim "just updated" while
    showing content that hadn't truly changed in a while.

    Also computes gmp_direction ("up"/"down"/"flat"/None) per record by
    comparing THIS fetch's GMP to the PREVIOUS fetch's GMP for the same
    company -- explicitly None (not "flat") when there's no prior value to
    compare against, so a brand-new IPO never shows a fake "unchanged"
    signal. This is decision-relevant data, so correctness here matters
    more than in most of the app -- see the docstring on _compute_gmp_direction.

    direction_state / persist_direction_state (added 2026-09-18): by
    default (both left as-is), this function is fully self-contained --
    loads the GMP-direction state itself, computes, and saves it back --
    identical to how it always worked. The live backend's refresh loop
    calls this three times per cycle (once per tab); to avoid loading and
    saving that same state three separate times for no reason, it can now
    load the state ONCE, pass the SAME dict into all three calls with
    persist_direction_state=False, then save it ONCE itself at the end.
    Passing a pre-loaded direction_state implies the caller is responsible
    for persisting it -- persist_direction_state is ignored (treated as
    False) whenever direction_state is provided, so the shared object
    never gets written by more than one place."""
    cache = {}
    if CACHE_FILE.exists():
        cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))

    prev_entry = cache.get(key, {})
    prev_records = prev_entry.get("records", [])

    # GMP direction is meaningful for Open, Upcoming, AND Closed --
    # InvestorGain often shows indicative GMP before an IPO opens, and
    # crucially GMP keeps moving for a Closed IPO too, right up until its
    # actual listing day (the stock hasn't started trading yet, so grey
    # market pricing is still live). Once an IPO is fully listed, GMP
    # naturally stops changing on its own -- no special-casing needed here,
    # the direction will just stay flat/last-known since nothing new comes in.
    if key in ("open", "upcoming", "closed"):
        owns_state = direction_state is None  # True => self-contained (load+save here); False => caller manages persistence
        state = direction_state if direction_state is not None else _load_gmp_direction_state()
        state_before = json.dumps(state, sort_keys=True) if owns_state else None
        new_records_data = []
        for r in records:
            d = r.to_dict()
            d["gmp_direction"] = _update_gmp_direction(
                company_name=d.get("company_name"),
                new_gmp=d.get("gmp"),
                state=state,
            )
            extremes = _update_gmp_extremes(
                company_name=d.get("company_name"),
                new_gmp_percent=d.get("gmp_percent"),
                status=d.get("status"),
                state=state,
            )
            d["gmp_opening"] = extremes["opening"]
            d["gmp_highest"] = extremes["highest"]
            d["gmp_lowest"] = extremes["lowest"]
            new_records_data.append(d)
        # Only push to GitHub if the state genuinely changed this cycle --
        # most 2-minute cycles won't move any IPO's GMP, so this avoids a
        # commit every 2 minutes all day when nothing actually happened.
        # Only save here when THIS call owns the state (no shared state was
        # passed in) -- otherwise the caller is responsible for the single
        # combined save after all tabs have been processed.
        if owns_state and json.dumps(state, sort_keys=True) != state_before:
            _save_gmp_direction_state(state)
    else:
        new_records_data = [r.to_dict() for r in records]

    # Compare against the previous snapshot's records (ignoring volatile
    # fields that always differ, like last_updated timestamps and the
    # gmp_direction we just computed) to decide if the data GENUINELY changed.
    def _comparable(rec_list):
        stripped = []
        for r in rec_list:
            r2 = dict(r)
            r2.pop("last_updated", None)
            r2.pop("gmp_direction", None)
            stripped.append(r2)
        return stripped

    prev_comparable = _comparable(prev_records)
    new_comparable = _comparable(new_records_data)
    data_actually_changed = prev_comparable != new_comparable

    now = now_iso()
    data_changed_at = now if (data_actually_changed or not prev_entry.get("data_changed_at")) else prev_entry.get("data_changed_at")

    cache[key] = {
        "attempted_at": now,
        "data_changed_at": data_changed_at,
        "fetch_returned_records": len(records) > 0,
        "fetched_at": now,  # kept for backward compatibility with older frontend builds
        "records": new_records_data,
    }
    CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def load_cache(key: str = "open") -> dict:
    if not CACHE_FILE.exists():
        return {"fetched_at": None, "attempted_at": None, "data_changed_at": None, "records": []}
    cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    return cache.get(key, {"fetched_at": None, "attempted_at": None, "data_changed_at": None, "records": []})


if __name__ == "__main__":
    # Quick local sanity check: run the scraper once, print what it found.
    # (The old --inspect / --inspect-subscription raw-dump flags were
    # removed 2026-09-19 -- fully superseded by the live /api/debug/
    # gmp-report-raw and /api/debug/subscription-raw endpoints, which need
    # no local setup and also cover price-band/registrar, which these
    # flags never did.)
    recs = scrape_open_mainboard_ipos()
    save_cache(recs, "open")
    print(f"Fetched {len(recs)} open Mainboard IPOs. Cached to {CACHE_FILE}")
    for r in recs:
        print(f"  - {r.company_name}: GMP {r.gmp}, Sub {r.subscription.total}x, "
              f"{r.open_date} -> {r.close_date}")
