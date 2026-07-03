"""Job-posting fetching and parsing for pushcv.

This module pairs ``curl_cffi`` (HTTP fetching with browser TLS/JA3
impersonation, which sidesteps the bot walls many job boards put up) with
``beautifulsoup4`` (HTML parsing) to turn a posting URL into the structured
fields pushcv tracks.

The public surface is intentionally small:

* :func:`fetch` — retrieve raw HTML for a URL.
* :func:`parse` — extract a :class:`ScrapedPosting` from HTML.
* :func:`scrape` — convenience wrapper that fetches then parses.
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from bs4 import BeautifulSoup
from bs4 import Tag
from curl_cffi import requests

# Browser profile curl_cffi impersonates by default. Chrome's fingerprint is
# the most widely accepted and least likely to be challenged.
DEFAULT_IMPERSONATE = "chrome"

# Network timeout (seconds) for a single fetch.
DEFAULT_TIMEOUT = 30


@dataclass
class ScrapedPosting:
    """The fields extracted from a job posting.

    ``company`` and ``title`` map directly onto
    :class:`pushcv.models.JobApplication`; ``url`` is the canonical link the
    posting was scraped from. Any field may be ``None`` when the page does not
    expose it in a recognizable way.
    """

    url: str
    title: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None


def fetch(
    url: str,
    *,
    impersonate: str = DEFAULT_IMPERSONATE,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """Fetch ``url`` and return the response body as text.

    Uses curl_cffi's browser impersonation so the request presents a realistic
    TLS/JA3 fingerprint. Raises for any non-2xx HTTP status.
    """
    response = requests.get(url, impersonate=impersonate, timeout=timeout)
    response.raise_for_status()
    return response.text


def _clean(value: Optional[str]) -> Optional[str]:
    """Collapse whitespace and strip, returning ``None`` for empty results."""
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _meta(soup: BeautifulSoup, *, prop: str) -> Optional[str]:
    """Return the ``content`` of an OpenGraph/meta tag, if present."""
    tag = soup.find("meta", attrs={"property": prop}) or soup.find(
        "meta", attrs={"name": prop}
    )
    if tag is None:
        return None
    content = tag.get("content")
    return _clean(content) if isinstance(content, str) else None


def parse(html: str, *, url: str) -> ScrapedPosting:
    """Parse posting ``html`` into a :class:`ScrapedPosting`.

    Extraction is best-effort and leans on conventional signals (OpenGraph
    metadata, the document ``<title>``, the first ``<h1>``). Site-specific
    selectors can be layered on top of this baseline as they are needed.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Title: prefer the OpenGraph title, then the first <h1>, then <title>.
    title = _meta(soup, prop="og:title")
    if title is None and soup.h1 is not None:
        title = _clean(soup.h1.get_text())
    if title is None and soup.title is not None:
        title = _clean(soup.title.get_text())

    # Company: OpenGraph site name is the most reliable generic source.
    company = _meta(soup, prop="og:site_name")

    description = _meta(soup, prop="og:description")

    return ScrapedPosting(
        url=url,
        title=title,
        company=company,
        description=description,
    )


def scrape(
    url: str,
    *,
    impersonate: str = DEFAULT_IMPERSONATE,
    timeout: int = DEFAULT_TIMEOUT,
) -> ScrapedPosting:
    """Fetch ``url`` and parse it into a :class:`ScrapedPosting`."""
    html = fetch(url, impersonate=impersonate, timeout=timeout)
    return parse(html, url=url)


# --------------------------------------------------------------------------- #
# LinkedIn jobs
# --------------------------------------------------------------------------- #
# LinkedIn aggressively bounces "desktop" clients toward authenticated/redirect
# walls. Presenting as Mobile Safari on iOS lets us reach the public,
# server-rendered guest job view that still embeds JSON-LD structured data.
LINKEDIN_IMPERSONATE = "safari_ios"

# The canonical /jobs/view/{id}/ page is frequently served as an auth-wall
# (login modal) to guests — it returns HTTP 200 but contains no JobPosting data.
# The guest fragment endpoint returns the server-rendered posting markup without
# the wall, and is the reliable source when JSON-LD is missing.
LINKEDIN_GUEST_API = (
    "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
)

