"""Full, evidence-bound user visual atlas projections.

The visual atlas is a presentation projection over the committed memory
substrate. It does not replace retrieval, Writer state, Source journals, or
the Session Graph. Structural identity and hierarchy are deterministic; an
agent may only provide readable labels and grounded semantic relations.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from .actor_provenance import origin_metadata

from typing import Any


VISUAL_ATLAS_SCHEMA_VERSION = "tmcra.visual-atlas.1"
VISUAL_ATLAS_PROMPT_VERSION = "tmcra-human-memory-map-agent-v9"
VISUAL_ATLAS_TAXONOMY_PROMPT_VERSION = "tmcra-visual-atlas-taxonomy-v4"
VISUAL_ATLAS_EPISODE_BATCH_PROMPT_VERSION = "tmcra-human-memory-map-batch-v9"
VISUAL_ATLAS_LEVELS = ("domain", "session", "episode", "evidence")
VISUAL_ATLAS_MEMORY_TYPES = frozenset(
    {
        "goal",
        "requirement",
        "decision",
        "action",
        "result",
        "problem",
        "solution",
        "lesson",
        "preference",
        "fact",
        "open_question",
    }
)
VISUAL_ATLAS_SEMANTIC_RELATIONS = frozenset(
    {
        "continues",
        "branches",
        "converges",
        "leads_to",
        "depends_on",
        "resolves",
        "updates",
        "supersedes",
        "contradicts",
        "reinforces",
        "derived_from",
        "applies_to",
        "related",
    }
)
VISUAL_ATLAS_AGENT_RELATIONS = VISUAL_ATLAS_SEMANTIC_RELATIONS - {"continues"}
VISUAL_ATLAS_RELATION_TYPES = frozenset(
    {"contains", "parent", "supports", *VISUAL_ATLAS_SEMANTIC_RELATIONS}
)
VISUAL_ATLAS_PATCH_KEYS = frozenset(
    {"domain_updates", "episode_updates", "memory_updates", "relations"}
)
VISUAL_ATLAS_MAX_RELATIONS_PER_PATCH = 128
VISUAL_ATLAS_MAX_EPISODES_PER_BATCH = 6
VISUAL_ATLAS_MAX_MEMORIES_PER_BATCH = 16
VISUAL_ATLAS_MAX_RELATIONS_PER_BATCH = 12


VISUAL_ATLAS_EPISODE_BATCH_SYSTEM_PROMPT = """You are the TMCRA Human Memory Map Editor.
The primary reader is the person whose memories these are. Turn the supplied
semantic memories into a map they can read and use without knowing TMCRA's
storage model.

Hard rules:
1. expected_episode_ids and expected_memory_evidence_ids are the exact write
   set for this batch. Return one episode_update for every expected_episode_id
   and one memory_update for every expected_memory_evidence_id. A continuation
   shard may have no expected_episode_ids; then episode_updates must be empty.
2. Never add, delete, merge, split, rewrite, or abbreviate an ID. All hierarchy,
   Session, memory, Source, evidence, and coordinate fields are immutable.
3. domain_updates must be an empty list. Return memory_updates only for the
   supplied expected_memory_evidence_ids. Do not update Source evidence,
   coordinates, layout, identity, provenance, or raw Source text.
4. The main graph consists of semantic memory evidence nodes. Episodes, domains,
   Sessions, files, database rows and Source records are navigation/provenance
   metadata, not concepts to show as graph nodes and not relation endpoints.
5. Rewrite every semantic memory for a human reader:
   - The label states the concrete thing remembered, not the conversation action.
   - The summary states what happened or was learned, why it matters, and the
     current result or unresolved state when the evidence provides it.
   - It must stand alone outside the original conversation.
   - Never use raw IDs, filenames, timestamps, message fragments, or labels such
     as Continue, Update, Discussion, Progress, User said, or Agent replied.
   - Do not mention Writer, Source, evidence, node, layer, scope or graph in the
     readable text unless the memory itself is about that technical concept.
   - Keep labels under 12 words and summaries under 70 words. Preserve uncertainty.
   - Memory evidence with actor_role assistant represents work performed or
     reported by the Agent. State the concrete action, result, remaining issue,
     or next step. Never rewrite it as a user preference or user claim.
   - Do not prefix a memory title with User, Assistant, Agent, said, replied,
     identified, reported, or discussed. actor_role is already displayed on a
     separate track; the readable title must name the remembered work itself.
   - If assistant evidence contains completed work followed by a next step, the
     label must preserve the completed result. The summary must preserve both
     the completed result and the still-pending next step. Never reduce a shipped
     or completed result to only its follow-up action.
6. Give each memory exactly one allowed_memory_type. Use goal, requirement,
   decision, action, result, problem, solution, lesson, preference, fact, or
   open_question according to what the person would expect to find later. Add
   zero to four short keywords taken from the memory's subject matter.
7. relations are optional and must connect two supplied semantic memory evidence
   IDs. Create a relation only when reading one memory changes how the other is
   understood or used: a decision leads to a result, a solution resolves a
   problem, new information updates or supersedes an earlier belief, an action
   depends on a requirement, or an experience yields a reusable lesson. Shared
   vocabulary, the same Session, and earlier/later order alone are insufficient.
   Cite both endpoint memory IDs in evidence_ids. Never target Source evidence,
   an episode, a Session, or a domain.
8. The relation direction has meaning. source_id is the grammatical subject and
   target_id is the object of the selected relation:
   - leads_to: source causes or enables target.
   - depends_on: source requires target.
   - resolves: source solves or materially addresses target.
   - updates or supersedes: source is newer knowledge that revises or replaces target.
   - derived_from: source was learned or produced from target.
   - applies_to: source is a rule, preference, or lesson that governs target.
   - reinforces: source provides additional support for target.
   - contradicts: source and target cannot both be accepted without qualification.
   - branches or converges: source starts a distinct path or joins target's path.
   - related: use only for a concrete relation that none of the types above express.
   A user requirement followed by completed Agent work is normally expressed as
   the result resolving the requirement, or as the requirement leading to the
   result. A later verification step does not retroactively become a dependency
   of work that the evidence says is already complete.
9. Every relation needs an Agent-written label and reason. label is a concise,
   natural phrase naming the actual connection between these two memories, such
   as "makes role separation necessary" or "turns this requirement into a
   shipped result". Do not copy an enum name, repeat both node titles, or write a
   vague label such as Related. reason is one grounded sentence explaining why
   the edge exists. The graph must remain understandable from node titles, edge
   labels, and reasons alone.
10. Use no more than max_relations relations and only allowed_relation_types.
   Canonical label/summary fields use the dominant evidence language. Every
   episode_update is an internal navigation summary and must also provide faithful
   Chinese and English display text:
   {"episode_id":"one expected_episode_id","label":"specific human title","summary":"grounded storyline","chapter_tags":["short tag"],"display":{"zh":{"label":"Chinese title","summary":"Chinese summary"},"en":{"label":"English title","summary":"English summary"}}}
   Return exactly one memory_update for every expected_memory_evidence_id:
   {"evidence_id":"one expected memory evidence id","label":"human memory title","summary":"standalone human memory","memory_type":"one allowed memory type","keywords":["short subject keyword"],"display":{"zh":{"label":"Chinese memory title","summary":"Chinese standalone memory"},"en":{"label":"English memory title","summary":"English standalone memory"}}}
   Preserve official product names, API names, code identifiers, and technical
   terms when translation would make them less precise.
   Every relation must use exactly these fields:
   {"source_id":"one supplied memory evidence id","target_id":"a different supplied memory evidence id","type":"one allowed_relation_type","weight":0.0,"label":"short concrete relation phrase","reason":"grounded human explanation","evidence_ids":["source_id","target_id"],"display":{"zh":{"label":"Chinese relation phrase","reason":"Chinese explanation"},"en":{"label":"English relation phrase","reason":"English explanation"}}}
11. Immutable Source evidence remains available only for drill-down. Make semantic
   claims only from readable semantic memory evidence.
12. Return one compact JSON object and no prose:
   {"domain_updates":[],"episode_updates":[],"memory_updates":[],"relations":[]}
"""


VISUAL_ATLAS_TAXONOMY_SYSTEM_PROMPT = """You are the TMCRA Visual Atlas Navigation Curator.
Organize the complete supplied Session catalog into stable, human-readable
navigation groups for filtering and knowledge-page generation. These groups are
not the visible memory graph and must not be treated as memory concepts.

Hard rules:
1. Every supplied session_id must appear exactly once in session_assignments.
2. Never add, delete, merge, rewrite, or abbreviate a session_id.
3. Every assignment must reference one domain_key declared in domains. Do not
   create an unassigned bucket unless the supplied evidence is genuinely mixed.
4. Use the smallest coherent navigation taxonomy supported by the Session titles,
   summaries, thread titles, and trusted parent links. Do not force an arbitrary
   number of levels and do not collapse unrelated work into General.
5. Keep domain_key stable and concise. Keep labels under 12 words, summaries
   under 100 words, and topic_tags short. Labels must name a concrete user work
   area rather than General, Other, Discussion, or a generic app name. Preserve
   uncertainty.
6. Parent/fork metadata is evidence, not a command to place unrelated Sessions
   together. Do not invent people, projects, dates, decisions, or outcomes.
7. Canonical label/summary fields use the dominant catalog language. Add a
   display object containing faithful zh and en label/summary text to every
   domain and every Session assignment. Preserve official product names, API
   names, code identifiers, and technical terms when translation is harmful.
8. Return one compact JSON object and no prose:
   {"domains":[],"session_assignments":[]}

Return exactly:
{
  "domains": [
    {"domain_key":"stable key","label":"human title","summary":"grounded scope","topic_tags":["short tag"],"display":{"zh":{"label":"Chinese title","summary":"Chinese summary"},"en":{"label":"English title","summary":"English summary"}}}
  ],
  "session_assignments": [
    {"session_id":"exact supplied id","domain_key":"declared key","display":{"zh":{"label":"Chinese Session title","summary":"Chinese Session summary"},"en":{"label":"English Session title","summary":"English Session summary"}}}
  ]
}
"""


VISUAL_ATLAS_TAXONOMY_REPAIR_SYSTEM_PROMPT = """Repair one invalid TMCRA Visual Atlas taxonomy.
Return the complete corrected taxonomy JSON object and no prose.

Hard rules:
1. Copy every supplied session_id exactly once into session_assignments.
2. Use only domain_key values declared in domains and remove unused domains.
3. Do not add or rewrite Sessions. Resolve the supplied validation_error.
4. Keep the same concise, evidence-grounded taxonomy requirements as the
   original request.
"""


VISUAL_ATLAS_EPISODE_BATCH_REPAIR_SYSTEM_PROMPT = """Repair one invalid TMCRA Visual Atlas episode batch.
Return the complete corrected patch JSON object and no prose.

