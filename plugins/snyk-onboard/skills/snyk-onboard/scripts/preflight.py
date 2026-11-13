#!/usr/bin/env python3
"""Progressive setup check for /snyk-onboard. It changes one thing only: when the Snyk MCP server is
missing and the snyk CLI is installed, it adds the server to Claude Code at user scope
(`claude mcp add snyk --scope user -- <snyk> mcp -t stdio`); pass --no-mcp-add to skip that.
Everything else is read-only.

Walks the prerequisites in order and stops at the first one that isn't met, printing the exact fix.
Fix it, rerun, and it moves on to the next stage:

  1. gh       GitHub CLI installed and signed in, with the repo and read:org scopes
  2. creds    Snyk credentials: SNYK_TOKEN, or the snyk CLI signed in (installs Node.js/CLI if missing)
  3. token    the Snyk token works (GET /rest/self)
  4. org      a Snyk org named like each repo's GitHub owner (or --snyk-org / SNYK_ORG)
  5. app      that Snyk org has the GitHub Cloud App integration (--wait polls while you install it)
  6. secrets  SNYK_TOKEN Actions secret on the repos (only needed for CI scans; warning, not a stop)
  7. mcp      Snyk MCP server in Claude Code: added automatically when missing (never a stop)

Usage:
  preflight.py --repo brightsheets/snyk-demo-node-1 [--repo test-snyk-demo/api] [--snyk-org <slug>]
  preflight.py --repo ... --wait [--wait-minutes 10]      # stage 5: wait for the app install
  preflight.py --repo ... --json                          # machine-readable result

Exit code 0 when every blocking stage passes, 1 otherwise. The last line is always
NEXT=<stage or "ready">.
"""
import argparse, json, os, shutil, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import snyk_import as si  # noqa: E402

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MCP_CHECK = os.path.join(SKILL_DIR, "..", "setup-mcp", "scripts", "check_snyk_mcp.sh")
SNYK_WEB = os.environ.get("SNYK_WEB", "https://app.snyk.io")
results = []


def report(stage, ok, msg, fix=None, blocking=True):
    mark = "✅" if ok else ("❌" if blocking else "⚠️ ")
    results.append({"stage": stage, "ok": ok, "blocking": blocking, "message": msg, "fix": fix})
    if not args.json:
        print(f"{mark} {stage:<8} {msg}")
        if fix and not ok:
            for line in fix.strip().splitlines():
                print(f"      {line}")


def sh(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr)
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)


def done(next_stage):
    if args.json:
        print(json.dumps({"results": results, "next": next_stage}, indent=2))
    print(f"NEXT={next_stage}")
    sys.exit(0 if next_stage == "ready" else 1)


# ---------- stages ----------

def stage_gh():
    if not shutil.which("gh"):
        report("gh", False, "GitHub CLI isn't installed.",
               "Install it (no sudo, into ~/.local/bin, checksum-verified):\n"
               "  bash scripts/install_tools.sh gh\n"
               "then sign in: ! gh auth login")
        return False
    rc, out = sh(["gh", "auth", "status"])
    if rc != 0:
        report("gh", False, "GitHub CLI isn't signed in.", "Run: ! gh auth login")
        return False
    scopes = out.split("Token scopes:", 1)[-1] if "Token scopes:" in out else ""
    missing = [s for s in ("repo", "read:org") if f"'{s}'" not in scopes]
    if scopes and missing:
        report("gh", False, f"GitHub token lacks scope(s): {', '.join(missing)}.",
               f"Run: ! gh auth refresh -h github.com -s {','.join(missing)}")
        return False
    login = sh(["gh", "api", "user", "--jq", ".login"])[1].strip()
    report("gh", True, f"signed in as {login}")
    return True


