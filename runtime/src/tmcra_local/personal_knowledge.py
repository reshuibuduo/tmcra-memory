"""Evidence-bound personal knowledge pages derived from the Visual Atlas.

The projection is intentionally separate from retrieval memory.  It can be
regenerated or exported without mutating Source, Writer memories, graphs, or
indexes.  Model output is accepted only when every statement cites an existing
Visual Atlas evidence node.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from .actor_provenance import permits_human_preference


PERSONAL_KNOWLEDGE_SCHEMA_VERSION = "tmcra.personal-knowledge.1"
PERSONAL_KNOWLEDGE_DOMAIN_SCHEMA_VERSION = "tmcra.personal-knowledge.domain.1"
PERSONAL_KNOWLEDGE_PROMPT_VERSION = "tmcra-personal-knowledge-agent-v3"
PERSONAL_KNOWLEDGE_MAX_EPISODES_PER_BATCH = 32
PERSONAL_KNOWLEDGE_MAX_PAGES_PER_BATCH = 4
PERSONAL_KNOWLEDGE_MAX_OUTPUT_TOKENS = 6144

PERSONAL_KNOWLEDGE_PAGE_TYPES = frozenset(
    {
        "overview",
        "concept",
        "method",
        "explanation",
        "research_note",
        "profile",
        "preferences",
        "requirements",
        "decisions",
        "milestones",
        "current_state",
        "open_questions",
        "lessons",
        "incident",
        "people",
        "reference",
    }
)
PERSONAL_KNOWLEDGE_COLLECTIONS = frozenset({"learned", "project", "personal"})
PERSONAL_KNOWLEDGE_COLLECTION_PAGE_TYPES = {
    "learned": frozenset(
        {"overview", "concept", "method", "explanation", "research_note", "lessons", "reference"}
    ),
    "project": frozenset(
        {"overview", "requirements", "decisions", "milestones", "current_state", "open_questions", "lessons", "incident", "reference"}
    ),
    "personal": frozenset(
        {"overview", "profile", "preferences", "people", "lessons", "reference"}
    ),
}
PERSONAL_KNOWLEDGE_STATUSES = frozenset(
    {"confirmed", "provisional", "superseded", "open"}
)

PERSONAL_KNOWLEDGE_SYSTEM_PROMPT = """You are TMCRA Personal Knowledge Curator.
Turn one complete, evidence-bound domain batch into durable, user-readable knowledge pages.

Hard rules:
1. Use only the supplied compact identifiers. Never invent or rewrite an ID.
2. Every claim and every section must cite one or more supplied evidence IDs.
3. Separate confirmed facts, provisional hypotheses, superseded information, and open questions.
4. Assistant proposals are not user decisions unless evidence explicitly records acceptance.
5. Preserve contradictions and uncertainty. Never promote suspicion into fact.
6. Prefer durable knowledge over chat narration. Omit greetings, continuations, and filler.
7. Curate three distinct collections when supported by evidence: learned knowledge
   (concepts, methods, explanations, research notes, reusable lessons), project
   knowledge (requirements, decisions, milestones, current state, incidents and
   open questions), and personal context (explicit profile, preferences and people).
   A named product or project's requirement, architecture choice, implementation
   milestone, current state, incident, or open question belongs to project. Use
   learned only for knowledge that remains reusable outside that one project.
8. Assistant explanations may become learned knowledge, but never present them as
   user decisions or independently verified external truth. Preserve actor provenance.
9. Create at most four pages. Each page has at most three claims and two sections.
10. If batch_count is greater than one, curate this batch's distinct chapter;
    only batch_index 1 may create a generic domain overview.
11. Use one allowed_collection, allowed_page_type and allowed_status exactly.
12. Canonical title, description, abstract, claim text, section heading, and
    section body use the dominant evidence language. Every readable object must
    also include a faithful display object for zh and en. Preserve official
    product names, API names, code identifiers, and technical terms when
    translation would make them less precise.
13. Transport actor_role user does not prove human authorship. Unknown, automated,
    app-generated or assistant-only evidence may support attributed provisional
    project/learned notes, never personal preferences or confirmed claims. Only
    explicit human_authored/human_adopted declarations support those classifications;
    declarations are not authentication or independently verified external truth.
14. Return one compact JSON object and no prose.

