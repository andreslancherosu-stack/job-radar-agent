"""Browser automation for the application-filling step of Job Radar.

Runs an observe-decide-act-verify loop against a supported web application
form, driven by a human-approved plan (field label -> value/profile path).
This module owns the hard guardrails: it never submits an application, and
it never invents an answer for a field the plan does not resolve.

Python 3.10+. Requires the `playwright` package and a downloaded browser
(see README.md / testapp/README.md for setup commands).
"""
import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

import answers

# Buttons whose visible text matches this pattern are never clicked by this
# tool, regardless of what a plan file requests. Page content (including
# button labels) is untrusted; this is defense-in-depth, not the primary
# guard. The primary guard is that the plan must mark a page as the review
# page, and once there this tool refuses to click anything at all.
SUBMIT_DENYLIST = re.compile(r'\b(submit|finish|complete|apply\s*now|send\s*application)\b', re.IGNORECASE)

ALLOWED_ACTIONS = ('fill', 'select', 'check', 'uncheck', 'upload', 'click_button', 'select_radio', 'select_combobox')

RUN_STATE_DIR = Path('private/application_runs')
RADAR_STATE_PATH = Path('private/state.json')


def already_applied(job_key, radar_state_path=RADAR_STATE_PATH):
    """Cross-checks radar.py's application history (a separate state file
    from this module's run state). Returns True if job_key is already
    recorded there as a confirmed application, so a caller can refuse to
    start filling the same job twice."""
    path = Path(radar_state_path)
    if not path.exists():
        return False
    radar_state = json.loads(path.read_text(encoding='utf-8'))
    return job_key in radar_state.get('applications', {})


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temp, path)


def run_state_path(run_key):
    return RUN_STATE_DIR / (run_key + '.json')


def run_lock_path(run_key):
    return RUN_STATE_DIR / (run_key + '.lock')


def resume_signal_path(run_key):
    """Human -> automation: 'continue to the next step.' Used at a
    pause_after_fill checkpoint, where the automation is deliberately
    blocked mid-run (browser still open, nothing further attempted) waiting
    for a human edit to finish. Distinct from close_signal_path — receiving
    THIS signal never closes the browser, only lets the SAME run continue."""
    return RUN_STATE_DIR / (run_key + '_resume.flag')


def close_signal_path(run_key):
    """Human -> automation: 'I am completely done with this browser window,
    you may exit and close it now.' Used only at a terminal hold (review
    page reached, blocked_validation, error, or session_lost) — never
    implied by resume_signal_path. Keeping these as two distinct files
    fixes a real bug: an earlier script treated one ambiguous 'done' signal
    as authorization to close, when the human had typed it to mean
    'continue automating,' and the browser closed out from under a
    still-in-progress manual edit."""
    return RUN_STATE_DIR / (run_key + '_close.flag')


class RunLockedError(RuntimeError):
    """Raised when another live process already holds the lock for this
    run_key — two workers must never drive the same browser/application at
    once (e.g. two `fill` invocations against the same run_key, or a `fill`
    started while a prior one is still mid-run holding at a checkpoint)."""


def _pid_is_running(pid):
    if os.name == 'nt':
        try:
            import subprocess
            out = subprocess.run(['tasklist', '/FI', 'PID eq %d' % pid],
                                  capture_output=True, text=True, timeout=5)
            return str(pid) in out.stdout
        except Exception:
            return True  # can't confirm either way — fail safe, don't reclaim a lock we can't verify is dead
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


def acquire_run_lock(run_key):
    """Prevents a second worker from touching the same application's
    browser/state concurrently. A lock file holding a PID that is no longer
    running is stale (e.g. a crashed process) and is reclaimed rather than
    blocking forever; a lock held by a live PID raises RunLockedError."""
    lock_path = run_lock_path(run_key)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        try:
            held_pid = int(lock_path.read_text(encoding='utf-8').strip())
        except (ValueError, OSError):
            held_pid = None
        if held_pid and _pid_is_running(held_pid):
            raise RunLockedError(
                'Another worker (pid %d) already holds the lock for run_key %r (%s). Refusing to start '
                'a second worker against the same application — it would risk two processes filling or '
                'navigating the same browser session at once.' % (held_pid, run_key, lock_path))
    lock_path.write_text(str(os.getpid()), encoding='utf-8')
    return lock_path


def release_run_lock(run_key):
    run_lock_path(run_key).unlink(missing_ok=True)


def load_run_state(run_key):
    path = run_state_path(run_key)
    if path.exists():
        return read_json(path)
    return {
        'run_key': run_key,
        'status': 'not_started',
        'current_url': None,
        'page_index': 0,
        'action_log': [],
        'missing_questions': [],
        'issues': [],
        'field_errors': [],
    }


