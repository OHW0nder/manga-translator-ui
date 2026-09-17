from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import httpx


class ProviderError(RuntimeError):
    pass


@dataclass(slots=True)
class OpenAICompatibleConfig:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 180.0


class OpenAICompatibleClient:
    """Minimal OpenAI-compatible chat and embedding client."""

    def __init__(self, config: OpenAICompatibleConfig):
        self.config = config

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if response_format:
            payload["response_format"] = response_format
        data = await self._post("/chat/completions", payload)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                f"chat completion response is missing content: {data}"
            ) from exc
        if isinstance(content, list):
            content = "".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict)
            )
        return str(content)

    async def chat_json(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        try:
            content = await self.chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
        except ProviderError:
            # Some OpenAI-compatible servers reject response_format but still
            # produce valid JSON when the prompt requires it.
            content = await self.chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        return parse_json_object(content)

    async def embeddings(self, inputs: Iterable[str]) -> list[list[float]]:
        values = [str(value) for value in inputs]
        if not values:
            return []
        data = await self._post(
            "/embeddings",
            {"model": self.config.model, "input": values},
        )
        try:
            rows = sorted(
                data["data"],
                key=lambda item: int(item.get("index", 0)),
            )
            return [
                [float(value) for value in row["embedding"]]
                for row in rows
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(
                f"embedding response is invalid: {data}"
            ) from exc

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        url = f"{self.config.base_url.rstrip('/')}{path}"
        try:
            async with httpx.AsyncClient(
                timeout=self.config.timeout_seconds
            ) as client:
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            detail = ""
            if getattr(exc, "response", None) is not None:
                detail = exc.response.text[:1000]
            raise ProviderError(
                f"OpenAI-compatible request failed for {url}: {exc} {detail}"
            ) from exc
        try:
            value = response.json()
        except json.JSONDecodeError as exc:
            raise ProviderError("provider returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ProviderError("provider response must be a JSON object")
        return value


def image_data_uri(path: str | Path) -> str:
    image_path = Path(path)
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".png": "image/png",
    }.get(image_path.suffix.casefold(), "application/octet-stream")
    payload = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{payload}"


def parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderError(
            f"model did not return valid JSON: {content[:1000]}"
        ) from exc
    if not isinstance(value, dict):
        raise ProviderError("model JSON response must be an object")
    return value


async def gather_limited(
    coroutines: list[Any],
    limit: int,
) -> list[Any]:
    semaphore = asyncio.Semaphore(max(1, limit))

    async def run(coroutine):
        async with semaphore:
            return await coroutine

    return await asyncio.gather(*(run(item) for item in coroutines))
