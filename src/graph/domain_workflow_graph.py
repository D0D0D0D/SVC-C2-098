"""Inner Cat2 workflow for the three SVC-C2-098 modes.

The graph receives only a JSON canonical envelope.  It resolves raw material
from a server-owned scoped store in local variables and persists generated
briefs there before a super-step can checkpoint them.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.graph.base_graph import BaseGraph
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from langgraph.graph import END, START
from src.evidence_gate import FindingField, project_typed_findings
from src.ingest_file_sanitizer import TextQualityPolicy, sanitize_file_bytes
from src.ingest_row_pipeline import IngestRowPipelineNode, RowPipelineHooks
from src.llm_draft_stage import DraftStageHooks, LlmDraftStageNode
from src.nodes.security import (
    SAFE_ID_RE,
    contains_forbidden_decision,
    find_forbidden_keys,
    has_pii_or_sensitive_operation,
    require_safe_ids,
)
from src.scoped_retrieval import ScopedRetrievalNode
from src.services.service import DomainServices, normalize_kb_query


class InnerState(AgentState, total=False):
    mode: str
    tenant_id: str
    case_ref: str
    payload_ref: str
    query: str
    evidence_present: bool
    evidence_gaps: list[str]
    provenance_ref: str
    proof_ref: str
    output: dict
    requires_human_review: bool
    ingest_summary: dict
    feedback_summary: dict
    error_code: str
    error_type: str


def _scope(state: dict) -> dict[str, str]:
    scope = {"tenant_id": state.get("tenant_id"), "case_ref": state.get("case_ref")}
    if any(not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value) for value in scope.values()):
        raise ValueError("scope_invalid")
    return scope  # type: ignore[return-value]


def _resolve_payload(services: DomainServices, state: dict) -> dict[str, Any]:
    value = services.raw_payload_store.resolve(
        state.get("payload_ref"), scope=_scope(state), session_id=str(state.get("session_id", "")), consume=False
    )
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("payload_shape_invalid")
    return value


def _resolve_provenance(services: DomainServices, state: dict, *, ref_key: str = "provenance_ref") -> dict[str, Any]:
    value = services.provenance_store.resolve(
        state.get(ref_key), scope=_scope(state), session_id=str(state.get("session_id", "")), consume=False
    )
    if not isinstance(value, dict) or not isinstance(value.get("records"), list):
        raise ValueError("provenance_shape_invalid")
    return value


class ParseEnvelopeNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        try:
            envelope = json.loads(state.get("user_input", ""))
            if not isinstance(envelope, dict) or set(envelope) != {"mode", "tenant_id", "case_ref", "payload_ref"}:
                raise ValueError("inner_envelope_invalid")
            if envelope["mode"] not in {"invoke", "ingest", "feedback"}:
                raise ValueError("inner_mode_invalid")
            if any(not isinstance(envelope[key], str) for key in ("tenant_id", "case_ref", "payload_ref")):
                raise ValueError("inner_envelope_type_invalid")
            _scope(envelope)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            emit_trace_event("inner_envelope_rejected", {"reason": type(exc).__name__}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["ParseEnvelopeNode: canonical_envelope_invalid"]}
        emit_trace_event("inner_envelope_parsed", {"mode": envelope["mode"]}, state)
        return {**envelope, "status": AgentStatus.SUCCESS.value}


def _public_check_code(exc: Exception) -> str:
    """Name the caller-fixable rejection; keep anything else generic.

    The broad ``except`` around the deterministic check also catches genuine
    internal faults. Reporting those as a caller mistake would be worse than
    saying nothing, so only the known input-contract failure is named.
    """

    if isinstance(exc, ValueError) and str(exc) == "current_evidence_required":
        return "current_evidence_required"
    return "current evidence unavailable"


class DeterministicCheckNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, services: DomainServices) -> None:
        super().__init__()
        self._services = services

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        try:
            payload = _resolve_payload(self._services, state)
            records = payload.get("records", [])
            if not isinstance(records, list):
                raise ValueError("current_evidence_records_missing")
            projected: list[dict[str, Any]] = []
            scope = _scope(state)
            for record in records:
                if not isinstance(record, dict):
                    continue
                source_id = record.get("source_id") or record.get("ticket_id") or record.get("log_id")
                if not isinstance(source_id, str) or not SAFE_ID_RE.fullmatch(source_id):
                    continue
                projected.append(
                    {
                        "source_id": source_id,
                        "record_kind": "current_evidence",
                        "stage": "stage1",
                        "tenant_id": scope["tenant_id"],
                        "case_id": scope["case_ref"],
                        "source_type": str(record.get("source_type", "evidence"))[:32],
                    }
                )
            if not projected:
                raise ValueError("current_evidence_required")
            extra = payload.get("__extra__", {})
            gaps = []
            if not isinstance(extra, dict) or not extra.get("sla_definition"):
                gaps.append("sla_definition_missing")
            if not isinstance(extra, dict) or not extra.get("measurement_period"):
                gaps.append("measurement_period_missing")
        except Exception as exc:  # no raw values in state/audit
            emit_trace_event("deterministic_check_rejected", {"reason": type(exc).__name__}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": [f"DeterministicCheckNode: {_public_check_code(exc)}"]}
        emit_trace_event("deterministic_check_complete", {"current_evidence_count": len(projected), "gap_count": len(gaps)}, state)
        return {
            "evidence_present": True,
            "evidence_gaps": gaps,
            "provenance_ref": self._services.provenance_store.put(
                {
                    "records": projected,
                    "stage1_current_ids": [record["source_id"] for record in projected],
                },
                scope=scope,
                session_id=str(state.get("session_id", "")),
            ),
            "query": f"sla deviation rca case {state['case_ref']}",
            "status": AgentStatus.SUCCESS.value,
        }


class PrecedentRecallNode(ScopedRetrievalNode):
    """Stage2 projection; precedents are view-point hints, never evidence."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, services: DomainServices, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._services = services

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        emit_trace_event("precedent_recall_started", {}, state)
        try:
            provenance = _resolve_provenance(self._services, state)
        except Exception as exc:
            emit_trace_event("precedent_recall_rejected", {"reason": type(exc).__name__}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["PrecedentRecallNode: provenance unavailable"]}
        result = super().execute(state)
        if result.get("status") == AgentStatus.ERROR.value:
            return result
        precedents: list[dict[str, Any]] = []
        for record in result.get("retrieved_records", []):
            projected = dict(record)
            projected["record_kind"] = "precedent"
            projected["tenant_id"] = state["tenant_id"]
            projected["case_id"] = state["case_ref"]
            precedents.append(projected)
        emit_trace_event("precedent_recall_complete", {"precedent_count": len(precedents)}, state)
        updated_provenance = {
            "records": list(provenance["records"]) + precedents,
            "stage1_current_ids": list(provenance.get("stage1_current_ids", [])),
        }
        new_ref = self._services.provenance_store.put(
            updated_provenance, scope=_scope(state), session_id=str(state.get("session_id", ""))
        )
        self._services.provenance_store.delete(
            state.get("provenance_ref"), scope=_scope(state), session_id=str(state.get("session_id", ""))
        )
        return {
            "provenance_ref": new_ref,
            "status": AgentStatus.SUCCESS.value,
            **({"degradation_reason": result["degradation_reason"]} if result.get("degradation_reason") else {}),
        }


