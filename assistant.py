#!/usr/bin/env python3
"""Shipping-cost watcher.

    python assistant.py run                      # full run -> output/output.csv + audit files + token log
    python assistant.py repro                    # 3 runs on the same input, compared byte by byte
    python assistant.py ask "why did Chennai-Bangalore get pricier in March 2025?"

Flags: --no-llm (force offline), --no-cache (re-call the LLM instead of using cached answers),
       --data-dir DIR (default ./data).
"""
from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

import core
import notes
import qa
from llm import LLMClient, TokenLog, estimate_tokens

ROOT = Path(__file__).resolve().parent
CONFIG = {
    "history_weeks": 8,            # from the brief
    "own_threshold_pct": 20.0,     # out of the ordinary vs own trailing average
    "peer_threshold_pct": 20.0,    # out of the ordinary vs same-type routes, same week
    "magnitude_tolerance_pp": 3.0, # slack when checking a note's stated % against the observed rise
    "candidate_window_days": 28,   # how far from the flagged week a note is still "nearby" for the explanation
}
OUTPUT_COLUMNS = ["route", "week_of", "cost_per_tonne_km", "vs_own_history", "vs_similar_routes",
                  "flagged", "matched_note_id", "reason"]


def fmt_own(r) -> str:
    if pd.isna(r.vs_own_pct):
        return "n/a (no prior weeks)"
    return f"{r.vs_own_pct:+.1f}% vs trailing {r.own_weeks_used}-wk avg ({r.own_avg:.2f})"


def fmt_peer(r) -> str:
    if pd.isna(r.vs_similar_pct):
        return f"n/a (no other {r.route_type} routes that week)"
    return f"{r.vs_similar_pct:+.1f}% vs other {r.route_type} routes avg ({r.peer_avg:.2f})"


def run_pipeline(data_dir: Path, use_llm: bool, use_cache: bool):
    shipments, issues = core.load_shipments(data_dir / "shipment_records.csv")
    weekly = core.weekly_route_costs(shipments)
    weekly = core.add_baselines(weekly, CONFIG["history_weeks"])
    weekly = core.mark_anomalies(weekly, CONFIG["own_threshold_pct"], CONFIG["peer_threshold_pct"])

    notes_df = notes.load_notes(data_dir / "context_notes.csv")
    routes = sorted(weekly["route"].unique())
    log = TokenLog()
    client = LLMClient(ROOT / "cache", log, use_cache=use_cache) if use_llm else None
    facts, llm_log = notes.build_facts(notes_df, routes, client)

    matcher = notes.NoteMatcher(facts, CONFIG)
    rows, verdicts = [], {}
    for r in weekly[weekly["anomaly"]].sort_values(["week_of", "route"]).itertuples(index=False):
        v = matcher.decide(r)
        verdicts[(r.route, r.week_of)] = v
        rows.append({"route": r.route, "week_of": f"{r.week_of:%Y-%m-%d}",
                     "cost_per_tonne_km": f"{r.cost_per_tonne_km:.2f}",
                     "vs_own_history": fmt_own(r), "vs_similar_routes": fmt_peer(r),
                     "flagged": "No (justified)" if v.justified else "Yes",
                     "matched_note_id": v.note_id, "reason": v.reason})
    output = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    return dict(shipments=shipments, issues=issues, weekly=weekly, notes_df=notes_df, facts=facts,
                llm_log=llm_log, output=output, verdicts=verdicts, token_log=log, client=client, routes=routes)


