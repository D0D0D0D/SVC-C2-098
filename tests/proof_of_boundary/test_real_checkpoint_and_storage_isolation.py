"""PB-3/PB-5: real saver and sqlite seams enforce isolation."""

from __future__ import annotations

import json
import sqlite3

import pytest
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from langgraph.checkpoint.memory import InMemorySaver

from src.graph.graph import SlaRcaRebuttalBriefAgent
from src.scoped_retrieval import ScopedRetrievalNode
from src.services.service import SQLiteRcaRepository, SQLiteScopedRetrieval, wire_services


def _brief_with_fact(statement: str) -> dict:
    return {
        "facts": [{"statement": statement, "source_ids": ["log_1"]}],
        "evidence_sources": [{"source_ids": ["log_1"]}],
        "unresolved_points": [{"point": "Current evidence needs review.", "source_ids": ["log_1"]}],
        "countervailing_observations": [{"observation": "Compare the source record.", "source_ids": ["log_1"]}],
        "review_questions": [{"question": "Which source supports this point?", "source_ids": ["log_1"]}],
    }


# SVC-C2-098 B-5: PII detection relies solely on the framework-standard
# ``detect_pii``; the bespoke Japanese-name supplement was removed. The response
# boundary still rejects the PII classes ``detect_pii`` covers (email / phone).
@pytest.mark.parametrize("statement", ["連絡先は 090-1234-5678", "メール taro@example.com"])
def test_pb_response_boundary_rejects_framework_detected_pii(statement):
    from src.services import resolve_invoke_output

    services = wire_services()
    scope = {"tenant_id": "tenant1", "case_ref": "case1"}
    ref = services.brief_store.put(_brief_with_fact(statement), scope=scope, session_id="fw-pii-session")
    with pytest.raises(ValueError, match="response_pii_detected"):
        resolve_invoke_output(services.brief_store, ref, scope=scope, session_id="fw-pii-session")


def test_pb_response_boundary_keeps_normal_japanese_operational_vocabulary():
    from src.services import resolve_invoke_output

    services = wire_services()
    scope = {"tenant_id": "tenant1", "case_ref": "case1"}
    ref = services.brief_store.put(
        _brief_with_fact("監視値と時系列を照合する。"),
        scope=scope,
        session_id="jp-business-session",
    )
    output = resolve_invoke_output(services.brief_store, ref, scope=scope, session_id="jp-business-session")
    assert output["facts"][0]["statement"].startswith("監視値と時系列")


def test_pb5_real_outer_and_inner_checkpoint_snapshots_hold_only_refs():
    services = wire_services()
    scope = {"tenant_id": "tenant1", "case_ref": "case1"}
    session_id = "checkpoint-session"
    raw_rca = "raw RCA body must not be checkpointed"
    raw_monitoring = "raw monitoring body must not be checkpointed"
    ref = services.raw_payload_store.put(
        json.dumps(
            {
                "records": [{"source_id": "log_1", "source_type": "monitor"}],
                "__extra__": {"rca": raw_rca, "monitoring": raw_monitoring, "sla_definition": "x", "measurement_period": "2026-07"},
            }
        ),
        scope=scope,
        session_id=session_id,
    )
    outer_saver = InMemorySaver()
    inner_saver = InMemorySaver()
    agent = SlaRcaRebuttalBriefAgent(
        config={"memory_enabled": True},
        services=services,
        trusted_scope=scope,
        request_session_id=session_id,
        inner_checkpointer_factory=lambda: inner_saver,
        inner_memory_enabled=True,
    )
    agent.compile(checkpointer=outer_saver)
    result = agent.invoke(
        json.dumps({"mode": "invoke", **scope, "payload_ref": ref}),
        ctx=InvocationContext(session_id=session_id, caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        input_context={"scope": scope},
    )
    assert result["status"] == "success"
    snapshots = [*outer_saver.list(None), *inner_saver.list(None)]
    assert snapshots
    serialized = "\n".join(json.dumps(snapshot.checkpoint, default=str, ensure_ascii=False) for snapshot in snapshots)
    for forbidden in (raw_rca, raw_monitoring, "Current incident evidence was indexed", "retrieved_records", "evidence_map", "cited_source_ids"):
        assert forbidden not in serialized


def test_pb3_real_sqlite_backend_and_repository_are_tenant_case_isolated():
    backend = SQLiteScopedRetrieval(sqlite3.connect(":memory:"))
    backend.add_projection(
        {
            "source_id": "a_1",
            "summary": "rca evidence",
            "metadata": {"tenant_id": "tenant_a", "case_ref": "case_a", "category": "causal_evidence"},
        }
    )
    backend.add_projection(
        {
            "source_id": "b_1",
            "summary": "rca evidence",
            "metadata": {"tenant_id": "tenant_b", "case_ref": "case_b", "category": "causal_evidence"},
        }
    )
    retrieval = ScopedRetrievalNode(
        scope_keys=("tenant_id", "case_ref"),
        backend=backend,
        normalize_query=lambda value: str(value),
        top_k=5,
    )
    retrieved = retrieval.execute({"tenant_id": "tenant_a", "case_ref": "case_a", "query": "rca"})
    assert [row["source_id"] for row in retrieved["retrieved_records"]] == ["a_1"]
    assert retrieved["retrieved_records"][0]["metadata"]["tenant_id"] == "tenant_a"
    assert retrieved["retrieved_records"][0]["metadata"]["case_ref"] == "case_a"
    assert retrieval.execute({"tenant_id": "", "case_ref": "case_a", "query": "rca"})["status"] == "error"

    repository = SQLiteRcaRepository(sqlite3.connect(":memory:"))
    repository.write_batch(
        [{"evaluation_id": "eval_a", "observed_at": "2026-07-01", "category": "causal_evidence"}],
        scope={"tenant_id": "tenant_a", "case_ref": "case_a"},
    )
    repository.write_batch(
        [{"evaluation_id": "eval_b", "observed_at": "2026-07-01", "category": "causal_evidence"}],
        scope={"tenant_id": "tenant_b", "case_ref": "case_b"},
    )
    assert [row["evaluation_id"] for row in repository.list_for_scope({"tenant_id": "tenant_a", "case_ref": "case_a"})] == ["eval_a"]
    assert repository.list_for_scope({"tenant_id": "", "case_ref": "case_a"}) == []
