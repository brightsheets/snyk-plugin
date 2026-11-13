# keezconsign

A Claude Code plugin marketplace.

| Command | What it does |
|---|---|
| `/snyk-onboard <repo> [repo ...]` | Onboards one or more GitHub repos (or `--org <org>`) to Snyk: detects the stack, imports through Snyk's GitHub integration into the `brightsheets` Snyk org, and opens a CI pull request. A bare name means `brightsheets/<name>`. [Details](plugins/snyk-onboard/skills/snyk-onboard/SKILL.md) |
| `/create-repo` · `/create-repo 5` · `/create-repo node 3` · `/create-repo java 2` | Creates demo repos in `brightsheets`. Go (default 2): `snyk-demo-go-N` with cross-org `go.mod` deps on `deps-01`, plus one with public deps only; each gets a `.github/workflows/ci.yml` calling the shared pipeline. Node/Java: copies of Snyk's `nodejs-goof` / `java-goof`. [Details](plugins/create-repo/skills/create-repo/SKILL.md) |

`/snyk-onboard --file repos.txt` reads the repos from a file instead: one per line, `#` for comments.

Go repos get a CI workflow that calls the shared pipeline in [`brightsheets/reusable-workflows`](https://github.com/brightsheets/reusable-workflows): lint, tests, Snyk, and a Docker build + container scan when there's a Dockerfile.

Add `--dry-run` to either one to see the plan without changing anything.

## Install

```
/plugin marketplace add brightsheets/snyk-plugin
/plugin install snyk-onboard@keezconsign
/plugin install create-repo@keezconsign
```

## Quick start

```
/create-repo
/snyk-onboard snyk-demo-go-1 snyk-demo-go-2 --dry-run
```

Sign in first: `gh auth login` and `snyk auth`. Each GitHub org maps to the Snyk org with the same name: `brightsheets` → [`brightsheets`](https://app.snyk.io/org/brightsheets), `test-snyk-demo` → `test-snyk-demo`. Create the Snyk org (with its GitHub Cloud App integration) before onboarding that GitHub org.
