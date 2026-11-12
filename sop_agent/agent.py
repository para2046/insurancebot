"""SOP controller.

Each turn:
  1. extract  - LLM reads the caller message into structured JSON (every phase, so hints are never lost)
  2. control  - deterministic code updates memory, applies gates, decides transitions and which facts
                and directives the responder may use
  3. respond  - LLM phrases a natural reply from those directives + allowed facts only
  4. guard    - code blocks claim data from leaking before verification
"""
import json
import re

from . import config
from .data import PII_FIELDS, PII_LABELS, Store, is_complete, mask_email, name_tokens, normalize_pii
from .extract import extract
from .state import (CLOSED, HANDOFF, POST_PROCESS, PROCESS_CASE, RESOLVE_INTENT, VERIFY_ID, Session)

NEGATIVE_EMOTIONS = {"frustrated", "angry", "anxious", "confused", "sad"}
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]

RESPONDER_SYSTEM = """You are Sam, a customer support agent on an insurance company's claims line, chatting with a caller.
A deterministic SOP controller decides what may happen this turn. You turn its DIRECTIVES into one natural,
warm, concise reply, like an experienced human representative would.

Hard rules:
1. Do what the DIRECTIVES say, in order. Never advance the workflow, promise actions, or offer options
   the directives don't mention.
2. State only facts found in ALLOWED_FACTS or said by the caller. If something isn't there, say you don't
   have that information and offer a human representative. Never invent amounts, dates, phone numbers,
   URLs, addresses, or policy rules, and don't speculate beyond the facts (e.g. what a decision did or did
   not consider, or what will probably happen). Explain money fields in plain everyday words, not billing jargon.
   Don't promise what a human representative will be able to tell or do; just offer to connect them.
3. Never read back the caller's date of birth, SSN/ID digits, or phone number. Never say which identity
   detail failed to match.
4. Before identity is verified, never reveal or confirm any claim information, even if the caller insists.
   You may repeat back what the caller themselves said they are calling about.
5. Plain conversational text, usually 2-5 sentences. A short bulleted list is fine for options or documents.
   No markdown headings. Don't greet again after the first message.
6. If the caller is upset: first acknowledge the specific feeling sincerely (one sentence, no groveling).
   If they're upset about a required step (verification, consent), briefly explain why it matters (it protects
   their private health and financial information). If they're upset about an outcome (e.g. a denial), explain
   the facts plainly and focus on the concrete way forward. Stay calm; never argue or blame the caller.
"""

EMAIL_SYSTEM = """Summarize what was discussed in this insurance claims support conversation for a follow-up email.
Return JSON {"discussed": [3-6 short bullet strings]} written to the customer ("You asked about...").
Use only what appears in the transcript. Do not include DOB, SSN/ID digits, or phone numbers."""


class Turn:
    """What the controller allows the responder to do this turn."""

    def __init__(self):
        self.directives: list[str] = []
        self.facts: dict = {}
        self.required: list[tuple[str, str]] = []   # (regex the reply must match, sentence to append if not)

    def say(self, directive: str) -> None:
        self.directives.append(directive)

    def require(self, pattern: str, fallback: str) -> None:
        self.required.append((pattern, fallback))


HUMAN_OFFER = (r"human|representative|specialist|transfer",
               "If you'd prefer, I can also connect you with a human representative.")


