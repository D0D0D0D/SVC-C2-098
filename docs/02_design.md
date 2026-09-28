# Template Design Specification — SVC-C2-098 Outsourcing SLA Deviation RCA Rebuttal-Brief Agent

## Position in AgentCore Architecture

- **Agent Class**: `SlaRcaRebuttalBriefAgent`（`src/graph/graph.py`）
- **L1 Base**: AgentBaseGraph （L1 直接継承。`AutonomousBaseGraph` ではない）
- **Base pattern**: `DocGenerationAgent`（蓄積型 3 モード。`ingest` / `invoke` / `feedback`）
- **Composition**: outer `AgentBaseGraph` の `main` スロットに `GraphNode`（内側に domain workflow graph）
- **Three-Layer Separation**:
  - State: flat TypedDict composition（Pydantic 不可 — msgpack 非互換）
  - Node: L1 inheritance（Template Method: `execute(self, state) -> dict` のみ override）
  - Graph: composition（`register_nodes()` でノード差し込み）

> 本テンプレートは既存の AMLDispositionMemoAgent / GMP Manufacturing Deviation & CAPA Report Drafting Agent と同じ Cat 2 蓄積型パターン
> （`AgentBaseGraph` + `main` の `GraphNode` + 内側 `DomainWorkflowGraph`）を踏襲する。旧 Level 2 base
> 継承モデル（`from agents.base ...`）は 2026-05-18 に廃止済みで、本テンプレートは L1 を直接継承する。

## Architecture Overview

### モード（3 モード）

| モード | エンドポイント | 責務 | trust 最小 |
|--------|---------------|------|-----------|
| ingest | `/ingest` | 過去 RCA 評価・受入基準・差戻し理由を構造化して DB / KB に蓄積（書込前サニタイズ必須） | `VERIFIED_EXTERNAL` |
| invoke | `/invoke` | 新規 SLA 逸脱案件について RCA 主張 × 実測データを照合し反証論点ブリーフを起草 | `VERIFIED_EXTERNAL` |
| feedback | `/feedback` | 人間が確定・編集した評価を明示承認 + 監査イベント付きで DB / KB に反映 | `VERIFIED_EXTERNAL` |

`entry-adapter v0.4.0` の operation policy は endpoint 名でなく実副作用へ対応付ける。`invoke` は
`READ_INVOKE`（永続 KB/DB を変更しない）、`ingest` は `WRITE_INGEST`、`feedback` は
`WRITE_FEEDBACK` とする。standalone token / STG default は `READ_INVOKE` のみ許可し、書込は
verified middleware trust + 非空 `caller_id` を必須とする。認証完了後に scope/session を注入して
外側/内側 graph を request ごとに構築する。module-global に置くのは stateless な設定と service bundle
だけで、`IngestRowPipelineNode` を含む graph/node インスタンスは共有しない。

> モード分岐は backbone の辺に持ち込まない。`pre_process → main` は無条件辺、`route()` は `main` の後に
> `status` を見るだけ。モード分岐は `main`（`GraphNode`）の**内部**（内側 `DomainWorkflowGraph`）で行う。

### Node Configuration

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| initialize | 既定初期化 | — | — | InitializeNode (default) |
| pre_process | テナント/権限解決 fail-closed・入力 schema 検証・S-2 機微情報最小化・案件参照 ID 化 | `mode`, `user_input`, `tenant_id`, `case_ref` | `validated_input`, `payload_ref` | `PreProcessNode(FunctionNode)` |
| main | `GraphNode`。内側 `DomainWorkflowGraph` を呼び、モード別に Stage1-4 / ingest / feedback を実行。S-3 Provenance 検証を境界で実施 | `validated_input` | `result`(参照 ID のみ), `status`, HITL 3 fields | `SlaRcaGraphNode(GraphNode)` |
| post_process | 出力整形・S-3 安全検査（禁止フィールド混入防御・PII 再走査）。resolve はしない | `result` | `formatted_output`(参照 ID のみ) | `PostProcessNode(FunctionNode)` |
| finalize | 既定終了 | — | — | FinalizeNode (default) |

#### 内側 `DomainWorkflowGraph`（`src/graph/domain_workflow_graph.py`・`BaseGraph` 継承）

`invoke` モードのメインパス（Stage 1 → 4）:

