// Origin is a bounded declaration, never proof of authentication. No body-text
// classifier: users can legitimately quote suggestions, prompts and automation.
const KINDS = new Set(['human_authored', 'human_adopted', 'app_generated', 'automation', 'assistant_generated', 'tool_generated', 'unknown']);
const METHODS = new Set(['host_event_metadata', 'explicit_api_declaration', 'legacy_role_only']);
const STRENGTHS = new Set(['host_declared', 'caller_declared', 'unknown']);
const AUTO = new Set(['app_generated', 'automation', 'assistant_generated', 'tool_generated']);

export function classifyPromptOrigin(input = {}) {
  let origin = input.origin_kind ?? input.message_origin ?? input.origin?.kind ?? input.metadata?.origin_kind;
  if (input.is_automated === true || input.automation_id) origin = 'automation';
  else if (input.is_synthetic === true || input.is_generated === true) origin = 'app_generated';
  const explicit = KINDS.has(origin) && origin !== 'unknown';
  return {
    origin_kind: explicit ? origin : 'unknown',
    capture_method: explicit ? 'host_event_metadata' : 'legacy_role_only',
    provenance_strength: explicit ? 'host_declared' : 'unknown',
  };
}

export function permitsPromptCapture(origin) {
  return !AUTO.has(origin?.origin_kind);
}

export function normalizeQueuedOrigin(metadata = {}, role = 'user') {
  // Preserve legacy actor bytes. Runtime interprets absent origin as unknown;
  // adding it here would change already-persisted immutable message identities.
  if (!['origin_kind', 'capture_method', 'provenance_strength'].some(k => k in metadata)) return { ...metadata };
  const origin = KINDS.has(metadata.origin_kind) ? metadata.origin_kind : 'unknown';
  const humanConflict = role !== 'user' && ['human_authored', 'human_adopted'].includes(origin);
  return { ...metadata,
    origin_kind: humanConflict ? 'unknown' : origin,
    capture_method: METHODS.has(metadata.capture_method) ? metadata.capture_method : 'legacy_role_only',
    provenance_strength: origin !== 'unknown' && !humanConflict && STRENGTHS.has(metadata.provenance_strength)
      ? metadata.provenance_strength : 'unknown',
  };
}
