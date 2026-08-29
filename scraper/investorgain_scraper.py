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

import argparse
import json
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
    m = re.search(r"[\d,]+\.?\d*", text.replace(",", ""))
    return float(m.group().replace(",", "")) if m else None


def _parse_date(day_month: Optional[str]) -> Optional[str]:
    """'28-Aug' -> '2026-08-28'. API gives no year; assumes current year."""
    if not day_month:
        return None
    day_month = day_month.strip()
    try:
        dt = datetime.strptime(f"{day_month}-{datetime.now().year}", "%d-%b-%Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return day_month or None  # fall back to raw text rather than losing the data


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


def scrape_closed_mainboard_ipos() -> list[IPORecord]:
    """Covers both 'closed, not yet listed' (C/CT) and 'listed' (L) rows --
    the GMP report includes recently-closed IPOs too, so this needs no
    extra request beyond what scrape_open/scrape_upcoming already make."""
    all_rows = _scrape_gmp_report(status_filter=None)
    return [r for r in all_rows if r.status in ("closed", "listed")]


def _scrape_gmp_report(status_filter: Optional[str] = None) -> list[IPORecord]:
    rows = fetch_report(GMP_REPORT_ID)
    sub_map = scrape_subscription_breakdown()
    records: list[IPORecord] = []

    for row in rows:
        # ~IPO_Category and ~ipo_status1 are authoritative, row-level fields
        # confirmed present in every captured row -- read these directly
        # instead of regex-parsing badges out of the "Name" HTML fragment,
        # which is fragile (badge count/order isn't guaranteed) and was the
        # source of IPOs being dropped or bucketed into the wrong tab.
        category_text = row.get("~IPO_Category") or ""
        status_text = row.get("~ipo_status1") or ""
        company_name, badge_category, badge_status = _parse_name_cell(row.get("Name", ""))
        category_text = category_text or badge_category
        status_text = status_text or badge_status

        if not company_name or not _is_mainboard(category_text):
            continue

        status = STATUS_MAP.get(status_text, status_filter or "unknown")
        if status_filter and status != status_filter:
            continue

        gmp_raw = _strip_tags(row.get("GMP", ""))
        gmp_val = None if "--" in gmp_raw.split("(")[0] else _first_number(gmp_raw)
        gmp_pct_match = re.search(r"\(([\d.]+)\s*%\)", gmp_raw)
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
        updated_text = _strip_tags(row.get("Updated-On", "")) or None

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
            rec.subscription.nii = detail.get("nii")
            rec.subscription.retail = detail.get("retail")
            if detail.get("total") is not None:
                rec.subscription.total = detail["total"]
            rec.subscription.started = True

        records.append(rec)

    return records


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
            "nii": _first_number(row.get("NII", "")),
            "retail": _first_number(row.get("RII", "")),
        }
    return result


def save_cache(records: list[IPORecord], key: str = "open") -> None:
    cache = {}
    if CACHE_FILE.exists():
        cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    cache[key] = {
        "fetched_at": now_iso(),
        "records": [r.to_dict() for r in records],
    }
    CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def load_cache(key: str = "open") -> dict:
    if not CACHE_FILE.exists():
        return {"fetched_at": None, "records": []}
    cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    return cache.get(key, {"fetched_at": None, "records": []})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", action="store_true",
                         help="Dump the raw GMP-report JSON instead of parsing, "
                              "for diffing against this file's assumptions")
    parser.add_argument("--inspect-subscription", action="store_true",
                         help="Dump the raw subscription-report JSON")
    args = parser.parse_args()

    if args.inspect:
        r = requests.get(_report_url(GMP_REPORT_ID), headers=HEADERS, timeout=REQUEST_TIMEOUT)
        Path("debug_gmp_report.json").write_text(r.text, encoding="utf-8")
        print(f"HTTP {r.status_code}. Saved raw response to debug_gmp_report.json")
    elif args.inspect_subscription:
        r = requests.get(_report_url(SUBSCRIPTION_REPORT_ID), headers=HEADERS, timeout=REQUEST_TIMEOUT)
        Path("debug_subscription_report.json").write_text(r.text, encoding="utf-8")
        print(f"HTTP {r.status_code}. Saved raw response to debug_subscription_report.json")
    else:
        recs = scrape_open_mainboard_ipos()
        save_cache(recs, "open")
        print(f"Fetched {len(recs)} open Mainboard IPOs. Cached to {CACHE_FILE}")
        for r in recs:
            print(f"  - {r.company_name}: GMP {r.gmp}, Sub {r.subscription.total}x, "
                  f"{r.open_date} -> {r.close_date}")
