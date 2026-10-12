"""Rebuild the Excel report from SAVED RECORDS only.

No search, no network, no LLM call. Everything here comes out of the canonical
ledger (private/state.json) that radar.py already maintains.

Two rules this module exists to enforce:

  * Rebuilding must never make an old posting look freshly verified. Every
    date written to a row is the date already stored on that record
    (date_found, verified_on, assessed_on, date_applied). The only "now" in
    the whole workbook is the report's own generated-at stamp, which is
    labelled as such.
  * Previous reports are historical snapshots. A rebuild always writes a NEW
    dated file and never edits or deletes an existing one. If the target name
    is taken -- or Excel holds a lock on it -- the next free version suffix is
    used instead. Excel is never force-closed.
"""
import datetime
import json
from pathlib import Path

import radar

REPORTS_DIR = Path('private/reports')
STATE_PATH = Path('private/state.json')


def load_state(state_path=STATE_PATH):
    return radar.read(state_path)


def confirmation_provenance(state, key):
    """How this application came to be recorded, from the event log.

    radar.confirm() appends a 'user_confirmed_application' event, so the ledger
    already knows when the user confirmed it. That is reported rather than
    re-derived, and a missing event is said to be missing rather than guessed.
    """
    events = [e for e in state.get('events', [])
              if e.get('key') == key and e.get('action') == 'user_confirmed_application']
    if not events:
        return 'recorded in applications ledger; no confirmation event found in the event log'
    first = min(e['date'] for e in events)
    extra = '' if len(events) == 1 else ' (%d confirmation events; earliest kept)' % len(events)
    return 'user confirmed on %s%s' % (first, extra)


def split_records(state, day=None):
    """(pending, submitted). A submitted requisition is never pending."""
    day = day or radar.today()
    applications = state.get('applications', {})
    pending, submitted = [], []
    for key, entry in state.get('jobs', {}).items():
        posting = entry.get('posting', {})
        assessment = entry.get('assessment') or {}
        row = {
            'key': key,
            'company': posting.get('company'),
            'role': posting.get('role'),
            'job_id': posting.get('job_id'),
            'url': posting.get('source_url'),
            'source_kind': posting.get('source_kind'),
            # Dates are READ, never stamped. A rebuild on a later day must not
            # make any of these move.
            'date_found': entry.get('date_found'),
            'assessed_on': entry.get('assessed_on'),
            'verified_on': posting.get('verified_on'),
            'fit': assessment.get('fit'),
            'next_action': assessment.get('next_action'),
        }
        if key in applications:
            app = applications[key]
            row['date_applied'] = app.get('date_applied')
            row['result'] = app.get('result')
            row['provenance'] = confirmation_provenance(state, key)
            submitted.append(row)
        else:
            try:
                row['decision'] = radar.decision(entry, day)
            except Exception as e:                 # a malformed record must not kill the report
                row['decision'] = 'UNKNOWN (%s)' % type(e).__name__
            pending.append(row)
    pending.sort(key=lambda r: (str(r.get('company') or ''), str(r.get('role') or '')))
    submitted.sort(key=lambda r: str(r.get('date_applied') or ''), reverse=True)
    return pending, submitted


def next_available_path(directory, stem, suffix='.xlsx'):
    """A path nothing else holds.

    Previous reports are snapshots, so this never reuses a name. If Excel has
    the file open, writing to it raises PermissionError on Windows -- the
    answer is a new version, never force-closing the user's Excel.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / (stem + suffix)
    version = 2
    while True:
        if not candidate.exists():
            try:                                   # prove we can actually create it
                with open(candidate, 'xb'):
                    pass
                candidate.unlink()
                return candidate
            except FileExistsError:
                pass
            except PermissionError:
                pass
        candidate = directory / ('%s_v%d%s' % (stem, version, suffix))
        version += 1
        if version > 200:
            raise RuntimeError('could not find a free report filename under %s' % directory)


def build(state=None, state_path=STATE_PATH, reports_dir=REPORTS_DIR, day=None, now=None):
    """Write a new dated workbook from saved records. Returns a summary dict."""
    import openpyxl
    from openpyxl.styles import Alignment, Font

    state = state if state is not None else load_state(state_path)
    day = day or radar.today()
    now = now or datetime.datetime.now()
    pending, submitted = split_records(state, day)

    wb = openpyxl.Workbook()
    bold = Font(bold=True)
    wrap = Alignment(wrap_text=True, vertical='top')

    ws = wb.active
    ws.title = 'Pending shortlist'
    ws.append(['Job Radar - pending shortlist (rebuilt from saved records)'])
    ws['A1'].font = bold
    ws.append(['Report generated', now.strftime('%Y-%m-%d %H:%M:%S')])
    ws.append(['Ledger date used for decisions', day])
    ws.append(['Pending opportunities', len(pending)])
    ws.append(['Submitted (excluded from this sheet)', len(submitted)])
    ws.append(['Source', 'Rebuilt from private/state.json only. No search was run, no posting '
                         'was re-fetched, and no LLM call was made. Every date below is the date '
                         'already stored on that record -- rebuilding does NOT re-verify anything.'])
    ws.append([])
    head = ['Company', 'Role', 'Requisition ID', 'Decision', 'Fit (saved assessment)',
            'First found', 'Assessed on', 'Posting verified on', 'Official URL']
    ws.append(head)
    for c in ws[ws.max_row]:
        c.font = bold
    for r in pending:
        ws.append([r['company'], r['role'], r['job_id'], r.get('decision'), r.get('fit') or '',
                   r.get('date_found') or '', r.get('assessed_on') or '',
                   r.get('verified_on') or 'not verified', r.get('url')])
    for col, width in zip('ABCDEFGHI', (16, 40, 14, 12, 18, 12, 12, 18, 58)):
        ws.column_dimensions[col].width = width

    hs = wb.create_sheet('Application history')
    hs.append(['Applications actually submitted and confirmed'])
    hs['A1'].font = bold
    hs.append(['These come from the canonical applications ledger in private/state.json, which '
               'radar.py writes through its own confirm() function. This sheet is a read-only '
               'view of that ledger, not a second history.'])
    hs.append([])
    hs.append(['Company', 'Role', 'Requisition ID', 'Official URL', 'Submission date',
               'Confirmation provenance', 'Result'])
    for c in hs[hs.max_row]:
        c.font = bold
    for r in submitted:
        hs.append([r['company'], r['role'], r['job_id'], r['url'], r.get('date_applied') or '',
                   r.get('provenance') or '', r.get('result') or 'no outcome recorded'])
    for col, width in zip('ABCDEFG', (16, 40, 14, 58, 16, 48, 22)):
        hs.column_dimensions[col].width = width
    for row in hs.iter_rows(min_row=4):
        for c in row:
            c.alignment = wrap

    stem = 'job_radar_ledger_report_%s' % now.strftime('%Y-%m-%d_%H%M%S')
    path = next_available_path(reports_dir, stem)
    wb.save(path)

    csv_path = Path(reports_dir) / ('job_radar_ledger_%s.csv' % now.strftime('%Y-%m-%d_%H%M%S'))
    radar.export(state, csv_path, day)

    return {'report_path': str(path), 'csv_path': str(csv_path),
            'pending': len(pending), 'submitted': len(submitted),
            'generated_at': now.strftime('%Y-%m-%d %H:%M:%S'), 'ledger_date': day}


def latest_report(reports_dir=REPORTS_DIR):
    files = sorted(Path(reports_dir).glob('*.xlsx')) if Path(reports_dir).exists() else []
    files = [f for f in files if not f.name.startswith('~$')]
    return files[-1] if files else None
