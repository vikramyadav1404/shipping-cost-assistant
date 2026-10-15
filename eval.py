#!/usr/bin/env python3
"""Lightweight eval harness:  python eval.py   ->  prints a score and writes eval_report.md

Part A - a small hand-labelled set: verdicts a careful human reaches by reading the notes,
         plus the exact numbers from sample_output_format.csv.
Part B - integrity: every verdict is internally consistent (no invented or mismatched citations).
Part C - guardrail tests: break or add a note on purpose and check the verdict changes (or not)
         the way the brief demands. Each test runs the real assistant on a temporary copy of the data.

It only uses the command line and the output CSV, so it tests the assistant exactly as graders would.
"""
from __future__ import annotations

import csv
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

# Part A: human labels (route, week_of, expected flagged, expected note id, why a human says so)
GOLD = [
    ("Delhi-Jaipur", "2024-11-11", "Yes", "", "no note covers Delhi-Jaipur in Nov 2024 (N007 is May 2024 and says roads improved)"),
    ("Delhi-Jaipur", "2024-11-18", "Yes", "", "same spike, second week, still no note"),
    ("Ahmedabad-Mumbai", "2025-01-20", "No (justified)", "N002", "festival-week surcharge on this corridor, that exact week"),
    ("Chennai-Bangalore", "2025-02-24", "No (justified)", "N001", "flooding Feb 24 - Mar 8"),
    ("Chennai-Bangalore", "2025-03-03", "No (justified)", "N001", "flooding Feb 24 - Mar 8"),
    ("Chennai-Bangalore", "2025-03-10", "Yes", "", "flood window ended Mar 8; N009 says the route is back to normal"),
    ("Chennai-Bangalore", "2025-03-17", "Yes", "", "still high after the flood window"),
    ("Mumbai-Pune", "2025-09-15", "Yes", "", "N006 only says demand was stable"),
]
MUST_NOT_APPEAR = [("Mumbai-Delhi", "2025-05-12", "diesel week (+11.6%): an ordinary 5-7% nationwide rise, not an anomaly")]


