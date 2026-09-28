# SVC-C2-098 — Outsourcing SLA Deviation RCA Rebuttal-Brief Agent

> **Category**: Cat 2 (domain workflow)
> **Industry**: SVC

## Overview

A vendor misses an SLA target and submits a root-cause analysis. Someone on the client side — a
service delivery manager, a contract manager in a BPO or IT outsourcing relationship — has to read
that free-text RCA against what the incident record actually shows: the timeline, the change
history, the monitoring series, the tickets raised at the time.

This agent prepares that reading. It takes the RCA narrative together with the current evidence
records for the case and drafts an **issue brief**: statements of fact, the sources behind them,
the causal steps that are asserted but not evidenced, observations that point the other way, and
the questions a reviewer should put to the vendor. Every claim it writes carries the identifiers of
the records it was drawn from.

It is deliberately **not** a decision engine. There is no field in its output for "accept" or
"reject", no penalty or SLA-credit calculation, no contract remedy. The response always comes back
with `requires_human_review: true` and a disclaimer to that effect. Acceptance of an RCA, the
commercial consequence, and the reading of the contract stay with the contract manager.

The agent accumulates. Three modes share one graph:

| Mode | Purpose |
|---|---|
| `POST /ingest` | Store sanitized evaluations of past RCAs, so later cases can be read against precedent |
| `POST /invoke` | Draft the issue brief for a new deviation |
| `POST /feedback` | Record a viewpoint a human reviewed and approved, so it joins the precedent set |

Two properties are structural rather than advisory, and they are the reason the design looks the
way it does:

- **The request body never enters graph state.** The HTTP adapter scrubs the incoming JSON, stores
  the raw body in a scope-bound payload store under a server-issued reference, and hands the graph
  a four-key envelope — `mode`, `tenant_id`, `case_ref`, `payload_ref` — and nothing else. The
  stored body is deleted on the way out, on the success and the failure path alike. A
  caller-supplied `payload_ref` is discarded; it can never be used to reach another case's data.
- **The tenant and case partition comes from the verified caller, never from the request.** Scope
  is resolved by the entry adapter from the authenticated principal. Headers and request JSON have
  no say in it, so a caller cannot name a partition it was not issued.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to your
own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime;
- caller authentication in front of the entry adapter — identity middleware that stamps a verified
  trust level, a caller id, and the `tenant_id` / `case_ref` scope;
- a `BaseLLM` implementation, and the domain stores, injected through `wire_services()`.

`wire_services(config)` is the single injection seam. Every dependency it takes has an in-memory
default so the template runs and its tests pass with nothing external attached:

| Injected via `config` | Default when absent |
|---|---|
| `llm` | `None` — the agent falls back to a deterministic brief (see *Behaviour without the platform*) |
| `raw_payload_store` / `brief_store` / `provenance_store` | In-process stores scoped to `tenant_id` + `case_ref`, each with its own TTL |
| `retrieval` | In-memory scoped retrieval |
| `repository` | In-memory case repository |

Runtime parameters read from `config/config.yaml` (defaults in parentheses):

| Key | Meaning |
|---|---|
| `max_retry` (`3`) | Graph retry ceiling |
| `timeout_s` (`30`) | Per-invocation timeout |
| `top_k` (`5`) | Number of past evaluations recalled as precedent |
| `memory_enabled` (`false`) | No checkpointer is attached |
| `hitl.enabled` (`false`) | No interrupt/resume cycle; review is advisory, not blocking |
| `payload_ttl_seconds` / `brief_ttl_seconds` / `provenance_ttl_seconds` (`300`) | Lifetime of each scope-bound reference |
| `ingest_file.allowed_channels` (`["csv"]`) | Ingest accepts lightweight text only — no PDF, no spreadsheet binaries, no OCR |
| `ingest_file.max_file_bytes` (`5242880`) | Ingest file size ceiling |
| `ingest_file.quality` | Text-quality floors an ingested file must clear (printable ratio, replacement-character ratio, control-character ratio, minimum signal) |

