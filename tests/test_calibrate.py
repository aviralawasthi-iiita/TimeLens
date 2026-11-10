import json
import warnings
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from tests.synth import BASE, synthetic_log
from timelens.cli import app
from timelens.errors import LogMappingError
from timelens.mining.calibrate import calibrate, joint_coverage, quantile_range
from timelens.mining.log import case_times, discover, load_mapping, split_by_time
from timelens.model.rules import Rules

MAPPING = "config/mapping_bpi2017.yaml"
TEMPLATE = "examples/bpi2017/loan_bpi.rules.template.json"


def _times(durations):
    """One case per duration, started one hour apart."""
    starts = [BASE + pd.Timedelta(hours=i) for i in range(len(durations))]
    return pd.DataFrame({
        "case_start": starts,
        "T:start": starts,
        "T:end": [s + pd.Timedelta(minutes=d) for s, d in zip(starts, durations, strict=True)],
    })


def test_quantiles_are_exact_on_known_durations():
    cal = calibrate(_times(range(1, 101)), ["T"], 0.9, {"T": (0, 0)})[0]
    # linear quantiles of 1..100: q(0.05) = 5.95 -> floor 5, q(0.95) = 95.05 -> ceil 96
    assert (cal.n, cal.min, cal.max, cal.calibrated) == (100, 5, 96, True)
    assert quantile_range(pd.Series(range(1, 101)).to_numpy(), 1.0) == (1, 100)


def test_split_is_chronological():
    times = _times(range(1, 101)).sample(frac=1.0, random_state=3)
    train, test = split_by_time(times)
    assert (len(train), len(test)) == (70, 30)
    assert train["case_start"].max() < test["case_start"].min()


def test_fewer_than_30_samples_warns_and_keeps_fallback():
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        cal = calibrate(_times(range(1, 21)), ["T"], 0.95, {"T": (7, 9)})[0]
    assert any("only 20 samples" in str(x.message) for x in w)
    assert (cal.calibrated, cal.min, cal.max) == (False, 7, 9)


def test_bad_durations_are_dropped():
    times = _times([10, 20, -5])
    times.loc[1, "T:end"] = pd.NaT
    with pytest.warns(UserWarning, match="only 1 samples"):
        assert calibrate(times, ["T"], 0.9, {"T": (0, 0)})[0].dropped == 2


def test_joint_coverage_counts_cases_inside_every_range():
    times = _times([5, 10, 50, 100])
    assert joint_coverage(times, {"T": (5, 50)}) == (0.75, 4)


def test_mapping_extracts_case_times():
    times = case_times(synthetic_log(10), load_mapping(MAPPING))
    assert len(times) == 10 and not times.isna().any().any()
    assert (times["Task_Validate:end"] > times["Task_Validate:start"]).all()


def test_missing_alternative_end_activity_only_warns():
    log = synthetic_log(10)
    log = log[log["concept:name"] != "A_Denied"]
    with pytest.warns(UserWarning, match="A_Denied"):
        case_times(log, load_mapping(MAPPING))


def test_unknown_activity_in_mapping_is_an_error(tmp_path):
    bad = tmp_path / "m.yaml"
    bad.write_text(open(MAPPING).read().replace("O_Returned", "O_Back"))
    with pytest.raises(LogMappingError, match="O_Back"):
        case_times(synthetic_log(10), load_mapping(bad))


def test_discover_lists_lifecycles():
    table = discover(synthetic_log(5))
    row = table[table["concept:name"] == "W_Complete application"]
    assert set(row["lifecycle:transition"]) == {"start", "complete"}
    assert row["count"].tolist() == [5, 5]


def test_cli_calibrate_writes_valid_rules(tmp_path):
    log = tmp_path / "synthetic.parquet"
    synthetic_log(200).to_parquet(log)
    out = tmp_path / "rules.json"
    res = CliRunner().invoke(app, ["calibrate", "examples/bpi2017/loan_bpi.bpmn", str(log),
                                   MAPPING, TEMPLATE, "--alpha", "0.9",
                                   "--out", str(out)])
    assert res.exit_code == 0, res.output
    rules = Rules.model_validate(json.loads(out.read_text()))
    assert "90th percentile" in (rules.notes or "")
    assert rules.tasks["Task_CustomerReturn"].max > 1440
    assert "Joint coverage on the test set" in res.output