| Stage | ノード | 適用部品 | 主要ロジック | 出力（内側 State） |
|-------|--------|---------|-------------|-------------------|
| Stage 1 | `deterministic_check_node` | （ドメイン固有） | SLA 定義・測定期間・必須添付を決定論的に検証（因果十分性の結論は出さない）。evidence log/ticket を `record_kind="current_evidence"` として `retrieved_records` に積む | `evidence_present` / `evidence_gaps`, `retrieved_records`(current_evidence 分) |
| Stage 2 | `precedent_recall_node` | `scoped-retrieval` | 同一テナント KB の過去 RCA 評価観点を想起（受理結論は継承しない）。retrieval-safe projection のみ検索。過去事例を `record_kind="precedent"` として `retrieved_records` に追加 | `recalled_view_points`, `retrieved_records`(precedent 分追加) |
| Stage 3 | `rebuttal_draft_node` | `llm-draft-stage` + `evidence-gate` typed projection | LLM が RCA 自由記述を障害時系列・監視値・チケットに当てはめ、因果の飛躍・不整合・CAP 未接続を区分抽出。検証側の `FindingField` 定数から response contract（section/key/type/enum/件数/本文上限）を生成して prompt に載せ（contract で表現できない **claim_id の全体一意性**は prompt の文で明示・0.1.2）、`project_typed_findings` で宣言外・必須欠落・型違いを fail-closed 射影する。`build_evidence` で `evidence_map` / `cited_source_ids` を生成 | `draft_brief`（区分別 facts/unresolved/countervailing/questions）, `evidence_map`, `cited_source_ids` |
| Stage 4 | （GraphNode 境界） | `evidence-gate` | `EvidenceGateNode._extra_security_gate_output()` が gate_kind="draft" 出力の provenance を機械検証（上記 §S-3）。claim→source 対応・precedent 証拠化禁止 | 検証済み `draft_brief`（正本キーは射影で除去）, `requires_human_review` |

`ingest` / `feedback` モードは内側で別ノード（`ingest_node` / `feedback_node`）へ分岐し、部品
`ingest-row-pipeline`（checkpoint スクラバ allowlist + 書込前サニタイズ）と `ingest-file-sanitizer`
（形式ゲート・品質ゲート）を通す（層2 データ統治）。

> **実装方式 = 共通部品の写経適用**（parts/ ライブラリ・パイロット Shooting Plan Risk Advisory & Incident History Agent で骨格無改変適用を実証）。
> 適用部品と adaptation は §適用部品指定表を参照。ゼロから実装せず、adaptation_points（キー名・prompt・
> fallback・scope・sanitize フック・経路 specs）のみをドメインに合わせる。

### Data Flow

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                            ↓ (retry, max 3)
                                          pre_process
