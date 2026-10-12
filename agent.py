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
DEFAULT_MAX_TOKENS = 8192   # form analysis is verbose; 4096 truncated a live run

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
data-science experience. Equally, do NOT silently discard it -- it is relevant professional \
experience, just not analytics tenure.
- MISSING EVIDENCE IS NOT EVIDENCE OF FAILURE. This is the single most common mistake, so apply it \
literally: mark a dimension "fail" ONLY when confirmed facts establish an actual shortfall or an \
explicit incompatibility (for example, the posting states a requirement the profile positively \
contradicts, or states an exclusion the profile positively triggers). When the profile simply does \
not establish whether a requirement is met, the correct status is "uncertain", not "fail". \
"The profile does not demonstrate N years of X" is uncertainty. "The profile shows the candidate \
cannot satisfy X" is failure. Do not confuse them, and do not let an accumulation of uncertainties \
become a POOR FIT by default.
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


# Matched against the API's own error message. Ordered most-specific first:
# a billing failure is also a 400 BadRequestError, so classifying purely on
# exception type would report "malformed request" for an empty wallet and
# send the operator looking for a bug that does not exist.
_API_STATUS_HINTS = (
    ('credit balance is too low',
     'The API key is valid but the Anthropic account is OUT OF CREDIT. Add credit or a payment method '
     'at console.anthropic.com -> Plans & Billing, then re-run. Nothing was lost: no tool call was '
     'issued on this step, and any live browser session this run was driving is still open and '
     'unchanged.'),
    ('rate limit',
     'Rate limited beyond the bounded retries. Wait and re-run; do not loop retrying.'),
    ('authentication',
     'The API key was REJECTED. Check ANTHROPIC_API_KEY in .env -- it may be revoked, truncated, or '
     'from a different account. The key value is never printed by this tool.'),
    ('permission',
     'The key authenticated but lacks permission for this request (model access or workspace scope). '
     'Check the key\'s workspace and model permissions in the console.'),
    ('not_found',
     'The API rejected the request target -- most often an unrecognized model id. Verify the --model '
     'value against the models your account can access.'),
)


def _api_status_guidance(exc):
    """Turn an APIStatusError into something an operator can ACT on.

    The raw SDK message is accurate but not actionable, and the exception
    type alone is misleading (an exhausted balance arrives as a generic
    BadRequestError). So match the message first, and fall back to the
    status code only when nothing matches.
    """
    raw = str(getattr(exc, 'message', None) or exc)
    low = raw.lower()
    for needle, guidance in _API_STATUS_HINTS:
        if needle in low:
            return '%s (API said: %s)' % (guidance, raw)
    status = getattr(exc, 'status_code', None)
    return ('The Anthropic API refused this request with %s %s and retrying will not fix it. '
            'API said: %s' % (status or 'an error', type(exc).__name__, raw))


# List prices in USD per million tokens. These are an ASSUMPTION stated in
# the open, not a reading of the account's invoice: the printed dollar figure
# is an estimate to be checked against console billing, while the token counts
# beside it come from the API's own usage block and are exact.
PRICING_USD_PER_MTOK = {
    'claude-opus-5': {'input': 15.0, 'output': 75.0},
    'claude-sonnet-5': {'input': 3.0, 'output': 15.0},
    'claude-haiku-4-5-20251001': {'input': 1.0, 'output': 5.0},
}


# Prompt-cache multipliers against the base input price: a write costs more
# than a fresh token, a read costs far less. Billing them at the plain input
# rate would overstate a cached run's cost by roughly an order of magnitude.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1


