import { useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Loader2, BarChart3, AlertTriangle } from "lucide-react";
import { api, sourcePdfUrl, type DashboardFrequency, type DashMetric, type DashSeries, type SeriesPoint } from "../lib/api";
import { canonicalBrokerName, splitBrokerSeriesName } from "../lib/brokerColors";
import { LineChart } from "./LineChart";

function brokerKey(name: string): string {
  return canonicalBrokerName(name).toLowerCase();
}

function groupSeriesByBroker(series: DashSeries[]): { broker: string; series: DashSeries[] }[] {
  const groups = new Map<string, { broker: string; series: DashSeries[] }>();
  for (const s of series) {
    const broker = canonicalBrokerName(s.broker) || s.broker || "Unknown";
    const key = brokerKey(broker);
    if (!groups.has(key)) groups.set(key, { broker, series: [] });
    groups.get(key)!.series.push(s);
  }
  return Array.from(groups.values()).sort((a, b) => a.broker.localeCompare(b.broker));
}

function variantLabel(name: string): string {
  const parsed = splitBrokerSeriesName(name);
  return parsed.variant || parsed.broker || name;
}

function disambiguateDuplicateVariants(group: { broker: string; series: DashSeries[] }): DashSeries[] {
  const counts = new Map<string, number>();
  for (const s of group.series) {
    const key = variantLabel(s.broker).toLowerCase();
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  return group.series.map((s) => {
    const parsed = splitBrokerSeriesName(s.broker);
    const variant = parsed.variant || parsed.broker || s.broker;
    if ((counts.get(variant.toLowerCase()) || 0) <= 1) return s;
    const sourceBroker = parsed.broker && parsed.broker !== group.broker ? ` (${parsed.broker})` : "";
    return { ...s, broker: `${group.broker} - ${variant}${sourceBroker}` };
  });
}

function DashboardChartCard({
  metric,
  onPointClick,
}: {
  metric: DashMetric;
  onPointClick: (s: DashSeries, p: SeriesPoint) => void;
}) {
  const brokerGroups = metric.split_by_broker ? groupSeriesByBroker(metric.series) : [];
  const split = metric.split_by_broker && brokerGroups.length > 0;

  return (
    <div className={`rounded-lg border bg-card p-3 ${split ? "lg:col-span-2 xl:col-span-3" : ""}`}>
      <div className="mb-1 flex items-baseline justify-between gap-2">
        <h4 className="text-sm font-semibold leading-tight">{metric.label}</h4>
        <span className="shrink-0 text-[10px] text-muted-foreground">{metric.unit}</span>
      </div>
      {split ? (
        <div className="grid grid-cols-1 gap-x-5 gap-y-4 xl:grid-cols-2">
          {brokerGroups.map((group) => (
            <div key={group.broker} className="min-w-0 border-t pt-2 first:border-t-0 xl:border-t">
              <div className="mb-1 flex items-center justify-between gap-2 px-1">
                <span className="text-xs font-bold">{group.broker}</span>
                <span className="text-[10px] text-muted-foreground">
                  {group.series.length} line{group.series.length === 1 ? "" : "s"}
                </span>
              </div>
              <LineChart
                series={disambiguateDuplicateVariants(group)}
                unit={metric.unit}
                height={190}
                legendMode="variant"
                onPointClick={onPointClick}
              />
            </div>
          ))}
        </div>
      ) : (
        <LineChart
          series={metric.series}
          unit={metric.unit}
          onPointClick={onPointClick}
        />
      )}
    </div>
  );
}

export function DashboardTab() {
  const qc = useQueryClient();
  const frequency: DashboardFrequency = "yearly";
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["dashboard", frequency],
    queryFn: () => api.dashboard(frequency),
    staleTime: 60_000,
  });

  const segments = data?.segments ?? [];
  const totalPanels = segments.reduce((n, s) => n + s.metrics.length, 0);
  const suppressed = data?.suppressed_points ?? 0;
  const needsReview = data?.needs_review_points ?? 0;
  const cache = data?.cache;
  const cacheNeedsAttention = !!cache && cache.status !== "ready";

  useEffect(() => {
    if (!cacheNeedsAttention) return;
    const timer = window.setInterval(() => {
      qc.invalidateQueries({ queryKey: ["dashboard", frequency] });
    }, 2500);
    return () => window.clearInterval(timer);
  }, [cacheNeedsAttention, frequency, qc]);

  const onPointClick = (
    _panelLabel: string,
    _panelUnit: string,
    _series: DashSeries,
    point: SeriesPoint,
  ) => {
    const filename = point.source?.filename || "";
    const page = point.source?.page || 0;
    if (!filename || !page || !filename.toLowerCase().endsWith(".pdf")) return;
    const href = sourcePdfUrl(filename, page);
    if (href) window.open(href, "_blank", "noopener,noreferrer");
  };

  return (
    <div className="mx-auto max-w-[1800px] px-6 py-6">
      <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-[280px] flex-1">
          <h2 className="text-lg font-bold">Time-Series Dashboard</h2>
          <p className="text-sm text-muted-foreground">
            Unit-normalized yearly datapoints extracted from indexed reports - one line per provider
            {totalPanels > 0 ? ` - ${totalPanels} charts` : ""}
            <span className="ml-2 text-[11px]">- hover a point for value/source - click PDF-backed points to open source - click a legend chip to hide/show that provider</span>
          </p>
        </div>
        {suppressed > 0 && (
          <div className="flex items-center gap-2 rounded-md border border-amber-300/60 bg-amber-50 px-3 py-1.5 text-[11px] font-medium text-amber-900">
            <AlertTriangle className="h-3.5 w-3.5" />
            {suppressed} quarantined value{suppressed === 1 ? "" : "s"} hidden from charts
            {needsReview > 0 ? ` - ${needsReview} need review` : ""}
          </div>
        )}
        <button
          type="button"
          onClick={() => refetch()}
          className="flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          title="Reload dashboard datapoints"
        >
          <RefreshCw className="h-4 w-4" />
          Reload
        </button>
      </div>

      {cache && cache.status !== "ready" ? (
        <div className="mb-4 rounded-md border border-sky-300/60 bg-sky-50 px-3 py-2 text-xs text-sky-950">
          Dashboard cache is {cache.status}
          {cache.dirty_reason ? ` - ${cache.dirty_reason}` : ""}.
          {cache.status === "building"
            ? " Charts will update when the background build finishes."
            : " Showing cached data for now."}
        </div>
      ) : null}

      {isLoading && (
        <div className="py-20 text-center text-muted-foreground">
          <Loader2 className="mx-auto mb-3 h-6 w-6 animate-spin" />
          Loading datapoints...
        </div>
      )}

      {error && (
        <div className="rounded-md border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          Failed to load dashboard: {String(error)}
        </div>
      )}

      {!isLoading && !error && totalPanels === 0 && (
        <div className="py-20 text-center">
          <BarChart3 className="mx-auto mb-3 h-10 w-10 opacity-30" />
          <p className="text-muted-foreground">
            No yearly datapoints yet. Upload reports to extract numeric series.
          </p>
        </div>
      )}

      {segments.map((segment) => (
        <section key={segment.segment} className="mb-8">
          <h3 className="mb-3 text-sm font-bold uppercase tracking-wide text-muted-foreground">
            {segment.segment}
          </h3>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 xl:grid-cols-3">
            {segment.metrics.map((metric) => (
              <DashboardChartCard
                key={metric.metric}
                metric={metric}
                onPointClick={(series, point) => onPointClick(metric.label, metric.unit, series, point)}
              />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