```

### State Definition（flat TypedDict — `State(AgentState)`）

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| `mode` | `str` | `"ingest"` / `"invoke"` / `"feedback"` | ✅ |
| `tenant_id` | `str` | テナント境界キー（fail-closed 解決） | ✅ |
| `case_ref` | `str` | SLA 逸脱案件の参照 ID | invoke/feedback |
| `validated_input` | `str` | pre_process が検証・直列化した入力（`json.dumps(..., default=str)`） | ✅ |
| `payload_ref` | `str` | 生 RCA / 監視データ本文の**参照ハンドル**（実本文は checkpoint に載せない） | ✅ |
| `result` | `dict` | ブリーフ結果（**参照 ID のみ**・実値ゼロ） | — |
| `formatted_output` | `dict` | 整形済み出力（**参照 ID のみ**） | — |
| `requires_human_review` | `bool` | HITL ルーティング要否 | — |
| `status` | `str` | `AgentStatus` の値（`"success"` / `"error"` / `"retry"`） | ✅ |
| HITL 7 fields | schema 1.4 | `hitl_feedback` / `hitl_metadata` / `hitl_status` / `hitl_draft` / `hitl_count` / `hitl_allowed` / `subgraph_thread_id` | HITL 時 |

**State Constraints (mandatory):**
- Flat TypedDict only（primitives + JSON-serializable）
- JWT / API keys / credentials を State に置かない（checkpoint DB 漏洩）
- InvocationContext は `config["configurable"]` 経由のみ（State に載せない）
- Pydantic / dataclass / arbitrary Python objects 不可（msgpack 非互換）
- **センシティブ実値（RCA 全文・監視生データ・機微運用情報）を `result` / `formatted_output` /
  checkpoint される State キーに一切載せない**（下記 §Sensitive Data 2-Layer Governance）
- **内側 `DomainWorkflowGraph` の State も同じ制約を負う**（checkpointer は inner graph の super-step 毎にも
  State を永続化する）。内側 State（別 `InnerState` schema）も **ref / ID / hash / 列挙値のみ**。RCA 本文・
  監視値・LLM 生成 `draft_brief` の実体はローカル変数または隔離 store（`brief_ref`）に置き、内側 State には
  参照ハンドルのみ載せる。`validated_input` の直列化は `default=str` を使わず**許可型以外は reject**する
  （`default=str` は非許可型を黙って文字列化して機微値を通す）。TypedDict に未宣言でも実行時 dict は永続化
  されるため、宣言の有無ではなく「何を書くか」で守る。

## Framework Utilization

### Shared Components Used
- [x] InvocationContext（correlation_id, session_id, caller_trust_level, hitl_allowed）— `InvocationContext.from_state(state)` で取得（直接構築禁止）
- [x] SecurityViolationError（S-3 Provenance 違反・禁止フィールド混入時に raise）
- [x] S-2: `_extra_security_gate_input()` — `PreProcessNode` で機微運用情報 / PII の追加走査・入力サイズ上限・テナント解決
- [x] S-3: `_extra_security_gate_output()` — **`SlaRcaGraphNode`（GraphNode 境界）** で Provenance ID 検証 + 禁止フィールド排除。`PostProcessNode` では防御的安全検査（禁止キー混入 raise・PII 再走査）のみ
- [x] S-4: `emit_trace_event()` — 各ノードの `execute()` 内で最低 1 ドメインイベント（`node_start/complete/error` は `__call__` が自動 emit — 重複させない）

> **S-2/S-3 gate behaviour by node type (ADR-017):**
> - `FunctionNode` subclass（`PreProcessNode` / `PostProcessNode`）→ framework `@final` gate が自動実行。
>   拡張は `_extra_security_gate_input/output()` のみ（`_security_gate_*` 直 override は class 定義時 `TypeError`）
> - `GraphNode`（`SlaRcaGraphNode`）→ `_security_gate_input/output` は意図的 no-op で `_extra_*` へ委譲。
>   **S-3 Provenance 検証はここに置く**（内側 graph 出力が丸ごと見える唯一の境界）

### S-3 Provenance ID 検証（本テンプレートの中核安全要件）

過去事例を想起し LLM が根拠リンクを引用する設計のため、**LLM が引用した evidence log / ticket / precedent
ID が Stage1/Stage2 の実出力に存在するかを機械照合する**（ID hallucination 防止。AquacultureEnvironmentalEarlyWarningAgent で
eng-security が "critical — novel pattern" として全過去事例想起型 AG に指示した契約）。

実装場所は **`SlaRcaGraphNode._extra_security_gate_output(result)`**（GraphNode 境界）。理由は、
照合の**正本**（Stage1 の evidence index・Stage2 の実在事例リスト = 機微データ本体）を `PostProcessNode` に
届けると outer state が checkpointer に永続化され、正本が checkpoint DB に平文で残るため。GraphNode フックなら
検証に正本を使い、**検証後に正本キーを落として返せる**のが唯一この境界の利点。

**実装は共通部品 `evidence-gate`（`ProvenanceContract` / `EvidenceGateNode`）を写経適用する**（origin =
GMP Manufacturing Deviation & CAPA Report Drafting Agent / SafetyPatrolRecordAccumulationWeatherMatchingAdvisoryAgent の実証済コード）。部品の契約キー名に本テンプレの Stage 出力を合わせる:

| evidence-gate 契約（`ProvenanceContract`） | 本テンプレでの割当（adaptation） |
|---|---|
| `gate_kind` | `"draft"`（invoke 経路の gate_kind。他モードは provenance を持たない） |
| `retrieved_records_key` = `retrieved_records` | Stage1 evidence log/ticket ID + Stage2 過去事例を**統合した実レコード集合**。各レコードに `record_kind`（`"current_evidence"` = Stage1 由来 / `"precedent"` = Stage2 由来）を付与 |
| `cited_ids_key` = `cited_source_ids` | Stage3-4 で LLM が引用した ID |
| `evidence_map_key` = `evidence_map` | claim→source 対応（各論点にどの source が対応するか） |
| `record_id_keys` | `retrieved_records` 内の ID キー候補（`source_id` / `case_id`） |

部品 `EvidenceGateNode._extra_security_gate_output(result)` が以下を機械検証する（GraphNode 境界）:
1. `gate_kind="draft"` の出力は `evidence_map` / `cited_source_ids` / `retrieved_records` を必須（欠落→raise）
2. `cited_source_ids` と `evidence_map` 内の全 ID が `retrieved_records` の ID 集合に存在（ID hallucination→raise）
3. 引用 ID とレコード ID は `[0-9A-Za-z_-]` のみ許可
4. `execute()` は contract キーだけを射影（`node_history` / `error_log` を返さない）＝正本キーは checkpoint に残らない

**本テンプレ固有の adaptation（部品標準に無い追加検証）** — `ProvenanceContract` を継承して足す:
- **precedent の証拠化禁止**: `record_kind="precedent"`（Stage2 過去事例）の ID は「現案件の evidence」として
  claim に対応させられない（過去事例は観点想起のみ）。`evidence_map` の各 claim が最低 1 件の
  `record_kind="current_evidence"` に対応することを要求。→ `StrictCoveragePolicy` 相当で追加。
- tenant/case/time-window の一致検証（引用 ID が当該案件スコープ内か）。

> **`_extra_security_gate_output(result)` は `state` を受け取らない**（部品の実シグネチャ）。mode/経路の判定は
> `merge_output()` が `result` に宣言した `gate_kind` に依存させる（下記 §経路 allowlist）。`gate_kind="draft"`
> のときだけ provenance キーが present、という不変条件で fail-open を防ぐ（キー有無単独では分岐しない）。

### Sensitive Data 2-Layer Governance（蓄積型 AG の必須 2 層防御）

RCA 自由記述・監視生データ・機微運用情報を扱うため、**独立した 2 層**の防御を置く（片方は他方の代わりに
ならない。蓄積型エージェントの 2 層データ統治の原則に従う）。

| 層 | 守る対象 | 手段 |
|---|---|---|
| 層1: checkpoint 隔離 | LangGraph が DB に平文永続化する **State** | `payload_ref` 間接化。生 RCA / 監視本文は `src/services/` の store に置き State には参照ハンドルのみ。センシティブ実値の resolve は checkpoint されない **`src/api/server.py` の invoke 戻り値のみ**（`src/services/output_resolver.py`・入力を mutate せず copy） |
| 層2: 永続 KB / DB の統治 | AG 自身が所有・蓄積する `rca_evaluations` / KB テーブル | **書込前サニタイズ**（ingest も feedback も `sanitize_records` を通す）。機微運用情報（パスワード・認証情報・内部アドレス・監視原文）と禁止フィールドを落とし、保持するのは因果・程度・照合観点。**fail-closed**（サニタイズ失敗 = 1 件も書かない）+ アクセス制御 + 保持期間 + 訂正削除 + 監査 |

> **PII 検知は FW 標準 `detect_pii` のみに従う（自前の日本語補完サニタイザは持たない）。** SDK `pii_detector`
> は日本語氏名を検知しない既知の制約があるが、敬称/文脈辞書に
> よる補完は抜け漏れが多すぎて実効性が低く、保守負担と過剰マスクだけが残るため採用しない。
> 日本語対応の拡充はフレームワーク側の検知器に委ね、本テンプレートでは補わない。
> 生の担当者名等が層1（`payload_ref` 隔離）と allowlist スクラバで State/checkpoint に載らない防御は維持される。

#### 実装時の gate-bypass 先回り（SafetyPatrolRecordAccumulationWeatherMatchingAdvisoryAgent で実機再現・塞いだ穴。実装フェーズで必ず実装）

1. **`payload_ref` はサーバ発行の内部トークン**。`src/api/server.py` が生本文を store へ退避して発行する。
   **クライアント入力に含まれる同名 `payload_ref` は無条件に破棄**してから常にスクラブする（「既に
   `payload_ref` があればスクラブ省略」の早期 return を作らない — 偽装 ref で層1 が丸ごと迂回される）。
2. **checkpoint スクラバは allowlist**（`_CHECKPOINT_SAFE_KEYS` = ID・列挙値・日付・コードなど「Stage1/route が
   読む構造化型 かつ 人が文章を書けない型」だけ列挙）。それ以外の文字列値は中身を問わず transient store へ
   退避（再帰全走査・非 JSON も `__unparsable__` envelope で退避）。denylist（機微キー列挙）は必ず漏れる。
3. **S-3 allowlist の免除条件に `.*`（中身は何でもよい）を書かない**。見出し/ラベルの免除は「構造的にラベル
   **かつ 述語を持たない**」場合のみ（本文長上限 + 述語語尾〔です/ます/ません/不要/必要/省略…〕の有無で判定。
   長さだけで切らない）。`###` 接頭辞で provenance 検査を裏口から回避されるのを防ぐ。

