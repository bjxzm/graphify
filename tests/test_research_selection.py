import graphify.discovery as d
from graphify.research import build_query_plan, expand_query_plan


def make(url: str, title: str, role: str = "general") -> d.Candidate:
    return d.Candidate(url=url, title=title, summary="DARPA ELGAR evidence",
                       provider="host", evidence_role=role)


def test_specific_domain_cap_overrides_broader_suffix_cap():
    items = [make(f"https://sam.gov/item/{number}", f"DARPA ELGAR item {number}")
             for number in range(5)]
    policy = d.DiscoveryPolicy(limit=10, max_per_domain=3,
                               domain_caps={"gov": 10, "sam.gov": 2})
    assert len(d.rank_candidates(items, "DARPA ELGAR", policy)) == 2


def test_review_selection_starts_with_evidence_chain_order():
    items = [
        make("https://darpa.mil/elgar", "DARPA ELGAR program", "program_source"),
        make("https://sam.gov/opp/HR001121S0042", "DARPA ELGAR solicitation HR001121S0042", "solicitation"),
        *[make(f"https://sam.gov/award/HR001122C012{number}",
               f"DARPA ELGAR award HR001122C012{number}", "award_notice")
          for number in range(1, 5)],
    ]
    plan = build_query_plan("DARPA ELGAR")
    expand_query_plan(plan, items)
    ranked = d.rank_candidates(items, "DARPA ELGAR",
                               d.DiscoveryPolicy(limit=10, role_quotas={"award_notice": 4}), plan)
    assert [candidate.evidence_role for candidate in ranked[:2]] == ["program_source", "solicitation"]
    assert sum(candidate.evidence_role == "award_notice" for candidate in ranked) == 4