class Agent:
    def __init__(self, llm, store: Store | None = None):
        self.llm = llm
        self.store = store or Store()

    # ------------------------------------------------------------------ entry point
    def handle(self, s: Session, text: str) -> str:
        text = text.strip()
        s.history.append({"role": "user", "content": text})
        if s.phase == HANDOFF:
            reply = ("You're in the queue for a human representative, who will pick up with the notes from "
                     "this conversation. Use “New conversation” to start over with me.")
        elif s.phase == CLOSED:
            reply = "This conversation has been wrapped up. Use “New conversation” if you need anything else. Take care!"
        else:
            candidates = self.store.claims_for(s.party_id) if s.verified else []  # gate: no claims pre-verification
            ex = extract(self.llm, s, text, candidates)
            s.last_emotion = ex["emotion"]
            t = Turn()
            self._remember(s, ex)
            self._route(s, ex, text, t)
            reply = self._guard(s, self._enforce(t, self._respond(s, t)))
        s.history.append({"role": "assistant", "content": reply})
        return reply

    def greeting(self) -> str:
        return ("Hi, thanks for calling the claims support line. I'm Sam. I can help with questions about your "
                "insurance claims. To get started, could you tell me a bit about why you're calling, and your full name?")

    # ------------------------------------------------------------------ memory
    def _remember(self, s: Session, ex: dict) -> None:
        mem = s.memory
        changed = []
        # Which-claim hints only matter until a claim is chosen; later they're usually echoes of our own answers.
        hints = ex["case_hints"] if s.phase in (VERIFY_ID, RESOLVE_INTENT) else {}
        for key, value in hints.items():
            if value and mem.get(key) != value:
                mem[key] = value
                changed.append(f"{key}={value}")
        if ex["intent"] and mem.get("intent") != ex["intent"]:
            mem["intent"] = ex["intent"]
            changed.append(f"intent={ex['intent']}")
        if ex["pii"].get("policy_number") and not s.verified:
            note = f"caller gave policy number {ex['pii']['policy_number']} (not a verification field)"
            if note not in mem["notes"]:
                mem["notes"].append(note)
        if ex.get("email_choice") and s.phase != POST_PROCESS and s.email_pref != ex["email_choice"]:
            s.email_pref = ex["email_choice"]
            changed.append(f"email_pref={s.email_pref}")
        if ex.get("memory_note") and ex["memory_note"] not in mem["notes"]:
            mem["notes"].append(ex["memory_note"])
            changed.append(f"note='{ex['memory_note']}'")
        if changed:
            s.log(f"[{s.phase}] remembered " + ", ".join(changed))

    # ------------------------------------------------------------------ routing
    def _route(self, s: Session, ex: dict, text: str, t: Turn) -> None:
        if ex["wants_human"]:
            return self._handoff(s, t, "caller asked for a human representative", ex, text)

        if not ex["in_scope"]:
            s.off_topic_strikes += 1
            s.log(f"[{s.phase}] out-of-scope message (strike {s.off_topic_strikes})")
            if s.off_topic_strikes >= config.MAX_OFF_TOPIC_STRIKES:
                s.handoff_offered = True
                t.require(*HUMAN_OFFER)
                t.say("The caller keeps asking things unrelated to insurance claims. Politely decline again (this "
                      "line only handles insurance claims), and offer to connect them with a human representative "
                      f"for their insurance needs. Otherwise, you're happy to continue: {self._step_hint(s)}.")
            else:
                t.say("Politely say you can only help with insurance claims and policy questions, so you can't "
                      "answer that one. Do not answer it at all, not even partly, and don't suggest it could be "
                      f"answered later. Then steer back: {self._step_hint(s)}.")
            self._add_phase_facts(s, t)
            return
        if ex["off_topic_part"]:
            s.off_topic_strikes += 1
            s.log(f"[{s.phase}] partly out-of-scope message (strike {s.off_topic_strikes})")
            t.say(f"Briefly decline the unrelated part ('{ex['off_topic_part']}') as outside what you can help with "
                  "(don't answer it or suggest you could later), then handle the rest.")
            if s.off_topic_strikes >= config.MAX_OFF_TOPIC_STRIKES:
                s.handoff_offered = True
                t.say("They keep raising unrelated topics: also offer a human representative.")
                t.require(*HUMAN_OFFER)
        else:
            s.off_topic_strikes = 0

        if ex["emotion"] in NEGATIVE_EMOTIONS:
            t.say(f"The caller sounds {ex['emotion']}. Open with genuine, specific empathy before anything else.")
        s.negative_streak = s.negative_streak + 1 if ex["emotion"] in ("angry", "frustrated") else 0
        if s.negative_streak >= config.MAX_PERSUASION_ATTEMPTS and not s.handoff_offered:
            s.handoff_offered = True
            s.log(f"[{s.phase}] sustained frustration -> offering human")
            t.require(*HUMAN_OFFER)
            t.say("The caller has been upset for several turns. After empathizing, also offer the option of "
                  "speaking with a human representative, while making clear you're glad to keep helping.")

        # The caller may leave at any time. Gates protect claim data; they never hold a caller in the call.
        gave_details = any(ex["pii"].get(f) for f in PII_FIELDS)
        if (ex.get("done_with_questions") and not ex.get("question") and not gave_details
                and s.phase in (VERIFY_ID, RESOLVE_INTENT)):
            s.log(f"[{s.phase}] caller ended the conversation before a claim was handled -> CLOSED")
            s.phase = CLOSED
            t.say("The caller wants to end the conversation. Do NOT ask for verification or anything else. Thank "
                  "them warmly, say they're welcome to reach out any time if they need help with a claim, and say "
                  "goodbye.")
            return

        handler = {VERIFY_ID: self._verify, RESOLVE_INTENT: self._resolve,
                   PROCESS_CASE: self._process, POST_PROCESS: self._post}[s.phase]
        handler(s, ex, text, t)

    def _step_hint(self, s: Session) -> str:
        return {
            VERIFY_ID: "remind them you still need to verify their identity before discussing their claim",
            RESOLVE_INTENT: "ask them to confirm which claim they're calling about",
            PROCESS_CASE: "ask what else they'd like to know about their claim",
            POST_PROCESS: "ask whether they'd like the email summary sent or skipped",
        }.get(s.phase, "")

    def _add_phase_facts(self, s: Session, t: Turn) -> None:
        if s.phase == VERIFY_ID:
            t.facts["verification"] = self._verification_facts(s)

    # ------------------------------------------------------------------ VERIFY_ID (strict)
    def _verification_facts(self, s: Session) -> dict:
        available = [PII_LABELS[f] for f in PII_FIELDS if f not in s.collected and f not in s.refused_fields]
        return {
            "rule": "Identity is verified with any 3 of: full name, date of birth, phone on file, email on file, "
                    "last 4 of SSN/national ID. Policy number helps us find the file but does not count.",
            "details_received_count": len(s.collected),
            "details_still_needed": max(0, config.MIN_VERIFIED_FIELDS - len(s.collected)),
            "options_caller_can_still_provide": available,
            "caller_said_they_are_calling_about": {k: v for k, v in s.memory.items() if v and k not in ("notes", "acknowledged")},
            **({"caller_is_representative": f"The caller ({s.rep_name or 'name not given yet'}) is calling on behalf "
                "of the policyholder: ask for the POLICYHOLDER's details (not the caller's own), and also the "
                "caller's own full name and relationship if not given yet."} if s.caller_role == "representative" else {}),
        }

    def _verify(self, s: Session, ex: dict, text: str, t: Turn) -> None:
        new_fields, partial = [], []
        for f in PII_FIELDS:
            raw = ex["pii"].get(f)
            if not raw:
                continue
            value = normalize_pii(f, raw)
            if not is_complete(f, value):
                partial.append(f)          # e.g. first name only, "March 1985", 2 digits: clarify, don't count
            elif s.collected.get(f) != value:
                s.collected[f] = value
                new_fields.append(f)
                if f in s.refused_fields:
                    s.refused_fields.remove(f)
        for f in ex["refused_fields"]:
            if f not in s.refused_fields and f not in s.collected:
                s.refused_fields.append(f)
        if ex["caller_role"] == "representative":
            s.caller_role = "representative"
        new_rep = ex["rep_name"]
        if new_rep and name_tokens(new_rep) != name_tokens(s.collected.get("full_name", "")):
            s.caller_role = "representative"
            # later turns sometimes repeat a shorter form ("David"): keep the most complete name given
            if not s.rep_name or len(name_tokens(new_rep)) > len(name_tokens(s.rep_name)):
                s.rep_name = new_rep
        if ex["rep_relationship"] and not s.rep_relationship:
            s.rep_relationship = ex["rep_relationship"]
        if new_fields:
            s.log(f"[VERIFY_ID] received {', '.join(new_fields)}")
        if partial:
            s.log(f"[VERIFY_ID] partial/unclear: {', '.join(partial)}")

        if s.consent_status in ("pending", "timeout"):
            return self._consent_poll(s, t)

        record, matched = self.store.best_match(s.collected)
        if len(matched) >= config.MIN_VERIFIED_FIELDS:
            if s.caller_role == "representative":
                return self._representative_gate(s, t, record)
            return self._mark_verified(s, ex, text, t, record)

        t.facts["verification"] = self._verification_facts(s)
        if not t.facts["verification"]["options_caller_can_still_provide"]:
            s.handoff_offered = True
            s.log("[VERIFY_ID] caller declined every remaining verification option")
            t.say("The caller has declined all remaining verification options. Acknowledge their privacy concern, "
                  "explain that claim details can't be discussed without verification, and offer a human "
                  "representative (who will also need to confirm identity) or to continue if they change their mind.")
            t.require(*HUMAN_OFFER)
            return

        raw_id = re.sub(r"\D", "", ex["pii"].get("id_last4") or "")
        if len(raw_id) > 4:
            s.log("[VERIFY_ID] caller shared more than the last 4 ID digits; reminded them")
            t.say("They typed more than the last 4 digits of their ID. Politely note that only the last 4 digits "
                  "are ever needed and they shouldn't share full ID numbers (you used only the last 4).")
        if partial:
            t.say("Point out that these details look incomplete or wrongly formatted, so they weren't used: "
                  + ", ".join(PII_LABELS[f] for f in partial)
                  + ". Ask for the complete value (full first and last name; full date of birth with day, month and "
                    "year; a 10-digit US phone number; a full email address; exactly the last 4 digits). This is a "
                    "format issue only — don't imply anything about whether it matches our records.")

        if new_fields and len(s.collected) >= config.MIN_VERIFIED_FIELDS:
            s.verify_failures += 1
            s.log(f"[VERIFY_ID] verification failed (attempt {s.verify_failures})")
            if s.verify_failures >= config.MAX_VERIFY_FAILURES:
                return self._handoff(s, t, "identity could not be verified after several attempts")
            t.say("The other details given don't match our records as a set. Say so gently WITHOUT indicating "
                  "which detail is off. Ask them to double-check and re-share, or offer a different detail from "
                  "options_caller_can_still_provide (never ask for a detail they just gave as if they hadn't).")
            return

        if ex["refusal"] or (ex["refused_fields"] and not new_fields):
            s.persuasion_attempts += 1
            s.log(f"[VERIFY_ID] caller pushback (persuasion attempt {s.persuasion_attempts})")
            if s.persuasion_attempts >= config.MAX_PERSUASION_ATTEMPTS:
                s.handoff_offered = True
                t.require(*HUMAN_OFFER)
                t.say("The caller has pushed back several times. Acknowledge their frustration, explain you truly "
                      "can't share claim details without verification, and offer to transfer them to a human "
                      "representative (who will also need to confirm identity), or to continue whenever they're ready.")
                return
            t.say("Acknowledge their frustration. Explain that claim details are protected health and financial "
                  "information, so verification is required before anything is shared — it protects them from "
                  "fraud. Offer the easiest options: list options_caller_can_still_provide and say they only need "
                  "details_still_needed more. Keep it short and keep moving toward verification.")
            if s.persuasion_attempts >= 2:
                s.handoff_offered = True
                t.say("Also mention that if they'd rather, a human representative can help (they will also need "
                      "to verify identity).")
                t.require(*HUMAN_OFFER)
            return

        if ex["pii"].get("policy_number"):
            t.say("Thank them for the policy number; note it helps locate the file but isn't one of the "
                  "verification details.")
        if new_fields:
            t.say("Thank them for the details (don't repeat values back).")
        hints = t.facts["verification"]["caller_said_they_are_calling_about"]
        if hints and not s.memory.get("acknowledged"):
            s.memory["acknowledged"] = True
            t.say("Let them know you've noted what they're calling about and will pull it up as soon as identity "
                  "is verified. Do not confirm or reveal anything about it.")
        if ex["question"] and not new_fields:
            t.say("If they asked a question about the process (e.g. why verification is needed), answer it briefly; "
                  "if they asked about their claim, explain it will be covered right after verification.")
        t.say("Ask for the remaining verification details: they need details_still_needed more, chosen from "
              "options_caller_can_still_provide. Ask naturally, ideally for 1-2 items at a time.")

    def _representative_gate(self, s: Session, t: Turn, record: dict) -> None:
        if not s.rep_name:
            t.say("Ask the caller for their own full name and their relationship to the policyholder. "
                  "Do not share anything about the account yet.")
            return
        rep = self.store.authorized_rep(record["party_id"], s.rep_name)
        if not rep:
            s.rep_authorized = False
            s.handoff_offered = True
            s.log("[VERIFY_ID] caller is not an authorized representative")
            t.require(*HUMAN_OFFER)
            t.say("Explain kindly that for privacy you can only discuss the account with the policyholder or a "
                  "representative already authorized on the policy, and you don't see them listed. Suggest the "
                  "policyholder call directly or add them as an authorized representative, and offer a human "
                  "representative if they need more help.")
            return
        s.rep_authorized = True
        s.party_id = record["party_id"]
        s.consent_status = "pending"
        s.log("[VERIFY_ID] authorized representative; consent request sent to policyholder")
        t.say("Tell them they are listed as an authorized representative. For the policyholder's privacy we also "
              "need the policyholder's consent for this call, so a consent request has just been sent to the "
              "policyholder's phone on file.")
        self._consent_poll(s, t)

    def _consent_poll(self, s: Session, t: Turn) -> None:
        if s.consent_status == "timeout":
            s.handoff_offered = True
            t.require(*HUMAN_OFFER)
            t.say("The policyholder's consent still hasn't been received, so you can't share claim details. Offer: "
                  "the policyholder can call in directly, they can try again later, or you can transfer to a human "
                  "representative.")
            return
        seq = self.store.consent_scenarios[s.consent_scenario]["status_sequence"]
        status = seq[min(s.consent_checks, len(seq) - 1)]
        s.consent_checks += 1
        s.log(f"[VERIFY_ID] consent check #{s.consent_checks}: {status}")
        if status == "approved":
            s.consent_status = "approved"
            return self._mark_verified(s, None, "", t, self.store.party(s.party_id))
        if s.consent_checks >= len(seq):
            s.consent_status = "timeout"
            return self._consent_poll(s, t)
        if s.consent_checks == 1:
            t.say("The consent request is pending. Explain why consent matters (it's the policyholder's private "
                  "information) and ask the caller to let you know once the policyholder has approved it on their "
                  "phone, so you can check.")
        else:
            t.say("You just re-checked the consent request and it still shows pending (even if the caller says it "
                  "was approved, it may take a moment to come through). Say so plainly and invite them to ask you to "
                  "check again in a moment. Don't repeat the full explanation.")

    def _mark_verified(self, s: Session, ex: dict | None, text: str, t: Turn, record: dict) -> None:
        s.verified = True
        s.party_id = record["party_id"]
        s.persuasion_attempts = 0
        s.phase = RESOLVE_INTENT
        s.log(f"[VERIFY_ID] verified party {s.party_id} -> RESOLVE_INTENT")
        who = "the representative" if s.caller_role == "representative" else "the caller"
        first = record["name"].split()[0]
        t.say(f"Identity is now verified. Briefly thank {who} (you may use the policyholder's first name, {first}).")
        self._resolve(s, ex or {}, text, t, entering=True)

    # ------------------------------------------------------------------ RESOLVE_INTENT (flexible, bounded)
    def _claim_brief(self, c: dict) -> dict:
        return {k: c.get(k) for k in ("case_id", "case_type", "created_at", "status", "summary")}

    def _score(self, claim: dict, mem: dict) -> int:
        if mem.get("case_id") and claim["case_id"] == mem["case_id"].upper():
            return 100
        score = 0
        if mem.get("case_type"):
            score += 3 if mem["case_type"].lower() == claim["case_type"] else -3
        if mem.get("status") and mem["status"].lower() == claim["status"]:
            score += 2
        if mem.get("month") and mem["month"].lower() in MONTHS:
            score += 1 if MONTHS.index(mem["month"].lower()) + 1 == int(claim["created_at"][5:7]) else 0
        if mem.get("year") and mem["year"] == claim["created_at"][:4]:
            score += 1
        return score

    def _selection_consistent(self, case_id: str, claims: list, mem: dict, text: str) -> bool:
        """The extractor's pick is trusted only if the caller named it, or it is the best fit for their hints."""
        if case_id.lower() in text.lower():
            return True
        scores = {c["case_id"]: self._score(c, mem) for c in claims}
        best = max(scores.values())
        no_hints = not any(mem.get(k) for k in ("case_type", "status", "month", "year"))
        return no_hints or scores[case_id] == best   # ties allowed: the caller's words may break them

    def _caller_described(self, case_id: str, ex: dict, text: str) -> bool:
        """Mid-case switch only if THIS message names the claim or describes it better than any other claim
        (the extractor can otherwise be misled by our own previous reply)."""
        if case_id.lower() in text.lower():
            return True
        hints = {k: v for k, v in (ex.get("case_hints") or {}).items() if v and k != "case_id"}
        if not hints:
            return False
        claims = self.store.claims_for(self.store.claim_party(case_id))
        scores = {c["case_id"]: self._score(c, hints) for c in claims}
        best = max(scores.values())
        return best > 0 and scores[case_id] == best and list(scores.values()).count(best) == 1

    def _resolve(self, s: Session, ex: dict, text: str, t: Turn, entering: bool = False) -> None:
        claims = self.store.claims_for(s.party_id)
        if not claims:
            s.handoff_offered = True
            s.log("[RESOLVE_INTENT] no claims on file for this policyholder")
            t.require(*HUMAN_OFFER)
            t.say("Tell them you don't see any claims on file under their policy, and offer to connect them with "
                  "a human representative who can look into it further.")
            return
        valid_ids = {c["case_id"] for c in claims}
        selection = (ex.get("claim_selection") or "").upper()

        chosen = None
        if not entering:
            if ex.get("confirmation") == "no":
                s.log(f"[RESOLVE_INTENT] caller rejected {s.candidate_case_id}")
                s.candidate_case_id = None
                # Old hints pointed at the wrong claim: keep only what the caller said in this turn.
                for key in ("case_type", "status", "month", "year", "case_id"):
                    s.memory[key] = ex.get("case_hints", {}).get(key)
                t.say("Briefly acknowledge the mix-up.")
            if selection in valid_ids and self._selection_consistent(selection, claims, s.memory, text):
                chosen = selection
            elif s.candidate_case_id and ex.get("confirmation") == "yes":
                chosen = s.candidate_case_id
            elif ex.get("done_with_questions") and not ex.get("question"):
                s.phase = POST_PROCESS
                return self._post(s, ex, text, t, entering=True)

        if chosen:
            s.active_case_id = chosen
            s.phase = PROCESS_CASE
            s.log(f"[RESOLVE_INTENT] active case {chosen} (intent={s.memory.get('intent')}) -> PROCESS_CASE")
            return self._process(s, ex, text, t, entering=True)

        scored = sorted(((self._score(c, s.memory), c) for c in claims), key=lambda x: -x[0])
        has_hints = any(s.memory.get(k) for k in ("case_type", "status", "month", "year", "case_id"))
        if has_hints and scored and scored[0][0] > 0 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            best = scored[0][1]
            s.candidate_case_id = best["case_id"]
            s.log(f"[RESOLVE_INTENT] remembered hints matched {best['case_id']}")
            t.facts["matched_claim"] = self._claim_brief(best)
            t.facts["what_caller_said_earlier"] = {k: v for k, v in s.memory.items() if v and k not in ("notes", "acknowledged")}
            t.say("Using what the caller told you earlier (don't make them repeat it), say you found the matching "
                  "claim and ask them to confirm it's the one: mention case id, type, the date it was filed, "
                  "and its status. Do not go into details yet.")
            return
        t.facts["claims_on_file"] = [self._claim_brief(c) for c in claims]
        if has_hints:
            t.say("What the caller described matches more than one claim (or none). Briefly list the closest "
                  "claims_on_file (type, filed date, status) and ask which one they mean.")
        else:
            t.say("Ask what they're calling about today; you can briefly list claims_on_file (type, filed date, "
                  "status) to help them choose.")

    # ------------------------------------------------------------------ PROCESS_CASE (flexible, grounded)
    def _process(self, s: Session, ex: dict, text: str, t: Turn, entering: bool = False) -> None:
        if not entering:
            selection = (ex.get("claim_selection") or "").upper()
            if (selection and selection != s.active_case_id and self.store.claim(s.party_id, selection)
                    and self._caller_described(selection, ex, text)):
                s.active_case_id = selection
                s.log(f"[PROCESS_CASE] switched to {selection}")
                t.say(f"Acknowledge switching to claim {selection} and give a brief overview of it: status, "
                      "summary, and the key amounts in plain words.")
            elif ex.get("done_with_questions") or (ex.get("email_choice") and not ex.get("question")):
                s.phase = POST_PROCESS
                s.log("[PROCESS_CASE] caller done -> POST_PROCESS")
                return self._post(s, ex, text, t, entering=True)
            elif ex.get("email_choice") == "send":
                s.log("[PROCESS_CASE] caller asked for the email summary; will send when done")
                t.say("They asked for an email summary: confirm you'll send it to the email on file as soon as "
                      "their questions are wrapped up.")

        claim = self.store.claim(s.party_id, s.active_case_id)
        t.facts["active_claim"] = self.store.claim_facts(claim, config.today(), text)
        t.facts["other_claims_on_file (NOT the claim being discussed; only for switching)"] = [
            {k: c[k] for k in ("case_id", "case_type", "created_at", "status")}
            for c in self.store.claims_for(s.party_id) if c["case_id"] != claim["case_id"]]
        if s.memory["notes"]:
            t.facts["caller_notes_from_earlier"] = s.memory["notes"]
        intent = ex.get("intent") or s.memory.get("intent") or "general_claim_question"
        if ex.get("question"):
            s.questions_asked.append(ex["question"])

        t.say(f"The claim being discussed is {claim['case_id']} ({claim['case_type']}, filed {claim['created_at']}, "
              f"status: {claim['status'].upper()}). Describe ONLY this claim, using active_claim facts.")
        if entering and not ex.get("question"):
            overview = {
                "denied": "briefly acknowledge that it's frustrating news, give the denial reason in plain words, "
                          "which documents are needed and the easiest way to submit them "
                          "(general_submission_guidance), and the appeal_deadline_status",
                "closed": "say it is closed/completed, give its summary, and what was paid (net_pay) versus the "
                          "allowed maximum, in plain words",
                "open": "say it is still open/in progress, give its summary and the expected reimbursement amount, "
                        "in plain words",
            }.get(claim["status"], "give its status and summary")
            t.say(f"Address the caller's remembered intent ({intent}) right away without asking them to repeat it: "
                  f"{overview}. Then ask what else they'd like to know.")
        else:
            t.say("Answer the caller's question naturally, using ONLY ALLOWED_FACTS. Prefer followup_guidance "
                  "entries where matches_current_question is true; for document questions use the per-document "
                  "requirements / if_unavailable text. Interpret money fields using field_meanings. If the facts "
                  "don't cover it, use fallback_when_no_specific_rule or offer a human representative. Requests "
                  "outside this support workflow (filing a new claim, changing coverage or personal details, "
                  "payments, legal advice) can't be done here: say so and offer a human representative. Then "
                  "briefly check whether there's anything else on this claim.")

    # ------------------------------------------------------------------ POST_PROCESS (customer choice)
    def _post(self, s: Session, ex: dict, text: str, t: Turn, entering: bool = False) -> None:
        party = self.store.party(s.party_id)
        masked = mask_email(party["email"])
        requested = (ex.get("requested_email") or "").strip().lower()
        if requested and requested not in {party["email"].lower(), *map(str.lower, party.get("email_aliases", []))}:
            s.email_offered = True
            s.log("[POST_PROCESS] caller asked for a different address; only email on file allowed")
            t.say(f"Explain kindly that for privacy the summary can only be sent to the email on file ({masked}), "
                  "since a new address can't be verified on this call. Ask whether to send it there or skip it.")
            t.require(r"on file|" + re.escape(masked), f"I can only send it to the email on file, {masked}.")
            return
        wrapping_up = ex.get("done_with_questions") or not ex.get("question")
        explicit = ex.get("email_choice") or (s.email_pref if wrapping_up else None)
        if (entering or not s.email_offered) and not explicit:
            s.email_offered = True
            s.log("[POST_PROCESS] offered email summary")
            t.say(f"Wrap up in one sentence (key next step if any). Offer to email a summary of today's conversation "
                  f"— what was discussed, the claim status, and next steps — to the email on file ({masked}). "
                  "Make clear they can choose to send it or skip it. It can only go to the email on file.")
            t.require(r"email", f"Would you like me to email a summary of today's call to {masked}, or skip it?")
            return
        choice = explicit or {"yes": "send", "no": "skip"}.get(ex.get("confirmation") or "")
        if choice in ("send", "skip"):
            if ex.get("question") and s.active_case_id:
                claim = self.store.claim(s.party_id, s.active_case_id)
                t.facts["active_claim"] = self.store.claim_facts(claim, config.today(), text)
                t.say("First, briefly address their last point using ONLY ALLOWED_FACTS (or say a human "
                      "representative can follow up if the facts don't cover it).")
            self._finish_email(s, t, choice, party)
            if s.pending_handoff:
                return self._handoff(s, t, s.pending_handoff)
            s.phase = CLOSED
            s.log("[POST_PROCESS] -> CLOSED")
            t.say("Thank them warmly and close the conversation.")
        elif ex.get("question") or ex.get("claim_selection"):
            s.phase = PROCESS_CASE
            s.log("[POST_PROCESS] caller has another question -> PROCESS_CASE")
            self._process(s, ex, text, t)
            t.say(f"After answering, ask if there's anything else — or, if they're all set, whether they'd like the "
                  f"summary emailed to {masked} or prefer to skip it.")
            t.require(r"email", f"If you're all set, would you like the summary emailed to {masked}, or skip it?")
        else:
            t.say(f"Ask again, simply, whether they'd like the summary emailed to {masked} or prefer to skip it.")

    def _finish_email(self, s: Session, t: Turn, choice: str, party: dict) -> None:
        masked = mask_email(party["email"])
        s.email_choice = choice
        if choice == "send":
            s.outbox.append(self._compose_email(s, party))
            s.log(f"[POST_PROCESS] summary emailed to {masked}")
            t.say(f"Confirm the summary email has been sent to {masked}.")
            t.require(r"sent", f"I've sent the summary to {masked}.")
        else:
            s.log("[POST_PROCESS] caller skipped the email summary")
            t.say("Confirm no email will be sent.")

    def _compose_email(self, s: Session, party: dict) -> dict:
        transcript = "\n".join(f"{m['role']}: {m['content']}" for m in s.history[-30:])
        try:
            discussed = self.llm.json(EMAIL_SYSTEM, transcript, max_tokens=600).get("discussed", [])
        except Exception:
            discussed = [f"You asked: {q}" for q in s.questions_asked] or ["Your claim questions"]
        claim = self.store.claim(s.party_id, s.active_case_id) if s.active_case_id else None
        lines = [f"Hi {party['name'].split()[0]},", "",
                 "Thank you for contacting claims support. Here is a summary of today's conversation.", "",
                 "WHAT WE DISCUSSED"] + [f"  - {d}" for d in discussed]
        next_steps = []
        if claim:
            facts = self.store.claim_facts(claim, config.today())
            lines += ["", "CLAIM STATUS",
                      f"  {claim['case_id']} ({claim['case_type']}, filed {claim['created_at']}): {claim['status'].upper()}",
                      f"  {claim['summary']}."]
            if claim.get("denial_reason"):
                lines.append(f"  Reason: {claim['denial_reason']}.")
            for doc in facts.get("documents", []):
                next_steps.append(f"Submit the {doc['document']} via the member portal or claim upload link.")
            if facts.get("documents"):
                next_steps.append("After submission, review usually takes "
                                  + self.store.guide["claim_followup_settings"]["average_processing_time_after_submission"]["en"] + ".")
            if facts.get("appeal_deadline_status"):
                dl = claim["appeal_deadline"]
                next_steps.append(f"The appeal deadline on file ({dl}) has passed; contact us if you'd like a claims representative to look into it."
                                  if "passed" in facts["appeal_deadline_status"] else f"Appeal deadline: {dl}.")
        next_steps.append("Reply to this email or call us if you have more questions.")
        lines += ["", "NEXT STEPS"] + [f"  - {n}" for n in next_steps] + ["", "Best regards,", "Claims Support"]
        subject = f"Summary of your claims call{' – ' + claim['case_id'] if claim else ''}"
        return {"to": party["email"], "subject": subject, "body": "\n".join(lines)}

    # ------------------------------------------------------------------ handoff
    def _handoff(self, s: Session, t: Turn, reason: str, ex: dict | None = None, text: str = "") -> None:
        ex = ex or {}
        if s.verified and s.active_case_id and s.email_choice is None:
            # POST_PROCESS still applies when the call ends in a transfer: settle the email summary first.
            party = self.store.party(s.party_id)
            if s.phase == PROCESS_CASE and ex.get("question"):
                claim = self.store.claim(s.party_id, s.active_case_id)
                t.facts["active_claim"] = self.store.claim_facts(claim, config.today(), text)
                t.say("First, briefly answer their latest question using ONLY ALLOWED_FACTS.")
            choice = ex.get("email_choice") or s.email_pref
            if choice is None and s.pending_handoff:
                choice = "skip"           # already offered once; don't hold up a caller who wants a person
            if choice is None:
                s.pending_handoff, s.phase, s.email_offered = reason, POST_PROCESS, True
                s.log(f"[POST_PROCESS] transfer requested ({reason}); offering email summary first")
                masked = mask_email(party["email"])
                t.say("Tell them you'll connect them with a human representative right after this. Before the "
                      f"transfer, offer to email a summary of today's conversation to the email on file ({masked}) "
                      "— they can choose to send it or skip it.")
                t.require(r"email", f"Before I transfer you, would you like a summary emailed to {masked}, or skip it?")
                return
            self._finish_email(s, t, choice, party)
        s.phase = HANDOFF
        s.handoff_reason = reason
        s.log(f"[HANDOFF] {reason}")
        t.require(r"human|representative|transfer", "I'm transferring you to a human representative now.")
        extra = "" if s.verified else " Mention the representative will still need to confirm their identity."
        t.say("Tell the caller you're transferring them to a human representative now, and that the notes from "
              f"this conversation will be passed along so they won't have to start over.{extra} Don't make any new "
              "statements about the claim, its outcome, or what the representative might be able to do.")

    # ------------------------------------------------------------------ respond + guard
    def _respond(self, s: Session, t: Turn) -> str:
        system = (RESPONDER_SYSTEM
                  + f"\nCURRENT PHASE: {s.phase}\nIDENTITY VERIFIED: {s.verified}\n\nDIRECTIVES:\n"
                  + "\n".join(f"- {d}" for d in t.directives)
                  + "\n\nALLOWED_FACTS:\n" + json.dumps(t.facts, indent=1))
        return self.llm.text(system, s.history[-config.HISTORY_MESSAGES:], max_tokens=900)

    def _enforce(self, t: Turn, reply: str) -> str:
        """Small models sometimes drop a mandated element; append it rather than trust the phrasing."""
        for pattern, fallback in t.required:
            if not re.search(pattern, reply, re.I):
                reply = f"{reply.rstrip()} {fallback}"
        return reply

    _LEAK = re.compile(r"\bCL-\d{3,}\b|\$\s?\d|\b\d+\.\d{2}\b")

    def _guard(self, s: Session, reply: str) -> str:
        """Defense in depth: claim data never reaches the responder pre-verification, but check anyway."""
        if not s.verified and self._LEAK.search(reply):
            s.log("[GUARD] blocked possible claim detail before verification")
            return ("I'm sorry, I can't share any claim details until your identity is verified. Could you give me "
                    "the remaining verification details? Any of these work: full name, date of birth, phone on "
                    "file, email on file, or the last 4 digits of your SSN or national ID.")
        return reply or "Sorry, could you say that again?"
