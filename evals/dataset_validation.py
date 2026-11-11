"""Validate the agent against the provided fixture dataset.

Part A (offline): exhaustive identity matrix - every policyholder x every field subset x format variants,
                  single-field corruptions, and cross-record mixes.
Part B (offline): fixture integrity - doc-name mapping, template filling, reps, consent sequences.
Part C (live):    conversations covering every policyholder, every claim, and every follow-up guidance
                  topic on every denied claim, with answers checked against ground truth from the fixtures.

Usage: python evals/dataset_validation.py [--offline-only] [--no-judge]
"""
import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.common import STORE, run_scripted, write_report  # noqa: E402
from sop_agent import config  # noqa: E402
from sop_agent.data import PII_FIELDS, field_matches, normalize_pii  # noqa: E402

MONTHS = "January February March April May June July August September October November December".split()


def variants(person: dict) -> dict[str, list[str]]:
    y, m, d = person["dob"].split("-")
    n = person["phone"][-10:]
    first, *rest = person["name"].split()
    return {
        "full_name": [person["name"], person["name"].upper(), " ".join([*rest, first]), *person.get("name_aliases", [])],
        "dob": [person["dob"], f"{int(m)}/{int(d)}/{y}", f"{MONTHS[int(m) - 1]} {int(d)}, {y}",
                f"{int(d)} {MONTHS[int(m) - 1]} {y}", f"{MONTHS[int(m) - 1]} {int(d)}th {y}"],
        "phone": [person["phone"], f"({n[:3]}) {n[3:6]}-{n[6:]}", f"{n[:3]}.{n[3:6]}.{n[6:]}", f"1-{n[:3]}-{n[3:6]}-{n[6:]}"],
        "email": [person["email"], person["email"].upper(), *person.get("email_aliases", [])],
        "id_last4": [person["id_last4"], f"ends in {person['id_last4']}", f"xxx-xx-{person['id_last4']}"],
    }


def corrupt(field: str, person: dict) -> str:
    if field == "full_name":
        return person["name"].split()[0] + " Smith"
    if field == "dob":
        return (date.fromisoformat(person["dob"]) + timedelta(days=1)).isoformat()
    if field == "phone":
        return person["phone"][:-1] + str((int(person["phone"][-1]) + 1) % 10)
    if field == "email":
        return "x" + person["email"]
    return str((int(person["id_last4"]) + 1) % 10000).zfill(4)


def verify(collected_raw: dict) -> str | None:
    collected = {f: normalize_pii(f, v) for f, v in collected_raw.items()}
    record, matched = STORE.best_match(collected)
    return record["party_id"] if len(matched) >= config.MIN_VERIFIED_FIELDS else None


def part_a() -> tuple[list[str], dict]:
    failures, stats = [], {"positive": 0, "below_threshold": 0, "corrupted": 0, "cross_record": 0}
    people = STORE.policyholders
    for p in people:
        var = variants(p)
        for k in range(1, 6):
            for fields in combinations(PII_FIELDS, k):
                # every format variant of every field, rotated so each variant appears at least once
                for i in range(max(len(var[f]) for f in fields)):
                    raw = {f: var[f][i % len(var[f])] for f in fields}
                    got = verify(raw)
                    want = p["party_id"] if k >= 3 else None
                    stats["positive" if k >= 3 else "below_threshold"] += 1
                    if got != want:
                        failures.append(f"{p['party_id']} {raw} -> {got}, expected {want}")
        for fields in combinations(PII_FIELDS, 3):
            for bad in fields:
                raw = {f: (corrupt(f, p) if f == bad else p[{'full_name': 'name', 'id_last4': 'id_last4'}.get(f, f)])
                       for f in fields}
                stats["corrupted"] += 1
                if verify(raw) is not None:
                    failures.append(f"{p['party_id']} corrupted {bad}: {raw} verified")
    key = {"full_name": "name"}
    for a in people:
        for b in people:
            if a is b:
                continue
            for fa in combinations(PII_FIELDS, 2):
                for fb in PII_FIELDS:
                    if fb in fa:
                        continue
                    raw = {f: a[key.get(f, f)] for f in fa} | {fb: b[key.get(fb, fb)]}
                    stats["cross_record"] += 1
                    if verify(raw) is not None:
                        failures.append(f"cross-record {raw} verified")
    return failures, stats


