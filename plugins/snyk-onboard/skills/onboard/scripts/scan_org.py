#!/usr/bin/env python3
"""Scan every repo in a GitHub org, detect stacks, and write an onboarding plan.

Usage:
  scan_org.py --org my-gh-org [--include-archived] [--include-forks]
              [--filter 'regex'] [--limit 500] [--out plan.json]

Requires `gh auth login`. Read-only: makes no changes anywhere.
"""
import argparse, json, os, re, subprocess, sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))


def list_repos(org, limit):
    out = subprocess.run(
        ["gh", "repo", "list", org, "--limit", str(limit), "--json",
         "nameWithOwner,isArchived,isFork,defaultBranchRef,primaryLanguage,pushedAt,visibility"],
        check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def detect(repo, branch):
    cmd = [sys.executable, os.path.join(HERE, "detect_stack.py"), "--github", repo]
    if branch:
        cmd += ["--branch", branch]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {"source": repo, "error": r.stderr.strip() or "detection failed"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", required=True)
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--filter", help="only repos whose name matches this regex")
    ap.add_argument("--include-archived", action="store_true")
    ap.add_argument("--include-forks", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", default="snyk-onboarding-plan.json")
    a = ap.parse_args()

    repos = list_repos(a.org, a.limit)
    skipped = []
    todo = []
    for r in repos:
        name = r["nameWithOwner"]
        if r["isArchived"] and not a.include_archived:
            skipped.append({"repo": name, "reason": "archived"}); continue
        if r["isFork"] and not a.include_forks:
            skipped.append({"repo": name, "reason": "fork"}); continue
        if a.filter and not re.search(a.filter, name):
            skipped.append({"repo": name, "reason": "filtered"}); continue
        if not r.get("defaultBranchRef"):
            skipped.append({"repo": name, "reason": "empty repo"}); continue
        todo.append((name, r["defaultBranchRef"]["name"]))

    print(f"Scanning {len(todo)} repos ({len(skipped)} skipped)...", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        results = list(ex.map(lambda t: detect(*t), todo))

    targets, errors, no_stack = [], [], []
    for res in results:
        if "error" in res:
            errors.append({"repo": res["source"], "error": res["error"]}); continue
        if not res["snyk_products"]:
            no_stack.append(res["source"]); continue
        owner, name = res["source"].split("/", 1)
        targets.append({
            "owner": owner, "name": name, "branch": res["branch"],
            "products": sorted(res["snyk_products"]),
            "ecosystems": sorted(res["ecosystems"]),
            "ci_systems": res["ci_systems"],
            "game_engines": res["game_engines"],
            "already_onboarded": res["already_onboarded"],
            "notes": res["notes"],
        })

    summary = {
        "org": a.org, "repos_total": len(repos), "scanned": len(todo),
        "to_onboard": len(targets), "no_supported_stack": len(no_stack),
        "errors": len(errors), "skipped": len(skipped),
        "by_product": {p: sum(p in t["products"] for t in targets)
                       for p in ["open_source", "code", "container", "iac", "unmanaged_cpp"]},
        "by_ecosystem": {},
    }
    for t in targets:
        for e in t["ecosystems"]:
            summary["by_ecosystem"][e] = summary["by_ecosystem"].get(e, 0) + 1

    plan = {"summary": summary, "targets": targets, "no_supported_stack": no_stack,
            "errors": errors, "skipped": skipped}
    with open(a.out, "w") as f:
        json.dump(plan, f, indent=2)
    print(json.dumps(summary, indent=2))
    print(f"Plan written to {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
