"""Context notes: read them into checkable facts, retrieve the relevant ones, and decide strictly.

    note text --(rules; optionally a second opinion from an LLM, verified)--> NoteFacts
    anomaly   --(TF-IDF retrieval + route/date filter)--> candidate notes --(strict checks)--> verdict

A note only justifies a rise if ALL of these hold:
  1. it concerns a route in this dataset, and this route (or "All Routes");
  2. it says costs went UP (not "unaffected", "absorbed", "normal", "improved");
  3. its time window overlaps the flagged week;
  4. it is big enough: a stated % range must cover the rise, and an all-routes note cannot explain
     one route jumping above its own peers (the peers had the same note).
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, replace
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from core import parse_dates

ALL_ROUTES = "all routes"
MONTH_NUM = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
# Whole month names only: "decreased 5 to 7" or "Junction 4 to 6" must not read as dates.
_MON = (r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
        r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
RANGE_RE = re.compile(_MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?\s*(?:to|until|till|through|-|–)\s*(?:"
                      + _MON + r"\s+)?(\d{1,2})\b", re.I)
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*%|(\d+(?:\.\d+)?)\s*%")

OUT_OF_SCOPE = (r"not part of (?:this|the) dataset", r"not (?:included|covered) in (?:this|the) (?:data|dataset)")
NO_EFFECT = (r"not significantly affected", r"without (?:a|any) rate change", r"no (?:major |significant )?disruptions?",
             r"returned to normal", r"remained (?:stable|normal)", r"\bimproved\b", r"\babsorbed\b")
INCREASE = (r"surcharge", r"\brose\b", r"higher (?:trip |transport(?:ation)? )?costs?", r"pushing up",
            r"\bincreas(?:e|ed|es|ing)\b", r"\bhike[sd]?\b", r"longer detours")


@dataclass(frozen=True)
class NoteFacts:
    note_id: str
    note_date: str                  # YYYY-MM-DD
    applies_to: str
    text: str
    in_scope: bool                  # concerns a route that exists in the dataset
    effect: str                     # "increase" | "no_effect" | "unclear"
    evidence: str                   # clause of the note that decided scope/effect (verbatim)
    window_start: str
    window_end: Optional[str]       # None = open-ended ("starting this week")
    window_basis: str
    magnitude_low: Optional[float]
    magnitude_high: Optional[float]
    source: str = "rules"

    @property
    def is_all_routes(self) -> bool:
        return self.applies_to.lower() == ALL_ROUTES

    def covers_route(self, route: str) -> bool:
        return self.is_all_routes or self.applies_to.lower() == route.lower()

    def window(self) -> tuple[pd.Timestamp, Optional[pd.Timestamp]]:
        end = pd.Timestamp(self.window_end) if self.window_end else None
        return pd.Timestamp(self.window_start), end

    def window_label(self) -> str:
        start, end = self.window()
        return f"{start:%d %b %Y} onwards" if end is None else f"{start:%d %b %Y} to {end:%d %b %Y}"


# --------------------------------------------------------------------------- reading notes

def load_notes(path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str).fillna("")
    df.columns = [c.strip() for c in df.columns]
    date_col = "date" if "date" in df.columns else "note_date"
    text_col = "note" if "note" in df.columns else "text"
    df = df.rename(columns={date_col: "date", text_col: "note"})
    df["date"] = parse_dates(df["date"])
    for col in ("note_id", "applies_to", "note"):
        df[col] = df[col].str.strip()
    return df


def _first_match(patterns, text) -> Optional[re.Match]:
    hits = [m for p in patterns if (m := re.search(p, text, re.I))]
    return min(hits, key=lambda m: m.start()) if hits else None


def _clause(text: str, match: re.Match) -> str:
    """The clause (between . or ;) that contains the match, quoted verbatim."""
    start = max(text.rfind(".", 0, match.start()), text.rfind(";", 0, match.start())) + 1
    ends = [i for i in (text.find(".", match.end()), text.find(";", match.end())) if i != -1]
    return text[start:min(ends) if ends else len(text)].strip(" .;")


def _window(text: str, note_date: pd.Timestamp) -> tuple[pd.Timestamp, Optional[pd.Timestamp], str]:
    week_start = note_date - pd.Timedelta(days=note_date.weekday())
    m = RANGE_RE.search(text)
    if m:
        m1, d1, m2, d2 = m.groups()
        try:
            start = pd.Timestamp(year=note_date.year, month=MONTH_NUM[m1.lower()[:3]], day=int(d1))
            end = pd.Timestamp(year=note_date.year, month=MONTH_NUM[(m2 or m1).lower()[:3]], day=int(d2))
        except ValueError:                                     # impossible date such as "Feb 30": use the rules below
            m = None
    if m:
        if start > note_date + pd.Timedelta(days=60):          # e.g. a January note about "Dec 20 to Dec 28"
            start, end = start - pd.DateOffset(years=1), end - pd.DateOffset(years=1)
        if end < start:                                        # e.g. "Dec 28 to Jan 3"
            end = end + pd.DateOffset(years=1)
        return start, end, f"explicit dates in the note ('{m.group(0)}'), year from the note date"
    if re.search(r"\bthis quarter\b", text, re.I):
        q = note_date.to_period("Q")
        return q.start_time.normalize(), q.end_time.normalize(), "quarter of the note date ('this quarter')"
    if re.search(r"\bstarting\b|\bfrom this week\b|\bonwards\b", text, re.I):
        return week_start, None, "open-ended from the week of the note ('starting this week')"
    return week_start, week_start + pd.Timedelta(days=6), "week of the note date (no explicit dates)"


def facts_from_rules(row, dataset_routes: list[str]) -> NoteFacts:
    text, note_date, applies = row.note, row.date, row.applies_to
    known_route = applies.lower() == ALL_ROUTES or applies.lower() in {r.lower() for r in dataset_routes}

    oos = _first_match(OUT_OF_SCOPE, text)
    no_eff = _first_match(NO_EFFECT, text)
    inc = _first_match(INCREASE, text)
    if no_eff:                       # "rose but absorbed" -> no effect on rates: the cautious reading wins
        effect, evidence = "no_effect", _clause(text, no_eff)
    elif inc:
        effect, evidence = "increase", _clause(text, inc)
    else:
        effect, evidence = "unclear", text.split(".")[0].strip()
    if oos:
        evidence = _clause(text, oos)
    if not known_route and not oos:
        evidence = f"applies to '{applies}', which is not a route in this dataset"

    start, end, basis = _window(text, note_date)
    pct = PCT_RE.search(text)
    lo = hi = None
    if pct:
        lo, hi = (float(pct.group(1)), float(pct.group(2))) if pct.group(1) else (float(pct.group(3)),) * 2

    return NoteFacts(note_id=row.note_id, note_date=f"{note_date:%Y-%m-%d}", applies_to=applies, text=text,
                     in_scope=known_route and oos is None, effect=effect, evidence=evidence,
                     window_start=f"{start:%Y-%m-%d}", window_end=None if end is None else f"{end:%Y-%m-%d}",
                     window_basis=basis, magnitude_low=lo, magnitude_high=hi)


# ------------------------------------------------------------- optional LLM second opinion

NOTE_SYSTEM = ("You read short logistics notes and extract facts. Use ONLY what the note says; never guess. "
               "Reply with one JSON object and nothing else.")
NOTE_USER = """Routes in our dataset: {routes}
Note id: {note_id}
Note date: {note_date}
Applies to: {applies_to}
Note text: \"\"\"{text}\"\"\"

