
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
MODEL_NAME = os.getenv("OLLAMA_MODEL", "qwen3-personal:latest")
EMBED_MODEL_NAME = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 600


def check_ollama():
    try:
        response = requests.get(
            f"{OLLAMA_URL}/api/tags",
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
        )
        response.raise_for_status()

        models = response.json().get("models", [])
        available = [model["name"] for model in models]

        return {
            "connected": True,
            "model": MODEL_NAME,
            "model_available": MODEL_NAME in available,
            "embedding_model": EMBED_MODEL_NAME,
            "embedding_model_available": any(
                name == EMBED_MODEL_NAME
                or name == f"{EMBED_MODEL_NAME}:latest"
                for name in available
            ),
            "available_models": available
        }

    except requests.RequestException as error:
        return {
            "connected": False,
            "error": str(error)
        }


def ask_ollama(prompt, timeout=None, options=None):
    request_options = options or {}

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "stream": False,
        "options": request_options
    }

    try:
        response = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json=payload,
            timeout=(
                CONNECT_TIMEOUT,
                timeout if timeout is not None else READ_TIMEOUT
            )
        )

        response.raise_for_status()
        result = response.json()

        content = result.get("message", {}).get("content", "")

        if not content.strip():
            raise RuntimeError("Ollama returned an empty response.")

        return content

    except requests.exceptions.Timeout as error:
        raise RuntimeError(
            f"Ollama timed out after "
            f"{timeout or READ_TIMEOUT} seconds."
        ) from error

    except requests.exceptions.RequestException as error:
        raise RuntimeError(
            f"Could not communicate with Ollama: {error}"
        ) from error


def embed_texts(texts, model=None):
    """Return local Ollama embeddings for a list of text strings."""
    values = [str(text) for text in texts]
    if not values or any(not value.strip() for value in values):
        raise ValueError("Embedding input must contain non-empty text.")

    embedding_model = model or EMBED_MODEL_NAME
    try:
        response = requests.post(
            f"{OLLAMA_URL}/api/embed",
            json={"model": embedding_model, "input": values},
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
        if response.status_code == 404:
            # Compatibility with older Ollama versions that expose one input per call.
            vectors = []
            for value in values:
                old_response = requests.post(
                    f"{OLLAMA_URL}/api/embeddings",
                    json={"model": embedding_model, "prompt": value},
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                )
                old_response.raise_for_status()
                vectors.append(old_response.json().get("embedding"))
            return vectors

        response.raise_for_status()
        result = response.json()
        vectors = result.get("embeddings")
        if vectors is None and result.get("embedding") is not None:
            vectors = [result["embedding"]]
        if not isinstance(vectors, list) or len(vectors) != len(values):
            raise RuntimeError("Ollama returned an unexpected embedding response.")
        if any(not isinstance(vector, list) or not vector for vector in vectors):
            raise RuntimeError("Ollama returned an empty embedding vector.")
        return vectors
    except requests.exceptions.Timeout as error:
        raise RuntimeError(
            f"Ollama embedding request timed out after {READ_TIMEOUT} seconds."
        ) from error
    except requests.exceptions.RequestException as error:
        detail = error.response.text[:400] if error.response is not None else str(error)
        raise RuntimeError(f"Could not create local embeddings: {detail}") from error
