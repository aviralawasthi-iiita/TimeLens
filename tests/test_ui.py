"""UI pages driven by Streamlit's AppTest with the example files (no browser)."""

import json

from streamlit.testing.v1 import AppTest

MINIMAL = "examples/loan_minimal/loan_minimal.bpmn"
MINIMAL_RULES = "examples/loan_minimal/loan_minimal.rules.json"


def test_check_page_explains_the_loan_minimal_conflict():
    at = _app("check")
    at.selectbox(key="pick:model").select(MINIMAL).run()
    at.selectbox(key="pick:rules").select(MINIMAL_RULES).run()
    at.button(key="run:check").click().run()
    assert not at.exception
    assert "not dynamically controllable" in at.error[0].value
    assert any("1 d too small" in m.value for m in at.markdown)


def test_check_page_shows_an_input_error(tmp_path):
    bad = tmp_path / "bad.rules.json"
    bad.write_text("{}")
    at = _app("check")
    at.selectbox(key="pick:model").select(MINIMAL).run()
    at.selectbox(key="pick:rules").select("Enter a path on this computer...").run()
    at.text_input(key="text:rules").input(str(bad)).run()
    at.button(key="run:check").click().run()
    assert not at.exception
    assert at.error[0].value.startswith("Input error:")


def _app(name):
    """Return a script function that runs the UI page `name` (AppTest needs plain source)."""
    source = {
        "check": "from timelens.ui.pages import check\ncheck()",
        "repair": "from timelens.ui.pages import repair\nrepair()",
        "explore": "from timelens.ui.pages import explore\nexplore()",
        "calibrate": "from timelens.ui.pages import calibrate\ncalibrate()",
        "frontier": "from timelens.ui.pages import frontier\nfrontier()",
        "alerts": "from timelens.ui.pages import alerts\nalerts()",
        "bench": "from timelens.ui.pages import bench\nbench()",
    }[name]
    return AppTest.from_string(source, default_timeout=120).run()


def _local(at, slot, path):
    at.selectbox(key=f"pick:{slot}").select("Enter a path on this computer...").run()
    at.text_input(key=f"text:{slot}").input(str(path)).run()


def _synthetic(tmp_path, n=200):
    from tests.synth import synthetic_log

    log = tmp_path / "synthetic.parquet"
    synthetic_log(n).to_parquet(log)
    return log


def test_repair_page_repairs_and_saves_the_rules(tmp_path, monkeypatch):
    monkeypatch.setattr("timelens.ui.pages.RESULTS", tmp_path)
    at = _app("repair")
    at.selectbox(key="pick:model").select(MINIMAL).run()
    at.selectbox(key="pick:rules").select(MINIMAL_RULES).run()
    at.selectbox(key="pick:levers").select("config/levers_loan.yaml").run()
    at.button(key="run:repair").click().run()
    assert not at.exception
    assert at.success[0].value.startswith("Repaired. Total cost 20,000 INR")
    at.button(key="save:repair").click().run()
    assert (tmp_path / "loan_minimal.repaired.rules.json").exists()


def test_saved_rules_go_to_the_next_pages_through_the_links_only(tmp_path, monkeypatch):
    from urllib.parse import parse_qs

    monkeypatch.setattr("timelens.ui.pages.RESULTS", tmp_path)
    at = _app("repair")
    at.button(key="run:repair").click().run()
    at.button(key="save:repair").click().run()
    links = at.get("page_link")
    assert len(links) == 2
    assert parse_qs(links[0].proto.query_string) == {
        "rules": [str(tmp_path / "loan_minimal.repaired.rules.json")], "model": [MINIMAL]}
    at.run()  # any later rerun on this page: its own inputs stay
    assert at.selectbox(key="pick:rules").value == MINIMAL_RULES
    assert not any("The inputs changed" in w.value for w in at.warning)


def test_a_link_picks_only_files_the_picker_lists():
    full = "examples/loan_full/loan_full.rules.json"
    at = AppTest.from_string("from timelens.ui.pages import check\ncheck()", default_timeout=60)
    at.query_params["rules"] = full
    at.run()
    assert at.selectbox(key="pick:rules").value == full and "rules" not in at.query_params
    at = AppTest.from_string("from timelens.ui.pages import check\ncheck()", default_timeout=60)
    at.query_params["rules"] = "/etc/passwd"  # the URL is input from outside
    at.run()
    assert at.selectbox(key="pick:rules").value == MINIMAL_RULES


def test_a_page_opens_with_the_rules_chosen_on_another_page():
    at = AppTest.from_string("from timelens.ui.pages import check\ncheck()", default_timeout=60)
    at.session_state["choice:rules"] = "examples/loan_full/loan_full.rules.json"
    at.session_state["touched:rules"] = True
    at.run()
    assert at.selectbox(key="pick:rules").value == "examples/loan_full/loan_full.rules.json"