def part_b() -> list[str]:
    failures = []
    ids = {p["party_id"] for p in STORE.policyholders}
    for c in STORE.claims:
        if c["party_id"] not in ids:
            failures.append(f"{c['case_id']} references unknown party")
        facts = STORE.claim_facts(c, config.today(), "how long does it take")
        for d in facts.get("documents", []):
            if not d["requirements"] or not d["if_unavailable"]:
                failures.append(f"{c['case_id']}: no guidance (or fallback) resolved for '{d['document']}'")
        for g in facts.get("followup_guidance", []):
            if re.search(r"\{\w+\}", g["text"]):
                failures.append(f"{c['case_id']}: unfilled template in {g['topic']}")
        if c.get("documents_needed") and not any(g["matches_current_question"] for g in facts["followup_guidance"]):
            failures.append(f"{c['case_id']}: 'how long does it take' matched no follow-up topic")
    for r in STORE.representatives:
        if r["buyer_party_id"] not in ids:
            failures.append(f"rep {r['rep_name']} references unknown party")
        if not STORE.authorized_rep(r["buyer_party_id"], r["rep_name"]):
            failures.append(f"rep {r['rep_name']} not resolvable")
    for name, sc in STORE.consent_scenarios.items():
        if name == "default" and sc["status_sequence"][-1] != "approved":
            failures.append("default consent scenario never approves")
    return failures


# ---------------------------------------------------------------- Part C: live conversations

def opening(p: dict, fields: tuple, hint: str = "") -> str:
    var = variants(p)
    parts = {"full_name": f"my name is {var['full_name'][2 if 'name_aliases' not in p else -1]}",
             "dob": f"date of birth {var['dob'][2]}", "phone": f"phone {var['phone'][1]}",
             "email": f"email {var['email'][0]}",
             "id_last4": f"{'SSN' if p['id_type'] == 'ssn_last4' else 'national ID'} last four {p['id_last4']}"}
    return "Hi, " + ", ".join(parts[f] for f in fields) + (f". {hint}" if hint else ".")


def claim_hint(c: dict) -> str:
    y, m, _ = c["created_at"].split("-")
    return f"I'm calling about my {c['case_type']} claim from {MONTHS[int(m) - 1]} {y}."


TOPIC_QUESTIONS = {
    "missing_required_material_alternatives": ("What if I can't get one of the documents?", ["replacement", "copy", "substitute", "human"]),
    "submission_timing": ("When should I submit the documents?", ["within a week", "a week"]),
    "processing_time_after_submission": ("How long does it take after I submit?", ["less than a week", "week"]),
    "submission_method": ("How do I submit the documents?", ["portal", "upload link"]),
    "file_format_requirements": ("What file format should the scan be, is a pdf ok?", ["clear", "complete", "readable", "legible", "pdf"]),
    "receipt_confirmation": ("How do I know you got it? Will it show up in status?", ["business days", "intake", "delay"]),
}