The template does not fetch tickets or monitoring data, does not parse binary document formats,
does not compute SLA attainment, and does not talk to a vendor system. Everything it reasons over
is supplied in the request or already accumulated through `/ingest`.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent is designed to run on the platform, which supplies identity, secrets, and the domain stores. Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The standalone server in `src/api/server.py` does start without the platform: `GET /health`
answers `{"status": "ok", "agent": "svc-c2-098", "ingress_isolation": true}`, the in-memory
stores stand in for the domain ones, and the test suite runs with no external connection.

What it does *not* do is silently degrade the security boundary. With no identity middleware and
no `INVOKE_AUTH_TOKEN` set, every request is anonymous and stops at the trust gate with **403**
before any node body runs. With `INVOKE_AUTH_TOKEN` set, a matching bearer token reaches `/invoke`
only; `/ingest` and `/feedback` still answer **403**, because writing to the accumulated record
requires a verified middleware principal that a shared deployment token cannot stand in for.

With no `BaseLLM` injected, `/invoke` still returns **200** — but the brief it returns is a
**deterministic fallback**, not a generated one: fixed, clearly generic statements anchored to the
`source_id`s that were actually supplied. This is a working demonstration of the output contract,
not a substitute for the real thing, and the agent does not report it differently from a generated
brief. Treat an uninjected LLM as an unconfigured deployment.

## Input

Every endpoint takes the same body:

```json
{"input": "<a JSON object, as a string>", "session_id": "optional"}
```

`input` is a JSON **string**, and the object inside it carries a single `payload` key. The endpoint
decides the mode — a `mode` key in the body is overwritten, and a `payload_ref` is discarded.

For `/invoke`, `payload` carries the current evidence records plus whatever free text the reviewer
has. A record is admitted when it has a `source_id` (or `ticket_id`, or `log_id`) of at most 128
characters drawn from `[0-9A-Za-z_-]`; anything else is skipped, and a payload that yields no
admissible record is rejected. Only the identifiers and a short `source_type` reach the graph — the
narrative text stays in the payload store and is used to build the brief, never written into state.

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{
    "input": "{\"payload\": {\"records\": [{\"source_id\": \"monitor_1\", \"source_type\": \"monitor\"}], \"rca\": \"Vendor RCA narrative\", \"sla_definition\": \"availability\", \"measurement_period\": \"2026-07\"}}",
    "session_id": "example-session"
  }'
