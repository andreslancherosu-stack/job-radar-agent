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
