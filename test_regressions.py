"""
Regression test suite for investorgain_scraper.py.

WHY THIS FILE EXISTS: throughout this project's development, the same
handful of bug CATEGORIES kept resurfacing in different forms (a merge
silently failing on a name mismatch, a numeric field crashing on
unexpected input, a date wrapping to the wrong year, etc.). Each was found
live, on the deployed site, then fixed -- but nothing stopped a future
change from accidentally reintroducing the same behavior. This file exists
so that CAN'T happen silently: every bug below is encoded as an explicit
test with a comment on when/why it was found. Run this before trusting any
future change to this file:

    cd scraper && python3 -m unittest test_regressions.py -v

If a change breaks one of these, a test fails loudly here -- instead of
the site quietly showing wrong data again for the same reason it did
before.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import investorgain_scraper as s
from models import IPORecord, Subscription


def _isolate_data_files(test_case: unittest.TestCase) -> Path:
    """Points every module-level cache/state file at a fresh temp
    directory for the duration of one test, and restores the originals
    afterward -- so tests never read/write the real data/ folder or
    interfere with each other."""
    test_dir = Path(f"/tmp/regression_test_{test_case.id().replace('.', '_')}")
    shutil.rmtree(test_dir, ignore_errors=True)
    test_dir.mkdir(parents=True)

    originals = {
        "CACHE_FILE": s.CACHE_FILE,
        "REGISTRAR_CACHE_FILE": s.REGISTRAR_CACHE_FILE,
        "GMP_DIRECTION_STATE_FILE": s.GMP_DIRECTION_STATE_FILE,
        "CLOSED_IPO_ARCHIVE_FILE": s.CLOSED_IPO_ARCHIVE_FILE,
        "GITHUB_TOKEN": s.GITHUB_TOKEN,
    }
    s.CACHE_FILE = test_dir / "ipo_cache.json"
    s.REGISTRAR_CACHE_FILE = test_dir / "registrar_cache.json"
    s.GMP_DIRECTION_STATE_FILE = test_dir / "gmp_direction_state.json"
    s.CLOSED_IPO_ARCHIVE_FILE = test_dir / "closed_ipo_archive.json"
    s.GITHUB_TOKEN = None  # never hit the real GitHub API from a test

    def restore():
        for attr, value in originals.items():
            setattr(s, attr, value)
        shutil.rmtree(test_dir, ignore_errors=True)

    test_case.addCleanup(restore)
    return test_dir


def _mock_report_response(rows: list[dict]):
    resp = mock.Mock()
    resp.json.return_value = {"reportTableData": rows}
    resp.raise_for_status = lambda: None
    return resp


class TestNegativeNumberParsing(unittest.TestCase):
    """Bug: _first_number's regex didn't include a leading "-", so
    negative GMP (a real, meaningful value -- grey market expects a
    listing BELOW issue price) silently became positive. Found when a
    real IPO had negative GMP and the site showed the wrong sign/color."""

    def test_negative_rupee_value_keeps_its_sign(self):
        self.assertEqual(s._first_number("₹-8"), -8.0)

    def test_negative_percent_keeps_its_sign(self):
        self.assertEqual(s._first_number("-3.20"), -3.2)

    def test_positive_still_works(self):
        self.assertEqual(s._first_number("₹372"), 372.0)


class TestSafeFloatConversion(unittest.TestCase):
    """Bug: report 377's ~str_closing_gain_in_per field can be an empty
    string ("") rather than missing entirely or a real number. A bare
    float("") crash took down the ENTIRE Closed tab refresh, every single
    cycle, until this was found via a captured traceback. _safe_float
    exists specifically so this class of input can never crash anything
    again -- test every failure mode explicitly."""

    def test_empty_string_returns_none_not_a_crash(self):
        self.assertIsNone(s._safe_float(""))

    def test_none_returns_none(self):
        self.assertIsNone(s._safe_float(None))

    def test_whitespace_only_returns_none(self):
        self.assertIsNone(s._safe_float("   "))

    def test_junk_text_returns_none_not_a_crash(self):
        self.assertIsNone(s._safe_float("not a number"))

    def test_real_number_still_converts(self):
        self.assertEqual(s._safe_float("5.2"), 5.2)
        self.assertEqual(s._safe_float(5.2), 5.2)


class TestYearBoundaryDateParsing(unittest.TestCase):
    """Bug: _parse_date's fallback path stamps every date with the
    CURRENT calendar year, with no awareness of other dates in the same
    IPO's timeline. For an IPO open in December and closing in January,
    both dates got the same year, making close appear to be BEFORE open.
    Fixed with _roll_year_if_before_open, applied only to the fallback
    path (never to InvestorGain's own already-correct ~Srt_* fields,
    which would double-roll and produce a wrong date)."""

    def test_december_open_january_close_rolls_forward_correctly(self):
        open_d = s._parse_date("30-Dec")
        close_d = s._parse_date("3-Jan")
        # Before the fix: both same year, close appears to precede open.
        self.assertEqual(open_d[:4], close_d[:4], "sanity check: both guessed the same year before correction")

        corrected = s._roll_year_if_before_open(close_d, open_d, was_guessed=True)
        open_dt = datetime.strptime(open_d, "%Y-%m-%d")
        close_dt = datetime.strptime(corrected, "%Y-%m-%d")
        self.assertGreater(close_dt, open_dt, "close date must come AFTER open date")
        self.assertEqual((close_dt - open_dt).days, 4, "Dec 30 -> Jan 3 is 4 days")

    def test_already_correct_srt_fields_are_never_rolled_again(self):
        """The correction must ONLY apply when was_guessed=True -- never
        to InvestorGain's own ~Srt_* fields, which already carry the
        right year. This is now enforced by the function itself (a
        required parameter), not just by caller discipline -- calling it
        with was_guessed=False (as the real code does for ~Srt_* fields)
        must leave the date completely untouched, even if the month
        comparison would otherwise suggest rolling it."""
        already_correct_close = "2027-01-03"  # genuinely correct, from ~Srt_Close
        open_d = "2026-12-30"
        result = s._roll_year_if_before_open(already_correct_close, open_d, was_guessed=False)
        self.assertEqual(result, already_correct_close, "must not double-roll an already-correct date")

    def test_same_year_dates_are_never_touched(self):
        open_d = "2026-08-27"
        close_d = "2026-08-29"
        self.assertEqual(s._roll_year_if_before_open(close_d, open_d, was_guessed=True), close_d)


class TestSubscriptionMergeNameNormalization(unittest.TestCase):
    """Bug (found 2026-09-21): report 333 (subscription) can spell a
    company name slightly differently (e.g. trailing whitespace) than
    report 331 (GMP) for the SAME company -- the exact same class of
    mismatch already known from report 377 (Symbiotec Pharmalab), but an
    earlier version of this codebase assumed (incorrectly) that reports
    331/333 were immune to it. An un-normalized merge silently fails,
    leaving every subscription field as None ("N/A" in the UI) even
    though the real data existed in the report all along."""

    def test_trailing_whitespace_in_subscription_report_still_merges(self):
        test_dir = _isolate_data_files(self)
        gmp_row = {
            "~ipo_status1": "O", "~IPO_Category": "IPO",
            "Name": '<a>NSE</a><span class="badge">IPO</span><span class="badge">O</span>',
            "GMP": "&#8377;<b>65</b> (3.64%)", "Sub": "5.71", "IPO Size": "&#8377;22561.57 Cr",
            "~Srt_Open": "2026-09-17", "~Srt_Close": "2026-09-21",
            "~Srt_BoA_Dt": "2026-09-22", "~Str_Listing": "2026-09-24", "Updated-On": "",
        }
        # The exact bug scenario: report 333 has a trailing space, 331 doesn't.
        sub_row = {
            "~IPO_Category": "IPO", "Name": "<a>NSE </a>",
            "Total": "5.71", "QIB": "12.68", "SHNI": "4.09",
            "BHNI": "7.78", "NII": "6.55", "RII": "1.39",
        }

        def fake_get(url, **kwargs):
            if "333" in url:
                return _mock_report_response([sub_row])
            if "377" in url:
                return _mock_report_response([])
            return _mock_report_response([gmp_row])

        with mock.patch("investorgain_scraper.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 9, 21)
            mock_dt.strptime = datetime.strptime
            with mock.patch.object(s.requests, "get", side_effect=fake_get):
                records = s._fetch_and_build_all_records()

        rec = records[0]
        self.assertIsNotNone(rec.subscription.qib, "QIB must merge despite the whitespace mismatch")
        self.assertEqual(rec.subscription.qib, 12.68)
        self.assertEqual(rec.subscription.shni, 4.09)
        self.assertTrue(rec.subscription.started)


class TestStatusDerivedFromDatesNotSiteFlag(unittest.TestCase):
    """Bug: InvestorGain's own ~ipo_status1 flag has been observed to lag
    -- staying "O" (open) for days after the real close/listing date has
    passed. Trusting it directly caused Open/Closed bucketing to be
    wrong. Fixed by deriving status from actual dates, only falling back
    to the site's flag when dates are unavailable."""

    def test_lagging_site_flag_is_overridden_by_real_dates(self):
        # Site says "O" (open), but the close date is 5 days in the past.
        fixed_now = datetime(2026, 9, 17)
        with mock.patch("investorgain_scraper.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.strptime = datetime.strptime
            status = s._derive_status("2026-09-10", "2026-09-12", "O")
        self.assertEqual(status, "closed", "real dates must override a stale site status flag")

    def test_genuinely_open_ipo_still_shows_open(self):
        fixed_now = datetime(2026, 9, 17)
        with mock.patch("investorgain_scraper.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.strptime = datetime.strptime
            status = s._derive_status("2026-09-15", "2026-09-19", "O")
        self.assertEqual(status, "open")


class TestClosedIpoArchiveRetention(unittest.TestCase):
    """Bug: InvestorGain's live report only keeps actively-tracked IPOs --
    once they stop tracking a listed IPO's GMP, it vanishes from their
    report entirely, and the Closed tab would silently lose it too, with
    no graceful fallback ("never updated / blank" bug report). Fixed with
    a local archive that keeps a listed IPO visible for
    CLOSED_IPO_RETENTION_DAYS after last being seen, then retires it."""

    def test_ipo_survives_disappearing_from_live_report_within_window(self):
        _isolate_data_files(self)
        rec = IPORecord(company_name="ListedCo", ipo_type="Mainboard", status="closed", gmp=50)

        day1 = datetime(2026, 9, 16, 10, 0)
        with mock.patch("investorgain_scraper.datetime") as mock_dt:
            mock_dt.now.return_value = day1
            mock_dt.fromisoformat = datetime.fromisoformat
            result_day1 = s._merge_closed_with_archive([rec])
        self.assertEqual([r.company_name for r in result_day1], ["ListedCo"])

        # Day 5: InvestorGain has dropped it entirely from the live report.
        day5 = day1 + timedelta(days=5)
        with mock.patch("investorgain_scraper.datetime") as mock_dt:
            mock_dt.now.return_value = day5
            mock_dt.fromisoformat = datetime.fromisoformat
            result_day5 = s._merge_closed_with_archive([])  # nothing live
        self.assertEqual(
            [r.company_name for r in result_day5], ["ListedCo"],
            "must still show the archived IPO within the retention window",
        )

    def test_ipo_retires_after_retention_window_expires(self):
        _isolate_data_files(self)
        rec = IPORecord(company_name="OldCo", ipo_type="Mainboard", status="closed", gmp=50)

        day1 = datetime(2026, 9, 16, 10, 0)
        with mock.patch("investorgain_scraper.datetime") as mock_dt:
            mock_dt.now.return_value = day1
            mock_dt.fromisoformat = datetime.fromisoformat
            s._merge_closed_with_archive([rec])

        far_future = day1 + timedelta(days=s.CLOSED_IPO_RETENTION_DAYS + 5)
        with mock.patch("investorgain_scraper.datetime") as mock_dt:
            mock_dt.now.return_value = far_future
            mock_dt.fromisoformat = datetime.fromisoformat
            result = s._merge_closed_with_archive([])
        self.assertEqual(result, [], "must be retired after the retention window passes")


class TestGmpDirectionStatePersistsAcrossDayRollover(unittest.TestCase):
    """Bug: the GMP-direction day-rollover branch used to REPLACE a
    company's entire state entry with a fresh dict containing only
    direction-tracking keys -- silently wiping out the "gmp_extremes"
    key that _update_gmp_extremes had stored there. This made
    Opening/Highest/Lowest reset to the same number every single day.
    Fixed by mutating the existing entry in place instead of replacing
    it wholesale."""

    def test_extremes_survive_a_day_boundary_the_direction_function_also_processes(self):
        state = {}
        day1 = datetime(2026, 9, 11, 10, 0)
        with mock.patch("investorgain_scraper.datetime") as mock_dt:
            mock_dt.now.return_value = day1
            s._update_gmp_direction("TestCo", 100, state)
            s._update_gmp_extremes("TestCo", 10.0, "open", state)

        day2 = datetime(2026, 9, 12, 10, 0)
        with mock.patch("investorgain_scraper.datetime") as mock_dt:
            mock_dt.now.return_value = day2
            # This call used to wipe out gmp_extremes as a side effect.
            s._update_gmp_direction("TestCo", 120, state)
            extremes = s._update_gmp_extremes("TestCo", 15.0, "open", state)

        self.assertEqual(extremes["opening"], 10.0, "opening must survive the day rollover, not reset to 15")
        self.assertEqual(extremes["highest"], 15.0)

    def test_extremes_freeze_permanently_once_listed(self):
        state = {}
        s._update_gmp_extremes("FreezeCo", 10.0, "open", state)
        s._update_gmp_extremes("FreezeCo", 20.0, "closed", state)
        frozen = s._update_gmp_extremes("FreezeCo", 20.0, "listed", state)
        self.assertEqual(frozen["highest"], 20.0)

        # Any further update after listing must be ignored entirely.
        after_listing = s._update_gmp_extremes("FreezeCo", 99.0, "listed", state)
        self.assertEqual(after_listing["highest"], 20.0, "must NOT update after the IPO has listed")
        self.assertEqual(after_listing["opening"], 10.0)


class TestSingleFetchPerRefreshCycle(unittest.TestCase):
    """Bug: each of Open/Upcoming/Closed used to call _scrape_gmp_report
    independently, each re-fetching the GMP report, subscription report,
    and performance report from scratch -- tripling every InvestorGain
    request per refresh cycle. Fixed with fetch_all_mainboard_data(),
    which fetches each report EXACTLY once and derives all three tabs
    from that single fetch."""

    def test_each_report_fetched_exactly_once(self):
        _isolate_data_files(self)
        gmp_row = {
            "~ipo_status1": "O", "~IPO_Category": "IPO",
            "Name": '<a>Co</a><span class="badge">IPO</span><span class="badge">O</span>',
            "GMP": "&#8377;<b>10</b> (5.00%)", "Sub": "1.0", "IPO Size": "&#8377;100.00 Cr",
            "~Srt_Open": "2026-09-15", "~Srt_Close": "2026-09-20",
            "~Srt_BoA_Dt": "2026-09-21", "~Str_Listing": "2026-09-23", "Updated-On": "",
        }
        call_counts = {"331": 0, "333": 0, "377": 0}

        def fake_get(url, **kwargs):
            if "333" in url:
                call_counts["333"] += 1
                return _mock_report_response([])
            if "377" in url:
                call_counts["377"] += 1
                return _mock_report_response([])
            call_counts["331"] += 1
            return _mock_report_response([gmp_row])

        with mock.patch("investorgain_scraper.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 9, 18)
            mock_dt.strptime = datetime.strptime
            mock_dt.fromisoformat = datetime.fromisoformat
            with mock.patch.object(s.requests, "get", side_effect=fake_get):
                s.fetch_all_mainboard_data()

        self.assertEqual(call_counts["331"], 1)
        self.assertEqual(call_counts["333"], 1)
        self.assertEqual(call_counts["377"], 1)


class TestSaveCacheSharedDirectionState(unittest.TestCase):
    """Efficiency fix: save_cache() used to load and save the GMP-
    direction state independently every time it was called (up to 3x per
    refresh cycle, once per tab). It now accepts a pre-loaded state dict
    so the caller can share ONE load/save across all three tabs -- while
    still being fully self-contained (loads/saves its own state) when
    called the old way, for backward compatibility with any standalone
    caller."""

    def test_standalone_call_still_self_manages_state(self):
        _isolate_data_files(self)
        rec = IPORecord(company_name="StandaloneCo", ipo_type="Mainboard", status="open", gmp=15, gmp_percent=5.0)
        s.save_cache([rec], "open")  # old-style call, no direction_state arg
        cache = s.load_cache("open")
        self.assertEqual(cache["records"][0]["company_name"], "StandaloneCo")
        self.assertTrue(s.GMP_DIRECTION_STATE_FILE.exists(), "must have self-saved its state")

    def test_shared_state_across_three_tabs_produces_correct_direction(self):
        _isolate_data_files(self)
        rec1 = IPORecord(company_name="SharedCo", ipo_type="Mainboard", status="open", gmp=50, gmp_percent=10.0)
        direction_state = s._load_gmp_direction_state()
        s.save_cache([rec1], "open", direction_state=direction_state, persist_direction_state=False)
        s._save_gmp_direction_state(direction_state)

        rec2 = IPORecord(company_name="SharedCo", ipo_type="Mainboard", status="open", gmp=60, gmp_percent=12.0)
        direction_state2 = s._load_gmp_direction_state()
        s.save_cache([rec2], "open", direction_state=direction_state2, persist_direction_state=False)
        s._save_gmp_direction_state(direction_state2)

        cache = s.load_cache("open")
        self.assertEqual(cache["records"][0]["gmp_direction"], "up")


class TestRegistrarCacheBackwardCompatibility(unittest.TestCase):
    """The registrar cache format changed from a plain string to a dict
    (to also hold price_band_floor/cap) after the feature shipped. Old
    cache entries from before that change must still work, not crash or
    silently drop the registrar name."""

    def test_old_string_format_cache_entry_still_reads_correctly(self):
        _isolate_data_files(self)
        s.REGISTRAR_CACHE_FILE.write_text(json.dumps({"12345": "Bigshare Services Pvt.Ltd."}), encoding="utf-8")

        rec = IPORecord(company_name="LegacyCo", ipo_type="Mainboard")
        row = {"~urlrewrite_folder_name": "/gmp/legacy-co-ipo/12345/"}
        s.enrich_with_registrar([rec], {"LegacyCo": row})

        self.assertEqual(rec.registrar, "Bigshare Services Pvt.Ltd.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