def test_explore_page_lists_activities(tmp_path):
    at = _app("explore")
    _local(at, "log", _synthetic(tmp_path, 20))
    at.button(key="run:explore").click().run()
    assert not at.exception
    table = at.dataframe[0].value
    assert "W_Complete application" in set(table["Activity"])


def test_calibrate_page_writes_rules_with_the_chosen_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr("timelens.ui.pages.RESULTS", tmp_path)
    at = _app("calibrate")
    _local(at, "log", _synthetic(tmp_path))
    at.checkbox[0].check().run()
    at.number_input[1].set_value(25).run()
    at.button(key="run:calibrate").click().run()
    assert not at.exception
    tiles = {m.label: m.value for m in at.metric}
    assert tiles["Deadline"] == "25 d" and tiles["Risk"].endswith(" %")
    at.button(key="save:calibrate").click().run()
    saved = json.loads((tmp_path / "loan_bpi.alpha0.95.rules.json").read_text())
    assert saved["constraints"][0]["max"] == 25 * 1440


def test_number_columns_use_thousands_separators(tmp_path):
    at = _app("calibrate")
    _local(at, "log", _synthetic(tmp_path))
    at.button(key="run:calibrate").click().run()
    columns = json.loads(at.dataframe[0].proto.columns)
    assert columns["Samples"]["type_config"]["format"] == "localized"
    assert columns["Dropped"]["type_config"]["format"] == "localized"


def test_calibrate_page_lists_its_input_files_by_name(tmp_path):
    at = _app("calibrate")
    _local(at, "log", _synthetic(tmp_path))
    at.button(key="run:calibrate").click().run()
    inputs = next(c.value for c in at.caption if c.value.startswith("Inputs:"))
    assert inputs.startswith("Inputs: model examples/bpi2017/loan_bpi.bpmn, log ")


def test_calibrate_table_names_the_tasks_as_the_model_does(tmp_path):
    at = _app("calibrate")
    _local(at, "log", _synthetic(tmp_path))
    at.button(key="run:calibrate").click().run()
    assert set(at.dataframe[0].value["Task"]) == {
        "Complete application", "Customer returns offer", "Validate application"}


def test_case_count_in_the_duration_text_has_a_separator():
    from timelens.workflow import Deadline

    stats = {"n": 22056, "median": 60, "p80": 120, "p90": 180}
    assert "n=22,056)" in Deadline(1440, "", stats).stats_text()


def test_frontier_page_shows_five_points(tmp_path):
    at = _app("frontier")
    _local(at, "log", _synthetic(tmp_path))
    at.button(key="run:frontier").click().run()
    assert not at.exception
    table = at.dataframe[0].value
    assert list(table["Alpha"]) == [0.8, 0.85, 0.9, 0.95, 0.99]
    assert set(table["Result"]) <= {"no change needed", "repaired",
                                    "no repair within the lever limits"}
    assert "Changes" not in table.columns  # listed in full under the table, not cut off


def test_alerts_page_rejects_an_xor_model(tmp_path):
    at = _app("alerts")
    at.selectbox(key="pick:model").select("examples/loan_full/loan_full.bpmn").run()
    at.selectbox(key="pick:rules").select("examples/loan_full/loan_full.rules.json").run()
    _local(at, "log", _synthetic(tmp_path, 20))
    at.button(key="run:alerts").click().run()
    assert not at.exception
    assert "without XOR" in at.error[0].value


def test_alerts_page_replays_calibrated_rules(tmp_path):
    from timelens.mining.calibrate import calibrate_template
    from timelens.model.process import load_process
    from timelens.workflow import load_template, prepare_times

    log = _synthetic(tmp_path, 100)
    train, test = prepare_times(log, "config/mapping_bpi2017.yaml")
    res = calibrate_template(load_process("examples/bpi2017/loan_bpi.bpmn"), train, test,
                             load_template("examples/bpi2017/loan_bpi.rules.template.json"),
                             0.95, 40 * 1440, "Deadline 40 d")
    rules = tmp_path / "bpi.rules.json"
    rules.write_text(json.dumps(res.rules))
    at = _app("alerts")
    _local(at, "rules", rules)
    _local(at, "log", log)
    at.button(key="run:alerts").click().run()
    assert not at.exception
    assert {m.label: m.value for m in at.metric}["Cases used"] == "30"
    table = at.dataframe[0].value  # one row per measure, so the two methods sit side by side
    assert list(table.columns) == ["Measure", "TimeLens latest safe start",
                                   "Static: no decision by deadline - 3 d"]
    assert list(table["Measure"])[-1] == "Median lead"


