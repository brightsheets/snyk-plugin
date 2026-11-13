#!/usr/bin/env python3
"""Create demo GitHub repos for Snyk testing.

  make_repos.py plan                   # Go, default 2: snyk-demo-go-N with cross-org deps on deps-01,
                                       #     and snyk-demo-go-N+1 with public deps only (uuid, x/text)
  make_repos.py plan 5                 # Go: 5 repos; the first 4 have cross-org deps, the last public only
                                       # Every Go repo gets .github/workflows/ci.yml calling
                                       # brightsheets/reusable-workflows go-ci.yaml@v1
  make_repos.py plan payments orders   # Go: these names, each with cross deps
  make_repos.py plan node 3            # snyk-demo-node-N x3: copies of snyk-labs/nodejs-goof
  make_repos.py plan java 2            # snyk-demo-java-N x2: copies of snyk-labs/java-goof
  make_repos.py create <same> --apply  # actually create (plan/dry run never writes)
  make_repos.py list                   # repos this script made

Numbered names continue after the highest existing one, so they never clash. New repos are private
unless --public. Go: if deps-01 is empty it is seeded with a small Go package tagged v1.0.0 first.
Never overwrites, force-pushes or deletes. Needs gh (repo scope) and git; go is optional (go.sum).
"""
import argparse, base64, json, os, re, shutil, subprocess, sys, tempfile, time

TOPIC = "go-deps-fixture"
MARKER = ".go-deps-fixture"
MAX_COUNT = 100        # GitHub throttles bulk content creation; stay well under its limits
GO_VERSION = "1.21"


# ---------- helpers ----------

def run(cmd, cwd=None, env=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f"Command failed: {' '.join(cmd)}\n{r.stderr.strip() or r.stdout.strip()}")
    return r


def gh_api(path, check=False):
    r = run(["gh", "api", path], check=False)
    if r.returncode != 0:
        if check:
            sys.exit(f"gh api {path} failed: {r.stderr.strip()}")
        return None
    return json.loads(r.stdout) if r.stdout.strip() else None


def git_env(owner):
    """git/go use gh's login for github.com, without changing any global git config."""
    env = dict(os.environ)
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.https://github.com.helper",
        "GIT_CONFIG_VALUE_0": "!gh auth git-credential",
        "GOPRIVATE": f"github.com/{owner}/*",
        "GOFLAGS": "-mod=mod",
        "GOTOOLCHAIN": "local",
    })
    return env


def pkg_name(repo):
    name = re.sub(r"[^a-z0-9]", "", repo.lower())
    return name if name and not name[0].isdigit() else "p" + name


def identity():
    """Commit author: the user's git config, else their GitHub name and private noreply address."""
    name = run(["git", "config", "user.name"], check=False).stdout.strip()
    email = run(["git", "config", "user.email"], check=False).stdout.strip()
    if name and email:
        return name, email
    u = gh_api("user", check=True)
    return name or u.get("name") or u["login"], email or f'{u["login"]}@users.noreply.github.com'


# ---------- repo state ----------

def repo_state(owner, name):
    """None if missing, else dict(empty, tags, marker, root_go, module)."""
    info = gh_api(f"repos/{owner}/{name}")
    if not info:
        return None
    contents = gh_api(f"repos/{owner}/{name}/contents/")
    st = {"visibility": info.get("visibility"), "empty": contents is None}
    files = [c["name"] for c in contents or []]
    st["marker"] = MARKER in files
    st["root_go"] = any(f.endswith(".go") and not f.endswith("_test.go") for f in files)
    tags = gh_api(f"repos/{owner}/{name}/tags?per_page=100") or []
    st["tags"] = [t["name"] for t in tags if re.match(r"^v\d+\.\d+\.\d+$", t["name"])]
    st["module"] = None
    if "go.mod" in files:
        gm = gh_api(f"repos/{owner}/{name}/contents/go.mod")
        if gm and gm.get("content"):
            text = base64.b64decode(gm["content"]).decode(errors="replace")
            m = re.search(r"^module\s+(\S+)", text, re.M)
            st["module"] = m and m.group(1)
    return st