def save_run_state(state):
    state['updated_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    write_json(run_state_path(state['run_key']), state)


def log_action(state, message):
    state['action_log'].append({
        'at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'message': message,
    })


def resolve_value(profile, value_or_path, answer_bank=None):
    """A plan step's value is one of:
      - a literal (returned as-is)
      - "profile:some.dotted.path" (looked up in the profile dict/lists)
      - "answerbank:<question text>" (looked up as an exact-normalized,
        human-confirmed match in the answer bank — see answers.py)

    Returns (value, missing) where missing=True means the reference could
    not be resolved and the caller must not invent a substitute.
    """
    if not isinstance(value_or_path, str):
        return value_or_path, False
    if value_or_path.startswith('answerbank:'):
        question = value_or_path[len('answerbank:'):]
        if answer_bank is None:
            return None, True
        match = answers.find_exact_match(answer_bank, question)
        return (match['answer'], False) if match else (None, True)
    if not value_or_path.startswith('profile:'):
        return value_or_path, False
    path = value_or_path[len('profile:'):]
    node = profile
    for part in path.split('.'):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None, True
    if node is None or node == '':
        return None, True
    return node, False


def is_denied_button(label):
    return bool(SUBMIT_DENYLIST.search(label or ''))


def load_plan(path, run_authorization=None):
    """run_authorization lets a human authorize ONE specific supervised run
    of a plan file that is still, deliberately, approved:false on disk (e.g.
    because some line items remain provisional) — distinct from flipping the
    file to approved:true, which would authorize any future unsupervised
    run. It bypasses only this one gate; the action allowlist below, and
    every other check in this module (missing-answer collection, the
    submit-button guardrails, evidence validation), still applies in full."""
    plan = read_json(path)
    if not plan.get('approved') and not run_authorization:
        raise ValueError('Plan is not approved. A human must set "approved": true after reviewing it, '
                          'or explicitly authorize this one run via run_authorization / '
                          '--authorize-unapproved-run.')
    if 'pages' not in plan or not isinstance(plan['pages'], list):
        raise ValueError('Plan must contain a "pages" list.')
    for page in plan['pages']:
        for step in page.get('steps', []):
            if step.get('action') not in ALLOWED_ACTIONS:
                raise ValueError('Unsupported plan action: ' + str(step.get('action')))
    return plan


def missing_for_step(profile, step, answer_bank=None):
    """Returns a (label, value_ref) pair if this step's value cannot be
    resolved from the profile or answer bank, else None. click_button has no
    value to check."""
    if step.get('skip_approved'):
        # A human has explicitly reviewed this exact field and approved
        # leaving it blank (e.g. an unknown exact day on a date field) —
        # distinct from "missing": this step is deliberately never resolved
        # or filled, and must not block the rest of an otherwise-ready plan.
        return None
    action = step['action']
    if action == 'click_button':
        return None
    if action == 'select_radio':
        _, missing = resolve_value(profile, step.get('value'), answer_bank)
        if missing:
            return (step.get('question', step.get('label', 'radio group')), step.get('value'))
        return None
    _, missing = resolve_value(profile, step.get('value'), answer_bank)
    if missing:
        return (step.get('label'), step.get('value'))
    return None


def collect_missing_questions(plan, profile, answer_bank=None):
    """Pre-flight pass over the whole plan, resolving every value against the
    profile/answer bank without touching a browser. Filling must never start
    on a page it cannot finish, so all missing answers are gathered as one
    batch up front rather than discovered mid-page."""
    missing = []
    for page_plan in plan['pages']:
        for step in page_plan.get('steps', []):
            found = missing_for_step(profile, step, answer_bank)
            if found:
                missing.append({'label': found[0], 'profile_path': found[1]})
    return missing


def locate_field(page, step):
    """Real forms sometimes give two different fields the identical
    accessible label (e.g. a Resume upload and a Cover Letter upload both
    computing to "Attach", seen on a real Greenhouse posting) — get_by_label
    alone is then ambiguous. A step may set "selector" (a raw CSS selector,
    e.g. "#resume") to target the field precisely instead of by label; this
    is an explicit escape hatch a human approves in the plan, not a guess
    made at fill time."""
    if step.get('selector'):
        return page.locator(step['selector'])
    return page.get_by_label(step.get('label'), exact=True)


_BLOCK_TAG_ATTR = 'data-apply-block-index'


def find_block_containers(page, remove_text, anchor_text, scope=None):
    """Locates each instance of a dynamically-cloned repeating form block
    (e.g. one employer or one education entry in an "add another" section)
    as its own scoped Locator, WITHOUT relying on the block's field ids
    being unique — measured on a real Avature application form that reuses
    the identical input id across different employer blocks, which broke
    page-wide label-proximity targeting (a "Title" edit landed on a
    different employer's field than intended).

    For each element whose text matches remove_text (the block's own
    "remove"-style control, one per block) WITHIN scope (a Locator for the
    section containing this repeating group — e.g. the "Work history"
    section, not the whole page), walks up the DOM, but never past scope's
    own boundary, to find the SMALLEST ancestor that contains anchor_text
    exactly once. Without scope, a "remove" link belonging to a DIFFERENT
    section (e.g. Employment's own remove links, which also match the
    literal text "remove") gets walked all the way up past its real block
    boundary, since employment blocks don't contain an education-specific
    anchor like "Field of study" at all — the walk doesn't stop at count==0
    and can spuriously latch onto some unrelated ancestor that happens to
    contain exactly one match. scope is required in practice whenever more
    than one repeating section share the same remove_text on one page.

    That ancestor is tagged with a unique data attribute so a stable,
    scoped Locator can be built from it; every further lookup within that
    block should be done via `container.get_by_text(...)` /
    `container.locator(...)`, never a page-wide query, so an id collision
    elsewhere on the page can't leak in.

    Returns a list of Locators, one per block, in document order. A block
    whose container can't be uniquely resolved is omitted, not guessed at —
    callers should treat a missing block as "leave untouched", not fill it
    positionally.
    """
    js = '''(root, [removeText, anchorText, attr]) => {
        root.querySelectorAll('[' + attr + ']').forEach(el => el.removeAttribute(attr));
        const escape = s => s.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
        const removeRe = new RegExp(escape(removeText), 'i');
        const anchorRe = new RegExp(escape(anchorText), 'i');
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
        const removeEls = [];
        let node;
        while ((node = walker.nextNode())) {
            const own = Array.from(node.childNodes)
                .filter(n => n.nodeType === Node.TEXT_NODE)
                .map(n => n.textContent).join(' ');
            if (removeRe.test(own) && node.children.length === 0) removeEls.push(node);
        }
        let idx = 0;
        for (const removeEl of removeEls) {
            let container = removeEl;
            let found = null;
            for (let depth = 0; depth < 12 && container && container !== root.parentElement; depth++) {
                const text = container.innerText || '';
                const count = (text.match(new RegExp(anchorRe.source, 'gi')) || []).length;
                if (count === 1) { found = container; break; }
                if (count > 1) break;
                if (container === root) break;
                container = container.parentElement;
            }
            // Skip inert template blocks some frameworks keep in the DOM to
            // clone from (measured: Avature marks these with a "-sample"
            // id suffix on their inputs AND keeps them non-rendered) --
            // offsetParent is null for anything not actually laid out.
            if (found && found.offsetParent === null) found = null;
            if (found) {
                found.setAttribute(attr, String(idx));
                idx += 1;
            }
        }
    }'''
    target = scope.first if scope is not None else page.locator('body')
    target.evaluate(js, [remove_text, anchor_text, _BLOCK_TAG_ATTR])
    tagged = target.locator(f'[{_BLOCK_TAG_ATTR}]')
    return [tagged.nth(i) for i in range(tagged.count())]


_QUESTION_TAG_ATTR = 'data-apply-question-index'


def find_question_container(page, question_text, scope=None):
    """Locates the SINGLE scoped container for one yes/no or
    multiple-choice QUESTION block, keyed by the question's own real,
    human-readable text -- never by an internal control/group name (e.g. a
    real, measured case: UKG/UltiPro renders radio groups named generically
    like "MultipleChoiceResponse4", which reveals nothing about which
    actual question it answers and cannot be trusted to stay in the same
    order between form loads) and never by field position.

    Finds the leaf DOM node whose OWN text (not descendants') matches
    question_text, then walks up to the smallest ancestor that both (a)
    still contains question_text exactly once (stops before leaking into a
    neighboring question's container) and (b) actually contains an
    answerable control (radio/checkbox/select) -- the question prompt
    itself is often a sibling of its controls, not their direct parent.

    Returns a list of Locators (normally 0 or 1; a caller should refuse to
    act when it isn't exactly 1, the same discipline find_block_containers
    uses for repeating blocks). This is the read side of a
    validate-once-reuse-carefully workflow: a caller should independently
    re-verify a SAVED mapping's question_text and options still match the
    live page before trusting it, never assume a mapping stays valid
    forever."""
    js = '''(root, [questionText, attr]) => {
        root.querySelectorAll('[' + attr + ']').forEach(el => el.removeAttribute(attr));
        const escape = s => s.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
        const qRe = new RegExp(escape(questionText), 'i');
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
        let node, leaf = null;
        while ((node = walker.nextNode())) {
            const own = Array.from(node.childNodes)
                .filter(n => n.nodeType === Node.TEXT_NODE)
                .map(n => n.textContent).join(' ');
            if (qRe.test(own) && node.offsetParent !== null) { leaf = node; break; }
        }
        if (!leaf) return;
        let container = leaf;
        for (let depth = 0; depth < 15 && container && container !== root.parentElement; depth++) {
            const hasControls = container.querySelectorAll(
                'input[type=radio], input[type=checkbox], select').length > 0;
            const text = container.innerText || '';
            const count = (text.match(new RegExp(qRe.source, 'gi')) || []).length;
            if (hasControls && count === 1) { container.setAttribute(attr, '1'); return; }
            if (count > 1) return;  // overshot into a neighboring question's territory
            if (container === root) return;
            container = container.parentElement;
        }
    }'''
    target = scope.first if scope is not None else page.locator('body')
    target.evaluate(js, [question_text, _QUESTION_TAG_ATTR])
    tagged = target.locator(f'[{_QUESTION_TAG_ATTR}]')
    return [tagged.nth(i) for i in range(tagged.count())]


def answer_radio_question(container, answer_label):
    """Selects the radio option within a SCOPED question container whose
    own accessible label matches answer_label, then re-reads is_checked()
    from that SAME element to confirm -- never a page-wide get_by_label
    search, which would match every identically-labeled "Yes"/"No" option
    across every OTHER question on the page too. Refuses (returns
    ok=False) rather than guess if the container doesn't resolve to
    exactly one matching radio."""
    radio = container.get_by_label(answer_label, exact=True)
    count = radio.count()
    if count != 1:
        return {'ok': False, 'error': 'expected exactly 1 radio labeled %r in this question container, found %d'
                                       % (answer_label, count)}
    radio.check()
    checked = radio.is_checked()
    return {'ok': checked, 'checked': checked}


def read_question_container_state(container):
    """Read-only: the question container's own visible text (for
    validating a saved mapping still matches the live page) and which
    radio option, if any, is currently checked -- scoped strictly to this
    one container, never the whole page. Reads each checked radio's label
    via the DOM's own `.labels` property, which resolves BOTH an explicit
    label[for=id] association AND a label that simply wraps the input
    (e.g. <label><input type=radio> Yes</label>, common on real forms and
    used throughout this project's own local fixtures) -- a for=id-only
    lookup would silently find nothing for the wrapping case."""
    text = container.inner_text()
    checked_labels = container.evaluate(
        '''(root) => Array.from(root.querySelectorAll('input[type="radio"]'))
            .filter(r => r.checked)
            .map(r => (r.labels && r.labels[0] ? r.labels[0].innerText : '').trim())
            .filter(Boolean)''')
    return {'container_text': text[:800], 'checked_labels': checked_labels}


def _text_inputs_in_block(container):
    # container.locator(...) with a plain CSS selector is bounded to
    # descendants of container ONLY -- unlike an xpath following:: axis,
    # which is document-relative and can escape the block. This is the
    # actual fix for the cross-block field mixup: positional indexing is
    # safe once the container itself is correctly, uniquely isolated.
    return container.locator('input[type="text"], input:not([type="month"]):not([type="password"]):not([type="hidden"]):not([type="checkbox"]):not([type="radio"]):not([type="file"])')


def fill_in_block(container, field_index, value, results, field_name, expected_label_text=None):
    """Fills the field_index-th text input found WITHIN a single scoped
    block container (from find_block_containers) -- bounded strictly to
    that container's own descendants, so a duplicate id/label elsewhere on
    the page cannot be matched instead. If expected_label_text is given,
    first sanity-checks that text appears exactly once in the container
    (confirms this is really the intended kind of block) before touching
    anything.

    A non-empty EXISTING value is compared against the intended `value`
    before being accepted -- a real, measured bug (caught by an
    independent audit) previously treated ANY non-empty existing value as
    an "already_filled" success, which silently accepted a wrong,
    never-reviewed resume-parser value (e.g. a garbled job title) as if it
    were an approved manual correction. Only a MATCHING existing value
    counts as already_filled; a mismatched one is reported as an explicit
    conflict (ok=False) for a human to resolve, and is never overwritten
    either -- this function still never types over an existing value."""
    if expected_label_text is not None:
        labelled = container.get_by_text(expected_label_text, exact=False)
        if labelled.count() != 1:
            results.append({'field': field_name, 'action': 'block_sanity_check_failed',
                             'label_text': expected_label_text, 'matches': labelled.count()})
            return False
    inputs = _text_inputs_in_block(container)
    if inputs.count() <= field_index:
        results.append({'field': field_name, 'action': 'field_index_out_of_range',
                         'field_index': field_index, 'input_count': inputs.count()})
        return False
    target = inputs.nth(field_index)
    try:
        current = target.input_value()
    except Exception:
        current = None
    if current:
        if current == str(value):
            results.append({'field': field_name, 'action': 'already_filled', 'value': current, 'ok': True})
            return True
        results.append({'field': field_name, 'action': 'conflict_existing_value_differs',
                         'existing_value': current, 'intended_value': str(value), 'ok': False})
        return False
    try:
        target.fill(str(value))
        actual = target.input_value()
        ok = actual == str(value)
        results.append({'field': field_name, 'action': 'filled', 'expected': str(value), 'actual': actual, 'ok': ok})
        return ok
    except Exception as e:
        results.append({'field': field_name, 'action': 'error', 'error': str(e)})
        return False


def read_block_field(container, field_index):
    """Reads back the field_index-th text input's current value, bounded to
    one block container -- used to independently verify what a block
    actually contains after edits, not just trust what was last written."""
    inputs = _text_inputs_in_block(container)
    if inputs.count() <= field_index:
        return None
    try:
        return inputs.nth(field_index).input_value()
    except Exception:
        return None


class HandoffSafetyCapReached(TimeoutError):
    """Raised by wait_for_handoff_signal when safety_cap_seconds elapses
    with no signal. This is NOT the fixed short "leave open N seconds then
    close" pattern this module deliberately removed (that pattern once
    closed the browser mid-edit while a human was actively still using it)
    -- it is a crash-guard against a truly abandoned process, deliberately
    generous (hours, not minutes) and never the intended way to end a
    handoff. Callers must handle it explicitly and distinctly from a
    normal completion or a site error, per an independent audit's finding
    that an earlier version of this module's own prose overstated this as
    "no fixed timeout anywhere" without that qualification."""


def wait_for_handoff_signal(page, signal_file, poll_seconds=2, safety_cap_seconds=4 * 3600):
    """Holds a browser session open for an UNBOUNDED human-editing period,
    polling only for signal_file to exist -- not a fixed sleep. A fixed
    "leave open N seconds then close regardless" timeout (the pattern used
    elsewhere in this module) measurably cut off a real, in-progress manual
    edit once: the browser closed itself mid-edit because the timer
    expired, not because of anything the human did. safety_cap_seconds is
    a crash-guard against a truly abandoned process, not the intended exit
    path, and is deliberately generous (hours, not minutes) -- the real
    "done" signal is the caller creating signal_file (e.g. once the human
    says "Done" in chat). This module does NOT promise indefinite waiting
    with no cap at all -- it promises no SHORT, silently-enforced closure
    while a human may still be editing; the generous cap still exists as a
    last-resort guard and callers must treat HandoffSafetyCapReached as its
    own distinct, explicitly-reported state, never a silent close. Returns
    the actual seconds waited. Keeps the SAME page/browser connection alive
    throughout, so the caller can continue automating in this same session
    afterward without reattaching."""
    signal_file = Path(signal_file)
    waited = 0
    while not signal_file.exists():
        page.wait_for_timeout(poll_seconds * 1000)
        waited += poll_seconds
        if waited >= safety_cap_seconds:
            raise HandoffSafetyCapReached('Safety cap reached waiting for handoff signal: ' + str(signal_file))
    return waited


def check_session_alive(page, session_check):
    """Verifies a resumed page genuinely continues a prior session, rather
    than silently landing on a fresh/logged-out re-render at the same URL —
    the real failure mode measured on a live Avature application: a
    close-then-relaunch against the same on-disk browser profile navigated
    to the same known URL, but the server no longer recognized the session
    (no "Sign Out" text, an empty-looking form) and the earlier fill data
    was not there. A durable cookie profile on disk is not proof the
    SERVER still honors that session — this reads the live page itself.

    session_check is {'text_present': [...]} / {'text_absent': [...]} from
    the plan (both optional). Every text_present string must appear in the
    page's visible body text; every text_absent string must not. Returns
    (alive: bool, reason: str|None) — reason is set only when alive=False,
    naming exactly which check failed so the caller can report a concrete
    reason rather than a bare "session lost"."""
    if not session_check:
        return True, None
    try:
        body_text = page.locator('body').inner_text()
    except Exception as e:
        return False, 'could not read the page body to verify session continuity: %s' % e
    for text in session_check.get('text_present', []):
        if text not in body_text:
            return False, 'expected text not found on the resumed page (session likely lost/reset): %r' % text
    for text in session_check.get('text_absent', []):
        if text in body_text:
            return False, 'unexpected text found on the resumed page (session likely lost/reset to a fresh state): %r' % text
    return True, None


def apply_step(page, step, profile, state, answer_bank=None):
    action = step['action']
    label = step.get('label')

    if step.get('skip_approved'):
        log_action(state, 'skipped (explicitly approved to leave blank): ' + str(label))
        return True

    if action == 'click_button':
        if is_denied_button(label):
            raise RuntimeError('Refusing to click a submit-like button: ' + label)
        # A real, measured gap: this previously always used a page-wide
        # get_by_role('button', name=label) search with no selector
        # escape hatch, unlike locate_field -- fine for a one-of-a-kind
        # "Save and continue", but unusable for a repeating list where
        # every item's remove control shares the identical accessible
        # name (e.g. every skill tag's own "x" button on a real UKG/
        # UltiPro Skills section). A caller that has already scoped an
        # anchored selector to the ONE intended control (the same
        # discipline find_question_container uses) can now pass it
        # through, still subject to the same submit-denylist check above.
        button = page.locator(step['selector']) if step.get('selector') else page.get_by_role(
            'button', name=label, exact=True)
        # The denylist check above only inspects the step's OWN DECLARED
        # label -- a selector escape hatch means that string is no longer
        # guaranteed to describe what actually gets clicked. A selector
        # could resolve to a real submit/apply button while the step
        # claims an innocent label like "Save and continue", silently
        # defeating the guardrail. Re-check the ACTUAL resolved element's
        # own visible text/accessible name before clicking, regardless of
        # what the step claims -- the denylist is enforced against reality,
        # never against a caller-supplied description of reality.
        actual_text = button.evaluate(
            "el => (el.innerText || el.value || el.getAttribute('aria-label') || '').trim()")
        if is_denied_button(actual_text):
            raise RuntimeError('Refusing to click: the selector resolves to a submit-like button (%r), '
                                'regardless of the declared label %r' % (actual_text, label))
        button.click()
        log_action(state, 'clicked button: ' + label)
        return True

    if action == 'select_radio':
        question = step.get('question', label or 'radio group')
        value, missing = resolve_value(profile, step.get('value'), answer_bank)
        if missing:
            raise RuntimeError('Missing profile value reached apply_step despite pre-flight check: ' + question)
        options = step.get('options', {})
        option_label = options.get(str(value))
        if option_label is None:
            raise RuntimeError('No option mapped for value %r in radio group %s' % (value, question))
        radio = page.get_by_label(option_label, exact=True)
        radio.check()
        if not radio.is_checked():
            raise RuntimeError('Verification failed for radio option: ' + option_label)
        log_action(state, 'selected radio option: ' + option_label)
        return True

    value, missing = resolve_value(profile, step.get('value'), answer_bank)
    if missing:
        raise RuntimeError('Missing profile value reached apply_step despite pre-flight check: ' + str(label))

    if action == 'fill':
        field = locate_field(page, step)
        field.fill(str(value))
        actual = field.input_value()
        if actual != str(value):
            raise RuntimeError('Verification failed for "%s": expected %r, page has %r' % (label, value, actual))
        log_action(state, 'filled field: ' + label)
    elif action == 'select':
        field = locate_field(page, step)
        field.select_option(str(value))
        log_action(state, 'selected option: ' + label)
    elif action == 'check':
        checkbox = locate_field(page, step)
        if value:
            checkbox.check()
        actual = checkbox.is_checked()
        if actual != bool(value):
            raise RuntimeError('Verification failed for checkbox "%s": expected %r, page has %r' % (label, value, actual))
        log_action(state, 'checkbox set: ' + label + ' = ' + str(bool(value)))
    elif action == 'uncheck':
        checkbox = locate_field(page, step)
        checkbox.uncheck()
        log_action(state, 'checkbox unset: ' + label)
    elif action == 'upload':
        file_path = str(value)
        if not Path(file_path).exists():
            raise RuntimeError('Upload file not found: ' + file_path)
        field = locate_field(page, step)
        # A stable handle on a wider container, captured BEFORE any click —
        # some real upload widgets (confirmed on a real Greenhouse page)
        # replace their own inner DOM after a successful selection, so
        # anything queried relative to the original <input> can stop
        # resolving afterward; walking up first and holding an element
        # handle (not a re-queried locator) survives that replacement.
        widget_handle = field.evaluate_handle(
            '''el => {
                let node = el;
                for (let i = 0; i < 4 && node.parentElement; i++) node = node.parentElement;
                return node;
            }''')
        # Trigger discovery, widest-first-match. A file input's real
        # user-facing trigger is very often a <label for="..."> styled as a
        # button, NOT an element with role=button -- measured on Bain's
        # Recruits Portal, where "From Device" is a label: the old
        # parent-scoped role=button lookup found nothing, silently fell
        # through to set_input_files(), and the page's own JS never ran, so
        # its hidden "Continue" button (id=uploadFileResume) was never
        # revealed and the application could not advance at all. The file
        # sat staged in the DOM input looking plausible while nothing had
        # actually been handed to the site.
        # Order matters, and VISIBILITY matters more. A real widget often has
        # BOTH a visible button and a visually-hidden <label for=...> kept
        # only for screen readers (exactly what testapp/page_upload_widget
        # .html reproduces from a real Greenhouse page) -- clicking that
        # hidden label instead of the visible button arms nothing. So: take
        # the first candidate that is actually VISIBLE, button first.
        input_id = None
        try:
            input_id = field.get_attribute('id')
        except Exception:
            pass
        # NOTE: get_by_role('button') is deliberately NOT used here. Chromium
        # gives a bare <input type="file"> an implicit ARIA button role, so a
        # role-based lookup matches the very input we are already holding and
        # "triggers" itself -- which looks like it worked while never touching
        # the page's own control. Match real elements instead.
        candidates = [field.locator('..').locator('button, [role="button"]')]
        if input_id:
            candidates.append(page.locator('label[for="%s"]' % input_id))
        trigger = None
        for candidate in candidates:
            try:
                if candidate.count() > 0 and candidate.first.is_visible():
                    trigger = candidate
                    break
            except Exception:
                continue
        if trigger is not None:
            # Real-user interaction: click the widget's own trigger and let
            # Playwright intercept the native file chooser, rather than
            # stuffing the (possibly JS-managed) hidden input directly —
            # measured to silently break a real widget's internal upload
            # handler (see docs/APPLY_VALIDATION.md).
            try:
                trigger_name = trigger.first.inner_text()
            except Exception:
                trigger_name = '<unreadable>'
            try:
                with page.expect_file_chooser(timeout=10000) as fc_info:
                    trigger.first.click()
                fc_info.value.set_files(file_path)
            except Exception as e:
                # No native file chooser appeared — capture what the widget
                # actually contains right now instead of surfacing a bare
                # Playwright timeout, so a real cause (wrong button matched,
                # widget already in a different state, page not yet
                # interactive) can be diagnosed rather than assumed.
                try:
                    widget_state = widget_handle.evaluate('el => el.innerHTML || ""')
                except Exception:
                    widget_state = '<unreadable>'
                raise RuntimeError(
                    'No file chooser appeared after clicking the "%s" button for "%s" (%s). '
                    'Widget contents at failure time: %r' % (trigger_name, label, e, widget_state[:500])) from e
        else:
            field.set_input_files(file_path)
        filename = Path(file_path).name
        # Poll rather than a single fixed sleep — measured on a real, loaded
        # page that a flat 500ms wait can be too short for the widget's own
        # render to settle, which previously produced a false NEGATIVE here
        # (the file was actually attached, but this check ran before the
        # widget's success text appeared, wasting a retry against a trigger
        # the widget had by then already replaced with a "Remove file"
        # control). Still bounded — a genuine failure still raises below.
        widget_text = ''
        for _ in range(10):
            widget_text = widget_handle.evaluate('el => el.innerText || el.textContent || ""')
            if filename in widget_text:
                break
            page.wait_for_timeout(500)
        if filename not in widget_text:
            raise RuntimeError('Verification failed for upload "%s": filename %r not shown near the field after '
                                'upload (widget text: %r) — treat as failed, not a successful upload'
                                % (label, filename, widget_text[:300]))
        log_action(state, 'uploaded and verified file for: ' + label + ' (filename shown: ' + filename + ')')
    elif action == 'select_combobox':
        # Many real ATS platforms (e.g. Greenhouse) implement "dropdown"
        # questions as a custom widget, not a native <select> — a trigger
        # exposing role="combobox" that opens a role="listbox" of
        # role="option" items. select_option() cannot touch these; this
        # clicks the trigger by its accessible name, then the option by its
        # visible text, per the WAI-ARIA combobox pattern real widgets
        # generally follow.
        #
        # Some such comboboxes are also type-ahead: the option list is only
        # populated (often asynchronously) after typing into the trigger
        # (e.g. a long school picker). When the trigger itself is an
        # editable input/textarea, type the target value into it before
        # looking for the option — a harmless no-op on a button-style
        # trigger (nothing to type into) and on an already-populated
        # ordinary listbox (typing the value narrows or does nothing to a
        # list that already contains it).
        trigger = page.get_by_role('combobox', name=label, exact=True)
        trigger.click()
        tag = trigger.evaluate('el => el.tagName.toLowerCase()')
        is_editable = tag in ('input', 'textarea')
        if is_editable:
            # locator.fill() (not page.keyboard.type) — it focuses the
            # element itself and dispatches input/change events reliably,
            # independent of ambient page-level focus state.
            trigger.fill(str(value))
        option = page.get_by_role('option', name=str(value), exact=True)
        option.wait_for(state='visible', timeout=10000)  # async filtering can take a moment
        option.click()
        actual = trigger.input_value() if is_editable else trigger.inner_text().strip()
        if actual != str(value):
            # Some widgets (e.g. react-select-style pickers) clear the search
            # input itself after selection and render the chosen value in a
            # nearby sibling element instead. Before concluding verification
            # failed, check a small ancestor container for the expected text.
            container_text = trigger.evaluate(
                '''(el, depth) => {
                    let node = el;
                    for (let i = 0; i < depth && node.parentElement; i++) node = node.parentElement;
                    return node.innerText || node.textContent || '';
                }''', 3)
            if str(value) not in container_text:
                raise RuntimeError('Verification failed for combobox "%s": expected %r, trigger shows %r '
                                    '(and not found in the surrounding container either)' % (label, value, actual))
            actual = str(value)
        log_action(state, 'selected combobox option: ' + label + ' = ' + str(value))
    return True


MAX_STEP_ATTEMPTS = 2


def apply_step_with_retries(page, step, profile, state, answer_bank=None, max_attempts=MAX_STEP_ATTEMPTS):
    """Wraps apply_step with bounded retries for one field. A field that
    still fails after max_attempts is recorded in state['field_errors'] and
    the run continues with the remaining, independent fields — one flaky or
    unsupported field must not abandon everything else already filled."""
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return apply_step(page, step, profile, state, answer_bank)
        except Exception as e:
            last_error = e
            if attempt < max_attempts:
                log_action(state, 'retrying step %d/%d after error on "%s": %s'
                           % (attempt, max_attempts, step.get('label'), e))
                page.wait_for_timeout(500)
    label = step.get('label') or step.get('question') or step.get('action')
    state.setdefault('field_errors', []).append({'label': label, 'error': str(last_error)})
    log_action(state, 'field failed after %d attempt(s), continuing with other fields: %s — %s'
               % (max_attempts, label, last_error))
    return False


_INSPECT_JS = """
() => {
  // Text within aria-hidden="true" is excluded from the accessible name by
  // the real accessible-name algorithm Playwright's get_by_label/get_by_role
  // use (e.g. a required-field "*" is very often wrapped in an aria-hidden
  // span for exactly this reason) — plain el.innerText does NOT know that,
  // so it over-reports label text a locator would never actually match.
  function visibleText(node) {
    let text = '';
    node.childNodes.forEach(child => {
      if (child.nodeType === Node.TEXT_NODE) {
        text += child.textContent;
      } else if (child.nodeType === Node.ELEMENT_NODE) {
        if (child.getAttribute('aria-hidden') === 'true') return;
        text += visibleText(child);
      }
    });
    return text;
  }
  function computeLabel(el) {
    if (el.labels && el.labels.length) return visibleText(el.labels[0]).trim();
    const ariaLabel = el.getAttribute('aria-label');
    if (ariaLabel) return ariaLabel.trim();
    const labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      const t = labelledby.split(' ').map(id => {
        const node = document.getElementById(id);
        return node ? visibleText(node) : '';
      }).join(' ').trim();
      if (t) return t;
    }
    return null;
  }
  const fields = [];
  document.querySelectorAll('input, select, textarea').forEach(el => {
    if (el.type === 'hidden') return;
    fields.push({
      tag: el.tagName.toLowerCase(),
      type: el.type || null,
      name: el.name || null,
      id: el.id || null,
      required: !!(el.required || el.getAttribute('aria-required') === 'true'),
      label: computeLabel(el),
      options: el.tagName.toLowerCase() === 'select'
        ? Array.from(el.options).map(o => o.text.trim()).filter(Boolean) : null,
    });
  });
  const buttons = [];
  document.querySelectorAll('button, input[type=submit], input[type=button]').forEach(el => {
    buttons.push({ text: (el.innerText || el.value || '').trim(), type: el.type || null,
                   role: el.getAttribute('role') || null });
  });
  const comboboxes = [];
  document.querySelectorAll('[role=combobox]').forEach(el => {
    comboboxes.push({
      label: computeLabel(el) || (el.innerText || '').trim() || null,
      current_text: (el.innerText || el.value || '').trim(),
      tag: el.tagName.toLowerCase(),
    });
  });
  return {
    fields: fields,
    buttons: buttons,
    comboboxes: comboboxes,
    custom_combobox_or_listbox_count: document.querySelectorAll('[role=combobox], [role=listbox]').length,
  };
}
"""


def inspect_page(url, headless=True, timeout=20000):
    """Read-only DOM inspection: navigates and reads the page's accessible
    form structure, but never fills, checks, clicks, or uploads anything.
    Used to build a plan and identify gaps BEFORE any plan is approved."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_page()
        try:
            page.goto(url, timeout=timeout)
            raw = page.evaluate(_INSPECT_JS)
        finally:
            browser.close()

    fields = raw['fields']
    for f in fields:
        f['looks_like_submit_button'] = False  # fields are never buttons; kept for shape symmetry
    buttons = raw['buttons']
    for b in buttons:
        b['looks_like_submit_button'] = is_denied_button(b['text'])

    return {
        'url': url,
        'field_count': len(fields),
        'fields': fields,
        'buttons': buttons,
        'comboboxes': raw['comboboxes'],
        'custom_combobox_or_listbox_count': raw['custom_combobox_or_listbox_count'],
    }


def inspect_combobox_options(url, queries, headless=True, timeout=20000):
    """Read-only: opens each named combobox (by its accessible name, as
    reported by inspect_page's comboboxes list) long enough to read its
    option list, then closes it with Escape — never clicking an option, so
    no value is ever set. One browser session, one page load, opened
    sequentially so only one listbox is ever open at a time.

    `queries` is a list of {'label': str, 'type_text': str|None}. When
    type_text is given, it is typed into the (now-focused) trigger before
    reading options — for type-ahead comboboxes where the full option list
    is only populated after filtering (e.g. a long school picker). Typing a
    search/filter string here is an inspection action, not personal data
    entry — no option is ever selected."""
    results = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_page()
        try:
            page.goto(url, timeout=timeout)
            for query in queries:
                label = query['label']
                type_text = query.get('type_text')
                trigger = page.get_by_role('combobox', name=label, exact=True)
                if trigger.count() == 0:
                    results[label] = {'error': 'No combobox found with this exact accessible name.'}
                    continue
                trigger.first.click()
                if type_text:
                    page.keyboard.type(type_text, delay=20)
                page.wait_for_timeout(400)  # let the listbox render/populate/filter
                options = page.locator('[role="option"]')
                visible_texts = []
                for i in range(options.count()):
                    opt = options.nth(i)
                    if opt.is_visible():
                        text = opt.inner_text().strip()
                        if text:
                            visible_texts.append(text)
                page.keyboard.press('Escape')
                page.wait_for_timeout(100)
                entry = {'options': visible_texts}
                if type_text:
                    entry['typed'] = type_text
                results[label] = entry
        finally:
            browser.close()
    return results


def read_validation_errors(page):
    errors = page.locator('.error.visible')
    count = errors.count()
    return [errors.nth(i).inner_text().strip() for i in range(count) if errors.nth(i).inner_text().strip()]


def _page_transitioned(page, url_before, next_page_plan):
    """Positive evidence that a Next click actually moved to a new page,
    rather than being silently absorbed (client/native validation with no
    recognized .error.visible element, a disabled/no-op button, etc.). A
    plan page may declare "landmark_text": a string expected to be visible
    once that page is genuinely reached -- required for a single-page
    application whose URL never changes; for a real multi-page site the
    URL changing is normally sufficient on its own. With neither a URL
    change nor a declared landmark to check, this deliberately returns
    False rather than assuming success -- a real, measured bug (caught by
    an independent audit) treated an unchanged URL as sufficient proof the
    plan's next page (potentially the review page) had been reached."""
    if page.url != url_before:
        return True
    landmark = (next_page_plan or {}).get('landmark_text')
    if landmark:
        try:
            return page.get_by_text(landmark, exact=False).count() > 0
        except Exception:
            return False
    return False


def read_current_value(page, step):
    """Best-effort read of a field's CURRENT live value, independent of
    whatever a fill step logged at the moment it acted — used for a final,
    whole-form verification pass, since a saved status/action-log entry is
    not proof of what the live page actually shows right now."""
    action = step['action']
    label = step.get('label')
    if action == 'upload':
        # Deliberately not handled here — see verify_filled_fields. A file
        # input reporting files.length > 0 is NOT proof of a successful
        # upload (measured on a real page: the input can hold a file while
        # the widget's own handler has already failed) and the original
        # selector/label may not even resolve anymore by the time this
        # final pass runs, since a real widget can replace its own DOM
        # after a successful selection.
        raise NotImplementedError('upload is verified directly in verify_filled_fields, not via read_current_value')
    if action == 'fill':
        return locate_field(page, step).input_value()
    if action == 'select':
        # A real, measured bug (caught by an independent audit): this
        # branch was missing entirely for 'select' (a native <select>),
        # so the function fell through to `return None` below -- a
        # CORRECTLY selected native option was then always reported as a
        # final-verification failure (None != the intended value), no
        # matter what the page actually showed. Playwright's
        # input_value() on a <select> element returns the selected
        # option's value attribute, matching what select_option() set.
        #
        # That fix alone is still not sufficient -- a second, separate
        # bug (measured live on a real UKG/UltiPro Education section):
        # Locator.select_option() called with a bare string first tries
        # matching the option's VALUE attribute, and only falls back to
        # matching its LABEL if no value matches. A month dropdown whose
        # options use "5" for May, or a Major dropdown whose options use
        # internal GUIDs, both select correctly via the label fallback --
        # but input_value() alone then returns "5" or a GUID, which never
        # equals the human-readable label the plan asked for ("May"),
        # producing a false verification failure even though the right
        # option is genuinely selected. Returning the selected option's
        # own label alongside its value lets the caller accept a match
        # against either representation.
        field = locate_field(page, step)
        return {
            'value': field.input_value(),
            'label': field.evaluate(
                "el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text.trim() : null"),
        }
    if action in ('check', 'uncheck'):
        return locate_field(page, step).is_checked()
    if action == 'select_radio':
        return None  # verify_filled_fields re-checks this directly by option label, not through this generic path
    if action == 'select_combobox':
        return _combobox_current_display_value(page, label)
    return None


def _combobox_current_display_value(page, label, depth=3):
    """Reads a custom combobox's actual current selection. An editable
    trigger's own input_value() is checked first; otherwise looks within a
    few ancestor levels for a LEAF element (no element children) whose own
    TRIMMED text exactly equals a candidate, rather than accepting the
    expected text merely appearing ANYWHERE inside a large ancestor text
    blob (a static finding from an independent audit: that could match an
    unrelated adjacent string, or a lingering, not-actually-selected
    listbox option). Returns the list of exact leaf-text candidates found,
    OR the trigger's own text if it has no leaf descendants -- verify_
    filled_fields treats a match against ANY item in that returned set as
    success, but never a mere substring match against the whole blob."""
    trigger = page.get_by_role('combobox', name=label, exact=True)
    tag = trigger.evaluate('el => el.tagName.toLowerCase()')
    if tag in ('input', 'textarea'):
        value = trigger.input_value()
        if value:
            return [value]
    leaves = trigger.evaluate(
        '''(el, depth) => {
            let node = el;
            for (let i = 0; i < depth && node.parentElement; i++) node = node.parentElement;
            const out = [];
            const walker = document.createTreeWalker(node, NodeFilter.SHOW_ELEMENT);
            let n;
            while ((n = walker.nextNode())) {
                if (n.children.length === 0) {
                    const t = (n.innerText || n.textContent || '').trim();
                    if (t) out.push(t);
                }
            }
            return out;
        }''', depth)
    if leaves:
        return leaves
    own_text = (trigger.inner_text() or '').strip()
    return [own_text] if own_text else []


_UPLOAD_FAILURE_NEAR_FILENAME = re.compile(
    r'\b(fail(?:ed|ure)?|error|try again|could not|cannot|unable to|rejected|invalid file|not (?:supported|accepted))\b',
    re.IGNORECASE)

_UPLOAD_PENDING_LANGUAGE = re.compile(r'\b(uploading|upload in progress|please wait|processing)\b', re.IGNORECASE)

_ATTACHMENT_TAG_ATTR = 'data-apply-attachment-index'


def find_attachment_container(page, label_hint, scope=None):
    """PREPARED, NOT YET DEPLOYED (written 2026-10-04; requires a worker
    restart to take effect, which was deliberately not done to preserve a
    live session -- see read_attachment_state below for the exact gap this
    closes).

    Locates the SINGLE scoped container for one attachment widget (e.g.
    "Resume" or "Cover Letter"), keyed by its own visible section label --
    never by the file <input>'s id, which is dynamically generated and
    changes between page loads (measured on this same live page:
    FileUpload_3j8h5wxhql one session, FileUpload_cq6iwczoj8 the next) and
    which can disappear from the DOM entirely after a real widget replaces
    itself post-upload (also measured, same page: the #resume input was
    gone from a later inspect even though the earlier upload had verified
    ok). Scoping by the widget's own stable section text survives both.

    Same TreeWalker discipline as find_question_container, but does NOT
    require a specific control inside the container -- unlike a yes/no
    question, a COMPLETED attachment widget often shows only a filename and
    a remove/replace control, no file <input> at all.

    Checks uniqueness across the WHOLE scope before resolving anything: if
    label_hint's own text appears on more than one visible leaf node, this
    returns empty rather than silently picking whichever one document order
    happened to reach first.

    Returns a list of Locators (normally 0 or 1); a caller should refuse to
    act -- never guess -- when it isn't exactly 1."""
    js = '''(root, [labelHint, attr]) => {
        root.querySelectorAll('[' + attr + ']').forEach(el => el.removeAttribute(attr));
        const escape = s => s.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
        // Word-boundary wrapped: a bare substring match caught itself on its
        // own test fixture (measured during local testing, 2026-10-04) --
        // the heading "Resume" matched case-insensitively INSIDE a filename
        // like "my_resume.pdf" sitting in the same container, inflating the
        // match count and triggering the overshoot guard on a perfectly
        // valid, uniquely-resolvable container. Real resume filenames
        // routinely contain the word "resume"; \\b prevents that collision
        // in the common case (word characters including "_" extend through
        // the boundary, so "my_resume" never falsely matches "resume" alone).
        const hintRe = new RegExp('\\\\b' + escape(labelHint) + '\\\\b', 'i');
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
        let node, leaves = [];
        while ((node = walker.nextNode())) {
            const own = Array.from(node.childNodes)
                .filter(n => n.nodeType === Node.TEXT_NODE)
                .map(n => n.textContent).join(' ');
            if (hintRe.test(own) && node.offsetParent !== null) { leaves.push(node); }
        }
        // Refuse on EITHER zero or more than one match across the whole
        // scope -- two separate attachment widgets sharing a heading (e.g.
        // "Resume" and a duplicate upload section) must not silently
        // resolve to whichever one the walker reaches first. A caller
        // seeing 0 containers back must treat that identically to "I don't
        // know", never infer which section was meant.
        if (leaves.length !== 1) return;
        let container = leaves[0];
        const leaf = leaves[0];
        for (let depth = 0; depth < 15 && container && container !== root.parentElement; depth++) {
            const text = container.innerText || '';
            const count = (text.match(new RegExp(hintRe.source, 'gi')) || []).length;
            if (count === 1 && container !== leaf) { container.setAttribute(attr, '1'); return; }
            if (count > 1) return;  // overshot into a neighboring widget's territory
            if (container === root) return;
            container = container.parentElement;
        }
    }'''
    target = scope.first if scope is not None else page.locator('body')
    target.evaluate(js, [label_hint, _ATTACHMENT_TAG_ATTR])
    tagged = target.locator(f'[{_ATTACHMENT_TAG_ATTR}]')
    return [tagged.nth(i) for i in range(tagged.count())]


def read_attachment_state(page, label_hint, expected_filename=None):
    """PREPARED, NOT YET DEPLOYED -- see find_attachment_container above.

    Read-only: the committed state of ONE attachment widget, scoped to its
    own container text -- never a page-wide or uncapped-body search. This is
    the read-only counterpart the upload check never had: verify_filled_
    fields' upload branch only runs immediately after an actual upload
    attempt inside the SAME fill_steps call, so there was previously no way
    to independently re-check an attachment's state later without
    re-uploading (which risks attaching a second time, and does not prove
    anything about what the FIRST attempt actually left behind).

    Directly closes a measured, reported gap: inspect's body_sample is
    page.locator('body').inner_text()[:3000], and on a real page where the
    job DESCRIPTION precedes the application form (measured: Momentum req
    8608934002, the resume widget sits well past the first 3000 characters,
    entirely consumed by company/role boilerplate), that cap structurally
    cannot reach the attachment widget at all -- not a rare edge case.

    Returns ok=True only when the expected filename is present in the
    widget's OWN scoped text AND no failure or pending-upload language
    appears in that same scope. Refuses (ok=False, inconclusive=True)
    rather than guess when the container isn't uniquely resolved."""
    containers = find_attachment_container(page, label_hint)
    if len(containers) != 1:
        return {'ok': False, 'inconclusive': True,
                'reason': 'expected exactly 1 container for %r, found %d' % (label_hint, len(containers))}
    text = containers[0].inner_text()[:2000]
    filename_present = (expected_filename in text) if expected_filename else None
    failure = bool(_UPLOAD_FAILURE_NEAR_FILENAME.search(text))
    pending = bool(_UPLOAD_PENDING_LANGUAGE.search(text))
    return {
        'ok': (filename_present is not False) and not failure and not pending,
        'inconclusive': False,
        'container_text': text,
        'expected_filename_present': filename_present,
        'failure_language_present': failure,
        'pending_upload_language_present': pending,
    }


def verify_filled_fields(page, page_plan, profile, answer_bank):
    """Re-reads every approved, non-skipped field from the LIVE page after
    the whole fill sequence has finished, and compares against the expected
    resolved value. This is the final, authoritative check this module
    makes — a saved 'awaiting_review' status or an earlier per-step check
    is not treated as proof the form is actually filled right now."""
    results = []
    for step in page_plan.get('steps', []):
        label = step.get('label') or step.get('question')
        if step.get('skip_approved'):
            results.append({'label': label, 'expected': None, 'ok': True, 'note': 'intentionally left blank'})
            continue
        if step['action'] == 'select_radio':
            # A real, measured bug (caught by an independent audit): this
            # step was previously skipped from the final audit entirely
            # ("already verified at select time"), meaning a radio option
            # that later got reset (e.g. by a subsequent step on the same
            # page, a client-side reset, or a re-render) would never be
            # caught here. Re-checks the actual live is_checked() state,
            # not the earlier apply_step-time result.
            question = step.get('question', label or 'radio group')
            value, missing = resolve_value(profile, step.get('value'), answer_bank)
            if missing:
                results.append({'label': question, 'expected': None, 'ok': False, 'note': 'value could not be resolved'})
                continue
            option_label = (step.get('options') or {}).get(str(value))
            if option_label is None:
                results.append({'label': question, 'expected': str(value), 'ok': False,
                                 'note': 'no option label mapped for this value in the plan'})
                continue
            try:
                actual_checked = page.get_by_label(option_label, exact=True).is_checked()
            except Exception as e:
                results.append({'label': question, 'expected': option_label, 'ok': False, 'note': 'read failed: %s' % e})
                continue
            results.append({'label': question, 'expected': option_label,
                             'actual': 'checked' if actual_checked else 'not checked', 'ok': actual_checked})
            continue
        value, missing = resolve_value(profile, step.get('value'), answer_bank)
        if missing:
            results.append({'label': label, 'expected': None, 'ok': False, 'note': 'value could not be resolved'})
            continue
        if step['action'] == 'upload':
            # Checked against the whole page's visible text, not the
            # original field selector — a real upload widget can replace
            # its own inner DOM after success, so the original locator may
            # no longer resolve by the time this final pass runs. A file
            # input merely holding a file is explicitly NOT accepted as
            # proof (that is the exact false-success case this guards
            # against — see docs/APPLY_VALIDATION.md and
            # tests/test_apply.py's upload-verification regression test).
            filename = Path(str(value)).name
            try:
                page_text = page.locator('body').inner_text()
            except Exception as e:
                results.append({'label': label, 'expected': filename, 'ok': False, 'note': 'read failed: %s' % e})
                continue
            idx = page_text.find(filename)
            if idx == -1:
                ok, note = False, 'filename not found anywhere on the page'
            else:
                # A real, measured bug (caught by an independent audit):
                # the filename appearing ANYWHERE in the body text
                # (including inside an error message that happens to
                # repeat it, e.g. "Upload failed: resume.pdf. Please try
                # again.") was previously accepted as proof of success.
                # Check the text immediately around the filename for a
                # failure-indicating phrase before accepting it -- scoped
                # to the filename's own line plus one line either side
                # (inner_text() breaks roughly at block-element
                # boundaries), NOT a raw character window, which can reach
                # into unrelated static page text several paragraphs away
                # (measured: a fixture's own instructional copy describing
                # what a failure looks like sat close enough in the full
                # body text to trigger a false negative on a genuinely
                # successful upload).
                lines = page_text.split('\n')
                line_idx, running = None, 0
                for i, line in enumerate(lines):
                    if running <= idx < running + len(line) + 1:
                        line_idx = i
                        break
                    running += len(line) + 1
                if line_idx is None:
                    nearby = page_text[max(0, idx - 80):idx + len(filename) + 80]
                else:
                    nearby = '\n'.join(lines[max(0, line_idx - 1):line_idx + 2])
                if _UPLOAD_FAILURE_NEAR_FILENAME.search(nearby):
                    ok, note = False, 'filename found, but near a failure-indicating phrase — not treated as a successful upload: %r' % nearby
                else:
                    ok, note = True, None
            results.append({'label': label, 'expected': filename,
                             'actual': 'shown on page' if ok else 'not shown', 'ok': ok, 'note': note})
            continue
        try:
            actual = read_current_value(page, step)
        except Exception as e:
            results.append({'label': label, 'expected': str(value), 'ok': False, 'note': 'read failed: %s' % e})
            continue
        if step['action'] == 'select_combobox':
            candidates = actual or []
            ok = str(value) in candidates
            actual_report = str(value) if ok else (candidates[0] if candidates else '')
            results.append({'label': label, 'expected': str(value), 'actual': actual_report, 'ok': ok})
            continue
        elif step['action'] == 'select':
            actual_value = actual.get('value') if isinstance(actual, dict) else actual
            actual_label = actual.get('label') if isinstance(actual, dict) else None
            ok = str(value) == actual_value or (actual_label is not None and str(value) == actual_label)
            results.append({'label': label, 'expected': str(value), 'actual_value': actual_value,
                             'actual_label': actual_label, 'ok': ok})
            continue
        elif step['action'] == 'check':
            ok = actual == bool(value)
        elif step['action'] == 'uncheck':
            ok = actual is False
        else:
            ok = actual == str(value)
            note = None
            if not ok and step.get('format_normalization') == 'phone':
                # A site's own input mask can reformat what was typed (e.g. a
                # phone field turning "555.401.0502" into "(555) 401-0502")
                # without changing the underlying value — measured on this
                # real page. Only treat this as equivalent when the digits
                # themselves match; a genuinely wrong value still fails.
                # Scoped to steps the plan explicitly marks as a phone field
                # -- a real, measured bug (caught by an independent audit)
                # applied this digit-only comparison to EVERY field
                # unconditionally, which meant a GPA of "3.2" was falsely
                # accepted against a page showing "32" (both normalize to
                # the same digit string "32") purely because the field
                # happened to fail the exact-text comparison.
                expected_digits = re.sub(r'\D', '', str(value))
                actual_digits = re.sub(r'\D', '', str(actual))
                if expected_digits and expected_digits == actual_digits:
                    ok = True
                    note = 'matched after normalizing formatting (site reformatted punctuation; digits are identical)'
            results.append({'label': label, 'expected': str(value), 'actual': str(actual), 'ok': ok, 'note': note})
            continue
        results.append({'label': label, 'expected': str(value), 'actual': str(actual), 'ok': ok})
    return results


# 'paused_for_review' was a real, measured omission (caught by an
# independent audit): if a process holding at a pause_after_fill
# checkpoint died (crash, kill, machine restart) before a resume signal
# arrived, a later `fill` invocation computed resume_index=0 (this status
# wasn't in the tuple) and silently restarted from start_url — exactly the
# "silent restart" this whole module exists to prevent.
RESUMABLE_STATUSES = ('blocked_validation', 'error', 'in_progress', 'awaiting_review', 'paused_for_review',
                       'handoff_safety_cap_reached')


def browser_profile_dir(run_key):
    return RUN_STATE_DIR / (run_key + '_browser_profile')


def worker_command_path(run_key):
    return RUN_STATE_DIR / (run_key + '_worker_command.json')


def worker_result_path(run_key):
    return RUN_STATE_DIR / (run_key + '_worker_result.json')


WORKER_COMMANDS = ('goto', 'inspect', 'read_fields', 'read_attachment_state', 'fill_steps',
                    'fix_field_by_current_value', 'answer_question', 'click_next', 'pause', 'status', 'close')


def send_worker_command(run_key, command, timeout_seconds=120, poll_seconds=0.5):
    """External API for driving an ALREADY-RUNNING run_worker process:
    writes a command file for it to pick up on its next poll, then waits
    for the matching result file and returns its contents. Distinct
    commands can be sent repeatedly over the worker's whole lifetime --
    inspect, then fill_steps, then click_next, then another fill_steps,
    then pause for a human handoff, then more fill_steps, then close --
    all against the SAME live browser/page. This is the direct answer to a
    real, independently-found gap: a login-then-fill-later handoff (e.g.
    signing in, then filling a form whose fields are only known AFTER
    login) previously had no way to continue filling in the SAME browser
    that owns the authenticated session -- only a close signal was
    accepted once paused, and a separate `fill` invocation would launch a
    brand-new browser context, which is not a continuation of anything."""
    if command.get('cmd') not in WORKER_COMMANDS:
        raise ValueError('Unknown worker command: %r (must be one of %s)' % (command.get('cmd'), WORKER_COMMANDS))
    worker_result_path(run_key).unlink(missing_ok=True)
    write_json(worker_command_path(run_key), command)
    waited = 0.0
    result_path = worker_result_path(run_key)
    while not result_path.exists():
        time.sleep(poll_seconds)
        waited += poll_seconds
        if waited >= timeout_seconds:
            raise TimeoutError('No response from the worker for run_key %r within %ss -- is it running? '
                                'Check with `python apply.py worker-status %s`.' % (run_key, timeout_seconds, run_key))
    result = read_json(result_path)
    result_path.unlink(missing_ok=True)
    return result


def run_worker(run_key, profile_path, start_url=None, headless=True, answer_bank_path=None,
                idle_safety_cap_seconds=4 * 3600, poll_seconds=1, pause_hook=None):
    acquire_run_lock(run_key)
    try:
        return _run_worker_locked(run_key, profile_path, start_url=start_url, headless=headless,
                                   answer_bank_path=answer_bank_path,
                                   idle_safety_cap_seconds=idle_safety_cap_seconds, poll_seconds=poll_seconds,
                                   pause_hook=pause_hook)
    finally:
        release_run_lock(run_key)


def _run_worker_locked(run_key, profile_path, start_url=None, headless=True, answer_bank_path=None,
                        idle_safety_cap_seconds=4 * 3600, poll_seconds=1, pause_hook=None):
    """One browser-owning process for the ENTIRE life of an application,
    accepting a SEQUENCE of commands from send_worker_command against the
    SAME live page -- never closing or relaunching between them, and never
    navigating to a "remembered" URL to fake a continuation. A command
    that fails (a field not found, a bad selector, an unexpected page
    state) is caught, recorded in THAT command's own result, and the
    worker loop CONTINUES running -- it does not exit or close the
    browser. The caller can send `inspect` to see the live page's real
    current state and retry with corrected information: a recoverable
    error is resumable after re-inspection, not a dead end. Only an
    explicit `close` command (or the idle safety cap, a crash-guard, never
    a short timeout) ends the loop."""
    profile = read_json(profile_path)
    answer_bank = answers.load_bank(answer_bank_path) if answer_bank_path else None
    state = load_run_state(run_key)
    profile_dir = browser_profile_dir(run_key)
    profile_dir.mkdir(parents=True, exist_ok=True)
    command_path = worker_command_path(run_key)
    command_path.unlink(missing_ok=True)
    worker_result_path(run_key).unlink(missing_ok=True)

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(str(profile_dir), headless=headless)
        page = context.pages[0] if context.pages else context.new_page()
        if start_url:
            page.goto(start_url)
            state['current_url'] = page.url
            log_action(state, 'worker opened ' + page.url)
        state['status'] = 'worker_running'
        save_run_state(state)

        idle = 0
        try:
            while True:
                if not command_path.exists():
                    page.wait_for_timeout(poll_seconds * 1000)
                    idle += poll_seconds
                    if idle >= idle_safety_cap_seconds:
                        state['status'] = 'handoff_safety_cap_reached'
                        state['issues'] = ['Worker idle safety cap reached with no commands for %ss '
                                           '(crash-guard, not a site error) -- resumable via a new worker '
                                           'pointed at the same run_key.' % idle_safety_cap_seconds]
                        log_action(state, state['issues'][0])
                        break
                    continue
                idle = 0
                try:
                    command = read_json(command_path)
                except Exception:
                    command_path.unlink(missing_ok=True)
                    continue
                command_path.unlink(missing_ok=True)
                cmd = command.get('cmd')
                result = {'cmd': cmd, 'ok': True}
                if cmd in ('fill_steps', 'click_next') and state.get('status') == 'session_lost':
                    # An actual lost session must stop further writes, not
                    # just be reported once and then ignored on the next
                    # command. Explicit override: a fresh 'pause' (with or
                    # without a session_check) that leaves status something
                    # other than session_lost clears this.
                    result['ok'] = False
                    result['error'] = ('refusing %r: worker status is session_lost (see run_state issues) -- '
                                        'send an explicit "pause" (to re-verify) or "inspect" first, never blind '
                                        'writes after a reported session loss' % cmd)
                    write_json(worker_result_path(run_key), result)
                    continue
                try:
                    if cmd == 'goto':
                        page.goto(command['url'])
                        state['current_url'] = page.url
                        log_action(state, 'worker navigated to ' + page.url)
                        result['url'] = page.url
                    elif cmd == 'inspect':
                        result['inspection'] = page.evaluate(_INSPECT_JS)
                        result['url'] = page.url
                        result['body_sample'] = page.locator('body').inner_text()[:3000]
                    elif cmd == 'read_fields':
                        # Read-only: reports each requested field's CURRENT
                        # live value (and, for a native <select>, its
                        # selected LABEL alongside the raw value -- see
                        # read_current_value) WITHOUT performing any fill,
                        # select, check, or click. A genuine precondition
                        # check -- e.g. "did this survive a restart?" --
                        # must never itself be a write.
                        steps = command.get('steps', [])
                        values = []
                        for step in steps:
                            entry = {'label': step.get('label') or step.get('question'), 'action': step.get('action')}
                            try:
                                entry['current'] = read_current_value(page, step)
                            except Exception as e:
                                entry['error'] = str(e)
                            values.append(entry)
                        result['values'] = values
                    elif cmd == 'read_attachment_state':
                        # Read-only, scoped to the ONE named attachment
                        # widget's own container text -- never a page-wide
                        # or uncapped-body search. See read_attachment_state's
                        # own docstring for the exact gap this closes.
                        result.update(read_attachment_state(
                            page, command['label_hint'], command.get('expected_filename')))
                    elif cmd == 'fill_steps':
                        steps = command.get('steps', [])
                        for step in steps:
                            apply_step_with_retries(page, step, profile, state, answer_bank)
                        result['verification'] = verify_filled_fields(page, {'steps': steps}, profile, answer_bank)
                        result['field_errors'] = list(state.get('field_errors', []))
                    elif cmd == 'fix_field_by_current_value':
                        # For a repeating block with NO unique per-field ids
                        # (measured on this real UltiPro page -- unlike
                        # Avature's "remove"-labeled blocks, these have no
                        # matchable per-block anchor text at all), the
                        # SAFEST way to correct one specific wrong value
                        # (e.g. a resume-parser garbled job title) is to
                        # locate it by its own CURRENT, already-confirmed
                        # wrong content -- never by position, which is
                        # exactly the class of bug find_block_containers
                        # exists to prevent elsewhere. Refuses if zero or
                        # more than one field currently contains the given
                        # substring, so this can never silently touch the
                        # wrong instance.
                        contains = command['contains']
                        new_value = command['new_value']
                        # A CSS/role locator's own .filter(has_text=...)
                        # matches rendered text content, which does NOT
                        # include an <input>/<textarea>'s current value --
                        # read each candidate's real input_value() instead.
                        handles = page.locator('input[type="text"], textarea').element_handles()
                        found = [h for h in handles if (h.input_value() or '').find(contains) != -1]
                        result['match_count'] = len(found)
                        if len(found) != 1:
                            raise RuntimeError('Refusing: expected exactly 1 field currently containing %r, found %d'
                                                % (contains, len(found)))
                        target = found[0]
                        before = target.input_value()
                        target.fill(new_value)
                        after = target.input_value()
                        result['before'] = before
                        result['after'] = after
                        result['ok'] = (after == new_value)
                        log_action(state, 'worker fixed one field (matched by its current wrong value %r) -> %r'
                                   % (contains[:60], new_value[:60]))
                    elif cmd == 'answer_question':
                        # Targets a yes/no or multiple-choice question by
                        # its own REAL TEXT, never an internal control
                        # name or field position -- see
                        # find_question_container's docstring for the
                        # real, measured case this exists for. Verifies
                        # the answer within the SAME scoped container
                        # before reporting success.
                        question_text = command['question_text']
                        answer_label = command['answer_label']
                        scope_selector = command.get('scope_selector')
                        scope = page.locator(scope_selector) if scope_selector else None
                        containers = find_question_container(page, question_text, scope=scope)
                        result['container_count'] = len(containers)
                        if len(containers) != 1:
                            raise RuntimeError('Refusing: expected exactly 1 container for question %r, found %d'
                                                % (question_text, len(containers)))
                        container = containers[0]
                        answer_result = answer_radio_question(container, answer_label)
                        result.update(answer_result)
                        result['verification'] = read_question_container_state(container)
                        log_action(state, 'worker answered question (scoped, verified) %r -> %r: ok=%s'
                                   % (question_text[:60], answer_label, result['ok']))
                    elif cmd == 'click_next':
                        label = command.get('label', 'Next')
                        if is_denied_button(label):
                            raise RuntimeError('Refusing to click a submit-like button: ' + label)
                        url_before = page.url
                        page.get_by_role('button', name=label, exact=True).click()
                        landmark = command.get('landmark_text')
                        result['validation_errors'] = read_validation_errors(page)
                        result['transitioned'] = _page_transitioned(
                            page, url_before, {'landmark_text': landmark} if landmark else None)
                        result['url'] = page.url
                        state['current_url'] = page.url
                        log_action(state, 'worker clicked "%s" -- transitioned=%s' % (label, result['transitioned']))
                    elif cmd == 'pause':
                        reason = command.get('reason', 'manual review/handoff')
                        signal_path = resume_signal_path(run_key)
                        signal_path.unlink(missing_ok=True)
                        state['status'] = 'paused_for_review'
                        log_action(state, 'worker pausing (%s) -- run `python apply.py resume %s` (or create %s) '
                                   'to continue' % (reason, run_key, signal_path))
                        save_run_state(state)
                        if pause_hook:
                            # Test-only synchronous injection point, run on
                            # the worker's OWN thread right before the wait
                            # begins -- lets a test simulate a human
                            # editing the live page during the pause
                            # deterministically. Never set outside tests.
                            pause_hook(page)
                        result['waited_seconds'] = wait_for_handoff_signal(page, signal_path, poll_seconds=2)
                        # A manual handoff is a point where the session
                        # could have been lost server-side. When the
                        # caller supplies session_check, verify the LIVE
                        # page before reporting resumed=alive -- an actual
                        # lost session must stop further writes and be
                        # reported, never silently treated as fine.
                        session_check = command.get('session_check')
                        if session_check:
                            alive, reason_lost = check_session_alive(page, session_check)
                            result['session_alive'] = alive
                            if not alive:
                                result['session_lost_reason'] = reason_lost
                                state['status'] = 'session_lost'
                                state['issues'] = [reason_lost]
                                log_action(state, 'SESSION LOST after worker pause: %s' % reason_lost)
                            else:
                                state['status'] = 'worker_running'
                        else:
                            state['status'] = 'worker_running'
                        log_action(state, 'worker resumed after %ss pause' % result['waited_seconds'])
                    elif cmd == 'status':
                        result['url'] = page.url
                        result['run_state'] = dict(state)
                        # Read-only visibility, not a guardrail change: a
                        # click can open a NEW tab/window (common on ATS
                        # "Apply" flows) that this worker's own `page`
                        # variable does not automatically follow. Lists
                        # every open tab's URL so a stale single-page view
                        # is never mistaken for "nothing happened."
                        try:
                            result['all_open_tabs'] = [p.url for p in context.pages]
                        except Exception:
                            pass
                    elif cmd == 'close':
                        result['closing'] = True
                    else:
                        result['ok'] = False
                        result['error'] = 'unknown command: %r' % cmd
                except Exception as e:
                    result['ok'] = False
                    result['error'] = str(e)
                    log_action(state, 'worker command %r failed (worker stays alive, ready for the next '
                               'command): %s' % (cmd, e))
                save_run_state(state)
                write_json(worker_result_path(run_key), result)
                if cmd == 'close':
                    log_action(state, 'worker received explicit close command -- exiting and closing the browser')
                    state['status'] = 'worker_closed'
                    break
        finally:
            save_run_state(state)
            context.close()
    return state


def _has_unresolved_verification_failures(state):
    """True if the saved state has ANY recorded field_errors, OR any
    final_verification/page_verifications entry with ok=False -- used to
    decide whether a saved 'awaiting_review' status can actually be
    trusted without a fresh live check. Checking field_errors alone missed
    the single-page stop_after_fill path's own final_verification list and
    the multi-page path's page_verifications dict, both of which can carry
    a real, recorded ok=False with field_errors staying empty."""
    if state.get('field_errors'):
        return True
    if any(not v.get('ok', True) for v in (state.get('final_verification') or [])):
        return True
    for results in (state.get('page_verifications') or {}).values():
        if any(not v.get('ok', True) for v in results):
            return True
    return False


def _hold_for_close_signal(page, run_key, state):
    """Blocks with the browser/page fully alive until the human explicitly
    signals they are completely done with this window (close_signal_path).
    Used only at a terminal hold — review page reached, blocked_validation,
    error, or session_lost — and only when hold_open was requested. Never
    triggered by resume_signal_path; see that function's docstring for the
    real incident this separation fixes."""
    signal_path = close_signal_path(run_key)
    signal_path.unlink(missing_ok=True)
    log_action(state, 'holding the browser open — run `python apply.py close %s` (or create %s) once you are '
               'completely done with this window' % (run_key, signal_path))
    save_run_state(state)
    waited = wait_for_handoff_signal(page, signal_path, poll_seconds=2)
    log_action(state, 'close signal received after %ds — this process will now exit and close the browser' % waited)
    save_run_state(state)


def run_fill(run_key, plan_path, profile_path, headless=True, answer_bank_path=None,
             job_key=None, radar_state_path=None, hold_open=False, run_authorization=None,
             force_refill=False, pause_hook=None):
    """Drives an application from start_url through every plan page inside
    ONE continuous Playwright session — one browser-owning process, one
    page, alive for the entire run, including across any pause. A plan page
    may set "pause_after_fill": true to deliberately stop for a human edit
    mid-application (not just at page boundaries the old design assumed
    were the only meaningful checkpoint); the wait blocks this same
    process/page rather than exiting and relying on a later process
    re-attaching. hold_open additionally keeps the process (and therefore
    the visible browser) open at a terminal state — review page reached,
    blocked_validation, error, session_lost — until the human sends an
    explicit close signal; without hold_open those states return (and the
    browser closes) immediately, as before, for headless/CI-style runs.

    Three things this function deliberately keeps distinct, per a real
    incident where conflating them caused data loss: (1) cookies/localStorage
    in the on-disk browser profile, (2) this module's own run_state JSON —
    what WE believe happened — and (3) what the remote server actually
    still has saved. Only (3), read live from the page via
    check_session_alive/verify_filled_fields, is ever treated as proof of
    anything; (1) and (2) are inputs to a resume attempt, never proof it
    will work."""
    acquire_run_lock(run_key)
    try:
        return _run_fill_locked(run_key, plan_path, profile_path, headless=headless,
                                 answer_bank_path=answer_bank_path, job_key=job_key,
                                 radar_state_path=radar_state_path, hold_open=hold_open,
                                 run_authorization=run_authorization, force_refill=force_refill,
                                 pause_hook=pause_hook)
    finally:
        release_run_lock(run_key)


def _run_fill_locked(run_key, plan_path, profile_path, headless=True, answer_bank_path=None,
                      job_key=None, radar_state_path=None, hold_open=False, run_authorization=None,
                      force_refill=False, pause_hook=None):
    plan = load_plan(plan_path, run_authorization=run_authorization)
    profile = read_json(profile_path)
    answer_bank = answers.load_bank(answer_bank_path) if answer_bank_path else None
    state = load_run_state(run_key)
    radar_state_path = radar_state_path or RADAR_STATE_PATH
    headed = not headless

    if run_authorization:
        state['run_authorization'] = {
            'text': run_authorization,
            'recorded_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'plan_file_approved_flag': bool(plan.get('approved')),
        }

    if job_key:
        state['job_key'] = job_key
        if already_applied(job_key, radar_state_path):
            state['status'] = 'blocked_duplicate'
            log_action(state, 'refusing to start: job_key %s is already recorded as APPLIED in %s '
                       '(radar.py state) — preventing a duplicate application' % (job_key, radar_state_path))
            save_run_state(state)
            return state

    if state.get('status') == 'awaiting_review' and not _has_unresolved_verification_failures(state) and not force_refill:
        # Already at the human review gate with nothing outstanding —
        # automation has nothing further to do until a new run_key or a
        # real submission. But if field_errors OR a saved final_verification/
        # page_verifications entry shows ok=False, this "awaiting_review"
        # was reached with some fields still failed (e.g. a bug since
        # fixed), or force_refill says the saved status cannot be trusted
        # (e.g. the human reports the tab/browser was closed) — fall
        # through and redo the fill rather than silently returning stale
        # state that may no longer reflect the live page. A real, measured
        # bug (caught by an independent audit) checked field_errors only,
        # so a saved awaiting_review with a failed final_verification but
        # an empty field_errors list was trusted forever on every later
        # call, with no live check ever performed again.
        return state

    if state.get('status') == 'session_lost' and not force_refill:
        # The previous run explicitly detected and reported a lost session
        # (see check_session_alive below). Silently falling through to
        # start_url here would be exactly the "silent restart" the human
        # explicitly prohibited — require an explicit, human-authorized
        # --force-refill (or a fresh run_key) instead of guessing.
        log_action(state, 'refusing to start: last run ended with session_lost (see issues) — this requires '
                   'an explicit --force-refill after a human decides how to proceed, not an automatic retry')
        save_run_state(state)
        return state

    if force_refill:
        # The saved page_index/current_url may point at a tab that no
        # longer exists — always restart from start_url rather than trying
        # to "resume" a URL nothing can confirm is still open.
        state['page_index'] = 0
        state['current_url'] = None
        log_action(state, 'force_refill requested — treating saved state as unverified and redoing the fill '
                   'from start_url rather than trusting it')

    missing = collect_missing_questions(plan, profile, answer_bank)
    if missing:
        state['status'] = 'paused_missing_info'
        state['missing_questions'] = missing
        log_action(state, 'paused before opening browser: %d missing profile answer(s)' % len(missing))
        save_run_state(state)
        return state

    # Resume at the page we last stopped on, using the same on-disk browser
    # profile, so cookies/localStorage (and any manual login) survive across
    # separate `fill` invocations instead of restarting from page one. This
    # is a best-effort input, not proof the server still honors the session
    # — check_session_alive below verifies the live page before trusting it.
    resume_index = state.get('page_index', 0) if state.get('status') in RESUMABLE_STATUSES else 0
    resume_url = state.get('current_url') if resume_index > 0 else None

    state['status'] = 'in_progress'
    state['missing_questions'] = []
    state['issues'] = []
    state['field_errors'] = []

    profile_dir = browser_profile_dir(run_key)
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(str(profile_dir), headless=headless)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            target_url = resume_url or plan['start_url']
            page.goto(target_url)
            state['current_url'] = page.url
            log_action(state, ('resumed at ' if resume_url else 'opened ') + page.url)

            if resume_url:
                alive, reason = check_session_alive(page, plan.get('resume_session_check'))
                if not alive:
                    state['status'] = 'session_lost'
                    state['issues'] = [reason]
                    log_action(state, 'SESSION LOST on resume: %s — refusing to fill or navigate further; '
                               'reporting this explicitly rather than silently restarting or overwriting '
                               'whatever is actually on this page' % reason)
                    if hold_open and headed:
                        _hold_for_close_signal(page, run_key, state)
                    return state

            for page_index, page_plan in enumerate(plan['pages']):
                if page_index < resume_index:
                    continue
                state['page_index'] = page_index

                if page_plan.get('is_review_page'):
                    state['status'] = 'awaiting_review'
                    log_action(state, 'reached review page; stopping before any submit action per guardrail')
                    if hold_open and headed:
                        _hold_for_close_signal(page, run_key, state)
                    return state

                for step in page_plan.get('steps', []):
                    apply_step_with_retries(page, step, profile, state, answer_bank)

                page_name = page_plan.get('name', 'page %d' % page_index)
                verification = []
                if page_plan.get('steps'):
                    # Verify complete records after this page's changes are
                    # done, not only immediately after each field's own
                    # apply_step call — re-reads every field from the LIVE
                    # page right now. A saved action-log entry or an
                    # earlier per-field check is not proof of what the page
                    # actually shows at this point (e.g. after a later step
                    # on the same page inadvertently altered an earlier
                    # one). Does not block advancing — it makes any
                    # unresolved field visible in state/logs instead of
                    # letting a page be silently reported as complete.
                    verification = verify_filled_fields(page, page_plan, profile, answer_bank)
                    state.setdefault('page_verifications', {})[page_name] = verification
                    unresolved = [v for v in verification if not v['ok']]
                    if unresolved:
                        log_action(state, 'page "%s" has %d field(s) not verified correct after filling: %s'
                                   % (page_name, len(unresolved), '; '.join(
                                       '%s (expected %r, got %r)' % (v['label'], v.get('expected'), v.get('actual'))
                                       for v in unresolved)))
                    save_run_state(state)

                if page_plan.get('stop_after_fill'):
                    # For a single-page application whose only button is the
                    # final Submit (common on embedded ATS forms, e.g.
                    # Greenhouse) — fields are filled and verified, but this
                    # attempts NO click at all, on this button or any other,
                    # rather than reusing is_review_page's "skip this page's
                    # steps entirely" meaning, which doesn't fit a page that
                    # legitimately needs filling.
                    state['final_verification'] = verification
                    failed = [v for v in verification if not v['ok']]
                    state['status'] = 'awaiting_review'
                    log_action(state, 'filled the single-page form; final verification found %d/%d fields correct'
                               % (len(verification) - len(failed), len(verification)))
                    if failed:
                        log_action(state, 'final verification failures: ' +
                                   '; '.join('%s (expected %r, got %r)' % (f['label'], f.get('expected'), f.get('actual')) for f in failed))
                    try:
                        page.bring_to_front()
                        # Scroll to the Resume/CV section specifically (not
                        # the page's top heading) so the field most likely to
                        # need a human's attention — the upload — is what's
                        # actually on screen when the tab comes to front.
                        anchor = page.get_by_text('Resume/CV', exact=False).first
                        if anchor.count() == 0:
                            anchor = page.get_by_text('Apply for this job', exact=False).first
                        if anchor.count() > 0:
                            anchor.scroll_into_view_if_needed(timeout=5000)
                    except Exception as e:
                        log_action(state, 'could not bring tab to front / scroll to Resume/CV: ' + str(e))
                    log_action(state, 'stopping before any submit action per guardrail')
                    if hold_open and headed:
                        _hold_for_close_signal(page, run_key, state)
                    return state

                if page_plan.get('pause_after_fill'):
                    # A plan-authored checkpoint for a human to review or
                    # correct THIS page's fields before advancing — e.g. a
                    # resume parser's auto-filled values that need a
                    # human's eyes before the Next click locks them in.
                    # Blocks this same page/process; nothing is closed,
                    # reloaded, or re-navigated while waiting. A required
                    # human checkpoint must not silently disappear because
                    # this run happens to be headless -- a real, measured
                    # bug (caught by an independent audit) skipped the
                    # pause entirely when headless, but a human can still
                    # send a resume signal without ever seeing the window
                    # (e.g. via `apply.py resume` from another terminal),
                    # so failing closed here means genuinely remaining
                    # paused, not disappearing.
                    signal_path = resume_signal_path(run_key)
                    signal_path.unlink(missing_ok=True)
                    state['status'] = 'paused_for_review'
                    log_action(state, 'pausing after filling "%s" for manual review/edit — browser stays '
                               'open; run `python apply.py resume %s` (or create %s) to continue'
                               % (page_plan.get('name', 'page %d' % page_index), run_key, signal_path))
                    save_run_state(state)
                    if pause_hook:
                        # Test-only synchronous injection point, invoked
                        # on this SAME thread before the wait begins —
                        # lets a test simulate "the human edits the live
                        # page during the pause" deterministically,
                        # without a second process/thread racing to
                        # reach the same page. Never set outside tests.
                        pause_hook(page)
                    waited = wait_for_handoff_signal(page, signal_path, poll_seconds=2)
                    log_action(state, 'resume signal received after %ds — continuing to the Next button '
                               '(any manual edits made during the pause are read fresh from the live page, '
                               'not overwritten)' % waited)
                    # A manual handoff is a point where the session could
                    # have been lost server-side (e.g. a timeout while the
                    # human was slowly editing) -- a real, measured gap
                    # (caught by an independent audit) applied this check
                    # only on a cold cross-process resume, not after every
                    # manual handoff. Checked here too when the plan
                    # declares one, even though the page was never
                    # navigated away during the pause.
                    alive, reason = check_session_alive(page, plan.get('resume_session_check'))
                    if not alive:
                        state['status'] = 'session_lost'
                        state['issues'] = [reason]
                        log_action(state, 'SESSION LOST after manual handoff: %s — refusing to fill or '
                                   'navigate further' % reason)
                        if hold_open and headed:
                            _hold_for_close_signal(page, run_key, state)
                        return state
                    state['status'] = 'in_progress'
                    save_run_state(state)

                next_label = page_plan.get('next_button_label', 'Next')
                if is_denied_button(next_label):
                    raise RuntimeError('Plan\'s next_button_label looks like a submit button: ' + next_label)
                url_before_click = page.url
                page.get_by_role('button', name=next_label, exact=True).click()

                errors = read_validation_errors(page)
                if errors:
                    state['status'] = 'blocked_validation'
                    state['issues'] = errors
                    log_action(state, 'validation errors: ' + '; '.join(errors))
                    if hold_open and headed:
                        _hold_for_close_signal(page, run_key, state)
                    return state

                # A real, measured bug (caught by an independent audit):
                # with no .error.visible element present, the code
                # previously assumed the Next click succeeded and advanced
                # the plan index regardless -- a click that is silently
                # absorbed by client/native validation (no URL change, no
                # recognized error element) could reach is_review_page on
                # the NEXT loop iteration despite the page never having
                # moved. Requires positive evidence of a real transition:
                # either the URL changed, or the next page declares a
                # landmark_text that is now visible.
                next_page_plan = plan['pages'][page_index + 1] if page_index + 1 < len(plan['pages']) else None
                if not _page_transitioned(page, url_before_click, next_page_plan):
                    state['status'] = 'blocked_validation'
                    state['issues'] = ['Clicked "%s" but the page did not visibly advance (URL unchanged and no '
                                        'expected landmark found) — refusing to treat the next plan page as '
                                        'reached.' % next_label]
                    log_action(state, state['issues'][0])
                    if hold_open and headed:
                        _hold_for_close_signal(page, run_key, state)
                    return state

                state['current_url'] = page.url
                log_action(state, 'advanced to ' + page.url)

            state['status'] = 'awaiting_review'
            if hold_open and headed:
                _hold_for_close_signal(page, run_key, state)
            return state
        except HandoffSafetyCapReached as e:
            # Distinct from a genuine site/automation error: this is the
            # crash-guard firing, not evidence anything is wrong with the
            # application itself. Reported as its own status so it is
            # never mistaken for a real error, and never silently treated
            # as "done" either.
            state['status'] = 'handoff_safety_cap_reached'
            state['issues'] = [str(e)]
            log_action(state, 'handoff safety cap reached (crash-guard, not a site error): ' + str(e))
            return state
        except Exception as e:
            state['status'] = 'error'
            state['issues'] = [str(e)]
            log_action(state, 'stopped on error: ' + str(e))
            if hold_open and headed:
                _hold_for_close_signal(page, run_key, state)
            return state
        finally:
            save_run_state(state)
            context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)

    fill_cmd = sub.add_parser('fill')
    fill_cmd.add_argument('run_key')
    fill_cmd.add_argument('--plan', required=True)
    fill_cmd.add_argument('--profile', required=True)
    fill_cmd.add_argument('--answer-bank', default=None,
                           help='Optional path to an answers.py bank (private/answer_bank.json) '
                                'for "answerbank:<question>" plan values.')
    fill_cmd.add_argument('--job-key', default=None,
                           help='radar.py job key for this posting, to refuse starting a duplicate '
                                'application if it is already recorded as APPLIED there.')
    fill_cmd.add_argument('--radar-state', default=None,
                           help='Path to the radar.py state file to check for duplicates against '
                                '(default: private/state.json). Use demo/state.json for fixture runs.')
    fill_cmd.add_argument('--authorize-unapproved-run', default=None, metavar='TEXT',
                           help='Explicitly authorize running this ONE fill even though the plan file itself '
                                'is still approved:false. Recorded into this run\'s state as an audit trail; '
                                'never modifies the plan file. All other validation (missing answers, the '
                                'action allowlist, submit-button guardrails) still applies in full.')
    fill_cmd.add_argument('--headed', action='store_true')
    fill_cmd.add_argument('--hold-open', action='store_true',
                           help='After reaching a review/blocked/error/session_lost state, keep this process '
                                '(and the SAME visible browser/page — nothing is closed or reloaded) open until '
                                'a human sends an explicit close signal via `python apply.py close RUN_KEY`. '
                                'No fixed timeout: replaces the old --leave-open SECONDS flag, which once closed '
                                'a real in-progress manual edit when its timer expired. Requires --headed.')
    fill_cmd.add_argument('--force-refill', action='store_true',
                           help='Redo the fill even if the saved state already says awaiting_review (with no '
                                'field errors) or session_lost — e.g. because the human reports the browser tab/ '
                                'window was closed, or confirms how to proceed after a lost session, and the '
                                'saved status can no longer be trusted. Always restarts at start_url rather than '
                                'a saved current_url nothing can confirm is still open.')

    status_cmd = sub.add_parser('status', help='Read-only: print the run_state JSON for a run_key — its status, '
                                 'current page, action log, and any issues. Safe to call anytime, including '
                                 'while another `fill` is mid-run.')
    status_cmd.add_argument('run_key')

    resume_cmd = sub.add_parser('resume', help='Tell a `fill --hold-open` run that is paused at a '
                                 'pause_after_fill checkpoint (status paused_for_review) to continue to the next '
                                 'step, in the SAME still-open browser. Does not close anything. No-op if the run '
                                 'is not currently waiting at such a checkpoint.')
    resume_cmd.add_argument('run_key')

    close_cmd = sub.add_parser('close', help='Tell a `fill --hold-open` run that is holding at a terminal state '
                                '(awaiting_review / blocked_validation / error / session_lost) that the human is '
                                'completely done and it may now exit, closing the browser. Distinct from resume: '
                                'never use this to mean "continue" — that is what resume is for.')
    close_cmd.add_argument('run_key')

    inspect_cmd = sub.add_parser('inspect', help='Read-only: list a page\'s fields/buttons. Never fills or clicks anything.')
    inspect_cmd.add_argument('url')
    inspect_cmd.add_argument('--headed', action='store_true')
    inspect_cmd.add_argument('--open-combobox', action='append', default=[], metavar='LABEL',
                              help='Read-only: open this combobox (by its exact accessible name) and record its '
                                   'option list, then close it with Escape without selecting anything. Repeatable.')
    inspect_cmd.add_argument('--type-into', action='append', default=[], nargs=2, metavar=('LABEL', 'TEXT'),
                              help='Read-only: open this combobox and type TEXT into it before recording options '
                                   '(for type-ahead comboboxes), then close with Escape without selecting. Repeatable.')

    worker_start_cmd = sub.add_parser('worker-start', help='Launch ONE browser-owning worker process for run_key '
                                       'that stays alive accepting a SEQUENCE of commands (worker-send) against '
                                       'the SAME live page -- inspect, fill, click-next, pause for a human '
                                       'handoff, more fill, close -- never closing/relaunching between them. '
                                       'Blocks in this terminal until the worker receives a close command; run it '
                                       'in its own terminal/background process and drive it with worker-send from '
                                       'another.')
    worker_start_cmd.add_argument('run_key')
    worker_start_cmd.add_argument('--profile', required=True)
    worker_start_cmd.add_argument('--answer-bank', default=None)
    worker_start_cmd.add_argument('--url', default=None, help='Initial URL to open. Omit to reuse whatever is '
                                   'already loaded in the persistent profile (e.g. a page a human left open).')
    worker_start_cmd.add_argument('--headed', action='store_true')
    worker_start_cmd.add_argument('--idle-cap-seconds', type=int, default=4 * 3600,
                                   help='Close the worker after this many seconds with NO commands '
                                        '(crash-guard). Use 0 to disable, for a long supervised session '
                                        'where a human is editing the page and automated commands are '
                                        'deliberately infrequent -- a measured failure mode: the default '
                                        '4h cap closed a live application session mid-review.')

    worker_send_cmd = sub.add_parser('worker-send', help='Send ONE command to an already-running worker-start '
                                      'process and print its result. Commands: inspect (read-only), read_fields '
                                      '(--steps-json PATH, read-only -- same step shape as fill_steps but nothing '
                                      'is written), fill_steps (--steps-json PATH, a JSON list of plan-style step '
                                      'objects), click_next '
                                      '(--label, --landmark), pause (--reason; blocks the WORKER, not this '
                                      'command, until `apply.py resume RUN_KEY`), goto (--url), status, close.')
    worker_send_cmd.add_argument('run_key')
    worker_send_cmd.add_argument('--cmd', required=True, choices=WORKER_COMMANDS)
    worker_send_cmd.add_argument('--url', default=None)
    worker_send_cmd.add_argument('--label', default='Next')
    worker_send_cmd.add_argument('--landmark', default=None)
    worker_send_cmd.add_argument('--reason', default='manual review/handoff')
    worker_send_cmd.add_argument('--steps-json', default=None, help='Path to a JSON file containing a list of '
                                  'plan-style step objects (same shape as a plan page\'s "steps") for fill_steps.')
    worker_send_cmd.add_argument('--contains', default=None, help='For fix_field_by_current_value: substring an '
                                  'existing field\'s CURRENT value must contain to be targeted (refused if this '
                                  'matches zero or more than one field).')
    worker_send_cmd.add_argument('--new-value', default=None, help='For fix_field_by_current_value: the '
                                  'corrected value to fill into the one matched field.')
    worker_send_cmd.add_argument('--question-text', default=None, help='For answer_question: the exact (or '
                                  'closely matching) visible question text to scope the target radio group by -- '
                                  'never an internal control name or field position.')
    worker_send_cmd.add_argument('--answer-label', default=None, help='For answer_question: the visible label '
                                  'of the option to select within that scoped question (e.g. "Yes"/"No").')
    worker_send_cmd.add_argument('--label-hint', default=None, help='For read_attachment_state: the attachment '
                                  'widget\'s own visible section text (e.g. "Resume") to scope the read to -- '
                                  'never the file <input>\'s id, which is dynamically generated and can change '
                                  'or disappear after a successful upload.')
    worker_send_cmd.add_argument('--expected-filename', default=None, help='For read_attachment_state: the '
                                  'filename to check for within the scoped widget text.')
    worker_send_cmd.add_argument('--scope-selector', default=None, help='For answer_question: optional CSS '
                                  'selector to further bound the search (e.g. when the same question text could '
                                  'legitimately appear more than once on the page).')
    worker_send_cmd.add_argument('--timeout', type=int, default=120, metavar='SECONDS',
                                  help='How long to wait for the worker to respond (not how long the command '
                                       'itself may take inside the worker, e.g. a pause can legitimately take '
                                       'much longer -- this is send_worker_command\'s own polling timeout).')

    worker_status_cmd = sub.add_parser('worker-status', help='Read-only: print run_state for a worker run_key '
                                        '(same shape as `status`). Does not require the worker to be running.')
    worker_status_cmd.add_argument('run_key')

    args = parser.parse_args()
    if args.command == 'fill':
        if args.hold_open and not args.headed:
            parser.error('--hold-open requires --headed (there is no visible browser to hold open otherwise)')
        try:
            result = run_fill(args.run_key, args.plan, args.profile, headless=not args.headed,
                               answer_bank_path=args.answer_bank, job_key=args.job_key,
                               radar_state_path=args.radar_state, hold_open=args.hold_open,
                               run_authorization=args.authorize_unapproved_run, force_refill=args.force_refill)
        except RunLockedError as e:
            result = {'run_key': args.run_key, 'status': 'refused_locked', 'issues': [str(e)]}
    elif args.command == 'status':
        result = load_run_state(args.run_key)
    elif args.command == 'resume':
        path = resume_signal_path(args.run_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('resume', encoding='utf-8')
        result = {'run_key': args.run_key, 'signal': 'resume', 'signal_file': str(path)}
    elif args.command == 'close':
        path = close_signal_path(args.run_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('close', encoding='utf-8')
        result = {'run_key': args.run_key, 'signal': 'close', 'signal_file': str(path)}
    elif args.command == 'inspect':
        result = inspect_page(args.url, headless=not args.headed)
        queries = [{'label': label, 'type_text': None} for label in args.open_combobox]
        queries += [{'label': label, 'type_text': text} for label, text in args.type_into]
        if queries:
            result['combobox_options'] = inspect_combobox_options(args.url, queries, headless=not args.headed)
    elif args.command == 'worker-start':
        try:
            # 0 disables the idle crash-guard entirely (float('inf') is never reached
            # by the integer idle counter), for supervised sessions where long human
            # pauses are expected and normal.
            idle_cap = args.idle_cap_seconds if args.idle_cap_seconds > 0 else float('inf')
            result = run_worker(args.run_key, args.profile, start_url=args.url, headless=not args.headed,
                                 answer_bank_path=args.answer_bank, idle_safety_cap_seconds=idle_cap)
        except RunLockedError as e:
            result = {'run_key': args.run_key, 'status': 'refused_locked', 'issues': [str(e)]}
    elif args.command == 'worker-send':
        command = {'cmd': args.cmd}
        if args.cmd == 'goto':
            command['url'] = args.url
        elif args.cmd == 'click_next':
            command['label'] = args.label
            if args.landmark:
                command['landmark_text'] = args.landmark
        elif args.cmd == 'pause':
            command['reason'] = args.reason
        elif args.cmd == 'fill_steps':
            if not args.steps_json:
                parser.error('fill_steps requires --steps-json PATH')
            command['steps'] = read_json(args.steps_json)
        elif args.cmd == 'read_fields':
            if not args.steps_json:
                parser.error('read_fields requires --steps-json PATH (same step shape as fill_steps, '
                              'but nothing is written -- only current values are read back)')
            command['steps'] = read_json(args.steps_json)
        elif args.cmd == 'read_attachment_state':
            if not args.label_hint:
                parser.error('read_attachment_state requires --label-hint (e.g. "Resume")')
            command['label_hint'] = args.label_hint
            if args.expected_filename:
                command['expected_filename'] = args.expected_filename
        elif args.cmd == 'fix_field_by_current_value':
            if not args.contains or args.new_value is None:
                parser.error('fix_field_by_current_value requires --contains and --new-value')
            command['contains'] = args.contains
            command['new_value'] = args.new_value
        elif args.cmd == 'answer_question':
            if not args.question_text or not args.answer_label:
                parser.error('answer_question requires --question-text and --answer-label')
            command['question_text'] = args.question_text
            command['answer_label'] = args.answer_label
            if args.scope_selector:
                command['scope_selector'] = args.scope_selector
        result = send_worker_command(args.run_key, command, timeout_seconds=args.timeout)
    elif args.command == 'worker-status':
        result = load_run_state(args.run_key)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
