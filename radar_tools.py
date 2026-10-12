"""Deterministic tools for the discovery-and-report run.

Division of labour, enforced here rather than merely described:

  Python decides   what was retrieved, whether a quote is real, whether a
                   requisition is already in the ledger, what the workbook
                   says, which URLs got opened.
  The model decides whether a posting suits the user, how to rank what it
                   found, what is still unresolved, and what to do next.

There is deliberately NO fit score, no weighting table and no ranking
function in this file. A tool that computed suitability and asked the model
to restate it would make the model decorative, which is exactly the failure
mode this project is graded against.

Run-scoped state lives in RUN. It is reset per run so a tool can refer to a
posting by a short key ("gartner:112849") instead of the model having to
quote whole postings back -- that is a context-cost decision, not cosmetic.
"""
import datetime
import json
import re
import webbrowser
from pathlib import Path

import discovery
import tools as core_tools

REPORTS_DIR = Path('private/reports')
CONFLICTS_PATH = Path('availability_conflicts.json')

MAX_SELECTIONS = 4

# Visible slice of a search result. See search_openings for why this is a
# recurring cost rather than a one-off.
MAX_CANDIDATES_SHOWN = 45


class Run:
    """Everything one radar run accumulates. Reset per run, never global state
    that leaks between runs."""

    def __init__(self):
        self.candidates = {}        # "source:job_id" -> compact row
        self.details = {}           # "source:job_id" -> full detail incl. full text
        self.full_text = {}         # "source:job_id" -> text used for the evidence gate
        self.coverage = []          # what was ACTUALLY reached, per source
        self.selections = []        # model's picks, validated
        self.searches = 0
        self.report_path = None
        self.opened_urls = []
        self.opened_set = set()   # urls already REQUESTED this run (idempotency)
        self.started = datetime.datetime.now()

    def scanned_count(self):
        """Listings SEEN (title/location rows returned by an employer API),
        summed across every search."""
        return sum(c.get('returned_by_source', 0) for c in self.coverage)

    def evaluation_counts(self):
        """Honest depth-of-work figures.

        "Fully evaluated" was a misnomer and is gone. Retrieving a FILTERED
        excerpt of requirement lines is not a complete assessment: the excerpt
        is capped, drops lines, and says so itself. These three numbers are
        different things and are reported as such:

          excerpt_retrieved  requirement text pulled, but filtered and capped
          full_text_retrieved the entire posting was pulled
          judged             the model actually formed a recorded judgment
                             (i.e. selected it); a posting it read and silently
                             passed over is NOT counted as judged
        """
        full = sum(1 for d in self.details.values() if d.get('full_text') is not None)
        return {
            'listings_scanned': self.scanned_count(),
            'postings_retrieved': len(self.details),
            'excerpt_only': len(self.details) - full,
            'full_text_retrieved': full,
            'judged_and_recorded': len(self.selections),
        }

    def key(self, row):
        return '%s:%s' % (row.get('source'), row.get('job_id'))

    def record_coverage(self, entries):
        """Union this search's coverage into the run's. A source counts as
        reached if ANY search reached it; scanned/kept totals add up; an error
        is kept only while that source has never succeeded."""
        by_source = {c['source']: c for c in self.coverage}
        for e in entries:
            prev = by_source.get(e['source'])
            if prev is None:
                by_source[e['source']] = dict(e, searches=1)
                continue
            prev['searches'] = prev.get('searches', 1) + 1
            prev['returned_by_source'] += e['returned_by_source']
            prev['kept_after_filter'] += e['kept_after_filter']
            if e['reached']:
                prev['reached'] = True
                prev['error'] = None if prev.get('reached') else prev.get('error')
                if e.get('total_reported_by_source') is not None:
                    prev['total_reported_by_source'] = e['total_reported_by_source']
            elif not prev['reached']:
                prev['error'] = e.get('error') or prev.get('error')
        self.coverage = sorted(by_source.values(), key=lambda c: c['source'])


RUN = Run()


def reset_run():
    global RUN
    RUN = Run()
    return RUN


def availability_conflict(key):
    """Official evidence that disagrees with itself about whether a
    requisition currently accepts applicants, or None.

    Deterministic and file-driven, so the exclusion is enforced by code rather
    than by remembering to mention it. Resolving one needs NEW evidence, not
    elapsed time.
    """
    try:
        data = json.loads(CONFLICTS_PATH.read_text(encoding='utf-8'))
    except Exception:
        return None
    return (data.get('conflicts') or {}).get(key)


_COUNT_CLAIM = re.compile(
    r'(\d[\d,]{1,6})\s*(?:\+\s*)?'
    r'(?:postings?|listings?|roles?|jobs?|openings?|results?)?\s*'
    r'(scanned|searched|read in detail|read in full|reviewed|evaluated|assessed|detailed)',
    re.IGNORECASE)


