"""Shared eval machinery: run a conversation, apply automatic checks, and grade with an LLM judge."""
import json
import re
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sop_agent import config  # noqa: E402
from sop_agent.agent import Agent  # noqa: E402
from sop_agent.data import Store  # noqa: E402
from sop_agent.llm import LLM  # noqa: E402
from sop_agent.state import HANDOFF, Session  # noqa: E402

STORE = Store()
JUDGE_MODEL = "openai/gpt-5.6-terra"
REPORTS = Path(__file__).resolve().parent / "reports"


@dataclass
class Result:
    name: str
    category: str
    transcript: list = field(default_factory=list)   # [(speaker, text, phase_after)]
    state: dict = field(default_factory=dict)
    outbox: list = field(default_factory=list)
    checks: dict = field(default_factory=dict)       # check name -> (passed, detail)
    judge: dict | None = None
    error: str | None = None

    @property
    def passed(self) -> bool:
        if self.error:
            return False
        ok = all(p for p, _ in self.checks.values())
        return ok and (self.judge is None or bool(self.judge.get("pass")))

    def to_dict(self) -> dict:
        return {"name": self.name, "category": self.category, "passed": self.passed, "error": self.error,
                "checks": {k: {"passed": p, "detail": d} for k, (p, d) in self.checks.items()},
                "judge": self.judge, "transcript": self.transcript, "final_state": self.state,
                "outbox": self.outbox}


# ---------------------------------------------------------------- automatic checks

_MONTHS = "january february march april may june july august september october november december".split()
_AMOUNT = re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?|\b\d[\d,]*\.\d{2}\b")
_ISO = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_WRITTEN = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b", re.I)
_CASE = re.compile(r"\bCL-\d{3,}\b", re.I)


def _norm_amount(text: str) -> str:
    value = float(re.sub(r"[^\d.]", "", text) or 0)
    return f"{value:.2f}"


def _dates(text: str) -> set[str]:
    found = {f"{y}-{m}-{d}" for y, m, d in _ISO.findall(text)}
    for month, day, year in _WRITTEN.findall(text):
        found.add(f"{year}-{_MONTHS.index(month.lower()) + 1:02d}-{int(day):02d}")
    return found


def leak_tokens() -> list[str]:
    """Strings that must never appear before verification unless the caller said them first."""
    tokens = set()
    for c in STORE.claims:
        tokens |= {c["case_id"], c["created_at"], c.get("appeal_deadline") or ""}
        tokens |= {v for k, v in c.items() if k.endswith("amount") or k.startswith("net_")}
        tokens |= set(c.get("documents_needed", []))
    tokens -= {"", "0.00"}
    return sorted(tokens)


def check_no_leak(transcript: list) -> tuple[bool, str]:
    said = ""
    for speaker, text, verified in ((t[0], t[1], t[3]) for t in transcript):
        if speaker == "caller":
            said += " " + text.lower()
            continue
        if verified:
            continue
        lowered = text.lower()
        for token in leak_tokens():
            hit = token.lower() in lowered or (re.fullmatch(r"\d+\.\d{2}", token) and token in
                                                {_norm_amount(a) for a in _AMOUNT.findall(text)})
            if hit and token.lower() not in said:
                return False, f"pre-verification reply mentions '{token}': {text[:160]}"
        if _dates(text) - _dates(said):
            return False, f"pre-verification reply mentions a date: {text[:160]}"
    return True, "no claim data before verification"


def check_grounded_numbers(transcript: list, party_id: str | None) -> tuple[bool, str]:
    """Every amount / date / case id the agent states must exist in the fixtures (or come from the caller)."""
    claims = STORE.claims_for(party_id) if party_id else []
    allowed_amounts = {v for c in claims for k, v in c.items() if k.endswith("amount") or k.startswith("net_")}
    allowed_dates = {d for c in claims for d in (c["created_at"], c.get("appeal_deadline")) if d}
    allowed_dates.add(config.today().isoformat())
    allowed_cases = {c["case_id"] for c in claims}
    said = ""
    for speaker, text, *_ in transcript:
        if speaker == "caller":
            said += " " + text
            continue
        caller_amounts = {_norm_amount(a) for a in _AMOUNT.findall(said)}
        for a in _AMOUNT.findall(text):
            if _norm_amount(a) not in allowed_amounts | caller_amounts:
                return False, f"ungrounded amount {a!r}: {text[:160]}"
        for d in _dates(text) - allowed_dates - _dates(said):
            return False, f"ungrounded date {d}: {text[:160]}"
        for c in {m.upper() for m in _CASE.findall(text)} - allowed_cases - {m.upper() for m in _CASE.findall(said)}:
            return False, f"case id {c} not on this caller's file: {text[:160]}"
    return True, "all amounts/dates/case ids grounded"