def latest(tags):
    return max(tags, key=lambda t: tuple(int(x) for x in t[1:].split("."))) if tags else None


# ---------- file contents ----------

def deps_files(owner, deps, version):
    mod = f"github.com/{owner}/{deps}"
    return {
        "go.mod": f"module {mod}\n\ngo {GO_VERSION}\n",
        "deps.go": (f"// Package deps is a shared module that the snyk-demo-go repos depend on.\n"
                    f"package deps\n\nimport \"fmt\"\n\n"
                    f"// Version identifies this module.\nfunc Version() string {{ return \"{deps} {version}\" }}\n\n"
                    f"// Greet is called by every dependent library.\n"
                    f"func Greet(caller string) string {{ return fmt.Sprintf(\"%s uses {deps}\", caller) }}\n"),
        "README.md": f"# {deps}\n\nShared Go module. Fixture repos tagged `{TOPIC}` depend on it.\n",
        MARKER: "created by go-deps-fixtures\n",
    }


REUSABLE = os.environ.get("REUSABLE_WORKFLOWS", "brightsheets/reusable-workflows@v1")


def caller_workflow(owner, private):
    """.github/workflows/ci.yml calling the shared Go pipeline (lint, tests, Snyk)."""
    repo, ref = REUSABLE.split("@", 1)
    w = [f"snyk-org: {owner}"]  # Snyk orgs are named like GitHub owners
    sec = ["snyk-token: ${{ secrets.SNYK_TOKEN }}"]
    note = "Needs secret SNYK_TOKEN (without it the Snyk scan is skipped with a warning)"
    if private:
        w.append(f"go-private-owners: {owner}")
        sec.append("go-modules-token: ${{ secrets.GO_MODULES_TOKEN }}")
        note += f",\n# and GO_MODULES_TOKEN: a token that can read the private {owner} repos this module imports"
    return (f"# Runs {repo}/.github/workflows/go-ci.yaml@{ref}: lint, tests, Snyk Open Source + Code.\n"
            f"# {note}.\n"
            "name: CI\non:\n  push:\n    branches: [main, \"hotfix/**\"]\n  pull_request:\n  workflow_dispatch:\n"
            "permissions:\n  contents: read\n  actions: read\n  security-events: write\n  pull-requests: write\n  packages: write\n"
            f"jobs:\n  ci:\n    uses: {repo}/.github/workflows/go-ci.yaml@{ref}\n"
            "    with:\n" + "".join(f"      {x}\n" for x in w) +
            "    secrets:\n" + "".join(f"      {x}\n" for x in sec))


def js_caller_workflow(src_dir, owner):
    """.github/workflows/ci.yml for a Node.js copy, calling the shared js-ci pipeline."""
    repo, ref = REUSABLE.split("@", 1)
    node, docker = "lts/*", os.path.exists(os.path.join(src_dir, "Dockerfile"))
    if docker:
        m = re.search(r"^FROM\s+node:(\d+)", open(os.path.join(src_dir, "Dockerfile")).read(), re.M | re.I)
        node = m.group(1) if m else node
    w = [f'node-version: "{node}"', f"snyk-org: {owner}"]
    if docker:
        w += ["docker-build: true", "dry-run: true   # demo app: build and scan the image, never push it"]
    return (f"# Runs {repo}/.github/workflows/js-ci.yaml@{ref}: build, Snyk Open Source + Code"
            + (", Docker build + Snyk Container" if docker else "") + ".\n"
            "# Needs secret SNYK_TOKEN (without it the Snyk scans are skipped with a warning).\n"
            "name: CI\non:\n  push:\n    branches: [main, \"hotfix/**\"]\n  pull_request:\n  workflow_dispatch:\n"
            "permissions:\n  contents: read\n  actions: read\n  security-events: write\n  pull-requests: write\n  packages: write\n"
            f"jobs:\n  ci:\n    uses: {repo}/.github/workflows/js-ci.yaml@{ref}\n"
            "    with:\n" + "".join(f"      {x}\n" for x in w) +
            "    secrets:\n      snyk-token: ${{ secrets.SNYK_TOKEN }}\n")


