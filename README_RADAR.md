# Job Radar — the terminal demonstration

One command. Real API calls. The model chooses the tools; Python does the facts.

```powershell
py agent.py radar --task "Review these job opportunities against my verified profile. Tell me which I should prioritize, which require clarification, and which I should skip. Explain the evidence and avoid duplicate applications."
```

Runs independently of any chat session. `--no-open` skips tabs entirely; `--force-open` re-opens pages already opened earlier today (needed if you rehearse and present on the same day). Verify the artefact afterwards with `py verify_report.py`.

---

## 1. What happens, in order

```
your task  ->  model decides which tool to call
                 |
                 v
        Python executes the real function
                 |
                 v
        actual observation goes back, tagged with its tool_use_id
                 |
                 v
        model decides again, now knowing what came back
                 |
            (repeat, max 25)
                 v
        dated Excel report  +  up to 4 application pages opened
```

Six deterministic tools do the work; the model does the judging.

| Tool | What it does (Python) | What it does NOT do |
|---|---|---|
| `list_sources` | Reports the five official employer APIs in coverage | Does not rank them |
| `search_openings` | Live search, keyword/location string filter | Cannot see your profile, so cannot judge fit |
| `get_posting_details` | Batch-fetches requirement text, up to 8 at once | Does not summarise or interpret |
| `check_application_history` | Looks up the canonical ledger | Does not decide whether to reapply |
| `select_opportunity` | Verifies the quote is verbatim, blocks duplicates, caps at 4 | **Does not score fit** |
| `export_report` / `open_application_urls` | Writes the workbook; requests at most 4 tabs, once | Cannot fill or submit a form |

**There is no fit score anywhere in Python.** No weights, no ranking function. If there were, the model would be decorative — restating a number Python had already computed. Suitability, priority order and "is this gap disqualifying?" are the model's, argued from text it actually retrieved.

What Python *does* enforce is harder to fake: a quote must appear character-for-character in the retrieved posting, a requisition already in the ledger is refused, and at most four selections can ever be recorded.

---

## 2. Facts versus judgment — a worked example

From a measured run. The model asked whether a company had been applied to, giving only the company name:

```
ACTION: check_application_history(company="Momentum")

Observation (TOOL ERROR):
{ "error": "provide job_id or source_url; identity must not be guessed" }
```

The tool refused. A company name does not identify a requisition, and guessing would silently corrupt duplicate prevention. On the next step the model changed its approach:

```
ACTION: check_application_history(company="Momentum", job_id="8606824002")
ACTION: check_application_history(company="Momentum", job_id="8607054002")
ACTION: check_application_history(company="Momentum", job_id="8608934002")
ACTION: check_application_history(company="Momentum", job_id="8608791002")
```

Two came back already applied. Those two were never selected. **The observation changed the next action** — that is the difference between an agent and a single scripted call.

---

## 3. The five sources

Each was probed directly and the result recorded in `sources.json`.

| Company | ATS | On priority list |
|---|---|---|
| Gartner | Workday | yes |
| JLL | Workday | yes |
| Blackstone | Workday | yes |
| Momentum | Greenhouse | no — see below |
| Elixirr | Greenhouse | no |

Momentum is in coverage deliberately: two of its requisitions are already submitted, so duplicate prevention is exercised against **real history** rather than a contrived test case.

Five employers is not the 51-company priority list, and the report says so on its own coverage sheet. CBRE and CoStar are deliberately absent — both are recorded as blocked by employer sponsorship policy against a confirmed future sponsorship need.

**A listing is not a vacancy.** An employer API returning a row means it listed that row just now. It is not proof the role still accepts applicants. The model is instructed to say so, and it does.

---

## 3b. Opening tabs

Tabs are requested once, at the end, and only for the application-ready shortlist:

- **after** `export_report` wrote a workbook that exists -- never for a shortlist nothing records
- **at most 4 unique URLs**, drawn only from the exported rows
- **idempotent within the run**: a second call, or a finalisation retry, requests nothing
- **idempotent across the day**: a same-day rerun skips what is already open (`--force-open` overrides)
- **honest**: the run reports `os_handoff_accepted`, which means the OS took the request -- not that the page loaded

