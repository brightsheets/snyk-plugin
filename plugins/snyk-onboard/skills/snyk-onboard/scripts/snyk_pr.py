#!/usr/bin/env python3
"""Step 5: one PR per repo adding .snyk and, if the repo has none, a caller workflow for the shared reusable pipeline.

  snyk_pr.py --repo owner/name [--repo …] [--plan <plan.json from plan_table.py>] [--apply] [--chat]

Without --apply it only shows what it would do (branch, .snyk diff). With --apply, per repo:
  1. skips the repo if main already has .snyk, or an open PR already adds .snyk
  2. clones into a NEW temporary folder (never deletes anything)
  3. picks a branch: snyk-onboarding, or snyk-onboarding-<YYYYMMDD-HHMM> when that name is taken
  4. writes .snyk from references/snyk-policy.yaml, keeping only the excludes that fit the stack
  5. commits, does a plain push (never --force), opens the PR
  6. checks the PR holds only those files; if not, closes it and reports a failure

Adds .github/workflows/snyk.yml only when no workflow calls the reusable workflows and that file doesn't exist.
Never edits or deletes an existing workflow, never force-pushes, never deletes a branch, folder or file.
No shell is used: every command runs as an argument list.
Exit code 0 when no repo failed (skips are fine), 1 otherwise.
"""
import argparse, concurrent.futures, json, os, re, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import snyk_import as si  # noqa: E402

TEMPLATE = os.path.join(HERE, "..", "references", "snyk-policy.yaml")
BASE_BRANCH = "snyk-onboarding"
# exclude glob -> keep it only when the stack has one of these (None = always keep)
GLOB_NEEDS = {
    "**/node_modules/**": {"npm", "yarn", "pnpm"},
    "**/vendor/**": {"gomodules", "composer", "bundler"},
    "**/Intermediate/**": "game", "**/Binaries/**": "game", "**/Library/**": "game", "**/Temp/**": "game",
}


def run(*cmd, cwd=None, check=True):
    r = subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])}…: {(r.stderr or r.stdout).strip()[:300]}")
    return r


def gh_json(*args):
    r = run("gh", *args, check=False)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout or "null")
    except ValueError:
        return None


def stack_of(repo, plan):
    for d in plan:
        if d.get("source", "").lower() == repo.lower():
            pms = {p for e in (d.get("ecosystems") or {}).values() for p in e.get("package_managers") or []}
            return pms, bool(d.get("game_engines")), d
    return set(), False, {}


def policy_text(pms, game):
    out, in_globs = [], False
    for line in open(TEMPLATE).read().splitlines():
        s = line.strip()
        if s.startswith("- ") and in_globs:
            glob = s[2:].strip().strip('"')
            need = GLOB_NEEDS.get(glob)
            if need == "game" and not game:
                continue
            if isinstance(need, set) and not (need & pms):
                continue
        if s.startswith("#") and in_globs:
            continue  # drop section comments inside the list
        if s == "global:":
            in_globs = True
        out.append(line)
    return "\n".join(out) + "\n"


def reusable_ci(repo):
    names = gh_json("api", f"repos/{repo}/contents/.github/workflows", "--jq", "[.[].name]") or []
    found = set()
    for n in names:
        r = run("gh", "api", f"repos/{repo}/contents/.github/workflows/{n}", "--jq", ".content", check=False)
        if r.returncode == 0:
            import base64
            text = base64.b64decode(r.stdout.strip() or b"").decode(errors="replace")
            for w in ("go-ci.yaml", "js-ci.yaml", "go-snyk.yaml", "js-snyk.yaml"):
                if f"reusable-workflows/.github/workflows/{w}" in text:
                    found.add(w)
    return sorted(found)


def branch_name(repo):
    if gh_json("api", f"repos/{repo}/git/ref/heads/{BASE_BRANCH}") is None:
        return BASE_BRANCH
    name = f"{BASE_BRANCH}-{time.strftime('%Y%m%d-%H%M')}"
    if gh_json("api", f"repos/{repo}/git/ref/heads/{name}") is not None:
        name += time.strftime("%S")
    return name


