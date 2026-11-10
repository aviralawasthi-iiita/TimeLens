"""TimeLens UI pages. Each page picks its inputs, runs one workflow step, keeps the result in the
session and shows it. The analysis code is the same as the CLI's."""

from __future__ import annotations

import io
import json
import math
import warnings
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from timelens.analysis.conflict import Conflict, Term
from timelens.analysis.frontier import FrontierPoint, cost_never_decreases
from timelens.analysis.frontier import frontier as run_frontier
from timelens.analysis.verdict import check_process
from timelens.bench.run import parse_sizes, run_bench
from timelens.mining.calibrate import calibrate_template
from timelens.mining.log import discover, load_log
from timelens.model.process import load_model, load_process
from timelens.model.rules import load_levers, rules_with_values
from timelens.report.plots import bench_figure, conflict_figure, frontier_figure, schedule_figure
from timelens.report.text import NO_COST_NOTE, check_report, header
from timelens.ui.inputs import PLOT_LOCK, messages, name_of, pick_file, run
from timelens.units import format_minutes, parse_minutes
from timelens.workflow import (
    alert_replay,
    choose_deadline,
    load_template,
    prepare_times,
    repair_report,
    replay_deadline,
)

RESULTS = Path("results")
MODEL_EXAMPLE = "examples/loan_minimal/loan_minimal.bpmn"  # first example of the README
LOG_EXAMPLE = "examples/bpi2017/loan_bpi.bpmn"  # the model that has a log mapping

Report = dict[str, Any]
FRONTIER_RESULT = {"ALREADY_DC": "no change needed", "REPAIRED": "repaired",
                   "NO_REPAIR": "no repair within the lever limits"}
BENCH_ROWS = {  # median table of the benchmark; the CSV keeps the raw names
    "timepoints": "Time points", "consistency_s": "Consistency (s)",
    "sc_s": "Strong controllability (s)", "dc_s": "DC, fast engine (s)",
    "dc_prov_s": "DC with provenance (s)"}
YES = {True: "yes", False: "no", None: "unknown"}
DC_TEXT = {"DC": "dynamically controllable", "NOT_DC": "not dynamically controllable",
           "UNKNOWN_XOR": "unknown (XOR model)"}


def _minutes(v: int | float | None) -> str:
    return "none" if v is None else format_minutes(v)


def _json_download(label: str, data: Any, file_name: str) -> None:
    clean = json.loads(json.dumps(data), parse_constant=lambda _: None)  # NaN -> null
    st.download_button(label, json.dumps(clean, indent=2, allow_nan=False), file_name=file_name,
                       mime="application/json", on_click="ignore", icon=":material/download:")


def _table(df: pd.DataFrame) -> None:
    """Show a table; number columns get thousands separators, as in the text around them."""
    st.dataframe(df, hide_index=True, column_config={
        c: st.column_config.NumberColumn(format="localized")
        for c in df.select_dtypes("number").columns})


def _tiles(tiles: dict[str, str]) -> None:
    """The main numbers of a result in one row, before the details."""
    with st.container(horizontal=True):
        for label, value in tiles.items():
            st.metric(label, value)


def _show_inputs(report: Report) -> None:
    h = report["header"]
    st.caption("Inputs: " + ", ".join(f"{k} {v}" for k, v in h["inputs"].items())
               + f". {h['assumption_note']}")


def _show_verdict(report: Report) -> None:
    v, dc = report["verdicts"], report["verdicts"]["dynamically_controllable"]
    if dc == "DC":
        st.success("Every case can meet its time rules: the rules are dynamically controllable.",
                   icon=":material/check_circle:")
    elif dc == "NOT_DC":
        st.error("Some cases can miss a time rule whatever the organization does: the rules are "
                 "not dynamically controllable.", icon=":material/cancel:")
    else:
        st.warning("TimeLens cannot decide for this XOR model.", icon=":material/help:")
    if report["note"]:
        st.info(report["note"], icon=":material/info:")
    st.dataframe(pd.DataFrame({
        "Check": ["Consistent", "Strongly controllable", "Dynamically controllable"],
        "Result": [YES[v["consistent"]], YES[v["strongly_controllable"]],
                   {"DC": "yes", "NOT_DC": "no"}.get(dc, "unknown")],
        "Meaning": ["Some choice of durations meets every rule.",
                    "One fixed timetable works for every duration.",
                    "A timetable that reacts to what has happened works for every duration."],
    }), hide_index=True)


