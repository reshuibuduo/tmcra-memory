#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TypeVar


def permits_source_extraction(role: str, actor_metadata: Mapping[str, Any]) -> bool:
    """Role is transport; unknown/automatic user-origin stays immutable-only."""
    if role == "assistant":
        return True  # Existing validator drops assistant assertions.
    return (role == "user"
            and actor_metadata.get("origin_kind") in {"human_authored", "human_adopted"}
            and actor_metadata.get("provenance_strength") in {"host_declared", "caller_declared"})


def origin_authority(role: str, actor_metadata: Mapping[str, Any], *, derived: bool = False) -> str:
    if role == "user" and permits_source_extraction(role, actor_metadata):
        return "user_assertion" if derived else "user_source"
    kind = actor_metadata.get("origin_kind", "unknown")
    stem = kind if kind in {"app_generated", "automation", "assistant_generated", "tool_generated"} else ("assistant" if role == "assistant" else "unknown_origin")
    return f"derived_{stem}_memory" if derived else f"{stem}_source"


WRITE_SCHEMA_VERSION = "tmcra.memory-write.v3.4"
PROMPT_VERSION = "tmcra-product-writer-2026-07-10.21"
SOURCE_JOURNAL_CLAIM_LEASE_SECONDS = 900
T = TypeVar("T")

FORBIDDEN_WRITER_FIELDS = {
    "question",
    "question_date",
    "question_type",
    "answer",
    "gold_answer",
    "answer_session_ids",
    "labels",
    "supervision",
}
MEMORY_TYPES = {
    "fact",
    "event",
    "state",
    "preference",
    "goal",
    "constraint",
    "plan",
    "identity",
    "relationship",
    "possession",
    "routine",
}
TEMPORAL_STATUSES = {"past", "current", "planned", "future", "timeless", "uncertain"}
POLARITIES = {"positive", "negative"}
OPERATIONS = {"append", "replace"}
FACET_TYPES = {"entity", "time", "quantity", "state", "location", "role"}
INTERACTION_TYPES = {"question", "request", "reminder", "task", "clarification", "feedback"}
INTERACTION_STATUSES = {"open", "informational"}
RESOLUTION_STATES = {"resolved", "partial", "unresolved"}
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,159}$")
ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
SOURCE_TOKEN_RE = re.compile(r"[\u3400-\u9fff]|[^\s\u3400-\u9fff]+")
SENTENCE_BREAK_RE = re.compile(r"(?<=[.!?])\s+|\n+|(?<=[。！？])")


SYSTEM_PROMPT = """You are the semantic write stage of a production personal-memory system.
Return one strict JSON object and nothing else. current_message is the only new source. previous_message,
existing_memory_slots, and pending_interactions are read-only context for reference, slot, and resolution.
When capacity_segment is present, current_message is an exact slice of full_current_message_context. Extract
every item whose evidence is inside that slice, using the full message only to interpret references and context.
Do not emit an item whose only evidence is outside the slice. Capacity segmentation never changes the semantic
contract and never permits omission, ranking, or summarization.

The output has three independent layers:
1. assertions: facts explicitly asserted by the user, including events, updates, preferences, goals,
   constraints, plans, identity, relationships, possessions, and routines. Questions are never assertions.
   A question's presupposition is not a fact. Assistant statements are not user assertions.
2. interactions: every explicit question, request, reminder, task, clarification, or meaningful feedback in
   current_message. A question-only message must produce an interaction even when assertions is empty.
   A mixed message can and usually should produce both assertions and interactions.
3. resolutions: whether current_message resolves a supplied pending_interaction. Use resolved only when the
   current message actually answers or completes it, partial when it advances it without completing it, and
   unresolved when it responds without supplying the requested result. Use exact supplied interaction_id.
   pending_interactions are candidates, not a checklist. Omit an unrelated candidate instead of emitting a
   resolution. Every emitted resolution must reference non-empty evidence from current_message.
   Use unresolved only when current_message explicitly responds without a result, such as a refusal or stated
   inability; the mere absence of an answer is not resolution evidence.

For assistant messages, assertions must be empty because assistant claims have different authority. Assistant
messages may create interactions only when the assistant explicitly asks the user a question, requests an
action, sets a task or reminder, or asks for clarification. An answer, recommendation, apology, correction
acknowledgement, confirmation, or explanatory statement is not a new interaction; it may resolve an existing
interaction and otherwise remains only immutable source. For user messages, extract user assertions, create
interactions, and resolve assistant interactions when applicable. Never infer an unstated fact. Never store
passwords, authentication secrets, private keys, or account IDs as assertions.

Each assertion and interaction must be atomic. Never copy or rewrite source text in the output. Instead, select
one exact evidence_span_id from evidence_spans. Prefer the shortest catalog span that fully supports the item;
e0 always means the full current_message and is represented compactly without repeating its text. Resolutions
also select one evidence_span_id. source_tokens is a compact string array whose zero-based array position is the
token id. Every facet/about item selects an inclusive token_start and token_end from source_tokens.
The token range must be the shortest exact source range that names the entity, time, quantity, state, location,
or role. Preserve negation, uncertainty, and correction semantics. Assertion authority is always the user; do
not emit redundant subject or object fields.

For assertions, entity_key identifies the stable real-world subject or domain and attribute_key identifies
the specific property, goal, state, or event kind. Use lowercase dot-separated tokens. Prefer the concrete
topic domain (for example starbucks.rewards or laptop.purchase) over the generic entity_key user. Never put
the changing value in either key. Reuse an existing entity_key plus attribute_key only when both the entity
and the predicate are the same. Topic similarity is insufficient: a goal to reach a membership level and the
number of points required for that level are different attributes. The runtime also isolates different memory
families, so a fact cannot replace a goal even when keys are imperfect. Use replace for mutable attributes and
append for repeatable events. memory_type goal is only the desired outcome itself. A threshold, requirement,
or current value is fact or state even when phrased as "I need N units to reach ...". relation and all
role/intent fields are lowercase snake_case.

The output message_role must exactly copy current_message.role. It is source
metadata, not a semantic classification task.

Return exactly:
{"schema_version":"tmcra.memory-write.v3.4","message_role":"user|assistant|system|tool",
 "assertions":[
  {"memory_type":"fact|event|state|preference|goal|constraint|plan|identity|relationship|possession|routine",
   "entity_key":"stable.entity.or.domain","attribute_key":"specific_attribute_or_event","operation":"append|replace",
   "evidence_span_id":"eN","relation":"snake_case",
   "temporal_status":"past|current|planned|future|timeless|uncertain",
   "polarity":"positive|negative","facets":[{"type":"entity|time|quantity|state|location|role",
   "role":"snake_case","token_start":0,"token_end":0}]}
 ],
 "interactions":[
  {"interaction_type":"question|request|reminder|task|clarification|feedback",
   "status":"open|informational","evidence_span_id":"eN","intent":"snake_case",
   "about":[{"type":"entity|time|quantity|state|location|role","role":"snake_case",
   "token_start":0,"token_end":0}]}
 ],
 "resolutions":[
   {"interaction_id":"exact supplied id","resolution":"resolved|partial|unresolved",
   "evidence_span_id":"eN"}
 ]}

Use empty arrays when a layer has no output. Return every distinct assertion, interaction, resolution, and
semantically relevant facet/about item. Never omit an item to meet an arbitrary count limit.
Do not rank, summarize, repair, or select only the most interesting items."""

PRO_PROMPT_VERSION = "tmcra-product-writer-pro-2026-07-10.10"
PRO_SYSTEM_PROMPT = """You are the high-accuracy primary writer for a production personal-memory system.
Return a complete output under the exact contract below. Recover every explicit assertion or interaction,
remove inferred content, choose precise memory_type/entity_key/attribute_key/operation values, and verify
resolutions against pending_interactions. Return one strict JSON object and nothing else.

Before returning, independently inspect every clause in current_message rather than reviewing only the Flash
items. Every explicit user self-report about an intention, plan, goal, consideration, current or past state,
preference, possession, relationship, identity, routine, event, or constraint must appear as an assertion.
This remains true when another clause in the same message asks a question or requests advice. Do not let an
interaction suppress a distinct assertion, and do not turn the question itself into an assertion.

""" + SYSTEM_PROMPT


class ProductWriterError(RuntimeError):
    pass


