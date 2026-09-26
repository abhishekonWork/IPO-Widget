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

    PYTHONPATH=scraper python3 -m unittest test_regressions.py -v   (run from the repo root)

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


class TestDetailPageUrlForViewDetailsButton(unittest.TestCase):
    """Feature (2026-09-26): a "View Details" link on each card to this
    IPO's own InvestorGain page (e.g.
    https://www.investorgain.com/ipo/moneyview-ipo/2198/). Built from the
    same slug+id already read from the report row for registrar
    enrichment -- no extra network fetch."""

    def test_detail_page_url_set_from_row_slug_and_id(self):
        _isolate_data_files(self)
        rec = IPORecord(company_name="Moneyview", ipo_type="Mainboard")
        row = {"~urlrewrite_folder_name": "/gmp/moneyview-ipo/2198/"}
        s.enrich_with_registrar([rec], {"Moneyview": row})
        self.assertEqual(rec.detail_page_url, "https://www.investorgain.com/ipo/moneyview-ipo/2198/")

    def test_no_url_when_slug_or_id_unavailable(self):
        _isolate_data_files(self)
        rec = IPORecord(company_name="NoSlugCo", ipo_type="Mainboard")
        s.enrich_with_registrar([rec], {"NoSlugCo": {"~urlrewrite_folder_name": ""}})
        self.assertIsNone(rec.detail_page_url, "never a guessed/fabricated link")

    def test_carried_forward_when_a_later_cycle_cannot_read_it(self):
        _isolate_data_files(self)
        rec = IPORecord(company_name="Moneyview", ipo_type="Mainboard", detail_page_url=None)
        s._carry_forward_missing_enrichment([rec])  # no previous cache/archive yet
        self.assertIsNone(rec.detail_page_url)

        cache = {"open": {"records": [{"company_name": "Moneyview", "detail_page_url": "https://www.investorgain.com/ipo/moneyview-ipo/2198/"}]}}
        s.CACHE_FILE.write_text(json.dumps(cache), encoding="utf-8")
        rec2 = IPORecord(company_name="Moneyview", ipo_type="Mainboard", detail_page_url=None)
        s._carry_forward_missing_enrichment([rec2])
        self.assertEqual(rec2.detail_page_url, "https://www.investorgain.com/ipo/moneyview-ipo/2198/")


