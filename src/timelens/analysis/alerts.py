"""Run-time alerts: latest safe start times from the design-time network, replay on past cases."""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

from timelens.errors import LogMappingError
from timelens.mining.log import mapped_tasks
from timelens.model.process import ProcessGraph
from timelens.stnu.build import timepoint
from timelens.stnu.network import STNU, weighted
from timelens.stnu.stn import allmax_edges, distance_matrix, floyd_warshall, negative_diagonal

STATIC_MARGIN_MIN = 3 * 1440  # baseline: alert if no decision by deadline - 3 days


@dataclass(frozen=True)
class CaseTrace:
    case_id: str
    times: dict[str, int]  # observed timepoint -> integer minutes after case start (Z = 0)
    duration: int  # case_end - case_start, minutes


@dataclass(frozen=True)
class Alert:
    time: float  # minutes after case start
    timepoint: str  # the task start at risk ("" if the observations are already inconsistent)
    message: str


def latest_safe_times(stnu: STNU, observed: Mapping[str, int]) -> npt.NDArray[np.float64] | None:
    """Return LST(X) = D[Z, X] for every timepoint, or None if the case is already at risk.

    AllMax projection; observed timepoints pinned to their times; a contingent link whose end
    is observed uses the observed time instead of its maximum. O(n^3).
    """
    idx = stnu.index
    links = [lk for lk in stnu.links if lk.contingent not in observed]
    triples: list[tuple[int, int, float]] = list(weighted(stnu, stnu.edges + allmax_edges(links)))
    for tp, t in observed.items():
        triples += [(0, idx[tp], t), (idx[tp], 0, -t)]
    D = floyd_warshall(distance_matrix(len(stnu.timepoints), triples))
    return None if negative_diagonal(D) is not None else D[0].copy()


def first_alert(stnu: STNU, trace: CaseTrace, names: Mapping[str, str]) -> Alert | None:
    """Replay one case and return its first alert, or None.

    After event k at t_k (next event at t_k+1), a pending task start X with LST(X) < t_k+1
    raises an alert at max(LST(X), t_k). O(events * n^3).
    """
    events = sorted(trace.times.items(), key=lambda kv: kv[1])
    idx = stnu.index
    watch = [tp for tp in stnu.timepoints if tp.endswith(":s")]
    for k in range(len(events) - 1):
        t_k, t_next = events[k][1], events[k + 1][1]
        observed = dict(events[: k + 1])
        lst = latest_safe_times(stnu, observed)
        if lst is None:
            return Alert(t_k, "", "Observed times already break a time rule: deadline at risk.")
        late = [(max(float(lst[idx[x]]), t_k), x) for x in watch
                if x not in observed and lst[idx[x]] < t_next]
        if late:
            at, x = min(late)
            return Alert(at, x, f"Start {names.get(x, x)} by minute {at:.0f}, "
                                f"else the deadline is at risk.")
    return None


def scores(alert_times: list[float | None], late: list[bool], deadline: int) -> dict[str, Any]:
    """Return precision, recall, F1 and median lead time (deadline - alert time) of true hits."""
    tp = sum(a is not None and lt for a, lt in zip(alert_times, late, strict=True))
    fp = sum(a is not None and not lt for a, lt in zip(alert_times, late, strict=True))
    fn = sum(a is None and lt for a, lt in zip(alert_times, late, strict=True))
    precision = tp / (tp + fp) if tp + fp else math.nan
    recall = tp / (tp + fn) if tp + fn else math.nan
    f1 = (2 * precision * recall / (precision + recall)
          if tp and precision + recall else math.nan)
    leads = [deadline - a for a, lt in zip(alert_times, late, strict=True)
             if a is not None and lt]
    return {"cases": len(late), "late": sum(late), "alerts": tp + fp, "tp": tp, "fp": fp,
            "fn": fn, "precision": precision, "recall": recall, "f1": f1,
            "median_lead_min": statistics.median(leads) if leads else math.nan}


def static_alert(trace: CaseTrace, deadline: int) -> float | None:
    """Baseline: alert at deadline - 3 days if the case has no decision by then."""
    at = max(0, deadline - STATIC_MARGIN_MIN)
    return float(at) if trace.duration > at else None


def evaluate(stnu: STNU, traces: list[CaseTrace], deadline: int,
             names: Mapping[str, str]) -> dict[str, Any]:
    """Return TimeLens and static-baseline scores on `traces`. A case is late if
    case_end - case_start > deadline."""
    late = [t.duration > deadline for t in traces]
    ours = [first_alert(stnu, t, names) for t in traces]
    return {"timelens": scores([a.time if a else None for a in ours], late, deadline),
            "static_baseline": scores([static_alert(t, deadline) for t in traces], late,
                                      deadline)}


def fits_model_order(process: ProcessGraph, times: Mapping[str, int]) -> bool:
    """Return True if no observed time runs against the flow order, through gateways too.

    `times` maps timepoints (Z, <task>:s, <task>:e, event ids) to minutes. O(nodes + flows).
    """
    floor: dict[str, int] = {}  # node -> latest observed time up to its end
    for n in process.topo:
        t = max((floor[process.flows[f].src] for f in process.in_flows[n]), default=0)
        for tp in (timepoint(process, n, "start"), timepoint(process, n, "end")):
            if tp in times:
                if times[tp] < t:
                    return False
                t = times[tp]
        floor[n] = t
    return True


def traces_from_times(times: pd.DataFrame, process: ProcessGraph,
                      tasks: list[str]) -> tuple[list[CaseTrace], int, int]:
    """Return (traces, cases without a case start or end, cases that break the model order).

    Columns: case_start -> Z, case_end -> end event, <task>:start/:end -> <task>:s/:e. A trace
    holds the task events that are present; the replay treats a missing one as pending.
    Raises LogMappingError if a task has no columns (no log mapping).
    """
    missing = sorted(set(tasks) - mapped_tasks(times))
    if missing:
        raise LogMappingError(f"model tasks without a log mapping: {', '.join(missing)}")
    cols = {"case_start": "Z", "case_end": process.end}
    for t in tasks:
        cols[f"{t}:start"], cols[f"{t}:end"] = f"{t}:s", f"{t}:e"
    full = times.dropna(subset=["case_start", "case_end"])
    start = full["case_start"]
    rel = full[list(cols)].apply(lambda c: (c - start).dt.total_seconds() // 60).to_numpy()
    traces, misfits = [], 0
    for case, row in zip(full.index, rel, strict=True):
        obs = {tp: int(v) for tp, v in zip(cols.values(), row, strict=True) if not math.isnan(v)}
        if fits_model_order(process, obs):
            traces.append(CaseTrace(str(case), obs, obs[process.end]))
        else:
            misfits += 1
    return traces, len(times) - len(full), misfits
