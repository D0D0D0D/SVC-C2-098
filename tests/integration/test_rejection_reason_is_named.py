"""A caller who omits the evidence records must be told which mistake they made.

Regression: every rejection collapsed into the same 422 "RCA brief processing
failed". The inner node knew the reason (`current_evidence_required`), but the
error text was prose the part's `known_failure_codes` allowlist could not match,
so it degraded to the generic `subgraph_failed` and the HTTP layer dropped it.
An operator could not tell "I forgot the records" from an internal fault — which
is also how this agent came to be recorded as "the /invoke path is broken".
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src.api import server
from src.graph.graph import CALLER_FIXABLE_ERROR_CODES, SlaRcaGraphNode

_BASE = {
    "rca": "根本原因の記述",
    "monitoring": "監視データ",
    "sla_definition": "availability",
    "measurement_period": "2026-07",
}
_HEADERS = {"authorization": "Bearer test-entry-token"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "test-entry-token")
    monkeypatch.setenv("STG_DEFAULT_TENANT_ID", "tenant1")
    monkeypatch.setenv("STG_DEFAULT_CASE_REF", "case1")
    with TestClient(server.app, raise_server_exceptions=False) as c:
        yield c


def _post(client, payload):
    return client.post(
        "/invoke",
        json={"input": json.dumps({"payload": payload}, ensure_ascii=False), "session_id": "s"},
        headers=_HEADERS,
    )


def test_canonical_packet_still_succeeds_and_uses_the_supplied_records(client):
    """Guards the claim that the scrubber 'drops records' — it does not."""

    response = _post(client, {**_BASE, "records": [{"source_id": "monitor_1", "source_type": "monitor"}]})
    assert response.status_code == 200
    facts = response.json()["output"]["facts"]
    assert ["monitor_1"] in [fact["source_ids"] for fact in facts]


@pytest.mark.parametrize(
    "payload",
    [dict(_BASE), {**_BASE, "records": []}, {"rca": "only"}],
    ids=["records-absent", "records-empty", "payload-incomplete"],
)
def test_missing_evidence_records_is_named_not_generic(client, payload):
    response = _post(client, payload)
    assert response.status_code == 422
    assert response.json()["detail"].endswith(": current_evidence_required")


def test_only_caller_fixable_codes_are_ever_exposed():
    """The HTTP layer names only what the caller can act on.

    known_failure_codes is the graph's own surface and also carries provider
    faults (llm_call_failed); those tell a caller nothing actionable, so the
    HTTP allowlist is deliberately narrower.
    """

    assert "current_evidence_required" in CALLER_FIXABLE_ERROR_CODES
    for internal in (
        "repository_write_failed", "request_context_missing", "subgraph_failed",
        "llm_call_failed", "llm_output_parse_failed",
    ):
        assert internal not in CALLER_FIXABLE_ERROR_CODES
    assert CALLER_FIXABLE_ERROR_CODES <= set(SlaRcaGraphNode.known_failure_codes)
