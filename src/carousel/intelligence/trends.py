"""The trend model (plan task 26, spec §6): is a topic growing faster than its field, beyond chance?

For one topic ``c`` and one source, over weekly observations (task 25) as
known at a time ``t``::

    n_{c,w} ~ NegBin(mu_w, alpha)      log mu_w = log N_w + f_c(w) + delta * recent_w

- ``N_w`` is the radar field's count that week (the exposure), so growth of the
  whole field is not a topic trend;
- ``f_c(w)`` is the topic's baseline share: a constant level ``b0`` while the
  baseline is short, plus a linear drift ``b1 * w`` once there are at least
  ``LINEAR_MIN_BASELINE`` baseline weeks. (With a few weeks, a fitted slope is
  so uncertain that extrapolating it over the recent weeks dominates the
  test; simulations showed flat topics read as rate ratio 0.6. Growth beyond
  the baseline level is then "topic growth"; with a long baseline it is growth
  beyond the existing drift, "acceleration".)
- ``recent_w`` marks the last ``RECENT_WEEKS`` weeks, and ``delta`` is their
  excess over the baseline: ``exp(delta)`` is the rate ratio, with a Wald
  confidence interval and a one-sided p-value (growth only).

A topic is **flagged** only when all of these hold:

- the effect is large: rate ratio at least ``MIN_RATE_RATIO``;
- it is statistically supported after **Benjamini-Hochberg** correction across
  the radar's topics (q at most ``FDR``);
- there is enough data: at least ``MIN_RECENT_COUNT`` papers in the recent
  weeks, and ``MIN_BASELINE_WEEKS`` weeks before them;
- it **persists**: the same criteria also hold when the evaluation ends one
  week earlier.

A topic's smoothed share ``s = (n + a) / (N + a K)`` (``SMOOTHING`` = a,
``K`` = number of topics) is reported for display. If the negative-binomial
fit fails (too few counts to estimate dispersion), a Poisson fit is used and
the result says so. Every result carries ``TREND_MODEL_VERSION``.

Needs ``pip install "carousel-research[trends]"`` (numpy, statsmodels). The
engine works without it; trends then report the model as unavailable.
"""

from __future__ import annotations

import dataclasses
import math
import warnings
from typing import Any, Dict, List, Mapping, Optional, Sequence

TREND_MODEL_VERSION = "trend-nb-1"
RECENT_WEEKS = 4
MIN_BASELINE_WEEKS = 8
MIN_RATE_RATIO = 1.5
FDR = 0.10
MIN_RECENT_COUNT = 10
SMOOTHING = 0.5
LINEAR_MIN_BASELINE = 26


class TrendsUnavailable(RuntimeError):
    """numpy/statsmodels are not installed (the [trends] extra)."""


def _libs():
    try:
        import numpy as np
        from scipy.stats import t as student_t
        from statsmodels.discrete.discrete_model import NegativeBinomial, Poisson
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise TrendsUnavailable('the trend model needs: pip install "carousel-research[trends]"') from exc
    return np, student_t, NegativeBinomial, Poisson


@dataclasses.dataclass
class Fit:
    status: str                      # ok | insufficient_history | no_counts | failed
    method: Optional[str] = None     # negbin | poisson
    rate_ratio: Optional[float] = None
    ci_low: Optional[float] = None
    ci_high: Optional[float] = None
    p_value: Optional[float] = None  # one-sided: the recent weeks exceed the baseline
    recent_count: int = 0
    expected_recent: Optional[float] = None
    dispersion: Optional[float] = None
    weeks: int = 0
    note: Optional[str] = None
    baseline: Optional[str] = None   # level | linear (f_c)
    # The fitted baseline share per week (exp f_c(w)) with a 95% band from the baseline intercept's uncertainty;
    # what the topic's share would be without the recent excess (task 29's evidence pages, task 30's chart).
    baseline_share: Optional[List[Dict[str, Any]]] = None


