"""Provider-neutral research planning for iterative source discovery."""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping


IDENTIFIER_PATTERNS = {
    "contract": re.compile(r"\bHR\d{6}[A-Z]\d{4}\b", re.IGNORECASE),
    "doi": re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE),
    "frequency": re.compile(r"\b\d+(?:\.\d+)?\s*GHz\b", re.IGNORECASE),
    "percentage": re.compile(r"\b\d+(?:\.\d+)?\s*%"),
}
GOVERNMENT_REQUIRED_ROLES = ["program_source", "solicitation", "award_notice", "publication", "current_status"]


@dataclass
class QuerySpec:
    text: str
    family: str = "seed"
    evidence_role: str = "general"
    priority: int = 0
    parent_candidate_id: str | None = None


@dataclass
class QueryPlan:
    topic: str
    profile: str = "general"
    aliases: list[str] = field(default_factory=list)
    identifiers: dict[str, list[str]] = field(default_factory=dict)
    queries: list[QuerySpec] = field(default_factory=list)
    required_roles: list[str] = field(default_factory=list)
    max_rounds: int = 2

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "QueryPlan":
        return cls(topic=str(value.get("topic") or ""), profile=str(value.get("profile") or "general"),
                   aliases=[str(x) for x in value.get("aliases", [])],
                   identifiers={str(k): [str(x) for x in values] for k, values in dict(value.get("identifiers") or {}).items()},
                   queries=[QuerySpec(**q) for q in value.get("queries", [])],
                   required_roles=[str(x) for x in value.get("required_roles", [])],
                   max_rounds=max(1, min(int(value.get("max_rounds", 2)), 4)))


def detect_profile(topic: str, requested: str = "auto") -> str:
    if requested != "auto":
        return requested
    markers = ("darpa", "arpa", "government program", "政府项目", "科研项目", "solicitation")
    return "government-program" if any(x in topic.casefold() for x in markers) else "general"


def _quoted(text: str) -> str:
    return f'"{text.strip().strip(chr(34))}"'


def build_query_plan(topic: str, profile: str = "auto", *, max_rounds: int = 2) -> QueryPlan:
    topic = topic.strip()
    resolved = detect_profile(topic, profile)
    aliases, required = [topic], []
    queries = [QuerySpec(topic, "seed", "general", 100)]
    if resolved == "government-program":
        required = list(GOVERNMENT_REQUIRED_ROLES)
        agency = "DARPA" if "darpa" in topic.casefold() else ""
        name = re.sub(r"\bDARPA\b", "", topic, flags=re.IGNORECASE).strip() or topic
        aliases.extend(x for x in (agency, name) if x and x.casefold() != topic.casefold())
        queries = [QuerySpec(_quoted(topic), "seed", "program_source", 100),
                   QuerySpec(f'site:darpa.mil {_quoted(name)}', "official", "program_source", 95),
                   QuerySpec(f'site:sam.gov {_quoted(name)}', "official", "solicitation", 90),
                   QuerySpec(f'{_quoted(topic)} solicitation BAA', "official", "solicitation", 85),
                   QuerySpec(f'{_quoted(topic)} publication results', "outcome", "publication", 60)]
    unique, seen = [], set()
    for query in queries:
        if query.text.casefold() not in seen:
            seen.add(query.text.casefold()); unique.append(query)
    return QueryPlan(topic, resolved, aliases, {}, unique, required, max(1, min(max_rounds, 4)))


def tokenize(text: str) -> set[str]:
    normalized = re.sub(r"(?<=[A-Za-z0-9])(?=[\u3400-\u9fff])|(?<=[\u3400-\u9fff])(?=[A-Za-z0-9])", " ", text)
    tokens = {x.casefold() for x in re.findall(r"[A-Za-z0-9][A-Za-z0-9._/-]*", normalized) if len(x) > 1}
    for segment in re.findall(r"[\u3400-\u9fff]+", normalized):
        tokens.add(segment); tokens.update(segment); tokens.update(segment[i:i + 2] for i in range(len(segment) - 1))
    return tokens