def lib_files(owner, name, deps_mod, deps_ver, deps_callable, prev, version):
    mod = f"github.com/{owner}/{name}"
    pkg = pkg_name(name)
    imports = [f'\tdeps "{deps_mod}"' if deps_callable else f'\t_ "{deps_mod}"']
    parts = [f'deps.Greet("{name}")'] if deps_callable else [f'"{name} uses {deps_mod}"']
    for p in prev:
        imports.append(f'\t{pkg_name(p)} "github.com/{owner}/{p}"')
        parts.append(f"{pkg_name(p)}.ID")  # a constant, not Name(): calling Name() would grow exponentially
    requires = [f"\t{deps_mod} {deps_ver}"] + [f"\tgithub.com/{owner}/{p} {version}" for p in prev]
    body = " + \" | \" + ".join(parts)
    return {
        "go.mod": f"module {mod}\n\ngo {GO_VERSION}\n\nrequire (\n" + "\n".join(requires) + "\n)\n",
        f"{pkg}.go": (f"// Package {pkg} is a fixture library with cross-repo Go module dependencies.\n"
                      f"package {pkg}\n\nimport (\n" + "\n".join(imports) + "\n)\n\n"
                      f"// ID is this module's name.\nconst ID = \"{name}\"\n\n"
                      f"// Name reports this library and its direct dependencies.\n"
                      f"func Name() string {{ return {body} }}\n"),
        ".github/workflows/ci.yml": caller_workflow(owner, private=True),
        "README.md": (f"# {name}\n\nFixture Go library. Depends on `{deps_mod}`"
                      + (", " + ", ".join(f"`github.com/{owner}/{p}`" for p in prev) if prev else "")
                      + ".\n"),
        MARKER: "created by go-deps-fixtures\n",
    }


# Public modules for the repo without cross-org deps. x/text v0.3.7 is deliberately old: it has a
# known vulnerability (CVE-2022-32149), so Snyk reports something on this repo too.
PUBLIC_DEPS = [("github.com/google/uuid", "v1.6.0"), ("golang.org/x/text", "v0.3.7")]


def standalone_files(owner, name):
    reqs = "\n".join(f"\t{m} {v}" for m, v in PUBLIC_DEPS)
    return {
        "go.mod": f"module github.com/{owner}/{name}\n\ngo {GO_VERSION}\n\nrequire (\n{reqs}\n)\n",
        "main.go": ("// Command " + name + " uses public modules only: no dependencies on other " + owner + " repos.\n"
                    "package main\n\nimport (\n\t\"fmt\"\n\n\t\"github.com/google/uuid\"\n\t\"golang.org/x/text/language\"\n)\n\n"
                    "func main() {\n\tfmt.Println(\"" + name + "\", uuid.NewString(), language.Make(\"en-US\").String())\n}\n"),
        ".github/workflows/ci.yml": caller_workflow(owner, private=False),
        "README.md": (f"# {name}\n\nGo module with public dependencies only "
                      f"({', '.join(m for m, _ in PUBLIC_DEPS)}), no cross-org dependencies.\n"),
        MARKER: "created by go-deps-fixtures\n",
    }



# ---------- languages ----------

