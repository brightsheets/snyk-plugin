#!/usr/bin/env python3
"""Import GitHub repos into Snyk through a Snyk org's existing SCM integration.

The Snyk org and integration for a repo come from references/snyk-orgs.json
(GitHub owner -> Snyk org -> integration). DRY RUN by default: pass --apply to
create Snyk projects. This script never creates or changes Snyk integrations.

For github-cloud-app imports where the Snyk GitHub App is installed on selected
repositories only, --apply first adds the repo to the app's installation (needs
GH_APP_ADMIN_TOKEN). After each import it waits for the Snyk job and prints
per-project results, and turns failures (401/403/404/SSO/no manifests/...) into
recommendations.

Usage:
  snyk_import.py orgs                                   # Snyk orgs the credentials can see
  snyk_import.py integrations --org-id <uuid>           # one org's integrations (live)
  snyk_import.py resolve --repo owner/name              # which Snyk org + integration a repo maps to
  snyk_import.py refresh-orgs [--out <file>]            # rebuild references/snyk-orgs.json from Snyk
  snyk_import.py import --repo owner/name [repo2 ...] [--branch b] [--org-id <uuid|slug>] [--apply]
                                                        # a bare name means $GITHUB_OWNER/name (default brightsheets)
  snyk_import.py import --file repos.txt [--apply]      # or --repo @repos.txt: one repo per line, '#' comments
  snyk_import.py repos --file repos.txt                 # just print the expanded list
  snyk_import.py import --plan snyk-onboarding-plan.json [--org-id <uuid|slug>] [--apply]

Env:
  SNYK_TOKEN   service-account or personal API token (falls back to the snyk CLI OAuth login)
  GH_APP_ADMIN_TOKEN  classic GitHub PAT (repo scope, SSO-authorized) of an org owner or Snyk app
               manager; needed to list/add repos on a selected-repos app installation
  SNYK_ORG     Snyk org (id or slug) for every unmapped owner. Unset: the Snyk org named like the GitHub owner
  SNYK_API     API base, default https://api.snyk.io  (EU: https://api.eu.snyk.io, AU: https://api.au.snyk.io)
"""
import argparse, collections, datetime, json, os, re, subprocess, sys, time
import urllib.error, urllib.parse, urllib.request

API = os.environ.get("SNYK_API", "https://api.snyk.io").rstrip("/")
REST_VERSION = "2024-10-15"
# Snyk org per repo: --org-id, else the mapping file, else the Snyk org whose slug or name equals the
# repo's GitHub owner (brightsheets -> brightsheets, test-snyk-demo -> test-snyk-demo). SNYK_ORG, if
# set, replaces the same-name rule for every unmapped owner. Nothing ever falls back to another org.
DEFAULT_ORG = os.environ.get("SNYK_ORG", "")
DEFAULT_OWNER = os.environ.get("GITHUB_OWNER", "brightsheets")


def read_repo_file(path):
    """Repo names from a file: one per line or comma separated; '#' starts a comment."""
    try:
        text = open(os.path.expanduser(path)).read()
    except OSError as e:
        sys.exit(f"Can't read repo file {path}: {e.strerror}")
    return [line.split("#", 1)[0] for line in text.splitlines()]


def split_repos(values, files=()):
    """['a', 'o/b,c', '@list.txt'] -> ['brightsheets/a', 'o/b', 'brightsheets/c', ...], duplicates dropped."""
    raw = []
    for v in values or []:
        raw += read_repo_file(v[1:]) if v.startswith("@") else [v]
    for f in files or []:
        raw += read_repo_file(f)
    out = []
    for v in raw:
        for r in filter(None, (x.strip() for x in v.split(","))):
            r = r.removeprefix("https://github.com/").removesuffix(".git").strip("/")
            r = r if "/" in r else f"{DEFAULT_OWNER}/{r}"
            if r.count("/") != 1:
                sys.exit(f"Not a repo name: {r!r} (use owner/name or a bare name)")
            if r not in out:
                out.append(r)
    return out
SCM_TYPES = ["github-cloud-app", "github", "github-enterprise"]  # preference order; never cli/ecr/etc.
DEFAULT_EXCLUDES = "fixtures, tests, __tests__, node_modules, vendor, third_party, Intermediate, Binaries, Library"
MAPPING = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "references", "snyk-orgs.json")

_AUTH = None


