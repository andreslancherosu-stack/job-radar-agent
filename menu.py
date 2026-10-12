"""Job Radar - terminal menu.

A thin front end over code that already exists. It adds no job-search logic of
its own: the agent loop, the canonical ledger, the duplicate rules and the
report builder are all reused as-is.

What it reuses, by name:
    radar.today()           America/Chicago calendar date
    radar.read / radar.write  atomic state file IO
    radar.key(job)          canonical job key (company + requisition)
    radar.ingest(...)       registers a posting in the ledger
    radar.confirm(...)      THE ONLY path that records an application
    radar.decision(...)     pending-shortlist status
    radar.export(...)       CSV export
    ledger_report.build()   Excel rebuilt from saved records
    agent.py radar          the LLM run (option 1), launched as its own process

Run:  python menu.py      (or double-click Start_Job_Radar.cmd)
"""
import json
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

import ledger_report
import radar

STATE_PATH = Path('private/state.json')

DEFAULT_TASK = ('Review these job opportunities against my verified profile. Tell me which I '
                'should prioritize, which require clarification, and which I should skip. '
                'Explain the evidence and avoid duplicate applications.')

RULE = '=' * 70


def _print(*parts):
    print(*parts)


def clean(text):
    """Strip whitespace plus the invisible characters that arrive when input
    is piped rather than typed -- a UTF-8/16 BOM or a zero-width space. These
    turned a piped "0" into an unrecognised option during launcher testing;
    a human typing never sees them, which is exactly why they are worth
    removing rather than debugging twice."""
    return (text or '').replace('﻿', '').replace('​', '').strip()


def ask(prompt, default=None):
    """Input with a VISIBLE default. Returns the default on an empty line."""
    suffix = ' [%s]' % default if default is not None else ''
    try:
        value = clean(input('%s%s: ' % (prompt, suffix)))
    except EOFError:
        return default
    return value or default


def ask_yes_no(prompt, default=None):
    """Strict yes/no. Anything unrecognised is re-asked rather than guessed --
    this gates whether an application is recorded, so a typo must not become
    a 'yes'."""
    hint = 'y/n' if default is None else ('Y/n' if default else 'y/N')
    while True:
        raw = ask('%s (%s)' % (prompt, hint), '' if default is None else ('y' if default else 'n'))
        if raw is None:
            return False
        raw = raw.strip().lower()
        if raw in ('y', 'yes'):
            return True
        if raw in ('n', 'no'):
            return False
        _print('  Please answer y or n.')


def load_state():
    if not STATE_PATH.exists():
        return radar.empty()
    return radar.read(STATE_PATH)


# ---------------------------------------------------------------------------
# 1. search and evaluate (the LLM run)
# ---------------------------------------------------------------------------

def run_search():
    _print('\nThis runs the real LLM agent loop and costs money.')
    _print('Limits: at most 25 iterations and a 180-second wall-clock budget.')
    if not ask_yes_no('Start the search now?', default=False):
        _print('Cancelled. Nothing was run.')
        return
    task = ask('Task (press Enter for the default)', DEFAULT_TASK)
    force = ask_yes_no('Re-open application pages already opened earlier today?', default=False)
    cmd = [sys.executable, 'agent.py', 'radar', '--max-steps', '25', '--task', task]
    if force:
        cmd.append('--force-open')
    _print('\n' + RULE)
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    # Inherit stdout/stderr so the live trace and any error stay visible.
    code = subprocess.call(cmd, cwd=str(Path(__file__).resolve().parent), env=env)
    _print(RULE)
    _print('Agent exited with code %d%s' % (code, '' if code == 0 else '  (non-zero = not completed)'))


# ---------------------------------------------------------------------------
# 2. record a submitted application
# ---------------------------------------------------------------------------

def choose_job(state):
    """Pick a saved opportunity by number, or paste an official URL.

    Internal job keys are never typed by the user. They are resolved here, by
    radar.key(), from the company and requisition the user can actually see.
    """
    jobs = state.get('jobs', {})
    applications = state.get('applications', {})
    rows = []
    for key, entry in jobs.items():
        p = entry.get('posting', {})
        rows.append((key, p.get('company'), p.get('role'), p.get('job_id'),
                     p.get('source_url'), key in applications))
    rows.sort(key=lambda r: (str(r[1] or ''), str(r[2] or '')))

    _print('\nSaved opportunities:')
    if not rows:
        _print('  (none yet)')
    for i, (_k, company, role, job_id, _u, applied) in enumerate(rows, 1):
        flag = '  [ALREADY RECORDED]' if applied else ''
        _print('  %2d. %-16s %-44s req %s%s'
               % (i, (company or '?')[:16], (role or '?')[:44], job_id or '?', flag))
    _print('   U. Paste an official application URL instead')
    _print('   0. Cancel')

    choice = ask('\nSelect', '0')
    if choice in (None, '0', ''):
        return None
    if choice.strip().lower() == 'u':
        return choose_by_url(state)
    if choice.isdigit() and 1 <= int(choice) <= len(rows):
        return rows[int(choice) - 1][0]
    _print('  Not a valid selection.')
    return None


