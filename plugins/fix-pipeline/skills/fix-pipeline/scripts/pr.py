#!/usr/bin/env python3
"""Steps 4 and 7 of /fix-pipeline: a safe working copy, then the fix PR. Never pushes to the default
branch, never force-pushes, never merges, never deletes anything.

  pr.py prepare --repo owner/name [--base main]
      Clones into a NEW temporary folder at the tip of --base (default: the repo's default branch)
      and prints the folder. Edit files there.

  pr.py open --dir <folder> --run-url <url> --title "..." --body-file body.md [--base main]
      [--expect file ...] [--dry-run]
      Shows the diff, then commits the changes on a new branch fix-pipeline/<run-id> (or
      fix-pipeline/<run-id>-<HHMM> if taken), plain push, opens the PR into --base, and checks the PR
      holds only the changed files (and, with --expect, only those files). With --dry-run: diff only.
"""
import argparse, json, os, re, subprocess, sys, tempfile, time


def run(*cmd, cwd=None, check=True):
    r = subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"✗ {' '.join(cmd[:3])}…: {(r.stderr or r.stdout).strip()[:400]}")
    return r


def default_branch(repo):
    return run("gh", "api", f"repos/{repo}", "--jq", ".default_branch").stdout.strip()


def prepare(a):
    base = a.base or default_branch(a.repo)
    d = tempfile.mkdtemp(prefix="fix-pipeline-")
    run("gh", "repo", "clone", a.repo, d, "--", "-q", "--branch", base)
    sha = run("git", "rev-parse", "--short", "HEAD", cwd=d).stdout.strip()
    print(f"✓ {a.repo}@{base} ({sha}) cloned into {d}")
    print(f"DIR={d}")


def open_pr(a):
    d = a.dir
    repo = re.sub(r"\.git$", "", run("git", "remote", "get-url", "origin", cwd=d).stdout.strip().split("github.com/")[-1])
    base = a.base or default_branch(repo)
    changed = [l[3:] for l in run("git", "status", "--porcelain", cwd=d).stdout.splitlines() if l.strip()]
    if not changed:
        raise SystemExit("✗ nothing changed in the working copy: no PR")
    if a.expect and set(changed) - set(a.expect):
        raise SystemExit(f"✗ unexpected changed files: {sorted(set(changed) - set(a.expect))} (expected {a.expect})")
    print("Files:", ", ".join(changed))
    print(run("git", "--no-pager", "diff", "--stat", cwd=d).stdout)
    print(run("git", "--no-pager", "diff", cwd=d).stdout[:12000])
    if a.dry_run:
        print("DRY RUN: no branch, commit, push or PR.")
        return
    m = re.search(r"/actions/runs/(\d+)", a.run_url)
    branch = f"fix-pipeline/{m.group(1) if m else 'run'}"
    if run("git", "ls-remote", "--exit-code", "--heads", "origin", branch, cwd=d, check=False).returncode == 0:
        branch += time.strftime("-%H%M")
    run("git", "switch", "-q", "-c", branch, cwd=d)
    run("git", "add", "--", *changed, cwd=d)
    run("git", "commit", "-q", "-m", f"{a.title}\n\nFixes the failure in {a.run_url}", cwd=d)
    run("git", "push", "-q", "-u", "origin", branch, cwd=d)
    url = run("gh", "pr", "create", "-R", repo, "--base", base, "--head", branch,
              "--title", a.title, "--body-file", a.body_file, cwd=d).stdout.strip().splitlines()[-1]
    files = json.loads(run("gh", "pr", "view", url, "--json", "files", "-q", "[.files[].path]").stdout)
    if set(files) != set(changed):
        run("gh", "pr", "close", url, "-c", "Closed by /fix-pipeline: the PR held unexpected files.", check=False)
        raise SystemExit(f"✗ the PR held {files}, expected {changed}: closed it")
    print(f"✓ {repo} | PR {url} | branch {branch} → {base} | {', '.join(changed)}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare"); p.add_argument("--repo", required=True); p.add_argument("--base")
    o = sub.add_parser("open")
    o.add_argument("--dir", required=True); o.add_argument("--run-url", required=True)
    o.add_argument("--title", required=True); o.add_argument("--body-file", required=True)
    o.add_argument("--base"); o.add_argument("--expect", nargs="*"); o.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    prepare(a) if a.cmd == "prepare" else open_pr(a)


if __name__ == "__main__":
    main()
