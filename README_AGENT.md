# Job Radar — the agent core

This is the part to read and present: a small Python program that runs a real
LLM tool-calling loop from the terminal. Everything else in this repository is
the product that grew around it.

The repetitive problem it attacks: finding suitable openings, comparing vague
requirements against actual experience, preparing truthful answers, and typing
the same facts into form after form.

---

## 1. The three files that matter

| File | Role | Size to explain |
|---|---|---|
| `agent.py` | The loop. Calls the API, reads tool requests, executes them, feeds results back, repeats. | ~1 page |
| `tools.py` | The deterministic side. Five functions that return observations and never judge. | ~2 pages |
| `browser_tools.py` | Milestone B. Five more tools that drive the existing browser worker. Optional at presentation time. | skim |

Supporting cast (pre-existing, reused rather than rewritten): `radar.py`
(storage + validation), `apply.py` (browser worker), `answers.py`,
`import_posting.py`.

---

## 2. The loop, in six steps

From `agent.py` — this is the graded idea, and it is deliberately written out
instead of hidden in a framework:

1. **Call** the Messages API with the task, conversation so far, and tool definitions.
2. **Read** the response for `tool_use` content blocks.
3. **Validate and execute** — the tool name must be in the registry, arguments are
   checked, then the real Python function runs.
4. **Return** each observation as a `tool_result` block carrying the matching
   `tool_use_id`.
5. **Call again** with the extended conversation.
6. **Stop** on a final answer, a human-input requirement, or the step limit (default 12).

```python
for step in range(1, max_steps + 1):
    response = client.messages.create(model=..., system=SYSTEM_PROMPT,
                                      tools=tool_definitions, messages=messages)
    tool_uses = [b for b in response.content if b.type == 'tool_use']
    if not tool_uses:
        return {'status': 'completed', ...}          # the model answered
    messages.append({'role': 'assistant', 'content': response.content})
    results = []
    for block in tool_uses:
        observation, is_error = tools.execute_tool(block.name, block.input, registry=...)
        results.append({'type': 'tool_result', 'tool_use_id': block.id,
                        'content': json.dumps(observation), 'is_error': is_error})
    messages.append({'role': 'user', 'content': results})
```

**Why a loop and not one API call.** A single call can only reason over what we
decided to stuff into the prompt. The loop lets the model notice it needs the
work-authorization section, pull it, see that the status says *unconfirmed*,
check whether this requisition was already applied to, and change its
conclusion from what it would have said on turn one. The observations drive the
reasoning. That is the difference between an agent and a form letter.

---

## 3. Facts vs. judgment — the line this project is built on

**Tools return observations. The model decides.**

| Tool (deterministic Python) | Returns |
|---|---|
| `get_profile_facts(sections)` | Requested profile sections **plus confirmation status** |
| `get_posting(source)` | Posting text, URL, retrieval time, freshness warning |
| `check_application_history(company, job_id)` | Whether this requisition was already applied to |
| `request_missing_fact(question, why)` | Ends the run and hands the question to the human |
| `save_assessment(...)` | Validates and stores **the model's** verdict |

**The model decides:** which facts to fetch next, whether a qualification is
required or preferred, whether a gap is a real mismatch or just missing
evidence, the fit classification, and what to do next.

The rule I coded to: *no tool computes suitability or interprets sponsorship
policy.* If `save_assessment` ever started calculating a fit score, the model
would be reading a number back to us and the agent would be decoration.

**What Python still enforces, regardless of what the model asks for:**

- only registered tools run — unknown names return an error observation
- arguments are validated before anything executes
- duplicate applications are detected from the canonical ledger
- every evidence quote must appear **verbatim** in the posting text
- **there is no submission tool anywhere, by construction**

---

## 4. The anti-fabrication gate

The sharpest guardrail, inherited from `radar.py` and worth demonstrating live:

```python
if not isinstance(quote, str) or (quote and quote not in text):
    raise ValueError('Evidence not found verbatim in posting: ' + dim)
```

