# Five-minute demonstration

## The command

```powershell
py agent.py radar --max-steps 25 --force-open --task "Review these job opportunities against my verified profile. Tell me which I should prioritize, which require clarification, and which I should skip. Explain the evidence and avoid duplicate applications."
```

Maximise the window first — the trace is wide.

**`--force-open` matters.** Tab-opening is idempotent per day: if you rehearse in the morning, the afternoon run opens nothing because those pages are already in your browser. That is correct behaviour and wrong for a live audience. `--force-open` gives fresh tabs. Drop it and you will likely see `BROWSER OPEN REQUESTS: 0`.

Verify the artefact afterwards:

```powershell
py verify_report.py
```

---

## Measured runtime

Four complete runs: **133s, 154s, 160s, 186s**. Plan for **~2:40**, and know it can reach **3:10**.

The 180-second budget is checked *before* each API call, so the final call can finish past it — 186s is a run that started its last step at ~175s. That is by design: the alternative is killing a call mid-flight and reporting a partial for no reason.

| Time | On screen | What you say |
|---|---|---|
| **0:00–0:35** | Header, steps 1–2 | "One task in my own words. Ten tools. Nothing about the order is scripted." Point at `max steps=25 \| time budget=180s`. |
| **0:35–1:20** | `search_openings`, then `get_posting_details` with several keys | "Live calls to Gartner's and Momentum's own job APIs — not a scraper. It scanned ~360 listings and batched the detail lookups." |
| **1:20–2:00** | `check_application_history` | **The moment.** "Watch — it asked with only a company name. The tool refused: *identity must not be guessed*. Next step it came back with explicit requisition ids. The observation changed its next action." |
| **2:00–2:45** | `select_opportunity`, `export_report` | "Every selection carries a quote checked character-for-character. Requisitions I already applied to are refused as duplicates, against my real ledger." |
| **2:45–3:10** | `NEEDS VERIFICATION`, tabs open, `FINAL ANSWER` | "Gartner 112849 advertised an October 9 deadline. It is still listed with that sentence removed. That might be a repost — it is not proof. So it is kept and reported, but excluded from the shortlist and **no tab opens for it**." |
| **3:10–4:10** | Scroll to coverage, counts, usage | Numbers below. Open the workbook — three sheets. |
| **4:10–5:00** | Questions | Fall back to the limits list. |

---

## The four things worth saying

**1. No fit score exists in Python.** Grep for one. There is no weighting table and no ranking function. Python enforces what is checkable — quote verbatim, duplicate blocked, cap of four, availability conflict held back. The judgment is the model's.

**2. Limits are enforced, not promised.** 25 iterations, 180s checked before each call, 10s network timeouts, no retry loops. Hit either ceiling and you get a workbook named `_PARTIAL`, stamped `PARTIAL` inside, and a non-zero exit.

**3. Scanned ≠ retrieved ≠ judged.** 364 listings were *seen*; 10 had a **filtered requirement excerpt** retrieved; 4 were actually *judged and recorded*. All three are printed separately, because an excerpt is not a complete assessment and conflating them overstates the work.

**4. An OS handoff is not a loaded page.** The run reports `os_handoff_accepted=True`, which means the operating system accepted the request — not that the employer's page rendered or that the role is still open.

---

## Numbers to quote (final rehearsal)

| | |
|---|---|
| Elapsed | 186.5s |
| Iterations | 12 of 25 |
| API requests / retries | 12 / 0 |
| Tool calls / failures | 21 / 0 |
| Listings scanned | 364 |
| Requirement text retrieved | 10 (excerpt-only) |
| Prioritized for review | 3 |
| Needs Verification | 1 (Gartner 112849) |
| Browser open requests | 0 — already open from earlier today; `--force-open` gives 3 |

**Usage, all four categories separate** (rates verified 2026-10-12 against the published pricing table):

| | tokens |
|---|---|
| Uncached input | 219 |
| Cache creation input | 37,896 |
| Cache read input | 291,196 |
| **Total input billed** | **329,311** |
| Output | 13,438 |
| **Estimated cost (list price)** | **$0.72** |

Say "219 **uncached** input tokens" — never "219 input tokens". The run bills 329,311 input tokens in total; caching changes the *rate*, not the volume.

The configured rates were wrong until 2026-10-12: $15/$75 per MTok, carried over from an older model. Published Opus 5 rates are **$5 input / $25 output**, with 5-minute cache writes at $6.25 and cache reads at $0.50. Every cost previously reported here was overstated by exactly 3×. The figure is a **list-price estimate**, not a reading of the account invoice.

Test suite: **76 radar + agent tests**, full suite **193+**, all passing.

---

## Code walkthrough — five places to point at

All in `agent.py`.

**1. The API request.** `client.messages.create(model=, system=, tools=, messages=)`. Direct SDK call, no framework. The whole conversation goes every time — which is why caching exists.

**2. Reading the request.**
```python
tool_uses = [b for b in response.content if getattr(b, 'type', None) == 'tool_use']
```
The response is a list of blocks. That one line is "what did the model ask for?"

**3. Dispatch.** `tools.execute_tool(block.name, block.input, registry=tool_registry)` validates name against registry and arguments against schema, *then* calls the real function. A bad call becomes a recoverable error observation, not a crash.

**4. Returning results.** `'tool_use_id': block.id` — the pairing that makes the loop work. `is_error` is set honestly, which is how the model learned to retry with job ids.

**5. State and termination.** State is the `messages` list: append assistant turn, append results, call again. Every unfinished path exits through one `incomplete()` function, which is also where the partial report is written. `if not tool_uses:` is the subtle one — *the model stopping is not proof of success*, so the run also checks the required outcome tool succeeded and the CLI re-reads the workbook.

---

## If something goes wrong live

| Symptom | Meaning | Response |
|---|---|---|
| `BROWSER OPEN REQUESTS: 0` | Already opened today | You forgot `--force-open`. Say "idempotency working", move on |
| `api_unavailable` + OUT OF CREDIT | Billing | Show the measured runs in `README_RADAR.md` |
| A source `reached=NO` | That API failed | Honest-coverage feature working — point at it |
| Run hits 180s | Time ceiling | A `_PARTIAL` workbook was still written; open it |
| Fewer than 4 selections | Fewer qualified | Correct. Standards are not lowered to fill rows |

Nothing needs a rescue — every failure mode prints what it is.
