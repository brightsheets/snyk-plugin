#!/usr/bin/env bash
# Save a token into Claude Code's settings (~/.claude/settings.json -> "env") without it ever
# appearing on screen, in shell history or in the chat. Run it in your own terminal:
#
#   bash set_env_token.sh GH_APP_ADMIN_TOKEN     # classic GitHub PAT for adding repos to the Snyk app
#   bash set_env_token.sh SNYK_TOKEN             # Snyk API token
#
# The value is read with hidden input, the settings file is backed up and rewritten as JSON with
# only that one key changed, and its permissions are set to 600. Restart Claude Code afterwards.
# Remove the key again with:  bash set_env_token.sh --remove GH_APP_ADMIN_TOKEN
set -euo pipefail

REMOVE=0
[[ "${1:-}" == "--remove" ]] && { REMOVE=1; shift; }
NAME="${1:-}"
case "$NAME" in
  GH_APP_ADMIN_TOKEN|SNYK_TOKEN|GO_MODULES_TOKEN) ;;
  *) echo "Usage: bash $0 [--remove] GH_APP_ADMIN_TOKEN|SNYK_TOKEN|GO_MODULES_TOKEN" >&2; exit 2 ;;
esac

SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
mkdir -p "$(dirname "$SETTINGS")"

VALUE=""
if [[ $REMOVE -eq 0 ]]; then
  if [[ ! -t 0 ]]; then
    echo "Run this in an interactive terminal, so the token can be typed or pasted with hidden input." >&2
    exit 1
  fi
  case "$NAME" in
    GH_APP_ADMIN_TOKEN) echo "Create a classic token with the 'repo' scope (add 'admin:org' for organizations):"
                        echo "  https://github.com/settings/tokens/new?scopes=repo&description=snyk-onboard" ;;
    SNYK_TOKEN)         echo "Snyk token: https://app.snyk.io/account (Auth Token), or an org service account." ;;
    GO_MODULES_TOKEN)   echo "Token that can read your private Go module repos (fine-grained, Contents: read)." ;;
  esac
  read -r -s -p "Paste $NAME (input hidden): " VALUE; echo
  VALUE="${VALUE//[$'\t\r\n ']/}"
  [[ -z "$VALUE" ]] && { echo "Nothing entered; settings unchanged." >&2; exit 1; }
  if [[ "$NAME" == "GH_APP_ADMIN_TOKEN" && "$VALUE" != ghp_* ]]; then
    echo "Warning: classic GitHub tokens start with 'ghp_'. GitHub refuses fine-grained tokens (github_pat_...) for adding repos to an app." >&2
    read -r -p "Save it anyway? [y/N] " ok; [[ "$ok" == [yY]* ]] || { echo "Settings unchanged."; exit 1; }
  fi
fi

[[ -f "$SETTINGS" ]] && cp -p "$SETTINGS" "$SETTINGS.bak"
NAME="$NAME" VALUE="$VALUE" REMOVE="$REMOVE" SETTINGS="$SETTINGS" python3 - <<'PY'
import json, os
path = os.environ["SETTINGS"]
try:
    data = json.load(open(path)) if os.path.exists(path) and os.path.getsize(path) else {}
except ValueError as e:
    raise SystemExit(f"{path} isn't valid JSON ({e}); fix it first. Nothing changed.")
env = data.setdefault("env", {})
if os.environ["REMOVE"] == "1":
    env.pop(os.environ["NAME"], None)
else:
    env[os.environ["NAME"]] = os.environ["VALUE"]
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY
unset VALUE
if [[ $REMOVE -eq 1 ]]; then
  echo "Removed $NAME from $SETTINGS."
else
  echo "Saved $NAME to $SETTINGS (value not shown; backup at $SETTINGS.bak)."
fi
echo "Restart Claude Code so it takes effect."
