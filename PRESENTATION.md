# Five-minute demonstration

## The command

Paste this into PowerShell from the repo root. Nothing else to set up.

```powershell
py agent.py radar --task "Review these job opportunities against my verified profile. Tell me which I should prioritize, which require clarification, and which I should skip. Explain the evidence and avoid duplicate applications."
```

Maximise the window first — the trace is wide, and `ACTION:` lines wrapping mid-argument is the one thing that makes it hard to read.

---

## Timing, built on measured runs

Two consecutive runs took **153.9s** and **133.3s** (12 and 11 iterations of the 25 allowed). Plan for the agent to finish around **2:30**, and know it can reach **3:00**.

| Time | What is on screen | What you say |
|---|---|---|
| **0:00–0:35** | Type the command, press Enter. Steps 1–2 appear. | "One task, in my own words. The model has ten tools. Nothing about the order is scripted — it decides." Point at the header: `max steps=25 | time budget=180s`. |
| **0:35–1:20** | `search_openings`, then `get_posting_details` with several keys at once. | "That is a live call to Gartner's and Momentum's own job APIs — not a scraper, not a cached copy. It scanned ~300 postings. And it batched the detail lookups: six postings, one round trip." |
| **1:20–2:00** | `check_application_history` calls. **This is the moment to watch for.** | "It asked with only a company name. The tool refused — *identity must not be guessed*. Next step it came back with explicit requisition ids. The observation changed its next action. That is the agent loop doing its job." |
| **2:00–2:40** | `select_opportunity` calls, then `export_report`. | "Every selection carries a quote checked character-for-character against the real posting. Two Momentum requisitions I already applied to were refused as duplicates — against my real ledger." |
| **2:40–3:10** | Four browser tabs open. `FINAL ANSWER` prints. | "It opened the official application pages. It cannot fill them — the browser tools are not registered in this mode." |
| **3:10–4:10** | Scroll to `SEARCH COVERAGE`, then the metrics line. Open the workbook. | "Coverage is what it *actually* reached, including anything that failed. Five employers, not my 51-company list — the report says that itself." Then the numbers (see below). |
| **4:10–5:00** | Questions. | Fall back to the honest-limits list. |

If the agent finishes early, open the workbook sooner and spend the time on the coverage sheet — it is the most defensible part of the artefact.

---

## The three things worth saying out loud

**1. No fit score exists in Python.** Search for one — there is no weighting table and no ranking function. If there were, the model would just be restating a number the code already computed. Python enforces what is checkable: quote verbatim, duplicate blocked, at most four. The judgment is the model's.

**2. Limits are enforced, not promised.** 25 iterations, 180 seconds checked *before* each call, 10-second network timeouts, no retry loops. Hit either ceiling and you get a workbook named `_PARTIAL`, stamped `PARTIAL` inside, and a non-zero exit code. A budget-limited run cannot be presented as finished.

**3. Every number on screen was measured.** Token counts come from the API's usage block. The dollar figure is an estimate at list price and the run prints that assumption. `api_calls` is loop iterations; `api_requests` is HTTP attempts; the gap is retries.

---

## Numbers to quote

| | Run A | Run B |
|---|---|---|
| Elapsed | 153.9s | 133.3s |
| Iterations | 12 of 25 | 11 of 25 |
| API requests / retries | 12 / 0 | 11 / 0 |
| Tool calls / failures | 22 / 0 | 18 / 0 |
| Postings scanned | 184 | 304 |
| Selected | 4 | 4 |
| Estimated cost | $2.00 | $1.68 |

Test suite: **193 tests, all passing.**

---

## Code walkthrough — the five things to be able to point at

All in `agent.py`. These are the parts worth understanding.

**1. The API request — line 425.** `client.messages.create(...)` with `model`, `system`, `tools`, `messages`. Direct SDK call. No framework. The whole conversation goes every time, which is why caching exists at line 394.

**2. Reading the request — line 477.**
```python
tool_uses = [b for b in response.content if getattr(b, 'type', None) == 'tool_use']
```
The response is a list of blocks. Some are text, some are tool-use requests. That one line is "what did the model ask for?"

**3. Dispatch — line 547.** `tools.execute_tool(block.name, block.input, registry=tool_registry)` validates the name against the registry and the arguments against the schema, *then* calls the real function. An unknown or malformed call becomes an error observation the model can recover from — not a crash.

**4. Returning results — line 564.** `'tool_use_id': block.id`. This pairing is what makes the loop work: the API matches each result to the request that caused it. Get it wrong and the conversation is malformed. Note `is_error` is set honestly — a failed tool reports as failed, which is how the model learned to retry with job ids.

**5. State and termination.** State is the `messages` list — append the assistant turn (line 538), append the results (line 599), call again. Termination has one shape: every unfinished path goes through `incomplete()`, which is also where the partial report gets written. Line 397 is the iteration ceiling, 400 the time budget, 504 "no more tools requested — but check it wasn't truncated first", 602 the ceiling exit.

The point of line 504: *the model stopping is not proof of success.* The run also checks the required outcome tool actually succeeded, and the CLI re-checks the workbook exists on disk by size.

---

## If something goes wrong live

| Symptom | What it means | What to do |
|---|---|---|
| `api_unavailable` + "OUT OF CREDIT" | Billing, not a bug | Say so, show `README_RADAR.md` §6 for the measured runs |
| A source shows `reached=NO` | That employer's API failed | This is the honest-coverage feature working — point at it |
| Run hits 180s | Time ceiling | A `_PARTIAL` workbook was still written; open it |
| Fewer than 4 selections | Fewer qualified | Correct behaviour. Standards are not lowered to fill rows |

Nothing here needs a rescue. Every failure mode prints what it is.
