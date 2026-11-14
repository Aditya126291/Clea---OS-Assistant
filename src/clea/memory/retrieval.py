"""3-layer hybrid retrieval engine for Clea's conversations and long-term memory.

Layers:
1. Full-history coverage (zero recency cap across transcripts, episodes, and facts).
2. Multi-term token-ranked lexical scoring with stopword removal and weighted fields
   (Title +5.0, Episode summary +3.5, Durable Fact +3.0, Raw Turn +1.0).
3. Compact float32 semantic vector embeddings persisted for Episodes & Durable Facts
   with cosine similarity blending (0.6 * semantic + 0.4 * lexical) and zero-breakage
   graceful fallback to pure lexical scoring when embeddings are unavailable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import NonCallableMock
from uuid import uuid4

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_EMBEDDING_MODEL = "gemini-embedding-001"
DEFAULT_EMBEDDING_DIMS = 768
DEFAULT_OLLAMA_EMBED_MODELS = ("nomic-embed-text", "embeddinggemma")
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"

WEIGHT_TITLE = 5.0
WEIGHT_EPISODE = 3.5
WEIGHT_FACT = 3.0
WEIGHT_TURN = 1.0

# Conversational stopwords stripped from descriptive voice/text queries.
STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "about",
        "ago",
        "all",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "before",
        "but",
        "by",
        "can",
        "chat",
        "chats",
        "conversation",
        "conversations",
        "could",
        "did",
        "discussed",
        "do",
        "does",
        "earlier",
        "find",
        "for",
        "from",
        "get",
        "go",
        "got",
        "had",
        "has",
        "have",
        "here",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "last",
        "look",
        "me",
        "mentioned",
        "my",
        "not",
        "of",
        "on",
        "onto",
        "or",
        "other",
        "our",
        "out",
        "past",
        "previous",
        "recall",
        "remember",
        "said",
        "search",
        "should",
        "show",
        "so",
        "some",
        "talked",
        "tell",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "they",
        "this",
        "thread",
        "threads",
        "to",
        "up",
        "was",
        "we",
        "went",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "with",
        "would",
        "you",
        "your",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9_]+")
EmbedFn = Callable[[str], Sequence[float] | np.ndarray | None]


def text_hash(text: str) -> str:
    """Compute a deterministic SHA-256 digest of normalized text."""
    normalized = " ".join((text or "").split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def tokenize_all(text: str) -> list[str]:
    """Extract lowercased alphanumeric/underscore tokens from text."""
    if not text:
        return []
    return _TOKEN_RE.findall(text.lower())


def tokenize_query(query: str) -> list[str]:
    """Tokenize a descriptive query with punctuation stripping and stopword removal.

    Preserves order and deduplicates tokens. Falls back to raw non-trivial tokens if
    every token in the query is a stopword.
    """
    raw_tokens = tokenize_all(query)
    if not raw_tokens:
        return []

    filtered: list[str] = []
    seen: set[str] = set()
    for tok in raw_tokens:
        if tok in STOPWORDS:
            continue
        if len(tok) < 2 and not tok.isdigit():
            continue
        if tok not in seen:
            seen.add(tok)
            filtered.append(tok)

    if filtered:
        return filtered

    # Fallback when the entire query consisted of stopwords or single chars
    fallback: list[str] = []
    seen.clear()
    for tok in raw_tokens:
        if len(tok) >= 2 or tok.isdigit():
            if tok not in seen:
                seen.add(tok)
                fallback.append(tok)
    if fallback:
        return fallback

    for tok in raw_tokens:
        if tok not in seen:
            seen.add(tok)
            fallback.append(tok)
    return fallback


def _simple_stem(token: str) -> str:
    """Lightweight suffix stripper for common English inflections."""
    if len(token) < 4:
        return token
    for suffix in ("ing", "ed", "es", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)]
    return token


def token_match_score(token: str, text_lower: str, text_tokens: set[str]) -> float:
    """Score how well a single query token matches target text in [0.0, 1.0]."""
    if not token or not text_lower:
        return 0.0
    if token in text_tokens or token in text_lower:
        return 1.0
    if len(token) >= 4:
        q_stem = _simple_stem(token)
        if len(q_stem) >= 3:
            for candidate in text_tokens:
                if candidate.startswith(q_stem) or _simple_stem(candidate) == q_stem:
                    return 0.85
    return 0.0


def field_coverage(query_tokens: list[str], query_lower: str, text: str) -> float:
    """Compute query token coverage in [0.0, 1.0] for a single text field."""
    if not text:
        return 0.0
    text_lower = text.lower()
    if not query_tokens:
        return 1.0 if (query_lower and query_lower in text_lower) else 0.0

    text_tokens = set(tokenize_all(text_lower))
    total = sum(token_match_score(tok, text_lower, text_tokens) for tok in query_tokens)
    cov = total / len(query_tokens)
    if len(query_lower) >= 3 and query_lower in text_lower:
        cov = min(1.0, cov + 0.15)
    return min(1.0, cov)


def _overall_query_coverage(
    query_tokens: list[str],
    query_lower: str,
    texts: Sequence[str],
) -> float:
    """Compute combined coverage of query tokens across all fields of a candidate."""
    non_empty = [t for t in texts if t]
    if not non_empty:
        return 0.0
    combined_lower = "\n".join(t.lower() for t in non_empty)
    if not query_tokens:
        return 1.0 if (query_lower and query_lower in combined_lower) else 0.0

    combined_tokens = set(tokenize_all(combined_lower))
    total = sum(token_match_score(tok, combined_lower, combined_tokens) for tok in query_tokens)
    return min(1.0, total / len(query_tokens))


def score_conversation_lexical(
    *,
    query_tokens: list[str],
    query_lower: str,
    title: str,
    episode_summary: str,
    fact_cov: float,
    max_turn_cov: float,
    turn_texts_for_cov: Sequence[str] = (),
    fact_texts_for_cov: Sequence[str] = (),
) -> tuple[float, float, dict[str, float]]:
    """Score a conversation lexically across Title, Episode, Fact, and Turn fields.

    Returns ``(normalized_lexical_score, total_coverage, breakdown_dict)``.
    """
    cov_title = field_coverage(query_tokens, query_lower, title)
    cov_episode = field_coverage(query_tokens, query_lower, episode_summary)
    cov_fact = max(0.0, min(1.0, float(fact_cov)))
    cov_turn = max(0.0, min(1.0, float(max_turn_cov)))

    total_cov = _overall_query_coverage(
        query_tokens,
        query_lower,
        [title, episode_summary, *fact_texts_for_cov, *turn_texts_for_cov],
    )
    if total_cov <= 0.0:
        return 0.0, 0.0, {
            "title": 0.0,
            "episode": 0.0,
            "fact": 0.0,
            "turn": 0.0,
        }

    title_pts = WEIGHT_TITLE * cov_title
    episode_pts = WEIGHT_EPISODE * cov_episode
    fact_pts = WEIGHT_FACT * cov_fact
    turn_pts = WEIGHT_TURN * cov_turn

    raw_lexical = (title_pts + episode_pts + fact_pts + turn_pts) * total_cov
    # Normalize against 9.5 so Title (+5.0) > Episode (+3.5) > Fact (+3.0) > Turn (+1.0)
    # remains strictly ordered inside [0.0, 1.0].
    normalized = min(1.0, raw_lexical / 9.5)
    return normalized, total_cov, {
        "title": title_pts,
        "episode": episode_pts,
        "fact": fact_pts,
        "turn": turn_pts,
    }


def score_memory_lexical(
    *,
    query_tokens: list[str],
    query_lower: str,
    text: str,
    weight: float,
) -> float:
    """Compute normalized lexical score in [0.0, 1.0] for an Episode or Durable Fact."""
    cov = field_coverage(query_tokens, query_lower, text)
    if cov <= 0.0:
        return 0.0
    raw = weight * cov * cov
    return min(1.0, raw / 5.0)


def build_search_snippet(
    text: str,
    query_lower: str,
    query_tokens: list[str],
    *,
    window: int = 240,
) -> str:
    """Extract a readable snippet centered around the first matching query term."""
    clean = (text or "").strip()
    if len(clean) <= window:
        return clean

    lower = clean.lower()
    idx = lower.find(query_lower) if query_lower else -1
    if idx == -1:
        for tok in query_tokens:
            idx = lower.find(tok)
            if idx != -1:
                break
        if idx == -1:
            for tok in query_tokens:
                stem = _simple_stem(tok)
                if len(stem) >= 3:
                    idx = lower.find(stem)
                    if idx != -1:
                        break
    if idx == -1:
        return clean[: window - 1] + "…"

    half = window // 2
    start = max(0, idx - half)
    end = min(len(clean), start + window)
    if end - start < window:
        start = max(0, end - window)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(clean) else ""
    return f"{prefix}{clean[start:end].strip()}{suffix}"


def cosine_similarity(
    vec_a: Sequence[float] | np.ndarray,
    vec_b: Sequence[float] | np.ndarray,
) -> float:
    """Compute clamped cosine similarity in [0.0, 1.0] between two vectors."""
    a = np.asarray(vec_a, dtype=np.float32).ravel()
    b = np.asarray(vec_b, dtype=np.float32).ravel()
    if a.size == 0 or b.size == 0 or a.size != b.size:
        return 0.0
    norm_a = float(np.linalg.norm(a))
    norm_b = float(np.linalg.norm(b))
    if norm_a <= 1e-12 or norm_b <= 1e-12:
        return 0.0
    sim = float(np.dot(a, b) / (norm_a * norm_b))
    return max(0.0, min(1.0, sim))


def blend_scores(lexical_score: float, semantic_score: float | None) -> float:
    """Blend semantic and lexical scores (0.6 semantic + 0.4 lexical) with graceful fallback."""
    lex = max(0.0, min(1.0, float(lexical_score)))
    if semantic_score is None:
        return lex
    sem = max(0.0, min(1.0, float(semantic_score)))
    return round(0.6 * sem + 0.4 * lex, 6)


def _to_compact_float32_list(vec: Sequence[float] | np.ndarray | None) -> list[float] | None:
    """Convert any sequence/array to a compact JSON-serializable float32 list."""
    if vec is None:
        return None
    arr = np.asarray(vec, dtype=np.float32).ravel()
    if arr.size == 0 or not np.all(np.isfinite(arr)):
        return None
    return [round(float(x), 6) for x in arr]


def _is_mock_obj(obj: Any) -> bool:
    return isinstance(obj, NonCallableMock)


class EmbeddingProvider:
    """Generates semantic embeddings via custom callable, Gemini API, or local Ollama."""

    def __init__(
        self,
        *,
        embed_fn: EmbedFn | None = None,
        api_key: str | None = None,
        gemini_model: str = DEFAULT_GEMINI_EMBEDDING_MODEL,
        output_dimensionality: int = DEFAULT_EMBEDDING_DIMS,
        ollama_url: str | None = None,
        ollama_model: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.embed_fn = embed_fn
        self.api_key = api_key
        self.gemini_model = gemini_model
        self.output_dimensionality = output_dimensionality
        self.ollama_url = ollama_url
        self.ollama_model = ollama_model
        self.timeout = timeout
        self._gemini_client: Any = None

    def _can_call_network_in_env(self, *, mocked: bool = False) -> bool:
        """Avoid real external embedding network requests under pytest unless opted in/mocked."""
        if mocked:
            return True
        if os.environ.get("CLEA_EMBEDDING_PROVIDER"):
            return True
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return False
        return True

    def _embed_via_gemini(self, text: str) -> list[float] | None:
        try:
            from google import genai
            from google.genai import types
        except Exception:
            return None

        mocked = _is_mock_obj(genai.Client)
        if not self._can_call_network_in_env(mocked=mocked):
            return None

        resolved_key = self.api_key or os.environ.get("GEMINI_API_KEY") or ""
        if not resolved_key and not mocked:
            return None

        try:
            if self._gemini_client is None:
                self._gemini_client = genai.Client(api_key=resolved_key or "test-key")
            cfg = types.EmbedContentConfig(output_dimensionality=self.output_dimensionality)
            resp = self._gemini_client.models.embed_content(
                model=self.gemini_model,
                contents=text,
                config=cfg,
            )
            embeddings = getattr(resp, "embeddings", None)
            if not embeddings:
                return None
            values = getattr(embeddings[0], "values", None)
            return _to_compact_float32_list(values)
        except Exception as exc:
            logger.debug("Gemini embedding unavailable; falling back: %s", exc)
            return None

    def _embed_via_ollama(self, text: str) -> list[float] | None:
        mocked = _is_mock_obj(urllib.request.urlopen)
        configured_url = (
            self.ollama_url
            or os.environ.get("CLEA_OLLAMA_URL")
            or os.environ.get("OLLAMA_HOST")
        )
        configured_model = self.ollama_model or os.environ.get("CLEA_OLLAMA_EMBED_MODEL")

        if not self._can_call_network_in_env(mocked=mocked):
            return None
        if not mocked and not configured_url and not configured_model:
            if os.environ.get("CLEA_EMBEDDING_PROVIDER", "").lower() != "ollama":
                return None

        base_url = (configured_url or DEFAULT_OLLAMA_URL).rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            base_url = f"http://{base_url}"

        candidate_models = (
            [configured_model] if configured_model else list(DEFAULT_OLLAMA_EMBED_MODELS)
        )

        for model_name in candidate_models:
            # Try modern /api/embed then legacy /api/embeddings
            for endpoint, payload_key, resp_key in (
                ("/api/embed", "input", "embeddings"),
                ("/api/embeddings", "prompt", "embedding"),
            ):
                try:
                    body = json.dumps({"model": model_name, payload_key: text}).encode("utf-8")
                    req = urllib.request.Request(
                        f"{base_url}{endpoint}",
                        data=body,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        raw = resp.read().decode("utf-8")
                    data = json.loads(raw)
                    vec = data.get(resp_key)
                    if resp_key == "embeddings" and isinstance(vec, list) and vec:
                        vec = vec[0]
                    parsed = _to_compact_float32_list(vec)
                    if parsed is not None:
                        return parsed
                except Exception as exc:
                    logger.debug(
                        "Ollama embedding attempt (%s %s) unavailable: %s",
                        model_name,
                        endpoint,
                        exc,
                    )
                    continue
        return None

    def embed(self, text: str) -> list[float] | None:
        """Return a compact float32 embedding for text, or None if unavailable."""
        cleaned = (text or "").strip()
        if not cleaned:
            return None

        if self.embed_fn is not None:
            try:
                return _to_compact_float32_list(self.embed_fn(cleaned))
            except Exception as exc:
                logger.debug("Custom embed_fn failed; falling back to lexical: %s", exc)
                return None

        pref = os.environ.get("CLEA_EMBEDDING_PROVIDER", "").strip().lower()
        if pref in ("none", "off", "disabled", "lexical"):
            return None
        if pref == "ollama":
            return self._embed_via_ollama(cleaned)
        if pref == "gemini":
            return self._embed_via_gemini(cleaned)

        vec = self._embed_via_gemini(cleaned)
        if vec is not None:
            return vec
        return self._embed_via_ollama(cleaned)


class EmbeddingStore:
    """Persists compact float32 embeddings for Episode summaries and Durable Facts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._loaded = False
        self._items: dict[str, dict[str, Any]] = {}

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path.exists():
            self._items = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            items = raw.get("items") if isinstance(raw, dict) else None
            if isinstance(items, dict):
                self._items = {
                    str(k): dict(v)
                    for k, v in items.items()
                    if isinstance(k, str) and isinstance(v, dict)
                }
            else:
                self._items = {}
        except Exception as exc:
            logger.warning("Failed reading embeddings cache %s: %s", self.path, exc)
            self._items = {}

    def reload(self) -> None:
        """Force reload from disk."""
        self._loaded = False
        self._ensure_loaded()

    def get_vector(self, key: str, *, expected_text: str | None = None) -> list[float] | None:
        """Retrieve a cached vector if present and matching expected_text hash."""
        self._ensure_loaded()
        entry = self._items.get(key)
        if not entry:
            return None
        if expected_text is not None:
            if entry.get("text_hash") != text_hash(expected_text):
                return None
        vec = entry.get("vector")
        if not isinstance(vec, list) or not vec:
            return None
        return [float(x) for x in vec]

    def set_vector(self, key: str, text: str, vector: Sequence[float]) -> bool:
        """Store a compact float32 vector in memory (call save() to persist)."""
        compact = _to_compact_float32_list(vector)
        if compact is None:
            return False
        self._ensure_loaded()
        digest = text_hash(text)
        existing = self._items.get(key)
        if (
            existing
            and existing.get("text_hash") == digest
            and existing.get("vector") == compact
        ):
            return False
        self._items[key] = {
            "text_hash": digest,
            "dims": len(compact),
            "vector": compact,
        }
        return True

    def remove_key(self, key: str) -> bool:
        """Remove a vector entry if present."""
        self._ensure_loaded()
        return self._items.pop(key, None) is not None

    def save(self) -> Path:
        """Atomically persist embeddings cache to disk."""
        self._ensure_loaded()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.tmp.{uuid4().hex}")
        payload = {"version": 1, "items": self._items}
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp.replace(self.path)
        return self.path
