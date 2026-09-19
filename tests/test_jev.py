#!/usr/bin/env python3
"""Offline checks: every Jev reply is faked, no network, no key needed."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
from jev import Jev  # noqa: E402


class JevTest(unittest.TestCase):
  questions = {
    'urgent': {'type': 'boolean', 'instructions': 'Is it urgent?'},
    'team': {'type': 'choice', 'instructions': 'Who owns it?',
             'criteria': {'billing': 'money', 'technical': 'bugs'}},
    'anger': {'type': 'score', 'instructions': 'How angry?',
              'criteria': ['Calm', 'Concerned', 'Furious']},
  }

  def setUp(self):
    Jev.credentialsPath = os.path.join(tempfile.mkdtemp(), 'credentials')
    for name in ('TYPESAFE_API_KEY', 'AI_GATEWAY_API_KEY', 'VERCEL_AI_GATEWAY_API_KEY', 'OPENROUTER_API_KEY'):
      os.environ.pop(name, None)
    self.sent = {}

  def fake(self, reply):
    def transport(url, body, headers, timeout):
      self.sent = {'url': url, 'body': json.loads(body), 'headers': headers}
      return json.dumps(reply).encode('utf-8')
    return transport

  def test_noul_dialect_and_answers(self):
    os.environ['TYPESAFE_API_KEY'] = 'k'
    reply = {'answers': {
      'urgent': {'type': 'noul', 'noul': 0.99},
      'team': {'type': 'choice', 'choice': 'technical',
               'probabilities': {'billing': 0.29, 'technical': 0.71}, 'confidence': 0.56},
      'anger': {'type': 'score', 'score': 2.0, 'probabilities': {'0': 0, '1': 0, '2': 1}},
    }, 'usage': {'cost': 0.000018}}
    out = Jev.ask('ticket text', self.questions, transport=self.fake(reply))
    self.assertEqual(self.sent['url'], 'https://api.typesafe.ai/v1/systemone')
    self.assertEqual(self.sent['body']['questions']['urgent']['type'], 'noul')
    self.assertEqual(self.sent['body']['model'], 'jev-latest')
    self.assertEqual(out['answers']['urgent'], {'type': 'boolean', 'probability': 0.99})
    self.assertEqual(out['answers']['team']['choice'], 'technical')
    self.assertEqual(out['answers']['anger']['label'], 'Furious')

  def test_vercel_dialect_headers_and_confidence(self):
    os.environ['AI_GATEWAY_API_KEY'] = 'k'
    reply = {'answers': {
      'urgent': {'type': 'boolean', 'probability': 0.97},
      'team': {'type': 'choice', 'choice': 'billing', 'probabilities': {'billing': 0.9, 'technical': 0.1}},
      'anger': {'type': 'score', 'score': 0.4, 'probabilities': {'0': 0.6, '1': 0.4}},
    }, 'providerMetadata': {'typesafe': {'confidence': {'team': 0.9}}}}
    out = Jev.ask('ticket text', self.questions, transport=self.fake(reply))
    self.assertEqual(self.sent['headers']['ai-model-id'], 'typesafe-ai/jev')
    self.assertEqual(self.sent['headers']['ai-gateway-protocol-version'], '0.0.1')
    self.assertNotIn('model', self.sent['body'])
    self.assertEqual(self.sent['body']['questions']['urgent']['type'], 'boolean')
    self.assertEqual(out['answers']['team']['confidence'], 0.9)
    self.assertEqual(out['answers']['anger']['label'], 'Calm')

  def test_provider_precedence_and_openrouter_path(self):
    os.environ['OPENROUTER_API_KEY'] = 'k'
    reply = {'answers': {'urgent': {'type': 'noul', 'noul': 0.1}}}
    out = Jev.ask('x', {'urgent': self.questions['urgent']}, transport=self.fake(reply))
    self.assertEqual(out['provider'], 'openrouter')
    self.assertEqual(self.sent['url'], 'https://openrouter.ai/api/alpha/decisions')
    os.environ['TYPESAFE_API_KEY'] = 'k'
    out = Jev.ask('x', {'urgent': self.questions['urgent']}, transport=self.fake(reply))
    self.assertEqual(out['provider'], 'typesafe')

  def test_failures_raise_instead_of_returning_junk(self):
    with self.assertRaises(Jev.Error) as no_key:
      Jev.ask('x', {'urgent': self.questions['urgent']}, transport=self.fake({}))
    self.assertEqual(no_key.exception.code, 'no_key')
    os.environ['TYPESAFE_API_KEY'] = 'k'
    for reply in ({'answers': {'team': {'type': 'choice', 'choice': 'sales', 'probabilities': {}}}},
                  {'answers': {'team': {'type': 'choice'}}},
                  {'nope': 1}):
      with self.assertRaises(Jev.Error):
        Jev.ask('x', {'team': self.questions['team']}, transport=self.fake(reply))
    with self.assertRaises(Jev.Error):
      Jev.ask('x', {'bad': {'type': 'choice', 'instructions': 'i', 'criteria': {'one': 'only'}}},
              transport=self.fake({}))


if __name__ == '__main__':
  unittest.main()