# Copied demo apps: a fresh snapshot of the source repo's default branch (one commit,
# files as upstream has them). Cloned, not forked: GitHub allows one fork per source repo per account, and
# forks of public repos can't be private.
SOURCES = {"node": "snyk-labs/nodejs-goof", "java": "snyk-labs/java-goof"}
LANGS = {"go": "go", "golang": "go", "node": "node", "nodejs": "node", "js": "node",
         "javascript": "node", "java": "java"}
DEMO_TOPIC = "snyk-demo"


# ---------- planning ----------

def parse_items(a):
    """Split positional args into (lang, count, names). Language word first is optional."""
    items = [x.strip() for item in a.items for x in item.split(",") if x.strip()]
    lang = a.lang
    if items and items[0].lower() in LANGS:
        lang = LANGS[items.pop(0).lower()]
    lang = LANGS.get((lang or "go").lower()) or sys.exit(f"Unknown language {lang!r}; use go, node or java.")
    count, names = a.count, []
    if len(items) == 1 and items[0].isdigit():
        count = int(items[0])
    elif items:
        if any(x.isdigit() for x in items):
            sys.exit("Give either a number or repo names, not both.")
        names = items
    bad = [n for n in names if not re.match(r"^[A-Za-z0-9._-]+$", n) or n == a.deps]
    if bad:
        sys.exit(f"Not valid new repo names: {bad}")
    if len(set(names)) != len(names):
        sys.exit("Repo names must be different from each other.")
    total = count or 2
    if not names and not 1 <= total <= MAX_COUNT:
        sys.exit(f"Number of repos must be 1..{MAX_COUNT}")
    return lang, total, names


def existing_names(owner):
    r = run(["gh", "repo", "list", owner, "--limit", "4000", "--json", "name", "-q", ".[].name"])
    return set(r.stdout.split())


def numbered(owner, prefix, n):
    """n free names <prefix>-K, continuing after the highest existing K."""
    pat = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
    start = max([int(m.group(1)) for x in existing_names(owner) if (m := pat.match(x))] + [0]) + 1
    return [f"{prefix}-{i}" for i in range(start, start + n)]


def build_plan(a):
    owner = a.owner
    lang, total, names = parse_items(a)
    plan = {"owner": owner, "lang": lang, "visibility": "public" if a.public else "private",
            "version": a.version, "repos": []}
    prefix = a.prefix or f"snyk-demo-{lang}"
    auto = not names
    if auto:
        names = numbered(owner, prefix, total)

    if lang in SOURCES:
        src = SOURCES[lang]
        plan["source"] = src
        for n in names:
            plan["repos"].append({"name": n, "kind": "copy", "source": src})
    else:
        deps_mod = f"github.com/{owner}/{a.deps}"
        ds = repo_state(owner, a.deps)
        if ds is None or ds["empty"]:  # missing: create it (public) and seed it; empty: just seed it
            plan["repos"].append({"name": a.deps, "kind": "deps", "action": "create" if ds is None else "seed",
                                  "visibility": "public", "files": deps_files(owner, a.deps, a.version)})
            deps_ver, deps_callable = a.version, True
        else:
            if ds["module"] and ds["module"] != deps_mod:
                sys.exit(f"{owner}/{a.deps} go.mod declares module {ds['module']}, expected {deps_mod}.")
            if not ds["module"] or not ds["root_go"]:
                sys.exit(f"{owner}/{a.deps} has code but no go.mod or no Go package at its root; repos can't import it.")
            deps_ver = latest(ds["tags"])
            if not deps_ver:
                sys.exit(f"{owner}/{a.deps} has no vX.Y.Z tag. Tag it (e.g. v1.0.0) so go.mod can require it.")
            deps_callable = ds["marker"]  # our own seed has deps.Greet; otherwise import it for side effects
            plan["repos"].append({"name": a.deps, "kind": "deps", "action": "reuse", "version": deps_ver})
        plan["deps"] = {"module": deps_mod, "version": deps_ver, "calls_greet": deps_callable}

        # With a number, the last repo has public deps only, no cross-org deps (unless only one repo).
        standalone = a.standalone or (names[-1] if auto and len(names) > 1 else None)
        libs = [n for n in names if n != standalone]
        for i, name in enumerate(libs):
            prev = libs[max(0, i - 2):i][::-1]
            plan["repos"].append({"name": name, "kind": "go-deps", "requires": [a.deps] + prev,
                                  "files": lib_files(owner, name, deps_mod, deps_ver, deps_callable, prev, a.version)})
        if standalone:
            plan["repos"].append({"name": standalone, "kind": "go-public-deps", "requires": [m for m, _ in PUBLIC_DEPS],
                                  "files": standalone_files(owner, standalone)})

    for r in plan["repos"]:
        if r["kind"] == "deps":
            continue
        st = None if auto else repo_state(owner, r["name"])
        if st is None:
            r["action"] = "create"
        elif r["kind"] != "copy" and st["marker"] and a.version in st["tags"]:
            r["action"] = "reuse"
        else:
            sys.exit(f"{owner}/{r['name']} already exists. Pick another name, "
                     "or just give a number to get free names automatically.")
    return plan


