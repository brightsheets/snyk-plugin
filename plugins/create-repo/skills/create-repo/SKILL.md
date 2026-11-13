---
name: create-repo
description: >-
  Create demo GitHub repos in brightsheets for Snyk testing. Go (default): snyk-demo-go-1, -2, ...
  whose go.mod files depend on brightsheets/deps-01 and on each other, with the last one having no
  deps. Node or Java: snyk-demo-node-N / snyk-demo-java-N, each a copy of snyk-labs/nodejs-goof or
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
  - Go: seeds deps-01 with a small Go package and tag v0.1.0, only if it is
    empty. Creates snyk-demo-go-N repos, pushes them and tags v0.1.0.
  - Node/Java: creates snyk-demo-node-N / snyk-demo-java-N, each holding the
    latest code of snyk-labs/nodejs-goof or snyk-labs/java-goof.
  - Adds the topic snyk-demo to each so they're easy to find.
  It never overwrites, force-pushes or deletes anything.
-->

# Create demo repos

**Usage:** `/create-repo 5` creates `snyk-demo-go-1` to `-5` in `brightsheets`, with Go deps added automatically. `/create-repo node 3` and `/create-repo java 2` create copies of Snyk's demo apps.

If the user only asks how to use it, reply with the usage line and stop.

Script: `scripts/make_repos.py` (run it from this skill's folder). Pass the user's words straight through as positional arguments: an optional language (`go`, `node`, `java`), then a number or repo names.

## What gets built

| Command | Repos |
|---|---|
| `/create-repo 5` (Go is the default) | `snyk-demo-go-1` … `-5`. Repos 1–4 each have a `go.mod` (and `go.sum` when Go is installed) that requires and calls `deps-01` plus the one or two repos before it. Repo 5 has no deps at all. Tagged `v0.1.0`. |
| `/create-repo node 3` | `snyk-demo-node-1` … `-3`, each a copy of the latest [snyk-labs/nodejs-goof](https://github.com/snyk-labs/nodejs-goof) (a deliberately vulnerable Node.js app). |
| `/create-repo java 2` | `snyk-demo-java-1` … `-2`, each a copy of the latest [snyk-labs/java-goof](https://github.com/snyk-labs/java-goof). |
| `/create-repo payments orders` | Those names. For Go, each gets cross deps. `--standalone NAME` adds a Go repo with no deps. |

- **Numbering:** numbers continue after the highest existing one (if `snyk-demo-go-5` exists, the next run starts at 6), so names never clash. A name the user gives that already exists stops the run.
- **Copies, not forks:** GitHub allows only one fork of a repo per account, and forks of public repos can't be private.
- **`deps-01`:** for Go, if `deps-01` is empty it is seeded first with a small Go package and tag `v0.1.0`.

## Options (rarely needed)

| Option | Meaning |
|---|---|
| `--public` | New repos are public. Default private. |
| `--owner` | GitHub user or org. Default `brightsheets`. |
| `--prefix` | Name prefix for numbered repos. Default `snyk-demo-<lang>`. |
| `--deps` | Go: shared module repo. Default `deps-01`. |
| `--dry-run` | Show the plan only. |

## Steps

1. Check that `gh auth status` shows the `repo` scope. For Go, run `command -v go` to see whether go.sum files will be written.
2. **Plan.** Run `python3 scripts/make_repos.py plan <args>` and show the user the table it prints. If it stops on an existing repo, report why. Never delete a repo to make room.
3. If `--dry-run`, stop here.
4. **Ask** with AskUserQuestion: "Create these N repos as <private|public> in <owner>?" Options: Create, Cancel. More than 100 per run isn't allowed, because GitHub throttles bulk creation.
5. **Create.** Run `python3 scripts/make_repos.py create <same args> --apply`. Go repos are created in dependency order, so each tag exists before a repo needs it.
6. Report the repos created, as links.

## Using them with snyk-onboard

`/snyk-onboard snyk-demo-go-1 snyk-demo-go-2 snyk-demo-node-1 --dry-run`. With private repos and the Snyk GitHub App limited to selected repos, a Go repo's import can fail until its dependency repos are also granted; `snyk_import.py import --fix-deps` handles that. The Go repo with no deps is the control: it should import cleanly.

## Removing them

The script never deletes. `python3 scripts/make_repos.py list` shows the repos it made. To remove them, the user runs `gh auth refresh -s delete_repo` and then `gh repo delete <owner>/<name> --yes` for each. Only suggest this when asked.
