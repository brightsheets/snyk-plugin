#!/usr/bin/env python3
"""Steps 1-3 in one quiet command: detect each repo's stack, resolve its Snyk org, pick the onboarding
mode, and print the plan as one table (the standard display), then the skipped and stopped repos.

Onboarding mode per repo:
  scm      (default) import through the Snyk org's SCM integration, then the PR and the merge gate
  ci-only  no SCM import: only the Snyk caller workflow (CLI scan + autofix) and the merge gate. Picked
           automatically when go.mod needs private modules from another GitHub org (the SCM integration
           can't read them), or for every repo with --ci-only.

  plan_table.py --repo test-snyk-demo/snyk-demo-go-1 [--repo …] [--file repos.txt] [--snyk-org <slug>]
                [--json <path>]   # also save the full detection + resolve data for later steps

Read-only. Exit code 0 when at least one repo is in the plan, 1 otherwise.
"""
import argparse, concurrent.futures, json, os, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import detect_stack as ds  # noqa: E402
import snyk_pr as sp  # noqa: E402
from summary_table import box  # noqa: E402
import snyk_import as si  # noqa: E402

PRODUCTS = [("open_source", "Open Source"), ("code", "Code"), ("container", "Container"), ("iac", "IaC")]
PKG = {"gomodules": "Go modules", "npm": "npm", "yarn": "yarn", "pnpm": "pnpm", "pip": "pip", "pipenv": "Pipenv",
       "poetry": "Poetry", "uv": "uv", "maven": "Maven", "gradle": "Gradle", "sbt": "sbt", "bundler": "Bundler",
       "composer": "Composer", "nuget": "NuGet", "cargo": "Cargo", "swiftpm": "SwiftPM", "cocoapods": "CocoaPods",
       "hex": "Hex", "pub": "Pub", "conan": "Conan", "vcpkg": "vcpkg"}
CI = {"github_actions": "GitHub Actions", "gitlab_ci": "GitLab CI", "jenkins": "Jenkins", "circleci": "CircleCI",
      "azure_pipelines": "Azure Pipelines", "bitbucket_pipelines": "Bitbucket Pipelines"}
ITYPE = {"github-cloud-app": "GitHub Cloud App", "github": "GitHub", "github-enterprise": "GitHub Enterprise"}


def label(key, table):
    return table.get(key) or key.replace("_", " ").replace("-", " ").title()


def cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def products(d):
    sp = d.get("snyk_products") or {}
    names = [name for key, name in PRODUCTS if key in sp]
    names += [label(k, {}) for k in sp if k not in dict(PRODUCTS)]
    return ", ".join(names)


