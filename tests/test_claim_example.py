"""The claim_full example: three parallel branches, a claim-size XOR, an intermediate decision
event, three deadlines, three lags (one across parallel branches) and two flow gaps."""

import pytest

from timelens.analysis.repair import repair
from timelens.analysis.verdict import check_process
from timelens.model.process import load_model
from timelens.model.rules import load_levers

MODEL = "examples/claim_full/claim_full.bpmn"
RULES = "examples/claim_full/claim_full.rules.json"
LEVERS = "config/levers_claim.yaml"
LARGE = "Claim size?=Site survey by loss adjuster"


def test_claim_model_has_two_scenarios_and_every_rule_type():
    process, rules = load_model(MODEL, RULES)
    assert len(process.tasks) == 10
    assert sorted(s.name for s in process.scenarios()) == [
        "Claim size?=Fast-track assessment", LARGE]
    assert {c.type for c in rules.constraints} == {"deadline", "lag"}
    assert len(rules.flow_gaps) == 2


def test_only_large_claims_can_miss_the_decision_deadline():
    v = check_process(*load_model(MODEL, RULES))
    assert (v.consistent, v.dynamically_controllable) == (True, "NOT_DC")
    failing = v.failing()
    assert [n.name for n in failing] == [LARGE]
    form = failing[0].dc.conflict
    assert -form.value(failing[0].stnu.params) == 5 * 1440 + 8 * 60 + 5  # 5 d 8 h 5 m
    assert form.coefs["C_decision.max"] == 1


def test_cheapest_repair_uses_the_cheapest_levers_first():
    r = repair(*load_model(MODEL, RULES), load_levers(LEVERS))
    assert r.status == "REPAIRED" and r.verdict.dynamically_controllable == "DC"
    assert {c.lever: c.new_min - c.old_min for c in r.changes} == {
        "add_adjuster": -2 * 1440, "extra_committee": -2 * 1440, "remind_customer": -33 * 60}
    assert r.total_cost == pytest.approx(40000 + 50000 + 41250)