CALLER = ".github/workflows/snyk.yml"


def reusable_repo(owner, override=None):
    """The shared reusable-workflows repo: --reusable, else <owner>/reusable-workflows if it exists."""
    if override:
        return override
    info = gh_json("api", f"repos/{owner}/reusable-workflows")
    return info["full_name"] if info else None


def caller_text(repo, dest, pms, d, reusable):
    """A Snyk-only caller for the repo's stack: js-snyk.yaml (JavaScript) or go-snyk.yaml (Go), at @v2.
    No build, tests or Docker: just Snyk Open Source + Code, the PR comment and the merge gate."""
    owner = repo.split("/", 1)[0]
    head = ["name: Snyk", "on:", "  push:", '    branches: [main, "hotfix/**"]', "  pull_request:",
            "  workflow_dispatch:", "permissions:", "  contents: read", "  actions: read",
            "  security-events: write", "  pull-requests: write", "jobs:", "  snyk:"]
    common = ["      # Block PRs on high/critical findings; pushes stay report-only.",
              "      fail-on-issues: ${{ github.event_name == 'pull_request' }}",
              "      # Publish the default branch to Snyk (PRs and feat* branches publish under their branch name).",
              "      monitor: ${{ github.ref_name == github.event.repository.default_branch }}"]
    if "gomodules" in pms:
        xorg = si.go_cross_org_private(owner, repo.split("/", 1)[1], d.get("branch"))
        owners = sorted({x.split("/")[0] for x in xorg})
        lines = [f"# Snyk scan only: calls {reusable}/.github/workflows/go-snyk.yaml@v3 (Open Source + Code).",
                 "# Needs secret SNYK_TOKEN" + (f", and GO_MODULES_TOKEN (read access to {', '.join(xorg)})." if owners else ".")]
        lines += head + [f"    uses: {reusable}/.github/workflows/go-snyk.yaml@v3", "    with:", f"      snyk-org: {owner}"]
        if owners:
            lines.append(f"      go-private-owners: {','.join(owners)}")
        lines += common + ["    secrets:", "      snyk-token: ${{ secrets.SNYK_TOKEN }}"]
        if owners:
            lines.append("      go-modules-token: ${{ secrets.GO_MODULES_TOKEN }}")
        return "\n".join(lines) + "\n"
    if "npm" in pms and os.path.exists(os.path.join(dest, "package-lock.json")):
        node = None
        try:
            node = (json.load(open(os.path.join(dest, "package.json"))).get("engines") or {}).get("node")
        except (OSError, ValueError):
            pass
        lines = [f"# Snyk scan only: calls {reusable}/.github/workflows/js-snyk.yaml@v3 (Open Source + Code).",
                 "# Needs secret SNYK_TOKEN."]
        lines += head + [f"    uses: {reusable}/.github/workflows/js-snyk.yaml@v3", "    with:", f"      snyk-org: {owner}"]
        if node:
            lines.append(f'      node-version: "{node}"')
        lines += common + ["    secrets:", "      snyk-token: ${{ secrets.SNYK_TOKEN }}"]
        return "\n".join(lines) + "\n"
    return None  # no Snyk reusable workflow for this stack


