"""Unit tests for pushcv's pure helpers.

These cover the parsing/formatting functions that have no network or database
dependency — the safest, highest-value things to test. New contributors: this
file is the pattern to follow when expanding coverage (see CONTRIBUTING.md).
"""
import pytest

from pushcv.ai_engine import (
    _clean_estimate_line,
    _condense_estimate,
    currency_for_location,
)
from pushcv.scraper import _linkedin_job_id, normalize_linkedin_url
from pushcv.search import _format_amount, _parse_amounts, _trim_outliers


# --------------------------------------------------------------------------- #
# LinkedIn URL normalization
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw, expected_id",
    [
        ("https://www.linkedin.com/jobs/view/123456/", "123456"),
        ("https://www.linkedin.com/jobs/view/senior-engineer-at-acme-3999888777", "3999888777"),
        ("https://www.linkedin.com/jobs/search/?currentJobId=3812345678&keywords=x", "3812345678"),
    ],
)
def test_linkedin_job_id_extraction(raw, expected_id):
    assert _linkedin_job_id(raw) == expected_id


def test_normalize_linkedin_url_canonical_form():
    url = "https://www.linkedin.com/jobs/search/?currentJobId=42&foo=bar"
    assert normalize_linkedin_url(url) == "https://www.linkedin.com/jobs/view/42/"


def test_linkedin_job_id_raises_when_absent():
    with pytest.raises(ValueError):
        _linkedin_job_id("https://example.com/not-a-linkedin-job")


# --------------------------------------------------------------------------- #
# Currency inference
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "location, expected_prefix",
    [
        ("Bengaluru, India", "INR"),
        ("London, United Kingdom", "GBP"),
        ("Berlin, Germany", "EUR"),
        ("Toronto, Canada", "CAD"),
        ("", "USD"),                       # empty -> default
        ("Austin, Texas", "USD"),          # unrecognized US city -> default
    ],
)
def test_currency_for_location(location, expected_prefix):
    assert currency_for_location(location).startswith(expected_prefix)


# --------------------------------------------------------------------------- #
# Salary parsing & formatting
# --------------------------------------------------------------------------- #
def test_parse_amounts_reads_k_suffix():
    assert _parse_amounts("pays $120k to $140k base", "$") == [120000.0, 140000.0]


def test_parse_amounts_ignores_other_currencies():
    # Only figures in the requested symbol are returned.
    assert _parse_amounts("₹25L or $90k", "$") == [90000.0]


def test_parse_amounts_drops_implausibly_small_figures():
    # Below the plausibility floor (e.g. an hourly/monthly number) is dropped.
    assert _parse_amounts("$50/hr", "$") == []


@pytest.mark.parametrize(
    "value, symbol, expected",
    [
        (2500000, "₹", "₹25L"),      # INR reads in lakhs
        (140000, "$", "$140k"),
        (1500000, "$", "$1.5M"),
    ],
)
def test_format_amount(value, symbol, expected):
    assert _format_amount(value, symbol) == expected


def test_trim_outliers_drops_stray_high_value():
    trimmed = _trim_outliers([100.0, 110.0, 120.0, 130.0, 10000.0])
    assert 10000.0 not in trimmed
    assert 100.0 in trimmed


def test_trim_outliers_noop_on_small_sample():
    # Fewer than 4 points: returned unchanged (can't compute a stable IQR).
    assert _trim_outliers([100.0, 5000.0]) == [100.0, 5000.0]


# --------------------------------------------------------------------------- #
# Estimate line cleanup
# --------------------------------------------------------------------------- #
def test_condense_estimate_picks_money_line_and_strips_tail():
    raw = "Here is the estimate:\n$120k - $140k base (based on market data)"
    assert _condense_estimate(raw) == "$120k - $140k base"


def test_clean_estimate_line_strips_trailing_explanation():
    assert _clean_estimate_line("₹25L - ₹35L base, based on levels.fyi") == "₹25L - ₹35L base"


def test_condense_estimate_handles_empty():
    assert _condense_estimate("") == "Estimate unavailable"