class TestEnrichmentReportFailureKeepsLastGoodData(unittest.TestCase):
    """Bug (found live 2026-09-24): the site sometimes showed "N/A" for the
    whole subscription breakdown (QIB/SHNI/BHNI/NII/Retail) while GMP
    looked fine. Reports 333 (subscription) and 377 (listing performance)
    fail SOFT -- if one was unreachable, returned zero rows, or stopped
    listing a closed IPO, the fresh record had no breakdown and save_cache()
    overwrote the good cached values with N/A every cycle. A failed report
    331 already fell back to the last good cache; 333/377 did not.

    Fix: _carry_forward_missing_enrichment reuses the last REAL value when
    the fresh breakdown is completely missing -- never overriding a fresh
    value, never guessing."""

    GMP_ROW = {
        "~ipo_status1": "O", "~IPO_Category": "IPO",
        "Name": '<a>NSE</a><span class="badge">IPO</span><span class="badge">O</span>',
        "GMP": "&#8377;<b>65</b> (3.64%)", "Sub": "5.71", "IPO Size": "&#8377;22561.57 Cr",
        "~Srt_Open": "2026-09-17", "~Srt_Close": "2026-09-21",
        "~Srt_BoA_Dt": "2026-09-22", "~Str_Listing": "2026-09-24", "Updated-On": "",
    }
    SUB_ROW = {
        "~IPO_Category": "IPO", "Name": "<a>NSE</a>",
        "Total": "5.71", "QIB": "12.68", "SHNI": "4.09",
        "BHNI": "7.78", "NII": "6.55", "RII": "1.39",
    }

    def _run_cycle(self, sub_behaviour, day=datetime(2026, 9, 21)):
        """sub_behaviour: a list of rows for a normal 333 response, or an
        Exception instance to simulate 333 being unreachable."""
        def fake_get(url, **kwargs):
            if "333" in url:
                if isinstance(sub_behaviour, Exception):
                    raise sub_behaviour
                return _mock_report_response(sub_behaviour)
            if "377" in url:
                return _mock_report_response([])
            return _mock_report_response([self.GMP_ROW])

        with mock.patch("investorgain_scraper.datetime") as mock_dt:
            mock_dt.now.return_value = day
            mock_dt.strptime = datetime.strptime
            mock_dt.fromisoformat = datetime.fromisoformat
            with mock.patch.object(s.requests, "get", side_effect=fake_get):
                with mock.patch("investorgain_scraper.time.sleep"):  # skip the real retry backoff in tests
                    return s._fetch_and_build_all_records()

    def test_unreachable_subscription_report_keeps_previous_breakdown(self):
        _isolate_data_files(self)
        good = self._run_cycle([self.SUB_ROW])
        s.save_cache(good, "open", direction_state={}, persist_direction_state=False)
        self.assertEqual(good[0].subscription.qib, 12.68)

        bad = self._run_cycle(s.requests.ConnectionError("boom"))
        rec = bad[0]
        self.assertEqual(rec.subscription.qib, 12.68, "QIB must not turn into N/A because report 333 failed")
        self.assertEqual(rec.subscription.shni, 4.09)
        self.assertEqual(rec.subscription.bhni, 7.78)
        self.assertEqual(rec.subscription.nii, 6.55)
        self.assertEqual(rec.subscription.retail, 1.39)
        self.assertTrue(rec.subscription.started)
        self.assertFalse(s.report_status()["333"]["ok"], "the failure must be visible via /api/health")
        self.assertIn("boom", s.report_status()["333"]["error"])

    def test_empty_subscription_report_keeps_previous_breakdown(self):
        _isolate_data_files(self)
        s.save_cache(self._run_cycle([self.SUB_ROW]), "open", direction_state={}, persist_direction_state=False)
        rec = self._run_cycle([])[0]  # 333 answers fine but no longer lists the IPO
        self.assertEqual(rec.subscription.qib, 12.68)
        self.assertTrue(s.report_status()["333"]["ok"])
        self.assertEqual(s.report_status()["333"]["row_count"], 0)

    def test_closed_ipo_dropped_from_report_keeps_breakdown_via_archive(self):
        _isolate_data_files(self)
        good = self._run_cycle([self.SUB_ROW])
        s._merge_closed_with_archive(good)  # archive holds the last good record
        # Later: IPO is closed, cache was wiped (e.g. Render restart), 333 dropped it.
        s.CACHE_FILE.unlink(missing_ok=True)
        with mock.patch("investorgain_scraper.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 9, 25)
            mock_dt.strptime = datetime.strptime
            mock_dt.fromisoformat = datetime.fromisoformat
            rec = s.IPORecord(company_name="NSE", ipo_type="Mainboard", subscription=Subscription(total=5.71, started=True))
            s._carry_forward_missing_enrichment([rec])
        self.assertEqual(rec.subscription.qib, 12.68)

    def test_fresh_values_are_never_overridden_by_old_ones(self):
        _isolate_data_files(self)
        s.save_cache(self._run_cycle([self.SUB_ROW]), "open", direction_state={}, persist_direction_state=False)
        newer = dict(self.SUB_ROW, QIB="20.00")
        rec = self._run_cycle([newer])[0]
        self.assertEqual(rec.subscription.qib, 20.0)

    def test_no_history_means_no_invented_numbers(self):
        _isolate_data_files(self)
        rec = self._run_cycle(s.requests.ConnectionError("down"))[0]
        self.assertIsNone(rec.subscription.qib, "with nothing ever fetched, stay None (shows N/A), never a guess")


class TestTransientReportErrorRetriesOnce(unittest.TestCase):
    """Bug (found live 2026-09-24/25): InvestorGain occasionally returns a
    522 (Cloudflare: origin didn't respond) or a read timeout for a few
    seconds, then works again immediately after. Fixed by retrying once,
    after a short backoff, on exactly this class of transient failure --
    so a multi-second hiccup no longer costs a full 2-minute refresh
    cycle of carried-forward (stale) data."""

    def test_522_then_success_returns_real_data_not_carried_forward(self):
        _isolate_data_files(self)
        row = {
            "~ipo_status1": "O", "~IPO_Category": "IPO",
            "Name": '<a>Co</a><span class="badge">IPO</span><span class="badge">O</span>',
            "GMP": "&#8377;<b>10</b> (5.00%)", "Sub": "1.0", "IPO Size": "&#8377;100.00 Cr",
            "~Srt_Open": "2026-09-15", "~Srt_Close": "2026-09-20",
            "~Srt_BoA_Dt": "2026-09-21", "~Str_Listing": "2026-09-23", "Updated-On": "",
        }
        calls = {"n": 0}
        error_522 = s.requests.exceptions.HTTPError("522 Server Error")
        error_522.response = mock.Mock(status_code=522)

        def fake_get(url, **kwargs):
            if "331" in url:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise error_522
                return _mock_report_response([row])
            return _mock_report_response([])

        with mock.patch("investorgain_scraper.time.sleep"):
            with mock.patch.object(s.requests, "get", side_effect=fake_get):
                rows = s.fetch_report(s.GMP_REPORT_ID)

        self.assertEqual(calls["n"], 2, "must retry exactly once after the transient 522")
        self.assertEqual(len(rows), 1)
        self.assertTrue(s.report_status()[str(s.GMP_REPORT_ID)]["ok"])

    def test_404_is_not_retried(self):
        _isolate_data_files(self)
        calls = {"n": 0}
        error_404 = s.requests.exceptions.HTTPError("404 Not Found")
        error_404.response = mock.Mock(status_code=404)

        def fake_get(url, **kwargs):
            calls["n"] += 1
            raise error_404

        with mock.patch("investorgain_scraper.time.sleep") as mock_sleep:
            with mock.patch.object(s.requests, "get", side_effect=fake_get):
                with self.assertRaises(s.requests.exceptions.HTTPError):
                    s.fetch_report(s.GMP_REPORT_ID)

        self.assertEqual(calls["n"], 1, "a 4xx must not be retried")
        mock_sleep.assert_not_called()


class TestPlaceholderGmpNeverBecomesFakeZeroPercent(unittest.TestCase):
    """Bug (found live 2026-09-25, real example: Acevector/Snapdeal IPO):
    before any market maker quotes a real premium, InvestorGain's GMP
    cell is literally "--(0.00%)" -- a placeholder, not a real 0%
    reading. Parsing "(0.00%)" at face value produced a fake 0.0 that,
    via _update_gmp_extremes, permanently locked "Opening GMP" at 0% --
    even once a real premium (e.g. 6.25%) appeared later. Fixed by only
    trusting the percent when a real rupee value is also present."""

    def _gmp_row(self, gmp_cell, **overrides):
        row = {
            "~ipo_status1": "O", "~IPO_Category": "IPO",
            "Name": '<a>Acevector</a><span class="badge">IPO</span><span class="badge">O</span>',
            "GMP": gmp_cell, "Sub": "1.0", "IPO Size": "\u20b9100.00 Cr",
            "~Srt_Open": "2026-09-15", "~Srt_Close": "2026-09-20",
            "~Srt_BoA_Dt": "2026-09-21", "~Str_Listing": "2026-09-23", "Updated-On": "",
        }
        row.update(overrides)
        return row

    def _build(self, gmp_cell, day=datetime(2026, 9, 16)):
        def fake_get(url, **kwargs):
            if "331" in url:
                return _mock_report_response([self._gmp_row(gmp_cell)])
            return _mock_report_response([])

        with mock.patch("investorgain_scraper.datetime") as mock_dt:
            mock_dt.now.return_value = day
            mock_dt.strptime = datetime.strptime
            mock_dt.fromisoformat = datetime.fromisoformat
            with mock.patch.object(s.requests, "get", side_effect=fake_get):
                return s._fetch_and_build_all_records()[0]

    def test_placeholder_dash_gmp_yields_none_percent_not_zero(self):
        rec = self._build("--(0.00%)")
        self.assertIsNone(rec.gmp)
        self.assertIsNone(rec.gmp_percent, "a placeholder must not masquerade as a real 0.00%")

    def test_real_zero_gmp_with_actual_rupee_value_is_kept(self):
        rec = self._build("\u20b90 (0.00%)")
        self.assertEqual(rec.gmp, 0)
        self.assertEqual(rec.gmp_percent, 0.0, "a genuine \u20b90 flat premium IS real data and must be kept")

    def test_real_premium_still_parses_normally(self):
        rec = self._build("\u20b9<b>25</b> (6.25%)")
        self.assertEqual(rec.gmp, 25)
        self.assertEqual(rec.gmp_percent, 6.25)

    def test_placeholder_never_locks_opening_extreme(self):
        state = {}
        extremes = s._update_gmp_extremes("Acevector", None, "open", state)
        self.assertIsNone(extremes["opening"], "no real reading yet -- opening must stay unset")
        extremes = s._update_gmp_extremes("Acevector", 6.25, "open", state)
        self.assertEqual(extremes["opening"], 6.25, "the first REAL reading becomes opening")


class TestLegacyZeroOpeningRepair(unittest.TestCase):
    """One-time repair for state files already corrupted by the bug above
    (opening wrongly locked at 0.0 before the fix existed). Since
    "opening" is by design set once and never again, the parsing fix
    alone can't correct an IPO already affected -- this repairs the
    persisted state directly, exactly once."""

    def test_partial_corruption_only_resets_opening(self):
        # highest already moved to a real 6.25 -- proof a real reading came
        # in and correctly updated it. Only "opening" is stuck (by design it
        # locks on first-ever value and never updates again), so only it
        # should be reset; highest/lowest are already correct and untouched.
        state = {
            "Acevector": {"gmp_extremes": {"opening": 0.0, "highest": 6.25, "lowest": 0.0, "frozen": False}},
        }
        repaired = s._repair_legacy_zero_gmp_openings(state)
        self.assertIsNone(repaired["Acevector"]["gmp_extremes"]["opening"])
        self.assertEqual(repaired["Acevector"]["gmp_extremes"]["highest"], 6.25, "already-real highest is untouched")
        self.assertEqual(repaired["Acevector"]["gmp_extremes"]["lowest"], 0.0, "not reset -- ambiguous without more info")

    def test_full_corruption_resets_opening_highest_and_lowest(self):
        # GMP never moved away from the fake reading at all -- opening,
        # highest AND lowest are all still exactly 0.0, meaning no real
        # reading has landed yet. All three need to re-establish themselves.
        state = {
            "NewCo": {"gmp_extremes": {"opening": 0.0, "highest": 0.0, "lowest": 0.0, "frozen": False}},
        }
        repaired = s._repair_legacy_zero_gmp_openings(state)
        ex = repaired["NewCo"]["gmp_extremes"]
        self.assertIsNone(ex["opening"])
        self.assertIsNone(ex["highest"])
        self.assertIsNone(ex["lowest"])

    def test_frozen_listed_ipo_is_never_touched(self):
        state = {
            "AlreadyListed": {"gmp_extremes": {"opening": 0.0, "highest": 10.0, "lowest": 0.0, "frozen": True}},
        }
        repaired = s._repair_legacy_zero_gmp_openings(state)
        self.assertEqual(repaired["AlreadyListed"]["gmp_extremes"]["opening"], 0.0, "listed IPOs are permanent history")

    def test_runs_only_once(self):
        state = {
            "Co": {"gmp_extremes": {"opening": 0.0, "highest": 5.0, "lowest": 0.0, "frozen": False}},
        }
        once = s._repair_legacy_zero_gmp_openings(state)
        once["Co"]["gmp_extremes"]["opening"] = 0.0  # simulate a genuinely real 0.00% opening recorded AFTER repair
        twice = s._repair_legacy_zero_gmp_openings(once)
        self.assertEqual(twice["Co"]["gmp_extremes"]["opening"], 0.0, "a real post-repair 0.00% must not be wiped again")

    def test_nonzero_opening_is_left_alone(self):
        state = {
            "Co": {"gmp_extremes": {"opening": 3.5, "highest": 6.0, "lowest": 3.5, "frozen": False}},
        }
        repaired = s._repair_legacy_zero_gmp_openings(state)
        self.assertEqual(repaired["Co"]["gmp_extremes"]["opening"], 3.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