def contradicted_counts(summary, counts):
    """Count-claims in the model's prose that disagree with recorded metrics.

    Measured on the 2026-10-12 08:14 run: the model's own summary said
    "184 postings scanned, 16 read in detail" while the tools had recorded 244
    scanned and 12 retrieved -- two contradictory sets of numbers inside the
    same workbook. Prose is not a measurement. The recorded metrics are
    authoritative, and any disagreement is surfaced in the report rather than
    left for a reader to trip over.
    """
    if not summary:
        return []
    allowed = {int(v) for v in counts.values() if isinstance(v, int)}
    problems = []
    for m in _COUNT_CLAIM.finditer(summary):
        try:
            claimed = int(m.group(1).replace(',', ''))
        except ValueError:
            continue
        if claimed in allowed:
            continue
        verb = m.group(2).lower()
        if verb in ('scanned', 'searched'):
            truth = 'listings scanned = %d' % counts['listings_scanned']
        elif verb in ('read in full',):
            truth = 'full text retrieved = %d' % counts['full_text_retrieved']
        elif verb in ('assessed', 'evaluated', 'reviewed'):
            truth = ('judged and recorded = %d (requirement text retrieved for %d)'
                     % (counts['judged_and_recorded'], counts['postings_retrieved']))
        else:
            truth = 'postings with requirement text retrieved = %d' % counts['postings_retrieved']
        problems.append('narrative claims %s %s; recorded metric is %s'
                        % (m.group(1), verb, truth))
    return problems


def _today():
    return datetime.date.today().isoformat()


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------

def list_sources():
    """What this run is ABLE to search, and what it deliberately is not."""
    by_key, raw = discovery.load_sources()
    return {
        'sources': [{'key': s['key'], 'company': s['company'], 'ats': s['ats'],
                     'on_priority_list': s.get('on_priority_list', False),
                     'verified_on': s.get('verified_on'), 'note': s.get('note')}
                    for s in by_key.values()],
        'coverage_limits': raw.get('known_coverage_limits'),
        'probed_and_rejected': [p['target'] for p in raw.get('probed_and_rejected', [])],
        'rule': ('This is the complete list of employers reachable programmatically in this '
                 'run. It is NOT the user\'s 51-company priority list. Any conclusion drawn '
                 'must be scoped to these employers only.'),
    }


def search_openings(source_keys=None, keywords=None, locations=None, limit_per_source=60,
                    search_text=None):
    """Live search across official employer APIs. Returns COMPACT rows.

    The keyword/location filter is a mechanical string match so the model
    receives tens of candidates rather than hundreds. It encodes no opinion
    about suitability: it cannot see the profile.
    """
    by_key, _ = discovery.load_sources()
    source_keys = source_keys or list(by_key)
    rows, coverage = discovery.search_all(
        source_keys, keywords=keywords, locations=locations, search_text=search_text,
        limit_per_source=limit_per_source, sources=by_key)
    RUN.searches += 1
    # ACCUMULATE, never replace. Measured bug (2026-10-11): the model ran a
    # second, narrower search near the end of the run; that overwrote
    # RUN.coverage, and the report's "actual search coverage" sheet then
    # listed a single source -- under-reporting four employers that really had
    # been searched. Coverage is a claim about the whole run, so it has to be
    # the union of every search the run performed.
    RUN.record_coverage(coverage)
    # Every candidate row returned here is re-sent to the model on EVERY
    # subsequent step, so this list is a recurring cost, not a one-off.
    # Measured: broadening coverage from 17 to 156 candidates pushed one
    # 9-step run from 168k to 274k input tokens. The full set stays available
    # to later tools via RUN.candidates; only the visible slice is capped, and
    # the cap announces itself so a truncated list is never mistaken for the
    # whole result.
    shown = []
    for r in rows:
        k = RUN.key(r)
        RUN.candidates[k] = r
        if len(shown) < MAX_CANDIDATES_SHOWN:
            row = {'key': k, 'company': r['company'], 'title': r['title'],
                   'location': r['location']}
            if r.get('deadline'):
                row['deadline'] = r['deadline']
            shown.append(row)
    reached = [c for c in coverage if c['reached']]
    omitted = len(rows) - len(shown)
    return {
        'candidates': shown,
        'candidate_count': len(rows),
        'candidates_shown': len(shown),
        'candidates_omitted': omitted,
        'omission_note': (
            'Showing the first %d of %d matches to keep this observation small. The other %d '
            'were found and are NOT lost -- narrow `keywords` or `locations`, or set a more '
            'specific `search_text`, to surface different ones.' % (len(shown), len(rows), omitted)
        ) if omitted else None,
        'coverage': coverage,
        'sources_reached': len(reached),
        'sources_failed': [c['source'] for c in coverage if not c['reached']],
        'scanned_total': sum(c['returned_by_source'] for c in coverage),
        'note': ('Titles and locations only -- no requirement text yet. Call '
                 'get_posting_details with the keys you care about (batched, several at '
                 'once) to read requirements and any sponsorship language. '
                 'A row appearing here means the employer\'s API listed it right now; it '
                 'is not by itself proof the vacancy is open to new applicants.'),
    }


