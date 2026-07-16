from __future__ import annotations

import json

import graphify.discovery as d
from graphify.research import build_query_plan, expand_query_plan


def test_collect_status_update_preserves_plan_and_coverage(tmp_path):
    candidate = d.Candidate(
        url="https://sam.gov/opp/HR001121S0042",
        title="DARPA ELGAR solicitation HR001121S0042",
        summary="Official BAA", author="DARPA", provider="host",
        evidence_role="solicitation",
    )
    plan = build_query_plan("DARPA ELGAR")
    expand_query_plan(plan, [candidate])
    policy = d.DiscoveryPolicy(limit=10)
    d.score_candidate(candidate, "DARPA ELGAR", plan=plan)
    queue = tmp_path / "queue.json"
    d.save_queue(queue, "DARPA ELGAR", [candidate], policy, plan)

    d.collect_candidates(queue, approval="1", target_dir=tmp_path / "raw", dry_run=True)
    payload = json.loads(queue.read_text(encoding="utf-8"))
    assert payload["query_plan"]["identifiers"]["contract"] == ["HR001121S0042"]
    assert payload["coverage"]["solicitation"]["found"] == 1


def test_technical_metric_match_is_not_a_hard_unique_identifier():
    candidate = d.Candidate(
        url="https://example.org/220-ghz", title="220 GHz amplifier",
        summary="Measured at 220 GHz", provider="host",
    )
    plan = build_query_plan("DARPA 220 GHz")
    expand_query_plan(plan, [candidate])
    d.score_candidate(candidate, "DARPA 220 GHz", plan=plan)
    assert candidate.exact_match_score == 0.55
    assert candidate.match_reasons == ["exact technical metric: 220 GHZ"]


def test_plan_only_never_initializes_network_providers(monkeypatch, capsys):
    monkeypatch.setattr(d, "_providers_from_args", lambda opts: (_ for _ in ()).throw(
        AssertionError("providers must not be initialized")))
    d.dispatch_cli("discover", ["DARPA ELGAR", "--profile", "government-program", "--plan-only"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["profile"] == "government-program"
    assert any(query["family"] == "official" for query in payload["queries"])
