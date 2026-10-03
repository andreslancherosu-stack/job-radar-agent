---
name: ukg-ultipro-portal
description: Orchestrates apply.py's worker architecture to fill a UKG/UltiPro (recruiting2.ultipro.com) job application. Use whenever the current application's URL contains ultipro.com. Contains PLATFORM-level knowledge only (how UKG/UltiPro pages behave) — never personal data (stays in private/profile.json + private/answer_bank.json) and never employer-specific field mappings (stay in private/portal_mappings/ukg_ultipro/, validated live per posting).
---

# UKG/UltiPro portal skill

This skill is instructions for driving `apply.py`'s worker (`worker-start` /
`worker-send`), not a separate engine. Never duplicate apply.py's logic here
— if a capability is missing, add it to apply.py and test it locally first
(see `tests/test_apply.py`), then use it through the worker.

## Platform quirks (apply to ANY UKG/UltiPro employer, confirmed on a live form)

1. **Duplicate field IDs.** Contact-info fields (`#FirstName`, `#FamilyName`,
   `#Phone`, `#Country`, `#AddressLine1`, `#City`, `#State`, `#PostalCode`,
   ...) commonly resolve to TWO elements — one live, one a hidden
   `readonly` duplicate. Always suffix selectors with `:visible`
   (`#FirstName:visible`) to avoid a Playwright strict-mode error.

2. **Resume upload triggers server-side parsing and pre-fill.** After a
   successful upload, the page shows a banner like *"<file> has been used
   to pre-fill part of your application."* and auto-creates repeating
   Work Experience / Education blocks from the resume. The per-field
   `apply_step` upload check (which inspects the upload widget's own
   nearby DOM) can report a false failure even when the upload succeeded
   server-side — always independently confirm via a fresh `inspect` and
   look for that banner text, not just the per-step result.

3. **Resume-parser errors reproduce.** The same garbled-title parsing bug
   seen on other ATS platforms (a description fragment landing in a Title
   field) can recur here. Review parsed Work Experience/Education blocks
   against `private/profile.json`'s `resume_employment_entries` /
   `resume_education_entries` before trusting them.

4. **Repeating Work Experience / Education blocks have NO unique
   per-field ids for most fields** (Job Title, Company, dates are
   `"id": null` in `inspect`'s field dump) — positional/xpath-following
   targeting is unsafe (this is the exact class of bug
   `find_block_containers` exists to prevent). `School Name` and `Degree`
   in Education blocks DO have unique ids (`NewEducation_SchoolId{N}`,
   `NewEducation_DegreeId{N}`) — safe to target directly with `:visible`.
   To correct one wrong value in an unlabeled repeating field, use the
   `fix_field_by_current_value` worker command (locates by the field's
   own current wrong content, never by position) — or, when the fix is
   fast and the human is already looking at the screen, it is often
   faster and equally safe to just ask them to correct it directly.

5. **Question radio groups use generic, non-descriptive internal names**
   (`MultipleChoiceResponse0`, `MultipleChoiceResponse4`, ...) that do
   NOT reveal which real question they answer, and are not guaranteed to
   stay in the same order between form loads. Native `<select>` values
   (e.g. Country) are sometimes internal GUIDs, not the visible text.

   **Never** target a question by its internal group name, page
   position, or a page-wide `get_by_label("Yes")` search (every question
   shares the same "Yes"/"No" labels). **Always** use the
   `answer_question` worker command, which wraps
   `find_question_container` / `answer_radio_question` /
   `read_question_container_state` (apply.py): it locates the ONE
   container whose own visible text matches the question, selects the
   option by its label WITHIN that container, and reads back the
   checked state from that SAME container to verify. Refuses (does not
   guess) if the question text doesn't resolve to exactly one container.

## Question-mapping workflow (validate once, reuse carefully)

Mappings are **employer/posting-specific** and live in
`private/portal_mappings/ukg_ultipro/<employer_key>__<requisition_id>.json`
— never mixed with this platform-level skill, never with personal data.

1. For each question, call `answer_question` with the real question text
   read from a fresh `inspect`. Confirm `result.ok` and that
   `result.verification.checked_labels` matches the intended answer.
2. Only after that live verification, append an entry to the mapping
   file: `{"question_text": "...", "options": [...], "internal_group_name":
   "..." (recorded for reference only, never relied on), "validated_at":
   "YYYY-MM-DD"}`.
3. **Before reusing a saved mapping** (e.g. resuming the same application,
   or a near-identical posting from the same employer), re-run `inspect`
   and re-check that the container found for the saved `question_text`
   still exposes the same `options` list. If it doesn't match, treat the
   mapping as stale — fall back to step 1, don't force it.
4. A mapping file is evidence of what was verified, not a script — it is
   read and applied by whoever (human or Claude) is driving the worker
   next, always through the same verify-then-save discipline.

## Standard session sequence

```
python apply.py worker-start <run_key> --profile private/profile.json --headed --url <posting_url>
python apply.py worker-send <run_key> --cmd inspect                       # read-only, always first
# hand off "Apply now" + login to the human (submit-button denylist; never bypassed)
python apply.py resume <run_key>
python apply.py worker-send <run_key> --cmd fill_steps --steps-json <file>
python apply.py worker-send <run_key> --cmd answer_question --question-text "..." --answer-label "Yes"
python apply.py worker-send <run_key> --cmd inspect                       # re-verify before advancing
python apply.py worker-send <run_key> --cmd status                       # anytime
python apply.py worker-send <run_key> --cmd close                        # only when the human is fully done
```

Never restart a worker mid-application to pick up new apply.py code —
Python does not hot-reload a running process, and a restart risks the
live, unsaved DOM state (parsed resume pre-fill, corrected fields). If a
capability is missing, either use a same-session workaround (ask the
human for a fast manual fix) or get the human's explicit go-ahead before
restarting.

## What stays out of this skill

- Personal data: `private/profile.json`, `private/answer_bank.json`.
- Employer-specific question mappings: `private/portal_mappings/ukg_ultipro/`.
- The actual automation engine: `apply.py` (this skill only orchestrates it).