def get_posting_details(keys, full_text=False):
    """Batch-fetch requirement text for several postings in ONE tool call."""
    if not keys:
        return {'error': 'keys must be a non-empty list of candidate keys from search_openings'}
    if isinstance(keys, str):
        keys = [keys]
    if len(keys) > 8:
        return {'error': 'at most 8 keys per call (got %d) -- batch, but keep observations '
                         'small enough to stay useful' % len(keys)}
    unknown = [k for k in keys if k not in RUN.candidates]
    if unknown:
        return {'error': 'unknown candidate key(s) %s -- they must come from a '
                         'search_openings result in THIS run' % unknown}
    rows = [RUN.candidates[k] for k in keys]
    details = discovery.fetch_details(rows, full_text=full_text)
    out = []
    for k, d in zip(keys, details):
        if d.get('error'):
            out.append({'key': k, 'error': d['error']})
            continue
        RUN.details[k] = d
        if full_text:
            RUN.full_text[k] = d.get('full_text', '')
        else:
            # Keep a copy of the complete text for the evidence gate even
            # though the model only saw an excerpt -- so a legitimate quote
            # from the excerpt validates, and a fabricated one still fails.
            full = discovery.fetch_detail(RUN.candidates[k], full_text=True)
            RUN.full_text[k] = full.get('full_text', '')
        entry = {'key': k, 'company': d.get('company'), 'title': d.get('title'),
                 'location': d.get('location'), 'url': d.get('url'),
                 'posted': d.get('updated'), 'deadline_field': d.get('deadline_field'),
                 'still_listed': d.get('still_listed'), 'can_apply': d.get('can_apply')}
        if full_text:
            entry['full_text'] = d.get('full_text')
        else:
            entry.update({kk: d.get(kk) for kk in (
                'sponsorship_sentences', 'deadline_sentences', 'requirement_lines',
                'requirement_lines_omitted', 'full_text_chars', 'shown_chars',
                'omission_note')})
        out.append(entry)
    return {'postings': out, 'count': len(out)}


def select_opportunity(key, priority, why_suitable, evidence_quote,
                       unresolved_questions=None, next_action=None):
    """Record ONE opportunity the model judges suitable. Python validates; it
    does not re-judge.

    Three gates, each a real failure mode this project has already hit:
      * the quote must appear VERBATIM in the retrieved posting
      * the requisition must not already be in the canonical ledger
      * at most MAX_SELECTIONS, so a run cannot pad the report
    """
    if key not in RUN.details:
        return {'error': 'call get_posting_details for %r before selecting it -- a selection '
                         'must rest on retrieved text, not on a title' % key}
    if len(RUN.selections) >= MAX_SELECTIONS:
        return {'error': 'already selected the maximum of %d opportunities' % MAX_SELECTIONS}
    if any(s['key'] == key for s in RUN.selections):
        return {'error': '%r is already selected' % key}

    d = RUN.details[key]
    quote = (evidence_quote or '').strip()
    if len(quote) < 12:
        return {'error': 'evidence_quote must be a substantive verbatim span from the posting'}
    haystack = re.sub(r'\s+', ' ', RUN.full_text.get(key, ''))
    if re.sub(r'\s+', ' ', quote) not in haystack:
        return {'error': 'Evidence not found verbatim in the retrieved posting for %s. Quote '
                         'exactly what the employer wrote, or do not claim it.' % key}

    hist, hist_err = core_tools.execute_tool('check_application_history', {
        'company': d.get('company'), 'job_id': str(d.get('job_id') or ''),
        'source_url': d.get('url')})
    if not hist_err and hist.get('already_applied'):
        return {'error': 'DUPLICATE: the ledger already records an application to %s (%s). Do '
                         'not select it.' % (key, hist.get('application_record'))}

    # Availability conflicts route to Needs Verification rather than being
    # dropped. The assessment is still real work and the opportunity may well
    # be live -- what is NOT established is whether it currently accepts
    # applicants, and opening a tab for a possibly-closed requisition presents
    # it as actionable when it is not.
    conflict = availability_conflict(key)

    sel = {'key': key, 'company': d.get('company'), 'title': d.get('title'),
           'job_id': d.get('job_id'), 'location': d.get('location'), 'url': d.get('url'),
           'priority': priority, 'why_suitable': why_suitable, 'evidence_quote': quote,
           'unresolved_questions': unresolved_questions or [],
           'next_action': next_action,
           'posted': d.get('updated'), 'deadline_field': d.get('deadline_field'),
           'still_listed': d.get('still_listed'), 'can_apply': d.get('can_apply'),
           'availability_verified': conflict is None,
           'availability_note': (conflict or {}).get('why_unresolved'),
           'availability_evidence': availability_evidence(d),
           'verified_at': datetime.datetime.now().isoformat(timespec='seconds')}
    RUN.selections.append(sel)
    out = {'recorded': True, 'key': key, 'selections_so_far': len(RUN.selections),
           'remaining_slots': MAX_SELECTIONS - len(RUN.selections),
           'duplicate_check': 'passed -- not in the canonical ledger',
           'availability_verified': conflict is None}
    if conflict:
        out['routed_to'] = 'needs_verification'
        out['warning'] = (
            'AVAILABILITY UNVERIFIED for %s: %s This selection is KEPT and reported, but it is '
            'excluded from the application-ready shortlist and NO browser tab will be opened for '
            'it. Say so in your summary.' % (key, conflict.get('why_unresolved')))
    return out


