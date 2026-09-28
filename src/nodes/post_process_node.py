"""Checkpoint-safe formatting for the opaque output reference."""

from __future__ import annotations

from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.nodes.security import contains_forbidden_decision, find_forbidden_keys, has_pii_or_sensitive_operation
from src.output_envelope import OutputEnvelopeNode
from src.payload_store import is_reference


class PostProcessNode(OutputEnvelopeNode):
    """Use the output-envelope part while retaining references in State."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    gate_kind_by_mode: ClassVar[dict[str, str]] = {
        "invoke": "draft",
        "ingest": "ingest",
        "feedback": "feedback",
    }
    required_fields_by_mode: ClassVar[dict[str, tuple[str, ...]]] = {
        "invoke": ("mode", "gate_kind", "brief_ref"),
        "ingest": ("mode", "gate_kind", "ingest_summary"),
        "feedback": ("mode", "gate_kind", "feedback_summary"),
    }
    max_field_chars: ClassVar[int | None] = 512

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        result = super().execute(state)
        emit_trace_event("rca_output_reference_formatted", {"mode": state.get("mode", "")}, state)
        return result

    def format_invoke(self, state: dict) -> dict[str, Any]:
        result = state.get("result")
        if not isinstance(result, dict) or set(result) != {"brief_ref"}:
            raise SecurityViolationError("PostProcessNode: invoke result must be a brief_ref only")
        return {"brief_ref": result["brief_ref"]}

    #: Keys whose values are minted wholly by this agent and are therefore
    #: exempt from the PII scan (after each value proves it really was minted).
    #:
    #: ``evaluation_refs`` is deliberately NOT here.  Those are composed storage
    #: keys that embed the caller-supplied ``evaluation_id`` verbatim, so their
    #: shape is not proof of provenance: an ``evaluation_id`` of
    #: ``090-1234-5678`` satisfies ``SAFE_ID_RE`` and would smuggle real PII
    #: past the scan.  They are scanned like any other content.
    opaque_ref_keys: ClassVar[frozenset[str]] = frozenset({"brief_ref"})

    def _opaque_ref_free_view(self, value: Any) -> Any:
        """Return ``value`` with the agent-minted opaque references removed.

        ``brief_ref`` is minted end to end by ``PayloadStore.put()``; no caller,
        LLM or document text reaches it, so it cannot carry PII.  Historically it
        was a ``uuid4``, whose digit-and-hyphen shape collided with the
        ``detect_pii`` credit-card / phone / My-Number patterns and turned a
        successful invoke into a 422 at that rate.  References are now digit-free
        (payload-store v0.4.0), and this view additionally keeps the scan off
        them so the gate does not depend on the detector's precision.

        Each excluded value is separately verified to BE an agent-minted
        reference, so this narrows the scan without opening a channel through
        which free text could skip S-3.  Only values whose *provenance* is the
        agent qualify — a value that merely embeds caller-supplied text in a
        machine-looking shape does not (see :attr:`opaque_ref_keys`).
        """

        if isinstance(value, dict):
            return {
                key: self._opaque_ref_free_view(item)
                for key, item in value.items()
                if key not in self.opaque_ref_keys
            }
        if isinstance(value, list):
            return [self._opaque_ref_free_view(item) for item in value]
        return value

    def _collect_opaque_refs(self, value: Any) -> list[Any]:
        """Gather every value stored under an opaque-ref key, at any depth."""

        found: list[Any] = []
        if isinstance(value, dict):
            for key, item in value.items():
                if key in self.opaque_ref_keys:
                    found.extend(item if isinstance(item, list) else [item])
                else:
                    found.extend(self._collect_opaque_refs(item))
        elif isinstance(value, list):
            for item in value:
                found.extend(self._collect_opaque_refs(item))
        return found

    @staticmethod
    def _is_agent_minted_ref(value: Any) -> bool:
        # Uses the part's own shape definition rather than a hand-rolled regex,
        # so the gate cannot drift from what ``PayloadStore.put()`` mints.  Only
        # values the agent mints end to end qualify -- a shape that merely embeds
        # caller-supplied text (see ``opaque_ref_keys``) is not provenance.
        return is_reference(value)

    def _extra_security_gate_output(self, result: dict) -> dict:
        result = super()._extra_security_gate_output(result)
        formatted = result.get("formatted_output", {})
        if find_forbidden_keys(formatted):
            raise SecurityViolationError("S-3 output gate: prohibited acceptance/remedy field")
        # Decision language is checked over the WHOLE output: the exemption below
        # narrows only the PII scan, and must never widen into other checks.
        if contains_forbidden_decision(formatted):
            raise SecurityViolationError("S-3 output gate: unsafe client-visible content")

        # Every value skipped by the PII scan must prove it is agent-minted;
        # free text never qualifies, so it can never buy its way out of S-3.
        for ref in self._collect_opaque_refs(formatted):
            if not self._is_agent_minted_ref(ref):
                raise SecurityViolationError("S-3 output gate: opaque reference is not agent-minted")

        if has_pii_or_sensitive_operation(self._opaque_ref_free_view(formatted)):
            raise SecurityViolationError("S-3 output gate: unsafe client-visible content")
        if result.get("status") not in {AgentStatus.SUCCESS.value, AgentStatus.ERROR.value}:
            raise SecurityViolationError("S-3 output gate: undeclared terminal status")
        return result
