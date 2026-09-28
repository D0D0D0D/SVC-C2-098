"""S-2 validation for the server-issued canonical invocation envelope."""

from __future__ import annotations

import json
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.nodes.security import SAFE_ID_RE


_MODES = frozenset({"invoke", "ingest", "feedback"})
_ENVELOPE_KEYS = frozenset({"mode", "tenant_id", "case_ref", "payload_ref", "__unparsable__"})


class PreProcessNode(FunctionNode):
    """Accept only a non-sensitive, server-issued canonical envelope."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        envelope, error = self._canonical_envelope(state)
        if error:
            emit_trace_event("rca_envelope_rejected", {"reason": error}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": [f"PreProcessNode: {error}"]}
        emit_trace_event(
            "rca_envelope_validated",
            {"mode": envelope["mode"], "has_payload_ref": True},
            state,
        )
        return {
            "mode": envelope["mode"],
            "tenant_id": envelope["tenant_id"],
            "case_ref": envelope["case_ref"],
            "payload_ref": envelope["payload_ref"],
            "validated_input": json.dumps(envelope, ensure_ascii=False, sort_keys=True),
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        """Reject uncanonicalized/enlarged input before graph checkpointing."""

        raw = state.get("user_input", "")
        if not isinstance(raw, str) or len(raw) > 4096:
            state["status"] = AgentStatus.ERROR.value
            state["error_log"] = ["PreProcessNode: canonical envelope is missing or oversized"]
            return state
        # `input_context.scope` is server-created from authenticate(), not
        # client headers.  Its values are checked again in execute.
        context = state.get("input_context")
        if not isinstance(context, dict) or not isinstance(context.get("scope"), dict):
            state["status"] = AgentStatus.ERROR.value
            state["error_log"] = ["PreProcessNode: verified scope is missing"]
        return state

    @staticmethod
    def _canonical_envelope(state: dict) -> tuple[dict[str, str], str | None]:
        try:
            parsed = json.loads(state.get("user_input", ""))
        except (TypeError, json.JSONDecodeError):
            return {}, "canonical envelope is not JSON"
        if not isinstance(parsed, dict) or set(parsed) - _ENVELOPE_KEYS:
            return {}, "canonical envelope contains undeclared fields"
        scope = state.get("input_context", {}).get("scope", {})
        if not isinstance(scope, dict):
            return {}, "verified scope is missing"
        mode = parsed.get("mode")
        tenant_id = parsed.get("tenant_id")
        case_ref = parsed.get("case_ref")
        payload_ref = parsed.get("payload_ref")
        if mode not in _MODES:
            return {}, "mode is undeclared"
        if not all(isinstance(value, str) and SAFE_ID_RE.fullmatch(value) for value in (tenant_id, case_ref)):
            return {}, "tenant_id/case_ref must be safe IDs"
        if tenant_id != scope.get("tenant_id") or case_ref != scope.get("case_ref"):
            return {}, "canonical scope does not match authenticated scope"
        if not isinstance(payload_ref, str) or not payload_ref:
            return {}, "server payload_ref is missing"
        return {"mode": mode, "tenant_id": tenant_id, "case_ref": case_ref, "payload_ref": payload_ref}, None