def availability_evidence(detail):
    """What the SOURCE actually said about this requisition being open.

    Previously every row that lacked a recorded conflict was stamped
    availability_verified=true, which overstated the evidence in two ways at
    once. Greenhouse exposes no accept-applications flag at all, so a missing
    can_apply was being read as confirmation; and Workday's canApply only
    means it is rendering an apply control. Neither is the employer asserting
    that it is still taking candidates. A missing flag is UNKNOWN -- it is
    neither proof of closure nor proof of acceptance.
    """
    source = (detail or {}).get('source') or ''
    can_apply = (detail or {}).get('can_apply')
    listed = (detail or {}).get('still_listed')
    if source in ('momentum', 'elixirr') or (detail or {}).get('ats') == 'greenhouse':
        flag = ('This source (Greenhouse) does not expose an accept-applications flag. '
                'Absence of one is UNKNOWN, not closure and not acceptance.')
    elif can_apply is True:
        flag = ('Workday reports canApply=true, which means it is rendering an apply control -- '
                'not that the employer confirmed it is still taking candidates.')
    elif can_apply is False:
        flag = 'Workday reports canApply=false.'
    else:
        flag = 'No accept-applications flag was returned by this source.'
    return {'listed_at_retrieval': bool(listed),
            'accept_flag_reported': can_apply,
            'acceptance_confirmed': False,          # no source we read ever asserts this
            'evidence_note': flag}


def prioritized_for_review(selections=None):
    """Shortlist rows with no recorded availability CONFLICT.

    Deliberately NOT called "application-ready". Every row here still carries
    unresolved fit or eligibility questions -- that is why the unresolved
    column exists -- and no source we read confirms the employer is still
    accepting candidates. These are worth the candidate's attention next;
    they are not cleared to submit.
    """
    return [s for s in (selections if selections is not None else RUN.selections)
            if s.get('availability_verified', True)]


# Kept so existing callers and tests keep working; the honest name is above.
application_ready = prioritized_for_review


def needs_verification(selections=None):
    return [s for s in (selections if selections is not None else RUN.selections)
            if not s.get('availability_verified', True)]


