# Local test application (fictional)

A static, 4-page fixture that imitates a typical ATS application flow, used
to develop and test the browser-automation loop in `apply.py` before
pointing it at any real employer site. Nothing here is a real job or a real
employer. No network calls; all answers are stored in the browser's
`localStorage`, so refreshing or reopening a page preserves progress
(useful for testing pause/resume).

Pages:
1. `page1.html` - personal information (text fields, dropdown, checkbox).
2. `page2.html` - repeatable education and employment entries ("Add Another").
3. `page3.html` - conditional question (sponsorship follow-up), skill
   checkboxes, resume/cover-letter upload, required certification checkbox,
   and an optional marketing checkbox that must never be auto-checked.
4. `page4_review.html` - read-only summary and the `#submit-application-btn`
   button. Automation must never click this button; only a human does.

Client-side validation mirrors common real-world friction: required-field
errors, an email format check, a 5-digit ZIP check, and a conditional
required field.

## Run it

```
py -m http.server 8765 --directory testapp
```

Then open http://localhost:8765/page1.html, or point `apply.py` at that URL.
