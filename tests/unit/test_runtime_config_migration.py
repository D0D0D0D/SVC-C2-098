from __future__ import annotations

import yaml

from src.api import server


EXPECTED_RUNTIME_CONFIG = {
    "max_retry": 3,
    "timeout_s": 30,
    "budget": 0,
    "top_k": 5,
    "memory_enabled": False,
    "hitl": {"enabled": False},
    "payload_ttl_seconds": 300,
    "brief_ttl_seconds": 300,
    "provenance_ttl_seconds": 300,
    "ingest_file": {
        "allowed_channels": ["csv"],
        "max_file_bytes": 5_242_880,
        "quality": {
            "min_text_chars": 4,
            "min_printable_ratio": 0.85,
            "max_replacement_ratio": 0.01,
            "max_control_ratio": 0.02,
            "min_signal_ratio": 0.15,
        },
    },
}


def test_runtime_config_keeps_pre_migration_effective_values() -> None:
    assert server._load_runtime_config() == EXPECTED_RUNTIME_CONFIG
    assert server.RUNTIME_CONFIG == EXPECTED_RUNTIME_CONFIG


def test_agent_manifest_is_registry_only() -> None:
    manifest = yaml.safe_load((server.Path(__file__).resolve().parents[2] / "config" / "agent.yaml").read_text())

    assert "agent" not in manifest
    assert "module" not in manifest
    assert "config" not in manifest
    assert manifest["class"] == "src.graph.graph.SlaRcaRebuttalBriefAgent"
