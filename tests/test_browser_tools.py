"""Safety tests for the Milestone B browser toolset.

These are UNIT/GUARD tests. They do not drive a real browser and are not
evidence that any live portal works. Live portal validation is reported
separately and explicitly in the README.
"""
import unittest

import browser_tools
import tools


class SubmissionIsImpossible(unittest.TestCase):

    def test_no_submit_tool_is_registered(self):
        for name in browser_tools.TOOL_REGISTRY:
            self.assertNotIn('submit', name.lower())

    def test_navigation_allowlist_excludes_submit_like_labels(self):
        for forbidden in ('Submit', 'Submit Application', 'Apply now', 'Confirm and submit'):
            self.assertNotIn(forbidden, browser_tools.NAVIGATION_ALLOWLIST)

    def test_advance_refuses_a_label_outside_the_allowlist(self):
        with self.assertRaises(ValueError) as ctx:
            browser_tools.advance_to_next_page('any_run', 'Submit Application')
        self.assertIn('refusing to click', str(ctx.exception))

    def test_refusal_happens_before_any_worker_contact(self):
        """A forbidden label must be rejected on argument grounds, not by luck."""
        with self.assertRaises(ValueError) as ctx:
            browser_tools.advance_to_next_page('definitely_not_a_real_run_key', 'Apply now')
        self.assertIn('Final submission is never performed', str(ctx.exception))


class DeadSessionIsNeverMistakenForSuccess(unittest.TestCase):
    """With no worker alive, every tool must refuse rather than assume page state."""

    MISSING = 'no_such_run_key_for_tests'

    def test_inspect_refuses_without_a_live_worker(self):
        with self.assertRaises(ValueError) as ctx:
            browser_tools.inspect_application_page(self.MISSING)
        self.assertIn('No live browser worker', str(ctx.exception))

    def test_fill_refuses_without_a_live_worker(self):
        with self.assertRaises(ValueError) as ctx:
            browser_tools.fill_application_fields(
                self.MISSING, [{'action': 'fill', 'selector': '#x', 'value': 'y'}])
        self.assertIn('No live browser worker', str(ctx.exception))

    def test_refusal_is_surfaced_to_the_model_as_an_observation_not_a_crash(self):
        observation, is_error = tools.execute_tool(
            'inspect_application_page', {'run_key': self.MISSING},
            registry=browser_tools.TOOL_REGISTRY)
        self.assertTrue(is_error)
        self.assertIn('No live browser worker', observation['error'])


class ArgumentValidation(unittest.TestCase):

    def test_fill_rejects_a_step_without_an_explicit_selector(self):
        with self.assertRaises(ValueError) as ctx:
            browser_tools.fill_application_fields('r', [{'action': 'fill', 'value': 'v'}])
        self.assertIn('selector', str(ctx.exception))

    def test_fill_rejects_navigation_smuggled_in_as_a_field_step(self):
        with self.assertRaises(ValueError) as ctx:
            browser_tools.fill_application_fields(
                'r', [{'action': 'click_button', 'selector': '#submit', 'value': True}])
        self.assertIn('action must be', str(ctx.exception))

    def test_upload_rejects_a_missing_file(self):
        with self.assertRaises(ValueError) as ctx:
            browser_tools.upload_document('r', '#resume', 'C:/nope/not_a_real_file.pdf')
        self.assertIn('file does not exist', str(ctx.exception))

    def test_read_fields_rejects_an_unknown_kind(self):
        with self.assertRaises(ValueError) as ctx:
            browser_tools.read_application_fields('r', [{'selector': '#a', 'kind': 'radio'}])
        self.assertIn('kind must be', str(ctx.exception))


if __name__ == '__main__':
    unittest.main(verbosity=2)
