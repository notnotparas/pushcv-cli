"""Tests for the portal registry and the per-portal payload parsers.

Everything here is pure — URL parsing, payload normalization, dispatch — with
fixture payloads shaped like the real public-API responses. No network.
"""
import pytest

from pushcv.portals import detect, greenhouse, lever, linkedin, smartrecruiters
from pushcv.portals.generic import parse_html


# --------------------------------------------------------------------------- #
# Registry dispatch
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.linkedin.com/jobs/view/123456/", linkedin),
        ("https://job-boards.greenhouse.io/acme/jobs/4567890", greenhouse),
        ("https://boards.eu.greenhouse.io/acme/jobs/4567890", greenhouse),
        ("https://jobs.lever.co/acme/a1b2c3d4-e5f6-7890-abcd-ef1234567890", lever),
        ("https://jobs.eu.lever.co/acme/a1b2c3d4-e5f6-7890-abcd-ef1234567890", lever),
        ("https://jobs.smartrecruiters.com/Acme/743999912345678-engineer", smartrecruiters),
    ],
)
def test_detect_routes_to_the_owning_portal(url, expected):
    assert detect(url) is expected


def test_detect_returns_none_for_unknown_hosts():
    assert detect("https://careers.example.com/jobs/1") is None
    # Lookalike domains must not match.
    assert detect("https://evillinkedin.com/jobs/view/1/") is None


# --------------------------------------------------------------------------- #
# Greenhouse
# --------------------------------------------------------------------------- #
def test_greenhouse_board_and_job_from_hosted_url():
    board, job_id, region = greenhouse.board_and_job(
        "https://job-boards.greenhouse.io/acme/jobs/4567890?gh_src=abc"
    )
    assert (board, job_id, region) == ("acme", "4567890", "us")


def test_greenhouse_board_and_job_from_embed_url():
    board, job_id, region = greenhouse.board_and_job(
        "https://boards.greenhouse.io/embed/job_app?for=acme&token=4567890"
    )
    assert (board, job_id, region) == ("acme", "4567890", "us")


def test_greenhouse_board_and_job_rejects_unparseable():
    with pytest.raises(ValueError):
        greenhouse.board_and_job("https://boards.greenhouse.io/acme")


def test_greenhouse_parse_payload_unescapes_content():
    payload = {
        "title": "Senior Engineer",
        "company_name": "Acme Corp",
        "location": {"name": "Berlin, Germany"},
        "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/4567890",
        "content": "&lt;p&gt;Build &amp;amp; ship things.&lt;/p&gt;",
    }
    result = greenhouse.parse_payload(payload, url="https://x.example/ignored")
    assert result["portal"] == "greenhouse"
    assert result["title"] == "Senior Engineer"
    assert result["company"] == "Acme Corp"
    assert result["location"] == "Berlin, Germany"
    assert result["description_text"] == "Build & ship things."
    assert result["canonical_url"].endswith("/jobs/4567890")
    assert result["apply_type"] == "direct"


def test_greenhouse_parse_payload_uses_board_fallback_company():
    result = greenhouse.parse_payload(
        {"title": "SRE"}, url="https://u.example", company_fallback="Acme"
    )
    assert result["company"] == "Acme"


# --------------------------------------------------------------------------- #
# Lever
# --------------------------------------------------------------------------- #
def test_lever_company_and_posting_accepts_apply_suffix():
    company, posting_id, api_base = lever.company_and_posting(
        "https://jobs.lever.co/acme/a1b2c3d4-e5f6-7890-abcd-ef1234567890/apply"
    )
    assert company == "acme"
    assert posting_id == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    assert api_base == "https://api.lever.co/v0/postings"


