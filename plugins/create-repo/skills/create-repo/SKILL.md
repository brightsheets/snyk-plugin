---
name: create-repo
description: >-
  Create a batch of GitHub repos whose go.mod files depend on a shared module repo (default
  brightsheets/deps-01) and on each other, plus one repo with no dependencies. Use when the user
  wants Go module test repos, cross-repo go.mod dependencies, fixtures for testing Snyk (or other)
  Go dependency scanning, or to seed deps-01. Plans first and creates nothing until the user
  approves. Supports --dry-run.
argument-hint: "[number | repo names...] [--public]"
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

# Create Go module repos

**Usage:** `/create-repo 5` creates 5 new repos in `brightsheets` with free names chosen automatically. `/create-repo payments orders` creates repos with those names. Deps are added automatically.

If the user only asks how to use it, reply with the usage line and stop.

Script: `scripts/make_go_repos.py` (run it from this skill's folder). Pass the user's number or names straight through as positional arguments.

## What gets built

**A number N** (default 5): N new repos, never clashing with existing ones.
- N−1 libs named after the highest existing `go-lib-NN` (e.g. `go-lib-11`, `go-lib-12`, …). Each one's `go.mod` requires and calls `deps-01` plus the one or two libs before it in the batch.
- One repo with no deps at all: `go-standalone`, or the next free `go-standalone-02`, `-03`, …
- With N = 1, it creates just one lib with deps.

**Repo names**: those repos, each with cross deps (on `deps-01` and the one or two names before it). `--standalone NAME` adds one repo with no deps. A name that already exists stops the run, unless an earlier run of this plugin made it.

Every repo gets `go.mod`, `go.sum` (when Go is installed), Go code that calls its deps, a `v0.1.0` tag and the topic `go-deps-fixture`. If `deps-01` is empty, it's seeded first with a small Go package and tag `v0.1.0`.

## Options (rarely needed)

| Option | Meaning |
|---|---|
| `--public` | New repos are public. Default private. |
| `--owner` | GitHub user or org. Default `brightsheets`. |
| `--deps` | Shared module repo. Default `deps-01`. |
| `--prefix` | Name prefix for numbered repos. Default `go-lib`. |
| `--standalone NAME` | Name of the repo with no deps. |
| `--dry-run` | Show the plan only. |

## Steps

1. Check that `gh auth status` shows the `repo` scope, and run `command -v go` to see whether go.sum files will be written.
2. **Plan.** Run `python3 scripts/make_go_repos.py plan <number or names> [options]` and show the user the table it prints. If it stops on an existing repo, report why. Never delete a repo to make room.
3. If `--dry-run`, stop here.
4. **Ask** with AskUserQuestion: "Create these N repos as <private|public> in <owner>?" Options: Create, Cancel. For more than 100 repos, say it's capped at 100 per run because GitHub throttles bulk creation.
5. **Create.** Run `python3 scripts/make_go_repos.py create <same arguments> --apply`. It works in dependency order (deps-01, then libs, then the standalone repo), so every required tag exists before a repo needs it.
6. Report the repos created, as links.

## Using them with snyk-onboard

`/snyk-onboard go-lib-11 go-lib-12 go-standalone --dry-run`. With private repos and the Snyk GitHub App limited to selected repos, a lib's import can fail until its dependency repos are also granted; `snyk_import.py import --fix-deps` handles that. The repo with no deps is the control: it should import cleanly.

## Removing them

The script never deletes. To remove the repos, the user runs `gh auth refresh -s delete_repo` and then `gh repo delete <owner>/<name> --yes` for each one listed by `make_go_repos.py list`. Only suggest this when asked.