def check_against_sample(res, sample_path: Path) -> list[str]:
    """Compare our numbers with the example rows in sample_output_format.csv (if it has any)."""
    if not sample_path.exists():
        return ["sample_output_format.csv not found - skipped"]
    header, body = read_sample(sample_path)
    same_names = sorted(header) == sorted(OUTPUT_COLUMNS)
    if header == OUTPUT_COLUMNS:
        out = ["header matches sample_output_format.csv"]
    elif same_names:
        out = ["sample has the same columns in another order -> output follows the sample's order"]
    elif len(header) == len(OUTPUT_COLUMNS):
        out = [f"sample header {header} differs from defaults -> output uses the sample's names"]
    else:
        out = [f"sample header {header} has {len(header)} columns, not {len(OUTPUT_COLUMNS)} -> output keeps default names"]
    w = res["weekly"]
    for row in body:
        s = dict(zip(header if same_names else OUTPUT_COLUMNS, row))   # sample values by column name
        route, week_text = s.get("route", ""), s.get("week_of", "")
        try:
            week = core.parse_dates(pd.Series([week_text])).iloc[0]
        except ValueError:
            out.append(f"{route}: could not parse week '{week_text}'"); continue
        hit = w[(w["route"] == route) & (w["week_of"] == week)]
        if hit.empty:
            out.append(f"{route} {week:%Y-%m-%d}: route-week not found"); continue
        h = hit.iloc[0]
        ours = (f"{h.cost_per_tonne_km:.2f}", f"{h.vs_own_pct:+.1f}%", f"{h.vs_similar_pct:+.1f}%")
        theirs = (s.get("cost_per_tonne_km", "").strip(), s.get("vs_own_history", "").split(" ")[0],
                  s.get("vs_similar_routes", "").split(" ")[0])
        out.append(f"{route} {week:%Y-%m-%d}: ours={ours} sample={theirs} -> {'MATCH' if ours == theirs else 'DIFF'}")
    return out


def write_token_log(res, path_md: Path):
    client: LLMClient | None = res["client"]
    log: TokenLog = res["token_log"]
    lines = ["# Token / cost log - one full run", ""]
    if client is not None and client.enabled:
        s = log.summary(client.price_in, client.price_out)
        lines += [f"- Model: `{client.model}` via `{client.base_url}` (temperature 0)",
                  f"- LLM calls made: **{s['llm_calls_made']}** (cache hits: {s['cache_hits']})",
                  f"- Input tokens: **{s['input_tokens']}**, output tokens: **{s['output_tokens']}** (reported by the API)",
                  f"- Cost at ${client.price_in}/M in, ${client.price_out}/M out: **${s['est_cost_usd']}** "
                  f"(free tier: $0)", "", "| call | input | output | cached |", "|---|---|---|---|"]
        lines += [f"| {c['purpose']} | {c['input_tokens']} | {c['output_tokens']} | {c['cached']} |" for c in log.calls]
    else:
        prompts = [notes.NOTE_SYSTEM + notes.note_prompt(r, res["routes"]) for r in res["notes_df"].itertuples(index=False)]
        est_in = sum(estimate_tokens(p) for p in prompts)
        est_out = 110 * len(prompts)
        from llm import DEFAULT_PRICE_IN as pin, DEFAULT_PRICE_OUT as pout, DEFAULT_MODEL
        why = "--no-llm was given" if client is None else "no API key set"
        lines += [f"- This run used **no LLM** ({why}): **0 calls, 0 tokens, $0**.",
                  "", "ESTIMATE for LLM mode (not measured - chars/4 rule, ~110 output tokens per note):",
                  f"- Calls: {len(prompts)} (one per context note; repeat runs hit the cache = 0 calls)",
                  f"- Input tokens: ~{est_in}, output tokens: ~{est_out}",
                  f"- Cost on {DEFAULT_MODEL} at ${pin}/M in, ${pout}/M out: ~${est_in/1e6*pin + est_out/1e6*pout:.5f} "
                  f"(free tier: $0)",
                  "", "Run with `LLM_API_KEY` set to replace these estimates with real numbers."]
    lines += ["", "The weekly numbers, comparisons and flags never call an LLM (pure pandas), so cost does",
              "not grow with the number of shipments - only with the number of notes."]
    path_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_sample(path: Path) -> tuple[list[str], list[list[str]]]:
    """Read sample_output_format.csv tolerantly. The provided sample has an unquoted comma inside the
    last (reason) column, so a row can have extra fields; they are joined back into the reason."""
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        rows = [r for r in csv.reader(fh) if any(c.strip() for c in r)]
    header = [c.strip() for c in rows[0]]
    body = []
    for r in rows[1:]:
        if len(r) > len(header):
            r = r[:len(header) - 1] + [",".join(r[len(header) - 1:])]
        body.append(r + [""] * (len(header) - len(r)))
    return header, body


def sample_header(data_dir: Path) -> list[str] | None:
    """Use the exact column names of sample_output_format.csv (same 8 fields, same order as the screenshot)."""
    path = data_dir / "sample_output_format.csv"
    if not path.exists():
        return None
    cols, _ = read_sample(path)
    return cols if len(cols) == len(OUTPUT_COLUMNS) else None


