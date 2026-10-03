import os
import unittest
from unittest import mock

import llm_client
import radar


class Availability(unittest.TestCase):
    def test_unavailable_when_env_var_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('ANTHROPIC_API_KEY', None)
            self.assertFalse(llm_client.available())

    def test_available_when_env_var_set(self):
        with mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'sk-ant-fake-for-test-only'}):
            self.assertTrue(llm_client.available())


class RefusesWithoutKey(unittest.TestCase):
    def test_assess_raises_clear_error_without_network_call(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('ANTHROPIC_API_KEY', None)
            with self.assertRaises(RuntimeError) as ctx:
                llm_client.assess_with_llm({'text': 'x', 'source_url': 'y'}, {})
            self.assertIn('ANTHROPIC_API_KEY', str(ctx.exception))


class SchemaMatchesRadarValidator(unittest.TestCase):
    def test_dimensions_match_radar_dimensions_tuple(self):
        schema_dims = {k for k in llm_client.ASSESSMENT_SCHEMA['properties']
                        if k not in ('fit', 'next_action')}
        self.assertEqual(schema_dims, set(radar.DIMENSIONS))

    def test_fit_enum_matches_radar_accepted_values(self):
        self.assertEqual(set(llm_client.ASSESSMENT_SCHEMA['properties']['fit']['enum']),
                          {'GOOD FIT', 'STRETCH BUT REASONABLE', 'POOR FIT'})

    def test_status_enum_matches_radar_accepted_values(self):
        status_enum = set(llm_client._DIMENSION_SCHEMA['properties']['status']['enum'])
        self.assertEqual(status_enum, {'pass', 'uncertain', 'fail'})


class BuildUserContent(unittest.TestCase):
    def test_includes_posting_text_and_profile_facts(self):
        posting = {'text': 'Graduate students accepted.', 'source_url': 'https://x', 'source_kind': 'employer'}
        profile = {'name': 'Taylor Example'}
        content = llm_client.build_user_content(posting, profile)
        self.assertIn('Graduate students accepted.', content)
        self.assertIn('Taylor Example', content)


if __name__ == '__main__':
    unittest.main()