def export_report(summary=None, partial=False, partial_reason=None):
    """Dated Excel report. Deterministic; the model supplies no numbers."""
    import openpyxl
    from openpyxl.styles import Font, Alignment

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = RUN.started.strftime('%Y-%m-%d_%H%M%S')
    name = 'job_radar_report_%s%s.xlsx' % (stamp, '_PARTIAL' if partial else '')
    path = REPORTS_DIR / name

    wb = openpyxl.Workbook()
    bold = Font(bold=True)
    wrap = Alignment(wrap_text=True, vertical='top')

    ws = wb.active
    ws.title = 'Prioritized for review'
    status = ('PARTIAL -- %s' % (partial_reason or 'run ended before completing')) if partial \
        else 'COMPLETE'
    ws.append(['Job Radar - opportunities prioritized for review'])
    ws['A1'].font = bold
    ws.append(['Generated', RUN.started.strftime('%Y-%m-%d %H:%M:%S')])
    ws.append(['Run status', status])
    counts = RUN.evaluation_counts()
    ws.append(['Prioritized for review', len(application_ready())])
    ws.append(['Held in Needs Verification', len(needs_verification())])
    ws.append(['RUN FACTS BELOW ARE FROM RECORDED METRICS', 'not from model prose'])
    ws.append(['Listings scanned (title rows returned)', counts['listings_scanned']])
    ws.append(['Postings with requirement text retrieved', counts['postings_retrieved']])
    ws.append(['  of those, excerpt only (filtered, capped)', counts['excerpt_only']])
    ws.append(['  of those, full text retrieved', counts['full_text_retrieved']])
    ws.append(['Judged and recorded (selected)', counts['judged_and_recorded']])
    ws.append(['NOTE', 'Retrieving a filtered requirement excerpt is NOT a complete assessment. '
                       'Only the "judged and recorded" figure reflects a decision the model '
                       'actually made and defended with evidence.'])
    ws.append(['Evaluation basis',
               'Live retrieval from official employer APIs during this run. Being listed is NOT '
               'confirmation that a vacancy accepts new applicants, and no source read here '
               'asserts that. Every row below still has unresolved fit or eligibility questions '
               '-- these are prioritized for YOUR review, not cleared to submit.'])
    if summary:
        ws.append(['Model narrative', summary])
        bad = contradicted_counts(summary, counts)
        if bad:
            ws.append(['CORRECTION -- model narrative contradicts recorded metrics',
                       '; '.join(bad)])
            for c in ws[ws.max_row]:
                c.font = bold
    ws.append([])
    head = ['Priority', 'Company', 'Title', 'Req ID', 'Location', 'Why suitable (model)',
            'Evidence (verbatim from posting)', 'Unresolved questions', 'Next action',
            'Posted', 'Deadline field', 'Listed at retrieval',
            'Acceptance confirmed by source?', 'Source availability evidence',
            'Verified at (run clock)', 'Official application URL']
    ws.append(head)
    for c in ws[ws.max_row]:
        c.font = bold
    for s in sorted(application_ready(), key=lambda x: str(x.get('priority'))):
        ws.append([s.get('priority'), s.get('company'), s.get('title'), s.get('job_id'),
                   s.get('location'), s.get('why_suitable'), s.get('evidence_quote'),
                   '; '.join(s.get('unresolved_questions') or []) or 'none stated',
                   s.get('next_action'), str(s.get('posted') or ''),
                   str(s.get('deadline_field') or ''),
                   str((s.get('availability_evidence') or {}).get('listed_at_retrieval')),
                   'NO -- not asserted by any source read',
                   (s.get('availability_evidence') or {}).get('evidence_note') or '',
                   s.get('verified_at') or '', s.get('url')])
    for col, width in zip('ABCDEFGHIJKLMNOP', (9, 14, 34, 11, 20, 52, 58, 40, 34, 16, 14, 12, 16, 54, 20, 56)):
        ws.column_dimensions[col].width = width
    for row in ws.iter_rows(min_row=8):
        for c in row:
            c.alignment = wrap

    nv = wb.create_sheet('Needs Verification')
    nv.append(['Assessed, but NOT application-ready'])
    nv['A1'].font = bold
    nv.append(['These requisitions were evaluated and may well be real opportunities. What is '
               'NOT established is whether they currently accept applicants, so no browser tab '
               'was opened for them. Resolving one needs new official evidence.'])
    nv.append([])
    nv.append(['Company', 'Title', 'Req ID', 'Why availability is unverified',
               'Unresolved questions', 'How to resolve', 'Official URL'])
    for c in nv[nv.max_row]:
        c.font = bold
    # Two populations, deliberately. (a) selections the model made that were
    # held back, and (b) requisitions with a KNOWN availability conflict that
    # this run encountered as candidates but did not select. Without (b) the
    # record would vanish from the report on any run where the model happened
    # not to pick it -- and "keep its record in Needs Verification" has to
    # survive the model's choices, not depend on them.
    listed = set()
    for s_ in needs_verification():
        conflict = availability_conflict(s_['key']) or {}
        listed.add(s_['key'])
        nv.append([s_.get('company'), s_.get('title'), s_.get('job_id'),
                   s_.get('availability_note') or conflict.get('why_unresolved'),
                   '; '.join(s_.get('unresolved_questions') or []) or 'none stated',
                   conflict.get('how_to_resolve') or '', s_.get('url')])
    for key, row in RUN.candidates.items():
        if key in listed:
            continue
        conflict = availability_conflict(key)
        if not conflict:
            continue
        nv.append([conflict.get('company') or row.get('company'),
                   conflict.get('title') or row.get('title'),
                   conflict.get('job_id') or row.get('job_id'),
                   conflict.get('why_unresolved'),
                   'not selected this run; held on availability grounds',
                   conflict.get('how_to_resolve') or '', row.get('url')])
    for col, width in zip('ABCDEFG', (14, 34, 11, 70, 46, 54, 56)):
        nv.column_dimensions[col].width = width
    for row in nv.iter_rows(min_row=4):
        for c in row:
            c.alignment = wrap

    cs = wb.create_sheet('Search coverage')
    cs.append(['What this run ACTUALLY searched'])
    cs['A1'].font = bold
    cs.append(['Searches run', RUN.searches])
    cs.append(['Listings scanned (titles seen)', RUN.scanned_count()])
    cs.append(['Postings fully evaluated (requirement text retrieved)', len(RUN.details)])
    cs.append(['NOTE', 'A scanned listing is a title/location row returned by an employer API. '
                       'A fully evaluated posting is one whose requirement text was actually '
                       'retrieved and read. They are different numbers and are reported separately.'])
    cs.append([])
    cs.append(['Source', 'Company', 'ATS', 'Reached', 'Error', 'Searches',
               'Listings scanned', 'Total reported by source',
               'Kept after keyword/location filter', 'Fully evaluated'])
    for c in cs[cs.max_row]:
        c.font = bold
    for c in RUN.coverage:
        evaluated = sum(1 for k in RUN.details if k.startswith(c['source'] + ':'))
        cs.append([c['source'], c['company'], c['ats'], 'yes' if c['reached'] else 'NO',
                   c.get('error') or '', c.get('searches', 1), c['returned_by_source'],
                   c.get('total_reported_by_source'), c['kept_after_filter'], evaluated])
    cs.append([])
    _, raw = discovery.load_sources()
    cs.append(['Documented coverage limits'])
    cs[cs.max_row][0].font = bold
    for lim in raw.get('known_coverage_limits', []):
        cs.append([lim])
    for col, width in zip('ABCDEFGHIJ', (14, 16, 12, 9, 26, 10, 16, 20, 30, 14)):
        cs.column_dimensions[col].width = width

    wb.save(path)
    RUN.report_path = str(path)
    return {'report_path': str(path),
            'prioritized_for_review': len(prioritized_for_review()),
            'application_ready': len(prioritized_for_review()),
            'needs_verification': len(needs_verification()),
            'listings_scanned': RUN.scanned_count(),
            'postings_retrieved': len(RUN.details),
            'scanned': RUN.scanned_count(),          # legacy key, kept for callers

            'selections': len(RUN.selections),
            'status': status, 'partial': bool(partial),
            'sources_reached': sum(1 for c in RUN.coverage if c['reached']),
            'sources_total': len(RUN.coverage),
            'verify': 'File written and closed. Re-read it to confirm contents.'}


