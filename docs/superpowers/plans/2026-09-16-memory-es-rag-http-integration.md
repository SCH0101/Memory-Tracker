# Memory Tracker ES RAG HTTP Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace every active Memory Tracker dependency on ES source files or Qdrant with one validated HTTP gateway using the latest external ES RAG API.

**Architecture:** `EsRagGateway` is the sole transport boundary and owns configuration, authentication, HTTP encoding, response validation, capabilities, and ES errors. Memory-specific evidence/chart/dashboard normalization stays in Memory services, which call only public gateway methods.

**Tech Stack:** Python 3, FastAPI, stdlib `urllib`, `unittest`, local in-process HTTP test servers, React/Vite verification.

**Spec:** `docs/superpowers/specs/2026-09-16-memory-es-rag-http-integration-design.md`

## Global Constraints

- Use `http://localhost:8080` for same-machine production deployment.
- Require ES external API contract `2026-09-02` or newer.
- Preserve existing Memory Tracker functionality and local cached gallery data.
- Do not add a watchdog or automatic ES process management.
- Do not import ES source modules or access Qdrant directly.
- Do not log or document secret values.
- The project is not a Git repository, so commit steps are replaced by fresh test and source-diff checkpoints.

---

## File Structure

- Create `backend/services/es_rag_gateway.py`: configuration, authenticated HTTP, multipart/JSON/SSE parsing, capability compatibility, and all ES operations.
- Create `tests/test_es_rag_gateway.py`: transport and contract tests using a real local HTTP server.
- Modify `backend/services/fact_pool.py`: retain Memory policies/normalization; replace transport and direct ES/Qdrant code with gateway calls.
- Modify `backend/services/dashboard_refresh.py`: source, chunk, and user-input reads through the gateway only.
- Modify `backend/api/routes/charts.py`: unified gateway for inventory/assets and removal of ES-local path access.
- Modify `backend/api/routes/chat_bridge.py`: unified gateway for Ask SSE.
- Modify `backend/api/routes/upload.py`, `inject.py`, `paste.py`, and `excel.py`: public gateway write methods and explicit service errors.
- Modify `backend/api/routes/corpus_stats.py`: external health contract instead of Qdrant count.
- Modify `backend/api/server.py`: one exception handler mapping gateway failures to safe `503` responses.
- Modify `backend/paths.py` and `backend/services/datapoints.py`: remove legacy ES repository paths; keep local fact-export audit data under Memory runtime data.
- Remove `backend/services/es_chart_client.py` and `backend/services/es_ask_client.py` after callers migrate.
- Modify `tests/test_es_chart_gallery.py` and `tests/test_es_ask_client.py`: target the unified gateway and route behaviour.
- Create `tests/test_es_rag_integration.py`: Memory evidence/corpus/dashboard boundary regression tests.
- Create `docs/es-rag-integration.md`: operator and developer documentation.

### Task 1: Unified ES RAG Gateway

**Files:**
- Create: `backend/services/es_rag_gateway.py`
- Create: `tests/test_es_rag_gateway.py`

**Interfaces:**
- Produces: `EsRagGateway.from_environment() -> EsRagGateway`
- Produces: `get_es_rag_gateway() -> EsRagGateway`
- Produces: `EsRagError`, `EsRagConfigurationError`, `EsRagTransportError`, `EsRagContractError`
- Produces public methods: `capabilities`, `health`, `query`, `sources`, `chunks`, `chart_inventory`, `chart_asset`, `ingest_pdf`, `index_text`, `index_excel`, `index_chart_captions`, `inject_chart`, `inject_fact`, `get_job`, `list_jobs`, `wait_for_job`, and `ask`.

- [ ] **Step 1: Write failing configuration, authentication, and capability tests**

