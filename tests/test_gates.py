"""Deterministic tests of the SOP gates. A fake LLM returns scripted extractions, so no API calls are made.

The fake responder records every prompt it receives, which lets us assert that claim data never even
reaches the model before identity verification.
"""
import json

import pytest

from sop_agent.agent import Agent
from sop_agent.data import Store, name_matches, norm_dob, norm_phone
from sop_agent.state import CLOSED, HANDOFF, POST_PROCESS, PROCESS_CASE, RESOLVE_INTENT, VERIFY_ID, Session

BASE = {
    "pii": {}, "refused_fields": [], "caller_role": None, "rep_name": None, "rep_relationship": None,
    "case_hints": {}, "intent": None, "claim_selection": None, "confirmation": None,
    "done_with_questions": False, "email_choice": None, "wants_human": False, "in_scope": True,
    "off_topic_part": None, "emotion": "neutral", "refusal": False, "question": None,
}


class FakeLLM:
    def __init__(self, extractions):
        self.extractions = list(extractions)
        self.prompts = []

    def json(self, system, user, max_tokens=0, schema=None):
        if system.startswith("Summarize"):
            return {"discussed": ["You asked why your claim was denied."]}
        ex = json.loads(json.dumps(BASE))
        override = self.extractions.pop(0)
        for k, v in override.items():
            ex[k] = {**ex[k], **v} if isinstance(v, dict) else v
        return ex

    def text(self, system, messages, max_tokens=0):
        self.prompts.append(system)
        return "ok"


def run(extractions, session=None):
    llm = FakeLLM(extractions)
    agent, s = Agent(llm, Store()), session or Session()
    for _ in range(len(extractions)):
        agent.handle(s, "msg")
    return s, llm


MARGARET_OPENING = {
    "pii": {"full_name": "Margaret Chen", "dob": "1985-03-15", "id_last4": "4472", "policy_number": "POL-9921"},
    "case_hints": {"case_type": "healthcare", "status": "denied", "month": "January"},
    "intent": "denial_question",
}


# ---------- normalization ----------

def test_normalizers():
    assert norm_phone("(650) 521-2836") == norm_phone("+16505212836")
    assert norm_dob("March 15, 1985") == "1985-03-15"
    assert norm_dob("03/15/1985") == "1985-03-15"
    rec = Store().policyholders[3]
    assert name_matches("Yaven Li", rec) and name_matches("Yawen Li", rec) and name_matches("Li Ya Wen", rec)
    assert not name_matches("Ya Li", rec)


# ---------- VERIFY_ID ----------

def test_demo_case_verifies_and_uses_remembered_hint():
    s, llm = run([MARGARET_OPENING])
    assert s.verified and s.phase == RESOLVE_INTENT
    assert s.candidate_case_id == "CL-2048"          # denied healthcare Jan 2026, not closed CL-2011
    assert s.memory["intent"] == "denial_question"


def test_no_claim_data_reaches_model_before_verification():
    s, llm = run([
        {"pii": {"full_name": "Margaret Chen"}, "case_hints": {"case_type": "healthcare", "status": "denied"}},
        {"refusal": True, "emotion": "angry"},
    ])
    assert not s.verified and s.phase == VERIFY_ID
    assert s.memory["case_type"] == "healthcare"     # hint stored while still in VERIFY_ID
    for prompt in llm.prompts:
        assert "CL-2048" not in prompt and "pathology" not in prompt and "1450" not in prompt


def test_two_fields_are_not_enough_and_policy_number_does_not_count():
    s, _ = run([{"pii": {"full_name": "Margaret Chen", "dob": "1985-03-15", "policy_number": "POL-9921"}}])
    assert not s.verified


def test_fields_from_different_records_do_not_combine():
    # Margaret's name + DOB, Ava's SSN last 4
    s, _ = run([{"pii": {"full_name": "Margaret Chen", "dob": "1985-03-15", "id_last4": "9180"}}])
    assert not s.verified and s.verify_failures == 1


def test_alternate_fields_across_turns():
    s, _ = run([
        {"pii": {"full_name": "Ya Wen Li"}, "refused_fields": ["id_last4"]},
        {"pii": {"phone": "650-521-2830"}},
        {"pii": {"email": "yawen.li@example.com"}},   # email alias
    ])
    assert s.verified and s.party_id == "P13"


