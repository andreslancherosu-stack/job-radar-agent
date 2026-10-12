"""Programmatic discovery against OFFICIAL employer job APIs.

This is the deterministic retrieval layer. It finds and fetches postings; it
never decides whether one suits the user. Every function here returns facts
(titles, ids, urls, requirement text, posting dates) or an explicit failure.
Suitability, prioritisation and "is this gap disqualifying?" belong to the
model, not to this file.

Two protocols are supported, both employer-controlled and public:

  greenhouse  GET  boards-api.greenhouse.io/v1/boards/{token}/jobs[?content=true]
  workday     POST {tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
              GET  .../wday/cxs/{tenant}/{site}{externalPath}   (one posting)

Design constraints that are load-bearing, not stylistic:

  * Every network call has a bounded timeout and there are NO retry loops.
    A slow source degrades the run into a reported partial, never a hang.
  * Searches run concurrently, so wall-clock is the slowest single source
    rather than the sum of all of them.
  * Observations are COMPACT by construction. A Workday description is
    ~12,000 characters; handing several of those to the model per step would
    dominate the context window and the bill. extract_requirements() returns
    the decision-relevant lines plus an explicit note about what it dropped
    and how to retrieve it.
"""
import concurrent.futures
import html
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from import_posting import html_to_text

SOURCES_PATH = Path('sources.json')

DEFAULT_TIMEOUT = 10
DEFAULT_EXCERPT_CHARS = 800   # measured: 1400 drove a 9-step run to 168k input tokens

_UA = 'Mozilla/5.0 (JobRadar/1.0; personal job-search agent)'


def load_sources(path=SOURCES_PATH):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    return {s['key']: s for s in data['sources']}, data


def _get_json(url, timeout, payload=None):
    headers = {'Accept': 'application/json', 'User-Agent': _UA}
    data = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8', 'replace'))


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------

def _search_greenhouse(src, timeout):
    url = 'https://boards-api.greenhouse.io/v1/boards/%s/jobs' % src['board_token']
    payload = _get_json(url, timeout)
    rows = []
    for j in payload.get('jobs', []):
        rows.append({
            'source': src['key'],
            'company': src['company'],
            'title': j.get('title'),
            'job_id': str(j.get('id')),
            'location': (j.get('location') or {}).get('name'),
            'url': j.get('absolute_url'),
            'updated': j.get('updated_at'),
            'deadline': j.get('application_deadline'),
        })
    return rows, len(payload.get('jobs', []))


WORKDAY_PAGE = 20          # measured: limit>20 returns HTTP 400 Bad Request
WORKDAY_MAX_PAGES = 6      # bounded -- discovery must not walk an entire careers site