### DB / KB スキーマ（design-entry blocker (a) 対応）

| テーブル / ストア | 用途 | 可視性 |
|---|---|---|
| `sla_deviations`（DB） | 監査用原記録（案件メタ・監視参照・RCA 参照ハンドル） | 非モデル可視（tenant key 必須） |
| `rca_evaluations`（DB） | 監査用原記録（過去の人間評価・差戻し理由・受入結論） | 非モデル可視。**受理結論ラベルは KB 射影から除外** |
| `evidence_registry`（DB） | evidence log / ticket / change の ID インデックス（Provenance 照合の正本源） | 非モデル可視 |
| KB document（vector store） | **retrieval-safe projection のみ**: 承認済み・source-linked な過去評価観点、監視メトリクス参照カテゴリ、非結論的観点、品質/provenance | モデル可視。**受理結論・結論含み自由記述は禁止** |
| `feedback_audit_events`（DB） | feedback 反映の監査イベント | 非モデル可視 |

### Composition Pattern

- **Pattern**: GraphNode (subgraph)
- **Composition target**: 内側 `DomainWorkflowGraph`（`src/graph/domain_workflow_graph.py`・`BaseGraph`）
- **Error propagation strategy**: propagate（内側エラーを `SubgraphError` として再送出 — fail fast）
- **上流 error passthrough**: `pre_process → main` は無条件辺のため、`SlaRcaGraphNode.execute()` を override して
  上流 `status="error"` を passthrough（`return {}`）してから内側 invoke を呼ぶ