def choose_by_url(state):
    """Resolve a pasted URL to a saved posting, or register it honestly."""
    url = ask('Paste the official application URL', '')
    if not url:
        return None
    url_norm = url.strip().rstrip('/').casefold()
    for key, entry in state.get('jobs', {}).items():
        saved = (entry.get('posting', {}).get('source_url') or '').strip().rstrip('/').casefold()
        if saved and saved == url_norm:
            _print('  Matched a saved opportunity.')
            return key

    _print('\n  That URL is not in the ledger yet.')
    _print('  To register it I need the facts you already have. I will not invent any of them.')
    if not ask_yes_no('  Register this posting now?', default=False):
        return None

    company = ask('  Company (exactly as the employer writes it)')
    role = ask('  Role title')
    job_id = ask('  Requisition / job ID (blank if the employer shows none)', '')
    if not company or not role:
        _print('  Company and role are required. Nothing was saved.')
        return None

    text = fetch_posting_text(url)
    if not text:
        _print('\n  Could not retrieve the posting text automatically.')
        _print('  The ledger stores a real snapshot, so it will not accept an empty or invented one.')
        _print('  Paste the posting text, then a single line containing just: END')
        lines = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line.strip() == 'END':
                break
            lines.append(line)
        text = '\n'.join(lines).strip()
    if not text:
        _print('  No posting text, so nothing was saved. The ledger stays clean.')
        return None

    job = {'company': company, 'role': role, 'source_url': url.strip(),
           'source_kind': 'user_copy', 'text': text}
    if job_id:
        job['job_id'] = job_id
    try:
        # Reuses radar's own ingestion path and key derivation -- no parallel store.
        result = radar.ingest(state, job, radar.today())
    except ValueError as e:
        _print('  Could not register it: %s' % e)
        return None
    radar.write(STATE_PATH, state)
    _print('  Registered as job key %s%s'
           % (result['key'], ' (already present)' if result['duplicate'] else ''))
    return result['key']


def fetch_posting_text(url):
    """Best-effort retrieval through the paths this project already has."""
    try:
        import import_posting
        text = import_posting.html_to_text(import_posting.fetch_url(url, timeout=15))
        if text and len(text.strip()) > 200:
            return text.strip()
    except Exception:
        pass
    return None


def record_application():
    state = load_state()
    key = choose_job(state)
    if not key:
        _print('\nCancelled. No application was recorded.')
        return

    entry = state['jobs'].get(key)
    if entry is None:
        _print('\nThat job is not in the ledger. Nothing was recorded.')
        return
    posting = entry['posting']

    _print('\n' + RULE)
    _print('You selected:')
    _print('  Company      : %s' % posting.get('company'))
    _print('  Role         : %s' % posting.get('role'))
    _print('  Requisition  : %s' % (posting.get('job_id') or '(none shown by employer)'))
    _print('  Official URL : %s' % posting.get('source_url'))
    _print('  Job key      : %s' % key)
    _print(RULE)

    if key in state.get('applications', {}):
        existing = state['applications'][key]
        _print('\nThis requisition is ALREADY recorded as submitted on %s.'
               % existing.get('date_applied'))
        _print('Recording it again will not create a second application and will not change')
        _print('that date. Nothing further is needed.')
        return

    _print('\nSubmission means the application is FULLY submitted and confirmed.')
    _print('These do NOT count as submitted:')
    _print('  - you opened or filled the form but did not finish')
    _print('  - you clicked Submit but an email/code verification step is still pending')
    _print('  - you are not sure whether it went through')
    if not ask_yes_no('\nDid you complete submission AND receive confirmation?', default=False):
        _print('\nNot recorded. Come back once the submission is confirmed.')
        return

    default_day = radar.today()
    while True:
        day = ask('Actual submission date (YYYY-MM-DD, America/Chicago)', default_day)
        try:
            radar.date.fromisoformat(day)
            break
        except Exception:
            _print('  Please enter a date as YYYY-MM-DD.')

    _print('\nAbout to record: %s - %s on %s' % (posting.get('company'), posting.get('role'), day))
    if not ask_yes_no('Save this to the application ledger?', default=False):
        _print('\nCancelled. No application was recorded.')
        return

    try:
        # The ONLY way this program records an application. Same function the
        # CLI uses, same ledger, same duplicate rule.
        result = radar.confirm(state, key, day, True)
    except (ValueError, KeyError) as e:
        _print('\nRefused: %s' % e)
        return
    radar.write(STATE_PATH, state)

    # Read back from disk -- not from the object we just mutated.
    reread = radar.read(STATE_PATH)
    saved = reread.get('applications', {}).get(key)
    _print('\nSaved. Read back from disk: %s' % json.dumps(saved, ensure_ascii=False))

    # Prove the duplicate rule on the re-read state, without writing it.
    again = radar.confirm(reread, key, day, True)
    _print('Duplicate check: %s' % json.dumps(again, ensure_ascii=False))
    if again.get('counted'):
        _print('!! WARNING: duplicate prevention did not hold. Investigate before trusting this.')
    else:
        _print('Confirmed: a repeat confirmation adds nothing and leaves the original date intact.')

    _print('\nRebuilding the Excel report so the shortlist drops this submitted role...')
    try:
        out = ledger_report.build()
        _print('  New report : %s' % out['report_path'])
        _print('  CSV        : %s' % out['csv_path'])
        _print('  Pending %d | submitted %d' % (out['pending'], out['submitted']))
    except Exception as e:
        _print('  Report rebuild failed (%s: %s). The application IS saved; rebuild from the menu.'
               % (type(e).__name__, e))