def test_lever_eu_host_maps_to_eu_api():
    _, _, api_base = lever.company_and_posting(
        "https://jobs.eu.lever.co/acme/a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    )
    assert api_base == "https://api.eu.lever.co/v0/postings"


def test_lever_company_and_posting_rejects_non_posting_paths():
    with pytest.raises(ValueError):
        lever.company_and_posting("https://jobs.lever.co/acme")


def test_lever_parse_payload_folds_lists_into_description():
    payload = {
        "text": "Backend Engineer",
        "categories": {"location": "Toronto, Canada"},
        "workplaceType": "remote",
        "descriptionPlain": "We build pipelines.",
        "lists": [
            {"text": "Requirements", "content": "<li>Python</li><li>SQL</li>"}
        ],
        "additionalPlain": "Benefits included.",
        "hostedUrl": "https://jobs.lever.co/acme/a1b2c3d4",
        "applyUrl": "https://jobs.lever.co/acme/a1b2c3d4/apply",
    }
    result = lever.parse_payload(payload, url="https://u.example", company_fallback="acme")
    assert result["title"] == "Backend Engineer"
    assert result["company"] == "Acme"
    assert result["location"] == "Toronto, Canada (Remote)"
    assert "We build pipelines." in result["description_text"]
    assert "Requirements" in result["description_text"]
    assert "Python" in result["description_text"]
    assert "Benefits included." in result["description_text"]
    assert result["apply_url"].endswith("/apply")
    assert result["canonical_url"] == "https://jobs.lever.co/acme/a1b2c3d4"


# --------------------------------------------------------------------------- #
# SmartRecruiters
# --------------------------------------------------------------------------- #
def test_smartrecruiters_company_and_posting_with_slug():
    assert smartrecruiters.company_and_posting(
        "https://jobs.smartrecruiters.com/AcmeCorp/743999912345678-senior-engineer"
    ) == ("AcmeCorp", "743999912345678")


def test_smartrecruiters_company_and_posting_without_slug():
    assert smartrecruiters.company_and_posting(
        "https://jobs.smartrecruiters.com/AcmeCorp/743999912345678"
    ) == ("AcmeCorp", "743999912345678")


def test_smartrecruiters_company_and_posting_rejects_unparseable():
    with pytest.raises(ValueError):
        smartrecruiters.company_and_posting(
            "https://jobs.smartrecruiters.com/AcmeCorp"
        )


def test_smartrecruiters_parse_payload_joins_sections_and_location():
    payload = {
        "name": "Data Engineer",
        "company": {"identifier": "acmecorp", "name": "Acme Corp"},
        "location": {"city": "Pune", "region": "MH", "country": "in", "remote": False},
        "jobAd": {
            "sections": {
                "companyDescription": {"title": "About us", "text": "<p>We are Acme.</p>"},
                "jobDescription": {"title": "The role", "text": "<p>Own the ETL stack.</p>"},
                "qualifications": {"title": "You have", "text": "<ul><li>Spark</li></ul>"},
            }
        },
    }
    result = smartrecruiters.parse_payload(
        payload, url="https://jobs.smartrecruiters.com/AcmeCorp/743999912345678"
    )
    assert result["title"] == "Data Engineer"
    assert result["company"] == "Acme Corp"
    assert result["location"] == "Pune, MH, IN"
    assert "We are Acme." in result["description_text"]
    assert "Own the ETL stack." in result["description_text"]
    assert "Spark" in result["description_text"]


# --------------------------------------------------------------------------- #
# Generic JSON-LD fallback
# --------------------------------------------------------------------------- #
def test_generic_parse_html_reads_json_ld_job_posting():
    html_text = """
    <html><head>
    <script type="application/ld+json">
    {"@type": "JobPosting", "title": "Platform Engineer",
     "hiringOrganization": {"name": "Example Inc"},
     "jobLocation": {"address": {"addressLocality": "Austin",
                                 "addressRegion": "TX", "addressCountry": "US"}},
     "description": "<p>Run the platform.</p>"}
    </script>
    </head><body><h1>ignored</h1></body></html>
    """
    result = parse_html(html_text, url="https://careers.example.com/jobs/1")
    assert result["portal"] == "generic"
    assert result["title"] == "Platform Engineer"
    assert result["company"] == "Example Inc"
    assert result["location"] == "Austin, TX, US"
    assert result["description_text"] == "Run the platform."


def test_generic_parse_html_falls_back_to_opengraph():
    html_text = """
    <html><head>
    <meta property="og:title" content="QA Engineer at Example" />
    <meta property="og:site_name" content="Example Careers" />
    </head><body></body></html>
    """
    result = parse_html(html_text, url="https://careers.example.com/jobs/2")
    assert result["title"] == "QA Engineer at Example"
    assert result["company"] == "Example Careers"
