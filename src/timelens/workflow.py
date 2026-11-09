"""Workflow steps shared by the CLI and the UI. They return data and never print."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from timelens.analysis.alerts import evaluate, traces_from_times
from timelens.analysis.repair import Repair, repair
from timelens.analysis.verdict import check_process
from timelens.errors import LogMappingError, TimeLensError
from timelens.mining.calibrate import case_duration_stats, default_deadline_days
from timelens.mining.log import case_times, load_log, load_mapping, split_by_time
from timelens.model.process import ProcessGraph
from timelens.model.rules import Levers, Rules, case_deadline, load_rules
from timelens.report.text import check_report, header, repair_block
from timelens.stnu.build import build_stnu
from timelens.units import format_minutes


def load_template(path: str | Path) -> dict[str, Any]:
    """Return the rules template as JSON data after schema validation. Raises RulesError."""
    load_rules(path)
    data: dict[str, Any] = json.loads(Path(path).read_text())
    return data


def prepare_times(log: str | Path, mapping: str | Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (train, test) case time tables, split 70/30 by case start."""
    return split_by_time(case_times(load_log(log), load_mapping(mapping)))


@dataclass(frozen=True)
class Deadline:
    minutes: int
    reason: str  # written into the rules notes and every report header
    stats: dict[str, float]  # training case durations in minutes: n, median, p80, p90

    def stats_text(self) -> str:
        s = self.stats
        return (f"Case duration (training set, n={int(s['n']):,}): median "
                f"{format_minutes(round(s['median']))}, p80 {format_minutes(round(s['p80']))}, "
                f"p90 {format_minutes(round(s['p90']))}")


def choose_deadline(train: pd.DataFrame, days: int | None) -> Deadline:
    """Return the user's deadline, or the p90 training case duration rounded up to whole days."""
    stats = case_duration_stats(train)
    if not stats["n"]:
        raise LogMappingError("no training case has both a case start and a case end")
    if days is not None:
        return Deadline(days * 1440, f"Deadline {days} d, chosen by the user "
                                     "(assumption: the log has none).", stats)
    d = default_deadline_days(stats)
    return Deadline(d * 1440, f"Deadline {d} d = 90th percentile of training case durations "
                              f"({format_minutes(round(stats['p90']))}) rounded up to whole days; "
                              "no user choice given (assumption: the log has no official "
                              "deadline).", stats)


def replay_deadline(process: ProcessGraph, rules: Rules) -> int:
    """Return the case deadline for the alert replay. Check this before loading the log.

    Raises TimeLensError for models with XOR choices, RulesError without a start-to-end deadline.
    """
    if process.has_xor():
        raise TimeLensError("alerts-eval supports only models without XOR choices: the replay "
                            "uses one network for every case")
    deadline = case_deadline(rules, process.start, process.end).max
    assert deadline is not None  # the schema requires max on a deadline
    return deadline


def alert_replay(process: ProcessGraph, rules: Rules, test: pd.DataFrame) -> dict[str, Any]:
    """Return the replay of the test cases: case counts, TimeLens and static-baseline scores.

    `rules_dc` is False if the rules are not DC (then every case alerts at once).
    """
    deadline = replay_deadline(process, rules)
    rules_dc = check_process(process, rules).dynamically_controllable == "DC"
    traces, skipped, misfits = traces_from_times(test, process, process.tasks)
    names = {f"{t}:s": process.nodes[t].name for t in process.tasks}
    res = evaluate(build_stnu(process, rules), traces, deadline, names)
    return {"deadline_min": deadline, "rules_dc": rules_dc, "test_cases_used": len(traces),
            "test_cases_skipped": skipped, "test_cases_misfit": misfits, **res}


def repair_report(process: ProcessGraph, rules: Rules, levers: Levers,
                  inputs: dict[str, str], margin: int = 0) -> tuple[dict[str, Any], Repair]:
    """Return the repair report (verdicts after and before, schedule, repair block) and the
    repair. `inputs` names the input files for the report header. `margin`: spare minutes
    to keep on every deadline and lag."""
    result = repair(process, rules, levers, margin)
    head = header(inputs, levers.assumption_note, deadline_choice=rules.notes)
    report = check_report(process, rules, result.verdict, head, values=result.values)
    b = result.before
    report["verdicts_before"] = {"consistent": b.consistent,
                                 "strongly_controllable": b.strongly_controllable,
                                 "dynamically_controllable": b.dynamically_controllable}
    report["repair"] = repair_block(result)
    return report, result
