#!/usr/bin/env python3
"""Final step: one Markdown table that sums up the run, like the plan table.

  summary_table.py --import-log snyk-import-log.json [--pr-log pr-log.json] [--ruleset-log ruleset-log.json]
                   [--settings applied|unchanged|not-run|failed|shown]

Reads only the logs this run wrote; prints the table, then a one-line total. Read-only.
"""
import argparse, json, re, sys, textwrap

SHORT = {"Snyk Code supported files": "Snyk Code"}


def projects(rec):
    out = []
    for p in rec.get("projects") or []:
        name = SHORT.get(p.get("target"), p.get("target") or "?")
        out.append(("✓ " if p.get("ok") else "✗ ") + name)
    return ", ".join(out) or "—"


def box(head, rows, width=26):
    """Draw a table with box characters, wrapping long cells, like the rendered plan table."""
    cols = list(zip(head, *rows))
    widths = [max(len(str(c)) for c in col) if i == 0 else min(width, max(len(str(c)) for c in col))
              for i, col in enumerate(cols)]  # never wrap the repo name
    def lines(cells):
        wrapped = [textwrap.wrap(str(c), w) or [""] for c, w in zip(cells, widths)]
        h = max(len(x) for x in wrapped)
        return ["│ " + " │ ".join((x[i] if i < len(x) else "").ljust(w) for x, w in zip(wrapped, widths)) + " │"
                for i in range(h)]
    edge = lambda l, m, r: l + m.join("─" * (w + 2) for w in widths) + r
    out = [edge("┌", "┬", "┐")]
    out += lines(head)
    out.append(edge("├", "┼", "┤"))
    for i, r in enumerate(rows):
        out += lines(r)
        out.append(edge("├", "┼", "┤") if i < len(rows) - 1 else edge("└", "┴", "┘"))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--import-log", required=True)
    ap.add_argument("--pr-log")
    ap.add_argument("--ruleset-log")
    ap.add_argument("--markdown", action="store_true", help="print a Markdown table instead of the drawn box")
    ap.add_argument("--settings", default="not-run",
                    choices=["applied", "unchanged", "not-run", "failed", "shown"])
    a = ap.parse_args()
    imports = json.load(open(a.import_log))
    prs = {r["repo"].lower(): r for r in (json.load(open(a.pr_log)) if a.pr_log else [])}
    gates = {r["repo"].lower(): r for r in (json.load(open(a.ruleset_log)) if a.ruleset_log else [])}
    settings_txt = {"applied": "✓ recommended applied", "unchanged": "✓ already recommended",
                    "not-run": "—", "failed": "✗ failed", "shown": "shown only"}[a.settings]

    head = ["Repo", "Import", "Projects", "PR checks & fix PRs", ".snyk PR", "Merge gate", "Result"]
    rows, links = [], []
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
            pr_txt = f"#{n.group(1)}" if n else pr["url"]
            links.append(f"{repo}: {pr['url']}")
        elif pr["status"] == "skip":
            pr_txt = f"skipped: {pr.get('reason')}"
        else:
            pr_txt = f"✗ {pr.get('reason')}"
        g = gates.get(repo.lower())
        if not ok or (pr and pr["status"] == "fail"):
            gate_txt = "—"
        elif not g:
            gate_txt = "not run"
        elif g["status"] == "ok":
            gate_txt = "✓ added" if g.get("applied") else "would add"
        elif g["status"] == "skip":
            gate_txt = "✓ already gated" if "already applies" in (g.get("reason") or "") else f"skipped: {g.get('reason')}"
        else:
            gate_txt = f"✗ {g.get('reason')}"
        if res == "skipped":
            result = f"Skipped: {rec.get('detail', '')}".strip(); skipped += 1
        elif not ok:
            result = "Stopped at import"; stopped += 1
        elif pr and pr["status"] == "fail":
            result = "Stopped at .snyk PR"; stopped += 1
        elif g and g["status"] == "fail":
            result = "Stopped at merge gate"; stopped += 1
        else:
            result = "Done"; done += 1
        rows.append([repo, imp, projects(rec), settings_txt if ok else "—", pr_txt, gate_txt, result])
    if a.markdown:
        print("| " + " | ".join(head) + " |")
        print("|" + "|".join("---" for _ in head) + "|")
        for r in rows:
            print("| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |")
    else:
        print(box(head, rows))
    print()
    if links:
        print("PRs:")
        for l in links:
            print(f"  {l}")
        print()
    print(f"Done: {done} · Stopped: {stopped} · Skipped: {skipped}")


if __name__ == "__main__":
    main()
