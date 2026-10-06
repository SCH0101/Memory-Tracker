# ES RAG integration

## Current integration — 2026-10-02

This section supersedes the historical contract description below. Current ES
has retired vector queries, chunks, source/chunk inventory and external ingestion.
Do not use those endpoints as fallbacks or interpret their failure as empty data.

Memory Tracker's gateway now recognizes `equity_swarm_external_capabilities_v1`.
For that contract, Ask uses `POST /api/external/answer` (JSON), not the old SSE
payload. It sends `question`, `filters`, `allow_web:false`, `include_charts:false`.
Session/project/context fields remain local and are not sent. The current answer
endpoint is stateless; follow-up conversation memory is not provided by this
migration. Legacy dated-contract servers retain the old transport for compatibility.

### Enforced AI scope

Every current Ask and `search_research_documents` request includes
`filters.themes:["ai"]`. AI is ES's broad industry universe: memory and other
semiconductors, datacenter, hardware, internet/cloud, networking and power
infrastructure. It includes SK hynix, Samsung, NVIDIA, Meta and Google by their
stored classifications. It does not mean every document explicitly discusses AI.

Use `gateway.research_filters()` to inspect ES taxonomy and
`gateway.search_research_documents(filters=..., limit=20, offset=0)` for discovery.
Pass returned `next_offset` and `inventory_revision` with identical filters to
continue. Result `industries` and `sectors` fields support industry grouping.
Supported dimensions include publication `date_from/date_to`, full Bloomberg
`tickers`, `industries`, `sectors`, canonical `brokers`, `document_types`,
`file_formats` and exact stored `source_types`. Different dimensions intersect;
values within a list are alternatives. The gateway checks ES's `applied_filters`
before accepting results. Use canonical filter values from ES; unknown fields
and conflicting inputs fail, rather than silently widening retrieval.

Legacy `year` converts to inclusive calendar publication bounds. Selected broker
research converts to initiation/earnings preview/review/company update/primer/
conference-note document types. Earnings transcripts map to
`earnings_call_transcript` and `transcript`. These are document categories, not
proof of publisher identity; use a broker filter when publisher scope matters.
Tables-only and user-input-only selection are not supported by the current
document filter contract and raise an explicit error. Selecting all source
categories adds no document-type restriction. There is no arbitrary `$or` or
retired chunk-metadata translation. The question limit is 12,000 characters.

### Credentials and deployment verification

Continue configuring `ESRAG_API_URL` and `ES_RAG_API_KEY` in this app's environment.
Requests send Bearer authentication (plus the historical header for compatibility).
The individual tracker key needs `external:answer` (or compatible `rag:ask`) for
answers and `research:read` for document discovery/shared research access.
Never borrow another app's key, log credentials, or grant scopes automatically.

Verified on 2026-10-02: the tracker credential was absent from ES's external key
registry. With user approval, its existing key digest was registered as
`memory-tracker-answer-reader` with only `external:answer` and `research:read`.
The secret was not changed or copied from another application. Capabilities and
AI/memory/date-filtered discovery now pass. A real call through the tracker
gateway returned ES's final answer with original research-page citations.
The tracker backend was restarted independently; ES and ngrok were not restarted.
The live browser-facing `/api/chat` check also returned HTTP 200 with a cited
answer. `ESRAG_PUBLIC_URL` sets the public ES origin for citation links, while
`ESRAG_API_URL` remains the backend transport address. Private source links still
require ES reader sign-in; no credential is embedded in URLs or browser replies.

The Ask, Overview and new discovery paths are migrated. Dashboard services that
still call legacy `query`, `sources`, `chunks` or ingestion are **not migrated**;
the gateway rejects those operations on the current ES contract instead of
pretending they remain available. This is not a claim that every tracker feature
works with current ES.

### Overview direct-answer migration

Both `/api/overview/industry` and `/api/overview/company` now request ES's final
Markdown answer through the same authenticated gateway. They enforce the broad
AI theme, inclusive publication bounds from today minus 20 days through today,
and supported Overview source-type selections. No local chunk retrieval or
second answering model is invoked. Public source links require ES reader access.
The existing Industry/Company tabs render ES headings, bullets and citations
using the same safe Markdown component as Ask. Legacy chart-reference cards are
not fabricated from an answer. Industry headings cover supply/demand, pricing,
HBM, inventory, capex and risks; Company headings cover memory suppliers and
AI/cloud demand context. Missing evidence is requested explicitly.

Caches use `es_direct_answer_v1` plus the publication-window, source-selection
and prompt fingerprint. Old generated cards remain stored but are not served
as current answers. Refresh regenerates; empty/failed answers are not cached.
The top-level Industry/Company prompt overrides remain additional instructions
(combined ES question limit 12,000 characters). Historical per-section chunk
query/reranking settings are no longer used by this direct-answer workflow.
Dashboard is not migrated by this change. No credentials/scopes were changed.

Focused regression command (from this directory):