def test_lockout_after_repeated_failures():
    wrong = lambda d: {"pii": {"full_name": "Margaret Chen", "dob": d, "id_last4": "0000"}}
    s, _ = run([wrong("1985-01-01"), wrong("1985-01-02"), wrong("1985-01-03")])
    assert s.phase == HANDOFF and not s.verified


def test_persuasion_limit_offers_human():
    s, _ = run([{"refusal": True, "emotion": "frustrated"}] * 3)
    assert s.handoff_offered and s.phase == VERIFY_ID and not s.verified


# ---------- representative + consent ----------

REP = {"caller_role": "representative", "rep_name": "David Chen", "rep_relationship": "son",
       "pii": {"full_name": "Margaret Chen", "dob": "1985-03-15", "email": "margaret@email.com"}}


def test_representative_consent_approved():
    s, _ = run([REP, {}])
    assert s.consent_status == "approved" and s.verified


def test_representative_consent_timeout():
    s, _ = run([REP] + [{}] * 5, Session(consent_scenario="timeout"))
    assert s.consent_status == "timeout" and not s.verified and s.handoff_offered


def test_unlisted_representative_is_refused():
    s, _ = run([{**REP, "rep_name": "Kevin Smith"}])
    assert s.rep_authorized is False and not s.verified


# ---------- scope ----------

def test_off_topic_strikes_offer_human_and_reset():
    s, _ = run([{"in_scope": False}] * 2 + [{}])
    assert s.off_topic_strikes == 0
    s, _ = run([{"in_scope": False}] * 3)
    assert s.handoff_offered and s.phase == VERIFY_ID


def test_human_request_hands_off():
    s, _ = run([{"wants_human": True}])
    assert s.phase == HANDOFF


# ---------- full workflow ----------

def test_full_workflow_with_email():
    s, llm = run([
        MARGARET_OPENING,
        {"confirmation": "yes"},
        {"question": "how do I submit", "intent": "document_submission"},
        {"done_with_questions": True},
        {"email_choice": "send"},
    ])
    assert s.phase == CLOSED and s.email_choice == "send"
    email = s.outbox[0]
    assert email["to"] == "margaret@email.com" and "CL-2048" in email["body"] and "pathology report" in email["body"]
    # grounded doc guidance was resolved despite the name mismatch in the fixtures
    assert "specimen details" in llm.prompts[2]


def test_full_workflow_skip_email():
    s, _ = run([MARGARET_OPENING, {"confirmation": "yes"}, {"done_with_questions": True}, {"email_choice": "skip"}])
    assert s.phase == CLOSED and s.outbox == []


def test_rejecting_candidate_lists_claims():
    s, llm = run([MARGARET_OPENING, {"confirmation": "no"}, {"claim_selection": "CL-2102"}])
    assert s.active_case_id == "CL-2102" and s.phase == PROCESS_CASE


def test_followup_question_after_email_offer_returns_to_process():
    s, _ = run([MARGARET_OPENING, {"confirmation": "yes"}, {"done_with_questions": True},
                {"question": "how long does review take?"}])
    assert s.phase == PROCESS_CASE


def test_deadline_passed_is_flagged():
    from datetime import date
    store = Store()
    facts = store.claim_facts(store.claim("P9", "CL-2048"), date(2026, 11, 10))
    assert "passed" in facts["appeal_deadline_status"]
    facts = store.claim_facts(store.claim("P9", "CL-2048"), date(2026, 2, 1))
    assert "remain" in facts["appeal_deadline_status"]


def test_policyholder_without_claims_is_told_so():
    s, llm = run([{"pii": {"full_name": "Ava Lopez", "dob": "1990-08-21", "id_last4": "9180"}}])
    assert s.verified and s.handoff_offered and "don't see any claims" in llm.prompts[-1]


def test_partial_answers_are_clarified_not_counted_as_failures():
    s, llm = run([{"pii": {"full_name": "Margaret", "dob": "March 1985", "id_last4": "44"}}])
    assert s.collected == {} and s.verify_failures == 0
    assert "incomplete or unclear" in llm.prompts[-1]


def test_refused_field_can_still_be_given_later():
    s, _ = run([
        {"pii": {"full_name": "Margaret Chen"}, "refused_fields": ["id_last4"]},
        {"pii": {"dob": "1985-03-15", "id_last4": "4472"}},
    ])
    assert s.verified and "id_last4" not in s.refused_fields


