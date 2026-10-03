#!/usr/bin/env python3
"""Step 1: collect the merged pull requests (PRs) that may fix token inefficiency (TI) bugs (Section 3.1).

For each of the 50 harness repositories, the script searches the titles and bodies of the issues and PRs created up to the
cutoff date for each keyword (one keyword per query; issues and PRs separately), keeps the records that match at least one
keyword of high or medium precision, and then keeps the PRs among them that were merged and are not reverts. For each kept
PR it records the title, body and the issues that the PR closes (GitHub closingIssuesReferences), whose text Step 2 reads.
It also records the number of issues and PRs of each repository up to the cutoff (Table 1).

Needs a GitHub token in the environment variable named in config.yaml (default GITHUB_TOKEN). GitHub search counts drift
over time, and the merged state is read at query time, so a new run gives counts close to, not equal to, the paper's.

usage: python3 code/step1_collect.py [--config code/config.yaml]
       -> work/repo_counts.csv, work/keyword_matches.csv, work/merged_prs.jsonl
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"

# The 50 harness repositories (names at the cutoff date; Section 3.1, Step 1).
REPOS = [
    'openclaw/openclaw', 'NousResearch/hermes-agent', 'anomalyco/opencode', 'Significant-Gravitas/AutoGPT',
    'browser-use/browser-use', 'google-gemini/gemini-cli', 'openai/codex', 'OpenHands/OpenHands',
    'earendil-works/pi', 'bytedance/deer-flow', 'FoundationAgents/MetaGPT', 'openinterpreter/openinterpreter',
    'cline/cline', 'FoundationAgents/OpenManus', 'AntonOsika/gpt-engineer', 'aaif-goose/goose', 'Aider-AI/aider',
    'HKUDS/nanobot', 'zhayujie/CowAgent', 'Hmbown/Codewhale', 'bytedance/UI-TARS-desktop', 'AstrBotDevs/AstrBot',
    'reworkd/AgentGPT', 'khoj-ai/khoj', 'tinyhumansai/openhuman', 'continuedev/continue', 'Pythagora-io/gpt-pilot',
    'zeroclaw-labs/zeroclaw', 'agentscope-ai/QwenPaw', 'Gitlawb/openclaude', 'nanocoai/nanoclaw',
    'sipeed/picoclaw', 'esengine/DeepSeek-Reasonix', 'charmbracelet/crush', 'Fosowl/agenticSeek',
    'Kilo-Org/kilocode', 'QwenLM/qwen-code', 'zai-org/Open-AutoGLM', 'RooCodeInc/Roo-Code', 'Skyvern-AI/skyvern',
    'can1357/oh-my-pi', 'SWE-agent/SWE-agent', 'stitionai/devika', 'elizaOS/eliza', 'agent0ai/agent-zero',
    'avante-corp/avante.nvim', 'RightNow-AI/openfang', 'TransformerOptimus/SuperAGI', 'leon-ai/leon',
    'rowboatlabs/rowboat',
]

# Keywords with their precision class; a record found only by BROAD keywords is dropped (Section 3.1, Step 2).
KEYWORDS = {
    'token usage': 'HIGH', 'token count': 'HIGH', 'token counting': 'HIGH', 'tokens used': 'HIGH',
    'input tokens': 'HIGH', 'output tokens': 'HIGH', 'cached tokens': 'HIGH', 'usage tokens': 'HIGH',
    'token budget': 'HIGH', 'token cost': 'HIGH', 'token consumption': 'HIGH', 'token accounting': 'HIGH',
    'overcount': 'MEDIUM', 'undercount': 'MEDIUM', 'excessive tokens': 'HIGH', 'context window': 'MEDIUM',
    'context length': 'MEDIUM', 'context limit': 'MEDIUM', 'context overflow': 'HIGH', 'context too large': 'HIGH',
    'too much context': 'HIGH', 'prompt too long': 'HIGH', 'conversation too long': 'HIGH',
    'history too large': 'HIGH', 'context exceeded': 'HIGH', 'maximum context': 'MEDIUM', 'compaction': 'MEDIUM',
    'compact context': 'HIGH', 'auto compact': 'MEDIUM', 'truncate': 'BROAD', 'truncation': 'MEDIUM',
    'prune': 'BROAD', 'pruning': 'MEDIUM', 'summarize history': 'HIGH', 'summarization': 'MEDIUM',
    'conversation summary': 'HIGH', 'history compression': 'HIGH', 'context compression': 'HIGH',
    'prompt cache': 'HIGH', 'prompt caching': 'HIGH', 'cache hit': 'MEDIUM', 'cache miss': 'MEDIUM',
    'prefix cache': 'HIGH', 'prefix caching': 'HIGH', 'cache invalidation': 'HIGH', 'cache reuse': 'HIGH',
    'duplicate request': 'HIGH', 'duplicate call': 'HIGH', 'repeated request': 'HIGH', 'repeated call': 'MEDIUM',
    'redundant call': 'HIGH', 'unnecessary call': 'HIGH', 'extra call': 'MEDIUM', 'extra request': 'MEDIUM',
    'reprocess': 'MEDIUM', 'reprocessing': 'HIGH', 'retry': 'BROAD', 'retries': 'BROAD', 'retry loop': 'MEDIUM',
    'infinite loop': 'MEDIUM', 'tool loop': 'MEDIUM', 'repeated tool': 'MEDIUM', 'max iterations': 'MEDIUM',
    'iteration limit': 'MEDIUM', 'polling': 'BROAD', 'wait loop': 'MEDIUM', 'does not stop': 'MEDIUM',
    'never stops': 'MEDIUM', 'tool output': 'MEDIUM', 'tool result': 'MEDIUM', 'large output': 'MEDIUM',
    'large tool output': 'HIGH', 'huge output': 'MEDIUM', 'truncate tool': 'MEDIUM',
    'tool output truncation': 'HIGH', 'tool result truncation': 'HIGH', 'output too large': 'HIGH',
    'fallback model': 'HIGH', 'model fallback': 'HIGH', 'model routing': 'HIGH', 'routing model': 'MEDIUM',
    'model selection': 'MEDIUM', 'extra model call': 'HIGH', 'additional model call': 'HIGH',
    'multiple model calls': 'HIGH', 'unnecessary model': 'MEDIUM', 'subagent': 'MEDIUM', 'sub-agent': 'MEDIUM',
    'subagents': 'MEDIUM', 'sub-agents': 'MEDIUM', 'spawn agent': 'MEDIUM', 'nested agent': 'MEDIUM',
    'child agent': 'MEDIUM', 'parallel agents': 'MEDIUM', 'agent recursion': 'HIGH', 'budget exceeded': 'HIGH',
    'quota': 'BROAD', 'rate limit': 'BROAD', 'too expensive': 'MEDIUM', 'expensive': 'BROAD', 'cost spike': 'HIGH',
    'usage spike': 'HIGH', 'token spike': 'HIGH', 'wasted tokens': 'HIGH', 'token bloat': 'HIGH',
    'context bloat': 'HIGH', 'million tokens': 'HIGH', 'millions of tokens': 'HIGH', 'tokens later': 'HIGH',
    'tokens burned': 'HIGH', 'tokens burnt': 'HIGH', 'tokens wasted': 'HIGH',
}

ISSUE_FIELDS = "number title body"
PR_QUERY = """query($o: String!, $n: String!, $num: Int!) {
  repository(owner: $o, name: $n) { pullRequest(number: $num) {
    number title body state merged mergedAt
    closingIssuesReferences(first: 5) { nodes { %s repository { nameWithOwner } } } } } }""" % ISSUE_FIELDS


class GitHub:
    def __init__(self, token):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})

    def get(self, url, **params):
        for attempt in range(6):
            r = self.s.get(url, params=params, timeout=60)
            if r.status_code in (403, 429) or r.status_code >= 500:  # rate limit or server error: wait and retry
                time.sleep(min(60 * (attempt + 1), int(r.headers.get("Retry-After", 60))))
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()

    def graphql(self, query, **variables):
        for attempt in range(6):
            r = self.s.post(f"{API}/graphql", json={"query": query, "variables": variables}, timeout=60)
            if r.status_code == 200 and "errors" not in r.json():
                return r.json()["data"]
            time.sleep(30 * (attempt + 1))
        raise RuntimeError(r.text[:500])

    def search(self, q):
        """All items of a search query (the Search API returns at most 1,000 per query)."""
        items, page = [], 1
        while True:
            j = self.get(f"{API}/search/issues", q=q, per_page=100, page=page)
            items += j.get("items", [])
            if len(j.get("items", [])) < 100 or len(items) >= min(j.get("total_count", 0), 1000):
                return items, j.get("total_count", 0)
            page += 1
            time.sleep(2)  # the Search API allows 30 requests per minute


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "code/config.yaml"))
    cfg = yaml.safe_load(open(ap.parse_args().config))
    gh = GitHub(os.environ[cfg["github"]["token_env"]])
    cutoff = cfg["cutoff_date"]
    work = ROOT / "work"
    work.mkdir(exist_ok=True)

    with open(work / "repo_counts.csv", "w", newline="") as f:  # issues and PRs per repository (Table 1)
        w = csv.writer(f)
        w.writerow(["repo", "issues", "prs"])
        for repo in REPOS:
            n = [gh.search(f"repo:{repo} is:{t} created:<={cutoff}")[1] for t in ("issue", "pr")]
            w.writerow([repo, *n])

    with open(work / "merged_prs.jsonl", "w") as out, open(work / "keyword_matches.csv", "w", newline="") as fm:
        wm = csv.writer(fm)
        wm.writerow(["repo", "matched_issues", "matched_prs"])
        for repo in REPOS:
            hits = {"issue": {}, "pr": {}}  # number -> precision classes of the keywords that found it
            for kw, tier in KEYWORDS.items():
                for t in ("issue", "pr"):
                    items, _ = gh.search(f'"{kw}" repo:{repo} is:{t} created:<={cutoff} in:title,body')
                    for it in items:
                        hits[t].setdefault(it["number"], set()).add(tier)
            kept = {t: sorted(n for n, tiers in hits[t].items() if tiers - {"BROAD"}) for t in hits}
            wm.writerow([repo, len(kept["issue"]), len(kept["pr"])])
            # from here the unit is a PR; issues return only as the issues that a kept PR closes
            owner, name = repo.split("/")
            for num in kept["pr"]:
                pr = gh.graphql(PR_QUERY, o=owner, n=name, num=num)["repository"]["pullRequest"]
                if not pr["merged"] or (pr["title"] or "").lower().startswith("revert"):
                    continue
                issues = [i for i in pr["closingIssuesReferences"]["nodes"] if i["repository"]["nameWithOwner"] == repo]
                out.write(json.dumps({"repo": repo, "number": num, "title": pr["title"], "body": pr["body"],
                                      "merged_at": pr["mergedAt"],
                                      "issues": [{k: i[k] for k in ("number", "title", "body")} for i in issues]})
                          + "\n")
            print(f"{repo}: {len(kept['issue'])} issues and {len(kept['pr'])} PRs matched", flush=True)


if __name__ == "__main__":
    main()