def estimate_cost(model, input_tokens, output_tokens, cache_write_tokens=0, cache_read_tokens=0):
    """(usd, pricing_note). usd is None when the model's price is unknown --
    an unknown price must read as unknown, never as $0.00."""
    price = PRICING_USD_PER_MTOK.get(model)
    if price is None:
        return None, 'no list price recorded for %r; token counts are exact, cost is not shown' % model
    usd = ((input_tokens / 1e6) * price['input']
           + (output_tokens / 1e6) * price['output']
           + (cache_write_tokens / 1e6) * price['input'] * CACHE_WRITE_MULTIPLIER
           + (cache_read_tokens / 1e6) * price['input'] * CACHE_READ_MULTIPLIER)
    return usd, ('estimated at list price %s: $%.2f/Mtok in, $%.2f/Mtok out, cache write x%.2f, '
                 'cache read x%.2f (verify against console billing)'
                 % (model, price['input'], price['output'],
                    CACHE_WRITE_MULTIPLIER, CACHE_READ_MULTIPLIER))


def _fmt_args(d, limit=150):
    """Readable call signature: name=value, with long values clipped. The
    trace must show what the model ACTUALLY asked for, so arguments are
    rendered, not summarised away."""
    parts = []
    for k, v in (d or {}).items():
        t = json.dumps(v, ensure_ascii=False, default=str)
        if len(t) > limit:
            t = t[:limit] + '...<clipped>'
        parts.append('%s=%s' % (k, t))
    return ', '.join(parts)


def _fmt_observation(obs, limit=1100):
    """Compact, readable observation for the trace. Clipping is ANNOUNCED --
    a silently truncated observation would misrepresent what the model saw."""
    text = json.dumps(obs, ensure_ascii=False, indent=1, default=str)
    if len(text) <= limit:
        return text
    return text[:limit] + ('\n ...<trace display clipped at %d of %d chars; the MODEL received '
                           'the complete observation>' % (limit, len(text)))


