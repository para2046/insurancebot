"""Run LLM-simulated callers. Usage: python evals/run_generated.py [--n 40] [--seed 7] [--no-judge] [--only substr]"""
import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.common import write_report  # noqa: E402
from evals.generated import generate, run_generated  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    cases = [c for c in generate(args.n, args.seed) if args.only in c.name]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda c: run_generated(c, not args.no_judge), cases))
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
    by_cat = {}
    for r in results:
        by_cat.setdefault(r.category, []).append(r.passed)
    summary = "| category | passed |\n|---|---|\n" + "\n".join(
        f"| {k} | {sum(v)}/{len(v)} |" for k, v in sorted(by_cat.items()))
    path = write_report(f"Generated caller simulations (seed {args.seed})", results, f"generated-s{args.seed}", summary)
    print(f"\n{sum(r.passed for r in results)}/{len(results)} passed. Report: {path}")


if __name__ == "__main__":
    main()
