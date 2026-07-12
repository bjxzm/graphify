"""Discover, review, collect, and schedule web sources for graphify.

The module deliberately separates discovery from ingestion.  Providers only
produce candidates; nothing is downloaded into the corpus until a candidate is
explicitly approved or passes an opt-in automatic approval threshold.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol

from graphify.ingest import ingest
from graphify.security import safe_fetch_text


SCHEMA_VERSION = 1
MAX_CANDIDATES = 1000
MAX_CANDIDATE_FILE_BYTES = 5 * 1024 * 1024
TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
PROVIDER_QUALITY = {"arxiv": 0.95, "crossref": 0.9, "rss": 0.72, "host": 0.65}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonicalize_url(url: str) -> str:
    """Return a stable public URL, removing fragments and tracking parameters."""
    parsed = urllib.parse.urlsplit(str(url).strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"candidate URL must be absolute http(s): {url!r}")
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower().rstrip(".")
    port = parsed.port
    netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower().startswith("utm_") or key.lower() in TRACKING_PARAMS:
            continue
        query.append((key, value))
    return urllib.parse.urlunsplit((scheme, netloc, path, urllib.parse.urlencode(sorted(query)), ""))


def _candidate_id(url: str) -> str:
    return "src_" + hashlib.sha256(canonicalize_url(url).encode("utf-8")).hexdigest()[:12]


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class Candidate:
    url: str
    title: str
    provider: str
    source_type: str = "webpage"
    summary: str = ""
    author: str = ""
    published_at: str | None = None
    query: str = ""
    discovered_at: str = field(default_factory=_now)
    canonical_url: str = ""
    id: str = ""
    relevance_score: float = 0.0
    quality_score: float = 0.0
    freshness_score: float = 0.0
    score: float = 0.0
    status: str = "pending"
    quality_issues: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    local_path: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        self.url = str(self.url).strip()
        self.title = re.sub(r"\s+", " ", str(self.title)).strip()[:500]
        self.summary = re.sub(r"\s+", " ", str(self.summary)).strip()[:4000]
        self.provider = re.sub(r"[^a-z0-9_-]", "", str(self.provider).lower())[:50] or "host"
        self.canonical_url = self.canonical_url or canonicalize_url(self.url)
        self.id = self.id or _candidate_id(self.canonical_url)
        if self.status not in {"pending", "approved", "rejected", "collected", "failed", "duplicate"}:
            raise ValueError(f"invalid candidate status: {self.status!r}")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, query: str = "", provider: str = "host") -> "Candidate":
        if not isinstance(value, Mapping):
            raise ValueError("candidate must be a JSON object")
        return cls(
            url=str(value.get("url") or value.get("canonical_url") or ""),
            title=str(value.get("title") or "Untitled source"),
            provider=str(value.get("provider") or provider),
            source_type=str(value.get("source_type") or value.get("type") or "webpage"),
            summary=str(value.get("summary") or value.get("abstract") or ""),
            author=str(value.get("author") or ""),
            published_at=value.get("published_at") or value.get("date"),
            query=str(value.get("query") or query),
            discovered_at=str(value.get("discovered_at") or _now()),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass
class DiscoveryPolicy:
    limit: int = 10
    min_score: float = 0.0
    max_per_domain: int = 3
    allowed_domains: list[str] = field(default_factory=list)
    blocked_domains: list[str] = field(default_factory=list)
    require_https: bool = False
    max_age_days: int | None = None
    auto_approve_score: float | None = None
    max_collect: int = 10

    def __post_init__(self) -> None:
        self.limit = max(1, min(int(self.limit), 100))
        self.max_per_domain = max(1, min(int(self.max_per_domain), 20))
        self.max_collect = max(1, min(int(self.max_collect), 100))
        if self.auto_approve_score is not None and not 0 <= self.auto_approve_score <= 1:
            raise ValueError("auto_approve_score must be between 0 and 1")


class DiscoveryProvider(Protocol):
    name: str

    def discover(self, query: str, limit: int) -> list[Candidate]: ...


class HostFileProvider:
    name = "host"

    def __init__(self, path: Path):
        self.path = Path(path)

    def discover(self, query: str, limit: int) -> list[Candidate]:
        if self.path.stat().st_size > MAX_CANDIDATE_FILE_BYTES:
            raise ValueError("host candidate file exceeds 5 MB limit")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        values = payload.get("candidates", []) if isinstance(payload, dict) else payload
        if not isinstance(values, list):
            raise ValueError("host candidate JSON must be a list or contain a candidates list")
        if len(values) > MAX_CANDIDATES:
            raise ValueError(f"host candidate file exceeds {MAX_CANDIDATES} items")
        return [Candidate.from_mapping(v, query=query, provider=self.name) for v in values[:limit]]


class ArxivProvider:
    name = "arxiv"

    def discover(self, query: str, limit: int) -> list[Candidate]:
        params = urllib.parse.urlencode({"search_query": f"all:{query}", "start": 0, "max_results": limit})
        root = ET.fromstring(safe_fetch_text(f"https://export.arxiv.org/api/query?{params}"))
        ns = {"a": "http://www.w3.org/2005/Atom"}
        out = []
        for entry in root.findall("a:entry", ns):
            url = (entry.findtext("a:id", default="", namespaces=ns) or "").strip()
            if not url:
                continue
            authors = [a.findtext("a:name", default="", namespaces=ns) for a in entry.findall("a:author", ns)]
            out.append(Candidate(
                url=url, title=entry.findtext("a:title", default="", namespaces=ns),
                summary=entry.findtext("a:summary", default="", namespaces=ns),
                author=", ".join(x for x in authors if x),
                published_at=entry.findtext("a:published", default=None, namespaces=ns),
                provider=self.name, source_type="paper", query=query,
            ))
        return out


class CrossrefProvider:
    name = "crossref"

    def discover(self, query: str, limit: int) -> list[Candidate]:
        params = urllib.parse.urlencode({"query": query, "rows": limit, "select": "DOI,title,author,published,URL,abstract,type"})
        payload = json.loads(safe_fetch_text(f"https://api.crossref.org/works?{params}"))
        out = []
        for item in payload.get("message", {}).get("items", []):
            url = item.get("URL") or (f"https://doi.org/{item['DOI']}" if item.get("DOI") else "")
            if not url:
                continue
            title = (item.get("title") or ["Untitled work"])[0]
            authors = ", ".join(
                " ".join(filter(None, (a.get("given"), a.get("family")))) for a in item.get("author", [])
            )
            parts = ((item.get("published") or {}).get("date-parts") or [[]])[0]
            published = "-".join(str(x) for x in parts) if parts else None
            out.append(Candidate(url=url, title=title, summary=re.sub(r"<[^>]+>", " ", item.get("abstract", "")),
                                 author=authors, published_at=published, provider=self.name,
                                 source_type=str(item.get("type") or "paper"), query=query,
                                 metadata={"doi": item.get("DOI")}))
        return out


class RssProvider:
    name = "rss"

    def __init__(self, feed_url: str):
        self.feed_url = canonicalize_url(feed_url)

    def discover(self, query: str, limit: int) -> list[Candidate]:
        root = ET.fromstring(safe_fetch_text(self.feed_url))
        out: list[Candidate] = []
        entries = root.findall(".//item") or root.findall("{http://www.w3.org/2005/Atom}entry")
        atom = "{http://www.w3.org/2005/Atom}"
        for entry in entries[:limit * 3]:
            title = entry.findtext("title") or entry.findtext(atom + "title") or "Untitled source"
            link = entry.findtext("link")
            if not link:
                node = entry.find(atom + "link")
                link = node.get("href") if node is not None else None
            if not link:
                continue
            summary = entry.findtext("description") or entry.findtext(atom + "summary") or ""
            date = entry.findtext("pubDate") or entry.findtext(atom + "published") or entry.findtext(atom + "updated")
            out.append(Candidate(url=link, title=title, summary=re.sub(r"<[^>]+>", " ", summary),
                                 published_at=date, provider=self.name, source_type="feed", query=query,
                                 metadata={"feed_url": self.feed_url}))
        return out[:limit]


def _tokens(text: str) -> set[str]:
    return {x for x in re.findall(r"[\w\u4e00-\u9fff]+", text.lower()) if len(x) > 1}


def score_candidate(candidate: Candidate, query: str, *, now: datetime | None = None) -> Candidate:
    q = _tokens(query)
    text = _tokens(f"{candidate.title} {candidate.summary}")
    candidate.relevance_score = min(1.0, len(q & text) / max(1, len(q)))
    completeness = sum(bool(x) for x in (candidate.title, candidate.summary, candidate.author, candidate.published_at)) / 4
    candidate.quality_score = min(1.0, PROVIDER_QUALITY.get(candidate.provider, 0.55) * 0.75 + completeness * 0.25)
    published = _parse_date(candidate.published_at)
    if published is None:
        candidate.freshness_score = 0.5
    else:
        days = max(0, ((now or datetime.now(timezone.utc)) - published).days)
        candidate.freshness_score = math.exp(-days / 1095)
    candidate.quality_issues = []
    if candidate.title.lower().startswith("untitled"):
        candidate.quality_issues.append("missing title")
    if not candidate.summary:
        candidate.quality_issues.append("missing summary")
    if not candidate.published_at:
        candidate.quality_issues.append("unknown publication date")
    candidate.score = round(0.55 * candidate.relevance_score + 0.3 * candidate.quality_score + 0.15 * candidate.freshness_score, 4)
    return candidate


def rank_candidates(candidates: Iterable[Candidate], query: str, policy: DiscoveryPolicy) -> list[Candidate]:
    deduped: dict[str, Candidate] = {}
    for item in candidates:
        domain = urllib.parse.urlsplit(item.canonical_url).hostname or ""
        if policy.require_https and not item.canonical_url.startswith("https://"):
            continue
        if policy.allowed_domains and not any(domain == d or domain.endswith("." + d) for d in policy.allowed_domains):
            continue
        if any(domain == d or domain.endswith("." + d) for d in policy.blocked_domains):
            continue
        if policy.max_age_days is not None:
            published = _parse_date(item.published_at)
            if published and datetime.now(timezone.utc) - published > timedelta(days=policy.max_age_days):
                continue
        score_candidate(item, query)
        old = deduped.get(item.canonical_url)
        if old is None or item.score > old.score:
            deduped[item.canonical_url] = item
    ranked = sorted(deduped.values(), key=lambda c: (-c.score, c.canonical_url))
    selected, per_domain = [], {}
    for item in ranked:
        domain = urllib.parse.urlsplit(item.canonical_url).hostname or ""
        if item.score < policy.min_score or per_domain.get(domain, 0) >= policy.max_per_domain:
            continue
        selected.append(item)
        per_domain[domain] = per_domain.get(domain, 0) + 1
        if len(selected) >= policy.limit:
            break
    return selected


def save_queue(path: Path, topic: str, candidates: list[Candidate], policy: DiscoveryPolicy) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": SCHEMA_VERSION, "topic": topic, "created_at": _now(),
               "policy": asdict(policy), "candidates": [asdict(c) for c in candidates]}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def load_queue(path: Path) -> tuple[str, DiscoveryPolicy, list[Candidate], dict[str, Any]]:
    path = Path(path)
    if path.stat().st_size > MAX_CANDIDATE_FILE_BYTES:
        raise ValueError("candidate queue exceeds 5 MB limit")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported candidate queue schema")
    values = payload.get("candidates")
    if not isinstance(values, list) or len(values) > MAX_CANDIDATES:
        raise ValueError("invalid candidate queue")
    candidates = []
    for value in values:
        candidate = Candidate.from_mapping(value, query=str(value.get("query") or payload.get("topic") or ""),
                                           provider=str(value.get("provider") or "host"))
        for key in ("id", "canonical_url", "discovered_at", "status", "local_path", "error"):
            if key in value:
                setattr(candidate, key, value[key])
        for key in ("relevance_score", "quality_score", "freshness_score", "score"):
            setattr(candidate, key, float(value.get(key, 0)))
        candidate.quality_issues = list(value.get("quality_issues") or [])
        candidates.append(candidate)
    return str(payload.get("topic") or ""), DiscoveryPolicy(**payload.get("policy", {})), candidates, payload


def render_review(candidates: list[Candidate]) -> str:
    lines = ["| # | ID | Score | Title | Provider / type | Date | Quality | URL |",
             "|---:|---|---:|---|---|---|---|---|"]
    for idx, c in enumerate(candidates, 1):
        issues = ", ".join(c.quality_issues) or "OK"
        title = c.title.replace("|", "\\|")
        lines.append(f"| {idx} | `{c.id}` | {c.score:.3f} | {title} | {c.provider} / {c.source_type} | {c.published_at or 'unknown'} | {issues} | {c.canonical_url} |")
    lines += ["", "Review required: use `graphify collect --approve 1,3` or `--approve-all`. Nothing has been ingested yet."]
    return "\n".join(lines)


def parse_approval(value: str, candidates: list[Candidate]) -> set[str]:
    approved: set[str] = set()
    for token in re.split(r"[,\s]+", value.strip()):
        if not token:
            continue
        if token.isdigit() and 1 <= int(token) <= len(candidates):
            approved.add(candidates[int(token) - 1].id)
        elif any(c.id == token for c in candidates):
            approved.add(token)
        else:
            raise ValueError(f"unknown candidate selection: {token!r}")
    if not approved:
        raise ValueError("no candidates explicitly approved")
    return approved


def _provenance_path(target_dir: Path) -> Path:
    return target_dir / ".graphify-sources.jsonl"


def _existing_urls(path: Path) -> set[str]:
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
            if record.get("status") == "collected":
                out.add(record.get("canonical_url", ""))
        except json.JSONDecodeError:
            continue
    return out


def collect_candidates(queue_path: Path, *, approval: str | None = None, approve_all: bool = False,
                       target_dir: Path = Path("raw"), author: str | None = None,
                       contributor: str | None = None, dry_run: bool = False,
                       selection_mode: str = "manual") -> list[Candidate]:
    topic, policy, candidates, _ = load_queue(queue_path)
    approved = {c.id for c in candidates} if approve_all else parse_approval(approval or "", candidates)
    if len(approved) > policy.max_collect:
        raise ValueError(f"approval exceeds max_collect={policy.max_collect}")
    target_dir = Path(target_dir)
    provenance = _provenance_path(target_dir)
    existing = _existing_urls(provenance)
    records = []
    for c in candidates:
        if c.id not in approved:
            continue
        c.status = "approved"
        if c.canonical_url in existing:
            c.status = "duplicate"
            continue
        if dry_run:
            continue
        try:
            out = ingest(c.canonical_url, target_dir, author=author or c.author, contributor=contributor)
            c.local_path = str(out)
            c.status = "collected"
            digest = hashlib.sha256(out.read_bytes()).hexdigest()
            existing.add(c.canonical_url)
            records.append({"schema_version": SCHEMA_VERSION, "candidate_id": c.id, "original_url": c.url,
                            "canonical_url": c.canonical_url, "provider": c.provider, "query": topic,
                            "discovered_at": c.discovered_at, "collected_at": _now(),
                            "selection_mode": selection_mode, "score": c.score,
                            "local_path": c.local_path, "sha256": digest, "status": "collected"})
        except Exception as exc:
            c.status, c.error = "failed", str(exc)[:1000]
            records.append({"schema_version": SCHEMA_VERSION, "candidate_id": c.id,
                            "canonical_url": c.canonical_url, "provider": c.provider,
                            "collected_at": _now(), "selection_mode": selection_mode,
                            "status": "failed", "error": c.error})
    if records:
        target_dir.mkdir(parents=True, exist_ok=True)
        with provenance.open("a", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    save_queue(queue_path, topic, candidates, policy)
    return candidates


PROVIDER_FACTORIES: dict[str, Callable[[Mapping[str, Any]], DiscoveryProvider]] = {
    "arxiv": lambda cfg: ArxivProvider(),
    "crossref": lambda cfg: CrossrefProvider(),
    "rss": lambda cfg: RssProvider(str(cfg["url"])),
    "host": lambda cfg: HostFileProvider(Path(str(cfg["path"]))),
}


def discover(topic: str, providers: Iterable[DiscoveryProvider], policy: DiscoveryPolicy) -> list[Candidate]:
    if not topic.strip() or len(topic) > 500:
        raise ValueError("topic must contain 1-500 characters")
    found: list[Candidate] = []
    for provider in providers:
        found.extend(provider.discover(topic, min(policy.limit * 3, 100)))
    return rank_candidates(found, topic, policy)


def _schedule_state_path(config_path: Path) -> Path:
    return config_path.with_suffix(config_path.suffix + ".state.json")


def run_schedule(config_path: Path, *, force: bool = False, now: datetime | None = None) -> dict[str, Any]:
    config_path = Path(config_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    state_path = _schedule_state_path(config_path)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    current = now or datetime.now(timezone.utc)
    last = _parse_date(state.get("last_run"))
    interval = max(5, int(config.get("interval_minutes", 1440)))
    if not force and last and current - last < timedelta(minutes=interval):
        return {"status": "not_due", "last_run": state.get("last_run")}
    lock = config_path.with_suffix(config_path.suffix + ".lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError as exc:
        raise RuntimeError(f"schedule already running: {lock}") from exc
    summary = {"status": "completed", "topics": [], "collected": 0, "failed": 0}
    try:
        output_dir = Path(config.get("output_dir", "graphify-out/discovery"))
        target_dir = Path(config.get("target_dir", "raw"))
        for topic_cfg in config.get("topics", []):
            topic = str(topic_cfg["query"] if isinstance(topic_cfg, dict) else topic_cfg)
            raw_policy = dict(config.get("policy", {}))
            policy = DiscoveryPolicy(**raw_policy)
            providers = [PROVIDER_FACTORIES[p["type"]](p) for p in config.get("providers", [])]
            candidates = discover(topic, providers, policy)
            slug = hashlib.sha256(topic.encode("utf-8")).hexdigest()[:12]
            queue = output_dir / f"{slug}.json"
            save_queue(queue, topic, candidates, policy)
            approved = [c for c in candidates if policy.auto_approve_score is not None and c.score >= policy.auto_approve_score]
            if approved:
                results = collect_candidates(queue, approval=",".join(c.id for c in approved[:policy.max_collect]),
                                             target_dir=target_dir, selection_mode="automatic")
                summary["collected"] += sum(c.status == "collected" for c in results)
                summary["failed"] += sum(c.status == "failed" for c in results)
            summary["topics"].append({"topic": topic, "queue": str(queue), "candidates": len(candidates),
                                      "auto_approved": len(approved[:policy.max_collect])})
        state_path.write_text(json.dumps({"last_run": current.isoformat(), "summary": summary}, indent=2) + "\n", encoding="utf-8")
        return summary
    finally:
        lock.unlink(missing_ok=True)


def _providers_from_args(opts: argparse.Namespace) -> list[DiscoveryProvider]:
    providers: list[DiscoveryProvider] = []
    for name in opts.provider:
        if name not in {"arxiv", "crossref"}:
            raise ValueError(f"unknown provider: {name}")
        providers.append(PROVIDER_FACTORIES[name]({}))
    providers.extend(RssProvider(url) for url in opts.feed)
    providers.extend(HostFileProvider(Path(path)) for path in opts.input)
    return providers or [ArxivProvider(), CrossrefProvider()]


def dispatch_cli(command: str, argv: list[str]) -> None:
    if command == "discover":
        p = argparse.ArgumentParser(prog="graphify discover")
        p.add_argument("topic")
        p.add_argument("--provider", action="append", default=[])
        p.add_argument("--feed", action="append", default=[])
        p.add_argument("--input", action="append", default=[])
        p.add_argument("--limit", type=int, default=10)
        p.add_argument("--min-score", type=float, default=0.0)
        p.add_argument("--max-per-domain", type=int, default=3)
        p.add_argument("--out", default="graphify-out/discovery/candidates.json")
        p.add_argument("--json", action="store_true")
        opts = p.parse_args(argv)
        policy = DiscoveryPolicy(limit=opts.limit, min_score=opts.min_score, max_per_domain=opts.max_per_domain)
        candidates = discover(opts.topic, _providers_from_args(opts), policy)
        out = save_queue(Path(opts.out), opts.topic, candidates, policy)
        print(json.dumps([asdict(c) for c in candidates], indent=2, ensure_ascii=False) if opts.json else render_review(candidates))
        print(f"\nCandidate queue: {out}")
    elif command == "collect":
        p = argparse.ArgumentParser(prog="graphify collect")
        p.add_argument("--candidates", default="graphify-out/discovery/candidates.json")
        group = p.add_mutually_exclusive_group(required=True)
        group.add_argument("--approve")
        group.add_argument("--approve-all", action="store_true")
        p.add_argument("--dir", default="raw")
        p.add_argument("--author")
        p.add_argument("--contributor")
        p.add_argument("--dry-run", action="store_true")
        opts = p.parse_args(argv)
        results = collect_candidates(Path(opts.candidates), approval=opts.approve, approve_all=opts.approve_all,
                                     target_dir=Path(opts.dir), author=opts.author,
                                     contributor=opts.contributor, dry_run=opts.dry_run)
        for c in results:
            if c.status != "pending":
                print(f"{c.id}: {c.status}" + (f" -> {c.local_path}" if c.local_path else "") + (f" ({c.error})" if c.error else ""))
    elif command == "schedule":
        p = argparse.ArgumentParser(prog="graphify schedule")
        sub = p.add_subparsers(dest="action", required=True)
        init = sub.add_parser("init")
        init.add_argument("--config", default="graphify-sources.json")
        run = sub.add_parser("run")
        run.add_argument("--config", default="graphify-sources.json")
        run.add_argument("--force", action="store_true")
        run.add_argument("--loop", action="store_true")
        status = sub.add_parser("status")
        status.add_argument("--config", default="graphify-sources.json")
        opts = p.parse_args(argv)
        config_path = Path(opts.config)
        if opts.action == "init":
            if config_path.exists():
                raise FileExistsError(f"refusing to overwrite {config_path}")
            sample = {"interval_minutes": 1440, "topics": [{"query": "knowledge graph LLM"}],
                      "providers": [{"type": "arxiv"}, {"type": "crossref"}],
                      "target_dir": "raw", "output_dir": "graphify-out/discovery",
                      "policy": {"limit": 10, "min_score": 0.35, "max_per_domain": 3,
                                 "auto_approve_score": None, "max_collect": 5}}
            config_path.write_text(json.dumps(sample, indent=2) + "\n", encoding="utf-8")
            print(f"Created {config_path}; auto_approve_score is disabled until you opt in.")
        elif opts.action == "status":
            state = _schedule_state_path(config_path)
            print(state.read_text(encoding="utf-8") if state.exists() else "No schedule run recorded.")
        else:
            while True:
                print(json.dumps(run_schedule(config_path, force=opts.force), indent=2, ensure_ascii=False))
                if not opts.loop:
                    break
                time.sleep(60)
    else:
        raise ValueError(f"unsupported discovery command: {command}")