### GraphNode 3 abstract + 内側受渡し（M-1 対応）

- `get_subgraph()` → compiled `DomainWorkflowGraph` を返す（`register_nodes` で `SlaRcaGraphNode(inner_config=...)`
  を明示注入。GraphNode は外側 self.config を自動参照しない）。
- `extract_input(state) -> str` → GraphNode は内側へ**文字列 + ctx のみ**渡すため、canonical envelope
  （`mode`, tenant-bound `case_ref`, `payload_ref`, 非機微メタ）を `json.dumps(..., <許可型のみ>)` で直列化。
  内側先頭ノードで parse。**生本文は入れない**（payload_ref 経由）。
- `merge_output(state, sub_result) -> dict` → §S-3 の経路 allowlist に従い変更キーのみ返す。
- **正常系は独自に inner `.invoke()` を再実装しない**。`SlaRcaGraphNode.execute()` は上流 error passthrough
  （`if state.get("status")==AgentStatus.ERROR.value: return {}`）の後、**`super().execute(state)` に委譲**する
  （context 伝播・`error_strategy` を壊さないため）。

### 経路 allowlist（罠1-4 対応・denylist 禁止）

`merge_output()` は**経路を決めるだけ**、S-3 ゲートは**payload を検証**、`post_process` は業務結果の
有無で fail-closed 分岐、と 2 層に分ける。`status` は 7 値（`pending/success/retry/error/timeout/awaiting_human/cancelled`）
あるため「error だけ弾く」denylist は 5 つ漏れる。**載せてよい `(status, mode)` の組だけを allowlist**（
`invoke_success` / `ingest_success` / `feedback_success`）で列挙し、未知・欠落は `no_business_result` に倒す
（既定経路に業務結果を通さない）。業務キー契約は**経路ごとに対称に閉じる**（`ALLOWED_BUSINESS_KEYS[kind]`
に無いキーが混入したら方向を問わず raise）。`post_process` は `gate_kind`（= merge_output が宣言）で分岐し、
業務結果が無ければ `formatted_output` を作らない。

### テナント fail-closed（H-4 対応・参照/書込/resolve 横断）

- **tenant の唯一の source は認証済み principal の claim**（`InvocationContext`）。リクエスト本文の `tenant_id`
  は無視するか、principal claim と不一致なら即 `status=ERROR`（fail-closed）。他テナントに絶対フォールバックしない。
- 全 DB / KB / store 操作を tenant 条件付きにする。`payload_ref` / `brief_ref` は **tenant・case・用途・
  有効期限に束縛**して発行し、resolve 時に principal と一致検証する。
- `feedback` は reviewer 権限 + 対象 draft の version/hash + 監査上の actor ID を必須にする（optimistic version check）。
- ingest 書込経路は read-only invoke より機微 → ingest ノードに最低 `VERIFIED_EXTERNAL`、invoke と同じ S-1
  サニタイズを ingest payload にも適用、全 ingest/feedback 操作を `emit_trace_event` で監査。

