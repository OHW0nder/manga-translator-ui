from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class ModelManagerError(RuntimeError):
    pass


@dataclass(slots=True)
class ExternalModelManagerClient:
    """Control facade for an already-running legacy Qwen container."""

    base_url: str
    timeout_seconds: int = 10

    async def status(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._health)

    async def load(self, model_id: str) -> dict[str, Any]:
        return await self.status()

    async def unload(self, model_id: str | None = None) -> dict[str, Any]:
        # The external container has no safe control API. Keep it resident and
        # let the Web runtime unload OCR/inpaint before calling it.
        return await self.status()

    async def ensure_loaded(self, model_id: str) -> dict[str, Any]:
        return await self.status()

    def _health(self) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}/health",
            headers={"Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModelManagerError(
                f"external Qwen container is unavailable at {self.base_url}: {exc}"
            ) from exc
        return {
            "available": True,
            "loaded_model": "external-qwen",
            "state": "loaded",
            "external": True,
        }


@dataclass(slots=True)
class ModelManagerClient:
    base_url: str
    token: str
    timeout_seconds: int = 300

    async def status(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._request, "GET", "/status")

    async def load(self, model_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._request,
            "POST",
            f"/models/{model_id}/load",
            {},
        )

    async def unload(self, model_id: str | None = None) -> dict[str, Any]:
        path = "/models/unload" if model_id is None else f"/models/{model_id}/unload"
        return await asyncio.to_thread(self._request, "POST", path, {})

    async def ensure_loaded(self, model_id: str) -> dict[str, Any]:
        status = await self.status()
        if status.get("loaded_model") == model_id:
            return status
        return await self.load(model_id)

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None
        headers = {
            "Accept": "application/json",
            "X-Internal-Token": self.token,
        }
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ModelManagerError(
                f"model manager returned HTTP {exc.code}: {detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModelManagerError(
                f"model manager is unavailable at {self.base_url}: {exc}"
            ) from exc
        if not body:
            return {}
        try:
            value = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ModelManagerError(
                f"model manager returned invalid JSON: {body[:500]}"
            ) from exc
        if not isinstance(value, dict):
            raise ModelManagerError("model manager response must be an object")
        return value


class ModelLease:
    """Load an allowlisted model for one pipeline stage, then release it."""

    def __init__(
        self,
        client: ModelManagerClient,
        model_id: str,
        *,
        unload_on_exit: bool = True,
    ):
        self.client = client
        self.model_id = model_id
        self.unload_on_exit = unload_on_exit

    async def __aenter__(self) -> "ModelLease":
        await self.client.ensure_loaded(self.model_id)
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self.unload_on_exit:
            try:
                await self.client.unload(self.model_id)
            except ModelManagerError:
                if exc_type is None:
                    raise


async def unload_web_models() -> None:
    """Release models cached in the Web container before loading Qwen."""

    from manga_translator.server.core import task_manager

    await asyncio.to_thread(task_manager.reset_global_translator)