def print_plan(plan):
    print(f"Owner {plan['owner']}, new repos {plan['visibility']}")
    if plan["lang"] in SOURCES:
        print(f"Each repo is a copy of https://github.com/{plan['source']} (latest code, one commit)")
    else:
        print(f"Go repos, tag {plan['version']}. Shared module {plan['deps']['module']} @ {plan['deps']['version']}"
              + ("" if plan['deps']['calls_greet'] else " (imported for side effects: not seeded by this script)"))
    for r in plan["repos"]:
        what = ("copy of " + r["source"]) if r["kind"] == "copy" else \
               ("shared module" if r["kind"] == "deps" else
                ("public deps only: " if r["kind"] == "go-public-deps" else "cross-org deps: ") + (", ".join(r["requires"]) or "none"))
        print(f"  {r['action']:<7} {r['name']:<24} {what}")
    n = sum(r["action"] in ("create", "seed") for r in plan["repos"])
    print(f"{n} repo(s) to create or seed." + ("" if n else " Nothing to do."))


# ---------- apply ----------

def git_cmd(author):
    return ["git", "-c", f"user.name={author[0]}", "-c", f"user.email={author[1]}"]


def create_remote(plan, r, description):
    if r["action"] == "create":
        run(["gh", "repo", "create", f"{plan['owner']}/{r['name']}", f"--{r.get('visibility', plan['visibility'])}",
             "--description", description])


def push_repo(plan, r, author, env, has_go, delay):
    owner, name = plan["owner"], r["name"]
    url = f"https://github.com/{owner}/{name}.git"
    create_remote(plan, r, "Go module demo with cross-repo dependencies" if r["kind"] != "deps"
                  else "Shared Go module")
    work = tempfile.mkdtemp(prefix=f"{name}-")
    try:
        for path, text in r["files"].items():
            os.makedirs(os.path.dirname(os.path.join(work, path)), exist_ok=True)
            with open(os.path.join(work, path), "w") as f:
                f.write(text)
        if has_go and r.get("requires"):
            res = run(["go", "mod", "tidy"], cwd=work, env=env, check=False)
            if res.returncode != 0:
                sys.exit(f"go mod tidy failed in {name}:\n{res.stderr.strip()}")
        g = git_cmd(author)
        run(["git", "init", "-q", "-b", "main"], cwd=work)
        run(g + ["add", "-A"], cwd=work)
        run(g + ["commit", "-q", "-m", {"go-deps": "Add Go module with cross-org dependencies", "go-public-deps": "Add Go module with public dependencies only"}.get(r["kind"], "Add Go module")], cwd=work)
        run(g + ["tag", plan["version"]], cwd=work)  # lightweight tag
        run(["git", "push", "-q", url, "main", plan["version"]], cwd=work, env=env)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    run(["gh", "repo", "edit", f"{owner}/{name}", "--add-topic", TOPIC, "--add-topic", DEMO_TOPIC], check=False)
    print(f"  {r['action']:<7} https://github.com/{owner}/{name}")
    time.sleep(delay)


