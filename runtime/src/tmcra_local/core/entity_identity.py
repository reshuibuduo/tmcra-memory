"""Caller-declared, scope-local entity identity and auditable correction metadata.

No model IDs, fuzzy names, translations, or global alias registry are trusted here.
The caller maps extraction keys explicitly; unbound/ambiguous declarations isolate
rather than silently joining a legacy name slot. Declarations are not verified facts.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

IDENTITY_FIELDS = ("entity_id", "entity_aliases", "entity_scope_id", "identity_status",
                   "identity_origin", "original_canonical_key", "declared_correction")


def _text(value: Any, field: str, limit: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError(f"identity_metadata.{field} must be a nonempty bounded string")
    return value.strip()


def _strings(value: Any, field: str, *, required: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) > 32 or (required and not value):
        raise ValueError(f"identity_metadata.{field} must be a list of 1..32 strings" if required else
                         f"identity_metadata.{field} must be a list of at most 32 strings")
    return list(dict.fromkeys(_text(v, field) for v in value))


def normalize_identity_metadata(value: Any) -> dict[str, Any]:
    if value is None or value == {}:
        return {}
    if not isinstance(value, Mapping) or set(value) - {"entity_bindings", "corrections"}:
        raise ValueError("identity_metadata accepts only entity_bindings and corrections")
    bindings = value.get("entity_bindings", [])
    corrections = value.get("corrections", [])
    if not isinstance(bindings, list) or len(bindings) > 32 or not isinstance(corrections, list) or len(corrections) > 32:
        raise ValueError("identity_metadata collections must contain at most 32 items")
    result: dict[str, Any] = {"entity_bindings": [], "corrections": []}
    for binding in bindings:
        if not isinstance(binding, Mapping) or set(binding) - {"entity_id", "entity_keys", "aliases"}:
            raise ValueError("identity binding fields are entity_id, entity_keys, aliases")
        result["entity_bindings"].append({"entity_id": _text(binding.get("entity_id"), "entity_id"),
            "entity_keys": _strings(binding.get("entity_keys"), "entity_keys", required=True),
            "aliases": _strings(binding.get("aliases", []), "aliases")})
    ids = {b["entity_id"] for b in result["entity_bindings"]}
    seen = set()
    for correction in corrections:
        if not isinstance(correction, Mapping) or set(correction) - {"entity_id", "attribute_key", "effective_at", "reason"}:
            raise ValueError("invalid correction fields")
        item = {"entity_id": _text(correction.get("entity_id"), "entity_id"),
                "attribute_key": _text(correction.get("attribute_key"), "attribute_key"),
                "effective_at": _text(correction.get("effective_at"), "effective_at", 64)}
        if item["entity_id"] not in ids:
            raise ValueError("correction entity_id requires an explicit binding in this message")
        try:
            timestamp = datetime.fromisoformat(item["effective_at"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("correction effective_at must be an ISO timestamp") from exc
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("correction effective_at must include timezone")
        if "reason" in correction:
            item["reason"] = _text(correction["reason"], "reason", 1000)
        key = (item["entity_id"], item["attribute_key"])
        if key in seen:
            raise ValueError("duplicate entity/attribute correction is ambiguous")
        seen.add(key)
        result["corrections"].append(item)
    if len(json.dumps(result, ensure_ascii=False).encode()) > 32768:
        raise ValueError("identity_metadata exceeds 32 KiB")
    return result if bindings or corrections else {}


def _key(value: Any) -> str:
    # Mirror v3 canonical_identifier's ASCII token separators + reserved prefix.
    # This is wire-key normalization, never fuzzy label/alias matching. Colliding
    # declarations of different IDs remain ambiguous and therefore separate.
    raw = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return ".".join(re.findall(r"[a-z0-9]+", raw)).removeprefix("user.")


def _digest(*values: str) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()[:24]


def bind_assertion_identity(assertion: Mapping[str, Any], context: Any, *, scope_id: str,
                            source_message_id: str) -> dict[str, Any]:
    # Fields produced by an LLM, including a guessed entity_id, are discarded.
    result = {k: v for k, v in assertion.items() if k not in IDENTITY_FIELDS}
    data = normalize_identity_metadata(context)
    if not data:
        return result
    key = _key(assertion.get("entity_key"))
    matches = [b for b in data["entity_bindings"] if key in {_key(k) for k in b["entity_keys"]}]
    ids = {b["entity_id"] for b in matches}
    result.update({"original_canonical_key": str(assertion.get("canonical_key") or ""),
                   "entity_scope_id": scope_id, "identity_origin": "caller_declared"})
    attribute = str(assertion.get("attribute_key") or "")
    family = str(assertion.get("memory_family") or assertion.get("memory_type") or "")
    if len(ids) == 1:
        eid = next(iter(ids))
        result.update({"entity_id": eid, "identity_status": "declared",
            "entity_aliases": list(dict.fromkeys(a for b in matches for a in b["aliases"])),
            "canonical_key": f"identity.{_digest(scope_id, eid)}.{_digest(family, attribute)}"})
        correction = next((c for c in data["corrections"] if c["entity_id"] == eid
                           and ".".join(re.findall(r"[a-z0-9]+", c["attribute_key"].lower())) == attribute), None)
        if correction:
            result["declared_correction"] = dict(correction)
    else:
        # Do not silently fall back to a homonymous global/legacy slot.
        result.update({"identity_status": "ambiguous" if ids else "unbound",
            "canonical_key": f"identity.unresolved.{_digest(scope_id, source_message_id, key)}.{_digest(family, attribute)}"})
    return result


def identity_compatible(incoming: Mapping[str, Any], existing: Mapping[str, Any], scope_id: str) -> bool:
    if incoming.get("identity_status") in {"ambiguous", "unbound"} or existing.get("identity_status") in {"ambiguous", "unbound"}:
        return False
    incoming_id, existing_id = incoming.get("entity_id"), existing.get("entity_id")
    if not incoming_id and not existing_id:
        return True  # Preserve legacy candidate behavior, not a new ID assertion.
    return bool(incoming_id and incoming_id == existing_id
                and incoming.get("entity_scope_id") == scope_id
                and existing.get("entity_scope_id") == scope_id
                and incoming.get("identity_origin") == existing.get("identity_origin") == "caller_declared")


def correction_entry(*, previous: Any, incoming: Any, scope_id: str,
                     observed_at: str) -> dict[str, Any]:
    before, after = dict(previous.metadata or {}), dict(incoming.metadata or {})
    declaration = after.get("declared_correction") or {}
    return {"transition_id": _digest(scope_id, str(previous.memory_id), str(incoming.memory_id)),
        "entity_id": after.get("entity_id", ""), "scope_id": scope_id,
        "previous_memory_id": str(previous.memory_id), "memory_id": str(incoming.memory_id),
        "old_value": str(previous.value), "new_value": str(incoming.value),
        "previous_source_record_id": str(before.get("source_record_id") or ""),
        "source_record_id": str(after.get("source_record_id") or ""),
        "effective_at": declaration.get("effective_at") or observed_at,
        "effective_at_origin": "caller_declared" if declaration.get("effective_at") else "source_timestamp",
        "recorded_at": datetime.now(timezone.utc).isoformat(), "reason": declaration.get("reason") or "reconciliation_replace_current",
        "verification": "unverified_source_assertion"}


def purge_identity_history(metadata: Mapping[str, Any], deleted_source_ids: set[str]) -> dict[str, Any]:
    """Remove correction snapshots derived from erased Sources, not the new claim."""
    result = dict(metadata or {})
    if "correction_history" in result:
        result["correction_history"] = [entry for entry in result["correction_history"]
            if not deleted_source_ids.intersection({str(entry.get("source_record_id") or ""),
                    str(entry.get("previous_source_record_id") or "")})]
    return result