def fit_series(series: Sequence[Mapping[str, Any]], recent: int = RECENT_WEEKS) -> Fit:
    """Fit one topic's weekly ``{count, denominator}`` series (oldest first); weeks with no field count are skipped."""
    np, student_t, NegativeBinomial, Poisson = _libs()
    rows = [r for r in series if r.get("denominator")]
    weeks = len(rows)
    if weeks < MIN_BASELINE_WEEKS + recent:
        return Fit(status="insufficient_history", weeks=weeks,
                   note=f"{weeks} weeks with counts; the model needs {MIN_BASELINE_WEEKS} baseline + {recent} recent")
    n = np.array([r["count"] for r in rows], dtype=float)
    N = np.array([r["denominator"] for r in rows], dtype=float)
    recent_n = int(n[-recent:].sum())
    if n.sum() == 0:
        return Fit(status="no_counts", weeks=weeks, recent_count=0, note="the topic has no papers in these weeks")
    w = (np.arange(weeks) - (weeks - 1) / 2) / weeks  # centred, scaled week index
    is_recent = (np.arange(weeks) >= weeks - recent).astype(float)
    linear = weeks - recent >= LINEAR_MIN_BASELINE
    X = np.column_stack([np.ones(weeks), w, is_recent] if linear else [np.ones(weeks), is_recent])
    method, res, note = None, None, None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            res = NegativeBinomial(n, X, loglike_method="nb2", exposure=N).fit(disp=0, maxiter=300)
            alpha = float(res.params[-1])
            if not (np.all(np.isfinite(res.params)) and np.all(np.isfinite(res.bse))) or alpha > 1e4:
                raise ValueError("did not converge")
            method = "negbin"
        except Exception as exc:  # few counts: dispersion can't be estimated
            note = f"negative binomial fit failed ({type(exc).__name__}); Poisson used"
            try:
                res = Poisson(n, X, exposure=N).fit(disp=0, maxiter=300)
                method, alpha = "poisson", 0.0
            except Exception as exc2:
                return Fit(status="failed", weeks=weeks, recent_count=recent_n,
                           note=f"no model could be fitted ({type(exc2).__name__})")
    if linear:
        b0, b1, delta = (float(x) for x in res.params[:3])
        se = float(res.bse[2])
    else:
        b0, delta = (float(x) for x in res.params[:2])
        b1, se = 0.0, float(res.bse[1])
    se_b0 = float(res.bse[0])
    if not math.isfinite(se) or se <= 0:
        return Fit(status="failed", weeks=weeks, recent_count=recent_n, method=method, note="no standard error for the excess")
    expected = float((N[-recent:] * np.exp(b0 + b1 * w[-recent:])).sum())
    # A t reference with the residual degrees of freedom, not the normal: with ~16 weeks and an estimated
    # dispersion the Wald z-test was anti-conservative in simulation (too many flags under the null).
    df = max(1, weeks - X.shape[1] - (1 if method == "negbin" else 0))
    crit = float(student_t.ppf(0.975, df))
    base = [{"week": r["week"], "share": math.exp(b0 + b1 * float(wi)),
             "low": math.exp(b0 - crit * se_b0 + b1 * float(wi)), "high": math.exp(b0 + crit * se_b0 + b1 * float(wi))}
            for r, wi in zip(rows, w)]
    return Fit(status="ok", baseline_share=base, method=method, rate_ratio=math.exp(delta), ci_low=math.exp(delta - crit * se),
               ci_high=math.exp(delta + crit * se), p_value=float(student_t.sf(delta / se, df)), recent_count=recent_n,
               expected_recent=expected, dispersion=alpha, weeks=weeks, note=note,
               baseline="linear" if linear else "level")


def benjamini_hochberg(p: Mapping[str, float]) -> Dict[str, float]:
    """BH-adjusted q-values (monotone, capped at 1)."""
    items = sorted(p.items(), key=lambda kv: (kv[1], kv[0]))
    m = len(items)
    q: Dict[str, float] = {}
    running = 1.0
    for rank in range(m, 0, -1):
        key, pv = items[rank - 1]
        running = min(running, pv * m / rank)
        q[key] = running
    return q


