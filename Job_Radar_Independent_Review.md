# Job Radar — independent implementation review

Review date: September 29, 2026 (UTC). Source snapshot and workbook dated September 28, 2026.

## Assessment

The project has a useful foundation and one recorded successful application: Momentum. The supplied implementation is not yet a reliable end-to-end application agent. Its main problems are browser ownership across handoffs, verification that can report false success, and a workbook whose ranking and history are not consistently derived from authoritative records.

The next engineering session should repair these specific mechanisms, prove them locally, then complete one suitable application through its final review page. Expanding employer coverage before that will multiply the current failures.

This review did not modify the supplied source or workbook, open a live application, submit anything, or use uploaded browser sessions. It does not establish that any previously researched vacancy is available today.

## Scope and evidence

Reviewed `apply.py`, `test_apply.py`, `crow_holdings_inspect_and_handoff.py`, `build_opportunities_workbook.py`, their relevant saved records, and the uploaded Excel workbook. The root and private copies of the Crow script and workbook builder are identical in the received package.

Ten targeted offline probes reproduced undesired behavior in the original code. Browser-facing probes use explicit locator/context doubles; they test Python verification and state-transition logic, not real browser rendering or an employer's server. All writes use temporary directories and fictional data. No answer-bank behavior was tested through the import shim.

The supplied test file contains 48 test methods. An attempt to run it stopped during import because Playwright is not installed in this review environment. Independently, the package omits `answers.py` and `testapp/`, both needed by that suite; it also omits `radar.py` and the dependency manifest. I cannot independently endorse the claimed 48/48 result or audit the complete search/evaluation application from this package.

Source line references below refer to this uploaded snapshot.

## Priority findings

### 1. The Crow login handoff cannot continue filling in its existing browser

**High priority — `crow_holdings_inspect_and_handoff.py`, lines 72–100; `apply.py`, lines 946–960 and 1079–1086.**

After the user signs in and signals resume, the Crow script inspects the page, writes a report, and waits only for a close signal. There is no branch that accepts a prepared plan and executes it on that same `page`. Its message suggests that a separate `apply.py fill` command can continue, but that command launches a new browser context. This is not a continuation of the login-owning page.

The generic engine has a similar dead end: `blocked_validation`, `error`, and `session_lost` use `_hold_for_close_signal`. A resume flag does not resume those states. Keeping a browser open is therefore not equivalent to being able to recover and continue it.

**Repair:** keep one browser-owning worker for the application. It must accept inspect, prepare/fill, resume, status, and explicit close commands against the same page. A recoverable error must be resumable after re-inspection. Do not solve the handoff by running another script against the same profile directory or by navigating to a remembered URL. Uncertain recovery must remain explicit.

This code review does not prove the earlier hypothesis that Avature uses a particular kind of session token. The observed lost session and the unproven token explanation must remain separate.

### 2. “Verified” can mean either a false positive or a false negative

**High priority — `apply.py`, lines 368–404 and 826–936.**

Reproduced cases:

| Case | Received code's result | Correct behavior |
|---|---|---|
| Expected GPA `3.2`, observed `32` | Verified because both normalize to digits `32` | Reject the mismatch; normalization must depend on field type |
| Page says `Upload failed: Fictional_Resume.pdf` | Upload verified because the filename appears somewhere in body text | Require success evidence in the correct attachment widget and no upload error/pending state |
| Existing employment title differs from the intended title | `fill_in_block` returns success with `already_filled` | Classify as a conflict; distinguish an approved manual edit from an incorrect parser value |
| Correct native `select` value is present | Final verification reads `None` and fails | Implement readback for native selects using selected values/options |

Additional static findings: radio buttons are skipped in the final audit, and combobox verification can accept the expected text anywhere in an ancestor container. That text might be an option or adjacent content rather than the selected value.

