"""Study C: Research Delta accuracy, benchmarked against annotated episodes (plan task 31).

A **benchmark** (YAML, :class:`Benchmark`) lists episodes: a radar field, a topic
(its key terms), the event type an expert expects (or ``expected: false`` for a
control where nothing should be found), the date an expert would place the event,
and where that date comes from.

**Time-forward replay.** For each episode, weekly counts of the field and the
topic are counted (task 25's count requests), and the pipeline (tasks 25-28: the
as-of rebuild, the trend model, the artifact checks) is run at every week ``t``
using only observations made by ``t``. Counts recorded today are final, so each
week's count is treated as observed the day after the week ended; the indexing-lag
check (task 27) then holds back the last weeks as it would have, so detection
latency includes that delay. (Indexing lag itself can't be replayed from today's
counts; this is the replay's main limitation.)

**Metrics.**

- **precision and recall** over episodes: a true positive is an expected event
  detected within ``TOLERANCE`` of the expert date; a detection outside it, or in a
  control, is a false positive;
- **detection latency**: days from the expert date to the first flag;
- **stability**: whether each episode's outcome holds when the topic is defined
  with one key term fewer (a refit can change a topic's terms);
- **explanation faithfulness**: whether each detection's evidence recomputes
  exactly from the stored observations and supports the claim (the recent weeks
  exceed the baseline, and the interval lies above 1).

A leakage test (in the tests) changes every observation after ``t`` and checks the
decision at ``t`` doesn't move.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional, Sequence, Tuple, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field

from carousel.connectors.base import SearchQuery
from carousel.intelligence.observations import FIELD

DELTA_EVAL_VERSION = "delta-eval-1"
TOLERANCE_BEFORE = dt.timedelta(weeks=8)
TOLERANCE_AFTER = dt.timedelta(weeks=26)
TOPIC = "episode_topic"


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Episode(_M):
    id: str
    description: str
    field_queries: List[str] = Field(min_length=1)
    field_exclude: List[str] = []
    topic_label: str
    key_terms: List[str] = Field(min_length=1)
    event_type: Literal["topic_acceleration"] = "topic_acceleration"
    expected: bool
    expert_date: Optional[dt.date] = Field(None, description="When an expert would place the event (expected episodes)")
    replay_start: dt.date
    replay_end: dt.date
    source: str = "pubmed"
    reference: str = Field(description="Where the expert date comes from")
    annotation: str = Field("draft", description="draft | reviewed (by whom)")


class Benchmark(_M):
    benchmark: str
    version: int
    status: str
    note: str = ""
    episodes: List[Episode]


def load_benchmark(path: Union[str, Path]) -> Benchmark:
    return Benchmark.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


# -------------------------
# Counting and replay
# -------------------------

def weeks(start: dt.date, end: dt.date) -> List[Tuple[dt.date, dt.date]]:
    """ISO weeks (Monday-Sunday) from the week containing ``start`` to the one ending by ``end``."""
    monday = start - dt.timedelta(days=start.isoweekday() - 1)
    out = []
    while monday + dt.timedelta(days=6) <= end:
        out.append((monday, monday + dt.timedelta(days=6)))
        monday += dt.timedelta(days=7)
    return out


def count_episode(ep: Episode, counter: Any, key_terms: Optional[Sequence[str]] = None,
                  field: bool = True) -> List[Dict[str, Any]]:
    """Observation rows (task 25's shape) for the field and the topic, each week observed the day after it ended."""
    terms = list(key_terms if key_terms is not None else ep.key_terms)
    rows = []
    for start, end in weeks(ep.replay_start, ep.replay_end):
        observed = dt.datetime.combine(end + dt.timedelta(days=1), dt.time(6), dt.timezone.utc)
        for topic, require in (((FIELD, []),) if field else ()) + ((TOPIC, terms),):
            q = SearchQuery(terms=ep.field_queries, exclude=ep.field_exclude, require=require, start=start, end=end,
                            max_results=1)
            res = counter.count(q)
            rows.append({"radar_id": ep.id, "classifier_version": "benchmark", "topic_id": topic, "source": ep.source,
                         "period_start": start.isoformat(), "period_end": end.isoformat(), "count": res.count,
                         "status": "ok", "query": res.native_query, "cost": res.cost, "error": None,
                         "observed_at": observed.strftime("%Y-%m-%dT%H:%M:%S.%fZ")})
    return rows


def replay(ep: Episode, rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Run the checked trend model at every Monday of the replay, each time on what was observed by then."""
    from carousel.intelligence.checks import checked_trends

    evaluations = []
    first = None
    t = ep.replay_start + dt.timedelta(days=(7 - ep.replay_start.isoweekday() + 1) % 7 or 7)  # the first Monday after
    while t <= ep.replay_end + dt.timedelta(days=28):
        as_of = dt.datetime.combine(t, dt.time(12), dt.timezone.utc)
        rep = checked_trends(rows, [TOPIC], ep.source, as_of, classifier_version="benchmark")
        topic = rep["topics"][0]
        evaluations.append({"as_of": t.isoformat(), "evaluated_through": rep["evaluated_through"],
                            "status": topic["status"], "flagged": topic["flagged"], "rate_ratio": topic.get("rate_ratio"),
                            "q_value": topic.get("q_value"), "failed": topic["failed"]})
        if topic["flagged"] and first is None:
            first = {"as_of": t.isoformat(), "report": rep, "topic": topic}
        t += dt.timedelta(days=7)
    return {"evaluations": evaluations, "first_flag": first}


def _outcome(ep: Episode, first: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    detected = first is not None
    date = dt.date.fromisoformat(first["as_of"]) if detected else None
    latency = (date - ep.expert_date).days if detected and ep.expert_date else None
    in_window = bool(detected and ep.expert_date and ep.expert_date - TOLERANCE_BEFORE <= date <= ep.expert_date + TOLERANCE_AFTER)
    if ep.expected:
        label = "true_positive" if in_window else ("false_positive" if detected else "false_negative")
    else:
        label = "false_positive" if detected else "true_negative"
    return {"detected": detected, "detection_date": date.isoformat() if date else None, "latency_days": latency,
            "label": label}


def faithful(ep: Episode, rows: Sequence[Mapping[str, Any]], first: Mapping[str, Any]) -> Dict[str, Any]:
    """Does the first flag's evidence recompute from the stored rows, and does it support the claim?"""
    from carousel.intelligence.checks import checked_trends

    as_of = dt.datetime.combine(dt.date.fromisoformat(first["as_of"]), dt.time(12), dt.timezone.utc)
    again = checked_trends(rows, [TOPIC], ep.source, as_of, classifier_version="benchmark")["topics"][0]
    t = first["topic"]
    reproduces = all(again[k] == t[k] for k in ("rate_ratio", "ci_low", "ci_high", "p_value", "recent_count"))
    supports = bool(t["recent_count"] > (t["expected_recent"] or 0) and (t["ci_low"] or 0) > 1)
    return {"reproduces": reproduces, "supports_claim": supports, "faithful": reproduces and supports}


def evaluate_benchmark(bench: Benchmark, counter_for: Callable[[Episode], Any]) -> Dict[str, Any]:
    """Run every episode; ``counter_for(episode)`` gives a connector with ``count()`` for its source."""
    episodes = []
    for ep in bench.episodes:
        counter = counter_for(ep)
        rows = count_episode(ep, counter)
        run = replay(ep, rows)
        out = _outcome(ep, run["first_flag"])
        # Stability: the same episode with the topic's last key term dropped (as a refit could change it).
        if len(ep.key_terms) > 1:
            field_rows = [r for r in rows if r["topic_id"] == FIELD]
            alt = replay(ep, field_rows + count_episode(ep, counter, ep.key_terms[:-1], field=False))
            stable = _outcome(ep, alt["first_flag"])["label"] == out["label"]
        else:
            stable = None
        faith = faithful(ep, rows, run["first_flag"]) if run["first_flag"] else None
        weekly = [r for r in rows if r["topic_id"] == TOPIC]
        episodes.append({"id": ep.id, "description": ep.description, "expected": ep.expected,
                         "expert_date": ep.expert_date.isoformat() if ep.expert_date else None, **out,
                         "stable_without_last_term": stable, "faithfulness": faith,
                         "first_flag_evidence": {k: run["first_flag"]["topic"].get(k) for k in (
                             "rate_ratio", "ci_low", "ci_high", "q_value", "recent_count", "expected_recent")}
                         if run["first_flag"] else None,
                         "weeks": len(weekly), "topic_papers": sum(r["count"] for r in weekly),
                         "reference": ep.reference, "annotation": ep.annotation, "evaluations": run["evaluations"]})
    labels = [e["label"] for e in episodes]
    tp, fp, fn = labels.count("true_positive"), labels.count("false_positive"), labels.count("false_negative")
    lat = [e["latency_days"] for e in episodes if e["label"] == "true_positive"]
    faith = [e["faithfulness"]["faithful"] for e in episodes if e["faithfulness"]]
    stab = [e["stable_without_last_term"] for e in episodes if e["stable_without_last_term"] is not None]
    return {
        "eval_version": DELTA_EVAL_VERSION, "benchmark": bench.benchmark, "benchmark_version": bench.version,
        "status": bench.status,
        "metrics": {"episodes": len(episodes), "true_positives": tp, "false_positives": fp, "false_negatives": fn,
                    "true_negatives": labels.count("true_negative"),
                    "precision": tp / (tp + fp) if tp + fp else None, "recall": tp / (tp + fn) if tp + fn else None,
                    "median_latency_days": sorted(lat)[len(lat) // 2] if lat else None,
                    "stability": sum(stab) / len(stab) if stab else None,
                    "faithfulness": sum(faith) / len(faith) if faith else None},
        "tolerance_weeks": {"before": TOLERANCE_BEFORE.days // 7, "after": TOLERANCE_AFTER.days // 7},
        "episodes": episodes,
    }


def report_markdown(report: Mapping[str, Any]) -> str:
    m = report["metrics"]

    def f(x: Any, pct: bool = False) -> str:
        return "—" if x is None else (f"{100 * x:.0f}%" if pct else str(x))

    lines = [f"# Study C: Research Delta accuracy ({report['benchmark']} v{report['benchmark_version']})", "",
             f"Benchmark status: **{report['status']}**. Evaluator {report['eval_version']}.", "",
             "| Metric | Value |", "| --- | --- |",
             f"| Episodes | {m['episodes']} |", f"| Precision | {f(m['precision'], True)} |", f"| Recall | {f(m['recall'], True)} |",
             f"| Median detection latency (days after the expert date) | {f(m['median_latency_days'])} |",
             f"| Stable with one key term fewer | {f(m['stability'], True)} |",
             f"| Faithful explanations | {f(m['faithfulness'], True)} |", "",
             f"A detection counts if it falls between {report['tolerance_weeks']['before']} weeks before and "
             f"{report['tolerance_weeks']['after']} weeks after the expert date.", "",
             "| Episode | Expected | Expert date | Detected | Latency (days) | Outcome | Stable | Faithful |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for e in report["episodes"]:
        fa = e["faithfulness"]
        lines.append(f"| {e['id']} | {'yes' if e['expected'] else 'no (control)'} | {e['expert_date'] or '—'} | "
                     f"{e['detection_date'] or 'no'} | {f(e['latency_days'])} | {e['label'].replace('_', ' ')} | "
                     f"{f(e['stable_without_last_term'])} | {'—' if fa is None else ('yes' if fa['faithful'] else 'no')} |")
    lines += ["", "Each episode's reference and annotation status are in the JSON report.", ""]
    return "\n".join(lines)


def write_report(report: Mapping[str, Any], out: Union[str, Path]) -> List[Path]:
    out = Path(out)
    md, js = out.with_suffix(".md"), out.with_suffix(".json")
    md.write_text(report_markdown(report), encoding="utf-8")
    js.write_text(json.dumps(report, indent=1, default=str) + "\n", encoding="utf-8")
    return [md, js]


__all__ = ["Benchmark", "DELTA_EVAL_VERSION", "Episode", "count_episode", "evaluate_benchmark", "faithful", "load_benchmark",
           "replay", "report_markdown", "weeks", "write_report"]