_SOURCE_IDS_FIELD = "source_ids"
_CLAIM_ID_FIELD = "claim_id"
_SOURCE_ID_FIELD = "source_id"
_RECORD_KIND_FIELD = "record_kind"
_FACTS_SECTION = "facts"
_EVIDENCE_SOURCES_SECTION = "evidence_sources"
_UNRESOLVED_POINTS_SECTION = "unresolved_points"
_COUNTERVAILING_SECTION = "countervailing_observations"
_REVIEW_QUESTIONS_SECTION = "review_questions"
_STATEMENT_FIELD = "statement"
_POINT_FIELD = "point"
_OBSERVATION_FIELD = "observation"
_QUESTION_FIELD = "question"
_DRAFT_SECTION_LIMIT = 12
_DRAFT_TEXT_MAX_CHARS = 1200
_RECORD_KINDS = ("current_evidence", "precedent")


@dataclass(frozen=True)
class _DraftSectionContract:
    fields: tuple[FindingField, ...]
    text_field: str | None
    identifier_field: str


_DRAFT_SECTIONS: dict[str, _DraftSectionContract] = {
    _FACTS_SECTION: _DraftSectionContract(
        fields=(
            FindingField(_CLAIM_ID_FIELD, str, required=True),
            FindingField(_STATEMENT_FIELD, str, required=True),
            FindingField(_SOURCE_IDS_FIELD, list, required=True, item_type=str),
        ),
        text_field=_STATEMENT_FIELD,
        identifier_field=_CLAIM_ID_FIELD,
    ),
    _EVIDENCE_SOURCES_SECTION: _DraftSectionContract(
        fields=(
            FindingField(_SOURCE_ID_FIELD, str, required=True),
            FindingField(_RECORD_KIND_FIELD, str, required=True, allowed_values=_RECORD_KINDS),
            FindingField(_SOURCE_IDS_FIELD, list, required=True, item_type=str),
        ),
        text_field=None,
        identifier_field=_SOURCE_ID_FIELD,
    ),
    _UNRESOLVED_POINTS_SECTION: _DraftSectionContract(
        fields=(
            FindingField(_CLAIM_ID_FIELD, str, required=True),
            FindingField(_POINT_FIELD, str, required=True),
            FindingField(_SOURCE_IDS_FIELD, list, required=True, item_type=str),
        ),
        text_field=_POINT_FIELD,
        identifier_field=_CLAIM_ID_FIELD,
    ),
    _COUNTERVAILING_SECTION: _DraftSectionContract(
        fields=(
            FindingField(_CLAIM_ID_FIELD, str, required=True),
            FindingField(_OBSERVATION_FIELD, str, required=True),
            FindingField(_SOURCE_IDS_FIELD, list, required=True, item_type=str),
        ),
        text_field=_OBSERVATION_FIELD,
        identifier_field=_CLAIM_ID_FIELD,
    ),
    _REVIEW_QUESTIONS_SECTION: _DraftSectionContract(
        fields=(
            FindingField(_CLAIM_ID_FIELD, str, required=True),
            FindingField(_QUESTION_FIELD, str, required=True),
            FindingField(_SOURCE_IDS_FIELD, list, required=True, item_type=str),
        ),
        text_field=_QUESTION_FIELD,
        identifier_field=_CLAIM_ID_FIELD,
    ),
}


