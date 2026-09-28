# Test Specification — SVC-C2-098

## Scope and execution

Unit tests use injected LLM/KB/DB/store seams only.  No test opens a network
connection.  The common-part layer (`tests/parts/`) remains enabled; the
following layer-2 cases concretize its contracts for tenant + case isolation.

| Command | Result |
|---|---|
| `source .venv/bin/activate && pytest tests/ -q` | 451 passed, 2 skipped (PB-7 auto-waived) |
| `source .venv/bin/activate && pytest tests/proof_of_boundary/ -q` | 45 passed, 2 skipped (PB-7 auto-waived) |

## Framework compliance (TC)

| ID | Test | Expected result | Evidence |
|---|---|---|---|
| TC-01 | `test_tc01_state_is_flat_typed_dict_with_ref_fields_only` | State is flat and only holds refs/control data | `tests/unit/test_rca_security_contract.py` |
| TC-02 | unknown cited ID | `SecurityViolationError`; no draft | same, `test_tc02...` |
| TC-03 | forbidden decision field, including nested | S-3 structural reject | same, `test_tc03...` |
| TC-04 | canonical inner input | InvocationContext/raw body absent; opaque ref only | same, `test_tc04...` |
| TC-05 | lifecycle audit | domain `execute` has no duplicate `node_start/complete/error` | same, `test_tc05...` |
| TC-06 | S-2 extension | FunctionNode subclasses use `_extra_security_gate_input` | same, `test_tc06_tc07...` |
| TC-07 | S-3 extension | FunctionNode subclasses use `_extra_security_gate_output` | same, `test_tc06_tc07...` |
| TC-08 | trust gate | anonymous caller is refused。加えて PB-6 の常設フィクスチャで **`execute()` が呼ばれなかったこと**を証明する（`test_pb_invoke_order.py` の `test_s1_denial_refuses_execution_before_execute` — `_PrivilegedTrustGateFixture` を不足 trust で `node(state)` 呼びし、`status=error` / `error_log[0]` の `S-1 trust gate denied` / `events == ["s1_denied"]` / `execute` 未呼び出し を assert。正本 docs/03 の TC-08 要求） | same, `test_tc08...` |

## Part integration (layer 2)

| Part | Domain-specific assertion |
|---|---|
| cat2-graph-skeleton | cached inner graph, `propagate_hitl=False`, error strategy `handle`, safe `error_code` / `error_type` propagation, and closed mode merge contract |
| entry-adapter | `tenant_id` + `case_ref` derive only from verified scope; standalone token は `READ_INVOKE` のみ、ingest/feedback は middleware identity 必須 |
| payload-store | raw and brief refs have separate purpose stores; tenant/case/session mismatch fails closed |
| output-envelope | invoke State carries `brief_ref` only; HTTP output validates exactly the five approved fields |
| evidence-gate | every claim needs `current_evidence`; response contract から生成した正常 draft が通り、宣言外・必須欠落・dict-in-str・dict-in-list[str]・enum 外は typed projection で拒否 |
| scoped-retrieval | backend receives exact tenant/case scope; 実 SQLite の保存列由来 scope を hit に投影して A 区画 1 件だけを返し B 区画を不可視化 |
| ingest-row-pipeline | client `payload_ref` is always replaced; `AuthContext.scope` / session は constructor 注入、並行 2 request が scope/session を混線せず、state 偽装では置換不能 |
| ingest-file-sanitizer | documented `input.payload.file_b64` CSV only; XLSX/PDF/quality failures reject before write |
| llm-draft-stage | validation constants から section/key/type/enum/上限を prompt contract に生成。LLM 未設定だけ fallback、provider/parse/contract failure は error で例外本文を非露出 |

### `parts/TESTING.md` junction cases

| ID | This template's concrete proof |
|---|---|
| R-1 / PB-3 | production `SQLiteScopedRetrieval` に A/B 2 区画を実 INSERT し、A の検索で A の保存行由来 scope 付き hit 1 件を確認 |
| R-2 / R-5 | SQL `WHERE tenant_id=? AND case_ref=?` と保存列 projection。hit の scope 欠落/一部欠落/二重不一致/正常一致は部品同梱 test を維持 |
| R-3 / R-4 | allowlist projection と 0-hit success degradation は部品同梱 test、ドメイン adapter は受理結論フィールドを保存しない |
| V-1 / V-4 | provenance の current-evidence 必須に加え、`FindingField` の正常/宣言外/必須欠落/型違い/list 要素違い/enum 外を双方向確認 |
| L-2 / L-4 / L-5 | malformed JSON、provider/response 例外、未設定 fallback の境界と stable error code/例外本文非露出を部品 test + domain exception marker test で確認 |
| N-1..N-6 | scope header 非採用、token/STG 境界、operation enum、write principal、partial stamp、予約 caller prefix を部品 test。実 `/ingest` への token は 403 |
| I-4..I-8 | scalar 全型 scrub、実 `PayloadStore.resolve(..., consume=True)`、constructor trusted context、state 置換拒否、並行 2 request 分離を確認 |
| X-2 | typed projection 呼出しを一時的に bypass し、実ファイルへの変異を `rg` で確認後、undeclared-key test が赤になることを確認して復元 |
| X-2-bis | outer `get_output()` の error 射影を一時的に削除し、`graph.invoke()` の `error_code` test が赤になることを確認して復元 |

## Proof of boundary (PB)