# ---------------------------------------------------------------------------
# 3. history   4. rebuild   5. open latest
# ---------------------------------------------------------------------------

def view_history():
    state = load_state()
    pending, submitted = ledger_report.split_records(state)
    _print('\n' + RULE)
    _print('APPLICATION HISTORY (%d submitted)' % len(submitted))
    _print(RULE)
    if not submitted:
        _print('No applications recorded yet.')
    for r in submitted:
        _print('\n  %s - %s' % (r['company'], r['role']))
        _print('    requisition : %s' % (r['job_id'] or '(none)'))
        _print('    submitted   : %s' % (r.get('date_applied') or 'unknown'))
        _print('    provenance  : %s' % r.get('provenance'))
        _print('    result      : %s' % (r.get('result') or 'no outcome recorded'))
        _print('    url         : %s' % r['url'])
    _print('\n%d opportunity(ies) still pending.' % len(pending))


def rebuild_report():
    _print('\nRebuilding from saved records only -- no search, no LLM call, no re-verification.')
    try:
        out = ledger_report.build()
    except Exception as e:
        _print('Failed: %s: %s' % (type(e).__name__, e))
        return
    _print('  New report : %s' % out['report_path'])
    _print('  CSV        : %s' % out['csv_path'])
    _print('  Pending %d | submitted %d | generated %s'
           % (out['pending'], out['submitted'], out['generated_at']))
    _print('  Earlier reports were left untouched as historical snapshots.')


def open_latest_report():
    path = ledger_report.latest_report()
    if path is None:
        _print('\nNo report found under %s. Use option 4 to build one.' % ledger_report.REPORTS_DIR)
        return
    _print('\nOpening %s' % path)
    try:
        if hasattr(os, 'startfile'):
            os.startfile(str(path))                       # noqa: S606 - Windows shell open
        else:
            webbrowser.open('file://%s' % path.resolve())
        _print('  Handed to the operating system. That is not proof Excel opened it.')
    except Exception as e:
        _print('  Could not open it (%s: %s). The file is at the path above.' % (type(e).__name__, e))


# ---------------------------------------------------------------------------
# menu
# ---------------------------------------------------------------------------

MENU = """
======================================================================
  JOB RADAR
======================================================================
  1. Search and evaluate opportunities      (uses the LLM API - costs money)
  2. Record a submitted application         (no LLM call)
  3. View application history               (no LLM call)
  4. Rebuild the Excel report               (no new search, no LLM call)
  5. Open the latest Excel report
  0. Exit
"""

ACTIONS = {
    '1': run_search,
    '2': record_application,
    '3': view_history,
    '4': rebuild_report,
    '5': open_latest_report,
}


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        pass
    os.chdir(Path(__file__).resolve().parent)
    while True:
        _print(MENU)
        try:
            choice = clean(input('Select an option: '))
        except (EOFError, KeyboardInterrupt):
            _print('\nBye.')
            return 0
        if choice == '0':
            _print('Bye.')
            return 0
        action = ACTIONS.get(choice)
        if action is None:
            _print('  Please choose 0-5.')
            continue
        try:
            action()
        except KeyboardInterrupt:
            _print('\nInterrupted. Returning to the menu; nothing was left half-saved.')
        except Exception as e:
            # Keep the error VISIBLE rather than closing the window on it.
            import traceback
            _print('\n' + RULE)
            _print('ERROR: %s: %s' % (type(e).__name__, e))
            traceback.print_exc()
            _print(RULE)
        try:
            input('\nPress Enter to return to the menu...')
        except EOFError:
            return 0


if __name__ == '__main__':
    sys.exit(main())