def _response_contract() -> dict[str, Any]:
    """Generate the prompt contract from the same typed fields as validation."""

    properties: dict[str, Any] = {}
    for section_name, section in _DRAFT_SECTIONS.items():
        item_properties: dict[str, Any] = {}
        required: list[str] = []
        for field in section.fields:
            field_contract: dict[str, Any] = {"type": _json_type(field.expected_type)}
            if field.item_type is not None:
                field_contract["items"] = {"type": _json_type(field.item_type)}
            if field.allowed_values:
                field_contract["enum"] = list(field.allowed_values)
            if field.name == section.text_field:
                field_contract["minLength"] = 1
                field_contract["maxLength"] = _DRAFT_TEXT_MAX_CHARS
            item_properties[field.name] = field_contract
            if field.required:
                required.append(field.name)
        properties[section_name] = {
            "type": "array",
            "minItems": 1,
            "maxItems": _DRAFT_SECTION_LIMIT,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": required,
                "properties": item_properties,
            },
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(_DRAFT_SECTIONS),
        "properties": properties,
    }


def _json_type(declared: type | tuple[type, ...]) -> str | list[str]:
    declarations = declared if isinstance(declared, tuple) else (declared,)
    names = {
        str: "string",
        list: "array",
        dict: "object",
        int: "integer",
        float: "number",
        bool: "boolean",
    }
    labels = [names[item] for item in declarations]
    return labels[0] if len(labels) == 1 else labels


