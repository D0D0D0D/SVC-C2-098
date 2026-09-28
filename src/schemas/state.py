"""Checkpoint-safe outer state for SVC-C2-098.

Only identifiers, enums, and small control values belong here.  Raw RCA text,
monitoring payloads, and generated briefs remain in scope-bound transient
stores and are represented in the graph by opaque references only.
"""

from typing import NotRequired

from framework.schemas.agent_state import AgentState


class State(AgentState, total=False):
    """Flat, msgpack-safe state used by the outer AgentBaseGraph."""

    mode: str
    tenant_id: str
    case_ref: str
    validated_input: str
    payload_ref: str
    result: dict
    formatted_output: dict
    gate_kind: str
    requires_human_review: bool
    ingest_summary: NotRequired[dict]
    feedback_summary: NotRequired[dict]
    error_code: str
    error_type: str
