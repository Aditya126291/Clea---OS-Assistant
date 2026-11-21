"""Tier 3 fact bank reconciler: rule-based command parser and LLM ADD/UPDATE/NOOP/DELETE."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, field_validator

from clea.memory.database import (
    FactCategory,
    MemoryDatabase,
    MemoryFactRecord,
    SurfaceType,
    is_secret_or_sensitive,
    normalize_fact_category,
    slugify_fact_key,
)

if TYPE_CHECKING:
    from clea.providers.base import LLMProvider, Message

logger = logging.getLogger(__name__)

ReconcileOperation = Literal["ADD", "UPDATE", "NOOP", "DELETE"]


class CandidateFact(BaseModel):
    """A candidate durable fact or retraction extracted from user input."""

    fact_text: str
    category: FactCategory = "preference"
    fact_key: str = ""
    confidence: float = 0.9
    operation_hint: Literal["add", "update", "delete"] | None = None
    source_surface: SurfaceType = "chat"
    source_session_id: str = ""


class ReconciliationDecision(BaseModel):
    """Structured reconciliation action for a candidate fact against the fact bank."""

    operation: ReconcileOperation
    category: FactCategory = "preference"
    fact_key: str = ""
    fact_text: str = ""
    target_fact_id: str | None = None
    confidence: float = 0.9
    reason: str = ""

    @field_validator("operation", mode="before")
    @classmethod
    def _normalize_op(cls, value: Any) -> ReconcileOperation:
        raw = str(value or "NOOP").strip().upper()
        if raw in ("REMOVE", "FORGET"):
            return "DELETE"
        if raw in ("ADD", "UPDATE", "NOOP", "DELETE"):
            return raw  # type: ignore[return-value]
        return "NOOP"

    @field_validator("category", mode="before")
    @classmethod
    def _normalize_cat(cls, value: Any) -> FactCategory:
        return normalize_fact_category(str(value) if value is not None else None)


class ReconciliationBatch(BaseModel):
    """Batch of reconciliation decisions returned by the structured LLM reconciler."""

    decisions: list[ReconciliationDecision] = Field(default_factory=list)


class ReconciliationResult(BaseModel):
    """Execution summary after applying reconciliation decisions to MemoryDatabase."""

    decisions: list[ReconciliationDecision] = Field(default_factory=list)
    added_ids: list[str] = Field(default_factory=list)
    updated_ids: list[str] = Field(default_factory=list)
    superseded_ids: list[str] = Field(default_factory=list)
    deleted_ids: list[str] = Field(default_factory=list)
    noop_ids: list[str] = Field(default_factory=list)
    used_llm: bool = False
    records: list[MemoryFactRecord] = Field(default_factory=list)


RECONCILIATION_SYSTEM_PROMPT = """You are Clea's Tier 3 Fact Bank reconciliation engine.
Given candidate user statements and existing active memory facts, decide for each candidate
whether to ADD a new fact, UPDATE (supersede) an existing fact, DELETE a retracted fact,
or NOOP if already accurately captured or not durable.

