"""Job Radar agent: an explicit LLM tool-calling loop, written out by hand.

THE LOOP (this is the whole graded idea, and it is deliberately readable):

    1. Call the Anthropic Messages API with the task, context, and tool definitions.
    2. Read the model's requested tool calls out of the response content blocks.
    3. Validate the tool name and arguments, then execute the real Python function.
    4. Send the ACTUAL results back, each tagged with its own tool_use_id.
    5. Call the model again with the extended conversation.
    6. Stop on: a final answer, a human-input requirement, or the step limit.

WHY A LOOP AND NOT ONE CALL:
    One call can only answer from what we decided to put in the prompt. A loop
    lets the model decide what it still needs to know -- pull the work
    authorization section, notice sponsorship is unconfirmed, check whether we
    already applied -- and change its conclusion based on what came back. The
    observations drive the reasoning; that is what makes it an agent.

WHAT THIS FILE REFUSES TO DO:
    No submission tool is registered, here or in tools.py. The agent physically
    cannot submit an application, regardless of what the model asks for.

Run:  python agent.py assess --source momentum_associate_strategy_ops_analyst.json
Python 3.10+.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import tools

DEFAULT_MODEL = 'claude-opus-5'
DEFAULT_MAX_STEPS = 12          # hard ceiling on API calls, so a confused run cannot spin forever
DEFAULT_MAX_TOKENS = 4096

SYSTEM_PROMPT = """You are the judgment layer of Job Radar, a personal job-search assistant for one \
candidate. You decide; deterministic Python tools supply facts and perform storage.

YOUR JOB
Assess one real job posting against the candidate's verified profile, then save that assessment.
Work in steps: gather what you actually need with tools, then conclude. Do not guess at facts you \
could simply look up.

EVIDENCE RULES
- Every dimension you save needs a `quote` copied VERBATIM from the posting text, character for \
character. The save is rejected if the quote is not found in the posting. Never paraphrase a quote. \
Leave `quote` empty ONLY when that dimension's status is "uncertain".
- Only use facts returned by tools. Never invent an employer, a date, a credential, a reference, a \
proficiency rating, an exact graduation day, or a duration of experience.
- A field that comes back null or absent is UNKNOWN, not false and not zero. If a required decision \
depends on an unknown fact, call request_missing_fact instead of assuming.
- Check each fact's confirmation status. A fact parsed from a resume is weaker than one the \
candidate confirmed, and must not be presented as confirmed.

JUDGMENT RULES
- Classify fit as exactly one of: GOOD FIT, STRETCH BUT REASONABLE, POOR FIT. Never output a \
percentage, a score, or a hiring-probability estimate.
- Distinguish REQUIRED from PREFERRED qualifications. Operations and leadership experience is real \
experience, but it does not by itself satisfy a stated requirement for years of professional \
data-science experience.
- Read degree-population requirements literally (a graduate student does not automatically qualify \
for an undergraduate-only program).
- Assess exactly four dimensions: experience, education, work_authorization, career_fit.

WORK AUTHORIZATION AND SPONSORSHIP -- keep these four things separate:
  (a) current work authorization, (b) future sponsorship need, (c) current OPT status, \
(d) the employer's actual policy.
- A sponsorship SCREENING QUESTION on a form establishes nothing about employer policy.
- An unknown employer policy is "uncertain". It is NEVER an automatic rejection.
- Apply any restriction to the specific requisition you are reading, unless there is reliable \
evidence of a broader company policy.
- Never infer current visa/CPT/OPT eligibility from a past internship, and never state a legal \
conclusion. Compare the posting's stated requirement against the profile's stated facts, nothing more.

CONTENT EXCLUSIONS (standing instructions from the candidate)
- Exclude the MBA and any unsupported hospitality experience from application content.
- USD 50,000 is the candidate's salary EXPECTATION, not a minimum filter for excluding jobs.
- Do not infer willingness to relocate, and do not infer an immediate start date.
- Parser-generated defaults are not confirmed answers.

