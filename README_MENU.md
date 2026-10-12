# Job Radar — menu and launcher

## Starting it

**Double-click `Start_Job_Radar.cmd`.** That is the whole setup.

From a terminal instead:

```powershell
py menu.py
```

The launcher changes to its own folder first, so the project's relative paths (`private\state.json`, `sources.json`, `.env`) resolve wherever you start it from. It prefers a project virtual environment (`.venv\Scripts\python.exe`, then `venv\`), then `py`, then `python`. Every path is quoted — the project lives under *"FALL 26 BUAN 6V99 Agentic AI"*, so unquoted paths would fail on the spaces. If anything goes wrong the window **pauses** and shows the exit code instead of vanishing.

## The menu

| | Does | Costs money |
|---|---|---|
| **1** | Search and evaluate opportunities | **Yes** — runs the LLM agent loop |
| **2** | Record a submitted application | No |
| **3** | View application history | No |
| **4** | Rebuild the Excel report from saved records | No |
| **5** | Open the latest Excel report | No |
| **0** | Exit | — |

Only option 1 spends anything, and it asks before starting. It runs the same `agent.py radar` command as before — same handwritten API loop, same **25-iteration ceiling**, same 180-second budget. No framework, no web UI.

## Recording an application (option 2)

Deliberately hard to get wrong, because a false entry corrupts duplicate prevention everywhere else.

1. **Pick from a numbered list** of saved opportunities showing company, role and requisition ID. You never type an internal job key — they are derived by `radar.key()` from what you can actually see. Already-recorded roles are flagged.
2. **Or paste an official URL.** A known URL resolves to its saved posting. An unknown one can be registered, but only through `radar.ingest()` with facts you supply. If the posting text cannot be retrieved, it asks you to paste it and **saves nothing** if you don't — an empty snapshot would be invented metadata.
3. **It shows the exact job** — company, role, requisition, URL, key — before asking anything.
4. **It asks one strict question**, and spells out what does *not* count:
   - opened or filled the form but didn't finish
   - clicked Submit but an email/code verification step is still pending
   - not sure whether it went through

   Anything other than `y`/`n` is re-asked rather than guessed.
5. **Date defaults visibly to today** in America/Chicago (`radar.today()`), and you can backdate it.
6. **It saves through `radar.confirm()`**, re-reads the file from disk, and runs the duplicate check again in front of you.

Re-recording the same requisition **cannot** create a second application and **cannot** silently change the original date — `radar.confirm()` returns `{"counted": false, "reason": "Already recorded"}`. The menu detects this before asking anything and says so.

## Excel

After a successful recording, and any time via option 4, a **new dated workbook** is written:

- **Pending shortlist** — opportunities not yet submitted, with saved decision, fit, and original dates.
- **Application history** — company, role, requisition ID, official URL, submission date, confirmation provenance (taken from the `user_confirmed_application` event), and result.

Submitted roles leave the pending sheet. A CSV goes out alongside, via `radar.export()`, so it stays consistent with the ledger.

Two guarantees worth knowing:

- **Old reports are never touched.** Each rebuild writes a new file. If the name is taken — or Excel holds a lock — it writes `..._v2`, `..._v3`. Excel is never force-closed.
- **Rebuilding never re-verifies anything.** Every date on a row is the date already stored on that record. Rebuild in December and a posting found in October still reads *2026-10-01*. The only "now" in the workbook is the generated-at stamp, labelled as such.

## What this reuses (no parallel logic)

| Reused | From | Why it matters |
|---|---|---|
| `radar.confirm()` | `radar.py` | The **only** path that records an application. The duplicate rule and the demo-posting refusal are its, not a copy. |
| `radar.ingest()` | `radar.py` | Registers a posting; validates required fields and refuses a conflicting snapshot. |
| `radar.key()` | `radar.py` | Canonical company+requisition key, so the menu and the CLI always agree. |
| `radar.today()` | `radar.py` | America/Chicago date, including its missing-tzdata error. |
| `radar.read` / `radar.write` | `radar.py` | Atomic write via temp file + `os.replace`. |
| `radar.decision()` | `radar.py` | RECOMMEND / VERIFY / SKIP on the pending sheet. |
| `radar.export()` | `radar.py` | CSV export. |
| `import_posting.fetch_url` / `html_to_text` | `import_posting.py` | Best-effort posting retrieval for a pasted URL. |
| `agent.py radar` | `agent.py` | Option 1, run as its own process so the live trace stays visible. |

`ledger_report.py` is the one new module: it reads the ledger and writes the workbook. It makes no decisions — `radar.decision()` does that.

## Testing

```powershell
py -m unittest tests.test_menu -v
```

33 tests covering selection, cancellation, successful recording, repeated confirmation, report rebuilding, locked files, timestamp preservation, launcher quoting, and **paths containing spaces** (fixtures build under *"job radar test .../my project folder"*).

Everything runs against a temporary ledger. No real opportunity is marked submitted, no paid search runs, and the paused application browser is never touched.
