"""Outer AgentBaseGraph for the SVC-C2-098 Cat2 workflow."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.graph.agent_base_graph import AgentBaseGraph
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.cat2_graph_skeleton import DomainWorkflowGraphNode, ModeSpec
from src.evidence_gate import ProvenanceContract, validate_provenance
from src.graph.domain_workflow_graph import DomainWorkflowGraph
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.security import SAFE_ID_RE, find_forbidden_keys
from src.schemas.state import State
from src.services.service import DomainServices, wire_services


# What the HTTP layer may name back to a caller: input-contract failures the
# caller can actually correct. A provider fault tells them nothing actionable,
# so it stays the generic message.
CALLER_FIXABLE_ERROR_CODES: frozenset[str] = frozenset(
    {"canonical_envelope_invalid", "current_evidence_required", "inner_mode_invalid"}
)


class SlaRcaGraphNode(DomainWorkflowGraphNode):
    """Main-slot GraphNode with S-3 provenance enforcement at its boundary."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    error_strategy: ClassVar[str] = "handle"
    propagate_hitl: ClassVar[bool] = False
    # Codes the part may surface instead of its generic "subgraph_failed".
    # The two llm_* codes predate the input-contract ones and describe provider
    # faults, not caller mistakes; they are kept for the graph's own output but
    # are NOT in CALLER_FIXABLE_ERROR_CODES below, which is what the HTTP layer
    # is allowed to name back to a caller.
    known_failure_codes: ClassVar[tuple[str, ...]] = (
        "llm_call_failed",
        "llm_output_parse_failed",
        "canonical_envelope_invalid",
        "current_evidence_required",
        "inner_mode_invalid",
    )
    mode_specs: ClassVar[dict[str, ModeSpec]] = {
        "invoke": ModeSpec(
            gate_kind="draft",
            fields={
                "result": "output",
                "evidence_map": "evidence_map",
                "cited_source_ids": "cited_source_ids",
                "retrieved_records": "retrieved_records",
                "requires_human_review": "requires_human_review",
                "error_code": "error_code",
                "error_type": "error_type",
                "error_log": "error_log",
            },
            required_fields=("result", "evidence_map", "cited_source_ids", "retrieved_records"),
        ),
        "ingest": ModeSpec(
            gate_kind="ingest",
            fields={
                "ingest_summary": "ingest_summary",
                "error_code": "error_code",
                "error_type": "error_type",
                "error_log": "error_log",
            },
        ),
        "feedback": ModeSpec(
            gate_kind="feedback",
            fields={
                "feedback_summary": "feedback_summary",
                "error_code": "error_code",
                "error_type": "error_type",
                "error_log": "error_log",
            },
        ),
    }
    _contract = ProvenanceContract(
        gate_kind="draft",
        retrieved_records_key="retrieved_records",
        cited_ids_key="cited_source_ids",
        evidence_map_key="evidence_map",
        record_id_keys=("source_id", "case_id"),
        passthrough_keys=("result", "degradation_reason", "requires_human_review"),
    )

    def __init__(
        self,
        *,
        config: dict[str, Any],
        services: DomainServices,
        trusted_scope: Mapping[str, str] | None = None,
        request_session_id: str | None = None,
        checkpointer_factory: Any | None = None,
        inner_memory_enabled: bool = False,
    ) -> None:
        self._services = services
        self._trusted_scope = dict(trusted_scope) if trusted_scope is not None else None
        self._request_session_id = request_session_id
        self._inner_memory_enabled = inner_memory_enabled
        super().__init__(
            config=config,
            checkpointer_factory=checkpointer_factory,
            use_inner_checkpointer=True,
        )

    def _parent_config(self) -> dict[str, Any]:
        """Forward only service seams/runtime controls to the cached subgraph."""

        return {
            "llm": self._config.get("llm"),
            "store": self._services.raw_payload_store,
            "retrieval": self._services.retrieval,
            "top_k": self._config.get("top_k", 5),
            "memory_enabled": self._inner_memory_enabled,
            "hitl": {"enabled": False},
            "ingest_file": self._config.get("ingest_file", {}),
        }

    def _build_subgraph(self):
        return DomainWorkflowGraph(
            self._parent_config(),
            services=self._services,
            trusted_scope=self._trusted_scope,
            request_session_id=self._request_session_id,
        )

    def execute(self, state: State) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("main_upstream_error_passthrough", {}, state)
            return {}
        emit_trace_event("main_subgraph_invoke", {"mode": state.get("mode", "")}, state)
        return super().execute(state)

    def extract_input(self, state: State) -> str:
        """Serialize a strict canonical envelope; never stringify arbitrary types."""

        envelope = {
            "mode": state.get("mode"),
            "tenant_id": state.get("tenant_id"),
            "case_ref": state.get("case_ref"),
            "payload_ref": state.get("payload_ref"),
        }
        if envelope["mode"] not in {"invoke", "ingest", "feedback"}:
            raise SecurityViolationError("SlaRcaGraphNode: mode is undeclared")
        if any(not isinstance(envelope[key], str) for key in ("tenant_id", "case_ref", "payload_ref")):
            raise SecurityViolationError("SlaRcaGraphNode: canonical envelope has non-string values")
        if any(not SAFE_ID_RE.fullmatch(envelope[key]) for key in ("tenant_id", "case_ref")):
            raise SecurityViolationError("SlaRcaGraphNode: canonical scope identifier is unsafe")
        return json.dumps(envelope, ensure_ascii=False, sort_keys=True)

    def merge_output(self, state: State, sub_result: dict) -> dict:
        """Enforce a closed business-key contract before the S-3 gate runs."""

        mode = state.get("mode")
        allowed = {
            "invoke": {"status", "mode", "output", "proof_ref", "requires_human_review", "degradation_reason"},
            "ingest": {"status", "mode", "ingest_summary"},
            "feedback": {"status", "mode", "feedback_summary"},
        }
        if mode not in allowed:
            raise SecurityViolationError("SlaRcaGraphNode: unknown mode cannot merge output")
        unexpected = set(sub_result) - allowed[mode]
        if unexpected:
            raise SecurityViolationError(f"SlaRcaGraphNode: undeclared inner output keys {sorted(unexpected)}")
        if mode == "invoke":
            scope = {"tenant_id": state["tenant_id"], "case_ref": state["case_ref"]}
            session_id = str(state.get("session_id", ""))
            proof_ref = sub_result.get("proof_ref")
            try:
                proof = self._services.provenance_store.resolve(
                    proof_ref,
                    scope=scope,
                    session_id=session_id,
                    consume=True,
                )
            finally:
                self._services.provenance_store.delete(
                    proof_ref,
                    scope=scope,
                    session_id=session_id,
                )
            if not isinstance(proof, dict):
                raise SecurityViolationError("SlaRcaGraphNode: proof reference is invalid")
            sub_result = {
                **sub_result,
                "evidence_map": proof.get("evidence_map"),
                "cited_source_ids": proof.get("cited_source_ids"),
                "retrieved_records": proof.get("records"),
                "stage1_current_ids": proof.get("stage1_current_ids"),
            }
            for record in sub_result.get("retrieved_records", []):
                if not isinstance(record, dict) or (
                    record.get("tenant_id") != state.get("tenant_id") or record.get("case_id") != state.get("case_ref")
                ):
                    raise SecurityViolationError("SlaRcaGraphNode: evidence record scope mismatch")
        # The proof reference is a boundary-only transport key, not part of
        # the Cat2 skeleton's state projection.
        sub_result.pop("proof_ref", None)
        delta = super().merge_output(state, sub_result)
        if mode == "invoke":
            delta["stage1_current_ids"] = sub_result["stage1_current_ids"]
        if delta.get("status") != AgentStatus.SUCCESS.value:
            raise SecurityViolationError("SlaRcaGraphNode: non-success inner outcome cannot carry business output")
        return delta

    def _extra_security_gate_input(self, state: State) -> State:
        """Independently check the explicit route declaration at the boundary."""

        if state.get("mode") not in self.mode_specs:
            raise SecurityViolationError("S-2 main gate: mode is not declared")
        for key in ("tenant_id", "case_ref", "payload_ref"):
            if not isinstance(state.get(key), str) or not state.get(key):
                raise SecurityViolationError(f"S-2 main gate: {key} is required")
        return state

    def _extra_security_gate_output(self, result: dict) -> dict:
        """Validate provenance then project all proof material out of outer State."""

        if result.get("status") == AgentStatus.ERROR.value:
            allowed = {"status", "error_code", "error_type", "error_log"}
            if set(result) - allowed:
                raise SecurityViolationError("S-3 main gate: undeclared error output")
            return result
        if find_forbidden_keys(result):
            raise SecurityViolationError("S-3 main gate: prohibited decision field")
        kind = result.get("gate_kind")
        if kind in {"ingest", "feedback"}:
            allowed = {"gate_kind", "status", "ingest_summary", "feedback_summary"}
            if set(result) - allowed:
                raise SecurityViolationError("S-3 main gate: undeclared write-path output")
            return result
        if kind != "draft":
            raise SecurityViolationError("S-3 main gate: output route was not declared")
        validate_provenance(result, self._contract)
        self._require_current_evidence_per_claim(result)
        brief = result.get("result")
        if not isinstance(brief, dict) or set(brief) != {"brief_ref"}:
            raise SecurityViolationError("S-3 main gate: draft result must contain only brief_ref")
        # `retrieved_records`, evidence_map and cited IDs are validation-only
        # material.  Returning them would place evidence canonical data in the
        # outer checkpointer, so project them out after successful validation.
        return {
            "gate_kind": "draft",
            "status": AgentStatus.SUCCESS.value,
            "result": {"brief_ref": brief["brief_ref"]},
            "requires_human_review": bool(result.get("requires_human_review", True)),
            **({"degradation_reason": result["degradation_reason"]} if result.get("degradation_reason") else {}),
        }

    def _require_current_evidence_per_claim(self, result: dict) -> None:
        stage1_ids = result.get("stage1_current_ids")
        if not isinstance(stage1_ids, list):
            raise SecurityViolationError("S-3 provenance gate: Stage1 origin IDs are missing")
        stage1_ids = set(map(str, stage1_ids))
        current_ids = {
            str(record.get("source_id") or record.get("case_id"))
            for record in result.get("retrieved_records", [])
            if isinstance(record, dict)
            and record.get("record_kind") == "current_evidence"
            and record.get("stage") == "stage1"
            and str(record.get("source_id") or record.get("case_id")) in stage1_ids
        }
        if not current_ids:
            raise SecurityViolationError("S-3 provenance gate: no current evidence record exists")
        for claim in result.get("evidence_map", []):
            if not isinstance(claim, dict):
                raise SecurityViolationError("S-3 provenance gate: claim must be an object")
            ids = claim.get("source_ids") or claim.get("supporting_source_ids")
            if not isinstance(ids, list) or not set(map(str, ids)) & current_ids:
                raise SecurityViolationError("S-3 provenance gate: each claim requires current_evidence")