class Metrics:
    """Measured, not promised. Printed at the end of every run."""

    def __init__(self, model=None):
        self.started = time.monotonic()
        self.model = model
        # api_calls counts SUCCESSFUL model responses -- i.e. loop iterations.
        # api_requests counts HTTP attempts, so a retried step shows up as
        # more requests than calls. Reporting only one of these would hide
        # either wasted spend or the real iteration count.
        self.api_calls = 0
        self.api_requests = 0
        self.api_retries = 0
        self.tool_calls = 0
        self.tool_failures = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_write_tokens = 0
        self.cache_read_tokens = 0
        self.stop_reason = None          # why OUR loop ended
        self.api_stop_reason = None      # why the LAST API response ended (truncation shows up here)
        self.successful_tools = set()    # tools that actually returned a non-error observation

    def elapsed(self):
        return time.monotonic() - self.started

    def cost(self):
        return estimate_cost(self.model, self.input_tokens, self.output_tokens,
                            self.cache_write_tokens, self.cache_read_tokens)

    def as_dict(self):
        usd, note = self.cost()
        return {
            'elapsed_seconds': round(self.elapsed(), 2),
            'model': self.model,
            'api_calls': self.api_calls,
            'api_requests': self.api_requests,
            'api_retries': self.api_retries,
            'tool_calls': self.tool_calls,
            'tool_failures': self.tool_failures,
            'input_tokens': self.input_tokens,
            'output_tokens': self.output_tokens,
            'cache_write_tokens': self.cache_write_tokens,
            'cache_read_tokens': self.cache_read_tokens,
            'estimated_cost_usd': None if usd is None else round(usd, 4),
            'pricing_note': note,
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
    elif name == 'radar':
        # Discovery + reporting. Deliberately does NOT include the browser
        # tools: this mode opens application pages for the human and must be
        # structurally incapable of filling a form, not merely instructed not to.
        import radar_tools
        keep = {'get_profile_facts', 'get_confirmed_answers', 'check_application_history',
                'request_missing_fact'}
        definitions = [d for d in definitions if d['name'] in keep] + radar_tools.TOOL_DEFINITIONS
        registry = {k: v for k, v in registry.items() if k in keep}
        registry.update(radar_tools.TOOL_REGISTRY)
    elif name != 'assess':
        raise ValueError('unknown toolset %r (use: assess, apply, radar)' % name)
    if any('submit' in n.lower() for n in registry):
        raise RuntimeError('refusing to run: a submission tool is registered')
    return definitions, registry


def run_agent(task, client=None, model=None, max_steps=DEFAULT_MAX_STEPS, verbose=True,
              tool_definitions=None, tool_registry=None, required_outcome_tool=None,
              retry_backoff_seconds=2, time_budget_seconds=None, system_prompt=None,
              on_incomplete=None, trace=False):
    """The agent loop. `client` is injectable so tests can run it without a network.

    time_budget_seconds is a wall-clock ceiling checked BEFORE each model call,
    so a run cannot quietly overrun a live demonstration slot. It is a second,
    independent ceiling alongside max_steps -- steps bound how many decisions
    the model may make, seconds bound how long the user waits, and either can
    bite first.

    on_incomplete(reason) is called when the loop ends WITHOUT finishing, so a
    caller can still persist whatever real work was completed (e.g. export an
    explicitly-labelled partial report). It must never be used to dress a
    budget-limited run up as a successful one.
    """
    model = model or os.environ.get('JOB_RADAR_MODEL') or DEFAULT_MODEL
    if tool_definitions is None:
        tool_definitions = tools.TOOL_DEFINITIONS
    if tool_registry is None:
        tool_registry = tools.TOOL_REGISTRY
    metrics = Metrics(model=model)
    system_prompt = system_prompt or SYSTEM_PROMPT

    def incomplete(status, problem, **extra):
        """Single exit path for every unfinished run, so the partial-save hook
        can never be forgotten on one branch and remembered on another."""
        metrics.stop_reason = status
        payload = {'status': status, 'problem': problem,
                   'required_outcome_met': (required_outcome_tool in metrics.successful_tools
                                            if required_outcome_tool else None),
                   'transcript': transcript, 'metrics': metrics.as_dict()}
        payload.update(extra)
        if on_incomplete is not None:
            try:
                payload['partial_save'] = on_incomplete(status)
            except Exception as e:                       # a failed save must not mask the real stop
                payload['partial_save_error'] = '%s: %s' % (type(e).__name__, str(e)[:120])
        return payload

    # Imported unconditionally (not just when client is None) because the
    # retry loop below needs anthropic.APIConnectionError as a real type to
    # catch even when a test injects its own client double.
    try:
        import anthropic
    except ImportError:
        raise RuntimeError('The `anthropic` package is not installed. Run: py -m pip install -r requirements.txt')

    if client is None:
        problem = api_key_problem()
        if problem:
            raise RuntimeError(problem + '\nEdit the .env file next to agent.py, then re-run. '
                                         'Never paste the key into a chat.')
        client = anthropic.Anthropic()

    messages = [{'role': 'user', 'content': task}]
    transcript = []

    # Prompt caching. Every step resends the whole conversation, so input cost
    # grows quadratically with step count: a measured 9-step run billed 274,000
    # input tokens for roughly 30,000 tokens of distinct content. Marking the
    # static prefix (this system prompt, which also carries the tool rules)
    # lets later steps re-read it at the cache rate instead of the full input
    # rate. Degrades safely: if the API ignores cache_control, the run is
    # simply billed as before.
    cached_system = [{'type': 'text', 'text': system_prompt,
                      'cache_control': {'type': 'ephemeral'}}]

    for step in range(1, max_steps + 1):
        # ---- STEP 0: wall-clock ceiling, checked BEFORE spending another call ----
        if time_budget_seconds is not None and metrics.elapsed() >= time_budget_seconds:
            return incomplete(
                'time_budget_exceeded',
                'Stopped after %.1fs against a %.0fs budget, before step %d. This run is '
                'INCOMPLETE: do not present it as a finished result.'
                % (metrics.elapsed(), time_budget_seconds, step))

        if verbose and trace:
            print('\n----- STEP %d -----' % step)

        # ---- STEP 1: call the model with the conversation so far + our tools ----
        # A real, measured gap (2026-10-04): a transient connection drop here
        # raised anthropic.APIConnectionError, which nothing caught -- it
        # crashed the whole process after tool calls had already made real,
        # verified progress on a live form. At most 2 retries (bounded, per
        # project policy against unbounded retry loops) with short backoff,
        # ONLY for errors that are genuinely about not having reached the
        # server -- never for an auth or bad-request error, which retrying
        # cannot fix and would just repeat.
        response = None
        last_error = None
        for attempt in range(3):
            try:
                metrics.api_requests += 1
                if attempt:
                    metrics.api_retries += 1
                response = client.messages.create(
                    model=model,
                    max_tokens=DEFAULT_MAX_TOKENS,
                    system=cached_system,
                    tools=tool_definitions,
                    messages=messages,
                )
                break
            except (anthropic.APIConnectionError, anthropic.RateLimitError) as e:
                # Both are genuinely transient: we either never reached the
                # server, or were told to slow down. RateLimitError must be
                # caught BEFORE APIStatusError below, since it is a subclass.
                last_error = e
                if attempt < 2:
                    if verbose:
                        print('[step %d] %s (attempt %d/3), retrying: %s'
                              % (step, type(e).__name__, attempt + 1, e))
                    time.sleep(retry_backoff_seconds * (attempt + 1))
            except anthropic.APIStatusError as e:
                # Auth, billing, bad request, model-not-found, permissions.
                # Retrying cannot fix any of these, so fail fast with an
                # ACTIONABLE message instead of a traceback. This was a real
                # measured failure: an exhausted credit balance surfaced as a
                # raw anthropic.BadRequestError stack trace mid-run, which
                # says nothing about what the operator should actually do,
                # and buried the fact that the live browser session was
                # still intact and nothing had been lost.
                metrics.stop_reason = 'api_unavailable'
                return {'status': 'api_unavailable',
                        'problem': _api_status_guidance(e),
                        'error_type': type(e).__name__,
                        'http_status': getattr(e, 'status_code', None),
                        'transcript': transcript, 'metrics': metrics.as_dict()}
        if response is None:
            return incomplete('connection_failed',
                              'Could not reach the Anthropic API after 3 attempts: %s. No tool call '
                              'was lost -- everything verified before this point is still on the live '
                              'page. Re-run once connectivity is back; do not loop retrying.' % last_error)
        metrics.api_calls += 1
        metrics.api_stop_reason = getattr(response, 'stop_reason', None)
        usage = getattr(response, 'usage', None)
        step_in = (getattr(usage, 'input_tokens', 0) or 0) if usage is not None else 0
        step_out = (getattr(usage, 'output_tokens', 0) or 0) if usage is not None else 0
        step_cw = (getattr(usage, 'cache_creation_input_tokens', 0) or 0) if usage is not None else 0
        step_cr = (getattr(usage, 'cache_read_input_tokens', 0) or 0) if usage is not None else 0
        metrics.input_tokens += step_in
        metrics.output_tokens += step_out
        metrics.cache_write_tokens += step_cw
        metrics.cache_read_tokens += step_cr

        # ---- STEP 2: read what the model said and which tools it wants ----
        said = [b.text for b in response.content if getattr(b, 'type', None) == 'text']
        tool_uses = [b for b in response.content if getattr(b, 'type', None) == 'tool_use']
        for text in said:
            transcript.append({'step': step, 'role': 'assistant', 'text': text})
        if verbose and trace:
            spoken = '\n'.join(t.strip() for t in said if t.strip())
            print('Agent:')
            if spoken:
                print(spoken)
            for block in tool_uses:
                print('ACTION: %s(%s)' % (block.name, _fmt_args(block.input)))
            usd, _ = metrics.cost()
            cache_bit = ''
            if step_cw or step_cr:
                cache_bit = ' | cache +%s w / %s r' % (format(step_cw, ','), format(step_cr, ','))
            print('[cost] step %d: in %s / out %s tok%s  | running total %s in + %s out  = %s'
                  % (step, format(step_in, ','), format(step_out, ','), cache_bit,
                     format(metrics.input_tokens, ','), format(metrics.output_tokens, ','),
                     ('$%.4f' % usd) if usd is not None else 'cost unknown'))
        elif verbose:
            for text in said:
                if text.strip():
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
            if verbose and not trace:
                print('[step %d] tool -> %s(%s)' % (step, block.name,
                      json.dumps(block.input, ensure_ascii=False)[:160]))
            observation, is_error = tools.execute_tool(block.name, block.input, registry=tool_registry)
            if verbose and trace:
                print('\nObservation%s:' % (' (TOOL ERROR)' if is_error else ''))
                print(_fmt_observation(observation))
            metrics.tool_calls += 1
            if is_error:
                metrics.tool_failures += 1
                if verbose and not trace:
                    print('[step %d]   !! FAILED: %s' % (step, observation.get('error', '')[:200]))
            else:
                metrics.successful_tools.add(block.name)
                if verbose and not trace:
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
        # Move a cache breakpoint to the newest tool_result each turn, so the
        # conversation built up so far is read from cache on the next call
        # rather than re-billed in full.
        #
        # The API allows a MAXIMUM OF 4 cache_control blocks per request, and
        # that budget is counted across the WHOLE request -- system prompt plus
        # every message. Clearing only the current turn's blocks left the
        # previous turns' breakpoints in place, and a measured run died at step
        # 5 with "A maximum of 4 blocks with cache_control may be provided.
        # Found 5." So strip every prior breakpoint first: one on the system
        # prompt plus one on the newest tool_result is all that is needed,
        # because a later breakpoint already caches everything before it.
        for prior in messages:
            content = prior.get('content')
            if isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict):
                        blk.pop('cache_control', None)
        for block in results_for_model:
            block.pop('cache_control', None)
        results_for_model[-1]['cache_control'] = {'type': 'ephemeral'}
        messages.append({'role': 'user', 'content': results_for_model})

    # ---- STEP 6b: explicit execution limit ----
    return incomplete('max_steps_reached',
                      'Hit the %d-step iteration ceiling before finishing. The task is '
                      'INCOMPLETE and must not be reported as completed.' % max_steps)


