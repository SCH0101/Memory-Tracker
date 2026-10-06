import { galleryRequest } from "./galleryCache";

const BASE = "";

export function sourcePdfUrl(filename?: string | null, page?: number | null): string {
  const clean = (filename || "").trim();
  if (!clean) return "";
  const qs = new URLSearchParams({ filename: clean }).toString();
  const hash = page && page > 0 ? `#page=${page}` : "";
  return `${BASE}/api/charts/source-pdf?${qs}${hash}`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, init);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status}: ${text}`);
  }
  return res.json();
}

export const api = {
  getChartsByCategory: (refresh = false, bypassBrowserCache = false) => {
    const gallery = galleryRequest(refresh, Date.now(), bypassBrowserCache);
    return request<CategoryGroup[]>(gallery.path, gallery.init);
  },

  getCharts: (params?: Record<string, string>) => {
    const qs = params ? "?" + new URLSearchParams(params).toString() : "";
    return request<ChartRecord[]>(`/api/charts${qs}`);
  },

  getCategories: () =>
    request<Category[]>("/api/categories"),

  getBrokers: () =>
    request<string[]>("/api/charts/brokers"),

  getJob: (jobId: string) =>
    request<JobStatus>(`/api/jobs/${jobId}`),

  uploadPdfs: async (files: File[]) => {
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    return request<UploadResult[]>("/api/upload", {
      method: "POST",
      body: fd,
    });
  },

  updateChart: (id: number, body: Partial<ChartRecord>) =>
    request<ChartRecord>(`/api/charts/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  deleteChart: (id: number) =>
    request<{ ok: boolean }>(`/api/charts/${id}`, { method: "DELETE" }),

  // RAG-grounded chat (Ask-Claude-style, scoped to the AI theme by default).
  chat: (messages: ChatTurn[], sessionId?: string) =>
    request<ChatResponse>("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages, session_id: sessionId }),
    }),

  // RAG chart search / inventory (each chart has a displayable image_url).
  ragCharts: (
    query: string,
    opts?: { topN?: number; ranked?: boolean; inventory?: boolean; broker?: string; niche?: string; industry?: string },
  ) => {
    const qs = new URLSearchParams({
      query,
      top_n: String(opts?.inventory ? 0 : opts?.topN ?? 12),
      ranked: String(opts?.inventory ? false : opts?.ranked ?? true),
      mode: opts?.inventory ? "inventory" : "ranked",
    });
    if (opts?.broker) qs.set("broker", opts.broker);
    if (opts?.niche) qs.set("niche", opts.niche);
    if (opts?.industry) qs.set("industry", opts.industry);
    return request<{ query: string; total: number; ranked: boolean; capped: boolean; top_n: number; charts: RagChart[] }>(
      `/api/charts/rag?${qs.toString()}`
    );
  },

  // Paste an image → Haiku tags/categorizes it, saves it, and indexes it into
  // the RAG as a CHART. Returns the tag + whether it landed in the fact pool.
  pasteChart: async (
    file: File | Blob,
    opts?: { hint?: string; category?: string; subcategory?: string }
  ) => {
    const fd = new FormData();
    fd.append("image", file, "pasted.png");
    if (opts?.hint) fd.append("user_hint", opts.hint);
    if (opts?.category) fd.append("category", opts.category);
    if (opts?.subcategory) fd.append("subcategory", opts.subcategory);
    return request<PasteResult>("/api/paste-chart", { method: "POST", body: fd });
  },

  // Paste a free-text note → indexed as `doc_type='user_input'` in the shared
  // fact pool. Optional metadata wins over auto-tag.
  pasteText: async (
    text: string,
    opts?: { title?: string; ticker?: string; broker?: string; date?: string;
             user_hint?: string; auto_tag?: boolean },
  ) => {
    const fd = new FormData();
    fd.append("text", text);
    if (opts?.title) fd.append("title", opts.title);
    if (opts?.ticker) fd.append("ticker", opts.ticker);
    if (opts?.broker) fd.append("broker", opts.broker);
    if (opts?.date) fd.append("date_pertains_to", opts.date);
    if (opts?.user_hint) fd.append("user_hint", opts.user_hint);
    if (opts?.auto_tag === false) fd.append("auto_tag", "false");
    return request<PasteTextResult>("/api/paste-text", { method: "POST", body: fd });
  },

  // ---- Overview (RAG-grounded industry / company narrative) ----
  industryOverview: (refresh = false, broker = "Arete") => {
    const params = new URLSearchParams({ broker });
    if (refresh) {
      params.set("refresh", "true");
      params.set("_", String(Date.now()));
    }
    return request<OverviewResponse>(
      `/api/overview/industry?${params.toString()}`,
      refresh ? { cache: "no-store" } : undefined,
    );
  },
  industryBrokers: () => request<IndustryBrokerOptions>("/api/overview/industry/brokers"),
  companyOverview: (refresh = false) =>
    request<OverviewResponse>(
      `/api/overview/company${refresh ? `?refresh=true&_=${Date.now()}` : ""}`,
      refresh ? { cache: "no-store" } : undefined,
    ),
  chartIndex: () => request<ChartIndex>("/api/overview/chartindex"),

  // ---- Model (SK Hynix HBM/DRAM/NAND revenue model) ----
  getModel: () => request<ModelResponse>("/api/model/hbm"),
  getWorkbookModel: () =>
    request<WorkbookModelResponse>(`/api/model/workbook?_=${Date.now()}`, { cache: "no-store" }),
  setModelCell: (row_key: string, period: string, value: number | null, formula?: string | null) =>
    request<{ ok: boolean }>("/api/model/hbm/cell", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ row_key, period, value, ...(formula !== undefined ? { formula } : {}) }),
    }),
  createRackRow: (vendor: string) =>
    request<{ ok: boolean; row_id: number }>("/api/model/hbm/rack-row", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ vendor }),
    }),
  updateRackRow: (row_id: number, body: { name?: string; vendor?: string; display_order?: number }) =>
    request<{ ok: boolean }>(`/api/model/hbm/rack-row/${row_id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  deleteRackRow: (row_id: number) =>
    request<{ ok: boolean }>(`/api/model/hbm/rack-row/${row_id}`, { method: "DELETE" }),
  setRackCell: (row_id: number, field_key: string, period: string, value: number | null, formula?: string | null) =>
    request<{ ok: boolean }>("/api/model/hbm/rack-cell", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ row_id, field_key, period, value, ...(formula !== undefined ? { formula } : {}) }),
    }),
  refreshModel: () =>
    request<{ ok: boolean; cells_pulled: number }>("/api/model/hbm/refresh", { method: "POST" }),
  getModelNews: () => request<NewsResponse>("/api/model/news"),
  refreshModelNews: () => request<NewsResponse>("/api/model/news/refresh", { method: "POST" }),

  // ---- Dashboard (time-series) ----
  dashboard: (frequency: DashboardFrequency = "yearly") =>
    request<DashboardResponse>(
      `/api/dashboard/timeseries?${new URLSearchParams({ frequency }).toString()}`,
    ),
  dashboardQuarantine: (limit = 100, frequency: DashboardFrequency = "yearly") =>
    request<{ rows: DashboardQuarantineRow[] }>(
      `/api/dashboard/quarantine?${new URLSearchParams({ limit: String(limit), frequency }).toString()}`,
    ),
  dashboardHealthAudit: (frequency: DashboardFrequency = "yearly") =>
    request<DashboardHealthAudit>(
      `/api/dashboard/health-audit?${new URLSearchParams({ frequency }).toString()}`,
    ),
  rebuildDashboard: (metric?: string) =>
    request<{ ok: boolean; metric?: string | null; result: Record<string, number> }>(
      `/api/dashboard/rebuild${metric ? `?${new URLSearchParams({ metric }).toString()}` : ""}`,
      { method: "POST" },
    ),
  rebuildDashboardCache: (params?: { frequency?: DashboardFrequency | "all"; wait?: boolean }) => {
    const qs = new URLSearchParams({
      frequency: params?.frequency || "all",
      wait: String(params?.wait ?? false),
    });
    return request<{ ok: boolean; frequency: string; wait: boolean; jobs: DashboardCacheJob[] }>(
      `/api/dashboard/rebuild-cache?${qs.toString()}`,
      { method: "POST" },
    );
  },
  dashboardCacheStatus: () =>
    request<DashboardCacheStatus>("/api/dashboard/cache-status"),
  refreshDashboardRag: (params?: { metric?: string; broker?: string; frequency?: DashboardFrequency }) => {
    const qs = params
      ? "?" + new URLSearchParams(Object.entries(params).filter(([, v]) => v) as [string, string][]).toString()
      : "";
    return request<{
      job_id: string;
      known_brokers: string[];
      active_excel_brokers?: string[];
      rag_brokers?: string[];
      allowed_sources?: string[];
      excel_override_policy?: string;
    }>(
      `/api/dashboard/refresh-rag${qs}`,
      { method: "POST" },
    );
  },
  refreshDashboardStatus: (job_id: string) =>
    request<RefreshJobStatus>(`/api/dashboard/refresh-status/${job_id}`),
  inputDashboardDataAi: (body: { broker: string; text: string; frequency?: DashboardFrequency }) =>
    request<DashboardInputAiResult>("/api/dashboard/input-ai", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  // ---- Corpus stats (Qdrant point count + last-write ts) ----
  corpusStats: () => request<CorpusStats>("/api/corpus-stats"),

  // ---- Excel model ingestion ----
  uploadExcel: async (file: File, opts?: { broker?: string; force?: boolean }) => {
    const fd = new FormData();
    fd.append("file", file);
    if (opts?.broker) fd.append("broker", opts.broker);
    if (opts?.force) fd.append("force", "true");
    return request<ExcelIngestResult>("/api/upload-excel", { method: "POST", body: fd });
  },
  resolveExcelBroker: (filename: string) =>
    request<ExcelBrokerResolution>(
      `/api/excel/resolve-broker?${new URLSearchParams({ filename }).toString()}`,
    ),
  excelIngests: () => request<{ ingests: ExcelIngestRow[] }>("/api/excel-ingests"),

  // ---- RAG / prompt settings ----
  ragSettings: () => request<RagSettingsResponse>("/api/settings/rag"),
  updateRagSettings: (body: RagSettings) =>
    request<{ settings: RagSettings }>("/api/settings/rag", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
};

export interface RagRetrievalSettings {
  chunks_get: number;
  rerank_keep: number;
  sources: string[];
}
export interface RagSectionSetting {
  query?: string;
  prompt?: string;
  sources?: string[];
}
export interface RagSettings {
  retrieval: RagRetrievalSettings;
  tabs: {
    overview: {
      industry_prompt: string;
      company_prompt: string;
      sources: string[];
      sections: Record<string, RagSectionSetting>;
    };
    dashboard: { prompt: string; sources: string[] };
    model: { prompt: string; sources: string[] };
    ask_claude: { prompt: string; sources: string[] };
  };
}
export interface RagPromptTemplates {
  overview: {
    industry_prompt: string;
    company_prompt: string;
  };
  dashboard: { prompt: string };
  model: { prompt: string };
  ask_claude: { prompt: string };
}
export interface RagOverviewSectionSetting {
  key: string;
  label: string;
  default_query: string;
  query: string;
  prompt: string;
  sources: string[];
}
export interface RagSettingsResponse {
  settings: RagSettings;
  overview_sections: RagOverviewSectionSetting[];
  source_options: string[];
  prompt_templates: RagPromptTemplates;
  overview_api_prompts: { industry: string; company: string };
}

export interface ExcelIngestResult {
  ok: boolean;
  status?: "ingested" | "skipped" | "needs_template" | "needs_broker" | "broker_conflict" | "failed";
  skipped?: boolean;
  needs_broker?: boolean;
  file_sha256?: string;
  broker?: string;
  prior_broker?: string;
  filename?: string;
  template?: string;
  active?: boolean;
  rows_added?: number;
  raw_rows?: number;
  verified_rows?: number;
  quarantined_rows?: number;
  sheets_processed?: string[];
  warnings?: string[];
  coverage?: unknown;
  es_rag_index?: unknown;
  message?: string;
  error?: string;
}

export interface ExcelBrokerResolution {
  filename: string;
  broker: string;
  needs_broker: boolean;
  known_brokers: string[];
}

export interface ExcelIngestRow {
  id: number;
  file_sha256: string;
  source_filename: string;
  broker: string;
  template?: string;
  status?: string;
  active?: boolean;
  ingested_at: string;
  uploaded_at?: string;
  rows_added: number;
  raw_rows?: number;
  verified_rows?: number;
  quarantined_rows?: number;
  sheets_processed: string;
  warnings?: string[];
  coverage?: unknown;
}

// Types
export interface ChartRecord {
  id: number;
  source_pdf_id: number;
  subcategory_id: number | null;
  image_path: string;
  image_url?: string;
  page_number: number;
  region_type: string;
  tag: string;
  caption: string;
  chart_type: string;
  broker: string;
  chart_date: string;
  chart_date_int: number | null;
  quality_status: string;
  category_name: string | null;
  subcategory_name: string | null;
  source_filename: string | null;
  is_rag?: boolean;
}

export interface SubcategoryGroup {
  subcategory: string;
  sub_order: number;
  charts: ChartRecord[];
}

export interface CategoryGroup {
  main_group: string;
  category: string;
  subcategories: SubcategoryGroup[];
}

export interface Category {
  id: number;
  name: string;
  display_order: number;
  chart_count: number;
  subcategories: {
    id: number;
    name: string;
    display_order: number;
    chart_count: number;
  }[];
}

export interface JobStatus {
  id: string;
  filename: string;
  status: string;
  total_charts: number;
  processed_charts: number;
  error_message: string | null;
}

export interface UploadResult {
  job_id: string;
  filename: string;
  message: string;
}

export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
}

export interface RagChart {
  id?: number;
  tag?: string;
  caption: string;
  image_url: string;
  image_path?: string;
  broker: string;
  date: string;
  date_int: number;
  filename: string;
  source_filename?: string;
  page: number | null;
  chart_file: string;
  score: number;
  category?: string;
  subcategory?: string;
  content_type?: string;
  media_type?: string;
  doc_type?: string;
  source_doc_type?: string;
  source_rel_path?: string;
  citation_url?: string;
  citation_markdown?: string;
  primary_ticker?: string;
  primary_company?: string;
  primary_industry?: string;
  primary_niche_sector?: string;
  industry_tags?: string[];
  niche_sector_tags?: string[];
  subindustry_tags?: string[];
  kpi_tags?: string[];
  topic_tags?: string[];
  company_tags?: string[];
  period_tags?: string[];
  geo_tags?: string[];
  unit_tags?: string[];
}

export interface ChatSource {
  id?: string;
  provider?: string;
  broker: string;
  date: string;
  filename: string;
  page?: number | null;
}

export interface ChatResponse {
  reply?: string;
  error?: string;
  sources?: ChatSource[];
  charts?: RagChart[];
}

export interface PasteResult {
  id: number;
  image_path: string;
  tag: string;
  broker: string;
  date: string;
  category: string;
  subcategory: string;
  caption: string;
  rag_indexed: boolean;
  rag_image_url: string;
  message: string;
  /** true when the same image was already in the gallery (no duplicate row created). */
  deduped?: boolean;
}

export interface PasteTextResult {
  ok: boolean;
  fact_id?: string;
  chunks?: number;
  title?: string;
  ticker?: string;
  broker?: string;
  date?: string;
  sector?: string;
  auto_tagged?: boolean;
  company_tags?: string[];
  kpi_tags?: string[];
  topic_tags?: string[];
  message?: string;
  reason?: string;
}

// ---- Overview ----
export interface OverviewSourceRef {
  provider?: string;
  filename: string;
  page: number;
  broker: string;
  date: string;
  content_type: string;
}
export interface IndustryBrokerOptions {
  brokers: string[];
  date_from: string;
  date_to: string;
}
export interface OverviewTheme {
  title: string;
  bullets: string[];
}
export interface OverviewSection {
  group?: string;
  topic: string;
  unit?: string;
  summary?: string;
  summary_bullets?: string[];
  themes: OverviewTheme[];
}
export interface CompanyView {
  broker: string;
  bullets: string[];
}
export interface CompanySegment {
  topic: string;
  views: CompanyView[];
}
export interface CompanyEntry {
  name: string;
  summary?: string;
  summary_bullets?: string[];
  segments: CompanySegment[];
  strategic_updates?: string[];
}
export interface AreteLatestCard {
  topic: string;
  bullets: string[];
}
export interface AreteLatest {
  title?: string;
  source?: string;
  cards?: AreteLatestCard[];
  unavailable_bullets?: string[];
}
export interface OverviewChartRef {
  tag: string;
  caption: string;
  broker: string;
  date: string;
  filename: string;
  page: number;
  chart_file: string;
  chunk_id: string;
}
export interface OverviewResponse {
  answer_markdown?: string;
  title?: string;
  last_updated?: string;
  overall_summary?: string;
  overall_bullets?: string[];
  bull_case?: string[];
  bear_case?: string[];
  uncertain?: string[];
  arete_latest?: AreteLatest;
  sections?: OverviewSection[]; // industry
  companies?: CompanyEntry[]; // company
  _source_index?: Record<string, OverviewSourceRef>;
  /** RAG-sourced chart index baked into the response — same [id:N] keys
   * the LLM was given. Falls back to the legacy /chartindex endpoint
   * (local DB) for older cached responses that lack this field. */
  _chart_index?: Record<string, OverviewChartRef>;
  _generation_seconds?: number;
  error?: string;
}
export interface ChartIndexEntry {
  tag: string;
  broker: string;
  category: string | null;
  subcategory: string | null;
  date: string;
  image: string;
  filename: string | null;
  page: number | null;
}
export type ChartIndex = Record<string, ChartIndexEntry>;

// ---- Model ----
export interface ModelCell {
  value: number | null;
  source: string; // "user" | "formula" | "rag" | "default"
  quote: string;
  formula?: string;
}
export interface ModelRow {
  key: string;
  label: string;
  unit: string;
  type: "input" | "formula";
  group: string;
  tabs: string[];
  expr?: string | null;
  highlight: boolean;
  has_query: boolean;
  cells: Record<string, ModelCell>;
}
export interface RackField {
  key: string;
  label: string;
  unit: string;
  type: "input" | "formula";
  group: string;
  expr?: string | null;
  highlight: boolean;
  period_mode?: "period" | "single";
  display?: boolean;
}
export interface RackRow {
  id: number;
  vendor: string;
  name: string;
  display_order: number;
  cells: Record<string, Record<string, ModelCell>>;
}
export interface RackSection {
  vendor: string;
  label: string;
  rows: RackRow[];
}
export interface ModelResponse {
  periods: string[];
  tabs: string[];
  rows: ModelRow[];
  rack_fields?: RackField[];
  rack_sections?: RackSection[];
}
export interface WorkbookCell {
  address: string;
  value: string | number | boolean | null;
  display: string;
  is_percentage: boolean;
}
export interface WorkbookRow {
  row: number;
  cells: WorkbookCell[];
}
export interface WorkbookTable {
  sheet: string;
  range: string;
  columns: string[];
  rows: WorkbookRow[];
}
export interface WorkbookCapexSeries {
  name: string;
  values: Array<number | null>;
}
export interface WorkbookCapexTable extends WorkbookTable {
  table: WorkbookTable;
  years: Array<number | string | null>;
  series: WorkbookCapexSeries[];
}
export interface WorkbookModelResponse {
  source: {
    filename: string;
    modified_at: string;
  };
  hbm: WorkbookTable;
  capex: WorkbookCapexTable;
}
export interface NewsRow {
  company: string;
  topic: string;
  news_guidance: string;
  key_data: string;
  source: string;
  url: string;
  date: string;
}
export interface NewsResponse {
  company: string;
  columns: string[];
  rows: NewsRow[];
  status: string;
  message: string;
  last_updated: string | null;
}

// ---- Dashboard ----
export type DashboardFrequency = "yearly" | "quarterly";

export interface SeriesPoint {
  period: string;
  period_int: number;
  value: number;
  source?: {
    filename: string;
    page: number;
    quote?: string;
    date?: string;
    source_type?: string;
    winner_reason?: string;
    source_broker?: string;
    subject_entity?: string;
  };
  verification_status?: "verified" | "needs_review";
  verification_note?: string;
  source_type?: string;
  winner_reason?: string;
  source_broker?: string;
  subject_entity?: string;
}

export interface RefreshJobStatus {
  id?: string;
  status?: "queued" | "running" | "validating" | "cache_rebuilding" | "done";
  total?: number;
  completed?: number;
  inserted?: number;
  failed?: number;
  metrics?: string[];
  brokers?: string[];
  started_at?: string;
  finished_at?: string;
  error?: string;
  errors?: { metric: string; broker: string; error: string }[];
  skipped_excel_brokers?: string[];
  allowed_sources?: string[];
  active_excel_brokers?: string[];
  validation?: Record<string, number | string>;
  cache_job?: DashboardCacheJob;
  cache_job_id?: string;
  frequency?: DashboardFrequency;
}

export interface DashboardInputAiResult {
  ok: boolean;
  broker: string;
  extracted: number;
  inserted: number;
  rejected: number;
  rejected_rows?: Array<{
    row: Record<string, unknown>;
    reason: string;
  }>;
  validation?: {
    verified?: number;
    quarantined?: number;
    needs_review?: number;
  };
  cache_job?: DashboardCacheJob;
  rows?: Array<{
    metric: string;
    period: string;
    raw_value: number;
    raw_unit: string;
    broker: string;
    source_quote: string;
  }>;
}

export interface DashSeries {
  broker: string;
  points: SeriesPoint[];
}
export interface DashMetric {
  metric: string;
  label: string;
  unit: string;
  series: DashSeries[];
  split_by_broker?: boolean;
}
export interface DashSegment {
  segment: string;
  metrics: DashMetric[];
}
export interface DashboardResponse {
  version: string;
  frequency?: DashboardFrequency;
  segments: DashSegment[];
  suppressed_points?: number;
  needs_review_points?: number;
  peer_gate_ratio?: number;
  cache?: DashboardCacheMeta;
}

export interface DashboardCacheMeta {
  frequency: DashboardFrequency;
  status: "ready" | "dirty" | "building" | "missing" | "error";
  version?: string;
  dirty_reason?: string;
  error?: string;
  raw_max_id?: number;
  verified_max_id?: number;
  quarantine_max_id?: number;
  created_at?: string;
  updated_at?: string;
  job_id?: string;
}

export interface DashboardCacheJob {
  id?: string;
  job_id?: string;
  frequency: DashboardFrequency;
  status: string;
  started?: boolean;
  started_at?: string;
  finished_at?: string;
  version?: string;
  error?: string;
}

export interface DashboardCacheStatus {
  frequencies: Record<DashboardFrequency, DashboardCacheMeta>;
  jobs: DashboardCacheJob[];
}

export interface DashboardQuarantineRow {
  metric: string;
  period: string;
  period_int: number;
  raw_value: number;
  raw_unit: string;
  broker: string;
  source_filename: string;
  source_date: string;
  source_page: number;
  source_quote: string;
  source_sheet?: string;
  source_cell?: string;
  extractor?: string;
  source_type?: string;
  source_broker?: string;
  subject_entity?: string;
  subject_type?: string;
  frequency?: DashboardFrequency;
  batch_id?: string;
  canonical_value?: number | null;
  canonical_unit?: string;
  reason: string;
}

export interface DashboardHealthAudit {
  frequency: DashboardFrequency;
  active_excel_brokers: string[];
  active_excel_dates: Record<string, string>;
  active_excel_models: Array<{
    broker: string;
    template: string;
    source_filename: string;
    uploaded_at?: string;
    ingested_at?: string;
    rows_added?: number;
    verified_rows?: number;
    quarantined_rows?: number;
  }>;
  source_mix_by_metric: Array<{ metric: string; source_type: string; rows: number }>;
  winner_reasons: Array<{ winner_reason: string; rows: number }>;
  latest_source_by_metric: Array<{
    metric: string;
    source_broker: string;
    subject_entity: string;
    latest_source_date: string;
    rows: number;
  }>;
  quarantine_reasons: Array<{ source_type: string; reason: string; rows: number }>;
  cache?: DashboardCacheStatus;
}

// ---- Corpus stats ----
export interface CorpusStats {
  ok: boolean;
  collection?: string;
  points?: number;
  last_index_ts?: number;
  qdrant_alive?: boolean;
  reason?: string;
}
