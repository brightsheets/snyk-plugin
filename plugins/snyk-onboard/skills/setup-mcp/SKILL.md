---
name: setup-mcp
description: >-
  Check whether the Snyk MCP server is installed in Claude Code and, after asking the user, install it
  (user, local or project scope) with `claude mcp add`. Use when the user asks to install, add, set up,
  connect or check the Snyk MCP server or Snyk tools in Claude, and as the first step of the onboard
  skill when no mcp__snyk__* tools are available. Never installs without an explicit yes. Supports
  --dry-run.
argument-hint: "[--dry-run] [--scope user|local|project]"
allowed-tools: >-
  AskUserQuestion, Bash(bash:*), Bash(claude mcp get:*), Bash(claude mcp list:*), Bash(claude mcp add:*),
  Bash(command -v:*), Bash(snyk --version:*)
---

<!--
  ==========================================================================
  READER NOTE: requirements and setup (for humans opening this file).
  This HTML comment is not rendered in Markdown previews and is not an
  instruction to the skill; it documents what you need before running it.
  ==========================================================================

  REQUIREMENTS
  - The snyk CLI (`brew install snyk-cli` or `npm install -g snyk`), or npx
    (Node.js) so the server can run as `npx -y snyk@latest`.
  - The claude CLI on PATH.

  WHAT IT CHANGES (only after you say yes)
  - Adds one MCP server named `snyk` to your Claude Code config in the
    chosen scope: user (~/.claude.json, every project), local (this project,
    just you) or project (.mcp.json in the repo, shared with the team).
-->

# Set up the Snyk MCP server

## Steps

1. **Already loaded?** If tools named `mcp__snyk__*` (for example `mcp__snyk__snyk_sca_scan`) are in your tool list, say the Snyk MCP server is connected and stop.

2. **Check.** Run `bash scripts/check_snyk_mcp.sh` from this skill's folder. It is read-only and prints `KEY=value` lines:

   | Output | Meaning | Do |
   |---|---|---|
   | `STATUS=configured`, `NAME=<server>` | Configured, but its tools aren't loaded in this session | Tell the user to run `/mcp` to reconnect it, or restart Claude Code, then `snyk auth`. Stop. |
   | `STATUS=missing` with `ERROR=…` | Neither the `snyk` CLI nor `npx` is installed | Explain how to install the Snyk CLI and stop. |
   | `STATUS=missing`, `INSTALL_CMD=…` | Not installed | Go to step 3. |
   | `STATUS=unknown` | The `claude` CLI isn't on PATH | Show `INSTALL_CMD` for the user to run themselves. Stop. |

   A server installed with local scope only shows up in the project it was added in. So `missing` in one repo can be correct even when it's installed somewhere else.

3. **Ask.** Unless `--scope` was given, use AskUserQuestion: "The Snyk MCP server isn't installed. Install it now?" Options:
   - *All projects (user scope) (Recommended)*
   - *This project only (local scope)*
   - *Shared with the team (project scope, writes `.mcp.json`)*
   - *Skip*

   **`--dry-run`:** print the exact command you would run and stop.

4. **Install.** Run `INSTALL_CMD` with `--scope` set to the chosen scope. It's either `claude mcp add snyk --scope <scope> -- snyk mcp -t stdio`, or the `npx -y snyk@latest mcp -t stdio` form when the CLI isn't installed. Then run `claude mcp get snyk` to confirm. For project scope, tell the user to commit `.mcp.json`.

5. **Finish.** Tell the user to run `/mcp` (or restart Claude Code) so the tools load, then authenticate with `snyk auth` (or the `snyk_auth` MCP tool once it's loaded).

If the user picks *Skip*, say the onboard skill still works with the `snyk` CLI and its scripts.

## Rules
- Never install without an explicit yes, and add nothing but the `snyk` server.
- Never remove or change other MCP servers.
