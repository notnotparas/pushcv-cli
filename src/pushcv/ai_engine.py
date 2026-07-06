"""AI synthesis engine for pushcv (Phase 3).

Routes resume-tailoring completions through ``litellm`` to a *local*,
OpenAI-compatible inference server (e.g. Lemonade) so synthesis stays
local-first — no data leaves the machine.
"""
from __future__ import annotations

import os
import re
from typing import Optional

import litellm
from dotenv import load_dotenv

# Pull any local overrides (e.g. PUSHCV_AI_BASE) from a .env in the workspace.
# The explicit relative path matters: with no argument, python-dotenv searches
# upward from THIS package's directory — not from the user's working directory
# — and would never find the workspace .env. pushcv is cwd-scoped by design.
load_dotenv(".env")

# Local OpenAI-compatible inference server. Defaults target Lemonade; point
# PUSHCV_AI_BASE at any other OpenAI-compatible server — e.g. Ollama
# (http://localhost:11434/v1) or llama.cpp — via the environment or a .env
# file in the workspace. The api_key is a non-secret placeholder most local
# servers expect; nothing is sent to a remote provider.
LOCAL_API_BASE = os.getenv("PUSHCV_AI_BASE", "http://localhost:13305/v1")
LOCAL_API_KEY = os.getenv("PUSHCV_AI_KEY", "lemonade-local")

# Request timeouts (seconds). Local generation on CPU can legitimately take
# minutes (model load + long completion), but an unbounded call hangs forever
# when the server accepts a request it can never serve (e.g. unknown model).
GENERATION_TIMEOUT = 300
SALARY_TIMEOUT = 120

# Instruction set: tailor strictly from supplied facts, never invent experience.
SYSTEM_PROMPT = """\
You are an expert technical recruiter and professional resume writer.

Your task: produce a tailored, ATS-optimized resume for a specific job by
cross-referencing the JOB DESCRIPTION against the CANDIDATE PROFILE.

Rules you must follow:
1. Cross-reference the job description with the candidate profile and identify
   the skills, tools, and qualifications the role explicitly requires.
2. Aggressively filter the profile. Select ONLY the experiences, projects, and
   skills that directly prove the candidate meets the job's requirements. Omit
   anything irrelevant to this role — a focused resume beats a complete one.
3. Mirror the job description's terminology and keywords where the candidate
   genuinely has the matching experience, so the resume passes ATS screening.
4. Output a single, highly professional resume in clean Markdown: a header,
   then sections such as Summary, Skills, Experience, and Projects. Use concise,
   impact-focused bullet points (action verb + what + measurable result).
5. Adhere STRICTLY to the facts in the candidate profile. Never invent, inflate,
   or hallucinate employers, titles, dates, metrics, or skills. If the profile
   lacks something the job wants, simply leave it out — do not fabricate it.

Output ONLY the finished Markdown resume — no preamble, commentary, or notes.
"""


# Instruction set for cover letters: short, specific, strictly factual.
COVER_LETTER_SYSTEM_PROMPT = """\
You are an expert career coach and professional writer.

Your task: write a compelling, concise cover letter for a specific job by
cross-referencing the JOB DESCRIPTION against the CANDIDATE PROFILE.

Rules you must follow:
1. Keep it short: 3–4 paragraphs, under 300 words. Recruiters skim.
2. Open with a hook specific to this company and role — never a template line
   like "I am writing to apply for...".
3. Build the body around the 2–3 strongest matches between the candidate's
   experience and the job's requirements, with concrete results and numbers
   taken from the profile.
4. Adhere STRICTLY to the facts in the candidate profile. Never invent,
   inflate, or hallucinate employers, titles, dates, metrics, or skills.
5. Confident, warm, human tone. No clichés ("team player", "fast-paced
   environment", "passionate about synergy") and no flattery padding.
6. Output clean Markdown: a greeting ("Dear Hiring Team," unless the
   description names a person), the paragraphs, and a sign-off with the
   candidate's name from the profile. No addresses, dates, or letterhead.

Output ONLY the finished Markdown cover letter — no preamble or notes.
"""


# Instruction set for compensation estimates: one concise line, no prose.
SALARY_SYSTEM_PROMPT = """\
You are an expert tech recruiter. Analyze the provided web search snippets to
estimate the salary range for this role. Output ONLY a concise, 1-line string
(e.g., '$120k - $140k base + equity'). Do not include conversational filler.

Additional rules:
- Ground the estimate in the WEB SEARCH SNIPPETS where they are relevant; use
  your own market knowledge to fill gaps when the snippets are thin or missing.
- Anchor the range on the SENIORITY level of THIS role (see the EXPERIENCE
  section) — not the company-wide average across all levels. A senior role pays
  well above a junior one at the same company.
- If a CANDIDATE EXPERIENCE figure is given, position within the role's band: at
  or above the requirement -> mid-to-upper end; clearly below it -> lower end.
  Never drop below the role's band just because the candidate is under-qualified.
- Express the figures STRICTLY in the currency named in the CURRENCY section.
  Use that currency's symbol/code and conventional local notation (e.g. "k" for
  thousands, or "L"/"lakh" for INR where natural). Never use any other currency.
- Style examples: "$120k - $140k base + equity", "₹25L - ₹35L base",
  "£70k - £85k base + bonus". Mention equity or bonus only when typical.
- Output the range and nothing else. No parentheses, no rationale, no notes, and
  no text of any kind after the figures.
- If you genuinely cannot estimate, output exactly: Estimate unavailable
"""