**Repair:** use field-specific readers and comparators. Phone punctuation normalization belongs to phone fields only. Upload evidence must be tied to the intended Resume/Cover Letter widget. Scope repeating entries to stable record identities and compare complete employer/title/date tuples. Preserve confirmed manual corrections; report conflicts instead of silently treating any non-empty value as correct.

### 3. The state machine can reach review without reaching the review page

**High priority — `apply.py`, lines 1024–1033, 1100–1168 and 1206–1225.**

The engine logs failed page verification but still clicks Next. It looks for validation errors only under `.error.visible`, then advances the plan index. The next plan item can declare `is_review_page` without checking that the live page actually changed.

An offline flow probe left the page URL unchanged after Next and returned a failed field audit. The engine still returned `awaiting_review`. Another probe saved `awaiting_review` with a failed `final_verification` and empty `field_errors`; the next invocation returned that old state without any live check.

The final audit also iterates only the fields listed in the plan. Required live fields missing from the plan can remain invisible to this audit.

**Repair:** distinguish `needs_input`, `verification_failed`, `blocked_validation`, `session_lost`, and `ready_for_review`. Before advancing, verify the required visible field inventory, current page identity, and the actual next-page transition. Do not use URL alone for a single-page application; include stable page landmarks. Read back after a human handoff, since the human may have changed values or advanced the page. Saved status is not live evidence.

### 4. Handoff promises still exceed the implementation

**High priority — `apply.py`, lines 419–438, 939 and 1172–1204.**

- `wait_for_handoff_signal` still has a default four-hour safety cap and raises `TimeoutError`. “No fixed timeout anywhere” is not true. What happens afterward depends on its caller; it is not safe to promise indefinite handoff.
- A headless run skips `pause_after_fill` and continues automatically. A test explicitly expects this behavior. A required human checkpoint should fail closed or remain paused, rather than disappear because of the execution mode.
- `paused_for_review` is not a resumable saved state. A later invocation can fall back to the start rather than reconcile a paused application's real state.
- The session check is optional and applied to one cold-resume path. It is not a mandatory check after every manual handoff.

**Repair:** define explicit ownership and handoff states. No timer-triggered closure while the user is editing. On a disconnected or expired session, preserve evidence, report the loss, and require an explicit recovery path. Do not infer that a running Chrome process means the agent controls the current application tab.

### 5. Workbook coverage loses three distinct target companies

**High priority for coverage accuracy — `build_opportunities_workbook.py`, lines 430–475 and 613–649.**

The company matcher accepts two overlapping words. This merges the following target companies with **Shangri-La Hotels & Resorts** because their names share “Hotels” and “Resorts”:

- Capella Hotels & Resorts
- Four Seasons Hotels & Resorts
- Rosewood Hotels & Resorts

The uploaded Company Coverage sheet contains **56 data rows**, while Read Me claims **59**. The three missing rows are reproducible through `build_registry_rows`. The count calculation and the sheet-building algorithm do not use the same effective identity rules.

**Repair:** assign canonical company IDs with reviewed aliases. Match Citi/Citibank and other real aliases explicitly. Do not deduplicate employers through generic name tokens. Generate both the counts and the sheet from the same resulting rows, and verify every one of the 51 target IDs is represented exactly once.

### 6. The priority shortlist contradicts its own evidence

**High priority for decisions — `build_opportunities_workbook.py`, lines 49–64 and 403–426; Excel: Needs Verification, rows 2–5.**

The four highlighted “only sponsorship unresolved” rows come from a hardcoded set of company/title pairs, not a rule evaluating each row's current gaps.