def _fallback_brief(state: dict) -> dict[str, list[dict[str, Any]]]:
    provenance = state.get("_provenance", {})
    current = [r for r in provenance.get("records", []) if r.get("record_kind") == "current_evidence"]
    source_ids = require_safe_ids(record["source_id"] for record in current)
    if not source_ids:
        raise ValueError("current_evidence_required")
    primary = source_ids[0]
    return {
        _FACTS_SECTION: [
            {
                _CLAIM_ID_FIELD: "fallback_fact_1",
                _STATEMENT_FIELD: "Current incident evidence was indexed for the stated SLA deviation.",
                _SOURCE_IDS_FIELD: [primary],
            }
        ],
        _EVIDENCE_SOURCES_SECTION: [
            {
                _SOURCE_ID_FIELD: sid,
                _RECORD_KIND_FIELD: "current_evidence",
                _SOURCE_IDS_FIELD: [sid],
            }
            for sid in source_ids
        ],
        _UNRESOLVED_POINTS_SECTION: [
            {
                _CLAIM_ID_FIELD: "fallback_unresolved_1",
                _POINT_FIELD: "The causal chain asserted in the RCA requires a source-linked explanation.",
                _SOURCE_IDS_FIELD: [primary],
            }
        ],
        _COUNTERVAILING_SECTION: [
            {
                _CLAIM_ID_FIELD: "fallback_countervailing_1",
                _OBSERVATION_FIELD: "Compare the RCA hypothesis against the incident timeline and monitoring record.",
                _SOURCE_IDS_FIELD: [primary],
            }
        ],
        _REVIEW_QUESTIONS_SECTION: [
            {
                _CLAIM_ID_FIELD: "fallback_question_1",
                _QUESTION_FIELD: "Which current record establishes each causal step and its corrective-action connection?",
                _SOURCE_IDS_FIELD: [primary],
            },
            {
                _CLAIM_ID_FIELD: "fallback_disclaimer_1",
                _QUESTION_FIELD: "This is issue organization, not an RCA acceptance or contract decision.",
                _SOURCE_IDS_FIELD: [primary],
            },
        ],
    }


def _build_prompt(state: dict) -> str:
    raw = state.get("_raw_payload", {})
    provenance = state.get("_provenance", {})
    return (
        "Return only one JSON object that conforms exactly to response_contract. "
        f"response_contract={json.dumps(_response_contract(), ensure_ascii=False, sort_keys=True)}. "
        "Every source_ids array must contain only IDs from retrieved_records supplied below, and each "
        "claim must cite at least one current_evidence ID. Each evidence_sources source_id must also appear "
        "in that object's source_ids. "
        # _normalise_draft rejects a claim_id reused anywhere in the response; the
        # registered model reuses them across sections unless told otherwise
        # (28/40 rejected on 2026-09-17 without this sentence, 40/40 with it).
        "Every claim_id must be unique across the whole response: never reuse a claim_id in another item "
        "or section (number them consecutively, e.g. claim_1 ... claim_N). "
        "Do not infer intent or certainty beyond the cited record and do not decide RCA acceptance, penalties, "
        "SLA credits, or contract remedies. Compare the vendor RCA with incident timeline, monitoring, tickets, "
        "and changes for causal leaps, contradictions, and CAP/root-cause disconnects. "
        f"retrieved_records={json.dumps(provenance.get('records', []), ensure_ascii=False)}; "
        f"material={json.dumps(raw, ensure_ascii=False)}"
    )


