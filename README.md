# keezconsign

A Claude Code plugin marketplace.

| Command | What it does |
|---|---|
| `/snyk-onboard <repo> [repo ...]` | Onboards one or more GitHub repos (or `--org <org>`) to Snyk: detects the stack, imports through Snyk's GitHub integration into the `brightsheets` Snyk org, and opens a CI pull request. A bare name means `brightsheets/<name>`. [Details](plugins/snyk-onboard/skills/snyk-onboard/SKILL.md) |
| `/create-repo 5` · `/create-repo node 3` · `/create-repo java 2` | Creates demo repos in `brightsheets`. Go: `snyk-demo-go-1…4` with cross-org `go.mod` deps on `deps-01` and each other, and `snyk-demo-go-5` with public deps only. Node/Java: copies of Snyk's `nodejs-goof` / `java-goof`. [Details](plugins/create-repo/skills/create-repo/SKILL.md) |

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
/create-repo 5
/snyk-onboard snyk-demo-go-1 snyk-demo-go-2 --dry-run
```

Sign in first: `gh auth login` and `snyk auth`. The default Snyk org is [`brightsheets`](https://app.snyk.io/org/brightsheets); set `SNYK_ORG` to use another.