Hard rules:
1. Copy every expected_episode_id exactly once into episode_updates.
2. domain_updates must be empty. Copy every expected_memory_evidence_id exactly
   once into memory_updates with label, summary, memory_type, keywords and zh/en
   display. Keep every immutable ID and hierarchy untouched.
3. Relations are optional. Keep only grounded relations whose endpoints and
   evidence_ids are semantic memory evidence IDs in the supplied batch. Every
   accepted relation requires a concrete label and zh/en display.label/reason.
   Remove invalid relations.
4. Return only domain_updates, episode_updates, memory_updates, and relations. Resolve the
   validation_error. A second invalid patch is rejected.
"""


class VisualAtlasError(ValueError):
    """A contract or evidence-binding violation in a visual atlas."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: Any, maximum: int = 0) -> str:
    clean = value.strip() if isinstance(value, str) else ""
    if maximum and len(clean) > maximum:
        return clean[:maximum].rstrip()
    return clean


def _items(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result else default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}.{hashlib.sha256(value.encode('utf-8')).hexdigest()[:20]}"


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _dedupe(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _short(value: Any, maximum: int) -> str:
    clean = " ".join(_text(value).split())
    if len(clean) <= maximum:
        return clean
    return clean[: max(1, maximum - 1)].rstrip() + "..."


def _bilingual_display(
    value: Any,
    fields: Mapping[str, int],
    *,
    code: str,
) -> dict[str, dict[str, str]]:
    """Validate the additive zh/en presentation contract."""

    if not isinstance(value, Mapping) or set(value) != {"zh", "en"}:
        raise VisualAtlasError(code, "display must contain exactly zh and en")
    normalized: dict[str, dict[str, str]] = {}
    for locale in ("zh", "en"):
        localized = value.get(locale)
        if not isinstance(localized, Mapping) or set(localized) != set(fields):
            raise VisualAtlasError(
                code,
                f"display.{locale} must contain exactly {sorted(fields)}",
            )
        rendered = {
            field: _short(localized.get(field), maximum)
            for field, maximum in fields.items()
        }
        if any(not item for item in rendered.values()):
            raise VisualAtlasError(code, f"display.{locale} fields must be non-empty")
        normalized[locale] = rendered
    return normalized


def _attributes(node: Mapping[str, Any]) -> Mapping[str, Any]:
    value = node.get("attributes")
    return value if isinstance(value, Mapping) else {}


def _node_label(node: Mapping[str, Any], fallback: str) -> str:
    return _short(node.get("label") or node.get("summary") or fallback, 96) or fallback


def _source_record_id(node: Mapping[str, Any]) -> str:
    attributes = _attributes(node)
    return _text(
        node.get("source_record_id")
        or attributes.get("source_record_id")
        or (node.get("id") if _text(node.get("layer")).lower() == "source" else ""),
        512,
    )


def _memory_id(node: Mapping[str, Any]) -> str:
    attributes = _attributes(node)
    return _text(node.get("memory_id") or attributes.get("memory_id") or node.get("id"), 512)


def _source_refs(node: Mapping[str, Any]) -> list[str]:
    attributes = _attributes(node)
    values = node.get("source_record_ids") or attributes.get("source_record_ids") or []
    if isinstance(values, str):
        values = [values]
    return _dedupe([_text(value, 512) for value in values if _text(value, 512)])


def _turn_index(node: Mapping[str, Any]) -> int | None:
    value = node.get("turn_index")
    if value is None:
        value = _attributes(node).get("turn_index")
    if value is None:
        return None
    return _integer(value)


def _memory_type(value: Any) -> str:
    kind = _text(value, 40).lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "objective": "goal",
        "task": "action",
        "event": "action",
        "milestone": "result",
        "outcome": "result",
        "issue": "problem",
        "incident": "problem",
        "answer": "solution",
        "method": "solution",
        "insight": "lesson",
        "experience": "lesson",
        "question": "open_question",
        "state": "fact",
        "entity": "fact",
    }
    normalized = aliases.get(kind, kind)
    return normalized if normalized in VISUAL_ATLAS_MEMORY_TYPES else "fact"


def _assistant_source_memory_copy(value: Any) -> tuple[str, str, str]:
    """Create a bounded fallback for an Agent progress memory candidate.

    The local Atlas Agent rewrites this candidate into concise bilingual copy.
    Keeping a deterministic fallback means the assistant's completed work is
    still visible when the annotation worker is temporarily unavailable.
    """

    clean = " ".join(_text(value).split())
    if not clean:
        return "Agent work recorded", "Agent work was recorded in this conversation.", "action"
    summary = _short(clean, 800)
    label = clean.lstrip(" \t#>*-`\u2022")
    for delimiter in ("。", ". ", "！", "! ", "？", "? ", "；", "; "):
        position = label.find(delimiter)
        if 12 <= position < 120:
            label = label[: position + (1 if len(delimiter) == 1 else 0)]
            break
    label = _short(label, 96) or "Agent work recorded"
    lowered = clean.casefold()
    if any(
        marker in lowered
        for marker in (
            "completed",
            "finished",
            "implemented",
            "deployed",
            "fixed",
            "resolved",
            "passed",
            "已完成",
            "完成了",
            "已实现",
            "已部署",
            "已修复",
            "通过测试",
        )
    ):
        memory_type = "result"
    elif any(
        marker in lowered
        for marker in ("next", "todo", "remaining", "下一步", "待处理", "还需要")
    ):
        memory_type = "action"
    else:
        memory_type = "action"
    return label, summary, memory_type


def _domain_key(session: Mapping[str, Any], view: Mapping[str, Any]) -> tuple[str, str]:
    explicit_key = _short(session.get("domain_key") or view.get("domain_key"), 80)
    if explicit_key:
        explicit_label = _short(
            session.get("domain")
            or session.get("domain_label")
            or view.get("domain")
            or view.get("domain_label")
            or explicit_key,
            80,
        )
        return explicit_key.casefold(), explicit_label or explicit_key
    candidates: list[Any] = [
        session.get("domain"),
        session.get("topic"),
        view.get("topic"),
    ]
    for container in (session.get("topic_tags"), view.get("topic_tags")):
        if isinstance(container, Sequence) and not isinstance(container, (str, bytes)):
            candidates.extend(container)
    for candidate in candidates:
        label = _short(candidate, 80)
        if label:
            return label.casefold(), label
    return "general", "General"


