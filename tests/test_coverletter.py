import tempfile
import unittest
from pathlib import Path

import coverletter


class DraftApproveGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        coverletter.DOCS_DIR = Path(self.tmp.name)

    def test_draft_rejects_empty_text(self):
        with self.assertRaises(ValueError):
            coverletter.draft('k1', 'Acme', 'Analyst', '   ', 'profile facts')

    def test_generate_refuses_before_approval(self):
        coverletter.draft('k1', 'Acme', 'Analyst', 'Dear Hiring Manager,\n\nI am interested.',
                           'profile facts', day='2026-09-24')
        with self.assertRaises(ValueError):
            coverletter.generate('k1')

    def test_approve_then_generate_produces_verified_docx(self):
        coverletter.draft('k1', 'Acme', 'Analyst', 'Dear Hiring Manager,\n\nI am interested.',
                           'profile facts', day='2026-09-24')
        coverletter.approve('k1', day='2026-09-24')
        record = coverletter.generate('k1')
        self.assertTrue(record['generated_verified'])
        self.assertTrue(Path(record['generated_docx_path']).exists())

    def test_show_unknown_key_raises(self):
        with self.assertRaises(ValueError):
            coverletter.load_record('does-not-exist')


if __name__ == '__main__':
    unittest.main()