def open_ledger_path(day=None):
    """Where today's already-requested URLs are remembered, so reruns on the
    same day do not reopen what is already sitting in the browser."""
    return REPORTS_DIR / ('opened_urls_%s.json' % (day or _today()))


def _load_open_ledger(day=None):
    p = open_ledger_path(day)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return {}                       # a corrupt ledger must not block the run


def _save_open_ledger(data, day=None):
    p = open_ledger_path(day)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')


def open_application_urls(keys=None, force=False):
    """Request the official application pages for the FINAL shortlist.

    Four properties this enforces, each of which was absent and each of which
    a rerun or a retry would otherwise violate:

      * It runs only AFTER export_report wrote a workbook that exists on disk.
        Opening tabs for a shortlist that was never exported would put pages
        in front of the user that no artefact records.
      * It opens at most MAX_SELECTIONS unique URLs, and only URLs belonging
        to the exported shortlist -- never a candidate, never a retrieval.
      * It is idempotent within the run and across the day. Calling it twice,
        or retrying finalisation, re-requests nothing. A same-day rerun skips
        what is already open unless force=True.
      * It reports what was REQUESTED, not what loaded. webbrowser returning
        True means the OS accepted a handoff; it is not evidence the employer's
        page rendered, or that the requisition is still live.
    """
    if not RUN.report_path or not Path(RUN.report_path).exists():
        return {'error': 'refusing to open anything: the shortlist has not been exported yet. '
                         'Call export_report first and let it succeed -- pages must never be '
                         'opened for a shortlist that no workbook records.'}
    if not RUN.selections:
        return {'error': 'nothing selected to open'}

    ready = application_ready()
    blocked = needs_verification()
    sel = ready if not keys else [s for s in ready if s['key'] in keys]
    if not sel:
        return {'error': 'nothing application-ready to open%s' % (
            ' -- %d selection(s) are held in Needs Verification: %s' % (
                len(blocked), [b['key'] for b in blocked]) if blocked else '')}

    ledger = _load_open_ledger()
    requested, failed = [], []
    skipped_run, skipped_today, skipped_cap = [], [], []
    seen_urls = set()

    # Decide the whole target set BEFORE touching a browser, and budget the
    # cap against what this RUN has already requested -- not against this
    # call. Capping inside the attempt loop had two measured defects: a
    # finalisation retry opened the selection that the first call had capped
    # out (so a "retry" still produced a new tab, breaking the per-execution
    # limit of 4), and a browser error never consumed budget, so a failing
    # run walked the entire shortlist instead of stopping at 4.
    budget = MAX_SELECTIONS - len(RUN.opened_set)
    targets = []
    for s in sel:
        url, key = s.get('url'), s['key']
        if not url:
            failed.append({'key': key, 'error': 'no url on this selection'})
            continue
        if url in seen_urls or url in RUN.opened_set:
            skipped_run.append({'key': key, 'url': url})
            continue
        if not force and url in ledger:
            skipped_today.append({'key': key, 'url': url,
                                  'first_requested': ledger[url].get('at')})
            continue
        seen_urls.add(url)
        if len(targets) >= max(0, budget):
            skipped_cap.append({'key': key, 'url': url})
            continue
        targets.append(s)

    for s in targets:
        url, key = s['url'], s['key']
        # An attempt consumes budget whether or not it succeeds, so a failing
        # browser cannot walk past the cap.
        RUN.opened_set.add(url)
        try:
            accepted = webbrowser.open_new_tab(url)
        except Exception as e:
            failed.append({'key': key, 'error': str(e)[:80]})
            continue
        entry = {'key': key, 'company': s['company'], 'url': url,
                 'os_handoff_accepted': bool(accepted)}
        requested.append(entry)
        ledger[url] = {'key': key, 'at': datetime.datetime.now().isoformat(timespec='seconds'),
                       'report': RUN.report_path}

    _save_open_ledger(ledger)
    RUN.opened_urls = (RUN.opened_urls or []) + requested
    return {
        'requested': requested,
        'requested_count': len(requested),
        'skipped_already_requested_this_run': skipped_run,
        'skipped_already_requested_today': skipped_today,
        'skipped_over_cap': skipped_cap,
        'failed': failed,
        'shortlist_size': len(RUN.selections),
        'cap': MAX_SELECTIONS,
        'note': ('These URLs were handed to the operating system to open. os_handoff_accepted '
                 'only means the OS accepted the request -- it is NOT evidence that the page '
                 'loaded, that the requisition is still open, or that it accepts applicants. '
                 'No form was filled and nothing was submitted. Re-requesting is suppressed for '
                 'the rest of today; pass force=true only if the user explicitly asks.'),
    }


