# Memory Tracker ES RAG HTTP Integration Design

**Date:** 2026-09-16

**Status:** Approved

## Objective

Replace Memory Tracker's mixed direct-import, direct-Qdrant, and HTTP integration with one HTTP-only boundary against the latest Equity Swarm external RAG API. The production endpoint remains `http://localhost:8080`, authenticated with `X-RAG-Ingest-Key` using `ES_RAG_API_KEY`.

The integration must support ES contract version `2026-09-02` or newer and cover Overview, Model, Dashboard, gallery/chart retrieval, chart assets, uploads, Excel indexing, user facts, chart injection, jobs, sources, chunks, and corpus health.

## Architecture

Memory Tracker will depend on a single `EsRagGateway` deep module. The gateway owns:

- environment configuration and validation;
- authentication headers;
- request construction and multipart encoding;
- JSON and SSE response parsing;
- timeouts and transport errors;
- ES capability discovery and contract-version validation;
- query, ingestion, job, health, source, chunk, chart inventory, and chart-asset operations.

Application routes and services will not import code from the ES repository. They will not alter `sys.path`, mirror ES environment files, call Qdrant directly, or resolve ES-owned files from local filesystem paths.

Memory-specific evidence selection and response normalization remain in Memory Tracker. Transport and external contract concerns remain inside the gateway.

```text
Memory routes and domain services
              |
              v
         EsRagGateway
  config/auth/contract/transport
              |
              v
    ES external HTTP API :8080
```

## External Contract

The minimum supported ES contract is `2026-09-02`.

Required endpoints are:

- `GET /api/rag/capabilities`
- `GET /api/rag/health`
- `POST /api/rag/query`
- `GET /api/rag/sources`
- `GET /api/rag/chunks`
- `POST /api/rag/ingest-pdf`
- `POST /api/rag/index-text`
- `POST /api/rag/index-excel`
- `POST /api/rag/index-chart-captions`
- `POST /api/rag/inject-chart`
- `POST /api/rag/inject-fact`
- `GET /api/rag/jobs`
- `GET /api/rag/jobs/{job_id}`
- `POST /api/rag/ask`
- `GET /api/analysis/chart-asset/{relpath}`

The gateway sends `X-RAG-Ingest-Key` on every request. Chart inventory uses the explicit unranked chart contract: `POST /api/rag/query` with a generic chart query, `n_results=0`, chart metadata filtering, and an optional theme. Ranked chart retrieval uses the normal query contract with `source_filter=charts`.

## Runtime Behaviour

The gateway is constructed from:

```env
ESRAG_API_URL=http://localhost:8080
ES_RAG_API_KEY=...
```

The first capability check is cached for a short TTL so normal navigation does not repeatedly rediscover the contract. Configuration errors, authentication errors, transport failures, incompatible contracts, invalid responses, and empty valid results are distinct states.

- Missing URL or key is a configuration error.
- An unavailable or timed-out ES API becomes a service-unavailable error.
- A contract older than `2026-09-02` is rejected with both required and detected versions.
- Malformed successful responses are contract errors.
- A valid response with zero hits remains an ordinary empty result.

Logs include the operation and status but never the API key. No watchdog, public tunnel, or process manager is added.

## Feature Mapping

- Overview, Model, and metric-first Dashboard retrieval use `query`.
- Document-first Dashboard discovery uses `sources`; deterministic document loading uses `chunks`.
- Dashboard user-input fallback discovers user-input sources and loads their chunks through the API.
- Gallery inventory and semantic chart search use `query`.
- Gallery images are proxied by Memory Tracker while ES serves the bytes from its chart-asset endpoint.
- PDF uploads use queued ES ingestion and the ES job endpoints.
- Pre-extracted text, Excel workbooks, caption batches, pasted charts, and pasted facts use their corresponding external write endpoints.
- Corpus status uses `health` and process-local last-write time. It does not expose a fabricated direct-Qdrant point count.

## Code Structure

- `backend/services/es_rag_gateway.py`: the only ES HTTP transport and contract module.
- `backend/services/fact_pool.py`: Memory evidence normalization and source-selection policy using the gateway.
- `backend/services/dashboard_refresh.py`: dashboard domain logic using gateway source/chunk methods only.
- API routes: consume public gateway or fact-pool interfaces; no private transport helpers.
- `es_ask_client.py` and `es_chart_client.py`: removed after their consumers use the gateway.

The public Memory route payloads remain compatible wherever possible. Where the old implementation silently returned empty data on connectivity failure, routes must surface a clear `503` or an explicit `ok: false` status according to their existing response style.

## Caching

Only stable or expensive read operations are cached:

- capabilities: short TTL;
- gallery inventory: retain the existing Memory cache;
- dashboard user-input materialization: retain the existing bounded TTL.

Errors are not cached as successful empty results. Existing gallery data in Memory's local database remains available. If cached remote gallery content is shown while ES is unavailable, the response must identify it as stale rather than fresh.

## Testing

Implementation follows red-green-refactor.

- Gateway tests use a local in-process HTTP server and assert real request paths, headers, bodies, multipart fields, response parsing, error mapping, and contract validation.
- Domain tests verify hit normalization, source filtering, chart mapping, and that empty results differ from failures.
- Route tests verify corpus status and chart-asset proxy behaviour.
- Dashboard tests verify source/chunk discovery uses HTTP and never falls back to direct Qdrant access.
- A live contract smoke test checks capabilities, health, one evidence query, and chart inventory against the running ES service.
- Browser verification checks Overview and gallery loading.
- A source scan confirms no remaining `equity_swarm_V5`, ES `tools.*`, Qdrant client, or ES repository path dependency in active Memory Tracker code.

## Documentation

`docs/es-rag-integration.md` will document ownership boundaries, environment variables, minimum contract version, endpoint mapping, timeouts, failure behaviour, troubleshooting, and live verification commands.

## Constraints

- Use `http://localhost:8080` for same-machine production deployment.
- Require ES external API contract `2026-09-02` or newer.
- Preserve existing Memory Tracker functionality and local cached gallery data.
- Do not add a watchdog or automatic ES process management.
- Do not import ES source modules or access Qdrant directly.
- Do not log or document secret values.