def _evaluate(series_by_topic: Mapping[str, Sequence[Mapping[str, Any]]], recent: int) -> Dict[str, Dict[str, Any]]:
    fits = {tid: fit_series(s, recent) for tid, s in series_by_topic.items()}
    q = benjamini_hochberg({t: f.p_value for t, f in fits.items() if f.status == "ok" and f.p_value is not None})
    out = {}
    for tid, f in fits.items():
        failed = []
        if f.status != "ok":
            failed.append(f.status)
        else:
            if (f.rate_ratio or 0) < MIN_RATE_RATIO:
                failed.append("effect_size")
            if q.get(tid, 1.0) > FDR:
                failed.append("significance")
            if f.recent_count < MIN_RECENT_COUNT:
                failed.append("min_count")
        out[tid] = {**dataclasses.asdict(f), "q_value": q.get(tid), "passes": not failed, "failed": failed}
    return out


def smoothed_shares(series_by_topic: Mapping[str, Sequence[Mapping[str, Any]]]) -> Dict[str, List[Dict[str, Any]]]:
    k = max(1, len(series_by_topic))
    return {tid: [{"week": r["week"], "share": (r["count"] + SMOOTHING) / (r["denominator"] + SMOOTHING * k)}
                  for r in s if r.get("denominator") is not None] for tid, s in series_by_topic.items()}


def evaluate(series_by_topic: Mapping[str, Sequence[Mapping[str, Any]]], recent: int = RECENT_WEEKS) -> Dict[str, Any]:
    """Every topic of one radar and source: the fit now, the fit a week earlier, and whether it is flagged."""
    now = _evaluate(series_by_topic, recent)
    before = _evaluate({t: list(s)[:-1] for t, s in series_by_topic.items()}, recent)
    smooth = smoothed_shares(series_by_topic)
    topics = []
    for tid in sorted(series_by_topic):
        cur, prev = now[tid], before[tid]
        failed = list(cur["failed"]) + ([] if prev["passes"] or not cur["passes"] else ["persistence"])
        topics.append({"topic_id": tid, **{k: v for k, v in cur.items() if k not in ("passes", "failed")},
                       "flagged": cur["passes"] and prev["passes"], "failed": failed,
                       "previous_week": {k: prev[k] for k in ("status", "rate_ratio", "q_value", "recent_count", "passes")},
                       "smoothed_share": smooth[tid]})
    return {"trend_model_version": TREND_MODEL_VERSION,
            "criteria": {"recent_weeks": recent, "linear_min_baseline": LINEAR_MIN_BASELINE, "min_baseline_weeks": MIN_BASELINE_WEEKS, "min_rate_ratio": MIN_RATE_RATIO,
                         "fdr": FDR, "min_recent_count": MIN_RECENT_COUNT, "persistence_weeks": 2, "smoothing": SMOOTHING},
            "topics": topics}


def trends_from_observations(rows: Sequence[Mapping[str, Any]], topic_ids: Sequence[str], source: str,
                             as_of: Any, recent: int = RECENT_WEEKS) -> Dict[str, Any]:
    """Evaluate a radar's topics for one source from stored observations, as known at ``as_of`` (task 25)."""
    from carousel.intelligence.observations import counts_as_of, series

    known = counts_as_of(rows, as_of)
    report = evaluate({tid: series(known, source, tid) for tid in topic_ids}, recent)
    return {"source": source, "as_of": as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of), **report}


__all__ = ["FDR", "MIN_BASELINE_WEEKS", "MIN_RATE_RATIO", "MIN_RECENT_COUNT", "RECENT_WEEKS", "TREND_MODEL_VERSION",
           "TrendsUnavailable", "benjamini_hochberg", "evaluate", "fit_series", "smoothed_shares", "trends_from_observations"]
