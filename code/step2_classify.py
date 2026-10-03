#!/usr/bin/env python3
"""Step 2: select the candidate cases among the merged PRs of Step 1 (Section 3.1).

Two filters, as in the paper:
1. A rule filter keeps a PR when the text of the PR and of the issues it closes mentions both token usage (words such
   as "token" and "context") and inefficiency (words such as "repeat" and "unnecessary").
2. An LLM classifier, called through its API, answers three questions about each remaining PR: whether the PR fixes a
   bug, whether the same task uses fewer tokens after the fix, and whether the PR changes the code, prompts or settings
   of the harness. A PR becomes a candidate case when every answer is likely (each probability at least the threshold
   in config.yaml).

The default configuration calls Claude Opus-5 through the Anthropic API; set the API key in the environment variable named
in config.yaml (default ANTHROPIC_API_KEY). The candidate cases were then labeled with the codebook of Section 3.2;
data/ti_bugs.csv holds the 486 TI bugs that this labeling identified.

usage: python3 code/step2_classify.py [--config code/config.yaml]   ->  work/rule_filter.csv, work/candidates.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

# ---- Filter 1: token usage and inefficiency words (whole words, with their inflections)
USAGE = [r"tokens?", r"prompts?", r"contexts?", r"model calls?", r"api calls?", r"completions?", r"inference",
         r"prefill(?:s|ed|ing)?", r"cach(?:e|es|ed|ing)", r"histor(?:y|ies)", r"(?:llm|model|api) (?:call|calls|request|requests)"]
INEFFICIENCY = [r"repeat(?:s|ed|ing|edly)?", r"repetitive", r"repetition", r"duplicat(?:e|es|ed|ing|ion)", r"retry",
                r"retries", r"retried", r"retrying", r"loop(?:s|ed|ing)?", r"unnecessar(?:y|ily)", r"re-?sen(?:d|ds|ding|t)",
                r"replay(?:s|ed|ing)?", r"keeps? calling", r"fail(?:s|ed)? to stop", r"invalidat[a-z]*",
                r"consum(?:e|es|ed|ing|ption)", r"wast(?:e|es|ed|ing|eful)", r"excess(?:ive|ively)?",
                r"redundan(?:t|tly|cy)", r"unbounded", r"bloat[a-z]*", r"explo(?:de|des|ded|ding|sion)",
                r"burn(?:s|ed|t|ing)?", r"spik(?:e|es|ed|ing)", r"too (?:many|much)", r"dedup[a-z]*"]


def words(terms):
    return re.compile(r"(?<![a-z])(?:" + "|".join(terms) + r")(?![a-z])")


USAGE_RE, INEFFICIENCY_RE = words(USAGE), words(INEFFICIENCY)


def text_of(pr):
    parts = [pr.get("title") or "", pr.get("body") or ""]
    for i in pr["issues"]:
        parts += [i.get("title") or "", i.get("body") or ""]
    return re.sub(r"\s+", " ", " ".join(parts)).lower()


def rule_filter(pr):
    t = text_of(pr)
    return bool(USAGE_RE.search(t)) and bool(INEFFICIENCY_RE.search(t))


# ---- Filter 2: the LLM classifier
PROMPT = """You are helping with an empirical study of bugs in LLM agent harnesses. An agent harness is the software that \
runs an agent: it calls the language model, executes the actions that the model proposes, and keeps the conversation. \
Model providers bill every model call by the tokens that the model reads (input tokens) and generates (output tokens).

Below is a merged pull request (PR) from the repository {repo}, together with the issues that the PR closes. Answer three \
questions about the PR. For each question, give the probability (between 0 and 1) that the answer is yes.

1. fix: Does the PR fix a bug, that is, behavior that the developers consider wrong (not a new feature, a refactoring \
or a documentation change)?
2. fewer_tokens: After the fix, does the same task use fewer tokens than before, because the harness makes fewer model \
calls, or because the model reads or generates fewer tokens?
3. harness_change: Does the PR change the code, the prompts or the settings of the harness itself (not only its tests, \
documentation or build files)?

Reply with JSON only, in this form:
{{"fix": 0.0, "fewer_tokens": 0.0, "harness_change": 0.0, "reason": "one sentence"}}

{view}"""

CAPS = {"issue_body": 5000, "issues": 9000, "pr_body": 8000}


def cap(s, n):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + " [...]"


def view(pr):
    parts = []
    issues = "\n\n".join(f"=== ISSUE #{i['number']}: {i.get('title')}\n{cap(i.get('body'), CAPS['issue_body'])}"
                         for i in pr["issues"])
    if issues:
        parts.append(cap(issues, CAPS["issues"]))
    parts.append(f"=== MERGED PULL REQUEST #{pr['number']}: {pr.get('title')}\n{cap(pr.get('body'), CAPS['pr_body'])}")
    return "\n\n".join(parts)


def classify(client, cfg, pr):
    msg = PROMPT.format(repo=pr["repo"], view=view(pr))
    for attempt in range(5):
        try:
            r = client.messages.create(model=cfg["model"], max_tokens=cfg.get("max_tokens", 300),
                                       temperature=cfg.get("temperature", 0),
                                       messages=[{"role": "user", "content": msg}])
            text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
            return json.loads(text[text.index("{"): text.rindex("}") + 1])
        except Exception as e:  # rate limit, server error or unparsable reply: wait and retry
            last = e
            time.sleep(20 * (attempt + 1))
    raise RuntimeError(f"{pr['repo']}#{pr['number']}: {last}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "code/config.yaml"))
    cfg = yaml.safe_load(open(ap.parse_args().config))
    work = ROOT / "work"
    prs = [json.loads(x) for x in open(work / "merged_prs.jsonl") if x.strip()]

    kept = [pr for pr in prs if rule_filter(pr)]
    with open(work / "rule_filter.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["repo", "pr", "passes"])
        w.writerows([pr["repo"], pr["number"], rule_filter(pr)] for pr in prs)
    print(f"rule filter: {len(kept)} of {len(prs)} merged PRs pass", flush=True)

    import anthropic  # imported here so that the rule filter runs without the SDK
    llm = cfg["llm"]
    client = anthropic.Anthropic(api_key=os.environ[llm["api_key_env"]])
    th = llm["threshold"]
    n = 0
    with open(work / "candidates.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["repo", "pr", "pr_url", "fix", "fewer_tokens", "harness_change", "candidate", "reason"])
        for pr in kept:
            a = classify(client, llm, pr)
            cand = min(float(a.get(k, 0)) for k in ("fix", "fewer_tokens", "harness_change")) >= th
            n += cand
            w.writerow([pr["repo"], pr["number"], f"https://github.com/{pr['repo']}/pull/{pr['number']}",
                        a.get("fix"), a.get("fewer_tokens"), a.get("harness_change"), cand, a.get("reason", "")])
    print(f"LLM classifier: {n} of {len(kept)} PRs are candidate cases", flush=True)


if __name__ == "__main__":
    main()
