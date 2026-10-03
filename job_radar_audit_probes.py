"""Offline audit of uploaded Job Radar code; never opens a browser.

Playwright and answers are absent from the review environment/package.
Import-only shims are explicit; no answer-bank behavior is tested.
Small locator doubles provide values to the ORIGINAL verification code.
Browser-flow doubles test state transitions, not real DOM/site behavior.
All writes are confined to temporary directories with fictional data.
These assertions document defects in the received version, not correctness.
"""
import argparse
import importlib.util
import inspect
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument('--source-dir', type=Path, default=Path(__file__).resolve().parent / 'job_radar')
audit_args, remaining_args = parser.parse_known_args()
sys.argv = [sys.argv[0], *remaining_args]
ROOT = audit_args.source_dir.resolve()
playwright = types.ModuleType('playwright')
sync_api = types.ModuleType('playwright.sync_api')

def no_browser():
    raise AssertionError('A real browser must never be opened by these probes')

sync_api.sync_playwright = no_browser
playwright.sync_api = sync_api
sys.modules['playwright'] = playwright
sys.modules['playwright.sync_api'] = sync_api
sys.modules['answers'] = types.ModuleType('answers')
spec = importlib.util.spec_from_file_location('audited_apply', ROOT / 'apply.py')
apply = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply)
workbook_path = ROOT / 'build_opportunities_workbook.py'
if not workbook_path.exists():
    workbook_path = ROOT / 'private' / 'build_opportunities_workbook.py'
wb_spec = importlib.util.spec_from_file_location('audited_workbook', workbook_path)
workbook = importlib.util.module_from_spec(wb_spec)
wb_spec.loader.exec_module(workbook)


class Field:
    def __init__(self, value=''):
        self.value = value

    def input_value(self):
        return self.value

    def fill(self, value):
        self.value = value

    def select_option(self, value):
        self.value = value


class Text:
    def __init__(self, value):
        self.value = value

    def inner_text(self):
        return self.value


class Collection:
    def __init__(self, items):
        self.items = items

    def count(self):
        return len(self.items)

    def nth(self, i):
        return self.items[i]


class Page:
    def __init__(self, field=None, body=''):
        self.field = field or Field()
        self.body = body
        self.url = 'about:blank'
        self.next_clicks = 0

    def get_by_label(self, *args, **kwargs):
        return self.field

    def locator(self, selector):
        if selector == 'body':
            return Text(self.body)
        if selector == '.error.visible':
            return Collection([])
        raise AssertionError(selector)

    def goto(self, url):
        self.url = url

    def get_by_role(self, role, **kwargs):
        assert role == 'button' and kwargs['name'] == 'Next'
        return self

    def click(self):
        # Deliberately no transition: e.g., client/native validation blocks Next.
        self.next_clicks += 1

    def wait_for_timeout(self, milliseconds):
        # No real wait: only used to exercise the code's elapsed-time branch.
        pass


