"""Acceptance coverage for the AGENTIC STAR Marketplace entry adapter."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import cli
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.services.llm.base_llm import BaseLLM


_SCOPE = {"tenant_id": "tenant-marketplace", "case_ref": "case-marketplace"}
_OUTPUT_SECTIONS = {
    "facts",
    "evidence_sources",
    "unresolved_points",
    "countervailing_observations",
    "review_questions",
}


def _material(**extra: Any) -> dict[str, Any]:
    return {
        "records": [
            {"source_id": "incident_ticket_814", "source_type": "ticket"},
            {"source_id": "monitor_availability_814", "source_type": "monitor"},
            {"source_id": "deploy_change_772", "source_type": "change"},
        ],
        "__extra__": {
            "sla_definition": "Monthly API availability target is 99.90 percent.",
            "measurement_period": "2026-07",
            "vendor_rca": "The vendor attributes the outage to a traffic spike.",
            "incident_timeline": "Errors rose after deploy change 772.",
            "monitoring": "Traffic stayed normal while connection exhaustion rose.",
        },
        **extra,
    }


def _draft() -> dict[str, list[dict[str, Any]]]:
    source_id = "incident_ticket_814"
    return {
        "facts": [
            {
                "claim_id": "fact_1",
                "statement": "The incident ticket documents the service interruption.",
                "source_ids": [source_id],
            }
        ],
        "evidence_sources": [
            {
                "source_id": source_id,
                "record_kind": "current_evidence",
                "source_ids": [source_id],
            }
        ],
        "unresolved_points": [
            {
                "claim_id": "unresolved_1",
                "point": "The causal sequence requires source-linked review.",
                "source_ids": [source_id],
            }
        ],
        "countervailing_observations": [
            {
                "claim_id": "counter_1",
                "observation": "The monitoring record provides a comparison point.",
                "source_ids": [source_id],
            }
        ],
        "review_questions": [
            {
                "claim_id": "question_1",
                "question": "Which record establishes the proposed causal link?",
                "source_ids": [source_id],
            }
        ],
    }


class CountingLLM(BaseLLM):
    def __init__(self) -> None:
        self.complete_calls = 0
        self.messages: list[list[Any]] = []

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        self.complete_calls += 1
        self.messages.append(messages)
        return {"content": json.dumps(_draft())}

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield self.complete(messages)["content"]

    def bind_tools(self, tools: list[Any]) -> "CountingLLM":
        del tools
        return self


@pytest.fixture(autouse=True)
def marketplace_environment(monkeypatch):
    values = {
        "AZURE_OPENAI_API_KEY": "unit-value",
        "AZURE_OPENAI_ENDPOINT": "https://unit.invalid",
        "AZURE_OPENAI_DEPLOYMENT": "unit-deployment",
        "STG_DEFAULT_TENANT_ID": _SCOPE["tenant_id"],
        "STG_DEFAULT_CASE_REF": _SCOPE["case_ref"],
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _runner_invoke(agent: cli.MarketplaceGraph, material: dict[str, Any]) -> dict[str, Any]:
    agent.compile()
    agent.provision_secrets(cli._AllowlistedEnvironmentProvider())
    ctx = InvocationContext(
        caller_id="marketplace-test",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
    )
    return agent.invoke(
        json.dumps(material),
        ctx=ctx,
        input_context={"conversation_history": [{"role": "user", "content": "CANARY"}]},
    )


def test_runner_order_resolves_brief_calls_llm_and_excludes_canary():
    fake = CountingLLM()
    result = _runner_invoke(cli.MarketplaceGraph(config={"llm": fake}), _material())

    assert result["status"] == "success"
    assert set(result["output"]) == _OUTPUT_SECTIONS
    assert all(result["output"][section] for section in _OUTPUT_SECTIONS)
    assert result["requires_human_review"] is True
    assert result["disclaimer"] == cli._DISCLAIMER
    assert "CANARY" not in json.dumps(result, ensure_ascii=False)
    assert fake.complete_calls == 1


def test_default_llm_is_installed_before_services_are_wired():
    agent = cli.MarketplaceGraph(config={})

    assert isinstance(agent.config["llm"], cli.MarketplaceAzureLLM)
    assert isinstance(agent._services.llm, cli.MarketplaceAzureLLM)
    assert agent._services.llm is agent.config["llm"]
    assert vars(agent.config["llm"]) == {}


def test_null_llm_from_runtime_config_is_replaced_and_invoked(monkeypatch):
    calls = 0

    def complete(_self, _messages):
        nonlocal calls
        calls += 1
        return {"content": json.dumps(_draft())}

    monkeypatch.setattr(cli.MarketplaceAzureLLM, "complete", complete)
    runtime_config = cli.load_agent_config(Path(__file__).resolve().parents[2])
    agent = cli.MarketplaceGraph(config={**runtime_config, "llm": None})

    assert isinstance(agent.config["llm"], cli.MarketplaceAzureLLM)
    assert isinstance(agent._services.llm, cli.MarketplaceAzureLLM)
    result = _runner_invoke(agent, _material())
    assert result["status"] == "success"
    assert result["output"]["facts"][0]["claim_id"] == "fact_1"
    assert calls == 1


def test_caller_supplied_llm_survives_by_identity():
    fake = CountingLLM()
    agent = cli.MarketplaceGraph(config={"llm": fake})

    assert agent.config["llm"] is fake
    assert agent._services.llm is fake


def test_main_passes_file_relative_config_to_marketplace_runner(monkeypatch):
    loaded = {"top_k": 7}
    captured: dict[str, Any] = {}

    monkeypatch.setattr(cli, "load_agent_config", lambda path: loaded)
    monkeypatch.setattr(
        cli,
        "run_agent_marketplace",
        lambda graph, **kwargs: captured.update({"graph": graph, **kwargs}),
    )

    cli.main()

    assert captured == {
        "graph": cli.MarketplaceGraph,
        "agent_name": "SVC-C2-098",
        "namespace": "svc",
        "config": loaded,
    }


@pytest.mark.parametrize("requested_mode", ["ingest", "feedback"])
def test_marketplace_write_modes_are_forced_to_invoke(requested_mode):
    fake = CountingLLM()
    agent = cli.MarketplaceGraph(config={"llm": fake})

    result = _runner_invoke(agent, _material(mode=requested_mode))

    assert result["status"] == "success"
    assert not agent._services.repository.records
    prompt = fake.messages[-1][0]["content"]
    assert f'"mode": "{requested_mode}"' not in prompt
    assert '"mode": "invoke"' in prompt


@pytest.mark.parametrize("key", cli._AZURE_OPENAI_SECRET_KEYS)
@pytest.mark.parametrize("bad_value", [None, "", " padded", "line\nbreak"])
def test_azure_startup_values_fail_closed(monkeypatch, key, bad_value):
    agent = cli.MarketplaceGraph(config={"llm": CountingLLM()})
    if bad_value is None:
        monkeypatch.delenv(key)
    else:
        monkeypatch.setenv(key, bad_value)

    with pytest.raises((ValueError, RuntimeError)):
        agent.provision_secrets(cli._AllowlistedEnvironmentProvider())


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(monkeypatch, endpoint):
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    agent = cli.MarketplaceGraph(config={"llm": CountingLLM()})
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        agent.provision_secrets(cli._AllowlistedEnvironmentProvider())


@pytest.mark.parametrize("key", ["STG_DEFAULT_TENANT_ID", "STG_DEFAULT_CASE_REF"])
@pytest.mark.parametrize("bad_value", [None, "", " padded", "line\nbreak"])
def test_scope_startup_values_fail_closed(monkeypatch, key, bad_value):
    if bad_value is None:
        monkeypatch.delenv(key)
    else:
        monkeypatch.setenv(key, bad_value)

    with pytest.raises((ValueError, RuntimeError)):
        cli.MarketplaceGraph(config={"llm": CountingLLM()})


def test_same_agent_supports_two_consecutive_invocations():
    fake = CountingLLM()
    agent = cli.MarketplaceGraph(config={"llm": fake})
    agent.compile()
    agent.provision_secrets(cli._AllowlistedEnvironmentProvider())
    ctx = InvocationContext(caller_id="marketplace-test")

    first = agent.invoke(json.dumps(_material()), ctx=ctx)
    second = agent.invoke(json.dumps(_material()), ctx=ctx)

    assert first["status"] == second["status"] == "success"
    assert set(first["output"]) == set(second["output"]) == _OUTPUT_SECTIONS
    assert fake.complete_calls == 2
    assert not agent._services.raw_payload_store._items


def test_brief_resolution_failure_is_fail_closed(monkeypatch):
    def reject_resolution(*args, **kwargs):
        del args, kwargs
        raise ValueError("blocked output")

    monkeypatch.setattr(cli, "resolve_invoke_output", reject_resolution)
    agent = cli.MarketplaceGraph(config={"llm": CountingLLM()})
    agent.compile()
    agent.provision_secrets(cli._AllowlistedEnvironmentProvider())

    with pytest.raises(ValueError, match="blocked output"):
        agent.invoke(json.dumps(_material()))

    assert not agent._services.raw_payload_store._items


def test_runner_input_context_is_rebuilt_not_merged(monkeypatch):
    captured: dict[str, Any] = {}

    def capture_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        del self, user_input
        captured.update({"session_id": session_id, "ctx": ctx, "input_context": input_context})
        return {"status": "error", "output": None}

    monkeypatch.setattr(cli.SlaRcaRebuttalBriefAgent, "invoke", capture_invoke)
    agent = cli.MarketplaceGraph(config={"llm": CountingLLM()})
    result = agent.invoke(
        json.dumps(_material()),
        input_context={"conversation_history": [{"role": "user", "content": "CANARY"}]},
    )

    assert result["status"] == "error"
    assert captured["input_context"] == {"scope": _SCOPE}
    assert captured["session_id"] == cli._MARKETPLACE_SESSION_ID
    assert captured["ctx"].session_id == cli._MARKETPLACE_SESSION_ID
    assert captured["ctx"].caller_trust_level is TrustLevel.VERIFIED_EXTERNAL
    assert captured["ctx"].hitl_allowed is False
    assert not agent._services.raw_payload_store._items


@pytest.mark.parametrize("bad_input", ["not-json", "[]", "", "null"])
def test_invalid_business_input_fails_without_echoing_it(bad_input):
    agent = cli.MarketplaceGraph(config={"llm": CountingLLM()})

    with pytest.raises(ValueError) as raised:
        agent.invoke(bad_input)

    if bad_input:
        assert bad_input not in str(raised.value)