def auth_header():
    """SNYK_TOKEN if set, else the snyk CLI's OAuth login (`snyk auth`). Never printed."""
    global _AUTH
    if _AUTH:
        return _AUTH
    token = os.environ.get("SNYK_TOKEN")
    if token:
        _AUTH = f"token {token}"
        return _AUTH
    try:
        subprocess.run(["snyk", "whoami", "--experimental"], capture_output=True, timeout=60)  # refreshes OAuth
        raw = subprocess.run(["snyk", "config", "get", "INTERNAL_OAUTH_TOKEN_STORAGE"],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        access = json.loads(raw).get("access_token") if raw else None
    except (OSError, ValueError, subprocess.SubprocessError):
        access = None
    if not access:
        sys.exit("No Snyk credentials. Export SNYK_TOKEN (service account or personal token) or run `snyk auth`.")
    _AUTH = f"bearer {access}"
    return _AUTH


def call(method, path, body=None, rest=False):
    url = path if path.startswith("http") else f"{API}{path}"
    if rest and "version=" not in url:
        url += ("&" if "?" in url else "?") + f"version={REST_VERSION}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": auth_header(),
               "Content-Type": "application/vnd.api+json" if rest else "application/json"}
    for attempt in range(5):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
                raw = r.read()
                return r.status, dict(r.headers), (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 4:
                time.sleep(2 ** attempt * 5)
                continue
            return e.code, dict(e.headers), {"error": e.read().decode(errors="replace")}


def rest_all(path, max_pages=50):
    """GET every page of a REST collection."""
    items, url, pages = [], path, 0
    while url and pages < max_pages:
        status, _, body = call("GET", url, rest=True)
        if status != 200:
            raise RuntimeError(f"GET {url} -> HTTP {status}: {body}")
        items += body.get("data", [])
        nxt = body.get("links", {}).get("next")
        url = (nxt if nxt.startswith("http") else (API + nxt if nxt.startswith("/rest") else API + "/rest" + nxt)) if nxt else None
        pages += 1
    return items


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def org_id_for(key):
    """Snyk org UUID for an id or slug. The v1 import API needs the UUID."""
    if UUID_RE.match(key):
        return key
    for o in rest_all(f"/rest/orgs?slug={urllib.parse.quote(key)}&limit=10"):
        if o["attributes"].get("slug") == key:
            return o["id"]
    # Slug lookups are exact; also accept an org whose display name matches (case-insensitive).
    for o in rest_all("/rest/orgs?limit=100"):
        a = o["attributes"]
        if key.lower() in ((a.get("slug") or "").lower(), (a.get("name") or "").lower()):
            return o["id"]
    raise LookupError(f"No Snyk org named '{key}' is visible to these credentials (see `snyk_import.py orgs`). "
                      f"Create it in Snyk, or give this token access to it.")


def live_integrations(org_id):
    org_id = org_id_for(org_id)
    status, _, body = call("GET", f"/v1/org/{org_id}/integrations")
    if status != 200:
        sys.exit(f"Could not list integrations for {org_id} (HTTP {status}): {body}")
    return body


# ---------- mapping file ----------

def load_mapping():
    try:
        return json.load(open(MAPPING))
    except (OSError, ValueError):
        return {"orgs": []}


def find_org(mapping, key):
    """key = Snyk org id or slug."""
    for o in mapping["orgs"]:
        if key in (o["id"], o["slug"]):
            return o
    return None


def resolve(mapping, owner, org_key=None, integration=None):
    """Return (org, integration_type, integration_id, reason) for a GitHub owner."""
    if org_key:
        org = find_org(mapping, org_key)
        if not org:  # unknown to the mapping: use it live
            try:
                oid = org_id_for(org_key)
            except LookupError as e:
                return None, None, None, str(e)
            org = {"id": oid, "slug": org_key, "integrations": live_integrations(oid), "github_orgs": {}}
        candidates = [org]
    else:
        lower = owner.lower()
        candidates = [o for o in mapping["orgs"] if lower in {g.lower() for g in o.get("github_orgs", {})}]
        if not candidates:  # same-name rule: GitHub owner X -> Snyk org X (or SNYK_ORG if set)
            org, itype, iid, why = resolve(mapping, owner, DEFAULT_ORG or owner, integration)
            if org and why and not why.startswith("Snyk org"):
                why += f"; Snyk org matched by {'SNYK_ORG' if DEFAULT_ORG else 'GitHub owner name'}"
            return org, itype, iid, why
        if len(candidates) > 1:
            return None, None, None, f"'{owner}' maps to several Snyk orgs ({[o['slug'] for o in candidates]}); pass --org-id"
    org = candidates[0]
    ints = org["integrations"]
    owner_map = {g.lower(): t for g, t in org.get("github_orgs", {}).items()}
    order = [integration] if integration else [owner_map.get(owner.lower())] + SCM_TYPES
    for t in order:
        if t and t in SCM_TYPES and ints.get(t):
            why = "explicit" if integration else ("used by this owner's existing projects" if t == owner_map.get(owner.lower()) else "first SCM integration available")
            return org, t, ints[t], why
    return org, None, None, f"Snyk org {org['slug']} has no GitHub SCM integration (has: {sorted(ints)}); configure one in Snyk > Settings > Integrations"


# ---------- GitHub side: Snyk GitHub App installation ----------
# Listing / adding an installation's repositories is only allowed with a classic PAT (repo scope,
# SSO-authorized) of an org owner or app manager. `gh`'s own OAuth token is refused by GitHub for those
# endpoints, so they use GH_APP_ADMIN_TOKEN when set. Everything else goes through `gh api`.

GH_API = "https://api.github.com"
_INSTALL_CACHE = {}


def gh(method, path, token=None):
    """Return (status, data). Uses GH_APP_ADMIN_TOKEN-style token if given, else the gh CLI."""
    if token:
        req = urllib.request.Request(f"{GH_API}/{path.lstrip('/')}", method=method, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urllib.request.urlopen(req) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            return e.code, {"message": e.read().decode(errors="replace")}
    r = subprocess.run(["gh", "api", "-X", method, path], capture_output=True, text=True)
    if r.returncode == 0:
        return 200, (json.loads(r.stdout) if r.stdout.strip() else None)
    m = re.search(r"HTTP (\d{3})", r.stderr)
    return (int(m.group(1)) if m else 0), {"message": r.stderr.strip()}


def snyk_app_installation(owner):
    """Find the Snyk GitHub App installation on a GitHub org. Cached per owner."""
    if owner in _INSTALL_CACHE:
        return _INSTALL_CACHE[owner]
    token = os.environ.get("GH_APP_ADMIN_TOKEN")
    ostatus, odata = gh("GET", f"users/{owner}", token)
    if ostatus == 200 and (odata or {}).get("type") == "User":
        # Personal accounts: GitHub has no endpoint that lists a user's app installations with a PAT
        # (only with the app's own user token), so access can't be checked ahead of time.
        res = {"state": "personal", "settings_url": "https://github.com/settings/installations"}
        _INSTALL_CACHE[owner] = res
        return res
    status, data = gh("GET", f"orgs/{owner}/installations?per_page=100", token)
    if status != 200:
        res = {"state": "unknown", "status": status, "detail": ((data or {}).get("message", "") or "").splitlines()[0][:160] if (data or {}).get("message") else ""}
    else:
        inst = [i for i in data.get("installations", []) if "snyk" in i.get("app_slug", "").lower()]
        if not inst:
            res = {"state": "not-installed"}
        else:
            i = inst[0]
            res = {"state": "found", "id": i["id"], "app_slug": i["app_slug"],
                   "selection": i.get("repository_selection"),
                   "settings_url": f"https://github.com/organizations/{owner}/settings/installations/{i['id']}"}
    _INSTALL_CACHE[owner] = res
    return res


REFUSED_APP_STATES = ("unknown", "not-installed", "repo-not-found", "selected-unverified", "add-failed")


def highlight(msg):
    """Red, bold banner on a TTY; plain banner otherwise."""
    if sys.stdout.isatty() and not os.environ.get("NO_COLOR"):
        return f"     \033[1;41;97m {msg} \033[0m"
    return f"     !!! {msg} !!!"


def app_access_fixes(owner, name, detail="", settings_url=None):
    """Fixes for when the script can't confirm or grant the Snyk app's access to owner/name."""
    url = settings_url or (f"https://github.com/organizations/{owner}/settings/installations"
                           if gh("GET", f"orgs/{owner}")[0] == 200 else "https://github.com/settings/installations")
    fixes = []
    if "SAML" in detail or "SSO" in detail:
        fixes.append(f"Authorize GH_APP_ADMIN_TOKEN for {owner}'s SSO: github.com/settings/tokens > the token > "
                     f"Configure SSO > Authorize {owner}. Then rerun.")
    fixes += [
        f"Add {owner}/{name} to the Snyk GitHub App by hand: {url} > Snyk > Configure > Repository access "
        "(needs an org owner or Snyk app manager). Then rerun.",
        f"Give the token elevated access: GH_APP_ADMIN_TOKEN must be a classic PAT (repo + admin:org scopes) of an "
        f"owner of {owner} or a manager of the Snyk app, SSO-authorized for {owner}. GitHub refuses gh's own token here.",
        "If the repo is already in the app's access, rerun with --no-grant-app to skip this check.",
    ]
    return fixes


_PROJECTS_CACHE = {}


def existing_projects(org_id, owner, name):
    """Live list of the org's Snyk projects for owner/name, as (origin, type, name). Cached per org."""
    if org_id not in _PROJECTS_CACHE:
        try:
            _PROJECTS_CACHE[org_id] = [p["attributes"] for p in rest_all(f"/rest/orgs/{org_id}/projects?limit=100")]
        except RuntimeError as e:
            print(f"     (couldn't list existing Snyk projects: {e})")
            _PROJECTS_CACHE[org_id] = None
    rows = _PROJECTS_CACHE[org_id]
    if rows is None:
        return None
    full = f"{owner}/{name}".lower()
    return [(a.get("origin", ""), a.get("type", ""), a.get("name", "")) for a in rows
            if (a.get("name", "").lower() == full or a.get("name", "").lower().startswith(full + ":")
                or a.get("name", "").lower().startswith(full + "("))]


def default_branch(owner, name):
    status, data = gh("GET", f"repos/{owner}/{name}")
    return (data or {}).get("default_branch") if status == 200 else None


def ensure_app_access(owner, name, apply):
    """For github-cloud-app imports: make sure the Snyk app can see owner/name.

    Returns {"state": ..., "detail": ..., "recommendations": [...], "blocking": bool}.
    """
    inst = snyk_app_installation(owner)
    recs = []
    if inst["state"] == "personal":
        return {"state": "personal", "blocking": False,
                "detail": (f"{owner} is a personal account: GitHub doesn't let a token list its app installations, "
                           "so the Snyk import itself checks the app's access"),
                "recommendations": [f"If the import fails with 'not found', add {name} to the Snyk app: "
                                    f"{inst['settings_url']} > Snyk > Configure > Repository access."]}
    if inst["state"] == "unknown":
        detail = inst.get("detail", "")
        return {"state": "unknown", "blocking": True,
                "detail": f"Couldn't read {owner}'s GitHub App installations (HTTP {inst['status']}): {detail}",
                "recommendations": app_access_fixes(owner, name, detail)}
    if inst["state"] == "not-installed":
        return {"state": "not-installed", "blocking": True, "detail": f"Snyk GitHub App is not installed on {owner}",
                "recommendations": [f"Install the Snyk GitHub App on {owner}: in Snyk, open Settings > Integrations > "
                                    "GitHub Cloud App and choose Install, or ask a GitHub org owner to. Or import with "
                                    "`--integration github` if that org's OAuth integration can see the repo."]}
    if inst["selection"] == "all":
        return {"state": "all-repos", "blocking": False, "recommendations": [],
                "detail": f"{inst['app_slug']} has access to all repositories in {owner}"}

    # selected repositories
    token = os.environ.get("GH_APP_ADMIN_TOKEN")
    if not token:
        return {"state": "selected-unverified", "blocking": True,
                "detail": f"{inst['app_slug']} is installed on selected repos only; can't check or add {name} without GH_APP_ADMIN_TOKEN",
                "recommendations": app_access_fixes(owner, name, "", inst["settings_url"])}
    status, repo = gh("GET", f"repos/{owner}/{name}", token)
    if status != 200:
        return {"state": "repo-not-found", "blocking": True, "detail": f"GitHub returned HTTP {status} for {owner}/{name}",
                "recommendations": ["Check the repo name."] + app_access_fixes(
                    owner, name, (repo or {}).get("message", "") + " SSO", inst["settings_url"])}
    page = 1
    while True:
        status, data = gh("GET", f"user/installations/{inst['id']}/repositories?per_page=100&page={page}", token)
        if status != 200:
            return {"state": "selected-unverified", "blocking": True,
                    "detail": f"Couldn't list the app's repos (HTTP {status})",
                    "recommendations": app_access_fixes(owner, name, (data or {}).get("message", ""), inst["settings_url"])}
        repos = data.get("repositories", [])
        if any(r["id"] == repo["id"] for r in repos):
            return {"state": "already-added", "blocking": False, "recommendations": [],
                    "detail": f"{owner}/{name} is already in the {inst['app_slug']} installation"}
        if len(repos) < 100:
            break
        page += 1
    if not apply:
        return {"state": "would-add", "blocking": False, "recommendations": [],
                "detail": f"would add {owner}/{name} to the {inst['app_slug']} installation (selected repos)"}
    status, data = gh("PUT", f"user/installations/{inst['id']}/repositories/{repo['id']}", token)
    if status == 204 or status == 200:
        time.sleep(3)  # let Snyk see the new grant
        return {"state": "added", "blocking": False, "recommendations": [],
                "detail": f"added {owner}/{name} to the {inst['app_slug']} installation"}
    return {"state": "add-failed", "blocking": True, "detail": f"GitHub refused adding the repo (HTTP {status}): "
            f"{(data or {}).get('message', '')[:200]}",
            "recommendations": app_access_fixes(owner, name, (data or {}).get("message", "") + " SSO", inst["settings_url"])}


def go_private_deps(owner, name, branch=None):
    """Repos in the same GitHub org that owner/name's go.mod requires (private cross-repo modules)."""
    ref = f"?ref={branch}" if branch else ""
    status, data = gh("GET", f"repos/{owner}/{name}/contents/go.mod{ref}", os.environ.get("GH_APP_ADMIN_TOKEN"))
    if status != 200 or not data or "content" not in data:
        return []
    import base64
    text = base64.b64decode(data["content"]).decode(errors="replace")
    pat = re.compile(r"github\.com/" + re.escape(owner) + r"/([A-Za-z0-9_.-]+)", re.I)
    deps = []
    for line in text.splitlines():
        line = line.split("//", 1)[0]
        if line.strip().startswith("module "):
            continue
        for m in pat.finditer(line):
            dep = m.group(1)
            if dep.lower() != name.lower() and dep not in deps:
                deps.append(dep)
    return deps


# ---------- feedback ----------

def recommend(status, text, ctx):
    """Turn an import failure into concrete next steps. ctx: owner, slug, itype, app (ensure_app_access result)."""
    t = (text or "").lower()
    o, slug, itype = ctx["owner"], ctx["slug"], ctx["itype"]
    recs = []
    if status == 401 or "unauthori" in t:
        recs.append("Snyk rejected the credentials: run `snyk auth` again, or export a fresh SNYK_TOKEN.")
    if "saml" in t or "sso" in t:
        recs.append(f"GitHub SSO blocked the integration's access to {o}: the token behind the Snyk {itype} "
                    f"integration must be SSO-authorized for {o} (for the GitHub App: re-approve it in {o}'s settings).")
    elif status == 403 or "forbidden" in t or "permission" in t:
        recs.append(f"Your Snyk user or service account can't add projects to org '{slug}'. It needs the Org Admin "
                        "or Org Collaborator role (or a custom role with 'Add project') there; a service account must "
                        f"belong to '{slug}' or its group. Ask a Snyk admin for {slug}.")
    if status == 404 or "not found" in t or "no access" in t or "could not access" in t:
        if "integration" in t:
            recs.append("The integration ID is stale: run `snyk_import.py refresh-orgs`.")
        elif itype == "github-cloud-app":
            app = ctx.get("app") or {}
            if app.get("state") in ("selected-unverified", "would-add", "add-failed"):
                recs.append("The Snyk GitHub App can't see the repo: add it to the app installation (see the step above).")
            else:
                recs.append(f"The Snyk GitHub App can't see {o}'s repo: check the app's repository access for {o}.")
        elif itype == "github":
            recs.append(f"The GitHub user who connected Snyk's 'github' integration in '{slug}' needs read access to the "
                        f"repo, and {o} must allow the Snyk OAuth app: https://github.com/organizations/{o}/settings/oauth_application_policy")
        else:
            recs.append(f"The PAT in Snyk's '{itype}' integration needs access to the repo (and SSO authorization for {o}).")
    if status == 409 or "already" in t:
        recs.append("Already imported or an import is running: check the target in Snyk before retrying.")
    if status in (400, 422) or "branch" in t:
        recs.append("Snyk rejected the request: check the branch name (--branch) and that the repo isn't empty or archived.")
    if status == 429 or "rate limit" in t:
        recs.append("Rate limited: raise --delay and import in smaller waves.")
    if status >= 500:
        recs.append("Snyk server error: retry later; if it repeats, open a Snyk support ticket with the job URL.")
    if "no supported" in t or "no projects" in t or "manifest" in t:
        recs.append("Snyk found no supported manifests: check detect_stack.py's output. Code-only repos need Snyk Code "
                    f"switched on in '{slug}' (Settings > Snyk Code).")
    return recs or ["Unrecognized failure: open the job URL in a browser while signed in to Snyk, or rerun without --no-wait to get the job's logs."]


def wait_for_job(job_url, timeout):
    """Poll a Snyk import job. Returns (state, [(target, ok, message)], raw)."""
    deadline, delay = time.time() + timeout, 3
    while True:
        status, _, body = call("GET", job_url)
        if status != 200:
            return "unknown", [("job", False, f"HTTP {status}: {str(body)[:200]}")], body
        if body.get("status") != "pending" or time.time() > deadline:
            break
        time.sleep(delay)
        delay = min(delay * 2, 20)
    outcomes = []
    for log in body.get("logs", []) or []:
        projects = log.get("projects") or []
        if not projects:
            msg = log.get("message") or log.get("error") or ("no projects created" if log.get("status") == "complete" else log.get("status"))
            outcomes.append((log.get("name", "?"), False, str(msg)))
        for p in projects:
            ok = p.get("success", False)
            msg = p.get("projectUrl") if ok else (p.get("userMessage") or p.get("message") or p.get("error") or "failed")
            outcomes.append((p.get("targetFile") or log.get("name", "?"), ok, str(msg)))
    state = body.get("status", "unknown")
    if state == "pending":
        state = "still-pending"
    return state, outcomes, body


# ---------- commands ----------

def cmd_orgs(_):
    for o in rest_all("/rest/orgs?limit=100"):
        a = o["attributes"]
        print(f'{o["id"]}  {a.get("slug"):<30} {a.get("name")}')


def cmd_integrations(a):
    print(json.dumps(live_integrations(a.org_id), indent=2))


def cmd_resolve(a):
    a.repo = split_repos([a.repo])[0]
    owner = a.repo.split("/", 1)[0]
    org, itype, iid, why = resolve(load_mapping(), owner, a.org_id, a.integration)
    print(json.dumps({"repo": a.repo, "snyk_org": org and org["slug"], "snyk_org_id": org and org["id"],
                      "integration_type": itype, "integration_id": iid, "reason": why}, indent=2))
    sys.exit(0 if iid else 1)


def cmd_refresh(a):
    """Rebuild the mapping: orgs, integrations, and which GitHub owners each org already imports (and how)."""
    out = {"_about": "Generated by snyk_import.py refresh-orgs. github_orgs maps a GitHub owner to the SCM "
                     "integration type its existing Snyk projects use. Edit by hand to add owners; rerun to refresh.",
           "generated": datetime.date.today().isoformat(), "api": API, "orgs": []}
    for o in rest_all("/rest/orgs?limit=100"):
        attrs = o["attributes"]
        ints = live_integrations(o["id"])
        origins = collections.Counter()
        try:
            for p in rest_all(f"/rest/orgs/{o['id']}/projects?limit=100", max_pages=a.max_pages):
                pa = p["attributes"]
                if pa.get("origin") in SCM_TYPES and "/" in pa.get("name", ""):
                    origins[(pa["name"].split("/", 1)[0], pa["origin"])] += 1
        except RuntimeError as e:
            print(f"warn: {attrs.get('slug')}: {e}", file=sys.stderr)
        owners = {}
        for (owner, origin), _n in origins.most_common():
            owners.setdefault(owner, origin)  # most-used integration per owner
        # keep hand-added owners from the existing file
        old = find_org(load_mapping(), o["id"])
        for g, t in (old or {}).get("github_orgs", {}).items():
            owners.setdefault(g, t)
        out["orgs"].append({"slug": attrs.get("slug"), "name": attrs.get("name"), "id": o["id"],
                            "group_id": attrs.get("group_id"),
                            "integrations": {k: v for k, v in sorted(ints.items())},
                            "github_orgs": owners})
        print(f"{attrs.get('slug'):<28} owners={owners}", file=sys.stderr)
    path = a.out or MAPPING
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
        f.write("\n")
    print(f"wrote {os.path.normpath(path)}", file=sys.stderr)


# ---------- integration settings (PR checks, fix PRs) ----------

PRESETS = {
    # PR checks on dependencies and code that fail only on high/critical issues, fix PRs for new
    # and existing vulnerabilities, and dependency upgrade PRs (5 open at most).
    "recommended": {
        "pullRequestTestEnabled": True,
        "pullRequestFailOnAnyVulns": False,
        "pullRequestFailOnlyForHighSeverity": True,
        "pullRequestTestCodeEnabled": True,
        "pullRequestTestCodeSeverity": "high",
        "autoRemediationPrs": {"freshPrsEnabled": True, "backlogPrsEnabled": True},
        "autoDepUpgradeEnabled": True,
        "autoDepUpgradeLimit": 5,
    },
    # Only PR checks (no Snyk-authored PRs).
    "checks-only": {
        "pullRequestTestEnabled": True,
        "pullRequestFailOnlyForHighSeverity": True,
        "pullRequestTestCodeEnabled": True,
        "pullRequestTestCodeSeverity": "high",
    },
}
SETTING_LABELS = {
    "pullRequestTestEnabled": "PR checks: dependencies",
    "pullRequestFailOnAnyVulns": "PR checks fail on any vulnerability",
    "pullRequestFailOnlyForHighSeverity": "PR checks fail only on high/critical",
    "pullRequestTestCodeEnabled": "PR checks: code (Snyk Code)",
    "pullRequestTestCodeSeverity": "PR code checks: severity",
    "autoRemediationPrs.freshPrsEnabled": "Fix PRs for new vulnerabilities",
    "autoRemediationPrs.backlogPrsEnabled": "Fix PRs for existing vulnerabilities (backlog)",
    "autoDepUpgradeEnabled": "Dependency upgrade PRs",
    "autoDepUpgradeLimit": "Dependency upgrade PRs: max open",
}


def _flat(d, prefix=""):
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(_flat(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def _parse_set(items):
    """['autoDepUpgradeLimit=3', 'autoRemediationPrs.backlogPrsEnabled=false'] -> nested dict."""
    out = {}
    for item in items or []:
        key, _, raw = item.partition("=")
        val = {"true": True, "false": False, "null": None}.get(raw.lower(), raw)
        if isinstance(val, str) and val.isdigit():
            val = int(val)
        node = out
        parts = key.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = val
    return out


def _merge(base, extra):
    out = dict(base)
    for k, v in extra.items():
        out[k] = _merge(out.get(k, {}), v) if isinstance(v, dict) else v
    return out


def cmd_integration_settings(a):
    """Show (and with --apply, change) the GitHub integration's PR-check and fix-PR settings."""
    owner = split_repos([a.repo])[0].split("/")[0] if a.repo else a.github_owner
    org, itype, iid, why = resolve(load_mapping(), owner or "", a.org_id, a.integration)
    if not iid:
        sys.exit(f"Can't find the Snyk org's GitHub integration: {why}")
    path = f"/v1/org/{org['id']}/integrations/{iid}/settings"
    status, _, current = call("GET", path)
    if status != 200:
        sys.exit(f"Couldn't read the integration settings (HTTP {status}): {current}")
    wanted = _merge(PRESETS.get(a.preset, {}) if a.preset else {}, _parse_set(a.set))
    cur, new = _flat(current), _flat(wanted)
    keys = list(dict.fromkeys([k for k in SETTING_LABELS] + list(new)))
    print(f"Snyk org {org['slug']}, {itype} integration {iid}")
    print(f"  {'setting':<48} {'now':<8} {'proposed'}")
    changed = {}
    for k in keys:
        if k not in cur and k not in new:
            continue
        now, prop = cur.get(k), new.get(k, cur.get(k))
        mark = "  ← change" if k in new and prop != now else ""
        if mark:
            changed[k] = prop
        print(f"  {SETTING_LABELS.get(k, k):<48} {str(now):<8} {str(prop)}{mark}")
    if not changed:
        print("Nothing to change.")
        return
    if not a.apply:
        print(f"\nDry run: {len(changed)} setting(s) would change. Rerun with --apply to save them.")
        return
    body = {}
    for k, v in wanted.items():  # nested objects are sent whole, merged with their current values
        body[k] = _merge(current.get(k) or {}, v) if isinstance(v, dict) else v
    status, _, resp = call("PUT", path, body)
    if status not in (200, 204):
        sys.exit(f"Snyk refused the change (HTTP {status}): {resp}")
    status, _, after = call("GET", path)
    after = _flat(after or {})
    bad = [k for k, v in changed.items() if after.get(k) != v]
    if bad:
        sys.exit(f"Saved, but these didn't take effect: {', '.join(bad)}")
    print(f"\nSaved {len(changed)} setting(s). They apply to every repo imported through this integration.")


def cmd_import(a):
    if a.plan:
        plan = json.load(open(a.plan))
        targets = plan["targets"]
        if a.skip_onboarded:
            targets = [t for t in targets if not t["already_onboarded"]["snyk_in_github_actions"]]
    elif a.repo or a.file:
        repos = split_repos(a.repo, a.file)
        if not repos:
            sys.exit("No repos given (the file was empty?)")
        targets = [{"owner": r.split("/", 1)[0], "name": r.split("/", 1)[1], "branch": a.branch}
                   for r in repos]
    else:
        sys.exit("Pass --plan, --repo or --file")

    mapping = load_mapping()
    groups, unresolved = collections.OrderedDict(), []
    for t in targets:
        org, itype, iid, why = resolve(mapping, t["owner"], a.org_id, a.integration)
        if not iid:
            unresolved.append((f"{t['owner']}/{t['name']}", why))
            continue
        groups.setdefault((org["id"], org["slug"], itype, iid), []).append(t)

    print(f"{'APPLY' if a.apply else 'DRY RUN'}: {sum(len(v) for v in groups.values())} repo(s)")
    results = []
    for repo, why in unresolved:
        print(f"  SKIP {repo}: {why}")
        results.append({"repo": repo, "result": "skipped", "detail": why, "recommendations": [why]})

    def fail(rec, repo, detail, recs):
        rec.update(result="failed", detail=detail, recommendations=recs)
        print(f"     ✗ {repo}: {detail}")
        for r in recs:
            print(f"       → {r}")

    for (org_id, slug, itype, iid), ts in groups.items():
        print(f"  -> Snyk org {slug} ({org_id}) via {itype} {iid}")
        for t in ts:
            repo = f"{t['owner']}/{t['name']}"
            rec = {"repo": repo, "snyk_org": slug, "integration": itype}
            results.append(rec)
            ctx = {"owner": t["owner"], "slug": slug, "itype": itype}

            # 1. GitHub side: give the Snyk app access to the repo when it's limited to selected repos
            if itype == "github-cloud-app" and not a.no_grant_app:
                app = ensure_app_access(t["owner"], t["name"], a.apply)
                ctx["app"] = rec["github_app"] = app
                print(f"     app: {app['state']}: {app['detail']}")
                if app["state"] in REFUSED_APP_STATES:
                    print(highlight(f"⚠ GITHUB REFUSED: repo NOT added to the Snyk GitHub App ({app['state']})"))
                if not app["blocking"]:
                    for r in app["recommendations"]:
                        print(f"       → {r}")
                else:
                    fail(rec, repo, "not imported: the Snyk GitHub App can't reach the repo", app["recommendations"])
                    continue

            # 2. Snyk side: import
            target = {"owner": t["owner"], "name": t["name"]}
            # The GitHub Cloud App import requires a branch; default to the repo's default branch.
            branch = t.get("branch") or default_branch(t["owner"], t["name"])
            if branch:
                target["branch"] = branch
            have = existing_projects(org_id, t["owner"], t["name"])
            if have:
                kinds = ", ".join(sorted({f"{o} {ty}" for o, ty, _ in have}))
                print(f"     already in Snyk: {len(have)} project(s) ({kinds}); importing again refreshes them")
            elif have is not None:
                print("     not in Snyk yet (checked the org's live project list)")
            if not a.apply:
                print(f"     would import {repo}@{target.get('branch') or 'default branch (unknown)'}")
                rec["result"] = "dry-run"
                continue
            status, headers, resp = call("POST", f"/v1/org/{org_id}/integrations/{iid}/import",
                                         {"target": target, "exclusionGlobs": a.exclude})
            job = headers.get("Location") or headers.get("location")
            rec.update(status=status, job=job)
            if status not in (200, 201):
                fail(rec, repo, f"Snyk import request failed (HTTP {status}): {str(resp)[:200]}",
                     recommend(status, json.dumps(resp), ctx))
                time.sleep(a.delay)
                continue
            print(f"     ✓ import started: {job}")

            # 3. Wait for the job and report per-project outcomes
            if a.no_wait or not job:
                rec["result"] = "started"
            else:
                state, outcomes, _raw = wait_for_job(job, a.wait_timeout)
                rec["job_status"] = state
                rec["projects"] = [{"target": n, "ok": ok, "message": m} for n, ok, m in outcomes]
                good = [o for o in outcomes if o[1]]
                bad = [o for o in outcomes if not o[1]]
                for n, _ok, m in good:
                    print(f"       ✓ {n}: {m}")
                if state == "still-pending":
                    rec["result"] = "pending"
                    print(f"       … still running after {a.wait_timeout}s; check {job} later")
                elif bad or state == "failed" or not good:
                    msgs = "; ".join(m for _n, _ok, m in bad) or f"job {state}, no projects created"
                    go_failed = [n for n, _ok, m in bad if n.endswith("go.mod")]
                    deps = go_private_deps(t["owner"], t["name"], t.get("branch")) if go_failed else []
                    if deps and itype == "github-cloud-app":
                        print(f"       ✗ {', '.join(go_failed)}: {msgs[:200]}")
                        print(f"       ⓘ go.mod requires private modules from {t['owner']}: {', '.join(deps)}")
                        print(f"         Snyk resolves Go modules through the GitHub App, so it needs read access to those repos too.")
                        if a.fix_deps:
                            granted = []
                            for d in deps:
                                dep_app = ensure_app_access(t["owner"], d, True)
                                print(f"       app: {d}: {dep_app['state']}")
                                if dep_app["state"] in ("added", "already-added", "all-repos"):
                                    granted.append(d)
                            rec["deps_granted"] = granted
                            if len(granted) == len(deps):
                                print("       ↻ retrying import with dependency access granted")
                                status, headers, resp = call("POST", f"/v1/org/{org_id}/integrations/{iid}/import",
                                                             {"target": target, "exclusionGlobs": a.exclude})
                                job = headers.get("Location") or headers.get("location")
                                state, outcomes, _raw = wait_for_job(job, a.wait_timeout) if status in (200, 201) and job else ("failed", [], None)
                                rec.update(retry_job=job, retry_status=state,
                                           projects=[{"target": n, "ok": ok, "message": m} for n, ok, m in outcomes])
                                for n, ok, m in outcomes:
                                    print(f"       {'✓' if ok else '✗'} {n}: {m}")
                                if outcomes and all(o[1] for o in outcomes):
                                    rec["result"] = "imported-after-fix"
                                    time.sleep(a.delay)
                                    continue
                        deps_recs = [
                            f"Give the Snyk GitHub App read access to {', '.join(t['owner'] + '/' + d for d in deps)}: rerun with "
                            f"`--apply --fix-deps`, or add them at {snyk_app_installation(t['owner']).get('settings_url', 'the app installation settings')}.",
                            f"Or scan Go in CI instead: set GOPRIVATE=github.com/{t['owner']}/* and a token with read access, then "
                            "`snyk monitor` (the generated workflow's open-source job).",
                        ]
                        fail(rec, repo, "go.mod import blocked: private cross-repo modules are forbidden to Snyk", deps_recs)
                    else:
                        fail(rec, repo, f"import finished with errors: {msgs[:300]}", recommend(0, msgs, ctx))
                else:
                    rec["result"] = "imported"
            time.sleep(a.delay)

    # summary
    counts = collections.Counter(r.get("result", "?") for r in results)
    print("\nSummary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    all_recs = collections.Counter(x for r in results if r.get("result") in ("failed", "skipped")
                                   for x in r.get("recommendations", []))
    if all_recs:
        print("Recommendations:")
        for r, n in all_recs.most_common():
            print(f"  - ({n} repo{'s' if n > 1 else ''}) {r}")
    if a.apply:
        out = a.log or "snyk-import-log.json"
        json.dump(results, open(out, "w"), indent=2)
        print(f"Log written to {out}.")
    if counts.get("failed") or (unresolved and not groups):
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("orgs").set_defaults(fn=cmd_orgs)
    p = sub.add_parser("integrations"); p.add_argument("--org-id", required=True); p.set_defaults(fn=cmd_integrations)
    p = sub.add_parser("resolve"); p.add_argument("--repo", required=True)
    p.add_argument("--org-id", help="Snyk org id or slug (overrides the mapping)")
    p.add_argument("--integration", choices=SCM_TYPES); p.set_defaults(fn=cmd_resolve)
    p = sub.add_parser("refresh-orgs"); p.add_argument("--out")
    p.add_argument("--max-pages", type=int, default=5, help="project pages (100 each) sampled per org")
    p.set_defaults(fn=cmd_refresh)
    p = sub.add_parser("import")
    p.add_argument("--org-id", help="Snyk org id or slug; default: per repo from the mapping, else the Snyk org named like the GitHub owner")
    p.add_argument("--integration", choices=SCM_TYPES, help="force an integration type")
    p.add_argument("--plan"); p.add_argument("--branch")
    p.add_argument("--repo", nargs="+", help="one or more repos: owner/name or bare name (owner brightsheets), space or comma separated; @file reads a list")
    p.add_argument("--file", action="append", help="file of repo names, one per line or comma separated, '#' comments")
    p.add_argument("--exclude", default=DEFAULT_EXCLUDES)
    p.add_argument("--skip-onboarded", action="store_true")
    p.add_argument("--delay", type=float, default=1.0)
    p.add_argument("--log")
    p.add_argument("--no-grant-app", action="store_true",
                   help="don't add the repo to the Snyk GitHub App installation when it's limited to selected repos")
    p.add_argument("--fix-deps", action="store_true",
                   help="when a go.mod import is forbidden, add the same-org module repos to the Snyk GitHub App and retry")
    p.add_argument("--no-wait", action="store_true", help="don't wait for import jobs to finish")
    p.add_argument("--wait-timeout", type=int, default=300, help="seconds to wait per import job")
    p.add_argument("--apply", action="store_true",
                   help="actually import, and add repos to the Snyk GitHub App (default is dry run)")
    p.set_defaults(fn=cmd_import)
    p = sub.add_parser("integration-settings", help="show/change PR checks and fix PRs on the GitHub integration")
    p.add_argument("--repo", help="a repo whose GitHub owner picks the Snyk org (same-name rule)")
    p.add_argument("--github-owner", help="GitHub owner instead of --repo")
    p.add_argument("--org-id", help="Snyk org id or slug (overrides the same-name rule)")
    p.add_argument("--integration", choices=SCM_TYPES)
    p.add_argument("--preset", choices=sorted(PRESETS), help="recommended | checks-only")
    p.add_argument("--set", action="append", help="KEY=VALUE, e.g. autoDepUpgradeLimit=3 (dotted keys for nested)")
    p.add_argument("--apply", action="store_true", help="save the changes (default: show only)")
    p.set_defaults(fn=cmd_integration_settings)
    p = sub.add_parser("repos", help="print the expanded repo list, one per line")
    p.add_argument("--repo", nargs="*"); p.add_argument("--file", action="append")
    p.set_defaults(fn=lambda a: print("\n".join(split_repos(a.repo, a.file))))
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
