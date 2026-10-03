#!/usr/bin/env python3
"""Step 2: judge the merged PRs of Step 1, as the basis for selecting the candidate cases (Section 3.1).

Two steps, as in the paper:
1. A rule filter keeps a PR when the text of the PR and of the issues it closes mentions both token usage (words such
   as "token" and "context") and inefficiency (words such as "repeat" and "unnecessary").
2. An LLM classifier, called through its API, answers three questions about each remaining PR: whether the PR fixes a
   bug, whether the same task uses fewer tokens after the fix, and whether the PR changes the code, prompts or settings
   of the harness. For each PR it gives the probability that each answer is yes, with a one-sentence reason.

The paper's 872 candidate cases were selected based on these judgments; the script records the judgments and does not
select. An optional threshold in config.yaml only marks the PRs whose three probabilities all reach it, to help screening.

The LLM is set in config.yaml (section llm): the API format (anthropic: Messages API; openai: Chat Completions API), an
optional base_url for any endpoint that speaks that format, the model, the environment variable that holds the API key,
temperature, max_tokens and the optional threshold. The default is Claude Opus-5 through the Anthropic API (key in
ANTHROPIC_API_KEY). The candidate cases were then labeled with the codebook of Section 3.2; data/ti_bugs.csv holds the 486
TI bugs that this labeling identified.

usage: python3 code/step2_classify.py [--config code/config.yaml] [--limit N]
       -> work/rule_filter.csv, work/classifier_scores.csv   (--limit: judge only the first N PRs, e.g. to test a setup)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from pathlib import Path

import requests
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


def view(pr):
    """The text the classifier reads: the issues that the PR closes, then the PR itself, in full."""
    parts = [f"=== ISSUE #{i['number']}: {i.get('title')}\n{(i.get('body') or '').strip()}" for i in pr["issues"]]
    parts.append(f"=== MERGED PULL REQUEST #{pr['number']}: {pr.get('title')}\n{(pr.get('body') or '').strip()}")
    return "\n\n".join(parts)


DEFAULT_BASE_URL = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com/v1"}


def ask(llm, msg):
    """One request to the configured LLM; returns the text of its reply."""
    api = llm.get("api", "anthropic")
    key = os.environ[llm["api_key_env"]]
    base = (llm.get("base_url") or DEFAULT_BASE_URL[api]).rstrip("/")
    body = {"model": llm["model"], "max_tokens": llm.get("max_tokens", 300), "temperature": llm.get("temperature", 0),
            "messages": [{"role": "user", "content": msg}]}
    if api == "anthropic":
        r = requests.post(f"{base}/v1/messages", json=body, timeout=llm.get("timeout", 300),
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
    if api == "openai":
        r = requests.post(f"{base}/chat/completions", json=body, timeout=llm.get("timeout", 300),
                          headers={"Authorization": f"Bearer {key}"})
        r.raise_for_status()
        return r.json()["choices"][0]["message"].get("content") or ""
    raise ValueError(f"llm.api must be anthropic or openai, not {api!r}")


def classify(llm, pr):
    msg = PROMPT.format(repo=pr["repo"], view=view(pr))
    for attempt in range(5):
        try:
            text = ask(llm, msg)
            return json.loads(text[text.index("{"): text.rindex("}") + 1])
        except Exception as e:  # rate limit, server error or unparsable reply: wait and retry
            last = e
            time.sleep(20 * (attempt + 1))
    raise RuntimeError(f"{pr['repo']}#{pr['number']}: {last}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "code/config.yaml"))
    ap.add_argument("--limit", type=int, default=None, help="judge only the first N PRs that pass the rule filter")
    args = ap.parse_args()
    cfg = yaml.safe_load(open(args.config))
    work = ROOT / "work"
    prs = [json.loads(x) for x in open(work / "merged_prs.jsonl") if x.strip()]

    kept = [pr for pr in prs if rule_filter(pr)]
    with open(work / "rule_filter.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["repo", "pr", "passes"])
        w.writerows([pr["repo"], pr["number"], rule_filter(pr)] for pr in prs)
    print(f"rule filter: {len(kept)} of {len(prs)} merged PRs pass", flush=True)

    llm = cfg["llm"]
    if llm["api_key_env"] not in os.environ:
        raise SystemExit(f"set the API key in the environment variable {llm['api_key_env']} (llm.api_key_env in the config)")
    th = llm.get("threshold")  # optional; only marks PRs to help screening
    if args.limit is not None:
        kept = kept[:args.limit]
    qs = ("fix", "fewer_tokens", "harness_change")
    n = 0
    with open(work / "classifier_scores.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["repo", "pr", "pr_url", *qs, "reason"] + (["reaches_threshold"] if th is not None else []))
        for pr in kept:
            a = classify(llm, pr)
            row = [pr["repo"], pr["number"], f"https://github.com/{pr['repo']}/pull/{pr['number']}",
                   *(a.get(k) for k in qs), a.get("reason", "")]
            if th is not None:
                mark = min(float(a.get(k) or 0) for k in qs) >= th
                n += mark
                row.append(mark)
            w.writerow(row)
    print(f"LLM classifier ({llm['model']}): judged {len(kept)} PRs"
          + (f"; {n} reach the threshold {th} on all three questions" if th is not None else ""), flush=True)


if __name__ == "__main__":
    main()
