"""MOCKED-RUN tests for the agent loop. No network, no API key, no real model.

These prove the LOOP MECHANICS are correct:
  - the model's requested tools actually execute
  - results go back paired to the right tool_use_id
  - a human-input request stops the run cleanly
  - the step limit is enforced
  - an unknown/invalid tool call becomes a recoverable observation, not a crash

They prove NOTHING about model judgment. Only a real API run does that, and
it is labelled as such in the README.
"""
import json
import types
import unittest

import agent
import tools


def text_block(text):
    return types.SimpleNamespace(type='text', text=text)


def tool_use_block(block_id, name, payload):
    return types.SimpleNamespace(type='tool_use', id=block_id, name=name, input=payload)


class ScriptedClient:
    """Stands in for anthropic.Anthropic(). Replays a fixed list of responses."""

    def __init__(self, scripted_contents, stop_reasons=None):
        self._scripted = list(scripted_contents)
        self._stop_reasons = list(stop_reasons) if stop_reasons else None
        self.received_messages = []
        self.messages = self

    def create(self, **kwargs):
        self.received_messages.append(kwargs['messages'])
        content = self._scripted.pop(0)
        if self._stop_reasons:
            stop = self._stop_reasons.pop(0)
        else:
            stop = 'tool_use' if any(getattr(b, 'type', None) == 'tool_use' for b in content) else 'end_turn'
        return types.SimpleNamespace(
            content=content,
            usage=types.SimpleNamespace(input_tokens=100, output_tokens=20),
            stop_reason=stop,
        )


class TransientConnectionErrorIsRetried(unittest.TestCase):
    """Regression for a measured crash (2026-10-04): a real network blip raised
    anthropic.APIConnectionError mid-run and nothing caught it, crashing the
    process after earlier tool calls had already made verified progress on a
    live form. Bounded to 3 total attempts -- this project does not retry
    unboundedly."""

    class FlakyThenOkClient:
        def __init__(self, fail_times, final_content):
            import anthropic
            self.fail_times = fail_times
            self.final_content = final_content
            self.calls = 0
            self.messages = self

        def create(self, **kwargs):
            import anthropic
            self.calls += 1
            if self.calls <= self.fail_times:
                raise anthropic.APIConnectionError(request=types.SimpleNamespace())
            return types.SimpleNamespace(
                content=self.final_content,
                usage=types.SimpleNamespace(input_tokens=10, output_tokens=5),
                stop_reason='end_turn')

    class AlwaysFailsClient:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            import anthropic
            raise anthropic.APIConnectionError(request=types.SimpleNamespace())

    def test_succeeds_after_one_transient_failure(self):
        client = self.FlakyThenOkClient(fail_times=1, final_content=[text_block('Recovered.')])
        result = agent.run_agent('task', client=client, verbose=False, retry_backoff_seconds=0)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(client.calls, 2)

    def test_gives_up_after_three_attempts_and_reports_connection_failed(self):
        client = self.AlwaysFailsClient()
        result = agent.run_agent('task', client=client, verbose=False, max_steps=5, retry_backoff_seconds=0)
        self.assertEqual(result['status'], 'connection_failed')
        self.assertIn('3 attempts', result['problem'])
        # Must not have silently looped past the bound into a second API "step".
        self.assertEqual(result['metrics']['api_calls'], 0)