| Row | Saved evidence problem | Needed correction |
|---|---|---|
| Cognizant, row 5 | The row quotes a restriction against needing company sponsorship now or in the future. The user's future sponsorship need is already confirmed. Its rationale instead treats current authorization as the unresolved issue. | Under the saved wording, this is a known conflict for this requisition. Reverify that wording if considering the role; do not treat it as a company-wide exclusion. |
| Google, row 4 | Its own Gaps cell flags insufficiently evidenced SQL/analytics experience against the one-year requirements. | Keep the experience gap visible. It is not a sponsorship-only case, and general operations tenure does not automatically establish SQL experience duration. |
| PwC, row 3 | The saved text contains a sponsorship restriction with an exception policy. | Verify the exception's applicability. The policy is not simply unstated. |
| Alvarez & Marsal, row 2 | “Direct Application URL” points to a Dallas search-results page. The saved evidence also describes access limitations. | Obtain and verify the actual requisition/application page before calling this a verified, ready opportunity. |

These are internal consistency findings from the supplied workbook, not fresh claims about the employers' current vacancies.

The workbook contains **35 Needs Verification rows across 15 company names**, **0 recommended openings**, and **63 Excluded or Closed rows**. Nine of the 35 Needs Verification rows have no direct application URL; two Bain rows are placeholders with details not reviewed. These are research leads, not 35 verified matching applications ready to execute.

**Repair:** store fit, mandatory qualification conflicts, sponsorship policy, personal authorization facts, location constraints, and evidence freshness separately. Compute ranking from those fields. Unknown employer policy is neither confirmed rejection nor confirmed acceptance. Use per-requisition decisions and preserve uncertainty without asking the user to reconfirm facts already settled.

### 7. Rebuilding the workbook does not refresh availability or reliably reconcile applications

**High priority for history; medium priority for export — `build_opportunities_workbook.py`, lines 23, 403–426, 481–527 and 652–701.**

- The rebuild reads local JSON and hardcoded legacy rows; it performs no live search or availability checks.
- `TODAY` is fixed to `2026-09-28`, and new batch rows receive that value for both discovery and verification dates. Evidence dates are assigned during export instead of being preserved from individual verification events.
- Every imported batch opening is initialized as `Not started` without consulting the submission ledger.
- `build_tracker_rows` iterates company-registry entries rather than the application ledger. It joins the first job for a company, which cannot reliably distinguish multiple requisitions.
- An offline probe showed a registry claim still displayed as `APPLIED` even when the authoritative applications dictionary was empty; only a note said it was not reconciled.
- A second probe showed a real fictional submission in the applications dictionary omitted from the tracker when its company lacked the registry's application-status field.
- Marriott and Bain tracker rows are appended with hardcoded blocked/not-submitted text. They will become stale after their actual status changes.

**Confirmed current history:** `private/state.json` has one submitted application key, `7031bdd0727ab70e`, with `date_applied=2026-09-27`. That agrees with the user's Momentum submission and the current export. No Bain or Marriott submission is recorded in that dictionary. Missing records alone would not prove what happened externally; here the known incomplete status also comes from the conversation.

**Repair:** key every opportunity and application by canonical employer plus requisition/job ID. Build the tracker from the authoritative application/attempt records, then join employer metadata. Derive `APPLIED` only from a confirmed submission record. Separate file generation time from `last_verified_at`; rebuilds must never manufacture verification events. A future watch command needs an actual search/verification stage before this export stage.

### 8. The received review ZIP is not sanitized

**Must fix before sharing the package elsewhere — received artifact vs README.**

The ZIP is **184,736,831 bytes** and contains **2,884 files**, including **2,874 under `private/`**. It contains the CV, personal records and browser-profile/cookie-store files. The README says these are excluded, and the earlier description claimed ten files totaling roughly 50 KB. The received artifact does not match those statements.

I did not read credential/cookie contents or use those sessions. This finding does not establish that the archive was shared publicly or that an account was compromised.

**Repair:** create review exports from an explicit file allowlist into an empty staging folder. Include fictional fixtures and required source dependencies, then inspect the archive member list. Never recursively package `private/`, browser profiles, credentials, real application records or the CV. Keep an explicit manifest so the shared ZIP matches its description.

## Reproduction results

The companion script, `job_radar_audit_probes.py`, exercises the original code with fictional inputs. It can be placed in the project root and run with:

