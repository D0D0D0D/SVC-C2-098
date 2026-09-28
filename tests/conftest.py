"""Fixtures consumed by ``work/scripts/mrb_live_llm_check.py``."""

from __future__ import annotations

import pytest

from src.graph.graph import SlaRcaRebuttalBriefAgent
from src.services.service import wire_services


_LIVE_SCOPE = {"tenant_id": "tenant-live", "case_ref": "case-live"}
_LIVE_SESSION_ID = "live-1"


def _live_rca_material() -> dict:
    """A vendor RCA contradicted by monitoring, timeline, and change evidence."""
    return {
        "records": [
            {"source_id": "incident_ticket_814", "source_type": "ticket"},
            {"source_id": "monitor_availability_814", "source_type": "monitor"},
            {"source_id": "deploy_change_772", "source_type": "change"},
            {"source_id": "rollback_log_772", "source_type": "log"},
        ],
        "__extra__": {
            "sla_definition": "Monthly API availability target is 99.90 percent.",
            "measurement_period": "2026-07",
            "vendor_rca": (
                "The vendor attributes the 47-minute outage solely to an unexpected traffic spike "
                "and proposes lowering the monitoring alert threshold as the corrective action."
            ),
            "incident_timeline": (
                "Error rates rose nine minutes after deploy change 772; service recovered four "
                "minutes after rollback 772 completed."
            ),
            "monitoring": (
                "Request volume stayed within the normal weekday band while database connection "
                "exhaustion and application errors increased immediately after the deployment."
            ),
            "ticket_notes": (
                "Incident ticket 814 records no capacity alarm and asks whether the release changed "
                "the connection-pool limit."
            ),
            "corrective_action_plan": (
                "The proposed alert-threshold change does not address the deployment correlation or "
                "connection-pool exhaustion."
            ),
        },
    }


@pytest.fixture
def _live_services_and_ref(monkeypatch):
    services = wire_services()
    services.retrieval.add_projection(
        {
            "source_id": "precedent_cap_disconnect",
            "summary": "sla deviation rca case case-live deployment change cap connection",
            "metadata": {
                **_LIVE_SCOPE,
                "category": "cap_connection",
                "record_type": "evaluation_viewpoint",
            },
        }
    )
    ref = services.raw_payload_store.put(
        _live_rca_material(),
        scope=_LIVE_SCOPE,
        session_id=_LIVE_SESSION_ID,
    )
    original_invoke = SlaRcaRebuttalBriefAgent.invoke

    def invoke_with_verified_scope(self, user_input, *args, **kwargs):
        context = kwargs.get("input_context")
        kwargs["input_context"] = {
            **(context if isinstance(context, dict) else {}),
            "scope": dict(_LIVE_SCOPE),
        }
        return original_invoke(self, user_input, *args, **kwargs)

    monkeypatch.setattr(SlaRcaRebuttalBriefAgent, "invoke", invoke_with_verified_scope)
    return services, ref


@pytest.fixture
def agent_config(_live_services_and_ref) -> dict:
    services, _ = _live_services_and_ref
    return {
        "raw_payload_store": services.raw_payload_store,
        "brief_store": services.brief_store,
        "provenance_store": services.provenance_store,
        "retrieval": services.retrieval,
        "repository": services.repository,
        "top_k": 5,
        "memory_enabled": False,
        "hitl": {"enabled": False},
    }


@pytest.fixture
def case_payload(_live_services_and_ref) -> dict:
    _services, ref = _live_services_and_ref
    return {
        "mode": "invoke",
        **_LIVE_SCOPE,
        "payload_ref": ref,
    }
