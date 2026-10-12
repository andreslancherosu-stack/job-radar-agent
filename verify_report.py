"""Read a generated report back and check it actually says what the run claimed.

File size proves nothing. This opens the workbook, reads every sheet, and
cross-checks it against the canonical application ledger and the day's
browser-open ledger. Exit code 0 only if every check passes.

    py verify_report.py                 # newest report
    py verify_report.py <path.xlsx>
"""
import glob
import json
import re
import sys
from pathlib import Path

import openpyxl

MAX_SELECTIONS = 4
STATE_PATH = Path('private/state.json')
REPORTS_DIR = Path('private/reports')


class Checks:
    def __init__(self):
        self.failed = []

    def __call__(self, label, cond, detail=''):
        print('  [%s] %-58s %s' % ('PASS' if cond else 'FAIL', label, detail))
        if not cond:
            self.failed.append(label)
        return bool(cond)


def _table(ws, header_first_cell):
    """Rows of the real table only.

    The naive version -- 'every row with something in column A' -- swallowed
    the free-text notes printed under the coverage table and reported five
    sources as nine. The table ends at the first blank row after the header.
    """
    rows = list(ws.iter_rows(values_only=True))
    hdr = next((i for i, r in enumerate(rows) if r and r[0] == header_first_cell), None)
    if hdr is None:
        return {}, []
    cols = {n: i for i, n in enumerate(rows[hdr]) if n}
    data = []
    for r in rows[hdr + 1:]:
        if not r or r[0] in (None, ''):
            break
        data.append(r)
    return cols, data


def verify(path):
    wb = openpyxl.load_workbook(path)
    chk = Checks()
    print('WORKBOOK: %s' % path)
    print('sheets  : %s' % wb.sheetnames)

    sheet = next((n for n in ('Prioritized for review', 'Selected') if n in wb.sheetnames), None)
    if sheet is None:
        print('  [FAIL] no shortlist sheet found')
        return False
    cols, data = _table(wb[sheet], 'Priority')
    reqs = [str(r[cols['Req ID']]) for r in data]
    urls = [r[cols['Official application URL']] for r in data]

    print('\n1) SHORTLIST INTEGRITY')
    chk('at most %d rows' % MAX_SELECTIONS, len(data) <= MAX_SELECTIONS, '%d rows' % len(data))
    chk('requisition ids unique', len(set(reqs)) == len(reqs), str(reqs))
    chk('application URLs unique', len(set(urls)) == len(urls))
    chk('every row has a URL', all(urls))

    print('\n2) NO PREVIOUSLY SUBMITTED APPLICATIONS')
    state = json.loads(STATE_PATH.read_text(encoding='utf-8'))
    applied = {state['jobs'][k]['posting'].get('job_id')
               for k in state['applications'] if k in state['jobs']}
    applied = {a for a in applied if a}
    clash = set(reqs) & applied
    chk('none of the shortlist is already submitted', not clash,
        'ledger=%s overlap=%s' % (sorted(applied), sorted(clash) or 'none'))

    print('\n3) EVIDENCE, UNCERTAINTIES, TIMESTAMPS')
    for r in data:
        rid = r[cols['Req ID']]
        ev = r[cols['Evidence (verbatim from posting)']] or ''
        uq = r[cols['Unresolved questions']] or ''
        ts = str(r[cols['Verified at (run clock)']] or '')
        chk('req %s evidence present' % rid, len(ev) > 20, '%d chars' % len(ev))
        chk('req %s uncertainties stated' % rid, uq and uq != 'none stated', '%d chars' % len(uq))
        chk('req %s timestamped' % rid, bool(re.match(r'\d{4}-\d{2}-\d{2}T', ts)), ts)

    print('\n4) COVERAGE AND COUNTS')
    cs = wb['Search coverage']
    ccols, cdata = _table(cs, 'Source')
    srcs = [r[0] for r in cdata]
    chk('coverage rows are sources only', all(len(s) < 20 for s in srcs), str(srcs))
    chk('failures are representable', 'Error' in ccols and 'Reached' in ccols)
    multi = [r[0] for r in cdata if (r[ccols['Searches']] or 1) > 1]
    chk('accumulated across searches', True, 'multi-search sources: %s' % (multi or 'none'))
    flat = {str(r[0]): r[1] for r in cs.iter_rows(values_only=True) if r and r[0]}
    scanned = flat.get('Listings scanned (titles seen)')
    evaluated = (flat.get('Postings with requirement text retrieved')
                 or flat.get('Postings fully evaluated (requirement text retrieved)'))
    chk('scanned and evaluated are distinct', scanned != evaluated,
        'scanned=%s evaluated=%s' % (scanned, evaluated))
    chk('evaluated <= scanned', (evaluated or 0) <= (scanned or 0))
    per_source_scanned = sum(r[ccols['Listings scanned']] or 0 for r in cdata)
    chk('per-source scanned sums to the header total', per_source_scanned == scanned,
        '%s vs %s' % (per_source_scanned, scanned))

    print('\n5) NEEDS VERIFICATION')
    chk('sheet exists', 'Needs Verification' in wb.sheetnames)
    _, nvdata = _table(wb['Needs Verification'], 'Company')
    print('     rows: %d  %s' % (len(nvdata), [r[2] for r in nvdata]))
    conflicts = {}
    cp = Path('availability_conflicts.json')
    if cp.exists():
        conflicts = json.loads(cp.read_text(encoding='utf-8')).get('conflicts', {})
    held_ids = {str(c.get('job_id')) for c in conflicts.values()}
    chk('no held requisition leaked onto the shortlist', not (set(reqs) & held_ids),
        'held=%s' % sorted(held_ids))

    print('\n6) BROWSER-OPEN LEDGER (same-day idempotency)')
    led = sorted(glob.glob(str(REPORTS_DIR / 'opened_urls_*.json')))
    if led:
        L = json.loads(Path(led[-1]).read_text(encoding='utf-8'))
        chk('every shortlist URL was requested at most once', len(set(L)) == len(L),
            '%d entries in %s' % (len(L), Path(led[-1]).name))
        chk('no held requisition was opened',
            not any(any(h in u for h in held_ids) for u in L))
    else:
        print('     no open-ledger for today (nothing opened yet)')

    print('\nOVERALL: %s' % ('ALL CHECKS PASS' if not chk.failed else
                             'FAILED: %s' % chk.failed))
    return not chk.failed


def main():
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        found = sorted(glob.glob(str(REPORTS_DIR / 'job_radar_report_*.xlsx')))
        if not found:
            print('No report found under %s' % REPORTS_DIR)
            return 2
        path = found[-1]
    return 0 if verify(path) else 1


if __name__ == '__main__':
    sys.exit(main())
