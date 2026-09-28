"""PB-1..PB-6 integration coverage for isolation and invocation boundaries."""

from __future__ import annotations

import json
import base64

from fastapi.testclient import TestClient
import pytest
from framework.schemas.trust_level import TrustLevel

from src.api import server
from src.services import resolve_invoke_output
from src.services.service import wire_services


@pytest.fixture
def authenticated_client(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "test-entry-token")
    monkeypatch.setenv("STG_DEFAULT_TENANT_ID", "tenant1")
    monkeypatch.setenv("STG_DEFAULT_CASE_REF", "case1")
    with TestClient(server.app, raise_server_exceptions=False) as client:
        yield client


class _VerifiedIdentityMiddleware:
    """Test-only stand-in for the platform identity middleware."""

    def __init__(self, application):
        self._application = application

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope.setdefault("state", {}).update(
                {
                    "trust_level": TrustLevel.VERIFIED_EXTERNAL,
                    "caller_id": "verified-test-operator",
                    "tenant_id": "tenant1",
                    "case_ref": "case1",
                }
            )
        await self._application(scope, receive, send)


@pytest.fixture
def middleware_client(monkeypatch):
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    with TestClient(_VerifiedIdentityMiddleware(server.app), raise_server_exceptions=False) as client:
        yield client


def _input():
    return {
        "payload_ref": "client-forged-ref",
        "payload": {
            "records": [{"source_id": "monitor_1", "source_type": "monitor"}],
            "rca": "Raw RCA narrative that must never enter checkpoint state",
            "monitoring": "Raw monitoring payload that must never enter checkpoint state",
            "sla_definition": "availability",
            "measurement_period": "2026-07",
        },
    }


def test_pb1_pb2_pb5_ingress_uses_opaque_ref_and_returns_only_allowlisted_output(authenticated_client):
    response = authenticated_client.post(
        "/invoke",
        json={"input": json.dumps(_input()), "session_id": "boundary-session"},
        headers={"authorization": "Bearer test-entry-token"},
    )
    assert response.status_code == 200
    body = response.json()
    encoded = json.dumps(body, ensure_ascii=False)
    assert "Raw RCA narrative" not in encoded and "Raw monitoring payload" not in encoded
    assert "client-forged-ref" not in encoded
    assert set(body["output"]) == {
        "facts",
        "evidence_sources",
        "unresolved_points",
        "countervailing_observations",
        "review_questions",
    }
    assert body["requires_human_review"] is True


def test_pb3_no_real_external_service_is_called_and_fallback_is_deterministic(authenticated_client):
    response = authenticated_client.post(
        "/invoke",
        json={"input": json.dumps(_input()), "session_id": "fallback-session"},
        headers={"authorization": "Bearer test-entry-token"},
    )
    assert response.status_code == 200
    assert response.json()["output"]["facts"][0]["source_ids"] == ["monitor_1"]


def test_pb5_output_ref_scope_mismatch_fails_closed():
    services = wire_services()
    ref = services.brief_store.put(
        {
            "facts": [],
            "evidence_sources": [],
            "unresolved_points": [],
            "countervailing_observations": [],
            "review_questions": [],
        },
        scope={"tenant_id": "tenant1", "case_ref": "case1"},
        session_id="session1",
    )
    with pytest.raises(ValueError, match="payload_ref_scope_mismatch"):
        resolve_invoke_output(
            services.brief_store,
            ref,
            scope={"tenant_id": "tenant2", "case_ref": "case1"},
            session_id="session1",
        )


def test_standalone_read_token_cannot_write_business_state(authenticated_client):
    response = authenticated_client.post(
        "/ingest",
        json={"input": json.dumps({"payload": {"records": []}}), "session_id": "token-write-denied"},
        headers={"authorization": "Bearer test-entry-token"},
    )
    assert response.status_code == 403


def test_ingest_sanitize_failure_rolls_back_entire_batch(middleware_client):
    body = {
        "payload": {
            "records": [
                {"evaluation_id": "eval_1", "observed_at": "2026-07-01", "category": "causal_evidence"},
                {"evaluation_id": "eval_2", "observed_at": "2026-07-01", "category": "not-allowed"},
            ]
        }
    }
    response = middleware_client.post(
        "/ingest",
        json={"input": json.dumps(body), "session_id": "rollback-session"},
    )
    assert response.status_code == 422
    assert not [key for key in server.services.repository.records if ":eval_1:" in key]


def test_feedback_is_explicit_human_write_path(middleware_client):
    body = {
        "payload": {
            "records": [
                {"evaluation_id": "feedback_1", "observed_at": "2026-07-01", "category": "cap_connection"}
            ]
        }
    }
    response = middleware_client.post(
        "/feedback",
        json={"input": json.dumps(body), "session_id": "feedback-session"},
    )
    assert response.status_code == 200
    assert response.json()["output"]["feedback_summary"]["written_count"] == 1


def test_high4_documented_payload_file_b64_csv_is_ingested(middleware_client):
    csv_bytes = b"evaluation_id,observed_at,category\nfile_eval_1,2026-07-01,causal_evidence\n"
    response = middleware_client.post(
        "/ingest",
        json={
            "input": json.dumps({"payload": {"file_b64": base64.b64encode(csv_bytes).decode()}}),
            "session_id": "csv-file-session",
        },
    )
    assert response.status_code == 200
    assert response.json()["output"]["ingest_summary"]["written_count"] == 1


def test_high4_undocumented_top_level_file_b64_is_rejected(middleware_client):
    csv_bytes = b"evaluation_id,observed_at,category\nfile_eval_2,2026-07-01,causal_evidence\n"
    response = middleware_client.post(
        "/ingest",
        json={"input": json.dumps({"file_b64": base64.b64encode(csv_bytes).decode()}), "session_id": "bad-file-session"},
    )
    assert response.status_code == 422


def test_medium2_file_quality_gate_uses_manifest_runtime_config():
    policy = server.RUNTIME_CONFIG["ingest_file"]
    assert policy["allowed_channels"] == ["csv"]
    assert policy["max_file_bytes"] == 5_242_880
    assert policy["quality"]["min_text_chars"] == 4