### server.py 応答の安全境界（H-3 対応）

`PostProcessNode` は ref しか見ないため実値に S-3 が効かない。よって resolve 後の HTTP response に**独立した
安全境界**を設ける（`src/api/server.py` + `src/services/output_resolver.py`）:
- ref の tenant/case/principal/purpose/expiry が一致することを検証してから、**copy にのみ** resolve。
- 最終 response を **allowlist response schema**（`facts` / `evidence_sources` / `unresolved_points` /
  `countervailing_observations` / `review_questions` のみを持つ）で検証してから返す。禁止語 blocklist ではなく
  「許可フィールドだけを持つ schema」で担保（State には Pydantic を置かず、HTTP 応答モデルのみで実施）。

### retry と書込副作用の衝突回避（M-4 対応）

backbone は最大 3 retry するため、ingest/feedback の書込に idempotency を持たせる:
- idempotency key = `tenant_id + evaluation_id + version/content_hash`。
- ingest/feedback は全行の検証・サニタイズ・正規化後、`ingest-row-pipeline` v0.4.0 の batch hook から
  repository の `write_batch` を 1 回だけ呼ぶ。repository transaction が失敗した場合は全行 rollback し、
  部分適用を認めない。
- **原子性の範囲は repository の DB transaction まで**。現実装は DB commit 後に KB projection を一括投入するため、
  DB + KB 全体は原子的ではない。KB 障害時まで一貫性を保証するには、同一 transaction、transactional outbox、
  または補償処理を別途実装する必要がある（本対応の範囲外）。
- feedback は optimistic version check。**S-3 違反・認可失敗は retry 不可**（`error` を retry に倒さない）。

### HITL 方針（M-3 対応）

`requires_human_review` は interrupt を起動しない（フラグに過ぎない）。本テンプレートは `/feedback` エンドポイントを
持つため、**実装フェーズでは `hitl.enabled: false` を既定とし、「review required は advisory フラグ、確定反映は
feedback endpoint」**の設計に寄せる方が安全（checkpointer 前提の interrupt 経路を減らせる）。HITL を有効化する
場合のみ、起動ノード・`propagate_hitl`・`memory_enabled: true`・`max_hitl`・`hitl_allowed` guard・
APPROVED/CORRECTED/REJECTED 全分岐・PB-7 を確定する。→ **設計フェーズの人間判断項目**。

### S-1 / S-4 のノード単位契約（M-2 対応）

- **全 `FunctionNode` サブクラス（`PreProcessNode` / `PostProcessNode`）は `required_trust_level` を class 本体に
  明示宣言**（暗黙 ANONYMOUS 継承は不可）。DB/KB/LLM を扱う `SlaRcaGraphNode` と内側ノードも同様。標準操作の
  最小は `VERIFIED_EXTERNAL`、ingest 書込経路も同以上。
- 各 `execute()` は最低 1 ドメインイベントを `emit_trace_event(type, payload, state)` で emit。**payload は
  ref・件数・hash のみ**（生 RCA・生成文を出さない）。provenance イベントは `state` を渡す（`result` ではない）。

### 出力契約（design-entry blocker (c) 対応）

出力は以下を**別フィールド固定**する:
- `facts`（事実） / `evidence_sources`（根拠ログ・チケットリンク） / `unresolved_points`（未回答論点） /
  `countervailing_observations`（反証観点） / `review_questions`（レビュー質問）

以下のフィールドは**出力スキーマに持たない**（S-3 で構造的に排除）:
- `rca_accepted` / `penalty_amount` / `sla_credit` / `contract_remedy`

## Import Isolation Confirmation
- [x] Template は agenticstar-platform SDK（Level 0）を import しない
- [x] Import 対象: `framework/` と `shared/` のみ（`agents/base/` は不要）

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph** | 固定パイプラインの蓄積型ワークフロー。自律 think-act ループ不要 |
| Composition pattern | Standalone FunctionNode | GraphNode + inner graph | **GraphNode + inner graph** | Stage1-4 の多段ドメインロジックと ingest/feedback 分岐を `main` 内に隠蔽。S-3 Provenance 検証を境界に置ける |
| S-3 Provenance 検証の位置 | PostProcessNode | GraphNode 境界 | **GraphNode 境界** | 正本（機微データ）を checkpoint に載せずに検証できる唯一の境界 |
| センシティブ値 resolve の位置 | post_process S-3 内 | server.py invoke 戻り値 | **server.py invoke 戻り値** | checkpointer が super-step 毎に State を永続化するため、グラフ内 resolve は必ず checkpoint 漏洩する |
| HITL | 無効 | 有効（低信頼時レビュー） | **設計論点（設計フェーズの判断項目）** | 契約レビュー前の人間確認は正常用法。要否と max_hitl を人間判断で確定 |

