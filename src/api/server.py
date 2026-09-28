"""HTTP adapter with the mandatory SVC-C2-098 isolation boundaries.

tenant_id + case_ref are the KB partition: one customer's cases must never be
mixed.  Scope is therefore resolved exclusively by ``authenticate`` from
the verified principal, never from client headers or request JSON.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import yaml

from framework.schemas.invocation_context import InvocationContext
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.entry_adapter import AuthContext, Operation, authenticate
from src.graph.graph import CALLER_FIXABLE_ERROR_CODES, SlaRcaRebuttalBriefAgent
from src.ingest_row_pipeline import CheckpointScrubPolicy, scrub_checkpoint_envelope
from src.services import resolve_invoke_output, wire_services
from src.services.service import SAFE_ID


# The isolation implementation below has replaced the former 503 placeholder
# guard.  Keep a constant (rather than an environment toggle) so deployments
# cannot accidentally re-enable a raw-input checkpoint path.
INGRESS_ISOLATION_READY = True


def _load_runtime_config() -> dict:
    """Load runtime controls before wiring services; malformed config fails startup."""

    root = Path(__file__).resolve().parents[2]
    try:
        runtime_file = yaml.safe_load((root / "config" / "config.yaml").read_text()) or {}
    except (OSError, TypeError, yaml.YAMLError) as exc:
        raise RuntimeError("SVC-C2-098 runtime configuration is invalid") from exc
    if not isinstance(runtime_file, dict):
        raise RuntimeError("SVC-C2-098 runtime configuration is invalid")
    return runtime_file

app = FastAPI(title="SVC-C2-098 Outsourcing SLA Deviation RCA Rebuttal-Brief Agent")
RUNTIME_CONFIG = _load_runtime_config()
services = wire_services(RUNTIME_CONFIG)
app.state.services = services


_MODE_OPERATIONS = {
    "invoke": Operation.READ_INVOKE,
    "ingest": Operation.WRITE_INGEST,
    "feedback": Operation.WRITE_FEEDBACK,
}


class InvocationRequest(BaseModel):
    input: str
    session_id: str = ""


def _safe_json_for_mode(raw: str, mode: Literal["invoke", "ingest", "feedback"]) -> str:
    """Endpoint mode wins; client payload_ref is discarded later by the scrubber."""

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="input must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="input must be a JSON object")
    payload["mode"] = mode
    # Re-serialize without default=str: non-JSON values are rejected, never
    # silently coerced into checkpointable text.
    try:
        return json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="input contains unsupported values") from exc


def _canonical_envelope(raw: str, *, mode: Literal["invoke", "ingest", "feedback"], scope: dict[str, str], session_id: str) -> dict[str, str]:
    if not all(SAFE_ID.fullmatch(scope.get(key, "")) for key in ("tenant_id", "case_ref")):
        raise HTTPException(status_code=403, detail="verified scope contains unsafe identifier")
    safe_raw = _safe_json_for_mode(raw, mode)
    policy = CheckpointScrubPolicy(
        safe_keys=frozenset({"mode", "record_id", "source_record_id", "observed_at", "category", "record_type", "status", "op", "evaluation_id", "version"}),
        scope_keys=frozenset({"tenant_id", "case_ref"}),
        enum_value_keys={
            "mode": frozenset({"invoke", "ingest", "feedback"}),
            "category": frozenset({"causal_evidence", "log_consistency", "cap_connection", "data_quality"}),
            "status": frozenset({"open", "closed", "recorded"}),
            "record_type": frozenset({"evidence", "evaluation", "feedback"}),
            "op": frozenset({"create", "record"}),
        },
        safe_scalar_list_keys=frozenset({"source_ids"}),
        max_safe_string_chars=64,
    )
    scrubbed = scrub_checkpoint_envelope(
        safe_raw,
        services.raw_payload_store,
        session_id=session_id,
        trusted_scope=scope,
        policy=policy,
    )
    try:
        envelope = json.loads(scrubbed)
    except json.JSONDecodeError as exc:  # impossible unless the part contract changes
        raise HTTPException(status_code=500, detail="canonical envelope construction failed") from exc
    # Scope and endpoint mode are set after scrubbing.  A client-provided
    # payload_ref was removed by the part and the replacement is server-issued.
    envelope["mode"] = mode
    envelope["tenant_id"] = scope["tenant_id"]
    envelope["case_ref"] = scope["case_ref"]
    # The scrubber can retain harmless control markers (for example a
    # ``payload`` subtree containing only ``[scrubbed:…]`` markers).  They are
    # deliberately *not* forwarded: the graph contract is the four-key
    # canonical envelope below, not the scrubber's broader intermediary shape.
    return {
        "mode": envelope["mode"],
        "tenant_id": envelope["tenant_id"],
        "case_ref": envelope["case_ref"],
        "payload_ref": envelope["payload_ref"],
    }


def _request_agent(auth: AuthContext, session_id: str) -> SlaRcaRebuttalBriefAgent:
    """Build the graph only after auth, binding trusted context to this request.

    Ingest nodes retain their constructor arguments for the life of a compiled
    graph.  A module-global graph would therefore mix scope/session values
    between concurrent callers.  Services remain process-scoped; graph nodes do
    not.
    """

    request_agent = SlaRcaRebuttalBriefAgent(
        config=RUNTIME_CONFIG,
        services=services,
        trusted_scope=auth.scope,
        request_session_id=session_id,
    )
    request_agent.compile()
    request_agent.provision_secrets(secrets_factory(namespace="SVC", agent_name="svc-c2-098"))
    return request_agent


async def _run(req: InvocationRequest, request: Request, mode: Literal["invoke", "ingest", "feedback"]):
    if not INGRESS_ISOLATION_READY:
        raise HTTPException(status_code=503, detail="Ingress isolation boundary unavailable")
    auth = authenticate(request, operation=_MODE_OPERATIONS[mode])
    scope = auth.scope
    session_id = req.session_id or str(uuid4())
    canonical = _canonical_envelope(req.input, mode=mode, scope=scope, session_id=session_id)
    raw_ref = canonical["payload_ref"]
    request_agent = _request_agent(auth, session_id)
    ctx = InvocationContext(
        session_id=session_id,
        caller_trust_level=auth.trust,
        caller_id=auth.principal["caller_id"],
        hitl_allowed=False,
    )
    try:
        with bound_secrets(request_agent._secrets_provider):
            result = request_agent.invoke(
                json.dumps(canonical, ensure_ascii=False, sort_keys=True),
                ctx=ctx,
                input_context={"scope": scope},
            )
    except Exception as exc:
        # error_strategy=propagate: no partial graph data is returned.
        raise HTTPException(status_code=422, detail="RCA brief processing failed") from exc
    finally:
        # The raw incoming body has served the inner stages; retain neither a
        # successful nor failed request beyond this response transaction.
        services.raw_payload_store.delete(raw_ref, scope=scope, session_id=session_id)

    if result.get("status") != "success" or not isinstance(result.get("output"), dict):
        # Name the failure when the graph declared the code as publicly safe.
        # Without this a caller who omitted the evidence records saw exactly the
        # same string as a genuine internal fault, with no way to tell them apart.
        code = result.get("error_code")
        detail = "RCA brief processing failed"
        if isinstance(code, str) and code in CALLER_FIXABLE_ERROR_CODES:
            detail = f"{detail}: {code}"
        raise HTTPException(status_code=422, detail=detail)
    formatted = result["output"]
    if mode == "invoke":
        try:
            output = resolve_invoke_output(
                services.brief_store,
                formatted.get("brief_ref", ""),
                scope=scope,
                session_id=session_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="RCA brief response blocked") from exc
        return {
            "status": "success",
            "output": output,
            "requires_human_review": True,
            "disclaimer": "論点整理でありRCA受理判断・ペナルティ算定・契約是正ではありません。契約解釈は人間が行ってください。",
        }
    key = "ingest_summary" if mode == "ingest" else "feedback_summary"
    return {"status": "success", "output": {key: formatted.get(key, {})}}


@app.post("/invoke")
async def invoke(req: InvocationRequest, request: Request):
    return await _run(req, request, "invoke")


@app.post("/ingest")
async def ingest(req: InvocationRequest, request: Request):
    return await _run(req, request, "ingest")


@app.post("/feedback")
async def feedback(req: InvocationRequest, request: Request):
    return await _run(req, request, "feedback")


@app.get("/health")
def health():
    return {"status": "ok", "agent": "svc-c2-098", "ingress_isolation": INGRESS_ISOLATION_READY}
