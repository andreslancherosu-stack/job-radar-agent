import tempfile
import unittest
from pathlib import Path

import answers


class Normalize(unittest.TestCase):
    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(answers.normalize('  Are You   Authorized?  '), 'are you authorized?')


class AddAndFind(unittest.TestCase):
    def setUp(self):
        self.bank = {'entries': []}

    def test_add_requires_source(self):
        with self.assertRaises(ValueError):
            answers.add_answer(self.bank, 'Q', 'A', '', True)

    def test_add_rejects_blank_question_or_answer(self):
        with self.assertRaises(ValueError):
            answers.add_answer(self.bank, '  ', 'A', 'source', True)
        with self.assertRaises(ValueError):
            answers.add_answer(self.bank, 'Q', '  ', 'source', True)

    def test_confirmed_entry_is_found_by_exact_normalized_match(self):
        answers.add_answer(self.bank, 'Are you willing to relocate?', 'Yes', 'user confirmed', True, day='2026-09-01')
        match = answers.find_exact_match(self.bank, '  are you willing to relocate?  ')
        self.assertEqual(match['answer'], 'Yes')

    def test_unconfirmed_entry_is_never_matched(self):
        answers.add_answer(self.bank, 'Salary expectations?', '$X', 'draft', False)
        self.assertIsNone(answers.find_exact_match(self.bank, 'Salary expectations?'))

    def test_differently_worded_question_does_not_match(self):
        answers.add_answer(self.bank, 'Are you willing to relocate?', 'Yes', 'user confirmed', True)
        self.assertIsNone(answers.find_exact_match(self.bank, 'Would you consider relocating?'))

    def test_most_recent_confirmed_entry_wins_on_duplicate_question(self):
        answers.add_answer(self.bank, 'Available start date?', 'Immediately', 'old', True, day='2026-01-01')
        answers.add_answer(self.bank, 'Available start date?', 'June 2026', 'updated', True, day='2026-06-01')
        match = answers.find_exact_match(self.bank, 'Available start date?')
        self.assertEqual(match['answer'], 'June 2026')


class Persistence(unittest.TestCase):
    def test_round_trips_through_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bank.json'
            bank = answers.load_bank(path)
            answers.add_answer(bank, 'Q1', 'A1', 'source', True, day='2026-09-01')
            answers.save_bank(bank, path)
            reloaded = answers.load_bank(path)
            self.assertEqual(reloaded['entries'][0]['answer'], 'A1')


if __name__ == '__main__':
    unittest.main()
