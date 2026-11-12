"""Run the dataset validation conversations (Part C) against a deployed instance through its web API,
exactly as a browser would: fresh session, the visitor's key (OPENROUTER_API_KEY) in the key field.

Usage: python evals/run_on_website.py [URL]   (default: the Render deployment)
"""
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradio_client import Client  # noqa: E402

from evals.common import Result, check_grounded_numbers, check_no_leak, write_report  # noqa: E402
from sop_agent import config  # noqa: E402
from evals.dataset_validation import part_c_cases  # noqa: E402

URL = sys.argv[1] if len(sys.argv) > 1 else "https://insurancebot-w6cl.onrender.com/"


def _text(content) -> str:
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return str(content)


def run_case(case: dict) -> Result:
    result = Result(case["name"], case["category"])
    try:
        client = Client(URL, verbose=False)
        _, state, _ = client.predict(case.get("scenario", "default"), api_name="/new_session")
        for turn in case["turns"]:
            verified_before = bool(state.get("verified"))
            result.transcript.append(("caller", turn, state.get("phase"), verified_before))
            if state.get("phase") == "HUMAN_HANDOFF":
                break
            # like a visitor: their own key in the 🔑 field, model field left empty
            _, conv, state, _ = client.predict(turn, config.api_key(), None, api_name="/chat")
            reply = _text(conv[-1]["content"])
            if reply.startswith("⚠️"):
                raise RuntimeError(f"app returned an error message: {reply[:200]}")
            result.transcript.append(("agent", reply, state["phase"], bool(state["verified"])))
        s = SimpleNamespace(**state)
        result.state = state
        result.checks["no_leak_before_verification"] = check_no_leak(result.transcript)
        result.checks["grounded_numbers"] = check_grounded_numbers(result.transcript, s.party_id if s.verified else None)
        if case.get("expect"):
            ok, detail = case["expect"](s)
            result.checks["expected_outcome"] = (bool(ok), detail)
        replies = [t[1] for t in result.transcript if t[0] == "agent"]
        for idx, alternatives in case.get("must_say") or []:
            text = " ".join(replies) if idx == "any" else (replies[idx] if idx < len(replies) else "")
            result.checks[f"reply{idx}_mentions:{alternatives[0][:20]}"] = (
                any(a.lower() in text.lower() for a in alternatives), f"expected one of {alternatives}")
    except Exception as e:
        result.error = f"{e}\n{traceback.format_exc()[-600:]}"
    return result


def main():
    cases = part_c_cases()
    print(f"Running {len(cases)} dataset conversations against {URL}", flush=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run_case, cases))
    for r in results:
        print(("PASS " if r.passed else "FAIL ") + r.name)
        if not r.passed:
            for k, (p, d) in r.checks.items():
                if not p:
                    print(f"    {k}: {d}")
            if r.error:
                print("    error:", r.error.splitlines()[0])
    by_cat = {}
    for r in results:
        by_cat.setdefault(r.category, []).append(r.passed)
    md = f"Target: {URL} (browser-style calls, settings fields empty)\n\n| category | passed |\n|---|---|\n" + \
         "\n".join(f"| {k} | {sum(v)}/{len(v)} |" for k, v in sorted(by_cat.items()))
    path = write_report("Dataset validation on the deployed website", results, "website", md)
    print(f"\n{sum(r.passed for r in results)}/{len(results)} passed. Report: {path}")


if __name__ == "__main__":
    main()