```powershell
python -m pytest tests/test_es_current_contract.py tests/test_es_ask_client.py tests/test_es_rag_gateway.py -q -p no:cacheprovider
```

## Historical integration (retired ES contract only)

The remaining sections describe the old ES deployment, not current capabilities.

Memory Tracker treats ES RAG as an external authenticated service. Application
code must use `backend.services.es_rag_gateway`; it must not import ES source
modules, alter `sys.path`, read the ES repository's files, or connect directly
to its vector database.

## Configuration

Set these values in Memory Tracker's own environment:

```dotenv
ESRAG_API_URL=http://localhost:8080
ES_RAG_API_KEY=<shared ingest key>
ESRAG_TIMEOUT_SEC=180
ESRAG_CAPABILITY_TTL_SEC=300
```

`ESRAG_API_URL` and `ES_RAG_API_KEY` are required. The key is sent only in the
`X-RAG-Ingest-Key` header and must never be logged or returned to the browser.
The gateway requires ES contract version `2026-09-02` or newer.

Optional local source-document roots belong in Memory Tracker's environment:

```dotenv
SELLSIDE_LOCAL_MIRROR=C:\path\to\research
SHAREPOINT_LOCAL_ROOT=C:\path\to\JCAPSharePoint - Documents
```

These roots are used only to open a cited PDF locally. They are not an ES data
transport or fallback.

## API ownership

The gateway owns authentication, contract checks, timeouts, request encoding,
response validation, and error translation for:

- capabilities, health, query, sources, and chunks;
- the read-only `/api/charts/browse` Gallery catalog and chart assets;
- PDF, extracted text, Excel, chart-caption, chart-image, and pasted-note writes;
- ingestion job polling and Ask streaming.

Routes and domain services own Memory-specific policy such as
`memory_semiconductors` tagging, broker normalization, gallery grouping, and
the browser-safe chart proxy URL. Gallery parent sectors and niches come from
ES's current `industry`, `industry_label`, and `niche` fields. Memory Tracker
does not hard-code the ES sector list.

The Ask panel sends the latest user question to the answer-ready
`POST /api/rag/ask` SSE endpoint with a stable session ID. It renders the
terminal `done` answer and treats `error` or a stream without `done` as a
failure; Memory Tracker does not run a second answering model.

## Failure behavior

There is no direct-import or direct-vector-store fallback. Configuration,
transport, and contract failures derive from `EsRagError`. Unhandled gateway
failures become a safe HTTP 503 response:

```json
{"detail":"ES RAG service unavailable","operation":"query"}
```

The response intentionally excludes the API key, upstream response bodies, and
local filesystem details. A failed retrieval is not converted into an empty
result because that could make stale or missing evidence look authoritative.
Write responses must explicitly confirm success (or a valid deduplication
skip); malformed HTTP-200 responses are treated as contract failures.

## Cache behavior

- Capabilities are cached per process for `ESRAG_CAPABILITY_TTL_SEC`.
- Chart inventory uses stale-while-revalidate caching; the first load waits for
  ES, while later stale loads return cached rows and refresh in the background.
- `/api/charts/by-category` sets `X-ES-RAG-Stale: true` whenever it serves a
  cached remote inventory whose TTL has expired.
- A manual gallery refresh bypasses both the server inventory cache and browser
  cache.
- Corpus health is cached for 10 seconds.
- Source and chunk inventories follow `next_offset` until exhausted and reject
  repeated offsets instead of looping indefinitely.

## Diagnostics

Do not put the key directly in shell history. With the service environment
already loaded, verify the API through Memory Tracker or use a short local
script that reads `ES_RAG_API_KEY` from the environment.

Expected checks:

1. `GET /api/rag/capabilities` reports contract `2026-09-02` or newer.
2. `GET /api/rag/health?detail=true` reports healthy service dependencies.
3. `POST /api/rag/query` returns a JSON object with a `hits` list.
4. `GET /api/charts/browse?group=industry&limit=1` returns current Gallery
   sectors and niches; Memory Tracker follows category pages until the reported
   group count is exhausted.
5. `POST /api/rag/ask` completes with a terminal SSE `done` event; chart bytes
   are served to the browser only through `/api/charts/rag-asset/{path}`.

Run backend verification from the project root:

```powershell
python -m unittest discover -s tests -v
```

Build the frontend directly from `frontend` when the organization name's
ampersand confuses the npm command shim:

```powershell
Set-Location .\frontend
node .\node_modules\typescript\bin\tsc -b
node .\node_modules\vite\bin\vite.js build
```
# Gallery AI scope

Gallery sends `theme=ai` to ES `/api/charts/browse` for the initial catalogue
and every category page. ES filters before group counts and pagination, using
its shared AI industry universe and Gallery taxonomy. Unrelated and unclassified
charts are excluded; the main ES Gallery remains unfiltered unless requested.
Restart the Tracker backend after deployment to clear the old in-memory inventory.
