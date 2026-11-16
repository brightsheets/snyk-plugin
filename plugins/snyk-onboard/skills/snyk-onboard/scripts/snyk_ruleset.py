#!/usr/bin/env python3
"""Step 5b: the merge gate. One repository ruleset per repo that blocks merging into the default branch.

  CI-only repos: "Require code scanning results" for the Snyk tools (new alerts at high or higher).
  SCM repos (--plan mode scm): "Require status checks" for Snyk's own PR checks,
    security/snyk (<snyk-org>) and, with Snyk Code, code/snyk (<snyk-org>). Only with --scm-checks on:
    Snyk posts those statuses only when its PR checks are on (step 4b), else the rule would block every merge.

  snyk_ruleset.py --repo owner/name [--repo …] [--pr-log pr-log.json] [--apply] [--chat] [--log ruleset-log.json]

Without the ruleset, the Snyk check goes red but the PR can still be merged. The ruleset is GitHub's
"Require code scanning results" rule for the tools the shared Snyk workflows upload (SARIF):
"Snyk Open Source" and "SnykCode", security alerts "High or higher", on ~DEFAULT_BRANCH, no bypass.

Per repo:
  1. Eligible only if the repo uploads Snyk results to code scanning: a workflow on the default branch
     calls the reusable workflows (js-ci, go-ci, js-snyk, go-snyk), or this run's onboarding PR
     (--pr-log) adds the Snyk caller workflow. Otherwise skipped: the rule would wait for results that
     never come and block every merge.
  2. Skipped if a code scanning rule for those tools already applies to the default branch
     (repo or org ruleset). An existing rule is never edited or deleted.
  3. With --apply, creates the ruleset and reads it back.

Exit code 0 when no repo failed (skips are fine), 1 otherwise.
"""
import argparse, json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import snyk_import as si  # noqa: E402
from snyk_pr import gh_json, reusable_ci  # noqa: E402

NAME = "Block new Snyk high+ alerts"
SCM_NAME = "Require Snyk PR checks"
TOOLS = ["Snyk Open Source", "SnykCode"]
THRESHOLD = "high_or_higher"


def ruleset_body():
    return {
        "name": NAME,
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "bypass_actors": [],
        "rules": [{
            "type": "code_scanning",
            "parameters": {"code_scanning_tools": [
                {"tool": t, "security_alerts_threshold": THRESHOLD, "alerts_threshold": "errors"} for t in TOOLS]},
        }],
    }


def scm_body(contexts):
    return {
        "name": SCM_NAME,
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "bypass_actors": [],
        "rules": [{
            "type": "required_status_checks",
            "parameters": {"strict_required_status_checks_policy": False, "do_not_enforce_on_create": False,
                           "required_status_checks": [{"context": c} for c in contexts]},
        }],
    }


def existing_checks(repo, branch):
    """Required status check contexts that already apply to the default branch (rulesets + branch protection)."""
    have = set()
    for r in gh_json("api", f"repos/{repo}/rules/branches/{branch}") or []:
        if r.get("type") == "required_status_checks":
            have |= {c.get("context") for c in (r.get("parameters") or {}).get("required_status_checks") or []}
    bp = gh_json("api", f"repos/{repo}/branches/{branch}/protection/required_status_checks") or {}
    have |= set(bp.get("contexts") or [])
    return have


def one_scm(repo, entry, apply, scm_checks):
    """SCM repos: require Snyk's own PR checks (commit statuses posted by the Snyk GitHub integration)."""
    meta = gh_json("api", f"repos/{repo}", "--jq", "{branch: .default_branch, admin: .permissions.admin}")
    if not meta:
        raise RuntimeError("can't read the repo through the GitHub API")
    branch = meta["branch"]
    org = ((entry or {}).get("resolve") or {}).get("snyk_org")
    products = (entry or {}).get("snyk_products") or {}
    contexts = [f"security/snyk ({org})"] + ([f"code/snyk ({org})"] if "code" in products else [])
    print(f"{repo}: default branch {branch}; SCM import, Snyk org {org}; Snyk PR checks: {scm_checks}")
    if scm_checks != "on":
        return "skip", (f"– {repo} | skipped: Snyk PR checks are off, so Snyk posts no status to require "
                        "(turn them on in step 4b for a merge gate)")
    if not org:
        raise RuntimeError("no Snyk org for this repo in plan.json")
    have = existing_checks(repo, branch)
    if set(contexts) <= have:
        return "skip", f"– {repo} | skipped: {', '.join(contexts)} already required on {branch}"
    if not meta.get("admin"):
        raise RuntimeError("the signed-in GitHub account isn't an admin of this repo (rulesets need admin)")
    print(f"  ruleset: \"{SCM_NAME}\" on ~DEFAULT_BRANCH · require status checks · {' + '.join(contexts)} · no bypass")
    if not apply:
        return "ok", f"✓ {repo} | would require Snyk's PR checks on {branch}"
    p = subprocess.run(["gh", "api", "-X", "POST", f"repos/{repo}/rulesets", "--input", "-"],
                       input=json.dumps(scm_body(contexts)), capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"creating the ruleset failed: {(p.stderr or p.stdout).strip()[:300]}")
    rid = json.loads(p.stdout).get("id")
    if not set(contexts) <= existing_checks(repo, branch):
        raise RuntimeError(f"ruleset {rid} was created but doesn't apply to {branch} when read back")
    return "ok", f"✓ {repo} | merge gate https://github.com/{repo}/rules/{rid} | requires {', '.join(contexts)} on {branch}"