```python
def test_query_sends_auth_and_latest_payload(self):
    gateway = EsRagGateway(self.base_url, "secret", capability_ttl_seconds=60)
    result = gateway.query(
        "HBM supply",
        source_filter="sellside",
        n_results=7,
        metadata_filters={"broker": "MS"},
    )
    self.assertEqual("secret", self.handler.request_headers["X-RAG-Ingest-Key"])
    self.assertEqual("/api/rag/query", self.handler.request_path)
    self.assertEqual("sellside", self.handler.request_json["source_filter"])
    self.assertEqual({"broker": "MS"}, self.handler.request_json["metadata_filters"])
    self.assertEqual([{"text": "HBM supply"}], result["hits"])

def test_rejects_older_contract(self):
    self.handler.capabilities = {"contract_version": "2026-08-31"}
    with self.assertRaisesRegex(EsRagContractError, "2026-09-02"):
        EsRagGateway(self.base_url, "secret").capabilities()
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `python -m unittest tests.test_es_rag_gateway -v`

Expected: import failure because `backend.services.es_rag_gateway` does not exist.

- [ ] **Step 3: Implement configuration, JSON requests, errors, and capability TTL**

Implement environment validation for `ESRAG_API_URL`, `ES_RAG_API_KEY`, positive timeout values, `X-RAG-Ingest-Key`, redacted errors, `MIN_CONTRACT_VERSION = "2026-09-02"`, and a per-instance capability cache keyed by monotonic expiry.

- [ ] **Step 4: Add failing multipart, chart-asset, jobs, sources/chunks, and SSE tests**

Each test server response must mirror the full external response used by the caller. Assert consumer-visible results and the request contract, including multipart field names and safe chart paths.

- [ ] **Step 5: Implement the remaining gateway methods minimally**

Use stdlib multipart encoding so no new runtime dependency is needed. `chart_inventory()` must send:

```python
{
    "query": "all charts",
    "source_filter": "charts",
    "n_results": 0,
    "metadata_filters": {"media_type": "chart"},
    "theme": "ai",
}
```

`ask()` must require a terminal SSE `done` event and raise `EsRagContractError` if the stream ends without one.

- [ ] **Step 6: Run gateway tests and record the checkpoint**

Run: `python -m unittest tests.test_es_rag_gateway -v`

Expected: all gateway tests pass with zero warnings or leaked secrets.

### Task 2: Evidence, Charts, Ask, and Corpus Reads

**Files:**
- Modify: `backend/services/fact_pool.py`
- Modify: `backend/api/routes/charts.py`
- Modify: `backend/api/routes/chat_bridge.py`
- Modify: `backend/api/routes/corpus_stats.py`
- Modify: `backend/api/server.py`
- Modify: `tests/test_es_chart_gallery.py`
- Modify: `tests/test_es_ask_client.py`
- Create: `tests/test_es_rag_integration.py`

**Interfaces:**
- Consumes: `get_es_rag_gateway()` and gateway read methods from Task 1.
- Produces: unchanged `retrieve_for_section(...) -> list[dict]` and `retrieve_rag_charts(...) -> list[dict]` domain interfaces.
- Produces: global FastAPI handling of `EsRagError` as a redacted JSON `503`.

- [ ] **Step 1: Write failing evidence and corpus route tests**

```python
def test_corpus_stats_uses_external_health(self):
    with patch("backend.api.routes.corpus_stats.get_es_rag_gateway") as factory:
        factory.return_value.health.return_value = {
            "status": "ok",
            "collection": "equity_research_openai",
            "qdrant_alive": True,
            "qdrant_collection_healthy": True,
        }
        response = TestClient(app).get("/api/corpus-stats")
    self.assertEqual(200, response.status_code)
    self.assertTrue(response.json()["ok"])
    self.assertNotIn("points", response.json())

