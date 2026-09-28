"""Resolve a brief reference only at the HTTP response boundary."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, ValidationError

from src.payload_store import PayloadStore, PayloadStoreError
from src.nodes.security import contains_forbidden_decision, has_pii_or_sensitive_operation, normalized_guard_token
from src.services.service import SAFE_ID


OUTPUT_FIELDS = (
    "facts",
    "evidence_sources",
    "unresolved_points",
    "countervailing_observations",
    "review_questions",
)
FORBIDDEN_OUTPUT_KEYS = frozenset({"rca_accepted", "penalty_amount", "sla_credit", "contract_remedy"})
_FORBIDDEN_GUARD_KEYS = frozenset(normalized_guard_token(key) for key in FORBIDDEN_OUTPUT_KEYS)


class InvokeOutputSchema(BaseModel):
    """HTTP-only allowlist schema; it is never stored in graph State."""

    model_config = ConfigDict(extra="forbid")

    facts: list[dict[str, Any]]
    evidence_sources: list[dict[str, Any]]
    unresolved_points: list[dict[str, Any]]
    countervailing_observations: list[dict[str, Any]]
    review_questions: list[dict[str, Any]]


def resolve_invoke_output(
    brief_store: PayloadStore,
    brief_ref: str,
    *,
    scope: Mapping[str, str],
    session_id: str,
) -> dict[str, list[dict[str, Any]]]:
    """Resolve a scope-bound brief once and validate a copied allowlist view."""

    if not isinstance(brief_ref, str) or not brief_ref:
        raise ValueError("brief_ref_missing")
    try:
        value = brief_store.resolve(brief_ref, scope=scope, session_id=session_id, consume=True)
    except PayloadStoreError as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(value, dict):
        raise ValueError("brief_ref_payload_invalid")
    copied = deepcopy(value)
    _assert_safe_structure(copied)
    try:
        return InvokeOutputSchema.model_validate(copied).model_dump()
    except ValidationError as exc:
        raise ValueError("response_allowlist_schema_invalid") from exc


def _assert_safe_structure(value: Any, key: str = "") -> None:
    if isinstance(value, dict):
        for child_key, child in value.items():
            if normalized_guard_token(child_key) in _FORBIDDEN_GUARD_KEYS:
                raise ValueError("response_forbidden_field")
            _assert_safe_structure(child, str(child_key))
        return
    if isinstance(value, list):
        for child in value:
            _assert_safe_structure(child, key)
        return
    if isinstance(value, str):
        if contains_forbidden_decision(value):
            raise ValueError("response_forbidden_decision_text")
        # PII detection is framework-only (SVC-C2-098 B-5): ordinary Japanese
        # names are a known ``detect_pii`` gap that is intentionally not covered
        # here — a bespoke supplement was removed as too leaky to maintain.
        # ``has_pii_or_sensitive_operation`` already calls ``detect_pii``, so it
        # is the single check for both PII and sensitive-operation tokens.
        if has_pii_or_sensitive_operation(value):
            raise ValueError("response_pii_detected")
        # IDs are safe as evidence identifiers; free prose is separately
        # generated from safe templates/LLM and may contain Japanese text.
        if key in {"source_id", "case_id"} and not SAFE_ID.fullmatch(value):
            raise ValueError("response_unsafe_evidence_id")