def test_bench_page_rejects_bad_sizes_and_runs():
    at = _app("bench")
    at.text_input(key="sizes").input("0").run()
    at.button(key="run:bench").click().run()
    assert at.error[0].value.startswith("Input error: sizes must be positive integers")
    at.text_input(key="sizes").input("5").run()
    at.number_input[0].set_value(1).run()
    at.button(key="run:bench").click().run()
    assert not at.exception
    table = at.dataframe[0].value  # one column per size, so six measures fit the page width
    assert list(table.columns) == ["Median per model", "5 tasks"]
    assert "Strong controllability (s)" in set(table["Median per model"])


def test_ui_command_starts_streamlit_on_localhost_only(monkeypatch):
    from typer.testing import CliRunner

    from timelens.cli import app

    calls = []
    monkeypatch.setattr("subprocess.call", lambda args: calls.append(args) or 0)
    res = CliRunner().invoke(app, ["ui", "--port", "8600"])
    assert res.exit_code == 0, res.output
    args = calls[0]
    assert args[1:4] == ["-m", "streamlit", "run"] and args[4].endswith("ui/app.py")
    flags = dict(zip(args[5::2], args[6::2], strict=True))
    assert flags["--server.address"] == "localhost" and flags["--server.port"] == "8600"
    assert flags["--browser.gatherUsageStats"] == "false"


def test_rules_follow_the_model_until_the_user_picks_them():
    at = _app("check")
    at.selectbox(key="pick:model").select(MINIMAL).run()
    assert at.selectbox(key="pick:rules").value == MINIMAL_RULES
    full_rules = "examples/loan_full/loan_full.rules.json"
    at.selectbox(key="pick:rules").select(full_rules).run()
    at.selectbox(key="pick:model").select("examples/bpi2017/loan_bpi.bpmn").run()
    assert at.selectbox(key="pick:rules").value == full_rules


def test_calibrate_saves_inside_results_whatever_the_process_id(tmp_path, monkeypatch):
    results = tmp_path / "results"
    monkeypatch.setattr("timelens.ui.pages.RESULTS", results)
    model = tmp_path / "evil.bpmn"
    model.write_text(open("examples/bpi2017/loan_bpi.bpmn").read()
                     .replace('id="loan_bpi"', 'id="../evil"'))
    at = _app("calibrate")
    _local(at, "model", model)
    at.selectbox(key="pick:template").select(
        "examples/bpi2017/loan_bpi.rules.template.json").run()
    _local(at, "log", _synthetic(tmp_path))
    at.button(key="run:calibrate").click().run()
    at.button(key="save:calibrate").click().run()
    assert not at.exception
    assert (results / "evil.alpha0.95.rules.json").exists()
    assert not (tmp_path / "evil.alpha0.95.rules.json").exists()


def test_each_upload_keeps_its_own_file():
    at = AppTest.from_string('''
import io
import streamlit as st
from timelens.ui.inputs import _store_upload

class Upload(io.BytesIO):
    def __init__(self, name, data):
        super().__init__(data)
        self.name = name

for name, data in (("a.json", b"A"), ("b.json", b"B")):
    st.session_state["up:rules"] = Upload(name, data)
    _store_upload("rules")
saved = st.session_state["uploaded:rules"]
st.write(open(saved["Uploaded: a.json"]).read() + open(saved["Uploaded: b.json"]).read())
''', default_timeout=30).run()
    assert not at.exception
    assert at.markdown[0].value == "AB"


def test_an_unexpected_error_clears_the_old_result(tmp_path):
    bad = tmp_path / "broken.parquet"
    bad.write_bytes(b"not parquet")
    at = _app("explore")
    _local(at, "log", _synthetic(tmp_path, 20))
    at.button(key="run:explore").click().run()
    assert len(at.dataframe) == 1
    at.text_input(key="text:log").input(str(bad)).run()
    at.button(key="run:explore").click().run()
    assert at.exception
    at.run()
    assert len(at.dataframe) == 0


def test_mapping_warnings_show_again_when_the_cache_answers(tmp_path):
    from tests.synth import synthetic_log

    log = tmp_path / "no_denied.parquet"
    df = synthetic_log(200)
    df[df["concept:name"] != "A_Denied"].to_parquet(log)
    at = _app("calibrate")
    _local(at, "log", log)
    for _ in range(2):  # the second click reads the cached case times
        at.button(key="run:calibrate").click().run()
        assert any("A_Denied" in w.value for w in at.warning)