def stage_creds():
    if os.environ.get("SNYK_TOKEN"):
        report("creds", True, "SNYK_TOKEN is set (value not shown)")
        return True
    if shutil.which("snyk"):
        rc, out = sh(["snyk", "whoami", "--experimental"], timeout=60)
        if rc == 0:
            report("creds", True, f"snyk CLI signed in as {out.strip().splitlines()[-1] if out.strip() else '?'}")
            return True
        report("creds", False, "snyk CLI is installed but not signed in.",
               "Run: ! snyk auth        (opens a browser)\n"
               "Or put a token in Claude Code's settings: ~/.claude/settings.json -> \"env\": {\"SNYK_TOKEN\": \"...\"}, then restart.")
        return False
    report("creds", False, "No Snyk credentials: SNYK_TOKEN isn't set and the snyk CLI isn't installed.",
           "Either install the Snyk CLI (standalone binary, no Node.js or sudo, checksum-verified) and sign in:\n"
           "  bash scripts/install_tools.sh snyk\n  ! snyk auth\n"
           "or put a token in Claude Code's settings: ~/.claude/settings.json -> \"env\": {\"SNYK_TOKEN\": \"...\"}, then restart.\n"
           "Token: Snyk -> Account settings -> Auth Token (or an org service account for team use).")
    return False


def stage_token():
    try:
        si.auth_header()
        status, _, body = si.call("GET", "/rest/self", rest=True)
    except SystemExit as e:
        report("token", False, f"Couldn't get Snyk credentials: {e}", "Fix stage 'creds' first.")
        return False
    if status == 200:
        a = (body or {}).get("data", {}).get("attributes", {})
        report("token", True, f"Snyk API accepts the token ({a.get('username') or a.get('email') or a.get('name') or 'ok'}, {si.API})")
        return True
    if status == 401:
        fix = ("The token is wrong or expired. Make a new one (Snyk -> Account settings -> Auth Token) "
               "or run `! snyk auth` again. EU/AU tenants also set SNYK_API=https://api.eu.snyk.io (or api.au).")
    else:
        fix = f"Unexpected HTTP {status} from {si.API}/rest/self: {str(body)[:200]}"
    report("token", False, f"Snyk API rejected the token (HTTP {status}).", fix)
    return False


def stage_org(owners):
    """Returns {owner: (org_id, label)} for every owner whose Snyk org exists."""
    found, ok = {}, True
    for owner in owners:
        key = args.snyk_org or si.DEFAULT_ORG or owner
        try:
            oid = si.org_id_for(key)
            found[owner] = (oid, key)
            how = "--snyk-org" if args.snyk_org else ("SNYK_ORG" if si.DEFAULT_ORG else "same name as the GitHub owner")
            report("org", True, f"GitHub {owner} -> Snyk org {key} ({how})")
        except LookupError:
            ok = False
            report("org", False, f"No Snyk org named '{key}' for GitHub owner {owner} (or the token can't see it).",
                   f"Create it: {SNYK_WEB} -> org switcher -> Create organization, name it exactly '{key}'.\n"
                   "If it exists with a different URL name, its display name still matches; check the token's access.\n"
                   "Some Snyk plans allow one org only: then use --snyk-org <existing-org>.")
    return found if ok else None


def stage_app(orgs):
    ok_all = True
    for owner, (oid, label) in orgs.items():
        url = f"{SNYK_WEB}/org/{label}/integrations/github-cloud-app"
        deadline = time.time() + (args.wait_minutes * 60 if args.wait else 0)
        while True:
            status, _, ints = si.call("GET", f"/v1/org/{oid}/integrations")
            ints = ints if status == 200 and isinstance(ints, dict) else {}
            if ints.get("github-cloud-app"):
                report("app", True, f"Snyk org {label} has the GitHub Cloud App integration")
                check_admin_token(owner)
                break
            if time.time() >= deadline:
                other = [t for t in ("github", "github-enterprise") if ints.get(t)]
                note = (f" It has the older '{other[0]}' (OAuth/PAT) integration; imports would work through it, "
                        "but the GitHub Cloud App is the recommended one.") if other else ""
                report("app", False, f"Snyk org {label} has no GitHub Cloud App integration.{note}",
                       f"1. Open {url}\n"
                       f"2. Click Add/Connect; on GitHub choose '{owner}', pick repository access, Install & Authorize.\n"
                       f"3. Check on GitHub: https://github.com/{'settings' if owner_is_user(owner) else 'organizations/' + owner + '/settings'}/installations\n"
                       "Rerun with --wait to have this check poll until the install shows up.",
                       blocking=not other)
                ok_all = ok_all and bool(other)
                break
            if not args.json:
                print(f"   … waiting for the GitHub Cloud App in Snyk org {label} (install it at {url})", flush=True)
            time.sleep(10)
    return ok_all