def test_csv_log_loads_and_caches(tmp_path):
    from timelens.mining.log import cache_path, load_log

    csv = tmp_path / "bpi.csv"
    csv.write_text(
        "Action,concept:name,lifecycle:transition,time:timestamp,case:concept:name,OfferID\n"
        "Created,A_Create Application,complete,2016-01-01 09:51:15.304000+00:00,App_1,\n"
        "Obtained,W_Complete application,start,2016-01-02 10:45:22+00:00,App_1,\n")
    df = load_log(csv)
    assert list(df.columns) == ["case:concept:name", "concept:name", "lifecycle:transition",
                                "time:timestamp"]
    assert str(df["time:timestamp"].dt.tz) == "UTC" and len(df) == 2
    assert cache_path(csv) == tmp_path / "bpi.csv.parquet" and cache_path(csv).exists()
    assert cache_path(tmp_path / "bpi.xes.gz") != cache_path(csv)


def test_csv_without_lifecycle_or_timestamp_is_an_error(tmp_path):
    from timelens.mining.log import load_log

    csv = tmp_path / "bad.csv"
    csv.write_text("concept:name,case:concept:name\nA_Create Application,App_1\n")
    with pytest.raises(LogMappingError, match="time:timestamp"):
        load_log(csv)


def test_calibration_sets_the_case_deadline_whatever_its_id():
    from timelens.mining.calibrate import calibrate_template
    from timelens.model.process import load_process

    template = json.loads(Path(TEMPLATE).read_text())
    template["constraints"][0]["id"] = "D_decision"
    train, test = split_by_time(case_times(synthetic_log(100), load_mapping(MAPPING)))
    res = calibrate_template(load_process("examples/bpi2017/loan_bpi.bpmn"), train, test,
                             template, 0.9, 36 * 1440, "Deadline 36 d")
    assert res.rules["constraints"][0]["max"] == 36 * 1440
    assert res.rules["notes"] == "Deadline 36 d"


def _calibrate_bpi(template, mapping=MAPPING):
    from timelens.mining.calibrate import calibrate_template
    from timelens.model.process import load_process

    train, test = split_by_time(case_times(synthetic_log(100), load_mapping(mapping)))
    return calibrate_template(load_process("examples/bpi2017/loan_bpi.bpmn"), train, test,
                              template, 0.9, 36 * 1440, "note")


def test_mapped_task_unknown_to_the_template_is_an_error(tmp_path):
    typo = tmp_path / "m.yaml"
    typo.write_text(Path(MAPPING).read_text().replace("Task_Validate:", "Task_Validte:"))
    with pytest.raises(LogMappingError, match="Task_Validte"):
        _calibrate_bpi(json.loads(Path(TEMPLATE).read_text()), typo)


def test_controllable_tasks_keep_their_template_range():
    template = json.loads(Path(TEMPLATE).read_text())
    template["tasks"]["Task_Complete"]["kind"] = "controllable"
    res = _calibrate_bpi(template)
    assert res.rules["tasks"]["Task_Complete"] == template["tasks"]["Task_Complete"]
    assert "Task_Complete" not in {c.task for c in res.table}


def test_contingent_task_without_mapping_warns(tmp_path):
    from timelens.mining.log import LogMapping

    data = load_mapping(MAPPING).model_dump(exclude_none=True)
    del data["tasks"]["Task_Validate"]
    small = tmp_path / "m.yaml"
    small.write_text(json.dumps(LogMapping.model_validate(data).model_dump(exclude_none=True)))
    with pytest.warns(UserWarning, match="Task_Validate: contingent task without a log mapping"):
        _calibrate_bpi(json.loads(Path(TEMPLATE).read_text()), small)
