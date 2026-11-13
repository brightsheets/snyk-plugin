#!/usr/bin/env python3
"""Detect a repository's tech stack and map it to Snyk products.

Usage:
  detect_stack.py --path ./my-repo
  detect_stack.py --github owner/repo [--branch main]     (uses `gh api`, no clone)

Prints JSON to stdout.
"""
import argparse, fnmatch, json, os, re, subprocess, sys
from collections import Counter

SKIP_DIRS = {".git", "node_modules", "vendor", "third_party", "thirdparty", "Pods",
             ".venv", "venv", "dist", "build", "target", "Library", "Intermediate",
             "Binaries", "DerivedDataCache", ".terraform",
             "test", "tests", "__tests__", "fixtures", "__fixtures__", "testdata", "spec"}

# manifest file -> (ecosystem, package manager)
MANIFESTS = [
    ("package.json", "javascript", "npm"), ("package-lock.json", "javascript", "npm"),
    ("yarn.lock", "javascript", "yarn"), ("pnpm-lock.yaml", "javascript", "pnpm"),
    ("requirements*.txt", "python", "pip"), ("Pipfile", "python", "pipenv"),
    ("Pipfile.lock", "python", "pipenv"), ("pyproject.toml", "python", "poetry/pep621"),
    ("poetry.lock", "python", "poetry"), ("setup.py", "python", "setuptools"),
    ("uv.lock", "python", "uv"),
    ("pom.xml", "java", "maven"), ("build.gradle", "java", "gradle"),
    ("build.gradle.kts", "kotlin", "gradle"), ("build.sbt", "scala", "sbt"),
    ("go.mod", "go", "gomodules"), ("Gopkg.lock", "go", "dep"),
    ("Gemfile", "ruby", "bundler"), ("Gemfile.lock", "ruby", "bundler"),
    ("composer.json", "php", "composer"), ("composer.lock", "php", "composer"),
    ("*.csproj", "dotnet", "nuget"), ("*.fsproj", "dotnet", "nuget"),
    ("*.vbproj", "dotnet", "nuget"), ("packages.config", "dotnet", "nuget"),
    ("project.assets.json", "dotnet", "nuget"), ("Directory.Packages.props", "dotnet", "nuget"),
    ("Cargo.toml", "rust", "cargo"), ("Cargo.lock", "rust", "cargo"),
    ("Package.swift", "swift", "swiftpm"), ("Podfile", "swift", "cocoapods"),
    ("Podfile.lock", "swift", "cocoapods"), ("mix.exs", "elixir", "hex"),
    ("pubspec.yaml", "dart", "pub"), ("conanfile.txt", "cpp", "conan"),
    ("conanfile.py", "cpp", "conan"), ("vcpkg.json", "cpp", "vcpkg"),
]

CODE_EXT = {
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".py": "python", ".java": "java",
    ".kt": "kotlin", ".scala": "scala", ".groovy": "groovy", ".go": "go", ".rb": "ruby",
    ".php": "php", ".cs": "csharp", ".vb": "vbnet", ".swift": "swift", ".rs": "rust",
    ".c": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".h": "c", ".hpp": "cpp",
    ".ex": "elixir", ".exs": "elixir", ".dart": "dart", ".cls": "apex",
}
SNYK_CODE_LANGS = set(CODE_EXT.values())

CONTAINER = ["Dockerfile", "Dockerfile.*", "*.Dockerfile", "Containerfile",
             "docker-compose*.yml", "docker-compose*.yaml", "compose.yml", "compose.yaml"]
IAC = {
    "terraform": ["*.tf", "*.tf.json", "*.tfvars"],
    "kubernetes": ["kustomization.yaml", "kustomization.yml", "k8s/*.yaml", "k8s/*.yml",
                   "kubernetes/*.yaml", "manifests/*.yaml", "deploy/*.yaml"],
    "helm": ["Chart.yaml"],
    "cloudformation": ["*.template", "*.template.json", "*.template.yaml", "cfn/*.yaml",
                       "cloudformation/*.yaml", "template.yaml", "template.yml"],
    "arm": ["azuredeploy.json", "*.bicep"],
    "serverless": ["serverless.yml", "serverless.yaml"],
}
CI = {
    "github_actions": [".github/workflows/*.yml", ".github/workflows/*.yaml"],
    "jenkins": ["Jenkinsfile", "Jenkinsfile.*"], "gitlab": [".gitlab-ci.yml"],
    "azure_pipelines": ["azure-pipelines.yml", "azure-pipelines*.yml"],
    "circleci": [".circleci/config.yml"], "bitbucket": ["bitbucket-pipelines.yml"],
    "teamcity": [".teamcity/*"], "buildkite": [".buildkite/*"],
}
ENGINES = {
    "unity": ["ProjectSettings/ProjectVersion.txt", "Packages/manifest.json", "*.unity"],
    "unreal": ["*.uproject", "*.uplugin"],
    "godot": ["project.godot"],
}


