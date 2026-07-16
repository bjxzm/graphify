import graphify.discovery as d
from graphify.research import build_query_plan, expand_query_plan


def test_search_query_identifier_is_not_treated_as_source_evidence():
    candidate = d.Candidate(
        url="https://example.org/generic",
        title="Generic award page",
        summary="No contract identifier is present in this source.",
        provider="host",
        query_variant='site:sam.gov "HR001122C0122"',
        evidence_role="award_notice",
    )
    plan = build_query_plan("DARPA ELGAR")
    plan.identifiers = {"contract": ["HR001122C0122"]}
    d.score_candidate(candidate, "DARPA ELGAR", plan=plan)
    assert candidate.exact_match_score == 0.0
    assert not any("HR001122C0122" in reason for reason in candidate.match_reasons)


def test_explicit_verified_identifiers_can_drive_fan_out():
    candidate = d.Candidate(
        url="https://example.org/verified",
        title="Verified record",
        summary="Record metadata was verified by the host.",
        provider="host",
        identifiers={"contract": ["HR001122C0122"]},
    )
    plan = build_query_plan("DARPA ELGAR")
    added = expand_query_plan(plan, [candidate])
    assert plan.identifiers["contract"] == ["HR001122C0122"]
    assert any(query.text == '"HR001122C0122"' for query in added)
