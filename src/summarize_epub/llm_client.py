from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .config import Config


class LLMError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def retry_delay(value: str | None, attempt: int) -> float:
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError):
                pass
    return min(60.0, 2.0 ** attempt) + random.uniform(0, 1)


class LLMClient:
    def __init__(self, config: Config, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self.semaphore = asyncio.Semaphore(config.concurrency)
        self.http = httpx.AsyncClient(timeout=config.timeout, transport=transport, follow_redirects=False)
        self.log = logging.getLogger(__name__)
        self.input_tokens = 0
        self.output_tokens = 0
        self.max_token_field = "max_tokens"
        self.send_temperature = True

    async def __aenter__(self) -> LLMClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.http.aclose()

    async def complete(self, messages: list[dict[str, Any]], model: str | None = None) -> str:
        """Negotiate parameter differences from API errors, never provider names."""
        for attempt in range(8):
            payload: dict[str, Any] = {"model": model or self.config.model, "messages": messages,
                                      self.max_token_field: self.config.max_tokens}
            if self.send_temperature:
                payload["temperature"] = self.config.temperature
            try:
                async with self.semaphore:
                    response = await self.http.post(self.config.base_url.rstrip("/") + "/chat/completions",
                        headers={"Authorization": "Bearer " + self.config.api_key}, json=payload)
            except httpx.TransportError:
                if attempt == 7:
                    raise LLMError("LLM connection/timeout failure after retries") from None
                await asyncio.sleep(retry_delay(None, attempt))
                continue
            if response.status_code in {400, 422}:
                # Inspect but never log error bodies: some servers echo credentials/input.
                try:
                    detail = json.dumps(response.json()).lower()
                except ValueError:
                    detail = ""
                if "max_completion_tokens" in detail and self.max_token_field == "max_tokens":
                    self.max_token_field = "max_completion_tokens"
                    self.log.warning("API requested max_completion_tokens; adapting")
                    continue
                if "temperature" in detail and any(x in detail for x in ("unsupported", "not support", "only", "default")) and self.send_temperature:
                    self.send_temperature = False
                    self.log.warning("API does not accept configured temperature; using model default")
                    continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 7:
                    raise LLMError(f"LLM HTTP {response.status_code} after retries", response.status_code)
                delay = retry_delay(response.headers.get("Retry-After"), attempt)
                self.log.warning("Retrying LLM request: status=%s delay=%.1fs", response.status_code, delay)
                await asyncio.sleep(delay)
                continue
            if response.is_error:
                raise LLMError(f"LLM HTTP {response.status_code} (check endpoint, credentials, model and capabilities)", response.status_code)
            try:
                data = response.json()
                usage = data.get("usage", {})
                self.input_tokens += int(usage.get("prompt_tokens", 0))
                self.output_tokens += int(usage.get("completion_tokens", 0))
                choice = data["choices"][0]
                if choice.get("finish_reason") not in {None, "stop", "end_turn"}:
                    raise LLMError("Incomplete/filtered model response; increase --max-tokens if truncated")
                content = choice["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    raise LLMError("LLM returned empty or non-text content")
                return content
            except (ValueError, KeyError, IndexError, TypeError):
                raise LLMError("Malformed chat/completions response") from None
        raise LLMError("Parameter negotiation exhausted")
