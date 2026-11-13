#!/usr/bin/env bash
# Reports whether a Snyk MCP server is configured for Claude Code, and the
# exact command to install one if not. Read-only.
#   Output (one line each):  STATUS=configured|missing  NAME=<server>  INSTALL_CMD=<cmd>
set -u

if command -v snyk >/dev/null 2>&1; then
  SERVER_CMD="snyk mcp -t stdio"
elif command -v npx >/dev/null 2>&1; then
  SERVER_CMD="npx -y snyk@latest mcp -t stdio"
else
  echo "STATUS=missing"
  echo "ERROR=Neither the snyk CLI nor npx is installed. Install one first: 'brew install snyk-cli' or 'npm install -g snyk'."
  exit 0
fi
INSTALL_CMD="claude mcp add snyk --scope user -- ${SERVER_CMD}"

if ! command -v claude >/dev/null 2>&1; then
  echo "STATUS=unknown"
  echo "ERROR=claude CLI not on PATH; check the session's tool list for mcp__snyk__* tools instead."
  echo "INSTALL_CMD=${INSTALL_CMD}"
  exit 0
fi

# Fast path: a server literally named "snyk"
if claude mcp get snyk >/dev/null 2>&1; then
  echo "STATUS=configured"; echo "NAME=snyk"; exit 0
fi
# Slower: any server whose name or command mentions snyk
line=$(claude mcp list 2>/dev/null | grep -i 'snyk' | head -1)
if [ -n "$line" ]; then
  echo "STATUS=configured"; echo "NAME=${line%%:*}"; exit 0
fi

echo "STATUS=missing"
echo "INSTALL_CMD=${INSTALL_CMD}"