```powershell
py job_radar_audit_probes.py --source-dir .
```

It requires `openpyxl` to import the workbook builder. It deliberately substitutes import-only Playwright/answer-bank modules and minimal browser doubles; it cannot access a live browser or real account. It supports the builder at either the project root or `private/build_opportunities_workbook.py`.

**All ten defect reproductions succeeded against the received code:**

1. Wrong GPA accepted after digit-only comparison.
2. Wrong existing employment title accepted as already filled.
3. Upload error with filename accepted as successful upload.
4. Correct native selection rejected by final verifier.
5. Failed final verification trusted on the next run.
6. Unchanged page after Next still promoted to review status.
7. Four-hour handoff cap still present and its timeout branch reachable.
8. Four distinct hotel companies collapsed into one coverage row.
9. `APPLIED` displayed without an authoritative submission record.
10. An authoritative submission omitted from the tracker.

These assertions intentionally demonstrate the bugs: a passing probe confirms undesired behavior in this snapshot. They are not correctness tests for a repaired version. Convert them to the opposite, desired invariants in the project's own regression suite, then add genuine browser integration tests with the missing fictional fixtures.

## Completion criteria for the next implementation session

1. **One continuous local application:** at least three pages, a manual login/edit checkpoint, a repeated education/employment section, an asynchronous upload and a validation failure. Resume after each checkpoint in the same browser-owning process. Preserve manual values. Refuse a competing worker. An actual lost session must stop writes and be reported.
2. **Truthful verification:** all ten cases above handled correctly; include a required field absent from the plan, a selected option changed after filling, an upload error containing the filename, and a Next click that does not advance. None may produce a ready-for-review status.
3. **Correct records:** all 51 target company IDs represented exactly once, additional employers separate, totals derived from rows, application states joined by job key, actual evidence timestamps retained, and no highlighted claim contradicted by its row's gaps.
4. **One real demonstration:** reverify an already-selected suitable opening, inspect its current form, fill it with confirmed information and the approved CV, advance through its supported sections, and leave the actual verified review tab open. Request only genuinely new required answers or necessary login/consent. Final submission remains manual. Do not use live portals as disposable test fixtures.
5. **Honest handoff:** report the employer, requisition, actual page, verified required fields, attachment evidence, remaining blockers and what still requires the user. Save a status consistent with that report. A filled form is not a submitted application.

The existing approved-plan boundary, profile-path resolution, fictional test fixtures concept, separate manual final submission, and Momentum history should be preserved. Ordinary bug fixes do not need repeated proposals. Cover-letter drafting and additional employer adapters can follow once this acceptance sequence works.

## Ready-to-use implementation instruction

> Read this independent review and reproduce the reported defects against the current source before editing. Reuse the existing project and preserve the profile, confirmed answers, manual corrections and Momentum submission record. Fix browser ownership/resume, verification gates, and canonical job/company/history joins in that order. Integrate Crow's inspect-to-fill handoff into the same live worker; do not close/relaunch or start a second browser to continue a login. Replace false-success checks with field-specific and widget-scoped evidence, verify actual page transitions, and make required human checkpoints mandatory. Repair the Excel inconsistencies without claiming that a local rebuild refreshes job availability. Build regression tests for the desired behavior and a continuous multi-page local demonstration; include all fixtures and dependencies in a sanitized review export. Then reverify one suitable existing opportunity and bring it through final review with the approved CV, preserving the user's login and edits. Fix ordinary implementation bugs within this task; ask only for genuinely new required information or an unavoidable login/consent action. Do not submit. Do not expand employer research before the core workflow meets the review's completion criteria. Report measured outcomes and remaining limitations, not another general proposal.

## Input identification

- ZIP SHA-256: `a11956c29b4f29b10f4c5901fa18ee6cf77cad85b9a0b876dfa1788e9c7ae9c7`
- Excel SHA-256: `9c35690180a523935da59a101f04446f25d64cd9fe79054392c57212448bdec7`