def ecosystems(d):
    pms = []
    for eco in (d.get("ecosystems") or {}).values():
        for pm in eco.get("package_managers") or []:
            if label(pm, PKG) not in pms:
                pms.append(label(pm, PKG))
    parts = [", ".join(pms)] if pms else []
    if d.get("containers"):
        parts.append(", ".join(d["containers"]))
    if d.get("iac"):
        parts.append(", ".join(label(k, {}) for k in d["iac"]))
    return "; ".join(parts) or "none"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", nargs="*", default=[])
    ap.add_argument("--file", action="append")
    ap.add_argument("--snyk-org")
    ap.add_argument("--json", help="save detection + resolve results here")
    ap.add_argument("--markdown", action="store_true", help="print a Markdown table instead of the drawn box")
    ap.add_argument("--reusable", help="owner/repo of the shared reusable workflows (default: <repo owner>/reusable-workflows)")
    ap.add_argument("--ci-only", action="store_true", help="onboard every repo without the SCM import (CI caller + merge gate only)")
    a = ap.parse_args()

    repos = si.split_repos(a.repo, a.file or [])
    mapping = si.load_mapping()
    rows, skipped, stopped, data = [], [], [], []

    def fetch(repo):
        try:
            return ds.list_github(repo, None), None
        except subprocess.CalledProcessError as e:
            return None, (e.stderr or "").strip() or "GitHub API error"

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        fetched = list(ex.map(fetch, repos))
    for repo, (got, err) in zip(repos, fetched):
        if err:
            stopped.append((repo, "detection", err))
            continue
        paths, meta = got
        d = {"source": repo, **meta, **ds.detect(paths)}
        if meta.get("truncated"):
            d["notes"].append("GitHub tree was truncated (very large repo); clone and use --path for full accuracy.")
        if meta.get("archived"):
            skipped.append((repo, "archived")); continue
        if (d.get("already_onboarded") or {}).get("snyk_in_github_actions"):
            skipped.append((repo, "already runs Snyk in GitHub Actions")); continue
        if not d.get("snyk_products"):
            skipped.append((repo, "no supported stack")); continue
        org, itype, iid, why = si.resolve(mapping, repo.split("/", 1)[0], a.snyk_org, None)
        d["resolve"] = {"snyk_org": org and org["slug"], "snyk_org_id": org and org["id"],
                        "integration_type": itype, "integration_id": iid, "reason": why}
        data.append(d)
        pms = {p for e in (d.get("ecosystems") or {}).values() for p in e.get("package_managers") or []}
        stack = "go" if "gomodules" in pms else "js" if "npm" in pms and "package-lock.json" in (d.get("manifest_files") or []) else None
        owner, name = repo.split("/", 1)
        xorg = si.go_cross_org_private(owner, name, d.get("branch")) if stack == "go" else []
        if a.ci_only or xorg:
            d["mode"] = "ci-only"
            d["mode_reason"] = (f"go.mod needs private modules from another GitHub org ({', '.join(xorg)}), "
                                "which the SCM integration can't read") if xorg else "--ci-only"
        else:
            d["mode"] = "scm"
        if d["mode"] == "scm" and not iid:
            stopped.append((repo, "resolve", why)); continue
        if d["mode"] == "ci-only" and not org:
            stopped.append((repo, "resolve", why)); continue
        has_ci = bool(sp.reusable_ci(repo))
        if d["mode"] == "ci-only" and not has_ci and not (stack and sp.reusable_repo(owner, a.reusable)):
            stopped.append((repo, "plan", "CI-only onboarding needs a Snyk caller, and there's no reusable Snyk workflow for this stack"))
            continue
        adds = []
        if sp.gh_json("api", f"repos/{repo}/contents/.snyk") is None:
            adds.append(".snyk")
        # The Snyk caller only for CI-only repos: with an SCM import it would double-report.
        if d["mode"] == "ci-only" and not has_ci and sp.gh_json("api", f"repos/{repo}/contents/{sp.CALLER}") is None:
            if stack and sp.reusable_repo(owner, a.reusable):
                adds.append(f"snyk.yml ({stack}-snyk + autofix)")
        d["pr_adds"] = adds
        mode = "SCM import" if d["mode"] == "scm" else "CI only (cross-org deps)" if xorg else "CI only"
        rows.append([repo, d["branch"], products(d), ecosystems(d),
                     ", ".join(label(c, CI) for c in d.get("ci_systems") or []) or "none",
                     f"{org['slug']} ({label(itype, ITYPE) if d['mode'] == 'scm' else 'CLI'})", mode,
                     " + ".join(adds) or "nothing (has both)",
                     "; ".join(d.get("notes") or []) or "none"])

    head = ["Repo", "Branch", "Products", "Ecosystems", "CI", "Snyk org", "Onboarding", "PR adds", "Notes"]
    if rows and a.markdown:
        print("| " + " | ".join(head) + " |")
        print("|" + "|".join("---" for _ in head) + "|")
        for r in rows:
            print("| " + " | ".join(cell(x) for x in r) + " |")
    elif rows:
        print(box(head, rows, width=22))
    else:
        print("No repos in the plan.")
    print()
    ci_only = [d for d in data if d.get("mode") == "ci-only" and d["source"] in {r[0] for r in rows}]
    if ci_only:
        print("CI only (no SCM import; the caller scans with the Snyk CLI, the merge gate blocks): "
              + "; ".join(f"{d['source']} ({d['mode_reason']})" for d in ci_only) + ".")
    print("Skipped: " + ("; ".join(f"{r} ({why})" for r, why in skipped) if skipped else "none") + ".")
    if stopped:
        print("Stopped: " + "; ".join(f"{r} ({step}: {why})" for r, step, why in stopped) + ".")
    if a.json:
        json.dump(data, open(a.json, "w"), indent=2)
    sys.exit(0 if rows else 1)


if __name__ == "__main__":
    main()
