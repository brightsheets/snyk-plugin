---
name: create
description: >-
  Create a batch of GitHub repos whose go.mod files depend on a shared module repo (default
  brightsheets/deps-01) and on each other, plus one repo with no dependencies. Use when the user
  wants Go module test repos, cross-repo go.mod dependencies, fixtures for testing Snyk (or other)
  Go dependency scanning, or to seed deps-01. Plans first and creates nothing until the user
  approves. Supports --dry-run.
argument-hint: "[--dry-run] [--count 10] [--owner brightsheets] [--deps deps-01] [--public]"
allowed-tools: >-
  AskUserQuestion, Bash(python3:*), Bash(gh auth status:*), Bash(command -v:*)
---

<!--
  READER NOTE (for humans opening this file; not an instruction to the skill).

  REQUIREMENTS
  - gh CLI signed in (`gh auth login`) with the `repo` scope, and git.
  - Optional: go. With it, each repo gets a go.sum from `go mod tidy`.
    Without it, go.mod still lists every require and go.sum is left out.

  WHAT IT CHANGES (only with --apply, after you approve)
  - Seeds the shared repo (deps-01) with a small Go package and tag v0.1.0,
    but only if that repo is empty.
  - Creates go-lib-01..NN and go-standalone, pushes them and tags v0.1.0.
  - Adds the topic go-deps-fixture to each so they're easy to find.
  It never overwrites, force-pushes or deletes anything.
-->

# Create Go module fixture repos

Script: `scripts/make_go_repos.py` (run it from this skill's folder).

## What gets built

| Repo | go.mod |
|---|---|
| `deps-01` | Shared module. Seeded only if empty: `package deps` with `Greet()` and `Version()`, tag `v0.1.0`. If it already has code, it is used as is. |
| `go-lib-01` … `go-lib-NN` | Each requires and calls `deps-01`, plus the one or two libs before it (`go-lib-05` → `go-lib-04`, `go-lib-03`). Cross deps, no cycles. |
| `go-standalone` | No dependencies at all (standard library only). |

## Arguments

| Argument | Meaning |
|---|---|
| `--count N` | Number of cross-dependent libs, 1–100. Default 10. |
| `--owner` | GitHub user or org. Default `brightsheets`. |
| `--deps` | Shared module repo. Default `deps-01`. It must already exist; it may be empty. |
| `--prefix` / `--standalone` | Repo names. Defaults `go-lib`, `go-standalone`. |
| `--public` | New repos are public. Default private. |
| `--dry-run` | Show the plan only. |

## Steps

1. Check `gh auth status` shows the `repo` scope, and run `command -v go` to see whether go.sum files will be written.
2. **Plan.** Run `python3 scripts/make_go_repos.py plan [options]` and show the user the table it prints. If it stops on an existing repo, report why and suggest another `--prefix`. Never delete a repo to make room.
3. If `--dry-run`, stop here.
4. **Ask** with AskUserQuestion: "Create these N repos as <private|public> under <owner>?" Options: create, change the count, cancel. "As many as possible" still goes through this question; suggest the cap of 100 and mention that GitHub can throttle bulk creation.
5. **Create.** Run `python3 scripts/make_go_repos.py create [same options] --apply`. It goes in dependency order (deps, then libs from low to high, then standalone), so every required tag exists before a repo needs it. It is safe to rerun: repos it already made are reused.
6. Report the repos created, then run `python3 scripts/make_go_repos.py list`.

## Using them with snyk-onboard

The libs are meant to test Go cross-repo dependencies, for example
`/snyk-onboard:onboard --org brightsheets --filter 'go-lib|go-standalone' --dry-run`.
With private repos and the Snyk GitHub App limited to selected repos, a lib's import can fail
until its dependency repos are also granted. `snyk_import.py import --fix-deps` handles that.
`go-standalone` is the control: it should import cleanly with no dependency findings.

## Removing them

The script never deletes. To remove the repos, the user runs `gh auth refresh -s delete_repo`
and then `gh repo delete <owner>/<name> --yes` for each one listed by `make_go_repos.py list`.
Only suggest this when asked.