def verify_export_matches_open_set(report_path=None):
    """Deterministic cross-check: do the workbook's rows and the URLs we
    requested agree EXACTLY?

    This exists because "the tool said ok" and "a file exists" are both weaker
    than reading the artefact back. A mismatch here means the user would be
    looking at a tab that the report does not justify, or vice versa.
    """
    import openpyxl
    path = Path(report_path or RUN.report_path or '')
    if not path.exists():
        return {'ok': False, 'error': 'no workbook at %s' % path}
    wb = openpyxl.load_workbook(path)
    # Sheet was renamed from 'Selected' to 'Prioritized for review' -- accept
    # either so a report generated before the rename still verifies.
    sheet = next((n for n in ('Prioritized for review', 'Selected') if n in wb.sheetnames), None)
    if sheet is None:
        return {'ok': False, 'error': 'no shortlist sheet in %s' % path}
    ws = wb[sheet]
    rows = list(ws.iter_rows(values_only=True))
    header_idx = next((i for i, r in enumerate(rows) if r and r[0] == 'Priority'), None)
    if header_idx is None:
        return {'ok': False, 'error': 'no header row found in the Selected sheet'}
    cols = {name: i for i, name in enumerate(rows[header_idx]) if name}
    url_col = cols.get('Official application URL')
    req_col = cols.get('Req ID')
    data = [r for r in rows[header_idx + 1:] if r and r[url_col]]

    sheet_urls = [r[url_col] for r in data]
    sheet_reqs = [str(r[req_col]) for r in data]
    requested_urls = [e['url'] for e in (RUN.opened_urls or [])]

    problems = []
    if len(sheet_urls) > MAX_SELECTIONS:
        problems.append('workbook lists %d rows, more than the cap of %d'
                        % (len(sheet_urls), MAX_SELECTIONS))
    if len(set(sheet_urls)) != len(sheet_urls):
        problems.append('workbook contains duplicate URLs')
    if len(set(sheet_reqs)) != len(sheet_reqs):
        problems.append('workbook contains duplicate requisition ids')
    extra = [u for u in requested_urls if u not in sheet_urls]
    if extra:
        problems.append('opened URL(s) not present in the workbook: %s' % extra)

    # "Agreement" has to account for same-day idempotency. On a rerun the
    # browser already holds the tabs, so this run legitimately requests zero
    # and comparing the sheet against THIS run's requests alone reported a
    # false mismatch. The real question is whether every shortlist row is open
    # in the browser -- requested now, or already requested earlier today --
    # and whether anything was opened that the workbook does not justify.
    ledger = _load_open_ledger()
    accounted = set(requested_urls) | set(ledger)
    missing = [u for u in sheet_urls if u not in accounted]
    if missing:
        problems.append('shortlist URL(s) never requested, today or now: %s' % missing)

    return {
        'ok': not problems,
        'problems': problems,
        'workbook_rows': len(sheet_urls),
        'workbook_requisitions': sheet_reqs,
        'urls_requested_this_run': requested_urls,
        'urls_already_open_from_earlier_today': [u for u in sheet_urls
                                                 if u in ledger and u not in requested_urls],
        'urls_in_workbook_not_accounted_for': missing,
        'exact_agreement': not problems and sorted(sheet_urls) == sorted(set(u for u in accounted
                                                                             if u in sheet_urls)),
    }