class CompletionIsNotAssumed(unittest.TestCase):
    """Silence from the model is not success. These guard requirement 3."""

    def test_truncated_response_is_not_reported_as_completed(self):
        client = ScriptedClient([[text_block('I was about to say that the candi')]],
                                stop_reasons=['max_tokens'])
        result = agent.run_agent('task', client=client, verbose=False,
                                 required_outcome_tool='save_assessment')
        self.assertEqual(result['status'], 'truncated')
        self.assertIn('cut off', result['problem'])
        self.assertEqual(result['metrics']['api_stop_reason'], 'max_tokens')

    def test_ending_without_the_required_save_is_not_completed(self):
        client = ScriptedClient([[text_block('Looks like a decent role overall.')]],
                                stop_reasons=['end_turn'])
        result = agent.run_agent('task', client=client, verbose=False,
                                 required_outcome_tool='save_assessment')
        self.assertEqual(result['status'], 'ended_without_required_outcome')
        self.assertFalse(result['required_outcome_met'])

    def test_a_failed_save_does_not_count_as_the_required_outcome(self):
        """A rejected save must not be mistaken for a completed task."""
        bad_save = {'source': 'momentum_associate_strategy_ops_analyst.json', 'fit': 'GOOD FIT',
                    'next_action': 'apply',
                    'experience': {'status': 'pass', 'quote': 'NOT IN THE POSTING AT ALL',
                                   'reason': 'fabricated'},
                    'education': {'status': 'uncertain', 'quote': '', 'reason': 'n/a'},
                    'work_authorization': {'status': 'uncertain', 'quote': '', 'reason': 'n/a'},
                    'career_fit': {'status': 'uncertain', 'quote': '', 'reason': 'n/a'}}
        client = ScriptedClient([
            [tool_use_block('toolu_s', 'save_assessment', bad_save)],
            [text_block('Saved, I think.')],
        ], stop_reasons=['tool_use', 'end_turn'])
        result = agent.run_agent('task', client=client, verbose=False,
                                 required_outcome_tool='save_assessment')
        self.assertEqual(result['status'], 'ended_without_required_outcome')
        self.assertNotIn('save_assessment', result['metrics']['tools_that_succeeded'])

    def test_successful_save_then_end_turn_is_completed(self):
        client = ScriptedClient([
            [tool_use_block('toolu_ok', 'get_profile_facts', {'sections': ['identity']})],
            [text_block('Here is my conclusion.')],
        ], stop_reasons=['tool_use', 'end_turn'])
        result = agent.run_agent('task', client=client, verbose=False,
                                 required_outcome_tool='get_profile_facts')
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['required_outcome_met'])