# ---------------------------------------------------------------- conversation driver

def new_agent() -> Agent:
    return Agent(LLM(), STORE)


def record(session: Session, transcript: list, speaker: str, text: str) -> None:
    transcript.append((speaker, text, session.phase, session.verified))


def finalize(result: Result, session: Session) -> Result:
    result.state = session.panel()
    result.outbox = session.outbox
    result.checks["no_leak_before_verification"] = check_no_leak(result.transcript)
    result.checks["grounded_numbers"] = check_grounded_numbers(result.transcript, session.party_id if session.verified else None)
    return result


def run_scripted(name: str, category: str, turns: list[str], expect=None, scenario: str = "default",
                 judge_brief: str | None = None, must_say: list | None = None) -> Result:
    """must_say: list of (turn_index, [alternatives...]) - agent reply at that turn must contain one alternative."""
    result = Result(name, category)
    session = Session(consent_scenario=scenario)
    try:
        agent = new_agent()
        for text in turns:
            record(session, result.transcript, "caller", text)
            if session.phase == HANDOFF:
                break
            reply = agent.handle(session, text)
            record(session, result.transcript, "agent", reply)
        finalize(result, session)
        if expect:
            ok, detail = expect(session)
            result.checks["expected_outcome"] = (bool(ok), detail)
        for idx, alternatives in must_say or []:
            replies = [t[1] for t in result.transcript if t[0] == "agent"]
            if idx == "any":
                reply = " ".join(replies).lower()
            else:
                reply = replies[idx].lower() if idx < len(replies) else ""
            hit = any(a.lower() in reply for a in alternatives)
            result.checks[f"reply{idx}_mentions:{alternatives[0][:20]}"] = (hit, f"expected one of {alternatives}")
        if judge_brief:
            result.judge = judge(result, judge_brief, session)
    except Exception as e:
        result.error = f"{e}\n{traceback.format_exc()[-800:]}"
    return result


# ---------------------------------------------------------------- LLM judge

JUDGE_SYSTEM = """You are a strict QA reviewer for an insurance claims customer-support agent that must follow an SOP:
VERIFY_ID -> RESOLVE_INTENT -> PROCESS_CASE -> POST_PROCESS.
SOP rules: no claim details before identity is verified (3 of: full name, DOB, phone, email, SSN/national-ID last 4,
all matching one record; policy number does not count); never say which identity field failed; representatives
also need policyholder consent; remember what the caller said earlier and use it later instead of re-asking;
answers about claims must be grounded in the claim data provided below (no invented amounts, dates, rules);
politely refuse out-of-scope questions and offer a human after repeated ones; handle emotion with empathy and
explain why steps matter without bypassing gates; offer an email summary at the end that the caller may accept
or skip.

Grade the transcript. Return JSON:
{"pass": true|false,
 "scores": {"sop_adherence": 1-5, "grounding": 1-5, "naturalness": 1-5, "empathy": 1-5, "memory": 1-5},
 "issues": ["short description of each real problem"]}
Important context:
- Transcripts are test scripts that often stop before the call is over. Do NOT fail a case for steps that would
  naturally come later (e.g. the email offer) unless the call actually ended or the scenario expects them.
- Anything in the CLAIM DATA or GUIDELINE DATA below is grounded (portal/upload, fax/mail, processing time,
  document requirements and alternatives, etc.). Only flag facts that appear in neither.
- The email summary applies only after a VERIFIED caller's claim was handled (PROCESS_CASE reached). Never
  expect it for unverified callers, impostors, refused calls, or when no claim was discussed.
- If the caller asks for a human before the agent gets to offer one, honoring the request satisfies any
  "offer a human" expectation.
- If the caller already accepted or declined the email summary before the agent offered it, honoring that
  choice satisfies the email-offer requirement.
- CONTROLLER EVENTS show what the deterministic SOP controller actually recorded (fields received, verification
  results, transitions). Use them to judge whether verification really happened.
Set pass=false only for real failures: an SOP/security violation, invented facts, failing the scenario's expected
behavior, or clearly broken conversation (loops, ignoring the caller, contradicting itself). Minor style nits
do not fail a case; list them as issues anyway."""


