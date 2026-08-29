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
    source_url: Optional[str] = None
    last_updated: Optional[str] = None    # when WE fetched it, ISO datetime
    source: str = "InvestorGain"

    def to_dict(self):
        d = asdict(self)
        d["subscription"] = self.subscription.to_dict()
        return d


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
