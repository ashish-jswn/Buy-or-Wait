"""The shared primary-model client: one place that talks to the provider SDK.

Every call goes through ``extraction.usage.record_call`` (logged per attempt, failures
included) and a retry-with-backoff loop. Callers ask for JSON matching a schema and get a
parsed dict back; they never see the SDK. Tests inject a fake ``sdk_client``.
"""

import json
import time
from typing import Any, Optional

from config import (
    MODEL_MAX_ATTEMPTS,
    MODEL_MAX_OUTPUT_TOKENS,
    MODEL_REASONING_EFFORT,
    MODEL_RETRY_BASE_SECONDS,
    MODEL_RETRY_MAX_SECONDS,
    MODEL_TIMEOUT_SECONDS,
    PRIMARY_API_KEY,
    PRIMARY_API_VERSION,
    PRIMARY_ENDPOINT,
    PRIMARY_MODEL,
    PRIMARY_PROVIDER,
    missing_primary_config,
)
from extraction.usage import ROLE_PRIMARY, UsageLog, record_call


class ModelCallError(RuntimeError):
    """Raised when every attempt at a model call has failed."""


def build_primary_sdk_client() -> Any:
    """Construct the provider SDK client from config. Raises if config is incomplete.

    An endpoint ending in /openai/v1 is Azure's v1 surface, reached through the plain
    OpenAI client; anything else uses AzureOpenAI.
    """
    missing = missing_primary_config()
    if missing:
        raise ModelCallError(f"primary model not configured; missing {', '.join(missing)}")
    if PRIMARY_ENDPOINT.rstrip("/").endswith("/openai/v1"):
        from openai import OpenAI

        return OpenAI(base_url=PRIMARY_ENDPOINT, api_key=PRIMARY_API_KEY, timeout=MODEL_TIMEOUT_SECONDS)
    from openai import AzureOpenAI

    return AzureOpenAI(
        azure_endpoint=PRIMARY_ENDPOINT,
        api_key=PRIMARY_API_KEY,
        api_version=PRIMARY_API_VERSION,
        timeout=MODEL_TIMEOUT_SECONDS,
    )


def _usage_of(response: Any) -> tuple[int, int]:
    """(input_tokens, output_tokens) from a chat completion response."""
    usage = getattr(response, "usage", None)
    return (getattr(usage, "prompt_tokens", 0) or 0, getattr(usage, "completion_tokens", 0) or 0)


class PrimaryClient:
    """Structured-JSON calls to the primary model, retried and usage-logged."""

    def __init__(
        self,
        sdk_client: Any = None,
        model: str = PRIMARY_MODEL,
        log: Optional[UsageLog] = None,
        sleep=time.sleep,
    ) -> None:
        """`sdk_client` defaults to the configured provider; tests pass a fake."""
        self._sdk = sdk_client
        self.model = model
        self._log = log
        self._sleep = sleep

    @property
    def sdk(self) -> Any:
        """The provider SDK client, built on first use."""
        if self._sdk is None:
            self._sdk = build_primary_sdk_client()
        return self._sdk

    def complete_json(
        self,
        system: str,
        user_content: Any,
        schema_name: str,
        schema: dict,
        tag: str,
        request_id: Optional[str] = None,
    ) -> dict:
        """One chat completion constrained to `schema`; returns the parsed object.

        `user_content` is a string, or an OpenAI content-parts list for vision. Retries
        up to MODEL_MAX_ATTEMPTS with exponential backoff on any error, including
        unparseable output. Raises ModelCallError when every attempt fails.
        """
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": schema, "strict": True},
        }
        last_error: Optional[Exception] = None
        for attempt in range(1, MODEL_MAX_ATTEMPTS + 1):
            try:
                response = record_call(
                    lambda: self.sdk.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        response_format=response_format,
                        max_completion_tokens=MODEL_MAX_OUTPUT_TOKENS,
                        reasoning_effort=MODEL_REASONING_EFFORT,
                    ),
                    _usage_of,
                    role=ROLE_PRIMARY,
                    provider=PRIMARY_PROVIDER,
                    model=self.model,
                    tag=tag,
                    request_id=request_id,
                    attempts=attempt,
                    log=self._log,
                )
                text = response.choices[0].message.content or ""
                return json.loads(text)
            except Exception as error:  # noqa: BLE001 - retry policy decides
                last_error = error
                if attempt < MODEL_MAX_ATTEMPTS:
                    self._sleep(min(MODEL_RETRY_BASE_SECONDS * 2 ** (attempt - 1), MODEL_RETRY_MAX_SECONDS))
        raise ModelCallError(f"{tag}: all {MODEL_MAX_ATTEMPTS} attempts failed: {last_error}")
