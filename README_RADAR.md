# Job Radar — the terminal demonstration

One command. Real API calls. The model chooses the tools; Python does the facts.

```powershell
py agent.py radar --task "Review these job opportunities against my verified profile. Tell me which I should prioritize, which require clarification, and which I should skip. Explain the evidence and avoid duplicate applications."
```

Runs independently of any chat session. Add `--no-open` to skip opening browser tabs.

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
        dated Excel report  +  browser tabs opened
```

Six deterministic tools do the work; the model does the judging.

| Tool | What it does (Python) | What it does NOT do |
|---|---|---|
| `list_sources` | Reports the five official employer APIs in coverage | Does not rank them |
| `search_openings` | Live search, keyword/location string filter | Cannot see your profile, so cannot judge fit |
| `get_posting_details` | Batch-fetches requirement text, up to 8 at once | Does not summarise or interpret |
| `check_application_history` | Looks up the canonical ledger | Does not decide whether to reapply |
| `select_opportunity` | Verifies the quote is verbatim, blocks duplicates, caps at 4 | **Does not score fit** |
| `export_report` / `open_application_urls` | Writes the workbook, opens tabs | Cannot fill or submit a form |

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

## 4. Limits, and what happens when they bite

| Limit | Value | Enforced where |
|---|---|---|
| Agent iterations | 25 | checked by the loop; test asserts exactly 25 calls |
| Wall clock | 180s | checked **before** each API call, so no call is spent that cannot be afforded |
| Per-request network | 10s | every HTTP call; **no retry loops** |
| Selections | 4 | `select_opportunity` |

Both ceilings exit through a single `incomplete()` path that writes a workbook named `..._PARTIAL.xlsx` and stamped `PARTIAL` inside. Completed assessments survive; the run exits non-zero. **A budget-limited run cannot report as completed** — not in the filename, not in the file, not in the exit code.

`partial` means *the run was cut short*. It does not mean "coverage is narrow" or "questions remain" — those are normal and belong in the summary and in each selection's unresolved questions.

---

## 5. Accounting

`api_calls` counts loop iterations. `api_requests` counts HTTP attempts. `api_retries` counts the difference. Reporting only one would hide either wasted spend or the true iteration count.

Token counts come from the API's own usage block and are **exact**. The dollar figure is an **estimate at stated list prices** — the run prints the assumption so it can be checked against console billing.

Prompt caching matters here: every step resends the whole conversation, so cost grows quadratically with step count. A measured 9-step run billed 274,000 input tokens for roughly 30,000 tokens of distinct content. With caching, a comparable run billed **219** input tokens plus cache reads.

---

## 6. Measured runs

Two consecutive end-to-end runs, no cherry-picking:

| | Run A | Run B |
|---|---|---|
| Elapsed | 153.9s | 133.3s |
| Iterations | 12 of 25 | 11 of 25 |
| API requests / retries | 12 / 0 | 11 / 0 |
| Tool calls / failures | 22 / 0 | 18 / 0 |
| Postings scanned | 184 | 304 |
| Selected | 4 | 4 |
| Status | COMPLETE | COMPLETE |
| Estimated cost | $2.00 | $1.68 |

Workbook verified on disk by size in both; 4 tabs opened in both.

---

## 7. What still depends on a human

- **Discovery is programmatic for these five employers only.** The other 46 priority companies are not reachable this way yet. Four probes failed on a wrong site id — those are unfinished leads, not proof of absence.
- **The browser application mode (`agent.py prepare`) is separate and unchanged.** It uses the same verified facts and the same loop, but a different toolset. `radar` has no browser write tools at all — it is structurally incapable of filling a form, not merely told not to.
- **Nothing is ever submitted.** No submission tool is registered in any mode; `build_toolset` refuses to start if one ever appears.
- **Availability is not verified.** The agent reports what the API listed, flags that this is not proof, and leaves confirmation to you.
- **Per-requisition sponsorship is usually unknown.** Most postings say nothing. Silence is recorded as uncertainty, never as approval and never as rejection.
