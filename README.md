# keezconsign

A Claude Code plugin marketplace.

| Plugin | What it does |
|---|---|
| [snyk-onboard](plugins/snyk-onboard) | Onboards a GitHub repo, or a whole org, to Snyk: detects the stack, imports the repo through Snyk's GitHub integration, and opens a CI pull request. See its [SKILL.md](plugins/snyk-onboard/skills/onboard/SKILL.md). |

## Install

```
/plugin marketplace add brightsheets/snyk-plugin
/plugin install snyk-onboard@keezconsign
```

## First run

1. Sign in: `gh auth login` and `snyk auth`.
2. Fill the Snyk org list: `python3 plugins/snyk-onboard/skills/onboard/scripts/snyk_import.py refresh-orgs`
3. Onboard a repo: `/snyk-onboard:onboard owner/repo --dry-run`