Discovery, retrieval, assessment and retries open nothing. There is exactly one `webbrowser` call
site in the whole project.

## 3c. Availability conflicts

`availability_conflicts.json` records requisitions where official evidence disagrees with itself.
Such a requisition keeps its assessment and appears under **Needs Verification**, but is excluded
from the application-ready shortlist and gets no tab. The record persists even on runs where the
model does not select it.

Currently held: **Gartner req 112849**. It advertised "Application Deadline: October 9th, 2026";
that date passed, the sentence has since been removed from the posting body, and `startDate` is now
2026-10-10 with `endDate: null` and `canApply: true`. Consistent with a repost -- not proof of one.
`canApply` means Workday is rendering an apply control, not that the employer accepts new
applicants for this cycle. Resolving it needs an answer from Gartner recruiting, not elapsed time.

## 4. Limits, and what happens when they bite

| Limit | Value | Enforced where |
|---|---|---|
| Agent iterations | 25 | checked by the loop; test asserts exactly 25 calls |
| Wall clock | 180s | checked **before** each API call, so no call is spent that cannot be afforded |
| Per-request network | 10s | every HTTP call; **no retry loops** |
| Selections | 4 | `select_opportunity` |
| Browser tabs per run | 4 unique, application-ready only | `open_application_urls` |

Both ceilings exit through a single `incomplete()` path that writes a workbook named `..._PARTIAL.xlsx` and stamped `PARTIAL` inside. Completed assessments survive; the run exits non-zero. **A budget-limited run cannot report as completed** — not in the filename, not in the file, not in the exit code.

`partial` means *the run was cut short*. It does not mean "coverage is narrow" or "questions remain" — those are normal and belong in the summary and in each selection's unresolved questions.

---

## 5. Accounting

`api_calls` counts loop iterations. `api_requests` counts HTTP attempts. `api_retries` counts the difference. Reporting only one would hide either wasted spend or the true iteration count.

Token counts come from the API's own usage block and are **exact**. The dollar figure is an **estimate at stated list prices** — the run prints the assumption so it can be checked against console billing.

Prompt caching matters here: every step resends the whole conversation, so cost grows quadratically with step count. A measured 9-step run billed 274,000 input tokens for roughly 30,000 tokens of distinct content. With caching, a comparable run billed **219** input tokens plus cache reads.

---

## 6. Measured runs

Four complete runs, no cherry-picking: **133s, 154s, 160s, 186s**. Final rehearsal:

| | |
|---|---|
| Elapsed | 186.5s |
| Iterations | 12 of 25 |
| API requests / retries | 12 / 0 |
| Tool calls / failures | 21 / 0 |
| Listings scanned | 364 |
| Postings fully evaluated | 10 |
| Application-ready | 3 |
| Needs Verification | 1 |
| Estimated cost | $2.16 |

Usage, separately: **219 uncached input**, **37,896 cache creation**, **291,196 cache read**,
**13,438 output** -- 329,311 input tokens billed in total. Caching changes the rate, not the
volume; "219 input tokens" alone would be a false claim.

The workbook is verified by READING IT BACK (`py verify_report.py`), not by file size.

---

## 7. What still depends on a human

- **Discovery is programmatic for these five employers only.** The other 46 priority companies are not reachable this way yet. Four probes failed on a wrong site id — those are unfinished leads, not proof of absence.
- **The browser application mode (`agent.py prepare`) is separate and unchanged.** It uses the same verified facts and the same loop, but a different toolset. `radar` has no browser write tools at all — it is structurally incapable of filling a form, not merely told not to.
- **Nothing is ever submitted.** No submission tool is registered in any mode; `build_toolset` refuses to start if one ever appears.
- **Availability is not verified.** The agent reports what the API listed, flags that this is not proof, and leaves confirmation to you.
- **Per-requisition sponsorship is usually unknown.** Most postings say nothing. Silence is recorded as uncertainty, never as approval and never as rejection.
