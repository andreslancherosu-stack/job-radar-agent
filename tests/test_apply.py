import functools
import http.server
import json
import socketserver
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import apply


class UploadVerificationRegression(unittest.TestCase):
    """Guards against a real, measured false-success bug: a Greenhouse-style
    upload widget's hidden <input> can report files.length > 0 while the
    widget's OWN handler has already failed (thrown "Cannot read properties
    of undefined (reading 'uploadFile')") and never actually attached
    anything — verification must catch this instead of trusting the raw
    file count. testapp/page_upload_widget.html reproduces the exact
    mechanism (its handler only succeeds if the visible button was clicked
    first, exactly like the real widget)."""

    @classmethod
    def setUpClass(cls):
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory='testapp')
        cls.httpd = socketserver.TCPServer(('127.0.0.1', 0), handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def url(self, path):
        return 'http://127.0.0.1:%d/%s' % (self.port, path)

    def test_bypassing_the_click_is_caught_as_failure_not_success(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(self.url('page_upload_widget.html'))
            # Deliberately reproduce the real bug: set the file directly on
            # the hidden input, bypassing the button click the widget's
            # own logic depends on.
            page.locator('#resume_upload').set_input_files('demo/fixtures/test_resume.pdf')
            page.wait_for_timeout(300)
            plan_page = {'steps': [{'action': 'upload', 'label': 'Attach', 'value': 'profile:resume'}]}
            results = apply.verify_filled_fields(page, plan_page, {'resume': 'demo/fixtures/test_resume.pdf'}, None)
            browser.close()
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]['ok'], 'Bypassing the click must be verified as FAILED, not a false success')

    def test_the_real_click_interaction_is_verified_as_success(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(self.url('page_upload_widget.html'))
            state = apply.load_run_state('upload_regression_test_scratch')
            step = {'action': 'upload', 'label': 'Attach', 'selector': '#resume_upload',
                    'value': 'demo/fixtures/test_resume.pdf'}
            apply.apply_step(page, step, {}, state, None)
            results = apply.verify_filled_fields(page, {'steps': [step]}, {}, None)
            browser.close()
        Path('private/application_runs/upload_regression_test_scratch.json').unlink(missing_ok=True)
        self.assertTrue(results[0]['ok'])


class VerifyFilledFieldsPhoneNormalization(unittest.TestCase):
    """A real site's own input mask can reformat what was typed (measured:
    '555.401.0502' typed in, '(555) 401-0502' shown after) without changing
    the underlying value. verify_filled_fields must not report that as a
    failure, but a genuinely different phone number must still fail."""

    def make_page(self, displayed_value):
        page = mock.Mock()
        page.get_by_label.return_value.input_value.return_value = displayed_value
        return page

    def test_reformatted_but_same_digits_is_ok(self):
        step = {'action': 'fill', 'label': 'Phone', 'value': 'profile:phone', 'format_normalization': 'phone'}
        page = self.make_page('(555) 401-0502')
        results = apply.verify_filled_fields(page, {'steps': [step]}, {'phone': '555.401.0502'}, None)
        self.assertTrue(results[0]['ok'])
        self.assertIn('normalizing formatting', results[0]['note'])

    def test_genuinely_different_number_still_fails(self):
        step = {'action': 'fill', 'label': 'Phone', 'value': 'profile:phone', 'format_normalization': 'phone'}
        page = self.make_page('(212) 000-0000')
        results = apply.verify_filled_fields(page, {'steps': [step]}, {'phone': '555.401.0502'}, None)
        self.assertFalse(results[0]['ok'])


class RepeatingBlockDuplicateIdRegression(unittest.TestCase):
    """Guards against a real, measured bug on a live Avature application
    form: dynamically-cloned 'add another' blocks (one per employer/
    education entry) reuse the IDENTICAL input id/name across different
    blocks. Page-wide label-proximity targeting (get_by_text(...).locator(
    'xpath=following::input[1]')) silently grabbed the WRONG block's field
    as a result -- editing one employer's Title ended up reading/writing a
    DIFFERENT employer's row. testapp/page_duplicate_ids.html reproduces
    the exact structure (three blocks, identical ids in every block)."""

    @classmethod
    def setUpClass(cls):
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory='testapp')
        cls.httpd = socketserver.TCPServer(('127.0.0.1', 0), handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def url(self, path):
        return 'http://127.0.0.1:%d/%s' % (self.port, path)

    def test_editing_one_block_does_not_corrupt_another(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(self.url('page_duplicate_ids.html'))

            blocks = apply.find_block_containers(page, 'remove', 'Company *')
            self.assertEqual(len(blocks), 4, 'should resolve exactly 4 distinct blocks despite duplicate ids')

            results = []
            # Blocks 0-2 already have values for both fields, so
            # fill_in_block should report "already_filled" and must NOT
            # overwrite them. Block 3 is genuinely empty -- this is the
            # actual write-path test: filling it must land ONLY in block 3.
            apply.fill_in_block(blocks[0], 1, 'CORRUPTED', results, 'Block0 Title', expected_label_text='Title *')
            apply.fill_in_block(blocks[2], 1, 'CORRUPTED', results, 'Block2 Title', expected_label_text='Title *')
            apply.fill_in_block(blocks[3], 0, 'New Employer Inc', results, 'Block3 Company', expected_label_text='Company *')
            apply.fill_in_block(blocks[3], 1, 'New Title', results, 'Block3 Title', expected_label_text='Title *')

            company0 = apply.read_block_field(blocks[0], 0)
            title0 = apply.read_block_field(blocks[0], 1)
            company1 = apply.read_block_field(blocks[1], 0)
            title1 = apply.read_block_field(blocks[1], 1)
            company2 = apply.read_block_field(blocks[2], 0)
            title2 = apply.read_block_field(blocks[2], 1)
            company3 = apply.read_block_field(blocks[3], 0)
            title3 = apply.read_block_field(blocks[3], 1)

            browser.close()

        self.assertEqual(company0, 'Cherry Coatings')
        self.assertEqual(company1, 'Alu Inmobiliaria')
        self.assertEqual(company2, 'Universidad Pontificia Bolivariana')
        # Pre-filled blocks must be unchanged -- "already_filled" means
        # fill_in_block correctly refused to overwrite a non-empty field,
        # even though CORRUPTED was the value it was asked to write.
        self.assertEqual(title0, 'AI & Copilot Architecture Intern')
        self.assertEqual(title1, 'Founder & Broker')
        self.assertIn('HR turnover', title2)
        self.assertNotIn('CORRUPTED', [title0, title1, title2])
        # The real write-path assertion: block 3 was genuinely empty and
        # received exactly its own new values, and nothing else changed.
        self.assertEqual(company3, 'New Employer Inc')
        self.assertEqual(title3, 'New Title')

    def test_multiple_sections_sharing_remove_text_stay_isolated(self):
        """Real bug found on the live Bain form: find_block_containers with
        no scope walks up from EVERY 'remove' link on the page, including
        ones from an unrelated section (e.g. Employment) that doesn't
        contain the anchor text at all (e.g. an education-only anchor like
        'Field of study'). Depending on incidental DOM nesting this either
        finds nothing for that section (safe) or -- what actually happened
        -- latches onto a coincidental ancestor and returns MORE blocks
        than really exist (5 instead of 3 education blocks, in production).
        A `scope` Locator bounding the search to one section's own
        container is the fix -- this proves it isolates cleanly both ways."""
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(self.url('page_duplicate_ids.html'))

            work_scope = page.locator('#work-history')
            edu_scope = page.locator('#education-history')

            employment_blocks = apply.find_block_containers(page, 'remove', 'Company *', scope=work_scope)
            education_blocks = apply.find_block_containers(page, 'remove', 'Major', scope=edu_scope)

            browser.close()

        self.assertEqual(len(employment_blocks), 4, 'employment section has 4 entry-blocks')
        self.assertEqual(len(education_blocks), 2, 'education section has 2 entry-blocks -- scoping must not leak in employment blocks')


class LocateField(unittest.TestCase):
    def test_uses_selector_when_given(self):
        page = mock.Mock()
        apply.locate_field(page, {'action': 'upload', 'label': 'Attach', 'selector': '#resume'})
        page.locator.assert_called_once_with('#resume')
        page.get_by_label.assert_not_called()

    def test_falls_back_to_label_when_no_selector(self):
        page = mock.Mock()
        apply.locate_field(page, {'action': 'fill', 'label': 'Full Name'})
        page.get_by_label.assert_called_once_with('Full Name', exact=True)
        page.locator.assert_not_called()


class InspectPageGuardrails(unittest.TestCase):
    def test_denylist_flags_submit_button_in_inspection_output(self):
        # is_denied_button is reused directly by inspect_page's button
        # classification, so a real "Submit application" button must be
        # flagged the same way it would be refused during a fill.
        self.assertTrue(apply.is_denied_button('Submit application'))


class SubmitGuardrail(unittest.TestCase):
    def test_denylist_catches_submit_variants(self):
        for label in ['Submit', 'Submit Application', 'Finish Application',
                      'Complete Application', 'SEND APPLICATION', 'Apply Now']:
            self.assertTrue(apply.is_denied_button(label), label)

    def test_denylist_allows_safe_navigation_labels(self):
        for label in ['Next', 'Continue', 'Back', 'Save', 'Add Another Education', 'Review Application']:
            self.assertFalse(apply.is_denied_button(label), label)

    def test_click_button_step_refuses_submit_label(self):
        with self.assertRaises(RuntimeError):
            apply.apply_step(None, {'action': 'click_button', 'label': 'Submit Application'}, {}, apply.load_run_state('unused'))


class ResolveValue(unittest.TestCase):
    def test_literal_passthrough(self):
        self.assertEqual(apply.resolve_value({}, 'TX'), ('TX', False))

    def test_profile_dotted_path(self):
        self.assertEqual(apply.resolve_value({'a': {'b': 'x'}}, 'profile:a.b'), ('x', False))

    def test_missing_when_null(self):
        self.assertEqual(apply.resolve_value({'a': None}, 'profile:a'), (None, True))

    def test_missing_when_absent(self):
        self.assertEqual(apply.resolve_value({}, 'profile:missing.path'), (None, True))

    def test_list_index_path(self):
        profile = {'education': [{'school': 'X'}]}
        self.assertEqual(apply.resolve_value(profile, 'profile:education.0.school'), ('X', False))

    def test_list_index_out_of_range_is_missing(self):
        profile = {'education': []}
        _, missing = apply.resolve_value(profile, 'profile:education.0.school')
        self.assertTrue(missing)

    def test_answerbank_confirmed_match_resolves(self):
        bank = {'entries': [{'question': 'Are you willing to relocate?', 'answer': 'Yes',
                              'source': 'user', 'confirmed': True, 'date': '2026-09-01'}]}
        value, missing = apply.resolve_value({}, 'answerbank:Are you willing to relocate?', bank)
        self.assertEqual((value, missing), ('Yes', False))

    def test_answerbank_missing_without_a_bank(self):
        value, missing = apply.resolve_value({}, 'answerbank:Some question?', None)
        self.assertEqual((value, missing), (None, True))

    def test_answerbank_missing_when_unconfirmed(self):
        bank = {'entries': [{'question': 'Salary expectations?', 'answer': '$X',
                              'source': 'draft', 'confirmed': False, 'date': '2026-09-01'}]}
        _, missing = apply.resolve_value({}, 'answerbank:Salary expectations?', bank)
        self.assertTrue(missing)


class DuplicateDetection(unittest.TestCase):
    def test_not_applied_when_state_file_absent(self):
        self.assertFalse(apply.already_applied('some-key', radar_state_path='does/not/exist.json'))

    def test_true_when_key_recorded_as_applied(self):
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8')
        json.dump({'applications': {'abc123': {'date_applied': '2026-09-01'}}}, f)
        f.close()
        self.assertTrue(apply.already_applied('abc123', radar_state_path=f.name))
        self.assertFalse(apply.already_applied('other-key', radar_state_path=f.name))


class MissingQuestions(unittest.TestCase):
    def test_collects_only_unresolved_steps(self):
        plan = {'pages': [
            {'steps': [
                {'action': 'fill', 'label': 'Full Name', 'value': 'profile:full_name'},
                {'action': 'fill', 'label': 'Phone', 'value': 'profile:phone'},
            ]},
            {'is_review_page': True, 'steps': []},
        ]}
        profile = {'full_name': 'Taylor', 'phone': None}
        missing = apply.collect_missing_questions(plan, profile)
        self.assertEqual([m['label'] for m in missing], ['Phone'])

    def test_select_radio_missing_uses_question_text(self):
        plan = {'pages': [{'steps': [
            {'action': 'select_radio', 'question': 'Work authorized?', 'value': 'profile:work_auth',
             'options': {'yes': 'Yes', 'no': 'No'}},
        ]}]}
        missing = apply.collect_missing_questions(plan, {'work_auth': None})
        self.assertEqual(missing, [{'label': 'Work authorized?', 'profile_path': 'profile:work_auth'}])

    def test_click_button_never_counts_as_missing(self):
        plan = {'pages': [{'steps': [{'action': 'click_button', 'label': 'Add Another Education'}]}]}
        self.assertEqual(apply.collect_missing_questions(plan, {}), [])

    def test_skip_approved_step_never_counts_as_missing(self):
        plan = {'pages': [{'steps': [
            {'action': 'fill', 'label': 'Graduation day', 'value': 'profile:missing_field', 'skip_approved': True},
        ]}]}
        self.assertEqual(apply.collect_missing_questions(plan, {}), [])


class SkipApproved(unittest.TestCase):
    def test_apply_step_skips_without_touching_the_page(self):
        state = apply.load_run_state('unused')
        result = apply.apply_step(None, {'action': 'fill', 'label': 'X', 'value': 'profile:y', 'skip_approved': True},
                                   {}, state)
        self.assertTrue(result)
        self.assertIn('skipped', state['action_log'][-1]['message'])

    def test_missing_for_step_ignores_action_type_when_skip_approved(self):
        # Even an action with no 'value' key at all (which would otherwise
        # crash resolve_value) must short-circuit cleanly once skipped.
        step = {'action': 'select_combobox', 'label': 'X', 'skip_approved': True}
        self.assertIsNone(apply.missing_for_step({}, step))


class PlanLoading(unittest.TestCase):
    def write_plan(self, value):
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8')
        json.dump(value, f)
        f.close()
        return f.name

    def test_requires_explicit_approval(self):
        path = self.write_plan({'approved': False, 'pages': []})
        with self.assertRaises(ValueError):
            apply.load_plan(path)

    def test_run_authorization_bypasses_approval_without_editing_the_file(self):
        path = self.write_plan({'approved': False, 'pages': []})
        plan = apply.load_plan(path, run_authorization='user authorized this one supervised run, 2026-09-26')
        self.assertEqual(plan['pages'], [])
        # The file on disk must be untouched — still approved:false.
        self.assertFalse(json.loads(Path(path).read_text(encoding='utf-8'))['approved'])

    def test_run_authorization_does_not_skip_other_validation(self):
        path = self.write_plan({'approved': False, 'pages': [
            {'steps': [{'action': 'delete_everything', 'label': 'x'}]}
        ]})
        with self.assertRaises(ValueError):
            apply.load_plan(path, run_authorization='approved anyway')

    def test_requires_pages_list(self):
        path = self.write_plan({'approved': True})
        with self.assertRaises(ValueError):
            apply.load_plan(path)

    def test_rejects_unsupported_action(self):
        path = self.write_plan({'approved': True, 'pages': [
            {'steps': [{'action': 'delete_everything', 'label': 'x'}]}
        ]})
        with self.assertRaises(ValueError):
            apply.load_plan(path)

    def test_accepts_a_well_formed_plan(self):
        path = self.write_plan({'approved': True, 'start_url': 'http://x', 'pages': [
            {'steps': [{'action': 'fill', 'label': 'Full Name', 'value': 'profile:full_name'}]},
            {'is_review_page': True, 'steps': []},
        ]})
        plan = apply.load_plan(path)
        self.assertEqual(len(plan['pages']), 2)


class HandoffSignalBlocksUntilSignaled(unittest.TestCase):
    """Formalizes what private/diagnostics/handoff_mechanism_test.py proved
    manually into a real, repeatable regression test: wait_for_handoff_signal
    must actually BLOCK (not return early) until the signal file exists, and
    must resume correctly once it does. This is the direct fix for a real
    incident — a fixed 30-minute --leave-open timeout closed the browser
    while the human was still mid-edit — and it is the mechanism run_fill's
    pause_after_fill / hold_open now depend on, so it needs its own
    unconditional proof rather than living only in a one-off script."""

    def test_blocks_past_a_short_elapsed_time_then_resumes(self):
        signal_file = Path(tempfile.mktemp(suffix='.flag'))
        signal_file.unlink(missing_ok=True)

        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto('about:blank')

            # Start the delayed-signal thread only once the browser/page are
            # fully ready, so the measured elapsed time isn't eaten by
            # Chromium's own launch latency (which previously made this
            # flaky: the delay thread had a head start before timing began).
            def create_after_delay():
                import time as _time
                _time.sleep(2.0)
                signal_file.write_text('done', encoding='utf-8')

            threading.Thread(target=create_after_delay, daemon=True).start()

            start = __import__('time').time()
            waited = apply.wait_for_handoff_signal(page, signal_file, poll_seconds=1)
            elapsed = __import__('time').time() - start
            # Proves the SAME connection survived the wait, not just that
            # the function returned.
            title = page.title()
            browser.close()
        signal_file.unlink(missing_ok=True)

        self.assertGreaterEqual(elapsed, 1.5, 'must have actually waited for the signal, not returned immediately')
        self.assertGreaterEqual(waited, 1)
        self.assertEqual(title, '')

    def test_safety_cap_raises_rather_than_hanging_forever(self):
        signal_file = Path(tempfile.mktemp(suffix='.flag'))
        signal_file.unlink(missing_ok=True)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto('about:blank')
            with self.assertRaises(TimeoutError):
                apply.wait_for_handoff_signal(page, signal_file, poll_seconds=1, safety_cap_seconds=2)
            browser.close()


class RunLockPreventsCompetingWorkers(unittest.TestCase):
    """A second `fill` invocation against a run_key already being worked by
    a live process must be refused, not silently allowed to race the first
    (e.g. both navigating/filling the same browser session at once)."""

    def setUp(self):
        self.run_key = 'lock_test_scratch'
        apply.release_run_lock(self.run_key)

    def tearDown(self):
        apply.release_run_lock(self.run_key)

    def test_second_acquire_is_refused_while_first_still_held(self):
        apply.acquire_run_lock(self.run_key)
        with self.assertRaises(apply.RunLockedError):
            apply.acquire_run_lock(self.run_key)

    def test_release_then_reacquire_succeeds(self):
        apply.acquire_run_lock(self.run_key)
        apply.release_run_lock(self.run_key)
        apply.acquire_run_lock(self.run_key)  # must not raise

    def test_stale_lock_from_a_dead_pid_is_reclaimed(self):
        lock_path = apply.run_lock_path(self.run_key)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text('999999999', encoding='utf-8')  # a PID essentially guaranteed not to exist
        apply.acquire_run_lock(self.run_key)  # must not raise — stale lock reclaimed


class SessionAliveCheck(unittest.TestCase):
    """Unit-level proof of check_session_alive's two independent checks,
    isolated from run_fill so a failure here points directly at the
    detection logic rather than the whole multi-page flow."""

    @classmethod
    def setUpClass(cls):
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory='testapp')
        cls.httpd = socketserver.TCPServer(('127.0.0.1', 0), handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def url(self, path):
        return 'http://127.0.0.1:%d/%s' % (self.port, path)

    def test_none_check_is_always_alive(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(self.url('page2.html'))
            alive, reason = apply.check_session_alive(page, None)
            browser.close()
        self.assertTrue(alive)
        self.assertIsNone(reason)

    def test_missing_expected_text_is_detected_as_session_lost(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(self.url('page2.html'))
            alive, reason = apply.check_session_alive(
                page, {'text_present': ['TEXT_THAT_DOES_NOT_EXIST_ON_THIS_PAGE_xyz']})
            browser.close()
        self.assertFalse(alive)
        self.assertIn('TEXT_THAT_DOES_NOT_EXIST_ON_THIS_PAGE_xyz', reason)

    def test_unexpected_text_present_is_detected_as_session_lost(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(self.url('page2.html'))
            # page2.html legitimately shows "Step 2 of 4" — declaring that
            # text_absent simulates "this does not look like the real
            # continuation we expected."
            alive, reason = apply.check_session_alive(page, {'text_absent': ['Step 2 of 4']})
            browser.close()
        self.assertFalse(alive)
        self.assertIn('Step 2 of 4', reason)

    def test_both_checks_passing_is_alive(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(self.url('page2.html'))
            alive, reason = apply.check_session_alive(
                page, {'text_present': ['Step 2 of 4'], 'text_absent': ['NOPE_xyz']})
            browser.close()
        self.assertTrue(alive)
        self.assertIsNone(reason)


class SessionLossIntegration(unittest.TestCase):
    """Demonstration item 6: a resumed run_fill must detect a lost session
    from the LIVE page and stop — never silently refilling into what might
    be the wrong page, and never silently restarting the application on the
    very next call either (that would be the same silent-restart failure
    mode by a different route)."""

    @classmethod
    def setUpClass(cls):
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory='testapp')
        cls.httpd = socketserver.TCPServer(('127.0.0.1', 0), handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def url(self, path):
        return 'http://127.0.0.1:%d/%s' % (self.port, path)

    def setUp(self):
        self.run_key = 'session_loss_test_scratch'
        self._cleanup()

    def tearDown(self):
        self._cleanup()

    def _cleanup(self):
        import shutil
        for p in (apply.run_state_path(self.run_key), apply.run_lock_path(self.run_key),
                  apply.resume_signal_path(self.run_key), apply.close_signal_path(self.run_key)):
            p.unlink(missing_ok=True)
        shutil.rmtree(apply.browser_profile_dir(self.run_key), ignore_errors=True)

    def _write_plan(self, **extra):
        plan = {
            'approved': True,
            'start_url': self.url('page1.html'),
            'pages': [
                {'name': 'Personal Information', 'next_button_label': 'Next', 'steps': [
                    {'action': 'fill', 'label': 'Full Name', 'value': 'profile:full_name'},
                ]},
                {'name': 'Education and Employment', 'next_button_label': 'Next', 'steps': [
                    {'action': 'fill', 'label': 'Company Name (Entry 1)', 'value': 'profile:employment.0.company'},
                ]},
            ],
        }
        plan.update(extra)
        path = Path(tempfile.mktemp(suffix='.json'))
        path.write_text(json.dumps(plan), encoding='utf-8')
        return path

    def test_resume_with_failed_session_check_stops_without_filling_and_refuses_silent_restart(self):
        plan_path = self._write_plan(resume_session_check={'text_absent': ['Step 2 of 4']})

        # Pre-seed run_state as if a prior run had legitimately stopped
        # mid-application on page 2 (page_index=1) — the exact situation a
        # real cross-process resume starts from.
        state = apply.load_run_state(self.run_key)
        state['status'] = 'blocked_validation'
        state['page_index'] = 1
        state['current_url'] = self.url('page2.html')
        apply.save_run_state(state)

        result = apply.run_fill(self.run_key, str(plan_path), 'demo/test_profile.json', headless=True)

        self.assertEqual(result['status'], 'session_lost')
        self.assertTrue(result['issues'], 'must explicitly report a reason, not fail silently')
        self.assertIn('Step 2 of 4', result['issues'][0])
        # Nothing on page 2 should have been touched.
        self.assertNotIn('Education and Employment', result.get('page_verifications', {}))

        # The NEXT call, without an explicit human-authorized force_refill,
        # must refuse to proceed too — not quietly fall back to start_url
        # and refill everything as if nothing happened.
        second = apply.run_fill(self.run_key, str(plan_path), 'demo/test_profile.json', headless=True)
        self.assertEqual(second['status'], 'session_lost')
        self.assertNotIn('opened ' + self.url('page1.html'), [a['message'] for a in second['action_log']])

    def test_force_refill_after_session_loss_is_explicit_and_restarts_cleanly(self):
        plan_path = self._write_plan(resume_session_check={'text_absent': ['Step 2 of 4']})
        state = apply.load_run_state(self.run_key)
        state['status'] = 'session_lost'
        state['page_index'] = 1
        state['current_url'] = self.url('page2.html')
        apply.save_run_state(state)

        result = apply.run_fill(self.run_key, str(plan_path), 'demo/test_profile.json',
                                 headless=True, force_refill=True)
        # force_refill restarts at start_url (page1), so the failing
        # resume_session_check (which only applies when actually resuming
        # mid-application) never triggers.
        self.assertNotEqual(result['status'], 'session_lost')


class MultiPagePauseResumeContinuity(unittest.TestCase):
    """The end-to-end demonstration requested for the repaired session
    architecture, run against the local testapp fixture: fill page one,
    pause for a manual edit, resume in the SAME browser, preserve that
    edit, advance to page two and fill it, and reach final review without
    ever touching Submit — all inside one continuous run_fill call (one
    browser launch, confirmed via the action log), never closing or
    relaunching mid-application."""

    @classmethod
    def setUpClass(cls):
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory='testapp')
        cls.httpd = socketserver.TCPServer(('127.0.0.1', 0), handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def url(self, path):
        return 'http://127.0.0.1:%d/%s' % (self.port, path)

    def setUp(self):
        self.run_key = 'pause_resume_continuity_test_scratch'
        self._cleanup()

    def tearDown(self):
        self._cleanup()

    def _cleanup(self):
        import shutil
        for p in (apply.run_state_path(self.run_key), apply.run_lock_path(self.run_key),
                  apply.resume_signal_path(self.run_key), apply.close_signal_path(self.run_key)):
            p.unlink(missing_ok=True)
        shutil.rmtree(apply.browser_profile_dir(self.run_key), ignore_errors=True)

    def test_pause_preserves_manual_edit_then_advances_to_review_without_submitting(self):
        plan = json.loads(Path('demo/apply_plan_testapp.json').read_text(encoding='utf-8'))
        plan['start_url'] = self.url('page1.html')
        plan['pages'][0]['pause_after_fill'] = True
        plan_path = Path(tempfile.mktemp(suffix='.json'))
        plan_path.write_text(json.dumps(plan), encoding='utf-8')

        captured = {}

        def pause_hook(page):
            # Runs synchronously on run_fill's own thread, right at the
            # pause checkpoint — simulates a human correcting an
            # automation-filled value with the SAME live page/session
            # run_fill itself is using, then signals resume. No second
            # thread/process needs to race to reach this page.
            full_name = page.get_by_label('Full Name', exact=True)
            captured['automation_filled_value'] = full_name.input_value()
            full_name.fill('Jordan ManualEdit')
            captured['after_manual_edit'] = full_name.input_value()
            apply.resume_signal_path(self.run_key).write_text('resume', encoding='utf-8')

        # pause_after_fill only engages when headed (there is no human to
        # resume a headless run) — this environment has a real desktop
        # session (headed runs have been used throughout this project), so
        # this exercises the actual pause code path, not just pause_hook in
        # isolation.
        state = apply.run_fill(self.run_key, str(plan_path), 'demo/test_profile.json',
                                headless=False, pause_hook=pause_hook)

        self.assertEqual(captured['automation_filled_value'], 'Taylor Example')
        self.assertEqual(captured['after_manual_edit'], 'Jordan ManualEdit')

        # Reached final review, never clicked Submit.
        self.assertEqual(state['status'], 'awaiting_review')
        self.assertEqual(state['current_url'], self.url('page4_review.html'))

        # Genuinely paused and resumed (not skipped) — and only ONE
        # navigation-from-scratch happened for the whole run, proving one
        # continuous browser session was used for both pages, not a
        # close-and-relaunch between them.
        messages = [a['message'] for a in state['action_log']]
        self.assertTrue(any(m.startswith('pausing after filling') for m in messages))
        self.assertTrue(any(m.startswith('resume signal received') for m in messages))
        opens = [m for m in messages if m.startswith('opened ') or m.startswith('resumed at ')]
        self.assertEqual(len(opens), 1, 'must be exactly one navigation-from-scratch for the whole run: %r' % messages)

        # Page 2 (Education and Employment) was genuinely filled after the
        # pause, not skipped.
        self.assertIn('Education and Employment', state.get('page_verifications', {}))
        page2_results = {v['label']: v for v in state['page_verifications']['Education and Employment']}
        self.assertTrue(page2_results['School Name (Entry 1)']['ok'])

        # Independently re-read the PERSISTED outcome from the same
        # browser profile/origin after run_fill has already returned (and
        # therefore already closed its own connection) — proof the manual
        # edit reached the actual saved application data, not just that
        # the pause/resume calls didn't error.
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                str(apply.browser_profile_dir(self.run_key)), headless=True)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(self.url('page4_review.html'))
            review_text = page.locator('#review-output').inner_text()
            submitted_banner_visible = page.locator('#submitted-banner').is_visible()
            context.close()

        self.assertIn('Jordan ManualEdit', review_text)
        self.assertNotIn('Taylor Example', review_text)
        self.assertFalse(submitted_banner_visible, 'must never have clicked Submit')

    def test_headless_run_still_pauses_and_is_resumable_not_skipped(self):
        """Corrected per an independent audit: a required human checkpoint
        must fail closed (remain paused) rather than silently disappear
        because the run happens to be headless -- a human can still send a
        resume signal without ever seeing the window (e.g. from another
        terminal via `apply.py resume`). Proves the run genuinely BLOCKS
        (via a real background thread racing the wait, not just a
        synchronous hook) and only proceeds once signaled."""
        plan = json.loads(Path('demo/apply_plan_testapp.json').read_text(encoding='utf-8'))
        plan['start_url'] = self.url('page1.html')
        plan['pages'][0]['pause_after_fill'] = True
        plan_path = Path(tempfile.mktemp(suffix='.json'))
        plan_path.write_text(json.dumps(plan), encoding='utf-8')

        reached_pause = {'seen': False}

        def wait_for_pause_then_signal():
            deadline = time.time() + 15
            while time.time() < deadline:
                state = apply.load_run_state(self.run_key)
                if state.get('status') == 'paused_for_review':
                    reached_pause['seen'] = True
                    apply.resume_signal_path(self.run_key).write_text('resume', encoding='utf-8')
                    return
                time.sleep(0.2)

        t = threading.Thread(target=wait_for_pause_then_signal, daemon=True)
        t.start()
        state = apply.run_fill(self.run_key, str(plan_path), 'demo/test_profile.json', headless=True)
        t.join(timeout=5)

        self.assertTrue(reached_pause['seen'], 'a headless run must still genuinely reach paused_for_review, not skip it')
        self.assertEqual(state['status'], 'awaiting_review')
        messages = [a['message'] for a in state['action_log']]
        self.assertTrue(any(m.startswith('pausing after filling') for m in messages))
        self.assertFalse(any('skipping the pause' in m for m in messages),
                          'pause_after_fill must never be silently skipped because a run is headless')


class IndependentAuditRegressionTests(unittest.TestCase):
    """Inverts each of the 10 defect reproductions from the independent
    review's job_radar_audit_probes.py: those probes intentionally asserted
    the BUGGY behavior to prove the defects existed (a passing probe meant
    a confirmed bug). These assert the CORRECTED, desired behavior after
    the fixes in apply.py -- a passing test here means the bug is gone."""

    # --- Probe 1: GPA "32" vs "3.2" was falsely verified via unconditional
    # digit-only normalization meant only for phone fields. ---
    def test_gpa_digit_coincidence_is_not_falsely_verified(self):
        page = mock.Mock()
        page.get_by_label.return_value.input_value.return_value = '32'
        step = {'action': 'fill', 'label': 'GPA', 'value': '3.2'}  # no format_normalization -> phone rule must not apply
        result = apply.verify_filled_fields(page, {'steps': [step]}, {}, None)
        self.assertFalse(result[0]['ok'])
        self.assertNotEqual(result[0]['expected'], result[0]['actual'])

    # --- Probe 2: a wrong, never-reviewed existing employment title was
    # accepted as "already_filled" just because it was non-empty. ---
    def test_wrong_existing_employment_title_is_reported_as_conflict_not_accepted(self):
        field = types.SimpleNamespace(value='Wrong parsed title')
        field.input_value = lambda: field.value

        def fake_fill(v):
            field.value = v
        field.fill = fake_fill
        block = types.SimpleNamespace(locator=lambda _: Collection([field]))
        result = []
        accepted = apply.fill_in_block(block, 0, 'Professor & Thesis Advisor', result, 'Title')
        self.assertFalse(accepted)
        self.assertEqual(field.value, 'Wrong parsed title', 'must never overwrite an existing value either way')
        self.assertEqual(result[0]['action'], 'conflict_existing_value_differs')
        self.assertFalse(result[0]['ok'])

    # --- Probe 3: an "Upload failed: <filename>" error message was
    # accepted as proof of a successful upload because the filename
    # happened to appear somewhere in the body text. ---
    def test_upload_error_containing_filename_is_not_falsely_verified(self):
        page = mock.Mock()
        page.locator.return_value.inner_text.return_value = 'Upload failed: Fictional_Resume.pdf. Please try again.'
        result = apply.verify_filled_fields(page, {'steps': [
            {'action': 'upload', 'label': 'Resume', 'value': '/tmp/Fictional_Resume.pdf'}]}, {}, None)
        self.assertFalse(result[0]['ok'])

    # --- Probe 4: a correctly-selected native <select> option was always
    # reported as a final-verification failure -- read_current_value had
    # no branch for the 'select' action and fell through to None. ---
    def test_correct_native_select_passes_final_verifier(self):
        page = mock.Mock()
        page.get_by_label.return_value.input_value.return_value = 'Master-MS'
        result = apply.verify_filled_fields(page, {'steps': [
            {'action': 'select', 'label': 'Degree', 'value': 'Master-MS'}]}, {}, None)
        self.assertTrue(result[0]['ok'])
        self.assertEqual(result[0]['actual_value'], 'Master-MS')

    # --- Second, separate select bug found live on a real UKG/UltiPro
    # Education section (2026-09-30): Locator.select_option() with a bare
    # string matches the option's VALUE attribute first, and only falls
    # back to matching its LABEL if no value matches. A month dropdown
    # using "5" for May, or a Major dropdown using an internal GUID,
    # both select correctly via that fallback -- but input_value() alone
    # then returns "5" or the GUID, which never equals the human-readable
    # label the plan asked for ("May"), producing a false verification
    # failure even though the right option is genuinely selected. ---
    def test_select_option_label_fallback_is_not_falsely_flagged_as_mismatch(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content('<select id="m"><option value="1">Jan</option><option value="5">May</option></select>')
            step = {'action': 'select', 'selector': '#m', 'label': 'Month', 'value': 'May'}
            apply.apply_step(page, step, {}, {'field_errors': [], 'action_log': []})
            result = apply.verify_filled_fields(page, {'steps': [step]}, {}, None)
            browser.close()
        self.assertTrue(result[0]['ok'], result[0])
        self.assertEqual(result[0]['actual_value'], '5')
        self.assertEqual(result[0]['actual_label'], 'May')

    def test_select_wrong_option_still_correctly_fails(self):
        # The fix must not become lenient -- a genuinely wrong selection
        # (label mismatch too) still has to fail.
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content('<select id="m"><option value="1">Jan</option><option value="5">May</option></select>')
            page.locator('#m').select_option('1')  # wrong: selects Jan
            step = {'action': 'select', 'selector': '#m', 'label': 'Month', 'value': 'May'}
            result = apply.verify_filled_fields(page, {'steps': [step]}, {}, None)
            browser.close()
        self.assertFalse(result[0]['ok'])
        self.assertEqual(result[0]['actual_label'], 'Jan')

    # --- Precondition check for a controlled restart (2026-09-30): there
    # was no way to inspect a field's CURRENT value without going through
    # fill_steps, which always WRITES first. A genuine "did this survive?"
    # check must never itself be a write -- read_current_value already
    # reads without writing; this confirms it leaves the field exactly as
    # found even when the step's OWN 'value' key names something
    # different (proving it is never used to overwrite anything). ---
    def test_read_current_value_never_writes(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content('<input id="f" value="existing confirmed value">')
            step = {'action': 'fill', 'selector': '#f', 'label': 'Field', 'value': 'a DIFFERENT value never meant to be written'}
            observed = apply.read_current_value(page, step)
            still_there = page.locator('#f').input_value()
            browser.close()
        self.assertEqual(observed, 'existing confirmed value')
        self.assertEqual(still_there, 'existing confirmed value', 'read_current_value must never write')


class ScopedClickButtonRegression(unittest.TestCase):
    """A real, measured gap found live on a real UKG/UltiPro Skills
    section (2026-09-30): every removable skill tag's own remove control
    shares the IDENTICAL accessible name ("x"), so click_button's old
    page-wide get_by_role('button', name=label) search could never target
    one specific tag among several -- and simply accepting a 'selector'
    override on the step, the way locate_field already does for fill/
    select/check, fixes it without inventing a new action type."""

    def _page_with_tag_cards(self, pw):
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('''
            <div class="card"><h4>Pmi</h4><button aria-label="x" onclick="this.closest('.card').remove()">x</button></div>
            <div class="card"><h4>Python</h4><button aria-label="x" onclick="this.closest('.card').remove()">x</button></div>
            <div class="card"><h4>Pmp</h4><button aria-label="x" onclick="this.closest('.card').remove()">x</button></div>
        ''')
        return browser, page

    def test_scoped_selector_removes_only_the_intended_tag(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser, page = self._page_with_tag_cards(pw)
            step = {
                'action': 'click_button', 'label': 'x',
                'selector': '//h4[normalize-space()="Pmi"]/following::button[1]',
            }
            apply.apply_step(page, step, {}, {'field_errors': [], 'action_log': []})
            remaining = page.locator('.card h4').all_inner_texts()
            browser.close()
        self.assertEqual(sorted(remaining), sorted(['Python', 'Pmp']))

    def test_selector_cannot_bypass_denylist_with_an_innocent_declared_label(self):
        # The denylist check must validate what the selector ACTUALLY
        # resolves to, not the step's own claimed label -- otherwise a
        # selector could point straight at a real submit button while
        # declaring a harmless label like "Save and continue".
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content('<button id="realSubmit" onclick="this.dataset.clicked=\'1\'">Submit Application</button>')
            step = {'action': 'click_button', 'label': 'Save and continue', 'selector': '#realSubmit'}
            with self.assertRaises(RuntimeError):
                apply.apply_step(page, step, {}, {'field_errors': [], 'action_log': []})
            clicked = page.locator('#realSubmit').get_attribute('data-clicked')
            browser.close()
        self.assertIsNone(clicked, 'the real submit button must never have actually been clicked')

    def test_unscoped_selector_on_identical_labels_raises_instead_of_guessing(self):
        # Without a selector, the old page-wide behavior is preserved --
        # and Playwright's own strict-mode violation on 3 identically
        # labeled buttons is a safe, loud failure, never a silent guess.
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser, page = self._page_with_tag_cards(pw)
            step = {'action': 'click_button', 'label': 'x'}
            with self.assertRaises(Exception):
                apply.apply_step(page, step, {}, {'field_errors': [], 'action_log': []})
            browser.close()


class LabelTriggeredUploadRegression(unittest.TestCase):
    """Measured live on Bain's Recruits Portal (req 10397, 2026-10-05).

    The real upload trigger was a <label for="resumeFile"> styled as a
    button -- NOT an element with role=button. The old trigger lookup only
    searched the input's immediate parent for role=button, found nothing,
    and silently fell through to set_input_files(). That stages the file in
    the DOM (input.value becomes C:\\fakepath\\...) so it LOOKS plausible,
    but the page's own change handler never ran, so its hidden Continue
    button was never revealed and the application could not advance at all.

    This fixture reproduces that exact shape: a label trigger, and a submit
    button that starts hidden and is only revealed by the handler that runs
    on a genuine file-chooser interaction."""

    NOTE_ON_WHAT_THIS_CAN_PROVE = '''
        Playwright's set_input_files() DOES fire input/change, so a fixture
        that only listens for `change` is revealed by BOTH code paths and
        proves nothing (verified directly before writing this test). What
        actually distinguishes the paths is whether the page's own TRIGGER
        was really clicked -- the old code never touched it. So that is what
        is asserted here, via a click listener on the label.
    '''

    PAGE = '''
        <label for="resumeFile" id="trigger">From Device</label>
        <input type="file" id="resumeFile" name="resumeFile">
        <script>
          window.__triggerClicked = false;
          document.getElementById('trigger').addEventListener('click', function () {
            window.__triggerClicked = true;
          });
        </script>
    '''

    def _upload_and_report(self, pw, file_path):
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(self.PAGE)
        step = {'action': 'upload', 'selector': '#resumeFile',
                'label': 'From Device', 'value': file_path}
        try:
            apply.apply_step(page, step, {}, {'field_errors': [], 'action_log': []})
        except RuntimeError:
            # The verification half may still fail (this bare fixture renders
            # no filename anywhere); the trigger question is what matters.
            pass
        clicked = page.evaluate('() => window.__triggerClicked === true')
        staged = page.locator('#resumeFile').input_value()
        browser.close()
        return clicked, staged

    def test_the_pages_own_label_trigger_is_actually_clicked(self):
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 'Andres_Lancheros_Resume.pdf'
            f.write_bytes(b'%PDF-1.5 test')
            with sync_playwright() as pw:
                clicked, staged = self._upload_and_report(pw, str(f))
        self.assertTrue(clicked,
                        "the page's own <label for=...> trigger must be clicked so the site's handlers "
                        'run; the previous code bypassed it entirely via set_input_files()')
        self.assertIn('Andres_Lancheros_Resume.pdf', staged, 'the file must still end up staged')


class ScopedAttachmentVerificationRegression(unittest.TestCase):
    """PREPARED CAPABILITY, not yet deployed to any live worker (added
    2026-10-04). Reproduces the exact measured gap on a real Momentum
    Greenhouse application (req 8608934002): the job DESCRIPTION precedes
    the application form, so inspect's body_sample (capped at the first
    3000 characters) never reaches the resume widget at all, and the
    widget's file <input> id is dynamically generated and can be ABSENT
    after a successful upload replaces its own DOM -- so neither existing
    read path can independently re-check an attachment without re-uploading."""

    def _page(self, pw, filler_chars=3400, widget_html=None):
        widget_html = widget_html or (
            '<div class="attach"><h3>Resume</h3><p>my_resume.pdf — uploaded successfully</p></div>')
        filler = '<p>' + ('Lorem ipsum job description text. ' * (filler_chars // 34)) + '</p>'
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<body>' + filler + widget_html + '</body>')
        return browser, page

    def test_committed_attachment_is_verified_past_the_3000_char_cap(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser, page = self._page(pw)
            body_sample = page.locator('body').inner_text()[:3000]
            result = apply.read_attachment_state(page, 'Resume', expected_filename='my_resume.pdf')
            browser.close()
        self.assertNotIn('my_resume.pdf', body_sample,
                         'the fixture must reproduce the gap -- filename past the 3000-char cap')
        self.assertTrue(result['ok'])
        self.assertTrue(result['expected_filename_present'])
        self.assertFalse(result['failure_language_present'])
        self.assertFalse(result['pending_upload_language_present'])

    def test_missing_input_id_does_not_block_verification(self):
        # The widget below has NO <input> at all -- exactly what a real
        # upload widget can look like after it replaces its own DOM post-
        # success. A selector/id-based read would find nothing; the scoped
        # text read still works because it never depended on the input.
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser, page = self._page(pw, widget_html=(
                '<div class="attach"><h3>Resume</h3>'
                '<span>V5_Resume_Lancheros.pdf</span><button>Remove</button></div>'))
            result = apply.read_attachment_state(page, 'Resume', expected_filename='V5_Resume_Lancheros.pdf')
            browser.close()
        self.assertTrue(result['ok'])

    def test_pending_upload_is_not_reported_as_verified(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser, page = self._page(pw, widget_html=(
                '<div class="attach"><h3>Resume</h3><p>Uploading my_resume.pdf...</p></div>'))
            result = apply.read_attachment_state(page, 'Resume', expected_filename='my_resume.pdf')
            browser.close()
        self.assertFalse(result['ok'])
        self.assertTrue(result['pending_upload_language_present'])

    def test_failure_language_is_not_reported_as_verified(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser, page = self._page(pw, widget_html=(
                '<div class="attach"><h3>Resume</h3><p>Upload failed: my_resume.pdf. Please try again.</p></div>'))
            result = apply.read_attachment_state(page, 'Resume', expected_filename='my_resume.pdf')
            browser.close()
        self.assertFalse(result['ok'])
        self.assertTrue(result['failure_language_present'])

    def test_wrong_filename_is_not_reported_as_verified(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser, page = self._page(pw)
            result = apply.read_attachment_state(page, 'Resume', expected_filename='a_totally_different_file.pdf')
            browser.close()
        self.assertFalse(result['ok'])
        self.assertFalse(result['expected_filename_present'])

    def test_ambiguous_scope_refuses_rather_than_guesses(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content('''
                <div><h3>Resume</h3><p>a.pdf</p></div>
                <div><h3>Resume</h3><p>b.pdf</p></div>
            ''')
            result = apply.read_attachment_state(page, 'Resume', expected_filename='a.pdf')
            browser.close()
        self.assertTrue(result['inconclusive'])
        self.assertFalse(result['ok'])


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


class FakeFlowPage:
    """A minimal Playwright Page double for exercising run_fill's
    STATE-MACHINE logic in isolation from real browser rendering -- the
    same technique the independent review's own probes used for this. A
    real Chromium instance covers actual DOM/site behavior elsewhere in
    this file (MultiPagePauseResumeContinuity, UploadVerificationRegression,
    etc.); this double exists specifically to make an otherwise-unreachable
    state (e.g. "the click did nothing, no URL change, no .error.visible
    element") reproducible and deterministic."""

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
        self.next_clicks += 1  # deliberately no transition

    def wait_for_timeout(self, milliseconds):
        pass


def _fake_playwright_manager(page):
    context = types.SimpleNamespace(pages=[page], close=lambda: None)
    pw = types.SimpleNamespace(chromium=types.SimpleNamespace(launch_persistent_context=lambda *a, **k: context))
    manager = mock.MagicMock()
    manager.__enter__.return_value = pw
    return manager


class StateMachineRegressionTests(unittest.TestCase):
    """Probes 5-7, inverted: a saved status must never be trusted over a
    live check, a click that does not visibly transition the page must
    never be promoted to the next plan page, and the handoff safety cap
    must be its own explicit, resumable, non-error status."""

    def setUp(self):
        self.run_key = 'audit_regression_scratch_%s' % self.id().split('.')[-1]
        self._cleanup()

    def tearDown(self):
        self._cleanup()

    def _cleanup(self):
        for p in (apply.run_state_path(self.run_key), apply.run_lock_path(self.run_key),
                  apply.resume_signal_path(self.run_key), apply.close_signal_path(self.run_key)):
            p.unlink(missing_ok=True)

    def test_failed_final_verification_is_not_trusted_on_restart(self):
        state = apply.load_run_state(self.run_key)
        state.update(status='awaiting_review', field_errors=[], final_verification=[{'label': 'Resume', 'ok': False}])
        apply.save_run_state(state)

        page = FakeFlowPage()
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / 'plan.json'
            profile = Path(tmp) / 'profile.json'
            plan.write_text(json.dumps({'approved': True, 'start_url': 'http://127.0.0.1/fixture', 'pages': []}))
            profile.write_text('{}')
            with mock.patch.object(apply, 'sync_playwright', return_value=_fake_playwright_manager(page)):
                result = apply.run_fill(self.run_key, str(plan), str(profile))

        # Must have genuinely attempted a live resume (a new "opened"/
        # "resumed at" action-log entry), not just handed back the stale
        # saved state untouched.
        messages = [a['message'] for a in result['action_log']]
        self.assertTrue(any(m.startswith('opened ') or m.startswith('resumed at ') for m in messages),
                         'a saved awaiting_review with a failed final_verification must trigger a fresh live check')

    def test_next_click_without_real_transition_is_not_promoted_to_review(self):
        page = FakeFlowPage()
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / 'plan.json'
            profile = Path(tmp) / 'profile.json'
            plan.write_text(json.dumps({'approved': True, 'start_url': 'http://127.0.0.1/fixture', 'pages': [
                {'name': 'Personal information', 'steps': [{'action': 'select', 'label': 'Degree', 'value': 'Master-MS'}], 'next_button_label': 'Next'},
                {'name': 'Review', 'is_review_page': True}]}))
            profile.write_text('{}')
            with mock.patch.object(apply, 'sync_playwright', return_value=_fake_playwright_manager(page)):
                result = apply.run_fill(self.run_key, str(plan), str(profile))

        self.assertNotEqual(result['status'], 'awaiting_review')
        self.assertEqual(result['status'], 'blocked_validation')
        self.assertEqual(page.next_clicks, 1)
        self.assertTrue(result['issues'])
        self.assertIn('did not visibly advance', result['issues'][0])

    def test_handoff_safety_cap_is_its_own_resumable_non_error_status(self):
        page = FakeFlowPage()
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / 'plan.json'
            profile = Path(tmp) / 'profile.json'
            plan.write_text(json.dumps({'approved': True, 'start_url': 'http://127.0.0.1/fixture', 'pages': [
                {'name': 'Personal information', 'steps': [], 'pause_after_fill': True, 'next_button_label': 'Next'},
            ]}))
            profile.write_text('{}')
            with mock.patch.object(apply, 'sync_playwright', return_value=_fake_playwright_manager(page)), \
                 mock.patch.object(apply, 'wait_for_handoff_signal',
                                    side_effect=apply.HandoffSafetyCapReached('safety cap reached (test)')):
                result = apply.run_fill(self.run_key, str(plan), str(profile))

        self.assertEqual(result['status'], 'handoff_safety_cap_reached')
        self.assertIn('handoff_safety_cap_reached', apply.RESUMABLE_STATUSES)
        self.assertNotEqual(result['status'], 'error', 'a crash-guard firing must never be reported as a generic error')


class WorkerArchitectureDemonstration(unittest.TestCase):
    """The independent review's completion criterion 1: one continuous
    local application through the worker architecture -- at least three
    pages, a manual-handoff checkpoint, a repeated education/employment
    section, a real async upload, and a genuine validation failure --
    resumed after each checkpoint in the SAME browser-owning worker
    process, never closing or relaunching. Also proves a competing worker
    is refused and a lost session stops further writes."""

    @classmethod
    def setUpClass(cls):
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory='testapp')
        cls.httpd = socketserver.TCPServer(('127.0.0.1', 0), handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def url(self, path):
        return 'http://127.0.0.1:%d/%s' % (self.port, path)

    def setUp(self):
        # A unique run_key per test (not just per class) -- a test that
        # errors partway through must never leave a live worker holding
        # the lock (or the browser profile dir) for the NEXT test. Belt
        # and suspenders: _start_worker also registers a guaranteed
        # close+join cleanup regardless of how the test ends.
        self.run_key = 'worker_demo_scratch_%s' % self.id().split('.')[-1]
        self._cleanup()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for p in (apply.run_state_path(self.run_key), apply.run_lock_path(self.run_key),
                  apply.resume_signal_path(self.run_key), apply.close_signal_path(self.run_key),
                  apply.worker_command_path(self.run_key), apply.worker_result_path(self.run_key)):
            p.unlink(missing_ok=True)

    def _start_worker(self, **kwargs):
        kwargs.setdefault('start_url', self.url('page1.html'))
        kwargs.setdefault('headless', True)
        worker_thread = threading.Thread(target=apply.run_worker, args=(self.run_key, 'demo/test_profile.json'),
                                          kwargs=kwargs, daemon=True)
        worker_thread.start()

        def _guaranteed_close():
            try:
                apply.send_worker_command(self.run_key, {'cmd': 'close'}, timeout_seconds=5)
            except Exception:
                pass
            worker_thread.join(timeout=10)
        self.addCleanup(_guaranteed_close)
        self._wait_for_status('worker_running')
        return worker_thread

    def _wait_for_status(self, expected, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = apply.load_run_state(self.run_key)
            if st.get('status') == expected:
                return st
            time.sleep(0.2)
        self.fail('never reached status %r (last was %r)' % (expected, apply.load_run_state(self.run_key).get('status')))

    def test_full_worker_lifecycle_through_final_review(self):
        plan = json.loads(Path('demo/apply_plan_testapp.json').read_text(encoding='utf-8'))
        page1_steps, page2_steps, page3_steps = (plan['pages'][i]['steps'] for i in range(3))

        captured = {}

        def pause_hook(page):
            # Runs synchronously on the worker's OWN thread at the pause
            # checkpoint -- simulates a human correcting a value live in
            # the same page the worker owns, then signals resume itself.
            full_name = page.get_by_label('Full Name', exact=True)
            captured['before_manual_edit'] = full_name.input_value()
            full_name.fill('Jordan ManualEdit')
            apply.resume_signal_path(self.run_key).write_text('resume', encoding='utf-8')

        self._start_worker(pause_hook=pause_hook)

        # --- Page 1: fill only PART of the required fields, then attempt
        # Next -- a genuine client-side validation failure. ---
        partial_steps = [s for s in page1_steps if s['label'] in ('Full Name', 'Email Address')]
        r = apply.send_worker_command(self.run_key, {'cmd': 'fill_steps', 'steps': partial_steps})
        self.assertTrue(r['ok'])
        r = apply.send_worker_command(self.run_key, {'cmd': 'click_next', 'label': 'Next'})
        self.assertFalse(r['transitioned'], 'must not report a transition when required fields are still missing')
        self.assertTrue(r['validation_errors'], 'a genuine validation failure must be visible')

        # --- Now finish page 1's remaining required fields. ---
        remaining_steps = [s for s in page1_steps if s['label'] not in ('Full Name', 'Email Address')]
        r = apply.send_worker_command(self.run_key, {'cmd': 'fill_steps', 'steps': remaining_steps})
        self.assertTrue(all(v['ok'] for v in r['verification']))

        # --- Manual handoff checkpoint, still on page 1: pause, a live
        # edit happens (via pause_hook, on the SAME page), then resume
        # with a session check. This models the login/manual-edit
        # checkpoint the completion criteria call for -- the worker
        # blocks here, nothing is closed or reloaded, and the edit is
        # never overwritten afterward. pause_hook writes the resume
        # signal itself BEFORE the wait begins, so this call returns
        # quickly and deterministically -- no need to race a background
        # thread to observe the transient paused_for_review state, which
        # can flash by in milliseconds between polls (confirmed via the
        # action log: "worker resumed after 0s pause"). ---
        r = apply.send_worker_command(
            self.run_key,
            {'cmd': 'pause', 'reason': 'manual review', 'session_check': {'text_present': ['Personal Information']}},
            timeout_seconds=30)
        self.assertTrue(r['ok'])
        self.assertTrue(r['session_alive'], 'session_check against the same still-open page must pass')
        self._wait_for_status('worker_running')

        # --- NOW advance to page 2 -- the manual edit stays untouched
        # since nothing re-fills page 1's fields after the pause. ---
        r = apply.send_worker_command(self.run_key, {'cmd': 'click_next', 'label': 'Next'})
        self.assertTrue(r['transitioned'])
        self.assertEqual(r['url'], self.url('page2.html'))

        # --- Page 2: repeated education/employment section. ---
        r = apply.send_worker_command(self.run_key, {'cmd': 'fill_steps', 'steps': page2_steps})
        self.assertTrue(all(v['ok'] for v in r['verification']))
        r = apply.send_worker_command(self.run_key, {'cmd': 'click_next', 'label': 'Next'})
        self.assertTrue(r['transitioned'])
        self.assertEqual(r['url'], self.url('page3.html'))

        # --- Page 3: real async upload, radio, checkboxes. ---
        r = apply.send_worker_command(self.run_key, {'cmd': 'fill_steps', 'steps': page3_steps})
        self.assertTrue(r['ok'])
        upload_result = [v for v in r['verification'] if v['label'] == 'Resume (PDF or DOCX)'][0]
        self.assertTrue(upload_result['ok'], 'the async upload must be independently verified, not just attempted')

        r = apply.send_worker_command(self.run_key, {'cmd': 'click_next', 'label': 'Review Application'})
        self.assertTrue(r['transitioned'])
        self.assertEqual(r['url'], self.url('page4_review.html'))

        # --- Reached the actual review page -- never submitted. ---
        r = apply.send_worker_command(self.run_key, {'cmd': 'inspect'})
        self.assertNotIn('Test application submitted', r['body_sample'])

        r = apply.send_worker_command(self.run_key, {'cmd': 'status'})
        messages = [a['message'] for a in r['run_state']['action_log']]
        opens = [m for m in messages if m.startswith('worker opened')]
        self.assertEqual(len(opens), 1, 'exactly one navigation-from-scratch for the whole run: %r' % messages)

        r = apply.send_worker_command(self.run_key, {'cmd': 'close'})
        self.assertTrue(r['closing'])

        # Independently re-read the PERSISTED outcome, proving the manual
        # edit made during the pause reached the actual saved data, not
        # just that the pause/resume calls didn't error. Give the worker
        # thread a moment to finish releasing the profile dir first.
        deadline = time.time() + 10
        while apply.run_lock_path(self.run_key).exists() and time.time() < deadline:
            time.sleep(0.2)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(str(apply.browser_profile_dir(self.run_key)), headless=True)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(self.url('page4_review.html'))
            review_text = page.locator('#review-output').inner_text()
            context.close()
        self.assertIn('Jordan ManualEdit', review_text)
        self.assertNotIn(captured['before_manual_edit'], review_text)

    def test_competing_worker_is_refused(self):
        self._start_worker()

        with self.assertRaises(apply.RunLockedError):
            apply.run_worker(self.run_key, 'demo/test_profile.json', start_url=self.url('page1.html'), headless=True)

    def test_lost_session_stops_further_writes(self):
        self._start_worker()

        def send_pause():
            return apply.send_worker_command(
                self.run_key,
                {'cmd': 'pause', 'reason': 'manual review',
                 'session_check': {'text_present': ['TEXT_THAT_WILL_NEVER_BE_ON_PAGE1_xyz']}},
                timeout_seconds=15)

        holder = {}

        def run_pause():
            holder['result'] = send_pause()

        t = threading.Thread(target=run_pause, daemon=True)
        t.start()
        self._wait_for_status('paused_for_review')
        apply.resume_signal_path(self.run_key).write_text('resume', encoding='utf-8')
        t.join(timeout=10)

        self.assertFalse(holder['result']['session_alive'])
        self.assertEqual(apply.load_run_state(self.run_key)['status'], 'session_lost')

        r = apply.send_worker_command(self.run_key, {'cmd': 'fill_steps', 'steps': [
            {'action': 'fill', 'label': 'Full Name', 'value': 'Should Not Be Written'}]})
        self.assertFalse(r['ok'], 'a write attempted after a reported session loss must be refused')


class QuestionContainerMappingRegression(unittest.TestCase):
    """A real, measured problem (encountered on a live UKG/UltiPro
    application): multiple yes/no questions share generic internal radio
    group names (MultipleChoiceResponse0, MultipleChoiceResponse1, ...)
    and IDENTICAL "Yes"/"No" option labels -- a page-wide label search or
    any positional/index-based guess cannot safely tell them apart.
    find_question_container/answer_radio_question/read_question_container_state
    must target and verify a question by its own real text instead."""

    @classmethod
    def setUpClass(cls):
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory='testapp')
        cls.httpd = socketserver.TCPServer(('127.0.0.1', 0), handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def url(self, path):
        return 'http://127.0.0.1:%d/%s' % (self.port, path)

    def test_answers_the_correct_question_despite_identical_labels_and_generic_names(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(self.url('page_generic_radio_questions.html'))

            containers = apply.find_question_container(page, 'Have you been previously employed with this company?')
            self.assertEqual(len(containers), 1)
            result = apply.answer_radio_question(containers[0], 'No')
            self.assertTrue(result['ok'])

            # The OTHER identically-labeled "Yes"/"No" questions must be
            # completely untouched -- this is the actual bug being
            # guarded against, not just whether the answer call itself
            # reports success.
            work_auth = apply.find_question_container(page, 'Are you eligible to work in the U.S.?')[0]
            eighteen = apply.find_question_container(page, 'Are you at least 18 years or older?')[0]
            relatives = apply.find_question_container(page, 'Do you have any relatives employed by this company?')[0]
            for container, label in [(work_auth, 'work-auth'), (eighteen, 'eighteen'), (relatives, 'relatives')]:
                state = apply.read_question_container_state(container)
                self.assertEqual(state['checked_labels'], [], '%s must be untouched, got %r' % (label, state['checked_labels']))

            # The intended question's own state, read back WITHIN its own
            # scoped container, must show exactly the selected answer.
            prior_employment = apply.find_question_container(page, 'Have you been previously employed with this company?')[0]
            state = apply.read_question_container_state(prior_employment)
            self.assertEqual(state['checked_labels'], ['No'])
            self.assertIn('previously employed', state['container_text'])

            browser.close()

    def test_refuses_when_question_text_does_not_match_exactly_one_container(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(self.url('page_generic_radio_questions.html'))
            containers = apply.find_question_container(page, 'This question does not exist on the page')
            self.assertEqual(len(containers), 0, 'a non-matching question text must resolve to zero containers, never a guess')
            browser.close()

    def test_multiple_choice_question_with_more_than_two_options(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(self.url('page_generic_radio_questions.html'))
            containers = apply.find_question_container(page, 'Highest level of education')
            self.assertEqual(len(containers), 1)
            result = apply.answer_radio_question(containers[0], "Bachelor's Degree")
            self.assertTrue(result['ok'])
            state = apply.read_question_container_state(containers[0])
            self.assertEqual(state['checked_labels'], ["Bachelor's Degree"])
            browser.close()


class BodyWindowRegression(unittest.TestCase):
    """inspect's body_sample used to be a silent body_text[:3000]. On a page
    where a long job description precedes the form (CBRE, Bain), that cap
    structurally cannot reach the part you need, and the caller could not
    distinguish a short page from a truncated one."""

    def test_default_window_matches_the_old_cap(self):
        out = apply.body_window('x' * 5000)
        self.assertEqual(len(out['body_sample']), 3000)
        self.assertEqual(out['body_total'], 5000)
        self.assertTrue(out['body_truncated'])

    def test_offset_reaches_text_the_old_cap_could_not(self):
        text = ('a' * 3000) + 'SPONSORSHIP STATEMENT'
        self.assertNotIn('SPONSORSHIP', apply.body_window(text)['body_sample'])
        out = apply.body_window(text, offset=3000, chars=100)
        self.assertIn('SPONSORSHIP STATEMENT', out['body_sample'])
        self.assertEqual(out['body_offset'], 3000)
        self.assertFalse(out['body_truncated'])

    def test_short_page_is_not_reported_as_truncated(self):
        out = apply.body_window('short page')
        self.assertFalse(out['body_truncated'])
        self.assertEqual(out['body_sample'], 'short page')

    def test_window_past_the_end_is_empty_not_an_error(self):
        out = apply.body_window('abc', offset=100, chars=50)
        self.assertEqual(out['body_sample'], '')
        self.assertEqual(out['body_total'], 3)
        self.assertFalse(out['body_truncated'])

    def test_degenerate_arguments_fall_back_instead_of_returning_nothing(self):
        for bad in (0, -5, None):
            out = apply.body_window('y' * 4000, chars=bad)
            self.assertEqual(len(out['body_sample']), 3000, 'chars=%r' % bad)
        self.assertEqual(apply.body_window('y' * 10, offset=-3)['body_offset'], 0)


class BrowserClosedDuringHandoffRegression(unittest.TestCase):
    """Measured crash (2026-10-05, Gartner req 112849): the worker was
    paused at Workday step 1 for human sign-in, the browser window was
    closed, and the idle poll's page.wait_for_timeout() raised
    TargetClosedError. It escaped unhandled -- traceback, exit code 1, and
    a stale lock file -- which is indistinguishable from a real engine
    fault. Closing the window during a handoff is the single most likely
    moment for this, since that is when a human is actually at the keyboard."""

    def test_target_closed_is_recognized_by_class_name(self):
        err = type('TargetClosedError', (Exception,), {})('anything at all')
        self.assertTrue(apply._is_target_closed_error(err))

    def test_target_closed_is_recognized_by_message_across_versions(self):
        for msg in (
            'Page.wait_for_timeout: Target page, context or browser has been closed',
            'Browser has been closed',
            'Target closed',
        ):
            self.assertTrue(apply._is_target_closed_error(Exception(msg)), msg)

    def test_real_faults_still_propagate(self):
        """The narrow part. A selector miss or strict-mode violation must NOT
        be swallowed as "the human closed the window" -- that would convert
        genuine targeting bugs into silent clean exits."""
        for msg in (
            'Timeout 30000ms exceeded waiting for locator("#nope")',
            'strict mode violation: locator("a") resolved to 7 elements',
            'Element is not visible',
            'net::ERR_CONNECTION_REFUSED',
        ):
            self.assertFalse(apply._is_target_closed_error(Exception(msg)), msg)


if __name__ == '__main__':
    unittest.main()