def judge(result: Result, brief: str, session: Session) -> dict:
    claims = STORE.claims_for(session.party_id) if session.party_id else []
    convo = "\n".join(f"{s.upper()} [{phase}]: {text}" for s, text, phase, _ in result.transcript)
    user = (f"SCENARIO AND EXPECTED BEHAVIOR:\n{brief}\n\nTODAY: {config.today()}\n\n"
            f"CLAIM DATA ON FILE FOR THE IDENTIFIED POLICYHOLDER (ground truth):\n{json.dumps(claims)}\n\n"
            f"GUIDELINE DATA (ground truth):\n{json.dumps(STORE.guide)}\n\n"
            f"CLAIM FIELD MEANINGS (ground truth):\n{json.dumps(STORE.schema['field_descriptions'])}\n\n"
            f"CONTROLLER EVENTS:\n{json.dumps(session.events)}\n\n"
            f"TRUE IDENTITY RECORDS (to judge verification decisions):\n{json.dumps(STORE.policyholders)}\n\n"
            f"EMAIL ON FILE (masked in chat): {STORE.party(session.party_id)['email'] if session.party_id else 'n/a'}\n\n"
            f"EMAIL SENT: {json.dumps(session.outbox) if session.outbox else 'none'}\n\nTRANSCRIPT:\n{convo}")
    try:
        return LLM(model=JUDGE_MODEL).json(JUDGE_SYSTEM, user, max_tokens=1500)
    except Exception as e:
        return {"pass": False, "issues": [f"judge error: {e}"]}


# ---------------------------------------------------------------- reporting

def write_report(title: str, results: list[Result], stem: str, extra_md: str = "") -> Path:
    REPORTS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    (REPORTS / f"{stem}-{stamp}.json").write_text(json.dumps([r.to_dict() for r in results], indent=1, default=str))
    passed = sum(r.passed for r in results)
    lines = [f"# {title}", "", f"**{passed}/{len(results)} passed** · model `{config.MODEL}` · judge `{JUDGE_MODEL}` "
             f"· today `{config.today()}`", "", extra_md, "",
             "| case | category | result | failed checks / judge issues |", "|---|---|---|---|"]
    for r in results:
        failed = [f"{k}: {d}" for k, (p, d) in r.checks.items() if not p]
        if r.judge and not r.judge.get("pass"):
            failed += [f"judge: {i}" for i in r.judge.get("issues", [])]
        if r.error:
            failed.append("error: " + r.error.splitlines()[0])
        cell = "<br>".join(x.replace("|", "/").replace("\n", " ")[:220] for x in failed) or "—"
        lines.append(f"| {r.name} | {r.category} | {'PASS' if r.passed else '**FAIL**'} | {cell} |")
    scores = [r.judge.get("scores", {}) for r in results if r.judge and r.judge.get("scores")]
    if scores:
        lines += ["", "**Average judge scores (1-5):** " + ", ".join(
            f"{k} {sum(s.get(k, 0) for s in scores) / len(scores):.2f}"
            for k in ("sop_adherence", "grounding", "naturalness", "empathy", "memory"))]
    lines += ["", "## Transcripts", ""]
    for r in results:
        lines.append(f"<details><summary>{'✅' if r.passed else '❌'} {r.name}</summary>\n")
        for speaker, text, phase, _ in r.transcript:
            lines.append(f"- **{speaker}** `{phase}`: {text}")
        if r.judge:
            lines.append(f"\n_judge_: {json.dumps(r.judge)}")
        lines.append("\n</details>\n")
    path = REPORTS / f"{stem}-{stamp}.md"
    path.write_text("\n".join(lines))
    return path
