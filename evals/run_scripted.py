"""Run the scripted edge cases. Usage: python evals/run_scripted.py [--no-judge] [name ...]"""
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.common import run_scripted, write_report  # noqa: E402
from evals.scripted import CASES  # noqa: E402


def main(argv):
    use_judge = "--no-judge" not in argv
    names = [a for a in argv if not a.startswith("--")]
    cases = [c for c in CASES if not names or c["name"] in names]

    def go(c):
        return run_scripted(c["name"], c["category"], c["turns"], expect=c.get("expect"),
                            scenario=c.get("scenario", "default"), must_say=c.get("must_say"),
                            judge_brief=c.get("judge") if use_judge else None)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(go, cases))
    for r in results:
        print(("PASS " if r.passed else "FAIL ") + r.name)
        if not r.passed:
            for k, (p, d) in r.checks.items():
                if not p:
                    print(f"    {k}: {d}")
            if r.judge and not r.judge.get("pass"):
                print(f"    judge: {r.judge.get('issues')}")
            if r.error:
                print("    error:", r.error.splitlines()[0])
    path = write_report("Scripted edge cases", results, "scripted")
    print(f"\n{sum(r.passed for r in results)}/{len(results)} passed. Report: {path}")


if __name__ == "__main__":
    main(sys.argv[1:])