## Entry adapters

Two entry adapters share the same graph and the same security boundaries:
`src/api/server.py` (HTTP, all three modes, scope from `authenticate()`) and
`cli.py` (AGENTIC STAR Marketplace, `invoke` only, scope from the declared
`STG_DEFAULT_*` environment). Both build the same four-key canonical envelope
(`mode` / `tenant_id` / `case_ref` / `payload_ref`) and pass `{"scope": ...}` as
`input_context`; neither places raw business material in `user_input`. Node and
graph code is identical for both paths. The Marketplace write-mode scope note is
recorded in `docs/07_operation_guide.md`.

## Design-entry blocker チェックリスト（decision.md §7 / §12 対応）

> 表記方針: 設計方針が確定したものを 🟩、実装フェーズで詰める詳細スキーマを残すものを 🟨、
> PoC / 顧客窓口で確定する運用条件を ⬜ とする（「完了」の過剰主張をしない）。

- 🟨 (a) `sla_deviations` / `rca_evaluations` / `evidence_registry` / KB document schema — 責務分離（監査用原記録 vs
  retrieval-safe projection）と可視性方針は確定（§DB/KB スキーマ）。**実装フェーズ前に各ストアの主キー・tenant key・
  version・保持期限・削除連鎖（blob / vector index / キャッシュ）・暗号化境界・KB projection schema を確定する**。
- 🟨 (b) evidence/export schema の最小要件（時刻基準・単位・source ID・完全性 / 改竄検知）と RCA 主張との照合
  ロジック（Stage 1 / Stage 4）— 方針確定、最小 export schema は実装フェーズ前に確定。
- 🟩 (c) S-3 安全契約を schema・受入テストへ落とす（受理結論の自動継承禁止・context への受理判定ラベル混入禁止・
  `rca_accepted`/`penalty_amount` 等の構造排除・全論点 evidence link 必須・precedent の証拠化禁止）。
- ⬜ (d) 顧客の SLA 定義・監視値・RCA・チケット export の最小要件・データ品質・保持/削除/監査の統治契約 —
  **PoC / 顧客窓口で確定（Design 後の運用条件）**。未確定の間は該当連携を feature-disabled にする Go/No-Go 条件を持つ。

## 実装フェーズの前提条件（設計レビュー High-2 対応・2026-07-23）

以下は **実装フェーズで必ず実装し、実装なしにステージング環境へ進めない**（設計レビュー High-2）。
設計フェーズ時点の `src/api/server.py` は identity のみ実値化した design baseline で、生入力の直渡しが
残っている。実装フェーズで:
- **ingress 境界**: `server.py` がグラフ invoke の前に生 RCA / 監視 payload を **サーバ発行 `payload_ref`** へ隔離し
  （クライアント供給の `payload_ref` は破棄）、**非機微 canonical envelope のみ** `agent.invoke()` へ渡す。
- **response 境界**: グラフ完了後に `src/services/output_resolver.py` でセンシティブ実値を **HTTP 応答にのみ** resolve し、
  **allowlist response schema**（`facts` / `evidence_sources` / `unresolved_points` / `countervailing_observations` /
  `review_questions` のみ）で検証してから返す。
- 検証: 全 checkpoint 断面（outer / inner）に生 RCA・監視値・生成文が無いことを PB テストで assert。
- **ガード（設計フェーズで導入済み）**: `src/api/server.py` の `/invoke` は `INGRESS_ISOLATION_READY` が true でない限り
  503 で fail-closed（生入力の checkpoint 流入を機械的に阻止）。実装フェーズで isolation 実装 + PB checkpoint テスト緑後に true。
- **trust policy の実効境界（設計レビュー Medium）**: manifest は `VERIFIED_EXTERNAL` を要求。全ノードの
  `required_trust_level` を明示宣言し、`deploy/local-stg.yml` の `INVOKE_AUTH_TOKEN` を必須化（token 空起動を塞ぐ）。
  ingress ガードと合わせて実装フェーズで確定・テストする。

