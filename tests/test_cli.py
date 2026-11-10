import json
import re

import pytest
from typer.testing import CliRunner

from timelens.cli import app

runner = CliRunner()


def test_check_both_examples_exit_1_with_valid_json(tmp_path, minimal_paths, full_paths):
    for paths in (minimal_paths, full_paths):
        out = tmp_path / "check.json"
        res = runner.invoke(app, ["check", *map(str, paths), "--json", str(out)])
        assert res.exit_code == 1, res.output
        report = json.loads(out.read_text())
        assert report["verdicts"]["dynamically_controllable"] == "NOT_DC"
        assert report["conflicts"] and report["header"]["assumption_note"]


def test_check_minimal_report_shape(tmp_path, minimal_paths):
    out = tmp_path / "check.json"
    runner.invoke(app, ["check", *map(str, minimal_paths), "--json", str(out),
                        "--force-schedule"])
    report = json.loads(out.read_text())
    assert report["verdicts"] == {"consistent": True, "strongly_controllable": False,
                                  "dynamically_controllable": "NOT_DC"}
    assert report["conflicts"][0]["shortfall_min"] == 1440
    deadline = next(t for t in report["conflicts"][0]["terms"] if t["param"] == "C_deadline.max")
    assert (deadline["coefficient"], deadline["value_min"]) == (1, 10080)
    assert report["schedule"] == []  # AllMax misses the deadline: no worst-case schedule


def test_repair_minimal_exit_0(tmp_path, minimal_paths):
    out = tmp_path / "repair.json"
    res = runner.invoke(app, ["repair", *map(str, minimal_paths), "config/levers_loan.yaml",
                              "--json", str(out)])
    assert res.exit_code == 0, res.output
    report = json.loads(out.read_text())
    assert report["repair"]["status"] == "REPAIRED"
    assert report["repair"]["total_cost"] == 20000
    assert report["repair"]["changes"][0] | {"cost": 20000} == {
        "lever": "add_kyc_staff", "label": "Add one KYC officer", "param": "Task_KYC.max",
        "old_min": 2880, "new_min": 1440, "cost": 20000}
    assert report["verdicts"]["dynamically_controllable"] == "DC"
    kyc = next(r for r in report["schedule"] if r["element"] == "Task_KYC" and
               r["point"] == "start")
    assert (kyc["est_min"], kyc["lst_min"], kyc["slack_min"]) == (7200, 7200, 0)


def test_input_error_exit_2(tmp_path, minimal_paths):
    bad = tmp_path / "bad.rules.json"
    bad.write_text("{}")
    res = runner.invoke(app, ["check", str(minimal_paths[0]), str(bad)])
    assert res.exit_code == 2


def test_repair_writes_rules_that_check_as_dc(tmp_path, minimal_paths):
    out = tmp_path / "repaired.rules.json"
    runner.invoke(app, ["repair", *map(str, minimal_paths), "config/levers_loan.yaml",
                        "--rules-out", str(out)])
    with pytest.warns(UserWarning, match="Task_KYC: contingent with min == max"):
        res = runner.invoke(app, ["check", str(minimal_paths[0]), str(out)])
    assert res.exit_code == 0, res.output


def test_alerts_eval_writes_both_result_files(tmp_path):
    from tests.synth import synthetic_log

    log = tmp_path / "synthetic.parquet"
    synthetic_log(100).to_parquet(log)
    rules = tmp_path / "bpi.rules.json"
    runner.invoke(app, ["calibrate", "examples/bpi2017/loan_bpi.bpmn", str(log),
                        "config/mapping_bpi2017.yaml",
                        "examples/bpi2017/loan_bpi.rules.template.json",
                        "--out", str(rules), "--deadline-days", "40"])
    res = runner.invoke(app, ["alerts-eval", "examples/bpi2017/loan_bpi.bpmn", str(rules),
                              str(log), "config/mapping_bpi2017.yaml", "--out-dir",
                              str(tmp_path)])
    assert res.exit_code == 0, res.output
    text = (tmp_path / "alerts_eval.json").read_text()
    assert "NaN" not in text  # undefined scores are null, so the file is standard JSON
    report = json.loads(text)
    assert report["test_cases_used"] == 30 and "static_baseline" in report
    assert (report["test_cases_skipped"], report["test_cases_misfit"]) == (0, 0)
    assert report["timelens"]["recall"] is None  # 40 d deadline: no case is late
    md = (tmp_path / "alerts_eval.md").read_text()
    assert "Deadline choice: Deadline 40 d, chosen by the user" in md


def test_bad_template_is_an_input_error(tmp_path):
    from tests.synth import synthetic_log

    log = tmp_path / "synthetic.parquet"
    synthetic_log(50).to_parquet(log)
    bad = tmp_path / "t.json"
    bad.write_text("{not json")
    res = runner.invoke(app, ["calibrate", "examples/bpi2017/loan_bpi.bpmn", str(log),
                              "config/mapping_bpi2017.yaml", str(bad),
                              "--out", str(tmp_path / "r.json")])
    assert res.exit_code == 2


def test_frontier_cost_drop_is_a_warning_not_a_crash(tmp_path, monkeypatch):
    from tests.synth import synthetic_log

    log = tmp_path / "synthetic.parquet"
    synthetic_log(200).to_parquet(log)
    monkeypatch.setattr("timelens.cli.cost_never_decreases", lambda points: False)
    res = runner.invoke(app, ["frontier", "examples/bpi2017/loan_bpi.bpmn", str(log),
                              "config/mapping_bpi2017.yaml",
                              "examples/bpi2017/loan_bpi.rules.template.json",
                              "config/levers_bpi2017.yaml", "--out-dir", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "Warning: the repair cost goes down" in res.output
    assert (tmp_path / "frontier.csv").exists()


def test_alerts_eval_rejects_models_with_xor(full_paths):
    res = runner.invoke(app, ["alerts-eval", *map(str, full_paths), "unused.parquet",
                              "config/mapping_bpi2017.yaml"])
    assert res.exit_code == 2
    assert "without XOR" in res.output


def test_schedule_table_keeps_long_values_and_full_names_apart():
    res = runner.invoke(app, ["repair", "examples/order_fulfilment/order_fulfilment.bpmn",
                              "examples/order_fulfilment/order_fulfilment.rules.json",
                              "config/levers_order.yaml"])
    assert res.exit_code == 0, res.output
    rows = [re.split(r"\s{2,}", ln.strip()) for ln in res.output.splitlines()]
    assert ["Parcel delivered", "start", "17 d 23 h 30 m", "27 d 23 h 59 m", "10 d 29 m"] in rows
    assert ["International carrier ships parcel", "end", "14 d 23 h 30 m", "15 d", "30 m"] in rows


def test_repair_margin_option_reaches_the_report(tmp_path, minimal_paths):
    out = tmp_path / "repair.json"
    res = runner.invoke(app, ["repair", *map(str, minimal_paths), "config/levers_loan.yaml",
                              "--margin", "1h", "--json", str(out)])
    assert res.exit_code == 0, res.output
    assert json.loads(out.read_text())["repair"]["margin_min"] == 60
    bad = runner.invoke(app, ["repair", *map(str, minimal_paths), "config/levers_loan.yaml",
                              "--margin", "soon"])
    assert bad.exit_code == 2
