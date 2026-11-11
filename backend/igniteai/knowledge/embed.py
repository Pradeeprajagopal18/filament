"""Embeddings, always on your own servers.

* local: a small model run inside Ignite (fastembed, ONNX on the CPU). The model is downloaded
  once into the data directory, or read from model_path when the servers can't reach the
  internet (copy the model folder there).
* endpoint: an embedding server you run, with the OpenAI-style /v1/embeddings API: Ollama,
  Hugging Face text-embeddings-inference, vLLM, LocalAI...
* none: keyword search only.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from igniteai.config import get_settings

SECRET_REF = re.compile(r"^\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}$")


class EmbedError(RuntimeError):
    pass


class Embedder:
    name = "none"

    def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover - interface
        raise NotImplementedError


@lru_cache(maxsize=4)
def _fastembed(model: str, model_path: str | None):
    try:
        from fastembed import TextEmbedding
    except ImportError as exc:
        raise EmbedError("The local embedding model needs the fastembed package on the "
                         "server (pip install 'igniteai[knowledge]')") from exc
    cache = Path(get_settings().data_dir) / "models"
    kwargs = {"cache_dir": str(cache)}
    if model_path:
        kwargs["specific_model_path"] = model_path
    try:
        return TextEmbedding(model_name=model, **kwargs)
    except Exception as exc:  # noqa: BLE001 - download or load failures
        raise EmbedError(f"Couldn't load the embedding model {model}: {exc}. If the server "
                         "can't reach the internet, copy the model folder to the server and "
                         "set its path.") from exc


class LocalEmbedder(Embedder):
    def __init__(self, model: str, model_path: str | None = None):
        self.model, self.model_path, self.name = model, model_path, f"local:{model}"

    def embed(self, texts):
        m = _fastembed(self.model, self.model_path)
        return [[float(x) for x in v] for v in m.embed(texts, batch_size=32)]


class EndpointEmbedder(Embedder):
    def __init__(self, url: str, model: str, key_ref: str | None = None):
        self.url, self.model, self.name = url.rstrip("/"), model, f"endpoint:{model}"
        self.key = None
        if key_ref:
            m = SECRET_REF.match(key_ref)
            self.key = os.environ.get(m.group(1)) if m else None

    def embed(self, texts):
        import httpx

        url = self.url if self.url.endswith("/embeddings") else self.url + "/embeddings"
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        out: list[list[float]] = []
        for i in range(0, len(texts), 64):
            try:
                r = httpx.post(url, json={"model": self.model, "input": texts[i:i + 64]},
                               headers=headers, timeout=120)
                r.raise_for_status()
                data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
            except Exception as exc:  # noqa: BLE001
                raise EmbedError(f"The embedding server at {url} failed: {exc}") from exc
            out.extend(d["embedding"] for d in data)
        return out


def for_index(index) -> Embedder | None:
    if index is None or index.provider == "none":
        return None
    if index.provider == "local":
        return LocalEmbedder(index.model, index.model_path)
    if index.provider == "endpoint":
        if not index.endpoint_url:
            raise EmbedError("Set the embedding server URL")
        return EndpointEmbedder(index.endpoint_url, index.model, index.api_key_ref)
    raise EmbedError(f"Unknown embedding provider {index.provider}")