def _show_schedule(report: Report) -> None:
    rows = report["schedule"]
    if not rows:
        return
    st.subheader("Worst-case schedule")
    st.write("Every customer and staff duration takes its maximum. A task start with zero slack "
             "is on the critical path.")
    with PLOT_LOCK:
        st.pyplot(schedule_figure(rows), alt="Worst-case schedule: one bar per task with its "
                                             "latest safe start and slack.")
    st.dataframe(pd.DataFrame([{
        "Element": r["name"], "Point": r["point"], "Earliest": _minutes(r["est_min"]),
        "Latest": _minutes(r["lst_min"]), "Slack": _minutes(r["slack_min"])} for r in rows]),
        hide_index=True)
    names = {r["element"]: r["name"] for r in rows}
    st.write("Critical path: " + (" -> ".join(names[e] for e in report["critical_path"])
                                  or "none"))


def _conflict(c: Report) -> Conflict:
    terms = [Term(**t) for t in c["terms"]]
    return Conflict(c["scenario"], c["shortfall_min"], [t for t in terms if t.coefficient > 0],
                    [t for t in terms if t.coefficient < 0], c["text"])


def _show_conflicts(report: Report) -> None:
    for c in report["conflicts"]:
        scenario = "" if c["scenario"] in ("default", "union") else f" (scenario {c['scenario']})"
        st.subheader(f"Why the rules fail{scenario}")
        if not c["terms"]:  # network too large for provenance
            st.code(c["text"], language=None)
            continue
        st.markdown(f"In the worst case the time budget is {format_minutes(c['shortfall_min'])} "
                    "too small for what the steps demand.")
        with PLOT_LOCK:
            st.pyplot(conflict_figure(_conflict(c)), alt="Budget against demand; the red part "
                                                         "is the shortfall.")
        st.dataframe(pd.DataFrame([{
            "Side": "Budget" if t["coefficient"] > 0 else "Demand",
            "Rule": t["label"] if abs(t["coefficient"]) == 1
            else f"{abs(t['coefficient'])} x {t['label']}",
            "Time": format_minutes(t["value_min"])} for t in c["terms"]]), hide_index=True)


def _check(model: Any, rules: Any, force: bool) -> Report:
    process, r = load_model(model, rules)
    head = header({"model": name_of(model), "rules": name_of(rules)}, NO_COST_NOTE,
                  deadline_choice=r.notes)
    return {"report": check_report(process, r, check_process(process, r), head, force)}


def check() -> None:
    st.title("Check time rules")
    st.write("Find out if every case can meet its deadlines and lags, however long the customer "
             "and staff steps take within their ranges.")
    model = pick_file("model", prefer=MODEL_EXAMPLE)
    rules = pick_file("rules", model)
    force = st.checkbox("Show the worst-case schedule even if the rules fail")
    inputs = (model, rules, force)
    if st.button("Check rules", type="primary", disabled=not (model and rules), key="run:check"):
        run("res:check", "Checking the rules...", lambda: _check(model, rules, force), inputs)
    res = st.session_state.get("res:check")
    if res and messages(res, inputs):
        report = res["report"]
        _show_verdict(report)
        _show_schedule(report)
        _show_conflicts(report)
        _json_download("Download the report (JSON)", report, "check.json")
        _show_inputs(report)


def _save_json(label: str, data: Any, path: Path, key: str) -> bool:
    """A button that writes `data` to `path`, so later pages can pick the file.

    Return True in the run where it saved; the caller confirms it below the button row.
    """
    if not st.button(label, key=key, icon=":material/save:"):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    return True


def _saved(path: Path, links: list[tuple[Any, str, str]]) -> None:
    """Confirm a save and link to the next pages, which open with these rules and this model.

    `links` are (page function, label, icon). The files go in the link, so this page keeps
    its own inputs. The saved file is in the rules picker only if it is under results/.
    """
    st.success(f"Saved {path}. The pages below open with these rules and this model.",
               icon=":material/check_circle:")
    files = {"rules": str(path), "model": st.session_state["pick:model"]}
    with st.container(horizontal=True):
        for page, label, icon in links:
            st.page_link(st.Page(page), label=label, icon=icon, query_params=files)


