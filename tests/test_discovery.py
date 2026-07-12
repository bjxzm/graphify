from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import graphify.discovery as d


def candidate(url: str, title: str = "Knowledge graph with LLM", **kwargs) -> d.Candidate:
    return d.Candidate(url=url, title=title, provider=kwargs.pop("provider", "host"),
                       summary=kwargs.pop("summary", "knowledge graph LLM research"),
                       query=kwargs.pop("query", "knowledge graph LLM"), **kwargs)


def test_canonicalize_removes_tracking_fragment_and_sorts_query():
    assert d.canonicalize_url("HTTPS://Example.COM/a/?utm_source=x&b=2&a=1#part") == (
        "https://example.com/a?a=1&b=2"
    )


def test_candidate_id_is_stable_across_tracking_variants():
    a = candidate("https://example.com/paper?utm_source=x")
    b = candidate("https://EXAMPLE.com/paper#abstract")
    assert a.id == b.id
    assert a.canonical_url == b.canonical_url


def test_host_provider_accepts_list_and_rejects_oversize(tmp_path, monkeypatch):
    path = tmp_path / "host.json"
    path.write_text(json.dumps([{"url": "https://example.com/a", "title": "A"}]), encoding="utf-8")
    found = d.HostFileProvider(path).discover("topic", 10)
    assert found[0].provider == "host"
    monkeypatch.setattr(d, "MAX_CANDIDATE_FILE_BYTES", 1)
    with pytest.raises(ValueError, match="5 MB"):
        d.HostFileProvider(path).discover("topic", 10)


def test_ranking_deduplicates_and_caps_domains():
    items = [
        candidate("https://a.example/1", title="knowledge graph LLM"),
        candidate("https://a.example/1?utm_campaign=x", title="duplicate"),
        candidate("https://a.example/2", title="knowledge graph LLM"),
        candidate("https://b.example/3", title="knowledge graph LLM"),
    ]
    ranked = d.rank_candidates(items, "knowledge graph LLM", d.DiscoveryPolicy(limit=10, max_per_domain=1))
    assert len(ranked) == 2
    assert {Path(c.canonical_url).name for c in ranked} == {"1", "3"}


def test_quality_flags_are_visible_in_review():
    item = candidate("https://example.com/a", title="Untitled source", summary="", published_at=None)
    d.score_candidate(item, "knowledge graph")
    review = d.render_review([item])
    assert "missing title" in review
    assert "missing summary" in review
    assert "unknown publication date" in review
    assert "Nothing has been ingested yet" in review


def test_queue_roundtrip_and_explicit_approval(tmp_path):
    path = tmp_path / "queue.json"
    items = [candidate("https://example.com/a"), candidate("https://example.org/b")]
    policy = d.DiscoveryPolicy(limit=2)
    d.save_queue(path, "knowledge graph", items, policy)
    topic, loaded_policy, loaded, _ = d.load_queue(path)
    assert topic == "knowledge graph"
    assert loaded_policy.limit == 2
    assert d.parse_approval("1," + loaded[1].id, loaded) == {loaded[0].id, loaded[1].id}
    with pytest.raises(ValueError, match="unknown candidate"):
        d.parse_approval("looks-good", loaded)


def test_collect_only_approved_and_writes_provenance(tmp_path, monkeypatch):
    queue = tmp_path / "queue.json"
    target = tmp_path / "raw"
    items = [candidate("https://example.com/a"), candidate("https://example.org/b")]
    d.save_queue(queue, "knowledge graph", items, d.DiscoveryPolicy(limit=2))
    calls = []

    def fake_ingest(url, target_dir, author=None, contributor=None):
        calls.append(url)
        target_dir.mkdir(parents=True, exist_ok=True)
        out = target_dir / "saved.md"
        out.write_text(url, encoding="utf-8")
        return out

    monkeypatch.setattr(d, "ingest", fake_ingest)
    results = d.collect_candidates(queue, approval="1", target_dir=target)
    assert calls == [items[0].canonical_url]
    assert results[0].status == "collected"
    assert results[1].status == "pending"
    record = json.loads((target / ".graphify-sources.jsonl").read_text(encoding="utf-8").strip())
    assert record["candidate_id"] == items[0].id
    assert record["canonical_url"] == items[0].canonical_url
    assert len(record["sha256"]) == 64


