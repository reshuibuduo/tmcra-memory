from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from .http_transport import open_request

from .core.v4_batch_writer import (
    BATCH_SYSTEM_PROMPT,
    ProductWriterError,
    batch_response_json_schema,
)


UsageSink = Callable[[Mapping[str, Any]], None]


RECONCILIATION_SYSTEM_PROMPT = """You bind one new cited assertion to a compact
controller-retrieved candidate-slot set. Use only supplied source quotes and
candidate IDs. Return exactly one JSON object and no prose:
{"slot_decision":"bind_existing|keep_proposed|quarantine",
"selected_memory_id":"candidate ID or empty string",
"decision":"insert|merge_support|replace_current|keep_parallel|challenge|quarantine"}.
bind_existing means the new assertion is the same real-world memory slot as the
selected candidate. keep_proposed means none of the candidates is the same slot
and requires decision=insert with an empty selected_memory_id. quarantine means
unsafe or ungrounded and requires decision=quarantine. For a bound slot:
merge_support means the same atomic fact with additional evidence;
replace_current is a clear update; keep_parallel means simultaneous values; and
challenge means conflicting evidence without a winner. Never select an ID
outside the supplied candidates."""


class OpenAICompatibleClient:
    """Small OpenAI Chat Completions client with local usage accounting.

    The API key is held only in memory. Request/response bodies are never written
    to the usage ledger; the caller receives hashes and token counters instead.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        timeout: float = 180.0,
        max_tokens: int = 8192,
        response_format: str = "json_object",
        usage_sink: UsageSink | None = None,
        provider: str = "openai-compatible",
    ) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.model = str(model or "").strip()
        self.api_key = str(api_key or "").strip()
        self.timeout = float(timeout)
        self.max_tokens = int(max_tokens)
        self.response_format = str(response_format or "json_object").strip().lower()
        self.usage_sink = usage_sink
        self.provider = str(provider or "openai-compatible").strip()
        self.user_id = ""
        if not self.base_url or not self.model:
            raise ProductWriterError("generation base URL and model are required")
        if self.timeout <= 0 or self.max_tokens <= 0:
            raise ProductWriterError("generation timeout and max_tokens must be positive")
        if self.response_format not in {"json_object", "json_schema", "none"}:
            raise ProductWriterError(
                "response_format must be json_object, json_schema, or none"
            )

    @staticmethod
    def _usage(value: Any) -> dict[str, Any]:
        payload = dict(value or {}) if isinstance(value, Mapping) else {}

        def count(*names: str) -> int:
            for name in names:
                raw = payload.get(name)
                if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                    return max(0, int(raw))
            return 0

        prompt = count("prompt_tokens", "input_tokens")
        completion = count("completion_tokens", "output_tokens")
        total = count("total_tokens") or prompt + completion
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "prompt_cache_hit_tokens": count(
                "prompt_cache_hit_tokens", "cache_read_input_tokens", "cached_tokens"
            ),
            "prompt_cache_miss_tokens": count(
                "prompt_cache_miss_tokens", "cache_miss_input_tokens"
            ),
            "usage_reported": bool(payload),
        }

    def _request_payload(
        self,
        *,
        system_prompt: str,
        payload: Mapping[str, Any],
        response_schema: Mapping[str, Any] | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        request_payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
            ],
            "temperature": 0,
            "max_tokens": int(max_tokens or self.max_tokens),
        }
        if self.response_format == "json_schema" and response_schema is not None:
            request_payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "tmcra_structured_response",
                    "strict": True,
                    "schema": dict(response_schema),
                },
            }
        elif self.response_format in {"json_object", "json_schema"}:
            request_payload["response_format"] = {"type": "json_object"}
        if self.user_id:
            request_payload["user"] = self.user_id
        return request_payload

    def complete_json(
        self,
        *,
        system_prompt: str,
        payload: Mapping[str, Any],
        stage: str,
        response_schema: Mapping[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> tuple[str, dict[str, Any]]:
        request_payload = self._request_payload(
            system_prompt=system_prompt,
            payload=payload,
            response_schema=response_schema,
            max_tokens=max_tokens,
        )
        wire = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
        request_hash = hashlib.sha256(wire).hexdigest()
        call_id = "local_" + uuid.uuid4().hex
        started = time.time()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=wire,
            headers=headers,
            method="POST",
        )
        try:
            with open_request(request, timeout=self.timeout) as response:
                status = int(response.getcode())
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # Provider bodies can echo request fragments. Keep them out of API
            # errors and local logs; the status code is enough for routing.
            raise ProductWriterError(f"{stage} generation HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProductWriterError(f"{stage} generation request failed: {exc}") from exc
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProductWriterError(f"{stage} returned invalid HTTP JSON") from exc
        choices = body.get("choices") if isinstance(body, Mapping) else None
        if not isinstance(choices, list) or not choices:
            raise ProductWriterError(f"{stage} returned no completion choice")
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, Mapping) else None
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str) or not content.strip():
            raise ProductWriterError(f"{stage} returned empty completion content")
        usage = self._usage(body.get("usage") if isinstance(body, Mapping) else None)
        metadata = {
            "physical_call_id": call_id,
            "physical_api_call": True,
            "physical_api_calls": 1,
            "stage": stage,
            "provider": self.provider,
            "model": self.model,
            "status": "completed",
            "http_status": status,
            "request_sha256": request_hash,
            "response_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "latency_seconds": round(time.time() - started, 6),
            **usage,
            "usage": dict(usage),
        }
        if self.usage_sink is not None:
            self.usage_sink(metadata)
        return content, metadata

    def complete(self, payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        schema = (
            batch_response_json_schema(payload)
            if self.response_format == "json_schema"
            else None
        )
        return self.complete_json(
            system_prompt=BATCH_SYSTEM_PROMPT,
            payload=payload,
            stage="memory_writer",
            response_schema=schema,
        )

    def reconcile(self, payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        return self.complete_json(
            system_prompt=RECONCILIATION_SYSTEM_PROMPT,
            payload=payload,
            stage="memory_reconciliation",
            max_tokens=min(self.max_tokens, 1024),
        )


__all__ = ["OpenAICompatibleClient", "RECONCILIATION_SYSTEM_PROMPT"]
