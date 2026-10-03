"""Optional standalone Anthropic API integration for job-fit assessment.

Why this file exists: today, the only way this project produces a real
assessment is inside an interactive chat with a tool-enabled assistant
(Claude Code or Copilot Agent) following AGENT_INSTRUCTIONS.md. That is a
legitimate architecture, but it means the *standalone* radar.py/apply.py
programs have never made their own call to an LLM. This module is a real,
separate integration: it calls the Anthropic Messages API directly, so the
assessment step can run without a live chat session, if you choose to set
it up.

It is optional and inert by default. Nothing in this repository calls it
automatically, and it never runs without ANTHROPIC_API_KEY set.

Setup (run in your own terminal, never pasted into a chat):
    setx ANTHROPIC_API_KEY "sk-ant-your-key-here"     # Windows, persists for new shells
    (open a new terminal so the variable takes effect)
    python llm_client.py check                        # reports whether a key is visible
    python llm_client.py assess private/postings_drafts/<file>.json

The key is read only from the environment. It is never written to, logged
by, or read from any file in this repository.

Output shape matches exactly what `radar.py assess` validates (the same
dimensions/status/quote/reason/fit/next_action schema used by a chat-drafted
assessment). This script does NOT call `radar.py assess` for you — review
its output, then run `python radar.py assess KEY FILE` yourself, exactly as
you would with a chat-drafted assessment. radar.py's evidence validator
(every quote must appear verbatim in the posting text) applies identically
regardless of whether a human, a chat session, or this script wrote it.

Known gap vs. the chat-driven path: this script does not read
private/history_reference.md (e.g. the Toyota-exclusion policy, prior
company history), to avoid folding tens of kilobytes of private history
into every API call by default. The chat-driven path in AGENT_INSTRUCTIONS.md
does account for that file. Treat an API-produced assessment as narrower
than a chat-produced one until this is revisited.

Python 3.10+. Requires the `anthropic` package (already in requirements.txt).
"""
import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_MODEL = os.environ.get('JOB_RADAR_ANTHROPIC_MODEL', 'claude-opus-5')

_DIMENSION_SCHEMA = {
    'type': 'object',
    'properties': {
        'status': {'type': 'string', 'enum': ['pass', 'uncertain', 'fail']},
        'quote': {'type': 'string',
                  'description': 'Exact substring copied verbatim from the posting text, or empty string only if status is uncertain.'},
        'reason': {'type': 'string'},
    },
    'required': ['status', 'quote', 'reason'],
    'additionalProperties': False,
}

ASSESSMENT_SCHEMA = {
    'type': 'object',
    'properties': {
        'fit': {'type': 'string', 'enum': ['GOOD FIT', 'STRETCH BUT REASONABLE', 'POOR FIT']},
        'next_action': {'type': 'string'},
        'experience': _DIMENSION_SCHEMA,
        'education': _DIMENSION_SCHEMA,
        'work_authorization': _DIMENSION_SCHEMA,
        'career_fit': _DIMENSION_SCHEMA,
    },
    'required': ['fit', 'next_action', 'experience', 'education', 'work_authorization', 'career_fit'],
    'additionalProperties': False,
}

# Condensed from AGENT_INSTRUCTIONS.md's "Judgment" section, which is public
# (committed) project policy — not private data. Deliberately does NOT
# include private/history_reference.md; see module docstring.
SYSTEM_PROMPT = """You are evaluating one real job posting against one candidate's verified \
profile, for a personal job-search tool. Be conservative and evidence-based.

Rules:
- Classify overall fit as GOOD FIT, STRETCH BUT REASONABLE, or POOR FIT. Never output a \
percentage score or a hiring-probability estimate.
- Distinguish required from preferred qualifications. Operations/leadership experience is \
valuable but does not by itself satisfy a requirement for years of professional data-science \
experience.
- Evaluate degree-population requirements literally (e.g. a graduate student does not \
automatically qualify for an undergraduate-only program).
- Evaluate exactly four dimensions: experience, education, work_authorization, career_fit. \
Each needs a status (pass/uncertain/fail), a `quote` that is an EXACT substring copied \
character-for-character from the posting text (never paraphrased, never invented — leave it \
an empty string only when status is uncertain), and a concise evidence-based `reason`.
- A missing sponsorship statement means work_authorization is uncertain, not pass. Do not \
treat a company's general sponsorship reputation as evidence for this specific posting. \
Explicit incompatible restrictions are fail; ambiguous wording is uncertain. Never infer \
current visa/CPT/OPT eligibility from a past internship, and never state a personal legal \
conclusion — only compare the posting's stated requirement to the profile's stated facts.
- If the profile leaves a fact unknown (null), do not guess it; treat dimensions that depend \
on it as uncertain.
- Provide a short next_action.

You will be given the posting text and the candidate profile as JSON. Only use facts present \
in those two inputs — never invent employers, dates, credentials, or requirements not stated \
in the posting."""


def available():
    return bool(os.environ.get('ANTHROPIC_API_KEY'))


def build_user_content(posting, profile):
    return (
        'POSTING\nsource_kind: %s\nsource_url: %s\n\n%s\n\n'
        'CANDIDATE PROFILE (JSON)\n%s\n\n'
        'Evaluate this posting against this profile per the system rules.'
    ) % (
        posting.get('source_kind', 'unknown'),
        posting.get('source_url', ''),
        posting.get('text', ''),
        json.dumps(profile, ensure_ascii=False, indent=2),
    )


def assess_with_llm(posting, profile, model=None, effort='medium'):
    if not available():
        raise RuntimeError('ANTHROPIC_API_KEY is not set. See llm_client.py module docstring for setup.')
    import anthropic  # imported here so `check`/schema tests need no network or key

    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model=model or DEFAULT_MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            output_config={'effort': effort, 'format': {'type': 'json_schema', 'schema': ASSESSMENT_SCHEMA}},
            messages=[{'role': 'user', 'content': build_user_content(posting, profile)}],
        )
    except anthropic.AuthenticationError as e:
        raise RuntimeError('Anthropic API rejected the key: %s' % e) from e
    except anthropic.RateLimitError as e:
        raise RuntimeError('Anthropic API rate-limited this request: %s' % e) from e
    except anthropic.APIStatusError as e:
        raise RuntimeError('Anthropic API returned an error (status %s): %s' % (e.status_code, e.message)) from e
    except anthropic.APIConnectionError as e:
        raise RuntimeError('Could not reach the Anthropic API: %s' % e) from e

    text = next((b.text for b in response.content if b.type == 'text'), None)
    if text is None:
        raise RuntimeError('No text content in the API response; got: %r' % (response.content,))
    return json.loads(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('check', help='Report whether ANTHROPIC_API_KEY is visible, without calling the API.')

    assess_cmd = sub.add_parser('assess')
    assess_cmd.add_argument('posting_file')
    assess_cmd.add_argument('--profile', default='private/profile.json')
    assess_cmd.add_argument('--model', default=None)
    assess_cmd.add_argument('--effort', default='medium', choices=['low', 'medium', 'high', 'xhigh', 'max'])

    args = parser.parse_args()

    if args.command == 'check':
        print(json.dumps({'anthropic_api_key_configured': available(), 'model': DEFAULT_MODEL}))
        return

    posting = json.loads(Path(args.posting_file).read_text(encoding='utf-8'))
    profile = json.loads(Path(args.profile).read_text(encoding='utf-8'))
    try:
        result = assess_with_llm(posting, profile, model=args.model, effort=args.effort)
    except RuntimeError as e:
        print(json.dumps({'error': str(e)}))
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
