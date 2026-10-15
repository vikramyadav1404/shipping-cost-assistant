#!/usr/bin/env python3
"""Guided walkthrough:  python demo.py   (press Enter between steps; --no-pause to run straight through)

Runs the real commands (run, repro, eval, ask) and presents the results step by step.
Changes nothing in the project except regenerating the outputs those commands normally write.
"""
import subprocess
import sys
import textwrap
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
PAUSE = "--no-pause" not in sys.argv
QUESTION = "why did Chennai-Bangalore get pricier in March 2025?"


def header(n: int, title: str, what: str):
    print("\n" + "=" * 78)
    print(f" STEP {n}/5  {title}")
    print("=" * 78)
    print(textwrap.fill(what, 78) + "\n")


def run(*args: str) -> str:
    res = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if res.returncode not in (0, 1):                     # eval.py exits 1 when a test fails; still show it
        print(res.stdout + res.stderr)
        sys.exit(f"command failed: {' '.join(args)}")
    return res.stdout


def show(text: str, keep=lambda line: True, limit: int = 40):
    lines = [l for l in text.splitlines() if l.strip() and keep(l)]
    for line in lines[:limit]:
        print("   " + line)
    if len(lines) > limit:
        print(f"   ... ({len(lines) - limit} more lines)")


def pause():
    if PAUSE:
        input("\n   [Enter] for the next step ")


def main():
    print("\nSHIPPING COST WATCHER - guided demo")
    print("Spots routes whose cost per tonne-km rises unusually, then checks the context notes")
    print("before flagging. A rise is excused only if a note genuinely explains it.")
    pause()

    header(1, "Weekly costs, two comparisons, flags   (python assistant.py run)",
           "Groups 2,940 shipments into route-weeks, computes cost per tonne-km, compares each week with "
           "the route's own 8-week history and with similar-length routes, then checks the notes. "
           "The last three lines prove our numbers equal the example rows in sample_output_format.csv.")
    show(run("assistant.py", "run", "--no-cache"), keep=lambda l: not l.startswith("wrote"))
    pause()

    header(2, "The result   (output/output.csv)",
           "Every week that rose unusually. 'Yes' = no note explains it, needs a human. "
           "'No (justified)' = a note genuinely explains it, and its id is in matched_note_id.")
    out = pd.read_csv(ROOT / "output" / "output.csv", keep_default_na=False)
    view = out[["route", "week_of", "cost_per_tonne_km", "flagged", "matched_note_id"]].copy()
    view.insert(3, "vs_own", out["vs_own_history"].str.split().str[0])
    view.insert(4, "vs_peers", out["vs_similar_routes"].str.split().str[0])
    view.columns = ["route", "week", "cost/t-km", "vs own", "vs peers", "flagged", "note"]
    show(view.to_string(index=False), limit=60)
    print("\n   Per route:")
    summary = out.groupby("route")["flagged"].agg(
        flagged=lambda s: (s == "Yes").sum(), justified=lambda s: (s != "Yes").sum())
    show(summary.to_string())
    for label, rows in (("A justified row", out[out["flagged"] != "Yes"]), ("A flagged row", out[out["flagged"] == "Yes"])):
        if len(rows):
            r = rows.iloc[0]
            print(f"\n   {label}: {r['route']}, week of {r['week_of']}")
            print(textwrap.fill(r["reason"], 74, initial_indent="     ", subsequent_indent="     "))
    pause()

    header(3, "Same input, same output   (python assistant.py repro)",
           "Runs the assistant three times in separate processes and compares the files byte by byte.")
    show(run("assistant.py", "repro"), keep=lambda l: (l.startswith("|") and not l.startswith("|---")) or "Result" in l)
    pause()

    header(4, "Can we trust it?   (python eval.py)",
           "Hand-labelled weeks, the sample numbers, a citation check on every row, and guardrail tests "
           "that deliberately break a note (wrong route, wrong week, 'costs not affected', a 2% rise "
           "against a 35% jump) to make sure the assistant is not fooled.")
    show(run("eval.py"))
    pause()

    header(5, "Ask in plain English   (python assistant.py ask \"...\")",
           f"Question: {QUESTION}  Answers come only from the computed numbers and the notes.")
    for line in run("assistant.py", "ask", QUESTION).splitlines()[:16]:
        if line.strip():
            print(textwrap.fill(line, 78, initial_indent="   ", subsequent_indent="      "))

    print("\n" + "=" * 78)
    print(" Done. Details: README.md, eval_report.md, repro/repro_check.md, token_log.md")
    print("=" * 78 + "\n")


if __name__ == "__main__":
    main()
