import pandas as pd
import pytest

from tests.helpers import example_stnu
from timelens.analysis.alerts import (
    CaseTrace,
    evaluate,
    first_alert,
    fits_model_order,
    latest_safe_times,
    traces_from_times,
)
from timelens.model.process import load_process

D = 1440
NAMES = {"Task_KYC:s": "KYC check"}


@pytest.fixture
def stnu(minimal_paths):
    return example_stnu(minimal_paths, {"Task_KYC.max": D})  # repaired loan_minimal, DC


def _trace(cid, upload_end, kyc_start, approval_start, end):
    return CaseTrace(cid, {"Z": 0, "Task_Upload:s": 0, "Task_Upload:e": upload_end,
                           "Task_KYC:s": kyc_start, "Task_KYC:e": kyc_start + D,
                           "Task_Approval:s": approval_start, "Task_Approval:e":
                           approval_start + D, "EndEvent_1": end}, end)


ON_TIME = _trace("on_time", 2 * D, 2 * D, 3 * D, 4 * D)
LATE_EARLY_ALERT = _trace("late_alert", 3 * D, 6 * D, 7 * D, 8 * D)  # KYC starts after 5 d
LATE_NO_ALERT = _trace("late_quiet", 1 * D, 1 * D, 2 * D, 8 * D)  # delay after the last task


def test_latest_safe_start_of_kyc_is_five_days(stnu):
    lst = latest_safe_times(stnu, {"Z": 0, "Task_Upload:s": 0, "Task_Upload:e": 2 * D})
    assert lst[stnu.index["Task_KYC:s"]] == 5 * D


def test_inconsistent_observations_mean_risk(stnu):
    assert latest_safe_times(stnu, {"Z": 0, "Task_KYC:s": 6 * D}) is None


def test_alert_text_and_time(stnu):
    alert = first_alert(stnu, LATE_EARLY_ALERT, NAMES)
    assert alert is not None and alert.time == 5 * D and alert.timepoint == "Task_KYC:s"
    assert alert.message.startswith("Start KYC check by minute 7200")
    assert first_alert(stnu, ON_TIME, NAMES) is None
    assert first_alert(stnu, LATE_NO_ALERT, NAMES) is None


def test_replay_scores(stnu):
    res = evaluate(stnu, [ON_TIME, LATE_EARLY_ALERT, LATE_NO_ALERT], 7 * D, NAMES)
    ours = res["timelens"]
    assert (ours["precision"], ours["recall"]) == (1.0, 0.5)
    assert ours["f1"] == pytest.approx(2 / 3)
    assert ours["median_lead_min"] == 2 * D
    base = res["static_baseline"]
    assert (base["precision"], base["recall"], base["median_lead_min"]) == (1.0, 1.0, 3 * D)


TASKS = ["Task_Upload", "Task_KYC", "Task_Approval"]
T0 = pd.Timestamp("2016-01-01", tz="UTC")


def _row(**over):
    """Times of one loan_minimal case: task i runs from day i to day i + 1, end on day 4."""
    row = {"case_start": T0, "case_end": T0 + pd.Timedelta(days=4)}
    for i, task in enumerate(TASKS):
        row[f"{task}:start"] = T0 + pd.Timedelta(days=i)
        row[f"{task}:end"] = T0 + pd.Timedelta(days=i + 1)
    return row | over


def test_traces_keep_cases_with_missing_task_events(minimal_paths):
    times = pd.DataFrame([_row(), _row(**{"Task_KYC:end": pd.NaT}), _row(case_end=pd.NaT)],
                         index=["full", "partial", "open"])
    traces, skipped, misfits = traces_from_times(times, load_process(minimal_paths[0]), TASKS)
    assert ([t.case_id for t in traces], skipped, misfits) == (["full", "partial"], 1, 0)
    assert traces[0].times["Task_KYC:s"] == D and traces[0].duration == 4 * D
    assert "Task_KYC:e" not in traces[1].times


def test_traces_that_break_the_model_order_are_misfits(minimal_paths):
    kyc_early = _row(**{"Task_KYC:start": T0 + pd.Timedelta(hours=12)})  # before upload ends
    before_case = _row(**{"Task_Upload:start": T0 - pd.Timedelta(hours=1)})
    times = pd.DataFrame([_row(), kyc_early, before_case], index=["ok", "kyc", "early"])
    traces, skipped, misfits = traces_from_times(times, load_process(minimal_paths[0]), TASKS)
    assert ([t.case_id for t in traces], skipped, misfits) == (["ok"], 0, 2)


def test_model_order_check_follows_gateways(full_paths):
    process = load_process(full_paths[0])
    times = {"Z": 0, "Task_Scoring:s": 10, "Task_Fraud:s": 5, "Task_Fraud:e": 50}
    assert fits_model_order(process, times)  # parallel branches: any order
    assert not fits_model_order(process, times | {"Task_Approval:s": 40})  # before fraud ends
    assert fits_model_order(process, times | {"Task_Approval:s": 60})


def test_model_task_without_mapping_is_an_input_error(minimal_paths):
    from timelens.errors import LogMappingError

    t0 = pd.Timestamp("2016-01-01", tz="UTC")
    times = pd.DataFrame([{"case_start": t0, "case_end": t0, "Task_Upload:start": t0,
                           "Task_Upload:end": t0}], index=["a"])
    with pytest.raises(LogMappingError, match="Task_Approval, Task_KYC"):
        traces_from_times(times, load_process(minimal_paths[0]),
                          ["Task_Upload", "Task_KYC", "Task_Approval"])