def part_c_cases() -> list[dict]:
    cases = []
    combos = [("full_name", "dob", "id_last4"), ("full_name", "phone", "email"), ("dob", "email", "id_last4")]
    for p in STORE.policyholders:
        for fields in combos:
            pid = p["party_id"]
            has_claims = bool(STORE.claims_for(pid))
            cases.append(dict(
                name=f"verify_{pid}_{'+'.join(fields)}", category="identity",
                turns=[opening(p, fields, "What's the status of my claim?")],
                expect=lambda s, pid=pid, hc=has_claims: (
                    s.verified and s.party_id == pid and (hc or s.handoff_offered),
                    f"verified as {pid}" + ("" if hc else " and told no claims on file"))))
    for c in STORE.claims:
        p = STORE.party(c["party_id"])
        status_kw = {"denied": [d for d in c.get("documents_needed", [])],
                     "closed": [c["net_pay"], f"{float(c['net_pay']):,.2f}", f"{float(c['net_pay']):,.0f}"],
                     "open": [c["expected_reimbursement_amount"], f"{float(c['expected_reimbursement_amount']):,.0f}"]}[c["status"]]
        cases.append(dict(
            name=f"claim_{c['case_id']}_overview", category="claim_facts",
            turns=[opening(p, ("full_name", "dob", "id_last4"), claim_hint(c)), "Yes, that's the one.",
                   "What's the status, and how much was paid or can I expect?"],
            expect=lambda s, cid=c["case_id"]: (s.active_case_id == cid, f"resolved to {cid}"),
            must_say=[(0, [c["case_id"]]), (2, [c["status"], *(["open", "in progress"] if c["status"] == "open" else [])]),
                      ("any" if c["status"] == "denied" else 2, status_kw)],
            judge=f"Ground-truth check for claim {c['case_id']}: {c}. Agent must state the status and amounts "
                  f"correctly and only from this data."))
        if c["status"] == "denied":
            for topic, (question, kws) in TOPIC_QUESTIONS.items():
                cases.append(dict(
                    name=f"guidance_{c['case_id']}_{topic}", category="guidance",
                    turns=[opening(p, ("full_name", "dob", "id_last4"), claim_hint(c)), "Yes.", question],
                    expect=lambda s, cid=c["case_id"]: (s.active_case_id == cid, f"resolved to {cid}"),
                    must_say=[(2, kws)]))
    m = STORE.party("P9")
    rep_turns = ["Hi, I'm David Chen, calling for my mother Margaret Chen.",
                 f"Her DOB is {m['dob']}, phone {m['phone']}.", "She approved it.", "Check again please.",
                 "Again?", "Anything yet?"]
    cases.append(dict(name="rep_P9_consent_default", category="representative", turns=rep_turns[:4],
                      expect=lambda s: (s.verified and s.consent_status == "approved", "consent approved -> access")))
    cases.append(dict(name="rep_P9_consent_timeout", category="representative", turns=rep_turns, scenario="timeout",
                      expect=lambda s: (not s.verified and s.consent_status == "timeout", "consent timeout -> no access")))
    return cases


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline-only", action="store_true")
    ap.add_argument("--no-judge", action="store_true")
    args = ap.parse_args()

    a_fail, a_stats = part_a()
    b_fail = part_b()
    total_a = sum(a_stats.values())
    print(f"Part A identity matrix: {total_a - len(a_fail)}/{total_a} correct  {a_stats}")
    for f in a_fail[:20]:
        print("   ", f)
    print(f"Part B fixture integrity: {'OK' if not b_fail else b_fail}")
    md = (f"## Part A — identity matrix (offline)\n\n**{total_a - len(a_fail)}/{total_a} correct** — "
          + ", ".join(f"{k}: {v}" for k, v in a_stats.items())
          + ("\n\nFailures:\n" + "\n".join(f"- {f}" for f in a_fail[:50]) if a_fail else "")
          + f"\n\n## Part B — fixture integrity (offline)\n\n{'All checks passed.' if not b_fail else chr(10).join('- ' + f for f in b_fail)}"
          + "\n\n## Part C — live conversations over the dataset\n")
    if args.offline_only:
        return
    cases = part_c_cases()

    def go(c):
        return run_scripted(c["name"], c["category"], c["turns"], expect=c.get("expect"),
                            scenario=c.get("scenario", "default"), must_say=c.get("must_say"),
                            judge_brief=None if args.no_judge else c.get("judge"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(go, cases))
    for r in results:
        if not r.passed:
            print("FAIL", r.name, {k: d for k, (p, d) in r.checks.items() if not p},
                  r.judge.get("issues") if r.judge else "", r.error.splitlines()[0] if r.error else "")
    by_cat = {}
    for r in results:
        by_cat.setdefault(r.category, []).append(r.passed)
    md += "\n| category | passed |\n|---|---|\n" + "\n".join(f"| {k} | {sum(v)}/{len(v)} |" for k, v in sorted(by_cat.items()))
    path = write_report("Dataset validation", results, "dataset", md)
    print(f"Part C live: {sum(r.passed for r in results)}/{len(results)} passed. Report: {path}")


if __name__ == "__main__":
    main()