# Exact header set of a real iPhone running Safari. curl_cffi sets a matching
# TLS/JA3 fingerprint via ``impersonate``; these HTTP headers complete the
# disguise so the request is internally consistent with an iOS device.
IPHONE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_3_1 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# CSS classes LinkedIn uses on the external/direct "apply" anchor across its
# guest-view variants.
_APPLY_BUTTON_SELECTOR = (
    "a.apply-button, "
    "a[class*='apply-button'], "
    "a.sign-up-modal__direct-apply-on-company-site, "
    "a[data-tracking-control-name*='apply']"
)


def normalize_linkedin_url(raw_url: str) -> str:
    """Reduce any LinkedIn job URL to its canonical ``/jobs/view/{id}/`` form.

    The numeric job ID is pulled, in priority order, from:

    1. the ``currentJobId`` query parameter (collection / search URLs), then
    2. the ``/jobs/view/<slug-or-id>`` path (the trailing digits are the ID).

    Raises :class:`ValueError` if no job ID can be located.
    """
    return f"https://www.linkedin.com/jobs/view/{_linkedin_job_id(raw_url)}/"


def _linkedin_job_id(raw_url: str) -> str:
    """Extract the numeric LinkedIn job ID from any job URL.

    Raises :class:`ValueError` if no job ID can be located.
    """
    # 1. Search/collection URLs carry the active posting in ?currentJobId=...
    match = re.search(r"[?&]currentJobId=(\d+)", raw_url)

    # 2. Otherwise the ID is the trailing run of digits on the /jobs/view/ path,
    #    whether the path is a bare ID or a "<title>-at-<company>-<id>" slug.
    if match is None:
        match = re.search(r"/jobs/view/(?:[^/?#]*?-)?(\d+)", raw_url)

    if match is None:
        raise ValueError(f"Could not extract a LinkedIn job ID from: {raw_url!r}")

    return match.group(1)


def _iter_json_ld(soup: BeautifulSoup) -> List[Any]:
    """Yield every successfully decoded ``application/ld+json`` payload."""
    payloads: List[Any] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        try:
            payloads.append(json.loads(raw))
        except (json.JSONDecodeError, ValueError):
            # Malformed block — skip it rather than failing the whole scrape.
            continue
    return payloads


