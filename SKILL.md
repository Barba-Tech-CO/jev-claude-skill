---
name: jev
description: Use when code needs a structured decision about some state rather than prose — route this to a model/team/queue, classify or triage these messages, rank or filter passages by relevance, score output against a rubric, verify a claim or guardrail as yes/no, or the same judgment repeated over many items in a loop or pipeline. Also use when the user mentions Jev, TypeSafe, System One, or the Decisions API. Calls TypeSafe's Jev through TypeSafe direct, Vercel AI Gateway, or OpenRouter, which answers with calibrated probabilities (noul, choice, score) and never text. Do not use for writing, explaining, summarizing, coding, or any answer a person reads.
license: MIT
---

# Jev — typed decisions, no prose

Jev is TypeSafe's "System One" model. It writes no text. You send one `state` plus named
typed questions; it answers all of them in one request (~0.4–1 s, ~USD 0.042 per 1M input tokens,
output free) with calibrated probabilities your code branches on.

Your code owns the workflow; Jev only answers the questions you hand it.

## When to reach for it

Fire on any of these, whether or not Jev is named:

- **Routing** — which model, team, queue, agent, or skill this input belongs to.
- **Classification and triage** — kind, priority, urgency, sentiment, "must a human see this".
- **Ranking and filtering** — which retrieved passages, search hits, or candidates are worth
  reading, and which carry injected instructions.
- **Verification and guardrails** — does this output satisfy the rule, is this request
  destructive, did the answer actually use the source.
- **Grading** — position on a rubric you wrote, for evaluation sets or self-checks.
- **Batch judgment** — the same small question asked over a list, a loop, or a pipeline stage,
  where one LLM call per item would be slow and expensive.

The shape that gives it away: the answer is consumed by an `if`, a sort, or a filter — not read
by a person.

**Do not fire** for writing, explaining, summarizing, translating, generating code, open-ended
reasoning, or anything needing an answer longer than a label. Jev returns no text. One-off
judgments you can make yourself in the turn do not need a network call either — reach for it
when the decision repeats, must be calibrated, or must be the same every time.

## Question types

| type | you supply | you get back |
|---|---|---|
| `boolean` | `instructions` | `probability` 0..1 that the statement is true |
| `choice` | `instructions`, `criteria` = `{option: description}` (2–255) | `choice`, `probabilities`, `confidence` |
| `score` | `instructions`, `criteria` = `[lowest, …, highest]` (2–10) | `score` (fractional), `label`, `probabilities`, `confidence` |

`boolean` and `noul` are the same question. Write `boolean`; the script translates per provider.

## Run it

```bash
python3 scripts/jev.py ask --state "payment retries charge us twice, fix it TODAY" --questions '{
  "urgent": {"type": "boolean", "instructions": "Does this express urgency?"},
  "team": {"type": "choice", "instructions": "Which team owns this?",
           "criteria": {"billing": "charges and refunds", "technical": "bugs and integrations"}},
  "anger": {"type": "score", "instructions": "How frustrated is the customer?",
            "criteria": ["Calm", "Concerned", "Furious"]}
}'
```

`--state-file <path>` reads the state from a file, and with neither flag the state is read from
stdin. `--questions` also takes a path to a `.json` file. Output is one JSON object:
`{provider, model, latency_ms, usage, answers}`.

Store a key once, without putting it in a command line or a log — hidden prompt, one
verification call, then `~/.config/jev/credentials` at mode 0600 (`JEV_CREDENTIALS` moves it):

```bash
python3 scripts/jev.py set-key --provider openrouter
```

The environment variable wins over the stored file when both exist.

Check credentials and connectivity: `python3 scripts/jev.py doctor` (it spends one ~USD 0.00002
call per configured provider).

Measure a question set before trusting it — replay hand-labelled cases and read the
calibration, not just the accuracy:

```bash
python3 scripts/jev.py eval --questions route.json --cases cases.json
```