Rules:
1. Allowed categories: preference, environment, project, workflow, identity, constraint.
2. NEVER store secrets, API keys, tokens, passwords, card numbers, or government IDs.
3. For UPDATE or DELETE, set "target_fact_id" to the matching existing fact ID when present.
4. Provide a concise "fact_key" slug (snake_case) and clean standalone "fact_text".
5. Respond strictly with valid JSON matching the schema."""

_POLITE_PREFIX_RE = re.compile(
    r"^(?:(?:hey|hi|ok|okay)\s+)?(?:clea\s*[,:]?\s*)?(?:please\s+)?",
    flags=re.IGNORECASE,
)
_QUESTION_RECALL_RE = re.compile(
    r"^(?:(?:do|did|can|could|would)\s+you\s+remember|i\s+(?:don'?t|do\s+not|can'?t)\s+remember)\b",
    flags=re.IGNORECASE,
)
_REMEMBER_CMD_RE = re.compile(
    r"^(?:don'?t\s+forget\s+that|don'?t\s+forget|do\s+not\s+forget\s+that|do\s+not\s+forget|"
    r"remember\s+that|remember)\s+(.+)$",
    flags=re.IGNORECASE,
)
_FORGET_CMD_RE = re.compile(
    r"^(?:forget\s+that|forget\s+about|forget)\s+(.+)$",
    flags=re.IGNORECASE,
)
_NO_LONGER_RE = re.compile(
    r"^(?:i\s+no\s+longer)\s+(.+)$",
    flags=re.IGNORECASE,
)


def _infer_fact_category(text: str) -> FactCategory:
    low = (text or "").lower()
    if any(
        kw in low
        for kw in ("never ", "must not", "don't ", "do not ", "avoid ", "constraint", "limit")
    ):
        return "constraint"
    if any(
        kw in low
        for kw in (
            "my name",
            "i am ",
            "i'm ",
            "live in",
            "located in",
            "work at",
            "timezone",
        )
    ):
        return "identity"
    if any(
        kw in low
        for kw in (
            "windows",
            "macos",
            "linux",
            "laptop",
            "desktop",
            "machine",
            "editor",
            "cursor",
            "vscode",
            "vs code",
            "terminal",
            "python",
            "uv ",
            "docker",
            "os ",
        )
    ):
        return "environment"
    if any(kw in low for kw in ("project", "repo", "repository", "clea", "codebase", "building")):
        return "project"
    if any(
        kw in low
        for kw in ("workflow", "commit", "worktree", "branch", "always run", "before ")
    ):
        return "workflow"
    return "preference"


def parse_explicit_fact_commands(
    text: str,
    *,
    source_surface: SurfaceType = "chat",
    source_session_id: str = "",
) -> list[CandidateFact]:
    """Deterministically extract explicit remember/forget/no-longer commands from text."""
    raw = (text or "").strip()
    if not raw:
        return []

    clauses = [c.strip() for c in re.split(r"(?<=[.!?\n;])\s+|\n+", raw) if c.strip()]
    candidates: list[CandidateFact] = []
    seen_pairs: set[tuple[str | None, str]] = set()

    for clause in clauses:
        cleaned_clause = _POLITE_PREFIX_RE.sub("", clause).strip().rstrip(".!?;").strip()
        if not cleaned_clause or _QUESTION_RECALL_RE.match(cleaned_clause):
            continue

        op_hint: Literal["add", "delete"] | None = None
        extracted = ""

        # Check "don't forget" / "remember" BEFORE "forget"
        m_rem = _REMEMBER_CMD_RE.match(cleaned_clause)
        if m_rem:
            op_hint = "add"
            extracted = m_rem.group(1).strip()
        else:
            m_fgt = _FORGET_CMD_RE.match(cleaned_clause)
            if m_fgt:
                op_hint = "delete"
                extracted = m_fgt.group(1).strip()
            else:
                m_nl = _NO_LONGER_RE.match(cleaned_clause)
                if m_nl:
                    op_hint = "delete"
                    extracted = m_nl.group(1).strip()

        extracted = extracted.strip(" \"'").rstrip(".!?;").strip()
        if not extracted or len(extracted) < 2:
            continue
        if is_secret_or_sensitive(clause) or is_secret_or_sensitive(extracted):
            continue

        cat = _infer_fact_category(extracted)
        dedup_key = (op_hint, extracted.lower())
        if dedup_key in seen_pairs:
            continue
        seen_pairs.add(dedup_key)

        candidates.append(
            CandidateFact(
                fact_text=extracted,
                category=cat,
                fact_key=slugify_fact_key(extracted, cat),
                confidence=0.95 if op_hint == "add" else 0.9,
                operation_hint=op_hint,
                source_surface=source_surface,
                source_session_id=source_session_id,
            )
        )

    return candidates


def _extract_json_block(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("{") and cleaned.endswith("}"):
        try:
            json.loads(cleaned)
            return cleaned
        except Exception:
            pass
    if cleaned.startswith("```"):
        first_nl = cleaned.find("\n")
        last_fence = cleaned.rfind("```")
        if first_nl != -1 and last_fence > first_nl:
            inner = cleaned[first_nl + 1 : last_fence].strip()
            if inner.startswith("{") and inner.endswith("}"):
                return inner
            cleaned = inner
    first_brace = cleaned.find("{")
    last_brace = cleaned.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        return cleaned[first_brace : last_brace + 1].strip()
    return cleaned


def _inline_schema_defs(schema: dict[str, Any]) -> dict[str, Any]:
    defs = schema.pop("$defs", {})
    if defs and "decisions" in schema.get("properties", {}):
        item_def = defs.get("ReconciliationDecision")
        if item_def and schema["properties"]["decisions"].get("items") == {
            "$ref": "#/$defs/ReconciliationDecision"
        }:
            schema["properties"]["decisions"]["items"] = item_def
    return schema


def _delete_search_terms(phrase: str) -> list[str]:
    clean = (phrase or "").strip()
    if not clean:
        return []
    terms = [clean]
    tokens = [
        t
        for t in re.findall(r"[A-Za-z0-9_.-]+", clean)
        if len(t) >= 3
        and t.lower()
        not in {
            "that",
            "about",
            "the",
            "this",
            "live",
            "used",
            "use",
            "have",
            "had",
            "from",
            "with",
            "for",
            "and",
            "anymore",
        }
    ]
    terms.extend(sorted(tokens, key=len, reverse=True))
    return terms


class FactBankReconciler:
    """Reconciles explicit commands and conversational facts into Tier 3 MemoryDatabase."""

    def __init__(
        self,
        db: MemoryDatabase,
        provider: LLMProvider | None = None,
        llm_timeout_seconds: float = 20.0,
    ) -> None:
        self.db = db
        self.provider = provider
        self.llm_timeout_seconds = max(0.05, float(llm_timeout_seconds))

    def _get_response_format_kwargs(self) -> dict[str, Any]:
        if self.provider is None:
            return {}
        schema = _inline_schema_defs(ReconciliationBatch.model_json_schema())
        response_format = {
            "type": "text",
            "mime_type": "application/json",
            "schema": schema,
        }
        sig = inspect.signature(self.provider.stream_turn)
        if "response_format" in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        ):
            return {"response_format": response_format}
        return {}

    async def _call_llm_reconciler(
        self,
        *,
        user_text: str,
        candidates: list[CandidateFact],
        existing_facts: list[MemoryFactRecord],
    ) -> ReconciliationBatch | None:
        if self.provider is None:
            return None

        lines: list[str] = []
        if existing_facts:
            lines.append("Existing Active Memory Facts:")
            for fact in existing_facts:
                lines.append(
                    f"- id={fact.id} category={fact.category} key={fact.fact_key} "
                    f"text={fact.fact_text}"
                )
        else:
            lines.append("Existing Active Memory Facts: (none)")

        if candidates:
            lines.append("\nExplicit Candidate Commands:")
            for cand in candidates:
                lines.append(
                    f"- hint={cand.operation_hint or 'add'} category={cand.category} "
                    f"key={cand.fact_key} text={cand.fact_text}"
                )

        if user_text.strip():
            lines.append(f"\nUser Input:\n{user_text.strip()[:3000]}")

        prompt = "\n".join(lines)
        call_kwargs = self._get_response_format_kwargs()

        async def _collect() -> str:
            from clea.providers.base import Message

            parts: list[str] = []
            async for chunk in self.provider.stream_turn(  # type: ignore[union-attr]
                messages=[Message(role="user", content=prompt)],
                tools=[],
                system_prompt=RECONCILIATION_SYSTEM_PROMPT,
                **call_kwargs,
            ):
                if chunk.type == "text_delta" and chunk.text:
                    parts.append(chunk.text)
            return "".join(parts)

        try:
            raw = await asyncio.wait_for(_collect(), timeout=self.llm_timeout_seconds)
            if not raw.strip():
                return None
            extracted = _extract_json_block(raw)
            return ReconciliationBatch.model_validate_json(extracted)
        except TimeoutError:
            logger.warning(
                "Fact reconciliation LLM timed out after %.1fs; falling back to rules",
                self.llm_timeout_seconds,
            )
            return None
        except Exception as exc:
            logger.warning("Fact reconciliation LLM failed (%s); falling back to rules", exc)
            return None

    def _gather_relevant_facts(
        self,
        *,
        text: str,
        candidates: Sequence[CandidateFact],
        limit: int = 15,
    ) -> list[MemoryFactRecord]:
        by_id: dict[str, MemoryFactRecord] = {}
        for cand in candidates:
            for item in self.db.find_relevant_active_facts(
                cand.fact_text, category=cand.category, limit=5
            ):
                by_id[item.id] = item
        if text.strip():
            for item in self.db.find_relevant_active_facts(text, limit=8):
                by_id[item.id] = item
        if len(by_id) < limit:
            for item in self.db.list_facts(status="active", limit=limit):
                by_id.setdefault(item.id, item)
                if len(by_id) >= limit:
                    break
        return list(by_id.values())[:limit]

    def _decisions_from_candidates(
        self,
        candidates: Sequence[CandidateFact],
    ) -> list[ReconciliationDecision]:
        decisions: list[ReconciliationDecision] = []
        for cand in candidates:
            if is_secret_or_sensitive(cand.fact_text):
                continue
            if cand.operation_hint == "delete":
                target_id: str | None = None
                matches = self.db.find_relevant_active_facts(cand.fact_text, limit=3)
                if matches:
                    target_id = matches[0].id
                else:
                    for term in _delete_search_terms(cand.fact_text):
                        sub_matches = self.db.find_relevant_active_facts(term, limit=1)
                        if sub_matches:
                            target_id = sub_matches[0].id
                            break
                decisions.append(
                    ReconciliationDecision(
                        operation="DELETE" if target_id else "NOOP",
                        category=cand.category,
                        fact_key=cand.fact_key,
                        fact_text=cand.fact_text,
                        target_fact_id=target_id,
                        confidence=cand.confidence,
                        reason="Rule-based explicit deletion",
                    )
                )
            else:
                matches = self.db.find_relevant_active_facts(
                    cand.fact_text, category=cand.category, limit=5
                )
                norm_cand = " ".join(cand.fact_text.lower().split())
                exact = next(
                    (m for m in matches if " ".join(m.fact_text.lower().split()) == norm_cand),
                    None,
                )
                if exact is not None:
                    decisions.append(
                        ReconciliationDecision(
                            operation="NOOP",
                            category=exact.category,
                            fact_key=exact.fact_key,
                            fact_text=cand.fact_text,
                            target_fact_id=exact.id,
                            confidence=cand.confidence,
                            reason="Exact fact already active",
                        )
                    )
                    continue

                same_key = next(
                    (
                        m
                        for m in matches
                        if m.fact_key == cand.fact_key and m.category == cand.category
                    ),
                    None,
                )
                if same_key is not None or cand.operation_hint == "update":
                    decisions.append(
                        ReconciliationDecision(
                            operation="UPDATE" if same_key is not None else "ADD",
                            category=cand.category,
                            fact_key=cand.fact_key,
                            fact_text=cand.fact_text,
                            target_fact_id=same_key.id if same_key is not None else None,
                            confidence=cand.confidence,
                            reason="Rule-based key reconciliation",
                        )
                    )
                else:
                    decisions.append(
                        ReconciliationDecision(
                            operation="ADD",
                            category=cand.category,
                            fact_key=cand.fact_key,
                            fact_text=cand.fact_text,
                            confidence=cand.confidence,
                            reason="Rule-based explicit remember",
                        )
                    )
        return decisions

    def _execute_decisions(
        self,
        decisions: Sequence[ReconciliationDecision],
        *,
        source_surface: SurfaceType = "chat",
        source_session_id: str = "",
        used_llm: bool = False,
    ) -> ReconciliationResult:
        result = ReconciliationResult(used_llm=used_llm)
        for dec in decisions:
            if dec.fact_text and is_secret_or_sensitive(dec.fact_text):
                logger.warning("Rejected secret fact during reconciliation.")
                continue

            result.decisions.append(dec)
            op = dec.operation

            if op == "NOOP":
                if dec.target_fact_id:
                    touched = self.db.touch_fact(dec.target_fact_id)
                    if touched is not None:
                        result.noop_ids.append(touched.id)
                        result.records.append(touched)
                continue

            if op == "DELETE":
                deleted: list[str] = []
                if dec.target_fact_id:
                    deleted = self.db.delete_fact(fact_id=dec.target_fact_id)
                if not deleted and dec.fact_text:
                    deleted = self.db.delete_fact(query=dec.fact_text)
                if not deleted and dec.fact_text:
                    for term in _delete_search_terms(dec.fact_text):
                        deleted = self.db.delete_fact(query=term)
                        if deleted:
                            break
                result.deleted_ids.extend(deleted)
                continue

            clean_text = (dec.fact_text or "").strip()
            if not clean_text:
                continue
            key = dec.fact_key.strip() or slugify_fact_key(clean_text, dec.category)

            if op == "UPDATE":
                res = self.db.manage_fact(
                    "update",
                    fact_text=clean_text,
                    category=dec.category,
                    fact_key=key,
                    fact_id=dec.target_fact_id,
                    query=clean_text,
                    confidence=dec.confidence,
                    source_surface=source_surface,
                    source_session_id=source_session_id,
                )
            else:
                res = self.db.manage_fact(
                    "add",
                    fact_text=clean_text,
                    category=dec.category,
                    fact_key=key,
                    confidence=dec.confidence,
                    source_surface=source_surface,
                    source_session_id=source_session_id,
                )

            actual_op = res.get("operation")
            fact_dict = res.get("fact")
            if isinstance(fact_dict, dict):
                rec = MemoryFactRecord.model_validate(fact_dict)
                result.records.append(rec)
                if actual_op == "add":
                    result.added_ids.append(rec.id)
                elif actual_op == "update":
                    result.updated_ids.append(rec.id)
                    if res.get("superseded_id"):
                        result.superseded_ids.append(str(res["superseded_id"]))
                elif actual_op == "noop":
                    result.noop_ids.append(rec.id)

        return result

    def reconcile_explicit_sync(
        self,
        text: str,
        *,
        source_surface: SurfaceType = "chat",
        source_session_id: str = "",
    ) -> ReconciliationResult:
        """Synchronously parse and reconcile explicit user fact commands."""
        candidates = parse_explicit_fact_commands(
            text,
            source_surface=source_surface,
            source_session_id=source_session_id,
        )
        if not candidates:
            return ReconciliationResult()
        decisions = self._decisions_from_candidates(candidates)
        return self._execute_decisions(
            decisions,
            source_surface=source_surface,
            source_session_id=source_session_id,
            used_llm=False,
        )

    async def reconcile_text(
        self,
        text: str,
        *,
        source_surface: SurfaceType = "chat",
        source_session_id: str = "",
        candidates: list[CandidateFact] | None = None,
    ) -> ReconciliationResult:
        """Reconcile text (and optional explicit candidates) using LLM with rule fallback."""
        resolved_candidates = (
            list(candidates)
            if candidates is not None
            else parse_explicit_fact_commands(
                text,
                source_surface=source_surface,
                source_session_id=source_session_id,
            )
        )
        clean_text = (text or "").strip()
        if not resolved_candidates and (not clean_text or self.provider is None):
            return ReconciliationResult()
        if clean_text and is_secret_or_sensitive(clean_text) and not resolved_candidates:
            return ReconciliationResult()

        existing = self._gather_relevant_facts(text=clean_text, candidates=resolved_candidates)
        if self.provider is not None:
            batch = await self._call_llm_reconciler(
                user_text=clean_text,
                candidates=resolved_candidates,
                existing_facts=existing,
            )
            if batch is not None and (batch.decisions or not resolved_candidates):
                return self._execute_decisions(
                    batch.decisions,
                    source_surface=source_surface,
                    source_session_id=source_session_id,
                    used_llm=True,
                )

        if not resolved_candidates:
            return ReconciliationResult()
        decisions = self._decisions_from_candidates(resolved_candidates)
        return self._execute_decisions(
            decisions,
            source_surface=source_surface,
            source_session_id=source_session_id,
            used_llm=False,
        )

    async def reconcile_messages(
        self,
        messages: Sequence[Message | dict[str, Any]],
        *,
        source_surface: SurfaceType = "chat",
        source_session_id: str = "",
    ) -> ReconciliationResult:
        """Reconcile facts from a sequence of Message objects or transcript dicts."""
        user_texts: list[str] = []
        candidates: list[CandidateFact] = []
        for msg in messages:
            if isinstance(msg, dict):
                role = str(msg.get("role") or "")
                raw_val = msg.get("text") if msg.get("text") is not None else msg.get("content")
                content = str(raw_val or "")
            else:
                role = str(getattr(msg, "role", "") or "")
                content = str(getattr(msg, "content", "") or "")
            if role != "user":
                continue
            stripped = content.strip()
            if not stripped:
                continue
            user_texts.append(stripped)
            candidates.extend(
                parse_explicit_fact_commands(
                    stripped,
                    source_surface=source_surface,
                    source_session_id=source_session_id,
                )
            )

        combined_user_text = "\n".join(user_texts).strip()
        if not combined_user_text and not candidates:
            return ReconciliationResult()

        return await self.reconcile_text(
            combined_user_text,
            source_surface=source_surface,
            source_session_id=source_session_id,
            candidates=candidates,
        )