def _search_workday(src, timeout, search_text, limit):
    base = 'https://%s.%s.myworkdayjobs.com/wday/cxs/%s/%s' % (
        src['tenant'], src['host'], src['tenant'], src['site'])

    def page(offset):
        return _get_json(base + '/jobs', timeout,
                         {'appliedFacets': {}, 'limit': WORKDAY_PAGE, 'offset': offset,
                          'searchText': search_text or ''})

    # Workday caps a page at 20, so reaching `limit` means paging. Without
    # this, a location filter applied to only the first 20 of 248 postings
    # silently returned zero Texas roles for JLL and Blackstone and looked
    # like "no matching jobs" rather than "we only ever looked at page 1".
    first = page(0)
    postings = list(first.get('jobPostings') or [])
    total = first.get('total')
    pages = min(WORKDAY_MAX_PAGES, -(-min(limit, total or limit) // WORKDAY_PAGE))
    if pages > 1 and postings:
        offsets = [p * WORKDAY_PAGE for p in range(1, pages)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(offsets)) as ex:
            for extra in ex.map(lambda o: page(o).get('jobPostings') or [], offsets):
                postings.extend(extra)
    rows = []
    for j in postings[:limit]:
        ext = j.get('externalPath') or ''
        bullets = j.get('bulletFields') or []
        rows.append({
            'source': src['key'],
            'company': src['company'],
            'title': j.get('title'),
            'job_id': bullets[0] if bullets else None,
            'location': j.get('locationsText'),
            'url': 'https://%s.%s.myworkdayjobs.com/%s%s' % (
                src['tenant'], src['host'], src['site'], ext),
            'updated': j.get('postedOn'),
            'deadline': None,
            '_external_path': ext,
        })
    return rows, total


def search_source(src, search_text, timeout=DEFAULT_TIMEOUT, limit=20):
    """One source. Returns (rows, total_reported, error_or_None).

    Never raises: a dead source must degrade the run to a reported partial,
    not abort discovery for every other source.
    """
    try:
        if src['ats'] == 'greenhouse':
            rows, total = _search_greenhouse(src, timeout)
        elif src['ats'] == 'workday':
            rows, total = _search_workday(src, timeout, search_text, limit)
        else:
            return [], None, 'unsupported ats %r' % src['ats']
        return rows, total, None
    except urllib.error.HTTPError as e:
        return [], None, 'HTTP %s' % e.code
    except Exception as e:                                  # timeouts, DNS, bad JSON
        return [], None, '%s: %s' % (type(e).__name__, str(e)[:80])


def matches(row, keywords, locations):
    """Mechanical string filter -- retrieval, NOT a suitability judgment.

    This exists so the model is handed tens of candidates instead of 531. It
    decides nothing about fit: it cannot see the user's profile, and a row it
    drops was dropped for not containing a requested word, never for being a
    weak match. Both filters are OR-within / AND-between: any keyword AND any
    location.
    """
    title = (row.get('title') or '').lower()
    loc = (row.get('location') or '').lower()
    if keywords and not any(k.lower() in title for k in keywords):
        return False
    if locations and not any(l.lower() in loc for l in locations):
        return False
    return True


def search_all(source_keys, keywords=None, locations=None, search_text=None,
               timeout=DEFAULT_TIMEOUT, limit_per_source=20, sources=None):
    """Concurrent search across sources. Returns rows + honest coverage facts."""
    all_sources = sources if sources is not None else load_sources()[0]
    chosen = [all_sources[k] for k in source_keys if k in all_sources]
    unknown = [k for k in source_keys if k not in all_sources]

    coverage, rows = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(chosen))) as ex:
        # NOT ' '.join(keywords). Measured failure (2026-10-11 live run): the
        # model passed nine keywords, they were concatenated into one Workday
        # searchText, and Workday matched almost nothing -- Gartner returned 1
        # of 178 and JLL 0 of 248, so every selection came from the two
        # Greenhouse boards and the run looked like "no Workday jobs fit"
        # rather than "we never really searched Workday". Server-side narrowing
        # now happens only when the caller asks for it explicitly; otherwise we
        # page a bounded window and filter locally, where multi-keyword OR
        # actually works.
        # ONE term, not ' '.join(keywords). Measured on the 2026-10-11 live
        # run: nine keywords concatenated into a single Workday searchText
        # matched almost nothing -- Gartner returned 1 of 178 and JLL 0 of 248,
        # so every selection came from the two Greenhouse boards and the run
        # read as "no Workday jobs fit" rather than "Workday was never really
        # searched". Re-measured afterwards across the same three Workday
        # tenants: one term kept 32 candidates, an empty term kept 7.
        # So: narrow server-side with a single term, then OR the full keyword
        # list client-side where multi-term matching actually works.
        server_term = search_text if search_text is not None else (
            (keywords or [''])[0] if keywords else '')
        futures = {ex.submit(search_source, s, server_term,
                             timeout, limit_per_source): s for s in chosen}
        for fut in concurrent.futures.as_completed(futures):
            src = futures[fut]
            got, total, err = fut.result()
            kept = [r for r in got if matches(r, keywords, locations)]
            rows.extend(kept)
            coverage.append({
                'source': src['key'], 'company': src['company'], 'ats': src['ats'],
                'reached': err is None, 'error': err,
                'returned_by_source': len(got),
                'total_reported_by_source': total,
                'kept_after_filter': len(kept),
            })
    for k in unknown:
        coverage.append({'source': k, 'company': None, 'ats': None, 'reached': False,
                         'error': 'unknown source key (not in sources.json)',
                         'returned_by_source': 0, 'total_reported_by_source': None,
                         'kept_after_filter': 0})
    coverage.sort(key=lambda c: c['source'])
    return rows, coverage


# --------------------------------------------------------------------------
# detail
# --------------------------------------------------------------------------

_SPONSORSHIP = re.compile(
    r'[^.\n]*\b(sponsor|sponsorship|visa|work authorization|authorized to work|'
    r'right to work|immigration)\b[^.\n]*\.?', re.IGNORECASE)

_REQUIREMENT_HINT = re.compile(
    r'\b(bachelor|master|degree|years? of|experience with|required|requirement|'
    r'qualification|must have|minimum|graduat)\w*\b', re.IGNORECASE)

_DEADLINE = re.compile(
    r'[^.\n]*\b(application deadline|apply by|closing date|close[sd]? on)\b[^.\n]*', re.IGNORECASE)


