"""Session state: the single source of truth for phase, gates, memory and counters."""
from dataclasses import asdict, dataclass, field

VERIFY_ID = "VERIFY_ID"
RESOLVE_INTENT = "RESOLVE_INTENT"
PROCESS_CASE = "PROCESS_CASE"
POST_PROCESS = "POST_PROCESS"
CLOSED = "CLOSED"
HANDOFF = "HUMAN_HANDOFF"

INTENTS = ["denial_question", "status_inquiry", "document_submission", "next_steps", "general_claim_question"]


@dataclass
class Session:
    phase: str = VERIFY_ID
    consent_scenario: str = "default"

    # identity
    collected: dict = field(default_factory=dict)       # normalized PII the caller has given
    refused_fields: list = field(default_factory=list)
    caller_role: str = "self"                           # self | representative
    rep_name: str | None = None
    rep_relationship: str | None = None
    rep_authorized: bool | None = None
    consent_status: str | None = None                   # None | pending | approved | timeout
    consent_checks: int = 0
    verified: bool = False
    party_id: str | None = None
    verify_failures: int = 0

    # cross-phase memory (captured whenever the caller says it)
    memory: dict = field(default_factory=lambda: {
        "case_type": None, "status": None, "month": None, "year": None,
        "case_id": None, "intent": None, "notes": [],
    })

    # case handling
    candidate_case_id: str | None = None
    active_case_id: str | None = None
    questions_asked: list = field(default_factory=list)

    # guardrail counters
    off_topic_strikes: int = 0
    persuasion_attempts: int = 0
    last_emotion: str | None = None
    negative_streak: int = 0
    handoff_offered: bool = False
    handoff_reason: str | None = None

    # post process
    email_offered: bool = False
    email_choice: str | None = None
    email_pref: str | None = None           # send/skip, remembered whenever the caller says it
    pending_handoff: str | None = None      # transfer requested after a case was handled; email offer first
    outbox: list = field(default_factory=list)

    history: list = field(default_factory=list)          # [{"role", "content"}]
    events: list = field(default_factory=list)           # audit log of controller decisions

    def log(self, event: str) -> None:
        self.events.append(event)

    def panel(self) -> dict:
        """What the demo UI shows in the state panel."""
        data = asdict(self)
        data.pop("history")
        data.pop("outbox")
        data["collected"] = {k: ("•••" + v[-2:] if k in ("dob", "id_last4", "phone") else v)
                             for k, v in self.collected.items()}
        data["events"] = self.events[-12:]
        return data
