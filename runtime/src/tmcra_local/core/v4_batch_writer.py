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
import unicodedata
import urllib.error
import urllib.request
import uuid
import weakref
from collections import defaultdict
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from .entity_identity import (IDENTITY_FIELDS, normalize_identity_metadata, bind_assertion_identity,
    identity_compatible, correction_entry, purge_identity_history)

from . import v3_product_writer as _v3_writer

FORBIDDEN_WRITER_FIELDS = _v3_writer.FORBIDDEN_WRITER_FIELDS
INTERACTION_STATUSES = _v3_writer.INTERACTION_STATUSES
INTERACTION_TYPES = _v3_writer.INTERACTION_TYPES
MEMORY_TYPES = {*_v3_writer.MEMORY_TYPES, "belief", "opinion"}
_v3_writer.MEMORY_TYPES = set(MEMORY_TYPES)
_BASE_MEMORY_FAMILY = _v3_writer.memory_family


def _v4_memory_family(memory_type: str) -> str:
    if memory_type in {"belief", "opinion"}:
        return "fact"
    try:
        return _BASE_MEMORY_FAMILY(memory_type)
    except KeyError:
        return "fact"


_v3_writer.memory_family = _v4_memory_family
OPERATIONS = _v3_writer.OPERATIONS
POLARITIES = {*_v3_writer.POLARITIES, "neutral"}
_v3_writer.POLARITIES = set(POLARITIES)
RESOLUTION_STATES = _v3_writer.RESOLUTION_STATES
ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{1,127}$")
_v3_writer.ROLE_RE = ROLE_RE
TEMPORAL_STATUSES = _v3_writer.TEMPORAL_STATUSES
ProductWriterError = _v3_writer.ProductWriterError
_build_v3_graph_records = _v3_writer.build_graph_records
clean_text = _v3_writer.clean_text
exact_evidence_spans = _v3_writer.exact_evidence_spans
exact_source_tokens = _v3_writer.exact_source_tokens
sha256_text = _v3_writer.sha256_text
source_token_matches = _v3_writer.source_token_matches
validate_writer_output = _v3_writer.validate_writer_output


class GroundingIntegrityError(ProductWriterError):
    """A model response broke immutable source/evidence alignment."""


BATCH_SCHEMA_VERSION = "tmcra.memory-write-batch.v4"
PROMPT_VERSION = "tmcra-product-writer-batch-2026-10-16.identity-hints.1"
RECONCILIATION_SCHEMA_VERSION = "tmcra.memory-reconcile.v4"
CANDIDATE_SELECTOR_VERSION = "tmcra.v4.lexical-slot-candidates.3"
DEFAULT_TARGET_TOKENS = 3000
DEFAULT_MIN_SOFT_TOKENS = 2000
DEFAULT_MAX_SOFT_TOKENS = 4000
DEFAULT_HARD_TOKEN_LIMIT = 32768
DECISIONS = {"insert", "merge_support", "replace_current", "keep_parallel", "challenge", "quarantine"}
SLOT_DECISIONS = {"bind_existing", "keep_proposed", "quarantine"}
GRAPH_AUTO_SUPERSESSION_REASONS = {
    "same_state_revision",
    "slot_disallows_parallel",
    "v4_reconciliation_replace_current",
}
GRAPH_INJECTED_BENCHMARK_METADATA_KEYS = {
    "origin_answer_id",
    "origin_answer_ids",
    "origin_question_id",
    "origin_question_ids",
    "benchmark_id",
    "gold_label",
}
SAFE_VALIDATION_WARNING_CODES = {
    "identifier_case_normalized",
    "identifier_separator_normalized",
    "duplicate_facet_dropped",
    "duplicate_assertion_merged",
    "duplicate_interaction_merged",
    "duplicate_resolution_merged",
    "optional_facet_dropped",
    "optional_resolution_dropped",
    "invalid_assertion_quarantined",
    "invalid_interaction_quarantined",
    "invalid_resolution_quarantined",
}