RADAR_MAX_STEPS = 25              # hard ceiling required for the graded demonstration
RADAR_TIME_BUDGET_SECONDS = 180   # wall-clock ceiling; the demo slot is five minutes total

RADAR_SYSTEM_PROMPT = """You are the judgment layer of Job Radar, a personal job-search assistant \
for one candidate. Deterministic Python tools retrieve data, check the application ledger, validate \
evidence, build the report and open URLs. YOU decide what is worth applying to and in what order. \
No tool scores a job for you; that judgment is yours alone and must be argued from retrieved facts.

YOUR TASK, IN ORDER
1. Search live openings across the official employer APIs available to you.
2. Read requirement text for the candidates that look plausible -- BATCH them, several keys per
   get_posting_details call, because each call costs an API round trip.
3. Judge fit against the candidate's verified profile, then select UP TO FOUR opportunities.
4. Call export_report.
5. Call open_application_urls.
Steps 4 and 5 are the deliverable. A run that reasons well and never calls them has failed.

EFFICIENCY -- YOU HAVE AT MOST 25 STEPS AND ABOUT 3 MINUTES
- Pull profile facts ONCE, naming only the sections you need. Do not re-request them.
- Batch posting lookups. One call with six keys beats six calls with one.
- Do not re-read a posting you already have. Do not ask for full_text unless an excerpt genuinely
  left out something decision-relevant.
- Stop as soon as you have four good selections, or fewer if fewer qualify.
- Search once with a broad keyword list unless the first search genuinely failed. Prefer a SINGLE
  short search_text term ("analyst"); several joined terms match almost nothing on Workday.
- When you check application history, always pass job_id -- the tool refuses a company name alone.

"PARTIAL" MEANS THE RUN WAS CUT SHORT -- nothing else. Pass partial=true only if you ran out of
steps or time or a source failed, so work you meant to do did not happen. Narrow coverage is NOT
partial: this tool only ever reaches five employers and that is a known, documented limit.
Unresolved questions are NOT partial either; they are the normal output of honest assessment and
belong in unresolved_questions. If you did what you set out to do, the run is COMPLETE even if the
answer is "only two of these are worth applying to".

EVIDENCE RULES
- Every selection needs a quote copied VERBATIM from the retrieved posting. It is checked against
  the real text and rejected if it is not there. Never paraphrase inside a quote.
- Only use facts returned by tools. Never invent an employer, date, requisition, credential or
  duration of experience.
- A null or absent field is UNKNOWN -- not false, not zero.
- A posting appearing in search results means the employer's API listed it just now. That is NOT
  proof the vacancy still accepts applicants. Say so when it matters.

JUDGMENT RULES
- MISSING EVIDENCE IS NOT EVIDENCE OF FAILURE. Rule a posting out only when retrieved facts
  establish an actual shortfall or an explicit incompatibility. When the posting simply does not
  establish whether a requirement is met, that is an UNRESOLVED QUESTION -- record it in
  unresolved_questions and let the selection stand or fall on everything else.
- Distinguish REQUIRED from PREFERRED. Operations and leadership experience is real professional
  experience, but it does not by itself satisfy a stated requirement for years of analytics tenure.
- Never output a percentage, score or hiring-probability estimate.
- Do NOT weaken your standards to reach four selections. Three good ones beat four padded ones, and
  zero is a legitimate answer if nothing qualifies.

WORK AUTHORIZATION -- keep four things apart: (a) current authorization, (b) future sponsorship
need, (c) current OPT status, (d) the employer's actual stated policy. An explicit "no sponsorship
now or in the future" clause conflicts with this candidate's confirmed future sponsorship need and
is a genuine exclusion. An employer's silence on sponsorship is UNCERTAIN, never an auto-reject.

STANDING EXCLUSIONS: exclude the MBA and unsupported hospitality experience. USD 50,000 is an
expectation to state if asked, NEVER a filter for excluding jobs. Do not infer relocation
willingness or an immediate start date.

DUPLICATES: selecting a requisition already in the ledger is a defect. The tool blocks it; do not
fight the block, pick something else.

AVAILABILITY: if select_opportunity returns availability_verified=false, that requisition has
official evidence disagreeing with itself about whether it still accepts applicants. It is KEPT
and reported under Needs Verification, but it is NOT application-ready and no tab will open for
it. Do not argue with that, do not re-select it, and SAY so plainly in your closing summary --
naming the requisition and why it is unresolved. Then select another opportunity if a slot remains.

FINISHING: call export_report once, then open_application_urls once. open_application_urls opens
only application-ready rows, at most four, and is idempotent -- calling it twice opens nothing the
second time. Do not call it before export_report has succeeded.

FINISH by stating your prioritised decisions with evidence, what remains unresolved, what you could
not cover, and where the report was saved."""


