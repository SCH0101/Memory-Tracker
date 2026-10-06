import { useCallback, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Loader2, TrendingUp, TrendingDown, HelpCircle, FileText, Sparkles } from "lucide-react";
import { api, sourcePdfUrl, type OverviewResponse, type ChartIndex, type OverviewChartRef } from "../lib/api";
import { useLightbox } from "../contexts/LightboxContext";
import { useCorpusBaseline } from "../hooks/useCorpusBaseline";
import { MarkdownBlock } from "./MarkdownBlock";

type Sub = "industry" | "company";

function OverviewSections({ markdown, sources, headingLevel = 2 }: {
  markdown: string;
  sources?: OverviewResponse["_source_index"];
  headingLevel?: 1 | 2;
}) {
  const sections: { title: string; body: string }[] = [];
  let title = "Summary";
  let lines: string[] = [];
  const flush = () => {
    const body = lines.join("\n").trim();
    if (body && title.toLowerCase() !== "evidence gaps") sections.push({ title, body });
    lines = [];
  };
  for (const line of markdown.replace(/\r\n?/g, "\n").split("\n")) {
    const topic = line.match(/^(#{1,2})\s+(.+?)\s*$/);
    if (topic && topic[1].length === headingLevel) {
      flush();
      title = topic[2].replace(/\s*#+\s*$/, "").trim();
    } else if (headingLevel !== 2 || !/^#\s+/.test(line)) {
      lines.push(line);
    }
  }
  flush();
  return (
    <div className="space-y-4">
      {sections.map((section, index) => (
        <section key={`${section.title}-${index}`} className="rounded-lg border bg-card p-5">
          <h3 className="mb-3 text-base font-bold">{section.title}</h3>
          <MarkdownBlock text={section.body} sources={sources} />
        </section>
      ))}
    </div>
  );
}

// Render bullet text, turning [id:N] and [src:S#] markers into hover chips.
// Chart chips open the chart in the in-app lightbox; their label is the
// broker name when known, or "Chart" as fallback (NOT the numeric id).
// Citation resolution: prefer the response-baked `_chart_index` (live RAG
// scroll at generation time) over the legacy local-DB `chartIndex` query.
function CitedText({
  text,
  chartIndex,        // legacy local-DB fallback (from /api/overview/chartindex)
  ragChartIndex,     // RAG-sourced, returned in the Overview response
  sources,
}: {
  text: string;
  chartIndex?: ChartIndex;
  ragChartIndex?: Record<string, OverviewChartRef>;
  sources?: OverviewResponse["_source_index"];
}) {
  const lightbox = useLightbox();
  const parts = text.split(/(\[id:\d+\]|\[src:S\d+\])/g);
  return (
    <>
      {parts.map((part, i) => {
        const idm = part.match(/^\[id:(\d+)\]$/);
        if (idm) {
          // Prefer the RAG-sourced index baked into the response; fall back
          // to the legacy /chartindex (local DB) for cached responses that
          // pre-date this change.
          const rag = ragChartIndex?.[idm[1]];
          const c = chartIndex?.[idm[1]];
          const broker = rag?.broker || c?.broker || "";
          const label = broker.trim() || "Chart";
          const tag = rag?.tag || c?.tag || "";
          const date = rag?.date || c?.date || "";
          const filename = rag?.filename || c?.filename || "";
          const page = rag?.page ?? c?.page ?? undefined;
          // Image URL: RAG entries point at /api/charts/rag-asset/{chart_file}
          // (swarm chart cache); legacy entries use /charts/{image} static mount.
          const image = rag
            ? (rag.chart_file ? `/api/charts/rag-asset/${encodeURIComponent(rag.chart_file)}` : "")
            : (c?.image || "");
          const subInfo = c ? `\n${c.category || ""} › ${c.subcategory || ""}` : "";
          const title = (rag || c)
            ? `${tag || "chart"} — ${broker || "?"} ${date}${subInfo}`
            : `chart #${idm[1]}`;
          const enabled = !!(rag || c);
          const onClick = (e: React.MouseEvent) => {
            e.preventDefault();
            if (!enabled) return;
            lightbox.open({
              image,
              tag,
              broker,
              date,
              category: c?.category || undefined,
              subcategory: c?.subcategory || undefined,
              page,
              filename,
            });
          };
          return (
            <button
              key={i}
              type="button"
              onClick={onClick}
              disabled={!enabled}
              title={title}
              className="mx-0.5 inline-flex cursor-pointer items-center gap-0.5 rounded border border-[#1e3a5f] bg-[#0b1f3a] px-1 text-[9px] font-medium text-white align-middle hover:bg-[#12355f] disabled:cursor-not-allowed disabled:opacity-50"
            >
              📊{label}
            </button>
          );
        }
        const sm = part.match(/^\[src:(S\d+)\]$/);
        if (sm) {
          const s = sources?.[sm[1]];
          const title = s
            ? `${s.broker || "?"} ${s.date || ""}\n${s.filename || ""}${s.page ? ` · p.${s.page}` : ""}`
            : sm[1];
          return (
            <span
              key={i}
              title={title}
              className="mx-0.5 inline-flex cursor-pointer items-center rounded border border-[#1e3a5f] bg-[#0b1f3a] px-1 text-[9px] font-medium text-white align-middle hover:bg-[#12355f]"
            >
              {s?.broker || "src"}
            </span>
          );
        }
        return <span key={i}>{part}</span>;
      })}
    </>
  );
}

function HoverCitedText({
  text,
  chartIndex,
  ragChartIndex,
  sources,
}: {
  text: string;
  chartIndex?: ChartIndex;
  ragChartIndex?: Record<string, OverviewChartRef>;
  sources?: OverviewResponse["_source_index"];
}) {
  const basename = (path?: string | null) => (path || "").split(/[\\/]/).filter(Boolean).pop() || "";
  const sourceLabel = (filename?: string | null, page?: number | null) => {
    const file = basename(filename) || "Source";
    return page ? `${file} P.${page}` : file;
  };
  const citeChip = (
    label: string,
    cite: string,
    filename?: string | null,
    page?: number | null,
  ) => {
    const href = sourcePdfUrl(filename, page);
    const chip = (
      <span className="mc-cite-chip" tabIndex={0} data-cite={cite}>
        {label}
      </span>
    );
    return href ? (
      <a href={href} target="_blank" rel="noreferrer" className="mc-cite-link">
        {chip}
      </a>
    ) : chip;
  };
  const providerLabel = (label?: string | null) => {
    const clean = (label || "").trim();
    return clean && clean.toUpperCase() !== "MEMORY" ? clean : "source";
  };

  return (
    <>
      {text.split(/(\[id:\d+\]|\[src:S\d+\])/g).map((part, i) => {
        const idm = part.match(/^\[id:(\d+)\]$/);
        if (idm) {
          const rag = ragChartIndex?.[idm[1]];
          const c = chartIndex?.[idm[1]];
          const label = providerLabel(rag?.broker || c?.broker);
          const cite = sourceLabel(rag?.filename || c?.filename, rag?.page ?? c?.page);
          return (
            <span key={i}>
              {citeChip(label, cite, rag?.filename || c?.filename, rag?.page ?? c?.page)}
            </span>
          );
        }
        const sm = part.match(/^\[src:(S\d+)\]$/);
        if (sm) {
          const s = sources?.[sm[1]];
          const label = providerLabel(s?.broker);
          const cite = s ? sourceLabel(s.filename, s.page) : sm[1];
          return (
            <span key={i}>
              {citeChip(label, cite, s?.filename, s?.page)}
            </span>
          );
        }
        return <span key={i}>{part}</span>;
      })}
    </>
  );
}

function CaseList({
  title,
  items,
  icon,
  tone,
  chartIndex,
  ragChartIndex,
  sources,
}: {
  title: string;
  items?: string[];
  icon: React.ReactNode;
  tone: string;
  chartIndex?: ChartIndex;
  ragChartIndex?: Record<string, OverviewChartRef>;
  sources?: OverviewResponse["_source_index"];
}) {
  if (!items || items.length === 0) return null;
  return (
    <div className="rounded-lg border bg-card p-3">
      <div className={`mb-2 flex items-center gap-1.5 text-sm font-semibold ${tone}`}>
        {icon}
        {title}
      </div>
      <ul className="space-y-1.5">
        {items.map((b, i) => (
          <li key={i} className="flex gap-1.5 text-xs leading-relaxed">
            <span className={`mt-1 h-1 w-1 shrink-0 rounded-full ${tone.replace("text-", "bg-")}`} />
            <span>
              <HoverCitedText text={b} chartIndex={chartIndex} ragChartIndex={ragChartIndex} sources={sources} />
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function AreteLatestSection({
  arete,
  chartIndex,
  ragChartIndex,
  sources,
}: {
  arete?: OverviewResponse["arete_latest"];
  chartIndex?: ChartIndex;
  ragChartIndex?: Record<string, OverviewChartRef>;
  sources?: OverviewResponse["_source_index"];
}) {
  const cards = (arete?.cards || []).filter((c) => c.topic || (c.bullets && c.bullets.length));
  const unavailable = arete?.unavailable_bullets || [];
  if (!arete || (cards.length === 0 && unavailable.length === 0)) return null;

  return (
    <section className="rounded-lg border border-slate-300/70 bg-slate-50/70 p-4">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <div className="flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-slate-700" />
          <h3 className="text-base font-bold text-slate-900">{arete.title || "ARETE Latest PDF Summary"}</h3>
        </div>
        {arete.source && arete.source !== "N/A" && (
          <span className="text-[11px] font-medium text-slate-600">{arete.source}</span>
        )}
      </div>
      {unavailable.length > 0 && (
        <ul className="space-y-1.5 rounded-md border border-slate-200 bg-white p-3">
          {unavailable.map((bullet, i) => (
            <li key={i} className="flex gap-1.5 text-xs leading-relaxed text-slate-700">
              <span className="mt-1 h-1 w-1 shrink-0 rounded-full bg-slate-500" />
              <span>{bullet}</span>
            </li>
          ))}
        </ul>
      )}
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
        {cards.map((card, i) => (
          <div key={`${card.topic}-${i}`} className="rounded-md border border-slate-200 bg-white p-3">
            <h4 className="mb-1.5 text-xs font-bold uppercase tracking-wide text-slate-700">{card.topic}</h4>
            <ul className="space-y-1.5">
              {(card.bullets || []).map((b, j) => (
                <li key={j} className="flex gap-1.5 text-xs leading-relaxed text-slate-800">
                  <span className="mt-1 h-1 w-1 shrink-0 rounded-full bg-slate-500" />
                  <span>
                    <HoverCitedText text={b} chartIndex={chartIndex} ragChartIndex={ragChartIndex} sources={sources} />
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </section>
  );
}

export function OverviewTab() {
  const [sub, setSub] = useState<Sub>("industry");
  const [brokerSelection, setBrokerSelection] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const qc = useQueryClient();
  const { newChunks, acknowledge } = useCorpusBaseline("overview");

  const { data: chartIndex } = useQuery({ queryKey: ["chartIndex"], queryFn: api.chartIndex, staleTime: 300_000 });
  const { data, isLoading, error } = useQuery<OverviewResponse>({
    queryKey: ["overview", sub],
    queryFn: () => (sub === "industry" ? api.industryOverview() : api.companyOverview()),
    staleTime: Infinity,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
  const { data: brokerChoices, isLoading: brokersLoading, error: brokersError } = useQuery({
    queryKey: ["overview", "industry", "brokers"],
    queryFn: api.industryBrokers,
    enabled: sub === "industry",
    staleTime: 300_000,
  });
  const brokerOptions = brokerChoices?.brokers || [];
  const selectedBroker = brokerOptions.includes(brokerSelection)
    ? brokerSelection
    : brokerOptions.includes("GS") ? "GS" : brokerOptions[0] || "";
  const { data: brokerData, isLoading: brokerLoading, error: brokerError, refetch: refetchBroker } = useQuery<OverviewResponse>({
    queryKey: ["overview", "industry", selectedBroker],
    queryFn: () => api.industryOverview(false, selectedBroker),
    enabled: sub === "industry" && Boolean(selectedBroker) && !isLoading,
    staleTime: Infinity,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });

  const refresh = useCallback(async () => {
    setRefreshing(true);
    setRefreshError(null);
    try {
      const fresh = sub === "industry"
        ? await api.industryOverview(true, "Arete")
        : await api.companyOverview(true);
      if (fresh.error) {
        setRefreshError(fresh.error);
      }
      qc.setQueryData(["overview", sub], fresh);
      if (sub === "industry" && selectedBroker) {
        const brokerFresh = await api.industryOverview(true, selectedBroker);
        qc.setQueryData(["overview", "industry", selectedBroker], brokerFresh);
        if (brokerFresh.error) setRefreshError(brokerFresh.error);
      }
      acknowledge();  // baseline = current corpus points, banner dismisses
    } catch (err) {
      setRefreshError(String(err));
    } finally {
      setRefreshing(false);
    }
  }, [sub, selectedBroker, qc, acknowledge]);

  const sources = data?._source_index;
  // RAG-sourced chart index baked into the Overview response. Preferred over
  // the legacy /chartindex local-DB query (kept for backwards compat with
  // older cached overviews that don't carry _chart_index).
  const ragChartIndex = data?._chart_index;
  const industrySectionGroups = useMemo(() => {
    const sections = data?.sections || [];
    const order = ["Demand & Supply Summary", "Demand", "Supply"];
    const groups = new Map<string, typeof sections>();
    for (const sec of sections) {
      const group = sec.group || "Other";
      groups.set(group, [...(groups.get(group) || []), sec]);
    }
    return Array.from(groups.entries()).sort((a, b) => {
      const ai = order.indexOf(a[0]);
      const bi = order.indexOf(b[0]);
      return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
    });
  }, [data?.sections]);

  return (
    <div className="mx-auto max-w-[1100px] px-6 py-6">
      {/* Sub-toggle + refresh */}
      <div className="mb-5 flex items-center justify-between">
        <div className="inline-flex rounded-md border p-0.5">
          {(["industry", "company"] as Sub[]).map((s) => (
            <button
              key={s}
              onClick={() => {
                setSub(s);
                setRefreshError(null);
              }}
              className={`rounded px-4 py-1.5 text-sm font-medium capitalize transition-colors ${
                sub === s ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted"
              }`}
            >
              {s}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-3">
          {data?.last_updated && (
            <span className="text-[11px] text-muted-foreground">Updated {data.last_updated}</span>
          )}
          <button
            onClick={refresh}
            disabled={refreshing}
            className="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            title="Regenerate from the latest fact pool (slow)"
          >
            {refreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
            {refreshing ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </div>

      {newChunks > 0 && !refreshing && (
        <div className="mb-4 flex items-center justify-between rounded-md border border-amber-300/60 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          <span className="flex items-center gap-1.5">
            <Sparkles className="h-3.5 w-3.5" />
            <strong>{newChunks.toLocaleString()}</strong> new chunk{newChunks === 1 ? "" : "s"} in the fact pool since this view was generated.
          </span>
          <button
            onClick={refresh}
            className="rounded border border-amber-400 bg-white px-2 py-0.5 text-[11px] font-semibold text-amber-900 hover:bg-amber-100"
          >
            Refresh now
          </button>
        </div>
      )}

      {(isLoading || refreshing) && (
        <div className="py-20 text-center text-muted-foreground">
          <Loader2 className="mx-auto mb-3 h-6 w-6 animate-spin" />
          {refreshing ? "Regenerating overview from the fact pool…" : "Loading overview…"}
          <p className="mt-1 text-[11px]">First generation pulls per-section research and can take ~30s.</p>
        </div>
      )}

      {error && !refreshing && (
        <div className="rounded-md border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          Failed to load overview: {String(error)}
        </div>
      )}
      {data?.error && (
        <div className="rounded-md border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          {data.error}
        </div>
      )}
      {refreshError && !refreshing && (
        <div className="mb-4 rounded-md border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          Refresh failed: {refreshError}
        </div>
      )}

      {!isLoading && !refreshing && data?.answer_markdown && !data.error && (
        sub === "industry" ? (
          <div className="space-y-4">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <h2 className="text-lg font-bold">Arete</h2>
              <span className="text-xs text-muted-foreground">ES research · {data.last_updated}</span>
            </div>
            <OverviewSections markdown={data.answer_markdown} sources={sources} />
          </div>
        ) : (
          <div className="space-y-4">
            <div className="text-xs text-muted-foreground">ES research · {data.last_updated}</div>
            <OverviewSections markdown={data.answer_markdown} sources={sources} headingLevel={1} />
          </div>
        )
      )}
      {sub === "industry" && !refreshing && (
        <div className="mt-8 space-y-4">
          <div className="flex flex-col items-start gap-2">
            <h2 className="text-lg font-bold">Other broker</h2>
            <select
              aria-label="Select broker"
              value={selectedBroker}
              onChange={(event) => setBrokerSelection(event.target.value)}
              disabled={brokersLoading || brokerOptions.length === 0}
              className="rounded-md border bg-background px-3 py-2 text-sm font-medium"
            >
              {brokerOptions.length === 0 && <option value="">Select broker</option>}
              {brokerOptions.map((broker) => (
                <option key={broker} value={broker}>{broker === "BOFA" ? "BofA" : broker}</option>
              ))}
            </select>
          </div>
          {brokersLoading && <p className="text-sm text-muted-foreground">Finding recent broker sources…</p>}
          {brokersError && <p className="text-sm text-destructive">Could not load broker choices: {String(brokersError)}</p>}
          {!brokersLoading && !brokersError && !selectedBroker && (
            <p className="text-sm text-muted-foreground">No other broker sources in this evidence window.</p>
          )}
          {selectedBroker && brokerLoading && (
            <p className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading {selectedBroker} summary…
            </p>
          )}
          {brokerError && (
            <div className="flex flex-wrap items-center gap-3 text-sm text-destructive">
              <span>Could not load {selectedBroker}: {String(brokerError)}</span>
              <button type="button" onClick={() => void refetchBroker()} className="rounded border px-2 py-1 font-semibold">
                Retry
              </button>
            </div>
          )}
          {brokerData?.error && <p className="text-sm text-destructive">{brokerData.error}</p>}
          {brokerData?.answer_markdown && !brokerLoading && !brokerData.error && (
            <>
              <div className="text-xs text-muted-foreground">ES research · {brokerData.last_updated}</div>
              <OverviewSections markdown={brokerData.answer_markdown} sources={brokerData._source_index} />
            </>
          )}
        </div>
      )}
      {!isLoading && !refreshing && data && !data.error && !data.answer_markdown && (
        <div className="space-y-6">
          <div>
            <h2 className="text-xl font-bold">{data.title}</h2>
            {data.overall_bullets && data.overall_bullets.length > 0 ? (
              <ul className="mt-2 space-y-1.5">
                {data.overall_bullets.map((b, i) => (
                  <li key={i} className="flex gap-2 text-sm leading-relaxed text-foreground/90">
                    <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-primary/60" />
                    <span>
                      <HoverCitedText text={b} chartIndex={chartIndex} ragChartIndex={ragChartIndex} sources={sources} />
                    </span>
                  </li>
                ))}
              </ul>
            ) : data.overall_summary && data.overall_summary !== "N/A" && (
              <ul className="mt-2 space-y-1.5">
                <li className="flex gap-2 text-sm leading-relaxed text-foreground/90">
                  <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-primary/60" />
                  <span><HoverCitedText text={data.overall_summary} chartIndex={chartIndex} ragChartIndex={ragChartIndex} sources={sources} /></span>
                </li>
              </ul>
            )}
          </div>

          <AreteLatestSection
            arete={data.arete_latest}
            chartIndex={chartIndex}
            ragChartIndex={ragChartIndex}
            sources={sources}
          />

          {/* Bull / Bear / Uncertain */}
          {(data.bull_case || data.bear_case || data.uncertain) && (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
              <CaseList title="Bull case" items={data.bull_case} icon={<TrendingUp className="h-4 w-4" />} tone="text-emerald-600" chartIndex={chartIndex} ragChartIndex={ragChartIndex} sources={sources} />
              <CaseList title="Bear case" items={data.bear_case} icon={<TrendingDown className="h-4 w-4" />} tone="text-red-600" chartIndex={chartIndex} ragChartIndex={ragChartIndex} sources={sources} />
              <CaseList title="Uncertain / debated" items={data.uncertain} icon={<HelpCircle className="h-4 w-4" />} tone="text-amber-600" chartIndex={chartIndex} ragChartIndex={ragChartIndex} sources={sources} />
            </div>
          )}

          {/* Industry sections */}
          {industrySectionGroups.map(([group, sections]) => (
            <div key={group} className="space-y-3">
              {group !== "Other" && (
                <div className="border-b pb-1">
                  <h3 className="text-sm font-bold uppercase tracking-wide text-muted-foreground">{group}</h3>
                </div>
              )}
              {sections.map((sec) => (
                <section key={sec.topic} className="rounded-lg border bg-card p-4">
                  <div className="mb-2 flex items-baseline justify-between gap-3">
                    <h4 className="text-base font-bold">{sec.topic}</h4>
                    {sec.unit && <span className="shrink-0 text-[11px] text-muted-foreground">{sec.unit}</span>}
                  </div>
                  {(sec.summary_bullets?.length || (sec.summary && sec.summary !== "N/A")) ? (
                    <ul className="mb-3 space-y-1.5 text-xs text-muted-foreground">
                      {(sec.summary_bullets?.length ? sec.summary_bullets : [sec.summary!]).map((bullet, i) => (
                        <li key={i} className="flex gap-1.5 leading-relaxed">
                          <span className="mt-1 h-1 w-1 shrink-0 rounded-full bg-primary/50" />
                          <span><HoverCitedText text={bullet} chartIndex={chartIndex} ragChartIndex={ragChartIndex} sources={sources} /></span>
                        </li>
                      ))}
                    </ul>
                  ) : null}
                  <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                    {sec.themes?.map((t, i) => (
                      <div key={i} className="rounded border bg-background p-2.5">
                        <h5 className="mb-1.5 text-xs font-semibold">{t.title}</h5>
                        <ul className="space-y-1">
                          {t.bullets?.map((b, j) => (
                            <li key={j} className="flex gap-1.5 text-xs leading-relaxed">
                              <span className="mt-1 h-1 w-1 shrink-0 rounded-full bg-primary/50" />
                              <span>
                                <HoverCitedText text={b} chartIndex={chartIndex} ragChartIndex={ragChartIndex} sources={sources} />
                              </span>
                            </li>
                          ))}
                        </ul>
                      </div>
                    ))}
                  </div>
                </section>
              ))}
            </div>
          ))}

          {/* Company entries */}
          {data.companies?.map((co) => (
            <section key={co.name} className="rounded-lg border bg-card p-4">
              <h3 className="text-base font-bold">{co.name}</h3>
              {(co.summary_bullets?.length || co.summary) ? (
                <ul className="mb-3 mt-1 space-y-1.5 text-xs text-muted-foreground">
                  {(co.summary_bullets?.length ? co.summary_bullets : [co.summary!]).map((bullet, i) => (
                    <li key={i} className="flex gap-1.5 leading-relaxed">
                      <span className="mt-1 h-1 w-1 shrink-0 rounded-full bg-primary/50" />
                      <span><HoverCitedText text={bullet} chartIndex={chartIndex} ragChartIndex={ragChartIndex} sources={sources} /></span>
                    </li>
                  ))}
                </ul>
              ) : null}
              <div className="space-y-3">
                {co.segments?.map((seg, i) => (
                  <div key={i}>
                    <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                      {seg.topic}
                    </h4>
                    <div className="space-y-1.5 pl-2">
                      {seg.views?.map((v, j) => (
                        <ul key={j} className="space-y-1">
                          {v.bullets?.map((b, k) => (
                            <li key={k} className="flex gap-1.5 text-xs leading-relaxed">
                              <span className="mt-1 h-1 w-1 shrink-0 rounded-full bg-primary/50" />
                              <span>
                                <span className="font-medium">{v.broker}: </span>
                                <HoverCitedText text={b} chartIndex={chartIndex} ragChartIndex={ragChartIndex} sources={sources} />
                              </span>
                            </li>
                          ))}
                        </ul>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
              {co.strategic_updates && co.strategic_updates.length > 0 && (
                <div className="mt-3 rounded border bg-background p-2.5">
                  <div className="mb-1 flex items-center gap-1.5 text-xs font-semibold">
                    <FileText className="h-3.5 w-3.5" /> Strategic updates
                  </div>
                  <ul className="space-y-1">
                    {co.strategic_updates.map((s, i) => (
                      <li key={i} className="flex gap-1.5 text-xs leading-relaxed">
                        <span className="mt-1 h-1 w-1 shrink-0 rounded-full bg-primary/50" />
                        <span>
                          <HoverCitedText text={s} chartIndex={chartIndex} ragChartIndex={ragChartIndex} sources={sources} />
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
