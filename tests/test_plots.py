from matplotlib.text import Text

from timelens.analysis.conflict import explain
from timelens.analysis.verdict import check_process
from timelens.model.process import load_model
from timelens.report.plots import conflict_figure, schedule_figure
from timelens.report.text import check_report, header


def _texts(fig):
    return {t.get_text() for t in fig.findobj(Text)}


def test_conflict_figure_shows_budget_demand_and_shortfall(minimal_paths):
    verdict = check_process(*load_model(*minimal_paths))
    net = verdict.failing()[0]
    texts = _texts(conflict_figure(explain(net.dc.conflict, net.stnu.params, net.name)))
    assert {"Budget", "Demand", "Shortfall 1 d"} <= texts


def test_conflict_figure_shows_the_time_where_a_name_does_not_fit(minimal_paths):
    verdict = check_process(*load_model(*minimal_paths))
    net = verdict.failing()[0]
    texts = _texts(conflict_figure(explain(net.dc.conflict, net.stnu.params, net.name)))
    assert "Upload documents (customer), maximum" in texts  # 5 d: the name fits
    assert {"2 d", "1 d"} <= texts  # KYC check and Approval: too narrow for the name


def test_schedule_figure_names_tasks_and_slack(minimal_paths):
    process, rules = load_model(*minimal_paths)
    verdict = check_process(process, rules, values={"Task_KYC.max": 1440})
    report = check_report(process, rules, verdict, header({}, "test"),
                          values={"Task_KYC.max": 1440})
    texts = _texts(schedule_figure(report["schedule"]))
    assert {"Upload documents", "KYC check", "Approval"} <= {t.split(" (")[0] for t in texts}
    assert "slack 0 m" in texts


def test_frontier_cost_axis_uses_thousands_separators():
    from timelens.analysis.frontier import FrontierPoint
    from timelens.report.plots import frontier_figure

    points = [FrontierPoint(0.9, 0.37, "NOT_DC", "REPAIRED", 112500.0, "", ""),
              FrontierPoint(0.95, 0.12, "NOT_DC", "REPAIRED", 519583.0, "", "")]
    fig = frontier_figure(points, "INR", "test")
    fig.canvas.draw()
    labels = [t.get_text() for t in fig.axes[0].get_yticklabels()]
    assert "500,000" in labels