# Location keyword (lowercase substring) -> human-readable currency instruction.
# Ordered most-specific first; the first match on the location string wins.
_CURRENCY_BY_LOCATION = [
    (("india", "bengaluru", "bangalore", "mumbai", "delhi", "hyderabad",
      "pune", "chennai", "gurgaon", "noida", "punjab"), "INR (₹, Indian Rupees)"),
    (("united kingdom", "uk", "england", "scotland", "wales", "london"),
     "GBP (£, British Pounds)"),
    (("ireland", "germany", "france", "spain", "italy", "netherlands",
      "portugal", "belgium", "austria", "finland", "eurozone", "europe"),
     "EUR (€, Euros)"),
    (("canada", "toronto", "vancouver", "montreal"), "CAD (Canadian Dollars)"),
    (("australia", "sydney", "melbourne"), "AUD (Australian Dollars)"),
    (("singapore",), "SGD (Singapore Dollars)"),
    (("united arab emirates", "uae", "dubai", "abu dhabi"), "AED (UAE Dirham)"),
    (("japan", "tokyo"), "JPY (¥, Japanese Yen)"),
    (("united states", "usa", " us", "u.s", "remote - us"), "USD ($, US Dollars)"),
]

DEFAULT_CURRENCY = "USD ($, US Dollars)"


def currency_for_location(location: str) -> str:
    """Infer a target currency instruction from a free-text location.

    Falls back to :data:`DEFAULT_CURRENCY` (USD) when the location is empty or
    unrecognized — which also naturally covers US locations given by state/city.
    """
    text = (location or "").lower()
    for keywords, currency in _CURRENCY_BY_LOCATION:
        if any(kw in text for kw in keywords):
            return currency
    return DEFAULT_CURRENCY


# Explanatory tails the model sometimes appends after the figures, e.g.
# "₹40L - ₹60L base (estimated based on ...)" or "..., based on market data".
_ESTIMATE_TAIL_RE = re.compile(
    r"(?i)\s*[(,–—-]?\s*"
    r"(?:estimated|based on|according to|reflecting|considering|note:?|"
    r"this (?:is|reflects)|depending|as per|source)\b.*$"
)


def _clean_estimate_line(line: str) -> str:
    """Strip trailing explanations/parentheticals from a compensation line."""
    line = re.sub(r"\s*\([^)]*\)\s*$", "", line)  # drop a trailing "(...)"
    line = _ESTIMATE_TAIL_RE.sub("", line)  # drop "... estimated/based on ..."
    return line.strip(" ,;:-–—") or "Estimate unavailable"


def _condense_estimate(text: str) -> str:
    """Reduce a model response to a single clean compensation line."""
    text = _strip_code_fence(text.strip())
    lines = [line.strip(" -*`\"'") for line in text.splitlines() if line.strip()]
    if not lines:
        return "Estimate unavailable"
    # Prefer the first line that looks like a money figure/range.
    for line in lines:
        if re.search(r"[$€£₹]|\d", line):
            return _clean_estimate_line(line)[:120]
    return _clean_estimate_line(lines[0])[:120]