## 適用部品指定表（実装フェーズの写経方式・共通部品ライブラリ）

実装フェーズでは以下9部品を `apply_part.py` で写経適用し、adaptation_points のみドメインに合わせる（骨格無改変）。
2026-08-13 Batch 2 で表記版へ refresh 済み。

| 部品 ID | 版 | 役割 | 本テンプレでの adaptation |
|---|---:|---|---|
| `cat2-graph-skeleton` | 0.1.4 | Cat2 外側 GraphNode 骨格（mode_specs / merge_output / 経路 allowlist） | `mode_specs` = invoke/ingest/feedback の gate_kind・field map。`_parent_config` に llm/store/retrieval/top_k。`propagate_hitl=False`（HITL 無効）。request 単位で構築し、`extract_input` は canonical envelope 直列化 |
| `entry-adapter` | 0.4.0 | server.py の scope 認証・trust 昇格・operation policy | `authenticate(request, operation=...)`、scope は `tenant_id` / `case_ref`。read token と middleware write を分離 |
| `payload-store` | 0.4.0 | 生 RCA/監視 payload の scope 束縛 opaque-ref（層1・High-2 ingress） | `scope_keys=("tenant_id", "case_ref")`、TTL 設定。unscoped `get()` は持たず、参照形式は部品 helper で検証 |
| `output-envelope` | 0.1.2 | resolve-after-graph 応答境界（High-2）・経路別出力形・fail-open 防止 | `format_invoke/ingest/feedback`・`required_fields_by_mode`・`gate_kind_by_mode`。resolve は server.py 側 |
| `evidence-gate` | 0.3.0 | S-3 Provenance + LLM finding 型付き射影 | §S-3 の照合に加え `FindingField` から prompt contract を生成し、宣言外/型違い/enum 外を拒否 |
| `scoped-retrieval` | 0.3.0 | テナント scoped KB 検索（Stage2・retrieval-safe projection） | backend は全 scope を server-side 条件に使い、各 hit に**保存レコード由来** scope を返す。SQLite adapter でも実証 |
| `ingest-row-pipeline` | 0.4.0 | checkpoint スクラバ allowlist + scope-bound batch write | `AuthContext.scope` / server session を constructor 注入し、認証後に request-scoped graph へ配線。全 scalar scrub と typed preserve を維持し、全行を repository `write_batch` へ 1 回で渡す。KB projection は DB commit 後のため DB transaction の原子性範囲外 |
| `ingest-file-sanitizer` | 0.1.1 | ingest ファイルの形式ゲート・品質ゲート | `allowed_channels=("csv",)`・`max_file_bytes`・manifest 品質閾値。DOCX adversarial tests は部品層で維持 |
| `llm-draft-stage` | 0.3.0 | Stage3 LLM draft（注入シーム・fallback・evidence 生成） | LLM 未設定のみ fallback。provider/parse 障害は stable `error_code` の error、例外本文は state/trace に載せない |

> 部品同梱テスト（層1）は `tests/parts/{part-id}/` に入り削除・弱体化禁止（緑維持のみ）。接合部テスト（層2）は
> `parts/TESTING.md` の該当行を docs/03_test_spec.md へ引き写して具体化する。写経忠実性は `verify_part.py` で機械照合。

## 受入テスト条件（S-3 契約の検証可能条件・M-5 対応。`docs/03_test_spec.md` へ落とす）

以下を最低限の受入条件として列挙する（各々テストケース化）:
- 未知 ID の引用 → S-3 raise（ID hallucination）
- 引用なしの invoke 出力 / claim ごとの根拠欠落 → fail-closed または `requires_human_review`
- precedent case を現案件の evidence として引用 → raise（過去事例の証拠化禁止）
- 禁止フィールド（`rca_accepted` 等）のネスト混入 → raise
- outer / inner **両方の checkpoint 断面**に生 RCA・監視値・生成文・正本キー（`retrieved_records` 等の
  evidence-gate contract キー）が存在しない（`EvidenceGateNode.execute()` の contract キー射影で除去される）
- 別 tenant の ref を resolve しようとする → fail-closed
- sanitize 1 件失敗時に全件 rollback（部分書込しない）
- retry 時の重複書込なし（idempotency key で冪等）

---

*Design spec for SVC-C2-098. 実装契約の一次情報は AgentCore SDK のドキュメントを参照。
本設計は敵対的コードレビュー（2026-07-23・High 5 / Medium 5）を反映済み。*