class SlaRcaRebuttalBriefAgent(AgentBaseGraph):
    """Cat2 accumulation agent; RCA acceptance and remedies remain human work."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        services: DomainServices | None = None,
        trusted_scope: Mapping[str, str] | None = None,
        request_session_id: str | None = None,
        inner_checkpointer_factory: Any | None = None,
        inner_memory_enabled: bool = False,
    ) -> None:
        runtime = {
            "max_retry": 3,
            "timeout_s": 30,
            "budget": 0,
            "top_k": 5,
            "memory_enabled": False,
            "hitl": {"enabled": False},
            "ingest_file": {
                "allowed_channels": ["csv"],
                "max_file_bytes": 5_242_880,
                "quality": {
                    "min_text_chars": 4,
                    "min_printable_ratio": 0.85,
                    "max_replacement_ratio": 0.01,
                    "max_control_ratio": 0.02,
                    "min_signal_ratio": 0.15,
                },
            },
        }
        runtime.update(config or {})
        super().__init__(config=runtime)
        self._services = services or wire_services(runtime)
        self._trusted_scope = dict(trusted_scope) if trusted_scope is not None else None
        self._request_session_id = request_session_id
        self._inner_checkpointer_factory = inner_checkpointer_factory
        self._inner_memory_enabled = inner_memory_enabled

    @property
    def name(self) -> str:
        return "svc-c2-098"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = SlaRcaGraphNode(
            config=self.config,
            services=self._services,
            trusted_scope=self._trusted_scope,
            request_session_id=self._request_session_id,
            checkpointer_factory=self._inner_checkpointer_factory,
            inner_memory_enabled=self._inner_memory_enabled,
        )
        self._nodes["post_process"] = PostProcessNode()

    def get_output(self, state) -> dict:
        """Return refs only; HTTP resolution is intentionally outside the graph."""

        output = {
            "output": state.get("formatted_output") or state.get("result"),
            "status": state.get("status"),
            "trace_id": state.get("trace_id", ""),
            "correlation_id": state.get("correlation_id", ""),
        }
        if state.get("error_code"):
            output["error_code"] = state["error_code"]
        if state.get("error_type"):
            output["error_type"] = state["error_type"]
        if state.get("error_log"):
            output["error_log"] = list(dict.fromkeys(state.get("error_log", [])))
        return output
