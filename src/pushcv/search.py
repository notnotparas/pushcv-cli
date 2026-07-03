"""Web-search helpers for pushcv.

Pulls live compensation data from DuckDuckGo. Two consumers:

* :func:`get_salary_snippets` — raw snippet text to ground the optional AI
  salary synthesis.
* :func:`extract_salary` — a dependency-free estimator that parses figures
  straight from the snippets and cites the source (the default path; no model
  required).
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

# `duckduckgo_search` was renamed to `ddgs`; the old package's backend is now
# broken, so prefer the maintained one and fall back only if it's absent.
try:
    from ddgs import DDGS
except ImportError:  # pragma: no cover - legacy fallback
    from duckduckgo_search import DDGS

# Number of search results to fold into the salary context.
MAX_RESULTS = 5
# DuckDuckGo's free endpoint rate-limits / returns empty intermittently, so
# retry a few times with backoff before giving up (avoids the user having to
# re-run `status` until it happens to succeed).
SEARCH_RETRIES = 3
SEARCH_RETRY_DELAY = 1.5  # seconds, multiplied by the attempt number


def _search(query: str) -> List[Dict[str, Any]]:
    """Run a DuckDuckGo text search with retries, returning [] if all fail."""
    for attempt in range(SEARCH_RETRIES):
        try:
            with DDGS() as ddgs:
                results = ddgs.text(query, max_results=MAX_RESULTS) or []
            if results:
                return results
        except Exception:
            # Rate limit, network error, or backend change — retry below.
            pass
        if attempt < SEARCH_RETRIES - 1:
            time.sleep(SEARCH_RETRY_DELAY * (attempt + 1))
    return []


def _salary_query(
    job_title: str, company: str, location: str, experience: str = ""
) -> str:
    """Build the salary search query, optionally anchored to a seniority level."""
    parts = [job_title, experience, company, location, "salary compensation range"]
    return " ".join(p for p in parts if p).strip()


def get_salary_snippets(
    job_title: str, company: str, location: str, experience: str = ""
) -> str:
    """Return concatenated web snippets about pay for a role.

    Joins the ``body`` text of the top results into a single context string;
    empty if the search yielded nothing.
    """
    results = _search(_salary_query(job_title, company, location, experience))
    snippets = [
        r["body"].strip()
        for r in results
        if isinstance(r, dict) and r.get("body")
    ]
    return "\n\n".join(snippets)


# --------------------------------------------------------------------------- #
# Non-LLM extraction
# --------------------------------------------------------------------------- #
# Multipliers for the scale word following a figure (e.g. "44 lakhs", "$120k").
_UNIT_SCALE = {
    "k": 1e3, "thousand": 1e3,
    "m": 1e6, "mn": 1e6, "million": 1e6,
    "l": 1e5, "lac": 1e5, "lakh": 1e5, "lakhs": 1e5, "lpa": 1e5,
    "cr": 1e7, "crore": 1e7, "crores": 1e7,
}

# A currency-symbol-prefixed amount with an optional scale word, e.g.
# "₹44.2 lakhs", "$120k", "£85,000", "€90k". Requiring the symbol filters out
# stray counts/years ("139 profiles", "2026").
_MONEY_RE = re.compile(
    r"(?P<sym>[$€£₹])\s?(?P<num>\d[\d,]*(?:\.\d+)?)\s?"
    r"(?P<unit>lakhs?|lpa|lac|crores?|cr|thousand|million|mn|[kml])?",
    re.IGNORECASE,
)

# Minimum plausible *annual* salary per currency — figures below this are noise
# (monthly amounts, hourly rates, partial numbers). INR salaries start ~1 lakh;
# ¥ runs large; everything else ~10k. Keeps "₹40,000/mo" from rendering "₹0L".
_MIN_PLAUSIBLE = {"₹": 1e5, "¥": 1e6}
_DEFAULT_MIN_PLAUSIBLE = 1e4


def _parse_amounts(text: str, symbol: str) -> List[float]:
    """Extract absolute monetary amounts in ``symbol`` from free text."""
    floor = _MIN_PLAUSIBLE.get(symbol, _DEFAULT_MIN_PLAUSIBLE)
    amounts: List[float] = []
    for match in _MONEY_RE.finditer(text):
        if match.group("sym") != symbol:
            continue
        value = float(match.group("num").replace(",", ""))
        unit = (match.group("unit") or "").lower()
        value *= _UNIT_SCALE.get(unit, 1.0)
        if value >= floor:
            amounts.append(value)
    return amounts


def _format_amount(value: float, symbol: str) -> str:
    """Render an absolute amount in compact local notation."""
    if symbol == "₹":
        # Indian salaries read naturally in lakhs.
        return f"₹{value / 1e5:.0f}L"
    if value >= 1e6:
        return f"{symbol}{value / 1e6:.1f}M".replace(".0M", "M")
    if value >= 1e3:
        return f"{symbol}{value / 1e3:.0f}k"
    return f"{symbol}{value:.0f}"


def _domain(url: str) -> str:
    """Return a bare hostname (no leading www.) for source attribution."""
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


# Reputable salary sources, preferred over generic job-board aggregators (which
# tend to surface stray, mislevelled, or partial figures).
_TRUSTED_SALARY_SOURCES = (
    "levels.fyi", "glassdoor", "ambitionbox", "payscale", "salary.com",
    "indeed", "comparably", "6figr", "builtin", "ziprecruiter", "talent.com",
    "salaryexpert", "wellfound", "linkedin",
)


def _is_trusted_source(domain: str) -> bool:
    return any(t in domain for t in _TRUSTED_SALARY_SOURCES)


def _trim_outliers(amounts: List[float]) -> List[float]:
    """Drop statistical outliers (IQR rule) so one stray figure can't blow up
    the range. Returns the input unchanged when there are too few points."""
    if len(amounts) < 4:
        return amounts
    ordered = sorted(amounts)
    q1 = ordered[len(ordered) // 4]
    q3 = ordered[(3 * len(ordered)) // 4]
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    trimmed = [a for a in ordered if lo <= a <= hi]
    return trimmed or ordered


def extract_salary(
    job_title: str,
    company: str,
    location: str,
    currency_symbol: str = "$",
    experience: str = "",
) -> Optional[str]:
    """Estimate pay directly from web snippets, no LLM involved.

    Searches for the role (anchored to ``experience`` when given), parses figures
    in ``currency_symbol``, drops outliers, and returns a sourced range like
    ``"₹27L - ₹130L · per ambitionbox.com"``. ``None`` if no usable figures.
    """
    results = _search(_salary_query(job_title, company, location, experience))

    # Group parsed figures by source, keeping reputable salary sites separate
    # from generic aggregators.
    trusted: List[tuple] = []
    fallback: List[tuple] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        found = _parse_amounts(result.get("body") or "", currency_symbol)
        if not found:
            continue
        domain = _domain(result.get("href") or "")
        (trusted if _is_trusted_source(domain) else fallback).append((domain, found))

    # Prefer trusted sources entirely; only fall back to aggregators if needed.
    pool = trusted or fallback
    if not pool:
        return None

    amounts = _trim_outliers([value for _, values in pool for value in values])
    source = pool[0][0]  # cite the top-ranked contributing source
    low, high = min(amounts), max(amounts)
    estimate = _format_amount(low, currency_symbol)
    if high > low:
        estimate += f" - {_format_amount(high, currency_symbol)}"
    if source:
        estimate += f" · per {source}"
    return estimate
