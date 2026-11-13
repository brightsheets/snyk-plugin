# keezconsign

A Claude Code plugin marketplace for onboarding GitHub repos to Snyk.

| Command | What it does |
|---|---|
| `/snyk-onboard <org>/<repo> [<org>/<repo> ...]` | Imports repos into Snyk through the GitHub Cloud App integration, each into the Snyk org named like its GitHub org, and (unless `--no-ci`) opens a PR adding Snyk CI. [Details](plugins/snyk-onboard/skills/snyk-onboard/SKILL.md) |
| `/create-repo [go\|node\|java] [count \| names]` | Creates demo repos for Snyk testing, each with a CI workflow calling [`brightsheets/reusable-workflows`](https://github.com/brightsheets/reusable-workflows). [Details](plugins/create-repo/skills/create-repo/SKILL.md) |

Add `--dry-run` to either command to see the plan without changing anything. Both plan first and ask before writing.

## Install

```
/plugin marketplace add brightsheets/snyk-plugin
/plugin install snyk-onboard@keezconsign
/plugin install create-repo@keezconsign
```

Update later with `/plugin marketplace update keezconsign`, then `/reload-plugins`.

## Onboard repos: `/snyk-onboard`

```
/snyk-onboard <github-org>/<repo> [<github-org>/<repo> ...] [--no-ci] [--dry-run]
```

```
/snyk-onboard brightsheets/snyk-demo-node-1 --no-ci
/snyk-onboard brightsheets/snyk-demo-node-1 test-snyk-demo/payments-api --no-ci --dry-run
/snyk-onboard --file repos.txt                 # one <org>/<repo> per line, '#' comments, orgs can be mixed
/snyk-onboard --org test-snyk-demo --dry-run   # every repo in a GitHub org (--filter <regex> to narrow)
/snyk-onboard snyk-demo-node-1                 # a bare name means brightsheets/<name>
```

**GitHub org → Snyk org.** Each repo goes to the Snyk org with the same name as its GitHub org. Repos from different orgs can be onboarded in one command; the plan groups them by Snyk org.

| Repo | Imported into Snyk org |
|---|---|
| `brightsheets/snyk-demo-node-1` | [`brightsheets`](https://app.snyk.io/org/brightsheets) |
| `test-snyk-demo/payments-api` | `test-snyk-demo` |
| `acme-corp/api` (no Snyk org named `acme-corp`) | not imported; the plan says why |

Override for one run with `--snyk-org <slug|id>`. Setting `SNYK_ORG` sends every unmapped owner to that one org, so leave it unset to keep the same-name rule.

| Option | Meaning |
|---|---|
| `--no-ci` | Import into Snyk only. Use it for repos that already run Snyk in CI (the `/create-repo` demo repos do). |
| `--dry-run` | Show the plan: detected stack, Snyk products, Snyk org and integration per repo. Imports nothing. |
| `--file <path>` / `@path` | Read repos from a file. |
| `--org <github-org>` | Every repo in that GitHub org (archived, forks and empty repos skipped). |
| `--severity <level>` | Failure threshold for the generated CI: `low`, `medium`, `high` (default), `critical`. |

### Setup is checked for you, one stage at a time

Every `/snyk-onboard` run starts with a setup check (`scripts/preflight.py`). It stops at the first thing that's missing, says how to fix it, and continues from there on the next run:

| # | Stage | If it's missing |
|---|---|---|
| 1 | GitHub CLI installed and signed in (`repo`, `read:org`) | Offers to install `gh` into `~/.local/bin` (no sudo, checksum-verified), then `gh auth login` |
| 2 | Snyk credentials | Offers to install the Snyk CLI (standalone, no Node.js), then `snyk auth`; or `SNYK_TOKEN` in `~/.claude/settings.json` |
| 3 | Snyk accepts the token | New token, `snyk auth` again, or `SNYK_API` for EU/AU |
| 4 | A Snyk org named like each GitHub org | Steps to create it |
| 5 | GitHub Cloud App integration in that Snyk org | The install link, and it can wait (`--wait`) while you click Install |
| 6 | `SNYK_TOKEN` Actions secret on the repos | Warning with the `gh secret set` command (only CI needs it) |
| 7 | Snyk MCP server | Added to Claude Code automatically (user scope) once the Snyk CLI is installed; `--no-mcp-add` skips it |

Run it on its own: `python3 scripts/preflight.py --repo brightsheets/snyk-demo-node-1 [--wait]`. Install the CLIs by hand: `bash scripts/install_tools.sh gh snyk`.

### PR checks and fix PRs

After an import, `/snyk-onboard` offers to turn on the GitHub Cloud App features in that Snyk org: PR checks (dependencies and code, failing only on high/critical), fix PRs for new and existing vulnerabilities, and dependency upgrade PRs. It shows the current and proposed values and asks first. By hand:

```
python3 scripts/snyk_import.py integration-settings --repo brightsheets/snyk-demo-node-1 --preset recommended          # show
python3 scripts/snyk_import.py integration-settings --repo brightsheets/snyk-demo-node-1 --preset recommended --apply  # save
```

Presets: `recommended`, `checks-only`; single settings with `--set key=value`. The settings apply to every repo in that Snyk org.

### Before onboarding a GitHub org

| Step | Where |
|---|---|
| A Snyk org named exactly like the GitHub org | Snyk → org switcher → Create organization |
| The **GitHub Cloud App** integration in that Snyk org, installed on the GitHub org (one click by a GitHub org owner; it can't be automated) | Snyk → Integrations → GitHub Cloud App |
| A Snyk token that can see that org, available as `SNYK_TOKEN` (or `snyk auth`) | Snyk → Account settings → Auth Token, or an org service account |
| `SNYK_TOKEN` as a GitHub Actions secret, for the CI scans | `gh secret set SNYK_TOKEN --org <org> --visibility all` (orgs) or `--repo <owner/repo>` (personal accounts) |
| `gh auth login` with the `repo` and `read:org` scopes | terminal |

If the app is installed on **selected repositories** only, the importer adds each repo to it when `GH_APP_ADMIN_TOKEN` is set: a classic PAT of an org owner (`repo` + `admin:org`, SSO-authorized) for an organization, or of the account itself (`repo`) for a personal account. `gh`'s own token can't do this. Without it, the importer tells you which repos to add by hand. Public repos import even without app access, but PR checks and fix PRs only work once the repo is in the app.

## Create demo repos: `/create-repo`

| Command | Creates (in `brightsheets` by default) |
|---|---|
| `/create-repo` | 2 Go repos: `snyk-demo-go-N` with cross-org `go.mod` deps on `deps-01`, and `snyk-demo-go-N+1` with public deps only |
| `/create-repo 5` | 5 Go repos: 4 with cross-org deps, the last with public deps only |
| `/create-repo payments orders` | Go repos with those names, each with cross-org deps |
| `/create-repo node 3` | `snyk-demo-node-1…3`: copies of [snyk-labs/nodejs-goof](https://github.com/snyk-labs/nodejs-goof) |
| `/create-repo java 2` | `snyk-demo-java-1…2`: copies of [snyk-labs/java-goof](https://github.com/snyk-labs/java-goof) |

- `--owner <github-org>` creates them in another org; their CI scans into the Snyk org of the same name.
- `--public` makes them public (default private). GitHub's Security tab (SARIF upload) is free only on public repos.
- Numbers continue after the highest existing one, so names never clash. `deps-01` is created and tagged `v1.0.0` automatically when missing.
- Go repos get `.github/workflows/ci.yml` calling `go-ci.yaml@v1`; Node copies get one calling `js-ci.yaml@v1` (build, Snyk, Docker build + Snyk Container, never pushing the image). Upstream workflows in the copies are removed.

## Shared CI: `brightsheets/reusable-workflows`

The callers point at [`brightsheets/reusable-workflows`](https://github.com/brightsheets/reusable-workflows) `@v1`:

| Pipeline | Steps |
|---|---|
| `go-ci.yaml` | lint → tests → Snyk Open Source + Code → Docker build → Snyk Container |
| `js-ci.yaml` | build → Snyk Open Source + Code → Docker build → Snyk Container |

Snyk is report-only by default (findings in the Security tab and a PR comment; `snyk-fail-on-issues: true` makes it a gate), publishes to Snyk (`snyk monitor`) only from the default branch and `hotfix/*`, and is skipped with a warning when no `SNYK_TOKEN` secret is set.

Projects from CI show in Snyk under the **CLI** integration; projects from `/snyk-onboard` show under **GitHub Cloud App**. Having both is normal: Git integration gives visibility and fix PRs, CI scans the built image and gates the build.
