"""Embedding providers for local-brain RAG."""

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional

import httpx
import numpy as np

from local_brain import config


class Embedder(ABC):
    """Abstract embedding provider."""

    @abstractmethod
    def embed(self, texts: List[str]) -> List[List[float]]:
        """Return a list of embedding vectors for the input texts."""
        raise NotImplementedError

    @property
    @abstractmethod
    def dimension(self) -> int:
        raise NotImplementedError

    @abstractmethod
    def is_available(self) -> bool:
        raise NotImplementedError


class FastEmbedEmbedder(Embedder):
    """Local embeddings via fastembed (ONNX runtime, no PyTorch)."""

    def __init__(self, model_name: Optional[str] = None, cache_dir: Optional[str] = None):
        self.model_name = model_name or config.get(
            "embedding.model", "sentence-transformers/all-MiniLM-L6-v2"
        )
        self.cache_dir = cache_dir or config.get(
            "embedding.cache_dir", str(Path.home() / ".cache" / "brain" / "embeddings")
        )
        os.makedirs(self.cache_dir, exist_ok=True)
        os.chmod(self.cache_dir, 0o700)
        self._model = None

    def _load(self):
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(
                model_name=self.model_name,
                cache_dir=self.cache_dir,
            )
        return self._model

    def embed(self, texts: List[str]) -> List[List[float]]:
        model = self._load()
        # Normalize to unit vectors for cosine similarity
        vectors = []
        for vec in model.embed(texts):
            arr = np.array(vec)
            norm = np.linalg.norm(arr)
            if norm > 0:
                arr = arr / norm
            vectors.append(arr.tolist())
        return vectors

    @property
    def dimension(self) -> int:
        # Trigger lazy load to get dimension
        model = self._load()
        # fastembed exposes dim via query_embedding_shape or sample
        sample = self.embed(["test"])[0]
        return len(sample)

    def is_available(self) -> bool:
        try:
            self._load()
            return True
        except Exception:
            return False


class OllamaEmbedder(Embedder):
    """Embeddings via Ollama API."""

    def __init__(self, url: Optional[str] = None, model: Optional[str] = None):
        self.url = (url or config.get("ollama.url", "http://localhost:11434")).rstrip("/")
        self.model = model or config.get("embedding.model", "nomic-embed-text")

    def is_available(self) -> bool:
        try:
            resp = httpx.get(f"{self.url}/api/tags", timeout=2.0)
            return resp.status_code == 200
        except Exception:
            return False

    def embed(self, texts: List[str]) -> List[List[float]]:
        vectors = []
        for text in texts:
            resp = httpx.post(
                f"{self.url}/api/embeddings",
                json={"model": self.model, "prompt": text},
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()
            arr = np.array(data["embedding"])
            norm = np.linalg.norm(arr)
            if norm > 0:
                arr = arr / norm
            vectors.append(arr.tolist())
        return vectors

    @property
    def dimension(self) -> int:
        sample = self.embed(["test"])
        return len(sample[0])


def get_embedder() -> Embedder:
    """Return the configured embedder, falling back from Ollama to fastembed."""
    provider = config.get("embedding.provider", "fastembed").lower()

    if provider == "ollama":
        embedder = OllamaEmbedder()
        if embedder.is_available():
            return embedder
        # Fall back to fastembed
        return FastEmbedEmbedder()

    if provider == "fastembed":
        return FastEmbedEmbedder()

    # Auto-detect: try Ollama if configured model looks like an Ollama model,
    # otherwise use fastembed.
    model = config.get("embedding.model", "")
    if "ollama" in provider or model in ("nomic-embed-text", "mxbai-embed-large"):
        ollama = OllamaEmbedder(model=model)
        if ollama.is_available():
            return ollama
    return FastEmbedEmbedder()


_EMBEDDER: Optional[Embedder] = None


def embed(texts: List[str]) -> List[List[float]]:
    """Global embedding helper using the configured provider."""
    global _EMBEDDER
    if _EMBEDDER is None:
        _EMBEDDER = get_embedder()
    return _EMBEDDER.embed(texts)


def current_dimension() -> int:
    global _EMBEDDER
    if _EMBEDDER is None:
        _EMBEDDER = get_embedder()
    return _EMBEDDER.dimension


def reset_embedder() -> None:
    global _EMBEDDER
    _EMBEDDER = None
