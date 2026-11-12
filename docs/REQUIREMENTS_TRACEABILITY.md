# Requirements traceability

Each requirement from the assignment, its design, and the tests that validate it.

**Test ID legend**
- `U:` unit test in `tests/test_gates.py` (fake LLM, deterministic, free)
- `S:` scripted live case in `evals/scripted.py` (real model + LLM judge)
- `D:` dataset validation in `evals/dataset_validation.py` (A = offline identity matrix, B = fixture integrity, C = live)
- `G:` generated simulated-caller category in `evals/generated.py`
- `W:` dataset conversations against the deployed website (`evals/run_on_website.py`)

Latest results: U 44/44 · S 39/39 (judge averages: SOP adherence 4.97, grounding 5.0, naturalness 4.9, empathy 4.49, memory 4.97) · D-A 1013/1013 · D-B OK · D-C 31/31 · G 38/40 on an unseen seed (both failures analysed; the real bug is fixed) · W 31/31 (6 runs).

---

## Core workflow

| # | Requirement | Design | Validated by | Status |
|---|---|---|---|---|
| R1 | Fixed workflow VERIFY_ID → RESOLVE_INTENT → PROCESS_CASE → POST_PROCESS | `Session.phase` is changed only by code in `agent.py`. The LLM never sets the phase. | U: `test_full_workflow_with_email`, `test_full_workflow_skip_email` · S: `demo_case_full_workflow` | ✅ |
| R2 | Different steps get different levels of freedom | VERIFY: matching in code, and the LLM only phrases the reply. RESOLVE: LLM extraction, but its claim choice is accepted only if it fits what the caller said. PROCESS: the LLM reasons over `ALLOWED_FACTS`. POST: a binary send/skip choice decided in code. | See R3–R7 | ✅ |
| R10 | The LLM interprets and phrases; the SOP controls order, gates and allowed actions | Each turn runs extract (LLM, strict schema) → control (code) → respond (LLM, directives + allowed facts only) → enforce (code) → guard (code). | U: `test_no_claim_data_reaches_model_before_verification`, `test_required_human_offer_is_enforced_even_if_model_omits_it` · S: `prompt_injection` | ✅ |

## VERIFY_ID (strict)

| # | Requirement | Design | Validated by | Status |
|---|---|---|---|---|
| R3a | Don't advance until verified with ≥3 PII (name, DOB, phone, email, SSN last 4) | `Store.best_match()`: at least 3 fields matching **one** record. The policy number doesn't count. | D-A: 1013/1013 (297 positive, 236 below threshold, 120 corrupted, 360 cross-record) · U: `test_two_fields_are_not_enough_and_policy_number_does_not_count`, `test_fields_from_different_records_do_not_combine` · S: `two_fields_not_enough`, `policy_number_does_not_count`, `mixed_records_rejected` | ✅ |
| R3b | Never disclose claim details before verification | Claim data is never put in the LLM context before verification, and an output guard blocks case IDs and amounts. | U: `test_no_claim_data_reaches_model_before_verification` · automatic `no_leak_before_verification` check on **every** live conversation (S, D-C, G, W) · S: `prompt_injection`, `bonus_frustration_example` | ✅ |
| R4a | Natural conversation and clarification questions | The question is answered (why a detail is needed, what "on file" means), then verification continues. | S: `clarification_question_during_verify` | ✅ (added in this review) |
| R4b | Partial answers | Incomplete values (first name only, "March 1985", 2 digits, malformed phone) get a format clarification and don't count as failures. A full ID number triggers a reminder that only the last 4 digits are needed. | U: `test_partial_answers_are_clarified_not_counted_as_failures`, `test_malformed_phone_is_flagged_even_when_other_details_fail`, `test_full_id_number_gets_privacy_reminder` · S: `partial_answers_clarified` | ✅ |
| R4c | Refusals | A refused field is respected and the agent offers the others. If every field is refused, it offers a human. | U: `test_refused_field_can_still_be_given_later`, `test_all_fields_refused_offers_human` · S: `alternate_fields_refuse_ssn`, `persistent_refusal_escalates` · G: `refuse` | ✅ |
| R4d | Alternate identity fields | Any 3 of the 5 fields. Also handles national ID instead of SSN, name aliases and order ("Yaven Li"), email aliases, and phone/DOB formats. | D-A (format variants) · U: `test_alternate_fields_across_turns`, `test_normalizers` · S: `national_id_instead_of_ssn`, `name_alias_and_order` | ✅ |
| R3c | Failed verification is handled safely | The agent never says which field failed. After 3 failures it hands off to a human. | U: `test_lockout_after_repeated_failures` · S: `wrong_dob_does_not_say_which`, `lockout_after_three_failures` · G: `impostor`, `wrong_then_right` | ✅ |

## RESOLVE_INTENT and PROCESS_CASE (flexible)

