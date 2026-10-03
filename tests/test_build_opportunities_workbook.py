"""Regression tests for private/build_opportunities_workbook.py, inverting
probes 8-10 from the independent review's job_radar_audit_probes.py: those
probes intentionally asserted the BUGGY behavior to prove the defects
existed. These assert the CORRECTED, desired behavior -- a passing test
here means the bug is gone. All data below is fictional.
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('build_opportunities_workbook', ROOT / 'private' / 'build_opportunities_workbook.py')
workbook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workbook)


class HotelCompanyMergeRegression(unittest.TestCase):
    """Probe 8: a generic 2-shared-word name matcher merged four distinct
    hotel companies (Shangri-La/Capella/Four Seasons/Rosewood, all sharing
    only "Hotels" and "Resorts") into a single Company Coverage row.
    build_registry_rows now uses REGISTRY_KEY_TO_TARGET_NAME, an explicit
    canonical map, with no generic name-token matching at all."""

    def test_four_distinct_hotel_targets_stay_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / 'registry.json'
            targets = Path(tmp) / 'targets.json'
            registry.write_text(json.dumps({'companies': {}}))
            names = ['Shangri-La Hotels & Resorts', 'Capella Hotels & Resorts',
                     'Four Seasons Hotels & Resorts', 'Rosewood Hotels & Resorts']
            targets.write_text(json.dumps({'priority_companies': [{'name': n} for n in names]}))
            with patch.object(workbook, 'REGISTRY_PATH', registry), patch.object(workbook, 'TARGET_PATH', targets):
                rows = workbook.build_registry_rows()
            self.assertEqual(len(rows), 4, 'four distinct target companies must produce four distinct rows')
            self.assertEqual({r['company'] for r in rows}, set(names))


class TrackerReconciliationRegression(unittest.TestCase):
    """Probes 9-10: the tracker must be built FROM private/state.json's
    authoritative applications ledger, joined to registry/job metadata --
    never the other way around (iterating the registry and trusting its
    own application_status field)."""

    def test_registry_applied_claim_without_ledger_backing_is_not_shown_as_applied(self):
        """Probe 9, inverted: a registry entry claiming APPLIED with an
        EMPTY state.json applications dict must never surface as a
        confirmed APPLIED row -- the ledger is authoritative, not the
        registry's own unverified claim."""
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / 'registry.json'
            state = Path(tmp) / 'state.json'
            registry.write_text(json.dumps({'companies': {'fictional': {
                'company': 'Fictional Employer', 'business_unit': 'Example role',
                'application_status': {'state': 'APPLIED', 'provenance': 'Synthetic claim'}}}}))
            state.write_text(json.dumps({'jobs': {}, 'applications': {}}))
            with patch.object(workbook, 'REGISTRY_PATH', registry), patch.object(workbook, 'STATE_PATH', state):
                rows = workbook.build_tracker_rows()
            applied_rows = [r for r in rows if r.get('company') == 'Fictional Employer' and r.get('status') == 'APPLIED']
            self.assertEqual(applied_rows, [], 'an unbacked registry claim must never show as a confirmed APPLIED row')
            # The gap itself should still be visible, just not as APPLIED.
            flagged = [r for r in rows if r.get('company') == 'Fictional Employer']
            self.assertTrue(flagged, 'the unverified claim should still be surfaced, just not trusted as APPLIED')
            self.assertIn('UNVERIFIED', flagged[0]['status'])

    def test_confirmed_ledger_submission_is_never_absent_from_tracker(self):
        """Probe 10, inverted: a REAL confirmed submission in state.json's
        applications dict must appear in the tracker even when the
        registry has no entry for that company at all."""
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / 'registry.json'
            state = Path(tmp) / 'state.json'
            registry.write_text(json.dumps({'companies': {}}))
            state.write_text(json.dumps({'jobs': {'fictional-key': {'posting': {
                'company': 'Fictional Employer', 'role': 'Example role'}}},
                'applications': {'fictional-key': {'date_applied': '2026-09-28'}}}))
            with patch.object(workbook, 'REGISTRY_PATH', registry), patch.object(workbook, 'STATE_PATH', state):
                rows = workbook.build_tracker_rows()
            matches = [r for r in rows if r.get('company') == 'Fictional Employer']
            self.assertTrue(matches, 'a confirmed ledger submission must never be silently absent from the tracker')
            self.assertEqual(matches[0]['status'], 'APPLIED')
            self.assertIn('fictional-key', matches[0]['provenance'])


class SponsorshipReclassificationRegression(unittest.TestCase):
    """Not one of the 10 numbered probes, but the review's own finding #6:
    a row whose employer text excludes candidates needing sponsorship "now
    or in the future" is a CONFIRMED conflict against Andres's settled
    future_sponsorship_required fact, not an open "sponsorship unknown"
    question -- must be reclassified out of NEEDS_VERIFICATION."""

    def test_now_or_future_exclusion_is_reclassified_as_excluded(self):
        row = {'decision': 'NEEDS_VERIFICATION', 'gaps': 'Nothing else unresolved.',
               'sponsorship_statement': 'We will not sponsor a visa now or in the future.',
               'next_action': 'solely because of sponsorship'}
        workbook.reclassify_sponsorship([row], future_sponsorship_required=True)
        self.assertEqual(row['decision'], 'EXCLUDED')
        self.assertFalse(row['sponsorship_only_gap'])
        self.assertIn('RECLASSIFIED', row['gaps'])

    def test_row_with_other_gap_is_not_flagged_sponsorship_only(self):
        row = {'decision': 'NEEDS_VERIFICATION',
               'gaps': 'SQL experience is under one year against the required 1 year minimum. Sponsorship unaddressed.',
               'sponsorship_statement': 'Unknown', 'next_action': 'on that basis alone'}
        workbook.reclassify_sponsorship([row], future_sponsorship_required=True)
        self.assertEqual(row['decision'], 'NEEDS_VERIFICATION')
        self.assertFalse(row['sponsorship_only_gap'], 'a row with a separate real gap must not be flagged sponsorship-only')

    def test_row_with_only_unread_sponsorship_stays_flagged(self):
        row = {'decision': 'NEEDS_VERIFICATION',
               'gaps': 'Full posting text (incl. sponsorship language) could not be read -- genuinely unverified, not rejected.',
               'sponsorship_statement': 'Unknown -- unverified.', 'next_action': ''}
        workbook.reclassify_sponsorship([row], future_sponsorship_required=True)
        self.assertEqual(row['decision'], 'NEEDS_VERIFICATION')
        self.assertTrue(row['sponsorship_only_gap'])


if __name__ == '__main__':
    unittest.main()