def fetch_source(src, env):
    """Shallow clone of the source's default branch; returns (dir, short sha)."""
    d = tempfile.mkdtemp(prefix="src-")
    run(["git", "clone", "-q", "--depth", "1", f"https://github.com/{src}.git", d], env=env)
    sha = run(["git", "rev-parse", "--short", "HEAD"], cwd=d).stdout.strip()
    shutil.rmtree(os.path.join(d, ".git"))
    return d, sha


def push_copy(plan, r, src_dir, sha, author, env, delay):
    owner, name = plan["owner"], r["name"]
    create_remote(plan, r, f"Snyk demo app, copied from {r['source']}")
    work = tempfile.mkdtemp(prefix=f"{name}-")
    try:
        shutil.copytree(src_dir, work, dirs_exist_ok=True, symlinks=True)
        # Drop upstream workflows (they expect the upstream repo's secrets); Node copies get a
        # caller of the shared JavaScript pipeline instead.
        shutil.rmtree(os.path.join(work, ".github", "workflows"), ignore_errors=True)
        if plan["lang"] == "node":
            os.makedirs(os.path.join(work, ".github", "workflows"), exist_ok=True)
            with open(os.path.join(work, ".github", "workflows", "ci.yml"), "w") as f:
                f.write(js_caller_workflow(src_dir, owner))
        g = git_cmd(author)
        run(["git", "init", "-q", "-b", "main"], cwd=work)
        run(g + ["add", "-A"], cwd=work)
        run(g + ["commit", "-q", "-m", f"Import https://github.com/{r['source']} at {sha}"], cwd=work)
        run(["git", "push", "-q", f"https://github.com/{owner}/{name}.git", "main"], cwd=work, env=env)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    run(["gh", "repo", "edit", f"{owner}/{name}", "--add-topic", DEMO_TOPIC], check=False)
    print(f"  {r['action']:<7} https://github.com/{owner}/{name}")
    time.sleep(delay)


def cmd_plan(a):
    print_plan(build_plan(a))


def cmd_create(a):
    plan = build_plan(a)
    print_plan(plan)
    if not a.apply:
        print("\nDry run. Rerun with --apply to create these repos.")
        return
    author, env = identity(), git_env(plan["owner"])
    # Fresh module cache per run: a deleted and recreated repo can reuse a tag (e.g. deps-01 v1.0.0)
    # with different content, and a cached old copy would put a stale checksum in go.sum.
    modcache = tempfile.mkdtemp(prefix="gomodcache-")
    env["GOMODCACHE"] = modcache
    print()
    try:
        _create(plan, a, author, env)
    finally:
        subprocess.run(["chmod", "-R", "u+w", modcache], capture_output=True)  # go makes the cache read-only
        shutil.rmtree(modcache, ignore_errors=True)
    print("Done.")


def _create(plan, a, author, env):
    if plan["lang"] in SOURCES:
        src_dir, sha = fetch_source(plan["source"], env)
        try:
            for r in plan["repos"]:
                if r["action"] == "create":
                    push_copy(plan, r, src_dir, sha, author, env, a.delay)
        finally:
            shutil.rmtree(src_dir, ignore_errors=True)
    else:
        has_go = shutil.which("go") is not None
        if not has_go:
            print("Note: go isn't installed, so go.sum files are left out (go.mod still lists every require).\n")
        for r in plan["repos"]:  # order matters: deps, then libs low to high, then the no-deps repo
            if r["action"] in ("create", "seed"):
                push_repo(plan, r, author, env, has_go, a.delay)