def test_free_form_notes_are_remembered_and_used_later():
    s, llm = run([
        {**MARGARET_OPENING, "memory_note": "already asked the lab to resend the pathology report"},
        {"confirmation": "yes"},
    ])
    assert "already asked the lab" in llm.prompts[-1]


def test_sustained_anger_offers_human_after_verification():
    s, _ = run([MARGARET_OPENING, {"confirmation": "yes"}] + [{"emotion": "angry", "question": "why?"}] * 3)
    assert s.handoff_offered and s.phase == PROCESS_CASE


def test_inconsistent_selection_is_not_trusted():
    # "the one from 2025" after "healthcare January": extractor wrongly picks dental CL-1899
    s, _ = run([
        {"pii": MARGARET_OPENING["pii"], "case_hints": {"case_type": "healthcare", "month": "January"}},
        {"case_hints": {"year": "2025"}, "claim_selection": "CL-1899"},
    ])
    assert s.active_case_id is None and s.candidate_case_id == "CL-2011"


def test_rejection_with_new_hint_selects_directly():
    s, _ = run([MARGARET_OPENING,
                {"confirmation": "no", "case_hints": {"case_type": "dental"}, "claim_selection": "CL-1899"}])
    assert s.active_case_id == "CL-1899" and s.phase == PROCESS_CASE


def test_email_only_to_address_on_file():
    s, _ = run([MARGARET_OPENING, {"confirmation": "yes"}, {"done_with_questions": True},
                {"email_choice": "send", "requested_email": "margaret.chen@work.com"}])
    assert s.outbox == [] and s.phase == POST_PROCESS


def test_explicit_email_choice_when_done():
    s, _ = run([MARGARET_OPENING, {"confirmation": "yes"},
                {"done_with_questions": True, "email_choice": "send"}])
    assert s.phase == CLOSED and len(s.outbox) == 1


def test_required_human_offer_is_enforced_even_if_model_omits_it():
    llm = FakeLLM([{"in_scope": False}] * 3)
    agent, s = Agent(llm, Store()), Session()
    replies = [agent.handle(s, "what is RL?") for _ in range(3)]
    assert "human representative" in replies[-1] and "human" not in replies[0]


def test_email_requested_mid_process_is_sent_when_done():
    s, _ = run([MARGARET_OPENING, {"confirmation": "yes"},
                {"email_choice": "send", "question": "how long is review?"},
                {"done_with_questions": True}])
    assert s.phase == CLOSED and len(s.outbox) == 1


def test_mixed_off_topic_counts_as_strike():
    s, _ = run([{"off_topic_part": "recipe"}] * 3)
    assert s.off_topic_strikes == 3 and s.handoff_offered


def test_handoff_after_case_offers_email_first():
    s, _ = run([MARGARET_OPENING, {"confirmation": "yes"}, {"wants_human": True}, {"email_choice": "send"}])
    assert s.phase == HANDOFF and len(s.outbox) == 1


def test_handoff_with_requested_email_sends_immediately():
    s, _ = run([MARGARET_OPENING, {"confirmation": "yes"}, {"wants_human": True, "email_choice": "send"}])
    assert s.phase == HANDOFF and len(s.outbox) == 1


def test_handoff_before_verification_is_immediate():
    s, _ = run([{"wants_human": True}])
    assert s.phase == HANDOFF and s.outbox == []


def test_early_email_decline_is_honored():
    s, _ = run([{**MARGARET_OPENING, "email_choice": "skip"}, {"confirmation": "yes"}, {"done_with_questions": True}])
    assert s.phase == CLOSED and s.outbox == [] and s.email_choice == "skip"


def test_all_fields_refused_offers_human():
    s, _ = run([{"refused_fields": ["full_name", "dob", "phone", "email", "id_last4"]}])
    assert s.handoff_offered and not s.verified


def test_done_with_trailing_question_still_sends_email():
    s, llm = run([MARGARET_OPENING, {"confirmation": "yes"},
                  {"done_with_questions": True, "email_choice": "send", "question": "are PDFs accepted?"}])
    assert s.phase == CLOSED and len(s.outbox) == 1 and "briefly address" in llm.prompts[-1]
