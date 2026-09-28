"""Unit checks for the Cat2 main-slot GraphNode contract."""

import inspect

from framework.schemas.agent_status import AgentStatus

from src.graph.graph import SlaRcaGraphNode
from src.services.service import wire_services


def _node() -> SlaRcaGraphNode:
    return SlaRcaGraphNode(config={"hitl": {"enabled": False}, "memory_enabled": False}, services=wire_services())


def test_main_node_passes_upstream_error_without_starting_subgraph():
    node = _node()
    assert node.execute({"status": AgentStatus.ERROR.value}) == {}
    assert node._subgraph is None


def test_main_node_canonical_envelope_contains_only_safe_refs():
    envelope = _node().extract_input(
        {"mode": "invoke", "tenant_id": "tenant1", "case_ref": "case1", "payload_ref": "opaque"}
    )
    assert "opaque" in envelope
    assert "user_input" not in envelope


def test_main_node_execute_contract_has_no_invoke_impl_override():
    signature = inspect.signature(SlaRcaGraphNode.execute)
    assert list(signature.parameters) == ["self", "state"]
    assert "_invoke_impl" not in SlaRcaGraphNode.__dict__