def cmd_add_caller(a):
    """Add .github/workflows/ci.yml to existing Go repos made by this script (skips ones that have it)."""
    owner, env, author = a.owner, git_env(a.owner), identity()
    for name in a.repos:
        if gh_api(f"repos/{owner}/{name}/contents/.github/workflows/ci.yml"):
            print(f"  skip    {name}: already has .github/workflows/ci.yml"); continue
        gm = gh_api(f"repos/{owner}/{name}/contents/go.mod")
        if not gm:
            print(f"  skip    {name}: no go.mod"); continue
        text = base64.b64decode(gm["content"]).decode(errors="replace")
        private = f"github.com/{owner.lower()}/" in text.split("\n", 1)[-1].lower()
        if not a.apply:
            print(f"  would add ci.yml to {name}" + (" (private modules)" if private else "")); continue
        work = tempfile.mkdtemp(prefix=f"{name}-")
        try:
            run(["git", "clone", "-q", "--depth", "1", f"https://github.com/{owner}/{name}.git", work], env=env)
            os.makedirs(os.path.join(work, ".github", "workflows"), exist_ok=True)
            with open(os.path.join(work, ".github", "workflows", "ci.yml"), "w") as f:
                f.write(caller_workflow(owner, private))
            g = git_cmd(author)
            run(g + ["add", ".github/workflows/ci.yml"], cwd=work)
            run(g + ["commit", "-q", "-m", "Add CI workflow calling brightsheets/reusable-workflows"], cwd=work)
            run(["git", "push", "-q", "origin", "HEAD"], cwd=work, env=env)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        print(f"  added   https://github.com/{owner}/{name}/blob/main/.github/workflows/ci.yml")


def cmd_list(a):
    r = run(["gh", "repo", "list", a.owner, "--limit", "4000", "--json", "name,visibility,url,repositoryTopics"])
    rows = [x for x in json.loads(r.stdout or "[]")
            if {t["name"] for t in x.get("repositoryTopics") or []} & {TOPIC, DEMO_TOPIC}]
    for x in sorted(rows, key=lambda x: x["name"]):
        print(f'{x["name"]:<32} {x["visibility"].lower():<8} {x["url"]}')
    print(f"{len(rows)} repo(s) made by create-repo.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("plan", cmd_plan), ("create", cmd_create)):
        p = sub.add_parser(name)
        p.add_argument("items", nargs="*", help="[go|node|java] then a number of repos (default 2) or repo names")
        p.add_argument("--lang", help="go (default), node or java")
        p.add_argument("--count", type=int, help=f"number of repos, 1..{MAX_COUNT} (same as giving a number)")
        p.add_argument("--owner", default=os.environ.get("FIXTURE_OWNER", "brightsheets"))
        p.add_argument("--prefix", help="name prefix for numbered repos (default snyk-demo-<lang>)")
        p.add_argument("--deps", default="deps-01", help="Go: shared module repo (default deps-01)")
        p.add_argument("--standalone", help="Go: name of the repo with public deps only (default: the last numbered one)")
        p.add_argument("--version", default="v1.0.0", help="Go: tag for new repos (default v1.0.0)")
        p.add_argument("--public", action="store_true", help="make new repos public (default private)")
        p.add_argument("--delay", type=float, default=2.0, help="seconds between repos (default 2)")
        if name == "create":
            p.add_argument("--apply", action="store_true", help="actually create (default is dry run)")
        p.set_defaults(fn=fn)
    p = sub.add_parser("add-caller", help="add the CI caller workflow to existing Go repos")
    p.add_argument("repos", nargs="+"); p.add_argument("--apply", action="store_true")
    p.add_argument("--owner", default=os.environ.get("FIXTURE_OWNER", "brightsheets"))
    p.set_defaults(fn=cmd_add_caller)
    p = sub.add_parser("list"); p.add_argument("--owner", default=os.environ.get("FIXTURE_OWNER", "brightsheets"))
    p.set_defaults(fn=cmd_list)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