Every dimension the model saves must quote the posting *character for
character*. A plausible-sounding paraphrase is rejected. A test
(`EvidenceGate`) feeds in a fabricated quote and asserts the save fails and the
ledger is untouched.

---

## 5. Setup (Windows)

```
cd "C:\Users\andru\Desktop\AL\UTDALLAS\FALL 26 BUAN 6V99 Agentic AI\JobAgent\job-radar-agent"
py -m pip install -r requirements.txt
copy .env.example .env
notepad .env
```

Put your real key in `.env` (`ANTHROPIC_API_KEY=sk-ant-...`), save, close.
`.env` is git-ignored. Never paste a key into a chat window.

Confirm configuration without spending a token:

```
py agent.py check
```

---

## 6. Commands

```
py agent.py check                                              # config only, no API call
py agent.py assess --source momentum_associate_strategy_ops_analyst.json
py agent.py assess --source <draft.json> --json                # full machine-readable run record
py agent.py prepare --run-key <live_run>                       # Milestone B: browser toolset
```

Existing workflow commands still work unchanged (`radar.py`, `apply.py`,
`import_posting.py`).

---

## 7. Failure handling

| Situation | Behaviour |
|---|---|
| Unknown tool requested | Error observation returned; model can recover next turn |
| Invalid arguments | Rejected before execution, with the reason |
| Required fact unknown | `request_missing_fact` ends the run and asks the human |
| Model loops forever | Hard step limit (default 12 API calls) |
| Fabricated evidence | Save rejected; ledger unchanged |
| Browser worker dead | Tools refuse; page state is never assumed |
| Worker command times out | Reported as `UNKNOWN_TIMEOUT`, explicitly **not** as failure, with "do not retry, re-inspect" |
| Human editing the page | Write tools refuse until the human signals resume |

The timeout case is deliberate. The worker keeps executing after the caller
stops waiting, so calling a timeout "failed" would invite a retry that performs
the same action twice.

---

## 8. Measured results

Every run prints real measurements, not promises:

```json
{"elapsed_seconds": ..., "api_calls": ..., "tool_calls": ...,
 "tool_failures": ..., "input_tokens": ..., "output_tokens": ...,
 "stop_reason": "final_answer|human_input_required|max_steps_reached"}
```

**Evidence levels — these are not interchangeable, and this README will not
blur them:**

| Level | What it proves | Status |
|---|---|---|
| Unit tests | Validation and storage logic | ✅ passing |
| Mocked agent runs | Loop mechanics: tool dispatch, id pairing, limits, stops | ✅ 11 tests passing |
| Guard tests | Submission impossible; dead session never read as success | ✅ 11 tests passing |
| Local browser fixtures | Form filling against `testapp/` pages | ✅ passing (pre-existing) |
| **Real API run** | **Model judgment** | ⏳ blocked: needs `.env` key |
| **Live portal run** | **End-to-end on a real employer form** | ⏳ follows the API run |

A mocked run is never reported as a real API run. Passing tests do not
establish that the model's judgment is good — only a real run does.

---

## 9. Five-minute presentation outline

1. **The problem** (30s) — repetitive job applications; the same facts retyped, requirements that need interpreting.
2. **Show the loop** (90s) — `agent.py`, the six numbered steps. Say why a loop beats one call: the model pulls what it needs and revises.
3. **Facts vs. judgment** (60s) — the table in §3. "Tools observe; the model decides. No tool scores fit."
4. **Live demo** (90s) — `py agent.py assess --source ...`. Narrate the tool calls as they print: profile → posting → history → save.
5. **The guardrails** (45s) — verbatim evidence gate, no submit tool, step limit. Show the rejected fabricated quote.
6. **Honest limits** (15s) — tested portals, what still needs a human, what is not yet proven live.

**Code to be able to explain on the spot:** the `for step in range(...)` loop in
`agent.py`; `execute_tool()` in `tools.py`; the verbatim-quote check in
`radar.assess()`; and why `TOOL_REGISTRY` contains no submit function.
