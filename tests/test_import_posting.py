import json
import tempfile
import unittest
from pathlib import Path

import import_posting as ip

SAMPLE_HTML = """
<html><head><title>x</title><style>.x{color:red}</style>
<script>var x = 1;</script></head>
<body>
<nav>Home | Careers</nav>
<h1>Business Intelligence Intern</h1>
<p>Graduate students accepted. Python and Power BI required.</p>
<footer>&copy; 2026 Example Co</footer>
</body></html>
"""


class HtmlToText(unittest.TestCase):
    def test_strips_script_and_style(self):
        text = ip.html_to_text(SAMPLE_HTML)
        self.assertNotIn('color:red', text)
        self.assertNotIn('var x = 1', text)

    def test_keeps_visible_text(self):
        text = ip.html_to_text(SAMPLE_HTML)
        self.assertIn('Business Intelligence Intern', text)
        self.assertIn('Graduate students accepted.', text)

    def test_collapses_whitespace(self):
        text = ip.html_to_text('<p>a</p>\n\n\n\n<p>b</p>')
        self.assertNotIn('\n\n\n', text)


class LooksIncomplete(unittest.TestCase):
    def test_flags_short_text(self):
        warnings = ip.looks_incomplete('short')
        self.assertTrue(any('short' in w.lower() for w in warnings))

    def test_flags_js_marker(self):
        warnings = ip.looks_incomplete('Please enable JavaScript to view this page. ' * 10)
        self.assertTrue(any('enable javascript' in w.lower() for w in warnings))

    def test_no_warnings_for_normal_posting(self):
        text = ('We are hiring a Business Intelligence Intern. ' * 10 +
                'Graduate students accepted. Python and Power BI required.')
        self.assertEqual(ip.looks_incomplete(text), [])


class BuildDraftPosting(unittest.TestCase):
    def test_leaves_open_and_verified_on_null(self):
        posting = ip.build_draft_posting('https://x.example/job', 'Acme', 'Analyst',
                                          'REQ-1', 'some text', '2026-09-24', [])
        self.assertIsNone(posting['open'])
        self.assertIsNone(posting['verified_on'])
        self.assertFalse(posting['human_verified'])
        self.assertEqual(posting['source_kind'], 'employer')

    def test_matches_radar_required_fields(self):
        import radar
        posting = ip.build_draft_posting('https://x.example/job', 'Acme', 'Analyst',
                                          None, 'some text', '2026-09-24', [])
        state = radar.empty()
        # radar.ingest only rejects missing/blank company/role/text/source_url
        # and an invalid source_kind — a fetched draft must satisfy both.
        result = radar.ingest(state, posting, '2026-09-24')
        self.assertIn('key', result)


class Slug(unittest.TestCase):
    def test_slug_is_filesystem_safe(self):
        slug = ip.slug_for('Acme & Sons, Inc.', 'Business/Intel Intern')
        self.assertRegex(slug, r'^[a-z0-9-]+$')


class ImportUrlWithoutNetwork(unittest.TestCase):
    def test_writes_draft_file_from_injected_fetch(self):
        # Exercise import_url's file-writing/warning-collection logic without
        # touching the network, by monkeypatching fetch_url.
        original = ip.fetch_url
        ip.fetch_url = lambda url, timeout=15: (url, 200, SAMPLE_HTML)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / 'draft.json'
                result = ip.import_url('https://x.example/job', 'Example Co', 'BI Intern',
                                        'REQ-1', out_path=out, day='2026-09-24')
                self.assertEqual(result['draft_file'], str(out))
                saved = json.loads(out.read_text(encoding='utf-8'))
                self.assertIn('Business Intelligence Intern', saved['text'])
                self.assertEqual(saved['source_url'], 'https://x.example/job')
        finally:
            ip.fetch_url = original


if __name__ == '__main__':
    unittest.main()


class ClosedPostingDetection(unittest.TestCase):
    """Measured 2026-10-02: five of six Greenhouse postings found via search had
    been taken down and 200-OK'd to a careers index. The old heuristics passed
    all of them, so a landing page could be ingested as a job description."""

    POSTING = 'https://job-boards.greenhouse.io/acme/jobs/123'

    def test_redirect_to_board_is_flagged(self):
        warnings = ip.redirected_away_from_posting(
            self.POSTING, 'https://job-boards.greenhouse.io/acme', 'Current openings at Acme')
        self.assertTrue(any('REDIRECTED' in w for w in warnings))

    def test_index_like_text_is_flagged_even_without_a_redirect(self):
        warnings = ip.redirected_away_from_posting(
            self.POSTING, self.POSTING, 'View all jobs and join our team today')
        self.assertTrue(any('job INDEX' in w for w in warnings))

    def test_a_real_posting_is_not_flagged(self):
        warnings = ip.redirected_away_from_posting(
            self.POSTING, self.POSTING,
            'Back to jobs Data Analyst Remote Apply Minimum qualifications 2 or more years')
        self.assertEqual(warnings, [])
