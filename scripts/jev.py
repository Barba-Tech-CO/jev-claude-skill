#!/usr/bin/env python3
"""Call TypeSafe's Jev decision model through TypeSafe direct, OpenRouter or Vercel AI Gateway.

Stdlib only. One wire format in, one normalized answer shape out:

  boolean -> {"type": "boolean", "probability": 0.0..1.0}
  choice  -> {"type": "choice", "choice": key, "probabilities": {...}, "confidence": 0..1}
  score   -> {"type": "score", "score": float, "probabilities": {...}, "confidence": 0..1}
"""
from __future__ import annotations

import argparse
import getpass
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request


class Jev:
  """Provider-agnostic Jev client."""

  providers = {
    'typesafe': {
      'url': 'https://api.typesafe.ai/v1/systemone',
      'model': 'jev-latest',
      'keys': ('TYPESAFE_API_KEY',),
      'dialect': 'noul',
    },
    'openrouter': {
      'url': 'https://openrouter.ai/api/alpha/decisions',
      'model': 'typesafe/jev-1.13',
      'keys': ('OPENROUTER_API_KEY',),
      'dialect': 'noul',
    },
    'vercel': {
      'url': 'https://ai-gateway.vercel.sh/v4/ai/evaluation-model',
      'model': 'typesafe-ai/jev',
      'keys': ('AI_GATEWAY_API_KEY', 'VERCEL_AI_GATEWAY_API_KEY'),
      'dialect': 'boolean',
    },
  }
  order = ('typesafe', 'vercel', 'openrouter')
  maxStateChars = 60000
  credentialsPath = os.path.expanduser(os.environ.get('JEV_CREDENTIALS', '~/.config/jev/credentials'))

  class Error(RuntimeError):
    def __init__(self, code: str, detail: str = '') -> None:
      super().__init__(f'{code}: {detail}' if detail else code)
      self.code = code

  @staticmethod
  def stored() -> dict:
    try:
      with open(Jev.credentialsPath, encoding='utf-8') as handle:
        saved = json.load(handle)
      return saved if isinstance(saved, dict) else {}
    except (OSError, json.JSONDecodeError):
      return {}

  @staticmethod
  def store(provider: str, key: str) -> str:
    """Write the key to a 0600 file. Never logged, never echoed back."""
    saved = Jev.stored()
    saved[provider] = key
    folder = os.path.dirname(Jev.credentialsPath)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    handle = os.open(Jev.credentialsPath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, 'w', encoding='utf-8') as file:
      json.dump(saved, file)
    os.chmod(Jev.credentialsPath, 0o600)
    return Jev.credentialsPath

  @staticmethod
  def keyFor(provider: str) -> str:
    for name in Jev.providers[provider]['keys']:
      value = os.environ.get(name, '').strip()
      if value:
        return value
    return str(Jev.stored().get(provider, '')).strip()

  @staticmethod
  def resolve(provider: str = 'auto') -> str:
    if provider != 'auto':
      if provider not in Jev.providers:
        raise Jev.Error('bad_provider', provider)
      if not Jev.keyFor(provider):
        raise Jev.Error('no_key', f'{provider}: set {Jev.providers[provider]["keys"][0]}')
      return provider
    for name in Jev.order:
      if Jev.keyFor(name):
        return name
    raise Jev.Error('no_key', 'set TYPESAFE_API_KEY, AI_GATEWAY_API_KEY or OPENROUTER_API_KEY')

  @staticmethod
  def encodeQuestions(questions: dict, dialect: str) -> dict:
    """Accept `boolean` or `noul` from the caller; emit what the provider expects."""
    out = {}
    for name, question in questions.items():
      kind = question.get('type')
      if kind not in ('boolean', 'noul', 'choice', 'score'):
        raise Jev.Error('bad_question', f'{name}: unknown type {kind!r}')
      if not question.get('instructions'):
        raise Jev.Error('bad_question', f'{name}: no instructions')
      copy = dict(question)
      if kind in ('boolean', 'noul'):
        copy['type'] = 'boolean' if dialect == 'boolean' else 'noul'
      elif kind == 'choice':
        if not isinstance(copy.get('criteria'), dict) or len(copy['criteria']) < 2:
          raise Jev.Error('bad_question', f'{name}: choice needs 2+ criteria options')
      else:
        if not isinstance(copy.get('criteria'), list) or len(copy['criteria']) < 2:
          raise Jev.Error('bad_question', f'{name}: score needs 2+ ordered levels')
      out[name] = copy
    return out

  @staticmethod
  def unit(value, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
      raise Jev.Error('malformed', f'{where} is not a number')
    return min(1.0, max(0.0, float(value)))

  @staticmethod
  def normalize(name: str, question: dict, answer, confidences: dict) -> dict:
    if not isinstance(answer, dict):
      raise Jev.Error('malformed', f'{name}: no answer')
    asked = question['type']
    confidence = answer.get('confidence', confidences.get(name, 1.0))
    if asked in ('boolean', 'noul'):
      raw = answer.get('probability', answer.get('noul'))
      return {'type': 'boolean', 'probability': Jev.unit(raw, f'{name}.probability')}
    if asked == 'choice':
      picked = answer.get('choice')
      if picked not in question['criteria']:
        raise Jev.Error('malformed', f'{name}: chose an option that was not offered')
      raw = answer.get('probabilities') or {}
      return {
        'type': 'choice',
        'choice': picked,
        'probabilities': {k: Jev.unit(v, f'{name}.p[{k}]') for k, v in raw.items()},
        'confidence': Jev.unit(confidence, f'{name}.confidence'),
      }
    levels = len(question['criteria'])
    value = answer.get('score')
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
      raise Jev.Error('malformed', f'{name}: no numeric score')
    if not -0.5 <= float(value) <= levels - 0.5:
      raise Jev.Error('malformed', f'{name}: score off the rubric')
    raw = answer.get('probabilities') or {}
    return {
      'type': 'score',
      'score': float(value),
      'label': question['criteria'][min(levels - 1, max(0, int(round(float(value)))))],
      'probabilities': {str(k): Jev.unit(v, f'{name}.p[{k}]') for k, v in raw.items()},
      'confidence': Jev.unit(confidence, f'{name}.confidence'),
    }

  @staticmethod
  def post(url: str, body: bytes, headers: dict, timeout: float) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
      with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(1_000_000)
    except urllib.error.HTTPError as error:
      code = {401: 'auth_failed', 403: 'auth_failed', 402: 'credits_exhausted',
              429: 'rate_limited', 529: 'overloaded'}.get(error.code, f'http_{error.code}')
      raise Jev.Error(code, error.read(2000).decode('utf-8', 'replace')) from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
      raise Jev.Error('network', str(error)) from None

  @staticmethod
  def ask(state, questions: dict, *, provider: str = 'auto', model: str = '',
          timeout: float = 10.0, transport=None, api_key: str = '') -> dict:
    """One request, every question answered in parallel by the model."""
    if not questions:
      raise Jev.Error('bad_question', 'no questions')
    name = provider if api_key and provider != 'auto' else Jev.resolve(provider)
    spec = Jev.providers[name]
    encoded = state if isinstance(state, str) else json.dumps(state, default=str)
    if len(encoded) > Jev.maxStateChars:
      raise Jev.Error('state_too_large', f'{len(encoded)} chars')
    wire = Jev.encodeQuestions(questions, spec['dialect'])
    headers = {
      'Authorization': f'Bearer {api_key or Jev.keyFor(name)}',
      'Content-Type': 'application/json',
      'Accept': 'application/json',
    }
    payload = {'state': state, 'questions': wire}
    if name == 'vercel':
      headers['ai-model-id'] = model or spec['model']
      headers['ai-evaluation-model-specification-version'] = '4'
      headers['ai-gateway-protocol-version'] = '0.0.1'
    else:
      payload['model'] = model or os.environ.get('JEV_MODEL') or spec['model']
    started = time.monotonic()
    raw = (transport or Jev.post)(spec['url'], json.dumps(payload).encode('utf-8'), headers, timeout)
    try:
      reply = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
      raise Jev.Error('malformed', 'reply is not JSON') from None
    answers = reply.get('answers') if isinstance(reply, dict) else None
    if not isinstance(answers, dict):
      raise Jev.Error('malformed', 'reply has no answers')
    meta = reply.get('providerMetadata') or {}
    confidences = (meta.get('typesafe') or {}).get('confidence') or {}
    return {
      'provider': name,
      'model': reply.get('model') or model or spec['model'],
      'latency_ms': int((time.monotonic() - started) * 1000),
      'usage': reply.get('usage') or {},
      'answers': {k: Jev.normalize(k, q, answers.get(k), confidences) for k, q in questions.items()},
    }

  @staticmethod
  def setKey(provider: str) -> dict:
    """Read one key from a pipe or a hidden prompt, verify it, store it 0600."""
    if provider not in Jev.providers:
      raise Jev.Error('bad_provider', provider)
    key = (sys.stdin.readline() if not sys.stdin.isatty() else
           getpass.getpass(f'{provider} API key (hidden, not echoed): ')).strip()
    if not key:
      raise Jev.Error('no_key', 'nothing was entered')
    try:
      Jev.ask('The build finished and all tests passed.',
              {'ok': {'type': 'boolean', 'instructions': 'The text reports a successful outcome'}},
              provider=provider, timeout=30.0, api_key=key)
    except Jev.Error as error:
      return {'status': 'rejected', 'provider': provider, 'error': error.code,
              'detail': str(error)[:500], 'key_length': len(key), 'stored': False}
    return {'status': 'stored', 'provider': provider, 'verified': True,
            'path': Jev.store(provider, key), 'key_length': len(key)}

  @staticmethod
  def doctor() -> dict:
    report = {}
    for name in Jev.order:
      key = Jev.keyFor(name)
      source = 'env' if any(os.environ.get(k, '').strip() for k in Jev.providers[name]['keys']) \
        else ('file' if name in Jev.stored() else 'none')
      entry = {'key_present': bool(key), 'key_source': source,
               'url': Jev.providers[name]['url'], 'reachable': False}
      if key:
        try:
          Jev.ask('The build finished and all tests passed.',
                  {'ok': {'type': 'boolean', 'instructions': 'The text reports a successful outcome'}},
                  provider=name, timeout=15.0)
          entry['reachable'] = True
        except Jev.Error as error:
          entry['error'] = str(error)
      report[name] = entry
    report['selected'] = next((n for n in Jev.order if report[n]['key_present']), None)
    return report

  @staticmethod
  def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Ask Jev typed questions about a state.')
    sub = parser.add_subparsers(dest='command', required=True)
    ask = sub.add_parser('ask')
    ask.add_argument('--state', help='literal state string; omit to read stdin')
    ask.add_argument('--state-file')
    ask.add_argument('--questions', required=True, help='JSON string or path to a .json file')
    ask.add_argument('--provider', default='auto', choices=['auto', 'typesafe', 'openrouter', 'vercel'])
    ask.add_argument('--model', default='')
    ask.add_argument('--timeout', type=float, default=10.0)
    sub.add_parser('doctor')
    setKey = sub.add_parser('set-key')
    setKey.add_argument('--provider', required=True, choices=['typesafe', 'openrouter', 'vercel'])
    args = parser.parse_args(argv)

    if args.command == 'doctor':
      print(json.dumps(Jev.doctor(), indent=2))
      return 0

    if args.command == 'set-key':
      try:
        result = Jev.setKey(args.provider)
      except Jev.Error as error:
        print(json.dumps({'error': error.code, 'detail': str(error)}), file=sys.stderr)
        return 1
      print(json.dumps(result, indent=2))
      return 0 if result['status'] == 'stored' else 1

    if args.state_file:
      with open(args.state_file, encoding='utf-8') as handle:
        state = handle.read()
    elif args.state is not None:
      state = args.state
    else:
      state = sys.stdin.read()
    source = args.questions
    if os.path.exists(source):
      with open(source, encoding='utf-8') as handle:
        source = handle.read()
    try:
      questions = json.loads(source)
      result = Jev.ask(state, questions, provider=args.provider, model=args.model, timeout=args.timeout)
    except json.JSONDecodeError as error:
      print(json.dumps({'error': 'bad_questions_json', 'detail': str(error)}), file=sys.stderr)
      return 2
    except Jev.Error as error:
      print(json.dumps({'error': error.code, 'detail': str(error)}), file=sys.stderr)
      return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
  raise SystemExit(Jev.main())
