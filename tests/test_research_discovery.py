from __future__ import annotations

import json
from pathlib import Path

import graphify.discovery as d
from graphify.research import (build_query_plan, coverage_report, expand_query_plan,
                               extract_identifiers, tokenize)


FIXTURE = Path(__file__).parent / "fixtures" / "discovery" / "elgar_candidates.json"


def elgar_candidates() -> list[d.Candidate]:
    values = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [d.Candidate.from_mapping(value, query="DARPA ELGAR", provider="host") for value in values]


def test_government_program_plan_starts_with_authoritative_queries():
    plan = build_query_plan("DARPA ELGAR")
    assert plan.profile == "government-program"
    assert {query.family for query in plan.queries} >= {"seed", "official", "outcome"}
    assert any("site:darpa.mil" in query.text for query in plan.queries)
    assert any("site:sam.gov" in query.text for query in plan.queries)
    assert plan.required_roles == [
        "program_source", "solicitation", "award_notice", "publication", "current_status",
    ]


def test_mixed_chinese_ascii_tokenization_and_identifier_patterns():
    assert {"elgar", "项目"} <= tokenize("ELGAR项目")
    found = extract_identifiers("合同 HR001122C0122 achieved 220 GHz and 18% PAE; DOI 10.1109/X.2026.1")
    assert found["contract"] == ["HR001122C0122"]
    assert found["frequency"] == ["220 GHZ"]
    assert found["percentage"] == ["18%"]
    assert found["doi"] == ["10.1109/x.2026.1"]


def test_elgar_benchmark_preserves_all_awards_and_rejects_namesake_from_top_ten():
    items = elgar_candidates()
    plan = build_query_plan("DARPA ELGAR")
    expand_query_plan(plan, items)
    ranked = d.rank_candidates(items, "DARPA ELGAR", d.DiscoveryPolicy(limit=10, max_per_domain=1), plan)

    urls = {candidate.canonical_url for candidate in ranked}
    assert len(ranked) == 10
    assert sum(candidate.evidence_role == "award_notice" for candidate in ranked) == 4
    assert all(f"https://sam.gov/award/HR001122C012{number}" in urls for number in range(1, 5))
    assert not any("atom" in candidate.title.lower() or "gravitation" in candidate.title.lower()
                   for candidate in ranked)
    assert all(candidate.query_variant and candidate.evidence_role and candidate.match_reasons
               for candidate in ranked)


def test_elgar_coverage_reports_unresolved_current_status_gap():
    items = elgar_candidates()
    plan = build_query_plan("DARPA ELGAR")
    report = coverage_report(items, plan)
    assert report["program_source"]["found"] == 1
    assert report["award_notice"]["found"] == 5  # includes the duplicate before canonical ranking
    assert report["current_status"]["gap"] is True


def test_review_groups_evidence_roles_and_shows_discovery_path():
    items = elgar_candidates()[:2]
    plan = build_query_plan("DARPA ELGAR")
    expand_query_plan(plan, items)
    ranked = d.rank_candidates(items, "DARPA ELGAR", d.DiscoveryPolicy(limit=10), plan)
    review = d.render_review(ranked, plan)
    assert "Evidence coverage:" in review
    assert "program_source" in review
    assert "current_status | 0 / 1 | YES" in review
    assert "exact identifier: HR001121S0042" in review
    assert "Nothing has been ingested yet" in review


def test_queue_persists_query_plan_and_coverage(tmp_path):
    items = elgar_candidates()[:2]
    plan = build_query_plan("DARPA ELGAR")
    expand_query_plan(plan, items)
    ranked = d.rank_candidates(items, "DARPA ELGAR", d.DiscoveryPolicy(limit=10), plan)
    queue = tmp_path / "candidates.json"
    d.save_queue(queue, "DARPA ELGAR", ranked, d.DiscoveryPolicy(limit=10), plan)
    payload = json.loads(queue.read_text(encoding="utf-8"))
    assert payload["query_plan"]["profile"] == "government-program"
    assert payload["query_plan"]["identifiers"]["contract"] == ["HR001121S0042"]
    assert payload["coverage"]["award_notice"]["gap"] is True


def test_discover_with_plan_fans_out_after_identifier_discovery():
    class IterativeProvider:
        name = "test-web"

        def __init__(self):
            self.queries: list[str] = []
            self.seed_returned = False

        def discover(self, query: str, limit: int) -> list[d.Candidate]:
            self.queries.append(query)
            if not self.seed_returned and "DARPA ELGAR" in query:
                self.seed_returned = True
                return [d.Candidate(
                    url="https://darpa.mil/elgar", title="DARPA ELGAR program",
                    summary="Solicitation HR001121S0042", author="DARPA", provider=self.name,
                    evidence_role="program_source",
                )]
            if "HR001121S0042" in query:
                return [d.Candidate(
                    url="https://sam.gov/opp/HR001121S0042", title="ELGAR BAA HR001121S0042",
                    summary="Official solicitation", author="DARPA", provider=self.name,
                    evidence_role="solicitation",
                )]
            return []

    provider = IterativeProvider()
    candidates, plan = d.discover_with_plan(
        "DARPA ELGAR", [provider], d.DiscoveryPolicy(limit=10, max_rounds=2),
    )
    assert any('"HR001121S0042"' == query for query in provider.queries)
    assert plan.identifiers["contract"] == ["HR001121S0042"]
    assert {candidate.evidence_role for candidate in candidates} >= {"program_source", "solicitation"}