def caller_in_pr(repo, pr_rec):
    """True if this run's onboarding PR for the repo adds the Snyk caller workflow."""
    url = (pr_rec or {}).get("url") or ""
    if (pr_rec or {}).get("status") != "ok" or "/pull/" not in url:
        return False
    files = gh_json("api", f"repos/{repo}/pulls/{url.rsplit('/', 1)[1]}/files", "--jq", "[.[].filename]") or []
    return ".github/workflows/snyk.yml" in files


def existing_gate(repo, branch):
    """Code scanning rules that already apply to the default branch (repo and org rulesets)."""
    rules = gh_json("api", f"repos/{repo}/rules/branches/{branch}") or []
    tools = set()
    for r in rules:
        if r.get("type") == "code_scanning":
            for t in (r.get("parameters") or {}).get("code_scanning_tools") or []:
                tools.add(t.get("tool"))
    return tools


def one(repo, pr_rec, apply):
    meta = gh_json("api", f"repos/{repo}", "--jq", "{branch: .default_branch, admin: .permissions.admin}")
    if not meta:
        raise RuntimeError("can't read the repo through the GitHub API")
    branch = meta["branch"]
    calls = reusable_ci(repo)
    via = ", ".join(calls) if calls else ("Snyk caller in the onboarding PR" if caller_in_pr(repo, pr_rec) else "")
    print(f"{repo}: default branch {branch}; Snyk results to code scanning: {via or 'none'}")
    if not via:
        return "skip", f"– {repo} | skipped: no workflow uploads Snyk results to code scanning, so the rule would block every merge"
    have = existing_gate(repo, branch)
    if set(TOOLS) <= have:
        return "skip", f"– {repo} | skipped: a code scanning rule for {', '.join(TOOLS)} already applies to {branch}"
    if have:
        return "skip", (f"– {repo} | skipped: a different code scanning rule already applies to {branch} "
                        f"({', '.join(sorted(t for t in have if t))}); left as is")
    if not meta.get("admin"):
        raise RuntimeError("the signed-in GitHub account isn't an admin of this repo (rulesets need admin)")
    print(f"  ruleset: \"{NAME}\" on ~DEFAULT_BRANCH · require code scanning results · "
          f"{' + '.join(TOOLS)} · security alerts {THRESHOLD.replace('_', ' ')} · no bypass")
    if not apply:
        return "ok", f"✓ {repo} | would add the merge gate on {branch}"
    p = subprocess.run(["gh", "api", "-X", "POST", f"repos/{repo}/rulesets", "--input", "-"],
                       input=json.dumps(ruleset_body()), capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"creating the ruleset failed: {(p.stderr or p.stdout).strip()[:300]}")
    rid = json.loads(p.stdout).get("id")
    if not set(TOOLS) <= existing_gate(repo, branch):
        raise RuntimeError(f"ruleset {rid} was created but doesn't apply to {branch} when read back")
    url = f"https://github.com/{repo}/rules/{rid}"
    return "ok", f"✓ {repo} | merge gate {url} | blocks new {' / '.join(TOOLS)} alerts at high or higher on {branch}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", nargs="*", default=[])
    ap.add_argument("--file", action="append")
    ap.add_argument("--pr-log", help="pr-log.json from snyk_pr.py (to see which PRs add the Snyk caller)")
    ap.add_argument("--plan", help="plan.json from plan_table.py: each repo's mode (scm or ci-only) and Snyk org")
    ap.add_argument("--scm-checks", choices=["on", "off"], default="off",
                    help="whether step 4b left Snyk PR checks on (SCM repos are gated by them only when on)")
    ap.add_argument("--apply", action="store_true", help="create the rulesets (default: show only)")
    ap.add_argument("--chat", action="store_true", help="prefix lines with - / + for a ```diff block")
    ap.add_argument("--log", help="write one JSON record per repo here (used by summary_table.py)")
    a = ap.parse_args()
    if a.chat:
        sys.stdout = si.ChatMarks(sys.stdout)
    prs = {r["repo"].lower(): r for r in (json.load(open(a.pr_log)) if a.pr_log else [])}
    plan = {d["source"].lower(): d for d in (json.load(open(a.plan)) if a.plan else [])}
    repos = si.split_repos(a.repo, a.file or [])
    print(f"{'APPLY' if a.apply else 'DRY RUN'}: Snyk merge gate (repository ruleset) for {len(repos)} repo(s)")
    counts, lines, records = {"ok": 0, "skip": 0, "fail": 0}, [], []
    for repo in repos:
        try:
            entry = plan.get(repo.lower())
            if entry and entry.get("mode", "scm") == "scm":
                status, line = one_scm(repo, entry, a.apply, a.scm_checks)
            else:
                status, line = one(repo, prs.get(repo.lower()), a.apply)
        except Exception as e:
            status, line = "fail", f"✗ {repo} | failed: {e}"
        counts[status] += 1
        lines.append(line)
        reason = line.split("| skipped: ", 1)[1] if "| skipped: " in line else (
            line.split("| failed: ", 1)[1] if "| failed: " in line else None)
        records.append({"repo": repo, "status": status, "reason": reason, "applied": a.apply})
    if a.log:
        json.dump(records, open(a.log, "w"), indent=2)
    print()
    for line in lines:
        print(line)
    print(f"\nSummary: {'added' if a.apply else 'would-add'}={counts['ok']}, skipped={counts['skip']}"
          + (f", failed={counts['fail']}" if counts["fail"] else ""))
    sys.stdout.flush()
    sys.exit(1 if counts["fail"] else 0)


if __name__ == "__main__":
    main()