Return exactly:
{
  "schema_version":"tmcra.personal-knowledge.domain.1",
  "domain_id":"supplied domain id",
  "batch_id":"supplied batch id",
  "title":"readable domain title",
  "description":"grounded domain description",
  "display":{"zh":{"title":"Chinese domain title","description":"Chinese description"},"en":{"title":"English domain title","description":"English description"}},
  "pages":[{
    "collection":"learned, project, or personal",
    "page_type":"allowed type",
    "title":"page title",
    "abstract":"short grounded abstract",
    "display":{"zh":{"title":"Chinese page title","abstract":"Chinese abstract"},"en":{"title":"English page title","abstract":"English abstract"}},
    "claims":[{"text":"grounded statement","status":"allowed status","evidence_ids":["supplied evidence id"],"display":{"zh":{"text":"Chinese statement"},"en":{"text":"English statement"}}}],
    "sections":[{"heading":"section heading","body":"grounded explanation","evidence_ids":["supplied evidence id"],"display":{"zh":{"heading":"Chinese heading","body":"Chinese explanation"},"en":{"heading":"English heading","body":"English explanation"}}}]
  }],
  "excluded_evidence_ids":["supplied evidence id"]
}
"""

PERSONAL_KNOWLEDGE_REPAIR_SYSTEM_PROMPT = """You repair one invalid TMCRA Personal Knowledge domain result.
Return the complete corrected JSON object and no prose.

Hard rules:
1. Resolve the supplied validation_error.
2. Use only compact IDs present in the supplied batch.
3. Every claim and section requires evidence from that same batch.
4. Remove an unsupported statement instead of inventing replacement evidence.
5. Preserve confirmed, provisional, superseded, and open distinctions.
6. Keep collection distinctions and actor provenance intact.
   Named-project requirements, decisions, milestones, and current state belong
   to project; reusable concepts and methods belong to learned.