| ID | Boundary | Test / expected result |
|---|---|---|
| PB-1 | audit | domain nodes emit domain audit events; no raw RCA/monitoring text in payload |
| PB-2 | State serialization | ingress integration asserts raw text and forged ref never appear in result; State safety AST test remains green |
| PB-3 | external seams | sqlite injection test proves tenant/case predicates isolate KB/repository reads; deterministic fallback has no network client |
| PB-4 | import isolation | `tests/proof_of_boundary/test_import_isolation.py` AST scan passes |
| PB-5 | checkpoint/ref safety | actual outer+inner `InMemorySaver.list()` snapshots contain no raw RCA/monitoring/brief/provenance contract keys; refs are scope+session bound; resolved brief rejects framework-`detect_pii`-covered PII (email/phone) while accepting normal Japanese operations vocabulary (SVC-C2-098 B-5: bespoke Japanese-name supplement removed — FW-standard `detect_pii` only) |
| PB-6 | invoke ordering | `tests/proof_of_boundary/test_pb_invoke_order.py` verifies S-1 → S-4 start → S-2 → execute → S-3 → S-4 complete |
| PB-7 | HITL interrupt | **Auto-waived — non-HITL.** `hitl.enabled: false`, `memory_enabled: false`, and the supplied skip stub remains unchanged. |

## Business acceptance cases

| ID | Scenario | Expected result |
|---|---|---|
| BL-01 | valid invoke with current monitoring/ticket IDs | source-linked `facts`, `evidence_sources`, unresolved/countervailing/review fields; `requires_human_review=true` |
| BL-02 | LLM unavailable | deterministic source-linked fallback, never a decision/penalty/remedy |
| BL-03 | fabricated/missing LLM citation | provenance gate fails closed; no first-source fallback is applied to successful LLM output |
| BL-04 | precedent used as present-case evidence | provenance gate fails closed |
| BL-05 | cross-tenant/case brief resolve | payload-store scope mismatch fails closed |
| BL-06 | ingest batch contains one unsafe row | no row from that batch is persisted |
| BL-07 | feedback row | only explicit `/feedback` persists a sanitized audit event |

## Result interpretation

Two skipped tests are the intentional PB-7 scaffold skips for this non-HITL
agent. DOCX dependencies are installed and the adversarial DOCX cases execute.
A graph error is handled into stable `error_code` / `error_type` fields without
provider-authored text; the HTTP adapter still returns 422 without partial
business output.

## 実 LLM 1 回転検証

モックだけで固めた実装は実 LLM を繋いだ瞬間に動かないことがあるため、注入シームへ実 LLM クライアントを刺して 1 回 invoke し、**中身のある出力が出ること**を確認している（`gitproject-dev-flow.md` §14-1-bis）。

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-20 |
| 使用 LLM | ローカル CLI（Azure OpenAI）を `BaseLLM` 実装として注入 |
| 注入シーム | `config[llm]` |
| 実 LLM 呼び出し | 1 回 |
| 出力 | `brief_ref` / `mode` / `gate_kind` が非空。縮退理由なし |
| ブリーフ実体 | `brief_ref` は不透明参照なので、参照が立っただけでは中身の証拠にならない。`src/services/output_resolver.py` の `resolve_invoke_output()` で解決して実体を確認した — `facts` 5 件 / `evidence_sources` 5 件 / `unresolved_points` 4 件 / `countervailing_observations` 5 件 / `review_questions` 5 件（いずれも `source_ids` が `retrieved_records` の ID に接地し、RCA 受諾・違約金・SLA クレジットの判断は含まない） |

本番の LLM は `cli.py` が構築する Azure OpenAI クライアント（`BaseLLM` 実装）で、これは検証用シーム。テンプレの `src/` は変更していない（一時テストを生成→実行→削除する方式）。CI には入れていない（外部 CLI と鍵が要るため）。

## llm-draft-stage 0.3.0 の回帰テスト（LLM 出力の複数 JSON）

AGENTIC STAR の登録モデルが draft JSON を改行区切りで 2 回連結して返す、または JSON の後ろに `{` `}` を含む散文を付ける応答で、
Stage の parse が `Extra data` になり fail-closed していた。部品 `llm-draft-stage` を 0.3.0 に更新し、複数候補を走査して
一意な非空 object だけを受理する（相異なる複数は `AmbiguousJsonOutput` で fail-closed・`{}` / NaN / 配列は不受理）。
対応テスト: `tests/parts/llm-draft-stage/test_draft_node.py`（実物 fixture `fixtures/gpt54mini_duplicated_decision.txt` を含む）。

## Stage 3 プロンプトの claim_id 一意性（0.1.2）

登録モデル（gpt-5.4-mini）は `claim_1`〜`claim_N` をセクション間・セクション内で使い回すため、`_normalise_draft` の
validate-not-repair ゲートが `draft claim identifier is duplicated` で拒否し、`status=error` になっていた
（2026-09-17・登録値モデルでローカル 40 回: **28/40 success・12 error・12 件とも同一原因**を生応答の再検証で確認）。
`_response_contract()` は型・件数・enum を伝えるが一意性を表現できないため、`_build_prompt` に
「Every claim_id must be unique across the whole response …」を固定した。ゲート側は無改変（修復しない）。

| ID | 観点 | 内容 | テスト |
|---|---|---|---|
| C-1 | プロンプトがゲートの規則を伝える | `_build_prompt` に一意性の文が含まれ、同じ claim_id を各セクションに置いた draft は依然 `duplicated` で拒否される（ゲート無改変の証明） | `tests/unit/test_review_remediations.py::test_prompt_states_the_claim_id_uniqueness_the_validator_enforces` |

計測（登録値モデル・ローカル runner 相当・同一入力）: 文なし 28/40 → 文あり **40/40**。N=40 の 0 失敗は内容退行の検査であり率の保証ではない。