def match_sample_columns(output: pd.DataFrame, header: list[str] | None) -> pd.DataFrame:
    """Same names in another order -> reorder by name (never relabel data); other names -> rename by position."""
    if not header:
        return output
    if sorted(header) == sorted(OUTPUT_COLUMNS):
        return output[header]
    return output.set_axis(header, axis=1)


def cmd_run(args):
    res = run_pipeline(Path(args.data_dir), use_llm=not args.no_llm, use_cache=not args.no_cache)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    output = match_sample_columns(res["output"], sample_header(Path(args.data_dir)))
    output.to_csv(out_path, index=False, lineterminator="\n")
    if args.csv_only:
        return
    audit = out_path.parent
    w = res["weekly"].copy()
    w["week_of"] = w["week_of"].dt.strftime("%Y-%m-%d")
    w.round(4).to_csv(audit / "weekly_metrics.csv", index=False, lineterminator="\n")
    (audit / "note_facts.json").write_text(notes.facts_to_json(res["facts"]) + "\n", encoding="utf-8")
    write_token_log(res, ROOT / "token_log.md")

    o = res["output"]
    mode = "LLM + rules" if res["client"] is not None and res["client"].enabled else "rules only (offline)"
    print(f"mode: {mode}")
    for msg in res["issues"] + res["llm_log"]:
        print("note:", msg)
    print(f"route-weeks analysed: {len(res['weekly'])}, anomalies: {len(o)}, "
          f"flagged (unexplained): {(o.flagged == 'Yes').sum()}, justified: {(o.flagged != 'Yes').sum()}")
    try:
        checks = check_against_sample(res, Path(args.data_dir) / "sample_output_format.csv")
    except Exception as exc:
        checks = [f"could not read sample file ({type(exc).__name__}: {exc}) - output was still written"]
    for line in checks:
        print("sample check:", line)
    print(f"wrote {out_path}")


def cmd_repro(args):
    rdir = ROOT / "repro"
    rdir.mkdir(exist_ok=True)
    files, hashes = [], []
    for i in range(1, args.runs + 1):
        path = rdir / f"run{i}.csv"
        cmd = [sys.executable, str(ROOT / "assistant.py"), "run", "--out", str(path), "--csv-only",
               "--data-dir", args.data_dir, "--no-cache"] + (["--no-llm"] if args.no_llm else [])
        subprocess.run(cmd, check=True)
        files.append(path)
        hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())

    base = files[0].read_text(encoding="utf-8").splitlines()
    report = ["# Reproducibility check", "", f"{args.runs} independent runs (separate processes, LLM cache "
              "disabled so any LLM would be re-called each time) on the same input.", "",
              "| run | rows | sha256 |", "|---|---|---|"]
    for f, h in zip(files, hashes):
        report.append(f"| {f.name} | {len(pd.read_csv(f))} | `{h[:16]}...` |")
    identical = len(set(hashes)) == 1
    report += ["", f"**Result: {'IDENTICAL - all runs byte-for-byte the same' if identical else 'DIFFERENT'}**", ""]
    for f in files[1:]:
        diff = list(difflib.unified_diff(base, f.read_text(encoding="utf-8").splitlines(), files[0].name, f.name,
                                         lineterm=""))
        report.append(f"diff {files[0].name} vs {f.name}: " + ("(empty)" if not diff else ""))
        if diff:
            report += ["```diff", *diff[:200], "```"]
    (rdir / "repro_check.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report))


def cmd_ask(args):
    res = run_pipeline(Path(args.data_dir), use_llm=not args.no_llm, use_cache=True)
    print(qa.answer(args.question, res["weekly"], res["verdicts"], res["facts"], res["client"]))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--out", default=str(ROOT / "output" / "output.csv"))
    r.add_argument("--csv-only", action="store_true")
    r.add_argument("--no-cache", action="store_true")
    r.add_argument("--no-llm", action="store_true")
    r.add_argument("--data-dir", default=str(ROOT / "data"))
    rp = sub.add_parser("repro")
    rp.add_argument("--runs", type=int, default=3)
    rp.add_argument("--no-llm", action="store_true")
    rp.add_argument("--data-dir", default=str(ROOT / "data"))
    a = sub.add_parser("ask")
    a.add_argument("question")
    a.add_argument("--no-llm", action="store_true")
    a.add_argument("--data-dir", default=str(ROOT / "data"))
    args = p.parse_args()
    {"run": cmd_run, "repro": cmd_repro, "ask": cmd_ask}[args.cmd](args)


if __name__ == "__main__":
    main()