7. Keep at most four pages, three claims per page, and two sections per page.
8. Return exactly the same JSON shape requested by the original prompt.
"""


class PersonalKnowledgeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: Any, maximum: int = 0) -> str:
    clean = value.strip() if isinstance(value, str) else ""
    if maximum and len(clean) > maximum:
        clean = clean[:maximum].rstrip()
    return clean


def _items(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _dedupe(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}.{hashlib.sha256(value.encode('utf-8')).hexdigest()[:20]}"


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bilingual_display(
    value: Any,
    fields: Mapping[str, int],
    *,
    code: str,
) -> dict[str, dict[str, str]]:
    if not isinstance(value, Mapping) or set(value) != {"zh", "en"}:
        raise PersonalKnowledgeError(code, "display must contain exactly zh and en")
    normalized: dict[str, dict[str, str]] = {}
    for locale in ("zh", "en"):
        localized = value.get(locale)
        if not isinstance(localized, Mapping) or set(localized) != set(fields):
            raise PersonalKnowledgeError(
                code,
                f"display.{locale} must contain exactly {sorted(fields)}",
            )
        rendered = {
            field: _text(localized.get(field), maximum)
            for field, maximum in fields.items()
        }
        if any(not item for item in rendered.values()):
            raise PersonalKnowledgeError(code, f"display.{locale} fields must be non-empty")
        normalized[locale] = rendered
    return normalized


def _atlas_nodes(atlas: Mapping[str, Any]) -> list[dict[str, Any]]:
    if atlas.get("schema_version") != "tmcra.visual-atlas.1":
        raise PersonalKnowledgeError(
            "personal_knowledge_atlas_schema",
            "personal knowledge requires a Visual Atlas v1 projection",
        )
    if atlas.get("full_projection") is not True or atlas.get("truncated") is not False:
        raise PersonalKnowledgeError(
            "personal_knowledge_atlas_incomplete",
            "personal knowledge requires a complete, non-truncated Visual Atlas",
        )
    nodes = [dict(item) for item in _items(atlas.get("nodes"))]
    identifiers = [_text(item.get("id"), 512) for item in nodes]
    if not nodes or any(not item for item in identifiers) or len(identifiers) != len(set(identifiers)):
        raise PersonalKnowledgeError(
            "personal_knowledge_atlas_identity",
            "Visual Atlas nodes must have unique immutable IDs",
        )
    return nodes


def personal_knowledge_source_fingerprint(atlas: Mapping[str, Any]) -> str:
    nodes = _atlas_nodes(atlas)
    return _fingerprint(
        {
            "prompt_version": PERSONAL_KNOWLEDGE_PROMPT_VERSION,
            "scope_name": atlas.get("scope_name"),
            "snapshot_id": atlas.get("snapshot_id"),
            "nodes": [
                {
                    key: node.get(key)
                    for key in (
                        "id",
                        "level",
                        "domain_id",
                        "session_id",
                        "episode_id",
                        "label",
                        "summary",
                        "evidence_ids",
                        "evidence_kind",
                        "memory_id",
                        "source_record_id",
                        "content_sha256",
                        "state",
                        "actor_role",
                        "origin_kind",
                        "capture_method",
                        "provenance_strength",
                    )
                }
                for node in nodes
            ],
        }
    )


def build_personal_knowledge_batches(
    atlas: Mapping[str, Any],
    *,
    max_episodes: int = PERSONAL_KNOWLEDGE_MAX_EPISODES_PER_BATCH,
) -> list[dict[str, Any]]:
    """Build complete domain-local batches without cropping linked evidence."""

    if max_episodes < 1:
        raise ValueError("max_episodes must be positive")
    nodes = _atlas_nodes(atlas)
    node_map = {_text(node.get("id"), 512): node for node in nodes}
    domains = sorted(
        (node for node in nodes if _text(node.get("level"), 32) == "domain"),
        key=lambda item: _text(item.get("id"), 512),
    )
    if not domains:
        raise PersonalKnowledgeError(
            "personal_knowledge_no_domains", "Visual Atlas contains no domains"
        )

    batches: list[dict[str, Any]] = []
    for domain in domains:
        domain_id = _text(domain.get("id"), 512)
        sessions = sorted(
            (
                node
                for node in nodes
                if _text(node.get("level"), 32) == "session"
                and _text(node.get("domain_id"), 512) == domain_id
            ),
            key=lambda item: _text(item.get("id"), 512),
        )
        episodes = sorted(
            (
                node
                for node in nodes
                if _text(node.get("level"), 32) == "episode"
                and _text(node.get("domain_id"), 512) == domain_id
            ),
            key=lambda item: (
                _text(item.get("session_id"), 512),
                int(item.get("first_turn") or 0),
                _text(item.get("id"), 512),
            ),
        )
        chunks = [
            episodes[index : index + max_episodes]
            for index in range(0, len(episodes), max_episodes)
        ] or [[]]
        for batch_index, episode_chunk in enumerate(chunks, start=1):
            episode_ids = [_text(item.get("id"), 512) for item in episode_chunk]
            session_ids = {
                _text(item.get("session_id"), 512)
                for item in episode_chunk
                if _text(item.get("session_id"), 512)
            }
            if not episode_chunk:
                session_ids = {_text(item.get("session_id"), 512) for item in sessions}
            selected_sessions = [
                item
                for item in sessions
                if _text(item.get("session_id"), 512) in session_ids
            ]
            evidence_ids = _dedupe(
                [
                    _text(value, 512)
                    for episode in episode_chunk
                    for value in (
                        episode.get("evidence_ids")
                        if isinstance(episode.get("evidence_ids"), list)
                        else []
                    )
                ]
            )
            selected_evidence = [
                node_map[evidence_id]
                for evidence_id in evidence_ids
                if evidence_id in node_map
                and _text(node_map[evidence_id].get("level"), 32) == "evidence"
            ]
            if set(evidence_ids) != {_text(item.get("id"), 512) for item in selected_evidence}:
                raise PersonalKnowledgeError(
                    "personal_knowledge_evidence_missing",
                    f"domain {domain_id} references evidence outside the Visual Atlas",
                )
            batch_id = _stable_id(
                "knowledge-batch",
                domain_id + "|" + str(batch_index) + "|" + "|".join(episode_ids),
            )
            batch = {
                "schema_version": "tmcra.personal-knowledge.batch.1",
                "scope_name": atlas.get("scope_name"),
                "source_snapshot_id": atlas.get("snapshot_id"),
                "domain_id": domain_id,
                "batch_id": batch_id,
                "batch_index": batch_index,
                "batch_count": len(chunks),
                "complete_episode_batch": True,
                "no_evidence_truncation": True,
                "allowed_page_types": sorted(PERSONAL_KNOWLEDGE_PAGE_TYPES),
                "allowed_collections": sorted(PERSONAL_KNOWLEDGE_COLLECTIONS),
                "collection_page_types": {
                    key: sorted(value)
                    for key, value in PERSONAL_KNOWLEDGE_COLLECTION_PAGE_TYPES.items()
                },
                "allowed_statuses": sorted(PERSONAL_KNOWLEDGE_STATUSES),
                "domain": domain,
                "sessions": selected_sessions,
                "episodes": episode_chunk,
                "evidence": selected_evidence,
                "expected_episode_ids": episode_ids,
                "expected_evidence_ids": evidence_ids,
            }
            batch["source_fingerprint"] = _fingerprint(
                {
                    "prompt_version": PERSONAL_KNOWLEDGE_PROMPT_VERSION,
                    "domain": domain,
                    "sessions": selected_sessions,
                    "episodes": episode_chunk,
                    "evidence": selected_evidence,
                }
            )
            batches.append(batch)
    return batches


def validate_personal_knowledge_batch(
    batch: Mapping[str, Any], result: Mapping[str, Any]
) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        raise PersonalKnowledgeError(
            "personal_knowledge_invalid_result", "domain result must be an object"
        )
    if result.get("schema_version") != PERSONAL_KNOWLEDGE_DOMAIN_SCHEMA_VERSION:
        raise PersonalKnowledgeError(
            "personal_knowledge_schema_mismatch", "unsupported domain result schema"
        )
    for key in ("domain_id", "batch_id"):
        if _text(result.get(key), 512) != _text(batch.get(key), 512):
            raise PersonalKnowledgeError(
                "personal_knowledge_batch_identity", f"{key} does not match the request"
            )
    allowed_evidence = {
        _text(item.get("id"), 512) for item in _items(batch.get("evidence"))
    }
    evidence_by_id = {_text(item.get("id"), 512): item for item in _items(batch.get("evidence"))}

    def require_human_origin(ids: Sequence[str]) -> None:
        if not ids or not all(permits_human_preference(evidence_by_id.get(item, {})) for item in ids):
            raise PersonalKnowledgeError("personal_knowledge_origin_not_human",
                "personal context and confirmed claims require explicit human-origin evidence; role alone is insufficient")

    pages = _items(result.get("pages"))
    if not 1 <= len(pages) <= PERSONAL_KNOWLEDGE_MAX_PAGES_PER_BATCH:
        raise PersonalKnowledgeError(
            "personal_knowledge_page_count",
            f"a domain batch must contain 1-{PERSONAL_KNOWLEDGE_MAX_PAGES_PER_BATCH} pages",
        )

    normalized_pages: list[dict[str, Any]] = []
    for page in pages:
        collection = _text(page.get("collection"), 32)
        page_type = _text(page.get("page_type"), 40)
        title = _text(page.get("title"), 160)
        abstract = _text(page.get("abstract"), 800)
        if (
            collection not in PERSONAL_KNOWLEDGE_COLLECTIONS
            or page_type not in PERSONAL_KNOWLEDGE_PAGE_TYPES
            or page_type not in PERSONAL_KNOWLEDGE_COLLECTION_PAGE_TYPES.get(
                collection, frozenset()
            )
            or not title
            or not abstract
        ):
            raise PersonalKnowledgeError(
                "personal_knowledge_page_invalid",
                "each page needs an allowed collection, type, title, and abstract",
            )
        claims = _items(page.get("claims"))
        sections = _items(page.get("sections"))
        if len(claims) > 3 or len(sections) > 2:
            raise PersonalKnowledgeError(
                "personal_knowledge_page_too_large",
                "a page may contain at most three claims and two sections",
            )
        normalized_claims: list[dict[str, Any]] = []
        normalized_sections: list[dict[str, Any]] = []
        for claim in claims:
            evidence_ids = _dedupe(
                [_text(value, 512) for value in claim.get("evidence_ids", [])]
                if isinstance(claim.get("evidence_ids"), list)
                else []
            )
            status = _text(claim.get("status"), 32)
            text = _text(claim.get("text"), 1200)
            if (
                not text
                or status not in PERSONAL_KNOWLEDGE_STATUSES
                or not evidence_ids
                or not set(evidence_ids).issubset(allowed_evidence)
            ):
                raise PersonalKnowledgeError(
                    "personal_knowledge_claim_invalid",
                    "every claim must be grounded in evidence from its batch",
                )
            if collection == "personal" or status == "confirmed":
                require_human_origin(evidence_ids)
            normalized_claims.append(
                {
                    "text": text,
                    "status": status,
                    "evidence_ids": evidence_ids,
                    "display": _bilingual_display(
                        claim.get("display"),
                        {"text": 1200},
                        code="personal_knowledge_display_invalid",
                    ),
                }
            )
        for section in sections:
            evidence_ids = _dedupe(
                [_text(value, 512) for value in section.get("evidence_ids", [])]
                if isinstance(section.get("evidence_ids"), list)
                else []
            )
            heading = _text(section.get("heading"), 160)
            body = _text(section.get("body"), 2400)
            if (
                not heading
                or not body
                or not evidence_ids
                or not set(evidence_ids).issubset(allowed_evidence)
            ):
                raise PersonalKnowledgeError(
                    "personal_knowledge_section_invalid",
                    "every section must be grounded in evidence from its batch",
                )
            if collection == "personal":
                require_human_origin(evidence_ids)
            normalized_sections.append(
                {
                    "heading": heading,
                    "body": body,
                    "evidence_ids": evidence_ids,
                    "display": _bilingual_display(
                        section.get("display"),
                        {"heading": 160, "body": 2400},
                        code="personal_knowledge_display_invalid",
                    ),
                }
            )
        if not normalized_claims and not normalized_sections:
            raise PersonalKnowledgeError(
                "personal_knowledge_page_empty", "a knowledge page cannot be empty"
            )
        normalized_pages.append(
            {
                "collection": collection,
                "page_type": page_type,
                "title": title,
                "abstract": abstract,
                "display": _bilingual_display(
                    page.get("display"),
                    {"title": 160, "abstract": 800},
                    code="personal_knowledge_display_invalid",
                ),
                "claims": normalized_claims,
                "sections": normalized_sections,
            }
        )

    excluded = _dedupe(
        [_text(value, 512) for value in result.get("excluded_evidence_ids", [])]
        if isinstance(result.get("excluded_evidence_ids"), list)
        else []
    )
    if not set(excluded).issubset(allowed_evidence):
        raise PersonalKnowledgeError(
            "personal_knowledge_excluded_invalid",
            "excluded evidence must belong to the supplied batch",
        )
    return {
        "schema_version": PERSONAL_KNOWLEDGE_DOMAIN_SCHEMA_VERSION,
        "domain_id": _text(batch.get("domain_id"), 512),
        "batch_id": _text(batch.get("batch_id"), 512),
        "source_fingerprint": _text(batch.get("source_fingerprint"), 128),
        "title": _text(result.get("title"), 160)
        or _text(dict(batch.get("domain") or {}).get("label"), 160),
        "description": _text(result.get("description"), 1200)
        or _text(dict(batch.get("domain") or {}).get("summary"), 1200),
        "display": _bilingual_display(
            result.get("display"),
            {"title": 160, "description": 1200},
            code="personal_knowledge_display_invalid",
        ),
        "pages": normalized_pages,
        "excluded_evidence_ids": excluded,
    }


def sanitize_personal_knowledge_grounding(
    batch: Mapping[str, Any], result: Mapping[str, Any]
) -> dict[str, Any]:
    """Remove unsupported citations without manufacturing replacement evidence.

    This is used only after the model's repair pass still violates the evidence
    boundary.  Statements with no surviving in-batch evidence are removed; all
    readable text and valid citations remain unchanged for normal validation.
    """

    allowed = {
        _text(item.get("id"), 512) for item in _items(batch.get("evidence"))
    }
    sanitized = dict(result)
    pages: list[dict[str, Any]] = []
    for page in _items(result.get("pages")):
        normalized_page = dict(page)
        claims: list[dict[str, Any]] = []
        for claim in _items(page.get("claims")):
            evidence_ids = _dedupe(
                [
                    _text(value, 512)
                    for value in claim.get("evidence_ids", [])
                    if _text(value, 512) in allowed
                ]
                if isinstance(claim.get("evidence_ids"), list)
                else []
            )
            if evidence_ids:
                claims.append({**dict(claim), "evidence_ids": evidence_ids})
        sections: list[dict[str, Any]] = []
        for section in _items(page.get("sections")):
            evidence_ids = _dedupe(
                [
                    _text(value, 512)
                    for value in section.get("evidence_ids", [])
                    if _text(value, 512) in allowed
                ]
                if isinstance(section.get("evidence_ids"), list)
                else []
            )
            if evidence_ids:
                sections.append({**dict(section), "evidence_ids": evidence_ids})
        if claims or sections:
            normalized_page["claims"] = claims
            normalized_page["sections"] = sections
            pages.append(normalized_page)
    sanitized["pages"] = pages
    sanitized["excluded_evidence_ids"] = [
        value
        for value in _dedupe(
            [_text(item, 512) for item in result.get("excluded_evidence_ids", [])]
            if isinstance(result.get("excluded_evidence_ids"), list)
            else []
        )
        if value in allowed
    ]
    return sanitized


def build_personal_knowledge_fallback(atlas: Mapping[str, Any]) -> dict[str, Any]:
    nodes = _atlas_nodes(atlas)
    domains = []
    for node in nodes:
        if _text(node.get("level"), 32) != "domain":
            continue
        domain = {
            "domain_id": _text(node.get("id"), 512),
            "title": _text(node.get("label"), 160) or "Knowledge domain",
            "description": _text(node.get("summary"), 1200),
            "page_ids": [],
            "session_count": int(node.get("session_count") or 0),
            "evidence_count": int(node.get("evidence_count") or 0),
        }
        display = node.get("display")
        if isinstance(display, Mapping):
            mapped: dict[str, dict[str, str]] = {}
            for locale in ("zh", "en"):
                localized = display.get(locale)
                if isinstance(localized, Mapping):
                    mapped[locale] = {
                        "title": _text(localized.get("label"), 160),
                        "description": _text(localized.get("summary"), 1200),
                    }
            if set(mapped) == {"zh", "en"} and all(
                all(value.values()) for value in mapped.values()
            ):
                domain["display"] = mapped
        domains.append(domain)
    source_fingerprint = personal_knowledge_source_fingerprint(atlas)
    return {
        "schema_version": PERSONAL_KNOWLEDGE_SCHEMA_VERSION,
        "scope_name": _text(atlas.get("scope_name"), 512),
        "snapshot_id": _stable_id(
            "knowledge-snapshot",
            _text(atlas.get("snapshot_id"), 512) + "|" + source_fingerprint,
        ),
        "source_snapshot_id": _text(atlas.get("snapshot_id"), 512),
        "source_fingerprint": source_fingerprint,
        "view": "personal_knowledge_base",
        "projection_state": "fallback",
        "generated_by": "deterministic-knowledge-catalog",
        "prompt_version": None,
        "model": None,
        "full_projection": True,
        "truncated": False,
        "domains": domains,
        "pages": [],
        "evidence_catalog": {},
        "counts": {
            "domains": len(domains),
            "pages": 0,
            "learned_pages": 0,
            "project_pages": 0,
            "personal_pages": 0,
            "claims": 0,
            "sections": 0,
            "evidence": 0,
        },
    }


def merge_personal_knowledge_batches(
    atlas: Mapping[str, Any],
    batches: Sequence[Mapping[str, Any]],
    results: Sequence[Mapping[str, Any]],
    *,
    model: str,
    agent_call: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    nodes = _atlas_nodes(atlas)
    node_map = {_text(node.get("id"), 512): node for node in nodes}
    batch_map = {_text(batch.get("batch_id"), 512): batch for batch in batches}
    normalized = {
        _text(result.get("batch_id"), 512): validate_personal_knowledge_batch(
            batch_map[_text(result.get("batch_id"), 512)], result
        )
        for result in results
        if _text(result.get("batch_id"), 512) in batch_map
    }
    if set(normalized) != set(batch_map):
        raise PersonalKnowledgeError(
            "personal_knowledge_batch_coverage",
            "knowledge results must cover every exact domain batch once",
        )

    batches_by_domain: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for batch in batches:
        batches_by_domain[_text(batch.get("domain_id"), 512)].append(dict(batch))
    domains: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    cited_evidence: set[str] = set()
    agent_batches: dict[str, dict[str, Any]] = {}
    domain_nodes = {
        _text(node.get("id"), 512): node
        for node in nodes
        if _text(node.get("level"), 32) == "domain"
    }
    for domain_id in sorted(domain_nodes):
        domain_batches = sorted(
            batches_by_domain.get(domain_id, []),
            key=lambda item: int(item.get("batch_index") or 0),
        )
        page_ids: list[str] = []
        titles: list[str] = []
        descriptions: list[str] = []
        displays: list[dict[str, Any]] = []
        domain_evidence: set[str] = set()
        for batch in domain_batches:
            batch_id = _text(batch.get("batch_id"), 512)
            result = normalized[batch_id]
            agent_batches[batch_id] = result
            titles.append(_text(result.get("title"), 160))
            descriptions.append(_text(result.get("description"), 1200))
            if isinstance(result.get("display"), Mapping):
                displays.append(dict(result["display"]))
            for page_index, page in enumerate(_items(result.get("pages")), start=1):
                page_id = _stable_id(
                    "knowledge-page",
                    "|".join(
                        (
                            domain_id,
                            batch_id,
                            str(page_index),
                            _text(page.get("collection"), 32),
                            _text(page.get("page_type"), 40),
                            _text(page.get("title"), 160),
                        )
                    ),
                )
                evidence_ids = _dedupe(
                    [
                        _text(value, 512)
                        for claim in _items(page.get("claims"))
                        for value in claim.get("evidence_ids", [])
                    ]
                    + [
                        _text(value, 512)
                        for section in _items(page.get("sections"))
                        for value in section.get("evidence_ids", [])
                    ]
                )
                cited_evidence.update(evidence_ids)
                domain_evidence.update(evidence_ids)
                pages.append(
                    {
                        "page_id": page_id,
                        "domain_id": domain_id,
                        "batch_id": batch_id,
                        "collection": page.get("collection"),
                        "page_type": page.get("page_type"),
                        "title": page.get("title"),
                        "abstract": page.get("abstract"),
                        "display": dict(page.get("display") or {}),
                        "claims": [dict(item) for item in _items(page.get("claims"))],
                        "sections": [dict(item) for item in _items(page.get("sections"))],
                        "evidence_ids": evidence_ids,
                        "source_fingerprint": batch.get("source_fingerprint"),
                    }
                )
                page_ids.append(page_id)
        domain_node = domain_nodes[domain_id]
        domains.append(
            {
                "domain_id": domain_id,
                "title": next((value for value in titles if value), None)
                or _text(domain_node.get("label"), 160)
                or "Knowledge domain",
                "description": next((value for value in descriptions if value), None)
                or _text(domain_node.get("summary"), 1200),
                "display": next((value for value in displays if value), {}),
                "source_fingerprint": _fingerprint(
                    [batch.get("source_fingerprint") for batch in domain_batches]
                ),
                "page_ids": page_ids,
                "session_count": int(domain_node.get("session_count") or 0),
                "evidence_count": len(domain_evidence),
            }
        )

    evidence_catalog = {
        evidence_id: {
            key: node_map[evidence_id].get(key)
            for key in (
                "id",
                "label",
                "summary",
                "display",
                "evidence_kind",
                "memory_id",
                "source_record_id",
                "source_record_ids",
                "session_ids",
                "episode_ids",
                "turn_index",
                "occurred_at",
                "actor_role",
                "origin_kind",
                "capture_method",
                "provenance_strength",
                "state",
                "confidence",
            )
        }
        for evidence_id in sorted(cited_evidence)
        if evidence_id in node_map
    }
    source_fingerprint = personal_knowledge_source_fingerprint(atlas)
    return {
        "schema_version": PERSONAL_KNOWLEDGE_SCHEMA_VERSION,
        "scope_name": _text(atlas.get("scope_name"), 512),
        "snapshot_id": _stable_id(
            "knowledge-snapshot",
            _text(atlas.get("snapshot_id"), 512) + "|" + source_fingerprint,
        ),
        "source_snapshot_id": _text(atlas.get("snapshot_id"), 512),
        "source_fingerprint": source_fingerprint,
        "view": "personal_knowledge_base",
        "projection_state": "ready",
        "generated_by": "local-personal-knowledge-agent",
        "prompt_version": PERSONAL_KNOWLEDGE_PROMPT_VERSION,
        "model": model,
        "full_projection": True,
        "truncated": False,
        "domains": domains,
        "pages": pages,
        "evidence_catalog": evidence_catalog,
        "counts": {
            "domains": len(domains),
            "pages": len(pages),
            "learned_pages": sum(
                page.get("collection") == "learned" for page in pages
            ),
            "project_pages": sum(
                page.get("collection") == "project" for page in pages
            ),
            "personal_pages": sum(
                page.get("collection") == "personal" for page in pages
            ),
            "claims": sum(len(_items(page.get("claims"))) for page in pages),
            "sections": sum(len(_items(page.get("sections"))) for page in pages),
            "evidence": len(evidence_catalog),
        },
        "agent_batches": agent_batches,
        "agent_call": dict(agent_call or {}),
    }
