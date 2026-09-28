"""Domain service seams for the RCA rebuttal-brief workflow.

The default implementations are deliberately in-memory.  Production wiring
can inject the platform DB, KB, and BaseLLM clients through ``wire_services``;
nodes never construct provider clients or read environment variables.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from src.payload_store import PayloadStore

try:  # wheel 1.0.0 exposes the class from its concrete module
    from shared.services.llm import BaseLLM
except ImportError:  # pragma: no cover - compatibility with the supplied wheel
    from shared.services.llm.base_llm import BaseLLM


SAFE_ID = re.compile(r"^[0-9A-Za-z_-]{1,128}$")


class RetrievalBackend(Protocol):
    def search(self, *, scope: Mapping[str, str], query: str, top_k: int) -> list[dict[str, Any]]: ...


def normalize_kb_query(value: Any) -> str:
    """Use one conservative normalisation form for KB read and write paths."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(text.split())[:160]


class InMemoryScopedRetrieval:
    """Injection-friendly retrieval backend with strict tenant/case filtering."""

    def __init__(self) -> None:
        self._documents: list[dict[str, Any]] = []

    def add_projection(self, record: dict[str, Any]) -> None:
        # The caller supplies an already-sanitized retrieval-safe projection.
        self._documents.append(dict(record))

    def search(self, *, scope: Mapping[str, str], query: str, top_k: int) -> list[dict[str, Any]]:
        if set(scope) != {"tenant_id", "case_ref"} or not query.strip():
            return []
        needle = normalize_kb_query(query)
        matches = []
        for document in self._documents:
            metadata = document.get("metadata", {})
            if not isinstance(metadata, dict):
                continue
            if metadata.get("tenant_id") != scope["tenant_id"] or metadata.get("case_ref") != scope["case_ref"]:
                continue
            haystack = normalize_kb_query(document.get("summary", ""))
            if not needle or any(token in haystack for token in needle.split() if len(token) > 2):
                matches.append(dict(document))
        return matches[:top_k]


class SQLiteScopedRetrieval:
    """SQLite retrieval adapter that returns scope read from stored rows.

    The request scope is used only in the parameterized ``WHERE`` clause.  Hit
    scope is projected from selected columns, allowing scoped-retrieval v0.3.0
    to independently reject an under-filtered or incorrectly projected hit.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS rca_kb_precedents "
            "(tenant_id TEXT NOT NULL, case_ref TEXT NOT NULL, source_id TEXT NOT NULL, "
            "summary TEXT NOT NULL, metadata TEXT NOT NULL, "
            "PRIMARY KEY (tenant_id, case_ref, source_id))"
        )

    def add_projection(self, record: dict[str, Any]) -> None:
        metadata = record.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("retrieval_projection_scope_invalid")
        scope = {"tenant_id": metadata.get("tenant_id"), "case_ref": metadata.get("case_ref")}
        if not _valid_scope(scope):
            raise ValueError("retrieval_projection_scope_invalid")
        source_id = record.get("source_id")
        summary = record.get("summary")
        if not isinstance(source_id, str) or not SAFE_ID.fullmatch(source_id) or not isinstance(summary, str):
            raise ValueError("retrieval_projection_invalid")
        safe_metadata = {
            key: value
            for key, value in metadata.items()
            if key not in {"tenant_id", "case_ref"}
        }
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO rca_kb_precedents"
                "(tenant_id, case_ref, source_id, summary, metadata) VALUES (?, ?, ?, ?, ?)",
                (
                    scope["tenant_id"],
                    scope["case_ref"],
                    source_id,
                    normalize_kb_query(summary),
                    json.dumps(safe_metadata, ensure_ascii=False, sort_keys=True),
                ),
            )

    def search(self, *, scope: Mapping[str, str], query: str, top_k: int) -> list[dict[str, Any]]:
        if not _valid_scope(scope) or not isinstance(top_k, int) or top_k < 1:
            return []
        normalized_query = normalize_kb_query(query)
        if not normalized_query:
            return []
        tokens = [token for token in normalized_query.split() if len(token) > 2]
        rows = self._connection.execute(
            "SELECT tenant_id, case_ref, source_id, summary, metadata "
            "FROM rca_kb_precedents WHERE tenant_id = ? AND case_ref = ? LIMIT ?",
            (scope["tenant_id"], scope["case_ref"], top_k),
        ).fetchall()
        hits: list[dict[str, Any]] = []
        for stored_tenant, stored_case, source_id, summary, metadata_json in rows:
            if tokens and not any(token in summary for token in tokens):
                continue
            metadata = json.loads(metadata_json)
            metadata.update({"tenant_id": stored_tenant, "case_ref": stored_case})
            hits.append({"source_id": source_id, "summary": summary, "metadata": metadata})
        return hits


@dataclass
class InMemoryRcaRepository:
    """Transactional test/local persistence seam for sanitized audit records."""

    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    feedback_audit_events: list[dict[str, str]] = field(default_factory=list)

    def write_batch(self, rows: list[dict[str, Any]], *, scope: Mapping[str, str], feedback: bool = False) -> list[str]:
        # All validation/sanitization happens before this call.  Copy first so a
        # failed batch cannot leave a partial write behind.
        staged: dict[str, dict[str, Any]] = {}
        ids: list[str] = []
        for row in rows:
            evaluation_id = str(row["evaluation_id"])
            version = str(row.get("version", "1"))
            digest = hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
            key = f"{scope['tenant_id']}:{scope['case_ref']}:{evaluation_id}:{version}:{digest}"
            staged.setdefault(key, dict(row))
            ids.append(key)
        self.records.update(staged)
        if feedback:
            self.feedback_audit_events.extend(
                {"tenant_id": scope["tenant_id"], "case_ref": scope["case_ref"], "evaluation_id": str(row["evaluation_id"])}
                for row in rows
            )
        return ids


class SQLiteRcaRepository:
    """Minimal real-SQL repository seam used by PB isolation verification.

    STG wiring may inject the platform database implementation instead.  This
    adapter keeps tenant/case predicates parameterized and fail-closed so the
    same boundary can be exercised without a networked DB.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS rca_evaluations "
            "(storage_key TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, case_ref TEXT NOT NULL, payload TEXT NOT NULL)"
        )

    def write_batch(self, rows: list[dict[str, Any]], *, scope: Mapping[str, str], feedback: bool = False) -> list[str]:
        if not _valid_scope(scope):
            raise ValueError("repository_scope_invalid")
        entries = [(_evaluation_storage_key(row, scope), scope["tenant_id"], scope["case_ref"], json.dumps(row, sort_keys=True)) for row in rows]
        with self._connection:
            self._connection.executemany(
                "INSERT OR IGNORE INTO rca_evaluations(storage_key, tenant_id, case_ref, payload) VALUES (?, ?, ?, ?)",
                entries,
            )
        return [entry[0] for entry in entries]

    def list_for_scope(self, scope: Mapping[str, str]) -> list[dict[str, Any]]:
        if not _valid_scope(scope):
            return []
        rows = self._connection.execute(
            "SELECT payload FROM rca_evaluations WHERE tenant_id = ? AND case_ref = ?",
            (scope["tenant_id"], scope["case_ref"]),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]


