#!/usr/bin/env python3
"""Final step: one Markdown table that sums up the run, like the plan table.

  summary_table.py --import-log snyk-import-log.json [--pr-log pr-log.json]
                   [--settings applied|unchanged|not-run|failed|shown]

Reads only the logs this run wrote; prints the table, then a one-line total. Read-only.
"""
import argparse, json, re, sys

SHORT = {"Snyk Code supported files": "Snyk Code"}


def projects(rec):
    out = []
    for p in rec.get("projects") or []:
        name = SHORT.get(p.get("target"), p.get("target") or "?")
        out.append(("✓ " if p.get("ok") else "✗ ") + name)
    return ", ".join(out) or "—"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--import-log", required=True)
    ap.add_argument("--pr-log")
    ap.add_argument("--settings", default="not-run",
                    choices=["applied", "unchanged", "not-run", "failed", "shown"])
    a = ap.parse_args()
    imports = json.load(open(a.import_log))
    prs = {r["repo"].lower(): r for r in (json.load(open(a.pr_log)) if a.pr_log else [])}
    settings_txt = {"applied": "✓ recommended applied", "unchanged": "✓ already recommended",
                    "not-run": "—", "failed": "✗ failed", "shown": "shown only"}[a.settings]

    head = ["Repo", "Import", "Projects", "PR checks & fix PRs", ".snyk PR", "Result"]
    print("| " + " | ".join(head) + " |")
    print("|" + "|".join("---" for _ in head) + "|")
    done = stopped = skipped = 0
    for rec in imports:
        repo, res = rec["repo"], rec.get("result")
        projs = rec.get("projects") or []
        if res in ("imported", "imported-after-fix"):
            imp = "✓ imported"
        elif res == "failed" and any(p.get("ok") for p in projs):
            imp = "✗ partial"
        elif res == "failed":
            imp = "✗ failed"
        elif res == "skipped":
            imp = "skipped"
        else:
            imp = res or "?"
        ok = imp == "✓ imported"
        pr = prs.get(repo.lower())
        if not ok:
            pr_txt = "—"
        elif not pr:
            pr_txt = "not run"
        elif pr["status"] == "ok" and pr.get("url"):
            n = re.search(r"/pull/(\d+)", pr["url"])
            pr_txt = f"[#{n.group(1)}]({pr['url']})" if n else pr["url"]
        elif pr["status"] == "skip":
            pr_txt = f"skipped: {pr.get('reason')}"
        else:
            pr_txt = f"✗ {pr.get('reason')}"
        if res == "skipped":
            result = f"Skipped: {rec.get('detail', '')}".strip(); skipped += 1
        elif not ok:
            result = "Stopped at import"; stopped += 1
        elif pr and pr["status"] == "fail":
            result = "Stopped at .snyk PR"; stopped += 1
        else:
            result = "Done"; done += 1
        cells = [repo, imp, projects(rec), settings_txt if ok else "—", pr_txt, result]
        print("| " + " | ".join(str(c).replace("|", "\\|") for c in cells) + " |")
    print()
    print(f"Done: {done} · Stopped: {stopped} · Skipped: {skipped}")


if __name__ == "__main__":
    main()