```

`/ingest` and `/feedback` take `payload.records` as well, shaped as evaluations rather than
evidence — `evaluation_id`, `observed_at`, and a `category` drawn from a closed set
(`causal_evidence`, `log_consistency`, `cap_connection`, `data_quality`). An out-of-set value
rejects the whole batch rather than writing part of it.

### Errors

A rejected request comes back as **422** with `RCA brief processing failed`. The message is
extended with an error code **only** for the failures a caller can act on:

| Named code | Meaning |
|---|---|
| `current_evidence_required` | No admissible evidence record in the payload |
| `canonical_envelope_invalid` | The envelope the adapter built did not satisfy the graph contract |
| `inner_mode_invalid` | The mode did not resolve to a known branch |

Everything else — a provider fault, a repository write failure, a provenance mismatch — stays the
bare message on purpose. Naming an internal fault back to a caller tells them nothing they can fix
and tells an attacker something about the inside. Malformed input is rejected earlier and more
plainly: `input must be a JSON object`, `input contains unsupported values`. A scope that fails its
identifier check is **403**, not 422.

## Output

`/invoke` returns five sections, and only these five — the response is validated against a closed
schema after it leaves the graph, so a field the model invented cannot ride out with it:

```json
{
  "status": "success",
  "output": {
    "facts": [
      {"claim_id": "...", "statement": "...", "source_ids": ["monitor_1"]}
    ],
    "evidence_sources": [
      {"source_id": "monitor_1", "record_kind": "current_evidence", "source_ids": ["monitor_1"]}
    ],
    "unresolved_points": [
      {"claim_id": "...", "point": "...", "source_ids": ["monitor_1"]}
    ],
    "countervailing_observations": [
      {"claim_id": "...", "observation": "...", "source_ids": ["monitor_1"]}
    ],
    "review_questions": [
      {"claim_id": "...", "question": "...", "source_ids": ["monitor_1"]}
    ]
  },
  "requires_human_review": true,
  "disclaimer": "..."
}
```

Values above are illustrative; real output depends on the case supplied.

`requires_human_review` is always `true`. It is not a confidence signal — it is the statement that
this output is an input to a human decision, and the agent has no mode in which that stops being
so.

`/ingest` and `/feedback` return a count, not a brief: `{"status": "success", "output":
{"ingest_summary": {...}}}` and `{"status": "success", "output": {"feedback_summary": {...}}}`.

## Security and Limitations

The five-layer AgentCore model applies, with these template-specific boundaries:

- **Trust.** Every node requires verified-external trust. Anonymous callers are refused before any
  node body runs. Write modes require a verified middleware principal, which a standalone bearer
  token cannot supply.
- **Partition.** `tenant_id` + `case_ref` scope every store operation and every retrieval. A
  reference minted for one scope fails to resolve under another; the check is a hard mismatch
  error, not a filtered empty result.
- **Ingress.** Raw request content is held outside graph state for the life of one request, behind
  a server-issued reference, and deleted in a `finally` block. Checkpointing is off
  (`memory_enabled: false`), so there is no checkpoint for raw content to reach even transiently.
- **Egress.** The `/invoke` response is rebuilt from a scope-bound reference and validated against
  the closed five-section schema. Keys on a forbidden list are rejected outright — the response
  cannot grow a conclusion field by accident.
- **Provenance.** Source identifiers the model cites are checked against the records that were
  actually indexed for this case, and every claim must cite at least one current-evidence
  identifier. A citation to an identifier that was not supplied does not pass the gate — it
  raises, rather than being dropped from the response.
- **Personal data.** Input is scanned by the framework's own detector. That detector does not
  currently recognise Japanese personal names in every form, and this template deliberately does
  not hand-roll a replacement — an honorific-and-context dictionary misses too much to be worth its
  maintenance and over-masking cost. A deployment handling Japanese free text that may carry
  personal names should account for this in its own data handling.

Known limitations, stated rather than hidden:

- The brief is drafted by a language model over the supplied records. Citing a `source_id` proves
  the claim was drawn from a record that exists in this case — it does not prove the claim is a
  correct reading of that record.
- Precedent recall is only as good as what was ingested. An empty precedent set produces a brief
  grounded solely in the current case, and says nothing about that being unusual.
- Ingest accepts lightweight text (CSV by default). Documents that live in PDF or spreadsheet form
  have to be converted before they can be accumulated.
- With no language model injected, the fallback brief is generic by construction and carries no
  case-specific reasoning.

Output is machine-generated and assistive. It does not substitute for expert review, and the
acceptance, commercial, and contractual decisions it feeds must be made by a person.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2), accumulation-based document-generation pattern
- Generation mode: `llm`
- HITL: not enabled — review is an advisory flag on the response, not an interrupt
- Persistence: no checkpointer; domain stores are deployment-injected
- External API clients: deployment-injected; none bundled

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. To exercise the HTTP surface locally:

```bash
export INVOKE_AUTH_TOKEN=local-dev-token
export STG_DEFAULT_TENANT_ID=tenant1
export STG_DEFAULT_CASE_REF=case1
uvicorn src.api.server:app --reload
```

The two `STG_DEFAULT_*` variables supply a scope **only** when the caller is token-authenticated
and no verified middleware scope is present. They are a local-development convenience, not a
production path — a real deployment gets its scope from identity middleware.

`cli.py` is the AGENTIC STAR Marketplace entry point. It serves `/invoke` only; the write modes are
refused there, since a Marketplace caller cannot present the verified middleware principal that
writing to the accumulated record requires. It reads its scope and its Azure OpenAI credentials
(`AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`) from an explicit
allowlist of environment variables and rejects anything else, and it requires the endpoint to be a
bare Azure resource endpoint rather than an arbitrary URL.

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the store and retrieval contracts, and
the security design. `docs/03_test_spec.md` records the test specification and the verification
results.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

---
