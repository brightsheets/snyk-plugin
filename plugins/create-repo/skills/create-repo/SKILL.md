---
name: create-repo
description: >-
  Create demo GitHub repos in brightsheets for Snyk testing. Go (default): snyk-demo-go-1, -2, ...
  whose go.mod files have cross-org deps on brightsheets/deps-01 and on each other, with the last one
  having public deps only (no cross-org deps). Node or Java: snyk-demo-node-N / snyk-demo-java-N, each a copy of snyk-labs/nodejs-goof or
  snyk-labs/java-goof. Use when the user wants to create demo, test or fixture repos, Go repos with
  cross-repo dependencies, or copies of the Snyk goof apps. Plans first and creates nothing until
  the user approves. Supports --dry-run.
argument-hint: "[go|node|java] [number | repo names...] [--public]"
allowed-tools: >-
  AskUserQuestion, Bash(python3:*), Bash(gh auth status:*), Bash(command -v:*)
---

<!--
  READER NOTE (for humans opening this file; not an instruction to the skill).

  REQUIREMENTS
  - gh CLI signed in (`gh auth login`) with the `repo` scope, and git.
  - Optional: go. With it, each repo gets a go.sum from `go mod tidy`.
    Without it, go.mod still lists every require and go.sum is left out.

  WHAT IT CHANGES (only after you approve)
  - Go: creates deps-01 if it's missing, and seeds it with a small Go package
    and tag v1.0.0 if it's missing or empty. Creates snyk-demo-go-N repos,
    pushes them and tags v1.0.0.
  - Node/Java: creates snyk-demo-node-N / snyk-demo-java-N, each holding the
    latest code of snyk-labs/nodejs-goof or snyk-labs/java-goof.
  - Adds the topic snyk-demo to each so they're easy to find.
  It never overwrites, force-pushes or deletes anything.
-->

# Create demo repos

**Usage:** `/create-repo` creates 2 Go repos in `brightsheets`: one with cross-org deps and one without. `/create-repo 5` creates 5. `/create-repo node 3` and `/create-repo java 2` create copies of Snyk's demo apps.

If the user only asks how to use it, reply with the usage line and stop.

Script: `scripts/make_repos.py` (run it from this skill's folder). Pass the user's words straight through as positional arguments: an optional language (`go`, `node`, `java`), then a number or repo names.

## What gets built

| Command | Repos |
|---|---|
| `/create-repo` (default: 2 Go repos) | `snyk-demo-go-N` with cross-org deps on `deps-01`, and `snyk-demo-go-N+1` with public deps only. |
| `/create-repo 5` (Go is the default language) | `snyk-demo-go-1` … `-5`. Repos 1–4 have **cross-org deps**: each `go.mod` (and `go.sum` when Go is installed) requires and calls `deps-01` plus the one or two repos before it. Repo 5 has **public deps only**, no cross-org deps: `github.com/google/uuid` and `golang.org/x/text` v0.3.7 (deliberately old, with a known vulnerability, so Snyk finds something). Tagged `v1.0.0`. |
| `/create-repo node 3` | `snyk-demo-node-1` … `-3`, each a copy of the latest [snyk-labs/nodejs-goof](https://github.com/snyk-labs/nodejs-goof) (a deliberately vulnerable Node.js app). Upstream workflows are replaced by `.github/workflows/ci.yml` calling `js-ci.yaml@v1` (build, Snyk, Docker build + Snyk Container; the image is never pushed). |
| `/create-repo java 2` | `snyk-demo-java-1` … `-2`, each a copy of the latest [snyk-labs/java-goof](https://github.com/snyk-labs/java-goof), with its upstream workflows removed (no shared Java pipeline yet). |
| `/create-repo payments orders` | Those names. For Go, each gets cross deps. `--standalone NAME` adds a Go repo with public deps only. |

- **CI caller:** every Go repo gets `.github/workflows/ci.yml`, which calls `brightsheets/reusable-workflows/.github/workflows/go-ci.yaml@v1` (lint, tests, Snyk). Repos with cross-org deps also pass `go-private-owners` and the `GO_MODULES_TOKEN` secret. Without a `SNYK_TOKEN` secret the Snyk scan is skipped with a warning; without `GO_MODULES_TOKEN`, repos with cross-org deps can't download their private modules, so lint and tests fail until it's set. To add the caller to Go repos made before this, run `python3 scripts/make_repos.py add-caller <repos…>` (dry run) and then add `--apply`.
- **Numbering:** numbers continue after the highest existing one (if `snyk-demo-go-5` exists, the next run starts at 6), so names never clash. A name the user gives that already exists stops the run.
- **Copies, not forks:** GitHub allows only one fork of a repo per account, and forks of public repos can't be private.
- **`deps-01`:** for Go, if `deps-01` is missing it is created (public), and if it is missing or empty it is seeded with a small Go package and tag `v1.0.0`.

## Options (rarely needed)

| Option | Meaning |
|---|---|
| `--public` | New repos are public. Default private. |
| `--owner` | GitHub user or org. Default `brightsheets`. |
| `--prefix` | Name prefix for numbered repos. Default `snyk-demo-<lang>`. |
| `--deps` | Go: shared module repo. Default `deps-01`. Created (public) and seeded if missing. |
| `--dry-run` | Show the plan only. |

## Steps

1. Check that `gh auth status` shows the `repo` scope. For Go, run `command -v go` to see whether go.sum files will be written.
2. **Plan.** Run `python3 scripts/make_repos.py plan <args>` and show the user the table it prints. If it stops on an existing repo, report why. Never delete a repo to make room.
3. If `--dry-run`, stop here.
4. **Ask** with AskUserQuestion: "Create these N repos as <private|public> in <owner>?" Options: Create, Cancel. More than 100 per run isn't allowed, because GitHub throttles bulk creation.
5. **Create.** Run `python3 scripts/make_repos.py create <same args> --apply`. Go repos are created in dependency order, so each tag exists before a repo needs it.
6. Report the repos created, as links.

## Using them with snyk-onboard

`/snyk-onboard snyk-demo-go-1 snyk-demo-go-2 snyk-demo-node-1 --dry-run`. With private repos and the Snyk GitHub App limited to selected repos, a Go repo's import can fail until its dependency repos are also granted; `snyk_import.py import --fix-deps` handles that. The Go repo with public deps only is the control: it imports without needing access to other brightsheets repos.

## Removing them

The script never deletes. `python3 scripts/make_repos.py list` shows the repos it made. To remove them, the user runs `gh auth refresh -s delete_repo` and then `gh repo delete <owner>/<name> --yes` for each. Only suggest this when asked.
