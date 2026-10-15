"""Stretch goal: plain-English questions about the findings, answered only from computed evidence.

The evidence (weekly numbers, verdicts, relevant notes) is selected by code. With an LLM key the
model writes the answer from that evidence and every note id it cites is checked; without a key
the evidence itself is returned as the answer.
"""
from __future__ import annotations

import re

import pandas as pd

from notes import MONTH_NUM

QA_SYSTEM = ("You answer questions about weekly freight-cost findings. Use ONLY the evidence provided. "
             "Cite note ids (like N001) whenever you rely on a note. If the evidence does not answer the "
             "question, say exactly that. Plain English, under 120 words.")
_MONTH_WORD = (r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
               r"sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b")
# "may" is also a verb ("may I know ..."): count it as the month only next to a number or a time word.
_MAY_AS_MONTH = re.compile(r"\b(?:in|during|of|since|until|till|by|from|through|early|mid|late)[\s-]+may\b"
                           r"|\bmay[\s,]+\d")


def _routes_in(question: str, routes: list[str]) -> list[str]:
    q = question.lower()
    full = [r for r in routes if all(p.lower() in q for p in r.split("-"))]
    if full:
        return full
    return [r for r in routes if any(re.search(rf"\b{re.escape(p.lower())}\b", q) for p in r.split("-"))]


def build_evidence(question: str, weekly: pd.DataFrame, verdicts: dict, facts) -> tuple[list[str], list[str]]:
    q = question.lower()
    routes = _routes_in(question, sorted(weekly["route"].unique()))
    months = [MONTH_NUM[m[:3]] for m in re.findall(_MONTH_WORD, q) if m != "may" or _MAY_AS_MONTH.search(q)]
    years = [int(y) for y in re.findall(r"\b(20\d\d)\b", q)]

    def in_period(day: pd.Series) -> pd.Series:   # month and year must hold for the SAME day
        ok = pd.Series(True, index=day.index)
        if months:
            ok &= day.dt.month.isin(months)
        if years:
            ok &= day.dt.year.isin(years)
        return ok

    w = weekly if not routes else weekly[weekly["route"].isin(routes)]
    if months or years:                         # a week counts if it starts or ends in the named period
        w = w[in_period(w["week_of"]) | in_period(w["week_of"] + pd.Timedelta(days=6))]
    else:
        w = w[w["anomaly"]]                     # no period named: talk about the flagged weeks

    lines = []
    for r in w.sort_values(["route", "week_of"]).head(60).itertuples(index=False):
        own = "n/a" if pd.isna(r.vs_own_pct) else f"{r.vs_own_pct:+.1f}%"
        peer = "n/a" if pd.isna(r.vs_similar_pct) else f"{r.vs_similar_pct:+.1f}%"
        line = (f"{r.route}, week of {r.week_of:%Y-%m-%d}: {r.cost_per_tonne_km:.2f} INR per tonne-km, "
                f"{own} vs own trailing avg, {peer} vs other {r.route_type} routes")
        v = verdicts.get((r.route, r.week_of))
        if v is not None:
            line += f" | FLAG CHECK: {'justified by ' + v.note_id if v.justified else 'unexplained'} | {v.reason}"
        lines.append(line)

    note_lines = []
    if len(w):
        lo, hi = w["week_of"].min() - pd.Timedelta(days=28), w["week_of"].max() + pd.Timedelta(days=34)
        for f in facts:
            start, end = f.window()
            in_time = start <= hi and (end is None or end >= lo)
            if in_time and (not routes or any(f.covers_route(r) for r in routes)):
                note_lines.append(f"{f.note_id} ({f.note_date}, {f.applies_to}): {f.text}")
    return lines, note_lines


def answer(question: str, weekly: pd.DataFrame, verdicts: dict, facts, llm=None) -> str:
    lines, note_lines = build_evidence(question, weekly, verdicts, facts)
    if not lines:
        return ("I could not find any weeks matching that question. Name a route (e.g. Chennai-Bangalore) "
                "and/or a month and year (e.g. March 2025).")
    evidence = "WEEKLY FINDINGS:\n" + "\n".join(lines) + "\n\nNOTES IN RANGE:\n" + ("\n".join(note_lines) or "(none)")

    if llm is not None and llm.enabled:
        try:
            text = llm.complete(QA_SYSTEM, f"Question: {question}\n\nEvidence:\n{evidence}", purpose="ask")
        except Exception as exc:                # network/API failure -> the evidence alone still answers it
            return f"LLM unavailable ({type(exc).__name__}) -- showing the evidence instead.\n\n" + evidence
        allowed = {n.split(" ")[0] for n in note_lines}
        bad = sorted(set(re.findall(r"\bN\d{3}\b", text)) - allowed)
        if bad:
            text += f"\n\n[check] The answer cited {', '.join(bad)}, which is not in the evidence -- ignore that part."
        return text + "\n\n--- evidence used ---\n" + evidence
    return "Offline mode (no LLM key): here is the evidence that answers your question.\n\n" + evidence
