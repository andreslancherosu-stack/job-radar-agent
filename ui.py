"""Minimal local dashboard for Job Radar.

Thin orchestration layer only. Every decision and state mutation happens in
radar.py / apply.py / import_posting.py / answers.py / coverletter.py /
llm_client.py, invoked here as subprocesses and rendered from their JSON
output or state files — this file does not reimplement any of their logic.

Launch (from the project root, so relative paths like private/state.json
resolve correctly):
    python -m streamlit run ui.py

Real mode uses private/profile.json and private/state.json (real, gitignored
data). The "Use demo fixtures" toggle in the sidebar switches every action to
demo/state.json and fictional data instead — never mixed in one state file,
matching radar.py's own rule.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import streamlit as st

PY = sys.executable

st.set_page_config(page_title='Job Radar', layout='wide')


def run_cli(args, timeout=30):
    """Runs a project CLI script as a real subprocess and returns
    (ok, parsed_json_or_none, raw_stdout, raw_stderr). This is the only way
    this UI talks to radar.py/apply.py/etc. — it never imports their logic
    directly, so the UI can never drift from what the command-line tools
    actually do."""
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    try:
        result = subprocess.run([PY] + args, capture_output=True, text=True,
                                 encoding='utf-8', errors='replace', timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return False, None, '', 'Timed out after %ss running: %s' % (timeout, ' '.join(args))
    ok = result.returncode == 0
    parsed = None
    try:
        parsed = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        pass
    return ok, parsed, result.stdout, result.stderr


def read_json_file(path):
    path = Path(path)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


st.title('Job Radar')

st.sidebar.header('Mode')
use_demo = st.sidebar.toggle('Use demo fixtures (fictional data only)', value=False)
state_path = 'demo/state.json' if use_demo else 'private/state.json'
state_args = ['--state', state_path, '--date', '2026-09-09'] if use_demo else ['--state', state_path]
st.sidebar.caption('State file: `%s`' % state_path)
if not use_demo:
    st.sidebar.warning('Real mode. `Fill application` opens a real browser and, once you click '
                        'Submit yourself, `I submitted it` records a real application in your history.')

anthropic_ok, anthropic_check, _, _ = run_cli(['llm_client.py', 'check'])
if anthropic_ok and anthropic_check:
    key_status = 'configured' if anthropic_check['anthropic_api_key_configured'] else 'not configured'
    st.sidebar.caption('Standalone Anthropic API key: **%s** (model %s)' %
                        (key_status, anthropic_check['model']))
    if not anthropic_check['anthropic_api_key_configured']:
        st.sidebar.caption('Without it, assessments still work via a chat session following '
                            'AGENT_INSTRUCTIONS.md — this key only enables the optional standalone path.')

# --- Shortlist ---
st.header('1. Shortlist')
ok, listing, out, err = run_cli(['radar.py'] + state_args + ['list'])
if not ok:
    st.error('radar.py list failed:\n' + err)
listing = listing or []
rows = [{'Key': item['key'], 'Company': item['company'], 'Role': item['role'], 'Decision': item['decision']}
        for item in listing]

selected_key = None
if rows:
    st.dataframe(rows, width='stretch', hide_index=True)
    label_by_key = {r['Key']: '%s — %s (%s)' % (r['Company'], r['Role'], r['Decision']) for r in rows}
    selected_key = st.selectbox('Selected job', options=list(label_by_key), format_func=lambda k: label_by_key[k])
else:
    st.info('No postings ingested yet in this state file.')

if st.button('Run shortlist (at most two RECOMMEND targets)'):
    ok, shortlist_result, out, err = run_cli(['radar.py'] + state_args + ['shortlist'])
    st.json(shortlist_result if ok else {'error': err})

# --- Import from URL ---
with st.expander('2. Import a job posting from a URL'):
    st.caption('Plain HTTP fetch — does not run JavaScript. If the extracted text looks thin, '
               'paste the posting text into a draft file by hand instead.')
    url = st.text_input('Posting URL')
    col_a, col_b, col_c = st.columns(3)
    company = col_a.text_input('Company')
    role = col_b.text_input('Role')
    job_id = col_c.text_input('Job ID (optional)')
    if st.button('Fetch and write draft'):
        if not (url and company and role):
            st.warning('URL, company, and role are required.')
        else:
            with st.spinner('Fetching...'):
                args = ['import_posting.py', 'fetch', url, '--company', company, '--role', role]
                if job_id:
                    args += ['--job-id', job_id]
                ok, result, out, err = run_cli(args, timeout=30)
            if ok:
                st.success('Draft written to %s (%d chars extracted)' % (result['draft_file'], result['text_length']))
                for w in result['warnings']:
                    st.warning(w)
                st.caption('Review the draft, set open / verified_on / human_verified once you have '
                           'actually checked the page yourself, then:')
                st.code('python radar.py --state %s ingest %s' % (state_path, result['draft_file']))
            else:
                st.error('Fetch failed:\n' + err)

# --- Selected job detail ---
if selected_key:
    st.header('3. Selected job')
    ok, detail, out, err = run_cli(['radar.py'] + state_args + ['read', selected_key])
    if ok and detail:
        posting = detail['posting']
        assessment = detail.get('assessment')
        st.subheader('%s — %s' % (posting['company'], posting['role']))
        decision = label_by_key.get(selected_key, '').rsplit('(', 1)
        decision = decision[1].rstrip(')') if len(decision) > 1 else '?'
        st.markdown('#### Decision: %s' % decision)
        st.link_button('Open application (source posting)', posting['source_url'])
        with st.expander('Posting text'):
            st.text(posting['text'])
        if assessment:
            st.write('**Fit (qualitative):** %s   **Next action:** %s' % (assessment['fit'], assessment['next_action']))
            for dim in ('experience', 'education', 'work_authorization', 'career_fit'):
                d = assessment[dim]
                st.markdown('- **%s** — %s: %s' % (dim, d['status'], d['reason']))
                if d['quote']:
                    st.caption('Quote: "%s"' % d['quote'])
        else:
            st.info('Not yet assessed. Either continue the chat-driven AGENT_INSTRUCTIONS.md loop, '
                    'or try the optional standalone API path below.')
            with st.expander('Assess with the standalone Anthropic API (optional)'):
                draft_path = st.text_input('Draft posting JSON path', value='private/postings_drafts/')
                out_path = st.text_input('Write assessment to', value='private/assessments/%s.json' % selected_key)
                if st.button('Call API and draft assessment'):
                    with st.spinner('Calling the API...'):
                        ok, result, out, err = run_cli(['llm_client.py', 'assess', draft_path,
                                                         '--profile', 'private/profile.json' if not use_demo
                                                         else 'demo/test_profile.json'], timeout=120)
                    if ok:
                        st.json(result)
                        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                        Path(out_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
                        st.success('Written to %s. Review it, then:' % out_path)
                        st.code('python radar.py --state %s assess %s %s' % (state_path, selected_key, out_path))
                    else:
                        st.error(out or err)

# --- Fill application ---
if selected_key:
    st.header('4. Fill application')
    st.caption('Requires a human-approved plan (see demo/apply_plan_testapp.json for the shape). '
               'Automation stops at the review page and never clicks Submit.')
    default_plan = 'demo/apply_plan_testapp.json' if use_demo else ''
    default_profile = 'demo/test_profile.json' if use_demo else 'private/profile.json'
    plan_path = st.text_input('Plan file', value=default_plan)
    profile_path = st.text_input('Profile file', value=default_profile)
    answer_bank_path = st.text_input('Answer bank (optional)', value='private/answer_bank.json')
    headed = st.checkbox('Headed browser (required for real sites needing login/MFA/CAPTCHA)',
                          value=not use_demo)
    run_key = st.text_input('Run key (reuse the same key to resume)', value=selected_key)

    if st.button('Fill application (start / resume)'):
        if not plan_path:
            st.warning('A plan file is required — it is the human-approved answer set for this posting.')
        else:
            args = ['apply.py', 'fill', run_key, '--plan', plan_path, '--profile', profile_path,
                    '--job-key', selected_key, '--radar-state', state_path]
            if answer_bank_path:
                args += ['--answer-bank', answer_bank_path]
            if headed:
                args += ['--headed']
            with st.spinner('Running the fill loop... a visible browser window may open for you to '
                             'log in if headed mode is on.'):
                ok, result, out, err = run_cli(args, timeout=900)
            if not ok:
                st.error('apply.py fill failed:\n' + err)

    run_state = read_json_file(Path('private/application_runs') / (run_key + '.json'))
    if run_state:
        status = run_state['status']
        st.subheader('Status: ' + status)
        if status == 'paused_missing_info':
            st.warning('Missing information. Approve these, add them to the profile or answer bank, '
                       'then click "Fill application" again to resume:')
            for q in run_state['missing_questions']:
                st.write('- **%s**  (`%s`)' % (q['label'], q['profile_path']))
        elif status == 'blocked_validation':
            st.warning('The site rejected the values that were entered:')
            for issue in run_state['issues']:
                st.write('- ' + issue)
        elif status == 'blocked_duplicate':
            st.error('This job is already recorded as applied in %s — refusing to start a duplicate.' % state_path)
        elif status == 'error':
            st.error('Stopped on an error: ' + '; '.join(run_state.get('issues', [])))
        elif status == 'awaiting_review':
            st.success('Reached the review page. Nothing further is filled automatically.')

        with st.expander('Action log (%d entries)' % len(run_state['action_log'])):
            for entry in run_state['action_log']:
                st.text('%s  %s' % (entry['at'], entry['message']))

        if status == 'awaiting_review':
            st.header('5. Final review — submission is yours')
            st.info('Go to the browser window and check every field and the uploaded documents '
                    'yourself. Only you click the site\'s own Submit button — this tool refuses to.')
            if st.button('I submitted it — record as APPLIED'):
                ok, result, out, err = run_cli(['radar.py'] + state_args +
                                                ['confirm', selected_key, '--user-confirmed'])
                if ok:
                    st.success('Recorded in %s.' % state_path)
                else:
                    st.error('radar.py confirm failed:\n' + err)

# --- Answer bank ---
with st.expander('Answer bank'):
    ok, entries, out, err = run_cli(['answers.py', 'list'])
    st.dataframe(entries or [], width='stretch', hide_index=True)
    st.caption('Add a confirmed answer (only confirmed entries are ever reused automatically):')
    q_col, a_col = st.columns(2)
    question = q_col.text_input('Question', key='bank_question')
    answer = a_col.text_input('Answer', key='bank_answer')
    source = st.text_input('Source', key='bank_source', placeholder='e.g. "user confirmed 2026-09-25"')
    if st.button('Add confirmed answer'):
        if question and answer and source:
            ok, result, out, err = run_cli(['answers.py', 'add', question, answer, '--source', source, '--confirmed'])
            if ok:
                st.success('Added.')
                st.rerun()
            else:
                st.error(err)
        else:
            st.warning('Question, answer, and source are all required.')

# --- Cover letter ---
if selected_key:
    with st.expander('Cover letter for the selected job'):
        cl_record = None
        try:
            cl_record = read_json_file(Path('private/documents/cover_letters') / (selected_key + '.json'))
        except (json.JSONDecodeError, OSError):
            pass
        if cl_record:
            st.write('Status: **%s**' % ('approved' if cl_record['approved'] else 'draft, not yet approved'))
            st.text_area('Current text', value=cl_record['text'], height=200, disabled=True)
            if not cl_record['approved']:
                if st.button('Approve this draft'):
                    ok, result, out, err = run_cli(['coverletter.py', 'approve', selected_key])
                    st.success('Approved.') if ok else st.error(err)
                    st.rerun()
            elif not cl_record['generated_verified']:
                if st.button('Generate .docx'):
                    ok, result, out, err = run_cli(['coverletter.py', 'generate', selected_key])
                    if ok:
                        st.success('Generated and verified: %s' % result['generated_docx_path'])
                    else:
                        st.error(out or err)
            else:
                st.success('Generated and verified: %s' % cl_record['generated_docx_path'])
        else:
            st.caption('No draft yet. A tailored letter should be written by a chat session using only '
                       'verified profile/posting facts, then pasted here for you to approve.')
            text = st.text_area('Draft text (paste here for review)', height=200, key='cl_draft_text')
            notes = st.text_input('Source notes', placeholder='which profile facts / posting lines this is based on')
            if st.button('Save as draft'):
                if text.strip() and notes.strip():
                    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as f:
                        f.write(text)
                        tmp_path = f.name
                    company_role = label_by_key.get(selected_key, selected_key)
                    ok, result, out, err = run_cli(['coverletter.py', 'draft', selected_key,
                                                     '--company', company_role, '--role', '',
                                                     '--text-file', tmp_path, '--source-notes', notes])
                    if ok:
                        st.success('Draft saved. Review it above, then approve it.')
                        st.rerun()
                    else:
                        st.error(err)
                else:
                    st.warning('Draft text and source notes are both required.')