def test_fresh_check_page_has_matching_defaults_and_runs():
    at = _app("check")
    assert at.selectbox(key="pick:model").value == MINIMAL
    assert at.selectbox(key="pick:rules").value == MINIMAL_RULES
    at.button(key="run:check").click().run()
    assert not at.exception
    assert not [e for e in at.error if e.value.startswith("Input error")]
    assert "not dynamically controllable" in at.error[0].value


def test_fresh_repair_page_picks_the_levers_of_the_model():
    at = _app("repair")
    assert at.selectbox(key="pick:levers").value == "config/levers_loan.yaml"
    at.button(key="run:repair").click().run()
    assert at.success[0].value.startswith("Repaired.")


def test_fresh_log_pages_start_from_the_bpi_files():
    at = _app("calibrate")
    assert at.selectbox(key="pick:model").value == "examples/bpi2017/loan_bpi.bpmn"
    assert at.selectbox(key="pick:mapping").value == "config/mapping_bpi2017.yaml"
    assert (at.selectbox(key="pick:template").value
            == "examples/bpi2017/loan_bpi.rules.template.json")


def test_check_page_shows_the_verdict_before_the_inputs():
    at = _app("check")
    at.button(key="run:check").click().run()
    texts = [str(e.value) for e in at.main if e.type in ("error", "caption")]
    verdict = next(i for i, t in enumerate(texts) if "not dynamically controllable" in t)
    inputs = next(i for i, t in enumerate(texts) if t.startswith("Inputs:"))
    assert verdict < inputs


def test_a_result_says_when_the_inputs_changed_after_the_run():
    def stale(at):
        return any("The inputs changed" in w.value for w in at.warning)

    at = _app("check")
    at.button(key="run:check").click().run()
    assert not stale(at)
    at.checkbox[0].check().run()
    assert stale(at)
    at.button(key="run:check").click().run()
    assert not stale(at)


def test_repair_page_keeps_the_spare_time_the_user_asks_for():
    at = _app("repair")
    at.text_input(key="margin").input("soon").run()
    assert at.button(key="run:repair").disabled and at.error
    at.text_input(key="margin").input("1h").run()
    at.button(key="run:repair").click().run()
    assert at.success[0].value.startswith("Repaired.")
    assert any("Spare time kept on every deadline and lag: 1 h." in c.value for c in at.caption)


def test_a_choice_from_another_page_wins_over_the_widget_value_left_from_it():
    # On a page switch the old page's widget value is still in the session state, but the
    # new widget does not use it, so the picker must set the value from the user's choice.
    at = AppTest.from_string("from timelens.ui.pages import check\ncheck()", default_timeout=60)
    at.session_state["pick:rules"] = MINIMAL_RULES
    at.session_state["choice:rules"] = "examples/loan_full/loan_full.rules.json"
    at.session_state["touched:rules"] = True
    at.run()
    assert at.selectbox(key="pick:rules").value == "examples/loan_full/loan_full.rules.json"


def test_rules_picker_lists_repaired_rules_that_the_cli_writes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # the README writes `repair --rules-out results/bpi.repaired.json`
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "bpi.repaired.json").write_text("{}")
    at = _app("check")
    assert "results/bpi.repaired.json" in at.selectbox(key="pick:rules").options


def test_ui_command_starts_headless_without_a_terminal(monkeypatch):
    # Streamlit's first-run email prompt reads stdin; with no terminal it fails with exit 255.
    from typer.testing import CliRunner

    from timelens.cli import app

    calls = []
    monkeypatch.setattr("subprocess.call", lambda args: calls.append(args) or 0)
    res = CliRunner().invoke(app, ["ui"])  # CliRunner's stdin is not a terminal
    assert res.exit_code == 0, res.output
    flags = dict(zip(calls[0][5::2], calls[0][6::2], strict=True))
    assert flags["--server.headless"] == "true"


def test_ui_command_starts_headless_when_stdin_is_closed(monkeypatch):
    import sys

    import pytest
    import typer

    from timelens.cli import ui

    calls = []
    monkeypatch.setattr("subprocess.call", lambda args: calls.append(args) or 0)
    monkeypatch.setattr(sys, "stdin", None)  # as with `timelens ui <&-`
    with pytest.raises(typer.Exit) as done:  # typer.Exit carries the Streamlit exit code
        ui(port=8501)
    assert done.value.exit_code == 0
    flags = dict(zip(calls[0][5::2], calls[0][6::2], strict=True))
    assert flags["--server.headless"] == "true"


def test_a_result_kept_from_older_code_shows_as_old_instead_of_failing():
    at = AppTest.from_string("from timelens.ui.pages import check\ncheck()", default_timeout=60)
    at.session_state["res:check"] = {"warnings": [], "error": "old"}  # no "run_inputs" key
    at.run()
    assert not at.exception
    assert any("The inputs changed" in w.value for w in at.warning)