def build_visual_atlas_taxonomy_payload(
    sessions: Sequence[Mapping[str, Any]],
    session_views: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the complete Session-only payload used for theme taxonomy."""

    views = session_views if isinstance(session_views, Mapping) else {}
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for session in sessions:
        if not isinstance(session, Mapping):
            continue
        session_id = _text(session.get("session_id"), 512)
        if not session_id or session_id in seen:
            raise VisualAtlasError(
                "visual_atlas_session_identity",
                "Session IDs must be unique and non-empty",
            )
        seen.add(session_id)
        view = views.get(session_id) if isinstance(views.get(session_id), Mapping) else {}
        thread_titles = [
            _short(item.get("title"), 120)
            for item in _items(view.get("threads"))
            if _short(item.get("title"), 120)
        ]
        records.append(
            {
                "session_id": session_id,
                "title": _short(view.get("title") or session.get("title"), 160)
                or f"Session {session_id[:12]}",
                "summary": _short(view.get("summary") or session.get("summary"), 1200),
                "parent_session_id": _text(session.get("parent_session_id"), 512) or None,
                "source_app": _text(session.get("source_app"), 80) or None,
                "status": _text(session.get("status"), 32) or "active",
                "message_count": _integer(session.get("message_count")),
                "thread_titles": thread_titles,
                "existing_topic_tags": _dedupe(
                    [
                        _short(value, 40)
                        for container in (session.get("topic_tags"), view.get("topic_tags"))
                        if isinstance(container, Sequence) and not isinstance(container, (str, bytes))
                        for value in container
                        if _short(value, 40)
                    ]
                )[:12],
            }
        )
    return {
        "schema_version": VISUAL_ATLAS_SCHEMA_VERSION,
        "prompt_version": VISUAL_ATLAS_TAXONOMY_PROMPT_VERSION,
        "complete_session_catalog": True,
        "sessions": records,
        "return_shape": {"domains": [], "session_assignments": []},
    }


def validate_visual_atlas_taxonomy(
    sessions: Sequence[Mapping[str, Any]],
    taxonomy: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate that a taxonomy covers the exact supplied Session set."""

    expected = {
        _text(item.get("session_id"), 512)
        for item in sessions
        if isinstance(item, Mapping) and _text(item.get("session_id"), 512)
    }
    if len(expected) != len([item for item in sessions if isinstance(item, Mapping)]):
        raise VisualAtlasError(
            "visual_atlas_session_identity",
            "Session IDs must be unique and non-empty",
        )
    if not isinstance(taxonomy, Mapping):
        raise VisualAtlasError("visual_atlas_taxonomy_invalid", "taxonomy must be an object")
    unknown = set(taxonomy) - {"domains", "session_assignments"}
    if unknown:
        raise VisualAtlasError(
            "visual_atlas_taxonomy_fields",
            f"unsupported taxonomy fields: {sorted(unknown)}",
        )
    domains: list[dict[str, Any]] = []
    domain_keys: set[str] = set()
    for domain in _items(taxonomy.get("domains")):
        key = _short(domain.get("domain_key"), 80).casefold()
        label = _short(domain.get("label"), 120)
        if not key or not label or key in domain_keys:
            raise VisualAtlasError(
                "visual_atlas_taxonomy_domain",
                "domain_key and label must be non-empty and domain keys must be unique",
            )
        forbidden = set(domain) - {
            "domain_key",
            "label",
            "summary",
            "topic_tags",
            "display",
        }
        if forbidden:
            raise VisualAtlasError(
                "visual_atlas_taxonomy_domain",
                f"unsupported domain fields: {sorted(forbidden)}",
            )
        tags = domain.get("topic_tags")
        if tags is not None and not isinstance(tags, list):
            raise VisualAtlasError("visual_atlas_taxonomy_domain", "topic_tags must be a list")
        domains.append(
            {
                "domain_key": key,
                "label": label,
                "summary": _short(domain.get("summary"), 800),
                "topic_tags": _dedupe([_short(value, 40) for value in (tags or []) if _short(value, 40)])[:12],
                "display": _bilingual_display(
                    domain.get("display"),
                    {"label": 120, "summary": 800},
                    code="visual_atlas_taxonomy_display",
                ),
            }
        )
        domain_keys.add(key)
    if expected and (not domains or len(domains) > len(expected)):
        raise VisualAtlasError(
            "visual_atlas_taxonomy_domain",
            "taxonomy must define between one and the number of supplied Sessions domains",
        )

    assignments: list[dict[str, Any]] = []
    assigned: set[str] = set()
    used_domains: set[str] = set()
    for assignment in _items(taxonomy.get("session_assignments")):
        if set(assignment) - {"session_id", "domain_key", "display"}:
            raise VisualAtlasError(
                "visual_atlas_taxonomy_assignment",
                "session assignment contains unsupported fields",
            )
        session_id = _text(assignment.get("session_id"), 512)
        domain_key = _short(assignment.get("domain_key"), 80).casefold()
        if session_id not in expected or session_id in assigned or domain_key not in domain_keys:
            raise VisualAtlasError(
                "visual_atlas_taxonomy_assignment",
                "every supplied Session must be assigned exactly once to a declared domain",
            )
        assignments.append(
            {
                "session_id": session_id,
                "domain_key": domain_key,
                "display": _bilingual_display(
                    assignment.get("display"),
                    {"label": 160, "summary": 800},
                    code="visual_atlas_taxonomy_display",
                ),
            }
        )
        assigned.add(session_id)
        used_domains.add(domain_key)
    if assigned != expected:
        missing = sorted(expected - assigned)
        raise VisualAtlasError(
            "visual_atlas_taxonomy_incomplete",
            f"taxonomy omitted supplied Sessions: {missing[:5]}",
        )
    if used_domains != domain_keys:
        raise VisualAtlasError(
            "visual_atlas_taxonomy_unused_domain",
            "every declared domain must contain at least one Session",
        )
    return {
        "domains": sorted(domains, key=lambda item: item["domain_key"]),
        "session_assignments": sorted(assignments, key=lambda item: item["session_id"]),
    }


def apply_visual_atlas_taxonomy(
    sessions: Sequence[Mapping[str, Any]],
    taxonomy: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Attach validated semantic domains without changing Session identity."""

    normalized = validate_visual_atlas_taxonomy(sessions, taxonomy)
    domains = {item["domain_key"]: item for item in normalized["domains"]}
    assignments = {
        item["session_id"]: item
        for item in normalized["session_assignments"]
    }
    result: list[dict[str, Any]] = []
    for session in sessions:
        row = dict(session)
        session_id = _text(row.get("session_id"), 512)
        assignment = assignments[session_id]
        domain = domains[assignment["domain_key"]]
        row["domain_key"] = domain["domain_key"]
        row["domain"] = domain["label"]
        row["domain_summary"] = domain["summary"]
        row["domain_topic_tags"] = list(domain["topic_tags"])
        row["domain_display"] = _clone(domain["display"])
        row["display"] = _clone(assignment["display"])
        result.append(row)
    return result


def _episode_key(node: Mapping[str, Any]) -> str:
    attributes = _attributes(node)
    for value in (
        node.get("episode_key"),
        node.get("thread_id"),
        attributes.get("thread_id"),
        node.get("cluster_id"),
        attributes.get("cluster_id"),
        node.get("category"),
        node.get("kind"),
    ):
        clean = _short(value, 80)
        if clean:
            return clean.casefold()
    return "conversation"


def _episode_label(key: str, records: Sequence[Mapping[str, Any]], fallback: str) -> str:
    for record in records:
        label = _node_label(record, "")
        if label:
            return label
    return fallback if fallback else key.replace("_", " ").title()


def _graph_for_session(
    session_id: str,
    view: Mapping[str, Any],
    source_graphs: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    graph = source_graphs.get(session_id)
    if not isinstance(graph, Mapping):
        candidate = view.get("source_graph")
        graph = candidate if isinstance(candidate, Mapping) else view
    page = graph.get("page")
    if isinstance(page, Mapping) and bool(page.get("truncated")):
        raise VisualAtlasError(
            "visual_atlas_incomplete_source_graph",
            f"source graph for Session {session_id} is truncated; a full projection is required",
        )
    return graph


def _identity_fields(node: Mapping[str, Any]) -> dict[str, Any]:
    level = _text(node.get("level"))
    fields: dict[str, Any] = {"level": level}
    if level == "domain":
        fields.update({"domain_id": _text(node.get("domain_id")), "domain_key": _text(node.get("domain_key"))})
    elif level == "session":
        fields.update(
            {
                "session_id": _text(node.get("session_id")),
                "parent_session_id": _text(node.get("parent_session_id")) or None,
                "domain_id": _text(node.get("domain_id")),
            }
        )
    elif level == "episode":
        fields.update(
            {
                "episode_id": _text(node.get("episode_id")),
                "session_id": _text(node.get("session_id")),
                "domain_id": _text(node.get("domain_id")),
            }
        )
    elif level == "evidence":
        fields.update(
            {
                "evidence_kind": _text(node.get("evidence_kind")),
                **origin_metadata(node),
                "memory_id": _text(node.get("memory_id")) or None,
                "source_record_id": _text(node.get("source_record_id")) or None,
                "source_record_ids": sorted(_dedupe([_text(value, 512) for value in node.get("source_record_ids", [])])),
                "session_ids": sorted(_dedupe([_text(value, 512) for value in node.get("session_ids", [])])),
                "episode_ids": sorted(_dedupe([_text(value, 512) for value in node.get("episode_ids", [])])),
                "turn_index": node.get("turn_index"),
                "content_sha256": _text(node.get("content_sha256")) or None,
            }
        )
    return fields


def _node_map(atlas: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        _text(node.get("id")): node
        for node in _items(atlas.get("nodes"))
        if _text(node.get("id"))
    }


def _edge_key(edge: Mapping[str, Any]) -> tuple[str, str, str]:
    return (_text(edge.get("source")), _text(edge.get("target")), _text(edge.get("type")).lower())


def _descendant_evidence(atlas: Mapping[str, Any]) -> dict[str, set[str]]:
    nodes = _node_map(atlas)
    result: dict[str, set[str]] = defaultdict(set)
    for node_id, node in nodes.items():
        if _text(node.get("level")) != "evidence":
            continue
        if _text(node.get("evidence_kind")) == "memory":
            result[node_id].add(node_id)
        for episode_id in node.get("episode_ids", []):
            episode = nodes.get(_text(episode_id))
            if not episode:
                continue
            result[_text(episode_id)].add(node_id)
            domain_id = _text(episode.get("domain_id"))
            if domain_id:
                result[domain_id].add(node_id)
    for edge in _items(atlas.get("edges")):
        source, target, relation = _edge_key(edge)
        if relation == "supports" and source in nodes and target in nodes:
            if _text(nodes[source].get("evidence_kind")) == "memory":
                result[source].add(target)
    return result


def validate_visual_atlas(atlas: Mapping[str, Any]) -> dict[str, Any]:
    """Validate structure, immutable identity, and hierarchy/evidence binding.

    The function returns a JSON-safe copy and raises ``VisualAtlasError`` on
    any violation. It deliberately validates the complete node set rather
    than applying a display-size limit.
    """

    if not isinstance(atlas, Mapping):
        raise VisualAtlasError("visual_atlas_invalid", "visual atlas must be an object")
    if _text(atlas.get("schema_version")) != VISUAL_ATLAS_SCHEMA_VERSION:
        raise VisualAtlasError("visual_atlas_schema_mismatch", "unsupported visual atlas schema")
    nodes_raw = _items(atlas.get("nodes"))
    nodes = _node_map(atlas)
    if len(nodes) != len(nodes_raw):
        raise VisualAtlasError("visual_atlas_duplicate_node", "node IDs must be unique and non-empty")
    if not nodes:
        raise VisualAtlasError("visual_atlas_empty", "visual atlas must contain nodes")
    manifest = atlas.get("identity_manifest")
    if not isinstance(manifest, Mapping) or set(manifest) != set(nodes):
        raise VisualAtlasError("visual_atlas_identity_manifest", "identity manifest must cover every node")
    if atlas.get("full_projection") is not True or atlas.get("truncated") is not False:
        raise VisualAtlasError("visual_atlas_not_full", "visual atlas must be a complete, non-truncated projection")

    by_level: dict[str, list[str]] = {level: [] for level in VISUAL_ATLAS_LEVELS}
    for node_id, node in nodes.items():
        level = _text(node.get("level"))
        if level not in by_level:
            raise VisualAtlasError("visual_atlas_invalid_level", f"unknown node level: {level}")
        by_level[level].append(node_id)
        if not isinstance(manifest[node_id], Mapping) or _identity_fields(node) != dict(manifest[node_id]):
            raise VisualAtlasError(
                "visual_atlas_immutable_identity",
                f"immutable identity changed for node {node_id}",
            )
        if not bool(node.get("immutable", level == "evidence")) and level == "evidence":
            raise VisualAtlasError("visual_atlas_evidence_mutable", f"evidence node {node_id} is not immutable")

    if list(atlas.get("levels") or []) != list(VISUAL_ATLAS_LEVELS):
        raise VisualAtlasError("visual_atlas_levels", "atlas levels must declare the four-level contract")
    addressability = atlas.get("addressability")
    if not isinstance(addressability, Mapping) or not isinstance(addressability.get("by_level"), Mapping):
        raise VisualAtlasError("visual_atlas_addressability", "addressability index is required")
    indexed_by_level = {
        level: sorted(_text(value, 512) for value in addressability["by_level"].get(level, []) if _text(value, 512))
        for level in VISUAL_ATLAS_LEVELS
    }
    if indexed_by_level != {level: sorted(values) for level, values in by_level.items()}:
        raise VisualAtlasError("visual_atlas_addressability", "addressability index does not cover all nodes")
    indexed_nodes = [_text(value, 512) for value in addressability.get("node_ids", [])]
    expected_nodes = [node_id for level in VISUAL_ATLAS_LEVELS for node_id in sorted(by_level[level])]
    if indexed_nodes != expected_nodes:
        raise VisualAtlasError("visual_atlas_addressability", "addressability node order is incomplete or unstable")

    edges = _items(atlas.get("edges"))
    seen_edges: set[tuple[str, str, str]] = set()
    structural_children: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        source, target, relation = _edge_key(edge)
        if source not in nodes or target not in nodes or not source or not target or source == target:
            raise VisualAtlasError("visual_atlas_invalid_edge", "edge endpoints must be existing distinct nodes")
        key = (source, target, relation)
        if key in seen_edges:
            raise VisualAtlasError("visual_atlas_duplicate_edge", "duplicate visual atlas edge")
        seen_edges.add(key)
        if relation not in VISUAL_ATLAS_RELATION_TYPES:
            raise VisualAtlasError("visual_atlas_invalid_relation", f"unsupported relation: {relation}")
        source_level = _text(nodes[source].get("level"))
        target_level = _text(nodes[target].get("level"))
        if relation == "contains":
            if (source_level, target_level) not in {
                ("domain", "session"),
                ("session", "episode"),
                ("episode", "evidence"),
            }:
                raise VisualAtlasError("visual_atlas_invalid_hierarchy", "contains edge crosses invalid levels")
            structural_children[source].append(target)
        elif relation == "parent":
            if source_level != "session" or target_level != "session":
                raise VisualAtlasError("visual_atlas_invalid_parent", "parent edges must connect Sessions")
            if _text(nodes[target].get("parent_session_id")) != _text(nodes[source].get("session_id")):
                raise VisualAtlasError("visual_atlas_parent_mismatch", "parent edge disagrees with Session metadata")
        elif relation == "supports":
            if source_level != "evidence" or target_level != "evidence":
                raise VisualAtlasError("visual_atlas_invalid_support", "supports edges must connect evidence")
            if _text(nodes[source].get("evidence_kind")) != "memory" or _text(nodes[target].get("evidence_kind")) != "source":
                raise VisualAtlasError("visual_atlas_invalid_support", "only memory evidence can support Source evidence")
        else:
            if relation not in VISUAL_ATLAS_SEMANTIC_RELATIONS:
                raise VisualAtlasError("visual_atlas_invalid_relation", f"unsupported semantic relation: {relation}")
            allowed_endpoint_levels = {"domain", "episode", "evidence"}
            if source_level not in allowed_endpoint_levels or target_level not in allowed_endpoint_levels:
                raise VisualAtlasError("visual_atlas_semantic_endpoint", "semantic relation endpoint is not supported")
            if source_level == "evidence" or target_level == "evidence":
                if source_level != target_level or any(
                    _text(nodes[item].get("evidence_kind")) != "memory"
                    for item in (source, target)
                ):
                    raise VisualAtlasError(
                        "visual_atlas_semantic_endpoint",
                        "evidence relations may only connect semantic memory nodes",
                    )
            evidence_ids = [_text(value, 512) for value in edge.get("evidence_ids", [])] if isinstance(edge.get("evidence_ids"), list) else []
            descendants = _descendant_evidence(atlas)
            if not evidence_ids or not set(evidence_ids).issubset(set(nodes) & {item for item in nodes if _text(nodes[item].get("level")) == "evidence"}):
                raise VisualAtlasError("visual_atlas_relation_evidence", "semantic relation lacks known evidence IDs")
            if not set(evidence_ids).issubset(descendants.get(source, set()) | descendants.get(target, set())):
                raise VisualAtlasError("visual_atlas_relation_evidence", "relation evidence is outside its endpoint subtrees")
            if not _short(edge.get("reason"), 240):
                raise VisualAtlasError("visual_atlas_relation_reason", "semantic relation needs a grounded reason")

    for node_id, node in nodes.items():
        level = _text(node.get("level"))
        if level == "session" and not structural_children.get(_text(node.get("domain_id")), []).count(node_id):
            raise VisualAtlasError("visual_atlas_missing_domain", f"Session {node_id} is not attached to its domain")
        if level == "episode" and structural_children.get(node_id, []) is None:
            raise VisualAtlasError("visual_atlas_invalid_episode", f"Episode {node_id} is invalid")
        if level == "evidence" and not any(node_id in children for children in structural_children.values()):
            raise VisualAtlasError("visual_atlas_missing_evidence", f"evidence {node_id} is not attached to an episode")

    for node_id in by_level["session"]:
        node = nodes[node_id]
        if not structural_children.get(_text(node.get("domain_id")), []).count(node_id):
            raise VisualAtlasError("visual_atlas_missing_domain", f"Session {node_id} has no domain edge")
    for node_id in by_level["episode"]:
        if not any(node_id in children for children in structural_children.values()):
            raise VisualAtlasError("visual_atlas_missing_session", f"Episode {node_id} has no Session edge")
    for node_id in by_level["evidence"]:
        if not any(node_id in children for children in structural_children.values()):
            raise VisualAtlasError("visual_atlas_missing_episode", f"evidence {node_id} has no Episode edge")

    counts = atlas.get("counts")
    expected_counts = {
        "nodes": len(nodes),
        "domains": len(by_level["domain"]),
        "sessions": len(by_level["session"]),
        "episodes": len(by_level["episode"]),
        "evidence": len(by_level["evidence"]),
        "edges": len(edges),
    }
    if not isinstance(counts, Mapping) or any(_integer(counts.get(key), -1) != value for key, value in expected_counts.items()):
        raise VisualAtlasError("visual_atlas_count_mismatch", "atlas counts do not match the full projection")
    return _clone(dict(atlas))


def _build_node(
    *,
    node_id: str,
    level: str,
    label: str,
    summary: str,
    **fields: Any,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "level": level,
        "label": _short(label, 120),
        "summary": _short(summary, 800),
        "immutable": level == "evidence",
        **fields,
    }


def build_visual_atlas(
    scope_name: str,
    sessions: Sequence[Mapping[str, Any]],
    session_views: Mapping[str, Mapping[str, Any]] | None = None,
    source_graphs: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a complete deterministic four-level user-facing visual atlas.

    ``source_graphs`` must contain complete graph pages. No display limit is
    applied. ``session_views`` is used only for existing readable titles and
    thread metadata; it cannot remove raw semantic or Source evidence nodes.
    """

    views = session_views if isinstance(session_views, Mapping) else {}
    graphs = source_graphs if isinstance(source_graphs, Mapping) else {}
    session_rows = [dict(item) for item in sessions if isinstance(item, Mapping)]
    if not session_rows:
        raise VisualAtlasError("visual_atlas_no_sessions", "at least one Session is required")

    domain_records: dict[str, dict[str, Any]] = {}
    session_records: dict[str, dict[str, Any]] = {}
    raw_by_session: dict[str, dict[str, Any]] = {}
    for session in session_rows:
        session_id = _text(session.get("session_id"), 512)
        if not session_id or session_id in session_records:
            raise VisualAtlasError("visual_atlas_session_identity", "Session IDs must be unique and non-empty")
        view = views.get(session_id) if isinstance(views.get(session_id), Mapping) else {}
        domain_key, domain_label = _domain_key(session, view)
        domain_id = _stable_id("domain", domain_key)
        domain_records.setdefault(
            domain_id,
            {
                "domain_id": domain_id,
                "domain_key": domain_key,
                "label": domain_label,
                "summary": _short(session.get("domain_summary"), 800),
                "display": _clone(session.get("domain_display"))
                if isinstance(session.get("domain_display"), Mapping)
                else None,
                "topic_tags": _dedupe(
                    [
                        _short(value, 40)
                        for value in (session.get("domain_topic_tags") or [])
                        if _short(value, 40)
                    ]
                )[:12]
                if isinstance(session.get("domain_topic_tags"), list)
                else [],
                "session_ids": [],
            },
        )
        domain_records[domain_id]["session_ids"].append(session_id)
        graph = _graph_for_session(session_id, view, graphs)
        raw_nodes = [dict(item) for item in _items(graph.get("nodes"))]
        raw_by_session[session_id] = {"graph": graph, "view": view, "nodes": raw_nodes, "domain_id": domain_id}
        title = _short(view.get("title") or session.get("title"), 160) or f"Session {session_id[:12]}"
        summary = _short(view.get("summary") or session.get("summary"), 800) or "Conversation Session"
        session_records[session_id] = {
            "session_id": session_id,
            "domain_id": domain_id,
            "title": title,
            "summary": summary,
            "display": _clone(session.get("display"))
            if isinstance(session.get("display"), Mapping)
            else None,
            "status": _text(session.get("status"), 32) or "active",
            "source_app": _text(session.get("source_app"), 80) or None,
            "native_thread_id": _text(session.get("native_thread_id"), 200) or None,
            "parent_session_id": _text(session.get("parent_session_id"), 512) or None,
            "created_at": session.get("created_at"),
            "updated_at": session.get("last_ingest_at"),
            "message_count": _integer(session.get("message_count")),
            "ingest_request_count": _integer(session.get("ingest_request_count")),
        }

    nodes: list[dict[str, Any]] = []
    for domain_id in sorted(domain_records):
        record = domain_records[domain_id]
        nodes.append(
            _build_node(
                node_id=domain_id,
                level="domain",
                label=record["label"],
                summary=record["summary"]
                or f"Theme galaxy containing {len(record['session_ids'])} conversation Session(s).",
                **({"display": record["display"]} if record.get("display") else {}),
                domain_id=domain_id,
                domain_key=record["domain_key"],
                topic_tags=record["topic_tags"],
                session_count=len(record["session_ids"]),
                episode_count=0,
                evidence_count=0,
                session_ids=sorted(record["session_ids"]),
            )
        )

    for session_id in sorted(session_records):
        record = session_records[session_id]
        nodes.append(
            _build_node(
                node_id="session:" + session_id,
                level="session",
                label=record["title"],
                summary=record["summary"],
                **({"display": record["display"]} if record.get("display") else {}),
                session_id=session_id,
                domain_id=record["domain_id"],
                parent_session_id=record["parent_session_id"],
                status=record["status"],
                source_app=record["source_app"],
                native_thread_id=record["native_thread_id"],
                created_at=record["created_at"],
                updated_at=record["updated_at"],
                message_count=record["message_count"],
                ingest_request_count=record["ingest_request_count"],
                episode_count=0,
                evidence_count=0,
            )
        )

    episode_records: dict[str, dict[str, Any]] = {}
    memory_records: dict[str, dict[str, Any]] = {}
    source_records: dict[str, dict[str, Any]] = {}
    raw_to_memory: dict[tuple[str, str], str] = {}
    source_to_session: dict[str, str] = {}
    for session_id in sorted(raw_by_session):
        record = raw_by_session[session_id]
        semantic_nodes = [node for node in record["nodes"] if _text(node.get("layer")).lower() != "source"]
        source_nodes = [node for node in record["nodes"] if _text(node.get("layer")).lower() == "source"]
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for node in semantic_nodes:
            grouped[_episode_key(node)].append(node)
        if not grouped:
            grouped["conversation"] = []
        session_episode_ids: list[str] = []
        for key in sorted(grouped):
            episode_id = _stable_id("episode", session_id + "|" + key)
            group = grouped[key]
            label = _episode_label(key, group, f"Chapter {len(session_episode_ids) + 1}")
            summary = _short(
                " ".join(_text(node.get("summary") or node.get("label"), 240) for node in group[:3]),
                800,
            ) or "Conversation chapter"
            episode_records[episode_id] = {
                "episode_id": episode_id,
                "session_id": session_id,
                "domain_id": record["domain_id"],
                "key": key,
                "label": label,
                "summary": summary,
                "memory_ids": [],
                "evidence_ids": [],
                "first_turn": min((_turn_index(node) for node in group if _turn_index(node) is not None), default=10**9),
            }
            session_episode_ids.append(episode_id)
            for node in group:
                if not _text(node.get("id"), 512):
                    raise VisualAtlasError(
                        "visual_atlas_memory_identity",
                        f"semantic memory in Session {session_id} has no immutable id",
                    )
                memory_id = _memory_id(node)
                if not memory_id:
                    raise VisualAtlasError(
                        "visual_atlas_memory_identity",
                        f"semantic memory in Session {session_id} has no memory_id",
                    )
                raw_to_memory[(session_id, _text(node.get("id"), 512))] = memory_id
                entry = memory_records.setdefault(
                    memory_id,
                    {
                        "memory_id": memory_id,
                        "label": _node_label(node, "Memory evidence"),
                        "summary": _short(node.get("summary") or node.get("label"), 800),
                        "memory_type": _memory_type(
                            node.get("kind") or _attributes(node).get("kind")
                        ),
                        "layer": _text(node.get("layer"), 40).lower() or None,
                        "source_record_ids": set(),
                        "session_ids": set(),
                        "episode_ids": set(),
                        "turn_index": _turn_index(node),
                        "occurred_at": _text(node.get("occurred_at")) or None,
                        "actor_role": _text(node.get("actor_role") or _attributes(node).get("actor_role"), 40) or None,
                        **origin_metadata({**_attributes(node), **node}),
                        "confidence": _number(node.get("confidence"), 0.75),
                        "salience": _number(node.get("salience"), 0.5),
                        "state": _text(node.get("state"), 40) or "active",
                        "tags": _dedupe(
                            [
                                _short(value, 40)
                                for value in (_attributes(node).get("topic_tags") or [])
                                if _short(value, 40)
                            ]
                        )[:12]
                        if isinstance(_attributes(node).get("topic_tags"), list)
                        else [],
                        "content_sha256": _text(node.get("content_sha256")) or None,
                    },
                )
                entry["source_record_ids"].update(_source_refs(node))
                entry["session_ids"].add(session_id)
                entry["episode_ids"].add(episode_id)
                episode_records[episode_id]["memory_ids"].append(memory_id)
        for node in source_nodes:
            source_id = _source_record_id(node)
            if not source_id:
                raise VisualAtlasError("visual_atlas_source_identity", f"Source in Session {session_id} has no source_record_id")
            if source_id in source_records:
                previous = source_records[source_id]
                if _text(previous.get("content_sha256")) != _text(node.get("content_sha256")):
                    raise VisualAtlasError("visual_atlas_source_collision", f"Source identity {source_id} has conflicting content")
            source_records.setdefault(
                source_id,
                {
                    "source_record_id": source_id,
                    "label": _node_label(node, "Source evidence"),
                    "summary": _short(node.get("summary") or node.get("label"), 800),
                    "session_ids": set(),
                    "episode_ids": set(),
                    "turn_index": _turn_index(node),
                    "occurred_at": _text(node.get("occurred_at")) or None,
                    "actor_role": _text(node.get("actor_role") or _attributes(node).get("actor_role"), 40) or None,
                        **origin_metadata({**_attributes(node), **node}),
                    "confidence": _number(node.get("confidence"), 1.0),
                    "salience": _number(node.get("salience"), 0.35),
                    "state": _text(node.get("state"), 40) or "committed",
                    "content_sha256": _text(node.get("content_sha256")) or None,
                    "_source_text": _text(node.get("_source_text")),
                },
            )
            source_records[source_id]["session_ids"].add(session_id)
            source_to_session[source_id] = session_id

        for episode_id in session_episode_ids:
            episode = episode_records[episode_id]
            for memory_id in episode["memory_ids"]:
                memory_records[memory_id]["episode_ids"].add(episode_id)
        for source_id, source in source_records.items():
            if source_to_session.get(source_id) != session_id:
                continue
            linked_memory_ids = [
                memory_id
                for memory_id in memory_records
                if session_id in memory_records[memory_id]["session_ids"]
                and source_id in memory_records[memory_id]["source_record_ids"]
            ]
            linked_episodes = sorted(
                {
                    episode_id
                    for memory_id in linked_memory_ids
                    for episode_id in memory_records[memory_id]["episode_ids"]
                }
            )
            if not linked_episodes:
                candidates = [
                    (abs((_turn_index(raw) or 0) - (source["turn_index"] or 0)), episode_id)
                    for episode_id in session_episode_ids
                    for raw in grouped.get(episode_records[episode_id]["key"], [])
                ]
                linked_episodes = [min(candidates)[1]] if candidates else session_episode_ids[:1]
            for episode_id in linked_episodes[:1]:
                source["episode_ids"].add(episode_id)
                episode_records[episode_id]["evidence_ids"].append(source_id)
                if (
                    not linked_memory_ids
                    and source.get("actor_role") == "assistant"
                    and source.get("_source_text")
                ):
                    label, summary, memory_type = _assistant_source_memory_copy(
                        source["_source_text"]
                    )
                    memory_id = _stable_id("memory.agent-source", source_id)
                    memory_records[memory_id] = {
                        "memory_id": memory_id,
                        "label": label,
                        "summary": summary,
                        "memory_type": memory_type,
                        "layer": "source-derived",
                        "source_record_ids": {source_id},
                        "session_ids": {session_id},
                        "episode_ids": {episode_id},
                        "turn_index": source["turn_index"],
                        "occurred_at": source["occurred_at"],
                        "actor_role": "assistant",
                        **origin_metadata(source),
                        "confidence": min(0.85, source["confidence"]),
                        "salience": max(0.55, source["salience"]),
                        "state": "active",
                        "tags": [],
                        "content_sha256": source["content_sha256"],
                    }
                    episode_records[episode_id]["memory_ids"].append(memory_id)

    for memory in memory_records.values():
        references = list(memory["source_record_ids"])
        originals = [origin_metadata(source_records.get(identifier, {})) for identifier in references]
        if not originals:
            originals = [origin_metadata({})]
        for field in ("origin_kind", "capture_method", "provenance_strength"):
            values = {item[field] for item in originals}
            memory[field] = next(iter(values)) if len(values) == 1 else ("legacy_role_only" if field == "capture_method" else "unknown")
        if memory["origin_kind"] == "unknown":
            memory["provenance_strength"] = "unknown"

    for memory_id in sorted(memory_records):
        entry = memory_records[memory_id]
        evidence_id = _stable_id("evidence.memory", memory_id)
        entry["evidence_id"] = evidence_id
        for episode_id in sorted(entry["episode_ids"]):
            episode_records[episode_id]["evidence_ids"].append(evidence_id)
        nodes.append(
            _build_node(
                node_id=evidence_id,
                level="evidence",
                label=entry["label"],
                summary=entry["summary"] or "Semantic memory evidence",
                evidence_kind="memory",
                memory_id=memory_id,
                source_record_id=None,
                source_record_ids=sorted(entry["source_record_ids"]),
                session_ids=sorted(entry["session_ids"]),
                episode_ids=sorted(entry["episode_ids"]),
                turn_index=entry["turn_index"],
                occurred_at=entry["occurred_at"],
                actor_role=entry["actor_role"],
                **origin_metadata(entry),
                confidence=entry["confidence"],
                salience=entry["salience"],
                state=entry["state"],
                tags=entry["tags"],
                memory_type=entry["memory_type"],
                layer=entry["layer"],
                content_sha256=entry["content_sha256"],
            )
        )
    for source_id in sorted(source_records):
        entry = source_records[source_id]
        evidence_id = _stable_id("evidence.source", source_id)
        entry["evidence_id"] = evidence_id
        nodes.append(
            _build_node(
                node_id=evidence_id,
                level="evidence",
                label=entry["label"],
                summary=entry["summary"] or "Immutable Source evidence",
                evidence_kind="source",
                memory_id=None,
                source_record_id=source_id,
                source_record_ids=[],
                session_ids=sorted(entry["session_ids"]),
                episode_ids=sorted(entry["episode_ids"]),
                turn_index=entry["turn_index"],
                occurred_at=entry["occurred_at"],
                actor_role=entry["actor_role"],
                **origin_metadata(entry),
                confidence=entry["confidence"],
                salience=entry["salience"],
                state=entry["state"],
                tags=[],
                content_sha256=entry["content_sha256"],
            )
        )

    for episode in episode_records.values():
        episode["evidence_ids"] = _dedupe(
            [
                source_records[item]["evidence_id"] if item in source_records else item
                for item in episode["evidence_ids"]
            ]
        )

    for episode_id in sorted(episode_records):
        episode = episode_records[episode_id]
        nodes.append(
            _build_node(
                node_id=episode_id,
                level="episode",
                label=episode["label"],
                summary=episode["summary"],
                episode_id=episode_id,
                session_id=episode["session_id"],
                domain_id=episode["domain_id"],
                episode_key=episode["key"],
                memory_count=len(set(episode["memory_ids"])),
                evidence_count=len(episode["evidence_ids"]),
                evidence_ids=episode["evidence_ids"],
                first_turn=episode["first_turn"] if episode["first_turn"] != 10**9 else None,
                last_turn=max(
                    (
                        memory_records[memory_id]["turn_index"]
                        for memory_id in episode["memory_ids"]
                        if memory_records[memory_id]["turn_index"] is not None
                    ),
                    default=None,
                ),
            )
        )

    node_map = {node["id"]: node for node in nodes}
    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add_edge(source: str, target: str, relation: str, **fields: Any) -> None:
        key = (source, target, relation)
        if source not in node_map or target not in node_map or source == target or key in seen:
            return
        seen.add(key)
        edges.append({"id": _stable_id("visual-edge", "|".join(key)), "source": source, "target": target, "type": relation, **fields})

    for domain_id, record in domain_records.items():
        for session_id in sorted(record["session_ids"]):
            add_edge(domain_id, "session:" + session_id, "contains", origin="deterministic_hierarchy")
    for episode_id, episode in episode_records.items():
        add_edge("session:" + episode["session_id"], episode_id, "contains", origin="deterministic_hierarchy")
        for evidence_key in episode["evidence_ids"]:
            evidence_id = evidence_key if evidence_key in node_map else source_records.get(evidence_key, {}).get("evidence_id")
            if evidence_id:
                add_edge(episode_id, evidence_id, "contains", origin="deterministic_hierarchy")
    for source_id, source in source_records.items():
        source_evidence_id = source["evidence_id"]
        for memory_id, memory in memory_records.items():
            if source_id in memory["source_record_ids"]:
                add_edge(memory["evidence_id"], source_evidence_id, "supports", origin="deterministic_source_binding")
    for session_id in sorted(session_records):
        parent = session_records[session_id]["parent_session_id"]
        if parent and parent in session_records:
            add_edge("session:" + parent, "session:" + session_id, "parent", origin="trusted_session_metadata")
    for session_id in sorted(session_records):
        ordered = sorted(
            [episode for episode in episode_records.values() if episode["session_id"] == session_id],
            key=lambda item: (item["first_turn"], item["episode_id"]),
        )
        for previous, current in zip(ordered, ordered[1:]):
            evidence_ids = _dedupe(
                list(previous["evidence_ids"][:2]) + list(current["evidence_ids"][:2])
            )
            if evidence_ids:
                add_edge(
                    previous["episode_id"],
                    current["episode_id"],
                    "continues",
                    weight=0.72,
                    evidence_ids=evidence_ids,
                    reason="Deterministic episode chronology from turn order.",
                    origin="deterministic_chronology",
                )

    for node in nodes:
        if node["level"] == "domain":
            node["episode_count"] = sum(episode["domain_id"] == node["domain_id"] for episode in episode_records.values())
            node["evidence_count"] = sum(
                bool(node["domain_id"] == episode["domain_id"])
                for episode in episode_records.values()
                for _ in episode["evidence_ids"]
            )
        elif node["level"] == "session":
            node["episode_count"] = sum(episode["session_id"] == node["session_id"] for episode in episode_records.values())
            node["evidence_count"] = sum(
                len(episode["evidence_ids"])
                for episode in episode_records.values()
                if episode["session_id"] == node["session_id"]
            )

    manifest = {node["id"]: _identity_fields(node) for node in nodes}
    levels = {level: sorted(node_id for node_id, node in node_map.items() if node["level"] == level) for level in VISUAL_ATLAS_LEVELS}
    snapshot_ids = sorted(
        {
            _text(record["graph"].get("snapshot_id"), 512)
            for record in raw_by_session.values()
            if _text(record["graph"].get("snapshot_id"), 512)
        }
    )
    snapshot_id = (
        snapshot_ids[0]
        if len(snapshot_ids) == 1
        else _stable_id("visual-snapshot", "|".join(snapshot_ids) or _text(scope_name, 512))
    )
    atlas = {
        "schema_version": VISUAL_ATLAS_SCHEMA_VERSION,
        "prompt_version": None,
        "model": None,
        "scope_name": _text(scope_name, 512),
        "snapshot_id": snapshot_id,
        "view": "visual_atlas",
        "projection_state": "fallback",
        "generated_by": "deterministic-visual-atlas-fallback",
        "full_projection": True,
        "truncated": False,
        "levels": list(VISUAL_ATLAS_LEVELS),
        "nodes": sorted(nodes, key=lambda node: (VISUAL_ATLAS_LEVELS.index(node["level"]), node["id"])),
        "edges": edges,
        "identity_manifest": manifest,
        "addressability": {"by_level": levels, "node_ids": [node_id for level in VISUAL_ATLAS_LEVELS for node_id in levels[level]]},
        "counts": {
            "nodes": len(nodes),
            "domains": len(levels["domain"]),
            "sessions": len(levels["session"]),
            "episodes": len(levels["episode"]),
            "evidence": len(levels["evidence"]),
            "edges": len(edges),
        },
    }
    return validate_visual_atlas(atlas)


def build_visual_atlas_episode_batches(
    atlas: Mapping[str, Any],
    *,
    max_episodes: int = VISUAL_ATLAS_MAX_EPISODES_PER_BATCH,
    max_memories: int = VISUAL_ATLAS_MAX_MEMORIES_PER_BATCH,
) -> list[dict[str, Any]]:
    """Partition a full atlas into bounded, domain-local human-memory jobs.

    Domain-local batches let the Agent see related memories from more than one
    Session. Session and episode records remain immutable provenance wrappers;
    only semantic memory nodes may become visible relation endpoints.
    """

    validated = validate_visual_atlas(atlas)
    nodes = _node_map(validated)
    limit = max(1, min(VISUAL_ATLAS_MAX_EPISODES_PER_BATCH, _integer(max_episodes, 1)))
    memory_limit = max(
        1,
        min(
            VISUAL_ATLAS_MAX_MEMORIES_PER_BATCH,
            _integer(max_memories, 1),
        ),
    )
    domains = {
        _text(node.get("domain_id"), 512): node
        for node in nodes.values()
        if node.get("level") == "domain"
    }
    sessions = {
        _text(node.get("session_id"), 512): node
        for node in nodes.values()
        if node.get("level") == "session"
    }
    evidence = [node for node in nodes.values() if node.get("level") == "evidence"]
    evidence_by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in evidence:
        for episode_id in item.get("episode_ids", []):
            clean_episode_id = _text(episode_id, 512)
            if clean_episode_id:
                evidence_by_episode[clean_episode_id].append(item)
    episodes_by_domain: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes.values():
        if node.get("level") == "episode":
            episodes_by_domain[_text(node.get("domain_id"), 512)].append(node)

    batches: list[dict[str, Any]] = []
    covered_episode_ids: set[str] = set()
    covered_memory_evidence_ids: set[str] = set()
    def append_batch(
        domain_id: str,
        domain: Mapping[str, Any],
        batch_episodes: Sequence[Mapping[str, Any]],
        expected_episode_ids: Sequence[str],
        expected_memory_evidence_ids: Sequence[str],
        *,
        shard_index: int,
    ) -> None:
        episode_ids = [_text(value, 512) for value in expected_episode_ids]
        memory_ids = [_text(value, 512) for value in expected_memory_evidence_ids]
        context_episode_ids = [
            _text(item.get("episode_id"), 512) for item in batch_episodes
        ]
        if (
            any(not value for value in context_episode_ids)
            or len(context_episode_ids) != len(set(context_episode_ids))
            or len(episode_ids) != len(set(episode_ids))
            or len(memory_ids) != len(set(memory_ids))
            or (not episode_ids and not memory_ids)
        ):
            raise VisualAtlasError(
                "visual_atlas_batch_identity",
                "visual atlas shards require unique context and write-set IDs",
            )
        episode_id_set = set(episode_ids)
        memory_id_set = set(memory_ids)
        if covered_episode_ids.intersection(episode_id_set):
            raise VisualAtlasError(
                "visual_atlas_batch_overlap",
                "an episode update may appear in only one visual atlas shard",
            )
        if covered_memory_evidence_ids.intersection(memory_id_set):
            raise VisualAtlasError(
                "visual_atlas_batch_memory_overlap",
                "a semantic memory update may appear in only one visual atlas shard",
            )
        covered_episode_ids.update(episode_id_set)
        covered_memory_evidence_ids.update(memory_id_set)

        context_episode_id_set = set(context_episode_ids)
        batch_evidence_by_id: dict[str, dict[str, Any]] = {}
        for memory_id in memory_ids:
            item = nodes.get(memory_id)
            if item is not None and item.get("evidence_kind") == "memory":
                batch_evidence_by_id[memory_id] = item
        for episode_id in context_episode_id_set:
            for item in evidence_by_episode.get(episode_id, []):
                item_id = _text(item.get("id"), 512)
                if item_id and item.get("evidence_kind") != "memory":
                    batch_evidence_by_id[item_id] = item
        batch_evidence = list(batch_evidence_by_id.values())
        evidence_ids = set(batch_evidence_by_id)
        relation_candidate_memory_ids = sorted(memory_id_set)
        batch_session_ids = sorted(
            {
                _text(item.get("session_id"), 512)
                for item in batch_episodes
                if _text(item.get("session_id"), 512)
            }
        )
        selected_sessions = [
            sessions[session_id]
            for session_id in batch_session_ids
            if session_id in sessions
        ]
        if len(selected_sessions) != len(batch_session_ids):
            raise VisualAtlasError(
                "visual_atlas_batch_session",
                f"domain batch {domain_id} references an unknown Session",
            )
        batches.append(
            {
                    "schema_version": VISUAL_ATLAS_SCHEMA_VERSION,
                    "prompt_version": VISUAL_ATLAS_EPISODE_BATCH_PROMPT_VERSION,
                    "batch_id": _stable_id(
                        "visual-batch",
                        "|".join(
                            [
                                domain_id,
                                str(shard_index),
                                *context_episode_ids,
                                *memory_ids,
                            ]
                        ),
                    ),
                    "atlas_full_projection": True,
                    "complete_episode_batch": set(episode_ids)
                    == set(context_episode_ids),
                    "complete_memory_slice": True,
                    "no_evidence_truncation": True,
                    "context_episode_ids": context_episode_ids,
                    "expected_episode_ids": episode_ids,
                    "expected_memory_evidence_ids": memory_ids,
                    "relation_candidate_memory_ids": relation_candidate_memory_ids,
                    "max_relations": min(
                        VISUAL_ATLAS_MAX_RELATIONS_PER_BATCH,
                        max(0, len(relation_candidate_memory_ids) * 2),
                    ),
                    "allowed_relation_types": sorted(VISUAL_ATLAS_AGENT_RELATIONS),
                    "allowed_memory_types": sorted(VISUAL_ATLAS_MEMORY_TYPES),
                    "domain": {
                        key: domain.get(key)
                        for key in ("domain_id", "domain_key", "label", "summary", "display")
                        if domain.get(key) is not None
                    },
                    "sessions": [
                        {
                            key: session.get(key)
                            for key in (
                                "session_id",
                                "domain_id",
                                "parent_session_id",
                                "label",
                                "summary",
                                "status",
                                "message_count",
                                "display",
                            )
                            if session.get(key) is not None
                        }
                        for session in selected_sessions
                    ],
                    "episodes": [
                        {
                            key: item.get(key)
                            for key in (
                                "episode_id",
                                "session_id",
                                "domain_id",
                                "label",
                                "summary",
                                "evidence_ids",
                                "memory_count",
                                "first_turn",
                                "last_turn",
                                "display",
                            )
                            if item.get(key) is not None
                        }
                        for item in batch_episodes
                    ],
                    "evidence": [
                        (
                            {
                                key: item.get(key)
                                for key in (
                                    "id",
                                    "evidence_kind",
                                    "memory_id",
                                    "episode_ids",
                                    "label",
                                    "summary",
                                    "memory_type",
                                    "layer",
                                    "actor_role",
                                    "origin_kind",
                                    "capture_method",
                                    "provenance_strength",
                                    "occurred_at",
                                    "tags",
                                    "display",
                                )
                            }
                            if item.get("evidence_kind") == "memory"
                            else {
                                key: item.get(key)
                                for key in ("id", "evidence_kind", "episode_ids")
                            }
                        )
                        for item in sorted(batch_evidence, key=lambda value: _text(value.get("id"), 512))
                    ],
                    "existing_relations": [
                        {
                            key: (
                                edge.get("source")
                                if key == "source_id"
                                else edge.get("target")
                                if key == "target_id"
                                else edge.get(key)
                            )
                            for key in (
                                "source_id",
                                "target_id",
                                "type",
                                "label",
                                "evidence_ids",
                                "reason",
                                "display",
                            )
                        }
                        for edge in _items(validated.get("edges"))
                        if _text(edge.get("source"), 512) in set(relation_candidate_memory_ids)
                        and _text(edge.get("target"), 512) in set(relation_candidate_memory_ids)
                        and bool(
                            {
                                _text(value, 512)
                                for value in edge.get("evidence_ids", [])
                            }.intersection(evidence_ids)
                        )
                    ],
                    "return_shape": {
                        "domain_updates": [],
                        "episode_updates": [],
                        "memory_updates": [],
                        "relations": [],
                    },
            }
        )

    assigned_memory_ids: set[str] = set()
    for domain_id in sorted(episodes_by_domain):
        domain = domains.get(domain_id)
        if not domain:
            raise VisualAtlasError(
                "visual_atlas_batch_domain",
                f"episode batch has no domain node: {domain_id}",
            )
        ordered = sorted(
            episodes_by_domain[domain_id],
            key=lambda item: (
                _text(item.get("session_id"), 512),
                item.get("first_turn") is None,
                _integer(item.get("first_turn"), 10**9),
                _text(item.get("episode_id"), 512),
            ),
        )
        pending_episodes: list[dict[str, Any]] = []
        pending_memory_ids: list[str] = []
        shard_index = 0

        def flush_pending() -> None:
            nonlocal pending_episodes, pending_memory_ids, shard_index
            if not pending_episodes:
                return
            append_batch(
                domain_id,
                domain,
                pending_episodes,
                [_text(item.get("episode_id"), 512) for item in pending_episodes],
                pending_memory_ids,
                shard_index=shard_index,
            )
            shard_index += 1
            pending_episodes = []
            pending_memory_ids = []

        for episode in ordered:
            episode_id = _text(episode.get("episode_id"), 512)
            episode_memory_ids = sorted(
                {
                    _text(item.get("id"), 512)
                    for item in evidence_by_episode.get(episode_id, [])
                    if item.get("evidence_kind") == "memory"
                    and _text(item.get("id"), 512)
                    and _text(item.get("id"), 512) not in assigned_memory_ids
                }
            )
            assigned_memory_ids.update(episode_memory_ids)
            if len(episode_memory_ids) > memory_limit:
                flush_pending()
                for offset in range(0, len(episode_memory_ids), memory_limit):
                    append_batch(
                        domain_id,
                        domain,
                        [episode],
                        [episode_id] if offset == 0 else [],
                        episode_memory_ids[offset : offset + memory_limit],
                        shard_index=shard_index,
                    )
                    shard_index += 1
                continue
            if pending_episodes and (
                len(pending_episodes) >= limit
                or len(pending_memory_ids) + len(episode_memory_ids) > memory_limit
            ):
                flush_pending()
            pending_episodes.append(dict(episode))
            pending_memory_ids.extend(episode_memory_ids)
        flush_pending()

    expected = {
        _text(node.get("episode_id"), 512)
        for node in nodes.values()
        if node.get("level") == "episode"
    }
    if covered_episode_ids != expected:
        raise VisualAtlasError(
            "visual_atlas_batch_coverage",
            "episode batches do not cover the exact full atlas episode set",
        )
    expected_memory_evidence = {
        _text(node.get("id"), 512)
        for node in nodes.values()
        if node.get("level") == "evidence" and node.get("evidence_kind") == "memory"
    }
    if covered_memory_evidence_ids != expected_memory_evidence:
        raise VisualAtlasError(
            "visual_atlas_batch_memory_coverage",
            "episode batches do not cover the exact semantic memory evidence set",
        )
    return batches


def validate_visual_atlas_patch(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an Agent patch without allowing identity or structure edits."""

    validated = validate_visual_atlas(base)
    if not isinstance(patch, Mapping):
        raise VisualAtlasError("visual_atlas_patch_invalid", "patch must be an object")
    unknown = set(patch) - set(VISUAL_ATLAS_PATCH_KEYS)
    if unknown:
        raise VisualAtlasError("visual_atlas_patch_fields", f"unsupported patch fields: {sorted(unknown)}")
    nodes = _node_map(validated)
    allowed_update_fields = {
        "domain_updates": {"domain_id", "label", "summary", "topic_tags", "display"},
        "episode_updates": {"episode_id", "label", "summary", "chapter_tags", "display"},
    }
    normalized: dict[str, Any] = {
        "domain_updates": [],
        "episode_updates": [],
        "memory_updates": [],
        "relations": [],
    }
    for update_key, level in (("domain_updates", "domain"), ("episode_updates", "episode")):
        seen: set[str] = set()
        raw_updates = patch.get(update_key)
        if raw_updates is None:
            raw_updates = []
        if not isinstance(raw_updates, Sequence) or isinstance(raw_updates, (str, bytes)):
            raise VisualAtlasError("visual_atlas_patch_fields", f"{update_key} must be a list")
        if any(not isinstance(item, Mapping) for item in raw_updates):
            raise VisualAtlasError("visual_atlas_patch_fields", f"{update_key} items must be objects")
        for update in raw_updates:
            identifier_key = "domain_id" if level == "domain" else "episode_id"
            identifier = _text(update.get(identifier_key), 512)
            if not identifier or identifier in seen:
                raise VisualAtlasError("visual_atlas_patch_identity", f"duplicate or missing {identifier_key}")
            node = nodes.get(identifier)
            if not node or _text(node.get("level")) != level:
                raise VisualAtlasError("visual_atlas_patch_identity", f"unknown {level} node: {identifier}")
            forbidden = set(update) - allowed_update_fields[update_key]
            if forbidden:
                raise VisualAtlasError("visual_atlas_patch_immutable", f"immutable or unsupported fields: {sorted(forbidden)}")
            item: dict[str, Any] = {identifier_key: identifier}
            label = _short(update.get("label"), 120)
            summary = _short(update.get("summary"), 800)
            if not label and not summary and not isinstance(update.get("topic_tags" if level == "domain" else "chapter_tags"), list):
                raise VisualAtlasError("visual_atlas_patch_empty", f"{level} update has no readable fields")
            if label:
                item["label"] = label
            if summary:
                item["summary"] = summary
            if "display" in update:
                item["display"] = _bilingual_display(
                    update.get("display"),
                    {"label": 120 if level == "domain" else 120, "summary": 800},
                    code="visual_atlas_patch_display",
                )
            tag_key = "topic_tags" if level == "domain" else "chapter_tags"
            if tag_key in update:
                tags = update[tag_key]
                if not isinstance(tags, list):
                    raise VisualAtlasError("visual_atlas_patch_tags", f"{tag_key} must be a list")
                item[tag_key] = _dedupe([_short(tag, 40) for tag in tags if _short(tag, 40)])[:8]
            normalized[update_key].append(item)
            seen.add(identifier)

    raw_memory_updates = patch.get("memory_updates")
    if raw_memory_updates is None:
        raw_memory_updates = []
    if not isinstance(raw_memory_updates, Sequence) or isinstance(
        raw_memory_updates, (str, bytes)
    ):
        raise VisualAtlasError(
            "visual_atlas_patch_fields", "memory_updates must be a list"
        )
    seen_memory_ids: set[str] = set()
    for update in raw_memory_updates:
        allowed_memory_fields = {
            "evidence_id",
            "label",
            "summary",
            "memory_type",
            "keywords",
            "display",
        }
        if not isinstance(update, Mapping) or set(update) - allowed_memory_fields:
            raise VisualAtlasError(
                "visual_atlas_patch_immutable",
                "memory updates contain immutable or unsupported fields",
            )
        evidence_id = _text(update.get("evidence_id"), 512)
        node = nodes.get(evidence_id)
        if (
            not evidence_id
            or evidence_id in seen_memory_ids
            or not node
            or node.get("level") != "evidence"
            or node.get("evidence_kind") != "memory"
        ):
            raise VisualAtlasError(
                "visual_atlas_patch_identity",
                "memory_updates must target unique semantic memory evidence nodes",
            )
        label = _short(update.get("label"), 120)
        summary = _short(update.get("summary"), 800)
        memory_type = _text(update.get("memory_type"), 40).lower()
        keywords = update.get("keywords")
        if not label or not summary or memory_type not in VISUAL_ATLAS_MEMORY_TYPES:
            raise VisualAtlasError(
                "visual_atlas_patch_memory_readability",
                "memory updates require a readable label, summary, and allowed memory_type",
            )
        if not isinstance(keywords, list):
            raise VisualAtlasError(
                "visual_atlas_patch_memory_keywords", "memory keywords must be a list"
            )
        normalized["memory_updates"].append(
            {
                "evidence_id": evidence_id,
                "label": label,
                "summary": summary,
                "memory_type": memory_type,
                "keywords": _dedupe(
                    [_short(value, 40) for value in keywords if _short(value, 40)]
                )[:4],
                "display": _bilingual_display(
                    update.get("display"),
                    {"label": 120, "summary": 800},
                    code="visual_atlas_patch_display",
                ),
            }
        )
        seen_memory_ids.add(evidence_id)

    descendants = _descendant_evidence(validated)
    existing = {_edge_key(edge) for edge in _items(validated.get("edges"))}
    relations = patch.get("relations")
    if not isinstance(relations, Sequence) or isinstance(relations, (str, bytes)):
        raise VisualAtlasError("visual_atlas_patch_relations", "relations must be a list")
    if len(relations) > VISUAL_ATLAS_MAX_RELATIONS_PER_PATCH:
        raise VisualAtlasError("visual_atlas_patch_relations", "too many relations in one patch")
    if any(not isinstance(item, Mapping) for item in relations):
        raise VisualAtlasError("visual_atlas_patch_relations", "relation items must be objects")
    for relation in _items(relations):
        source = _text(relation.get("source_id"), 512)
        target = _text(relation.get("target_id"), 512)
        relation_type = _text(relation.get("type"), 40).lower()
        if source not in nodes or target not in nodes or source == target:
            raise VisualAtlasError("visual_atlas_patch_relation", "relation endpoints must be existing distinct nodes")
        source_level = _text(nodes[source].get("level"))
        target_level = _text(nodes[target].get("level"))
        if source_level == "evidence" or target_level == "evidence":
            if source_level != target_level or any(
                _text(nodes[item].get("evidence_kind")) != "memory"
                for item in (source, target)
            ):
                raise VisualAtlasError(
                    "visual_atlas_patch_relation",
                    "evidence relations may only connect semantic memory nodes",
                )
        elif source_level not in {"domain", "episode"} or target_level not in {
            "domain",
            "episode",
        }:
            raise VisualAtlasError(
                "visual_atlas_patch_relation", "unsupported semantic relation endpoints"
            )
        if relation_type not in VISUAL_ATLAS_SEMANTIC_RELATIONS:
            raise VisualAtlasError("visual_atlas_patch_relation", f"unsupported semantic relation: {relation_type}")
        key = (source, target, relation_type)
        if key in existing:
            raise VisualAtlasError("visual_atlas_patch_relation", "relation already exists")
        evidence_ids = _dedupe([_text(value, 512) for value in relation.get("evidence_ids", [])]) if isinstance(relation.get("evidence_ids"), list) else []
        if not evidence_ids or not set(evidence_ids).issubset(set(descendants.get(source, set())) | set(descendants.get(target, set()))):
            raise VisualAtlasError("visual_atlas_patch_evidence", "relation evidence is not bound to its endpoints")
        if source_level == "evidence" and not {source, target}.issubset(
            set(evidence_ids)
        ):
            raise VisualAtlasError(
                "visual_atlas_patch_evidence",
                "memory relations must cite both endpoint memories",
            )
        label = _short(relation.get("label"), 80)
        reason = _short(relation.get("reason"), 240)
        if not reason:
            raise VisualAtlasError("visual_atlas_patch_reason", "relation requires a grounded reason")
        item = {
            "source_id": source,
            "target_id": target,
            "type": relation_type,
            "weight": max(0.0, min(1.0, _number(relation.get("weight"), 0.55))),
            "reason": reason,
            "evidence_ids": evidence_ids,
        }
        if label:
            item["label"] = label
        if "display" in relation:
            item["display"] = _bilingual_display(
                relation.get("display"),
                ({"label": 80, "reason": 240} if label else {"reason": 240}),
                code="visual_atlas_patch_relation_display",
            )
        normalized["relations"].append(item)
        existing.add(key)
    return normalized


def validate_visual_atlas_episode_batch_patch(
    base: Mapping[str, Any],
    batch: Mapping[str, Any],
    patch: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate one bounded episode patch and require exact batch coverage."""

    normalized = validate_visual_atlas_patch(base, patch)
    expected = [_text(value, 512) for value in batch.get("expected_episode_ids", [])]
    expected_memory = [
        _text(value, 512)
        for value in batch.get("expected_memory_evidence_ids", [])
    ]
    if (
        len(expected) != len(set(expected))
        or any(not value for value in expected)
        or (not expected and not expected_memory)
    ):
        raise VisualAtlasError(
            "visual_atlas_batch_identity",
            "a visual atlas shard requires a unique episode or memory write set",
        )
    if normalized["domain_updates"]:
        raise VisualAtlasError(
            "visual_atlas_batch_domain_update",
            "episode batches may not update domains",
        )
    returned = [item["episode_id"] for item in normalized["episode_updates"]]
    if set(returned) != set(expected) or len(returned) != len(expected):
        raise VisualAtlasError(
            "visual_atlas_batch_coverage",
            "episode batch must update every expected episode exactly once",
        )
    if any("display" not in item for item in normalized["episode_updates"]):
        raise VisualAtlasError(
            "visual_atlas_batch_display",
            "every episode update requires Chinese and English display text",
        )
    if (
        len(expected_memory) != len(set(expected_memory))
        or any(not value for value in expected_memory)
    ):
        raise VisualAtlasError(
            "visual_atlas_batch_memory_identity",
            "expected_memory_evidence_ids must be a unique list",
        )
    returned_memory = [item["evidence_id"] for item in normalized["memory_updates"]]
    if set(returned_memory) != set(expected_memory) or len(returned_memory) != len(
        expected_memory
    ):
        raise VisualAtlasError(
            "visual_atlas_batch_memory_coverage",
            "episode batch must rewrite every expected semantic memory exactly once",
        )
    maximum = min(
        VISUAL_ATLAS_MAX_RELATIONS_PER_BATCH,
        max(0, _integer(batch.get("max_relations"))),
    )
    if len(normalized["relations"]) > maximum:
        raise VisualAtlasError(
            "visual_atlas_batch_relations",
            f"episode batch returned more than {maximum} relations",
        )
    allowed_evidence = {
        _text(item.get("id"), 512)
        for item in _items(batch.get("evidence"))
        if _text(item.get("id"), 512)
    }
    allowed_relation_types = {
        _text(value, 40).lower()
        for value in batch.get("allowed_relation_types", [])
        if _text(value, 40)
    }
    relation_candidates = {
        _text(value, 512)
        for value in batch.get("relation_candidate_memory_ids", [])
        if _text(value, 512)
    }
    for relation in normalized["relations"]:
        endpoints = {relation["source_id"], relation["target_id"]}
        if not endpoints.issubset(relation_candidates) or not endpoints.intersection(
            set(expected_memory)
        ):
            raise VisualAtlasError(
                "visual_atlas_batch_relation",
                "memory relation endpoints must be supplied candidates and include a rewritten memory",
            )
        if relation["type"] not in allowed_relation_types:
            raise VisualAtlasError(
                "visual_atlas_batch_relation_type",
                "episode batch relation type is not allowed for Agent inference",
            )
        if not relation.get("label"):
            raise VisualAtlasError(
                "visual_atlas_batch_relation_label",
                "every Agent-generated memory relation requires a concrete label",
            )
        if not set(relation["evidence_ids"]).issubset(allowed_evidence):
            raise VisualAtlasError(
                "visual_atlas_batch_evidence",
                "episode batch relation cited evidence outside the supplied batch",
            )
        if "display" not in relation:
            raise VisualAtlasError(
                "visual_atlas_batch_relation_display",
                "every memory relation requires Chinese and English label and reason text",
            )
    return normalized


def validate_visual_atlas_episode_batch_patch_with_relation_rejections(
    base: Mapping[str, Any],
    batch: Mapping[str, Any],
    patch: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Keep exact episode coverage while rejecting invalid optional relations individually."""

    if not isinstance(patch, Mapping):
        raise VisualAtlasError("visual_atlas_patch_invalid", "patch must be an object")
    updates_only = {
        "domain_updates": patch.get("domain_updates"),
        "episode_updates": patch.get("episode_updates"),
        "memory_updates": patch.get("memory_updates"),
        "relations": [],
    }
    normalized = validate_visual_atlas_episode_batch_patch(base, batch, updates_only)
    rejected: list[dict[str, Any]] = []
    raw_relations = patch.get("relations")
    if not isinstance(raw_relations, Sequence) or isinstance(raw_relations, (str, bytes)):
        rejected.append(
            {
                "index": 0,
                "code": "visual_atlas_patch_relations",
                "message": "relations must be a list",
            }
        )
        return normalized, rejected

    maximum = min(
        VISUAL_ATLAS_MAX_RELATIONS_PER_BATCH,
        max(0, _integer(batch.get("max_relations"))),
    )
    accepted: list[dict[str, Any]] = []
    for index, relation in enumerate(raw_relations):
        if index >= maximum:
            rejected.append(
                {
                    "index": index,
                    "code": "visual_atlas_batch_relations",
                    "message": f"episode batch allows at most {maximum} relations",
                }
            )
            continue
        if not isinstance(relation, Mapping):
            rejected.append(
                {
                    "index": index,
                    "code": "visual_atlas_patch_relations",
                    "message": "relation item must be an object",
                }
            )
            continue
        candidate = {
            "domain_updates": normalized["domain_updates"],
            "episode_updates": normalized["episode_updates"],
            "memory_updates": normalized["memory_updates"],
            "relations": [*accepted, dict(relation)],
        }
        try:
            candidate_normalized = validate_visual_atlas_episode_batch_patch(
                base, batch, candidate
            )
        except VisualAtlasError as exc:
            rejected.append(
                {
                    "index": index,
                    "code": exc.code,
                    "message": str(exc),
                }
            )
            continue
        accepted = candidate_normalized["relations"]

    normalized["relations"] = accepted
    return normalized, rejected


def merge_visual_atlas_episode_batch_patches(
    base: Mapping[str, Any],
    batches: Sequence[Mapping[str, Any]],
    patches: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Merge validated bounded patches and prove exact full-atlas coverage."""

    if len(batches) != len(patches):
        raise VisualAtlasError(
            "visual_atlas_batch_count",
            "every episode batch must have exactly one patch",
        )
    merged: dict[str, list[dict[str, Any]]] = {
        "domain_updates": [],
        "episode_updates": [],
        "memory_updates": [],
        "relations": [],
    }
    seen_episode_ids: set[str] = set()
    seen_memory_ids: set[str] = set()
    seen_relation_keys: set[tuple[str, str, str]] = set()
    for batch, patch in zip(batches, patches):
        normalized = validate_visual_atlas_episode_batch_patch(base, batch, patch)
        for update in normalized["episode_updates"]:
            episode_id = update["episode_id"]
            if episode_id in seen_episode_ids:
                raise VisualAtlasError(
                    "visual_atlas_batch_overlap",
                    f"episode was returned by more than one batch: {episode_id}",
                )
            seen_episode_ids.add(episode_id)
            merged["episode_updates"].append(update)
        for update in normalized["memory_updates"]:
            evidence_id = update["evidence_id"]
            if evidence_id in seen_memory_ids:
                raise VisualAtlasError(
                    "visual_atlas_batch_memory_overlap",
                    f"semantic memory was returned by more than one batch: {evidence_id}",
                )
            seen_memory_ids.add(evidence_id)
            merged["memory_updates"].append(update)
        for relation in normalized["relations"]:
            key = (
                relation["source_id"],
                relation["target_id"],
                relation["type"],
            )
            if key not in seen_relation_keys:
                merged["relations"].append(relation)
                seen_relation_keys.add(key)

    atlas = validate_visual_atlas(base)
    expected_episode_ids = {
        _text(item.get("episode_id"), 512)
        for item in _items(atlas.get("nodes"))
        if item.get("level") == "episode"
    }
    if seen_episode_ids != expected_episode_ids:
        raise VisualAtlasError(
            "visual_atlas_batch_coverage",
            "merged episode batches do not cover the exact full atlas",
        )
    expected_memory_ids = {
        _text(item.get("id"), 512)
        for item in _items(atlas.get("nodes"))
        if item.get("level") == "evidence" and item.get("evidence_kind") == "memory"
    }
    if seen_memory_ids != expected_memory_ids:
        raise VisualAtlasError(
            "visual_atlas_batch_memory_coverage",
            "merged episode batches do not cover every semantic memory",
        )
    return validate_visual_atlas_patch(base, merged)


def apply_visual_atlas_patch(
    base: Mapping[str, Any],
    patch: Mapping[str, Any],
    *,
    model: str | None = None,
) -> dict[str, Any]:
    """Apply only readable Agent annotations to a validated full atlas."""

    normalized = validate_visual_atlas_patch(base, patch)
    result = _clone(dict(base))
    nodes = {node["id"]: node for node in result["nodes"]}
    for update in normalized["domain_updates"]:
        node = nodes[update["domain_id"]]
        for key in ("label", "summary", "topic_tags", "display"):
            if key in update:
                node[key] = update[key]
    for update in normalized["episode_updates"]:
        node = nodes[update["episode_id"]]
        for key in ("label", "summary", "chapter_tags", "display"):
            if key in update:
                node[key] = update[key]
    for update in normalized["memory_updates"]:
        node = nodes[update["evidence_id"]]
        for key in ("label", "summary", "memory_type", "keywords", "display"):
            node[key] = update[key]
    for relation in normalized["relations"]:
        source = relation["source_id"]
        target = relation["target_id"]
        result["edges"].append(
            {
                "id": _stable_id("visual-edge", f"{source}|{target}|{relation['type']}"),
                "source": source,
                "target": target,
                "type": relation["type"],
                "weight": relation["weight"],
                "evidence_ids": relation["evidence_ids"],
                **({"label": relation["label"]} if relation.get("label") else {}),
                "reason": relation["reason"],
                **({"display": relation["display"]} if "display" in relation else {}),
                "origin": "visual_atlas_agent",
                "prompt_version": VISUAL_ATLAS_PROMPT_VERSION,
            }
        )
    result["projection_state"] = "ready"
    result["generated_by"] = "visual-atlas-agent"
    result["prompt_version"] = VISUAL_ATLAS_PROMPT_VERSION
    result["model"] = _text(model, 160) or None
    result["counts"]["edges"] = len(result["edges"])
    return validate_visual_atlas(result)
