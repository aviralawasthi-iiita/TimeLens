"""The order_fulfilment example: gateways nested three deep, four XOR scenarios, rules that apply
only in some scenarios, and three rules that fail for backorders by hand-computed gaps."""

import pytest

from timelens.analysis.repair import repair
from timelens.analysis.verdict import check_process
from timelens.errors import RulesError
from timelens.model.process import load_model
from timelens.model.rules import load_levers
from timelens.report.text import check_report, header

MODEL = "examples/order_fulfilment/order_fulfilment.bpmn"
RULES = "examples/order_fulfilment/order_fulfilment.rules.json"
LEVERS = "config/levers_order.yaml"
IN_STOCK, BACKORDER = "Items in stock?=Pick items", "Items in stock?=Backorder from supplier"
DOMESTIC, ABROAD = "Destination?=Print domestic label", "Destination?=Prepare export papers"
FAILING_RULES = ["L_auth_expiry.max", "C_domestic.max", "C_international.max"]
D, H = 1440, 60


def dc(values: dict[str, int] | None = None) -> str:
    return check_process(*load_model(MODEL, RULES), values=values).dynamically_controllable


def test_order_model_has_four_scenarios_and_rules_for_some_of_them():
    process, rules = load_model(MODEL, RULES)
    assert len(process.tasks) == 16
    assert sorted(s.name for s in process.scenarios()) == sorted(
        f"{a}, {b}" for a in (IN_STOCK, BACKORDER) for b in (DOMESTIC, ABROAD))
    assert len(rules.constraints) == 7 and len(rules.flow_gaps) == 2


def test_only_backorders_can_miss_a_rule():
    v = check_process(*load_model(MODEL, RULES))
    assert (v.consistent, v.dynamically_controllable) == (True, "NOT_DC")
    assert sorted(n.name for n in v.failing()) == [f"{BACKORDER}, {ABROAD}",
                                                    f"{BACKORDER}, {DOMESTIC}"]


# Worst case of a backorder: authorise 30 m, supplier 6 d, inspect 1 d, pack 2 h, capture 1 h.
# Domestic adds label 1 m (chosen) and courier 3 d. International adds papers 2 d, gap 4 h,
# ship 8 d and customs clearance 3 d.
@pytest.mark.parametrize("rule, limit, gap", [
    ("L_auth_expiry.max", 7 * D, 2 * H),                  # supplier + inspect + pack
    ("C_domestic.max", 10 * D, 3 * H + 31),               # the whole domestic chain
    ("C_international.max", 18 * D, 2 * D + 7 * H + 30),  # the whole international chain
])
def test_each_rule_fails_by_its_hand_computed_gap(rule, limit, gap):
    relaxed = {r: 60 * D for r in FAILING_RULES if r != rule}
    assert dc({**relaxed, rule: limit + gap}) == "DC"
    assert dc({**relaxed, rule: limit + gap - 1}) == "NOT_DC"


def test_cheapest_repair_fixes_all_three_rules_with_three_levers():
    r = repair(*load_model(MODEL, RULES), load_levers(LEVERS))
    assert r.status == "REPAIRED" and r.verdict.dynamically_controllable == "DC"
    assert {c.lever: c.new_min - c.old_min for c in r.changes} == {
        "priority_broker": -D, "inspection_shift": -20 * H, "express_supplier": -12 * H}
    assert r.total_cost == pytest.approx(4000 + 5000 + 4000)


def test_check_lists_every_rule_that_fails_in_each_scenario():
    process, rules = load_model(MODEL, RULES)
    report = check_report(process, rules, check_process(process, rules), header({}, ""))
    found = {(c["scenario"], tuple(t["param"] for t in c["terms"] if t["coefficient"] > 0),
              c["shortfall_min"]) for c in report["conflicts"]}
    assert found == {
        (f"{BACKORDER}, {DOMESTIC}", ("L_auth_expiry.max",), 2 * H),
        (f"{BACKORDER}, {DOMESTIC}", ("C_domestic.max",), 3 * H + 31),
        (f"{BACKORDER}, {ABROAD}", ("L_auth_expiry.max",), 2 * H),
        (f"{BACKORDER}, {ABROAD}", ("C_international.max",), 2 * D + 7 * H + 30),
    }


def test_margin_keeps_spare_time_on_every_deadline_and_lag():
    process, rules = load_model(MODEL, RULES)
    r = repair(process, rules, load_levers(LEVERS), margin=H)
    assert r.status == "REPAIRED" and r.verdict.dynamically_controllable == "DC"
    assert {c.lever: c.new_min - c.old_min for c in r.changes} == {  # supplier 12 h -> 13 h
        "priority_broker": -D, "inspection_shift": -20 * H, "express_supplier": -13 * H}
    assert r.values["C_international.max"] == 18 * D  # the margin is not written into the rules
    tighter = {f"{c.id}.max": c.max - H for c in rules.constraints if c.max is not None}
    assert check_process(process, rules, {**r.values, **tighter}).dynamically_controllable == "DC"


def test_margin_longer_than_a_rule_is_an_input_error():
    with pytest.raises(RulesError, match="L_pick_start"):  # picking must start within 2 h
        repair(*load_model(MODEL, RULES), load_levers(LEVERS), margin=3 * H)
