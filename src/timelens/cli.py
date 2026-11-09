"""TimeLens command line interface.

Exit codes: 0 success and DC, 1 success and NOT DC (or NO_REPAIR), 2 input error,
3 internal error.
"""

from __future__ import annotations

import functools
import json
import subprocess
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, ParamSpec, TypeVar

import pandas as pd
import typer

from timelens.analysis.frontier import cost_never_decreases
from timelens.analysis.frontier import frontier as run_frontier
from timelens.analysis.verdict import check_process
from timelens.errors import TimeLensError
from timelens.mining.calibrate import (
    CalibrationResult,
    calibrate_template,
)
from timelens.mining.log import discover, load_log
from timelens.model.process import load_model, load_process
from timelens.model.rules import load_levers, rules_with_values
from timelens.report.text import (
    NO_COST_NOTE,
    check_report,
    header,
    render,
    render_markdown,
)
from timelens.units import format_minutes, parse_minutes
from timelens.workflow import (
    Deadline,
    alert_replay,
    choose_deadline,
    load_template,
    prepare_times,
    repair_report,
    replay_deadline,
)

app = typer.Typer(help="TimeLens: temporal constraint analysis for BPMN processes.",
                  no_args_is_help=True)

P = ParamSpec("P")
T = TypeVar("T")
# Local only: bind to localhost, send no usage statistics. Colors match the report charts.
UI_FLAGS = {
    "--server.address": "localhost", "--server.maxUploadSize": "1024",
    "--browser.gatherUsageStats": "false", "--client.toolbarMode": "viewer",
    "--theme.base": "light",
    "--theme.primaryColor": "#2a78d6", "--theme.backgroundColor": "#fcfcfb",
    "--theme.secondaryBackgroundColor": "#eef1f4", "--theme.textColor": "#1b2530",
    "--theme.headingFont": "serif", "--theme.baseRadius": "small",
}
JsonOut = Annotated[Path | None, typer.Option("--json", help="Write the JSON report here.")]