def estimate_compensation(
    job_title: str,
    company: str,
    location: str,
    search_context: str,
    model_name: str = "qwen3-1.7b",
    currency: Optional[str] = None,
    role_experience: str = "",
    candidate_yoe: Optional[int] = None,
) -> str:
    """Estimate compensation from live web snippets as a concise string.

    Analyzes ``search_context`` (web snippets about pay for the role) to produce
    a single line such as ``"$120k - $140k base + equity"``. ``role_experience``
    (the seniority/years the posting expects) anchors the band; ``candidate_yoe``
    positions the estimate within it. Figures use ``currency`` when given, else
    the currency inferred from ``location`` (defaulting to USD). On any failure,
    returns a clean error string (prefixed ``ERROR:``) instead of raising.
    """
    target_currency = currency or currency_for_location(location)
    experience_line = role_experience or "Not specified"
    if candidate_yoe is not None:
        experience_line += f"\nCandidate experience: {candidate_yoe} years."
    user_prompt = (
        f"# JOB TITLE\n{job_title}\n\n"
        f"# COMPANY\n{company or 'Not specified'}\n\n"
        f"# LOCATION\n{location or 'Not specified'}\n\n"
        f"# EXPERIENCE\nRole seniority/requirement: {experience_line}\n\n"
        f"# CURRENCY\nExpress the estimate strictly in {target_currency}.\n\n"
        f"# WEB SEARCH SNIPPETS\n{search_context or 'No snippets found.'}\n\n"
        "Provide the single-line compensation estimate now."
    )

    try:
        response = litellm.completion(
            model=f"openai/{model_name}",
            api_base=LOCAL_API_BASE,
            api_key=LOCAL_API_KEY,
            stream=False,
            timeout=SALARY_TIMEOUT,
            messages=[
                {"role": "system", "content": SALARY_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        content = response.choices[0].message.content
        if not content or not content.strip():
            return (
                "ERROR: The local model returned an empty response. "
                "Verify the model is loaded and try again."
            )
        return _condense_estimate(content)
    except Exception as exc:  # connection refused, timeout, bad model, etc.
        return (
            "ERROR: Could not estimate compensation. Failed to reach the local "
            f"AI server at {LOCAL_API_BASE} (model '{model_name}').\n"
            f"Details: {exc}\n"
            "Make sure your local inference server (e.g. Lemonade) is running "
            "and the requested model is available."
        )


def _strip_code_fence(text: str) -> str:
    """Remove a wrapping Markdown code fence the model often adds.

    Models frequently wrap the whole resume in ```` ```markdown … ``` ````.
    Only strips when an opening fence is on the first line and a closing fence
    is the last line, so genuine inline code blocks are left untouched.
    """
    lines = text.splitlines()
    if len(lines) >= 2 and re.match(r"^\s*```", lines[0]) and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return text


def generate_tailored_resume(
    job_title: str,
    job_description: str,
    user_profile: str,
    model_name: str,
) -> str:
    """Generate an ATS-optimized Markdown resume tailored to a job posting.

    Cross-references ``job_description`` against ``user_profile`` via a local
    LLM and returns the resume as a Markdown string. On any connection/inference
    failure, returns a clean human-readable error string instead of raising.
    """
    user_prompt = (
        f"# JOB TITLE\n{job_title}\n\n"
        f"# JOB DESCRIPTION\n{job_description}\n\n"
        f"# CANDIDATE PROFILE\n{user_profile}\n\n"
        "Write the tailored, ATS-optimized Markdown resume now."
    )

    try:
        response = litellm.completion(
            model=f"openai/{model_name}",
            api_base=LOCAL_API_BASE,
            api_key=LOCAL_API_KEY,
            stream=False,
            timeout=GENERATION_TIMEOUT,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        content = response.choices[0].message.content
        if not content or not content.strip():
            return (
                "ERROR: The local model returned an empty response. "
                "Verify the model is loaded and try again."
            )
        return _strip_code_fence(content.strip())
    except Exception as exc:  # connection refused, timeout, bad model, etc.
        return (
            "ERROR: Could not generate the resume. Failed to reach the local AI "
            f"server at {LOCAL_API_BASE} (model '{model_name}').\n"
            f"Details: {exc}\n"
            "Make sure your local inference server (e.g. Lemonade) is running "
            "and the requested model is available."
        )


def generate_cover_letter(
    job_title: str,
    company: str,
    job_description: str,
    user_profile: str,
    model_name: str,
) -> str:
    """Generate a short, tailored Markdown cover letter for a job posting.

    Same local-first contract as :func:`generate_tailored_resume`: the
    completion runs against the local server, and any connection/inference
    failure returns a clean human-readable error string instead of raising.
    """
    user_prompt = (
        f"# JOB TITLE\n{job_title}\n\n"
        f"# COMPANY\n{company or 'Not specified'}\n\n"
        f"# JOB DESCRIPTION\n{job_description}\n\n"
        f"# CANDIDATE PROFILE\n{user_profile}\n\n"
        "Write the tailored Markdown cover letter now."
    )

    try:
        response = litellm.completion(
            model=f"openai/{model_name}",
            api_base=LOCAL_API_BASE,
            api_key=LOCAL_API_KEY,
            stream=False,
            timeout=GENERATION_TIMEOUT,
            messages=[
                {"role": "system", "content": COVER_LETTER_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        content = response.choices[0].message.content
        if not content or not content.strip():
            return (
                "ERROR: The local model returned an empty response. "
                "Verify the model is loaded and try again."
            )
        return _strip_code_fence(content.strip())
    except Exception as exc:  # connection refused, timeout, bad model, etc.
        return (
            "ERROR: Could not generate the cover letter. Failed to reach the "
            f"local AI server at {LOCAL_API_BASE} (model '{model_name}').\n"
            f"Details: {exc}\n"
            "Make sure your local inference server (e.g. Lemonade) is running "
            "and the requested model is available."
        )
