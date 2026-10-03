# Job Radar Agent

BUAN 6V99 solo mini-project. Evaluate a small batch of job postings for an operations professional moving into analytics and applied AI, then automate the repetitive parts of filling out an application. Return up to two evidenced recommendations and a verification queue. Preserve structured memory and require user confirmation to record applications or submit anything.

This is a tool-using agent configuration for Copilot Agent or Claude Code, with Python tools. The coding assistant supplies the LLM and tool loop by default. `radar.py` alone is NOT an AI agent; it performs validation, storage and filtering. `apply.py` is the browser-automation core for the application-filling step; it also is not an AI agent by itself — a plan is prepared and approved by a human before it runs. `run_demo.py` is a scripted fixture replay, NOT a model demonstration.

A separate, optional standalone integration (`llm_client.py`) can call the Anthropic API directly, without a live chat session — see "Optional standalone LLM integration" below. It is inert unless `ANTHROPIC_API_KEY` is set, and one run of it (or of a chat session) is not proof the whole program has a generally working runtime LLM connection; each is its own piece of evidence, reported separately in `docs/APPLY_VALIDATION.md` and `docs/VALIDATION.md`. Job discovery beyond pasted text is limited to `import_posting.py fetch URL`, a plain HTTP GET with no JavaScript execution and no search — not broad web discovery.

## Application filling (v0.2 vertical slice)

`apply.py` drives a real browser (Playwright/Chromium) through a supported multi-page application form using an observe-decide-act-verify loop:

1. A human-approved plan (JSON) maps profile fields to the form's accessible labels. `apply.py` refuses to run an unapproved plan (`"approved": true` required) or an action outside a small allowlist.
2. Before opening a browser, it resolves every value the plan needs against the profile. Any missing answer pauses the whole run with a batched list of what's missing — nothing is invented, and no field is filled while a required answer for that page is unresolved.
3. Each fill/check/upload is re-read from the live page immediately after acting, to confirm it actually took.
4. The plan marks one page as the review page. On reaching it, the tool stops and refuses to click anything at all — the final submit click is always left to the human. As defense-in-depth, any button whose visible text looks like a submit action (`Submit`, `Finish`, `Complete`, `Apply Now`, `Send Application`) is refused everywhere, not only on the review page.
5. Progress is saved to `private/application_runs/<run_key>.json` (status, page reached, action log, missing questions, validation issues) plus a persistent on-disk browser profile per run, so a later `fill` call with the same run key resumes at the exact page it stopped on — without repeating already-completed pages — instead of restarting the form.

Currently supported: the local fixture in `testapp/` only (see below). No real employer ATS is wired up yet; see `docs/APPLY_VALIDATION.md` for exactly what has been tested and what has not.

### Setup
```
python -m pip install -r requirements.txt
python -m playwright install chromium
```

### Try it against the local fixture
```
python -m http.server 8765 --directory testapp
python apply.py fill demo_run --plan demo/apply_plan_testapp.json --profile demo/test_profile.json
python apply.py status demo_run
```
Both `demo/apply_plan_testapp.json` and `demo/test_profile.json` are fictional, matching the `demo/` convention used for `radar.py` fixtures.

`apply.py fill` also accepts `--answer-bank private/answer_bank.json` (reuse confirmed recurring answers via an `"answerbank:<question>"` plan value — exact-normalized match only, see `answers.py` below) and `--job-key KEY --radar-state private/state.json` (refuses to start if `KEY` is already recorded as applied there — real, tested duplicate prevention, not just a comment).

## Dashboard (UI)

`ui.py` is a minimal local Streamlit dashboard: shortlist table, import-a-URL form, selected-job detail with fit evidence, fill-application controls with live run status, the final-review/confirm step, an answer-bank editor, and a cover-letter approve/generate panel. It is a thin layer — every button shells out to `radar.py` / `apply.py` / `import_posting.py` / `answers.py` / `coverletter.py` / `llm_client.py` as a real subprocess and renders their JSON output; it does not reimplement any decision or automation logic itself.

```
python -m streamlit run ui.py
```
Run this from the project root so relative paths (`private/state.json`, `demo/state.json`, ...) resolve correctly. A sidebar toggle switches every panel between real data (`private/`) and demo fixtures (`demo/`) — never mixed, matching radar.py's own rule. Verified by loading it in a real (non-demo) browser session: it renders with no ingested postings and no exception, and with the demo toggle on it correctly lists the fictional postings, shows one's fit evidence, and offers Fill application.

## Answer bank