def _repair(model: Any, rules: Any, levers: Any, margin: int) -> Report:
    process, r = load_model(model, rules)
    report, result = repair_report(process, r, load_levers(levers), {
        "model": name_of(model), "rules": name_of(rules), "levers": name_of(levers)}, margin)
    out = None if result.status == "NO_REPAIR" else rules_with_values(r, result.values)
    return {"report": report, "rules_out": out,
            "file": Path(rules).name.removesuffix(".json").removesuffix(".rules")}


def _show_repair(res: Report) -> None:
    report = res["report"]
    rp = report["repair"]
    cur = rp["currency"]
    if rp["status"] == "REPAIRED":
        st.success(f"Repaired. Total cost {rp['total_cost']:,.0f} {cur} (assumption).",
                   icon=":material/check_circle:")
    elif rp["status"] == "ALREADY_DC":
        st.success("The rules are already dynamically controllable. No change is needed.",
                   icon=":material/check_circle:")
    else:
        st.error("No repair within the lever limits.", icon=":material/cancel:")
        st.write("Levers that touch the conflict: " + (", ".join(rp["touching_levers"])
                                                       or "none"))
        if rp.get("note"):
            st.info(rp["note"], icon=":material/info:")
        if rp.get("last_conflict"):
            st.code(rp["last_conflict"], language=None)
    if rp["changes"]:
        st.dataframe(pd.DataFrame([{
            "Lever": c["label"], "Parameter": c["param"], "From": format_minutes(c["old_min"]),
            "To": format_minutes(c["new_min"]), f"Cost ({cur})": f"{c['cost']:,.0f}"}
            for c in rp["changes"]]), hide_index=True)
    before = DC_TEXT[report["verdicts_before"]["dynamically_controllable"]]
    spare = (f" Spare time kept on every deadline and lag: {format_minutes(rp['margin_min'])}."
             if rp.get("margin_min") else "")
    st.caption(f"Before the repair the rules were {before}. "
               f"Linear program rounds: {rp['lp_rounds']}.{spare}")
    st.subheader("After the repair")
    _show_verdict(report)
    _show_schedule(report)
    _show_conflicts(report)
    path = RESULTS / f"{res['file']}.repaired.rules.json"
    saved = False
    with st.container(horizontal=True):
        _json_download("Download the report (JSON)", report, "repair.json")
        if res["rules_out"] is not None:
            _json_download("Download the repaired rules (JSON)", res["rules_out"], path.name)
            saved = _save_json(f"Save to {path}", res["rules_out"], path, "save:repair")
    if saved:
        _saved(path, [(check, "Check the repaired rules", ":material/rule:"),
                      (alerts, "Replay past cases with them", ":material/notifications:")])


def repair() -> None:
    st.title("Repair time rules")
    st.write("Find the cheapest set of business levers, such as more staff or a later deadline, "
             "that makes every case meet its rules. Costs come from the levers file.")
    model = pick_file("model", prefer=MODEL_EXAMPLE)
    rules, levers = pick_file("rules", model), pick_file("levers", model)
    text = st.text_input("Spare time to keep on every deadline and lag", "0", key="margin",
                         help="For example 4h or 1d. With 0 the repair is the cheapest one, "
                              "and it can leave no spare time.")
    try:
        margin: int | None = parse_minutes(text)
    except ValueError as e:
        st.error(str(e), icon=":material/error:")
        margin = None
    inputs = (model, rules, levers, margin)
    if st.button("Find the cheapest repair", type="primary", key="run:repair",
                 disabled=not (model and rules and levers) or margin is None):
        run("res:repair", "Searching for the cheapest repair...",
            lambda: _repair(model, rules, levers, margin or 0), inputs)
    res = st.session_state.get("res:repair")
    if res and messages(res, inputs):
        _show_repair(res)
        _show_inputs(res["report"])


def _explore(log: Any) -> Report:
    return {"log": name_of(log), "table": discover(load_log(log)).rename(columns={
        "concept:name": "Activity", "lifecycle:transition": "Lifecycle", "count": "Events"})}


def explore() -> None:
    st.title("Explore an event log")
    st.write("List every activity of the log with its lifecycle values and event counts. Use "
             "these names in the log mapping.")
    log = pick_file("log")
    if st.button("List activities", type="primary", disabled=log is None, key="run:explore"):
        run("res:explore", "Reading the event log...", lambda: _explore(log), (log,))
    res = st.session_state.get("res:explore")
    if res and messages(res, (log,)):
        st.caption(f"Log: {res['log']}. {len(res['table'])} activity and lifecycle pairs.")
        _table(res["table"])


