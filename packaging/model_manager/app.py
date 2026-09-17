from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException


CONTROL_PORT = int(os.environ.get("MT_MODEL_CONTROL_PORT", "8090"))
LLAMA_PORT = int(os.environ.get("MT_LLAMA_PORT", "8080"))
INTERNAL_TOKEN = os.environ.get(
    "MT_MODEL_MANAGER_TOKEN",
    "manga-translator-internal",
)
MODEL_DIR = Path(os.environ.get("MT_MODEL_DIR", "/models"))

MODEL_DEFINITIONS: dict[str, dict[str, Any]] = {
    "qwen35-4b-vl": {
        "kind": "chat-vl",
        "model": os.environ.get(
            "MT_QWEN_MODEL_PATH",
            str(
                MODEL_DIR
                / "Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf"
            ),
        ),
        "mmproj": os.environ.get(
            "MT_QWEN_MMPROJ_PATH",
            str(
                MODEL_DIR
                / "mmproj-Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-BF16.gguf"
            ),
        ),
        "alias": "local-qwen35-4b-vl",
        "context_size": int(
            os.environ.get("MT_QWEN_CONTEXT_SIZE", "16384")
        ),
        "parallel": 1,
    },
    "local-embedding": {
        "kind": "embedding",
        "model": os.environ.get(
            "MT_EMBEDDING_MODEL_PATH",
            str(MODEL_DIR / "bge-m3-Q4_K_M.gguf"),
        ),
        "alias": "local-embedding",
        "context_size": 8192,
        "parallel": 1,
    },
}


class ModelSupervisor:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._loaded_model: str | None = None
        self._last_error: str | None = None

    def definitions(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for model_id, definition in MODEL_DEFINITIONS.items():
            item = dict(definition)
            item["available"] = Path(definition["model"]).is_file()
            if definition.get("mmproj"):
                item["available"] = item["available"] and Path(
                    definition["mmproj"]
                ).is_file()
            item.pop("model", None)
            item.pop("mmproj", None)
            result[model_id] = item
        return result

    async def status(self) -> dict[str, Any]:
        process_running = (
            self._process is not None and self._process.poll() is None
        )
        return {
            "available": True,
            "loaded_model": self._loaded_model if process_running else None,
            "state": "loaded" if process_running else "unloaded",
            "pid": self._process.pid if process_running else None,
            "last_error": self._last_error,
            "models": self.definitions(),
        }

    async def load(self, model_id: str) -> dict[str, Any]:
        if model_id not in MODEL_DEFINITIONS:
            raise KeyError(model_id)
        async with self._lock:
            definition = MODEL_DEFINITIONS[model_id]
            self._validate_files(definition)
            if self._loaded_model == model_id and self._is_running():
                return await self.status()
            await self._stop_locked()
            self._last_error = None
            command = self._build_command(model_id, definition)
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            self._loaded_model = model_id
            try:
                await self._wait_until_ready()
            except Exception as exc:
                self._last_error = str(exc)
                await self._stop_locked()
                raise
            return await self.status()

    async def unload(self, model_id: str | None = None) -> dict[str, Any]:
        async with self._lock:
            if model_id and self._loaded_model and model_id != self._loaded_model:
                return await self.status()
            await self._stop_locked()
            return await self.status()

    async def shutdown(self) -> None:
        async with self._lock:
            await self._stop_locked()

    def _build_command(
        self,
        model_id: str,
        definition: dict[str, Any],
    ) -> list[str]:
        command = [
            "/app/llama-server",
            "--model",
            str(definition["model"]),
            "--alias",
            str(definition["alias"]),
            "--host",
            "0.0.0.0",
            "--port",
            str(LLAMA_PORT),
            "--ctx-size",
            str(definition.get("context_size", 32768)),
            "--parallel",
            str(definition.get("parallel", 1)),
        ]
        if definition["kind"] == "chat-vl":
            command.extend(
                [
                    "--mmproj",
                    str(definition["mmproj"]),
                    "--cache-type-k",
                    "q8_0",
                    "--cache-type-v",
                    "q8_0",
                    "--load-mode",
                    "none",
                    "--flash-attn",
                    "on",
                    "--n-gpu-layers",
                    "all",
                    "--jinja",
                    "--reasoning",
                    "off",
                ]
            )
        elif definition["kind"] == "embedding":
            command.extend(
                [
                    "--embedding",
                    "--pooling",
                    "mean",
                    "--n-gpu-layers",
                    "all",
                ]
            )
        return command

    async def _wait_until_ready(self) -> None:
        if self._process is None:
            raise RuntimeError("model process was not started")
        deadline = asyncio.get_running_loop().time() + 240
        url = f"http://127.0.0.1:{LLAMA_PORT}/health"
        async with httpx.AsyncClient(timeout=2.0) as client:
            while asyncio.get_running_loop().time() < deadline:
                return_code = self._process.poll()
                if return_code is not None:
                    output = self._drain_output()
                    raise RuntimeError(
                        f"model process exited with code {return_code}: {output}"
                    )
                try:
                    response = await client.get(url)
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)
        raise TimeoutError("model did not become ready within 240 seconds")

    async def _stop_locked(self) -> None:
        process = self._process
        self._process = None
        self._loaded_model = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            await asyncio.to_thread(process.wait, 20)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait, 10)

    def _is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _validate_files(self, definition: dict[str, Any]) -> None:
        model_path = Path(definition["model"])
        if not model_path.is_file():
            raise FileNotFoundError(f"model file is missing: {model_path}")
        mmproj = definition.get("mmproj")
        if mmproj and not Path(mmproj).is_file():
            raise FileNotFoundError(f"mmproj file is missing: {mmproj}")

    def _drain_output(self) -> str:
        if self._process is None or self._process.stdout is None:
            return ""
        lines: list[str] = []
        while len(lines) < 60:
            line = self._process.stdout.readline()
            if not line:
                break
            lines.append(line.rstrip())
        return "\n".join(lines)


supervisor = ModelSupervisor()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await supervisor.shutdown()


app = FastAPI(
    title="Manga Translator Model Manager",
    version="1.0",
    lifespan=lifespan,
)


def require_internal_token(
    x_internal_token: str = Header(default=""),
) -> None:
    if x_internal_token != INTERNAL_TOKEN:
        raise HTTPException(401, detail="invalid internal token")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok"}


@app.get("/status", dependencies=[Depends(require_internal_token)])
async def status() -> dict[str, Any]:
    return await supervisor.status()


@app.post("/models/{model_id}/load", dependencies=[Depends(require_internal_token)])
async def load_model(model_id: str) -> dict[str, Any]:
    try:
        return await supervisor.load(model_id)
    except KeyError as exc:
        raise HTTPException(404, detail="model is not allowlisted") from exc
    except FileNotFoundError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, detail=str(exc)) from exc


@app.post("/models/{model_id}/unload", dependencies=[Depends(require_internal_token)])
async def unload_model(model_id: str) -> dict[str, Any]:
    if model_id not in MODEL_DEFINITIONS:
        raise HTTPException(404, detail="model is not allowlisted")
    return await supervisor.unload(model_id)


@app.post("/models/unload", dependencies=[Depends(require_internal_token)])
async def unload_all_models() -> dict[str, Any]:
    return await supervisor.unload()


def _handle_signal(signum, frame) -> None:
    del signum, frame
    if supervisor._process and supervisor._process.poll() is None:
        supervisor._process.terminate()


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=CONTROL_PORT)