`answers.py` stores confirmed recurring question/answer pairs (question, answer, source, confirmed flag, date) in `private/answer_bank.json`. Matching is exact text (case/whitespace-normalized) only — it never guesses that two differently worded questions mean the same thing; only `confirmed: true` entries are ever reused automatically.
```
python answers.py add "Are you willing to relocate?" "Yes" --source "user confirmed 2026-09-25" --confirmed
python answers.py list
```

## Cover letter pipeline

`coverletter.py` stores a draft, gates document generation behind an explicit `approve` step (the same human-approval pattern as `apply.py`'s plan), and only after that generates a `.docx` via `python-docx` — then re-reads the generated file and verifies its text matches the approved text exactly before calling it done. Writing the actual tailored sentences is still a chat session's job, using only verified profile/posting facts; this module never invents wording.
```
python coverletter.py draft KEY --company C --role R --text-file draft.txt --source-notes "profile skills + posting requirement X"
python coverletter.py approve KEY
python coverletter.py generate KEY
```
Tested locally with fictional text: the approval gate refuses `generate` before `approve`, and the generated `.docx` round-trips its content correctly (see `tests/test_coverletter.py`).

## Optional standalone LLM integration

`llm_client.py` calls the Anthropic API (`claude-opus-5` by default, structured JSON output) directly — a genuinely separate integration from this chat, for producing an assessment without a live coding-assistant session. It does nothing unless `ANTHROPIC_API_KEY` is set in your environment (never in a repo file):
```
setx ANTHROPIC_API_KEY "sk-ant-your-key-here"     # new terminal needed afterward
python llm_client.py check
python llm_client.py assess private/postings_drafts/<file>.json
```
Its output schema is unit-tested against `radar.DIMENSIONS` so the two paths cannot silently drift apart, but the network call itself has not been exercised in this environment — no key is configured here (`python llm_client.py check` reports `"anthropic_api_key_configured": false`). It does not read `private/history_reference.md` (e.g. the Toyota-exclusion policy), so treat an API-produced assessment as narrower than a chat-produced one until that gap is closed.

## Start on Windows
1. Extract the ZIP into its own folder and open that folder in VS Code.
2. Open the integrated terminal. Run `python --version` (Python 3.10+). If your installation uses `py`, substitute `py` for `python`.
3. Run `python run_demo.py`. Expected: one RECOMMEND, one VERIFY, three SKIP; only one target.
4. Run `python -m unittest discover -s tests -v`.
5. Read AGENT_INSTRUCTIONS.md and docs/LEARNING_GUIDE.md. Review private/profile.json; graduation date, current authorization and availability are intentionally unknown.
6. Open Copilot Chat and select Agent. Ask: "Read AGENT_INSTRUCTIONS.md. Explain the available tools. Then evaluate one real posting I will paste, using private/profile.json. Show source evidence and missing information before shortlisting. Do not modify the program or mark any application submitted."
7. Paste a complete employer posting and its URL. Copilot writes the input JSON and calls the Python tools. Watch the commands, inspect assessment JSON, and challenge its conclusions. The account needs access to Copilot and command execution; usage limits depend on your account. No separate model API key is used by this project.

For a synthetic model test, use the instructions in demo/README.md. Keep the resulting chat/tool transcript as evidence of actual agent execution.

The default date uses America/Chicago. Some Windows Python installations lack timezone data: either install `tzdata` with `python -m pip install tzdata`, or supply `--date YYYY-MM-DD` using the current Dallas date. `radar.py`/`answers.py`/`import_posting.py` need no extra packages; `apply.py`, `ui.py`, `coverletter.py`, and `llm_client.py` need `pip install -r requirements.txt` (Playwright additionally needs `python -m playwright install chromium`) — see the sections above for each.

## Architecture and commands
- AGENT_INSTRUCTIONS.md: decision policy, tools, stopping rules and limitations.
- private/profile.json: user's background, constraints and unknowns.
- private/history_reference.md: original supplied product vision and historical application reference.
- radar.py: deterministic tools and validation for finding/evaluating postings.
- apply.py: browser-automation core for filling an approved application; never clicks submit.
- import_posting.py: plain-HTTP fetch + text extraction to draft a posting.json from a URL.
- answers.py: reusable, exact-match-only bank of confirmed recurring answers.
- coverletter.py: draft/approve/generate pipeline for cover letters, with a read-back verify.
- llm_client.py: optional standalone Anthropic API call for an assessment, off by default.
- ui.py: minimal local Streamlit dashboard orchestrating all of the above.
- testapp/: local fictional multi-page application fixture used to test apply.py.
- private/state.json: source of truth for postings, assessment history, application confirmations and same-day radar.
- private/application_runs/: per-run status, action log and browser profile for apply.py (gitignored).
- private/answer_bank.json, private/documents/, private/postings_drafts/: real data for the pieces above (gitignored).
- private/jobs.csv: export for inspection; editing it does not update state.
- demo/: fictional inputs and explicitly prewritten expected assessments, plus the fictional apply.py plan/profile used against testapp/.
- tests/: guardrail checks.

Command examples (global flags go before subcommands):
```
python radar.py --date 2026-09-09 ingest private/posting.json
python radar.py list
python radar.py read KEY
python radar.py assess KEY private/assessments/role.json
python radar.py shortlist
python radar.py export private/jobs.csv
python radar.py summary
```
After the user actually confirms a submission:
```
python radar.py confirm KEY --user-confirmed
```
The flag prevents accidental invocation; it is not authentication. A tool-enabled host can technically edit files, so instruction compliance and human review remain necessary. The validator checks that evidence exists, not that the model interpreted it correctly.

## MVP decisions
Prefer up to two qualified targets over exactly two. Missing authorization evidence means VERIFY. A good skills match cannot override a hard eligibility blocker. Unknown graduation/authorization details must be resolved where material. No numeric fit percentage or hiring probability is fabricated. Same-day targets remain stable. A past application remains a historical submission even if later judged a poor fit.

Original requirements deliberately deferred: autonomous internet search (only a direct-URL fetch exists), scheduled check-ins, streaks, weekly metrics, CV/resume tailoring (cover-letter drafting exists; editing the CV document itself does not), history import and automatic replacement of stale targets. Historical five applications are kept as a reference, not imported into a new counter. Do not present this as the complete master-prompt product.

Known limitations: single-user sequential file writes; no concurrent writers; snapshots cannot be refreshed in place; current-source validation requires a new review; hard filters depend on model extraction and human verification; quote matching is not semantic validation; no host-enforced sandbox. A production version needs stronger isolation and source-refresh support.

`apply.py` limitations (see docs/APPLY_VALIDATION.md for measured results): only the local `testapp/` fixture is supported; no real ATS has been tested. Repeatable sections (education/employment) are filled up to however many entries the plan lists — the plan does not yet click "Add Another" for extra rows on its own. Login, MFA, CAPTCHA and other page interruptions are not yet specifically detected; the persistent browser profile means a manual login during a paused run is preserved for the next `fill` call, but the tool does not yet recognize an interruption and pause for it automatically. The submit-button denylist is defense-in-depth on page text, which is untrusted; the actual guarantee against submission is that automation never acts at all on the page the plan marks as the review page.

Other new-module limitations: `import_posting.py` cannot execute JavaScript, so many modern ATS pages return thin or empty text — it flags this itself rather than silently ingesting a bad posting, but does not work around it. `answers.py` never judges whether two differently worded questions mean the same thing (exact match only), so a rephrased question still requires a fresh confirmed answer. `coverletter.py` does not draft content — a chat session (or a future extension of `llm_client.py`) must still write the tailored text; this module only stores, gates, and verifies it. `llm_client.py`'s API path has been schema-tested but never actually called against the live Anthropic API in this environment (no key configured), so it is unproven beyond its deterministic parts until run with a real key. `ui.py` is unauthenticated, single-user, and assumes it runs on your own machine — it is not designed to be exposed on a network.

## GitHub and academic evidence
Private data is ignored by git. Publish only generic example profile, code, synthetic fixtures and learning notes. Inspect `git status` before committing; do not force-add private/. Do not upload the whole ZIP to a public repository because it includes your personal profile and history. No API keys are included or required by Python.

Suggested first local steps after reviewing the files:
```
git init
git add .
git status
git commit -m "Add initial Job Radar tools and fictional validation cases"
```
Connect your own GitHub repository through your normal workflow. Invite the instructor and TA if private. This package has not created a remote repository. Make later commits for real changes you review and test; do not fabricate a development history.

AI assistance: this initial scaffold, policy consolidation, fixtures and tests were generated by ChatGPT. Andrés's personal problem, supplied requirements and profile informed the design. His own review and understanding have not yet been demonstrated; document those truthfully in docs/LEARNING_GUIDE.md.

Official host references:
- https://code.visualstudio.com/docs/agents/overview
- https://code.visualstudio.com/docs/agent-customization/custom-instructions
