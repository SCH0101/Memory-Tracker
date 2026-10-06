"""Authenticated HTTP boundary to the Equity Swarm external RAG API.

No Memory Tracker module should import ES source code or access its vector
store directly.  This gateway owns the external contract, transport, and
error vocabulary while callers retain Memory-specific domain policy.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


MIN_CONTRACT_VERSION = "2026-09-02"
DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_CAPABILITY_TTL_SECONDS = 300.0
GALLERY_PAGE_SIZE = 500
_VALID_SOURCE_FILTERS = {
    "all", "sellside", "transcripts", "expert_calls", "tables", "charts",
}
_VALID_JOB_STATUSES = {"pending", "queued", "running", "done", "failed", "error", "skipped"}


class EsRagError(RuntimeError):
    """Base class for safe, operation-aware ES RAG failures."""

    def __init__(
        self,
        message: str,
        *,
        operation: str = "request",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.status_code = status_code


class EsRagConfigurationError(EsRagError):
    """Memory Tracker is missing or has invalid ES configuration."""


class EsRagTransportError(EsRagError):
    """The ES service could not complete an HTTP operation."""


class EsRagContractError(EsRagError):
    """ES returned a response that violates the supported contract."""


def _positive_float(raw: str, variable: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise EsRagConfigurationError(
            f"{variable} must be a number", operation="configuration"
        ) from exc
    if value <= 0:
        raise EsRagConfigurationError(
            f"{variable} must be greater than zero", operation="configuration"
        )
    return value


def _version_tuple(value: str) -> tuple[int, int, int]:
    try:
        parts = tuple(int(part) for part in str(value).strip().split("-"))
    except ValueError as exc:
        raise EsRagContractError(
            f"ES RAG returned invalid contract_version {value!r}",
            operation="capabilities",
        ) from exc
    if len(parts) != 3:
        raise EsRagContractError(
            f"ES RAG returned invalid contract_version {value!r}",
            operation="capabilities",
        )
    return parts


def _multipart_body(
    fields: dict[str, Any],
    *,
    file_field: str,
    filename: str,
    content: bytes,
    media_type: str,
) -> tuple[bytes, str]:
    boundary = f"----MemoryTracker{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        if value is None:
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = str(value)
        chunks.extend((
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            rendered.encode("utf-8"),
            b"\r\n",
        ))
    safe_filename = re.sub(
        r"[\x00-\x1f\x7f]+", "_", Path(filename).name.replace('"', "")
    ) or "upload.bin"
    chunks.extend((
        f"--{boundary}\r\n".encode(),
        (
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{safe_filename}"\r\n'
        ).encode(),
        f"Content-Type: {media_type}\r\n\r\n".encode(),
        bytes(content),
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ))
    return b"".join(chunks), boundary


class EsRagGateway:
    """Small synchronous client for the current ES RAG external contract."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        capability_ttl_seconds: float = DEFAULT_CAPABILITY_TTL_SECONDS,
    ) -> None:
        clean_url = str(base_url or "").strip().rstrip("/")
        clean_key = str(api_key or "").strip()
        if not clean_url:
            raise EsRagConfigurationError(
                "ESRAG_API_URL is not configured", operation="configuration"
            )
        if not clean_key:
            raise EsRagConfigurationError(
                "ES_RAG_API_KEY is not configured", operation="configuration"
            )
        if timeout_seconds <= 0 or capability_ttl_seconds <= 0:
            raise EsRagConfigurationError(
                "ES RAG timeout values must be greater than zero",
                operation="configuration",
            )
        self.base_url = clean_url
        self._api_key = clean_key
        self.timeout_seconds = float(timeout_seconds)
        self.capability_ttl_seconds = float(capability_ttl_seconds)
        self._capabilities: dict[str, Any] | None = None
        self._capabilities_expires_at = 0.0

    @classmethod
    def from_environment(cls) -> "EsRagGateway":
        base_url = os.getenv("ESRAG_API_URL", "").strip()
        api_key = os.getenv("ES_RAG_API_KEY", "").strip()
        if not base_url:
            raise EsRagConfigurationError(
                "ESRAG_API_URL is not configured", operation="configuration"
            )
        if not api_key:
            raise EsRagConfigurationError(
                "ES_RAG_API_KEY is not configured", operation="configuration"
            )
        timeout = _positive_float(
            os.getenv("ESRAG_TIMEOUT_SEC", str(DEFAULT_TIMEOUT_SECONDS)),
            "ESRAG_TIMEOUT_SEC",
        )
        capability_ttl = _positive_float(
            os.getenv(
                "ESRAG_CAPABILITY_TTL_SEC",
                str(DEFAULT_CAPABILITY_TTL_SECONDS),
            ),
            "ESRAG_CAPABILITY_TTL_SEC",
        )
        return cls(
            base_url,
            api_key,
            timeout_seconds=timeout,
            capability_ttl_seconds=capability_ttl,
        )

    def _headers(self, **extra: str) -> dict[str, str]:
        return {"Authorization": "Bearer " + self._api_key, "X-RAG-Ingest-Key": self._api_key, **extra}

    def _request_bytes(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[bytes, str]:
        if (self._capabilities or {}).get('contract')=='equity_swarm_external_capabilities_v1' and path.startswith('/api/rag/') and path.split('?')[0] not in {'/api/rag/capabilities','/api/rag/ask'}:
            raise EsRagContractError('This legacy ES operation is retired; use filtered document discovery or external answers',operation=operation)
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers=self._headers(**(headers or {})),
            method=method,
        )
        try:
            with urlopen(
                request,
                timeout=float(timeout_seconds or self.timeout_seconds),
            ) as response:
                content = response.read()
                return content, response.headers.get_content_type()
        except HTTPError as exc:
            # Consume the body but never include upstream-controlled text in
            # exceptions: it may echo credentials or private document data.
            exc.read(2000)
            raise EsRagTransportError(
                f"ES RAG {operation} returned HTTP {exc.code}",
                operation=operation,
                status_code=exc.code,
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise EsRagTransportError(
                f"ES RAG {operation} request failed: {reason}",
                operation=operation,
            ) from exc

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        payload: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        content, _ = self._request_bytes(
            method,
            path,
            operation=operation,
            body=body,
            headers=headers,
            timeout_seconds=timeout_seconds,
        )
        try:
            parsed = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EsRagContractError(
                f"ES RAG {operation} returned invalid JSON", operation=operation
            ) from exc
        if not isinstance(parsed, dict):
            raise EsRagContractError(
                f"ES RAG {operation} response must be an object",
                operation=operation,
            )
        return parsed

    def _multipart_json(
        self,
        path: str,
        *,
        operation: str,
        fields: dict[str, Any],
        filename: str,
        content: bytes,
        media_type: str,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        body, boundary = _multipart_body(
            fields,
            file_field="file",
            filename=filename,
            content=content,
            media_type=media_type,
        )
        raw, _ = self._request_bytes(
            "POST",
            path,
            operation=operation,
            body=body,
            headers={
                "Accept": "application/json",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            timeout_seconds=timeout_seconds,
        )
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EsRagContractError(
                f"ES RAG {operation} returned invalid JSON", operation=operation
            ) from exc
        if not isinstance(parsed, dict):
            raise EsRagContractError(
                f"ES RAG {operation} response must be an object",
                operation=operation,
            )
        return parsed

    def capabilities(self, *, force_refresh: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if (
            not force_refresh
            and self._capabilities is not None
            and now < self._capabilities_expires_at
        ):
            return self._capabilities
        result = self._request_json(
            "GET", "/api/rag/capabilities", operation="capabilities"
        )
        if result.get('contract') == 'equity_swarm_external_capabilities_v1':
            if 'answer' not in result.get('operations',[]):
                raise EsRagContractError('ES answer operation unavailable',operation='capabilities')
            self._capabilities=result
            self._capabilities_expires_at=now+self.capability_ttl_seconds
            return result
        detected = str(result.get("contract_version") or "").strip()
        if _version_tuple(detected) < _version_tuple(MIN_CONTRACT_VERSION):
            raise EsRagContractError(
                "ES RAG contract is incompatible: "
                f"requires {MIN_CONTRACT_VERSION} or newer, detected {detected}",
                operation="capabilities",
            )
        self._capabilities = result
        self._capabilities_expires_at = now + self.capability_ttl_seconds
        return result

    def _ensure_compatible(self) -> None:
        self.capabilities()

    @staticmethod
    def _validate_count_write(
        result: dict[str, Any], *, operation: str, count_field: str = "chunks"
    ) -> dict[str, Any]:
        if result.get("skipped") is True:
            return result
        if result.get("ok") is not True:
            raise EsRagContractError(
                f"ES RAG {operation} did not confirm success",
                operation=operation,
            )
        count = result.get(count_field)
        if isinstance(count, bool) or not isinstance(count, (int, float)):
            raise EsRagContractError(
                f"ES RAG {operation} response has invalid {count_field}",
                operation=operation,
            )
        return result

    def health(self, *, detail: bool = False) -> dict[str, Any]:
        self._ensure_compatible()
        return self._request_json(
            "GET",
            f"/api/rag/health?{urlencode({'detail': str(bool(detail)).lower()})}",
            operation="health",
            timeout_seconds=min(10.0, self.timeout_seconds),
        )

    def query(
        self,
        query: str,
        *,
        ticker: str = "",
        source_filter: str = "all",
        n_results: int = 8,
        user_input: bool = True,
        database_candidates: int | None = None,
        rerank_candidates: int | None = None,
        include_evidence_packet: bool = False,
        debug: bool = False,
        filters: dict[str, Any] | None = None,
        metadata_filters: dict[str, Any] | None = None,
        theme: str | list[str] | None = None,
    ) -> dict[str, Any]:
        self._ensure_compatible()
        clean_filter = str(source_filter or "all").strip().lower()
        if clean_filter not in _VALID_SOURCE_FILTERS:
            raise ValueError(
                "source_filter must be one of: " + ", ".join(sorted(_VALID_SOURCE_FILTERS))
            )
        payload: dict[str, Any] = {
            "ticker": ticker,
            "query": query,
            "source_filter": clean_filter,
            "n_results": int(n_results),
            "user_input": bool(user_input),
            "include_evidence_packet": bool(include_evidence_packet),
            "debug": bool(debug),
        }
        if database_candidates is not None:
            payload["database_candidates"] = max(1, int(database_candidates))
        if rerank_candidates is not None:
            payload["rerank_candidates"] = max(0, int(rerank_candidates))
        if filters:
            payload["filters"] = filters
        if metadata_filters:
            payload["metadata_filters"] = metadata_filters
        if theme:
            payload["theme"] = theme
        result = self._request_json(
            "POST", "/api/rag/query", operation="query", payload=payload
        )
        if not isinstance(result.get("hits"), list):
            raise EsRagContractError(
                "ES RAG query response has invalid hits", operation="query"
            )
        return result

    def chart_inventory(
        self,
        *,
        theme: str = "ai",
        metadata_filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Read the current ES Gallery taxonomy and complete chart inventory.

        Gallery classification is owned by ``/api/charts/browse`` rather than
        the RAG query endpoint.  The compatibility return shape keeps existing
        Memory callers isolated from that transport change.
        """
        filters = dict(metadata_filters or {})
        category = str(
            filters.get("industry") or filters.get("primary_industry") or ""
        ).strip()
        niche = str(
            filters.get("niche_sector")
            or filters.get("primary_niche_sector")
            or ""
        ).strip()
        broker = str(filters.get("broker") or "").strip().casefold()

        catalogue = self.browse_charts(group="industry", limit=1, theme=theme)
        groups = catalogue["groups"]
        if category:
            groups = [group for group in groups if group.get("group") == category]

        hits: list[dict[str, Any]] = []
        for group in groups:
            slug = str(group.get("group") or "").strip()
            if not slug:
                continue
            count = max(0, int(group.get("count") or 0))
            if niche:
                matching_niche = next(
                    (
                        item for item in (group.get("subsectors") or [])
                        if str(item.get("niche") or "") == niche
                    ),
                    None,
                )
                count = max(0, int((matching_niche or {}).get("count") or 0))
            label = str(group.get("label") or slug)
            unique_rows: dict[tuple[str, str, str, str], dict[str, Any]] = {}
            # Offset pagination has no snapshot token. Retry once when a page
            # shift produces duplicate/missing stable chart identities.
            for attempt in range(2):
                unique_rows = {}
                for offset in range(0, count, GALLERY_PAGE_SIZE):
                    page = self.browse_charts(
                        group="industry",
                        limit=GALLERY_PAGE_SIZE,
                        offset=offset,
                        category=slug,
                        niche=niche,
                        theme=theme,
                    )
                    page_group = next(
                        (
                            item for item in page["groups"]
                            if str(item.get("group") or "") == slug
                        ),
                        None,
                    )
                    if page_group is None:
                        break
                    rows = page_group.get("charts") or []
                    if not rows:
                        break
                    for chart in rows:
                        row = dict(chart)
                        if not str(row.get("industry") or "").strip():
                            row["industry"] = slug
                        if not str(row.get("industry_label") or "").strip():
                            row["industry_label"] = label
                        identity = (
                            str(row.get("source_content_hash") or ""),
                            str(row.get("chart_file") or ""),
                            str(row.get("image_url") or ""),
                            str(row.get("source_page") or row.get("page_number") or ""),
                        )
                        unique_rows.setdefault(identity, row)
                if len(unique_rows) >= count or attempt == 1:
                    break
            for row in unique_rows.values():
                if broker and str(row.get("broker") or "").strip().casefold() != broker:
                    continue
                hits.append(row)

        return {
            "hits": hits,
            "n_total": len(hits),
            "counts": {"charts": len(hits)},
            "inventory": True,
            "ranked": False,
            "gallery_by": "industry",
            "theme": theme,
        }

    def browse_charts(
        self,
        *,
        group: str = "industry",
        limit: int = 20,
        offset: int = 0,
        category: str = "",
        niche: str = "",
        theme: str = "",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "group": group,
            "limit": max(1, int(limit)),
            "offset": max(0, int(offset)),
        }
        if category:
            params["category"] = category
        if niche:
            params["niche"] = niche
        if theme:
            params["theme"] = theme
        result = self._request_json(
            "GET",
            f"/api/charts/browse?{urlencode(params)}",
            operation="gallery_browse",
        )
        groups = result.get("groups")
        if not isinstance(groups, list) or any(not isinstance(item, dict) for item in groups):
            raise EsRagContractError(
                "ES Gallery browse response has invalid groups",
                operation="gallery_browse",
            )
        for item in groups:
            if not isinstance(item.get("charts"), list):
                raise EsRagContractError(
                    "ES Gallery browse group has invalid charts",
                    operation="gallery_browse",
                )
        return result

    def chart_asset(self, relpath: str) -> tuple[bytes, str]:
        self._ensure_compatible()
        clean = str(relpath or "").replace("\\", "/").lstrip("/")
        if not clean or ".." in clean.split("/"):
            raise EsRagContractError(
                "Invalid ES chart asset path",
                operation="chart_asset",
                status_code=404,
            )
        content, media_type = self._request_bytes(
            "GET",
            f"/api/analysis/chart-asset/{quote(clean, safe='/')}",
            operation="chart_asset",
            headers={"Accept": "image/*"},
        )
        if not content:
            raise EsRagContractError(
                "ES RAG chart asset response was empty", operation="chart_asset"
            )
        return content, media_type or "application/octet-stream"

    def sources(self, **filters: Any) -> dict[str, Any]:
        self._ensure_compatible()
        params = {
            key: (str(value).lower() if isinstance(value, bool) else value)
            for key, value in filters.items()
            if value is not None and value != ""
        }
        path = "/api/rag/sources"
        if params:
            path += "?" + urlencode(params)
        result = self._request_json("GET", path, operation="sources")
        if not isinstance(result.get("sources"), list):
            raise EsRagContractError(
                "ES RAG sources response has invalid sources", operation="sources"
            )
        return result

    def chunks(self, *, filename: str, **filters: Any) -> dict[str, Any]:
        self._ensure_compatible()
        if not str(filename or "").strip():
            raise ValueError("filename is required")
        params: dict[str, Any] = {"filename": filename}
        params.update({
            key: (str(value).lower() if isinstance(value, bool) else value)
            for key, value in filters.items()
            if value is not None and value != ""
        })
        result = self._request_json(
            "GET", "/api/rag/chunks?" + urlencode(params), operation="chunks"
        )
        if not isinstance(result.get("chunks"), list):
            raise EsRagContractError(
                "ES RAG chunks response has invalid chunks", operation="chunks"
            )
        return result

    def ingest_pdf(
        self,
        content: bytes,
        *,
        filename: str,
        ticker: str = "MEMORY",
        broker: str = "",
        date: str = "",
        quarter: str = "",
        report_type: str = "broker_research",
        extract_charts: bool = True,
        force: bool = False,
        source: str = "",
        source_rel_path: str = "",
        source_origin: str = "external_api",
    ) -> dict[str, Any]:
        self._ensure_compatible()
        result = self._multipart_json(
            "/api/rag/ingest-pdf",
            operation="ingest_pdf",
            fields={
                "ticker": ticker, "broker": broker, "date": date,
                "quarter": quarter, "report_type": report_type,
                "extract_charts": extract_charts, "force": force,
                "source": source, "source_rel_path": source_rel_path,
                "source_origin": source_origin,
            },
            filename=filename,
            content=content,
            media_type="application/pdf",
        )
        if not str(result.get("job_id") or "").strip():
            raise EsRagContractError(
                "ES RAG ingest_pdf response has no job_id",
                operation="ingest_pdf",
            )
        return result

    def index_text(self, text: str, *, filename: str, **fields: Any) -> dict[str, Any]:
        self._ensure_compatible()
        payload = {"text": text, "filename": filename, **fields}
        result = self._request_json(
            "POST", "/api/rag/index-text", operation="index_text", payload=payload
        )
        return self._validate_count_write(result, operation="index_text")

    def index_excel(
        self,
        path_or_bytes: str | Path | bytes,
        *,
        filename: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        self._ensure_compatible()
        if isinstance(path_or_bytes, (bytes, bytearray)):
            content = bytes(path_or_bytes)
            name = filename or "workbook.xlsx"
        else:
            path = Path(path_or_bytes)
            content = path.read_bytes()
            name = filename or path.name
        result = self._multipart_json(
            "/api/rag/index-excel",
            operation="index_excel",
            fields=fields,
            filename=name,
            content=content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        return self._validate_count_write(result, operation="index_excel")

    def index_chart_captions(
        self,
        captions: list[dict[str, Any]],
        *,
        filename: str,
        ticker: str = "MEMORY",
        broker: str = "",
        date: str = "",
        source_rel_path: str = "",
    ) -> dict[str, Any]:
        self._ensure_compatible()
        result = self._request_json(
            "POST",
            "/api/rag/index-chart-captions",
            operation="index_chart_captions",
            payload={
                "captions": captions, "filename": filename, "ticker": ticker,
                "broker": broker, "date": date, "source_rel_path": source_rel_path,
            },
        )
        return self._validate_count_write(result, operation="index_chart_captions")

    def inject_chart(
        self,
        content: bytes,
        *,
        filename: str,
        caption: str = "",
        ticker: str = "MEMORY",
        broker: str = "",
        date: str = "",
        page: int = 0,
        auto_caption: bool = True,
        source: str = "",
    ) -> dict[str, Any]:
        self._ensure_compatible()
        result = self._multipart_json(
            "/api/rag/inject-chart",
            operation="inject_chart",
            fields={
                "caption": caption, "ticker": ticker, "broker": broker,
                "date": date, "page": int(page or 0),
                "auto_caption": auto_caption, "source": source,
            },
            filename=filename,
            content=content,
            media_type="application/octet-stream",
        )
        inner = result.get("result")
        if result.get("ok") is not True or not isinstance(inner, dict):
            raise EsRagContractError(
                "ES RAG inject_chart did not confirm success",
                operation="inject_chart",
            )
        indexed = inner.get("indexed")
        if isinstance(indexed, bool) or not isinstance(indexed, (int, float)):
            raise EsRagContractError(
                "ES RAG inject_chart response has invalid indexed count",
                operation="inject_chart",
            )
        return result

    def inject_fact(self, text: str, *, date: str, **fields: Any) -> dict[str, Any]:
        self._ensure_compatible()
        payload = {"text": text, "date": date, **fields}
        result = self._request_json(
            "POST", "/api/rag/inject-fact", operation="inject_fact", payload=payload
        )
        inner = result.get("result")
        if result.get("ok") is not True or not isinstance(inner, dict):
            raise EsRagContractError(
                "ES RAG inject_fact did not confirm success",
                operation="inject_fact",
            )
        chunks = inner.get("chunks")
        if isinstance(chunks, bool) or not isinstance(chunks, (int, float)):
            raise EsRagContractError(
                "ES RAG inject_fact response has invalid chunks",
                operation="inject_fact",
            )
        return result

    def get_job(self, job_id: str) -> dict[str, Any]:
        self._ensure_compatible()
        result = self._request_json(
            "GET", f"/api/rag/jobs/{quote(str(job_id), safe='')}", operation="get_job"
        )
        status = str(result.get("status") or "").lower()
        if status not in _VALID_JOB_STATUSES:
            raise EsRagContractError(
                "ES RAG get_job response has invalid status",
                operation="get_job",
            )
        return result

    def list_jobs(self, *, limit: int = 50, status: str = "") -> dict[str, Any]:
        self._ensure_compatible()
        params: dict[str, Any] = {"limit": int(limit)}
        if status:
            params["status"] = status
        return self._request_json(
            "GET", "/api/rag/jobs?" + urlencode(params), operation="list_jobs"
        )

    def wait_for_job(
        self,
        job_id: str,
        *,
        timeout_s: float = 600.0,
        poll_s: float = 2.0,
        terminal: Iterable[str] = ("done", "failed", "skipped"),
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        terminal_states = set(terminal)
        while True:
            job = self.get_job(job_id)
            if str(job.get("status") or "") in terminal_states:
                return job
            if time.monotonic() >= deadline:
                raise EsRagTransportError(
                    f"ES RAG job {job_id} did not finish within {timeout_s}s",
                    operation="wait_for_job",
                )
            time.sleep(poll_s)

    def ask(
        self,
        message: str,
        *,
        session_id: str = "default",
        project: str = "memory_tracker",
        ticker: str = "",
        context_label: str = "",
        filters: dict[str, Any] | None = None,
        metadata_filters: dict[str, Any] | None = None,
        theme: str | list[str] | None = None,
    ) -> str:
        self._ensure_compatible()
        if self._capabilities.get('contract') == 'equity_swarm_external_capabilities_v1':
            from .es_research_filters import memory_filters
            scope=memory_filters(filters,metadata_filters)
            if ticker:
                scope=memory_filters(scope,{'tickers':[ticker]})
            result=self._request_json('POST','/api/external/answer',operation='ask',payload={
                'question':message,'filters':scope,'allow_web':False,'include_charts':False})
            applied=result.get('applied_filters') or {}
            if (result.get('contract')!='equity_swarm_external_answer_v1'
                    or not isinstance(result.get('answer_markdown'),str)
                    or any(applied.get(k)!=v for k,v in scope.items())):
                raise EsRagContractError('ES did not confirm the requested research scope',operation='ask')
            return result['answer_markdown']
        payload: dict[str, Any] = {
            "message": message,
            "session_id": session_id,
            "project": project,
            "ticker": ticker,
            "context_label": context_label,
        }
        if filters:
            payload["filters"] = filters
        if metadata_filters:
            payload["metadata_filters"] = metadata_filters
        if theme:
            payload["theme"] = theme
        raw, _ = self._request_bytes(
            "POST",
            "/api/rag/ask",
            operation="ask",
            body=json.dumps(payload).encode("utf-8"),
            headers={
                "Accept": "text/event-stream",
                "Content-Type": "application/json",
            },
            timeout_seconds=max(self.timeout_seconds, 300.0),
        )
        event = "message"
        tokens: list[str] = []
        terminal: dict[str, Any] | None = None
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if not line:
                event = "message"
                continue
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip() or "message"
                continue
            if not line.startswith("data:"):
                continue
            data_text = line.split(":", 1)[1].strip()
            try:
                data = json.loads(data_text)
            except json.JSONDecodeError:
                data = {"raw": data_text}
            if event == "token" and isinstance(data, dict):
                tokens.append(str(data.get("token") or ""))
            elif event == "done" and isinstance(data, dict):
                terminal = data
            elif event == "error":
                detail = data.get("message") if isinstance(data, dict) else str(data)
                raise EsRagTransportError(
                    f"ES RAG ask stream failed: {detail or data}", operation="ask"
                )
        if terminal is None:
            raise EsRagContractError(
                "ES RAG ask stream ended without a terminal done event",
                operation="ask",
            )
        return str(terminal.get("content") or "".join(tokens))

    def research_filters(self):
        result=self._request_json('GET','/api/research-documents/filters',operation='research_filters')
        if result.get('contract')!='research_documents_v1':
            raise EsRagContractError('Unsupported filter catalogue',operation='research_filters')
        return result

    def search_research_documents(self, *, filters=None, limit=20, offset=0, inventory_revision=None):
        from .es_research_filters import memory_filters
        scope=memory_filters(filters)
        result=self._request_json('POST','/api/research-documents/search',operation='research_search',payload={
            'filters':scope,'limit':limit,'offset':offset,'inventory_revision':inventory_revision})
        if (result.get('contract')!='research_documents_v1' or not isinstance(result.get('results'),list)
                or any((result.get('applied_filters') or {}).get(k)!=v for k,v in scope.items())):
            raise EsRagContractError('ES did not confirm the requested research scope',operation='research_search')
        return result

_gateway_cache: dict[tuple[str, str, float, float], EsRagGateway] = {}


def get_es_rag_gateway() -> EsRagGateway:
    """Return a process-cached gateway for the current environment values."""
    candidate = EsRagGateway.from_environment()
    key = (
        candidate.base_url,
        candidate._api_key,
        candidate.timeout_seconds,
        candidate.capability_ttl_seconds,
    )
    gateway = _gateway_cache.get(key)
    if gateway is None:
        gateway = candidate
        _gateway_cache[key] = gateway
    return gateway


__all__ = [
    "MIN_CONTRACT_VERSION",
    "EsRagError",
    "EsRagConfigurationError",
    "EsRagTransportError",
    "EsRagContractError",
    "EsRagGateway",
    "get_es_rag_gateway",
]
