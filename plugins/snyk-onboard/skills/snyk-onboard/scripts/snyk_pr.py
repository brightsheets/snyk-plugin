#!/usr/bin/env python3
"""Step 5: open a .snyk-only PR per repo, the same safe way every run.

  snyk_pr.py --repo owner/name [--repo …] [--plan <plan.json from plan_table.py>] [--apply] [--chat]

Without --apply it only shows what it would do (branch, .snyk diff). With --apply, per repo:
  1. skips the repo if main already has .snyk, or an open PR already adds .snyk
  2. clones into a NEW temporary folder (never deletes anything)
  3. picks a branch: snyk-onboarding, or snyk-onboarding-<YYYYMMDD-HHMM> when that name is taken
  4. writes .snyk from references/snyk-policy.yaml, keeping only the excludes that fit the stack
  5. commits, does a plain push (never --force), opens the PR
  6. checks the PR holds only .snyk; if not, closes it and reports a failure

Never touches .github/workflows/, never force-pushes, never deletes a branch, folder or file.
No shell is used: every command runs as an argument list.
Exit code 0 when no repo failed (skips are fine), 1 otherwise.
"""
import argparse, json, os, subprocess, sys, tempfile, time

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
            for w in ("go-ci.yaml", "js-ci.yaml"):
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


def one(repo, plan, apply):
    """Returns (status, line) with status in ok | skip | fail."""
    if gh_json("api", f"repos/{repo}/contents/.snyk") is not None:
        return "skip", f"– {repo} | skipped: main already has .snyk"
    prs = gh_json("pr", "list", "-R", repo, "--state", "open", "--json", "number,files") or []
    for p in prs:
        if any(f.get("path") == ".snyk" for f in p.get("files") or []):
            return "skip", f"– {repo} | skipped: open PR #{p['number']} already adds .snyk"
    pms, game, d = stack_of(repo, plan)
    text = policy_text(pms, game)
    ci = reusable_ci(repo)
    branch = branch_name(repo)
    print(f"  {repo}: branch {branch}{' (snyk-onboarding is taken)' if branch != BASE_BRANCH else ''}")
    print("  .snyk:")
    for line in text.splitlines():
        print(f"    + {line}")
    if not apply:
        return "ok", f"  {repo} | would open a .snyk PR on {branch} | reusable CI: {'yes (' + ', '.join(ci) + ')' if ci else 'no'}"

    work = tempfile.mkdtemp(prefix="snyk-pr-")
    dest = os.path.join(work, repo.split("/", 1)[1])
    run("gh", "repo", "clone", repo, dest, "--", "--depth", "1", "-q")
    run("git", "switch", "-q", "-c", branch, cwd=dest)
    with open(os.path.join(dest, ".snyk"), "w") as f:
        f.write(text)
    run("git", "add", ".snyk", cwd=dest)
    staged = run("git", "diff", "--cached", "--name-only", cwd=dest).stdout.split()
    if staged != [".snyk"]:
        return "fail", f"✗ {repo} | failed: staged files {staged}, expected only .snyk; nothing pushed"
    ident = []
    if not run("git", "config", "user.name", cwd=dest, check=False).stdout.strip():
        login = (gh_json("api", "user", "--jq", "{l: .login, n: .name}") or {})
        if login.get("l"):
            ident = ["-c", f"user.name={login.get('n') or login['l']}",
                     "-c", f"user.email={login['l']}@users.noreply.github.com"]
    run("git", *ident, "commit", "-q", "-m", "chore: add Snyk policy", cwd=dest)
    push = run("git", "push", "-q", "-u", "origin", branch, cwd=dest, check=False)
    if push.returncode != 0:
        return "fail", f"✗ {repo} | failed: push rejected ({push.stderr.strip()[:200]}); nothing forced, no PR opened"
    products = ", ".join((d.get("snyk_products") or {}).keys()) or "see detection"
    body = (f"Adds a `.snyk` policy only. No CI workflow changes: CI scanning comes from the shared reusable workflows.\n\n"
            f"- **Snyk products:** {products}\n"
            f"- **Detection notes:** {'; '.join(d.get('notes') or []) or 'none'}\n"
            f"- **Calls the reusable workflows:** {'yes (' + ', '.join(ci) + ')' if ci else 'no'}\n")
    pr = run("gh", "pr", "create", "-R", repo, "--head", branch, "--title", "chore: add Snyk policy", "--body", body,
             check=False)
    if pr.returncode != 0:
        return "fail", f"✗ {repo} | failed: gh pr create ({pr.stderr.strip()[:200]})"
    url = pr.stdout.strip().splitlines()[-1]
    files = gh_json("pr", "view", url, "--json", "files", "--jq", "[.files[].path]") or []
    if files != [".snyk"]:
        run("gh", "pr", "close", url, "-c", "Closed by snyk-onboard: this PR must hold only .snyk.", check=False)
        return "fail", f"✗ {repo} | failed: PR {url} held {files}; closed it"
    return "ok", f"✓ {repo} | PR {url} | reusable CI: {'yes (' + ', '.join(ci) + ')' if ci else 'no'}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", nargs="*", default=[])
    ap.add_argument("--file", action="append")
    ap.add_argument("--plan", help="plan.json from plan_table.py --json (stack data for the excludes)")
    ap.add_argument("--apply", action="store_true", help="push and open the PRs (default: show only)")
    ap.add_argument("--chat", action="store_true", help="prefix lines with - / + for a ```diff block")
    a = ap.parse_args()
    if a.chat:
        sys.stdout = si.ChatMarks(sys.stdout)
    plan = json.load(open(a.plan)) if a.plan else []
    repos = si.split_repos(a.repo, a.file or [])
    print(f"{'APPLY' if a.apply else 'DRY RUN'}: .snyk PRs for {len(repos)} repo(s)")
    counts, lines = {"ok": 0, "skip": 0, "fail": 0}, []
    for repo in repos:
        try:
            status, line = one(repo, plan, a.apply)
        except RuntimeError as e:
            status, line = "fail", f"✗ {repo} | failed: {e}"
        counts[status] += 1
        lines.append(line)
    print()
    for line in lines:
        print(line)
    print(f"\nSummary: {'opened' if a.apply else 'would-open'}={counts['ok']}, skipped={counts['skip']}"
          + (f", failed={counts['fail']}" if counts["fail"] else ""))
    sys.stdout.flush()
    sys.exit(1 if counts["fail"] else 0)


if __name__ == "__main__":
    main()
