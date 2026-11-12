# Test summary (final code, 2026-11-11)

Model under test: `openai/gpt-5.6-luna` · judge: `openai/gpt-5.6-terra` · simulated callers: `gpt-5.6-luna`

| Suite | What it covers | Result |
|---|---|---|
| `pytest tests` | 40 deterministic gate tests (fake LLM): verification rules, leak prevention, memory, consent, scope, email flow | **40/40** |
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

## Deployed website (https://insurancebot-w6cl.onrender.com)

`evals/run_on_website.py` runs the 31 dataset conversations through the site's web API, the way a browser does
(fresh session, settings fields left empty).

| Run | Result | What happened |
|---|---|---|
| First deploy | every message failed | `app.py` called `.strip()` on the API-key field, which the browser sends as `None`. Tests had always passed `""`. Fixed. |
| 1st website run | 30/31 | A closed dental claim (CL-1899) was described with another claim's denial reason, then the agent switched claims because it was misled by its own reply. Fixed: status-specific overview built in code, no summaries of other claims, and switching needs the caller's own words. |
| 2nd website run | 30/31 | Authorized rep David Chen was rejected: a later turn overwrote his name with "David". Fixed: keep the most complete name; accept first-name matches (consent is still required). |
| 3rd and 4th website runs | **31/31**, **31/31** | — |

Also found: auto-deploy on push wasn't active, because the service wasn't created through Render's GitHub app. Deploys are triggered manually.