class AgentLoopMechanics(unittest.TestCase):

    def test_tool_result_is_returned_with_the_matching_tool_use_id(self):
        client = ScriptedClient([
            [tool_use_block('toolu_ABC123', 'get_profile_facts', {'sections': ['work_authorization']})],
            [text_block('Done.')],
        ])
        result = agent.run_agent('task', client=client, verbose=False)

        self.assertEqual(result['status'], 'completed')
        # Second API call must carry our tool_result, paired by id.
        second_call = client.received_messages[1]
        tool_results = [b for m in second_call if isinstance(m.get('content'), list)
                        for b in m['content'] if isinstance(b, dict) and b.get('type') == 'tool_result']
        self.assertEqual(len(tool_results), 1)
        self.assertEqual(tool_results[0]['tool_use_id'], 'toolu_ABC123')
        self.assertFalse(tool_results[0]['is_error'])
        # And it must contain the REAL observation, not a placeholder.
        payload = json.loads(tool_results[0]['content'])
        self.assertIn('current_work_authorization_status', payload['facts'])

    def test_observations_actually_reach_the_model_and_can_change_the_answer(self):
        """The duplicate check is a real observation the model can act on."""
        client = ScriptedClient([
            [tool_use_block('toolu_1', 'check_application_history',
                            {'company': 'Momentum', 'job_id': '8607054002'})],
            [text_block('Already applied; skipping.')],
        ])
        result = agent.run_agent('task', client=client, verbose=False)
        observed = [t for t in result['transcript'] if t['role'] == 'tool'][0]['observation']
        self.assertTrue(observed['already_applied'],
                        'Momentum is a confirmed prior submission and must be reported as such')

    def test_request_missing_fact_stops_the_run_for_the_human(self):
        client = ScriptedClient([
            [tool_use_block('toolu_2', 'request_missing_fact',
                            {'question': 'What is your exact availability date?',
                             'why_needed': 'The form requires a start date and the profile leaves it null.'})],
        ])
        result = agent.run_agent('task', client=client, verbose=False)
        self.assertEqual(result['status'], 'needs_human_input')
        self.assertEqual(result['metrics']['loop_stop_reason'], 'human_input_required')
        self.assertIn('availability', result['question']['question'])

    def test_unknown_tool_becomes_a_recoverable_observation_not_a_crash(self):
        client = ScriptedClient([
            [tool_use_block('toolu_3', 'submit_application', {'confirm': True})],
            [text_block('Understood, that tool does not exist.')],
        ])
        result = agent.run_agent('task', client=client, verbose=False)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['metrics']['tool_failures'], 1)
        observed = [t for t in result['transcript'] if t['role'] == 'tool'][0]
        self.assertTrue(observed['is_error'])
        self.assertIn('unknown tool', observed['observation']['error'])

    def test_invalid_arguments_are_rejected_before_execution(self):
        client = ScriptedClient([
            [tool_use_block('toolu_4', 'get_profile_facts', {'sections': ['salary_history']})],
            [text_block('I will use a valid section instead.')],
        ])
        result = agent.run_agent('task', client=client, verbose=False)
        observed = [t for t in result['transcript'] if t['role'] == 'tool'][0]
        self.assertTrue(observed['is_error'])
        self.assertIn('unknown section', observed['observation']['error'])

    def test_step_limit_is_enforced(self):
        # A model that never stops asking for tools must still terminate.
        client = ScriptedClient([[tool_use_block('t%d' % i, 'get_profile_facts',
                                                 {'sections': ['identity']})] for i in range(10)])
        result = agent.run_agent('task', client=client, max_steps=3, verbose=False)
        self.assertEqual(result['status'], 'max_steps_reached')
        self.assertEqual(result['metrics']['api_calls'], 3)

    def test_metrics_are_measured(self):
        client = ScriptedClient([
            [tool_use_block('toolu_5', 'get_profile_facts', {'sections': ['identity']})],
            [text_block('Done.')],
        ])
        result = agent.run_agent('task', client=client, verbose=False)
        m = result['metrics']
        self.assertEqual(m['api_calls'], 2)
        self.assertEqual(m['tool_calls'], 1)
        self.assertEqual(m['input_tokens'], 200)
        self.assertGreaterEqual(m['elapsed_seconds'], 0)