def test_es_failure_is_not_reported_as_empty_evidence(self):
    with patch("backend.services.fact_pool.get_es_rag_gateway") as factory:
        factory.return_value.query.side_effect = EsRagTransportError("ES unavailable")
        response = TestClient(app).get("/api/overview/industry")
    self.assertEqual(503, response.status_code)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python -m unittest tests.test_es_rag_integration -v`

Expected: failures show direct Qdrant corpus access or swallowed transport errors.

- [ ] **Step 3: Refactor `fact_pool.py` reads to the gateway**

Keep source-option mapping and `_to_row`. Delete `_ensure_swarm_path`, `_qdrant_alive`, environment mirroring, query-activity imports, direct vector-store retrieval, Qdrant chart scrolling, and local chart resolution. Send supported `source_filter`, `metadata_filters`, `theme`, and `rerank_candidates` through `query`.

- [ ] **Step 4: Migrate gallery inventory, semantic charts, assets, and Ask**

Use `gateway.chart_inventory`, `gateway.query(source_filter="charts")`, `gateway.chart_asset`, and `gateway.ask`. Convert ES `/api/analysis/chart-asset/<relpath>` URLs into Memory `/api/charts/rag-asset/<relpath>` URLs without filesystem inspection.

- [ ] **Step 5: Replace corpus Qdrant introspection and add the global error handler**

Return external collection/queue/health fields plus `last_index_ts`. Map gateway errors to:

```json
{"detail":"ES RAG service unavailable","operation":"query"}
```

The log may retain the internal reason; the response and log must not contain the API key.

- [ ] **Step 6: Run read-path tests**

Run: `python -m unittest tests.test_es_rag_integration tests.test_es_chart_gallery tests.test_es_ask_client -v`

Expected: all tests pass and the gallery cache still reuses inventory.

### Task 3: External Write and Job Paths

**Files:**
- Modify: `backend/api/routes/upload.py`
- Modify: `backend/api/routes/inject.py`
- Modify: `backend/api/routes/paste.py`
- Modify: `backend/api/routes/excel.py`
- Modify: `backend/services/fact_pool.py`
- Modify: `backend/paths.py`
- Modify: `backend/services/datapoints.py`
- Modify: `tests/test_es_rag_integration.py`

**Interfaces:**
- Consumes gateway write methods from Task 1.
- Produces the existing Memory upload/job/paste/Excel route response shapes.

- [ ] **Step 1: Write failing route tests for queued PDF, pasted fact/chart, and Excel indexing**

Assert that each route calls the public gateway interface, maps a returned ES job id to `esrag:<id>`, and returns `503` for an unavailable ES service. Assert no direct fallback is attempted.

- [ ] **Step 2: Run the write-path tests and verify RED**

Run: `python -m unittest tests.test_es_rag_integration -v`

Expected: failures identify private `_esrag_client` or direct-import fallback paths.

- [ ] **Step 3: Migrate all writes to public gateway methods**

Remove `ping()` preconditions and direct fallbacks. A successful write bumps `last_index_ts`; a skipped write does not. Preserve queued upload and job polling behaviour.

- [ ] **Step 4: Remove ES-owned local cache writes**

Delete Memory writes to ES chart cache directories. Pasted charts use the ES-returned `result.image_url`/`chart_file` and the Memory chart proxy. Move datapoint JSON audit exports to `DATA_DIR / "fact_exports"`.

- [ ] **Step 5: Run write-path and existing workbook tests**

Run: `python -m unittest tests.test_es_rag_integration tests.test_workbook_model tests.test_openai_chart_categorization -v`

Expected: all tests pass; local workbook behaviour remains unchanged.

### Task 4: Dashboard Sources, Chunks, and User Inputs

**Files:**
- Modify: `backend/services/dashboard_refresh.py`
- Modify: `tests/test_es_rag_integration.py`

**Interfaces:**
- Consumes: `gateway.sources(...) -> dict` and `gateway.chunks(filename=..., ...) -> dict`.
- Produces unchanged `DocumentRef` discovery and normalized dashboard chunk rows.

- [ ] **Step 1: Write failing dashboard discovery tests**

Use a fake gateway response with complete source and chunk rows. Verify broker aliases, freshness filtering, source-family filtering, pagination through `next_offset`, and user-input chunk normalization.

- [ ] **Step 2: Run dashboard tests and verify RED**

Run: `python -m unittest tests.test_es_rag_integration.DashboardHttpOnlyTests -v`

Expected: tests fail because direct scroll fallback remains.

- [ ] **Step 3: Replace dashboard-specific HTTP and Qdrant helpers**

Delete `_esrag_get_json`, `_scroll_payloads_for_manifest`, `_scroll_chunks_for_filename`, and all `_ensure_swarm_path`, `qdrant_client`, or `tools.qdrant_store` usage. If `/sources` or `/chunks` fails, propagate the gateway error; do not reinterpret it as an empty corpus.

- [ ] **Step 4: Load user inputs through sources and chunks**

Call `sources(doc_type="user_input", theme="ai", limit=200)` and page through `chunks` for each returned exact filename. Retain the existing 15-minute in-process cache and Memory-sector filter.

- [ ] **Step 5: Run dashboard and full backend tests**

Run: `python -m unittest discover -s tests -v`

Expected: all backend tests pass.

### Task 5: Remove Legacy Modules, Document, and Verify Live

**Files:**
- Remove: `backend/services/es_chart_client.py`
- Remove: `backend/services/es_ask_client.py`
- Create: `docs/es-rag-integration.md`
- Modify: `backend/.env.example` if present; otherwise document variables without creating a secret-bearing file.

**Interfaces:**
- Consumes all completed tasks.
- Produces operator documentation and a source tree with no active legacy coupling.

- [ ] **Step 1: Remove obsolete clients after import scan is clean**

Run before deletion:

`rg -n "es_chart_client|es_ask_client" backend tests -g "*.py"`

Expected: no production import remains.

- [ ] **Step 2: Write integration documentation**

Document environment variables, minimum contract, endpoint mapping, timeout controls, error meanings, cache behaviour, and commands for capabilities, health, evidence query, chart inventory, and Memory route verification. Use placeholders for keys.

- [ ] **Step 3: Run legacy dependency scan**

Run:

`rg -n "equity_swarm_V5|qdrant_client|tools\\.(qdrant_store|vector_store|sellside_research|query_activity)|from ESRAG|import ESRAG|sys\\.path" backend -g "*.py"`

Expected: no matches in active backend code.

- [ ] **Step 4: Run the complete automated verification**

Run:

- `python -m unittest discover -s tests -v`
- `npm run build` from `frontend`

Expected: both commands exit zero.

- [ ] **Step 5: Run live ES contract smoke checks**

With the Memory `.env` loaded without printing secrets, verify:

- capabilities returns contract `2026-09-02` or newer;
- health is HTTP 200 and collection healthy;
- a Memory evidence query returns a valid `hits` array;
- unranked AI chart inventory returns `inventory: true`;
- `/api/corpus-stats`, `/api/overview/industry`, and `/api/charts/by-category` return successful responses from the rehosted Memory service.

- [ ] **Step 6: Browser verification**

Open Memory Tracker and verify Overview visibly distinguishes empty evidence from failure, Gallery loads from cache immediately when populated, forced gallery refresh reaches ES, and chart assets render through the Memory proxy.

- [ ] **Step 7: Final requirement audit**

Re-read the design spec, compare each requirement to the final source and verification output, and report any unimplemented requirement instead of claiming completion.
