#!/usr/bin/env python3
"""Step 3: compute the main results of RQ1-RQ3 from the 486 labeled token inefficiency (TI) bugs.

Reads data/ti_bugs.csv (one row per TI bug) and prints the counts behind the paper's findings. Standard library only.

usage: python3 code/step3_results.py [--json]
"""
from __future__ import annotations

import csv
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "ti_bugs.csv"
CHECKS_OF_INPUT = {"request", "prompt cache", "token count"}


def cramers_v(pairs):
    """Cramer's V of two categorical variables given as (a, b) pairs."""
    n = len(pairs)
    a, b, ab = Counter(x for x, _ in pairs), Counter(y for _, y in pairs), Counter(pairs)
    chi2 = sum((ab[(x, y)] - a[x] * b[y] / n) ** 2 / (a[x] * b[y] / n) for x in a for y in b)
    return math.sqrt(chi2 / (n * (min(len(a), len(b)) - 1)))


def pct(k, n):
    return f"{k} ({round(100 * k / n)}%) of {n}"


def main():
    rows = list(csv.DictReader(open(DATA, encoding="utf-8")))
    for r in rows:
        r["checks"] = set(r["test_checks"].split(";"))
    n = len(rows)
    out, lines = {"n": n}, [f"TI bugs: {n} in {len({r['repo'] for r in rows})} harnesses"]

    # ---- RQ1: types, root causes, call sites
    out["types"] = dict(Counter(r["type"] for r in rows).most_common())
    out["root_causes"] = dict(Counter(r["root_cause"] for r in rows).most_common())
    by_type = defaultdict(Counter)
    for r in rows:
        by_type[r["root_cause"]][r["type"]] += 1
    out["root_cause_by_type"] = {k: dict(v) for k, v in by_type.items()}
    out["stop_condition_by_call_site"] = {}
    for site in ("main loop", "other call site"):
        at = [r for r in rows if r["call_site"] == site]
        k = sum(r["root_cause"] == "faulty stop condition" for r in at)
        out["stop_condition_by_call_site"][site] = [k, len(at)]
    lines += ["", "RQ1  Types and Root Causes",
              "  types: " + ", ".join(f"{k} {v}" for k, v in out["types"].items()),
              "  root causes (call-control / call-content):"]
    lines += [f"    {k:32s} {v:4d}  ({by_type[k]['call-control']} / {by_type[k]['call-content']})"
              for k, v in out["root_causes"].items()]
    m, o = out["stop_condition_by_call_site"]["main loop"], out["stop_condition_by_call_site"]["other call site"]
    lines.append(f"  faulty stop conditions cause {pct(*m)} bugs in the main loop but {pct(*o)} at other call sites")

    # ---- RQ2: resources, symptoms, how developers noticed the inefficiency
    out["resources"] = dict(Counter(r["resource"] for r in rows).most_common())
    out["symptoms"] = {res: dict(Counter(r["symptom"] for r in rows if r["resource"] == res).most_common())
                       for res in out["resources"]}
    calls = [r for r in rows if r["resource"] == "Model calls"]
    inputs = [r for r in rows if r["resource"] == "Input tokens"]
    repeat = sum(r["symptom"] in ("Repeated actions", "Repeated model requests") for r in calls)
    too_much = sum(r["symptom"] in ("Oversized tool results", "Context bloat", "Over-limit context") for r in inputs)
    out["noticed_by"] = {res: dict(Counter(r["noticed_by"] for r in rows if r["resource"] == res).most_common())
                         for res in out["resources"]}
    out["noticed_in_runtime_behavior"] = {
        res: [sum(r["noticed_by"] == "runtime behavior" for r in grp), len(grp)]
        for res, grp in (("Model calls", calls), ("Input tokens", inputs))}
    causes_per_symptom = {s: len({r["root_cause"] for r in rows if r["symptom"] == s}) for s in {r["symptom"] for r in rows}}
    out["symptoms_with_several_root_causes"] = [sum(v > 1 for v in causes_per_symptom.values()), len(causes_per_symptom)]
    out["cramers_v_symptom_root_cause"] = round(cramers_v([(r["symptom"], r["root_cause"]) for r in rows]), 2)
    lines += ["", "RQ2  Symptoms",
              "  resources: " + ", ".join(f"{k} {pct(v, n)}" for k, v in out["resources"].items())]
    for res, s in out["symptoms"].items():
        lines.append(f"    {res}: " + ", ".join(f"{k} {v}" for k, v in s.items()))
    lines += [f"  bugs that cause unnecessary model calls and repeat work (repeated actions or requests): {pct(repeat, len(calls))}",
              f"  bugs that cause unnecessary input tokens and send too much content: {pct(too_much, len(inputs))}; "
              f"prompt cache misses: {len(inputs) - too_much}",
              "  noticed in the runtime behavior: model calls "
              f"{pct(*out['noticed_in_runtime_behavior']['Model calls'])}, input tokens "
              f"{pct(*out['noticed_in_runtime_behavior']['Input tokens'])}",
              f"  symptoms that come from more than one root cause: {out['symptoms_with_several_root_causes'][0]} of "
              f"{out['symptoms_with_several_root_causes'][1]}; Cramer's V (symptom, root cause) = "
              f"{out['cramers_v_symptom_root_cause']:.2f}"]

    # ---- RQ3: fix strategies, fix size, what the tests check
    strat = defaultdict(Counter)
    for r in rows:
        strat[r["root_cause"]][r["fix_strategy"]] += 1
    main_of = {rc: c.most_common(1)[0] for rc, c in strat.items()}
    out["main_fix_strategy"] = {rc: [s, k, sum(strat[rc].values())] for rc, (s, k) in main_of.items()}
    out["main_fix_strategy_of_root_cause"] = [sum(r["fix_strategy"] == main_of[r["root_cause"]][0] for r in rows), n]
    out["cramers_v_root_cause_strategy"] = round(cramers_v([(r["root_cause"], r["fix_strategy"]) for r in rows]), 2)
    out["cramers_v_symptom_strategy"] = round(cramers_v([(r["symptom"], r["fix_strategy"]) for r in rows]), 2)
    fl, ff = [int(r["fix_lines"]) for r in rows], [int(r["fix_files"]) for r in rows]
    out["fix_size"] = {"median_lines": statistics.median(fl), "median_files": statistics.median(ff),
                       "one_file": sum(x == 1 for x in ff)}
    out["call_bug_fixes_with_call_count_test"] = [sum("call count" in r["checks"] for r in calls), len(calls)]
    out["call_count_test_by_type"] = {t: [sum("call count" in r["checks"] for r in calls if r["type"] == t),
                                          sum(r["type"] == t for r in calls)] for t in ("call-control", "call-content")}
    out["input_bug_fixes_checking_input"] = [sum(bool(r["checks"] & CHECKS_OF_INPUT) for r in inputs), len(inputs)]
    out["fixes_without_test_change"] = sum("no test change" in r["checks"] for r in rows)
    out["fixes_checking_no_calls_or_input"] = sum(not (r["checks"] & (CHECKS_OF_INPUT | {"call count"})) for r in rows)
    lines += ["", "RQ3  Fixing Strategies", "  main fix strategy of each root cause:"]
    lines += [f"    {rc:32s} {s} ({k} of {t})" for rc, (s, k, t) in out["main_fix_strategy"].items()]
    lines += [f"  fixes that use the main strategy of their root cause: {pct(*out['main_fix_strategy_of_root_cause'])}",
              f"  Cramer's V: root cause and fix strategy {out['cramers_v_root_cause_strategy']:.2f}, symptom and fix "
              f"strategy {out['cramers_v_symptom_strategy']:.2f}",
              f"  fix size (tests and documentation excluded): median {out['fix_size']['median_lines']:g} lines in "
              f"{out['fix_size']['median_files']:g} files; {pct(out['fix_size']['one_file'], n)} change one file",
              f"  fixes of bugs that cause unnecessary model calls with a test that counts calls: "
              f"{pct(*out['call_bug_fixes_with_call_count_test'])} (call-control "
              f"{out['call_count_test_by_type']['call-control'][0]} of {out['call_count_test_by_type']['call-control'][1]}, "
              f"call-content {out['call_count_test_by_type']['call-content'][0]} of "
              f"{out['call_count_test_by_type']['call-content'][1]})",
              f"  fixes of bugs that cause unnecessary input tokens with a test of the request, the cache or a token "
              f"count: {pct(*out['input_bug_fixes_checking_input'])}",
              f"  fixes with no test that checks the calls, the request, the cache or a token count: "
              f"{pct(out['fixes_checking_no_calls_or_input'], n)}; of these, {out['fixes_without_test_change']} change "
              f"no test"]
    print(json.dumps(out, indent=1) if "--json" in sys.argv else "\n".join(lines))


if __name__ == "__main__":
    main()
