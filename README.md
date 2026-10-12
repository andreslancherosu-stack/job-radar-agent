# Job Radar Agent

BUAN 6V99 solo mini-project. A local Python program that discovers real job openings from official employer APIs, judges them against a verified profile using the Anthropic API, writes a dated Excel report with evidence, and opens the application pages. It never submits an application.

## The graded core: a handwritten LLM tool-calling loop

`agent.py` owns the loop. There is no framework — no LangChain, no LangGraph, no hosted agent runtime — and no coding assistant in the loop at runtime. It calls the Anthropic Messages API directly through the official SDK:

1. Call the API with the task, the conversation so far, and the tool definitions.
2. Read the tool calls the model requested out of the response content blocks.
3. Validate each tool name and its arguments, then execute the real Python function.
4. Send the actual results back, each tagged with its own `tool_use_id`.
5. Call the model again with the extended conversation.
6. Stop on a final answer, a required human answer, the **25-iteration ceiling**, or a 180-second wall-clock budget.

**Facts and judgment are separate, and it is enforced rather than described.** Python retrieves postings, checks the application ledger, validates that every quoted piece of evidence appears verbatim in the real posting, writes the workbook and opens URLs. The model decides what suits the candidate and in what order. There is deliberately **no fit score anywhere in Python** — no weighting table, no ranking function. A tool that computed suitability and asked the model to restate it would make the model decorative.

Both ceilings are real. Hitting either writes a workbook named `..._PARTIAL.xlsx`, stamped `PARTIAL` inside, and exits non-zero. A budget-limited run cannot report as completed.

## Run the demonstration

```powershell
python -m pip install -r requirements.txt
copy .env.example .env          # then put your real Anthropic key in .env
python agent.py check           # confirms configuration without calling the API
```

Then either double-click **`Start_Job_Radar.cmd`** (Windows menu, see `README_MENU.md`) or run it directly:

```powershell
python agent.py radar --max-steps 25 --task "Review these job opportunities against my verified profile. Tell me which I should prioritize, which require clarification, and which I should skip. Explain the evidence and avoid duplicate applications."
```

Verify the artefact afterwards — reading it back, not trusting the file size:

```powershell
python verify_report.py
```

`.env` is git-ignored and must never be committed. `python agent.py check` reports whether a key is configured without printing it.

Measured end to end, four complete runs: **133s, 154s, 160s, 186s**, each using 11–12 of the 25 allowed iterations. A typical run costs roughly **$0.70** at published list prices (an estimate, not a reading of the account invoice).

## Actual discovery coverage: five employers

Discovery is programmatic against official, employer-controlled endpoints — not a scraper and not an aggregator. `sources.json` records every probe and its result:

| Company | ATS | On the priority list |
|---|---|---|
| Gartner | Workday | yes |
| JLL | Workday | yes |
| Blackstone | Workday | yes |
| Momentum | Greenhouse | no — included deliberately |
| Elixirr | Greenhouse | no |

Momentum is in coverage on purpose: two of its requisitions are already recorded as submitted, so duplicate prevention is exercised against real history rather than a contrived case.

**Five employers is not a complete job search.** It is not the candidate's 51-company priority list, and the generated report says so on its own coverage sheet. Employers absent from `sources.json` are absent because no working official feed is wired up for them — never because they were ruled out. Four probes failed on a wrong site id; those are unfinished leads, not proof of absence.

A posting appearing in a search result means the employer's API listed it at that moment. That is **not** proof the vacancy still accepts applicants, and the agent is required to say so.

## The other pieces

`radar.py` is deterministic validation, storage and filtering — not an AI agent. `apply.py` is browser automation for filling an approved application; it never clicks submit. `run_demo.py` is a scripted fixture replay, **not** a model demonstration. `llm_client.py` is an older, narrower standalone API call that predates `agent.py`; the graded loop is `agent.py`.

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
Its output schema is unit-tested against `radar.DIMENSIONS`. This module predates `agent.py` and is NOT the graded loop -- it makes a single call, not an agent loop. It also does not read `private/history_reference.md`, so treat its output as narrower. Use `agent.py` instead.

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

Original requirements deliberately deferred: scheduled check-ins, streaks, weekly metrics, CV/resume tailoring (cover-letter drafting exists; editing the CV document itself does not), history import and automatic replacement of stale targets. (Programmatic discovery is NO LONGER deferred -- see 'Actual discovery coverage' above -- but it reaches five employers, not the open web.) Historical five applications are kept as a reference, not imported into a new counter. Do not present this as the complete master-prompt product.

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

AI assistance, stated plainly: the initial scaffold, policy consolidation, fixtures and tests were generated with ChatGPT; the agent loop (`agent.py`), discovery layer (`discovery.py`), radar toolset (`radar_tools.py`), report builders and the terminal menu were developed with Claude Code. The personal problem, requirements, profile and every judgment call about what counts as honest reporting are the author's. Claude Code was used to BUILD the program; it is not part of the program's runtime -- `agent.py` calls the Anthropic API itself and runs standalone from a terminal.

Official host references:
- https://code.visualstudio.com/docs/agents/overview
- https://code.visualstudio.com/docs/agent-customization/custom-instructions