def build_radar_task(max_steps, budget, user_task=None):
    return (
        (user_task or
         'Review the job market for me across the official employer sources you can reach. '
         'Tell me which openings I should prioritise, which need clarification before I apply, '
         'and which to skip. Explain the evidence and avoid duplicate applications.')
        + '\n\nOperating limits for this run: at most %d agent steps and about %d seconds of '
          'wall clock. Budget them. Finish by exporting the report and opening the application '
          'URLs for the opportunities you selected.' % (max_steps, budget))


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

    radar = sub.add_parser('radar', help='Discover live openings from official employer APIs, '
                                         'judge them, export a dated Excel report, and open the '
                                         'selected application pages. Never fills a form.')
    radar.add_argument('--task', default=None, help='Your request in your own words. A sensible '
                                                    'default is used when omitted.')
    radar.add_argument('--model', default=None)
    radar.add_argument('--max-steps', type=int, default=RADAR_MAX_STEPS,
                       help='Hard iteration ceiling (default %d).' % RADAR_MAX_STEPS)
    radar.add_argument('--time-budget', type=int, default=RADAR_TIME_BUDGET_SECONDS,
                       help='Wall-clock ceiling in seconds (default %d).' % RADAR_TIME_BUDGET_SECONDS)
    radar.add_argument('--env-file', default='.env')
    radar.add_argument('--no-open', action='store_true',
                       help='Do everything except actually opening browser tabs.')
    radar.add_argument('--force-open', action='store_true',
                       help='Re-open application pages even if they were already opened earlier '
                            'today. Use this if you rehearsed this morning and want fresh tabs '
                            'for the live demonstration.')
    radar.add_argument('--json', action='store_true')

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
    run_kwargs = {}
    if args.command == 'radar':
        import radar_tools
        radar_tools.reset_run()
        if args.no_open:
            radar_tools.TOOL_REGISTRY['open_application_urls'] = lambda keys=None, force=False: {
                'requested': [], 'requested_count': 0,
                'note': 'Opening suppressed by --no-open; the URLs are in the report.'}
        elif args.force_open:
            # Same-day idempotency is the right default, but it makes a second
            # rehearsal open nothing -- which is confusing in front of an
            # audience. This is the explicit opt-out the user asks for.
            _open = radar_tools.open_application_urls
            radar_tools.TOOL_REGISTRY['open_application_urls'] = (
                lambda keys=None, force=False: _open(keys=keys, force=True))
        task = build_radar_task(args.max_steps, args.time_budget, args.task)
        definitions, registry = build_toolset('radar')
        # "The model stopped talking" is not success. This run is only done if
        # the workbook was actually written.
        required_outcome = 'export_report'
        run_kwargs = {
            'system_prompt': RADAR_SYSTEM_PROMPT,
            'time_budget_seconds': args.time_budget,
            'trace': True,
            # Preserve whatever real work was completed when a ceiling bites.
            # The report is written with partial=True so the file itself says
            # it is incomplete -- a budget-limited run must never be filed as
            # a finished one.
            'on_incomplete': lambda reason: radar_tools.export_report(
                summary='Run stopped early (%s) before the model finished.' % reason,
                partial=True, partial_reason=reason),
        }
        print('=' * 70)
        print('JOB RADAR AGENT  |  model=%s  |  max steps=%d  |  time budget=%ds'
              % (args.model or os.environ.get('JOB_RADAR_MODEL') or DEFAULT_MODEL,
                 args.max_steps, args.time_budget))
        print('Task: %s' % (args.task or 'Review live openings and tell me what to prioritise.'))
        print('=' * 70)
    elif args.command == 'assess':
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
                           required_outcome_tool=required_outcome, **run_kwargs)
    except RuntimeError as e:
        print('ERROR: %s' % e, file=sys.stderr)
        sys.exit(1)

    if args.command == 'radar':
        print('\n' + '=' * 70)
        print('FINAL ANSWER:')
        print(result.get('final_text') or '(the model produced no closing summary)')

    print('\n' + '=' * 70)
    print('status: %s' % result['status'])
    if result.get('problem'):
        print('PROBLEM: %s' % result['problem'])
    if result.get('required_outcome_met') is not None:
        print('required outcome (%s) met: %s' % (required_outcome, result['required_outcome_met']))
    if result['status'] == 'needs_human_input':
        q = result['question']
        print('\nThe agent needs one fact from you:\n  Q: %s\n  why: %s' % (q['question'], q['why_needed']))
    if args.command == 'radar':
        import radar_tools
        run = radar_tools.RUN
        # Verify the deliverable ON DISK. The tool reporting success is not
        # proof the file exists -- "no more tool calls" and "the tool said ok"
        # are both weaker than looking.
        path = Path(run.report_path) if run.report_path else None
        exists = bool(path and path.exists())
        ready = radar_tools.application_ready()
        held = radar_tools.needs_verification()
        print('\nAPPLICATION-READY (%d):' % len(ready))
        for s in sorted(ready, key=lambda x: str(x.get('priority'))):
            print('  %s. %s -- %s [%s]' % (s.get('priority'), s.get('company'),
                                           s.get('title'), s.get('job_id')))
            print('     %s' % s.get('url'))
            for q in (s.get('unresolved_questions') or []):
                print('     ? %s' % q)
        if held:
            print('\nNEEDS VERIFICATION (%d) -- assessed, but NOT application-ready, no tab opened:'
                  % len(held))
            for s in held:
                print('  %s -- %s [%s]' % (s.get('company'), s.get('title'), s.get('job_id')))
                print('     why: %s' % (s.get('availability_note') or 'availability unverified'))

        print('\nSEARCH COVERAGE (what was actually reached):')
        for c in run.coverage:
            print('  %-11s %-12s reached=%-3s scanned=%-4s kept=%s%s'
                  % (c['source'], c['company'] or '', 'yes' if c['reached'] else 'NO',
                     c['returned_by_source'], c['kept_after_filter'],
                     '  ERROR: %s' % c['error'] if c.get('error') else ''))
        print('\nCOUNTS: %d listings scanned | %d postings fully evaluated (requirement text read)'
              % (run.scanned_count(), len(run.details)))
        print('\nREPORT: %s' % (run.report_path or 'NOT WRITTEN'))
        print('  exists on disk: %s%s' % (exists,
              ' (%d bytes)' % path.stat().st_size if exists else ''))

        # Size proves nothing. Read the workbook back and require that its rows
        # and the URLs we actually requested agree EXACTLY, or say they do not.
        if exists:
            check = radar_tools.verify_export_matches_open_set()
            print('  workbook rows: %d  requisitions: %s'
                  % (check.get('workbook_rows', 0), check.get('workbook_requisitions')))
            already = check.get('urls_already_open_from_earlier_today') or []
            print('  rows match pages requested to open: %s%s'
                  % (check.get('exact_agreement'),
                     '  (%d already open from earlier today)' % len(already) if already else ''))
            for problem in check.get('problems') or []:
                print('  !! %s' % problem)
            if not check.get('ok'):
                print('  !! CONTENT CHECK FAILED -- this run must not be reported as complete.')

        print('\nBROWSER OPEN REQUESTS: %d (cap %d)'
              % (len(run.opened_urls), radar_tools.MAX_SELECTIONS))
        for e in run.opened_urls:
            print('  %s  os_handoff_accepted=%s' % (e['url'], e.get('os_handoff_accepted')))
        print('  note: an accepted OS handoff is not proof the page loaded.')

        m = result['metrics']
        print('\nAPI USAGE (all four input categories are separate):')
        print('  uncached input tokens   : %s' % format(m['input_tokens'], ','))
        print('  cache CREATION input    : %s' % format(m['cache_write_tokens'], ','))
        print('  cache READ input        : %s' % format(m['cache_read_tokens'], ','))
        print('  output tokens           : %s' % format(m['output_tokens'], ','))
        print('  total input billed      : %s  (uncached + creation + read)'
              % format(m['input_tokens'] + m['cache_write_tokens'] + m['cache_read_tokens'], ','))
        print('  estimated cost          : %s'
              % ('$%.4f' % m['estimated_cost_usd'] if m['estimated_cost_usd'] is not None
                 else 'unknown'))
        print('  rates                   : %s' % m['pricing_note'])
        print('  API requests %d (loop iterations %d, retries %d) | tool calls %d (failures %d)'
              % (m['api_requests'], m['api_calls'], m['api_retries'], m['tool_calls'],
                 m['tool_failures']))

    print('\nmetrics: %s' % json.dumps(result['metrics']))
    # A run that did not achieve its outcome must not look like a success to a
    # shell script, a CI job, or a tired human reading the last line.
    if result['status'] != 'completed':
        sys.exit(2)
    if args.json:
        print('\n' + json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
    main()