def one(repo, plan, apply, reusable_override=None):
    """Returns (status, line) with status in ok | skip | fail."""
    need_snyk = gh_json("api", f"repos/{repo}/contents/.snyk") is None
    ci = reusable_ci(repo)
    need_caller = not ci
    if need_caller and gh_json("api", f"repos/{repo}/contents/{CALLER}") is not None:
        need_caller = False  # a snyk.yml exists but doesn't call the reusable workflows: never overwrite it
        caller_note = f"{CALLER} exists and doesn't call the reusable workflows; left as is"
    else:
        caller_note = None
    if not need_snyk and not need_caller:
        return "skip", f"– {repo} | skipped: has .snyk and " + (caller_note or "already calls the reusable workflows")
    prs = gh_json("pr", "list", "-R", repo, "--state", "open", "--json", "number,files") or []
    for p in prs:
        if any(f.get("path") in (".snyk", CALLER) for f in p.get("files") or []):
            return "skip", f"– {repo} | skipped: open PR #{p['number']} already adds .snyk or {CALLER}"
    pms, game, d = stack_of(repo, plan)
    reusable = reusable_repo(repo.split("/", 1)[0], reusable_override) if need_caller else None
    if need_caller and not reusable:
        need_caller, caller_note = False, "no reusable-workflows repo found (pass --reusable owner/repo)"
    text = policy_text(pms, game) if need_snyk else None
    branch = branch_name(repo)
    print(f"  {repo}: branch {branch}{' (snyk-onboarding is taken)' if branch != BASE_BRANCH else ''}")
    if text:
        print("  .snyk:")
        for line in text.splitlines():
            print(f"    + {line}")
    if not apply:
        adds = " + ".join(x for x, n in ((".snyk", need_snyk), (CALLER, need_caller)) if n) or "nothing"
        if need_caller:
            print(f"  {CALLER}: caller of {reusable} (generated from the repo's files at --apply)")
        return "ok", (f"  {repo} | would open a PR adding {adds} on {branch} | reusable CI: "
                      + ("yes (" + ", ".join(ci) + ")" if ci else ("will be added" if need_caller else "no"))
                      + (f" | note: {caller_note}" if caller_note else ""))

    work = tempfile.mkdtemp(prefix="snyk-pr-")
    dest = os.path.join(work, repo.split("/", 1)[1])
    run("gh", "repo", "clone", repo, dest, "--", "--depth", "1", "-q")
    run("git", "switch", "-q", "-c", branch, cwd=dest)
    expected = []
    if need_snyk:
        with open(os.path.join(dest, ".snyk"), "w") as f:
            f.write(text)
        expected.append(".snyk")
    if need_caller:
        wf = caller_text(repo, dest, pms, d, reusable)
        if wf:
            os.makedirs(os.path.join(dest, ".github", "workflows"), exist_ok=True)
            with open(os.path.join(dest, CALLER), "w") as f:
                f.write(wf)
            expected.append(CALLER)
            print(f"  {CALLER}:")
            for line in wf.splitlines():
                print(f"    + {line}")
        else:
            caller_note = "no Snyk reusable workflow for this stack (Go, or npm with a package-lock.json)"
    if not expected:
        return "skip", f"– {repo} | skipped: nothing to add ({caller_note})"
    run("git", "add", *expected, cwd=dest)
    staged = sorted(run("git", "diff", "--cached", "--name-only", cwd=dest).stdout.split())
    if staged != sorted(expected):
        return "fail", f"✗ {repo} | failed: staged files {staged}, expected {expected}; nothing pushed"
    ident = []
    if not run("git", "config", "user.name", cwd=dest, check=False).stdout.strip():
        login = (gh_json("api", "user", "--jq", "{l: .login, n: .name}") or {})
        if login.get("l"):
            ident = ["-c", f"user.name={login.get('n') or login['l']}",
                     "-c", f"user.email={login['l']}@users.noreply.github.com"]
    title = "chore: add Snyk policy and CI" if len(expected) == 2 else (
        "chore: add Snyk policy" if expected == [".snyk"] else "chore: add Snyk CI")
    run("git", *ident, "commit", "-q", "-m", title, cwd=dest)
    push = run("git", "push", "-q", "-u", "origin", branch, cwd=dest, check=False)
    if push.returncode != 0:
        return "fail", f"✗ {repo} | failed: push rejected ({push.stderr.strip()[:200]}); nothing forced, no PR opened"
    products = ", ".join((d.get("snyk_products") or {}).keys()) or "see detection"
    body = ("Onboards this repo to Snyk.\n\n"
            + ("- **`.snyk`**: Snyk policy with excludes for this stack.\n" if ".snyk" in expected else "")
            + (f"- **`{CALLER}`**: a Snyk-only workflow calling the shared `{reusable}` Snyk scan for this stack at `@v3` (Open Source + Code on every PR, "
               "one PR comment, merge gate on high findings). Needs the `SNYK_TOKEN` secret.\n" if CALLER in expected else "")
            + f"- **Snyk products:** {products}\n"
            + f"- **Detection notes:** {'; '.join(d.get('notes') or []) or 'none'}\n"
            + (f"- **Already calls the reusable workflows:** yes ({', '.join(ci)})\n" if ci else ""))
    pr = run("gh", "pr", "create", "-R", repo, "--head", branch, "--title", title, "--body", body, check=False)
    if pr.returncode != 0:
        return "fail", f"✗ {repo} | failed: gh pr create ({pr.stderr.strip()[:200]})"
    url = pr.stdout.strip().splitlines()[-1]
    files = sorted(gh_json("pr", "view", url, "--json", "files", "--jq", "[.files[].path]") or [])
    if files != sorted(expected):
        run("gh", "pr", "close", url, "-c", "Closed by snyk-onboard: this PR must hold only " + " and ".join(expected) + ".",
            check=False)
        return "fail", f"✗ {repo} | failed: PR {url} held {files}; closed it"
    return "ok", (f"✓ {repo} | PR {url} | adds {' + '.join(expected)} | reusable CI: "
                  + ("yes (" + ", ".join(ci) + ")" if ci else ("added" if CALLER in expected else "no")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", nargs="*", default=[])
    ap.add_argument("--file", action="append")
    ap.add_argument("--plan", help="plan.json from plan_table.py --json (stack data for the excludes)")
    ap.add_argument("--apply", action="store_true", help="push and open the PRs (default: show only)")
    ap.add_argument("--chat", action="store_true", help="prefix lines with - / + for a ```diff block")
    ap.add_argument("--log", help="write one JSON record per repo here (used by summary_table.py)")
    ap.add_argument("--reusable", help="owner/repo of the shared reusable workflows (default: <repo owner>/reusable-workflows)")
    a = ap.parse_args()
    if a.chat:
        sys.stdout = si.ChatMarks(sys.stdout)
    si.ROUTER.out = sys.stdout
    sys.stdout = si.ROUTER
    plan = json.load(open(a.plan)) if a.plan else []
    repos = si.split_repos(a.repo, a.file or [])
    print(f"{'APPLY' if a.apply else 'DRY RUN'}: Snyk onboarding PRs (.snyk + Snyk caller workflow) for {len(repos)} repo(s)")
    counts, lines = {"ok": 0, "skip": 0, "fail": 0}, []

    def work(repo):
        si.ROUTER.local.buf = []
        try:
            status, line = one(repo, plan, a.apply, a.reusable)
        except Exception as e:
            status, line = "fail", f"✗ {repo} | failed: {e}"
        out = "".join(si.ROUTER.local.buf)
        si.ROUTER.local.buf = None
        return status, line, out

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(4, len(repos)))) as ex:
        done = list(ex.map(work, repos))
    records = []
    for repo, (status, line, out) in zip(repos, done):
        sys.stdout.write(out)
        counts[status] += 1
        lines.append(line)
        url = re.search(r"PR (https://\S+)", line)
        why = re.search(r"\| (?:skipped|failed): (.*)$", line)
        records.append({"repo": repo, "status": status, "url": url and url.group(1),
                        "reason": why and why.group(1), "applied": a.apply})
    if a.log:
        json.dump(records, open(a.log, "w"), indent=2)
    print()
    for line in lines:
        print(line)
    print(f"\nSummary: {'opened' if a.apply else 'would-open'}={counts['ok']}, skipped={counts['skip']}"
          + (f", failed={counts['fail']}" if counts["fail"] else ""))
    sys.stdout.flush()
    sys.exit(1 if counts["fail"] else 0)


if __name__ == "__main__":
    main()