| # | Requirement | Design | Validated by | Status |
|---|---|---|---|---|
| R5a | Interpret messy user language | LLM extraction into a strict schema (hints, intent, selection, yes/no). | S: `messy_language_multi_hint` · G: 7 speaking styles (terse/typos, rambling, non-native, confused…) | ✅ |
| R5b | Resolve ambiguity | Hints are scored against the caller's claims. A unique match is proposed for confirmation; otherwise the closest claims are listed. A rejected candidate clears stale hints. | U: `test_inconsistent_selection_is_not_trusted`, `test_rejection_with_new_hint_selects_directly` · S: `ambiguous_hint_lists_choices`, `caller_rejects_candidate` | ✅ |
| R5c | Answer grounded follow-up questions | `claim_facts()` provides the claim record, field meanings, per-document requirements and alternatives, follow-up templates filled in for the claim, and the deadline status. Anything outside that gets "I don't have that" plus a human offer. | D-C: all 6 guidance topics × 2 denied claims + status/amounts for all 5 claims · automatic `grounded_numbers` check on every live conversation · S: `document_alternatives`, `unknown_question_falls_back`, `deadline_passed_honesty`, `confused_caller` | ✅ |
| R5d | Choose the bounded workflow path | Intent comes from a fixed set (the guideline's `intent_hints`). Out-of-workflow requests (new claim, address change) are routed to a human. | S: `out_of_workflow_request` · D-C guidance topics | ✅ |
| R5e | Switch claims, and never mix them up | A switch needs evidence in the caller's own words. The overview is built in code from the claim's real status. | U: `test_no_switch_to_claim_the_caller_did_not_describe`, `test_switch_when_caller_describes_other_claim` · S: `switch_claim_midway` · W (found and fixed a CL-1899/CL-2048 mix-up) | ✅ |

## POST_PROCESS

| # | Requirement | Design | Validated by | Status |
|---|---|---|---|---|
| R6a | Offer an email summary covering what was discussed, claim status/outcome, and follow-ups | The email is built in code: an LLM recap of the conversation, claim status and reason, next steps (documents, timing, deadline). It's shown in the UI's Email outbox (sending is mocked). | U: `test_full_workflow_with_email` (asserts the body content) · S: `demo_case_full_workflow` | ✅ |
| R6b | The customer chooses to send or skip | Send/skip is decided in code. A preference stated earlier ("just email it") is remembered. It's only ever sent to the email on file. | U: `test_full_workflow_skip_email`, `test_email_only_to_address_on_file`, `test_early_email_decline_is_honored`, `test_email_requested_mid_process_is_sent_when_done` · S: `email_skip`, `email_other_address`, `question_after_email_offer` | ✅ |
| R6c | POST only after a claim was handled | Ending before a claim was handled closes the call without an email. A handoff after a handled case settles the email first. | U: `test_caller_can_end_call_before_verification`, `test_handoff_after_case_offers_email_first` · S: `end_call_during_verification` | ✅ |

## Scope

| # | Requirement | Design | Validated by | Status |
|---|---|---|---|---|
| R7a | Politely reject out-of-scope questions ("what is RL?") | Extraction flags `in_scope` / `off_topic_part`. The agent declines with no partial answer and steers back. Questions about insurance terms are in scope. | S: `off_topic_three_strikes`, `mixed_in_and_out_of_scope`, `off_topic_after_verification`, `confused_caller` (a term question is **not** rejected) · G: `off_topic` | ✅ |
| R7b | Offer a human after repeated irrelevant questions | A strike counter (partly off-topic messages count too). At 3 strikes, code enforces a human offer. | U: `test_off_topic_strikes_offer_human_and_reset`, `test_mixed_off_topic_counts_as_strike`, `test_required_human_offer_is_enforced_even_if_model_omits_it` | ✅ |

## Memory

| # | Requirement | Design | Validated by | Status |
|---|---|---|---|---|
| R8a | Remember useful info whenever it's said, even if it belongs to a later phase; stay in VERIFY_ID | Extraction runs in every phase. `Session.memory` holds claim hints, intent, email preference and free-text notes. The phase doesn't change. | U: `test_no_claim_data_reaches_model_before_verification` (hint stored, still VERIFY_ID), `test_free_form_notes_are_remembered_and_used_later`, `test_early_email_decline_is_honored` · S: `claim_question_before_verify_is_remembered` | ✅ |
| R8b | Use the remembered hint after verification instead of asking from scratch | `_resolve()` scores memory against the claims and proposes the match. | U: `test_demo_case_verifies_and_uses_remembered_hint` · S: `demo_case_full_workflow`, `claim_question_before_verify_is_remembered` | ✅ |
| R8c | Memory across separate conversations (caller returns another day) | **Not implemented, by design.** Memory lasts for one conversation (per browser session). The assignment's memory requirement is about phases within a call. Cross-call memory would need a database keyed by the verified `party_id`, recalled only after re-verification, with a retention policy. | — | ⚪ Out of scope (can be added) |

## Official demo test case

| # | Expected behavior | Validated by | Status |
|---|---|---|---|
| T1 | Extracts identity info and verifies the caller | U: `test_demo_case_verifies_and_uses_remembered_hint` · S: `demo_case_full_workflow` · W | ✅ |
| T2 | Doesn't disclose claim details before verification | Automatic leak check | ✅ |
| T3 | Remembers the denied-healthcare-January hint | U + S as above (picks CL-2048, not the closed January-2025 CL-2011) | ✅ |
| T4 | Uses the hint to resolve the case instead of asking from scratch | S: `demo_case_full_workflow` (`must_say` CL-2048 on reply 1) | ✅ |
| T5 | PROCESS_CASE answers naturally, only from claim/tool data | S judge (grounding score) + `grounded_numbers` check | ✅ |

## Bonus: emotional support and SOP recovery

| # | Requirement | Design | Validated by | Status |
|---|---|---|---|---|
| B1 | Recognize frustration, anxiety, anger, confusion, refusal | The `emotion` and `refusal` extraction fields. | S: `bonus_frustration_example`, `anxious_caller`, `angry_after_verification`, `confused_caller`, `persistent_refusal_escalates` · G: `angry_comply`, styles | ✅ |
| B2 | Empathy and de-escalation before pushing the workflow | An empathy-first directive for negative emotions. For outcome anger, facts plus a path forward, without being defensive. | S judge `empathy` score: 4.49/5 average over the 39 scripted cases | ✅ |
| B3 | Explain why the SOP steps matter (verification, consent) | The verification explanation (privacy, fraud protection), and why consent matters for representatives. | S: `bonus_frustration_example`, `representative_consent_approved/timeout` | ✅ |
| B4 | Persuade without bypassing gates | Emotion never changes a gate. Gates are code-only. | U: `test_persuasion_limit_offers_human` · S: `persistent_refusal_escalates`, `prompt_injection` | ✅ |
| B5 | Offer alternatives (other ID fields, human transfer) | Lists the remaining options. From the 2nd pushback, a human is offered as an alternative. | U: `test_all_fields_refused_offers_human` · S: `alternate_fields_refuse_ssn` | ✅ |
| B6 | Know when to stop persuading and escalate | After 3 pushbacks or 3 angry turns in a row, offer a human. After 3 failed verifications, hand off. | U: `test_persuasion_limit_offers_human`, `test_sustained_anger_offers_human_after_verification` · S: `persistent_refusal_escalates` | ✅ |
| B7 | Bonus example ("I already told you who I am…") | — | S: `bonus_frustration_example` | ✅ |

## Deliverables

| # | Requirement | Design | Validated by | Status |
|---|---|---|---|---|
| D1 | Hosted demo URL, or Docker image/repo with setup instructions | https://insurancebot-w6cl.onrender.com · repo with Dockerfile, `render.yaml` and README | W: 31/31 against the live URL | ✅ |
| D2 | Setup accepts an API auth token | A 🔑 field at the top of the UI (each visitor's own key, never stored). `OPENROUTER_API_KEY` env var for self-hosting. | Live check: no key → "please enter your key"; with key → works | ✅ |
| D3 | Simple test UI with natural-language chat | Gradio chat plus a live SOP-state panel, email outbox and test identities | W, manual | ✅ |
| D4 | Demo shows the full workflow | README walkthrough (6 steps) + 9 extra scenarios | S: `demo_case_full_workflow` | ✅ |

## Beyond the written requirements (from the data and from review feedback)

| # | Behavior | Validated by |
|---|---|---|
| X1 | Representative callers need an authorized listing **and** policyholder consent (pending → approved / timeout) | U: `test_representative_consent_*`, `test_unlisted_representative_is_refused`, `test_rep_name_not_overwritten_by_shorter_form`, `test_rep_matching_rules` · S: 3 rep cases · D-C, W |
| X2 | Policyholders with no claims (P7, P13): say so and offer a human, never invent a claim | U: `test_policyholder_without_claims_is_told_so` · S: `no_claims_on_file` · D-C |
| X3 | Document-name mismatch between claims and guideline is bridged; CL-3001's missing guidance falls back to general guidance | D-B · D-C guidance cases |
| X4 | Appeal deadline compared with today, with no invented late-appeal options | U: `test_deadline_passed_is_flagged` · S: `deadline_passed_honesty` |
| X5 | Caller can end the call at any time, in any language | U: `test_caller_can_end_call_before_verification` · S: `end_call_during_verification`, `end_call_in_other_language` |
| X6 | Ask for a human at any time | U: `test_human_request_hands_off`, `test_handoff_*` · S: `asks_for_human` |
| X7 | Robust to malformed model output (strict JSON schema; a failed extraction opens no gates) | Live suites (no crashes since adopting the schema) |

## Known limitations
- Memory doesn't persist across separate conversations (R8c).
- Email sending and consent requests are simulated, as they are in the fixtures.
- The free Render instance sleeps when idle, so the first request takes about a minute to wake it.
- The live suites depend on the model and are nondeterministic. Intermittent issues found on the website were fixed and re-run several times.
