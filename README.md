# Insurance Claims SOP Agent

A support agent that follows a fixed workflow and still talks naturally:

```
VERIFY_ID  ->  RESOLVE_INTENT  ->  PROCESS_CASE  ->  POST_PROCESS
 (strict)      (flexible, bounded)  (flexible, grounded)  (customer choice)
```

## Run

**Docker**
```bash
docker build -t insurance-sop-agent .
docker run -e OPENROUTER_API_KEY=sk-or-... -p 7860:7860 insurance-sop-agent
# open http://localhost:7860
```

**Local**
```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # put your OpenRouter key in it
.venv/bin/python app.py
```

The API key can come from `OPENROUTER_API_KEY` or be pasted under **Settings** in the UI. The model defaults
to `openai/gpt-5.6-luna` (OpenRouter); change it with `MODEL` or in the UI.

## Tests

Three layers. Only the first is free; the others call the model and cost roughly $0.01–0.02 per conversation.

```bash
.venv/bin/python -m pytest tests                          # 35 gate tests, fake LLM, no API calls
.venv/bin/python evals/dataset_validation.py              # A+B offline, C live: covers every fixture record
.venv/bin/python evals/dataset_validation.py --offline-only
.venv/bin/python evals/run_scripted.py                    # 34 hand-written edge cases + LLM judge
.venv/bin/python evals/run_generated.py --n 40 --seed 23  # LLM-simulated callers + LLM judge
```

- **`evals/dataset_validation.py`** tests against the provided fixtures:
  - **A.** An exhaustive identity matrix (1013 checks): every policyholder × every field subset × format variants (phone and DOB formats, name order and aliases, email aliases), plus single-field corruptions and fields mixed across different records.
  - **B.** Fixture integrity: document-name mapping, template filling, representatives, and consent sequences.
  - **C.** Live conversations covering every policyholder, every claim (status and amounts checked against ground truth), every follow-up guidance topic on every denied claim, and both consent scenarios.
- **`evals/run_scripted.py`** pins one requirement per case: the demo case, the bonus frustration example, partial answers, alternate ID fields, prompt injection, ambiguous claims, deadline honesty, off-topic strikes, representatives, and email choices.
- **`evals/run_generated.py`** samples personas from the fixtures (identity, which 3 fields the caller will share, field formats, speaking style, goal, email preference). A model plays the caller and the outcome is checked against expectations.
- Every live conversation is checked automatically for: (1) no claim data before verification, and (2) every amount, date or case ID the agent states exists in the fixtures. An LLM judge (`gpt-5.6-terra`) then grades it against a rubric. Reports are written to `evals/reports/`.

## Design

Every turn runs **extract → control → respond → guard**:

| Step | Who | What |
|---|---|---|
| extract | LLM | Reads the caller message into strict JSON-schema output: PII, claim hints, intent, emotion, scope, refusals, yes/no, email preference, free-form notes. Runs in **every** phase. If extraction fails, nothing is extracted, so no gate can open. |
| control | code | `sop_agent/agent.py` owns the phase, gates, counters, and transitions, and decides which facts and directives the responder gets. |
| respond | LLM | Phrases a reply from the directives and `ALLOWED_FACTS` only. |
| enforce | code | If a directive requires something (an offer of a human, the email offer) and the model left it out, code appends it. |
| guard | code | Before verification, blocks any reply that contains a case ID or an amount. |

**Strict where the SOP demands it**
- Identity is checked in code: 3 of name, DOB, phone, email, or SSN/national-ID last 4, all from the **same** record. The policy number helps locate the file but doesn't count. Matching tolerates name aliases and name order, phone formatting, and email aliases.
- Before verification, claim data is never put in the model's context, so it can't be leaked.
- Failed checks never say which field was wrong. After 3 failed attempts the call goes to a human.
- Partial answers (first name only, "March 1985", 2 digits) get a clarifying question. They don't count as failures.
- Representatives (`representatives.json`) also need the policyholder's consent, using the `consent_scenarios.json` sequence. If consent times out, the call can't proceed and the agent offers a human.

**Flexible where reasoning helps**
- Hints like "denied healthcare claim from January", an email preference, or notes like "already asked the lab" are remembered in any phase. After verification they're scored against the caller's claims; a unique match is proposed for confirmation, otherwise the closest claims are listed. The extractor's choice of claim is trusted only if it fits what the caller said.
- PROCESS_CASE answers from the claim record and `required_document_guideline.json`, with the templates filled in and matched to the question. Document names are bridged (the claim's "office note" maps to the guideline's "treating provider office note"). The appeal deadline is checked against today (`TODAY` overrides it).

**Guardrails in every phase**
- Out-of-scope questions get a polite decline. After 3 in a row, the agent offers a human.
- When the caller is upset, the agent responds with empathy first, then explains why the step matters, then offers alternatives. After 3 pushbacks it offers a human, but no gate ever opens because of emotion.
- Asking for a human hands off immediately, and the context is passed along. If a verified case was already handled, the email summary is settled first (sent if the caller asked for it, otherwise offered once).

**POST_PROCESS:** the agent offers an email summary to the email on file, and the caller can send or skip it. It never sends to an address given during the call. The email is assembled in code (status, reason, documents, deadline, timing), plus an LLM recap of what was discussed. Sending is mocked; the email is shown in the UI's **Email outbox** tab.

The **SOP state** tab shows the phase, collected fields (masked), memory, counters, and a log of controller decisions.

## Notes on the data
- CL-2048's appeal deadline (2026-03-18) is in the past relative to the real date. The agent says it has passed, doesn't invent late-appeal options, and offers a human. To demo an open appeal window, set `TODAY=2026-02-01`.
- CL-3001 needs a "diagnosis report", and `required_document_guideline.json` has no document-specific guidance for it. The agent falls back to the general submission guidance.
- P7 (Ava Lopez) and P13 (Ya Wen Li) have no claims. After verification, the agent says so and offers a human.