BATCH_SYSTEM_PROMPT = """You are the semantic extraction stage of a production personal-memory system.
Return exactly one JSON object and no prose. The request contains consecutive messages from one session.
For each message, source_spans are the only source text; their order reconstructs the exact message. Never
invent a message, span ID, interaction ID, quote, timestamp, or fact. Do not request or assume an existing
memory-slot inventory. Never emit benchmark questions, answers, labels, answer-session IDs, judge output,
passwords, authentication secrets, private keys, or account credentials.

Extract three independent layers for every supplied user or assistant message:
1. assertions: explicit user self-reports about facts, events, states, beliefs, opinions, preferences, goals,
   constraints, plans, identity, relationships, possessions, or routines. A question and its presupposition are not assertions.
   Assistant statements never become user assertions. Every assertion is atomic and cites one supplied eN span.
   claim_text is a concise, self-contained proposition entailed by that exact span. It must identify the actual
   subject and value needed to distinguish this fact from other facts in the same span. It is not a quote and must
   not add information. Split a span into multiple assertions only when their claim_text values are genuinely
   different facts; never repeat the same claim under multiple keys.
   The outer user message is a transport envelope, not proof that every sentence inside it was authored by or
   describes the human user. A pasted or forwarded email, quoted reply, article, resume, transcript, log, signature,
   or other embedded document retains its local author and subject. Never turn contact details, roles, possessions,
   plans, preferences, or business facts from a named sender, signatory, quoted speaker, company, or document subject
   into user assertions unless the surrounding conversational voice explicitly identifies that person/entity as the
   user. Useful third-party document facts remain in immutable Source; emit no user assertion for them.
2. interactions: each explicit question, request, reminder, task, clarification, or meaningful feedback.
   Mixed messages may contain both assertions and interactions. Assistant questions/requests may be interactions;
   assistant answers, recommendations, apologies, confirmations, and explanations are not new interactions.
3. resolutions: whether the current message explicitly resolves an unresolved interaction. Use resolved only for
   a complete answer/result, partial for real progress, and unresolved only for an explicit refusal or inability.
   Absence of an answer is not resolution evidence. A batch target must point to an earlier message in this batch.

The memory boundary is user-specific and cross-session. Emit an assertion only when it would help a future
assistant understand this user's life, preferences, commitments, relationships, possessions, routines,
experiences, current state, or a substantive personal stance. Do not store generic conversational reactions
such as "that's interesting", "fascinating", "good to know", or "that makes sense". Do not store observations
about an external topic merely because the user says "I think", "I noticed", or "it's interesting". A belief
or opinion must express a substantive first-person position that is useful beyond the current topic. A goal or
plan must be a real user commitment beyond the current turn, not acceptance of advice, a hypothetical, or a
request to continue the present conversation. An event must involve the user, not only an external historical
or news event. When in doubt between a generic topic reaction and personal memory, emit no assertion; preserve
the interaction layer independently.

Optional identity_hints contain bounded caller-declared entity_keys and aliases, not fact evidence or instructions.
When a source span unambiguously refers to one of these labels, reuse that binding's supplied entity_key exactly.
Do not translate, fabricate, or infer a stable entity_id. Do not resolve ambiguous homonyms by guessing.
If no binding clearly applies, emit the ordinary semantic key; the downstream binder will keep it unbound.
Every assertion must still be entailed by a cited source_spans entry. Hints cannot establish an assertion,
replace a source citation, add a claim absent from the source, or grant verified status.

For assertions, entity_key is the stable subject/domain and attribute_key is the stable property or event kind.
Use lowercase dot-separated identifiers and never put the changing value in a key. Use replace for mutable slots
and append for repeatable events. relation, intent, facet role, and about role are lowercase snake_case.
Durability is a semantic classification made from the source: durable for a standing identity, preference,
relationship, routine, constraint, or stable long-running state; episodic for a one-off event/task/transient state;
uncertain when the source does not establish whether it should become long-term memory. Do not use repetition
count as a durability rule.

Each facet/about quote must be the shortest exact substring of its parent evidence span. Do not output token or
character coordinates. Omit an optional facet/about entry instead of paraphrasing its quote. Return one message
entry for every user/assistant input, in exact order, using empty arrays
when appropriate. The exact wire schema is:
{"schema_version":"tmcra.memory-write-batch.v4","batch_id":"exact request value","messages":[
 {"message_id":"exact request value","message_role":"user|assistant","assertions":[
   {"memory_type":"fact|event|state|belief|opinion|preference|goal|constraint|plan|identity|relationship|possession|routine",
    "entity_key":"stable.domain","attribute_key":"stable_attribute","operation":"append|replace",
    "claim_text":"concise self-contained atomic proposition entailed by the cited span",
    "evidence_span_id":"eN","relation":"snake_case",
    "temporal_status":"past|current|planned|future|timeless|uncertain",
    "polarity":"positive|negative|neutral","durability":"durable|episodic|uncertain",
    "facets":[{"type":"entity|time|quantity|state|location|role","role":"snake_case","quote":"exact substring"}]}],
  "interactions":[{"interaction_type":"question|request|reminder|task|clarification|feedback",
    "status":"open|informational","evidence_span_id":"eN","intent":"snake_case",
    "about":[{"type":"entity|time|quantity|state|location|role","role":"snake_case","quote":"exact substring"}]}],
  "resolutions":[{"target":{"kind":"existing","interaction_id":"supplied id"},
    "resolution":"resolved|partial|unresolved","evidence_span_id":"eN"}]}]}
For a batch-local resolution target, replace target with
{"kind":"batch","message_id":"earlier exact message id","interaction_index":0}.
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_json(value: Any) -> str:
    return sha256_text(_json(value))


def _graph_slot_key(canonical_key: Any) -> str:
    value = clean_text(canonical_key)
    if not value:
        raise ProductWriterError("canonical slot key must not be empty")
    return value if value.startswith("memory.") else f"memory.{value}"


_SLOT_STOP_TOKENS = {
    "user",
    "memory",
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
    "current",
    "timeless",
    "replace",
    "append",
}

# These words identify broad domains or generic attribute shapes. Sharing only
# these words is not enough to spend a Pro call on slot binding.
_BROAD_SLOT_IDENTITY_TOKENS = {
    "home",
    "house",
    "utilities",
    "utility",
    "setup",
    "set",
    "up",
    "service",
    "services",
    "status",
    "information",
    "info",
    "details",
    "has",
    "have",
    "needs",
    "need",
    "uses",
    "use",
    "to",
}


def _slot_tokens(*values: Any) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        tokens.update(
            token
            for token in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", str(value or "").casefold())
            if token not in _SLOT_STOP_TOKENS
            and (len(token) > 1 or bool(re.fullmatch(r"[\u4e00-\u9fff]", token)))
        )
    return tokens


def _strict_json_object(value: str, path: str) -> dict[str, Any]:
    if not value or not value.strip():
        raise ProductWriterError(f"{path} must be a non-empty JSON object")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ProductWriterError(f"{path} is not strict JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ProductWriterError(f"{path} root must be an object")
    return parsed


def _exact_keys(value: Mapping[str, Any], expected: set[str], path: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ProductWriterError(
            f"{path} keys differ from schema; missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )


def _string(value: Any, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ProductWriterError(f"{path} must be a string")
    if not allow_empty and not value:
        raise ProductWriterError(f"{path} must not be empty")
    if value != value.strip():
        raise ProductWriterError(f"{path} must not have surrounding whitespace")
    return value


def _integer(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProductWriterError(f"{path} must be an integer")
    return value


def _enum(value: Any, allowed: set[str], path: str) -> str:
    result = _string(value, path)
    if result not in allowed:
        raise ProductWriterError(f"{path} is unsupported: {result!r}")
    return result


def _audited_enum(
    value: Any,
    allowed: set[str],
    path: str,
    warnings: list[dict[str, Any]],
) -> str:
    if not isinstance(value, str):
        raise ProductWriterError(f"{path} must be a string")
    normalized = re.sub(r"_+", "_", value.strip().lower().replace("-", "_").replace(" ", "_"))
    if normalized not in allowed:
        raise ProductWriterError(f"{path} is unsupported: {value!r}")
    if normalized != value:
        warnings.append(
            {
                "path": path,
                "code": "enum_value_normalized",
                "detail": f"normalized enum value: {value!r} -> {normalized!r}",
                "dropped_count": 0,
            }
        )
    return normalized


def _audited_item_keys(
    value: Mapping[str, Any],
    expected: set[str],
    path: str,
    warnings: list[dict[str, Any]],
    *,
    optional: set[str] = frozenset(),
) -> None:
    actual = set(value)
    missing = expected - actual - optional
    if missing:
        raise ProductWriterError(f"{path} is missing required keys: {sorted(missing)}")
    extra = actual - expected
    if extra:
        warnings.append(
            {
                "path": path,
                "code": "extra_item_fields_ignored",
                "detail": f"ignored unsupported item fields: {sorted(extra)}",
                "dropped_count": len(extra),
            }
        )


def _audited_durability(
    value: Any,
    path: str,
    warnings: list[dict[str, Any]],
) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        warnings.append(
            {
                "path": path,
                "code": "durability_defaulted_uncertain",
                "detail": "missing durability defaulted to uncertain",
                "dropped_count": 0,
            }
        )
        return "uncertain"
    try:
        return _audited_enum(
            value,
            {"durable", "episodic", "uncertain"},
            path,
            warnings,
        )
    except ProductWriterError:
        normalized = (
            value.strip().lower().replace("-", "_").replace(" ", "_")
            if isinstance(value, str)
            else ""
        )
        if normalized in TEMPORAL_STATUSES:
            warnings.append(
                {
                    "path": path,
                    "code": "temporal_durability_defaulted_uncertain",
                    "detail": (
                        f"durability contained temporal value {value!r}; "
                        "preserved assertion with uncertain durability"
                    ),
                    "dropped_count": 0,
                }
            )
            return "uncertain"
        raise


def _case_warning(
    warnings: list[dict[str, Any]] | None,
    *,
    path: str,
    original: str,
    normalized: str,
) -> None:
    if warnings is None:
        raise ProductWriterError(f"{path} requires unaudited case normalization")
    warnings.append(
        {
            "path": path,
            "code": "identifier_case_normalized",
            "detail": f"normalized identifier case: {original!r} -> {normalized!r}",
            "dropped_count": 0,
        }
    )


def _symbol_warning(
    warnings: list[dict[str, Any]] | None,
    *,
    path: str,
    original: str,
    normalized: str,
) -> None:
    if warnings is None:
        raise ProductWriterError(f"{path} requires unaudited symbol normalization")
    warnings.append(
        {
            "path": path,
            "code": "identifier_symbol_normalized",
            "detail": f"normalized identifier symbol: {original!r} -> {normalized!r}",
            "dropped_count": 0,
        }
    )


def _role_identifier(
    value: Any,
    path: str,
    warnings: list[dict[str, Any]] | None = None,
) -> str:
    result = _string(value, path)
    if not ROLE_RE.fullmatch(result):
        normalized = result.lower()
        if normalized != result and ROLE_RE.fullmatch(normalized):
            _case_warning(
                warnings,
                path=path,
                original=result,
                normalized=normalized,
            )
            return normalized
        # Ampersands commonly survive when a grounded brand acronym is copied
        # into a model-generated label (for example, T&T). This is the only
        # symbol rewrite accepted here; whitespace and arbitrary punctuation
        # remain hard failures.
        if "&" in result and not any(char.isspace() for char in result):
            symbol_normalized = re.sub(
                r"_+", "_", result.lower().replace("&", "_and_")
            ).strip("_")
            if ROLE_RE.fullmatch(symbol_normalized):
                _symbol_warning(
                    warnings,
                    path=path,
                    original=result,
                    normalized=symbol_normalized,
                )
                return symbol_normalized
        raise ProductWriterError(f"{path} is not snake_case: {result!r}")
    return result


def _canonical_identifier(value: Any, path: str) -> str:
    result = _string(value, path)
    if result != result.lower() or not all(char.isalnum() or char in "_.-" for char in result):
        raise ProductWriterError(f"{path} is not a canonical identifier: {result!r}")
    if len(result) < 2 or len(result) > 160 or result[0] not in "abcdefghijklmnopqrstuvwxyz0123456789":
        raise ProductWriterError(f"{path} is not a canonical identifier: {result!r}")
    return result


@dataclass(frozen=True)
class SourceMessage:
    scope_id: str
    session_id: str
    session_index: int
    message_index: int
    message_id: str
    role: str
    timestamp: str
    content: str
    actor_metadata: Mapping[str, str] = field(default_factory=dict)
    identity_metadata: Mapping[str, Any] = field(default_factory=dict)

    def request_dict(self) -> dict[str, Any]:
        result = {
            "message_id": self.message_id,
            "message_role": self.role,
            "timestamp": self.timestamp,
            "source_spans": [
                {"span_id": span["span_id"], "text": span["text"]}
                for span in lossless_source_spans(self.content)
            ],
        }
        declared = normalize_identity_metadata(self.identity_metadata)
        if declared:
            # Hints select a caller's spelling; they never establish a fact or a Source span.
            # Stable IDs/scope remain outside model output and are bound after validation.
            result["identity_hints"] = {
                "trust": "caller_declared_labels_not_fact_evidence",
                "bindings": [{"entity_keys": item["entity_keys"], "aliases": item["aliases"]}
                             for item in declared["entity_bindings"]],
            }
        return result


@dataclass(frozen=True)
class SourceBatch:
    scope_id: str
    session_id: str
    session_index: int
    batch_index: int
    messages: tuple[SourceMessage, ...]

    @property
    def batch_id(self) -> str:
        return f"{self.scope_id}:{self.session_id}:b{self.batch_index:04d}"


def _timestamp(raw_message: Mapping[str, Any], row: Mapping[str, Any], index: int) -> str:
    value = raw_message.get("timestamp", raw_message.get("time", ""))
    if value:
        return str(value)
    dates = list(row.get("haystack_dates") or row.get("dates") or [])
    session_index = int(row.get("session_index", 0) or 0)
    if session_index < len(dates) and dates[session_index]:
        try:
            return _v3_writer.historical_timestamp(dates[session_index], index)
        except ProductWriterError:
            return str(dates[session_index])
    return ""


def _row_sessions(row: Mapping[str, Any], row_index: int) -> list[tuple[str, list[Mapping[str, Any]], int]]:
    forbidden = sorted(FORBIDDEN_WRITER_FIELDS & set(row))
    if forbidden:
        raise ProductWriterError(f"input row contains forbidden benchmark fields: {forbidden}")
    qid = clean_text(row.get("question_id")) or f"row{row_index:04d}"
    scope_id = clean_text(row.get("scope_id")) or f"tmcra_v4:{qid}"
    if len(scope_id) > 512 or any(character in scope_id for character in "\r\n\0"):
        raise ProductWriterError(f"{qid}: scope_id is invalid")
    if "haystack_sessions" in row:
        sessions = list(row.get("haystack_sessions") or [])
        ids = [clean_text(value) for value in list(row.get("haystack_session_ids") or [])]
        if not ids:
            ids = [f"session-{index:03d}" for index in range(len(sessions))]
        if len(ids) != len(sessions):
            raise ProductWriterError(f"{qid}: session ID count differs from session count")
        return [(scope_id, list(session or []), index) for index, session in enumerate(sessions)]
    if "sessions" in row:
        sessions = list(row.get("sessions") or [])
        return [
            (scope_id, list(session or []), index)
            for index, session in enumerate(sessions)
        ]
    messages = list(row.get("messages") or [])
    session_id = clean_text(row.get("session_id")) or f"session-{row_index:03d}"
    return [(scope_id, messages, 0)]


def normalize_source_inventory(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[SourceMessage], list[dict[str, Any]]]:
    output: list[SourceMessage] = []
    exclusions: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row_index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ProductWriterError(f"input row {row_index} must be an object")
        for scope_id, session, session_index in _row_sessions(row, row_index):
            qid = scope_id.split(":", 1)[-1]
            session_ids = [clean_text(value) for value in list(row.get("haystack_session_ids") or [])]
            session_id = (
                session_ids[session_index]
                if session_index < len(session_ids) and session_ids[session_index]
                else clean_text(row.get("session_id")) or f"session-{session_index:03d}"
            )
            for message_index, raw_message in enumerate(session):
                if not isinstance(raw_message, Mapping):
                    raise ProductWriterError(f"{qid}/s{session_index:03d}/m{message_index:03d}: message must be an object")
                role = clean_text(raw_message.get("role")).lower()
                content = str(raw_message.get("content") or "")
                explicit_index = raw_message.get("message_index", message_index)
                if isinstance(explicit_index, bool):
                    raise ProductWriterError(
                        f"{qid}/s{session_index:03d}/m{message_index:03d}: message_index is invalid"
                    )
                try:
                    source_message_index = int(explicit_index)
                except (TypeError, ValueError) as exc:
                    raise ProductWriterError(
                        f"{qid}/s{session_index:03d}/m{message_index:03d}: message_index is invalid"
                    ) from exc
                if source_message_index < 0:
                    raise ProductWriterError(
                        f"{qid}/s{session_index:03d}/m{message_index:03d}: message_index must be non-negative"
                    )
                message_id = clean_text(raw_message.get("message_id")) or (
                    f"s{session_index:03d}_m{source_message_index:03d}"
                )
                if len(message_id) > 512 or any(
                    character in message_id for character in "\r\n\0"
                ):
                    raise ProductWriterError(
                        f"{qid}/s{session_index:03d}/m{message_index:03d}: message_id is invalid"
                    )
                if role not in {"user", "assistant", "system", "tool"}:
                    raise ProductWriterError(f"{qid}/s{session_index:03d}/m{message_index:03d}: invalid role")
                if not content.strip():
                    exclusions.append(
                        {
                            "scope_id": scope_id,
                            "session_id": session_id,
                            "session_index": session_index,
                            "message_index": message_index,
                            "message_id": message_id,
                            "message_role": role,
                            "reason": "empty_content",
                            "content_sha256": sha256_text(content),
                        }
                    )
                    continue
                key = (scope_id, message_id)
                if key in seen:
                    raise ProductWriterError(f"duplicate source message ID: {message_id}")
                seen.add(key)
                actor_metadata = raw_message.get("actor_metadata") or {}
                if not isinstance(actor_metadata, Mapping):
                    raise ProductWriterError(
                        f"{qid}/s{session_index:03d}/m{message_index:03d}: actor_metadata must be an object"
                    )
                output.append(
                    SourceMessage(
                        scope_id=scope_id,
                        session_id=session_id,
                        session_index=session_index,
                        message_index=source_message_index,
                        message_id=message_id,
                        role=role,
                        timestamp=_timestamp(raw_message, {**row, "session_index": session_index}, message_index),
                        content=content,
                        identity_metadata=normalize_identity_metadata(raw_message.get("identity_metadata")),
                        actor_metadata={
                            str(key): clean_text(value)
                            for key, value in dict(actor_metadata).items()
                            if clean_text(key) and clean_text(value)
                        },
                    )
                )
    return output, exclusions


def normalize_source_rows(rows: Sequence[Mapping[str, Any]]) -> list[SourceMessage]:
    return normalize_source_inventory(rows)[0]


def build_batches(
    messages: Sequence[SourceMessage],
    *,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    min_soft_tokens: int = DEFAULT_MIN_SOFT_TOKENS,
    max_soft_tokens: int = DEFAULT_MAX_SOFT_TOKENS,
    hard_limit_tokens: int = DEFAULT_HARD_TOKEN_LIMIT,
) -> list[SourceBatch]:
    if not (0 < min_soft_tokens <= target_tokens <= max_soft_tokens) or hard_limit_tokens <= 0:
        raise ValueError("batch token limits must satisfy min <= target <= max and hard > 0")
    batches: list[SourceBatch] = []
    current: list[SourceMessage] = []
    current_tokens = 0
    batch_index_by_session: dict[tuple[str, str], int] = {}

    def flush() -> None:
        nonlocal current, current_tokens
        if not current:
            return
        first = current[0]
        key = (first.scope_id, first.session_id)
        incremental = os.getenv("TMCRA_LOCAL_INCREMENTAL_BATCH_IDS", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        index = (
            int(first.message_index)
            if incremental
            else batch_index_by_session.get(key, 0)
        )
        batches.append(
            SourceBatch(
                first.scope_id,
                first.session_id,
                first.session_index,
                index,
                tuple(current),
            )
        )
        batch_index_by_session[key] = index + 1
        current = []
        current_tokens = 0

    previous_key: tuple[str, str] | None = None
    for message in messages:
        key = (message.scope_id, message.session_id)
        if previous_key != key:
            flush()
        previous_key = key
        token_count = len(exact_source_tokens(message.content))
        if token_count > hard_limit_tokens:
            raise ProductWriterError(
                f"{message.message_id}: source message has {token_count} tokens, over hard limit {hard_limit_tokens}"
            )
        if current and current_tokens + token_count > target_tokens:
            flush()
        current.append(message)
        current_tokens += token_count
    flush()
    return batches


def lossless_source_spans(content: str) -> list[dict[str, Any]]:
    """Build the only source text representation sent to Flash.

    Evidence spans retain V3's eN IDs. Gap spans preserve whitespace between
    evidence spans, so the sequence is lossless and non-overlapping without a
    second full-content or token-string payload.
    """
    evidence = exact_evidence_spans(content)
    if len(evidence) == 1 or (
        len(evidence) == 2
        and int(evidence[1]["char_start"]) == 0
        and int(evidence[1]["char_end"]) == len(content)
    ):
        return [{"span_id": "e0", "text": content, "char_start": 0, "char_end": len(content)}]
    output: list[dict[str, Any]] = []
    cursor = 0
    gap_index = 0
    for span in evidence[1:]:
        start = int(span["char_start"])
        if start > cursor:
            output.append({"span_id": f"gap{gap_index}", "text": content[cursor:start], "char_start": cursor, "char_end": start})
            gap_index += 1
        output.append(dict(span))
        cursor = int(span["char_end"])
    if cursor < len(content):
        output.append({"span_id": f"gap{gap_index}", "text": content[cursor:], "char_start": cursor, "char_end": len(content)})
    if "".join(str(span["text"]) for span in output) != content:
        raise ProductWriterError("lossless source span sequence does not reconstruct source content")
    previous_end = 0
    for span in output:
        if int(span["char_start"]) != previous_end or int(span["char_end"]) < int(span["char_start"]):
            raise ProductWriterError("lossless source span sequence overlaps or is unordered")
        previous_end = int(span["char_end"])
    return output


def build_batch_request(batch: SourceBatch, unresolved_interactions: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    return {
        "schema_version": BATCH_SCHEMA_VERSION,
        "batch_id": batch.batch_id,
        "messages": [message.request_dict() for message in batch.messages
                     if _v3_writer.permits_source_extraction(message.role, message.actor_metadata)],
        "unresolved_interactions": [dict(item) for item in unresolved_interactions],
    }


def batch_response_json_schema(request: Mapping[str, Any]) -> dict[str, Any]:
    """Build a request-bound schema for local constrained decoding.

    The schema fixes the batch identity, message count, message order, roles,
    and evidence identifiers.  The normal validator remains authoritative for
    grounding, provenance, and semantic policy after decoding.
    """

    messages = request.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ProductWriterError("batch response schema requires request messages")
    response_messages = [
        message
        for message in messages
        if isinstance(message, Mapping)
        and clean_text(message.get("message_role")) in {"user", "assistant"}
    ]
    if not response_messages:
        raise ProductWriterError(
            "batch response schema requires one user or assistant message"
        )

    identifier = {"type": "string", "pattern": r"^[a-z][a-z0-9_]{1,127}$"}
    dotted_identifier = {
        "type": "string",
        "pattern": r"^[a-z][a-z0-9_.]{1,255}$",
    }
    facet = {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["entity", "time", "quantity", "state", "location", "role"],
            },
            "role": identifier,
            "quote": {"type": "string", "minLength": 1},
        },
        "required": ["type", "role", "quote"],
        "additionalProperties": False,
    }
    prior_message_ids: list[str] = []
    message_schemas: list[dict[str, Any]] = []
    unresolved_ids = [
        clean_text(item.get("interaction_id"))
        for item in request.get("unresolved_interactions") or []
        if isinstance(item, Mapping) and clean_text(item.get("interaction_id"))
    ]
    for index, message in enumerate(response_messages):
        if not isinstance(message, Mapping):
            raise ProductWriterError(
                f"batch response schema message[{index}] must be an object"
            )
        message_id = clean_text(message.get("message_id"))
        role = clean_text(message.get("message_role"))
        spans = message.get("source_spans")
        evidence_ids = [
            clean_text(span.get("span_id"))
            for span in spans or []
            if isinstance(span, Mapping)
            and clean_text(span.get("span_id")).startswith("e")
        ]
        if not message_id or role not in {"user", "assistant"} or not evidence_ids:
            raise ProductWriterError(
                f"batch response schema message[{index}] identity is invalid"
            )
        evidence = {"type": "string", "enum": evidence_ids}
        assertion = {
            "type": "object",
            "properties": {
                "memory_type": identifier,
                "entity_key": dotted_identifier,
                "attribute_key": dotted_identifier,
                "operation": {"type": "string", "enum": sorted(OPERATIONS)},
                "claim_text": {"type": "string", "minLength": 1, "maxLength": 1000},
                "evidence_span_id": evidence,
                "relation": identifier,
                "temporal_status": {
                    "type": "string",
                    "enum": sorted(TEMPORAL_STATUSES),
                },
                "polarity": {"type": "string", "enum": sorted(POLARITIES)},
                "durability": {
                    "type": "string",
                    "enum": ["durable", "episodic", "uncertain"],
                },
                "facets": {"type": "array", "items": facet, "maxItems": 32},
            },
            "required": [
                "memory_type",
                "entity_key",
                "attribute_key",
                "operation",
                "claim_text",
                "evidence_span_id",
                "relation",
                "temporal_status",
                "polarity",
                "durability",
                "facets",
            ],
            "additionalProperties": False,
        }
        interaction = {
            "type": "object",
            "properties": {
                "interaction_type": {
                    "type": "string",
                    "enum": sorted(INTERACTION_TYPES),
                },
                "status": {"type": "string", "enum": sorted(INTERACTION_STATUSES)},
                "evidence_span_id": evidence,
                "intent": identifier,
                "about": {"type": "array", "items": facet, "maxItems": 32},
            },
            "required": [
                "interaction_type",
                "status",
                "evidence_span_id",
                "intent",
                "about",
            ],
            "additionalProperties": False,
        }
        target_variants: list[dict[str, Any]] = []
        if unresolved_ids:
            target_variants.append(
                {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["existing"]},
                        "interaction_id": {"type": "string", "enum": unresolved_ids},
                    },
                    "required": ["kind", "interaction_id"],
                    "additionalProperties": False,
                }
            )
        if prior_message_ids:
            target_variants.append(
                {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["batch"]},
                        "message_id": {"type": "string", "enum": prior_message_ids},
                        "interaction_index": {"type": "integer", "minimum": 0},
                    },
                    "required": ["kind", "message_id", "interaction_index"],
                    "additionalProperties": False,
                }
            )
        target_schema: dict[str, Any]
        if target_variants:
            target_schema = {"oneOf": target_variants}
        else:
            # The array may be empty.  This impossible target prevents the
            # decoder from inventing a resolution with no legal predecessor.
            target_schema = {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["__no_legal_target__"]}
                },
                "required": ["kind"],
                "additionalProperties": False,
            }
        resolution = {
            "type": "object",
            "properties": {
                "target": target_schema,
                "resolution": {"type": "string", "enum": sorted(RESOLUTION_STATES)},
                "evidence_span_id": evidence,
            },
            "required": ["target", "resolution", "evidence_span_id"],
            "additionalProperties": False,
        }
        message_schemas.append(
            {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "enum": [message_id]},
                    "message_role": {"type": "string", "enum": [role]},
                    "assertions": {
                        "type": "array",
                        "items": assertion,
                        "maxItems": 64,
                    },
                    "interactions": {
                        "type": "array",
                        "items": interaction,
                        "maxItems": 64,
                    },
                    "resolutions": {
                        "type": "array",
                        "items": resolution,
                        "maxItems": 64,
                    },
                },
                "required": [
                    "message_id",
                    "message_role",
                    "assertions",
                    "interactions",
                    "resolutions",
                ],
                "additionalProperties": False,
            }
        )
        prior_message_ids.append(message_id)
    return {
        "type": "object",
        "properties": {
            "schema_version": {"type": "string", "enum": [BATCH_SCHEMA_VERSION]},
            "batch_id": {
                "type": "string",
                "enum": [clean_text(request.get("batch_id"))],
            },
            "messages": {
                "type": "array",
                "prefixItems": message_schemas,
                "minItems": len(message_schemas),
                "maxItems": len(message_schemas),
            },
        },
        "required": ["schema_version", "batch_id", "messages"],
        "additionalProperties": False,
    }


def _evidence_span(content: str, span_id: str, path: str) -> dict[str, Any]:
    for span in exact_evidence_spans(content):
        if span["span_id"] == span_id:
            return span
    raise GroundingIntegrityError(
        f"{path} is not in the current-message evidence catalog: {span_id!r}"
    )


def _exact_provenance_offsets(
    content: str,
    span_id: str,
    evidence_quote: str,
    path: str,
) -> tuple[int, int]:
    parent_span = _evidence_span(content, span_id, f"{path}.evidence_span_id")
    parent_start = int(parent_span["char_start"])
    parent_text = content[parent_start : int(parent_span["char_end"])]
    relative_start = parent_text.find(evidence_quote)
    if relative_start < 0:
        raise GroundingIntegrityError(
            f"{path}.evidence_quote is not an exact Source slice"
        )
    if parent_text.find(evidence_quote, relative_start + 1) >= 0:
        raise GroundingIntegrityError(
            f"{path}.evidence_quote is ambiguous within its Source span"
        )
    start = parent_start + relative_start
    return start, start + len(evidence_quote)


def _validate_facet(
    value: Any,
    content: str,
    parent_span: Mapping[str, Any],
    path: str,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProductWriterError(f"{path} must be an object")
    _exact_keys(value, {"type", "role", "quote"}, path)
    facet_type = _enum(value.get("type"), {"entity", "time", "quantity", "state", "location", "role"}, f"{path}.type")
    role = _role_identifier(value.get("role"), f"{path}.role", warnings)
    quote = _string(value.get("quote"), f"{path}.quote")
    parent_start = int(parent_span["char_start"])
    parent_end = int(parent_span["char_end"])
    parent_text = content[parent_start:parent_end]
    relative_start = parent_text.find(quote)
    if relative_start < 0:
        raise ProductWriterError(f"{path}.quote is not an exact substring of its parent evidence span")
    absolute_start = parent_start + relative_start
    absolute_end = absolute_start + len(quote)
    tokens = source_token_matches(content)
    start = next((index for index, token in enumerate(tokens) if token.start() <= absolute_start < token.end()), None)
    end = next((index for index, token in enumerate(tokens) if token.start() < absolute_end <= token.end()), None)
    if start is None or end is None or end < start:
        raise ProductWriterError(f"{path}.quote must overlap source tokens")
    return {"type": facet_type, "role": role, "token_start": start, "token_end": end}


def _validate_evidence(value: Any, content: str, path: str, allowed_ids: set[str] | None = None) -> str:
    try:
        span_id = _string(value, path)
    except ProductWriterError as exc:
        raise GroundingIntegrityError(str(exc)) from exc
    if allowed_ids is not None and span_id not in allowed_ids:
        raise GroundingIntegrityError(
            f"{path} was not supplied in the lossless source-span sequence: {span_id!r}"
        )
    _evidence_span(content, span_id, path)
    return span_id


def _deterministic_interaction_id(scope_id: str, message_id: str, interaction_index: int) -> str:
    return f"interaction:{scope_id}:{message_id}:{interaction_index}"


def _assertion_identity(value: Mapping[str, Any]) -> tuple[Any, ...]:
    if "canonical_key" in value:
        canonical_key = str(value["canonical_key"])
    else:
        entity = _v3_writer.normalize_canonical_key(str(value["entity_key"])).removeprefix("user.")
        attribute = _v3_writer.normalize_canonical_key(str(value["attribute_key"]))
        family = _v3_writer.memory_family(str(value["memory_type"]))
        canonical_key = f"user.{entity}.{family}.{attribute}"
    operation = str(value["operation"])
    facet_identity: tuple[Any, ...] = ()
    if operation == "append":
        facet_identity = tuple(
            sorted(
                (
                    str(facet["type"]),
                    str(facet["role"]),
                    int(facet["token_start"]),
                    int(facet["token_end"]),
                )
                for facet in value.get("facets") or []
            )
        )
    return (
        canonical_key,
        str(value["memory_type"]),
        operation,
        str(value["evidence_span_id"]),
        str(value["relation"]),
        str(value["temporal_status"]),
        str(value["polarity"]),
        facet_identity,
    )


def _interaction_identity(value: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        str(value["interaction_type"]),
        str(value["status"]),
        str(value["evidence_span_id"]),
        str(value["intent"]),
    )


def _facet_quote_lookup(
    normalized_facets: Sequence[Mapping[str, Any]],
    raw_facets: Sequence[Mapping[str, Any]],
    raw_quotes: Sequence[str],
) -> list[str]:
    by_key: dict[tuple[Any, ...], list[str]] = {}
    for facet, quote in zip(raw_facets, raw_quotes):
        key = (
            str(facet["type"]),
            str(facet["role"]),
            int(facet["token_start"]),
            int(facet["token_end"]),
        )
        by_key.setdefault(key, []).append(str(quote))
    output = []
    for facet in normalized_facets:
        key = (
            str(facet["type"]),
            str(facet["role"]),
            int(facet["token_start"]),
            int(facet["token_end"]),
        )
        candidates = by_key.get(key)
        if not candidates:
            raise ProductWriterError("validated facet cannot be mapped back to its exact quote")
        output.append(min(candidates, key=lambda item: (len(item), item)))
    return output


def validate_batch_response(
    payload: Mapping[str, Any] | str,
    batch: SourceBatch,
    unresolved_interactions: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ProductWriterError(f"batch response is not strict JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ProductWriterError("batch response root must be an object")
    _exact_keys(payload, {"schema_version", "batch_id", "messages"}, "root")
    if payload.get("schema_version") != BATCH_SCHEMA_VERSION:
        raise ProductWriterError(f"unexpected batch schema: {payload.get('schema_version')!r}")
    if payload.get("batch_id") != batch.batch_id:
        raise ProductWriterError("batch response batch_id differs from controller value")
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list):
        raise ProductWriterError("root.messages must be an array")
    expected_messages = [message for message in batch.messages
                         if _v3_writer.permits_source_extraction(message.role, message.actor_metadata)]
    if len(raw_messages) != len(expected_messages):
        raise ProductWriterError("batch response must contain exactly one entry for every user or assistant message")
    existing_ids = {
        _string(item.get("interaction_id"), "unresolved_interactions[].interaction_id")
        for item in unresolved_interactions
    }
    normalized_messages: list[dict[str, Any]] = []
    # Raw batch-local interaction indexes may collapse during duplicate merge.
    # This map preserves the model-facing raw ID while resolving it to the
    # interaction ID that will actually be persisted.
    prior_interactions: dict[str, str] = {}
    prior_message_ids: set[str] = set()
    for response_index, (raw_message, source) in enumerate(zip(raw_messages, expected_messages)):
        path = f"root.messages[{response_index}]"
        controller_warnings: list[dict[str, Any]] = []
        if not isinstance(raw_message, Mapping):
            raise ProductWriterError(f"{path} must be an object")
        _audited_item_keys(
            raw_message,
            {"message_id", "message_role", "assertions", "interactions", "resolutions"},
            path,
            controller_warnings,
            optional={"assertions", "interactions", "resolutions"},
        )
        if raw_message.get("message_id") != source.message_id:
            raise ProductWriterError(f"{path}.message_id differs from controller value")
        if raw_message.get("message_role") != source.role:
            raise ProductWriterError(f"{path}.message_role differs from controller value")
        assertions = raw_message.get("assertions")
        interactions = raw_message.get("interactions")
        resolutions = raw_message.get("resolutions")
        for name, values in (("assertions", assertions), ("interactions", interactions), ("resolutions", resolutions)):
            if not isinstance(values, list):
                controller_warnings.append(
                    {
                        "path": f"{path}.{name}",
                        "code": "invalid_item_collection_defaulted_empty",
                        "detail": "missing or non-array item collection defaulted to []",
                        "dropped_count": 1 if values is not None else 0,
                    }
                )
        assertions = assertions if isinstance(assertions, list) else []
        interactions = interactions if isinstance(interactions, list) else []
        resolutions = resolutions if isinstance(resolutions, list) else []
        raw_v3_assertions: list[dict[str, Any]] = []
        assertion_facet_quotes: list[list[str]] = []
        assertion_claim_texts: list[str] = []
        durability: list[str] = []
        if source.role == "assistant" and assertions:
            controller_warnings.append(
                {
                    "path": f"{path}.assertions",
                    "code": "assistant_assertions_dropped",
                    "detail": (
                        "assistant-authored assertions cannot become user memory; "
                        "immutable source was retained"
                    ),
                    "dropped_count": len(assertions),
                }
            )
            assertions = []
        allowed_evidence_ids = {
            str(span["span_id"])
            for span in lossless_source_spans(source.content)
            if str(span["span_id"]).startswith("e")
        }
        for assertion_index, raw_assertion in enumerate(assertions):
            assertion_path = f"{path}.assertions[{assertion_index}]"
            try:
                if not isinstance(raw_assertion, Mapping):
                    raise ProductWriterError(f"{assertion_path} must be an object")
                raw_assertion = dict(raw_assertion)
                _audited_item_keys(
                    raw_assertion,
                    {
                        "memory_type", "entity_key", "attribute_key", "operation",
                        "claim_text", "evidence_span_id", "relation", "temporal_status",
                        "polarity", "durability", "facets",
                    },
                    assertion_path,
                    controller_warnings,
                    optional={"durability", "facets"},
                )
                if not isinstance(raw_assertion.get("facets"), list):
                    controller_warnings.append(
                        {
                            "path": f"{assertion_path}.facets",
                            "code": "optional_facets_defaulted_empty",
                            "detail": "missing or non-array optional facets defaulted to []",
                            "dropped_count": 0,
                        }
                    )
                    raw_assertion["facets"] = []
                claim_text = _string(
                    raw_assertion.get("claim_text"), f"{assertion_path}.claim_text"
                )
                if len(claim_text) > 1000:
                    raise ProductWriterError(
                        f"{assertion_path}.claim_text exceeds 1000 characters"
                    )
                memory_type = _role_identifier(
                    raw_assertion.get("memory_type"),
                    f"{assertion_path}.memory_type",
                    controller_warnings,
                )
                if memory_type not in MEMORY_TYPES:
                    MEMORY_TYPES.add(memory_type)
                    _v3_writer.MEMORY_TYPES.add(memory_type)
                    controller_warnings.append(
                        {
                            "path": f"{assertion_path}.memory_type",
                            "code": "memory_type_extension_accepted",
                            "detail": f"accepted grounded snake_case extension: {memory_type}",
                            "dropped_count": 0,
                        }
                    )
                evidence_span_id = _validate_evidence(
                    raw_assertion.get("evidence_span_id"),
                    source.content,
                    f"{assertion_path}.evidence_span_id",
                    allowed_evidence_ids,
                )
                v3_assertion = {
                    "memory_type": memory_type,
                    "entity_key": _canonical_identifier(raw_assertion.get("entity_key"), f"{assertion_path}.entity_key"),
                    "attribute_key": _canonical_identifier(raw_assertion.get("attribute_key"), f"{assertion_path}.attribute_key"),
                    "operation": _audited_enum(raw_assertion.get("operation"), set(OPERATIONS), f"{assertion_path}.operation", controller_warnings),
                    "evidence_span_id": evidence_span_id,
                    "relation": _role_identifier(raw_assertion.get("relation"), f"{assertion_path}.relation", controller_warnings),
                    "temporal_status": _audited_enum(raw_assertion.get("temporal_status"), set(TEMPORAL_STATUSES), f"{assertion_path}.temporal_status", controller_warnings),
                    "polarity": _audited_enum(raw_assertion.get("polarity"), set(POLARITIES), f"{assertion_path}.polarity", controller_warnings),
                    "facets": [],
                }
                assertion_durability = _audited_durability(
                    raw_assertion.get("durability"),
                    f"{assertion_path}.durability",
                    controller_warnings,
                )
                parent_span = _evidence_span(
                    source.content,
                    evidence_span_id,
                    f"{assertion_path}.evidence_span_id",
                )
                kept_facets: list[dict[str, Any]] = []
                kept_facet_quotes: list[str] = []
                for facet_index, item in enumerate(raw_assertion["facets"]):
                    facet_path = f"{assertion_path}.facets[{facet_index}]"
                    try:
                        kept_facets.append(
                            _validate_facet(
                                item,
                                source.content,
                                parent_span,
                                facet_path,
                                controller_warnings,
                            )
                        )
                        kept_facet_quotes.append(
                            _string(item.get("quote"), f"{facet_path}.quote")
                        )
                    except ProductWriterError as exc:
                        controller_warnings.append(
                            {
                                "path": facet_path,
                                "code": "optional_facet_dropped",
                                "detail": str(exc),
                                "dropped_count": 1,
                            }
                        )
                v3_assertion["facets"] = kept_facets
                assertion_facet_quotes.append(kept_facet_quotes)
                assertion_claim_texts.append(claim_text)
                durability.append(assertion_durability)
                raw_v3_assertions.append(v3_assertion)
            except GroundingIntegrityError:
                raise
            except ProductWriterError as exc:
                controller_warnings.append(
                    {
                        "path": assertion_path,
                        "code": "invalid_assertion_quarantined",
                        "detail": str(exc),
                        "dropped_count": 1,
                    }
                )
        raw_v3_interactions: list[dict[str, Any]] = []
        interaction_about_quotes: list[list[str]] = []
        interaction_source_indexes: list[int] = []
        for interaction_index, raw_interaction in enumerate(interactions):
            interaction_path = f"{path}.interactions[{interaction_index}]"
            try:
                if not isinstance(raw_interaction, Mapping):
                    raise ProductWriterError(f"{interaction_path} must be an object")
                raw_interaction = dict(raw_interaction)
                _audited_item_keys(
                    raw_interaction,
                    {"interaction_type", "status", "evidence_span_id", "intent", "about"},
                    interaction_path,
                    controller_warnings,
                    optional={"about"},
                )
                if not isinstance(raw_interaction.get("about"), list):
                    controller_warnings.append(
                        {
                            "path": f"{interaction_path}.about",
                            "code": "optional_about_defaulted_empty",
                            "detail": "missing or non-array optional about defaulted to []",
                            "dropped_count": 0,
                        }
                    )
                    raw_interaction["about"] = []
                interaction_evidence_id = _validate_evidence(
                    raw_interaction.get("evidence_span_id"),
                    source.content,
                    f"{interaction_path}.evidence_span_id",
                    allowed_evidence_ids,
                )
                interaction_parent_span = _evidence_span(
                    source.content,
                    interaction_evidence_id,
                    f"{interaction_path}.evidence_span_id",
                )
                kept_about: list[dict[str, Any]] = []
                kept_about_quotes: list[str] = []
                for about_index, item in enumerate(raw_interaction["about"]):
                    about_path = f"{interaction_path}.about[{about_index}]"
                    try:
                        kept_about.append(
                            _validate_facet(
                                item,
                                source.content,
                                interaction_parent_span,
                                about_path,
                                controller_warnings,
                            )
                        )
                        kept_about_quotes.append(
                            _string(item.get("quote"), f"{about_path}.quote")
                        )
                    except ProductWriterError as exc:
                        controller_warnings.append(
                            {
                                "path": about_path,
                                "code": "optional_facet_dropped",
                                "detail": str(exc),
                                "dropped_count": 1,
                            }
                        )
                interaction_about_quotes.append(kept_about_quotes)
                interaction_source_indexes.append(interaction_index)
                raw_v3_interactions.append(
                    {
                        "interaction_type": _audited_enum(raw_interaction.get("interaction_type"), set(INTERACTION_TYPES), f"{interaction_path}.interaction_type", controller_warnings),
                        "status": _audited_enum(raw_interaction.get("status"), set(INTERACTION_STATUSES), f"{interaction_path}.status", controller_warnings),
                        "evidence_span_id": interaction_evidence_id,
                        "intent": _role_identifier(raw_interaction.get("intent"), f"{interaction_path}.intent", controller_warnings),
                        "about": kept_about,
                    }
                )
            except GroundingIntegrityError:
                raise
            except ProductWriterError as exc:
                controller_warnings.append(
                    {
                        "path": interaction_path,
                        "code": "invalid_interaction_quarantined",
                        "detail": str(exc),
                        "dropped_count": 1,
                    }
                )
        raw_v3_resolutions: list[dict[str, Any]] = []
        for resolution_index, raw_resolution in enumerate(resolutions):
            resolution_path = f"{path}.resolutions[{resolution_index}]"
            try:
                if not isinstance(raw_resolution, Mapping):
                    raise ProductWriterError(f"{resolution_path} must be an object")
                _exact_keys(raw_resolution, {"target", "resolution", "evidence_span_id"}, resolution_path)
                target = raw_resolution.get("target")
                if not isinstance(target, Mapping):
                    raise ProductWriterError(f"{resolution_path}.target must be an object")
                kind = target.get("kind")
                if kind == "existing":
                    _exact_keys(target, {"kind", "interaction_id"}, f"{resolution_path}.target")
                    interaction_id = _string(target.get("interaction_id"), f"{resolution_path}.target.interaction_id")
                    if interaction_id not in existing_ids:
                        raise ProductWriterError(f"{resolution_path} targets an unknown existing interaction")
                elif kind == "batch":
                    _exact_keys(target, {"kind", "message_id", "interaction_index"}, f"{resolution_path}.target")
                    target_message_id = _string(target.get("message_id"), f"{resolution_path}.target.message_id")
                    target_index = _integer(target.get("interaction_index"), f"{resolution_path}.target.interaction_index")
                    if target_message_id not in prior_message_ids or target_index < 0:
                        raise ProductWriterError(f"{resolution_path} batch target must reference an earlier message and interaction")
                    raw_target_id = _deterministic_interaction_id(
                        batch.scope_id, target_message_id, target_index
                    )
                    interaction_id = prior_interactions.get(raw_target_id, "")
                    if not interaction_id:
                        raise ProductWriterError(f"{resolution_path} batch target interaction index is invalid")
                else:
                    raise ProductWriterError(f"{resolution_path}.target.kind must be existing or batch")
                raw_v3_resolutions.append(
                    {
                        "interaction_id": interaction_id,
                        "resolution": _enum(raw_resolution.get("resolution"), set(RESOLUTION_STATES), f"{resolution_path}.resolution"),
                        "evidence_span_id": _validate_evidence(raw_resolution.get("evidence_span_id"), source.content, f"{resolution_path}.evidence_span_id", allowed_evidence_ids),
                    }
                )
            except ProductWriterError as exc:
                controller_warnings.append(
                    {
                        "path": resolution_path,
                        "code": "optional_resolution_dropped",
                        "detail": str(exc),
                        "dropped_count": 1,
                    }
                )
        v3_payload = {
            "schema_version": "tmcra.memory-write.v3.4",
            "message_role": source.role,
            "assertions": raw_v3_assertions,
            "interactions": raw_v3_interactions,
            "resolutions": raw_v3_resolutions,
        }
        normalized = validate_writer_output(
            v3_payload,
            source.content,
            message_role=source.role,
            pending_interaction_ids=[*existing_ids, *set(prior_interactions.values())],
        )
        warning_codes = {
            str(warning.get("code"))
            for warning in normalized.get("validation_warnings") or []
        }
        unsupported_warnings = sorted(warning_codes - SAFE_VALIDATION_WARNING_CODES)
        if unsupported_warnings:
            raise ProductWriterError(
                f"{path} has unsafe V3 validation warnings: {unsupported_warnings}"
            )
        normalized["validation_warnings"] = [
            *list(normalized.get("validation_warnings") or []),
            *controller_warnings,
        ]

        normalized_durability: list[str] = []
        for normalized_assertion in normalized.get("assertions") or []:
            matching = [
                index
                for index, raw_assertion in enumerate(raw_v3_assertions)
                if _assertion_identity(raw_assertion)
                == _assertion_identity(normalized_assertion)
            ]
            if not matching:
                raise ProductWriterError(
                    f"{path} validated assertion cannot be mapped to its durability"
                )
            durability_values = {durability[index] for index in matching}
            if len(durability_values) > 1:
                normalized["validation_warnings"].append(
                    {
                        "path": f"{path}.assertions",
                        "code": "conflicting_durability_defaulted_uncertain",
                        "detail": (
                            "equivalent grounded assertions used conflicting "
                            "durability values; defaulted to uncertain"
                        ),
                        "dropped_count": 0,
                    }
                )
            normalized_durability.append(
                next(iter(durability_values))
                if len(durability_values) == 1
                else "uncertain"
            )
            claim_values = {
                assertion_claim_texts[index]
                for index in matching
            }
            if len(claim_values) > 1:
                normalized["validation_warnings"].append(
                    {
                        "path": f"{path}.assertions",
                        "code": "duplicate_claim_text_canonicalized",
                        "detail": "equivalent grounded assertions used different claim_text values; selected deterministically",
                        "dropped_count": len(claim_values) - 1,
                    }
                )
            normalized_assertion["claim_text"] = min(
                claim_values,
                key=lambda item: (len(item), item.casefold(), item),
            )
            raw_facets = [
                facet
                for index in matching
                for facet in raw_v3_assertions[index]["facets"]
            ]
            raw_quotes = [
                quote
                for index in matching
                for quote in assertion_facet_quotes[index]
            ]
            exact_quotes = _facet_quote_lookup(
                list(normalized_assertion.get("facets") or []),
                raw_facets,
                raw_quotes,
            )
            for facet, quote in zip(
                normalized_assertion.get("facets") or [], exact_quotes
            ):
                facet["quote"] = quote

        deduplicated_assertions: list[dict[str, Any]] = []
        deduplicated_durability: list[str] = []
        assertion_by_grounded_claim: dict[tuple[str, str], int] = {}
        for normalized_assertion, assertion_durability in zip(
            normalized.get("assertions") or [], normalized_durability
        ):
            claim_key = (
                _normalized_claim(str(normalized_assertion["claim_text"])),
                _normalized_evidence(str(normalized_assertion["evidence_quote"])),
            )
            existing_index = assertion_by_grounded_claim.get(claim_key)
            if existing_index is None:
                assertion_by_grounded_claim[claim_key] = len(deduplicated_assertions)
                deduplicated_assertions.append(dict(normalized_assertion))
                deduplicated_durability.append(assertion_durability)
                continue
            if deduplicated_durability[existing_index] != assertion_durability:
                deduplicated_durability[existing_index] = "uncertain"
                normalized.setdefault("validation_warnings", []).append(
                    {
                        "path": f"{path}.assertions",
                        "code": "conflicting_durability_defaulted_uncertain",
                        "detail": (
                            "duplicate atomic claim used conflicting durability "
                            "values; defaulted to uncertain"
                        ),
                        "dropped_count": 0,
                    }
                )
            existing = deduplicated_assertions[existing_index]
            for facet in normalized_assertion.get("facets") or []:
                if facet not in existing["facets"]:
                    existing["facets"].append(facet)
            normalized.setdefault("validation_warnings", []).append(
                {
                    "path": f"{path}.assertions",
                    "code": "duplicate_atomic_claim_merged",
                    "detail": "same atomic claim and evidence were emitted under multiple slots",
                    "dropped_count": 1,
                }
            )
        normalized["assertions"] = deduplicated_assertions
        normalized_durability = deduplicated_durability

        for normalized_interaction_index, normalized_interaction in enumerate(
            normalized.get("interactions") or []
        ):
            matching = [
                index
                for index, raw_interaction in enumerate(raw_v3_interactions)
                if _interaction_identity(raw_interaction)
                == _interaction_identity(normalized_interaction)
            ]
            if not matching:
                raise ProductWriterError(
                    f"{path} validated interaction cannot be mapped to exact about quotes"
                )
            raw_about = [
                facet
                for index in matching
                for facet in raw_v3_interactions[index]["about"]
            ]
            raw_quotes = [
                quote
                for index in matching
                for quote in interaction_about_quotes[index]
            ]
            exact_quotes = _facet_quote_lookup(
                list(normalized_interaction.get("about") or []),
                raw_about,
                raw_quotes,
            )
            for facet, quote in zip(
                normalized_interaction.get("about") or [], exact_quotes
            ):
                facet["quote"] = quote
            persisted_interaction_id = _deterministic_interaction_id(
                batch.scope_id, source.message_id, normalized_interaction_index
            )
            for raw_interaction_index in matching:
                source_interaction_index = interaction_source_indexes[
                    raw_interaction_index
                ]
                raw_interaction_id = _deterministic_interaction_id(
                    batch.scope_id, source.message_id, source_interaction_index
                )
                prior_interactions[raw_interaction_id] = persisted_interaction_id
        normalized_messages.append(
            {
                "message_id": source.message_id,
                "message_role": source.role,
                "v3": normalized,
                "durability": normalized_durability,
            }
        )
        prior_message_ids.add(source.message_id)
    return {
        "schema_version": BATCH_SCHEMA_VERSION,
        "batch_id": batch.batch_id,
        "messages": normalized_messages,
    }


class BatchClient(Protocol):
    def complete(self, payload: Mapping[str, Any]) -> Any:
        ...


class ReconciliationClient(Protocol):
    def reconcile(self, payload: Mapping[str, Any]) -> Any:
        ...


class BatchAPIError(ProductWriterError):
    def __init__(self, message: str, *, metadata: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.metadata = dict(metadata)


class DeepSeekBatchClient:
    """One-shot OpenAI-compatible client; no retry or fallback policy lives here."""

    def __init__(self, *, base_url: str, model: str, api_keys: Sequence[str], timeout: float = 180.0, max_tokens: int = 8192) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_keys = [clean_text(value) for value in api_keys if clean_text(value)]
        self.timeout = float(timeout)
        self.max_tokens = int(max_tokens)
        # The commercial service may set this to a privacy-safe business-side
        # identity.  It is intentionally empty for benchmark/reproduction runs
        # so their request contract remains unchanged.
        self.user_id = ""
        self.call_count = 0
        if not self.base_url or not self.api_keys or self.timeout <= 0 or self.max_tokens <= 0:
            raise ProductWriterError("base URL and API key pool are required")
        if self.model not in {"deepseek-v4-flash", "deepseek-v4-pro"}:
            raise ProductWriterError(f"unsupported V4 writer model: {self.model!r}")

    @staticmethod
    def _usage(value: Any) -> dict[str, int]:
        if not isinstance(value, Mapping):
            raise ProductWriterError("DeepSeek success response lacks usage")

        def count(name: str, *aliases: str) -> int:
            raw = next((value.get(key) for key in (name, *aliases) if value.get(key) is not None), 0)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or int(raw) < 0:
                raise ProductWriterError(f"DeepSeek usage.{name} is invalid")
            return int(raw)

        prompt = count("prompt_tokens", "input_tokens")
        completion = count("completion_tokens", "output_tokens")
        hit = count(
            "prompt_cache_hit_tokens", "cache_read_input_tokens", "cached_tokens"
        )
        miss_value_present = any(
            value.get(key) is not None
            for key in ("prompt_cache_miss_tokens", "cache_miss_input_tokens")
        )
        miss = count("prompt_cache_miss_tokens", "cache_miss_input_tokens")
        if hit > prompt or (miss_value_present and hit + miss != prompt):
            raise ProductWriterError("DeepSeek cache usage does not balance prompt tokens")
        if not miss_value_present:
            miss = prompt - hit
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "prompt_cache_hit_tokens": hit,
            "prompt_cache_miss_tokens": miss,
            "total_tokens": count("total_tokens") or prompt + completion,
        }

    def _complete(
        self,
        *,
        model: str,
        system_prompt: str,
        payload: Mapping[str, Any],
        stage: str,
        response_schema: Mapping[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        key_index = self.call_count % len(self.api_keys)
        self.call_count += 1
        request_payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": _json(payload)},
            ],
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "response_format": (
                {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "tmcra_structured_response",
                        "strict": True,
                        "schema": dict(response_schema),
                    },
                }
                if response_schema is not None
                else {"type": "json_object"}
            ),
            "thinking": {"type": "disabled"},
            "enable_thinking": False,
        }
        user_id = clean_text(getattr(self, "user_id", ""))
        if user_id:
            if len(user_id) > 512 or re.fullmatch(r"[A-Za-z0-9_-]+", user_id) is None:
                raise ProductWriterError("DeepSeek user_id is invalid")
            request_payload["user_id"] = user_id
        physical_call_id = "dsc_" + uuid.uuid4().hex
        request_sha256 = sha256_text(_json(request_payload))
        started = time.time()
        base_metadata = {
            "physical_call_id": physical_call_id,
            "physical_api_call": True,
            "physical_api_calls": 1,
            "stage": stage,
            "model": model,
            "api_key_index": key_index,
            "request_sha256": request_sha256,
            "started_at": started,
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_keys[key_index]}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                http_status = int(response.getcode())
                raw_http = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:2000]
            metadata = {
                **base_metadata,
                "status": "http_error",
                "http_status": int(exc.code),
                "latency_seconds": round(time.time() - started, 3),
                "error": detail,
            }
            raise BatchAPIError(f"{stage} HTTP {exc.code}: {detail}", metadata=metadata) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            metadata = {
                **base_metadata,
                "status": "request_error",
                "latency_seconds": round(time.time() - started, 3),
                "error": f"{exc.__class__.__name__}: {exc}",
            }
            raise BatchAPIError(f"{stage} request failed: {exc}", metadata=metadata) from exc
        try:
            body = json.loads(raw_http)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            metadata = {
                **base_metadata,
                "status": "invalid_http_json",
                "http_status": http_status,
                "latency_seconds": round(time.time() - started, 3),
                "response_sha256": sha256_text(raw_http),
            }
            raise BatchAPIError(f"{stage} returned invalid HTTP JSON", metadata=metadata) from exc
        if not isinstance(body, Mapping):
            raise BatchAPIError(
                f"{stage} response root is not an object",
                metadata={**base_metadata, "status": "invalid_response", "http_status": http_status},
            )
        choices = body.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], Mapping):
            raise BatchAPIError(
                f"{stage} response must contain exactly one choice",
                metadata={**base_metadata, "status": "invalid_response", "http_status": http_status},
            )
        choice = choices[0]
        message = choice.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        finish_reason = clean_text(choice.get("finish_reason"))
        try:
            usage = self._usage(body.get("usage"))
        except ProductWriterError as exc:
            raise BatchAPIError(
                f"{stage} response usage is invalid: {exc}",
                metadata={**base_metadata, "status": "invalid_usage", "http_status": http_status},
            ) from exc
        metadata = {
            **base_metadata,
            **usage,
            "usage": usage,
            "status": "completed",
            "http_status": http_status,
            "response_id": clean_text(body.get("id")),
            "latency_seconds": round(time.time() - started, 3),
            "response_sha256": sha256_text(content if isinstance(content, str) else raw_http),
            "finish_reason": finish_reason,
        }
        if response_schema is not None:
            metadata["response_schema_sha256"] = sha256_text(_json(response_schema))
        if not isinstance(content, str) or not content or finish_reason != "stop":
            metadata["status"] = "incomplete_response"
            raise BatchAPIError(
                f"{stage} response was not a clean JSON completion", metadata=metadata
            )
        return content, metadata

    def complete(self, payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        if self.model != "deepseek-v4-flash":
            raise ProductWriterError("batch extraction client must use deepseek-v4-flash")
        return self._complete(model=self.model, system_prompt=BATCH_SYSTEM_PROMPT, payload=payload, stage="batch_flash")

    def reconcile(self, payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        if self.model != "deepseek-v4-pro":
            raise ProductWriterError("reconciliation client must use deepseek-v4-pro")
        prompt = (
            "You bind one new cited assertion to a compact controller-retrieved candidate-slot set. "
            "Use only supplied source quotes and candidate IDs. Return exactly one JSON object and no prose: "
            '{"slot_decision":"bind_existing|keep_proposed|quarantine",'
            '"selected_memory_id":"candidate ID or empty string",'
            '"decision":"insert|merge_support|replace_current|keep_parallel|challenge|quarantine"}. '
            "bind_existing means the new assertion is the same real-world memory slot as the selected candidate. "
            "keep_proposed means none of the candidates is the same slot and requires decision=insert with an empty "
            "selected_memory_id. quarantine means unsafe or ungrounded and requires decision=quarantine. For a bound "
            "slot: merge_support means the atomic claim is the same fact and only its new evidence should be attached; "
            "replace_current is a clear update, keep_parallel means simultaneous values, and challenge means "
            "conflicting evidence without a winner. When exact_slot_match is true, slot identity is already fixed: "
            "use bind_existing with a supplied ID, and use keep_parallel rather than insert for an independent value. "
            "Never select an ID outside the supplied candidates."
        )
        return self._complete(model=self.model, system_prompt=prompt, payload=payload, stage="reconciliation_pro")


class V4BatchStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA secure_delete=ON")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS v4_source_journal (
                    scope_id TEXT NOT NULL, session_id TEXT NOT NULL, message_id TEXT NOT NULL,
                    session_index INTEGER NOT NULL, message_index INTEGER NOT NULL, message_role TEXT NOT NULL,
                    timestamp TEXT NOT NULL, content TEXT NOT NULL, content_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL, source_record_id TEXT NOT NULL DEFAULT '', source_turn_index INTEGER NOT NULL DEFAULT 0,
                    source_persisted_at TEXT NOT NULL DEFAULT '', enrichment_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY (scope_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS v4_batch_journal (
                    batch_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    batch_index INTEGER NOT NULL, request_json TEXT NOT NULL, request_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL, api_started_at TEXT NOT NULL DEFAULT '', response_json TEXT NOT NULL DEFAULT '',
                    response_sha256 TEXT NOT NULL DEFAULT '', response_metadata_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '', recovery_history_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS v4_interactions (
                    interaction_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    message_id TEXT NOT NULL, interaction_index INTEGER NOT NULL, message_role TEXT NOT NULL,
                    interaction_json TEXT NOT NULL, status TEXT NOT NULL, resolution_history_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS v4_reconciliation_jobs (
                    job_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, batch_id TEXT NOT NULL,
                    message_id TEXT NOT NULL DEFAULT '', canonical_slot_key TEXT NOT NULL,
                    assertion_index INTEGER NOT NULL, request_json TEXT NOT NULL,
                    status TEXT NOT NULL, decision TEXT NOT NULL DEFAULT '', response_json TEXT NOT NULL DEFAULT '',
                    response_metadata_json TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS v4_message_commit_journal (
                    commit_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL,
                    scope_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    message_id TEXT NOT NULL, message_index INTEGER NOT NULL,
                    response_sha256 TEXT NOT NULL, plan_json TEXT NOT NULL DEFAULT '',
                    plan_sha256 TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                    semantic_committed INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(scope_id, message_id)
                );
                """
            )
            connection.execute("DROP TABLE IF EXISTS v4_source_records")
            connection.execute("DROP TABLE IF EXISTS v4_fast_assertion_leaves")
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(v4_source_journal)")}
            if "source_record_id" not in columns:
                connection.execute("ALTER TABLE v4_source_journal ADD COLUMN source_record_id TEXT NOT NULL DEFAULT ''")
            if "source_turn_index" not in columns:
                connection.execute("ALTER TABLE v4_source_journal ADD COLUMN source_turn_index INTEGER NOT NULL DEFAULT 0")
            if "enrichment_error" not in columns:
                connection.execute("ALTER TABLE v4_source_journal ADD COLUMN enrichment_error TEXT NOT NULL DEFAULT ''")
            if "source_persisted_at" not in columns:
                connection.execute(
                    "ALTER TABLE v4_source_journal ADD COLUMN source_persisted_at TEXT NOT NULL DEFAULT ''"
                )
            batch_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(v4_batch_journal)")
            }
            if "recovery_history_json" not in batch_columns:
                connection.execute(
                    "ALTER TABLE v4_batch_journal ADD COLUMN "
                    "recovery_history_json TEXT NOT NULL DEFAULT '[]'"
                )
            reconciliation_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(v4_reconciliation_jobs)")
            }
            if "message_id" not in reconciliation_columns:
                connection.execute(
                    "ALTER TABLE v4_reconciliation_jobs ADD COLUMN message_id TEXT NOT NULL DEFAULT ''"
                )

    def prepare(self, batch: SourceBatch, request: Mapping[str, Any]) -> sqlite3.Row:
        request_json = _json(request)
        request_hash = sha256_text(request_json)
        now = _now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for message in batch.messages:
                existing = connection.execute(
                    "SELECT * FROM v4_source_journal WHERE scope_id=? AND message_id=?",
                    (message.scope_id, message.message_id),
                ).fetchone()
                if existing is not None:
                    if existing["content_sha256"] != sha256_text(message.content) or existing["message_role"] != message.role:
                        raise ProductWriterError(f"{message.message_id}: immutable source journal content changed")
                    continue
                connection.execute(
                    "INSERT INTO v4_source_journal(scope_id,session_id,message_id,session_index,message_index,message_role,timestamp,content,content_sha256,status,source_record_id,source_turn_index,source_persisted_at,enrichment_error,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (message.scope_id, message.session_id, message.message_id, message.session_index, message.message_index,
                     message.role, message.timestamp, message.content, sha256_text(message.content), "pending", "", 0, "", "", now, now),
                )
            existing_batch = connection.execute("SELECT * FROM v4_batch_journal WHERE batch_id=?", (batch.batch_id,)).fetchone()
            if existing_batch is None:
                connection.execute(
                    "INSERT INTO v4_batch_journal(batch_id,scope_id,session_id,batch_index,request_json,request_sha256,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (batch.batch_id, batch.scope_id, batch.session_id, batch.batch_index, request_json, request_hash, "prepared", now, now),
                )
            elif existing_batch["request_sha256"] != request_hash:
                raise ProductWriterError(f"{batch.batch_id}: prepared request changed")
            connection.commit()
            return connection.execute("SELECT * FROM v4_batch_journal WHERE batch_id=?", (batch.batch_id,)).fetchone()

    def batch_row(self, batch_id: str) -> sqlite3.Row | None:
        with closing(self._connect()) as connection:
            return connection.execute(
                "SELECT * FROM v4_batch_journal WHERE batch_id=?", (batch_id,)
            ).fetchone()

    @staticmethod
    def _message_commit_id(batch: SourceBatch, message: SourceMessage) -> str:
        return f"{batch.batch_id}:{message.message_id}"

    def prepare_message_commit(
        self,
        batch: SourceBatch,
        message: SourceMessage,
        response_message: Mapping[str, Any],
    ) -> sqlite3.Row:
        commit_id = self._message_commit_id(batch, message)
        response_sha256 = _hash_json(response_message)
        now = _now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM v4_message_commit_journal WHERE commit_id=?",
                (commit_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO v4_message_commit_journal("
                    "commit_id,batch_id,scope_id,session_id,message_id,message_index,"
                    "response_sha256,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        commit_id,
                        batch.batch_id,
                        batch.scope_id,
                        batch.session_id,
                        message.message_id,
                        int(message.message_index),
                        response_sha256,
                        "prepared",
                        now,
                        now,
                    ),
                )
            else:
                identity = (
                    str(row["batch_id"]),
                    str(row["scope_id"]),
                    str(row["session_id"]),
                    str(row["message_id"]),
                    int(row["message_index"]),
                    str(row["response_sha256"]),
                )
                expected = (
                    batch.batch_id,
                    batch.scope_id,
                    batch.session_id,
                    message.message_id,
                    int(message.message_index),
                    response_sha256,
                )
                if identity != expected:
                    raise ProductWriterError(
                        f"{commit_id}: message commit identity or response changed"
                    )
            connection.commit()
            return connection.execute(
                "SELECT * FROM v4_message_commit_journal WHERE commit_id=?",
                (commit_id,),
            ).fetchone()

    def freeze_message_commit_plan(
        self, commit_id: str, plan: Mapping[str, Any]
    ) -> sqlite3.Row:
        plan_json = _json(plan)
        plan_sha256 = sha256_text(plan_json)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM v4_message_commit_journal WHERE commit_id=?",
                (commit_id,),
            ).fetchone()
            if row is None or row["status"] not in {"prepared", "committed"}:
                raise ProductWriterError(
                    f"{commit_id}: message commit is not preparable"
                )
            if clean_text(row["plan_sha256"]):
                if row["plan_sha256"] != plan_sha256:
                    raise ProductWriterError(
                        f"{commit_id}: frozen message commit plan changed"
                    )
            elif row["status"] == "committed":
                raise ProductWriterError(
                    f"{commit_id}: committed message lacks a frozen plan"
                )
            else:
                connection.execute(
                    "UPDATE v4_message_commit_journal SET plan_json=?,plan_sha256=?,"
                    "error='',updated_at=? WHERE commit_id=? AND status='prepared'",
                    (plan_json, plan_sha256, _now(), commit_id),
                )
            connection.commit()
            return connection.execute(
                "SELECT * FROM v4_message_commit_journal WHERE commit_id=?",
                (commit_id,),
            ).fetchone()

    def record_message_commit_error(self, commit_id: str, error: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE v4_message_commit_journal SET error=?,updated_at=? "
                "WHERE commit_id=? AND status='prepared'",
                (error, _now(), commit_id),
            )

    def record_batch_commit_error(self, batch_id: str, error: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE v4_batch_journal SET error=?,updated_at=? "
                "WHERE batch_id=? AND status='validated'",
                (error, _now(), batch_id),
            )

    def finalize_message_commit(
        self,
        connection: sqlite3.Connection,
        *,
        commit_id: str,
        batch: SourceBatch,
        message: SourceMessage,
        source_record_id: str,
        interactions: Sequence[Mapping[str, Any]],
        resolutions: Sequence[Mapping[str, Any]],
        semantic_committed: int,
    ) -> None:
        row = connection.execute(
            "SELECT * FROM v4_message_commit_journal WHERE commit_id=?",
            (commit_id,),
        ).fetchone()
        if row is None:
            raise ProductWriterError(f"{commit_id}: message commit journal is missing")
        if row["status"] == "committed":
            if int(row["semantic_committed"]) != int(semantic_committed):
                raise ProductWriterError(
                    f"{commit_id}: committed semantic count changed"
                )
            return
        if row["status"] != "prepared" or not clean_text(row["plan_sha256"]):
            raise ProductWriterError(
                f"{commit_id}: message commit plan is not frozen"
            )
        for index, interaction in enumerate(interactions):
            interaction_id = _deterministic_interaction_id(
                batch.scope_id, message.message_id, index
            )
            payload = {
                **dict(interaction),
                "source_record_id": source_record_id,
            }
            values = (
                interaction_id,
                batch.scope_id,
                batch.session_id,
                message.message_id,
                index,
                message.role,
                _json(payload),
                interaction.get("status", "open"),
                "[]",
            )
            connection.execute(
                "INSERT OR IGNORE INTO v4_interactions VALUES (?,?,?,?,?,?,?,?,?)",
                values,
            )
            persisted = connection.execute(
                "SELECT scope_id,session_id,message_id,interaction_index,message_role,"
                "interaction_json FROM v4_interactions WHERE interaction_id=?",
                (interaction_id,),
            ).fetchone()
            if persisted is None or tuple(persisted) != values[1:7]:
                raise ProductWriterError(
                    f"{interaction_id}: interaction identity collided"
                )
        for resolution in resolutions:
            interaction_id = str(resolution["interaction_id"])
            target = connection.execute(
                "SELECT status,resolution_history_json FROM v4_interactions "
                "WHERE interaction_id=?",
                (interaction_id,),
            ).fetchone()
            if target is None:
                raise ProductWriterError(
                    f"resolution target does not exist: {interaction_id}"
                )
            resolution_state = str(resolution["resolution"])
            next_status = (
                "resolved"
                if resolution_state == "resolved"
                else "partial"
                if resolution_state == "partial"
                else str(target["status"])
            )
            history = json.loads(target["resolution_history_json"] or "[]")
            event = {
                "resolution": resolution_state,
                "message_id": message.message_id,
                "evidence_quote": str(resolution["evidence_quote"]),
            }
            if event not in history:
                history.append(event)
            connection.execute(
                "UPDATE v4_interactions SET status=?,resolution_history_json=? "
                "WHERE interaction_id=?",
                (next_status, _json(history), interaction_id),
            )
        updated = connection.execute(
            "UPDATE v4_source_journal SET status='enriched',enrichment_error='',"
            "updated_at=? WHERE scope_id=? AND message_id=?",
            (_now(), batch.scope_id, message.message_id),
        ).rowcount
        if updated != 1:
            raise ProductWriterError(
                f"{commit_id}: source journal could not commit atomically"
            )
        updated = connection.execute(
            "UPDATE v4_message_commit_journal SET status='committed',"
            "semantic_committed=?,error='',updated_at=? "
            "WHERE commit_id=? AND status='prepared'",
            (int(semantic_committed), _now(), commit_id),
        ).rowcount
        if updated != 1:
            raise ProductWriterError(
                f"{commit_id}: message journal could not commit atomically"
            )

    def mark_api_started(self, batch_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute("SELECT status FROM v4_batch_journal WHERE batch_id=?", (batch_id,)).fetchone()
            if row is None:
                raise ProductWriterError(f"{batch_id}: batch journal is missing")
            if row["status"] == "prepared":
                connection.execute("UPDATE v4_batch_journal SET status='api_started',api_started_at=?,updated_at=? WHERE batch_id=?", (_now(), _now(), batch_id))
            elif row["status"] not in {"api_started", "validated", "committed"}:
                raise ProductWriterError(f"{batch_id}: cannot start API from status {row['status']!r}")

    def abandon_interrupted_batch_call(self, batch_id: str) -> sqlite3.Row:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status,response_json FROM v4_batch_journal WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            if row is None or row["status"] != "api_started" or clean_text(row["response_json"]):
                raise ProductWriterError(
                    f"{batch_id}: interrupted Flash recovery requires api_started without a response"
                )
            updated = connection.execute(
                "UPDATE v4_batch_journal SET status='prepared',error='',updated_at=? WHERE batch_id=? AND status='api_started' AND response_json=''",
                (_now(), batch_id),
            ).rowcount
            if updated != 1:
                raise ProductWriterError(
                    f"{batch_id}: interrupted Flash call could not be abandoned atomically"
                )
            connection.commit()
            return connection.execute(
                "SELECT * FROM v4_batch_journal WHERE batch_id=?", (batch_id,)
            ).fetchone()

    def recover_failed_billing_call(self, batch_id: str) -> sqlite3.Row:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM v4_batch_journal WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            if row is None or row["status"] != "failed" or clean_text(row["response_json"]):
                raise ProductWriterError(
                    f"{batch_id}: billing recovery requires a failed call without a response"
                )
            try:
                metadata = json.loads(str(row["response_metadata_json"] or "{}"))
            except json.JSONDecodeError as exc:
                raise ProductWriterError(
                    f"{batch_id}: billing recovery metadata is invalid"
                ) from exc
            error = clean_text(row["error"])
            if (
                clean_text(metadata.get("status")) != "http_error"
                or int(metadata.get("http_status") or 0) != 402
                or metadata.get("physical_api_call") is not True
                or not error.startswith("BatchAPIError:")
                or "HTTP 402" not in error
            ):
                raise ProductWriterError(
                    f"{batch_id}: failed call is not a proven billing rejection"
                )
            try:
                history = json.loads(str(row["recovery_history_json"] or "[]"))
            except json.JSONDecodeError as exc:
                raise ProductWriterError(
                    f"{batch_id}: recovery history is invalid"
                ) from exc
            if not isinstance(history, list):
                raise ProductWriterError(
                    f"{batch_id}: recovery history must be a list"
                )
            recovery = {
                "schema_version": "tmcra.v4.billing-call-recovery.1",
                "reason": "provider_billing_exhausted",
                "http_status": 402,
                "prior_error_sha256": sha256_text(error),
                "prior_response_metadata_sha256": sha256_text(
                    str(row["response_metadata_json"] or "{}")
                ),
                "prior_updated_at": str(row["updated_at"]),
                "recovered_at": _now(),
                "physical_api_calls": 0,
            }
            history.append(recovery)
            updated = connection.execute(
                "UPDATE v4_batch_journal SET status='prepared',api_started_at='',"
                "response_metadata_json='{}',error='',recovery_history_json=?,updated_at=? "
                "WHERE batch_id=? AND status='failed' AND response_json='' "
                "AND error=? AND response_metadata_json=?",
                (
                    _json(history),
                    recovery["recovered_at"],
                    batch_id,
                    str(row["error"]),
                    str(row["response_metadata_json"]),
                ),
            ).rowcount
            if updated != 1:
                raise ProductWriterError(
                    f"{batch_id}: billing failure could not be recovered atomically"
                )
            connection.commit()
            return connection.execute(
                "SELECT * FROM v4_batch_journal WHERE batch_id=?", (batch_id,)
            ).fetchone()

    def persist_response(self, batch_id: str, response: Mapping[str, Any], metadata: Mapping[str, Any]) -> None:
        raw = _json(response)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE v4_batch_journal SET status='validated',response_json=?,response_sha256=?,response_metadata_json=?,updated_at=? WHERE batch_id=? AND status IN ('api_started','prepared')",
                (raw, sha256_text(raw), _json(metadata), _now(), batch_id),
            )

    def revalidate_failed_response(
        self,
        batch_id: str,
        response: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> sqlite3.Row:
        raw = _json(response)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM v4_batch_journal WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if row is None or row["status"] != "failed":
                raise ProductWriterError(
                    f"{batch_id}: raw response revalidation requires failed status"
                )
            if clean_text(row["response_json"]):
                raise ProductWriterError(
                    f"{batch_id}: failed batch already has a validated response; replay is unsafe"
                )
            updated = connection.execute(
                "UPDATE v4_batch_journal SET status='validated',response_json=?,response_sha256=?,response_metadata_json=?,error='',updated_at=? WHERE batch_id=? AND status='failed' AND response_json=''",
                (raw, sha256_text(raw), _json(metadata), _now(), batch_id),
            ).rowcount
            if updated != 1:
                raise ProductWriterError(
                    f"{batch_id}: failed raw response could not be revalidated atomically"
                )
            connection.commit()
            return connection.execute(
                "SELECT * FROM v4_batch_journal WHERE batch_id=?", (batch_id,)
            ).fetchone()

    def reconciliation_jobs_for_batch(self, batch_id: str) -> list[sqlite3.Row]:
        with closing(self._connect()) as connection:
            return connection.execute(
                "SELECT * FROM v4_reconciliation_jobs WHERE batch_id=? ORDER BY created_at,job_id",
                (batch_id,),
            ).fetchall()

    def message_commit_rows_for_batch(self, batch_id: str) -> list[sqlite3.Row]:
        with closing(self._connect()) as connection:
            return connection.execute(
                "SELECT * FROM v4_message_commit_journal "
                "WHERE batch_id=? ORDER BY message_index,commit_id",
                (batch_id,),
            ).fetchall()

    def revalidate_failed_reconciliation_job(
        self,
        job_id: str,
        decision: str,
        response: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status,response_json FROM v4_reconciliation_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if row is None or row["status"] != "failed" or clean_text(row["response_json"]):
                raise ProductWriterError(
                    f"{job_id}: reconciliation revalidation requires one failed response"
                )
            updated = connection.execute(
                "UPDATE v4_reconciliation_jobs SET status='completed',decision=?,response_json=?,response_metadata_json=?,error='',updated_at=? WHERE job_id=? AND status='failed' AND response_json=''",
                (decision, _json(response), _json(metadata), _now(), job_id),
            ).rowcount
            if updated != 1:
                raise ProductWriterError(
                    f"{job_id}: reconciliation response could not be revalidated atomically"
                )
            connection.commit()

    def resume_failed_validated_batch(
        self,
        batch_id: str,
        metadata: Mapping[str, Any],
        *,
        allowed_pending_job_ids: Sequence[str] = (),
    ) -> sqlite3.Row:
        allowed_pending = {clean_text(value) for value in allowed_pending_job_ids}
        if "" in allowed_pending:
            raise ProductWriterError(
                f"{batch_id}: pending reconciliation allowlist contains an empty job ID"
            )
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status,response_json FROM v4_batch_journal WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            if row is None or row["status"] != "failed" or not clean_text(row["response_json"]):
                raise ProductWriterError(
                    f"{batch_id}: validated commit recovery requires a failed batch response"
                )
            uncertain = connection.execute(
                "SELECT job_id,status FROM v4_reconciliation_jobs WHERE batch_id=? AND status!='completed'",
                (batch_id,),
            ).fetchall()
            actual_pending = {
                clean_text(item["job_id"])
                for item in uncertain
                if clean_text(item["status"]) == "pro_pending"
            }
            unexpected = [
                (clean_text(item["job_id"]), clean_text(item["status"]))
                for item in uncertain
                if clean_text(item["status"]) != "pro_pending"
                or clean_text(item["job_id"]) not in allowed_pending
            ]
            if unexpected or actual_pending != allowed_pending:
                raise ProductWriterError(
                    f"{batch_id}: reconciliation jobs remain outside the explicit pending allowlist: "
                    f"unexpected={unexpected}, expected={sorted(allowed_pending)}, "
                    f"actual={sorted(actual_pending)}"
                )
            updated = connection.execute(
                "UPDATE v4_batch_journal SET status='validated',response_metadata_json=?,error='',updated_at=? WHERE batch_id=? AND status='failed' AND response_json!=''",
                (_json(metadata), _now(), batch_id),
            ).rowcount
            if updated != 1:
                raise ProductWriterError(
                    f"{batch_id}: failed validated batch could not resume atomically"
                )
            connection.commit()
            return connection.execute(
                "SELECT * FROM v4_batch_journal WHERE batch_id=?", (batch_id,)
            ).fetchone()

    def fail_batch(
        self,
        batch_id: str,
        error: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        with closing(self._connect()) as connection, connection:
            if metadata is None:
                connection.execute(
                    "UPDATE v4_batch_journal SET status='failed',error=?,updated_at=? WHERE batch_id=? AND status!='committed'",
                    (error, _now(), batch_id),
                )
            else:
                connection.execute(
                    "UPDATE v4_batch_journal SET status='failed',error=?,response_metadata_json=?,updated_at=? WHERE batch_id=? AND status!='committed'",
                    (error, _json(dict(metadata)), _now(), batch_id),
                )

    def set_source_record(self, scope_id: str, message_id: str, source_record_id: str, source_turn_index: int) -> None:
        with closing(self._connect()) as connection, connection:
            self.finalize_source_record(
                connection,
                scope_id=scope_id,
                message_id=message_id,
                source_record_id=source_record_id,
                source_turn_index=source_turn_index,
            )

    def finalize_source_record(
        self,
        connection: sqlite3.Connection,
        *,
        scope_id: str,
        message_id: str,
        source_record_id: str,
        source_turn_index: int,
    ) -> None:
        updated = connection.execute(
            "UPDATE v4_source_journal SET source_record_id=?,source_turn_index=?,"
            "source_persisted_at=CASE WHEN source_persisted_at='' THEN ? "
            "ELSE source_persisted_at END,updated_at=? WHERE scope_id=? "
            "AND message_id=? AND (status='pending' OR "
            "(status='failed' AND source_record_id='') OR source_record_id=?)",
            (
                source_record_id,
                int(source_turn_index),
                _now(),
                _now(),
                scope_id,
                message_id,
                source_record_id,
            ),
        ).rowcount
        if updated != 1:
            raise ProductWriterError(
                f"{message_id}: source journal could not bind real graph source record"
            )

    def source_info(self, scope_id: str, message_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM v4_source_journal WHERE scope_id=? AND message_id=?", (scope_id, message_id)).fetchone()
        if row is None:
            raise ProductWriterError(f"{message_id}: source journal row is missing")
        return dict(row)

    def delete_message(self, scope_id: str, message_id: str) -> dict[str, Any]:
        """Physically remove one owner-local incremental writer journal entry."""

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            source_row = connection.execute(
                "SELECT source_record_id FROM v4_source_journal WHERE scope_id=? AND message_id=?",
                (scope_id, message_id),
            ).fetchone()
            removed_sources = {str(source_row["source_record_id"])} if source_row and source_row["source_record_id"] else set()

            def cites_removed_source(value: Any) -> bool:
                if isinstance(value, Mapping):
                    return any(cites_removed_source(item) for item in value.values())
                if isinstance(value, list):
                    return any(cites_removed_source(item) for item in value)
                return isinstance(value, str) and value in removed_sources

            # Reconciliation/commit snapshots can embed an older Source's value
            # and aliases. Remove those audits rather than rewriting frozen hashes.
            # Existing graph facts and their own immutable Sources remain intact.
            for table, primary_key, json_column in (
                ("v4_reconciliation_jobs", "job_id", "request_json"),
                ("v4_message_commit_journal", "commit_id", "plan_json"),
            ):
                for candidate in connection.execute(
                    f"SELECT {primary_key},{json_column} FROM {table} WHERE scope_id=?", (scope_id,)
                ).fetchall():
                    if cites_removed_source(json.loads(candidate[json_column] or "{}")):
                        connection.execute(f"DELETE FROM {table} WHERE {primary_key}=?", (candidate[primary_key],))
            committed_rows = connection.execute(
                "SELECT DISTINCT batch_id FROM v4_message_commit_journal "
                "WHERE scope_id=? AND message_id=?",
                (scope_id, message_id),
            ).fetchall()
            batch_ids = {str(row["batch_id"]) for row in committed_rows}
            # A provider or validation failure can happen before the message
            # commit journal exists. Discover those prepared/failed batches
            # from their frozen request so retry/delete does not leave source
            # text behind.
            scoped_batches = connection.execute(
                "SELECT batch_id,request_json FROM v4_batch_journal WHERE scope_id=?",
                (scope_id,),
            ).fetchall()
            for row in scoped_batches:
                try:
                    request = json.loads(str(row["request_json"] or "{}"))
                except json.JSONDecodeError:
                    request = {}
                request_messages = (
                    request.get("messages") if isinstance(request, Mapping) else None
                )
                if any(
                    isinstance(item, Mapping)
                    and clean_text(item.get("message_id")) == message_id
                    for item in request_messages or []
                ):
                    batch_ids.add(str(row["batch_id"]))
            connection.execute(
                "DELETE FROM v4_interactions WHERE scope_id=? AND message_id=?",
                (scope_id, message_id),
            )
            connection.execute(
                "DELETE FROM v4_reconciliation_jobs WHERE scope_id=? AND message_id=?",
                (scope_id, message_id),
            )
            connection.execute(
                "DELETE FROM v4_message_commit_journal WHERE scope_id=? AND message_id=?",
                (scope_id, message_id),
            )
            connection.execute(
                "DELETE FROM v4_source_journal WHERE scope_id=? AND message_id=?",
                (scope_id, message_id),
            )
            deleted_batches = 0
            for batch_id in sorted(batch_ids):
                row = connection.execute(
                    "SELECT request_json FROM v4_batch_journal WHERE batch_id=?",
                    (batch_id,),
                ).fetchone()
                if row is None:
                    continue
                try:
                    request = json.loads(str(row["request_json"]))
                except json.JSONDecodeError:
                    request = {}
                request_messages = request.get("messages") if isinstance(request, Mapping) else None
                ids = {
                    clean_text(item.get("message_id"))
                    for item in request_messages or []
                    if isinstance(item, Mapping)
                }
                if not ids or ids == {message_id}:
                    connection.execute(
                        "DELETE FROM v4_batch_journal WHERE batch_id=?", (batch_id,)
                    )
                    deleted_batches += 1
            connection.commit()
        return {"batch_ids": sorted(batch_ids), "deleted_batches": deleted_batches}

    def secure_compact(self) -> None:
        with closing(self._connect()) as connection:
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("VACUUM")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def mark_source_enrichment_failed(self, batch: SourceBatch, error: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE v4_source_journal SET status='failed',enrichment_error=?,updated_at=? WHERE scope_id=? AND message_id IN ({})".format(",".join("?" for _ in batch.messages)),
                (error, _now(), batch.scope_id, *[message.message_id for message in batch.messages]),
            )

    def mark_source_enriched(self, batch: SourceBatch) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE v4_source_journal SET status='enriched',enrichment_error='',updated_at=? WHERE scope_id=? AND message_id IN ({})".format(",".join("?" for _ in batch.messages)),
                (_now(), batch.scope_id, *[message.message_id for message in batch.messages]),
            )


    def batch_row(self, batch_id: str) -> sqlite3.Row | None:
        with closing(self._connect()) as connection:
            return connection.execute("SELECT * FROM v4_batch_journal WHERE batch_id=?", (batch_id,)).fetchone()

    def unresolved_interactions(self, scope_id: str, session_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM v4_interactions WHERE scope_id=? AND session_id=? AND status IN ('open','partial') ORDER BY rowid",
                (scope_id, session_id),
            ).fetchall()
        output = []
        for row in rows:
            item = json.loads(row["interaction_json"])
            item["interaction_id"] = row["interaction_id"]
            item["message_id"] = row["message_id"]
            item["message_role"] = row["message_role"]
            output.append(item)
        return output

    def insert_interaction(self, *, interaction_id: str, scope_id: str, session_id: str, message_id: str, index: int, role: str, interaction: Mapping[str, Any]) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT OR IGNORE INTO v4_interactions VALUES (?,?,?,?,?,?,?,?,?)",
                (interaction_id, scope_id, session_id, message_id, index, role, _json(interaction), interaction.get("status", "open"), "[]"),
            )

    def update_interaction_resolution(self, interaction_id: str, resolution: str, source_message_id: str, evidence_quote: str) -> None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute("SELECT status,resolution_history_json FROM v4_interactions WHERE interaction_id=?", (interaction_id,)).fetchone()
            if row is None:
                raise ProductWriterError(f"resolution target does not exist: {interaction_id}")
            next_status = "resolved" if resolution == "resolved" else ("partial" if resolution == "partial" else row["status"])
            history = json.loads(row["resolution_history_json"] or "[]")
            event = {"resolution": resolution, "message_id": source_message_id, "evidence_quote": evidence_quote}
            if event not in history:
                history.append(event)
            connection.execute("UPDATE v4_interactions SET status=?,resolution_history_json=? WHERE interaction_id=?", (next_status, _json(history), interaction_id))

    def reconciliation_job(self, job_id: str) -> sqlite3.Row | None:
        with closing(self._connect()) as connection:
            return connection.execute("SELECT * FROM v4_reconciliation_jobs WHERE job_id=?", (job_id,)).fetchone()

    def create_reconciliation_job(
        self,
        *,
        job_id: str,
        scope_id: str,
        batch_id: str,
        message_id: str,
        slot: str,
        assertion_index: int,
        request: Mapping[str, Any],
    ) -> None:
        request_json = _json(request)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                "SELECT scope_id,batch_id,message_id,canonical_slot_key,assertion_index,request_json FROM v4_reconciliation_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if existing is not None:
                expected_identity = (
                    scope_id,
                    batch_id,
                    message_id,
                    slot,
                    int(assertion_index),
                )
                if tuple(existing)[:5] != expected_identity:
                    raise ProductWriterError(
                        f"{job_id}: reconciliation job identity collided with different evidence"
                    )
                frozen_request = json.loads(str(existing["request_json"]))
                for field in (
                    "schema_version",
                    "candidate_selector_version",
                    "canonical_slot_key",
                    "message_id",
                    "new_cited_assertion",
                ):
                    if frozen_request.get(field) != request.get(field):
                        raise ProductWriterError(
                            f"{job_id}: frozen reconciliation {field} changed"
                        )
                return
            connection.execute(
                "INSERT INTO v4_reconciliation_jobs(job_id,scope_id,batch_id,message_id,canonical_slot_key,assertion_index,request_json,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    scope_id,
                    batch_id,
                    message_id,
                    slot,
                    assertion_index,
                    request_json,
                    "pro_pending",
                    _now(),
                    _now(),
                ),
            )

    def start_reconciliation_job(self, job_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("UPDATE v4_reconciliation_jobs SET status='pro_started',updated_at=? WHERE job_id=? AND status='pro_pending'", (_now(), job_id))

    def abandon_interrupted_reconciliation_call(self, job_id: str) -> sqlite3.Row:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status,response_json FROM v4_reconciliation_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if row is None or row["status"] != "pro_started" or clean_text(row["response_json"]):
                raise ProductWriterError(
                    f"{job_id}: interrupted Pro recovery requires pro_started without a response"
                )
            updated = connection.execute(
                "UPDATE v4_reconciliation_jobs SET status='pro_pending',error='',updated_at=? WHERE job_id=? AND status='pro_started' AND response_json=''",
                (_now(), job_id),
            ).rowcount
            if updated != 1:
                raise ProductWriterError(
                    f"{job_id}: interrupted Pro call could not be abandoned atomically"
                )
            connection.commit()
            return connection.execute(
                "SELECT * FROM v4_reconciliation_jobs WHERE job_id=?", (job_id,)
            ).fetchone()

    def finish_reconciliation_job(self, job_id: str, decision: str, response: Mapping[str, Any], metadata: Mapping[str, Any]) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("UPDATE v4_reconciliation_jobs SET status='completed',decision=?,response_json=?,response_metadata_json=?,updated_at=? WHERE job_id=?", (decision, _json(response), _json(metadata), _now(), job_id))

    def fail_reconciliation_job(
        self,
        job_id: str,
        error: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE v4_reconciliation_jobs SET status='failed',error=?,response_metadata_json=?,updated_at=? WHERE job_id=?",
                (error, _json(dict(metadata or {})), _now(), job_id),
            )

    def commit_batch(self, batch_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT status FROM v4_batch_journal WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if row is not None and row["status"] == "committed":
                return
            updated = connection.execute(
                "UPDATE v4_batch_journal SET status='committed',updated_at=? WHERE batch_id=? AND status='validated'",
                (_now(), batch_id),
            ).rowcount
            if updated != 1:
                raise ProductWriterError(
                    f"{batch_id}: validated batch could not transition to committed"
                )


def configure_real_graph_environment(
    overrides: Mapping[str, Any] | None = None,
) -> None:
    """Apply the stable graph defaults without disabling a selected embedder.

    The benchmark writer historically forced every optional retrieval path off.
    A local product runtime must preserve the user's configured dense index, so
    these values are defaults only and explicit runtime overrides win.
    """

    defaults = {
        "TMCRA_PROFILE_CONSOLIDATOR_ENABLED": "0",
        "TMCRA_LEGACY_PROFILE_LAYER_ENABLED": "0",
        "TMCRA_MEMORY_ROUTER_MODE": "off",
        "TMCRA_INJECTION_PLANNER_MODE": "off",
        "TMCRA_TEMPORAL_LAYER_MODE": "off",
        "TMCRA_TEMPORAL_ROUTER_MODE": "off",
        "TMCRA_DEEPSEEK_GRAPH_MODEL_MODE": "off",
        "TMCRA_TOPIC_BUCKET_MODE": "off",
        "TMCRA_MULTI_UNIT_CHAIN_SLOT_MODE": "off",
        "TMCRA_UNIT_COVERAGE_PACK_MODE": "off",
    }
    for name, value in defaults.items():
        os.environ.setdefault(name, value)
    for name, value in dict(overrides or {}).items():
        if not str(name).startswith("TMCRA_"):
            raise ProductWriterError(f"invalid graph environment key: {name!r}")
        os.environ[str(name)] = str(value)


class RealGraphBackend:
    """Adapter boundary for the real TMCRA graph; V4 tables never mirror graph leaves."""

    supports_atomic_message_commit = True

    _commit_locks_guard = threading.Lock()
    _commit_locks: weakref.WeakValueDictionary[str, threading.RLock] = (
        weakref.WeakValueDictionary()
    )

    def __init__(
        self,
        *,
        repo: Path,
        database: Path,
        scope_id: str,
        audit_retention: int = 4096,
        retrieval_mode: str = "heuristic",
        node_model_path: str = "",
        path_model_path: str = "",
        node_model_device: str = "",
        graph_environment: Mapping[str, Any] | None = None,
    ) -> None:
        configure_real_graph_environment(graph_environment)
        repo = Path(repo).resolve()
        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))
        try:
            from experiments.replacement.adapters.memory_adapters import GraphSessionMemoryAdapter
            from experiments.replacement.memory_graph import (
                SessionMemoryEdgeV2,
                SessionMemoryRecordV2,
                StaleGraphSnapshotError,
            )
        except ImportError as exc:
            raise ProductWriterError(f"--repo does not expose the real TMCRA graph adapter: {repo}") from exc
        self._record_class = SessionMemoryRecordV2
        self._edge_class = SessionMemoryEdgeV2
        self._stale_snapshot_error_class = StaleGraphSnapshotError
        self.adapter = GraphSessionMemoryAdapter(
            auto_extract=False,
            storage_backend="sqlite",
            storage_path=str(database),
            scope_id=scope_id,
            audit_retention=max(256, int(audit_retention)),
            retrieval_mode=str(retrieval_mode or "heuristic"),
            node_model_path=str(node_model_path or ""),
            path_model_path=str(path_model_path or ""),
            node_model_device=str(node_model_device or ""),
        )
        self.scope_id = scope_id
        self._scope_lock_depth = 0
        self._defer_reload_depth = 0
        self._deferred_persist_dirty = False
        self._deferred_transaction_hooks: list[
            Callable[[sqlite3.Connection], None]
        ] = []
        self._slow_refresh_pending = False
        self._persisted_graph_rows = self._capture_persisted_graph_rows()

    def _commit_lock_key(self) -> str:
        return f"{Path(self.adapter.storage_path).resolve()}\0{self.scope_id}"

    def _process_commit_lock(self) -> threading.RLock:
        key = self._commit_lock_key()
        with self._commit_locks_guard:
            lock = self._commit_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._commit_locks[key] = lock
            return lock

    @contextmanager
    def _commit_guard(self):
        """Serialize only the same-scope graph mutation critical section."""

        if getattr(self, "_scope_lock_depth", 0):
            self._scope_lock_depth += 1
            try:
                yield
            finally:
                self._scope_lock_depth -= 1
            return

        lock_path = Path(self.adapter.storage_path).resolve().with_name(
            f".{Path(self.adapter.storage_path).name}.{sha256_text(self.scope_id)[:16]}.commit.lock"
        )
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._process_commit_lock(), lock_path.open("a+b") as lock_file:
            try:
                import fcntl
            except ImportError:
                fcntl = None
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            self._scope_lock_depth = 1
            try:
                yield
            finally:
                self._scope_lock_depth = 0
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def mutation_batch(self):
        """Load once while serializing one same-scope local mutation batch."""

        if getattr(self, "_defer_reload_depth", 0):
            self._defer_reload_depth += 1
            try:
                yield
            finally:
                self._defer_reload_depth -= 1
            return
        with self._commit_guard():
            self._reload(force=True)
            self._defer_reload_depth = 1
            self._deferred_persist_dirty = False
            self._deferred_transaction_hooks = []
            try:
                yield
                if self._deferred_persist_dirty:
                    hooks = tuple(self._deferred_transaction_hooks)

                    def finalize(connection: sqlite3.Connection) -> None:
                        for hook in hooks:
                            hook(connection)

                    self._persist_immediate(
                        transaction_hook=finalize if hooks else None
                    )
            except Exception:
                # Frozen Writer plans are durable outside this graph transaction.
                # Discard only uncommitted in-memory graph mutations so a retry
                # can replay those plans without repeating model calls.
                self._defer_reload_depth = 0
                self._deferred_persist_dirty = False
                self._deferred_transaction_hooks = []
                self._reload(force=True)
                raise
            finally:
                self._defer_reload_depth = 0
                self._deferred_persist_dirty = False
                self._deferred_transaction_hooks = []

    @property
    def transaction_batch_active(self) -> bool:
        return bool(getattr(self, "_defer_reload_depth", 0))

    def defer_transaction_hook(
        self, hook: Callable[[sqlite3.Connection], None]
    ) -> None:
        if not self.transaction_batch_active:
            raise ProductWriterError(
                "graph transaction hook requires an active mutation batch"
            )
        self._deferred_transaction_hooks.append(hook)
        self._deferred_persist_dirty = True

    def _reload(self, *, force: bool = False) -> None:
        if getattr(self, "_defer_reload_depth", 0) and not force:
            return
        self.adapter._reload_graph()
        self._slow_refresh_pending = True
        self._persisted_graph_rows = self._capture_persisted_graph_rows()

    def refresh_after_stale_snapshot(self) -> None:
        self._reload(force=True)

    def _capture_persisted_graph_rows(
        self,
    ) -> dict[
        str,
        tuple[
            tuple[str, ...],
            tuple[str, ...],
            dict[tuple[Any, ...], tuple[Any, ...]],
        ],
    ]:
        if not getattr(self, "scope_id", "") or getattr(
            getattr(self, "adapter", None), "_store", None
        ) is None:
            return {}
        return self._serialized_graph_rows(
            storage_revision=int(
                getattr(self.adapter.graph, "_storage_revision", 0) or 0
            )
        )

    def is_stale_snapshot_error(self, exc: BaseException) -> bool:
        return isinstance(exc, self._stale_snapshot_error_class)

    def _loaded_leaf(self, memory_id: str) -> dict[str, Any] | None:
        record = self.adapter.graph.records_by_id.get(memory_id)
        if record is None:
            return None
        metadata = self._source_metadata(record)
        if (
            metadata.get("content_variant") != "product_semantic_memory"
            or metadata.get("memory_layer") != "fast"
            or metadata.get("node_kind") != "atomic_user_assertion"
        ):
            return None
        return {
            "memory_id": str(record.memory_id),
            "value": str(record.value),
            "claim_text": str(record.value),
            "evidence_quote": clean_text(metadata.get("evidence_quote"))
            or str(record.value),
            "canonical_slot_key": clean_text(metadata.get("canonical_slot_key")),
            "durability": clean_text(metadata.get("durability")),
            "record_state": str(record.state),
            "turn_index": int(record.turn_index),
            "metadata": metadata,
        }

    def _loaded_current_leaves(self, graph_slot_key: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for record in self.adapter.graph.records_by_id.values():
            metadata = self._source_metadata(record)
            if (
                metadata.get("content_variant") == "product_semantic_memory"
                and metadata.get("memory_layer") == "fast"
                and metadata.get("node_kind") == "atomic_user_assertion"
                and metadata.get("canonical_slot_key") == graph_slot_key
                and str(record.state) in {"active", "parallel_active", "promoted"}
            ):
                loaded = self._loaded_leaf(str(record.memory_id))
                if loaded is not None:
                    rows.append(loaded)
        return rows

    def _validate_frozen_commit_context(
        self,
        *,
        extraction: Mapping[str, Any],
        current_by_index: Mapping[int, Sequence[Mapping[str, Any]]],
        duplicate_provenance: Sequence[Mapping[str, Any]],
    ) -> None:
        """Reject a frozen plan when a graph change touched a cited slot."""

        active_states = {"active", "parallel_active", "promoted"}
        assertions = list(extraction.get("assertions") or [])
        for assertion_index, assertion in enumerate(assertions):
            if not isinstance(assertion, Mapping):
                raise ProductWriterError(
                    "frozen message commit plan contains an invalid assertion"
                )
            graph_slot_key = _graph_slot_key(clean_text(assertion.get("canonical_key")))
            expected = [dict(item) for item in current_by_index.get(assertion_index, [])]
            for frozen_leaf in expected:
                memory_id = clean_text(frozen_leaf.get("memory_id"))
                current_leaf = self._loaded_leaf(memory_id)
                if (
                    not memory_id
                    or current_leaf is None
                    or clean_text(current_leaf.get("record_state")) not in active_states
                    or _binding_identity(current_leaf) != _binding_identity(frozen_leaf)
                ):
                    raise ProductWriterError(
                        "frozen message commit plan references a changed graph leaf"
                    )
            expected_slot = sorted(
                _json(_binding_identity(item))
                for item in expected
                if clean_text(
                    item.get("canonical_slot_key")
                    or dict(item.get("metadata") or {}).get("canonical_slot_key")
                )
                == graph_slot_key
                and clean_text(item.get("record_state")) in active_states
            )
            current_slot = sorted(
                _json(_binding_identity(item))
                for item in self._loaded_current_leaves(graph_slot_key)
            )
            if current_slot != expected_slot:
                raise ProductWriterError(
                    "frozen message commit plan no longer matches its graph slot"
                )

        for item in duplicate_provenance:
            leaf_id = clean_text(item.get("leaf_id"))
            current_leaf = self._loaded_leaf(leaf_id)
            if (
                not leaf_id
                or current_leaf is None
                or clean_text(current_leaf.get("record_state")) not in active_states
            ):
                raise ProductWriterError(
                    "frozen duplicate provenance target is no longer active"
                )
            frozen_identity = item.get("leaf_identity")
            if not isinstance(frozen_identity, Mapping):
                raise ProductWriterError(
                    "frozen duplicate provenance target lacks its identity"
                )
            if dict(frozen_identity) != _binding_identity(current_leaf):
                raise ProductWriterError(
                    "frozen duplicate provenance target changed"
                )

    def _persist(
        self,
        transaction_hook: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        if self.transaction_batch_active:
            self._deferred_persist_dirty = True
            if transaction_hook is not None:
                self._deferred_transaction_hooks.append(transaction_hook)
            return
        self._persist_immediate(transaction_hook=transaction_hook)

    def _persist_immediate(
        self,
        transaction_hook: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        store = getattr(self.adapter, "_store", None)
        if store is None:
            if transaction_hook is not None:
                raise ProductWriterError(
                    "atomic V4 commit requires the SQLite graph store"
                )
            self.adapter._persist_graph()
            return
        self.adapter.graph.configure_persistence(
            backend=self.adapter.storage_backend,
            path=self.adapter.storage_path,
            audit_retention=self.adapter.audit_retention,
        )
        graph = self.adapter.graph
        raw_expected_revision = getattr(graph, "_storage_revision", None)
        expected_revision = (
            int(raw_expected_revision or 0)
            if raw_expected_revision is not None
            else None
        )
        with store._managed_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            revision_row = connection.execute(
                "SELECT value_json FROM meta WHERE scope_id=? "
                "AND key='storage_revision'",
                (self.scope_id,),
            ).fetchone()
            current_revision = (
                int(json.loads(revision_row["value_json"]) or 0)
                if revision_row
                else 0
            )
            if expected_revision is None:
                existing_record_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM records WHERE scope_id=?",
                        (self.scope_id,),
                    ).fetchone()[0]
                )
                if current_revision or existing_record_count:
                    raise self._stale_snapshot_error_class(
                        "unversioned graph cannot replace an existing scope; "
                        "load_graph must establish the snapshot revision first"
                    )
                expected_revision = 0
            if expected_revision != current_revision:
                raise self._stale_snapshot_error_class(
                    "graph snapshot revision is stale: "
                    f"expected={expected_revision}, current={current_revision}"
                )

            # Slow-graph control-plane writes and append-only audit events can
            # land outside the Writer process. Merge them before calculating
            # this transaction's desired rows, matching the canonical store.
            if getattr(self, "_slow_refresh_pending", False):
                store._refresh_authoritative_slow_state(
                    connection, self.scope_id, graph
                )
            store._refresh_authoritative_audit_state(
                connection, self.scope_id, graph
            )
            next_revision = current_revision + 1
            desired = self._serialized_graph_rows(
                storage_revision=next_revision
            )
            for table, specification in desired.items():
                columns, key_columns, rows = specification
                previous_rows = getattr(self, "_persisted_graph_rows", {}).get(
                    table, (columns, key_columns, {})
                )[2]
                self._sync_graph_table(
                    connection,
                    table=table,
                    columns=columns,
                    key_columns=key_columns,
                    previous_rows=previous_rows,
                    desired_rows=rows,
                )
            if transaction_hook is not None:
                transaction_hook(connection)
        graph._storage_revision = next_revision
        self._persisted_graph_rows = desired
        self._slow_refresh_pending = False
        self.adapter._invalidate_runtime_graph_cache()

    def _serialized_graph_rows(
        self, *, storage_revision: int
    ) -> dict[str, tuple[tuple[str, ...], tuple[str, ...], dict[tuple[Any, ...], tuple[Any, ...]]]]:
        graph = self.adapter.graph

        def payload(value: Any) -> str:
            return json.dumps(value, ensure_ascii=False)

        records = {
            (str(record.memory_id),): (
                self.scope_id,
                str(record.memory_id),
                str(record.category),
                str(record.slot_key),
                str(record.value),
                str(record.relation),
                payload(list(record.anchor_concepts)),
                payload(list(record.evidence_anchors)),
                float(record.salience),
                float(record.confidence),
                str(record.source_kind),
                int(record.turn_index),
                str(record.state),
                payload(list(record.supersedes)),
                payload(dict(record.metadata)),
            )
            for record in graph.records_by_id.values()
        }
        slot_heads = {
            (str(slot_key),): (
                self.scope_id,
                str(slot_key),
                str(memory_id),
            )
            for slot_key, memory_id in graph.slot_heads.items()
        }
        slot_history = {
            (str(slot_key), int(ordinal)): (
                self.scope_id,
                str(slot_key),
                int(ordinal),
                str(memory_id),
            )
            for slot_key, memory_ids in graph.slot_history.items()
            for ordinal, memory_id in enumerate(memory_ids)
        }
        edges = {
            (str(edge.edge_id),): (
                self.scope_id,
                str(edge.edge_id),
                str(edge.source_memory_id),
                str(edge.target_memory_id),
                str(edge.edge_type),
                float(edge.score),
                float(edge.model_score),
                int(edge.evidence_turn),
                str(edge.evidence),
                payload(dict(edge.metadata)),
            )
            for edge in graph.memory_edges.values()
        }
        subject_heads = {
            (str(subject_signature), str(depth_layer)): (
                self.scope_id,
                str(subject_signature),
                str(depth_layer),
                str(memory_id),
            )
            for subject_signature, heads in graph.subject_depth_heads.items()
            for depth_layer, memory_id in heads.items()
        }

        audit_rows: dict[str, dict[tuple[Any, ...], tuple[Any, ...]]] = {}
        for table, events in (
            ("audit_turn_log", list(graph.turn_log)),
            ("audit_retrieval_log", list(graph.retrieval_log)),
            ("audit_answer_support", list(graph.answer_support_log)),
        ):
            audit_rows[table] = {
                (int(event_index),): (
                    self.scope_id,
                    int(event_index),
                    payload(dict(event)),
                )
                for event_index, event in enumerate(events)
            }

        meta_entries = {
            "turn_index": int(graph.turn_index),
            "noise_turn_count": int(graph.noise_turn_count),
            "audit_retention": int(graph.audit_retention),
            "audit_turn_events": int(
                graph.audit_event_totals.get("turn_log", 0) or 0
            ),
            "audit_retrieval_events": int(
                graph.audit_event_totals.get("retrieval_log", 0) or 0
            ),
            "audit_answer_support_events": int(
                graph.audit_event_totals.get("answer_support_log", 0) or 0
            ),
            "audit_trimmed_turn_log": int(
                graph.audit_trimmed_counts.get("turn_log", 0) or 0
            ),
            "audit_trimmed_retrieval_log": int(
                graph.audit_trimmed_counts.get("retrieval_log", 0) or 0
            ),
            "audit_trimmed_answer_support_log": int(
                graph.audit_trimmed_counts.get("answer_support_log", 0) or 0
            ),
            "schema_version": 3,
            "storage_revision": int(storage_revision),
        }
        meta = {
            (str(key),): (self.scope_id, str(key), payload(value))
            for key, value in meta_entries.items()
        }
        return {
            "records": (
                (
                    "scope_id",
                    "memory_id",
                    "category",
                    "slot_key",
                    "value",
                    "relation",
                    "anchor_concepts_json",
                    "evidence_anchors_json",
                    "salience",
                    "confidence",
                    "source_kind",
                    "turn_index",
                    "state",
                    "supersedes_json",
                    "metadata_json",
                ),
                ("memory_id",),
                records,
            ),
            "slot_heads": (
                ("scope_id", "slot_key", "memory_id"),
                ("slot_key",),
                slot_heads,
            ),
            "slot_history": (
                ("scope_id", "slot_key", "ordinal", "memory_id"),
                ("slot_key", "ordinal"),
                slot_history,
            ),
            "memory_edges": (
                (
                    "scope_id",
                    "edge_id",
                    "source_memory_id",
                    "target_memory_id",
                    "edge_type",
                    "score",
                    "model_score",
                    "evidence_turn",
                    "evidence",
                    "metadata_json",
                ),
                ("edge_id",),
                edges,
            ),
            "subject_depth_heads": (
                (
                    "scope_id",
                    "subject_signature",
                    "depth_layer",
                    "memory_id",
                ),
                ("subject_signature", "depth_layer"),
                subject_heads,
            ),
            "audit_turn_log": (
                ("scope_id", "event_index", "payload_json"),
                ("event_index",),
                audit_rows["audit_turn_log"],
            ),
            "audit_retrieval_log": (
                ("scope_id", "event_index", "payload_json"),
                ("event_index",),
                audit_rows["audit_retrieval_log"],
            ),
            "audit_answer_support": (
                ("scope_id", "event_index", "payload_json"),
                ("event_index",),
                audit_rows["audit_answer_support"],
            ),
            "meta": (
                ("scope_id", "key", "value_json"),
                ("key",),
                meta,
            ),
        }

    def _sync_graph_table(
        self,
        connection: sqlite3.Connection,
        *,
        table: str,
        columns: tuple[str, ...],
        key_columns: tuple[str, ...],
        previous_rows: Mapping[tuple[Any, ...], tuple[Any, ...]],
        desired_rows: Mapping[tuple[Any, ...], tuple[Any, ...]],
    ) -> None:
        removed = sorted(set(previous_rows) - set(desired_rows))
        if removed:
            where = " AND ".join(
                ["scope_id=?", *(f"{column}=?" for column in key_columns)]
            )
            connection.executemany(
                f"DELETE FROM {table} WHERE {where}",
                [(self.scope_id, *key) for key in removed],
            )

        changed = [
            row
            for key, row in desired_rows.items()
            if previous_rows.get(key) != row
        ]
        if not changed:
            return
        conflict_columns = ("scope_id", *key_columns)
        mutable_columns = tuple(
            column for column in columns if column not in conflict_columns
        )
        placeholders = ",".join("?" for _ in columns)
        if mutable_columns:
            updates = ",".join(
                f"{column}=excluded.{column}" for column in mutable_columns
            )
            conflict = ",".join(conflict_columns)
            statement = (
                f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders}) "
                f"ON CONFLICT({conflict}) DO UPDATE SET {updates}"
            )
        else:
            statement = (
                f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) "
                f"VALUES ({placeholders})"
            )
        connection.executemany(statement, changed)

    @staticmethod
    def _source_metadata(record: Any) -> dict[str, Any]:
        return dict(getattr(record, "metadata", {}) or {})

    @classmethod
    def _verified_source_content(cls, record: Any) -> str:
        metadata = cls._source_metadata(record)
        raw_content = metadata.get("raw_content")
        if not isinstance(raw_content, str) or not raw_content:
            raise ProductWriterError("real immutable source lacks exact raw_content")
        for field in ("source_span", "source_turn_text"):
            if metadata.get(field) != raw_content:
                raise ProductWriterError(
                    f"real immutable source {field} differs from raw_content"
                )
        # The legacy graph store normalizes whitespace in record.value. Exact
        # source text lives in raw_content; value must remain equivalent.
        if clean_text(getattr(record, "value", "")) != clean_text(raw_content):
            raise ProductWriterError(
                "real immutable source graph value differs from raw_content"
            )
        return raw_content

    def ensure_source(self, message: SourceMessage) -> tuple[str, int]:
        with self._commit_guard():
            self._reload()
            for record in self.adapter.graph.records_by_id.values():
                metadata = self._source_metadata(record)
                if metadata.get("content_variant") != "source_message" or metadata.get("message_id") != message.message_id:
                    continue
                if self._verified_source_content(record) != message.content:
                    raise ProductWriterError(f"{message.message_id}: real source graph content changed")
                if int(metadata.get("session_index", -1)) != message.session_index or int(
                    metadata.get("message_index", -1)
                ) != message.message_index:
                    raise ProductWriterError(
                        f"{message.message_id}: real source graph location changed"
                    )
                if normalize_identity_metadata(metadata.get("identity_metadata")) != normalize_identity_metadata(message.identity_metadata):
                    raise ProductWriterError(f"{message.message_id}: immutable source identity metadata changed")
                return str(record.memory_id), int(record.turn_index)
            turn_index = self.adapter.graph.next_turn()
            records, _ = build_graph_records(
                self._record_class,
                scope_id=self.scope_id,
                turn_index=turn_index,
                session_id=message.session_id,
                session_index=message.session_index,
                message_id=message.message_id,
                message_index=message.message_index,
                date=message.timestamp[:10],
                timestamp=message.timestamp,
                role=message.role,
                content=message.content,
                extraction=None,
                actor_metadata=message.actor_metadata,
            )
            source = records[0]
            source.metadata.update(
                {
                    "source": "tmcra_v4_batch_writer",
                    "writer_schema_version": BATCH_SCHEMA_VERSION,
                    "prompt_version": PROMPT_VERSION,
                    "enrichment_status": "pending",
                    "source_record_id": source.memory_id,
                    "identity_metadata": normalize_identity_metadata(message.identity_metadata),
                }
            )
            stored = self.adapter.graph.add_records([source])
            if source.memory_id not in stored and source.memory_id not in self.adapter.graph.records_by_id:
                raise ProductWriterError(f"{message.message_id}: real immutable source record was not persisted")
            self.adapter.graph.record_turn(
                turn_kind="memory_write",
                text=message.content,
                turn_index=turn_index,
                record_ids=[source.memory_id],
                speaker=message.role,
                metadata={
                    "source": "tmcra_v4_batch_writer",
                    "message_id": message.message_id,
                    "source_record_id": source.memory_id,
                    "enrichment_status": "pending",
                    **dict(message.actor_metadata),
                },
            )
            self._persist()
        self.verify_source(message, str(source.memory_id), turn_index)
        return str(source.memory_id), turn_index

    def verify_source(
        self,
        message: SourceMessage,
        source_record_id: str,
        source_turn_index: int,
    ) -> None:
        self._reload()
        record = self.adapter.graph.records_by_id.get(source_record_id)
        if record is None:
            raise ProductWriterError(
                f"{message.message_id}: committed real source record is missing"
            )
        metadata = self._source_metadata(record)
        expected = {
            "content_variant": "source_message",
            "message_id": message.message_id,
            "session_id": message.session_id,
            "session_index": message.session_index,
            "message_index": message.message_index,
        }
        for key, value in expected.items():
            if metadata.get(key) != value:
                raise ProductWriterError(
                    f"{message.message_id}: committed real source {key} changed"
                )
        if clean_text(metadata.get("actor_role") or metadata.get("speaker")) != message.role:
            raise ProductWriterError(
                f"{message.message_id}: committed real source actor_role changed"
            )
        for key in (
            "agent_id",
            "agent_name",
            "agent_role",
            "agent_specialty",
            "agent_team",
            "target_agent_id",
            "origin_kind",
            "capture_method",
            "provenance_strength",
        ):
            if clean_text(metadata.get(key)) != clean_text(
                message.actor_metadata.get(key)
            ):
                raise ProductWriterError(
                    f"{message.message_id}: committed real source {key} changed"
                )
        if normalize_identity_metadata(metadata.get("identity_metadata")) != normalize_identity_metadata(message.identity_metadata):
            raise ProductWriterError(f"{message.message_id}: committed real source identity metadata changed")
        if self._verified_source_content(record) != message.content:
            raise ProductWriterError(
                f"{message.message_id}: committed real source content changed"
            )
        if int(record.turn_index) != int(source_turn_index):
            raise ProductWriterError(
                f"{message.message_id}: committed real source turn changed"
            )

    def set_enrichment_status(self, source_record_id: str, status: str, error: str = "") -> None:
        with self._commit_guard():
            self._reload()
            record = self.adapter.graph.records_by_id.get(source_record_id)
            if record is None:
                raise ProductWriterError(f"real source record is missing: {source_record_id}")
            metadata = self._source_metadata(record)
            metadata["enrichment_status"] = status
            if error:
                metadata["enrichment_error"] = error
            else:
                metadata.pop("enrichment_error", None)
            record.metadata = metadata
            self._persist()

    def source_enrichment_statuses(
        self, source_record_ids: Sequence[str]
    ) -> dict[str, str]:
        self._reload()
        result: dict[str, str] = {}
        for source_record_id in source_record_ids:
            record = self.adapter.graph.records_by_id.get(source_record_id)
            if record is None:
                raise ProductWriterError(
                    f"real source record is missing: {source_record_id}"
                )
            result[source_record_id] = clean_text(
                self._source_metadata(record).get("enrichment_status")
            )
        return result

    def delete_message_records(self, message_id: str) -> dict[str, Any]:
        """Delete Source plus all directly grounded derivatives for a message."""

        with self._commit_guard():
            self._reload()
            graph = self.adapter.graph
            source_ids = {
                str(record.memory_id)
                for record in graph.records_by_id.values()
                if clean_text(dict(getattr(record, "metadata", {}) or {}).get("message_id"))
                == message_id
                and clean_text(
                    dict(getattr(record, "metadata", {}) or {}).get("content_variant")
                )
                == "source_message"
            }
            if not source_ids:
                return {"source_record_ids": [], "deleted_record_ids": [], "deleted_edges": 0}

            def references_deleted_source(record: Any) -> bool:
                metadata = dict(getattr(record, "metadata", {}) or {})
                if clean_text(metadata.get("message_id")) == message_id:
                    return True
                for name in (
                    "source_record_id",
                    "origin_source_record_id",
                    "primary_source_record_id",
                ):
                    if clean_text(metadata.get(name)) in source_ids:
                        return True
                for name in (
                    "source_record_ids",
                    "support_memory_ids",
                    "provenance_source_ids",
                ):
                    values = metadata.get(name)
                    if isinstance(values, Sequence) and not isinstance(
                        values, (str, bytes, bytearray)
                    ) and source_ids.intersection(clean_text(value) for value in values):
                        return True
                return False

            deleted_ids = {
                str(record.memory_id)
                for record in graph.records_by_id.values()
                if str(record.memory_id) in source_ids or references_deleted_source(record)
            }
            for memory_id in deleted_ids:
                graph.records_by_id.pop(memory_id, None)
            for record in graph.records_by_id.values():
                record.metadata = purge_identity_history(record.metadata, source_ids)
                record.supersedes = [mid for mid in getattr(record, "supersedes", []) if mid not in deleted_ids]
            deleted_edges = 0
            for edge_id, edge in list(graph.memory_edges.items()):
                if (
                    str(getattr(edge, "source_memory_id", "")) in deleted_ids
                    or str(getattr(edge, "target_memory_id", "")) in deleted_ids
                ):
                    graph.memory_edges.pop(edge_id, None)
                    deleted_edges += 1
            for slot_key in list(graph.slot_history):
                remaining = [
                    memory_id
                    for memory_id in graph.slot_history[slot_key]
                    if memory_id in graph.records_by_id
                ]
                if remaining:
                    graph.slot_history[slot_key] = remaining
                    active = list(graph._slot_active_records(slot_key))
                    if active:
                        graph.slot_heads[slot_key] = max(
                            active, key=lambda record: int(record.turn_index)
                        ).memory_id
                    else:
                        graph.slot_heads.pop(slot_key, None)
                else:
                    graph.slot_history.pop(slot_key, None)
                    graph.slot_heads.pop(slot_key, None)
            graph.subject_depth_heads = defaultdict(dict)
            for record in sorted(
                graph.records_by_id.values(), key=lambda item: int(item.turn_index)
            ):
                graph._refresh_subject_depth_head(record)
            needles = {message_id, *source_ids, *deleted_ids}

            def retained(event: Any) -> bool:
                serialized = json.dumps(event, ensure_ascii=False, default=str)
                return not any(needle and needle in serialized for needle in needles)

            for field in ("turn_log", "retrieval_log", "answer_support_log"):
                setattr(
                    graph,
                    field,
                    [event for event in getattr(graph, field) if retained(event)],
                )
            self.adapter._runtime_graph_cache_key = None
            self.adapter._runtime_graph_cache_source_hits = None
            self.adapter._runtime_graph_cache_payload = None
            self.adapter._runtime_graph_cache_symbolic_index = None
            self._persist()
            return {
                "source_record_ids": sorted(source_ids),
                "deleted_record_ids": sorted(deleted_ids),
                "deleted_edges": deleted_edges,
            }

    def current_leaves(self, canonical_slot_key: str) -> list[dict[str, Any]]:
        self._reload()
        graph_slot_key = _graph_slot_key(canonical_slot_key)
        return self._loaded_current_leaves(graph_slot_key)

    def leaf_by_id(self, memory_id: str) -> dict[str, Any] | None:
        self._reload()
        return self._loaded_leaf(memory_id)

    def leaf_for_source_assertion(
        self,
        source_record_id: str,
        assertion_index: int,
    ) -> dict[str, Any] | None:
        self._reload()
        matches: list[dict[str, Any]] = []
        for record in self.adapter.graph.records_by_id.values():
            metadata = self._source_metadata(record)
            if (
                metadata.get("content_variant") == "product_semantic_memory"
                and clean_text(metadata.get("source_record_id"))
                == source_record_id
                and int(metadata.get("llm_write_proposal_index", -1))
                == int(assertion_index)
            ):
                leaf = self.leaf_by_id(str(record.memory_id))
                if leaf is not None:
                    matches.append(leaf)
        if len(matches) > 1:
            raise ProductWriterError(
                f"{source_record_id}: assertion {assertion_index} has multiple persisted semantic leaves"
            )
        return matches[0] if matches else None

    def repair_partial_replacement(
        self,
        historical_memory_id: str,
        incoming_memory_id: str,
    ) -> dict[str, Any]:
        with self._commit_guard():
            return self._repair_partial_replacement_locked(
                historical_memory_id, incoming_memory_id
            )

    def _repair_partial_replacement_locked(
        self,
        historical_memory_id: str,
        incoming_memory_id: str,
    ) -> dict[str, Any]:
        self._reload()
        historical = self.adapter.graph.records_by_id.get(historical_memory_id)
        incoming = self.adapter.graph.records_by_id.get(incoming_memory_id)
        if historical is None or incoming is None:
            raise ProductWriterError(
                "partial replacement repair requires both persisted records"
            )
        historical_metadata = self._source_metadata(historical)
        incoming_metadata = self._source_metadata(incoming)
        existing_link = clean_text(historical_metadata.get("superseded_by"))
        if (
            str(historical.state) != "superseded"
            or clean_text(historical_metadata.get("superseded_reason"))
            != "v4_reconciliation_replace_current"
            or existing_link not in {"", incoming_memory_id}
        ):
            raise ProductWriterError(
                f"{historical_memory_id}: partial replacement lifecycle is incompatible"
            )
        historical_metadata["superseded_by"] = incoming_memory_id
        historical_metadata["superseded_reason"] = (
            "v4_reconciliation_replace_current"
        )
        historical.metadata = historical_metadata
        incoming.state = "active"
        incoming_metadata.pop("superseded_by", None)
        incoming_metadata.pop("superseded_reason", None)
        incoming.metadata = incoming_metadata
        supersedes = list(getattr(incoming, "supersedes", []) or [])
        if historical_memory_id not in supersedes:
            supersedes.append(historical_memory_id)
        incoming.supersedes = supersedes
        self.adapter.graph.slot_heads[str(incoming.slot_key)] = incoming.memory_id
        self._persist()
        repaired = self.leaf_by_id(incoming_memory_id)
        if repaired is None:
            raise ProductWriterError(
                f"{incoming_memory_id}: repaired replacement disappeared"
            )
        return repaired

    def candidate_leaves(
        self, assertion: Mapping[str, Any], *, limit: int = 3
    ) -> list[dict[str, Any]]:
        self._reload()
        if assertion.get("identity_status") in {"ambiguous", "unbound"}:
            return []
        proposed_slot = _graph_slot_key(assertion.get("canonical_key"))
        proposed_canonical = (
            _slot_tokens(proposed_slot) - _BROAD_SLOT_IDENTITY_TOKENS
        )
        proposed_attribute = (
            _slot_tokens(assertion.get("attribute_key"))
            - _BROAD_SLOT_IDENTITY_TOKENS
        )
        proposed_family = clean_text(
            assertion.get("memory_family") or assertion.get("memory_type")
        )
        proposed_identity = _slot_tokens(
            assertion.get("canonical_key"),
            assertion.get("entity_key"),
            assertion.get("graph_entity_key"),
            assertion.get("attribute_key"),
            assertion.get("relation"),
        )
        proposed_value = _slot_tokens(
            assertion.get("claim_text"),
            *[
                facet.get("quote")
                for facet in assertion.get("facets") or []
                if isinstance(facet, Mapping)
            ],
        )
        by_slot: dict[str, tuple[float, int, Any, dict[str, Any]]] = {}
        exact_claim_candidates: list[dict[str, Any]] = []
        for record in self.adapter.graph.records_by_id.values():
            metadata = self._source_metadata(record)
            slot = clean_text(metadata.get("canonical_slot_key"))
            if (
                metadata.get("content_variant") != "product_semantic_memory"
                or metadata.get("memory_layer") != "fast"
                or metadata.get("node_kind") != "atomic_user_assertion"
                or str(record.state) not in {"active", "parallel_active", "promoted"}
                or not slot
                or slot == proposed_slot
            ):
                continue
            if not identity_compatible(assertion, metadata, str(getattr(self, "scope_id", ""))):
                continue
            # Explicit ID/attribute matches survive extraction-key and language drift.
            # They are candidates, never an automatic replace/merge decision.
            if (assertion.get("entity_id") and assertion.get("entity_id") == metadata.get("entity_id")
                and clean_text(assertion.get("attribute_key")) == clean_text(metadata.get("attribute_key"))
                and proposed_family == clean_text(metadata.get("memory_family") or metadata.get("memory_type"))):
                item = {"memory_id": str(record.memory_id), "value": str(record.value),
                    "claim_text": str(record.value), "evidence_quote": clean_text(metadata.get("evidence_quote")) or str(record.value),
                    "canonical_slot_key": slot, "durability": metadata.get("durability"),
                    "record_state": str(record.state), "turn_index": int(record.turn_index),
                    "metadata": metadata, "candidate_score": 2_000_000.0,
                    "candidate_reason": "explicit_same_scope_entity_attribute"}
                existing = by_slot.get(slot)
                ranked = (2_000_000.0, int(record.turn_index), record, item)
                if existing is None or ranked[:2] > existing[:2]:
                    by_slot[slot] = ranked
                continue
            if _normalized_claim(str(record.value)) == _normalized_claim(
                str(assertion.get("claim_text"))
            ):
                exact_claim_candidates.append(
                    {
                        "memory_id": str(record.memory_id),
                        "value": str(record.value),
                        "claim_text": str(record.value),
                        "evidence_quote": clean_text(metadata.get("evidence_quote")) or str(record.value),
                        "canonical_slot_key": slot,
                        "durability": metadata.get("durability"),
                        "record_state": str(record.state),
                        "turn_index": int(record.turn_index),
                        "metadata": metadata,
                        "candidate_score": 1_000_000.0,
                        "candidate_reason": "exact_atomic_claim",
                    }
                )
                continue
            existing_identity = _slot_tokens(
                slot.removeprefix("memory."),
                metadata.get("entity_key"),
                metadata.get("graph_entity_key"),
                metadata.get("attribute_key"),
                record.relation,
            )
            shared_identity = proposed_identity & existing_identity
            strong_shared_identity = (
                shared_identity - _BROAD_SLOT_IDENTITY_TOKENS
            )
            existing_canonical = (
                _slot_tokens(slot) - _BROAD_SLOT_IDENTITY_TOKENS
            )
            existing_attribute = (
                _slot_tokens(metadata.get("attribute_key"))
                - _BROAD_SLOT_IDENTITY_TOKENS
            )
            canonical_overlap = proposed_canonical & existing_canonical
            attribute_overlap = proposed_attribute & existing_attribute
            same_entity = clean_text(assertion.get("graph_entity_key")) == clean_text(
                metadata.get("graph_entity_key")
            ) and bool(clean_text(assertion.get("graph_entity_key")))
            existing_family = clean_text(
                metadata.get("memory_family") or metadata.get("memory_type")
            )
            if (
                not proposed_family
                or proposed_family != existing_family
                or len(attribute_overlap) < 1
                or len(canonical_overlap) < 2
            ):
                continue
            existing_value = _slot_tokens(record.value, metadata.get("object"))
            value_overlap = len(proposed_value & existing_value)
            same_family = clean_text(assertion.get("memory_family")) == clean_text(
                metadata.get("memory_family")
            )
            score = (
                2.0 * len(shared_identity)
                + float(value_overlap)
                + (0.75 if same_entity else 0.0)
                + (0.35 if same_family else 0.0)
            )
            item = {
                "memory_id": str(record.memory_id),
                "value": str(record.value),
                "claim_text": str(record.value),
                "evidence_quote": clean_text(metadata.get("evidence_quote")) or str(record.value),
                "canonical_slot_key": slot,
                "durability": metadata.get("durability"),
                "record_state": str(record.state),
                "turn_index": int(record.turn_index),
                "metadata": metadata,
                "candidate_score": round(score, 6),
                "shared_identity_tokens": sorted(shared_identity),
                "strong_shared_identity_tokens": sorted(strong_shared_identity),
                "shared_canonical_tokens": sorted(canonical_overlap),
                "shared_attribute_tokens": sorted(attribute_overlap),
            }
            existing = by_slot.get(slot)
            ranked = (score, int(record.turn_index), record, item)
            if existing is None or ranked[:2] > existing[:2]:
                by_slot[slot] = ranked
        if exact_claim_candidates:
            return sorted(
                exact_claim_candidates,
                key=lambda item: (
                    -int(item["turn_index"]),
                    str(item["memory_id"]),
                ),
            )[: max(1, int(limit))]
        ranked_candidates = sorted(
            by_slot.values(), key=lambda item: (item[0], item[1], str(item[2].memory_id)), reverse=True
        )
        return [item[3] for item in ranked_candidates[: max(1, int(limit))]]

    def add_provenance(
        self,
        leaf_id: str,
        *,
        source_record_id: str,
        source_turn_index: int,
        provenance: Mapping[str, Any],
    ) -> None:
        with self._commit_guard():
            self._add_provenance_locked(
                leaf_id,
                source_record_id=source_record_id,
                source_turn_index=source_turn_index,
                provenance=provenance,
            )

    def _add_provenance_locked(
        self,
        leaf_id: str,
        *,
        source_record_id: str,
        source_turn_index: int,
        provenance: Mapping[str, Any],
    ) -> None:
        self._reload()
        record = self.adapter.graph.records_by_id.get(leaf_id)
        if record is None:
            raise ProductWriterError(f"real fast leaf not found for provenance: {leaf_id}")
        metadata = self._source_metadata(record)
        provenance_entry = {
            **dict(provenance),
            "source_record_id": source_record_id,
            "source_turn_index": int(source_turn_index),
        }
        values = list(metadata.get("provenance") or [])
        if provenance_entry not in values:
            values.append(provenance_entry)
        metadata["provenance"] = values
        record.metadata = metadata
        self.adapter.graph._upsert_memory_edge(
            self._edge_class(
                edge_id=f"{leaf_id}->{source_record_id}:grounded_in",
                source_memory_id=leaf_id,
                target_memory_id=source_record_id,
                edge_type="grounded_in",
                score=1.0,
                model_score=0.0,
                evidence_turn=int(source_turn_index),
                evidence=str(provenance.get("evidence_quote") or record.value),
                metadata={
                    "edge_source": "product_writer_provenance",
                    "source_record_id": source_record_id,
                    **provenance_entry,
                },
            )
        )
        self._persist()

    def repair_provenance_offsets(self) -> dict[str, Any]:
        with self._commit_guard():
            return self._repair_provenance_offsets_locked()

    def _repair_provenance_offsets_locked(self) -> dict[str, Any]:
        self._reload()
        repairs: list[dict[str, Any]] = []
        for record in self.adapter.graph.records_by_id.values():
            metadata = self._source_metadata(record)
            if metadata.get("content_variant") != "product_semantic_memory":
                continue
            provenance = list(metadata.get("provenance") or [])
            changed = False
            for index, raw_entry in enumerate(provenance):
                entry = dict(raw_entry or {})
                start = entry.get("source_char_start")
                end = entry.get("source_char_end")
                if start is not None and end is not None:
                    continue
                source_record_id = clean_text(entry.get("source_record_id"))
                source_record = self.adapter.graph.records_by_id.get(source_record_id)
                if source_record is None:
                    raise ProductWriterError(
                        f"{record.memory_id}: provenance Source record is missing: {source_record_id}"
                    )
                source_content = self._verified_source_content(source_record)
                evidence_quote = str(entry.get("evidence_quote") or "")
                evidence_span_id = clean_text(entry.get("evidence_span_id"))
                if not evidence_quote or not evidence_span_id:
                    raise ProductWriterError(
                        f"{record.memory_id}: provenance lacks an exact quote or span identity"
                    )
                repaired_start, repaired_end = _exact_provenance_offsets(
                    source_content,
                    evidence_span_id,
                    evidence_quote,
                    f"{record.memory_id}.provenance[{index}]",
                )
                entry["source_char_start"] = repaired_start
                entry["source_char_end"] = repaired_end
                provenance[index] = entry
                repairs.append(
                    {
                        "memory_id": str(record.memory_id),
                        "provenance_index": index,
                        "source_record_id": source_record_id,
                        "source_char_start": repaired_start,
                        "source_char_end": repaired_end,
                    }
                )
                changed = True
            if changed:
                metadata["provenance"] = provenance
                record.metadata = metadata
        if repairs:
            self._persist()
        return {
            "schema_version": "tmcra.v4.provenance-offset-repair.1",
            "scope_id": self.scope_id,
            "repair_count": len(repairs),
            "repairs": repairs,
        }

    @staticmethod
    def _restore_replayed_semantic_record(
        graph: Any,
        persisted: Any,
        replayed: Any,
        decision: str,
    ) -> None:
        persisted_metadata = dict(getattr(persisted, "metadata", {}) or {})
        replayed_metadata = dict(getattr(replayed, "metadata", {}) or {})
        top_level_identity = (
            ("memory_id", str),
            ("slot_key", clean_text),
            ("value", clean_text),
            ("turn_index", int),
        )
        for field, normalize in top_level_identity:
            if normalize(getattr(persisted, field)) != normalize(
                getattr(replayed, field)
            ):
                raise ProductWriterError(
                    f"{getattr(replayed, 'memory_id', '')}: replayed semantic record {field} changed"
                )
        for field in (
            "content_variant",
            "memory_layer",
            "node_kind",
            "message_id",
            "source_record_id",
            "llm_write_proposal_index",
            "canonical_slot_key",
            "event_signature",
            "evidence_quote",
            "source_span",
            "agent_id",
            "agent_name",
            "agent_role",
            "agent_specialty",
            "agent_team",
            "target_agent_id",
        ):
            if clean_text(persisted_metadata.get(field)) != clean_text(
                replayed_metadata.get(field)
            ):
                raise ProductWriterError(
                    f"{getattr(replayed, 'memory_id', '')}: replayed semantic record {field} changed"
                )
        persisted_actor_role = clean_text(
            persisted_metadata.get("actor_role")
            or persisted_metadata.get("speaker")
            or persisted_metadata.get("role")
        )
        replayed_actor_role = clean_text(
            replayed_metadata.get("actor_role")
            or replayed_metadata.get("speaker")
            or replayed_metadata.get("role")
        )
        if persisted_actor_role != replayed_actor_role:
            raise ProductWriterError(
                f"{getattr(replayed, 'memory_id', '')}: replayed semantic record actor_role changed"
            )
        merged_metadata = {**persisted_metadata, **replayed_metadata}
        provenance: list[Any] = []
        for item in [
            *list(persisted_metadata.get("provenance") or []),
            *list(replayed_metadata.get("provenance") or []),
        ]:
            if item not in provenance:
                provenance.append(item)
        if provenance:
            merged_metadata["provenance"] = provenance
        if decision in {"insert", "replace_current", "keep_parallel"}:
            merged_metadata.pop("superseded_by", None)
            merged_metadata.pop("superseded_reason", None)
            persisted.state = (
                "parallel_active" if decision == "keep_parallel" else "active"
            )
            graph.slot_heads[str(replayed.slot_key)] = str(replayed.memory_id)
        elif decision == "challenge":
            persisted.state = "challenged"
        elif decision == "quarantine":
            persisted.state = "quarantined"
        else:
            raise ProductWriterError(
                f"{getattr(replayed, 'memory_id', '')}: unsupported replay decision {decision!r}"
            )
        persisted.metadata = merged_metadata

    @staticmethod
    def _restore_auto_superseded_current(
        graph: Any,
        incoming: Any,
        current: Sequence[Mapping[str, Any]],
        *,
        decision: str,
    ) -> list[str]:
        """Undo only graph-policy supersessions caused by a non-replacing insert."""
        restored: list[str] = []
        incoming_id = clean_text(getattr(incoming, "memory_id", ""))
        incoming_slot = clean_text(getattr(incoming, "slot_key", ""))
        incoming_turn = int(getattr(incoming, "turn_index", -1))
        if not incoming_id or not incoming_slot:
            raise ProductWriterError(
                f"{decision} incoming record identity is incomplete"
            )
        for snapshot in current:
            memory_id = clean_text(snapshot.get("memory_id"))
            if not memory_id:
                raise ProductWriterError(
                    f"{incoming_id}: {decision} current record lacks memory_id"
                )
            record = graph.records_by_id.get(memory_id)
            if record is None:
                raise ProductWriterError(
                    f"{incoming_id}: {decision} current record disappeared: {memory_id}"
                )
            metadata = dict(getattr(record, "metadata", {}) or {})
            if not (
                clean_text(getattr(record, "state", "")) == "superseded"
                and clean_text(metadata.get("superseded_by")) == incoming_id
            ):
                continue
            reason = clean_text(metadata.get("superseded_reason"))
            prior_state = clean_text(snapshot.get("record_state"))
            if reason not in GRAPH_AUTO_SUPERSESSION_REASONS:
                raise ProductWriterError(
                    f"{incoming_id}: {decision} would erase {memory_id} for unsupported reason {reason!r}"
                )
            if (
                clean_text(getattr(record, "slot_key", "")) != incoming_slot
                or int(getattr(record, "turn_index", -1)) > incoming_turn
                or prior_state not in {"active", "parallel_active", "promoted"}
            ):
                raise ProductWriterError(
                    f"{incoming_id}: {decision} supersession lifecycle is inconsistent for {memory_id}"
                )
            record.state = prior_state
            metadata.pop("superseded_by", None)
            metadata.pop("superseded_reason", None)
            record.metadata = metadata
            restored.append(memory_id)
        if restored:
            restored_ids = set(restored)
            incoming.supersedes = [
                memory_id
                for memory_id in list(getattr(incoming, "supersedes", []) or [])
                if clean_text(memory_id) not in restored_ids
            ]
        return restored

    @staticmethod
    def _honor_keep_parallel_decision(
        graph: Any,
        incoming: Any,
        current: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        restored = RealGraphBackend._restore_auto_superseded_current(
            graph,
            incoming,
            current,
            decision="keep_parallel",
        )
        if restored:
            incoming_metadata = dict(getattr(incoming, "metadata", {}) or {})
            incoming_metadata["conflict_action"] = "keep_parallel"
            incoming_metadata["conflict_reason"] = "v4_reconciliation_keep_parallel"
            incoming.metadata = incoming_metadata
        return restored

    @staticmethod
    def _honor_challenge_decision(
        graph: Any,
        incoming: Any,
        current: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        restored = RealGraphBackend._restore_auto_superseded_current(
            graph,
            incoming,
            current,
            decision="challenge",
        )
        if restored:
            incoming_metadata = dict(getattr(incoming, "metadata", {}) or {})
            incoming_metadata["conflict_action"] = "challenge"
            incoming_metadata["conflict_reason"] = "v4_reconciliation_challenge"
            incoming.metadata = incoming_metadata
        return restored

    @staticmethod
    def _remove_empty_graph_benchmark_metadata(record: Any) -> list[str]:
        metadata = dict(getattr(record, "metadata", {}) or {})
        removed: list[str] = []
        for key in sorted(GRAPH_INJECTED_BENCHMARK_METADATA_KEYS.intersection(metadata)):
            value = metadata[key]
            if value not in (None, "", [], {}, False):
                raise ProductWriterError(
                    f"{getattr(record, 'memory_id', '')}: graph injected non-empty benchmark metadata {key}"
                )
            del metadata[key]
            removed.append(key)
        if removed:
            record.metadata = metadata
        return removed

    def commit_message(
        self,
        *,
        message: SourceMessage,
        source_record_id: str,
        source_turn_index: int,
        extraction: Mapping[str, Any],
        durabilities: Sequence[str],
        decisions: Mapping[int, str],
        current_by_index: Mapping[int, Sequence[Mapping[str, Any]]],
        duplicate_provenance: Sequence[Mapping[str, Any]] = (),
        transaction_hook: Callable[[sqlite3.Connection, int], None] | None = None,
    ) -> int:
        with self._commit_guard():
            return self._commit_message_locked(
                message=message,
                source_record_id=source_record_id,
                source_turn_index=source_turn_index,
                extraction=extraction,
                durabilities=durabilities,
                decisions=decisions,
                current_by_index=current_by_index,
                duplicate_provenance=duplicate_provenance,
                transaction_hook=transaction_hook,
            )

    def _commit_message_locked(
        self,
        *,
        message: SourceMessage,
        source_record_id: str,
        source_turn_index: int,
        extraction: Mapping[str, Any],
        durabilities: Sequence[str],
        decisions: Mapping[int, str],
        current_by_index: Mapping[int, Sequence[Mapping[str, Any]]],
        duplicate_provenance: Sequence[Mapping[str, Any]],
        transaction_hook: Callable[[sqlite3.Connection, int], None] | None,
    ) -> int:
        self._reload()
        self._validate_frozen_commit_context(
            extraction=extraction,
            current_by_index=current_by_index,
            duplicate_provenance=duplicate_provenance,
        )
        actor_provenance = dict(message.actor_metadata)
        source_record = self.adapter.graph.records_by_id.get(source_record_id)
        if source_record is None or int(source_record.turn_index) != int(source_turn_index):
            raise ProductWriterError(
                f"{message.message_id}: real source record/turn is missing before enrichment"
            )
        source_metadata = self._source_metadata(source_record)
        source_metadata["enrichment_status"] = "enriched"
        source_metadata.pop("enrichment_error", None)
        source_record.metadata = source_metadata
        for item in duplicate_provenance:
            leaf_id = clean_text(item.get("leaf_id"))
            leaf = self.adapter.graph.records_by_id.get(leaf_id)
            if leaf is None:
                raise ProductWriterError(
                    f"real fast leaf not found for provenance: {leaf_id}"
                )
            metadata = self._source_metadata(leaf)
            provenance_entry = {
                **dict(item.get("provenance") or {}),
                "source_record_id": source_record_id,
                "source_turn_index": int(source_turn_index),
                **actor_provenance,
            }
            values = list(metadata.get("provenance") or [])
            if provenance_entry not in values:
                values.append(provenance_entry)
            metadata["provenance"] = values
            leaf.metadata = metadata
            self.adapter.graph._upsert_memory_edge(
                self._edge_class(
                    edge_id=f"{leaf_id}->{source_record_id}:grounded_in",
                    source_memory_id=leaf_id,
                    target_memory_id=source_record_id,
                    edge_type="grounded_in",
                    score=1.0,
                    model_score=0.0,
                    evidence_turn=int(source_turn_index),
                    evidence=str(
                        provenance_entry.get("evidence_quote") or leaf.value
                    ),
                    metadata={
                        "edge_source": "product_writer_provenance",
                        **provenance_entry,
                    },
                )
            )
        records, _ = build_graph_records(
            self._record_class,
            scope_id=self.scope_id,
            turn_index=source_turn_index,
            session_id=message.session_id,
            session_index=message.session_index,
            message_id=message.message_id,
            message_index=message.message_index,
            date=message.timestamp[:10],
            timestamp=message.timestamp,
            role=message.role,
            content=message.content,
            extraction=extraction,
            actor_metadata=message.actor_metadata,
        )
        semantic_records = [
            record
            for record in records
            if self._source_metadata(record).get("content_variant") != "source_message"
        ]
        assertion_by_index = {
            int(self._source_metadata(record).get("llm_write_proposal_index", -1)): record
            for record in semantic_records
            if self._source_metadata(record).get("content_variant") == "product_semantic_memory"
        }
        decision_by_event_signature: dict[str, str] = {}
        desired_state_by_id: dict[str, str] = {}
        for record in semantic_records:
            metadata = self._source_metadata(record)
            metadata["source_record_id"] = source_record_id
            metadata["enrichment_status"] = "enriched"
            metadata["writer_schema_version"] = BATCH_SCHEMA_VERSION
            metadata["prompt_version"] = PROMPT_VERSION
            metadata["source"] = "tmcra_v4_batch_writer"
            if metadata.get("content_variant") == "product_semantic_memory":
                assertion_index = int(metadata.get("llm_write_proposal_index", -1))
                if not 0 <= assertion_index < len(durabilities):
                    raise ProductWriterError(
                        f"{message.message_id}: assertion durability index is invalid"
                    )
                metadata["durability"] = durabilities[assertion_index]
                decision = decisions.get(assertion_index, "insert")
                metadata["reconciliation_decision"] = decision
                event_signature = clean_text(metadata.get("event_signature"))
                if event_signature:
                    decision_by_event_signature[event_signature] = decision
                if decision in {"keep_parallel", "challenge", "quarantine"}:
                    metadata["write_operation"] = "append"
                    metadata["allow_parallel_state"] = True
                if decision == "keep_parallel":
                    desired_state_by_id[record.memory_id] = "parallel_active"
                    metadata["conflict_action"] = "keep_parallel"
                elif decision == "challenge":
                    desired_state_by_id[record.memory_id] = "challenged"
                    metadata["conflict_action"] = "challenge"
                elif decision == "quarantine":
                    desired_state_by_id[record.memory_id] = "quarantined"
                    metadata["conflict_action"] = "quarantine"
                    metadata["excluded_from_retrieval"] = True
                elif decision == "replace_current":
                    metadata["conflict_action"] = "replace_current"
            if metadata.get("content_variant") == "product_interaction":
                interaction_index = int(str(metadata.get("interaction_id", "").rsplit(".", 1)[-1]).split(":", 1)[0] or 0)
                interaction_id = _deterministic_interaction_id(self.scope_id, message.message_id, interaction_index)
                record.memory_id = interaction_id
                metadata["interaction_id"] = interaction_id
            record.metadata = metadata

        new_records = []
        for record in semantic_records:
            metadata = self._source_metadata(record)
            if record.memory_id in self.adapter.graph.records_by_id:
                continue
            if metadata.get("content_variant") == "event_facet_write":
                parent_signature = clean_text(metadata.get("facet_parent_event_signature"))
                if decision_by_event_signature.get(parent_signature) == "quarantine":
                    continue
            new_records.append(record)
        stored_ids = self.adapter.graph.add_records(new_records)
        for assertion_index, record in assertion_by_index.items():
            persisted = self.adapter.graph.records_by_id.get(record.memory_id)
            if persisted is None:
                continue
            decision = decisions.get(assertion_index, "insert")
            self._restore_replayed_semantic_record(
                self.adapter.graph,
                persisted,
                record,
                decision,
            )
            desired_state = desired_state_by_id.get(record.memory_id)
            if desired_state:
                persisted.state = desired_state
            if decision == "keep_parallel":
                self._honor_keep_parallel_decision(
                    self.adapter.graph,
                    persisted,
                    current_by_index.get(assertion_index, []),
                )
            elif decision == "challenge":
                self._honor_challenge_decision(
                    self.adapter.graph,
                    persisted,
                    current_by_index.get(assertion_index, []),
                )
            if decision in {"challenge", "quarantine"}:
                slot_key = clean_text(self._source_metadata(persisted).get("canonical_slot_key"))
                if self.adapter.graph.slot_heads.get(slot_key) == persisted.memory_id:
                    replacement = next(
                        (
                            str(item["memory_id"])
                            for item in current_by_index.get(assertion_index, [])
                            if str(item.get("record_state"))
                            in {"active", "parallel_active", "promoted"}
                        ),
                        "",
                    )
                    if replacement:
                        self.adapter.graph.slot_heads[slot_key] = replacement
                    else:
                        self.adapter.graph.slot_heads.pop(slot_key, None)
        for assertion_index, decision in decisions.items():
            if decision != "replace_current":
                continue
            incoming = assertion_by_index.get(assertion_index)
            if incoming is None:
                raise ProductWriterError(
                    f"{message.message_id}: replacement assertion record is missing"
                )
            persisted_incoming = self.adapter.graph.records_by_id.get(
                incoming.memory_id
            )
            if persisted_incoming is None:
                raise ProductWriterError(
                    f"{message.message_id}: replacement assertion was not persisted"
                )
            incoming_metadata = self._source_metadata(persisted_incoming)
            for current in current_by_index.get(assertion_index, []):
                current_id = clean_text(current.get("memory_id"))
                if not current_id or current_id == incoming.memory_id:
                    continue
                old = self.adapter.graph.records_by_id.get(current_id)
                if old is None:
                    raise ProductWriterError(
                        f"{incoming.memory_id}: replacement target disappeared: {current_id}"
                    )
                old_metadata = self._source_metadata(old)
                existing_link = clean_text(old_metadata.get("superseded_by"))
                existing_reason = clean_text(
                    old_metadata.get("superseded_reason")
                )
                if str(old.state) == "superseded":
                    allowed_reasons = {
                        "v4_reconciliation_replace_current",
                        *GRAPH_AUTO_SUPERSESSION_REASONS,
                    }
                    if (
                        existing_reason not in allowed_reasons
                        or existing_link not in {"", incoming.memory_id}
                    ):
                        raise ProductWriterError(
                            f"{incoming.memory_id}: replacement target has an incompatible supersession lifecycle"
                        )
                if str(old.state) not in {
                    "active", "parallel_active", "promoted", "superseded"
                }:
                    raise ProductWriterError(
                        f"{incoming.memory_id}: replacement target state is unsupported: {old.state!r}"
                    )
                event = correction_entry(previous=old, incoming=persisted_incoming,
                    scope_id=self.scope_id, observed_at=message.timestamp)
                history = list(incoming_metadata.get("correction_history") or [])
                if not any(item.get("transition_id") == event["transition_id"] for item in history):
                    history.append(event)
                incoming_metadata["correction_history"] = history
                old_metadata["superseded_effective_at"] = event["effective_at"]
                old_metadata["superseded_recorded_at"] = event["recorded_at"]
                old.state = "superseded"
                old_metadata["superseded_by"] = incoming.memory_id
                old_metadata["superseded_reason"] = (
                    "v4_reconciliation_replace_current"
                )
                old.metadata = old_metadata
                supersedes = list(
                    getattr(persisted_incoming, "supersedes", []) or []
                )
                if current_id not in supersedes:
                    supersedes.append(current_id)
                persisted_incoming.supersedes = supersedes
            persisted_incoming.state = "active"
            incoming_metadata.pop("superseded_by", None)
            incoming_metadata.pop("superseded_reason", None)
            persisted_incoming.metadata = incoming_metadata
            self.adapter.graph.slot_heads[str(persisted_incoming.slot_key)] = (
                persisted_incoming.memory_id
            )
        for memory_id in stored_ids:
            stored = self.adapter.graph.records_by_id.get(str(memory_id))
            if stored is not None:
                self._remove_empty_graph_benchmark_metadata(stored)
        for record in semantic_records:
            if record.memory_id not in self.adapter.graph.records_by_id:
                continue
            metadata = self._source_metadata(record)
            if metadata.get("content_variant") not in {"product_semantic_memory", "product_interaction"}:
                continue
            self.adapter.graph._upsert_memory_edge(
                self._edge_class(
                    edge_id=f"{record.memory_id}->{source_record_id}:grounded_in",
                    source_memory_id=record.memory_id,
                    target_memory_id=source_record_id,
                    edge_type="grounded_in",
                    score=1.0,
                    model_score=0.0,
                    evidence_turn=source_turn_index,
                    evidence=str(metadata.get("source_span") or record.value),
                    metadata={
                        "edge_source": "product_writer_provenance",
                        "message_id": message.message_id,
                        "source_record_id": source_record_id,
                        **actor_provenance,
                    },
                )
            )
        for assertion_index, decision in decisions.items():
            if decision not in {"challenge", "quarantine"}:
                continue
            candidate = assertion_by_index.get(assertion_index)
            if candidate is None or candidate.memory_id not in self.adapter.graph.records_by_id:
                continue
            for current in current_by_index.get(assertion_index, []):
                current_id = str(current["memory_id"])
                edge_type = "contradicts" if decision == "challenge" else "quarantined_against"
                self.adapter.graph._upsert_memory_edge(
                    self._edge_class(
                        edge_id=f"{candidate.memory_id}->{current_id}:{edge_type}",
                        source_memory_id=candidate.memory_id,
                        target_memory_id=current_id,
                        edge_type=edge_type,
                        score=1.0,
                        model_score=0.0,
                        evidence_turn=source_turn_index,
                        evidence=str(candidate.value),
                        metadata={
                            "edge_source": "v4_reconciliation",
                            "decision": decision,
                            "canonical_slot_key": candidate.metadata.get("canonical_slot_key"),
                            **actor_provenance,
                        },
                    )
                )
        for resolution in list(extraction.get("resolutions") or []):
            target_id = str(resolution["interaction_id"])
            target = self.adapter.graph.records_by_id.get(target_id)
            if target is None:
                raise ProductWriterError(f"{message.message_id}: resolution target does not exist: {target_id}")
            target_meta = self._source_metadata(target)
            previous_status = clean_text(target_meta.get("interaction_status")) or "open"
            next_status = "resolved" if resolution["resolution"] == "resolved" else ("partial" if resolution["resolution"] == "partial" else previous_status)
            target_meta["interaction_status"] = next_status
            target_meta.setdefault("resolution_history", []).append({"message_id": message.message_id, "source_record_id": source_record_id, "resolution": resolution["resolution"], "evidence_quote": resolution["evidence_quote"], **actor_provenance})
            target.metadata = target_meta
            edge_type = {"resolved": "answered_by", "partial": "partially_answered_by", "unresolved": "responded_without_resolution"}[resolution["resolution"]]
            self.adapter.graph._upsert_memory_edge(self._edge_class(edge_id=f"{target_id}->{source_record_id}:{edge_type}", source_memory_id=target_id, target_memory_id=source_record_id, edge_type=edge_type, score=1.0 if edge_type == "answered_by" else 0.72, model_score=0.0, evidence_turn=source_turn_index, evidence=str(resolution["evidence_quote"]), metadata={"edge_source": "product_writer_resolution", "message_id": message.message_id, "resolution": resolution["resolution"], **actor_provenance}))

        event_ids = [source_record_id, *stored_ids]
        for event in self.adapter.graph.turn_log:
            if int(event.get("turn_index", -1)) == int(source_turn_index):
                event["record_ids"] = list(dict.fromkeys([*event.get("record_ids", []), *event_ids]))
                event.setdefault("metadata", {})["enrichment_status"] = "enriched"
                break
        committed_count = sum(
            1
            for assertion_index, record in assertion_by_index.items()
            if decisions.get(assertion_index) != "quarantine"
            and record.memory_id in self.adapter.graph.records_by_id
        )
        self._persist(
            None
            if transaction_hook is None
            else lambda connection: transaction_hook(connection, committed_count)
        )
        return committed_count


class RealGraphFactory:
    def __init__(
        self,
        *,
        repo: Path,
        database: Path,
        retrieval_mode: str = "heuristic",
        node_model_path: str = "",
        path_model_path: str = "",
        node_model_device: str = "",
        graph_environment: Mapping[str, Any] | None = None,
    ) -> None:
        self.repo = Path(repo)
        self.database = Path(database)
        self.retrieval_mode = str(retrieval_mode or "heuristic")
        self.node_model_path = str(node_model_path or "")
        self.path_model_path = str(path_model_path or "")
        self.node_model_device = str(node_model_device or "")
        self.graph_environment = dict(graph_environment or {})
        self.backends: dict[str, RealGraphBackend] = {}
        self._backends_lock = threading.RLock()

    def for_scope(self, scope_id: str) -> RealGraphBackend:
        with self._backends_lock:
            if scope_id not in self.backends:
                self.backends[scope_id] = RealGraphBackend(
                    repo=self.repo,
                    database=self.database,
                    scope_id=scope_id,
                    retrieval_mode=self.retrieval_mode,
                    node_model_path=self.node_model_path,
                    path_model_path=self.path_model_path,
                    node_model_device=self.node_model_device,
                    graph_environment=self.graph_environment,
                )
            return self.backends[scope_id]


def _client_result(result: Any) -> tuple[Mapping[str, Any] | str, dict[str, Any]]:
    if isinstance(result, tuple) and len(result) == 2:
        return result[0], dict(result[1] or {})
    return result, {}


def _configured_client_model(client: Any, fallback: str) -> str:
    return clean_text(getattr(client, "model", "")) or fallback


def _normalized_evidence(value: str) -> str:
    return clean_text(unicodedata.normalize("NFKC", value))


def _normalized_claim(value: str) -> str:
    return clean_text(unicodedata.normalize("NFKC", value)).casefold()


def build_graph_records(record_class: Any, **kwargs: Any) -> tuple[list[Any], dict[str, int]]:
    """Build V3-compatible records while keeping claims separate from evidence."""
    extraction = kwargs.get("extraction")
    records, counts = _build_v3_graph_records(record_class, **kwargs)
    assertions = list((extraction or {}).get("assertions") or [])
    for record in records:
        metadata = dict(getattr(record, "metadata", {}) or {})
        if metadata.get("content_variant") != "product_semantic_memory":
            continue
        assertion_index = int(metadata.get("llm_write_proposal_index", -1))
        if not 0 <= assertion_index < len(assertions):
            raise ProductWriterError("semantic record cannot be mapped to its assertion")
        assertion = assertions[assertion_index]
        claim_text = clean_text(assertion.get("claim_text"))
        raw_evidence_quote = assertion.get("evidence_quote")
        evidence_quote = (
            raw_evidence_quote if isinstance(raw_evidence_quote, str) else ""
        )
        if not claim_text or not evidence_quote:
            raise ProductWriterError(
                "V4 semantic records require claim_text and exact evidence_quote"
            )
        source_turn_text = metadata.get("source_turn_text")
        try:
            evidence_start = int(metadata["evidence_char_start"])
            evidence_end = int(metadata["evidence_char_end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProductWriterError(
                "V4 semantic record lacks exact evidence offsets"
            ) from exc
        if (
            not isinstance(source_turn_text, str)
            or evidence_start < 0
            or evidence_end <= evidence_start
            or evidence_end > len(source_turn_text)
            or source_turn_text[evidence_start:evidence_end] != evidence_quote
            or metadata.get("raw_content") != evidence_quote
            or metadata.get("source_span") != evidence_quote
        ):
            raise ProductWriterError(
                "V4 semantic evidence quote differs from its exact Source slice"
            )
        record.value = claim_text
        metadata["claim_text"] = claim_text
        metadata["evidence_quote"] = evidence_quote
        metadata["semantic_value_kind"] = "atomic_claim"
        for key in IDENTITY_FIELDS:
            if key in assertion:
                metadata[key] = assertion[key]
        record.metadata = metadata
    return records, counts


def _binding_identity(value: Mapping[str, Any]) -> dict[str, str]:
    metadata = dict(value.get("metadata") or {})
    return {
        **{key: _json(value.get(key, metadata.get(key))) for key in IDENTITY_FIELDS
           if key in value or key in metadata},
        "memory_id": clean_text(value.get("memory_id")),
        "claim_text": _normalized_claim(
            clean_text(value.get("claim_text") or value.get("value"))
        ),
        "canonical_slot_key": clean_text(
            value.get("canonical_slot_key") or metadata.get("canonical_slot_key")
        ),
        "evidence_quote": _normalized_evidence(
            clean_text(value.get("evidence_quote") or value.get("value"))
        ),
        "durability": clean_text(
            value.get("durability") or metadata.get("durability")
        ),
        "source_record_id": clean_text(
            value.get("source_record_id") or metadata.get("source_record_id")
        ),
        "entity_key": clean_text(
            value.get("entity_key") or metadata.get("entity_key")
        ),
        "graph_entity_key": clean_text(
            value.get("graph_entity_key") or metadata.get("graph_entity_key")
        ),
        "attribute_key": clean_text(
            value.get("attribute_key") or metadata.get("attribute_key")
        ),
        "memory_type": clean_text(
            value.get("memory_type") or metadata.get("memory_type")
        ),
        "memory_family": clean_text(
            value.get("memory_family") or metadata.get("memory_family")
        ),
        "temporal_status": clean_text(
            value.get("temporal_status") or metadata.get("target_status")
        ),
        "polarity": clean_text(value.get("polarity") or metadata.get("polarity")),
    }


def _binding_semantic_identity(value: Mapping[str, Any]) -> dict[str, str]:
    identity = _binding_identity(value)
    identity.pop("memory_id")
    return identity


def _reconciliation_job_id(
    batch: SourceBatch,
    message: SourceMessage,
    assertion_index: int,
    assertion: Mapping[str, Any],
) -> str:
    return sha256_text(
        _json(
            {
                "batch_id": batch.batch_id,
                "message_id": message.message_id,
                "assertion_index": assertion_index,
                "slot": _graph_slot_key(assertion["canonical_key"]),
                "evidence": assertion["evidence_quote"],
            }
        )
    )[:32]


class V4BatchWriter:
    def __init__(
        self,
        *,
        store: V4BatchStore,
        flash_client: BatchClient,
        pro_client: ReconciliationClient | None = None,
        graph_factory: RealGraphFactory | None = None,
        log_dir: Path | None = None,
        revalidate_failed_raw_response: bool = False,
        recover_interrupted_api_calls: bool = False,
    ) -> None:
        self.store = store
        self.flash_client = flash_client
        self.pro_client = pro_client
        self.graph_factory = graph_factory
        self.log_dir = Path(log_dir) if log_dir is not None else None
        if self.log_dir is not None:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        self.revalidate_failed_raw_response = bool(revalidate_failed_raw_response)
        self.recover_interrupted_api_calls = bool(recover_interrupted_api_calls)
        self.stats = {
            "batches": 0,
            "resumed_batches": 0,
            "flash_calls": 0,
            "pro_calls": 0,
            "input_messages": 0,
            "source_messages": 0,
            "excluded_empty_source_messages": 0,
            "fast_assertion_leaves": 0,
            "reconciliation_jobs": 0,
            "reconciliation_response_quarantines": 0,
            "validation_warnings": 0,
            "interrupted_call_recoveries": 0,
            "billing_call_recoveries": 0,
            "validated_batch_recoveries": 0,
            "historical_binding_recoveries": 0,
            "committed_source_status_repairs": 0,
            "stale_graph_snapshot_retries": 0,
        }

    def _append_unique_jsonl(self, filename: str, key: str, value: Mapping[str, Any]) -> None:
        if self.log_dir is None:
            return
        path = self.log_dir / filename
        identity = clean_text(value.get(key))
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    if clean_text(json.loads(line).get(key)) == identity:
                        return
                except json.JSONDecodeError:
                    continue
        with path.open("a", encoding="utf-8") as handle:
            handle.write(_json(value) + "\n")

    def _artifact_count(self, filename: str, call_key: str) -> int:
        if self.log_dir is None:
            raise ProductWriterError(
                "interrupted call recovery requires a durable log directory"
            )
        path = self.log_dir / filename
        if not path.exists():
            return 0
        count = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            count += int(clean_text(value.get("call_key")) == call_key)
        return count

    def _record_interrupted_call(
        self,
        *,
        call_key: str,
        batch: SourceBatch,
        stage: str,
        model: str,
        job_id: str = "",
    ) -> None:
        physical_call_id = "interrupted:" + sha256_text(call_key)[:32]
        self._append_unique_jsonl(
            "product_writer_interrupted_calls.jsonl",
            "call_key",
            {
                "call_key": call_key,
                "batch_id": batch.batch_id,
                "scope_id": batch.scope_id,
                "session_id": batch.session_id,
                "job_id": job_id,
                "stage": stage,
                "model": model,
                "status": "outcome_unknown_after_confirmed_process_loss",
                "physical_api_call": True,
                "physical_api_calls": 1,
                "physical_call_id": physical_call_id,
                "usage_recorded": False,
                "replacement_call_authorized": True,
                "replacement_model": model,
                "same_model_replacement": True,
                "recovered_at": _now(),
            },
        )
        self.stats["interrupted_call_recoveries"] += 1

    def _assert_interrupted_call_has_no_response(self, call_key: str) -> None:
        raw_count = self._artifact_count(
            "product_writer_raw_responses.jsonl", call_key
        )
        call_count = self._artifact_count("product_writer_calls.jsonl", call_key)
        if raw_count or call_count:
            raise ProductWriterError(
                f"{call_key}: interrupted call has durable response/call artifacts; refusing replacement"
            )

    def _record_api_call(
        self,
        *,
        call_key: str,
        batch: SourceBatch,
        stage: str,
        model: str,
        metadata: Mapping[str, Any],
        job_id: str = "",
        error: str = "",
    ) -> None:
        if not metadata:
            return
        physical_call_id = clean_text(metadata.get("physical_call_id"))
        response_sha256 = clean_text(metadata.get("response_sha256"))
        artifact_id = sha256_text(
            "\0".join((call_key, physical_call_id, response_sha256, error))
        )
        self._append_unique_jsonl(
            "product_writer_calls.jsonl",
            "artifact_id",
            {
                "artifact_id": artifact_id,
                "call_key": call_key,
                "batch_id": batch.batch_id,
                "scope_id": batch.scope_id,
                "session_id": batch.session_id,
                "job_id": job_id,
                "model": model,
                "stage": stage,
                "api_call_count": 1,
                "physical_api_call_count": 1,
                "metadata": dict(metadata),
                "error": error,
            },
        )

    def _record_raw_api_response(
        self,
        *,
        call_key: str,
        batch: SourceBatch,
        stage: str,
        model: str,
        response: Any,
        metadata: Mapping[str, Any],
        job_id: str = "",
    ) -> None:
        raw_response = response if isinstance(response, str) else _json(response)
        raw_response_sha256 = sha256_text(raw_response)
        physical_call_id = clean_text(metadata.get("physical_call_id"))
        artifact_id = sha256_text(
            "\0".join((call_key, physical_call_id, raw_response_sha256))
        )
        self._append_unique_jsonl(
            "product_writer_raw_responses.jsonl",
            "artifact_id",
            {
                "artifact_id": artifact_id,
                "call_key": call_key,
                "batch_id": batch.batch_id,
                "scope_id": batch.scope_id,
                "session_id": batch.session_id,
                "job_id": job_id,
                "stage": stage,
                "model": clean_text(metadata.get("model")) or model,
                "physical_call_id": physical_call_id,
                "request_sha256": clean_text(metadata.get("request_sha256")),
                "raw_response": raw_response,
                "raw_response_sha256": raw_response_sha256,
                "metadata_response_sha256": clean_text(
                    metadata.get("response_sha256")
                ),
            },
        )

    def _raw_api_response(
        self,
        call_key: str,
        *,
        expected_response_sha256: str = "",
        expected_physical_call_id: str = "",
    ) -> tuple[str, dict[str, Any]]:
        if self.log_dir is None:
            raise ProductWriterError("raw response revalidation requires a log directory")
        path = self.log_dir / "product_writer_raw_responses.jsonl"
        if not path.is_file():
            raise ProductWriterError("raw response revalidation artifact is missing")
        matches: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if clean_text(value.get("call_key")) == call_key:
                matches.append(value)
        if expected_response_sha256:
            matches = [
                value
                for value in matches
                if clean_text(value.get("raw_response_sha256"))
                == expected_response_sha256
                and clean_text(value.get("metadata_response_sha256"))
                == expected_response_sha256
            ]
        if expected_physical_call_id:
            exact = [
                value
                for value in matches
                if clean_text(value.get("physical_call_id"))
                == expected_physical_call_id
            ]
            if exact:
                matches = exact
            elif len(matches) != 1:
                matches = []
        if len(matches) != 1:
            raise ProductWriterError(
                f"{call_key}: expected exactly one raw response, found {len(matches)}"
            )
        record = matches[0]
        raw_response = record.get("raw_response")
        if not isinstance(raw_response, str) or not raw_response:
            raise ProductWriterError(f"{call_key}: raw response is empty")
        raw_hash = sha256_text(raw_response)
        if raw_hash != clean_text(record.get("raw_response_sha256")):
            raise ProductWriterError(f"{call_key}: raw response hash differs")
        metadata_hash = clean_text(record.get("metadata_response_sha256"))
        if not metadata_hash or metadata_hash != raw_hash:
            raise ProductWriterError(
                f"{call_key}: raw response does not match durable API metadata"
            )
        return raw_response, record

    def _revalidate_failed_reconciliation_batch(
        self,
        batch: SourceBatch,
        row: Mapping[str, Any],
    ) -> sqlite3.Row:
        jobs = self.store.reconciliation_jobs_for_batch(batch.batch_id)
        if not jobs:
            try:
                response = json.loads(str(row.get("response_json") or ""))
            except json.JSONDecodeError as exc:
                raise ProductWriterError(
                    f"{batch.batch_id}: failed validated batch response is unreadable"
                ) from exc
            response_messages = response.get("messages") if isinstance(response, Mapping) else None
            if not isinstance(response_messages, list):
                raise ProductWriterError(
                    f"{batch.batch_id}: failed validated batch response has no message list"
                )
            expected = {
                clean_text(message.get("message_id")): _hash_json(message)
                for message in response_messages
                if isinstance(message, Mapping)
                and clean_text(message.get("message_id"))
            }
            commits = self.store.message_commit_rows_for_batch(batch.batch_id)
            actual = {clean_text(commit["message_id"]): commit for commit in commits}
            frozen = bool(
                len(expected) == len(response_messages)
                and set(actual) == set(expected)
                and all(
                    clean_text(actual[message_id]["status"])
                    in {"prepared", "committed"}
                    and clean_text(actual[message_id]["response_sha256"])
                    == response_sha256
                    and clean_text(actual[message_id]["plan_json"])
                    and clean_text(actual[message_id]["plan_sha256"])
                    == sha256_text(str(actual[message_id]["plan_json"]))
                    for message_id, response_sha256 in expected.items()
                )
            )
            if response_messages and not frozen:
                raise ProductWriterError(
                    f"{batch.batch_id}: failed validated batch lacks complete frozen message plans"
                )
            batch_metadata = json.loads(
                str(row.get("response_metadata_json") or "{}")
            )
            batch_metadata.update(
                {
                    "validated_batch_commit_recovered": True,
                    "revalidated_at": _now(),
                    "revalidated_reconciliation_job_ids": [],
                    "interrupted_reconciliation_job_ids": [],
                    "pending_reconciliation_job_ids": [],
                    "frozen_message_plan_recovery": bool(response_messages),
                    "physical_api_calls_revalidation": 0,
                }
            )
            recovery_artifact = {
                "schema_version": "tmcra.v4.validated-batch-recovery.1",
                "batch_id": batch.batch_id,
                "scope_id": batch.scope_id,
                "session_id": batch.session_id,
                "response_sha256": clean_text(row.get("response_sha256")),
                "prior_error_sha256": sha256_text(clean_text(row.get("error"))),
                "completed_reconciliation_job_ids": [],
                "revalidated_reconciliation_job_ids": [],
                "interrupted_reconciliation_job_ids": [],
                "pending_reconciliation_job_ids": [],
                "frozen_message_plan_recovery": bool(response_messages),
                "physical_api_calls": 0,
                "recovered_at": _now(),
            }
            self._append_unique_jsonl(
                "product_writer_validated_batch_recoveries.jsonl",
                "batch_id",
                recovery_artifact,
            )
            self.stats["validated_batch_recoveries"] += 1
            return self.store.resume_failed_validated_batch(
                batch.batch_id,
                batch_metadata,
            )
        failed_jobs = [job for job in jobs if clean_text(job["status"]) == "failed"]
        invalid = [
            (clean_text(job["job_id"]), clean_text(job["status"]))
            for job in jobs
            if clean_text(job["status"])
            not in {"completed", "failed", "pro_pending", "pro_started"}
        ]
        if invalid:
            raise ProductWriterError(
                f"{batch.batch_id}: reconciliation jobs have unsupported recovery states: {invalid}"
            )
        recovered_job_ids: list[str] = []
        for job in failed_jobs:
            job_id = clean_text(job["job_id"])
            metadata = json.loads(str(job["response_metadata_json"] or "{}"))
            if (
                clean_text(metadata.get("status")) != "completed"
                or metadata.get("physical_api_call") is not True
                or int(metadata.get("http_status") or 0) != 200
            ):
                raise ProductWriterError(
                    f"{job_id}: failed reconciliation lacks one clean completed API response"
                )
            raw_response, raw_record = self._raw_api_response(
                f"pro:{job_id}",
                expected_response_sha256=clean_text(metadata.get("response_sha256")),
                expected_physical_call_id=clean_text(metadata.get("physical_call_id")),
            )
            parsed = _strict_json_object(
                raw_response, f"revalidation[reconciliation:{job_id}]"
            )
            request = json.loads(str(job["request_json"]))
            candidates = request.get("candidate_cited_leaves")
            exact_slot_match = request.get("exact_slot_match")
            if not isinstance(candidates, list) or type(exact_slot_match) is not bool:
                raise ProductWriterError(
                    f"{job_id}: frozen reconciliation request is malformed"
                )
            adjudication = self._validate_reconciliation_response(
                parsed,
                current_cited=candidates,
                exact_slot_match=exact_slot_match,
                path=f"revalidation[reconciliation:{job_id}]",
            )
            recovery_metadata = {
                **metadata,
                "raw_response_revalidated": True,
                "revalidated_at": _now(),
                "revalidation_raw_response_sha256": raw_record[
                    "raw_response_sha256"
                ],
                "prior_error_sha256": sha256_text(clean_text(job["error"])),
                "model_adjudication_sha256": _hash_json(parsed),
                "normalized_adjudication_sha256": _hash_json(adjudication),
                "physical_api_calls_revalidation": 0,
            }
            if parsed != adjudication:
                recovery_metadata["controller_normalization"] = (
                    "slot_decision_from_selected_candidate_and_conflict_action"
                )
            self.store.revalidate_failed_reconciliation_job(
                job_id,
                adjudication["decision"],
                adjudication,
                recovery_metadata,
            )
            self._append_unique_jsonl(
                "product_writer_reconciliation_revalidations.jsonl",
                "job_id",
                {
                    "job_id": job_id,
                    "batch_id": batch.batch_id,
                    "raw_response_sha256": raw_record["raw_response_sha256"],
                    "normalized_adjudication_sha256": _hash_json(adjudication),
                    "controller_normalization": recovery_metadata.get(
                        "controller_normalization", "none"
                    ),
                    "physical_api_calls": 0,
                },
            )
            recovered_job_ids.append(job_id)
        interrupted_job_ids: list[str] = []
        for job in jobs:
            if clean_text(job["status"]) != "pro_started":
                continue
            job_id = clean_text(job["job_id"])
            self._recover_interrupted_reconciliation_call(batch, job_id)
            interrupted_job_ids.append(job_id)

        current_jobs = self.store.reconciliation_jobs_for_batch(batch.batch_id)
        pending_job_ids = sorted(
            clean_text(job["job_id"])
            for job in current_jobs
            if clean_text(job["status"]) == "pro_pending"
        )
        incomplete = [
            (clean_text(job["job_id"]), clean_text(job["status"]))
            for job in current_jobs
            if clean_text(job["status"]) not in {"completed", "pro_pending"}
        ]
        if incomplete:
            raise ProductWriterError(
                f"{batch.batch_id}: reconciliation recovery did not reach durable states: {incomplete}"
            )
        batch_metadata = json.loads(str(row.get("response_metadata_json") or "{}"))
        batch_metadata.update(
            {
                "validated_batch_commit_recovered": True,
                "revalidated_at": _now(),
                "revalidated_reconciliation_job_ids": recovered_job_ids,
                "interrupted_reconciliation_job_ids": interrupted_job_ids,
                "pending_reconciliation_job_ids": pending_job_ids,
                "physical_api_calls_revalidation": 0,
            }
        )
        if recovered_job_ids:
            batch_metadata["reconciliation_raw_response_revalidated"] = True
        recovery_artifact = {
            "schema_version": "tmcra.v4.validated-batch-recovery.1",
            "batch_id": batch.batch_id,
            "scope_id": batch.scope_id,
            "session_id": batch.session_id,
            "response_sha256": clean_text(row.get("response_sha256")),
            "prior_error_sha256": sha256_text(clean_text(row.get("error"))),
            "completed_reconciliation_job_ids": sorted(
                clean_text(job["job_id"])
                for job in current_jobs
                if clean_text(job["status"]) == "completed"
            ),
            "revalidated_reconciliation_job_ids": recovered_job_ids,
            "interrupted_reconciliation_job_ids": interrupted_job_ids,
            "pending_reconciliation_job_ids": pending_job_ids,
            "physical_api_calls": 0,
            "recovered_at": _now(),
        }
        self._append_unique_jsonl(
            "product_writer_validated_batch_recoveries.jsonl",
            "batch_id",
            recovery_artifact,
        )
        self.stats["validated_batch_recoveries"] += 1
        return self.store.resume_failed_validated_batch(
            batch.batch_id,
            batch_metadata,
            allowed_pending_job_ids=pending_job_ids,
        )

    def _recover_interrupted_batch_call(
        self, batch: SourceBatch
    ) -> sqlite3.Row:
        if not self.recover_interrupted_api_calls:
            raise ProductWriterError(
                f"{batch.batch_id}: API call was started without a durable response; refusing retry"
            )
        call_key = f"flash:{batch.batch_id}"
        self._assert_interrupted_call_has_no_response(call_key)
        self._record_interrupted_call(
            call_key=call_key,
            batch=batch,
            stage="batch_flash_interrupted",
            model=_configured_client_model(self.flash_client, "configured-memory-writer"),
        )
        return self.store.abandon_interrupted_batch_call(batch.batch_id)

    def _recover_failed_billing_call(
        self, batch: SourceBatch, row: Mapping[str, Any]
    ) -> sqlite3.Row:
        if not self.recover_interrupted_api_calls:
            raise ProductWriterError(
                f"{batch.batch_id}: prior billing failure requires audited recovery"
            )
        row_data = dict(row)
        recovered = self.store.recover_failed_billing_call(batch.batch_id)
        self._append_unique_jsonl(
            "product_writer_billing_recoveries.jsonl",
            "batch_id",
            {
                "schema_version": "tmcra.v4.billing-call-recovery.1",
                "batch_id": batch.batch_id,
                "scope_id": batch.scope_id,
                "session_id": batch.session_id,
                "prior_error_sha256": sha256_text(
                    clean_text(row_data.get("error"))
                ),
                "physical_api_calls": 0,
                "replacement_model": _configured_client_model(
                    self.flash_client, "configured-memory-writer"
                ),
                "recovered_at": _now(),
            },
        )
        self.stats["billing_call_recoveries"] += 1
        return recovered

    def _recover_interrupted_reconciliation_call(
        self, batch: SourceBatch, job_id: str
    ) -> sqlite3.Row:
        if not self.recover_interrupted_api_calls:
            raise ProductWriterError(
                f"{job_id}: reconciliation has an uncertain external-call outcome; refusing replacement"
            )
        call_key = f"pro:{job_id}"
        self._assert_interrupted_call_has_no_response(call_key)
        self._record_interrupted_call(
            call_key=call_key,
            batch=batch,
            stage="reconciliation_pro_interrupted",
            model=_configured_client_model(self.pro_client, "configured-reconciliation-model"),
            job_id=job_id,
        )
        job = self.store.abandon_interrupted_reconciliation_call(job_id)
        if job["status"] != "pro_pending":
            raise ProductWriterError(
                f"{job_id}: interrupted Pro recovery did not return to pro_pending"
            )
        return job

    def _revalidate_failed_batch(
        self,
        batch: SourceBatch,
        row: Mapping[str, Any],
        unresolved: Sequence[Mapping[str, Any]],
    ) -> sqlite3.Row:
        row_data = dict(row)
        if clean_text(row_data.get("response_json")):
            return self._revalidate_failed_reconciliation_batch(
                batch, row_data
            )
        metadata = json.loads(str(row_data.get("response_metadata_json") or "{}"))
        if (
            clean_text(metadata.get("status")) != "completed"
            or metadata.get("physical_api_call") is not True
            or int(metadata.get("http_status") or 0) != 200
        ):
            raise ProductWriterError(
                f"{batch.batch_id}: failed batch lacks one clean completed API response"
            )
        raw_response, raw_record = self._raw_api_response(
            f"flash:{batch.batch_id}",
            expected_response_sha256=clean_text(metadata.get("response_sha256")),
            expected_physical_call_id=clean_text(metadata.get("physical_call_id")),
        )
        raw_payload = _strict_json_object(
            raw_response, f"revalidation[{batch.batch_id}]"
        )
        validated = validate_batch_response(raw_payload, batch, unresolved)
        recovery_metadata = {
            **metadata,
            "raw_response_revalidated": True,
            "revalidated_at": _now(),
            "revalidation_prompt_version": PROMPT_VERSION,
            "revalidation_raw_response_sha256": raw_record["raw_response_sha256"],
            "prior_error_sha256": sha256_text(clean_text(row_data.get("error"))),
            "validated_response_sha256": _hash_json(validated),
        }
        persisted = self.store.revalidate_failed_response(
            batch.batch_id, validated, recovery_metadata
        )
        self._append_unique_jsonl(
            "product_writer_revalidations.jsonl",
            "batch_id",
            {
                "batch_id": batch.batch_id,
                "raw_response_sha256": raw_record["raw_response_sha256"],
                "validated_response_sha256": _hash_json(validated),
                "prompt_version": PROMPT_VERSION,
                "physical_api_calls": 0,
            },
        )
        return persisted

    def _ensure_graph_sources(
        self,
        batch: SourceBatch,
        *,
        verify_only: bool = False,
    ) -> RealGraphBackend:
        if self.graph_factory is None:
            raise ProductWriterError("--repo real graph backend is required")
        backend = self.graph_factory.for_scope(batch.scope_id)
        mutation_batch = getattr(backend, "mutation_batch", None)
        with mutation_batch() if callable(mutation_batch) else nullcontext():
            for message in batch.messages:
                if verify_only:
                    info = self.store.source_info(batch.scope_id, message.message_id)
                    source_record_id = clean_text(info.get("source_record_id"))
                    if not source_record_id:
                        raise ProductWriterError(
                            f"{message.message_id}: committed source journal lacks a real graph record ID"
                        )
                    backend.verify_source(
                        message,
                        source_record_id,
                        int(info.get("source_turn_index") or 0),
                    )
                    continue
                source_record_id, source_turn_index = backend.ensure_source(message)
                defer_hook = getattr(backend, "defer_transaction_hook", None)
                if callable(defer_hook) and getattr(
                    backend, "transaction_batch_active", False
                ):
                    defer_hook(
                        lambda connection,
                        scope_id=batch.scope_id,
                        message_id=message.message_id,
                        record_id=source_record_id,
                        turn_index=source_turn_index: self.store.finalize_source_record(
                            connection,
                            scope_id=scope_id,
                            message_id=message_id,
                            source_record_id=record_id,
                            source_turn_index=turn_index,
                        )
                    )
                else:
                    self.store.set_source_record(
                        batch.scope_id,
                        message.message_id,
                        source_record_id,
                        source_turn_index,
                    )
        return backend

    def _set_graph_source_status(self, batch: SourceBatch, status: str, error: str = "") -> None:
        if self.graph_factory is None:
            return
        backend = self.graph_factory.for_scope(batch.scope_id)
        mutation_batch = getattr(backend, "mutation_batch", None)
        with mutation_batch() if callable(mutation_batch) else nullcontext():
            for message in batch.messages:
                info = self.store.source_info(batch.scope_id, message.message_id)
                source_record_id = clean_text(info.get("source_record_id"))
                if source_record_id:
                    backend.set_enrichment_status(source_record_id, status, error)

    def _reconcile(
        self,
        batch: SourceBatch,
        message: SourceMessage,
        assertion_index: int,
        assertion: Mapping[str, Any],
        durability: str,
        current: list[dict[str, Any]],
        *,
        exact_slot_match: bool,
        backend: Any,
    ) -> tuple[dict[str, str], Mapping[str, Any] | None]:
        slot = _graph_slot_key(assertion["canonical_key"])
        cited = {
            "canonical_slot_key": slot,
            "claim_text": assertion["claim_text"],
            "evidence_span_id": assertion["evidence_span_id"],
            "evidence_quote": assertion["evidence_quote"],
            "memory_type": assertion["memory_type"],
            "entity_key": assertion["entity_key"],
            "attribute_key": assertion["attribute_key"],
            "operation": assertion["operation"],
            "relation": assertion["relation"],
            "temporal_status": assertion["temporal_status"],
            "polarity": assertion["polarity"],
            "durability": durability,
            **{key: assertion[key] for key in IDENTITY_FIELDS if key in assertion},
        }
        current_cited = []
        for leaf in current:
            metadata = dict(leaf.get("metadata") or {})
            current_cited.append(
                {
                    "memory_id": clean_text(leaf.get("memory_id")),
                    "canonical_slot_key": clean_text(leaf.get("canonical_slot_key")),
                    "claim_text": clean_text(
                        leaf.get("claim_text") or leaf.get("value")
                    ),
                    "evidence_quote": clean_text(
                        leaf.get("evidence_quote") or leaf.get("value")
                    ),
                    "durability": clean_text(
                        leaf.get("durability") or metadata.get("durability")
                    ),
                    "record_state": clean_text(leaf.get("record_state")),
                    "temporal_status": clean_text(metadata.get("target_status")),
                    "polarity": clean_text(metadata.get("polarity")),
                    "source_record_id": clean_text(metadata.get("source_record_id")),
                    "entity_key": clean_text(metadata.get("entity_key")),
                    "graph_entity_key": clean_text(metadata.get("graph_entity_key")),
                    "attribute_key": clean_text(metadata.get("attribute_key")),
                    "memory_type": clean_text(metadata.get("memory_type")),
                    "memory_family": clean_text(metadata.get("memory_family")),
                    **{key: metadata[key] for key in IDENTITY_FIELDS if key in metadata},
                }
            )
        request = {
            "schema_version": RECONCILIATION_SCHEMA_VERSION,
            "candidate_selector_version": CANDIDATE_SELECTOR_VERSION,
            "message_id": message.message_id,
            "canonical_slot_key": slot,
            "exact_slot_match": bool(exact_slot_match),
            "new_cited_assertion": cited,
            "candidate_cited_leaves": current_cited,
        }
        job_id = _reconciliation_job_id(
            batch, message, assertion_index, assertion
        )
        self.store.create_reconciliation_job(
            job_id=job_id,
            scope_id=batch.scope_id,
            batch_id=batch.batch_id,
            message_id=message.message_id,
            slot=slot,
            assertion_index=assertion_index,
            request=request,
        )
        self.stats["reconciliation_jobs"] += 1
        job = self.store.reconciliation_job(job_id)
        if job is None:
            raise ProductWriterError(f"{job_id}: reconciliation job disappeared")
        frozen_request = json.loads(str(job["request_json"]))
        frozen_candidates = frozen_request.get("candidate_cited_leaves")
        frozen_exact_slot_match = frozen_request.get("exact_slot_match")
        if (
            not isinstance(frozen_candidates, list)
            or type(frozen_exact_slot_match) is not bool
        ):
            raise ProductWriterError(
                f"{job_id}: frozen reconciliation request is malformed"
            )
        def verify_current_binding(
            adjudication: Mapping[str, str],
        ) -> tuple[dict[str, str], Mapping[str, Any] | None]:
            normalized = dict(adjudication)
            if normalized.get("slot_decision") != "bind_existing":
                return normalized, None
            selected_memory_id = clean_text(normalized.get("selected_memory_id"))
            selected_current = next(
                (
                    item
                    for item in current
                    if clean_text(item.get("memory_id")) == selected_memory_id
                ),
                None,
            )
            if selected_current is not None:
                frozen_current = next(
                    (
                        item
                        for item in frozen_candidates
                        if clean_text(item.get("memory_id"))
                        == selected_memory_id
                    ),
                    None,
                )
                if (
                    frozen_current is None
                    or _binding_identity(selected_current)
                    != _binding_identity(frozen_current)
                ):
                    raise ProductWriterError(
                        f"{job_id}: current Pro selection differs from its frozen identity"
                    )
                return normalized, selected_current

            batch_row = self.store.batch_row(batch.batch_id)
            batch_metadata = (
                json.loads(str(batch_row["response_metadata_json"] or "{}"))
                if batch_row is not None
                else {}
            )
            frozen_selected = next(
                (
                    item
                    for item in frozen_candidates
                    if clean_text(item.get("memory_id")) == selected_memory_id
                ),
                None,
            )
            historical = backend.leaf_by_id(selected_memory_id)
            historical_metadata = (
                dict(historical.get("metadata") or {})
                if historical is not None
                else {}
            )
            source_info = self.store.source_info(
                batch.scope_id, message.message_id
            )
            source_record_id = clean_text(source_info.get("source_record_id"))
            replayed_incoming = (
                backend.leaf_for_source_assertion(
                    source_record_id, assertion_index
                )
                if source_record_id
                else None
            )
            incoming_metadata = (
                dict(replayed_incoming.get("metadata") or {})
                if replayed_incoming is not None
                else {}
            )
            frozen_slot = clean_text(
                (frozen_selected or {}).get("canonical_slot_key")
            )
            linked_incoming_id = clean_text(
                historical_metadata.get("superseded_by")
            )
            verified_partial_commit = bool(
                frozen_selected is not None
                and historical is not None
                and replayed_incoming is not None
                and clean_text(historical.get("record_state")) == "superseded"
                and clean_text(historical_metadata.get("superseded_reason"))
                == "v4_reconciliation_replace_current"
                and _binding_identity(historical)
                == _binding_identity(frozen_selected)
                and clean_text(incoming_metadata.get("source_record_id"))
                == source_record_id
                and clean_text(incoming_metadata.get("message_id"))
                == message.message_id
                and int(incoming_metadata.get("llm_write_proposal_index", -1))
                == assertion_index
                and _normalized_claim(
                    clean_text(replayed_incoming.get("claim_text") or replayed_incoming.get("value"))
                )
                == _normalized_claim(clean_text(assertion.get("claim_text")))
                and _normalized_evidence(
                    clean_text(replayed_incoming.get("evidence_quote"))
                )
                == _normalized_evidence(clean_text(assertion.get("evidence_quote")))
                and clean_text(replayed_incoming.get("canonical_slot_key"))
                == frozen_slot
                and (
                    not linked_incoming_id
                    or linked_incoming_id
                    == clean_text(replayed_incoming.get("memory_id"))
                )
            )
            if (
                (
                    batch_metadata.get("validated_batch_commit_recovered")
                    is not True
                    and not verified_partial_commit
                )
                or frozen_selected is None
                or historical is None
                or clean_text(historical_metadata.get("superseded_reason"))
                != "v4_reconciliation_replace_current"
                or _binding_identity(historical) != _binding_identity(frozen_selected)
            ):
                raise ProductWriterError(
                    f"{job_id}: frozen Pro selection is absent from the current candidate set"
                )
            if verified_partial_commit:
                replayed_incoming = backend.repair_partial_replacement(
                    selected_memory_id,
                    clean_text(replayed_incoming.get("memory_id")),
                )
            frozen_identity = _binding_identity(frozen_selected)
            historical_identity = _binding_identity(historical)
            if normalized.get("decision") == "replace_current":
                resolved_binding = historical
                binding_mode = (
                    "verified_partial_message_commit"
                    if verified_partial_commit
                    else "verified_historical_selected"
                )
            else:
                semantic_identity = _binding_semantic_identity(frozen_selected)
                equivalent_active = [
                    item
                    for item in backend.current_leaves(
                        clean_text(frozen_selected.get("canonical_slot_key"))
                    )
                    if _binding_semantic_identity(item) == semantic_identity
                ]
                if len(equivalent_active) != 1:
                    raise ProductWriterError(
                        f"{job_id}: frozen Pro selection lacks one unique active semantic equivalent"
                    )
                resolved_binding = equivalent_active[0]
                binding_mode = "unique_active_semantic_equivalent"
            resolved_semantic_identity = _binding_semantic_identity(resolved_binding)
            self._append_unique_jsonl(
                "product_writer_historical_binding_recoveries.jsonl",
                "job_id",
                {
                    "schema_version": "tmcra.v4.historical-binding-recovery.1",
                    "job_id": job_id,
                    "batch_id": batch.batch_id,
                    "scope_id": batch.scope_id,
                    "message_id": message.message_id,
                    "selected_memory_id": selected_memory_id,
                    "resolved_memory_id": clean_text(
                        resolved_binding.get("memory_id")
                    ),
                    "replayed_incoming_memory_id": clean_text(
                        (replayed_incoming or {}).get("memory_id")
                    ),
                    "source_record_id": source_record_id,
                    "binding_mode": binding_mode,
                    "decision": normalized["decision"],
                    "historical_record_state": clean_text(
                        historical.get("record_state")
                    ),
                    "superseded_reason": "v4_reconciliation_replace_current",
                    "frozen_binding_identity_sha256": _hash_json(frozen_identity),
                    "historical_binding_identity_sha256": _hash_json(
                        historical_identity
                    ),
                    "frozen_semantic_identity_sha256": _hash_json(
                        _binding_semantic_identity(frozen_selected)
                    ),
                    "resolved_semantic_identity_sha256": _hash_json(
                        resolved_semantic_identity
                    ),
                    "physical_api_calls": 0,
                    "recovered_at": _now(),
                },
            )
            self.stats["historical_binding_recoveries"] += 1
            return normalized, resolved_binding

        if job["status"] == "completed":
            parsed = _strict_json_object(
                str(job["response_json"]), f"reconciliation[{job_id}]"
            )
            return verify_current_binding(
                self._validate_reconciliation_response(
                    parsed,
                    current_cited=frozen_candidates,
                    exact_slot_match=frozen_exact_slot_match,
                    path=f"reconciliation[{job_id}]",
                )
            )
        if job["status"] == "failed":
            raise ProductWriterError(
                f"{job_id}: reconciliation has a failed external-call outcome; refusing retry"
            )
        if job["status"] == "pro_started":
            job = self._recover_interrupted_reconciliation_call(batch, job_id)
        if self.pro_client is None:
            raise ProductWriterError(f"{job_id}: Pro client is required for candidate-slot adjudication")
        self.store.start_reconciliation_job(job_id)
        metadata: dict[str, Any] = {}
        self.stats["pro_calls"] += 1
        try:
            result, metadata = _client_result(
                self.pro_client.reconcile(frozen_request)
            )
            self._record_raw_api_response(
                call_key=f"pro:{job_id}",
                batch=batch,
                stage="reconciliation_pro",
                model=_configured_client_model(self.pro_client, "configured-reconciliation-model"),
                response=result,
                metadata=metadata,
                job_id=job_id,
            )
            parsed: Mapping[str, Any] | None = None
            validation_error = ""
            try:
                parsed = (
                    result
                    if isinstance(result, Mapping)
                    else _strict_json_object(
                        str(result), f"reconciliation[{job_id}]"
                    )
                )
                adjudication = self._validate_reconciliation_response(
                    parsed,
                    current_cited=frozen_candidates,
                    exact_slot_match=frozen_exact_slot_match,
                    path=f"reconciliation[{job_id}]",
                )
            except ProductWriterError as exc:
                validation_error = f"{exc.__class__.__name__}: {exc}"
                adjudication = {
                    "slot_decision": "quarantine",
                    "selected_memory_id": "",
                    "decision": "quarantine",
                }
                selected_binding = None
                self.stats["reconciliation_response_quarantines"] += 1
                self._append_unique_jsonl(
                    "product_writer_reconciliation_quarantines.jsonl",
                    "job_id",
                    {
                        "schema_version": "tmcra.v4.reconciliation-quarantine.1",
                        "job_id": job_id,
                        "batch_id": batch.batch_id,
                        "scope_id": batch.scope_id,
                        "message_id": message.message_id,
                        "assertion_index": assertion_index,
                        "error": validation_error,
                        "raw_response_sha256": _hash_json(result),
                        "physical_api_calls": 1,
                        "quarantined_at": _now(),
                    },
                )
            else:
                adjudication, selected_binding = verify_current_binding(adjudication)
            model_adjudication = dict(parsed) if parsed is not None else {"raw_response": str(result)}
            if model_adjudication != adjudication:
                metadata = {
                    **metadata,
                    "controller_normalization": (
                        "invalid_pro_response_quarantined"
                        if validation_error
                        else "exact_slot_identity_and_parallel_action"
                    ),
                    "controller_validation_error": validation_error,
                    "model_adjudication_sha256": _hash_json(model_adjudication),
                    "normalized_adjudication_sha256": _hash_json(adjudication),
                }
            self.store.finish_reconciliation_job(
                job_id, adjudication["decision"], adjudication, metadata
            )
            self._record_api_call(
                call_key=f"pro:{job_id}",
                batch=batch,
                stage="reconciliation_pro",
                model=_configured_client_model(self.pro_client, "configured-reconciliation-model"),
                metadata=metadata,
                job_id=job_id,
            )
        except Exception as exc:
            call_metadata = dict(getattr(exc, "metadata", None) or metadata)
            error = f"{exc.__class__.__name__}: {exc}"
            self.store.fail_reconciliation_job(job_id, error, call_metadata)
            self._record_api_call(
                call_key=f"pro:{job_id}",
                batch=batch,
                stage="reconciliation_pro",
                model=_configured_client_model(self.pro_client, "configured-reconciliation-model"),
                metadata=call_metadata,
                job_id=job_id,
                error=error,
            )
            raise
        return adjudication, selected_binding

    @staticmethod
    def _validate_reconciliation_response(
        value: Mapping[str, Any],
        *,
        current_cited: Sequence[Mapping[str, Any]],
        exact_slot_match: bool,
        path: str,
    ) -> dict[str, str]:
        required = {"slot_decision", "selected_memory_id", "decision"}
        missing = required - set(value)
        if missing:
            raise ProductWriterError(
                f"{path} is missing required keys: {sorted(missing)}"
            )
        raw_slot_value = value.get("slot_decision")
        if not isinstance(raw_slot_value, str) or not raw_slot_value.strip():
            raise ProductWriterError(f"{path}.slot_decision must be a non-empty string")
        raw_slot_decision = raw_slot_value.strip().lower().replace("-", "_").replace(" ", "_")
        raw_selected_memory_id = value.get("selected_memory_id")
        if not isinstance(raw_selected_memory_id, str):
            raise ProductWriterError(f"{path}.selected_memory_id must be a string")
        selected_memory_id = raw_selected_memory_id.strip()
        raw_decision_value = value.get("decision")
        if not isinstance(raw_decision_value, str) or not raw_decision_value.strip():
            raise ProductWriterError(f"{path}.decision must be a non-empty string")
        raw_decision = raw_decision_value
        decision = re.sub(
            r"_+",
            "_",
            raw_decision.strip().lower().replace("-", "_").replace(" ", "_"),
        )
        if decision not in DECISIONS:
            raise ProductWriterError(
                f"{path}.decision is unsupported: {raw_decision!r}"
            )
        candidates = [item for item in current_cited if clean_text(item.get("memory_id"))]
        candidate_ids = {clean_text(item.get("memory_id")) for item in candidates}
        if raw_slot_decision in SLOT_DECISIONS:
            slot_decision = raw_slot_decision
        elif (
            raw_slot_decision == decision
            and decision in {"merge_support", "replace_current", "keep_parallel", "challenge"}
            and selected_memory_id in candidate_ids
        ):
            # Pro occasionally places the conflict action in both enum fields.
            # A supplied valid candidate ID makes the intended slot binding
            # unambiguous; all other out-of-schema values remain hard failures.
            slot_decision = "bind_existing"
        else:
            raise ProductWriterError(
                f"{path}.slot_decision is unsupported: {raw_slot_decision!r}"
            )
        if exact_slot_match and slot_decision != "quarantine":
            if not candidates:
                raise ProductWriterError(f"{path}: exact slot collision lacks candidates")
            preferred = min(
                candidates,
                key=lambda item: (
                    {"active": 0, "promoted": 1, "parallel_active": 2}.get(
                        clean_text(item.get("record_state")), 3
                    ),
                    -int(item.get("turn_index") or 0),
                    clean_text(item.get("memory_id")),
                ),
            )
            slot_decision = "bind_existing"
            selected_memory_id = clean_text(preferred.get("memory_id"))
            if decision == "insert":
                decision = "keep_parallel"
        if slot_decision == "bind_existing":
            if selected_memory_id not in candidate_ids:
                raise ProductWriterError(
                    f"{path}.selected_memory_id is not a supplied candidate"
                )
            if decision == "insert":
                raise ProductWriterError(
                    f"{path}: a bound existing slot cannot use insert"
                )
        elif slot_decision == "keep_proposed":
            if selected_memory_id or decision != "insert":
                raise ProductWriterError(
                    f"{path}: keep_proposed requires empty selected_memory_id and insert"
                )
        else:
            if selected_memory_id or decision != "quarantine":
                raise ProductWriterError(
                    f"{path}: quarantine requires empty selected_memory_id and quarantine"
                )
        return {
            "slot_decision": slot_decision,
            "selected_memory_id": selected_memory_id,
            "decision": decision,
        }

    def _commit_message(
        self,
        batch: SourceBatch,
        message_index: int,
        response_message: Mapping[str, Any],
    ) -> int:
        source = batch.messages[message_index]
        journal = self.store.prepare_message_commit(
            batch, source, response_message
        )
        commit_id = str(journal["commit_id"])
        if journal["status"] == "committed":
            return int(journal["semantic_committed"])
        v3 = dict(response_message["v3"])
        durability = list(response_message["durability"])
        if self.graph_factory is None:
            raise ProductWriterError("real graph backend is required for V4 semantic commit")
        backend = self.graph_factory.for_scope(batch.scope_id)
        source_info = self.store.source_info(batch.scope_id, source.message_id)
        source_record_id = clean_text(source_info.get("source_record_id"))
        if not source_record_id:
            raise ProductWriterError(f"{source.message_id}: real source record ID is missing")
        source_turn_index = int(source_info.get("source_turn_index") or 0)
        if clean_text(journal["plan_sha256"]):
            plan_json = str(journal["plan_json"])
            if sha256_text(plan_json) != clean_text(journal["plan_sha256"]):
                raise ProductWriterError(
                    f"{commit_id}: frozen message commit plan hash changed"
                )
            return self._execute_message_commit_plan(
                backend=backend,
                batch=batch,
                source=source,
                source_record_id=source_record_id,
                plan=json.loads(plan_json),
                response_message=response_message,
            )
        assertions = [bind_assertion_identity(item, source.identity_metadata,
            scope_id=batch.scope_id, source_message_id=source.message_id)
            for item in v3.get("assertions") or []]
        decisions: dict[int, str] = {}
        current_by_index: dict[int, Sequence[Mapping[str, Any]]] = {}
        duplicate_provenance: list[dict[str, Any]] = []

        def add_duplicate_provenance(assertion: Mapping[str, Any], leaf: Mapping[str, Any]) -> None:
            evidence_quote = str(assertion["evidence_quote"])
            evidence_char_start, evidence_char_end = _exact_provenance_offsets(
                source.content,
                str(assertion["evidence_span_id"]),
                evidence_quote,
                f"{source.message_id}.duplicate_provenance",
            )
            duplicate_provenance.append(
                {
                    "leaf_id": leaf["memory_id"],
                    "leaf_identity": _binding_identity(leaf),
                    "provenance": {
                        "batch_id": batch.batch_id,
                        "message_id": source.message_id,
                        "evidence_span_id": assertion["evidence_span_id"],
                        "evidence_quote": evidence_quote,
                        "source_char_start": evidence_char_start,
                        "source_char_end": evidence_char_end,
                        **dict(source.actor_metadata),
                    },
                },
            )

        for assertion_index, assertion in enumerate(assertions):
            exact_current = [leaf for leaf in backend.current_leaves(str(assertion["canonical_key"]))
                if identity_compatible(assertion, dict(leaf.get("metadata") or {}), batch.scope_id)]
            new_claim = _normalized_claim(str(assertion["claim_text"]))
            persisted_job_id = _reconciliation_job_id(
                batch, source, assertion_index, assertion
            )
            persisted_job = self.store.reconciliation_job(persisted_job_id)
            exact_duplicate = [
                leaf
                for leaf in exact_current
                if _normalized_claim(str(leaf["value"])) == new_claim
            ]
            if exact_duplicate and persisted_job is None:
                add_duplicate_provenance(assertion, exact_duplicate[0])
                decisions[assertion_index] = "duplicate"
                current_by_index[assertion_index] = exact_current
                continue

            candidates = list(
                exact_current or backend.candidate_leaves(assertion, limit=3)
            )
            if persisted_job is not None:
                frozen_request = json.loads(str(persisted_job["request_json"]))
                for frozen in list(
                    frozen_request.get("candidate_cited_leaves") or []
                ):
                    if not isinstance(frozen, Mapping):
                        continue
                    frozen_memory_id = clean_text(frozen.get("memory_id"))
                    if not frozen_memory_id or any(
                        clean_text(item.get("memory_id")) == frozen_memory_id
                        for item in candidates
                    ):
                        continue
                    leaf = backend.leaf_by_id(frozen_memory_id)
                    if (
                        leaf is not None
                        and clean_text(leaf.get("record_state"))
                        in {"active", "parallel_active", "promoted"}
                        and _binding_identity(leaf) == _binding_identity(frozen)
                    ):
                        candidates.append(leaf)
            if not candidates and persisted_job is None:
                decisions[assertion_index] = "insert"
                current_by_index[assertion_index] = []
                continue
            adjudication, selected_binding = self._reconcile(
                batch,
                source,
                assertion_index,
                assertion,
                durability[assertion_index],
                candidates,
                exact_slot_match=bool(exact_current),
                backend=backend,
            )
            slot_decision = adjudication["slot_decision"]
            if slot_decision == "bind_existing":
                if selected_binding is None:
                    raise ProductWriterError(
                        f"{batch.batch_id}: bound reconciliation lacks a selected leaf"
                    )
                selected = selected_binding
                assertion = self._bind_assertion_to_existing(assertion, selected)
                assertions[assertion_index] = assertion
                current = backend.current_leaves(str(assertion["canonical_key"]))
                if (
                    adjudication["decision"] == "replace_current"
                    and not any(
                        clean_text(item.get("memory_id"))
                        == clean_text(selected.get("memory_id"))
                        for item in current
                    )
                ):
                    current = [selected, *current]
                bound_duplicate = [
                    leaf
                    for leaf in current
                    if _normalized_claim(str(leaf["value"])) == new_claim
                ]
                if bound_duplicate:
                    add_duplicate_provenance(assertion, bound_duplicate[0])
                    decisions[assertion_index] = "duplicate"
                    current_by_index[assertion_index] = current
                    continue
                if adjudication["decision"] == "merge_support":
                    add_duplicate_provenance(assertion, selected)
                    decisions[assertion_index] = "duplicate"
                    current_by_index[assertion_index] = current
                    continue
                decisions[assertion_index] = adjudication["decision"]
                current_by_index[assertion_index] = current
            elif slot_decision == "keep_proposed":
                decisions[assertion_index] = "insert"
                current_by_index[assertion_index] = []
            else:
                decisions[assertion_index] = "quarantine"
                current_by_index[assertion_index] = candidates
        committed_assertions: list[Mapping[str, Any]] = []
        committed_durabilities: list[str] = []
        committed_decisions: dict[int, str] = {}
        committed_current: dict[int, Sequence[Mapping[str, Any]]] = {}
        for original_index, assertion in enumerate(assertions):
            decision = decisions.get(original_index, "insert")
            if decision == "duplicate":
                continue
            committed_index = len(committed_assertions)
            committed_assertions.append(assertion)
            committed_durabilities.append(durability[original_index])
            committed_decisions[committed_index] = decision
            committed_current[committed_index] = current_by_index.get(original_index, [])
        committed_v3 = dict(v3)
        committed_v3["assertions"] = committed_assertions
        interactions = list(v3.get("interactions") or [])
        resolutions = list(v3.get("resolutions") or [])
        plan = {
            "schema_version": "tmcra.v4.message-commit-plan.1",
            "batch_id": batch.batch_id,
            "message_id": source.message_id,
            "source_record_id": source_record_id,
            "source_turn_index": source_turn_index,
            "extraction": committed_v3,
            "durabilities": committed_durabilities,
            "decisions": committed_decisions,
            "current_by_index": committed_current,
            "duplicate_provenance": duplicate_provenance,
            "interactions": interactions,
            "resolutions": resolutions,
        }
        self.store.freeze_message_commit_plan(commit_id, plan)

        return self._execute_message_commit_plan(
            backend=backend,
            batch=batch,
            source=source,
            source_record_id=source_record_id,
            plan=plan,
            response_message=response_message,
        )

    def _execute_message_commit_plan(
        self,
        *,
        backend: Any,
        batch: SourceBatch,
        source: SourceMessage,
        source_record_id: str,
        plan: Mapping[str, Any],
        response_message: Mapping[str, Any],
    ) -> int:
        commit_id = self.store._message_commit_id(batch, source)
        if (
            clean_text(plan.get("batch_id")) != batch.batch_id
            or clean_text(plan.get("message_id")) != source.message_id
            or clean_text(plan.get("source_record_id")) != source_record_id
        ):
            raise ProductWriterError(
                f"{commit_id}: frozen message commit plan identity changed"
            )
        extraction = dict(plan.get("extraction") or {})
        durabilities = list(plan.get("durabilities") or [])
        decisions = {
            int(key): str(value)
            for key, value in dict(plan.get("decisions") or {}).items()
        }
        current_by_index = {
            int(key): list(value or [])
            for key, value in dict(plan.get("current_by_index") or {}).items()
        }
        duplicate_provenance = [
            dict(item) for item in list(plan.get("duplicate_provenance") or [])
        ]
        interactions = [dict(item) for item in list(plan.get("interactions") or [])]
        resolutions = [dict(item) for item in list(plan.get("resolutions") or [])]

        def finalize(
            connection: sqlite3.Connection, semantic_committed: int
        ) -> None:
            self.store.finalize_message_commit(
                connection,
                commit_id=commit_id,
                batch=batch,
                message=source,
                source_record_id=source_record_id,
                interactions=interactions,
                resolutions=resolutions,
                semantic_committed=semantic_committed,
            )

        atomic_commit = bool(
            getattr(backend, "supports_atomic_message_commit", False)
        )
        try:
            if not atomic_commit:
                for item in duplicate_provenance:
                    backend.add_provenance(
                        str(item["leaf_id"]),
                        source_record_id=source_record_id,
                        source_turn_index=int(plan["source_turn_index"]),
                        provenance=dict(item.get("provenance") or {}),
                    )
            kwargs = {
                "message": source,
                "source_record_id": source_record_id,
                "source_turn_index": int(plan["source_turn_index"]),
                "extraction": extraction,
                "durabilities": durabilities,
                "decisions": decisions,
                "current_by_index": current_by_index,
            }
            if atomic_commit:
                kwargs.update(
                    {
                        "duplicate_provenance": duplicate_provenance,
                        "transaction_hook": finalize,
                    }
                )
            stale_attempts = 0
            while True:
                try:
                    committed_count = backend.commit_message(**kwargs)
                    break
                except Exception as exc:
                    stale_checker = getattr(backend, "is_stale_snapshot_error", None)
                    is_stale_snapshot = bool(
                        callable(stale_checker) and stale_checker(exc)
                    )
                    if not atomic_commit or not is_stale_snapshot or stale_attempts >= 3:
                        raise
                    stale_attempts += 1
                    self.stats["stale_graph_snapshot_retries"] += 1
                    refresh = getattr(backend, "refresh_after_stale_snapshot", None)
                    if callable(refresh):
                        refresh()
                    time.sleep(0.01 * stale_attempts)
            committed_row = self.store.prepare_message_commit(
                batch, source, response_message
            )
            deferred_atomic_commit = bool(
                atomic_commit
                and getattr(backend, "transaction_batch_active", False)
            )
            if (
                committed_row["status"] != "committed"
                and not deferred_atomic_commit
            ):
                with closing(self.store._connect()) as connection, connection:
                    finalize(connection, committed_count)
        except Exception as exc:
            self.store.record_message_commit_error(
                commit_id, f"{exc.__class__.__name__}: {exc}"
            )
            raise
        self.stats["fast_assertion_leaves"] += committed_count
        return committed_count

    @staticmethod
    def _bind_assertion_to_existing(
        assertion: Mapping[str, Any], leaf: Mapping[str, Any]
    ) -> dict[str, Any]:
        metadata = dict(leaf.get("metadata") or {})
        canonical_slot_key = clean_text(
            leaf.get("canonical_slot_key") or metadata.get("canonical_slot_key")
        )
        if not canonical_slot_key:
            raise ProductWriterError("selected binding candidate lacks canonical slot")
        bound = dict(assertion)
        bound["canonical_key"] = canonical_slot_key.removeprefix("memory.")
        for key in (
            "entity_key",
            "graph_entity_key",
            "attribute_key",
            "memory_type",
            "memory_family",
        ):
            value = clean_text(metadata.get(key))
            if value:
                bound[key] = value
        bound["operation"] = "replace"
        return bound

    def run(self, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        messages, exclusions = normalize_source_inventory(rows)
        self.stats["input_messages"] = len(messages) + len(exclusions)
        self.stats["source_messages"] = len(messages)
        self.stats["excluded_empty_source_messages"] = len(exclusions)
        if self.log_dir is not None:
            payload = {
                "schema_version": "tmcra.v4.source-exclusions.1",
                "reason_policy": "exclude_only_whitespace_empty_message_carriers",
                "count": len(exclusions),
                "messages": exclusions,
            }
            path = self.log_dir / "source_exclusions.json"
            temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)
        batches = build_batches(messages)
        self.stats["batches"] = len(batches)
        for batch in batches:
            existing_batch = self.store.batch_row(batch.batch_id)
            if existing_batch is None:
                unresolved = self.store.unresolved_interactions(
                    batch.scope_id, batch.session_id
                )
                request = build_batch_request(batch, unresolved)
            else:
                request = json.loads(str(existing_batch["request_json"]))
                if (
                    request.get("schema_version") != BATCH_SCHEMA_VERSION
                    or request.get("batch_id") != batch.batch_id
                ):
                    raise ProductWriterError(
                        f"{batch.batch_id}: persisted batch request schema or ID changed"
                    )
                unresolved = list(request.get("unresolved_interactions") or [])
            row = self.store.prepare(batch, request)
            if row["status"] == "committed":
                backend = self._ensure_graph_sources(batch, verify_only=True)
                source_infos = [
                    self.store.source_info(batch.scope_id, message.message_id)
                    for message in batch.messages
                ]
                source_record_ids = [
                    clean_text(info.get("source_record_id")) for info in source_infos
                ]
                journal_statuses = {
                    source_record_id: clean_text(info.get("status"))
                    for source_record_id, info in zip(source_record_ids, source_infos)
                }
                graph_statuses = backend.source_enrichment_statuses(source_record_ids)
                repair_ids = [
                    source_record_id
                    for source_record_id in source_record_ids
                    if (
                        journal_statuses.get(source_record_id) != "enriched"
                        or graph_statuses.get(source_record_id) != "enriched"
                    )
                ]
                if repair_ids:
                    self._append_unique_jsonl(
                        "product_writer_committed_source_repairs.jsonl",
                        "batch_id",
                        {
                            "schema_version": "tmcra.v4.committed-source-repair.1",
                            "batch_id": batch.batch_id,
                            "scope_id": batch.scope_id,
                            "source_record_ids": repair_ids,
                            "prior_enrichment_statuses": {
                                source_record_id: graph_statuses.get(source_record_id, "")
                                for source_record_id in repair_ids
                            },
                            "prior_source_journal_statuses": {
                                source_record_id: journal_statuses.get(source_record_id, "")
                                for source_record_id in repair_ids
                            },
                            "physical_api_calls": 0,
                            "repaired_at": _now(),
                        },
                    )
                    self.store.mark_source_enriched(batch)
                    for source_record_id in source_record_ids:
                        backend.set_enrichment_status(source_record_id, "enriched")
                    self.stats["committed_source_status_repairs"] += len(
                        repair_ids
                    )
                self.stats["resumed_batches"] += 1
                continue
            if row["status"] == "api_started":
                row = self._recover_interrupted_batch_call(batch)
            if row["status"] == "failed":
                row_data = dict(row)
                try:
                    failure_metadata = json.loads(
                        str(row_data.get("response_metadata_json") or "{}")
                    )
                except json.JSONDecodeError:
                    failure_metadata = {}
                if (
                    clean_text(failure_metadata.get("status")) == "http_error"
                    and int(failure_metadata.get("http_status") or 0) == 402
                ):
                    row = self._recover_failed_billing_call(batch, row_data)
                else:
                    if not self.revalidate_failed_raw_response:
                        raise ProductWriterError(f"{batch.batch_id}: prior batch failed; refusing retry")
                    row = self._revalidate_failed_batch(batch, row_data, unresolved)
            try:
                backend = self._ensure_graph_sources(
                    batch, verify_only=row["status"] == "validated"
                )
            except Exception as exc:
                error = f"{exc.__class__.__name__}: {exc}"
                self.store.mark_source_enrichment_failed(batch, error)
                self.store.fail_batch(batch.batch_id, error)
                raise
            if row["status"] == "validated":
                validated = json.loads(row["response_json"])
                self.stats["resumed_batches"] += 1
            elif not any(_v3_writer.permits_source_extraction(message.role, message.actor_metadata) for message in batch.messages):
                # Immutable-only batches are journaled and committed without a semantic API call.
                validated = {"schema_version": BATCH_SCHEMA_VERSION, "batch_id": batch.batch_id, "messages": []}
                self.store.persist_response(batch.batch_id, validated, {"api_call_count": 0, "reason": "immutable_only_batch"})
            else:
                self.store.mark_api_started(batch.batch_id)
                metadata: dict[str, Any] = {}
                self.stats["flash_calls"] += 1
                try:
                    result, metadata = _client_result(self.flash_client.complete(request))
                    self._record_raw_api_response(
                        call_key=f"flash:{batch.batch_id}",
                        batch=batch,
                        stage="batch_flash",
                        model=_configured_client_model(self.flash_client, "configured-memory-writer"),
                        response=result,
                        metadata=metadata,
                    )
                    raw_payload = result if isinstance(result, Mapping) else _strict_json_object(str(result), f"batch[{batch.batch_id}]")
                    validated = validate_batch_response(raw_payload, batch, unresolved)
                    self.store.persist_response(batch.batch_id, validated, metadata)
                    self._record_api_call(
                        call_key=f"flash:{batch.batch_id}",
                        batch=batch,
                        stage="batch_flash",
                        model=_configured_client_model(self.flash_client, "configured-memory-writer"),
                        metadata={
                            **metadata,
                            "request_content_sha256": _hash_json(request),
                            "validated_response_sha256": _hash_json(validated),
                        },
                    )
                except Exception as exc:
                    error = f"{exc.__class__.__name__}: {exc}"
                    call_metadata = dict(getattr(exc, "metadata", None) or metadata)
                    self._record_api_call(
                        call_key=f"flash:{batch.batch_id}",
                        batch=batch,
                        stage="batch_flash",
                        model=_configured_client_model(self.flash_client, "configured-memory-writer"),
                        metadata=call_metadata,
                        error=error,
                    )
                    self.store.mark_source_enrichment_failed(batch, error)
                    self._set_graph_source_status(batch, "failed", error)
                    self.store.fail_batch(batch.batch_id, error, call_metadata)
                    raise
            try:
                source_indexes = {
                    message.message_id: index for index, message in enumerate(batch.messages)
                }
                product_write_rows: list[dict[str, Any]] = []
                mutation_batch = getattr(backend, "mutation_batch", None)
                with mutation_batch() if callable(mutation_batch) else nullcontext():
                    for response_message in validated["messages"]:
                        source_index = source_indexes[response_message["message_id"]]
                        source = batch.messages[source_index]
                        if not _v3_writer.permits_source_extraction(source.role, source.actor_metadata):
                            raise ProductWriterError("persisted response requires explicit origin review before semantic replay")
                        committed_count = self._commit_message(
                            batch, source_index, response_message
                        )
                        output = response_message["v3"]
                        validation_warnings = list(
                            output.get("validation_warnings") or []
                        )
                        self.stats["validation_warnings"] += len(
                            validation_warnings
                        )
                        product_write_rows.append(
                            {
                                "message_key": f"{batch.scope_id}:{response_message['message_id']}",
                                "batch_id": batch.batch_id,
                                "scope_id": batch.scope_id,
                                "message_id": response_message["message_id"],
                                "message_role": response_message["message_role"],
                                "content_sha256": sha256_text(
                                    batch.messages[source_index].content
                                ),
                                "source": 1,
                                "semantic_proposals": len(
                                    output.get("assertions") or []
                                ),
                                "semantic_committed": committed_count,
                                "facet": sum(
                                    len(item.get("facets") or [])
                                    for item in output.get("assertions") or []
                                ),
                                "interaction": len(
                                    output.get("interactions") or []
                                ),
                                "resolution_count": len(
                                    output.get("resolutions") or []
                                ),
                                "validation_warning_count": len(
                                    validation_warnings
                                ),
                                "validation_warnings": validation_warnings,
                                "writer_called": True,
                            }
                        )
                source_only = tuple(message for message in batch.messages
                    if not _v3_writer.permits_source_extraction(message.role, message.actor_metadata))
                if source_only:
                    source_batch = SourceBatch(batch.scope_id, batch.session_id, batch.session_index, batch.batch_index, source_only)
                    self.store.mark_source_enriched(source_batch)
                    self._set_graph_source_status(source_batch, "enriched")
                for product_write_row in product_write_rows:
                    self._append_unique_jsonl(
                        "product_write_messages.jsonl",
                        "message_key",
                        product_write_row,
                    )
                self.store.commit_batch(batch.batch_id)
                if not validated["messages"]:
                    self.store.mark_source_enriched(batch)
                    self._set_graph_source_status(batch, "enriched")
                elif not bool(
                    getattr(
                        self.graph_factory.for_scope(batch.scope_id),
                        "supports_atomic_message_commit",
                        False,
                    )
                ):
                    # Test/legacy backends cannot join the SQLite transaction.
                    self._set_graph_source_status(batch, "enriched")
            except Exception as exc:
                error = f"{exc.__class__.__name__}: {exc}"
                # The validated response remains replayable. Message journals
                # identify exactly which graph commits completed, so a local
                # commit failure must not downgrade the whole batch or already
                # committed source messages.
                self.store.record_batch_commit_error(batch.batch_id, error)
                raise
        return dict(self.stats)


def _build_cli_client(*, reviewer_model: str, timeout: float, max_tokens: int) -> tuple[DeepSeekBatchClient, DeepSeekBatchClient]:
    base_url = clean_text(os.getenv("TMCRA_WRITER_BASE_URL"))
    model = clean_text(os.getenv("TMCRA_WRITER_MODEL"))
    keys = [clean_text(value) for value in os.getenv("TMCRA_WRITER_API_KEY_POOL", "").split(",") if clean_text(value)]
    if not base_url or not model or not reviewer_model or not keys:
        raise ProductWriterError("explicit writer base URL, Flash model, Pro model, and API key pool are required")
    if model != "deepseek-v4-flash" or reviewer_model != "deepseek-v4-pro":
        raise ProductWriterError("V4 requires deepseek-v4-flash and deepseek-v4-pro")
    return (
        DeepSeekBatchClient(base_url=base_url, model=model, api_keys=keys, timeout=timeout, max_tokens=max_tokens),
        DeepSeekBatchClient(base_url=base_url, model=reviewer_model, api_keys=keys, timeout=timeout, max_tokens=max_tokens),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="TMCRA V4 consecutive-session batch writer")
    parser.add_argument("--input", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--reviewer-model", default="deepseek-v4-pro")
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument(
        "--revalidate-failed-raw-response",
        action="store_true",
        help="revalidate one clean, hashed failed response without another API call",
    )
    parser.add_argument(
        "--recover-interrupted-api-calls",
        action="store_true",
        help=(
            "after explicit process-loss review, replace started calls that have "
            "no durable response or call artifact using the same model"
        ),
    )
    parser.add_argument(
        "--repair-provenance-offsets-only",
        action="store_true",
        help="deterministically repair missing duplicate-provenance Source offsets without API calls",
    )
    args = parser.parse_args()
    repo = Path(args.repo).resolve()
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    input_path = Path(args.input).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise ProductWriterError("writer input must be a non-empty JSON array")
    database = out_dir / "native_memory.sqlite3"
    if args.repair_provenance_offsets_only:
        messages, _ = normalize_source_inventory(rows)
        scopes = sorted({message.scope_id for message in messages})
        if len(scopes) != 1:
            raise ProductWriterError(
                "provenance repair requires exactly one frozen scope per worker"
            )
        report = RealGraphFactory(repo=repo, database=database).for_scope(
            scopes[0]
        ).repair_provenance_offsets()
        report_path = out_dir / "provenance_offset_repair_report.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    flash, pro = _build_cli_client(reviewer_model=args.reviewer_model, timeout=args.timeout_seconds, max_tokens=args.max_tokens)
    writer = V4BatchWriter(
        store=V4BatchStore(database),
        flash_client=flash,
        pro_client=pro,
        graph_factory=RealGraphFactory(repo=repo, database=database),
        log_dir=out_dir,
        revalidate_failed_raw_response=args.revalidate_failed_raw_response,
        recover_interrupted_api_calls=args.recover_interrupted_api_calls,
    )
    report = writer.run(rows)
    report.update({"schema_version": "tmcra.v4.batch-writer-run.1", "writer_schema_version": BATCH_SCHEMA_VERSION, "prompt_version": PROMPT_VERSION, "candidate_selector_version": CANDIDATE_SELECTOR_VERSION, "completed": True, "db_path": str(out_dir / "native_memory.sqlite3")})
    (out_dir / "product_writer_report.json").write_text(_json(report) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