def _find_job_posting(payloads: List[Any]) -> Optional[Dict[str, Any]]:
    """Return the first JobPosting node found across JSON-LD payloads.

    Handles bare objects, top-level lists, and ``@graph`` containers.
    """

    def _is_job(node: Any) -> bool:
        if not isinstance(node, dict):
            return False
        node_type = node.get("@type")
        if isinstance(node_type, list):
            return "JobPosting" in node_type
        return node_type == "JobPosting"

    def _walk(node: Any) -> Optional[Dict[str, Any]]:
        if _is_job(node):
            return node
        if isinstance(node, dict):
            for value in node.get("@graph", []) if "@graph" in node else node.values():
                found = _walk(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = _walk(item)
                if found is not None:
                    return found
        return None

    for payload in payloads:
        found = _walk(payload)
        if found is not None:
            return found
    return None


def _org_name(hiring_org: Any) -> Optional[str]:
    """Pull ``name`` from a ``hiringOrganization`` node (dict or list)."""
    if isinstance(hiring_org, list):
        hiring_org = hiring_org[0] if hiring_org else None
    if isinstance(hiring_org, dict):
        return _clean(hiring_org.get("name"))
    return None


def _format_address(address: Any) -> Optional[str]:
    """Render a schema.org PostalAddress (or string) as a readable location."""
    if isinstance(address, str):
        return _clean(address)
    if not isinstance(address, dict):
        return None
    parts = [
        address.get("addressLocality"),
        address.get("addressRegion"),
        address.get("addressCountry"),
    ]
    joined = ", ".join(p for p in parts if isinstance(p, str) and p.strip())
    return _clean(joined)


def _job_location(job_location: Any) -> Optional[str]:
    """Extract the address from ``jobLocation`` (``jobLocation[0].address``)."""
    if isinstance(job_location, list):
        job_location = job_location[0] if job_location else None
    if isinstance(job_location, dict):
        return _format_address(job_location.get("address"))
    return None


def _strip_html(description: Any) -> Optional[str]:
    """Strip tags/entities from an HTML description into clean plain text."""
    if not isinstance(description, str) or not description:
        return None
    text = BeautifulSoup(description, "html.parser").get_text(separator=" ")
    return _clean(text)


def _select_text(soup: BeautifulSoup, *selectors: str) -> Optional[str]:
    """Return cleaned text of the first element matching any CSS selector."""
    for selector in selectors:
        el = soup.select_one(selector)
        if el is not None:
            text = _clean(el.get_text(separator=" "))
            if text:
                return text
    return None


def _parse_linkedin_dom(soup: BeautifulSoup) -> Dict[str, Any]:
    """Extract posting fields from LinkedIn's rendered DOM (guest markup).

    These selectors cover both the full guest job-view page and the
    ``jobs-guest`` fragment, which carry no JSON-LD and so must be parsed from
    markup.
    """
    title = _select_text(
        soup,
        ".top-card-layout__title",
        ".topcard__title",
        "h1.topcard__title",
        "h1",
    )
    company = _select_text(
        soup,
        "a.topcard__org-name-link",
        ".topcard__org-name-link",
        ".top-card-layout__second-subline a",
    )
    location = _select_text(
        soup,
        ".topcard__flavor--bullet",
        ".top-card-layout__second-subline .topcard__flavor--bullet",
    )

    desc_el = soup.select_one(
        ".show-more-less-html__markup"
    ) or soup.select_one(".description__text")
    description_text = (
        _clean(desc_el.get_text(separator=" ")) if desc_el is not None else None
    )

    return {
        "title": title,
        "company": company,
        "location": location,
        "description_text": description_text,
    }


def _unwrap_apply_url(url: Optional[str]) -> Optional[str]:
    """Unwrap a LinkedIn ``/safety/go/?url=<encoded>`` redirect to its target.

    LinkedIn routes off-site apply clicks through a safety interstitial that
    carries the real employer URL (fully percent-encoded, dots included) in the
    ``url`` query parameter. Non-safety URLs are returned unchanged.
    """
    if not url:
        return None
    split = urlsplit(url)
    host = (split.hostname or "").lower()
    if host.endswith("linkedin.com") and "/safety/go" in split.path:
        target = parse_qs(split.query).get("url", [None])[0]
        if target:
            return unquote(target)
    return url


def _is_external_apply(url: Optional[str]) -> bool:
    """True only for an http(s) URL that leaves linkedin.com.

    A genuine off-site apply link points at the employer's own site. Any
    linkedin.com URL is a login/authwall/redirect gate, never the real target.
    """
    if not url:
        return False
    host = (urlsplit(url).hostname or "").lower()
    if not host or not url.lower().startswith(("http://", "https://")):
        return False
    return host != "linkedin.com" and not host.endswith(".linkedin.com")


def _apply_url_from_code(soup: BeautifulSoup) -> Optional[str]:
    """Extract the off-site apply URL from ``<code id="applyUrl">``.

    LinkedIn defers parsing by stashing the URL as a JSON string literal inside
    an HTML comment, e.g. ``<code id="applyUrl"><!--"https:\\/\\/co.com\\/x"-->``.
    JSON-decoding restores escaped slashes; entities are unescaped.
    """
    code = soup.find("code", id="applyUrl")
    if not isinstance(code, Tag):
        return None

    inner = code.decode_contents().strip()
    inner = re.sub(r"^<!--", "", inner)
    inner = re.sub(r"-->$", "", inner).strip()
    if not inner:
        return None

    try:
        decoded = json.loads(inner)
        if isinstance(decoded, str):
            inner = decoded
    except (json.JSONDecodeError, ValueError):
        inner = inner.strip('"')

    return _clean(html.unescape(inner))


def _linkedin_apply_url(soup: BeautifulSoup) -> Optional[str]:
    """Recover the external (off-site) apply URL from the DOM.

    Prefers the authoritative ``<code id="applyUrl">`` payload, then an
    apply-button anchor. linkedin.com targets (login/authwall redirects) are
    rejected; returning ``None`` indicates an in-platform "Easy Apply" posting.
    """
    from_code = _unwrap_apply_url(_apply_url_from_code(soup))
    if _is_external_apply(from_code):
        return from_code

    anchor = soup.select_one(_APPLY_BUTTON_SELECTOR)
    if isinstance(anchor, Tag):
        href = anchor.get("href")
        unwrapped = _unwrap_apply_url(href) if isinstance(href, str) else None
        if _is_external_apply(unwrapped):
            return _clean(unwrapped)

    return None


# LinkedIn marks off-site postings (apply leaves LinkedIn) with these tokens in
# the apply CTA's tracking name / class, even when the URL itself is gated.
_OFFSITE_SIGNALS = ("apply-link-offsite", "offsite-apply", "apply-button__offsite")


def _is_offsite_apply(html_text: str) -> bool:
    """True if the markup signals an off-site application (vs in-platform Easy Apply)."""
    return any(signal in html_text for signal in _OFFSITE_SIGNALS)


# Off-site apply URLs are also embedded in LinkedIn's serialized data models
# under one of these JSON keys (slashes are usually escaped as ``\/``).
_APPLY_KEY_RE = re.compile(
    r'"(?:companyApplyUrl|applyUrl|companyApplyURL|easyApplyUrl)"'
    r'\s*:\s*"((?:[^"\\]|\\.)*)"'
)


def _apply_url_from_html(html_text: str) -> Optional[str]:
    """Scan raw HTML for an off-site apply URL inside embedded JSON models.

    Catches postings where the URL lives in a serialized data blob rather than
    the ``<code id="applyUrl">`` element. Returns the first external match.
    """
    for match in _APPLY_KEY_RE.finditer(html_text):
        raw = match.group(1)
        try:
            url = json.loads(f'"{raw}"')  # restores \/ escapes and entities
        except (json.JSONDecodeError, ValueError):
            url = raw.replace("\\/", "/")
        url = _unwrap_apply_url(_clean(html.unescape(url)))
        if _is_external_apply(url):
            return url

    # Fall back to any safety/go redirect embedded in the markup.
    for match in re.finditer(r"/safety/go/?\?[^\"'<>\\ ]*?url=([^\"'<>&\\ ]+)", html_text):
        url = _unwrap_apply_url(f"https://www.linkedin.com/safety/go/?url={match.group(1)}")
        if _is_external_apply(url):
            return url
    return None


def _linkedin_session() -> "requests.Session":
    """Create a curl_cffi session that impersonates Mobile Safari on iOS."""
    session = requests.Session(impersonate=LINKEDIN_IMPERSONATE)
    session.headers.update(IPHONE_HEADERS)
    return session


def fetch_linkedin_job(
    raw_url: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Fetch and parse a LinkedIn job posting as an iPhone would see it.

    The URL is normalized to its canonical ``/jobs/view/{id}/`` form and fetched
    through a Mobile-Safari-impersonating ``curl_cffi`` session (matching iOS
    HTTP headers + TLS fingerprint) to dodge LinkedIn's desktop redirects.

    Two sources are tried, gaps filled from each:

    1. The canonical page's JSON-LD ``JobPosting`` block (richest, when present).
    2. The ``jobs-guest`` fragment endpoint — which is *not* behind the login
       auth-wall — parsed from its rendered markup. This is the fallback that
       rescues postings where the canonical page returns the login modal.

    Returns a dict with keys: ``title``, ``company``, ``location``,
    ``apply_url``, ``apply_type``, ``is_easy_apply``, ``description_text`` and
    ``original_linkedin_url``.

    ``apply_type`` is one of ``"offsite"`` (external URL recovered),
    ``"offsite_gated"`` (off-site posting whose URL LinkedIn hides behind
    sign-in — not recoverable from guest HTML) or ``"easy_apply"``
    (in-platform application).
    """
    job_id = _linkedin_job_id(raw_url)
    canonical_url = f"https://www.linkedin.com/jobs/view/{job_id}/"

    # Mobile iOS impersonation: TLS/JA3 via impersonate, HTTP headers via the
    # explicit iPhone header set.
    session = _linkedin_session()

    fields: Dict[str, Any] = {
        "title": None,
        "company": None,
        "location": None,
        "description_text": None,
        "apply_url": None,
        "is_offsite": False,
    }

    def _absorb(html_text: str) -> None:
        """Fill any still-empty field from a page (JSON-LD, then DOM, then JSON)."""
        soup = BeautifulSoup(html_text, "html.parser")
        job = _find_job_posting(_iter_json_ld(soup)) or {}
        candidates = {
            "title": _clean(job.get("title")),
            "company": _org_name(job.get("hiringOrganization")),
            "location": _job_location(job.get("jobLocation")),
            "description_text": _strip_html(job.get("description")),
        }
        # JSON-LD has no apply link; DOM fills the rest where JSON-LD was empty.
        dom = _parse_linkedin_dom(soup)
        for key in candidates:
            value = candidates[key] or dom.get(key)
            if value and not fields[key]:
                fields[key] = value
        if not fields["apply_url"]:
            # DOM apply elements first, then a scan of embedded JSON models.
            fields["apply_url"] = (
                _linkedin_apply_url(soup) or _apply_url_from_html(html_text)
            )
        if _is_offsite_apply(html_text):
            fields["is_offsite"] = True

    # ----- 1. Canonical guest job-view page (JSON-LD when not walled) ----- #
    response = session.get(canonical_url, timeout=timeout)
    response.raise_for_status()
    _absorb(response.text)

    # ----- 2. Guest fragment fallback (bypasses the auth-wall modal) ----- #
    # Needed when the canonical page was walled (core fields empty) OR when the
    # off-site apply URL is still unresolved — the authoritative apply link
    # (<code id="applyUrl">) is only emitted by the guest fragment endpoint.
    if (
        not fields["title"]
        or not fields["description_text"]
        or not fields["apply_url"]
    ):
        guest_response = session.get(
            LINKEDIN_GUEST_API.format(job_id=job_id),
            timeout=timeout,
            headers={"Referer": canonical_url},
        )
        if guest_response.ok and guest_response.text.strip():
            _absorb(guest_response.text)

    # Classify the application route. An off-site signal with no recoverable URL
    # means LinkedIn gated it behind sign-in (not Easy Apply).
    if fields["apply_url"]:
        apply_type = "offsite"
    elif fields["is_offsite"]:
        apply_type = "offsite_gated"
    else:
        apply_type = "easy_apply"

    return {
        "title": fields["title"],
        "company": fields["company"],
        "location": fields["location"],
        "apply_url": fields["apply_url"],
        "apply_type": apply_type,
        "is_easy_apply": apply_type == "easy_apply",
        "description_text": fields["description_text"],
        "original_linkedin_url": canonical_url,
    }


def fetch_linkedin_debug(
    raw_url: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
) -> List[Dict[str, Any]]:
    """Fetch both LinkedIn sources raw, for diagnosing apply-URL extraction.

    Returns one entry per source (canonical page, guest fragment) with the HTTP
    status, byte length, raw HTML, and every candidate apply URL found by each
    strategy — so we can see exactly where (or whether) the URL is exposed.
    """
    job_id = _linkedin_job_id(raw_url)
    canonical_url = f"https://www.linkedin.com/jobs/view/{job_id}/"
    guest_url = LINKEDIN_GUEST_API.format(job_id=job_id)

    session = _linkedin_session()
    sources = [
        ("canonical", canonical_url, {}),
        ("guest", guest_url, {"Referer": canonical_url}),
    ]

    report: List[Dict[str, Any]] = []
    for name, url, extra_headers in sources:
        try:
            resp = session.get(url, timeout=timeout, headers=extra_headers or None)
            html_text = resp.text
            soup = BeautifulSoup(html_text, "html.parser")
            # Every external href on the page (helps spot where apply links live).
            external_hrefs = sorted(
                {
                    _clean(a.get("href"))
                    for a in soup.find_all("a", href=True)
                    if _is_external_apply(a.get("href"))
                }
            )
            report.append(
                {
                    "source": name,
                    "url": url,
                    "status": resp.status_code,
                    "length": len(html_text),
                    "html": html_text,
                    "code_apply_url": _apply_url_from_code(soup),
                    "json_apply_url": _apply_url_from_html(html_text),
                    "selector_apply_url": _linkedin_apply_url(soup),
                    "external_hrefs": external_hrefs,
                }
            )
        except Exception as exc:  # network/HTTP failures shouldn't abort the dump
            report.append(
                {"source": name, "url": url, "status": None, "error": str(exc)}
            )
    return report
