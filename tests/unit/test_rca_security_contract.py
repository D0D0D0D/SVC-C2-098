"""TC-01..08 and SVC-C2-098-specific S-2/S-3 unit coverage."""

from __future__ import annotations

import inspect

import pytest
from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import SlaRcaGraphNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State
from src.services.service import wire_services


def _main_node() -> SlaRcaGraphNode:
    return SlaRcaGraphNode(config={"hitl": {"enabled": False}, "memory_enabled": False}, services=wire_services())


def _draft(**overrides):
    result = {
        "gate_kind": "draft",
        "status": "success",
        "result": {"brief_ref": "brief_ref_1"},
        "requires_human_review": True,
        "cited_source_ids": ["log_1", "precedent_1"],
        "retrieved_records": [
            {"source_id": "log_1", "record_kind": "current_evidence", "stage": "stage1", "tenant_id": "tenant1", "case_id": "case1"},
            {"source_id": "precedent_1", "record_kind": "precedent", "tenant_id": "tenant1", "case_id": "case1"},
        ],
        "evidence_map": [{"claim_id": "c1", "source_ids": ["log_1"]}],
        "stage1_current_ids": ["log_1"],
    }
    result.update(overrides)
    return result


def test_tc01_state_is_flat_typed_dict_with_ref_fields_only():
    annotations = State.__annotations__
    assert {"payload_ref", "result", "formatted_output", "tenant_id", "case_ref"} <= set(annotations)
    assert not {"token", "api_key", "secret", "rca_text", "monitoring_payload"} & set(annotations)


def test_tc02_unknown_citation_is_security_violation():
    with pytest.raises(SecurityViolationError, match="non-existent"):
        _main_node()._extra_security_gate_output(_draft(cited_source_ids=["invented_id"]))


def test_tc03_forbidden_decision_field_is_structurally_rejected_nested():
    with pytest.raises(SecurityViolationError, match="prohibited decision field"):
        _main_node()._extra_security_gate_output(_draft(result={"brief_ref": "brief_ref_1", "nested": {"penalty_amount": 1}}))


def test_tc04_canonical_input_uses_refs_not_invocation_context():
    node = _main_node()
    serialized = node.extract_input(
        {"mode": "invoke", "tenant_id": "tenant1", "case_ref": "case1", "payload_ref": "server-ref"}
    )
    assert "InvocationContext" not in serialized
    assert serialized == '{"case_ref": "case1", "mode": "invoke", "payload_ref": "server-ref", "tenant_id": "tenant1"}'


def test_tc05_no_lifecycle_event_is_emitted_by_domain_execute_source():
    for node in (PreProcessNode, PostProcessNode):
        source = inspect.getsource(node.execute)
        assert "node_start" not in source and "node_complete" not in source and "node_error" not in source


def test_tc06_tc07_function_nodes_use_extra_hooks_not_final_gate_override():
    for node in (PreProcessNode, PostProcessNode):
        assert "_security_gate_input" not in node.__dict__
        assert "_security_gate_output" not in node.__dict__
        assert "_extra_security_gate" in inspect.getsource(node)


def test_tc08_verified_trust_is_required():
    node = PreProcessNode()
    result = node(
        {
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
            "user_input": "{}",
            "input_context": {"scope": {}},
            "correlation_id": "tc08",
        }
    )
    assert result["status"] == AgentStatus.ERROR.value


def test_precedent_cannot_be_used_as_current_evidence():
    draft = _draft(evidence_map=[{"claim_id": "c1", "source_ids": ["precedent_1"]}])
    with pytest.raises(SecurityViolationError, match="current_evidence"):
        _main_node()._extra_security_gate_output(draft)


def test_medium1_record_kind_self_declaration_without_stage1_origin_is_rejected():
    draft = _draft(
        retrieved_records=[
            {
                "source_id": "forged_precedent",
                "record_kind": "current_evidence",
                "stage": "stage2",
                "tenant_id": "tenant1",
                "case_id": "case1",
            }
        ],
        stage1_current_ids=["log_1"],
        cited_source_ids=["forged_precedent"],
        evidence_map=[{"claim_id": "c1", "source_ids": ["forged_precedent"]}],
    )
    with pytest.raises(SecurityViolationError, match="current evidence"):
        _main_node()._extra_security_gate_output(draft)


def test_validated_draft_projects_provenance_canonical_data_out_of_outer_state():
    output = _main_node()._extra_security_gate_output(_draft())
    assert output == {
        "gate_kind": "draft",
        "status": "success",
        "result": {"brief_ref": "brief_ref_1"},
        "requires_human_review": True,
    }


def test_inner_evidence_scope_mismatch_is_rejected_before_merge():
    node = _main_node()
    proof_ref = node._services.provenance_store.put(
        {
            "records": [
                {"source_id": "log_1", "record_kind": "current_evidence", "stage": "stage1", "tenant_id": "tenant2", "case_id": "case1"}
            ],
            "stage1_current_ids": ["log_1"],
            "evidence_map": [{"claim_id": "c1", "source_ids": ["log_1"]}],
            "cited_source_ids": ["log_1"],
        },
        scope={"tenant_id": "tenant1", "case_ref": "case1"},
        session_id="s1",
    )
    with pytest.raises(SecurityViolationError, match="scope mismatch"):
        node.merge_output(
            {"mode": "invoke", "tenant_id": "tenant1", "case_ref": "case1", "session_id": "s1"},
            {
                "status": "success",
                "mode": "invoke",
                "output": {"brief_ref": "brief_ref_1"},
                "proof_ref": proof_ref,
                "requires_human_review": True,
            },
        )
