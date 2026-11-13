# keezconsign

A Claude Code plugin marketplace.

| Command | What it does |
|---|---|
| `/snyk-onboard <repo> [repo ...]` | Onboards one or more GitHub repos (or `--org <org>`) to Snyk: detects the stack, imports through Snyk's GitHub integration into the `brightsheets` Snyk org, and opens a CI pull request. A bare name means `brightsheets/<name>`. [Details](plugins/snyk-onboard/skills/snyk-onboard/SKILL.md) |
| `/create-repo [count]` | Creates Go repos in `brightsheets`: `go-lib-01..NN` with `go.mod`/`go.sum` depending on `deps-01` and on each other, plus `go-standalone` with no deps. [Details](plugins/create-repo/skills/create-repo/SKILL.md) |

`/snyk-onboard --file repos.txt` reads the repos from a file instead: one per line, `#` for comments.

Add `--dry-run` to either one to see the plan without changing anything.

## Install

```
/plugin marketplace add brightsheets/snyk-plugin
/plugin install snyk-onboard@keezconsign
/plugin install create-repo@keezconsign
```

## Quick start

```
/create-repo 5 --dry-run
/snyk-onboard go-lib-01 go-lib-02 --dry-run
```

Sign in first: `gh auth login` and `snyk auth`. The default Snyk org is [`brightsheets`](https://app.snyk.io/org/brightsheets); set `SNYK_ORG` to use another.