def test_collect_deduplicates_from_provenance(tmp_path, monkeypatch):
    queue = tmp_path / "queue.json"
    target = tmp_path / "raw"
    item = candidate("https://example.com/a")
    d.save_queue(queue, "topic", [item], d.DiscoveryPolicy())
    calls = 0

    def fake_ingest(url, target_dir, **kwargs):
        nonlocal calls
        calls += 1
        target_dir.mkdir(parents=True, exist_ok=True)
        out = target_dir / f"{calls}.md"
        out.write_text(url, encoding="utf-8")
        return out

    monkeypatch.setattr(d, "ingest", fake_ingest)
    d.collect_candidates(queue, approval="1", target_dir=target)
    d.collect_candidates(queue, approval="1", target_dir=target)
    assert calls == 1
    assert d.load_queue(queue)[2][0].status == "duplicate"


def test_dry_run_never_ingests(tmp_path, monkeypatch):
    queue = tmp_path / "queue.json"
    d.save_queue(queue, "topic", [candidate("https://example.com/a")], d.DiscoveryPolicy())
    monkeypatch.setattr(d, "ingest", lambda *a, **k: pytest.fail("ingest called"))
    result = d.collect_candidates(queue, approval="1", target_dir=tmp_path / "raw", dry_run=True)
    assert result[0].status == "approved"
    assert not (tmp_path / "raw").exists()


def test_rss_provider_uses_safe_fetch(monkeypatch):
    xml = """<rss><channel><item><title>Knowledge Graph</title>
    <link>https://example.com/post</link><description>LLM research</description>
    <pubDate>Mon, 01 Jun 2026 00:00:00 GMT</pubDate></item></channel></rss>"""
    seen = []
    monkeypatch.setattr(d, "safe_fetch_text", lambda url: seen.append(url) or xml)
    result = d.RssProvider("https://example.com/feed.xml").discover("knowledge graph", 5)
    assert seen == ["https://example.com/feed.xml"]
    assert result[0].title == "Knowledge Graph"


def test_crossref_provider_parses_structured_metadata(monkeypatch):
    payload = {"message": {"items": [{"DOI": "10.1/x", "title": ["Graph RAG"],
               "author": [{"given": "A", "family": "B"}], "published": {"date-parts": [[2026, 1]]},
               "URL": "https://doi.org/10.1/x", "abstract": "<jats:p>LLM graph</jats:p>", "type": "article"}]}}
    monkeypatch.setattr(d, "safe_fetch_text", lambda url: json.dumps(payload))
    result = d.CrossrefProvider().discover("graph", 5)
    assert result[0].author == "A B"
    assert result[0].metadata["doi"] == "10.1/x"


def test_schedule_is_due_once_and_auto_collection_is_opt_in(tmp_path, monkeypatch):
    host = tmp_path / "host.json"
    host.write_text(json.dumps([{"url": "https://example.com/a", "title": "knowledge graph LLM",
                                "summary": "knowledge graph LLM"}]), encoding="utf-8")
    config = tmp_path / "sources.json"
    config.write_text(json.dumps({
        "interval_minutes": 60,
        "topics": [{"query": "knowledge graph LLM"}],
        "providers": [{"type": "host", "path": str(host)}],
        "target_dir": str(tmp_path / "raw"),
        "output_dir": str(tmp_path / "queues"),
        "policy": {"limit": 5, "auto_approve_score": 0.0, "max_collect": 2},
    }), encoding="utf-8")

    def fake_ingest(url, target_dir, **kwargs):
        target_dir.mkdir(parents=True, exist_ok=True)
        out = target_dir / "saved.md"
        out.write_text(url, encoding="utf-8")
        return out

    monkeypatch.setattr(d, "ingest", fake_ingest)
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    first = d.run_schedule(config, now=now)
    second = d.run_schedule(config, now=now + timedelta(minutes=10))
    assert first["collected"] == 1
    assert second["status"] == "not_due"
    assert not config.with_suffix(".json.lock").exists()


def test_schedule_without_threshold_only_creates_review_queue(tmp_path):
    host = tmp_path / "host.json"
    host.write_text(json.dumps([{"url": "https://example.com/a", "title": "topic"}]), encoding="utf-8")
    config = tmp_path / "sources.json"
    config.write_text(json.dumps({
        "topics": ["topic"], "providers": [{"type": "host", "path": str(host)}],
        "output_dir": str(tmp_path / "queues"), "target_dir": str(tmp_path / "raw"),
        "policy": {"limit": 5, "auto_approve_score": None},
    }), encoding="utf-8")
    result = d.run_schedule(config, force=True)
    assert result["collected"] == 0
    assert len(list((tmp_path / "queues").glob("*.json"))) == 1
    assert not (tmp_path / "raw").exists()