class ProductWriterResponseError(ProductWriterError):
    def __init__(
        self,
        message: str,
        *,
        response_content: str = "",
        request_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.response_content = response_content
        self.request_metadata = dict(request_metadata or {})


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def physical_requests_from_metadata(metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
    requests = metadata.get("requests")
    if isinstance(requests, list):
        return [dict(value) for value in requests if isinstance(value, Mapping)]
    return [dict(metadata)] if metadata.get("model") else []


def load_jsonl_for_resume(path: Path) -> tuple[list[dict[str, Any]], bool]:
    if not path.exists():
        return [], False
    data = path.read_bytes()
    rows: list[dict[str, Any]] = []
    offset = 0
    lines = data.splitlines(keepends=True)
    for index, raw_line in enumerate(lines):
        next_offset = offset + len(raw_line)
        if not raw_line.strip():
            offset = next_offset
            continue
        try:
            decoded = raw_line.decode("utf-8")
            value = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            later_content = any(line.strip() for line in lines[index + 1 :])
            has_complete_ending = raw_line.endswith((b"\n", b"\r"))
            if later_content or has_complete_ending:
                raise ProductWriterError(f"{path.name}: malformed non-tail JSONL row {index + 1}: {exc}") from exc
            with path.open("r+b") as handle:
                handle.truncate(offset)
            return rows, True
        if not isinstance(value, dict):
            raise ProductWriterError(f"{path.name}: JSONL row {index + 1} must be an object")
        rows.append(value)
        offset = next_offset
    return rows, False


def exact_evidence_spans(value: str) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = [
        {"span_id": "e0", "text": value, "char_start": 0, "char_end": len(value)}
    ]
    cursor = 0
    for match in [*SENTENCE_BREAK_RE.finditer(value), None]:
        boundary = match.start() if match is not None else len(value)
        start = cursor
        end = boundary
        while start < end and value[start].isspace():
            start += 1
        while end > start and value[end - 1].isspace():
            end -= 1
        if start < end:
            spans.append(
                {
                    "span_id": f"e{len(spans)}",
                    "text": value[start:end],
                    "char_start": start,
                    "char_end": end,
                }
            )
        cursor = match.end() if match is not None else len(value)
    return spans


def compact_writer_evidence_spans(value: str) -> list[dict[str, Any]]:
    spans = exact_evidence_spans(value)
    return [
        {
            "span_id": "e0",
            "source": "full_current_message",
            "char_start": 0,
            "char_end": len(value),
        },
        *[span for span in spans[1:]],
    ]


def exact_source_tokens(value: str) -> list[dict[str, Any]]:
    return [
        {"token_id": index, "text": match.group(0)}
        for index, match in enumerate(source_token_matches(value))
    ]


def source_token_matches(value: str) -> list[re.Match[str]]:
    return list(SOURCE_TOKEN_RE.finditer(value))


def split_capacity_range(value: str, start: int, end: int) -> tuple[tuple[int, int], tuple[int, int]] | None:
    while start < end and value[start].isspace():
        start += 1
    while end > start and value[end - 1].isspace():
        end -= 1
    if end - start < 2:
        return None

    segment = value[start:end]
    midpoint = start + (end - start) / 2
    boundaries: list[tuple[int, int]] = []
    for match in SENTENCE_BREAK_RE.finditer(segment):
        boundaries.append((start + match.start(), start + match.end()))
    if len(boundaries) < 3:
        return None

    for boundary_index in sorted(
        range(1, len(boundaries) - 1),
        key=lambda index: abs(((boundaries[index][0] + boundaries[index][1]) / 2) - midpoint),
    ):
        # Keep one complete sentence on both sides of the split. A source that cannot
        # be divided at sentence boundaries fails visibly instead of cutting a fact.
        left_end = boundaries[boundary_index + 1][0]
        right_start = boundaries[boundary_index - 1][1]
        if start < left_end and right_start < end:
            return (start, left_end), (right_start, end)
    return None


def merge_segment_outputs(
    outputs: Sequence[Mapping[str, Any]],
    *,
    message_role: str,
) -> tuple[dict[str, Any], int]:
    merged: dict[str, Any] = {
        "schema_version": WRITE_SCHEMA_VERSION,
        "message_role": message_role,
        "assertions": [],
        "interactions": [],
        "resolutions": [],
        "validation_warnings": [],
        "quarantined_item_count": 0,
    }
    assertion_by_key: dict[tuple[str, ...], dict[str, Any]] = {}
    interaction_by_key: dict[tuple[str, ...], dict[str, Any]] = {}
    resolution_by_id: dict[str, dict[str, Any]] = {}
    duplicate_count = 0

    def merge_facets(target: list[dict[str, Any]], incoming: Sequence[Mapping[str, Any]]) -> None:
        nonlocal duplicate_count
        seen = {
            (str(item.get("type")), str(item.get("role")), str(item.get("quote")))
            for item in target
        }
        for item in incoming:
            facet = dict(item)
            key = (str(facet.get("type")), str(facet.get("role")), str(facet.get("quote")))
            if key in seen:
                duplicate_count += 1
                continue
            seen.add(key)
            target.append(facet)

    for segment_index, raw_output in enumerate(outputs):
        output = dict(raw_output)
        merged["quarantined_item_count"] += int(output.get("quarantined_item_count", 0) or 0)
        for raw_warning in output.get("validation_warnings") or []:
            warning = dict(raw_warning)
            warning["path"] = f"capacity_segments[{segment_index}].{warning.get('path', 'root')}"
            merged["validation_warnings"].append(warning)

        for raw_assertion in output.get("assertions") or []:
            assertion = dict(raw_assertion)
            append_member_identity = ()
            if str(assertion.get("operation")) == "append":
                append_member_identity = tuple(
                    sorted(
                        (
                            str(facet.get("type")),
                            str(facet.get("role")),
                            int(facet.get("token_start", -1)),
                            int(facet.get("token_end", -1)),
                        )
                        for facet in assertion.get("facets") or []
                    )
                )
            key = (
                str(assertion.get("canonical_key")),
                str(assertion.get("evidence_quote")),
                str(assertion.get("operation")),
                str(assertion.get("temporal_status")),
                str(assertion.get("polarity")),
                str(assertion.get("memory_type")),
                str(assertion.get("relation")),
                str(assertion.get("evidence_char_start")),
                str(assertion.get("evidence_char_end")),
                append_member_identity,
            )
            existing = assertion_by_key.get(key)
            if existing is not None:
                duplicate_count += 1
                merge_facets(existing["facets"], assertion.get("facets") or [])
                continue
            assertion_by_key[key] = assertion
            merged["assertions"].append(assertion)

        for raw_interaction in output.get("interactions") or []:
            interaction = dict(raw_interaction)
            key = (
                str(interaction.get("interaction_type")),
                str(interaction.get("intent")),
                str(interaction.get("evidence_quote")),
                str(interaction.get("status")),
                str(interaction.get("evidence_char_start")),
                str(interaction.get("evidence_char_end")),
            )
            existing = interaction_by_key.get(key)
            if existing is not None:
                duplicate_count += 1
                merge_facets(existing["about"], interaction.get("about") or [])
                continue
            interaction_by_key[key] = interaction
            merged["interactions"].append(interaction)

        for raw_resolution in output.get("resolutions") or []:
            resolution = dict(raw_resolution)
            interaction_id = str(resolution.get("interaction_id"))
            existing = resolution_by_id.get(interaction_id)
            if existing is None:
                resolution_by_id[interaction_id] = resolution
                merged["resolutions"].append(resolution)
                continue
            duplicate_count += 1
            if existing.get("resolution") != resolution.get("resolution"):
                raise ProductWriterError(
                    f"capacity segments produced conflicting resolutions for {interaction_id!r}: "
                    f"{existing.get('resolution')!r} != {resolution.get('resolution')!r}"
                )

    return merged, duplicate_count


def slug(value: Any, *, limit: int = 80) -> str:
    result = re.sub(r"[^a-z0-9]+", "_", clean_text(value).lower()).strip("_")
    return (result or "unknown")[:limit]


def normalize_canonical_key(value: str) -> str:
    return ".".join(re.findall(r"[a-z0-9]+", value.lower()))


def memory_family(memory_type: str) -> str:
    return {
        "fact": "fact",
        "state": "fact",
        "event": "event",
        "preference": "preference",
        "goal": "goal",
        "constraint": "constraint",
        "plan": "plan",
        "identity": "identity",
        "relationship": "relationship",
        "possession": "possession",
        "routine": "routine",
    }[memory_type]


def graph_entity_key(entity_key: str, attribute_key: str) -> str:
    normalized_entity = normalize_canonical_key(entity_key)
    if normalized_entity not in {"user", "self", "person.user"}:
        return normalized_entity
    attribute_parts = normalize_canonical_key(attribute_key).split(".")
    domain = ".".join(attribute_parts[:2]) if len(attribute_parts) >= 2 else attribute_parts[0]
    return f"user.{domain}"


def exact_json_object(
    content: str,
    *,
    warnings: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    if not content or not content.strip():
        raise ProductWriterError("writer response must be a non-empty JSON object")
    normalized = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n([\s\S]*?)\n```", normalized, flags=re.IGNORECASE)
    if fenced:
        normalized = fenced.group(1).strip()
        if warnings is not None:
            warnings.append(
                {
                    "path": "root",
                    "code": "json_fence_removed",
                    "error": "removed one outer Markdown JSON fence",
                }
            )
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise ProductWriterError(f"writer response is not strict JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ProductWriterError("writer response root must be an object")
    return parsed


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], *, path: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ProductWriterError(
            f"{path} keys differ from schema; missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )


def _require_string(value: Any, *, path: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ProductWriterError(f"{path} must be a string")
    if not allow_empty and not value:
        raise ProductWriterError(f"{path} must not be empty")
    if value != value.strip():
        raise ProductWriterError(f"{path} must not have surrounding whitespace")
    return value


def _require_int(value: Any, *, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProductWriterError(f"{path} must be an integer")
    return value


def validate_writer_output(
    payload: Mapping[str, Any],
    current_message: str,
    *,
    message_role: str,
    pending_interaction_ids: Sequence[str] = (),
) -> dict[str, Any]:
    root_keys = {"schema_version", "message_role", "assertions", "interactions", "resolutions"}
    _require_exact_keys(dict(payload), root_keys, path="root")
    if payload.get("schema_version") != WRITE_SCHEMA_VERSION:
        raise ProductWriterError(f"unexpected writer schema: {payload.get('schema_version')!r}")
    output_role = _require_string(payload.get("message_role"), path="root.message_role")
    role_warning: dict[str, Any] | None = None
    if output_role != message_role:
        role_warning = {
            "path": "root.message_role",
            "code": "model_message_role_overridden",
            "error": (
                "source message role is controller-owned; "
                f"overrode model value {output_role!r} with {message_role!r}"
            ),
            "dropped_count": 0,
        }
        output_role = message_role

    raw_layers = {
        "assertions": payload.get("assertions"),
        "interactions": payload.get("interactions"),
        "resolutions": payload.get("resolutions"),
    }
    warnings: list[dict[str, Any]] = [role_warning] if role_warning else []

    def warn(path: str, code: str, error: str, *, dropped_count: int = 0) -> None:
        warnings.append(
            {
                "path": path,
                "code": code,
                "error": error,
                "dropped_count": max(0, int(dropped_count)),
            }
        )

    def enum_value(value: Any, allowed: set[str], *, path: str) -> str:
        original = _require_string(value, path=path)
        normalized = original.lower()
        if normalized != original:
            warn(path, "identifier_case_normalized", f"normalized {original!r} to {normalized!r}")
        if normalized not in allowed:
            raise ProductWriterError(f"{path} is unsupported: {original!r}")
        return normalized

    def role_identifier(value: Any, *, path: str) -> str:
        original = _require_string(value, path=path)
        normalized = original.lower()
        if normalized != original:
            warn(path, "identifier_case_normalized", f"normalized {original!r} to {normalized!r}")
        if not ROLE_RE.fullmatch(normalized):
            raise ProductWriterError(f"{path} is not snake_case: {original!r}")
        return normalized

    def canonical_identifier(value: Any, *, path: str) -> str:
        original = _require_string(value, path=path)
        lowered = original.lower()
        if lowered != original:
            warn(path, "identifier_case_normalized", f"normalized {original!r} to {lowered!r}")
        if not KEY_RE.fullmatch(lowered):
            raise ProductWriterError(f"{path} is not a canonical identifier: {original!r}")
        normalized = normalize_canonical_key(lowered)
        if normalized != lowered:
            warn(path, "identifier_separator_normalized", f"normalized {lowered!r} to {normalized!r}")
        return normalized

    for name, values in raw_layers.items():
        if not isinstance(values, list):
            raise ProductWriterError(f"root.{name} must be an array")
    assertions = list(raw_layers["assertions"])
    interactions = list(raw_layers["interactions"])
    resolutions = list(raw_layers["resolutions"])
    if message_role != "user" and assertions:
        raise ProductWriterError(f"{message_role} messages cannot emit user assertions")

    evidence_catalog = {span["span_id"]: span for span in exact_evidence_spans(current_message)}
    token_matches = source_token_matches(current_message)

    def resolve_evidence_span(value: Any, *, path: str) -> tuple[str, str, int, int]:
        span_id = _require_string(value, path=path)
        evidence_span = evidence_catalog.get(span_id)
        if evidence_span is None:
            raise ProductWriterError(f"{path} is not in the current-message evidence catalog: {span_id!r}")
        return (
            span_id,
            str(evidence_span["text"]),
            int(evidence_span["char_start"]),
            int(evidence_span["char_end"]),
        )

    def validate_facets(
        raw_facets: Any,
        *,
        path: str,
    ) -> list[dict[str, Any]]:
        if not isinstance(raw_facets, list):
            warn(path, "invalid_facet_array_dropped", f"{path} must be an array")
            return []
        output: list[dict[str, Any]] = []
        seen_facets: set[tuple[str, str, int, int]] = set()
        for facet_index, raw_facet in enumerate(raw_facets):
            facet_path = f"{path}[{facet_index}]"
            try:
                if not isinstance(raw_facet, dict):
                    raise ProductWriterError(f"{facet_path} must be an object")
                required_facet_keys = {"type", "role", "token_start", "token_end"}
                actual_facet_keys = frozenset(raw_facet)
                if actual_facet_keys not in {frozenset(required_facet_keys), frozenset({*required_facet_keys, "quote"})}:
                    raise ProductWriterError(
                        f"{facet_path} keys differ from schema; "
                        f"missing={sorted(required_facet_keys - actual_facet_keys)}, "
                        f"extra={sorted(actual_facet_keys - required_facet_keys)}"
                    )
                facet_type = enum_value(raw_facet.get("type"), FACET_TYPES, path=f"{facet_path}.type")
                facet_role = role_identifier(raw_facet.get("role"), path=f"{facet_path}.role")
                token_start = _require_int(raw_facet.get("token_start"), path=f"{facet_path}.token_start")
                token_end = _require_int(raw_facet.get("token_end"), path=f"{facet_path}.token_end")
                if token_start < 0 or token_end < token_start or token_end >= len(token_matches):
                    raise ProductWriterError(f"{facet_path} has an out-of-range source token interval")
                facet_quote = current_message[token_matches[token_start].start() : token_matches[token_end].end()]
                if "quote" in raw_facet:
                    supplied_quote = raw_facet.get("quote")
                    warn(
                        facet_path,
                        (
                            "redundant_facet_quote_ignored"
                            if isinstance(supplied_quote, str) and supplied_quote == facet_quote
                            else "mismatched_redundant_facet_quote_ignored"
                        ),
                        "token coordinates remain the authoritative exact-source facet evidence",
                    )
                key = (facet_type, facet_role, token_start, token_end)
                if key in seen_facets:
                    warn(facet_path, "duplicate_facet_dropped", "duplicates an earlier facet", dropped_count=1)
                    continue
                seen_facets.add(key)
                output.append(
                    {
                        "type": facet_type,
                        "role": facet_role,
                        "token_start": token_start,
                        "token_end": token_end,
                        "quote": facet_quote,
                    }
                )
            except ProductWriterError as exc:
                warn(facet_path, "invalid_facet_quarantined", str(exc), dropped_count=1)
        return output

    normalized_assertions: list[dict[str, Any]] = []
    assertion_seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    assertion_keys = {
        "memory_type", "entity_key", "attribute_key", "operation", "evidence_span_id", "relation",
        "temporal_status", "polarity", "facets",
    }
    for assertion_index, raw_assertion in enumerate(assertions):
        path = f"root.assertions[{assertion_index}]"
        try:
            if not isinstance(raw_assertion, dict):
                raise ProductWriterError(f"{path} must be an object")
            _require_exact_keys(raw_assertion, assertion_keys, path=path)
            memory_type = enum_value(raw_assertion.get("memory_type"), MEMORY_TYPES, path=f"{path}.memory_type")
            entity_key = canonical_identifier(raw_assertion.get("entity_key"), path=f"{path}.entity_key")
            attribute_key = canonical_identifier(raw_assertion.get("attribute_key"), path=f"{path}.attribute_key")
            operation = enum_value(raw_assertion.get("operation"), OPERATIONS, path=f"{path}.operation")
            evidence_span_id, evidence_quote, evidence_char_start, evidence_char_end = resolve_evidence_span(
                raw_assertion.get("evidence_span_id"), path=f"{path}.evidence_span_id"
            )
            relation = role_identifier(raw_assertion.get("relation"), path=f"{path}.relation")
            temporal_status = enum_value(
                raw_assertion.get("temporal_status"), TEMPORAL_STATUSES, path=f"{path}.temporal_status"
            )
            polarity = enum_value(raw_assertion.get("polarity"), POLARITIES, path=f"{path}.polarity")
            entity_key = entity_key.removeprefix("user.")
            assertion_family = memory_family(memory_type)
            assertion_graph_entity = graph_entity_key(entity_key, attribute_key)
            canonical_key = f"user.{entity_key}.{assertion_family}.{attribute_key}"
            if not entity_key or not attribute_key or len(canonical_key) > 160:
                raise ProductWriterError(f"{path} has an invalid entity/attribute key or relation")
            normalized_facets = validate_facets(raw_assertion.get("facets"), path=f"{path}.facets")
            append_member_identity = ()
            if operation == "append":
                append_member_identity = tuple(
                    sorted(
                        (
                            facet["type"],
                            facet["role"],
                            facet["token_start"],
                            facet["token_end"],
                        )
                        for facet in normalized_facets
                    )
                )
            key = (
                canonical_key,
                evidence_char_start,
                evidence_char_end,
                memory_type,
                operation,
                relation,
                temporal_status,
                polarity,
                append_member_identity,
            )
            existing_assertion = assertion_seen.get(key)
            if existing_assertion is not None:
                existing_facets = existing_assertion["facets"]
                existing_facet_keys = {
                    (facet["type"], facet["role"], facet["token_start"], facet["token_end"])
                    for facet in existing_facets
                }
                for facet in normalized_facets:
                    facet_key = (facet["type"], facet["role"], facet["token_start"], facet["token_end"])
                    if facet_key not in existing_facet_keys:
                        existing_facets.append(facet)
                        existing_facet_keys.add(facet_key)
                warn(path, "duplicate_assertion_merged", "merged facets into an identical assertion")
                continue
            normalized_assertion = {
                "memory_type": memory_type,
                "entity_key": entity_key,
                "attribute_key": attribute_key,
                "memory_family": assertion_family,
                "graph_entity_key": assertion_graph_entity,
                "canonical_key": canonical_key,
                "operation": operation,
                "evidence_span_id": evidence_span_id,
                "evidence_quote": evidence_quote,
                "evidence_char_start": evidence_char_start,
                "evidence_char_end": evidence_char_end,
                "subject": "user",
                "relation": relation,
                "object": "",
                "temporal_status": temporal_status,
                "polarity": polarity,
                "facets": normalized_facets,
            }
            assertion_seen[key] = normalized_assertion
            normalized_assertions.append(normalized_assertion)
        except ProductWriterError as exc:
            warn(path, "invalid_assertion_quarantined", str(exc), dropped_count=1)

    normalized_interactions: list[dict[str, Any]] = []
    interaction_seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    for interaction_index, raw_interaction in enumerate(interactions):
        path = f"root.interactions[{interaction_index}]"
        try:
            if not isinstance(raw_interaction, dict):
                raise ProductWriterError(f"{path} must be an object")
            _require_exact_keys(
                raw_interaction,
                {"interaction_type", "status", "evidence_span_id", "intent", "about"},
                path=path,
            )
            interaction_type = enum_value(
                raw_interaction.get("interaction_type"), INTERACTION_TYPES, path=f"{path}.interaction_type"
            )
            status = enum_value(raw_interaction.get("status"), INTERACTION_STATUSES, path=f"{path}.status")
            evidence_span_id, evidence_quote, evidence_char_start, evidence_char_end = resolve_evidence_span(
                raw_interaction.get("evidence_span_id"), path=f"{path}.evidence_span_id"
            )
            intent = role_identifier(raw_interaction.get("intent"), path=f"{path}.intent")
            normalized_about = validate_facets(raw_interaction.get("about"), path=f"{path}.about")
            key = (
                interaction_type,
                status,
                intent,
                evidence_char_start,
                evidence_char_end,
            )
            existing_interaction = interaction_seen.get(key)
            if existing_interaction is not None:
                existing_about = existing_interaction["about"]
                existing_about_keys = {
                    (facet["type"], facet["role"], facet["token_start"], facet["token_end"])
                    for facet in existing_about
                }
                for facet in normalized_about:
                    facet_key = (facet["type"], facet["role"], facet["token_start"], facet["token_end"])
                    if facet_key not in existing_about_keys:
                        existing_about.append(facet)
                        existing_about_keys.add(facet_key)
                warn(path, "duplicate_interaction_merged", "merged about items into an identical interaction")
                continue
            normalized_interaction = {
                "interaction_type": interaction_type,
                "status": status,
                "evidence_span_id": evidence_span_id,
                "evidence_quote": evidence_quote,
                "evidence_char_start": evidence_char_start,
                "evidence_char_end": evidence_char_end,
                "intent": intent,
                "about": normalized_about,
            }
            interaction_seen[key] = normalized_interaction
            normalized_interactions.append(normalized_interaction)
        except ProductWriterError as exc:
            warn(path, "invalid_interaction_quarantined", str(exc), dropped_count=1)

    allowed_pending = set(pending_interaction_ids)
    normalized_resolutions: list[dict[str, Any]] = []
    resolution_seen: dict[str, str] = {}
    resolution_conflicts: list[str] = []
    for resolution_index, raw_resolution in enumerate(resolutions):
        path = f"root.resolutions[{resolution_index}]"
        try:
            if not isinstance(raw_resolution, dict):
                raise ProductWriterError(f"{path} must be an object")
            _require_exact_keys(raw_resolution, {"interaction_id", "resolution", "evidence_span_id"}, path=path)
            interaction_id = _require_string(raw_resolution.get("interaction_id"), path=f"{path}.interaction_id")
            resolution = enum_value(
                raw_resolution.get("resolution"), RESOLUTION_STATES, path=f"{path}.resolution"
            )
            evidence_span_id, evidence_quote, evidence_char_start, evidence_char_end = resolve_evidence_span(
                raw_resolution.get("evidence_span_id"), path=f"{path}.evidence_span_id"
            )
            if interaction_id not in allowed_pending:
                raise ProductWriterError(f"{path}.interaction_id was not supplied as pending: {interaction_id!r}")
            existing_resolution = resolution_seen.get(interaction_id)
            if existing_resolution is not None:
                if existing_resolution != resolution:
                    resolution_conflicts.append(
                        f"{interaction_id!r}: {existing_resolution!r} != {resolution!r}"
                    )
                else:
                    warn(path, "duplicate_resolution_merged", "merged an identical resolution")
                continue
            resolution_seen[interaction_id] = resolution
            normalized_resolutions.append(
                {
                    "interaction_id": interaction_id,
                    "resolution": resolution,
                    "evidence_span_id": evidence_span_id,
                    "evidence_quote": evidence_quote,
                    "evidence_char_start": evidence_char_start,
                    "evidence_char_end": evidence_char_end,
                }
            )
        except ProductWriterError as exc:
            warn(path, "invalid_resolution_quarantined", str(exc), dropped_count=1)
    if resolution_conflicts:
        raise ProductWriterError(
            "root.resolutions contains conflicting states for the same interaction: "
            + "; ".join(resolution_conflicts)
        )
    return {
        "schema_version": WRITE_SCHEMA_VERSION,
        "message_role": output_role,
        "assertions": normalized_assertions,
        "interactions": normalized_interactions,
        "resolutions": normalized_resolutions,
        "validation_warnings": warnings,
        "quarantined_item_count": sum(int(warning.get("dropped_count", 0) or 0) for warning in warnings),
    }


class DeepSeekProductWriter:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        reviewer_model: str,
        api_keys: Sequence[str],
        timeout: float,
        max_tokens: int,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.reviewer_model = reviewer_model
        self.api_keys = list(dict.fromkeys(clean_text(value) for value in api_keys if clean_text(value)))
        self.request_index = 0
        self.timeout = max(1.0, float(timeout))
        self.max_tokens = max(256, int(max_tokens))
        if not self.base_url or not self.api_keys:
            raise ProductWriterError("writer base URL and API key pool are required")
        if self.model != "deepseek-v4-flash":
            raise ProductWriterError(f"product writer requires deepseek-v4-flash, got {self.model!r}")
        if self.reviewer_model != "deepseek-v4-pro":
            raise ProductWriterError(f"product writer requires deepseek-v4-pro, got {self.reviewer_model!r}")

    def _request(
        self,
        *,
        model: str,
        system_prompt: str,
        user_payload: Mapping[str, Any],
        stage: str,
    ) -> tuple[str, dict[str, Any]]:
        request_payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))},
            ],
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
        }
        key_index = self.request_index % len(self.api_keys)
        self.request_index += 1
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_keys[key_index]}"},
            method="POST",
        )
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            metadata = {
                "model": model,
                "api_key_index": key_index,
                "latency_seconds": round(time.time() - started, 3),
                "response_sha256": sha256_text(detail),
                "finish_reason": "http_error",
                "http_status": int(exc.code),
                "max_output_tokens": self.max_tokens,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
            raise ProductWriterResponseError(
                f"{stage} HTTP {exc.code}: {detail}",
                response_content=detail,
                request_metadata=metadata,
            ) from exc
        except Exception as exc:
            metadata = {
                "model": model,
                "api_key_index": key_index,
                "latency_seconds": round(time.time() - started, 3),
                "response_sha256": sha256_text(""),
                "finish_reason": "request_error",
                "error_type": exc.__class__.__name__,
                "max_output_tokens": self.max_tokens,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
            raise ProductWriterResponseError(
                f"{stage} request failed: {exc.__class__.__name__}: {exc}",
                request_metadata=metadata,
            ) from exc
        choices = list(response_payload.get("choices") or [])
        usage = dict(response_payload.get("usage") or {})
        raw_response = json.dumps(response_payload, ensure_ascii=False, sort_keys=True)
        if len(choices) != 1:
            raise ProductWriterResponseError(
                f"{stage} returned {len(choices)} choices",
                response_content=raw_response,
                request_metadata={
                    "model": model,
                    "api_key_index": key_index,
                    "latency_seconds": round(time.time() - started, 3),
                    "response_sha256": sha256_text(raw_response),
                    "finish_reason": "invalid_response",
                    "max_output_tokens": self.max_tokens,
                    "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                    "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                    "total_tokens": int(usage.get("total_tokens", 0) or 0),
                },
            )
        content = dict(choices[0].get("message") or {}).get("content")
        if not isinstance(content, str):
            raise ProductWriterResponseError(
                f"{stage} response has no string content",
                response_content=raw_response,
                request_metadata={
                    "model": model,
                    "api_key_index": key_index,
                    "latency_seconds": round(time.time() - started, 3),
                    "response_sha256": sha256_text(raw_response),
                    "finish_reason": "invalid_response",
                    "max_output_tokens": self.max_tokens,
                    "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                    "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                    "total_tokens": int(usage.get("total_tokens", 0) or 0),
                },
            )
        finish_reason = clean_text(choices[0].get("finish_reason"))
        metadata = {
            "model": model,
            "api_key_index": key_index,
            "latency_seconds": round(time.time() - started, 3),
            "response_sha256": sha256_text(content),
            "finish_reason": finish_reason,
            "max_output_tokens": self.max_tokens,
            "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        }
        if finish_reason != "stop":
            raise ProductWriterResponseError(
                f"{stage} did not finish cleanly: finish_reason={finish_reason!r}",
                response_content=content,
                request_metadata=metadata,
            )
        return content, metadata

    def write(
        self,
        *,
        current_message: Mapping[str, Any],
        previous_message: Mapping[str, Any] | None,
        existing_memory_slots: Sequence[Mapping[str, Any]] = (),
        pending_interactions: Sequence[Mapping[str, Any]] = (),
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        current_content = str(current_message.get("content") or "")
        pending_ids = [clean_text(value.get("interaction_id")) for value in pending_interactions]
        message_role = clean_text(current_message.get("role")).lower()
        if message_role == "user":
            selected_model = self.reviewer_model
            selected_prompt = PRO_SYSTEM_PROMPT
            selected_prompt_version = PRO_PROMPT_VERSION
            routing_reason = "authoritative_user_memory"
        elif message_role == "assistant" and pending_interactions:
            selected_model = self.reviewer_model
            selected_prompt = PRO_SYSTEM_PROMPT
            selected_prompt_version = PRO_PROMPT_VERSION
            routing_reason = "pending_interaction_resolution"
        elif message_role == "assistant":
            selected_model = self.model
            selected_prompt = SYSTEM_PROMPT
            selected_prompt_version = PROMPT_VERSION
            routing_reason = "assistant_source_or_interaction"
        else:
            raise ProductWriterError(f"unsupported routed writer role: {message_role!r}")

        previous_payload = (
            {
                "role": clean_text(previous_message.get("role")),
                "timestamp": clean_text(previous_message.get("timestamp")),
                "content": str(previous_message.get("content") or ""),
            }
            if previous_message
            else None
        )

        def invoke(
            content: str,
            *,
            stage: str,
            capacity_range: tuple[int, int] | None,
        ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
            user_payload: dict[str, Any] = {
                "current_message": {
                    "role": clean_text(current_message.get("role")),
                    "timestamp": clean_text(current_message.get("timestamp")),
                    "content": content,
                },
                "previous_message": previous_payload,
                "evidence_spans": compact_writer_evidence_spans(content),
                "source_tokens": [token["text"] for token in exact_source_tokens(content)],
                "existing_memory_slots": [dict(value) for value in existing_memory_slots],
                "pending_interactions": [dict(value) for value in pending_interactions],
            }
            if capacity_range is not None:
                user_payload["capacity_segment"] = {
                    "char_start": capacity_range[0],
                    "char_end": capacity_range[1],
                }
                user_payload["full_current_message_context"] = {
                    "role": clean_text(current_message.get("role")),
                    "timestamp": clean_text(current_message.get("timestamp")),
                    "content": current_content,
                }
            response_content, request_metadata = self._request(
                model=selected_model,
                system_prompt=selected_prompt,
                user_payload=user_payload,
                stage=stage,
            )
            try:
                transport_warnings: list[dict[str, Any]] = []
                wire_output = exact_json_object(response_content, warnings=transport_warnings)
                parsed = validate_writer_output(
                    wire_output,
                    content,
                    message_role=message_role,
                    pending_interaction_ids=pending_ids,
                )
                parsed["validation_warnings"] = [
                    *transport_warnings,
                    *list(parsed.get("validation_warnings") or []),
                ]
            except ProductWriterError as exc:
                raise ProductWriterResponseError(
                    str(exc),
                    response_content=response_content,
                    request_metadata=request_metadata,
                ) from exc
            return parsed, request_metadata, wire_output, response_content

        def annotate_request(
            request_metadata: Mapping[str, Any],
            *,
            attempt: str,
            capacity_range: tuple[int, int] | None,
        ) -> dict[str, Any]:
            annotated = dict(request_metadata)
            annotated["attempt"] = attempt
            if capacity_range is not None:
                annotated["segment_char_start"] = capacity_range[0]
                annotated["segment_char_end"] = capacity_range[1]
            return annotated

        def rebase_segment(parsed: dict[str, Any], capacity_range: tuple[int, int], segment_index: int) -> None:
            segment_start, segment_end = capacity_range
            segment_content = current_content[segment_start:segment_end]
            local_tokens = source_token_matches(segment_content)
            global_tokens = source_token_matches(current_content)

            def original_token_index(char_offset: int) -> int:
                for index, match in enumerate(global_tokens):
                    if match.start() <= char_offset < match.end():
                        return index
                raise ProductWriterError("capacity segment token coordinates do not map to the original source")

            def rebase_item(item: dict[str, Any], facet_field: str | None = None) -> None:
                item["evidence_span_id"] = f"c{segment_index}.{item['evidence_span_id']}"
                item["evidence_char_start"] = segment_start + int(item["evidence_char_start"])
                item["evidence_char_end"] = segment_start + int(item["evidence_char_end"])
                if facet_field is None:
                    return
                for facet in item.get(facet_field) or []:
                    local_start = int(facet["token_start"])
                    local_end = int(facet["token_end"])
                    global_start = segment_start + local_tokens[local_start].start()
                    global_end = segment_start + local_tokens[local_end].start()
                    facet["token_start"] = original_token_index(global_start)
                    facet["token_end"] = original_token_index(global_end)

            for assertion in parsed.get("assertions") or []:
                rebase_item(assertion, "facets")
            for interaction in parsed.get("interactions") or []:
                rebase_item(interaction, "about")
            for resolution in parsed.get("resolutions") or []:
                rebase_item(resolution)

        primary_stage = "writer_primary_pro" if selected_model == self.reviewer_model else "writer_primary_flash"
        requests: list[dict[str, Any]] = []
        wire_outputs: list[dict[str, Any]] = []
        response_hashes: list[str] = []
        duplicate_count = 0
        try:
            parsed, request_metadata, wire_output, response_content = invoke(
                current_content,
                stage=primary_stage,
                capacity_range=None,
            )
            requests.append(annotate_request(request_metadata, attempt="full", capacity_range=None))
            wire_outputs.append({"capacity_range": None, "output": wire_output})
            response_hashes.append(sha256_text(response_content))
            writer_mode = "single_pass_routed"
        except ProductWriterResponseError as initial_error:
            if initial_error.request_metadata.get("finish_reason") != "length":
                raise
            requests.append(annotate_request(initial_error.request_metadata, attempt="full", capacity_range=None))
            response_hashes.append(sha256_text(initial_error.response_content))
            initial_split = split_capacity_range(current_content, 0, len(current_content))
            if initial_split is None:
                raise

            segment_outputs: list[dict[str, Any]] = []
            pending_ranges = [initial_split[0], initial_split[1]]
            while pending_ranges:
                if len(requests) >= 64:
                    raise ProductWriterResponseError(
                        "capacity segmentation exceeded 64 API attempts; source was retained and nothing was committed",
                        request_metadata={"requests": requests},
                    )
                capacity_range = pending_ranges.pop(0)
                segment_content = current_content[capacity_range[0] : capacity_range[1]]
                segment_stage = (
                    "writer_capacity_segment_pro"
                    if selected_model == self.reviewer_model
                    else "writer_capacity_segment_flash"
                )
                try:
                    segment_output, segment_request, segment_wire, segment_response = invoke(
                        segment_content,
                        stage=segment_stage,
                        capacity_range=capacity_range,
                    )
                except ProductWriterResponseError as segment_error:
                    requests.append(
                        annotate_request(
                            segment_error.request_metadata,
                            attempt="capacity_segment",
                            capacity_range=capacity_range,
                        )
                    )
                    response_hashes.append(sha256_text(segment_error.response_content))
                    if segment_error.request_metadata.get("finish_reason") != "length":
                        raise ProductWriterResponseError(
                            str(segment_error),
                            response_content=segment_error.response_content,
                            request_metadata={
                                "requests": requests,
                                "physical_api_attempt_count": len(requests),
                                "terminal_request": requests[-1],
                            },
                        ) from segment_error
                    nested_split = split_capacity_range(current_content, capacity_range[0], capacity_range[1])
                    if nested_split is None:
                        raise ProductWriterResponseError(
                            "an indivisible capacity segment still exceeded the model output limit",
                            response_content=segment_error.response_content,
                            request_metadata={"requests": requests},
                        ) from segment_error
                    pending_ranges = [nested_split[0], nested_split[1], *pending_ranges]
                    continue
                requests.append(
                    annotate_request(
                        segment_request,
                        attempt="capacity_segment",
                        capacity_range=capacity_range,
                    )
                )
                response_hashes.append(sha256_text(segment_response))
                rebase_segment(segment_output, capacity_range, len(segment_outputs))
                segment_outputs.append(segment_output)
                wire_outputs.append(
                    {
                        "capacity_range": {"char_start": capacity_range[0], "char_end": capacity_range[1]},
                        "output": segment_wire,
                    }
                )
            try:
                parsed, duplicate_count = merge_segment_outputs(segment_outputs, message_role=message_role)
            except ProductWriterError as merge_error:
                raise ProductWriterResponseError(
                    str(merge_error),
                    request_metadata={
                        "requests": requests,
                        "physical_api_attempt_count": len(requests),
                        "terminal_request": requests[-1],
                    },
                ) from merge_error
            writer_mode = "capacity_segmented_on_length"

        metadata = {
            "writer_mode": writer_mode,
            "routing_reason": routing_reason,
            "prompt_version": selected_prompt_version,
            "prompt_sha256": sha256_text(selected_prompt),
            "model": selected_model,
            "request": requests[0],
            "requests": requests,
            "api_call_count": len(requests),
            "capacity_segment_count": len(wire_outputs) if writer_mode != "single_pass_routed" else 0,
            "capacity_duplicate_count": duplicate_count,
            "wire_output": wire_outputs[0]["output"] if writer_mode == "single_pass_routed" else None,
            "wire_outputs": wire_outputs,
            "latency_seconds": round(sum(float(request.get("latency_seconds", 0) or 0) for request in requests), 3),
            "response_sha256": sha256_text("|".join(response_hashes)),
            "prompt_tokens": sum(int(request.get("prompt_tokens", 0) or 0) for request in requests),
            "completion_tokens": sum(int(request.get("completion_tokens", 0) or 0) for request in requests),
            "total_tokens": sum(int(request.get("total_tokens", 0) or 0) for request in requests),
        }
        return parsed, metadata


def historical_timestamp(value: Any, message_index: int) -> str:
    text = clean_text(value)
    for pattern in ("%Y/%m/%d (%a) %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M"):
        try:
            base = datetime.strptime(text, pattern).replace(tzinfo=timezone.utc)
            return (base + timedelta(seconds=int(message_index))).isoformat(timespec="seconds")
        except ValueError:
            continue
    raise ProductWriterError(f"unsupported historical timestamp: {text!r}")


def graph_category(memory_type: str) -> str:
    return {
        "identity": "profile",
        "relationship": "profile",
        "routine": "profile",
        "state": "status",
        "plan": "stage_state",
    }.get(memory_type, memory_type)


def graph_facet_type(facet_type: str) -> str:
    return {
        "time": "temporal",
        "quantity": "numeric",
        "location": "entity",
        "role": "role",
    }.get(facet_type, facet_type)


def build_graph_records(
    record_class: Any,
    *,
    scope_id: str,
    turn_index: int,
    session_id: str,
    session_index: int,
    message_id: str,
    message_index: int,
    date: str,
    timestamp: str,
    role: str,
    content: str,
    extraction: Mapping[str, Any] | None,
    actor_metadata: Mapping[str, Any] | None = None,
) -> tuple[list[Any], dict[str, int]]:
    allowed_actor_fields = {
        "actor_provenance_schema",
        "actor_role",
        "origin_kind",
        "capture_method",
        "provenance_strength",
        "agent_id",
        "agent_name",
        "agent_role",
        "agent_specialty",
        "agent_team",
        "target_agent_id",
    }
    actor_provenance = {
        str(key): str(value)
        for key, value in dict(actor_metadata or {}).items()
        if key in allowed_actor_fields and value not in (None, "")
    }
    declared_actor_role = clean_text(actor_provenance.get("actor_role"))
    if declared_actor_role and declared_actor_role != role:
        raise ProductWriterError("actor_role differs from source message role")
    actor_provenance["actor_role"] = role
    event_id = f"event::tmcra:{scope_id}:{message_id}"
    sidecar = {
        "session_id": session_id,
        "session_index": int(session_index),
        "message_id": message_id,
        "message_index": int(message_index),
        "historical_date": date,
        "role": role,
    }
    source_slot = f"source.s{session_index:03d}.m{message_index:03d}"
    source_metadata = {
        "source": "tmcra_v3_product_runtime",
        "authority": origin_authority(role, actor_provenance),
        "writer_schema_version": WRITE_SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "content_variant": "source_message",
        "memory_layer": "fast",
        "node_kind": "immutable_source_message",
        "immutable_evidence_leaf": True,
        "raw_content": content,
        "source_span": content,
        "source_turn_text": content,
        "speaker": role,
        "timestamp": timestamp,
        "session_id": session_id,
        "session_index": int(session_index),
        "message_id": message_id,
        "message_index": int(message_index),
        "historical_date": date,
        "event_id": event_id,
        "source_record_id": f"{source_slot}:{turn_index}",
        "event_signature": f"source:{message_id}",
        "dia_id": f"tmcra:{scope_id}:{message_id}",
        "canonical_slot_key": source_slot,
        "semantic_slot": "source_message",
        "allow_parallel_state": True,
        "memory_gate_decision": "source_grounding",
        "subject_signature": slug(f"source.{message_id}"),
        "sidecar_hint_metadata": sidecar,
        **actor_provenance,
    }
    records: list[Any] = [
        record_class(
            memory_id=f"{source_slot}:{turn_index}",
            category="source",
            slot_key=source_slot,
            value=content,
            relation="dialogue_source",
            anchor_concepts=[role, session_id, date],
            evidence_anchors=[role, session_id, date],
            salience=0.72,
            confidence=1.0,
            source_kind=f"public_dialog_{role}_turn",
            turn_index=turn_index,
            state="evidence",
            metadata=source_metadata,
        )
    ]
    counts = {"source": 1, "semantic": 0, "facet": 0, "interaction": 0}
    if not permits_source_extraction(role, actor_provenance):
        return records, counts
    assertions = list((extraction or {}).get("assertions") or [])
    if not permits_source_extraction(role, actor_provenance) or role != "user":
        assertions = []
    for memory_index_in_message, memory in enumerate(assertions):
        canonical_key = str(memory["canonical_key"])
        entity_key = str(memory["entity_key"])
        attribute_key = str(memory["attribute_key"])
        operation = str(memory["operation"])
        evidence_quote = str(memory["evidence_quote"])
        evidence_char_start = int(memory.get("evidence_char_start", 0) or 0)
        evidence_char_end = int(memory.get("evidence_char_end", 0) or 0)
        memory_type = str(memory["memory_type"])
        assertion_family = str(memory["memory_family"])
        assertion_graph_entity = str(memory["graph_entity_key"])
        relation = str(memory["relation"])
        subject = str(memory["subject"])
        object_value = str(memory["object"])
        slot_key = f"memory.{canonical_key}"
        value_hash = sha256_text(evidence_quote)[:16]
        event_signature = (
            f"{canonical_key}:{message_id}:{value_hash}:"
            f"{evidence_char_start}:{evidence_char_end}:{memory['polarity']}"
        )
        anchors = [subject, relation, object_value]
        for facet in list(memory.get("facets") or []):
            anchors.extend([str(facet["role"]), str(facet["quote"])])
        anchors = [value for value in dict.fromkeys(clean_text(value) for value in anchors) if value]
        metadata = {
            **source_metadata,
            "content_variant": "product_semantic_memory",
            "memory_layer": "fast",
            "node_kind": "atomic_user_assertion",
            "atomic_evidence_leaf": True,
            "authority": origin_authority(role, actor_provenance, derived=True),
            "raw_content": evidence_quote,
            "source_span": evidence_quote,
            "evidence_char_start": int(memory.get("evidence_char_start", 0) or 0),
            "evidence_char_end": int(memory.get("evidence_char_end", 0) or 0),
            "source_turn_text": content,
            "canonical_slot_key": slot_key,
            "semantic_slot": relation,
            "memory_type": memory_type,
            "memory_family": assertion_family,
            "entity_key": entity_key,
            "graph_entity_key": assertion_graph_entity,
            "attribute_key": attribute_key,
            "write_operation": operation,
            "allow_parallel_state": operation == "append",
            "subject": subject,
            "subject_signature": slug(assertion_graph_entity),
            "object": object_value,
            "target_status": str(memory["temporal_status"]),
            "polarity": str(memory["polarity"]),
            "event_signature": event_signature,
            "memory_gate_decision": "model_exact_evidence",
            "llm_write_proposal_index": memory_index_in_message,
        }
        if graph_category(memory_type) == "profile":
            metadata["profile_type"] = memory_type
            metadata["profile_domain"] = assertion_graph_entity
        parent = record_class(
            memory_id=f"{slot_key}:{turn_index}:{memory_index_in_message}",
            category=graph_category(memory_type),
            slot_key=slot_key,
            value=evidence_quote,
            relation=relation,
            anchor_concepts=anchors,
            evidence_anchors=[evidence_quote],
            salience=0.92,
            confidence=1.0,
            source_kind="public_dialog_semantic_memory",
            turn_index=turn_index,
            state="active",
            metadata=metadata,
        )
        records.append(parent)
        counts["semantic"] += 1
        for facet_index, facet in enumerate(list(memory.get("facets") or [])):
            facet_type = graph_facet_type(str(facet["type"]))
            facet_role = str(facet["role"])
            facet_quote = str(facet["quote"])
            facet_slot = f"{slot_key}.facet.{message_id}.{memory_index_in_message}.{facet_index}"
            facet_metadata = {
                **source_metadata,
                "content_variant": "event_facet_write",
                "memory_layer": "fast",
                "node_kind": "atomic_assertion_facet",
                "raw_content": facet_quote,
                "source_span": facet_quote,
                "facet_source_span": facet_quote,
                "facet_type": facet_type,
                "facet_role": facet_role,
                "facet_value": facet_quote,
                "facet_parent_slot_key": slot_key,
                "facet_parent_event_signature": event_signature,
                "canonical_slot_key": facet_slot,
                "semantic_slot": facet_role,
                "allow_parallel_state": True,
                "subject": subject,
                "subject_signature": slug(assertion_graph_entity),
                "graph_entity_key": assertion_graph_entity,
                "event_signature": f"{event_signature}:facet:{facet_index}",
                "memory_gate_decision": "model_exact_evidence_facet",
            }
            records.append(
                record_class(
                    memory_id=f"{facet_slot}:{turn_index}",
                    category={"temporal": "time", "state": "status"}.get(facet_type, "fact"),
                    slot_key=facet_slot,
                    value=facet_quote,
                    relation=f"has_{facet_type}_facet",
                    anchor_concepts=[facet_role, facet_quote, subject],
                    evidence_anchors=[facet_quote],
                    salience=0.84,
                    confidence=1.0,
                    source_kind="public_dialog_semantic_facet",
                    turn_index=turn_index,
                    state="evidence",
                    metadata=facet_metadata,
                )
            )
            counts["facet"] += 1
    interactions = list((extraction or {}).get("interactions") or [])
    for interaction_index, interaction in enumerate(interactions):
        interaction_type = str(interaction["interaction_type"])
        interaction_status = str(interaction["status"])
        evidence_quote = str(interaction["evidence_quote"])
        intent = str(interaction["intent"])
        interaction_slot = f"interaction.{message_id}.{interaction_index}"
        interaction_memory_id = f"{interaction_slot}:{turn_index}"
        interaction_metadata = {
            **source_metadata,
            "content_variant": "product_interaction",
            "memory_layer": "fast",
            "node_kind": "atomic_interaction",
            "atomic_evidence_leaf": True,
            "raw_content": evidence_quote,
            "source_span": evidence_quote,
            "evidence_char_start": int(interaction.get("evidence_char_start", 0) or 0),
            "evidence_char_end": int(interaction.get("evidence_char_end", 0) or 0),
            "source_turn_text": content,
            "canonical_slot_key": interaction_slot,
            "semantic_slot": intent,
            "interaction_id": interaction_memory_id,
            "interaction_type": interaction_type,
            "interaction_status": interaction_status,
            "interaction_speaker": role,
            "allow_parallel_state": True,
            "subject_signature": slug(f"interaction.{message_id}.{interaction_index}"),
            "event_signature": f"interaction:{message_id}:{interaction_index}",
            "memory_gate_decision": "model_exact_interaction_evidence",
        }
        about = list(interaction.get("about") or [])
        interaction_metadata["about"] = about
        interaction_anchors = [intent, interaction_type]
        for facet in about:
            interaction_anchors.extend([str(facet["role"]), str(facet["quote"])])
        records.append(
            record_class(
                memory_id=interaction_memory_id,
                category="question" if interaction_type in {"question", "clarification"} else "interaction_intent",
                slot_key=interaction_slot,
                value=evidence_quote,
                relation=intent,
                anchor_concepts=[
                    value for value in dict.fromkeys(clean_text(value) for value in interaction_anchors) if value
                ],
                evidence_anchors=[evidence_quote],
                salience=0.82,
                confidence=1.0,
                source_kind=(
                    "public_dialog_question"
                    if interaction_type in {"question", "clarification"}
                    else "public_dialog_interaction"
                ),
                turn_index=turn_index,
                state="evidence",
                metadata=interaction_metadata,
            )
        )
        counts["interaction"] += 1
    return records, counts


def ingest_product_message(
    adapter: Any,
    record_class: Any,
    edge_class: Any,
    *,
    scope_id: str,
    session_id: str,
    session_index: int,
    message_id: str,
    message_index: int,
    date: str,
    timestamp: str,
    role: str,
    content: str,
    extraction: Mapping[str, Any] | None,
) -> dict[str, Any]:
    adapter._reload_graph()
    turn_index = adapter.graph.next_turn()
    records, counts = build_graph_records(
        record_class,
        scope_id=scope_id,
        turn_index=turn_index,
        session_id=session_id,
        session_index=session_index,
        message_id=message_id,
        message_index=message_index,
        date=date,
        timestamp=timestamp,
        role=role,
        content=content,
        extraction=extraction,
    )
    stored_ids = adapter.graph.add_records(records)
    source_ids = [record.memory_id for record in records if record.category == "source"]
    if len(source_ids) != 1 or source_ids[0] not in stored_ids:
        raise ProductWriterError(f"{message_id}: immutable source record was not persisted")
    provenance_count = 0
    for record in records:
        content_variant = clean_text(dict(record.metadata or {}).get("content_variant"))
        if content_variant not in {"product_semantic_memory", "product_interaction"}:
            continue
        if record.memory_id not in stored_ids or record.memory_id not in adapter.graph.records_by_id:
            raise ProductWriterError(
                f"{message_id}: semantic record {record.memory_id!r} was merged or dropped before provenance"
            )
        adapter.graph._upsert_memory_edge(
            edge_class(
                edge_id=f"{record.memory_id}->{source_ids[0]}:grounded_in",
                source_memory_id=record.memory_id,
                target_memory_id=source_ids[0],
                edge_type="grounded_in",
                score=1.0,
                model_score=0.0,
                evidence_turn=turn_index,
                evidence=clean_text(dict(record.metadata or {}).get("source_span")) or record.value,
                metadata={
                    "edge_source": "product_writer_provenance",
                    "message_id": message_id,
                    "source_record_id": source_ids[0],
                },
            )
        )
        provenance_count += 1
    resolution_rows: list[dict[str, str]] = []
    for resolution in list((extraction or {}).get("resolutions") or []):
        interaction_id = str(resolution["interaction_id"])
        resolution_state = str(resolution["resolution"])
        evidence_quote = str(resolution["evidence_quote"])
        interaction_record = adapter.graph.records_by_id.get(interaction_id)
        if interaction_record is None:
            raise ProductWriterError(f"{message_id}: resolution target does not exist: {interaction_id}")
        interaction_metadata = dict(interaction_record.metadata or {})
        if clean_text(interaction_metadata.get("content_variant")) != "product_interaction":
            raise ProductWriterError(f"{message_id}: resolution target is not an interaction: {interaction_id}")
        previous_status = clean_text(interaction_metadata.get("interaction_status")) or "open"
        next_status = "resolved" if resolution_state == "resolved" else ("partial" if resolution_state == "partial" else previous_status)
        history = list(interaction_metadata.get("resolution_history") or [])
        history.append(
            {
                "message_id": message_id,
                "source_record_id": source_ids[0],
                "speaker": role,
                "timestamp": timestamp,
                "resolution": resolution_state,
                "evidence_quote": evidence_quote,
            }
        )
        interaction_metadata.update(
            {
                "interaction_status": next_status,
                "resolution_history": history,
                "last_resolution": resolution_state,
                "last_resolution_message_id": message_id,
                "last_resolution_source_record_id": source_ids[0],
                "last_resolution_timestamp": timestamp,
            }
        )
        if next_status == "resolved":
            interaction_metadata["resolved_at"] = timestamp
            interaction_metadata["resolved_by_message_id"] = message_id
            interaction_metadata["resolved_by_source_record_id"] = source_ids[0]
        interaction_record.metadata = interaction_metadata
        edge_type = {
            "resolved": "answered_by",
            "partial": "partially_answered_by",
            "unresolved": "responded_without_resolution",
        }[resolution_state]
        adapter.graph._upsert_memory_edge(
            edge_class(
                edge_id=f"{interaction_id}->{source_ids[0]}:{edge_type}",
                source_memory_id=interaction_id,
                target_memory_id=source_ids[0],
                edge_type=edge_type,
                score=1.0 if resolution_state == "resolved" else (0.72 if resolution_state == "partial" else 0.35),
                model_score=0.0,
                evidence_turn=turn_index,
                evidence=evidence_quote,
                metadata={
                    "edge_source": "product_writer_resolution",
                    "resolution": resolution_state,
                    "previous_status": previous_status,
                    "next_status": next_status,
                    "message_id": message_id,
                    "speaker": role,
                },
            )
        )
        resolution_rows.append(
            {
                "interaction_id": interaction_id,
                "resolution": resolution_state,
                "previous_status": previous_status,
                "next_status": next_status,
                "edge_type": edge_type,
            }
        )
    adapter.graph.record_turn(
        turn_kind="memory_write",
        text=content,
        turn_index=turn_index,
        record_ids=stored_ids,
        speaker=role,
        assistant_text="",
        metadata={
            "source": "tmcra_v3_product_runtime",
            "writer_schema_version": WRITE_SCHEMA_VERSION,
            "prompt_version": PROMPT_VERSION,
            "session_id": session_id,
            "session_index": int(session_index),
            "message_id": message_id,
            "message_index": int(message_index),
            "historical_date": date,
            "timestamp": timestamp,
            "source_record_id": source_ids[0],
        },
    )
    adapter._persist_graph()
    return {
        "turn_index": turn_index,
        "source_record_id": source_ids[0],
        "stored_ids": stored_ids,
        "resolution_count": len(resolution_rows),
        "provenance_edge_count": provenance_count,
        "resolutions": resolution_rows,
        **counts,
    }


def database_counts(path: Path, scope_id: str) -> dict[str, int]:
    with sqlite3.connect(path) as connection:
        fast_records = sum(
            1
            for (metadata_json,) in connection.execute(
                "SELECT metadata_json FROM records WHERE scope_id=?",
                (scope_id,),
            )
            if clean_text(json.loads(metadata_json or "{}").get("memory_layer"))
            == "fast"
        )
        non_slow_edges = sum(
            1
            for (metadata_json,) in connection.execute(
                "SELECT metadata_json FROM memory_edges WHERE scope_id=?",
                (scope_id,),
            )
            if clean_text(json.loads(metadata_json or "{}").get("edge_source"))
            != "slow_graph_control_plane"
        )
        return {
            "records": fast_records,
            "memory_edges": non_slow_edges,
            "audit_turn_log": int(
                connection.execute(
                    "SELECT COUNT(*) FROM audit_turn_log WHERE scope_id=?",
                    (scope_id,),
                ).fetchone()[0]
            ),
            "product_source_journal": int(
                connection.execute(
                    "SELECT COUNT(*) FROM product_source_journal WHERE scope_id=?",
                    (scope_id,),
                ).fetchone()[0]
            ),
        }


def reconstruct_persisted_message(
    graph: Any,
    *,
    message_id: str,
    extraction: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    records = [
        record
        for record in graph.records_by_id.values()
        if clean_text(dict(record.metadata or {}).get("message_id")) == message_id
    ]
    source_records = [
        record
        for record in records
        if clean_text(dict(record.metadata or {}).get("content_variant")) == "source_message"
    ]
    if not records and not source_records:
        return None
    if len(source_records) != 1:
        raise ProductWriterError(f"{message_id}: staged graph commit has {len(source_records)} source records")
    source_record = source_records[0]
    variants = [clean_text(dict(record.metadata or {}).get("content_variant")) for record in records]
    provenance_count = sum(
        clean_text(dict(edge.metadata or {}).get("edge_source")) == "product_writer_provenance"
        and edge.target_memory_id == source_record.memory_id
        for edge in graph.memory_edges.values()
    )
    return {
        "turn_index": int(source_record.turn_index),
        "source_record_id": source_record.memory_id,
        "stored_ids": [record.memory_id for record in records],
        "resolution_count": len(list((extraction or {}).get("resolutions") or [])),
        "provenance_edge_count": provenance_count,
        "resolutions": [],
        "source": variants.count("source_message"),
        "semantic": variants.count("product_semantic_memory"),
        "facet": variants.count("event_facet_write"),
        "interaction": variants.count("product_interaction"),
    }


def _ensure_source_journal_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS product_source_journal (
            scope_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            session_index INTEGER NOT NULL,
            message_index INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            enrichment_status TEXT NOT NULL,
            source_record_id TEXT NOT NULL,
            error TEXT NOT NULL,
            extraction_json TEXT NOT NULL DEFAULT '',
            call_metadata_json TEXT NOT NULL DEFAULT '',
            persisted_json TEXT NOT NULL DEFAULT '',
            claim_owner TEXT NOT NULL DEFAULT '',
            claim_token TEXT NOT NULL DEFAULT '',
            claim_expires_at TEXT NOT NULL DEFAULT '',
            api_call_started_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (scope_id, message_id)
        )
        """
    )
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(product_source_journal)")}
    required_columns = {
        "extraction_json": "TEXT NOT NULL DEFAULT ''",
        "call_metadata_json": "TEXT NOT NULL DEFAULT ''",
        "persisted_json": "TEXT NOT NULL DEFAULT ''",
        "claim_owner": "TEXT NOT NULL DEFAULT ''",
        "claim_token": "TEXT NOT NULL DEFAULT ''",
        "claim_expires_at": "TEXT NOT NULL DEFAULT ''",
        "api_call_started_at": "TEXT NOT NULL DEFAULT ''",
    }
    for column, definition in required_columns.items():
        if column not in columns:
            connection.execute(f"ALTER TABLE product_source_journal ADD COLUMN {column} {definition}")


def _source_journal_claim_expired(claim_expires_at: str, *, now: datetime) -> bool:
    if not claim_expires_at:
        return True
    try:
        expires_at = datetime.fromisoformat(claim_expires_at)
    except ValueError as exc:
        raise ProductWriterError("source journal has malformed claim lease timestamp") from exc
    if expires_at.tzinfo is None:
        raise ProductWriterError("source journal claim lease timestamp must include a timezone")
    return expires_at <= now


def claim_source_journal(
    path: Path,
    *,
    scope_id: str,
    message_id: str,
    claim_owner: str,
    lease_seconds: int = SOURCE_JOURNAL_CLAIM_LEASE_SECONDS,
) -> str | None:
    owner = clean_text(claim_owner)
    if not owner:
        raise ValueError("source journal claim owner is required")
    if int(lease_seconds) <= 0:
        raise ValueError("source journal claim lease must be positive")
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(seconds=int(lease_seconds))).isoformat(timespec="seconds")
    token = uuid.uuid4().hex
    with closing(sqlite3.connect(path, timeout=30, isolation_level=None)) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            _ensure_source_journal_schema(connection)
            row = connection.execute(
                """
                SELECT enrichment_status,claim_token,claim_expires_at,
                       extraction_json,api_call_started_at
                FROM product_source_journal WHERE scope_id=? AND message_id=?
                """,
                (scope_id, message_id),
            ).fetchone()
            if row is None:
                raise ProductWriterError(f"{message_id}: source journal row was not found for claim")
            if str(row[0]) != "pending":
                connection.commit()
                return None
            existing_token = str(row[1] or "")
            if existing_token and not _source_journal_claim_expired(str(row[2] or ""), now=now):
                connection.commit()
                return None
            if (
                existing_token
                and _source_journal_claim_expired(str(row[2] or ""), now=now)
                and str(row[4] or "")
                and not str(row[3] or "")
            ):
                connection.execute(
                    "UPDATE product_source_journal SET enrichment_status='failed',"
                    "error='expired Writer claim has an uncertain external-call outcome; explicit resume required',"
                    "claim_owner='',claim_token='',claim_expires_at='',updated_at=? "
                    "WHERE scope_id=? AND message_id=? AND enrichment_status='pending' "
                    "AND claim_token=?",
                    (
                        now.isoformat(timespec="seconds"),
                        scope_id,
                        message_id,
                        existing_token,
                    ),
                )
                connection.commit()
                raise ProductWriterError(
                    f"{message_id}: expired Writer claim has an uncertain external-call outcome; "
                    "explicit resume is required before replay"
                )
            cursor = connection.execute(
                """
                UPDATE product_source_journal
                SET claim_owner=?, claim_token=?, claim_expires_at=?, updated_at=?
                WHERE scope_id=? AND message_id=? AND enrichment_status='pending'
                """,
                (owner, token, expires_at, now.isoformat(timespec="seconds"), scope_id, message_id),
            )
            if cursor.rowcount != 1:
                raise ProductWriterError(f"{message_id}: source journal claim was lost")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return token


def renew_source_journal_claim(
    path: Path,
    *,
    scope_id: str,
    message_id: str,
    claim_owner: str,
    claim_token: str,
    lease_seconds: int,
) -> None:
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(seconds=int(lease_seconds))).isoformat(timespec="seconds")
    now_text = now.isoformat(timespec="seconds")
    with closing(sqlite3.connect(path, timeout=30, isolation_level=None)) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            renewed = connection.execute(
                "UPDATE product_source_journal SET claim_expires_at=?,updated_at=? "
                "WHERE scope_id=? AND message_id=? AND enrichment_status='pending' "
                "AND claim_owner=? AND claim_token=? AND claim_expires_at>=?",
                (
                    expires_at,
                    now_text,
                    scope_id,
                    message_id,
                    claim_owner,
                    claim_token,
                    now_text,
                ),
            )
            if renewed.rowcount != 1:
                raise ProductWriterError(f"{message_id}: Writer claim could not be renewed")
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def mark_source_api_call_started(
    path: Path,
    *,
    scope_id: str,
    message_id: str,
    claim_owner: str,
    claim_token: str,
) -> None:
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(sqlite3.connect(path)) as connection, connection:
        marked = connection.execute(
            f"UPDATE product_source_journal SET api_call_started_at=?,updated_at=? "
            f"WHERE {_claimed_journal_where()} AND extraction_json=''",
            (
                updated_at,
                updated_at,
                scope_id,
                message_id,
                claim_owner,
                claim_token,
                updated_at,
            ),
        )
        if marked.rowcount != 1:
            raise ProductWriterError(f"{message_id}: Writer API-call marker could not be staged")


def call_with_source_claim_heartbeat(
    call: Callable[[], T],
    *,
    path: Path,
    scope_id: str,
    message_id: str,
    claim_owner: str,
    claim_token: str,
    lease_seconds: int,
) -> T:
    stop = threading.Event()
    errors: list[Exception] = []
    interval = max(0.1, min(30.0, int(lease_seconds) / 3.0))

    def heartbeat() -> None:
        while not stop.wait(interval):
            try:
                renew_source_journal_claim(
                    path,
                    scope_id=scope_id,
                    message_id=message_id,
                    claim_owner=claim_owner,
                    claim_token=claim_token,
                    lease_seconds=lease_seconds,
                )
            except Exception as exc:
                errors.append(exc)
                stop.set()

    thread = threading.Thread(
        target=heartbeat,
        name=f"writer-lease-{message_id}",
        daemon=True,
    )
    thread.start()
    try:
        result = call()
    finally:
        stop.set()
        thread.join(timeout=max(1.0, interval + 1.0))
    if thread.is_alive():
        raise ProductWriterError(f"{message_id}: Writer claim heartbeat did not stop")
    if errors:
        raise ProductWriterError(f"{message_id}: Writer claim heartbeat failed: {errors[0]}")
    renew_source_journal_claim(
        path,
        scope_id=scope_id,
        message_id=message_id,
        claim_owner=claim_owner,
        claim_token=claim_token,
        lease_seconds=lease_seconds,
    )
    return result


def _claimed_journal_where() -> str:
    return (
        "scope_id=? AND message_id=? AND enrichment_status='pending' "
        "AND claim_owner=? AND claim_token=? AND claim_expires_at>?"
    )


def journal_source_message(
    path: Path,
    *,
    scope_id: str,
    session_id: str,
    session_index: int,
    message_id: str,
    message_index: int,
    timestamp: str,
    role: str,
    content: str,
) -> None:
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(sqlite3.connect(path)) as connection, connection:
        _ensure_source_journal_schema(connection)
        existing = connection.execute(
            "SELECT content_sha256, enrichment_status FROM product_source_journal WHERE scope_id=? AND message_id=?",
            (scope_id, message_id),
        ).fetchone()
        if existing is not None:
            if str(existing[0]) != sha256_text(content):
                raise ProductWriterError(f"{message_id}: source journal content changed")
            raise ProductWriterError(f"{message_id}: source journal row already exists with status={existing[1]}")
        connection.execute(
            """
            INSERT INTO product_source_journal (
                scope_id, message_id, session_id, session_index, message_index, timestamp, role, content,
                content_sha256, enrichment_status, source_record_id, error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', '', '', ?, ?)
            """,
            (
                scope_id,
                message_id,
                session_id,
                int(session_index),
                int(message_index),
                timestamp,
                role,
                content,
                sha256_text(content),
                created_at,
                created_at,
            ),
        )


def stage_source_enrichment(
    path: Path,
    *,
    scope_id: str,
    message_id: str,
    claim_owner: str,
    claim_token: str,
    extraction: Mapping[str, Any] | None,
    call_metadata: Mapping[str, Any],
) -> None:
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(sqlite3.connect(path)) as connection, connection:
        cursor = connection.execute(
            f"""
            UPDATE product_source_journal
            SET extraction_json=?, call_metadata_json=?, updated_at=?
            WHERE {_claimed_journal_where()}
            """,
            (
                json.dumps(extraction, ensure_ascii=False, sort_keys=True),
                json.dumps(dict(call_metadata), ensure_ascii=False, sort_keys=True),
                updated_at,
                scope_id,
                message_id,
                claim_owner,
                claim_token,
                updated_at,
            ),
        )
        if cursor.rowcount != 1:
            raise ProductWriterError(f"{message_id}: source journal could not stage Writer output")


def stage_source_persisted(
    path: Path,
    *,
    scope_id: str,
    message_id: str,
    claim_owner: str,
    claim_token: str,
    persisted: Mapping[str, Any],
) -> None:
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(sqlite3.connect(path)) as connection, connection:
        cursor = connection.execute(
            f"""
            UPDATE product_source_journal
            SET persisted_json=?, updated_at=?
            WHERE {_claimed_journal_where()}
            """,
            (
                json.dumps(dict(persisted), ensure_ascii=False, sort_keys=True),
                updated_at,
                scope_id,
                message_id,
                claim_owner,
                claim_token,
                updated_at,
            ),
        )
        if cursor.rowcount != 1:
            raise ProductWriterError(f"{message_id}: source journal could not stage graph commit")


def finish_source_journal(
    path: Path,
    *,
    scope_id: str,
    message_id: str,
    claim_owner: str,
    claim_token: str,
    status: str,
    source_record_id: str = "",
    error: str = "",
) -> None:
    if status not in {"enriched", "enriched_with_warnings", "failed"}:
        raise ValueError(f"unsupported source journal status: {status}")
    normalized_error = clean_text(error)
    if status == "enriched_with_warnings":
        try:
            warning_rows = json.loads(normalized_error)
        except json.JSONDecodeError as exc:
            raise ProductWriterError(
                f"{message_id}: warning journal payload must be valid JSON"
            ) from exc
        if not isinstance(warning_rows, list) or not warning_rows:
            raise ProductWriterError(
                f"{message_id}: warning journal payload must be a nonempty list"
            )
        stored_error = json.dumps(
            warning_rows, ensure_ascii=False, sort_keys=True
        )
    else:
        stored_error = normalized_error[:1000]
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(sqlite3.connect(path)) as connection, connection:
        cursor = connection.execute(
            f"""
            UPDATE product_source_journal
            SET enrichment_status=?, source_record_id=?, error=?, claim_owner='', claim_token='',
                claim_expires_at='', updated_at=?
            WHERE {_claimed_journal_where()}
            """,
            (
                status,
                source_record_id,
                stored_error,
                updated_at,
                scope_id,
                message_id,
                claim_owner,
                claim_token,
                updated_at,
            ),
        )
        if cursor.rowcount != 1:
            raise ProductWriterError(f"{message_id}: source journal pending row was not found")


def repair_warning_journal_payloads(path: Path, *, scope_id: str) -> int:
    repaired = 0
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT message_id,error,extraction_json FROM product_source_journal "
            "WHERE scope_id=? AND enrichment_status='enriched_with_warnings'",
            (scope_id,),
        ).fetchall()
        for row in rows:
            extraction = json.loads(row["extraction_json"] or "{}")
            warnings = extraction.get("validation_warnings")
            if not isinstance(warnings, list) or not warnings:
                raise ProductWriterError(
                    f"{row['message_id']}: staged extraction lacks warning payload"
                )
            expected = json.dumps(warnings, ensure_ascii=False, sort_keys=True)
            if row["error"] == expected:
                continue
            connection.execute(
                "UPDATE product_source_journal SET error=?,updated_at=? "
                "WHERE scope_id=? AND message_id=?",
                (expected, updated_at, scope_id, row["message_id"]),
            )
            repaired += 1
    return repaired


def source_journal_row(path: Path, *, scope_id: str, message_id: str) -> dict[str, str] | None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='product_source_journal'"
        ).fetchone()
        if table_exists is None:
            return None
        _ensure_source_journal_schema(connection)
        row = connection.execute(
            """
            SELECT content_sha256, enrichment_status, source_record_id, error,
                   extraction_json, call_metadata_json, persisted_json,
                   claim_owner, claim_token, claim_expires_at, api_call_started_at
            FROM product_source_journal WHERE scope_id=? AND message_id=?
            """,
            (scope_id, message_id),
        ).fetchone()
    return {key: str(row[key] or "") for key in row.keys()} if row is not None else None


def decode_journal_json(raw_value: str, *, message_id: str, field: str) -> Any:
    try:
        return json.loads(raw_value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProductWriterError(f"{message_id}: staged journal field {field} is malformed") from exc


def call_metadata_from_log(row: Mapping[str, Any]) -> dict[str, Any]:
    metadata = dict(row)
    for field in (
        "question_id",
        "scope_id",
        "session_index",
        "message_index",
        "message_id",
        "message_role",
        "content_sha256",
        "assertion_count",
        "interaction_count",
        "resolution_count",
        "validated_output",
        "persisted",
    ):
        metadata.pop(field, None)
    return metadata


def reopen_failed_source_journal(path: Path, *, scope_id: str, message_id: str) -> None:
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(sqlite3.connect(path)) as connection, connection:
        cursor = connection.execute(
            """
            UPDATE product_source_journal
            SET enrichment_status='pending', source_record_id='', error='', claim_owner='', claim_token='',
                claim_expires_at='', api_call_started_at='', updated_at=?
            WHERE scope_id=? AND message_id=? AND enrichment_status='failed'
            """,
            (updated_at, scope_id, message_id),
        )
        if cursor.rowcount != 1:
            raise ProductWriterError(f"{message_id}: failed source journal row was not found")


def writer_slot_candidates(graph: Any, current_message: str, *, limit: int = 64) -> list[dict[str, Any]]:
    by_key: dict[str, Any] = {}
    for record in graph.records_by_id.values():
        metadata = dict(record.metadata or {})
        if clean_text(metadata.get("content_variant")) != "product_semantic_memory":
            continue
        if clean_text(metadata.get("write_operation")) != "replace":
            continue
        if clean_text(record.state).lower() not in {"active", "parallel_active"}:
            continue
        canonical_key = normalize_canonical_key(clean_text(metadata.get("canonical_slot_key")).removeprefix("memory."))
        if not canonical_key:
            continue
        existing = by_key.get(canonical_key)
        if existing is None or int(record.turn_index) > int(existing.turn_index):
            by_key[canonical_key] = record
    if not by_key:
        return []

    def tokens(value: Any) -> set[str]:
        return set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", str(value or "").lower()))

    query_tokens = tokens(current_message)
    scored: list[tuple[float, int, str, Any]] = []
    for canonical_key, record in by_key.items():
        metadata = dict(record.metadata or {})
        candidate_tokens = tokens(
            " ".join(
                [
                    canonical_key.replace(".", " "),
                    clean_text(record.relation),
                    clean_text(metadata.get("object")),
                    clean_text(record.value),
                ]
            )
        )
        overlap = len(query_tokens & candidate_tokens) / max(1, len(query_tokens))
        scored.append((overlap, int(record.turn_index), canonical_key, record))
    relevant = sorted((row for row in scored if row[0] > 0), key=lambda row: (row[0], row[1]), reverse=True)[:48]
    recent = sorted(scored, key=lambda row: row[1], reverse=True)[:16]
    selected: list[tuple[float, int, str, Any]] = []
    seen: set[str] = set()
    for row in [*relevant, *recent]:
        if row[2] in seen:
            continue
        seen.add(row[2])
        selected.append(row)
        if len(selected) >= max(1, int(limit)):
            break
    return [
        {
            "canonical_key": canonical_key,
            "entity_key": clean_text(dict(record.metadata or {}).get("entity_key")),
            "attribute_key": clean_text(dict(record.metadata or {}).get("attribute_key")),
            "memory_type": clean_text(dict(record.metadata or {}).get("memory_type")),
            "memory_family": clean_text(dict(record.metadata or {}).get("memory_family")),
            "relation": clean_text(record.relation),
            "current_evidence_quote": str(record.value),
        }
        for _, _, canonical_key, record in selected
    ]


def pending_interaction_candidates(
    graph: Any,
    *,
    current_role: str,
    session_id: str,
    session_index: int,
    current_message_index: int,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    candidates: list[Any] = []
    for record in graph.records_by_id.values():
        metadata = dict(record.metadata or {})
        if clean_text(metadata.get("content_variant")) != "product_interaction":
            continue
        if clean_text(metadata.get("session_id")) != session_id:
            continue
        if int(metadata.get("session_index", -1)) != int(session_index):
            continue
        if int(metadata.get("message_index", -1)) >= int(current_message_index):
            continue
        if clean_text(metadata.get("interaction_status")) not in {"open", "partial"}:
            continue
        if clean_text(metadata.get("interaction_speaker")) == current_role:
            continue
        candidates.append(record)
    candidates.sort(key=lambda record: int(record.turn_index), reverse=True)
    rows = [
        {
            "interaction_id": clean_text(dict(record.metadata or {}).get("interaction_id")) or record.memory_id,
            "interaction_type": clean_text(dict(record.metadata or {}).get("interaction_type")),
            "status": clean_text(dict(record.metadata or {}).get("interaction_status")),
            "speaker": clean_text(dict(record.metadata or {}).get("interaction_speaker")),
            "intent": clean_text(record.relation),
            "evidence_quote": str(record.value),
            "about": list(dict(record.metadata or {}).get("about") or []),
        }
        for record in candidates
    ]
    return rows if limit is None else rows[: max(1, int(limit))]


def main() -> int:
    parser = argparse.ArgumentParser(description="TMCRA V3 product message writer with strict DeepSeek extraction")
    parser.add_argument("--input", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    input_path = Path(args.input).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "native_memory.sqlite3"
    if db_path.exists() and not args.resume:
        raise FileExistsError(f"product writer requires a fresh database: {db_path}")
    if args.resume and not db_path.exists():
        raise FileNotFoundError(f"product writer resume database does not exist: {db_path}")
    rows = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise ProductWriterError("writer input must be a non-empty JSON array")
    for row in rows:
        forbidden = sorted(FORBIDDEN_WRITER_FIELDS & set(row))
        if forbidden:
            raise ProductWriterError(f"writer input contains query/evaluation fields: {forbidden}")

    base_url = clean_text(os.getenv("TMCRA_WRITER_BASE_URL"))
    model = clean_text(os.getenv("TMCRA_WRITER_MODEL"))
    reviewer_model = clean_text(os.getenv("TMCRA_WRITER_REVIEW_MODEL"))
    api_keys = [
        clean_text(value)
        for value in os.getenv("TMCRA_WRITER_API_KEY_POOL", "").split(",")
        if clean_text(value)
    ]
    if not base_url or not model or not reviewer_model or not api_keys:
        raise ProductWriterError(
            "explicit TMCRA writer base URL, Flash model, Pro model, and API key pool are required"
        )
    writer = DeepSeekProductWriter(
        base_url=base_url,
        model=model,
        reviewer_model=reviewer_model,
        api_keys=api_keys,
        timeout=float(os.getenv("TMCRA_WRITER_TIMEOUT_SECONDS", "180")),
        max_tokens=int(os.getenv("TMCRA_WRITER_MAX_TOKENS", "8192")),
    )
    try:
        journal_claim_lease_seconds = int(
            os.getenv("TMCRA_WRITER_CLAIM_LEASE_SECONDS", str(SOURCE_JOURNAL_CLAIM_LEASE_SECONDS))
        )
    except ValueError as exc:
        raise ProductWriterError("TMCRA_WRITER_CLAIM_LEASE_SECONDS must be an integer") from exc
    if journal_claim_lease_seconds <= 0:
        raise ProductWriterError("TMCRA_WRITER_CLAIM_LEASE_SECONDS must be positive")
    journal_claim_owner = f"writer:{os.getpid()}:{uuid.uuid4().hex}"

    repo = Path(args.repo).resolve()
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    os.environ.update(
        {
            "TMCRA_PROFILE_CONSOLIDATOR_ENABLED": "0",
            "TMCRA_LEGACY_PROFILE_LAYER_ENABLED": "0",
            "TMCRA_WRITE_EMBEDDER_INDEX_MODE": "off",
            "TMCRA_EMBEDDER_INDEX_RECALL_MODE": "off",
            "TMCRA_EMBEDDER_PRE_RECALL_MODE": "off",
            "TMCRA_EMBEDDER_FUSION_MODE": "off",
            "TMCRA_MEMORY_ROUTER_MODE": "off",
            "TMCRA_INJECTION_PLANNER_MODE": "off",
            "TMCRA_TEMPORAL_LAYER_MODE": "off",
            "TMCRA_TEMPORAL_ROUTER_MODE": "off",
            "TMCRA_DEEPSEEK_GRAPH_MODEL_MODE": "off",
            "TMCRA_TOPIC_BUCKET_MODE": "off",
            "TMCRA_MULTI_UNIT_CHAIN_SLOT_MODE": "off",
            "TMCRA_UNIT_COVERAGE_PACK_MODE": "off",
        }
    )
    from experiments.replacement.adapters.memory_adapters import GraphSessionMemoryAdapter
    from experiments.replacement.memory_graph import SessionMemoryEdgeV2, SessionMemoryRecordV2

    message_total = sum(len(session or []) for row in rows for session in list(row.get("haystack_sessions") or []))
    started = time.time()
    call_log_path = out_dir / "product_writer_calls.jsonl"
    message_log_path = out_dir / "product_write_messages.jsonl"
    failure_log_path = out_dir / "product_writer_failures.jsonl"
    recovery_log_path = out_dir / "product_writer_resume_repairs.jsonl"
    journal_recovery_log_path = out_dir / "product_writer_journal_repairs.jsonl"
    jsonl_tail_repairs = 0
    journal_warning_repairs = 0
    existing_calls: dict[tuple[str, str], dict[str, Any]] = {}
    existing_messages: dict[tuple[str, str], dict[str, Any]] = {}
    recoverable_extractions: dict[tuple[str, str], dict[str, Any]] = {}
    if args.resume:
        call_rows, call_repaired = load_jsonl_for_resume(call_log_path)
        message_rows, message_repaired = load_jsonl_for_resume(message_log_path)
        failure_rows_for_resume, failure_repaired = load_jsonl_for_resume(failure_log_path)
        repaired_paths = [
            path.name
            for path, repaired in (
                (call_log_path, call_repaired),
                (message_log_path, message_repaired),
                (failure_log_path, failure_repaired),
            )
            if repaired
        ]
        jsonl_tail_repairs = len(repaired_paths)
        if repaired_paths:
            with recovery_log_path.open("a", encoding="utf-8") as handle:
                for repaired_path in repaired_paths:
                    handle.write(
                        json.dumps(
                            {
                                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                "repair": "truncated_incomplete_jsonl_tail",
                                "path": repaired_path,
                            },
                            sort_keys=True,
                        )
                        + "\n"
                    )
        for value in call_rows:
            existing_calls[(clean_text(value.get("question_id")), clean_text(value.get("message_id")))] = value
        for value in message_rows:
            existing_messages[(clean_text(value.get("question_id")), clean_text(value.get("message_id")))] = value
        for value in failure_rows_for_resume:
            if value.get("phase") != "graph_persist" or not isinstance(value.get("validated_output"), Mapping):
                continue
            recoverable_extractions[
                (clean_text(value.get("question_id")), clean_text(value.get("message_id")))
            ] = value
    reports: list[dict[str, Any]] = []
    for row in rows:
        qid = clean_text(row.get("question_id"))
        sessions = list(row.get("haystack_sessions") or [])
        session_ids = [clean_text(value) for value in list(row.get("haystack_session_ids") or [])]
        dates = [clean_text(value) for value in list(row.get("haystack_dates") or [])]
        if not qid or not sessions or len(sessions) != len(session_ids) or len(sessions) != len(dates):
            raise ProductWriterError(f"{qid}: malformed history-only writer input")
        scope_id = f"tmcra_v3:{qid}"
        adapter = GraphSessionMemoryAdapter(
            auto_extract=False,
            storage_backend="sqlite",
            storage_path=str(db_path),
            scope_id=scope_id,
            audit_retention=max(1024, message_total + 128),
            retrieval_mode="hybrid_node_scored",
            node_model_path=clean_text(os.getenv("TMCRA_NODE_MODEL_PATH")),
            path_model_path=clean_text(os.getenv("TMCRA_PATH_MODEL_PATH")),
            node_model_device=clean_text(os.getenv("TMCRA_NODE_MODEL_DEVICE")) or "cpu",
        )
        if args.resume:
            repaired_warning_rows = repair_warning_journal_payloads(
                db_path, scope_id=scope_id
            )
            journal_warning_repairs += repaired_warning_rows
            if repaired_warning_rows:
                with journal_recovery_log_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "timestamp": datetime.now(timezone.utc).isoformat(
                                    timespec="seconds"
                                ),
                                "repair": "restore_full_warning_json_from_staged_extraction",
                                "scope_id": scope_id,
                                "row_count": repaired_warning_rows,
                            },
                            sort_keys=True,
                        )
                        + "\n"
                    )
        totals = {
            "messages": 0,
            "user_messages": 0,
            "assistant_messages": 0,
            "system_messages": 0,
            "tool_messages": 0,
            "writer_calls": 0,
            "flash_writer_calls": 0,
            "pro_writer_calls": 0,
            "api_calls": 0,
            "empty_semantic_calls": 0,
            "source_records": 0,
            "semantic_records": 0,
            "facet_records": 0,
            "interaction_records": 0,
            "resolution_edges": 0,
            "provenance_edges": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "resumed_messages": 0,
            "messages_with_warnings": 0,
            "validation_warning_count": 0,
            "quarantined_item_count": 0,
            "capacity_segmented_messages": 0,
            "capacity_segments": 0,
            "capacity_duplicate_count": 0,
        }
        for session_index, session in enumerate(sessions):
            previous_message: dict[str, Any] | None = None
            for message_index, raw_message in enumerate(list(session or [])):
                if not isinstance(raw_message, Mapping):
                    raise ProductWriterError(f"{qid}/s{session_index}/m{message_index}: message must be an object")
                role = clean_text(raw_message.get("role")).lower()
                content = str(raw_message.get("content") or "")
                if role not in {"user", "assistant", "system", "tool"} or not content:
                    raise ProductWriterError(f"{qid}/s{session_index}/m{message_index}: invalid role or empty content")
                message_id = f"s{session_index:03d}_m{message_index:03d}"
                timestamp = historical_timestamp(dates[session_index], message_index)
                current_message = {"role": role, "timestamp": timestamp, "content": content}
                existing_message = existing_messages.get((qid, message_id))
                journal = source_journal_row(db_path, scope_id=scope_id, message_id=message_id)
                if existing_message is not None:
                    if (
                        not args.resume
                        or journal is None
                        or journal["enrichment_status"] not in {"enriched", "enriched_with_warnings"}
                    ):
                        raise ProductWriterError(f"{message_id}: completed log and source journal disagree")
                    if clean_text(existing_message.get("role")) != role:
                        raise ProductWriterError(f"{message_id}: resumed role changed")
                    if clean_text(existing_message.get("content_sha256")) != sha256_text(content):
                        raise ProductWriterError(f"{message_id}: resumed content changed")
                    totals["messages"] += 1
                    totals["resumed_messages"] += 1
                    totals[f"{role}_messages"] = int(totals.get(f"{role}_messages", 0)) + 1
                    totals["source_records"] += int(existing_message.get("source", 0) or 0)
                    totals["semantic_records"] += int(existing_message.get("semantic", 0) or 0)
                    totals["facet_records"] += int(existing_message.get("facet", 0) or 0)
                    totals["interaction_records"] += int(existing_message.get("interaction", 0) or 0)
                    totals["resolution_edges"] += int(existing_message.get("resolution_count", 0) or 0)
                    totals["provenance_edges"] += int(existing_message.get("provenance_edge_count", 0) or 0)
                    if bool(existing_message.get("writer_called")):
                        existing_call = existing_calls.get((qid, message_id))
                        if existing_call is None:
                            raise ProductWriterError(f"{message_id}: resumed writer call log is missing")
                        totals["writer_calls"] += 1
                        existing_api_calls = max(1, int(existing_call.get("api_call_count", 1) or 1))
                        if clean_text(existing_call.get("model")) == model:
                            totals["flash_writer_calls"] += existing_api_calls
                        elif clean_text(existing_call.get("model")) == reviewer_model:
                            totals["pro_writer_calls"] += existing_api_calls
                        else:
                            raise ProductWriterError(f"{message_id}: resumed writer model is unsupported")
                        totals["api_calls"] += existing_api_calls
                        existing_segment_count = int(existing_call.get("capacity_segment_count", 0) or 0)
                        if existing_segment_count:
                            totals["capacity_segmented_messages"] += 1
                            totals["capacity_segments"] += existing_segment_count
                        totals["capacity_duplicate_count"] += int(
                            existing_call.get("capacity_duplicate_count", 0) or 0
                        )
                        totals["prompt_tokens"] += int(existing_call.get("prompt_tokens", 0) or 0)
                        totals["completion_tokens"] += int(existing_call.get("completion_tokens", 0) or 0)
                        existing_output = dict(existing_call.get("validated_output") or {})
                        warning_count = len(list(existing_output.get("validation_warnings") or []))
                        totals["validation_warning_count"] += warning_count
                        totals["quarantined_item_count"] += int(existing_output.get("quarantined_item_count", 0) or 0)
                        if warning_count:
                            totals["messages_with_warnings"] += 1
                        if not existing_output.get("assertions") and not existing_output.get("interactions") and not existing_output.get("resolutions"):
                            totals["empty_semantic_calls"] += 1
                    previous_message = current_message
                    continue
                if journal is None:
                    journal_source_message(
                        db_path,
                        scope_id=scope_id,
                        session_id=session_ids[session_index],
                        session_index=session_index,
                        message_id=message_id,
                        message_index=message_index,
                        timestamp=timestamp,
                        role=role,
                        content=content,
                    )
                    journal = source_journal_row(db_path, scope_id=scope_id, message_id=message_id)
                else:
                    if not args.resume or journal["content_sha256"] != sha256_text(content):
                        raise ProductWriterError(f"{message_id}: source journal cannot be resumed")
                    if journal["enrichment_status"] == "failed":
                        reopen_failed_source_journal(db_path, scope_id=scope_id, message_id=message_id)
                        journal = source_journal_row(db_path, scope_id=scope_id, message_id=message_id)
                    elif journal["enrichment_status"] not in {"pending", "enriched", "enriched_with_warnings"}:
                        raise ProductWriterError(
                            f"{message_id}: unsupported resume journal status={journal['enrichment_status']}"
                        )
                if journal is None:
                    raise ProductWriterError(f"{message_id}: source journal row was not created")
                journal_status = journal["enrichment_status"]
                claim_token = ""
                if journal_status == "pending":
                    claim_token = claim_source_journal(
                        db_path,
                        scope_id=scope_id,
                        message_id=message_id,
                        claim_owner=journal_claim_owner,
                        lease_seconds=journal_claim_lease_seconds,
                    ) or ""
                    if not claim_token:
                        current_claim = source_journal_row(
                            db_path, scope_id=scope_id, message_id=message_id
                        )
                        if current_claim is None:
                            raise ProductWriterError(f"{message_id}: source journal disappeared during claim")
                        raise ProductWriterError(
                            f"{message_id}: source journal is actively claimed by "
                            f"{current_claim['claim_owner']} until {current_claim['claim_expires_at']}; "
                            "refusing a duplicate Writer call"
                        )
                    journal = source_journal_row(db_path, scope_id=scope_id, message_id=message_id)
                    if (
                        journal is None
                        or journal["enrichment_status"] != "pending"
                        or journal["claim_owner"] != journal_claim_owner
                        or journal["claim_token"] != claim_token
                        or _source_journal_claim_expired(
                            journal["claim_expires_at"], now=datetime.now(timezone.utc)
                        )
                    ):
                        raise ProductWriterError(f"{message_id}: source journal changed after claim")
                extraction: dict[str, Any] | None = None
                call_metadata: dict[str, Any] = {}
                call_log_row: dict[str, Any] | None = None
                staged_extraction = bool(journal["extraction_json"])
                if staged_extraction:
                    staged_value = decode_journal_json(
                        journal["extraction_json"], message_id=message_id, field="extraction_json"
                    )
                    if staged_value is not None and not isinstance(staged_value, Mapping):
                        raise ProductWriterError(f"{message_id}: staged extraction is not an object or null")
                    extraction = dict(staged_value) if isinstance(staged_value, Mapping) else None
                    staged_metadata = decode_journal_json(
                        journal["call_metadata_json"], message_id=message_id, field="call_metadata_json"
                    )
                    if not isinstance(staged_metadata, Mapping):
                        raise ProductWriterError(f"{message_id}: staged call metadata is not an object")
                    call_metadata = dict(staged_metadata)

                adapter._reload_graph()
                preexisting_graph_commit = reconstruct_persisted_message(
                    adapter.graph,
                    message_id=message_id,
                    extraction=extraction,
                )
                if role in {"user", "assistant"}:
                    existing_slots = writer_slot_candidates(adapter.graph, content) if role == "user" else []
                    pending_interactions = pending_interaction_candidates(
                        adapter.graph,
                        current_role=role,
                        session_id=session_ids[session_index],
                        session_index=session_index,
                        current_message_index=message_index,
                    )
                    recovered_call = existing_calls.get((qid, message_id))
                    recovered_failure = recoverable_extractions.get((qid, message_id))
                    if not staged_extraction:
                        if recovered_call is not None:
                            if clean_text(recovered_call.get("content_sha256")) != sha256_text(content):
                                raise ProductWriterError(f"{message_id}: recoverable call log source changed")
                            recovered_output = recovered_call.get("validated_output")
                            if not isinstance(recovered_output, Mapping):
                                raise ProductWriterError(f"{message_id}: recoverable call log has no validated output")
                            extraction = dict(recovered_output)
                            call_metadata = call_metadata_from_log(recovered_call)
                        elif recovered_failure is not None:
                            if clean_text(recovered_failure.get("content_sha256")) != sha256_text(content):
                                raise ProductWriterError(f"{message_id}: recoverable extraction source changed")
                            extraction = dict(recovered_failure["validated_output"])
                            call_metadata = dict(recovered_failure["call_metadata"])
                        elif preexisting_graph_commit is not None:
                            raise ProductWriterError(
                                f"{message_id}: graph is committed but Writer output is absent; refusing a duplicate API call"
                            )
                        elif journal_status != "pending":
                            raise ProductWriterError(
                                f"{message_id}: completed journal has no staged Writer output"
                            )
                        else:
                            try:
                                mark_source_api_call_started(
                                    db_path,
                                    scope_id=scope_id,
                                    message_id=message_id,
                                    claim_owner=journal_claim_owner,
                                    claim_token=claim_token,
                                )
                                extraction, call_metadata = call_with_source_claim_heartbeat(
                                    lambda: writer.write(
                                        current_message=current_message,
                                        previous_message=previous_message,
                                        existing_memory_slots=existing_slots,
                                        pending_interactions=pending_interactions,
                                    ),
                                    path=db_path,
                                    scope_id=scope_id,
                                    message_id=message_id,
                                    claim_owner=journal_claim_owner,
                                    claim_token=claim_token,
                                    lease_seconds=journal_claim_lease_seconds,
                                )
                            except Exception as exc:
                                failure_row = {
                                    "question_id": qid,
                                    "scope_id": scope_id,
                                    "session_index": session_index,
                                    "message_index": message_index,
                                    "message_id": message_id,
                                    "message_role": role,
                                    "phase": "writer",
                                    "content_sha256": sha256_text(content),
                                    "error": f"{exc.__class__.__name__}: {exc}",
                                }
                                if isinstance(exc, ProductWriterResponseError):
                                    failed_requests = physical_requests_from_metadata(exc.request_metadata)
                                    failure_row["request"] = (
                                        dict(exc.request_metadata.get("terminal_request") or {})
                                        if exc.request_metadata.get("terminal_request")
                                        else (failed_requests[-1] if failed_requests else dict(exc.request_metadata))
                                    )
                                    failure_row["requests"] = failed_requests
                                    failure_row["physical_api_attempt_count"] = len(failed_requests)
                                    failure_row["response_content"] = exc.response_content
                                    failure_row["response_sha256"] = sha256_text(exc.response_content)
                                with failure_log_path.open("a", encoding="utf-8") as handle:
                                    handle.write(json.dumps(failure_row, ensure_ascii=False, sort_keys=True) + "\n")
                                finish_source_journal(
                                    db_path,
                                    scope_id=scope_id,
                                    message_id=message_id,
                                    claim_owner=journal_claim_owner,
                                    claim_token=claim_token,
                                    status="failed",
                                    error=f"{exc.__class__.__name__}: {exc}",
                                )
                                raise
                    if extraction is None:
                        raise ProductWriterError(f"{message_id}: writable message has no validated Writer output")
                    call_metadata.setdefault("existing_slot_candidate_count", len(existing_slots))
                    call_metadata.setdefault("pending_interaction_candidate_count", len(pending_interactions))
                    if not staged_extraction and journal_status == "pending":
                        stage_source_enrichment(
                            db_path,
                            scope_id=scope_id,
                            message_id=message_id,
                            claim_owner=journal_claim_owner,
                            claim_token=claim_token,
                            extraction=extraction,
                            call_metadata=call_metadata,
                        )
                        staged_extraction = True
                    totals["writer_calls"] += 1
                    physical_call_count = max(1, int(call_metadata.get("api_call_count", 1) or 1))
                    if call_metadata["model"] == model:
                        totals["flash_writer_calls"] += physical_call_count
                    elif call_metadata["model"] == reviewer_model:
                        totals["pro_writer_calls"] += physical_call_count
                    else:
                        raise ProductWriterError(f"{message_id}: routed writer selected an unsupported model")
                    totals["api_calls"] += physical_call_count
                    capacity_segment_count = int(call_metadata.get("capacity_segment_count", 0) or 0)
                    if capacity_segment_count:
                        totals["capacity_segmented_messages"] += 1
                        totals["capacity_segments"] += capacity_segment_count
                    totals["capacity_duplicate_count"] += int(
                        call_metadata.get("capacity_duplicate_count", 0) or 0
                    )
                    totals["prompt_tokens"] += int(call_metadata.get("prompt_tokens", 0) or 0)
                    totals["completion_tokens"] += int(call_metadata.get("completion_tokens", 0) or 0)
                    warning_count = len(list(extraction.get("validation_warnings") or []))
                    totals["validation_warning_count"] += warning_count
                    totals["quarantined_item_count"] += int(extraction.get("quarantined_item_count", 0) or 0)
                    if warning_count:
                        totals["messages_with_warnings"] += 1
                    if not extraction["assertions"] and not extraction["interactions"] and not extraction["resolutions"]:
                        totals["empty_semantic_calls"] += 1
                    call_log_row = {
                        "question_id": qid,
                        "scope_id": scope_id,
                        "session_index": session_index,
                        "message_index": message_index,
                        "message_id": message_id,
                        "message_role": extraction["message_role"],
                        "content_sha256": sha256_text(content),
                        "assertion_count": len(extraction["assertions"]),
                        "interaction_count": len(extraction["interactions"]),
                        "resolution_count": len(extraction["resolutions"]),
                        "validated_output": extraction,
                        **call_metadata,
                    }
                else:
                    if staged_extraction and extraction is not None:
                        raise ProductWriterError(f"{message_id}: system/tool journal contains semantic extraction")
                    if not staged_extraction and journal_status == "pending":
                        stage_source_enrichment(
                            db_path,
                            scope_id=scope_id,
                            message_id=message_id,
                            claim_owner=journal_claim_owner,
                            claim_token=claim_token,
                            extraction=None,
                            call_metadata={},
                        )
                        staged_extraction = True
                persisted: dict[str, Any] | None = None
                staged_persisted = bool(journal["persisted_json"])
                adapter._reload_graph()
                reconstructed = reconstruct_persisted_message(
                    adapter.graph,
                    message_id=message_id,
                    extraction=extraction,
                )
                if staged_persisted:
                    staged_value = decode_journal_json(
                        journal["persisted_json"], message_id=message_id, field="persisted_json"
                    )
                    if not isinstance(staged_value, Mapping):
                        raise ProductWriterError(f"{message_id}: staged graph commit is not an object")
                    persisted = dict(staged_value)
                    if reconstructed is None:
                        raise ProductWriterError(f"{message_id}: staged graph commit is absent from the graph")
                    for field in (
                        "source_record_id",
                        "source",
                        "semantic",
                        "facet",
                        "interaction",
                        "resolution_count",
                        "provenance_edge_count",
                    ):
                        if persisted.get(field) != reconstructed.get(field):
                            raise ProductWriterError(
                                f"{message_id}: staged graph field {field} disagrees with persisted graph"
                            )
                elif reconstructed is not None:
                    persisted = reconstructed
                    if journal_status == "pending":
                        stage_source_persisted(
                            db_path,
                            scope_id=scope_id,
                            message_id=message_id,
                            claim_owner=journal_claim_owner,
                            claim_token=claim_token,
                            persisted=persisted,
                        )
                        staged_persisted = True
                else:
                    if journal_status != "pending":
                        raise ProductWriterError(f"{message_id}: completed journal has no graph commit")
                    try:
                        persisted = ingest_product_message(
                            adapter,
                            SessionMemoryRecordV2,
                            SessionMemoryEdgeV2,
                            scope_id=scope_id,
                            session_id=session_ids[session_index],
                            session_index=session_index,
                            message_id=message_id,
                            message_index=message_index,
                            date=dates[session_index],
                            timestamp=timestamp,
                            role=role,
                            content=content,
                            extraction=extraction,
                        )
                    except Exception as exc:
                        if call_log_row is not None:
                            failed_requests = physical_requests_from_metadata(call_metadata)
                            with failure_log_path.open("a", encoding="utf-8") as handle:
                                handle.write(
                                    json.dumps(
                                        {
                                            "question_id": qid,
                                            "scope_id": scope_id,
                                            "session_index": session_index,
                                            "message_index": message_index,
                                            "message_id": message_id,
                                            "message_role": role,
                                            "phase": "graph_persist",
                                            "content_sha256": sha256_text(content),
                                            "error": f"{exc.__class__.__name__}: {exc}",
                                            "physical_api_attempt_count": len(failed_requests),
                                            "requests": failed_requests,
                                            "call_metadata": call_metadata,
                                            "validated_output": extraction,
                                        },
                                        ensure_ascii=False,
                                        sort_keys=True,
                                    )
                                    + "\n"
                                )
                        finish_source_journal(
                            db_path,
                            scope_id=scope_id,
                            message_id=message_id,
                            claim_owner=journal_claim_owner,
                            claim_token=claim_token,
                            status="failed",
                            error=f"{exc.__class__.__name__}: {exc}",
                        )
                        raise
                    stage_source_persisted(
                        db_path,
                        scope_id=scope_id,
                        message_id=message_id,
                        claim_owner=journal_claim_owner,
                        claim_token=claim_token,
                        persisted=persisted,
                    )
                    staged_persisted = True
                if persisted is None:
                    raise ProductWriterError(f"{message_id}: graph commit was not produced")
                if call_log_row is not None:
                    call_log_row["persisted"] = persisted
                    existing_call = existing_calls.get((qid, message_id))
                    if existing_call is None:
                        with call_log_path.open("a", encoding="utf-8") as handle:
                            handle.write(json.dumps(call_log_row, ensure_ascii=False, sort_keys=True) + "\n")
                        existing_calls[(qid, message_id)] = call_log_row
                    elif (
                        clean_text(existing_call.get("content_sha256")) != sha256_text(content)
                        or dict(existing_call.get("validated_output") or {}) != extraction
                        or dict(existing_call.get("persisted") or {}).get("source_record_id")
                        != persisted.get("source_record_id")
                    ):
                        raise ProductWriterError(f"{message_id}: existing call log disagrees with staged commit")
                current_journal = source_journal_row(db_path, scope_id=scope_id, message_id=message_id)
                if current_journal is None:
                    raise ProductWriterError(f"{message_id}: source journal disappeared before completion")
                if current_journal["enrichment_status"] == "pending":
                    finish_source_journal(
                        db_path,
                        scope_id=scope_id,
                        message_id=message_id,
                        claim_owner=journal_claim_owner,
                        claim_token=claim_token,
                        status=(
                            "enriched_with_warnings"
                            if extraction and extraction.get("validation_warnings")
                            else "enriched"
                        ),
                        source_record_id=str(persisted["source_record_id"]),
                        error=(
                            json.dumps(extraction.get("validation_warnings"), ensure_ascii=False, sort_keys=True)
                            if extraction and extraction.get("validation_warnings")
                            else ""
                        ),
                    )
                elif (
                    current_journal["enrichment_status"] not in {"enriched", "enriched_with_warnings"}
                    or current_journal["source_record_id"] != str(persisted["source_record_id"])
                ):
                    raise ProductWriterError(f"{message_id}: completed journal disagrees with staged graph commit")
                totals["messages"] += 1
                totals[f"{role}_messages"] = int(totals.get(f"{role}_messages", 0)) + 1
                totals["source_records"] += int(persisted["source"])
                totals["semantic_records"] += int(persisted["semantic"])
                totals["facet_records"] += int(persisted["facet"])
                totals["interaction_records"] += int(persisted["interaction"])
                totals["resolution_edges"] += int(persisted["resolution_count"])
                totals["provenance_edges"] += int(persisted["provenance_edge_count"])
                message_log_row = {
                    "question_id": qid,
                    "scope_id": scope_id,
                    "session_index": session_index,
                    "message_index": message_index,
                    "message_id": message_id,
                    "role": role,
                    "timestamp": timestamp,
                    "content_sha256": sha256_text(content),
                    "writer_called": role in {"user", "assistant"},
                    "extracted_assertion_count": len(extraction["assertions"]) if extraction else 0,
                    "extracted_interaction_count": len(extraction["interactions"]) if extraction else 0,
                    "resolution_count": len(extraction["resolutions"]) if extraction else 0,
                    "validation_warnings": list(extraction.get("validation_warnings") or []) if extraction else [],
                    "quarantined_item_count": int(extraction.get("quarantined_item_count", 0) or 0) if extraction else 0,
                    **persisted,
                }
                with message_log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(message_log_row, sort_keys=True) + "\n")
                existing_messages[(qid, message_id)] = message_log_row
                previous_message = current_message
        if totals["messages"] != totals["source_records"]:
            raise ProductWriterError(f"{qid}: not every message has exactly one immutable source record")
        if totals["writer_calls"] != totals["user_messages"] + totals["assistant_messages"]:
            raise ProductWriterError(f"{qid}: writer call count differs from user+assistant message count")
        if totals["api_calls"] < totals["writer_calls"]:
            raise ProductWriterError(f"{qid}: physical API call count is lower than logical writer call count")
        if totals["flash_writer_calls"] + totals["pro_writer_calls"] != totals["api_calls"]:
            raise ProductWriterError(f"{qid}: routed physical model call counts differ from API call count")
        counts = database_counts(db_path, scope_id)
        if counts["audit_turn_log"] != totals["messages"]:
            raise ProductWriterError(f"{qid}: persisted audit count differs from message count")
        if counts["product_source_journal"] != totals["messages"]:
            raise ProductWriterError(f"{qid}: source journal count differs from message count")
        reports.append({"question_id": qid, "scope_id": scope_id, "totals": totals, "database_counts": counts})

    failure_rows = (
        [json.loads(line) for line in failure_log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if failure_log_path.exists()
        else []
    )
    failed_writer_api_attempts = sum(
        int(row.get("physical_api_attempt_count", 0) or 0)
        for row in failure_rows
        if row.get("phase") == "writer"
    )
    successful_api_calls = sum(int(sample["totals"]["api_calls"]) for sample in reports)
    successful_call_rows = list(existing_calls.values())

    def observed_prompt_values(model_name: str, field_name: str) -> list[str]:
        return sorted(
            {
                clean_text(row.get(field_name))
                for row in successful_call_rows
                if clean_text(row.get("model")) == model_name
                and clean_text(row.get(field_name))
            }
        )

    flash_prompt_versions = observed_prompt_values(model, "prompt_version")
    flash_prompt_hashes = observed_prompt_values(model, "prompt_sha256")
    pro_prompt_versions = observed_prompt_values(
        reviewer_model, "prompt_version"
    )
    pro_prompt_hashes = observed_prompt_values(reviewer_model, "prompt_sha256")
    jsonl_tail_repairs_total = (
        sum(1 for line in recovery_log_path.read_text(encoding="utf-8").splitlines() if line.strip())
        if recovery_log_path.exists()
        else 0
    )
    journal_warning_repairs_total = (
        sum(
            int(json.loads(line).get("row_count", 0) or 0)
            for line in journal_recovery_log_path.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        )
        if journal_recovery_log_path.exists()
        else 0
    )
    report = {
        "schema_version": "tmcra.v3.product-writer-run.6",
        "status": "complete",
        "writer_schema_version": WRITE_SCHEMA_VERSION,
        "flash_prompt_version": (
            flash_prompt_versions[0]
            if len(flash_prompt_versions) == 1
            else "mixed"
            if flash_prompt_versions
            else PROMPT_VERSION
        ),
        "flash_prompt_sha256": (
            flash_prompt_hashes[0]
            if len(flash_prompt_hashes) == 1
            else "mixed"
            if flash_prompt_hashes
            else sha256_text(SYSTEM_PROMPT)
        ),
        "pro_prompt_version": (
            pro_prompt_versions[0]
            if len(pro_prompt_versions) == 1
            else "mixed"
            if pro_prompt_versions
            else PRO_PROMPT_VERSION
        ),
        "pro_prompt_sha256": (
            pro_prompt_hashes[0]
            if len(pro_prompt_hashes) == 1
            else "mixed"
            if pro_prompt_hashes
            else sha256_text(PRO_SYSTEM_PROMPT)
        ),
        "observed_flash_prompt_versions": flash_prompt_versions,
        "observed_flash_prompt_hashes": flash_prompt_hashes,
        "observed_pro_prompt_versions": pro_prompt_versions,
        "observed_pro_prompt_hashes": pro_prompt_hashes,
        "flash_model": model,
        "pro_model": reviewer_model,
        "routing_policy": "user_or_pending_interaction_to_pro_else_flash",
        "one_model_call_per_message": False,
        "normal_path_one_model_call_per_writable_message": True,
        "capacity_overflow_extra_calls_enabled": True,
        "capacity_overflow_policy": "same_routed_model_recursive_source_segmentation_no_semantic_caps",
        "capacity_attempt_circuit_breaker": 64,
        "semantic_item_count_limits": None,
        "writer_input_catalog_format": "compact_e0_descriptor_and_token_string_array",
        "max_output_tokens": writer.max_tokens,
        "validation_tolerance_policy": "warn_on_safe_syntax_normalization_and_quarantine_invalid_items",
        "syntactic_case_normalization_enabled": True,
        "redundant_facet_quote_tolerance_enabled": True,
        "item_quarantine_enabled": True,
        "silent_repairs_enabled": False,
        "failed_attempts_logged": len(failure_rows),
        "failed_writer_api_attempts": failed_writer_api_attempts,
        "successful_api_calls": successful_api_calls,
        "physical_api_attempts_including_failures": successful_api_calls + failed_writer_api_attempts,
        "jsonl_tail_repairs": jsonl_tail_repairs_total,
        "journal_warning_repairs": journal_warning_repairs_total,
        "api_key_pool_size": len(api_keys),
        "resumed": bool(args.resume),
        "input": str(input_path),
        "database": str(db_path),
        "elapsed_seconds": round(time.time() - started, 3),
        "samples": reports,
        "fallbacks_enabled": False,
        "semantic_rule_repairs_enabled": False,
        "legacy_profile_aggregate_layer_enabled": False,
        "query_or_evaluation_fields_visible_to_writer": False,
        "source_write_ahead_log_enabled": True,
        "staged_source_transaction_enabled": True,
        "source_journal_cross_process_claiming": True,
        "source_journal_claim_lease_seconds": journal_claim_lease_seconds,
        "source_transaction_stages": [
            "source_journal",
            "writer_output",
            "graph_commit",
            "call_log",
            "journal_complete",
            "message_log",
        ],
    }
    (out_dir / "product_writer_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