def _valid_scope(scope: Mapping[str, str]) -> bool:
    return (
        set(scope) == {"tenant_id", "case_ref"}
        and all(isinstance(scope[key], str) and bool(SAFE_ID.fullmatch(scope[key])) for key in scope)
    )


def _evaluation_storage_key(row: dict[str, Any], scope: Mapping[str, str]) -> str:
    evaluation_id = str(row["evaluation_id"])
    version = str(row.get("version", "1"))
    digest = hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    return f"{scope['tenant_id']}:{scope['case_ref']}:{evaluation_id}:{version}:{digest}"


@dataclass(frozen=True)
class DomainServices:
    raw_payload_store: PayloadStore
    brief_store: PayloadStore
    provenance_store: PayloadStore
    retrieval: RetrievalBackend
    repository: Any
    llm: BaseLLM | None = None


def wire_services(config: Mapping[str, Any] | None = None) -> DomainServices:
    """Create the server-owned service bundle before the first invocation.

    ``config`` is an explicit injection seam for the real LLM/KB/DB clients.
    No credentials, model identifiers, or client construction appear in nodes.
    Separate raw and brief stores make the opaque references purpose-bound.
    """

    supplied = dict(config or {})
    return DomainServices(
        raw_payload_store=supplied.get("raw_payload_store")
        or PayloadStore(scope_keys=("tenant_id", "case_ref"), ttl_seconds=int(supplied.get("payload_ttl_seconds", 300))),
        brief_store=supplied.get("brief_store")
        or PayloadStore(scope_keys=("tenant_id", "case_ref"), ttl_seconds=int(supplied.get("brief_ttl_seconds", 300))),
        provenance_store=supplied.get("provenance_store")
        or PayloadStore(scope_keys=("tenant_id", "case_ref"), ttl_seconds=int(supplied.get("provenance_ttl_seconds", 300))),
        retrieval=supplied.get("retrieval") or InMemoryScopedRetrieval(),
        repository=supplied.get("repository") or InMemoryRcaRepository(),
        llm=supplied.get("llm"),
    )
