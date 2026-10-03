"""Milestone B tools: let the SAME agent loop drive the existing browser worker.

Kept separate from tools.py on purpose. tools.py is the small graded core you
have to explain in five minutes; this file is the product growing around it.
agent.py composes them (see --toolset).

EVERY tool here is a thin, validated wrapper over apply.py's existing worker
command protocol. No new browser engine, no second session, no duplicated
automation logic.

HARD SAFETY PROPERTIES (enforced in Python, not by asking the model nicely):
  - There is no submit tool. Advancing is limited to NAVIGATION_ALLOWLIST
    labels, and apply.py independently re-checks the ACTUAL resolved button's
    text against its submit denylist before clicking, so a harmless-looking
    label cannot smuggle a click onto a real submit button.
  - fill/upload/navigate refuse to run unless a worker is actually alive, so a
    timeout can never be silently read as success.
  - Reads never write. inspect/read are strictly read-only commands.
  - During a human handoff the agent does not act: ACTIVE-SESSION tools return
    a refusal observation if the run is paused for review.

Python 3.10+.
"""
import json
from pathlib import Path

import apply

# Intermediate navigation only. "Submit", "Apply now", "Confirm" are absent by
# design and would additionally be rejected by apply.py's denylist.
NAVIGATION_ALLOWLIST = {'Save and continue', 'Save & continue', 'Continue', 'Next', 'Save'}

# Statuses that mean a human currently owns the page; the agent must not act.
HUMAN_OWNED_STATUSES = {'paused_for_review', 'awaiting_review'}

DEFAULT_TIMEOUT = 120


def _state_path(run_key):
    return apply.RUN_STATE_DIR / (run_key + '.json')


def _worker_alive(run_key):
    """A lock file whose PID is gone means a dead worker, not a live session."""
    lock = apply.RUN_STATE_DIR / (run_key + '.lock')
    if not lock.exists():
        return False, 'no lock file for run_key %r -- no worker is running' % run_key
    try:
        pid = int(lock.read_text(encoding='utf-8').strip())
    except (ValueError, OSError) as e:
        return False, 'unreadable lock file: %s' % e
    import subprocess
    out = subprocess.run(['tasklist', '/FI', 'PID eq %d' % pid],
                         capture_output=True, text=True).stdout
    return (str(pid) in out), ('worker pid %d is %s' % (pid, 'running' if str(pid) in out else 'NOT running'))


