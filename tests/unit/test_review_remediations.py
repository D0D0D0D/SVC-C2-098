"""Adversarial-review regressions for provenance, normalization, and PII."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from framework.errors import SecurityViolationError
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import SlaRcaRebuttalBriefAgent
from src.nodes.security import find_forbidden_keys
from src.services import resolve_invoke_output
from src.services.service import wire_services


class FakeLlm:
    def __init__(self, content: dict) -> None:
        self._content = content

    def complete(self, _messages):
        return {"content": json.dumps(self._content, ensure_ascii=False)}


class RaisingLlm:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def complete(self, _messages):
        raise self._exc


def _llm_draft(source_ids: list[str]) -> dict:
    return {
        "facts": [{"claim_id": "fact_1", "statement": "Claim text", "source_ids": source_ids}],
        "evidence_sources": [
            {"source_id": "log_1", "record_kind": "current_evidence", "source_ids": source_ids}
        ],
        "unresolved_points": [{"claim_id": "gap_1", "point": "Gap text", "source_ids": source_ids}],
        "countervailing_observations": [{"claim_id": "counter_1", "observation": "Counter text", "source_ids": source_ids}],
        "review_questions": [{"claim_id": "question_1", "question": "Question text", "source_ids": source_ids}],
    }


def _invoke_with_llm(draft: dict):
    services = wire_services({"llm": FakeLlm(draft)})
    scope = {"tenant_id": "tenant1", "case_ref": "case1"}
    session_id = "llm-review-session"
    ref = services.raw_payload_store.put(
        json.dumps(
            {
                "records": [{"source_id": "log_1", "source_type": "monitor"}],
                "__extra__": {"rca": "vendor narrative", "sla_definition": "x", "measurement_period": "2026-07"},
            }
        ),
        scope=scope,
        session_id=session_id,
    )
    agent = SlaRcaRebuttalBriefAgent(
        services=services,
        trusted_scope=scope,
        request_session_id=session_id,
    )
    result = agent.invoke(
        json.dumps({"mode": "invoke", **scope, "payload_ref": ref}),
        ctx=InvocationContext(session_id=session_id, caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        input_context={"scope": scope},
    )
    return result, services, scope, session_id


def test_high1_llm_claim_without_own_source_ids_is_rejected_not_repaired():
    result, _services, _scope, _session = _invoke_with_llm(_llm_draft([]))
    assert result["status"] == "error"


def test_graph_invoke_surfaces_safe_error_code_without_provider_text():
    provider_marker = "provider-raw-marker-8f21c3"
    services = wire_services({"llm": RaisingLlm(RuntimeError(f"provider failed: {provider_marker}"))})
    scope = {"tenant_id": "tenant1", "case_ref": "case1"}
    session_id = "llm-error-hop-session"
    ref = services.raw_payload_store.put(
        json.dumps(
            {
                "records": [{"source_id": "log_1", "source_type": "monitor"}],
                "__extra__": {"sla_definition": "x", "measurement_period": "2026-07"},
            }
        ),
        scope=scope,
        session_id=session_id,
    )
    agent = SlaRcaRebuttalBriefAgent(
        services=services,
        trusted_scope=scope,
        request_session_id=session_id,
    )

    result = agent.invoke(
        json.dumps({"mode": "invoke", **scope, "payload_ref": ref}),
        ctx=InvocationContext(session_id=session_id, caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        input_context={"scope": scope},
    )

    assert result["status"] == "error"
    assert result["error_code"] == "llm_call_failed"
    assert result["error_type"] == "SubgraphError"
    assert result["error_log"] == ["DomainWorkflowGraphNode: llm_call_failed"]
    assert provider_marker not in json.dumps(result, default=str)


def test_outer_get_output_deduplicates_error_log_entries():
    agent = SlaRcaRebuttalBriefAgent()

    result = agent.get_output(
        {
            "status": "error",
            "error_code": "subgraph_failed",
            "error_type": "SubgraphError",
            "error_log": ["DomainWorkflowGraphNode: subgraph_failed", "DomainWorkflowGraphNode: subgraph_failed"],
        }
    )

    assert result["error_log"] == ["DomainWorkflowGraphNode: subgraph_failed"]


def test_prompt_shaped_llm_output_with_plural_evidence_source_ids_passes_in_runtime_config():
    result, services, scope, session_id = _invoke_with_llm(_llm_draft(["log_1"]))
    assert result["status"] == "success"
    output = resolve_invoke_output(services.brief_store, result["output"]["brief_ref"], scope=scope, session_id=session_id)
    assert output["facts"][0]["source_ids"] == ["log_1"]


def test_generated_response_contract_mechanically_builds_a_valid_draft():
    from src.graph.domain_workflow_graph import _build_prompt, _normalise_draft, _response_contract

    contract = _response_contract()
    draft = {}
    for section_name, section_schema in contract["properties"].items():
        item = {}
        properties = section_schema["items"]["properties"]
        for field_name in section_schema["items"]["required"]:
            field_schema = properties[field_name]
            if "enum" in field_schema:
                item[field_name] = field_schema["enum"][0]
            elif field_schema["type"] == "array":
                item[field_name] = ["log_1"]
            elif field_name == "source_id":
                item[field_name] = "log_1"
            elif field_name == "claim_id":
                item[field_name] = f"{section_name}_1"
            else:
                item[field_name] = "Source-linked review text"
        draft[section_name] = [item]

    assert _normalise_draft(draft) == draft
    serialized_contract = json.dumps(contract, ensure_ascii=False, sort_keys=True)
    assert f"response_contract={serialized_contract}" in _build_prompt({})


def test_prompt_states_the_claim_id_uniqueness_the_validator_enforces():
    """_normalise_draft rejects any claim_id reused across the response, but the
    generated response_contract cannot express that rule. The prompt must say
    it: without the sentence the registered model reused claim ids in 12 of 40
    drafts (2026-09-17), each rejected as 'draft claim identifier is duplicated'."""

    from src.graph.domain_workflow_graph import _build_prompt, _normalise_draft, _response_contract

    prompt = _build_prompt({})
    assert "claim_id must be unique across the whole response" in prompt

    contract = _response_contract()
    draft = {}
    for section_name, section_schema in contract["properties"].items():
        item = {}
        for field_name in section_schema["items"]["required"]:
            field_schema = section_schema["items"]["properties"][field_name]
            if "enum" in field_schema:
                item[field_name] = field_schema["enum"][0]
            elif field_schema["type"] == "array":
                item[field_name] = ["log_1"]
            elif field_name == "source_id":
                item[field_name] = "log_1"
            elif field_name == "claim_id":
                item[field_name] = "claim_1"  # the same id in every section, as the model did
            else:
                item[field_name] = "Source-linked review text"
        draft[section_name] = [item]
    with pytest.raises(SecurityViolationError, match="duplicated"):
        _normalise_draft(draft)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda draft: draft["facts"][0].update({"raw_document_text": "sensitive-marker"}),
            id="undeclared-key",
        ),
        pytest.param(
            lambda draft: draft["facts"][0].update({"statement": {"raw_document_text": "sensitive-marker"}}),
            id="dict-in-string-field",
        ),
        pytest.param(
            lambda draft: draft["facts"][0].update({"source_ids": [{"raw": "sensitive-marker"}]}),
            id="dict-in-list-string-field",
        ),
        pytest.param(
            lambda draft: draft["facts"][0].pop("claim_id"),
            id="required-field-missing",
        ),
        pytest.param(
            lambda draft: draft["evidence_sources"][0].update({"record_kind": "provider-own-vocabulary"}),
            id="enum-outside-contract",
        ),
    ],
)
def test_typed_projection_rejects_undeclared_and_wrong_typed_llm_fields_without_echo(mutate):
    from src.graph.domain_workflow_graph import _normalise_draft

    draft = _llm_draft(["log_1"])
    mutate(draft)
    with pytest.raises(SecurityViolationError) as caught:
        _normalise_draft(draft)
    assert "sensitive-marker" not in str(caught.value)


@pytest.mark.parametrize("key", ["penaltyAmount", "rcaAccepted", "SLA Credit", "contract-remedy"])
def test_high2_normalized_forbidden_output_keys_are_rejected(key):
    assert find_forbidden_keys({"nested": {key: 1}}) == [key]


def test_high2_response_allowlist_uses_same_normalization():
    services = wire_services()
    scope = {"tenant_id": "tenant1", "case_ref": "case1"}
    ref = services.brief_store.put(
        {
            "facts": [{"penaltyAmount": 1}],
            "evidence_sources": [],
            "unresolved_points": [],
            "countervailing_observations": [],
            "review_questions": [],
        },
        scope=scope,
        session_id="normalization-session",
    )
    with pytest.raises(ValueError, match="forbidden_field"):
        resolve_invoke_output(services.brief_store, ref, scope=scope, session_id="normalization-session")


def test_normal_japanese_operational_vocabulary_survives_sanitization():
    # SVC-C2-098 B-5: the bespoke Japanese-name pseudonymizer was removed.
    # Normal operational free text must still pass sanitization untouched.
    from src.graph.domain_workflow_graph import _sanitize_evaluation

    row = _sanitize_evaluation(
        {
            "evaluation_id": "eval_ops_1",
            "observed_at": "2026-07-01",
            "category": "causal_evidence",
            "verification_viewpoint": "監視値と時系列を照合し、根因を確認した。",
        }
    )
    assert row["verification_viewpoint"] == "監視値と時系列を照合し、根因を確認した。"


# --- S-3 opaque-reference exemption (UUID false-positive regression) ---------
#
# `detect_pii` is documented as high-recall / low-precision and instructs callers
# to post-filter.  Feeding it the agent's own `uuid4` brief_ref made ~0.56% of
# otherwise-successful invokes fail closed with a 422 (the digit-and-hyphen shape
# collides with the credit-card / phone_jp / my_number_jp patterns).  The gate now
# skips the PII scan for values it can prove the agent minted, and only those.


_REF = "abcdefghijklmnopabcdefghijklmnop"  # shape minted by payload_store.mint_reference


def _gate(formatted: dict, status: str = "success") -> dict:
    from src.nodes.post_process_node import PostProcessNode

    return PostProcessNode()._extra_security_gate_output(
        {"formatted_output": formatted, "status": status}
    )


def test_uuid_shaped_reference_would_have_tripped_the_detector():
    """Why references are letter-only: uuid4 text collides with PII patterns."""
    from src.nodes.security import has_pii_or_sensitive_operation

    assert has_pii_or_sensitive_operation({"x": "96473410-0055-4616-968b-dc66bbaf5111"})  # credit_card
    assert has_pii_or_sensitive_operation({"x": "42cabe04-5028-4985-b584-00c1758a8a1e"})  # phone_jp


def test_minted_references_are_never_seen_as_pii():
    from src.nodes.security import has_pii_or_sensitive_operation
    from src.payload_store import mint_reference

    for _ in range(2000):
        ref = mint_reference()
        assert not has_pii_or_sensitive_operation({"brief_ref": ref})
        result = _gate({"brief_ref": ref, "mode": "invoke", "gate_kind": "draft"})
        assert result["formatted_output"]["brief_ref"] == ref


@pytest.mark.parametrize(
    "evaluation_id",
    [
        pytest.param("090-1234-5678", id="pii-smuggled-through-evaluation_id"),
        pytest.param("rca-accepted", id="decision-language-smuggled-through-evaluation_id"),
    ],
)
def test_evaluation_refs_are_never_exempt_because_callers_control_them(evaluation_id):
    """Shape is not provenance.

    An evaluation storage key looks machine-formed, but it embeds the
    caller-supplied ``evaluation_id`` verbatim -- and ``090-1234-5678``
    satisfies ``SAFE_ID_RE``. Exempting these keys from the scan on shape alone
    would let a caller smuggle real PII (or decision language) straight through
    S-3, so they are scanned like any other content.
    """

    with pytest.raises(SecurityViolationError, match="unsafe client-visible content"):
        _gate(
            {
                "ingest_summary": {
                    "written_count": 1,
                    "evaluation_refs": [f"tenant1:case1:{evaluation_id}:1:0123456789abcdef"],
                },
                "mode": "ingest",
                "gate_kind": "ingest",
            }
        )


def test_ordinary_evaluation_refs_still_pass():
    refs = ["tenant1:case1:eval_1:1:0123456789abcdef"]
    result = _gate(
        {
            "ingest_summary": {"written_count": 1, "evaluation_refs": refs},
            "mode": "ingest",
            "gate_kind": "ingest",
        }
    )
    assert result["formatted_output"]["ingest_summary"]["evaluation_refs"] == refs


def test_free_text_smuggled_into_brief_ref_is_rejected_not_exempted():
    # The exemption must not become a channel: a value that is not an
    # agent-minted reference fails closed even though it sits under brief_ref.
    with pytest.raises(SecurityViolationError, match="not agent-minted"):
        _gate({"brief_ref": "連絡先は 090-1234-5678", "mode": "invoke", "gate_kind": "draft"})


def test_free_text_in_evaluation_refs_is_caught_by_the_normal_scan():
    with pytest.raises(SecurityViolationError, match="unsafe client-visible content"):
        _gate(
            {
                "ingest_summary": {"evaluation_refs": ["taro@example.com"]},
                "mode": "ingest",
                "gate_kind": "ingest",
            }
        )


def test_pii_in_non_exempt_field_is_still_blocked():
    # Only the opaque-ref keys are exempt; everything else is scanned as before.
    with pytest.raises(SecurityViolationError, match="unsafe client-visible content"):
        _gate(
            {
                "brief_ref": "abcdefghijklmnopabcdefghijklmnop",
                "degradation_reason": "LLM call failed for taro@example.com",
                "mode": "invoke",
                "gate_kind": "draft",
            }
        )


# Adversarial matrix for the opaque-ref exemption.  The exemption narrows the
# PII scan, so the boundary that keeps it honest is `_is_agent_minted_ref`:
# every value skipped by the scan must first prove it is agent-minted.  These
# cases are the ways free text could try to reach the exemption.
@pytest.mark.parametrize(
    "formatted",
    [
        pytest.param(
            {"brief_ref": "連絡先は 090-1234-5678", "mode": "invoke", "gate_kind": "draft"},
            id="free-text-directly-in-brief_ref",
        ),
        pytest.param(
            {"items": [{"brief_ref": "taro@example.com"}], "brief_ref": _REF,
             "mode": "invoke", "gate_kind": "draft"},
            id="free-text-under-brief_ref-nested-in-a-list",
        ),
        pytest.param(
            {"brief_ref": {"x": "taro@example.com"}, "mode": "invoke", "gate_kind": "draft"},
            id="nested-dict-under-brief_ref",
        ),
        pytest.param(
            {"brief_ref": "rca_accepted", "mode": "invoke", "gate_kind": "draft"},
            id="forbidden-decision-token-as-ref",
        ),
        pytest.param(
            {"brief_ref": None, "mode": "invoke", "gate_kind": "draft"},
            id="non-string-ref",
        ),
    ],
)
def test_only_agent_minted_values_may_skip_the_pii_scan(formatted):
    with pytest.raises(SecurityViolationError):
        _gate(formatted)


def test_exemption_and_verification_traverse_identically():
    """A value skipped by the scan is always a value the verifier inspected.

    If `_opaque_ref_free_view` removed a subtree that `_collect_opaque_refs`
    did not collect, that subtree would skip S-3 unverified — the exact shape
    of hole this exemption must not have.
    """
    from src.nodes.post_process_node import PostProcessNode

    node = PostProcessNode()
    payload = {
        "brief_ref": _REF,
        "ingest_summary": {"written_count": 1},
        "nested": [{"brief_ref": _REF}, {"other": "kept"}],
        "free_text": "kept",
    }
    scanned = node._opaque_ref_free_view(payload)
    collected = node._collect_opaque_refs(payload)

    # Everything removed from the scanned view was handed to the verifier.
    assert collected.count(_REF) == 2
    assert "brief_ref" not in scanned
    assert all("brief_ref" not in item for item in scanned["nested"])
    # ...and non-exempt content survives untouched, so it is still scanned.
    assert scanned["free_text"] == "kept"
    assert scanned["ingest_summary"]["written_count"] == 1
    assert scanned["nested"][1]["other"] == "kept"


@pytest.mark.parametrize(
    "version",
    [
        pytest.param("1:2", id="colon-would-add-a-key-segment"),
        pytest.param("1 2", id="whitespace"),
        pytest.param("a" * 200, id="over-length"),
    ],
)
def test_invalid_version_is_rejected_before_any_write(version):
    """`version` is a segment of the composed storage key, so it obeys the same
    contract as the other segments -- and is rejected *before* persistence.

    Truncating instead would let ":" or whitespace corrupt the key structure,
    and the failure would only surface at the S-3 gate, after the row had
    already been written.
    """
    from src.graph.domain_workflow_graph import _sanitize_evaluation

    with pytest.raises(ValueError, match="evaluation_version_invalid"):
        _sanitize_evaluation(
            {
                "evaluation_id": "eval_1",
                "observed_at": "2026-07-01",
                "category": "causal_evidence",
                "version": version,
            }
        )


def test_ingest_with_invalid_version_writes_nothing():
    """End-to-end: the batch is rejected and the repository stays empty."""
    services = wire_services()
    scope = {"tenant_id": "tenant1", "case_ref": "case1"}
    session_id = "bad-version-session"
    ref = services.raw_payload_store.put(
        json.dumps(
            {"records": [{"evaluation_id": "eval_1", "observed_at": "2026-07-01",
                          "category": "causal_evidence", "version": "1:2"}]}
        ),
        scope=scope,
        session_id=session_id,
    )
    agent = SlaRcaRebuttalBriefAgent(
        services=services,
        trusted_scope=scope,
        request_session_id=session_id,
    )
    result = agent.invoke(
        json.dumps({"mode": "ingest", **scope, "payload_ref": ref}),
        ctx=InvocationContext(session_id=session_id, caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        input_context={"scope": scope},
    )
    assert result["status"] == "error"
    assert services.repository.records == {}


def _request_bound_ingest(services, scope: dict[str, str], session_id: str, evaluation_id: str):
    ref = services.raw_payload_store.put(
        {
            "records": [
                {
                    "evaluation_id": evaluation_id,
                    "observed_at": "2026-08-13",
                    "category": "causal_evidence",
                }
            ]
        },
        scope=scope,
        session_id=session_id,
    )
    agent = SlaRcaRebuttalBriefAgent(
        services=services,
        trusted_scope=scope,
        request_session_id=session_id,
    )
    return agent.invoke(
        json.dumps({"mode": "ingest", **scope, "payload_ref": ref}),
        ctx=InvocationContext(session_id=session_id, caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        input_context={"scope": scope},
    )


def test_parallel_request_scoped_graphs_do_not_mix_ingest_context():
    services = wire_services()
    cases = [
        ({"tenant_id": "tenant_a", "case_ref": "case_a"}, "session-a", "eval_a"),
        ({"tenant_id": "tenant_b", "case_ref": "case_b"}, "session-b", "eval_b"),
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda args: _request_bound_ingest(services, *args), cases))

    assert [result["status"] for result in results] == ["success", "success"]
    keys = list(services.repository.records)
    assert len([key for key in keys if key.startswith("tenant_a:case_a:eval_a:")]) == 1
    assert len([key for key in keys if key.startswith("tenant_b:case_b:eval_b:")]) == 1
    assert not any("tenant_a:case_b" in key or "tenant_b:case_a" in key for key in keys)


def test_state_scope_cannot_replace_constructor_injected_ingest_context():
    services = wire_services()
    state_scope = {"tenant_id": "tenant_a", "case_ref": "case_a"}
    injected_scope = {"tenant_id": "tenant_b", "case_ref": "case_b"}
    session_id = "scope-substitution-session"
    ref = services.raw_payload_store.put(
        {
            "records": [
                {"evaluation_id": "eval_a", "observed_at": "2026-08-13", "category": "causal_evidence"}
            ]
        },
        scope=state_scope,
        session_id=session_id,
    )
    agent = SlaRcaRebuttalBriefAgent(
        services=services,
        trusted_scope=injected_scope,
        request_session_id=session_id,
    )
    result = agent.invoke(
        json.dumps({"mode": "ingest", **state_scope, "payload_ref": ref}),
        ctx=InvocationContext(session_id=session_id, caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        input_context={"scope": state_scope},
    )
    assert result["status"] == "error"
    assert services.repository.records == {}


def test_repository_exception_text_is_not_exposed_through_row_part_boundary():
    marker = "llm-authored-sensitive-marker"

    class _FailingRepository:
        def write_batch(self, *_args, **_kwargs):
            raise RuntimeError(marker)

    services = wire_services({"repository": _FailingRepository()})
    result = _request_bound_ingest(
        services,
        {"tenant_id": "tenant1", "case_ref": "case1"},
        "failing-repository-session",
        "eval_1",
    )
    assert result["status"] == "error"
    assert marker not in json.dumps(result, ensure_ascii=False)