def extract_identifiers(text: str) -> dict[str, list[str]]:
    out = {}
    for kind, pattern in IDENTIFIER_PATTERNS.items():
        values = []
        for match in pattern.findall(text):
            value = match.lower() if kind == "doi" else re.sub(r"\s+", " ", match).strip().upper()
            if value not in values: values.append(value)
        if values: out[kind] = values
    return out


def candidate_text(candidate: Any) -> str:
    """Return evidence supplied by the source, excluding the query that found it."""
    return " ".join(str(x) for x in (getattr(candidate, "title", ""), getattr(candidate, "summary", ""),
        getattr(candidate, "author", ""), getattr(candidate, "url", ""),
        getattr(candidate, "metadata", {}) or {}))


def infer_evidence_role(candidate: Any) -> str:
    explicit = getattr(candidate, "evidence_role", "")
    if explicit and explicit != "general": return explicit
    text = candidate_text(candidate).casefold()
    host = (urllib.parse.urlsplit(getattr(candidate, "canonical_url", "")).hostname or "").casefold()
    ids = extract_identifiers(text)
    if getattr(candidate, "provider", "").casefold() in {"arxiv", "crossref"} or getattr(candidate, "source_type", "").casefold() in {"paper", "article", "journal-article"}: return "publication"
    if "sam.gov" in host and ids.get("contract"):
        return "solicitation" if any("S" in value[8:9] for value in ids["contract"]) else "award_notice"
    if any(x in text for x in ("solicitation", "broad agency announcement", " baa ")): return "solicitation"
    if any(x in text for x in ("final report", "program complete", "current status", "transitioned")): return "current_status"
    if "darpa.mil" in host or ("program" in text and "darpa" in text): return "program_source"
    return "general"


def domain_authority(url: str) -> float:
    host = (urllib.parse.urlsplit(url).hostname or "").casefold()
    if host == "darpa.mil" or host.endswith(".darpa.mil") or host == "sam.gov" or host.endswith(".sam.gov"): return 1.0
    if host.endswith(".gov") or host.endswith(".mil"): return 0.95
    if host in {"arxiv.org", "export.arxiv.org", "doi.org"}: return 0.9
    if host.endswith(".edu"): return 0.85
    return 0.55


def merge_identifiers(target: dict[str, list[str]], found: Mapping[str, Iterable[str]]) -> bool:
    changed = False
    for kind, values in found.items():
        bucket = target.setdefault(kind, [])
        for value in values:
            if value not in bucket: bucket.append(value); changed = True
    return changed


def expand_query_plan(plan: QueryPlan, candidates: Iterable[Any]) -> list[QuerySpec]:
    existing, added = {q.text.casefold() for q in plan.queries}, []
    for candidate in candidates:
        found = {str(kind): [str(value) for value in values]
                 for kind, values in (getattr(candidate, "identifiers", {}) or {}).items()}
        merge_identifiers(found, extract_identifiers(candidate_text(candidate)))
        merge_identifiers(plan.identifiers, found)
        for value in found.get("contract", []):
            role = "solicitation" if "S" in value[8:9] else "award_notice"
            for text in (_quoted(value), f'site:sam.gov {_quoted(value)}'):
                if text.casefold() not in existing:
                    query = QuerySpec(text, "identifier", role, 100, getattr(candidate, "id", None))
                    plan.queries.append(query); added.append(query); existing.add(text.casefold())
        author = str(getattr(candidate, "author", "")).strip()
        if author and infer_evidence_role(candidate) == "award_notice":
            text = f'{_quoted(author)} {_quoted(plan.topic)} publication'
            if text.casefold() not in existing:
                query = QuerySpec(text, "performer", "publication", 70, getattr(candidate, "id", None))
                plan.queries.append(query); added.append(query); existing.add(text.casefold())
    return added


def coverage_report(candidates: Iterable[Any], plan: QueryPlan | None) -> dict[str, dict[str, Any]]:
    if plan is None: return {}
    report = {}
    for role in plan.required_roles:
        matches = [getattr(c, "id", "") for c in candidates if infer_evidence_role(c) == role]
        report[role] = {"required": 1, "found": len(matches), "candidate_ids": matches, "gap": not matches}
    return report