def _run_status(run_key):
    path = _state_path(run_key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8')).get('status')
    except (OSError, json.JSONDecodeError):
        return None


def _guard(run_key, needs_write):
    """Shared precondition check. Returns an error string, or None if OK."""
    alive, detail = _worker_alive(run_key)
    if not alive:
        return ('No live browser worker for %r (%s). Nothing was attempted. A human must start or '
                'resume the session; do not assume the page state.' % (run_key, detail))
    if needs_write:
        status = _run_status(run_key)
        if status in HUMAN_OWNED_STATUSES:
            return ('Run %r is %r -- a human is editing this page right now. The agent must not fill, '
                    'upload, or navigate until the human explicitly signals to resume.' % (run_key, status))
    return None


def _send(run_key, command, timeout=DEFAULT_TIMEOUT):
    """Send one worker command. A timeout is reported as UNKNOWN, never as failure.

    This matters: apply.py's worker dequeues a command and keeps working even
    after the caller stops waiting. Reporting "failed" would invite a retry
    that double-applies the same action.
    """
    try:
        return apply.send_worker_command(run_key, command, timeout_seconds=timeout), False
    except TimeoutError as e:
        return ({'outcome': 'UNKNOWN_TIMEOUT',
                 'detail': str(e),
                 'instruction': ('The worker did not answer in time. It may STILL be executing this '
                                 'command. Do NOT retry it. Re-inspect the page to observe the real '
                                 'state before deciding anything.')}, True)
    except Exception as e:                                      # noqa: BLE001
        return ({'error': '%s: %s' % (type(e).__name__, e)}, True)


# --------------------------------------------------------------------------
# Read-only tools
# --------------------------------------------------------------------------

def inspect_application_page(run_key):
    """Read the live page's form structure. Strictly read-only."""
    err = _guard(run_key, needs_write=False)
    if err:
        raise ValueError(err)
    result, failed = _send(run_key, {'cmd': 'inspect'})
    if failed:
        return result
    inspection = result.get('inspection', {})
    fields = inspection.get('fields', [])
    return {
        'url': result.get('url'),
        'field_count': len(fields),
        'fields': [{'label': f.get('label'), 'type': f.get('type'), 'id': f.get('id'),
                    'required': f.get('required'), 'options_count': len(f.get('options') or [])}
                   for f in fields],
        'buttons': sorted({b.get('text', '').strip() for b in inspection.get('buttons', []) if b.get('text')}),
        'visible_text_sample': result.get('body_sample', '')[:1500],
        'caveat': ('Field VALUES are not included here. Use read_application_fields to observe current '
                   'values, and never assume a field is filled because it exists.'),
    }


def read_application_fields(run_key, fields):
    """Observe current values of specific fields. Writes nothing.

    `fields` is a list of {selector, label, kind} where kind is text|select.
    For a select, both the stored value AND the visible label are returned,
    because an option's internal value (a GUID, or "5" for May) is not the
    answer a human would recognise.
    """
    # Argument validation first: an invalid request is invalid whether or not a
    # session happens to be alive, and the model should get the precise reason.
    if not isinstance(fields, list) or not fields:
        raise ValueError('fields must be a non-empty list')
    steps = []
    for f in fields:
        if not isinstance(f, dict) or not f.get('selector'):
            raise ValueError('each field needs a selector')
        kind = f.get('kind', 'text')
        if kind not in ('text', 'select'):
            raise ValueError('kind must be text or select')
        steps.append({'action': 'select' if kind == 'select' else 'fill',
                      'selector': f['selector'], 'label': f.get('label') or f['selector']})
    err = _guard(run_key, needs_write=False)
    if err:
        raise ValueError(err)
    result, failed = _send(run_key, {'cmd': 'read_fields', 'steps': steps})
    if failed:
        return result
    return {'values': result.get('values', []),
            'note': 'Observed values only. Nothing was written.'}


# --------------------------------------------------------------------------
# Write tools (blocked while a human owns the page)
# --------------------------------------------------------------------------

def fill_application_fields(run_key, steps):
    """Fill fields and return per-field verification: expected vs. actually observed."""
    if not isinstance(steps, list) or not steps:
        raise ValueError('steps must be a non-empty list')
    for s in steps:
        if not isinstance(s, dict):
            raise ValueError('each step must be an object')
        if s.get('action') not in ('fill', 'select', 'check'):
            raise ValueError('action must be fill, select, or check (navigation and upload have their own tools)')
        if not s.get('selector'):
            raise ValueError('each step needs an explicit selector scoped to ONE intended control')
        if 'value' not in s:
            raise ValueError('each step needs a value')
    err = _guard(run_key, needs_write=True)
    if err:
        raise ValueError(err)
    result, failed = _send(run_key, {'cmd': 'fill_steps', 'steps': steps})
    if failed:
        return result
    verification = result.get('verification', [])
    unverified = [v for v in verification if not v.get('ok')]
    return {
        'verification': verification,
        'all_verified': not unverified,
        'unverified_fields': unverified,
        'rule': ('A field counts as filled ONLY if its verification entry is ok. For a select, compare '
                 'the intended answer against actual_label as well as actual_value. Do not advance the '
                 'page while required fields remain unverified.'),
    }


def answer_dropdown_question(run_key, question_label, option_label):
    """Answer an ARIA combobox question (the Greenhouse 'dropdown' pattern).

    A real, measured failure: these render as role="combobox" with a text
    input, so a plain fill() reports success and commits NOTHING. Verified on
    this live Greenhouse form -- 17 of its questions are comboboxes, and the
    sponsorship answer silently did not stick. Routes to apply.py's
    select_combobox, which clicks the trigger, picks the option by its visible
    text, and re-reads the committed selection.
    """
    if not isinstance(question_label, str) or not question_label.strip():
        raise ValueError('question_label must be the question\'s full visible text')
    if not isinstance(option_label, str) or not option_label.strip():
        raise ValueError('option_label must be the exact option text to select')
    err = _guard(run_key, needs_write=True)
    if err:
        raise ValueError(err)
    result, failed = _send(run_key, {'cmd': 'fill_steps', 'steps': [
        {'action': 'select_combobox', 'label': question_label, 'value': option_label}]})
    if failed:
        return result
    verification = result.get('verification', [])
    ok = bool(verification) and all(v.get('ok') for v in verification)
    return {
        'answered': ok, 'verification': verification,
        'rule': ('answered=false means the option is NOT committed. An option merely visible in an '
                 'open menu is not a selected answer. Re-read before trusting it.'),
    }


def upload_document(run_key, selector, file_path, label='Upload'):
    """Attach a document and verify the attachment actually completed."""
    if not Path(file_path).exists():
        raise ValueError('file does not exist: %s' % file_path)
    err = _guard(run_key, needs_write=True)
    if err:
        raise ValueError(err)
    result, failed = _send(run_key, {'cmd': 'fill_steps', 'steps': [
        {'action': 'upload', 'selector': selector, 'label': label, 'value': file_path}]})
    if failed:
        return result
    verification = result.get('verification', [])
    ok = bool(verification) and all(v.get('ok') for v in verification)
    return {
        'upload_verified': ok,
        'verification': verification,
        'rule': ('Selecting a local file, or seeing the filename elsewhere on the page, is NOT a '
                 'completed upload. Treat upload_verified=false as not attached.'),
    }


def advance_to_next_page(run_key, button_label, expected_destination_text=None):
    """Click an INTERMEDIATE navigation control and confirm the page really moved.

    Refuses any label outside NAVIGATION_ALLOWLIST. apply.py separately
    re-checks the actual resolved button text against its submit denylist.
    """
    # The allowlist is checked FIRST and unconditionally. A submit-like label
    # must be refused on its own merits -- never because a session happened to
    # be dead at that moment.
    if button_label not in NAVIGATION_ALLOWLIST:
        raise ValueError('refusing to click %r. Only intermediate navigation is allowed: %s. '
                         'Final submission is never performed by this agent.'
                         % (button_label, ', '.join(sorted(NAVIGATION_ALLOWLIST))))
    err = _guard(run_key, needs_write=True)
    if err:
        raise ValueError(err)
    command = {'cmd': 'click_next', 'label': button_label}
    if expected_destination_text:
        command['landmark_text'] = expected_destination_text
    result, failed = _send(run_key, command)
    if failed:
        return result
    return {
        'clicked': button_label,
        'url_after': result.get('url'),
        'page_changed': result.get('transitioned', result.get('ok')),
        'raw_result': result,
        'rule': ('Confirm the destination by its visible content before continuing. A changed URL alone '
                 'can be a login redirect, and an unchanged URL can still mean nothing happened.'),
    }


# --------------------------------------------------------------------------
# Definitions + registry (same shape tools.py uses, so agent.py can merge them)
# --------------------------------------------------------------------------

_RUN_KEY = {'type': 'string', 'description': 'Identifier of the live application run/session.'}

TOOL_DEFINITIONS = [
    {'name': 'inspect_application_page',
     'description': 'Read the live application page structure (fields, buttons, visible text). Read-only. '
                    'Does NOT return field values.',
     'input_schema': {'type': 'object', 'properties': {'run_key': _RUN_KEY}, 'required': ['run_key']}},
    {'name': 'read_application_fields',
     'description': 'Observe the CURRENT values of specific fields without changing them. For selects, '
                    'returns both the stored value and the visible label.',
     'input_schema': {'type': 'object', 'properties': {
         'run_key': _RUN_KEY,
         'fields': {'type': 'array', 'items': {'type': 'object', 'properties': {
             'selector': {'type': 'string', 'description': 'CSS/XPath scoped to ONE control.'},
             'label': {'type': 'string'},
             'kind': {'type': 'string', 'enum': ['text', 'select']}},
             'required': ['selector']}}},
         'required': ['run_key', 'fields']}},
    {'name': 'fill_application_fields',
     'description': 'Fill or select fields, then return per-field verification of what the page actually '
                    'shows afterwards. Blocked while a human is editing the page.',
     'input_schema': {'type': 'object', 'properties': {
         'run_key': _RUN_KEY,
         'steps': {'type': 'array', 'items': {'type': 'object', 'properties': {
             'action': {'type': 'string', 'enum': ['fill', 'select', 'check']},
             'selector': {'type': 'string'},
             'label': {'type': 'string'},
             'value': {'description': 'Value to enter, option label to select, or boolean to check.'}},
             'required': ['action', 'selector', 'value']}}},
         'required': ['run_key', 'steps']}},
    {'name': 'answer_dropdown_question',
     'description': 'Answer a DROPDOWN/combobox question by selecting an option, and verify the '
                    'selection actually committed. Use this (not fill_application_fields) for any '
                    'question rendered as a combobox -- a plain fill reports success but commits '
                    'nothing on these widgets.',
     'input_schema': {'type': 'object', 'properties': {
         'run_key': _RUN_KEY,
         'question_label': {'type': 'string', 'description': "The question's full visible text."},
         'option_label': {'type': 'string', 'description': 'Exact option text to select, e.g. "Yes".'}},
         'required': ['run_key', 'question_label', 'option_label']}},
    {'name': 'upload_document',
     'description': 'Attach a document to a specific upload widget and verify the attachment completed.',
     'input_schema': {'type': 'object', 'properties': {
         'run_key': _RUN_KEY, 'selector': {'type': 'string'},
         'file_path': {'type': 'string'}, 'label': {'type': 'string'}},
         'required': ['run_key', 'selector', 'file_path']}},
    {'name': 'advance_to_next_page',
     'description': 'Click an INTERMEDIATE navigation button (e.g. "Save and continue") and confirm the '
                    'page actually moved. Cannot submit an application; submit-like labels are refused.',
     'input_schema': {'type': 'object', 'properties': {
         'run_key': _RUN_KEY,
         'button_label': {'type': 'string', 'enum': sorted(NAVIGATION_ALLOWLIST)},
         'expected_destination_text': {'type': 'string',
                                       'description': 'Visible text expected on the destination page.'}},
         'required': ['run_key', 'button_label']}},
]

TOOL_REGISTRY = {
    'inspect_application_page': inspect_application_page,
    'read_application_fields': read_application_fields,
    'fill_application_fields': fill_application_fields,
    'answer_dropdown_question': answer_dropdown_question,
    'upload_document': upload_document,
    'advance_to_next_page': advance_to_next_page,
}

assert set(TOOL_REGISTRY) == {d['name'] for d in TOOL_DEFINITIONS}
assert not any('submit' in n.lower() for n in TOOL_REGISTRY), 'no submission tool may exist'