@st.cache_data(max_entries=4, show_spinner=False)
def _times(log: str, mapping: str, stamp: tuple[float, float]) -> tuple[Any, Any, list[str]]:
    """Return (train, test, warnings); `stamp` (file times) renews the cache on a change."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        train, test = prepare_times(log, mapping)
    return train, test, [str(w.message) for w in caught if issubclass(w.category, UserWarning)]


def _cached_times(log: Path, mapping: Path) -> tuple[Any, Any]:
    """Return (train, test); repeat the mapping warnings also when the cache answers."""
    train, test, notes = _times(str(log), str(mapping),
                                (log.stat().st_mtime, mapping.stat().st_mtime))
    for note in notes:
        warnings.warn(note, stacklevel=2)
    return train, test


def _deadline_days() -> int | None:
    own = st.checkbox("Set the deadline myself",
                      help="Default: the 90th percentile of the training case durations, rounded "
                           "up to whole days. The log has no official deadline.")
    return int(st.number_input("Deadline (days)", min_value=1, value=30, step=1)) if own else None


def _calibrate(model: Any, log: Any, mapping: Any, template: Any, alpha: float,
               days: int | None) -> Report:
    process = load_process(model)
    train, test = _cached_times(log, mapping)
    deadline = choose_deadline(train, days)
    res = calibrate_template(process, train, test, load_template(template), alpha,
                             deadline.minutes, deadline.reason)
    return {"inputs": f"model {name_of(model)}, log {name_of(log)}, mapping {name_of(mapping)}, "
                      f"template {name_of(template)}",
            "stats": deadline.stats_text(), "reason": deadline.reason,
            "deadline_min": deadline.minutes,
            "table": [{**asdict(c), "name": process.nodes[c.task].name} for c in res.table],
            "coverage": res.coverage,
            "cases": res.coverage_cases, "rules": res.rules,
            "file": Path(f"{process.process_id}.alpha{alpha:g}.rules.json").name}


def calibrate() -> None:
    st.title("Calibrate duration ranges")
    st.write("Learn the range of every customer and staff step from an event log. The first 70 % "
             "of cases (by start time) train the ranges; the last 30 % test them.")
    model, log = pick_file("model", prefer=LOG_EXAMPLE), pick_file("log")
    mapping, template = pick_file("mapping", model), pick_file("template", model)
    alpha = st.number_input("Coverage alpha", min_value=0.5, max_value=0.999, value=0.95,
                            step=0.01, format="%.3f",
                            help="Share of training durations inside each range. A higher alpha "
                                 "gives wider ranges, a lower risk and a higher repair cost.")
    days = _deadline_days()
    ready = model and log and mapping and template
    inputs = (model, log, mapping, template, alpha, days)
    if st.button("Calibrate", type="primary", disabled=not ready, key="run:calibrate"):
        run("res:calibrate", "Reading the log and calibrating...",
            lambda: _calibrate(model, log, mapping, template, alpha, days), inputs)
    res = st.session_state.get("res:calibrate")
    if not (res and messages(res, inputs)):
        return
    cov = res["coverage"]
    unknown = math.isnan(cov)
    _tiles({"Deadline": format_minutes(res["deadline_min"]),
            "Joint coverage": "unknown" if unknown else f"{cov:.3f}",
            "Risk": "unknown" if unknown else f"{(1 - cov) * 100:.1f} %",
            "Test cases": f"{res['cases']:,}"})
    if unknown:
        st.warning("No test case has every mapped event, so the risk is unknown.",
                   icon=":material/warning:")
    st.caption("Joint coverage: the share of test cases where every step stays inside its "
               "range. Risk: the share where some step leaves its range.")
    st.write(res["stats"])
    st.write(res["reason"])
    _table(pd.DataFrame([{
        "Task": c["name"], "Samples": c["n"], "Dropped": c["dropped"],
        "Minimum": format_minutes(c["min"]), "Maximum": format_minutes(c["max"]),
        "Calibrated": "yes" if c["calibrated"] else "no (fewer than 30 samples)"}
        for c in res["table"]]))
    path = RESULTS / res["file"]
    with st.container(horizontal=True):
        _json_download("Download the calibrated rules (JSON)", res["rules"], path.name)
        saved = _save_json(f"Save to {path}", res["rules"], path, "save:calibrate")
    if saved:
        _saved(path, [(repair, "Repair these rules", ":material/build:")])
    st.caption(f"Inputs: {res['inputs']}.")


def _frontier(model: Any, log: Any, mapping: Any, template: Any, levers: Any,
              days: int | None) -> Report:
    process, lv = load_process(model), load_levers(levers)
    train, test = _cached_times(log, mapping)
    deadline = choose_deadline(train, days)
    points = run_frontier(process, train, test, load_template(template), lv, deadline.minutes,
                          deadline.reason)
    return {"points": [asdict(p) for p in points], "currency": lv.currency,
            "caption": f"{lv.assumption_note} {deadline.reason}", "reason": deadline.reason}


def frontier() -> None:
    st.title("Risk against repair cost")
    st.write("Calibrate at alpha 0.80, 0.85, 0.90, 0.95 and 0.99 and find the cheapest repair "
             "for each. A higher alpha means fewer cases outside a range, at a higher cost.")
    model, log = pick_file("model", prefer=LOG_EXAMPLE), pick_file("log")
    mapping, levers = pick_file("mapping", model), pick_file("levers", model)
    template = pick_file("template", model)
    days = _deadline_days()
    ready = model and log and mapping and template and levers
    inputs = (model, log, mapping, template, levers, days)
    if st.button("Compute the frontier", type="primary", disabled=not ready,
                 key="run:frontier"):
        run("res:frontier", "Calibrating and repairing at five alpha values...",
            lambda: _frontier(model, log, mapping, template, levers, days), inputs)
    res = st.session_state.get("res:frontier")
    if not (res and messages(res, inputs)):
        return
    points = [FrontierPoint(**p) for p in res["points"]]
    st.write(res["reason"])
    if not cost_never_decreases(points):
        st.warning("The repair cost goes down when alpha goes up. The repair is not a proven "
                   "minimum, so compare the levers of the points.", icon=":material/warning:")
    with PLOT_LOCK:
        fig = frontier_figure(points, res["currency"], res["caption"])
        st.pyplot(fig, alt="Risk in percent against the minimum repair cost, one point per "
                           "alpha.")
        png = io.BytesIO()
        fig.savefig(png, dpi=130, format="png")
    _table(pd.DataFrame([{
        "Alpha": p.alpha, "Risk (%)": round(p.risk * 100, 1),
        "Controllable before repair": {"DC": "yes", "NOT_DC": "no"}.get(p.verdict, "unknown"),
        "Result": FRONTIER_RESULT[p.status], f"Cost ({res['currency']})":
        "n/a" if math.isnan(p.cost) else f"{p.cost:,.0f}"} for p in points]))
    changed = [p for p in points if p.changes]
    if changed:  # in full here: a table cell cuts long text off
        st.markdown("Lever changes of each repair:\n\n" + "\n".join(
            f"- alpha {p.alpha:g}: {p.changes}" for p in changed))
    csv = pd.DataFrame([asdict(p) for p in points]).to_csv(index=False)  # raw, as the CLI
    with st.container(horizontal=True):
        st.download_button("Download the table (CSV)", csv,
                           file_name="frontier.csv", mime="text/csv", on_click="ignore",
                           icon=":material/download:")
        st.download_button("Download the chart (PNG)", png.getvalue(),
                           file_name="frontier.png", mime="image/png", on_click="ignore",
                           icon=":material/download:")


def _alerts(model: Any, rules: Any, log: Any, mapping: Any) -> Report:
    process, r = load_model(model, rules)
    replay_deadline(process, r)  # fail before the slow log load
    _, test = _cached_times(log, mapping)
    report = alert_replay(process, r, test)
    report["header"] = header({"model": name_of(model), "rules": name_of(rules),
                               "log": name_of(log), "mapping": name_of(mapping)}, NO_COST_NOTE,
                              deadline_choice=r.notes)
    return {"report": report}


def _lead(v: float) -> str:
    return "n/a" if math.isnan(v) else format_minutes(round(v))


def _scores(sc: Report) -> dict[str, str]:
    """One alert method as text, so counts and ratios share a column without losing format."""
    def ratio(v: float) -> str:
        return "n/a" if math.isnan(v) else f"{v:.3f}"
    return {"Alerts": f"{sc['alerts']:,}", "True alerts": f"{sc['tp']:,}",
            "False alerts": f"{sc['fp']:,}", "Missed late cases": f"{sc['fn']:,}",
            "Precision": ratio(sc["precision"]), "Recall": ratio(sc["recall"]),
            "F1": ratio(sc["f1"]), "Median lead": _lead(sc["median_lead_min"])}


def alerts() -> None:
    st.title("Replay past cases with alerts")
    st.write("Replay the test cases (the last 30 % of the log) and raise an alert when a task "
             "has not started by its latest safe start. Compare with a simple rule: alert when "
             "no decision is made 3 days before the deadline.")
    model = pick_file("model", prefer=LOG_EXAMPLE)
    rules, log = pick_file("rules", model), pick_file("log")
    mapping = pick_file("mapping", model)
    inputs = (model, rules, log, mapping)
    if st.button("Replay the cases", type="primary",
                 disabled=not (model and rules and log and mapping), key="run:alerts"):
        run("res:alerts", "Replaying the test cases...",
            lambda: _alerts(model, rules, log, mapping), inputs)
    res = st.session_state.get("res:alerts")
    if not (res and messages(res, inputs)):
        return
    rep = res["report"]
    if not rep["rules_dc"]:
        st.warning("These rules are not dynamically controllable, so the worst case already "
                   "misses the deadline and every case alerts at once. Use repaired rules.",
                   icon=":material/warning:")
    _tiles({"Cases used": f"{rep['test_cases_used']:,}",
            "Late cases": f"{rep['timelens']['late']:,}",
            "Deadline": format_minutes(rep["deadline_min"])})
    st.caption(f"Not used: {rep['test_cases_skipped']:,} cases without a case start or end, "
               f"{rep['test_cases_misfit']:,} cases against the model order.")
    st.dataframe(pd.DataFrame({  # one row per measure: it fits the page width
        "TimeLens latest safe start": _scores(rep["timelens"]),
        "Static: no decision by deadline - 3 d": _scores(rep["static_baseline"]),
    }).rename_axis("Measure").reset_index(), hide_index=True)
    st.caption("TimeLens is useful only if it alerts earlier at a similar precision.")
    _json_download("Download the report (JSON)", rep, "alerts_eval.json")
    _show_inputs(rep)


def _bench(sizes: str, models: int, seed: int) -> Report:
    size_list = parse_sizes(sizes)
    bar = st.progress(0.0, text="Timing the checks...")
    total, done = len(size_list) * models, [0]

    def step(_: str) -> None:
        done[0] += 1
        bar.progress(done[0] / total, text=f"Model {done[0]} of {total}")

    return {"table": run_bench(size_list, models, seed, progress=step)}


def bench() -> None:
    st.title("Benchmark the checks")
    st.write("Time the consistency, strong and dynamic controllability checks on random "
             "structured processes. Large sizes take minutes.")
    sizes = st.text_input("Tasks per model (comma-separated)", value="10,25,50", key="sizes")
    models = int(st.number_input("Models per size", min_value=1, max_value=100, value=5))
    seed = int(st.number_input("Random seed", min_value=0, value=7))
    if st.button("Run the benchmark", type="primary", key="run:bench"):
        run("res:bench", "Running the benchmark...", lambda: _bench(sizes, models, seed),
            (sizes, models, seed))
    res = st.session_state.get("res:bench")
    if not (res and messages(res, (sizes, models, seed))):
        return
    table = res["table"]
    with PLOT_LOCK:
        st.pyplot(bench_figure(table), alt="Median wall time per model size and check.")
    medians = table.groupby("tasks")[list(BENCH_ROWS)].median()

    def cell(col: str, v: float) -> str:  # checks take milliseconds; large sizes skip provenance
        return "n/a" if math.isnan(v) else f"{v:,g}" if col == "timepoints" else f"{v:.4f}"
    st.dataframe(pd.DataFrame({  # one column per size: six measures fit the page width
        f"{size} tasks": {BENCH_ROWS[c]: cell(c, medians.at[size, c]) for c in BENCH_ROWS}
        for size in medians.index}).rename_axis("Median per model").reset_index(),
        hide_index=True)
    st.download_button("Download all timings (CSV)", table.to_csv(index=False),
                       file_name="bench.csv", mime="text/csv", on_click="ignore",
                       icon=":material/download:")