def _normalise_draft(draft: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Validate—not repair—an LLM draft so its own citations remain auditable."""

    if not isinstance(draft, dict) or find_forbidden_keys(draft) or contains_forbidden_decision(draft):
        raise SecurityViolationError("draft contains prohibited decision content")
    normalized: dict[str, list[dict[str, Any]]] = {}
    if set(draft) != set(_DRAFT_SECTIONS):
        raise SecurityViolationError("draft has undeclared or missing output fields")
    claim_ids: set[str] = set()
    for section_name, section in _DRAFT_SECTIONS.items():
        candidate = draft.get(section_name)
        if not isinstance(candidate, list) or not candidate or len(candidate) > _DRAFT_SECTION_LIMIT:
            raise SecurityViolationError("draft section has an invalid item count")
        entries = project_typed_findings(candidate, section.fields)
        for item in entries:
            identifier = item[section.identifier_field]
            if not isinstance(identifier, str) or not SAFE_ID_RE.fullmatch(identifier):
                raise SecurityViolationError("draft item identifier is invalid")
            if section.identifier_field == _CLAIM_ID_FIELD:
                if identifier in claim_ids:
                    raise SecurityViolationError("draft claim identifier is duplicated")
                claim_ids.add(identifier)
            source_ids = item[_SOURCE_IDS_FIELD]
            if not source_ids:
                raise SecurityViolationError("draft item has no source ids")
            item[_SOURCE_IDS_FIELD] = require_safe_ids(source_ids)
            if section.identifier_field == _SOURCE_ID_FIELD and identifier not in item[_SOURCE_IDS_FIELD]:
                raise SecurityViolationError("draft evidence source identifier is not cited")
            if section.text_field is not None:
                text = item[section.text_field]
                if not text.strip() or len(text) > _DRAFT_TEXT_MAX_CHARS:
                    raise SecurityViolationError("draft item prose is invalid")
            if has_pii_or_sensitive_operation(item):
                raise SecurityViolationError("draft item is unsafe")
        normalized[section_name] = entries
    return normalized


def _build_evidence(state: dict, draft: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Build the map from the model's own claim IDs and citation lists."""

    normalized = _normalise_draft(draft)
    evidence_map: list[dict[str, Any]] = []
    cited: set[str] = set()
    for section_name, section in _DRAFT_SECTIONS.items():
        if section.text_field is None:
            continue
        for item in normalized[section_name]:
            source_ids = item[_SOURCE_IDS_FIELD]
            evidence_map.append(
                {
                    _CLAIM_ID_FIELD: item[_CLAIM_ID_FIELD],
                    "claim_text": item[section.text_field],
                    _SOURCE_IDS_FIELD: source_ids,
                }
            )
            cited.update(source_ids)
    for item in normalized[_EVIDENCE_SOURCES_SECTION]:
        cited.update(item[_SOURCE_IDS_FIELD])
    if not evidence_map or not cited:
        raise SecurityViolationError("draft has no source-linked claims")
    return evidence_map, sorted(cited)


class RebuttalDraftNode(LlmDraftStageNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, services: DomainServices) -> None:
        super().__init__(
            config={"llm": services.llm},
            hooks=DraftStageHooks(build_prompt=_build_prompt, build_fallback=_fallback_brief, build_evidence=_build_evidence),
        )
        self._services = services

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        try:
            transient = dict(state)
            transient["_raw_payload"] = _resolve_payload(self._services, state)
            transient["_provenance"] = _resolve_provenance(self._services, state)
            result = super().execute(transient)
            if result.get("status") == AgentStatus.ERROR.value:
                return result
            # Only an unavailable/failed LLM may use the deterministic,
            # source-linked fallback.  A malformed successful LLM response is
            # rejected rather than repaired with invented citations.
            brief = _fallback_brief(transient) if result.get("degradation_reason") else _normalise_draft(result["draft"])
            evidence_map, cited_source_ids = _build_evidence(transient, brief)
            brief_ref = self._services.brief_store.put(
                brief, scope=_scope(state), session_id=str(state.get("session_id", ""))
            )
            proof_ref = self._services.provenance_store.put(
                {
                    "records": transient["_provenance"]["records"],
                    "stage1_current_ids": transient["_provenance"].get("stage1_current_ids", []),
                    "evidence_map": evidence_map,
                    "cited_source_ids": cited_source_ids,
                },
                scope=_scope(state),
                session_id=str(state.get("session_id", "")),
            )
            self._services.provenance_store.delete(
                state.get("provenance_ref"), scope=_scope(state), session_id=str(state.get("session_id", ""))
            )
        except Exception as exc:
            emit_trace_event("rebuttal_draft_rejected", {"reason": type(exc).__name__}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["RebuttalDraftNode: safe brief could not be produced"]}
        emit_trace_event("rebuttal_draft_stored", {"cited_count": len(result.get("cited_source_ids", []))}, state)
        return {
            "output": {"brief_ref": brief_ref},
            "proof_ref": proof_ref,
            "requires_human_review": True,
            "status": AgentStatus.SUCCESS.value,
            **({"degradation_reason": result["degradation_reason"]} if result.get("degradation_reason") else {}),
        }


class _DomainPayloadResolver:
    """Adapt the vaulted API payload to the row part without weakening scope."""

    def __init__(self, *, services: DomainServices, file_config: dict[str, Any]) -> None:
        self._services = services
        self._file_config = dict(file_config)

    def resolve(
        self,
        ref: str | None,
        *,
        scope: Mapping[str, str],
        session_id: str,
        consume: bool = True,
    ) -> dict[str, list[dict[str, Any]]]:
        value = self._services.raw_payload_store.resolve(
            ref,
            scope=scope,
            session_id=session_id,
            consume=consume,
        )
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, dict):
            raise ValueError("payload_shape_invalid")
        rows = _rows_from_payload(value, self._file_config)
        if not rows:
            raise ValueError("no_sanitized_records")
        # Preflight the whole batch before the part calls any write hook.  This
        # preserves the domain's all-or-nothing validation guarantee while the
        # part still owns scoped resolve, per-row orchestration and summary.
        for row in rows:
            if not isinstance(row, dict) or not any(row.get(key) not in (None, "") for key in ("verification_viewpoint", "category")):
                raise ValueError("evaluation_required_field_missing")
            _sanitize_evaluation(row)
        return {"records": rows}