def guarded(fn: Callable[P, T]) -> Callable[P, T]:
    """Map input errors to exit code 2 and unexpected errors to exit code 3."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return fn(*args, **kwargs)
        except typer.Exit:
            raise
        except TimeLensError as e:
            typer.echo(f"Input error: {e}", err=True)
            raise typer.Exit(2) from e
        except Exception as e:
            traceback.print_exc()
            typer.echo(f"Internal error: {e}", err=True)
            raise typer.Exit(3) from e

    return wrapper


def _write_json(path: Path | None, report: dict[str, Any]) -> None:
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2))
        typer.echo(f"JSON report: {path}")


@app.command()
@guarded
def check(model: Path, rules: Path, json_out: JsonOut = None,
          force_schedule: Annotated[bool, typer.Option(help="Print the schedule even if not DC.")]
          = False) -> None:
    """Check consistency, strong and dynamic controllability; print schedule or conflicts."""
    process, r = load_model(model, rules)
    verdict = check_process(process, r)
    head = header({"model": str(model), "rules": str(rules)}, NO_COST_NOTE,
                  deadline_choice=r.notes)
    report = check_report(process, r, verdict, head, force_schedule)
    typer.echo(render(report))
    _write_json(json_out, report)
    raise typer.Exit(0 if verdict.dynamically_controllable == "DC" else 1)


@app.command()
@guarded
def repair(model: Path, rules: Path, levers: Path, json_out: JsonOut = None,
           rules_out: Annotated[Path | None, typer.Option(
               help="Write the repaired rules JSON here (input for alerts-eval).")] = None,
           margin: Annotated[int, typer.Option(
               parser=parse_minutes, metavar="TIME",
               help="Spare time to keep on every deadline and lag, e.g. 4h.")] = 0) -> None:
    """Find the cheapest lever changes that make the process dynamically controllable."""
    process, r = load_model(model, rules)
    report, result = repair_report(process, r, load_levers(levers),
                                   {"model": str(model), "rules": str(rules),
                                    "levers": str(levers)}, margin)
    typer.echo(render(report))
    _write_json(json_out, report)
    if rules_out is not None and result.status != "NO_REPAIR":
        rules_out.parent.mkdir(parents=True, exist_ok=True)
        rules_out.write_text(json.dumps(rules_with_values(r, result.values), indent=2))
        typer.echo(f"Repaired rules: {rules_out}")
    raise typer.Exit(1 if result.status == "NO_REPAIR" else 0)


@app.command("discover-log")
@guarded
def discover_log(log: Path) -> None:
    """Print every activity with its lifecycle values and counts."""
    table = discover(load_log(log))
    typer.echo(f"{'activity':<40}{'lifecycle':<16}{'count':>8}")
    for row in table.itertuples(index=False):
        typer.echo(f"{row[0]:<40}{row[1]:<16}{row[2]:>8}")


def _deadline(train: pd.DataFrame, days: int | None) -> Deadline:
    """Return the deadline and print the case duration distribution and the reason."""
    deadline = choose_deadline(train, days)
    typer.echo(deadline.stats_text())
    typer.echo(deadline.reason)
    return deadline


def _print_calibration(res: CalibrationResult) -> None:
    typer.echo(f"{'task':<24}{'n':>7}{'dropped':>9}{'min':>14}{'max':>14}{'alpha':>7}")
    for c in res.table:
        flag = "" if c.calibrated else "  (not calibrated: < 30 samples)"
        typer.echo(f"{c.task:<24}{c.n:>7}{c.dropped:>9}{format_minutes(c.min):>14}"
                   f"{format_minutes(c.max):>14}{c.alpha:>7}{flag}")
    typer.echo(f"Joint coverage on the test set: {res.coverage:.3f} "
               f"({res.coverage_cases} cases), risk {res.risk:.3f}")


DeadlineDays = Annotated[int | None, typer.Option(
    help="Deadline in whole days. Default: p90 of case durations, rounded up.")]


@app.command()
@guarded
def calibrate(model: Path, log: Path, mapping: Path, template: Path,
              out: Annotated[Path, typer.Option(help="Calibrated rules JSON to write.")],
              alpha: Annotated[float, typer.Option(min=0.5, max=0.999)] = 0.95,
              deadline_days: DeadlineDays = None) -> None:
    """Calibrate contingent duration ranges from the event log (train on the first 70 %)."""
    process = load_process(model)
    train, test = prepare_times(log, mapping)
    deadline = _deadline(train, deadline_days)
    res = calibrate_template(process, train, test, load_template(template), alpha,
                             deadline.minutes, deadline.reason)
    _print_calibration(res)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res.rules, indent=2))
    typer.echo(f"Rules written: {out}")


ResultsDir = Annotated[Path, typer.Option(help="Directory for result files.")]


def _csv_header(head: dict[str, Any]) -> str:
    """Return the report header as '#' comment lines for CSV files."""
    lines = [f"date: {head['date']}", *[f"input {k}: {v}" for k, v in head["inputs"].items()],
             f"alpha: {head['alpha']}", f"deadline choice: {head['deadline_choice']}",
             f"assumptions: {head['assumption_note']}"]
    return "".join(f"# {x}\n" for x in lines)


@app.command()
@guarded
def frontier(model: Path, log: Path, mapping: Path, template: Path, levers: Path,
             deadline_days: DeadlineDays = None, out_dir: ResultsDir = Path("results")) -> None:
    """Risk vs cost frontier over alpha in 0.80, 0.85, 0.90, 0.95, 0.99."""
    from timelens.report.plots import frontier_png

    process, lv = load_process(model), load_levers(levers)
    train, test = prepare_times(log, mapping)
    deadline = _deadline(train, deadline_days)
    reason = deadline.reason
    points = run_frontier(process, train, test, load_template(template), lv, deadline.minutes,
                          reason)
    head = header({"model": str(model), "log": str(log), "mapping": str(mapping),
                   "template": str(template), "levers": str(levers)}, lv.assumption_note,
                  alpha="0.80,0.85,0.90,0.95,0.99", deadline_choice=reason)
    typer.echo(f"{'alpha':>6}{'risk %':>9}{'verdict':>10}{'status':>12}{'cost':>14}  changes")
    for p in points:
        typer.echo(f"{p.alpha:>6.2f}{p.risk * 100:>9.2f}{p.verdict:>10}{p.status:>12}"
                   f"{p.cost:>14,.0f}  {p.changes or '-'} {p.note}")
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = [vars(p) for p in points]
    with (out_dir / "frontier.csv").open("w") as f:
        f.write(_csv_header(head))
        f.write(pd.DataFrame(rows).to_csv(index=False))
    frontier_png(points, out_dir / "frontier.png", lv.currency,
                 f"{lv.assumption_note} {reason}")
    typer.echo(f"Wrote {out_dir / 'frontier.csv'} and {out_dir / 'frontier.png'}")
    if not cost_never_decreases(points):  # the repair is a heuristic: cuts plus rounding
        typer.echo("Warning: the repair cost goes down when alpha goes up. The repair is not "
                   "a proven minimum, so compare the levers of the points.")


def _score_line(name: str, sc: dict[str, Any]) -> str:
    lead = sc["median_lead_min"]
    lead_txt = "n/a" if lead != lead else format_minutes(round(lead))
    return (f"| {name} | {sc['alerts']} | {sc['tp']} | {sc['fp']} | {sc['fn']} | "
            f"{sc['precision']:.3f} | {sc['recall']:.3f} | {sc['f1']:.3f} | {lead_txt} |")


@app.command("alerts-eval")
@guarded
def alerts_eval(model: Path, rules: Path, log: Path, mapping: Path,
                out_dir: ResultsDir = Path("results")) -> None:
    """Replay the test cases (last 30 %) with run-time alerts; compare with a static rule."""
    process, r = load_model(model, rules)
    replay_deadline(process, r)  # fail before the slow log load
    _, test = prepare_times(log, mapping)
    res = alert_replay(process, r, test)
    if not res.pop("rules_dc"):
        typer.echo("Warning: these rules are not DC, so the worst case already misses the "
                   "deadline and every case alerts at once. Pass repaired rules "
                   "(timelens repair ... --rules-out).", err=True)
    head = header({"model": str(model), "rules": str(rules), "log": str(log),
                   "mapping": str(mapping)}, NO_COST_NOTE, deadline_choice=r.notes)
    report = {"header": head, **res}
    table = "\n".join([
        f"Test cases used: {res['test_cases_used']}, skipped (no case start or end): "
        f"{res['test_cases_skipped']}, do not fit the model order: {res['test_cases_misfit']}. "
        f"Deadline: {format_minutes(res['deadline_min'])}. Late: {res['timelens']['late']}.", "",
        "| method | alerts | TP | FP | FN | precision | recall | F1 | median lead |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        _score_line("TimeLens latest safe start", res["timelens"]),
        _score_line("Static: no decision by deadline - 3 d", res["static_baseline"]),
        "", "TimeLens is useful only if it alerts earlier at similar precision.",
    ])
    typer.echo(table)
    out_dir.mkdir(parents=True, exist_ok=True)
    clean = json.loads(json.dumps(report), parse_constant=lambda _: None)  # NaN -> null
    (out_dir / "alerts_eval.json").write_text(json.dumps(clean, indent=2, allow_nan=False))
    (out_dir / "alerts_eval.md").write_text(render_markdown("TimeLens alert evaluation",
                                                            report, table))
    typer.echo(f"Wrote {out_dir / 'alerts_eval.json'} and {out_dir / 'alerts_eval.md'}")


@app.command()
@guarded
def bench(sizes: Annotated[str, typer.Option(help="Comma-separated task counts.")]
          = "10,25,50,100,200,300",
          models: Annotated[int, typer.Option(min=1, help="Models per size.")] = 20,
          seed: int = 7, out_dir: ResultsDir = Path("results")) -> None:
    """Time the consistency, SC and DC checks on random structured processes."""
    from timelens.bench.run import parse_sizes, run_bench
    from timelens.report.plots import bench_png

    table = run_bench(parse_sizes(sizes), models, seed, progress=typer.echo)
    head = header({"generator": f"random structured processes, seed {seed}"},
                  "Random models; lag bounds and the lag count (one per 10 tasks) are "
                  "assumptions of the generator.")
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "bench.csv").open("w") as f:
        f.write(_csv_header(head))
        f.write(table.to_csv(index=False))
    bench_png(table, out_dir / "bench.png")
    med = table.groupby("tasks")[["timepoints", "dc", "consistency_s", "sc_s", "dc_s",
                                  "dc_prov_s"]].median()
    typer.echo(med.to_string(float_format=lambda v: f"{v:.4f}"))
    typer.echo(f"Wrote {out_dir / 'bench.csv'} and {out_dir / 'bench.png'}")


@app.command()
def ui(port: Annotated[int, typer.Option(min=1, max=65535)] = 8501) -> None:
    """Open the TimeLens web UI in the browser. It runs on this computer only.

    Without a terminal it starts headless: open the URL it prints.
    """
    app_file = Path(__file__).parent / "ui" / "app.py"
    opts = {**UI_FLAGS, "--server.port": str(port)}
    if not (sys.stdin and sys.stdin.isatty()):  # no one can answer Streamlit's email prompt
        opts["--server.headless"] = "true"
    flags = [x for kv in opts.items() for x in kv]
    raise typer.Exit(subprocess.call([sys.executable, "-m", "streamlit", "run", str(app_file),
                                      *flags]))
