"""Deterministic tool functions for the Job Radar agent.

DESIGN RULE (the whole point of this file):
    A tool performs a deterministic operation and returns an OBSERVATION.
    A tool never decides whether a job is a good fit, never interprets a
    sponsorship policy, and never chooses what to do next. Those are
    judgments, and they belong to the model (see agent.py's system prompt).

    Concretely: save_assessment() VALIDATES and STORES the model's verdict.
    It does not compute one. If this file ever starts scoring candidates,
    the agent has degenerated into a wrapper around a rule engine and the
    model is just reading a number back to us.

WHAT PYTHON STILL ENFORCES (not negotiable by the model):
    - only tools in TOOL_DEFINITIONS may run (unknown name -> error observation)
    - arguments are validated before anything executes
    - duplicate applications are detected from the canonical ledger
    - every evidence quote must appear VERBATIM in the posting text
      (delegated to radar.assess, which already did this correctly)
    - there is NO submit tool, anywhere, by construction

Storage is the EXISTING canonical ledger (private/state.json) through
radar.py. This file deliberately adds no competing history.

Python 3.10+.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import radar

PROFILE_PATH = Path('private/profile.json')
STATE_PATH = Path('private/state.json')
POSTINGS_DIR = Path('private/postings_drafts')

# Which profile keys each requested section exposes. The model asks for the
# sections it actually needs instead of receiving the whole ~10KB profile on
# every turn -- context hygiene, and it makes the agent's reasoning legible
# ("it asked for work_authorization before judging sponsorship").
PROFILE_SECTIONS = {
    # first/last/legal name parts were previously missing here, so the model saw
    # only "name" and "full_name" and could not tell whether the surname was
    # "Lancheros" or "Lancheros Ulloa" on a legal-name field. The profile had
    # already resolved this; the section mapping was simply hiding it.
    'identity': ['name', 'full_name', 'first_name', 'last_name', 'legal_name_parts',
                 'name_field_guidance', 'location', 'email', 'phone', 'linkedin_url', 'positioning'],
    'education': ['education', 'resume_education_entries', 'expected_graduation',
                  'expected_graduation_status', 'education_historical'],
    'employment': ['resume_employment_entries', 'technical_experience', 'background'],
    'skills': ['resume_skills', 'technical_experience', 'languages_spoken'],
    'work_authorization': ['current_work_authorization_details', 'current_work_authorization_status',
                           'future_sponsorship_required', 'future_sponsorship_required_status'],
    'preferences': ['preferences', 'availability', 'job_search_status', 'salary_expectation_usd',
                    'salary_expectation_status'],
    'guardrails': ['drafting_guardrails', 'education_historical', 'note'],
}

# Keys whose names end in _status (or are listed here) carry PROVENANCE:
# whether a fact is user-confirmed or merely parsed from the resume. The
# model must see these, because "resume says X" is not "the user confirmed X".
PROVENANCE_SUFFIX = '_status'


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _load_profile():
    return json.loads(PROFILE_PATH.read_text(encoding='utf-8'))


def _load_state():
    if STATE_PATH.exists():
        return radar.read(STATE_PATH)
    return radar.empty()


# --------------------------------------------------------------------------
# Tool implementations. Each returns a plain dict = one observation.
# --------------------------------------------------------------------------

def get_profile_facts(sections):
    """Return only the requested profile sections, with provenance attached."""
    if not isinstance(sections, list) or not sections:
        raise ValueError('sections must be a non-empty list')
    unknown = [s for s in sections if s not in PROFILE_SECTIONS]
    if unknown:
        raise ValueError('unknown section(s): %s; valid: %s'
                         % (', '.join(unknown), ', '.join(sorted(PROFILE_SECTIONS))))
    profile = _load_profile()
    out, provenance = {}, {}
    for section in sections:
        for field in PROFILE_SECTIONS[section]:
            if field in profile:
                out[field] = profile[field]
                if field.endswith(PROVENANCE_SUFFIX):
                    provenance[field] = 'CONFIRMATION STATUS -- read this before using the paired fact'
    return {
        'sections_returned': sections,
        'facts': out,
        'provenance_notes': provenance,
        'unset_fields_meaning': 'A field that is null/absent is UNKNOWN. Do not guess it; '
                                'use request_missing_fact if the decision depends on it.',
    }


def get_posting(source):
    """Retrieve a saved posting: its text, source URL, and retrieval time.

    `source` is either a posting-draft filename under private/postings_drafts/
    or a 16-char job key already present in the canonical ledger.

    Freshness is reported, never asserted: saved text is evidence that a
    posting EXISTED when retrieved, not that the vacancy is still open.
    """
    if not isinstance(source, str) or not source.strip():
        raise ValueError('source must be a non-empty string')
    source = source.strip()

    state = _load_state()
    if source in state.get('jobs', {}):
        entry = state['jobs'][source]
        posting = entry['posting']
        retrieved = entry.get('date_found')
    else:
        path = POSTINGS_DIR / source if not source.endswith('.json') else POSTINGS_DIR / source
        if not path.exists():
            path = POSTINGS_DIR / (source + '.json')
        if not path.exists():
            available = sorted(p.name for p in POSTINGS_DIR.glob('*.json'))
            raise ValueError('no saved posting %r; available drafts: %s' % (source, ', '.join(available)))
        posting = json.loads(path.read_text(encoding='utf-8'))
        retrieved = posting.get('verified_on') or (posting.get('_fetch_metadata') or {}).get('retrieved_on')

    text = posting.get('text') or ''
    return {
        'company': posting.get('company'),
        'role': posting.get('role'),
        'job_id': posting.get('job_id'),
        'source_url': posting.get('source_url'),
        'source_kind': posting.get('source_kind'),
        'retrieved_on': retrieved,
        'text': text,
        'text_length': len(text),
        'extraction_complete': len(text) >= 200,
        'freshness_warning': ('Saved posting text is NOT proof the vacancy is still open. '
                              'It was retrieved on %s.' % retrieved),
        'empty_text_warning': (None if len(text) >= 200 else
                               'Posting text is empty or too short to assess (likely a JavaScript-rendered '
                               'page that a plain HTTP fetch could not read). Report this limitation rather '
                               'than assessing an incomplete posting.'),
    }


def check_application_history(company, job_id=None, source_url=None):
    """Look up whether this requisition was already applied to. Duplicate guard."""
    if not isinstance(company, str) or not company.strip():
        raise ValueError('company must be a non-empty string')
    if not job_id and not source_url:
        raise ValueError('provide job_id or source_url; identity must not be guessed')

    state = _load_state()
    try:
        k = radar.key({'company': company, 'job_id': job_id, 'source_url': source_url})
    except ValueError as e:
        raise ValueError(str(e))

    applied = k in state.get('applications', {})
    record = state.get('applications', {}).get(k)
    known = state.get('jobs', {}).get(k)
    same_company = sorted({j['posting'].get('role') for j in state.get('jobs', {}).values()
                           if (j['posting'].get('company') or '').casefold() == company.casefold()})
    return {
        'job_key': k,
        'already_applied': applied,
        'application_record': record,
        'posting_known_in_ledger': known is not None,
        'other_known_roles_at_this_company': same_company,
        'rule': ('If already_applied is true, do NOT prepare another application for this same '
                 'requisition. A different requisition at the same employer is a separate job.'),
    }


def request_missing_fact(question, why_needed):
    """Signal that a REQUIRED fact is unknown and only the human can supply it.

    This deliberately ends the run. Inventing a graduation day, a reference,
    or a proficiency rating is the exact failure mode this tool prevents.
    """
    if not isinstance(question, str) or not question.strip():
        raise ValueError('question must be a non-empty string')
    if not isinstance(why_needed, str) or not why_needed.strip():
        raise ValueError('why_needed must be a non-empty string')
    return {
        'human_input_required': True,
        'question': question.strip(),
        'why_needed': why_needed.strip(),
        'asked_at': _now_iso(),
    }


def save_assessment(source, fit, next_action, experience, education, work_authorization, career_fit):
    """Validate and persist the MODEL'S assessment. Computes no judgment itself.

    Validation is delegated to radar.assess(), which rejects any dimension
    whose `quote` is not a verbatim substring of the posting text. That is
    the anti-fabrication gate: the model cannot invent supporting evidence.
    """
    day = radar.today()
    state = _load_state()

    if source in state.get('jobs', {}):
        k = source
    else:
        posting_obs = get_posting(source)
        posting = {
            'company': posting_obs['company'], 'role': posting_obs['role'],
            'job_id': posting_obs['job_id'], 'source_url': posting_obs['source_url'],
            'source_kind': posting_obs['source_kind'], 'text': posting_obs['text'],
        }
        posting = {kk: vv for kk, vv in posting.items() if vv is not None}
        # Resolve to the EXISTING ledger entry when this requisition is already
        # known. Re-ingesting would hit radar.ingest's snapshot-conflict guard
        # (a saved draft and the stored snapshot are rarely byte-identical) and,
        # worse, a second entry for one requisition would fork the history this
        # project depends on for duplicate prevention.
        k = radar.key(posting)
        if k not in state.get('jobs', {}):
            k = radar.ingest(state, posting, day)['key']

    assessment = {
        'fit': fit, 'next_action': next_action, 'experience': experience,
        'education': education, 'work_authorization': work_authorization, 'career_fit': career_fit,
    }
    outcome = radar.assess(state, k, assessment, day)   # raises if evidence is not verbatim
    radar.write(STATE_PATH, state)
    return {
        'saved': True, 'job_key': k, 'decision': outcome['decision'],
        'stored_in': str(STATE_PATH),
        'note': 'Assessment recorded. This is NOT an application; nothing was submitted.',
    }


# --------------------------------------------------------------------------
# Tool definitions sent to the API, and the dispatch registry.
# --------------------------------------------------------------------------

_DIMENSION_SCHEMA = {
    'type': 'object',
    'properties': {
        'status': {'type': 'string', 'enum': ['pass', 'uncertain', 'fail']},
        'quote': {'type': 'string',
                  'description': 'EXACT substring copied character-for-character from the posting text. '
                                 'Empty string ONLY when status is uncertain. Never paraphrase.'},
        'reason': {'type': 'string'},
    },
    'required': ['status', 'quote', 'reason'],
}

TOOL_DEFINITIONS = [
    {
        'name': 'get_profile_facts',
        'description': ('Read verified facts from the candidate profile, by section. Returns the facts plus '
                        'their confirmation status (provenance). Request only the sections you need. '
                        'A null/absent field means UNKNOWN -- never guess it.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'sections': {
                    'type': 'array',
                    'items': {'type': 'string', 'enum': sorted(PROFILE_SECTIONS)},
                    'description': 'One or more of: ' + ', '.join(sorted(PROFILE_SECTIONS)),
                },
            },
            'required': ['sections'],
        },
    },
    {
        'name': 'get_posting',
        'description': ('Retrieve a saved job posting: full text, source URL, and when it was retrieved. '
                        'Saved text proves the posting existed at retrieval time, NOT that it is still open.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'source': {'type': 'string',
                           'description': 'A posting-draft filename under private/postings_drafts/, '
                                          'or a 16-character job key from the ledger.'},
            },
            'required': ['source'],
        },
    },
    {
        'name': 'check_application_history',
        'description': ('Check the canonical application ledger for this requisition. Use this BEFORE '
                        'recommending an application, to avoid applying twice to the same requisition.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'company': {'type': 'string'},
                'job_id': {'type': 'string', 'description': 'Requisition id, if known.'},
                'source_url': {'type': 'string', 'description': 'Official posting URL, if job_id is unknown.'},
            },
            'required': ['company'],
        },
    },
    {
        'name': 'request_missing_fact',
        'description': ('Ask the human for a REQUIRED fact that the profile does not contain and that you '
                        'must not invent (e.g. exact graduation day, references, a proficiency rating, '
                        'availability date). Calling this ENDS the run and returns the question to the human.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'question': {'type': 'string', 'description': 'The exact question, in the human\'s words.'},
                'why_needed': {'type': 'string', 'description': 'Which decision is blocked without it.'},
            },
            'required': ['question', 'why_needed'],
        },
    },
    {
        'name': 'save_assessment',
        'description': ('Persist YOUR assessment of this posting to the canonical ledger. Every dimension '
                        'quote must be copied verbatim from the posting text or the save is rejected. '
                        'This records an assessment only -- it applies to nothing and submits nothing.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'source': {'type': 'string', 'description': 'Same source identifier passed to get_posting.'},
                'fit': {'type': 'string', 'enum': ['GOOD FIT', 'STRETCH BUT REASONABLE', 'POOR FIT']},
                'next_action': {'type': 'string', 'description': 'One short recommended next step.'},
                'experience': _DIMENSION_SCHEMA,
                'education': _DIMENSION_SCHEMA,
                'work_authorization': _DIMENSION_SCHEMA,
                'career_fit': _DIMENSION_SCHEMA,
            },
            'required': ['source', 'fit', 'next_action', 'experience', 'education',
                         'work_authorization', 'career_fit'],
        },
    },
]

# name -> callable. agent.py will refuse any name not in this registry.
TOOL_REGISTRY = {
    'get_profile_facts': get_profile_facts,
    'get_posting': get_posting,
    'check_application_history': check_application_history,
    'request_missing_fact': request_missing_fact,
    'save_assessment': save_assessment,
}

# Calling this tool means a human must act; the loop stops cleanly.
HUMAN_INPUT_TOOLS = {'request_missing_fact'}

assert set(TOOL_REGISTRY) == {d['name'] for d in TOOL_DEFINITIONS}, \
    'TOOL_REGISTRY and TOOL_DEFINITIONS must describe exactly the same tools'


def execute_tool(name, arguments, registry=None):
    """Validate and run one tool call. Returns (observation_dict, is_error).

    Every failure is returned to the model as a readable observation instead
    of crashing the run -- a wrong argument should be recoverable by the model
    on its next turn, which is how a real agent loop behaves.

    `registry` lets agent.py run an extended toolset (e.g. the browser tools)
    through this same validation path, so no tool anywhere bypasses it.
    """
    registry = TOOL_REGISTRY if registry is None else registry
    started = time.monotonic()
    if name not in registry:
        return ({'error': 'unknown tool %r. Allowed tools: %s'
                          % (name, ', '.join(sorted(registry)))}, True)
    if not isinstance(arguments, dict):
        return ({'error': 'tool arguments must be a JSON object'}, True)
    try:
        observation = registry[name](**arguments)
    except TypeError as e:
        return ({'error': 'invalid arguments for %s: %s' % (name, e)}, True)
    except ValueError as e:
        return ({'error': str(e)}, True)
    except Exception as e:                                  # noqa: BLE001 - surfaced to the model
        return ({'error': '%s failed: %s: %s' % (name, type(e).__name__, e)}, True)
    observation['_elapsed_seconds'] = round(time.monotonic() - started, 3)
    return (observation, False)