# --------------------------------------------------------------------------
# schemas
# --------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {'name': 'list_sources',
     'description': 'List the official employer job APIs this run can search, plus documented '
                    'coverage limits. Call this first if you need to know what coverage means.',
     'input_schema': {'type': 'object', 'properties': {}}},
    {'name': 'search_openings',
     'description': 'Search live openings across official employer APIs. Returns compact rows '
                    '(title, location, key) -- NOT requirement text. Keyword and location '
                    'filters are mechanical string matches used to keep the result small; they '
                    'make no suitability judgment.',
     'input_schema': {'type': 'object', 'properties': {
         'source_keys': {'type': 'array', 'items': {'type': 'string'},
                         'description': 'Source keys from list_sources. Omit to search all.'},
         'keywords': {'type': 'array', 'items': {'type': 'string'},
                      'description': 'Match any of these in the job title.'},
         'locations': {'type': 'array', 'items': {'type': 'string'},
                       'description': 'Match any of these in the location string.'},
         'limit_per_source': {'type': 'integer',
                              'description': 'Max postings to pull per source (default 60).'},
         'search_text': {'type': 'string',
                         'description': 'ONE short term sent to the employer\'s own search (e.g. '
                                        '"analyst"). Workday narrows server-side on this, and a '
                                        'single term works far better than several: measured, one '
                                        'term kept 32 candidates where an empty term kept 7 and '
                                        'nine joined keywords kept almost none. Defaults to your '
                                        'first keyword. Your full keyword list is still applied '
                                        'locally as an OR filter.'}}}},
    {'name': 'get_posting_details',
     'description': 'Batch-fetch requirement text, sponsorship language and posting dates for '
                    'up to 8 candidates in ONE call. Pass several keys at once. Observations '
                    'are excerpted and say what was omitted; set full_text=true for one '
                    'posting when you need everything.',
     'input_schema': {'type': 'object', 'properties': {
         'keys': {'type': 'array', 'items': {'type': 'string'},
                  'description': 'Candidate keys from search_openings, e.g. "gartner:112849".'},
         'full_text': {'type': 'boolean',
                       'description': 'Return the entire posting instead of an excerpt.'}},
         'required': ['keys']}},
    {'name': 'select_opportunity',
     'description': 'Record one opportunity you judge suitable. Requires a verbatim quote from '
                    'the retrieved posting as evidence; the quote is checked against the real '
                    'text and the requisition is checked against the application ledger. '
                    'Maximum 4 selections.',
     'input_schema': {'type': 'object', 'properties': {
         'key': {'type': 'string'},
         'priority': {'type': 'integer', 'description': '1 = apply first.'},
         'why_suitable': {'type': 'string',
                          'description': 'Your reasoning, referencing the user\'s verified facts.'},
         'evidence_quote': {'type': 'string',
                            'description': 'Verbatim span from the posting supporting the match.'},
         'unresolved_questions': {'type': 'array', 'items': {'type': 'string'},
                                  'description': 'What is still genuinely unknown.'},
         'next_action': {'type': 'string'}},
         'required': ['key', 'priority', 'why_suitable', 'evidence_quote']}},
    {'name': 'export_report',
     'description': 'Write the dated Excel report with selections, evidence, unresolved '
                    'questions, official URLs and actual search coverage. The coverage sheet is '
                    'filled in automatically from what was actually reached -- you do not supply '
                    'those numbers.',
     'input_schema': {'type': 'object', 'properties': {
         'summary': {'type': 'string', 'description': 'Short overall summary for the report.'},
         'partial': {'type': 'boolean',
                     'description': 'TRUE ONLY IF THIS RUN WAS CUT SHORT -- you ran out of steps '
                                    'or time, or a source failed, so work you intended to do did '
                                    'not happen. It marks the filename _PARTIAL and stamps the '
                                    'file INCOMPLETE. It is NOT the flag for "coverage is narrow" '
                                    'or "questions remain": this tool only ever searches five '
                                    'employers and open questions are normal, so neither of those '
                                    'makes a run partial. Put those in summary and in each '
                                    'selection\'s unresolved_questions instead. If you finished '
                                    'what you set out to do, pass false.'},
         'partial_reason': {'type': 'string',
                            'description': 'Required when partial is true: what stopped the run.'}}}},
    {'name': 'open_application_urls',
     'description': 'FINAL STEP. Request the official application pages for the exported '
                    'shortlist. Call it ONCE, and only after export_report has succeeded. It '
                    'opens at most 4 unique URLs, only ones on the shortlist, and is idempotent: '
                    'calling it again re-requests nothing, and URLs already requested earlier '
                    'today are skipped. It cannot fill or submit anything, and an accepted OS '
                    'handoff is not proof a page loaded.',
     'input_schema': {'type': 'object', 'properties': {
         'keys': {'type': 'array', 'items': {'type': 'string'},
                  'description': 'Omit to request every shortlisted selection.'},
         'force': {'type': 'boolean',
                   'description': 'Re-request URLs already opened earlier today. Only when the '
                                  'user explicitly asks for that.'}}}},
]

TOOL_REGISTRY = {
    'list_sources': list_sources,
    'search_openings': search_openings,
    'get_posting_details': get_posting_details,
    'select_opportunity': select_opportunity,
    'export_report': export_report,
    'open_application_urls': open_application_urls,
}