def run_assistant(notes: pd.DataFrame | None = None) -> pd.DataFrame:
    """Run the real assistant on a temp copy of data/ (optionally with modified notes)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for name in ("shipment_records.csv", "context_notes.csv", "sample_output_format.csv"):
            if (DATA / name).exists():
                shutil.copy(DATA / name, tmp / name)
        if notes is not None:
            notes.to_csv(tmp / "context_notes.csv", index=False)
        out = tmp / "out.csv"
        subprocess.run([sys.executable, str(ROOT / "assistant.py"), "run", "--no-llm", "--csv-only",
                        "--data-dir", str(tmp), "--out", str(out)], check=True, capture_output=True)
        return pd.read_csv(out, dtype=str, keep_default_na=False)


def verdict(out: pd.DataFrame, route: str, week: str) -> tuple[str, str] | None:
    hit = out[(out["route"] == route) & (out["week_of"] == week)]
    return None if hit.empty else (hit.iloc[0]["flagged"], hit.iloc[0]["matched_note_id"])


def read_sample() -> list[list[str]]:
    """Sample rows; an unquoted comma inside the last column is joined back (the provided file has one)."""
    with open(DATA / "sample_output_format.csv", newline="", encoding="utf-8-sig") as fh:
        rows = [r for r in csv.reader(fh) if any(c.strip() for c in r)]
    n = len(rows[0])
    return [r[:n - 1] + [",".join(r[n - 1:])] for r in rows[1:]]


def main():
    notes = pd.read_csv(DATA / "context_notes.csv", dtype=str, keep_default_na=False)
    base = run_assistant()
    results: list[tuple[str, str, bool, str]] = []          # (part, case, passed, detail)

    # ---- Part A: labelled verdicts + sample numbers
    for route, week, flag, note, why in GOLD:
        got = verdict(base, route, week)
        results.append(("A labelled", f"{route} {week} -> {flag} {note}".strip(),
                        got == (flag, note), f"got {got} | {why}"))
    for route, week, why in MUST_NOT_APPEAR:
        got = verdict(base, route, week)
        results.append(("A labelled", f"{route} {week} not flagged", got is None, f"got {got} | {why}"))
    for s in read_sample():
        hit = base[(base["route"] == s[0]) & (base["week_of"] == s[1])]
        row = None if hit.empty else hit.iloc[0]
        ours = None if row is None else (row.iloc[2], row.iloc[3].split(" ")[0], row.iloc[4].split(" ")[0])
        theirs = (s[2], s[3].split(" ")[0], s[4].split(" ")[0])
        results.append(("A labelled", f"sample numbers {s[0]} {s[1]}", ours == theirs, f"ours {ours} sample {theirs}"))

    # ---- Part B: citation integrity on every output row
    known = {r.note_id: r.applies_to.lower() for r in notes.itertuples()}
    bad = []
    for r in base.itertuples(index=False):
        nid = r.matched_note_id
        if r.flagged == "Yes" and nid:
            bad.append(f"{r.route} {r.week_of}: flagged but cites {nid}")
        if r.flagged != "Yes":
            if nid not in known:
                bad.append(f"{r.route} {r.week_of}: justified by unknown note '{nid}'")
            elif known[nid] not in ("all routes", r.route.lower()):
                bad.append(f"{r.route} {r.week_of}: {nid} is for another route")
            elif nid not in r.reason:
                bad.append(f"{r.route} {r.week_of}: reason does not name {nid}")
    results.append(("B integrity", f"all {len(base)} rows: citations exist, fit the route, named in reason",
                    not bad, "; ".join(bad) or "ok"))

    # ---- Part C: guardrail tests (each one modifies the notes, then reruns the real assistant)
    def with_note(**changes) -> pd.DataFrame:
        df = notes.copy()
        for nid, fields in changes.items():
            for k, v in fields.items():
                df.loc[df["note_id"] == nid, k] = v
        return df

    def plus(*rows) -> pd.DataFrame:
        return pd.concat([notes, pd.DataFrame(rows, columns=notes.columns)], ignore_index=True)

    tests = [
        ("wrong route: N002 moved to Mumbai-Pune", with_note(N002={"applies_to": "Mumbai-Pune"}),
         "Ahmedabad-Mumbai", "2025-01-20", ("Yes", "")),
        ("wrong window: N002 dated 6 weeks later", with_note(N002={"date": "2025-03-03"}),
         "Ahmedabad-Mumbai", "2025-01-20", ("Yes", "")),
        ("note says costs NOT affected", with_note(N002={"note": "A regional festival week on the Ahmedabad-Mumbai "
                                                          "corridor caused minor delays; costs were not significantly affected."}),
         "Ahmedabad-Mumbai", "2025-01-20", ("Yes", "")),
        ("stated rise too small (2% vs +35.5%)", plus(["N900", "2024-11-11", "Delhi-Jaipur",
                                                       "Toll charges on the Delhi-Jaipur stretch rose this week, pushing up costs by about 2%."]),
         "Delhi-Jaipur", "2024-11-11", ("Yes", "")),
        ("positive control: a genuine note is accepted", plus(["N901", "2024-11-11", "Delhi-Jaipur",
                                                               "A truckers' strike on the Delhi-Jaipur stretch from Nov 11 to Nov 24 forced transporters to charge higher trip costs."]),
         "Delhi-Jaipur", "2024-11-18", ("No (justified)", "N901")),
    ]
    for name, mod_notes, route, week, expected in tests:
        try:
            got = verdict(run_assistant(mod_notes), route, week)
        except subprocess.CalledProcessError as exc:
            got = f"run failed: {exc.stderr.decode(errors='replace')[-200:]}"
        results.append(("C guardrail", name, got == expected, f"{route} {week}: expected {expected}, got {got}"))

    # ---- report
    passed = sum(r[2] for r in results)
    lines = ["# Eval report", "", f"**Score: {passed}/{len(results)} passed**", "",
             "| part | case | result | detail |", "|---|---|---|---|"]
    lines += [f"| {p} | {c} | {'PASS' if ok else 'FAIL'} | {d.replace('|', '/')} |" for p, c, ok, d in results]
    (ROOT / "eval_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for p, c, ok, d in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {p}: {c}" + ("" if ok else f"  ({d})"))
    print(f"\nScore: {passed}/{len(results)} passed  -> eval_report.md")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