def extract_requirements(text, excerpt_chars=DEFAULT_EXCERPT_CHARS):
    """Decision-relevant lines out of a long posting, with honest omissions.

    Returns the sponsorship sentences in FULL and verbatim -- that is the one
    category where paraphrase or truncation has repeatedly been the difference
    between "needs a question asked" and "explicitly excluded", so it is never
    abbreviated. Requirement lines are capped, and the return value always
    states how much of the source text was not shown and how to get it.
    """
    text = (text or '').strip()
    sponsorship = [m.group(0).strip() for m in _SPONSORSHIP.finditer(text)]
    deadlines = [m.group(0).strip() for m in _DEADLINE.finditer(text)]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    req_lines, seen = [], set()
    for ln in lines:
        if len(ln) > 400 or ln in seen:
            continue
        if _REQUIREMENT_HINT.search(ln):
            seen.add(ln)
            req_lines.append(ln)
    MAX_LINES = 10          # keeps one posting's observation small enough to batch
    kept, used = [], 0
    for ln in req_lines:
        if used + len(ln) > excerpt_chars or len(kept) >= MAX_LINES:
            break
        kept.append(ln)
        used += len(ln)
    return {
        'sponsorship_sentences': sponsorship or None,
        'deadline_sentences': deadlines or None,
        'requirement_lines': kept,
        'requirement_lines_omitted': max(0, len(req_lines) - len(kept)),
        'full_text_chars': len(text),
        'shown_chars': used,
        'omission_note': (
            'Requirement lines are filtered and capped; sponsorship sentences are '
            'complete and verbatim. %d of %d characters of the full posting are NOT '
            'shown. Call get_posting_details again with full_text=true for this one '
            'posting to read everything.' % (max(0, len(text) - used), len(text))),
    }


def fetch_detail(row, timeout=DEFAULT_TIMEOUT, full_text=False, excerpt_chars=DEFAULT_EXCERPT_CHARS,
                 sources=None):
    """One posting's detail. Never raises; returns {'error': ...} instead."""
    all_sources = sources if sources is not None else load_sources()[0]
    src = all_sources.get(row.get('source'))
    if src is None:
        return {'error': 'unknown source %r' % row.get('source')}
    try:
        if src['ats'] == 'greenhouse':
            url = 'https://boards-api.greenhouse.io/v1/boards/%s/jobs/%s' % (
                src['board_token'], row['job_id'])
            d = _get_json(url, timeout)
            text = html_to_text(html.unescape(d.get('content') or ''))
            meta = {'title': d.get('title'), 'location': (d.get('location') or {}).get('name'),
                    'job_id': str(d.get('id')), 'url': d.get('absolute_url'),
                    'deadline_field': d.get('application_deadline'),
                    'updated': d.get('updated_at'), 'still_listed': True}
        else:
            base = 'https://%s.%s.myworkdayjobs.com/wday/cxs/%s/%s' % (
                src['tenant'], src['host'], src['tenant'], src['site'])
            d = _get_json(base + row['_external_path'], timeout)
            info = d.get('jobPostingInfo') or {}
            text = html_to_text(html.unescape(info.get('jobDescription') or ''))
            meta = {'title': info.get('title'), 'location': info.get('location'),
                    'job_id': info.get('jobReqId'), 'url': info.get('externalUrl'),
                    'deadline_field': info.get('endDate'),
                    'updated': info.get('startDate'),
                    'still_listed': bool(info.get('posted')),
                    'can_apply': info.get('canApply'),
                    'time_type': info.get('timeType')}
    except urllib.error.HTTPError as e:
        return {'error': 'HTTP %s' % e.code, 'source': row.get('source'), 'job_id': row.get('job_id')}
    except Exception as e:
        return {'error': '%s: %s' % (type(e).__name__, str(e)[:80]),
                'source': row.get('source'), 'job_id': row.get('job_id')}

    out = {'source': src['key'], 'company': src['company']}
    out.update(meta)
    if full_text:
        out['full_text'] = text
        out['full_text_chars'] = len(text)
    else:
        out.update(extract_requirements(text, excerpt_chars=excerpt_chars))
    return out


def fetch_details(rows, timeout=DEFAULT_TIMEOUT, full_text=False,
                  excerpt_chars=DEFAULT_EXCERPT_CHARS, sources=None):
    """Batched and concurrent on purpose: the model asks for several postings
    in ONE tool call, which costs one API round trip instead of N."""
    out = [None] * len(rows)
    if not rows:
        return out
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(rows))) as ex:
        futs = {ex.submit(fetch_detail, r, timeout, full_text, excerpt_chars, sources): i
                for i, r in enumerate(rows)}
        for fut in concurrent.futures.as_completed(futs):
            out[futs[fut]] = fut.result()
    return out