def list_local(path):
    out = []
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            out.append(os.path.relpath(os.path.join(root, f), path).replace(os.sep, "/"))
    return out


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def list_github(repo, branch):
    meta = json.loads(gh("api", f"repos/{repo}"))
    branch = branch or meta["default_branch"]
    tree = json.loads(gh("api", f"repos/{repo}/git/trees/{branch}?recursive=1"))
    paths = [t["path"] for t in tree.get("tree", []) if t.get("type") == "blob"]
    paths = [p for p in paths if not (set(p.split("/")[:-1]) & SKIP_DIRS)]
    return paths, {"branch": branch, "archived": meta.get("archived", False),
                   "private": meta.get("private"), "fork": meta.get("fork", False),
                   "language": meta.get("language"), "truncated": tree.get("truncated", False),
                   "pushed_at": meta.get("pushed_at")}


def match(path, patterns):
    base = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(base, p) if "/" not in p else fnmatch.fnmatch(path, "*" + p) or fnmatch.fnmatch(path, p)
               for p in patterns)


def detect(paths):
    eco, managers, manifest_files = Counter(), {}, []
    for p in paths:
        base = p.rsplit("/", 1)[-1]
        for pat, e, m in MANIFESTS:
            if fnmatch.fnmatch(base, pat):
                eco[e] += 1; managers.setdefault(e, set()).add(m); manifest_files.append(p)
                break
    langs = Counter()
    for p in paths:
        ext = os.path.splitext(p)[1].lower()
        if ext in CODE_EXT:
            langs[CODE_EXT[ext]] += 1
    containers = [p for p in paths if match(p, CONTAINER)]
    iac = {k: [p for p in paths if match(p, v)] for k, v in IAC.items()}
    iac = {k: v for k, v in iac.items() if v}
    ci = sorted(k for k, v in CI.items() if any(match(p, v) for p in paths))
    engines = sorted(k for k, v in ENGINES.items() if any(match(p, v) for p in paths))
    has_snyk_policy = any(p.rsplit("/", 1)[-1] == ".snyk" for p in paths)
    has_snyk_ci = any(re.search(r"snyk", p, re.I) for p in paths if p.startswith(".github/workflows/"))

    notes = []
    products = {}
    if eco:
        products["open_source"] = {"enabled": True, "cmd": "snyk test --all-projects",
                                   "monitor": "snyk monitor --all-projects"}
        if "python" in eco and managers["python"] <= {"setuptools", "poetry/pep621"}:
            notes.append("Python without a lockfile/requirements: Snyk CLI needs deps installed (pip install) before `snyk test`.")
        if {"java", "kotlin", "scala"} & set(eco):
            notes.append("JVM builds: CLI scans need the build tool (mvn/gradle/sbt) on PATH; SCM import works without it.")
        if "dotnet" in eco:
            notes.append(".NET: run `dotnet restore` before CLI scans so project.assets.json exists.")
    code_langs = sorted(l for l in langs if l in SNYK_CODE_LANGS)
    if code_langs:
        products["code"] = {"enabled": True, "cmd": "snyk code test", "languages": code_langs}
    if containers:
        products["container"] = {"enabled": True, "files": containers[:50],
                                 "cmd": "snyk container test <image> --file=<Dockerfile>"}
    if iac:
        products["iac"] = {"enabled": True, "types": sorted(iac), "cmd": "snyk iac test"}
    if ({"c", "cpp"} & set(langs)) and not ({"cpp"} & set(eco)):
        products["unmanaged_cpp"] = {"enabled": True, "cmd": "snyk test --unmanaged"}
        notes.append("C/C++ without a package manager: use `snyk test --unmanaged` for vendored libs.")
    if "unreal" in engines:
        notes.append("Unreal project: exclude Engine/, Intermediate/, Binaries/ from Snyk Code; C++ deps are usually vendored (use --unmanaged).")
    if "unity" in engines:
        notes.append("Unity project: Packages/manifest.json is UPM (not scanned by Snyk OS); C# scanned by Snyk Code. Exclude Library/ and Temp/.")
    if not products:
        notes.append("No Snyk-supported stack detected (docs/assets-only repo?). Consider skipping.")

    return {
        "ecosystems": {e: {"manifests": eco[e], "package_managers": sorted(managers[e])} for e in eco},
        "languages": dict(langs.most_common()),
        "manifest_files": manifest_files[:200],
        "containers": containers[:50],
        "iac": iac,
        "ci_systems": ci,
        "game_engines": engines,
        "already_onboarded": {"snyk_policy_file": has_snyk_policy, "snyk_in_github_actions": has_snyk_ci},
        "snyk_products": products,
        "notes": notes,
        "file_count": len(paths),
    }


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--path"); g.add_argument("--github", metavar="OWNER/REPO")
    ap.add_argument("--branch")
    a = ap.parse_args()
    if a.path:
        result = {"source": os.path.abspath(a.path), **detect(list_local(a.path))}
    else:
        try:
            paths, meta = list_github(a.github, a.branch)
        except subprocess.CalledProcessError as e:
            print(json.dumps({"source": a.github, "error": e.stderr.strip()})); sys.exit(1)
        result = {"source": a.github, **meta, **detect(paths)}
        if meta["truncated"]:
            result["notes"].append("GitHub tree was truncated (very large repo); clone and use --path for full accuracy.")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
