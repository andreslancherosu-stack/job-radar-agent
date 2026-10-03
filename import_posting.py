"""Job discovery/import: fetch a real posting URL and draft a posting.json.

Deterministic, stdlib-only HTTP fetch + best-effort HTML-to-text extraction.
This is the "direct employer or official ATS URL" entry point from the
product scope. It does NOT judge fit (that is radar.py's `assess`, driven by
a chat session or llm_client.py) and it does NOT assume the fetched page
means the role is currently open — `open` is left null for a human/LLM to
set after actually reading the extracted text, matching the existing rule
in AGENT_INSTRUCTIONS.md: "Only record open=true and verified_on when
checked."

Known limitation: this uses a plain HTTP GET, not a browser. Many modern
ATS platforms (Workday, some Greenhouse/iCIMS pages) render the posting
body via JavaScript, which this fetch will not execute — the extracted
text can come back thin or empty. `looks_incomplete()` flags that case so
you fall back to pasting the text manually (still fully supported by
radar.py's `user_copy` source_kind), rather than silently ingesting a
near-empty posting.

Python 3.10+. Standard library only — no new dependency for this module.
"""
import argparse
import hashlib
import json
import re
import urllib.error
import urllib.request
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

USER_AGENT = ('Mozilla/5.0 (compatible; JobRadarImport/0.1; '
              '+https://github.com/) personal-use fetch for one job posting at a time')

DRAFTS_DIR = Path('private/postings_drafts')


class _VisibleTextExtractor(HTMLParser):
    """Collects visible text, dropping script/style/head content and tags."""

    SKIP_TAGS = {'script', 'style', 'head', 'noscript', 'template'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.chunks = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0 and data.strip():
            self.chunks.append(data.strip())


def html_to_text(html):
    """Best-effort visible-text extraction. Not a layout-aware parser —
    good enough to hand a posting's text to a human or an LLM for judgment,
    not a claim of pixel-perfect page reproduction."""
    parser = _VisibleTextExtractor()
    parser.feed(html)
    text = '\n'.join(parser.chunks)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def looks_incomplete(text, min_chars=200):
    """Heuristic only — flags likely JS-rendered or blocked pages so a human
    reviews before trusting the extraction. Never treated as certainty."""
    warnings = []
    if len(text) < min_chars:
        warnings.append('Extracted text is short (%d chars) — the page may render content '
                         'via JavaScript this fetch cannot execute, or may have blocked the '
                         'request. Verify manually or paste the posting text instead.' % len(text))
    lowered = text.lower()
    for marker in ('enable javascript', 'please enable js', 'you need to enable javascript',
                   'captcha', 'access denied', 'are you a robot'):
        if marker in lowered:
            warnings.append('Extracted text contains "%s" — this page likely was not '
                             'fully served by a plain HTTP fetch.' % marker)
    return warnings


def fetch_url(url, timeout=15):
    """Returns (final_url, status_code, html_text). Raises urllib.error.URLError
    or urllib.error.HTTPError on failure — callers should report the specific
    error rather than retry silently or fabricate content."""
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or 'utf-8'
        html = raw.decode(charset, errors='replace')
        return response.geturl(), response.status, html


def slug_for(company, role):
    base = re.sub(r'[^a-z0-9]+', '-', (company + '-' + role).lower()).strip('-')
    return base[:60] or hashlib.sha256((company + role).encode()).hexdigest()[:12]


def build_draft_posting(url, company, role, job_id, text, retrieved_day, warnings):
    return {
        'company': company,
        'role': role,
        'job_id': job_id or None,
        'source_url': url,
        'source_kind': 'employer',
        'text': text,
        # Deliberately left for a human/LLM to set after reading `text` —
        # a 200 response proves the page loaded, not that the role is open.
        'open': None,
        'verified_on': None,
        'deadline': None,
        'human_verified': False,
        '_fetch_metadata': {
            'retrieved_on': retrieved_day,
            'method': 'plain_http_get_no_js',
            'warnings': warnings,
        },
    }


def import_url(url, company, role, job_id, out_path=None, timeout=15, day=None):
    day = day or date.today().isoformat()
    final_url, status, html = fetch_url(url, timeout=timeout)
    text = html_to_text(html)
    warnings = looks_incomplete(text)
    posting = build_draft_posting(final_url, company, role, job_id, text, day, warnings)
    out_path = Path(out_path) if out_path else DRAFTS_DIR / (slug_for(company, role) + '.json')
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(posting, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return {'draft_file': str(out_path), 'status': status, 'text_length': len(text), 'warnings': warnings}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    fetch_cmd = sub.add_parser('fetch', help='Fetch a URL and write a draft posting.json for review.')
    fetch_cmd.add_argument('url')
    fetch_cmd.add_argument('--company', required=True)
    fetch_cmd.add_argument('--role', required=True)
    fetch_cmd.add_argument('--job-id', default=None)
    fetch_cmd.add_argument('--out', default=None)
    args = parser.parse_args()
    if args.command == 'fetch':
        try:
            result = import_url(args.url, args.company, args.role, args.job_id, args.out)
        except (urllib.error.URLError, TimeoutError) as e:
            print(json.dumps({'error': 'Fetch failed: %s' % e}))
            raise SystemExit(1)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print('\nDraft written. Review/edit the file, set open/verified_on/human_verified '
              'once you have actually checked the page, then run:\n'
              '  python radar.py ingest %s' % result['draft_file'])


if __name__ == '__main__':
    main()