PROCESS
- Before recommending an application, check the application history for that requisition. Applying \
twice to the same requisition is a defect.
- If the posting text is empty or too short to assess, say so plainly and do not invent an \
assessment from the job title alone.
- When you have gathered enough, call save_assessment, then give a short plain-English summary of \
what you concluded and why."""


def load_dotenv_if_present(path='.env'):
    """Minimal .env reader so the key never has to live in the shell profile.

    Deliberately tiny and dependency-light: KEY=value per line, # comments
    ignored, existing environment variables always win.
    """
    env_path = Path(path)
    if not env_path.exists():
        return False
    for raw in env_path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        name, _, value = line.partition('=')
        name, value = name.strip(), value.strip().strip('"').strip("'")
        if name and name not in os.environ:
            os.environ[name] = value
    return True


PLACEHOLDER_MARKERS = ('replace-with-your-real-key', 'your-key-here', 'sk-ant-your')


def api_key_problem():
    """Return a human-readable reason the key is unusable, or None if it looks real.

    A copied-but-unedited .env is the most likely setup mistake, and reporting
    it as "configured" would turn a trivial fix into a confusing auth error
    halfway through a run.
    """
    key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not key:
        return 'ANTHROPIC_API_KEY is not set (no .env file, or the file has no value).'
    if any(marker in key for marker in PLACEHOLDER_MARKERS):
        return ('.env still contains the placeholder from .env.example. Replace it with your real key '
                'from https://console.anthropic.com/settings/keys')
    if not key.startswith('sk-ant-'):
        return 'ANTHROPIC_API_KEY does not look like an Anthropic key (expected it to start with sk-ant-).'
    return None


def api_key_is_real():
    return api_key_problem() is None


class Metrics:
    """Measured, not promised. Printed at the end of every run."""

    def __init__(self):
        self.started = time.monotonic()
        self.api_calls = 0
        self.tool_calls = 0
        self.tool_failures = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.stop_reason = None          # why OUR loop ended
        self.api_stop_reason = None      # why the LAST API response ended (truncation shows up here)
        self.successful_tools = set()    # tools that actually returned a non-error observation

    def as_dict(self):
        return {
            'elapsed_seconds': round(time.monotonic() - self.started, 2),
            'api_calls': self.api_calls,
            'tool_calls': self.tool_calls,
            'tool_failures': self.tool_failures,
            'input_tokens': self.input_tokens,
            'output_tokens': self.output_tokens,
            'loop_stop_reason': self.stop_reason,
            'api_stop_reason': self.api_stop_reason,
            'tools_that_succeeded': sorted(self.successful_tools),
        }


def build_toolset(name):
    """Compose which tools this run may use. The LOOP never changes; only the tools do.

    'assess' is the small graded core. 'apply' adds the browser tools from
    browser_tools.py for supervised form preparation. Everything still runs
    through tools.execute_tool, so validation is identical either way.
    """
    definitions = list(tools.TOOL_DEFINITIONS)
    registry = dict(tools.TOOL_REGISTRY)
    if name == 'apply':
        import browser_tools
        definitions += browser_tools.TOOL_DEFINITIONS
        registry.update(browser_tools.TOOL_REGISTRY)
    elif name != 'assess':
        raise ValueError('unknown toolset %r (use: assess, apply)' % name)
    if any('submit' in n.lower() for n in registry):
        raise RuntimeError('refusing to run: a submission tool is registered')
    return definitions, registry


def run_agent(task, client=None, model=None, max_steps=DEFAULT_MAX_STEPS, verbose=True,
              tool_definitions=None, tool_registry=None, required_outcome_tool=None):
    """The agent loop. `client` is injectable so tests can run it without a network."""
    model = model or os.environ.get('JOB_RADAR_MODEL') or DEFAULT_MODEL
    if tool_definitions is None:
        tool_definitions = tools.TOOL_DEFINITIONS
    if tool_registry is None:
        tool_registry = tools.TOOL_REGISTRY
    metrics = Metrics()

    if client is None:
        try:
            import anthropic
        except ImportError:
            raise RuntimeError('The `anthropic` package is not installed. Run: py -m pip install -r requirements.txt')
        problem = api_key_problem()
        if problem:
            raise RuntimeError(problem + '\nEdit the .env file next to agent.py, then re-run. '
                                         'Never paste the key into a chat.')
        client = anthropic.Anthropic()

    messages = [{'role': 'user', 'content': task}]
    transcript = []

    for step in range(1, max_steps + 1):
        # ---- STEP 1: call the model with the conversation so far + our tools ----
        response = client.messages.create(
            model=model,
            max_tokens=DEFAULT_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=tool_definitions,
            messages=messages,
        )
        metrics.api_calls += 1
        metrics.api_stop_reason = getattr(response, 'stop_reason', None)
        usage = getattr(response, 'usage', None)
        if usage is not None:
            metrics.input_tokens += getattr(usage, 'input_tokens', 0) or 0
            metrics.output_tokens += getattr(usage, 'output_tokens', 0) or 0

        # ---- STEP 2: read what the model said and which tools it wants ----
        said = [b.text for b in response.content if getattr(b, 'type', None) == 'text']
        tool_uses = [b for b in response.content if getattr(b, 'type', None) == 'tool_use']
        for text in said:
            transcript.append({'step': step, 'role': 'assistant', 'text': text})
            if verbose and text.strip():
                print('\n[step %d] model: %s' % (step, text.strip()))

        # The model stopped asking for tools. That alone does NOT mean success:
        # the response may have been cut off mid-sentence, or the model may have
        # talked its way to the end without ever producing the required outcome.
        # Both of those have to be reported as what they are.
        if not tool_uses:
            api_stop = getattr(response, 'stop_reason', None)
            metrics.api_stop_reason = api_stop
            final_text = '\n'.join(said).strip()

            if api_stop == 'max_tokens':
                metrics.stop_reason = 'truncated'
                return {'status': 'truncated', 'final_text': final_text,
                        'required_outcome_met': required_outcome_tool in metrics.successful_tools
                                                if required_outcome_tool else None,
                        'problem': ('The model hit max_tokens and its reply was cut off mid-generation. '
                                    'This is NOT a completed task.'),
                        'transcript': transcript, 'metrics': metrics.as_dict()}

            if api_stop == 'refusal':
                metrics.stop_reason = 'refusal'
                return {'status': 'refusal', 'final_text': final_text,
                        'transcript': transcript, 'metrics': metrics.as_dict()}

            if required_outcome_tool and required_outcome_tool not in metrics.successful_tools:
                metrics.stop_reason = 'ended_without_required_outcome'
                return {'status': 'ended_without_required_outcome', 'final_text': final_text,
                        'required_outcome_met': False,
                        'problem': ('The run ended without a successful %s call, so the task outcome was '
                                    'not achieved even though the model stopped talking.'
                                    % required_outcome_tool),
                        'transcript': transcript, 'metrics': metrics.as_dict()}

            metrics.stop_reason = 'final_answer'
            return {'status': 'completed', 'final_text': final_text,
                    'required_outcome_met': True if required_outcome_tool else None,
                    'transcript': transcript, 'metrics': metrics.as_dict()}

        # Keep the assistant turn verbatim; tool_result blocks must answer it.
        messages.append({'role': 'assistant', 'content': response.content})

        # ---- STEPS 3 & 4: validate, execute, and return results by tool_use_id ----
        results_for_model = []
        human_question = None
        for block in tool_uses:
            if verbose:
                print('[step %d] tool -> %s(%s)' % (step, block.name,
                      json.dumps(block.input, ensure_ascii=False)[:160]))
            observation, is_error = tools.execute_tool(block.name, block.input, registry=tool_registry)
            metrics.tool_calls += 1
            if is_error:
                metrics.tool_failures += 1
                if verbose:
                    print('[step %d]   !! FAILED: %s' % (step, observation.get('error', '')[:200]))
            else:
                metrics.successful_tools.add(block.name)
                if verbose:
                    print('[step %d]   ok' % step)
            transcript.append({'step': step, 'role': 'tool', 'name': block.name,
                               'input': block.input, 'observation': observation, 'is_error': is_error})
            results_for_model.append({
                'type': 'tool_result',
                'tool_use_id': block.id,                   # pairing identifier -- must match exactly
                'content': json.dumps(observation, ensure_ascii=False, default=str),
                'is_error': is_error,
            })
            if block.name in tools.HUMAN_INPUT_TOOLS and not is_error:
                human_question = observation

        # ---- STEP 6a: a required human answer stops the run cleanly ----
        if human_question is not None:
            metrics.stop_reason = 'human_input_required'
            return {'status': 'needs_human_input', 'question': human_question,
                    'transcript': transcript, 'metrics': metrics.as_dict()}

        # ---- STEP 5: feed the real observations back and loop ----
        messages.append({'role': 'user', 'content': results_for_model})

    # ---- STEP 6b: explicit execution limit ----
    metrics.stop_reason = 'max_steps_reached'
    return {'status': 'max_steps_reached',
            'required_outcome_met': (required_outcome_tool in metrics.successful_tools
                                     if required_outcome_tool else None),
            'problem': 'Hit the %d-step limit before finishing. The task is INCOMPLETE.' % max_steps,
            'transcript': transcript, 'metrics': metrics.as_dict()}


def build_assess_task(source):
    return (
        'Assess this saved job posting for me.\n\n'
        'Posting source identifier: %s\n\n'
        'Work through it: read the posting, pull the profile sections you need, check whether I have '
        'already applied to this requisition, and then save your assessment. If a required fact is '
        'genuinely unknown, ask me rather than guessing.' % source
    )


def main():
    parser = argparse.ArgumentParser(description='Job Radar agent (explicit tool-calling loop).')
    sub = parser.add_subparsers(dest='command', required=True)

    check = sub.add_parser('check', help='Report configuration without calling the API.')
    check.add_argument('--env-file', default='.env')

    assess = sub.add_parser('assess', help='Assess one saved posting with the agent loop.')
    assess.add_argument('--source', required=True,
                        help='Posting draft filename under private/postings_drafts/, or a ledger job key.')
    assess.add_argument('--model', default=None)
    assess.add_argument('--max-steps', type=int, default=DEFAULT_MAX_STEPS)
    assess.add_argument('--env-file', default='.env')
    assess.add_argument('--json', action='store_true', help='Print the machine-readable run record.')

    prepare = sub.add_parser('prepare', help='Supervised form preparation on a LIVE application run '
                                             '(browser toolset). Never submits.')
    prepare.add_argument('--run-key', required=True, help='Live application run identifier.')
    prepare.add_argument('--goal', default='Inspect the current page, report exactly what is filled, '
                                           'what is required and still missing, and what you would do next. '
                                           'Do not change anything yet.')
    prepare.add_argument('--model', default=None)
    prepare.add_argument('--max-steps', type=int, default=DEFAULT_MAX_STEPS)
    prepare.add_argument('--env-file', default='.env')
    prepare.add_argument('--json', action='store_true')

    args = parser.parse_args()
    load_dotenv_if_present(args.env_file)

    if args.command == 'check':
        print(json.dumps({
            'anthropic_api_key_configured': api_key_is_real(),
            'api_key_problem': api_key_problem(),
            'model': os.environ.get('JOB_RADAR_MODEL') or DEFAULT_MODEL,
            'env_file_found': Path(args.env_file).exists(),
            'tools_registered': sorted(tools.TOOL_REGISTRY),
            'submission_tool_registered': any('submit' in n.lower() for n in tools.TOOL_REGISTRY),
        }, indent=2))
        return

    required_outcome = None
    if args.command == 'assess':
        task = build_assess_task(args.source)
        definitions, registry = build_toolset('assess')
        # The assess task is only "done" if the assessment was actually saved.
        required_outcome = 'save_assessment'
    else:
        task = ('You are working on live application run %r.\n\n%s\n\n'
                'Observe the real page before acting. Never advance while required fields are '
                'unverified. You cannot submit; final submission is the human\'s.'
                % (args.run_key, args.goal))
        definitions, registry = build_toolset('apply')

    try:
        result = run_agent(task, model=args.model, max_steps=args.max_steps,
                           tool_definitions=definitions, tool_registry=registry,
                           required_outcome_tool=required_outcome)
    except RuntimeError as e:
        print('ERROR: %s' % e, file=sys.stderr)
        sys.exit(1)

    print('\n' + '=' * 70)
    print('status: %s' % result['status'])
    if result.get('problem'):
        print('PROBLEM: %s' % result['problem'])
    if result.get('required_outcome_met') is not None:
        print('required outcome (%s) met: %s' % (required_outcome, result['required_outcome_met']))
    if result['status'] == 'needs_human_input':
        q = result['question']
        print('\nThe agent needs one fact from you:\n  Q: %s\n  why: %s' % (q['question'], q['why_needed']))
    print('\nmetrics: %s' % json.dumps(result['metrics']))
    # A run that did not achieve its outcome must not look like a success to a
    # shell script, a CI job, or a tired human reading the last line.
    if result['status'] != 'completed':
        sys.exit(2)
    if args.json:
        print('\n' + json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
    main()