class AuditProbes(unittest.TestCase):
    def test_gpa_32_instead_of_3_point_2_is_falsely_verified(self):
        page = Page(Field('32'))
        result = apply.verify_filled_fields(page, {'steps': [
            {'action': 'fill', 'label': 'GPA', 'value': '3.2'}]}, {}, None)
        self.assertTrue(result[0]['ok'])
        self.assertNotEqual(result[0]['expected'], result[0]['actual'])

    def test_wrong_nonempty_employment_title_is_accepted(self):
        field = Field('Wrong parsed title')
        block = types.SimpleNamespace(locator=lambda _: Collection([field]))
        result = []
        accepted = apply.fill_in_block(block, 0, 'Professor & Thesis Advisor', result, 'Title')
        self.assertTrue(accepted)
        self.assertEqual(field.value, 'Wrong parsed title')
        self.assertEqual(result[0]['action'], 'already_filled')

    def test_upload_error_with_filename_is_falsely_verified(self):
        page = Page(body='Upload failed: Fictional_Resume.pdf. Please try again.')
        result = apply.verify_filled_fields(page, {'steps': [
            {'action': 'upload', 'label': 'Resume', 'value': '/tmp/Fictional_Resume.pdf'}]}, {}, None)
        self.assertTrue(result[0]['ok'])

    def test_correct_native_select_fails_final_verifier(self):
        page = Page(Field('Master-MS'))
        result = apply.verify_filled_fields(page, {'steps': [
            {'action': 'select', 'label': 'Degree', 'value': 'Master-MS'}]}, {}, None)
        self.assertFalse(result[0]['ok'])
        self.assertEqual(result[0]['actual'], 'None')

    def test_failed_final_verification_is_trusted_on_restart(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(apply, 'RUN_STATE_DIR', Path(tmp)):
            plan = Path(tmp) / 'plan.json'
            profile = Path(tmp) / 'profile.json'
            plan.write_text(json.dumps({'approved': True, 'start_url': 'http://127.0.0.1/fixture', 'pages': []}))
            profile.write_text('{}')
            state = apply.load_run_state('fictional')
            state.update(status='awaiting_review', field_errors=[], final_verification=[{'label': 'Resume', 'ok': False}])
            apply.save_run_state(state)
            result = apply.run_fill('fictional', plan, profile)
            self.assertEqual(result['status'], 'awaiting_review')
            self.assertFalse(result['final_verification'][0]['ok'])
            # no_browser() would have raised if any live verification occurred.

    def test_next_without_transition_still_reaches_review_state(self):
        page = Page()
        context = types.SimpleNamespace(pages=[page], close=lambda: None)
        pw = types.SimpleNamespace(chromium=types.SimpleNamespace(launch_persistent_context=lambda *a, **k: context))
        manager = unittest.mock.MagicMock()
        manager.__enter__.return_value = pw
        with tempfile.TemporaryDirectory() as tmp, patch.object(apply, 'RUN_STATE_DIR', Path(tmp)), patch.object(apply, 'sync_playwright', return_value=manager):
            plan = Path(tmp) / 'plan.json'
            profile = Path(tmp) / 'profile.json'
            plan.write_text(json.dumps({'approved': True, 'start_url': 'http://127.0.0.1/fixture', 'pages': [
                {'name': 'Personal information', 'steps': [{'action': 'select', 'label': 'Degree', 'value': 'Master-MS'}], 'next_button_label': 'Next'},
                {'name': 'Review', 'is_review_page': True}]}))
            profile.write_text('{}')
            result = apply.run_fill('fictional', plan, profile)
            self.assertEqual(result['status'], 'awaiting_review')
            self.assertFalse(result['page_verifications']['Personal information'][0]['ok'])
            self.assertEqual(page.url, 'http://127.0.0.1/fixture')
            self.assertEqual(page.next_clicks, 1)

    def test_handoff_default_still_has_four_hour_timeout(self):
        default = inspect.signature(apply.wait_for_handoff_signal).parameters['safety_cap_seconds'].default
        self.assertEqual(default, 14400)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(TimeoutError):
                apply.wait_for_handoff_signal(Page(), Path(tmp) / 'resume.flag', poll_seconds=1, safety_cap_seconds=2)

    def test_hotel_targets_are_incorrectly_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / 'registry.json'
            targets = Path(tmp) / 'targets.json'
            registry.write_text(json.dumps({'companies': {}}))
            names = ['Shangri-La Hotels & Resorts', 'Capella Hotels & Resorts',
                     'Four Seasons Hotels & Resorts', 'Rosewood Hotels & Resorts']
            targets.write_text(json.dumps({'priority_companies': [{'name': n} for n in names]}))
            with patch.object(workbook, 'REGISTRY_PATH', registry), patch.object(workbook, 'TARGET_PATH', targets):
                rows = workbook.build_registry_rows()
            self.assertEqual(len(rows), 1)  # Four distinct employers collapsed to one.

    def test_applied_status_can_exist_without_submission_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / 'registry.json'
            state = Path(tmp) / 'state.json'
            registry.write_text(json.dumps({'companies': {'fictional': {
                'company': 'Fictional Employer', 'business_unit': 'Example role',
                'application_status': {'state': 'APPLIED', 'provenance': 'Synthetic claim'}}}}))
            state.write_text(json.dumps({'jobs': {}, 'applications': {}}))
            with patch.object(workbook, 'REGISTRY_PATH', registry), patch.object(workbook, 'STATE_PATH', state):
                row = workbook.build_tracker_rows()[0]
            self.assertEqual(row['status'], 'APPLIED')
            self.assertIn('NOT reconciled', row['provenance'])

    def test_confirmed_submission_can_be_absent_from_tracker(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / 'registry.json'
            state = Path(tmp) / 'state.json'
            registry.write_text(json.dumps({'companies': {}}))
            state.write_text(json.dumps({'jobs': {'fictional-key': {'posting': {
                'company': 'Fictional Employer', 'role': 'Example role'}}},
                'applications': {'fictional-key': {'date_applied': '2026-09-28'}}}))
            with patch.object(workbook, 'REGISTRY_PATH', registry), patch.object(workbook, 'STATE_PATH', state):
                rows = workbook.build_tracker_rows()
            self.assertFalse(any(r['company'] == 'Fictional Employer' for r in rows))


if __name__ == '__main__':
    unittest.main(verbosity=2)
