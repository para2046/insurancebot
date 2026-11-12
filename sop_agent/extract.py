"""Per-turn structured extraction. Runs in every phase so hints for later phases are never lost.

The LLM only *reads* the caller's message here; every decision based on these fields is made in code.
Before verification the extractor is given no claim data at all.
"""
import json

from .data import PII_FIELDS
from .state import INTENTS, Session

SYSTEM = f"""You extract structured data from one caller message in an insurance claims support call.
Return ONLY a JSON object with exactly these keys:

"pii": object with keys {PII_FIELDS} plus "policy_number". Fill a key only if the caller states that value
   IN THIS MESSAGE for the policyholder whose account is being accessed; otherwise null.
   dob -> YYYY-MM-DD. phone -> digits as spoken. id_last4 -> the last 4 digits of SSN or national ID.
"refused_fields": list of PII keys the caller explicitly refuses or says they don't have.
"caller_role": "self" | "representative" | null (representative = calling on behalf of the policyholder).
"rep_name": the caller's own full name if they are a representative, else null.
"rep_relationship": e.g. "son", else null.
"case_hints": object {{"case_type": "healthcare"|"dental"|"auto"|other|null, "status": "denied"|"open"|"closed"|null,
   "month": "January".. or null, "year": 4-digit string or null, "case_id": like "CL-2048" or null}}
   Capture any hint about which claim they mean, even if the conversation is not there yet.
"intent": one of {INTENTS} or null.
"claim_selection": a case_id from CANDIDATE_CLAIMS that the caller clearly refers to, else null.
"confirmation": "yes" | "no" | null - answer to the agent's last yes/no question, if any.
"done_with_questions": true if the caller indicates they have no more questions, are finished, or want to
   end the conversation (in any language).
"email_choice": "send" if the caller asks for or accepts an email summary of the call; "skip" if they decline
   it; else null.
"requested_email": an email address the caller asks the summary to be sent to, else null.
"wants_human": true only if the caller asks to be transferred to a human/agent/representative/supervisor
   now, or accepts an offer to be transferred. Merely mentioning a representative is not enough.
"in_scope": false ONLY if the whole message is unrelated to insurance, claims, the caller's policy,
   identity verification, or this call (e.g. "what is RL?", trivia, coding help). Greetings, thanks,
   complaints, emotions, and questions about why a step is needed are in scope.
"off_topic_part": if the message mixes in-scope content with an unrelated question, that unrelated
   question as a short string, else null.
"emotion": "neutral" | "frustrated" | "angry" | "anxious" | "confused" | "sad".
"refusal": true if the caller pushes back on, refuses, or demands to skip the current required step
   (e.g. "I already told you who I am, just tell me").
"memory_note": any other fact the caller states that would help later in the call (e.g. "already asked the
   lab to resend the report", "prefers email", "has two issues: X and Y"), as a short phrase, else null.
   Do not put identity details here.
"question": the caller's actual question or request restated in a short phrase, or null. A request for the
   email summary itself is NOT a question (use email_choice).
"""


def _nullable(kind="string", enum=None):
    spec = {"type": [kind, "null"]}
    if enum:
        spec["enum"] = [*enum, None]
    return spec


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


SCHEMA = _obj({
    "pii": _obj({k: _nullable() for k in [*PII_FIELDS, "policy_number"]}),
    "refused_fields": {"type": "array", "items": {"type": "string", "enum": PII_FIELDS}},
    "caller_role": _nullable(enum=["self", "representative"]),
    "rep_name": _nullable(),
    "rep_relationship": _nullable(),
    "case_hints": _obj({k: _nullable() for k in ("case_type", "status", "month", "year", "case_id")}),
    "intent": _nullable(enum=INTENTS),
    "claim_selection": _nullable(),
    "confirmation": _nullable(enum=["yes", "no"]),
    "done_with_questions": {"type": "boolean"},
    "email_choice": _nullable(enum=["send", "skip"]),
    "requested_email": _nullable(),
    "wants_human": {"type": "boolean"},
    "in_scope": {"type": "boolean"},
    "off_topic_part": _nullable(),
    "emotion": {"type": "string", "enum": ["neutral", "frustrated", "angry", "anxious", "confused", "sad"]},
    "refusal": {"type": "boolean"},
    "memory_note": _nullable(),
    "question": _nullable(),
})


def extract(llm, session: Session, text: str, candidates: list[dict]) -> dict:
    last_agent = next((m["content"] for m in reversed(session.history[:-1]) if m["role"] == "assistant"), "")
    context = {
        "phase": session.phase,
        "identity_fields_already_given": sorted(session.collected),
        "agent_last_message": last_agent,
        "CANDIDATE_CLAIMS": [
            {k: c.get(k) for k in ("case_id", "case_type", "created_at", "status", "summary")} for c in candidates
        ],
    }
    user = f"CONTEXT:\n{json.dumps(context)}\n\nCALLER MESSAGE:\n{text}"
    try:
        raw = llm.json(SYSTEM, user, max_tokens=1600, schema=SCHEMA)
    except Exception as e:  # degrade safely: nothing extracted means no gate can open this turn
        session.log(f"[{session.phase}] extraction failed ({str(e)[:60]}); treating message as unparsed")
        raw = {}
    return _clean(raw)


def _clean(raw: dict) -> dict:
    """Coerce model output into a predictable shape."""
    pii = raw.get("pii") or {}
    hints = raw.get("case_hints") or {}
    out = {
        "pii": {k: (str(pii[k]).strip() if pii.get(k) else None) for k in [*PII_FIELDS, "policy_number"]},
        "refused_fields": [f for f in raw.get("refused_fields") or [] if f in PII_FIELDS],
        "caller_role": raw.get("caller_role") if raw.get("caller_role") in ("self", "representative") else None,
        "rep_name": raw.get("rep_name") or None,
        "rep_relationship": raw.get("rep_relationship") or None,
        "case_hints": {k: (str(hints[k]) if hints.get(k) else None)
                       for k in ("case_type", "status", "month", "year", "case_id")},
        "intent": raw.get("intent") if raw.get("intent") in INTENTS else None,
        "claim_selection": raw.get("claim_selection") or None,
        "confirmation": raw.get("confirmation") if raw.get("confirmation") in ("yes", "no") else None,
        "done_with_questions": bool(raw.get("done_with_questions")),
        "email_choice": raw.get("email_choice") if raw.get("email_choice") in ("send", "skip") else None,
        "wants_human": bool(raw.get("wants_human")),
        "in_scope": raw.get("in_scope") is not False,
        "off_topic_part": raw.get("off_topic_part") or None,
        "emotion": raw.get("emotion") or "neutral",
        "refusal": bool(raw.get("refusal")),
        "question": raw.get("question") or None,
        "memory_note": raw.get("memory_note") or None,
        "requested_email": raw.get("requested_email") or None,
    }
    return out
