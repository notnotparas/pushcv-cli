# Contributing to pushcv

Thanks for your interest in improving pushcv! This is a small, focused project
and contributions are very welcome — especially the items on the
[Roadmap](README.md#roadmap--contributions-welcome). Be kind and constructive,
and assume good faith.

## Guiding principles

Please keep these in mind — a PR that conflicts with them is unlikely to be
merged, however well-written:

1. **Local-first, always.** pushcv never sends a user's data to a third-party
   service. AI features run against a **local**, OpenAI-compatible server via
   LiteLLM — do **not** add integrations with hosted LLM providers (OpenAI,
   Anthropic, Gemini, etc.) or any telemetry/analytics. This is the core promise
   of the project, not a preference.
2. **Private by default.** Anything written to disk that contains user data
   (the database, `profile.md`, drafts, preferences) must stay in the working
   directory and be listed in `.gitignore`. Never commit personal data.
3. **Fail gracefully.** Network calls and model calls fail all the time. A
   failure should degrade cleanly (a clear message, an untouched database) —
   never a stack trace or corrupted state.
4. **Small surface area.** Prefer a few well-behaved commands over many
   half-working ones.

## Development setup

Requires **Python ≥ 3.10**.

```bash
git clone https://github.com/notnotparas/pushcv-cli.git
cd pushcv-cli
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"     # editable install + test tooling
pushcv --help               # confirm the entry point works
```

Run pushcv inside a throwaway directory so its `pushcv.db` / `profile.md` don't
clutter the repo (they're git-ignored regardless):

```bash
mkdir -p ~/pushcv-scratch && cd ~/pushcv-scratch
pushcv init && pushcv add "Test Co" "Engineer" && pushcv status
```

## Running the tests

```bash
pytest
```

The suite covers the **pure helpers** (salary parsing, URL normalization,
currency inference) — the parts with no network or database dependency. Adding
more of these is the single most valuable contribution right now; see
`tests/test_helpers.py` for the pattern.

## Coding style

- Follow the conventions already in the file you're editing — module docstring,
  typed signatures, and a short docstring on non-trivial functions.
- Keep comments about the **why**, not the **what**.
- No new hard dependencies without discussion — open an issue first.
- Line length ~88 chars, 4-space indent (Black-compatible), no trailing
  whitespace.

## Adding a new job board to `fetch`

This is the most-requested feature and a great first PR. Portals live in
[`src/pushcv/portals/`](src/pushcv/portals/) — one module per board, registered
in the `PORTALS` tuple in
[`portals/__init__.py`](src/pushcv/portals/__init__.py). LinkedIn, Greenhouse,
Lever, and SmartRecruiters are built in; **Ashby** and **Workable** are natural
next adapters (both expose public JSON APIs, no impersonation needed).

A portal module needs three things:

1. `matches(url) -> bool` — does this URL belong to your board?
2. `fetch_job(url, *, timeout) -> dict` — return the normalized posting dict
   built by `posting(...)` in [`portals/base.py`](src/pushcv/portals/base.py).
   Raise `ValueError` for a URL you claim but can't parse (the command turns
   that into a clean error message).
3. **Tests without network** — keep payload normalization in a pure
   `parse_payload(payload, *, url)` function and exercise it with a
   fixture-shaped dict in [`tests/test_portals.py`](tests/test_portals.py)
   (see the Greenhouse/Lever/SmartRecruiters tests for the pattern).

## Pull request process

1. **Open an issue first** for anything beyond a small fix, so we can agree on
   the approach before you invest time.
2. Branch off `master`, keep the change focused, and make sure `pytest` passes.
3. Fill out the PR template. Describe what you changed and how you tested it.
4. CI must be green. A maintainer will review — be patient, this is a
   volunteer-run project.

## Reporting bugs & requesting features

Use the issue templates. For bugs, include your OS, Python version, the exact
command, and the full output (redact anything personal). For salary or scraping
issues, remember both depend on live third-party data and can be flaky — note
whether it's reproducible.

Thanks again for helping make job hunting a little less painful. 💚