class ConfirmedAnswersReachTheAgent(unittest.TestCase):
    """Regression for a measured information-flow defect (2026-10-03).

    The agent had no tool that could read private/answer_bank.json. Fifteen
    answers the user confirmed on 2026-09-26 were therefore invisible to the
    model, which correctly reported them "unknown" and asked the user to
    supply them a SECOND time. Resume-derived uncertainty in profile.json
    must never override an explicit prior confirmation.
    """

    # Every one of these was confirmed by the user and then re-asked by the agent.
    PREVIOUSLY_RE_ASKED = [
        'What is your cumulative GPA?',
        'Are you comfortable working onsite 5 days/week in our Dallas office at 2601 Olive Street?',
        'If yes, are you currently in a period of Optional Practical Training (OPT)?',
        'I consent to have my personal data disclosed to other Momentum Group entities to be '
        'considered for all open positions that fit my profile.',
    ]

    def test_the_tool_is_registered_at_all(self):
        """The root cause was a missing tool, not a bad answer."""
        self.assertIn('get_confirmed_answers', tools.TOOL_REGISTRY)
        self.assertIn('get_confirmed_answers', {d['name'] for d in tools.TOOL_DEFINITIONS})

    def test_each_previously_re_asked_question_now_resolves(self):
        observation, is_error = tools.execute_tool(
            'get_confirmed_answers', {'questions': self.PREVIOUSLY_RE_ASKED})
        self.assertFalse(is_error)
        self.assertEqual(observation['no_confirmed_answer_for'], [],
                         'these were already confirmed and must not come back unanswered')
        answers_by_q = {m['question']: m['answer'] for m in observation['matched']}
        self.assertEqual(answers_by_q['What is your cumulative GPA?'], '3.0-3.49')
        self.assertEqual(
            answers_by_q['If yes, are you currently in a period of Optional Practical Training (OPT)?'],
            'No')

    def test_unconfirmed_entries_are_never_returned(self):
        observation, _ = tools.execute_tool('get_confirmed_answers', {})
        bank = __import__('answers').load_bank()
        confirmed_count = sum(1 for e in bank.get('entries', []) if e.get('confirmed'))
        self.assertEqual(observation['count'], confirmed_count)

    def test_the_observation_actually_reaches_the_model_through_the_loop(self):
        """End-to-end through run_agent: the answer must arrive in a tool_result."""
        client = ScriptedClient([
            [tool_use_block('toolu_ab', 'get_confirmed_answers',
                            {'questions': ['What is your cumulative GPA?']})],
            [text_block('GPA band is already confirmed; I will not ask again.')],
        ], stop_reasons=['tool_use', 'end_turn'])
        result = agent.run_agent('task', client=client, verbose=False)

        second_call = client.received_messages[1]
        tool_results = [b for m in second_call if isinstance(m.get('content'), list)
                        for b in m['content'] if isinstance(b, dict) and b.get('type') == 'tool_result']
        payload = json.loads(tool_results[0]['content'])
        self.assertEqual(payload['matched'][0]['answer'], '3.0-3.49',
                         'the confirmed answer must physically reach the model')
        self.assertEqual(result['status'], 'completed')

    def test_scope_rule_warns_against_blind_consent_reuse(self):
        observation, _ = tools.execute_tool('get_confirmed_answers', {})
        self.assertIn('CONSENT', observation['scope_rule'].upper())


class SafetyInvariants(unittest.TestCase):

    def test_no_submission_tool_exists_anywhere(self):
        for name in tools.TOOL_REGISTRY:
            self.assertNotIn('submit', name.lower(),
                             'a submission tool must never be registered')
        for definition in tools.TOOL_DEFINITIONS:
            self.assertNotIn('submit', definition['name'].lower())

    def test_registry_and_definitions_describe_the_same_tools(self):
        self.assertEqual(set(tools.TOOL_REGISTRY), {d['name'] for d in tools.TOOL_DEFINITIONS})

    def test_every_tool_definition_has_a_schema_the_api_will_accept(self):
        for definition in tools.TOOL_DEFINITIONS:
            self.assertIn('name', definition)
            self.assertIn('description', definition)
            schema = definition['input_schema']
            self.assertEqual(schema['type'], 'object')
            self.assertIn('properties', schema)
            for required_field in schema.get('required', []):
                self.assertIn(required_field, schema['properties'],
                              '%s: required field %r is not defined in properties'
                              % (definition['name'], required_field))


class EvidenceGate(unittest.TestCase):
    """save_assessment must reject fabricated evidence. This is the anti-hallucination gate."""

    def test_assessment_with_a_non_verbatim_quote_is_rejected(self):
        fabricated = {
            'source': 'momentum_associate_strategy_ops_analyst.json',
            'fit': 'GOOD FIT',
            'next_action': 'apply',
            'experience': {'status': 'pass', 'quote': 'we require exactly zero years of experience',
                           'reason': 'invented quote'},
            'education': {'status': 'uncertain', 'quote': '', 'reason': 'n/a'},
            'work_authorization': {'status': 'uncertain', 'quote': '', 'reason': 'n/a'},
            'career_fit': {'status': 'uncertain', 'quote': '', 'reason': 'n/a'},
        }
        observation, is_error = tools.execute_tool('save_assessment', fabricated)
        self.assertTrue(is_error, 'a fabricated quote must not be saved')
        self.assertIn('Evidence not found verbatim', observation['error'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