Return JSON with exactly these keys:
"in_scope": false only if the note itself says the affected routes are not in our dataset, else true,
"effect": "increase" if the note says freight/transport costs went up; "no_effect" if it says costs were unaffected, absorbed, unchanged, or conditions were normal/stable/improved; otherwise "unclear",
"evidence_quote": the exact words from the note that support "effect", copied verbatim,
"window_start": first date the effect applies (YYYY-MM-DD, use the note date's year),
"window_end": last date (YYYY-MM-DD), or null if the note says it continues with no end,
"magnitude_low_pct": lowest % change stated in the note, or null,
"magnitude_high_pct": highest % change stated in the note, or null"""


def note_prompt(row, dataset_routes: list[str]) -> str:
    return NOTE_USER.format(routes=", ".join(dataset_routes), note_id=row.note_id,
                            note_date=f"{row.date:%Y-%m-%d}", applies_to=row.applies_to, text=row.note)


def merge_llm_opinion(rules: NoteFacts, data: dict) -> tuple[NoteFacts, list[str]]:
    """The LLM may fill gaps the rules could not read, but it is never trusted blindly:
    its quote must exist verbatim in the note, numbers/dates must be literally present,
    and when it disagrees with the rules on whether costs rose, the cautious answer wins."""
    log: list[str] = []
    quote = str(data.get("evidence_quote") or "").strip()
    if not quote or quote.lower() not in rules.text.lower():
        return replace(rules, source="rules (LLM answer rejected: quote not found in note)"), \
            [f"{rules.note_id}: LLM quote not in note text -> ignored"]

    llm_in_scope = str(data.get("in_scope", True)).strip().lower() not in ("false", "0", "no")  # "false" is truthy
    in_scope = rules.in_scope and llm_in_scope
    llm_effect = data.get("effect") if data.get("effect") in ("increase", "no_effect", "unclear") else "unclear"
    if llm_effect != rules.effect:
        log.append(f"{rules.note_id}: effect rules={rules.effect} llm={llm_effect}")
    if rules.effect == "unclear":
        effect, evidence = llm_effect, quote                     # rules could not read it; LLM fills the gap
    elif "no_effect" in (rules.effect, llm_effect):
        effect, evidence = "no_effect", rules.evidence if rules.effect == "no_effect" else quote
    else:
        effect, evidence = rules.effect, rules.evidence

    start, end, basis = rules.window_start, rules.window_end, rules.window_basis
    if "explicit dates" not in basis:
        try:
            ls, le = data.get("window_start"), data.get("window_end")
            days_in_text = {int(d) for d in re.findall(r"\b(\d{1,2})\b", rules.text)}
            if ls and le and {pd.Timestamp(ls).day, pd.Timestamp(le).day} <= days_in_text:
                start, end, basis = f"{pd.Timestamp(ls):%Y-%m-%d}", f"{pd.Timestamp(le):%Y-%m-%d}", "LLM-read dates found in the note text"
        except (ValueError, TypeError):
            log.append(f"{rules.note_id}: unparseable LLM window ignored")

    lo, hi = rules.magnitude_low, rules.magnitude_high
    if lo is None:
        nums = [data.get("magnitude_low_pct"), data.get("magnitude_high_pct")]
        if all(isinstance(n, (int, float)) and f"{n:g}" in rules.text for n in nums):
            lo, hi = float(nums[0]), float(nums[1])

    return replace(rules, in_scope=in_scope, effect=effect, evidence=evidence, window_start=start,
                   window_end=end, window_basis=basis, magnitude_low=lo, magnitude_high=hi,
                   source="rules + LLM (verified)"), log


def build_facts(notes_df: pd.DataFrame, dataset_routes: list[str], llm=None) -> tuple[list[NoteFacts], list[str]]:
    facts, log = [], []
    for row in notes_df.itertuples(index=False):
        f = facts_from_rules(row, dataset_routes)
        if llm is not None and llm.enabled:
            try:
                raw = llm.complete(NOTE_SYSTEM, note_prompt(row, dataset_routes), purpose=f"read note {row.note_id}",
                                   json_mode=True)
                f, notes_log = merge_llm_opinion(f, json.loads(raw.strip().strip("`").removeprefix("json")))
                log += notes_log
            except Exception as exc:                            # network/JSON failure -> rules result stands
                log.append(f"{row.note_id}: LLM unavailable ({type(exc).__name__}) -> rules only")
        facts.append(f)
    return facts, log


# ------------------------------------------------------------------- retrieval + verdict

@dataclass
class Verdict:
    justified: bool
    note_id: str
    reason: str


class NoteMatcher:
    QUERY = "{route} route freight cost rise higher increase surcharge disruption flooding delays diesel toll"

    def __init__(self, facts: list[NoteFacts], cfg: dict):
        self.facts = facts
        self.cfg = cfg
        self.vectorizer = TfidfVectorizer(lowercase=True, stop_words="english",
                                          token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z]+\b")
        # An empty notes file has no vocabulary to fit; every anomaly then simply has no matching note.
        self.matrix = self.vectorizer.fit_transform([f"{f.applies_to} {f.text}" for f in facts]) if facts else None

    # -- retrieval: lexical relevance + same-route bonus + closeness in time
    def _scores(self, route: str, week: pd.Timestamp) -> np.ndarray:
        if not self.facts:
            return np.array([])
        q = self.vectorizer.transform([self.QUERY.format(route=route.replace("-", " "))])
        sim = cosine_similarity(q, self.matrix).ravel()
        out = []
        for f, s in zip(self.facts, sim):
            route_bonus = 1.0 if f.applies_to.lower() == route.lower() else 0.0
            days = abs((pd.Timestamp(f.note_date) - week).days)
            out.append(round(0.5 * s + 0.3 * route_bonus + 0.2 / (1 + days / 7), 6))
        return np.array(out)

    @staticmethod
    def _gap_days(f: NoteFacts, week: pd.Timestamp) -> int:
        """Days between the flagged week and the note's window (open-ended notes cover every week from their start)."""
        start, end = f.window()
        week_end = week + pd.Timedelta(days=6)
        if week_end < start:
            return (start - week_end).days
        if end is not None and week > end:
            return (week - end).days
        return 0

    def _rejection(self, f: NoteFacts, row) -> Optional[str]:
        """None if the note genuinely explains this rise, else a plain-English reason why not."""
        week, week_end = row.week_of, row.week_of + pd.Timedelta(days=6)
        start, end = f.window()
        if not f.in_scope:
            return f'concerns routes outside this dataset ("{f.evidence}")'
        if f.effect == "no_effect":
            return f'says "{f.evidence}" -- that is not a reason for a cost rise'
        if f.effect != "increase":
            return f'does not say costs went up ("{f.evidence}")'
        if week_end < start or (end is not None and week > end):
            return (f"describes higher costs on {f.applies_to} for {f.window_label()} only; "
                    f"the week of {week:%Y-%m-%d} is outside that window")
        if f.is_all_routes and row.peer_unusual:
            return (f"applies to all routes alike, so it cannot explain this route running "
                    f"{row.vs_similar_pct:+.1f}% above other {row.route_type} routes")
        if f.magnitude_high is not None:
            limit = f.magnitude_high + self.cfg["magnitude_tolerance_pp"]
            mag = f"{f.magnitude_low:g}-{f.magnitude_high:g}%" if f.magnitude_low != f.magnitude_high else f"{f.magnitude_high:g}%"
            if row.vs_own_pct > limit:
                return f"reports only a {mag} rise, too small to explain a {row.vs_own_pct:+.1f}% jump"
            # a small stated rise cannot explain a large gap over peers either (the gap was there before the note)
            if row.peer_unusual and row.vs_similar_pct > limit:
                return (f"reports only a {mag} rise, too small to explain running {row.vs_similar_pct:+.1f}% "
                        f"above other {row.route_type} routes")
        return None

    @staticmethod
    def why_flagged(row) -> str:
        parts = []
        if row.own_unusual:
            parts.append(f"{row.vs_own_pct:+.1f}% vs its own trailing average")
        if row.peer_unusual:
            parts.append(f"{row.vs_similar_pct:+.1f}% vs other {row.route_type} routes")
        return "Cost per tonne-km is " + " and ".join(parts) + "."

    def decide(self, row) -> Verdict:
        scores = self._scores(row.route, row.week_of)
        order = sorted(range(len(self.facts)), key=lambda i: (-scores[i], self.facts[i].note_id))
        covering = [i for i in order if self.facts[i].covers_route(row.route)]
        checks = {i: self._rejection(self.facts[i], row) for i in covering}

        valid = [i for i in covering if checks[i] is None]
        if valid:
            f = self.facts[valid[0]]
            return Verdict(True, f.note_id,
                           f'Matches note {f.note_id} dated {f.note_date}: "{f.text}" It applies to '
                           f"{f.applies_to} for {f.window_label()}, which covers this week, "
                           f"so the cost rise has a clear explanation.")

        nearby = [i for i in covering if self._gap_days(self.facts[i], row.week_of) <= self.cfg["candidate_window_days"]]
        why = self.why_flagged(row)
        if not nearby:
            return Verdict(False, "", f"{why} No matching note found for this route or date range -- "
                                      f"the rise looks unexplained and is worth a human review.")
        top = nearby[:2]
        checked = "; ".join(f"{self.facts[i].note_id} ({self.facts[i].note_date}) {checks[i]}" for i in top)
        label = "Closest notes checked" if len(top) > 1 else "Closest note checked"
        return Verdict(False, "", f"{why} {label}: {checked}. "
                                  f"No genuine justification found -- flagged for human review.")


def facts_to_json(facts: list[NoteFacts]) -> str:
    return json.dumps([asdict(f) for f in facts], indent=2)