A case is `{"state": …, "expect": {"<question>": <answer>}}` — an option key for a `choice`,
a level index or label for a `score`, `true`/`false` for a `boolean`. The report gives overall
and per-question accuracy, the misses, and accuracy split by confidence band. High confidence
that is no more accurate than low confidence means the question is badly written, not that the
model is wrong. Twenty cases cost about USD 0.0004.

Offline tests, no key needed: `python3 -m unittest discover -s tests`.

## Providers

Resolved automatically in this order, by which key is in the environment. Force one with
`--provider typesafe|vercel|openrouter`.

| provider | key | endpoint | default model |
|---|---|---|---|
| `typesafe` | `TYPESAFE_API_KEY` | `POST https://api.typesafe.ai/v1/systemone` | `jev-latest` |
| `vercel` | `AI_GATEWAY_API_KEY` or `VERCEL_AI_GATEWAY_API_KEY` | `POST https://ai-gateway.vercel.sh/v4/ai/evaluation-model` | `typesafe-ai/jev` |
| `openrouter` | `OPENROUTER_API_KEY` | `POST https://openrouter.ai/api/alpha/decisions` | `typesafe/jev-1.13` |

On OpenRouter `typesafe/jev-latest` does not resolve — use a dated id. Override the model with `--model` or `JEV_MODEL` (ignored on Vercel, which pins the model in a
header). Keys: TypeSafe at https://console.typesafe.ai/settings/keys, OpenRouter at
https://openrouter.ai/keys, Vercel in the AI Gateway dashboard.

Jev is **not** a chat model on any provider. `chat/completions` rejects it.

In TypeScript, Vercel's own path is `experimental_evaluate({model: 'typesafe-ai/jev', state,
questions})` from the `ai` package — same question schema, so this script's JSON ports over.

## Rules

**Never send secrets.** The state leaves the machine. Strip keys, tokens, credentials, and
personal data before the call. Send the turn, not the whole transcript; send a passage, not the
file path or store id.

**Fail open, honestly.** `no_key`, `network`, `rate_limited`, `timeout`, `malformed` — every
failure exits non-zero with a JSON error on stderr and no answer. Fall back to a rule you can
state out loud (keep the current model, keep every passage, take the cheapest option) and say
a Jev call failed. Never fabricate an answer or a confidence to keep the pipeline flowing: a
made-up classification is worse than an admitted gap, because everything downstream trusts it.

**Spend the call only on judgment.** Anything pure code can settle — a hard constraint, a
required capability, a budget ceiling, an exact-match rule — is settled before the request, not
asked. Filter first, then ask Jev about what is left, and never ask a question whose answer you
would override anyway.

**Confidence is data, not truth.** A `score` that averages to the middle of the rubric can mean
"medium" or "no idea" — read `probabilities`, not just the number. Below ~0.6 confidence on a
`choice`, treat it as no answer and take a fixed, stated default (the safe end, the cheap end,
the current behavior) rather than the winning option by a hair.

**Bound the blast radius.** Only let Jev pick from options you already judged safe. A destructive
or irreversible branch (delete, deploy, payment, migration) is never decided by a probability —
confirm with the user.

## Recipes

- **Routing**: do not hand Jev the candidate list. Ask about the *request* — domain, how much
  reasoning it needs, does it need long context or vision, is it latency-sensitive — and let
  plain code rank the live candidates against that profile. The option set then costs nothing
  when it changes, stays under the 255-option ceiling, and the same profile drives price,
  capability and safety rules at once. Shadow-mode it first: log the pick, keep the current
  behavior, and compare.
- **Memory filter**: one request, one `boolean` per passage (`"Does this passage help answer the
  query?"`), batched ~60 at a time with passages renamed `P0`, `P1`… Drop what scores low.
- **Triage**: `choice` for kind, `score` for urgency, `boolean` for "must a human see this".
- **Grading**: `score` against an explicit rubric you wrote; pair with a `boolean` for hard
  rule breaks.

- **Evaluation**: hand-label 20+ cases, run `jev.py eval`, and rewrite any question whose high-confidence band is no more accurate than its low-confidence one.
