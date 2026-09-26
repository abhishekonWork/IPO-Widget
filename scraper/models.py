"""
Normalized data structures for the IPO widget.

Every field that we could not actually read from InvestorGain is left as
None / "Not Available" — nothing here is ever invented. The scraper layer
is responsible for filling these in from real page content only.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


@dataclass
class Subscription:
    qib: Optional[float] = None      # e.g. 18.42 means "18.42x"
    shni: Optional[float] = None     # Small HNI (bids ₹2-10 lakh) -- separate figure from NII, not a sub-total of it (confirmed via live data 2026-09-13)
    bhni: Optional[float] = None     # Big HNI (bids ₹10 lakh+) -- same as above
    nii: Optional[float] = None
    retail: Optional[float] = None
    total: Optional[float] = None
    started: bool = False            # False => show "Not Started", never 0.00x

    def to_dict(self):
        return asdict(self)


@dataclass
class IPORecord:
    company_name: str
    ipo_type: str                    # "Mainboard" — SME is filtered out before this is built
    gmp: Optional[int] = None        # rupees, indicative
    gmp_percent: Optional[float] = None   # e.g. 86.71 means "86.71%" over the issue price
    gmp_updated_at: Optional[str] = None
    subscription: Subscription = field(default_factory=Subscription)
    issue_size_cr: Optional[float] = None
    open_date: Optional[str] = None       # ISO "YYYY-MM-DD"
    close_date: Optional[str] = None
    boa_date: Optional[str] = None
    listing_date: Optional[str] = None
    status: str = "unknown"          # "open" | "upcoming" | "closed" | "listed"
    listing_price: Optional[float] = None       # actual price on listing day, from report 377
    listing_gain_percent: Optional[float] = None  # actual (listing_price - issue_price)/issue_price * 100 -- NOT the GMP estimate
    registrar: Optional[str] = None              # e.g. "Bigshare Services Pvt.Ltd." -- from the individual IPO detail page
    price_band_floor: Optional[float] = None      # e.g. 40.00 -- from the "Price Band" row on the individual IPO detail page. Fixed at filing, cached permanently once fetched, same pattern as registrar.
    price_band_cap: Optional[float] = None         # e.g. 43.00 -- may equal price_band_floor for a fixed-price issue (rare), never guessed
    gmp_opening: Optional[float] = None            # the VERY FIRST GMP % we ever recorded for this IPO -- frozen forever once set, never updated again
    gmp_highest: Optional[float] = None            # all-time highest GMP % recorded while actively tracked (Upcoming/Open/Closed-not-yet-listed) -- freezes permanently once the IPO lists
    gmp_lowest: Optional[float] = None             # all-time lowest GMP % recorded, same freeze rule as gmp_highest
    source_url: Optional[str] = None
    detail_page_url: Optional[str] = None         # this IPO's own InvestorGain page, e.g. https://www.investorgain.com/ipo/moneyview-ipo/2198/ -- for a "View Details" link. None if the slug/id couldn't be read from the report row (never a guessed URL).
    last_updated: Optional[str] = None    # when WE fetched it, ISO datetime
    source: str = "InvestorGain"

    def to_dict(self):
        d = asdict(self)
        d["subscription"] = self.subscription.to_dict()
        return d


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
