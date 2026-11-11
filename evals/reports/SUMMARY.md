# Test summary (final code, 2026-11-11)

Model under test: `openai/gpt-5.6-luna` · judge: `openai/gpt-5.6-terra` · simulated callers: `gpt-5.6-luna`

| Suite | What it covers | Result |
|---|---|---|
| `pytest tests` | 36 deterministic gate tests (fake LLM): verification rules, leak prevention, memory, consent, scope, email flow | **36/36** |
| Dataset A (offline) | Identity matrix: 4 policyholders × all field subsets × format variants, corruptions, cross-record mixes | **1013/1013** |
| Dataset B (offline) | Fixture integrity: document mapping, templates, representatives, consent | **OK** |
| Dataset C (live) | Every policyholder (3 field combinations each), every claim's status and amounts, all 6 guidance topics × 2 denied claims, both consent scenarios | **31/31** |
| Scripted (live + judge) | 34 requirement-pinned edge cases | **33/34** in the final full run; the one failure was an over-strict keyword check (judge 5/5). It passes after the check was fixed, and the email-flow cases pass after the last fix |
| Generated (live + judge) | 40 simulated callers, unseen seed 23 | **38/40** in the full run → case 29 (a real bug: "done + send email" with a trailing question) is now fixed and passes. Case 02 is a simulator artefact: the caller hung up before the wrap-up |

Earlier runs on seeds 7 and 11 (used during development): 18/40 → 34/40 → 36/37, and 38/40 on seed 11 (both of those failures were fixed).

## Real bugs found by these tests and fixed
- An ambiguous follow-up ("the one from 2025") made the extractor pick the wrong claim. Its pick is now trusted only when it fits the caller's hints.
- When the caller rejected the proposed claim, their new hint was ignored. Stale hints are now cleared and the new hint is used.
- An email the caller requested mid-conversation was never sent, or was refused. Email preferences are now remembered in any phase.
- "Send it to my work email" sent the summary to the email on file without asking. Now only the email on file is allowed, and the agent asks again.
- A verified caller who was transferred to a human skipped the email summary. POST_PROCESS now runs before the handoff.
- The small model occasionally produced malformed JSON, so explicit "transfer me" requests were lost. Fixed with strict JSON-schema structured outputs and a safe fallback.
- The model sometimes dropped a mandated offer of a human (after off-topic strikes). Code now enforces required content.
- Partial identity answers were counted as wrong answers. They now get a clarifying question.
- The agent speculated: late appeals "may be possible", denials "not about exclusions", a new claim "may not be in the system yet". The prompts now forbid speculation.
- CL-3001's "diagnosis report" has no guidance entry in the fixtures. It now falls back to the general guidance.