SET_TOKEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "set_env_token.sh")


def token_steps(owner, user, scopes, create):
    """The exact message shown when GH_APP_ADMIN_TOKEN is missing or unusable. The skill prints it verbatim."""
    who = (f"from the {owner} account" if user else
           f"from an owner of the {owner} org or a Snyk app manager, authorized for {owner}'s SSO if it uses SSO")
    return (f"- Token: create a new token or use an existing one: classic (ghp_...), scope {' + '.join(scopes)} only, "
            f"{who}\n"
            "- Run: export GH_APP_ADMIN_TOKEN=<your-token>\n"
            "Then type \"done\" when it's completed.")


def check_admin_token(owner):
    """GH_APP_ADMIN_TOKEN lets the importer add repos to the Snyk app. Warn (never block) if missing or wrong."""
    user = owner_is_user(owner)
    scopes_needed = ["repo"] if user else ["repo", "admin:org"]
    scope_qs = ",".join(scopes_needed)
    create = f"https://github.com/settings/tokens/new?scopes={scope_qs}&description=snyk-onboard"
    who = f"a classic token of {owner}" if user else f"a classic token of an owner of {owner} (or a Snyk app manager)"
    setter = f"bash {SET_TOKEN} GH_APP_ADMIN_TOKEN"
    token = os.environ.get("GH_APP_ADMIN_TOKEN")
    if not token:
        report("app", False, "GH_APP_ADMIN_TOKEN isn't set.", token_steps(owner, user, scopes_needed, create),
               blocking=False)
        return
    import urllib.request, urllib.error
    req = urllib.request.Request("https://api.github.com/user", headers={
        "Authorization": f"token {token}", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            have = {x.strip() for x in (r.headers.get("X-OAuth-Scopes") or "").split(",") if x.strip()}
            classic = r.headers.get("X-OAuth-Scopes") is not None
            login = json.loads(r.read()).get("login", "?")
    except urllib.error.HTTPError as e:
        report("app", False, f"GH_APP_ADMIN_TOKEN is set but GitHub rejects it (HTTP {e.code}).",
               token_steps(owner, user, scopes_needed, create), blocking=False)
        return
    missing = [sc for sc in scopes_needed if sc not in have and not (sc == "admin:org" and "write:org" in have)]
    if not classic:
        report("app", False, f"GH_APP_ADMIN_TOKEN ({login}) is a fine-grained or app token; GitHub only accepts a "
               "classic token for adding repos to an app installation.",
               token_steps(owner, user, scopes_needed, create), blocking=False)
    elif missing:
        report("app", False, f"GH_APP_ADMIN_TOKEN ({login}) lacks scope(s): {', '.join(missing)}.",
               token_steps(owner, user, scopes_needed, create), blocking=False)
    else:
        report("app", True, f"GH_APP_ADMIN_TOKEN is a classic token of {login} with {', '.join(sorted(have & set(scopes_needed)))}: "
               "repos are added to the Snyk app automatically")


_user_cache = {}


def owner_is_user(owner):
    if owner not in _user_cache:
        _user_cache[owner] = sh(["gh", "api", f"users/{owner}", "--jq", ".type"])[1].strip() == "User"
    return _user_cache[owner]


def stage_secrets(repos):
    for repo in repos:
        owner = repo.split("/")[0]
        rc, out = sh(["gh", "secret", "list", "--repo", repo])
        has = rc == 0 and any(l.split("\t")[0] == "SNYK_TOKEN" for l in out.splitlines())
        if not has and not owner_is_user(owner):
            rc2, out2 = sh(["gh", "secret", "list", "--org", owner])
            has = rc2 == 0 and any(l.split("\t")[0] == "SNYK_TOKEN" for l in out2.splitlines())
        if has:
            report("secrets", True, f"{repo}: SNYK_TOKEN secret available to Actions")
        else:
            cmd = (f"! gh secret set SNYK_TOKEN --repo {repo}" if owner_is_user(owner)
                   else f"! gh secret set SNYK_TOKEN --org {owner} --visibility all")
            report("secrets", False, f"{repo}: no SNYK_TOKEN secret, so CI Snyk scans are skipped (the import still works).",
                   f"Run: {cmd}", blocking=False)


def stage_mcp():
    if not os.path.exists(MCP_CHECK):
        return
    rc, out = sh(["bash", MCP_CHECK], timeout=90)
    kv = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    if kv.get("STATUS") in ("configured", "connected"):
        report("mcp", True, f"Snyk MCP server configured in Claude Code ({kv.get('NAME', 'snyk')})", blocking=False)
        return
    snyk, claude = shutil.which("snyk"), shutil.which("claude")
    if not snyk:
        report("mcp", False, "Snyk MCP server not set up; it's added automatically once the snyk CLI is installed.",
               "Install the CLI (stage 'creds'): bash scripts/install_tools.sh snyk", blocking=False)
        return
    cmd = ["claude", "mcp", "add", "snyk", "--scope", "user", "--", snyk, "mcp", "-t", "stdio"]
    if args.no_mcp_add or not claude:
        report("mcp", False, "Snyk MCP server not set up.",
               "Add it with: ! " + " ".join(cmd), blocking=False)
        return
    rc, out = sh(cmd, timeout=60)
    ok = rc == 0 and sh(["claude", "mcp", "get", "snyk"], timeout=60)[0] == 0
    if ok:
        report("mcp", True, f"Added the Snyk MCP server to Claude Code (user scope, {snyk} mcp -t stdio). "
               "Run /mcp or restart Claude Code to load its tools.", blocking=False)
    else:
        report("mcp", False, f"Couldn't add the Snyk MCP server: {out.strip()[:200]}",
               "Add it by hand: ! " + " ".join(cmd), blocking=False)


# ---------- main ----------

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--repo", action="append", default=[], help="owner/repo or bare name (repeatable)")
ap.add_argument("--file", action="append", help="file of repo names")
ap.add_argument("--snyk-org", help="Snyk org to use for every repo instead of the same-name rule")
ap.add_argument("--wait", action="store_true", help="poll until the GitHub Cloud App integration appears")
ap.add_argument("--wait-minutes", type=float, default=10)
ap.add_argument("--json", action="store_true")
ap.add_argument("--no-mcp-add", action="store_true", help="don't add the Snyk MCP server when it's missing")
args = ap.parse_args()

repos = si.split_repos(args.repo, args.file)
owners = sorted({r.split("/")[0] for r in repos}, key=str.lower)
if not args.json:
    print(f"Preflight for {len(repos)} repo(s): {', '.join(repos) or '(none: org-level checks skipped)'}\n")

if not stage_gh():
    done("gh")
if not stage_creds():
    stage_mcp()
    done("creds")
if not stage_token():
    done("token")
if owners:
    orgs = stage_org(owners)
    if orgs is None:
        done("org")
    if not stage_app(orgs):
        done("app")
    stage_secrets(repos)
stage_mcp()
done("ready")