class IngestWriteNode(IngestRowPipelineNode):
    """Request-bound ingest/feedback path using the common row pipeline."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    required_fields: ClassVar[tuple[str, ...]] = ("evaluation_id", "observed_at")
    required_any_fields: ClassVar[tuple[tuple[str, ...], ...]] = (("verification_viewpoint", "category"),)

    def __init__(
        self,
        *,
        services: DomainServices,
        file_config: dict[str, Any],
        trusted_scope: Mapping[str, str] | None,
        session_id: str | None,
        feedback: bool = False,
    ) -> None:
        self._services = services
        self._feedback = feedback
        self._trusted_scope = dict(trusted_scope) if isinstance(trusted_scope, Mapping) else None
        self._request_session_id = session_id
        super().__init__(
            payload_store=_DomainPayloadResolver(services=services, file_config=file_config),
            trusted_scope=trusted_scope,
            session_id=session_id,
            hooks=RowPipelineHooks(
                sanitize=lambda _state, row: _sanitize_evaluation(row),
                normalize=lambda _state, row: _normalize_evaluation(row),
                write_batch=self._write_sanitized_batch,
            ),
        )

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        result = super().execute(state)
        if result.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("evaluation_write_rejected", {"feedback": self._feedback}, state)
            return result
        summary = result.get("ingest_summary")
        if not isinstance(summary, dict):
            return {"status": AgentStatus.ERROR.value, "error_log": ["IngestWriteNode: summary missing"]}
        summary = dict(summary)
        if summary.get("rejected_count"):
            emit_trace_event("evaluation_write_rejected", {"feedback": self._feedback}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["IngestWriteNode: sanitized batch was not written"],
            }
        summary["evaluation_refs"] = summary.pop("written_ids", [])
        emit_trace_event(
            "evaluation_write_complete",
            {"feedback": self._feedback, "written_count": summary.get("written_count", 0)},
            state,
        )
        key = "feedback_summary" if self._feedback else "ingest_summary"
        return {key: summary, "status": AgentStatus.SUCCESS.value}

    def _write_sanitized_batch(
        self,
        _state: dict[str, Any],
        rows: list[dict[str, Any]],
    ) -> list[str]:
        if self._trusted_scope is None or not self._request_session_id:
            raise ValueError("request_context_missing")
        try:
            ids = self._services.repository.write_batch(
                rows,
                scope=self._trusted_scope,
                feedback=self._feedback,
            )
            if len(ids) != len(rows):
                raise ValueError("repository_write_failed")
            _write_kb_projections(self._services, rows, self._trusted_scope)
        except Exception:
            # The common row part includes hook exception text in its batch
            # rejection summary.  Collapse repository/adapter exceptions to a
            # stable code before crossing that boundary.
            raise ValueError("repository_write_failed") from None
        return ids


def _rows_from_payload(payload: dict[str, Any], file_config: dict[str, Any]) -> list[dict[str, Any]]:
    records = payload.get("records")
    if isinstance(records, list):
        return [record for record in records if isinstance(record, dict)]
    extra = payload.get("__extra__", {})
    if isinstance(extra, dict) and isinstance(extra.get("payload.file_b64"), str):
        raw = base64.b64decode(extra["payload.file_b64"], validate=True)
        allowed_channels, max_file_bytes, quality_policy = _file_sanitize_policy(file_config)
        result = sanitize_file_bytes(
            raw,
            allowed_channels=allowed_channels,
            max_file_bytes=max_file_bytes,
            quality_policy=quality_policy,
        )
        if not result.accepted:
            raise ValueError("ingest_file_rejected")
        return result.sanitized_rows
    raise ValueError("evaluation_records_missing")


def _file_sanitize_policy(file_config: dict[str, Any]) -> tuple[tuple[str, ...], int, TextQualityPolicy]:
    """Read the manifest-declared quality gate; unsupported config fails closed."""

    channels = file_config.get("allowed_channels")
    max_file_bytes = file_config.get("max_file_bytes")
    quality = file_config.get("quality")
    if channels != ["csv"] or not isinstance(max_file_bytes, int) or max_file_bytes < 1 or not isinstance(quality, dict):
        raise ValueError("ingest_file_config_invalid")
    values = {
        "min_text_chars": quality.get("min_text_chars"),
        "min_printable_ratio": quality.get("min_printable_ratio"),
        "max_replacement_ratio": quality.get("max_replacement_ratio"),
        "max_control_ratio": quality.get("max_control_ratio"),
        "min_signal_ratio": quality.get("min_signal_ratio"),
    }
    if not isinstance(values["min_text_chars"], int) or values["min_text_chars"] < 1:
        raise ValueError("ingest_file_config_invalid")
    if any(not isinstance(value, (int, float)) for key, value in values.items() if key != "min_text_chars"):
        raise ValueError("ingest_file_config_invalid")
    return (
        ("csv",),
        max_file_bytes,
        TextQualityPolicy(
            min_text_chars=values["min_text_chars"],
            min_printable_ratio=float(values["min_printable_ratio"]),
            max_replacement_ratio=float(values["max_replacement_ratio"]),
            max_control_ratio=float(values["max_control_ratio"]),
            min_signal_ratio=float(values["min_signal_ratio"]),
        ),
    )


def _sanitize_evaluation(row: dict[str, Any]) -> dict[str, Any]:
    required = ("evaluation_id", "observed_at")
    if any(not isinstance(row.get(key), str) or not row[key].strip() for key in required):
        raise ValueError("evaluation_required_field_missing")
    if not SAFE_ID_RE.fullmatch(row["evaluation_id"]):
        raise ValueError("evaluation_id_invalid")
    category = str(row.get("category", "causal_evidence"))
    if category not in {"causal_evidence", "log_consistency", "cap_connection", "data_quality"}:
        raise ValueError("evaluation_category_invalid")
    viewpoint = str(row.get("verification_viewpoint", ""))
    if len(viewpoint) > 240 or has_pii_or_sensitive_operation(viewpoint) or contains_forbidden_decision(viewpoint):
        raise ValueError("evaluation_viewpoint_unsafe")
    source_ids = require_safe_ids(row.get("source_ids", [])) if isinstance(row.get("source_ids", []), list) else []
    # ``version`` becomes a segment of the composed storage key
    # ("{tenant}:{case}:{evaluation_id}:{version}:{digest}"), so it must obey the
    # same contract as the other segments.  Truncating alone would let a value
    # containing ":" or whitespace through and corrupt the key structure -- and
    # only after the row had already been written.  Reject before persistence.
    version = str(row.get("version", "1"))
    if not SAFE_ID_RE.fullmatch(version):
        raise ValueError("evaluation_version_invalid")
    return {
        "evaluation_id": row["evaluation_id"],
        "observed_at": row["observed_at"][:32],
        "category": category,
        "causal_strength": str(row.get("causal_strength", "unknown"))[:24],
        "verification_viewpoint": viewpoint,
        "source_ids": source_ids,
        "version": version,
    }


def _normalize_evaluation(row: dict[str, Any]) -> dict[str, Any]:
    """Use the retrieval normalizer on the exact text persisted for search."""

    normalized = dict(row)
    normalized["search_text"] = normalize_kb_query(
        f"{normalized['category']} {normalized['verification_viewpoint']}"
    )
    return normalized


def _write_kb_projections(
    services: DomainServices,
    rows: list[dict[str, Any]],
    scope: Mapping[str, str],
) -> None:
    backend = services.retrieval
    add_projection = getattr(backend, "add_projection", None)
    if not callable(add_projection):
        return
    for row in rows:
        add_projection(
            {
                "source_id": f"precedent_{row['evaluation_id']}",
                "summary": row["search_text"],
                "metadata": {
                    "tenant_id": scope["tenant_id"],
                    "case_ref": scope["case_ref"],
                    "category": row["category"],
                    "record_type": "evaluation_viewpoint",
                },
            }
        )


class DomainWorkflowGraph(BaseGraph):
    """Compiled once inside one request-scoped outer GraphNode."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        services: DomainServices,
        trusted_scope: Mapping[str, str] | None = None,
        request_session_id: str | None = None,
    ) -> None:
        super().__init__(config=config)
        self._services = services
        self._trusted_scope = dict(trusted_scope) if trusted_scope is not None else None
        self._request_session_id = request_session_id

    @property
    def name(self) -> str:
        return "svc-c2-098-domain-workflow"

    @property
    def state_schema(self) -> type:
        return InnerState

    def _validate_config(self) -> None:
        return None

    def register_nodes(self) -> None:
        self._nodes = {
            "parse_envelope": ParseEnvelopeNode(),
            "deterministic_check": DeterministicCheckNode(services=self._services),
            "precedent_recall": PrecedentRecallNode(
                services=self._services,
                scope_keys=("tenant_id", "case_ref"),
                backend=self._services.retrieval,
                normalize_query=normalize_kb_query,
                top_k=int(self.config.get("top_k", 5)),
            ),
            "rebuttal_draft": RebuttalDraftNode(services=self._services),
            "ingest": IngestWriteNode(
                services=self._services,
                file_config=self.config.get("ingest_file", {}),
                trusted_scope=self._trusted_scope,
                session_id=self._request_session_id,
            ),
            "feedback": IngestWriteNode(
                services=self._services,
                file_config=self.config.get("ingest_file", {}),
                trusted_scope=self._trusted_scope,
                session_id=self._request_session_id,
                feedback=True,
            ),
        }

    def add_edges(self) -> None:
        self._sg.add_edge(START, "parse_envelope")
        self._sg.add_conditional_edges(
            "parse_envelope",
            lambda state: state.get("mode", "error"),
            {"invoke": "deterministic_check", "ingest": "ingest", "feedback": "feedback", "error": END},
        )
        self._sg.add_edge("deterministic_check", "precedent_recall")
        self._sg.add_edge("precedent_recall", "rebuttal_draft")
        self._sg.add_edge("rebuttal_draft", END)
        self._sg.add_edge("ingest", END)
        self._sg.add_edge("feedback", END)

    def route(self, state):
        return state.get("mode", "error")

    def get_output(self, state) -> dict:
        mode = state.get("mode")
        output: dict[str, Any] = {"status": state.get("status", AgentStatus.ERROR.value), "mode": mode}
        if output["status"] == AgentStatus.ERROR.value:
            output["error_log"] = state.get("error_log", [])
            if state.get("error_code"):
                output["error_code"] = state["error_code"]
            if state.get("error_type"):
                output["error_type"] = state["error_type"]
            return output
        if mode == "invoke":
            for key in ("output", "proof_ref", "requires_human_review", "degradation_reason"):
                if key in state:
                    output[key] = state[key]
        elif mode == "ingest":
            output["ingest_summary"] = state.get("ingest_summary", {})
        elif mode == "feedback":
            output["feedback_summary"] = state.get("feedback_summary", {})
        else:
            output = {"status": AgentStatus.ERROR.value, "mode": mode, "error_log": ["inner_mode_invalid"]}
        return output
