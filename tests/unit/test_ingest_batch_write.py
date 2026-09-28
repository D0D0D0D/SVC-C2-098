"""Domain wiring regressions for atomic repository batch writes."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

import pytest

from src.graph import domain_workflow_graph
from src.graph.domain_workflow_graph import IngestWriteNode
from src.services.service import SQLiteRcaRepository, SQLiteScopedRetrieval, wire_services


SCOPE = {"tenant_id": "tenant_1", "case_ref": "case_1"}


class RecordingSQLiteRcaRepository(SQLiteRcaRepository):
    def __init__(self, connection: sqlite3.Connection) -> None:
        super().__init__(connection)
        self.calls: list[list[str]] = []

    def write_batch(
        self,
        rows: list[dict[str, Any]],
        *,
        scope: Mapping[str, str],
        feedback: bool = False,
    ) -> list[str]:
        self.calls.append([row["evaluation_id"] for row in rows])
        return super().write_batch(rows, scope=scope, feedback=feedback)


def _rows() -> list[dict[str, Any]]:
    return [
        {
            "evaluation_id": "eval_1",
            "observed_at": "2026-08-13T10:00:00Z",
            "category": "causal_evidence",
            "verification_viewpoint": "first valid viewpoint",
        },
        {
            "evaluation_id": "eval_2",
            "observed_at": "2026-08-13T10:01:00Z",
            "category": "causal_evidence",
            "verification_viewpoint": "second valid viewpoint",
        },
    ]


def _node(
    connection: sqlite3.Connection,
) -> tuple[IngestWriteNode, RecordingSQLiteRcaRepository]:
    repository = RecordingSQLiteRcaRepository(connection)
    services = wire_services(
        {
            "repository": repository,
            "retrieval": SQLiteScopedRetrieval(connection),
        }
    )
    return (
        IngestWriteNode(
            services=services,
            file_config={},
            trusted_scope=SCOPE,
            session_id="session-1",
        ),
        repository,
    )


def test_second_sqlite_insert_failure_rolls_back_the_whole_ingest_batch() -> None:
    connection = sqlite3.connect(":memory:")
    node, repository = _node(connection)
    connection.execute(
        """
        CREATE TRIGGER fail_second_evaluation
        BEFORE INSERT ON rca_evaluations
        WHEN json_extract(NEW.payload, '$.evaluation_id') = 'eval_2'
        BEGIN
            SELECT RAISE(ABORT, 'injected second write failure');
        END
        """
    )

    result = node.execute({"rows": _rows()})

    assert result == {
        "status": "error",
        "error_log": ["IngestWriteNode: sanitized batch was not written"],
    }
    assert repository.calls == [["eval_1", "eval_2"]]
    assert repository.list_for_scope(SCOPE) == []
    assert connection.execute("SELECT COUNT(*) FROM rca_kb_precedents").fetchone() == (0,)


def test_normal_sqlite_ingest_writes_repository_and_kb_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = sqlite3.connect(":memory:")
    node, repository = _node(connection)
    projection_batches: list[list[str]] = []
    write_kb_projections = domain_workflow_graph._write_kb_projections

    def record_projection_batch(services, rows, scope) -> None:
        projection_batches.append([row["evaluation_id"] for row in rows])
        write_kb_projections(services, rows, scope)

    monkeypatch.setattr(
        domain_workflow_graph,
        "_write_kb_projections",
        record_projection_batch,
    )

    result = node.execute({"rows": _rows()})

    assert result["status"] == "success"
    assert result["ingest_summary"]["written_count"] == 2
    assert len(result["ingest_summary"]["evaluation_refs"]) == 2
    assert repository.calls == [["eval_1", "eval_2"]]
    assert projection_batches == [["eval_1", "eval_2"]]
    assert [row["evaluation_id"] for row in repository.list_for_scope(SCOPE)] == [
        "eval_1",
        "eval_2",
    ]
    assert connection.execute(
        "SELECT source_id FROM rca_kb_precedents ORDER BY source_id"
    ).fetchall() == [("precedent_eval_1",), ("precedent_eval_2",)]
