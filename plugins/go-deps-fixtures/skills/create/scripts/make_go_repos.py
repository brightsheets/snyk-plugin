#!/usr/bin/env python3
"""Create GitHub repos with go.mod files that depend on a shared deps module and on each other.

Layout (default owner brightsheets, deps repo deps-01):
  deps-01          shared module. If the repo is empty it is seeded with a small Go package
                   and tagged v0.1.0. If it already has code, it is used as is (needs a root
                   Go package and a vX.Y.Z tag).
  go-lib-01..NN    each requires deps-01 and calls it, and requires the one or two libs before
                   it (go-lib-03 -> go-lib-02, go-lib-01 -> deps-01). Cross deps, no cycles.
  go-standalone    a go.mod with no dependencies at all (standard library only).

Every created repo gets the topic `go-deps-fixture` so they are easy to find later.

DRY RUN by default: prints the plan and touches nothing. --apply creates repos, pushes and tags.
Never overwrites, deletes or force-pushes. A repo that already exists is reused when it was made
by this script (it has the fixture marker and the tag) and otherwise stops the run.

Usage:
  make_go_repos.py plan   [--count 10] [--owner brightsheets] [--deps deps-01] [--public]
  make_go_repos.py create [same options] --apply
  make_go_repos.py list   [--owner brightsheets]          # fixture repos already on GitHub

Needs: gh CLI signed in with the `repo` scope, git. Optional: go (writes go.sum via `go mod tidy`;
without it go.mod lists the requires and go.sum is left out).
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

def deps_files(owner, deps):
    mod = f"github.com/{owner}/{deps}"
    return {
        "go.mod": f"module {mod}\n\ngo {GO_VERSION}\n",
        "deps.go": (f"// Package deps is a shared module that the go-lib fixture repos depend on.\n"
                    f"package deps\n\nimport \"fmt\"\n\n"
                    f"// Version identifies this module.\nfunc Version() string {{ return \"{deps} v0.1.0\" }}\n\n"
                    f"// Greet is called by every dependent library.\n"
                    f"func Greet(caller string) string {{ return fmt.Sprintf(\"%s uses {deps}\", caller) }}\n"),
        "README.md": f"# {deps}\n\nShared Go module. Fixture repos tagged `{TOPIC}` depend on it.\n",
        MARKER: "created by go-deps-fixtures\n",
    }


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
        "README.md": (f"# {name}\n\nFixture Go library. Depends on `{deps_mod}`"
                      + (", " + ", ".join(f"`github.com/{owner}/{p}`" for p in prev) if prev else "")
                      + ".\n"),
        MARKER: "created by go-deps-fixtures\n",
    }


def standalone_files(owner, name):
    return {
        "go.mod": f"module github.com/{owner}/{name}\n\ngo {GO_VERSION}\n",
        "main.go": ("// Command " + name + " has no module dependencies: standard library only.\n"
                    "package main\n\nimport \"fmt\"\n\nfunc main() { fmt.Println(\"" + name + ": no dependencies\") }\n"),
        "README.md": f"# {name}\n\nFixture Go module with no dependencies (standard library only).\n",
        MARKER: "created by go-deps-fixtures\n",
    }


# ---------- planning ----------

def build_plan(a):
    if a.count < 1 or a.count > MAX_COUNT:
        sys.exit(f"--count must be 1..{MAX_COUNT}")
    owner = a.owner
    deps_mod = f"github.com/{owner}/{a.deps}"
    plan = {"owner": owner, "visibility": "public" if a.public else "private", "version": a.version, "repos": []}

    ds = repo_state(owner, a.deps)
    if ds is None:
        sys.exit(f"{owner}/{a.deps} does not exist. Create it first (it can be empty).")
    if ds["empty"]:
        plan["repos"].append({"name": a.deps, "kind": "deps", "action": "seed", "files": deps_files(owner, a.deps)})
        deps_ver, deps_callable = a.version, True
    else:
        if ds["module"] and ds["module"] != deps_mod:
            sys.exit(f"{owner}/{a.deps} go.mod declares module {ds['module']}, expected {deps_mod}.")
        if not ds["module"] or not ds["root_go"]:
            sys.exit(f"{owner}/{a.deps} has code but no go.mod or no Go package at its root; libs can't import it.")
        deps_ver = latest(ds["tags"])
        if not deps_ver:
            sys.exit(f"{owner}/{a.deps} has no vX.Y.Z tag. Tag it (e.g. v0.1.0) so go.mod can require it.")
        deps_callable = ds["marker"]  # our own seed has deps.Greet; otherwise import it for side effects
        plan["repos"].append({"name": a.deps, "kind": "deps", "action": "reuse", "version": deps_ver})
    plan["deps"] = {"module": deps_mod, "version": deps_ver, "calls_greet": deps_callable}

    width = max(2, len(str(a.count)))
    libs = [f"{a.prefix}-{i:0{width}d}" for i in range(1, a.count + 1)]
    for i, name in enumerate(libs):
        prev = libs[max(0, i - 2):i][::-1]
        plan["repos"].append({"name": name, "kind": "lib", "requires": [a.deps] + prev,
                              "files": lib_files(owner, name, deps_mod, deps_ver, deps_callable, prev, a.version)})
    plan["repos"].append({"name": a.standalone, "kind": "standalone", "requires": [],
                          "files": standalone_files(owner, a.standalone)})

    for r in plan["repos"]:
        if r["kind"] == "deps":
            continue
        st = repo_state(owner, r["name"])
        if st is None:
            r["action"] = "create"
        elif st["marker"] and a.version in st["tags"]:
            r["action"] = "reuse"
        else:
            sys.exit(f"{owner}/{r['name']} already exists and wasn't made by this script (or lacks tag {a.version}). "
                     "Pick another --prefix / --standalone name, or remove that repo yourself.")
    return plan


def print_plan(plan):
    print(f"Owner {plan['owner']}, new repos {plan['visibility']}, tag {plan['version']}")
    print(f"Shared module {plan['deps']['module']} @ {plan['deps']['version']}"
          + ("" if plan['deps']['calls_greet'] else " (imported for side effects: not seeded by this script)"))
    for r in plan["repos"]:
        req = ", ".join(r.get("requires", [])) or "none"
        print(f"  {r['action']:<7} {r['name']:<24} {r['kind']:<10} requires: {req}")
    n = sum(r["action"] in ("create", "seed") for r in plan["repos"])
    print(f"{n} repo(s) to create or seed." + ("" if n else " Nothing to do."))


# ---------- apply ----------

def push_repo(plan, r, author, env, has_go, delay):
    owner, name = plan["owner"], r["name"]
    url = f"https://github.com/{owner}/{name}.git"
    if r["action"] == "create":
        run(["gh", "repo", "create", f"{owner}/{name}", f"--{plan['visibility']}",
             "--description", "Go module fixture (go-deps-fixtures)"])
    work = tempfile.mkdtemp(prefix=f"{name}-")
    try:
        for path, text in r["files"].items():
            with open(os.path.join(work, path), "w") as f:
                f.write(text)
        if has_go and r.get("requires"):
            res = run(["go", "mod", "tidy"], cwd=work, env=env, check=False)
            if res.returncode != 0:
                sys.exit(f"go mod tidy failed in {name}:\n{res.stderr.strip()}")
        g = ["git", "-c", f"user.name={author[0]}", "-c", f"user.email={author[1]}"]
        run(["git", "init", "-q", "-b", "main"], cwd=work)
        run(g + ["add", "-A"], cwd=work)
        run(g + ["commit", "-q", "-m", f"Add {r['kind']} Go module fixture"], cwd=work)
        run(g + ["tag", "-a", plan["version"], "-m", plan["version"]], cwd=work)
        run(["git", "push", "-q", url, "main", plan["version"]], cwd=work, env=env)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    run(["gh", "repo", "edit", f"{owner}/{name}", "--add-topic", TOPIC], check=False)
    print(f"  {r['action']:<7} {owner}/{name} @ {plan['version']}")
    time.sleep(delay)


def cmd_plan(a):
    print_plan(build_plan(a))


def cmd_create(a):
    plan = build_plan(a)
    print_plan(plan)
    if not a.apply:
        print("\nDry run. Rerun with --apply to create these repos.")
        return
    has_go = shutil.which("go") is not None
    if not has_go:
        print("\nNote: go isn't installed, so go.sum files are left out (go.mod still lists every require).")
    author, env = identity(), git_env(plan["owner"])
    print()
    for r in plan["repos"]:  # order matters: deps, then libs low to high, then standalone
        if r["action"] in ("create", "seed"):
            push_repo(plan, r, author, env, has_go, a.delay)
    print("Done. List them with: make_go_repos.py list")


def cmd_list(a):
    r = run(["gh", "search", "repos", f"topic:{TOPIC}", "--owner", a.owner, "--limit", "200",
             "--json", "fullName,visibility,url"], check=False)
    rows = json.loads(r.stdout or "[]") if r.returncode == 0 else []
    for x in sorted(rows, key=lambda x: x["fullName"]):
        print(f'{x["fullName"]:<40} {x["visibility"]:<8} {x["url"]}')
    print(f"{len(rows)} fixture repo(s). Search results can lag a few minutes behind new repos.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("plan", cmd_plan), ("create", cmd_create)):
        p = sub.add_parser(name)
        p.add_argument("--owner", default=os.environ.get("FIXTURE_OWNER", "brightsheets"))
        p.add_argument("--deps", default="deps-01", help="shared module repo (default deps-01)")
        p.add_argument("--count", type=int, default=10, help=f"number of cross-dependent libs, 1..{MAX_COUNT}")
        p.add_argument("--prefix", default="go-lib", help="lib repo name prefix (default go-lib)")
        p.add_argument("--standalone", default="go-standalone", help="name of the repo with no deps")
        p.add_argument("--version", default="v0.1.0", help="tag for new repos (default v0.1.0)")
        p.add_argument("--public", action="store_true", help="make new repos public (default private)")
        p.add_argument("--delay", type=float, default=2.0, help="seconds between repos (default 2)")
        if name == "create":
            p.add_argument("--apply", action="store_true", help="actually create (default is dry run)")
        p.set_defaults(fn=fn)
    p = sub.add_parser("list"); p.add_argument("--owner", default=os.environ.get("FIXTURE_OWNER", "brightsheets"))
    p.set_defaults(fn=cmd_list)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
