"""Fixture access, normalization and deterministic identity matching (no LLM in here)."""
import json
import re
from datetime import date, datetime
from itertools import permutations

from . import config

PII_FIELDS = ["full_name", "dob", "phone", "email", "id_last4"]
PII_LABELS = {
    "full_name": "full name",
    "dob": "date of birth",
    "phone": "phone number on file",
    "email": "email on file",
    "id_last4": "last 4 digits of SSN / national ID",
}


# ---------- normalization ----------

def name_tokens(value: str) -> list[str]:
    return re.findall(r"[a-z]+", (value or "").lower())


def norm_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def norm_email(value: str) -> str:
    return (value or "").strip().lower()


def norm_last4(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits[-4:] if len(digits) >= 4 else ""


_DOB_FORMATS = ["%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d %Y", "%b %d %Y",
                "%d %B %Y", "%d %b %Y", "%m/%d/%y"]


def norm_dob(value: str) -> str:
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", (value or "").strip()).replace(",", "")
    for fmt in _DOB_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def normalize_pii(field: str, value: str) -> str:
    return {
        "full_name": lambda v: " ".join(name_tokens(v)),
        "dob": norm_dob,
        "phone": norm_phone,
        "email": norm_email,
        "id_last4": norm_last4,
    }[field](value)


def is_complete(field: str, value: str) -> bool:
    """A normalized value that is usable for matching. Anything else is a partial answer to clarify."""
    if field == "full_name":
        return len(value.split()) >= 2
    if field == "phone":
        return len(value) == 10
    if field == "email":
        return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", value))
    if field == "id_last4":
        return len(value) == 4
    return bool(value)


def name_matches(given: str, record: dict) -> bool:
    """Exact match after normalization; tolerant of spacing ('Yawen'/'Ya Wen') and name order."""
    tokens = name_tokens(given)
    if not tokens or len(tokens) > 5:
        return False
    for candidate in [record["name"], *record.get("name_aliases", [])]:
        target = "".join(name_tokens(candidate))
        if any("".join(p) == target for p in permutations(tokens)):
            return True
    return False


def field_matches(field: str, value: str, record: dict) -> bool:
    if not value:
        return False
    if field == "full_name":
        return name_matches(value, record)
    if field == "dob":
        return value == record["dob"]
    if field == "phone":
        return value in {norm_phone(p) for p in [record["phone"], *record.get("phone_aliases", [])]}
    if field == "email":
        return value in {norm_email(e) for e in [record["email"], *record.get("email_aliases", [])]}
    if field == "id_last4":
        return value == record["id_last4"]
    return False


def mask_email(email: str) -> str:
    user, _, domain = email.partition("@")
    return f"{user[0]}{'*' * max(len(user) - 1, 3)}@{domain}"


# ---------- fixture store ----------

class Store:
    def __init__(self, fixtures_dir=config.FIXTURES_DIR):
        load = lambda name: json.loads((fixtures_dir / name).read_text())
        self.policyholders = load("policyholders.json")
        self.claims = load("claims.json")
        self.representatives = load("representatives.json")
        self.consent_scenarios = load("consent_scenarios.json")
        self.guide = load("required_document_guideline.json")
        self.schema = load("claim_schema.json")

    # --- identity ---
    def best_match(self, collected: dict) -> tuple[dict | None, list[str]]:
        """Return the policyholder record matching the most collected fields (all from ONE record)."""
        best, best_fields = None, []
        for record in self.policyholders:
            matched = [f for f in PII_FIELDS if field_matches(f, collected.get(f, ""), record)]
            if len(matched) > len(best_fields):
                best, best_fields = record, matched
        return best, best_fields

    def party(self, party_id: str) -> dict | None:
        return next((p for p in self.policyholders if p["party_id"] == party_id), None)

    def authorized_rep(self, party_id: str, rep_name: str) -> dict | None:
        target = name_tokens(rep_name)
        for rep in self.representatives:
            if rep["buyer_party_id"] == party_id and sorted(name_tokens(rep["rep_name"])) == sorted(target):
                return rep
        return None

    # --- claims ---
    def claims_for(self, party_id: str) -> list[dict]:
        return sorted((c for c in self.claims if c["party_id"] == party_id),
                      key=lambda c: c["created_at"], reverse=True)

    def claim_party(self, case_id: str) -> str | None:
        return next((c["party_id"] for c in self.claims if c["case_id"] == case_id), None)

    def claim(self, party_id: str, case_id: str) -> dict | None:
        return next((c for c in self.claims_for(party_id) if c["case_id"] == (case_id or "").upper()), None)

    # --- grounded guidance ---
    def _doc_key(self, doc: str) -> str | None:
        """Claims say 'office note'; guidance says 'treating provider office note'. Bridge the two."""
        doc = doc.lower()
        for key in self.guide["document_guidance"]:
            if doc in key or key in doc:
                return key
        return None

    def claim_facts(self, claim: dict, today: date, user_text: str = "") -> dict:
        g = self.guide
        docs = claim.get("documents_needed", [])
        docs_text = " and ".join(docs)
        processing = g["claim_followup_settings"]["average_processing_time_after_submission"]["en"]
        facts: dict = {
            "claim": {k: v for k, v in claim.items() if k != "party_id"},
            "field_meanings": {k: v["description"] for k, v in self.schema["field_descriptions"].items()},
            "today": today.isoformat(),
        }
        if claim.get("appeal_deadline"):
            deadline = date.fromisoformat(claim["appeal_deadline"])
            days = (deadline - today).days
            facts["appeal_deadline_status"] = (
                f"The appeal deadline ({deadline.isoformat()}) has already passed. Do not tell the caller "
                "they can still appeal on time, and do not claim late options exist: say you don't have "
                "information about late appeals and offer a human claims representative who can look into it."
                if days < 0 else f"{days} days remain before the appeal deadline ({deadline.isoformat()})."
            )
        if docs:
            facts["documents"] = []
            for doc in docs:
                key = self._doc_key(doc)
                facts["documents"].append({
                    "document": doc,
                    "requirements": g["document_guidance"].get(key, {}).get("en") or (
                        "No document-specific requirements on file; follow general_submission_guidance."),
                    "if_unavailable": g["document_alternative_guidance"].get(key, g["document_alternative_guidance"]["default"])["en"],
                })
            facts["general_submission_guidance"] = " ".join(filter(None, [
                g["default_guidance"]["en"],
                g["case_type_guidance"].get(claim["case_type"], {}).get("en"),
            ]))
            text = user_text.lower()
            facts["followup_guidance"] = []
            for rule in g["claim_followup_guidance"]:
                if rule.get("requires_documents") and not docs:
                    continue
                facts["followup_guidance"].append({
                    "topic": rule["topic"],
                    "matches_current_question": any(p in text for p in rule.get("match_any", [])),
                    "text": rule["en"].format(case_id=claim["case_id"], documents=docs_text,
                                              average_processing_time_after_submission=processing),
                })
            facts["human_review_rule"] = g["claim_followup_settings"]["human_review_after_document_alternatives_exhausted"]["en"]
        facts["fallback_when_no_specific_rule"] = g["claim_followup_fallback"]["en"]
        return facts
